package dev.virtuallife.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ActivityClassifierTest {
    @Test
    fun classifyByPackage() {
        assertEquals("browser", ActivityClassifier.classifyActivity("com.android.chrome", null, "Mozilla"))
        assertEquals("terminal", ActivityClassifier.classifyActivity("com.termux", null, "~"))
        assertEquals("game", ActivityClassifier.classifyActivity("com.mihoyo.yuanshen", null, ""))
        assertEquals("meeting", ActivityClassifier.classifyActivity("us.zoom.zpmobile", null, "Zoom Meeting"))
        assertEquals("chat", ActivityClassifier.classifyActivity("org.telegram.messenger", null, ""))
        assertEquals("docs", ActivityClassifier.classifyActivity("com.google.android.apps.docs", null, ""))
    }

    @Test
    fun classifyByTitle() {
        assertEquals("meeting", ActivityClassifier.classifyActivity(null, null, "Google Meet"))
        assertEquals("code", ActivityClassifier.classifyActivity(null, null, "MainActivity.kt"))
        assertEquals("docs", ActivityClassifier.classifyActivity(null, null, "report.pdf"))
        assertEquals("unknown", ActivityClassifier.classifyActivity(null, null, "Random stuff"))
    }

    @Test
    fun classifyFocus() {
        assertEquals("focused", ActivityClassifier.classifyFocus(true))
        assertEquals("unknown", ActivityClassifier.classifyFocus(false))
    }

    @Test
    fun sanitizeTextDropsInvisible() {
        assertEquals("hello world", SceneBuilder.sanitizeText("hel\u200blo\u202e world"))
        assertEquals("ab", SceneBuilder.sanitizeText("a\u0000b"))
        assertTrue(SceneBuilder.sanitizeText("x".repeat(500)).length <= SceneBuilder.MAX_TITLE_CHARS)
    }
}
