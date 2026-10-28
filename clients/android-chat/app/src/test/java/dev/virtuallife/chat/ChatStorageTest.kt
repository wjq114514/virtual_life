package dev.virtuallife.chat

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ChatStorageTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun missingFileYieldsOneEmptySession() {
        val state = ChatStorage(folder.newFile()).load()
        assertEquals(1, state.sessions.size)
        assertEquals("新会话", state.sessions.first().title)
        assertEquals(0, state.sessions.first().messages.size)
    }

    @Test
    fun corruptFileDegradesToAEmptyStateInsteadOfCrashing() {
        val file = folder.newFile()
        file.writeText("this is not json at all {{{")
        val state = ChatStorage(file).load()
        assertEquals(1, state.sessions.size)
    }

    @Test
    fun emptySessionsAreReplacedByAUsableOne() {
        val file = folder.newFile()
        file.writeText("""{"sessions":[],"activeSessionId":""}""")
        val state = ChatStorage(file).load()
        assertEquals(1, state.sessions.size)
        assertNotNull(state.active)
    }

    @Test
    fun roundTripPreservesSessionsSelectionMessagesAndUsage() {
        val file = folder.newFile()
        val session = ChatSession(
            id = "session-1",
            title = "第一次聊天",
            createdAt = 1_700_000_000_000,
            messages = listOf(
                ChatMessage(
                    id = "m1",
                    role = ChatRole.USER,
                    content = "你好",
                    commandId = "cmd-1",
                    createdAt = 1_700_000_000_001,
                    state = ChatMessageState.SENT,
                    latencyMs = 42,
                ),
                ChatMessage(
                    id = "m2",
                    role = ChatRole.ASSISTANT,
                    content = "我在。",
                    commandId = "cmd-1",
                    createdAt = 1_700_000_000_050,
                    state = ChatMessageState.SENT,
                    latencyMs = 42,
                    inputTokens = 120,
                    outputTokens = 8,
                ),
                ChatMessage(
                    id = "m3",
                    role = ChatRole.ASSISTANT,
                    commandId = "cmd-2",
                    state = ChatMessageState.PENDING,
                ),
            ),
        )
        val state = ChatState(sessions = listOf(session), activeSessionId = session.id)

        ChatStorage(file).save(state)
        val loaded = ChatStorage(file).load()

        assertEquals(1, loaded.sessions.size)
        assertEquals(session.id, loaded.activeSessionId)
        assertEquals("第一次聊天", loaded.active?.title)
        val messages = loaded.active?.messages ?: emptyList()
        assertEquals(3, messages.size)
        assertEquals("你好", messages[0].content)
        assertEquals(ChatRole.USER, messages[0].role)
        assertEquals("cmd-1", messages[0].commandId)
        assertEquals(42L, messages[0].latencyMs)
        assertEquals("我在。", messages[1].content)
        assertEquals(120, messages[1].inputTokens)
        assertEquals(8, messages[1].outputTokens)
        assertEquals(ChatMessageState.PENDING, messages[2].state)
        assertEquals("", messages[2].content)
    }

    @Test
    fun unknownFieldsAreIgnoredSoOlderFilesStayReadable() {
        val file = folder.newFile()
        file.writeText(
            """
            {"sessions":[{"id":"s1","title":"旧版","messages":[],
              "createdAt":1,"unknown_future_field":"ignored"}],
             "activeSessionId":"s1","another_future_field":true}
            """.trimIndent(),
        )
        val state = ChatStorage(file).load()
        assertEquals("旧版", state.active?.title)
    }

    @Test
    fun clearRemovesTheFile() {
        val file = folder.newFile()
        ChatStorage(file).save(ChatState.empty())
        file.deleteOnExit()
        val storage = ChatStorage(file)
        storage.clear()
        assertEquals(false, file.exists())
        assertEquals(1, storage.load().sessions.size)
    }
}
