"""Cumulative clarification, immutable evidence and independent recovery."""

import json
from datetime import timedelta

import pytest
import test_emotion
from pydantic import ValidationError
from test_emotion import emotion, experiences, seed_resonance, send
from virtual_life.emotion import EmotionCorrection, EmotionPolicy, EmotionService
from virtual_life.personality import PersonalityError

runtime = test_emotion.runtime


def correct(runtime, kind, command="clarify", event_id=None, **kwargs):
    client, app, _, _, _ = runtime
    return app.state.emotion.correct(
        "sui",
        event_id or experiences(client)[0]["event_id"],
        {
            "command_id": command,
            "expected_revision": emotion(client)["revision"],
            "kind": kind,
            **kwargs,
        },
    )


@pytest.mark.parametrize("kind", ["direction", "intensity", "attribution"])
def test_correction_semantics_and_original_evidence(runtime, kind):
    client, app, _, _, _ = runtime
    send(runtime)
    before, initial = emotion(client), experiences(client)[0]
    with app.state.emotion.store.transaction() as connection:
        raw = connection.execute("SELECT appraisal_json FROM emotion_experiences").fetchone()[0]
    after = correct(
        runtime, kind, **({"attribution": "situation"} if kind == "attribution" else {})
    )
    item = experiences(client)[0]
    assert item["status"] == "corrected"
    assert item["original_delta"] == initial["delta"]
    assert item["delta"]["energy"] == initial["delta"]["energy"] < 0
    assert after["effective_state"]["energy"] == before["effective_state"]["energy"]
    assert item["confidence"] <= initial["confidence"]
    assert item["uncertainty"] >= initial["uncertainty"]
    assert item["appraisal"]["uncertainty"] >= initial["appraisal"]["uncertainty"]
    assert before["context_version"] != after["context_version"]
    if kind == "direction":
        assert after["state"] == after["baseline"]
        assert item["reaction"] == "neutral"
    elif kind == "intensity":
        assert item["delta"]["valence"] == initial["delta"]["valence"] / 2
        assert item["appraisal"]["pleasantness"] == initial["appraisal"]["pleasantness"] / 2
    else:
        assert item["delta"]["valence"] == initial["delta"]["valence"]
        assert item["delta"]["arousal"] == initial["delta"]["arousal"]
        assert item["delta"]["control"] == initial["delta"]["control"] / 2
        assert item["attribution"] == "situation"
    with app.state.emotion.store.transaction() as connection:
        assert (
            connection.execute("SELECT appraisal_json FROM emotion_experiences").fetchone()[0]
            == raw
        )
    assert EmotionService(app.state.personality, app.state.memory).get("sui") == after


def test_intensity_is_cumulative_and_direction_never_flips(runtime):
    client, _, _, _, _ = runtime
    send(runtime)
    original = experiences(client)[0]["delta"]["valence"]
    correct(runtime, "intensity", retained_weight=0.2)
    correct(runtime, "intensity", command="again", retained_weight=0.5)
    assert experiences(client)[0]["delta"]["valence"] == pytest.approx(original * 0.1)
    correct(runtime, "direction", command="direction")
    correct(runtime, "intensity", command="zero", retained_weight=1.0)
    assert experiences(client)[0]["delta"]["valence"] == 0
    assert emotion(client)["state"] == emotion(client)["baseline"]


def test_idempotency_cas_cross_companion_and_invalidated(runtime):
    client, app, _, _, _ = runtime
    send(runtime)
    event = experiences(client)[0]["event_id"]
    payload = {
        "command_id": "clarify",
        "expected_revision": emotion(client)["revision"],
        "kind": "direction",
    }
    result = app.state.emotion.correct("sui", event, payload)
    assert app.state.emotion.correct("sui", event, payload) == result
    with pytest.raises(PersonalityError) as error:
        app.state.emotion.correct("sui", event, {**payload, "command_id": "stale"})
    assert error.value.code == "revision_conflict"
    client.post("/v1/companions", json={"companion_id": "other", "name": "另一位"})
    with pytest.raises(PersonalityError) as error:
        app.state.emotion.correct(
            "other", event, {**payload, "command_id": "cross", "expected_revision": 0}
        )
    assert error.value.status_code == 404
    app.state.emotion.invalidate(
        "sui", event, {"command_id": "invalidate", "expected_revision": result["revision"]}
    )
    with pytest.raises(PersonalityError) as error:
        correct(runtime, "direction", command="invalid")
    assert error.value.code == "experience_invalidated"


