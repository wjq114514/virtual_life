package dev.virtuallife.visiondemo.capture

import kotlin.math.abs

data class PolicyDecision(val accepted: Boolean, val reason: String, val changeScore: Double)

/** Android-free state machine; samples are equally spaced luminance values in 0..255. */
internal class FrameDecisionCore(
    private val minIntervalMs: Long = 2500,
    private val maxIntervalMs: Long = 15000,
    private val changeThreshold: Double = 0.04,
) {
    init {
        require(minIntervalMs >= 0 && maxIntervalMs >= minIntervalMs)
        require(changeThreshold.isFinite() && changeThreshold in 0.0..1.0)
    }

    private var baseline: IntArray? = null
    private var acceptedAt: Long? = null

    @Synchronized
    fun evaluate(samples: IntArray, nowMs: Long, force: Boolean = false): PolicyDecision {
        require(samples.isNotEmpty() && samples.all { it in 0..255 })
        require(nowMs >= 0)
        val previous = baseline
        require(previous == null || previous.size == samples.size)
        val score = if (previous == null) 1.0 else
            samples.indices.sumOf { abs(samples[it] - previous[it]).toDouble() } / (255 * samples.size)
        // Uniform near-black/white is an explicit heuristic error, not FLAG_SECURE detection.
        val low = samples.minOrNull()!!
        val high = samples.maxOrNull()!!
        if (high - low <= 2 && (high <= 8 || low >= 247)) {
            return PolicyDecision(false, "blank_frame", score)
        }
        val elapsed = acceptedAt?.let { nowMs - it }
        val reason = when {
            force -> "forced"
            previous == null -> "first_frame"
            elapsed!! < 0 -> "clock_regressed"
            elapsed < minIntervalMs -> "min_interval"
            elapsed >= maxIntervalMs -> "max_interval"
            score >= changeThreshold -> "changed"
            else -> "unchanged"
        }
        val accepted = reason in setOf("forced", "first_frame", "max_interval", "changed")
        if (accepted) {
            baseline = samples.copyOf()
            acceptedAt = nowMs
        }
        return PolicyDecision(accepted, reason, score)
    }

    @Synchronized
    fun reset() {
        baseline = null
        acceptedAt = null
    }
}
