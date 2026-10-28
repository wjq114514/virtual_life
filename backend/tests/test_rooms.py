"""Node rooms: the lifeform follows the room with the latest activity.

A node is a room. It is `occupied` while the companion's active_node_id matches
it and `empty` otherwise, but an empty room keeps its capabilities and tools.
`follow_room` is the single-party authoritative migration (no target handshake),
and auto-follow fires when a node reports user activity.
"""

from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.settings import Settings


class SilentProvider:
    def complete(self, request):
        from virtual_life.ports import ModelResult, ModelUsage

        return ModelResult("嗯。", "fake", ModelUsage(10, 2))


def build(tmp_path, **overrides):
    clock = Clock()
    settings = Settings(
        database_path=str(tmp_path / "rooms.sqlite"),
        api_token="test",
        model_base_url="http://model.invalid",
        model_name="fake",
        node_ttl_seconds=100000,
        lease_ttl_seconds=100000,
        **overrides,
    )
    app = create_app(settings, model_provider=SilentProvider())
    for service in (app.state.coordinator, app.state.personality, app.state.memory):
        service.clock = clock
    return app, clock


def register(app, node_id, label, capabilities):
    return app.state.coordinator.register_node(
        {
            "node_id": node_id,
            "label": label,
            "kind": "desktop",
            "capabilities": capabilities,
        }
    )


