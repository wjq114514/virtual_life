"""Bounded authenticated API writes and unauthenticated endpoint probes."""

import errno
import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .json_utils import (
    MAX_JSON_DEPTH,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    JsonError,
    decode_json,
    encode_json,
)


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
    # No ambient proxy configuration and no redirects to unconfigured endpoints.
    return build_opener(ProxyHandler({}), NoRedirect())


def node_path(node_id: str, action: str) -> str:
    return f"/v1/nodes/{quote(node_id, safe='')}/{action}"


class ApiClient:
    def __init__(self, server_url: str, token: str, timeout_seconds: float):
        self._server_url = server_url
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._opener = endpoint_opener()

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            body = encode_json(payload)
        except JsonError as exc:
            raise TransportError("api_request_invalid") from exc
        return self.post_encoded(path, body)

    def post_encoded(self, path: str, body: bytes) -> dict[str, Any]:
        if len(body) > MAX_REQUEST_BYTES:
            raise TransportError("api_request_invalid")
        request = Request(
            self._server_url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "virtual-life-system-node/1.0",
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
            result = decode_json(raw, max_bytes=MAX_RESPONSE_BYTES, max_depth=MAX_JSON_DEPTH)
        except JsonError as exc:
            raise TransportError("api_response_invalid") from exc
        if not isinstance(result, dict):
            raise TransportError("api_response_invalid")
        return result


@dataclass(frozen=True)
class ProbeResult:
    status: str
    quality: str
    errors: tuple[str, ...]
    latency_ms: float | None = None
    http_status: int | None = None


def _probe_error(adapter: str, exc: BaseException) -> ProbeResult:
    reason = exc.reason if isinstance(exc, URLError) else exc
    status = "unreachable"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        code = "timeout"
    elif isinstance(reason, socket.gaierror):
        code = "dns_error"
    elif isinstance(reason, ssl.SSLError):
        code = "tls_error"
        status = "unknown"
    elif isinstance(reason, OSError):
        if reason.errno in {errno.EACCES, errno.EPERM}:
            code = "permission_denied"
            status = "unknown"
        elif reason.errno == errno.ECONNREFUSED:
            code = "refused"
        elif reason.errno in {
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ECONNRESET,
            errno.EPIPE,
            errno.ENETDOWN,
            errno.ENOTCONN,
            errno.ETIMEDOUT,
        }:
            code = "network_error"
        else:
            code = "unavailable"
            status = "unknown"
    else:
        code = (
            "response_invalid"
            if isinstance(reason, http.client.HTTPException)
            else "invalid_target"
        )
        status = "unknown"
    return ProbeResult(
        status=status,
        quality="partial" if status == "unreachable" else "unknown",
        errors=(f"{adapter}_{code}",),
    )


def probe_tcp(host: str, port: int, timeout_seconds: float) -> ProbeResult:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            pass
    except (OSError, ValueError, OverflowError) as exc:
        return _probe_error("tcp", exc)
    return ProbeResult(
        status="reachable",
        quality="ok",
        errors=(),
        latency_ms=round((time.monotonic() - started) * 1000, 3),
    )


def probe_http(url: str, timeout_seconds: float) -> ProbeResult:
    started = time.monotonic()
    # Deliberately construct a separate request with no server token or cookies.
    request = Request(url, method="GET", headers={"User-Agent": "virtual-life-system-node/1.0"})
    try:
        with endpoint_opener().open(request, timeout=timeout_seconds) as response:
            status = response.status
            # The body is neither read nor retained; any HTTP status proves reachability.
    except HTTPError as exc:
        status = exc.code
        exc.close()
    except (URLError, OSError, ValueError, http.client.HTTPException) as exc:
        return _probe_error("http", exc)
    return ProbeResult(
        status="reachable",
        quality="ok",
        errors=(),
        http_status=status,
        latency_ms=round((time.monotonic() - started) * 1000, 3),
    )
