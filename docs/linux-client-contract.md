# Linux 客户端合同（阶段 0 冻结）

规划依据 [Linux 完整客户端实施计划](linux-client-implementation-plan.md)。本文件冻结首版客户端-守护进程本地 IPC、配置、退出码与呈现会话语义。服务端新接口见 [协议](protocol.md) 第 6 节描述与 OpenAPI。任何字段变化必须先改本文件。

冻结基线：协议 `1.0`，本地 IPC `version=1`，`schema_version=1`。目标环境实际 niri/DMS/kitty/fish 版本在阶段 0 环境报告中记录，不写进本合同的强制字段。

## 1. 进程与所有权

- 每设备只有一个原生写入者：用户级 `virtual-life-linux.service` 守护进程。它独占 owner Bearer token，独占节点注册、完整 capabilities、心跳/context 与 presentation。
- `vl` CLI、fish 事件、DMS bridge、Textual TUI 只通过本地 Unix socket 连接 daemon，不持有服务器 token，不直接心跳、不注册节点、不调用模型。
- daemon 不提供任意命令执行或任意文件读取 RPC。

## 2. 路径与权限

| 内容 | 位置 | 权限 |
| --- | --- | --- |
| 非秘密配置 | `$XDG_CONFIG_HOME/virtual-life/linux/config.toml`（缺省 `~/.config/...`） | 0644 |
| owner token | 同目录 `credentials` 文件（路径由 `credential_file` 指定） | 0600 |
| 持久状态 | `$XDG_STATE_HOME/virtual-life/linux/<server-key>/<node-id>/` | 0700 |
| 本地 IPC/锁 | `$XDG_RUNTIME_DIR/virtual-life/<server-key>/<node-id>/`，socket `daemon.sock`，锁 `daemon.lock` | 目录 0700，socket 0600 |
| 缓存 | `$XDG_CACHE_HOME/virtual-life/` | 可删除，不含凭据与聊天正文 |

`server-key` = `sha256(normalized scheme://host:port)[:16]`。配置里同时保存可读 `server_url` 供诊断。`node_id`、`device_id` 首装随机生成并持久化；不采集 machine-id/MAC/用户名。重装保留身份，重置需独立命令。持久状态与 runtime 目录都按 `<server-key>/<node-id>` 隔离，所以同一台机器上并行的多个 runtime 不会互相抢占 socket、锁或 SQLite。

## 3. 配置 schema

未知字段报错。省略项使用默认值。

```toml
schema_version = 1
server_url = "http://127.0.0.1:8038"
node_id = "linux-workstation"
device_id = "persisted-installation-uuid"
label = "Linux 工作站"
credential_file = "credentials"

[desktop]
backend = "niri"              # niri | none
attention_enabled = false
dms_enabled = true
fish_activity_enabled = false

[interaction]
terminal = "kitty"
auto_connect = false
notifications = "status_only" # off | status_only | full

[reporting]
heartbeat_seconds = 20
attention_seconds = 15
attention_ttl_seconds = 45
```

`reporting.heartbeat_seconds` 必须小于服务端 node TTL，`attention_ttl_seconds` 大于 `attention_seconds`。`credential_file` 若是相对路径，相对配置文件目录解析，且必须位于配置目录内。

## 4. 本地 IPC 帧

Unix stream socket + UTF-8 JSON Lines。单帧上限 256 KiB。每连接待发队列 64，最多 8 个本地连接；超出先合并状态/delta，仍堵塞则断开。socket 连接校验 peer UID 等于进程 UID。

请求：

```json
{"version":1,"request_id":"<id>","method":"status.get","params":{}}
```

回应：

```json
{"version":1,"request_id":"<id>","result":{...}}
{"version":1,"request_id":"<id>","error":{"code":"...","message":"...","status_code":409}}
```

订阅事件（`status.subscribe` 后推送）：

```json
{"version":1,"event":"state","sequence":12,"generation":"<daemon-generation>","data":{...}}
```

`sequence` 单调递增，`generation` 在 daemon 重启时更换。`error.status_code` 使用与 HTTP 一致语义（409 冲突、503 未配置等）。

### 方法

