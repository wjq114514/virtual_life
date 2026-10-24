package dev.virtuallife.visiondemo.inference

import android.content.Context
import android.os.Looper
import dev.virtuallife.visiondemo.debug.DebugLog
import org.json.JSONObject
import java.io.Closeable
import java.io.File

data class InferenceResult(
    val text: String,
    val firstTokenMs: Double?,
    val totalMs: Double,
    val visionMs: Double?,
    val prefillMs: Double?,
    val decodeMs: Double?,
)

/** Blocking worker-thread API. cancel() is the only operation intended to race inference. */
class MnnVisionEngine(context: Context, private val log: DebugLog) : Closeable {
    private val app = context.applicationContext
    private val store = ModelStore(app, log)
    private val operation = Any()
    @Volatile private var handle = 0L
    val isLoaded: Boolean get() = handle != 0L

    fun load(configPath: String, backend: String = "cpu", threads: Int = 4) {
        workerThread()
        require(backend == "cpu" || backend == "opencl") { "Backend must be cpu or opencl" }
        require(threads in 1..64) { "Threads must be between 1 and 64" }
        synchronized(operation) {
            val config = store.resolveConfigPath(configPath)
            val normalized = validateVisionModel(config)
            normalized.put("backend_type", backend).put("thread_num", threads)
            val processor = normalized.optJSONObject("mllm") ?: JSONObject()
            processor.put("backend_type", backend).put("thread_num", threads)
            normalized.put("mllm", processor)
            val nativeCache = File(app.cacheDir, "mnn").also {
                check(it.isDirectory || it.mkdirs()) { "Cannot create native cache directory" }
            }
            normalized.put("tmp_path", nativeCache.path + "/")
            // A private normalized config lets MNN resolve relative base_dir consistently,
            // without modifying the supplied model directory or its metadata.
            val runtimeConfig = File.createTempFile("mnn-runtime-", ".json", app.cacheDir)
            val runtimeMetadata = File.createTempFile("mnn-meta-", ".json", app.cacheDir)
            val replacement = try {
                runtimeMetadata.writeText("{}")
                normalized.put("llm_config", File(normalized.getString("base_dir")).toPath()
                    .relativize(runtimeMetadata.toPath()).toString())
                runtimeConfig.writeText(normalized.toString())
                nativeLoad(runtimeConfig.path, backend, threads)
            } finally {
                runtimeConfig.delete()
                runtimeMetadata.delete()
            }
            val old = handle
            handle = replacement
            if (old != 0L) nativeDestroy(old)
            log.event("model_loaded", mapOf("backend" to backend, "threads" to threads))
        }
    }

    fun analyze(imagePath: String, prompt: String, maxTokens: Int = 128,
                shouldContinue: () -> Boolean = { true },
                onDelta: (String) -> Unit): InferenceResult {
        workerThread()
        require(maxTokens in 1..8192) { "maxTokens must be between 1 and 8192" }
        require(!prompt.contains(Regex("</?(img|audio)(>|\\s)"))) {
            "Prompt must not contain MNN media tags"
        }
        val image = File(imagePath).canonicalFile
        require(image.isFile && image.canRead()) { "Image is not readable" }
        synchronized(operation) {
            check(isLoaded) { "Load a visual MNN model first" }
            val text = StringBuilder()
            val metrics = nativeAnalyze(handle, image.path, prompt, maxTokens, object : DeltaCallback {
                override fun isCancelled(): Boolean = !shouldContinue()
                override fun onDelta(bytes: ByteArray) {
                    val delta = bytes.toString(Charsets.UTF_8)
                    text.append(delta)
                    onDelta(delta)
                }
            })
            val result = InferenceResult(text.toString(), metrics[0].takeIf { it >= 0 },
                metrics[1], metrics[2], metrics[3], metrics[4])
            log.event("inference_complete", mapOf("first_visible_ms" to result.firstTokenMs,
                "total_ms" to result.totalMs, "vision_ms" to result.visionMs,
                "prefill_ms" to result.prefillMs, "decode_ms" to result.decodeMs))
            return result
        }
    }

    fun cancel() {
        val current = handle
        if (current != 0L) nativeCancel(current)
    }

    override fun close() {
        cancel()
        synchronized(operation) {
            val current = handle
            handle = 0L
            if (current != 0L) nativeDestroy(current)
        }
    }

    private fun workerThread() {
        check(Looper.myLooper() != Looper.getMainLooper()) { "Run model operations on an IO worker" }
    }

    private fun readMetadata(file: File): JSONObject {
        require(file.isFile && file.length() <= 4L * 1024 * 1024) {
            "Missing or oversized model JSON metadata"
        }
        val bytes = file.inputStream().use { input ->
            val output = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                require(output.size() + count <= 4 * 1024 * 1024) { "Oversized model JSON metadata" }
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        }
        return JSONObject(bytes.toString(Charsets.UTF_8))
    }

    private fun validateVisionModel(file: File): JSONObject {
        val config = readMetadata(file)
        val suppliedBase = config.optString("base_dir", "")
        val base = if (suppliedBase.isEmpty()) file.parentFile!! else {
            File(suppliedBase).takeIf { it.isAbsolute } ?: File(file.parentFile, suppliedBase)
        }.canonicalFile
        fun asset(name: String): File = File(name).takeIf { it.isAbsolute }
            ?: File(base, name)
        val metadata = asset(config.optString("llm_config", "llm_config.json"))
        val info = if (metadata.isFile) readMetadata(metadata) else JSONObject()
        val merged = JSONObject(config.toString())
        info.keys().forEach { key -> merged.put(key, info.get(key)) }
        require(merged.optBoolean("is_visual", false)) { "This is not a visual MNN-LLM model" }
        fun required(key: String, fallback: String): File {
            val name = merged.optString(key, fallback)
            // MNN concatenates base_dir with asset names. Keep its input relative.
            val path = asset(name).canonicalFile
            require(path.isFile && path.canRead()) { "Missing model asset: $key (${path.name})" }
            merged.put(key, base.toPath().relativize(path.toPath()).toString())
            return path
        }
        required("llm_model", "llm.mnn")
        required("llm_weight", "llm.mnn.weight")
        required("tokenizer_file", "tokenizer.txt")
        val visual = required("visual_model", "visual.mnn")
        // Inline-weight graphs are valid; JNI checks actual external ranges when present.
        nativeValidateVisualGraph(visual.path)
        val tie = merged.optJSONArray("tie_embeddings")
        val tied = tie != null && tie.length() >= 5 && tie.optLong(0) != 0L
        if (!tied && !merged.optJSONObject("tie_embeddings").let {
                it != null && it.optLong("weight_offset", 0) != 0L
            }) required("embedding_file", "embeddings_bf16.bin")
        // Metadata is already merged; avoid MNN merging the original base_dir a second time.
        merged.put("base_dir", base.path + "/")
        return merged
    }

    private interface DeltaCallback {
        fun isCancelled(): Boolean
        fun onDelta(bytes: ByteArray)
    }
    private external fun nativeValidateVisualGraph(path: String)
    private external fun nativeLoad(config: String, backend: String, threads: Int): Long
    private external fun nativeAnalyze(handle: Long, image: String, prompt: String,
                                      maxTokens: Int, callback: DeltaCallback): DoubleArray
    private external fun nativeCancel(handle: Long)
    private external fun nativeDestroy(handle: Long)

    companion object { init { System.loadLibrary("vision_mnn") } }
}
