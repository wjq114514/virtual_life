"""Authenticated scoped diagnostics, atomicity and mutation-free retrieval."""

import json

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop, companion_source
from test_memory import Clock
from virtual_life.api import create_app
from virtual_life.memory import MemoryService
from virtual_life.settings import Settings


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "logs.sqlite"), api_token="test-token"))
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        attach_desktop(client)
        yield client


def test_logs_auth_scope_cursor_and_filters(client):
    service = client.app.state.memory
    for companion, command, status in [
        ("sui", "a", "completed"),
        ("other", "foreign", "failed"),
        ("sui", "b", "failed"),
        ("sui", "c", "completed"),
    ]:
        service.logs.record(companion, "recall", status, command_id=command)
    url = "/v1/companions/sui/memories/logs"
    assert client.get(url, headers={"Authorization": ""}).status_code == 401
    page = client.get(url, params={"limit": 1, "operation": "recall"}).json()
    assert page["has_more"] and page["items"][0]["command_id"] == "a"
    following = client.get(
        url, params={"after_sequence": page["next_sequence"], "operation": "recall"}
    ).json()
    assert [x["command_id"] for x in following["items"]] == ["b", "c"]
    assert all(x["companion_id"] == "sui" for x in following["items"])
    assert (
        client.get(url, params={"operation": "recall", "status": "failed"}).json()["items"][0][
            "command_id"
        ]
        == "b"
    )
    assert len(client.get(url, params={"command_id": "c"}).json()["items"]) == 1
    for parameters in (
        {"limit": 0},
        {"after_sequence": -1},
        {"operation": "bad"},
        {"status": "bad"},
    ):
        assert client.get(url, params=parameters).status_code == 422


def test_redaction_size_bound_and_transaction_rollback(tmp_path):
    memory = MemoryService(tmp_path / "redaction.sqlite", clock=Clock())
    memory.logs.record(
        "sui",
        "recall",
        details={
            "query": "Bearer secretvalue sk-abc123 token=private",
            "password": "hidden",
            "selected": [
                {"memory_id": str(i), "source_event_ids": ["x" * 4096] * 10} for i in range(100)
            ],
        },
    )
    item = memory.logs.list("sui")["items"][0]
    encoded = json.dumps(item["details"], ensure_ascii=False)
    assert "secretvalue" not in encoded and "abc123" not in encoded and "private" not in encoded
    assert "password" not in item["details"]
    assert len(encoded.encode()) <= 16384
    with pytest.raises(RuntimeError), memory._connection(write=True) as connection:
        memory.logs.record("sui", "write", command_id="rolled-back", connection=connection)
        raise RuntimeError("rollback")
    assert memory.logs.list("sui", command_id="rolled-back")["items"] == []


def test_receipt_replay_and_recall_do_not_mutate_memory(client):
    memory = client.app.state.memory
    source = companion_source(client)
    payload = {
        "command_id": "original",
        "kind": "fact",
        "content": "林舟生日6月18日",
        "semantic_key": "user.birthday",
        "source_event_id": source,
    }
    base = "/v1/companions/sui/memories"
    first = client.post(base, json=payload).json()
    assert client.post(base, json=payload).json() == first
    before = memory.list("sui")["items"]
    for _ in range(2):
        assert (
            client.post(base + "/recall", json={"semantic_key": "user.birthday"}).status_code == 200
        )
    assert memory.list("sui")["items"] == before
    logs = client.get(base + "/logs", params={"command_id": "original"}).json()["items"]
    assert any(x["operation"] == "write" for x in logs)
    with memory._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_usage_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("outcome", ["completed", "failed", "discarded"])
def test_final_dialogue_correlation_and_only_successful_context_exposure(tmp_path, outcome):
    from virtual_life.model_provider import ProviderError
    from virtual_life.ports import ModelResult, ModelUsage

    class Provider:
        calls = 0

        def action(self):
            return None

        def complete(self, request):
            self.calls += 1
            self.action()
            if outcome == "failed":
                raise ProviderError("test_failure", "scripted failure", usage=ModelUsage(8, 2))
            return ModelResult("我记得你喜欢乌龙茶。", "fake", ModelUsage(8, 2))

    provider = Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "dialogue.sqlite"),
            api_token="test-token",
            model_base_url="http://fake.invalid",
            model_name="fake",
        ),
        model_provider=provider,
    )
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        memory = app.state.memory
        record = memory.write(
            "sui",
            {
                "command_id": "tea",
                "kind": "preference",
                "content": "喜欢乌龙茶",
                "source_event_id": companion_source(client),
                "pinned": True,
            },
        )
        if outcome == "discarded":
            provider.action = lambda: memory.update_metadata(
                "sui",
                record["memory_id"],
                {"command_id": "change", "expected_revision": 1, "confidence": 0.4},
            )
        assert (
            client.post(
                "/v1/companions/sui/personality/preview", json={"user_message": "乌龙茶"}
            ).status_code
            == 200
        )
        payload = {
            "command_id": "dialogue-check",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": "乌龙茶",
            "max_output_tokens": 64,
        }
        response = client.post("/v1/companions/sui/dialogue", json=payload)
        assert response.status_code == (502 if outcome == "failed" else 200)
        if outcome != "failed":
            assert response.json()["status"] == outcome
            assert (
                client.post("/v1/companions/sui/dialogue", json=payload).json() == response.json()
            )
        with memory._connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_usage_receipts").fetchone()[
                0
            ] == int(outcome == "completed")
            assert connection.execute("SELECT COUNT(*) FROM memory_access_state").fetchone()[
                0
            ] == int(outcome == "completed")
        logs = memory.logs.list("sui", command_id="dialogue-check")["items"]
        assert any(item["operation"] == "recall" for item in logs)
        assert any(item["operation"] == "dialogue" for item in logs)
        assert provider.calls == 1
        assert memory.budget_snapshot("sui")["spent_tokens"] == 10
