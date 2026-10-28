"""Read-only L2 slow variables: aggregation, decay, participation and settings."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings
from virtual_life.slow_variables import SlowVariablePolicy, SlowVariableService


class Provider:
    def __init__(self):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelResult("我在，陪你。", "fake", ModelUsage(70, 10))


def build_app(tmp_path, policy=None):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "slow.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        model_provider=provider,
        slow_variable_policy=policy,
    )
    for service in (app.state.coordinator, app.state.personality, app.state.dialogue):
        service.clock = clock
    return app, clock, provider


@pytest.fixture
def runtime(tmp_path):
    app, clock, provider = build_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        yield client, app, clock, provider, state


def send(client, state, command, message):
    return client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": command,
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": message,
        },
    )


def slow(client):
    response = client.get("/v1/companions/sui/slow-variables")
    assert response.status_code == 200, response.text
    return response.json()


def preview(client, message="继续吧"):
    response = client.post("/v1/companions/sui/personality/preview", json={"user_message": message})
    assert response.status_code == 200, response.text
    return response.json()


def test_disabled_policy_is_neutral(tmp_path):
    app, _, _ = build_app(tmp_path, SlowVariablePolicy(enabled=False))
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        client.post("/v1/companions", json={"companion_id": "sui", "name": "Sui"})
        snapshot = slow(client)
        assert snapshot["enabled"] is False
        assert snapshot["sample_size"] == 0
        assert snapshot["engagement"] == 0.5
        assert snapshot["tone_hint"] == "neutral"
        assert snapshot["model_calls"] == 0


def test_min_evidence_keeps_engagement_neutral(runtime):
    client, _, _, _, state = runtime
    assert send(client, state, "one", "谢谢你陪我读书").status_code == 200
    snapshot = slow(client)
    assert snapshot["sample_size"] == 1
    assert snapshot["tone_hint"] == "neutral"
    assert snapshot["engagement"] == 0.5


def test_positive_evidence_is_warm_and_raises_engagement(tmp_path):
    app, _, _ = build_app(tmp_path, SlowVariablePolicy(min_evidence_items=1))
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "one", "谢谢你陪我读书").status_code == 200
        snapshot = slow(client)
        assert snapshot["sample_size"] == 1
        assert snapshot["valence_mean"] > 0.15
        assert snapshot["tone_hint"] == "warm"
        assert snapshot["engagement"] > 0.5
        assert snapshot["valence_inertia"] == 1.0
        result = preview(client)
        assert "SLOW_VARIABLES_DATA" in result["context_text"]
        assert result["slow_variables"]["engagement"] == snapshot["engagement"]
        # Positive evidence yields a cheerful mood, which adds one sentence on
        # top of the engagement-scaled style baseline.
        assert result["emotion"]["effective_state"]["mood"] == "cheerful"
        assert result["participation"]["tone"] == "warm"
        base = min(8, max(1, round(2 * (0.5 + snapshot["engagement"]))))
        expected = min(8, max(1, base + 1))
        assert result["participation"]["max_reply_sentences"] == expected


def test_negative_evidence_is_cautious(tmp_path):
    app, _, _ = build_app(tmp_path, SlowVariablePolicy(min_evidence_items=1))
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "one", "我今天很难过").status_code == 200
        snapshot = slow(client)
        assert snapshot["valence_mean"] < -0.05
        assert snapshot["tone_hint"] == "cautious"
        assert snapshot["engagement"] < 0.5


def test_consistent_valence_has_higher_inertia(tmp_path):
    consistent_app, _, _ = build_app(tmp_path / "a", SlowVariablePolicy(min_evidence_items=1))
    mixed_app, _, _ = build_app(tmp_path / "b", SlowVariablePolicy(min_evidence_items=1))
    for app, messages in (
        (consistent_app, ["谢谢你陪我读书", "谢谢"]),
        (mixed_app, ["谢谢你陪我读书", "我今天很难过"]),
    ):
        with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
            state = attach_desktop(client)
            for index, message in enumerate(messages):
                assert send(client, state, f"t{index}", message).status_code == 200
            snapshot = slow(client)
            if app is consistent_app:
                consistent = snapshot["valence_inertia"]
            else:
                mixed = snapshot["valence_inertia"]
    assert consistent == 1.0
    assert mixed < consistent


def test_relationship_memory_warms_without_penalizing_engagement(runtime):
    client, _, _, _, state = runtime
    assert send(client, state, "one", "谢谢你陪我读书").status_code == 200
    events = client.get("/v1/events?limit=200").json()["events"]
    source = next(event["event_id"] for event in events if event["companion_id"] == "sui")
    created = client.post(
        "/v1/companions/sui/memories",
        json={
            "command_id": "rel",
            "kind": "relationship",
            "content": "我们是一起读书的朋友",
            "source_event_id": source,
        },
    )
    assert created.status_code == 201, created.text
    snapshot = slow(client)
    assert snapshot["relationship_warmth"] > 0.2
    # Only one experience exists, so participation still stays neutral.
    assert snapshot["engagement"] == 0.5


def test_read_is_deterministic_and_writes_nothing(runtime):
    client, app, _, _, state = runtime
    assert send(client, state, "one", "谢谢你陪我读书").status_code == 200
    first = slow(client)
    revision = app.state.emotion.get("sui")["revision"]
    experiences = client.get("/v1/companions/sui/emotion/experiences").json()
    assert slow(client) == first
    assert app.state.emotion.get("sui")["revision"] == revision
    assert client.get("/v1/companions/sui/emotion/experiences").json() == experiences
    # A fixed clock keeps the snapshot stable; elapsed time only changes it.
    assert (
        SlowVariableService(app.state.personality, app.state.emotion).get("sui")["engagement"]
        == first["engagement"]
    )


def test_unknown_companion_is_rejected(runtime):
    client, _, _, _, _ = runtime
    assert client.get("/v1/companions/missing/slow-variables").status_code == 404


def test_settings_file_and_environment_overrides(tmp_path, monkeypatch):
    config = tmp_path / "virtual-life.toml"
    config.write_text(
        "[runtime]\ndatabase_path = 'slow.sqlite'\napi_token = 'x'\n\n"
        "[personality.slow_variables]\nmin_evidence_items = 5\nhalf_life_seconds = 3600\n",
        encoding="utf-8",
    )
    settings = Settings.load(config)
    assert settings.slow_variable_policy.min_evidence_items == 5
    assert settings.slow_variable_policy.half_life_seconds == 3600
    monkeypatch.setenv("VIRTUAL_LIFE_PERSONALITY_SLOW_VARIABLES_MIN_EVIDENCE_ITEMS", "9")
    overridden = Settings.load(config)
    assert overridden.slow_variable_policy.min_evidence_items == 9


def test_old_evidence_decays_and_recent_evidence_dominates(tmp_path):
    policy = SlowVariablePolicy(
        min_evidence_items=1, half_life_seconds=3600, window_seconds=86400 * 400
    )
    app, _, _ = build_app(tmp_path, policy)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "old", "我今天很难过").status_code == 200
        clock = app.state.personality.clock
        clock.value = clock.value + timedelta(seconds=10 * 3600)
        assert send(client, state, "recent", "谢谢你陪我读书").status_code == 200
        snapshot = slow(client)
        # Ten half-lives leave the early concern with almost no weight, so the
        # recent joy dominates instead of averaging to a neutral midpoint.
        assert snapshot["valence_mean"] > 0.15
