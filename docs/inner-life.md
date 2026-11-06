# 内在活动与内在驱动的主动候选

内在活动（inner life）是生命体对**已有证据**的短时、可检查解释，用来让主动陪伴不只有宿主的任务边界。它是一条独立的本地层，不是第二套人格，也不是用户事实。

## 边界

- 默认 `model_assisted=false`，完全由本地规则生成；此时 `model_calls` 恒为 0。
- 显式开启 `model_assisted` 后，模型只评判感受标签与强度，不生成自由文本，不授予输出权。
- 不写入稳定人格、情绪反应、共同经历或长期记忆。
- 它最多生成一个主动候选；真实发言仍须通过完整 gate。
- 默认只读取：有效情绪、L2 慢变量、最近对话、宿主注意力类别、空闲时间与当前模式。
- `content` 始终由确定性模板选择；同一上下文版本与时间桶得到同一 `activity_id`，重启后可审计。
- 模型评判失败、输出无效、置信度不足或预算不足时回退本地规则，不阻塞活动生成。

## 活动

活动类型为 `restless`、`curious`、`concerned`、`reflective`、`content`、`resting`。每条活动保存：

- `activity_id`、`sequence`、`companion_id`
- `kind`、`trigger`、`content`
- `salience`、`urge`
- `attention_session_id`、`attention_boundary_id`
- `context_version`、`source_event_id`
- `generator`（`rules_v1` / `model_v1`）与可选 `model` 审计信息
- `thinking`（模型模式下捕获的 reasoning channel，内心独白，最多 2048 字符；`rules_v1` 时为 null）
- `status`（`active` / `expired`）与 `created_at` / `expires_at`

`urgency`（`urge`）是候选强度，不等于发言许可。`content` 是内部提示材料，真实对话提示会明确标注“这是你自己的内在活动，不是用户发言”。`model.rationale` 仅供审计，绝不进入后续提示。

### 回忆锚定（reminisce）

`reflective` 活动走本地模板路径时（规则直接产出、或模型评判未给出 `content`），独白会尝试锚定一条真实旧 episode：从 `memory_records` 取 `kind='episode'`、`active=1`、`COALESCE(occurred_at, updated_at)` 早于 7 天（`_REMINISCE_MIN_AGE_DAYS`）的最近至多 8 条（`_REMINISCE_CANDIDATES`），按 `activity_id` 哈希确定性选一条，剥离 `User:`/`Assistant:` 等前缀后截断 60 字（`_REMINISCE_SNIPPET_CHARS`），生成「想起一件旧事：……。当时的感觉还留着一点。」。片段逐字来自记忆记录、绝不改写；无合格旧 episode（太新、不存在或全空）时回退通用模板。其他 kind 不受影响。

### 可选模型评判

`model_assisted=true` 且模型已配置时，服务至多每个 `model_min_interval_seconds` 调用一次评判，并受每日次数与 token 账本限制。模型只返回 `kind`、`salience`、`urge`、`confidence`、`rationale`；服务验证 schema、置信度与允许类型，再按 `model_weight` 与本地分数混合。模型看到的最近对话被标记为不可信数据。调用不在 SQLite 写事务内执行，失败或无效输出自动使用本地结果。

模型调用若带 reasoning channel（`reasoning_content`），服务把它捕获为 `thinking`（`model_provider` 透传 `ModelResult.reasoning`，`inner_life._judge` 截断到 2048 字符），随活动持久化；DSH 侧的 `dsh/virtual-life-innerlife/` 插件消费该字段作为内心独白。`thinking` 与 `model.rationale` 一样仅供消费方参考，不进入后续提示。

## 接口

```text
GET /v1/companions/{companion_id}/inner-life
GET /v1/companions/{companion_id}/inner-life/activities?after_sequence=0&limit=50
```

快照包含当前情绪摘要、慢变量摘要、`needs`、主导活动、当前活动、最近 8 条活动，以及 `model_assisted` 与当前活动是否由模型评判的 `model_calls`。活动分页按 `sequence` 升序，`limit` 为 1..100。`enabled=false` 时轮询不创建新活动；历史接口仍可读取已有记录。

### 睡眠状态（`phase`）

快照的 `phase` 字段是显式睡眠状态机，取值 `awake`/`drowsy`/`asleep`：

