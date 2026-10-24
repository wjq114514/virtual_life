# 类似项目调研

调研日期：2026-10-11。以项目主仓库、官方文档与源码为依据。以下是架构借鉴和本项目的设计判断；本轮代码独立实现。

搜索 `opertai` 时，结果主要指向 **Operit AI** 和 `Operitai` 副本。本轮选取主仓库 [AAswordman/Operit](https://github.com/AAswordman/Operit) 作为安卓方向参考。MaiBot 原地址会重定向到 [Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)。

| 项目 | 源码/文档观察 | 本项目采用的设计 |
| --- | --- | --- |
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | 自然聊天时机、用户理解、插件事件；记忆支持来源、画像、情景与事实管理 | 将安静/参与模式作为持续状态；长期记忆区分共同经历与稳定事实，保留来源与修正历史 |
| [Operit AI](https://github.com/AAswordman/Operit) | 安卓宿主能力、悬浮入口、模型抽象；记忆候选按配置间隔积累并分批处理 | 安卓宿主与节点协议分开；能力按实际权限报告；记忆整理采用有上限的批次与预算 |
| [AIRI](https://github.com/moeru-ai/airi) | 独立 server SDK、模块身份、认证、重连与心跳；插件可查询能力就绪状态 | 节点独立注册身份与能力，协议集中定义；连接恢复与事件回放清晰分工 |
| [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) | 前端通过 WebSocket 接入 Agent、ASR、TTS，支持对话中断与表情呈现 | 对话、视觉、语音使用适配器；输出携带节点、epoch 与租约信息 |
| [SillyTavern](https://github.com/SillyTavern/SillyTavern) | 角色描述、性格、场景、开场白、示例对话与可排序的提示组件 | 显式角色定义、预算内提示组合和风格材料；人格与有来源的共同经历分开 |

## 人格与酒馆角色卡

2026-10-11 阅读的官方资料：

- [Character Design](https://docs.sillytavern.app/usage/core-concepts/characterdesign/)：描述、性格与场景属于持续提示材料；开场白和示例对话影响表达，角色定义越长，留给对话历史的空间越少。
- [Prompt Manager](https://docs.sillytavern.app/usage/prompts/prompt-manager/)：组织与排序提示组件，区分角色材料、背景与实际对话。
- [Character Card V2 原规范](https://github.com/malfoyslastname/character-card-spec-v2/blob/main/spec_v2.md)：基础字段和新增元数据、提示覆盖、角色 lorebook、备用问候与 extensions；要求保留未知扩展资料。

本项目实现 V2 JSON 子集导入，映射基础角色材料并完整归档原卡。提示覆盖与 lorebook 不激活，因此不声称完整酒馆 V2 前端兼容。版本、短期状态 TTL、参与规则、记忆预算和跨设备人格共享属于本项目的生命体设计；没有从参考资料推断它们已经由酒馆实现。接口与取舍见 [基础人格](personality.md)。

## 实际检查的源码

固定 commit 便于后续复核；这些链接记录的是本轮研究的版本。

### MaiBot

Commit：`f9ec3670a3638d6a4708ee5253f7ffe2838c9415`。

- [A_memorix 说明](https://github.com/Mai-with-u/MaiBot/blob/f9ec3670a3638d6a4708ee5253f7ffe2838c9415/src/A_memorix/README.md)
- [memory_search_service.py](https://github.com/Mai-with-u/MaiBot/blob/f9ec3670a3638d6a4708ee5253f7ffe2838c9415/src/A_memorix/core/runtime/services/memory_search_service.py)
- [官方记忆管理文档](https://docs.mai-mai.org/manual/webui/memory-management)

适合借鉴的是来源、事实与派生记忆的分工，以及用户纠正错误记忆的入口。向量和图检索的成本、数据规模与长期召回质量需要结合自己的样本评估。本轮先实现可替换检索接口与本地召回。

### Operit AI

Commit：`dbf71916fae9750cfdc9f9a774f5a0fee56633fb`。

- [仓库模块说明](https://github.com/AAswordman/Operit/blob/dbf71916fae9750cfdc9f9a774f5a0fee56633fb/Repo_Arch_Basic.md)
- [ChatMemoryWindowPlanner.kt](https://github.com/AAswordman/Operit/blob/dbf71916fae9750cfdc9f9a774f5a0fee56633fb/app/src/main/java/com/ai/assistance/operit/api/chat/library/ChatMemoryWindowPlanner.kt)
- [MemoryAutoSaveScheduler.kt](https://github.com/AAswordman/Operit/blob/dbf71916fae9750cfdc9f9a774f5a0fee56633fb/app/src/main/java/com/ai/assistance/operit/api/chat/library/MemoryAutoSaveScheduler.kt)

窗口规划器会控制单次处理的消息规模；整理器积累候选后按时间与批次数量处理。由此采用“先积累，再按预算整理”的策略，降低每次交互重复抽取整段历史的费用。

### AIRI

Commit：`ec682e2413279aee49dc3d5bfa68192d958a41dc`。

- [server SDK client.ts](https://github.com/moeru-ai/airi/blob/ec682e2413279aee49dc3d5bfa68192d958a41dc/packages/server-sdk/src/client.ts)
- [插件能力协议](https://github.com/moeru-ai/airi/blob/ec682e2413279aee49dc3d5bfa68192d958a41dc/packages/plugin-sdk/src/plugin/apis/protocol/capabilities/index.ts)

客户端包含模块认证、身份注册、连接恢复与心跳，插件协议提供能力快照与等待能力就绪。对本项目而言，设备是承载感知与呈现能力的节点，ready 确认应在实际呈现能力就绪后发出。

### Open-LLM-VTuber

- [官方后端结构文档](https://github.com/Open-LLM-VTuber/open-llm-vtuber.github.io/blob/main/docs/user-guide/backend/structure.md)

所查文档描述 v1.0.0 的流程，适合参考模块边界；具体当前实现需随集成版本再次核验。

## 安卓系统能力

[Android 官方前台服务限制](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start)说明后台启动、悬浮权限与相机/麦克风权限具有条件限制。节点 SDK 负责连接、迁移协议与输出 gate；安卓宿主负责用户入口、原生权限、悬浮窗口和服务生命周期。能力声明须与宿主实际可用能力一致。

SDK 使用的 [kotlinx-datetime 0.6.1 官方说明](https://github.com/Kotlin/kotlinx-datetime/blob/v0.6.1/README.md)指出 JVM 实现依赖 java.time，Android API 26 以下需要 AGP 4.0+ 并启用 core library desugaring。宿主配置参照 [Android 官方 Java API 支持文档](https://developer.android.com/studio/write/java8-support#library-desugaring)。JVM8 字节码兼容性和安卓系统 API 可用性需要分别满足。

## 服务器与家庭设备状态

| 项目与官方接口 | 可复用的机制 | 本项目接入路线 |
| --- | --- | --- |
| [Prometheus node_exporter](https://github.com/prometheus/node_exporter) | 按 collector 提供硬件与操作系统指标；容器部署时需要明确实际采集范围 | 已有指标系统通过后续适配器接入，避免在每台服务器重复建设采集体系 |
| [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/) | 认证后订阅 `state_changed` 事件，实体状态更新由网关接收 | 将实体映射为观察对象，保留网关来源与实体采样时间 |
| [OpenWrt ubus](https://openwrt.org/docs/techref/ubus) | 命名空间下的 JSON 调用，可查询网络接口等状态 | 增加本地路由器适配器，补足单一端口探测无法判断的 WAN 等状态 |

本轮轻客户端采用标准库本地采样与配置式 TCP / HTTP 观察，上表适配器留待后续实现。主服务器连接、目标可达性、服务健康与人的操作分别表达；探测成功不能直接证明设备整体健康。具体合同见 [设备状态跟踪](device-state-tracking.md)。

## 文本对话提供商

2026-10-11 核对 [DeepSeek 首次调用](https://api-docs.deepseek.com/)与 [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)：根地址 `https://api.deepseek.com`、`POST /chat/completions`、非流式 messages/model/max_tokens、返回 prompt_tokens/completion_tokens。当前示例采用 `deepseek-flash`；短文本对话可显式关闭 thinking。提供商参数保持服务端配置，API 密钥不传到 WebUI。

本项目独立实现 HTTP 适配器；持久命令领取、预算预留/结算、来源事件和回复前后的节点输出校验属于本运行时设计。没有启用工具、自动摘要或付费视觉识别。子代理工具使用的 DeepSeek 模型标识与公共 API 的模型名称分属不同配置。

本项目新增的“准备迁移 → 来源释放/目的就绪 → 提交”协议、状态版本和输出租约，是围绕多设备连续陪伴设计的机制。迁移的是同一陪伴者的活动与输出权，共同记忆持续留在权威存储。
