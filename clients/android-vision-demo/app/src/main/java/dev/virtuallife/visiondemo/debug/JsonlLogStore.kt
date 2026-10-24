package dev.virtuallife.visiondemo.debug

import java.io.File

/** Bounded metadata-only JSONL store, independent of Android for JVM testing. */
internal class JsonlLogStore(
    private val directory: File,
    private val maxFileBytes: Int = 256 * 1024,
    private val fileCount: Int = 4,
    private val tailBytes: Int = 32 * 1024,
) {
    init {
        require(maxFileBytes >= 1024 && fileCount in 1..16 && tailBytes > 0)
        check(directory.mkdirs() || directory.isDirectory)
    }

    @Synchronized
    fun append(name: String, fields: Map<String, Any?>, elapsedNs: Long): String {
        val safeName = if (TOKEN.matches(name) && name.length <= 80) name else "invalid_event"
        val parts = mutableListOf("\"event\":${quote(safeName)}", "\"elapsed_ns\":$elapsedNs")
        fields.entries.sortedBy { it.key }.take(32).forEach { (key, value) ->
            if (!TOKEN.matches(key) || key.length > 64 || FORBIDDEN.containsMatchIn(key.lowercase())) return@forEach
            val encoded = when (value) {
                null -> "null"
                is Boolean -> value.toString()
                is Number -> value.toDouble().takeIf { it.isFinite() }?.let { value.toString() }
                is String -> if (key in STRING_FIELDS && value.length <= 80 && TOKEN.matches(value)) quote(value) else null
                else -> null
            }
            if (encoded != null) parts += "${quote(key)}:$encoded"
        }
        val line = "{${parts.joinToString(",")}}\n"
        val bytes = line.toByteArray(Charsets.UTF_8)
        // Single records are bounded too; discard fields if a configured tiny file cannot fit them.
        val record = if (bytes.size <= maxFileBytes) bytes else
            "{\"event\":${quote(safeName)},\"elapsed_ns\":$elapsedNs,\"fields_dropped\":true}\n".toByteArray()
        val current = file(0)
        if (current.length() + record.size > maxFileBytes) {
            file(fileCount - 1).delete()
            for (i in fileCount - 2 downTo 0) {
                val source = file(i)
                if (source.exists()) check(source.renameTo(file(i + 1))) { "log_rotation_failed" }
            }
        }
        current.appendBytes(record)
        return record.toString(Charsets.UTF_8).trimEnd()
    }

    @Synchronized
    fun tail(): String {
        val text = snapshot().toString(Charsets.UTF_8)
        val bytes = text.toByteArray(Charsets.UTF_8)
        if (bytes.size <= tailBytes) return text
        // Keep complete JSONL records only.
        val start = bytes.size - tailBytes
        val newline = (start until bytes.size).firstOrNull { bytes[it] == 10.toByte() } ?: return ""
        return bytes.copyOfRange(newline + 1, bytes.size).toString(Charsets.UTF_8)
    }

    @Synchronized
    fun exportFile(): File {
        // A fixed, separately materialized snapshot prevents export accumulation.
        val target = File(directory, "vision-export.jsonl")
        val temporary = File(directory, "vision-export.tmp")
        temporary.writeBytes(snapshot())
        check(temporary.renameTo(target)) { "log_export_failed" }
        return target
    }

    private fun snapshot(): ByteArray {
        val output = java.io.ByteArrayOutputStream()
        for (i in fileCount - 1 downTo 0) {
            val source = file(i)
            if (source.exists()) source.inputStream().use { it.copyTo(output) }
        }
        return output.toByteArray()
    }

    private fun file(index: Int) = File(directory, "vision-$index.jsonl")

    companion object {
        private val TOKEN = Regex("[A-Za-z0-9_.:-]+")
        private val FORBIDDEN = Regex("prompt|text|content|image|png|path|uri|token_delta|response|output|message|exception|stack")
        private val STRING_FIELDS = setOf("reason", "stage", "backend", "status", "error_type", "mode")
        private fun quote(value: String) = "\"$value\"" // Only TOKEN-validated ASCII reaches here.
    }
}
