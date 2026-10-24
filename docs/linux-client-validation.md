# Linux 客户端验证记录

对应 [Linux 完整客户端实施计划](linux-client-implementation-plan.md) 阶段 5。本文件记录在隔离环境中的可复现结果，以及仍需真实桌面确认的项。

## 已在本仓库验证

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 全量后端测试 | `uv run pytest -q backend/tests` | 1022 passed |
| 客户端测试 | `uv run --project clients/linux-node pytest clients/linux-node/tests` | 113 passed |
| 客户端 lint | `uv run ruff check backend examples clients/linux-node/src clients/linux-node/tests` | clean |
| 客户端格式化 | `uv run ruff format --check backend examples clients/linux-node/src clients/linux-node/tests` | 195 files already formatted |
| 契约导出 | `uv run python examples/export_openapi.py` | docs/openapi.json 已更新（98 paths，sha256 `d30e82c99464ec5c76a33e8820eae0208769f3880ce5cfa26fb30938911d432a`，重导出无漂移） |
| 构建 | `uv run --project clients/linux-node python clients/linux-node/packaging/build.py` | wheel/sdist + sha256，含资产校验 |
| 客户端↔服务端字段联调 | 客户端 `ServerTransport` 绑定后端 ASGI app | capabilities/注册/心跳/presentation 开-心跳-释放字段一致 |
| 分发包冒烟 | 干净 venv 装 wheel + `vl --help` / `vl doctor` | entry point 正常，doctor 输出紧凑摘要并回显 runtime_dir |
| 安装脚本 | `packaging/install.py --dry-run --skip-package`（XDG 目录指向沙箱） | 7 个资产 + config.toml + 0600 credentials 占位 |
| 真实 daemon 冒烟 | 沙箱 XDG 环境跑 `vl daemon run` + `vl doctor` | socket 落在 `$XDG_RUNTIME_DIR/virtual-life/<server_key>/<node_id>/`，doctor 读到 daemon 与 attention session |

当前产物：

- `virtual_life_linux-0.1.0-py3-none-any.whl` sha256 `fa19a99a2765faec80bcc7737548f4221ebb9a9781f8b30d4adedbba081d5097`
- `virtual_life_linux-0.1.0.tar.gz` sha256 `565f00528f76ed1695288f8237775700494b2332ae136b9c0f8bd2c8bb878739`

客户端测试当前为 113 passed。实时联调用例（需运行中的服务端）见 `examples/linux_desktop_live_test.py`。wheel 校验项包括 `integrations/` 下 6 类资产与 `assets/config.example.toml`，缺任一项 `build.py` 直接失败。

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

## 待真实设备确认

以下需要目标 Niri/DMS/kitty/fish 环境，尚未验证（由用户在真实桌面上执行，仓库内不模拟）：

- presentation 与 Web access 在真实设备上的互斥与 `presentation_busy` 提示。
- 冻结 renderer 在租约到期后停止绘制（本地 deadline 与守护进程双侧）。
- 迁移 target_ready 绑定 presentation，以及提交前释放导致 `target_presentation_invalid` 中止。
- niri 焦点输出/workspace 采集与 DMS 桥接在真实 shell 上的可用性（DMS QML widget 待固定版本后接入）。
- 注意力样本在真实 logind/niri 上的取值（idle/locked 推导 availability），以及服务端 `attention_stale`/`attention_out_of_order` 是否会真实出现。
- fish 钩子只发送不透明令牌、不泄露命令与参数。
- legacy 服务器上遥测可用、完整聊天返回退出码 7。
- `packaging/install.py` 在干净用户会话中的 systemd 用户服务安装。

## 未决

- DMS widget 需要固定 Quickshell 版本与真实冒烟后再启用 `entry`。
- 阶段 6 的 desktop credential 权限收敛尚未开始。
