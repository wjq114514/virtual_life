package dev.virtuallife.chat

import android.os.SystemClock
import dev.virtuallife.node.AckPhase
import dev.virtuallife.node.ApiException
import dev.virtuallife.node.CompanionCreate
import dev.virtuallife.node.CompanionSnapshot
import dev.virtuallife.node.DialogueHistoryPage
import dev.virtuallife.node.DialogueRequest
import dev.virtuallife.node.DialogueResult
import dev.virtuallife.node.DialogueStreamEvent
import dev.virtuallife.node.LeaseGate
import dev.virtuallife.node.LeaseRenew
import dev.virtuallife.node.MonotonicClock
import dev.virtuallife.node.NodeHeartbeat
import dev.virtuallife.node.PersonalityHistoryMessage
import dev.virtuallife.node.PresentationOpen
import dev.virtuallife.node.RuntimeApi
import dev.virtuallife.node.TransferAck
import dev.virtuallife.node.TransferPrepare
import java.io.IOException
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient

/**
 * Owns one node registration, one native presentation, the companion claim, the output lease
 * and the dialogue requests for a single device.
 *
 * [LeaseGate] is the only authority on whether this device may present a reply: permits come
 * from measured REST lease replies, never from historical state, and nothing is rendered
 * without a live permit for the epoch that produced the text.
 */
