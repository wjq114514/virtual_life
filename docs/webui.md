# WebUI：实时日志与流程追踪

页面随 FastAPI 在 `/ui/` 提供，首页 `/` 跳转到这里。使用原生 HTML / CSS / JavaScript，不需要 Node 构建、CDN 或另外启动前端。页面用同源 API 的 Bearer token 连接；勾选「记住会话」时，令牌经 `POST /v1/session` 换成服务器签名的 HttpOnly 会话 cookie，页面内存随即不再保留令牌，刷新或重开页面会自动连接（`runtime.webui_session_days` 控制期限，设为 0 关闭记住）。未勾选时令牌只保存在当前页面内存。

## 页面范围

工作台只保留两块面板（参考 AstrBot 的实时日志终端）：

- **实时日志**：尾随 `GET /v1/flow/logs` 的内存环形缓冲。每个到达根日志器的记录（含请求 ID）按序推入，页面每 1.2 秒从上次游标拉取新行，按级别着色（DEBUG/INFO/WARN/ERROR/CRIT），可切换级别过滤、开/关跟随与清空。游标是严格递增序号，旧行被容量淘汰后页面按 `next_sequence` 自动重同步。
- **流程追踪**：尾随 `GET /v1/events` 的运行事件（对话完成/静默/丢弃、迁移准备/提交/中止、节点注册/观察/注意力、伙伴创建/跟随、情绪更新、主动陪伴计划与送达、自动记忆完成、遗忘）。事件按发生时间与序号显示，带伙伴、节点、状态、命令与原因摘要，可暂停/继续与清空。首次连接建立实时基线，不补播历史。

右上角状态点显示「已连接 / 未连接」；断开时点击页面任意处弹出令牌对话框。所有服务端数据都用 `textContent` 渲染，页面不使用 `innerHTML`。视觉资源全部同源，不请求 CDN 或外部字体。

## 后端接口

- `GET /v1/flow/logs?after_sequence=0&limit=200` → `{items, next_sequence, has_more}`。`items` 为 `{sequence, timestamp, level, logger, request_id, message, line}`；`line` 是完整的格式化行，`message` 是纯消息。缓冲默认 2000 条，容量下限 64。
- 环形缓冲由 `virtual_life.log.FlowLogBuffer` 提供，根日志器安装一个 `_FlowLogHandler` 镜像记录；`configure_logging` 与 `reset_logging` 会一并清理该处理器。页面不读取轮转文件。

## 连接与安全

令牌只进入 `Authorization: Bearer` 头，或经 `POST /v1/session` 换成服务器签名的 HttpOnly Cookie（`SameSite=Strict`，不包含令牌本身，轮换 API token 即失效全部会话）。页面不存储或回显模型密钥。`GET /v1/flow/logs` 与 `GET /v1/events` 都经过现有 `authorize` 依赖，只接受管理员令牌或有效会话 cookie。

## 浏览器检查

运行 `uv run --with playwright python examples/webui_smoke.py`；首次使用需执行 `uv run --with playwright playwright install chromium`，也可用 `--browser-executable <路径>` 或 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定现有 Chromium。可用 `--artifacts <目录>` 指定输出。脚本在临时 SQLite 数据库和随机本地端口启动独立服务，不读取真实提供商配置；退出时停止服务并删除临时数据库。截图与检查列表默认保存在 `.data/webui-review/`。该检查验证连接、流式日志尾随、事件尾随、级别过滤、清空、暂停与断开行为，不代表真实模型记忆质量。
