"""Immutable screen-vision configuration shared by settings, service and worker."""

from dataclasses import dataclass, field

_PROFILE_ADVICE = {
    "economy": (2000, 5000, 30000, 1024, 120),
    "balanced": (1000, 2500, 15000, 1536, 60),
    "detail": (500, 1000, 8000, 2048, 30),
}


@dataclass(frozen=True, slots=True)
class ScreenVisionConfig:
    enabled: bool = False
    base_url: str = ""
    model_name: str = ""
    api_key: str = ""
    timeout_seconds: float = 30.0
    storage_path: str = ".data/screen-vision"
    session_ttl_seconds: int = 1800
    frame_ttl_seconds: int = 90
    max_image_bytes: int = 8_388_608
    max_image_pixels: int = 12_000_000
    max_image_side: int = 8192
    global_concurrency: int = 2
    max_pending_global: int = 64
    max_pending_per_stream: int = 2
    max_uploads_per_minute_per_node: int = 30
    max_frames_per_minute_per_node: int = 30
    rate_burst: int = 5
    max_pending_manual_per_node: int = 1
    analysis_cache_ttl_seconds: int = 60
    # two_stage only: briefly wait for an in-flight analysis before dialogue
    # dispatch. 0 keeps the legacy no-wait behavior; waiting stops as soon as
    # the latest observation is analyzed, so it never exceeds this budget.
    evidence_wait_ms: int = 0
    comparison_window_frames: int = 20
    upload_retention_seconds: int = 300
    raw_retention_seconds: int = 600
    result_retention_seconds: int = 86400
    receipt_retention_seconds: int = 604800
    max_storage_bytes: int = 536_870_912
    output_tokens: int = 320
    max_model_images: int = 4
    daily_model_call_limit: int = 0
    daily_vision_token_limit: int = 0
    worker_poll_seconds: float = 0.5
    cleanup_interval_seconds: float = 30.0
    json_mode: bool = True
    extra_body: dict = field(default_factory=dict)
    enrichment: bool = True
    injection_gate: str = "deterministic"
    injection_base_url: str = ""
    injection_api_key: str = ""
    injection_model: str = ""
    injection_timeout_seconds: float = 5.0
    injection_threshold: float = 0.5
    max_context_windows: int = 12
    max_context_chars: int = 1200
    # two_stage: durable worker writes ScreenAnalysis, text dialogue injects evidence.
    # single_pass: no worker; the latest fresh frame is attached to the dialogue request.
    mode: str = "two_stage"

    @property
    def provider_configured(self) -> bool:
        return bool(self.base_url.strip() and self.model_name.strip())

    @property
    def single_pass(self) -> bool:
        return self.mode == "single_pass"

    def limits(self) -> dict:
        return {
            "max_image_bytes": self.max_image_bytes,
            "max_image_pixels": self.max_image_pixels,
            "max_image_side": self.max_image_side,
            "max_pending_per_stream": self.max_pending_per_stream,
            "max_pending_global": self.max_pending_global,
            "max_uploads_per_minute_per_node": self.max_uploads_per_minute_per_node,
            "max_frames_per_minute_per_node": self.max_frames_per_minute_per_node,
            "max_model_images": self.max_model_images,
            "output_tokens": self.output_tokens,
        }

    def advice(self, profile: str) -> dict:
        sample, min_upload, keyframe, long_side, refresh = _PROFILE_ADVICE.get(
            profile, _PROFILE_ADVICE["balanced"]
        )
        return {
            "sample_interval_ms": sample,
            "min_upload_interval_ms": min_upload,
            "max_keyframe_interval_ms": keyframe,
            "suggested_long_side": long_side,
            "model_refresh_seconds": refresh,
        }
