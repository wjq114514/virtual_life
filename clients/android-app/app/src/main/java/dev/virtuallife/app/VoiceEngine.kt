package dev.virtuallife.app

import android.content.Context
import dev.virtuallife.node.AndroidNode
import dev.virtuallife.node.AndroidNodeBridge
import dev.virtuallife.node.ApiException
import dev.virtuallife.node.CompanionCreate
import dev.virtuallife.node.CompanionSnapshot
import dev.virtuallife.node.FileCursorStore
import dev.virtuallife.node.HostPermissions
import dev.virtuallife.node.MonotonicClock
import dev.virtuallife.node.NodeSessionConfig
import dev.virtuallife.node.NodeWebSocketConnector
import dev.virtuallife.node.OutputHandle
import dev.virtuallife.node.PresentationOpen
import dev.virtuallife.node.RuntimeApi
import dev.virtuallife.node.ScreenSignals
import dev.virtuallife.node.StopReason
import dev.virtuallife.node.TransferPrepare
import dev.virtuallife.node.VoiceAsrConfig
import dev.virtuallife.node.VoiceEndReason
import dev.virtuallife.node.VoiceHeartbeat
import dev.virtuallife.node.VoiceInterruptReason
import dev.virtuallife.node.VoiceInterruptRequest
import dev.virtuallife.node.VoiceMode
import dev.virtuallife.node.VoiceOutputConfig
import dev.virtuallife.node.VoicePlaybackReport
import dev.virtuallife.node.VoicePlaybackStatus
import dev.virtuallife.node.VoiceSegmentInfo
import dev.virtuallife.node.VoiceSegmentStatus
import dev.virtuallife.node.VoiceSessionCreate
import dev.virtuallife.node.VoiceSessionSnapshot
import dev.virtuallife.node.VoiceSessionStatus
import dev.virtuallife.node.VoiceStopRequest
import dev.virtuallife.node.VoiceStreamEvent
import dev.virtuallife.node.VoiceTurnInput
import dev.virtuallife.node.VoiceTurnSubmit
import java.io.File
import java.time.Instant
import java.util.UUID
import kotlin.math.sqrt
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient

/**
 * Owns one native node session, one presentation, one voice session, the microphone,
 * the local recognizer and playback. All server writes go through the shared RuntimeApi.
 */
