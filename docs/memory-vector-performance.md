# 本地向量记忆性能优化

日期：2026-10-21。目标是在不改变记忆协议、来源校验、版本和衰减语义的前提下，把本地语义召回从秒级降到百毫秒级，并降低在线 embedding 调用开销。

## 结论

- **继续使用 SQLite**，不迁移 PostgreSQL 或 Redis。`DialogueService` 要求协调器、人格、记忆和对话命令共享同一个持久数据库，并在同一事务中提交事件、回执和记忆；拆库会破坏这个原子边界。Redis 不是权威存储，引入后还要处理双写、删除传播和设备切换。
- 实测瓶颈不是 BGE-M3 在线调用，而是 **JSON 解析、重复归一化和纯 Python 余弦循环**。
- 向量改为 little-endian `float32` BLOB；旧 JSON 向量仍可读取，并在启动或后台索引时有限回填。
- 本地打分改为 numpy 批量矩阵乘法；没有 numpy 时保留纯 Python 回退。
- embedding provider 复用同一个 `httpx.Client`，避免每次查询重新做 DNS/TCP/TLS。
- 5000 候选的本地召回从约 **1.97s** 降到约 **0.20s**；1000 候选约 **0.07s**。在线单条查询从约 **181ms** 降到约 **100ms**。

## 为什么不动数据库

当前数据库承担的不只是记忆表，还包括：

- `events`、`dialogue_commands`、人格状态和记忆写入的原子提交；
- 来源墓碑、自动记忆证据、token 账本和 embedding 任务回执；
- 单库文件锁和 WAL 既有的并发边界。

把向量或记忆拆到 PostgreSQL/Redis 会引入分布式事务或最终一致性，且无法复用现有“调用前预留、调用后结算、未知不重发”的账本语义。当前规模下，SQLite 的读取和事务成本不是主要瓶颈；真正慢的是向量解码和点积。因此本轮只优化存储编码和计算路径，保留单库架构。

## 实施内容

### 1. float32 BLOB 存储

`embedding_vectors` 新增可空 `vector_blob` 列：

- 新写入：`vector_blob` 保存归一化后的 little-endian `float32`，`vector_json` 写空串；
- 旧行：继续读取 `vector_json`；
- 迁移：启动时最多回填 256 行，后台 `run()` 每次最多回填 512 行；迁移是增量的，不阻塞启动；
- 读取：优先 BLOB，缺失时回退 JSON；维度不符或损坏的行直接跳过，不影响其他候选；
- 大小：1024 维向量从约 22.5KB JSON 降到 4KB，约 5.6 倍。

代码位置：`backend/virtual_life/vector_codec.py`、`backend/virtual_life/embedding_memory.py`。

### 2. numpy 批量打分

- 同一批 BLOB 直接 `frombuffer` 成矩阵，一次矩阵乘法得到全部余弦；
- 混合旧 JSON 行时逐条解码后批量计算；
- 没有 numpy 时回退到 `array('f')` + Python 循环；
- 归一化只在写入和查询向量时做一次，读取存储向量时不再重复 `hypot` 和除法。

### 3. 复用 HTTP 连接

`CompatibleEmbeddingProvider` 现在延迟创建并复用一个 `httpx.Client`，提供 `close()`；索引和对话查询可以共享连接池。此前每次 `embed()` 都新建 client，远程端点实测多付约 70–140ms 的 DNS/TCP/TLS 成本。

### 4. 减少查询次数

读取候选时用 `LEFT JOIN memory_access_state` 直接带出 `last_reinforced_at`，移除了原先按 512 条分批查询访问状态的逻辑。衰减和显著性计算保持不变。

## 兼容与迁移

- 新库直接创建带 `vector_blob` 的表；旧库通过 `PRAGMA table_info` 检查后 `ALTER TABLE` 增加列。
- 旧 JSON 行可读；迁移后 `vector_json` 变为空串，`vector_blob` 保存二进制。
- 这是单向升级。旧代码读取空 `vector_json` 时会跳过这些向量，不会损坏数据；如需降级，应重新索引。
- 没有修改 HTTP 协议、OpenAPI 或 Kotlin SDK 类型。

## 实测

测试环境：当前机器，Python 3.14，numpy 2.5.4，1024 维，SQLite WAL。基准使用 5000 条合成记忆，查询阈值设为 `-1.0`，即所有候选都参与打分，属于偏悲观的上界。

| 候选数 | 旧 JSON（实际召回） | 新 float32 BLOB |
| ---: | ---: | ---: |
| 500 | — | p50 24ms |
| 1000 | — | p50 73ms |
| 2000 | — | p50 102ms |
| 5000 | p50 1969ms | p50 197ms |

在线 embedding（SiliconFlow `BAAI/bge-m3`，本机网络）：

| 场景 | 优化前 p50 | 优化后 p50 |
| --- | ---: | ---: |
| 单条查询，项目适配器 | 181ms | 100ms |
| batch 16，项目适配器 | 287ms | 169ms |

## 使用建议

- 普通长期记忆召回可保留 `candidate_limit = 5000`，5000 条约 200ms；
- 高频对话的短期轮次兜底建议使用独立的小索引，或把候选限制在 1000 左右，约 70ms；
- BGE-M3 单次查询约 100ms，因此“1000 候选本地扫描 + 在线查询”总路径约 170ms，适合放在对话关键路径；
- 规模超过约 5 万条向量后，再考虑 ANN（如 sqlite-vec、FAISS 或独立向量服务），而不是继续扩大候选窗口。

## 后续可做

1. 两阶段扫描：先取轻量 `memory_id` 和向量打分，再只为过阈值的结果读取完整正文和来源，进一步降低大候选集开销。
2. 为短期对话轮次建立独立的小型向量索引，避免和历史长期记忆共用 5000 候选窗口。
3. 后台一次性完成旧 JSON 向量迁移，减少长期共存两种格式的时间。
4. 评估 ANN 索引和跨进程查询缓存；当前仍是单机 SQLite 余弦扫描。

## 验证

- `uv run pytest -q`：931 passed。
- 新增测试覆盖：BLOB/JSON 往返、维度校验、旧 JSON 读取、损坏向量跳过、增量回填。
- 可复跑基准：`uv run python examples/memory_vector_benchmark.py --records 5000`。
