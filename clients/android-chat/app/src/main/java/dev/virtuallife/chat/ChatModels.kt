package dev.virtuallife.chat

import kotlinx.serialization.Serializable
import java.util.UUID

@Serializable
enum class ChatRole { USER, ASSISTANT }

/** Whether the bubble is a settled message or a placeholder still waiting on the model. */
@Serializable
enum class ChatMessageState { SENT, PENDING, FAILED }

/**
 * One persisted chat bubble. [commandId] is kept so a redelivery can be told apart from a
 * retry, and it never leaves the device in a URL.
 */
@Serializable
data class ChatMessage(
    val id: String = UUID.randomUUID().toString(),
    val role: ChatRole,
    val content: String = "",
    val commandId: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val state: ChatMessageState = ChatMessageState.SENT,
    val latencyMs: Long? = null,
    val inputTokens: Int? = null,
    val outputTokens: Int? = null,
    val error: String? = null,
)

@Serializable
data class ChatSession(
    val id: String = UUID.randomUUID().toString(),
    val title: String = "新会话",
    val messages: List<ChatMessage> = emptyList(),
    val createdAt: Long = System.currentTimeMillis(),
)

@Serializable
data class ChatState(
    val sessions: List<ChatSession> = emptyList(),
    val activeSessionId: String = "",
) {
    val active: ChatSession?
        get() = sessions.firstOrNull { it.id == activeSessionId } ?: sessions.lastOrNull()

    companion object {
        fun empty() = ChatState(sessions = listOf(ChatSession()))
    }
}
