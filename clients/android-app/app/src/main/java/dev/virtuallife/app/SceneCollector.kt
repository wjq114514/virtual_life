package dev.virtuallife.app

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Rule-based activity classification for host-reported scene data.
 *
 * Mirror of the Linux [scene_classify.py] rule layer: every decision is
 * traceable to an input field (package, activity class, title, role) and returns
 * "unknown" instead of guessing. The labels match the server's
 * [AnalysisActivity] / [AttentionSample.activity] values.
 */
object ActivityClassifier {
    private val TERMINAL_APPS = setOf(
        "com.termux", "jackpal.androidterm", "com.termux.app", "com.samsung.android.app.notes",
    )
    private val CODE_APPS = setOf(
        "com.androidstudio", "com.tiagoferreirag.todo", "com.termux.app",
    )
    private val BROWSER_APPS = setOf(
        "com.android.chrome", "org.mozilla.firefox", "com.brave.browser",
        "com.microsoft.emmx", "com.opera.browser", "com.vivaldi.browser",
        "org.chromium.webview_shell", "com.sec.android.app.sbrowser",
    )
    private val VIDEO_APPS = setOf(
        "com.google.android.youtube", "tv.twitch.android.app", "com.netflix.mediaclient",
        "com.bilibili.app.in", "org.videolan.vlc", "com.google.android.exoplayer2",
        "com.mxtech.videoplayer.ad", "com.tencent.qqlive",
    )
    private val MEETING_APPS = setOf(
        "us.zoom.zpmobile", "com.microsoft.teams", "com.google.android.gm",
        "com.cisco.webex.meetings", "com.jitsi.meet",
    )
    private val GAME_APPS = setOf(
        "com.tencent.tmgp.sgame", "com.mihoyo.yuanshen", "com.mihoyo.hkrpg",
        "com.supercell.clashofclans", "com.king.candycrushsaga", "com.valvesoftware.android.steam.community",
        "com.epicgames.portal", "com.roblox.client",
    )
    private val CHAT_APPS = setOf(
        "org.telegram.messenger", "com.whatsapp", "com.discord", "com.slack",
        "com.tencent.mm", "com.tencent.mobileqq", "org.thoughtcrime.securesms",
        "im.vector.app", "org.gnome.Evolution",
    )
    private val MEDIA_APPS = setOf(
        "com.spotify.music", "com.zhihu.android", "com.netease.cloudmusic",
        "com.tencent.qqmusic", "com.google.android.apps.music",
    )
    private val DOCS_APPS = setOf(
        "com.google.android.apps.docs", "com.adobe.reader", "org.mozilla.gecko",
        "com.android.chrome", "com.microsoft.office.officehubrow",
    )

    private val CODE_TITLE_MARKERS = listOf(
        " — Visual Studio Code", " — VSCodium", " — Zed", " — Sublime Text",
        " — PyCharm", " — IntelliJ IDEA", " — GoLand", " — WebStorm",
    )
    private val CODE_EXTENSIONS = listOf(
        ".py", ".rs", ".go", ".js", ".ts", ".tsx", ".jsx", ".kt", ".kts",
        ".java", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".rb", ".php",
        ".lua", ".sh", ".bash", ".zsh", ".toml", ".yaml", ".yml", ".json", ".sql",
    )
    private val MEETING_TITLE_MARKERS = listOf("zoom", "meet", "google meet", "teams", "webex", "jitsi", "gather")
    private val VIDEO_TITLE_MARKERS = listOf("youtube", "bilibili", "netflix", "twitch", " - mpv")
    private val CHAT_TITLE_MARKERS = listOf("discord", "slack", "telegram", "whatsapp", "signal", "微信", "element")
    private val DOCS_TITLE_EXTENSIONS = listOf(".pdf", ".epub", ".djvu", ".odt", ".doc", ".docx", ".xlsx", ".pptx")