class VoiceEngine(
    private val context: Context,
    private val config: AppConfig,
    private val http: OkHttpClient,
) {
    val status = MutableStateFlow("空闲")
    val draft = MutableStateFlow("")
    val reply = MutableStateFlow("")
    val playing = MutableStateFlow(false)

    var bargeIn = true

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val clock = MonotonicClock { System.nanoTime() }
    private val api = RuntimeApi(config.baseUrl, config.token, clock, http)
    private val presentationId = "android-${config.nodeId}"
    private val player = PcmPlayer()
    private val sessionLock = Mutex()

    private var nodeJob: Job? = null
    private var heartbeatJob: Job? = null
    private var asr: LocalAsr? = null
    private val mic = MicCapture()

    @Volatile
    private var capturing = false

    @Volatile
    private var companion: CompanionSnapshot? = null

    @Volatile
    private var session: VoiceSessionSnapshot? = null

    @Volatile
    private var lastTurnId: String? = null

    @Volatile
    private var turnCancelled = false

    private val bridge = object : AndroidNodeBridge {
        override suspend fun capabilities(): Set<String> =
            setOf("text.display", "audio.output", "microphone.input")

        override suspend fun permissions(): HostPermissions = HostPermissions(
            microphoneGranted = true,
            notificationsGranted = true,
            foregroundServiceRunning = true,
        )

        override suspend fun screenSignals(): ScreenSignals =
            ScreenSignals(hostVisible = true, userInteracting = false)

        override suspend fun prepareRender(snapshot: CompanionSnapshot) {}

        override suspend fun stopOutput(companionId: String, reason: StopReason) {
            player.stop()
            playing.value = false
        }

        override suspend fun renderState(snapshot: CompanionSnapshot, output: OutputHandle) {}

        override suspend fun onFailure(error: Throwable) {
            status.value = "节点错误: ${error.message}"
        }
    }

    suspend fun start() {
        if (nodeJob != null) return
        val modelDir = ModelRepository.resolve(context)
        if (modelDir == null) {
            status.value = "缺少本地 ASR 模型，请先导入"
            return
        }
        asr = try {
            LocalAsr.load(modelDir)
        } catch (error: Throwable) {
            status.value = "模型加载失败: ${error.message}"
            return
        }
        status.value = "连接运行时…"
        val sessionNode = AndroidNode(
            NodeSessionConfig(config.nodeId, "Virtual Life Android"),
            api,
            NodeWebSocketConnector(config.baseUrl, config.token, http),
            bridge,
            FileCursorStore(File(context.filesDir, "cursor")),
            clock,
        )
        nodeJob = scope.launch { sessionNode.run() }
        try {
            ensureCompanion()
            waitForNodeOnline()
            openPresentation()
            claimCompanion()
            createSession()
            startHeartbeat()
            startCapture()
            status.value = "正在听"
        } catch (error: Throwable) {
            status.value = "启动失败: ${error.message}"
            stopInternal(notifyServer = true)
        }
    }

    suspend fun stop() {
        stopInternal(notifyServer = true)
        status.value = "已停止"
    }

    suspend fun interrupt() {
        val current = session ?: return
        turnCancelled = true
        player.stop()
        playing.value = false
        val turnId = lastTurnId
        if (turnId != null) {
            runCatching {
                api.interruptVoiceTurn(
                    config.nodeId,
                    current.sessionId,
                    VoiceInterruptRequest(
                        commandId = UUID.randomUUID().toString(),
                        sessionGeneration = current.sessionGeneration,
                        targetTurnId = turnId,
                        expectedOutputGeneration = current.outputGeneration,
                        reason = VoiceInterruptReason.STOP_BUTTON,
                    ),
                )
            }
        }
        draft.value = ""
        status.value = "正在听"
    }

    suspend fun commitText(text: String) {
        if (text.isBlank()) return
        submitTurn(text, VoiceTurnInput.Text(), VoiceEndReason.MANUAL)
    }

    private suspend fun waitForNodeOnline() {
        val online = withTimeoutOrNull(15_000) {
            while (api.getNode(config.nodeId).online.not()) delay(200)
            true
        }
        if (online == null) status.value = "节点注册超时"
    }

    private suspend fun ensureCompanion() {
        try {
            api.createCompanion(CompanionCreate(config.companionId, "Android 伙伴"))
        } catch (error: ApiException) {
            if (error.statusCode != 409) throw error
        }
        companion = api.getCompanion(config.companionId)
    }

    private suspend fun openPresentation() {
        val sample = api.openPresentation(
            config.nodeId,
            PresentationOpen(presentationId = presentationId),
        )
        require(sample.presentation.presentationId == presentationId) {
            "Presentation id mismatch"
        }
    }

    private suspend fun claimCompanion() {
        val current = api.getCompanion(config.companionId)
        companion = current
        if (current.activeNodeId != config.nodeId) {
            api.prepareTransfer(
                config.companionId,
                TransferPrepare(
                    commandId = UUID.randomUUID().toString(),
                    targetNodeId = config.nodeId,
                    expectedRevision = current.revision,
                ),
            )
        }
        val active = withTimeoutOrNull(20_000) {
            var snapshot = api.getCompanion(config.companionId)
            while (snapshot.activeNodeId != config.nodeId) {
                delay(250)
                snapshot = api.getCompanion(config.companionId)
            }
            snapshot
        }
        companion = active ?: throw IllegalStateException("未能接管伙伴输出权")
    }

    private suspend fun createSession() {
        val current = companion ?: throw IllegalStateException("缺少伙伴快照")
        session = api.createVoiceSession(
            config.nodeId,
            VoiceSessionCreate(
                commandId = UUID.randomUUID().toString(),
                companionId = config.companionId,
                epoch = current.epoch,
                expectedRevision = current.revision,
                presentationId = presentationId,
                mode = VoiceMode.CONTINUOUS,
                asr = VoiceAsrConfig(
                    engine = "sherpa_onnx",
                    modelId = ModelRepository.MODEL_ID,
                    language = "zh",
                ),
                output = VoiceOutputConfig(audioEnabled = true, voiceId = "default"),
            ),
        )
        companion = api.getCompanion(config.companionId)
    }

    private fun startHeartbeat() {
        heartbeatJob = scope.launch {
            while (isActive) {
                val current = session ?: break
                delay((current.heartbeatIntervalSeconds * 1000).toLong().coerceAtLeast(1000))
                val updated = try {
                    api.heartbeatVoiceSession(
                        config.nodeId,
                        current.sessionId,
                        VoiceHeartbeat(UUID.randomUUID().toString()),
                    )
                } catch (error: Throwable) {
                    null
                }
                if (updated != null) {
                    session = updated
                    if (updated.status != VoiceSessionStatus.ACTIVE) {
                        status.value = "会话已失效"
                        break
                    }
                }
                runCatching { api.heartbeatPresentation(config.nodeId, presentationId) }
            }
        }
    }

    private fun startCapture() {
        val recognizer = asr ?: return
        if (!mic.start()) {
            status.value = "麦克风初始化失败"
            return
        }
        capturing = true
        val frame = ShortArray(mic.frameSamples)
        val sampleRate = mic.sampleRate
        var voicedFrames = 0
        var playbackSpeechFrames = 0
        scope.launch(Dispatchers.IO) {
            while (capturing) {
                val read = mic.read(frame)
                if (read <= 0) continue
                val voiced = rms(frame, read) > ENERGY_THRESHOLD
                if (player.isPlaying()) {
                    if (bargeIn && voiced) {
                        playbackSpeechFrames += read
                        if (playbackSpeechFrames * 1000L / sampleRate >= BARGE_IN_MS) {
                            playbackSpeechFrames = 0
                            launch { interrupt() }
                        }
                    } else {
                        playbackSpeechFrames = 0
                    }
                    // Half-duplex fallback: never feed the recognizer its own playback.
                    continue
                }
                playbackSpeechFrames = 0
                if (voiced) voicedFrames += read
                val floats = FloatArray(read) { frame[it] / 32768.0f }
                runCatching {
                    recognizer.accept(floats, sampleRate)
                    recognizer.decodeWhileReady()
                    val text = recognizer.text()
                    if (text.isNotBlank() && text != draft.value) draft.value = text
                    if (recognizer.isEndpoint()) {
                        val finalText = text.trim()
                        val audioMs = voicedFrames * 1000 / sampleRate
                        voicedFrames = 0
                        recognizer.reset()
                        draft.value = ""
                        if (finalText.isNotEmpty()) {
                            submitTurn(
                                finalText,
                                VoiceTurnInput.Asr(
                                    startedAt = nowIso(),
                                    endedAt = nowIso(),
                                    audioDurationMs = audioMs.coerceIn(0, 45_000),
                                    asrModelId = ModelRepository.MODEL_ID,
                                    language = "zh",
                                    discontinuity = false,
                                ),
                                VoiceEndReason.ADAPTIVE_ENDPOINT,
                            )
                        }
                    }
                }
            }
        }
    }

    private fun stopCapture() {
        capturing = false
        mic.stop()
    }

    private fun submitTurn(
        text: String,
        input: VoiceTurnInput,
        endReason: VoiceEndReason,
    ) {
        turnCancelled = false
        scope.launch {
            sessionLock.withLock {
                val currentSession = session ?: return@withLock
                val current = runCatching { api.getCompanion(config.companionId) }.getOrNull()
                    ?: return@withLock
                companion = current
                val turnId = UUID.randomUUID().toString()
                lastTurnId = turnId
                status.value = "思考中"
                try {
                    api.submitVoiceTurn(
                        config.nodeId,
                        currentSession.sessionId,
                        VoiceTurnSubmit(
                            commandId = UUID.randomUUID().toString(),
                            turnId = turnId,
                            sessionGeneration = currentSession.sessionGeneration,
                            expectedOutputGeneration = currentSession.outputGeneration,
                            epoch = current.epoch,
                            expectedRevision = current.revision,
                            text = text,
                            endReason = endReason,
                            input = input,
                            continuationOfTurnId = null,
                        ),
                    )
                } catch (error: Throwable) {
                    status.value = "提交失败: ${error.message}"
                    return@withLock
                }
                streamTurn(currentSession, turnId)
                companion = runCatching { api.getCompanion(config.companionId) }.getOrNull()
                session = runCatching {
                    api.getVoiceSession(config.nodeId, currentSession.sessionId)
                }.getOrNull() ?: session
                status.value = if (capturing) "正在听" else "空闲"
            }
        }
    }

    private suspend fun streamTurn(
        currentSession: VoiceSessionSnapshot,
        turnId: String,
    ) {
        val segments = Channel<VoiceSegmentInfo>(Channel.UNLIMITED)
        val consumer = scope.launch {
            for (segment in segments) {
                if (turnCancelled) continue
                if (segment.status != VoiceSegmentStatus.READY) continue
                playSegment(currentSession.sessionId, turnId, segment)
            }
        }
        try {
            api.streamVoiceTurn(config.nodeId, currentSession.sessionId, turnId) { event ->
                when (event) {
                    is VoiceStreamEvent.TextDelta -> reply.value = event.value.text
                    is VoiceStreamEvent.SpeechSegment -> segments.trySend(event.value)
                    is VoiceStreamEvent.VoiceEnd -> segments.close()
                    is VoiceStreamEvent.Error -> status.value = "语音错误: ${event.value.message}"
                    else -> {}
                }
            }
        } catch (error: Throwable) {
            status.value = "事件流中断: ${error.message}"
        } finally {
            segments.close()
            consumer.join()
        }
    }

    private suspend fun playSegment(
        sessionId: String,
        turnId: String,
        segment: VoiceSegmentInfo,
    ) {
        val current = session ?: return
        if (turnCancelled) return
        if (segment.outputGeneration != null && segment.outputGeneration != current.outputGeneration) {
            return
        }
        val pcm = runCatching {
            api.downloadVoicePcm(config.nodeId, sessionId, turnId, segment)
        }.getOrElse {
            status.value = "音频下载失败: ${it.message}"
            return
        }
        if (turnCancelled) return
        player.start(pcm.sampleRate, pcm.channels)
        playing.value = true
        reportPlayback(sessionId, turnId, segment, 0, VoicePlaybackStatus.STARTED)
        runCatching { player.write(pcm.bytes) }
        reportPlayback(sessionId, turnId, segment, pcm.sampleCount, VoicePlaybackStatus.COMPLETED)
        playing.value = false
    }

    private fun reportPlayback(
        sessionId: String,
        turnId: String,
        segment: VoiceSegmentInfo,
        played: Long,
        status: VoicePlaybackStatus,
    ) {
        scope.launch {
            runCatching {
                api.reportVoicePlayback(
                    config.nodeId,
                    sessionId,
                    VoicePlaybackReport(
                        commandId = UUID.randomUUID().toString(),
                        turnId = turnId,
                        outputGeneration = segment.outputGeneration ?: 0,
                        segmentSequence = segment.segmentSequence,
                        playedSamples = played.coerceIn(0, 3_600_000_000L),
                        status = status,
                    ),
                )
            }
        }
    }

    private suspend fun stopInternal(notifyServer: Boolean) {
        stopCapture()
        heartbeatJob?.cancel()
        heartbeatJob = null
        val current = session
        if (notifyServer && current != null && current.status == VoiceSessionStatus.ACTIVE) {
            runCatching {
                api.stopVoiceSession(
                    config.nodeId,
                    current.sessionId,
                    VoiceStopRequest(UUID.randomUUID().toString()),
                )
            }
        }
        session = null
        player.release()
        playing.value = false
        runCatching { api.releasePresentation(config.nodeId, presentationId) }
        nodeJob?.cancel()
        nodeJob = null
        asr?.release()
        asr = null
        draft.value = ""
        reply.value = ""
    }

    private fun rms(samples: ShortArray, count: Int): Double {
        if (count <= 0) return 0.0
        var sum = 0.0
        for (index in 0 until count) {
            val value = samples[index].toDouble()
            sum += value * value
        }
        return sqrt(sum / count)
    }

    private fun nowIso(): String = Instant.now().toString()

    companion object {
        private const val ENERGY_THRESHOLD = 600.0
        private const val BARGE_IN_MS = 300L
    }
}
