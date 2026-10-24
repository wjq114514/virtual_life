"""Strict wire models for the server-side voice contract.

All JSON uses ``snake_case`` and rejects unknown fields. These models describe
the transport only; persistence and orchestration live in ``voice_service.py``.
"""

import re
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

from .models import Identifier

VoiceText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
VoiceMode = Literal["continuous", "push_to_talk", "text"]
EndReason = Literal["adaptive_endpoint", "manual", "push_to_talk"]
MAX_VERSION = 2**63 - 1
_RFC3339 = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def _parse_wire_datetime(value: object) -> datetime:
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise ValueError("must be an RFC 3339 date-time with a timezone")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("must be a valid RFC 3339 date-time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("must include a timezone offset")
    return parsed


def _wire_datetime(value: object) -> str:
    _parse_wire_datetime(value)
    return value


VoiceDateTime = Annotated[
    str,
    BeforeValidator(_wire_datetime),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class VoiceAsrConfig(_Model):
    engine: Identifier
    model_id: Identifier
    language: Identifier = "zh"


class VoiceOutputConfig(_Model):
    audio_enabled: bool = True
    voice_id: Identifier = "default"


class VoiceSessionLimits(_Model):
    max_input_chars: int = Field(ge=1, le=65536)
    max_input_age_seconds: float = Field(strict=True, gt=0, allow_inf_nan=False)
    max_turns_per_session: int = Field(ge=1, le=1024)
    max_segment_chars: int = Field(ge=1, le=4096)
    max_reply_chars: int = Field(ge=1, le=4096)


class VoiceSessionCreate(_Model):
    command_id: Identifier
    companion_id: Identifier
    epoch: int = Field(strict=True, ge=0, le=MAX_VERSION)
    expected_revision: int = Field(strict=True, ge=0, le=MAX_VERSION)
    presentation_id: Identifier
    user_initiated: Literal[True] = True
    mode: VoiceMode = "continuous"
    asr: VoiceAsrConfig | None = None
    output: VoiceOutputConfig = Field(default_factory=VoiceOutputConfig)
    replace_session_id: Identifier | None = None

    @model_validator(mode="after")
    def asr_required_unless_text(self):
        if self.mode == "text":
            if self.asr is not None:
                raise ValueError("Text voice sessions must omit the asr configuration")
        elif self.asr is None:
            raise ValueError("ASR voice sessions require an asr configuration")
        return self


class VoiceSessionSnapshot(_Model):
    session_id: Identifier
    command_id: Identifier
    companion_id: Identifier
    node_id: Identifier
    presentation_id: Identifier
    mode: VoiceMode
    epoch: int = Field(ge=0, le=MAX_VERSION)
    session_generation: int = Field(ge=0, le=MAX_VERSION)
    output_generation: int = Field(ge=0, le=MAX_VERSION)
    status: Literal["active", "stopped", "expired", "superseded"]
    audio_enabled: bool
    effective_voice: str
    asr: VoiceAsrConfig | None = None
    created_at: VoiceDateTime
    last_heartbeat_at: VoiceDateTime
    expires_at: VoiceDateTime
    idle_deadline_at: VoiceDateTime
    absolute_expires_at: VoiceDateTime
    idle_timeout_seconds: float = Field(strict=True, gt=0, allow_inf_nan=False)
    heartbeat_interval_seconds: float = Field(strict=True, gt=0, allow_inf_nan=False)
    limits: VoiceSessionLimits


class VoiceTurnInputAsr(_Model):
    kind: Literal["asr"] = "asr"
    started_at: VoiceDateTime
    ended_at: VoiceDateTime
    audio_duration_ms: int = Field(strict=True, ge=0, le=45_000)
    asr_model_id: Identifier
    language: Identifier = "zh"
    discontinuity: bool = False


class VoiceTurnInputText(_Model):
    kind: Literal["text"] = "text"
    mode: Literal["manual"] = "manual"


class VoiceScreenReference(_Model):
    node_id: Identifier
    session_id: Identifier
    generation: int = Field(strict=True, ge=0, le=MAX_VERSION)
    display_id: Identifier = "default"
    frame_id: Identifier | None = None
    analysis_id: Identifier | None = None
    policy: Literal["latest_if_fresh", "request_fresh"] = "latest_if_fresh"


class VoiceTurnSubmit(_Model):
    command_id: Identifier
    turn_id: Identifier
    session_generation: int = Field(strict=True, ge=0, le=MAX_VERSION)
    expected_output_generation: int = Field(strict=True, ge=0, le=MAX_VERSION)
    epoch: int = Field(strict=True, ge=0, le=MAX_VERSION)
    expected_revision: int = Field(strict=True, ge=0, le=MAX_VERSION)
    text: VoiceText
    user_initiated: Literal[True] = True
    end_reason: EndReason
    input: Annotated[Union[VoiceTurnInputAsr, VoiceTurnInputText], Field(discriminator="kind")]
    continuation_of_turn_id: Identifier | None = None
    screen_reference: VoiceScreenReference | None = None

    @model_validator(mode="after")
    def strip_text(self):
        object.__setattr__(self, "text", self.text.strip())
        if not self.text:
            raise ValueError("Turn text must not be blank")
        if isinstance(self.input, VoiceTurnInputText) and self.end_reason != "manual":
            raise ValueError("Text input requires manual end_reason")
        return self


class VoiceTurnReceipt(_Model):
    turn_id: Identifier
    command_id: Identifier
    session_id: Identifier
    companion_id: Identifier
    session_generation: int = Field(ge=0, le=MAX_VERSION)
    output_generation: int = Field(ge=0, le=MAX_VERSION)
    status: Literal["accepted", "in_progress", "completed", "discarded", "interrupted", "failed"]
    sequence: int = Field(ge=0)
    audio_enabled: bool
    speech_available: bool
    assistant_message: str | None = None
    discard_reason: str | None = None
    created_at: VoiceDateTime


class VoiceUsage(_Model):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class VoiceTurnSnapshot(_Model):
    turn_id: Identifier
    command_id: Identifier
    session_id: Identifier
    companion_id: Identifier
    status: Literal["accepted", "in_progress", "completed", "discarded", "interrupted", "failed"]
    discard_reason: str | None = None
    output_generation: int = Field(ge=0, le=MAX_VERSION)
    assistant_message: str | None = None
    model: str | None = None
    usage: VoiceUsage | None = None
    speech_status: str
    segment_count: int = Field(ge=0)
    sequence: int = Field(ge=0)
    created_at: VoiceDateTime


class VoiceInterruptRequest(_Model):
    command_id: Identifier
    session_generation: int = Field(strict=True, ge=0, le=MAX_VERSION)
    target_turn_id: Identifier
    expected_output_generation: int = Field(strict=True, ge=0, le=MAX_VERSION)
    reason: Literal["user_speech", "stop_button", "text_input", "session_stop"]


class VoiceInterruptReceipt(_Model):
    session_id: Identifier
    target_turn_id: Identifier
    status: Literal["interrupted", "already_terminal", "stale"]
    output_generation: int = Field(ge=0, le=MAX_VERSION)
    upstream_state: Literal["pending", "draining", "closed", "completed", "unknown"]
    replayed: bool = False
    created_at: VoiceDateTime


class VoicePlaybackReport(_Model):
    command_id: Identifier
    turn_id: Identifier
    output_generation: int = Field(strict=True, ge=0, le=MAX_VERSION)
    segment_sequence: int = Field(strict=True, ge=0)
    played_samples: int = Field(strict=True, ge=0, le=3_600_000_000)
    status: Literal["started", "progress", "completed", "interrupted", "failed"]


class VoiceStopRequest(_Model):
    command_id: Identifier


class VoiceHeartbeat(_Model):
    command_id: Identifier


class VoicePlaybackReceipt(_Model):
    recorded: bool
    valid: bool
    output_generation: int = Field(ge=0, le=MAX_VERSION)


class VoiceSegmentInfo(_Model):
    segment_sequence: int = Field(ge=0)
    text: str
    spoken_text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    audio_path: str
    media_type: str
    sample_rate: int = Field(ge=0)
    channels: int = Field(ge=1)
    sample_count: int = Field(ge=0)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    status: Literal["ready", "cancelled", "expired"]
    expires_at: VoiceDateTime
