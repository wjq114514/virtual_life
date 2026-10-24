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
    assert app.state.proactivity.process_next()["status"] == "completed"
    assert len(provider.requests) == 1
    assert provider.requests[0].purpose == "proactive"
    assert "PROACTIVE_CONTEXT_DATA" in provider.requests[0].context_text
    assert "CURRENT_USER_MESSAGE_DATA" not in provider.requests[0].context_text
    item = decisions(runtime)[0]
    assert item["status"] == "completed"
    receipt = client.get("/v1/companions/sui/dialogue/" + item["command_id"]).json()
    assert receipt["assistant_message"] == "歇一会儿，我在。"
    assert receipt["preview"]["participation"]["reason"] == "chat_participation"
    page = client.get("/v1/events?limit=500").json()["events"]
    events = [event for event in page if event["type"].startswith("companion.proactive_")]
    assert len(events) == 2
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
    budget_day = clock.value.date().isoformat()

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

    provider.action = invalidate
    assert app.state.proactivity.process_next()["status"] == "discarded"
    item = decisions(runtime)[0]
    receipt = client.get("/v1/companions/sui/dialogue/" + item["command_id"]).json()
    assert receipt["assistant_message"] is None
    assert receipt["discard_reason"]
    assert receipt["usage"]["output_tokens"] == 12
    assert app.state.memory.budget_snapshot("sui", budget_day)["spent_tokens"] == 92
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
    assert app.state.proactivity.process_next()["status"] == "completed"
    observe(runtime, 3)
    assert decisions(runtime)[-1]["reason"] == "cooldown"
    clock.value += timedelta(seconds=31)
    observe(runtime, 4)
    assert decisions(runtime)[-1]["reason"] == "daily_limit"
    restarted = ProactivityService(app.state.dialogue)
    assert restarted.get_policy("sui")["policy"]["enabled"]
    assert restarted.decisions("sui")["decisions"] == decisions(runtime)
    assert restarted.process_next() is None
    assert len(provider.requests) == 1


def test_unknown_provider_failure_is_not_retried_or_refunded(runtime):
    policy(runtime)
    observe(runtime)
    _, app, _, provider, _ = runtime

    def fail():
        raise ProviderError("timeout", "usage is unknown")

    provider.action = fail
    assert app.state.proactivity.process_next()["reason"] == "timeout"
    assert app.state.proactivity.process_next() is None
    assert app.state.memory.budget_snapshot("sui")["reserved_tokens"] > 0
    observe(runtime, 2)
    assert decisions(runtime)[-1]["reason"] == "cooldown"
    assert len(provider.requests) == 1


def test_simultaneous_workers_claim_only_once(runtime):
    policy(runtime)
    observe(runtime)
    _, app, _, provider, _ = runtime
    entered, release = Event(), Event()

    def wait():
        entered.set()
        assert release.wait(timeout=5)

    provider.action = wait
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(app.state.proactivity.process_next)
        assert entered.wait(timeout=5)
        try:
            assert pool.submit(app.state.proactivity.process_next).result(timeout=5) is None
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "completed"
    assert len(provider.requests) == 1


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
    assert app.state.proactivity.process_next()["status"] == "completed"
    assert len(provider.requests) == 1
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
    assert app.state.proactivity.process_next()["status"] == "completed"
    with app.state.coordinator.store.transaction(write=True) as connection:
        connection.execute("UPDATE proactivity_candidates SET status='running', finished_at=NULL")
    assert ProactivityService(app.state.dialogue).process_next() is None
    assert decisions(runtime)[0]["reason"] == "receipt_recovered"
    events = client.get("/v1/events?limit=500").json()["events"]
    assert sum(event["type"] == "companion.proactive_completed" for event in events) == 1
    assert len(provider.requests) == 1


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
    provider.action = done.set
    with TestClient(app, headers={"Authorization": "Bearer test"}):
        assert done.wait(timeout=5)
    assert decisions(runtime)[0]["status"] == "completed"
    assert len(provider.requests) == 1
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
