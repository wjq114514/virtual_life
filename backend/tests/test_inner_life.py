"""Deterministic inner activity, API reads and conservative proactive origin."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.inner_life import InnerLifePolicy
from virtual_life.memory import MemoryPolicy
from virtual_life.model_provider import ProviderError
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class Provider:
    def __init__(self):
        self.requests = []
        self.judge = {
            "kind": "concerned",
            "salience": 0.6,
            "urge": 0.5,
            "confidence": 0.9,
            "rationale": "对话与低唤醒状态显示她更想安静地在意，而不是追问。",
            "content": "刚才那阵安静之后，我有点想确认你还好，但不敢打扰你。",
        }

    def complete(self, request):
        self.requests.append(request)
        if request.purpose == "inner_life":
            if getattr(self, "fail_inner", False):
                raise ProviderError("provider_failed", "judge unavailable")
            return ModelResult(
                json.dumps(self.judge),
                "fake",
                ModelUsage(120, 30),
                reasoning=getattr(self, "inner_reasoning", ""),
            )
        return ModelResult("随：我在，慢慢来。", "fake", ModelUsage(80, 12))


def build_app(tmp_path, policy=None):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "inner.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        model_provider=provider,
        inner_life_policy=policy,
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.dialogue,
        app.state.memory,
    ):
        service.clock = clock
    return app, clock, provider


def _open_runtime(tmp_path, policy):
    app, clock, provider = build_app(tmp_path, policy)
    client = TestClient(app, headers={"Authorization": "Bearer test"})
    state = attach_desktop(client)
    app.state.coordinator.register_node(
        {
            "node_id": "desk",
            "label": "desk",
            "kind": "desktop",
            "capabilities": ["text.display", "attention.observe"],
        }
    )
    state = app.state.coordinator.patch_context(
        "sui",
        {
            "command_id": "chat",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "interaction_mode": "chat",
        },
    )
    app.state.personality.update(
        "sui",
        {
            "command_id": "initiative",
            "expected_revision": app.state.personality.get("sui")["revision"],
            "definition": {"traits": {"initiative": 0.8}},
        },
    )
    return client, app, clock, provider, state


@pytest.fixture
def runtime(tmp_path):
    policy = InnerLifePolicy(
        min_activity_gap_seconds=30,
        activity_ttl_seconds=600,
        min_evidence_items=1,
        proactive_urge_threshold=0.2,
        proactive_min_idle_seconds=60,
        idle_saturation_seconds=120,
        max_history=10,
    )
    client, app, clock, provider, state = _open_runtime(tmp_path, policy)
    yield client, app, clock, provider, state
    client.close()


@pytest.fixture
def model_runtime(tmp_path):
    policy = InnerLifePolicy(
        min_activity_gap_seconds=30,
        activity_ttl_seconds=600,
        min_evidence_items=1,
        proactive_urge_threshold=0.2,
        proactive_min_idle_seconds=60,
        idle_saturation_seconds=120,
        max_history=10,
        model_assisted=True,
        model_min_interval_seconds=0,
        model_daily_limit=24,
        model_min_confidence=0.55,
        model_weight=0.5,
    )
    client, app, clock, provider, state = _open_runtime(tmp_path, policy)
    yield client, app, clock, provider, state
    client.close()


def send(client, state, command, message):
    response = client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": command,
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": message,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def observe(client, clock, sequence=1, **changes):
    sample = {
        "session_id": "session",
        "sequence": sequence,
        "sampled_at": clock.value.isoformat(),
        "activity": "docs",
        "focus": "unfocused",
        "availability": "available",
        "user_present": True,
        "locked": False,
        "meeting": False,
        "screen_sharing": False,
        "boundary": "none",
        "boundary_id": None,
        **changes,
    }
    response = client.post(
        "/v1/nodes/desk/attention",
        json={"command_id": f"attention-{sequence}", "sample": sample},
    )
    assert response.status_code == 200, response.text


def enable_proactivity(client):
    current = client.get("/v1/companions/sui/proactivity").json()
    response = client.post(
        "/v1/companions/sui/proactivity",
        json={
            "command_id": "enable",
            "expected_revision": current["revision"],
            "policy": {**current["policy"], "enabled": True},
        },
    )
    assert response.status_code == 200, response.text


def test_snapshot_is_read_only_and_needs_history(runtime):
    client, app, clock, provider, state = runtime
    before = client.get("/v1/companions/sui/emotion").json()
    assert app.state.inner_life.tick() == 0
    snapshot = client.get("/v1/companions/sui/inner-life").json()
    assert snapshot["enabled"] is True
    assert snapshot["model_assisted"] is False
    assert snapshot["current"] is None
    assert snapshot["model_calls"] == 0
    assert provider.requests == []
    assert client.get("/v1/companions/sui/inner-life/activities").json()["items"] == []
    assert client.get("/v1/companions/sui/emotion").json() == before


def test_tick_records_one_deterministic_activity_without_model_or_emotion(runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    before = client.get("/v1/companions/sui/emotion").json()
    assert app.state.inner_life.tick() == 1
    assert app.state.inner_life.tick() == 0
    snapshot = client.get("/v1/companions/sui/inner-life").json()
    current = snapshot["current"]
    assert current["status"] == "active"
    assert current["kind"] in {
        "restless",
        "curious",
        "concerned",
        "reflective",
        "content",
        "resting",
    }
    assert 0 <= current["urge"] <= 1
    assert snapshot["activities"][0] == current
    assert provider.requests == []
    assert client.get("/v1/companions/sui/emotion").json() == before
    page = client.get("/v1/companions/sui/inner-life/activities?limit=1").json()
    assert page["items"] == [current]
    assert page["has_more"] is False
    assert page["next_sequence"] == current["sequence"]
    events = client.get("/v1/events?limit=500").json()["events"]
    assert any(event["type"] == "companion.inner_activity" for event in events)


def test_inner_activity_can_drive_proactive_output(runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    enable_proactivity(client)
    assert app.state.inner_life.tick() == 1
    activity = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert activity["urge"] >= 0.2
    access = client.post(
        "/v1/nodes/access/web",
        json={
            "browser_id": "inner-browser",
            "access_id": "inner-access",
            "bind_node_id": "desk",
        },
    )
    assert access.status_code == 201, access.text
    query = {"node_id": "desk", "access_id": "inner-access", "epoch": state["epoch"]}
    baseline = client.get("/v1/companions/sui/proactivity/output", params=query)
    assert baseline.status_code == 200, baseline.text
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation"
    assert len(provider.requests) == 0
    decisions = client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"]
    assert decisions[0]["origin"] == "inner"
    assert decisions[0]["source_activity_id"] == activity["activity_id"]
    # DSH-owned path: commit the externally generated reply through the route.
    command_id = decisions[0]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "我在，慢慢来。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["status"] == "completed"
    receipt = client.get(f"/v1/companions/sui/dialogue/{command_id}").json()
    assert receipt["assistant_message"] == "我在，慢慢来。"
    output = client.get(
        "/v1/companions/sui/proactivity/output",
        params={**query, "after_sequence": baseline.json()["next_sequence"]},
    )
    assert output.status_code == 200, output.text
    assert [item["assistant_message"] for item in output.json()["items"]] == ["我在，慢慢来。"]
    assert app.state.inner_life.tick() == 0


def test_focused_attention_does_not_enqueue_inner_candidate(runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock, focus="focused")
    enable_proactivity(client)
    assert app.state.inner_life.tick() == 1
    assert app.state.proactivity.process_next() is None
    assert provider.requests == []
    assert client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"] == []


def test_disabled_policy_hides_existing_activity(runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    app.state.inner_life.policy = app.state.inner_life.policy.model_copy(update={"enabled": False})
    snapshot = client.get("/v1/companions/sui/inner-life").json()
    assert snapshot["current"] is None
    assert snapshot["activities"] == []
    assert app.state.inner_life.tick() == 0


def test_expired_activity_is_not_current(runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    clock.value += timedelta(minutes=11)
    snapshot = client.get("/v1/companions/sui/inner-life").json()
    assert snapshot["current"] is None
    assert snapshot["activities"] == []


def test_model_assisted_judge_records_bounded_audit(model_runtime):
    client, app, clock, provider, state = model_runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    assert len(provider.requests) == 1
    capabilities = client.get("/v1/capabilities").json()["personality"]["inner_life"]
    assert capabilities["generators"] == ["rules_v1", "model_v1"]
    assert capabilities["model_assisted"] is True
    assert capabilities["model_available"] is True
    request = provider.requests[0]
    assert request.purpose == "inner_life"
    assert "judge_companion_inner_feeling" in request.context_text
    assert "recent_dialogue" in request.context_text
    snapshot = client.get("/v1/companions/sui/inner-life").json()
    current = snapshot["current"]
    assert snapshot["model_assisted"] is True
    assert snapshot["model_calls"] == 1
    assert current["generator"] == "model_v1"
    assert current["kind"] == "concerned"
    assert current["model"]["confidence"] == 0.9
    assert current["model"]["input_tokens"] == 120
    assert current["model"]["output_tokens"] == 30
    assert current["model"]["rationale"].startswith("对话与低唤醒")
    assert current["content"] == provider.judge["content"]
    assert current["thinking"] is None
    assert client.get("/v1/companions/sui/inner-life/activities").json()["items"][0] == current
    client.post("/v1/companions/sui/personality/preview", json={"user_message": "继续吧"})


def test_model_assisted_judge_records_reasoning(model_runtime):
    client, app, clock, provider, state = model_runtime
    provider.inner_reasoning = "(inner voice) 我感觉到一点担心，但不想打扰。"
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "model_v1"
    assert current["thinking"] == "(inner voice) 我感觉到一点担心，但不想打扰。"
    preview = client.post(
        "/v1/companions/sui/personality/preview", json={"user_message": "继续吧"}
    ).json()
    assert "INNER_ACTIVITY_DATA" in preview["context_text"]
    assert current["model"]["rationale"] not in preview["context_text"]
    budget = app.state.memory.budget_snapshot("sui")
    assert budget["spent_tokens"] > 0


def test_model_judge_invalid_output_falls_back_to_rules(model_runtime):
    client, app, clock, provider, state = model_runtime
    provider.judge = {
        "kind": "angry",
        "salience": 1.0,
        "urge": 1.0,
        "confidence": 1.0,
        "rationale": "invalid",
    }
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    assert len(provider.requests) == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "rules_v1"
    assert current["model"] is None
    assert current["kind"] in {
        "restless",
        "curious",
        "concerned",
        "reflective",
        "content",
        "resting",
    }
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] > 0


def test_model_judge_low_confidence_falls_back_to_rules(model_runtime):
    client, app, clock, provider, state = model_runtime
    provider.judge = {
        "kind": "content",
        "salience": 0.9,
        "urge": 0.9,
        "confidence": 0.1,
        "rationale": "too uncertain",
    }
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "rules_v1"
    assert current["model"] is None


def test_model_judge_without_content_falls_back_to_template(model_runtime):
    client, app, clock, provider, state = model_runtime
    provider.judge = {
        "kind": "concerned",
        "salience": 0.6,
        "urge": 0.5,
        "confidence": 0.9,
        "rationale": "no content offered",
    }
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "model_v1"
    # Model declined content; the template fallback kicked in.
    assert current["content"] != provider.judge["rationale"]
    assert isinstance(current["content"], str) and current["content"].strip()


def test_model_interval_keeps_local_rules_between_calls(model_runtime):
    client, app, clock, provider, state = model_runtime
    app.state.inner_life.policy = app.state.inner_life.policy.model_copy(
        update={"model_min_interval_seconds": 3_600}
    )
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    assert len(provider.requests) == 1
    assert client.get("/v1/companions/sui/inner-life").json()["current"]["generator"] == "model_v1"
    clock.value += timedelta(minutes=10)
    assert app.state.inner_life.tick() == 1
    assert len(provider.requests) == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "rules_v1"
    assert current["model"] is None


def test_model_daily_limit_keeps_local_rules_after_first_call(model_runtime):
    client, app, clock, provider, state = model_runtime
    app.state.inner_life.policy = app.state.inner_life.policy.model_copy(
        update={"model_min_interval_seconds": 0, "model_daily_limit": 1}
    )
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    assert len(provider.requests) == 1
    clock.value += timedelta(minutes=10)
    assert app.state.inner_life.tick() == 1
    assert len(provider.requests) == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "rules_v1"
    assert current["model"] is None


def test_model_judge_provider_failure_falls_back_to_rules(model_runtime):
    client, app, clock, provider, state = model_runtime
    provider.fail_inner = True
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    assert len(provider.requests) == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "rules_v1"
    assert current["model"] is None


def test_model_judge_budget_exhaustion_skips_provider(model_runtime):
    client, app, clock, provider, state = model_runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    app.state.memory.policy = MemoryPolicy(daily_model_token_limit=0)
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    assert provider.requests == []
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["generator"] == "rules_v1"
    assert current["model"] is None


def test_model_judge_is_not_called_when_inner_life_disabled(model_runtime):
    client, app, clock, provider, state = model_runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    app.state.inner_life.policy = app.state.inner_life.policy.model_copy(update={"enabled": False})
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 0
    assert provider.requests == []


def test_settings_file_and_environment_overrides(tmp_path, monkeypatch):
    config = tmp_path / "virtual-life.toml"
    config.write_text(
        "[runtime]\ndatabase_path = 'inner.sqlite'\napi_token = 'x'\n\n"
        "[inner_life]\npoll_seconds = 3.0\nmin_activity_gap_seconds = 120\n"
        "proactive_urge_threshold = 0.5\n",
        encoding="utf-8",
    )
    settings = Settings.load(config)
    assert settings.inner_life_poll_seconds == 3.0
    assert settings.inner_life_policy.min_activity_gap_seconds == 120
    assert settings.inner_life_policy.proactive_urge_threshold == 0.5
    monkeypatch.setenv("VIRTUAL_LIFE_INNER_LIFE_MIN_EVIDENCE_ITEMS", "4")
    monkeypatch.setenv("VIRTUAL_LIFE_INNER_LIFE_MODEL_ASSISTED", "true")
    monkeypatch.setenv("VIRTUAL_LIFE_INNER_LIFE_MODEL_WEIGHT", "0.25")
    monkeypatch.setenv("VIRTUAL_LIFE_INNER_LIFE_POLL_SECONDS", "7.5")
    overridden = Settings.load(config)
    assert overridden.inner_life_policy.min_evidence_items == 4
    assert overridden.inner_life_policy.model_assisted is True
    assert overridden.inner_life_policy.model_weight == 0.25
    assert overridden.inner_life_poll_seconds == 7.5


def test_openapi_and_capabilities_expose_inner_life(runtime):
    client, _, _, _, _ = runtime
    document = client.get("/openapi.json").json()
    assert "/v1/companions/{companion_id}/inner-life" in document["paths"]
    assert "/v1/companions/{companion_id}/inner-life/activities" in document["paths"]
    capabilities = client.get("/v1/capabilities").json()
    inner = capabilities["personality"]["inner_life"]
    assert inner["generators"] == ["rules_v1"]
    assert inner["model_assisted"] is False
    assert capabilities["proactivity"]["origins"] == ["attention_boundary", "inner_activity"]
