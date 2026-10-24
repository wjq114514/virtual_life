package dev.virtuallife.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

@Serializable
enum class PersonalityResponseLength {
    @SerialName("brief") BRIEF,
    @SerialName("balanced") BALANCED,
    @SerialName("detailed") DETAILED,
}

@Serializable
enum class PersonalityMood {
    @SerialName("calm") CALM,
    @SerialName("cheerful") CHEERFUL,
    @SerialName("curious") CURIOUS,
    @SerialName("concerned") CONCERNED,
    @SerialName("tired") TIRED,
}

@Serializable
enum class PersonalityHistoryRole {
    @SerialName("user") USER,
    @SerialName("assistant") ASSISTANT,
}

@Serializable
data class PersonalityTraits(
    val warmth: Double = 0.7,
    val curiosity: Double = 0.7,
    val assertiveness: Double = 0.6,
    val playfulness: Double = 0.3,
    val initiative: Double = 0.3,
) {
    init {
        require(warmth.isFinite() && warmth in 0.0..1.0) { "warmth must be finite and in 0..1" }
        require(curiosity.isFinite() && curiosity in 0.0..1.0) { "curiosity must be finite and in 0..1" }
        require(assertiveness.isFinite() && assertiveness in 0.0..1.0) { "assertiveness must be finite and in 0..1" }
        require(playfulness.isFinite() && playfulness in 0.0..1.0) { "playfulness must be finite and in 0..1" }
        require(initiative.isFinite() && initiative in 0.0..1.0) { "initiative must be finite and in 0..1" }
    }
}

@Serializable
data class PersonalitySpeechStyle(
    val language: String = "zh-CN",
    val tone: String = "温和、自然，有自己的观点",
    @SerialName("response_length") val responseLength: PersonalityResponseLength = PersonalityResponseLength.BRIEF,
    val habits: List<String> = emptyList(),
)

/** Stable definition edited by the owner; defaults match the unconfigured runtime personality. */
@Serializable
data class PersonalityDefinition(
    val description: String = "{{char}} 是陪你共读、生活和照看设备的虚拟伙伴。",
    val personality: String = "温和而独立，对你的想法和周围事物保持好奇。",
    val values: List<String> = listOf("尊重你的节奏", "诚实表达自己的判断", "珍惜共同经历"),
    val boundaries: List<String> = listOf("不编造共同经历或未取得的视觉信息", "不把设备在线等同于你正在关注它"),
    val traits: PersonalityTraits = PersonalityTraits(),
    @SerialName("speech_style") val speechStyle: PersonalitySpeechStyle = PersonalitySpeechStyle(),
    val scenario: String = "随着你在设备间移动，接续当前活动。",
    @SerialName("first_message") val firstMessage: String = "我在。想一起读一会儿，还是先安静待着？",
    @SerialName("example_dialogue") val exampleDialogue: String =
        "<START>\n{{user}}: 今天有点累。\n{{char}}: 那我们先安静待一会儿，不急着追下一件事。",
    @SerialName("alternate_greetings") val alternateGreetings: List<String> = emptyList(),
)

/** UTC ISO 8601 timestamps are null until the first explicit write. */
@Serializable
data class PersonalitySnapshot(
    @SerialName("companion_id") val companionId: String,
    val revision: Long,
    val configured: Boolean,
    val definition: PersonalityDefinition,
    @SerialName("source_card") val sourceCard: JsonObject? = null,
    @SerialName("import_warnings") val importWarnings: List<String> = emptyList(),
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("updated_at") val updatedAt: String? = null,
)

@Serializable
data class PersonalityUpdate(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val definition: PersonalityDefinition,
)

@Serializable
data class PersonalityCardImport(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val card: JsonObject,
)

@Serializable
data class PersonalityState(
    val mood: PersonalityMood = PersonalityMood.CALM,
    val energy: Double = 0.8,
    @SerialName("social_battery") val socialBattery: Double = 0.7,
) {
    init {
        require(energy.isFinite() && energy in 0.0..1.0) { "energy must be finite and in 0..1" }
        require(socialBattery.isFinite() && socialBattery in 0.0..1.0) { "social_battery must be finite and in 0..1" }
    }
}

/** [state] is effective now; expiry retains [recordedState] and its revision. */
@Serializable
data class PersonalityStateSnapshot(
    @SerialName("companion_id") val companionId: String,
    val revision: Long,
    val state: PersonalityState,
    @SerialName("recorded_state") val recordedState: PersonalityState? = null,
    val expired: Boolean,
    @SerialName("source_event_id") val sourceEventId: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("updated_at") val updatedAt: String? = null,
)

@Serializable
data class PersonalityStateUpdate(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val state: PersonalityState,
    @SerialName("ttl_seconds") val ttlSeconds: Int = 1800,
    @SerialName("source_event_id") val sourceEventId: String? = null,
)

@Serializable
data class PersonalityHistoryMessage(val role: PersonalityHistoryRole, val content: String)

@Serializable
data class PersonalityPreview(
    @SerialName("user_message") val userMessage: String,
    @SerialName("user_name") val userName: String = "你",
    @SerialName("user_initiated") val userInitiated: Boolean = true,
    @SerialName("audio_requested") val audioRequested: Boolean = false,
    val history: List<PersonalityHistoryMessage> = emptyList(),
    @SerialName("token_budget") val tokenBudget: Int = 3072,
    @SerialName("memory_token_budget") val memoryTokenBudget: Int = 800,
    @SerialName("max_memory_items") val maxMemoryItems: Int = 8,
)

/** A suggestion computed by the runtime; it cannot create or replace an [OutputHandle]. */
@Serializable
data class ParticipationPreview(
    val respond: Boolean,
    @SerialName("allow_audio") val allowAudio: Boolean,
    val presence: String,
    @SerialName("max_reply_sentences") val maxReplySentences: Int,
    val reason: String,
)

@Serializable
data class MemoryReference(
    @SerialName("memory_id") val memoryId: String,
    val revision: Long,
)

@Serializable
data class GraphEntityReference(
    @SerialName("entity_id") val entityId: String,
    val revision: Long,
)

@Serializable
data class PersonalityPreviewResult(
    @SerialName("companion_id") val companionId: String,
    @SerialName("personality_revision") val personalityRevision: Long,
    @SerialName("state_revision") val stateRevision: Long,
    @SerialName("companion_revision") val companionRevision: Long,
    val epoch: Long,
    @SerialName("active_node_id") val activeNodeId: String? = null,
    @SerialName("context_text") val contextText: String,
    @SerialName("estimated_tokens") val estimatedTokens: Int,
    @SerialName("token_budget") val tokenBudget: Int,
    val truncated: Boolean,
    @SerialName("omitted_sections") val omittedSections: List<String>,
    @SerialName("memory_refs") val memoryRefs: List<String>,
    val participation: ParticipationPreview,
    @SerialName("model_calls") val modelCalls: Int,
    @SerialName("memory_versions") val memoryVersions: List<MemoryReference> = emptyList(),
    @SerialName("entity_versions") val entityVersions: List<GraphEntityReference> = emptyList(),
    val emotion: EmotionSnapshot? = null,
)
