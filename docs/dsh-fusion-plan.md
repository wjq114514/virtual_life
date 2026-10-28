# DSH × virtual_life 融合方案

2026-10-28。目标：在 DeepSeek Harness 里跑通「纯文本 AI 网友对话」最小闭环——主动聊天、内心、情感、记忆都要在，并且把调用链路收成一条，让后续工具调用和插件接入不再各说各话。

前置阅读：[DSH 插件迁移分析](dsh-plugin-migration-analysis.md)（选型与市场替代）、[MCP 桥接层](mcp-bridge.md)（已经落地的桥）。

---

## 0. 结论

**现状不是「没接上」，而是「接上了但有两个大脑」。** 桥接（106 个 MCP 工具）和主动投递插件都在跑，可是生成回复的模型有两个：运行时自己的 `model_provider`，和 DSH 的 agent loop。两条链路各自组装 prompt、各自调模型、各自的工具面，结果是主动消息被说两遍、工具能力取交集、上下文各存一份。

**推荐做法：倒置大脑 + 两段式回合契约。**

- 生成权交给 DSH（唯一的 agent loop，唯一的模型调用点）。
- `virtual_life` 保留它真正值钱的部分：状态机与不变量——人格/记忆 provenance、情绪评估与共振、内心活动、注意力与参与判定、节点租约、画面证据。
- 在「组装完 prompt、马上就要调模型」这个位置切开，做成 `prepare`（准备上下文，不调模型）和 `commit`（接收外部生成的回复，跑完副作用）两个可独立幂等的操作。

这样一条链路同时满足三件事：模型只被调用一次；记忆/情绪的写入不变量仍然由 Python 把关；DSH 侧的 106 个 MCP 工具与全部插件能力自动成为「她」的工具。

---

## 1. 现状核查

### 1.1 已经在跑的东西

| 组件 | 位置 | 状态 |
| --- | --- | --- |
| 运行时 | `backend/virtual_life/`，`127.0.0.1:8038` | `/healthz` 返回 `{"status":"ok","version":"0.1.0"}` |
| MCP 桥 | `backend/virtual_life/mcp_bridge.py`，`127.0.0.1:8039/mcp` | GET 返回 405（POST 才是正确入口），进程在 |
| 工具面 | 契约驱动，从 `/openapi.json` 生成 | 106 个工具，形如 `mcp__virtual_life__companions_companion_id_dialogue` |
| 主动投递 | `dsh/virtual-life-proactivity/` | 轮询 6 秒，把「准备好的话」用 `ctx.schedule.create` 投成独立 turn |
| 桥接 bundle | `dsh/virtual-life-mcp/` | 只声明 `@deepseek-ai/dsh-mcp-client` 一行 |

### 1.2 对话链路（当前）

`POST /v1/companions/{id}/dialogue`（`api.py:1615`）→ `DialogueService.send`（`dialogue.py:410`）→ `_send`（`dialogue.py:472`）。`_send` 是一条直线管道：

| 阶段 | 位置 | 做什么 |
| --- | --- | --- |
| 幂等占位 + 快照 | `dialogue.py:519-555` | 写入 `dialogue_commands`（pending）、发 `dialogue_received` 事件、情绪 observe |
| 组装人格上下文 | `dialogue.py:594-600` | `prompt_builder.preview(...)`，吃人格/状态/情绪/慢变量/内心 |
| 记忆召回 | `dialogue.py:603-643` | embedding 召回，随后重读快照校验版本 |
| 情绪共振 | `dialogue.py:645-671` | `emotion_service.resonate(...)`，重建 prompt |
| 画面证据 | `dialogue.py:672-707` | 单遍模式挂图，两段模式等文字证据 |
| 选模型 | `dialogue.py:708-767` | `model_router.choose` + 场景分类器 |
| 参与判定 + 预留额度 | `dialogue.py:790-810` | `preview["participation"]["respond"]`，`reserve_model_tokens` |
| 派发门 | `dialogue.py:818-830` | 再验一次上下文版本，防竞态 |
| **调模型** | `dialogue.py:921-1000` | `provider.stream/complete`，最多 1 次工具调用 |
| 提交 | `dialogue.py:1062-1178` | `record_dialogue_usage` → `dialogue_completed` 事件 → `emotion.complete` → `automatic_memory.enqueue` → 回执 |

第 9 步就是「大脑」，其余全是领域不变量。

### 1.3 主动链路（当前）

