"""Negativity-bias decay: negative reactions linger longer than positive ones.

Reflects Baumeister et al. (2001) and the asymmetric decay in WASABI
(Becker-Asano 2008). Configured via `negative_half_life_seconds`; when unset the
shared base half-life applies to all reactions.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.emotion import EmotionPolicy
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class Provider:
    def complete(self, request):
        return ModelResult("我在，陪你。", "fake", ModelUsage(60, 8))


def build_app(tmp_path, policy=None):
    clock = Clock()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "neg.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
            emotion_policy=policy or EmotionPolicy(),
        ),
        model_provider=Provider(),
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.memory,
        app.state.dialogue,
        app.state.emotion,
    ):
        service.clock = clock
    return app, clock


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


def emotion(client):
    return client.get("/v1/companions/sui/emotion").json()


def run_case(tmp_path, message, policy, elapsed):
    app, clock = build_app(tmp_path, policy)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        assert send(client, state, "one", message).status_code == 200
        first = emotion(client)
        clock.value += timedelta(seconds=elapsed)
        later = emotion(client)
        return first, later


def test_default_no_negative_slowdown(tmp_path):
    # Without negative_half_life_seconds, a concern decays with the shared base.
    first, later = run_case(tmp_path, "我今天很难过", None, 1200)  # one base half-life
    for key in ("valence", "arousal", "control"):
        assert later["state"][key] - later["baseline"][key] == pytest.approx(
            (first["state"][key] - first["baseline"][key]) / 2, abs=1e-6
        )


def test_negative_reaction_slowdown_when_configured(tmp_path):
    policy = EmotionPolicy(negative_half_life_seconds=2400)
    first, later = run_case(tmp_path, "我今天很难过", policy, 1200)
    # After one BASE half-life, a 2400s negative half-life leaves ~2^-0.5 of the
    # impulse, strictly more than the 0.5 factor of an unslowed reaction. The
    # control dimension has no concern delta, so only valence/arousal apply.
    for key in ("valence", "arousal"):
        ratio = (later["state"][key] - later["baseline"][key]) / (
            first["state"][key] - first["baseline"][key]
        )
        assert ratio == pytest.approx(2**-0.5, abs=1e-4)
        assert ratio > 0.5  # slower than the shared base would allow


def test_positive_reaction_unaffected_by_negative_slowdown(tmp_path):
    policy = EmotionPolicy(negative_half_life_seconds=2400)
    first, later = run_case(tmp_path, "谢谢你陪我读书", policy, 1200)
    # joy is not in _NEGATIVE_REACTIONS, so it still uses the base half-life.
    for key in ("valence", "arousal", "control"):
        ratio = (later["state"][key] - later["baseline"][key]) / (
            first["state"][key] - first["baseline"][key]
        )
        assert ratio == pytest.approx(0.5, abs=1e-4)


def test_per_dimension_override_wins_over_negative_slowdown(tmp_path):
    policy = EmotionPolicy(
        negative_half_life_seconds=2400,
        valence_half_life_seconds=600,
    )
    first, later = run_case(tmp_path, "我今天很难过", policy, 1200)
    # The explicit valence half-life (600) wins over the negative adjustment for
    # the valence dimension.
    ratio = (later["state"]["valence"] - later["baseline"]["valence"]) / (
        first["state"]["valence"] - first["baseline"]["valence"]
    )
    assert ratio == pytest.approx(2**-2, abs=1e-4)  # 1200/600 = 2 half-lives
