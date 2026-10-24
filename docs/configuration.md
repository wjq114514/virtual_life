# 统一配置

服务端采用 TOML，无需额外依赖。完整默认配置见 [virtual-life.example.toml](../virtual-life.example.toml)。在项目根目录执行：

```bash
cp virtual-life.example.toml virtual-life.toml
uv run python -m virtual_life
```

编辑 `virtual-life.toml` 中的模型地址、模型名与密钥，再重启即可启用对话和自动记忆。`[server]` 控制监听地址与端口。配置在启动时读取，修改后需重启；没有热更新接口。`virtual-life.toml` 已被忽略，可保存本地密钥，示例文件只保留默认值。

## Linux 节点远程安装

WebUI 连接配置及设备区域提供 SSH 分发入口，无需新增 TOML 密钥项。SSH 凭据只用于当前请求，不保存在服务器配置或浏览器存储；目标机使用单独的自身上报凭据。目标需已有 Python 3.12+、systemd 和 root/无密码 sudo，表单服务器地址须从目标机可达。安装路径、主机指纹核对、回执恢复和撤销范围见 [Linux 安装与分发](linux-deployment.md)。

## 后台启停脚本

`./serve.sh` 用于本地后台运行，不需要额外依赖：

```bash
./serve.sh start|stop|restart|status|logs
```

默认监听 `0.0.0.0:8038`，可用 `VIRTUAL_LIFE_HOST`、`VIRTUAL_LIFE_PORT` 覆盖。脚本行为：

- `start` 优先用已同步的 `.venv/bin/uvicorn`，没有 venv 时退回 `uv run`；用 `setsid` 让服务脱离终端，父 shell 退出后继续运行。
- `start` 轮询 `/healthz` 直到就绪（默认 40 秒）才返回成功；超时则打印日志尾部并回滚停止。
- 端口被非本脚本启动的进程占用时拒绝启动，而不是杀掉对方。
- `stop` 按进程组终止。`uv run` 会再 fork 一层，只杀父进程会留下孤儿，因此整组一起发信号，15 秒后升级为 `KILL`。
- 记录写在 `.data/server-<端口>.pid`（两行：pid 与 `host:port`），按端口分文件以支持同时跑多个实例。读取前会校验 `/proc/<pid>/cmdline` 确认进程身份，避免误杀被复用的 PID；陈旧记录会被清理。
- 日志追加到 `.data/server.log`，用 `./serve.sh logs` 跟随。

脚本不改配置、不写数据库，仅做进程生命周期管理；所有产物都在已 gitignore 的 `.data/` 下。

## 加载顺序与路径

参数优先级从低到高为：内置默认值 → TOML 文件 → 已设置的环境变量。缺少章节或参数时使用默认值。环境变量只覆盖对应字段；`MODEL_EXTRA_BODY` 覆盖整个对象。空字符串也算显式覆盖，空的 token 或数值会因校验失败而停止启动。

默认查找当前工作目录的 `virtual-life.toml`，不存在时使用默认值。可用命令行或环境变量指定其他文件：

```bash
uv run python -m virtual_life --config /path/to/virtual-life.toml
VIRTUAL_LIFE_CONFIG=/path/to/virtual-life.toml uv run python -m virtual_life
```

命令行 `--config` 优先于 `VIRTUAL_LIFE_CONFIG`；显式指定的文件必须存在且可读取。文件语法错误、未知章节、未知字段或有效配置类型/范围错误均使启动失败，不忽略拼写错误。错误信息不包含密钥值。

文件内的 `runtime.database_path` 相对配置文件目录解析；`~` 展开到用户目录。环境变量 `VIRTUAL_LIFE_DATABASE` 的相对路径继续按进程工作目录解析。未指定数据库时，默认路径 `.data/runtime.sqlite3` 也相对进程工作目录。

文件内 `speech.model_dir`、`speech.model_manifest` 和含路径的 `speech.worker_python` 同样相对配置文件目录解析；裸解释器名通过 PATH 查找。环境变量覆盖的路径保持相对工作目录的语义。默认值不随配置目录移动。

加载器不自动读取 `.env`。已有 Uvicorn 启动方式仍可用，它会加载统一文件，监听参数由 Uvicorn 命令行控制：

