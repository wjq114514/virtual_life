"""The MCP bridge: contract-driven tools, JSON-RPC dispatch, both transports.

Hermetic: the contract comes from the checked-in OpenAPI document and upstream
calls run through ``httpx.MockTransport``, so no live runtime is needed.
"""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from virtual_life.mcp_bridge import (
    Bridge,
    Protocol,
    build_http_app,
    load_operations,
    tool_name_for,
)

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"
VALID_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def spec() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def operations():
    return load_operations(spec())


def by_name(operations, name):
    return next(op for op in operations if op.name == name)


def test_contract_is_present():
    assert CONTRACT.exists(), "docs/openapi.json must be checked in"
    loaded = spec()
    assert loaded.get("openapi") or loaded.get("swagger")


def test_tools_are_generated_and_valid(operations):
    assert len(operations) >= 80, f"expected a broad surface, got {len(operations)}"
    names = [op.name for op in operations]
    assert len(names) == len(set(names)), "tool names must be unique"
    for name in names:
        assert VALID_NAME.match(name), f"invalid MCP tool name: {name}"
    for op in operations:
        assert op.schema["type"] == "object"
        assert op.schema["additionalProperties"] is False
        for prop in op.schema.get("required", []):
            assert prop in op.schema["properties"], f"{op.name}: required {prop} not declared"


def test_streaming_and_binary_operations_are_excluded(operations):
    names = {op.name for op in operations}
    paths = {op.path for op in operations}
    # SSE dialogue stream, audio bytes, turn event stream, raw upload body, webui
    # cookie handshake and package transfer are all unusable as tool results.
    assert "companions_companion_id_dialogue_stream" not in names
    assert "/v1/nodes/{node_id}/voice/sessions/{session_id}/turns/{turn_id}/events" not in paths
    assert (
        "/v1/nodes/{node_id}/voice/sessions/{session_id}/turns/{turn_id}/segments/{sequence}/audio"
        not in paths
    )
    assert "/v1/nodes/{node_id}/screen/uploads/{upload_id}/content" not in paths
    assert "/v1/companions/{companion_id}/dialogue/stream" not in paths
    assert not any(p.startswith("/v1/dist/packages") for p in paths)
    assert "/v1/session" not in paths
    assert "/v1/deployments/linux/bundle" not in paths
    # The JSON dialogue executor and the durable event log survive.
    assert "companions_companion_id_dialogue" in names
    assert "events_list" in names


def test_zero_argument_reads_are_kept(operations):
    names = {op.name for op in operations}
    assert {"capabilities_list", "graph_get", "nodes_list", "companions_list"} <= names
    op = by_name(operations, "capabilities_list")
    assert op.schema == {"type": "object", "properties": {}, "additionalProperties": False}


def test_memory_recall_tool_shape(operations):
    op = by_name(operations, "companions_companion_id_memories_recall")
    props = op.schema["properties"]
    assert op.mutates
    assert "companion_id" in props
    assert "command_id" in props, "mutations get a synthetic idempotency key"
    assert op.synthetic_command_id
    assert "semantic_key" in props
    assert "layer" in props and set(props["layer"]["enum"]) == {"episodic", "semantic"}
    assert "description" in props["command_id"]


def test_get_verb_reflects_single_vs_collection(operations):
    assert by_name(operations, "nodes_list").path == "/v1/nodes"
    assert by_name(operations, "nodes_node_id_get").path == "/v1/nodes/{node_id}"


def test_name_generation_rule():
    taken: set[str] = set()
    assert tool_name_for("get", "/v1/nodes", taken) == "nodes_list"
    assert tool_name_for("post", "/v1/nodes", taken) == "nodes_create"
    assert tool_name_for("get", "/v1/nodes/{node_id}", taken) == "nodes_node_id_get"
    assert (
        tool_name_for("post", "/v1/companions/{c}/memories/recall", taken)
        == "companions_c_memories_recall"
    )
    assert tool_name_for("post", "/v1/companions/{c}/heartbeat", taken) == "companions_c_heartbeat"
    # POST on a bare collection keeps the verb.
    assert tool_name_for("post", "/v1/companions", taken) == "companions_create"


