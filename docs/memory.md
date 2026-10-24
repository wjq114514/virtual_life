# 长期记忆：持续相处与成本控制

同一位陪伴者的记忆由 companion_id 关联，统一持久化。设备迁移携带当前活动和 memory_refs，目的节点按需召回相关经历。显式写入、召回与提示预览在本地完成；完成的对话默认进入后台自动整理，同一次模型提取生成稳定认识、摘要和知识图谱。

## 保存什么

| 层次 | 当前实现 | 使用方式 |
| --- | --- | --- |
| 工作上下文 | 陪伴状态中的 activity、context、interaction_mode | 接续书籍、段落和话题；通过 revision 与租约控制更新 |
| 共同经历 | episode 记录 | 保存一起做过的事、时间线索和来源 |
| 稳定认识 | fact、preference、relationship 记录 | 保存习惯、偏好和关系；用 semantic_key 定位，显式更正并检查 revision |
| 派生摘要 | 带来源版本的批次摘要 | 自动整理生成，源记录变化后剔除过时结果 |
| 关系图谱 | SQLite 规范实体、显式别名与有来源支持的关系 | 去重证据、有界多跳与预算内融合召回 |
| 向量 | 独立 Embeddings 模型与 SQLite 缓存 | 可选后台索引与语义融合召回 |

持久化记录具有 importance、confidence、confirmed 和 pinned。召回的排序同时考虑文本相关度、重要性、固定状态、确认状态和时间；默认用有下限的指数衰减降低重要度的召回权重。当前不会仅因记录年久自动删除。

稳定人格由独立的 PersonalityService 保存；短期情绪与精力有有效期。角色卡描述和风格示例不写成共同经历，设备心跳也不自动更新情绪。PersonalityPromptBuilder 按需读取当前状态与本地召回，保留实际入提示的 memory_refs；归档卡和未采用的记忆不占提示预算。人格、状态和真实经历的合同见 [人格设计](personality.md)。

记忆可显式保存，也可从完成的对话自动积累。source_event_id 必须引用属于当前伙伴的真实事件；提供 source_node_id 时必须与事件节点一致。不能凭空填写来源，也不能引用无伙伴归属或其他伙伴的事件。原始 node.observation 没有伙伴归属，暂不能直接作为此接口来源；可使用属于伙伴的 companion.context_updated 或完成对话事件。自动提取仅接受用户消息中的精确证据片段，置信度上限为 0.85，默认未经确认、不固定；手动记录、手动编辑或确认过的记忆受保护。

episode 按 episode_key（未填写时按 source_event_id）识别同一次经历，不使用 semantic_key；同样正文来自不同事件时保留不同记录。稳定认识的规范化相同正文可以合并来源；同一 semantic_key 的不同正文必须走显式 corrections 接口，原 POST 写入返回 409 memory_correction_required。重复文本明确关联另一个 semantic_key 时仍返回 semantic_key_conflict。重复写入省略时间字段会保留已有时间；显式提交不同时间会返回冲突，episode 的未知发生时间可补充。

记录包含 revision（初值 1）、occurred_at、episode_key、valid_from、valid_to 和 supersedes_id。时间接受带时区的 RFC3339，服务端统一为 UTC；未知时间为空，旧库迁移不会从 created_at 猜测发生时间。来源事件的记录时间也不会自动当作经历发生时间。默认召回只采用 active 且在有效区间内的记录，valid_from 为包含下界，valid_to 为不包含上界。

POST `/v1/companions/{id}/memories/{memory_id}/corrections` 使用 command_id、expected_revision、content 和真实 source_event_id，另可提供 source_node_id、valid_from、valid_to、tags、importance、confidence、confirmed、pinned。更正适用于稳定认识，以 revision 比较后写入新记录，用 supersedes_id 指向旧记录；旧记录变为 inactive、revision 加一，新记录从 revision 1 开始。省略 valid_from 时取当前时间，省略 valid_to 或元数据时继承旧值。更正下界不得早于旧下界或晚于当前时间，且必须早于上界。

PATCH `/v1/companions/{id}/memories/{memory_id}` 使用 command_id、expected_revision 和至少一个元数据字段（tags、importance、confidence、confirmed、pinned、retention_policy），原位修改元数据并递增 revision；允许撤销固定/确认及降低置信度。并发版本不符返回 409。更正与元数据编辑的元数据、时间字段不接受显式 null；本轮不支持清空有效时间边界。

人格预览保留 memory_refs，并增加 memory_versions（memory_id 与 revision）。实际纳入的 MEMORY_DATA 带时间与版本；模型调用前和对话提交回复前均核验实际引用版本、当前有效性与自动事实证据。自动稳定认识至少需要一个有效的原始 episode 版本。更正、删除、元数据变更或有效期变化会阻止过时引用输出，不相关新增记忆不会废弃已经生成的回复。调用前核验失败释放预留且不调用模型；调用后核验失败丢弃回复，已知实际用量仍结算。