```bash
uv run uvicorn virtual_life.main:app --env-file .env --host 127.0.0.1 --port 8000
```

`.env` 的参数通过 `--env-file` 进入环境，会覆盖统一文件。所有实例应使用相同数据库、配置和模型额度。

## 配置章节

| 章节 | 配置内容 |
| --- | --- |
| `[server]` | 新启动入口的监听 host、port |
| `[logging]` | 控制台/文件日志级别、轮转大小与保留数量、HTTP 访问日志 |
| `[runtime]` | 数据库、API token、节点在线期限、输出租约、迁移期限、轮询与认证超时 |
| `[model]` | Chat Completions 根地址、模型名、密钥、HTTP 超时、额外参数 |
| `[scene_classifier]` | 本地场景分类器模式（off/observe/route）、模型产物、推理后端、超时与标签到回复模型的映射 |
| `[automatic_memory]` | 自动记忆开关、批次、最大等待、轮询、输出 token 上限 |
| `[proactivity]` | 注意力边界与内在活动候选的 poll_seconds，默认 2 秒；开关与限额通过伙伴策略 API 保存 |
| `[inner_life]` | 心理活动开关、轮询、活动间隔/TTL、证据门槛、主动冲动阈值与可选模型评判 |
| `[tools]` | 模型工具总开关、单轮最大调用数（1..2）与受信任插件工厂，默认关闭 |
| `[delegation]` | 外部代理 argv、工作目录、超时、输出上限与环境变量白名单，默认关闭 |
| `[proactive_schedule]` | `schedule_next_proactive` 的延迟边界与到点静默要求 |
| `[memory]` | 召回 token/条数上限、候选范围、时间衰减、整理计划、共享模型额度 |
| `[personality]` | 人格片段与整个提示的 token 上限 |
| `[personality.evolution]` | 人格进化开关、提议栅栏、阈值与模型辅助开关 |
| `[personality.slow_variables]` | L2 长期慢变量开关、半衰期、阈值与参与权重（只读） |
| `[embedding]` | 独立向量模型、索引与查询缓存、实际对话语义检索、本地额度模式 |
| `[rerank]` | 显式语义召回的 CPU 重排、候选上限、超时与原融合顺序权重 |
| `[knowledge_graph]` | 图谱最大跳数与累计候选上限 |
| `[screen_vision]` | 服务端屏幕视觉开关、独立视觉 provider、TTL、图片/队列上限与视觉 token 子额度 |
| `[voice]` | 连续语音文本轮次编排、会话期限、事件重放和旧调用收尾上限 |
| `[speech]` | 服务端本地 Kokoro TTS worker、模型路径、并发、分句和播放缓冲上限 |

模型地址与名称必须成对配置。自动记忆复用此模型，要求 JSON object 响应支持；摘要与知识图谱使用现有 SQLite，无需另配图数据库。每日模型额度由同一数据库内的全部伙伴和任务共享，按 UTC 日记账，详见 [自动记忆](automatic-memory.md)。默认召回额度/条数必须小于等于各自最大值；人格片段上限必须小于等于整个提示上限。

向量模型独立配置且默认关闭；启用后后台回填现有有效记忆，索引、查询、对话、自动整理共用模型额度。`embedding.dimensions` 默认省略；向量服务需支持 Embeddings 的 index、float 向量和 usage。`embedding.local = true` 用于本地 TEI 等不按 token 计费的部署：仍记录任务、usage 与单任务输入上限，但不占用付费日额度。配置细节、CPU 模型建议、空间切换与费用见 [向量记忆与图谱](vector-memory-and-graph.md)。

`[rerank]` 默认关闭，只作用于显式 `POST .../memories/semantic-recall` 的候选头部；普通 recall、人格预览和对话主链路不会调用。它采用 fail-open 策略：超时、传输失败或连续失败熔断时保留原 RRF 顺序。CPU 部署建议 `candidate_limit <= 8`、`timeout_seconds <= 0.5`，以避免可感知延迟。

`[memory]` 中 `default_recall_tokens` 和 `default_max_items` 是记忆服务未提供覆盖值时的默认值；HTTP 召回、提示预览和对话请求有各自的请求默认值，仍受配置的最大值约束。`consolidation_*` 用于本地整理计划，自动提取由 `[automatic_memory]` 的批次与输出配置控制。

