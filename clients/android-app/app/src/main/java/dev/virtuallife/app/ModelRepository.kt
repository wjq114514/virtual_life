package dev.virtuallife.app

import android.content.Context
import java.io.File

/**
 * The ASR weights are never bundled into the APK. The host either pushes them into
 * the app's external files directory (adb push) or copies them with a file picker.
 */
object ModelRepository {
    const val MODEL_ID = "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"

    private val REQUIRED = listOf(
        "encoder-epoch-99-avg-1.onnx",
        "decoder-epoch-99-avg-1.onnx",
        "joiner-epoch-99-avg-1.int8.onnx",
        "tokens.txt",
    )

    fun internalDir(context: Context): File = File(context.filesDir, "asr/$MODEL_ID")

    fun externalDir(context: Context): File =
        File(context.getExternalFilesDir(null) ?: context.filesDir, "asr/$MODEL_ID")

    /** Prefers the internal copy; falls back to the adb-pushable external directory. */
    fun resolve(context: Context): File? {
        val candidates = listOf(internalDir(context), externalDir(context))
        return candidates.firstOrNull(::isReady)
    }

    fun isReady(dir: File): Boolean = REQUIRED.all { File(dir, it).isFile }

    fun missing(dir: File): List<String> = REQUIRED.filterNot { File(dir, it).isFile }

    /** Copies a prepared external directory into app-private storage. Returns copied count. */
    fun adoptExternal(context: Context): Int {
        val source = externalDir(context)
        if (!isReady(source)) return 0
        val target = internalDir(context)
        target.mkdirs()
        var copied = 0
        for (name in REQUIRED) {
            val from = File(source, name)
            val to = File(target, name)
            if (to.isFile && to.length() == from.length()) continue
            from.copyTo(to, overwrite = true)
            copied++
        }
        return copied
    }
}
