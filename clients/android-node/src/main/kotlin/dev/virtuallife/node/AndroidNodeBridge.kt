package dev.virtuallife.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.put

@Serializable
data class HostPermissions(
    @SerialName("overlay_granted") val overlayGranted: Boolean = false,
    @SerialName("screen_capture_granted") val screenCaptureGranted: Boolean = false,
    @SerialName("microphone_granted") val microphoneGranted: Boolean = false,
    @SerialName("notifications_granted") val notificationsGranted: Boolean = false,
    @SerialName("foreground_service_running") val foregroundServiceRunning: Boolean = false,
)

/** Signals supplied explicitly by the host; the SDK does not discover other apps. */
@Serializable
data class ScreenSignals(
    @SerialName("screen_on") val screenOn: Boolean? = null,
    @SerialName("host_visible") val hostVisible: Boolean? = null,
    @SerialName("user_interacting") val userInteracting: Boolean? = null,
    @SerialName("reading_context") val readingContext: JsonObject? = null,
    val extra: JsonObject = emptyObject(),
)

enum class StopReason {
    TRANSFER_RELEASE, OWNERSHIP_CHANGED, LEASE_EXPIRED, LEASE_RENEWAL_FAILED,
    CONNECTION_LOST, HOST_CAPABILITY_LOST, RENDER_FAILED, SHUTDOWN,
}

/**
 * All callbacks are suspending. The host dispatches Android UI operations to its main
 * thread and waits for completion before returning. Preparation must not emit output.
 * stopOutput must stop existing audio/speech as well as visible rendering.
 */
interface AndroidNodeBridge {
    suspend fun capabilities(): Set<String>
    suspend fun permissions(): HostPermissions
    suspend fun screenSignals(): ScreenSignals
    suspend fun prepareRender(snapshot: CompanionSnapshot)
    suspend fun stopOutput(companionId: String, reason: StopReason)
    suspend fun renderState(snapshot: CompanionSnapshot, output: OutputHandle)
    suspend fun onEvent(event: ProtocolEvent) {}
    suspend fun onFailure(error: Throwable) {}
}

internal suspend fun AndroidNodeBridge.heartbeatContext(): JsonObject = buildJsonObject {
    put("permissions", NodeJson.encodeToJsonElement(permissions()))
    put("screen", NodeJson.encodeToJsonElement(screenSignals()))
}
