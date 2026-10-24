"""Strict parsing for voice and speech runtime configuration."""

import math

import pytest
from virtual_life.voice_config import speech_config, voice_config


@pytest.mark.parametrize(
    "field",
    (
        "session_heartbeat_seconds",
        "session_ttl_seconds",
        "idle_timeout_seconds",
        "session_max_seconds",
        "max_input_age_seconds",
    ),
)
@pytest.mark.parametrize("value", (True, math.nan, math.inf, -math.inf))
def test_voice_positive_numbers_reject_bool_and_non_finite_values(field, value):
    with pytest.raises(ValueError):
        voice_config({field: value})


@pytest.mark.parametrize(
    "field",
    (
        "max_reply_audio_seconds",
        "max_segment_audio_seconds",
        "max_buffered_playback_seconds",
    ),
)
@pytest.mark.parametrize("value", (True, math.nan, math.inf, -math.inf))
def test_speech_positive_numbers_reject_bool_and_non_finite_values(field, value):
    with pytest.raises(ValueError):
        speech_config({field: value})


@pytest.mark.parametrize(
    ("parser", "field"),
    (
        (voice_config, "max_input_chars"),
        (voice_config, "max_draining_per_companion"),
        (voice_config, "max_turns_per_session"),
        (voice_config, "event_buffer_size"),
        (speech_config, "sample_rate"),
        (speech_config, "global_concurrency"),
        (speech_config, "worker_threads"),
        (speech_config, "max_reply_chars"),
        (speech_config, "max_segment_chars"),
        (speech_config, "audio_ttl_seconds"),
        (speech_config, "max_pending_segments"),
    ),
)
def test_integer_configuration_rejects_bool(parser, field):
    with pytest.raises(ValueError):
        parser({field: True})


@pytest.mark.parametrize(
    ("parser", "field"),
    (
        (voice_config, "enabled"),
        (speech_config, "enabled"),
        (speech_config, "allow_remote_provider"),
    ),
)
def test_boolean_configuration_rejects_integer_values(parser, field):
    with pytest.raises(ValueError):
        parser({field: 1})


def test_voice_configuration_still_checks_related_limits():
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        voice_config({"session_heartbeat_seconds": 30, "session_ttl_seconds": 30})


def test_unknown_configuration_fields_are_rejected():
    with pytest.raises(ValueError, match="voice.unknown"):
        voice_config({"unknown": True})
    with pytest.raises(ValueError, match="speech.unknown"):
        speech_config({"unknown": True})
