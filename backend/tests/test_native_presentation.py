"""Native terminal presentations, native proactive output, history and transfer binding."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from test_proactivity import Provider, observe, policy
from virtual_life.api import create_app
from virtual_life.settings import Settings


@pytest.fixture
def runtime(tmp_path):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "native.sqlite"),
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
    client = TestClient(app, headers={"Authorization": "Bearer test"})
    attach_desktop(client)
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
            "epoch": app.state.coordinator.get_companion("sui")["epoch"],
            "expected_revision": app.state.coordinator.get_companion("sui")["revision"],
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


def open_pres(client, presentation_id="p1", node="desk"):
    return client.post(f"/v1/nodes/{node}/presentations", json={"presentation_id": presentation_id})


def native_poll(client, presentation_id="p1", node="desk", **params):
    state = client.get("/v1/companions/sui").json()
    query = {"node_id": node, "presentation_id": presentation_id, "epoch": state["epoch"]}
    return client.get("/v1/companions/sui/proactivity/native-output", params={**query, **params})


def complete(runtime):
    policy(runtime)
    observe(runtime)
    result = runtime[1].state.proactivity.process_next()
    assert result["status"] == "completed", result
    return result


def test_presentation_lifecycle_is_idempotent_and_bounded(runtime):
    client = runtime[0]
    response = open_pres(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["node_id"] == "desk" and body["presentation_id"] == "p1"
    assert body["kind"] == "terminal" and body["ttl_seconds"] == 15
    assert body["opened_sequence"] >= 0
    assert response.headers.get("cache-control") is None or True
    heartbeat = client.post("/v1/nodes/desk/presentations/p1/heartbeat")
    assert heartbeat.status_code == 200, heartbeat.text
    assert client.delete("/v1/nodes/desk/presentations/p1").status_code == 200
    assert client.post("/v1/nodes/desk/presentations/p1/heartbeat").status_code == 409
    # Releasing an already released session stays idempotent.
    assert client.delete("/v1/nodes/desk/presentations/p1").status_code == 200
    assert client.delete("/v1/nodes/desk/presentations/missing").status_code == 404


def test_new_presentation_supersedes_the_previous_generation(runtime):
    client = runtime[0]
    assert open_pres(client, "p1").status_code == 201
    assert open_pres(client, "p2").status_code == 201
    assert client.post("/v1/nodes/desk/presentations/p1/heartbeat").status_code == 409
    assert client.post("/v1/nodes/desk/presentations/p2/heartbeat").status_code == 200


def test_expired_presentation_cannot_revive(runtime):
    client, _, clock, _, _ = runtime
    assert open_pres(client, "p1").status_code == 201
    clock.value += timedelta(seconds=16)
    assert client.post("/v1/nodes/desk/presentations/p1/heartbeat").status_code == 409
    assert native_poll(client, "p1").status_code == 409


def test_web_and_native_entry_points_are_mutually_exclusive(runtime):
    client = runtime[0]
    assert open_pres(client, "p1").status_code == 201
    web = client.post(
        "/v1/nodes/access/web",
        json={"browser_id": "browser", "access_id": "web-1", "bind_node_id": "desk"},
    )
    assert web.status_code == 409 and web.json()["error"]["code"] == "presentation_busy"
    assert client.delete("/v1/nodes/desk/presentations/p1").status_code == 200
    assert (
        client.post(
            "/v1/nodes/access/web",
            json={"browser_id": "browser", "access_id": "web-1", "bind_node_id": "desk"},
        ).status_code
        == 201
    )
    blocked = open_pres(client, "p2")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "presentation_busy"
    assert client.delete("/v1/nodes/desk/access/web-1").status_code == 200
    assert open_pres(client, "p3").status_code == 201


def test_native_output_is_baselined_to_the_session(runtime):
    client = runtime[0]
    assert open_pres(client, "p1").status_code == 201
    baseline = native_poll(client)
    assert baseline.status_code == 200, baseline.text
    page = baseline.json()
    assert page["presentation_id"] == "p1" and page["items"] == []
    cursor = page["next_sequence"]
    complete(runtime)
    delivered = native_poll(client, after_sequence=cursor).json()
    assert len(delivered["items"]) == 1
    assert delivered["items"][0]["assistant_message"] == "歇一会儿，我在。"
    assert native_poll(client).json()["items"] == []
    assert len(runtime[3].requests) == 1


def test_native_output_never_replays_before_the_session(runtime):
    complete(runtime)
    client = runtime[0]
    assert open_pres(client, "p1").status_code == 201
    assert native_poll(client, after_sequence=0).json()["items"] == []
    assert open_pres(client, "p2").status_code == 201
    assert native_poll(client, "p2", after_sequence=0).json()["items"] == []


def test_native_output_requires_a_live_presentation(runtime):
    client = runtime[0]
    assert open_pres(client, "p1").status_code == 201
    assert client.delete("/v1/nodes/desk/presentations/p1").status_code == 200
    response = native_poll(client, "p1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "presentation_expired"


def dialogue_payload(runtime, command_id, **extra):
    state = runtime[0].get("/v1/companions/sui").json()
    return {
        "command_id": command_id,
        "node_id": "desk",
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "user_message": "你好",
        **extra,
    }


def test_dialogue_binds_to_a_live_presentation(runtime):
    client = runtime[0]
    assert open_pres(client, "p1").status_code == 201
    response = client.post(
        "/v1/companions/sui/dialogue",
        json=dialogue_payload(runtime, "turn-1", presentation_id="p1"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert client.delete("/v1/nodes/desk/presentations/p1").status_code == 200
    stale = client.post(
        "/v1/companions/sui/dialogue",
        json=dialogue_payload(runtime, "turn-2", presentation_id="p1"),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "presentation_expired"
    both = client.post(
        "/v1/companions/sui/dialogue",
        json=dialogue_payload(runtime, "turn-3", presentation_id="p1", access_id="web"),
    )
    assert both.status_code == 422


def test_dialogue_history_lists_completed_pairs_newest_first(runtime):
    client = runtime[0]
    assert open_pres(client, "p1").status_code == 201
    for command in ("turn-1", "turn-2"):
        response = client.post(
            "/v1/companions/sui/dialogue",
            json=dialogue_payload(runtime, command, presentation_id="p1"),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
    page = client.get("/v1/companions/sui/dialogue/history").json()
    assert [item["command_id"] for item in page["items"]] == ["turn-2", "turn-1"]
    assert page["items"][0]["user_message"] == "你好"
    assert page["items"][0]["assistant_message"] == "歇一会儿，我在。"
    assert page["has_more"] is False
    assert page["next_before_sequence"] == page["items"][-1]["sequence"]
    # The cursor is exclusive: page before the newest item with a one-item limit.
    older = client.get(
        "/v1/companions/sui/dialogue/history",
        params={"before_sequence": page["items"][0]["sequence"], "limit": 1},
    ).json()
    assert [item["command_id"] for item in older["items"]] == ["turn-1"]
    assert older["has_more"] is False


def prepare(client, target="phone"):
    state = client.get("/v1/companions/sui").json()
    return client.post(
        "/v1/companions/sui/transfers",
        json={
            "command_id": f"move-{target}",
            "target_node_id": target,
            "expected_revision": state["revision"],
        },
    )


def test_target_ready_binds_a_live_presentation(runtime):
    client = runtime[0]
    assert open_pres(client, "pt", node="phone").status_code == 201
    prepared = prepare(client)
    assert prepared.status_code == 201, prepared.text
    transfer_id = prepared.json()["transfer_id"]
    rejected = client.post(
        f"/v1/transfers/{transfer_id}/ack",
        json={"node_id": "phone", "phase": "ready", "presentation_id": "missing"},
    )
    assert rejected.status_code == 409
    ready = client.post(
        f"/v1/transfers/{transfer_id}/ack",
        json={"node_id": "phone", "phase": "ready", "presentation_id": "pt"},
    )
    assert ready.status_code == 200, ready.text
    assert ready.json()["target_presentation_id"] == "pt"
    released = client.post(
        f"/v1/transfers/{transfer_id}/ack", json={"node_id": "desk", "phase": "released"}
    )
    assert released.json()["status"] == "committed"


def test_bound_presentation_must_stay_live_to_commit(runtime):
    client = runtime[0]
    assert open_pres(client, "pt", node="phone").status_code == 201
    prepared = prepare(client)
    transfer_id = prepared.json()["transfer_id"]
    assert (
        client.post(
            f"/v1/transfers/{transfer_id}/ack",
            json={"node_id": "phone", "phase": "ready", "presentation_id": "pt"},
        ).status_code
        == 200
    )
    assert client.delete("/v1/nodes/phone/presentations/pt").status_code == 200
    released = client.post(
        f"/v1/transfers/{transfer_id}/ack", json={"node_id": "desk", "phase": "released"}
    )
    assert released.json()["status"] == "aborted"


def test_released_phase_rejects_a_presentation_id(runtime):
    client = runtime[0]
    prepared = prepare(client)
    transfer_id = prepared.json()["transfer_id"]
    response = client.post(
        f"/v1/transfers/{transfer_id}/ack",
        json={"node_id": "desk", "phase": "released", "presentation_id": "pt"},
    )
    assert response.status_code == 422


def test_legacy_transfer_without_presentation_still_commits(runtime):
    client = runtime[0]
    prepared = prepare(client)
    transfer_id = prepared.json()["transfer_id"]
    assert (
        client.post(
            f"/v1/transfers/{transfer_id}/ack",
            json={"node_id": "phone", "phase": "ready"},
        ).status_code
        == 200
    )
    released = client.post(
        f"/v1/transfers/{transfer_id}/ack", json={"node_id": "desk", "phase": "released"}
    )
    assert released.json()["status"] == "committed"
