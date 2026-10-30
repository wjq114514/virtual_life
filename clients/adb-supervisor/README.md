# adb-supervisor — 长期维持 adb 服务与断连自动重连

安卓侧视觉/交互依赖 adb 与设备连接。本守护进程做两件事：

1. **长期维持 adb server**：确保 adb 服务端进程存活（`adb start-server`），
   避免因服务端退出或端口冲突导致所有工具失联。
2. **设备断连自动重连**：周期性枚举设备，检测 `offline`/`unauthorized`/`missing`
   状态，自动执行 `adb reconnect` 与 `adb reconnect offline`，把设备恢复到
   `device` 在线状态。设备重新上线后，DSH 侧 `@zseven-w/dsh-android` 插件的
   `AndroidHostController` keep-alive（1s tick、5s 后 `ensureStreaming` 重开
   screencap 循环）会自动恢复帧流。

## 设计取舍

- **纯 stdlib**：Python 3.12+ 标准库即可运行，无第三方依赖，可与 `system-node`
  客户端共用宿主。adb 通过 `subprocess` 调用，路径解析顺序与 DSH 插件一致
  （`ADB` env → `PATH` → `ANDROID_HOME`/`ANDROID_SDK_ROOT` → `~/Android/Sdk`）。
- **不在 DSH 进程内**：DSH 插件已经覆盖"帧循环崩溃重开"；但 adb **服务端**进程
  与**设备离线恢复**不在插件职责内。这里用独立 systemd 用户服务补充，
  二者互补、互不干扰。
- **只报告，不伪造**：仅做 adb 健康维持；不会向 Virtual Life 服务端上报
  虚假的设备状态。可选 `--notify-url` 上报一次设备变化事件（见下）。

## 使用

```sh
# 手动运行（前台，观察日志）
python3 clients/adb-supervisor/adb_keep.py --once --debug

# 后台运行
python3 clients/adb-supervisor/adb_keep.py --interval 10 --log-file /tmp/adb-keep.log

# 安装为 systemd 用户服务（推荐）
mkdir -p ~/.config/systemd/user
cp clients/adb-supervisor/systemd/adb-supervisor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now adb-supervisor.service
systemctl --user status adb-supervisor.service
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interval` | `10` | 检查周期（秒）。每次做一次 `adb start-server` 健康检查 + 设备枚举/重连 |
| `--reconnect-offline` | 开 | 对 `offline`/`unauthorized`/`missing` 设备执行 `adb reconnect` + `adb reconnect offline` |
| `--restart-server` | 开 | adb 服务端失联（`adb start-server` 失败）时自动重启 |
| `--max-restarts` | `5` | 连续失败后放弃本次周期（指数退避），避免重启风暴 |
| `--once` | 关 | 只执行一次（诊断用，不循环） |
| `--debug` | 关 | 输出详细日志 |
| `--log-file` | 无 | 追加写日志文件（默认 stderr） |
| `--adb` | 自动 | 指定 adb 可执行文件路径 |
| `--notify-url` | 无 | 可选：上报设备状态变化到 Virtual Life 观察端点（见下） |
| `--notify-token` | 无 | 上报用 Bearer token（默认读 `VIRTUAL_LIFE_API_TOKEN` 环境变量） |
| `--observer-node-id` | 无 | 观察者节点 ID（`device.state.observe`），用于承载上报 |
| `--observer-label` | 无 | 观察者节点标题（默认同 node_id） |
| `--state-file` | 无 | 持久化最近一次设备状态，重启后仍能识别状态跳变 |
| `--no-report-observer` | 关 | 禁用观察者注册/心跳（仅上报状态变化） |

## 上报（可选）

配置 `--notify-url` 与 `--observer-node-id` 后，守护进程每周期：
1. 注册（或刷新）观察者节点 `adb-observer`（`kind=other`，`capabilities=["device.state.observe"]`，`device.device_id=observer`）。
2. 心跳该节点，保持其在线。
3. 设备状态发生跳变（`device`→`offline`、`offline`→`missing` 等）时，向该端点 POST 一条 `device_state` 观察（与
`system-node` 的 modality 约定一致）：

```json
{
  "command_id": "<uuid4>",
  "modality": "device_state",
  "user_initiated": false,
  "data": {
    "schema_version": "1.0",
    "subject_node_id": "<设备 serial>",
    "observer_node_id": "adb-observer",
    "changes": [{"field": "adb_state", "previous": "device", "current": "offline"}]
  }
}
```

`--state-file` 持久化最近一次设备状态，使守护进程重启后仍能识别跳变
（避免重启丢失基线导致漏报）。未配置 `--notify-url` 时只写本地日志，绝不自行上报。

## 与 DSH 插件的关系

| 层 | 谁负责 | 行为 |
|---|---|---|
| adb 服务端进程存活 | **本守护进程** | `adb start-server` 周期健康检查 |
| 设备 offline/断开恢复 | **本守护进程** | `adb reconnect offline` 周期执行 |
| 帧循环（screencap）崩溃 | DSH 插件 `AndroidHostController.startKeepAlive` | 1s tick、死机 5s 后 `ensureStreaming` 重开 |
| 帧循环空闲停止 | DSH 插件 | 无消费者 5 分钟后 `stop()` |

插件读取 adb 的路径顺序与守护进程一致（`ADB` env → `PATH` → SDK 根）。
本守护进程不改动插件的环境变量，只确保 `adb start-server` 的 5037 端口
与设备状态健康。

## 与容器化模拟器协作

`adb-supervisor.service` 监控 adb 服务器与所有已连接设备（含容器化模拟器 `emulator-5554`）。当模拟器离线/上线时，观察者节点 `adb-observer` 向 `/v1/nodes/adb-observer/observations` 上报 `device_state` 变化：

```json
{"changes":[{"field":"adb_state","previous":"offline","current":"device"}],"subject_node_id":"emulator-5554","observer_node_id":"adb-observer"}
```

容器化 KVM + NVIDIA GPU 模拟器的启动方式见 `docs/screen-vision.md` 的「容器化安卓模拟器」一节（`~/.local/share/vl-emulator/launch-emulator.sh`）。
