package dev.virtuallife.visiondemo.capture

import android.os.Binder
import android.os.ParcelFileDescriptor
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.system.exitProcess

internal const val MAX_PNG_BYTES = 24 * 1024 * 1024
internal const val CAPTURE_TIMEOUT_MS = 8000L

/** Public zero-argument constructor is required by Shizuku's reflective UserService loader. */
class CaptureUserService : IScreenCapture.Stub() {
    private val busy = AtomicBoolean(false)
    private val worker = Executors.newSingleThreadExecutor()
    private val timer = Executors.newSingleThreadScheduledExecutor()
    @Volatile private var process: Process? = null
    @Volatile private var output: ParcelFileDescriptor? = null
    private val ownerUid = AtomicIntegerOwner()

    override fun capture(): ParcelFileDescriptor {
        // Bind the instance to its first app caller; no arbitrary command/arguments cross IPC.
        ownerUid.check(Binder.getCallingUid())
        check(busy.compareAndSet(false, true)) { "capture_busy" }
        val pipe = try { ParcelFileDescriptor.createReliablePipe() } catch (e: Exception) {
            busy.set(false)
            throw e
        }
        output = pipe[1]
        try {
            worker.execute {
                val timedOut = AtomicBoolean(false)
                val watchdog = timer.schedule({
                    timedOut.set(true)
                    process?.destroyForcibly()
                    runCatching { pipe[1].closeWithError("capture_timeout") }
                }, CAPTURE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                try {
                    val child = ProcessBuilder("/system/bin/screencap", "-p")
                        .redirectError(java.io.File("/dev/null")).start()
                    process = child
                    if (timedOut.get()) child.destroyForcibly()
                    val bytes = ByteArrayOutputStream()
                    child.inputStream.use { input ->
                        val buffer = ByteArray(16 * 1024)
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            if (bytes.size() + count > MAX_PNG_BYTES) throw IOException("capture_too_large")
                            bytes.write(buffer, 0, count)
                        }
                    }
                    if (timedOut.get() || child.waitFor() != 0) throw IOException("screencap_failed")
                    if (bytes.size() < 24) throw IOException("empty_capture")
                    ParcelFileDescriptor.AutoCloseOutputStream(pipe[1]).use { bytes.writeTo(it) }
                } catch (_: Exception) {
                    runCatching { pipe[1].closeWithError(if (timedOut.get()) "capture_timeout" else "capture_failed") }
                } finally {
                    watchdog.cancel(false)
                    process?.destroyForcibly()
                    process = null
                    runCatching { pipe[1].close() }
                    output = null
                    busy.set(false)
                }
            }
        } catch (e: Exception) {
            pipe.forEach { runCatching { it.close() } }
            busy.set(false)
            throw e
        }
        return pipe[0]
    }

    // AIDL transaction 16777114 is Shizuku's documented removal protocol.
    override fun destroy() {
        process?.destroyForcibly()
        runCatching { output?.closeWithError("service_destroyed") }
        worker.shutdownNow()
        timer.shutdownNow()
        exitProcess(0)
    }

    private class AtomicIntegerOwner {
        private val uid = java.util.concurrent.atomic.AtomicInteger(-1)
        fun check(caller: Int) {
            uid.compareAndSet(-1, caller)
            if (uid.get() != caller) throw SecurityException("wrong_capture_caller")
        }
    }
}
