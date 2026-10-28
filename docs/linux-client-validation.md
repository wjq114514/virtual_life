# Linux 客户端验证记录

对应 [Linux 完整客户端实施计划](linux-client-implementation-plan.md) 阶段 5。本文件记录在隔离环境中的可复现结果，以及仍需真实桌面确认的项。

## 已在本仓库验证

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 全量后端测试 | `uv run pytest -q backend/tests` | 1043 passed（含 `test_dist_api.py` 10 项、`test_dist_configuration.py` 11 项） |
| 客户端测试 | `uv run --project clients/linux-node pytest clients/linux-node/tests` | 142 passed |
| 客户端 lint | `uv run ruff check backend examples clients/linux-node/src clients/linux-node/tests` | clean |
| 客户端格式化 | `uv run ruff format --check backend examples clients/system-node scripts clients/linux-node/src clients/linux-node/tests clients/linux-node/packaging` | 全部已格式化 |
| 契约导出 | `uv run python examples/export_openapi.py` | docs/openapi.json 已更新（101 paths，sha256 `7ac96fae3c9d19de90bb2fb11b09a7407e283cf38f70912d52b2587a3923331b`，重导出无漂移） |
| 分发端点 | `uv run pytest backend/tests/test_dist_api.py -q` | 10 passed：401 鉴权、空清单 404、无密钥 503、上传/下载往返与 sha256、版本升序与 default_version、错误密钥 403、非法文件名 400、包数 409、字节 413、预发布排序 |
| 分发链路冒烟 | 起带 `dist_upload_secret` 的后端 → curl 上传 wheel → 客户端拉取 | 无 token 401、错密钥 403、上传 201，manifest sha256 与本地构建一致，`dist_client` 下载后本地重算 sha256 匹配 |
| `vl setup --dry-run` | 隔离 XDG 目录 + `--server` 指向真实后端 | 拉到清单并选包、校验通过；打印全部计划写入，磁盘零落盘 |
| GitHub 清单 | 本地假 GitHub API（`--github-api`）返回 4 个 release，覆盖四种结果 | `latest` 跳过无校验和的 0.3.0 选 0.2.0；`--version 0.1.9` 可显式选预发布；0.3.0 报 `no checksum was published`；0.4.0（校验和写错）报 `sha256 mismatch` 且不安装；未知仓库报 `no such repository` |
| `vl setup --github` 真安装 | 假 GitHub 托管真实构建产物，`UV_TOOL_DIR`/XDG 全隔离 | 拉取 → sha256 验证通过 → `uv tool install` 装好 18 个依赖并生成 `vl` 入口；`config.toml` 写出且不含 token；credentials 因未提供而跳过 |
| 来源优先级 | `--wheel` 与 `--github` 同时给出 | `--wheel` 生效，`--github` 被忽略 |
| 真实 GitHub API | `github_manifest("wjq114514/virtual_life")`（该仓库当时 0 release） | 优雅返回空清单，`select` 报 `No client packages are published yet`；不存在仓库报 `no such repository` |
| 临时目录清理 | 三种失败路径（未知仓库 / 缺校验和 / 未知版本）+ 成功路径 | 下载的 wheel 临时目录全部清理，无残留 |
| 零发布安装 | `uv tool install "git+https://github.com/wjq114514/virtual_life#subdirectory=clients/linux-node"` | 从远端 HEAD 直接构建安装成功 |
| 安装写盘 | `installer` 直调，隔离 config/data root | config.toml 可重新加载、credentials 0600 含真实 token、`.device-id` 0600、6 类资产 + systemd + desktop + 2 个 DMS 文件就位 |
| 构建 | `uv run --project clients/linux-node python clients/linux-node/packaging/build.py` | wheel/sdist + sha256，含资产校验 |
| 客户端↔服务端字段联调 | 客户端 `ServerTransport` 绑定后端 ASGI app | capabilities/注册/心跳/presentation 开-心跳-释放字段一致 |
| 分发包冒烟 | 干净 venv 装 wheel + `vl --help` / `vl doctor` | entry point 正常，doctor 输出紧凑摘要并回显 runtime_dir |
| 安装脚本 | `packaging/install.py --dry-run --skip-package`（XDG 目录指向沙箱） | 7 个资产 + config.toml + 0600 credentials 占位 |
| 真实 daemon 冒烟 | 沙箱 XDG 环境跑 `vl daemon run` + `vl doctor` | socket 落在 `$XDG_RUNTIME_DIR/virtual-life/<server_key>/<node_id>/`，doctor 读到 daemon 与 attention session |

