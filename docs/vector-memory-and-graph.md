# 向量记忆与规范知识图谱

记忆检索组合词法匹配、重要度与时间、向量候选、摘要和有来源的图谱关系。记录、索引和图谱都按伙伴隔离，沿用当前版本、有效期、来源与提示预算约束。

## 向量模型配置

统一配置的 `[embedding]` 使用独立于对话模型的兼容提供商。示例：

```toml
[embedding]
enabled = true
base_url = "https://api.openai.com/v1"
name = "text-embedding-3-small"
api_key = "填写向量提供商密钥"
batch_size = 16
max_input_tokens = 4000
poll_seconds = 5.0
candidate_limit = 5000
similarity_threshold = 0.25
query_cache_size = 256
query_cache_ttl_seconds = 300.0
dialogue_recall = true
space_identity = ""
```

可以替换成其他兼容地址与模型。地址、模型名必须成对配置，启用时必须提供二者。适配器调用 `POST <base_url>/embeddings`，发送文本数组、model、`encoding_format="float"`，要求返回带 index 的向量以及 `usage.prompt_tokens` / `usage.total_tokens`。字段依据 [官方 Embeddings 接口](https://developers.openai.com/api/reference/resources/embeddings/methods/create)。`dimensions` 可选，仅在提供商支持时配置；省略后以首个成功响应确定空间维度，后续拒绝维度漂移。

向量参数与对话参数分开，向量密钥不会返回浏览器。`extra_body` 可设置提供商附加参数，禁止覆盖输入、模型、编码格式、维度及执行控制字段。空间身份包含地址、模型名、请求维度、附加参数和 `space_identity`，不包含密钥。即使模型名不变，后端模型升级或兼容服务改变向量含义时，也应修改 `space_identity`，开启新空间并重新索引；旧空间记录和未知任务仍保留供审计。

### 纯 CPU 本地 TEI

中文长记忆的默认 CPU 组合是 `BAAI/bge-base-zh-v1.5`（768 维、约 1 亿参数、512 上下文）做 embedding，`BAAI/bge-reranker-base`（约 2.78 亿参数）做可选重排。两者都可直接由 Hugging Face Text Embeddings Inference（TEI）CPU 镜像加载，不需要 GPU 显存；先启动容器并等待模型加载完成，再启动本服务：

```bash
docker run --rm -p 8080:80 \
  -v "$PWD/.data/tei-embedding:/data" \
  ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 \
  --model-id BAAI/bge-base-zh-v1.5 \
  --max-batch-tokens 4096 \
  --max-client-batch-size 16 \
  --max-concurrent-requests 32 \
  --tokenization-workers 4

# 可选：仅用于显式语义搜索的重排服务
docker run --rm -p 8081:80 \
  -v "$PWD/.data/tei-rerank:/data" \
  ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 \
  --model-id BAAI/bge-reranker-base \
  --max-batch-tokens 2048 \
  --max-client-batch-size 8 \
  --max-concurrent-requests 8 \
  --tokenization-workers 4
```

对应配置：

```toml
[embedding]
enabled = true
base_url = "http://127.0.0.1:8080/v1"
name = "BAAI/bge-base-zh-v1.5"
timeout_seconds = 2.0
# TEI 已固定该模型维度，通常省略 dimensions，让首个响应自动确定 768。
# dimensions = 768
max_input_tokens = 2000
local = true
space_identity = "tei-bge-base-zh-v1.5-768-v1"

[rerank]
enabled = false
base_url = "http://127.0.0.1:8081"
name = "BAAI/bge-reranker-base"
timeout_seconds = 0.5
candidate_limit = 8
min_candidates = 4
rrf_weight = 0.15
```

TEI 的 OpenAI 兼容 embedding 路由是 `POST /v1/embeddings`，所以 `embedding.base_url` 要包含 `/v1`；重排路由是根路径 `POST /rerank`，`rerank.base_url` 不要带 `/v1`。重排请求由服务端发送 `{query, texts, truncate: true, raw_scores: false}`，返回的裸数组按 `index` 对齐，不能与余弦相似度直接比较。

CPU 镜像的默认并发按 GPU 服务设计；直接使用默认 `--max-concurrent-requests 512` 时，重排容器在本机实测常驻内存约 11 GiB。上面的参数把重排限制在 8 路并发后降到约 2.4 GiB，embedding 约 0.4 GiB；代价只是超高并发时排队或返回 429，重排会按 fail-open 保留原顺序。并发上限应按实际同时请求数调整，调高会明显增加内存。`rerank.candidate_limit` 不要超过容器的 `--max-client-batch-size`，示例两边都是 8；`embedding.batch_size` 也不要超过 embedding 容器的 `--max-client-batch-size`，示例两边都是 16。TEI 1.9.4 还会把该 embedding 模型的内部 `max_batch_requests` 固定为 4；`batch_size = 16` 会被拆成多个内部批次，因此 embedding 的并发不要压到 8 以下，示例使用 32，避免批量索引返回 429。

`embedding.local = true` 表示本地不按 token 计费的模型：任务、usage、单任务输入上限和向量回执仍完整记录，但不占用 `[memory]` 的付费日额度。它不会绕过单任务上限，也不会改变来源和版本校验。本地部署也不要把 `timeout_seconds` 保留为云端的 60 秒：对话查询是内联的，服务异常时应快速回退本地检索，示例使用 2 秒；正常热态请求通常远低于该值。

CPU 重排默认关闭，并且即使开启也只作用于显式 `POST .../memories/semantic-recall`，不会进入普通 recall、人格预览或对话主链路，因此不会给聊天增加可感知延迟。若希望对话完全不承担内联查询开销，可设 `embedding.dialogue_recall = false`，对话只使用缓存命中和本地词法/图谱；显式语义搜索仍可用。显式语义搜索的候选头最多 `candidate_limit` 条；超过 `timeout_seconds`、传输失败或连续失败触发 30 秒熔断时，保留原 RRF 顺序。建议先在目标机器用真实数据测量。本机（40 vCPU Xeon E5-2690 v3，使用上面的容器参数）实测：单条 embedding p50 约 33 ms、16 条批量约 282 ms；8 条中文候选重排 p50 约 66–79 ms、p95 约 86–114 ms；显式语义召回端到端（查询向量命中缓存）p50 约 212 ms、p95 约 288 ms。CPU 型号、文本长度和并发数都会影响结果；若目标机器更慢或需要更严格的体感预算，宁可保持 `rerank.enabled = false`，或降低 `candidate_limit`、换更小的重排模型。

`bge-base-zh-v1.5` 的上下文是 512 token；TEI 默认开启 auto-truncate，超长单条输入会被截断。`max_input_tokens` 是单次请求的批次总预算，不是单条正文上限，所以示例保留 2000 以允许小批量索引，同时仍应由现有预算避免单条超长正文。模型首次下载和容器冷启动不计入超时预算；服务就绪后再启用，避免把冷启动误判为网络失败。

## 索引与费用

启用后后台自动索引已有和新增的有效 `memory_records`，覆盖共同经历与稳定认识。每批只处理一个伙伴，最多配置的条数与输入预算；完整单条超过任务预算时跳过并统计，不截掉证据正文。索引绑定 memory_id、revision、内容哈希和空间，元数据更新或更正后的版本会重新索引。摘要和关系通过支持记忆及图谱参与融合，本阶段不另外向量化摘要或边。

模型调用在数据库事务外。派发前和提交时再次检查当前版本、有效期与自动事实支持。向量、查询缓存与成功回执原子写入。非本地模式下，索引和查询共享现有 `[memory]` 的每日、单任务 token 账本；预估不等于准确 tokenizer，实际用量按提供商 usage 结算。索引耗尽日额度后持久延期到下一 UTC 日，不在每次轮询创建失败任务。`embedding.local = true` 时只在 `embedding_jobs`、日志和重排诊断中保留用量，不写付费日账本，也不触发日额度延期。

POSIX 文件锁覆盖领取、调用与提交，同库不同进程不会同时执行向量调用。索引运行时查询缓存命中可直接读取；缓存缺失且工作锁占用时返回 busy，实际对话回退本地召回。崩溃后确认未派发的 pending 任务释放已有预留；已进入 dispatching 的遗留任务标 unknown，保留预留与同版本尝试标记，不自动重新发送。确定未发送的请求构造失败允许后续重试；已发送的失败或未知结果同版本不自动重试，新版本仍可索引。

向量持久保存在 SQLite，归一化后以 little-endian `float32` BLOB 保存；旧 JSON 向量仍可读取，并在启动或后台索引时有限回填。本地打分使用 numpy 批量矩阵乘法，没有 numpy 时回退纯 Python；写入和查询各归一化一次，读取不再重复归一化。默认考察最新 5000 个有效向量，可调 `candidate_limit`；这不是 ANN 索引，超过候选窗口的旧向量不会参与当次匹配。5000 条 1024 维候选在本机实测约 200ms，1000 条约 70ms，高频对话可优先使用更小的候选窗口或独立短索引。向量空间不同不能混用，损坏缓存和向量会跳过并回退本地检索。规模扩大后可替换为支持全库检索的向量索引，继续复用来源与版本校验。详细基准见 [本地向量记忆性能优化](memory-vector-performance.md)。

## 召回与实际对话

| 入口 | 向量模型行为 |
| --- | --- |
| 普通 `POST .../memories/recall` | 不调用模型，也不重排；查询缓存命中时融合已有向量候选 |
| 人格 `preview` | 不调用模型，也不预留额度，也不重排 |
| `POST .../memories/semantic-recall` | 校验请求和策略后，缓存缺失时调用一次查询 embedding；可选对融合候选头部做一次 fail-open 重排 |
| 实际 `dialogue` | 启用 `dialogue_recall` 后，检查节点和参与条件，再准备查询向量并构建最终提示；不重排 |

查询向量缓存按伙伴、规范化查询和空间隔离，有容量与过期上限。实际对话沿用提示检索的前 2048 字符作为查询，完整用户消息仍进入提示。向量准备失败或锁占用时，对话回退词法、摘要、图谱；生成文字仍需通过原有节点、epoch、lease 与引用门控。查询用量可能已产生，失败记录保留在向量任务中。

召回对词法及图谱候选、向量候选分别排序，再用 reciprocal rank fusion 合并；不会把词法分数和余弦值直接相加。向量命中的支持记忆也可作为真实图谱关系的检索入口。来源、层级与有效性在候选限制前过滤，最终上下文继续遵守条数与 token 预算。

RecallResult 的 `retrieval` 包含 `semantic_cache_hit`、`semantic_candidates`、`vector_space`；语义召回 `model_calls` 为查询向量调用次数，`embedding_usage` 为本次已知实际用量。启用重排时，`rerank` 诊断包含 `enabled`、`applied`、`candidates`、`latency_ms` 和 `error_code`；`retrieval_strategy` 成功重排后带 `_reranked` 后缀，失败则保持原值。实际 DialogueResult 的原有 `model_calls` / `usage` 继续描述文本生成；新增 `memory_retrieval` 单独保存查询向量调用、缓存、用量及降级信息。背景索引和自动整理用量见各自任务账本。

## 图谱实体与证据

实体名称使用 NFKC、大小写折叠和空白归一，节点有稳定 entity_id。关系按规范主语、谓词、宾语聚合为稳定 relation_id / edge_id；重复抽取不会重复显示同一边。聚合关系保留原始 relation_ids、各自支持版本、来源事件、任务与时间。

独立有效来源计为 `evidence_count`。每条边最多返回 64 个独立来源，引用数组也有上限；超过时 `evidence_truncated=true`。某来源失效会从聚合中移除，其他有效来源仍可支持这条边。删除、更正、过期、来源墓碑继续限制查询和召回；关系置信度不因简单重复抽取而提升。多跳查询只返回已保存且有证据的关联边，不把路径自动转换成新事实。

`[knowledge_graph]` 设置 `max_hops`（默认 2，范围 1..3）与累计候选上限（默认 512）。查询可使用原有 query，或明确 entity_id；第一跳筛相关有效边，后续按端点扩展，来源过滤先于候选上限。limit 最多 200 条边。默认查询仅显示返回有效关系涉及的节点；明确 entity_id 可读取没有当前关系的实体元数据。

别名由用户明确设置，不由模型猜测。名称与别名在伙伴内唯一，已存在的规范实体不会被别名静默覆盖。模型后续使用已登记别名时解析到规范端点。撤回别名时，历史关系实际使用的原始拼写恢复为独立实体元数据，关系沿用原始来源。

进入提示的关系携带 `entity_versions`；PersonalityPreviewResult 汇总实际使用的实体版本。模型派发前和回复提交时都核验实体及记忆版本。生成期间修改或撤回别名会阻止旧实体解释继续输出。

## 接口与操作

下表路径均在 `/v1/companions/{companion_id}/memories` 下，使用已有 Bearer token。

| 方法与路径 | 用途 |
| --- | --- |
| GET `/embeddings` | 开关、模型状态、本地额度模式、重排状态、有效/待索引/超长记录数、任务状态和共享额度 |
| GET `/embeddings/jobs?limit=50&offset=0` | 索引/查询回执、用量与错误码；不返回原始查询或密钥 |
| POST `/embeddings/run`，`{}` 或 `{"limit":16}` | 索引一批可处理记录，limit 1..64；不会重发未知任务 |
| POST `/semantic-recall` | 使用原 MemoryRecall 格式进行语义融合召回 |
| GET `/knowledge-graph?query=某个别名&hops=2&limit=50` | 按名称、别名或关系匹配已有证据路径 |
| GET `/knowledge-graph?entity_id=ent_...&hops=2` | 从指定实体扩展 |
| PUT `/knowledge-graph/entities/{entity_id}/aliases` | 按版本替换别名列表，并保存幂等收据 |

别名更新示例：

```json
{
  "command_id": "alias-update-unique-id",
  "expected_revision": 1,
  "aliases": ["小王", "王同学"],
  "source_event_ids": []
}
```

别名和可选来源列表各最多 64 项，每个字符串 1..128 字符。提交来源事件时要求存在于同一伙伴；这些来源用于别名审计，不代替关系事实的证据。别名列表为空表示撤回。revision 不符返回 409；同命令同请求返回历史收据，换请求返回幂等冲突。

WebUI 提供索引状态、一批索引、语义召回开关、关系查询和别名编辑。Android SDK 同步相同接口。当前没有图数据库服务、自动实体合并或 ANN；无来源推理尚未实现；完整隐私清除已作为 `DELETE /v1/companions/{id}/memory` 提供（详见 [自动记忆](automatic-memory.md)），失效摘要自动重建已提供。向量查询与抽取的实际长期效果还需真实相处数据评估。

融合召回沿用 [记忆衰减](memory.md) 的有效重要度与永久保护候选；摘要和关系依赖其当前有效支持记忆。向量索引、缓存准备、显式语义召回和预览均不强化记忆。成功对话只强化最终上下文实际采用且相关的支持记忆，每个 UTC 日最多一次。MemoryRecall.semantic_key 可精确查找规范槽位，仍遵守来源、有效时间、层级与输出预算。

向量候选排序加入 `memory.decay.semantic_salience_weight`（默认 0.05）控制的小幅显著性先验，采用加法而非缩放整项查询相关度；原始余弦相似度门槛保持不变。相关永久来源记忆优先尝试进入最终提示，完整提示预算仍可能使其省略。

自动提取采用 `auto-memory-v2`：有用户证据的当前偏好、生日、人物关系应输出关联稳定记忆的正向边，第一人称统一为“用户”，具名生日保留原所有者。更正只生成新的当前对象，旧否定对象不生成正向边。优先谓词为 `prefers_drink`、`has_birthday`、`roommate`、`sibling`；不强制改写历史边。

抽取关系的主体和对象须在所指记忆内容及精确用户引用中获得文本支持；第一人称“用户”与明确生日所有者采用有限例外规则，明确否定分句中的对象保守跳过。过滤边的索引及原因保存在任务/日志 `relations_skipped`，稳定记忆仍成功保存，旧边继续由支持记忆版本和来源有效性控制。检查不使用历史补边或额外模型调用，也不替代来源失效和伙伴隔离。文本支持不等于完整语义蕴含：合法改写可能被漏掉，谓词错误或复杂否定仍需要真实数据评估；模型未输出的边不会由服务猜测创建。
