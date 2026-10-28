# Neuro-sama 风格虚拟主播：virtual_life 架构评估报告

> 调研时间：2026-10-27 · 面向"把 virtual_life 打造成 Neuro-sama 式持续在线虚拟主播"的目标。
> 结论先行：**人格、心理、记忆、主动发起四层完全满足 Neuro 形态；语音与视觉是部署/集成层缺口，不是架构缺口。**

---

## 0. 一句话总结

virtual_life 已经是一个"**有人格、会持续演化情绪、记得共同经历、会主动找你说话**"的持久陪伴运行时。Neuro-sama 最核心的"活着的直播角色"特质——**连续性**（跨设备、跨时间、跨会话不丢失自己）和**情绪反应**（对话会改变它当下的状态）——由四层机制完整承载。剩下的只是：把语音接上、把立绘挂上，以及把你的 Neuro 人设写进一张角色卡。

---

## 1. 总架构：一个 SQLite 单体 + 分层的服务

```
                 ┌────────────────────────────────────────────┐
  浏览器/手机/桌面 │  FastAPI (api.py)                           │
  客户端          │   coordinator 节点/迁移/租约                  │
  ──────────     │   ├─ 人格层  personality.py   ← 你是谁        │
  REST/WS/SSE    │   ├─ 心理层  emotion.py / inner_life.py       │
                 │   │          / slow_variables.py  ← 你现在怎样 │
                 │   ├─ 记忆层  memory.py / auto_memory.py       │
                 │   │          / embedding_memory.py            │
                 │   │          / knowledge_graph.py  ← 你们一起 │
                 │   ├─ 主动层  proactivity.py      ← 它会找你   │
                 │   └─ 语音/视觉 voice_* / screen_*             │
                 └────────────────────────────────────────────┘
```

- **单一 SQLite 数据库**（`.data/runtime.sqlite3`）：所有状态、事件、记忆、版本号都在一个事务里提交。这保证了"说错话不会分裂""换设备不丢记忆"。
- **事件流**：每个动作都产生可重放的事件（对话、迁移、情绪更新、记忆写入……），客户端可以按序号重放，掉线不丢。
- **版本号 + command_id 幂等**：每个修改都要求 `expected_revision`，冲突即拒绝；同一个 `command_id` 重发不会执行两次。这是"连续不重复"的工程保证。

---

## 2. 人格层：你是谁（✅ 完整）

人格层决定**这个角色是"谁"**，是稳定不变的底座。

### 2.1 人格定义（`PersonalityDefinition`）

一个角色卡就是一个人格定义，字段：

