# 连续语音实施与验证报告

日期：2026-10-19。对应 [执行规划](realtime-voice-and-client-plan.md)。状态：后端、Linux 宿主（`vl voice` CLI、TUI 语音控件、轮次状态机、文档与回放工具）、Android SDK（类型、传输与 README）与 Android 主宿主（`clients/android-app/`，APK 构建通过）均已完成；真实音频硬件、真机行为与真模型质量留待目标端验证，本文如实记录已执行与未执行项。

本次沿用已有部分实现，执行代理按用户要求使用 `gpt-6-luna`、`reasoning_effort=max`。改动只针对开发工作区；后端演练使用临时 SQLite 与独立节点，不连接已有运行服务，不修改已有伙伴数据。语音模型是显式下载的本地资源，不调用付费云端语音服务。

## 架构与实现位置

| 工作 | 位置 | 行为 |
| --- | --- | --- |
| 服务端会话、轮次与输出代际 | `backend/virtual_life/voice_service.py`、`voice_storage.py` | 持久幂等、会话期限、排队与收尾、取消、播放进度、重启恢复 |
| HTTP、SSE 与严格 DTO | `voice_api.py`、`voice_models.py`、`voice_stream.py` | 反序列化前 24 KiB 限额、有界事件重放、游标缺口、私有音频下载 |
| 原对话流程复用 | `dialogue.py`、`api.py` | 内部语音来源、预算结算、上下文/租约 gate、普通文字仲裁 |
| 服务端 TTS | `speech_provider.py`、`speech_worker.py`、`speech_text.py` | 分句流水线、独立运行环境、PCM、可取消合成与限额 |
| Linux 主宿主 | `clients/linux-node/` | daemon 持有采音/识别/播放，`vl voice` CLI 与 TUI 语音控件经原 IPC 操作 |
| Android SDK 与主宿主 | `clients/android-node/`、`clients/android-app/` | JVM 协议 SDK 与 Android 权限/音频/取屏宿主分层；取屏用 MediaProjection，视觉仍在服务端 |
| 隔离复现 | `examples/voice_live_test.py`、`examples/voice_replay.py` | 前者: HTTP / SSE / 幂等 / 音频下载 / 撤销闭环；后者: 虚拟时钟驱动的轮次端点与插话回放（无模型、无音频） |

麦克风音频只在客户端参与 ASR、VAD 和轮次判断；服务端只接收 final 文本及有限元信息。截图由客户端获取和上传，服务端现有视觉 worker 理解；新主宿主不依赖旧 Android Demo 的 MNN 视觉模型。

## 服务端关键行为

1. 一个会话具有 heartbeat、idle 和绝对期限。显式开始后接受连续轮次，过期、停止、迁移、锁屏或 presentation 失效后不恢复旧会话。普通文字输入停止语音会话；语音窗口内抑制后台主动输出。
2. `command_id` 与原运行时/对话共享命令命名空间。重复提交返回稳定原回执，接受后由 worker 执行一次，不因网络结果不明换 ID 重试。
3. `output_generation` 在接受新轮、插话、停止时推进。旧 queued 任务不派发，旧合成结果发布前重新校验，音频下载在返回头部前及每块读取前重新校验。已下载字节无法远程撤回，因此本地停播与本地代际校验仍然必需。
4. 一个伙伴允许一个有效模型调用和一个被撤销调用收尾。反复插话占满收尾槽时返回 `voice_busy_draining`，不会无限创建新请求。已发生用量继续结算；已派发但没有证据的用量保持未知。
5. 已完成的原 `DialogueResult` 不改写为 discarded。语音可以撤销未播音频，而历史文字回执保持不可变。播放报告记录事实，不授予恢复输出的权限。
6. LLM 回调只更新有界累计文本 mailbox，TTS 在线程中并行执行。默认最多 4 个待合成段，未消费音频达到约 8 秒后暂停派发；连续约 2 秒没有腾出空间则停止后续音频、保留文字并报告 `speech_backpressure`。每个段派发前都检查背压，不能一次切出多段后绕过限制。
7. TTS 使用累计草稿的非重叠范围，未朗读尾部可修订，已锁定前缀被改写则取消。没有稳定边界的短草稿等待，不进行零进度循环。英文句号、小数和分句有确定性测试。
8. 下一轮上下文保留用户文本；音频模式下只把确认完整播放的段作为已听到回复，未确认部分不冒充已经传达。仅文字模式可复用完整回复。
9. screen reference 必须属于同设备、同伙伴、同 display/session generation。待处理的 fresh 请求不会拖住普通对话，也不把旧分析伪装成当前画面。实际纳入的内容标为不可信观察；证据 TTL、purge 或屏障变化会停止依赖它的输出。
10. 重启使活跃会话和缓存音频失效；未知已派发任务不自动再发。音频存储只使用 turn ID 的摘要作为私有目录，文件不挂载到静态站点。

