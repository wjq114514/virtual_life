"""Authenticated HTTP device resolution and access-fenced dialogue integration."""

import hashlib
import json

import pytest
import test_emotion
from fastapi.testclient import TestClient
from test_emotion import send
from virtual_life.api import create_app
from virtual_life.settings import Settings

runtime = test_emotion.runtime
AUTH = {"Authorization": "Bearer test"}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(database_path=str(tmp_path / "devices.sqlite"), api_token="test"))


def register(client, node_id="desk"):
    result = client.post(
        "/v1/nodes",
        json={
            "node_id": node_id,
            "label": node_id,
            "kind": "desktop",
            "capabilities": ["audio.output", "reading.context"],
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def open_access(client, access="access-one", browser="browser-one", bind=None, **kwargs):
    payload = {"browser_id": browser, "access_id": access}
    if bind is not None:
        payload["bind_node_id"] = bind
    return client.post("/v1/nodes/access/web", json=payload, **kwargs)


def release(client, node, access):
    result = client.delete(f"/v1/nodes/{node}/access/{access}")
    assert result.status_code == 200, result.text
    return result.json()


def test_bound_access_preserves_native_context_and_capabilities(app):
    with TestClient(app, headers=AUTH) as client:
        register(client)
        native = client.post("/v1/nodes/desk/heartbeat", json={"context": {"book": "native"}})
        assert native.status_code == 200
        opened = open_access(client, bind="desk")
        assert opened.status_code == 201, opened.text
        assert opened.json()["resolution"] == "bound"
        assert opened.json()["node"]["node_id"] == "desk"
        heartbeat = client.post(
            "/v1/nodes/desk/access/access-one/heartbeat", json={"context": {"book": "web"}}
        )
        assert heartbeat.status_code == 200, heartbeat.text
        node = heartbeat.json()["node"]
        assert node["context"] == {"book": "native"}
        assert set(node["capabilities"]) == {"audio.output", "reading.context", "text.display"}
        assert len(client.get("/v1/nodes").json()) == 1
        closed = release(client, "desk", "access-one")
        assert set(closed["capabilities"]) == {"audio.output", "reading.context"}
        assert closed["context"] == {"book": "native"}


def test_refresh_reuses_browser_device_but_another_live_access_is_busy(app):
    with TestClient(app, headers=AUTH) as client:
        first = open_access(client)
        assert first.status_code == 201, first.text
        node_id = first.json()["node"]["node_id"]
        retry = open_access(client)
        assert retry.status_code == 201, retry.text
        assert retry.json()["node"]["node_id"] == node_id
        assert retry.json()["access_id"] == "access-one"
        busy = open_access(client, access="access-two")
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "device_access_busy"
        for index in range(3):
            old_access = "access-one" if index == 0 else f"refresh-{index - 1}"
            release(client, node_id, old_access)
            refreshed = open_access(client, access=f"refresh-{index}")
            assert refreshed.status_code == 201, refreshed.text
            assert refreshed.json()["resolution"] == "browser_id"
            assert refreshed.json()["node"]["node_id"] == node_id
        assert len(client.get("/v1/nodes").json()) == 1


def test_unique_direct_lan_peer_resolves_existing_native_node(app):
    with TestClient(app, headers=AUTH, client=("192.168.12.34", 41000)) as client:
        register(client)
        opened = open_access(client)
        assert opened.status_code == 201, opened.text
        assert opened.json()["resolution"] == "lan_ip"
        assert opened.json()["node"]["node_id"] == "desk"
        assert "192.168.12.34" in opened.json()["node"]["device"]["ip_addresses"]
        assert len(client.get("/v1/nodes").json()) == 1


def test_public_nat_peer_does_not_merge_different_browsers(app):
    with TestClient(app, headers=AUTH, client=("8.8.8.8", 41001)) as client:
        first = open_access(client)
        second = open_access(client, access="access-two", browser="browser-two")
        assert first.status_code == second.status_code == 201
        assert first.json()["resolution"] == second.json()["resolution"] == "created"
        assert first.json()["node"]["node_id"] != second.json()["node"]["node_id"]


@pytest.mark.parametrize("header", ["forwarded", "x-forwarded-for"])
def test_forwarded_transport_never_uses_proxy_or_spoofed_ip_for_matching(app, header):
    with TestClient(app, headers=AUTH, client=("192.168.12.34", 41002)) as client:
        register(client)
        opened = open_access(client, headers={header: "192.168.12.34"})
        assert opened.status_code == 201, opened.text
        assert opened.json()["resolution"] == "created"
        assert opened.json()["node"]["node_id"] != "desk"
        body_ip = client.post(
            "/v1/nodes/access/web",
            json={"browser_id": "spoof", "access_id": "spoof", "peer_ip": "192.168.12.34"},
        )
        assert body_ip.status_code == 422


def test_access_endpoints_require_bearer_authentication(app):
    with TestClient(app, headers=AUTH) as client:
        register(client)
        assert open_access(client, bind="desk").status_code == 201
        for method, path, body in (
            ("POST", "/v1/nodes/access/web", {"browser_id": "b", "access_id": "a"}),
            ("POST", "/v1/nodes/desk/access/access-one/heartbeat", {}),
            ("DELETE", "/v1/nodes/desk/access/access-one", None),
        ):
            result = client.request(method, path, json=body, headers={"Authorization": ""})
            assert result.status_code == 401


@pytest.mark.parametrize("case", ["unknown", "closed", "wrong_node"])
def test_heartbeat_rejects_unknown_closed_or_wrong_node_access(app, case):
    with TestClient(app, headers=AUTH) as client:
        register(client)
        register(client, "phone")
        assert open_access(client, bind="desk").status_code == 201
        node, access = "desk", "access-one"
        if case == "unknown":
            access = "missing"
        elif case == "closed":
            release(client, node, access)
        else:
            node = "phone"
        result = client.post(f"/v1/nodes/{node}/access/{access}/heartbeat", json={})
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "node_access_expired"


def test_browser_association_survives_service_restart_and_peer_change(tmp_path):
    settings = Settings(database_path=str(tmp_path / "restart.sqlite"), api_token="test")
    with TestClient(create_app(settings), headers=AUTH, client=("8.8.8.8", 41003)) as client:
        opened = open_access(client)
        assert opened.status_code == 201, opened.text
        node_id = opened.json()["node"]["node_id"]
        release(client, node_id, "access-one")
    with TestClient(create_app(settings), headers=AUTH, client=("1.1.1.1", 41004)) as client:
        reopened = open_access(client, access="after-restart")
        assert reopened.status_code == 201, reopened.text
        assert reopened.json()["node"]["node_id"] == node_id
        assert reopened.json()["resolution"] == "browser_id"
        assert len(client.get("/v1/nodes").json()) == 1


def test_active_bound_access_can_complete_dialogue(runtime):
    client, _, _, provider, _ = runtime
    assert open_access(client, bind="desk").status_code == 201
    result = send(runtime, access_id="access-one")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    assert provider.calls == 1


def test_replaced_access_discards_old_output_and_keeps_usage_and_receipt(runtime):
    client, app, _, provider, _ = runtime
    assert open_access(client, bind="desk").status_code == 201

    def replace_access():
        release(client, "desk", "access-one")
        replacement = open_access(client, access="replacement")
        assert replacement.status_code == 201, replacement.text
        assert replacement.json()["node"]["node_id"] == "desk"

    provider.action = replace_access
    response = send(runtime, access_id="access-one")
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "discarded"
    assert receipt["assistant_message"] is None
    assert receipt["usage"] == {"input_tokens": 70, "output_tokens": 10}
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 80
    assert client.get("/v1/companions/sui/dialogue/turn").json() == receipt
    assert send(runtime, access_id="access-one").json() == receipt
    assert provider.calls == 1


def test_legacy_dialogue_fingerprint_stays_compatible_without_access_id(runtime):
    client, app, _, _, state = runtime
    response = send(runtime)
    assert response.status_code == 200, response.text
    # Independently reconstruct the original pre-access schema defaults, rather
    # than serializing the new DialogueRequest and reproducing its own mistake.
    legacy_payload = {
        "command_id": "turn",
        "node_id": "desk",
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "user_message": "谢谢你陪我读书",
        "user_name": "你",
        "user_initiated": True,
        "audio_requested": False,
        "history": [],
        "token_budget": 3072,
        "memory_token_budget": 800,
        "max_memory_items": 8,
        "max_output_tokens": 512,
    }
    encoded = json.dumps(
        {"companion_id": "sui", "payload": legacy_payload},
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    with app.state.coordinator.store.transaction() as connection:
        fingerprint = connection.execute(
            "SELECT fingerprint FROM dialogue_commands WHERE command_id='turn'"
        ).fetchone()[0]
    assert fingerprint == hashlib.sha256(encoded).hexdigest()
    assert send(runtime).json() == response.json()
    assert client.get("/v1/companions/sui/dialogue/turn").json() == response.json()


def test_web_access_open_and_release_are_audited(app):
    with TestClient(app, headers=AUTH) as client:
        opened = open_access(client)
        assert opened.status_code == 201, opened.text
        node_id = opened.json()["node"]["node_id"]
        release(client, node_id, "access-one")
        events = client.get("/v1/events", params={"node_id": node_id, "limit": 500}).json()[
            "events"
        ]
        types = [event["type"] for event in events]
        assert "node.web_access_opened" in types
        assert "node.web_access_released" in types
        assert types.index("node.web_access_opened") < types.index("node.web_access_released")
        released = next(event for event in events if event["type"] == "node.web_access_released")
        assert released["payload"]["access_id"] == "access-one"
        # The endpoint is idempotent; only the transition itself is audited.
        release(client, node_id, "access-one")
        again = client.get("/v1/events", params={"node_id": node_id, "limit": 500}).json()["events"]
        assert len(again) == len(events)
