"""Owner-gated personality evolution: generation, guardrails and adoption."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.personality import _timestamp
from virtual_life.personality_evolution import EvolutionPolicy
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class Provider:
    def __init__(self):
        self.calls = 0
        self.action = lambda: None
        self.reply = "我在，陪你。"
        self.failure = None
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        self.action()
        if self.failure is not None:
            raise self.failure
        return ModelResult(self.reply, "fake", ModelUsage(70, 10))


def model_reply(**overrides):
    payload = {
        "confidence": 0.9,
        "rationale": "近来互动显示可以固定一小段安静时间。",
        "patch": [{"op": "add_item", "field": "habits", "item": "每天留一点安静时间"}],
        **overrides,
    }
    return json.dumps(payload)


def policy(**overrides):
    return EvolutionPolicy(
        min_evidence_items=2,
        trait_window_seconds=86_400,
        **overrides,
    )


@pytest.fixture
def runtime(tmp_path):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "evolution.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            lease_ttl_seconds=10000,
            node_ttl_seconds=10000,
        ),
        model_provider=provider,
        evolution_policy=policy(),
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


def send(runtime, command, message):
    client, _, _, _, state = runtime
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


def refresh(client, command_id, **body):
    return client.post(
        "/v1/companions/sui/personality/proposals/refresh",
        json={"command_id": command_id, **body},
    )


def accept(client, proposal_id, command_id, personality_revision, proposal_revision=1):
    return client.post(
        f"/v1/companions/sui/personality/proposals/{proposal_id}/accept",
        json={
            "command_id": command_id,
            "expected_personality_revision": personality_revision,
            "expected_proposal_revision": proposal_revision,
        },
    )


def add_proactive(app, status, event_id, command):
    ts = _timestamp(app.state.personality._now())
    with app.state.proactivity.store.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO proactivity_candidates(candidate_id, companion_id, node_id, epoch,
            source_event_id, boundary, policy_revision, status, reason, command_id, created_at,
            expires_at, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"cand_{command}",
                "sui",
                "desk",
                0,
                event_id,
                "task_completed",
                0,
                status,
                "test",
                command,
                ts,
                ts,
                ts,
                ts,
            ),
        )


def quiet_turn(runtime, command):
    assert send(runtime, command, "先安静陪我读一会儿").json()["status"] == "completed"


def test_quiet_requests_generate_owner_gated_proposal(runtime):
    client, app, _, _, _ = runtime
    quiet_turn(runtime, "quiet-1")
    quiet_turn(runtime, "quiet-2")
    response = refresh(client, "gen")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["generated"] == 1 and result["model_calls"] == 0
    proposal = result["proposals"][0]
    assert proposal["status"] == "proposed"
    assert proposal["base_personality_revision"] == 0
    operation = proposal["patch"][0]
    assert operation["op"] == "adjust_trait" and operation["field"] == "initiative"
    assert operation["before"] == 0.3 and operation["after"] == 0.25
    assert proposal["evidence"]["aggregates"][0]["metric"] == "quiet_request_rate"

    # Refresh is idempotent and never changes the profile by itself.
    assert refresh(client, "gen").json() == result
    assert client.get("/v1/companions/sui/personality").json()["revision"] == 0

    adopted = accept(client, proposal["proposal_id"], "take", 0)
    assert adopted.status_code == 200, adopted.text
    decision = adopted.json()
    assert decision["personality"]["revision"] == 1
    assert decision["personality"]["definition"]["traits"]["initiative"] == 0.25
    assert decision["proposal"]["status"] == "accepted"

    history = client.get("/v1/companions/sui/personality/history").json()
    assert history["items"][0]["revision"] == 1
    assert history["items"][0]["source"] == "evolution"
    assert history["items"][0]["source_ref"] == proposal["proposal_id"]
    assert not history["has_more"]

    # The accepted field is now cooling down, so nothing new is proposed.
    quiet_turn(runtime, "quiet-3")
    again = refresh(client, "gen-2").json()
    assert again["generated"] == 0
    assert any(item["reason"] == "cooldown_active" for item in again["skipped"])


def test_open_proposal_blocks_a_duplicate_field(runtime):
    client, _, _, _, _ = runtime
    quiet_turn(runtime, "quiet-1")
    quiet_turn(runtime, "quiet-2")
    assert refresh(client, "gen").json()["generated"] == 1
    quiet_turn(runtime, "quiet-3")
    second = refresh(client, "gen-2").json()
    assert second["generated"] == 0
    assert any(item["reason"] == "open_proposal_exists" for item in second["skipped"])
    listed = client.get("/v1/companions/sui/personality/proposals").json()
    assert len(listed["items"]) == 1 and listed["items"][0]["status"] == "proposed"


def test_owner_edit_supersedes_and_stale_accept_is_rejected(runtime):
    client, _, _, _, _ = runtime
    quiet_turn(runtime, "quiet-1")
    quiet_turn(runtime, "quiet-2")
    proposal = refresh(client, "gen").json()["proposals"][0]
    current = client.get("/v1/companions/sui/personality").json()
    owner = client.post(
        "/v1/companions/sui/personality",
        json={
            "command_id": "owner-edit",
            "expected_revision": current["revision"],
            "definition": current["definition"],
        },
    )
    assert owner.status_code == 200, owner.text
    assert owner.json()["revision"] == 1
    listed = client.get("/v1/companions/sui/personality/proposals").json()["items"][0]
    assert listed["status"] == "superseded"
    stale = accept(client, proposal["proposal_id"], "take", 1)
    assert stale.status_code == 409


def test_reject_keeps_the_profile_and_is_idempotent(runtime):
    client, _, _, _, _ = runtime
    quiet_turn(runtime, "quiet-1")
    quiet_turn(runtime, "quiet-2")
    proposal = refresh(client, "gen").json()["proposals"][0]
    body = {
        "command_id": "drop",
        "expected_personality_revision": 0,
        "expected_proposal_revision": 1,
        "reason": "not now",
    }
    path = f"/v1/companions/sui/personality/proposals/{proposal['proposal_id']}/reject"
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["proposal"]["status"] == "rejected"
    assert response.json()["personality"]["revision"] == 0
    assert client.post(path, json=body).json() == response.json()
    got = client.get(f"/v1/companions/sui/personality/proposals/{proposal['proposal_id']}").json()
    assert got == response.json()["proposal"]
    assert client.get("/v1/companions/sui/personality/proposals/missing").status_code == 404


def test_proactive_silence_generates_from_local_evidence(runtime):
    client, app, _, _, _ = runtime
    assert send(runtime, "hello-1", "你好呀").json()["status"] == "completed"
    assert send(runtime, "hello-2", "今天天气不错").json()["status"] == "completed"
    events = client.get("/v1/companions/sui/emotion/experiences").json()["items"]
    add_proactive(app, "silent", events[0]["event_id"], "p1")
    add_proactive(app, "discarded", events[1]["event_id"], "p2")
    result = refresh(client, "gen").json()
    assert result["generated"] == 1
    proposal = result["proposals"][0]
    assert proposal["evidence"]["aggregates"][0]["metric"] == "proactive_silent_rate"
    assert accept(client, proposal["proposal_id"], "take", 0).status_code == 200


def test_model_assisted_and_disabled_paths_stay_closed(runtime):
    client, app, _, provider, _ = runtime
    assert refresh(client, "m1", use_model=True).status_code == 422
    assert refresh(client, "m1", use_model=True).json()["error"]["code"] == (
        "model_assisted_disabled"
    )
    app.state.personality_evolution.policy = policy(model_assisted=True)
    # The provider replies with plain text, which is discarded rather than adopted.
    assisted = refresh(client, "m2", use_model=True)
    assert assisted.status_code == 200, assisted.text
    body = assisted.json()
    assert body["generated"] == 0
    assert body["model_calls"] == 1
    assert any(item["reason"] == "invalid_model_proposal" for item in body["skipped"])
    assert provider.calls == 1
    app.state.personality_evolution.policy = policy(enabled=False)
    assert refresh(client, "m3").status_code == 422


def test_model_assisted_without_provider_is_unavailable(tmp_path):
    app = create_app(
        Settings(
            database_path=str(tmp_path / "no-model.sqlite"),
            api_token="test",
            auto_memory_enabled=False,
        ),
        evolution_policy=EvolutionPolicy(model_assisted=True),
    )
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/personality/proposals/refresh",
            json={"command_id": "m", "use_model": True},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "model_assisted_unavailable"


def test_model_patch_is_bounded_and_adoptable(runtime):
    client, app, clock, provider, _ = runtime
    app.state.personality_evolution.policy = policy(model_assisted=True)
    provider.reply = model_reply()
    result = refresh(client, "model-ok", use_model=True).json()
    assert result["model_calls"] == 1
    assert result["generated"] == 1
    proposal = result["proposals"][0]
    assert proposal["generator"] == "model_v1"
    assert proposal["confidence"] == 0.7
    assert proposal["model"]["model"] == "fake"
    assert proposal["patch"][0]["item"] == "每天留一点安静时间"
    accepted = accept(client, proposal["proposal_id"], "adopt", 0)
    assert accepted.status_code == 200, accepted.text
    assert (
        "每天留一点安静时间"
        in accepted.json()["personality"]["definition"]["speech_style"]["habits"]
    )
    # Replaying the command reuses the stored receipt and does not call the model again.
    assert provider.calls == 1
    assert refresh(client, "model-ok", use_model=True).json() == result
    assert provider.calls == 1
    _ = clock


def test_model_cannot_widen_sources_or_exceed_bounds(runtime):
    client, app, _, provider, _ = runtime
    app.state.personality_evolution.policy = policy(model_assisted=True)
    provider.reply = model_reply(sources={"event_refs": ["evt_missing"]})
    widened = refresh(client, "model-src", use_model=True).json()
    assert widened["generated"] == 0
    assert any(item["reason"] == "invalid_model_proposal" for item in widened["skipped"])

    provider.reply = json.dumps(
        {
            "confidence": 0.9,
            "rationale": "步子太大",
            "patch": [
                {
                    "op": "adjust_trait",
                    "field": "initiative",
                    "delta": 0.15,
                    "before": 0.3,
                    "after": 0.45,
                }
            ],
        }
    )
    oversized = refresh(client, "model-step", use_model=True).json()
    assert oversized["generated"] == 0
    assert any(item["reason"] == "invalid_model_proposal" for item in oversized["skipped"])


def test_model_failure_produces_no_proposal(runtime):
    client, app, _, provider, _ = runtime
    from virtual_life.model_provider import ProviderError

    app.state.personality_evolution.policy = policy(model_assisted=True)
    provider.failure = ProviderError(
        "model_transport_error", "boom", dispatched=False, releasable=True
    )
    response = refresh(client, "model-fail", use_model=True)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "model_execution_failed"
    assert refresh(client, "model-ok-2").json()["proposals"] == []
    # The failed task is released so a new command can still reserve tokens.
    provider.failure = None
    provider.reply = model_reply()
    assert refresh(client, "model-retry", use_model=True).json()["generated"] == 1


def test_thresholds_and_windows_bound_generation(tmp_path):
    clock = Clock()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "bounded.sqlite"),
            api_token="test",
            auto_memory_enabled=False,
        ),
        evolution_policy=EvolutionPolicy(min_evidence_items=2, quiet_request_rate_threshold=0.9),
    )
    app.state.personality.clock = clock
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        for index, message in enumerate(("先安静陪我读一会儿", "今天天气不错")):
            client.post(
                "/v1/companions/sui/dialogue",
                json={
                    "command_id": f"turn-{index}",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "user_message": message,
                },
            )
        result = client.post(
            "/v1/companions/sui/personality/proposals/refresh",
            json={"command_id": "gen"},
        ).json()
        assert result["generated"] == 0
        assert any(item["reason"] == "below_threshold" for item in result["skipped"])
        clock.value += timedelta(days=2)
        aged = client.post(
            "/v1/companions/sui/personality/proposals/refresh",
            json={"command_id": "gen-aged"},
        ).json()
        assert aged["generated"] == 0
        assert any(item["reason"] == "insufficient_evidence" for item in aged["skipped"])


def test_evolution_policy_file_and_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        "[personality.evolution]\nenabled=false\nmin_evidence_items=4\n",
        encoding="utf8",
    )
    loaded = Settings.load(path)
    assert loaded.evolution_policy.enabled is False
    assert loaded.evolution_policy.min_evidence_items == 4
    monkeypatch.setenv("VIRTUAL_LIFE_PERSONALITY_EVOLUTION_MIN_EVIDENCE_ITEMS", "7")
    assert Settings.load(path).evolution_policy.min_evidence_items == 7


def test_unknown_evolution_fields_are_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[personality.evolution]\nmystery=true\n", encoding="utf8")
    with pytest.raises(ValueError):
        Settings.load(path)
    assert EvolutionPolicy(enabled=True).enabled is True
    with pytest.raises(Exception):
        EvolutionPolicy.model_validate({"min_evidence_items": "two"})


def test_evidence_expiry_marks_proposals_and_reads_are_pure(runtime):
    client, app, clock, _, _ = runtime
    quiet_turn(runtime, "quiet-1")
    quiet_turn(runtime, "quiet-2")
    proposal = refresh(client, "gen").json()["proposals"][0]
    revision = client.get("/v1/companions/sui/personality").json()["revision"]
    clock.value += timedelta(
        seconds=app.state.personality_evolution.policy.proposal_ttl_seconds + 1
    )
    response = client.get(
        f"/v1/companions/sui/personality/proposals/{proposal['proposal_id']}"
    ).json()
    assert response["status"] == "expired"
    assert accept(client, proposal["proposal_id"], "take", revision).status_code == 409
    # Reading expiry never rewrites the profile.
    assert client.get("/v1/companions/sui/personality").json()["revision"] == revision


def test_history_reads_do_not_create_rows(runtime):
    client, app, _, _, _ = runtime
    assert client.get("/v1/companions/sui/personality/history").json()["items"] == []
    with app.state.personality.store.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM personality_profile_revisions").fetchone()[0]
            == 0
        )


def test_evolution_clock_is_deterministic(runtime):
    client, _, clock, _, _ = runtime
    assert datetime(2025, 1, 1, tzinfo=UTC) == clock.value
    quiet_turn(runtime, "quiet-1")
    quiet_turn(runtime, "quiet-2")
    proposal = refresh(client, "gen").json()["proposals"][0]
    assert proposal["created_at"].startswith("2025-01-01")
    assert proposal["expires_at"].startswith("2025-01-31")
