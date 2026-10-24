package dev.virtuallife.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class SlowVariableTone {
    @SerialName("neutral") NEUTRAL,
    @SerialName("warm") WARM,
    @SerialName("cautious") CAUTIOUS,
}

/** Engineering parameters from the server policy; clients only render, never mutate. */
@Serializable
data class SlowVariablePolicy(
    val enabled: Boolean = true,
    @SerialName("half_life_seconds") val halfLifeSeconds: Int = 2592000,
    @SerialName("window_seconds") val windowSeconds: Int = 31536000,
    @SerialName("candidate_limit") val candidateLimit: Int = 256,
    @SerialName("min_evidence_items") val minEvidenceItems: Int = 3,
    @SerialName("warm_valence_threshold") val warmValenceThreshold: Double = 0.1,
    @SerialName("concern_valence_threshold") val concernValenceThreshold: Double = -0.05,
    @SerialName("warmth_memory_scale") val warmthMemoryScale: Double = 3.0,
    @SerialName("familiarity_scale") val familiarityScale: Double = 12.0,
    @SerialName("warmth_weight") val warmthWeight: Double = 0.8,
    @SerialName("engagement_min") val engagementMin: Double = 0.2,
    @SerialName("engagement_max") val engagementMax: Double = 0.8,
)

/**
 * Read-only L2 aggregate computed at server read time; it never writes the profile, calls a model,
 * or grants output rights. Below [SlowVariablePolicy.minEvidenceItems] the server keeps
 * [engagement] at exactly 0.5 and [tone] neutral. [contextVersion] changes with the evidence set.
 */
@Serializable
data class SlowVariableSnapshot(
    @SerialName("companion_id") val companionId: String,
    val enabled: Boolean,
    @SerialName("computed_at") val computedAt: String,
    @SerialName("window_start") val windowStart: String,
    @SerialName("sample_size") val sampleSize: Int = 0,
    @SerialName("valence_mean") val valenceMean: Double = 0.0,
    @SerialName("valence_inertia") val valenceInertia: Double = 0.0,
    @SerialName("relationship_warmth") val relationshipWarmth: Double = 0.5,
    val familiarity: Double = 0.0,
    val engagement: Double = 0.5,
    @SerialName("tone_hint") val tone: SlowVariableTone = SlowVariableTone.NEUTRAL,
    @SerialName("context_version") val contextVersion: String,
    @SerialName("model_calls") val modelCalls: Int = 0,
)