`ProactivityService`（`proactivity.py:196`）判断该不该说话（注意力边界、闲置、冷却、每日上限）→ 建 candidate（`proactivity.py:686-716`）→ `dialogue.send_proactive`（`dialogue.py:438`）**用运行时自己的模型把话生成出来** → `proactivity_output.poll`（`proactivity_output.py:146`）吐出 → DSH 插件 `index.js:117` 把它拼成 prompt 再投递：

```js
const prompt = `${cfg.delivery.promptPrefix}${item.assistant_message}`;
const record = await ctx.schedule.create(sessionId, { title, prompt, after_seconds });
```

---

## 2. 三个结构性问题

### P1 — 两个大脑，两次生成

同一个同伴，运行时的 `model_provider` 和 DSH 的 agent loop 各有一套 prompt 组装、各有模型配置、各有 token 账本。谁说的算不清楚，人格漂移不可控，记忆写入会出现两份来源。

### P2 — 主动消息被二次改写

`index.js:117` 把运行时写好的台词塞进 `<用户提示>` 位置，DSH 的 agent 看到的是「虚拟伙伴主动想说话：我注意到你停下来了」，于是**再生成一句**来回应。用户看到的是两条语气不同的消息，或者一条重复的意思。这不是配置问题，是投递语义错了：**已经生成好的台词应该作为「她的话」出现，而不是作为「别人对她说的话」**。

### P3 — 工具面割裂

- 运行时：`tools.py:176` 的 `ToolRegistry(max_calls_per_turn=1)`，内置只有 5 个工具（`ScheduleTool`、`ScheduleTaskTool`、`ListScheduledTasksTool`、`CancelScheduledTaskTool`、`DelegationTool`），一轮最多一次调用。
- DSH：106 个 MCP 工具 + 全部已装插件（schedule / subagent / web / 文件 / 检索……）。

想让「她有工具可用」，正确的做法不是往 `tools.py` 里继续堆，而是让运行时只保留**领域工具**（排程、注意力、委派），其余全部由 DSH 提供。

---

## 3. 三种接法

| | A. 运行时为脑，DSH 当壳 | B. DSH 为脑，运行时重写 | **C. 倒置大脑 + 两段式回合** |
| --- | --- | --- | --- |
| 模型调用点 | 运行时 | DSH | **DSH** |
| prompt 组装 | 运行时 | 重写到 TS | **运行时（保留）** |
| 记忆/情绪不变量 | 运行时 | 全部重写 | **运行时（保留）** |
| 工具面 | 运行时 5 个 | DSH 全部 | **DSH 全部** |
| 主动聊天 | 运行时决定并生成 | DSH 决定并生成 | **运行时决定，DSH 生成** |
| 迁移成本 | 约 0（已通） | 高，等于重写 `dialogue.py`/`memory.py` | **中，只在生成点切一刀** |
| 主要风险 | 永远两条链路 | 丢掉 provenance/revision 等资产 | prepare 挂起期间的租约与过期回收 |

A 已经在跑，但它注定是两条链路，P1/P2/P3 都解不掉。B 等于把项目最值钱的部分扔掉重做。**C 是唯一既保持单一模型调用点、又不重写领域逻辑的路。**

---

## 4. 推荐方案：倒置大脑

### 4.1 职责切分

```
DSH（唯一模型调用点）
 ├─ prompt 的「主体」：人格、记忆、情绪、内心、画面证据  ← 由 prepare 提供
 ├─ 工具调用：MCP 106 个 + 全部插件                 ← 由 DSH 的工具面提供
 ├─ 生成回复 / 主动开口
 └─ commit 回写

virtual_life（领域服务，不调模型）
 ├─ 幂等、租约、epoch、输出门
 ├─ 人格 / 记忆 provenance / 情绪评估与共振 / 内心活动 / 慢变量
 ├─ 参与判定（该不该回应）、注意力与冷却（该不该主动）
 └─ 事件日志、回执、token 账本
```

### 4.2 一次用户回合

```
用户 ──▶ DSH session
          │
          ├─ tool: dialogue_prepare(command_id, user_message, node_id, epoch, revision)
          │     运行时：占位 → 快照 → 组装 prompt → 召回记忆 → 情绪共振
          │             → 预留额度 → 派发门 → 返回 {status:"awaiting_generation", preview.context_text, ...}
          │
          ├─ (DSH agent 以同伴身份生成回复，可自由调用任意工具)
          │
          └─ tool: dialogue_commit(command_id, assistant_message, usage, tool_trace)
                运行时：再验上下文 → 结算额度 → record_dialogue_usage
                       → dialogue_completed 事件 → emotion.complete
                       → automatic_memory.enqueue → 回执
```

