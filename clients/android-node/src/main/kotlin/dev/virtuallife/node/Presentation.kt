package dev.virtuallife.node

import kotlinx.datetime.Instant
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class PresentationKind {
    @SerialName("terminal") TERMINAL,
    @SerialName("android") ANDROID,
}

@Serializable
data class PresentationOpen(
    @SerialName("presentation_id") val presentationId: String,
    val kind: PresentationKind = PresentationKind.ANDROID,
) {
    init { require(presentationId.isNotBlank()) }
}

@Serializable
data class PresentationSnapshot(
    @SerialName("node_id") val nodeId: String,
    @SerialName("presentation_id") val presentationId: String,
    val kind: PresentationKind,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("ttl_seconds") val ttlSeconds: Double,
    @SerialName("server_time") val serverTime: String,
    @SerialName("opened_sequence") val openedSequence: Long,
)

data class PresentationSample(
    val presentation: PresentationSnapshot,
    val requestStartedNanos: Long,
    val responseReceivedNanos: Long,
)

interface NativePresentationApi {
    suspend fun openPresentation(nodeId: String, body: PresentationOpen): PresentationSample
    suspend fun heartbeatPresentation(nodeId: String, presentationId: String): PresentationSample
    suspend fun releasePresentation(nodeId: String, presentationId: String): PresentationSnapshot
}

data class PresentationPermit(
    val nodeId: String,
    val presentationId: String,
    val kind: PresentationKind,
    val deadlineNanos: Long,
)

/** Local monotonic expiry for presentation state; wall-clock timestamps are validation input. */
class PresentationGate(
    private val clock: MonotonicClock,
    private val safetyMarginNanos: Long = 250_000_000L,
    private val maxTtlNanos: Long = 120_000_000_000L,
) {
    private var permit: PresentationPermit? = null
    private var lastRequestStartNanos: Long? = null
    private var lastClockNanos: Long? = null
    private var clockFault = false

    init { require(safetyMarginNanos >= 0 && maxTtlNanos > safetyMarginNanos) }

    @Synchronized
    fun grant(sample: PresentationSample): Boolean {
        val value = sample.presentation
        val now = readClock() ?: return false
        if (sample.responseReceivedNanos < sample.requestStartedNanos ||
            sample.responseReceivedNanos > now || value.ttlSeconds <= 0 ||
            !value.ttlSeconds.isFinite() || value.openedSequence < 0
        ) return false
        val previous = permit
        if (previous != null && (previous.nodeId != value.nodeId ||
                previous.presentationId != value.presentationId || previous.kind != value.kind)
        ) return false
        val lastStart = lastRequestStartNanos
        if (lastStart != null && sample.requestStartedNanos <= lastStart) return false
        val serverRemaining = runCatching {
            (Instant.parse(value.expiresAt) - Instant.parse(value.serverTime)).inWholeNanoseconds
        }.getOrNull() ?: return false
        val ttl = (value.ttlSeconds * 1_000_000_000.0).toLong()
        val bounded = minOf(serverRemaining, ttl, maxTtlNanos)
        if (bounded <= safetyMarginNanos) return false
        val deadline = sample.requestStartedNanos + bounded - safetyMarginNanos
        if (deadline <= now) return false
        lastRequestStartNanos = sample.requestStartedNanos
        permit = PresentationPermit(value.nodeId, value.presentationId, value.kind, deadline)
        return true
    }

    @Synchronized
    fun canPresent(nodeId: String, presentationId: String, kind: PresentationKind): Boolean {
        val current = readClock() ?: return false
        val active = permit ?: return false
        if (active.deadlineNanos <= current) {
            permit = null
            return false
        }
        return active.nodeId == nodeId && active.presentationId == presentationId && active.kind == kind
    }

    @Synchronized
    fun deadlineNanos(): Long? {
        val current = readClock() ?: return null
        val active = permit ?: return null
        if (active.deadlineNanos <= current) {
            permit = null
            return null
        }
        return active.deadlineNanos
    }

    @Synchronized
    fun revoke() { permit = null }

    private fun readClock(): Long? {
        if (clockFault) return null
        val current = clock.nowNanos()
        val previous = lastClockNanos
        if (current < 0 || (previous != null && current < previous)) {
            clockFault = true
            permit = null
            return null
        }
        lastClockNanos = current
        return current
    }
}