## 场景分类器配置

新增可选的 `[scene_classifier]`，默认 `mode="off"`，不加载模型、不增加对话延迟。分类标签固定为 `daily`、`emotional`、`intimate`、`explicit`，没有第五类。

- `mode="observe"`：旁路检测。分类结果只写入对话回执的 `scene` 诊断字段并记录错误日志；回复模型、提示、流式草稿和最终输出完全沿用原有链路。
- `mode="route"`：直接分流。分类标签命中 `models` 中的非空模型时覆盖本轮 `reply_model`；未命中或分类失败时沿用 `[model].name` 或既有路由器结果。原有所有权、epoch、租约、预算、提示和输出 gate 一律不变。
- `artifact`：分类器产物目录。TOML 相对路径以配置文件目录为基准；环境变量相对路径按进程工作目录解析。目录缺失、依赖未安装或推理失败都不会阻止启动或对话。
- `backend`：`auto`/`onnx`/`torch`。`auto` 在安装 `onnxruntime` 且产物含 ONNX 文件时优先 ONNX，否则回退 PyTorch。
- `max_length`/`threads`/`prefer_int8`：CPU 推理的 token 截断、线程数与量化优先策略；`threads=0` 使用运行时默认值。
- `max_chars`：送入分类器的用户消息字符上限，默认 2048。
- `timeout_seconds`：单次推理等待上限，默认 1 秒。超时、忙碌、异常或模型不可用时只产生带 `error_code` 的观察结果，不阻塞对话。
- `models`：`route` 模式使用的标签到回复模型映射，例如 `models = { explicit = "local-uncensored-model", intimate = "local-uncensored-model" }`。至少需要一项非空模型；空值或未列出的标签使用默认模型。
- `observe` 模式另外输出每轮一条 `scene_classifier` INFO 日志（标签、四路分数、延迟、错误码，不含原文）。回执中的观察结果可用 `scripts/export_scene_observations.py` 批量导出后人工核对，详见 [场景分类器实现说明](nsfw-classifier-implementation.md)。

分类器不是内容过滤器，也不改变输出权限。需要 `uv sync --group train` 才能安装 `onnxruntime`/`transformers`（或 PyTorch 后端依赖）；未安装时 `observe`/`route` 会安全降级。`GET /v1/capabilities` 的 `scene_classifier` 对象报告模式、可用性、后端与版本，不返回原始用户文本。分类器只处理普通文本和语音用户回合；主动对话没有用户场景，不运行分类器，回执中的 `scene` 为 null。

## 环境变量对应

保留已有环境变量，并补充运行时轮询、监听和策略覆盖。完整命名规则如下：

