package dev.virtuallife.app

import android.content.Context
import android.media.projection.MediaProjection
import android.os.PowerManager
import dev.virtuallife.node.ApiException
import dev.virtuallife.node.AttentionObservation
import dev.virtuallife.node.AttentionSample
import dev.virtuallife.node.ClientFeatures
import dev.virtuallife.node.ClientHash
import dev.virtuallife.node.HostMetadata
import dev.virtuallife.node.MonotonicClock
import dev.virtuallife.node.NodeHeartbeat
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

    @Volatile
    private var attentionSequence = 0L

    private val attentionSessionId: String = "android-screen-" + UUID.randomUUID().toString()

    @Volatile
    private var lastAttentionAtMs = 0L

    @Volatile
    private var lastHeartbeatAtMs = 0L

    private val heartbeatIntervalMs = 15_000L

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
            heartbeatNode()  // node liveness only comes from POST /nodes/{id}/heartbeat
            status.value = "创建屏幕会话…"
            var created = createSession()
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
                heartbeatNode()  // keep node online while capture is running
                val frame = capture.capture()
                if (frame == null) {
                    status.value = "屏幕不可用（关闭或未就绪）"
                    delay(interval)
                    continue
                }
                val decision = filter.evaluate(frame.fingerprint, System.currentTimeMillis())
                reportAttention(decision == FrameDecision.SKIP)
                if (decision == FrameDecision.SKIP) {
                    delay(interval)
                    continue
                }
                try {
                    submitFrame(created, frame, decision, sequence, filter.changeScore(frame.fingerprint))
                    sequence += 1
                } catch (error: Throwable) {
                    if (isSessionExpired(error)) {
                        // The screen session is a fixed server TTL and frames do not
                        // extend it. Once it expires every upload 409s, so renew the
                        // session and keep capturing instead of retrying a dead one.
                        status.value = "屏幕会话已过期，重建会话…"
                        runCatching { api.stopScreenSession(
                            config.nodeId, created.sessionId, ScreenCommand(UUID.randomUUID().toString())
                        ) }
                        val renewed = runCatching { createSession() }.getOrNull()
                        if (renewed == null || !coroutineContext.isActive) break
                        created = renewed
                        session = created
                        sequence = 0
                        status.value = "共享屏幕中（会话已续期）"
                    } else {
                        // Keep only the newest candidate: never queue a backlog of stale screenshots.
                        status.value = "上传失败，保留最新帧稍后重试: ${error.message}"
                    }
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

    private suspend fun createSession(): ScreenSessionReceipt =
        api.createScreenSession(
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

    private fun isSessionExpired(error: Throwable): Boolean =
        error is ApiException && (
            error.code == "screen_session_inactive" ||
                error.code == "screen_generation_conflict" ||
                error.code == "screen_frame_expired"
            )

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
                clientFeatures = clientFeatures(frame, changeScore),
                hostMetadata = hostMetadata(frame),
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
                        "attention.observe",
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

    /**
     * Keep the node online. Node liveness (online/offline) is computed from
     * `last_seen_at`, which only POST /nodes/{id}/heartbeat refreshes; frame
     * uploads and attention observations do not. Without this the node flips to
     * offline ~60s after registration even while the capture loop is running.
     * Throttled to once every 15s (well under node_ttl_seconds=60); failures
     * never break the capture loop.
     */
    private suspend fun heartbeatNode() {
        val nowMs = System.currentTimeMillis()
        if (nowMs - lastHeartbeatAtMs < heartbeatIntervalMs) return
        lastHeartbeatAtMs = nowMs
        try {
            api.heartbeat(config.nodeId, NodeHeartbeat())
        } catch (error: Throwable) {
            // Never let a heartbeat failure kill the capture loop.
        }
    }

    /**
     * Report host state as an ambient attention sample so the runtime can treat
     * this Android device as an observer room (activity, focus, presence) even
     * though the companion itself resides on an IM node (QQ/Matrix). Throttled
     * to at most once per second; failures never break the capture loop.
     */
    private suspend fun reportAttention(skippedFrame: Boolean) {
        val nowMs = System.currentTimeMillis()
        if (nowMs - lastAttentionAtMs < 1_000) return
        lastAttentionAtMs = nowMs
        val interactive = context.getSystemService(PowerManager::class.java)?.isInteractive ?: true
        val scene = AccessibilitySceneService.latest
        val activity = ActivityClassifier.classifyActivity(
            scene?.appId,
            scene?.className,
            scene?.windowTitle,
        )
        val focus = if (interactive) "focused" else "unfocused"
        try {
            api.observeAttention(
                config.nodeId,
                AttentionObservation(
                    commandId = UUID.randomUUID().toString(),
                    sample = AttentionSample(
                        sessionId = attentionSessionId,
                        sequence = attentionSequence,
                        sampledAt = Instant.now().toString(),
                        ttlSeconds = 90,
                        activity = activity,
                        focus = focus,
                        availability = if (interactive) "available" else "busy",
                        userPresent = interactive,
                        locked = !interactive,
                        meeting = activity == "meeting",
                        screenSharing = true,
                    ),
                ),
            )
            attentionSequence += 1
        } catch (error: Throwable) {
            // Ambient state reporting is advisory; a failure here must not kill
            // the screen-vision capture loop. We simply try again next frame.
        }
    }

    private fun clientFeatures(
        frame: CapturedFrame,
        changeScore: Double,
    ): ClientFeatures {
        val base = ClientFeatures(
            extractor = "average_hash",
            changeScore = changeScore,
            hashes = listOf(ClientHash("average_hash_64", hashHex(frame.fingerprint))),
        )
        // Merge the cached accessibility scene (if any) without losing the hash.
        val scene = AccessibilitySceneService.latest ?: return base
        val uiElements = scene.clientFeatures?.get("ui_elements")
        val mapped = (uiElements as? List<*>)
            ?.filterIsInstance<Map<*, *>>()
            ?.mapNotNull { item ->
                val role = item["role"]?.toString() ?: return@mapNotNull null
                val text = item["text"]?.toString() ?: ""
                val bbox = (item["bbox"] as? List<*>)
                    ?.mapNotNull { (it as? Number)?.toDouble() }
                    ?.takeIf { it.size == 4 }
                    ?: return@mapNotNull null
                val id = item["element_id"]?.toString() ?: "a11y:${text.hashCode()}"
                val conf = (item["confidence"] as? Number)?.toDouble()
                dev.virtuallife.node.UiElement(
                    elementId = id,
                    role = role,
                    text = text,
                    bbox = bbox,
                    confidence = conf,
                )
            }
            ?.take(8)
        return if (mapped.isNullOrEmpty()) {
            base
        } else {
            base.copy(
                extractor = "android_a11y",
                uiElements = mapped,
            )
        }
    }

    private fun hostMetadata(frame: CapturedFrame): HostMetadata {
        val interactive = context.getSystemService(PowerManager::class.java)?.isInteractive ?: true
        val scene = AccessibilitySceneService.latest
        val appId = scene?.appId
        val appClass = scene?.className
        val activity = ActivityClassifier.classifyActivity(
            appId,
            appClass,
            scene?.windowTitle,
        )
        return HostMetadata(
            locked = !interactive,
            userPresent = interactive,
            appId = appId,
            appClass = appClass,
            activity = activity,
        )
    }

    companion object {
        private val TERMINAL_FRAME_STATUS = setOf("succeeded", "failed")

        private fun sha256(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

        private fun hashHex(value: Long): String = java.lang.Long.toHexString(value)
    }
}
