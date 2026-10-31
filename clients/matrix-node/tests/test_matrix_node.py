"""Unit tests for the Matrix node bridge (stdlib-only, no network).

Covers: config validation, message extraction (plain + HTML), the Matrix client
login/sync/send/join request building via a mock opener, and the runtime
event-to-dialogue mapping with a mock API + mock Matrix client.
"""

import json
import unittest
from pathlib import Path

from matrix_node.config import ConfigError, load_config
from matrix_node.e2ee_bridge import E2eeBridge
from matrix_node.matrix import (
    MatrixClient,
    MatrixError,
    MatrixEventStream,
    extract_text,
    strip_html,
)
from matrix_node.runtime import (
    REPLY_BUBBLE_CHARS,
    REPLY_MAX_BUBBLES,
    MatrixRuntime,
    split_reply,
)

CONFIG = {
    "server_url": "http://127.0.0.1:8038",
    "token_env": "VIRTUAL_LIFE_API_TOKEN",
    "node_id": "matrix-room",
    "label": "Matrix 房间",
    "homeserver": "https://matrix.example.org",
    "user_id": "@bot:matrix.example.org",
    "auth_method": "password",
    "password": "secret",
    "device_name": "virtual-life-matrix-node",
    "rooms": [{"room": "!room1:matrix.example.org", "companion": "sui"}],
    "interval_seconds": 30,
    "sync_timeout_ms": 30000,
    "request_timeout_seconds": 10,
    "max_history": 6,
    "max_output_tokens": 512,
    "screen_observer_node": None,
    "maintain_node": True,
}


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status
        self.headers = {}

    def read(self, n: int = -1) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Records requests and replays canned responses/errors."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("no canned response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _canned(payload, status=200):
    return FakeResponse(json.dumps(payload).encode("utf-8"), status=status)


class TestConfig(unittest.TestCase):
    def test_load_valid(self):
        path = Path("/tmp/mx_test_config.json")
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config.node_id, "matrix-room")
        self.assertEqual(config.rooms[0].room_id, "!room1:matrix.example.org")
        self.assertEqual(config.rooms[0].companion_id, "sui")

    def test_auth_method_requires_credential(self):
        path = Path("/tmp/mx_test_config.json")
        data = dict(CONFIG)
        data["auth_method"] = "token"
        data["access_token"] = None
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_unknown_field_rejected(self):
        path = Path("/tmp/mx_test_config.json")
        data = dict(CONFIG)
        data["bogus"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_duplicate_room_rejected(self):
        path = Path("/tmp/mx_test_config.json")
        data = dict(CONFIG)
        data["rooms"] = [
            {"room": "!room1:matrix.example.org", "companion": "sui"},
            {"room": "!room1:matrix.example.org", "companion": "w"},
        ]
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)


class TestExtract(unittest.TestCase):
    def test_plain_text(self):
        event = {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "  hello world  "},
        }
        self.assertEqual(extract_text(event), "hello world")

    def test_html_body_preferred(self):
        event = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "hello world",
                "format": "org.matrix.custom.html",
                "formatted_body": "<p>hello <strong>world</strong></p>",
            },
        }
        self.assertEqual(extract_text(event), "hello world")

    def test_non_text_ignored(self):
        event = {"type": "m.room.message", "content": {"msgtype": "m.image", "body": "x"}}
        self.assertEqual(extract_text(event), "")

    def test_strip_html_entities(self):
        self.assertEqual(strip_html("<p>&amp; &lt;x&gt;</p>"), "& <x>")


