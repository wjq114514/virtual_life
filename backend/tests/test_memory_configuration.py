"""Strict decay configuration and explicit override precedence."""

import pytest
from pydantic import ValidationError
from virtual_life.memory import MemoryDecayPolicy
from virtual_life.settings import Settings


@pytest.mark.parametrize(
    "payload",
    [
        {"routine_half_life_days": 0},
        {"stable_floor": 1.1},
        {"routine_floor": -0.1},
        {"enabled": "true"},
        {"permanent_candidate_limit": True},
        {"unknown": 1},
        {"semantic_salience_weight": float("nan")},
    ],
)
def test_decay_rejects_invalid_values(payload):
    with pytest.raises(ValidationError):
        MemoryDecayPolicy.model_validate(payload)


def test_file_json_and_individual_override_precedence(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("VIRTUAL_LIFE_"):
            monkeypatch.delenv(key)
    path = tmp_path / "settings.toml"
    path.write_text(
        "[memory.decay]\nroutine_half_life_days=90.0\nroutine_floor=0.2\n", encoding="utf8"
    )
    assert Settings.load(path).memory_policy.decay.routine_half_life_days == 90
    monkeypatch.setenv(
        "VIRTUAL_LIFE_MEMORY_DECAY", '{"routine_half_life_days":180.0,"routine_floor":0.4}'
    )
    monkeypatch.setenv("VIRTUAL_LIFE_MEMORY_DECAY_ROUTINE_FLOOR", "0.6")
    policy = Settings.load(path).memory_policy.decay
    assert policy.routine_half_life_days == 180 and policy.routine_floor == 0.6
