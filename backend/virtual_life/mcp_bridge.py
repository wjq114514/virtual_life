"""Thin MCP bridge: expose the Virtual Life REST runtime as MCP tools.

The bridge is *contract-driven* rather than hand-written: at startup it reads the
runtime's own OpenAPI document and turns every operation into an MCP tool. A
runtime change therefore reaches the model by restarting the bridge, and the
advertised surface can never silently drift from the runtime it fronts.

Two transports, one protocol, no new dependencies.

    # Streamable HTTP -- what dsh-mcp-client expects for a service
    uv run python -m virtual_life.mcp_bridge --base-url http://127.0.0.1:8038 \
        --listen 127.0.0.1:8039

    # Stdio -- local inspection or any other MCP client
    uv run python -m virtual_life.mcp_bridge --stdio --base-url http://127.0.0.1:8038

    # No client needed: list or call tools directly
    uv run python -m virtual_life.mcp_bridge --list-tools
    uv run python -m virtual_life.mcp_bridge --call capabilities_list '{}'

Environment:
    VIRTUAL_LIFE_TOKEN  Runtime bearer token; ``--token`` overrides it.

Only ``httpx`` -- already a runtime dependency -- is used for the upstream call.
JSON-RPC 2.0 and both transports are implemented directly so the bridge needs no
extra package. The bridge is a separate process that calls the runtime over HTTP:
it exercises the real wire contract (bearer auth, strict request validation,
``command_id`` idempotency) instead of bypassing it.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "virtual-life-bridge"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_RESULT_CHARS = 200_000

# Operations that cannot be useful as a tool result: the browser cookie handshake
# and package/bundle transfers. Anything that streams bytes or events is dropped
# by media type instead, which tracks the contract automatically.
EXCLUDED_PATHS = ("/v1/session", "/v1/dist/packages", "/v1/dist/packages/{filename}")
# Media types a tool result cannot carry: SSE streams, raw bytes, audio and images.
# These are prefixes checked against the response media type.
STREAM_MEDIA = ("text/event-stream", "application/octet-stream", "audio/", "image/")
NON_JSON_BODY = ("multipart/form-data", "application/octet-stream", "image/", "text/")
MUTATIONS = ("post", "put", "patch", "delete")
METHOD_VERB = {
    "get": "get",
    "post": "create",
    "put": "replace",
    "patch": "update",
    "delete": "delete",
}


# ──────────────────────────────────────────────────────────────────────────────
# Contract -> tools
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Operation:
    """One callable upstream operation, resolved into a fillable schema."""

    name: str
    method: str
    path: str
    description: str
    schema: dict[str, Any]
    mutates: bool = False
    query_keys: frozenset[str] = frozenset()
    synthetic_command_id: bool = False


def _resolve(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Follow a local ``#/components/...`` reference."""
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _dereference(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    for _ in range(12):
        if not (isinstance(schema, dict) and "$ref" in schema):
            break
        schema = _resolve(schema["$ref"], spec)
    return schema


def simplify_schema(
    schema: dict[str, Any],
    spec: dict[str, Any],
    depth: int = 0,
) -> dict[str, Any]:
    """Reduce an OpenAPI schema to a JSON Schema a model can fill."""
    if depth > 8:
        return {"type": "object", "additionalProperties": True}
    schema = _dereference(schema, spec)
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "additionalProperties": True}

    for combinator in ("anyOf", "oneOf"):
        choices = schema.get(combinator)
        if isinstance(choices, list):
            concrete = [
                c
                for c in choices
                if not (
                    isinstance(c, dict)
                    and (c.get("type") == "null" or _dereference(c, spec).get("type") == "null")
                )
            ]
            if len(concrete) == 1:
                return simplify_schema(concrete[0], spec, depth + 1)
            if len(concrete) > 1:
                return {"anyOf": [simplify_schema(c, spec, depth + 1) for c in concrete]}

    if isinstance(schema.get("allOf"), list):
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            sub = simplify_schema(part, spec, depth + 1)
            for prop, prop_schema in (sub.get("properties") or {}).items():
                merged["properties"][prop] = prop_schema
            merged["required"].extend(sub.get("required") or [])
            if sub.get("additionalProperties") is False:
                merged["additionalProperties"] = False
        merged["required"] = list(dict.fromkeys(merged["required"]))
        return merged

    schema_type = schema.get("type", "object" if "properties" in schema else "string")

    if schema_type == "array" or "items" in schema:
        items = schema.get("items", {})
        return {"type": "array", "items": simplify_schema(items, spec, depth + 1)}

    if schema_type == "object" or "properties" in schema:
        properties: dict[str, Any] = {}
        for prop, prop_schema in (schema.get("properties") or {}).items():
            properties[prop] = simplify_schema(prop_schema, spec, depth + 1)
        result: dict[str, Any] = {"type": "object", "properties": properties}
        declared_required = schema.get("required") or []
        if declared_required:
            result["required"] = list(declared_required)
        result["additionalProperties"] = bool(schema.get("additionalProperties", False))
        if schema.get("description"):
            result["description"] = str(schema["description"])
        return result

    result = {"type": schema_type}
    for hint in (
        "enum",
        "const",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "default",
        "description",
    ):
        if hint in schema:
            result[hint] = schema[hint]
    return result


