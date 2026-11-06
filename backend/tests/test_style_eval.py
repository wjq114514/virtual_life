"""Style self-evaluation loop: metrics, verdict, prompt injection."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_inner_life import Provider, send
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.settings import Settings


class LongProvider(Provider):
    def complete(self, request):
        self.requests.append(request)
        if request.purpose == "inner_life":
            return super().complete(request)
        from virtual_life.ports import ModelResult, ModelUsage

        return ModelResult(
            "嗯，看到了。那一栏看起来挺简洁的，这个方案其实挺清楚的，我们按这个来就好，"
            "先把基础部分做完，后面再慢慢调，调的时候如果哪里不顺手随时和我说，"
            "我们可以换个思路再试一遍，不着急今天就把全部弄完。",
            "fake",
            ModelUsage(80, 20),
        )


def build_app(tmp_path: Path, clock: Clock, provider: Provider):
    app = create_app(
        Settings(
            database_path=str(tmp_path / "style-eval.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        model_provider=provider,
    )
    for service in (app.state.coordinator, app.state.personality, app.state.dialogue):
        service.clock = clock
    app.state.style_eval.memory.clock = clock
    return app


def _sync_state(client, state):
    fresh = client.get("/v1/companions/sui").json()
    return {**state, "revision": fresh["revision"]}


def test_style_eval_records_metrics_and_verdict(tmp_path):
    clock, provider = Clock(), LongProvider()
    app = build_app(tmp_path, clock, provider)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        for index in range(5):
            result = send(client, state, f"msg-{index}", "你觉得这个方案怎么样")
            assert result["status"] == "completed", result
            state = _sync_state(client, state)
        evaluation = app.state.style_eval.process("sui")
        assert evaluation is not None
        assert evaluation["sample_count"] == 5
        metrics = json.loads(evaluation["metrics_json"])
        assert metrics["avg_chars"] > 40  # the long canned reply
        assert metrics["long_reply_ratio"] == 1.0
        assert "偏长" in evaluation["verdict"]
        # no new replies since the recorded sequence -> no second row
        assert app.state.style_eval.process("sui") is None


def test_style_eval_note_injected_into_prompt(tmp_path):
    clock, provider = Clock(), LongProvider()
    app = build_app(tmp_path, clock, provider)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        for index in range(4):
            send(client, state, f"msg-{index}", "你觉得这个方案怎么样")
            state = _sync_state(client, state)
        assert app.state.style_eval.process("sui") is not None
        # the next turn's prompt carries the style feedback aside
        send(client, state, "msg-style", "再说一次")
        context_text = provider.requests[-1].context_text
        assert "风格反馈" in context_text
        assert "偏长" in context_text


def test_style_eval_skips_below_min_sample(tmp_path):
    clock, provider = Clock(), LongProvider()
    app = build_app(tmp_path, clock, provider)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        send(client, state, "only-one", "你觉得这个方案怎么样")
        assert app.state.style_eval.process("sui") is None
