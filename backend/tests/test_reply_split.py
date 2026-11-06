"""Tests for the reply-splitting feature (netizen-style multi-bubble replies)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_api import attach_desktop
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.reply_split import split_reply, typing_delays
from virtual_life.settings import Settings


class TestSplitReply:
    def test_short_reply_stays_single(self):
        text = "嗯，看到了。"
        assert split_reply(text) == [text]

    def test_short_reply_no_punctuation_stays_single(self):
        text = "好的我记下了"
        assert split_reply(text) == [text]

    def test_empty_and_whitespace_return_empty(self):
        assert split_reply("") == []
        assert split_reply("   \n ") == []

    def test_sentence_split_within_limit_stays_single(self):
        text = "今天天气不错，我们出去走走吧。顺便买点东西，晚上一起做饭。"
        assert split_reply(text, max_chars=120) == [text]

    def test_long_sentences_split_into_multiple(self):
        text = "嗯，看到了。那一栏看起来挺简洁的。这个方案其实挺清楚的，我们按这个来就好。"
        result = split_reply(text, max_messages=3, max_chars=40)
        assert 1 <= len(result) <= 3
        assert "".join(result) == text  # no content dropped

    def test_hard_chunk_last_resort(self):
        text = "x" * 400
        result = split_reply(text, max_messages=3, max_chars=120)
        assert len(result) == 3
        assert "".join(result) == text

    def test_max_messages_clamped(self):
        text = "一。二。三。四。五。六。七。"
        result = split_reply(text, max_messages=1, max_chars=5)
        assert len(result) == 1
        assert "".join(result) == text

    def test_never_drops_content(self):
        text = "第一句，有逗号。第二句！第三句？第四句；第五句、第六句，第七句。第八句第九句第十句"
        result = split_reply(text, max_messages=3, max_chars=15)
        assert "".join(result) == text


class LongProvider:
    """A provider that returns a reply long enough to be split."""

    def complete(self, request):
        text = (
            "嗯，看到了。那一栏看起来挺简洁的。这个方案其实挺清楚的，"
            "我们按这个来就好。先把基础部分做完，后面再慢慢调。"
        )
        return ModelResult(text, "fake", ModelUsage(80, 20))


class TestReplyMessagesInDialogue:
    def test_reply_messages_present_when_enabled(self, tmp_path):
        provider = LongProvider()
        settings = Settings(
            database_path=str(tmp_path / "dialogue.sqlite"),
            api_token="test-token",
            model_base_url="http://model.invalid",
            model_name="fake",
            dialogue_reply_split_enabled=True,
            dialogue_reply_split_max_messages=3,
            dialogue_reply_split_max_chars=40,
        )
        app = create_app(settings, model_provider=provider)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            state = attach_desktop(client)
            response = client.post(
                "/v1/companions/sui/dialogue",
                json={
                    "command_id": "split-on",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "user_message": "这个方案你觉得怎么样",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "completed"
            assert result["assistant_message"]
            assert result["reply_messages"] is not None
            assert 1 <= len(result["reply_messages"]) <= 3
            # no content dropped
            assert "".join(result["reply_messages"]) == result["assistant_message"]

    def test_reply_messages_none_when_disabled(self, tmp_path):
        provider = LongProvider()
        settings = Settings(
            database_path=str(tmp_path / "dialogue.sqlite"),
            api_token="test-token",
            model_base_url="http://model.invalid",
            model_name="fake",
            dialogue_reply_split_enabled=False,
        )
        app = create_app(settings, model_provider=provider)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            state = attach_desktop(client)
            response = client.post(
                "/v1/companions/sui/dialogue",
                json={
                    "command_id": "split-off",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "user_message": "这个方案你觉得怎么样",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "completed"
            assert result["assistant_message"]
            assert result["reply_messages"] is None


class TestReplySplitEngineField:
    def test_reply_split_engine_rules_when_split(self, tmp_path):
        provider = LongProvider()
        settings = Settings(
            database_path=str(tmp_path / "engine.sqlite"),
            api_token="test-token",
            model_base_url="http://model.invalid",
            model_name="fake",
            dialogue_reply_split_enabled=True,
            dialogue_reply_split_max_messages=3,
            dialogue_reply_split_max_chars=40,
        )
        app = create_app(settings, model_provider=provider)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            state = attach_desktop(client)
            response = client.post(
                "/v1/companions/sui/dialogue",
                json={
                    "command_id": "engine-rules",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "user_message": "这个方案你觉得怎么样",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "completed"
            # no LLM splitter configured: the deterministic rules engine runs
            assert result["reply_split_engine"] == "rules"
            assert result["reply_messages"] is not None

    def test_reply_split_engine_none_when_disabled(self, tmp_path):
        provider = LongProvider()
        settings = Settings(
            database_path=str(tmp_path / "engine-off.sqlite"),
            api_token="test-token",
            model_base_url="http://model.invalid",
            model_name="fake",
            dialogue_reply_split_enabled=False,
        )
        app = create_app(settings, model_provider=provider)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            state = attach_desktop(client)
            response = client.post(
                "/v1/companions/sui/dialogue",
                json={
                    "command_id": "engine-none",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "user_message": "这个方案你觉得怎么样",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "completed"
            assert result["reply_split_engine"] == "none"
            assert result["reply_messages"] is None


class TestTypingDelays:
    def test_empty_bubbles_return_empty(self):
        assert typing_delays([]) == []

    def test_deterministic_for_same_inputs(self):
        bubbles = ["嗯，看到了。", "这个方案其实挺清楚的。", "先把基础做完吧。"]
        first = typing_delays(bubbles, companion_id="sui")
        second = typing_delays(bubbles, companion_id="sui")
        assert first == second
        other = typing_delays(bubbles, companion_id="other")
        assert other != first  # companion seed changes the rhythm

    def test_delays_are_bounded_and_positive(self):
        bubbles = ["嗯。", "挺长的泡泡" * 30]
        delays = typing_delays(bubbles, companion_id="sui")
        assert len(delays) == len(bubbles)
        for delay in delays:
            assert 60 <= delay <= 4200

    def test_reply_delays_match_reply_messages(self, tmp_path):
        provider = LongProvider()
        settings = Settings(
            database_path=str(tmp_path / "delays.sqlite"),
            api_token="test-token",
            dialogue_reply_split_enabled=True,
            model_base_url="http://model.invalid",
            model_name="fake",
        )
        app = create_app(settings, model_provider=provider)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            state = attach_desktop(client)
            response = client.post(
                "/v1/companions/sui/dialogue",
                json={
                    "command_id": "delays-1",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "user_message": "这个方案你觉得怎么样",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "completed"
            assert result["reply_messages"], result
            assert len(result["reply_delays"]) == len(result["reply_messages"])
            for delay in result["reply_delays"]:
                assert 60 <= delay <= 4200

    def test_reply_delays_none_when_disabled(self, tmp_path):
        provider = LongProvider()
        settings = Settings(
            database_path=str(tmp_path / "delays-off.sqlite"),
            api_token="test-token",
            model_base_url="http://model.invalid",
            model_name="fake",
            dialogue_reply_split_enabled=False,
        )
        app = create_app(settings, model_provider=provider)
        with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
            state = attach_desktop(client)
            response = client.post(
                "/v1/companions/sui/dialogue",
                json={
                    "command_id": "delays-2",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "user_message": "这个方案你觉得怎么样",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "completed"
            assert result["reply_messages"] is None
            assert result["reply_delays"] is None