class TestMatrixClient(unittest.TestCase):
    def _client(self, opener):
        return MatrixClient(
            "https://matrix.example.org",
            "@bot:matrix.example.org",
            auth_method="password",
            password="secret",
            timeout_seconds=5,
            opener=opener,
        )

    def test_login_builds_request(self):
        opener = FakeOpener(
            [_canned({"user_id": "@bot:x", "access_token": "tok", "device_id": "dev"})]
        )
        client = self._client(opener)
        result = client.login()
        self.assertEqual(result["access_token"], "tok")
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://matrix.example.org/_matrix/client/v3/login")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["type"], "m.login.password")
        self.assertEqual(body["identifier"]["user"], "@bot:matrix.example.org")

    def test_sync_uses_since(self):
        opener = FakeOpener([_canned({"next_batch": "s1", "rooms": {}})])
        client = self._client(opener)
        client._access_token = "tok"
        client.sync(since="s0", timeout_ms=30000)
        request = opener.requests[0]
        self.assertIn("since=s0", request.full_url)
        self.assertIn("timeout=30000", request.full_url)

    def test_send_text_uses_txn_id(self):
        opener = FakeOpener([_canned({"event_id": "$ev"})])
        client = self._client(opener)
        client._access_token = "tok"
        event_id = client.send_text("!room:x", "hi")
        self.assertEqual(event_id, "$ev")
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertIn("/rooms/%21room%3Ax/send/m.room.message/", request.full_url)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["body"], "hi")

    def test_join_room(self):
        opener = FakeOpener([_canned({"room_id": "!room:x"})])
        client = self._client(opener)
        client._access_token = "tok"
        result = client.join("!room:x")
        self.assertEqual(result["room_id"], "!room:x")

    def test_matrix_error_on_errcode(self):
        opener = FakeOpener([_canned({"errcode": "M_UNKNOWN_TOKEN", "error": "bad"}, status=401)])
        client = self._client(opener)
        client._access_token = "tok"
        with self.assertRaises(MatrixError):
            client.sync()


class TestEventStream(unittest.TestCase):
    def test_messages_from_joined_rooms(self):
        response = {
            "next_batch": "s2",
            "rooms": {
                "join": {
                    "!r:x": {
                        "timeline": {
                            "events": [
                                {
                                    "type": "m.room.message",
                                    "sender": "@user:x",
                                    "content": {"msgtype": "m.text", "body": "hi"},
                                },
                                {"type": "m.room.message", "content": {"msgtype": "m.image"}},
                                {"type": "m.room.member"},
                            ]
                        }
                    }
                }
            },
        }
        stream = MatrixEventStream(response)
        self.assertEqual(stream.next_batch, "s2")
        messages = stream.messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["room_id"], "!r:x")


class FakeApi:
    def __init__(self):
        self.registered = None
        self.heartbeats = 0
        self.attention_calls = []
        self.dialogue_calls = []
        self.companion = {"revision": 1, "epoch": 0}
        self.dialogue_result = {"status": "completed", "assistant_message": "收到。"}

    def register(self, node_id, label, capabilities):
        self.registered = (node_id, label, capabilities)

    def heartbeat(self, node_id, context):
        self.heartbeats += 1

    def attention(self, node_id, payload):
        self.attention_calls.append(payload)

    def get_companion(self, companion_id):
        return self.companion

    def dialogue(self, companion_id, payload):
        self.dialogue_calls.append((companion_id, payload))
        return dict(self.dialogue_result)


class FakeMatrix:
    def __init__(self):
        self.sent = []
        self.logins = 0

    def login(self):
        self.logins += 1

    def sync(self, since=None, timeout_ms=30000):
        return {"next_batch": "s1", "rooms": {"join": {}}}

    def send_text(self, room_id, text):
        self.sent.append((room_id, text))
        return "$ev"


