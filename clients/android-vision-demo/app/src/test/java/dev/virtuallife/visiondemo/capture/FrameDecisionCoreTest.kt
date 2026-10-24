package dev.virtuallife.visiondemo.capture

import org.junit.Assert.*
import org.junit.Test

class FrameDecisionCoreTest {
    private fun frame(level: Int) = intArrayOf(level, level + 10, level, level + 10)

    @Test fun rejectedFramesDoNotConsumeBaselineOrRefreshDeadline() {
        val policy = FrameDecisionCore()
        assertTrue(policy.evaluate(frame(50), 0).accepted)
        assertEquals("min_interval", policy.evaluate(frame(200), 1000).reason)
        assertEquals("unchanged", policy.evaluate(frame(50), 3000).reason)
        assertEquals("max_interval", policy.evaluate(frame(50), 15000).reason)
    }

    @Test fun acceptedFrameIsCopiedAndForceOverridesInterval() {
        val policy = FrameDecisionCore()
        val original = frame(50)
        policy.evaluate(original, 0)
        original.fill(220)
        assertEquals("unchanged", policy.evaluate(frame(50), 3000).reason)
        assertEquals("forced", policy.evaluate(frame(180), 3100, true).reason)
        assertEquals("unchanged", policy.evaluate(frame(180), 6000).reason)
    }

    @Test fun blankHeuristicDoesNotCommitEvenWhenForced() {
        val policy = FrameDecisionCore()
        assertEquals("blank_frame", policy.evaluate(IntArray(4), 0, true).reason)
        assertEquals("blank_frame", policy.evaluate(IntArray(4) { 255 }, 0).reason)
        assertEquals("first_frame", policy.evaluate(frame(50), 1).reason)
        assertEquals("changed", policy.evaluate(frame(180), 2501).reason)
    }

    @Test fun resetAndClockRegressionAreExplicit() {
        val policy = FrameDecisionCore()
        policy.evaluate(frame(50), 5000)
        assertEquals("clock_regressed", policy.evaluate(frame(180), 4000).reason)
        policy.reset()
        assertEquals("first_frame", policy.evaluate(frame(50), 0).reason)
    }

    @Test(expected = IllegalArgumentException::class)
    fun invalidIntervalsFailEarly() { FrameDecisionCore(5000, 1000) }
}
