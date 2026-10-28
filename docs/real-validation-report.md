# 最近功能真实联调验收

2026-10-15，对情感纠错、自触发、设备身份、WebUI、流式聊天和 Linux SSH 分发进行隔离验收。测试实际启动浏览器、HTTP 服务、OpenSSH 和 systemd；模型端使用本地确定性 HTTP 服务，未调用外部 LLM。没有读取或改写用户 `.env`、生产数据库、现有运行服务或家庭设备。

## 环境与方法

- 主服务器：Python 3.14.3、Uvicorn、临时 SQLite、随机管理令牌。
- 浏览器：真实 Chromium，桌面 1440×900、手机 390×844。
- Linux 目标：QEMU/KVM 中的 Ubuntu 24.04 x86_64，独立内核、OpenSSH、Python 3.12.3、systemd 255。不是模拟 `systemctl` 的容器。Docker 仅承载 QEMU 进程。
- 官方 Ubuntu cloud image 在启动前按官方 `SHA256SUMS` 核验，镜像 SHA256 为 `6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2`。
- SSH 只转发至本机 `127.0.0.1:50922`，主机指纹与虚拟机启动日志独立核对。目标通过 QEMU 网络连接临时 API。
- SSH、SFTP、安装器、systemd、客户端采样和上报均使用真实实现；故障测试只替换上传包中的客户端程序。

## 验收结果

| 范围 | 实际验证 | 结果 |
| --- | --- | --- |
| Linux WebUI 分发，11 组 | 页面读取指纹、环境检查、首装、密码重装、加密 OpenSSH 私钥与非 root 免密 sudo、下载哈希和暂存清理 | 通过 |
| Linux 设备上报 | `/proc` 的 CPU、内存、磁盘真实采样；新凭据心跳、session/sequence 推进，重装保留同一设备身份 | 通过 |
| 身份与权限 | 重交同一命令不换 PID/凭据；错误指纹/密码/节点 ID/服务器地址不改配置；上报凭据无法管理或连接 WebSocket | 通过 |
| 撤销与秘密 | 撤销后服务退出，MainPID=0、NRestarts=0；token.env 为 0600；SSH 秘密未进入 SQLite、持久存储和报告 | 通过 |
| 真实启动故障 | 立即 exit 2；进程存活但不发就绪通知，30 秒启动超时 | 两种故障均回滚成功 |
| 回滚与再次安装 | 精确恢复 current/config/token；旧凭据重新产生新 session 的上报；后续好包安装成功，只有一个客户端 PID | 通过 |
| WebUI，19 组 | 对话、情感证据与澄清、设备复用/竞争访问、人格 CAS、记忆编辑、日志分页/导出、时间线、导航和响应式布局 | 通过，无页面异常或 HTTP 5xx |
| 人格并发专项 | 挂起旧版 GET，完成新版载入/编辑/保存后释放旧响应；字段、revision 2 和保存提示均保留 | 通过 |
| 浏览器流式专项，5 组 | 首段在数据库 pending 时可见；单次提交；中断后恢复原回执；停止和切换伙伴隔离 | 通过 |
| 实际 HTTP 模型链路 | 本地模型 → 提供商 → API 的真实 JSON/SSE TCP 传输；中文首段在后段暂停时到达；pending 回执、完成与幂等重放 | 通过 |
| 后台自触发 | 实际应用 lifespan 循环自动处理注意力边界，唯一回复；focused/meeting/显式 quiet 不增加模型请求 | 通过 |
| 情感系统 | 实际 HTTP 对话、rules-v2 经历、direction 纠错、过期 CAS 拒绝、幂等与情感静默解除 | 通过 |
| SDK 与分发包 | Kotlin SDK/示例编译；wheel 静态资源与源码一致；脱离源码路径从 wheel 生成同哈希安装包 | 通过 |

Linux 修复后最终包 SHA256：`9e02c0832bd1e6e0dbea289a8aa690e1a9f6135c53800972fc1b1b2093575a7f`。该哈希对应本次验收产物，不代表未来构建版本。

修复后完整后端测试为 **598 passed**，耗时 236.23 秒；包含情感、自触发、设备、记忆、流式、SSH、部署与凭据回归。另先行通过 Linux/凭据/就绪专项 **121 passed**。存在一条既有 Starlette/httpx 弃用警告。Ruff 检查和格式检查通过（90 个 Python 文件），JavaScript 语法及 Prettier 检查通过；Kotlin SDK 与示例构建成功。

## 发现并修复的问题

**启动检查竞态。** 原安装器执行 `systemctl start` 后立即检查 `is-active`，真实故障包快速退出时可能被误判成功；任务最终成为 `node_not_reporting`，没有恢复旧程序。改为 `Type=notify`：客户端完成首次完整上报周期后才发送 `READY=1`，服务启动最多等待 30 秒。立即退出和一直不就绪都会使安装启动失败并触发原有回滚。仍单独校验本次签发凭据的成功心跳。

**人格旧响应覆盖。** 浏览器验收最初在保存提示处超时；定位发现脚本未等载入完成，页面也缺少对同一伙伴的旧载入响应保护。载入/保存期间现会禁用相关控件，使用请求序号丢弃过期响应，保存使早先读取失效。脚本改为等待实际载入完成，并新增受控旧响应延迟回归。未将最初偶发超时直接等同于已复现的产品错误。

## 复现与证据

本地 HTTP 闭环无需外部模型或设备：

```sh
uv run python examples/runtime_live_test.py
uv run --with playwright python examples/webui_smoke.py --help
```

真实 SSH 验收需要自行准备全新的、可丢弃的目标机；脚本会安装服务，不能用于已有业务安装。完整参数见：

```sh
uv run --with playwright python examples/linux_live_test.py --help
uv run python examples/linux_live_fault_test.py --help
```

先运行主流程，保留同一目标机和测试私钥，再运行故障脚本。故障脚本只接受 `127.0.0.1:50922`、主机名 `vl-linux-live-test`，沿用上一轮临时 API origin；默认验证立即退出，`--fault-mode no-ready` 验证启动超时。密码从指定环境变量读取，不放在命令行。

本次非秘密报告与截图保留在：

- `.data/linux-live-review/report.json`、`desktop.png`、`mobile.png`、`environment.json`。
- `.data/linux-live-fault/report-before-fix.json` 为预期失败证据；同目录 `report.json` 为修复后通过结果。
- `.data/linux-live-no-ready/report.json` 为未就绪超时回滚证据。
- `.data/linux-live-review/persona-fixed/report.json` 和 `persona-race-evidence.json` 为完整 WebUI 与人格并发证据。
- `.data/linux-live-review/stream-regression/report.json`、`.data/runtime-live-review/runtime-live-test.json`。
- `.data/linux-live-review/pytest-final.txt`、`wheel-check.json`。

测试虚拟机容器、磁盘、cloud-init seed、SSH 私钥和密码已删除，临时 API 与模型服务已退出，临时 SQLite 已清理。

## 边界

Linux 真实安装只覆盖一个 Ubuntu 24.04 x86_64 虚拟机；没有验证 ARM、其他发行版、NAS 的系统差异、Android 真机或家庭物理设备。模型确定性响应验证的是协议、运行时与交互行为，不能证明真实 LLM 的记忆质量、情感表达或公网首字延迟。SSH 连接在安装中断开、目标掉电等情况仍遵循结果未知的恢复边界，不能据本次启动故障回滚测试承诺所有故障都能自动恢复。
