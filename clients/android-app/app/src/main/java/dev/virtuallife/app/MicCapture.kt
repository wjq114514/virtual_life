package dev.virtuallife.app

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder

/** One mono PCM16 AudioRecord. The caller owns the reading thread. */
class MicCapture {
    private var record: AudioRecord? = null

    var sampleRate: Int = LocalAsr.SAMPLE_RATE
        private set

    val frameSamples: Int = LocalAsr.SAMPLE_RATE / 10

    var initialized: Boolean = false
        private set

    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        val minBuffer = AudioRecord.getMinBufferSize(
            LocalAsr.SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuffer <= 0) return false
        val bufferSize = maxOf(minBuffer, frameSamples * 2 * 4)
        val created = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            LocalAsr.SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufferSize,
        )
        if (created.state != AudioRecord.STATE_INITIALIZED) {
            created.release()
            return false
        }
        record = created
        // The device may not honour 16 kHz exactly; report the real rate.
        sampleRate = if (created.sampleRate > 0) created.sampleRate else LocalAsr.SAMPLE_RATE
        created.startRecording()
        initialized = created.recordingState == AudioRecord.RECORDSTATE_RECORDING
        return initialized
    }

    fun read(target: ShortArray): Int = record?.read(target, 0, target.size) ?: -1

    fun stop() {
        val current = record ?: return
        record = null
        initialized = false
        runCatching {
            if (current.recordingState == AudioRecord.RECORDSTATE_RECORDING) current.stop()
        }
        current.release()
    }
}
