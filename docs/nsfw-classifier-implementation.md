# NSFW 场景分类器实现说明

本文是实现版本，配套设计稿 [NSFW 场景分类器训练指南](nsfw-classifier-training.md)。设计稿里的代码片段用于说明思路，不能直接运行；实际可运行的代码、修正后的流程和集成方式以本文为准。

> 分类器只输出场景档位，供模型路由选择推理链路。它不是内容过滤器，也不能替代输出审核、租约和权限检查。

---

## 1. 已实现内容

| 模块 | 位置 | 说明 |
|---|---|---|
| 标签、阈值决策、指标 | `backend/virtual_life/scene_classifier/` | 四分类；显式→亲密→情绪→日常的有序阈值规则；`explicit` 同时覆盖直接性意图和成人向问题咨询 |
| 数据清洗与切分 | 同上 | 归一化、PII 脱敏、长度/未成年人过滤、近似去重、分层切分 |
| 合成数据生成 | `scripts/generate_nsfw_training_data.py` | 复用 `virtual-life.toml` 的 `[model]`，支持断点续跑 |
| 清洗切分 | `scripts/clean_nsfw_dataset.py` | 生成 `train/val/test.jsonl` 和统计文件 |
| 训练 | `scripts/train_nsfw_classifier.py` | `BertForSequenceClassification`，CPU 冻结层、类别权重 |
| 评估 | `scripts/evaluate_classifier.py` | 阈值后指标、混淆矩阵、延迟分位数、验收门禁 |
| 阈值调优 | `scripts/tune_thresholds.py` | 直接搜索实际部署的有序规则，而不是错误的逐类 `argmax` |
| ONNX 导出 | `scripts/export_onnx.py` | FP32 校验 + 动态 UInt8，并用 64 条留出样本校验一致性 |
| 推理服务 | `scripts/nsfw_server.py` | `POST /classify` 和 Jev 兼容的 `POST /v1/decide` |
| 测试 | `backend/tests/test_scene_classifier.py` | 19 项测试，不依赖 torch |

默认数据目录是 `.data/nsfw_classifier/`，已经在 `.gitignore` 的 `.data/` 规则内。模型、训练集和预测文件都不要提交。

## 2. 对设计稿的关键修正

| 设计稿问题 | 实际实现 |
|---|---|
| `ClassifierModel.from_pretrained()` 不存在（自定义 `nn.Module` 没有该方法） | 直接使用 `BertForSequenceClassification`，保存/加载标准 checkpoint |
| `"layer.1." in name` 会同时匹配 `layer.10.`、`layer.11.` | 用正则提取层号，只冻结真正的 `encoder.layer.N` |
| 6000 条原始样本不可能切出 12000/4000/2400 | 切分比例可配置；默认 70/15/15，清洗脚本输出真实计数并支持 `--min-per-label` |
| 逐类阈值用 `np.argmax(probs > t)`，完全不是该类的一对多 F1 | 直接对部署规则做三维网格搜索，带 explicit recall / daily precision 约束 |
| 四阈值里 `daily` 永远在最后回退，阈值实际不起作用 | 明确为三个有效阈值 + `daily` 兜底；`daily` 字段保留只为兼容 |
| 模型在 `chinese-roberta-wwm-ext` 与 `rbt3` 之间混用 | 代码只认 `--model`，默认 `hfl/chinese-roberta-wwm-ext` |
| `clean_nsfw_dataset.py` 使用了未导入的 `random` | 已移除，排序/抽样统一使用种子化 RNG |
| 用 `SequenceMatcher` 做 O(n²) 去重 | 字符 shingle + 倒排索引；先精确去重，再做近似去重 |
| ONNX 导出导入无关 tokenizer，且没有一致性校验 | 导出后与 PyTorch 对比 FP32 最大误差；动态 UInt8 另用 64 条留出样本校验 argmax 一致率，未通过时服务回退 FP32 |
| `data/` 路径没有忽略，容易提交敏感数据 | 改用 `.data/`，并限制文件权限为 644 |
| 合成数据只按标签生成，容易学到提示词风格 | 加入日常/情绪硬负例、医学/法律/新闻语境负例和边界样本 |

另外增加了设计稿没有写的两道数据约束：

