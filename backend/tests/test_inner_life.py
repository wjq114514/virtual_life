"""Deterministic inner activity, API reads and conservative proactive origin."""

import json
import types
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life import inner_life as inner_life_module
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


def test_overnight_gap_resets_salience_in_daytime(runtime):
    # A >=6h gap that ends in daytime is sleep, not lost interest: the
    # salience should wake at the gentle morning factor instead of the
    # ~1e-6 raw exponential after a full night.
    client, app, clock, _, state = runtime
    send(client, state, "wake-a", "今天聊到这儿，我去睡了")
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    app.state.inner_life.tick()

    # 08:00 local + 26h -> 10:00 next day: daytime, well past the 6h sleep span.
    clock.value += timedelta(hours=26)
    data = client.get("/v1/companions/sui/inner-life").json()
    assert data["salience"] > 0.3


def test_long_gap_at_deep_night_still_decays(runtime):
    # The overnight reset only applies to daytime: still awake deep at night,
    # the old exponential fade keeps quieting a long-unmet impulse.
    client, app, clock, _, state = runtime
    send(client, state, "wake-b", "今天聊到这儿")
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    app.state.inner_life.tick()

    # 08:00 local + 16h -> midnight local: deep night, gap >= 6h.
    clock.value += timedelta(hours=16)
    data = client.get("/v1/companions/sui/inner-life").json()
    assert data["salience"] < 0.05


# ------------------------------------------------------- sleep state ----


def test_inner_life_snapshot_reports_sleep_phase(runtime):
    client, app, clock, provider, state = runtime
    # 08:00 local morning.
    assert client.get("/v1/companions/sui/inner-life").json()["phase"] == "awake"
    # 23:30 local: falling asleep.
    clock.value += timedelta(hours=15, minutes=30)
    assert client.get("/v1/companions/sui/inner-life").json()["phase"] == "drowsy"
    # 01:00 local: core sleep (no strong urge pending).
    clock.value += timedelta(hours=1, minutes=30)
    assert client.get("/v1/companions/sui/inner-life").json()["phase"] == "asleep"


def test_sleep_restores_energy_without_revising_state(runtime):
    client, app, clock, provider, state = runtime
    update = client.post(
        "/v1/companions/sui/personality/state",
        json={
            "command_id": "sleep-state",
            "expected_revision": 0,
            "state": {"mood": "tired", "energy": 0.2, "social_battery": 0.3},
            "ttl_seconds": 86400,
        },
    )
    assert update.status_code == 200, update.text
    clock.value += timedelta(hours=17)  # 01:00 local, asleep
    assert app.state.inner_life.tick() in (0, 1)
    snapshot = client.get("/v1/companions/sui/personality/state").json()
    assert snapshot["state"]["energy"] >= 0.25
    # Maintenance did not bump the revision or mint a new state event.
    assert snapshot["revision"] == 1
    assert app.state.inner_life.tick() in (0, 1)
    after = client.get("/v1/companions/sui/personality/state").json()
    assert after["state"]["energy"] > snapshot["state"]["energy"]


def test_asleep_companion_does_not_send_proactive_output(runtime, monkeypatch):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    enable_proactivity(client)
    assert app.state.inner_life.tick() == 1
    monkeypatch.setattr(app.state.inner_life, "sleep_phase", lambda *a, **k: "asleep")
    assert app.state.proactivity.process_next() is None
    assert provider.requests == []
    assert client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"] == []


def test_asleep_companion_does_not_touch_the_phone(runtime, monkeypatch):
    client, app, clock, provider, state = runtime
    service = app.state.inner_life
    service.policy.action_enabled = True
    service.policy.action_adb_serial = "emu"
    monkeypatch.setattr(
        service,
        "_action_gates",
        lambda *a, **k: {"activity_id": "ina_x", "kind": "curious", "salience": 0.9},
    )
    calls = []
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)
    monkeypatch.setattr(
        service,
        "_execute_action",
        lambda *a, **k: calls.append(a) or {"action": "look", "ok": True},
    )
    monkeypatch.setattr(service, "_remember_action", lambda *a, **k: None)
    # Awake at 08:00 local: the phone is reachable.
    assert service._maybe_action("sui", clock.value) is not None
    assert calls
    # 01:00 local: asleep — no phone.
    clock.value += timedelta(hours=17)
    assert service._maybe_action("sui", clock.value) is None
    assert len(calls) == 1


def test_peek_returns_foreground_app_label(monkeypatch):
    service = inner_life_module.InnerLifeService.__new__(inner_life_module.InnerLifeService)
    service.policy = InnerLifePolicy(
        action_enabled=True, action_adb_serial="emu", action_open_app_whitelist=()
    )
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)
    monkeypatch.setattr(
        inner_life_module, "_foreground_app", lambda serial: "com.google.android.deskclock"
    )
    assert service.peek() == "时钟"


def test_peek_none_when_disabled_offline_or_unknown(monkeypatch):
    service = inner_life_module.InnerLifeService.__new__(inner_life_module.InnerLifeService)
    service.policy = InnerLifePolicy(
        action_enabled=False, action_adb_serial="emu", action_open_app_whitelist=()
    )
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)
    monkeypatch.setattr(inner_life_module, "_foreground_app", lambda serial: "com.android.settings")
    assert service.peek() is None  # actions disabled

    service.policy = InnerLifePolicy(
        action_enabled=True, action_adb_serial="emu", action_open_app_whitelist=()
    )
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: False)
    assert service.peek() is None  # device offline

    service.policy = InnerLifePolicy(
        action_enabled=True, action_adb_serial="", action_open_app_whitelist=()
    )
    assert service.peek() is None  # no serial


