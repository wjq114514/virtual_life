package dev.virtuallife.chat

import dev.virtuallife.node.CompanionSnapshot
import dev.virtuallife.node.NodeKind
import dev.virtuallife.node.PersonalityHistoryRole
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatRequestsTest {

    private val config = ChatConfig(
        baseUrl = "http://10.0.2.2:8000/",
        token = "dev-token",
        nodeId = "android-chat",
        companionId = "companion-1",
    )

    @Test
    fun registrationRequestsTheTextDisplayCapability() {
        val registration = ChatRequests.registration(config)
        assertEquals("android-chat", registration.nodeId)
        assertEquals(NodeKind.ANDROID, registration.kind)
        assertEquals(listOf("text.display"), registration.capabilities)
    }

    @Test
    fun historyKeepsTheMostRecentTwentyCommittedMessages() {
        val messages = (1..25).map {
            ChatMessage(role = ChatRole.USER, content = "第 $it 条")
        }
        val history = ChatRequests.history(messages)
        assertEquals(20, history.size)
        assertEquals("第 6 条", history.first().content)
        assertEquals("第 25 条", history.last().content)
    }

    @Test
    fun historySkipsPendingFailedAndBlankMessages() {
        val committed = (1..22).map { ChatMessage(role = ChatRole.USER, content = "第 $it 条") }
        val noisy = committed +
            ChatMessage(role = ChatRole.ASSISTANT, content = "还没回完", state = ChatMessageState.PENDING) +
            ChatMessage(role = ChatRole.ASSISTANT, content = "报错了", state = ChatMessageState.FAILED, error = "boom") +
            ChatMessage(role = ChatRole.USER, content = "   ")

        val history = ChatRequests.history(noisy)

        // Filtering drops the 3 noisy rows first, leaving 22 committed rows, so the window keeps
        // 第 3 条 through 第 22 条.
        assertEquals(20, history.size)
        assertEquals("第 3 条", history.first().content)
        assertEquals("第 22 条", history.last().content)
        assertTrue(history.none { it.content.contains("还没回完") })
        assertTrue(history.none { it.content.contains("报错了") })
    }

    @Test
    fun historyClipsEveryMessageToTheServerLimit() {
        val long = "字".repeat(ChatRequests.MAX_MESSAGE_CHARS + 1000)
        val history = ChatRequests.history(
            listOf(
                ChatMessage(role = ChatRole.USER, content = long),
                ChatMessage(role = ChatRole.ASSISTANT, content = long),
            ),
        )
        assertEquals(4096, history.first().content.length)
        assertEquals(4096, history.last().content.length)
    }

    @Test
    fun roleMappingCoversEveryChatRole() {
        assertEquals(PersonalityHistoryRole.USER, ChatRequests.toNodeRole(ChatRole.USER))
        assertEquals(PersonalityHistoryRole.ASSISTANT, ChatRequests.toNodeRole(ChatRole.ASSISTANT))
    }

    @Test
    fun dialogueRequestCarriesTheFreshSnapshotAndTheConfiguredBudgets() {
        val snapshot = snapshot(revision = 7, epoch = 3)
        val request = ChatRequests.dialogue(
            config,
            snapshot,
            presentationId = "chat-android-chat",
            commandId = "cmd-1",
            text = "在吗",
            history = emptyList(),
        )
        assertEquals("cmd-1", request.commandId)
        assertEquals("android-chat", request.nodeId)
        assertEquals(3, request.epoch)
        assertEquals(7L, request.expectedRevision)
        assertEquals("在吗", request.userMessage)
        assertEquals("你", request.userName)
        assertEquals(true, request.userInitiated)
        assertEquals(false, request.audioRequested)
        assertEquals("chat-android-chat", request.presentationId)
        assertEquals(4096, request.tokenBudget)
        assertEquals(800, request.memoryTokenBudget)
        assertEquals(8, request.maxMemoryItems)
        assertEquals(512, request.maxOutputTokens)
        assertEquals(0, request.history.size)
    }

    @Test
    fun dialogueRequestClampsBudgetsToTheProtocolRange() {
        val overBudget = config.copy(
            tokenBudget = 999_999,
            memoryTokenBudget = 999_999,
            maxMemoryItems = 999_999,
            maxOutputTokens = 99_999,
        )
        val underBudget = config.copy(
            tokenBudget = 0,
            memoryTokenBudget = -50,
            maxMemoryItems = 0,
            maxOutputTokens = 0,
        )
        assertEquals(64_000, ChatRequests.dialogue(overBudget, snapshot(), "p", "c", "hi", emptyList()).tokenBudget)
        assertEquals(64_000, ChatRequests.dialogue(overBudget, snapshot(), "p", "c", "hi", emptyList()).memoryTokenBudget)
        assertEquals(100, ChatRequests.dialogue(overBudget, snapshot(), "p", "c", "hi", emptyList()).maxMemoryItems)
        assertEquals(1024, ChatRequests.dialogue(overBudget, snapshot(), "p", "c", "hi", emptyList()).maxOutputTokens)
        assertEquals(1, ChatRequests.dialogue(underBudget, snapshot(), "p", "c", "hi", emptyList()).tokenBudget)
        assertEquals(0, ChatRequests.dialogue(underBudget, snapshot(), "p", "c", "hi", emptyList()).memoryTokenBudget)
        assertEquals(1, ChatRequests.dialogue(underBudget, snapshot(), "p", "c", "hi", emptyList()).maxMemoryItems)
        assertEquals(1, ChatRequests.dialogue(underBudget, snapshot(), "p", "c", "hi", emptyList()).maxOutputTokens)
    }

    @Test
    fun dialogueRequestClipsTheUserMessageAndKeepsAUsableName() {
        val request = ChatRequests.dialogue(
            config.copy(userName = "   "),
            snapshot(),
            presentationId = "p",
            commandId = "cmd-2",
            text = "字".repeat(ChatRequests.MAX_MESSAGE_CHARS + 5),
            history = emptyList(),
        )
        assertEquals(4096, request.userMessage.length)
        assertEquals("你", request.userName)
    }

    private fun snapshot(revision: Long = 1, epoch: Long = 1) = CompanionSnapshot(
        companionId = config.companionId,
        name = "聊天伙伴",
        revision = revision,
        epoch = epoch,
        createdAt = "2026-01-01T00:00:00Z",
        updatedAt = "2026-01-01T00:00:00Z",
    )
}