def test_split_arguments_routes_query_and_body(operations):
    op = by_name(operations, "companions_companion_id_memories_recall")
    url, query, body = Bridge._split_arguments(
        op, {"companion_id": "c-1", "query": "hello", "limit": 3}
    )
    assert url == "/v1/companions/c-1/memories/recall"
    # 'query' and 'limit' are body fields of MemoryRecall, not URL query params.
    assert query == {}
    assert body == {"query": "hello", "limit": 3}
    # A real URL query parameter routes to the query string, not the body.
    state = by_name(operations, "nodes_node_id_screen_state_get")
    url, query, body = Bridge._split_arguments(
        state, {"node_id": "n-1", "companion_id": "c-1", "display_id": "main"}
    )
    assert url == "/v1/nodes/n-1/screen/state"
    assert query == {"companion_id": "c-1", "display_id": "main"}
    assert body == {}


def test_split_arguments_requires_path_params(operations):
    op = by_name(operations, "nodes_node_id_get")
    with pytest.raises(ValueError):
        Bridge._split_arguments(op, {})


def _upstream_responder(request: httpx.Request) -> httpx.Response:
    """A tiny fake runtime: one read, one create, one action, one 404."""
    if request.method == "GET" and request.url.path == "/v1/capabilities":
        return httpx.Response(
            200, json={"protocol_version": "1.0", "runtime_mode": "single_owner_local"}
        )
    if request.method == "GET" and request.url.path == "/v1/companions":
        return httpx.Response(200, json=[])
    if request.method == "POST" and request.url.path == "/v1/companions":
        assert request.headers.get("authorization") == "Bearer secret"
        assert request.headers.get("idempotency-key"), "mutations must carry an idempotency key"
        return httpx.Response(201, json={"companion_id": "c-1"})
    if request.method == "POST" and "/memories/recall" in request.url.path:
        return httpx.Response(200, json={"items": [], "budget": {"tokens": 800}})
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "no such route"}})


@pytest.fixture
def bridge():
    return Bridge(
        spec(),
        base_url="http://runtime.local",
        token="secret",
        timeout=5.0,
        transport=httpx.MockTransport(_upstream_responder),
    )


@pytest.fixture
def protocol(bridge):
    return Protocol(bridge, "test instructions")


@pytest.mark.anyio
async def test_call_tool_success(protocol):
    reply = (
        await protocol.dispatch_async(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "capabilities_list", "arguments": {}},
            }
        )
    )[0]
    assert reply["id"] == 1 and "error" not in reply
    result = reply["result"]
    assert result["isError"] is False
    assert "single_owner_local" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_call_tool_upstream_404_is_an_error(protocol):
    reply = (
        await protocol.dispatch_async(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "companions_companion_id_get",
                    "arguments": {"companion_id": "missing"},
                },
            }
        )
    )[0]
    result = reply["result"]
    assert result["isError"] is True
    assert "404" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_call_tool_unknown_tool(protocol):
    reply = (
        await protocol.dispatch_async(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            }
        )
    )[0]
    assert reply["error"]["code"] == -32002


@pytest.mark.anyio
async def test_call_tool_missing_path_param(protocol):
    reply = (
        await protocol.dispatch_async(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "nodes_node_id_get", "arguments": {}},
            }
        )
    )[0]
    assert reply["error"]["code"] == -32602


