"""Replaceable provider contracts; importing these contracts performs no model calls.

DialogueService reserves token quota before text generation and settles the
provider's actual usage afterwards. Future background jobs use the same ledger.
Memory sources and derived summaries are deliberately distinct values here.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

MAX_TOOL_CALLS = 2
MAX_TOOL_DEFINITIONS = 16
MAX_TOOL_ARGUMENT_BYTES = 16_384
MAX_TOOL_CONTENT_BYTES = 16_384
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@runtime_checkable
class TokenEstimator(Protocol):
    """A deterministic, nonnegative estimate of the complete supplied text."""

    @property
    def name(self) -> str: ...

    def estimate(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelImage:
    """One validated image attached to a chat request."""

    data: bytes
    media_type: str
    detail: Literal["low", "high", "auto"] = "high"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    arguments: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    task_id: str
    companion_id: str
    purpose: str
    model: str
    context_text: str
    input_token_limit: int
    output_token_limit: int
    tools: Sequence[ToolDefinition] = ()
    tool_results: Sequence[ToolResult] = ()
    images: Sequence[ModelImage] = ()


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str
    model: str
    usage: ModelUsage
    metadata: Mapping[str, object] = field(default_factory=dict)
    tool_calls: Sequence[ToolCall] = ()


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    task_id: str
    companion_id: str
    model: str
    texts: Sequence[str]
    input_token_limit: int


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: Sequence[Sequence[float]]
    model: str
    usage: ModelUsage


@dataclass(frozen=True, slots=True)
class RerankRequest:
    task_id: str
    companion_id: str
    model: str
    query: str
    texts: Sequence[str]


@dataclass(frozen=True, slots=True)
class RerankResult:
    scores: Sequence[float]
    model: str


class EmbeddingProvider(Protocol):
    def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...


class RerankProvider(Protocol):
    def rerank(self, request: RerankRequest) -> RerankResult: ...


class ModelProvider(Protocol):
    def complete(self, request: ModelRequest) -> ModelResult: ...

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class PerceptionRequest:
    node_id: str
    modality: str
    data: Mapping[str, object]
    user_initiated: bool


@dataclass(frozen=True, slots=True)
class PerceptionResult:
    description: str
    observations: Sequence[Mapping[str, object]]
    metadata: Mapping[str, object] = field(default_factory=dict)


class PerceptionProvider(Protocol):
    def perceive(self, request: PerceptionRequest) -> PerceptionResult: ...


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    companion_id: str
    node_id: str
    text: str
    voice: str
    max_audio_seconds: float


@dataclass(frozen=True, slots=True)
class SpeechResult:
    audio: bytes
    media_type: str
    metadata: Mapping[str, object] = field(default_factory=dict)


class SpeechProvider(Protocol):
    def synthesize(self, request: SpeechRequest) -> SpeechResult: ...


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """An adapter's candidate; the caller must recheck scope, activity and budget."""

    memory_id: str
    companion_id: str
    relevance: float


class MemoryRetriever(Protocol):
    def retrieve(
        self, companion_id: str, query: str, limit: int
    ) -> Sequence[RetrievalCandidate]: ...


@dataclass(frozen=True, slots=True)
class ConsolidationRequest:
    task_id: str
    companion_id: str
    source_memory_ids: Sequence[str]
    source_text: str
    input_token_limit: int
    output_token_limit: int


@dataclass(frozen=True, slots=True)
class DerivedSummary:
    """A derived result with provenance, never a replacement for raw sources."""

    text: str
    source_memory_ids: Sequence[str]
    usage: ModelUsage
    model: str


class MemoryConsolidator(Protocol):
    def consolidate(self, request: ConsolidationRequest) -> DerivedSummary: ...


@dataclass(frozen=True, slots=True)
class ParticipationContext:
    interaction_mode: Literal["quiet", "chat", "focus", "away"]
    user_initiated: bool
    output_permitted: bool
    audio_requested: bool = False
    host_allows_audio: bool = False


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    respond: bool
    allow_audio: bool
    reason: str


class BehaviorPolicy(Protocol):
    def decide(self, context: ParticipationContext) -> ParticipationDecision: ...


class QuietParticipationPolicy:
    """A default participation boundary; hosts still enforce their output lease."""

    def decide(self, context: ParticipationContext) -> ParticipationDecision:
        if not context.output_permitted:
            return ParticipationDecision(False, False, "output_not_permitted")
        if context.interaction_mode == "away":
            return ParticipationDecision(False, False, "companion_away")
        if not context.user_initiated and context.interaction_mode != "chat":
            return ParticipationDecision(False, False, "waiting_for_user")
        return ParticipationDecision(
            True,
            context.audio_requested and context.host_allows_audio,
            "user_requested" if context.user_initiated else "chat_participation",
        )


@runtime_checkable
class StreamingModelProvider(Protocol):
    """Optional incremental generation with final billing evidence."""

    def stream(self, request: ModelRequest, on_delta: Callable[[str], None]) -> ModelResult: ...