### 4.3 一次主动回合

```
运行时后台：注意力/闲置/冷却 → 该说话了 → 建 candidate → prepare（不生成）
     └─ 输出通道吐出「冲动」：{candidate_id, reason, boundary, inner_activity, context_text, memory_refs}
DSH 插件：轮询到冲动 → ctx.schedule.create 投一个独立 turn（prompt 是环境提示，不是她的台词）
DSH agent：以同伴身份生成开口 → dialogue_commit(proactive=true)
```

「什么时候说」留在 Python（注意力边界、冷却、每日上限是它独有的资产），「说什么」交给 DSH。P2 就此消失。

---

## 5. 接口设计

### 5.1 请求侧：`generator`

`DialogueRequest`（`dialogue.py:122`）增加：

```python
generator: Literal["internal", "external"] = "internal"
```

`internal` 是今天的行为，默认值。为避免升级时把已存在的 pending 命令的 fingerprint 全部作废，`_send` 里沿用 `access_id`/`presentation_id` 的既有写法（`dialogue.py:511-513`），在 `generator == "internal"` 时从指纹里剔除该字段。

### 5.2 `prepare`：`DialogueResult` 增加一个状态

`DialogueResult.status`（`dialogue.py:168`）从 `Literal["completed","silent","discarded"]` 扩为 `Literal["completed","silent","discarded","awaiting_generation"]`。

`awaiting_generation` 时返回的字段：

| 字段 | 含义 |
| --- | --- |
| `preview.context_text` | 完整组装好的 prompt（人格 + 记忆 + 情绪 + 内心 + 证据），拿来即用 |
| `preview.memory_refs` / `memory_versions` | 本次引用的记忆及其版本，`commit` 时用于最终校验 |
| `preview.participation` | `{respond, reason}`，为 false 时不生成 |
| `preview.token_budget` / `estimated_input_tokens` | 输入预算 |
| `emotion` | 当前情绪快照 |
| `accepted_event_id` | 用户事件 id，情绪评估的锚点 |
| `command_id` | 后续 commit 的唯一凭据 |

DB 行状态写 `awaiting_generation`，`result_json` 里存 `{"receipt": {...}, "pending": {snapshots, task_id, model, proactive}}`。`_read`（`dialogue.py:250`）对 `awaiting_generation` 只返回 `receipt`，不泄漏 `pending`。

### 5.3 `commit`

`POST /v1/companions/{companion_id}/dialogue/{command_id}/commit`

```json
{
  "assistant_message": "嗯，我看你停在那一页很久了。",
  "model": "deepseek-chat",
  "usage": {"input_tokens": 2140, "output_tokens": 38},
  "tool_trace": [{"name": "virtual_life_proactivity_status", "arguments": {}, "result": "ok"}]
}
```

约束：

- 只有 `awaiting_generation` 的行可以 commit，否则 `409 dialogue_not_awaiting`。
- 复用 `_same_context`（`dialogue.py:358`）与 `_validate_references`；上下文变了就 `409 dialogue_context_changed`，并释放预留。
- `usage` 缺省按 0 结算（外部生成不计入本地账本时不虚增）；给了就 `settle_model_tokens`。
- 超过 TTL（默认 300 秒）→ `409 dialogue_commit_expired`，释放预留，行置 `discarded`。
- commit 是幂等的：同一行已完成时返回同一份回执。
- `tool_trace` 只做记录（写进 `dialogue_completed` 事件的 `tool_trace` 字段与 memory log），不参与门禁。

### 5.4 状态机

```
pending ──(prepare)──▶ awaiting_generation ──(commit)──▶ completed
                            │                           └▶ discarded（上下文变了 / 被撤回）
                            └──(TTL 到期)──▶ discarded
```

`silent` 仍由 `internal` 路径产生；`external` 路径下 `participation.respond=false` 直接回 `silent`，不占配额。

### 5.5 为什么不用「直接传 prompt 给 DSH」

把 `context_text` 交给 DSH 之后，DSH 只有一次生成机会，**没有** dispatch 门之后的再校验。所以 prepare 必须自己完成「预留额度 + 派发门」，commit 再验一次版本。这两道门是现有代码里最不能丢的东西（`dialogue.py:818-830`、`dialogue.py:1064-1074`）。

---

## 6. DSH 侧需要什么

### 6.1 工具：不用新写

MCP 桥是契约驱动的（`mcp_bridge.py` 启动时读 `/openapi.json`）。**新增 `commit` 路由后重启桥，工具自动出现**，命名 `mcp__virtual_life__companions_companion_id_dialogue_command_id_commit`（超 64 字符会截断，以描述里的 `Upstream: POST /path` 为准）。这是这套架构最省的一点：运行时加接口，DSH 零改动获得工具。

