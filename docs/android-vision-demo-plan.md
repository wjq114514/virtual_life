# Android 屏幕视觉 Demo 实施合同

> 2026-10-18 方向更新：主客户端的后续适配改为端侧语音识别、取屏上传和服务端视觉理解，见[连续语音与客户端实施规划](realtime-voice-and-client-plan.md)。本文仅保留独立 MNN Demo 的原有合同，不再作为 Android 主客户端的视觉架构。

2026-10-17。主代理规划与整合，执行子代理统一使用 `gpt-6.1-sol`、`low`。
目标设备为红魔 10S Pro；首版独立于节点运行时，直接加载 MNN 视觉模型。
用户后续确认尚未部署模型，因此选择并准备官方 Qwen3-VL-2B-Instruct-MNN 4bit 文件。

## 交付范围

- 新项目 `clients/android-vision-demo/`，包名 `dev.virtuallife.visiondemo`，Android 11+
  （API 30），arm64，独立 debug APK；不修改现有 Kotlin/JVM SDK 和服务端合同。
- Shizuku 权限、独立 UserService、受限取屏接口、死亡/撤权处理。原型通过固定的
  `/system/bin/screencap -p` 获取 PNG，使用文件描述符传输，避免 Binder 大对象。
  这是便于核验的低频原型路径；连续 Surface 采集是后续优化，不宣称 scrcpy 级性能。
- 手动截图分析，以及用户显式开始/停止的前台持续观察；锁屏/熄屏暂停；变化检测、
  最短调用间隔、最长刷新间隔、单路推理，旧会话回调不得覆盖新会话。
- 图片在应用私有缓存中传给真实 MNN-LLM JNI，使用视觉模型本身的图像输入，
  不用文字模型或伪造分析结果代替。直接加载应用可读目录中的 config.json，
  提供系统目录选择器复制已有模型文件的入口。
- 流式展示识别文字；JSONL 调试日志、logcat、导出日志、截图预览和明确错误。
  日志包含单调时钟阶段耗时，不记录截图内容、用户提示或生成正文。
- 未来 MCP/skill 使用独立视觉服务接口；本版不实现完整 MCP、摄像头和自动操作。

## 分工及跨模块 API

三个执行子代理独占不同文件集合，不得覆盖其他人的修改。

### 采集与日志模块

目录：`app/src/main/{java/dev/virtuallife/visiondemo/{capture,debug},aidl}/`。

```kotlin
package dev.virtuallife.visiondemo.debug
class DebugLog(context: Context) {
    fun event(name: String, fields: Map<String, Any?> = emptyMap())
    fun tail(): String
    fun exportFile(): File
}

package dev.virtuallife.visiondemo.capture
data class CapturedScreen(
    val bytes: ByteArray,
    val sampledAtElapsedNs: Long,
    val captureMs: Double,
)
class ShizukuCaptureClient(context: Context, log: DebugLog) : Closeable {
    val isConnected: Boolean
    fun requestPermission(activity: Activity)
    suspend fun connect()
    suspend fun capture(): CapturedScreen
    override fun close()
}
data class PolicyDecision(val accepted: Boolean, val reason: String, val changeScore: Double)
class CapturePolicy(
    minIntervalMs: Long = 2500,
    maxIntervalMs: Long = 15000,
    changeThreshold: Double = 0.04,
) {
    fun evaluate(bitmap: Bitmap, nowMs: Long, force: Boolean = false): PolicyDecision
    fun reset()
}
```

`evaluate` 被接受时提交该帧为比较基线；如果推理忙，控制器不得调用它消耗机会。
接收时间与耗时使用 `SystemClock.elapsedRealtimeNanos()`。

### 本地 MNN 模块

目录：`app/src/main/{java/dev/virtuallife/visiondemo/inference,cpp}/` 及
`scripts/prepare-mnn.sh`。MNN 固定 `3.6.1`；头文件和实现必须来自相同版本。

```kotlin
package dev.virtuallife.visiondemo.inference
data class InferenceResult(
    val text: String,
    val firstTokenMs: Double?,
    val totalMs: Double,
    val visionMs: Double?,
    val prefillMs: Double?,
    val decodeMs: Double?,
)
class MnnVisionEngine(context: Context, log: DebugLog) : Closeable {
    val isLoaded: Boolean
    fun load(configPath: String, backend: String = "cpu", threads: Int = 4)
    fun analyze(imagePath: String, prompt: String, maxTokens: Int = 128,
                shouldContinue: () -> Boolean = { true },
                onDelta: (String) -> Unit): InferenceResult
    fun cancel()
    override fun close()
}
class ModelStore(context: Context, log: DebugLog) {
    fun defaultModelDirectory(): File
    fun importTree(uri: Uri): File // returns imported config.json
    fun resolveConfigPath(input: String): File
}
```

模型加载/推理由控制器串行调用；`cancel` 可跨线程发出原子取消请求。
JNI 对异常、UTF-8 分段、回调异常和 native 生命周期做边界处理。
`shouldContinue` 是跨 native 调用边界仍然有效的会话谓词；控制器传入会话仍有效、
job 仍活跃且屏幕可用的联合条件。JNI 在重置取消标记后、图片处理与 response 前、
逐 token 和输出回调阶段检查谓词，防止停止请求在推理入口被覆盖；谓词异常按回调异常回传。
只提供真实后端，模型缺失或非视觉模型应明确失败。

### 应用与调度模块

目录：`app/src/main/{java/dev/virtuallife/visiondemo/app,res}/`、
`app/src/main/AndroidManifest.xml`。

Android 原生 Activity + foreground service；组件包名使用 `.app`。
所有跨模块接口严格按本文；若必须调整，先通知主代理及另一模块负责人。
加载、导入、抓图与 native 推理不阻塞主线程；停止不阻塞主线程等待 native。
通知提供停止按钮；前台服务声明 `specialUse` 和对应权限/说明。
调用期间不能卸载 native 引擎；停止、重新开始及销毁必须防止旧结果覆盖。

## 构建与验证

主代理负责 Gradle 项目、JDK/Android SDK/NDK 准备、整合、构建和最终文档。
Gradle wrapper 复用仓库已固定版本，编译 SDK 35，AGP 8.7.3，Kotlin 2.0.21。
检查包括 debug APK 完整 native 构建、Android lint、关键筛帧/时效逻辑测试，
以及 APK 内 arm64 native 库检查。没有真机连接时不宣称已验证 Shizuku、模型
实际输出或红魔设备首字性能；记录可直接执行的真机验收步骤。
