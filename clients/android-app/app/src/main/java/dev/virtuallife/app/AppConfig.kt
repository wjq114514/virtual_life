package dev.virtuallife.app

import android.content.Context

data class AppConfig(
    val baseUrl: String,
    val token: String,
    val adminToken: String,
    val nodeId: String,
    val companionId: String,
    val displayId: String = "default",
)

/** Small local store; the bearer token never leaves the app-private preferences. */
class AppConfigStore(context: Context) {
    private val prefs = context.getSharedPreferences("virtual_life", Context.MODE_PRIVATE)

    fun load(defaultNodeId: String, defaultCompanionId: String): AppConfig = AppConfig(
        baseUrl = prefs.getString(KEY_BASE_URL, null) ?: DEFAULT_BASE_URL,
        token = prefs.getString(KEY_TOKEN, null) ?: DEFAULT_TOKEN,
        adminToken = prefs.getString(KEY_ADMIN_TOKEN, null) ?: DEFAULT_TOKEN,
        nodeId = prefs.getString(KEY_NODE_ID, null) ?: defaultNodeId,
        companionId = prefs.getString(KEY_COMPANION_ID, null) ?: defaultCompanionId,
        displayId = "default",
    )

    fun save(config: AppConfig) {
        prefs.edit()
            .putString(KEY_BASE_URL, config.baseUrl)
            .putString(KEY_TOKEN, config.token)
            .putString(KEY_ADMIN_TOKEN, config.adminToken)
            .putString(KEY_NODE_ID, config.nodeId)
            .putString(KEY_COMPANION_ID, config.companionId)
            .apply()
    }

    companion object {
        // 10.0.2.2 reaches the developer machine from the emulator.
        const val DEFAULT_BASE_URL = "http://10.0.2.2:8000/"
        const val DEFAULT_TOKEN = "local-dev-token"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_TOKEN = "token"
        private const val KEY_ADMIN_TOKEN = "admin_token"
        private const val KEY_NODE_ID = "node_id"
        private const val KEY_COMPANION_ID = "companion_id"
    }
}
