package dev.virtuallife.app

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.Looper
import android.view.Display
import android.view.Surface
import android.view.WindowManager
import java.io.ByteArrayOutputStream
import kotlin.math.max
import kotlin.math.min

/** One captured frame plus the client-side fingerprint used by [ScreenFrameFilter]. */
data class CapturedFrame(
    val jpeg: ByteArray,
    val width: Int,
    val height: Int,
    val rotationDegrees: Int,
    val fingerprint: Long,
)

interface ScreenCapturer {
    /** Returns the latest frame, or null when the display is off/unavailable. */
    fun capture(): CapturedFrame?

    fun release()
}

/**
 * Single-shot MediaProjection capture. The host owns the user consent flow; this
 * class only mirrors the default display through a [VirtualDisplay] and reads the
 * newest frame on demand. Rotation is applied by the framework mirroring path, so
 * the reported [CapturedFrame.rotationDegrees] follows the current display state.
 */
class MediaProjectionCapturer(
    private val context: Context,
    private val projection: MediaProjection,
    private val targetLongSide: Int,
    private val maxImageBytes: Int,
    private val jpegQuality: Int = 72,
) : ScreenCapturer {
    private var imageReader: ImageReader? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var frameWidth = 0
    private var frameHeight = 0

    private val handler = Handler(Looper.getMainLooper())

    override fun capture(): CapturedFrame? {
        if (imageReader == null) {
            if (!prepare()) return null
        }
        val reader = imageReader ?: return null
        val image = try {
            reader.acquireLatestImage()
        } catch (_: IllegalStateException) {
            return null
        } ?: return null

        val bitmap = try {
            image.use { toBitmap(it) }
        } catch (_: IllegalArgumentException) {
            return null
        } ?: return null

        return try {
            val fingerprint = bitmapToHash(bitmap)
            val jpeg = compress(bitmap)
            CapturedFrame(jpeg, bitmap.width, bitmap.height, rotationDegrees(), fingerprint)
        } finally {
            bitmap.recycle()
        }
    }

    override fun release() {
        virtualDisplay?.release()
        virtualDisplay = null
        imageReader?.close()
        imageReader = null
    }

    private fun prepare(): Boolean {
        val bounds = context.getSystemService(WindowManager::class.java)
            ?.maximumWindowMetrics
            ?.bounds
            ?: return false
        val sourceWidth = bounds.width()
        val sourceHeight = bounds.height()
        if (sourceWidth <= 0 || sourceHeight <= 0) return false

        val longSide = max(sourceWidth, sourceHeight)
        val scale = min(1.0, targetLongSide.toDouble() / longSide)
        frameWidth = (sourceWidth * scale).toInt().coerceAtLeast(2)
        frameHeight = (sourceHeight * scale).toInt().coerceAtLeast(2)

        val densityDpi = context.resources.displayMetrics.densityDpi
        val reader = ImageReader.newInstance(frameWidth, frameHeight, PixelFormat.RGBA_8888, 3)
        val display = projection.createVirtualDisplay(
            "virtual-life-screen",
            frameWidth,
            frameHeight,
            densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface,
            null,
            handler,
        )
        if (display == null) {
            reader.close()
            return false
        }
        imageReader = reader
        virtualDisplay = display
        return true
    }

    private fun toBitmap(image: android.media.Image): Bitmap? {
        val plane = image.planes.firstOrNull() ?: return null
        val buffer = plane.buffer
        val pixelStride = plane.pixelStride
        val rowStride = plane.rowStride
        val rowPadding = rowStride - pixelStride * image.width
        val paddedWidth = image.width + rowPadding / pixelStride
        val bitmap = Bitmap.createBitmap(paddedWidth, image.height, Bitmap.Config.ARGB_8888)
        bitmap.copyPixelsFromBuffer(buffer)
        if (paddedWidth == image.width) return bitmap
        val cropped = Bitmap.createBitmap(bitmap, 0, 0, image.width, image.height)
        bitmap.recycle()
        return cropped
    }

    private fun rotationDegrees(): Int {
        val display = context.getSystemService(DisplayManager::class.java)
            ?.getDisplay(Display.DEFAULT_DISPLAY)
            ?: return 0
        return when (display.rotation) {
            Surface.ROTATION_90 -> 90
            Surface.ROTATION_180 -> 180
            Surface.ROTATION_270 -> 270
            else -> 0
        }
    }

    private fun compress(bitmap: Bitmap): ByteArray {
        var current = bitmap
        var owned = false
        var quality = jpegQuality
        repeat(3) {
            val output = ByteArrayOutputStream()
            current.compress(Bitmap.CompressFormat.JPEG, quality, output)
            val bytes = output.toByteArray()
            if (bytes.size <= maxImageBytes) {
                if (owned) current.recycle()
                return bytes
            }
            quality = (quality - 20).coerceAtLeast(20)
            val scaled = Bitmap.createScaledBitmap(
                current,
                (current.width * 3 / 4).coerceAtLeast(2),
                (current.height * 3 / 4).coerceAtLeast(2),
                true,
            )
            if (owned) current.recycle()
            current = scaled
            owned = true
        }
        val output = ByteArrayOutputStream()
        current.compress(Bitmap.CompressFormat.JPEG, 20, output)
        if (owned) current.recycle()
        return output.toByteArray()
    }

    companion object {
        private const val HASH_SIDE = 8

        fun bitmapToHash(bitmap: Bitmap): Long {
            val scaled = Bitmap.createScaledBitmap(bitmap, HASH_SIDE, HASH_SIDE, true)
            val pixels = IntArray(HASH_SIDE * HASH_SIDE)
            scaled.getPixels(pixels, 0, HASH_SIDE, 0, 0, HASH_SIDE, HASH_SIDE)
            if (scaled !== bitmap) scaled.recycle()
            var total = 0L
            for (pixel in pixels) {
                val r = (pixel shr 16) and 0xFF
                val g = (pixel shr 8) and 0xFF
                val b = pixel and 0xFF
                total += (r * 299 + g * 587 + b * 114) / 1000
            }
            val mean = total / pixels.size
            var hash = 0L
            for ((index, pixel) in pixels.withIndex()) {
                val r = (pixel shr 16) and 0xFF
                val g = (pixel shr 8) and 0xFF
                val b = pixel and 0xFF
                val gray = (r * 299 + g * 587 + b * 114) / 1000
                if (gray >= mean) hash = hash or (1L shl index)
            }
            return hash
        }
    }
}
