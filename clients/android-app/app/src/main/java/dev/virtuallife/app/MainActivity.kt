package dev.virtuallife.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.projection.MediaProjectionManager
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private lateinit var statusView: TextView
    private lateinit var draftView: TextView
    private lateinit var replyView: TextView
    private lateinit var modelView: TextView
    private lateinit var screenStatusView: TextView
    private lateinit var screenSummaryView: TextView

    private val requestProjection =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val data = result.data
            if (result.resultCode == Activity.RESULT_OK && data != null) {
                val intent = Intent(this, ScreenService::class.java)
                    .setAction(ScreenService.ACTION_START)
                    .putExtra(ScreenService.EXTRA_RESULT_CODE, result.resultCode)
                    .putExtra(ScreenService.EXTRA_RESULT_DATA, data)
                startForegroundService(intent)
            } else {
                screenStatusView.text = "屏幕共享: 未授权"
            }
        }

    private val requestPermissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
            if (granted[Manifest.permission.RECORD_AUDIO] == true) {
                startVoice()
            } else {
                statusView.text = "未授予麦克风权限，无法开启连续语音"
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())

        val engine = VoiceCore.ensure(this)
        lifecycleScope.launch {
            engine.status.collect { statusView.text = it }
        }
        lifecycleScope.launch {
            engine.draft.collect { draftView.text = if (it.isBlank()) "—" else "识别: $it" }
        }
        lifecycleScope.launch {
            engine.reply.collect { replyView.text = if (it.isBlank()) "—" else it }
        }
        refreshModelState()

        val screen = ScreenCore.ensure(this)
        lifecycleScope.launch {
            screen.status.collect { screenStatusView.text = "屏幕: $it" }
        }
        lifecycleScope.launch {
            screen.summary.collect { screenSummaryView.text = if (it.isBlank()) "—" else it }
        }
    }

    private fun requestScreenSharing() {
        val manager = getSystemService(MediaProjectionManager::class.java)
        if (manager == null) {
            screenStatusView.text = "屏幕: 系统不支持"
            return
        }
        requestProjection.launch(manager.createScreenCaptureIntent())
    }

    private fun buildUi(): ViewGroup {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 60, 40, 40)
        }

        statusView = TextView(this).apply {
            textSize = 18f
            setTypeface(typeface, Typeface.BOLD)
            text = "空闲"
        }
        draftView = TextView(this).apply { text = "—"; setTextColor(Color.DKGRAY) }
        replyView = TextView(this).apply { text = "—"; textSize = 16f }

        val scroll = ScrollView(this).apply {
            addView(replyView)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f,
            )
        }

        val input = EditText(this).apply {
            hint = "文字提交 / 朗读"
        }
        val actions = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }

        fun button(label: String, onClick: () -> Unit): Button =
            Button(this).apply {
                text = label
                setOnClickListener { onClick() }
            }

        actions.addView(button("启动") { requestPermissionsAndStart() })
        actions.addView(button("停止") { send(VoiceService.ACTION_STOP) })
        actions.addView(button("打断") { send(VoiceService.ACTION_INTERRUPT) })
        actions.addView(button("发送文字") {
            val text = input.text.toString()
            if (text.isNotBlank()) {
                input.text.clear()
                send(VoiceService.ACTION_TEXT, text)
            }
        })
        actions.addView(button("导入模型") { importModel() })

        modelView = TextView(this).apply { textSize = 14f; setTextColor(Color.GRAY) }

        val screenActions = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        screenActions.addView(button("共享屏幕") { requestScreenSharing() })
        screenActions.addView(button("停止共享") {
            startService(Intent(this, ScreenService::class.java).setAction(ScreenService.ACTION_STOP))
        })
        screenStatusView = TextView(this).apply { text = "屏幕: 未共享"; textSize = 14f }
        screenSummaryView = TextView(this).apply {
            text = "—"
            textSize = 14f
            setTextColor(Color.DKGRAY)
        }

        root.addView(statusView)
        root.addView(draftView)
        root.addView(scroll)
        root.addView(input)
        root.addView(actions)
        root.addView(screenActions)
        root.addView(screenStatusView)
        root.addView(screenSummaryView)
        root.addView(modelView)
        return root
    }

    private fun requestPermissionsAndStart() {
        val needed = buildList {
            if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED
            ) {
                add(Manifest.permission.RECORD_AUDIO)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                ContextCompat.checkSelfPermission(
                    this@MainActivity,
                    Manifest.permission.POST_NOTIFICATIONS,
                ) != PackageManager.PERMISSION_GRANTED
            ) {
                add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        if (needed.isEmpty()) {
            startVoice()
        } else {
            requestPermissions.launch(needed.toTypedArray())
        }
    }

    private fun startVoice() {
        if (ModelRepository.resolve(this) == null) {
            statusView.text = "缺少本地 ASR 模型，请先用 adb push 后点击导入模型"
            return
        }
        ModelRepository.adoptExternal(this)
        refreshModelState()
        startForegroundService(Intent(this, VoiceService::class.java).setAction(VoiceService.ACTION_START))
    }

    private fun send(action: String, text: String? = null) {
        val intent = Intent(this, VoiceService::class.java).setAction(action)
        if (text != null) intent.putExtra(VoiceService.EXTRA_TEXT, text)
        startService(intent)
    }

    private fun importModel() {
        val copied = ModelRepository.adoptExternal(this)
        refreshModelState()
        if (copied == 0 && ModelRepository.resolve(this) == null) {
            statusView.text = "未找到可导入的模型文件"
        }
    }

    private fun refreshModelState() {
        val dir = ModelRepository.resolve(this)
        modelView.text = if (dir == null) {
            "模型: 未安装 (期望目录 ${ModelRepository.externalDir(this).absolutePath})"
        } else {
            "模型: 已就绪 (${dir.absolutePath})"
        }
    }
}