def test_corrected_sources_revoke_resonance_and_forgetting_hides_details(runtime):
    client, app, _, _, _ = runtime
    _, records, _, _ = seed_resonance(runtime)
    correct(runtime, "intensity")
    assert not experiences(client)[-1]["resonance_valid"]
    send(runtime, "new-recall", "夜航船")
    assert experiences(client)[-1]["resonance_delta"]["valence"] == pytest.approx(
        experiences(client)[0]["delta"]["valence"] * 0.2
    )
    with app.state.emotion.store.transaction() as connection:
        resonance = json.loads(
            connection.execute(
                "SELECT resonance_json FROM emotion_experiences ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert list(resonance["correction_revisions"].values()) == [
        experiences(client)[0]["correction"]["revision"]
    ]
    app.state.memory.delete("sui", records[0]["memory_id"])
    item = experiences(client)[0]
    assert item["status"] == "forgotten"
    assert item["appraisal"] is item["original_delta"] is item["correction"] is None
    with pytest.raises(PersonalityError) as error:
        correct(runtime, "direction", command="forgotten")
    assert error.value.code == "experience_forgotten"


def test_legacy_appraisal_and_resonance_compatibility(runtime):
    client, app, _, _, _ = runtime
    seed_resonance(runtime)
    with app.state.emotion.store.transaction(write=True) as connection:
        for row in connection.execute(
            "SELECT event_id, appraisal_json, resonance_json FROM emotion_experiences"
        ).fetchall():
            appraisal = json.loads(row["appraisal_json"])
            for key in ("appraisal", "appraisal_version", "uncertainty", "attribution"):
                appraisal.pop(key, None)
            resonance = json.loads(row["resonance_json"])
            resonance.pop("correction_revisions", None)
            connection.execute(
                "UPDATE emotion_experiences SET appraisal_json=?, resonance_json=? WHERE event_id=?",
                (json.dumps(appraisal), json.dumps(resonance), row["event_id"]),
            )
    assert experiences(client)[0]["appraisal"] is None
    assert experiences(client)[0]["appraisal_version"] == "legacy_v1"
    assert experiences(client)[0]["uncertainty"] == 1
    assert experiences(client)[-1]["resonance_valid"]
    correct(runtime, "intensity")
    assert not experiences(client)[-1]["resonance_valid"]


def test_dimension_recovery_and_time_stable_context(runtime):
    client, app, clock, _, _ = runtime
    app.state.emotion.policy = EmotionPolicy(
        valence_half_life_seconds=60,
        arousal_half_life_seconds=120,
        control_half_life_seconds=240,
        resource_half_life_seconds=480,
    )
    send(runtime)
    first = emotion(client)
    baseline_resources = app.state.personality.get_state("sui")["state"]
    clock.value += timedelta(seconds=120)
    later = emotion(client)
    assert later["context_version"] == first["context_version"]
    for key, half_life in first["recovery_half_lives"].items():
        if key in first["state"]:
            old = first["state"][key] - first["baseline"][key]
            new = later["state"][key] - later["baseline"][key]
        else:
            old = first["effective_state"][key] - baseline_resources[key]
            new = later["effective_state"][key] - baseline_resources[key]
        assert new == pytest.approx(old * 2 ** (-120 / half_life), abs=1e-6)


def test_correction_during_generation_fences_output(runtime):
    client, _, _, provider, _ = runtime
    provider.action = lambda: correct(runtime, "direction")
    result = send(runtime).json()
    assert result["status"] == "discarded"
    assert result["usage"]["input_tokens"] == 70
    assert experiences(client)[0]["delta"]["energy"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "direction", "retained_weight": 0.5},
        {"kind": "intensity", "attribution": "self"},
        {"kind": "attribution"},
        {"kind": "intensity", "retained_weight": None},
        {"kind": "intensity", "retained_weight": 1.1},
        {"kind": "direction", "unknown": True},
    ],
)
def test_correction_request_rejects_inapplicable_fields(fields):
    with pytest.raises(ValidationError):
        EmotionCorrection(command_id="test", expected_revision=0, **fields)


def test_correction_transaction_rolls_back_receipt_state_and_history(runtime, monkeypatch):
    client, app, _, _, _ = runtime
    send(runtime)
    before = emotion(client)
    original = app.state.emotion._bump

    def fail(*args):
        original(*args)
        raise RuntimeError("injected")

    monkeypatch.setattr(app.state.emotion, "_bump", fail)
    with pytest.raises(RuntimeError, match="injected"):
        correct(runtime, "intensity")
    assert emotion(client) == before
    assert experiences(client)[0]["status"] == "active"
    with app.state.emotion.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM emotion_corrections").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE type='companion.emotion_corrected'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM personality_commands WHERE command_id='clarify'"
            ).fetchone()[0]
            == 0
        )


def test_longest_recovery_horizon_and_original_quiet_ttl(runtime):
    client, app, clock, _, _ = runtime
    app.state.emotion.policy = EmotionPolicy(half_life_seconds=60, arousal_half_life_seconds=7200)
    send(runtime, message="先安静陪我读一会儿")
    first = emotion(client)
    clock.value += timedelta(seconds=60 * 25)
    later = emotion(client)
    assert not later["quiet_requested"]
    assert later["active_experiences"] == 1
    assert later["state"]["arousal"] < later["baseline"]["arousal"]
    assert later["context_version"] == first["context_version"]
    clock.value += timedelta(seconds=7200 * 25)
    assert emotion(client)["state"] == emotion(client)["baseline"]


def test_correction_scales_own_resonance_without_scaling_reply_cost(runtime):
    client, app, _, _, _ = runtime
    _, _, _, item = seed_resonance(runtime)
    correct(runtime, "intensity", event_id=item["event_id"], retained_weight=0.25)
    updated = experiences(client)[-1]
    assert updated["resonance_delta"]["valence"] == item["resonance_delta"]["valence"] * 0.25
    assert updated["delta"]["energy"] == item["delta"]["energy"]
    correct(runtime, "direction", command="direction", event_id=item["event_id"])
    assert experiences(client)[-1]["resonance_delta"]["valence"] == 0
    assert experiences(client)[-1]["reaction"] == "neutral"


def test_default_and_explicit_intensity_share_command_receipt(runtime):
    client, app, _, _, _ = runtime
    send(runtime)
    event = experiences(client)[0]["event_id"]
    payload = {
        "command_id": "same",
        "expected_revision": emotion(client)["revision"],
        "kind": "intensity",
    }
    result = app.state.emotion.correct("sui", event, payload)
    assert app.state.emotion.correct("sui", event, {**payload, "retained_weight": 0.5}) == result
