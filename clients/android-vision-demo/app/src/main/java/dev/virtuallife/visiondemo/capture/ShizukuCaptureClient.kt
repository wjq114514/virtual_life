package dev.virtuallife.visiondemo.capture

import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.os.IBinder
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.system.Os
import android.system.OsConstants
import android.system.StructPollfd
import dev.virtuallife.visiondemo.debug.DebugLog
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import rikka.shizuku.Shizuku

data class CapturedScreen(
    val bytes: ByteArray,
    /** Monotonic capture-request start, not the unknown pixel exposure time. */
    val sampledAtElapsedNs: Long,
    val captureMs: Double,
)

class ShizukuCaptureClient(context: Context, private val log: DebugLog) : Closeable {
    private val args = Shizuku.UserServiceArgs(
        ComponentName(context.applicationContext, CaptureUserService::class.java),
    ).daemon(false).processNameSuffix("vision_capture").version(1)
    private val lock = Any()
    private val connectMutex = Mutex()
    private val captureMutex = Mutex()
    @Volatile private var closed = false
    @Volatile private var remote: IScreenCapture? = null
    private var connection: ServiceConnection? = null
    private var pending: CompletableDeferred<Unit>? = null
    private var activeFd: ParcelFileDescriptor? = null
    private val death = IBinder.DeathRecipient { disconnect("binder_died") }
    private val shizukuDeath = Shizuku.OnBinderDeadListener { disconnect("shizuku_died") }
    private val permissionResult = Shizuku.OnRequestPermissionResultListener { _, result ->
        if (result != PackageManager.PERMISSION_GRANTED) disconnect("permission_revoked")
    }

    init {
        Shizuku.addBinderDeadListener(shizukuDeath)
        Shizuku.addRequestPermissionResultListener(permissionResult)
    }

    val isConnected: Boolean
        get() = !closed && remote?.asBinder()?.isBinderAlive == true && hasPermission()

    fun requestPermission(activity: Activity) {
        check(!closed) { "capture_client_closed" }
        check(!activity.isFinishing && !activity.isDestroyed) { "activity_unavailable" }
        check(Shizuku.pingBinder()) { "shizuku_unavailable" }
        if (!hasPermission()) Shizuku.requestPermission(PERMISSION_REQUEST_CODE)
    }

