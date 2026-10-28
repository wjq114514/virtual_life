"""QQ -> Virtual Life bridge runtime.

Registers a node (a "room" that is the QQ conversation), heartbeats with a
presence context, maps private messages to dialogue turns, maps typing/presence
events to attention samples, and sends companion replies back to QQ through
NapCat's HTTP action server.
"""

import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .api import ApiClient, TransportError
from .config import QQConfig
from .onebot import HttpActionError, NapCatActionClient, OneBotServer, WebSocketError

LOGGER = logging.getLogger(__name__)

CAPABILITIES = ["text.display", "attention.observe", "qq.message"]
MAX_MESSAGE_LENGTH = 4096


def utc_timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(UTC)


class QQRuntime:
    def __init__(self, config: QQConfig, api: ApiClient):
        self._config = config
        self._api = api
        self._sequence = 0
        self._session_id = str(uuid.uuid4())
        self._registered = False
        self._last_typing_at: dict[int, float] = {}
        self._pending_send: dict[int, str] = {}
        self._lock = threading.Lock()
        self._server: OneBotServer | None = None
        self._actions: NapCatActionClient | None = None

    # -- lifecycle ---------------------------------------------------------

    def register(self) -> None:
        self._api.register(self._config.node_id, self._config.label, CAPABILITIES)
        self._registered = True
        LOGGER.info("registered node=%s capabilities=%s", self._config.node_id, CAPABILITIES)

    def heartbeat(self) -> None:
        context = {"presence": {"source": "qq", "status": "online"}}
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

    def _companion_for(self, user_id: int) -> str | None:
        if not self._config.companions:
            return None
        return self._config.companions[0]

    # -- OneBot event handling --------------------------------------------

    def _extract_text(self, message: Any) -> str:
        """OneBot v11 message segments -> plain text."""
        if isinstance(message, str):
            return message.strip()
        if not isinstance(message, list):
            return ""
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") == "text":
                data = segment.get("data", {})
                if isinstance(data, dict) and isinstance(data.get("text"), str):
                    parts.append(data["text"])
            elif segment.get("type") == "image":
                parts.append("[图片]")
            elif segment.get("type") == "at":
                data = segment.get("data", {})
                if isinstance(data, dict) and data.get("qq") == "all":
                    parts.append("@全体成员")
                elif isinstance(data, dict) and isinstance(data.get("qq"), (str, int)):
                    parts.append(f"@{data['qq']}")
        return "".join(parts).strip()

    def _handle_message(self, event: dict[str, Any]) -> None:
        message_type = event.get("message_type")
        if message_type != "private":
            return
        user_id = event.get("user_id")
        if not isinstance(user_id, int):
            return
        text = self._extract_text(event.get("message"))
        if not text:
            return
        companion = self._companion_for(user_id)
        if companion is None:
            LOGGER.info("no_companion user_id=%s", user_id)
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
                    "command_id": "qq-" + str(uuid.uuid4()),
                    "node_id": self._config.node_id,
                    "epoch": epoch,
                    "expected_revision": revision,
                    "user_message": text[:MAX_MESSAGE_LENGTH],
                    "user_name": str(event.get("nickname", user_id)),
                    "max_output_tokens": self._config.max_output_tokens,
                },
            )
        except TransportError as exc:
            LOGGER.warning("dialogue_failed reason=%s", exc.code)
            return
        status = result.get("status")
        if status in ("completed", "silent") and result.get("assistant_message"):
            self._send_private(user_id, result["assistant_message"])

    def _handle_notice(self, event: dict[str, Any]) -> None:
        notice_type = event.get("notice_type")
        user_id = event.get("user_id")
        if notice_type == "typing" and isinstance(user_id, int):
            self._last_typing_at[user_id] = time.monotonic()
            self.report_presence(user_present=True, typing=True)
        elif notice_type == "offline":
            self.report_presence(user_present=False)

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("post_type")
        if event_type == "message":
            self._handle_message(event)
        elif event_type == "notice":
            self._handle_notice(event)
        elif event_type == "meta_event":
            self.report_presence(user_present=True)

    # -- sending -----------------------------------------------------------

    def _send_private(self, user_id: int, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._pending_send[user_id] = text[:MAX_MESSAGE_LENGTH]

    def drain_sends(self) -> None:
        with self._lock:
            pending = dict(self._pending_send)
            self._pending_send.clear()
        if self._actions is None:
            return
        for user_id, text in pending.items():
            try:
                result = self._actions.send_private(user_id, text)
            except HttpActionError as exc:
                LOGGER.warning("send_failed user_id=%s reason=%s", user_id, exc.code)
                continue
            status = result.get("status")
            if status not in (None, "ok", "async"):
                LOGGER.warning("send_failed user_id=%s status=%s", user_id, status)

    # -- main loop ---------------------------------------------------------

    def run(self, stop: threading.Event) -> int:
        backoff = 1.0
        while not stop.is_set():
            try:
                self._server = OneBotServer(
                    self._config.websocket_url, self._config.access_token, self.handle_event
                )
                self._server.start()
                self._actions = NapCatActionClient(
                    self._config.http_url,
                    self._config.access_token,
                    self._config.request_timeout_seconds,
                )
                backoff = 1.0
                LOGGER.info(
                    "listening ws=%s http=%s",
                    self._config.websocket_url,
                    self._config.http_url,
                )
                if not self._registered:
                    self.register()
                last_heartbeat = time.monotonic()
                while not stop.is_set():
                    self.drain_sends()
                    if time.monotonic() - last_heartbeat >= self._config.interval_seconds:
                        try:
                            self.heartbeat()
                        except TransportError as exc:
                            LOGGER.warning("heartbeat_failed reason=%s", exc.code)
                        last_heartbeat = time.monotonic()
                    stop.wait(0.5)
            except (WebSocketError, HttpActionError, TransportError, OSError) as exc:
                LOGGER.warning("connection_error reason=%s", exc)
                if self._server is not None:
                    self._server.close()
                    self._server = None
                if stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60.0)
        if self._server is not None:
            self._server.close()
        return 0
