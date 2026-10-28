# Virtual Life 聊天安卓客户端

纯文字聊天客户端。它复用本仓库的 `clients/android-node` JVM SDK（composite build，不重复实现协议层），
按 [`GPT-FIG`](https://github.com/DDxfy/GPT-FIG)（MIT）的 Compose 单屏聊天形态实现 UI，对接本项目后端。

- 产物：`app/build/outputs/apk/debug/app-debug.apk`
- 包名：`dev.virtuallife.chat`
- 最低版本：Android 11（API 30），targetSdk 34

## 构建

需要 JDK 17+ 与 Android SDK（compileSdk 34）。脚本会复用 `android-node` 的 Gradle 缓存：

```sh
cd clients/android-chat
./build-chat.sh                 # :app:assembleDebug + :app:testDebugUnitTest
./build-chat.sh :app:assembleDebug
```

环境变量（可选）：`ANDROID_CHAT_JAVA_HOME`、`ANDROID_HOME`、`ANDROID_CHAT_GRADLE_CACHE`。
没有设备也可以完成编译和单测：本机无 `adb` 设备时 `:app:assembleDebug` 仍是完整校验。

## 首次运行

启动后右上角齿轮进入设置：

| 项 | 说明 |
| --- | --- |
| 服务地址 | 以 `/` 结尾。模拟器里访问宿主机用 `http://10.0.2.2:8038/`；真机换成局域网 IP |
| Bearer token | 与后端 `[server]` 的 token 一致，开发环境默认 `local-dev-token` |
| 节点 ID | 默认 `android-<ANDROID_ID>`，同一设备重启后仍是同一个节点 |
| 伙伴 ID | 默认 `android-chat`；不存在时客户端会自动创建同名伙伴 |
| 你的名字 | 作为 `user_name` 随对话请求发出，默认 `你` |
| 最大输出 tokens | 1–1024，默认 512 |

保存后客户端重新注册节点、重新申请输出权。连接状态显示在标题栏下方。

## 会话与历史

- 本地会话保存在 `files/chat-state.json`（会话列表 + 消息 + 延迟/用量），应用重启后仍在；文件损坏时降级为一个空会话而不是崩溃。
- 右上角刷新把服务端 `GET /v1/companions/{id}/dialogue/history` 的历史**追加**到当前会话（按 `command_id` 去重，游标翻页，最多 20 页）；`Add` 图标或会话列表里的「重置」用服务端历史**替换**本地内容。
- 发送时只把**已提交**消息送进 `history` 窗口：最多 20 条、每条裁剪到 4096 字符，占位中（PENDING）和失败（FAILED）的消息不会回喂给模型。

## 协议流程

客户端**自己驱动会话**（不调用 `AndroidNode`），但输出仍走 SDK 的 `LeaseGate`：

```
registerNode(text.display) → 节点心跳 15s
openPresentation            → 展示心跳 5s（TTL 15s）
createCompanion (409 忽略)
prepareTransfer
→ acknowledgeTransfer(READY, presentation_id)   ← 必需，见下
→ 轮询 getCompanion 直到 active_node_id == 本机
renewLease      → LeaseGate.grant(sample)   每 3s 补一次（租约 TTL 10s）
```

**transfer 必须 ack。** `POST .../transfers` 只是 `prepared`：服务端只有在目标节点用
`phase=ready` 确认后才置 `target_ready`，而提交要求 `target_ready` 为真。未 ack 的 transfer
会一直挂着，并且服务端规定「有 pending transfer 时拒绝 dialogue」。顺带说一句，
`clients/android-app` 的 `VoiceEngine.claimCompanion` 只 prepare 不 ack，它只有在伙伴已经属于
本机时才走得通——这是本仓库待修的隐患，本客户端没有沿用。

`ready` ack 同时绑定本机那个活跃的 presentation：服务端会校验它仍然存活，所以「已经打开
渲染窗口」这件事在所有权转移时被服务端确认过，而不是客户端自报。

发一句对话：重新 `getCompanion` 取最新 `epoch`/`revision` → 生成新的 `command_id` →
`POST /v1/companions/{id}/dialogue`。后端要求节点在线、持有有效租约、revision 匹配、能力包含
`text.display`，所以租约轮询是必需环节，不是优化。

回复展示前再过一次门：

```kotlin
if (result.preview.activeNodeId != config.nodeId) 拒绝
else gate.tryOutput(companionId, preview.epoch) { 渲染 }
```

即：**只有仍持有该 epoch 活跃租约的本机才能呈现这条回复**。租约失效时这条回复被标记为
「输出租约已失效，本次回复未展示」，不会渲染。

## 流式与降级

- `GET /v1/capabilities` 的 `dialogue.streaming` 为真时走 `streamDialogue`。注意这个字段嵌在
  `dialogue` 对象里，顶层没有 `streaming`；读错位置会永久退回 JSON 路径。
- SSE 是标准格式：种类在 `event:` 行，JSON 在 `data:` 行。`Delta.value.text` 是整段替换而不是
  追加，UI 因此显示累计文本。
- 传输中断不等于回合失败：命令一旦被接受就是持久的。`streamDialogue` 抛出 `IOException` 时改用
  `getDialogueResult(commandId)` 读回已存结果，最多重试 3 次；**绝不重发模型调用**，否则同一句
  话会被生成两遍。`ApiException` 不恢复——那说明服务端明确拒绝了这次请求。
- `SILENT`（模型选择不说话）显示「（这次选择了安静）」；`DISCARDED` 显示 `discard_reason`。两者都不经过输出门，因为它们不携带回复正文。
- 「停止」只取消本地等待；服务端命令仍然持久化。此时用「拉取服务端历史」可以让界面与真实状态对齐。

## 测试

```sh
cd clients/android-chat && ./build-chat.sh :app:testDebugUnitTest
cd clients/android-node  && ./build-sdk.sh test
```

- `ChatRequestsTest`：历史窗口 20 条上限、4096 字符裁剪、PENDING/FAILED/空白跳过、`toNodeRole` 映射、请求字段的 epoch/revision 透传、四类预算 clamp 到协议区间。
- `ChatStorageTest`：往返保真、缺失/损坏/空文件降级、未知字段忽略、`clear()`。
- `DialogueRuntimeApiTest`（SDK 侧）：`dialogueHistory` 的路径、游标拼装、Bearer 头、分页解码、越界参数拒绝。

## 端到端验证

本机没有真机和模拟器，所以除了编译和单测之外，用一个一次性后端实例（临时配置、临时 SQLite、
端口 8199、复用本机 llama.cpp 模型）跑过一遍客户端实际执行的完整协议序列，34 项断言全绿：

- 节点注册 → `online` 且带 `text.display` 能力；展示窗口 open + heartbeat
- 伙伴创建（重复创建返回 409，客户端吞掉）
- `prepareTransfer` → **`acknowledgeTransfer(READY, presentation_id)`** → 轮询确认接管
- 提交即签发租约；`renewLease` 返回 `expires_at` / `ttl_seconds` 并保持 `epoch`
- 对话：`status=completed`、回复非空、`preview.active_node_id` 与请求 `node_id` 一致、
  `preview.epoch` 与请求一致、`usage` 有输入与输出 token
- 幂等重放：同一 `command_id` 再次提交返回完全相同的文本，没有二次调用模型
- `GET /v1/companions/{id}/dialogue/{command_id}` 能读回同一结果（传输中断的恢复路径）
- SSE：`content-type: text/event-stream`，事件序为 `status, snapshot, status, delta×13, result`，
  delta `sequence` 连续，且每个 delta 都是整段替换
- 历史：新→旧排序、字段名与 SDK 解码一致、`before_sequence` 游标翻出的页面严格更旧
- 输出门：过期 `epoch` 被服务端拒绝为 `epoch_conflict`；非持有节点同样被拒
- 关闭时 `DELETE` 展示窗口

这一步抓到了两个只靠编译发现不了的问题：`claimCompanion` 漏了 ready ack（transfer 永远停在
`prepared`，对话会被 `transfer_in_progress` 拒绝），以及 streaming 开关读错了 JSON 路径。

## 已知限制

- 没有 WebSocket 事件流：不消费 `POST /v1/nodes/{id}/events`，因此「主动发起」的对话不会推送过来，只能靠用户发消息或手动拉历史。SDK 已提供 `AndroidNode` 完整会话（事件回放 + 迁移处理），接入方式是后续工作。
- 没有推送、前台服务与后台续租；应用被杀死后需要重新打开。
- 不支持语音、记忆/人格可视化、附件、多语言切换；这是纯文字第一版。
- UI 为浅色单主题，未做 `LocalContentStorage` 之外的加密存储（token 存在普通 SharedPreferences，与 `clients/android-app` 一致）。

## 第三方署名

UI 形态与组件结构参考 [DDxfy/GPT-FIG](https://github.com/DDxfy/GPT-FIG)（MIT，版权 2026 DDxfy），
完整许可证见 [THIRD_PARTY.md](THIRD_PARTY.md)。协议层代码未从该项目复制，全部来自本仓库 SDK。
