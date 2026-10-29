"""Bounded tool calls, proactive scheduling and optional external delegation."""

import json
import sys
import types
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.delegation import DelegationConfig, DelegationOutcome
from virtual_life.model_provider import ProviderError
from virtual_life.ports import ModelResult, ModelUsage, ToolCall, ToolDefinition
from virtual_life.proactivity import SchedulePolicy
from virtual_life.settings import Settings
from virtual_life.tools import ToolDescriptor, ToolOutcome, ToolRegistry

USAGE = ModelUsage(10, 2)


class Provider:
    def __init__(self):
        self.requests = []
        self.tool_call = None
        self.reply = "好。"

    def complete(self, request):
        self.requests.append(request)
        if request.tools and self.tool_call is not None:
            call = self.tool_call
            self.tool_call = None
            return ModelResult("", "fake", USAGE, tool_calls=(call,))
        return ModelResult(self.reply, "fake", ModelUsage(20, 5))


def make_runtime(
    tmp_path,
    provider,
    *,
    delegation=None,
    schedule=None,
    tools=True,
    plugins=(),
    delegation_executor=None,
):
    settings = Settings(
        database_path=str(tmp_path / "tools.sqlite"),
        api_token="test",
        model_base_url="http://model.invalid",
        model_name="fake",
        auto_memory_enabled=False,
        node_ttl_seconds=100000,
        lease_ttl_seconds=100000,
        tools_enabled=tools,
        tools_max_calls_per_turn=1,
        delegation=delegation or DelegationConfig(),
        tools_plugins=plugins,
        proactive_schedule=schedule or SchedulePolicy(min_delay_seconds=600, min_idle_seconds=60),
    )
    clock = Clock()
    app = create_app(settings, model_provider=provider, delegation_executor=delegation_executor)
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.memory,
        app.state.dialogue,
    ):
        service.clock = clock
    app.state.delegation.coordinator.clock = clock
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
            "expected_revision": 0,
            "definition": {"traits": {"initiative": 0.8}},
        },
    )
    return client, app, clock, state


def send(client, command_id="turn"):
    state = client.get("/v1/companions/sui").json()
    return client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": command_id,
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": "帮我盯着点。",
        },
    )


def events(client, prefix):
    return [
        event
        for event in client.get("/v1/events?limit=500").json()["events"]
        if event["type"].startswith(prefix)
    ]


def test_schedule_tool_uses_two_rounds_and_later_fires(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1", "schedule_next_proactive", '{"delay_seconds": 900, "reason": "wait"}'
    )
    client, app, clock, _ = make_runtime(tmp_path, provider)
    try:
        response = send(client)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["status"] == "completed"
        assert receipt["assistant_message"] == "好。"
        assert receipt["model_calls"] == 2
        assert receipt["usage"] == {"input_tokens": 30, "output_tokens": 7}
        assert len(provider.requests) == 2
        assert provider.requests[0].tools
        assert provider.requests[1].tools == ()
        assert provider.requests[1].tool_results[0].name == "schedule_next_proactive"
        schedule = client.get("/v1/companions/sui/proactivity/schedule").json()
        assert schedule["schedule"]["status"] == "pending"
        assert schedule["schedule"]["reason"] == "wait"
        assert events(client, "companion.proactive_scheduled")

        current = client.get("/v1/companions/sui/proactivity").json()
        client.post(
            "/v1/companions/sui/proactivity",
            json={
                "command_id": "policy",
                "expected_revision": current["revision"],
                "policy": {**current["policy"], "enabled": True},
            },
        )
        clock.value += timedelta(seconds=901)
        sample = {
            "session_id": "session",
            "sequence": 1,
            "sampled_at": clock.value.isoformat(),
            "activity": "docs",
            "focus": "unfocused",
            "availability": "available",
            "user_present": True,
            "locked": False,
            "meeting": False,
            "screen_sharing": False,
            "boundary": "none",
        }
        assert (
            client.post(
                "/v1/nodes/desk/attention",
                json={"command_id": "attention", "sample": sample},
            ).status_code
            == 200
        )
        result = app.state.proactivity.process_next()
        assert result["status"] == "awaiting_generation", result
        # DSH-owned path: commit the externally generated reply through the route.
        decision = client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"][-1]
        commit = client.post(
            f"/v1/companions/sui/dialogue/{decision['command_id']}/commit",
            json={"assistant_message": "买牛奶，好的。", "usage": None},
        )
        assert commit.status_code == 200, commit.text
        assert commit.json()["status"] == "completed"
        decision = client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"][-1]
        assert decision["origin"] == "schedule"
    finally:
        client.close()


