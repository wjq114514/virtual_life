# Linux 节点安装与 SSH 分发

WebUI 的连接配置和设备区域提供「Linux 分发」入口。由主服务器连接目标 SSH，上传当前版本的轻客户端并安装 systemd 服务；浏览器提交任务并查询回执。首版安装直接模式的设备状态节点，采集本机 CPU、内存、负载、运行时间和磁盘，不调用模型、不申请角色输出租约。

## 使用条件

- 目标为 Linux，已安装 Python 3.12+、systemd 和 SSH 服务。安装器不会自动安装 Python 或系统依赖。
- SSH 使用 root，或可通过 `sudo -n` 无交互执行安装器的账户；暂不支持交互式 sudo 密码。
- 主服务器能访问目标 SSH，目标能访问表单里的主服务器 HTTP(S) origin。地址从目标机看必须可达，远程机器上的 `127.0.0.1` 通常不是主服务器。
- 主服务器按现有单所有者、单进程方式运行。不要为同一数据库同时启动多个部署进程；此版任务调度不是跨进程队列。

SSH 可选密码，或粘贴私钥及可选解密口令。服务端不读取 SSH agent 或 `~/.ssh` 私钥。Python 字段可填程序名或绝对路径，建议使用服务账户能够执行的系统 Python；位于 root 私有目录的解释器可能无法供受限服务使用。

## WebUI 操作

1. 连接运行时，在连接设置或设备区域打开 Linux 分发。
2. 输入 SSH 地址、端口和用户名，读取目标主机 SHA256 指纹。通过独立可信渠道核对后勾选信任；更改地址或端口会使本次信任失效。
3. 输入 SSH 凭据，可以先检查环境。检查读取固定安装路径的现有身份，不安装程序。SSH 凭据在发出请求后即从输入框清除，正式安装时需要重新输入。
4. 填写目标机可访问的服务器地址和节点标题。可选勾选“采集桌面状态”，默认关闭；说明见[自动采集](linux-attention-collection.md)。首次安装可自动生成节点 ID；升级默认沿用目标机配置的 ID。提交后保留任务 ID，等待安装与新凭据心跳验证。
5. 可以关闭窗口再打开查询原任务。页面刷新会丢失内存中的任务引用，刷新前应记录任务 ID；已知 ID 可通过下列 GET 接口查询。

读取指纹只是展示该连接观察到的密钥，不能替代独立核对。实际认证连接先验证所确认的指纹，匹配后才尝试 SSH 认证。SSH 原始输出和异常不展示到页面。

SSH 密码、私钥和口令只在该次请求及工作线程内存中使用，不写数据库、配置导出或浏览器持久存储。WebUI 管理令牌仍按已有连接设置处理；跨机器访问时应通过 HTTPS 保护请求。

## 身份与凭据

目标机获得本次部署独有的 `vl_reporter_` 凭据，数据库仅保存哈希。它只允许以 `kind=other`、`device.device_id=node_id`、已授权能力注册自身，并写自身心跳和自身观察。默认只有 `device.state.report`；显式开启桌面采集时，此次凭据增加自身 `attention.observe` 和注意力写入权限。不能读取伙伴、记忆、事件或配置，不能使用 WebSocket、安装其他节点或领取输出租约。

授权固定在每个凭据上，旧凭据不会自动提权。两种受限 reporter 可用原凭据恢复原能力配置，保留升级/降级失败时的安装器回滚路径；不要并行运行两个完整写入者。其他部署的旧凭据仍按原有规则单独撤销。

已有 Android、桌面呈现节点、Web 专用节点以及带其他设备身份或额外能力的原生节点不能被此安装器覆盖。若已有一个原生宿主管理该设备，需由宿主汇聚遥测，保持每台设备只有一个完整心跳写入者。浏览器依旧通过独立 Web access 访问设备。

重新安装必须保留已有 `node_id` 和 `server_url`；指定不一致会失败，不能借升级把现有服务静默绑定到另一服务器。若本地配置身份已丢失，应由用户明确填写已有节点 ID；不会扫描网络或用 MAC 猜测身份。

任务只有在安装器报告成功，并且**此次新签发凭据**提交过成功心跳后才标记完成。旧客户端的在线状态不能替代该证据。服务默认采样间隔为 20 秒。

## 安装位置与恢复

| 内容 | 固定位置 |
| --- | --- |
| 程序版本 | `/opt/virtual-life-system-node/versions/<version>-<hash>/` |
| 当前版本 | `/opt/virtual-life-system-node/current` |
| 配置 | `/etc/virtual-life-system-node/config.json` |
| 节点凭据 | `/etc/virtual-life-system-node/token.env`，0600 |
| 运行状态及进程锁 | `/var/lib/virtual-life-system-node/` |
| systemd 单元 | `/etc/systemd/system/virtual-life-system-node.service` |

