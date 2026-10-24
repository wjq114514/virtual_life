package dev.virtuallife.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class InnerActivityKind {
    @SerialName("restless") RESTLESS,
    @SerialName("curious") CURIOUS,
    @SerialName("concerned") CONCERNED,
    @SerialName("reflective") REFLECTIVE,
    @SerialName("content") CONTENT,
    @SerialName("resting") RESTING,
}

@Serializable
enum class InnerActivityGenerator {
    @SerialName("rules_v1") RULES_V1,
    @SerialName("model_v1") MODEL_V1,
}

@Serializable
enum class InnerActivityStatus {
    @SerialName("active") ACTIVE,
    @SerialName("expired") EXPIRED,
}

@Serializable
enum class InnerActivityTrigger {
    @SerialName("social_need") SOCIAL_NEED,
    @SerialName("curiosity") CURIOSITY,
    @SerialName("concern") CONCERN,
    @SerialName("reflection") REFLECTION,
    @SerialName("contentment") CONTENTMENT,
    @SerialName("low_energy") LOW_ENERGY,
}

/** Audit metadata for an optional model-assisted judgment. Never prompt content. */
@Serializable
data class InnerModelInfo(
    val model: String,
    @SerialName("input_tokens") val inputTokens: Int,
    @SerialName("output_tokens") val outputTokens: Int,
    val confidence: Double,
    val rationale: String,
)

/** Local, short-lived interpretation. It never grants output permission. */
@Serializable
data class InnerActivity(
    @SerialName("activity_id") val activityId: String,
    @SerialName("companion_id") val companionId: String,
    val sequence: Long,
    val kind: InnerActivityKind,
    val trigger: InnerActivityTrigger,
    val content: String,
    val salience: Double,
    val urge: Double,
    @SerialName("attention_session_id") val attentionSessionId: String? = null,
    @SerialName("attention_boundary_id") val attentionBoundaryId: String? = null,
    @SerialName("context_version") val contextVersion: String,
    @SerialName("source_event_id") val sourceEventId: String? = null,
    val generator: InnerActivityGenerator = InnerActivityGenerator.RULES_V1,
    val model: InnerModelInfo? = null,
    val status: InnerActivityStatus,
    @SerialName("created_at") val createdAt: String,
    @SerialName("expires_at") val expiresAt: String,
)

@Serializable
data class InnerNeeds(
    val social: Double,
    val curiosity: Double,
    val concern: Double,
    val reflection: Double,
    val rest: Double,
)

@Serializable
data class InnerLifeSnapshot(
    @SerialName("companion_id") val companionId: String,
    val enabled: Boolean,
    @SerialName("computed_at") val computedAt: String,
    @SerialName("context_version") val contextVersion: String,
    @SerialName("idle_seconds") val idleSeconds: Long,
    @SerialName("last_interaction_at") val lastInteractionAt: String? = null,
    val mood: String,
    val energy: Double,
    @SerialName("social_battery") val socialBattery: Double,
    val valence: Double,
    @SerialName("relationship_warmth") val relationshipWarmth: Double,
    val familiarity: Double,
    val needs: InnerNeeds,
    @SerialName("dominant_kind") val dominantKind: InnerActivityKind,
    val salience: Double,
    val urge: Double,
    val current: InnerActivity? = null,
    val activities: List<InnerActivity> = emptyList(),
    @SerialName("model_assisted") val modelAssisted: Boolean = false,
    @SerialName("model_calls") val modelCalls: Int = 0,
)

@Serializable
data class InnerActivityPage(
    @SerialName("companion_id") val companionId: String,
    val items: List<InnerActivity>,
    @SerialName("next_sequence") val nextSequence: Long,
    @SerialName("has_more") val hasMore: Boolean,
)
