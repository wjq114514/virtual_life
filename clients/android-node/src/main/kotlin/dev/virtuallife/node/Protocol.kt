package dev.virtuallife.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** v1.0 JSON is snake_case; timestamps remain their original UTC ISO 8601 strings. */
@OptIn(ExperimentalSerializationApi::class)
val NodeJson: Json = Json {
    ignoreUnknownKeys = true
    encodeDefaults = true
    explicitNulls = false
}

internal fun emptyObject() = JsonObject(emptyMap())
internal fun idleActivity() = JsonObject(mapOf("type" to JsonPrimitive("idle")))

@Serializable
enum class NodeKind {
    @SerialName("desktop") DESKTOP,
    @SerialName("android") ANDROID,
    @SerialName("web") WEB,
    @SerialName("other") OTHER,
}

@Serializable
enum class InteractionMode {
    @SerialName("quiet") QUIET,
    @SerialName("chat") CHAT,
    @SerialName("focus") FOCUS,
    @SerialName("away") AWAY,
}

@Serializable
enum class TransferStatus {
    @SerialName("prepared") PREPARED,
    @SerialName("committed") COMMITTED,
    @SerialName("aborted") ABORTED,
    @SerialName("expired") EXPIRED,
}

@Serializable
enum class AckPhase {
    @SerialName("ready") READY,
    @SerialName("released") RELEASED,
}

@Serializable
enum class TransferRole {
    @SerialName("source") SOURCE,
    @SerialName("target") TARGET,
}

/** Stable nodeId identifies a device, never a per-page Web access. */
@Serializable
data class NodeRegistration(
    @SerialName("node_id") val nodeId: String,
    val label: String,
    val kind: NodeKind = NodeKind.ANDROID,
    val capabilities: List<String>,
    val device: DeviceIdentity? = null,
)

@Serializable
data class NodeSnapshot(
    @SerialName("node_id") val nodeId: String,
    val label: String,
    val kind: NodeKind,
    val capabilities: List<String>,
    val online: Boolean,
    val context: JsonObject = emptyObject(),
    @SerialName("created_at") val createdAt: String,
    @SerialName("last_seen_at") val lastSeenAt: String,
    val device: NodeDevice? = null,
    val accesses: List<NodeWebAccess> = emptyList(),
)

@Serializable
data class DeviceIdentity(
    @SerialName("device_id") val deviceId: String? = null,
    @SerialName("mac_addresses") val macAddresses: List<String> = emptyList(),
)

@Serializable
data class NodeDevice(
    @SerialName("device_id") val deviceId: String? = null,
    @SerialName("mac_addresses") val macAddresses: List<String> = emptyList(),
    @SerialName("ip_addresses") val ipAddresses: List<String> = emptyList(),
)

@Serializable
data class NodeWebAccess(
    @SerialName("access_id") val accessId: String,
    val kind: String = "web",
    @SerialName("last_seen_at") val lastSeenAt: String,
    @SerialName("expires_at") val expiresAt: String,
)

/** Persist browserId; create a fresh accessId for each access and use the returned node.nodeId. */
@Serializable
data class WebAccessRequest(
    @SerialName("browser_id") val browserId: String,
    @SerialName("access_id") val accessId: String,
    val label: String = "本机设备",
    val kind: NodeKind = NodeKind.OTHER,
    @SerialName("bind_node_id") val bindNodeId: String? = null,
) {
    init {
        require(kind != NodeKind.WEB) { "kind describes the device, not its Web access" }
    }
}

@Serializable
data class WebAccessHeartbeat(val context: JsonObject = emptyObject())

@Serializable
data class WebAccessSnapshot(
    val node: NodeSnapshot,
    @SerialName("access_id") val accessId: String,
    @SerialName("expires_at") val expiresAt: String,
    val resolution: String,
)

@Serializable
data class NodeHeartbeat(val context: JsonObject = emptyObject())

@Serializable
data class GraphEdge(
    @SerialName("source_node_id") val sourceNodeId: String,
    @SerialName("target_node_id") val targetNodeId: String,
    val available: Boolean,
    val reason: String,
)

@Serializable
data class GraphSnapshot(val nodes: List<NodeSnapshot>, val edges: List<GraphEdge>)

@Serializable
data class CompanionCreate(
    @SerialName("companion_id") val companionId: String,
    val name: String,
)

@Serializable
data class CompanionSnapshot(
    @SerialName("companion_id") val companionId: String,
    val name: String,
    val revision: Long,
    @SerialName("active_node_id") val activeNodeId: String? = null,
    val epoch: Long,
    @SerialName("lease_expires_at") val leaseExpiresAt: String? = null,
    val activity: JsonObject = idleActivity(),
    @SerialName("interaction_mode") val interactionMode: InteractionMode = InteractionMode.QUIET,
    val context: JsonObject = emptyObject(),
    @SerialName("memory_refs") val memoryRefs: List<String> = emptyList(),
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
)

