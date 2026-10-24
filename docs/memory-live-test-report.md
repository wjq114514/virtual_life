# 真实模型与嵌入记忆联测

最新复测（2026-10-13）**23/23 项通过**，更正后的规范图谱边及有效来源已验证；保守过滤仍有覆盖边界，见末节。

历史首轮日期：2026-10-12。当轮结果 **19 项断言，18 项通过，1 项未通过**。这轮检验使用 6 个全新 GPT-6 Luna、medium reasoning 子代理，以及已配置 SiliconFlow 的真实 `BAAI/bge-m3`，没有替换模型或伪造模型回答。

## 范围与可审计性

脚本为 `examples/memory_live_test.py`。最终机器记录、6 对 request/response、4 份完整人格提示预览、初始图谱及 SQLite 均保留在忽略目录 `.data/memory-tests/live-20261012-final/`。机器断言在 `results.json`，数据库为 `runtime.sqlite3`，测试伙伴 ID 为 `live-memory-test`。没有打开日常运行数据库，没有输出凭据。嵌入凭据由 `Settings.load()` 在进程内读取现有配置。

每个 Luna 子代理只读取本例的 `request.context_text`，无跨例历史、预期答案或正确率提示。两次提取通过真实 `AutoMemoryService.process_next` 校验与提交；四次回答来自真实 `PersonalityPromptBuilder.preview`，`history=[]`，中性 `memory-test.v2.json` 人格，`output_permitted=True`，用户主动提问，音频权限保持关闭。

**对话完成事件是显式标记的合成测试夹具**，用户内容是虚构资料；未调用真实聊天 HTTP 提供商，不能据此宣称 DialogueService 端到端真实提供商成功。子代理工具不提供实际 token usage，提取适配器为接口提供的 `ModelUsage(0,0)` 仅是合成占位；既不代表实际零 token，也不能用于计算 Luna 账单。确定性后端账本测试与真实嵌入记账分别验证。

## 实际回答

| history=[] 问题 | Luna 原始回答 | 结果 |
| --- | --- | --- |
| 我的生日和妹妹小禾的生日分别是哪一天？ | 林舟，你的生日是6月18日；妹妹小禾的生日是11月3日。 | 正确区分人物 |
| 我喜欢喝什么茶？ | 你喜欢乌龙茶。 | 正确 |
| 我现在喜欢喝什么茶？只说当前偏好。 | 无咖啡因薄荷茶。 | 更正生效，没有旧偏好 |
| 我的昵称和我养的狗叫什么名字？ | 我只知道你自称林舟；你的昵称和狗狗的名字，我都还不知道。 | 人工读全文：两个字段均未知，没有编造昵称、狗名或养狗事实 |

## 集成断言

| 检查 | 观察 |
| --- | --- |
| 初始自动提取 | 完成：6 条稳定记忆，精确用户引文及来源版本通过校验 |
| 人物生日永久保留 | 实际召回两条不同主体生日，均为 permanent；并非空集合判断 |
| 真实向量索引与同义问题 | 初始一批索引成功；“我平时偏爱的饮品是什么”语义召回包含乌龙茶 |
| 重复查询缓存 | 第二次 `prepare_query` 为 cache_hit=true、model_calls=0 |
| 偏好更正与来源 | 第二次提取完成；新偏好有 supersedes_id，回答仅输出当前偏好 |
| 图谱与别名 | 初始含生日、妹妹、室友与茶偏好关系；顾言添加人工测试别名“小顾”后可查询命中，别名更新不属于模型推断 |
| 更正后的旧图谱边 | 乌龙茶偏好边不再出现在当前图谱中 |
| **更正后的新图谱边** | **未通过：纠正提取返回 relations=[]，没有薄荷茶新边。模型记忆更新覆盖到了偏好，图谱覆盖不足；没有把缺失边当作通过** |
| 确定性衰减 | 仅测试 MemoryService 时钟推进365天：routine≈0.1002、significant≈0.4717、permanent=1；不是现实等待实验 |
| 事实有效期 | 额外本地临时事实过 valid_to 后，结构化召回为空 |
| 召回预算与数量 | 24 token、最多1条时返回0条，estimated_tokens=0，truncated=true，未超限 |

衰减、有效期、预算与别名为本地确定性探针；不能算作额外真实模型调用。它们执行后新增的测试记录尚未索引，最终 `embedding_status` 记录的是这些探针之前的真实向量状态。

## 真实嵌入费用证据

最终运行共有 **3 次真实外部请求**：初始索引 input123、纠正索引 input60、查询 input9，向量空间实测 **1024维**，合计 **192 provider-reported input tokens / 0 output tokens**。两次索引各一批，重复查询没有新请求；`embedding_jobs.items[*].usage` 记录实际返回用量。此处是用量，不是货币账单，也不包含 Luna 的未知用量。

## 中止尝试与脚本修复

早期尝试保留于 `.data/memory-tests/live-20261012/`，不计入最终通过率。首次发现文件适配器只接受 text 包装，但提取子代理按实际提取 schema 写出了 JSON；其提取未提交。随后预览默认 output_permitted=false，模型正确保持沉默，生日回答为空；这是测试设置问题。状态字段误用 vector_count 也使脚本中止。最终运行从全新数据库开始，适配器接受直接提取 JSON，预览显式授权文字输出，使用 stored_vectors，并重新生成全部六次结果。早期尝试至少发生过一次真实嵌入索引请求，其用量不包含于上述最终运行192 tokens。