    fun classifyActivity(packageName: String?, className: String?, title: String?): String {
        val pkg = (packageName ?: "").lowercase()
        val cls = (className ?: "").lowercase()
        val rawTitle = (title ?: "").trim()
        val titleLower = rawTitle.lowercase()

        // Meeting and video before code/browser because a video player or
        // meeting app can carry a document-like title.
        if (pkg in MEETING_APPS || MEETING_TITLE_MARKERS.any { titleLower.contains(it) }) {
            return "meeting"
        }
        if (pkg in VIDEO_APPS || VIDEO_TITLE_MARKERS.any { titleLower.contains(it) }) {
            return "video"
        }
        if (pkg in GAME_APPS || pkg.startsWith("com.mihoyo") || pkg.startsWith("com.tencent.tmgp")) {
            return "game"
        }
        if (pkg in CODE_APPS || cls in CODE_APPS ||
            CODE_TITLE_MARKERS.any { titleLower.contains(it) }
        ) {
            return "code"
        }
        if (pkg in TERMINAL_APPS || cls in TERMINAL_APPS) {
            return "terminal"
        }
        if (pkg in BROWSER_APPS || cls in BROWSER_APPS) {
            return "browser"
        }
        if (pkg in CHAT_APPS || CHAT_TITLE_MARKERS.any { titleLower.contains(it) }) {
            return "chat"
        }
        if (pkg in MEDIA_APPS) {
            return "media"
        }
        if (pkg in DOCS_APPS || DOCS_TITLE_EXTENSIONS.any { rawTitle.endsWith(it) }) {
            return "docs"
        }
        // A bare source filename without a known editor package is still likely code.
        if (CODE_EXTENSIONS.any { rawTitle.endsWith(it) } && "/" !in rawTitle) {
            return "code"
        }
        return "unknown"
    }

    fun classifyFocus(hasFocusedWindow: Boolean): String =
        if (hasFocusedWindow) "focused" else "unknown"
}

/**
 * One bounded scene snapshot collected from the platform accessibility tree.
 */
data class SceneSnapshot(
    val clientFeatures: Map<String, Any?>? = null,
    val hostMetadata: Map<String, Any?>? = null,
    val activity: String = "unknown",
    val focus: String = "unknown",
    val appId: String? = null,
    val className: String? = null,
    val windowTitle: String? = null,
)

/**
 * Builds a [SceneSnapshot] from a root accessibility node without touching
 * any user content beyond the focused window and a bounded set of controls.
 *
 * Titles and text are always sanitized and truncated before they leave the
 * host; this mirrors the Linux `niri_scene` opt-in behavior.
 */
object SceneBuilder {
    const val MAX_WINDOWS = 8
    const val MAX_TITLE_CHARS = 160
    const val MAX_TEXT_CHARS = 80
    private const val MAX_ELEMENTS = 12

    fun sanitizeText(text: String, limit: Int = MAX_TITLE_CHARS): String {
        // Drop control/invisible/format characters and collapse whitespace.
        val builder = StringBuilder()
        for (char in text) {
            val type = Character.getType(char)
            if (type == Character.FORMAT.toInt() || type == Character.CONTROL.toInt() ||
                type == Character.SURROGATE.toInt() || type == Character.PRIVATE_USE.toInt() ||
                type == Character.UNASSIGNED.toInt()
            ) {
                continue
            }
            builder.append(char)
        }
        val collapsed = builder.toString().replace(Regex("\\s+"), " ").trim()
        return if (collapsed.length > limit) collapsed.substring(0, limit).trim() else collapsed
    }

