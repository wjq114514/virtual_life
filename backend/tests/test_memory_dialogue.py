"""Public memory contracts and memory changes during model execution."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop, companion_source
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


@pytest.fixture
def client(tmp_path):
    app = create_app(
        Settings(database_path=str(tmp_path / "runtime.sqlite"), api_token="test-token")
    )
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        yield client


def test_memory_api_layers_correction_and_metadata(client):
    attach_desktop(client)
    source = companion_source(client)
    base = "/v1/companions/sui/memories"
    payload = {
        "command_id": "fact",
        "kind": "preference",
        "content": "喜欢茶",
        "semantic_key": "drink",
        "source_event_id": source,
        "pinned": True,
    }
    response = client.post(base, json=payload)
    assert response.status_code == 201, response.text
    old = response.json()
    assert old["revision"] == 1 and old["layer"] == "semantic"
    response = client.post(
        base, json={**payload, "command_id": "missing", "source_event_id": "missing"}
    )
    assert response.status_code == 422
    response = client.post(base, json={**payload, "command_id": "implicit", "content": "喜欢咖啡"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "memory_correction_required"
    correction = {
        "command_id": "correct",
        "expected_revision": old["revision"],
        "content": "喜欢咖啡",
        "source_event_id": source,
    }
    url = f"{base}/{old['memory_id']}/corrections"
    response = client.post(url, json=correction)
    assert response.status_code == 201, response.text
    current = response.json()
    assert current["supersedes_id"] == old["memory_id"] and current["pinned"]
    assert client.post(url, json=correction).json() == current
    assert client.post(url, json={**correction, "command_id": "stale"}).status_code == 409
    response = client.patch(
        f"{base}/{current['memory_id']}",
        json={"command_id": "unpin", "expected_revision": 1, "pinned": False, "confidence": 0.2},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2 and not response.json()["pinned"]
    response = client.post(
        base,
        json={
            "command_id": "episode",
            "kind": "episode",
            "content": "共读",
            "source_event_id": source,
        },
    )
    assert response.status_code == 201, response.text
    for layer, kind in (("episodic", "episode"), ("semantic", "preference")):
        response = client.post(f"{base}/recall", json={"layer": layer})
        assert response.status_code == 200, response.text
        assert [record["kind"] for record in response.json()["items"]] == [kind]
    preview = client.post(
        "/v1/companions/sui/personality/preview", json={"user_message": "喜欢喝什么"}
    )
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["memory_versions"]
    assert [item["memory_id"] for item in result["memory_versions"]] == result["memory_refs"]
    version = next(
        item for item in result["memory_versions"] if item["memory_id"] == current["memory_id"]
    )
    assert version["revision"] == 2


class Clock:
    def __init__(self):
        self.value = datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value


class Provider:
    def __init__(self):
        self.calls = 0
        self.action = lambda: None

    def complete(self, request):
        self.calls += 1
        assert "旧的阅读偏好" in request.context_text
        self.action()
        return ModelResult("依据旧认识生成的回答", "fake", ModelUsage(80, 20))


@pytest.mark.parametrize("change", ["correct", "delete", "metadata", "expire", "unrelated"])
def test_memory_change_during_dialogue_keeps_billing_and_checks_used_versions(tmp_path, change):
    clock = Clock()
    provider = Provider()
    settings = Settings(
        database_path=str(tmp_path / "dialogue.sqlite"),
        api_token="test-token",
        model_base_url="http://model.invalid",
        model_name="fake",
    )
    app = create_app(settings, model_provider=provider)
    app.state.coordinator.clock = clock
    app.state.personality.clock = clock
    app.state.memory.clock = clock
    app.state.dialogue.clock = clock
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        source = companion_source(client)
        payload = {
            "command_id": "original",
            "kind": "preference",
            "content": "旧的阅读偏好",
            "semantic_key": "reading",
            "source_event_id": source,
            "pinned": True,
        }
        if change == "expire":
            payload["valid_to"] = (clock.value + timedelta(seconds=1)).isoformat()
        response = client.post("/v1/companions/sui/memories", json=payload)
        assert response.status_code == 201, response.text
        memory = response.json()
        service = app.state.memory

        def mutate():
            if change == "correct":
                service.correct(
                    "sui",
                    memory["memory_id"],
                    {
                        "command_id": "correction",
                        "expected_revision": 1,
                        "content": "新的阅读偏好",
                        "source_event_id": source,
                    },
                )
            elif change == "delete":
                service.delete("sui", memory["memory_id"])
            elif change == "metadata":
                service.update_metadata(
                    "sui",
                    memory["memory_id"],
                    {
                        "command_id": "metadata",
                        "expected_revision": 1,
                        "confidence": 0.1,
                    },
                )
            elif change == "expire":
                clock.value += timedelta(seconds=2)
            else:
                service.write(
                    "sui",
                    {
                        "command_id": "unrelated",
                        "kind": "fact",
                        "content": "另一个无关事实",
                        "source_event_id": source,
                    },
                )

        provider.action = mutate
        request = {
            "command_id": "dialogue",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": "旧的阅读偏好是什么？",
            "max_output_tokens": 64,
        }
        response = client.post("/v1/companions/sui/dialogue", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["preview"]["memory_versions"] == [
            {"memory_id": memory["memory_id"], "revision": 1}
        ]
        assert result["status"] == ("completed" if change == "unrelated" else "discarded")
        if change != "unrelated":
            assert result["assistant_message"] is None and result["source_event_id"] is None
        quota = service.budget_snapshot("sui")
        assert quota["spent_tokens"] == 100 and quota["reserved_tokens"] == 0
        assert client.post("/v1/companions/sui/dialogue", json=request).json() == result
        assert provider.calls == 1


def test_changed_memory_before_dispatch_releases_reservation(tmp_path):
    provider = Provider()
    settings = Settings(
        database_path=str(tmp_path / "before-dispatch.sqlite"),
        api_token="test-token",
        model_base_url="http://model.invalid",
        model_name="fake",
    )
    app = create_app(settings, model_provider=provider)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        source = companion_source(client)
        record = app.state.memory.write(
            "sui",
            {
                "command_id": "memory",
                "kind": "fact",
                "content": "旧的阅读偏好",
                "source_event_id": source,
            },
        )
        preview = app.state.personality_prompt_builder.preview

        def changed_preview(*args, **kwargs):
            result = preview(*args, **kwargs)
            app.state.memory.delete("sui", record["memory_id"])
            return result

        app.state.personality_prompt_builder.preview = changed_preview
        response = client.post(
            "/v1/companions/sui/dialogue",
            json={
                "command_id": "dialogue",
                "node_id": "desk",
                "epoch": state["epoch"],
                "expected_revision": state["revision"],
                "user_message": "旧的阅读偏好",
                "max_output_tokens": 64,
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "memory_context_changed"
        assert provider.calls == 0
        quota = app.state.memory.budget_snapshot("sui")
        assert quota["reserved_tokens"] == quota["spent_tokens"] == 0


def test_historical_memory_receipt_without_new_fields_remains_readable(client):
    import json

    attach_desktop(client)
    base = "/v1/companions/sui/memories"
    payload = {
        "command_id": "original",
        "kind": "episode",
        "content": "一段旧经历",
        "source_event_id": companion_source(client),
    }
    record = client.post(base, json=payload).json()
    old = {
        key: value
        for key, value in record.items()
        if key
        not in (
            "revision",
            "layer",
            "occurred_at",
            "episode_key",
            "valid_from",
            "valid_to",
            "supersedes_id",
        )
    }
    with client.app.state.memory._connection(write=True) as connection:
        connection.execute(
            "UPDATE memory_commands SET result_json = ? WHERE command_id = 'original'",
            (json.dumps(old),),
        )
    assert client.delete(f"{base}/{record['memory_id']}").status_code == 200
    response = client.post(base, json=payload)
    assert response.status_code == 201, response.text
    upgraded = response.json()
    assert upgraded["layer"] == "episodic" and upgraded["revision"] == 1
    assert upgraded["occurred_at"] is None and upgraded["content"] == old["content"]
    assert client.get(base).json()["items"] == []
