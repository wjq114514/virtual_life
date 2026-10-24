# 情感系统第二阶段实施合同

2026-10-15。主代理规划，gpt-6.1-sol（low）执行，主代理负责整合与验证。依据现有 emotion.md 与 ambient-signals-and-emotion.md，交付本地评价、可解释纠错与分维度恢复。以下权重为工程参数，不宣称心理测量或临床有效性，不增加模型调用。

## 评价与兼容

新增 emotion_appraisal.py，导出 AppraisalVector、appraise(message, traits)。六维：pleasantness=-1..1（默认 0）、effort/uncertainty/attention=0..1（默认 0）、agency=-1..1（默认 0）、control=0..1（默认 0.5）。appraise 返回原 reaction/reason/confidence/delta，并增加 appraisal（六维对象）、appraisal_version="rules_v2"、uncertainty=0..1、attribution=self/other/situation/unspecified。uncertainty 表示规则解释的不确定性，非用户心理测量；attribution 表示线索归因，非事实断言。保留现有 reason 和 reaction 集合；无明确证据时中性、confidence=0、uncertainty=1。delta 保持原有五维和边界。

使用有来源边界的中文模板与程度修饰，不从否定、转述、引用、假设、疑问或讽刺短句机械翻转情绪。安静请求优先；探索表达不被当作负面情绪陈述。保留已有明确短句的含义，未识别文本不凭词命中推断。原始对话不复制入情感台账。

## 澄清接口

新增 POST /v1/companions/{id}/emotion/experiences/{event_id}/correct，EmotionCorrection 请求：command_id、expected_revision、kind（direction/intensity/attribution）、retained_weight（仅 intensity；默认 0.5，0..1）、attribution（仅 attribution 且必填，self/other/situation/unspecified）。其他 kind 不接受专属字段，拒绝未知字段。

- direction：撤去这条经历的即时情绪影响，reaction 回中性，不翻转符号。成功回复已发生的精力消耗保留。
- intensity：按当前影响乘 retained_weight，只缩小不放大；评价向量按中性值收缩，置信度降低、不确定性不下降。
- attribution：只修改归因与 control/agency 解释，减弱原 control 增量，保持 valence/arousal 与资源消耗；不得凭纠错创造新的正负体验。

原 appraisal_json 保留，另表保存累计权重、最后一次澄清与版本；完整历史可从持久事件追踪。状态、版本、事件与幂等回执原子提交，CAS 与跨伙伴隔离同旧 invalidate。已 invalidated 或 forgotten 的经历拒绝 correct；旧 invalidate 保留完整撤回语义，也能撤回 corrected 经历。

EmotionExperience 新增可缺省字段：appraisal（AppraisalVector|null）、appraisal_version（默认 legacy_v1）、uncertainty（默认 1.0）、attribution（默认 unspecified）、original_delta（AffectDelta|null）、correction（EmotionCorrectionInfo|null）。correction 为 {kind, retained_weight:float|null, attribution:string|null, revision:int, corrected_at:string}。status 增加 corrected，delta 为当前有效影响，original_delta 为原始记录。忘记来源时隐藏这些解释和澄清细节。旧记录不伪造六维评价。

澄清后的来源不能继续支持旧共鸣；新共鸣只使用有效的修正后影响，并记录来源纠错版本。任何澄清都改变 context_version，阻止正在生成的旧解释输出；普通时间恢复不改变版本。澄清不创建新用户消息、不增加模型调用、不自动写长期偏好。

## 恢复与配置

EmotionPolicy 增加 valence_half_life_seconds、arousal_half_life_seconds、control_half_life_seconds、resource_half_life_seconds，均为 nullable int，非 null 时 60..7200。默认 null，沿用 half_life_seconds，保持原来的数值与旧测试语义。按维度计算确定性的回归基线，资源维度共享 resource 参数；不增加随机噪声、未经校准的跨维度耦合或日夜惩罚。

EmotionSnapshot 新增 recovery_half_lives（可缺省/null，新的响应给出 valence/arousal/control/energy/social_battery 的最终整数参数）。静默请求仍沿用 half_life_seconds 的既有有效期。候选和历史查询保持有界；长时间离线返回基线，读取不写状态。

## 分工与验收

- 评价执行者：emotion_appraisal.py 与独立语义规则测试。
- 状态执行者：emotion.py、纠错/恢复与迁移测试，不编辑 API 或客户端。
- 客户端执行者：Emotion.kt、RuntimeApi.kt、WebUI 与 SDK README。
- 主代理：API、配置兼容、协议/研究取舍说明、OpenAPI、交叉模块审查和完整回归。

验收包含否定/引用边界、程度变化、老数据库/回执兼容、三类澄清的原子性/幂等/版本/跨伙伴隔离、共鸣来源纠错、删除隐藏、分维度时间恢复、生成中澄清导致丢弃、自触发与明确静默保持、完整 pytest、Ruff、JS 语法、SDK 编译和 OpenAPI 同步。真实表达效果和设备表现另行验证。