- 未成年人相关文本在清洗阶段直接丢弃（`未成年`、`儿童`、`萝莉`、`17岁` + 性内容等组合）。
- 可选的 PII 脱敏（邮箱、手机号、证件号、链接、账号）。

这两道约束是启发式兜底，不是完整的内容安全系统。

## 3. 安装

运行时依赖不变。训练/评估/导出使用单独的 `train` 依赖组：

```bash
uv sync                        # 运行时，不安装 torch
uv sync --group train          # 训练、评估、ONNX；torch 走 CPU wheel
```

`pyproject.toml` 已配置 `pytorch-cpu` 索引，避免默认拉取 CUDA 依赖。Python 3.12+；当前环境 Python 3.14.3 已验证导入、训练、评估和导出流程。

推理服务同样需要 `torch` 或 `onnxruntime` + `transformers`；只启动主 API、不启用场景路由时不需要安装这一组。

如果团队仍希望隔离训练环境，可以在单独目录创建 Python 3.12 虚拟环境，再安装同一组依赖；服务端不需要 torch 时可以继续只用运行时时依赖。

## 4. 完整流程

### 4.1 生成合成样本

先确认 `virtual-life.toml` 的 `[model]` 指向本地 OpenAI 兼容端点。脚本会读取 `base_url`、`name`、`api_key`、`timeout_seconds`。

```bash
# 只打印提示词，不调用模型
uv run python scripts/generate_nsfw_training_data.py --dry-run

# 小规模冒烟：每类 1 批，总共最多 4 次调用
uv run python scripts/generate_nsfw_training_data.py \
  --batches-per-label 1 --messages-per-batch 10 --limit 4

# 正式生成：每类 150 批 × 10 条 = 1500 条/类，共 6000 条
uv run python scripts/generate_nsfw_training_data.py \
  --batches-per-label 150 --messages-per-batch 10

# 只补成人向问题咨询，归入 explicit，不新增第五类
uv run python scripts/generate_nsfw_training_data.py \
  --label explicit --explicit-mode consultation \
  --batches-per-label 40 --messages-per-batch 10 \
  --output-dir .data/nsfw_classifier/raw_consult
```

`explicit` 不只表示直接求欢，也覆盖避孕失败、性病检查、性交疼痛、性欲变化、
性功能、伴侣沟通等成人向性健康/性行为问题咨询。这类消息虽然语气是提问，仍然
属于 NSFW，应走敏感链路。`--explicit-mode mix` 会按 `--consultation-ratio`
混合直接意图与咨询样本；`--explicit-mode direct` 保持原来的直接意图数据。
`--consultation-topic` 可强制单一咨询主题，便于按主题均衡生成，避免模型反复
输出疼痛、出血等同一种问法。

边界上，避孕、性病、性行为、性功能、性传播风险归 `explicit`；单纯的月经不调、
备孕体检、前列腺增生等非性行为医疗问题仍归 `daily`，避免把所有生殖健康问题
都升级到敏感链路。

输出：

```text
.data/nsfw_classifier/raw/{daily,emotional,intimate,explicit}.jsonl
.data/nsfw_classifier/train_raw.jsonl
```

支持中断续跑：已有文本会进入去重集合，不会重复写入；`--overwrite` 才会清空对应标签。`--json-mode/--no-json-mode` 控制是否发送 `response_format`，服务端拒绝时会自动重试一次。

生成量建议：每类至少 1000 条可用样本，最好 1500～3000 条。真实用户分布短、口语化、有错别字，合成数据只能作为冷启动。

### 4.2 清洗与切分

```bash
uv run python scripts/clean_nsfw_dataset.py \
  --input .data/nsfw_classifier/train_raw.jsonl \
  --output-dir .data/nsfw_classifier \
  --min-per-label 200
```

输出：

```text
.data/nsfw_classifier/train.jsonl
.data/nsfw_classifier/val.jsonl
.data/nsfw_classifier/test.jsonl
.data/nsfw_classifier/dataset_stats.json
```

默认 70/15/15。切分按标签分层，先去重再切分，保证同一条文本不会跨 split。若某一类 train 少于 `--min-per-label`，脚本以非零状态退出，避免训练出严重偏斜的模型。

真实日志回灌时才使用 `--redact`。合成数据一般不需要，但加上也不会破坏语义。

