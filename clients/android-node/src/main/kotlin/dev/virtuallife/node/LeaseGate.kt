package dev.virtuallife.node

import kotlinx.datetime.Instant

/** Android hosts MUST use elapsedRealtimeNanos(), which includes device sleep. */
fun interface MonotonicClock {
    fun nowNanos(): Long
}

/** For JVM hosts/tests only; Android hosts provide SystemClock.elapsedRealtimeNanos. */
object JvmMonotonicClock : MonotonicClock {
    override fun nowNanos(): Long = System.nanoTime()
}

data class OutputPermit(
    val companionId: String,
    val nodeId: String,
    val epoch: Long,
    val deadlineNanos: Long,
    val renewAtNanos: Long,
)

/**
 * Only measured REST lease replies can create permits. Historical state/events cannot.
 * The deadline is request-start + min(server remaining time, TTL) - safety margin.
 */
class LeaseGate(
    val nodeId: String,
    private val clock: MonotonicClock,
    private val safetyMarginNanos: Long = 250_000_000L,
    private val maxLeaseNanos: Long = 120_000_000_000L,
) {
    private val snapshots = mutableMapOf<String, CompanionSnapshot>()
    private val permits = mutableMapOf<String, OutputPermit>()
    private val paused = mutableSetOf<String>()
    private val lastLeaseStarts = mutableMapOf<String, Long>()
    private var lastClockRead: Long? = null
    private var clockFault = false

    init {
        require(nodeId.isNotBlank())
        require(safetyMarginNanos >= 0 && maxLeaseNanos > safetyMarginNanos)
    }

    /** Reject old or contradictory snapshots, including responses arriving out of order. */
    @Synchronized
    fun observe(snapshot: CompanionSnapshot): Boolean {
        if (snapshot.epoch < 0 || snapshot.revision < 0) return false
        val previous = snapshots[snapshot.companionId]
        if (previous != null) {
            if (snapshot.epoch < previous.epoch || snapshot.revision < previous.revision) return false
            if (snapshot.epoch > previous.epoch && snapshot.revision <= previous.revision) return false
            if (snapshot.epoch == previous.epoch && snapshot.activeNodeId != previous.activeNodeId) return false
            if (snapshot.epoch == previous.epoch && snapshot.revision == previous.revision &&
                (snapshot.name != previous.name || snapshot.activity != previous.activity ||
                    snapshot.context != previous.context || snapshot.memoryRefs != previous.memoryRefs ||
                    snapshot.interactionMode != previous.interactionMode)
            ) return false
        }
        snapshots[snapshot.companionId] = snapshot
        val permit = permits[snapshot.companionId]
        if (snapshot.activeNodeId != nodeId || permit?.epoch != snapshot.epoch || snapshot.leaseExpiresAt == null) {
            permits.remove(snapshot.companionId)
        }
        return true
    }

    @Synchronized
    fun snapshot(companionId: String): CompanionSnapshot? = snapshots[companionId]

    @Synchronized
    fun grant(sample: LeaseSample): Boolean {
        val lease = sample.lease
        val snapshot = snapshots[lease.companionId] ?: return false
        val now = readClock() ?: return false
        if (paused.contains(lease.companionId) || snapshot.activeNodeId != nodeId || lease.nodeId != nodeId ||
            lease.epoch != snapshot.epoch || !lease.ttlSeconds.isFinite() || lease.ttlSeconds <= 0
        ) return false
        if (sample.responseReceivedNanos < sample.requestStartedNanos || sample.responseReceivedNanos > now) {
            faultClock()
            return false
        }
        val lastStart = lastLeaseStarts[lease.companionId]
        if (lastStart != null && sample.requestStartedNanos <= lastStart) return false
        val remaining = runCatching { (Instant.parse(lease.expiresAt) - Instant.parse(lease.serverTime)).inWholeNanoseconds }
            .getOrNull() ?: return false
        if (remaining <= 0) return false
        val ttlNanos = minOf(lease.ttlSeconds * 1_000_000_000.0, maxLeaseNanos.toDouble()).toLong()
        val budget = minOf(ttlNanos, remaining, maxLeaseNanos) - safetyMarginNanos
        if (budget <= 0 || sample.requestStartedNanos > Long.MAX_VALUE - budget) return false
        val deadline = sample.requestStartedNanos + budget
        if (deadline <= now) return false
        permits[lease.companionId] = OutputPermit(
            lease.companionId, nodeId, lease.epoch, deadline, sample.requestStartedNanos + budget / 2,
        )
        lastLeaseStarts[lease.companionId] = sample.requestStartedNanos
        return true
    }

    @Synchronized
    fun permit(companionId: String, epoch: Long? = null): OutputPermit? {
        val now = readClock() ?: return null
        val permit = permits[companionId] ?: return null
        val snapshot = snapshots[companionId]
        if (now >= permit.deadlineNanos || paused.contains(companionId) || snapshot?.activeNodeId != nodeId ||
            snapshot.epoch != permit.epoch || (epoch != null && epoch != permit.epoch)
        ) {
            if (now >= permit.deadlineNanos || snapshot?.activeNodeId != nodeId || snapshot?.epoch != permit.epoch) {
                permits.remove(companionId)
            }
            return null
        }
        return permit
    }

    fun canOutput(companionId: String, epoch: Long): Boolean = permit(companionId, epoch) != null

    /** Check immediately where a short output action actually begins (after host dispatch). */
    @Synchronized
    fun tryOutput(companionId: String, epoch: Long, action: () -> Unit): Boolean {
        if (permit(companionId, epoch) == null) return false
        action()
        return true
    }

    @Synchronized
    fun pause(companionId: String) {
        paused.add(companionId)
        permits.remove(companionId)
    }

    @Synchronized
    fun resume(companionId: String) {
        paused.remove(companionId)
    }

    @Synchronized
    fun revoke(companionId: String) {
        permits.remove(companionId)
    }

    @Synchronized
    fun revokeAll() {
        permits.clear()
    }

    @Synchronized
    fun healthyClock(): Boolean = readClock() != null

    private fun readClock(): Long? {
        if (clockFault) return null
        val now = clock.nowNanos()
        if (lastClockRead?.let { now < it } == true) {
            faultClock()
            return null
        }
        lastClockRead = now
        return now
    }

    private fun faultClock() {
        clockFault = true
        permits.clear()
    }
}

/** Handles are tied to one epoch; a transferred companion invalidates old handles. */
class OutputHandle internal constructor(val companionId: String, val epoch: Long, private val gate: LeaseGate) {
    fun currentPermit(): OutputPermit? = gate.permit(companionId, epoch)
    fun canOutput(): Boolean = gate.canOutput(companionId, epoch)
    fun tryOutput(action: () -> Unit): Boolean = gate.tryOutput(companionId, epoch, action)
}
