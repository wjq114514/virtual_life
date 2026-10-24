package dev.virtuallife.node

import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/** Implement save atomically and durably. Failure must throw so replay cannot skip work. */
interface CursorStore {
    suspend fun load(nodeId: String): Long
    suspend fun save(nodeId: String, sequence: Long)
}

/** For tests or an intentionally ephemeral session, never the production default. */
class InMemoryCursorStore : CursorStore {
    private val mutex = Mutex()
    private val cursors = mutableMapOf<String, Long>()
    override suspend fun load(nodeId: String): Long = mutex.withLock { cursors[nodeId] ?: 0 }
    override suspend fun save(nodeId: String, sequence: Long) {
        require(sequence >= 0)
        mutex.withLock { cursors[nodeId] = maxOf(cursors[nodeId] ?: 0, sequence) }
    }
}

/**
 * Process-restart cursor storage for one SDK session per node. Files are fsynced before
 * atomic same-directory rename. For stronger device power-loss guarantees or multiple
 * writers, provide the host's transactional database through [CursorStore].
 */
class FileCursorStore(private val directory: File) : CursorStore {
    private val mutex = Mutex()

    override suspend fun load(nodeId: String): Long = mutex.withLock {
        withContext(Dispatchers.IO) { read(file(nodeId)) }
    }

    override suspend fun save(nodeId: String, sequence: Long) {
        require(sequence >= 0)
        mutex.withLock {
            withContext(Dispatchers.IO) {
                if (!directory.isDirectory && !directory.mkdirs()) throw IOException("Cannot create cursor directory")
                val target = file(nodeId)
                if (sequence <= read(target)) return@withContext
                val temp = File(directory, ".${target.name}.${UUID.randomUUID()}.tmp")
                try {
                    FileOutputStream(temp).use { stream ->
                        stream.write(sequence.toString().toByteArray(Charsets.UTF_8))
                        stream.fd.sync()
                    }
                    if (!temp.renameTo(target)) throw IOException("Cannot replace node cursor atomically")
                } finally {
                    temp.delete()
                }
            }
        }
    }

    private fun file(nodeId: String): File {
        require(nodeId.isNotBlank())
        val digest = MessageDigest.getInstance("SHA-256").digest(nodeId.toByteArray(Charsets.UTF_8))
        val name = digest.joinToString("") { "%02x".format(it.toInt() and 0xff) }
        return File(directory, "$name.cursor")
    }

    private fun read(file: File): Long {
        if (!file.exists()) return 0
        val value = file.readText(Charsets.UTF_8).trim().toLongOrNull()
        if (value == null || value < 0) throw IOException("Stored cursor is invalid")
        return value
    }
}

/**
 * Event and scan-cursor frames must be delivered in stream order. A handler or store
 * failure propagates; the caller closes that stream and reconnects at [cursor].
 */
class ReplayEventRouter(
    private val nodeId: String,
    private val store: CursorStore,
    private val handler: suspend (ProtocolEvent) -> Unit,
) {
    private val mutex = Mutex()
    private var loaded = false
    private var sequence = 0L

    suspend fun cursor(): Long = mutex.withLock { load(); sequence }

    suspend fun route(event: ProtocolEvent) = mutex.withLock {
        load()
        require(event.sequence > 0) { "Event sequence must be positive" }
        if (event.sequence <= sequence) return@withLock
        if (event.recipientNodeId == null || event.recipientNodeId == nodeId) {
            require(event.protocolVersion == "1.0") { "Unsupported event protocol version" }
            handler(event)
        }
        persist(event.sequence)
    }

    /** Safe only after every preceding event frame has completed successfully. */
    suspend fun scanCursor(nextSequence: Long) = mutex.withLock {
        load()
        require(nextSequence >= 0)
        if (nextSequence > sequence) persist(nextSequence)
    }

    suspend fun replayPage(page: EventPage) {
        require(page.nextSequence >= 0)
        var previous = -1L
        for (event in page.events) {
            require(event.sequence > previous && event.sequence <= page.nextSequence) { "Invalid event page order" }
            previous = event.sequence
        }
        for (event in page.events) route(event)
        scanCursor(page.nextSequence)
    }

    private suspend fun load() {
        if (!loaded) {
            sequence = store.load(nodeId)
            require(sequence >= 0) { "Stored cursor must be nonnegative" }
            loaded = true
        }
    }

    private suspend fun persist(next: Long) {
        store.save(nodeId, next)
        sequence = next
    }
}
