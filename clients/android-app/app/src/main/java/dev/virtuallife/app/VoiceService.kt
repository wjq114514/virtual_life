package dev.virtuallife.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/** Holds the single engine so the UI and the foreground service share one node session. */
object VoiceCore {
    @Volatile
    var engine: VoiceEngine? = null
        private set

    fun ensure(context: Context): VoiceEngine = engine ?: synchronized(this) {
        engine ?: build(context).also { engine = it }
    }

    private fun build(context: Context): VoiceEngine {
        val appContext = context.applicationContext
        val androidId = Settings.Secure.getString(
            appContext.contentResolver,
            Settings.Secure.ANDROID_ID,
        ) ?: "unknown"
        val config = AppConfigStore(appContext).load(
            defaultNodeId = "android-$androidId",
            defaultCompanionId = "android-companion",
        )
        return VoiceEngine(appContext, config, OkHttpClient())
    }
}

class VoiceService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    private val engine: VoiceEngine
        get() = VoiceCore.ensure(this)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                promoteToForeground()
                scope.launch { engine.start() }
            }

            ACTION_STOP -> scope.launch {
                engine.stop()
                stopSelf()
            }

            ACTION_INTERRUPT -> scope.launch { engine.interrupt() }

            ACTION_TEXT -> {
                val text = intent.getStringExtra(EXTRA_TEXT)
                scope.launch { engine.commitText(text.orEmpty()) }
            }
        }
        return START_NOT_STICKY
    }

    private fun promoteToForeground() {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel),
                NotificationManager.IMPORTANCE_LOW,
            )
            manager.createNotificationChannel(channel)
        }
        val tap = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText("连续语音会话进行中")
            .setSmallIcon(R.drawable.ic_voice)
            .setOngoing(true)
            .setContentIntent(tap)
            .build()
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
        )
    }

    companion object {
        const val ACTION_START = "dev.virtuallife.app.START"
        const val ACTION_STOP = "dev.virtuallife.app.STOP"
        const val ACTION_INTERRUPT = "dev.virtuallife.app.INTERRUPT"
        const val ACTION_TEXT = "dev.virtuallife.app.TEXT"
        const val EXTRA_TEXT = "text"
        private const val CHANNEL_ID = "voice"
        private const val NOTIFICATION_ID = 1
    }
}
