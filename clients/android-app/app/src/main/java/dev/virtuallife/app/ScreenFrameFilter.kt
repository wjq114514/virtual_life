package dev.virtuallife.app

/**
 * Frame-change logic only, so it is deterministic and unit-testable on the JVM.
 *
 * The filter compares an 8x8 average-hash fingerprint against the last *submitted*
 * frame, so a slowly drifting screen accumulates difference until it crosses the
 * threshold. Submissions are bounded by [minUploadIntervalMs] and forced to refresh
 * after [keyframeIntervalMs] even when the screen looks unchanged.
 */
enum class FrameDecision { FIRST, CHANGED, REFRESH, SKIP }

object FrameHash {
    fun hamming(a: Long, b: Long): Int = java.lang.Long.bitCount(a xor b)
}

class ScreenFrameFilter(
    private val changeThreshold: Int = 6,
    minUploadIntervalMs: Long,
    keyframeIntervalMs: Long,
) {
    private val minInterval = minUploadIntervalMs.coerceAtLeast(0)
    private val keyframeInterval = keyframeIntervalMs.coerceAtLeast(minInterval)

    private var lastHash: Long? = null
    private var lastSubmitMs: Long? = null

    val submittedHash: Long? get() = lastHash

    fun changeScore(hash: Long): Double {
        val base = lastHash ?: return 1.0
        return FrameHash.hamming(hash, base) / 64.0
    }

    fun evaluate(hash: Long, nowMs: Long): FrameDecision {
        val base = lastHash
        val lastSubmit = lastSubmitMs
        val intervalSatisfied = lastSubmit == null || nowMs - lastSubmit >= minInterval
        val changed = base == null || FrameHash.hamming(hash, base) >= changeThreshold
        val keyframeDue = lastSubmit == null || nowMs - lastSubmit >= keyframeInterval

        val decision = when {
            base == null -> FrameDecision.FIRST
            changed && intervalSatisfied -> FrameDecision.CHANGED
            keyframeDue -> FrameDecision.REFRESH
            else -> FrameDecision.SKIP
        }
        if (decision != FrameDecision.SKIP) {
            lastHash = hash
            lastSubmitMs = nowMs
        }
        return decision
    }
}