### 6.2 人格：一个小 bundle 就够（已落地，见 `dsh/virtual-life-persona/`）

`dsh/virtual-life-persona/` 往 system prompt 里注册一个**动态 context 块** `virtual-life:companion`
（`order: 220`），内容为运行时的人格/状态/慢变量/内心活动快照，带标签、有界、且 `interpolate:false`
（保留 `{{char}}`/`{{user}}` 字面量，见 [dsh-troubleshooting-char-variable.md](dsh-troubleshooting-char-variable.md)）：

```yaml
- insert:
    - id: virtual-life-persona
      name: '@virtuallife/dsh-virtual-life-persona'
      config:
        baseUrl: http://127.0.0.1:8038
        token: local-dev-token
        companionId: proact-demo
        refreshMs: 30000
        maxContextChars: 4000
        order: 220
```

实际渲染的四块（数据，不是指令）：

```
[VIRTUAL_LIFE_COMPANION]   # definition{description,personality,values,boundaries,traits,speech_style,scenario}
[VIRTUAL_LIFE_STATE]       # mood/energy/social_battery/emotion
[VIRTUAL_LIFE_SLOW_VARIABLES]  # engagement/familiarity/relationship_warmth/tone_hint/sample_size
[VIRTUAL_LIFE_INNER_ACTIVITY]  # kind/trigger/content/salience/urge
```

人格的**内容**仍然来自运行时（`personality_prompt.py` 组装，`prepare` 返回的 `context_text` 里已经包含），
bundle 只负责把快照带进 DSH 的 system prompt。P1（`prepare`/`commit`）落地前，这是"身份声明 + 上下文承载"；
P1 之后由 `prepare` 的 `context_text` 取代。

### 6.3 主动：改投递语义

`dsh/virtual-life-proactivity/index.js:112-125` 从「把台词塞进 prompt」改为「把冲动塞进 prompt」：

```js
const prompt = [
  `[环境] ${item.reason_text}（边界：${item.boundary}）`,
  `你现在想主动开口。先调用 dialogue_prepare(candidate_id=...) 取回你的上下文。`,
].join('\n');
```

`preview.context_text` 由运行时给，agent 不需要自己编。

---

## 7. 分阶段落地

| 阶段 | 内容 | 可验证的结果 |
| --- | --- | --- |
| **P0 已完成** | MCP 桥、主动投递 bundle、迁移选型 | 106 工具可见；主动消息能投进会话 |
| **P1 切开生成点** | `generator=external` + `commit` 路由 + `_finalize` 抽取 | `prepare` 返回 `awaiting_generation`；`commit` 后事件、情绪、记忆副作用与 `internal` 路径一致 |
| **P2 消灭双重发言** | 主动链路改冲动投递；`virtual-life-persona` bundle | 用户只看到一条主动消息，语气与人格一致 |
| **P3 工具面归一** | 运行时只留领域工具；`tools.py` 的 5 个工具改为 MCP 工具 | 同伴可调用 DSH 任意插件工具，`max_calls_per_turn=1` 不再是上限 |
| **P4 收尾** | WebUI 改 DSH 面板；`internal` 路径降级为兼容模式 | 单一入口、单一账本 |

P1 是唯一有代码风险的阶段，其余都是接线与配置。

---

## 8. 不做的事

- **不重写记忆**。`revision` / `supersedes_id` / `valid_from`–`valid_to` / `semantic_key` 冲突 409 / 来源墓碑，这套 provenance 模型是项目最硬的资产，市场里没有等价物。
- **不重写情绪评估与内心活动**。appraisal、慢变量、内心活动是差异化，不是通用能力。
- **不把主动判定搬到 DSH**。`ctx.schedule` 只会「到点叫我」，不会「判断现在该不该说话」；注意力边界、冷却、每日上限留在 `proactivity.py`。
- **不发第二套 Web UI**。`webui/` 的浏览器页面在 P4 之后退化成调试面。
- **不在 `tools.py` 里继续堆工具**。DSH 的工具面已经是超集。

---

## 9. 一句话

> 桥已经搭好，缺的是把「谁调模型」这件事定下来：**让 DSH 当唯一的大脑，让 `virtual_life` 当唯一的记忆与情绪**，中间用 `prepare`/`commit` 切开。切开之后，主动聊天不再说两遍，工具面自动变成 DSH 的全集，新插件接进来不需要动运行时一行代码。