def test_room_state_reflects_occupancy_and_empty_keeps_tools(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        register(app, "study", "书房", ["text.display"])
        register(app, "kitchen", "厨房", ["text.display", "attention.observe"])
        # desk is occupied, the others are empty but still carry capabilities.
        nodes = {n["node_id"]: n for n in client.get("/v1/nodes").json()}
        assert nodes["desk"]["room_state"] == "occupied"
        assert nodes["study"]["room_state"] == "empty"
        assert nodes["study"]["capabilities"] == ["text.display"]
        assert nodes["kitchen"]["capabilities"] == ["attention.observe", "text.display"]


def test_follow_room_moves_the_lifeform_and_empties_the_old_room(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        register(app, "study", "书房", ["text.display"])
        response = client.post(
            "/v1/companions/sui/follow",
            json={
                "command_id": "follow-1",
                "target_node_id": "study",
                "expected_revision": state["revision"],
                "reason": "user_walked_to_study",
            },
        )
        assert response.status_code == 200, response.text
        companion = response.json()
        assert companion["active_node_id"] == "study"
        assert companion["revision"] == state["revision"] + 1
        assert companion["epoch"] == state["epoch"] + 1
        assert companion["last_activity_node_id"] == "study"
        nodes = {n["node_id"]: n for n in client.get("/v1/nodes").json()}
        assert nodes["study"]["room_state"] == "occupied"
        assert nodes["desk"]["room_state"] == "empty"
        assert set(nodes["desk"]["capabilities"]) == {
            "avatar.render",
            "text.display",
            "reading.context",
        }  # tools remain


def test_follow_room_to_offline_target_is_rejected(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/follow",
            json={
                "command_id": "follow-offline",
                "target_node_id": "ghost",
                "expected_revision": state["revision"],
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] in ("target_missing", "not_found")


def test_follow_room_to_same_room_refreshes_lease_without_revision_bump(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/follow",
            json={
                "command_id": "follow-same",
                "target_node_id": "desk",
                "expected_revision": state["revision"],
            },
        )
        assert response.status_code == 200, response.text
        companion = response.json()
        assert companion["active_node_id"] == "desk"
        # same room: no revision/epoch bump, but activity stamp is refreshed.
        assert companion["revision"] == state["revision"]
        assert companion["last_activity_node_id"] == "desk"


def test_auto_follow_fires_on_user_initiated_observation(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        register(app, "phone", "手机", ["text.display", "vision.capture"])
        # The user interacts with the phone: the lifeform follows.
        response = client.post(
            "/v1/nodes/phone/observations",
            json={
                "command_id": "obs-1",
                "modality": "ambient",
                "data": {"hint": "user active"},
                "user_initiated": True,
            },
        )
        assert response.status_code == 201, response.text
        companion = client.get("/v1/companions/sui").json()
        assert companion["active_node_id"] == "phone"


def test_auto_follow_respects_quiet_window(tmp_path):
    app, _clock = build(tmp_path, auto_follow_quiet_seconds=60)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        register(app, "study", "书房", ["text.display"])
        register(app, "kitchen", "厨房", ["text.display"])
        # First follow to study.
        client.post(
            "/v1/companions/sui/follow",
            json={
                "command_id": "f1",
                "target_node_id": "study",
                "expected_revision": state["revision"],
            },
        )
        # A second room reports activity within the quiet window: no move.
        client.post(
            "/v1/nodes/kitchen/observations",
            json={"command_id": "obs-q", "modality": "ambient", "data": {}, "user_initiated": True},
        )
        companion = client.get("/v1/companions/sui").json()
        assert companion["active_node_id"] == "study"


def test_auto_follow_disabled_keeps_position(tmp_path):
    app, _clock = build(tmp_path, auto_follow_rooms=False)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        register(app, "study", "书房", ["text.display"])
        client.post(
            "/v1/nodes/study/observations",
            json={
                "command_id": "obs-off",
                "modality": "ambient",
                "data": {},
                "user_initiated": True,
            },
        )
        companion = client.get("/v1/companions/sui").json()
        assert companion["active_node_id"] == "desk"


def test_follow_room_is_idempotent_by_command_id(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        register(app, "study", "书房", ["text.display"])
        payload = {
            "command_id": "follow-idem",
            "target_node_id": "study",
            "expected_revision": state["revision"],
        }
        first = client.post("/v1/companions/sui/follow", json=payload)
        second = client.post("/v1/companions/sui/follow", json=payload)
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json()


def test_attention_sample_with_user_present_triggers_follow(tmp_path):
    app, clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        register(app, "phone", "手机", ["text.display", "attention.observe"])
        # The user is at the phone: an attention sample with user_present flows
        # the lifeform there.
        response = client.post(
            "/v1/nodes/phone/attention",
            json={
                "command_id": "att-1",
                "sample": {
                    "session_id": "sess-1",
                    "sequence": 1,
                    "sampled_at": clock.value.isoformat(),
                    "activity": "chat",
                    "focus": "focused",
                    "availability": "available",
                    "user_present": True,
                    "locked": False,
                    "meeting": False,
                    "screen_sharing": False,
                    "boundary": "none",
                },
            },
        )
        assert response.status_code == 200, response.text
        companion = client.get("/v1/companions/sui").json()
        assert companion["active_node_id"] == "phone"


def test_follow_room_rejected_while_transfer_pending(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        register(app, "study", "书房", ["text.display"])
        register(app, "phone", "手机", ["text.display"])
        # An explicit two-party transfer to phone is prepared but not acked.
        transfer = client.post(
            "/v1/companions/sui/transfers",
            json={
                "command_id": "tx-pending",
                "target_node_id": "phone",
                "expected_revision": state["revision"],
            },
        )
        assert transfer.status_code in (200, 201), transfer.text
        # A room-follow impulse must lose to the pending handshake.
        follow = client.post(
            "/v1/companions/sui/follow",
            json={
                "command_id": "follow-while-tx",
                "target_node_id": "study",
                "expected_revision": state["revision"],
            },
        )
        assert follow.status_code == 409
        assert follow.json()["error"]["code"] == "transfer_in_progress"
        companion = client.get("/v1/companions/sui").json()
        assert companion["active_node_id"] == "desk"


def test_follow_room_emits_events_and_room_state(tmp_path):
    app, _clock = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        register(app, "study", "书房", ["text.display"])
        response = client.post(
            "/v1/companions/sui/follow",
            json={
                "command_id": "follow-events",
                "target_node_id": "study",
                "expected_revision": state["revision"],
            },
        )
        assert response.status_code == 200
        events = client.get("/v1/events?after_sequence=0").json()["events"]
        types = [event["type"] for event in events]
        assert "companion.room_followed" in types
        payload = next(
            event["payload"] for event in events if event["type"] == "companion.room_followed"
        )
        assert payload["from_node_id"] == "desk"
        assert payload["to_node_id"] == "study"
        assert payload["reason"] == "room_activity"
