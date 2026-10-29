# Virtual Life · 节点式 AI 陪伴运行时

设备是 AI 可以感知和穿梭的节点；陪伴者携带持续状态在呈现节点之间接力，共同记忆统一持久化。安卓、电脑提供陪伴入口，服务器和家庭设备通过轻客户端或网关报告状态。

接口包含节点注册与能力图、共读上下文、迁移握手、输出租约、事件回放、有召回预算的长期记忆，以及跨设备共享的人格与短期状态。WebUI 提供文本对话与记忆查看。

**推理与运行边界**：模型调用已全部移交 [DSH](#dsh-插件)（外部生成，`generator: external` 在派发闸口挂起、由 DSH 提交回复）；Virtual Life 本身只维护状态机（节点/房间、迁移、租约、输出 gate）、长期记忆、人格与内心活动（inner_life），并保留本地提示预览、记忆写入与召回。没有配置本地 provider 时 VL 降级为纯数据面：`external` 仍返回 `awaiting_generation`，`internal` 返回 `503 model_not_configured`。

## 设计理念

每个设备是一个节点，节点相当于一个房间。生命体不是被创建出来等待召唤的，而是随着主人的步伐迁徙——主人走到哪个房间，她就出现在哪里。屏幕、语音、环境信号是房间自带的家具，让陪伴在具体的空间里发生，而不是悬浮于抽象的云端。

同一份共同记忆、同一份人格、同一份短期状态，在不同房间之间接力传递。她在厨房听见你的说话，在书房看见你读的段落，在卧室的手机上接住你睡着前的最后一句问候——房间换了她没换。

当前项目仍处于开发框架阶段，尚未形成一个可直接使用的成品。节点、能力、记忆、人格、语音、屏幕视觉这些子系统各自已具备可用原型，但完整的用户体验、真实的设备联调、稳定的人格表现和有温度的交互节奏还需要逐步打磨。

## 启动

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。在项目根目录执行：

```bash
uv sync
cp virtual-life.example.toml virtual-life.toml
uv run python -m virtual_life
```

浏览器打开 [WebUI](http://127.0.0.1:8000/ui/)，输入默认开发 token `local-dev-token` 连接。也可打开 [接口文档](http://127.0.0.1:8000/docs)，点 Authorize。API 使用 `Authorization: Bearer <token>`。

数据库默认保存到 `.data/runtime.sqlite3`。统一配置见 [virtual-life.example.toml](virtual-life.example.toml)，集中设置监听、运行时、模型、自动记忆、召回与额度、人格提示预算。修改 `virtual-life.toml` 后重启；环境变量覆盖文件，完整规则见 [配置说明](docs/configuration.md)。也可用 `--config /path/to/config.toml` 指定文件。

日志默认同时写到控制台和 `.data/logs/virtual-life.log`，文件按大小轮转；级别、路径和保留数量在 `[logging]` 中配置。每行包含时间、短级别、模块与行号，HTTP 请求还带状态、耗时和 `X-Request-ID`，便于把一次页面操作和后台任务串起来。控制台在终端自动着色，`NO_COLOR=1` 可关闭。详见[日志配置](docs/configuration.md#日志配置)。

已有 [.env.example](.env.example) 方式仍可用，复制成 `.env` 后通过 Uvicorn 加载：

```bash
uv run uvicorn virtual_life.main:app --env-file .env --host 127.0.0.1 --port 8000
```

## WebUI 实时日志与流程追踪

页面使用原生 HTML / CSS / JavaScript，随 API 提供，无需另装前端依赖。连接后只显示两块面板：左侧**实时日志**尾随 `GET /v1/flow/logs` 的内存环形缓冲（级别过滤、跟随、清空），右侧**流程追踪**尾随 `GET /v1/events` 的运行事件（对话、迁移、节点、记忆、主动陪伴；暂停/继续、清空）。令牌经 Bearer 头或 `POST /v1/session` 换成 HttpOnly 会话 Cookie。合同与检查见 [WebUI](docs/webui.md)。

后台自触发已接入两类来源：宿主通过类型化注意力接口上报任务完成、开始休息或用户返回；以及 `inner_life` 根据既有情绪、慢变量、对话历史与空闲时间生成的短时心理活动。两者都要显式启用、chat 模式、参与条件与预算允许，且生成和呈现时复核上下文、epoch 与租约；专注、会议、静默、未知或过期信号默认不触发。主动文字只向仍有输出权的呈现端展示，不伪造用户发言或补播过时问候；其事件与决策可在流程追踪面板查看。Linux 已支持可选的[桌面状态自动采集](docs/linux-attention-collection.md)，会议、共享与专注识别仍保留未知。详见[注意力与自触发](docs/proactivity.md)、[内在活动](docs/inner-life.md)及[后台保护验证](docs/background-flow-validation.md)。

实际对话需在 `virtual-life.toml` 的 `[model]` 中配置兼容 Chat Completions 的提供商，再重启服务。例如 [DeepSeek 官方接口](https://api-docs.deepseek.com/)：

```toml
[model]
base_url = "https://api.deepseek.com"
name = "deepseek-flash"
api_key = "填写自己的密钥"
extra_body = { thinking = { type = "disabled" } }
```

模型密钥只由服务器读取，页面连接 token 只存当前页面内存，或经 `POST /v1/session` 换成服务器签名的 HttpOnly 会话 Cookie（脚本读不到、不随跨站请求发送、不包含令牌本身）。未配置模型不影响日志与事件查看。详见[设备身份与访问](docs/device-identity.md)与 [WebUI 合同](docs/webui.md)。

> **DSH 模式**：推荐把 `generator: "external"`（DSH 通过 MCP 桥或 `commit` 路由提交回复）作为主路径，本地 `[model]` 仅用于提示预览、记忆写入/召回和演示；`generator: "internal"` 保留为本地回退。DSH 侧消费 [MCP 桥](docs/mcp-bridge.md) 或 [DSH 插件](#dsh-插件) 完成生成与提交。

## 连续语音与服务端视觉

原生宿主在设备上采音、进行流式 ASR / VAD 和轮次判断，只提交冻结文本；服务端复用对话与预算流程，分句合成 TTS，客户端下载并播放。截图由设备采集、服务端理解。停止或插话先在本地清空播放，再持久撤销旧输出代际，已发生的模型用量仍结算。

通过 `[voice]` 开启会话，通过 `[speech]` 单独配置本地 Kokoro worker；模型需显式安装。使用 [Linux 客户端](clients/linux-node/README.md) 或 [Android 主客户端](clients/android-app/README.md)。接口见 [语音合同](docs/voice-contract.md)，安装、实际测试和硬件边界见 [实施报告](docs/realtime-voice-implementation-report.md)。隔离演练不会访问正在运行的服务或现有伙伴数据库：

```bash
uv run python examples/voice_live_test.py
```

默认使用 fake LLM 和静音 PCM，只验证 HTTP / SSE / 幂等 / 下载 / 撤销闭环。传 `--tts-config /path/to/config.toml` 可使用真实 TTS，LLM 仍为固定测试回复；这不替代麦克风和扬声器验收。

## 运行节点接力示例

服务运行时，在另一个终端执行：

```bash
uv run python examples/two_nodes.py
```

示例注册电脑和安卓模拟节点，创建陪伴者，保存共读位置与共同经历，完成电脑 → 安卓 → 电脑迁移，并检查活动、话题与记忆引用连续性。每次使用独立 ID，可以重复运行。它是协议示例，不会在手机上创建悬浮窗口。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /v1/nodes` | 注册节点与可用能力 |
| `POST /v1/nodes/{id}/heartbeat` | 更新在线状态与宿主环境信号 |
| `GET /v1/graph` | 查看节点与可迁移的连边 |
| `POST /v1/companions` | 创建持续存在的陪伴者 |
| `POST /v1/companions/{id}/context` | 保存共读活动和相处上下文 |
| `POST /v1/companions/{id}/transfers` | 准备迁移 |
| `POST /v1/transfers/{id}/ack` | 目的 ready / 来源 released |
| `POST /v1/companions/{id}/lease` | 续期节点输出权 |
| `GET /v1/events` | 按持久 cursor 回放事件 |
| `WS /v1/nodes/{id}/events` | 实时接收与恢复节点事件流 |
| `POST /v1/companions/{id}/memories` | 保存有来源的共同经历或偏好 |
| `POST /v1/companions/{id}/memories/{memory_id}/corrections` | 按版本显式更正并保留历史 |
| `PATCH /v1/companions/{id}/memories/{memory_id}` | 按版本调整标签、置信度与固定/确认状态 |
| `GET /v1/companions/{id}/memories` | 分页查看有效与历史记忆 |
| `GET /v1/companions/{id}/memories/stats` | 查看存储统计与共享 token 额度 |
| `POST /v1/companions/{id}/memories/recall` | 按条数和估算 token 预算召回 |
| `GET /v1/companions/{id}/memories/automation` | 自动整理配置、队列进度和额度 |
| `GET /v1/companions/{id}/memories/automation/jobs` | 整理回执、错误及待确认冲突 |
| `POST /v1/companions/{id}/memories/automation/run` | 提前整理一批待处理对话 |
| `GET /v1/companions/{id}/memories/summaries` | 查看有效记忆摘要 |
| `GET /v1/companions/{id}/memories/knowledge-graph` | 查询有来源支持的实体关系 |
| `PUT /v1/companions/{id}/memories/knowledge-graph/entities/{entity_id}/aliases` | 按版本设置实体别名 |
| `GET /v1/companions/{id}/memories/embeddings` | 查看向量索引进度与额度 |
| `GET /v1/companions/{id}/memories/embeddings/jobs` | 查看向量模型任务及用量 |
| `POST /v1/companions/{id}/memories/embeddings/run` | 索引一批可处理记忆 |
| `POST /v1/companions/{id}/memories/semantic-recall` | 使用向量模型进行语义融合召回 |
| `GET / POST /v1/companions/{id}/personality` | 读取或显式编辑稳定人格 |
| `POST /v1/companions/{id}/personality/import` | 导入酒馆 V2 JSON 子集并保留原卡 |
| `GET / POST /v1/companions/{id}/personality/state` | 读取或更新有有效期的短期状态 |
| `GET /v1/companions/{id}/emotion` | 读取随时间恢复的共享情感状态 |
| `GET /v1/companions/{id}/inner-life` | 读取本地生成的心理活动、需求与主动冲动 |
| `GET /v1/companions/{id}/inner-life/activities` | 分页查看心理活动历史 |
| `GET /v1/companions/{id}/emotion/experiences` | 分页查看反应、共鸣与来源 |
| `POST /v1/companions/{id}/emotion/experiences/{event_id}/invalidate` | 澄清一次反应并撤回其影响 |
| `POST /v1/companions/{id}/emotion/experiences/{event_id}/correct` | 按方向、强度或归因调整理解，保留原始依据 |
| `POST /v1/companions/{id}/personality/preview` | 审查预算内提示与参与建议 |
| `POST /v1/companions/{id}/dialogue` | 在当前节点执行一次有预算的文本对话 |
| `POST /v1/companions/{id}/dialogue/stream` | 流式临时回复与最终持久回执，沿用相同命令合同 |
| `GET /v1/companions/{id}/dialogue/{command_id}` | 查询原命令的持久回执 |
| `GET /v1/companions/{id}/memories/consolidation-plan` | 查看分批记忆整理计划 |
| `POST /v1/nodes/{id}/observations` | 接入阅读、屏幕或用户发起的视觉观察 |
| `POST /v1/nodes/{id}/screen/sessions` | 显式创建屏幕视觉会话 |
| `GET /v1/nodes/{id}/screen/sessions/{session_id}` | 读取会话状态与最新帧/分析指针 |
| `POST /v1/nodes/{id}/screen/sessions/{session_id}/stop` | 停止会话 |
| `POST /v1/nodes/{id}/screen/sessions/{session_id}/purge` | 清除会话内容与文件 |
| `POST /v1/nodes/{id}/screen/uploads` | 注册一次截图上传 |
| `PUT /v1/nodes/{id}/screen/uploads/{upload_id}/content` | 以原始二进制写入截图 |
| `POST /v1/nodes/{id}/screen/frames` | 提交图片/reference/unavailable 帧 |
| `GET /v1/nodes/{id}/screen/frames/{frame_id}` | 查询帧处理进度 |
| `GET /v1/nodes/{id}/screen/analyses/{analysis_id}` | 读取有界结构化分析 |
| `GET /v1/nodes/{id}/screen/state` | 读取当前可观察状态 |
| `POST /v1/nodes/access/web` | 将 Web 访问关联到设备，返回已有或新建设备节点 |
| `POST /v1/nodes/{id}/access/{access_id}/heartbeat` | 刷新本页访问，不覆盖原生设备状态 |
| `DELETE /v1/nodes/{id}/access/{access_id}` | 关闭页面访问，保留设备 |

完整格式、错误与 WebSocket 帧见 [节点协议](docs/protocol.md)。OpenAPI 可通过 `/openapi.json` 获取，仓库内也提供 [导出的接口定义](docs/openapi.json)。重新生成：

```bash
uv run python examples/export_openapi.py
```

## 屏幕视觉

服务端屏幕视觉由显式用户会话驱动：节点声明 `vision.capture` 能力，创建会话后上传截图、提交帧，后台 worker 归一化、重验像素精确相同并调用独立的兼容 Chat Completions 视觉模型，产出有界结构化分析与可观察状态。视觉结论是独立证据，不自动触发发言、主动参与或长期记忆，也不授予输出权。首版 `subject_node_id` 必须等于 `observer_node_id`，服务端 OCR 与近似重复复用未实现。

默认关闭，且仅管理员 token 可用；reporter 凭据在鉴权层对 `/screen` 路径直接 403。启用需在 `[screen_vision]` 配置独立视觉 provider（`base_url`/`name`/可选 `api_key`），视觉子配额 `daily_model_call_limit`/`daily_vision_token_limit` 默认 0（不限制），用量记入 `screen_vision_usage` 供统计。协议、错误码、配置与未验证项见[屏幕视觉](docs/screen-vision.md)，算法与验收规划见[服务端实施规划](docs/screen-vision-server-plan.md)。运行时可用 `uv run python examples/screen_vision.py` 走通上传 → 帧 → 分析 → 状态 → 停止/清除闭环。

## 安卓接入边界

[Kotlin 节点 SDK](clients/android-node/README.md) 承接协议类型、HTTP / WebSocket / SSE、迁移确认、语音下载与输出 gate，继续保持 JVM8、不依赖 `android.*`。[Android 主客户端](clients/android-app/README.md) 承接设备权限、服务生命周期、本地语音识别、播放与服务端截图上传；构建和真机验证分别记录在其说明与实施报告中。

[屏幕视觉 Demo](clients/android-vision-demo/README.md) 是独立 Android APK 原型：使用 Shizuku 低频取屏、变化筛选和本机 MNN 视觉模型，提供流式结果与阶段耗时日志。它不接入节点迁移或自动操作；真机验收与限制见该目录说明。

在项目根目录构建 SDK 与连接示例：

```bash
clients/android-node/build-sdk.sh assemble exampleClasses
```

宿主必须在停止输出后确认 released，在呈现能力准备好后确认 ready。输出动作需要同时通过节点、epoch、租约期限校验，断线后按保存的 cursor 恢复事件。

## 通用设备轻客户端

Linux 现已支持从 WebUI 连接配置或设备区域，通过 SSH 密码或私钥一键分发：核对主机指纹后检查环境、安装 systemd 服务并验证心跳。要求目标已有 Python 3.12+、systemd，以及 root 或无密码 sudo；升级保留设备身份，只分发独立的自身上报凭据。也可下载源码/`.pyz` 安装包手动部署。见 [Linux 安装与 SSH 分发](docs/linux-deployment.md)。

[通用客户端](clients/system-node/README.md) 使用 Python 3.12+ 标准库，可单独放到服务器或家庭网关运行。直接模式采集本地 CPU、内存、负载、运行时间与磁盘；网关模式为配置的 TCP / HTTP 目标报告可达性。各设备使用稳定节点 ID，主服务器保持生命体的统一状态。

在主服务器运行后，设置相同的开发 Token 并启动一个直接节点：

```bash
export VIRTUAL_LIFE_API_TOKEN=local-dev-token
python3 clients/system-node/node_client.py --config clients/system-node/examples/local.json
```

网关配置见 `clients/system-node/examples/gateway.json`，其中目标地址需要改成自己的设备端点。`GET /v1/nodes` 的 `context.device_state` 包含最新状态，显著类别变化通过已有 observations / 事件流传递。可选操作元数据表达“从电脑维护某台服务器”，不改变生命体的当前呈现节点。

上报链路在线、设备可达、人的操作和角色呈现分别有明确字段。网关收到 HTTP 响应只能说明端点可达；未知指标保留为 `null`。采样默认每 20 秒一次，不调用模型、不自动写长期记忆。协议、来源、新鲜度和扩展路线见 [设备状态跟踪](docs/device-state-tracking.md)。

[QQ 节点桥](clients/qq-node/README.md) 把 QQ（OneBot v11 反向 WebSocket）接入为另一个房间：私聊消息映射为对话轮次，「正在输入 / 在线」映射为 attention 样本（`user_present` + `activity=chat`），伙伴回复通过 `send_private_msg` 回发。运行（stdlib-only，Python 3.12+）：

```bash
VIRTUAL_LIFE_API_TOKEN=local-dev-token \
python3 clients/qq-node/qq_node.py --config clients/qq-node/example.json
```

事件映射、配置字段与单测命令见该目录 README。

[Matrix 节点桥](clients/matrix-node/README.md) 把 Matrix（经任一 homeserver）接入为另一个房间：房间消息映射为对话轮次，用户在场/输入映射为 attention 样本（`user_present` + `activity=chat`），伙伴回复通过 `m.room.message` 回发。默认 stdlib-only 子集（密码/Token 认证、sync 长轮询、`m.text` 提取、发送、加入房间）；`e2ee: true` 时启用可选 Node 边车（`matrix-js-sdk` RustCrypto）做 Olm/Megolm 加解密。运行（Python 3.12+）：

```bash
VIRTUAL_LIFE_API_TOKEN=local-dev-token \
python3 clients/matrix-node/matrix_node.py --config clients/matrix-node/example.json
```

E2EE 需先 `cd clients/matrix-node/e2ee && npm install`；缺 Node 或边车无法启动时以错误码 3 退出。事件映射与配置字段见该目录 README。

## DSH 插件

模型推理由 DSH（DeepSeek Harness）承担，VL 提供数据面与状态机。DSH 侧以三种方式接入：

- **`virtual-life-mcp`**：契约驱动 MCP 桥（`mcp_bridge.py` 启动时读运行时 OpenAPI，把每个 operation 暴露为 MCP 工具；streamable HTTP + stdio，JSON-RPC 2.0 over httpx，真鉴权/校验/幂等）。详见 [MCP 桥](docs/mcp-bridge.md)。
- **`virtual-life-persona`**：把人格 / 状态 / 慢变量快照注入为 labeled context（`[VIRTUAL_LIFE_COMPANION]` / `[VIRTUAL_LIFE_STATE]` / `[VIRTUAL_LIFE_SLOW_VARIABLES]`），作为数据而非指令。
- **`virtual-life-proactivity`**：轮询主动输出通道（`/proactivity/output`、`/proactivity/native-output`），每条 ready 消息经 `ctx.schedule` 成独立 turn；回复经 `POST /dialogue/{command_id}/commit` 提交，运行时把候选解析为 `completed`。
- **`virtual-life-innerlife`**：轮询内心活动通道（`/inner-life`、`/inner-life/activities`），缓存最新活动与 `thinking`（模型模式捕获的 reasoning 内心独白），注册 labeled context 块并暴露查看/翻页工具。

插件定位、迁移分析与融合规划见 [DSH 集成](docs/dsh-integration.md) 与 [融合计划](docs/dsh-fusion-plan.md)。

## 长期记忆

当前保存工作上下文、共同经历、稳定认识、派生摘要与知识图谱。完成对话默认自动排队，在同一伙伴积累 4 轮或最旧事件等待 60 秒后批量整理；用户明确陈述形成未经确认的事实、偏好和关系，助手文字作为对话行为归档。手动记录、手动编辑及固定/确认内容受保护。记忆页可查看进度、摘要、关系及提前整理。

原始记忆、摘要和图谱共用来源与版本检查，删除、更正或过期后剔除相应过时派生内容。普通召回融合本地词法、有界图谱及已有查询向量缓存，默认 800 token / 8 项，读取不调用模型也不重排。自动整理、对话、非本地向量索引和付费语义查询共享每日 10000、单任务 5000 token 额度，未知执行保留预留；`embedding.local=true` 的本地 TEI 只记录 usage，不占付费日额度。DELETE 保存来源墓碑阻止后台再生，历史事件、整理证据及回执仍保留，完整遗忘尚未实现。

自动整理复用当前配置模型，需要 JSON object 响应支持；Linux/POSIX 后端使用文件锁协调执行和中断恢复。统一配置 `[automatic_memory]` 设置开关、批次、等待时间和输出预算，`[memory]` 设置共享额度；环境变量仍可覆盖。关闭开关后仍可显式保存与读取已有记忆。配置、参考插件与限制见 [自动记忆、摘要与图谱](docs/automatic-memory.md)，基础合同见 [长期记忆设计](docs/memory.md)。

`[embedding]` 可启用独立向量模型，自动索引已有和新增有效记忆，并与词法、摘要、图谱融合召回。纯 CPU 部署可接本地 TEI 并用 `local=true` 跳过付费日额度；`[rerank]` 可选、默认关闭，只作用于显式语义召回，超时即回退，不影响对话延迟。实际对话可准备查询向量，普通预览仍不调用模型。图谱支持规范实体、显式别名、独立来源聚合和有界多跳检索；记忆日志、自动整理与图谱相关事件显示在流程追踪面板。向量模型默认关闭，CPU 模型建议、启用与费用规则见 [向量记忆与图谱](docs/vector-memory-and-graph.md)。ANN 仍在后续阶段，失效旧摘要自动重建已提供。

## 基础人格与陪伴方式

人格绑定 `companion_id`，包括描述、性格、价值取向、表达风格与行为偏向；设备切换后读取同一份人格。精力、社交精力与情绪独立保存，默认 30 分钟过期，过期后恢复有效默认状态并保留原记录。人格与状态各有 revision、幂等命令和事件通知，编辑不改变迁移 epoch。

参考 SillyTavern 的角色卡与提示组合，当前支持 V2 JSON 基础字段映射和完整来源归档。示例对话塑造表达风格；共同经历来自实际记忆。卡片中的提示覆盖、lorebook 与扩展保留为来源。范围和字段限制见 [人格设计与接口](docs/personality.md)。

参与规则让 quiet / focus 的非主动互动保持安静；用户主动交谈可以回应，chat 的主动参与则受人格偏向和当前精力约束。预览按服务端节点与租约条件给出建议，并组合人格、有效状态、活动、记忆和近期对话。默认提示预算 3072、记忆预算 800 个估算 token；人格核心和当前消息超预算时返回明确错误。真实输出仍经过宿主租约 gate。

主服务运行后，可创建独立演示数据并打印提示：

```bash
uv run python examples/personality_preview.py --show-prompt
```

示例角色卡是 [sui.v2.json](examples/character_cards/sui.v2.json)。此脚本会写入演示节点、人格、状态与一条共读记忆。预览的 `model_calls` 为 0；真实文本对话可在 WebUI 中通过配置的模型执行。

后台自触发已接入两类来源：宿主通过类型化注意力接口上报任务完成、开始休息或用户返回；以及 `inner_life` 根据既有情绪、慢变量、对话历史与空闲时间生成的短时心理活动。两者都要显式启用、chat 模式、参与条件与预算允许，且生成和呈现时复核上下文、epoch 与租约；专注、会议、静默、未知或过期信号默认不触发。主动文字只向仍有输出权的呈现端展示，不伪造用户发言或补播过时问候；其事件与决策可在流程追踪面板查看。Linux 已支持可选的[桌面状态自动采集](docs/linux-attention-collection.md)，会议、共享与专注识别仍保留未知。详见[注意力与自触发](docs/proactivity.md)、[内在活动](docs/inner-life.md)及[后台保护验证](docs/background-flow-validation.md)。

模型工具默认关闭。开启 `[tools]` 后，模型可在对话或主动轮次中安排延迟发言、创建/列出/取消计划任务或提醒，并把明确请求委派给外部代理；主动计划和计划任务到点仍须通过全部参与与输出 gate。工具通过 `ToolRegistry` 注册，内建工具与受信任的 `module:factory` 插件共享同一合同，委派执行器也可替换。插件接口、错误、限额与查询 API 见[模型工具与插件](docs/tools.md)。

## 开发验证

```bash
uv run pytest
uv run ruff check backend examples clients/system-node
```

安卓 SDK 已完成 JVM 库与连接示例编译。后端已成功启动、完成 OpenAPI 导出和静态检查；已有分模块检查结果见 [实施记录](docs/implementation-plan.md)。

通用设备客户端已通过 Ruff 静态与格式检查、Python 编译；本阶段未运行测试、真实上报或设备联调。主服务器目前按原样保存设备状态 JSON，状态归一化、旧会话拒绝和历史清理将在后续阶段实现。

人格阶段已通过 Ruff、Python 编译、26 个路径的 OpenAPI 导出，以及安卓 SDK / 连接示例编译。此次未新增或运行测试、启动 API、执行演示脚本或调用真实模型；人格表达效果与实际设备行为随后验证。

WebUI 阶段已通过 Ruff、Python/JavaScript 语法检查、28 个路径的 OpenAPI 导出、安卓 SDK 编译及包含页面资源的 wheel 构建。没有运行测试、浏览器自动化、实际对话或设备联调；详细分工和检查记录见 [实施记录](docs/implementation-plan.md)。

分层记忆第一阶段的整合测试为 70 项通过；最终 Ruff/格式、JavaScript 语法检查和 29 个路径的 OpenAPI 导出通过，安卓 SDK 与示例编译成功。收尾的省略时间字段序列化修正未追加行为测试；未调用真实模型或进行浏览器、设备联调。详细检查范围见 [实施记录](docs/implementation-plan.md)。

自动记忆阶段通过 Ruff/格式检查（34 个 Python 文件）、Python 编译、JavaScript 语法、34 个路径的 OpenAPI 导出和 SDK/示例编译。本阶段未新增或运行测试，未调用真实模型或进行浏览器、设备联调；此前测试结果不代表新的自动流程已完成行为验证。

屏幕视觉阶段新增 7 个合同测试（上传 → 帧 → 分析闭环、幂等/冲突、乱序、精确复用、reference/unavailable 屏障、reporter 403、purge），全量后端测试 741 项通过；Ruff/格式检查、74 个路径的 OpenAPI 导出、Kotlin SDK 与连接示例编译通过，并用注入的 fake provider 端到端跑通 `examples/screen_vision.py`。本阶段未调用真实视觉模型，识别质量、成本与耗时需在部署环境实测。

随后完成窗口上下文富化与注入闸门、linux-node 截图上传链路（linux-node 65 项通过）、每日视觉配额改为 0=不限制（全量后端 761 项通过），并用 `examples/screen_vision_latency.py` 实测全链路出首字：4K 截图下约 **27s**（视觉 `glm-4.6v` 25–41s + 对话首字 ~1.2s，对话模型为硅基流动 `deepseek-ai/DeepSeek-V4.1-Flash`），详见[屏幕视觉](docs/screen-vision.md)。

## 文档与范围

- [实施计划与子代理分工](docs/implementation-plan.md)
- [设备节点与记忆架构](docs/architecture.md)
- [设备状态跟踪与轻客户端](docs/device-state-tracking.md)
- [屏幕感知与心流判定](docs/screen-perception-and-flow.md)
- [屏幕视觉协议与配置](docs/screen-vision.md)
- [屏幕视觉服务端实施规划](docs/screen-vision-server-plan.md)
- [非视觉自触发与情感系统](docs/ambient-signals-and-emotion.md)
- [注意力边界与自触发实现](docs/proactivity.md)
- [内在活动与内在驱动的主动候选](docs/inner-life.md)
- [长期记忆与成本预算](docs/memory.md)
- [分层记忆调研与演进路线](docs/layered-memory-research.md)
- [自动记忆、摘要与知识图谱](docs/automatic-memory.md)
- [基础人格、状态与提示预览](docs/personality.md)
- [连续情绪、共同经历与澄清](docs/emotion.md)
- [设备身份与 Web 访问](docs/device-identity.md)
- [客户端节点安装、分发与更新调研](docs/client-distribution-research.md)
- [Linux 安装包与 WebUI SSH 分发](docs/linux-deployment.md)
- [Linux 桌面状态自动采集](docs/linux-attention-collection.md)
- [Linux 完整客户端实施计划：Shorin DMS Niri / kitty / fish](docs/linux-client-implementation-plan.md)
- [自动采集与真实 LLM 对话验收](docs/linux-attention-validation.md)
- [本地实时语音识别与对话交接调研](docs/local-realtime-speech-research.md)
- [连续语音、TTS 与 Linux / Android 客户端实施规划](docs/realtime-voice-and-client-plan.md)
- [最近功能真实联调验收](docs/real-validation-report.md)
- [后台专注保护与主动陪伴验证](docs/background-flow-validation.md)
- [WebUI、对话执行与模型配置](docs/webui.md)
- [流式聊天与断流恢复](docs/dialogue-streaming.md)
- [统一配置文件与环境变量](docs/configuration.md)
- [向量记忆、实体别名与多跳图谱](docs/vector-memory-and-graph.md)
- [MaiBot、Operit、AIRI 等参考调研](docs/references.md)
- [节点协议](docs/protocol.md)
- [MCP 桥与 DSH 工具面](docs/mcp-bridge.md)
- [DSH 集成与插件定位](docs/dsh-integration.md)

本轮是单所有者的本地开发框架。账号、设备配对、多人隔离、服务端取消对话、角色动画资产和安卓宿主 UI 属于后续工作。
