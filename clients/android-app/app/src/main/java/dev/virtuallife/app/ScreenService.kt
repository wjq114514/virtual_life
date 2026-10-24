package dev.virtuallife.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/** Holds the single screen controller so the UI and the service share one session. */
object ScreenCore {
    @Volatile
    var controller: ServerScreenController? = null
        private set

    fun ensure(context: Context): ServerScreenController = controller ?: synchronized(this) {
        controller ?: build(context).also { controller = it }
    }

    private fun build(context: Context): ServerScreenController {
        val appContext = context.applicationContext
        val androidId = Settings.Secure.getString(
            appContext.contentResolver,
            Settings.Secure.ANDROID_ID,
        ) ?: "unknown"
        val config = AppConfigStore(appContext).load(
            defaultNodeId = "android-$androidId",
            defaultCompanionId = "android-companion",
        )
        return ServerScreenController(appContext, config, OkHttpClient())
    }
}

/**
 * MediaProjection foreground service. The host obtains user consent in the
 * activity and passes the result here; the service owns the projection lifetime
 * and stops sharing whenever the system revokes it.
 */
class ScreenService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    private var projection: MediaProjection? = null

    private val controller: ServerScreenController
        get() = ScreenCore.ensure(this)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                promoteToForeground()
                startProjection(intent)
            }

            ACTION_STOP -> scope.launch {
                controller.stop()
                releaseProjection()
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        scope.launch { controller.stop() }
        releaseProjection()
        super.onDestroy()
    }

    private fun startProjection(intent: Intent) {
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
        @Suppress("DEPRECATION")
        val resultData = intent.getParcelableExtra(EXTRA_RESULT_DATA) as? Intent
        if (resultData == null) {
            controller.status.value = "缺少屏幕捕获授权"
            stopSelf()
            return
        }
        val manager = getSystemService(MediaProjectionManager::class.java) ?: run {
            controller.status.value = "系统不支持屏幕捕获"
            stopSelf()
            return
        }
        val mediaProjection = manager.getMediaProjection(resultCode, resultData) ?: run {
            controller.status.value = "屏幕捕获授权失败"
            stopSelf()
            return
        }
        mediaProjection.registerCallback(
            object : MediaProjection.Callback() {
                override fun onStop() {
                    scope.launch {
                        controller.stop()
                        releaseProjection()
                        stopSelf()
                    }
                }
            },
            Handler(Looper.getMainLooper()),
        )
        projection = mediaProjection
        controller.start(mediaProjection)
    }

    private fun releaseProjection() {
        projection?.stop()
        projection = null
    }

    private fun promoteToForeground() {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    getString(R.string.notification_channel_screen),
                    NotificationManager.IMPORTANCE_LOW,
                ),
            )
        }
        val tap = PendingIntent.getActivity(
            this,
            1,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title_screen))
            .setContentText("正在共享屏幕给服务端视觉")
            .setSmallIcon(R.drawable.ic_voice)
            .setOngoing(true)
            .setContentIntent(tap)
            .build()
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION,
        )
    }

    companion object {
        const val ACTION_START = "dev.virtuallife.app.SCREEN_START"
        const val ACTION_STOP = "dev.virtuallife.app.SCREEN_STOP"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        private const val CHANNEL_ID = "screen"
        private const val NOTIFICATION_ID = 2
    }
}
