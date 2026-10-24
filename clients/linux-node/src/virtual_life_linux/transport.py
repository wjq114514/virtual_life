"""Bearer-authenticated HTTP transport for the Virtual Life runtime API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
STREAM_TIMEOUT = httpx.Timeout(None, connect=10.0)


class ServerError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


class AuthError(ServerError):
    pass


class ConflictError(ServerError):
    pass


class OfflineError(ServerError):
    pass


class FeatureUnsupportedError(ServerError):
    pass


def _error_from_response(response: httpx.Response) -> ServerError:
    code = f"http_{response.status_code}"
    message = response.text[:300]
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = str(error.get("code", code))
            message = str(error.get("message", message))
        elif isinstance(body.get("detail"), str):
            message = body["detail"]
    if response.status_code == 401:
        return AuthError(code, message, response.status_code)
    if response.status_code == 409:
        return ConflictError(code, message, response.status_code)
    return ServerError(code, message, response.status_code)


async def iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield (event_name, parsed_json) pairs from a text/event-stream body."""
    event = "message"
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                yield event, payload
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        yield event, payload


class ServerTransport:
    """Thin async wrapper; one instance lives in the daemon."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=DEFAULT_TIMEOUT,
            )
        self._client = client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "ServerTransport":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise OfflineError("offline", str(exc)) from exc
        if response.status_code >= 400:
            raise _error_from_response(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- runtime ----------------------------------------------------------
    async def capabilities(self) -> dict:
        return await self._request("GET", "/v1/capabilities")

    async def require_native_presentation(self) -> None:
        caps = await self.capabilities()
        block = caps.get("native_presentation") or {}
        if not block.get("supported"):
            raise FeatureUnsupportedError(
                "native_presentation_unsupported",
                "This server does not offer native terminal presentations",
            )

    # -- nodes ------------------------------------------------------------
    async def register_node(
        self,
        *,
        node_id: str,
        label: str,
        capabilities: list[str],
        device_id: str,
        kind: str = "desktop",
    ) -> dict:
        return await self._request(
            "POST",
            "/v1/nodes",
            json={
                "node_id": node_id,
                "label": label,
                "kind": kind,
                "capabilities": capabilities,
                "device": {"device_id": device_id},
            },
        )

    async def get_node(self, node_id: str) -> dict:
        return await self._request("GET", f"/v1/nodes/{node_id}")

    async def heartbeat(self, node_id: str, context: dict | None = None) -> dict:
        return await self._request(
            "POST", f"/v1/nodes/{node_id}/heartbeat", json={"context": context or {}}
        )

    async def update_context(self, payload: dict) -> dict:
        return await self._request("POST", "/v1/context", json=payload)

    async def attention(self, node_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/nodes/{node_id}/attention", json=payload)

    async def observe(self, node_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/nodes/{node_id}/observations", json=payload)

    # -- screen vision (owner/admin token only) ---------------------------
    async def screen_create_session(self, node_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/nodes/{node_id}/screen/sessions", json=payload)

    async def screen_get_session(self, node_id: str, session_id: str) -> dict:
        return await self._request("GET", f"/v1/nodes/{node_id}/screen/sessions/{session_id}")

    async def screen_stop_session(self, node_id: str, session_id: str, command_id: str) -> dict:
        return await self._request(
            "POST",
            f"/v1/nodes/{node_id}/screen/sessions/{session_id}/stop",
            json={"command_id": command_id},
        )

    async def screen_register_upload(self, node_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/nodes/{node_id}/screen/uploads", json=payload)

    async def screen_upload_content(
        self, node_id: str, upload_id: str, data: bytes, media_type: str
    ) -> dict:
        return await self._request(
            "PUT",
            f"/v1/nodes/{node_id}/screen/uploads/{upload_id}/content",
            content=data,
            headers={"content-type": media_type},
        )

    async def screen_submit_frame(self, node_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/nodes/{node_id}/screen/frames", json=payload)

    async def screen_get_frame(self, node_id: str, frame_id: str) -> dict:
        return await self._request("GET", f"/v1/nodes/{node_id}/screen/frames/{frame_id}")

    async def screen_get_analysis(self, node_id: str, analysis_id: str) -> dict:
        return await self._request("GET", f"/v1/nodes/{node_id}/screen/analyses/{analysis_id}")

    async def screen_state(
        self, node_id: str, companion_id: str, display_id: str = "default"
    ) -> dict:
        return await self._request(
            "GET",
            f"/v1/nodes/{node_id}/screen/state",
            params={"companion_id": companion_id, "display_id": display_id},
        )

    # -- presentations ----------------------------------------------------
    async def open_presentation(self, node_id: str, presentation_id: str) -> dict:
        return await self._request(
            "POST",
            f"/v1/nodes/{node_id}/presentations",
            json={"presentation_id": presentation_id, "kind": "terminal"},
        )

    async def heartbeat_presentation(self, node_id: str, presentation_id: str) -> dict:
        return await self._request(
            "POST", f"/v1/nodes/{node_id}/presentations/{presentation_id}/heartbeat"
        )

    async def release_presentation(self, node_id: str, presentation_id: str) -> dict:
        return await self._request("DELETE", f"/v1/nodes/{node_id}/presentations/{presentation_id}")

    # -- companions / lease ----------------------------------------------
    async def list_companions(self) -> list[dict]:
        return await self._request("GET", "/v1/companions")

    async def get_companion(self, companion_id: str) -> dict:
        return await self._request("GET", f"/v1/companions/{companion_id}")

    async def renew_lease(self, companion_id: str, node_id: str, epoch: int) -> dict:
        return await self._request(
            "POST",
            f"/v1/companions/{companion_id}/lease",
            json={"node_id": node_id, "epoch": epoch},
        )

    # -- dialogue ---------------------------------------------------------
    async def send_dialogue(self, companion_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/companions/{companion_id}/dialogue", json=payload)

    async def dialogue_result(self, companion_id: str, command_id: str) -> dict:
        return await self._request("GET", f"/v1/companions/{companion_id}/dialogue/{command_id}")

    async def dialogue_history(
        self, companion_id: str, *, before_sequence: int | None = None, limit: int = 20
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if before_sequence is not None:
            params["before_sequence"] = before_sequence
        return await self._request(
            "GET", f"/v1/companions/{companion_id}/dialogue/history", params=params
        )

    async def stream_dialogue(
        self, companion_id: str, payload: dict
    ) -> AsyncIterator[tuple[str, dict]]:
        try:
            async with self._client.stream(
                "POST",
                f"/v1/companions/{companion_id}/dialogue/stream",
                json=payload,
                timeout=STREAM_TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error_from_response(response)
                async for event in iter_sse(response):
                    yield event
        except httpx.TransportError as exc:
            raise OfflineError("offline", str(exc)) from exc

    # -- voice ------------------------------------------------------------
    @staticmethod
    def _voice_base(node_id: str) -> str:
        return f"/v1/nodes/{quote(node_id, safe='')}/voice"

    async def voice_create_session(self, node_id: str, payload: dict) -> dict:
        return await self._request("POST", f"{self._voice_base(node_id)}/sessions", json=payload)

    async def voice_get_session(self, node_id: str, session_id: str) -> dict:
        path = f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}"
        return await self._request("GET", path)

    async def voice_session_heartbeat(self, node_id: str, session_id: str, command_id: str) -> dict:
        path = f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}/heartbeat"
        return await self._request("POST", path, json={"command_id": command_id})

    async def voice_stop_session(self, node_id: str, session_id: str, command_id: str) -> dict:
        path = f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}/stop"
        return await self._request("POST", path, json={"command_id": command_id})

    async def voice_submit_turn(self, node_id: str, session_id: str, payload: dict) -> dict:
        path = f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}/turns"
        return await self._request("POST", path, json=payload)

    async def voice_get_turn(self, node_id: str, session_id: str, turn_id: str) -> dict:
        path = (
            f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}"
            f"/turns/{quote(turn_id, safe='')}"
        )
        return await self._request("GET", path)

    async def stream_voice_turn_events(
        self,
        node_id: str,
        session_id: str,
        turn_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        path = (
            f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}"
            f"/turns/{quote(turn_id, safe='')}/events"
        )
        try:
            async with self._client.stream(
                "GET",
                path,
                params={"after_sequence": after_sequence},
                timeout=STREAM_TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error_from_response(response)
                async for event in iter_sse(response):
                    yield event
        except httpx.TransportError as exc:
            raise OfflineError("offline", str(exc)) from exc

    async def voice_fetch_audio(
        self,
        node_id: str,
        session_id: str,
        turn_id: str,
        sequence: int,
        audio_path: str,
        *,
        max_bytes: int,
    ) -> tuple[bytes, dict[str, str]]:
        if sequence < 0 or max_bytes <= 0:
            raise ValueError("invalid audio segment bounds")
        expected_path = (
            f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}"
            f"/turns/{quote(turn_id, safe='')}/segments/{sequence}/audio"
        )
        parsed = urlsplit(audio_path)
        if parsed.query or parsed.fragment:
            raise ValueError("voice audio path must not contain a query or fragment")
        if parsed.scheme or parsed.netloc:
            origin = urlsplit(self.base_url)
            if (
                parsed.scheme.lower() != origin.scheme.lower()
                or parsed.netloc.lower() != origin.netloc.lower()
            ):
                raise ValueError("voice audio URL is not same-origin")
        if parsed.path != expected_path:
            raise ValueError("voice audio path does not match its session, turn, and sequence")
        try:
            async with self._client.stream(
                "GET", expected_path, timeout=DEFAULT_TIMEOUT
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error_from_response(response)
                raw_length = response.headers.get("content-length")
                if raw_length is None or not raw_length.isdecimal():
                    raise ServerError("voice_audio_invalid", "Audio length is missing")
                if int(raw_length) > max_bytes:
                    raise ServerError("voice_audio_too_large", "Audio exceeds the client bound")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(8192):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ServerError("voice_audio_too_large", "Audio exceeds the client bound")
                    chunks.append(chunk)
                if size != int(raw_length):
                    raise ServerError(
                        "voice_audio_invalid", "Audio length does not match its header"
                    )
                return b"".join(chunks), {
                    key.lower(): value for key, value in response.headers.items()
                }
        except httpx.TransportError as exc:
            raise OfflineError("offline", str(exc)) from exc

    async def voice_interrupt(self, node_id: str, session_id: str, payload: dict) -> dict:
        path = f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}/interrupt"
        return await self._request("POST", path, json=payload)

    async def voice_playback(self, node_id: str, session_id: str, payload: dict) -> dict:
        path = f"{self._voice_base(node_id)}/sessions/{quote(session_id, safe='')}/playback"
        return await self._request("POST", path, json=payload)

    # -- proactivity ------------------------------------------------------
    async def native_output(
        self,
        companion_id: str,
        *,
        node_id: str,
        presentation_id: str,
        epoch: int,
        after_sequence: int,
    ) -> dict:
        return await self._request(
            "GET",
            f"/v1/companions/{companion_id}/proactivity/native-output",
            params={
                "node_id": node_id,
                "presentation_id": presentation_id,
                "epoch": epoch,
                "after_sequence": after_sequence,
            },
        )

    # -- travel -----------------------------------------------------------
    async def prepare_transfer(self, companion_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/companions/{companion_id}/transfers", json=payload)

    async def get_transfer(self, transfer_id: str) -> dict:
        return await self._request("GET", f"/v1/transfers/{transfer_id}")

    async def ack_transfer(self, transfer_id: str, payload: dict) -> dict:
        return await self._request("POST", f"/v1/transfers/{transfer_id}/ack", json=payload)

    async def abort_transfer(self, transfer_id: str) -> dict:
        return await self._request("POST", f"/v1/transfers/{transfer_id}/abort")

    # -- events -----------------------------------------------------------
    async def events(
        self, after_sequence: int = 0, *, node_id: str | None = None, limit: int = 100
    ) -> dict:
        params: dict[str, Any] = {"after_sequence": after_sequence, "limit": limit}
        if node_id is not None:
            params["node_id"] = node_id
        return await self._request("GET", "/v1/events", params=params)