def test_user_turn_cancels_pending_schedule(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall("call-1", "schedule_next_proactive", '{"delay_seconds": 900}')
    client, app, _, _ = make_runtime(tmp_path, provider)
    try:
        assert send(client, "first").status_code == 200
        assert app.state.proactivity.snapshot_schedule("sui")["schedule"]["status"] == "pending"
        assert send(client, "second").status_code == 200
        assert app.state.proactivity.snapshot_schedule("sui")["schedule"]["status"] == "cancelled"
    finally:
        client.close()


def test_schedule_task_tool_persists_fires_and_is_idempotent(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "schedule_task",
        '{"title": "买牛奶", "details": "下班路上", "delay_seconds": 900}',
    )
    client, app, clock, _ = make_runtime(tmp_path, provider)
    try:
        response = send(client)
        assert response.status_code == 200, response.text
        tasks = client.get("/v1/companions/sui/scheduled-tasks").json()
        assert tasks["total"] == 1
        task = tasks["items"][0]
        assert task["status"] == "pending"
        assert task["title"] == "买牛奶"
        assert task["details"] == "下班路上"
        assert task["candidate_id"] is None
        assert events(client, "companion.scheduled_task_created")

        replay = send(client)
        assert replay.json() == response.json()
        assert client.get("/v1/companions/sui/scheduled-tasks").json()["total"] == 1

        current = client.get("/v1/companions/sui/proactivity").json()
        client.post(
            "/v1/companions/sui/proactivity",
            json={
                "command_id": "policy",
                "expected_revision": current["revision"],
                "policy": {**current["policy"], "enabled": True},
            },
        )
        clock.value += timedelta(seconds=901)
        sample = {
            "session_id": "session",
            "sequence": 1,
            "sampled_at": clock.value.isoformat(),
            "activity": "docs",
            "focus": "unfocused",
            "availability": "available",
            "user_present": True,
            "locked": False,
            "meeting": False,
            "screen_sharing": False,
            "boundary": "none",
        }
        assert (
            client.post(
                "/v1/nodes/desk/attention",
                json={"command_id": "attention", "sample": sample},
            ).status_code
            == 200
        )
        result = app.state.proactivity.process_next()
        assert result["status"] == "awaiting_generation", result
        task = client.get("/v1/companions/sui/scheduled-tasks").json()["items"][0]
        assert task["status"] in ("queued", "pending")  # queued while running, pending once parked
        decision = client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"][-1]
        assert decision["origin"] == "task"
        assert decision["scheduled_task_id"] == task["task_id"]
        # DSH-owned path: commit the externally generated reply through the route.
        commit = client.post(
            f"/v1/companions/sui/dialogue/{decision['command_id']}/commit",
            json={"assistant_message": "买牛奶，好的。", "usage": None},
        )
        assert commit.status_code == 200, commit.text
        assert commit.json()["status"] == "completed"
        task = client.get("/v1/companions/sui/scheduled-tasks").json()["items"][0]
        assert task["status"] == "completed"
        assert task["reason"] == "completed"
        assert task["candidate_id"]
        decision = client.get("/v1/companions/sui/proactivity/decisions").json()["decisions"][-1]
        assert decision["status"] == "completed"
    finally:
        client.close()


def test_schedule_task_tool_rejects_invalid_delay(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "schedule_task",
        '{"title": "太快了", "delay_seconds": 1}',
    )
    client, _, _, _ = make_runtime(tmp_path, provider)
    try:
        response = send(client)
        assert response.status_code == 200, response.text
        assert "invalid_arguments" in provider.requests[1].tool_results[0].content
        assert client.get("/v1/companions/sui/scheduled-tasks").json()["total"] == 0
    finally:
        client.close()


def test_scheduled_task_can_be_cancelled_before_due(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "schedule_task",
        '{"title": "提醒我", "delay_seconds": 900}',
    )
    client, app, clock, _ = make_runtime(tmp_path, provider)
    try:
        assert send(client).status_code == 200
        task = client.get("/v1/companions/sui/scheduled-tasks").json()["items"][0]
        response = client.delete(f"/v1/companions/sui/scheduled-tasks/{task['task_id']}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert response.json()["reason"] == "user_cancelled"
        assert events(client, "companion.scheduled_task_cancelled")
        clock.value += timedelta(seconds=901)
        assert app.state.proactivity.process_next() is None
        task = client.get("/v1/companions/sui/scheduled-tasks").json()["items"][0]
        assert task["status"] == "cancelled"
    finally:
        client.close()


def test_list_scheduled_tasks_tool_filters_active_tasks(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "schedule_task",
        '{"title": "买牛奶", "details": "下班路上", "delay_seconds": 900}',
    )
    client, _, _, _ = make_runtime(tmp_path, provider)
    try:
        assert send(client, "schedule").status_code == 200
        provider.tool_call = ToolCall(
            "call-2", "list_scheduled_tasks", '{"status": "active", "limit": 5}'
        )
        response = send(client, "list")
        assert response.status_code == 200, response.text
        result = json.loads(provider.requests[-1].tool_results[0].content)
        assert result["total"] == 1
        assert result["status"] == "active"
        assert result["tasks"][0]["title"] == "买牛奶"
        assert result["tasks"][0]["status"] == "pending"
        assert client.get("/v1/companions/sui/scheduled-tasks?status=active").json()["total"] == 1
        assert (
            client.get("/v1/companions/sui/scheduled-tasks?status=completed").json()["total"] == 0
        )
    finally:
        client.close()


def test_cancel_scheduled_task_tool_matches_title(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "schedule_task",
        '{"title": "买牛奶", "delay_seconds": 900}',
    )
    client, _, _, _ = make_runtime(tmp_path, provider)
    try:
        assert send(client, "schedule").status_code == 200
        provider.tool_call = ToolCall("call-2", "cancel_scheduled_task", '{"title": "买牛奶"}')
        response = send(client, "cancel")
        assert response.status_code == 200, response.text
        result = json.loads(provider.requests[-1].tool_results[0].content)
        assert result["status"] == "cancelled"
        assert result["reason"] == "user_cancelled"
        task = client.get("/v1/companions/sui/scheduled-tasks").json()["items"][0]
        assert task["status"] == "cancelled"
        assert events(client, "companion.scheduled_task_cancelled")
    finally:
        client.close()


def test_cancel_scheduled_task_tool_reports_ambiguous_title(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "schedule_task",
        '{"title": "吃药", "delay_seconds": 900}',
    )
    client, app, _, _ = make_runtime(tmp_path, provider)
    try:
        assert send(client, "schedule").status_code == 200
        app.state.proactivity.schedule_task("sui", "manual", "call-manual", "吃药", None, 900)
        provider.tool_call = ToolCall("call-2", "cancel_scheduled_task", '{"title": "吃药"}')
        response = send(client, "cancel")
        assert response.status_code == 200, response.text
        result = json.loads(provider.requests[-1].tool_results[0].content)
        assert result["error"] == "scheduled_task_ambiguous"
        tasks = client.get("/v1/companions/sui/scheduled-tasks?status=active").json()
        assert tasks["total"] == 2
        assert all(task["status"] == "pending" for task in tasks["items"])
    finally:
        client.close()


def test_tool_round_rejects_too_many_calls(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "schedule_next_proactive",
        '{"delay_seconds": 900}',
    )
    client, app, _, _ = make_runtime(tmp_path, provider)
    try:
        # A provider that emits two calls at once exceeds the configured bound.
        duplicate_calls = (
            ToolCall("call-1", "schedule_next_proactive", '{"delay_seconds": 900}'),
            ToolCall("call-2", "schedule_next_proactive", '{"delay_seconds": 900}'),
        )
        provider.complete = lambda request: (
            ModelResult("", "fake", USAGE, tool_calls=duplicate_calls)
            if request.tools
            else ModelResult("好。", "fake", USAGE)
        )
        response = send(client)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "model_invalid_response"
    finally:
        client.close()


def test_delegation_runs_configured_argv_and_is_idempotent(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "delegate_task",
        '{"instruction": "say hello"}',
    )
    command = (
        sys.executable,
        "-c",
        "import sys; print('ran:' + sys.argv[1])",
        "{prompt}",
    )
    delegation = DelegationConfig(
        enabled=True,
        command=command,
        working_dir=str(tmp_path),
        timeout_seconds=30,
        max_output_chars=1000,
        env_allowlist=("PATH",),
    )
    client, app, _, _ = make_runtime(tmp_path, provider, delegation=delegation)
    try:
        response = send(client)
        assert response.status_code == 200, response.text
        task = client.get("/v1/companions/sui/delegations").json()["items"][0]
        assert task["status"] == "pending"
        assert app.state.delegation.process_next() is True
        task = client.get("/v1/companions/sui/delegations").json()["items"][0]
        assert task["status"] == "completed"
        assert task["result_text"] == "ran:say hello"
        assert events(client, "companion.delegation_completed")
        assert (
            app.state.delegation.enqueue("sui", "turn", "call-1", "ignored")["task_id"]
            == task["task_id"]
        )
    finally:
        client.close()


class _StreamingProvider:
    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        raise AssertionError("streaming path expected")

    def stream(self, request, on_delta):
        self.requests.append(request)
        if request.tools:
            return ModelResult(
                "",
                "fake",
                USAGE,
                tool_calls=(
                    ToolCall("call-1", "schedule_next_proactive", '{"delay_seconds": 900}'),
                ),
            )
        on_delta("好。")
        return ModelResult("好。", "fake", USAGE)


def test_streaming_tool_call_defers_text_until_second_round(tmp_path):
    provider = _StreamingProvider()
    client, app, _, _ = make_runtime(tmp_path, provider)
    try:
        state = client.get("/v1/companions/sui").json()
        events_seen = []
        receipt = app.state.dialogue.send_stream(
            "sui",
            {
                "command_id": "streamed",
                "node_id": "desk",
                "epoch": state["epoch"],
                "expected_revision": state["revision"],
                "user_message": "帮我盯着点。",
            },
            lambda kind, data: events_seen.append((kind, data)),
        )
        assert receipt["status"] == "completed"
        assert receipt["assistant_message"] == "好。"
        assert [item[1]["stage"] for item in events_seen if item[0] == "status"] == [
            "preparing",
            "generating",
            "tools",
        ]
        assert len(provider.requests) == 2
    finally:
        client.close()


def test_provider_parses_tool_call_without_text(monkeypatch):
    import json

    import httpx
    from virtual_life.model_provider import CompatibleChatProvider
    from virtual_life.ports import ModelRequest

    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "schedule_next_proactive",
                                "arguments": '{"delay_seconds": 900}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    seen = []
    real_client = httpx.Client

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200, json=payload))[1]
        )
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    provider = CompatibleChatProvider("https://example.test/v1", "key")
    request = ModelRequest(
        "task",
        "companion",
        "dialogue",
        "model",
        "hello",
        100,
        50,
        tools=(
            ToolDefinition(
                "schedule_next_proactive",
                "Schedule a later remark.",
                {"type": "object", "properties": {}},
            ),
        ),
    )
    result = provider.complete(request)
    assert result.text == ""
    assert result.tool_calls[0].name == "schedule_next_proactive"
    body = json.loads(seen[0].content)
    assert body["tools"][0]["function"]["name"] == "schedule_next_proactive"

    with pytest.raises(ProviderError) as caught:
        provider.complete(ModelRequest("task", "companion", "dialogue", "model", "hello", 1, 1))
    assert caught.value.code == "model_invalid_response"


