# WebUI：运行工作台

页面随 FastAPI 在 `/ui/` 提供，首页 `/` 跳转到这里。使用原生 HTML / CSS / JavaScript，不需要 Node 构建、CDN 或另外启动前端。

工作台是一个单页外壳：左侧固定导航切换视图，右侧是同一套卡片、表格与图表组件。浏览器的令牌经 `POST /v1/session` 换成服务器签名的 HttpOnly 会话 cookie，页面内存不保留令牌，刷新或重开页面自动连接（`runtime.webui_session_days` 控制期限，设为 0 关闭会话）。没有会话时第一次请求收到 401，页面弹出令牌对话框。

## 视图

七个视图共用顶部工具条：伙伴选择器、自动刷新开关、手动刷新与明暗主题切换。伙伴选择器决定「记忆 / 知识图谱 / 内心活动」三个视图的数据源，选择结果写入 `localStorage`。

- **总览**：伙伴卡片、情绪仪表（valence / arousal / control 三个环形量表）、内心活动摘要、记忆构成条形图、关系慢变量、设备在线数，以及最近 6 条内心活动时间线。数据来自 `GET /v1/companions/{id}`、`/inner-life`、`/emotion`、`/slow-variables`、`/memories/stats`、`/v1/graph`、`/inner-life/activities`。
- **日志**：尾随 `GET /v1/flow/logs` 的内存环形缓冲。每 1.2 秒从游标增量拉取，按级别着色（DEBUG / INFO / WARN / ERROR / CRIT），支持级别开关、全文搜索（消息、logger、request id）、跟随开关与清空。单条日志可展开，显示序号、logger、request id 与完整格式化行。消息本身带语法高亮：引号字符串、`key=value`、UUID、IP:端口、HTTP 状态码、`/v1/...` 路径、错误词与数字分别着色；访问日志（`GET /v1/... 200`）识别为「方法 + 路径 + 状态」三段。页面最多保留 3000 条、DOM 最多 800 行，超出后丢弃最旧的行。
- **事件**：尾随 `GET /v1/events`，最新的在最上面。每条显示序号、事件类型、伙伴/节点、时间；点击展开美化后的 `payload` JSON（键、字符串、数字、布尔分别着色）。
- **记忆**：三个标签页 —— 记录（`GET /v1/companions/{id}/memories`，kind 过滤 + 搜索 + 每页 20 条 + 分页，卡片显示 kind / layer / 生效状态 / pinned / confirmed / 保留策略、importance 与 confidence 条，点击展开完整字段）、摘要（`/memories/summaries`）、诊断（`/memories/logs` 的操作流水表格）。右侧常驻记忆统计与合并计划两张卡片（`/memories/stats`、`/memories/consolidation-plan`）。
- **知识图谱**：`GET /v1/companions/{id}/memories/knowledge-graph`。确定性力导向布局（固定迭代，不引入动画依赖），支持 1/2/3 跳、按实体名检索和重置视图；画布可用指针拖动平移、滚轮缩放（0.4×–2.6×）。点击实体在右侧显示别名、度数、修订号、与之相关的全部关系以及证据记忆（`memory_versions`）。图谱为空时说明它由自动记忆蒸馏产生，需要先跑记忆自动化。
- **内心活动**：`GET /v1/companions/{id}/inner-life` 与 `/inner-life/activities`。展示开关与模型状态、mood / energy / social_battery / salience / urge 指标、五项需求（social / curiosity / concern / reflection / rest）的雷达图与条形图、当前活动原文与思考过程，以及最近 25 条活动时间线；每条活动的 thinking 可单独展开。
- **设备**：`GET /v1/graph`。顶部三张摘要卡（节点数、类型分布、连接可达性），下面是可按节点名 / 类型 / 房间 / 能力搜索的节点表（在线优先排序，最多 400 行），点击一行展开该节点的完整字段与 `context`、`capabilities`、`device`。节点连接不逐条铺开——`/v1/graph` 的边是完全配对矩阵，一个大实例有上万条——可达性折算成每行的「可达 up/total」。

所有服务端数据都用 `textContent` 渲染，页面不使用 `innerHTML`；唯一的例外是把 JSON 解析后按类型包进 `<span>`，键值仍来自 `textContent`。视觉资源全部同源，不请求 CDN 或外部字体。主题在两套 CSS 变量间切换，初始值取自 `localStorage`，未设置时跟随 `prefers-color-scheme`。

## 后端接口

除上面各视图列出的读接口外，页面只调用两个写接口：`POST /v1/session`（把令牌换成会话 cookie）与 `DELETE /v1/session`（等价地清除会话，按钮在左侧导航底部）。

- `GET /v1/flow/logs?after_sequence=0&limit=200` → `{items, next_sequence, has_more}`。`items` 为 `{sequence, timestamp, level, logger, request_id, message, line}`；`line` 是完整的格式化行，`message` 是纯消息。缓冲默认 2000 条，容量下限 64。
- 环形缓冲由 `virtual_life.log.FlowLogBuffer` 提供，根日志器安装一个 `_FlowLogHandler` 镜像记录；`configure_logging` 与 `reset_logging` 会一并清理该处理器。页面不读取轮转文件。
- 游标都是严格递增序号：旧记录被容量淘汰后，页面按 `next_sequence` 自动重同步，不会卡在缺口处。

## 独立打开与内嵌 DSH

- 独立：直接访问 `http://<host>:<port>/ui/`，是整个页面。
- 内嵌：`dsh/virtual-life-studio/` 是 DeepSeek Harness 插件，把同一套数据做成侧边栏面板（伙伴状态 / 日志 / 记忆 / 知识图谱 / 内心活动）。它不复用这里的 HTML，而是用 Host 半侧的只读同源代理 `GET /virtual-life-studio/api/<path>` 访问运行时，因此不需要 CORS，也不需要把运行时的 Bearer 令牌交给浏览器。安装与配置见该目录的 `README.md`。

## 连接与安全

令牌只进入 `Authorization: Bearer` 头，或经 `POST /v1/session` 换成服务器签名的 HttpOnly Cookie（`SameSite=Strict`，不包含令牌本身，轮换 API token 即失效全部会话）。页面不存储或回显模型密钥。所有 `/v1` 接口都经过现有 `authorize` 依赖，只接受管理员令牌或有效会话 cookie。

## 浏览器检查

运行 `uv run --with playwright python examples/webui_smoke.py`；首次使用需执行 `uv run --with playwright playwright install chromium`，也可用 `--browser-executable <路径>` 或 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定现有 Chromium。可用 `--artifacts <目录>` 指定输出。脚本在临时 SQLite 数据库和随机本地端口启动独立服务，先写入一个伙伴与一个节点，再逐视图检查：令牌对话框、连接、七个导航项、日志尾随与展开、级别开关、事件尾随、记忆标签页、知识图谱空态、内心活动卡片、设备搜索、主题切换、伙伴切换与刷新往返。截图与检查列表默认保存在 `.data/webui-review/`。该检查验证页面装配与轮询，不代表真实模型记忆质量。