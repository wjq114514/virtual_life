"""Unit tests for the QQ bridge runtime (stdlib-only, no live server)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_node.config import GroupBinding, QQConfig
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
        self.follow_calls = []

    def register(self, node_id, label, capabilities):
        self.registered = (node_id, label, list(capabilities))

    def heartbeat(self, node_id, context):
        self.heartbeat_calls.append((node_id, context))

    def attention(self, node_id, payload):
        self.attention_calls.append((node_id, payload))

    def get_companion(self, companion_id):
        return dict(self.companion_state)

    def follow_room(self, companion_id, payload):
        self.follow_calls.append((companion_id, payload))
        return {"ok": True}

    def dialogue(self, companion_id, payload):
        self.dialogue_calls.append((companion_id, payload))
        return dict(self.dialogue_result)


class _StubActions:
    """Stands in for NapCatActionClient so drain_sends has something to call."""

    def __init__(self):
        self.sent = []

    def send_private(self, user_id, text):
        self.sent.append(("private", user_id, text))
        return {"status": "ok"}

    def send_group(self, group_id, text):
        self.sent.append(("group", group_id, text))
        return {"status": "ok"}


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
        "groups": [],
        "test_user_id": 2563853921,
        "interval_seconds": 30,
        "request_timeout_seconds": 10,
        "max_history": 6,
        "max_output_tokens": 512,
        "screen_observer_node": None,
        "maintain_node": True,
        "send_interval_min_seconds": 0.8,
        "send_interval_jitter_seconds": 1.2,
        "send_max_retries": 3,
        "retry_backoff_initial_seconds": 2,
        "merge_on_limit": True,
        "max_messages_per_reply": 3,
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
            self.assertEqual(list(self.runtime._send_queue), [("private", 10001, "好的，我记下了。")])

    def test_private_message_without_text_is_ignored(self):
        self.runtime.handle_event(
            {"post_type": "message", "message_type": "private", "user_id": 10001, "message": ""}
        )
        self.assertEqual(self.api.dialogue_calls, [])

    def test_unbound_group_message_is_ignored(self):
        self.runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 999,
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
            self.assertEqual(list(self.runtime._send_queue), [])

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
            self.assertEqual(list(self.runtime._send_queue), [("private", 10001, "稍后见")])

    def test_reply_messages_are_queued_as_bubbles(self):
        # A completed turn may carry reply_messages (server-side split). The
        # runtime must queue each bubble as its own paced message.
        self.api.dialogue_result = {
            "status": "completed",
            "assistant_message": "嗯，看到了。那一栏挺简洁的。",
            "reply_messages": ["嗯，看到了。", "那一栏挺简洁的。"],
        }
        actions = _StubActions()
        self.runtime._actions = actions
        self.runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        with self.runtime._lock:
            self.assertEqual(
                list(self.runtime._send_queue),
                [("private", 10001, "嗯，看到了。"), ("private", 10001, "那一栏挺简洁的。")],
            )
        # drain_sends paces ONE bubble per cycle and enforces the minimum
        # inter-bubble interval: the second drain call inside the interval
        # window must NOT send.
        clock = FakeClock()
        self.runtime._monotonic = clock.monotonic
        self.runtime._sleep = clock.sleep
        self.runtime.drain_sends()
        with self.runtime._lock:
            self.assertEqual(list(self.runtime._send_queue), [("private", 10001, "那一栏挺简洁的。")])
        self.assertEqual(actions.sent, [("private", 10001, "嗯，看到了。")])
        # Still inside the interval window -> nothing new is sent.
        self.runtime.drain_sends()
        self.assertEqual(len(actions.sent), 1)
        # Advance the fake clock past the interval -> the bubble goes out.
        clock.advance(self.runtime._next_send_at + 1.0)
        self.runtime.drain_sends()
        self.assertEqual(
            actions.sent,
            [
                ("private", 10001, "嗯，看到了。"),
                ("private", 10001, "那一栏挺简洁的。"),
            ],
        )


class FakeClock:
    """Deterministic monotonic clock + recorded sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, to):
        self.now = max(self.now, to)