def _looks_plural(segment: str) -> bool:
    """A crude plural check so GET /nodes becomes *_list and GET /graph stays *_get."""
    return len(segment) > 3 and segment.endswith("s") and not segment.endswith(("ss", "us", "is"))


def tool_name_for(method: str, path: str, taken: set[str]) -> str:
    """Derive a stable, unique, MCP-legal tool name from an operation."""
    method = method.lower()
    parts = [p for p in path.strip("/").split("/") if p]
    if parts and parts[0] == "v1":
        parts = parts[1:]
    slugs = [p[1:-1] if p.startswith("{") and p.endswith("}") else p for p in parts]
    has_param = any(p.startswith("{") or p.startswith(":") for p in parts)
    last = slugs[-1] if slugs else ""
    if (
        method in MUTATIONS
        and has_param
        and last not in ("", "create", "update", "replace", "delete", "list", "get")
    ):
        # An action verb on a specific resource reads better without the verb
        # (memories_recall, heartbeat, lease, preview). POST on a bare collection
        # still gets the method verb (nodes_create).
        name = "_".join(slugs)
    else:
        verb = METHOD_VERB.get(method, "call")
        if method == "get":
            last_raw = parts[-1] if parts else ""
            # A single resource reads as *_get, a collection as *_list.
            verb = (
                "get"
                if last_raw.startswith(("{", ":"))
                else ("list" if _looks_plural(last_raw) else "get")
            )
        name = "_".join(slugs + [verb])
    name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:64]
    name = name.strip("_") or "tool"
    candidate, suffix = name, 2
    while candidate in taken:
        candidate = f"{name}_{suffix}"[:64]
        suffix += 1
    taken.add(candidate)
    return candidate


def _operation_blocked(path: str, operation: dict[str, Any], method: str) -> str | None:
    if path in EXCLUDED_PATHS:
        return "explicitly excluded"
    if fnmatch.fnmatch(path, "/v1/dist/packages*") or path.startswith(
        "/v1/deployments/linux/bundle"
    ):
        return "binary or package transfer"
    # Binary segments, event streams and raw image uploads: the bytes are not a
    # tool result. Path suffixes cover endpoints whose media type the OpenAPI
    # document does not declare.
    if method.lower() in MUTATIONS:
        content = operation.get("requestBody", {}).get("content", {})
        if not content:
            if fnmatch.fnmatch(path, "*/*/*content") or fnmatch.fnmatch(path, "*/*/*/*/*/*audio"):
                return "request body is binary"
        elif all(media.startswith(NON_JSON_BODY) for media in content):
            return "request body is not JSON"
    if fnmatch.fnmatch(path, "*/*/*/*/*/*audio") or fnmatch.fnmatch(path, "*/*/*/*/*/*events"):
        return "response streams bytes or events"
    for status, body in operation.get("responses", {}).items():
        if status.startswith("2"):
            responses = body.get("content", {})
            if responses and all(media.startswith(STREAM_MEDIA) for media in responses):
                return "response streams bytes or events"
    return None