### 4.3 训练

```bash
uv run python scripts/train_nsfw_classifier.py \
  --data-dir .data/nsfw_classifier \
  --output .data/nsfw_classifier/model/final \
  --epochs 5 \
  --freeze-layers 8
```

默认是 CPU 训练：`batch_size=32`、`lr=2e-5`、`max_length=128`、冻结前 8 层编码器。8 核机器上 6000 条 × 5 epoch 仍然需要数小时；先用 `--max-train-samples 200 --epochs 1` 验证链路，再跑全量。

产物：

```text
model/final/
├── config.json
├── model.safetensors
├── tokenizer.json / vocab.txt
├── manifest.json          # 版本、数据量、指标、延迟、验收结果
├── thresholds.json        # 初始阈值
├── val_predictions.jsonl  # 调阈值用
└── test_predictions.jsonl # 最终评估用
```

训练使用验证集选择 best checkpoint，测试集只在最后评估一次。不要用测试集调阈值。

### 4.4 调阈值

```bash
uv run python scripts/tune_thresholds.py \
  --artifact .data/nsfw_classifier/model/final \
  --fail-under
```

搜索的是实际部署规则：`explicit → intimate → emotional → daily`。目标：

- `explicit` recall ≥ 0.92（漏判比误升更严重）
- `daily` precision ≥ 0.85（避免正常内容误走本地）
- 满足约束后最大化 macro F1；同等指标下优先接近默认阈值

如果约束不可达，脚本会报告 `"feasible": false`，并选择 explicit recall 最高的组合，而不是静默降级。此时应补数据或重训。

### 4.5 评估

```bash
uv run python scripts/evaluate_classifier.py \
  --artifact .data/nsfw_classifier/model/final \
  --data .data/nsfw_classifier/test.jsonl \
  --fail-under
```

验收门禁：

| 指标 | 门禁 |
|---|---|
| accuracy | > 0.88 |
| macro F1 | > 0.85 |
| explicit recall | > 0.92 |
| daily precision | > 0.85 |
| 最差类别 F1 | > 0.75 |
| 单条 P99 延迟 | < 50 ms |

延迟必须在目标机器上实测。设计稿里的 25–30 ms 是理想值；8 核 CPU、128 token、PyTorch 全精度通常更慢。优先用已验证的 ONNX UInt8 或 FP32 ONNX，或增加线程，而不是改验收标准。

### 4.6 导出 ONNX

```bash
uv run python scripts/export_onnx.py \
  --artifact .data/nsfw_classifier/model/final
```

会生成 `model.onnx` 和 `model.int8.onnx`，并校验 FP32 最大误差、动态 UInt8 在 64 条留出样本上的 argmax 一致率。只有 `int8_verified: true` 时，服务端 `--backend auto` 才会优先使用量化模型；否则自动回退 FP32。`--no-int8` 可在评估时强制 FP32。

### 4.7 启动服务

```bash
uv run python scripts/nsfw_server.py \
  --artifact .data/nsfw_classifier/model/final \
  --backend auto \
  --threads 8 \
  --host 127.0.0.1 --port 8039
```

简单接口：

```bash
curl -s localhost:8039/classify \
  -H 'content-type: application/json' \
  -d '{"text":"今天好累，想抱抱你"}'
```

返回：

```json
{
  "label": "intimate",
  "confidence": 0.61,
  "scores": {"daily": 0.18, "emotional": 0.12, "intimate": 0.61, "explicit": 0.09},
  "reason": "threshold",
  "threshold": 0.55,
  "model": "hfl/chinese-roberta-wwm-ext",
  "version": "v1-2026-10-22",
  "backend": "onnx",
  "latency_ms": 4.2
}
```

可用环境变量 `VIRTUAL_LIFE_SCENE_CLASSIFIER_TOKEN` 或 `--token` 开启 Bearer 鉴权。服务不记录请求文本。

## 5. 运行时接入

主 API 现已支持把分类器嵌入进程，并提供两种互斥模式：

