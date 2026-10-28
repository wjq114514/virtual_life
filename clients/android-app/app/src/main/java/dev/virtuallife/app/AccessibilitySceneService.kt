package dev.virtuallife.app

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.graphics.Rect
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Optional accessibility scene collector.
 *
 * This service is opt-in: the user must enable it in system settings, and the
 * app must disclose its purpose. It only reads the active window's structure
 * and a bounded set of controls; it never performs actions. The collected
 * [SceneSnapshot] is cached and picked up by [ServerScreenController] on the
 * next frame so the screen-vision prompt gets window/activity context without
 * a second pixel-only pass.
 *
 * Titles and node text are always sanitized; when [sceneTitlesEnabled] is false
 * (the default) only package and class names are reported, mirroring the Linux
 * niri scene collector's opt-in behavior.
 */
class AccessibilitySceneService : AccessibilityService() {
    companion object {
        @Volatile
        var latest: SceneSnapshot? = null
            private set

        @Volatile
        var includeTitles: Boolean = false

        private const val MAX_EVENTS_PER_SECOND = 8
        private var lastEmitMs = 0L
        private var lastSnapshot: SceneSnapshot? = null
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        // We are a reader, not a controller.
        serviceInfo = serviceInfo.also {
            it.flags = it.flags or
                android.accessibilityservice.AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or
                android.accessibilityservice.AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
            it.eventTypes = AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED or
                AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED or
                AccessibilityEvent.TYPE_WINDOWS_CHANGED
            it.feedbackType = android.accessibilityservice.AccessibilityServiceInfo.FEEDBACK_GENERIC
            it.notificationTimeout = 100
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        val now = System.currentTimeMillis()
        if (now - lastEmitMs < 1000 / MAX_EVENTS_PER_SECOND) return
        lastEmitMs = now
        val snapshot = collect()
        if (snapshot != null) {
            latest = snapshot
            lastSnapshot = snapshot
        }
    }

    override fun onInterrupt() {
        // No-op: the collector is passive.
    }

    private fun collect(): SceneSnapshot? {
        val root = rootInActiveWindow ?: return lastSnapshot
        val pkg = root.packageName?.toString()
        val cls = root.className?.toString()
        val screen = resources.displayMetrics
        val title = windowTitle(root, includeTitles)
        return SceneBuilder.fromRoot(
            root,
            pkg,
            cls,
            title,
            includeTitles,
            screen.widthPixels,
            screen.heightPixels,
        ) ?: lastSnapshot
    }

    private fun windowTitle(root: AccessibilityNodeInfo, includeTitles: Boolean): String? {
        if (!includeTitles) return null
        // Prefer the focused window's title from a focused node or content-desc.
        val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        val candidate = focused ?: root
        val text = candidate.contentDescription?.toString() ?: candidate.text?.toString()
        return text?.takeIf { it.isNotBlank() }
    }
}
