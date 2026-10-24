package dev.virtuallife.visiondemo.app

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import kotlinx.coroutines.*

class VisionService : Service() {
    companion object {
        const val START = "dev.virtuallife.visiondemo.START"
        const val SINGLE = "dev.virtuallife.visiondemo.SINGLE"
        const val STOP = "dev.virtuallife.visiondemo.STOP"
        private const val CHANNEL = "screen_observation"
        private const val NOTIFICATION = 1001
    }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var observing = false
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "屏幕视觉观察", NotificationManager.IMPORTANCE_LOW)
        )
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action != START && intent?.action != SINGLE) {
            VisionController.get(this).stop(); stopForeground(STOP_FOREGROUND_REMOVE); stopSelf()
            return START_NOT_STICKY
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, Intent(this, VisionService::class.java).setAction(STOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_view).setContentTitle(if (intent?.action == SINGLE) "3秒后截图分析" else "屏幕视觉正在观察")
            .setContentText("熄屏或锁屏暂停，点击停止结束观察")
            .setContentIntent(open).setOngoing(true)
            .addAction(Notification.Action.Builder(null, "停止", stop).build()).build()
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(NOTIFICATION, notification)
        val controller = VisionController.get(this)
        if (!controller.state.value.loaded || !controller.state.value.connected) {
            stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); return START_NOT_STICKY
        }
        val prompt = intent?.getStringExtra("prompt") ?: MainActivity.DEFAULT_PROMPT
        if (intent?.action == SINGLE) controller.manual(prompt)
        else controller.start(intent?.getLongExtra("interval", 1000) ?: 1000, prompt)
        if (!observing) {
            observing = true
            scope.launch { controller.state.collect { if (!it.running) {
                stopForeground(STOP_FOREGROUND_REMOVE); stopSelf()
            } } }
        }
        return START_NOT_STICKY
    }
    override fun onDestroy() { scope.cancel(); val controller = VisionController.get(this); if (controller.state.value.running) controller.stop(); super.onDestroy() }
}