class TestRuntime(unittest.TestCase):
    def _runtime(self):
        path = Path("/tmp/mx_runtime_config.json")
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        config = load_config(path)
        api = FakeApi()
        client = FakeMatrix()
        return MatrixRuntime(config, api, client), api, client

    def test_register_and_heartbeat(self):
        runtime, api, _ = self._runtime()
        runtime.register()
        self.assertEqual(api.registered[0], "matrix-room")
        runtime.heartbeat()
        self.assertEqual(api.heartbeats, 1)

    def test_handle_message_maps_to_dialogue(self):
        runtime, api, client = self._runtime()
        runtime._handle_message(
            {
                "room_id": "!room1:matrix.example.org",
                "sender": "@user:matrix.example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "你好"},
            }
        )
        self.assertEqual(len(api.dialogue_calls), 1)
        companion, payload = api.dialogue_calls[0]
        self.assertEqual(companion, "sui")
        self.assertEqual(payload["user_message"], "你好")
        # reply queued
        runtime.drain_sends()
        self.assertEqual(client.sent, [("!room1:matrix.example.org", "收到。")])

    def test_own_message_ignored(self):
        runtime, api, _ = self._runtime()
        runtime._handle_message(
            {
                "room_id": "!room1:matrix.example.org",
                "sender": "@bot:matrix.example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "hello"},
            }
        )
        self.assertEqual(len(api.dialogue_calls), 0)

    def test_unbound_room_ignored(self):
        runtime, api, _ = self._runtime()
        runtime._handle_message(
            {
                "room_id": "!other:x",
                "sender": "@user:x",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "hello"},
            }
        )
        self.assertEqual(len(api.dialogue_calls), 0)

    def test_sender_filter_allows_and_rejects(self):
        """A binding with `senders` accepts only those senders."""
        path = Path("/tmp/mx_runtime_config_sf.json")
        cfg = dict(CONFIG)
        cfg["rooms"] = [
            {
                "room": "!room1:matrix.example.org",
                "companion": "sui",
                "senders": ["@allowed:matrix.example.org"],
            }
        ]
        path.write_text(json.dumps(cfg), encoding="utf-8")
        config = load_config(path)
        api = FakeApi()
        client = FakeMatrix()
        runtime = MatrixRuntime(config, api, client)
        # allowed sender -> dialogue
        runtime._handle_message(
            {
                "room_id": "!room1:matrix.example.org",
                "sender": "@allowed:matrix.example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "你好"},
            }
        )
        self.assertEqual(len(api.dialogue_calls), 1)
        # disallowed sender -> ignored
        runtime._handle_message(
            {
                "room_id": "!room1:matrix.example.org",
                "sender": "@blocked:matrix.example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "hi"},
            }
        )
        self.assertEqual(len(api.dialogue_calls), 1)

    def test_sender_filter_absent_accepts_all(self):
        """No `senders` on a binding -> any non-self sender accepted."""
        runtime, api, _ = self._runtime()
        runtime._handle_message(
            {
                "room_id": "!room1:matrix.example.org",
                "sender": "@anyone:matrix.example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "你好"},
            }
        )
        self.assertEqual(len(api.dialogue_calls), 1)

    def test_config_senders_validation(self):
        path = Path("/tmp/mx_runtime_config_sv.json")
        cfg = dict(CONFIG)
        cfg["rooms"] = [
            {"room": "!room1:matrix.example.org", "companion": "sui", "senders": ["@a:x", "@a:x"]}
        ]
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_config_senders_not_string_rejected(self):
        path = Path("/tmp/mx_runtime_config_sn.json")
        cfg = dict(CONFIG)
        cfg["rooms"] = [
            {"room": "!room1:matrix.example.org", "companion": "sui", "senders": [1, 2]}
        ]
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_attention_sample_sequence_monotonic(self):
        runtime, _, _ = self._runtime()
        s1 = runtime._attention_sample(user_present=True)
        s2 = runtime._attention_sample(user_present=False)
        self.assertEqual(s2["sequence"], s1["sequence"] + 1)
        self.assertTrue(s1["user_present"])
        self.assertFalse(s2["user_present"])

    def test_sidecar_message_normalized_to_dialogue(self):
        """E2EE sidecar plaintext messages drive the same dialogue path."""
        runtime, api, client = self._runtime()
        bridge = runtime._e2ee
        self.assertIsNone(bridge)
        # Attach a fake bridge after construction to simulate sidecar events.
        runtime._e2ee = _FakeBridge()
        runtime._e2ee.on_message(runtime._on_sidecar_message)
        runtime._e2ee.emit(
            {
                "event": "message",
                "room_id": "!room1:matrix.example.org",
                "sender": "@user:matrix.example.org",
                "body": "你好",
                "event_id": "$e",
                "ts": 1234,
            }
        )
        self.assertEqual(len(api.dialogue_calls), 1)
        companion, payload = api.dialogue_calls[0]
        self.assertEqual(companion, "sui")
        self.assertEqual(payload["user_message"], "你好")

    def test_sidecar_message_own_sender_ignored(self):
        runtime, api, _ = self._runtime()
        runtime._e2ee = _FakeBridge()
        runtime._e2ee.on_message(runtime._on_sidecar_message)
        runtime._e2ee.emit(
            {
                "event": "message",
                "room_id": "!room1:matrix.example.org",
                "sender": "@bot:matrix.example.org",
                "body": "hello",
            }
        )
        self.assertEqual(len(api.dialogue_calls), 0)

    def test_drain_sends_routes_to_sidecar_when_e2ee(self):
        runtime, api, client = self._runtime()
        runtime._e2ee = _FakeBridge()
        runtime._e2ee.on_message(runtime._on_sidecar_message)
        runtime._send_text("!room1:matrix.example.org", "回复")
        runtime.drain_sends()
        self.assertEqual(runtime._e2ee.sent, [("!room1:matrix.example.org", "回复")])
        self.assertEqual(client.sent, [])

    def test_sidecar_verification_sas_surfaced(self):
        """SAS emoji/decimal events are surfaced and stored on the runtime."""
        runtime, api, _ = self._runtime()
        bridge = _FakeBridge()
        runtime._e2ee = bridge
        runtime._e2ee.on_message(runtime._on_sidecar_message)
        runtime._e2ee.on_verification(runtime._on_sidecar_verification)
        bridge.emit_verification(
            {
                "event": "verification",
                "state": "sas",
                "transaction_id": "$txn",
                "emoji": [["🐶", "Dog"], ["🚀", "Rocket"]],
                "decimal": [128, 256, 512],
            }
        )
        state = runtime.verification_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["state"], "sas")
        self.assertEqual(state["transaction_id"], "$txn")
        self.assertEqual(len(state["emoji"]), 2)

    def test_sidecar_verification_confirm_routes_to_bridge(self):
        runtime, api, _ = self._runtime()
        bridge = _FakeBridge()
        runtime._e2ee = bridge
        runtime._e2ee.on_message(runtime._on_sidecar_message)
        runtime._e2ee.on_verification(runtime._on_sidecar_verification)
        bridge.emit_verification(
            {"event": "verification", "state": "sas", "transaction_id": "$txn"}
        )
        runtime.verification_confirm("$txn")
        self.assertEqual(bridge.verification_commands, [("confirm", "$txn")])

    def test_sidecar_verification_done_clears(self):
        runtime, api, _ = self._runtime()
        bridge = _FakeBridge()
        runtime._e2ee = bridge
        runtime._e2ee.on_message(runtime._on_sidecar_message)
        runtime._e2ee.on_verification(runtime._on_sidecar_verification)
        bridge.emit_verification(
            {"event": "verification", "state": "done", "transaction_id": "$txn"}
        )
        state = runtime.verification_state()
        self.assertEqual(state["state"], "done")

    def test_verification_unavailable_without_e2ee(self):
        runtime, api, _ = self._runtime()
        self.assertIsNone(runtime._e2ee)
        with self.assertRaises(RuntimeError):
            runtime.verification_confirm("$txn")

    def test_e2ee_bridge_spawns_and_reads_ready(self):
        """E2eeBridge starts a real node sidecar and receives the ready event."""
        sidecar_dir = Path(__file__).resolve().parent.parent / "e2ee"
        bridge = E2eeBridge(
            sidecar_dir,
            config={
                "baseUrl": "https://matrix.example.org",
                "userId": "@bot:matrix.example.org",
                "accessToken": "x",
                "deviceId": "DEV",
            },
        )
        bridge.start(timeout_seconds=15)
        self.assertTrue(bridge._ready.is_set())
        bridge.send_command({"cmd": "ping"})
        bridge.stop()


