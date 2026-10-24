"""Time recovery, one-event resonance, evidence invalidation and output fencing."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.emotion import EmotionPolicy, EmotionService, appraise
from virtual_life.model_provider import ProviderError
from virtual_life.personality import PersonalityDefinition
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class Provider:
    def __init__(self):
        self.calls = 0
        self.action = lambda: None
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        self.action()
        return ModelResult("我在，陪你。", "fake", ModelUsage(70, 10))


@pytest.fixture
def runtime(tmp_path):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "affect.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            lease_ttl_seconds=10000,
            node_ttl_seconds=10000,
        ),
        model_provider=provider,
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.memory,
        app.state.dialogue,
    ):
        service.clock = clock
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        yield client, app, clock, provider, state


def send(runtime, command="turn", message="谢谢你陪我读书", **kwargs):
    client, _, _, _, state = runtime
    return client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": command,
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": message,
            **kwargs,
        },
    )


def emotion(client):
    response = client.get("/v1/companions/sui/emotion")
    assert response.status_code == 200, response.text
    return response.json()


def experiences(client):
    return client.get("/v1/companions/sui/emotion/experiences").json()["items"]


def test_recovery_restart_and_read_only_preview(runtime):
    client, app, clock, provider, _ = runtime
    result = send(runtime).json()
    assert result["status"] == "completed"
    assert result["preview"]["emotion"]["reaction"] == "joy"
    assert "emotion" in provider.requests[0].context_text
    first = emotion(client)
    budget = app.state.memory.budget_snapshot("sui")
    for _ in range(3):
        preview = client.post(
            "/v1/companions/sui/personality/preview", json={"user_message": "谢谢你"}
        )
        assert preview.status_code == 200, preview.text
        assert emotion(client) == first
    assert len(experiences(client)) == 1
    assert send(runtime).json() == result
    assert provider.calls == 1
    assert app.state.memory.budget_snapshot("sui") == budget
    clock.value += timedelta(seconds=first["half_life_seconds"])
    later = emotion(client)
    for key in ("valence", "arousal", "control"):
        assert later["state"][key] - later["baseline"][key] == pytest.approx(
            (first["state"][key] - first["baseline"][key]) / 2, abs=1e-6
        )
    assert later["context_version"] == first["context_version"]
    assert later["revision"] == first["revision"]
    restarted = EmotionService(app.state.personality, app.state.memory)
    assert restarted.get("sui") == later
    assert later["effective_state"]["energy"] > first["effective_state"]["energy"]


def test_natural_recovery_during_generation_does_not_discard(runtime):
    client, _, clock, provider, _ = runtime
    provider.action = lambda: setattr(clock, "value", clock.value + timedelta(seconds=5))
    response = send(runtime)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert emotion(client)["reaction"] == "joy"


@pytest.mark.parametrize(
    "message", ["我今天不难过", "他说：我很难过", "谢谢你？", "我不想说谢谢你", "她今天很累"]
)
def test_rules_do_not_promote_negated_or_quoted_cues(message):
    result = appraise(message, PersonalityDefinition().traits.model_dump())
    assert result["reaction"] == "neutral" and result["confidence"] == 0


def test_quiet_and_empathy_are_distinct_from_user_mood(runtime):
    client, _, _, _, _ = runtime
    send(runtime, "quiet", "先安静陪我读一会儿")
    assert emotion(client)["reaction"] == "quiet"
    preview = client.post(
        "/v1/companions/sui/personality/preview", json={"user_message": "继续吧"}
    ).json()
    assert preview["participation"]["max_reply_sentences"] == 1
    send(runtime, "distress", "我今天很难过")
    assert experiences(client)[1]["reaction"] == "concern"
    assert experiences(client)[1]["delta"]["valence"] > -0.1
    assert emotion(client)["reaction"] == "concern"
    assert emotion(client)["quiet_requested"]


def test_manual_state_ttl_compatibility(runtime):
    client, _, clock, _, _ = runtime
    old = client.post(
        "/v1/companions/sui/personality/state",
        json={
            "command_id": "manual",
            "expected_revision": 0,
            "state": {"mood": "tired", "energy": 0.3, "social_battery": 0.2},
            "ttl_seconds": 30,
        },
    ).json()
    result = send(runtime).json()
    assert result["emotion"]["effective_state"]["mood"] == "tired"
    assert client.get("/v1/companions/sui/personality/state").json() == old
    clock.value += timedelta(seconds=31)
    legacy = client.get("/v1/companions/sui/personality/state").json()
    assert legacy["expired"] and legacy["state"]["mood"] == "calm"
    assert emotion(client)["effective_state"]["mood"] == "cheerful"


def seed_resonance(runtime):
    client, app, clock, _, _ = runtime
    initial = send(runtime, "happy").json()
    records = []
    for command in ("reading", "reading-copy"):
        records.append(
            app.state.memory.write(
                "sui",
                {
                    "command_id": command,
                    "kind": "episode",
                    "content": "一起阅读夜航船",
                    "source_event_id": initial["source_event_id"],
                    "pinned": True,
                },
            )
        )
    clock.value += timedelta(seconds=1200)
    result = send(runtime, "recall", "夜航船").json()
    assert result["status"] == "completed"
    item = experiences(client)[-1]
    assert item["resonance_valid"]
    assert item["resonance_event_ids"] == [initial["accepted_event_id"]]
    assert item["resonance_delta"]["valence"] > 0
    assert item["resonance_delta"]["energy"] == 0
    assert item["resonance_delta"]["valence"] == pytest.approx(
        experiences(client)[0]["delta"]["valence"] * 0.2
    )
    return initial, records, result, item


@pytest.mark.parametrize("change", ["metadata", "delete", "invalidate"])
def test_resonance_deduplicates_evidence_and_retracts_invalid_sources(runtime, change):
    client, app, _, provider, _ = runtime
    initial, records, result, item = seed_resonance(runtime)
    first = emotion(client)
    assert send(runtime, "recall", "夜航船").json() == result
    assert emotion(client) == first
    assert provider.calls == 2
    if change == "metadata":
        app.state.memory.update_metadata(
            "sui",
            records[0]["memory_id"],
            {
                "command_id": "edit",
                "expected_revision": item["memory_versions"][0]["revision"],
                "importance": 0.2,
            },
        )
    elif change == "delete":
        app.state.memory.delete("sui", records[0]["memory_id"])
    else:
        response = client.post(
            f"/v1/companions/sui/emotion/experiences/{initial['accepted_event_id']}/invalidate",
            json={"command_id": "clarify", "expected_revision": first["revision"]},
        )
        assert response.status_code == 200, response.text
    current = emotion(client)
    assert current["state"]["valence"] < first["state"]["valence"]
    assert current["context_version"] != first["context_version"]
    assert not experiences(client)[-1]["resonance_valid"]
    if change == "delete":
        assert experiences(client)[0]["status"] == "forgotten"
        assert experiences(client)[0]["confidence"] == 0
        assert experiences(client)[0]["delta"]["valence"] == 0
    elif change == "metadata":
        # Only memory-derived resonance is withdrawn; real historical joy remains.
        assert experiences(client)[0]["status"] == "active"
        assert current["state"]["valence"] > current["baseline"]["valence"]


def test_invalidation_is_atomic_idempotent_and_revision_checked(runtime):
    client, _, _, _, _ = runtime
    initial = send(runtime).json()
    request = {"command_id": "clear", "expected_revision": emotion(client)["revision"]}
    path = f"/v1/companions/sui/emotion/experiences/{initial['accepted_event_id']}/invalidate"
    response = client.post(path, json=request)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == response.json()["baseline"]
    assert client.post(path, json=request).json() == response.json()
    assert client.post(path, json={**request, "command_id": "stale"}).status_code == 409
    assert client.post(path, json={**request, "expected_revision": 0}).status_code == 409
    assert client.get("/v1/companions/missing/emotion").status_code == 404
    page = client.get("/v1/companions/sui/emotion/experiences?limit=1").json()
    assert len(page["items"]) == 1 and not page["has_more"]
    assert (
        client.get(
            f"/v1/companions/sui/emotion/experiences?after_sequence={page['next_sequence']}"
        ).json()["items"]
        == []
    )
    assert client.get("/v1/companions/sui/emotion/experiences?limit=101").status_code == 422


def test_rejected_owner_never_records_an_experience(runtime):
    client, _, _, _, state = runtime
    response = send(runtime, epoch=state["epoch"] + 1)
    assert response.status_code == 409
    assert experiences(client) == [] and emotion(client)["revision"] == 0


def test_provider_failure_preserves_accepted_user_event_once(runtime):
    client, _, _, provider, _ = runtime

    def fail():
        raise ProviderError("failed", "Local failure", releasable=True, dispatched=False)

    provider.action = fail
    response = send(runtime)
    assert response.status_code == 502, response.text
    assert send(runtime).json() == response.json()
    assert provider.calls == 1
    assert emotion(client)["reaction"] == "joy"
    assert len(experiences(client)) == 1
    assert experiences(client)[0]["completed_event_id"] is None
    assert experiences(client)[0]["delta"]["energy"] == 0


def test_newer_turn_fences_old_output_but_keeps_accounting(runtime):
    client, app, _, provider, _ = runtime

    def next_turn():
        provider.action = lambda: None
        assert send(runtime, "newer", "我今天很难过").json()["status"] == "completed"

    provider.action = next_turn
    result = send(runtime).json()
    assert result["status"] == "discarded" and result["assistant_message"] is None
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 160
    assert len(experiences(client)) == 2
    assert experiences(client)[0]["completed_event_id"] is None


def test_invalidating_context_during_generation_discards_reply(runtime):
    client, _, _, provider, _ = runtime

    def invalidate():
        snapshot = emotion(client)
        response = client.post(
            f"/v1/companions/sui/emotion/experiences/{snapshot['source_event_id']}/invalidate",
            json={"command_id": "clear", "expected_revision": snapshot["revision"]},
        )
        assert response.status_code == 200, response.text

    provider.action = invalidate
    result = send(runtime).json()
    assert result["status"] == "discarded" and result["usage"]["input_tokens"] == 70
    assert emotion(client)["state"] == emotion(client)["baseline"]


def test_transfer_keeps_emotion_and_stale_node_cannot_stimulate(runtime):
    client, _, _, _, state = runtime
    send(runtime)
    original = emotion(client)
    transfer = client.post(
        "/v1/companions/sui/transfers",
        json={
            "command_id": "phone",
            "expected_revision": state["revision"],
            "target_node_id": "phone",
        },
    ).json()
    base = f"/v1/transfers/{transfer['transfer_id']}/ack"
    client.post(base, json={"node_id": "phone", "phase": "ready"})
    assert (
        client.post(base, json={"node_id": "desk", "phase": "released"}).json()["status"]
        == "committed"
    )
    assert emotion(client) == original
    assert send(runtime, "stale").status_code == 409
    assert len(experiences(client)) == 1


def test_candidate_work_is_bounded_and_configuration_can_disable(runtime):
    client, app, _, _, _ = runtime
    app.state.emotion.policy = EmotionPolicy(candidate_limit=1)
    send(runtime, "first")
    send(runtime, "second", "hello")
    assert emotion(client)["truncated"] and emotion(client)["active_experiences"] == 1
    app.state.emotion.policy = EmotionPolicy(enabled=False)
    result = send(runtime, "disabled").json()
    assert result["status"] == "completed"
    assert len(experiences(client)) == 2
    assert not result["emotion"]["enabled"]
    assert result["emotion"]["state"] == result["emotion"]["baseline"]


@pytest.mark.parametrize(
    "values",
    [
        {"half_life_seconds": 0},
        {"enabled": "true"},
        {"resonance_strength": float("nan")},
        {"candidate_limit": 0},
    ],
)
def test_invalid_policy(values):
    with pytest.raises(ValidationError):
        EmotionPolicy.model_validate(values)


def test_policy_file_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[emotion]\nenabled=false\nhalf_life_seconds=600\n", encoding="utf8")
    assert Settings.load(path).emotion_policy.half_life_seconds == 600
    monkeypatch.setenv("VIRTUAL_LIFE_EMOTION_HALF_LIFE_SECONDS", "1800")
    assert Settings.load(path).emotion_policy.half_life_seconds == 1800


def test_unrelated_pinned_episode_does_not_resonate(runtime):
    client, app, _, _, _ = runtime
    result = send(runtime).json()
    app.state.memory.write(
        "sui",
        {
            "command_id": "reading",
            "kind": "episode",
            "content": "一起阅读夜航船",
            "pinned": True,
            "source_event_id": result["source_event_id"],
        },
    )
    assert send(runtime, "unrelated", "深海潜水装备").json()["status"] == "completed"
    assert not experiences(client)[-1]["resonance_valid"]
    assert experiences(client)[-1]["resonance_delta"]["valence"] == 0


def test_recovery_horizon_keeps_fingerprint_and_returns_baseline(runtime):
    client, _, clock, _, _ = runtime
    send(runtime)
    first = emotion(client)
    clock.value += timedelta(seconds=first["half_life_seconds"] * 25)
    current = emotion(client)
    assert current["state"] == current["baseline"]
    assert current["effective_state"]["energy"] == 0.8
    assert current["context_version"] == first["context_version"]
    assert current["active_experiences"] == 0
    assert len(experiences(client)) == 1


def test_acceptance_event_emotion_and_command_roll_back_together(runtime, monkeypatch):
    client, app, _, _, _ = runtime
    observe = app.state.emotion.observe

    def broken(*args):
        observe(*args)
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(app.state.emotion, "observe", broken)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        send(runtime)
    assert emotion(client)["revision"] == 0
    assert experiences(client) == []
    with app.state.coordinator.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM dialogue_commands").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE type='companion.dialogue_received'"
            ).fetchone()[0]
            == 0
        )


def test_missing_provider_keeps_observation_and_auth_is_required(runtime):
    client, app, _, _, _ = runtime
    app.state.dialogue.provider = None
    assert send(runtime).status_code == 503
    assert len(experiences(client)) == 1
    assert (
        client.get("/v1/companions/sui/emotion", headers={"Authorization": ""}).status_code == 401
    )
    client.post("/v1/companions", json={"companion_id": "other", "name": "另一位"})
    response = client.post(
        f"/v1/companions/other/emotion/experiences/{experiences(client)[0]['event_id']}/invalidate",
        json={"command_id": "cross", "expected_revision": 0},
    )
    assert response.status_code == 404


def test_saturation_remains_bounded_and_does_not_change_fact_recall(runtime):
    client, app, _, _, _ = runtime
    result = send(runtime).json()
    record = app.state.memory.write(
        "sui",
        {
            "command_id": "fact",
            "kind": "fact",
            "content": "生日是三月六日",
            "pinned": True,
            "source_event_id": result["source_event_id"],
        },
    )
    for index in range(7):
        assert send(runtime, f"praise-{index}").json()["status"] == "completed"
    state = emotion(client)
    assert state["state"]["valence"] == 1
    assert 0 <= state["state"]["arousal"] <= 1
    recall = client.post("/v1/companions/sui/memories/recall", json={"query": "生日"}).json()
    assert recall["items"][0]["memory_id"] == record["memory_id"]


def test_manual_state_expiry_during_generation_fences_output(runtime):
    client, _, clock, provider, _ = runtime
    client.post(
        "/v1/companions/sui/personality/state",
        json={
            "command_id": "manual",
            "expected_revision": 0,
            "state": {"mood": "tired"},
            "ttl_seconds": 30,
        },
    )
    provider.action = lambda: setattr(clock, "value", clock.value + timedelta(seconds=31))
    result = send(runtime).json()
    assert result["status"] == "discarded" and result["usage"]["input_tokens"] == 70


def test_affect_cannot_bypass_prompt_budget(runtime):
    client, app, _, provider, _ = runtime
    response = send(runtime, token_budget=1)
    assert response.status_code == 422
    assert provider.calls == 0
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 0
    assert len(experiences(client)) == 1
