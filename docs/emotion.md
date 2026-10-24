# 连续情绪、评价与可纠正的理解

2026-10-15，第二阶段在已有连续状态与记忆共鸣上加入六维评价、三类澄清和分维度恢复。默认启用，完全本地计算，不增加模型调用。服务端统一保存经历，设备切换共享同一状态；旧库自动新增纠错表，旧记录和回执保持可读。

## 触发与评价

有效用户对话先校验节点、epoch、revision、输出租约、节点能力和迁移状态，再将命令占用、`companion.dialogue_received` 事件及情感经历原子提交。重复命令不重复触发。模型失败仍保留已接受的用户事件；失败、静默和丢弃的回复不消耗模拟精力。[自触发](proactivity.md) 使用独立事件，不制造用户消息或情感刺激。

`emotion_appraisal.py` 将明确线索先映射为评价向量，再投影为生命体的情绪增量。当前支持感谢、安静请求、探索，以及“我最近有点难过”“我现在非常累”等时间和程度变体。程度系数为有点/有一点 0.6、很/无程度词 1、非常/十分 1.25、特别 1.4。它们是可替换的工程参数；对难过表达产生轻度关切，不直接复制用户状态。

仍使用有明确范围的整句模板：否定、引用、转述、假设、反问、讽刺和复杂多分句通常保持中性，不按关键词翻转情绪。探索允许限定长度的后缀，显式引用、假设或讽刺标记使其回到中性。安静请求优先。此实现没有训练语言分类器，也没有导入情绪词库数据。

| appraisal 字段 | 范围 | 规则解释 |
| --- | --- | --- |
| pleasantness | -1..1 | 线索的愉快性 |
| effort | 0..1 | 表达的努力线索，不直接扣除精力 |
| uncertainty | 0..1 | 本次解释的不确定性 |
| attention | 0..1 | 注意投入线索 |
| agency | -1..1 | 自己到他人的责任线索，正值偏向说话者 |
| control | 0..1 | 情境控制线索，中性为 0.5 |

每项经历还返回 `appraisal_version="rules_v2"`、`confidence`、顶层 `uncertainty` 和 `attribution=self|other|situation|unspecified`。归因是规则对线索的解释；这些值不衡量用户心理状态，也不证明实际因果。未识别内容的 confidence=0、uncertainty=1。旧记录返回 appraisal=null、appraisal_version=legacy_v1，不补造历史评价。情感台账不复制消息正文。

## 连续状态与分维度恢复

人格 warmth、curiosity、assertiveness 决定愉悦度、唤醒度与控制感基线。有效状态是基线加有效经历随时间衰减的增量，再限幅：

```text
factor[axis] = 2^(-max(0, elapsed_seconds) / half_life[axis])
effective[axis] = clamp(baseline[axis] + sum(valid_delta[axis] * factor[axis]))
```

愉悦度范围 -1..1，唤醒度和控制感范围 0..1。精力与社交精力独立，成功回复按实际字符长度有限消耗；资源共享一个可单独设置的半衰期。整个经历仍以消息接受时间为恢复锚点。离线、断网和迁移没有情感惩罚。

读取、提示预览和回执查询不新增刺激、不改变 revision。最多处理最近 `candidate_limit` 项，超出返回 `truncated=true`。超过最长已配置半衰期的 24 倍后不再计入增量，历史仍可分页查看；这是有界近似。最近仍有明确影响的经历决定表达标签，数值保留其他经历的影响。安静请求沿用共享 `half_life_seconds` 的一个半衰期有效期，不随单轴配置改变；其回复建议限制为一句，不修改 interaction_mode，也不硬裁剪模型文本。

## 三类澄清与完整撤回

新接口 `POST /v1/companions/{id}/emotion/experiences/{event_id}/correct` 接收 command_id、expected_revision 与 kind。默认强度请求示例：

```json
{"command_id":"clarify-1","expected_revision":2,"kind":"intensity"}
```

| kind | 专属字段 | 行为 |
| --- | --- | --- |
| direction | 无 | 撤去即时情绪与自身共鸣的影响，reaction 归中性；不自动翻转符号 |
| intensity | retained_weight，省略时 0.5，范围 0..1 | 当前影响再乘保留比例，评价向中性收缩，置信度降低、不确定性不下降 |
| attribution | attribution，必填 | 更新归因，将当前 control 增量及 agency/control 评价各减半；保持 valence/arousal |

各类澄清都保留已发生的成功回复精力消耗。多次强度澄清累计相乘，后续操作不能重新放大已撤去的影响。专属字段只允许对应 kind 使用，显式 null 与未知字段也被拒绝。省略保留比例和显式提交 0.5 具有相同幂等语义。

原始 appraisal_json 保留，另表保存累计权重和最近澄清信息；`companion.emotion_corrected` 事件保留历次操作及权重。经历页 `status=corrected`，delta 是修正后贡献，original_delta 是原始贡献；correction 包含 kind、retained_weight、attribution、revision、corrected_at。方向澄清调整本次理解，不删除对话或自动形成长期偏好。

旧 `invalidate` 接口保留完整撤回语义，也可撤回 corrected 经历，包括其资源贡献。已 invalidated 或 forgotten 的经历拒绝新的 correct。完整撤回与三类调整在 UI 中分别显示，避免混淆。

