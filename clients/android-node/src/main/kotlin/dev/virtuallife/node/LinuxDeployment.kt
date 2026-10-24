package dev.virtuallife.node

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class LinuxHostKeyRequest(val host: String, val port: Int = 22)

@Serializable
data class LinuxHostKey(
    val host: String,
    val port: Int,
    @SerialName("key_type") val keyType: String,
    val fingerprint: String,
)

/** SSH secrets are explicitly supplied, in-memory only. Never persist or log serialized requests. */
@Serializable
class LinuxInspectRequest(
    val host: String,
    val username: String,
    @SerialName("host_key_fingerprint") val hostKeyFingerprint: String,
    val port: Int = 22,
    val python: String = "python3",
    val password: String? = null,
    @SerialName("private_key") val privateKey: String? = null,
    val passphrase: String? = null,
) {
    override fun toString(): String = "LinuxInspectRequest([redacted])"
}

@Serializable
data class LinuxInspection(
    val ready: Boolean,
    @SerialName("python_version") val pythonVersion: String? = null,
    val systemd: Boolean,
    val privileged: Boolean,
    @SerialName("existing_node_id") val existingNodeId: String? = null,
    @SerialName("existing_server_url") val existingServerUrl: String? = null,
    @SerialName("reason_codes") val reasonCodes: List<String> = emptyList(),
)

/** Use one commandId for ambiguous retries. SSH secrets must not be stored or logged. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
class LinuxDeploymentRequest(
    @SerialName("command_id") val commandId: String,
    val host: String,
    val username: String,
    @SerialName("host_key_fingerprint") val hostKeyFingerprint: String,
    @SerialName("server_url") val serverUrl: String,
    val label: String,
    val port: Int = 22,
    val python: String = "python3",
    @SerialName("node_id") val nodeId: String? = null,
    val password: String? = null,
    @SerialName("private_key") val privateKey: String? = null,
    val passphrase: String? = null,
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("attention_enabled") val attentionEnabled: Boolean = false,
) {
    override fun toString(): String = "LinuxDeploymentRequest([redacted])"
}

@Serializable
enum class LinuxDeploymentStatus {
    @SerialName("queued") QUEUED,
    @SerialName("running") RUNNING,
    @SerialName("completed") COMPLETED,
    @SerialName("failed") FAILED,
    @SerialName("interrupted") INTERRUPTED,
}

@Serializable
enum class LinuxDeploymentStage {
    @SerialName("queued") QUEUED,
    @SerialName("connecting") CONNECTING,
    @SerialName("checking") CHECKING,
    @SerialName("uploading") UPLOADING,
    @SerialName("installing") INSTALLING,
    @SerialName("verifying") VERIFYING,
    @SerialName("completed") COMPLETED,
    @SerialName("failed") FAILED,
}

@Serializable
data class LinuxDeploymentError(val code: String, val message: String)

@Serializable
data class LinuxDeploymentJob(
    @SerialName("command_id") val commandId: String,
    val status: LinuxDeploymentStatus,
    val stage: LinuxDeploymentStage,
    val host: String,
    val port: Int,
    val username: String,
    @SerialName("node_id") val nodeId: String? = null,
    val version: String,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
    @SerialName("package_sha256") val packageSha256: String,
    @SerialName("credential_revoked") val credentialRevoked: Boolean,
    val error: LinuxDeploymentError? = null,
)
