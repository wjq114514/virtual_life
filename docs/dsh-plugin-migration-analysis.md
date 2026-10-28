# 把 virtual_life 做成 DeepSeek Harness 插件：可行性与选型分析

2026-10-27

## 0. 结论速览

**不要重写。** 本项目是 Python 单体后端 + 多平台设备客户端，价值在领域逻辑（记忆 provenance/revision、多节点租约迁移、屏幕帧管线、双向语音会话协议、情感评估/慢变量），而不在通用 agent 能力。DSH 侧 90% 的通用能力已有现成实现，社区市场里另有大量可用件。

正确做法分四层，按投入产出排序：

1. **桥接层（MCP）**：写一个薄 MCP server 包住现有 FastAPI，现有每个 REST 路由自动变成 DSH 工具。约 50 行胶水代码，零 Python 移植。DSH 官方 `@deepseek-ai/dsh-mcp-client@0.2.1-rc.3` 的 9 个 peer 依赖在本 profile 全部已满足，可直接装。
2. **原生替代层**：模型路由、LLM 客户端、工具注册表、会话存储、KV 存储、prompt 组装、附件、图像 offload、会话检索、标题——全部丢弃本项目的对应模块，用 DSH 内置件。
3. **社区替代层**：ASR/TTS、embedding+rerank、记忆召回预算与可解释性、屏幕采集端、手机伴侣端、IM 推送——装市场插件，改配置。
4. **自研插件层（只留 4 件）**：主动参与驱动、设备节点注册表与房间迁移、场景/NSFW 分类器守卫、屏幕帧管线。这四件在 3552 个社区插件里**零替代**。

---

## 1. 事实底座：当前环境核查结果

### 1.1 本 profile 实际状态

| 项 | 值 |
|---|---|
| DSH 版本 | `0.2.1-rc.3` |
| 插件总数 | 195 条 Loader 行 |
| `dshmarket` 市场插件 | 已安装并启用（`1.70.2`），当前 region = `china` |
| 社区目录规模 | **3552** 个插件（`cdn.jsdelivr.net/npm/dsh-plugin-catalog/index.json`，2026-10-27 抓取） |
| 已有 `@deepseek-ai/dsh-schedule` | ✅ 启用（提醒 + `ui-schedule` 设置页） |
| 已有 `dsh-session-query-sqlite` / `storage-domain` / `compaction-image-offload` / subagent / goal / session-projection | ✅ 启用 |
| `@deepseek-ai/dsh-mcp-client` | ❌ **未装**，但包在 DSH checkout 内存在，9 个 peer 依赖（attachment/llm/scope/subprocess/timeout/tools/mcp-resources/system-prompt/cordis）**全部满足** |
| `@deepseek-ai/dsh-experimental-voice-input-bundle` | ❌ 未装；官方 optional，availability = `installation` |
| `@deepseek-ai/dsh-experimental-agent-team-profile` | ❌ 未装；官方 optional，可装 |

> 注：`/dsh-market/*` 路由需要浏览器登录态，脚本无法直接取；上面的市场数据来自其上游公开 CDN 目录，与 Web Discover 页同源。

### 1.2 关键前提：官方 MCP client 是可用的最短路径

`@deepseek-ai/dsh-mcp-client` 支持 `stdio` 与 `streamable-http` 两种传输，工具注册为 `mcp__<serverName>__<tool>`，凭 `serverName` 命名空间隔离，慢/崩的 server 会拖慢启动或调用。virtual_life 已经有完整鉴权（`local-dev-token`）、`command_id` 幂等与 snake_case 合同，包一层 MCP server 几乎零改造。

---

## 2. 第一优先级：查市场可替代的

按「已有替代 → 直接省掉工作」逐项核对。

### 2.1 语音（`voice_*` / `speech_*`）— **一半可完全替代**

| 需求 | 现成方案 | 版本 |
|---|---|---|
| 端侧录音转文字 | **官方** `@deepseek-ai/dsh-experimental-voice-input-bundle`：本地 SenseVoice ONNX + speech Remote + Web 录音 UI，运行时首次使用自动下载 | `0.2.1-rc.3`（随 DSH） |
| 按住说话进输入框 | `dsh-dictation-hotkey`（走 DSH 自带 SenseVoice Remote） | `0.3.0` |
| 回复朗读（TTS） | `@memef1f1y/dsh-plugin-tts`（免费 Edge TTS 或自有 RVC 音色，分块渐进无缝播放）/ `dsh-speak`（系统语音，零依赖零 token 成本） | `0.6.7` / `1.8.2` |