| 组 | 方法 | 说明 |
| --- | --- | --- |
| 状态 | `status.get`、`status.subscribe`、`diagnostics.get` | 只读聚合状态与诊断 |
| 呈现 | `renderer.attach`、`renderer.heartbeat`、`renderer.detach` | TUI 注册/续期/退出；创建 renderer_id 与 generation |
| 伙伴 | `companion.list`、`companion.select`、`companion.connect`、`companion.mode.set` | 选择与显式接入；`connect` 执行迁移/续租 |
| 对话 | `dialogue.send`、`dialogue.hide`、`dialogue.receipt.get`、`dialogue.history.get` | 发送、撤草稿、原回执、历史 |
| 迁移 | `travel.prepare`、`travel.status`、`travel.abort` | 迁移 |
| 注意力 | `attention.override.set`、`attention.override.clear`、`attention.boundary` | 显式限时状态与边界 |
| 管理 | `webui.open` | 只打开配置 origin 的固定页面 |
| fish | `shell.register`、`shell.started`、`shell.finished`、`shell.closed` | 可选、不含 `$argv` |

`renderer.attach` 结果至少含 `renderer_id`、`generation`、`node_id`、`companion_id?`、`epoch?`、`presentation_id?`、`lease_deadline_boottime?`。同一伙伴同一时刻只有一个实时角色 renderer；第二个 TUI 需用户显式接管才替换，旧 generation 的草稿、结果、心跳全部丢弃。

`dialogue.send` 参数 `{text, command_id?}`；正文只走 IPC，不进 argv。服务端流式 `delta.text` 是累计全文，替换草稿。`dialogue.hide` 只撤本地显示，不取消服务端任务。

### 注意力采样

周期性采样只在 `desktop.attention_enabled` 且 `backend != "none"` 时运行，也就是只在注册能力含 `attention.observe` 时运行；间隔为 `reporting.attention_seconds`。来源只读、有界、不含内容：

| 来源 | 数据 |
| --- | --- |
| niri | 焦点 workspace 的 `id`/`idx`/`output`；不读命名 workspace 与窗口标题 |
| logind | `IdleHint`、`LockedHint` 两个布尔 |
| DMS | 仅 presence 探测；不产生样本 |

样本字段固定为：`session_id = <node_id>-attn`（≤128 字符）、`sequence`、`sampled_at`、`ttl_seconds = min(reporting.attention_ttl_seconds, 300)`、`activity = "unknown"`（客户端不分类内容）、`focus`（有焦点 workspace 才为 `focused`）、`availability`/`user_present`/`locked`（只由 idle/locked 布尔推导，未知时给 `unknown`/`null`）、`boundary`/`boundary_id`。

`sequence` 持久化在本地 SQLite，重启后延续；服务端要求同 session 严格递增，因此发送前就消耗序号，失败请求不复用同号。内容去重排除 `sequence`/`sampled_at`/`ttl_seconds`，无变化不发。`attention.boundary` 立即发送一次样本（best effort），失败则留在队列等下一周期，并记入 `last_attention_error`。样本发送后通过订阅推送 `attention.sample` 事件，`diagnostics.get` 返回 `attention.{enabled,session_id,sequence,adapters,last_error}`。

## 5. 呈现会话与输出状态机

状态：`detached -> renderer_ready -> presentation_open -> connecting -> active -> paused/releasing`。

只有同时满足当前 node owner、epoch、有效 presentation、本地 renderer generation、未过期保守租约、允许的桌面状态，才实际绘制。保守截止：

```text
remaining = min(server expires_at - server_time, ttl_seconds, local_max)
deadline_boottime = request_started_boottime + remaining - safety_margin
```

以请求开始时间计，不以响应到达时间计；`safety_margin` 从 250 ms 起。时钟使用 `time.clock_gettime_ns(time.CLOCK_BOOTTIME)`；不可用则 fail closed。daemon 通过 IPC 下发 deadline，TUI 每次绘制与自身定时器复核；daemon 单方面停发消息不足，被冻结的旧 delta 仍必须被 renderer 拒绝。断开 socket 立即清草稿。

TUI attach：daemon 确保节点 capability 含 `text.display`，随后 `POST /v1/nodes/{node_id}/presentations` 开启 presentation，创建 lease。detach/replaced/SIGTERM：先清本地输出，再 `DELETE` presentation，最后更新注册能力。

