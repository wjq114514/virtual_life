"""Proactive outreach feedback loop: reply credit and timeout dip."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_inner_life import (
    Provider,
    enable_proactivity,
    observe,
    send,
)
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.inner_life import InnerLifePolicy
from virtual_life.settings import Settings


def build_app(tmp_path):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "feedback.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        model_provider=provider,
        inner_life_policy=InnerLifePolicy(
            min_activity_gap_seconds=30,
            activity_ttl_seconds=600,
            min_evidence_items=1,
            proactive_urge_threshold=0.2,
            proactive_min_idle_seconds=60,
            idle_saturation_seconds=120,
            max_history=10,
        ),
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.dialogue,
        app.state.memory,
    ):
        service.clock = clock
    return app, clock, provider


@pytest.fixture
def runtime(tmp_path):
    app, clock, provider = build_app(tmp_path)
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
    yield client, app, clock, provider, state
    client.close()


def _drive_one_proactive_message(runtime):
    """Bring one inner-origin proactive message to completed state."""
    client, app, clock, provider, state = runtime
    send(client, state, "one", "谢谢你陪我读书")
    provider.requests.clear()
    clock.value += timedelta(minutes=20)
    observe(client, clock)
    enable_proactivity(client)
    assert app.state.inner_life.tick() == 1
    client.post(
        "/v1/nodes/access/web",
        json={
            "browser_id": "inner-browser",
            "access_id": "inner-access",
            "bind_node_id": "desk",
        },
    )
    result = app.state.proactivity.process_next()
    assert result["status"] == "awaiting_generation"
    decisions = client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"]
    command_id = decisions[0]["command_id"]
    commit = client.post(
        f"/v1/companions/sui/dialogue/{command_id}/commit",
        json={"assistant_message": "我在，慢慢来。", "usage": None},
    )
    assert commit.status_code == 200, commit.text
    events = client.get("/v1/events?limit=500").json()["events"]
    completed = [event for event in events if event["type"] == "companion.proactive_completed"]
    assert len(completed) == 1
    fresh = client.get("/v1/companions/sui").json()
    return completed[0], fresh


def _feedback_events(client):
    events = client.get("/v1/events?limit=500").json()["events"]
    return [event for event in events if event["type"] == "companion.proactive_feedback"]


def test_user_reply_credits_proactive_outreach(runtime):
    client, app, clock, provider, state = runtime
    completed, fresh = _drive_one_proactive_message(runtime)
    candidate_id = completed["payload"]["candidate_id"]

    send(client, fresh, "two", "在的，刚才在忙")
    feedback = _feedback_events(client)
    assert len(feedback) == 1
    assert feedback[0]["payload"]["replied"] is True
    assert feedback[0]["payload"]["candidate_id"] == candidate_id
    assert feedback[0]["payload"]["latency_seconds"] >= 0

    experiences = client.get("/v1/companions/sui/emotion/experiences").json()["items"]
    credited = [item for item in experiences if item["event_id"] == feedback[0]["event_id"]]
    assert len(credited) == 1
    assert credited[0]["reaction"] == "joy"
    assert credited[0]["reason"] == "proactive_replied"

    # A second ordinary message never double-charges the same outreach.
    clock.value += timedelta(minutes=5)
    fresh = client.get("/v1/companions/sui").json()
    send(client, fresh, "three", "继续聊")
    assert len(_feedback_events(client)) == 1
    experiences = client.get("/v1/companions/sui/emotion/experiences").json()["items"]
    assert len([item for item in experiences if item["event_id"] == feedback[0]["event_id"]]) == 1


def test_unanswered_outreach_records_timeout_dip(runtime):
    client, app, clock, provider, state = runtime
    completed, fresh = _drive_one_proactive_message(runtime)
    candidate_id = completed["payload"]["candidate_id"]

    clock.value += timedelta(hours=3)
    app.state.proactivity.process_next()

    feedback = _feedback_events(client)
    assert len(feedback) == 1
    assert feedback[0]["payload"]["replied"] is False
    assert feedback[0]["payload"]["candidate_id"] == candidate_id
    experiences = client.get("/v1/companions/sui/emotion/experiences").json()["items"]
    dipped = [item for item in experiences if item["event_id"] == feedback[0]["event_id"]]
    assert len(dipped) == 1
    assert dipped[0]["reaction"] == "concern"
    assert dipped[0]["reason"] == "proactive_unanswered"

    # The timeout scan is idempotent per outreach.
    app.state.proactivity.process_next()
    assert len(_feedback_events(client)) == 1


def test_reply_after_window_does_not_credit(runtime):
    client, app, clock, provider, state = runtime
    completed, fresh = _drive_one_proactive_message(runtime)

    clock.value += timedelta(hours=7)
    send(client, fresh, "late", "刚看到，抱歉")
    feedback = _feedback_events(client)
    assert feedback == []
    experiences = client.get("/v1/companions/sui/emotion/experiences").json()["items"]
    assert not [
        item
        for item in experiences
        if item["reason"] in {"proactive_replied", "proactive_unanswered"}
    ]
