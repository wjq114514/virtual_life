package dev.virtuallife.chat

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/** File-backed session store. A corrupt file degrades to a fresh state rather than crashing. */
class ChatStorage(private val file: File) {
    private val json = Json {
        prettyPrint = true
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    fun load(): ChatState = try {
        if (!file.exists()) ChatState.empty()
        else json.decodeFromString(ChatState.serializer(), file.readText())
            .takeIf { it.sessions.isNotEmpty() } ?: ChatState.empty()
    } catch (_: Exception) {
        ChatState.empty()
    }

    fun save(state: ChatState) {
        file.parentFile?.mkdirs()
        file.writeText(json.encodeToString(ChatState.serializer(), state))
    }

    fun clear() {
        if (file.exists()) file.delete()
    }
}