@Serializable
data class ContextUpdate(
    @SerialName("command_id") val commandId: String,
    @SerialName("node_id") val nodeId: String,
    val epoch: Long,
    @SerialName("expected_revision") val expectedRevision: Long,
    val activity: JsonObject? = null,
    @SerialName("interaction_mode") val interactionMode: InteractionMode? = null,
    val context: JsonObject? = null,
    @SerialName("memory_refs") val memoryRefs: List<String>? = null,
)

@Serializable
data class TransferPrepare(
    @SerialName("command_id") val commandId: String,
    @SerialName("target_node_id") val targetNodeId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val reason: String = "manual",
)

@Serializable
data class TransferSnapshot(
    @SerialName("transfer_id") val transferId: String,
    @SerialName("companion_id") val companionId: String,
    @SerialName("source_node_id") val sourceNodeId: String? = null,
    @SerialName("target_node_id") val targetNodeId: String,
    val status: TransferStatus,
    @SerialName("source_released") val sourceReleased: Boolean,
    @SerialName("target_ready") val targetReady: Boolean,
    @SerialName("base_revision") val baseRevision: Long,
    val snapshot: CompanionSnapshot,
    val reason: String,
    @SerialName("created_at") val createdAt: String,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("committed_revision") val committedRevision: Long? = null,
    @SerialName("target_presentation_id") val targetPresentationId: String? = null,
)

@Serializable
data class TransferAck(
    @SerialName("node_id") val nodeId: String,
    val phase: AckPhase,
    // Native targets may bind a live presentation on ready; sources omit it.
    @SerialName("presentation_id") val presentationId: String? = null,
)

@Serializable
data class LeaseRenew(@SerialName("node_id") val nodeId: String, val epoch: Long)

@Serializable
data class OutputLease(
    @SerialName("companion_id") val companionId: String,
    @SerialName("node_id") val nodeId: String,
    val epoch: Long,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("ttl_seconds") val ttlSeconds: Double,
    @SerialName("server_time") val serverTime: String,
)

@Serializable
data class ObservationCreate(
    @SerialName("command_id") val commandId: String,
    val modality: String,
    val data: JsonObject,
    @SerialName("user_initiated") val userInitiated: Boolean = false,
)

@Serializable
data class ProtocolEvent(
    @SerialName("event_id") val eventId: String,
    val sequence: Long,
    @SerialName("protocol_version") val protocolVersion: String,
    val type: String,
    @SerialName("companion_id") val companionId: String? = null,
    @SerialName("node_id") val nodeId: String? = null,
    @SerialName("recipient_node_id") val recipientNodeId: String? = null,
    val payload: JsonObject,
    @SerialName("occurred_at") val occurredAt: String,
)

@Serializable
data class EventPage(val events: List<ProtocolEvent>, @SerialName("next_sequence") val nextSequence: Long)

@Serializable
data class PreparedTransferPayload(val transfer: TransferSnapshot, val role: TransferRole)

@Serializable
data class TransferPayload(val transfer: TransferSnapshot)

@Serializable
data class TransferredPayload(val transfer: TransferSnapshot, val companion: CompanionSnapshot)

@Serializable
data class CompanionPayload(val companion: CompanionSnapshot)

@Serializable
enum class MemoryKind {
    @SerialName("episode") EPISODE,
    @SerialName("fact") FACT,
    @SerialName("preference") PREFERENCE,
    @SerialName("relationship") RELATIONSHIP,
}

@Serializable
enum class MemoryLayer {
    @SerialName("episodic") EPISODIC,
    @SerialName("semantic") SEMANTIC,
}

@Serializable
enum class MemoryRetentionPolicy {
    @SerialName("auto") AUTO,
    @SerialName("permanent") PERMANENT,
    @SerialName("stable") STABLE,
    @SerialName("significant_episode") SIGNIFICANT_EPISODE,
    @SerialName("routine_episode") ROUTINE_EPISODE,
}

@Serializable
data class MemoryWrite(
    @SerialName("command_id") val commandId: String,
    val kind: MemoryKind,
    val content: String,
    @SerialName("source_event_id") val sourceEventId: String,
    @SerialName("source_node_id") val sourceNodeId: String? = null,
    @SerialName("semantic_key") val semanticKey: String? = null,
    val tags: List<String> = emptyList(),
    val importance: Double = 0.5,
    val confidence: Double = 1.0,
    val confirmed: Boolean = false,
    val pinned: Boolean = false,
    @SerialName("occurred_at") val occurredAt: String? = null,
    @SerialName("episode_key") val episodeKey: String? = null,
    @SerialName("valid_from") val validFrom: String? = null,
    @SerialName("valid_to") val validTo: String? = null,
    @SerialName("retention_policy") val retentionPolicy: MemoryRetentionPolicy = MemoryRetentionPolicy.AUTO,
)

@Serializable
data class MemoryCorrection(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val content: String,
    @SerialName("source_event_id") val sourceEventId: String,
    @SerialName("source_node_id") val sourceNodeId: String? = null,
    @SerialName("valid_from") val validFrom: String? = null,
    @SerialName("valid_to") val validTo: String? = null,
    val tags: List<String>? = null,
    val importance: Double? = null,
    val confidence: Double? = null,
    val confirmed: Boolean? = null,
    val pinned: Boolean? = null,
    @SerialName("retention_policy") val retentionPolicy: MemoryRetentionPolicy? = null,
)

