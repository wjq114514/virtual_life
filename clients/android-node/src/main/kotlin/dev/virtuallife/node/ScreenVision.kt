package dev.virtuallife.node

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonClassDiscriminator
import kotlinx.serialization.json.JsonObject

/** Wire contracts for admin-token, server-side screen vision. All fields are snake_case. */

@Serializable
data class ScreenSessionCreate(
    @SerialName("command_id") val commandId: String,
    @SerialName("schema_version") val schemaVersion: String = "1.0",
    @SerialName("companion_id") val companionId: String,
    @SerialName("subject_node_id") val subjectNodeId: String,
    @SerialName("display_id") val displayId: String = "default",
    val mode: String,
    @SerialName("user_initiated") val userInitiated: Boolean,
    @SerialName("capture_source") val captureSource: String = "other",
    @SerialName("requested_profile") val requestedProfile: String = "balanced",
) {
    init {
        require(userInitiated) { "Screen sessions require user_initiated=true" }
    }
}

@Serializable
data class ScreenCommand(@SerialName("command_id") val commandId: String)

@Serializable
data class ScreenUploadRegister(
    @SerialName("command_id") val commandId: String,
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    @SerialName("media_type") val mediaType: String,
    @SerialName("byte_length") val byteLength: Long,
    val sha256: String,
    val width: Int,
    val height: Int,
)

@Serializable
data class ClientHash(
    val algorithm: String,
    val value: String,
)

@Serializable
data class OcrBlock(
    val text: String,
    val bbox: List<Double>,
    val confidence: Double? = null,
    val language: String? = null,
)

@Serializable
data class UiElement(
    @SerialName("element_id") val elementId: String,
    @SerialName("parent_id") val parentId: String? = null,
    val role: String,
    val text: String = "",
    val bbox: List<Double>,
    val confidence: Double? = null,
)

/** Host-computed hints; optional except [extractor]. */
@Serializable
data class ClientFeatures(
    @SerialName("schema_version") val schemaVersion: String = "1.0",
    val extractor: String,
    @SerialName("extractor_version") val extractorVersion: String? = null,
    @SerialName("baseline_frame_id") val baselineFrameId: String? = null,
    val hashes: List<ClientHash> = emptyList(),
    @SerialName("change_score") val changeScore: Double? = null,
    @SerialName("filtered_count_since_previous") val filteredCountSincePrevious: Long? = null,
    @SerialName("ocr_blocks") val ocrBlocks: List<OcrBlock> = emptyList(),
    @SerialName("ui_elements") val uiElements: List<UiElement> = emptyList(),
)

@Serializable
data class HostMetadata(
    @SerialName("app_id") val appId: String? = null,
    @SerialName("window_id") val windowId: String? = null,
    @SerialName("app_class") val appClass: String? = null,
    val locked: Boolean? = null,
    @SerialName("user_present") val userPresent: Boolean? = null,
    val meeting: Boolean? = null,
    @SerialName("screen_sharing") val screenSharing: Boolean? = null,
)

@Serializable
data class RedactedRegion(
    val bbox: List<Double>,
    val reason: String,
)

/** Discriminated on `kind`; the discriminator value is the subclass [SerialName]. */
@OptIn(ExperimentalSerializationApi::class)
@JsonClassDiscriminator("kind")
@Serializable
sealed interface ScreenFrameInput {
    @Serializable
    @SerialName("image")
    data class Image(
        @SerialName("upload_id") val uploadId: String,
        @SerialName("capture_width") val captureWidth: Int,
        @SerialName("capture_height") val captureHeight: Int,
        @SerialName("rotation_degrees") val rotationDegrees: Int = 0,
    ) : ScreenFrameInput

    @Serializable
    @SerialName("reference")
    data class Reference(
        @SerialName("reference_frame_id") val referenceFrameId: String,
        val reason: String = "client_filtered",
    ) : ScreenFrameInput

    @Serializable
    @SerialName("unavailable")
    data class Unavailable(val reason: String) : ScreenFrameInput
}

@Serializable
data class ScreenFrameSubmit(
    @SerialName("command_id") val commandId: String,
    @SerialName("schema_version") val schemaVersion: String = "1.0",
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    val sequence: Long,
    @SerialName("sampled_at") val sampledAt: String,
    @SerialName("ttl_seconds") val ttlSeconds: Int = 90,
    val trigger: String = "changed",
    val input: ScreenFrameInput,
    @SerialName("client_features") val clientFeatures: ClientFeatures? = null,
    @SerialName("host_metadata") val hostMetadata: HostMetadata? = null,
    @SerialName("redacted_regions") val redactedRegions: List<RedactedRegion> = emptyList(),
)

@Serializable
data class ScreenLimits(
    @SerialName("max_image_bytes") val maxImageBytes: Int,
    @SerialName("max_image_pixels") val maxImagePixels: Int,
    @SerialName("max_image_side") val maxImageSide: Int,
    @SerialName("max_pending_per_stream") val maxPendingPerStream: Int,
    @SerialName("max_pending_global") val maxPendingGlobal: Int,
    @SerialName("max_uploads_per_minute_per_node") val maxUploadsPerMinutePerNode: Int,
    @SerialName("max_frames_per_minute_per_node") val maxFramesPerMinutePerNode: Int,
    @SerialName("max_model_images") val maxModelImages: Int,
    @SerialName("output_tokens") val outputTokens: Int,
)

@Serializable
data class ScreenAdvice(
    @SerialName("sample_interval_ms") val sampleIntervalMs: Int,
    @SerialName("min_upload_interval_ms") val minUploadIntervalMs: Int,
    @SerialName("max_keyframe_interval_ms") val maxKeyframeIntervalMs: Int,
    @SerialName("suggested_long_side") val suggestedLongSide: Int,
    @SerialName("model_refresh_seconds") val modelRefreshSeconds: Int,
)

