package dev.virtuallife.visiondemo.inference

import android.content.Context
import android.net.Uri
import android.os.Looper
import android.provider.DocumentsContract
import dev.virtuallife.visiondemo.debug.DebugLog
import java.io.File
import java.util.UUID

class ModelStore(context: Context, private val log: DebugLog) {
    private val app = context.applicationContext

    fun defaultModelDirectory(): File {
        val external = checkNotNull(app.getExternalFilesDir(null)) { "External app storage unavailable" }
        return File(external, "models").also {
            check(it.isDirectory || it.mkdirs()) { "Cannot create model directory" }
        }
    }

    fun resolveConfigPath(input: String): File {
        require(input.isNotBlank()) { "Select a model directory or config.json" }
        val selected = File(input).canonicalFile
        val config = if (selected.isDirectory) File(selected, "config.json") else selected
        require(config.name == "config.json" && config.isFile && config.canRead()) {
            "Readable config.json not found"
        }
        return config.canonicalFile
    }

    /** Caller supplies the SAF permission and invokes on an IO worker. Copies, never moves. */
    fun importTree(uri: Uri): File {
        check(Looper.myLooper() != Looper.getMainLooper()) { "Import models on an IO worker" }
        require(uri.scheme == "content" && DocumentsContract.isTreeUri(uri)) { "Expected a SAF directory" }
        val root = File(defaultModelDirectory(), "import-${UUID.randomUUID()}")
        check(root.mkdir()) { "Cannot create import directory" }
        var files = 0
        var copiedBytes = 0L
        val maxBytes = 12L * 1024 * 1024 * 1024
        val buffer = ByteArray(1024 * 1024)
        val configs = mutableListOf<File>()
        val visited = mutableSetOf<String>()
        fun copyDirectory(id: String, destination: File, depth: Int) {
            check(depth <= 32 && visited.add(id)) { "Cyclic or excessively deep model directory" }
            val children = DocumentsContract.buildChildDocumentsUriUsingTree(uri, id)
            val projection = arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME, DocumentsContract.Document.COLUMN_MIME_TYPE)
            val cursor = checkNotNull(app.contentResolver.query(children, projection, null, null, null)) {
                "Cannot list selected directory"
            }
            cursor.use {
                val names = mutableSetOf<String>()
                while (it.moveToNext()) {
                    check(!Thread.currentThread().isInterrupted) { "Model import interrupted" }
                    val childId = it.getString(0)
                    val name = it.getString(1)
                    require(!name.isNullOrEmpty() && name != "." && name != ".." &&
                        name.none { c -> c == '/' || c == '\\' || c == '\u0000' } && names.add(name)) {
                        "Unsafe or duplicate document name"
                    }
                    check(++files <= 10000) { "Too many model files" }
                    val target = File(destination, name).canonicalFile
                    require(target.toPath().startsWith(root.canonicalFile.toPath())) { "Path escapes import directory" }
                    if (it.getString(2) == DocumentsContract.Document.MIME_TYPE_DIR) {
                        check(target.mkdir()) { "Cannot create imported directory" }
                        copyDirectory(childId, target, depth + 1)
                    } else {
                        val source = DocumentsContract.buildDocumentUriUsingTree(uri, childId)
                        checkNotNull(app.contentResolver.openInputStream(source)) { "Cannot read model file" }.use { sourceStream ->
                            target.outputStream().use { output ->
                                while (true) {
                                    check(!Thread.currentThread().isInterrupted) { "Model import interrupted" }
                                    val count = sourceStream.read(buffer)
                                    if (count < 0) break
                                    copiedBytes += count
                                    check(copiedBytes <= maxBytes) { "Model import exceeds the 12 GiB limit" }
                                    check(root.usableSpace >= count + 16L * 1024 * 1024) {
                                        "Not enough free storage to import the model"
                                    }
                                    output.write(buffer, 0, count)
                                }
                            }
                        }
                        if (name == "config.json") configs.add(target)
                    }
                }
            }
        }
        try {
            copyDirectory(DocumentsContract.getTreeDocumentId(uri), root, 0)
            val config = File(root, "config.json").takeIf { it.isFile }
                ?: configs.singleOrNull()
                ?: error("Select a directory containing one model config.json")
            val resolved = resolveConfigPath(config.path)
            log.event("model_import_complete", mapOf("file_count" to files))
            return resolved
        } catch (failure: Throwable) {
            root.deleteRecursively()
            log.event("model_import_failed", mapOf("error_type" to failure.javaClass.simpleName))
            throw failure
        }
    }
}
