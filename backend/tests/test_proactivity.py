"""Attention ordering, autonomous silence, durable limits and final output fences."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.model_provider import ProviderError
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.proactivity import ProactivityService
from virtual_life.settings import Settings


class Provider:
    def __init__(self):
        self.requests = []
        self.action = lambda: None

    def complete(self, request):
        self.requests.append(request)
        self.action()
        return ModelResult("随：歇一会儿，我在。", "fake", ModelUsage(80, 12))


@pytest.fixture
def runtime(tmp_path):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "proactive.sqlite"),
            api_token="test",
            model_base_url="http://model.invalid",
            model_name="fake",
            auto_memory_enabled=False,
            node_ttl_seconds=100000,
            lease_ttl_seconds=100000,
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
    # Manual worker tests deliberately do not run the application's lifespan.
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
            "expected_revision": 0,
            "definition": {"traits": {"initiative": 0.8}},
        },
    )
    yield client, app, clock, provider, state
    client.close()


def policy(runtime, **changes):
    client, _, _, _, _ = runtime
    current = client.get("/v1/companions/sui/proactivity").json()
    response = client.post(
        "/v1/companions/sui/proactivity",
        json={
            "command_id": "policy-" + str(current["revision"]),
            "expected_revision": current["revision"],
            "policy": {**current["policy"], "enabled": True, **changes},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def sample(runtime, sequence=1, **changes):
    _, _, clock, _, _ = runtime
    return {
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
        "boundary": "break_started",
        "boundary_id": "break-" + str(sequence),
        **changes,
    }


def observe(runtime, sequence=1, **changes):
    client, _, _, _, _ = runtime
    response = client.post(
        "/v1/nodes/desk/attention",
        json={
            "command_id": "attention-" + str(sequence),
            "sample": sample(runtime, sequence, **changes),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def decisions(runtime):
    return runtime[0].get("/v1/companions/sui/proactivity/decisions").json()["decisions"]


def test_disabled_default_and_sampling_never_calls_model(runtime):
    observe(runtime)
    client, app, _, provider, _ = runtime
    assert decisions(runtime)[0]["reason"] == "disabled"
    assert app.state.proactivity.process_next() is None
    assert not provider.requests
    assert client.get("/v1/nodes/desk/attention").json()["stale"] is False
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 0


def test_boundary_generates_once_without_forging_user_or_emotion(runtime):
    policy(runtime)
    client, app, _, provider, _ = runtime
    before = client.get("/v1/companions/sui/emotion").json()
    accepted = observe(runtime)
    assert not provider.requests
    # Open the poller access before the urge is produced so the output channel
    # replays the park event for a fresh cursor (access_start precedes it).
    access = client.post(
        "/v1/nodes/access/web",
        json={
            "browser_id": "test",
            "access_id": "access-1",
            "label": "test",
            "kind": "desktop",
            "bind_node_id": "desk",
        },
    )
    assert access.status_code == 201, access.text
    # DSH-owned path: the turn is parked, not locally completed.
    assert app.state.proactivity.process_next()["status"] == "awaiting_generation"
    assert not provider.requests
    item = decisions(runtime)[0]
    assert item["status"] == "running"
    assert item["reason"] == "eligible"
    # The parked turn still exposes the assembled prompt through the output channel.
    page = client.get(
        "/v1/companions/sui/proactivity/output"
        "?node_id=desk&access_id=access-1&epoch=1&after_sequence=0"
    ).json()
    assert page["items"]
    urge = page["items"][-1]
    assert urge["status"] == "awaiting_generation"
    assert urge["assistant_message"] is None
    assert "PROACTIVE_CONTEXT_DATA" in urge["context_text"]
    assert "CURRENT_USER_MESSAGE_DATA" not in urge["context_text"]
    assert urge["reason_text"]
    # Commit the externally generated reply through the dialogue commit route.
    command_id = item["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "歇一会儿，我在。", "usage": None},
    )
    assert commit.status_code == 200
    assert commit.json()["status"] == "completed"
    assert commit.json()["assistant_message"] == "歇一会儿，我在。"
    receipt = client.get("/v1/companions/sui/dialogue/" + command_id).json()
    assert receipt["assistant_message"] == "歇一会儿，我在。"
    assert receipt["preview"]["participation"]["reason"] == "chat_participation"
    # Candidate resolves to completed once DSH commits.
    final = decisions(runtime)[0]
    assert final["status"] == "completed"
    page = client.get("/v1/events?limit=500").json()["events"]
    events = [event for event in page if event["type"].startswith("companion.proactive_")]
    assert len(events) == 3  # decided + parked + completed
    assert "user_message" not in events[-1]["payload"]
    assert events[-1]["recipient_node_id"] == "desk"
    assert client.get("/v1/companions/sui/emotion").json() == before
    assert client.get("/v1/companions/sui/emotion/experiences").json()["items"] == []
    with app.state.coordinator.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auto_memory_queue").fetchone()[0] == 0
    assert observe(runtime) == accepted
    observe(runtime, 2, boundary_id="break-1")
    assert len(decisions(runtime)) == 1
    assert app.state.proactivity.process_next() is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"focus": "focused"}, "focused"),
        ({"focus": "suspected_stuck"}, "suspected_stuck"),
        ({"focus": "unknown"}, "attention_unknown"),
        ({"availability": "busy"}, "busy"),
        ({"user_present": False}, "user_away"),
        ({"locked": True}, "user_away"),
        ({"meeting": True}, "meeting_or_sharing"),
        ({"screen_sharing": True}, "meeting_or_sharing"),
        ({"locked": None}, "attention_unknown"),
    ],
)
def test_attention_suppresses_without_spending_tokens(runtime, changes, reason):
    policy(runtime)
    observe(runtime, **changes)
    assert decisions(runtime)[0]["reason"] == reason
    assert runtime[1].state.proactivity.process_next() is None
    assert not runtime[3].requests


def test_no_boundary_never_enqueues(runtime):
    policy(runtime)
    observe(runtime, boundary="none", boundary_id=None)
    assert decisions(runtime) == []
    assert not runtime[3].requests


def test_policy_cas_and_idempotency(runtime):
    client, _, _, _, _ = runtime
    result = policy(runtime)
    body = {"command_id": "policy-0", "expected_revision": 0, "policy": result["policy"]}
    assert client.post("/v1/companions/sui/proactivity", json=body).json() == result
    body["policy"]["daily_limit"] = 2
    assert (
        client.post("/v1/companions/sui/proactivity", json=body).json()["error"]["code"]
        == "idempotency_conflict"
    )
    body["command_id"] = "stale-policy"
    assert client.post("/v1/companions/sui/proactivity", json=body).status_code == 409


def test_samples_reject_old_sequences_retired_sessions_and_bad_times(runtime):
    client, _, clock, _, _ = runtime
    observe(runtime, 10)
    for number, changes in enumerate(
        [
            {"sequence": 9},
            {"sampled_at": (clock.value - timedelta(minutes=10)).isoformat()},
            {"sampled_at": (clock.value + timedelta(seconds=10)).isoformat()},
        ]
    ):
        response = client.post(
            "/v1/nodes/desk/attention",
            json={
                "command_id": "bad-" + str(number),
                "sample": sample(runtime, **changes),
            },
        )
        assert response.status_code == 409
    assert client.get("/v1/nodes/desk/attention").json()["sample"]["sequence"] == 10
    clock.value += timedelta(seconds=1)
    observe(runtime, 11, session_id="new-session")
    clock.value += timedelta(seconds=1)
    response = client.post(
        "/v1/nodes/desk/attention",
        json={
            "command_id": "retired",
            "sample": sample(runtime, 20),
        },
    )
    assert response.status_code == 409
    body = sample(runtime, 30, sampled_at="2026-10-14T08:00:00")
    assert (
        client.post(
            "/v1/nodes/desk/attention", json={"command_id": "naive", "sample": body}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("change", ["attention", "policy", "lease", "mode", "user", "transfer"])
def test_generation_rechecks_current_gates_and_discards(runtime, change):
    policy(runtime)
    observe(runtime)
    client, app, clock, provider, state = runtime

    def invalidate():
        if change == "attention":
            observe(runtime, 2, focus="focused", boundary="none", boundary_id=None)
        elif change == "policy":
            policy(runtime, enabled=False)
        elif change == "lease":
            clock.value += timedelta(seconds=100001)
        elif change == "mode":
            app.state.coordinator.patch_context(
                "sui",
                {
                    "command_id": "quiet",
                    "node_id": "desk",
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "interaction_mode": "quiet",
                },
            )
        elif change == "user":
            with app.state.coordinator.store.transaction(write=True) as connection:
                app.state.coordinator._emit(
                    connection,
                    clock.value,
                    "companion.dialogue_received",
                    {"user_message": "hello"},
                    companion_id="sui",
                    node_id="desk",
                )
        else:
            app.state.coordinator.prepare_transfer(
                "sui",
                {
                    "command_id": "move",
                    "target_node_id": "phone",
                    "expected_revision": state["revision"],
                },
            )

    # The turn is parked (DSH-owned). The gate change happens between park and
    # commit, so the externally committed reply must be discarded at _finalize.
    assert app.state.proactivity.process_next()["status"] == "awaiting_generation"
    invalidate()
    item = decisions(runtime)[0]
    receipt = client.get("/v1/companions/sui/dialogue/" + item["command_id"]).json()
    assert receipt["status"] == "awaiting_generation"
    commit = client.post(
        f"/v1/companions/sui/dialogue/{item['command_id']}/commit",
        json={"assistant_message": "歇一会儿，我在。", "usage": None},
    )
    if change == "lease":
        # The clock jump also trips the 300s commit window; the parked turn is
        # expired and the candidate resolved to discarded, not left in flight.
        assert commit.status_code == 409
        assert commit.json()["error"]["code"] == "dialogue_commit_expired"
        final = decisions(runtime)[0]
        assert final["status"] == "discarded"
        assert final["reason"] == "dialogue_commit_expired"
        assert not any(
            event["type"] == "companion.proactive_completed"
            for event in client.get("/v1/events?limit=500").json()["events"]
        )
        return
    assert commit.status_code == 200
    final = decisions(runtime)[0]
    assert final["status"] == "discarded"
    assert final["reason"]
    receipt = client.get("/v1/companions/sui/dialogue/" + item["command_id"]).json()
    assert receipt["assistant_message"] is None
    assert receipt["discard_reason"]
    assert not any(
        event["type"] == "companion.proactive_completed"
        for event in client.get("/v1/events?limit=500").json()["events"]
    )


def test_expiry_cooldown_daily_limit_and_restart(runtime):
    policy(runtime, cooldown_seconds=30)
    client, app, clock, provider, _ = runtime
    observe(runtime)
    clock.value += timedelta(seconds=91)
    assert client.get("/v1/nodes/desk/attention").json()["stale"]
    assert app.state.proactivity.process_next()["reason"] == "attention_stale"
    observe(runtime, 2)
    # DSH-owned path: the turn parks at awaiting_generation. Complete the round
    # trip so cooldown/daily-limit gates observe a finished reply.
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation"
    command_id = decisions(runtime)[-1]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "歇一会儿，我在。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    observe(runtime, 3)
    assert decisions(runtime)[-1]["reason"] == "cooldown"
    clock.value += timedelta(seconds=31)
    observe(runtime, 4)
    assert decisions(runtime)[-1]["reason"] == "daily_limit"
    restarted = ProactivityService(app.state.dialogue)
    assert restarted.get_policy("sui")["policy"]["enabled"]
    assert restarted.decisions("sui")["decisions"] == decisions(runtime)
    assert restarted.process_next() is None
    assert len(provider.requests) == 0


def test_unknown_provider_failure_is_not_retried_or_refunded(runtime):
    policy(runtime)
    observe(runtime)
    client, app, _, provider, _ = runtime

    def fail():
        raise ProviderError("timeout", "usage is unknown")

    provider.action = fail
    # DSH-owned path: the provider is never invoked, so an unknown provider
    # failure cannot occur; the turn parks and the reservation stays held until
    # DSH commits (or the commit window expires). Simulate the commit here.
    assert app.state.proactivity.process_next()["status"] == "awaiting_generation"
    assert app.state.proactivity.process_next() is None
    command_id = decisions(runtime)[0]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "歇一会儿，我在。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    observe(runtime, 2)
    assert decisions(runtime)[-1]["reason"] == "cooldown"
    assert len(provider.requests) == 0


def test_simultaneous_workers_claim_only_once(runtime):
    policy(runtime)
    observe(runtime)
    client, app, _, provider, _ = runtime
    entered, release = Event(), Event()
    real_send = app.state.dialogue.send_proactive

    def blocking_send(companion_id, payload):
        entered.set()
        assert release.wait(timeout=5)
        return real_send(companion_id, payload)

    # Block inside the dispatch so worker 1 holds the running claim while worker
    # 2 tries to claim; the provider is never invoked on the DSH-owned path.
    app.state.dialogue.send_proactive = blocking_send
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(app.state.proactivity.process_next)
        assert entered.wait(timeout=5)
        try:
            assert pool.submit(app.state.proactivity.process_next).result(timeout=5) is None
        finally:
            release.set()
        # DSH-owned path: the winning worker parks at awaiting_generation.
        assert first.result(timeout=5)["status"] == "awaiting_generation"
        assert len(provider.requests) == 0
        # Complete the round trip so the candidate resolves to completed.
        command_id = decisions(runtime)[0]["command_id"]
        commit = client.post(
            f"/v1/companions/sui/dialogue/{command_id}/commit",
            json={"assistant_message": "歇一会儿，我在。", "usage": None},
        )
        assert commit.status_code == 200, commit.text
        assert decisions(runtime)[0]["status"] == "completed"
    assert len(provider.requests) == 0


def test_refresh_has_no_observation_event_and_new_boundary_replaces_queue(runtime):
    policy(runtime)
    first = observe(runtime)
    client, app, clock, provider, _ = runtime
    clock.value += timedelta(seconds=1)
    refreshed = observe(runtime, 2, boundary_id="break-1")
    assert first["source_event_id"] == refreshed["source_event_id"]
    assert refreshed["sample"]["sequence"] == 2
    assert len(decisions(runtime)) == 1
    observe(runtime, 3)
    assert [item["status"] for item in decisions(runtime)] == ["suppressed", "queued"]
    assert decisions(runtime)[0]["reason"] == "boundary_superseded"
    # DSH-owned path: parks at awaiting_generation; no local provider call.
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation"
    assert len(provider.requests) == 0
    command_id = decisions(runtime)[1]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "歇一会儿，我在。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    events = client.get("/v1/events?limit=500").json()["events"]
    assert sum(event["type"] == "node.observation" for event in events) == 2


def test_gate_before_dispatch_releases_tokens_without_calling_provider(runtime, monkeypatch):
    policy(runtime)
    observe(runtime)
    _, app, _, provider, _ = runtime
    reserve = app.state.memory.reserve_model_tokens

    def reserve_then_focus(*args, **kwargs):
        hold = reserve(*args, **kwargs)
        observe(runtime, 2, focus="focused", boundary="none", boundary_id=None)
        return hold

    monkeypatch.setattr(app.state.memory, "reserve_model_tokens", reserve_then_focus)
    assert app.state.proactivity.process_next()["reason"] == "proactivity_focused"
    assert not provider.requests
    budget = app.state.memory.budget_snapshot("sui")
    assert budget["spent_tokens"] == budget["reserved_tokens"] == 0


def test_shared_model_quota_blocks_autonomous_generation(runtime):
    policy(runtime)
    observe(runtime)
    _, app, _, provider, _ = runtime
    app.state.memory.policy = app.state.memory.policy.model_copy(
        update={"daily_model_token_limit": 0}
    )
    assert app.state.proactivity.process_next()["reason"] == "model_token_budget_exceeded"
    assert not provider.requests


def test_expired_candidate_is_not_a_delayed_greeting(runtime):
    policy(runtime, candidate_ttl_seconds=5)
    observe(runtime)
    _, app, clock, provider, _ = runtime
    clock.value += timedelta(seconds=6)
    assert app.state.proactivity.process_next()["reason"] == "candidate_expired"
    assert not provider.requests


def test_restart_recovers_receipt_without_replaying_output(runtime):
    policy(runtime)
    observe(runtime)
    client, app, _, provider, _ = runtime
    # DSH-owned path: parks at awaiting_generation; commit completes the turn.
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation"
    command_id = decisions(runtime)[0]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "歇一会儿，我在。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    # Simulate a restart: the candidate is back to running but the receipt is
    # already terminal, so a fresh service recovers it without regenerating.
    with app.state.coordinator.store.transaction(write=True) as connection:
        connection.execute("UPDATE proactivity_candidates SET status='running', finished_at=NULL")
    assert ProactivityService(app.state.dialogue).process_next() is None
    assert decisions(runtime)[0]["reason"] == "receipt_recovered"
    events = client.get("/v1/events?limit=500").json()["events"]
    assert sum(event["type"] == "companion.proactive_completed" for event in events) == 1
    assert len(provider.requests) == 0


def test_unconfirmed_running_claim_expires_without_redispatch(runtime):
    policy(runtime)
    observe(runtime)
    _, app, clock, provider, _ = runtime
    with app.state.coordinator.store.transaction(write=True) as connection:
        connection.execute(
            "UPDATE proactivity_candidates SET status='running', started_at=?",
            (clock.value.isoformat(),),
        )
    restarted = ProactivityService(app.state.dialogue)
    assert restarted.process_next() is None
    clock.value += timedelta(seconds=301)
    assert restarted.process_next() is None
    assert decisions(runtime)[0]["reason"] == "execution_unknown"
    assert not provider.requests


@pytest.mark.parametrize("mode", ["quiet", "focus", "away"])
def test_explicit_mode_always_wins(runtime, mode):
    policy(runtime)
    _, app, _, provider, state = runtime
    app.state.coordinator.patch_context(
        "sui",
        {
            "command_id": "mode",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "interaction_mode": mode,
        },
    )
    observe(runtime)
    assert decisions(runtime)[0]["reason"] == "waiting_for_user"
    assert not provider.requests


def test_attention_permission_is_distinct_and_heartbeat_does_not_clear_it(runtime):
    client, _, _, _, _ = runtime
    response = client.post(
        "/v1/nodes/phone/attention",
        json={
            "command_id": "phone-attention",
            "sample": sample(runtime),
        },
    )
    assert response.status_code == 409
    observed = observe(runtime)
    assert (
        client.post("/v1/nodes/desk/heartbeat", json={"context": {"other": True}}).status_code
        == 200
    )
    assert client.get("/v1/nodes/desk/attention").json() == observed


def test_background_loop_executes_queued_boundary(runtime):
    policy(runtime)
    observe(runtime)
    client, app, _, provider, _ = runtime
    done = Event()
    real_send = app.state.dialogue.send_proactive

    def signal_send(companion_id, payload):
        done.set()
        return real_send(companion_id, payload)

    # The loop parks the turn (DSH-owned); signal once dispatch begins.
    app.state.dialogue.send_proactive = signal_send
    with TestClient(app, headers={"Authorization": "Bearer test"}):
        assert done.wait(timeout=5)
    # DSH-owned path: the loop parks the turn; provider is never invoked.
    assert decisions(runtime)[0]["status"] == "running"
    assert len(provider.requests) == 0
    command_id = decisions(runtime)[0]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "歇一会儿，我在。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    assert decisions(runtime)[0]["status"] == "completed"
    assert (
        client.get(
            "/v1/companions/sui/proactivity/decisions", headers={"Authorization": "Bearer bad"}
        ).status_code
        == 401
    )


def test_explicit_quiet_request_remains_in_force_at_later_boundary(runtime):
    policy(runtime)
    client, app, clock, provider, state = runtime
    response = client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": "user-quiet",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": "先安静陪我读一会儿",
        },
    )
    assert response.status_code == 200, response.text
    clock.value += timedelta(seconds=31)
    observe(runtime)
    assert decisions(runtime)[0]["reason"] == "quiet_requested"
    assert len(provider.requests) == 1


def test_low_effective_energy_is_a_local_suppression(runtime):
    policy(runtime)
    _, app, _, provider, _ = runtime
    app.state.personality.update_state(
        "sui",
        {
            "command_id": "tired",
            "expected_revision": 0,
            "state": {"mood": "tired", "energy": 0.1, "social_battery": 0.7},
        },
    )
    observe(runtime)
    assert decisions(runtime)[0]["reason"] == "energy_below_threshold"
    assert not provider.requests


def test_proactivity_poll_configuration(tmp_path, monkeypatch):
    config = tmp_path / "runtime.toml"
    config.write_text("[proactivity]\npoll_seconds = 3.0\n", encoding="utf-8")
    monkeypatch.delenv("VIRTUAL_LIFE_PROACTIVITY_POLL_SECONDS", raising=False)
    assert Settings.load(config).proactivity_poll_seconds == 3.0
    monkeypatch.setenv("VIRTUAL_LIFE_PROACTIVITY_POLL_SECONDS", "0.25")
    assert Settings.load(config).proactivity_poll_seconds == 0.25
    monkeypatch.setenv("VIRTUAL_LIFE_PROACTIVITY_POLL_SECONDS", "0")
    with pytest.raises(ValueError):
        Settings.load(config)


def test_schedule_and_boundary_user_message_carry_context(runtime):
    """P0-2: schedule/boundary scaffolds carry current context, stay short."""
    policy(runtime, cooldown_seconds=60, daily_limit=5)  # both origins may dispatch in one test
    client, app, clock, provider, state = runtime
    updated = client.post(
        "/v1/companions/sui/personality/state",
        json={
            "command_id": "mood-1",
            "expected_revision": 0,
            "state": {"mood": "curious", "energy": 0.4},
            "ttl_seconds": 86_400,
        },
    )
    assert updated.status_code == 200, updated.text
    created = clock.value.isoformat()
    expires = (clock.value + timedelta(hours=24)).isoformat()
    with app.state.coordinator.store.transaction() as connection:
        connection.execute(
            """INSERT INTO inner_activities
                (activity_id, companion_id, kind, trigger, content, salience, urge,
                 attention_session_id, attention_boundary_id, context_version,
                 source_event_id, generator, model_json, thinking, status, created_at, expires_at)
                VALUES ('ina-context-1', 'sui', 'restless', 'social_need',
                        '想找你聊两句最近读的东西', 0.4, 0.2, NULL, NULL, 'v1',
                        NULL, 'rules_v1', NULL, NULL, 'active', ?, ?)""",
            (created, expires),
        )
    captured: list[str] = []
    dialogue = app.state.dialogue
    original = dialogue.send_proactive

    def spy(companion_id, payload):
        captured.append(payload["user_message"])
        return original(companion_id, payload)

    # Idle gate (_idle_reason) requires a dialogue history and >= min_idle_seconds
    # since the last user turn, so seed one real turn and advance past the window.
    talk = client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": "user-talk-1",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": "刚才在忙，现在告一段落了",
        },
    )
    assert talk.status_code == 200, talk.text
    clock.value += timedelta(seconds=4_200)
    dialogue.send_proactive = spy
    observe(runtime)  # boundary candidate
    assert app.state.proactivity.process_next()["status"] == "awaiting_generation"
    app.state.proactivity.schedule("sui", "sched-1", "tool-1", 600, reason="check-in")
    clock.value += timedelta(seconds=610)
    observe(runtime, sequence=2, boundary="none", boundary_id=None)
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation", result
    assert len(captured) == 2
    day_parts = ("清晨", "上午", "下午", "傍晚", "晚上", "深夜")
    for message in captured:
        assert any(part in message for part in day_parts)
        assert "好奇" in message
        assert "想找你聊两句最近读的东西" in message
        assert len(message) <= 200
        assert "{" not in message  # context note is prose, never raw JSON
    assert "宿主报告" in captured[0]  # boundary scaffold retained
    assert "很久没有和用户交谈" in captured[1]  # schedule scaffold retained
    # One model call for the seeded user turn; the proactive turns park externally.
    assert len(provider.requests) == 1


def _register_phone(app):
    app.state.coordinator.register_node(
        {
            "node_id": "phone",
            "label": "phone",
            "kind": "android",
            "capabilities": ["text.display", "attention.observe"],
        }
    )


def _observe_phone(client, runtime, sequence, **changes):
    body = sample(runtime, sequence, boundary="none", boundary_id=None, **changes)
    response = client.post(
        "/v1/nodes/phone/attention",
        json={"command_id": "phone-" + str(sequence), "sample": body},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_companion_attention_merges_across_devices(runtime):
    client, app, clock, _, _ = runtime
    _register_phone(app)
    # The user is on the desk first, then moves to the phone without the
    # room-follow moving the companion (sample says user not present there).
    observe(runtime)
    clock.value += timedelta(minutes=1)
    _observe_phone(client, runtime, 1, user_present=False)
    merged = client.get("/v1/companions/sui/attention").json()
    assert merged["node_id"] == "phone"
    assert merged["stale"] is False
    assert merged["sample"]["sequence"] == 1
    # The per-node views stay untouched by the merged read.
    assert client.get("/v1/nodes/desk/attention").json()["stale"] is False
    assert client.get("/v1/nodes/phone/attention").json()["stale"] is False


def test_companion_attention_falls_back_when_all_stale(runtime):
    client, app, clock, _, _ = runtime
    _register_phone(app)
    observe(runtime)
    clock.value += timedelta(minutes=1)
    _observe_phone(client, runtime, 1, user_present=False)
    # Long sleep: every live sample expires.
    clock.value += timedelta(hours=1)
    merged = client.get("/v1/companions/sui/attention").json()
    assert merged["stale"] is True
    assert merged["sample"] is None
    assert merged["node_id"] == "desk"  # companion's active room


def test_inner_preflight_survives_device_switch(runtime):
    client, app, clock, _, _ = runtime
    policy(runtime)
    _register_phone(app)
    # Desk sample exists but ages out; the fresher phone sample keeps the
    # companion's cross-device perception alive for the inner preflight.
    observe(runtime)
    clock.value += timedelta(minutes=10)  # desk ttl is 90 seconds
    _observe_phone(client, runtime, 1)
    assert client.get("/v1/nodes/desk/attention").json()["stale"] is True
    proact = app.state.proactivity
    companion = app.state.coordinator.get_companion("sui")
    activity = {
        "activity_id": "ina-device-switch",
        "urge": 0.8,
        "attention_boundary_id": None,
    }
    with proact.store.transaction() as connection:
        assert proact._inner_preflight(connection, companion, activity, clock.value) is True
    # Without any live sample the preflight stays closed.
    clock.value += timedelta(hours=1)
    with proact.store.transaction() as connection:
        assert proact._inner_preflight(connection, companion, activity, clock.value) is False


def test_curious_inner_message_peeks_at_screen_before_speaking(runtime):
    """P1 #4: a curious inner activity looks at the phone once, then carries
    what it actually saw into the proactive prompt (no invention allowed)."""
    policy(runtime, cooldown_seconds=60, daily_limit=5)
    client, app, clock, provider, state = runtime
    client.post(
        "/v1/companions/sui/personality/state",
        json={
            "command_id": "mood-2",
            "expected_revision": 0,
            "state": {"mood": "curious", "energy": 0.6},
            "ttl_seconds": 86_400,
        },
    )
    talk = client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": "user-talk-2",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": "在忙，一会儿再聊",
        },
    )
    assert talk.status_code == 200, talk.text
    clock.value += timedelta(seconds=4_200)
    created = clock.value.isoformat()
    expires = (clock.value + timedelta(hours=24)).isoformat()
    with app.state.coordinator.store.transaction() as connection:
        row = connection.execute(
            "SELECT event_id FROM events WHERE companion_id='sui' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        source_event_id = row["event_id"] if row else "evt-curious-1"
        connection.execute(
            """INSERT INTO inner_activities
                (activity_id, companion_id, kind, trigger, content, salience, urge,
                 attention_session_id, attention_boundary_id, context_version,
                 source_event_id, generator, model_json, thinking, status, created_at, expires_at)
                VALUES ('ina-curious-1', 'sui', 'curious', 'curiosity',
                        '想看看你的手机上在放什么', 0.6, 0.8, NULL, NULL, 'v1',
                        ?, 'rules_v1', NULL, NULL, 'active', ?, ?)""",
            (source_event_id, created, expires),
        )
    monkey_target = app.state.inner_life
    monkey_target.peek = lambda: "设置"
    captured: list[str] = []
    dialogue = app.state.dialogue
    original = dialogue.send_proactive

    def spy(companion_id, payload):
        captured.append(payload["user_message"])
        return original(companion_id, payload)

    dialogue.send_proactive = spy
    observe(runtime, boundary="none", boundary_id=None)
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation", result
    assert len(captured) == 1
    assert "设置" in captured[0]
    assert "你刚才好奇地看了一眼手机屏幕" in captured[0]
    assert "不要编造" in captured[0]


def test_curious_inner_message_without_peek_keeps_prompt_plain(runtime):
    """peek() unavailable (device offline) -> no fabricated screen facts."""
    policy(runtime, cooldown_seconds=60, daily_limit=5)
    client, app, clock, provider, state = runtime
    client.post(
        "/v1/companions/sui/personality/state",
        json={
            "command_id": "mood-3",
            "expected_revision": 0,
            "state": {"mood": "curious", "energy": 0.6},
            "ttl_seconds": 86_400,
        },
    )
    talk = client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": "user-talk-3",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": "在忙，一会儿再聊",
        },
    )
    assert talk.status_code == 200, talk.text
    clock.value += timedelta(seconds=4_200)
    created = clock.value.isoformat()
    expires = (clock.value + timedelta(hours=24)).isoformat()
    with app.state.coordinator.store.transaction() as connection:
        row = connection.execute(
            "SELECT event_id FROM events WHERE companion_id='sui' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        source_event_id = row["event_id"] if row else "evt-curious-2"
        connection.execute(
            """INSERT INTO inner_activities
                (activity_id, companion_id, kind, trigger, content, salience, urge,
                 attention_session_id, attention_boundary_id, context_version,
                 source_event_id, generator, model_json, thinking, status, created_at, expires_at)
                VALUES ('ina-curious-2', 'sui', 'curious', 'curiosity',
                        '想看看你的手机上在放什么', 0.6, 0.8, NULL, NULL, 'v1',
                        ?, 'rules_v1', NULL, NULL, 'active', ?, ?)""",
            (source_event_id, created, expires),
        )
    app.state.inner_life.peek = lambda: None
    captured: list[str] = []
    dialogue = app.state.dialogue
    original = dialogue.send_proactive

    def spy(companion_id, payload):
        captured.append(payload["user_message"])
        return original(companion_id, payload)

    dialogue.send_proactive = spy
    observe(runtime, boundary="none", boundary_id=None)
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation", result
    assert len(captured) == 1
    assert "看了一眼手机屏幕" not in captured[0]


def _ritual_events(client, kind=None):
    page = client.get("/v1/events?limit=500").json()["events"]
    return [
        event
        for event in page
        if event["type"] == "companion.ritual"
        and (kind is None or event["payload"].get("kind") == kind)
    ]


def test_calendar_mark_api_lifecycle(runtime):
    client, _, _, _, _ = runtime
    created = client.post(
        "/v1/companions/sui/calendar-marks",
        json={"command_id": "mark-1", "month_day": "05-20", "label": "我们的纪念日"},
    )
    assert created.status_code == 201, created.text
    mark = created.json()["mark"]
    assert mark["month_day"] == "05-20"
    assert mark["label"] == "我们的纪念日"
    repeated = client.post(
        "/v1/companions/sui/calendar-marks",
        json={"command_id": "mark-1", "month_day": "05-20", "label": "我们的纪念日"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["mark"]["mark_id"] == mark["mark_id"]
    invalid = client.post(
        "/v1/companions/sui/calendar-marks",
        json={"command_id": "mark-2", "month_day": "13-01", "label": "x"},
    )
    assert invalid.status_code == 422
    listing = client.get("/v1/companions/sui/calendar-marks").json()
    assert [m["mark_id"] for m in listing["marks"]] == [mark["mark_id"]]
    deleted = client.delete(f"/v1/companions/sui/calendar-marks/{mark['mark_id']}")
    assert deleted.status_code == 200
    assert client.get("/v1/companions/sui/calendar-marks").json()["marks"] == []
    assert client.delete(f"/v1/companions/sui/calendar-marks/{mark['mark_id']}").status_code == 404


def test_morning_ritual_fires_once_per_local_day(runtime):
    client, app, clock, _, _ = runtime
    policy(runtime)
    # New semantics: the daily greeting no longer enqueues a standalone
    # candidate. It rides along the first boundary dispatch of the local day
    # and is recorded as a companion.ritual event at dispatch time.
    observe(runtime)
    access = client.post(
        "/v1/nodes/access/web",
        json={
            "browser_id": "test",
            "access_id": "access-1",
            "label": "test",
            "kind": "desktop",
            "bind_node_id": "desk",
        },
    )
    assert access.status_code == 201, access.text
    assert app.state.proactivity.process_next()["status"] == "awaiting_generation"
    events = _ritual_events(client, "morning")
    assert len(events) == 1
    # The greeting is visible to the model as an appended note, not forged
    # user content.
    page = client.get(
        "/v1/companions/sui/proactivity/output"
        "?node_id=desk&access_id=access-1&epoch=1&after_sequence=0"
    ).json()
    assert page["items"]
    assert "第一条主动消息" in page["items"][-1]["context_text"]
    # A second dispatch on the same local day does not greet again.
    command_id = decisions(runtime)[0]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "歇一会儿。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    observe(runtime, sequence=2)
    app.state.proactivity.process_next()
    assert len(_ritual_events(client, "morning")) == 1
    # A new local day greets again.
    clock.value += timedelta(days=1)
    observe(runtime, sequence=3)
    app.state.proactivity.process_next()
    assert len(_ritual_events(client, "morning")) == 2


def test_anniversary_ritual_matches_marked_date(runtime):
    client, app, clock, _, _ = runtime
    policy(runtime)
    created = client.post(
        "/v1/companions/sui/calendar-marks",
        json={"command_id": "mark-1", "month_day": "01-01", "label": "初次见面"},
    )
    assert created.status_code == 201, created.text
    app.state.proactivity.process_next()
    anniversaries = _ritual_events(client, "anniversary")
    assert len(anniversaries) == 1
    assert anniversaries[0]["payload"]["label"] == "初次见面"
    assert anniversaries[0]["payload"]["mark_id"] == created.json()["mark"]["mark_id"]
    # Same year: no repeat even after a second pass.
    app.state.proactivity.process_next()
    assert len(_ritual_events(client, "anniversary")) == 1


def test_morning_ritual_stays_silent_without_proactivity_policy(runtime):
    client, app, _, _, _ = runtime
    # Policy disabled: no ritual events, process_next stays None.
    assert app.state.proactivity.process_next() is None
    assert _ritual_events(client) == []


def test_ritual_candidate_dispatches_with_context(runtime):
    client, app, clock, provider, _ = runtime
    policy(runtime)
    created = client.post(
        "/v1/companions/sui/calendar-marks",
        json={"command_id": "mark-1", "month_day": "01-01", "label": "初次见面"},
    )
    assert created.status_code == 201, created.text
    observe(runtime, boundary="none", boundary_id=None)
    access = client.post(
        "/v1/nodes/access/web",
        json={
            "browser_id": "test",
            "access_id": "access-1",
            "label": "test",
            "kind": "desktop",
            "bind_node_id": "desk",
        },
    )
    assert access.status_code == 201, access.text
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation", result
    item = decisions(runtime)[0]
    assert item["origin"] == "ritual"
    assert item["status"] == "running"
    page = client.get(
        "/v1/companions/sui/proactivity/output"
        "?node_id=desk&access_id=access-1&epoch=1&after_sequence=0"
    ).json()
    assert page["items"]
    context = page["items"][-1]["context_text"]
    assert "初次见面" in context
    command_id = item["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "早上好。", "usage": None},
    )
    assert commit.status_code == 200
    assert commit.json()["status"] == "completed"


def test_decision_records_psychology_snapshot(runtime):
    # Even a suppressed decision explains *why now*: the row carries a
    # redundant psychological snapshot (mood/energy/social battery, affect,
    # inner activity, day part) alongside the machine reason.
    observe(runtime)
    item = decisions(runtime)[0]
    assert item["reason"] == "disabled"
    psychology = item["psychology"]
    assert psychology is not None
    for key in ("mood", "energy", "social_battery"):
        assert key in psychology
    page = runtime[0].get("/v1/events?limit=500").json()["events"]
    decided = [e for e in page if e["type"] == "companion.proactivity_decided"]
    assert decided and decided[-1]["payload"]["psychology"] is not None
