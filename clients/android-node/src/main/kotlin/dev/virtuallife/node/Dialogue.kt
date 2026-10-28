package dev.virtuallife.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

@Serializable
enum class DialogueStatus {
    @SerialName("completed") COMPLETED,
    @SerialName("silent") SILENT,
    @SerialName("discarded") DISCARDED,
}

/** One bounded, user-initiated text request; the server enforces owner, lease and budgets. */
@Serializable
data class DialogueRequest(
    @SerialName("command_id") val commandId: String,
    @SerialName("node_id") val nodeId: String,
    val epoch: Long,
    @SerialName("expected_revision") val expectedRevision: Long,
    @SerialName("user_message") val userMessage: String,
    @SerialName("user_name") val userName: String = "你",
    @SerialName("user_initiated") val userInitiated: Boolean = true,
    @SerialName("audio_requested") val audioRequested: Boolean = false,
    val history: List<PersonalityHistoryMessage> = emptyList(),
    @SerialName("token_budget") val tokenBudget: Int = 3072,
    @SerialName("memory_token_budget") val memoryTokenBudget: Int = 800,
    @SerialName("max_memory_items") val maxMemoryItems: Int = 8,
    @SerialName("max_output_tokens") val maxOutputTokens: Int = 512,
    @SerialName("access_id") val accessId: String? = null,
    @SerialName("presentation_id") val presentationId: String? = null,
)

/**
 * One completed user-initiated turn from GET /v1/companions/{id}/dialogue/history.
 * History grants no present-output rights and never triggers a model or memory write.
 */
@Serializable
data class DialogueHistoryItem(
    @SerialName("event_id") val eventId: String,
    val sequence: Long,
    @SerialName("command_id") val commandId: String?,
    @SerialName("node_id") val nodeId: String?,
    @SerialName("occurred_at") val occurredAt: String,
    @SerialName("user_message") val userMessage: String,
    @SerialName("assistant_message") val assistantMessage: String,
)

/** Newest first, exclusive cursor; a null next cursor means the client has reached the start. */
@Serializable
data class DialogueHistoryPage(
    @SerialName("companion_id") val companionId: String,
    val items: List<DialogueHistoryItem>,
    @SerialName("next_before_sequence") val nextBeforeSequence: Long? = null,
    @SerialName("has_more") val hasMore: Boolean = false,
)

@Serializable
data class DialogueUsage(
    @SerialName("input_tokens") val inputTokens: Int,
    @SerialName("output_tokens") val outputTokens: Int,
)

/** Diagnostic scene-classifier metadata; never grants output authority. */
@Serializable
data class SceneObservation(
    val mode: String,
    val label: String? = null,
    val confidence: Double? = null,
    val scores: Map<String, Double>? = null,
    val reason: String? = null,
    val threshold: Double? = null,
    @SerialName("routed_model") val routedModel: String? = null,
    @SerialName("classifier_version") val classifierVersion: String? = null,
    val backend: String? = null,
    @SerialName("latency_ms") val latencyMs: Double? = null,
    @SerialName("error_code") val errorCode: String? = null,
)

/** Historical receipt, not an output permit. [createdAt] is a UTC ISO 8601 string. */
@Serializable
data class DialogueResult(
    @SerialName("command_id") val commandId: String,
    @SerialName("companion_id") val companionId: String,
    val status: DialogueStatus,
    @SerialName("assistant_message") val assistantMessage: String? = null,
    @SerialName("source_event_id") val sourceEventId: String? = null,
    val preview: PersonalityPreviewResult,
    val model: String? = null,
    val usage: DialogueUsage? = null,
    @SerialName("estimated_input_tokens") val estimatedInputTokens: Int,
    @SerialName("model_calls") val modelCalls: Int,
    @SerialName("created_at") val createdAt: String,
    @SerialName("memory_retrieval") val memoryRetrieval: JsonObject? = null,
    @SerialName("accepted_event_id") val acceptedEventId: String? = null,
    val emotion: EmotionSnapshot? = null,
    val scene: SceneObservation? = null,
    @SerialName("discard_reason") val discardReason: String? = null,
)

@Serializable
data class DialogueStreamStatus(
    @SerialName("command_id") val commandId: String,
    @SerialName("companion_id") val companionId: String,
    val stage: String,
    @SerialName("discard_reason") val discardReason: String? = null,
    @SerialName("accepted_event_id") val acceptedEventId: String? = null,
)

@Serializable
data class DialogueStreamSnapshot(
    @SerialName("command_id") val commandId: String,
    @SerialName("companion_id") val companionId: String,
    val preview: PersonalityPreviewResult,
    val emotion: EmotionSnapshot? = null,
)

/** [text] replaces the entire temporary reply; it is not an append-only chunk. */
@Serializable
data class DialogueStreamDelta(
    @SerialName("command_id") val commandId: String,
    @SerialName("companion_id") val companionId: String,
    val sequence: Long,
    val text: String,
)

@Serializable
data class DialogueStreamFailure(
    @SerialName("status_code") val statusCode: Int,
    val code: String,
    val message: String,
)

@Serializable
data class DialogueStreamError(
    @SerialName("command_id") val commandId: String,
    @SerialName("companion_id") val companionId: String,
    val error: DialogueStreamFailure,
)

/** Temporary events and historical results never grant output rights; enforce [LeaseGate]. */
sealed interface DialogueStreamEvent {
    data class Status(val value: DialogueStreamStatus) : DialogueStreamEvent
    data class Snapshot(val value: DialogueStreamSnapshot) : DialogueStreamEvent
    data class Delta(val value: DialogueStreamDelta) : DialogueStreamEvent
    data class Result(val value: DialogueResult) : DialogueStreamEvent
    data class Error(val value: DialogueStreamError) : DialogueStreamEvent
}
