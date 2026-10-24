package dev.virtuallife.visiondemo.capture

import android.graphics.Bitmap

class CapturePolicy(
    minIntervalMs: Long = 2500,
    maxIntervalMs: Long = 15000,
    changeThreshold: Double = 0.04,
) {
    private val core = FrameDecisionCore(minIntervalMs, maxIntervalMs, changeThreshold)

    /** Call only when inference is idle: acceptance commits the comparison baseline. */
    fun evaluate(bitmap: Bitmap, nowMs: Long, force: Boolean = false): PolicyDecision {
        require(!bitmap.isRecycled && bitmap.width > 0 && bitmap.height > 0)
        val reduced = Bitmap.createScaledBitmap(bitmap, 32, 32, true)
        try {
            val pixels = IntArray(32 * 32)
            reduced.getPixels(pixels, 0, 32, 0, 0, 32, 32)
            for (i in pixels.indices) {
                val color = pixels[i]
                pixels[i] = (77 * ((color ushr 16) and 255) +
                    150 * ((color ushr 8) and 255) + 29 * (color and 255)) ushr 8
            }
            return core.evaluate(pixels, nowMs, force)
        } finally {
            if (reduced !== bitmap) reduced.recycle()
        }
    }

    fun reset() = core.reset()
}