def _operation_description(path: str, method: str, operation: dict[str, Any], mutates: bool) -> str:
    pieces = []
    summary = operation.get("summary")
    if summary:
        pieces.append(str(summary))
    description = operation.get("description")
    if description and (not summary or str(description) != str(summary)):
        pieces.append(str(description).strip())
    pieces.append(f"Upstream: {method.upper()} {path}")
    if mutates:
        pieces.append(
            "Mutating operation. Pass command_id to make the call idempotent; omit it to have the bridge "
            "mint one. The id is echoed back so a failed call can be retried exactly."
        )
    return "\n\n".join(pieces)


def load_operations(spec: dict[str, Any]) -> list[Operation]:
    """Build the tool table from an OpenAPI document."""
    operations: list[Operation] = []
    taken: set[str] = set()
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in METHOD_VERB:
                continue
            if not isinstance(operation, dict):
                continue
            if _operation_blocked(path, operation, method.lower()):
                continue
            mutates = method.lower() in MUTATIONS
            properties: dict[str, Any] = {}
            required: list[str] = []
            query_keys: set[str] = set()

            for parameter in operation.get("parameters", []):
                if isinstance(parameter, dict) and "$ref" in parameter:
                    parameter = _resolve(parameter["$ref"], spec)
                if not isinstance(parameter, dict):
                    continue
                location = str(parameter.get("in", ""))
                if location not in ("path", "query"):
                    continue
                prop_schema = simplify_schema(parameter.get("schema", {"type": "string"}), spec)
                if parameter.get("description"):
                    prop_schema["description"] = str(parameter["description"])
                properties[str(parameter["name"])] = prop_schema
                if location == "path" or parameter.get("required") is True:
                    required.append(str(parameter["name"]))
                if location == "query":
                    query_keys.add(str(parameter["name"]))

            body = operation.get("requestBody", {})
            json_body = body.get("content", {}).get("application/json", {})
            if json_body.get("schema"):
                body_schema = simplify_schema(json_body["schema"], spec)
                for prop, prop_schema in (body_schema.get("properties") or {}).items():
                    if prop not in properties:
                        properties[prop] = prop_schema
                for prop in body_schema.get("required") or []:
                    if prop in properties and prop not in required:
                        required.append(prop)

            injects_command_id = False
            if mutates and "command_id" not in properties:
                properties["command_id"] = {
                    "type": "string",
                    "description": "Idempotency key for this mutation. Optional; the bridge mints one if omitted.",
                }
                injects_command_id = True

            # Zero-argument reads (GET /capabilities, /graph, /nodes, ...) keep an
            # empty object schema: the model calls them with {}.
            schema: dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
            if required:
                schema["required"] = list(dict.fromkeys(required))
            operations.append(
                Operation(
                    name=tool_name_for(method, path, taken),
                    method=method.lower(),
                    path=path,
                    description=_operation_description(path, method, operation, mutates),
                    schema=schema,
                    mutates=mutates,
                    query_keys=frozenset(query_keys),
                    synthetic_command_id=injects_command_id,
                )
            )
    return operations


# ──────────────────────────────────────────────────────────────────────────────
# Upstream calls
# ──────────────────────────────────────────────────────────────────────────────


