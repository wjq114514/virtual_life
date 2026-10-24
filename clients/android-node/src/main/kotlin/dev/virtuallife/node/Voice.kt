package dev.virtuallife.node

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonClassDiscriminator
import kotlinx.serialization.json.JsonObject

@Serializable
enum class VoiceMode {
    @SerialName("continuous") CONTINUOUS,
    @SerialName("push_to_talk") PUSH_TO_TALK,
    @SerialName("text") TEXT,
}

@Serializable
data class VoiceAsrConfig(
    val engine: String,
    @SerialName("model_id") val modelId: String,
    val language: String = "zh",
)

@Serializable
data class VoiceOutputConfig(
    @SerialName("audio_enabled") val audioEnabled: Boolean = true,
    @SerialName("voice_id") val voiceId: String = "default",
)

@Serializable
data class VoiceSessionCreate(
    @SerialName("command_id") val commandId: String,
    @SerialName("companion_id") val companionId: String,
    val epoch: Long,
    @SerialName("expected_revision") val expectedRevision: Long,
    @SerialName("presentation_id") val presentationId: String,
    @SerialName("user_initiated") val userInitiated: Boolean = true,
    val mode: VoiceMode = VoiceMode.CONTINUOUS,
    val asr: VoiceAsrConfig? = null,
    val output: VoiceOutputConfig = VoiceOutputConfig(),
    @SerialName("replace_session_id") val replaceSessionId: String? = null,
) {
    init {
        require(userInitiated) { "Voice sessions must be explicitly user initiated" }
        require(epoch >= 0 && expectedRevision >= 0)
        require((mode == VoiceMode.TEXT) == (asr == null)) {
            "Text sessions omit ASR; microphone sessions require ASR configuration"
        }
    }
}

@Serializable
enum class VoiceSessionStatus {
    @SerialName("active") ACTIVE,
    @SerialName("stopped") STOPPED,
    @SerialName("expired") EXPIRED,
    @SerialName("superseded") SUPERSEDED,
}

@Serializable
data class VoiceSessionSnapshot(
    @SerialName("session_id") val sessionId: String,
    @SerialName("command_id") val commandId: String,
    @SerialName("companion_id") val companionId: String,
    @SerialName("node_id") val nodeId: String,
    @SerialName("presentation_id") val presentationId: String,
    val mode: VoiceMode,
    val epoch: Long,
    @SerialName("session_generation") val sessionGeneration: Long,
    @SerialName("output_generation") val outputGeneration: Long,
    val status: VoiceSessionStatus,
    @SerialName("audio_enabled") val audioEnabled: Boolean,
    @SerialName("effective_voice") val effectiveVoice: String,
    val asr: VoiceAsrConfig? = null,
    @SerialName("created_at") val createdAt: String,
    @SerialName("last_heartbeat_at") val lastHeartbeatAt: String,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("idle_deadline_at") val idleDeadlineAt: String,
    @SerialName("absolute_expires_at") val absoluteExpiresAt: String,
    @SerialName("idle_timeout_seconds") val idleTimeoutSeconds: Double,
    @SerialName("heartbeat_interval_seconds") val heartbeatIntervalSeconds: Double,
    val limits: VoiceSessionLimits,
)

@Serializable
data class VoiceSessionLimits(
    @SerialName("max_input_chars") val maxInputChars: Int,
    @SerialName("max_input_age_seconds") val maxInputAgeSeconds: Double,
    @SerialName("max_turns_per_session") val maxTurnsPerSession: Int,
    @SerialName("max_segment_chars") val maxSegmentChars: Int,
    @SerialName("max_reply_chars") val maxReplyChars: Int,
)

@Serializable
data class VoiceHeartbeat(@SerialName("command_id") val commandId: String)

@Serializable
data class VoiceStopRequest(@SerialName("command_id") val commandId: String)

