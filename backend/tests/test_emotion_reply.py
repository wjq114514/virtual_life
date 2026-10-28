"""Emotion-driven reply behavior: tone and verbosity follow the effective mood.

The participation block must expose a human-readable tone hint and nudge the
reply-length baseline so the model's reply character actually changes with the
companion's emotional state, not just its resource gating.
"""

from fastapi.testclient import TestClient
from test_api import attach_desktop
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class Provider:
    def complete(self, request):
        return ModelResult("我在，陪你。", "fake", ModelUsage(60, 8))


def build_app(tmp_path):
    app = create_app(
        Settings(
            database_path=str(tmp_path / "reply.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        model_provider=Provider(),
    )
    return app


def send(client, state, command, message):
    return client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": command,
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": message,
        },
    )


def preview(client, message="继续吧"):
    response = client.post("/v1/companions/sui/personality/preview", json={"user_message": message})
    assert response.status_code == 200, response.text
    return response.json()


def test_positive_mood_raises_verbosity_and_warm_tone(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "one", "谢谢你陪我读书").status_code == 200
        result = preview(client)
        part = result["participation"]
        assert result["emotion"]["effective_state"]["mood"] == "cheerful"
        assert part["tone"] == "warm"
        # brief style (2) + cheerful (+1) = 3
        assert part["max_reply_sentences"] == 3
        assert "tone" in result["context_text"] or "warm" in result["context_text"]


def test_negative_mood_restrains_verbosity_and_soothing_tone(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "one", "我今天很难过").status_code == 200
        result = preview(client)
        part = result["participation"]
        assert part["tone"] in ("soothing", "terse", "subdued", "gentle")
        # brief (2) + concern (-1) = 1, floor at 1
        assert part["max_reply_sentences"] <= 2


def test_annoyance_is_terse_and_restrained(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "one", "气死我了").status_code == 200
        result = preview(client)
        part = result["participation"]
        assert result["emotion"]["effective_state"]["mood"] == "annoyed"
        assert part["tone"] == "terse"
        assert part["max_reply_sentences"] == 1


def test_focus_cap_still_wins_over_positive_nudge(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "one", "谢谢你陪我读书").status_code == 200
        # focus mode caps at 2 regardless of the +1 cheerful nudge
        response = client.post(
            "/v1/companions/sui/context",
            json={
                "command_id": "focus",
                "node_id": "desk",
                "epoch": state["epoch"],
                "expected_revision": state["revision"],
                "interaction_mode": "focus",
            },
        )
        assert response.status_code == 200, response.text
        result = preview(client)
        assert result["participation"]["max_reply_sentences"] == 2