    suspend fun connect() = connectMutex.withLock {
        check(!closed) { "capture_client_closed" }
        if (isConnected) return@withLock
        check(hasPermission()) { "shizuku_permission_required" }
        disconnect("reconnect")
        val ready = CompletableDeferred<Unit>()
        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName, binder: IBinder) {
                synchronized(lock) {
                    if (closed || connection !== this || !hasPermission()) {
                        ready.completeExceptionally(IOException("stale_capture_connection"))
                        return
                    }
                    try {
                        binder.linkToDeath(death, 0)
                        remote = IScreenCapture.Stub.asInterface(binder)
                        ready.complete(Unit)
                        log.event("capture_connected")
                    } catch (e: Exception) {
                        ready.completeExceptionally(e)
                    }
                }
            }
            override fun onServiceDisconnected(name: ComponentName) {
                if (synchronized(lock) { connection === this }) disconnect("service_disconnected")
            }
            override fun onBindingDied(name: ComponentName) = onServiceDisconnected(name)
            override fun onNullBinding(name: ComponentName) = onServiceDisconnected(name)
        }
        synchronized(lock) {
            check(!closed) { "capture_client_closed" }
            pending = ready
            connection = conn
        }
        try {
            synchronized(lock) {
                check(!closed && connection === conn) { "capture_client_disconnected" }
                Shizuku.bindUserService(args, conn)
            }
            withTimeout(10_000) { ready.await() }
        } catch (e: Throwable) {
            disconnect("connect_failed")
            throw e
        }
    }

    suspend fun capture(): CapturedScreen = captureMutex.withLock {
        withContext(Dispatchers.IO) {
            if (!hasPermission()) {
                disconnect("permission_revoked")
                throw SecurityException("shizuku_permission_required")
            }
            check(isConnected) { "capture_not_connected" }
            val started = SystemClock.elapsedRealtimeNanos()
            try {
                val service = remote ?: throw IOException("capture_not_connected")
                // This IPC only creates a pipe and schedules work; no screencap waits on Binder.
                val fd = service.capture() ?: throw IOException("capture_missing_fd")
                synchronized(lock) {
                    if (closed || remote !== service) {
                        fd.close()
                        throw IOException("capture_disconnected")
                    }
                    activeFd = fd
                }
                val bytes = try { fd.use { readPng(it, started) } } finally {
                    synchronized(lock) { if (activeFd === fd) activeFd = null }
                }
                validatePng(bytes)
                val finished = SystemClock.elapsedRealtimeNanos()
                val duration = (finished - started) / 1_000_000.0
                log.event("capture_complete", mapOf("bytes" to bytes.size, "capture_ms" to duration))
                CapturedScreen(bytes, started, duration)
            } catch (e: Exception) {
                if (e is SecurityException || !hasPermission()) disconnect("permission_revoked")
                log.event("capture_error", mapOf("error_type" to e.javaClass.simpleName))
                throw e
            }
        }
    }

    private suspend fun readPng(fd: ParcelFileDescriptor, started: Long): ByteArray {
        val poll = StructPollfd().apply {
            this.fd = fd.fileDescriptor
            events = OsConstants.POLLIN.toShort()
        }
        val buffer = ByteArray(16 * 1024)
        val bytes = ByteArrayOutputStream()
        while (true) {
            currentCoroutineContext().ensureActive()
            if (!hasPermission()) throw SecurityException("shizuku_permission_required")
            if ((SystemClock.elapsedRealtimeNanos() - started) / 1_000_000 >= CAPTURE_TIMEOUT_MS + 1000) {
                throw IOException("capture_timeout")
            }
            if (Os.poll(arrayOf(poll), 100) == 0) continue
            val count = Os.read(fd.fileDescriptor, buffer, 0, buffer.size)
            if (count == 0) break
            if (bytes.size() + count > MAX_PNG_BYTES) throw IOException("capture_too_large")
            bytes.write(buffer, 0, count)
        }
        fd.checkError()
        return bytes.toByteArray()
    }

    private fun validatePng(bytes: ByteArray) {
        val signature = byteArrayOf(137.toByte(), 80, 78, 71, 13, 10, 26, 10)
        if (bytes.size < 24 || !bytes.copyOfRange(0, 8).contentEquals(signature)) {
            throw IOException("invalid_capture_png")
        }
        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
        if (options.outWidth <= 0 || options.outHeight <= 0 ||
            options.outWidth.toLong() * options.outHeight > 32_000_000L) {
            throw IOException("invalid_capture_dimensions")
        }
        options.inJustDecodeBounds = false
        var sampleSize = 1
        while (maxOf(options.outWidth, options.outHeight) / sampleSize > 128) sampleSize *= 2
        options.inSampleSize = sampleSize
        val preview = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
            ?: throw IOException("invalid_capture_png")
        try {
            if (CapturePolicy().evaluate(preview, 0).reason == "blank_frame") {
                // This is only a blank-image heuristic; it cannot identify FLAG_SECURE reliably.
                throw IOException("blank_capture")
            }
        } finally {
            preview.recycle()
        }
    }

    private fun hasPermission(): Boolean = runCatching {
        Shizuku.pingBinder() && Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
    }.getOrDefault(false)

    private fun disconnect(reason: String) {
        val old = synchronized(lock) {
            remote?.asBinder()?.let { runCatching { it.unlinkToDeath(death, 0) } }
            remote = null
            runCatching { activeFd?.close() }
            activeFd = null
            pending?.completeExceptionally(IOException(reason))
            pending = null
            connection.also { connection = null }
        }
        if (old != null) runCatching { Shizuku.unbindUserService(args, old, true) }
        log.event("capture_disconnected", mapOf("reason" to reason))
    }

    /** Final disposal; a disposed instance must be replaced. connect() handles transient loss. */
    override fun close() {
        synchronized(lock) {
            if (closed) return
            closed = true
        }
        disconnect("closed")
        Shizuku.removeBinderDeadListener(shizukuDeath)
        Shizuku.removeRequestPermissionResultListener(permissionResult)
    }

    companion object { const val PERMISSION_REQUEST_CODE = 4130 }
}
