"""Strict request and response schema checks for the voice wire contract."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from virtual_life.voice_models import (
    VoiceSessionCreate,
    VoiceSessionLimits,
    VoiceTurnInputAsr,
    VoiceTurnSubmit,
)


def _session_payload(mode="text", asr=None):
    return {
        "command_id": "start-voice",
        "companion_id": "sui",
        "epoch": 0,
        "expected_revision": 0,
        "presentation_id": "desktop-presentation",
        "mode": mode,
        "asr": asr,
    }


def _asr_input(**changes):
    return {
        "kind": "asr",
        "started_at": "2026-10-18T10:00:00Z",
        "ended_at": "2026-10-18T10:00:01+00:00",
        "audio_duration_ms": 1000,
        "asr_model_id": "streaming-zh",
        "language": "zh",
        "discontinuity": False,
        **changes,
    }


def _turn_payload(input_value, end_reason="adaptive_endpoint"):
    return {
        "command_id": "voice-turn",
        "turn_id": "turn-1",
        "session_generation": 1,
        "expected_output_generation": 1,
        "epoch": 0,
        "expected_revision": 0,
        "text": "你好",
        "user_initiated": True,
        "end_reason": end_reason,
        "input": input_value,
    }


@pytest.mark.parametrize(
    ("mode", "asr", "valid"),
    (
        ("text", None, True),
        ("text", {"engine": "sherpa", "model_id": "streaming-zh"}, False),
        ("continuous", None, False),
        ("push_to_talk", None, False),
        ("continuous", {"engine": "sherpa", "model_id": "streaming-zh"}, True),
        ("push_to_talk", {"engine": "sherpa", "model_id": "streaming-zh"}, True),
    ),
)
def test_session_mode_requires_matching_asr_configuration(mode, asr, valid):
    if valid:
        VoiceSessionCreate.model_validate(_session_payload(mode, asr))
    else:
        with pytest.raises(ValidationError):
            VoiceSessionCreate.model_validate(_session_payload(mode, asr))


def test_text_turn_requires_manual_end_reason():
    VoiceTurnSubmit.model_validate(_turn_payload({"kind": "text"}, "manual"))
    with pytest.raises(ValidationError):
        VoiceTurnSubmit.model_validate(_turn_payload({"kind": "text"}, "adaptive_endpoint"))
    with pytest.raises(ValidationError):
        VoiceTurnSubmit.model_validate(
            _turn_payload({"kind": "text", "mode": "continuous"}, "manual")
        )


def test_asr_duration_is_capped_at_45_seconds_and_rejects_bool():
    VoiceTurnInputAsr.model_validate(_asr_input(audio_duration_ms=45_000))
    with pytest.raises(ValidationError):
        VoiceTurnInputAsr.model_validate(_asr_input(audio_duration_ms=45_001))
    with pytest.raises(ValidationError):
        VoiceTurnInputAsr.model_validate(_asr_input(audio_duration_ms=True))


@pytest.mark.parametrize(
    "value",
    (
        "2026-10-18T10:00:00",
        "2026-02-30T10:00:00Z",
        "2026-10-18T10:00:00+25:00",
        datetime(2026, 10, 18, tzinfo=UTC),
        None,
    ),
)
@pytest.mark.parametrize("field", ("started_at", "ended_at"))
def test_asr_timestamps_require_valid_timezone_aware_wire_strings(field, value):
    payload = _asr_input()
    payload[field] = value
    with pytest.raises(ValidationError):
        VoiceTurnInputAsr.model_validate(payload)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), -float("inf"), True))
def test_session_limits_reject_non_finite_and_boolean_seconds(value):
    with pytest.raises(ValidationError):
        VoiceSessionLimits(
            max_input_chars=4096,
            max_input_age_seconds=value,
            max_turns_per_session=64,
            max_segment_chars=80,
            max_reply_chars=600,
        )


def test_strict_models_reject_bool_for_integer_fields():
    payload = _session_payload()
    payload["expected_revision"] = True
    with pytest.raises(ValidationError):
        VoiceSessionCreate.model_validate(payload)


def test_voice_datetime_fields_publish_openapi_date_time_format():
    asr_schema = VoiceTurnInputAsr.model_json_schema()
    assert asr_schema["properties"]["started_at"]["format"] == "date-time"
    assert asr_schema["properties"]["ended_at"]["format"] == "date-time"


def test_turn_input_model_is_not_mutated_when_validating_bad_timestamps():
    payload = _asr_input()
    invalid = deepcopy(payload)
    invalid["ended_at"] = "not-a-date"
    with pytest.raises(ValidationError):
        VoiceTurnInputAsr.model_validate(invalid)
    assert payload["ended_at"] == "2026-10-18T10:00:01+00:00"