```toml
[scene_classifier]
mode = "off"          # off | observe | route
artifact = ".data/nsfw_classifier/model/final"
backend = "auto"
max_length = 128
threads = 0
prefer_int8 = true
max_chars = 2048
timeout_seconds = 1.0
# route 模式至少配置一个非空模型；未列出或留空的标签沿用 [model].name。
models = { daily = "online-cheap-model", emotional = "online-strong-model", intimate = "local-uncensored-model", explicit = "local-uncensored-model" }
```

- `observe`：旁路检测。分类器只把标签、分数、版本和延迟写入对话回执的 `scene` 字段；回复模型、提示、流式草稿和最终输出完全不变。
- `route`：直接分流。标签命中 `models` 时只覆盖本轮 `reply_model`，仍然经过原有所有权、epoch、租约、预算、提示和输出 gate。未命中或分类失败时回退到 `[model].name` 或既有路由器结果。
- 分类器加载、推理、超时或缺少 `onnxruntime`/`transformers` 时均 fail-open：对话继续，`scene.error_code` 记录原因，不会阻塞生成或输出。
- 回执只保存标签、分数和诊断元数据，不保存原始用户文本；`scene` 是诊断元数据，不是内容过滤器，也不授予输出权限。
- `GET /v1/capabilities` 的 `scene_classifier` 对象报告 `mode`、`available`、`backend`、`version`、`route_models` 和失败原因。

主进程内推理会增加约数百 MB 内存；如果更看重隔离、独立重启和限流，仍可使用下面的独立服务方案。

### 5.1 独立服务 + `model_routing`

服务端已经实现 Jev `choice` 兼容接口 `POST /v1/decide`，因此不需要修改 `backend/virtual_life/model_routing.py`。把路由的 `criteria` 键写成场景标签即可：

```toml
[model_routing]
enabled = true
gate = "jev"
base_url = "http://127.0.0.1:8039/v1/decide"
model = "scene-classifier"
timeout_seconds = 0.5
min_confidence = 0.0
default_route = "daily"

[[model_routing.routes]]
id = "daily"
criteria = "日常闲聊、工作学习、天气美食"
reply_model = "online-cheap-model"

[[model_routing.routes]]
id = "emotional"
criteria = "情绪倾诉、安慰、孤独、焦虑"
reply_model = "online-strong-model"

[[model_routing.routes]]
id = "intimate"
criteria = "亲密、暧昧、调情、暗示"
reply_model = "local-uncensored-model"

[[model_routing.routes]]
id = "explicit"
criteria = "直接性意图、露骨内容，或成人向性健康/性行为问题咨询"
reply_model = "local-uncensored-model"
```

注意：

- `HttpChoiceRouter` 只会把配置了 `reply_model`（或 `vision_model`）的路由发给分类器；四条路由都必须有可用的 `reply_model`。
- `min_confidence` 初始建议设 `0`。`daily` 的 confidence 是 softmax 概率，可能并不高；设高会把正常消息频繁打回默认路由。
- 分类器超时、不可用、返回未知标签或低置信度时，现有路由器自动回退到 `default_route`，不会阻塞对话。
- 这只是选择模型，不改变输出权限、租约、记忆写入或安全检查。

### 5.2 旁路记录的核对与回训

`observe` 模式下的判断结果有三条可查路径，全部只包含标签、分数和诊断元数据，不含用户原文：

1. 对话回执的 `scene` 字段（`dialogue_commands.result_json`），可用 `GET /v1/companions/{id}/dialogue/{command_id}` 取回。
2. 每轮一条 INFO 日志 `scene_classifier companion_id=... command_id=... label=... daily=... emotional=... intimate=... explicit=... routed_model=... latency_ms=... error_code=...`，写入 `.data/logs/virtual-life.log` 和 `.data/server.log`。
3. 批量导出供人工核对：

```bash
# 只读打开运行时数据库，把回执里的 scene 与 companion.dialogue_received 事件里的原文对齐
uv run python scripts/export_scene_observations.py \
    --output .data/nsfw_classifier/observe_review.jsonl --sensitive-only --dedupe
```

导出行以 `text` + `label` 开头，附带 `confidence`、`scores`、`command_id`、`created_at`、`classifier_version`，与 `train.jsonl` 同构；人工改完标签后可以直接并回训练集，例如：

```bash
uv run python scripts/train_nsfw_classifier.py \
    --data-dir .data/nsfw_classifier/v1 \
    --output .data/nsfw_classifier/model/v2 \
    --version v2-<date>
```