| TOML 字段 | 环境变量 |
| --- | --- |
| `server.host` / `server.port` | `VIRTUAL_LIFE_SERVER_HOST` / `VIRTUAL_LIFE_SERVER_PORT` |
| `logging.<字段>` | `VIRTUAL_LIFE_LOGGING_<字段大写>` |
| `runtime.database_path` | `VIRTUAL_LIFE_DATABASE` |
| `runtime.api_token` | `VIRTUAL_LIFE_API_TOKEN` |
| `runtime.<其余字段>` | `VIRTUAL_LIFE_<字段大写>`，例如 `VIRTUAL_LIFE_EVENT_POLL_SECONDS` |
| `model.base_url` | `VIRTUAL_LIFE_MODEL_BASE_URL` |
| `model.name` | `VIRTUAL_LIFE_MODEL` |
| `model.api_key` | `VIRTUAL_LIFE_MODEL_API_KEY` |
| `model.timeout_seconds` | `VIRTUAL_LIFE_MODEL_TIMEOUT_SECONDS` |
| `model.extra_body` | `VIRTUAL_LIFE_MODEL_EXTRA_BODY`，使用 JSON 对象 |
| `scene_classifier.<字段>` | `VIRTUAL_LIFE_SCENE_CLASSIFIER_<字段大写>`；models 使用 JSON 对象 |
| `automatic_memory.<字段>` | `VIRTUAL_LIFE_AUTO_MEMORY_<字段大写>` |
| `proactivity.poll_seconds` | `VIRTUAL_LIFE_PROACTIVITY_POLL_SECONDS` |
| `inner_life.poll_seconds` | `VIRTUAL_LIFE_INNER_LIFE_POLL_SECONDS` |
| `inner_life.<其余字段>` | `VIRTUAL_LIFE_INNER_LIFE_<字段大写>` |
| `tools.<字段>` | `VIRTUAL_LIFE_TOOLS_<字段大写>`；plugins 使用 JSON 数组 |
| `delegation.<字段>` | `VIRTUAL_LIFE_DELEGATION_<字段大写>`；command/env_allowlist 使用 JSON 数组 |
| `proactive_schedule.<字段>` | `VIRTUAL_LIFE_PROACTIVE_SCHEDULE_<字段大写>` |
| `memory.<字段>` | `VIRTUAL_LIFE_MEMORY_<字段大写>` |
| `personality.<字段>` | `VIRTUAL_LIFE_PERSONALITY_<字段大写>` |
| `personality.evolution.<字段>` | `VIRTUAL_LIFE_PERSONALITY_EVOLUTION_<字段大写>` |
| `personality.slow_variables.<字段>` | `VIRTUAL_LIFE_PERSONALITY_SLOW_VARIABLES_<字段大写>` |
| `embedding.name` | `VIRTUAL_LIFE_EMBEDDING_MODEL` |
| `embedding.local` | `VIRTUAL_LIFE_EMBEDDING_LOCAL` |
| `embedding.<其余字段>` | `VIRTUAL_LIFE_EMBEDDING_<字段大写>`；dimensions 空字符串表示不指定 |
| `rerank.name` | `VIRTUAL_LIFE_RERANK_MODEL` |
| `rerank.<其余字段>` | `VIRTUAL_LIFE_RERANK_<字段大写>` |
| `knowledge_graph.max_hops` / `knowledge_graph.candidate_limit` | `VIRTUAL_LIFE_GRAPH_MAX_HOPS` / `VIRTUAL_LIFE_GRAPH_CANDIDATE_LIMIT` |
| `screen_vision.name` | `VIRTUAL_LIFE_SCREEN_VISION_NAME`（映射到内部 model_name，避免与文本模型变量冲突） |
| `screen_vision.<其余字段>` | `VIRTUAL_LIFE_SCREEN_VISION_<字段大写>` |
| `voice.<字段>` | `VIRTUAL_LIFE_VOICE_<字段大写>` |
| `speech.<字段>` | `VIRTUAL_LIFE_SPEECH_<字段大写>` |

布尔环境变量接受 `true/false/1/0`，不区分大小写。整数参数使用整数，时间与衰减参数使用有限正数。常用环境覆盖见 [.env.example](../.env.example)。示例：

```bash
VIRTUAL_LIFE_MEMORY_DAILY_MODEL_TOKEN_LIMIT=20000 uv run python -m virtual_life
```

Python 嵌入场景可用 `Settings.load(path)` 加载同一配置；`Settings.from_environment()` 兼容入口也会读取统一文件。直接构建 `Settings(...)` 使用显式参数与内置默认值。`create_app(settings, memory_policy=..., personality_policy=...)` 中显式传入的策略优先于 Settings 中的策略。

## 记忆衰减配置

统一配置使用 `[memory.decay]` 嵌套表（完整示例见 `virtual-life.example.toml`）：

```toml
[memory.decay]
enabled = true
routine_half_life_days = 30.0
routine_floor = 0.1
significant_half_life_days = 180.0
significant_floor = 0.3
stable_half_life_days = 365.0
stable_floor = 0.5
permanent_candidate_limit = 128
reinforcement_min_coverage = 0.5
semantic_salience_weight = 0.05
```

半衰期为有限正数，下限与强化覆盖率为 0..1，永久候选上限为正整数。`VIRTUAL_LIFE_MEMORY_DECAY` 接受这些字段组成的 JSON 对象；`VIRTUAL_LIFE_MEMORY_DECAY_ENABLED`、`VIRTUAL_LIFE_MEMORY_DECAY_ROUTINE_HALF_LIFE_DAYS` 等逐字段覆盖优先于对象覆盖。未知字段和错误类型会在启动时拒绝，错误不回显配置值。关闭衰减后 `memory.recency_half_life_days` 继续作为旧排序的时间参数。衰减只影响显著性，完整行为见 [记忆设计](memory.md)。