class _FlakyActions:
    """Fails the first N sends, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.sent = []

    def send_private(self, user_id, text):
        return self._send(text)

    def send_group(self, group_id, text):
        return self._send(text)

    def _send(self, text):
        if self.fail_times > 0:
            self.fail_times -= 1
            return {"status": "failed", "retcode": 1200}
        self.sent.append(text)
        return {"status": "ok"}


class PacingTest(unittest.TestCase):
    def _runtime(self, **overrides):
        api = StubApi()
        runtime = QQRuntime(config(**overrides), api)
        clock = FakeClock()
        runtime._monotonic = clock.monotonic
        runtime._sleep = clock.sleep
        runtime._clock = clock
        return runtime, api

    def test_interval_between_bubbles_at_least_min(self):
        runtime, _ = self._runtime()
        actions = _StubActions()
        runtime._actions = actions
        runtime._enqueue("private", 10001, "a")
        runtime._enqueue("private", 10001, "b")
        runtime.drain_sends()
        first_at = runtime._next_send_at
        # The next send is gated until the interval has elapsed.
        runtime._clock.advance(first_at - 0.01)
        runtime.drain_sends()
        self.assertEqual(len(actions.sent), 1)
        runtime._clock.advance(runtime._next_send_at)
        runtime.drain_sends()
        self.assertEqual(len(actions.sent), 2)
        # interval >= send_interval_min_seconds (0.8)
        self.assertGreaterEqual(first_at - 1000.0, 0.8)
        self.assertLessEqual(first_at - 1000.0, 0.8 + 1.2)

    def test_failure_retries_with_exponential_backoff_then_succeeds(self):
        runtime, _ = self._runtime(
            send_max_retries=2, retry_backoff_initial_seconds=2, send_interval_jitter_seconds=0
        )
        actions = _FlakyActions(fail_times=2)
        runtime._actions = actions
        runtime._enqueue("private", 10001, "hi")
        runtime.drain_sends()
        # Two failures then success: sleeps 2, 4 (exponential).
        self.assertEqual(runtime._clock.sleeps, [2, 4])
        self.assertEqual(actions.sent, ["hi"])

    def test_retries_exhausted_merges_remaining_bubbles_into_one(self):
        runtime, _ = self._runtime(
            send_max_retries=0, retry_backoff_initial_seconds=2, send_interval_jitter_seconds=0
        )
        actions = _FlakyActions(fail_times=1)
        runtime._actions = actions
        runtime._enqueue("private", 10001, "one")
        runtime._enqueue("private", 10001, "two")
        runtime.drain_sends()
        # First bubble failed (0 retries) -> remaining merged into one attempt.
        self.assertEqual(actions.sent, ["one\ntwo"])
        with runtime._lock:
            self.assertEqual(list(runtime._send_queue), [])

    def test_merge_keeps_bubbles_of_other_chats(self):
        runtime, _ = self._runtime(
            send_max_retries=0, retry_backoff_initial_seconds=2, send_interval_jitter_seconds=0
        )
        actions = _FlakyActions(fail_times=1)
        runtime._actions = actions
        runtime._enqueue("private", 10001, "one")
        runtime._enqueue("private", 20002, "other-chat")
        runtime.drain_sends()
        self.assertEqual(actions.sent, ["one"])
        with runtime._lock:
            self.assertEqual(
                list(runtime._send_queue), [("private", 20002, "other-chat")]
            )

    def test_merge_disabled_drops_only_failed_bubble(self):
        runtime, _ = self._runtime(
            send_max_retries=0, retry_backoff_initial_seconds=2,
            send_interval_jitter_seconds=0, merge_on_limit=False,
        )
        actions = _FlakyActions(fail_times=1)
        runtime._actions = actions
        runtime._enqueue("private", 10001, "one")
        runtime._enqueue("private", 10001, "two")
        runtime.drain_sends()
        self.assertEqual(actions.sent, [])
        with runtime._lock:
            self.assertEqual(list(runtime._send_queue), [("private", 10001, "two")])


class GroupSupportTest(unittest.TestCase):
    @staticmethod
    def _groups(raw):
        return [
            GroupBinding(
                group_id=item["group_id"],
                companion_id=item["companion"],
                senders=tuple(item.get("senders", ())),
            )
            for item in raw
        ]

    def _runtime(self, groups, **overrides):
        api = StubApi()
        runtime = QQRuntime(config(groups=self._groups(groups), **overrides), api)
        actions = _StubActions()
        runtime._actions = actions
        return runtime, api, actions

    def _group_event(self, group_id=12345, user_id=10001, card=None, text="群里问个事"):
        sender = {"nickname": "小林"}
        if card is not None:
            sender["card"] = card
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": group_id,
            "user_id": user_id,
            "sender": sender,
            "message": [{"type": "text", "data": {"text": text}}],
        }

    def test_group_message_runs_dialogue_and_replies_to_same_group(self):
        runtime, api, actions = self._runtime([{"group_id": 12345, "companion": "sui"}])
        api.dialogue_result = {"status": "completed", "assistant_message": "群里回复"}
        runtime.handle_event(self._group_event(card="群名片"))
        self.assertEqual(len(api.dialogue_calls), 1)
        _, payload = api.dialogue_calls[0]
        self.assertEqual(payload["user_name"], "群名片")
        clock = FakeClock()
        runtime._monotonic = clock.monotonic
        runtime._sleep = clock.sleep
        runtime.drain_sends()
        self.assertEqual(actions.sent, [("group", 12345, "群里回复")])

    def test_group_sender_filter(self):
        runtime, api, _ = self._runtime(
            [{"group_id": 12345, "companion": "sui", "senders": [10001]}]
        )
        runtime.handle_event(self._group_event(user_id=10001))
        self.assertEqual(len(api.dialogue_calls), 1)
        runtime.handle_event(self._group_event(user_id=99999))
        self.assertEqual(len(api.dialogue_calls), 1)

    def test_group_at_extraction(self):
        runtime, api, actions = self._runtime([{"group_id": 12345, "companion": "sui"}])
        runtime.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 12345,
                "user_id": 10001,
                "sender": {"nickname": "小林"},
                "message": [
                    {"type": "at", "data": {"qq": "2563853921"}},
                    {"type": "text", "data": {"text": " 你在吗"}},
                ],
            }
        )
        self.assertEqual(api.dialogue_calls[0][1]["user_message"], "@2563853921 你在吗")

    def test_max_messages_per_reply_caps_bubbles(self):
        runtime, api, actions = self._runtime(
            [{"group_id": 12345, "companion": "sui"}], max_messages_per_reply=2
        )
        api.dialogue_result = {
            "status": "completed",
            "assistant_message": "x",
            "reply_messages": ["a", "b", "c", "d"],
        }
        runtime.handle_event(self._group_event())
        with runtime._lock:
            self.assertEqual(
                list(runtime._send_queue),
                [("group", 12345, "a"), ("group", 12345, "b")],
            )


if __name__ == "__main__":
    unittest.main()
