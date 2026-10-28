# 注意力边界、连续专注与自触发

2026-10-14。沿用[屏幕感知与心流判定](screen-perception-and-flow.md)与[非视觉自触发](ambient-signals-and-emotion.md)的部分设计，补查一手资料后实现服务端闭环：宿主上报注意力快照与任务边界，本地规则记录参与决策，后台只消费已经入队的候选，允许时调用一次模型，再次核验后发布定向文字事件。候选另有一个本地来源：`inner_life` 生成的短时心理活动，详见[内在活动](inner-life.md)。

输入是宿主提供的行为线索，focus 仍是建议值；Linux 已有可选的[桌面状态自动采集](linux-attention-collection.md)，但没有截图、无障碍采集、键鼠监听、阅读/卡住分类器或经过校准的心流检测。接入信号的节点必须同时是当前呈现节点，不支持独立感知节点替另一个节点提供开口依据。

## 调研后的取舍

| 一手资料 | 能支持的结论 | 本次采用 |
| --- | --- | --- |
| [Google：Using Logs Data to Identify When Software Engineers Experience Flow or Focused Work，CHI 2023](https://research.google/pubs/using-logs-data-to-identify-when-software-engineers-experience-flow-or-focused-work/) | 关联行为序列可以识别专注工作，其中包含部分心流；不能据此测到心流的全部体验 | 保留 focused / unfocused / suspected_stuck / unknown；不输出“检测到心流” |
| [Iqbal 与 Bailey：Oasis，TOCHI 2010](https://www.microsoft.com/en-us/research/publication/oasis-a-framework-for-linking-notification-delivery-to-the-perceptual-structure-of-goal-directed-tasks/) | 可以把任务结构检测与通知调度策略分层，在任务边界安排通知 | 感知上报与生成解耦，只让明确的任务边界创建候选 |
| [苏黎世大学 HASEL：FlowLight](https://hasel.dev/publication/flowlight-reducing-interruptions/) | 作者报告基于电脑交互的可打断性提示在 449 人现场研究中减少了打断 | 复用宿主已有存在感与 quiet/focus 语义；不把该研究效果外推为本项目效果 |
| [Android Media projection 官方文档](https://developer.android.com/media/grow/media-projection) | 捕获会话有显式授权、生命周期与 onStop() 资源释放要求 | 采集继续由原生宿主管理，服务端信号接口不启动捕获 |

以下阈值与状态机属于本项目工程选择，尚未经过用户现场校准。相对原草案，本次作了以下取舍：

- 不自动把 companion 的 interaction_mode 改成 focus。观察到专注只否决本次主动输出，避免自动恢复时覆盖用户亲自设置的模式。
- 不把低输入率当成“可开口”。静止阅读、等待与卡住都可能没有键鼠事件；未明确知道可参与时保留 unknown，suspected_stuck 也不授权主动帮助。
- 首轮参与判断使用确定性本地规则；模型只负责已经获得参与机会的一小段措辞。
- 不实现“无回应后升级声音”与“自行判断紧急后越过静默”。第一版只有文字，quiet/focus/away 都不主动发言，用户主动对话仍走原接口。
- 被压住的候选持久化；失去时机后结束，不在用户稍后空闲时补发旧问候。

## 工具调度的延迟发言

开启 `[tools].enabled` 后，对话或主动轮次中的模型可调用 `schedule_next_proactive` 安排下一次发言，而不是立即开口。`delay_seconds` 受 `[proactive_schedule]` 的 `min_delay_seconds`/`max_delay_seconds` 约束；同一伙伴只保留一个待生效计划，新计划替换旧计划。计划持久化，重启后仍可恢复。

到点时服务端先检查最后一次用户对话是否早于 `min_idle_seconds`（默认 3600 秒）。若用户仍在互动，计划取消而不是延后。通过后计划转换为普通主动候选，继续经过注意力新鲜度、人格参与、token 预算、呈现租约和冷却/每日限额；任一 gate 失败都会把该候选记为 `suppressed`，计划保持终态 `fired`，不会绕过既有边界。新用户消息到达时，待生效计划在生成前取消。

事件为 `companion.proactive_scheduled` 与 `companion.proactive_schedule_cancelled`；`GET /v1/companions/{id}/proactivity/schedule` 返回 `status`、`due_at`、`reason`、`origin=schedule` 及取消/失败原因。该工具不会创建第二条对话路径，最终输出仍走同一主动 gate。

## 计划任务提醒

`schedule_task` 用于用户明确要求“稍后提醒我”或“之后提一下”的场景。工具持久化标题、可选详情和相对延迟（60 秒到 30 天），同一 `command_id:tool_call_id` 幂等。到点后任务转换为 `origin=task` 的主动候选，仍必须通过用户静默、注意力新鲜度、人格参与、预算、租约和冷却/每日限额；任一 gate 失败会把任务记为 `suppressed`，没有在线节点或静默条件不满足时记为 `missed`，不会绕过主动边界。任务只生成提醒，不执行外部动作。

计划任务可以有多个，不会替换待生效的 `schedule_next_proactive`。`list_scheduled_tasks` 可按 `active` 或具体状态列出任务；`cancel_scheduled_task` 可按任务 ID 或精确标题取消，活动任务同名时返回 `scheduled_task_ambiguous`，不会擅自选择。`GET /v1/companions/{id}/scheduled-tasks` 返回列表和终态原因（支持 `status` 过滤），`DELETE /v1/companions/{id}/scheduled-tasks/{task_id}` 可取消尚未执行的任务。事件为 `companion.scheduled_task_created`、`companion.scheduled_task_cancelled` 和 `companion.scheduled_task_missed`。

## 信号与接口

宿主注册完整能力时加入 `attention.observe`，文字输出仍需要 `text.display`。一个 node 仍由一个进程负责完整能力与 context 写入。心跳继续完整替换 context；注意力单独存储，心跳不会清空它，也不会生成候选。

| 接口 | 行为 |
| --- | --- |
| `POST /v1/nodes/{node_id}/attention` | `{command_id, sample}`；原子保存快照、必要的观察事件、候选与命令回执 |
| `GET /v1/nodes/{node_id}/attention` | 当前快照与 stale；无样本时 sample=null、stale=true |
| `GET /v1/nodes/{node_id}/focus` | 连续专注建议快照、保护状态与最近已完成专注段；读取不写状态 |
| `GET /v1/companions/{id}/proactivity` | 默认关闭的策略、独立 revision |
| `POST /v1/companions/{id}/proactivity` | `{command_id, expected_revision, policy}`；完整替换策略，CAS 与幂等 |
| `GET /v1/companions/{id}/proactivity/decisions` | 按候选 sequence 分页查看当前决策；`latest=true` 取最新 N 条后升序返回 |
| `GET /v1/companions/{id}/proactivity/output` | 当前 Web access 的实时主动文字页；重新检查呈现条件，省略 cursor 只建立基线 |
| `GET /v1/companions/{id}/proactivity/schedule` | 当前或最近一次 `schedule_next_proactive` 计划及其终态 |
| `GET /v1/companions/{id}/inner-life` | 当前心理活动、需求、冲动与本地上下文摘要 |
| `GET /v1/companions/{id}/inner-life/activities` | 分页查看心理活动历史 |
| `GET /v1/companions/{id}/dialogue/{command_id}` | 读取历史生成回执，不授予当前输出权限 |

全部接口沿用 Bearer 验证。决策表的 sequence 是候选创建顺序；queued/running 会原位更新，刷新时应重新查询相应页面。要连续跟踪转换，使用 `/events` 或 WebSocket 的 `companion.proactivity_decided` 事件。

sample 示例，sampled_at 必须替换为实际采样时刻：

```json
{
  "schema_version": "1.0",
  "session_id": "host-process-uuid",
  "sequence": 17,
  "sampled_at": "2026-10-14T08:00:00Z",
  "ttl_seconds": 90,
  "activity": "docs",
  "focus": "unfocused",
  "availability": "available",
  "user_present": true,
  "locked": false,
  "meeting": false,
  "screen_sharing": false,
  "boundary": "break_started",
  "boundary_id": "break-uuid"
}
```

availability 为 available/busy/unknown。activity 支持 code/docs/chat/meeting/video/game/media/terminal/browser/unknown。用户在场、锁定、会议、共享屏幕缺省均为 null；任一未知都不能获得主动发言机会，宿主不能因缺少探测器就填写 false。focus、availability 缺省为 unknown。

首版只有 task_completed、break_started、user_returned 三种边界；其余采样使用 boundary=none 并省略 boundary_id。应用切换、短暂停顿、编译结束不直接等同于任务完成。宿主应从明确操作或可靠的任务集成上报，不应定时伪造边界。boundary_id 对同一次边界固定，重复采样不能换 ID。

同一节点会话内 sequence 严格递增，sampled_at 不倒退；新会话的采样时间必须晚于当前会话，已退休会话不能恢复写入。时间必须带时区，最多容忍向未来偏移 5 秒。TTL 为 5..300 秒，默认 90 秒，按采样时刻起算且不因向未来偏移延长。过期样本拒绝。相同命令重放原回执，实时状态另行 GET。

只有语义状态或边界改变才发 node.observation，modality=ambient，数据位于 payload.data.attention。仅 sequence、时间或 TTL 改变时只刷新快照。通用 `/observations` 仍按原协议保存 JSON，向那里写入同形数据不会接入本阶段调度，避免把历史或非类型化观察变成行动请求。

## 决策、限额与恢复

允许进入模型前须同时满足：

1. 伙伴显式启用 proactivity，策略版本与候选一致，候选未过期。
2. interaction_mode=chat，人格 initiative>=0.65，精力>=0.35、社交精力>=0.25，且没有有效的 quiet_requested；原有人格参与规则继续通过。
3. 当前节点、epoch、租约、在线状态及能力有效，无待提交迁移。
4. 最新注意力未过期、仍来自同一会话，明确 user_present=true、locked/meeting/screen_sharing=false、focus=unfocused、availability=available。activity=meeting 也强制静默。
5. 原边界没有被新的其他边界替代；候选产生后没有接受新用户对话，最近 30 秒也没有用户对话。
6. 冷却与当日主动次数有余额，模型已配置，共享 token 账本允许预留。
7. `origin=inner` 时，活动仍 active、冲动达到 `proactive_urge_threshold`、距最近用户对话不少于 `proactive_min_idle_seconds`，且已有历史对话；注意力 session/boundary 须与活动生成时一致。

`origin=attention` 的候选来自明确的宿主边界；`origin=inner` 的候选来自本地心理活动。两者共用队列、限额、提示预算、输出回执与中断检查，但后者不把活动内容当作用户发言或事实。决策与输出读取应展示 `origin`，不要伪造 `source_event_id` 的语义。

策略默认值：

```json
{
  "enabled": false,
  "daily_limit": 1,
  "cooldown_seconds": 3600,
  "candidate_ttl_seconds": 120,
  "token_budget": 3072,
  "memory_token_budget": 400,
  "max_output_tokens": 128
}
```

daily_limit 为 1..20，cooldown_seconds 为 30..86400，candidate_ttl_seconds 为 5..600。提示预算为 1..4096，记忆预算为 0..4096，输出上限为 1..256。人格核心预算不足仍返回既有错误，不截断身份来勉强发言。

主动次数按 UTC 日计，领取执行的尝试占用次数；生成后丢弃、失败与未知用量也占次数，防止失败重试形成打扰风暴。token 与对话、自动记忆共享账本，purpose=proactive；发送前预留，按实际 usage 结算，结果不确定时保留预留、不自动退款或重试。主动发言只用本地记忆召回，不付费生成查询向量。

同一伙伴最多一个 running 和一个 queued 候选；新边界替代旧 queued 候选。候选以伙伴、节点、宿主会话、boundary_id 去重，即使迁移返回同节点也不会重播同一边界。样本、入队/静默决策、观察事件和命令回执同事务提交；领取通过 SQLite 写事务串行化，多 worker 不重复领取。

后台每 proactivity.poll_seconds 检查已入队候选，默认 2 秒；`inner_life.poll_seconds` 单独控制活动生成，默认 10 秒。空队列与无合适活动都不创建问候、不调用模型。重启后的 queued 仍需重验；running 若已有终态对话回执则恢复决策，没有可确认回执的任务在 max(300 秒，模型超时+60 秒) 后记为 execution_unknown，绝不重新发模型请求。

决策保留 source_event_id、node/epoch、policy_revision、有效期与 reason。常见 reason：disabled、waiting_for_user、focused、suspected_stuck、attention_unknown、attention_stale、meeting_or_sharing、user_away、user_interacting、cooldown、daily_limit、boundary_superseded、policy_changed。生成后丢弃时记录具体 discard_reason，如 proactivity_focused 或 lease_expired。

## 对话、情感与呈现

主动生成复用 DialogueService 的预算、记忆版本检查与节点输出 gate。内部请求 user_initiated=false，提示使用 PROACTIVE_CONTEXT_DATA，不产生虚构用户消息。原 `/dialogue` POST 仍只接受 user_initiated=true。

主动事件为 companion.proactive_received/completed/silent/discarded。completed 定向给当时的呈现节点，payload 包含 command_id、candidate_id、epoch、companion_revision、assistant_message、usage 与记忆引用，不包含 user_message。回执 accepted_event_id 指向 proactive_received。输出事件与回执一起提交；调度器决策随后写入，崩溃间隙通过回执恢复。

这类输出不调用用户情感评价、不触发经历共鸣、不排入自动记忆，也不强化召回记忆的重要性。样本与主动文字不被认定为用户事实或双方共同经历。

生成前与提交前都重验最新注意力、策略、模式、用户对话、迁移、人格/状态/记忆版本、epoch 与租约。执行中重新进入专注或共享屏幕，结果标记 discarded，assistant_message=null；实际用量照常结算。

completed 是历史结果。宿主重放时不能直接显示旧问候；应核对候选有效期、当前策略与最新注意力、人格状态及记忆版本，并经现有 OutputHandle/LeaseGate 检查当前节点、epoch 与新鲜租约。本地锁屏、会议或静默应直接停止主动显示。SDK 提供类型与 REST 方法；Android 采集、通知、悬浮窗和主动文字呈现仍由宿主实现。WebUI 可显式修改主动策略、查看连续专注建议与最新决策，并通过实时输出页显示主动文字；不自动采集注意力，也不自动朗读。

启用时先 GET 策略，再以其 revision 完整写回并设置 enabled=true；同时由所有者显式允许 chat 模式与人格主动参与。只打开 enabled 不会越过既有 quiet/focus 或人格阈值。通过 [RuntimeApi](../clients/android-node/src/main/kotlin/dev/virtuallife/node/RuntimeApi.kt) 的 observeAttention、getAttention、getFocus、getProactivityPolicy、updateProactivityPolicy、getProactivityDecisions、getProactivityOutput 接入。

## 验证与下一步

自动化验证使用临时数据库、确定性时钟与假模型，覆盖开关/CAS/幂等、边界去重、专注/未知/会议静默、采样过期与旧会话、次数/冷却、并发领取、后台消费、生成中状态变化和未知用量不重试。本轮完整测试、真实 Chromium 与实际 HTTP 链路结果见[后台专注保护与主动陪伴验证](background-flow-validation.md)；SDK 编译与 Android 真机验证分开。未以这些自动化用例宣称真实心流识别效果。

下一步优先接一个明确边界的宿主来源，例如阅读器的“结束本章/开始休息”操作，再测误触发与遗漏。后续屏幕语义、活动分类及个体阈值需要单独校准；本轮已加入下述持续状态与滞回。评测应同时统计不适当开口、合理静默、用户拒绝与预算成本；没有触发并不自动代表策略正确。

## 连续专注建议与保护

`GET /v1/nodes/{node_id}/focus` 返回 `FocusSnapshot`：`node_id`、`state`、
`source="host_advisory"`、可为空的 `session_id/started_at/confirmed_at/release_started_at/last_sampled_at/expires_at`、
`stale`、`protection_active`、`duration_seconds`、`completed_count` 与 `recent_segments`。
`state` 为 `unknown/unfocused/candidate/focused/releasing`；这里的 candidate 是专注段的候选状态，
不是允许主动发言的候选。source 始终说明它来自宿主建议，不能向用户宣称“已检测到心流”。

第一个有效且明确的 focused 样本立即开启保护，连续有效 focused 达 60 秒后确认专注段。
60 秒和下述 30 秒是当前工程默认值，不是论文给出的心流识别阈值，也未经个人现场校准。
尚未确认时短暂切到 unfocused 再回来，需要重新累计连续 60 秒；已确认段可在短暂波动后延续。
无明确任务结束边界时，unfocused 进入 30 秒 releasing，期间仍保护；只有后续有效样本确认持续
unfocused 达 30 秒才释放。若明确、安全的 unfocused 同时带 `task_completed/break_started`，
可立即结束。仅 boundary 字段不能覆盖 focused、未知状态、离席、锁屏、会议或屏幕共享。

释放等待本身不计专注时长，结束时刻回到 `release_started_at`。unknown、会话切换与采样空档
不能作为连续专注证据；过期读取显示 unknown/stale，不凭墙钟自动确认或结束专注段。
`duration_seconds` 被有效采样期限和释放开始时刻限制。GET 不修改记录，后续接受的新样本才结算旧段。
这套状态机既不自动产生主动发言候选，也不修改用户的 `interaction_mode`。

只有已确认后结束的段才增加 `completed_count`，活跃段不计入完成数。
每节点最多保存 100 段详细历史，快照返回最近 20 段（最新优先），清理历史不减少累计完成数。
段字段为 `node_id/session_id/started_at/ended_at/duration_seconds/reason`；当前 reason 包括
`explicit_boundary/unfocused/session_changed/sample_gap/attention_invalid`。
后端时长按整秒返回；SDK 使用 Double 接收数值秒，保留小数秒扩展兼容。

## WebUI 策略与主动文字轮询

WebUI 将启用开关、限额和冷却等策略与专注建议放在同一伙伴的状态区域，更新策略仍使用独立
revision/CAS。打开策略不等于获得输出权限；它不能越过节点所有权、租约、专注保护、quiet/focus/away
模式或未知注意力。状态区显示的是宿主建议与累计专注段，不反向伪造 focused 样本。
主动文字采用普通文本呈现，不把模型输出当成 HTML、脚本或用户发言。

## DSH 投递（2026-10-28 接线）

DSH（DeepSeek Harness）侧通过 `dsh/virtual-life-proactivity/` bundle 消费同一输出通道：
轮询 `/v1/companions/{id}/proactivity/output`（`node_id/access_id/epoch/after_sequence`），
把每个未投递的 `item.assistant_message` 用 `ctx.schedule.create` 投成独立 turn，
并维护 delivered 游标。插件提供 `virtual_life_proactivity_status` / `_deliver` 两个工具。

```yaml
- insert:
    - id: virtual-life-proactivity
      name: '@virtuallife/dsh-virtual-life-proactivity'
      config:
        baseUrl: http://127.0.0.1:8038
        token: local-dev-token
        keepNodeAlive: true
        poller: { enabled: true, intervalMs: 6000, maxPerPoll: 5 }
        delivery: { title: 虚拟伙伴主动参与, promptPrefix: 虚拟伙伴主动想说话：, delaySeconds: 1 }
        sessions:
          - sessionId: session-...
            companionId: proact-demo
            nodeId: proact-node
            accessId: proact-access-3
```

边界：Web access 有 TTL，过期后 poll 返回 `node_access_expired`，需重新 `POST /v1/nodes/access/web`
并更新 `accessId`。当前投递语义是把运行时生成的台词塞进 prompt（DSH 会再生成一句）；
设计上已规划改为"冲动投递"（见 [dsh-fusion-plan.md](dsh-fusion-plan.md) §6.3），P1 两段式回合落地后处理。

`GET /v1/companions/{id}/proactivity/output` 必须提供 `node_id/access_id/epoch`，
可选 `after_sequence`。响应为 `ProactivityOutputPage`：

```json
{
  "companion_id": "companion-1",
  "node_id": "node-1",
  "access_id": "access-1",
  "epoch": 1,
  "companion_revision": 7,
  "items": [],
  "next_sequence": 100
}
```

item 包含 `sequence/event_id/command_id/assistant_message/created_at`。省略 `after_sequence`
只返回当前基线和空 items；新页面、新 Web access、切换节点或 epoch 后先建立新基线，不能用
`after_sequence=0` 代替这一步重播历史。后续请求使用已接受页面的 `next_sequence`，按 sequence
去重，仅推进当前会话的 cursor。接口使用 `Cache-Control: no-store`。

这个 GET 不调用模型。服务端仅向当前有效 Web access 返回仍可输出的文字，重查 owner、epoch、
租约、节点在线/能力、迁移、注意力、专注保护、策略、候选 TTL、用户交互及人格/状态/记忆上下文。
历史 dialogue 回执或 completed 事件仍然只是历史记录，不能绕过这条实时输出路径。
被新状态抑制或已过期的旧问候不会等到稍后空闲再补播。
检查同时查看消息完成之后的注意力变化；进入过专注、未知、会议/共享或其他不安全状态，或出现了不同边界，即使最新信号又恢复可用，也不会补播这条旧问候。

网络返回也可能晚于本地锁屏、专注变化、access 关闭或伙伴迁移。UI 在请求发出时记录
companion/node/access/epoch，收到后再次比较响应的这些字段与 companion_revision，确认本地页面
仍拥有同一有效 access、可输出节点和新鲜租约，且没有本地锁屏、会议、共享屏幕、静默或专注保护。
任何不匹配都丢弃整页，不把晚响应写入新会话的消息区或 cursor；重新建立基线后再轮询。
Android 宿主同样需要在实际呈现前通过本地 OutputHandle/LeaseGate 和宿主安全状态复核。

决策面板另用 `GET .../decisions?latest=true&limit=N` 刷新最近决策。`latest` 默认 false，
保留原升序分页行为；true 时在 `sequence > after_sequence` 范围取最新 N 条，响应仍按 sequence
升序排列，`next_sequence` 为本页最大 sequence（空页保留输入 cursor）。决策状态可原位更新，
因此刷新最新面板应使用 `after_sequence=0,latest=true`，而非只追递增 cursor。
决策页的 cursor 与实时文字页的 event sequence cursor 分开维护。
