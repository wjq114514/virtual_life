package dev.virtuallife.chat

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.setValue

class MainActivity : ComponentActivity() {
    /** Bumps when the config changes so the composition repoints at the rebuilt controller. */
    private val generation = mutableIntStateOf(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ChatCore.ensure(this, ChatConfigStore(this).load())
        setContent {
            ChatTheme {
                // Reading the counter registers a snapshot so a config change repoints this
                // composition at the rebuilt controller.
                generation.value
                ChatCore.current()?.let { ChatShell(it, ::applyConfig) }
            }
        }
    }

    private fun applyConfig(next: ChatConfig) {
        ChatConfigStore(this).save(next)
        ChatCore.close()
        ChatCore.ensure(this, next)
        generation.value++
    }
}
