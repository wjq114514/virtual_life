# Kotlin 安卓节点 SDK

这是可由 Android 宿主接入的 Kotlin/JVM 库，输出 JVM 8 字节码，使用 OkHttp 4.12.0、Kotlin serialization、coroutines 和 kotlinx-datetime。它承接节点协议与迁移状态机；悬浮窗、动画、权限入口、前台服务及设备信号由宿主实现。本目录不包含 APK。

## 构建与接入

构建需要 JDK 17+。已固定 Gradle 8.10.2 wrapper 和分发包 SHA256；首次构建需要联网下载依赖。`build-sdk.sh` 默认在此工作站选择 `/usr/lib/jvm/java-21-openjdk-amd64`，也可以用 `ANDROID_NODE_JAVA_HOME` 指定 JDK；缓存默认在 `/tmp/virtual-life-android-node-gradle`，可用 `ANDROID_NODE_GRADLE_CACHE` 覆盖。

```bash
cd clients/android-node
./build-sdk.sh assemble exampleClasses
```

产物为 `build/libs/virtual-life-android-node-0.1.0.jar` 和对应 sources JAR。宿主可将本目录作为 Gradle library project，或运行 `./build-sdk.sh publishToMavenLocal` 后配置 `mavenLocal()`：

```kotlin
dependencies {
    implementation("dev.virtuallife:virtual-life-android-node:0.1.0")
}
```