`semantic_salience_weight` 为有限的 0..1 数值，控制向量排序的加性显著性先验；不会改变原始余弦相似度门槛。

## 日志配置

`[logging]` 统一控制运行时日志，参考 AstrBot 的单行输出与轮转文件方案：

```toml
[logging]
level = "INFO"          # DEBUG / INFO / WARNING / ERROR / CRITICAL
console = true          # TTY 下自动着色，NO_COLOR=1 可关闭
file_enabled = true
file_path = ".data/logs/virtual-life.log"
max_mb = 10             # 1..1024
backup_count = 3        # 1..100
access = true           # 每个 HTTP 请求一行访问日志
```

控制台格式为 `[HH:MM:SS.mmm] [INFO ] [api:470] [req=...] message`，文件使用完整日期并包含异常栈。`virtual_life.*`、`uvicorn`、`httpx` 等日志汇入同一格式；`httpx`、`httpcore`、`asyncio` 等噪音 logger 固定为 WARNING。文件达到 `max_mb` 后轮转为 `.1`、`.2`，最多保留 `backup_count` 个。

`access=true` 时每个请求记录方法、路径、状态码、耗时和客户端地址，不记录查询串、请求头或正文。服务端为每个请求生成或透传合法的 `X-Request-ID`，并通过响应头返回；同一请求内的日志行带相同 `[req=...]`，后台任务可用 `virtual_life.log.request_context` 继承。`/healthz` 与 `/ui/` 静态资源只在 `DEBUG` 级别记录。

TOML 中的 `file_path` 相对配置文件目录解析；环境变量与默认值保持相对进程工作目录。`./serve.sh` 启动的外层 stdout 仍追加到 `.data/server.log`，结构化日志同时写入上面配置的轮转文件；脚本已关闭 uvicorn 自带的访问日志，避免重复。直接使用 `uvicorn` 命令时建议加 `--no-access-log`。

## 情感配置

新增 `[emotion]`，默认 enabled=true、half_life_seconds=1200、candidate_limit=256、resonance_strength=0.2。环境变量为 `VIRTUAL_LIFE_EMOTION_<字段大写>`，优先于文件；修改后重启。完全使用本地规则，不增加模型调用。字段范围和禁用行为见[情感实现](emotion.md)。

## 人格进化配置

新增 `[personality.evolution]` 子表，默认 enabled=true、min_evidence_items=3、trait_window_seconds=7776000（90 天）、max_trait_step=0.05、max_cumulative_drift=0.2、trait_cooldown_seconds=604800（7 天）、max_open_proposals=5、proposal_ttl_seconds=2592000（30 天）、quiet_request_rate_threshold=0.4、proactive_silent_rate_threshold=0.6、preference_repeat_threshold=3。环境变量为 `VIRTUAL_LIFE_PERSONALITY_EVOLUTION_<字段大写>`，优先于文件，且优先于 JSON 形式的子表值；修改后重启。

`model_assisted` 默认 false：此时只运行本地 `rules_v1`，`use_model=true` 返回 422。设为 true 且配置了模型 provider 与模型名后，`use_model=true` 走 `model_v1`，复用 `[model]` 的 provider 与 `output_token_limit` 输出预算，usage 记入同一 token 账本；provider 缺失时返回 422 `model_assisted_unavailable`。提议始终需要所有者显式采纳，可随时通过 `refresh` 生成、`accept`/`reject` 处理；合同与阈值语义见[人格进化实施合同](personality-evolution-plan.md)。

## 长期慢变量配置

新增 `[personality.slow_variables]` 子表，默认 enabled=true、half_life_seconds=2592000（30 天）、window_seconds=31536000（365 天）、candidate_limit=256、min_evidence_items=3、warm_valence_threshold=0.1、concern_valence_threshold=-0.05、warmth_memory_scale=3.0、familiarity_scale=12.0、warmth_weight=0.8、engagement_min=0.2、engagement_max=0.8。环境变量为 `VIRTUAL_LIFE_PERSONALITY_SLOW_VARIABLES_<字段大写>`，优先于文件与 JSON 子表值；修改后重启。