@OptIn(ExperimentalSerializationApi::class)
@JsonClassDiscriminator("kind")
@Serializable
sealed interface VoiceTurnInput {
    @Serializable
    @SerialName("asr")
    data class Asr(
        @SerialName("started_at") val startedAt: String,
        @SerialName("ended_at") val endedAt: String,
        @SerialName("audio_duration_ms") val audioDurationMs: Int,
        @SerialName("asr_model_id") val asrModelId: String,
        val language: String = "zh",
        val discontinuity: Boolean = false,
    ) : VoiceTurnInput {
        init { require(audioDurationMs in 0..45_000) }
    }

    @Serializable
    @SerialName("text")
    data class Text(val mode: TextInputMode = TextInputMode.MANUAL) : VoiceTurnInput
}

@Serializable
enum class TextInputMode {
    @SerialName("manual") MANUAL,
}

@Serializable
enum class VoiceEndReason {
    @SerialName("adaptive_endpoint") ADAPTIVE_ENDPOINT,
    @SerialName("manual") MANUAL,
    @SerialName("push_to_talk") PUSH_TO_TALK,
}

@Serializable
enum class ScreenReferencePolicy {
    @SerialName("latest_if_fresh") LATEST_IF_FRESH,
    @SerialName("request_fresh") REQUEST_FRESH,
}

@Serializable
data class VoiceScreenReference(
    @SerialName("node_id") val nodeId: String,
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    @SerialName("display_id") val displayId: String = "default",
    @SerialName("frame_id") val frameId: String? = null,
    @SerialName("analysis_id") val analysisId: String? = null,
    val policy: ScreenReferencePolicy = ScreenReferencePolicy.LATEST_IF_FRESH,
) {
    init { require(generation >= 0) }
}

@Serializable
data class VoiceTurnSubmit(
    @SerialName("command_id") val commandId: String,
    @SerialName("turn_id") val turnId: String,
    @SerialName("session_generation") val sessionGeneration: Long,
    @SerialName("expected_output_generation") val expectedOutputGeneration: Long,
    val epoch: Long,
    @SerialName("expected_revision") val expectedRevision: Long,
    val text: String,
    @SerialName("user_initiated") val userInitiated: Boolean = true,
    @SerialName("end_reason") val endReason: VoiceEndReason,
    val input: VoiceTurnInput,
    @SerialName("continuation_of_turn_id") val continuationOfTurnId: String? = null,
    @SerialName("screen_reference") val screenReference: VoiceScreenReference? = null,
) {
    init {
        require(commandId.isNotBlank() && turnId.isNotBlank())
        require(sessionGeneration >= 0 && expectedOutputGeneration >= 0)
        require(epoch >= 0 && expectedRevision >= 0)
        require(userInitiated)
        require(text.trim().isNotEmpty() && text.length <= 4096)
        require(input !is VoiceTurnInput.Text || endReason == VoiceEndReason.MANUAL) {
            "Text input requires end_reason=manual"
        }
    }
}

@Serializable
enum class VoiceTurnStatus {
    @SerialName("accepted") ACCEPTED,
    @SerialName("in_progress") IN_PROGRESS,
    @SerialName("completed") COMPLETED,
    @SerialName("discarded") DISCARDED,
    @SerialName("interrupted") INTERRUPTED,
    @SerialName("failed") FAILED,
}

@Serializable
data class VoiceTurnReceipt(
    @SerialName("turn_id") val turnId: String,
    @SerialName("command_id") val commandId: String,
    @SerialName("session_id") val sessionId: String,
    @SerialName("companion_id") val companionId: String,
    @SerialName("session_generation") val sessionGeneration: Long,
    @SerialName("output_generation") val outputGeneration: Long,
    val status: VoiceTurnStatus,
    val sequence: Long,
    @SerialName("audio_enabled") val audioEnabled: Boolean,
    @SerialName("speech_available") val speechAvailable: Boolean,
    @SerialName("assistant_message") val assistantMessage: String? = null,
    @SerialName("discard_reason") val discardReason: String? = null,
    @SerialName("created_at") val createdAt: String,
)

