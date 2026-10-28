"""Unit tests for the NapCat reverse-WS server and HTTP action client.

Uses a local loopback socket pair for the WS handshake and a ThreadingHTTPServer
for the HTTP action client. Never talks to a real NapCat or the network beyond
127.0.0.1.
"""

import base64
import hashlib
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_node.json_utils import encode_json  # noqa: E402
from qq_node.onebot import (  # noqa: E402
    HttpActionError,
    NapCatActionClient,
    WebSocketError,
    _handle_upgrade,
)


def _client_handshake(sock: socket.socket, key: str, token: str | None):
    headers = [
        "GET / HTTP/1.1",
        "Host: 127.0.0.1",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token is not None:
        headers.append(f"Authorization: Bearer {token}")
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))


class HandshakeTest(unittest.TestCase):
    def _make_pair(self):
        left, right = socket.socketpair()
        return left, right

    def test_handshake_accepts_without_token(self):
        left, right = self._make_pair()
        key = base64.b64encode(b"0123456789abcdef").decode()
        _client_handshake(right, key, None)
        try:
            sock, _ = _handle_upgrade(left, None)
            response = right.recv(4096)
            self.assertIn(b"101", response)
            self.assertIn(b"Sec-WebSocket-Accept:", response)
            expected = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            )
            self.assertIn(expected, response)
            sock.close()
        finally:
            left.close()
            right.close()

    def test_handshake_accepts_matching_token(self):
        left, right = self._make_pair()
        key = base64.b64encode(b"0123456789abcdef").decode()
        _client_handshake(right, key, "secret")
        try:
            _, _ = _handle_upgrade(left, "secret")
            self.assertIn(b"101", right.recv(4096))
        finally:
            left.close()
            right.close()

    def test_handshake_rejects_wrong_token(self):
        left, right = self._make_pair()
        key = base64.b64encode(b"0123456789abcdef").decode()
        _client_handshake(right, key, "wrong")
        try:
            with self.assertRaises(WebSocketError) as ctx:
                _handle_upgrade(left, "secret")
            self.assertEqual(ctx.exception.code, "token_verify_failed")
        finally:
            left.close()
            right.close()

    def test_handshake_rejects_non_get(self):
        left, right = self._make_pair()
        right.sendall(b"POST / HTTP/1.1\r\nHost: x\r\n\r\n")
        try:
            with self.assertRaises(WebSocketError):
                _handle_upgrade(left, None)
        finally:
            left.close()
            right.close()


class _ActionHandler(BaseHTTPRequestHandler):
    """Echo the request path and body; return a canned OneBot response."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = {"status": "ok", "retcode": 0, "path": self.path}
        if "send_private_msg" in self.path:
            payload["sent_user"] = json.loads(body).get("user_id")
        raw = encode_json(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        return


class ActionClientTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ActionHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_send_private_posts_action_and_token(self):
        captured = {}

        class _Handler(_ActionHandler):
            def do_POST(self):
                captured["authorization"] = self.headers.get("Authorization")
                captured["path"] = self.path
                super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = NapCatActionClient(f"http://127.0.0.1:{port}", "napcat-secret", 5)
            result = client.send_private(10001, "你好")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["sent_user"], 10001)
            self.assertEqual(captured["path"], "/send_private_msg")
            self.assertEqual(captured["authorization"], "Bearer napcat-secret")
        finally:
            server.shutdown()
            server.server_close()

    def test_send_private_without_token_omits_header(self):
        captured = {}

        class _Handler(_ActionHandler):
            def do_POST(self):
                captured["authorization"] = self.headers.get("Authorization")
                super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = NapCatActionClient(f"http://127.0.0.1:{port}", None, 5)
            client.send_private(10001, "hi")
            self.assertIsNone(captured["authorization"])
        finally:
            server.shutdown()
            server.server_close()

    def test_invalid_url_rejected(self):
        with self.assertRaises(HttpActionError):
            NapCatActionClient("ftp://127.0.0.1:1", None, 5)


if __name__ == "__main__":
    unittest.main()
