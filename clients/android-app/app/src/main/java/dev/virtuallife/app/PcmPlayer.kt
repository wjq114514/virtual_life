package dev.virtuallife.app

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack

/**
 * Streaming PCM16 playback. Every write is checked; a dead track is torn down so a
 * later segment can recreate it and stop the old reply.
 */
class PcmPlayer {
    private var track: AudioTrack? = null
    private var sampleRate = 0
    private var channels = 0

    @Volatile
    private var stopped = false

    @Synchronized
    fun start(rate: Int, channelCount: Int) {
        stopped = false
        if (track != null && rate == sampleRate && channelCount == channels) return
        releaseLocked()
        val channelMask =
            if (channelCount == 2) AudioFormat.CHANNEL_OUT_STEREO else AudioFormat.CHANNEL_OUT_MONO
        val minBuffer = AudioTrack.getMinBufferSize(
            rate,
            channelMask,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufferSize = maxOf(minBuffer, rate * channelCount * 2 / 5)
        val builder = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(rate)
                    .setChannelMask(channelMask)
                    .build(),
            )
            .setBufferSizeInBytes(bufferSize)
            .setTransferMode(AudioTrack.MODE_STREAM)
        val created = builder.build()
        if (created.state != AudioTrack.STATE_INITIALIZED) {
            created.release()
            throw IllegalStateException("AudioTrack could not be initialized")
        }
        created.play()
        track = created
        sampleRate = rate
        channels = channelCount
    }

    /** Writes the whole buffer unless stopped or the track dies. */
    @Synchronized
    fun write(bytes: ByteArray): Int {
        val current = track ?: return 0
        var offset = 0
        while (offset < bytes.size && !stopped) {
            val written = current.write(bytes, offset, bytes.size - offset, AudioTrack.WRITE_BLOCKING)
            if (written < 0) {
                releaseLocked()
                throw IllegalStateException("AudioTrack write failed: $written")
            }
            offset += written
        }
        return offset
    }

    fun isPlaying(): Boolean = track?.playState == AudioTrack.PLAYSTATE_PLAYING

    /** Real playback-head position in frames, not bytes written into the buffer. */
    fun playedSamples(): Long = track?.playbackHeadPosition?.toLong()?.and(0xFFFFFFFFL) ?: 0L

    fun stop() {
        synchronized(this) {
            stopped = true
            val current = track ?: return
            current.pause()
            current.flush()
            current.stop()
        }
    }

    fun release() = synchronized(this) { releaseLocked() }

    private fun releaseLocked() {
        val current = track ?: return
        track = null
        runCatching {
            current.pause()
            current.flush()
            current.stop()
        }
        current.release()
    }
}
