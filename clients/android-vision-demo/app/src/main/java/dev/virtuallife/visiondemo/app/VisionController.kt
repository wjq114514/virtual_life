package dev.virtuallife.visiondemo.app

import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.PowerManager
import android.app.KeyguardManager
import android.os.SystemClock
import dev.virtuallife.visiondemo.capture.*
import dev.virtuallife.visiondemo.debug.DebugLog
import dev.virtuallife.visiondemo.inference.*
import java.io.File
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

internal data class UiState(
    val status: String = "就绪", val connected: Boolean = false,
    val authorized: Boolean = false, val shizukuAvailable: Boolean = false,
    val loaded: Boolean = false, val running: Boolean = false, val busy: Boolean = false,
    val result: String = "", val timing: String = "", val preview: File? = null,
    val config: String = "", val logs: String = "",
)

internal class VisionController private constructor(private val context: Context) {
    companion object {
        @Volatile private var instance: VisionController? = null
        fun get(context: Context): VisionController = instance ?: synchronized(this) {
            instance ?: VisionController(context.applicationContext).also { instance = it }
        }
    }
    val log = DebugLog(context)
    private val capture = ShizukuCaptureClient(context, log)
    @Volatile private var engine = MnnVisionEngine(context, log)
    private val store = ModelStore(context, log)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val nativeLock = Mutex()
    private val captureLock = Mutex()
    private val mutable = MutableStateFlow(UiState())
    val state = mutable.asStateFlow()
    @Volatile private var session = 0L
    private var frame = 0L
    @Volatile private var activeFrame = 0L
    private var sampling: Job? = null
    private var consumer: Job? = null
    private var manual: Job? = null
    private var queue: Channel<Frame>? = null
    private data class Frame(val screen: CapturedScreen, val id: Long, val session: Long)
    private fun update(block: (UiState) -> UiState) { mutable.value = block(mutable.value) }
    private fun event(name: String, sid: Long = session, fid: Long? = null, extra: Map<String, Any?> = emptyMap()) {
        log.event(name, mapOf("session" to sid, "frame" to fid) + extra)
        update { it.copy(logs = log.tail()) }
    }
    private fun task(block: suspend () -> Unit): Job {
        val taskSession = session
        return scope.launch {
        try { block() } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (session == taskSession) {
            event("error", extra = mapOf("error_type" to e.javaClass.simpleName))
            stop()
            context.stopService(android.content.Intent(context, VisionService::class.java))
            update { it.copy(status = "失败：${e.message}", connected = capture.isConnected, running = false) }
        } }
        }
    }
    fun refreshConnection() {
        val available = runCatching { rikka.shizuku.Shizuku.pingBinder() }.getOrDefault(false)
        val authorized = available && runCatching { rikka.shizuku.Shizuku.checkSelfPermission() == android.content.pm.PackageManager.PERMISSION_GRANTED }.getOrDefault(false)
        val connected = capture.isConnected
        update { it.copy(connected = connected, authorized = authorized, shizukuAvailable = available) }
        if (mutable.value.running && (!available || !authorized || !connected)) stopForCaptureLoss()
    }
    private fun stopForCaptureLoss() {
        event("capture_connection_lost")
        stop()
        context.stopService(android.content.Intent(context, VisionService::class.java))
        update { it.copy(status = "Shizuku 已断开或授权失效，观察已停止", connected = false) }
    }
    fun permission(activity: Activity) { capture.requestPermission(activity) }
    fun connect() = task {
        val client = capture
        val sid = session
        withContext(Dispatchers.IO) { captureLock.withLock { client.connect() } }
        if (session == sid && client === capture) update { it.copy(connected = client.isConnected, status = "Shizuku 已连接") }
    }
    private var operation = false
    private fun operation(block: suspend (Long) -> Unit) {
        if (operation || mutable.value.running) return
        operation = true
        update { it.copy(busy = true) }
        val sid = session
        task {
            try { block(sid) } finally {
                operation = false
                update { it.copy(busy = false, loaded = engine.isLoaded) }
            }
        }
    }
    fun importModel(uri: Uri) {
        if (mutable.value.loaded) return
        operation { sid ->
            update { it.copy(status = "正在导入模型…") }
            val file = withContext(Dispatchers.IO) { store.importTree(uri) }
            if (session == sid) update { it.copy(config = file.absolutePath, status = "导入完成") }
        }
    }
    fun load(path: String, backend: String, threads: Int) = operation { sid ->
        update { it.copy(status = "正在加载模型…") }
        withContext(Dispatchers.IO) { nativeLock.withLock {
            engine.load(store.resolveConfigPath(path).absolutePath, backend, threads)
        } }
        if (session == sid) {
            update { it.copy(loaded = engine.isLoaded, status = "模型已加载") }
            event("model_loaded")
        }
    }
    fun unload() {
        if (operation) return
        stop()
        operation { sid ->
            withContext(Dispatchers.IO) { nativeLock.withLock { engine.close(); engine = MnnVisionEngine(context, log) } }
            if (session == sid) { update { it.copy(loaded = false, status = "模型已卸载") }; event("model_unloaded") }
        }
    }
    private fun screenAvailable(): Boolean = context.getSystemService(PowerManager::class.java).isInteractive &&
        !context.getSystemService(KeyguardManager::class.java).isKeyguardLocked
    private suspend fun sample(sid: Long): Frame {
        val id = ++frame
        event("capture_start", sid, id)
        val client = capture
        val screen = withContext(Dispatchers.IO) { captureLock.withLock { client.capture() } }
        event("capture_end", sid, id, mapOf("capture_ms" to screen.captureMs))
        return Frame(screen, id, sid)
    }
    fun start(interval: Long, prompt: String) {
        if (mutable.value.running || operation || !mutable.value.loaded) return
        manual?.cancel(); engine.cancel()
        session++
        val sid = session
        val channel = Channel<Frame>(Channel.CONFLATED)
        queue = channel
        update { it.copy(running = true, status = "持续观察中") }
        event("session_start", sid)
        val policy = CapturePolicy(minIntervalMs = 2500, maxIntervalMs = 15000)
        sampling = task {
            while (currentCoroutineContext().isActive && sid == session) {
                if (!capture.isConnected) {
                    stopForCaptureLoss()
                    break
                }
                if (screenAvailable()) {
                    val captured = sample(sid)
                    if (sid == session && ageMs(captured) <= 3000 && screenAvailable()) channel.trySend(captured)
                    else event("frame_discarded", sid, captured.id, mapOf("reason" to "stale_or_paused"))
                }
                else { update { it.copy(status = "熄屏或锁屏，已暂停") }; event("screen_paused", sid) }
                delay(interval.coerceIn(250, 60000))
            }
        }.also { job -> job.invokeOnCompletion { channel.close() } }
        consumer = task {
            for (next in channel) {
                if (sid != session || !screenAvailable()) continue
                withContext(Dispatchers.IO) { nativeLock.withLock {
                    var latest = next
                    while (true) { latest = channel.tryReceive().getOrNull() ?: break }
                    analyze(latest, prompt, policy, false)
                } }
            }
        }
    }
    fun manual(prompt: String) {
        if (mutable.value.running || operation || manual?.isActive == true || !mutable.value.loaded) return
        session++
        val sid = session
        update { it.copy(running = true, status = "3秒后截图，请切换到目标应用…") }
        event("manual_countdown", sid)
        manual = task {
            try {
                delay(3000)
                if (!screenAvailable()) error("请解锁并点亮屏幕")
                val sampled = sample(sid)
                withContext(Dispatchers.IO) { nativeLock.withLock {
                    analyze(sampled, prompt, CapturePolicy(), true)
                } }
            } finally {
                if (sid == session) update { it.copy(running = false) }
            }
        }
    }
    // Keep the first-visible definition aligned with native utf8_segments.hpp.
    private fun containsVisibleScalar(delta: String): Boolean = delta.codePoints().anyMatch { cp ->
        !(cp <= 0x20 || cp in 0x7F..0xA0 || cp == 0x1680 ||
            cp in 0x2000..0x200F || cp in 0x2028..0x202F || cp == 0x205F ||
            cp == 0x2060 || cp == 0x3000 || cp == 0xFEFF)
    }
    private fun ageMs(frame: Frame) = (SystemClock.elapsedRealtimeNanos() - frame.screen.sampledAtElapsedNs) / 1e6
    private suspend fun analyze(next: Frame, prompt: String, policy: CapturePolicy, force: Boolean) {
        withContext(Dispatchers.IO) {
            if (next.session != session || !screenAvailable() || ageMs(next) > 3000) return@withContext
            val preprocessStarted = SystemClock.elapsedRealtimeNanos()
            val bitmap = BitmapFactory.decodeByteArray(next.screen.bytes, 0, next.screen.bytes.size)
                ?: error("截图解码失败")
            val originalWidth = bitmap.width
            val originalHeight = bitmap.height
            val file = File(context.cacheDir, "vision-${next.session}-${next.id}.png")
            var width = 0
            var height = 0
            try {
            val decision = try {
                val probeScale = 96.0 / maxOf(bitmap.width, bitmap.height)
                val probe = Bitmap.createScaledBitmap(bitmap, maxOf(1, (bitmap.width * probeScale).toInt()), maxOf(1, (bitmap.height * probeScale).toInt()), true)
                val decision = try { policy.evaluate(probe, SystemClock.elapsedRealtime(), force) }
                    finally { if (probe !== bitmap) probe.recycle() }
                if (decision.accepted) {
                    val scale = minOf(1.0, 960.0 / maxOf(bitmap.width, bitmap.height))
                    width = maxOf(1, (bitmap.width * scale).toInt())
                    height = maxOf(1, (bitmap.height * scale).toInt())
                    val resized = Bitmap.createScaledBitmap(bitmap, width, height, true)
                    try { file.outputStream().use { check(resized.compress(Bitmap.CompressFormat.PNG, 100, it)) { "截图压缩失败" } } }
                    finally { if (resized !== bitmap) resized.recycle() }
                }
                decision
            } finally { bitmap.recycle() }
            withContext(Dispatchers.Main) { event("policy", next.session, next.id, mapOf("accepted" to decision.accepted, "reason" to decision.reason)) }
            if (!decision.accepted) return@withContext
            if (next.session != session || !screenAvailable() || ageMs(next) > 3000) { file.delete(); return@withContext }
            val preprocessMs = (SystemClock.elapsedRealtimeNanos() - preprocessStarted) / 1e6
            val started = SystemClock.elapsedRealtimeNanos()
            activeFrame = next.id
            var first: Double? = null
            var captureToFirst: Double? = null
            val queueMs = ((preprocessStarted - next.screen.sampledAtElapsedNs) / 1e6 - next.screen.captureMs).coerceAtLeast(0.0)
            val output = StringBuilder()
            val finished = java.util.concurrent.atomic.AtomicBoolean(false)
            withContext(Dispatchers.Main) { if (session == next.session && activeFrame == next.id) {
                mutable.value.preview?.takeIf { it != file }?.let { old -> scope.launch(Dispatchers.IO) { old.delete() } }
                update { it.copy(preview = file, result = "", status = "正在分析…") }
                event("inference_start", next.session, next.id, mapOf(
                    "original_width" to originalWidth, "original_height" to originalHeight,
                    "input_width" to width, "input_height" to height, "preprocess_ms" to preprocessMs,
                    "queue_ms" to queueMs))
            } }
            try {
                currentCoroutineContext().ensureActive()
                if (next.session != session || !screenAvailable() || ageMs(next) > 3000) return@withContext
                val inferenceJob = currentCoroutineContext()[Job]
                val result = engine.analyze(file.absolutePath, prompt, 128,
                    shouldContinue = {
                        session == next.session && inferenceJob?.isActive != false && screenAvailable()
                    }) { delta ->
                    val now = SystemClock.elapsedRealtimeNanos()
                    val elapsed = (now - started) / 1e6
                    val isFirst = first == null && containsVisibleScalar(delta)
                    if (isFirst) {
                        first = elapsed
                        captureToFirst = (now - next.screen.sampledAtElapsedNs) / 1e6
                        log.event("first_delta", mapOf("session" to next.session, "frame" to next.id,
                            "wait_ms" to elapsed, "capture_to_first_ms" to captureToFirst,
                            "queue_ms" to queueMs))
                    }
                    output.append(delta)
                    val text = output.toString()
                    scope.launch { if (session == next.session && activeFrame == next.id) {
                        if (!finished.get()) update { it.copy(result = text) }
                    } }
                }
                finished.set(true)
                val total = (SystemClock.elapsedRealtimeNanos() - started) / 1e6
                withContext(Dispatchers.Main) { if (session == next.session && activeFrame == next.id) {
                    update { it.copy(result = result.text, status = "分析完成", timing =
                        "输入 ${originalWidth}×${originalHeight} → ${width}×${height}；预处理 $preprocessMs ms\n采集 ${next.screen.captureMs} ms；排队 $queueMs ms\n采集开始→首字 $captureToFirst ms；推理→首字 ${first ?: result.firstTokenMs} ms；推理总计 $total ms\n视觉 ${result.visionMs} / prefill ${result.prefillMs} / decode ${result.decodeMs} ms\n真实画面出现时刻未知") }
                    event("inference_end", next.session, next.id, mapOf("total_ms" to total))
                } }
            } finally { finished.set(true) }
            } finally {
                if (next.session != session || mutable.value.preview != file) file.delete()
            }
        }
    }
    fun stop() {
        session++
        activeFrame = 0L
        sampling?.cancel(); consumer?.cancel(); manual?.cancel(); queue?.cancel(); queue = null
        engine.cancel()
        update { it.copy(running = false, connected = capture.isConnected, status = "已停止") }
        val oldPreview = mutable.value.preview
        update { it.copy(preview = null) }
        scope.launch(Dispatchers.IO) { oldPreview?.delete() }
        event("session_stop")
    }
    fun export(uri: Uri) = task {
        withContext(Dispatchers.IO) { context.contentResolver.openOutputStream(uri, "wt")?.use { out ->
            log.exportFile().inputStream().use { it.copyTo(out) }
        } ?: error("无法写入目标文件") }
        update { it.copy(status = "日志已导出") }
    }
}