daemon 是 presentation 的唯一所有者：即使 TUI 崩溃或未 attach，daemon 也按 `min(5s, ttl_seconds/3)` 续期 presentation，并在服务端报过期时清掉本地 `presentation_id`、停止 voice、重新注册能力（移除 `text.display`）。这样不会把一个已死会话的显示能力继续上报给服务端。

## 6. 迁移与 target_ready

本机为 source：先停 live 输出、清草稿、停止 lease 续期，再发 `released`。本机为 target：确认 TUI 已准备、presentation 有效、输出初始化完成后，`ready` 携带 `presentation_id`。绑定后释放/过期该 presentation 会使迁移在提交前中止。

## 7. CLI 与退出码

```fish
vl setup | doctor [--json] | status | open | tui | chat [companion] | connect <companion> | disconnect
vl companion list | companion select <companion>
vl mode <quiet|chat|focus|away> | break | task done | ask [--stdin] [text]
vl travel <node-id> | web | attention override <state> [--seconds] | attention clear | renderer <attach|detach|status>
vl shell <register|started|finished|closed> | screen <start|stop|status> | voice <doctor|devices|start|stop|status|commit|interrupt>
vl daemon run | version
```

选中伙伴有三条路：`vl companion list` 看列表与当前选中项（daemon 首次 list 会自动选中第一个）；`vl companion select <id>` 只改选中、不接入；`vl chat <id>` / `vl connect <id>` 是带接入的选择。`connect` 之前必须有已 attach 的 renderer，否则返回 `renderer_required`（退出码 5），TUI 就是那个 renderer。

`vl setup` 是完整安装与更新入口：先从服务端（或 `--dist-url`/`--wheel`）拉取客户端包并校验 sha256，再写 config、凭据与桌面资产。可 `--dry-run`、`--no-systemd`、`--clobber`、`--json`。它与 `packaging/install.py` 共用 `virtual_life_linux.installer`，因此不再要求本机有源码树。凭据只从 `--token`、`--token-file`、`VIRTUAL_LIFE_TOKEN` 或既有 0600 凭据文件读取，交互终端才提示输入，输入不回显；任何情况下不把 token 打印或写入日志。`vl chat`/`vl tui` 拥有自己的事件循环，不能在 daemon 进程里启动。

`vl connect` 非交互且无已准备 renderer 时返回 `renderer_required`。

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 未分类失败 |
| 2 | 用法/配置错误 |
| 3 | 认证失败 |
| 4 | 离线/不可达 |
| 5 | 冲突（owner/epoch/lease/presentation） |
| 6 | 结果未明（需按原 command_id 查询） |
| 7 | 服务端缺少所需特性 |

主入口是 TUI。`vl ask [text]` 便于脚本调用，但文本会出现在 argv 与 shell 历史中，批量或敏感文本请用 `vl ask --stdin`。失败保留原 command_id，不自动换 ID 重发。

## 8. 服务端能力投影

daemon 注册 `kind=desktop`。能力计算：默认 `device.state.report`；适配器存在且用户启用时加 `attention.observe`；renderer attach 并初始化完成后才加 `text.display`。不声明 `avatar.render`、`audio.output`、`vision.capture`。TUI 断开/替换/退出时先释放 presentation，再移除 `text.display`。

## 9. 兼容与诊断

- 旧服务器无 presentation/native-output 时，遥测与只读诊断仍可用；完整聊天接入提示缺少特性（退出码 7），不静默绕过呈现隔离。
- `vl doctor --json` 报告完整结构：客户端版本、配置路径/URL/node、凭据是否可读、服务端可达性与协议版本、daemon 聚合状态、renderer/presentation/lease、注意力与 voice 状态、最后错误。不带 `--json` 时输出紧凑摘要（只含布尔与标识，不含正文）。探测服务端只用 6 秒超时的只读请求，不注册节点、不写任何数据。秘密、命令行、窗口标题、聊天正文不进入诊断。
- 遥测采集为小范围抽取的纯标准库模块，构建时复制白名单并记录来源哈希。
- wheel 把 `integrations/` 与 `config.example.toml` 打成包数据；`packaging/build.py` 校验缺失资产并失败，`packaging/install.py` 先读源码树、再读已安装包数据，因此从 wheel 安装后仍可 `--skip-package` 补装资产。