Android 宿主可直接使用 API 26+；API 21–25 必须使用 AGP 4.0+ 并启用 core library desugaring。虽然 SDK 没有直接调用 `java.time`，但 [kotlinx-datetime 0.6.1 的 JVM 实现依赖 java.time](https://github.com/Kotlin/kotlinx-datetime/blob/v0.6.1/README.md#using-in-your-projects)，因此低版本 Android 仍需 [Android 官方文档中的 library desugaring 配置](https://developer.android.com/studio/write/java8-support#library-desugaring)。SDK 已避免使用 `ConcurrentHashMap.computeIfAbsent` 和 API 24 的 KeySetView。Android 宿主还需自己的 Android SDK、`INTERNET` 权限及网络安全配置；开发时连接本地 HTTP 需要宿主明确允许该开发地址，部署时使用 HTTPS。

本轮在 JDK 21 / Gradle 8.10.2 下执行 `./build-sdk.sh assemble exampleClasses`，库与连接示例已编译成功；未新增或运行 JVM 测试、集成测试或连接 smoke。尚未构建/验证完整 APK，也未验证真实设备、悬浮权限、跨应用行为或前台服务生命周期。

## 宿主接口

实现 `AndroidNodeBridge`：

| 方法 | 宿主责任 |
| --- | --- |
| `capabilities()` | 仅返回当前确实可用的能力，例如 `text.display`、`avatar.render`、`reading.context` |
| `permissions()` | 报告已有授权和前台服务状态；SDK 不发起权限请求 |
| `screenSignals()` | 提供屏幕、宿主可见性、用户主动交互和阅读上下文信号 |
| `prepareRender(snapshot)` | 完成资源准备并等待结果；准备期间不得发出画面、音频或语音输出 |
| `stopOutput(id, reason)` | 等待所有可见输出、音频、语音及相关异步任务停止，再返回；必须幂等并允许重试 |
| `renderState(snapshot, output)` | 更新当前呈现状态，通过输出 handle 校验后才开始输出 |
| `onEvent(event)` | 接收已处理的协议事件；失败会阻止该事件的 cursor 保存 |
| `onFailure(error)` | 可选的诊断回调，不应包含 token |

`renderState` 在状态更新及租约续期后都可能调用，必须幂等。宿主应根据事件/业务消息 ID 去重语音，避免每次续租重复播放。准备渲染时检查权限和生命周期；失去权限后更新可用能力并取消输出。主线程调度由宿主负责，SDK 的 callback 不保证在主线程执行。callback 应使用挂起操作，不要阻塞线程；`stopOutput` 可能在 `renderState` 挂起等待播放时调用，宿主需要能取消该播放。

一个 Android 宿主的连接方式：

```kotlin
val clock = MonotonicClock { android.os.SystemClock.elapsedRealtimeNanos() }
val http = okhttp3.OkHttpClient()
val api = RuntimeApi(runtimeUrl, runtimeToken, clock, http)
val node = AndroidNode(
    NodeSessionConfig(nodeId = stableDeviceId, label = "我的手机"),
    api,
    NodeWebSocketConnector(runtimeUrl, runtimeToken, http),
    hostBridge,
    hostCursorStore,
    clock,
)
val sessionJob = serviceScope.launch(Dispatchers.Default) { node.run() }
// 在停止原生服务前等待 SDK 输出清理：
// sessionJob.cancelAndJoin()
```

所有租约计算必须使用同一个 clock。Android 必须注入 `SystemClock.elapsedRealtimeNanos()`，它包含设备休眠经过的时间；`JvmMonotonicClock` 只用于 JVM 示例/测试环境。`NodeSessionConfig.heartbeatMillis` 应小于运行时配置的节点存活 TTL。宿主在实际开始 UI/音频操作的位置再次校验 handle，尤其是在 `withContext(Dispatchers.Main)` 之后：

```kotlin
override suspend fun renderState(snapshot: CompanionSnapshot, output: OutputHandle) {
    withContext(Dispatchers.Main.immediate) {
        output.tryOutput { renderer.apply(snapshot) }
    }
}
```

handle 绑定 companion 和 epoch。`tryOutput` 内仅执行立即开始输出的短操作，不在其中等待播放结束。宿主可通过 `output.currentPermit()` 获取单调时钟 deadline，并让持续媒体在该期限前结束；SDK 同时有独立 watchdog 主动调用 `stopOutput`。默认每 100ms 检查一次，并从服务端租期预留 250ms 安全余量。宿主仍负责 Android 休眠、调度、前台服务和音频停止的实际时延；不要把一次播放启动检查视为整段语音的永久授权。

## 迁移、租约与恢复

- `transfer.prepared` 必须先用 REST 查询该 transfer 的当前状态和 companion。历史 prepared 事件已经终结时，只同步当前 companion，不执行旧的停播/准备动作。
- 目的节点完成 `prepareRender` 后才发送 `ready`；来源节点完成 `stopOutput` 后才发送 `released`。任一宿主操作失败，均不越过对应事件保存 cursor。
- 目的节点的 prepared 快照没有输出权。commit 后重新查询当前 companion，再通过带时钟采样的 REST 续租获得输出 permit。
- 输出需要同时满足当前 node 所有权、epoch 和单调时钟期限。snapshot/event 中的历史 `lease_expires_at` 从不直接创建 permit。deadline 从 HTTP 请求开始计时，并扣除服务端剩余时间、往返延迟和安全余量。
- epoch/revision 退回或同版本互相矛盾的 snapshot 被忽略；旧 lease 响应不能替换较新的响应。时钟倒退会锁闭 gate，需要创建新的 session/gate。
- 来源在 prepared 期间停播并暂停续租。abort/expire 后同步当前状态，并在重新准备资源与获取新租约后恢复输出。
- 续租失败、租约到期、能力丢失、连接失败及 session 取消时会主动停止已有输出。expiry watchdog 不等待 HTTP/迁移操作的 mutex；租约响应后会重新检查 renderer 是否被 watchdog 清理。

REST 使用 `Authorization: Bearer ...`。WebSocket 的第一个 frame 为 `{type:"auth", token, after_sequence}`，token 不进入 URL。校验 welcome 后按顺序处理 event 与 cursor frame；定时 ping/pong、有限缓冲、退避与抖动重连都由 session 管理。缓冲溢出或事件失败时关闭当前连接，按已保存 cursor 回放。

`CursorStore.save` 必须原子保存且失败时抛异常。SDK 在事件处理成功后才持久化 cursor；扫描 cursor 可以跨过过滤给其他节点的记录，且必须在前面所有事件完成后处理。`FileCursorStore` 提供单进程、单节点会话的重启恢复（先 fsync 临时文件，再在同目录 rename）；更强的断电保障或多个写入者应使用宿主的事务数据库。`InMemoryCursorStore` 仅适合有意不保留进度的场景。

同一 node_id 同时只启动一个 session。持久 cursor 应以稳定 node_id 和运行时实例为键；切换到新的空数据库时清除对应 cursor。heartbeat 仅报告在线状态；用户注意力和跨应用阅读内容必须由宿主显式提供，SDK 不会自动识别其他应用。

## REST 与连接示例

`RuntimeApi` 提供节点、能力图、companion/context、迁移/ack、租约、事件、观察、记忆、人格、人格进化和对话接口。协议类型见 `Protocol.kt`，人格类型见 `Personality.kt`，人格进化类型见 `PersonalityEvolution.kt`，长期慢变量类型见 `SlowVariables.kt`，对话类型见 `Dialogue.kt`，扩展 JSON 对象使用 `JsonObject`。`MemoryRecall` 未显式设置 `tokenBudget`/`maxItems` 时省略这两个字段，沿用中心记忆策略；显式设置时传递整数。命令 ID 由调用方生成并在重试时复用；版本冲突和 ownership/lease 错误以带 `statusCode`、`code` 的 `ApiException` 返回。视觉观察仍要求宿主声明 `vision.capture` 并由用户主动发起。

## 人格、短期状态与预览

人格与短期状态绑定 `companionId`，保存在主服务器，切换设备后继续使用。它们具有独立的 `revision`；未配置时返回 revision 0 与默认值，读取不写入数据库。人格默认值与 [人格合同](../../docs/personality.md) 一致，traits、energy 和 socialBattery 必须为 0..1 的有限数。时间字段保持 UTC ISO 8601 字符串；未写入的时间、来源事件与归档字段为 null。

| `RuntimeApi` 方法 | 请求 / 响应 |
| --- | --- |
| `getPersonality(companionId)` | `PersonalitySnapshot` |
| `updatePersonality(companionId, body)` | `PersonalityUpdate` → `PersonalitySnapshot` |
| `importPersonality(companionId, body)` | `PersonalityCardImport` → `PersonalitySnapshot` |
| `getPersonalityState(companionId)` | `PersonalityStateSnapshot` |
| `updatePersonalityState(companionId, body)` | `PersonalityStateUpdate` → `PersonalityStateSnapshot` |
| `previewPersonality(companionId, body)` | `PersonalityPreview` → `PersonalityPreviewResult` |

编辑和导入使用已有 Bearer token，通过 `expectedRevision` 检查当前版本；冲突返回 HTTP 409。宿主重试时复用完整请求与 `commandId`。已完成命令的重试返回当时保存的回执，可能早于当前版本；处理回执后需要最新记录时再次 GET。状态更新的 TTL 为 30..86400 秒，默认 1800 秒。状态过期后 `state` 回到中性默认值，`recordedState` 保留原记录，`revision` 继续保留。

Android 宿主可在自己的 coroutine 中读取、编辑并审查提示：

```kotlin
val companionId = "sui"
val personality = api.getPersonality(companionId)
val edit = PersonalityUpdate(
    commandId = java.util.UUID.randomUUID().toString(),
    expectedRevision = personality.revision,
    definition = personality.definition.copy(
        speechStyle = personality.definition.speechStyle.copy(
            responseLength = PersonalityResponseLength.BALANCED,
        ),
    ),
)
val saved = api.updatePersonality(companionId, edit)

val state = api.getPersonalityState(companionId)
val savedState = api.updatePersonalityState(
    companionId,
    PersonalityStateUpdate(
        commandId = java.util.UUID.randomUUID().toString(),
        expectedRevision = state.revision,
        state = PersonalityState(mood = PersonalityMood.CURIOUS),
        ttlSeconds = 1800,
    ),
)
val preview = api.previewPersonality(
    companionId,
    PersonalityPreview(userMessage = "想一起读一会儿吗？"),
)
val contextForReview = preview.contextText
```

`PersonalityPreview.history` 接收 `PersonalityHistoryMessage`，role 仅为 `PersonalityHistoryRole.USER` / `ASSISTANT`。预览返回预算内的提示、实际采用的 `memoryRefs`、省略块以及参与建议，`modelCalls` 固定为 0；预览不调用模型、不写共同记忆。`participation.respond` / `allowAudio` 是建议，宿主实际发出 UI、音频或语音时仍须经过当前 `OutputHandle` 与租约 gate 校验。

角色卡导入接收 `PersonalityCardImport.card: JsonObject`，目前支持 Character Card V2 JSON 子集。可用 `NodeJson.parseToJsonElement(cardJson)` 并通过 kotlinx.serialization 的 `jsonObject` 取得对象。完整原卡保存在 `sourceCard`，后续编辑仍保留归档；`importWarnings` 提示名字差异及未启用的内容。卡片中的提示覆盖、lorebook 与 extensions 均不会执行，模板仅替换 `{{char}}` / `{{user}}`，不会更改 companion 名字或身份。

`companion.personality_updated` 和 `companion.personality_state_updated` 通过已有 `AndroidNodeBridge.onEvent` 到达宿主。根据事件外层的 `companionId` 分别调用 `getPersonality` / `getPersonalityState` 并保存最新记录，再返回 callback。事件中只包含 revision 等通知信息，历史事件应触发当前记录的重读。宿主决定何时更新界面与表达状态；SDK 没有自动 UI 或语音行为。

## 人格进化与定义历史

人格进化类型见 `PersonalityEvolution.kt`。稳定人格只能由所有者采纳提议后改变；`refreshPersonalityProposals` 只生成有来源的提议，绝不改写 `PersonalitySnapshot`。补丁 `patch` 是多态 `PersonalityPatchOperation`（`TraitAdjustment` / `TextReplacement` / `StyleReplacement` / `ItemAddition`，JSON 判别字段为 `op`），且永不删除 values、boundaries 或 habits。

| `RuntimeApi` 方法 | 请求 / 响应 |
| --- | --- |
| `getPersonalityHistory(companionId, beforeRevision?, limit)` | `PersonalityHistoryPage` |
| `listPersonalityProposals(companionId, status?, afterSequence, limit)` | `PersonalityEvolutionPage` |
| `getPersonalityProposal(companionId, proposalId)` | `PersonalityEvolutionProposal` |
| `refreshPersonalityProposals(companionId, body)` | `PersonalityEvolutionRefresh` → `PersonalityEvolutionRefreshResult` |
| `acceptPersonalityProposal(companionId, proposalId, body)` | `PersonalityEvolutionDecision` → `PersonalityEvolutionDecisionResult` |
| `rejectPersonalityProposal(companionId, proposalId, body)` | `PersonalityEvolutionRejection` → `PersonalityEvolutionDecisionResult` |

`refresh` 幂等；`modelCalls` 在本地规则路径为 0，模型辅助路径为 1。`useModel=true` 需要服务端 `model_assisted=true` 且配置了 provider 与模型名，否则返回 422；模型输出的越界补丁记为 `EvolutionSkipReason.INVALID_MODEL_PROPOSAL`。`accept`/`reject` 通过 `expectedPersonalityRevision` 与 `expectedProposalRevision` 做 CAS，提议过期、被取代或证据失效返回 HTTP 409。`companion.personality_evolution_proposed` / `_accepted` / `_rejected` 事件同样经 `AndroidNodeBridge.onEvent` 到达。合同、阈值与后续阶段见 [人格进化实施合同](../../docs/personality-evolution-plan.md)。

## 长期慢变量

L2 类型见 `SlowVariables.kt`。`getSlowVariables(companionId)` 返回只读的 `SlowVariableSnapshot`（`valenceMean`、`valenceInertia`、`relationshipWarmth`、`familiarity`、`engagement`、`tone`、`contextVersion`、`modelCalls=0`）。它由服务端在读取时现算，不写库、不调用模型、不改写稳定人格，只在提示与参与中调制回复长度；证据不足时 `engagement` 固定为 0.5 且 `tone` 为 `NEUTRAL`。`SlowVariablePolicy` 描述服务端阈值，客户端只渲染。语义见 [人格](../../docs/personality.md)。

## 单次文本对话

`sendDialogue(companionId, body: DialogueRequest)` 返回 `DialogueResult`，`getDialogueResult(companionId, commandId)` 查询已保存回执。请求必须由用户主动发起（`userInitiated=true`），仅允许文本（`audioRequested=false`）；每次最多调用一次文本生成模型，没有自主对话循环。启用语义记忆时还可能调用一次查询 embedding 模型。模型地址、名称和密钥由主服务器配置，未配置返回 HTTP 503 `model_not_configured`，宿主不持有模型密钥。完整合同及配置见 [WebUI 与对话合同](../../docs/webui.md)。

发送前读取新鲜 companion 快照，使用当前 `nodeId`、`epoch` 和 `expectedRevision`，并确认在线 `text.display` 能力与本地单调租约仍有效。`history` 复用 `PersonalityHistoryMessage`，最多 20 条，每条和 `userMessage` 最多 4096 字符；提示预算 `tokenBudget` 为 1..64000，`memoryTokenBudget` 为 0..64000，`maxMemoryItems` 为 1..100，输出预算 `maxOutputTokens` 为 1..1024（默认 512）。服务端还会检查有界 JSON、共享 token 账本和待提交迁移。

`DialogueStatus.COMPLETED` 可包含 `assistantMessage` 与显式保存记忆所用的 `sourceEventId`；`SILENT` 不调用模型；`DISCARDED` 记录已发生的用量，但不返回助手文字。`usage` 可为 null，时间字段仍为 UTC ISO 8601 字符串。`modelCalls` 与 `usage` 只统计文本生成；可选的 `memoryRetrieval: JsonObject` 单独报告记忆查询的 `model_calls`、缓存命中、用量，或降级及错误代码。`preview` 包含采用的记忆引用、参与建议及当时的版本信息。

所有回执都是历史结果，发送重试和 GET 回执均不授予新的输出权。收到回复后、实际呈现前，宿主必须重新检查当前 owner、epoch、companion revision 与回复的 `preview` 一致，并确认人格及短期状态版本仍适用；同时核验 `preview.entityVersions` 中的实体版本，避免别名变更后呈现过期内容，并经过当前 `OutputHandle` 与本地单调租约 gate。完成的历史回执不能直接触发新的角色输出；历史列表由宿主明确作为历史呈现。SDK 不会自动渲染回复或播放语音。

`commandId` 在所有 companion 间唯一，重试必须复用完整请求；更改请求返回 HTTP 409 `idempotency_conflict`，运行中返回 `dialogue_in_progress`。查询已保存的失败返回对应 `ApiException`，不会自动重发模型调用；网络中断、未知用量或运行中回执也不能视作免费失败或以新 command ID 自动重试。需要显式保存的共同记忆可调用 `writeMemory` 并保留真实来源事件；启用自动记忆时，完成的对话也会排队等待后台整理。

### 流式回复

`streamDialogue(companionId, body, onEvent)` 使用相同请求并返回最终 `DialogueResult`。`DialogueStreamEvent` 包含 Status、Snapshot、Delta、Result 和 Error；Delta 的 `text` 是累计草稿，替换临时回复而不是追加。回调运行在 OkHttp IO 线程，宿主负责调度到 UI 线程，并继续校验 LeaseGate、当前节点、epoch 和上下文；只有最终复核通过的 Result 才进入正式对话与记忆来源。

Status 的 discarded、Error、协程取消或输出权丢失时清除临时文字。协程取消关闭传输，服务器仍会完成用量结算和原命令回执；断流或缺失 Result 后查询 getDialogueResult，不自动重试模型。只实现 complete 的服务端提供商也支持此接口，但只有单段草稿。帧类型与预算见[流式对话协议](../../docs/dialogue-streaming.md)。

## 连续语音会话

SDK 提供纯 JVM 的语音协议类型与传输（`Voice.kt`、`RuntimeApi` 的 `voice*` 方法），不依赖 `android.*`：宿主负责 `AudioRecord` / `AudioTrack`、权限、前台服务与本地 ASR，SDK 只按冻结 schema 联网并保持输出 gate。字段使用显式 `@SerialName`，JSON 为 `snake_case`，未知字段拒绝。合同见 [语音合同](../../docs/voice-contract.md)。

| `RuntimeApi` 方法 | HTTP 接口与响应 |
| --- | --- |
| `createVoiceSession(nodeId, body)` | `POST /v1/nodes/{node_id}/voice/sessions` → `VoiceSessionSnapshot` |
| `getVoiceSession(nodeId, sessionId)` | `GET .../sessions/{session_id}` → `VoiceSessionSnapshot` |
| `heartbeatVoiceSession(nodeId, sessionId, body)` | `POST .../sessions/{session_id}/heartbeat` → `VoiceSessionSnapshot` |
| `stopVoiceSession(nodeId, sessionId, body)` | `POST .../sessions/{session_id}/stop` → `VoiceSessionSnapshot` |
| `submitVoiceTurn(nodeId, sessionId, body)` | `POST .../sessions/{session_id}/turns` → `VoiceTurnReceipt` |
| `getVoiceTurn(nodeId, sessionId, turnId)` | `GET .../turns/{turn_id}` → `VoiceTurnSnapshot` |
| `streamVoiceTurn(nodeId, sessionId, turnId, onEvent)` | `GET .../turns/{turn_id}/events`（SSE）→ `VoiceStreamEvent` |
| `downloadVoicePcm(nodeId, sessionId, turnId, segment)` | `GET .../segments/{sequence}/audio` → `VoicePcmSegment` |
| `interruptVoiceTurn(nodeId, sessionId, body)` | `POST .../sessions/{session_id}/interrupt` → `VoiceInterruptReceipt` |
| `reportVoicePlayback(nodeId, sessionId, body)` | `POST .../sessions/{session_id}/playback` → `VoicePlaybackReceipt` |

`VoiceTurnSubmit.identifier` 为 `VoiceTurnInput` 的 `Asr` 或 `Text` 判别联合：`Asr` 携带识别来源、采样时长与连续性字段，`Text` 不编造录音信息。`end_reason` 为 `ADAPTIVE_ENDPOINT` / `MANUAL` / `PUSH_TO_TALK`。`mode`（`CONTINUOUS` / `PUSH_TO_TALK` / `TEXT`）由 `VoiceSessionCreate` 指定。一次会话每个 companion / 主宿主至多一个，重复创建返回 `voice_session_busy`，须用 `replaceSessionId` 显式替换。

`streamVoiceTurn` 在 `voice_end` 结束；`voice_end` 之前的 EOF 是未完成的订阅（`IOException`），不是重放信号：先停止本地播放，再查询 `getVoiceTurn` 或复用原 `commandId`。`downloadVoicePcm` 校验 `sha256`、采样率、声道与采样数，且只接受 `READY` 段。每个回执与段的 `output_generation` 必须与当前 `VoiceSessionSnapshot` 一致，插话/停止/迁移推进代际后旧段不得播放或下载。播放进度用真实播放头位置上报，不把写进硬件缓冲的整句当作播完。心跳不替代节点心跳、presentation 心跳或租约，也不刷新无人声 idle 计时。

宿主应优先使用耳机全双工，并对 `AcousticEchoCanceler.isAvailable()` / `create()` / enabled 分别检查；不可用时不宣称自动全双工。`AudioRecord` 用固定 mono / PCM16 并核对实际采样率，`AudioTrack` 用 `MODE_STREAM`，停播执行 pause / flush / stop 并清空队列。SDK 不做 UI、不申请权限、不自动播放。

## 自动记忆、摘要与知识图谱

完成的对话会在主服务器排队，后台从用户证据批量提取记忆，并在 SQLite 中维护摘要和知识图谱；禁用自动化时不排队。默认满 4 条或最旧事件等待 60 秒触发，模型调用计入与对话相同的 token 账本。自动提取产生的记忆未经用户确认，宿主应提供审查和纠正入口。

| `RuntimeApi` 方法 | HTTP 接口与响应 |
| --- | --- |
| `memoryAutomation(companionId)` | `GET /v1/companions/{id}/memories/automation` → `JsonObject` 状态 |
| `memoryAutomationJobs(companionId, limit, offset)` | `GET .../memories/automation/jobs` → `JsonObject` 任务页 |
| `runMemoryAutomation(companionId, MemoryAutomationRun(force = true))` | `POST .../memories/automation/run` → `JsonObject` 本批处理结果 |
| `memorySummaries(companionId, limit, offset)` | `GET .../memories/summaries` → `JsonObject` 摘要页 |
| `knowledgeGraph(companionId, query, limit, entityId, hops)` | `GET .../memories/knowledge-graph` → `JsonObject` 图谱 |

`recallMemory` 使用本地记忆召回，不调用模型。`semanticRecallMemory(companionId, MemoryRecall(...))` 显式调用 `POST .../memories/semantic-recall`；查询 embedding 未缓存时可能调用一次模型并消耗共享预算，缓存命中时为零次；`embedding.local=true` 时不占用付费日额度。`RecallResult.retrieval` 保留语义缓存命中、候选数和向量空间等服务端检索信息；可空的 `rerank` 报告服务端可选的显式召回重排是否启用/生效、候选数与耗时，`embeddingUsage` 单独报告查询 embedding 的输入与输出 token 用量。人格预览仍使用本地召回，`modelCalls=0`，不产生 embedding 查询费用，也不重排。

| `RuntimeApi` 方法 | HTTP 接口与响应 |
| --- | --- |
| `memoryEmbeddings(companionId)` | `GET .../memories/embeddings` → `JsonObject` 状态 |
| `memoryEmbeddingJobs(companionId, limit, offset)` | `GET .../memories/embeddings/jobs` → `JsonObject` 任务页 |
| `runMemoryEmbeddings(companionId, MemoryEmbeddingRun(limit = 16))` | `POST .../memories/embeddings/run` → `JsonObject` 本批结果 |
| `semanticRecallMemory(companionId, body)` | `POST .../memories/semantic-recall` → `RecallResult` |
| `updateGraphEntityAliases(companionId, entityId, body)` | `PUT .../memories/knowledge-graph/entities/{entity_id}/aliases` → `GraphEntityAliases` |

`MemoryEmbeddingRun.limit` 为 1..64；省略时使用服务端配置，一次只处理一批。向量构建会调用 embedding 模型，查询状态和任务列表不调用模型。`knowledgeGraph` 可通过 `entityId` 选择起点，`hops` 默认 1、范围 1..3，并继续接受 `query` 与 `limit`，限制遍历范围。`GraphAliasUpdate` 使用 `commandId` 和 `expectedRevision` 显式替换实体的完整别名列表（最多 64 项），空列表清除别名；可携带 `sourceEventIds` 保留来源。重试复用同一完整请求，冲突后重新读取当前实体版本。

手动 `runMemoryAutomation` 默认 `force=true`，可提前整理一批，但仍受共享预算约束。它不会重复派发 running 或 unknown 来源；Linux/POSIX 执行器用文件锁协调，下一次取得锁后将已中断 running 标为 unknown，新 pending 可以继续处理。网络中断后应查询状态和任务列表。自动化接口返回原始 `JsonObject`，宿主按服务端合同读取状态、任务和派生内容。

摘要和图谱关系保留原始记忆来源及版本，源记录纠正、删除或版本变化后不再返回旧派生内容。`recallMemory` 的 `RecallResult.summaries` / `relations` 默认空列表，与原始 `items: List<MemoryRecord>` 一起参与预算内融合召回；`contextText` 和 `estimatedTokens` 包含实际采用的内容。人格预览的 `memoryRefs` / `memoryVersions` 是实际入提示记录及摘要/关系支持记录的去重并集，支持正文不一定全部进入提示。`MemoryRecall.layer` 仍可用于层过滤。删除记忆不等于清除历史对话、整理证据、事件或命令回执，历史回执应作为历史呈现。失效旧批摘要尚不自动重建，详见 [自动记忆合同](../../docs/automatic-memory.md)。

## JVM 连接示例

`src/example/kotlin/dev/virtuallife/node/NodeExample.kt` 是已编译的 JVM 控制台宿主示例。它连接真实运行时，注册节点并创建一个 companion，由 SDK 完成目的 ready、commit 后续租与显示，然后保存阅读上下文和 cursor。它会写入运行时数据，不创建手机界面。先在项目根目录启动服务，再按需运行：

```bash
VIRTUAL_LIFE_URL=http://127.0.0.1:8000/ ./build-sdk.sh nodeExample
```

token 默认采用协议的开发 token，可通过 `VIRTUAL_LIFE_API_TOKEN` 提供。可用 `VIRTUAL_LIFE_NODE_ID` 固定节点标识，`VIRTUAL_LIFE_CURSOR_DIR` 指定 cursor 目录。示例会在结束时等待停播并关闭连接；本轮未运行该示例。

架构分为 `Protocol.kt` / `Personality.kt` / `Dialogue.kt`（wire contract）、`RuntimeApi`/`NodeWebSocketConnector`（transport）、`ReplayEventRouter`（顺序与 cursor）、`MigrationController`（迁移与宿主操作）和 `LeaseGate`（输出授权）。原生实现可参考项目中的 [Operit/AIRI 调研](../../docs/references.md)，本 SDK 为独立实现。

`MemoryWrite.retentionPolicy` 默认为 `MemoryRetentionPolicy.AUTO`，另有 PERMANENT、STABLE、SIGNIFICANT_EPISODE、ROUTINE_EPISODE；更正/元数据更新的同名可选字段省略时继承，不能显式传 null。`MemoryRecord.retentionPolicy` 保留指定策略，`effectiveRetentionPolicy` 为服务端计算策略；召回可包含 `decayFactor` / `effectiveImportance`。固定记录及 auto 下具已知生日语义槽位且有来源证据支持的事实受永久保护；普通经历与稳定认识按配置做有下限的指数显著性衰减，不会自动删除。`MemoryRecall.semanticKey` 支持精确规范槽位查找，仍受来源、层与预算约束。普通召回与预览不强化记忆；成功对话实际使用的相关支持记录每 UTC 日至多强化一次，详见 [记忆设计](../../docs/memory.md)。

### Memory diagnostic logs

`RuntimeApi.memoryLogs(companionId, afterSequence = 0, limit = 100, operation = null, status = null, commandId = null)` reads the authenticated, companion-scoped `/memories/logs` endpoint and returns a `JsonObject`. Inspect `items`, then reuse `next_sequence` with the same filters while `has_more` is true. `limit` is bounded to 1–500; operation/status/command ID filters match exactly. Restart at zero when changing filters. Reading logs does not call a model or reinforce memories. Diagnostics are bounded and redacted; use command receipts for execution state and original provenance for memory sources.

## 情感状态

RuntimeApi 提供 getEmotion、getEmotionExperiences、correctEmotion 和 invalidateEmotion；PersonalityPreviewResult 与 DialogueResult 添加可选 EmotionSnapshot。DialogueResult.acceptedEventId 标识已接受的用户事件。读取最新 state/effectiveState 用于表现；contextVersion 检查回执是否过时，数值可随时间恢复而不增加 revision。情感事件和历史回执不授予输出权。详见[服务端契约](../../docs/emotion.md)。

## 注意力与自触发

`Proactivity.kt` 提供宿主注意力样本、默认关闭的参与策略与持久决策类型。
RuntimeApi 增加 observeAttention、getAttention、getProactivityPolicy、updateProactivityPolicy、
getProactivityDecisions；宿主注册完整能力时加入 attention.observe。SDK 不采集其他应用、
不把静止屏幕当作专注，也不替宿主判断会议或授权。未知信号保持 null/unknown。

同一进程会话内样本 sequence 递增，sampledAt 为带时区的真实采样时刻；重试固定原
commandId 与样本。同一次任务完成、开始休息或用户返回使用固定 boundaryId，其余采样
boundary=none、boundaryId=null。伙伴启用策略后仍须处于 chat 且人格允许主动参与。

通过 onEvent 接收 companion.proactive_completed 后，可用 command_id 调用 getDialogueResult；
acceptedEventId 此时指主动候选接受事件，不代表用户说过话。discardReason 解释被丢弃原因。
回执与重放事件只能作为历史材料，不能直接输出旧问候。宿主须检查当前候选有效期、策略、
注意力、人格/记忆版本与 epoch，并使用原有 OutputHandle/LeaseGate 检查新鲜租约；本地
锁屏或静默时停止显示。后台采集、主动文字/通知与权限生命周期仍由 Android 宿主实现。
完整字段、限额与原子性见[自触发合同](../../docs/proactivity.md)。


`correctEmotion(companionId, eventId, EmotionCorrection(commandId, expectedRevision, kind, ...))`
调整今后对这段经历的理解，原始对话仍保留。`direction` 撤去即时情绪、不翻转方向，保留已发生的回复精力消耗；
`intensity` 用 `retainedWeight`（0..1，省略默认 0.5）缩小当前影响；`attribution` 必须提供
`attribution`（self/other/situation/unspecified），只调整归因解释。请求类型校验适用字段，并保证即使
`NodeJson.encodeDefaults=true` 也不发送不适用的空字段。`invalidateEmotion` 仍完整撤回经历影响，
`corrected` 经历可继续调整或撤回；`invalidated` 和 `forgotten` 不可修改。CAS 冲突时重新读取版本，
重试同一次请求时保留 commandId 和原请求，不把历史成功快照作为当前状态或输出授权。

`EmotionExperience` 可带 `AppraisalVector`、`appraisalVersion`、`uncertainty`、`attribution`、
`originalDelta` 和 `EmotionCorrectionInfo`；缺少新字段的旧回执仍可解码，旧记录不伪造评价向量。
评价向量和不确定性仅解释本地规则，不测量用户心理；归因只是有来源的线索解释。来源遗忘时服务端隐藏解释，
宿主不应缓存后重新展示已隐藏的细节。`EmotionSnapshot.recoveryHalfLives` 可选，给出各维度恢复半衰期；
时间恢复不授予输出权，也不要求增加 revision。当前输出仍须经过节点、epoch、租约及 contextVersion 校验。

## 设备身份与 Web 访问

稳定 `nodeId` 属于实际设备，不是每个页面或每次连接生成的会话 ID。原生 `NodeRegistration`
新增可选 `device: DeviceIdentity(deviceId, macAddresses)`；服务端归一化 MAC 并维护设备身份。
身份已注册为其他节点时返回 `409 device_already_registered`，应复用错误中给出的 canonical nodeId，
不能继续生成新 ID 重复注册。MAC 必须由宿主明确提供；服务端不会读取远端 MAC 或扫描局域网。
现有原生构造和 `AndroidNode` 生命周期不变。

Web 宿主使用独立访问方法：

- `openWebAccess(WebAccessRequest(browserId, accessId, ..., bindNodeId))`：持久保存 browserId，
  每次接入生成新 accessId，同一次 HTTP 重试保留原 ID；采用返回 `snapshot.node.nodeId`。
- `heartbeatWebAccess(nodeId, accessId, WebAccessHeartbeat(...))`：仅在访问仍有效时续期；
  TTL 到期后须重新打开新 accessId，不能用心跳复活旧访问。
- `releaseWebAccess(nodeId, accessId)`：幂等关闭指定 Web 访问，保留设备和原生客户端。

`NodeSnapshot.device` 包含设备 ID、MAC 和服务端从已认证直连传输观测到的 IP；
`accesses` 展示当前有效 Web 访问。新增字段均有默认值，旧节点响应仍可解码。
`WebAccessRequest.kind` 描述实际设备，只能是 desktop/android/other，不能是 web。
`bindNodeId` 用于关联浏览器所在的已有设备，不表示远程控制目标。自动识别按显式绑定、
持久 browserId、近期唯一局域网 IP、创建设备的顺序进行；代理/公网 IP 不作为自动匹配依据。
浏览器 ID 是同源浏览器安装的退路，不是跨浏览器硬件指纹。

同一设备只允许一个活跃 Web 访问，`device_access_busy` 时等待原页释放或过期；
`device_ambiguous` 时明确选择已有设备，不抢占或偷偷复制设备。退出、隐藏、离线或切换伙伴时，
宿主应立即停止本地输出，再尽力释放捕获的旧 nodeId/accessId；释放失败由 TTL 兜底。
Web 对话在 `DialogueRequest.accessId` 发送当前访问 ID，完成显示前也要复核其仍在当前
`NodeSnapshot.accesses` 中、未过期，并保留 epoch、lease、revision 和 contextVersion 校验。
历史回执和旧访问不授予新会话输出权；原生调用可继续省略 accessId。