    /**
     * Collect a bounded scene from the current window root.
     * Returns null when no root is available.
     */
    fun fromRoot(
        root: AccessibilityNodeInfo?,
        packageName: String?,
        className: String?,
        windowTitle: String?,
        includeTitles: Boolean,
        screenWidth: Int,
        screenHeight: Int,
    ): SceneSnapshot? {
        if (root == null) return null
        val sanitizedPkg = sanitizeText(packageName ?: "", 128)
        val sanitizedCls = sanitizeText(className ?: "", 128)
        val sanitizedTitle = if (includeTitles) sanitizeText(windowTitle ?: "", MAX_TITLE_CHARS) else ""

        val elements = mutableListOf<Map<String, Any?>>()
        // Focused window entry first, exactly like the niri scene collector.
        if (sanitizedTitle.isNotEmpty()) {
            elements.add(
                mapOf(
                    "element_id" to "window:focus",
                    "role" to "focused_window",
                    "text" to sanitizedTitle,
                    "bbox" to listOf(0.0, 0.0, 1.0, 1.0),
                    "confidence" to 1.0,
                ),
            )
        }
        collectElements(root, elements, includeTitles, screenWidth, screenHeight, 0)

        val clientFeatures = mapOf(
            "schema_version" to "1.0",
            "extractor" to "android_a11y",
            "extractor_version" to "1",
            "ui_elements" to elements,
        )
        val hostMetadata = mutableMapOf<String, Any?>(
            "app_id" to sanitizedPkg.ifEmpty { null },
            "app_class" to sanitizedCls.ifEmpty { null },
        )

        val activity = ActivityClassifier.classifyActivity(sanitizedPkg, sanitizedCls, sanitizedTitle)
        val focus = ActivityClassifier.classifyFocus(true)

        return SceneSnapshot(
            clientFeatures = clientFeatures,
            hostMetadata = hostMetadata,
            activity = activity,
            focus = focus,
            appId = sanitizedPkg.ifEmpty { null },
            className = sanitizedCls.ifEmpty { null },
            windowTitle = sanitizedTitle.ifEmpty { null },
        )
    }

    private fun collectElements(
        node: AccessibilityNodeInfo,
        out: MutableList<Map<String, Any?>>,
        includeTitles: Boolean,
        screenWidth: Int,
        screenHeight: Int,
        depth: Int,
    ) {
        if (out.size >= MAX_ELEMENTS) return
        val role = nodeRole(node)
        val text = if (includeTitles) sanitizeText(node.text?.toString() ?: "", MAX_TEXT_CHARS) else ""
        if (role.isNotEmpty() && (text.isNotEmpty() || isInteractive(role))) {
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            val bbox = normalizeBbox(bounds, screenWidth, screenHeight)
            if (bbox != null) {
                out.add(
                    mapOf(
                        "element_id" to "a11y:${node.hashCode()}",
                        "role" to role,
                        "text" to text,
                        "bbox" to bbox,
                    ),
                )
            }
        }
        if (depth >= 4) return
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            collectElements(child, out, includeTitles, screenWidth, screenHeight, depth + 1)
        }
    }

    private fun nodeRole(node: AccessibilityNodeInfo): String {
        if (node.isFocused) return "focused"
        return when (node.className?.toString()?.lowercase() ?: "") {
            "android.widget.button", "android.widget.imagebutton" -> "button"
            "android.widget.edittext" -> "text_field"
            "android.widget.checkbox" -> "checkbox"
            "android.widget.spinner" -> "dropdown"
            "android.widget.textview" -> "text"
            "android.widget.listview" -> "list"
            else -> "unknown"
        }
    }

    private fun isInteractive(role: String): Boolean =
        role == "button" || role == "text_field" || role == "checkbox" ||
            role == "dropdown" || role == "focused" || role == "list"

    private fun normalizeBbox(bounds: Rect, screenWidth: Int, screenHeight: Int): List<Double>? {
        if (screenWidth <= 0 || screenHeight <= 0 || bounds.width() <= 0 || bounds.height() <= 0) {
            return null
        }
        return listOf(
            bounds.left.toDouble() / screenWidth,
            bounds.top.toDouble() / screenHeight,
            bounds.right.toDouble() / screenWidth,
            bounds.bottom.toDouble() / screenHeight,
        ).map { it.coerceIn(0.0, 1.0) }
    }
}
