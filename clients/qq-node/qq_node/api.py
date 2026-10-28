"""Bounded authenticated API writes for the QQ node (stdlib-only, mirrors system-node)."""

import http.client
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .json_utils import MAX_RESPONSE_BYTES, JsonError, decode_json, encode_json


class TransportError(Exception):
    def __init__(self, code: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint_opener():
    return build_opener(ProxyHandler({}), NoRedirect())


def node_path(node_id: str, action: str) -> str:
    return f"/v1/nodes/{quote(node_id, safe='')}/{action}"


class ApiClient:
    def __init__(self, server_url: str, token: str, timeout_seconds: float):
        self._server_url = server_url
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._opener = endpoint_opener()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = encode_json(payload)
        request = Request(
            self._server_url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "virtual-life-qq-node/1.0",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise TransportError("api_auth_rejected", status=status) from exc
            if 300 <= status < 400:
                raise TransportError("api_redirect_rejected", status=status) from exc
            raise TransportError(
                "api_http_error", retryable=status >= 500 or status == 429, status=status
            ) from exc
        except (URLError, OSError, http.client.HTTPException) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            code = (
                "api_timeout"
                if isinstance(reason, (TimeoutError, socket.timeout))
                else "api_network_error"
            )
            raise TransportError(code, retryable=True) from exc
        except (ValueError, UnicodeError) as exc:
            raise TransportError("api_response_invalid") from exc
        try:
            result = decode_json(raw, max_bytes=MAX_RESPONSE_BYTES, max_depth=32)
        except JsonError as exc:
            raise TransportError("api_response_invalid") from exc
        if not isinstance(result, dict):
            raise TransportError("api_response_invalid")
        return result

    def register(self, node_id: str, label: str, capabilities: list[str]) -> None:
        self._post(
            "/v1/nodes",
            {"node_id": node_id, "label": label, "kind": "other", "capabilities": capabilities},
        )

    def heartbeat(self, node_id: str, context: dict[str, Any]) -> None:
        self._post(node_path(node_id, "heartbeat"), {"context": context})

    def observe(self, node_id: str, payload: dict[str, Any]) -> None:
        self._post(node_path(node_id, "observations"), payload)

    def attention(self, node_id: str, payload: dict[str, Any]) -> None:
        self._post(node_path(node_id, "attention"), payload)

    def get_companion(self, companion_id: str) -> dict[str, Any]:
        body = encode_json({})
        request = Request(
            self._server_url + f"/v1/companions/{quote(companion_id, safe='')}",
            data=body,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "virtual-life-qq-node/1.0",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise TransportError("api_http_error", retryable=status >= 500, status=status) from exc
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise TransportError("api_network_error", retryable=True) from exc
        result = decode_json(raw, max_bytes=MAX_RESPONSE_BYTES, max_depth=32)
        return result

    def dialogue(self, companion_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/v1/companions/{quote(companion_id, safe='')}/dialogue", payload)

    def follow_room(self, companion_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/v1/companions/{quote(companion_id, safe='')}/follow", payload)