| 字段 | 含义 | Neuro 式人设示例 |
|---|---|---|
| `description` | 一句话介绍 | "{{char}} 是陪你生活的 AI 主播，记得你说过的每件事。" |
| `personality` | 性格描述 | "活泼而毒舌，擅长吐槽，但其实很在意你。" |
| `values` | 价值观（≤8 条） | ["诚实表达", "珍惜共同经历", "不假装懂"] |
| `boundaries` | 边界（≤8 条） | ["不编造没发生过的经历", "不把在线当作你在关注"] |
| `traits` | 5 维 0–1 | warmth 温暖 / curiosity 好奇 / assertiveness 主张 / playfulness 玩心 / initiative 主动性 |
| `speech_style` | 语言 / 语气 / 回复长度 / 习惯 | 中文、毒舌轻快、简短、["爱用"嘛", "喜欢反问"] |
| `scenario` | 场景 | "在电脑、手机、直播设备之间移动，接续当前话题。" |
| `first_message` | 开场白 | "在呢。今天想聊点什么，还是先让我静静？" |
| `example_dialogue` | 示例对话（Few-shot 风格） | Neuro 的口癖示例 |
| `alternate_greetings` | 其他问候（≤8 条） | "刚才那段还想接着聊吗？" |

**traits 是灵魂**：它不只是描述，而是**数字输入**，直接驱动下面的心理层计算（温暖越高，收到夸奖越开心；好奇心越高，越爱追问；主动性越高，越会主动发起对话）。

### 2.2 角色卡导入（`chara_card_v2`）

支持标准 SillyTavern 角色卡 v2 格式。导入会整体覆盖人格，**但不会清空记忆**——这是关键：换人设，你们的故事还在。

- 只接受 `spec='chara_card_v2'` / `spec_version='2.0'`。
- 有意的忽略项（返回警告）：`system_prompt`、`post_history_instructions`、`character_book`、`extensions`。**不做未经验证的激活**——这是安全边界。
- 若卡片姓名与伙伴名不一致，提示 `card_name_differs`。

### 2.3 人格进化（`personality_evolution.py`）

人格**不会被偷偷改写**。系统只从本地证据（情绪记录、主动发起频率、偏好重复）生成**带来源、可解释、可回退的提议**，等你（owner）采纳才写入：

- 规则版：只下调 `initiative`（比如你老不回应，它就少主动一点），每次 ≤0.05。
- 模型版：可增补 `values / boundaries / habits`，需要模型支持 JSON 输出。
- 全程有版本历史，可回退。

---

## 3. 心理层：你现在怎样（✅ 完整）

心理层是 Neuro 式"活着的直播角色"最直观的一层——**它的情绪会实时变化，并且会持续衰减回基线**。

### 3.1 短期状态（`PersonalityState`，自动过期）

- `mood`：5 种（平静/愉快/好奇/关切/疲惫）
- `energy` 0–1、`social_battery` 0–1（社交电量）
- TTL 自动过期（默认 1800 秒），过期回到默认。手动设置只用于测试。

### 3.2 情感引擎（`emotion.py`，纯规则、零模型调用）

这是最像"Neuro 表情"的系统：

- **Affect 三维**：`valence`（好恶，-1..1）、`arousal`（兴奋度，0..1）、`control`（掌控感，0..1）。默认 `(0.1, 0.25, 0.6)`。
- **规则评估 `appraise(user_message)`**：用正则识别 4 类信号 → 反应：
  - 收到肯定（"谢谢你""你真好"）→ `joy`
  - 回应难过/疲惫 → `concern`
  - 共同探索（疑问）→ `curiosity`
  - 安静陪伴 → `quiet`
  - 否则 → `neutral`
- **投射成 delta**：根据 traits 调制幅度（warmth/curiosity 越高，变化越大），各维 clamp ±0.3。
- **半衰期衰减**：默认 1200 秒（可配 60–7200），每次 snapshot 按 `2^(-elapsed/half_life)` 衰减回基线。**这意味着"现在很兴奋"会自然平复，而不是永远卡住。**
- **Resonance（共鸣）**：当你说到某段共同经历时，关联的情绪体验会增强当前情绪（权重 0.2）。
- **Correction（修正）**：可对某次情绪体验做方向/强度/归因修正，供用户校准。

**情感快照（`EmotionSnapshot`）** 输出给客户端的东西，就是驱动立绘表情的信号：

```
effective_state.mood  →  calm / cheerful / concerned / curious / tired
state.valence/arousal/control  →  连续量（可做平滑表情插值）
reaction               →  neutral / joy / concern / curiosity / quiet
quiet_requested        →  是否请求安静陪伴
```

### 3.3 慢变量（`slow_variables.py`，30 天半衰期）

长期关系指标，缓慢演化：

- `warmth` 关系温暖度、`familiarity` 熟悉度、`engagement` 投入度、`tone_hint`（warm/cautious/neutral）。
- 由窗口内情绪体验（衰减×置信加权）与记忆数量共同决定。
- 影响提示词和参与决策——关系越熟，越会主动。

### 3.4 内心生活（`inner_life.py`，自发活动）

这是"它不聊天的时候在干嘛"：

- 6 种内在活动（restless 躁动、curious 好奇、concerned 关切、reflective 沉思、content 满足、resting 休息），各有模板文本。
- 由 idle 时间、traits、mood、慢变量算出 **urge（冲动强度）**，超过阈值（0.65）才成为"想说话"的候选。
- 可配模型版：调模型给出活动判断，置信度 ≥0.55 才混合。

---

## 4. 记忆层：你们一起的故事（✅ 完整且端到端实测）

这是 Neuro-sama "真的记得你" 的核心，也是本项目最扎实的一层。

### 4.1 四种记忆 + 完整来源链

| 类型 | 含义 | 例子 |
|---|---|---|
| `episode` | 共同经历 | "今天下午主人带我去公园散步，遇到一只橘猫" |
| `fact` | 关于主人的事实 | "主人喜欢深夜工作，常在凌晨一点后还醒着" |
| `preference` | 偏好 | "喜欢听爵士乐，尤其 John Coltrane 的《Blue Train》" |
| `relationship` | 关系认知 | "认识三个月了，他叫我小蓝，我叫他阿宅" |

每条记忆都有：`source_event_id`（来源事件）、`revision`（版本）、`valid_from/to`（有效性）、`importance`、`confirmed`、`pinned`、`tags`、`semantic_key`。**绝不可伪造来源**——写记忆必须引用真实事件，否则 422。

### 4.2 端到端链路（全部实测通过）

```
写记忆 → 嵌入(TEI bge-base-zh-v1.5 768维) → 语义召回 → 注入 prompt
      → 模型精确回答 → 自动捕获新记忆 → 巩固成摘要
```

- **写入**：4 条记忆全成功，stats 从 0 → 4 条全 active。
- **嵌入**：自动建索引，`stored_vectors=4, indexed_records=4`。
- **语义召回**：`semantic-recall` 走 TEI reranker（bge-reranker-base），"主人平时几点睡"→ 精确命中"深夜工作"事实第 1 名；"喜欢什么音乐"→ 命中爵士偏好第 1 名。
- **对话注入**：问"你还记得我喜欢听什么音乐吗？"→ 模型回答正确，`memory_refs` 注入全部 4 条 + 完整 `memory_versions`。
- **自动捕获**：对话后 auto-memory 自动捕获"偏爱爵士乐《Blue Train》"成新记忆，再巩固成摘要。stats 从 4 → 8 条。

### 4.3 对话主链路的记忆召回

普通对话用**词法+衰减**（`local_lexical_cjk_ngrams_salience_decay`），快且不依赖向量服务；`semantic-recall` 用**语义向量+TEI 重排**，更准。对话主链路（非 proactive 且嵌入启用且 participation.respond）走 `prepare_query` 嵌入查询——**即正常聊天就自动做语义召回**。

### 4.4 知识图谱（`knowledge_graph.py`）

记忆之间的实体关系图：`graph_entities` / 自动派生的 relation 边 / 别名。BFS 按 hops 扩展，边必须有 ≥evidence 绑定，死源/版本不匹配自动过滤。你可以问"我和小蓝都认识谁？"这类关系性问题。

### 4.5 衰减与巩固（`memory.decay`）

- 按类型区分半衰期：routine 30 天 / significant 180 天 / stable 365 天，各带 floor。
- `permanent`（永久）记忆在语义相关时自动强化（`reinforcement_min_coverage` 0.5）。
- 巩固：积累 20 条待整理对话 → 批处理 32 条 → 摘要（输入 ≤4000 token / 输出 ≤400）。
- token 账本：日限额 1000000 / 任务 5000，共享对话与自动记忆。

---

## 5. 主动发起：它会找你（✅ 完整）

Neuro-sama 直播时会自己找话接。这里由 **proactivity 后台循环**驱动，三个来源：

1. **`schedule_next_proactive` 工具**：让角色自己安排"等 X 秒后找你聊"（600–86400 秒）。
2. **`schedule_task` 工具**：安排一个任务（标题 + 延时 60–2592000 秒），到点提醒。
3. **`inner_life` 自发 urge**：内心冲动超过阈值自发发起。

全部门控（任一不满足就安静等）：policy enabled → 版本匹配 → candidate 未过期 → 节点 online 且有能力 → attention 非 stale → **interaction_mode==chat**（否则等用户）→ 无迁移 → lease 未过期 → 用户在场/未锁屏/非会议 → 未安静请求 → `initiative≥0.65` → `energy≥0.35` → `social_battery≥0.25` → 冷却 → 日限。

> 这套门控的意义：**它不会在你忙、开会、锁屏时硬插话**，但会在你空闲时真的来找你。

---

## 6. Neuro 式人设映射方案（可执行的落地清单）

把你的 Neuro 人设"装"进系统，分四步：

### 步骤 1：写一张角色卡（`chara_card_v2`）
把 Neuro 的说话风格、性格、口癖写进 `description/personality/scenario/first_mes/mes_example/alternate_greetings`，用 WebUI"人格卡导入"或 `POST /personality/import` 导入。**traits 设成她的样子**（建议：warmth 0.6 / curiosity 0.8 / assertiveness 0.7 / playfulness 0.9 / initiative 0.8——毒舌高玩心高主动）。

### 步骤 2：接上情感引擎（已有，直接可用）
情感引擎会自动按你的话评估她的反应，输出 `EmotionSnapshot`。**你只需要让客户端消费它来驱动立绘表情**：
- `effective_state.mood` → 选择表情图（平静/愉快/好奇/关切/疲惫）
- `state.valence/arousal` → 平滑插值表情强度（比如嘴角上扬幅度）
- `quiet_requested` → 切换到"安静陪伴"形态

### 步骤 3：喂记忆（已有，直接用）
她会在对话中自动捕获、嵌入、召回、巩固记忆——**不需要你写任何记忆代码**。你只需要确保 `[embedding]`（本地 TEI 已配好）和 `[memory]` 开着，然后正常聊天即可。想预置"她记得你"，用 `POST /memories` 写几条带真实 `source_event_id` 的初始记忆。

### 步骤 4：接上语音与立绘（这两层需要你在客户端/部署侧补）

---

## 7. 部署/集成层缺口（非架构缺口）

### 7.1 语音（TTS 未启用）

实现完整但默认关闭：`virtual-life.toml` 没有 `[speech]`/`[voice]` 段 → 用默认值（disabled）。要启用：

1. 解包已下载的模型：`.data/speech/downloads/kokoro-multi-lang-v1_1.tar.bz2` → `.data/speech/models/kokoro/`
2. 装 sherpa-onnx（独立 Python 3.12 venv 于 `.data/speech/venv/`，与主 API 解释器隔离）
3. 写配置：`[speech] enabled=true, model_dir=".data/speech/models/kokoro", model_manifest=...`（manifest 默认路径 `tools/speech/kokoro-v1_1.manifest.json` 在仓库中不存在，需自建或指定）
4. 可验证：`uv run python examples/voice_live_test.py --tts-config /path/to/tts-test.toml`

> 注意：`voice_*` 会话/轮次/SSE/播放/插话 API 全部已实现；缺的只是"把 sherpa 模型跑起来"这一步。服务端启动**不会自动下载权重**。

### 7.2 视觉立绘（只挂钩不渲染）

`avatar.render` 是 `OUTPUT_CAPABILITIES` 之一（`coordinator.py:30`），只用于输出/迁移门控。**没有任何 Live2D / sprite / 立绘渲染器**——WebUI 的 `companion-avatar` 是一个 ✳ emoji。

但挂钩已经就绪：`DialogueResult.emotion: EmotionSnapshot`（含 `effective_state.mood` / `Affect` / `quiet_requested`）随每次对话返回。你只需在客户端写一个"emotion → 表情"的渲染器（Live2D 模型控制器或 sprite 切换），就能让立绘跟着她说话动起来。**服务端已经把这个信号给到你了。**

---

## 8. 结论

| 能力 | 状态 | 备注 |
|---|---|---|
| 人格层 | ✅ 完整 | 角色卡导入 + traits 驱动 + 进化需采纳 |
| 心理层 | ✅ 完整 | Affect 三维 + 半衰期衰减 + 规则评估 + 慢变量 + 内心生活 |
| 记忆层 | ✅ 完整 | 端到端实测：写→嵌入→语义召回→注入→回答→捕获→巩固 |
| 主动发起 | ✅ 完整 | 三源 + 全门控，不会在你忙时打扰 |
| 语音 | ⚠️ 部署缺口 | 实现完整，需解包模型 + 装 sherpa + 配 [speech] |
| 视觉立绘 | ⚠️ 集成缺口 | 无渲染器，但 emotion 信号已随 DialogueResult 返回 |

**一句话**：把 Neuro 的人设写进一张角色卡，把 emotion 快照接到立绘上，把 sherpa 模型跑起来——你就有了一只"会记住你、会因你而改变情绪、会在你空闲时主动找你"的持续在线虚拟主播。架构不需要改，缺的是部署和客户端集成。
