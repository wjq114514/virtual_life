# Android 屏幕视觉 Demo

这是保留的本地视觉历史原型。主客户端迁移到 [android-app](../android-app/README.md)：手机负责取屏和本地语音识别，服务端负责视觉理解与 TTS。新主客户端不需要本 Demo 的 MNN 视觉权重；两者安装与能力边界分别记录，见 [连续语音规划](../../docs/realtime-voice-and-client-plan.md)。

独立 debug APK，面向 Android 11+、arm64（目标设备：红魔 10S Pro）。使用 Shizuku
UserService 取屏，在手机上直接加载 MNN 视觉模型，展示流式结果并导出阶段耗时日志。
本版不需要服务端；不接入伙伴节点、MCP、skill、摄像头或自动点击。

实现分工使用 `gpt-6.1-sol`、`low` 子代理，实施合同见
[规划](../../docs/android-vision-demo-plan.md)。

## 推荐模型

选择官方 [Qwen3-VL-2B-Instruct-MNN](https://huggingface.co/taobao-mnn/Qwen3-VL-2B-Instruct-MNN)，
语言权重为 4bit，包含图像编码器。所需文件约 1.47 GB，下载清单固定在
[recommended.json](models/recommended.json)，模型 revision 为
`9e49ec71ded22500a997ed0f9961e1e92b85bbc9`。下载时逐文件核验 LFS SHA-256
或 Git blob SHA-1；模型文件不打进 APK。

```bash
python3 clients/android-vision-demo/scripts/download-model.py
```

默认保存到仓库 `.data/android-vision-demo/models/Qwen3-VL-2B-Instruct-MNN/`。
支持中断后重新运行和 `--output DIRECTORY`，只检查文件用 `--verify-only`。
下载来自 Hugging Face，网络失败可重试；不需要账号或 API token。

保留完整运行文件：`config.json`、`llm_config.json`、`tokenizer.txt`、`llm.mnn`、
`llm.mnn.weight`、`visual.mnn`、`visual.mnn.weight`。该模型使用 tied embeddings，
`config.json` 里的 `embeddings_int4.bin` 不单独提供，embedding 来自语言权重，
不能据此判定包不完整。其他模型可通过目录导入，但须是 MNN-LLM 支持的视觉模型，
`llm_config.json` 中须有 `is_visual=true`；GGUF、ONNX、原始 safetensors 不能直接加载。

## 构建

需要 JDK 17+、Android SDK Platform 35、Build Tools 35.0.0、NDK 27.2.12479018、
CMake 3.22.1。可以用 Android Studio SDK Manager 安装，或在已有 command-line tools
环境下执行：

```bash
sdkmanager 'platform-tools' 'platforms;android-35' 'build-tools;35.0.0' \
  'ndk;27.2.12479018' 'cmake;3.22.1'
ANDROID_VISION_SDK=/path/to/Android/Sdk \
ANDROID_VISION_JAVA_HOME=/path/to/jdk \
  clients/android-vision-demo/build-demo.sh
```

脚本首次下载并校验固定 MNN 3.6.1 源码到本项目 `.deps/MNN/`，然后构建真实
MNN-LLM、图像编解码、OpenCL 与 JNI；不会下载模型。Gradle 8.10.2 wrapper 固定
分发包摘要。默认任务是 `assembleDebug`、`testDebugUnitTest`、`lintDebug`，也可以
在脚本后指定任务。Android Studio 可直接打开本目录。

产物：`app/build/outputs/apk/debug/app-debug.apk`。
APK 保留 Android 调试能力，native 使用 `RelWithDebInfo` 优化编译，避免以未优化
构建的耗时代替模型性能。

检查 APK 中的 arm64 native 库及 ELF/ZIP 16 KiB 对齐：

```bash
python3 clients/android-vision-demo/scripts/check-apk.py \
  clients/android-vision-demo/app/build/outputs/apk/debug/app-debug.apk
```

## 安装和模型部署

手机先安装并启动 [Shizuku](https://shizuku.rikka.app/guide/setup/)，允许本 demo
使用它。非 root 可在 Android 11+ 使用无线调试启动；设备重启后需重新启动 Shizuku。
root 启动也可使用同一套 demo 接口。

通过 ADB 安装 APK 并推送已下载、已校验的推荐模型：

```bash
python3 clients/android-vision-demo/scripts/deploy.py \
  --model .data/android-vision-demo/models/Qwen3-VL-2B-Instruct-MNN
```

仅安装 APK 可省略 `--model`；连接多个设备时加 `--serial SERIAL`。
脚本不会卸载已有应用或清除数据。默认模型路径为：

```text
/sdcard/Android/data/dev.virtuallife.visiondemo/files/models/Qwen3-VL-2B-Instruct-MNN/config.json
```

也可以手动安装 APK，将完整模型目录放进普通共享目录，然后在 demo 中点击
“选择模型目录并复制导入”。系统文件选择器不允许访问部分 `Android/data` 目录，
因此选择普通下载目录或使用上面的 ADB 部署方式。SAF 导入会复制模型，需要额外空间。
其他应用私有目录中的模型不能直接作为路径加载。

本工作区已准备 `.data/android-vision-demo/Qwen3-VL-2B-Instruct-MNN.zip`，约 1.47 GB，
旁边的 `.sha256` 文件可校验完整压缩包。若在手机上操作，先解压到下载目录，
再选择解压后的模型目录导入；demo 的目录选择器不直接加载 ZIP。

## 操作

1. 申请 Shizuku 授权，再点击连接/刷新，确认状态为已授权且已连接。
2. 确认 `config.json` 路径，先选 CPU、4 线程，点击显式加载模型。
3. 点击“3秒后截图分析”，demo 返回桌面；在倒计时内切到目标应用。
   完成后从通知或桌面返回 demo 看截图、流式结果与阶段耗时。
4. 持续模式点击“持续开始并返回桌面”，随后切换目标应用。
   通知中的停止按钮与应用内停止按钮都可以结束观察。
5. CPU 路径确认正确后，可卸载模型、切换 OpenCL、重新加载，比较日志中的真实耗时。

OpenCL 依赖手机驱动和模型算子支持，构建包含后端不代表整张图一定在 GPU 执行。
MNN 可以对部分算子回退 CPU；本 demo 不宣称使用了 NPU。

## 采样和调度策略

- 默认每 1000 ms 发起一次采样，界面允许 250～60000 ms。实际周期还包含取屏耗时。
- 最短模型调用间隔 2500 ms，最长刷新间隔 15000 ms；相对上次接受帧做低分辨率
  亮度变化比较，阈值为 0.04。长刷新间隔在模型空闲且持续有有效样本时起作用。
- 单路模型推理，忙时帧队列只保留最新一帧；推理前再次获取最新帧，取屏请求起点
  已超过 3000 ms 的帧被丢弃。拒绝帧不会更新变化基线或消耗刷新期限。
- 熄屏或锁屏暂停，不推断此时可打断用户。均匀近黑/近白是空白帧启发式，
  不代表已经可靠识别安全窗口。
- 输入等比缩小至最长边 960；日志保留原始和实际输入尺寸。小字识别不足时，
  后续可增加区域裁剪和输入尺寸对比。
- 停止递增会话标识、取消取屏与推理，旧会话回调不能覆盖新结果。
  native 取消在阶段边界/逐 token 生效，正在执行的单个视觉编码或 prefill
  计算不保证立即中断；新任务会等待原生调用退出。

取屏使用固定 `/system/bin/screencap -p`，通过 Shizuku shell/root 身份执行，
只允许固定截图接口。PNG 经文件描述符传输，不进入 Binder 大消息。
这是便于调试的低频原型；每次 PNG 编码和命令进程启动的成本会进入日志。
后续可在相同接口下替换为连续 Surface 采集。

## Debug 日志与首字定义

界面可以导出 JSONL。电脑端查看相同事件：

```bash
adb logcat -v threadtime -s VLVisionDemo:I
```

文件位于应用缓存的 `vision-debug/`，滚动日志最多 4×256 KiB，另有一份导出快照。
日志包含会话、帧号、事件、单调时间及阶段元数据，不记录截图、用户提示和生成正文。
缩图临时留在应用私有缓存以便预览，停止或被下一帧替换后清理；模型目录长期保留。

主要事件：

| 事件 | 含义 |
| --- | --- |
| `capture_start` / `capture_end` / `capture_complete` | 取屏请求和 PNG 接收耗时 |
| `policy` | 是否分析、拒绝原因 |
| `frame_discarded` | 帧过期或屏幕不可用 |
| `inference_start` | 输入尺寸、预处理、队列等待 |
| `first_delta` | 首个可显示字符回调及取屏起点到回调耗时，跳过空白和零宽字符 |
| `inference_complete` | native 首个可显示字符、视觉编码、prefill、decode、总耗时 |
| `inference_end` | 控制器完成结果发布 |
| `error` / `capture_error` | 错误类型；具体错误在界面显示 |

`sampledAtElapsedNs` 是取屏请求开始，不是像素实际变化时刻。
`first_delta.capture_to_first_ms` 包含取屏、队列、缩图和推理等待；
native 的 `first_visible_ms` 从 JNI 推理调用开始，跳过空白并处理 UTF-8 分段。
二者含义不同。帧采样之前的等待、文字到 UI 的实际绘制时间，需要单独测量。
首次模型加载和 GPU 预热也单独计算，不能与热启动 TTFT 混在一起。

## 真机验收

- Shizuku 未启动、拒绝授权、授权后连接、运行中停止 Shizuku，错误均应清楚显示。
- 单次倒计时切换应用后，缩图和生成结果应对应目标屏幕。
- 静态页面应大量出现 `unchanged`，变化且满足间隔时出现分析；忙时不形成历史帧积压。
- 停止/立即重启、旋转界面、通知停止、锁屏/解锁，旧帧不得覆盖新会话。
- 卸载/重载模型，切换 CPU/OpenCL，缺文件或误选文字模型应明确失败。
- 用同一页面预热后收集至少 20 次结果，分别统计 capture-to-first 和 native
  first-visible 的中位数/P95；屏幕出现到首字需额外包含实际采样等待。

没有连接真机时，编译、单元测试、lint、模型完整性和 APK 检查不能代替上述验收，
也不能给出红魔 10S Pro 的实测首字延迟。

2026-10-17 工作区验证已完成：完整 APK 构建、7 项 JVM 测试、3 组 host native
测试（UTF-8、视觉权重范围、跨 JNI 取消）、APK 签名、ELF/ZIP 16 KiB 对齐，
以及脚本 Ruff/语法检查均通过。Android lint 为 0 错误、14 条警告，主要涉及中文
界面国际化、单一 ABI、依赖更新提示等；尚未连接真机，Shizuku 取屏、实际模型
输出、OpenCL 驱动兼容性和首字耗时仍待上机验收。

## 实现依据

- [Shizuku API / UserService](https://github.com/RikkaApps/Shizuku-API)
- [MNN 3.6.1 LLM 接口](https://github.com/alibaba/MNN/blob/3.6.1/transformers/llm/engine/include/llm/llm.hpp)
- [MNN 3.6.1 多模态处理](https://github.com/alibaba/MNN/blob/3.6.1/transformers/llm/engine/src/omni.cpp)
- [MNN Android 示例](https://github.com/alibaba/MNN/tree/3.6.1/apps/Android/MnnLlmChat)