class _FakeBridge:
    """Minimal stand-in for E2eeBridge used in runtime tests."""

    def __init__(self):
        self._handlers = []
        self._verification_handlers = []
        self.sent = []
        self.verification_commands = []

    def on_message(self, handler):
        self._handlers.append(handler)

    def on_verification(self, handler):
        self._verification_handlers.append(handler)

    def emit(self, event):
        for handler in list(self._handlers):
            handler(event)

    def emit_verification(self, event):
        for handler in list(self._verification_handlers):
            handler(event)

    def send_command(self, command):
        self.sent.append((command.get("room_id"), command.get("body")))

    def send_verification_command(self, action, transaction_id=None):
        self.verification_commands.append((action, transaction_id))
        return {}


class SplitReplyTest(unittest.TestCase):
    def test_short_reply_single_bubble(self):
        self.assertEqual(split_reply("好的，我记下了。"), ["好的，我记下了。"])

    def test_long_reply_splits_into_multiple_bubbles(self):
        reply = "嗯，看到了，屏幕最显眼的文字应该是“空闲”，下面还有“文字提交 / 朗读”那一栏…这个界面看着还挺简洁的。"
        bubbles = split_reply(reply)
        self.assertGreaterEqual(len(bubbles), 2)
        self.assertLessEqual(len(bubbles), REPLY_MAX_BUBBLES)
        for bubble in bubbles:
            self.assertGreater(len(bubble), 0)

    def test_overflow_merged_into_last_bubble(self):
        reply = "x" * (REPLY_BUBBLE_CHARS * REPLY_MAX_BUBBLES + 10)
        bubbles = split_reply(reply)
        self.assertLessEqual(len(bubbles), REPLY_MAX_BUBBLES)

    def test_empty_returns_empty(self):
        self.assertEqual(split_reply("   "), [])