@Serializable
data class VoiceTurnSnapshot(
    @SerialName("turn_id") val turnId: String,
    @SerialName("command_id") val commandId: String,
    @SerialName("session_id") val sessionId: String,
    @SerialName("companion_id") val companionId: String,
    val status: VoiceTurnStatus,
    @SerialName("discard_reason") val discardReason: String? = null,
    @SerialName("output_generation") val outputGeneration: Long,
    @SerialName("assistant_message") val assistantMessage: String? = null,
    val model: String? = null,
    val usage: VoiceUsage? = null,
    @SerialName("speech_status") val speechStatus: String,
    @SerialName("segment_count") val segmentCount: Long,
    val sequence: Long,
    @SerialName("created_at") val createdAt: String,
)

@Serializable
data class VoiceUsage(
    @SerialName("input_tokens") val inputTokens: Long,
    @SerialName("output_tokens") val outputTokens: Long,
)

@Serializable
enum class VoiceInterruptReason {
    @SerialName("user_speech") USER_SPEECH,
    @SerialName("stop_button") STOP_BUTTON,
    @SerialName("text_input") TEXT_INPUT,
    @SerialName("session_stop") SESSION_STOP,
}

@Serializable
data class VoiceInterruptRequest(
    @SerialName("command_id") val commandId: String,
    @SerialName("session_generation") val sessionGeneration: Long,
    @SerialName("target_turn_id") val targetTurnId: String,
    @SerialName("expected_output_generation") val expectedOutputGeneration: Long,
    val reason: VoiceInterruptReason,
) {
    init { require(sessionGeneration >= 0 && expectedOutputGeneration >= 0) }
}

@Serializable
enum class VoiceInterruptStatus {
    @SerialName("interrupted") INTERRUPTED,
    @SerialName("already_terminal") ALREADY_TERMINAL,
    @SerialName("stale") STALE,
}

@Serializable
enum class VoiceUpstreamState {
    @SerialName("pending") PENDING,
    @SerialName("draining") DRAINING,
    @SerialName("closed") CLOSED,
    @SerialName("completed") COMPLETED,
    @SerialName("unknown") UNKNOWN,
}

@Serializable
data class VoiceInterruptReceipt(
    @SerialName("session_id") val sessionId: String,
    @SerialName("target_turn_id") val targetTurnId: String,
    val status: VoiceInterruptStatus,
    @SerialName("output_generation") val outputGeneration: Long,
    @SerialName("upstream_state") val upstreamState: VoiceUpstreamState,
    val replayed: Boolean = false,
    @SerialName("created_at") val createdAt: String,
)

@Serializable
enum class VoicePlaybackStatus {
    @SerialName("started") STARTED,
    @SerialName("progress") PROGRESS,
    @SerialName("completed") COMPLETED,
    @SerialName("interrupted") INTERRUPTED,
    @SerialName("failed") FAILED,
}

@Serializable
data class VoicePlaybackReport(
    @SerialName("command_id") val commandId: String,
    @SerialName("turn_id") val turnId: String,
    @SerialName("output_generation") val outputGeneration: Long,
    @SerialName("segment_sequence") val segmentSequence: Long,
    @SerialName("played_samples") val playedSamples: Long,
    val status: VoicePlaybackStatus,
) {
    init {
        require(outputGeneration >= 0 && segmentSequence >= 0)
        require(playedSamples in 0..3_600_000_000L)
    }
}

@Serializable
data class VoicePlaybackReceipt(
    val recorded: Boolean,
    val valid: Boolean,
    @SerialName("output_generation") val outputGeneration: Long,
)

@Serializable
enum class VoiceSegmentStatus {
    @SerialName("ready") READY,
    @SerialName("cancelled") CANCELLED,
    @SerialName("expired") EXPIRED,
}