- 23:00-00:00（本地时区）为入睡中的 `drowsy`；00:00-05:00 为核心睡眠 `asleep`，但若当前 `urge ≥ 0.6`（强烈内心冲动）则半醒为 `drowsy`；其余时间 `awake`。`rhythm_enabled=false` 时永不入睡。
- 用户消息走 `_phase`（可以被叫醒）：深夜用户主动发消息时，提示词会追加"你原本是睡着的，是被这条消息叫醒的"旁注，允许回复带睡意、更短。
- 主动消息与内心动作走 `sleep_phase`（不看唤醒阈值）：`asleep` 期间不发送主动消息、不碰手机；每轮 tick 睡眠中能量 +0.05（直接维护 `personality_states.state_json`，不改 revision、不发事件）。

## 主动候选

`[inner_life]` 的 `proactive_urge_threshold` 控制入队门槛。只有同时满足以下条件，最新活动才会生成 `origin="inner"` 的候选：

1. 伙伴主动策略已显式开启，且存在历史用户对话。
2. 活动仍为 `active` 且未过期，`urge` 达到阈值。
3. 注意力样本新鲜、可用、未锁定、非会议/共享/专注/疑似卡住。
4. 距离最近用户对话不少于 `proactive_min_idle_seconds`。
5. 对话模型已配置，且普通主动 gate 的冷却、每日限额、人格主动性、精力与社交精力仍通过。
6. 当前候选过期前，注意力 session 与 boundary 未切换；执行与呈现时再次复核 epoch、租约和上下文。

内在候选与注意力边界候选共用 `proactivity_candidates` 表、决策日志、输出回执和中断机制。决策记录新增 `origin` 与 `source_activity_id`；WebUI/客户端应显示来源，但不得把内部活动当作用户消息或已确认事实。

## 配置

完整字段见 [配置](configuration.md) 与 `virtual-life.example.toml`。关键默认值：

| 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| `enabled` | true | 是否生成新活动 |
| `min_activity_gap_seconds` | 600 | 两次活动的最小间隔 |
| `activity_ttl_seconds` | 3600 | 活动有效时间 |
| `min_evidence_items` | 1 | 慢变量证据门槛 |
| `proactive_urge_threshold` | 0.65 | 主动候选门槛 |
| `proactive_min_idle_seconds` | 1200 | 主动前最短空闲 |
| `idle_saturation_seconds` | 1800 | 空闲需求饱和时间 |
| `max_history` | 200 | 每个伙伴保留的活动上限 |
| `model_assisted` | false | 是否启用可选模型评判 |
| `model_min_interval_seconds` | 3600 | 两次模型评判的最小间隔 |
| `model_daily_limit` | 24 | 每伙伴每日模型评判次数上限 |
| `model_output_token_limit` | 200 | 单次评判输出 token 上限 |
| `model_min_confidence` | 0.55 | 接受模型结果的最低置信度 |
| `model_weight` | 0.5 | 模型分数在混合结果中的权重 |
| `action_enabled` | true* | 是否允许内心动作（需配置 adb serial） |
| `action_adb_serial` | "" | 目标模拟器 adb serial（如 emulator-5554） |
| `action_open_app_whitelist` | () | “做”动作允许打开的包名白名单 |
| `action_min_interval_seconds` | 1800 | 两次内心动作的最小间隔 |
| `action_daily_limit` | 8 | 每伙伴每日动作次数上限 |
| `action_curious_min_salience` | 0.35 | 触发动作的最低 salience |
| `action_screenshot_dir` | .data/inner_actions | 截屏落盘目录 |

*action_enabled 策略默认为 true，但未配置 action_adb_serial 时不会执行任何动作。

## 内心动作（实验性）

当内心活动为 curious 或 restless 且 salience ≥ `action_curious_min_salience` 时，运行时可对本地 Android 模拟器执行一次“内心动作”：