class Bridge:
    """Contract table plus a small authenticated client to the runtime."""

    def __init__(
        self,
        spec: dict[str, Any],
        base_url: str,
        token: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.operations = load_operations(spec)
        self.by_name = {op.name: op for op in self.operations}
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": op.name, "description": op.description, "inputSchema": op.schema}
            for op in self.operations
        ]

    @staticmethod
    def _split_arguments(
        operation: Operation, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Split one flat argument object into path params, query, and JSON body."""
        path_params: dict[str, Any] = {}
        for segment in operation.path.strip("/").split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                key = segment[1:-1]
                value = arguments.get(key)
                if value is None:
                    raise ValueError(f"missing required path parameter: {key}")
                path_params[key] = str(value)
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        for key, value in arguments.items():
            if key in path_params or value is None:
                continue
            if key in operation.query_keys:
                query[key] = value
            elif key == "command_id" and operation.synthetic_command_id:
                continue  # bridge-minted idempotency key; not part of the upstream body
            else:
                body[key] = value
        return operation.path.format(**path_params), query, body

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        operation = self.by_name.get(name)
        if operation is None:
            raise KeyError(f"Unknown tool: {name}")
        command_id = str(arguments.get("command_id") or f"mcp-{uuid.uuid4().hex[:24]}")
        arguments = dict(arguments)
        # Some routes require command_id inside the JSON body. Seed it there as well
        # as in the Idempotency-Key header, so a bridge-minted key is usable by both.
        if "command_id" in operation.schema["properties"]:
            arguments.setdefault("command_id", command_id)
        url, query, body = self._split_arguments(operation, arguments)
        headers = {"Authorization": f"Bearer {self.token}"}
        if operation.mutates:
            headers["Idempotency-Key"] = command_id
        async with httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
        ) as client:
            try:
                response = await client.request(
                    operation.method, url, params=query or None, json=body or None, headers=headers
                )
            except httpx.HTTPError as exc:
                return _error_result(f"Runtime unreachable at {self.base_url}: {exc}")
        envelope = {
            "tool": name,
            "upstream": {"method": operation.method.upper(), "path": operation.path},
            "command_id": command_id,
            "status": response.status_code,
        }
        if response.status_code >= 400:
            return _error_result(
                f"Runtime returned HTTP {response.status_code}:\n{response.text[:8000]}"
            )
        envelope["response"] = _decode(response)
        return _text_result(json.dumps(envelope, ensure_ascii=False, indent=2, default=str))


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    if "json" not in response.headers.get("content-type", ""):
        text = response.text
        return text[:MAX_RESULT_CHARS] + ("…[truncated]" if len(text) > MAX_RESULT_CHARS else "")
    try:
        return response.json()
    except ValueError:
        return response.text[:MAX_RESULT_CHARS]


def _text_result(text: str) -> dict[str, Any]:
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n…[truncated]"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ──────────────────────────────────────────────────────────────────────────────
# MCP JSON-RPC dispatch
# ──────────────────────────────────────────────────────────────────────────────

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TOOL_NOT_FOUND = -32002


def _reply(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _reject(message_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def _find_id(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("id")
    if isinstance(message, list) and message and isinstance(message[0], dict):
        return message[0].get("id")
    return None


class Protocol:
    """Transport-neutral MCP dispatch over the bridge.

    ``handle`` is synchronous and only used for non-call methods and for call
    methods outside a running event loop (tests, single-shot CLI). Under a running
    loop, use ``dispatch_async`` which awaits the upstream call directly.
    """

    def __init__(self, bridge: Bridge, instructions: str) -> None:
        self.bridge = bridge
        self.instructions = instructions

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return None
        method = message.get("method")
        message_id = message.get("id")
        if not isinstance(method, str):
            if message_id is None:
                return None
            return _reject(message_id, INVALID_REQUEST, "Invalid request: no method")
        if message_id is None and not str(method).startswith("notifications/"):
            return None
        if method == "initialize":
            params = message.get("params") or {}
            return _reply(
                message_id,
                {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                    "instructions": self.instructions,
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _reply(message_id, {})
        if method == "tools/list":
            return _reply(message_id, {"tools": self.bridge.list_tools()})
        if method == "tools/call":
            if not message_id:
                return None
            if _running_loop():
                raise RuntimeError("tools/call needs dispatch_async inside a running event loop")
            return asyncio.run(self._call(message, message_id))
        if message_id is None:
            return None
        return _reject(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    async def _call(self, message: dict[str, Any], message_id: Any) -> dict[str, Any]:
        params = message.get("params") or {}
        name = params.get("name")
        if not isinstance(name, str):
            return _reject(message_id, INVALID_PARAMS, "tools/call requires a string 'name'")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _reject(message_id, INVALID_PARAMS, "tools/call 'arguments' must be an object")
        if name not in self.bridge.by_name:
            return _reject(message_id, TOOL_NOT_FOUND, f"Unknown tool: {name}", {"tool": name})
        try:
            result = await self.bridge.call_tool(name, arguments)
            return _reply(message_id, result)
        except (ValueError, KeyError) as exc:
            return _reject(message_id, INVALID_PARAMS, str(exc))

    async def dispatch_async(self, message: Any) -> list[dict[str, Any]]:
        """Handle one JSON-RPC message or batch, dropping pure notifications."""
        if isinstance(message, list):
            results: list[dict[str, Any]] = []
            for raw in message:
                if not isinstance(raw, dict):
                    continue
                if raw.get("id") is not None and raw.get("method") == "tools/call":
                    results.append(await self._call(raw, raw.get("id")))
                else:
                    reply = self.handle(raw)
                    if reply:
                        results.append(reply)
            return results
        if isinstance(message, dict):
            if message.get("id") is not None and message.get("method") == "tools/call":
                return [await self._call(message, message.get("id"))]
            reply = self.handle(message)
            return [reply] if reply else []
        return []


def _running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Transports
# ──────────────────────────────────────────────────────────────────────────────


async def run_stdio(protocol: Protocol) -> None:
    """Newline-delimited JSON-RPC on stdin/stdout. Logs go to stderr."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            sys.stdout.write(
                json.dumps(_reject(None, INVALID_REQUEST, f"Invalid JSON: {exc}")) + "\n"
            )
            sys.stdout.flush()
            continue
        try:
            replies = await protocol.dispatch_async(message)
        except Exception as exc:  # noqa: BLE001 - the client needs an error, not a dead pipe
            replies = [_reject(_find_id(message), INTERNAL_ERROR, f"Internal error: {exc}")]
        for reply in replies:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def build_http_app(protocol: Protocol) -> Starlette:
    """Streamable HTTP transport: POST for requests, 405 for everything else."""

    async def _post(request: Request) -> Response:
        try:
            message = await request.json()
        except json.JSONDecodeError as exc:
            return JSONResponse(
                _reject(None, INVALID_REQUEST, f"Invalid JSON: {exc}"), status_code=400
            )
        methods = _request_methods(message)
        is_initialize = "initialize" in methods
        is_notification_only = bool(methods) and all(
            m.startswith("notifications/") for m in methods
        )
        headers: dict[str, str] = {}
        if is_initialize:
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
        elif not is_notification_only:
            incoming = request.headers.get("mcp-session-id", "")
            if not incoming:
                return JSONResponse(
                    _reject(_find_id(message), INVALID_PARAMS, "Missing Mcp-Session-Id"),
                    status_code=400,
                )
            headers["Mcp-Session-Id"] = incoming
        if is_notification_only:
            return Response(status_code=202, headers=headers)
        try:
            replies = await protocol.dispatch_async(message)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                _reject(_find_id(message), INTERNAL_ERROR, f"Internal error: {exc}"),
                headers=headers,
            )
        payload = replies[0] if len(replies) == 1 else replies
        return JSONResponse(payload, headers=headers)

    async def _not_allowed(request: Request) -> Response:
        return Response(status_code=405, headers={"Allow": "POST", "Content-Type": "text/plain"})

    async def _healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "server": SERVER_NAME})

    return Starlette(
        routes=[
            Route("/mcp", _post, methods=["POST"]),
            Route("/mcp", _not_allowed, methods=["GET", "PUT", "PATCH", "DELETE"]),
            Route("/healthz", _healthz, methods=["GET"]),
        ]
    )


