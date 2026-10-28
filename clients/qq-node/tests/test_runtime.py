"""Unit tests for the QQ bridge runtime (stdlib-only, no live server)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_node.config import QQConfig
from qq_node.runtime import MAX_MESSAGE_LENGTH, QQRuntime


class StubApi:
    """Records calls; returns scripted replies. Never touches the network."""

    def __init__(self):
        self.registered = None
        self.heartbeat_calls = []
        self.attention_calls = []
        self.companion_state = {"revision": 3, "epoch": 7}
        self.dialogue_calls = []
        self.dialogue_result = {"status": "silent"}

    def register(self, node_id, label, capabilities):
        self.registered = (node_id, label, list(capabilities))

    def heartbeat(self, node_id, context):
        self.heartbeat_calls.append((node_id, context))

    def attention(self, node_id, payload):
        self.attention_calls.append((node_id, payload))

    def get_companion(self, companion_id):
        return dict(self.companion_state)

    def dialogue(self, companion_id, payload):
        self.dialogue_calls.append((companion_id, payload))
        return dict(self.dialogue_result)


def config(**overrides):
    base = {
        "server_url": "http://127.0.0.1:8038",
        "token_env": "VIRTUAL_LIFE_API_TOKEN",
        "node_id": "qq-room",
        "label": "QQ 房间",
        "websocket_url": "ws://127.0.0.1:6700",
        "http_url": "http://127.0.0.1:3000",
        "access_token": None,
        "companions": ["sui"],
        "test_user_id": 2563853921,
        "interval_seconds": 30,
        "request_timeout_seconds": 10,
        "max_history": 6,
        "max_output_tokens": 512,
    }
    base.update(overrides)
    return QQConfig(**base)


class QQRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.api = StubApi()
        self.runtime = QQRuntime(config(), self.api)

    def test_register_declares_room_and_capabilities(self):
        self.runtime.register()
        self.assertEqual(self.api.registered[0], "qq-room")
        self.assertIn("qq.message", self.api.registered[2])

    def test_typing_notice_reports_present_and_chat_activity(self):
        self.runtime.handle_event(
            {"post_type": "notice", "notice_type": "typing", "user_id": 10001}
        )
        self.assertEqual(len(self.api.attention_calls), 1)
        node, payload = self.api.attention_calls[0]
        self.assertEqual(node, "qq-room")
        sample = payload["sample"]
        self.assertTrue(sample["user_present"])
        self.assertEqual(sample["activity"], "chat")
        self.assertEqual(sample["focus"], "focused")
        self.assertIsInstance(payload["command_id"], str)

    def test_offline_notice_reports_absent(self):
        self.runtime.handle_event(
            {"post_type": "notice", "notice_type": "offline", "user_id": 10001}
        )
        sample = self.api.attention_calls[0][1]["sample"]
        self.assertFalse(sample["user_present"])

    def test_meta_event_heartbeat_reports_present(self):
        self.runtime.handle_event({"post_type": "meta_event", "meta_event_type": "heartbeat"})
        sample = self.api.attention_calls[0][1]["sample"]
        self.assertTrue(sample["user_present"])

    def test_private_message_runs_dialogue_and_queues_reply(self):
        self.api.dialogue_result = {
            "status": "completed",
            "assistant_message": "好的，我记下了。",
        }
        self.runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "nickname": "小林",
                "message": [{"type": "text", "data": {"text": "我明天出差"}}],
            }
        )
        self.assertEqual(len(self.api.dialogue_calls), 1)
        _, payload = self.api.dialogue_calls[0]
        self.assertEqual(payload["node_id"], "qq-room")
        self.assertEqual(payload["epoch"], 7)
        self.assertEqual(payload["expected_revision"], 3)
        self.assertEqual(payload["user_message"], "我明天出差")
        self.assertEqual(payload["user_name"], "小林")
        self.assertEqual(payload["max_output_tokens"], 512)
        self.assertTrue(payload["command_id"].startswith("qq-"))
        with self.runtime._lock:
            self.assertEqual(self.runtime._pending_send.get(10001), "好的，我记下了。")

    def test_private_message_without_text_is_ignored(self):
        self.runtime.handle_event(
            {"post_type": "message", "message_type": "private", "user_id": 10001, "message": ""}
        )
        self.assertEqual(self.api.dialogue_calls, [])

    def test_group_message_is_ignored(self):
        self.runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 10001,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        self.assertEqual(self.api.dialogue_calls, [])

    def test_long_message_is_truncated(self):
        self.api.dialogue_result = {"status": "silent"}
        self.runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [{"type": "text", "data": {"text": "x" * (MAX_MESSAGE_LENGTH + 500)}}],
            }
        )
        self.assertEqual(len(self.api.dialogue_calls), 1)
        self.assertEqual(len(self.api.dialogue_calls[0][1]["user_message"]), MAX_MESSAGE_LENGTH)

    def test_segment_extraction_image_and_at(self):
        text = self.runtime._extract_text(
            [
                {"type": "text", "data": {"text": "看"}},
                {"type": "image", "data": {}},
                {"type": "at", "data": {"qq": "12345"}},
            ]
        )
        self.assertEqual(text, "看[图片]@12345")

    def test_silent_without_message_is_not_queued(self):
        self.api.dialogue_result = {"status": "silent"}
        self.runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        with self.runtime._lock:
            self.assertEqual(self.runtime._pending_send, {})

    def test_silent_with_message_is_queued(self):
        # A silent turn may still carry a short directive; the runtime sends it.
        self.api.dialogue_result = {"status": "silent", "assistant_message": "稍后见"}
        self.runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        with self.runtime._lock:
            self.assertEqual(self.runtime._pending_send.get(10001), "稍后见")


if __name__ == "__main__":
    unittest.main()