- **看（look）**：`adb -s SERIAL exec-out screencap -p` 截屏，PNG 落盘到 `action_screenshot_dir`（默认 `.data/inner_actions/`，文件名 `<companion_id>_<activity_id>.png`），路径与字节数写入事件载荷，供后续模型通道或屏幕分析引用。
- **做（open_app）**：当活动为 restless 且白名单非空时，从 `action_open_app_whitelist` 按 activity_id 确定性选取一个包，`adb -s SERIAL shell monkey -p PKG -c android.intent.category.LAUNCHER 1` 启动。
- **碰（press_home / press_back）**：restless 时约五分之一的动作是轻量的导航按键（`adb -s SERIAL shell input keyevent 3/4`），代替每次都启动应用——像顺手按了一下主页/返回键的"游荡"手势；白名单为空时 restless 只会执行这两种动作。

门槛与安全边界：`action_enabled`、间隔 ≥ `action_min_interval_seconds`、当日次数 < `action_daily_limit`；每次动作前用 `adb devices` 确认 serial 在线，不在线则静默跳过（仅 debug 日志）。只允许白名单内的包名，不执行 root 命令，不删除任何数据；任何 adb 失败只记日志、绝不抛出，不影响对话与主流程。动作结果以事件 `companion.inner_action` 发出，载荷含 `activity_id`、`kind`、`action`、`target`、`serial`、`outcome`（ok/failed/refused），`look` 成功时附 `screenshot_path` 与 `screenshot_bytes`。执行入口为 `InnerLifeService.tick()` 后台轮询（`[inner_life] poll_seconds`）末尾的 `_maybe_action()`，adb 子命令超时 30 秒。

配置四联动见 [配置](configuration.md)；`action_observer_node_id` 与 `action_analysis_wait_seconds` 预留给屏幕分析管道接入，当前执行路径未使用。

动作记忆：`outcome` 为 ok 或 failed 的动作，在事件提交后（写事务之外）由 `_remember_action()` 写入一条第一人称的 episode 记忆（`command_id = inner-action-<event_id>`，tag `inner_action` + `model_v1`/`rules_v1`，importance 0.4，source_event_id 指向该动作事件），使伴侣“做过什么”能被后续对话回忆；`refused`（非白名单）属于管理性跳过，不写入记忆。记忆正文优先由共享模型通道生成（`_action_memory_line()`，prompt 带动作描述、当时的内心活动与当前 mood，要求一句不超过 40 字的第一人称，台账 purpose `inner_action_memory`、task_id `actionmem-<activity_id 后 40 位>`）；模型不可用、超预算或产出不合格时回退到 `_ACTION_MEMORY_TEMPLATES` 模板池。写入是尽力而为的：记忆或模型失败只记 warning，绝不影响动作管线。

前台应用感知：`look` 动作在保存截屏后，会通过 `adb shell dumpsys activity activities` 的 `topResumedActivity` 行提取当前前台包名（正则 `topResumedActivity=(?:ComponentInfo\{)?<包名>/`，失败返回 None、不影响动作成败），写入事件 payload 的 `foreground_app`，并同步进入 `recent_action` 跟进上下文与动作记忆（记忆与独白里用中文友好标签，如 com.android.chrome→“浏览器”）。内心独白因此可以自然地“知道”刚才看屏幕时对方在哪个 app，而不做视觉细节的编造。

## 相关实现

- 服务与表：`backend/virtual_life/inner_life.py`
- 候选与 gate：`backend/virtual_life/proactivity.py`
- 中断复核：`backend/virtual_life/proactivity_output.py`
- 提示段：`backend/virtual_life/personality_prompt.py`
- 测试：`backend/tests/test_inner_life.py`

## 好奇学习（curiosity compounding）

好奇不再是一次性的张望。`_maybe_action` 执行 `look` 并成功取到前台应用时，会在同一写事务里向 `curiosity_observations`（companion_id, app_label, seen_at）追加一条观察记录；72 小时窗口内同一应用被观察到至少 3 次（`_CURIOSITY_WINDOW_HOURS`、`_CURIOSITY_REPEAT_MIN`）后，好奇独白的生成链 `judgment.content → 回忆锚定 → 好奇学习 → 模板` 会在第三步产出「最近几次看过去你都在用{应用}，我有点好奇它哪里吸引你，等你想聊的时候可以讲讲。」——好奇从"看一眼"累积成"想知道你为什么总去那里"。观察本身只是感知记录：`peek()` 纯查询不落库，只有真实执行的 look 动作才计数；应用名走 `_app_label` 友好名映射（未知包名取末段）。没有达到重复阈值时保持通用模板，不硬凑。
