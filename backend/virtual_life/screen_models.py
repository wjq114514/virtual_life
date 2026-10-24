"""Validated wire contracts for the server-side screen vision session.

These models are transport-only. Persistence, image processing and provider
calls live in their own modules so an importer never performs work. All JSON is
``snake_case``; unknown fields and non-finite numbers are rejected.
"""

import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import Identifier, RequestModel

SCREEN_SCHEMA_VERSION = "1.0"
SCREEN_PROTOCOL_VERSION = "1.0"
MAX_CLIENT_FEATURES_BYTES = 64 * 1024
MAX_FRAME_METADATA_BYTES = 96 * 1024
MAX_OCR_BLOCKS = 256
MAX_UI_ELEMENTS = 256
MAX_REDACTED_REGIONS = 32
MAX_ANALYSIS_TEXT_BLOCKS = 256
MAX_ANALYSIS_ELEMENTS = 256
MAX_ANALYSIS_CHANGES = 64
MAX_ANALYSIS_TEXT_CHARS = 2048
MAX_ANALYSIS_SCENE_SUMMARY_CHARS = 800
MAX_ANALYSIS_BYTES = 256 * 1024

CaptureSource = Literal[
    "android_shizuku",
    "android_media_projection",
    "android_accessibility",
    "x11",
    "wayland_portal",
    "macos",
    "windows",
    "browser",
    "other",
]
RequestedProfile = Literal["economy", "balanced", "detail"]
SessionMode = Literal["one_shot", "continuous"]
FrameTrigger = Literal["manual", "first_frame", "changed", "refresh", "app_changed", "recovered"]
UnavailableReason = Literal[
    "locked", "permission_denied", "user_paused", "capture_failed", "unknown"
]
ImageMediaType = Literal["image/png", "image/jpeg"]
AnalysisActivity = Literal[
    "code", "docs", "chat", "meeting", "video", "game", "media", "terminal", "browser", "unknown"
]
AnalysisQuality = Literal["ok", "partial", "unreadable", "unavailable", "unknown"]
AnalysisPath = Literal["exact_reuse", "ocr_only", "vlm", "fused", "client_only"]
EvidenceSource = Literal["server_ocr", "server_vlm", "client_ocr", "client_ui", "host_metadata"]

HashAlgorithm = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


class _Bounded(RequestModel):
    pass


def _validate_bbox(value: list[float]) -> list[float]:
    if len(value) != 4:
        raise ValueError("bbox must contain exactly four values")
    x1, y1, x2, y2 = value
    for item in value:
        if not 0.0 <= item <= 1.0:
            raise ValueError("bbox values must be within [0, 1]")
    if not x1 < x2 or not y1 < y2:
        raise ValueError("bbox must satisfy x1<x2 and y1<y2")
    return value


class BoundingBox(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    bbox: list[float] = Field(default_factory=list)

    @field_validator("bbox")
    @classmethod
    def valid(cls, value: list[float]) -> list[float]:
        return _validate_bbox(value)


class ClientHash(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    algorithm: HashAlgorithm
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class OcrBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    text: Annotated[str, StringConstraints(max_length=MAX_ANALYSIS_TEXT_CHARS)]
    bbox: list[float]
    confidence: float | None = Field(default=None, ge=0, le=1)
    language: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: list[float]) -> list[float]:
        return _validate_bbox(value)


class UiElement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    element_id: Identifier
    parent_id: Identifier | None = None
    role: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    text: Annotated[str, StringConstraints(max_length=MAX_ANALYSIS_TEXT_CHARS)] = ""
    bbox: list[float]
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: list[float]) -> list[float]:
        return _validate_bbox(value)


class ClientFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    schema_version: str = SCREEN_SCHEMA_VERSION
    extractor: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
    ]
    extractor_version: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None
    ) = None
    baseline_frame_id: Identifier | None = None
    hashes: list[ClientHash] = Field(default_factory=list, max_length=16)
    change_score: float | None = Field(default=None, ge=0, le=1)
    filtered_count_since_previous: int | None = Field(default=None, ge=0, le=10_000_000)
    ocr_blocks: list[OcrBlock] = Field(default_factory=list, max_length=MAX_OCR_BLOCKS)
    ui_elements: list[UiElement] = Field(default_factory=list, max_length=MAX_UI_ELEMENTS)

    @model_validator(mode="after")
    def serialized_bounds(self) -> "ClientFeatures":
        try:
            encoded = json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("client_features must contain finite JSON values") from exc
        if len(encoded.encode("utf-8")) > MAX_CLIENT_FEATURES_BYTES:
            raise ValueError(f"client_features must not exceed {MAX_CLIENT_FEATURES_BYTES} bytes")
        return self


class HostMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    app_id: Identifier | None = None
    window_id: Identifier | None = None
    app_class: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None
    locked: bool | None = None
    user_present: bool | None = None
    meeting: bool | None = None
    screen_sharing: bool | None = None

    @model_validator(mode="after")
    def serialized_bounds(self) -> "HostMetadata":
        encoded = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > 2048:
            raise ValueError("host_metadata must not exceed 2048 bytes")
        return self


class RedactedRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    bbox: list[float]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: list[float]) -> list[float]:
        return _validate_bbox(value)


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["image"]
    upload_id: Identifier
    capture_width: int = Field(ge=1, le=100_000)
    capture_height: int = Field(ge=1, le=100_000)
    rotation_degrees: Literal[0, 90, 180, 270] = 0


class ReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["reference"]
    reference_frame_id: Identifier
    reason: Literal["client_filtered"] = "client_filtered"


class UnavailableInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["unavailable"]
    reason: UnavailableReason


FrameInput = Annotated[ImageInput | ReferenceInput | UnavailableInput, Field(discriminator="kind")]


class ScreenSessionCreate(RequestModel):
    command_id: Identifier
    schema_version: str = SCREEN_SCHEMA_VERSION
    companion_id: Identifier
    subject_node_id: Identifier
    display_id: Identifier = "default"
    mode: SessionMode
    user_initiated: bool
    capture_source: CaptureSource = "other"
    requested_profile: RequestedProfile = "balanced"

    @model_validator(mode="after")
    def user_action_required(self) -> "ScreenSessionCreate":
        if not self.user_initiated:
            raise ValueError("Screen sessions require user_initiated=true")
        return self


class ScreenCommand(RequestModel):
    command_id: Identifier


class ScreenUploadRegister(RequestModel):
    command_id: Identifier
    session_id: Identifier
    generation: int = Field(ge=1, le=2**63 - 1)
    media_type: ImageMediaType
    byte_length: int = Field(ge=1, le=2**63 - 1)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    width: int = Field(ge=1, le=100_000)
    height: int = Field(ge=1, le=100_000)


class ScreenFrameSubmit(RequestModel):
    command_id: Identifier
    schema_version: str = SCREEN_SCHEMA_VERSION
    session_id: Identifier
    generation: int = Field(ge=1, le=2**63 - 1)
    sequence: int = Field(ge=0, le=2**63 - 1)
    sampled_at: str
    ttl_seconds: int = Field(default=90, ge=5, le=300)
    trigger: FrameTrigger = "changed"
    input: FrameInput
    client_features: ClientFeatures | None = None
    host_metadata: HostMetadata | None = None
    redacted_regions: list[RedactedRegion] = Field(
        default_factory=list, max_length=MAX_REDACTED_REGIONS
    )

    @field_validator("sampled_at")
    @classmethod
    def require_timezone(cls, value: str) -> str:
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("sampled_at must be an RFC3339 timestamp") from None
        if parsed.tzinfo is None:
            raise ValueError("sampled_at must include a timezone offset")
        return value


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class ScreenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScreenLimits(ScreenResponse):
    max_image_bytes: int
    max_image_pixels: int
    max_image_side: int
    max_pending_per_stream: int
    max_pending_global: int
    max_uploads_per_minute_per_node: int
    max_frames_per_minute_per_node: int
    max_model_images: int
    output_tokens: int


class ScreenAdvice(ScreenResponse):
    sample_interval_ms: int
    min_upload_interval_ms: int
    max_keyframe_interval_ms: int
    suggested_long_side: int
    model_refresh_seconds: int


class ScreenSessionReceipt(ScreenResponse):
    session_id: str
    generation: int
    companion_id: str
    observer_node_id: str
    subject_node_id: str
    display_id: str
    mode: SessionMode
    status: str
    effective_profile: RequestedProfile
    capture_source: CaptureSource
    expires_at: str
    server_time: str
    user_initiated: bool
    limits: ScreenLimits
    advice: ScreenAdvice


