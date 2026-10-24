"""Typed vision provider contract, independent of the text chat adapter.

Importing this module performs no network or model work. A ``ModelRequest`` only
carries text, so images cannot be smuggled through ``context_text``; callers pass
a :class:`VisionRequest` instead.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class VisionImage:
    """One encoded image already validated by the server image pipeline."""

    data: bytes
    media_type: str
    width: int
    height: int
    detail: Literal["low", "high", "auto"] = "auto"


@dataclass(frozen=True, slots=True)
class VisionRequest:
    task_id: str
    companion_id: str
    purpose: str
    model: str
    instructions: str
    images: Sequence[VisionImage]
    input_token_limit: int
    output_token_limit: int
    analysis_schema_version: str = "1.0"


@dataclass(frozen=True, slots=True)
class VisionUsage:
    input_tokens: int
    output_tokens: int
    image_count: int


@dataclass(frozen=True, slots=True)
class VisionResult:
    payload: Mapping[str, object]
    model: str
    usage: VisionUsage
    metadata: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class ScreenVisionProvider(Protocol):
    """A configured image analyzer; it never runs during import."""

    def estimate_input_tokens(self, request: VisionRequest) -> int: ...

    def complete(self, request: VisionRequest) -> VisionResult: ...