## 10. 客户端分发通道

`vl setup` 的包来源是服务端的分发端点（见 [统一配置](configuration.md) 的 `[dist]`）：

| 端点 | 用途 |
| --- | --- |
| `GET /v1/dist/manifest.json` | 按版本升序返回包清单，含 `default_version` |
| `GET /v1/dist/packages/{wheel}` | 下载指定 wheel |
| `POST /v1/dist/packages` | 上传 wheel，额外要求 `[dist] upload_secret` |

清单 schema（`dist_version = "1.0"`）：

```json
{
  "dist_version": "1.0",
  "default_version": "0.1.1",
  "packages": [
    {
      "version": "0.1.1",
      "wheel": "virtual_life_linux-0.1.1-py3-none-any.whl",
      "sha256": "64 hex",
      "size_bytes": 77276,
      "released_at": "2026-10-24T14:13:30Z",
      "requires_python": ">=3.12",
      "runtime_version": null
    }
  ]
}
```

客户端行为：`--version` 省略或为 `latest` 时取最高版本；指定版本必须精确匹配，否则列出可用版本并失败。下载完成后本地重算 sha256 与清单比对，不匹配则删除临时文件并中止安装，绝不安装校验失败的包。文件名不匹配 `virtual_life_linux-<版本>-py3-none-any.whl` 一律拒绝。

`--dist-url` 允许使用 `file://` 或无鉴权的静态 HTTP 位置，布局为 `<root>/dist/manifest.json` 与 `<root>/dist/packages/{wheel}`，便于离线或自建仓库发布。安装逻辑（资产、config、凭据、systemd）全部在 `virtual_life_linux.installer` 内，`packaging/install.py` 只是薄壳，保证源码树与 wheel 两条路径行为一致。

### 10.1 GitHub Releases 来源

`--github owner/repo` 让 `vl setup` 从仓库的 releases 拉取，无需自建服务器。来源优先级：`--wheel` > `--github` > `--dist-url` > `--server`。

| 选项 | 说明 |
| --- | --- |
| `--github owner/repo` | 仓库；也接受 `https://github.com/o/r`、`o/r.git`、`git@github.com:o/r.git` 等写法 |
| `--github-token` | 私有仓库用；默认读 `GITHUB_TOKEN`。绝与 runtime token 混用，也不写入客户端配置 |
| `--github-api` | API root，默认 `https://api.github.com`；可用 `VIRTUAL_LIFE_LINUX_GITHUB_API` 覆盖（GitHub Enterprise 与本地验证） |

客户端用 `GET /repos/{owner}/{repo}/releases?per_page=50` 读取 release 列表，按 wheel 文件名匹配 `virtual_life_linux-<版本>-py3-none-any.whl`，版本从**文件名**取（不信任 tag），tag 的可选 `v` 前缀不影响匹配。下载 URL 直接取资产的 `browser_download_url`。

**校验和规则（fail-closed）**。一个 wheel 只有在能拿到 sha256 时才可被安装，来源优先级：

1. 同一 release 内的校验和资产：`SHA256SUMS` / `sha256sums.txt` / `checksums.txt` / `sums` / `sha256.txt` / `*.sha256`，按 `sha256  文件名` 逐行解析（容忍 `*` 二进制标记）；
2. 资产的 `digest` 字段（`SHA256:<hex>` 或裸 hex）。

一个 release 完全没发校验和时，客户端报 `no published checksums` 并要求补发；单个 wheel 缺校验和时不会进 `latest`（`latest` 从不指向无法验证的版本），但仍在 `packages` 中列出，显式 `--version` 选中后在下载前被拒绝安装。校验和不匹配同样中止，且删除已下载的临时文件。

`DistPackage.download_url` 只由 GitHub 适配器填充，服务端 manifest 的 `from_json()` 从不读这个键，因此服务端无法把下载（以及 runtime token）重定向到第三方主机。GitHub token 只发往 `--github-api` 指向的主机。下载产生的临时目录在安装步骤结束后清理，无论成功或失败。

发布端：`packaging/build.py` 在 `dist/` 下生成 `SHA256SUMS`（含 wheel 与 sdist），运维者只需上传 wheel + `SHA256SUMS` 两个资产。