记忆列表包含 inactive 记录。DELETE 删除所选记录、词法索引及来源，并保存来源墓碑阻止后续自动提取再生；历史命令响应快照、运行事件及自动整理证据仍保留，供审计与幂等重试返回历史结果。重放该命令不会重新创建已删除记录。完整清除相关原始与派生内容需要后续专门接口。

## 设备状态与共同经历

设备轻客户端的 CPU、内存和端点可达性保存在节点最新快照中；显著变化与显式操作阶段进入运行事件。采样和上报不调用模型，不直接写入 MemoryService。状态合同见 [设备状态跟踪](device-state-tracking.md)。

后续经历整理可以按 `operation_id` 聚合一次维护过程，只在有陪伴意义时保存一条 episode，例如“一起修好了家庭网络”。来源引用实际操作事件，稳定设备信息则使用 semantic_key 定位事实，并通过显式更正更新。缺少阶段报告或元数据过期不能被总结成“已经完成”。原始指标、同阶段进度更新和重复网络抖动不需要占用长期召回预算。

运行事件当前持久保留；其短期保留与聚合机制仍需实现，并同时提供事件 cursor 过期后的快照恢复。长期共同记忆沿用现有持久策略，保留重要经历和用户确认的认识。

## 召回预算

MemoryRecord 的 layer 为 episodic（episode）或 semantic（fact/preference/relationship）。召回请求可用 layer 选择其中一类，缺省时查询两类；过滤先于候选条数限制。episodic 查询可携带摘要，semantic 查询可携带关系。RecallResult 新增 summaries、relations，各自保留来源版本；这些派生项也计入 max_items 和格式化上下文预算。

默认召回最多 8 项、800 个估算 token；可配置上限为 50 项、4096 个估算 token。预算包括 context_text 的标题、类型、记录 ID、版本、时间、内容与分隔符。放不下的整项会被跳过；items、summaries 和 relations 共同对应 context_text。派生内容先尝试使用至多 60% 预算，原始记录使用剩余额度；图谱匹配会帮助召回相关支持记录。

本地词法索引支持拉丁词和中文等文字的字符片段。候选来自文本匹配、重要记录与近期记录，避免检索只覆盖最近一段对话。它对同义表达和隐含关系的覆盖有限；长期效果还需要真实相处数据评估。TokenEstimator 可以替换成所接模型的 tokenizer，默认估算器只控制近似 token 数。

人格预览在召回预算外再限制完整提示；其格式化记忆数据块也计入独立记忆预算。默认人格核心上限 1200、完整提示策略上限 4096，客户端默认请求 3072 个估算 token。人格核心、有效状态与当前消息必须完整容纳；可选活动、记忆、表达示例与近期对话按余量纳入。预览不预留模型账本，也不写记忆。DialogueService 将提供商固定指令与消息包装一起估算，按输入估算加输出上限预留，并用最终提供商 usage 结算。

可选向量模型已接入候选融合：背景索引有效记录，显式语义召回和实际对话准备查询向量；普通召回与预览仅读查询缓存。词法、图谱和向量各自排序后通过 RRF 融合，继续执行来源、版本、层级、条数和完整上下文预算约束。规范实体与显式别名、独立来源聚合、有界多跳同样参与召回；配置与边界见 [向量记忆与图谱](vector-memory-and-graph.md)。

## 整理和模型账本

自动整理默认在同一伙伴积累 4 次完成对话或最旧事件等待 60 秒时触发；持久队列与完成事件、回执原子提交，网络调用在事务外进行，来源、派生结果与整理回执再次核验并原子落库。API 的 automation/jobs 展示执行进度，automation/run 可提前处理一批。详细流程、冲突、图谱与恢复边界见 [自动记忆](automatic-memory.md)。

原有 consolidation-plan 仍是针对任意显式 episode 的规划接口：20 条阈值、单批 32 条、估算输入上限 4000、输出上限 400。该接口不领取或执行自动队列，也不自动整理旧历史。

模型预算默认为每个运行时数据库每天 10000 个输入加输出 token、单任务 5000 token。所有陪伴者共享日额度；各自用量仍可查看。额度按任务首次预留时的 UTC 日期记账。这是 token 数量账本，金额需要后续结合提供商价格计算。`embedding.local=true` 的本地 TEI embedding 不进入该付费账本，但仍记录 `embedding_jobs` usage、日志和单任务输入上限；详见 [向量记忆与图谱](vector-memory-and-graph.md)。