## 重跑

执行 `uv run python examples/memory_live_test.py`，或提供新的 `--run-dir`。默认目录含 UTC 时间与随机后缀；任何非空目录都会拒绝，避免旧响应重放。程序打印 `WAITING request-NN` 后，需要编排者为每个请求启动新的 GPT-6 Luna medium 子代理，让它仅读取该请求、按 context_text 完成任务，并写对应 `response-NN.json`。提取可直接写 schema JSON，回答写 `{"text":"回答"}`；600秒内未收到响应会失败。程序随后调用本地 enrich_report；固定测试探针不额外调用提供商。

本轮通过一次真实运行发现了图谱覆盖边界；后续应评估是否要求偏好更正提取同时生成当前关系。不能将这一个模型样本推广为稳定成功率。

自动未知断言仅检验承认未知（unknown_acknowledgement），不是无幻觉的充分证明；全文人工复核结论另保存在 results.manual_review。脚本完成会打印通过/总数及失败名称，存在失败时退出码为1。

当前本机 WebUI 的 HTTP chat 提供商仍未配置。Luna 仅用于上述子代理评估；重启服务不会自动连接 Luna，脚本也不会把子代理注册为 WebUI 聊天提供商。

## 2026-10-13：图谱优化后的真实复测

全新隔离目录 `.data/memory-tests/20261012T230510Z-90dd704c/` 已完成 **23/23 项断言，脚本退出码0**；上轮的18/19结果保留为历史，没有覆盖旧数据库或复用旧回答。这轮仍为6次全新 Luna medium 子代理生成、两次真实自动提取及四次 history=[] 的完整人格提示回答。没有盲重试、人工补边或增加常规模型调用。两次提取均记录 `prompt_version=auto-memory-v2`。

纠正模型原始结果直接生成 `用户 → prefers_drink → 无咖啡因薄荷茶`。当前图谱 edge `rel_7dc346a098344058ec4cfca2` 引用 `fixture-correction`，支持版本为偏好记忆 `mem_0a30db8806ea47de8d5ddba21adadf1c` revision1、来源 episode `mem_0f7b51ded8654dca90b5d6a3041d0eaa` revision1。断言查询数据库确认所有支持版本实际存在且active、至少一个是当前薄荷茶偏好；主体、谓词、肯定对象均精确匹配，旧乌龙茶边不在当前图中。完整边实例及来源记录见机器报告 `graph_correction_evidence`。

新增严格断言同时验证用户6月18日和小禾11月3日的生日主体。来源删除探针使用 SQLite backup 创建单独的 `source-deletion-probe.sqlite3`，只在副本删除纠正来源episode；薄荷茶边随即消失。主 `runtime.sqlite3` 保留原始有效证据及新边，适合继续在界面检查。其他预算、有效期、衰减与别名探针也通过；它们是本地确定性检查，不能算额外真实提供商调用。

| history=[] 回答 | Luna 本轮原文 |
| --- | --- |
| 两人的生日 | 你林舟的生日是6月18日；妹妹小禾的生日是11月3日。 |
| 初始茶偏好 | 你喜欢喝乌龙茶。 |
| 更正后的当前偏好 | 无咖啡因薄荷茶。 |
| 未知昵称与狗名 | 你的昵称和狗的名字，我目前都不知道；现有资料里没有提到。 |

最后一条逐字人工复核：两个字段都承认未知，没有造名字或推断养狗事实。机器弱断言与人工结论仍分开记录于 `unknown_acknowledgement`、`manual_review`。

**覆盖边界仍存在**：初始提取给出的妹妹关系边引用“妹妹小禾的生日是11月3日。”，这个局部引文没有明确第一人称，因此被保守过滤。机器诊断记录 `relations_skipped_count=1`、`relation_index=2`、`reason=unsupported_user_subject`；纠正提取过滤数量0。生日事实和生日边仍正确，但本轮通过的23项不意味着全部关系都提取完整，也不证明该文本支持过滤器具备完整语义判断能力。没有为达到通过率补造被过滤关系。

真实 BGE 仍为两批索引及一个查询，provider-reported input分别115、54、9，合计 **178 input / 0 output tokens，1024维**；重复查询cache_hit=true、model_calls=0。Luna实际token usage未知，适配账本0仍只是合成占位，不能解释为免费。事件仍为虚构资料的合成dialogue_completed夹具；这轮也没有验证WebUI HTTP聊天提供商端到端调用。

本轮真实案例使用中文日期。随后补充的英文日期分句边界与“用户”自环防护由独立确定性回归验证，未在本轮已加载的应用中重新执行，因此不把那些回归结果包装成真实模型验证。

最终独立后端回归：168 passed（44.06秒），自动记忆定向35 passed，Ruff check与format check通过；有1条既有Starlette/httpx警告。英文缩写日期、点分日期及公历/农历生日主体归属由这些确定性回归验证。本轮live只验证上述中文资料及图谱纠正链路，未为后续边界修补追加真实模型调用。
