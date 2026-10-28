package dev.virtuallife.chat

import dev.virtuallife.node.ApiException
import dev.virtuallife.node.DialogueHistoryItem
import dev.virtuallife.node.DialogueResult
import dev.virtuallife.node.DialogueStatus
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** Everything the UI renders; there is no separate view model layer. */
data class ChatUiState(
    val connected: Boolean = false,
    val status: String = "未连接",
    val sessions: List<ChatSession> = emptyList(),
    val activeSessionId: String = "",
    val streamingText: String = "",
    val busy: Boolean = false,
    val error: String? = null,
    val historyLoading: Boolean = false,
) {
    val active: ChatSession?
        get() = sessions.firstOrNull { it.id == activeSessionId } ?: sessions.lastOrNull()
}

/**
 * Turns user input into a durable request and keeps the on-device session in sync with it.
 *
 * A turn inserts a user bubble plus a pending placeholder, then waits for the durable result.
 * Only that result is ever committed to history: streaming deltas are display-only, so a
 * silent, discarded or dropped reply never looks like something the companion said.
 */
class ChatController(
    val config: ChatConfig,
    private val client: ChatClient,
    private val storage: ChatStorage,
    private val scope: CoroutineScope,
) {
    private var state: ChatState = storage.load()

    private var turnJob: Job? = null

    private var turnStopped = false

    private val stateFlow = MutableStateFlow(ChatUiState(sessions = state.sessions, activeSessionId = state.activeSessionId))

    val ui: StateFlow<ChatUiState> = stateFlow

    init {
        client.onStatus = { message ->
            stateFlow.update { it.copy(connected = client.running, status = message) }
        }
    }

    fun connect() {
        stateFlow.update { it.copy(status = "连接中…", error = null) }
        client.connect()
    }

    fun close() {
        turnJob?.cancel()
        turnJob = null
        client.close()
        stateFlow.update { it.copy(connected = false, busy = false, status = "已断开", streamingText = "") }
    }

    fun send(text: String) {
        val content = text.trim()
        val session = state.active
        if (content.isBlank() || session == null || stateFlow.value.busy) return

        val commandId = UUID.randomUUID().toString()
        val userMessage = ChatMessage(role = ChatRole.USER, content = content, commandId = commandId)
        val placeholder = ChatMessage(
            role = ChatRole.ASSISTANT,
            commandId = commandId,
            state = ChatMessageState.PENDING,
        )
        mutateSession(session.id) { current ->
            val title = if (current.title == "新会话") content.take(16) else current.title
            current.copy(title = title, messages = current.messages + userMessage + placeholder)
        }
        val window = ChatRequests.history(session.messages + userMessage)

        turnStopped = false
        stateFlow.update { it.copy(busy = true, error = null, streamingText = "") }
        turnJob = scope.launch {
            val startedAt = System.currentTimeMillis()
            try {
                val result = client.send(content, window) { delta ->
                    stateFlow.update { it.copy(streamingText = delta) }
                }
                commit(result, placeholder.id, startedAt)
            } catch (error: CancellationException) {
                if (turnStopped) {
                    markStalled(placeholder.id, "已停止：服务器侧可能仍在生成，可重新拉取历史")
                }
                throw error
            } catch (error: Throwable) {
                markStalled(placeholder.id, describe(error))
            } finally {
                turnStopped = false
                stateFlow.update { it.copy(busy = false, streamingText = "") }
            }
        }
    }

    /** Aborts the local wait; the server command stays durable and is never replayed as a retry. */
    fun stopTurn() {
        turnStopped = true
        turnJob?.cancel()
    }

    fun clearError() = stateFlow.update { it.copy(error = null) }

    fun newSession() {
        mutate { current ->
            val session = ChatSession()
            current.copy(sessions = current.sessions + session, activeSessionId = session.id)
        }
    }

    fun renameSession(sessionId: String, title: String) {
        mutate { current ->
            val trimmed = title.trim()
            if (trimmed.isEmpty()) current
            else current.copy(sessions = current.sessions.map {
                if (it.id == sessionId) it.copy(title = trimmed.take(40)) else it
            })
        }
    }

    fun selectSession(sessionId: String) {
        mutate { current ->
            if (current.activeSessionId == sessionId || current.sessions.none { it.id == sessionId }) current
            else current.copy(activeSessionId = sessionId)
        }
    }

    fun deleteSession(sessionId: String) {
        mutate { current ->
            val sessions = current.sessions.filterNot { it.id == sessionId }
            if (sessions.isEmpty()) {
                val fresh = ChatSession()
                ChatState(sessions = listOf(fresh), activeSessionId = fresh.id)
            } else {
                val activeSessionId = if (current.activeSessionId == sessionId) sessions.last().id else current.activeSessionId
                current.copy(sessions = sessions, activeSessionId = activeSessionId)
            }
        }
    }

    /** Pulls the durable server-side history into the active session, oldest first. */
    fun loadHistory(reset: Boolean = false) {
        val session = state.active ?: return
        if (stateFlow.value.historyLoading || stateFlow.value.busy) return
        stateFlow.update { it.copy(historyLoading = true, error = null) }
        scope.launch {
            try {
                val items = ArrayList<DialogueHistoryItem>()
                var cursor: Long? = null
                var pages = 0
                do {
                    val page = client.history(beforeSequence = cursor, limit = 20)
                    items += page.items
                    cursor = page.nextBeforeSequence
                    pages += 1
                    if (!page.hasMore || cursor == null || pages >= 20) break
                } while (true)

                val loaded = items
                    .flatMap { item ->
                        buildList {
                            if (item.userMessage.isNotBlank()) {
                                add(ChatMessage(role = ChatRole.USER, content = item.userMessage, commandId = item.commandId, createdAt = epochMillis(item.occurredAt)))
                            }
                            if (item.assistantMessage.isNotBlank()) {
                                add(ChatMessage(role = ChatRole.ASSISTANT, content = item.assistantMessage, commandId = item.commandId, createdAt = epochMillis(item.occurredAt)))
                            }
                        }
                    }
                    .asReversed()

                val merged = if (reset) {
                    loaded
                } else {
                    val known = session.messages.mapNotNull { it.commandId }.toSet()
                    session.messages + loaded.filter { it.commandId == null || it.commandId !in known }
                }
                mutateSession(session.id) { it.copy(messages = merged) }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Throwable) {
                stateFlow.update { it.copy(error = "读取历史失败: ${describe(error)}") }
            } finally {
                stateFlow.update { it.copy(historyLoading = false) }
            }
        }
    }

    private fun commit(result: DialogueResult, placeholderId: String, startedAt: Long) {
        val latency = System.currentTimeMillis() - startedAt
        val usage = result.usage
        when {
            result.status == DialogueStatus.COMPLETED && !result.assistantMessage.orEmpty().isBlank() -> {
                // Gate at presentation time: the reply is only shown if this device still owns
                // the companion and holds a live lease for the epoch that produced it.
                val presented = client.present(result) {
                    mutateMessage(placeholderId) {
                        it.copy(
                            content = result.assistantMessage.orEmpty(),
                            state = ChatMessageState.SENT,
                            latencyMs = latency,
                            inputTokens = usage?.inputTokens,
                            outputTokens = usage?.outputTokens,
                        )
                    }
                }
                if (!presented) {
                    markStalled(placeholderId, "输出租约已失效，本次回复未展示")
                }
            }
            result.status == DialogueStatus.SILENT -> mutateMessage(placeholderId) {
                it.copy(content = "（这次选择了安静）", state = ChatMessageState.SENT, latencyMs = latency)
            }
            else -> mutateMessage(placeholderId) {
                it.copy(content = result.discardReason ?: "（这次没有回复）", state = ChatMessageState.SENT, latencyMs = latency)
            }
        }
    }

    private fun markStalled(messageId: String, message: String) {
        mutateMessage(messageId) { it.copy(state = ChatMessageState.FAILED, error = message) }
        stateFlow.update { it.copy(error = message) }
    }

    private fun mutate(transform: (ChatState) -> ChatState) {
        state = transform(state)
        publish()
    }

    private fun mutateSession(sessionId: String, transform: (ChatSession) -> ChatSession) {
        mutate { current ->
            current.copy(sessions = current.sessions.map { if (it.id == sessionId) transform(it) else it })
        }
    }

    private fun mutateMessage(messageId: String, transform: (ChatMessage) -> ChatMessage) {
        val session = state.active ?: return
        mutateSession(session.id) { current ->
            current.copy(messages = current.messages.map { if (it.id == messageId) transform(it) else it })
        }
    }

    private fun publish() {
        stateFlow.update { it.copy(sessions = state.sessions, activeSessionId = state.activeSessionId) }
        storage.save(state)
    }

    private fun describe(error: Throwable): String = when (error) {
        is ApiException -> "HTTP ${error.statusCode} (${error.code})"
        else -> error.message ?: error.javaClass.simpleName
    }

    private fun epochMillis(iso: String): Long =
        runCatching { Instant.parse(iso).toEpochMilli() }.getOrDefault(System.currentTimeMillis())
}
