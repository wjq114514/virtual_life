"""Integration checks for the public transport contracts."""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from virtual_life.api import _json_safe, create_app
from virtual_life.settings import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_path=str(tmp_path / "runtime.sqlite3"), api_token="test-token")
    with TestClient(create_app(settings), headers={"Authorization": "Bearer test-token"}) as client:
        yield client


def setup_nodes(client):
    for node_id, kind in (("desk", "desktop"), ("phone", "android")):
        response = client.post(
            "/v1/nodes",
            json={
                "node_id": node_id,
                "label": node_id,
                "kind": kind,
                "capabilities": ["avatar.render", "text.display", "reading.context"],
            },
        )
        assert response.status_code == 201, response.text
    response = client.post("/v1/companions", json={"companion_id": "sui", "name": "随"})
    assert response.status_code == 201, response.text
    return response.json()


def attach_desktop(client):
    setup_nodes(client)
    response = client.post(
        "/v1/companions/sui/transfers",
        json={
            "command_id": "initial",
            "target_node_id": "desk",
            "expected_revision": 0,
        },
    )
    assert response.status_code == 201, response.text
    transfer_id = response.json()["transfer_id"]
    response = client.post(
        f"/v1/transfers/{transfer_id}/ack", json={"node_id": "desk", "phase": "ready"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "committed"
    return client.get("/v1/companions/sui").json()


def companion_source(client, companion_id="sui", node_id=None):
    page = client.get("/v1/events?limit=500").json()
    return next(
        event["event_id"]
        for event in reversed(page["events"])
        if event["companion_id"] == companion_id
        and (node_id is None or event["node_id"] == node_id)
    )


def test_auth_and_openapi(client):
    assert client.get("/healthz", headers={"Authorization": ""}).status_code == 200
    assert client.get("/v1/nodes", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.get("/v1/nodes", headers={"Authorization": "Bearer wrong"}).headers[
            "www-authenticate"
        ]
        == "Bearer"
    )
    document = client.get("/openapi.json").json()
    assert "/v1/companions/{companion_id}/transfers" in document["paths"]
    assert document["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert (
        client.post("/v1/nodes", json={"node_id": "", "label": "x", "kind": "android"}).status_code
        == 422
    )
    assert client.get("/v1/events?after_sequence=-1").status_code == 422


def test_travel_preserves_context_and_memory_budget(client):
    state = attach_desktop(client)
    response = client.post(
        "/v1/companions/sui/context",
        json={
            "command_id": "reading",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "activity": {"type": "co_reading", "book_id": "夜航船", "paragraph": 12},
            "context": {"topic": "窗外的雨"},
        },
    )
    assert response.status_code == 200, response.text
    state = response.json()
    memory = client.post(
        "/v1/companions/sui/memories",
        json={
            "command_id": "memory",
            "kind": "episode",
            "content": "一起读夜航船，看窗外的雨。",
            "source_event_id": companion_source(client, node_id="desk"),
            "source_node_id": "desk",
            "importance": 0.9,
        },
    )
    assert memory.status_code == 201, memory.text
    response = client.post(
        "/v1/companions/sui/transfers",
        json={
            "command_id": "to-phone",
            "target_node_id": "phone",
            "expected_revision": state["revision"],
        },
    )
    assert response.status_code == 201, response.text
    transfer_id = response.json()["transfer_id"]
    response = client.post(
        f"/v1/transfers/{transfer_id}/ack", json={"node_id": "phone", "phase": "ready"}
    )
    assert response.json()["status"] == "prepared"
    response = client.post(
        f"/v1/transfers/{transfer_id}/ack", json={"node_id": "desk", "phase": "released"}
    )
    assert response.status_code == 200 and response.json()["status"] == "committed"
    migrated = client.get("/v1/companions/sui").json()
    assert migrated["active_node_id"] == "phone"
    assert migrated["activity"] == state["activity"] and migrated["context"] == state["context"]
    assert migrated["epoch"] == state["epoch"] + 1
    for budget in (1, 16, 800):
        response = client.post(
            "/v1/companions/sui/memories/recall",
            json={
                "query": "夜航船",
                "token_budget": budget,
                "max_items": 8,
            },
        )
        assert response.status_code == 200, response.text
        recall = response.json()
        assert recall["estimated_tokens"] <= budget and recall["model_calls"] == 0
        if budget == 800:
            assert memory.json()["memory_id"] in {item["memory_id"] for item in recall["items"]}
    stale = client.post(
        "/v1/companions/sui/context",
        json={
            "command_id": "old-node",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": migrated["revision"],
            "context": {"topic": "stale"},
        },
    )
    assert stale.status_code == 409


def test_memory_correction_and_persistence(tmp_path):
    settings = Settings(database_path=str(tmp_path / "restart.sqlite3"), api_token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(create_app(settings), headers=headers) as first:
        attach_desktop(first)
        source = companion_source(first)
        response = first.post(
            "/v1/companions/sui/memories",
            json={
                "command_id": "old",
                "kind": "preference",
                "content": "喜欢科幻小说",
                "semantic_key": "reading.preference",
                "source_event_id": source,
                "confirmed": True,
                "importance": 0.9,
            },
        )
        assert response.status_code == 201, response.text
        old = response.json()
        response = first.post(
            f"/v1/companions/sui/memories/{old['memory_id']}/corrections",
            json={
                "command_id": "new",
                "expected_revision": old["revision"],
                "content": "最近更喜欢历史小说",
                "source_event_id": source,
            },
        )
        assert response.status_code == 201, response.text
    with TestClient(create_app(settings), headers=headers) as second:
        assert second.get("/v1/companions/sui").json()["active_node_id"] == "desk"
        recall = second.post("/v1/companions/sui/memories/recall", json={"query": "小说"}).json()
        assert "最近更喜欢历史小说" in recall["context_text"]
        assert "喜欢科幻小说" not in recall["context_text"]
        assert second.get("/v1/companions/missing/memories").status_code == 404


def test_websocket_authentication_and_replay(client):
    setup_nodes(client)
    with client.websocket_connect("/v1/nodes/phone/events") as socket:
        socket.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
        assert exc.value.code == 4401
    # Snapshot the log after the failed handshake: the refusal is itself recorded
    # as a node.auth_failed event, so an earlier snapshot would be stale.
    page = client.get("/v1/events", params={"node_id": "phone"}).json()
    assert any(event["type"] == "node.auth_failed" for event in page["events"])
    with client.websocket_connect("/v1/nodes/phone/events") as socket:
        socket.send_json({"type": "auth", "token": "test-token", "after_sequence": 0})
        assert socket.receive_json()["type"] == "welcome"
        received = []
        while True:
            frame = socket.receive_json()
            if frame["type"] == "event":
                received.append(frame["event"]["sequence"])
            if frame["type"] == "cursor" and frame["sequence"] >= page["next_sequence"]:
                break
        assert received == [event["sequence"] for event in page["events"]]
        socket.send_json({"type": "ping"})
        assert socket.receive_json()["type"] == "pong"


def test_websocket_lifecycle_and_rejections_are_audited(client):
    setup_nodes(client)
    with client.websocket_connect("/v1/nodes/phone/events") as socket:
        socket.send_json({"type": "auth", "token": "test-token", "after_sequence": 2**63})
        with pytest.raises(WebSocketDisconnect) as cursor:
            socket.receive_json()
        assert cursor.value.code == 4400
    with client.websocket_connect("/v1/nodes/missing/events") as socket:
        socket.send_json({"type": "auth", "token": "test-token"})
        with pytest.raises(WebSocketDisconnect) as unknown:
            socket.receive_json()
        assert unknown.value.code == 4404
    with client.websocket_connect("/v1/nodes/phone/events") as socket:
        socket.send_json({"type": "auth", "token": "test-token", "after_sequence": 0})
        assert socket.receive_json()["type"] == "welcome"
    events = client.get("/v1/events", params={"limit": 500}).json()["events"]
    types = [event["type"] for event in events]
    failures = [event for event in events if event["type"] == "node.auth_failed"]
    assert {(event["payload"]["code"], event["payload"]["stage"]) for event in failures} == {
        (4400, "auth"),
        (4404, "auth"),
    }
    # The stream only ends after the client side closes, so the disconnect audit
    # must land after the node registration that made the socket available.
    assert "node.disconnected" in types
    assert types.index("node.registered") < types.index("node.disconnected")


def test_vision_observation_requires_host_capability(client):
    setup_nodes(client)
    response = client.post(
        "/v1/nodes/phone/observations",
        json={
            "command_id": "vision",
            "modality": "vision",
            "data": {"description": "一盆植物"},
            "user_initiated": True,
        },
    )
    assert response.status_code == 409


def test_non_finite_request_values_get_a_json_422(client):
    """NaN/Infinity must become a 422, never a 500.

    JSONResponse renders with allow_nan=False, and pydantic echoes the offending
    value under "input". Left unsanitised the ValueError escaped the validation
    handler, produced a 500 with a truncated non-JSON body, and tore down the
    keep-alive connection mid-response so the next pooled request failed.
    """
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = (
            b'{"node_id": "nan-node", "label": "n", "kind": "android", '
            b'"capabilities": ["text.display", ' + literal.encode() + b"}]"
        )
        try:
            response = client.post(
                "/v1/nodes",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
        except ValueError:
            pytest.fail(
                "the 422 handler raised while rendering its body, so the client "
                "saw an exception instead of a 422"
            )
        # A renderer failure would otherwise surface as a 500 with a plain-text
        # body instead of a 422.
        assert response.status_code == 422, (response.status_code, response.text[:120])
        print("    [probe]", literal, "LEN=", len(raw), "BODY=", response.text[:200])
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()
    # The same pooled connection must still be usable afterwards.
    assert client.get("/v1/nodes").status_code == 200


def test_validation_error_response_is_always_encodable(client):
    """The 422 body must always survive JSONResponse's allow_nan=False render.

    JSONResponse renders with allow_nan=False. Pydantic echoes the offending
    value under "input", and a value_error ctx can hold the raised exception
    itself, so a raw error payload raises ValueError out of the handler: the
    client got an HTTP 500 with a truncated text/plain body and the keep-alive
    connection came back unusable. _json_safe is the handler's sanitiser.
    """
    sanitized = _json_safe(
        [
            {
                "type": "string_type",
                "loc": ("body", "capabilities", 1),
                "msg": "Input should be a valid string",
                "input": float("nan"),
            },
            {
                "type": "value_error",
                "loc": ("body",),
                "msg": "Value error",
                "input": {"limit": float("inf"), "ok": 3},
                "ctx": {"error": ValueError("boom")},
            },
        ]
    )
    assert json.dumps(sanitized, allow_nan=False)
    for error in (
        {"input": float("nan")},
        {"input": float("inf")},
        {"input": ValueError("x")},
        {"input": {"nested": float("-inf")}},
        {"input": [float("nan"), "ok"]},
    ):
        assert json.dumps(_json_safe(error), allow_nan=False)
    assert _json_safe({"input": 2, "name": "desk", "flag": True, "none": None}) == {
        "input": 2,
        "name": "desk",
        "flag": True,
        "none": None,
    }


def test_non_finite_request_values_never_return_a_500(client):
    """A non-finite request value must become a 422, never a 500.

    httpx refuses to build a body containing NaN, so the raw bytes are passed
    through unchanged the way a real network client would send them.
    """
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = (
            b'{"node_id": "nan-node", "label": "n", "kind": "android", '
            b'"capabilities": ["text.display", ' + literal.encode() + b"}]}"
        )
        try:
            response = client.post(
                "/v1/nodes", content=raw, headers={"Content-Type": "application/json"}
            )
        except ValueError:
            pytest.fail(
                "the 422 handler raised while rendering its body; the client saw "
                "an exception instead of a 422"
            )
        assert response.status_code == 422, (response.status_code, response.text[:120])
        assert response.headers["content-type"].startswith("application/json")
        assert json.dumps(response.json(), allow_nan=False)
    # The same pooled connection must still be usable afterwards.
    assert client.get("/v1/nodes").status_code == 200


def test_preview_rejects_a_blank_user_message(client):
    """Whitespace-only input must not be accepted, billed or persisted."""
    setup_nodes(client)
    for blank in ("", "   ", "\t\n"):
        response = client.post(
            "/v1/companions/sui/personality/preview", json={"user_message": blank}
        )
        assert response.status_code == 422, response.text
    padded = client.post(
        "/v1/companions/sui/personality/preview", json={"user_message": "   林晚舟   "}
    )
    assert padded.status_code == 200, padded.text
    prompt = padded.json()["context_text"]
    assert "林晚舟" in prompt
    assert 'content": "   林晚舟' not in prompt, "padding must be stripped before the prompt"
    blank_history = client.post(
        "/v1/companions/sui/personality/preview",
        json={"user_message": "hi", "history": [{"role": "user", "content": "  "}]},
    )
    assert blank_history.status_code == 422, blank_history.text
