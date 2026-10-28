package dev.virtuallife.chat

import dev.virtuallife.node.CompanionSnapshot
import dev.virtuallife.node.DialogueRequest
import dev.virtuallife.node.NodeKind
import dev.virtuallife.node.NodeRegistration
import dev.virtuallife.node.PersonalityHistoryMessage
import dev.virtuallife.node.PersonalityHistoryRole

/**
 * Pure request builders. Keeping these out of the networking layer means the protocol budgets
 * can be checked without a device.
 *
 * The runtime rejects an over-budget request before any model call, so every value is clipped
 * to its documented range here instead of surfacing a 4xx to the user.
 */
object ChatRequests {
    /** Required capability for a node to be allowed to output text. */
    const val TEXT_DISPLAY = "text.display"

    const val MAX_MESSAGE_CHARS = 4096
    const val MAX_HISTORY_ITEMS = 20

    fun registration(config: ChatConfig): NodeRegistration = NodeRegistration(
        nodeId = config.nodeId,
        label = "Virtual Life 聊天",
        kind = NodeKind.ANDROID,
        capabilities = listOf(TEXT_DISPLAY),
    )

    fun clip(text: String): String =
        if (text.length <= MAX_MESSAGE_CHARS) text else text.take(MAX_MESSAGE_CHARS)

    /**
     * Newest last. Pending and failed placeholders are skipped so an uncommitted turn is never
     * replayed to the model as though the companion had said it.
     */
    fun history(messages: List<ChatMessage>): List<PersonalityHistoryMessage> = messages
        .filter { it.content.isNotBlank() && it.state == ChatMessageState.SENT }
        .takeLast(MAX_HISTORY_ITEMS)
        .map { PersonalityHistoryMessage(role = toNodeRole(it.role), content = clip(it.content)) }

    fun toNodeRole(role: ChatRole): PersonalityHistoryRole = when (role) {
        ChatRole.USER -> PersonalityHistoryRole.USER
        ChatRole.ASSISTANT -> PersonalityHistoryRole.ASSISTANT
    }

    fun dialogue(
        config: ChatConfig,
        companion: CompanionSnapshot,
        presentationId: String,
        commandId: String,
        text: String,
        history: List<PersonalityHistoryMessage>,
    ): DialogueRequest = DialogueRequest(
        commandId = commandId,
        nodeId = config.nodeId,
        epoch = companion.epoch,
        expectedRevision = companion.revision,
        userMessage = clip(text),
        userName = config.userName.ifBlank { "你" },
        userInitiated = true,
        audioRequested = false,
        history = history,
        tokenBudget = config.tokenBudget.coerceIn(1, 64_000),
        memoryTokenBudget = config.memoryTokenBudget.coerceIn(0, 64_000),
        maxMemoryItems = config.maxMemoryItems.coerceIn(1, 100),
        maxOutputTokens = config.maxOutputTokens.coerceIn(1, 1024),
        presentationId = presentationId,
    )
}
