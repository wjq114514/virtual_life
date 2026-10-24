package dev.virtuallife.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Host-supplied advisory signals. Unknown fields suppress proactive speech. */
@Serializable
data class AttentionSample(
    @SerialName("session_id") val sessionId: String,
    val sequence: Long,
    @SerialName("sampled_at") val sampledAt: String,
    @SerialName("schema_version") val schemaVersion: String = "1.0",
    @SerialName("ttl_seconds") val ttlSeconds: Int = 90,
    val activity: String = "unknown",
    val focus: String = "unknown",
    val availability: String = "unknown",
    @SerialName("user_present") val userPresent: Boolean? = null,
    val locked: Boolean? = null,
    val meeting: Boolean? = null,
    @SerialName("screen_sharing") val screenSharing: Boolean? = null,
    val boundary: String = "none",
    @SerialName("boundary_id") val boundaryId: String? = null,
)

@Serializable
data class AttentionObservation(
    @SerialName("command_id") val commandId: String,
    val sample: AttentionSample,
)

@Serializable
data class AttentionSnapshot(
    @SerialName("node_id") val nodeId: String,
    @SerialName("source_event_id") val sourceEventId: String? = null,
    val sample: AttentionSample? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    val stale: Boolean = true,
)

@Serializable
data class ProactivityPolicy(
    val enabled: Boolean = false,
    @SerialName("daily_limit") val dailyLimit: Int = 1,
    @SerialName("cooldown_seconds") val cooldownSeconds: Int = 3600,
    @SerialName("candidate_ttl_seconds") val candidateTtlSeconds: Int = 120,
    @SerialName("token_budget") val tokenBudget: Int = 3072,
    @SerialName("memory_token_budget") val memoryTokenBudget: Int = 400,
    @SerialName("max_output_tokens") val maxOutputTokens: Int = 128,
)

@Serializable
data class ProactivityPolicyUpdate(
    @SerialName("command_id") val commandId: String,
    @SerialName("expected_revision") val expectedRevision: Long,
    val policy: ProactivityPolicy,
)

@Serializable
data class ProactivityPolicySnapshot(
    @SerialName("companion_id") val companionId: String,
    val revision: Long,
    val policy: ProactivityPolicy,
    @SerialName("updated_at") val updatedAt: String? = null,
)

/** Durable decision/attempt, never permission to display its historical reply. */
@Serializable
data class ProactivityDecision(
    val sequence: Long,
    @SerialName("candidate_id") val candidateId: String,
    @SerialName("companion_id") val companionId: String,
    @SerialName("node_id") val nodeId: String,
    val epoch: Long,
    @SerialName("source_event_id") val sourceEventId: String,
    val boundary: String,
    @SerialName("policy_revision") val policyRevision: Long,
    val status: String,
    val reason: String,
    @SerialName("command_id") val commandId: String,
    @SerialName("created_at") val createdAt: String,
    @SerialName("expires_at") val expiresAt: String,
    val origin: String = "attention",
    @SerialName("source_activity_id") val sourceActivityId: String? = null,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("finished_at") val finishedAt: String? = null,
)

@Serializable
data class ProactivityDecisionPage(
    val decisions: List<ProactivityDecision>,
    @SerialName("next_sequence") val nextSequence: Long,
)

/** Continuous host advice, not a calibrated flow detector or permission to speak. */
@Serializable
data class FocusSegment(
    @SerialName("node_id") val nodeId: String,
    @SerialName("session_id") val sessionId: String,
    @SerialName("started_at") val startedAt: String,
    @SerialName("ended_at") val endedAt: String,
    @SerialName("duration_seconds") val durationSeconds: Double,
    val reason: String,
)

@Serializable
data class FocusSnapshot(
    @SerialName("node_id") val nodeId: String,
    val state: String = "unknown",
    val source: String = "host_advisory",
    @SerialName("session_id") val sessionId: String? = null,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("confirmed_at") val confirmedAt: String? = null,
    @SerialName("release_started_at") val releaseStartedAt: String? = null,
    @SerialName("last_sampled_at") val lastSampledAt: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    val stale: Boolean = true,
    @SerialName("protection_active") val protectionActive: Boolean = false,
    @SerialName("duration_seconds") val durationSeconds: Double = 0.0,
    @SerialName("completed_count") val completedCount: Int = 0,
    @SerialName("recent_segments") val recentSegments: List<FocusSegment> = emptyList(),
)

/** Fresh, server-filtered text; the host must still reject a late response locally. */
@Serializable
data class ProactivityOutput(
    val sequence: Long,
    @SerialName("event_id") val eventId: String,
    @SerialName("command_id") val commandId: String,
    @SerialName("assistant_message") val assistantMessage: String,
    @SerialName("created_at") val createdAt: String,
)

@Serializable
data class ProactivityOutputPage(
    @SerialName("companion_id") val companionId: String,
    @SerialName("node_id") val nodeId: String,
    @SerialName("access_id") val accessId: String,
    val epoch: Long,
    @SerialName("companion_revision") val companionRevision: Long,
    val items: List<ProactivityOutput>,
    @SerialName("next_sequence") val nextSequence: Long,
)
