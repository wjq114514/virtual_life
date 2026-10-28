package dev.virtuallife.chat

import android.content.Context
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import okhttp3.OkHttpClient

/**
 * Process-wide owner of the chat session so the node registration, presentation and output
 * lease survive configuration changes instead of being torn down and re-claimed on every
 * orientation change. A different [ChatConfig] rebuilds the session exactly once.
 */
object ChatCore {
    private val lock = Any()
    private val http = OkHttpClient()

    @Volatile private var controller: ChatController? = null
    @Volatile private var job: Job? = null
    @Volatile private var activeConfig: ChatConfig? = null

    fun current(): ChatController? = controller

    fun ensure(context: Context, config: ChatConfig): ChatController = synchronized(lock) {
        val existing = controller
        if (existing != null && config == activeConfig) {
            existing
        } else {
            existing?.close()
            val scope = CoroutineScope(SupervisorJob())
            val client = ChatClient(config, http)
            val storage = ChatStorage(File(context.filesDir, "chat-state.json"))
            val next = ChatController(config, client, storage, scope)
            controller = next
            activeConfig = config
            this.job = scope.coroutineContext[Job]
            next
        }
    }

    fun close() = synchronized(lock) {
        controller?.close()
        controller = null
        activeConfig = null
        job?.cancel()
        job = null
    }
}
