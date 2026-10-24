# 设备状态轻客户端

Python 3.12+ 标准库客户端，无第三方运行依赖。直接模式采集当前运行环境的 Linux 指标；网关模式还观察明确配置的 TCP 或 HTTP 端点。接口遵循 [设备状态合同](../../docs/device-state-tracking.md) 和 [节点协议](../../docs/protocol.md)。

可选启用[桌面状态自动采集](../../docs/linux-attention-collection.md)：logind 会话、锁屏和空闲提示；同用户 X11 环境补充有限前台应用类别。默认关闭，不采标题、截图或输入内容，会议/共享与专注保持未知。可通过 WebUI SSH 分发勾选，或使用[桌面配置](examples/desktop.json)；旧的仅设备上报凭据需重新授权才可提交注意力。

## 运行

先复制并编辑 [直接模式配置](examples/local.json) 或 [网关配置](examples/gateway.json)，将 Token 放入配置指定的环境变量（默认 `VIRTUAL_LIFE_API_TOKEN`），再启动：

```bash
python3.12 clients/system-node/node_client.py --config /path/to/config.json
```

运行会向配置的主服务器注册节点并持续写入完整心跳。`--once` 执行一轮真实注册、采样、心跳，并在发送预算内提交待发观察后退出，也会探测配置的端点：

```bash
python3.12 clients/system-node/node_client.py --config /path/to/config.json --once
```

网关示例中的 `192.0.2.1`、`192.0.2.2` 是文档地址，使用前替换为指定设备地址。客户端只访问所列端点，不扫描网络，也不执行远程命令。环境变量中的 HTTP 代理设置不会被使用。退出码：`0` 正常；`2` 配置或 Python 版本错误；`3` 主服务器返回 401/403；`4` 其他上报错误（包括 `--once` 的暂时失败）。SIGINT/SIGTERM 会结束等待，并在当前有超时的网络操作返回后停止。

节点以设备为单位，每个原生 `node_id` 仍由一个完整写入者管理；同一设备的 Android/桌面宿主与遥测需由宿主聚合能力和心跳，不要分别覆盖能力列表或 context。WebUI 已使用独立访问会话，可关联这个设备节点而不覆盖遥测。网关目标 ID 仍表示各个被观察设备。详见[设备身份](../../docs/device-identity.md)。

本客户端仅为自身注册附带 `device.device_id=node_id`，网关目标不会继承其设备身份或连接 IP。配置中的 ID 重启后保持稳定，每次启动的 `session_id` 是新的 UUID，`sequence` 在同一进程内递增；通用 device_state 样本的服务端会话隔离与乱序拒绝仍待实现（与 Web access 的隔离不同）。

## 配置

安装器生成的 systemd 服务采用 `Type=notify`：完成首次完整上报周期后发送就绪通知，启动最多等待 30 秒；失败会触发安装器尝试回滚。直接从命令行运行不需要 systemd，`--once` 和 `--check-config` 不发送就绪通知。真实 Ubuntu 安装、故障回滚及测试边界见[验收报告](../../docs/real-validation-report.md)。

配置必须是 UTF-8 常规 JSON 文件，最多 65,536 字节、8 层嵌套。拒绝未知字段、重复键、非有限数字以及不符合范围的值。必须提供 `server_url`、`node_id`、`label`。

| 字段 | 默认值与范围 |
| --- | --- |
| `server_url` | 必填 HTTP(S) origin，例如 `http://127.0.0.1:8000`；不含路径前缀或 query |
| `node_id` / `label` | ID 为 1–128 字符；标题为 1–256 字符，支持 Unicode |
| `device_class` | `other`；可选 `server`、`workstation`、`gateway`、`router`、`nas`、`other` |
| `deployment_mode` | `direct`；可选 `direct`、`gateway`；目标只允许出现在网关模式 |
| `token_env` | `VIRTUAL_LIFE_API_TOKEN`；1–128 字符的环境变量名称 |
| `disk_path` | `/`；只采集该路径所在文件系统；相对路径按配置文件目录解析 |
| `operation_file` | `null`；可选元数据文件，相对路径按配置文件目录解析 |
| `interval_seconds` | `20`；1–3,600 秒 |
| `ttl_seconds` | `60`；整数 1–86,400 秒，须大于下述上报预算 |
| `request_timeout_seconds` | `5`；0.1–30 秒 |
| `probe_timeout_seconds` | `3`；0.1–30 秒，目标可以覆盖 |
| `backoff_initial_seconds` / `backoff_max_seconds` | `1` / `60`；各为 0.1–3,600 秒，上限须不小于初始值 |
| `observation_min_interval_seconds` | `60`；1–86,400 秒，每个节点的状态类别事件间隔 |
| `pending_observations_limit` | `128`；整数 1–4,096 条 |
| `targets` | `[]`；最多 64 个不同 ID 的目标，并且与自身 ID 不同 |
| `attention` | `{enabled:false}`；可选 `session_id` 明确选择本机桌面；开启后间隔不超过 30 秒，额外计入 2 秒探测及一次请求预算 |