**无法替代的缺口**：`voice_service.py` 是**双向语音会话协议**——`POST /sessions`、`heartbeat`、`turns`、SSE `events`、`interrupt`、`playback`、`segments/{sequence}/audio` 返回音频字节、24 KiB 请求体上限、reporter 凭据 403、`operation_id` 聚合。市场里的语音插件全是「听写」或「朗读」，**没有任何一个实现多轮语音会话服务**。→ 保留，走 MCP 暴露；DSH 插件只当传输层。

### 2.2 记忆（`memory.py` / `auto_memory.py` / `embedding_memory.py` / `knowledge_graph.py` / `rerank.py`）— **市场最拥挤，但无一等比**

| 需求 | 最接近的现成件 | 版本 | 备注 |
|---|---|---|---|
| embedding + rerank 管线 | **`@dsh-ai-providers/runtime` + `/localai` 或 `/dashscope` 或 `/ollama`**：共享凭据 + embedding/rerank/speech/image 服务 | `0.5.3` | **可直接退休 `embedding_memory.py` 的 embedding 客户端** |
| 向量+BM25 混合召回 | `dsh-kb-rag`：10 个工具，BM25+向量+重排，全本地 bge，单文件库，DOI 溯源 | `1.6.8` | 最接近 `embedding_memory.py` + `rerank.py` 的整体形态 |
| 召回预算 + 可解释性 + 审计 | `dsh-memory-gate`：SQLite+FTS5 claims，每次注入 ≤3 条/1200 字符，给出 use/verify/ignore 可解释决策与完整审计轨迹 | `0.15.0` | **与本项目「有界召回预算 + provenance + validity」最像的设计**，值得抄交互模型 |
| 主动零提示召回 | `@a9i5k4/dsh-auto-memory`：固定边界注入（前缀缓存友好）、三层自动沉淀、技能固化、词法兜底 + 可选语义档 | `3.3.17` | 对应 `auto_memory.py` 的形态 |
| 知识图谱可视化 | `dsh-memory-eternal`（Markdown 知识卡 + 交互式图谱，图例过滤/时间维/框选/合并）；`dsh-plugin-sirchmunk`（本地图谱后端管理） | `0.7.0` / `1.1.0` | 对应 `knowledge_graph.py` 的呈现层 |
| 角色向持久状态 | `@dsh-agentic-roleplay/state`：SQLite 会话状态 + 故事记忆与回忆 | `0.6.1` | engines `>=0.2.1-rc.2 <0.2.2`，**与当前版本吻合** |

**无法替代的缺口**：`revision` / `supersedes_id` / `valid_from`–`valid_to` / `semantic_key` 冲突 409 / 更正是 revision 比较后写新记录 / 自动提取仅接受精确证据片段且 confidence 上限 0.85 / 来源墓碑阻止再生 / 调用前后双向核验实际 memory_refs 版本。市场没有一件具备这套**来源版本化的可追溯记忆模型**——这是本项目真正的差异化资产。→ 保留，MCP 暴露；但**建议直接借 `dsh-memory-gate` 的「有界召回 + 可解释决策 + 审计轨迹」作为对外接口范式**。

### 2.3 屏幕视觉（`screen_*`）— **采集端可替代，管线不可替代**

| 需求 | 现成方案 | 版本 |
|---|---|---|
| 屏幕截图 + 描述/OCR 工具 | `dsh-youreyes`、`dsh-vision-web`、`dsh-vision-fallback`、`dsh-vision-free-eyes` | `0.1.0` / `0.10.0` |
| 桌面控制 + 离线 OCR | `dsh-desktop-sidecar`（Windows，UIA 控件树 + 进程内 GDI 截图 + 找字点击，10 工具） | `0.4.5` |
| 跨平台 computer use | `dsh-computer-use`（11 工具，AX 语义树 + GLM-4V-Flash 兜底，快照 TTL/应用白名单护栏）；官方 `@deepseek-ai/dsh-experimental-computer-use-*` + `dsh-cua-macos` | `0.3.1` |
| HarmonyOS 设备截图/安装/日志 | `dsh-hdc-bridge` | — |

市场里的 vision 插件本质是**按需图片问答**（让纯文本模型能调 VLM）。**没有任何一件实现持久帧管线**：`command_id` 幂等的 upload/frame/receipt、sha256 `exact_reuse` 服务端像素重验复用、持久队列 + worker fencing、`unavailable` 序号屏障、TTL 清理 + purge、`two_stage` / `single_pass` 双模式、`requested_profile` 下采样到 1536、reporter 凭据在 `/screen` 直接拒绝。→ 管线保留，**但采集端建议直接复用 `dsh-computer-use` / 官方 computer-use provider，而不是从 `clients/android-node` 重新做采集**。

