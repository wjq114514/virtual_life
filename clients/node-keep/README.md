# node-keep — 为单个节点长期维持注册、心跳与所属 companion 的流转状态

每个设备节点需要定期向服务端上报心跳，服务端依据 `last_seen_at` 计算
`node.online`（默认 TTL 60s）。本守护进程负责**一个节点**的注册与心跳，
使其在任意时刻保持在线，与交互客户端（QQ / Matrix / 安卓应用）解耦。
若该节点拥有某个 companion，本守护进程还会顺带维持它的**流转状态**
（输出 lease 续期 / 房间跟随 / 转移）。

## 设计取舍

- **纯 stdlib**：Python 3.12+ 标准库即可运行，无第三方依赖。与
  `adb-supervisor`、`system-node` 共用宿主。
- **一进程一节点**：配置里指定 `node_id`。要为多个节点维持心跳，就启动
  多个 node-keep 实例（每个带自己的 systemd 单元）。
- **节点层面的事务合并**：节点在线（register + heartbeat）与 companion
  流转（lease / follow / transfer）都属于**节点层面**的状态，因此由同一个
  守护进程维护。交互客户端（QQ / Matrix）只收发消息，不碰任何节点状态。
- **严格配置**：JSON ≤ 65536 字节、深度 ≤ 8，拒绝重复键与未知字段
  （与 `system-node` 约定一致）。

## 使用

```sh
# 校验配置
python3 clients/node-keep/node_keep.py --config clients/node-keep/config.qq.json --check-config

# 单次执行（注册 + 一次心跳 + 一次流转周期）
VIRTUAL_LIFE_API_TOKEN=local-dev-token \
  python3 clients/node-keep/node_keep.py --config clients/node-keep/config.qq.json --once

# 后台运行
VIRTUAL_LIFE_API_TOKEN=local-dev-token \
  python3 clients/node-keep/node_keep.py --config clients/node-keep/config.qq.json \
    --log-file ~/.local/state/node-keep-qq.log

# 安装为 systemd 用户服务（推荐）
mkdir -p ~/.config/systemd/user
cp clients/node-keep/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now node-keep-qq.service node-keep-android.service
systemctl --user status node-keep-qq.service
```

## 配置字段

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `server_url` | str | 必填 | 服务端根地址 |
| `token_env` | str | `VIRTUAL_LIFE_API_TOKEN` | 从该环境变量读取访问令牌 |
| `node_id` | str | 必填 | 要维持的节点 id（1..128） |
| `label` | str | 必填 | 节点显示名（1..256） |
| `kind` | str | `other` | `other` / `desktop` / `android` / `web` |
| `capabilities` | list[str] | `["text.display"]` | 节点能力列表 |
| `context` | dict | `{}` | 合并进心跳上下文的附加字段 |
| `interval_seconds` | int | 20 | 心跳间隔（1..3600） |
| `timeout_seconds` | int | 10 | HTTP 超时（1..120） |
| `companion` | dict\|null | `null` | 可选：该节点拥有的 companion 流转配置 |

退出码：`0` 正常；`1` 配置错误；`2` 鉴权被拒（401/403）；`3` 传输故障。

## companion 流转配置（可选）

当配置里给出 `companion` 块时，守护进程在心跳之外还按
`companion.flow_interval_seconds` 运行流转周期：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `companion_id` | str | 必填 | 要维持流转的 companion id |
| `lease_renew_seconds` | float | 5 | 输出 lease 续期间隔（0.5..30，须 < 服务端 lease TTL，默认 10s） |
| `flow_interval_seconds` | float | = lease_renew | 流转周期（0.5..300） |
| `follow_to` | str\|null | null | 非空时按需 POST /follow 迁移房间 |
| `follow_reason` | str | `manual` | follow 的 reason |
| `follow_expected_revision` | int | 0 | follow 的 expected_revision |
| `transfer_to` | str\|null | null | 非空时按需发起两方转移 |
| `transfer_reason` | str | `manual` | transfer 的 reason |
| `transfer_expected_revision` | int | 0 | transfer 的 expected_revision |
| `ack_released` | bool | true | 转移准备后回 ack（released 阶段） |
| `ack_ready` | bool | true | 转移准备后回 ack（ready 阶段） |

流转周期动作：

- **transfer 优先于 follow**（两者同时配置时，避免竞态迁移）。
- 仅当 `active_node_id == node_id`（本守护进程的节点拥有该 companion）时
  才续期 lease，这样 dialogue 从交互客户端发出时永远不会遇到
  `lease_expired`。
- 输出 `(action, detail)`：`idle` / `lease_renewed` / `lease_failed` /
  `followed` / `transfer_prepared` / `transfer_failed`。

## systemd

`systemd/node-keep-qq.service` 与 `systemd/node-keep-android.service`
分别维持 `qq-room` 与安卓观察节点 `android-9edb439ece7b6fef` 的心跳；
其中 `node-keep-qq` 的配置带 `companion` 块，因此它也负责 `proact-demo`
的 lease 续期。两者都 `After=virtual-life-runtime.service`，
`Restart=always`。

## 与二角色拆分的关系

参见 `docs/architecture.md`：**二角色拆分**（角色 A：本守护进程 = 节点状态
+ 流转；角色 B：交互客户端 = 只收发消息）。交互客户端（QQ / Matrix）配置
`maintain_node: false`，不再注册或心跳节点。节点在线、lease 续期、房间
跟随、转移全部由 node-keep 维护，与交互客户端解耦。原 `flow-supervisor`
的职责已并入本守护进程（`flow-supervisor.service` 已停止并禁用）。