## 可复现的后端检查

在仓库根目录执行：

```bash
uv run pytest -q
uv run ruff check backend examples
uv run python examples/export_openapi.py
uv run python examples/voice_live_test.py
```

集成过程已完成的检查：

| 检查 | 实际结果（本轮实测） |
| --- | --- |
| 语音 API / 配置 / DTO 目标测试 | 103 passed（`test_voice_models.py`、`test_voice_configuration.py`、`test_voice_service.py`、`test_voice_api.py`）；包括真实 HTTP GET events、snapshot/replay/gap、JSON 实际字节限额与音频分块 |
| 服务编排目标测试 | 已覆盖旧任务收尾与新轮并行、反复插话上限、迟到 TTS、音频中途撤销、重启、背压、screen purge/TTL/pending、ASR 来源与迟到播放报告 |
| TTS 分句与规范化 | 11 passed；修复旧 partial 实现中短无标点文本的无限循环 |
| 后端全量回归 | `uv run pytest -q` → 876 passed（耗时约 417 秒，单条 Starlette `httpx` 弃用告警） |
| 隔离 HTTP/SSE 演练 | `examples/voice_live_test.py` → completed；1 段 fake 静音 PCM、4800 bytes；幂等重放相同，中断后 GET 返回 410，`sse_voice_end=true` |
| 轮次状态机回放 | `examples/voice_replay.py` → 3 次提交（含续说合并与两次插话续接），最终 `awaiting_reply`；`--json` 输出稳定 |
| Linux 宿主测试 | `clients/linux-node` → 87 passed（65 + `test_voice_turns.py` 17 + `test_voice_controller.py` 5） |
| Android SDK 构建与测试 | `clients/android-node/build-sdk.sh assemble exampleClasses test` → BUILD SUCCESSFUL |
| Android 主宿主 APK | `clients/android-app/build-app.sh`（`:app:assembleDebug`，JDK 21 + Android SDK 34）→ BUILD SUCCESSFUL，arm64-v8a APK 约 41 MB，含 sherpa 四个 `.so` |
| Android 帧过滤单元测试 | `:app:testDebugUnitTest`（`ScreenFrameFilterTest`）→ 7 tests、0 failures；`:app:lintDebug` → BUILD SUCCESSFUL |
| Ruff | 后端与 examples 全部通过；`clients/linux-node`（src/tests）在本轮修复 4 处未用导入/变量后全部通过 |
| OpenAPI | 已重新导出 84 条路径（`docs/openapi.json`） |

演练首段 SHA-256 为 `24ddaa4710480313757f965c38d60208a334556cb244f830d5006a893edd8da7`。它只证明传输/栅栏，不证明语音音质、识别准确率、声学插话或端到端延迟。

## 真实 TTS 与模型资源

模型路径、清单校验和 worker 解释器由 `[speech]` 配置（`model_dir` / `model_manifest` / `worker_python`）描述，安装与路径解析见 [配置说明](configuration.md) 与 [示例 TOML](../virtual-life.example.toml)。服务端启动不会自动下载权重；获取与校验模型是显式的运维步骤，缺少模型时 TTS 降级为文字。

真实模型下载/加载、两句合成耗时和音频证据：待 TTS 资源验证汇总。不要把上一节 fake PCM 的结果用于宣称真实 Kokoro 可用。

真实 TTS 在隔离数据库中的传输演练：

```bash
uv run python examples/voice_live_test.py --tts-config /absolute/path/to/tts-test.toml
```

此模式仅复制 TTS 配置，数据库、令牌、LLM、视觉与自动记忆仍使用隔离设置；输出不会播放到扬声器。TTS 生成音频也不等于麦克风连续对话验收。

## Linux 验证

CLI（`vl voice doctor|devices|start|stop|status|commit|interrupt`，支持 `continuous`/`push_to_talk`/`text` 三种模式）、设备检测、`[voice]` 配置、ASR 资源准备和安装说明见 [Linux README](../clients/linux-node/README.md)（本轮新增语音章节）。本轮在开发主机执行的 `clients/linux-node` 测试为 87 passed、`ruff check src tests` 全部通过；轮次端点与插话逻辑另有 `examples/voice_replay.py` 做无模型回放。真实模型加载与录音回放需要本地音频资源，未在本轮执行。