当前产物（`packaging/build.py` 同时生成 `dist/SHA256SUMS`，供 GitHub release 上传）：

- `virtual_life_linux-0.1.0-py3-none-any.whl` sha256 `31eff6fa97173eb61b4809dc27712c6bda9e0eca005f2e3d18d32f9a8eb8e8c0`
- `virtual_life_linux-0.1.0.tar.gz` sha256 `135eceecd576aed4be19381550141c3637bcf03d8972abd27d69331356b46b8e`

sdist 含时间戳，每次构建都会变；**wheel 是确定性的**，发布与校验以 wheel 为准。

客户端测试当前为 142 passed。实时联调用例（需运行中的服务端）见 `examples/linux_desktop_live_test.py`。wheel 校验项包括 `installer.py`、`dist_client.py`、`integrations/` 下 6 类资产与 `assets/config.example.toml`，缺任一项 `build.py` 直接失败。安装逻辑在 `virtual_life_linux.installer` 内（源码树与 wheel 共用），`packaging/install.py` 只是薄壳。

分发有两条互相独立的来源，优先级 `--wheel` > `--github` > `--dist-url` > `--server`。服务端来源读 `/v1/dist/manifest.json`，校验和来自清单内嵌字段；GitHub 来源读 `GET /repos/{owner}/{repo}/releases`，校验和按优先级取自同一 release 的校验和资产（`SHA256SUMS` / `sha256sums.txt` / `checksums.txt` / `sums` / `sha256.txt` / `*.sha256`）或资产 `digest`。没有校验和就无法验证下载，因此 fail-closed：单个 wheel 缺校验和时不会成为 `latest`，但仍在清单中列出以便显式选中，并在下载前被拒绝安装；整个 release 一个校验和都没有则直接报错要求补发。`DistPackage.download_url` 只由 GitHub 适配器填充，服务端 manifest 从不解析该键，因此服务端无法把下载与 runtime token 重定向到第三方主机；GitHub token 只发往 `--github-api` 指向的主机。

值得记录的两个边界：`latest` 只能在**不需要下载**的前提下做筛选，所以它跳过的是缺校验和的包；校验和写错与校验和写对在下载前无法区分，此时 `latest` 仍指向该版本，在下载后以 `sha256 mismatch` 失败——这是正确行为，静默回退到旧版本反而可能掩盖真实的供应链问题。另一个是 64 个 `0` 是合法 hex，同样被当成「已发布校验和」对待。

客户端测试覆盖：