@Serializable
data class ScreenSessionReceipt(
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    @SerialName("companion_id") val companionId: String,
    @SerialName("observer_node_id") val observerNodeId: String,
    @SerialName("subject_node_id") val subjectNodeId: String,
    @SerialName("display_id") val displayId: String,
    val mode: String,
    val status: String,
    @SerialName("effective_profile") val effectiveProfile: String,
    @SerialName("capture_source") val captureSource: String,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("server_time") val serverTime: String,
    @SerialName("user_initiated") val userInitiated: Boolean,
    val limits: ScreenLimits,
    val advice: ScreenAdvice,
)

@Serializable
data class ScreenSessionSnapshot(
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    @SerialName("companion_id") val companionId: String,
    @SerialName("observer_node_id") val observerNodeId: String,
    @SerialName("subject_node_id") val subjectNodeId: String,
    @SerialName("display_id") val displayId: String,
    val mode: String,
    val status: String,
    @SerialName("effective_profile") val effectiveProfile: String,
    @SerialName("capture_source") val captureSource: String,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("server_time") val serverTime: String,
    @SerialName("user_initiated") val userInitiated: Boolean,
    val limits: ScreenLimits,
    val advice: ScreenAdvice,
    @SerialName("purge_status") val purgeStatus: String = "none",
    @SerialName("latest_frame_id") val latestFrameId: String? = null,
    @SerialName("latest_analysis_id") val latestAnalysisId: String? = null,
    @SerialName("last_sequence") val lastSequence: Long? = null,
)

@Serializable
data class ScreenUploadReceipt(
    @SerialName("upload_id") val uploadId: String,
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    val status: String,
    @SerialName("media_type") val mediaType: String,
    @SerialName("byte_length") val byteLength: Long,
    val sha256: String,
    val width: Int,
    val height: Int,
    @SerialName("upload_expires_at") val uploadExpiresAt: String,
    @SerialName("upload_path") val uploadPath: String,
    val limits: ScreenLimits,
)

@Serializable
data class ScreenFrameReceipt(
    @SerialName("frame_id") val frameId: String,
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    val sequence: Long,
    @SerialName("accepted_at") val acceptedAt: String,
    @SerialName("accepted_status") val acceptedStatus: String,
    @SerialName("status_path") val statusPath: String,
)

@Serializable
data class ScreenFrameSnapshot(
    @SerialName("frame_id") val frameId: String,
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    val sequence: Long,
    @SerialName("input_kind") val inputKind: String,
    @SerialName("accepted_at") val acceptedAt: String,
    val status: String,
    @SerialName("analysis_id") val analysisId: String? = null,
    @SerialName("reused_from_frame_id") val reusedFromFrameId: String? = null,
    @SerialName("processing_stage") val processingStage: String? = null,
    @SerialName("error_code") val errorCode: String? = null,
    val retryable: Boolean = false,
    @SerialName("status_path") val statusPath: String,
)

@Serializable
data class ScreenAnalysis(
    @SerialName("analysis_id") val analysisId: String,
    @SerialName("schema_version") val schemaVersion: String,
    @SerialName("frame_id") val frameId: String,
    @SerialName("source_frame_ids") val sourceFrameIds: List<String>,
    @SerialName("companion_id") val companionId: String,
    @SerialName("node_id") val nodeId: String,
    @SerialName("session_id") val sessionId: String,
    val generation: Long,
    val sequence: Long,
    @SerialName("sampled_at") val sampledAt: String,
    @SerialName("completed_at") val completedAt: String,
    @SerialName("expires_at") val expiresAt: String,
    val status: String,
    val quality: String,
    val limitations: List<String> = emptyList(),
    val coverage: JsonObject = emptyObject(),
    @SerialName("scene_summary") val sceneSummary: String = "",
    val activity: String = "unknown",
    @SerialName("activity_score") val activityScore: Double? = null,
    @SerialName("text_blocks") val textBlocks: List<JsonObject> = emptyList(),
    val elements: List<JsonObject> = emptyList(),
    val changes: List<JsonObject> = emptyList(),
    val evidence: List<JsonObject> = emptyList(),
    @SerialName("analysis_path") val analysisPath: String,
    @SerialName("preprocessing_version") val preprocessingVersion: String,
    @SerialName("algorithm_version") val algorithmVersion: String,
    @SerialName("prompt_version") val promptVersion: String,
    @SerialName("model_identity") val modelIdentity: JsonObject = emptyObject(),
    val usage: JsonObject = emptyObject(),
)

@Serializable
data class ScreenState(
    @SerialName("companion_id") val companionId: String,
    @SerialName("observer_node_id") val observerNodeId: String,
    @SerialName("display_id") val displayId: String,
    val observable: Boolean,
    val stale: Boolean,
    val state: String,
    @SerialName("latest_observation_frame_id") val latestObservationFrameId: String? = null,
    @SerialName("latest_analysis_frame_id") val latestAnalysisFrameId: String? = null,
    @SerialName("latest_analysis_id") val latestAnalysisId: String? = null,
    @SerialName("analysis_path") val analysisPath: String? = null,
    @SerialName("pending_newer_frame") val pendingNewerFrame: Boolean = false,
    @SerialName("evidence_sampled_at") val evidenceSampledAt: String? = null,
    @SerialName("evidence_expires_at") val evidenceExpiresAt: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("barrier_sequence") val barrierSequence: Long? = null,
    @SerialName("updated_at") val updatedAt: String,
)

@Serializable
data class ScreenPurgeReceipt(
    @SerialName("session_id") val sessionId: String,
    @SerialName("command_id") val commandId: String,
    val status: String,
    @SerialName("requested_at") val requestedAt: String,
)