状态、版本、事件与幂等回执在同一事务保存。相同 command_id 与请求重放原回执；改变请求或使用过期 expected_revision 返回 409。伙伴之间的经历隔离，别的伙伴的经历返回 404。历史回执不授予当前输出权。

## 记忆共鸣与遗忘

首次组装预算内提示后，从实际包括的 episode 来源中找回过去的情感经历。词法相关性还须达到阈值，纯向量相关暂不触发共鸣。同一原始经历被多个记忆引用也只计一次。共鸣采用有效情感增量均值乘 `resonance_strength`，不复制历史精力消耗。每个用户事件只评估一次，空结果也持久记录，随后重新组装有界提示。

经历记录接受/完成事件 ID、规则原因、置信度、增量、记忆版本和原始共鸣经历 ID。共鸣还记录每个来源的纠错版本；来源澄清后，旧共鸣立即失效，新对话只基于修正后的贡献形成共鸣。旧缓存缺少纠错版本时按 0 处理，首次澄清后同样失效。来源失效不会抹去其他仍有效的反应。

记忆版本变化、来源失效和删除会撤回相应共鸣。来源 tombstone 禁止原始经历影响当前状态；经历页隐藏其评价、贡献、归因、原始增量和澄清细节。这遵循现有遗忘语义，并不清除历史事件与命令回执。

## 兼容与输出复核

原 `/personality/state` 手动写入和 TTL 语义保留。未到期的手动 mood 优先表达，精力叠加实际消耗。自动状态通过 `/emotion` 读取；提示与对话回执的 emotion 可缺省。新增经历字段均有兼容默认值，旧回执可解码。

`revision` 表示账本写入版本；`context_version` 同时反映来源有效性、纠错、人格、手动状态到期和策略。自然时间恢复只改变有效值及 evaluated_at，不改变 context_version。对话和主动输出在模型派发前、输出提交前重新校验该版本及节点/租约等条件；澄清阻止生成中的旧解释输出，已执行用量仍结算。

成功回复的精力更新、完成事件、自动记忆队列及终态回执原子提交。`companion.emotion_updated` 通知客户端重新读取，修正记忆来源后也应重读。WebUI 在显示回复前复核完成快照的 context_version。Android SDK 提供类型与 REST 方法，具体表情和语音由宿主呈现。

## API 与配置

所有接口使用现有 Bearer 鉴权并验证伙伴存在。

| 路由 | 行为 |
| --- | --- |
| `GET /v1/companions/{id}/emotion` | 状态、基线、effective_state、reaction、quiet_requested、版本、来源、恢复参数与时间 |
| `GET /v1/companions/{id}/emotion/experiences?after_sequence=0&limit=50` | 按 sequence 升序分页，limit=1..100，返回 items、next_sequence、has_more |
| `POST /v1/companions/{id}/emotion/experiences/{event_id}/correct` | 三类可解释调整，返回当时的 EmotionSnapshot |
| `POST /v1/companions/{id}/emotion/experiences/{event_id}/invalidate` | `{command_id, expected_revision}` 完整撤回，返回当时快照 |

快照 `recovery_half_lives` 返回 valence、arousal、control、energy、social_battery 的最终半衰期；旧快照可缺省/null。WebUI“反应经历与澄清”可查看规则解释、原始/当前贡献及最近澄清，并操作三类调整或完整撤回。

```toml
[emotion]
enabled = true
half_life_seconds = 1200
candidate_limit = 256
resonance_strength = 0.2
# 可选覆盖；不填沿用 half_life_seconds。
# valence_half_life_seconds = 1800
# arousal_half_life_seconds = 600
# control_half_life_seconds = 1200
# resource_half_life_seconds = 2400
```

环境变量 `VIRTUAL_LIFE_EMOTION_<字段大写>` 覆盖文件设置。四个可选半衰期的空环境变量恢复 None，继而回退共享半衰期；TOML 省略该字段即可。半衰期均为 60..7200 秒，候选上限 1..2048，共鸣系数 0..0.3。默认各轴保持旧值，没有静默改变恢复速度。enabled=false 后维持原人格与手动状态，不新增自动反应，历史仍可读取。配置修改需重启。

## 调研与取舍

参考已有[演进草案](ambient-signals-and-emotion.md)的六维评价、受控重评级与分维度动力学建议；本轮补查了[评价过程作者资料](https://biblio.ugent.be/publication/4316089)、[EMA 作者论文目录](https://www.ccs.neu.edu/home/marsella/publications/abstracts/MarsellaCSR09-abstract.html)和 [Chinese EmoBank 原仓库](https://github.com/NYCU-NLP/Chinese-EmoBank)。前者支持将线索解释与情感结果分开，中文资源明确区分程度、否定和情态，为规则边界提供参考。

草案中“方向澄清改变符号”没有直接采用：一次方向否认不足以支持相反体验，本阶段只撤去该解释。保留原始证据与独立修正也让后续校准可追溯。投影增益、程度系数和归因减半均是项目工程选择，未从论文或数据拟合。

当前没有实现完整 OU 随机过程、Kalman 参数估计、持续心境、长期关系演化或复杂语义理解。effort 等解释维度为校准保留，不擅自扣精力。表达效果仍需真实对话评估；已有测试验证确定性规则与协议行为。