def _stream_setup(monkeypatch, wire):
    import httpx
    from virtual_life.model_provider import CompatibleChatProvider

    stream = httpx.ByteStream(wire)
    real_client = httpx.Client

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        )
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return CompatibleChatProvider("https://example.test/v1", "key")


def _wire(value):
    import json

    return b"data: " + json.dumps(value).encode() + b"\n\n"


def test_streaming_provider_collects_tool_call_fragments(monkeypatch):
    from virtual_life.ports import ModelRequest

    tool = ToolDefinition(
        "schedule_next_proactive",
        "Schedule a later remark.",
        {"type": "object", "properties": {}},
    )
    wire = (
        _wire(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "schedule_next_proactive",
                                        "arguments": '{"delay_',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
        + _wire(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": 'seconds": 900}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        + _wire(
            {
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        )
        + b"data: [DONE]\n\n"
    )
    provider = _stream_setup(monkeypatch, wire)
    request = ModelRequest(
        "task", "companion", "dialogue", "model", "hello", 100, 50, tools=(tool,)
    )
    drafts = []
    result = provider.stream(request, drafts.append)
    assert drafts == []
    assert result.text == ""
    assert result.tool_calls == (
        ToolCall("call-1", "schedule_next_proactive", '{"delay_seconds": 900}'),
    )


def test_streaming_provider_rejects_text_before_tool_call(monkeypatch):
    from virtual_life.ports import ModelRequest

    tool = ToolDefinition(
        "schedule_next_proactive",
        "Schedule a later remark.",
        {"type": "object", "properties": {}},
    )
    wire = (
        _wire({"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]})
        + _wire(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "schedule_next_proactive",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        + _wire({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        + b"data: [DONE]\n\n"
    )
    provider = _stream_setup(monkeypatch, wire)
    request = ModelRequest(
        "task", "companion", "dialogue", "model", "hello", 100, 50, tools=(tool,)
    )
    with pytest.raises(ProviderError):
        provider.stream(request, lambda _: None)


class EchoTool:
    def describe(self):
        return ToolDescriptor(
            name="echo",
            description="Echo one text argument.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            purposes=("dialogue", "proactive"),
            source="test.echo",
        )

    def available(self, purpose):
        return purpose in ("dialogue", "proactive")

    def execute(self, context):
        return ToolOutcome("echo:" + str(context.arguments.get("text", "")))


def test_plugin_registration_execution_and_tools_endpoint(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall("call-1", "echo", '{"text": "hello"}')
    client, app, _, _ = make_runtime(tmp_path, provider)
    try:
        app.state.tools.register(EchoTool())
        response = send(client)
        assert response.status_code == 200, response.text
        assert response.json()["assistant_message"] == "好。"
        assert provider.requests[1].tool_results[0].content == "echo:hello"
        listed = client.get("/v1/companions/sui/tools").json()
        tool = next(item for item in listed["tools"] if item["name"] == "echo")
        assert tool["source"] == "test.echo"
        assert tool["available"] is True
        assert tool["purposes"] == ["dialogue", "proactive"]
        proactive = client.get("/v1/companions/sui/tools?purpose=proactive").json()
        assert any(item["name"] == "echo" for item in proactive["tools"])
    finally:
        client.close()


def test_configured_plugin_factory_receives_service_context(tmp_path, monkeypatch):
    captured = {}
    module = types.ModuleType("virtual_life_test_plugin")

    def build(context):
        captured["context"] = context
        return EchoTool()

    module.build = build
    monkeypatch.setitem(sys.modules, "virtual_life_test_plugin", module)
    provider = Provider()
    client, app, _, _ = make_runtime(
        tmp_path,
        provider,
        plugins=("virtual_life_test_plugin:build",),
    )
    try:
        assert "echo" in app.state.tools.names
        context = captured["context"]
        assert context.coordinator is app.state.coordinator
        assert context.settings.tools_plugins == ("virtual_life_test_plugin:build",)
    finally:
        client.close()


def test_tool_registry_rejects_duplicates_and_requires_tools_for_plugins():
    registry = ToolRegistry()
    registry.register(EchoTool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())
    with pytest.raises(ValueError, match="tools.enabled"):
        Settings(
            database_path=".data/plugin-test.sqlite3",
            tools_enabled=False,
            tools_plugins=("virtual_life_test_plugin:build",),
        )


class FakeDelegationExecutor:
    def __init__(self):
        self.calls = []

    def run(self, instruction, agent):
        self.calls.append((instruction, agent))
        return DelegationOutcome("completed", "remote:" + instruction)


def test_custom_delegation_executor_uses_durable_queue(tmp_path):
    provider = Provider()
    provider.tool_call = ToolCall(
        "call-1",
        "delegate_task",
        '{"instruction": "summarize", "agent": "remote"}',
    )
    delegation = DelegationConfig(enabled=True, command=())
    executor = FakeDelegationExecutor()
    client, app, _, _ = make_runtime(
        tmp_path,
        provider,
        delegation=delegation,
        delegation_executor=executor,
    )
    try:
        assert send(client).status_code == 200
        assert app.state.delegation.process_next() is True
        task = client.get("/v1/companions/sui/delegations").json()["items"][0]
        assert task["status"] == "completed"
        assert task["result_text"] == "remote:summarize"
        assert executor.calls == [("summarize", "remote")]
    finally:
        client.close()