L2 慢变量只读、聚合时不写库、不调用模型：读取时从 `emotion_experiences` 与 `memory_records` 现算，证据不足（`sample_size < min_evidence_items`）时 `engagement` 固定为中性 0.5、`tone_hint=neutral`，因此新伙伴不受惩罚。`enabled=false` 时无论数据多少都返回中性空快照。语义见[人格进化实施合同](personality-evolution-plan.md) 的阶段 B。

## 内在活动配置

新增 `[inner_life]`，默认 enabled=true、poll_seconds=10、min_activity_gap_seconds=600、activity_ttl_seconds=3600、min_evidence_items=1、proactive_urge_threshold=0.65、proactive_min_idle_seconds=1200、idle_saturation_seconds=1800、max_history=200。模型评判默认关闭；开启时使用 model_min_interval_seconds=3600、model_daily_limit=24、model_output_token_limit=200、model_min_confidence=0.55、model_weight=0.5，并共享每日模型 token 账本。`inner_life.<其余字段>` 使用 `VIRTUAL_LIFE_INNER_LIFE_<字段大写>`；`poll_seconds` 使用 `VIRTUAL_LIFE_INNER_LIFE_POLL_SECONDS`。

内在活动的内容和模板始终由本地规则生成，不修改人格、情绪或长期记忆。显式开启 `model_assisted` 后，模型只评判标签、强度与置信度，`rationale` 仅供审计且不进入提示；失败时回退本地规则。活动可以成为主动候选，但仅在伙伴主动策略开启、注意力新鲜且空闲、已有历史对话、冲动与冷却/每日限额均满足时才会经过对话输出 gate；默认阈值较高，新伙伴或专注场景不会因此频繁开口。接口与语义见[内在活动](inner-life.md)。

可单独设置 `valence_half_life_seconds`、`arousal_half_life_seconds`、`control_half_life_seconds` 和 `resource_half_life_seconds`，范围均为整数 60..7200 秒。默认省略/None，沿用共享 half_life_seconds；精力与社交精力共用 resource 参数，安静请求有效期仍由共享参数控制。例如 `VIRTUAL_LIFE_EMOTION_AROUSAL_HALF_LIFE_SECONDS=600` 仅调整唤醒度恢复；四个可选参数的空环境变量将覆盖文件值并恢复共享半衰期。快照 recovery_half_lives 显示最终采用值。

## 屏幕视觉配置

新增 `[screen_vision]`，默认 `enabled=false`。`mode` 默认 `two_stage`：启用时必须同时提供独立的视觉 provider `base_url` 与 `name`（可选 `api_key`），否则启动失败；`mode="single_pass"` 不启动视觉 worker，最新有效截图直接随 `[model]` 对话请求提交，视觉 provider 可省略，但 `[model]` 必须支持 OpenAI `image_url` 数据 URI。视觉路由仅接受管理员 token。关键字段：`mode`（`two_stage`/`single_pass`）、`storage_path`（默认 `.data/screen-vision`）、`session_ttl_seconds/frame_ttl_seconds`（1800/90）、`max_image_bytes/max_image_pixels/max_image_side`（8388608/12000000/8192）、`global_concurrency/max_pending_global/max_pending_per_stream`（2/64/2）、`evidence_wait_ms`（默认 0，两段式下派发前等待进行中分析的上限）、`daily_vision_token_limit`（5000）、`json_mode`（默认 true；GLM 系列不支持 `response_format`，须设 false）、`extra_body`（追加到视觉请求的 JSON 对象，推理模型需设 `{ thinking = { type = "disabled" } }`）、`enrichment`（默认 true，用 `client_features`/`host_metadata` 的窗口上下文富化提示词）、`injection_gate`（`off`/`deterministic`/`decision`/`jev`，默认 `deterministic`；后两者需 `injection_base_url`/`injection_model` 等，缺省或异常时退回确定性扫描）、`max_context_windows`/`max_context_chars`。视觉额度是共享每日 token 上限内的子额度，不增加总额；关闭后仍可用既有业务接口。协议、错误码与未验证项见[屏幕视觉](screen-vision.md)，规划见[服务端实施规划](screen-vision-server-plan.md)。

