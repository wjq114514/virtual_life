"""Matrix -> Virtual Life bridge runtime.

Registers a node (a "room" is the Matrix conversation), heartbeats with a
presence context, maps room messages to dialogue turns, maps user presence to
attention samples, and sends companion replies back to Matrix rooms.
"""

import logging
import random
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from .api import ApiClient, TransportError
from .config import MatrixConfig, RoomBinding
from .matrix import MatrixClient, MatrixError, MatrixEventStream, extract_text

LOGGER = logging.getLogger(__name__)

CAPABILITIES = ["text.display", "attention.observe", "matrix.message"]
MAX_MESSAGE_LENGTH = 4096


def utc_timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(UTC)


class MatrixRuntime:
    def __init__(
        self,
        config: MatrixConfig,
        api: ApiClient,
        client: MatrixClient,
        e2ee_bridge: Any | None = None,
    ):
        self._config = config
        self._api = api
        self._client = client
        self._e2ee = e2ee_bridge
        self._sequence = 0
        self._session_id = str(uuid.uuid4())
        self._registered = False
        self._since: str | None = None
        # FIFO queue of (room_id, text, delay_seconds) triples. A dict would
        # collapse multi-bubble replies for the same room, so replies are
        # paced one message per drain_sends() cycle instead. delay_seconds is
        # None for bubbles paced by the interval+jitter fallback; when the
        # server supplies reply_delays each bubble carries its own pre-send
        # wait (typing rhythm) and interval gating is skipped.
        self._send_queue: deque[tuple[str, str, float | None]] = deque()
        self._lock = threading.Lock()
        self._next_send_at = 0.0
        # Injectable for tests: fake clock / fake sleep keep pacing and
        # backoff assertions deterministic without real delays.
        self._monotonic = time.monotonic
        self._sleep = time.sleep
        self._verification: dict[str, Any] | None = None
        # Message handling pool: sync returns instantly, each room's events
        # are processed FIFO on a worker so one slow dialogue call never
        # blocks other rooms (instant receive -> instant trigger).
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, config.handle_workers), thread_name_prefix="mx-handle"
        )
        self._dispatch_lock = threading.Lock()
        self._room_events: dict[str, deque[dict[str, Any]]] = {}
        self._room_busy: set[str] = set()
        # Sender thread: drains the send queue without waiting for the next
        # sync long-poll round, so a reply leaves as soon as it is queued.
        self._send_cond = threading.Condition(self._lock)
        self._sender: threading.Thread | None = None
        self._sender_stop = threading.Event()
        # Typing observation: (room_id) -> last monotonic attention report.
        self._typing_seen: dict[str, float] = {}
        if self._e2ee is not None:
            self._e2ee.on_message(self._on_sidecar_message)
            self._e2ee.on_verification(self._on_sidecar_verification)

    # -- lifecycle ---------------------------------------------------------

    def register(self) -> None:
        self._api.register(self._config.node_id, self._config.label, CAPABILITIES)
        self._registered = True
        LOGGER.info("registered node=%s capabilities=%s", self._config.node_id, CAPABILITIES)

    def heartbeat(self) -> None:
        context = {"presence": {"source": "matrix", "status": "online"}}
        self._api.heartbeat(self._config.node_id, context)

    def _attention_sample(self, *, user_present: bool, typing: bool = False) -> dict[str, Any]:
        self._sequence += 1
        if self._sequence > 2**63 - 1:
            self._sequence = 1
        return {
            "schema_version": "1.0",
            "session_id": self._session_id,
            "sequence": self._sequence,
            "sampled_at": utc_timestamp(),
            "ttl_seconds": 90,
            "activity": "chat" if typing else "unknown",
            "focus": "focused" if typing else "unknown",
            "availability": "available" if user_present else "unknown",
            "user_present": user_present,
            "locked": False,
            "meeting": False,
            "screen_sharing": False,
            "boundary": "none",
            "boundary_id": None,
        }

    def report_presence(self, *, user_present: bool, typing: bool = False) -> None:
        sample = self._attention_sample(user_present=user_present, typing=typing)
        try:
            self._api.attention(
                self._config.node_id, {"command_id": str(uuid.uuid4()), "sample": sample}
            )
        except TransportError as exc:
            LOGGER.warning("attention_failed reason=%s", exc.code)

    def _companion_for(self, room_id: str) -> str | None:
        for binding in self._config.rooms:
            if binding.room_id == room_id:
                return binding.companion_id
        return None

    def _binding_for(self, room_id: str, sender: str) -> RoomBinding | None:
        """Return the binding that matches the room and, if the binding
        restricts senders, the sender. A binding without a `senders` allow-list
        accepts any sender in the room."""
        for binding in self._config.rooms:
            if binding.room_id != room_id:
                continue
            if binding.senders and sender not in binding.senders:
                LOGGER.info("sender_not_allowed room_id=%s sender=%s", room_id, sender)
                continue
            return binding
        return None

    # -- Matrix event handling --------------------------------------------

    def _handle_message(self, event: dict[str, Any]) -> None:
        room_id = event.get("room_id")
        if not isinstance(room_id, str):
            return
        sender = event.get("sender")
        if not isinstance(sender, str) or sender == self._config.user_id:
            return
        text = extract_text(event)
        if not text:
            return
        binding = self._binding_for(room_id, sender)
        if binding is None:
            LOGGER.info("no_companion room_id=%s", room_id)
            return
        companion = binding.companion_id
        # A message arriving IS user presence: report it before the (possibly
        # slow) dialogue round-trip so attention reflects the moment of speech.
        self.report_presence(user_present=True, typing=True)
        try:
            state = self._api.get_companion(companion)
        except TransportError as exc:
            LOGGER.warning("companion_unavailable reason=%s", exc.code)
            return
        # The companion may still be owned by another node (e.g. the Android
        # app claimed it). Room activity on this Matrix node is authoritative
        # for where the lifeform currently is, so best-effort follow first.
        # follow_room is idempotent when already in this node.
        try:
            self._api.follow_room(
                companion,
                {
                    "command_id": "matrix-follow-" + str(uuid.uuid4()),
                    "target_node_id": self._config.node_id,
                    "expected_revision": state.get("revision", 0),
                    "reason": "room_activity",
                },
            )
        except TransportError as exc:
            LOGGER.info("follow_skipped reason=%s", exc.code)
        # Re-read state after the (possible) follow so revision/epoch are fresh.
        try:
            state = self._api.get_companion(companion)
        except TransportError as exc:
            LOGGER.warning("companion_unavailable reason=%s", exc.code)
            return
        revision = state.get("revision", 0)
        epoch = state.get("epoch", 0)
        payload: dict[str, Any] = {
            "command_id": "matrix-" + str(uuid.uuid4()),
            "node_id": self._config.node_id,
            "epoch": epoch,
            "expected_revision": revision,
            "user_message": text[:MAX_MESSAGE_LENGTH],
            "user_name": sender,
            "max_output_tokens": self._config.max_output_tokens,
        }
        # The companion resides on this Matrix node, but the screen may be
        # captured by a separate observer node (e.g. Android). Name it so the
        # runtime injects that observer's fresh frames into this dialogue turn.
        if self._config.screen_observer_node:
            payload["screen_reference"] = {
                "node_id": self._config.screen_observer_node,
                "display_id": "default",
            }
        try:
            result = self._api.dialogue(companion, payload)
        except TransportError as exc:
            LOGGER.warning("dialogue_failed reason=%s", exc.code)
            return
        status = result.get("status")
        if status in ("completed", "silent") and result.get("assistant_message"):
            # The server may split a long reply into short netizen-style bubbles
            # (reply_messages). Send each bubble as its own message; fall back to
            # the single canonical assistant_message when no split is present.
            # max_messages_per_reply is a second cap aligned with the server's
            # reply_split_max_messages.
            bubbles = result.get("reply_messages") or [result["assistant_message"]]
            bubbles = bubbles[: self._config.max_messages_per_reply]
            delays: list[float | None] = [None] * len(bubbles)
            if self._config.use_reply_delays:
                raw_delays = result.get("reply_delays")
                if isinstance(raw_delays, list):
                    for index, value in enumerate(raw_delays[: len(bubbles)]):
                        if isinstance(value, (int, float)) and 0 <= value <= 60_000:
                            delays[index] = round(value / 1000.0, 3)
            for bubble, delay in zip(bubbles, delays, strict=True):
                self._send_text(room_id, bubble, delay)

    def _handle_room_message(self, event: dict[str, Any]) -> None:
        self._handle_message(event)

    # -- dispatch (instant receive -> instant trigger) -----------------------

    def dispatch_message(self, event: dict[str, Any]) -> None:
        """Queue a room message for handling. Same-room events keep FIFO
        order; different rooms proceed in parallel on the worker pool, so one
        slow dialogue call never delays another room's trigger."""
        room_id = event.get("room_id")
        if not isinstance(room_id, str):
            return
        with self._dispatch_lock:
            queue = self._room_events.setdefault(room_id, deque())
            queue.append(event)
            if room_id in self._room_busy:
                return
            self._room_busy.add(room_id)
        self._pool.submit(self._drain_room, room_id)

    def _drain_room(self, room_id: str) -> None:
        while True:
            with self._dispatch_lock:
                queue = self._room_events.get(room_id)
                if not queue:
                    self._room_busy.discard(room_id)
                    return
                event = queue.popleft()
            try:
                self._handle_room_message(event)
            except Exception:  # noqa: BLE001 - worker must survive any handler fault
                LOGGER.exception("handle_failed room_id=%s", room_id)

    def wait_dispatched(self, timeout: float = 10.0) -> None:
        """Block until every dispatched message has been handled (tests/liveness)."""
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            with self._dispatch_lock:
                if not self._room_events and not self._room_busy:
                    return
            self._sleep(0.01)

    # -- typing observation --------------------------------------------------

    def handle_typing(self, room_id: str, user_id: str) -> None:
        """Report attention when a bound-room user starts typing. Throttled
        per room so sync bursts do not flood the attention endpoint."""
        if not self._config.observe_typing:
            return
        if user_id == self._config.user_id:
            return
        if self._binding_for(room_id, user_id) is None:
            return
        now = self._monotonic()
        last = self._typing_seen.get(room_id)
        if last is not None and now - last < self._config.typing_attention_min_interval:
            return
        self._typing_seen[room_id] = now
        self.report_presence(user_present=True, typing=True)

    # -- E2EE sidecar path -------------------------------------------------

    def _on_sidecar_message(self, event: dict[str, Any]) -> None:
        """Handle a plaintext message emitted by the E2EE sidecar."""
        room_id = event.get("room_id")
        sender = event.get("sender")
        body = event.get("body")
        if not isinstance(room_id, str) or not isinstance(sender, str) or not isinstance(body, str):
            return
        # Normalize into the same shape _handle_message expects.
        normalized = {
            "type": "m.room.message",
            "room_id": room_id,
            "sender": sender,
            "event_id": event.get("event_id"),
            "origin_server_ts": event.get("ts"),
            "content": {"msgtype": "m.text", "body": body},
        }
        self.dispatch_message(normalized)

    def _on_sidecar_verification(self, event: dict[str, Any]) -> None:
        """Handle a SAS verification event emitted by the E2EE sidecar.

        We keep the latest SAS state so the operator (or an external driver)
        can confirm/mismatch/cancel the flow, and log a clear prompt when the
        emoji/decimal are ready for comparison on another trusted device.
        """
        self._verification = event
        state = event.get("state")
        if state == "sas":
            emoji = event.get("emoji") or []
            decimal = event.get("decimal") or []
            LOGGER.info(
                "verification_prompt transaction_id=%s emoji=%s decimal=%s",
                event.get("transaction_id"),
                emoji,
                decimal,
            )
        elif state == "done":
            LOGGER.info("verification_done transaction_id=%s", event.get("transaction_id"))
        elif state == "cancelled":
            LOGGER.info(
                "verification_cancelled transaction_id=%s reason=%s",
                event.get("transaction_id"),
                event.get("reason"),
            )

    def verification_state(self) -> dict[str, Any] | None:
        """Return the latest SAS verification event (or None)."""
        return self._verification

    def verification_confirm(self, transaction_id: str | None = None) -> dict[str, Any]:
        return self._send_verification("confirm", transaction_id)

    def verification_mismatch(self, transaction_id: str | None = None) -> dict[str, Any]:
        return self._send_verification("mismatch", transaction_id)

    def verification_cancel(self, transaction_id: str | None = None) -> dict[str, Any]:
        return self._send_verification("cancel", transaction_id)

    def _send_verification(self, action: str, transaction_id: str | None) -> dict[str, Any]:
        if self._e2ee is None:
            raise RuntimeError("E2EE not configured; verification unavailable")
        return self._e2ee.send_verification_command(action, transaction_id)

    # -- sending -----------------------------------------------------------

    def _send_text(self, room_id: str, text: str, delay: float | None = None) -> None:
        if not text:
            return
        with self._send_cond:
            self._send_queue.append((room_id, text[:MAX_MESSAGE_LENGTH], delay))
            self._send_cond.notify_all()

    def _interval_seconds(self) -> float:
        return self._config.send_interval_min_seconds + random.uniform(
            0, self._config.send_interval_jitter_seconds
        )

    def _typing_duration_seconds(self, text: str) -> float:
        """Estimated typing time for a bubble: length-proportional with a floor."""
        proportional = len(text) / max(self._config.typing_chars_per_second, 0.5)
        return max(self._config.typing_min_seconds, proportional)

    def _set_typing(self, room_id: str, text: str, timeout_ms: int | None = None) -> None:
        if timeout_ms is None:
            timeout_ms = int(self._typing_duration_seconds(text) * 1000)
        try:
            self._client.set_typing(room_id, self._config.user_id, timeout_ms)
        except (MatrixError, OSError) as exc:
            # Typing is a hint, never a gate: log and continue.
            LOGGER.info("typing_failed room_id=%s reason=%s", room_id, exc)

    def _deliver_once(self, room_id: str, text: str) -> bool:
        try:
            if self._e2ee is not None:
                self._e2ee.send_command({"cmd": "send", "room_id": room_id, "body": text})
            else:
                self._client.send_text(room_id, text)
            return True
        except (MatrixError, OSError) as exc:
            LOGGER.warning("send_failed room_id=%s reason=%s", room_id, exc)
            return False

    def _deliver_with_retry(self, room_id: str, text: str) -> bool:
        attempt = 0
        while True:
            if self._deliver_once(room_id, text):
                return True
            if attempt >= self._config.send_max_retries:
                return False
            delay = self._config.retry_backoff_initial_seconds * (2**attempt)
            LOGGER.warning(
                "send_retry room_id=%s attempt=%d delay=%.1fs", room_id, attempt + 1, delay
            )
            self._sleep(delay)
            attempt += 1

    def drain_sends(self) -> None:
        """Send at most one queued bubble per call.

        Two pacing modes: bubbles carrying a server-supplied reply_delays
        value wait exactly that long before sending (typing notifier held for
        the whole wait) and skip interval gating — the rhythm the runtime
        computed from bubble length. Bubbles without a delay are paced by
        interval + jitter as before. Failures retry with exponential backoff
        up to send_max_retries; when retries are exhausted and merge_on_limit
        is set, the remaining bubbles for the same room are merged into one
        message and sent as a final attempt.
        """
        if self._monotonic() < self._next_send_at:
            return
        with self._send_cond:
            if not self._send_queue:
                return
            room_id, text, delay = self._send_queue.popleft()
        if delay is not None:
            if self._config.typing_before_send:
                typing_ms = int(max(delay, self._typing_duration_seconds(text)) * 1000)
                self._set_typing(room_id, text, typing_ms)
            if delay > 0:
                self._sleep(delay)
            if self._deliver_with_retry(room_id, text):
                # Bubbles with explicit delays own their rhythm; the next
                # bubble (if it also carries a delay) may send immediately.
                self._next_send_at = self._monotonic()
            return
        if self._config.typing_before_send:
            self._set_typing(room_id, text)
        if self._deliver_with_retry(room_id, text):
            self._next_send_at = self._monotonic() + self._interval_seconds()
            return
        if not self._config.merge_on_limit:
            return
        with self._send_cond:
            remaining = [item for item in self._send_queue if item[0] == room_id]
            self._send_queue = deque(item for item in self._send_queue if item[0] != room_id)
        merged = "\n".join([text] + [item[1] for item in remaining])[:MAX_MESSAGE_LENGTH]
        LOGGER.warning("send_merge room_id=%s parts=%d", room_id, len(remaining) + 1)
        if self._config.typing_before_send:
            self._set_typing(room_id, merged)
        self._deliver_once(room_id, merged)
        self._next_send_at = self._monotonic() + self._interval_seconds()

    # -- main loop ---------------------------------------------------------

    def _sender_loop(self) -> None:
        """Drain the send queue continuously: a queued reply leaves as soon
        as its pacing allows, never waiting for the next sync round."""
        while not self._sender_stop.is_set():
            with self._send_cond:
                if not self._send_queue:
                    self._send_cond.wait(timeout=0.5)
                    continue
            self.drain_sends()

    def run(self, stop: threading.Event) -> int:
        backoff = 1.0
        self._sender_stop = threading.Event()
        self._sender = threading.Thread(target=self._sender_loop, name="mx-sender", daemon=True)
        self._sender.start()
        try:
            while not stop.is_set():
                try:
                    if not self._registered and self._config.maintain_node:
                        self.register()
                    # Re-authenticate if not already logged in (token restored via login()).
                    self._client.login()
                    LOGGER.info(
                        "connected homeserver=%s user=%s rooms=%d",
                        self._config.homeserver,
                        self._config.user_id,
                        len(self._config.rooms),
                    )
                    self._since = None
                    backoff = 1.0
                    last_heartbeat = time.monotonic()
                    while not stop.is_set():
                        response = self._client.sync(
                            since=self._since, timeout_ms=self._config.sync_timeout_ms
                        )
                        stream = MatrixEventStream(response)
                        next_batch = stream.next_batch
                        if next_batch:
                            self._since = next_batch
                        for room_id, user_id in stream.typing_users():
                            self.handle_typing(room_id, user_id)
                        for event in stream.messages():
                            # Dispatch to the worker pool: the sync loop returns
                            # to long-polling immediately, so the next message
                            # (any room) is received while this one is handled.
                            self.dispatch_message(event)
                        if (
                            self._config.maintain_node
                            and time.monotonic() - last_heartbeat >= self._config.interval_seconds
                        ):
                            try:
                                self.heartbeat()
                            except TransportError as exc:
                                LOGGER.warning("heartbeat_failed reason=%s", exc.code)
                            last_heartbeat = time.monotonic()
                except (MatrixError, TransportError, OSError) as exc:
                    LOGGER.warning("connection_error reason=%s", exc)
                    if stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, 60.0)
        finally:
            self._sender_stop.set()
            with self._send_cond:
                self._send_cond.notify_all()
            if self._sender is not None:
                self._sender.join(timeout=2.0)
            self._pool.shutdown(wait=True)
        return 0