def test_initialize_and_tools_list(protocol):
    init = protocol.handle(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert init["result"]["serverInfo"]["name"] == "virtual-life-bridge"
    assert init["result"]["capabilities"]["tools"] == {}
    listing = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = listing["result"]["tools"]
    assert len(tools) == len(protocol.bridge.operations)
    assert "inputSchema" in tools[0]


def test_notifications_produce_no_reply(protocol):
    assert protocol.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert (
        protocol.handle(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        is None
    )
    # An unknown notification method is also dropped.
    assert protocol.handle({"jsonrpc": "2.0", "method": "notifications/whatever"}) is None


def test_unknown_method(protocol):
    reply = protocol.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    assert reply["error"]["code"] == -32601


def test_http_transport_session_flow():
    protocol = Protocol(
        Bridge(
            spec(), "http://runtime.local", "secret", 5.0, httpx.MockTransport(_upstream_responder)
        ),
        "instr",
    )
    client = TestClient(build_http_app(protocol))
    # GET is not part of streamable HTTP here.
    assert client.get("/mcp").status_code == 405
    # Initialise and take the session id.
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert init.status_code == 200
    session = init.headers.get("mcp-session-id")
    assert session
    # tools/list without the session id is rejected.
    rejected = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert rejected.status_code == 400
    # tools/list with the session id works.
    listing = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={"Mcp-Session-Id": session},
    )
    assert listing.status_code == 200
    assert listing.json()["result"]["tools"]
    # Notification-only requests get 202 with no body.
    notified = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Mcp-Session-Id": session},
    )
    assert notified.status_code == 202
    assert notified.content == b""
    # A real call round-trips.
    called = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "capabilities_list", "arguments": {}},
        },
        headers={"Mcp-Session-Id": session},
    )
    assert called.status_code == 200
    assert "single_owner_local" in called.json()["result"]["content"][0]["text"]


# ──────────────────────────────────────────────────────────────────────────────
# command_id handling: seeded for body-declared keys, stripped for synthetic ones
# ──────────────────────────────────────────────────────────────────────────────


def _record_transport(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["idempotency"] = request.headers.get("idempotency-key")
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def test_minted_command_id_reaches_the_body_when_declared(operations):
    """Routes that require command_id in the body must receive the minted key."""
    seen: dict = {}
    bridge = Bridge(spec(), "http://runtime.local", "secret", 5.0, _record_transport(seen))
    operation = bridge.by_name["companions_companion_id_dialogue"]
    assert not operation.synthetic_command_id
    assert "command_id" in operation.schema["required"]

    asyncio.run(
        bridge.call_tool(
            operation.name,
            {
                "companion_id": "c1",
                "user_message": "hi",
                "node_id": "n1",
                "epoch": 0,
                "expected_revision": 0,
            },
        )
    )
    assert seen["body"]["command_id"].startswith("mcp-"), "minted key must be written to the body"
    assert seen["idempotency"] == seen["body"]["command_id"], "header and body must agree"
    assert seen["auth"] == "Bearer secret"


def test_minted_command_id_is_stripped_when_synthetic(operations):
    """Keys the bridge invented are not part of the upstream schema and must not leak."""
    seen: dict = {}
    bridge = Bridge(spec(), "http://runtime.local", "secret", 5.0, _record_transport(seen))
    operation = bridge.by_name["companions_companion_id_lease"]
    assert operation.synthetic_command_id

    asyncio.run(
        bridge.call_tool(operation.name, {"companion_id": "c1", "node_id": "n1", "epoch": 0})
    )
    assert "command_id" not in seen["body"], "synthetic key must not be sent upstream"
    assert seen["idempotency"].startswith("mcp-"), "it still rides in the idempotency header"


def test_caller_supplied_command_id_wins(operations):
    seen: dict = {}
    bridge = Bridge(spec(), "http://runtime.local", "secret", 5.0, _record_transport(seen))
    asyncio.run(
        bridge.call_tool(
            "companions_companion_id_lease",
            {"companion_id": "c1", "node_id": "n1", "epoch": 0, "command_id": "my-retry-key"},
        )
    )
    assert seen["idempotency"] == "my-retry-key"
    assert "command_id" not in seen["body"]
