package dev.virtuallife.node

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonClassDiscriminator
import kotlinx.serialization.json.JsonObject

@Serializable
enum class PersonalityProposalStatus {
    @SerialName("proposed") PROPOSED,
    @SerialName("accepted") ACCEPTED,
    @SerialName("rejected") REJECTED,
    @SerialName("superseded") SUPERSEDED,
    @SerialName("expired") EXPIRED,
}

@Serializable
enum class EvolutionSkipReason {
    @SerialName("insufficient_evidence") INSUFFICIENT_EVIDENCE,
    @SerialName("below_threshold") BELOW_THRESHOLD,
    @SerialName("open_proposal_exists") OPEN_PROPOSAL_EXISTS,
    @SerialName("cooldown_active") COOLDOWN_ACTIVE,
    @SerialName("cumulative_drift_exceeded") CUMULATIVE_DRIFT_EXCEEDED,
    @SerialName("field_at_bound") FIELD_AT_BOUND,
    @SerialName("calibration_requires_phase_c") CALIBRATION_REQUIRES_PHASE_C,
    @SerialName("model_semantics_required") MODEL_SEMANTICS_REQUIRED,
    @SerialName("invalid_model_proposal") INVALID_MODEL_PROPOSAL,
}

@Serializable
enum class EvolutionMetric {
    @SerialName("quiet_request_rate") QUIET_REQUEST_RATE,
    @SerialName("proactive_silent_rate") PROACTIVE_SILENT_RATE,
    @SerialName("correction_rate") CORRECTION_RATE,
    @SerialName("preference_repeat") PREFERENCE_REPEAT,
}

/**
 * Field-level patch operations proposed by the runtime. [op] is the JSON discriminator;
 * the runtime never removes values, boundaries, or habits.
 */
@OptIn(ExperimentalSerializationApi::class)
@JsonClassDiscriminator("op")
@Serializable
sealed interface PersonalityPatchOperation

@Serializable
@SerialName("adjust_trait")
data class TraitAdjustment(
    val field: String,
    val delta: Double,
    val before: Double,
    val after: Double,
) : PersonalityPatchOperation

@Serializable
@SerialName("set_text")
data class TextReplacement(
    val field: String,
    val before: String,
    val after: String,
) : PersonalityPatchOperation

@Serializable
@SerialName("set_style")
data class StyleReplacement(
    val field: String,
    val before: String,
    val after: String,
) : PersonalityPatchOperation

@Serializable
@SerialName("add_item")
data class ItemAddition(
    val field: String,
    val item: String,
) : PersonalityPatchOperation

@Serializable
data class EvolutionAggregate(
    val metric: EvolutionMetric,
    val value: Double,
    val threshold: Double,
    val samples: Int,
)

@Serializable
data class MemoryEvidenceReference(
    @SerialName("memory_id") val memoryId: String,
    val revision: Long,
)

@Serializable
data class CorrectionEvidenceReference(
    @SerialName("event_id") val eventId: String,
    val kind: String,
    val revision: Long,
)

/** Local, verifiable sources behind a proposal; nothing here is model-generated prose. */
@Serializable
data class EvolutionEvidence(
    @SerialName("window_days") val windowDays: Int,
    @SerialName("sample_size") val sampleSize: Int,
    @SerialName("memory_refs") val memoryRefs: List<MemoryEvidenceReference> = emptyList(),
    @SerialName("event_refs") val eventRefs: List<String> = emptyList(),
    @SerialName("correction_refs") val correctionRefs: List<CorrectionEvidenceReference> = emptyList(),
    val aggregates: List<EvolutionAggregate> = emptyList(),
)

/** A proposal never edits the profile until the owner explicitly accepts it. */
@Serializable
data class PersonalityEvolutionProposal(
    @SerialName("proposal_id") val proposalId: String,
    @SerialName("companion_id") val companionId: String,
    val revision: Long,
    val status: PersonalityProposalStatus,
    @SerialName("base_personality_revision") val basePersonalityRevision: Long,
    val generator: String,
    @SerialName("generator_version") val generatorVersion: String,
    val confidence: Double,
    val rationale: String,
    val patch: List<PersonalityPatchOperation>,
    val evidence: EvolutionEvidence,
    val model: JsonObject? = null,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("decided_at") val decidedAt: String? = null,
)

@Serializable
data class PersonalityEvolutionPage(
    @SerialName("companion_id") val companionId: String,
    val items: List<PersonalityEvolutionProposal>,
    @SerialName("next_sequence") val nextSequence: Long,
    @SerialName("has_more") val hasMore: Boolean,
)

@Serializable
data class PersonalityEvolutionRefresh(
    @SerialName("command_id") val commandId: String,
    @SerialName("use_model") val useModel: Boolean = false,
    @SerialName("max_proposals") val maxProposals: Int = 1,
)

@Serializable
data class EvolutionSkipped(
    val reason: EvolutionSkipReason,
    val metric: EvolutionMetric? = null,
)

@Serializable
data class PersonalityEvolutionRefreshResult(
    @SerialName("companion_id") val companionId: String,
    @SerialName("base_personality_revision") val basePersonalityRevision: Long,
    val generated: Int,
    val skipped: List<EvolutionSkipped>,
    val proposals: List<PersonalityEvolutionProposal>,
    @SerialName("model_calls") val modelCalls: Int = 0,
    @SerialName("created_at") val createdAt: String,
)

/** Revision fences make every owner decision compare-and-set on both the profile and proposal. */
@Serializable
data class PersonalityEvolutionDecision(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_personality_revision") val expectedPersonalityRevision: Long,
    @SerialName("expected_proposal_revision") val expectedProposalRevision: Long,
)

@Serializable
data class PersonalityEvolutionRejection(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_personality_revision") val expectedPersonalityRevision: Long,
    @SerialName("expected_proposal_revision") val expectedProposalRevision: Long,
    val reason: String? = null,
)

@Serializable
data class PersonalityEvolutionDecisionResult(
    @SerialName("companion_id") val companionId: String,
    val proposal: PersonalityEvolutionProposal,
    val personality: PersonalitySnapshot,
    @SerialName("decided_at") val decidedAt: String,
)

@Serializable
data class PersonalityRevisionEntry(
    @SerialName("companion_id") val companionId: String,
    val revision: Long,
    val definition: PersonalityDefinition,
    val source: String,
    @SerialName("source_ref") val sourceRef: String? = null,
    @SerialName("created_at") val createdAt: String,
)

@Serializable
data class PersonalityHistoryPage(
    @SerialName("companion_id") val companionId: String,
    val items: List<PersonalityRevisionEntry>,
    @SerialName("next_before_revision") val nextBeforeRevision: Long? = null,
    @SerialName("has_more") val hasMore: Boolean,
)
