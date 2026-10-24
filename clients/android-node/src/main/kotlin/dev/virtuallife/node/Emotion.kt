package dev.virtuallife.node

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class Affect(val valence: Double, val arousal: Double, val control: Double) {
    init {
        require(valence.isFinite() && valence in -1.0..1.0)
        require(arousal.isFinite() && arousal in 0.0..1.0)
        require(control.isFinite() && control in 0.0..1.0)
    }
}

@Serializable
data class AffectDelta(
    val valence: Double = 0.0,
    val arousal: Double = 0.0,
    val control: Double = 0.0,
    val energy: Double = 0.0,
    @SerialName("social_battery") val socialBattery: Double = 0.0,
)

/** Read at server time; contextVersion changes on writes/evidence changes, not ordinary recovery. */
@Serializable
data class EmotionSnapshot(
    @SerialName("companion_id") val companionId: String,
    val revision: Long,
    val enabled: Boolean,
    val state: Affect,
    val baseline: Affect,
    @SerialName("effective_state") val effectiveState: PersonalityState,
    val reaction: String,
    @SerialName("quiet_requested") val quietRequested: Boolean = false,
    @SerialName("context_version") val contextVersion: String,
    @SerialName("source_event_id") val sourceEventId: String? = null,
    @SerialName("evaluated_at") val evaluatedAt: String,
    @SerialName("updated_at") val updatedAt: String? = null,
    @SerialName("active_experiences") val activeExperiences: Int,
    val truncated: Boolean,
    @SerialName("half_life_seconds") val halfLifeSeconds: Int,
    @SerialName("model_calls") val modelCalls: Int = 0,
    @SerialName("recovery_half_lives") val recoveryHalfLives: EmotionRecoveryHalfLives? = null,
)

@Serializable
data class EmotionExperience(
    @SerialName("event_id") val eventId: String,
    @SerialName("completed_event_id") val completedEventId: String? = null,
    @SerialName("companion_id") val companionId: String,
    val reaction: String,
    val reason: String,
    val confidence: Double,
    val delta: AffectDelta,
    @SerialName("resonance_delta") val resonanceDelta: AffectDelta,
    @SerialName("memory_versions") val memoryVersions: List<MemoryReference>,
    @SerialName("resonance_event_ids") val resonanceEventIds: List<String>,
    val status: String,
    @SerialName("resonance_valid") val resonanceValid: Boolean,
    @SerialName("created_at") val createdAt: String,
    val appraisal: AppraisalVector? = null,
    @SerialName("appraisal_version") val appraisalVersion: String = "legacy_v1",
    val uncertainty: Double = 1.0,
    val attribution: String = "unspecified",
    @SerialName("original_delta") val originalDelta: AffectDelta? = null,
    val correction: EmotionCorrectionInfo? = null,
)

@Serializable
data class EmotionExperiencePage(
    val items: List<EmotionExperience>,
    @SerialName("next_sequence") val nextSequence: Long,
    @SerialName("has_more") val hasMore: Boolean,
)

@Serializable
data class EmotionInvalidation(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
)


/** Rule interpretation dimensions, not psychological measurements of the user. */
@Serializable
data class AppraisalVector(
    val pleasantness: Double = 0.0,
    val effort: Double = 0.0,
    val uncertainty: Double = 0.0,
    val attention: Double = 0.0,
    val agency: Double = 0.0,
    val control: Double = 0.5,
) {
    init {
        require(pleasantness.isFinite() && pleasantness in -1.0..1.0)
        require(agency.isFinite() && agency in -1.0..1.0)
        require(listOf(effort, uncertainty, attention, control).all { it.isFinite() && it in 0.0..1.0 })
    }
}

@Serializable
data class EmotionRecoveryHalfLives(
    val valence: Int,
    val arousal: Int,
    val control: Int,
    val energy: Int,
    @SerialName("social_battery") val socialBattery: Int,
)

@Serializable
data class EmotionCorrectionInfo(
    val kind: String,
    @SerialName("retained_weight") val retainedWeight: Double? = null,
    val attribution: String? = null,
    val revision: Long,
    @SerialName("corrected_at") val correctedAt: String,
)

/** Inapplicable fields are omitted even when NodeJson.encodeDefaults is enabled. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class EmotionCorrection(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val kind: String,
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("retained_weight") val retainedWeight: Double? = null,
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    val attribution: String? = null,
) {
    init {
        require(kind in setOf("direction", "intensity", "attribution"))
        require(retainedWeight == null || (kind == "intensity" && retainedWeight.isFinite() && retainedWeight in 0.0..1.0))
        require(if (kind == "attribution") attribution in setOf("self", "other", "situation", "unspecified") else attribution == null)
    }
}