### 2.4 场景分类器 / NSFW（`scene_classifier/`、`scripts/nsfw_server.py`）— **零替代，但生态位置很清晰**

全市场 3552 个插件里，`nsfw` 命中仅 1 个，且不相关（`dsh-tavern` 的「NSFW 模式」是玩法开关，不是内容审核）。

但市场有一整套**工具调用授权分类器**生态，位置和 `/v1/decide` 的用途高度重合：

- `@deepseek-ai/dsh-experimental-auto-review`（官方，per-tool LLM 授权审查）
- `dsh-auto-classifier`（tool 级 allow/deny 规则 + LLM 语义）
- `dsh-system1` / `dsh-jev-interceptor` / `@dnalec/dsh-auto-approve`
- `dsh-router-laya`（本地微调 7 题分类模型，ModernBERT 842MB，CPU 推理 <1s 选思考档位——**证明「小分类模型跑在 DSH 内」这条路是成熟的**）

→ 场景分类器是**唯一值得自研成 DSH 插件的独有资产**，而且挂点已成熟：注册到 `tools/pre-execute`。

### 2.5 情感 / 人格（`emotion.py` / `emotion_appraisal.py` / `slow_variables.py` / `personality*.py` / `focus.py` / `inner_life.py`）

**必须先研究的两个现成对标品**（在动手前）：

| 插件 | 描述 | 版本 |
|---|---|---|
| **`@m-rui/dsh-mate-companion`** | 「有生物钟、会真实睡觉、有自己的记忆和情绪的 AI 伴侣，**主动冲动以独立 turn 投递**」 | `1.5.4` |
| **`dsh-brainagent`** | 「受大脑启发的认知插件：情景、语义、程序与**情绪记忆**，带强化学习信号；**目标栈支持时间触发器**，好奇心驱动的自主网络研究与主动提议」 | `0.9.28` |

辅助：`@dsh-agentic-roleplay/core`（结构化世界、自主角色、持久会话、语音与图像生成）、`dsh-soul`（个性化面板 + `/soul`，向其他插件暴露 `soulConfig`）、`dsh-agent-persona`（按工作区路径/会话 ID 分发 system prompt 人设）、`dsh-tavern`（角色卡/世界书/剧情选项）。

**无法替代**：appraisal 评估理论、慢变量、人格进化的具体机制。但「主动冲动作为独立 turn 投递」这一投递方式已被 `dsh-mate-companion` 验证，应直接沿用而不是自造。

### 2.6 设备客户端 / 多节点（`clients/*`、`devices.py`、`coordinator.py`、`delegation.py`）

| 需求 | 现成方案 | 版本 |
|---|---|---|
| 手机伴侣端（配套原生客户端） | `dsh-plugin-mobile-gateway`：WebSocket + iOS 原生客户端 + 会话/实时事件/任务/Goal/审批/命令/文件传输 + 二维码配对 + 设备撤销 | `0.11.1` |
| 独立手机界面 | `@eternalloveone/dsh-palm`：扫码配对设备信任、SSE 实时同步、三通道完成通知（应用内/Web Push/Server酱·Bark·Telegram）、离线发件箱、PWA | `1.6.4` |
| IM 主动推送 | `dsh-msg-hub`（微信/QQ/飞书，定时任务唤醒 bot 推送到手机）；`dsh-im-companion` | `0.1.11` / `0.2.25` |
| NAS 设备桥 | `dsh-zspace`（极空间）、`@tnnevol/dsh-fnos`（飞牛）、`dsh-hdc-bridge`（鸿蒙） | `0.4.0` / `0.2.1-rc.2.12` |
| 跨节点任务 DAG | 官方 `@deepseek-ai/dsh-experimental-agent-team-profile`：Agent Teams 花名册 + 直接点对点消息 + 共享任务 DAG | `0.2.1-rc.3` |

**无法替代的缺口**：`coordinator.py` 的**节点能力模型 + epoch/lease/ownership 迁移**——「主人走到哪里她就出现在哪里，房间换了她没换」。DSH 的 agent-team 是 agent 对 agent，不是设备对 agent。→ **这是本项目最有价值的第一方插件候选**，因为它与 DSH 现有的一切正交。

### 2.7 直接删掉本项目对应模块的（DSH 原生已覆盖）