## 模型工具、主动调度与委派

`[tools]` 默认 `enabled=false`，此时提示、provider 请求与回执和旧版一致。开启后每轮允许一次工具轮次，`max_calls_per_turn` 为 1..2；工具可用于用户对话和主动轮次，不进入记忆提取、人格进化或屏幕视觉。工具名、参数、调用数、结果大小和最终文本均经过校验；超限调用在产生副作用前失败。内建工具包括主动调度、计划任务创建/列表/取消和可选外部委派；计划任务的延迟固定限制为 60 秒到 30 天，列表支持状态过滤，取消支持任务 ID 或精确标题，详情与状态通过 `GET /v1/companions/{id}/scheduled-tasks` 查询。`plugins` 为受信任的 `module:factory` 列表，最多 16 个工具定义；工厂接收 `ToolPluginContext`，失败会中止启动。插件合同、示例与接口见[模型工具与插件](tools.md)。

`[proactive_schedule]` 约束 `schedule_next_proactive`：`min_delay_seconds`/`max_delay_seconds` 默认 600/86400，`min_idle_seconds` 默认 3600。计划持久化并替换同一伙伴的待生效计划；到点后仍须通过注意力、人格、预算、租约与冷却 gate，且距最后一次用户对话超过静默要求，否则标记 `cancelled`。新的用户对话命令会取消待生效计划。

`[delegation]` 默认关闭；启用时必须同时开启 `[tools].enabled`。默认子进程后端使用无 shell 的 `command` argv 数组，必须恰好包含一个 `{prompt}` 参数；`working_dir` 留空时使用数据库目录。子进程只继承 `env_allowlist`（默认 PATH、HOME），超时 `timeout_seconds` 或输出超过 `max_output_chars` 时终止。任务持久化、单 worker 串行执行，结果通过 `companion.delegation_completed` 通知；模型不会等待子进程。代码嵌入可通过 `create_app(delegation_executor=...)` 替换执行器，此时可省略 command；接口见[模型工具与插件](tools.md)。

工具查询接口：`GET /v1/companions/{id}/tools`、`GET /v1/companions/{id}/proactivity/schedule`、`GET /v1/companions/{id}/delegations`。工具副作用先于最终文本提交，使用 `command_id:tool_call_id` 幂等；最终回复被丢弃时不回滚已执行副作用。

## 流式聊天与反向代理

连续语音需要显式设置 `[voice].enabled=true`；麦克风音频留在客户端，服务端只接收冻结后的文本和来源。TTS 单独由 `[speech].enabled=true` 开启，需先准备真实模型与 worker；缺少 TTS 时保留文字对话。安装与验证见 [语音实施报告](realtime-voice-implementation-report.md)，HTTP/SSE 合同见 [语音合同](voice-contract.md)。

默认会话心跳 10 秒、心跳期限 30 秒、无有效输入 120 秒暂停、绝对上限 30 分钟。输入最多 4096 字符、ASR 结束时间超过 10 秒拒绝新提交；重试原 command 只返回原回执。音频只暂存 60 秒。每次文本生成最多合成 600 字符/60 秒；单段最多 80 字符/10 秒，待合成最多 4 段、未播放最多约 8 秒；播放不前进时有界降级为文字。语音 `/v1/nodes/*/voice/sessions/*/turns/*/events` 同样需要代理关闭响应缓冲。

聊天页面默认使用流式接口，无需新增模型配置开关。提供商需支持 Chat Completions SSE 与 `stream_options.include_usage`；详见[流式对话](dialogue-streaming.md)。`model.extra_body` 不能覆盖 `stream` 或 `stream_options`。