- `test_config.py`：默认值、未知字段拒绝、device_id 回退、server key 隔离、URL 规范化、凭据目录约束、0600 权限、reporting 边界、runtime 目录按 server key + node_id 隔离、无 `XDG_RUNTIME_DIR` 报错、未隔离的回退路径。
- `test_lease.py`：请求起始锚定、TTL 上限、本地上限、剩余不足拒绝、过期拒绝、严格不等式。
- `test_storage.py`：身份一次性固化、cursor、命令恢复/结算、去重。
- `test_transport.py`：注册载荷与鉴权头、409/401 错误映射、legacy 服务器特性缺失、SSE 解析。
- `test_ipc.py`：调用往返、未知方法、订阅事件、socket 0600。
- `test_daemon.py`：能力投影、presentation 开启/释放/续期、过期后移除 `text.display`、generation 替换拒绝、companion 异地主机拒绝、对话回执、无 renderer/租约过期拒绝、webui 只暴露配置 origin、注意力样本去重与序号持久化、边界即时发送与失败入队、样本字段约束、注意力关闭分支、状态与诊断中的注意力信息、伙伴列表自动选中、attach 绑定真实 epoch、attach 拒绝异地伙伴。
- `test_cli.py`：参数解析、doctor 紧凑摘要（含 runtime_dir 与错误分支）、TUI 在非事件循环中分发、配置解析按路径缓存与回退、退出码映射。
- `test_tui.py`：`run()` 从配置解析 socket 路径（server-key/node 隔离）、缺少配置时报 `ConfigError`。
- `test_render_guard.py`：当前 generation、截止时间、无授权、重绑定。
- `test_state.py`：注意力覆盖 TTL 与失效、状态/上下文构建。
- `test_adapters.py`：niri workspace 解析不含名称、logind 属性有界、适配器异常隔离。
- `test_installer.py`：资产收集、不覆盖既有文件（需 `--clobber`）、跳过 systemd 单元、逐文件状态与 dry-run 不落盘、credentials 0600、既有凭据保留、`.device-id` 持久化与可预览不写盘、渲染出的 config 可被 `load_config` 加载、Python 版本门槛。
- `test_dist_client.py`：URL 构造（含 `/v1` 后缀归一与 `file://`）、鉴权失败、清单解析与排序、`latest`/精确/空/未知版本选择、sha256 校验通过、哈希不匹配时删除临时文件、file 源无需 token、空清单显式报错；GitHub 侧：仓库引用归一（URL/SSH/`.git`/片段/非法值）、服务端 manifest 无法注入 `download_url`、release 到清单的映射（版本取自 wheel 文件名、tag 可选 `v` 前缀、升序、`released_at`/`size_bytes`/`browser_download_url`）、校验和资产解析与 `digest` 回退、`latest` 跳过缺校验和的包、无校验和时 fail-closed、draft 与未知仓库、token 被拒、用资产 URL 下载并验证、篡改的 wheel 被拒。

## 待真实设备确认

以下需要目标 Niri/DMS/kitty/fish 环境，尚未验证（由用户在真实桌面上执行，仓库内不模拟）：

- presentation 与 Web access 在真实设备上的互斥与 `presentation_busy` 提示。
- 冻结 renderer 在租约到期后停止绘制（本地 deadline 与守护进程双侧）。
- 迁移 target_ready 绑定 presentation，以及提交前释放导致 `target_presentation_invalid` 中止。
- niri 焦点输出/workspace 采集与 DMS 桥接在真实 shell 上的可用性（DMS QML widget 待固定版本后接入）。
- 注意力样本在真实 logind/niri 上的取值（idle/locked 推导 availability），以及服务端 `attention_stale`/`attention_out_of_order` 是否会真实出现。
- fish 钩子只发送不透明令牌、不泄露命令与参数。
- legacy 服务器上遥测可用、完整聊天返回退出码 7。
- `vl setup` 在干净用户会话中的 `uv tool install`/`pip install --user`、systemd 用户服务启用，以及更新已安装的客户端后原进程仍为旧版本这一点是否符合预期。
- `vl setup --github` 对**真实** GitHub release 的端到端安装（本地已用假 GitHub API 覆盖清单解析、校验和选取、fail-closed 与下载验证全部分支；真实 release 尚未发布，见下）。

## 未决

- GitHub release 尚未发布：`wjq114514/virtual_life` 当前 0 个 release、0 个 tag，`vl setup --github` 这条路上线前需要先 `git push` 本轮改动并发布 release（`gh release create v0.1.0 <wheel> SHA256SUMS`）。发布动作会修改远端仓库，留给用户执行。
- DMS widget 需要固定 Quickshell 版本与真实冒烟后再启用 `entry`。
- 阶段 6 的 desktop credential 权限收敛尚未开始。
