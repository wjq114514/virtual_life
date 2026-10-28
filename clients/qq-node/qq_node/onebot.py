"""NapCat / OneBot v11 reverse WebSocket *server* for the QQ node.

NapCat is deployed as a OneBot v11 compatible stack with two channels:

- **Reverse WebSocket client** (NapCat's ``websocketClients[].url``) dials INTO
  this process. NapCat pushes events (``post_type``: message / notice /
  meta_event) over that socket.
- **HTTP server** (NapCat's ``httpServers``) receives action calls. We send
  companion replies with ``POST /send_private_msg`` and
  ``Authorization: Bearer <token>``.

This module is stdlib-only: the WS listener is built on ``http.server`` +
``socket`` with a raw frame codec, and the HTTP action client uses
``http.client``. It never depends on a third-party library.
"""

import base64
import hashlib
import http.client
import os
import socket
import ssl
import struct
import threading
from socketserver import BaseRequestHandler, ThreadingTCPServer
from typing import Any
from urllib.parse import urlsplit

from .json_utils import JsonError, decode_json, encode_json

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 2 * 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class HttpActionError(Exception):
    def __init__(self, code: str, *, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


def _frame_encode(opcode: int, payload: bytes) -> bytes:
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, length])
    elif length < 65536:
        header = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)
    if opcode == 0x8:  # close frame must not have a payload body
        return header
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return header + mask + masked


class _WsConnection:
    """A single accepted WebSocket connection (NapCat -> us)."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._recv_buffer = b""
        self._send_lock = threading.Lock()
        self._closed = threading.Event()

    @property
    def closed(self) -> threading.Event:
        return self._closed

    def _read_exact(self, n: int) -> bytes:
        while len(self._recv_buffer) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection_closed")
            self._recv_buffer += chunk
        result = self._recv_buffer[:n]
        self._recv_buffer = self._recv_buffer[n:]
        return result

    def _read_frame(self) -> tuple[int, bytes]:
        header = self._read_exact(2)
        opcode = header[0] & 0x0F
        masked = header[1] & 0x80
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if length > MAX_FRAME_BYTES:
            raise WebSocketError("frame_too_large")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload

    def send_text(self, text: str) -> None:
        with self._send_lock:
            self._sock.sendall(_frame_encode(0x1, text.encode("utf-8")))

    def recv(self, timeout: float | None = None) -> str | None:
        old_timeout = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            while True:
                opcode, payload = self._read_frame()
                if opcode == 0x8:  # close
                    raise WebSocketError("remote_closed")
                if opcode == 0x9:  # ping -> pong
                    self._send_pong(payload)
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode == 0x1:
                    try:
                        return payload.decode("utf-8")
                    except UnicodeError as exc:
                        raise WebSocketError("frame_invalid_utf8") from exc
                # Ignore continuation and binary frames for this protocol.
        except socket.timeout:
            return None
        finally:
            if timeout is not None:
                self._sock.settimeout(old_timeout)

    def _send_pong(self, payload: bytes) -> None:
        with self._send_lock:
            self._sock.sendall(_frame_encode(0xA, payload))

    def close(self) -> None:
        self._closed.set()
        try:
            self._sock.close()
        except OSError:
            pass


def _handle_upgrade(
    sock: socket.socket,
    token: str | None,
) -> tuple[socket.socket, bytes]:
    """Perform the server side of the WebSocket handshake.

    Returns the wrapped socket plus any leftover bytes. Raises WebSocketError
    on a rejected handshake.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise WebSocketError("handshake_closed")
        data += chunk
        if len(data) > 65536:
            raise WebSocketError("handshake_too_large")
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    request_line = lines[0].split(" ")
    if len(request_line) < 2 or request_line[0] != "GET":
        raise WebSocketError("method_not_get")
    key = None
    client_token = None
    upgrade = False
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == "sec-websocket-key":
            key = value
        elif name == "upgrade" and value.lower() == "websocket":
            upgrade = True
        elif name == "authorization" and value.lower().startswith("bearer "):
            client_token = value[7:].strip()
    if not upgrade or key is None:
        raise WebSocketError("handshake_invalid")
    if token is not None and client_token != token:
        raise WebSocketError("token_verify_failed")
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode(
        "ascii"
    )
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    sock.sendall(response.encode("ascii"))
    return sock, rest