字符串不含控制字符；节点 ID 还拒绝 `/`、`\` 和独立的 `.`、`..`。HTTP URL 最多 2,048 字符，使用 ASCII、百分号编码路径或 IDNA 域名；拒绝 URL 用户名/密码、fragment、敏感 Token/密钥 query 参数。Token 仅从环境变量读取，最多 4,096 个 ASCII 字符且不含空白或控制字符。探测请求使用独立请求对象，不携带主服务器 Token、不跟随重定向、不读取响应 body。主服务器的认证请求也不跟随重定向。

TCP 目标使用 `node_id`、`label`、`adapter: "tcp"`、`host` 和整数 `port`（1–65,535）；HTTP 目标使用 `node_id`、`label`、`adapter: "http"` 和 `url`。目标可选 `device_class` 和 `timeout_seconds`，其他字段被拒绝；TCP 不能配置 `url`，HTTP 不能配置 `host`/`port`。

采样和探测依次进行。客户端按以下公式估算保守的顺序上报预算，其中管理节点数包含自身和全部目标：

```text
观察发送预算 = min(interval_seconds / 4, request_timeout_seconds)
上报预算 = interval_seconds + 所有目标 timeout_seconds 之和
         + (管理节点数 + 1) × request_timeout_seconds + 观察发送预算