TUI 已加入语音控件：`ctrl+o` 连续开关、`ctrl+p` 按住说、`ctrl+g` 提交轮次、`ctrl+k` 打断；状态行显示 `voice=` 状态、播放进度、AEC 结论与本地转写草稿，活跃会话内输入行改走 voice turns API（同一条文本不会两个入口重复提交），`voice.*` 事件驱动界面更新。TUI 尚无自动化测试（沿用仓库现状），也未在真实终端会话中人力走查。

尚未完成：`clients/linux-node/packaging/build.py` / `install.py` 暂未加入语音相关的可选依赖与资源声明。

当前开发主机没有可用于验证的 PipeWire 采音/播放工具与音频硬件环境。因此声卡实际采音、扬声器停止时间、耳机切换和 AEC 效果须在目标桌面会话验证，不用 mock 结果代替。

## Android 验证

SDK 在 JDK 上构建为 JVM8，不依赖 Android 平台类。本轮在 JDK 21 下执行 `clients/android-node/build-sdk.sh assemble exampleClasses test` → BUILD SUCCESSFUL，包含 MockWebServer endpoint 与 PCM 校验测试；[SDK README](../clients/android-node/README.md) 已新增「连续语音会话」章节（`voice*` 方法表、判别联合输入、代际栅栏、EOF 语义、宿主音频职责与 AEC 检查）。

新主宿主 `clients/android-app/` 本轮已创建。安装 Android SDK（Build-Tools 34.0.0、Platform 34、Platform-Tools）、以 JDK 21 与 Gradle 8.10.2 执行 `clients/android-app/build-app.sh`（即 `:app:assembleDebug`）→ **BUILD SUCCESSFUL**，产出 `app/build/outputs/apk/debug/app-debug.apk`（arm64-v8a，约 41 MB）。APK 内已确认打包 `libonnxruntime.so`、`libsherpa-onnx-c-api.so`、`libsherpa-onnx-cxx-api.so`、`libsherpa-onnx-jni.so`（sherpa-onnx 1.13.8 AAR）。实现覆盖：应用 `android` kind presentation、伙伴接管、`voice/sessions` 创建、端侧 sherpa 流式识别、VAD/插话能量检测、`AudioTrack` 流式播放与播放上报、打断、会话与 presentation 心跳、前台服务与麦克风权限流程。SDK 通过 composite build 复用（与 Android Kotlin 插件类路径隔离）。

服务端视觉取屏已接入主宿主：以 `MediaProjection`（`capture_source=android_media_projection`）经 `ScreenService`（`foregroundServiceType=mediaProjection`）取屏，`ScreenFrameFilter`（8×8 平均哈希 + 最小间隔 + 关键帧间隔，纯逻辑、7 项单元测试）筛帧，再走服务端 screen API 的「注册上传 → PUT 二进制 → 提交帧 → 查询状态/分析」闭环；屏幕 API 仅接受管理令牌，故宿主使用独立的 `admin_token`。视觉仍在服务端推理，宿主不引入 MNN。Shizuku 取屏**未实现**，本版实现的是 MediaProjection 路径（规划中的 P1/P2 替代路径），未将其冒充为 Shizuku 结果。

尚未执行：真机安装与录音/回放、真实 ASR 准确率、AEC、耳机路由、音频焦点、锁屏/后台、真机取屏与旋转、screen API 真机往返、`zipalign -P 16`（Build-Tools 34 无此选项，需 35 或真机检查）。这些项目以真机与运行中的服务为前提，本轮未执行。

取屏授权、AudioRecord / AudioTrack、耳机路由、电话/通知音频焦点、锁屏与后台前台服务行为必须单独进行真机验证；构建成功不表示这些项目通过。旧本地视觉 APK 保留为历史 Demo，其 README 已增加迁移导航。

## 发布与后续验收边界

本轮不部署或重启现有服务，不安装 APK 到用户手机，不改变已有模型账户或真实伙伴数据。默认语音与 TTS 开关均关闭。

目标端需按客户端 README 配好模型、设备与音频权限后显式开始连续对话。声学验收至少记录：安静环境与耳机的真实对话；停顿续说/改口；回复中插话；网络中断；输出权迁移；锁屏/权限丢失；5 分钟持续对话的 CPU/RSS、识别实时系数、最后人声到首音时间与停止到静音时间。未执行这些测量前，不宣称达到规划的 p50/p95 延迟或真实全双工 AEC 指标。