| 本项目模块 | DSH 替代 | 说明 |
|---|---|---|
| `model_provider.py` / `model_routing.py` | 原生 `ctx.llm` + `dsh-llm-retry` + `dsh-token-meter` | 可再加 `@ljwei-stak/dsh-model-router`（`0.25.2`）、`dsh-router-core`（`0.4.7`）、`dsh-multi-model-orchestrator`（`0.2.2`） |
| `tools.py`（进程内工具注册表） | 原生 `ctx.tools` | 16 工具 / 16 KiB 硬限消失 |
| `proactivity.py` 的定时部分 | 原生 `@deepseek-ai/dsh-schedule`（已启用）+ `@opendsh/dsh-plugin-scheduled-tasks`（`0.2.4`，无头 agent 会话 + cron） |  |
| 长程意图 / 目标 | 原生 `@deepseek-ai/dsh-goal` + `dsh-brainagent` 目标栈 |  |
| `storage.py` 通用 KV | `@deepseek-ai/dsh-storage` + `storage-json` / `storage-sqlite` / `storage-domain` | domain 表单带 schema 校验 + 事件 |
| 会话持久化 / 历史检索 | 原生 JSONL 持久化 + `dsh-session-query-sqlite`（FTS5）+ `dsh-session-projection-cache` |  |
| `personality_prompt.py` 组装 | 原生 `@deepseek-ai/dsh-system-prompt` + 可选 `@deepseek-ai/dsh-persona`、`dsh-agent-persona`、`dsh-soul` |  |
| 屏幕图/附件落盘 | `@deepseek-ai/dsh-attachment-local` + `dsh-spill-local` | 内容寻址，会话作用域 |
| 图像超预算处理 | `@deepseek-ai/dsh-compaction-image-offload`（**已启用**） |  |
| 每日/任务级 token 台账 | 原生 `dsh-token-meter` + `@angelyeye/dsh-cost-tracker`（CNY 峰谷价） |  |
| 会话标题 | 原生 `dsh-session-title` + `dsh-session-title-llm` |  |
| WebUI（`webui/`） | DSH Web 客户端本身 | **不要再发第二套 Web UI**，改做 Sidebar 面板 |
| 入站事件 → 新会话 | 官方 `@deepseek-ai/dsh-webhook` + `dsh-webhook-github`（可复用其模式做环境信号入站） |  |
| 工具调用审查 | 见 2.4 |  |

---

## 3. 真正需要自己做的（4 件）

按「独特性 ÷ 市场覆盖度」× 杠杆排序。

### 3.1 `virtual-life-mcp` — 桥接，不是重写　⚡ 第一件

薄 MCP server 包住现有 FastAPI。现有 REST 全部变成 DSH 工具：记忆读写与更正、人格提示预览、屏幕会话、语音会话、场景分类、节点注册与迁移。**约 50 行胶水，零 Python 移植，立刻解锁全部功能。**

配套（都已存在，纯配置）：
- `dsh-mcp-tunnel`（`0.1.0`）：出站-only 隧道栈（mcp-proxy + cloudflared Quick Tunnel）把本地 MCP server 暴露给远程 agent
- `@opendsh/dsh-plugin-setting-mcp`（`0.1.2`）/ `dsh-armory`（`2.4.0`）：设置面板里可视化管理 MCP
- `hermes-dsh-bridge`（`0.11.4`）：反方向——把 DSH 自身做成 MCP server，供 Hermes/Claude Code 等外部 agent 驱动

### 3.2 `dsh-virtual-life-proactivity` — 主动参与驱动

DSH 有 `ctx.schedule`（持久提醒、原会话投递）；virtual_life 有**参与规则、注意力、节流、冷却、呈现权限、proactivity 轮次预算**。前者是「到点叫我」，后者是「我判断现在该不该说话」。这个差集就是插件。

投递方式**直接沿用 `@m-rui/dsh-mate-companion` 已验证的「主动冲动作为独立 turn 投递」**，不要自造。

### 3.3 `dsh-virtual-life-nodes` — 设备节点注册表 + 房间迁移

本项目独有、与 DSH 一切正交的最高价值资产：节点能力声明（`vision.capture` 等）、epoch/lease/ownership、`subject_node_id`/`observer_node_id`、跨节点携带 activity + `memory_refs` 按需召回。建议做成带自有 `ctx.<seam>` 的插件 + Sidebar 面板展示「她在哪个房间」。

### 3.4 `dsh-virtual-life-guard` — 场景/NSFW 分类器守卫

全市场零替代的独有资产，且是最小可交付：一个 ONNX 分类器（`scripts/nsfw_server.py` 已在跑 `/classify` 与 `/v1/decide`），注册到 `tools/pre-execute`。市场已有成熟同类范式（`dsh-router-laya` 证明本地小分类模型在 DSH 内是成熟路径）。

