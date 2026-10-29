"""HTTP surface for the built-in tool operations (P3: tool-surface unification).

These endpoints expose schedule_next_proactive / schedule_task / delegate_task
as plain HTTP routes so the MCP bridge can present the full runtime tool
surface as MCP tools. Each mutating call is idempotent by (command_id, tool_call_id).
"""

from fastapi.testclient import TestClient
from test_api import attach_desktop
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class NullProvider:
    def complete(self, request):
        return ModelResult("ok", "fake", ModelUsage(10, 2))


def build(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "tools.sqlite"),
        api_token="test-token",
        model_base_url="http://model.invalid",
        model_name="fake",
    )
    return create_app(settings, model_provider=NullProvider())


def test_schedule_proactive_http_roundtrip(tmp_path):
    app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        body = {
            "command_id": "sch-1",
            "tool_call_id": "call-1",
            "delay_seconds": 900,
            "reason": "wait a bit",
        }
        response = client.post(
            "/v1/companions/sui/proactivity/schedule", json=body
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "scheduled"
        assert result["delay_seconds"] == 900
        # idempotent replay
        replay = client.post(
            "/v1/companions/sui/proactivity/schedule", json=body
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["status"] == "scheduled"


def test_schedule_proactive_http_rejects_invalid_delay(tmp_path):
    app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/proactivity/schedule",
            json={
                "command_id": "sch-2",
                "tool_call_id": "call-2",
                "delay_seconds": 5,
            },
        )
        assert response.status_code == 422


def test_scheduled_task_http_roundtrip(tmp_path):
    app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        attach_desktop(client)
        body = {
            "command_id": "task-1",
            "tool_call_id": "call-3",
            "title": "喝水提醒",
            "details": "记得喝水",
            "delay_seconds": 120,
        }
        response = client.post(
            "/v1/companions/sui/scheduled-tasks", json=body
        )
        assert response.status_code == 200, response.text
        task = response.json()
        assert task["status"] == "pending"
        assert task["title"] == "喝水提醒"
        # it appears in the list
        listing = client.get("/v1/companions/sui/scheduled-tasks").json()
        assert listing["total"] == 1
        # idempotent replay
        replay = client.post(
            "/v1/companions/sui/scheduled-tasks", json=body
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["task_id"] == task["task_id"]


def test_delegation_http_requires_configured_executor(tmp_path):
    app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/delegations",
            json={
                "command_id": "del-1",
                "tool_call_id": "call-4",
                "instruction": "run a task",
            },
        )
        # delegation is disabled by default, so no worker runs; the enqueue
        # still creates a pending task record and returns 200.
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "pending"
        listing = client.get("/v1/companions/sui/delegations").json()
        assert listing["total"] >= 1
