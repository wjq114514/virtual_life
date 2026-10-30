# 节点状态维护调查（Node State Maintenance Survey）

> 范围：所有客户端（clients/）与服务器（backend/virtual_life/）对"节点状态"的维护职责。
> 依据：2026-10-30 对源码的逐行核对（coordinator.py / models.py / storage.py / node_keep.py / adb_keep.py / AndroidNode.kt / system_node / linux-node / qq-node / matrix-node）。
> 触发：用户 m03256"先梳理一下各客户端和服务器的节点的所有状态维护吧"。

---

## 1. 服务器侧节点模型（权威定义）

### 1.1 节点 = 房间（`backend/virtual_life/coordinator.py`）

| 字段 | 来源 | 说明 |
|---|---|---|
| `node_id` | `nodes` 表 PK | 节点唯一标识（`Identifier`） |
| `label` | `nodes.label` | 显示名（`DisplayName`） |
| `kind` | `nodes.kind` | `Literal["desktop","android","web","other"]` |
| `capabilities` | `nodes.capabilities_json` | `list[Identifier]`，max 64，注册时 unique+sorted |
| `context` | `nodes.context_json` | 心跳携带的自由 JSON（`bounded_json`） |
| `created_at` / `last_seen_at` | `nodes.*` | 时间戳 |
| `online` | **派生**：`now < last_seen_at + node_ttl_seconds` | `node_ttl_seconds = 60`（coordinator.py:98/126） |
| `room_state` | **派生**：`"occupied" if active_node_id == this else "empty"` | 节点快照只暴露 `online`/`room_state`，不存库 |
| `device` | `devices` 表（`devices.project`） | 仅当注册时带 `DeviceIdentity` |

**关键：节点自身没有 epoch/revision/lease。** 这些属于**同伴（companion）**。节点上的 `room_state` 只是"该同伴当前活跃节点是否等于我"的投影。

### 1.2 注册（`register_node`，coordinator.py:344-377）

- 请求体 `NodeRegister`（models.py:80）：`{node_id, label, kind, capabilities(≤64, unique+sorted), device: DeviceIdentity|None}`。
- `devices.check_registration` 校验设备身份。
- 行为：`INSERT ... ON CONFLICT(node_id) DO UPDATE SET label, kind, capabilities_json, last_seen_at`（**冲突时不动 context_json**）。
- 发出 `node.registered` 事件；随后 `_reconcile`。

### 1.3 心跳（`heartbeat`，coordinator.py:439-466）

- 请求体 `NodeHeartbeat`（models.py:93）：`{context: JsonObject}`（`bounded_json`）。
- 要求节点已存在（`_row`），否则 404。
- 多写者仲裁：`_accept_device_state`（session_id + 单调 sequence 游标，`device_state_session_id/sequence/at` 列）。接受 → `UPDATE nodes SET context_json=?, last_seen_at=?`；拒绝 → `UPDATE nodes SET last_seen_at=?`（context 不变）。
- `record_native_ip`；返回节点快照。

**这是 `last_seen_at` 唯一被刷新的入口** —— `online` 的"维持"完全依赖它。

### 1.4 观察（`observe`，coordinator.py:995-1004）

- 需要节点具备 `vision.capture` 能力，否则 `409 capability_required`。

### 1.5 输出能力门（coordinator.py:31/42/594）

- `OUTPUT_CAPABILITIES = {"avatar.render", "text.display", "audio.output"}`。
- 该集合决定：一个节点能否成为迁移目标（line 42）、能否作为输出汇（line 594）。

### 1.6 同伴流（全部在 `companions` 表，coordinator.py）

| 操作 | 方法 | 行为 |
|---|---|---|
| 续租 | `renew_lease` (826-850) | `UPDATE companions SET lease_expires_at=?`；校验 node+epoch |
| 跟随 | `follow_room` (864-917) | 单方权威迁移：bump epoch+revision，发 fresh lease，发布 `companion.room_followed` |
| 转移准备 | `prepare_transfer` (672) | 创建 Transfer 记录（两阶段） |
| 转移确认 | `acknowledge` | 两阶段 ack（released from source, ready to target） |
| 租约到期 | gate (572) | 输出条件之一，未续租即拒绝 |

---

## 2. 客户端侧节点状态维护（逐客户端）

### 2.1 node-keep —— 统一"每节点状态维持"守护进程（**当前主力**）

`clients/node-keep/node_keep.py`（纯 stdlib 3.12+，镜像 adb-supervisor/system-node 约定）：

| 方法 | 行 | 职责 |
|---|---|---|
| `register()` | 381 | POST /v1/nodes（409 容忍为刷新） |
| `heartbeat()` | 401 | POST /v1/nodes/{id}/heartbeat {context} |
| `get_companion()` | 417 | GET /v1/companions/{id} |
| `renew_lease()` | 427 | POST /v1/companions/{id}/lease {node_id, epoch} |
| `follow()` | 438 | POST /v1/companions/{id}/follow |
| `prepare_transfer()` | 454 | POST /v1/companions/{id}/transfers |
| `acknowledge()` | 470 | POST /v1/transfers/{id}/ack |
| `flow_cycle()` | 482 | 转移>跟随>续租；`idle/lease_renewed/lease_failed/followed/transfer_prepared/transfer_failed` |