### 3.5 屏幕帧管线 — 条件项

若决定 virtual_life 继续做视觉后端，则包成插件；否则 3.1 的 MCP 桥就够，**不必单独做插件**。

---

## 4. 建议目标栈（可执行清单）

### 4.1 立刻装进本 profile

```yaml
# cordis.patch.yml — 一行解锁全部现有功能
- id: mcp-virtual-life
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: virtual_life
    transport: streamable-http
    url: http://127.0.0.1:8038/mcp      # 需新增的薄适配层
    headers:
      Authorization: !!js '`Bearer ${process.env.VIRTUAL_LIFE_TOKEN}`'
```

其余（按价值排序）：

| 插件 | 作用 | 替代掉本项目的 |
|---|---|---|
| `@deepseek-ai/dsh-experimental-voice-input-bundle`（官方） | 本地 SenseVoice STT | `speech_worker.py` 的 ASR 端 |
| `@memef1f1y/dsh-plugin-tts` 或 `dsh-speak` | TTS 朗读 | 语音输出 |
| `@dsh-ai-providers/runtime` + `/dashscope`（或 `/localai`、`/ollama`） | embedding + rerank + speech + image | `embedding_memory.py` 的 embedding 客户端、`rerank.py` |
| `dsh-memory-gate` | 有界召回 + 可解释决策 + 审计轨迹 | 作为 `memory.py` 对外接口范式 |
| `@deepseek-ai/dsh-experimental-computer-use-*` 或 `dsh-computer-use` / `dsh-desktop-sidecar` | 屏幕采集端 | `clients/*` 的采集 |
| `dsh-plugin-mobile-gateway` 或 `@eternalloveone/dsh-palm` | 手机伴侣端 | `clients/android-app` |
| `dsh-msg-hub` | IM 主动推送 | 主动参与的手机侧出口 |
| `@deepseek-ai/dsh-experimental-agent-team-profile`（官方） | 跨 agent 任务 DAG | `delegation.py` 的部分形态 |

已具备、无需动作：`dsh-schedule`、`dsh-session-query-sqlite`、`dsh-storage-domain`、`dsh-compaction-image-offload`、subagent、goal、session-projection。

### 4.2 动手前必读

- `@m-rui/dsh-mate-companion` v1.5.4 —— 与本项目立意最接近的**现成产品**（生物钟、真实离线睡觉、记忆、情绪、主动冲动独立 turn 投递）。
- `dsh-brainagent` v0.9.28 —— 情景/语义/程序/情绪记忆 + 强化学习信号 + 目标栈时间触发器。
- `dsh-memory-gate` v0.15.0 —— 记忆预算与可解释性的现成范式。
- `dsh-kb-rag` v1.6.8 —— 全本地混合检索的现成范式。

### 4.3 风险与注意事项

1. **engines 版本钉死**：`@dsh-agentic-roleplay/*` 为 `>=0.2.1-rc.2 <0.2.2`，`@dsh-ai-providers/*` 为 `>=0.2.0-rc.2 <0.2.1 || >=0.2.1-0 <0.2.2`。当前 `0.2.1-rc.3` 都吻合，但**DSH 一升级就可能全线失效**，需要跟版本节奏。
2. **官方 vs 社区**：官方件（voice bundle、agent-team、mcp-client、webhook）是稳定依赖，可以放心把功能建在其上；社区件（memory-gate、tts、kb-rag）适合当参考实现与可选增强，不适合做硬依赖。
3. **安全面**：市场 3552 个插件里存在明确危险条目（如 `@beyondandsharp/dsh-plugin-notoken` 自述「移除鉴权步骤」）。安装前看市场卡片的 `capabilities` / `capabilityRedLines` 披露字段。
4. **不要做的**：模型路由、LLM 客户端、工具注册表、会话存储、KV 存储、prompt 组装、TTS/STT 管线、手机客户端、会话检索、附件存储、图像 offload、WebUI。**全部已有，重做即负债。**
5. **协议纪律仍适用**：走 MCP 桥不等于放松合同。snake_case、UTC ISO 8601、`command_id` 幂等、mutation 需 `command_id` 这些约束应在 MCP server 层保留，`docs/protocol.md`、`docs/openapi.json` 与 Kotlin `@SerialName` 仍要同步更新。

---

## 5. 一句话总结

> **用 MCP 把现有后端整体接进来（1 天），把语音/嵌入/采集/手机端让给现成插件（半天装配置），只把「主动参与」「节点房间迁移」「场景分类守卫」「屏幕帧管线」做成自己的插件——其余 90% 的工作量根本不存在。**