class ChatClient(
    private val config: ChatConfig,
    http: OkHttpClient,
) {
    /** Progress and failure notices; installed by the host that owns this client. */
    @Volatile
    var onStatus: (String) -> Unit = {}
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val clock = MonotonicClock { SystemClock.elapsedRealtimeNanos() }
    private val api = RuntimeApi(config.baseUrl, config.token, clock, http)
    val gate = LeaseGate(config.nodeId, clock)
    val presentationId = "chat-${config.nodeId}"

    private val loops = CopyOnWriteArrayList<Job>()

    @Volatile
    private var companion: CompanionSnapshot? = null

    @Volatile
    private var streamingEnabled = false

    @Volatile
    var running: Boolean = false
        private set

    fun scope(): CoroutineScope = scope

    fun canPresent(companionId: String, epoch: Long): Boolean = gate.canOutput(companionId, epoch)

    /** Idempotent connect sequence; a failure surfaces through [onStatus] and tears the session down. */
    fun connect() {
        if (running) return
        scope.launch {
            try {
                api.registerNode(ChatRequests.registration(config))
                loops += scope.launch { nodeHeartbeat() }
                api.openPresentation(config.nodeId, PresentationOpen(presentationId = presentationId))
                loops += scope.launch { presentationHeartbeat() }
                ensureCompanion()
                waitForNodeOnline()
                claimCompanion()
                streamingEnabled = detectStreaming()
                loops += scope.launch { leaseLoop() }
                running = true
                onStatus("已连接")
            } catch (error: Throwable) {
                onStatus("连接失败: ${error.message}")
                close()
            }
        }
    }

    fun close() {
        running = false
        loops.forEach { it.cancel() }
        loops.clear()
        companion = null
        scope.launch { runCatching { api.releasePresentation(config.nodeId, presentationId) } }
    }

    /** Re-reads the companion so a request always carries the current epoch and revision. */
    suspend fun freshSnapshot(): CompanionSnapshot {
        val snapshot = api.getCompanion(config.companionId)
        companion = snapshot
        return snapshot
    }

    /**
     * Submits one user turn. [onDelta] receives the cumulative reply text as it arrives; the
     * returned result is the durable outcome and the only thing that may be committed.
     */
    suspend fun send(
        text: String,
        history: List<PersonalityHistoryMessage>,
        onDelta: (String) -> Unit = {},
    ): DialogueResult {
        val snapshot = freshSnapshot()
        val commandId = UUID.randomUUID().toString()
        val request = ChatRequests.dialogue(
            config, snapshot, presentationId, commandId, text, history,
        )
        return if (streamingEnabled) {
            // A dropped transport is not a failed turn: the command is durable once accepted, so
            // read the stored result back instead of replaying it as a new model call.
            recover(commandId, MAX_STREAM_RECOVERIES) {
                api.streamDialogue(config.companionId, request) { event ->
                    when (event) {
                        is DialogueStreamEvent.Delta -> onDelta(event.value.text)
                        is DialogueStreamEvent.Error -> throw IOException(event.value.error.message)
                        else -> Unit
                    }
                }
            }
        } else {
            val result = api.sendDialogue(config.companionId, request)
            onDelta(result.assistantMessage ?: "")
            result
        }
    }

    /** Bounded transport recovery; the model is never called twice for one command. */
    private suspend fun recover(
        commandId: String,
        attempts: Int,
        attempt: suspend () -> DialogueResult,
    ): DialogueResult {
        try {
            return attempt()
        } catch (error: CancellationException) {
            throw error
        } catch (_: IOException) {
            // fall through: recover the durable outcome instead of re-running the model
        }
        if (attempts <= 0) throw IllegalStateException("对话结果读取失败")
        delay(500)
        try {
            return api.getDialogueResult(config.companionId, commandId)
        } catch (_: IOException) {
            return recover(commandId, attempts - 1, attempt)
        }
    }

    /**
     * Presents one durable result. Returns false when this device no longer owns the companion
     * or holds no live lease for the epoch that produced the reply.
     */
    fun present(result: DialogueResult, render: () -> Unit): Boolean {
        val preview = result.preview
        if (preview.activeNodeId != config.nodeId) return false
        return gate.tryOutput(config.companionId, preview.epoch, render)
    }

    suspend fun history(beforeSequence: Long? = null, limit: Int = 20): DialogueHistoryPage =
        api.dialogueHistory(config.companionId, beforeSequence, limit)

    suspend fun companionSnapshot(): CompanionSnapshot? = companion ?: freshSnapshot()

    private suspend fun ensureCompanion() {
        try {
            api.createCompanion(CompanionCreate(config.companionId, "聊天伙伴"))
        } catch (error: ApiException) {
            if (error.statusCode != 409) throw error
        }
    }

    private suspend fun waitForNodeOnline() {
        val online = withTimeoutOrNull(NODE_ONLINE_TIMEOUT_MILLIS) {
            while (!api.getNode(config.nodeId).online) delay(200)
            true
        }
        check(online != null) { "节点注册超时" }
    }

    private suspend fun claimCompanion() {
        val current = api.getCompanion(config.companionId)
        if (current.activeNodeId == config.nodeId) {
            companion = current
            return
        }
        // A prepared transfer stays pending until the target acknowledges ready, and the
        // runtime refuses dialogue while a transfer is pending. The ready also binds this
        // device's live presentation, so the renderer is proven open before ownership moves.
        val transfer = api.prepareTransfer(
            config.companionId,
            TransferPrepare(
                commandId = UUID.randomUUID().toString(),
                targetNodeId = config.nodeId,
                expectedRevision = current.revision,
            ),
        )
        api.acknowledgeTransfer(
            transfer.transferId,
            TransferAck(
                nodeId = config.nodeId,
                phase = AckPhase.READY,
                presentationId = presentationId,
            ),
        )
        val active = withTimeoutOrNull(CLAIM_TIMEOUT_MILLIS) {
            var snapshot = api.getCompanion(config.companionId)
            while (snapshot.activeNodeId != config.nodeId) {
                delay(250)
                snapshot = api.getCompanion(config.companionId)
            }
            snapshot
        }
        check(active != null) { "未能接管伙伴输出权" }
        companion = active
    }

    /** Streaming is advertised under dialogue.streaming; a missing key means the JSON route. */
    private suspend fun detectStreaming(): Boolean = runCatching {
        api.capabilities().get("dialogue")
            ?.jsonObject
            ?.get("streaming")
            ?.jsonPrimitive
            ?.booleanOrNull ?: false
    }.getOrDefault(false)

    private suspend fun nodeHeartbeat() {
        while (currentCoroutineContext().isActive) {
            delay(NODE_HEARTBEAT_MILLIS)
            if (!running) break
            runCatching { api.heartbeat(config.nodeId, NodeHeartbeat()) }
                .onFailure { if (running) onStatus("节点心跳失败，尝试重连") }
        }
    }

    private suspend fun presentationHeartbeat() {
        while (currentCoroutineContext().isActive) {
            delay(PRESENTATION_HEARTBEAT_MILLIS)
            if (!running) break
            val beat = runCatching { api.heartbeatPresentation(config.nodeId, presentationId) }
            if (beat.isFailure) {
                runCatching { api.openPresentation(config.nodeId, PresentationOpen(presentationId = presentationId)) }
            }
        }
    }

    private suspend fun leaseLoop() {
        while (currentCoroutineContext().isActive) {
            delay(LEASE_POLL_MILLIS)
            if (!running) break
            val current = companion
            if (current == null) continue
            if (gate.permit(config.companionId, current.epoch) != null) continue
            try {
                val sample = api.renewLease(config.companionId, LeaseRenew(config.nodeId, current.epoch))
                gate.grant(sample)
            } catch (error: ApiException) {
                when (error.code) {
                    "ownership_conflict", "epoch_conflict", "lease_expired" ->
                        runCatching { claimCompanion() }
                    else -> onStatus("租约续期失败: ${error.code}")
                }
            } catch (_: IOException) {
                if (running) onStatus("租约续期失败，网络不可达")
            }
        }
    }

    companion object {
        private const val NODE_HEARTBEAT_MILLIS = 15_000L
        private const val PRESENTATION_HEARTBEAT_MILLIS = 5_000L
        private const val LEASE_POLL_MILLIS = 3_000L
        private const val NODE_ONLINE_TIMEOUT_MILLIS = 15_000L
        private const val CLAIM_TIMEOUT_MILLIS = 20_000L
        private const val MAX_STREAM_RECOVERIES = 3
    }
}
