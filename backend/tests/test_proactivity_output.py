"""Current browser output is distinct from reading a historical dialogue receipt."""

from datetime import timedelta

import pytest
import test_proactivity
from test_proactivity import observe, policy

runtime = test_proactivity.runtime
PATH = "/v1/companions/sui/proactivity/output"


def poll(runtime, after=None, **changes):
    state = runtime[4]
    query = {"node_id": "desk", "access_id": "browser-access", "epoch": state["epoch"]}
    if after is not None:
        query["after_sequence"] = after
    return runtime[0].get(PATH, params={**query, **changes})


def subscribe(runtime):
    response = runtime[0].post(
        "/v1/nodes/access/web",
        json={
            "browser_id": "browser-one",
            "access_id": "browser-access",
            "bind_node_id": "desk",
        },
    )
    assert response.status_code == 201, response.text
    baseline = poll(runtime)
    assert baseline.status_code == 200, baseline.text
    assert baseline.json()["items"] == []
    assert baseline.headers["cache-control"] == "no-store"
    return baseline.json()["next_sequence"]


def complete(runtime):
    policy(runtime)
    observe(runtime)
    result = runtime[1].state.proactivity.process_next()
    assert result["status"] == "completed", result
    return result


def test_subscription_baseline_and_current_output_without_new_model_calls(runtime):
    cursor = subscribe(runtime)
    result = complete(runtime)
    response = poll(runtime, cursor)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["access_id"] == "browser-access"
    assert page["companion_revision"] == runtime[4]["revision"]
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["assistant_message"] == "歇一会儿，我在。"
    assert "user_message" not in item
    assert item["sequence"] <= page["next_sequence"]
    assert poll(runtime, page["next_sequence"]).json()["items"] == []
    assert poll(runtime).json()["items"] == []
    assert len(runtime[3].requests) == 1
    assert result["candidate_id"]


def test_new_access_cannot_receive_previous_access_output_even_with_old_cursor(runtime):
    complete(runtime)
    subscribe(runtime)
    assert poll(runtime, 0).json()["items"] == []
    assert len(runtime[3].requests) == 1


def test_receipt_commit_before_candidate_status_update_still_delivers(runtime):
    cursor = subscribe(runtime)
    completed = complete(runtime)
    with runtime[1].state.coordinator.store.transaction(write=True) as connection:
        connection.execute(
            "UPDATE proactivity_candidates SET status='running' WHERE candidate_id=?",
            (completed["candidate_id"],),
        )
    assert len(poll(runtime, cursor).json()["items"]) == 1


@pytest.mark.parametrize(
    "change", ["focused", "release_hold", "attention_stale", "quiet", "policy", "profile", "state"]
)
def test_changed_context_discards_output_and_advances_cursor(runtime, change):
    cursor = subscribe(runtime)
    complete(runtime)
    client, app, clock, _, state = runtime
    if change == "focused":
        observe(runtime, 2, focus="focused", boundary="none", boundary_id=None)
    elif change == "release_hold":
        observe(runtime, 2, focus="focused", boundary="none", boundary_id=None)
        clock.value += timedelta(seconds=5)
        observe(runtime, 3, boundary="none", boundary_id=None)
    elif change == "attention_stale":
        clock.value += timedelta(seconds=91)
    elif change == "quiet":
        app.state.coordinator.patch_context(
            "sui",
            {
                "command_id": "quiet-after-generation",
                "node_id": "desk",
                "epoch": state["epoch"],
                "expected_revision": state["revision"],
                "interaction_mode": "quiet",
            },
        )
    elif change == "policy":
        policy(runtime, enabled=False)
    elif change == "profile":
        current = client.get("/v1/companions/sui/personality").json()
        app.state.personality.update(
            "sui",
            {
                "command_id": "profile-after-generation",
                "expected_revision": current["revision"],
                "definition": current["definition"],
            },
        )
    else:
        current = client.get("/v1/companions/sui/personality/state").json()
        response = client.post(
            "/v1/companions/sui/personality/state",
            json={
                "command_id": "state-after-generation",
                "expected_revision": current["revision"],
                "state": {"mood": "calm", "energy": 0.8, "social_battery": 0.7},
                "ttl_seconds": 30,
            },
        )
        assert response.status_code == 200, response.text
    response = poll(runtime, cursor)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["next_sequence"] > cursor
    assert len(runtime[3].requests) == 1


def test_state_expiry_without_revision_change_invalidates_output(runtime):
    cursor = subscribe(runtime)
    client = runtime[0]
    response = client.post(
        "/v1/companions/sui/personality/state",
        json={
            "command_id": "brief-state",
            "expected_revision": 0,
            "state": {"mood": "calm", "energy": 0.9, "social_battery": 0.9},
            "ttl_seconds": 30,
        },
    )
    assert response.status_code == 200, response.text
    complete(runtime)
    runtime[2].value += timedelta(seconds=31)
    assert poll(runtime, cursor).json()["items"] == []


@pytest.mark.parametrize("change", ["focused", "meeting", "unknown", "new_boundary"])
def test_interrupted_remark_never_replays_after_attention_recovers(runtime, change):
    cursor = subscribe(runtime)
    complete(runtime)
    if change == "focused":
        observe(runtime, 2, focus="focused", boundary="none", boundary_id=None)
    elif change == "meeting":
        observe(runtime, 2, meeting=True, boundary="none", boundary_id=None)
    elif change == "unknown":
        observe(runtime, 2, focus="unknown", boundary="none", boundary_id=None)
    else:
        observe(runtime, 2, boundary_id="different-boundary")
    runtime[2].value += timedelta(seconds=1)
    observe(runtime, 3, boundary="none", boundary_id=None)
    runtime[2].value += timedelta(seconds=30)
    observe(runtime, 4, boundary="none", boundary_id=None)
    # Live state is safe again and the original candidate has not expired.
    assert not runtime[1].state.proactivity.focus.get("desk")["protection_active"]
    response = poll(runtime, cursor)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert len(runtime[3].requests) == 1


@pytest.mark.parametrize(
    "change", ["released", "wrong_epoch", "wrong_node", "wrong_access", "lease"]
)
def test_current_access_and_ownership_are_required(runtime, change):
    cursor = subscribe(runtime)
    complete(runtime)
    overrides = {}
    if change == "released":
        assert runtime[0].delete("/v1/nodes/desk/access/browser-access").status_code == 200
    elif change == "wrong_epoch":
        overrides["epoch"] = runtime[4]["epoch"] + 1
    elif change == "wrong_node":
        overrides["node_id"] = "phone"
    elif change == "wrong_access":
        overrides["access_id"] = "not-mine"
    else:
        with runtime[1].state.coordinator.store.transaction(write=True) as connection:
            connection.execute(
                "UPDATE companions SET lease_expires_at=NULL WHERE companion_id='sui'"
            )
    assert poll(runtime, cursor, **overrides).status_code == 409
    assert len(runtime[3].requests) == 1


def test_output_authentication_and_cursor_validation(runtime):
    subscribe(runtime)
    params = {"node_id": "desk", "access_id": "browser-access", "epoch": runtime[4]["epoch"]}
    assert runtime[0].get(PATH, params=params, headers={"Authorization": ""}).status_code == 401
    assert poll(runtime, -1).status_code == 422
    assert poll(runtime, 2**63 - 1).status_code == 409
    assert poll(runtime, node_id="").status_code == 422