class ScreenSessionSnapshot(ScreenSessionReceipt):
    purge_status: Literal["none", "pending", "completed", "failed"] = "none"
    latest_frame_id: str | None = None
    latest_analysis_id: str | None = None
    last_sequence: int | None = None


class ScreenUploadReceipt(ScreenResponse):
    upload_id: str
    session_id: str
    generation: int
    status: str
    media_type: ImageMediaType
    byte_length: int
    sha256: str
    width: int
    height: int
    upload_expires_at: str
    upload_path: str
    limits: ScreenLimits


class ScreenFrameReceipt(ScreenResponse):
    frame_id: str
    session_id: str
    generation: int
    sequence: int
    accepted_at: str
    accepted_status: str
    status_path: str


class ScreenFrameSnapshot(ScreenResponse):
    frame_id: str
    session_id: str
    generation: int
    sequence: int
    input_kind: str
    accepted_at: str
    status: str
    analysis_id: str | None = None
    reused_from_frame_id: str | None = None
    processing_stage: str | None = None
    error_code: str | None = None
    retryable: bool = False
    status_path: str


class ScreenEvidence(ScreenResponse):
    source: EvidenceSource
    frame_id: str
    bbox: list[float] | None = None
    extractor: str | None = None
    extractor_version: str | None = None
    score: float | None = None
    unlocalized: bool = False


class ScreenTextBlock(ScreenResponse):
    text: str
    bbox: list[float] | None = None
    confidence: float | None = None
    language: str | None = None
    evidence_refs: list[int] = Field(default_factory=list)


class ScreenElement(ScreenResponse):
    element_id: str
    parent_id: str | None = None
    role: str
    text: str = ""
    bbox: list[float] | None = None
    confidence: float | None = None
    evidence_refs: list[int] = Field(default_factory=list)


class ScreenChange(ScreenResponse):
    kind: str
    description: str
    bbox: list[float] | None = None
    evidence_refs: list[int] = Field(default_factory=list)


class ScreenAnalysisUsage(ScreenResponse):
    model_calls: int = 0
    image_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_unknown: bool = False
    queue_ms: int | None = None
    decode_ms: int | None = None
    dedup_ms: int | None = None
    ocr_ms: int | None = None
    model_ms: int | None = None
    total_ms: int | None = None


class ScreenAnalysis(ScreenResponse):
    analysis_id: str
    schema_version: str
    frame_id: str
    source_frame_ids: list[str]
    companion_id: str
    node_id: str
    session_id: str
    generation: int
    sequence: int
    sampled_at: str
    completed_at: str
    expires_at: str
    status: str
    quality: AnalysisQuality
    limitations: list[str] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    scene_summary: str = ""
    activity: AnalysisActivity = "unknown"
    activity_score: float | None = None
    text_blocks: list[ScreenTextBlock] = Field(default_factory=list)
    elements: list[ScreenElement] = Field(default_factory=list)
    changes: list[ScreenChange] = Field(default_factory=list)
    evidence: list[ScreenEvidence] = Field(default_factory=list)
    analysis_path: AnalysisPath
    preprocessing_version: str
    algorithm_version: str
    prompt_version: str
    model_identity: dict = Field(default_factory=dict)
    usage: ScreenAnalysisUsage = Field(default_factory=ScreenAnalysisUsage)


class ScreenState(ScreenResponse):
    companion_id: str
    observer_node_id: str
    display_id: str
    observable: bool
    stale: bool
    state: str
    latest_observation_frame_id: str | None = None
    latest_analysis_frame_id: str | None = None
    latest_analysis_id: str | None = None
    analysis_path: AnalysisPath | None = None
    pending_newer_frame: bool = False
    evidence_sampled_at: str | None = None
    evidence_expires_at: str | None = None
    expires_at: str | None = None
    barrier_sequence: int | None = None
    updated_at: str


class ScreenPurgeReceipt(ScreenResponse):
    session_id: str
    command_id: str
    status: str
    requested_at: str


def dump_bounded_analysis(payload: dict) -> None:
    """Reject analysis payloads that exceed the serialized contract size."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Analysis result must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_ANALYSIS_BYTES:
        raise ValueError(f"Analysis result must not exceed {MAX_ANALYSIS_BYTES} bytes")


def validate_model(model: type[BaseModel], payload: object) -> BaseModel:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(item) for item in error["loc"]) or "payload"
        raise ValueError(f"{location}: {error['msg']}") from exc