class ReplyBubblingTest(unittest.TestCase):
    def setUp(self):
        path = Path("/tmp/mx_runtime_config.json")
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        self.config = load_config(path)
        self.api = FakeApi()
        self.client = FakeMatrix()

    def _make(self):
        return MatrixRuntime(self.config, self.api, self.client)

    def test_long_reply_queues_paced_bubbles(self):
        runtime = self._make()
        self.api.dialogue_result = {
            "status": "completed",
            "assistant_message": (
                "嗯，看到了，屏幕最显眼的文字应该是“空闲”，"
                "下面还有“文字提交 / 朗读”那一栏…这个界面看着还挺简洁的。"
            ),
        }
        runtime._handle_message(
            {
                "room_id": "!room1:matrix.example.org",
                "sender": "@user:matrix.example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "屏幕上有啥"},
            }
        )
        with runtime._lock:
            queued = len(runtime._reply_queue)
        self.assertLessEqual(queued, REPLY_MAX_BUBBLES)
        # first drain sends ONE paced bubble (fast-path pending is empty for multi)
        runtime.drain_sends()
        with runtime._lock:
            remaining = len(runtime._reply_queue)
        self.assertLessEqual(remaining, queued)

    def test_short_reply_uses_fast_path(self):
        runtime = self._make()
        self.api.dialogue_result = {
            "status": "completed",
            "assistant_message": "好的，我记下了。",
        }
        runtime._handle_message(
            {
                "room_id": "!room1:matrix.example.org",
                "sender": "@user:matrix.example.org",
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "hi"},
            }
        )
        with runtime._lock:
            self.assertEqual(
                runtime._pending_send.get("!room1:matrix.example.org"), "好的，我记下了。"
            )
            self.assertEqual(runtime._reply_queue, [])


if __name__ == "__main__":
    unittest.main()
