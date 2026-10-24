"""HTTP clarification and per-axis configuration integration contracts."""

import os

import pytest
import test_emotion
from test_emotion import emotion, experiences, send
from virtual_life.settings import Settings

runtime = test_emotion.runtime
AXES = ("valence", "arousal", "control", "resource")


def correction_request(runtime, kind, **fields):
    client, _, _, _, _ = runtime
    event_id = experiences(client)[0]["event_id"]
    return (
        f"/v1/companions/sui/emotion/experiences/{event_id}/correct",
        {
            "command_id": "http-correct",
            "expected_revision": emotion(client)["revision"],
            "kind": kind,
            **fields,
        },
    )


@pytest.mark.parametrize(
    ("kind", "fields", "weight"),
    [
        ("direction", {}, None),
        ("intensity", {}, 0.5),
        ("intensity", {"retained_weight": 0.25}, 0.25),
        ("attribution", {"attribution": "situation"}, None),
    ],
)
def test_http_correction_requests_update_history_and_return_snapshot(runtime, kind, fields, weight):
    client, _, _, _, _ = runtime
    assert send(runtime).status_code == 200
    original = experiences(client)[0]
    path, request = correction_request(runtime, kind, **fields)
    response = client.post(path, json=request)
    assert response.status_code == 200, response.text
    assert response.json() == emotion(client)
    item = experiences(client)[0]
    assert item["status"] == "corrected"
    assert item["original_delta"] == original["delta"]
    assert item["correction"]["kind"] == kind
    assert item["correction"]["retained_weight"] == weight
    if kind == "direction":
        assert item["reaction"] == "neutral"
        assert item["delta"]["valence"] == 0
    elif kind == "intensity":
        assert item["delta"]["valence"] == pytest.approx(original["delta"]["valence"] * weight)
    else:
        assert item["attribution"] == "situation"
        assert item["delta"]["valence"] == original["delta"]["valence"]
    assert client.post(path, json=request).json() == response.json()


@pytest.mark.parametrize("explicit_first", [False, True])
def test_http_default_and_explicit_half_weight_share_idempotent_receipt(runtime, explicit_first):
    client, _, _, _, _ = runtime
    send(runtime)
    path, request = correction_request(runtime, "intensity")
    explicit = {**request, "retained_weight": 0.5}
    first, second = (explicit, request) if explicit_first else (request, explicit)
    response = client.post(path, json=first)
    assert response.status_code == 200, response.text
    replay = client.post(path, json=second)
    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    assert experiences(client)[0]["delta"]["valence"] == pytest.approx(
        experiences(client)[0]["original_delta"]["valence"] * 0.5
    )


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("direction", {"retained_weight": 0.5}),
        ("direction", {"retained_weight": None}),
        ("direction", {"attribution": "self"}),
        ("direction", {"attribution": None}),
        ("intensity", {"retained_weight": None}),
        ("intensity", {"attribution": "self"}),
        ("intensity", {"attribution": None}),
        ("attribution", {}),
        ("attribution", {"attribution": None}),
        ("attribution", {"attribution": "self", "retained_weight": None}),
        ("attribution", {"attribution": "other", "retained_weight": 0.5}),
        ("direction", {"unknown": True}),
    ],
)
def test_http_rejects_inapplicable_null_or_unknown_correction_fields(runtime, kind, fields):
    client, _, _, _, _ = runtime
    send(runtime)
    before = emotion(client)
    path, request = correction_request(runtime, kind, **fields)
    response = client.post(path, json=request)
    assert response.status_code == 422, response.text
    assert emotion(client) == before
    assert experiences(client)[0]["status"] == "active"


def test_http_correction_requires_authorization(runtime):
    client, _, _, _, _ = runtime
    send(runtime)
    before = emotion(client)
    path, request = correction_request(runtime, "direction")
    response = client.post(path, json=request, headers={"Authorization": ""})
    assert response.status_code == 401
    assert emotion(client) == before


@pytest.fixture
def clean_emotion_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("VIRTUAL_LIFE_EMOTION_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("axis", AXES)
def test_axis_toml_environment_and_empty_override_fallback(
    runtime, tmp_path, monkeypatch, clean_emotion_environment, axis
):
    path = tmp_path / "config.toml"
    field = f"{axis}_half_life_seconds"
    env_name = f"VIRTUAL_LIFE_EMOTION_{field.upper()}"
    path.write_text(f"[emotion]\nhalf_life_seconds=900\n{field}=600\n", encoding="utf8")
    assert getattr(Settings.load(path).emotion_policy, field) == 600
    monkeypatch.setenv(env_name, "1800")
    assert getattr(Settings.load(path).emotion_policy, field) == 1800
    monkeypatch.setenv(env_name, "  ")
    settings = Settings.load(path)
    assert getattr(settings.emotion_policy, field) is None
    client, app, _, _, _ = runtime
    app.state.emotion.policy = settings.emotion_policy
    assert emotion(client)["recovery_half_lives"] == {
        "valence": 900,
        "arousal": 900,
        "control": 900,
        "energy": 900,
        "social_battery": 900,
    }


@pytest.mark.parametrize("axis", AXES)
@pytest.mark.parametrize("source", ["environment", "toml"])
@pytest.mark.parametrize("value", ["private-malformed-value", "-12345", "987654321", "123.456"])
def test_invalid_axis_settings_are_rejected_without_echoing_values(
    tmp_path, monkeypatch, clean_emotion_environment, axis, source, value
):
    path = tmp_path / "config.toml"
    field = f"{axis}_half_life_seconds"
    path.write_text("[emotion]\n", encoding="utf8")
    if source == "environment":
        monkeypatch.setenv(f"VIRTUAL_LIFE_EMOTION_{field.upper()}", value)
    else:
        literal = f'"{value}"' if value == "private-malformed-value" else value
        path.write_text(f"[emotion]\n{field}={literal}\n", encoding="utf8")
    with pytest.raises(ValueError) as caught:
        Settings.load(path)
    assert value not in str(caught.value)
    assert field in str(caught.value).lower()
