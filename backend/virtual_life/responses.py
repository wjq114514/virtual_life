"""Public response contracts kept independent of persistence implementation."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class NodeDevice(ResponseModel):
    device_id: str | None = None
    mac_addresses: list[str] = Field(default_factory=list)
    ip_addresses: list[str] = Field(default_factory=list)


class NodeWebAccess(ResponseModel):
    access_id: str
    kind: Literal["web"] = "web"
    last_seen_at: datetime
    expires_at: datetime


class NodeSnapshot(ResponseModel):
    node_id: str
    label: str
    kind: Literal["desktop", "android", "web", "other"]
    capabilities: list[str]
    online: bool
    context: dict[str, Any]
    created_at: datetime
    last_seen_at: datetime
    device: NodeDevice | None = None
    accesses: list[NodeWebAccess] = Field(default_factory=list)


class WebAccessSnapshot(ResponseModel):
    node: NodeSnapshot
    access_id: str
    expires_at: datetime
    resolution: Literal["bound", "browser_id", "lan_ip", "created"]


class GraphEdge(ResponseModel):
    source_node_id: str
    target_node_id: str
    available: bool
    reason: str


class GraphSnapshot(ResponseModel):
    nodes: list[NodeSnapshot]
    edges: list[GraphEdge]


class CompanionSnapshot(ResponseModel):
    companion_id: str
    name: str
    revision: int
    active_node_id: str | None
    epoch: int
    lease_expires_at: datetime | None
    activity: dict[str, Any]
    interaction_mode: Literal["quiet", "chat", "focus", "away"]
    context: dict[str, Any]
    memory_refs: list[str]
    created_at: datetime
    updated_at: datetime


class TransferSnapshot(ResponseModel):
    transfer_id: str
    companion_id: str
    source_node_id: str | None
    target_node_id: str
    status: Literal["prepared", "committed", "aborted", "expired"]
    source_released: bool
    target_ready: bool
    base_revision: int
    snapshot: CompanionSnapshot
    reason: str
    created_at: datetime
    expires_at: datetime
    committed_revision: int | None
    target_presentation_id: str | None = None


class DialogueHistoryItem(ResponseModel):
    event_id: str
    sequence: int
    command_id: str
    node_id: str | None
    occurred_at: datetime
    user_message: str
    assistant_message: str


class DialogueHistoryPage(ResponseModel):
    companion_id: str
    items: list[DialogueHistoryItem]
    next_before_sequence: int | None
    has_more: bool


class OutputLease(ResponseModel):
    companion_id: str
    node_id: str
    epoch: int
    expires_at: datetime
    ttl_seconds: float
    server_time: datetime


class EventEnvelope(ResponseModel):
    event_id: str
    sequence: int
    protocol_version: str
    type: str
    companion_id: str | None
    node_id: str | None
    recipient_node_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


class EventPage(ResponseModel):
    events: list[EventEnvelope]
    next_sequence: int


class MemoryRecord(ResponseModel):
    memory_id: str
    companion_id: str
    kind: Literal["episode", "fact", "preference", "relationship"]
    layer: Literal["episodic", "semantic"]
    revision: int = 1
    occurred_at: datetime | None = None
    episode_key: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes_id: str | None = None
    content: str
    source_event_ids: list[str]
    semantic_key: str | None
    tags: list[str]
    importance: float
    confidence: float
    confirmed: bool
    pinned: bool
    retention_policy: Literal[
        "auto", "permanent", "stable", "significant_episode", "routine_episode"
    ] = "auto"
    effective_retention_policy: (
        Literal["permanent", "stable", "significant_episode", "routine_episode"] | None
    ) = None
    decay_factor: float | None = Field(default=None, ge=0, le=1)
    effective_importance: float | None = Field(default=None, ge=0, le=1)
    active: bool
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def historical_layer(cls, value):
        # Old command receipts deliberately retain their original snapshots.
        if isinstance(value, dict) and "layer" not in value:
            return {**value, "layer": "episodic" if value["kind"] == "episode" else "semantic"}
        return value


class MemorySourceVersion(ResponseModel):
    memory_id: str
    revision: int = Field(ge=1, le=2**63 - 1)


class GraphEntityVersion(ResponseModel):
    entity_id: str
    revision: int = Field(ge=1, le=2**63 - 1)


class MemorySummary(ResponseModel):
    summary_id: str
    content: str
    memory_versions: list[MemorySourceVersion]
    source_event_ids: list[str]
    created_at: datetime
    job_id: str
    kind: Literal["summary"]


class MemoryRelation(ResponseModel):
    relation_id: str
    subject: str
    predicate: str
    object: str
    confidence: float = Field(ge=0, le=1)
    memory_versions: list[MemorySourceVersion]
    source_event_ids: list[str]
    created_at: datetime
    job_id: str
    kind: Literal["relation"]
    edge_id: str | None = None
    subject_entity_id: str | None = None
    object_entity_id: str | None = None
    evidence_count: int = 1
    evidence_truncated: bool = False
    relation_ids: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    entity_versions: list[GraphEntityVersion] = Field(default_factory=list)


class KnowledgeGraphEntity(ResponseModel):
    entity_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    revision: int = 1
    updated_at: datetime | None = None
    source_event_ids: list[str] = Field(default_factory=list)


class KnowledgeGraphSnapshot(ResponseModel):
    companion_id: str
    entities: list[KnowledgeGraphEntity]
    relations: list[MemoryRelation]
    limit: int
    hops: int = 1


class ModelTokenUsage(ResponseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class MemoryRetrievalDiagnostics(ResponseModel):
    semantic_cache_hit: bool = False
    semantic_candidates: int = 0
    vector_space: str | None = None


class RerankDiagnostics(ResponseModel):
    enabled: bool = False
    applied: bool = False
    candidates: int = 0
    latency_ms: float = 0.0
    error_code: str | None = None


class RecallResult(ResponseModel):
    items: list[MemoryRecord]
    context_text: str
    estimated_tokens: int
    token_budget: int
    truncated: bool
    retrieval_strategy: str
    model_calls: int
    summaries: list[MemorySummary] = Field(default_factory=list)
    relations: list[MemoryRelation] = Field(default_factory=list)
    retrieval: MemoryRetrievalDiagnostics = Field(default_factory=MemoryRetrievalDiagnostics)
    rerank: RerankDiagnostics | None = None
    embedding_usage: ModelTokenUsage | None = None


class MemoryLogEntry(ResponseModel):
    sequence: int
    log_id: str
    companion_id: str
    operation: str
    status: str
    command_id: str | None
    job_id: str | None
    created_at: datetime
    duration_ms: float | None
    details: dict[str, Any]


class MemoryLogPage(ResponseModel):
    items: list[MemoryLogEntry]
    next_sequence: int
    has_more: bool