@Serializable
data class VoiceSegmentInfo(
    @SerialName("segment_sequence") val segmentSequence: Long,
    val text: String,
    @SerialName("spoken_text") val spokenText: String,
    @SerialName("char_start") val charStart: Long,
    @SerialName("char_end") val charEnd: Long,
    @SerialName("audio_path") val audioPath: String,
    @SerialName("media_type") val mediaType: String,
    @SerialName("sample_rate") val sampleRate: Int,
    val channels: Int,
    @SerialName("sample_count") val sampleCount: Long,
    val sha256: String,
    val status: VoiceSegmentStatus,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("output_generation") val outputGeneration: Long? = null,
) {
    init {
        require(segmentSequence >= 0 && charStart >= 0 && charEnd >= charStart)
        require(sampleRate >= 0 && channels >= 1 && sampleCount >= 0)
    }
}

@Serializable
data class VoiceStreamSnapshot(
    @SerialName("session_id") val sessionId: String,
    @SerialName("session_generation") val sessionGeneration: Long,
    @SerialName("turn_id") val turnId: String,
    @SerialName("output_generation") val outputGeneration: Long,
    val sequence: Long,
    val cursor: Long,
    val turn: VoiceTurnSnapshot,
    @SerialName("replay_gap") val replayGap: Boolean = false,
)

@Serializable
data class VoicePreviewSnapshot(
    @SerialName("session_id") val sessionId: String,
    @SerialName("session_generation") val sessionGeneration: Long,
    @SerialName("turn_id") val turnId: String,
    @SerialName("output_generation") val outputGeneration: Long,
    val sequence: Long,
    val preview: PersonalityPreviewResult,
    val emotion: EmotionSnapshot? = null,
)

@Serializable
data class VoiceEventStatus(
    val stage: String,
    val reason: String? = null,
    @SerialName("discard_reason") val discardReason: String? = null,
)

@Serializable
data class VoiceTextDelta(
    val text: String,
    val sequence: Long? = null,
    @SerialName("output_generation") val outputGeneration: Long? = null,
)

@Serializable
data class VoiceSpeechEnd(
    val status: String,
    @SerialName("segment_count") val segmentCount: Long = 0,
)

@Serializable
data class VoiceInterruptedEvent(val reason: String? = null)

@Serializable
data class VoiceEndEvent(val status: String)

@Serializable
data class VoiceStreamError(val code: String, val message: String)

sealed interface VoiceStreamEvent {
    data class Snapshot(val value: VoiceStreamSnapshot) : VoiceStreamEvent
    data class Preview(val value: VoicePreviewSnapshot) : VoiceStreamEvent
    data class Status(val value: VoiceEventStatus) : VoiceStreamEvent
    data class TextDelta(val value: VoiceTextDelta) : VoiceStreamEvent
    data class SpeechSegment(val value: VoiceSegmentInfo) : VoiceStreamEvent
    data class DialogueResult(val value: VoiceTurnReceipt) : VoiceStreamEvent
    data class SpeechEnd(val value: VoiceSpeechEnd) : VoiceStreamEvent
    data class Interrupted(val value: VoiceInterruptedEvent) : VoiceStreamEvent
    data class VoiceEnd(val value: VoiceEndEvent) : VoiceStreamEvent
    data class Error(val value: VoiceStreamError) : VoiceStreamEvent
}

data class VoicePcmSegment(
    val bytes: ByteArray,
    val mediaType: String,
    val sampleRate: Int,
    val channels: Int,
    val sampleCount: Long,
    val sha256: String,
    val outputGeneration: Long?,
) {
    init {
        require(mediaType.substringBefore(';').trim() == "audio/pcm")
        require(sampleRate > 0 && channels in 1..2 && sampleCount >= 0)
        require(sampleCount <= Long.MAX_VALUE / (channels * 2L))
        require(bytes.size.toLong() == sampleCount * channels * 2L) {
            "PCM byte count does not match its sample metadata"
        }
    }
}

const val MAX_VOICE_PCM_BYTES: Int = 16 * 1024 * 1024