- 配置：节点字段（server_url/node_id/label/kind/capabilities/context/interval_seconds/timeout_seconds/token_env）+ 可选 `companion` 块（companion_id/lease_renew_seconds/flow_interval_seconds/follow_to/.../transfer_to/.../ack_released/ack_ready）。
- `auth_rejected` → exit 2；传输失败计数，3 次后重新注册；401/403 放弃。
- 主循环：心跳按 `interval_seconds` 节奏；流按 `companion.flow_interval_seconds` 节奏；sleep = min（有 companion 时）。
- **角色**：每个节点一个守护进程；既维持节点在线，又（可选）维持同伴租约/驱动流转。这就是"2 角色拆分"里的节点状态维持角色。

### 2.2 adb-supervisor —— 设备状态观察者（`clients/adb-supervisor/adb_keep.py`）

- 注册**观察者**节点（kind=other，caps `["device.state.observe"]`，line 213-214）+ 心跳该节点。
- 在 adb 状态转移时 POST `device_state` 观察（notify 路径 line 333-381）。
- **这是"节点状态上报"，不是采集源** —— 它把"adb 设备在线/离线"映射为节点观察，不产生屏幕帧。

### 2.3 SDK AndroidNode —— 参考客户端状态机（`clients/android-node/.../AndroidNode.kt`）

- `heartbeatMillis=15_000`（29）、`leasePollMillis=100`（30）。
- `register()`（198-204）：caps 来自 `bridge.capabilities().sorted()`；能力变化时重新注册（218-221）。
- `heartbeat()`（213-222）：同样检查能力变化并重新注册；然后发心跳。
- 租约续期协程（366）：`renewLease(LeaseRenew(nodeId, snapshot.epoch))`。
- **角色**：SDK 层的"参考实现"，被 android-app（ServerScreenController）与 linux-node 守护进程使用。

### 2.4 system-node —— 纯 stdlib 严格约定（`clients/system-node/system_node/runtime.py`）

- 能力构建于 `run()`（155-164）：`["device.state.report"]` + 若 notify 加 `["device.state.observe"]` + 若启用加 `["attention.observe"]`。
- 注册节点 + 心跳。严格配置（≤65536B、≤8 深度、拒绝未知字段/重复键），systemd Type=notify。

### 2.5 linux-node 守护进程（`clients/linux-node/src/virtual_life_linux/daemon.py`）

- `register_node`（230-233，caps 来自 `capabilities()` 196）。
- `_refresh_node_capabilities()`（816-821，能力变化时重新注册）。
- `renew_lease`（694/730）。完整异步客户端。

### 2.6 qq-node / matrix-node —— 交互专用（已剥离）

- 现在 `maintain_node: false`（config 开关）；`register()`/`heartbeat()` 在 run 循环里被门控关闭（qq runtime.py:230/235；matrix runtime.py:263/287）。
- 能力：qq `[text.display, attention.observe, qq.message]`；matrix `[text.display, attention.observe, matrix.message]`。
- **角色**：仅消息传输（发消息/收消息 → 对话），不再维护节点状态。

### 2.7 当前在线节点（GET /v1/nodes，2026-10-30）

114 个节点。在线（4）：

| 节点 | kind | 能力 | 维护者 |
|---|---|---|---|
| `adb-observer` | other | `[device.state.observe]` | adb-supervisor |
| `android-9edb439ece7b6fef` | android | `[attention.observe, audio.output, microphone.input, text.display, vision.capture]` | node-keep-android（+App 也注册/心跳） |
| `proact-node` | desktop | `[attention.observe, text.display]` | （遗留） |
| `qq-room` | other | `[attention.observe, qq.message, text.display]` | node-keep-qq（room=occupied，同伴活跃节点） |

其余 110 个为离线/过期 e2e/测试节点（空 caps）。

---

## 3. 交叉与重叠点（发现）

1. **Android 节点双写**：`android-9edb439ece7b6fef` 同时被 node-keep-android（register+heartbeat）**和** App 内 `ServerScreenController`（registerNode+heartbeatNode）维护。二者都对同一 node_id 发注册/心跳——冗余但不冲突（upsert + last_seen_at 刷新），但会让 node-keep-android 的"状态维持"职责被 App 覆盖。
2. **能力来源不同步**：node-keep-android 配置里写死 caps（`attention.observe,audio.output,microphone.input,text.display,vision.capture`），App 运行时也从自身 bridge 构建同一套。若 App 能力变了（如关闭麦克风），node-keep 配置不会自动跟上 —— 目前靠"两边一致"的约定。
3. **room_state 是派生**：没有客户端"设置"room_state；它只是"同伴 active_node_id 是否等于我"的投影。任何改 active_node_id 的操作（follow/transfer/dialogue 门）都会让它翻转。
4. **节点无 lease**：`lease_expires_at` 在 companions 上，不在 nodes 上。所以"维持节点在线"（心跳）和"维持同伴租约"（lease）是两个独立职责 —— node-keep 里用两个独立节奏实现。

---

## 4. 结论 / 建议

- 服务器侧模型已稳定：节点 = 房间（last_seen_at → online 派生；active_node_id → room_state 派生），同伴持有 epoch/revision/lease。
- 客户端侧职责已按"2 角色拆分"落地：**node-keep（节点状态 + 流）** 与 **交互客户端（qq/matrix 仅消息）**。SDK/linux-node/system-node 是参考/完整实现；adb-supervisor 是纯观察者。
- 待处理：Android 节点双写（node-keep-android vs App）应明确"谁负责心跳"；当前 App 心跳（15s）+ node-keep 心跳（15s）并存。若要严格，可让 App 关闭自己的心跳（maintain_node 思路对 App 同样适用），完全交给 node-keep-android —— 但需注意 App 是真实设备上的采集进程，其"在线"语义与宿主守护进程不同。