```

`ttl_seconds` 必须严格大于上报预算，否则以固定代码 `ttl_not_above_reporting_budget` 拒绝配置。默认直接模式预算为 35 秒，双目标网关预算为 51 秒，示例中的 60 秒有效期均满足要求。主服务器的 `VIRTUAL_LIFE_NODE_TTL_SECONDS` 也应按该预算配合设置并保留余量，避免观察者在一轮顺序上报完成前被判为离线。

目标较多时，一轮耗时仍可能超过采样间隔。HTTP 超时限制 socket 操作，系统 DNS、本地文件系统延迟和故障重试不包含在此估算内；该预算不提供严格的整轮截止时间。恢复连接后的心跳重新采样，不重用旧样本并修改其时间。

## 状态与事件

所有节点注册 `kind: "other"` 和 `device.state.report`；网关自身额外声明 `device.state.observe`。每个代理目标作为独立节点注册。启动时注册一次，只有心跳 404 或观察者记录丢失时重新注册，不在每轮心跳重复注册。

开启桌面采集时仅自身额外注册 `attention.observe`，不授予网关目标。注意力经独立类型化接口提交，失败后重采，不排队重播旧样本；下一次心跳的 `context.attention_collection` 记录上一轮诊断及其采样时刻。探测不可用时保留 unknown，不伪造可打断状态。

每次心跳发送完整 `context.device_state`，包括身份、观察者、会话、序号、采样 UTC 时间、有效期、来源、可达性、健康、指标和操作元数据。服务端 `node.online` 只表示上报路径新鲜；代理可达性必须读取 `reachability.status`。

Linux 本地指标包含 CPU 差分使用率、内存总量/可用量/使用率、uptime、三项负载、配置路径的磁盘总量/空闲量/使用率。磁盘使用率采用系统返回的 `used / total`；`disk_free_bytes` 是调用者可用空间，在 Unix 上可能扣除保留块。首次 CPU 为 `null`，错误代码为 `cpu_warmup`；不可取得的数值也为 `null`，来源质量为 `partial` 或 `unknown`。非 Linux 平台只尝试负载和磁盘，其他指标标记为不支持。容器中的 `/proc` 和磁盘视图属于当前运行环境，可能混合宿主和容器范围。

TCP 拒绝、超时、DNS 或已知网络失败表示从该观察者 `unreachable`；权限、TLS 验证或无效响应等采集问题使用 `unknown`。HTTP 收到任何响应，包括重定向、401 或 500，都表示 `reachable`，另记 `http_status`。代理 `metrics` 为空。所有状态的健康始终为 `{"status":"unknown","reason":"not_evaluated"}`；端口开放或 HTTP 200 不触发整机健康判断。

第一份样本是基线。之后只有 `reachability.status`、`source.quality` 类别变化产生 `device_state` 观察，数值波动只刷新快照。观察提交到自身观察者节点，`data` 包含 `schema_version`、`subject_node_id`、`observer_node_id`、`session_id`、`sequence`、`sampled_at` 和 `changes: [{field, previous, current}]`。这些观察的 `user_initiated` 为 `false`。

同一节点只有一条待确认的类别观察。确认后默认至少等待 60 秒再发送下一条；其间的变化由下一次新样本与最后确认的基线比较，合并为最新净变化。每条已经入队的观察固定 UUID `command_id` 和完整字节请求体，重试使用原值。每轮最多发送 32 条，同时使用单调时钟限制观察发送阶段：到达 `min(interval_seconds / 4, request_timeout_seconds)` 的软预算后，停止发起新观察请求，默认预算为 5 秒；已经进行的请求可以超过该预算。未发送的观察按原 FIFO 顺序留到下一轮，后续重试沿用原请求。队列满时丢弃最旧待发观察并记录累计丢弃计数；后续类别差异可以从新样本重新生成，已丢弃的操作阶段可能丢失。

队列和去重记录只保存在内存，重启不恢复未确认事件。网络、5xx 和 429 错误采用有上限的指数退避，每次在当前退避上限的 50%–100% 之间加入抖动；成功后重置。主服务器 401/403 会明确退出。API 请求最多 65,536 字节、响应最多 131,072 字节，JSON 深度最多 32 层。日志只包含固定原因码、状态码和计数，不输出 Token、完整 URL、响应 body、指标或操作内容。

## 操作元数据

在配置中指定 `operation_file`，由维护工具通过原子替换写入 [操作示例](examples/operation.json)。示例的 `updated_at` 须换为当前 UTC 时间，`subject_node_id` 须属于当前配置管理的节点。

操作文件最多 4,096 字节，只允许并要求以下九个字段：`operation_id`、`subject_node_id`、`origin_node_id`、`kind`、`phase`、`label`、`updated_at`、`ttl_seconds`、`user_initiated`。三个 ID 最多 128 字符；label 最多 256 字符；时间须带 UTC 时区且不晚于当前时间；TTL 为整数 1–86,400 秒；`user_initiated` 必须是显式布尔值。

`kind` 为 `maintenance`、`deployment`、`backup`、`network_change` 或 `other`；`phase` 为 `started`、`progress`、`completed`、`failed` 或 `cancelled`。有效元数据只附加到对应目标的快照；阶段变化发送 `modality: "operation"`，其 `data` 为上述来源字段加 `operation` 对象，观察的 `user_initiated` 沿用文件值。同一 `(operation_id, phase)` 的标题变化只更新快照。

进程记住最近 4,096 个操作 ID/阶段组合；较早记录被淘汰后，再次出现可能重发。缺失、过期、无效或不属于本客户端的文件使 `operation` 变为 `null`，只记录原因码，不推断完成。客户端不读取 shell 历史、原始命令或进程参数，也不自动生成长期记忆。

## 可选 systemd 服务

[示例单元](systemd/virtual-life-system-node.service) 假定客户端放在 `/opt/virtual-life-system-node`，配置位于 `/etc/virtual-life-system-node/config.json`，Token 通过同目录的 `token.env` 提供。安装前调整 Python 3.12+ 路径、运行用户和本地文件路径，并确保该用户能读取配置、Token 文件、操作文件和磁盘路径。单元遇到配置错误或 401/403 时不会自动重启。此仓库只提供模板，未安装或启动该服务。

## Linux 独立包与系统服务安装

WebUI 已支持通过 SSH 密码或私钥远程安装这一直接模式客户端，入口在连接配置与设备区域；它会核对主机指纹、签发仅能上报自身的凭据、安装服务并验证新心跳。完整要求、接口与异常恢复见 [Linux SSH 分发](../../docs/linux-deployment.md)。网关模式继续手动配置，不适用首版 SSH 凭据范围。

从仓库根运行 `uv run python clients/system-node/build.py`，会先同步 wheel 内资源，
再输出 `clients/system-node/dist/` 下的 `source.tar.gz`、`system-node.pyz`、
`bundle.tar.gz`、`installer.py` 与 `SHA256SUMS`。客户端仅使用 Python 标准库，
目标需要已有 Python 3.12+。`.pyz` 可执行 `python3 system-node.pyz --version`，
或用 `--config config.json --check-config` 离线校验（不读取 token、不采样、不联网）。
`--once` 仍会真实注册、心跳并提交观测。

Linux 安装器只能以 root 运行（SSH 可使用 `sudo -n`），使用固定 systemd 系统服务：

```sh
python3 installer.py --bundle bundle.tar.gz --config-file config.json --token-file token.txt
```

`token.txt` 是独立的受限 ASCII URL-safe token 文件，不能包含在包内；上传暂存目录应为
0700，token 文件为 0600，调用后由分发端清理。配置 `token_env` 必须为
`VIRTUAL_LIFE_API_TOKEN`。安装器创建无登录 shell 的 `virtual-life` 系统用户，
安装到 `/opt/virtual-life-system-node/versions/<version>-<hash>/`，原子切换 `current`；
配置为 `/etc/virtual-life-system-node/config.json`，密钥为同目录的 0600 `token.env`，
运行状态目录为 `/var/lib/virtual-life-system-node/`。服务名为
`virtual-life-system-node.service`。

升级保持已有 `node_id` 和 `server_url`，修改这些字段会拒绝安装。
安装前离线校验配置，停止旧服务后才切换文件；启动失败则恢复旧版本、配置和 token，
并重启旧服务。服务持有状态目录的进程锁，重复启动同状态目录会失败。
安装成功的最后一行 stdout 是 `{ "node_id": "...", "version": "...", "status": "installed" }`；
失败只返回固定 reason，不输出命令 stderr。安装和回滚测试只使用临时路径及模拟服务命令，
不操作开发机 systemd。
