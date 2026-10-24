"""Validated ``[speech]`` and ``[voice]`` configuration sections."""

import math
from dataclasses import dataclass, field


def _positive(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite positive number")
    except OverflowError:
        raise ValueError(f"{name} must be a finite positive number") from None
    return float(value)


def _bounded_int(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    enabled: bool = False
    provider: str = "sherpa_onnx_kokoro"
    worker_python: str = ""
    model_dir: str = ".data/speech/models/kokoro"
    model_manifest: str = ""
    default_voice_id: str = "default"
    sample_rate: int = 24000
    global_concurrency: int = 1
    worker_threads: int = 2
    max_reply_chars: int = 600
    max_reply_audio_seconds: float = 60.0
    max_segment_chars: int = 80
    max_segment_audio_seconds: float = 10.0
    audio_ttl_seconds: int = 60
    max_pending_segments: int = 4
    max_buffered_playback_seconds: float = 8.0
    allow_remote_provider: bool = False
    remote_base_url: str = field(default="", repr=False)
    remote_api_key: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    enabled: bool = False
    session_heartbeat_seconds: float = 10.0
    session_ttl_seconds: float = 30.0
    idle_timeout_seconds: float = 120.0
    session_max_seconds: float = 1800.0
    max_input_chars: int = 4096
    max_input_age_seconds: float = 10.0
    max_draining_per_companion: int = 1
    max_turns_per_session: int = 64
    event_buffer_size: int = 256


def speech_config(values: object) -> SpeechConfig:
    if values is None or values == {}:
        return SpeechConfig()
    if isinstance(values, SpeechConfig):
        config = values
    elif isinstance(values, dict):
        allowed = set(SpeechConfig.__dataclass_fields__)
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration field: speech.{sorted(unknown)[0]}")
        try:
            config = SpeechConfig(**values)
        except TypeError as exc:
            raise ValueError(f"Invalid configuration for speech: {exc}") from None
    else:
        raise ValueError("Invalid configuration for speech: must be a table")
    for name in ("enabled", "allow_remote_provider"):
        if type(getattr(config, name)) is not bool:
            raise ValueError(f"speech.{name} must be a boolean")
    for name in ("provider", "worker_python", "model_dir", "model_manifest", "default_voice_id"):
        if not isinstance(getattr(config, name), str):
            raise ValueError(f"speech.{name} must be a string")
    if config.provider not in ("sherpa_onnx_kokoro", "none"):
        raise ValueError("speech.provider must be sherpa_onnx_kokoro or none")
    if config.enabled and not config.model_dir.strip():
        raise ValueError("speech.model_dir is required when speech is enabled")
    for name, (minimum, maximum) in {
        "sample_rate": (8000, 192000),
        "global_concurrency": (1, 8),
        "worker_threads": (1, 32),
        "max_reply_chars": (1, 4096),
        "max_segment_chars": (1, 4096),
        "audio_ttl_seconds": (1, 3600),
        "max_pending_segments": (1, 64),
    }.items():
        _bounded_int(getattr(config, name), f"speech.{name}", minimum, maximum)
    for name in (
        "max_reply_audio_seconds",
        "max_segment_audio_seconds",
        "max_buffered_playback_seconds",
    ):
        _positive(getattr(config, name), f"speech.{name}")
    if config.max_segment_chars > config.max_reply_chars:
        raise ValueError("speech.max_segment_chars must not exceed max_reply_chars")
    return config


def voice_config(values: object) -> VoiceConfig:
    if values is None or values == {}:
        return VoiceConfig()
    if isinstance(values, VoiceConfig):
        config = values
    elif isinstance(values, dict):
        allowed = set(VoiceConfig.__dataclass_fields__)
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration field: voice.{sorted(unknown)[0]}")
        try:
            config = VoiceConfig(**values)
        except TypeError as exc:
            raise ValueError(f"Invalid configuration for voice: {exc}") from None
    else:
        raise ValueError("Invalid configuration for voice: must be a table")
    if type(config.enabled) is not bool:
        raise ValueError("voice.enabled must be a boolean")
    for name in (
        "session_heartbeat_seconds",
        "session_ttl_seconds",
        "idle_timeout_seconds",
        "session_max_seconds",
        "max_input_age_seconds",
    ):
        _positive(getattr(config, name), f"voice.{name}")
    for name, (minimum, maximum) in {
        "max_input_chars": (1, 65536),
        "max_draining_per_companion": (0, 4),
        "max_turns_per_session": (1, 1024),
        "event_buffer_size": (16, 4096),
    }.items():
        _bounded_int(getattr(config, name), f"voice.{name}", minimum, maximum)
    if config.session_heartbeat_seconds >= config.session_ttl_seconds:
        raise ValueError("voice.session_heartbeat_seconds must be less than session_ttl_seconds")
    return config
