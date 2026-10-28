package dev.virtuallife.chat

import android.content.Context
import android.provider.Settings

data class ChatConfig(
    val baseUrl: String,
    val token: String,
    val nodeId: String,
    val companionId: String,
    val userName: String = "你",
    val tokenBudget: Int = 4096,
    val memoryTokenBudget: Int = 800,
    val maxMemoryItems: Int = 8,
    val maxOutputTokens: Int = 512,
)

/** Small local store; the bearer token never leaves the app-private preferences. */
class ChatConfigStore(context: Context) {
    private val context: Context = context
    private val prefs = context.getSharedPreferences("virtual_life_chat", Context.MODE_PRIVATE)

    fun load(): ChatConfig = ChatConfig(
        baseUrl = prefs.getString(KEY_BASE_URL, null) ?: DEFAULT_BASE_URL,
        token = prefs.getString(KEY_TOKEN, null) ?: DEFAULT_TOKEN,
        nodeId = prefs.getString(KEY_NODE_ID, null) ?: defaultNodeId(context),
        companionId = prefs.getString(KEY_COMPANION_ID, null) ?: DEFAULT_COMPANION_ID,
        userName = prefs.getString(KEY_USER_NAME, null) ?: "你",
        tokenBudget = prefs.getInt(KEY_TOKEN_BUDGET, 4096),
        memoryTokenBudget = prefs.getInt(KEY_MEMORY_TOKEN_BUDGET, 800),
        maxMemoryItems = prefs.getInt(KEY_MAX_MEMORY_ITEMS, 8),
        maxOutputTokens = prefs.getInt(KEY_MAX_OUTPUT_TOKENS, 512),
    )

    fun save(config: ChatConfig) {
        prefs.edit()
            .putString(KEY_BASE_URL, config.baseUrl)
            .putString(KEY_TOKEN, config.token)
            .putString(KEY_NODE_ID, config.nodeId)
            .putString(KEY_COMPANION_ID, config.companionId)
            .putString(KEY_USER_NAME, config.userName)
            .putInt(KEY_TOKEN_BUDGET, config.tokenBudget)
            .putInt(KEY_MEMORY_TOKEN_BUDGET, config.memoryTokenBudget)
            .putInt(KEY_MAX_MEMORY_ITEMS, config.maxMemoryItems)
            .putInt(KEY_MAX_OUTPUT_TOKENS, config.maxOutputTokens)
            .apply()
    }

    companion object {
        // 10.0.2.2 reaches the developer machine from the Android emulator.
        const val DEFAULT_BASE_URL = "http://10.0.2.2:8000/"
        const val DEFAULT_TOKEN = "local-dev-token"
        const val DEFAULT_COMPANION_ID = "android-chat"

        private const val KEY_BASE_URL = "base_url"
        private const val KEY_TOKEN = "token"
        private const val KEY_NODE_ID = "node_id"
        private const val KEY_COMPANION_ID = "companion_id"
        private const val KEY_USER_NAME = "user_name"
        private const val KEY_TOKEN_BUDGET = "token_budget"
        private const val KEY_MEMORY_TOKEN_BUDGET = "memory_token_budget"
        private const val KEY_MAX_MEMORY_ITEMS = "max_memory_items"
        private const val KEY_MAX_OUTPUT_TOKENS = "max_output_tokens"

        /** Stable per-device node id; registrations are keyed by this value. */
        fun defaultNodeId(context: Context): String = "android-${Settings.Secure.ANDROID_ID}"
    }
}
