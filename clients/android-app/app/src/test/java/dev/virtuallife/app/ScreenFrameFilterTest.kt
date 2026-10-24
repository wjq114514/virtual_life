package dev.virtuallife.app

import org.junit.Assert.assertEquals
import org.junit.Test

class ScreenFrameFilterTest {
    @Test
    fun firstFrameIsAlwaysSubmitted() {
        val filter = ScreenFrameFilter(minUploadIntervalMs = 1000, keyframeIntervalMs = 15000)
        assertEquals(FrameDecision.FIRST, filter.evaluate(0x0L, 0))
    }

    @Test
    fun nearIdenticalFrameIsSkipped() {
        val filter = ScreenFrameFilter(minUploadIntervalMs = 1000, keyframeIntervalMs = 15000)
        filter.evaluate(0L, 0)
        assertEquals(FrameDecision.SKIP, filter.evaluate(0b1L, 500))
    }

    @Test
    fun changeInsideMinIntervalIsSkipped() {
        val filter = ScreenFrameFilter(minUploadIntervalMs = 1000, keyframeIntervalMs = 15000)
        filter.evaluate(0L, 0)
        assertEquals(FrameDecision.SKIP, filter.evaluate(-1L, 500))
    }

    @Test
    fun changeAfterMinIntervalIsSubmitted() {
        val filter = ScreenFrameFilter(minUploadIntervalMs = 1000, keyframeIntervalMs = 15000)
        filter.evaluate(0L, 0)
        assertEquals(FrameDecision.CHANGED, filter.evaluate(-1L, 1500))
    }

    @Test
    fun unchangedScreenRefreshesAfterKeyframe() {
        val filter = ScreenFrameFilter(minUploadIntervalMs = 1000, keyframeIntervalMs = 15000)
        filter.evaluate(0L, 0)
        assertEquals(FrameDecision.SKIP, filter.evaluate(0L, 5000))
        assertEquals(FrameDecision.REFRESH, filter.evaluate(0L, 16000))
    }

    @Test
    fun slowDriftAccumulatesUntilThreshold() {
        val filter = ScreenFrameFilter(
            changeThreshold = 6,
            minUploadIntervalMs = 0,
            keyframeIntervalMs = 1_000_000,
        )
        filter.evaluate(0L, 0)
        var now = 100L
        repeat(5) { index ->
            assertEquals(FrameDecision.SKIP, filter.evaluate(1L shl index, now))
            now += 100
        }
        assertEquals(FrameDecision.CHANGED, filter.evaluate((1L shl 6) - 1L, now))
    }

    @Test
    fun changeScoreReportsFractionOfBits() {
        val filter = ScreenFrameFilter(minUploadIntervalMs = 0, keyframeIntervalMs = 1_000_000)
        filter.evaluate(0L, 0)
        assertEquals(0.5, filter.changeScore(0xFFFFFFFFL), 0.0001)
    }
}
