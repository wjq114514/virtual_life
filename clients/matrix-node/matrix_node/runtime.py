"""Matrix -> Virtual Life bridge runtime.

Registers a node (a "room" is the Matrix conversation), heartbeats with a
presence context, maps room messages to dialogue turns, maps user presence to
attention samples, and sends companion replies back to Matrix rooms.
"""

import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .api import ApiClient, TransportError
from .config import MatrixConfig
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
        self._pending_send: dict[str, str] = {}
        self._lock = threading.Lock()
        if self._e2ee is not None:
            self._e2ee.on_message(self._on_sidecar_message)

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
        companion = self._companion_for(room_id)
        if companion is None:
            LOGGER.info("no_companion room_id=%s", room_id)
            return
        try:
            state = self._api.get_companion(companion)
        except TransportError as exc:
            LOGGER.warning("companion_unavailable reason=%s", exc.code)
            return
        revision = state.get("revision", 0)
        epoch = state.get("epoch", 0)
        try:
            result = self._api.dialogue(
                companion,
                {
                    "command_id": "matrix-" + str(uuid.uuid4()),
                    "node_id": self._config.node_id,
                    "epoch": epoch,
                    "expected_revision": revision,
                    "user_message": text[:MAX_MESSAGE_LENGTH],
                    "user_name": sender,
                    "max_output_tokens": self._config.max_output_tokens,
                },
            )
        except TransportError as exc:
            LOGGER.warning("dialogue_failed reason=%s", exc.code)
            return
        status = result.get("status")
        if status in ("completed", "silent") and result.get("assistant_message"):
            self._send_text(room_id, result["assistant_message"])

    def _handle_room_message(self, event: dict[str, Any]) -> None:
        self._handle_message(event)

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
        self._handle_message(normalized)

    # -- sending -----------------------------------------------------------

    def _send_text(self, room_id: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._pending_send[room_id] = text[:MAX_MESSAGE_LENGTH]

    def drain_sends(self) -> None:
        with self._lock:
            pending = dict(self._pending_send)
            self._pending_send.clear()
        for room_id, text in pending.items():
            try:
                if self._e2ee is not None:
                    self._e2ee.send_command(
                        {"cmd": "send", "room_id": room_id, "body": text}
                    )
                else:
                    self._client.send_text(room_id, text)
            except (MatrixError, OSError) as exc:
                LOGGER.warning("send_failed room_id=%s reason=%s", room_id, exc)
                continue

    # -- main loop ---------------------------------------------------------

    def run(self, stop: threading.Event) -> int:
        backoff = 1.0
        while not stop.is_set():
            try:
                if not self._registered:
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
                    for event in stream.messages():
                        self._handle_room_message(event)
                    self.drain_sends()
                    if time.monotonic() - last_heartbeat >= self._config.interval_seconds:
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
        return 0
