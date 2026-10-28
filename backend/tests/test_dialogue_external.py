"""External generation mode: park a turn, then commit a reply generated elsewhere.

DSH (or any outside agent) can take over the model call. The runtime must still
own the dispatch gate, the token reservation, the memory/emotion side effects, and
the durable receipt, so an externally generated turn is indistinguishable from a
locally generated one after commit.
"""

import json
import sqlite3

from fastapi.testclient import TestClient
from test_api import attach_desktop
from virtual_life.api import create_app
from virtual_life.settings import Settings


class RecordingProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise AssertionError("external generation must not call the local provider")


def build(tmp_path):
    provider = RecordingProvider()
    settings = Settings(
        database_path=str(tmp_path / "external.sqlite"),
        api_token="test-token",
        model_base_url="http://model.invalid",
        model_name="fake",
    )
    app = create_app(settings, model_provider=provider)
    return settings, provider, app


def park(client, state, command_id="external-1", message="在吗"):
    return client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": command_id,
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": message,
            "generator": "external",
        },
    )


def test_external_park_returns_prompt_without_calling_a_model(tmp_path):
    _settings, provider, app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        response = park(client, state)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["status"] == "awaiting_generation"
        assert receipt["assistant_message"] is None
        assert receipt["preview"]["context_text"].strip()
        assert receipt["preview"]["token_budget"] > 0
        assert receipt["accepted_event_id"]
        assert provider.calls == 0
        # the parked receipt is readable but carries no committed reply
        read = client.get("/v1/companions/sui/dialogue/external-1")
        assert read.status_code == 200, read.text
        assert read.json()["status"] == "awaiting_generation"
        assert read.json()["assistant_message"] is None


def test_commit_completes_the_turn_and_emits_the_completed_event(tmp_path):
    _settings, provider, app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        assert park(client, state).status_code == 200
        response = client.post(
            "/v1/companions/sui/dialogue/external-1/commit",
            json={
                "assistant_message": "在的，刚把书合上。",
                "model": "external-agent",
                "usage": {"input_tokens": 900, "output_tokens": 12},
                "tool_trace": [{"name": "virtual_life_proactivity_status"}],
            },
        )
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["status"] == "completed"
        assert receipt["assistant_message"] == "在的，刚把书合上。"
        assert receipt["model"] == "external-agent"
        assert receipt["usage"] == {"input_tokens": 900, "output_tokens": 12}
        assert receipt["source_event_id"]
        assert provider.calls == 0
        # the durable receipt replays the committed text
        replay = client.get("/v1/companions/sui/dialogue/external-1")
        assert replay.json()["assistant_message"] == "在的，刚把书合上。"


def test_commit_is_idempotent_by_command_id(tmp_path):
    _settings, _provider, app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        assert park(client, state).status_code == 200
        first = client.post(
            "/v1/companions/sui/dialogue/external-1/commit",
            json={"assistant_message": "第一次。"},
        )
        second = client.post(
            "/v1/companions/sui/dialogue/external-1/commit",
            json={"assistant_message": "第二次，不应写入。"},
        )
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json()
        assert second.json()["assistant_message"] == "第一次。"


def test_commit_without_a_parked_turn_is_not_found(tmp_path):
    _settings, _provider, app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/dialogue/missing/commit",
            json={"assistant_message": "hi"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_commit_strips_an_own_name_role_label(tmp_path):
    _settings, _provider, app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        assert park(client, state).status_code == 200
        response = client.post(
            "/v1/companions/sui/dialogue/external-1/commit",
            json={"assistant_message": "随: 在的。"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["assistant_message"] == "在的。"


def test_an_expired_parked_turn_is_discarded(tmp_path):
    settings, _provider, app = build(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        assert park(client, state).status_code == 200
        # Age the parked turn past the commit deadline.
        connection = sqlite3.connect(settings.database_path)
        try:
            stored = json.loads(
                connection.execute(
                    "SELECT result_json FROM dialogue_commands WHERE command_id = ?",
                    ("external-1",),
                ).fetchone()[0]
            )
            stored["pending"]["parked_at"] = "2020-01-01T00:00:00.000000+00:00"
            connection.execute(
                "UPDATE dialogue_commands SET result_json = ? WHERE command_id = ?",
                (json.dumps(stored), "external-1"),
            )
            connection.commit()
        finally:
            connection.close()
        response = client.post(
            "/v1/companions/sui/dialogue/external-1/commit",
            json={"assistant_message": "太晚了。"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "dialogue_commit_expired"
        # The command is settled as discarded, so a retry replays instead of writing.
        replay = client.get("/v1/companions/sui/dialogue/external-1")
        assert replay.status_code == 200, replay.text
        assert replay.json()["status"] == "discarded"
        assert replay.json()["discard_reason"] == "dialogue_commit_expired"


def test_internal_is_still_the_default_and_calls_the_provider(tmp_path):
    calls = []

    class CountingProvider:
        def complete(self, request):
            from virtual_life.ports import ModelResult, ModelUsage

            calls.append(request)
            return ModelResult("嗯。", "fake", ModelUsage(10, 2))

    settings = Settings(
        database_path=str(tmp_path / "internal.sqlite"),
        api_token="test-token",
        model_base_url="http://model.invalid",
        model_name="fake",
    )
    app = create_app(settings, model_provider=CountingProvider())
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/dialogue",
            json={
                "command_id": "internal-1",
                "node_id": "desk",
                "epoch": state["epoch"],
                "expected_revision": state["revision"],
                "user_message": "在吗",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        assert response.json()["assistant_message"] == "嗯。"
        assert len(calls) == 1
