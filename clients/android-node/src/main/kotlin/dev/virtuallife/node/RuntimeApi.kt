package dev.virtuallife.node

import java.io.IOException
import java.security.MessageDigest
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.KSerializer
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.serializer
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

/** Time samples use the same clock instance as [LeaseGate]. */
data class LeaseSample(
    val lease: OutputLease,
    val requestStartedNanos: Long,
    val responseReceivedNanos: Long,
)

class ApiException(val statusCode: Int, val code: String, message: String) :
    IOException("HTTP $statusCode ($code): $message")

/** The small contract needed by the session is easy to replace in host tests. */
interface NodeApi {
    suspend fun registerNode(body: NodeRegistration): NodeSnapshot
    suspend fun heartbeat(nodeId: String, body: NodeHeartbeat): NodeSnapshot
    suspend fun listCompanions(): List<CompanionSnapshot>
    suspend fun getCompanion(companionId: String): CompanionSnapshot
    suspend fun getTransfer(transferId: String): TransferSnapshot
    suspend fun acknowledgeTransfer(transferId: String, body: TransferAck): TransferSnapshot
    suspend fun renewLease(companionId: String, body: LeaseRenew): LeaseSample
}

/** Asynchronous REST client. Bearer credentials never appear in request URLs. */
class RuntimeApi(
    baseUrl: String,
    private val token: String,
    private val clock: MonotonicClock,
    val httpClient: OkHttpClient = OkHttpClient(),
) : NodeApi, NativePresentationApi {
    internal val base: HttpUrl = baseUrl.toHttpUrl()

    init {
        require(token.isNotBlank()) { "A runtime token is required" }
        require(base.username.isEmpty() && base.password.isEmpty() && base.query == null && base.fragment == null) {
            "The runtime base URL must not contain credentials, query parameters, or a fragment"
        }
    }

    override suspend fun registerNode(body: NodeRegistration): NodeSnapshot = post(listOf("v1", "nodes"), body)

    override suspend fun openPresentation(nodeId: String, body: PresentationOpen): PresentationSample {
        requireIdentifier(nodeId)
        val data = perform(request("POST", listOf("v1", "nodes", nodeId, "presentations"),
            NodeJson.encodeToString(body)))
        return PresentationSample(
            NodeJson.decodeFromString<PresentationSnapshot>(data.body),
            data.startedNanos,
            data.receivedNanos,
        )
    }

    override suspend fun heartbeatPresentation(nodeId: String, presentationId: String): PresentationSample {
        requireIdentifier(nodeId)
        requireIdentifier(presentationId)
        val data = perform(request(
            "POST",
            listOf("v1", "nodes", nodeId, "presentations", presentationId, "heartbeat"),
            "{}",
        ))
        return PresentationSample(
            NodeJson.decodeFromString<PresentationSnapshot>(data.body),
            data.startedNanos,
            data.receivedNanos,
        )
    }

    override suspend fun releasePresentation(nodeId: String, presentationId: String): PresentationSnapshot {
        requireIdentifier(nodeId)
        requireIdentifier(presentationId)
        return decode(
            perform(request("DELETE", listOf("v1", "nodes", nodeId, "presentations", presentationId))),
            serializer(),
        )
    }

    /** Web access uses the server's canonical node.nodeId; accessId is not a node ID. */
    suspend fun openWebAccess(body: WebAccessRequest): WebAccessSnapshot =
        post(listOf("v1", "nodes", "access", "web"), body)

    suspend fun heartbeatWebAccess(nodeId: String, accessId: String, body: WebAccessHeartbeat = WebAccessHeartbeat()): WebAccessSnapshot =
        post(listOf("v1", "nodes", nodeId, "access", accessId, "heartbeat"), body)

    /** Closes only this access; the device and native client remain registered. */
    suspend fun releaseWebAccess(nodeId: String, accessId: String): NodeSnapshot =
        decode(perform(request("DELETE", listOf("v1", "nodes", nodeId, "access", accessId))), serializer())

    /** Linux deployment operations require an administrator token. Probe sends no SSH auth. */
    suspend fun probeLinuxHostKey(body: LinuxHostKeyRequest): LinuxHostKey =
        post(listOf("v1", "deployments", "linux", "host-key"), body)

    suspend fun inspectLinuxHost(body: LinuxInspectRequest): LinuxInspection =
        post(listOf("v1", "deployments", "linux", "inspect"), body)

    /** On uncertain completion query the same commandId; never generate a replacement retry ID. */
    suspend fun deployLinuxNode(body: LinuxDeploymentRequest): LinuxDeploymentJob =
        post(listOf("v1", "deployments", "linux"), body)

    suspend fun getLinuxDeployment(commandId: String): LinuxDeploymentJob =
        get(listOf("v1", "deployments", "linux", commandId))

    /** Revokes this job's reporter credential, without uninstalling the remote service. */
    suspend fun revokeLinuxDeploymentCredential(commandId: String): LinuxDeploymentJob =
        decode(perform(request("DELETE", listOf("v1", "deployments", "linux", commandId, "credential"))), serializer())

    suspend fun listNodes(): List<NodeSnapshot> = get(listOf("v1", "nodes"))

    suspend fun getNode(nodeId: String): NodeSnapshot = get(listOf("v1", "nodes", nodeId))

    override suspend fun heartbeat(nodeId: String, body: NodeHeartbeat): NodeSnapshot =
        post(listOf("v1", "nodes", nodeId, "heartbeat"), body)

    suspend fun graph(): GraphSnapshot = get(listOf("v1", "graph"))

    suspend fun capabilities(): JsonObject = get(listOf("v1", "capabilities"))

    suspend fun createCompanion(body: CompanionCreate): CompanionSnapshot = post(listOf("v1", "companions"), body)

    override suspend fun listCompanions(): List<CompanionSnapshot> = get(listOf("v1", "companions"))

    override suspend fun getCompanion(companionId: String): CompanionSnapshot = get(listOf("v1", "companions", companionId))

    suspend fun updateContext(companionId: String, body: ContextUpdate): CompanionSnapshot =
        post(listOf("v1", "companions", companionId, "context"), body)

    suspend fun getPersonality(companionId: String): PersonalitySnapshot =
        get(listOf("v1", "companions", companionId, "personality"))

    suspend fun updatePersonality(companionId: String, body: PersonalityUpdate): PersonalitySnapshot =
        post(listOf("v1", "companions", companionId, "personality"), body)

    suspend fun importPersonality(companionId: String, body: PersonalityCardImport): PersonalitySnapshot =
        post(listOf("v1", "companions", companionId, "personality", "import"), body)

    suspend fun getPersonalityState(companionId: String): PersonalityStateSnapshot =
        get(listOf("v1", "companions", companionId, "personality", "state"))

    suspend fun getEmotion(companionId: String): EmotionSnapshot =
        get(listOf("v1", "companions", companionId, "emotion"))

    suspend fun getEmotionExperiences(companionId: String, afterSequence: Long = 0, limit: Int = 50): EmotionExperiencePage =
        get(listOf("v1", "companions", companionId, "emotion", "experiences"),
            mapOf("after_sequence" to afterSequence.toString(), "limit" to limit.toString()))

    suspend fun invalidateEmotion(companionId: String, eventId: String, body: EmotionInvalidation): EmotionSnapshot =
        post(listOf("v1", "companions", companionId, "emotion", "experiences", eventId, "invalidate"), body)

    /** Adjusts interpretation; original dialogue remains. Historical snapshots do not grant output rights. */
    suspend fun correctEmotion(companionId: String, eventId: String, body: EmotionCorrection): EmotionSnapshot =
        post(listOf("v1", "companions", companionId, "emotion", "experiences", eventId, "correct"), body)

    /** Read-only L2 aggregate; it only informs prompt and participation and never edits the profile. */
    suspend fun getSlowVariables(companionId: String): SlowVariableSnapshot =
        get(listOf("v1", "companions", companionId, "slow-variables"))

    /** Local model-free inner activity; it never grants output permission. */
    suspend fun getInnerLife(companionId: String): InnerLifeSnapshot =
        get(listOf("v1", "companions", companionId, "inner-life"))

    suspend fun getInnerActivities(
        companionId: String,
        afterSequence: Long = 0,
        limit: Int = 50,
    ): InnerActivityPage =
        get(
            listOf("v1", "companions", companionId, "inner-life", "activities"),
            mapOf("after_sequence" to afterSequence.toString(), "limit" to limit.toString()),
        )

    suspend fun updatePersonalityState(companionId: String, body: PersonalityStateUpdate): PersonalityStateSnapshot =
        post(listOf("v1", "companions", companionId, "personality", "state"), body)

    suspend fun previewPersonality(companionId: String, body: PersonalityPreview): PersonalityPreviewResult =
        post(listOf("v1", "companions", companionId, "personality", "preview"), body)

    suspend fun getPersonalityHistory(
        companionId: String,
        beforeRevision: Long? = null,
        limit: Int = 20,
    ): PersonalityHistoryPage =
        get(
            listOf("v1", "companions", companionId, "personality", "history"),
            buildMap {
                beforeRevision?.let { put("before_revision", it.toString()) }
                put("limit", limit.toString())
            },
        )

    suspend fun listPersonalityProposals(
        companionId: String,
        status: PersonalityProposalStatus? = null,
        afterSequence: Long = 0,
        limit: Int = 20,
    ): PersonalityEvolutionPage =
        get(
            listOf("v1", "companions", companionId, "personality", "proposals"),
            buildMap {
                status?.let { put("status", NodeJson.encodeToString(it).trim('"')) }
                put("after_sequence", afterSequence.toString())
                put("limit", limit.toString())
            },
        )

    suspend fun getPersonalityProposal(companionId: String, proposalId: String): PersonalityEvolutionProposal =
        get(listOf("v1", "companions", companionId, "personality", "proposals", proposalId))

    /** Generates bounded proposals from local evidence; it never edits the profile itself. */
    suspend fun refreshPersonalityProposals(
        companionId: String,
        body: PersonalityEvolutionRefresh,
    ): PersonalityEvolutionRefreshResult =
        post(listOf("v1", "companions", companionId, "personality", "proposals", "refresh"), body)

    suspend fun acceptPersonalityProposal(
        companionId: String,
        proposalId: String,
        body: PersonalityEvolutionDecision,
    ): PersonalityEvolutionDecisionResult =
        post(listOf("v1", "companions", companionId, "personality", "proposals", proposalId, "accept"), body)

    suspend fun rejectPersonalityProposal(
        companionId: String,
        proposalId: String,
        body: PersonalityEvolutionRejection,
    ): PersonalityEvolutionDecisionResult =
        post(listOf("v1", "companions", companionId, "personality", "proposals", proposalId, "reject"), body)

    suspend fun sendDialogue(companionId: String, body: DialogueRequest): DialogueResult =
        post(listOf("v1", "companions", companionId, "dialogue"), body)

    /**
     * Events run on the OkHttp IO thread. Delta text replaces the entire temporary reply and
     * grants no output rights. Clear temporary text on discarded/error/cancellation; enforce
     * LeaseGate even after Result. Cancellation closes transport, not the server command.
     * Never automatically retries; recover ambiguous completion using getDialogueResult.
     */
    suspend fun streamDialogue(
        companionId: String,
        body: DialogueRequest,
        onEvent: (DialogueStreamEvent) -> Unit,
    ): DialogueResult = suspendCancellableCoroutine { continuation ->
        val outgoing = request("POST", listOf("v1", "companions", companionId, "dialogue", "stream"),
            NodeJson.encodeToString(body)).newBuilder().header("Accept", "text/event-stream").build()
        val call = httpClient.newBuilder().retryOnConnectionFailure(false).build().newCall(outgoing)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    val result = response.use {
                        val responseBody = it.body ?: throw IOException("Runtime returned an empty response")
                        if (!it.isSuccessful) {
                            val error = runCatching {
                                NodeJson.parseToJsonElement(responseBody.string()).jsonObject["error"]?.jsonObject
                            }.getOrNull()
                            throw ApiException(it.code,
                                error?.get("code")?.jsonPrimitive?.content ?: "http_error",
                                error?.get("message")?.jsonPrimitive?.content ?: "Runtime rejected the request")
                        }
                        val type = responseBody.contentType()
                        if (type?.type != "text" || type.subtype != "event-stream") {
                            throw IOException("Runtime did not return text/event-stream")
                        }
                        responseBody.charStream().buffered().use { reader ->
                            readDialogueStream(reader, companionId, body.commandId) { event ->
                                if (!continuation.isActive) throw IOException("Dialogue stream cancelled")
                                onEvent(event)
                            }
                        }
                    }
                    if (continuation.isActive) continuation.resume(result)
                } catch (error: Exception) {
                    if (continuation.isActive) continuation.resumeWithException(error)
                }
            }
        })
    }

    suspend fun createVoiceSession(nodeId: String, body: VoiceSessionCreate): VoiceSessionSnapshot {
        requireIdentifier(nodeId)
        return post(listOf("v1", "nodes", nodeId, "voice", "sessions"), body)
    }

    suspend fun getVoiceSession(nodeId: String, sessionId: String): VoiceSessionSnapshot {
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        return get(listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId))
    }

    suspend fun heartbeatVoiceSession(
        nodeId: String,
        sessionId: String,
        body: VoiceHeartbeat,
    ): VoiceSessionSnapshot {
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        return post(listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId, "heartbeat"), body)
    }

    suspend fun stopVoiceSession(
        nodeId: String,
        sessionId: String,
        body: VoiceStopRequest,
    ): VoiceSessionSnapshot {
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        return post(listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId, "stop"), body)
    }

    suspend fun submitVoiceTurn(
        nodeId: String,
        sessionId: String,
        body: VoiceTurnSubmit,
    ): VoiceTurnReceipt {
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        return post(listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId, "turns"), body)
    }

    suspend fun getVoiceTurn(nodeId: String, sessionId: String, turnId: String): VoiceTurnSnapshot {
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        requireIdentifier(turnId)
        return get(listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId, "turns", turnId))
    }

    /** EOF before voice_end is an incomplete turn, never a replay signal. */
    suspend fun streamVoiceTurn(
        nodeId: String,
        sessionId: String,
        turnId: String,
        afterSequence: Long = 0,
        onEvent: (VoiceStreamEvent) -> Unit,
    ): Unit = suspendCancellableCoroutine { continuation ->
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        requireIdentifier(turnId)
        require(afterSequence >= 0)
        val outgoing = request(
            "GET",
            listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId, "turns", turnId, "events"),
            query = mapOf("after_sequence" to afterSequence.toString()),
        ).newBuilder().header("Accept", "text/event-stream").build()
        val call = httpClient.newBuilder().retryOnConnectionFailure(false).build().newCall(outgoing)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    response.use {
                        val body = it.body ?: throw IOException("Runtime returned an empty voice event stream")
                        if (!it.isSuccessful) throw voiceApiException(it.code, body.string())
                        val type = body.contentType()
                        if (type?.type != "text" || type.subtype != "event-stream") {
                            throw IOException("Runtime did not return text/event-stream")
                        }
                        body.charStream().buffered().use { reader ->
                            readVoiceStream(reader, sessionId, turnId, afterSequence, onEvent)
                        }
                    }
                    if (continuation.isActive) continuation.resume(Unit)
                } catch (error: Exception) {
                    if (continuation.isActive) continuation.resumeWithException(error)
                }
            }
        })
    }

    /** Uses the fixed segment route; audio_path is untrusted display metadata. */
    suspend fun downloadVoicePcm(
        nodeId: String,
        sessionId: String,
        turnId: String,
        segment: VoiceSegmentInfo,
        maxBytes: Int = MAX_VOICE_PCM_BYTES,
    ): VoicePcmSegment = suspendCancellableCoroutine { continuation ->
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        requireIdentifier(turnId)
        require(maxBytes in 1..MAX_VOICE_PCM_BYTES)
        if (segment.status != VoiceSegmentStatus.READY ||
            segment.mediaType.substringBefore(';').trim() != "audio/pcm" ||
            !segment.sha256.matches(Regex("[0-9a-fA-F]{64}"))
        ) {
            continuation.resumeWithException(IOException("Voice segment is not valid PCM"))
            return@suspendCancellableCoroutine
        }
        val expectedBytes = runCatching {
            Math.multiplyExact(Math.multiplyExact(segment.sampleCount, segment.channels.toLong()), 2L)
        }.getOrNull()
        if (segment.sampleRate <= 0 || segment.channels !in 1..2 || expectedBytes == null ||
            expectedBytes <= 0 || expectedBytes > maxBytes
        ) {
            continuation.resumeWithException(IOException("Voice PCM metadata exceeds the download limit"))
            return@suspendCancellableCoroutine
        }
        val outgoing = request(
            "GET",
            listOf(
                "v1", "nodes", nodeId, "voice", "sessions", sessionId, "turns", turnId,
                "segments", segment.segmentSequence.toString(), "audio",
            ),
        ).newBuilder().header("Accept", segment.mediaType).build()
        val call = httpClient.newBuilder().retryOnConnectionFailure(false).build().newCall(outgoing)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    val pcm = response.use {
                        val body = it.body ?: throw IOException("Runtime returned an empty PCM response")
                        if (!it.isSuccessful) throw voiceApiException(it.code, body.string())
                        val declaredLength = it.header("Content-Length")?.toLongOrNull()
                            ?: throw IOException("PCM response is missing Content-Length")
                        if (declaredLength != expectedBytes || declaredLength > maxBytes) {
                            throw IOException("PCM Content-Length does not match its metadata")
                        }
                        val contentType = body.contentType()?.toString()
                            ?.substringBefore(';')?.trim() ?: ""
                        if (contentType != segment.mediaType.substringBefore(';').trim()) {
                            throw IOException("PCM media type does not match its metadata")
                        }
                        if (it.header("X-Audio-Sample-Rate")?.toIntOrNull() != segment.sampleRate ||
                            it.header("X-Audio-Channels")?.toIntOrNull() != segment.channels ||
                            it.header("X-Audio-Sample-Count")?.toLongOrNull() != segment.sampleCount
                        ) {
                            throw IOException("PCM response sample metadata does not match the event")
                        }
                        val digestHeader = it.header("X-Audio-SHA256")
                            ?: throw IOException("PCM response is missing its digest")
                        if (!digestHeader.equals(segment.sha256, ignoreCase = true)) {
                            throw IOException("PCM response digest does not match the event")
                        }
                        val bytes = ByteArray(expectedBytes.toInt())
                        val input = body.byteStream()
                        var offset = 0
                        while (offset < bytes.size) {
                            val count = input.read(bytes, offset, minOf(8192, bytes.size - offset))
                            if (count < 0) break
                            offset += count
                        }
                        if (offset != bytes.size || input.read() != -1) {
                            throw IOException("PCM response ended at an unexpected byte count")
                        }
                        val actualDigest = sha256Hex(bytes)
                        if (!actualDigest.equals(segment.sha256, ignoreCase = true)) {
                            throw IOException("PCM content digest verification failed")
                        }
                        VoicePcmSegment(
                            bytes,
                            contentType,
                            segment.sampleRate,
                            segment.channels,
                            segment.sampleCount,
                            actualDigest,
                            segment.outputGeneration,
                        )
                    }
                    if (continuation.isActive) continuation.resume(pcm)
                } catch (error: Exception) {
                    if (continuation.isActive) continuation.resumeWithException(error)
                }
            }
        })
    }

    suspend fun interruptVoiceTurn(
        nodeId: String,
        sessionId: String,
        body: VoiceInterruptRequest,
    ): VoiceInterruptReceipt {
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        return post(listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId, "interrupt"), body)
    }

    suspend fun reportVoicePlayback(
        nodeId: String,
        sessionId: String,
        body: VoicePlaybackReport,
    ): VoicePlaybackReceipt {
        requireIdentifier(nodeId)
        requireIdentifier(sessionId)
        return post(listOf("v1", "nodes", nodeId, "voice", "sessions", sessionId, "playback"), body)
    }

    private fun readDialogueStream(
        reader: java.io.BufferedReader,
        companionId: String,
        commandId: String,
        onEvent: (DialogueStreamEvent) -> Unit,
    ): DialogueResult {
        var event = "message"
        val data = StringBuilder()
        var sequence = 0L
        var frameSize = 0
        while (true) {
            val line = StringBuilder()
            while (true) {
                val char = reader.read()
                if (char == -1) throw IOException("Dialogue stream ended without a result")
                if (char == 10) break
                line.append(char.toChar())
                if (line.length > 262144) throw IOException("Dialogue stream line too large")
            }
            val text = line.toString().removeSuffix("\r")
            frameSize += text.length + 1
            if (frameSize > 1048576) throw IOException("Dialogue stream event too large")
            if (text.isEmpty()) {
                if (data.isNotEmpty()) {
                    val payload = data.toString().removeSuffix("\n")
                    val decoded: DialogueStreamEvent? = when (event) {
                        "status" -> DialogueStreamEvent.Status(NodeJson.decodeFromString(payload))
                        "snapshot" -> DialogueStreamEvent.Snapshot(NodeJson.decodeFromString(payload))
                        "delta" -> DialogueStreamEvent.Delta(NodeJson.decodeFromString(payload))
                        "result" -> DialogueStreamEvent.Result(NodeJson.decodeFromString(payload))
                        "error" -> DialogueStreamEvent.Error(NodeJson.decodeFromString(payload))
                        else -> null
                    }
                    if (decoded != null) {
                        val identity = NodeJson.parseToJsonElement(payload).jsonObject
                        if (identity["command_id"]?.jsonPrimitive?.content != commandId ||
                            identity["companion_id"]?.jsonPrimitive?.content != companionId) {
                            throw IOException("Dialogue stream identity mismatch")
                        }
                        if (decoded is DialogueStreamEvent.Delta) {
                            if (decoded.value.sequence != sequence + 1) throw IOException("Dialogue stream sequence mismatch")
                            sequence = decoded.value.sequence
                        }
                        onEvent(decoded)
                        if (decoded is DialogueStreamEvent.Error) {
                            throw ApiException(decoded.value.error.statusCode,
                                decoded.value.error.code, decoded.value.error.message)
                        }
                        if (decoded is DialogueStreamEvent.Result) return decoded.value
                    }
                }
                event = "message"
                data.setLength(0)
                frameSize = 0
            } else if (!text.startsWith(":")) {
                val field = text.substringBefore(':')
                val value = text.substringAfter(':', "").removePrefix(" ")
                when (field) {
                    "event" -> event = value
                    "data" -> data.append(value).append('\n')
                }
            }
        }
    }

    private fun readVoiceStream(
        reader: java.io.BufferedReader,
        sessionId: String,
        turnId: String,
        afterSequence: Long,
        onEvent: (VoiceStreamEvent) -> Unit,
    ) {
        var lastSequence = afterSequence
        var sessionGeneration: Long? = null
        var outputGeneration: Long? = null
        var ended = false
        readSseFrames(reader) { name, payload ->
            if (ended) throw IOException("Voice stream continued after voice_end")
            val data = runCatching { NodeJson.parseToJsonElement(payload) as? JsonObject }
                .getOrNull() ?: throw IOException("Voice stream event is not a JSON object")
            val actualSessionId = data.string("session_id")
                ?: throw IOException("Voice stream event is missing session_id")
            val actualTurnId = data.string("turn_id")
                ?: throw IOException("Voice stream event is missing turn_id")
            val actualSessionGeneration = data.long("session_generation")
                ?: throw IOException("Voice stream event is missing session_generation")
            val actualOutputGeneration = data.long("output_generation")
                ?: throw IOException("Voice stream event is missing output_generation")
            val sequence = data.long("sequence")
                ?: throw IOException("Voice stream event is missing sequence")
            if (actualSessionId != sessionId || actualTurnId != turnId ||
                actualSessionGeneration < 0 || actualOutputGeneration < 0 || sequence < 0
            ) {
                throw IOException("Voice stream identity is invalid")
            }
            if (sessionGeneration != null && sessionGeneration != actualSessionGeneration ||
                outputGeneration != null && outputGeneration != actualOutputGeneration
            ) {
                throw IOException("Voice stream generation changed")
            }
            sessionGeneration = actualSessionGeneration
            outputGeneration = actualOutputGeneration

            val stateSnapshot = name == "snapshot" && "turn" in data
            if (stateSnapshot) {
                if (sequence < afterSequence) throw IOException("Voice snapshot precedes the requested cursor")
            } else {
                if (sequence < lastSequence ||
                    (sequence == lastSequence && name != "voice_end")
                ) {
                    throw IOException("Voice stream sequence moved backwards or repeated")
                }
                lastSequence = sequence
            }

            val event = when (name) {
                "snapshot" -> if (stateSnapshot) {
                    VoiceStreamEvent.Snapshot(NodeJson.decodeFromString(payload))
                } else {
                    VoiceStreamEvent.Preview(NodeJson.decodeFromString(payload))
                }
                "status" -> VoiceStreamEvent.Status(NodeJson.decodeFromString(payload))
                "text_delta" -> VoiceStreamEvent.TextDelta(NodeJson.decodeFromString(payload))
                "dialogue_result" -> VoiceStreamEvent.DialogueResult(NodeJson.decodeFromString(payload))
                "speech_segment" -> VoiceStreamEvent.SpeechSegment(NodeJson.decodeFromString(payload))
                "speech_end" -> VoiceStreamEvent.SpeechEnd(NodeJson.decodeFromString(payload))
                "interrupted" -> VoiceStreamEvent.Interrupted(NodeJson.decodeFromString(payload))
                "voice_end" -> VoiceStreamEvent.VoiceEnd(NodeJson.decodeFromString(payload))
                "error" -> VoiceStreamEvent.Error(NodeJson.decodeFromString(payload))
                else -> null
            }
            if (event != null) onEvent(event)
            if (event is VoiceStreamEvent.VoiceEnd) ended = true
        }
        if (!ended) throw IOException("Voice stream ended without voice_end")
    }

    private fun readSseFrames(
        reader: java.io.BufferedReader,
        onFrame: (String, String) -> Unit,
    ) {
        var event = "message"
        val data = StringBuilder()
        var frameSize = 0
        while (true) {
            val line = StringBuilder()
            while (true) {
                val char = reader.read()
                if (char == -1) {
                    if (data.isNotEmpty()) onFrame(event, data.toString().removeSuffix("\n"))
                    return
                }
                if (char == 10) break
                line.append(char.toChar())
                if (line.length > 262144) throw IOException("Voice stream line too large")
            }
            val text = line.toString().removeSuffix("\r")
            frameSize += text.length + 1
            if (frameSize > 1048576) throw IOException("Voice stream event too large")
            if (text.isEmpty()) {
                if (data.isNotEmpty()) onFrame(event, data.toString().removeSuffix("\n"))
                event = "message"
                data.setLength(0)
                frameSize = 0
            } else if (!text.startsWith(":")) {
                val field = text.substringBefore(':')
                val value = text.substringAfter(':', "").removePrefix(" ")
                when (field) {
                    "event" -> event = value
                    "data" -> data.append(value).append('\n')
                }
            }
        }
    }

    private fun voiceApiException(statusCode: Int, body: String): ApiException {
        val error = runCatching {
            NodeJson.parseToJsonElement(body).jsonObject["error"]?.jsonObject
        }.getOrNull()
        val code = error?.get("code")?.jsonPrimitive?.content ?: "http_error"
        val message = error?.get("message")?.jsonPrimitive?.content ?: "Runtime rejected the voice request"
        return ApiException(statusCode, code, message)
    }

    private fun sha256Hex(bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
        val digits = "0123456789abcdef"
        return buildString(digest.size * 2) {
            digest.forEach { byte ->
                val value = byte.toInt() and 0xff
                append(digits[value ushr 4])
                append(digits[value and 0x0f])
            }
        }
    }

    private fun JsonObject.string(name: String): String? =
        runCatching { get(name)?.jsonPrimitive?.content }.getOrNull()

    private fun JsonObject.long(name: String): Long? = string(name)?.toLongOrNull()

    suspend fun getDialogueResult(companionId: String, commandId: String): DialogueResult =
        get(listOf("v1", "companions", companionId, "dialogue", commandId))

    /**
     * Completed dialogue turns, newest first; [beforeSequence] is the exclusive cursor from the
     * previous page's next_before_sequence. History grants no output rights.
     */
    suspend fun dialogueHistory(
        companionId: String,
        beforeSequence: Long? = null,
        limit: Int = 20,
    ): DialogueHistoryPage {
        require(limit in 1..100)
        require(beforeSequence == null || beforeSequence in 0..9_007_199_254_740_991L)
        val query = if (beforeSequence == null) {
            mapOf("limit" to limit.toString())
        } else {
            mapOf("before_sequence" to beforeSequence.toString(), "limit" to limit.toString())
        }
        return get(listOf("v1", "companions", companionId, "dialogue", "history"), query)
    }

    suspend fun prepareTransfer(companionId: String, body: TransferPrepare): TransferSnapshot =
        post(listOf("v1", "companions", companionId, "transfers"), body)

    override suspend fun getTransfer(transferId: String): TransferSnapshot = get(listOf("v1", "transfers", transferId))

    override suspend fun acknowledgeTransfer(transferId: String, body: TransferAck): TransferSnapshot =
        post(listOf("v1", "transfers", transferId, "ack"), body)

    suspend fun abortTransfer(transferId: String): TransferSnapshot =
        post(listOf("v1", "transfers", transferId, "abort"), JsonObject(emptyMap()))

    override suspend fun renewLease(companionId: String, body: LeaseRenew): LeaseSample {
        val data = perform(request("POST", listOf("v1", "companions", companionId, "lease"), NodeJson.encodeToString(body)))
        return LeaseSample(NodeJson.decodeFromString(data.body), data.startedNanos, data.receivedNanos)
    }

    suspend fun observations(nodeId: String, body: ObservationCreate): JsonObject =
        post(listOf("v1", "nodes", nodeId, "observations"), body)

    suspend fun observeAttention(nodeId: String, body: AttentionObservation): AttentionSnapshot =
        post(listOf("v1", "nodes", nodeId, "attention"), body)

    suspend fun getAttention(nodeId: String): AttentionSnapshot =
        get(listOf("v1", "nodes", nodeId, "attention"))

    suspend fun getFocus(nodeId: String): FocusSnapshot {
        requireIdentifier(nodeId)
        return get(listOf("v1", "nodes", nodeId, "focus"))
    }

    /** Screen-vision endpoints require an administrator token and a user-initiated session. */
    suspend fun createScreenSession(nodeId: String, body: ScreenSessionCreate): ScreenSessionReceipt =
        post(listOf("v1", "nodes", nodeId, "screen", "sessions"), body)

    suspend fun getScreenSession(nodeId: String, sessionId: String): ScreenSessionSnapshot =
        get(listOf("v1", "nodes", nodeId, "screen", "sessions", sessionId))

    suspend fun stopScreenSession(nodeId: String, sessionId: String, body: ScreenCommand): ScreenSessionReceipt =
        post(listOf("v1", "nodes", nodeId, "screen", "sessions", sessionId, "stop"), body)

    suspend fun purgeScreenSession(nodeId: String, sessionId: String, body: ScreenCommand): ScreenPurgeReceipt =
        post(listOf("v1", "nodes", nodeId, "screen", "sessions", sessionId, "purge"), body)

    suspend fun registerScreenUpload(nodeId: String, body: ScreenUploadRegister): ScreenUploadReceipt =
        post(listOf("v1", "nodes", nodeId, "screen", "uploads"), body)

    /** Uploads the raw encoded image; [mediaType] must match the registered upload media_type. */
    suspend fun putScreenUploadContent(
        nodeId: String,
        uploadId: String,
        bytes: ByteArray,
        mediaType: String,
    ): ScreenUploadReceipt {
        requireIdentifier(nodeId)
        requireIdentifier(uploadId)
        val path = listOf("v1", "nodes", nodeId, "screen", "uploads", uploadId, "content")
        return decode(perform(binaryPut(path, bytes, mediaType)), serializer())
    }

    suspend fun submitScreenFrame(nodeId: String, body: ScreenFrameSubmit): ScreenFrameReceipt =
        post(listOf("v1", "nodes", nodeId, "screen", "frames"), body)

    suspend fun getScreenFrame(nodeId: String, frameId: String): ScreenFrameSnapshot =
        get(listOf("v1", "nodes", nodeId, "screen", "frames", frameId))

    suspend fun getScreenAnalysis(nodeId: String, analysisId: String): ScreenAnalysis =
        get(listOf("v1", "nodes", nodeId, "screen", "analyses", analysisId))

    suspend fun getScreenState(nodeId: String, companionId: String, displayId: String = "default"): ScreenState {
        requireIdentifier(nodeId)
        requireIdentifier(companionId)
        requireIdentifier(displayId)
        return get(listOf("v1", "nodes", nodeId, "screen", "state"),
            mapOf("companion_id" to companionId, "display_id" to displayId))
    }

    suspend fun getProactivityPolicy(companionId: String): ProactivityPolicySnapshot =
        get(listOf("v1", "companions", companionId, "proactivity"))

    suspend fun updateProactivityPolicy(companionId: String, body: ProactivityPolicyUpdate): ProactivityPolicySnapshot =
        post(listOf("v1", "companions", companionId, "proactivity"), body)

    /** Refresh mutable decisions with latest=true; returned pages remain ascending. */
    suspend fun getProactivityDecisions(companionId: String, afterSequence: Long = 0, limit: Int = 50, latest: Boolean = false): ProactivityDecisionPage {
        requireIdentifier(companionId)
        require(afterSequence >= 0 && limit in 1..200)
        val query = mutableMapOf("after_sequence" to afterSequence.toString(), "limit" to limit.toString())
        if (latest) query["latest"] = "true"
        return get(listOf("v1", "companions", companionId, "proactivity", "decisions"), query)
    }

    /** Null cursor establishes a baseline without replay. Recheck access/lease/focus before display. */
    suspend fun getProactivityOutput(companionId: String, nodeId: String, accessId: String, epoch: Long, afterSequence: Long? = null): ProactivityOutputPage {
        requireIdentifier(companionId)
        requireIdentifier(nodeId)
        requireIdentifier(accessId)
        require(epoch >= 0 && (afterSequence == null || afterSequence >= 0))
        val query = mutableMapOf("node_id" to nodeId, "access_id" to accessId, "epoch" to epoch.toString())
        afterSequence?.let { query["after_sequence"] = it.toString() }
        return get(listOf("v1", "companions", companionId, "proactivity", "output"), query)
    }

    private fun requireIdentifier(value: String) {
        require(value.length in 1..128 && value.isNotBlank() && value == value.trim()) {
            "Identifiers must contain 1..128 nonblank characters without surrounding whitespace"
        }
    }

    suspend fun events(afterSequence: Long = 0, nodeId: String? = null, limit: Int = 100): EventPage {
        require(afterSequence >= 0 && limit in 1..500)
        val query = mutableMapOf("after_sequence" to afterSequence.toString(), "limit" to limit.toString())
        nodeId?.let { query["node_id"] = it }
        return get(listOf("v1", "events"), query)
    }

    suspend fun writeMemory(companionId: String, body: MemoryWrite): MemoryRecord =
        post(listOf("v1", "companions", companionId, "memories"), body)

    suspend fun correctMemory(companionId: String, memoryId: String, body: MemoryCorrection): MemoryRecord =
        post(listOf("v1", "companions", companionId, "memories", memoryId, "corrections"), body)

    suspend fun updateMemoryMetadata(companionId: String, memoryId: String, body: MemoryMetadataUpdate): MemoryRecord =
        decode(perform(request("PATCH", listOf("v1", "companions", companionId, "memories", memoryId), NodeJson.encodeToString(body))), serializer())

    suspend fun listMemories(companionId: String, limit: Int = 50, offset: Int = 0): JsonElement {
        require(limit in 1..200 && offset >= 0)
        return get(listOf("v1", "companions", companionId, "memories"), mapOf("limit" to "$limit", "offset" to "$offset"))
    }

    suspend fun recallMemory(companionId: String, body: MemoryRecall): RecallResult =
        post(listOf("v1", "companions", companionId, "memories", "recall"), body)

    /** May call the embedding model once when the query is not cached. */
    suspend fun semanticRecallMemory(companionId: String, body: MemoryRecall): RecallResult =
        post(listOf("v1", "companions", companionId, "memories", "semantic-recall"), body)

    suspend fun memoryEmbeddings(companionId: String): JsonObject =
        get(listOf("v1", "companions", companionId, "memories", "embeddings"))

    suspend fun memoryEmbeddingJobs(companionId: String, limit: Int = 50, offset: Int = 0): JsonObject {
        require(limit in 1..200 && offset >= 0)
        return get(listOf("v1", "companions", companionId, "memories", "embeddings", "jobs"), mapOf("limit" to "$limit", "offset" to "$offset"))
    }

    suspend fun runMemoryEmbeddings(companionId: String, body: MemoryEmbeddingRun = MemoryEmbeddingRun()): JsonObject =
        post(listOf("v1", "companions", companionId, "memories", "embeddings", "run"), body)

    suspend fun memoryStats(companionId: String): JsonObject = get(listOf("v1", "companions", companionId, "memories", "stats"))

    suspend fun memoryLogs(
        companionId: String,
        afterSequence: Long = 0,
        limit: Int = 100,
        operation: String? = null,
        status: String? = null,
        commandId: String? = null,
    ): JsonObject {
        require(afterSequence >= 0 && limit in 1..500)
        val query = mutableMapOf("after_sequence" to afterSequence.toString(), "limit" to limit.toString())
        operation?.let { query["operation"] = it }
        status?.let { query["status"] = it }
        commandId?.let { query["command_id"] = it }
        return get(listOf("v1", "companions", companionId, "memories", "logs"), query)
    }

    suspend fun consolidationPlan(companionId: String): JsonObject =
        get(listOf("v1", "companions", companionId, "memories", "consolidation-plan"))

    suspend fun memoryPolicy(): JsonObject = get(listOf("v1", "memory", "policy"))

    suspend fun memoryAutomation(companionId: String): JsonObject =
        get(listOf("v1", "companions", companionId, "memories", "automation"))

    suspend fun memoryAutomationJobs(companionId: String, limit: Int = 50, offset: Int = 0): JsonObject {
        require(limit in 1..200 && offset >= 0)
        return get(listOf("v1", "companions", companionId, "memories", "automation", "jobs"), mapOf("limit" to "$limit", "offset" to "$offset"))
    }

    /** Processes one eligible batch; it does not redispatch jobs already marked running. */
    suspend fun runMemoryAutomation(companionId: String, body: MemoryAutomationRun = MemoryAutomationRun()): JsonObject =
        post(listOf("v1", "companions", companionId, "memories", "automation", "run"), body)

    suspend fun memorySummaries(companionId: String, limit: Int = 50, offset: Int = 0): JsonObject {
        require(limit in 1..200 && offset >= 0)
        return get(listOf("v1", "companions", companionId, "memories", "summaries"), mapOf("limit" to "$limit", "offset" to "$offset"))
    }

    suspend fun knowledgeGraph(companionId: String, query: String = "", limit: Int = 50, entityId: String? = null, hops: Int = 1): JsonObject {
        require(limit in 1..200 && hops in 1..3)
        val parameters = mutableMapOf("query" to query, "limit" to "$limit", "hops" to "$hops")
        entityId?.let { require(it.isNotBlank()); parameters["entity_id"] = it }
        return get(listOf("v1", "companions", companionId, "memories", "knowledge-graph"), parameters)
    }

    suspend fun updateGraphEntityAliases(companionId: String, entityId: String, body: GraphAliasUpdate): GraphEntityAliases =
        decode(perform(request("PUT", listOf("v1", "companions", companionId, "memories", "knowledge-graph", "entities", entityId, "aliases"), NodeJson.encodeToString(body))), serializer())

    suspend fun deleteMemory(companionId: String, memoryId: String): JsonObject =
        decode(perform(request("DELETE", listOf("v1", "companions", companionId, "memories", memoryId))), serializer())

    internal fun url(path: List<String>, query: Map<String, String> = emptyMap()): HttpUrl = base.newBuilder().apply {
        path.forEach { segment -> require(segment.isNotEmpty()); addPathSegment(segment) }
        query.forEach { (name, value) -> addQueryParameter(name, value) }
    }.build()

    private fun request(method: String, path: List<String>, body: String? = null, query: Map<String, String> = emptyMap()): Request =
        Request.Builder()
            .url(url(path, query))
            .header("Authorization", "Bearer $token")
            .header("Accept", "application/json")
            .method(method, body?.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .build()

    /** Raw-byte PUT used by binary uploads; credentials stay in the Authorization header. */
    private fun binaryPut(path: List<String>, bytes: ByteArray, mediaType: String): Request =
        Request.Builder()
            .url(url(path))
            .header("Authorization", "Bearer $token")
            .header("Accept", "application/json")
            .put(bytes.toRequestBody(mediaType.toMediaType()))
            .build()

    private suspend inline fun <reified R> get(path: List<String>, query: Map<String, String> = emptyMap()): R =
        decode(perform(request("GET", path, query = query)), serializer())

    private suspend inline fun <reified R, reified B> post(path: List<String>, body: B): R =
        decode(perform(request("POST", path, NodeJson.encodeToString(body))), serializer())

    private fun <R> decode(data: HttpData, serializer: KSerializer<R>): R = NodeJson.decodeFromString(serializer, data.body)

    private data class HttpData(val body: String, val startedNanos: Long, val receivedNanos: Long)

    private suspend fun perform(request: Request): HttpData = suspendCancellableCoroutine { continuation ->
        val started = clock.nowNanos()
        val call = httpClient.newCall(request)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    val data = response.use {
                        val body = it.body?.string() ?: throw IOException("Runtime returned an empty response")
                        if (!it.isSuccessful) {
                            val error = runCatching { NodeJson.parseToJsonElement(body).jsonObject["error"]?.jsonObject }.getOrNull()
                            val code = error?.get("code")?.jsonPrimitive?.content ?: "http_error"
                            val message = error?.get("message")?.jsonPrimitive?.content ?: "Runtime rejected the request"
                            throw ApiException(it.code, code, message)
                        }
                        HttpData(body, started, clock.nowNanos())
                    }
                    if (continuation.isActive) continuation.resume(data)
                } catch (error: Exception) {
                    if (continuation.isActive) continuation.resumeWithException(error)
                }
            }
        })
    }
}
