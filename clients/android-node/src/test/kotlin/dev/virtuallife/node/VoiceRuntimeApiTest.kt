package dev.virtuallife.node

import java.io.IOException
import java.security.MessageDigest
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertThrows

class VoiceRuntimeApiTest {
    @get:Rule
    val server = MockWebServer()

    @Test
    fun submitTurnPostsTheTypedVoiceEndpoint() = runBlocking {
        server.enqueue(
            MockResponse().setBody(
                """{"turn_id":"turn-1","command_id":"cmd-1","session_id":"session-1","companion_id":"companion-1","session_generation":1,"output_generation":2,"status":"accepted","sequence":1,"audio_enabled":true,"speech_available":true,"assistant_message":null,"discard_reason":null,"created_at":"2026-01-01T00:00:00Z"}""",
            ),
        )
        val api = runtimeApi()
        val requestBody = VoiceTurnSubmit(
            commandId = "cmd-1",
            turnId = "turn-1",
            sessionGeneration = 1,
            expectedOutputGeneration = 2,
            epoch = 3,
            expectedRevision = 4,
            text = "hello",
            endReason = VoiceEndReason.MANUAL,
            input = VoiceTurnInput.Text(),
        )

        val receipt = api.submitVoiceTurn("node-1", "session-1", requestBody)
        val request = server.takeRequest()
        val encodedBody = request.body.readUtf8()

        assertEquals("POST", request.method)
        assertEquals("/v1/nodes/node-1/voice/sessions/session-1/turns", request.path)
        assertEquals("Bearer test-token", request.getHeader("Authorization"))
        assertEquals("accepted", receipt.status.toString().lowercase())
        assertTrue(encodedBody.contains("\"expected_output_generation\":2"))
        assertTrue(encodedBody.contains("\"kind\":\"text\""))
    }

    @Test
    fun streamRequiresMatchingIdentityAndVoiceEnd() = runBlocking {
        server.enqueue(
            MockResponse().setHeader("Content-Type", "text/event-stream; charset=utf-8").setBody(
                """event: snapshot
data: {"session_id":"session-1","session_generation":1,"turn_id":"turn-1","output_generation":2,"sequence":0,"cursor":0,"replay_gap":false,"turn":{"turn_id":"turn-1","command_id":"cmd-1","session_id":"session-1","companion_id":"companion-1","status":"in_progress","output_generation":2,"speech_status":"unknown","segment_count":0,"sequence":0,"created_at":"2026-01-01T00:00:00Z"}}

event: voice_end
data: {"session_id":"session-1","session_generation":1,"turn_id":"turn-1","output_generation":2,"sequence":1,"status":"completed"}

""",
            ),
        )
        val events = mutableListOf<VoiceStreamEvent>()

        runtimeApi().streamVoiceTurn("node-1", "session-1", "turn-1") { events += it }

        assertTrue(events.first() is VoiceStreamEvent.Snapshot)
        assertTrue(events.last() is VoiceStreamEvent.VoiceEnd)
        assertEquals("/v1/nodes/node-1/voice/sessions/session-1/turns/turn-1/events?after_sequence=0", server.takeRequest().path)
    }

    @Test
    fun streamRejectsAnIdentityMismatch() {
        server.enqueue(
            MockResponse().setHeader("Content-Type", "text/event-stream").setBody(
                """event: voice_end
data: {"session_id":"other-session","session_generation":1,"turn_id":"turn-1","output_generation":2,"sequence":1,"status":"completed"}

""",
            ),
        )

        assertThrows(IOException::class.java) {
            runBlocking {
                runtimeApi().streamVoiceTurn("node-1", "session-1", "turn-1") { }
            }
        }
    }

    @Test
    fun downloadUsesFixedRouteAndChecksPcmDigestAndHeaders() = runBlocking {
        val pcm = byteArrayOf(1, 0, 2, 0, 3, 0, 4, 0)
        val digest = sha256(pcm)
        server.enqueue(
            MockResponse()
                .setHeader("Content-Type", "audio/pcm")
                .setHeader("X-Audio-SHA256", digest)
                .setHeader("X-Audio-Sample-Rate", "16000")
                .setHeader("X-Audio-Channels", "1")
                .setHeader("X-Audio-Sample-Count", "4")
                .setBody(Buffer().write(pcm)),
        )
        val segment = segment(samples = 4, sha256 = digest, audioPath = "/untrusted/path")

        val downloaded = runtimeApi().downloadVoicePcm("node-1", "session-1", "turn-1", segment)
        val request = server.takeRequest()

        assertArrayEquals(pcm, downloaded.bytes)
        assertEquals(
            "/v1/nodes/node-1/voice/sessions/session-1/turns/turn-1/segments/7/audio",
            request.path,
        )
        assertEquals("Bearer test-token", request.getHeader("Authorization"))
    }

    @Test
    fun downloadSurfacesExpiredStatusAndRejectsOversizedMetadataBeforeRequest() {
        server.enqueue(
            MockResponse().setResponseCode(410)
                .setBody("""{"error":{"code":"speech_expired","message":"expired"}}"""),
        )
        val expired = assertThrows(ApiException::class.java) {
            runBlocking {
                runtimeApi().downloadVoicePcm(
                    "node-1", "session-1", "turn-1", segment(samples = 4),
                )
            }
        }
        assertEquals(410, expired.statusCode)
        assertEquals("speech_expired", expired.code)

        assertThrows(IOException::class.java) {
            runBlocking {
                runtimeApi().downloadVoicePcm(
                    "node-1", "session-1", "turn-1", segment(samples = 32), maxBytes = 32,
                )
            }
        }
        assertEquals(1, server.requestCount)
    }

    private fun runtimeApi() = RuntimeApi(
        baseUrl = server.url("/").toString(),
        token = "test-token",
        clock = MonotonicClock { 1L },
    )

    private fun segment(
        samples: Long,
        sha256: String = "0".repeat(64),
        audioPath: String = "/v1/not-trusted",
    ) = VoiceSegmentInfo(
        segmentSequence = 7,
        text = "hello",
        spokenText = "hello",
        charStart = 0,
        charEnd = 5,
        audioPath = audioPath,
        mediaType = "audio/pcm",
        sampleRate = 16_000,
        channels = 1,
        sampleCount = samples,
        sha256 = sha256,
        status = VoiceSegmentStatus.READY,
        expiresAt = "2026-01-01T00:00:00Z",
    )

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }
}