def _source_event(client):
    events = client.get("/v1/events?limit=500").json()["events"]
    for event in reversed(events):
        if event.get("companion_id") == "sui":
            return event["event_id"]
    raise AssertionError("no sui event found")


def test_reflective_monologue_anchors_on_old_episode(model_runtime):
    client, app, clock, provider, state = model_runtime
    send(client, state, "one", "谢谢你陪我读书")
    old = (clock.value - timedelta(days=8)).isoformat()
    app.state.memory.write(
        "sui",
        {
            "command_id": "remin-1",
            "kind": "episode",
            "content": "User: 我养了一只猫叫团子。\nAssistant: 替我摸摸它。",
            "source_event_id": _source_event(client),
            "occurred_at": old,
        },
    )
    provider.judge = {
        "kind": "reflective",
        "salience": 0.6,
        "urge": 0.5,
        "confidence": 0.9,
        "rationale": "reflection without content",
    }
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert current["kind"] == "reflective"
    assert "想起一件旧事" in current["content"]
    assert "团子" in current["content"]


def test_reminisce_skips_recent_or_missing_episodes(model_runtime):
    client, app, clock, provider, state = model_runtime
    send(client, state, "one", "谢谢你陪我读书")
    app.state.memory.write(
        "sui",
        {
            "command_id": "remin-2",
            "kind": "episode",
            "content": "User: 我养了一只猫叫团子。\nAssistant: 替我摸摸它。",
            "source_event_id": _source_event(client),
        },
    )
    provider.judge = {
        "kind": "reflective",
        "salience": 0.6,
        "urge": 0.5,
        "confidence": 0.9,
        "rationale": "reflection without content",
    }
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    # Episode is too recent: the generic reflective template is used instead.
    assert "想起一件旧事" not in current["content"]
    assert current["content"].strip()


def _write_curiosity(app, companion, labels, clock):
    with app.state.inner_life.store.transaction(write=True) as connection:
        for label in labels:
            connection.execute(
                "INSERT INTO curiosity_observations (companion_id, app_label, seen_at)"
                " VALUES (?, ?, ?)",
                (companion, label, clock.value.isoformat()),
            )


def test_curious_monologue_learns_from_repeated_look_observations(model_runtime):
    client, app, clock, provider, state = model_runtime
    provider.judge = {
        "kind": "curious",
        "salience": 0.6,
        "urge": 0.5,
        "confidence": 0.9,
        "rationale": "no content offered",
    }
    _write_curiosity(app, "sui", ["时钟", "时钟", "时钟"], clock)
    send(client, state, "one", "谢谢")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert "最近几次看过去你都在用时钟" in current["content"]


def test_curious_monologue_stays_generic_below_repeat_threshold(model_runtime):
    client, app, clock, provider, state = model_runtime
    provider.judge = {
        "kind": "curious",
        "salience": 0.6,
        "urge": 0.5,
        "confidence": 0.9,
        "rationale": "no content offered",
    }
    _write_curiosity(app, "sui", ["时钟", "时钟"], clock)
    send(client, state, "one", "谢谢")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    assert app.state.inner_life.tick() == 1
    current = client.get("/v1/companions/sui/inner-life").json()["current"]
    assert "最近几次看过去" not in current["content"]


def test_look_action_records_curiosity_observation(monkeypatch, tmp_path):
    clock = Clock()
    service = inner_life_module.InnerLifeService.__new__(inner_life_module.InnerLifeService)
    service.policy = InnerLifePolicy(
        action_enabled=True, action_adb_serial="emu", action_open_app_whitelist=()
    )
    # Minimal store + coordinator wiring so the write transaction works.
    from virtual_life.storage import SQLiteStore

    service.store = SQLiteStore(str(tmp_path / "look.sqlite"))
    with service.store.transaction(write=True) as connection:
        for statement in inner_life_module._SCHEMA:
            connection.execute(statement)
    with service.store.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO companions (companion_id, name, revision, epoch,"
            " activity_json, interaction_mode, context_json, memory_refs_json,"
            " created_at, updated_at)"
            " VALUES ('sui', 'Sui', 0, 0, '{}', 'chat', '{}', '[]', ?, ?)",
            (clock.value.isoformat(), clock.value.isoformat()),
        )
    service.personality = types.SimpleNamespace(_now=lambda: clock.value)
    service.coordinator = types.SimpleNamespace(
        _emit=lambda connection, now, etype, payload, companion_id: {
            "event_id": "evt_look_1",
        }
    )
    service.memory = None
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)
    monkeypatch.setattr(
        inner_life_module,
        "_foreground_app",
        lambda serial: "com.google.android.deskclock",
    )
    monkeypatch.setattr(
        service,
        "_execute_action",
        lambda action, target, activity: {
            "action": "look",
            "outcome": "ok",
            "foreground_app": "com.google.android.deskclock",
        },
    )
    monkeypatch.setattr(service, "_remember_action", lambda *a, **k: None)
    # Seed an active curious activity so _action_gates passes.
    with service.store.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO inner_activities (activity_id, companion_id, kind, trigger,"
            " content, salience, urge, context_version, generator, status, created_at,"
            " expires_at) VALUES ('ina_test', 'sui', 'curious', 'curiosity', '好奇',"
            " 0.8, 0.5, 'v1', 'rules_v1', 'active', ?, ?)",
            (clock.value.isoformat(), (clock.value + timedelta(hours=1)).isoformat()),
        )
    assert service._maybe_action("sui", clock.value) is not None
    with service.store.transaction(write=True) as connection:
        row = connection.execute(
            "SELECT app_label FROM curiosity_observations WHERE companion_id='sui'"
        ).fetchone()
    assert row is not None and row["app_label"] == "时钟"
