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
- `status`（`active` / `expired`）与 `created_at` / `expires_at`

`urgency`（`urge`）是候选强度，不等于发言许可。`content` 是内部提示材料，真实对话提示会明确标注“这是你自己的内在活动，不是用户发言”。`model.rationale` 仅供审计，绝不进入后续提示。

### 可选模型评判

`model_assisted=true` 且模型已配置时，服务至多每个 `model_min_interval_seconds` 调用一次评判，并受每日次数与 token 账本限制。模型只返回 `kind`、`salience`、`urge`、`confidence`、`rationale`；服务验证 schema、置信度与允许类型，再按 `model_weight` 与本地分数混合。模型看到的最近对话被标记为不可信数据。调用不在 SQLite 写事务内执行，失败或无效输出自动使用本地结果。

## 接口

```text
GET /v1/companions/{companion_id}/inner-life
GET /v1/companions/{companion_id}/inner-life/activities?after_sequence=0&limit=50
```

快照包含当前情绪摘要、慢变量摘要、`needs`、主导活动、当前活动、最近 8 条活动，以及 `model_assisted` 与当前活动是否由模型评判的 `model_calls`。活动分页按 `sequence` 升序，`limit` 为 1..100。`enabled=false` 时轮询不创建新活动；历史接口仍可读取已有记录。

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

## 相关实现

- 服务与表：`backend/virtual_life/inner_life.py`
- 候选与 gate：`backend/virtual_life/proactivity.py`
- 中断复核：`backend/virtual_life/proactivity_output.py`
- 提示段：`backend/virtual_life/personality_prompt.py`
- 测试：`backend/tests/test_inner_life.py`
