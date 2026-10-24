package dev.virtuallife.app

import android.content.Context
import android.media.projection.MediaProjection
import android.os.PowerManager
import dev.virtuallife.node.ApiException
import dev.virtuallife.node.ClientFeatures
import dev.virtuallife.node.ClientHash
import dev.virtuallife.node.HostMetadata
import dev.virtuallife.node.MonotonicClock
import dev.virtuallife.node.NodeRegistration
import dev.virtuallife.node.RuntimeApi
import dev.virtuallife.node.ScreenCommand
import dev.virtuallife.node.ScreenFrameInput
import dev.virtuallife.node.ScreenFrameSnapshot
import dev.virtuallife.node.ScreenFrameSubmit
import dev.virtuallife.node.ScreenSessionCreate
import dev.virtuallife.node.ScreenSessionReceipt
import dev.virtuallife.node.ScreenUploadRegister
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID
import kotlin.coroutines.coroutineContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/**
 * Server-side screen vision for the Android host: capture locally, upload only
 * changed frames to the runtime's screen API, and never run vision on-device.
 *
 * The screen API is admin-token only, so this controller authenticates with the
 * separate [AppConfig.adminToken] rather than the node reporter token.
 */
class ServerScreenController(
    private val context: Context,
    private val config: AppConfig,
    http: OkHttpClient,
) {
    val status = MutableStateFlow("屏幕未共享")
    val summary = MutableStateFlow("")

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val clock = MonotonicClock { System.nanoTime() }
    private val api = RuntimeApi(config.baseUrl, config.adminToken, clock, http)

    private var job: Job? = null

    @Volatile
    private var projection: MediaProjection? = null

    @Volatile
    private var session: ScreenSessionReceipt? = null

    fun start(mediaProjection: MediaProjection) {
        if (job?.isActive == true) return
        projection = mediaProjection
        job = scope.launch { runLoop(mediaProjection) }
    }

    suspend fun stop() {
        job?.cancel()
        job = null
        val current = session
        session = null
        if (current != null) {
            runCatching { stopSession(current) }
        }
        runCatching { projection?.stop() }
        projection = null
        status.value = "屏幕未共享"
    }

    private suspend fun runLoop(mediaProjection: MediaProjection) {
        var capturer: MediaProjectionCapturer? = null
        try {
            registerNode()
            status.value = "创建屏幕会话…"
            val created = api.createScreenSession(
                config.nodeId,
                ScreenSessionCreate(
                    commandId = UUID.randomUUID().toString(),
                    companionId = config.companionId,
                    subjectNodeId = config.nodeId,
                    displayId = config.displayId,
                    mode = "continuous",
                    userInitiated = true,
                    captureSource = "android_media_projection",
                    requestedProfile = "balanced",
                ),
            )
            session = created
            status.value = "共享屏幕中"

            val longSide = created.advice.suggestedLongSide
                .coerceAtMost(created.limits.maxImageSide)
                .coerceAtLeast(320)
            val capture = MediaProjectionCapturer(
                context,
                mediaProjection,
                longSide,
                created.limits.maxImageBytes,
            )
            capturer = capture
            val filter = ScreenFrameFilter(
                minUploadIntervalMs = created.advice.minUploadIntervalMs.toLong(),
                keyframeIntervalMs = created.advice.maxKeyframeIntervalMs.toLong(),
            )
            val interval = created.advice.sampleIntervalMs.toLong().coerceAtLeast(250)

            var sequence = 0L
            while (coroutineContext.isActive) {
                val frame = capture.capture()
                if (frame == null) {
                    status.value = "屏幕不可用（关闭或未就绪）"
                    delay(interval)
                    continue
                }
                val decision = filter.evaluate(frame.fingerprint, System.currentTimeMillis())
                if (decision == FrameDecision.SKIP) {
                    delay(interval)
                    continue
                }
                try {
                    submitFrame(created, frame, decision, sequence, filter.changeScore(frame.fingerprint))
                    sequence += 1
                } catch (error: Throwable) {
                    // Keep only the newest candidate: never queue a backlog of stale screenshots.
                    status.value = "上传失败，保留最新帧稍后重试: ${error.message}"
                }
                delay(interval)
            }
        } catch (error: Throwable) {
            if (coroutineContext.isActive) {
                status.value = "屏幕启动失败: ${error.message}"
            }
        } finally {
            capturer?.release()
            val current = session
            session = null
            if (current != null) {
                runCatching { stopSession(current) }
            }
            if (status.value != "屏幕未共享") status.value = "屏幕未共享"
        }
    }

    private suspend fun submitFrame(
        session: ScreenSessionReceipt,
        frame: CapturedFrame,
        decision: FrameDecision,
        sequence: Long,
        changeScore: Double,
    ) {
        val mediaType = "image/jpeg"
        val upload = api.registerScreenUpload(
            config.nodeId,
            ScreenUploadRegister(
                commandId = UUID.randomUUID().toString(),
                sessionId = session.sessionId,
                generation = session.generation,
                mediaType = mediaType,
                byteLength = frame.jpeg.size.toLong(),
                sha256 = sha256(frame.jpeg),
                width = frame.width,
                height = frame.height,
            ),
        )
        api.putScreenUploadContent(config.nodeId, upload.uploadId, frame.jpeg, mediaType)
        val trigger = when (decision) {
            FrameDecision.FIRST -> "first_frame"
            FrameDecision.REFRESH -> "refresh"
            else -> "changed"
        }
        val receipt = api.submitScreenFrame(
            config.nodeId,
            ScreenFrameSubmit(
                commandId = UUID.randomUUID().toString(),
                sessionId = session.sessionId,
                generation = session.generation,
                sequence = sequence,
                sampledAt = Instant.now().toString(),
                ttlSeconds = 90,
                trigger = trigger,
                input = ScreenFrameInput.Image(
                    uploadId = upload.uploadId,
                    captureWidth = frame.width,
                    captureHeight = frame.height,
                    rotationDegrees = frame.rotationDegrees,
                ),
                clientFeatures = ClientFeatures(
                    extractor = "average_hash",
                    changeScore = changeScore,
                    hashes = listOf(ClientHash("average_hash_64", hashHex(frame.fingerprint))),
                ),
                hostMetadata = hostMetadata(),
            ),
        )
        status.value = "已接收帧 #$sequence"
        val snapshot = waitForFrame(receipt.frameId)
        val analysisId = snapshot?.analysisId
        if (analysisId != null) {
            runCatching { api.getScreenAnalysis(config.nodeId, analysisId) }
                .getOrNull()
                ?.let { summary.value = it.sceneSummary }
        }
    }

    private suspend fun waitForFrame(frameId: String): ScreenFrameSnapshot? {
        val deadline = System.currentTimeMillis() + 25_000
        var snapshot: ScreenFrameSnapshot? = null
        while (System.currentTimeMillis() < deadline) {
            snapshot = runCatching { api.getScreenFrame(config.nodeId, frameId) }.getOrNull()
            if (snapshot == null || snapshot.status in TERMINAL_FRAME_STATUS) return snapshot
            delay(500)
        }
        return snapshot
    }

    private suspend fun stopSession(session: ScreenSessionReceipt) {
        api.stopScreenSession(
            config.nodeId,
            session.sessionId,
            ScreenCommand(UUID.randomUUID().toString()),
        )
        api.purgeScreenSession(
            config.nodeId,
            session.sessionId,
            ScreenCommand(UUID.randomUUID().toString()),
        )
    }

    private suspend fun registerNode() {
        try {
            api.registerNode(
                NodeRegistration(
                    nodeId = config.nodeId,
                    label = "Virtual Life Android",
                    capabilities = listOf(
                        "vision.capture",
                        "text.display",
                        "audio.output",
                        "microphone.input",
                    ),
                ),
            )
        } catch (error: ApiException) {
            if (error.statusCode != 409) throw error
        }
    }

    private fun hostMetadata(): HostMetadata {
        val interactive = context.getSystemService(PowerManager::class.java)?.isInteractive ?: true
        return HostMetadata(locked = !interactive, userPresent = interactive)
    }

    companion object {
        private val TERMINAL_FRAME_STATUS = setOf("succeeded", "failed")

        private fun sha256(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

        private fun hashHex(value: Long): String = java.lang.Long.toHexString(value)
    }
}
