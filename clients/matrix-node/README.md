# Matrix 节点桥

把 Matrix（经任一 homeserver）接入 Virtual Life。Matrix 房间消息映射为对话轮次，用户在场/输入映射为 attention 样本（`user_present` + `activity=chat`），伙伴回复通过 Matrix `m.room.message` 回发。

参考 [AstrBot matrix adapter](https://github.com/stevessr/astrbot_plugin_matrix_adapter) 但**只实现 stdlib-only 子集**：密码/Token 认证、sync 长轮询、`m.text` 消息提取、`m.room.message` 发送、加入房间。**默认不实现** E2EE、SSO/OAuth2/QR、媒体、贴纸、线程、Reaction（如需要，请参考 AstrBot 完整实现）。

## E2EE（可选）

`e2ee: true` 时启用端到端加密支持。此时运行一个 Node 边车进程（`e2ee/sidecar.js`），由 `matrix-js-sdk`（含 RustCrypto WASM）负责 Olm/Megolm 密钥交换与解密/加密；Python 侧只做 stdio 桥接。边车监听 `RoomTimeline`（事件入 timeline 时）与 `MatrixEventEvent.Decrypted`（megolm key 稍后经 to-device 到达、解密成功时）两类信号，解密后以明文 `message` 事件上报，运行时与普通 `m.room.message` 走同一对话路径；发送经 `send` 命令交由边车加密投递。

**边车生命周期约束（重要）**：Node 无原生 IndexedDB，且 wasm-bindgen 的 IndexedDB 绑定无法接受 `fake-indexeddb` 补丁（`Simple value parse error: Dynamic cast failed`），所以 RustCrypto 的 crypto store 是**内存态**。因此边车必须作为**单个长生命周期进程**运行——Python `matrix-node` 启动时 spawn 一次、会话期间常驻。若用**固定 `deviceId`** 反复重启边车，会每次重新生成 one-time keys 并撞上已上传的同签名 OTK（服务器 400 `One time key ... already exists`）；用**密码认证**每次登录获得新 `device_id`（本客户端默认行为）则无此冲突。

**交叉签名（cross-signing）**：每次密码登录都会得到**全新未交叉签名**的设备，接收方会报"未经其所有者验证的设备加密"。边车启动时默认调用 `crypto.bootstrapCrossSigning({setupNewCrossSigning:true, authUploadDeviceSigningKeys})`，用账号密码通过 UIA（`makeRequest(null)`→401+session→带 `m.login.password` 重试）重置交叉签名身份并**对本次登录设备自签名**，使发送设备 owner-verified。要点：
- **只有"发送"边车应启用**（默认）。接收方边车须设 `bootstrapCrossSigning: false`，否则其自身重置会使发送方的身份失效。
- 因每次重置都创建新 master 身份，**只有最近一次 bootstrap 的设备保持 owner 签名**（更早的设备被已废弃的 self-signing key 签名）。多设备稳定身份需持久化 crypto store（生产建议：IndexedDB 或 Node 兼容 store）+ 从 4S 恢复密钥。
- 发送前需确认房间已加载 `m.room.encryption`（`isRoomEncrypted`），否则会退化为明文 `m.room.message`。边车 `send` 命令内置 `waitForEncryption`（15s 超时），未确认加密则拒绝明文发送并报错。

**交叉验证（cross-verification / SAS）**：bootstrap 只解决"本次登录设备自签名"。要让**新设备**成为"已验证设备"（`device.verified`，被 owner 交叉签名），需要另一台**受信任设备**（已持有交叉签名身份）与它做 SAS 验证。边车默认启用该流程：
- 新设备边车设 `verification: "auto"`（或 `autoVerify: true`）——启动时调用 `crypto.requestOwnUserVerification()` 向本账号其它设备发起验证请求。
- 受信任设备边车（持有交叉签名身份，`bootstrapCrossSigning: false`）**始终监听** `CryptoEvent.VerificationRequestReceived` 并**自动 accept**，然后驱动 SAS。
- 双方边车都会把 SAS emoji/decimal（7 个 emoji + 3 个十进制数）作为 `verification` 事件（`state: "sas"`）经 stdout 上报，Python 侧 `verification_state()` 暴露、运行时打日志提示用户**在另一受信任设备上对照并确认**。
- 通过 `verification_confirm(transaction_id)` / `verification_mismatch(...)` / `verification_cancel(...)` 或边车命令 `{cmd:"verification", action:"confirm"|"mismatch"|"cancel", transaction_id}` 完成；**必须双方都 confirm** 才会 DONE。
- 也可由受信任设备主动驱动：`{cmd:"verification", action:"request_device", user_id, device_id}`（`crypto.requestDeviceVerification`）。**不要两端同时 `auto`**——同一 pair 的并发请求会被 Rust SDK 取消（`m.user`）；一驱一听。
- 验证完成后新设备被 self-signing 签名（owner 已验证），即可正常收发加密消息。相关配置键：`verification`（`"auto"`/`false`）、`autoVerify`（别名）、`bootstrapCrossSigning`。

配置：

- `e2ee`: `true` / `false`（默认 `false`）。
- `e2ee_sidecar`: 边车目录路径（默认 `客户端目录/e2ee`，需先 `cd e2ee && npm install`）。
- `e2ee_config`: 传给边车的对象，键为 `baseUrl`/`userId`/`accessToken`/`deviceId`/`syncTimeoutMs`/`bootstrapCrossSigning`/`verification`/`autoVerify`；缺省时从 `homeserver`/`user_id`/`access_token` 自动补。**密码认证时无需 `accessToken`**——边车自行 `m.login.password` 登录（每次获得新设备并自动处理 `deviceId` 绑定）；`accessToken` 存在则直接用 token（需与 `deviceId` 一致，否则 `/keys/upload` 报 400 `device_id ... does not match`）。`bootstrapCrossSigning`（默认 `true`）：发送边车应启用，接收边车应设为 `false`。`verification`（默认启用）：`"auto"` 使新设备自动请求验证，`false` 只监听；`autoVerify` 为 `verification` 的别名。

```json
{
  "e2ee": true,
  "e2ee_sidecar": "clients/matrix-node/e2ee",
  "e2ee_config": { "deviceId": "VLMX" }
}
```

边车依赖：Node ≥ 18 + `npm install`（`matrix-js-sdk`）。若边车无法启动（如 Node 缺失），客户端以错误码 3 退出。

运行（stdlib-only，Python 3.12+）：

```bash
VIRTUAL_LIFE_API_TOKEN=local-dev-token \
python3 matrix_node.py --config example.json
```

配置字段见 `example.json`。要点：

- `node_id` 是注册到运行时的房间节点；它会成为伙伴的 `active_node_id`（当伙伴跟随到 Matrix 房间时）。
- `homeserver` 是 Matrix 服务器基址；`user_id` 是机器人账号（`@bot:server`）；`auth_method` 可选 `password`（需 `password`）或 `token`（需 `access_token`）。
- `rooms` 是 `[{room, companion}]` 绑定列表：每个 Matrix 房间 ID（`!id:server` 或别名）映射到伙伴 ID。
- `maintain_node`（可选，默认 `true`）：本客户端是否负责节点的注册与心跳。三角色拆分后置为 `false`，节点在线由 `node-keep` 维持，本客户端只收发消息。
- `screen_observer_node`（可空）是屏幕观察节点 ID（如 Android 采集节点）。设置后，对话请求会附带 `screen_reference: {node_id: screen_observer_node}`，把该观察节点的最新帧注入本轮对话（屏幕视觉注入）；不设置时对话不携带引用，按旧行为使用对话节点自身的帧（需该节点具备 `vision.capture`）。
- `sync_timeout_ms` 是 sync 长轮询超时（默认 30000）；`interval_seconds` 是心跳间隔。
- `lease_renew_seconds`（默认 `5`）后台线程为本节点持有的 companion 续租的间隔。模型调用可达 30s 以上而服务端 companion lease TTL 可能很短（如 10s），没有续租时慢对话会在提交阶段被 409 lease_expired 拒绝；该线程只续 `active_node_id` 等于本节点的 companion，续租失败仅记 debug 日志。
- **发送节奏**（可选，均为客户端本地配置）：`send_interval_min_seconds`（默认 `0.8`）条间最小间隔；`send_interval_jitter_seconds`（默认 `1.2`）条间 0..jitter 均匀随机抖动；`send_max_retries`（默认 `3`）发送失败重试次数；`retry_backoff_initial_seconds`（默认 `2`）指数退避初值（2s、4s、8s…）；`merge_on_limit`（默认 `true`）退避耗尽后把同一房间的剩余段落合并为一条再发一次；`max_messages_per_reply`（默认 `3`，与服务端 `dialogue.reply_split_max_messages` 对齐）客户端侧拆条上限。服务端 `reply_messages` 已拆条时逐条发送，未拆条时退回整条单发。
- **typing**：`typing_before_send`（默认 `true`）在每条消息发送前先 `PUT /rooms/{id}/typing/{user}` 置输入状态；`typing_min_seconds`（默认 `0.5`）最短输入时长，`typing_chars_per_second`（默认 `12`）按长度估计输入时长 `max(typing_min, len/chars_per_second)`。typing 失败只记日志，不阻塞发送。
- **打字节奏（服务端 reply_delays）**：`use_reply_delays`（默认 `true`）消费服务端 `DialogueResult.reply_delays`（毫秒数组，见 docs/protocol.md），每条消息按该延迟发出，typing 状态全程覆盖延迟窗口；无该字段或字段关闭时退回 `send_interval_min_seconds`+jitter 本地节奏。
- **并发与即时性**：`handle_workers`（默认 `4`）收信处理线程池大小——收信后立刻派发到线程池触发对话全流程，慢回复不再阻塞 sync 长轮询与其他房间；同房间按到达顺序串行，不同房间并行。回复发送由独立 sender 线程处理：入队即发，不等下一次 sync 轮次。
- **输入观察**：`observe_typing`（默认 `true`）监听 m.typing 事件，用户开始输入即上报注意力（user_present+typing）；`typing_attention_min_interval`（默认 `8` 秒）同一房间两次上报的最小间隔，避免刷屏。

Matrix 事件映射：

| Matrix 事件 | Virtual Life 动作 |
| --- | --- |
| `m.room.message`（`m.text`，非本账号发送） | `POST /v1/companions/{id}/dialogue` |
| 用户发送消息 | `POST /v1/nodes/{node}/attention`（user_present=true, activity=chat） |
| 心跳 | `POST /v1/nodes/{node}/attention`（user_present=true） |
| 伙伴回复 | `PUT /_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}` |

依赖：仅标准库。`matrix_node/json_utils.py`、`matrix_node/api.py` 与 system-node/qq-node 风格一致（严格 JSON、字节/嵌套上限、无重定向）；`matrix_node/matrix.py` 用 `urllib.request` 实现 Matrix 客户端（登录、sync、发送、加入）；`matrix_node/runtime.py` 做事件→对话映射与发送队列；`matrix_node/e2ee_bridge.py` 桥接 Node 边车（E2EE 可选）。

## 测试

运行时事件映射与发送队列用标准库 `unittest` 覆盖（无真实 homeserver、无网络）；Matrix 客户端请求构建与响应解析用本地回环 opener 覆盖；E2EE 边车有真实 `node` 子进程冒烟测试（验证 ready/事件处理，无真实 homeserver）：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p "test_*.py"
```