@Serializable
data class MemoryMetadataUpdate(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val tags: List<String>? = null,
    val importance: Double? = null,
    val confidence: Double? = null,
    val confirmed: Boolean? = null,
    val pinned: Boolean? = null,
    @SerialName("retention_policy") val retentionPolicy: MemoryRetentionPolicy? = null,
)

@Serializable
data class MemoryRecord(
    @SerialName("memory_id") val memoryId: String,
    @SerialName("companion_id") val companionId: String,
    val kind: MemoryKind,
    val content: String,
    @SerialName("source_event_ids") val sourceEventIds: List<String>,
    @SerialName("semantic_key") val semanticKey: String? = null,
    val tags: List<String>,
    val importance: Double,
    val confidence: Double,
    val confirmed: Boolean,
    val pinned: Boolean,
    val active: Boolean,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
    val revision: Long = 1,
    val layer: MemoryLayer = if (kind == MemoryKind.EPISODE) MemoryLayer.EPISODIC else MemoryLayer.SEMANTIC,
    @SerialName("occurred_at") val occurredAt: String? = null,
    @SerialName("episode_key") val episodeKey: String? = null,
    @SerialName("valid_from") val validFrom: String? = null,
    @SerialName("valid_to") val validTo: String? = null,
    @SerialName("supersedes_id") val supersedesId: String? = null,
    @SerialName("retention_policy") val retentionPolicy: MemoryRetentionPolicy = MemoryRetentionPolicy.AUTO,
    @SerialName("effective_retention_policy") val effectiveRetentionPolicy: MemoryRetentionPolicy? = null,
    @SerialName("decay_factor") val decayFactor: Double? = null,
    @SerialName("effective_importance") val effectiveImportance: Double? = null,
)

@Serializable
data class MemoryRecall(
    val query: String,
    @SerialName("token_budget") val tokenBudget: Int? = null,
    @SerialName("max_items") val maxItems: Int? = null,
    val layer: MemoryLayer? = null,
    @SerialName("semantic_key") val semanticKey: String? = null,
)

@Serializable
data class RecallResult(
    val items: List<MemoryRecord>,
    @SerialName("context_text") val contextText: String,
    @SerialName("estimated_tokens") val estimatedTokens: Int,
    @SerialName("token_budget") val tokenBudget: Int,
    val truncated: Boolean,
    @SerialName("retrieval_strategy") val retrievalStrategy: String,
    @SerialName("model_calls") val modelCalls: Int,
    val summaries: List<JsonObject> = emptyList(),
    val relations: List<JsonObject> = emptyList(),
    val retrieval: JsonObject? = null,
    val rerank: MemoryRerank? = null,
    @SerialName("embedding_usage") val embeddingUsage: MemoryEmbeddingUsage? = null,
)

@Serializable
data class MemoryRerank(
    val enabled: Boolean = false,
    val applied: Boolean = false,
    val candidates: Int = 0,
    @SerialName("latency_ms") val latencyMs: Double = 0.0,
    @SerialName("error_code") val errorCode: String? = null,
)

@Serializable
data class MemoryEmbeddingUsage(
    @SerialName("input_tokens") val inputTokens: Int,
    @SerialName("output_tokens") val outputTokens: Int,
)

@Serializable
data class MemoryAutomationRun(val force: Boolean = true)

/** Omitting limit uses the server's configured embedding batch size. */
@Serializable
data class MemoryEmbeddingRun(val limit: Int? = null) {
    init {
        require(limit == null || limit in 1..64) { "limit must be in 1..64" }
    }
}

/** Replaces the complete alias list; an empty list explicitly clears aliases. */
@Serializable
data class GraphAliasUpdate(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val aliases: List<String>,
    @SerialName("source_event_ids") val sourceEventIds: List<String> = emptyList(),
) {
    init {
        require(commandId.length in 1..128 && commandId.isNotBlank()) { "command_id must contain 1..128 characters and not be blank" }
        require(expectedRevision in 1 until Long.MAX_VALUE) { "expected_revision must be in 1..2^63-2" }
        require(aliases.size <= 64) { "aliases must contain at most 64 entries" }
        require(aliases.all { it.length in 1..128 && it.isNotBlank() }) { "aliases must contain 1..128 nonblank characters" }
        require(sourceEventIds.size <= 64) { "source_event_ids must contain at most 64 entries" }
        require(sourceEventIds.all { it.length in 1..128 && it.isNotBlank() }) { "source_event_ids must contain 1..128 nonblank characters" }
    }
}

@Serializable
data class GraphEntityAliases(
    @SerialName("entity_id") val entityId: String,
    val name: String,
    val aliases: List<String>,
    val revision: Long,
    @SerialName("updated_at") val updatedAt: String,
    @SerialName("source_event_ids") val sourceEventIds: List<String>,
)