当前生产产物是**第一版** `v1-2026-10-22`（训练 4175 / 验证 895 / 测试 895，阈值 daily 0.0、emotional 0.55、intimate 0.30、explicit 0.35），已放入 `.data/nsfw_classifier/model/final`，不含谐音新增样本。后续版本与数据都保留原样：`v2c8-2026-10-23` 在 `.data/nsfw_classifier/model/v2c8-2026-10-23`，其数据在 `.data/nsfw_classifier/v2c8/`（含 `curated/homophone_nsfw_2026-10-23/`）。两版在同一份 1000 条测试集上的实测对比（`backend=onnx`，`max_length=128`）：

| 版本 | 整体准确率 | explicit recall | daily precision | macro F1 | p99 |
| --- | --- | --- | --- | --- | --- |
| v1-2026-10-22 | 0.927 | 0.852 | 0.825 | 0.929 | 29.9 ms |
| v2c8-2026-10-23 | 0.963 | 0.983 | 0.979 | 0.961 | 33.1 ms |

v1 的缺口主要是新增的 94 条成人向健康咨询类 `explicit` 样本，其中 44 条被错判成 `daily`（多是"要不要用药、要不要复查、用什么消毒"这类咨询表述）；daily precision 只有 0.825，说明 v1 也会把约 50 条非 daily 消息判成 daily。v1 在**它自己的** 895 条测试集上准确率是 0.972，因此缺口来自新增数据而不是模型退化。

## 6. 决策语义

```text
probs = softmax(logits)
if probs.explicit > t_explicit:  explicit
elif probs.intimate > t_intimate: intimate
elif probs.emotional > t_emotional: emotional
else: daily
```

默认阈值：`explicit=0.35`、`intimate=0.55`、`emotional=0.55`、`daily=0`（不使用）。阈值调优会覆盖前三个。顺序意味着只要 explicit 过线，即使 intimate 概率更高也会选 explicit；这是有意为之的“宁可误升”。

## 7. 测试与开发检查

```bash
uv run pytest backend/tests/test_scene_classifier.py -q
uv run ruff check backend/virtual_life/scene_classifier backend/tests/test_scene_classifier.py scripts
uv run ruff format --check backend/virtual_life/scene_classifier backend/tests/test_scene_classifier.py scripts
```

测试覆盖阈值顺序、调优约束、清洗/去重、分层切分、Jev 适配、鉴权和请求校验。训练相关脚本依赖 `--group train`，不在默认测试里执行。

本次实现已用一个 2 层随机初始化 BERT + 合成小数据集跑通：清洗切分 → 1 epoch 训练 → 验证/测试预测 → 阈值调优 → 评估 → ONNX 导出 → ONNX 服务加载。该冒烟结果不代表真实模型准确率。

## 8. 数据与安全边界

- 合成数据只用于训练本项目的路由分类器，保存在 `.data/`，不上传外部服务。
- 永久不训练涉及未成年人的内容；清洗阶段会丢弃明显组合，但不能保证覆盖所有变体。
- 真实用户日志回灌前必须脱敏；不要保存完整对话历史，只保留必要的消息文本和标签。
- 不要在线更新权重。所有迭代都是离线重训、版本化目录、切换服务。
- 分类器不是内容安全措施。即使分类为 `daily`，输出仍必须经过现有输出门禁；即使分类为 `explicit`，也不代表可以绕过任何权限。
- 合成数据会让模型学到提示词风格和模型口癖。上线前必须用真实、脱敏、人工标注的边界样本做一次盲测，重点看 `explicit → daily` 和 `intimate → daily`。

## 9. 后续工作

1. 用真实路由日志收集误判样本，按周回灌 200–500 条，离线重训并做 A/B。
2. 增加温度校准（temperature scaling），让 confidence 可用于 `min_confidence`。
3. 评估多标签（BCE）方案，处理“既情绪化又亲密”的复合消息。
4. 为服务增加批量接口和请求限流；当前单条推理已在线程池中执行。
5. 记录模型版本、阈值版本和数据集哈希，保证线上可追溯。

---

*实现版本：2026-10-22*
*对应设计稿：`docs/nsfw-classifier-training.md`*
