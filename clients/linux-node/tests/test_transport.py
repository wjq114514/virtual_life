import json

import httpx
import pytest

from virtual_life_linux.transport import (
    AuthError,
    ConflictError,
    FeatureUnsupportedError,
    ServerTransport,
    iter_sse,
)


def make_transport(handler) -> ServerTransport:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://testserver",
        headers={"Authorization": "Bearer secret"},
    )
    return ServerTransport("http://testserver", "secret", client=client)


async def test_register_and_heartbeat(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"node_id": "node-a", "online": True})

    transport = make_transport(handler)
    result = await transport.register_node(
        node_id="node-a", label="ws", capabilities=["device.state.report"], device_id="d"
    )
    assert result["node_id"] == "node-a"
    assert seen["url"] == "http://testserver/v1/nodes"
    assert seen["auth"] == "Bearer secret"
    assert seen["json"]["device"]["device_id"] == "d"


async def test_conflict_maps_to_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "presentation_busy", "message": "busy"}},
        )

    transport = make_transport(handler)
    with pytest.raises(ConflictError) as excinfo:
        await transport.open_presentation("node-a", "pid")
    assert excinfo.value.code == "presentation_busy"
    assert excinfo.value.status_code == 409


async def test_unauthorized_maps_to_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    transport = make_transport(handler)
    with pytest.raises(AuthError):
        await transport.heartbeat("node-a")


async def test_require_native_presentation_rejects_legacy_server():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"protocol_version": "1.0"})

    transport = make_transport(handler)
    with pytest.raises(FeatureUnsupportedError):
        await transport.require_native_presentation()


async def test_stream_dialogue_parses_sse():
    body = (
        'event: status\ndata: {"command_id":"c1","stage":"preparing"}\n\n'
        'event: delta\ndata: {"command_id":"c1","sequence":1,"text":"hi"}\n\n'
        'event: result\ndata: {"command_id":"c1","status":"completed"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    transport = make_transport(handler)
    events = [event async for event in transport.stream_dialogue("comp", {"command_id": "c1"})]
    assert [name for name, _ in events] == ["status", "delta", "result"]
    assert events[1][1]["text"] == "hi"


async def test_iter_sse_ignores_comments_and_pings():
    response = httpx.Response(
        200,
        content=b': ping\n\nevent: delta\ndata: {"a": 1}\n\n',
    )

    pairs = [(name, data) async for name, data in iter_sse(response)]
    assert pairs == [("delta", {"a": 1})]
