"""Clarification fences autonomous output while preserving explicit quiet gates."""

from datetime import timedelta

import test_proactivity
from test_proactivity import decisions, observe, policy

runtime = test_proactivity.runtime


def user_turn(runtime, message):
    client, _, _, _, state = runtime
    response = client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": "user-emotion",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": message,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    return response.json()


def clarify(runtime, event_id):
    client = runtime[0]
    current = client.get("/v1/companions/sui/emotion").json()
    response = client.post(
        f"/v1/companions/sui/emotion/experiences/{event_id}/correct",
        json={
            "command_id": "clarify",
            "expected_revision": current["revision"],
            "kind": "direction",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_correction_during_proactive_generation_discards_and_settles_usage(runtime):
    policy(runtime)
    client, app, clock, provider, _ = runtime
    turn = user_turn(runtime, "谢谢你陪我读书")
    before = client.get("/v1/companions/sui/emotion/experiences").json()["items"]
    clock.value += timedelta(seconds=31)
    budget_before = app.state.memory.budget_snapshot("sui")["spent_tokens"]
    observe(runtime)
    assert decisions(runtime)[-1]["status"] == "queued"
    provider.action = lambda: clarify(runtime, turn["accepted_event_id"])
    result = app.state.proactivity.process_next()
    assert result["status"] == "discarded"
    decision = decisions(runtime)[-1]
    receipt = client.get("/v1/companions/sui/dialogue/" + decision["command_id"]).json()
    assert receipt["assistant_message"] is None
    assert receipt["usage"]["input_tokens"] == 80
    assert receipt["usage"]["output_tokens"] == 12
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == budget_before + 92
    events = client.get("/v1/events?limit=500").json()["events"]
    assert not any(event["type"] == "companion.proactive_completed" for event in events)
    after = client.get("/v1/companions/sui/emotion/experiences").json()["items"]
    assert [item["event_id"] for item in after] == [item["event_id"] for item in before]
    assert after[0]["status"] == "corrected"
    assert len(provider.requests) == 2


def test_direction_clears_emotion_quiet_but_explicit_mode_still_gates(runtime):
    policy(runtime)
    client, app, clock, provider, state = runtime
    turn = user_turn(runtime, "先安静陪我读一会儿")
    clock.value += timedelta(seconds=31)
    observe(runtime)
    assert decisions(runtime)[-1]["reason"] == "quiet_requested"
    assert app.state.proactivity.process_next() is None
    assert clarify(runtime, turn["accepted_event_id"])["quiet_requested"] is False
    app.state.coordinator.patch_context(
        "sui",
        {
            "command_id": "explicit-quiet",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "interaction_mode": "quiet",
        },
    )
    observe(runtime, 2)
    assert decisions(runtime)[-1]["reason"] == "waiting_for_user"
    assert app.state.proactivity.process_next() is None
    assert len(provider.requests) == 1