def _request_methods(message: Any) -> list[str]:
    if isinstance(message, dict):
        return [str(message["method"])] if isinstance(message.get("method"), str) else []
    if isinstance(message, list):
        return [
            str(m["method"])
            for m in message
            if isinstance(m, dict) and isinstance(m.get("method"), str)
        ]
    return []


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────


def load_spec(source: str, base_url: str, token: str) -> dict[str, Any]:
    """Read the contract from a file or from the live runtime."""
    if source:
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0) as client:
        response = client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            raise SystemExit(
                f"Could not read OpenAPI from {base_url}/openapi.json: HTTP {response.status_code}"
            )
        return response.json()


def make_bridge(spec: dict[str, Any], base_url: str, token: str, timeout: float) -> Bridge:
    bridge = Bridge(spec, base_url, token, timeout)
    if not bridge.operations:
        raise SystemExit(
            "No operations survived contract filtering; check the runtime's OpenAPI document."
        )
    return bridge


def _default_instructions(count: int, base_url: str) -> str:
    return (
        f"Tools for the Virtual Life companion runtime at {base_url}. {count} operations generated from the "
        "runtime's own OpenAPI contract. Paths use snake_case, timestamps are UTC RFC3339. Mutating calls are "
        "idempotent by command_id: omit it to have the bridge mint one, or pass your own to retry a failed call "
        "exactly. Every result echoes the command_id and the upstream HTTP status."
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCP bridge over the Virtual Life runtime")
    parser.add_argument("--base-url", default="http://127.0.0.1:8038", help="Runtime base URL")
    parser.add_argument("--token", default=os.environ.get("VIRTUAL_LIFE_TOKEN", "local-dev-token"))
    parser.add_argument(
        "--openapi", help="Read the contract from this JSON file instead of the live runtime"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Upstream timeout in seconds"
    )
    parser.add_argument("--listen", help="Host:port for the streamable HTTP transport")
    parser.add_argument("--stdio", action="store_true", help="Serve JSON-RPC on stdin/stdout")
    parser.add_argument("--list-tools", action="store_true", help="Print generated tools and exit")
    parser.add_argument(
        "--call", nargs=2, metavar=("TOOL", "ARGS_JSON"), help="Call one tool and exit"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        spec = load_spec(args.openapi, args.base_url, args.token)
        bridge = make_bridge(spec, args.base_url, args.token, args.timeout)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"bridge: {exc}", file=sys.stderr)
        return 2
    protocol = Protocol(bridge, _default_instructions(len(bridge.operations), args.base_url))

    if args.list_tools:
        print(
            json.dumps(
                protocol.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})["result"],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.call:
        name, raw = args.call
        try:
            arguments = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            print(f"bridge: invalid ARGS_JSON: {exc}", file=sys.stderr)
            return 2
        reply = asyncio.run(
            protocol.dispatch_async(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
        )
        outcome = reply[0] if reply else {}
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 1 if outcome.get("error") else 0
    if args.stdio:
        asyncio.run(run_stdio(protocol))
        return 0
    if not args.listen:
        print("bridge: choose --stdio, --listen HOST:PORT, --list-tools or --call", file=sys.stderr)
        return 2
    host, _, port = args.listen.partition(":")
    uvicorn.run(
        build_http_app(protocol),
        host=host or "127.0.0.1",
        port=int(port or 8039),
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
