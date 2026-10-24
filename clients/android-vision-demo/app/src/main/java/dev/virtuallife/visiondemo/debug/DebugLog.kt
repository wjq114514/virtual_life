package dev.virtuallife.visiondemo.debug

import android.content.Context
import android.os.SystemClock
import android.util.Log
import java.io.File

class DebugLog(context: Context) {
    private val store = JsonlLogStore(File(context.applicationContext.cacheDir, "vision-debug"))

    /** Values are metadata only. Free text, paths, prompts, image bytes and output are dropped. */
    fun event(name: String, fields: Map<String, Any?> = emptyMap()) {
        try {
            val line = store.append(name, fields, SystemClock.elapsedRealtimeNanos())
            Log.i(TAG, line)
        } catch (_: Exception) {
            // Never let logging take down capture/inference, or leak exception messages.
            Log.w(TAG, "{\"event\":\"log_write_failed\"}")
        }
    }

    fun tail(): String = store.tail()
    fun exportFile(): File = store.exportFile()

    companion object { const val TAG = "VLVisionDemo" }
}