| 服务方法 | 输入与行为 |
| --- | --- |
| reserve_model_tokens | companion_id 及 `{task_id, purpose, input_tokens, output_tokens}`；预留额度，重试返回 new_reservation=false |
| settle_model_tokens | companion_id、task_id 及 `{input_tokens, output_tokens}`；记录提供商实际用量 |
| release_model_tokens | companion_id、task_id；释放未结算任务的预留，已结算用量不能释放 |
| budget_snapshot | companion_id，可选 UTC day；返回运行时总额度及各陪伴者用量 |

task_id 在数据库中唯一，终态任务不能换一天重新预留。超出预留的实际用量、释放后才收到的用量仍会入账；用量超额会影响后续预留。账本只承担记账。DialogueService 另用持久命令表协调执行；同一 command_id 不重复调用提供商，崩溃留下的运行中命令不自动重发。

网络中断、usage 缺失或无法判断是否执行时保留预留；已知提供商 usage 即使文本格式失败或回复失权也入账。仅在确定未发送的失败时释放。默认账本没有自动清除未知预留的策略，避免把可能已付费的任务算成免费；未来管理入口需要保留核对依据。

WebUI 展示记录、来源、固定/确认状态、预算召回，以及运行时与当前伙伴用量；记忆页还展示自动整理进度、近期摘要与关系。用户也可引用完成对话的 source_event_id 手动补充记忆。临时设备状态与原始运行事件仍沿用各自存储边界。

这些方法供后端任务集成调用。HTTP 的 memory policy、stats 和 consolidation-plan 接口展示策略与账本；目前没有开放客户端修改模型账本的路由。

## 配置与扩展

API 工厂可以接收记忆策略、token 估算器和迁移策略。例如在自己的启动模块中：

```python
from virtual_life.api import create_app
from virtual_life.memory import MemoryPolicy

app = create_app(
    memory_policy=MemoryPolicy(
        default_recall_tokens=600,
        default_max_items=6,
        daily_model_token_limit=8000,
        per_task_model_token_limit=4000,
    )
)
```

客户端未填写召回规模时使用注入策略的默认值。ModelProvider、MemoryConsolidator、PerceptionProvider 和 SpeechProvider 定义在 ports.py。ModelProvider 已接入单次文本对话与自动整理，均遵循调用前预留、调用后按实际 usage 结算的约定。自动整理配置见 .env.example 与 [自动记忆配置](automatic-memory.md)。

后续可增加失效摘要重建、ANN 索引与程序经验。当前向量保存在 SQLite，使用 `float32` BLOB、numpy 批量打分和有限候选的余弦扫描；旧 JSON 向量会在后台有限回填。图谱检索已有证据路径，不生成新的推理事实。精确证据校验不等于语义正确性证明，自动积累效果仍需真实长期数据评估。

## 有下限的记忆衰减

`retention_policy` 可为 `auto`（默认）、`permanent`、`stable`、`significant_episode`、`routine_episode`；写入可指定，更正与元数据编辑可省略继承，但不能传 null。记录同时返回存储策略与 `effective_retention_policy`。固定记录的有效策略总是 permanent。auto 对普通经历采用 routine_episode，对稳定认识采用 stable；只有 fact 的规范语义键具有已知生日槽位后缀（birthday、birth_date、date_of_birth、生日、出生日期）且来源证据支持该事实时才自动保护为 permanent，不从正文猜测生日。显式策略覆盖自动判断，固定保护优先。

衰减系数 `D(t) = floor + (1 - floor) * 2^(-t / half_life_days)`，召回使用 `importance * D(t)`。日常经历默认半衰期 30 天、下限 0.1；重要经历 180 天、0.3；稳定认识 365 天、0.5；permanent 系数为 1。这只改变显著性，不删除正文，也不改变事实有效期、confidence 或 revision。召回结果可报告 `decay_factor` 和 `effective_importance`。

只有成功完成对话、实际进入最终提示上下文且与查询相关的支持记忆才强化；覆盖率阈值默认 0.5，每个记录每个 UTC 日至多重置一次衰减锚点。后台整理、浏览、普通/语义召回与提示预览不强化；失败、静默和丢弃的对话不强化。更正创建新记录并从新锚点开始。

召回可传 `semantic_key`，按规范化后的槽位精确查找，绕过普通候选数量上限，仍遵守来源、层、有效时间、条数与 token 预算。永久保护候选有独立上限，不能保证所有永久记录都进入每次提示。关闭 `[memory.decay].enabled` 时沿用 `recency_half_life_days` 的旧排序。

向量排序使用小幅加性的显著性先验（`semantic_salience_weight` 默认 0.05），不会把整项相关度乘以衰减系数，也不改变原始余弦相似度阈值。相关永久来源记录优先于摘要和关系尝试进入提示，但仍可能因提示预算不足被省略。生日保护仅保留已有证据支持的槽位事实，不会创建定时生日提醒。