class _WsHandler(BaseRequestHandler):
    def handle(self):
        server = self.server  # type: ignore[attr-defined]
        sock = self.request
        try:
            if server.tls_context is not None:
                sock = server.tls_context.wrap_socket(sock, server_side=True)
            sock, _ = _handle_upgrade(sock, server.token)
        except (WebSocketError, OSError, ssl.SSLError) as exc:
            code = exc.code if isinstance(exc, WebSocketError) else "handshake_failed"
            status = 401 if code == "token_verify_failed" else 400
            try:
                sock.sendall(
                    f"HTTP/1.1 {status} X\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode(
                        "ascii"
                    )
                )
            except OSError:
                pass
            return
        connection = _WsConnection(sock)
        server.connections.append(connection)
        server.on_connection(connection)
        # Keep the handler thread alive for the connection lifecycle; the
        # reader thread owns all socket reads and sets the closed event.
        connection.closed.wait()
        if connection in server.connections:
            server.connections.remove(connection)


def _tls_context_for(scheme: str) -> ssl.SSLContext | None:
    if scheme == "wss":
        return ssl.create_default_context()
    return None


class OneBotServer:
    """Reverse WebSocket server that NapCat connects to."""

    def __init__(self, ws_url: str, token: str | None, on_event):
        parts = urlsplit(ws_url)
        if parts.scheme not in {"ws", "wss"} or not parts.netloc:
            raise WebSocketError("websocket_url_invalid")
        self._scheme = parts.scheme
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if parts.scheme == "wss" else 80)
        self._token = token
        self._on_event = on_event
        self._connections: list[_WsConnection] = []
        self._server: ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        connections = self._connections
        token = self._token
        tls_context = _tls_context_for(self._scheme)
        on_connection = self._on_connection

        class _Server(ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def __init__(self, *args, **kwargs):
                self.token = token
                self.tls_context = tls_context
                self.connections = connections
                self.on_connection = on_connection
                super().__init__(*args, **kwargs)

        self._server = _Server((self._host, self._port), _WsHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _on_connection(self, connection: _WsConnection) -> None:
        threading.Thread(target=self._reader_loop, args=(connection,), daemon=True).start()

    def _reader_loop(self, connection: _WsConnection) -> None:
        try:
            while not self._stop.is_set():
                raw = connection.recv(timeout=1.0)
                if raw is None:
                    continue
                try:
                    event = decode_json(
                        raw.encode("utf-8"), max_bytes=MAX_EVENT_BYTES, max_depth=32
                    )
                except JsonError:
                    continue
                if isinstance(event, dict):
                    self._on_event(event)
        except (WebSocketError, OSError):
            pass
        finally:
            if connection in self._connections:
                self._connections.remove(connection)

    @property
    def connections(self) -> int:
        return len(self._connections)

    def close(self) -> None:
        self._stop.set()
        for connection in list(self._connections):
            connection.close()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class NapCatActionClient:
    """HTTP client for NapCat's OneBot v11 action server.

    Actions are POSTed to ``{http_url}/{action}`` with a Bearer token. The
    response body is the OneBot v11 response object (``status``/``retcode``).
    """

    def __init__(self, http_url: str, token: str | None, timeout_seconds: float):
        parts = urlsplit(http_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise HttpActionError("http_url_invalid")
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._base_path = (parts.path or "").rstrip("/")
        self._scheme = parts.scheme
        self._token = token
        self._timeout = timeout_seconds

    def _call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        path = f"{self._base_path}/{action}"
        body = encode_json(params)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "virtual-life-qq-node/1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._scheme == "https":
            connection = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        else:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_HTTP_BODY_BYTES + 1)
            status = response.status
        except (OSError, socket.timeout) as exc:
            raise HttpActionError("action_network_error") from exc
        finally:
            connection.close()
        if status < 200 or status >= 300:
            raise HttpActionError("action_http_error", status=status)
        try:
            result = decode_json(raw, max_bytes=MAX_HTTP_BODY_BYTES, max_depth=32)
        except JsonError as exc:
            raise HttpActionError("action_response_invalid") from exc
        if not isinstance(result, dict):
            raise HttpActionError("action_response_invalid")
        return result

    def send_private(self, user_id: int, text: str) -> dict[str, Any]:
        return self._call("send_private_msg", {"user_id": user_id, "message": text})