安装器使用 `virtual-life` 系统账户运行客户端，验证包内哈希、离线配置和现有身份，停止旧服务后原子切换版本。服务采用 `Type=notify`，客户端完成首次完整上报周期后才通知就绪，启动最多等待 30 秒。启动失败会尝试恢复旧版本、配置和凭据。版本目录保留；此版不做自动更新、远程卸载和旧版本自动清理。

SSH 上传使用 0700 临时目录及 0600 文件，不将 Token 放在命令参数。若 SSH 在安装期间断开，远端安装器可能仍在运行；此时任务为失败或结果未知，暂存输入可能保留，不能据此断言已经回滚。先通过独立 SSH 检查 `systemctl status virtual-life-system-node.service`、固定配置和暂存目录，再决定清理或建立新任务。服务端重启把未完成任务标记为 `interrupted`，不会自动重装。

页面里的「撤销凭据」只撤销该任务签发的节点凭据，不卸载远端文件、不停止 systemd，也不撤销其他部署的旧凭据。客户端收到 401/403 后会退出，服务不会因配置或认证错误反复重启。检查失败而未签发凭据时没有可撤销的凭据。未收到验证心跳也可能是服务已安装但地址或网络配置错误，应先排查，避免连续提交新任务。

## HTTP 接口

全部需要管理 Bearer token，SSH 请求 body 上限 128 KiB；秘密字段相关校验只返回固定错误信息。

| 方法和路径 | 用途 |
| --- | --- |
| `GET /v1/deployments/linux/bundle` | 下载当前版本 gzip 安装包，`X-Content-SHA256` 提供完整性校验值 |
| `POST /v1/deployments/linux/host-key` | `{host,port:22}`，读取主机密钥类型与指纹 |
| `POST /v1/deployments/linux/inspect` | 检查固定安装环境与现有节点身份 |
| `POST /v1/deployments/linux` | 提交安装任务，202 返回持久任务回执 |
| `GET /v1/deployments/linux/{command_id}` | 查询原任务 |
| `DELETE /v1/deployments/linux/{command_id}/credential` | 终态任务的独立凭据撤销 |

SSH 检查请求为 `{host,port,username,password?,private_key?,passphrase?,host_key_fingerprint,python:"python3"}`，密码与私钥二选一。安装请求另需 `{command_id,server_url,label,node_id?,attention_enabled:false}`，采集字段严格为布尔值，不用于环境检查。私钥为文本本体，不接受服务器文件路径。字段合同以 [OpenAPI](openapi.json) 为准。

同 `command_id`、相同公开参数返回原任务；变更公开参数返回 409。SSH 秘密不进入幂等指纹，使用不同密码重交同一 ID 也不会重执行。最多两个执行线程、四个未完成任务，同 SSH 地址和端口不能并发安装；目标机还有安装锁，防止别名访问下重复切换。同一 ID 结果未知时只查询原任务，不自动重新发起安装。

## 本地构建与手动安装

从仓库根目录运行：

```sh
uv run python clients/system-node/build.py
python3 clients/system-node/dist/system-node.pyz --version
python3 clients/system-node/dist/system-node.pyz --config config.json --check-config
```

产物包括源码包、`.pyz`、安装器、`bundle.tar.gz` 和 `SHA256SUMS`。节点不需要安装 FastAPI、Paramiko 或主服务器；`.pyz` 只用 Python 标准库。服务端 wheel 同时包含生成安装包所需资源，部署入口不依赖原源码目录存在。

手动安装需自行准备配置和仅含 Token 的 ASCII 文件，放在受保护目录后以 root 执行：

```sh
python3 installer.py --bundle bundle.tar.gz --config-file config.json --token-file token.txt
```

手动提供的 Token 权限由其签发方决定；CLI 不会自行联系管理接口申请新凭据。包和示例不包含实际 Token。已在 Ubuntu 24.04 x86_64 虚拟机上通过真实 WebUI、OpenSSH、systemd 验证首装、重装、密码/加密私钥、非 root 免密 sudo、设备采样、撤销和两类启动故障回滚；环境、复现脚本和其他平台边界见[真实联调验收](real-validation-report.md)。

WebUI 专项可通过 `uv run --with playwright python examples/webui_linux_smoke.py` 重现（先安装 Chromium，或传入 `--browser-executable`）。该脚本使用隔离静态服务和模拟 API，不访问实际运行时或设备；结果与截图默认写入 `.data/webui-linux-review/`。后端安装、凭据和 SSH 握手测试使用 `uv run pytest backend/tests/test_linux*.py backend/tests/test_node_credentials.py`。