反向代理应让 `/v1/companions/*/dialogue/stream` 的 `text/event-stream` 及时透传。服务端已发送 `Cache-Control: no-store` 和 `X-Accel-Buffering: no`；若代理仍缓冲响应，关闭该路径的响应缓冲、缓存与压缩聚合。以 Nginx 为例可设置 `proxy_buffering off; proxy_cache off; proxy_read_timeout 130s;`，指令行为见 [Nginx 官方代理文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。空闲时每 10 秒发送注释心跳，浏览器单次请求总时限仍为 120 秒；断流后查询原命令回执。

## 本地模型前缀缓存（prefix caching）

本地模型服务器（llama.cpp、vLLM、SGLang、Ollama、LM Studio 等）普遍支持前缀缓存：对相同 token 前缀的请求复用已计算的 KV cache，显著降低 prefill 延迟。开启方式因服务器而异，客户端只能让 prompt 结构尽量稳定。

### 一键启动：`serve.py`

项目根目录的 `serve.py` 统一管理本地 MoE 模型服务器（llama.cpp）和 virtual_life API，MoE 优化默认值开箱即用：

```bash
# 设置模型路径后一键启动（模型 + API）
export MODEL_PATH=/path/to/Qwen3-30B-A3B-Q4_K_M.gguf
./serve.py start

# 只启动 API（模型已在别处运行）
./serve.py start --api-only

# 只启动模型服务器
./serve.py start --model-only

# 查看状态 / 健康检查 / 日志
./serve.py status
./serve.py health
./serve.py logs --follow

# 停止
./serve.py stop

# 查看当前配置
./serve.py config
```

MoE 模型优化默认值（可通过环境变量覆盖）：
- `MODEL_CTX_SIZE=150000` — 150K 上下文
- `MODEL_CACHE_REUSE=65536` — 大缓存复用，最大化前缀缓存命中
- `MODEL_FLASH_ATTN=false` — 默认关闭（P40/V100 等旧卡不兼容，按需开启）
- `MODEL_MLOCK=true` — 锁定内存，避免 swap 延迟抖动
- `MODEL_PARALLEL=2` — 2 个并行 slot（自动调整保证每 slot ≥ 4096 tokens）

交互式配置向导：`./serve.py setup`

完整环境变量见 `./serve.py --help`。

### 服务器端启用（手动方式）

如果不用 `serve.py`，手动启动各本地模型服务器时需开启前缀缓存：

- **vLLM**：`--enable-prefix-caching`（新版本默认开启）
- **SGLang**：`--enable-prefix-caching`
- **llama.cpp server**：`--cache-reuse N`（N 为可复用的 token 数，建议 ≥ 4096）；`--ctx-size` 须大于 prompt 预算（默认 4096 token）
- **Ollama**：自动，无需配置
- **LM Studio**：自动，可在设置中调整 cache 大小

### 客户端优化

虚拟人生已对 prompt 结构做缓存友好排序：稳定区块（人格、风格示例、活动上下文、慢变量、内心活动、记忆）放在前面，每轮变化的区块（当前状态、历史、用户消息）放在最后。这样同一伙伴的多轮对话可以最大化复用前缀 cache。

区块顺序（最稳定 → 最易变）：
1. `PERSONALITY_DATA` — 仅在人格修订时变化
2. `STYLE_EXAMPLES_DATA` — 仅在人格修订时变化
3. `ACTIVITY_CONTEXT_DATA` — 仅在同伴活动补丁时变化
4. `SLOW_VARIABLES_DATA` — L2 长期聚合，数周变化一次
5. `INNER_ACTIVITY_DATA` — 周期性心理活动（约 10 分钟间隔）
6. `MEMORY_DATA` — 话题召回，对话内相对稳定
7. `CURRENT_MODE_STATE_DATA` — 每轮变化（情绪、状态、参与）
8. `RECENT_HISTORY_DATA` — 每轮增长
9. `CURRENT_USER_MESSAGE_DATA` — 每轮变化

### 可选：prompt cache key

部分提供商（Together.ai、OpenRouter 等）支持显式 cache key，用于路由到同一实例以提高 cache 命中率。通过 `model.extra_body` 添加：

```toml
[model]
base_url = "https://api.together.xyz"
name = "Qwen/Qwen2.5-32B-Instruct"
api_key = "your-key"
extra_body = { prompt_cache_key = "virtual-life" }
```

本地模型服务器（vLLM、SGLang、Ollama、llama.cpp）基于 token hash 自动缓存，无需 cache key。如果提供商不支持该字段，请求会失败——请确认提供商支持后再添加。
