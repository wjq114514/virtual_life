"""Streaming drafts share durable receipts, gates and billing with JSON dialogue."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
import test_emotion
from virtual_life.dialogue import DialogueError
from virtual_life.model_provider import ProviderError
from virtual_life.ports import ModelResult, ModelUsage

runtime = test_emotion.runtime


def body(runtime, command="stream", **changes):
    state = runtime[-1]
    return {
        "command_id": command,
        "node_id": "desk",
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "user_message": "谢谢你陪我读书",
        **changes,
    }


def provider(runtime, action=lambda: None, text="随：我在，陪你。"):
    model = runtime[3]

    def stream(request, on_delta):
        model.calls += 1
        for part in ("随", "：", "我在", "，陪你。"):
            on_delta(part)
            if part == "我在":
                action()
        return ModelResult(text, "fake", ModelUsage(70, 10))

    model.stream = stream
    return model


def sse(response):
    assert response.status_code == 200, response.text
    events = []
    for frame in response.text.split("\n\n"):
        if frame.startswith("event: "):
            kind, raw = frame.split("\ndata: ", 1)
            events.append((kind[7:], json.loads(raw)))
    return events


def test_drafts_arrive_before_commit_and_receipt_uses_normalized_text(runtime):
    _, app, _, _, _ = runtime
    model = provider(runtime)
    events = []

    def receive(kind, data):
        events.append((kind, data))
        if kind == "delta":
            with pytest.raises(DialogueError, match="in progress"):
                app.state.dialogue.get_result("sui", "stream")

    result = app.state.dialogue.send_stream("sui", body(runtime), receive)
    assert [data["text"] for kind, data in events if kind == "delta"] == ["我在", "我在，陪你。"]
    assert [data["sequence"] for kind, data in events if kind == "delta"] == [1, 2]
    generation = next(data for kind, data in events if kind == "snapshot")
    assert generation["emotion"]["context_version"] != result["emotion"]["context_version"]
    assert result["assistant_message"] == "我在，陪你。"
    assert app.state.dialogue.get_result("sui", "stream") == result
    assert app.state.dialogue.send("sui", body(runtime)) == result
    replay = []
    assert (
        app.state.dialogue.send_stream("sui", body(runtime), lambda *e: replay.append(e)) == result
    )
    assert not replay and model.calls == 1
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 80


def test_sse_contract_headers_replay_json_and_auth(runtime):
    client, _, _, _, _ = runtime
    model = provider(runtime)
    path = "/v1/companions/sui/dialogue/stream"
    response = client.post(path, json=body(runtime))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    events = sse(response)
    assert events[0][0] == "status" and events[-1][0] == "result"
    result = events[-1][1]
    assert result["status"] == "completed"
    assert client.get("/v1/companions/sui/dialogue/stream").json() == result
    assert sse(client.post(path, json=body(runtime))) == [("result", result)]
    assert client.post("/v1/companions/sui/dialogue", json=body(runtime)).json() == result
    assert model.calls == 1
    assert client.post(path, json=body(runtime), headers={"Authorization": ""}).status_code == 401
    assert client.post(path, json={**body(runtime), "stream": True}).status_code == 422
    content = client.get("/openapi.json").json()["paths"][
        "/v1/companions/{companion_id}/dialogue/stream"
    ]["post"]["responses"]["200"]["content"]
    assert set(content) == {"text/event-stream"}


def test_json_first_then_stream_replays_without_calling_stream(runtime):
    client, _, _, model, _ = runtime
    result = client.post("/v1/companions/sui/dialogue", json=body(runtime)).json()
    model.stream = lambda *_: pytest.fail("A replay cannot dispatch a model")
    events = sse(client.post("/v1/companions/sui/dialogue/stream", json=body(runtime)))
    assert events == [("result", result)] and model.calls == 1


def test_concurrent_json_and_stream_share_pending_claim(runtime):
    client, app, _, model, _ = runtime
    entered, release = Event(), Event()

    def stream(request, on_delta):
        model.calls += 1
        on_delta("首段")
        entered.set()
        assert release.wait(5)
        return ModelResult("首段", "fake", ModelUsage(70, 10))

    model.stream = stream
    with ThreadPoolExecutor(max_workers=1) as executor:
        work = executor.submit(
            app.state.dialogue.send_stream, "sui", body(runtime), lambda *_: None
        )
        try:
            assert entered.wait(5)
            duplicate = client.post("/v1/companions/sui/dialogue", json=body(runtime))
            assert duplicate.status_code == 409
            assert duplicate.json()["error"]["code"] == "dialogue_in_progress"
            replay = sse(client.post("/v1/companions/sui/dialogue/stream", json=body(runtime)))
            assert replay == [
                (
                    "error",
                    {
                        "command_id": "stream",
                        "companion_id": "sui",
                        "error": {
                            "status_code": 409,
                            "code": "dialogue_in_progress",
                            "message": "Dialogue command is already in progress",
                        },
                    },
                )
            ]
        finally:
            release.set()
        assert work.result(timeout=5)["status"] == "completed"
    assert model.calls == 1


@pytest.mark.parametrize("change", ["profile", "emotion", "lease", "access", "memory"])
def test_each_chunk_checks_context_and_invalid_draft_stays_discarded(runtime, change):
    client, app, clock, _, _ = runtime
    request = body(runtime, user_message="喜欢茶，谢谢你陪我读书")
    memory_id = None
    if change == "access":
        access = client.post(
            "/v1/nodes/access/web",
            json={"browser_id": "browser", "access_id": "access", "bind_node_id": "desk"},
        )
        assert access.status_code == 201
        request["access_id"] = "access"
    if change == "memory":
        source = client.get("/v1/events?limit=500").json()["events"][-1]["event_id"]
        memory = client.post(
            "/v1/companions/sui/memories",
            json={
                "command_id": "tea",
                "kind": "preference",
                "content": "喜欢茶",
                "semantic_key": "drink",
                "source_event_id": source,
                "pinned": True,
            },
        )
        assert memory.status_code == 201, memory.text
        memory_id = memory.json()["memory_id"]

    def change_context():
        if change == "profile":
            profile = client.get("/v1/companions/sui/personality").json()
            response = client.post(
                "/v1/companions/sui/personality",
                json={
                    "command_id": "profile",
                    "expected_revision": profile["revision"],
                    "definition": {**profile["definition"], "description": "新的描述"},
                },
            )
        elif change == "emotion":
            emotion = client.get("/v1/companions/sui/emotion").json()
            response = client.post(
                f"/v1/companions/sui/emotion/experiences/{emotion['source_event_id']}/invalidate",
                json={"command_id": "clarify", "expected_revision": emotion["revision"]},
            )
        elif change == "lease":
            clock.value += timedelta(seconds=10001)
            return
        elif change == "access":
            response = client.delete("/v1/nodes/desk/access/access")
        else:
            response = client.delete(f"/v1/companions/sui/memories/{memory_id}")
        assert response.status_code == 200, response.text

    provider(runtime, change_context)
    events = []
    result = app.state.dialogue.send_stream("sui", request, lambda *e: events.append(e))
    assert result["status"] == "discarded" and result["assistant_message"] is None
    assert result["usage"] == {"input_tokens": 70, "output_tokens": 10}
    assert len([e for e in events if e[0] == "delta"]) == 1
    assert any(e[0] == "status" and e[1]["stage"] == "discarded" for e in events)
    if memory_id:
        assert memory_id in result["preview"]["memory_refs"]
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 80
    assert app.state.dialogue.get_result("sui", request["command_id"]) == result


@pytest.mark.parametrize("known_usage", [True, False])
def test_partial_failure_retains_receipt_and_usage_evidence(runtime, known_usage):
    client, app, _, model, _ = runtime

    def stream(request, on_delta):
        model.calls += 1
        on_delta("临时文字")
        raise ProviderError(
            "model_transport_error",
            "Model transport failed; usage is unknown",
            usage=ModelUsage(70, 10) if known_usage else None,
        )

    model.stream = stream
    events = sse(client.post("/v1/companions/sui/dialogue/stream", json=body(runtime)))
    assert any(kind == "delta" for kind, _ in events)
    assert events[-1][0] == "error" and events[-1][1]["error"]["code"] == "model_transport_error"
    assert client.get("/v1/companions/sui/dialogue/stream").status_code == 502
    budget = app.state.memory.budget_snapshot("sui")
    assert budget["spent_tokens"] == (80 if known_usage else 0)
    assert (budget["reserved_tokens"] > 0) is not known_usage
    assert model.calls == 1


def test_disconnected_callback_does_not_cancel_commit_or_accounting(runtime):
    _, app, _, _, _ = runtime
    model = provider(runtime)
    observed = []

    def disconnected(kind, data):
        observed.append(kind)
        raise OSError("Subscriber disconnected")

    result = app.state.dialogue.send_stream("sui", body(runtime), disconnected)
    assert observed == ["status"] and result["status"] == "completed"
    assert app.state.dialogue.get_result("sui", "stream") == result
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 80
    assert model.calls == 1


def test_complete_only_provider_has_safe_single_draft_fallback(runtime):
    client, _, _, model, _ = runtime
    events = sse(client.post("/v1/companions/sui/dialogue/stream", json=body(runtime)))
    assert [data["text"] for kind, data in events if kind == "delta"] == ["我在，陪你。"]
    assert events[-1][1]["status"] == "completed" and model.calls == 1


def test_mismatching_provider_stream_is_failed_but_usage_is_settled(runtime):
    client, app, _, _, _ = runtime
    model = provider(runtime, text="不同的最终结果")
    events = sse(client.post("/v1/companions/sui/dialogue/stream", json=body(runtime)))
    assert events[-1][0] == "error"
    assert events[-1][1]["error"]["code"] == "model_invalid_response"
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 80
    assert model.calls == 1


def test_long_fine_grained_drafts_are_bounded_and_still_commit_full_reply(runtime):
    client, app, _, model, _ = runtime
    parts = ["好" * 128] * 128
    reply = "".join(parts)

    def stream(request, on_delta):
        model.calls += 1
        for part in parts:
            on_delta(part)
        return ModelResult(reply, "fake", ModelUsage(70, 1024))

    model.stream = stream
    response = client.post("/v1/companions/sui/dialogue/stream", json=body(runtime))
    events = sse(response)
    drafts = [data for kind, data in events if kind == "delta"]
    assert 1 < len(drafts) < len(parts)
    assert [item["sequence"] for item in drafts] == list(range(1, len(drafts) + 1))
    assert drafts[-1]["text"] != reply
    assert len(response.content) < 2 * 1024 * 1024
    assert events[-1][0] == "result" and events[-1][1]["assistant_message"] == reply
    assert app.state.dialogue.get_result("sui", "stream")["status"] == "completed"
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 1094
    assert model.calls == 1


def test_context_is_rechecked_after_snapshot_delivery_before_dispatch(runtime):
    _, app, clock, model, _ = runtime

    def receive(kind, data):
        if kind == "snapshot":
            clock.value += timedelta(seconds=10001)

    with pytest.raises(DialogueError):
        app.state.dialogue.send_stream("sui", body(runtime), receive)
    assert model.calls == 0
    budget = app.state.memory.budget_snapshot("sui")
    assert budget["spent_tokens"] == 0 and budget["reserved_tokens"] == 0
