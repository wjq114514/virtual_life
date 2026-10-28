package dev.virtuallife.node

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class DialogueRuntimeApiTest {
    @get:Rule
    val server = MockWebServer()

    private fun pageBody() = """
        {"companion_id":"companion-1","items":[
          {"event_id":"event-1","sequence":41,"command_id":"cmd-1","node_id":"node-1",
           "occurred_at":"2026-01-01T00:00:00Z","user_message":"你好","assistant_message":"我在"},
          {"event_id":"event-0","sequence":40,"command_id":"cmd-0","node_id":"node-1",
           "occurred_at":"2026-01-01T00:01:00Z","user_message":"","assistant_message":""}
        ],"next_before_sequence":40,"has_more":true}
    """.trimIndent()

    @Test
    fun historySendsCursorAndBearerAuth() = runBlocking {
        server.enqueue(MockResponse().setBody(pageBody()))
        val api = runtimeApi()
        val page = api.dialogueHistory("companion-1", beforeSequence = 42, limit = 5)
        val recorded = server.takeRequest()

        assertEquals("/v1/companions/companion-1/dialogue/history?before_sequence=42&limit=5", recorded.path)
        assertEquals("Bearer dev-token", recorded.getHeader("Authorization"))
        assertEquals(2, page.items.size)
        assertEquals("companion-1", page.companionId)
        assertEquals(41L, page.items.first().sequence)
        assertEquals("event-1", page.items.first().eventId)
        assertEquals("你好", page.items.first().userMessage)
        assertEquals("我在", page.items.first().assistantMessage)
        assertEquals("", page.items[1].userMessage)
        assertEquals(40L, page.nextBeforeSequence)
        assertTrue(page.hasMore)
    }

    @Test
    fun historyOmitsTheCursorOnTheFirstPage() = runBlocking {
        server.enqueue(
            MockResponse().setBody(
                """{"companion_id":"companion-1","items":[],"next_before_sequence":null,"has_more":false}""",
            ),
        )
        val page = runtimeApi().dialogueHistory("companion-1")
        val recorded = server.takeRequest()

        assertEquals("/v1/companions/companion-1/dialogue/history?limit=20", recorded.path)
        assertEquals(emptyList<DialogueHistoryItem>(), page.items)
        assertNull(page.nextBeforeSequence)
        assertFalse(page.hasMore)
    }

    @Test
    fun historyRejectsOutOfRangePageBounds() = runBlocking {
        val api = runtimeApi()
        assertThrowsInvalid { api.dialogueHistory("companion-1", limit = 0) }
        assertThrowsInvalid { api.dialogueHistory("companion-1", limit = 101) }
        assertThrowsInvalid { api.dialogueHistory("companion-1", beforeSequence = -1) }
    }

    /** assertThrows takes a non-suspend lambda, so the suspend call has to be dispatched inside it. */
    private fun assertThrowsInvalid(block: suspend () -> Unit) {
        assertThrows(IllegalArgumentException::class.java) { runBlocking { block() } }
    }

    private fun runtimeApi(): RuntimeApi = RuntimeApi(
        baseUrl = server.url("/").toString(),
        token = "dev-token",
        clock = MonotonicClock { 1L },
    )
}
