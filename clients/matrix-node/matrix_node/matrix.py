"""Matrix client (stdlib-only): login, sync polling, message extraction, sending.

Implements the subset of the Matrix client-server protocol needed to bridge a
Matrix account into Virtual Life. Modeled on the AstrBot matrix adapter but
reduced to stdlib-only primitives: no E2EE, SSO, OAuth2, QR, media, or
threading support.
"""

import html
import re
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .json_utils import MAX_RESPONSE_BYTES, JsonError, decode_json, encode_json


class MatrixError(Exception):
    """Raised on Matrix protocol errors. Code is a stable reason."""

    def __init__(self, code: str, *, status: int | None = None, errcode: str | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.errcode = errcode


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint_opener():
    return build_opener(ProxyHandler({}), NoRedirect())


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(value: str) -> str:
    """Strip HTML tags from a Matrix formatted_body and collapse whitespace."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def extract_text(event: dict[str, Any]) -> str:
    """Extract plain text from an m.room.message event (m.text msgtype)."""
    if not isinstance(event, dict):
        return ""
    if event.get("type") != "m.room.message":
        return ""
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    if content.get("msgtype") != "m.text":
        return ""
    formatted = content.get("formatted_body")
    if isinstance(formatted, str) and formatted:
        return strip_html(formatted).strip()
    body = content.get("body")
    if isinstance(body, str):
        return body.strip()
    return ""


class MatrixClient:
    """A minimal Matrix client: password/token login, sync, send, join."""

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        *,
        auth_method: str = "password",
        password: str | None = None,
        access_token: str | None = None,
        device_id: str | None = None,
        device_name: str = "virtual-life-matrix-node",
        timeout_seconds: float = 15.0,
        opener: Any | None = None,
    ):
        self._homeserver = homeserver.rstrip("/")
        self._user_id = user_id
        self._auth_method = auth_method
        self._password = password
        self._access_token = access_token
        self._device_id = device_id
        self._device_name = device_name
        self._timeout_seconds = timeout_seconds
        self._txn_counter = 0
        self._opener = opener if opener is not None else endpoint_opener()

    # -- auth --------------------------------------------------------------

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def device_id(self) -> str | None:
        return self._device_id

    def login(self) -> dict[str, Any]:
        """Authenticate and return {user_id, access_token, device_id}."""
        if self._auth_method == "token":
            if not self._access_token:
                raise MatrixError("auth_token_required")
            self._whoami()
            return {
                "user_id": self._user_id,
                "access_token": self._access_token,
                "device_id": self._device_id,
            }
        if self._auth_method != "password":
            raise MatrixError("auth_method_unsupported")
        if not self._password:
            raise MatrixError("auth_password_required")
        payload: dict[str, Any] = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": self._user_id},
            "password": self._password,
            "initial_device_display_name": self._device_name,
        }
        result = self._request("POST", "/_matrix/client/v3/login", payload=payload, auth=False)
        user_id = result.get("user_id")
        access_token = result.get("access_token")
        device_id = result.get("device_id")
        if not isinstance(user_id, str) or not isinstance(access_token, str):
            raise MatrixError("auth_login_invalid")
        self._user_id = user_id
        self._access_token = access_token
        if isinstance(device_id, str):
            self._device_id = device_id
        return {"user_id": user_id, "access_token": access_token, "device_id": self._device_id}

    def _whoami(self) -> dict[str, Any]:
        result = self._request("GET", "/_matrix/client/v3/account/whoami", auth=True)
        user_id = result.get("user_id")
        if isinstance(user_id, str):
            self._user_id = user_id
        return result

    def validate_token(self) -> dict[str, Any]:
        """Validate the stored token via whoami (raises MatrixError on failure)."""
        return self._whoami()

    # -- sync --------------------------------------------------------------

    def sync(self, *, since: str | None = None, timeout_ms: int = 30000) -> dict[str, Any]:
        """Long-poll sync. Returns the raw response body."""
        query = {"timeout": str(timeout_ms)}
        if since:
            query["since"] = since
        encoded = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in query.items())
        return self._request("GET", f"/_matrix/client/v3/sync?{encoded}", auth=True)

    # -- send --------------------------------------------------------------

    def _next_txn_id(self) -> str:
        self._txn_counter += 1
        if self._txn_counter > 2**31:
            self._txn_counter = 1
        return f"vl-{int(time.time())}-{self._txn_counter}-{uuid.uuid4().hex[:8]}"

    def send_text(self, room_id: str, text: str) -> str:
        """Send an m.text message. Returns the event_id."""
        txn_id = self._next_txn_id()
        path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{txn_id}"
        result = self._request(
            "PUT", path, payload={"msgtype": "m.text", "body": text}, auth=True
        )
        event_id = result.get("event_id")
        if not isinstance(event_id, str):
            raise MatrixError("send_event_id_invalid")
        return event_id

    def join(self, room_id_or_alias: str) -> dict[str, Any]:
        """Join a room (also accepts an invite). Returns {room_id, ...}."""
        return self._request(
            "POST", f"/_matrix/client/v3/join/{quote(room_id_or_alias, safe='')}", auth=True
        )

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "virtual-life-matrix-node/1.0"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = encode_json(payload)
        if auth:
            if not self._access_token:
                raise MatrixError("auth_not_logged_in")
            headers["Authorization"] = f"Bearer {self._access_token}"
        request = Request(self._homeserver + path, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise MatrixError(
                "matrix_http_error",
                status=status,
                errcode=self._extract_errcode(exc),
            ) from exc
        except (URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            code = (
                "matrix_timeout"
                if isinstance(reason, TimeoutError)
                else "matrix_network_error"
            )
            raise MatrixError(code) from exc
        try:
            result = decode_json(raw, max_bytes=MAX_RESPONSE_BYTES, max_depth=32)
        except JsonError as exc:
            raise MatrixError("matrix_response_invalid") from exc
        if not isinstance(result, dict):
            raise MatrixError("matrix_response_invalid")
        errcode = result.get("errcode")
        if isinstance(errcode, str) and errcode.startswith("M_"):
            raise MatrixError("matrix_api_error", errcode=errcode)
        return result

    @staticmethod
    def _extract_errcode(exc: HTTPError) -> str | None:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            return None
        try:
            result = decode_json(raw, max_bytes=MAX_RESPONSE_BYTES, max_depth=32)
        except JsonError:
            return None
        if isinstance(result, dict) and isinstance(result.get("errcode"), str):
            return result["errcode"]
        return None


class MatrixEventStream:
    """Iterates joined-room m.room.message events from a sync response."""

    def __init__(self, response: dict[str, Any]):
        self._response = response

    @property
    def next_batch(self) -> str | None:
        token = self._response.get("next_batch")
        return token if isinstance(token, str) else None

    def messages(self) -> list[dict[str, Any]]:
        """Return all m.room.message events from joined rooms (room_id attached)."""
        events: list[dict[str, Any]] = []
        rooms = self._response.get("rooms", {})
        if not isinstance(rooms, dict):
            return events
        joined = rooms.get("join", {})
        if not isinstance(joined, dict):
            return events
        for room_id, state in joined.items():
            if not isinstance(state, dict):
                continue
            timeline = state.get("timeline", {})
            if not isinstance(timeline, dict):
                continue
            raw_events = timeline.get("events", [])
            if not isinstance(raw_events, list):
                continue
            for event in raw_events:
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "m.room.message":
                    continue
                content = event.get("content")
                if not isinstance(content, dict):
                    continue
                if content.get("msgtype") != "m.text":
                    continue
                item = dict(event)
                item["room_id"] = room_id
                events.append(item)
        return events
