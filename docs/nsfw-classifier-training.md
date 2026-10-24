# NSFW 场景分类器训练指南

本文档用于在 virtual-life 项目内训练一个**本地 NSFW 场景分类器**，作为场景路由（`docs/` 内路由层）的 Layer 4 兜底信号。目标：在 CPU 上以 <50ms 延迟判定用户当前消息属于哪种场景档位，用于决定是否切到本地无审查模型。

**定位**：分类器不是内容过滤器，只输出**场景档位标签**供路由器使用。分类结果不决定"能否生成 NSFW"，只决定"走哪条推理链路"。

**范围**：本文档只覆盖训练流程。集成到 `router.py` / `perceive_scene()` 的接线方式见项目主文档。

---

## 1. 模型选择

**本项目固定使用 `hfl/chinese-roberta-wwm-ext`**（102M 参数，RoBERTa 中文版全词掩码增强）。相比更小的蒸馏版本（如 rbt3），推理稍慢但泛化更稳，误判率更低，适合生产环境。

参考对比表（本项目用第一行）：

| 模型 | 参数量 | CPU 推理延迟 (128 tokens) | 中文质量 | 用途 |
|---|---|---|---|---|
| **`hfl/chinese-roberta-wwm-ext`** | 102M | ~25-30ms | ⭐⭐⭐⭐⭐ | **本项目使用** |
| `hfl/rbt3` | 59M | ~15-20ms | ⭐⭐⭐⭐⭐ | 备选（更快，但泛化稍弱） |
| `hfl/chinese-bert-wwm-ext` | 102M | ~25ms | ⭐⭐⭐⭐ | 备选（BERT 版） |
| `hfl/chinese-roberta-wwm-ext-large` | 355M | ~80ms | ⭐⭐⭐⭐⭐ | 太大，不推荐 |
| `distilbert-base-uncased` | 66M | ~15ms | ⭐⭐ | ❌ 英文模型，中文场景效果差 |

加载方式：

```python
from transformers import BertTokenizer, BertModel
tokenizer = BertTokenizer.from_pretrained("hfl/chinese-roberta-wwm-ext")
model = BertModel.from_pretrained("hfl/chinese-roberta-wwm-ext")
```

**注意 tokenizer 加载时必须用 `BertTokenizer` 而不是 `AutoTokenizer`**（HFL 官方仓库的说明）<ref>fb03.1</ref>。RoBERTa 系列用 `RobertaTokenizer` 亦可，但 HFL 中文版的词表是 BERT-style，`BertTokenizer` 兼容更好。

---

## 2. 标签体系

不要做二分类（SFW/NSFW），你的项目里 NSFW 内部还有分级，且"情绪强度"和"话题类型"是分离的信号。**推荐三分类或四分类**：

### 方案 A：三分类（简单起步）

```
0 = sfw       # 正常日常对话，走在线便宜模型
1 = intimate  # 亲密/调情/暧昧，切本地
2 = explicit  # 露骨/直接 NSFW，切本地（可开启更宽松的推理参数）
```

### 方案 B：四分类（更精细）⭐ 推荐

```
0 = daily        # 日常闲聊、工作学习、天气美食
1 = emotional    # 情感倾诉、心事、难过/开心
2 = intimate     # 亲密、暧昧、调情、暗示
3 = explicit     # 直接 NSFW 内容
```

### 方案 C：多标签（未来扩展）

用 BCEWithLogitsLoss 做多标签，每条样本可以同时命中多个标签。适合"用户既在表达情绪又在暗示亲密"这种复合场景。

**建议**：先用方案 B（四分类）跑通，如果误判率合适就冻结。多标签方案作为 v2。

---

## 3. 数据集构建

**没有现成的中文 NSFW RP 数据集**——你必须自己造。以下三条路径按性价比排序。

### 3.1 冷启动：从项目自身产生数据（最快 ⭐⭐⭐⭐⭐）

**这是本项目特有的优势**。你的项目已经能跑主模型，可以直接让主模型生成训练数据。

```python
# scripts/generate_nsfw_training_data.py
"""
用本地主模型生成训练样本。
注意：这里生成的数据仅用于训练分类器，不用于其他目的。
"""
from openai import OpenAI
import json, os, random

client = OpenAI(
    base_url="http://localhost:8000/v1",  # vLLM 或 Ollama
    api_key="local",
)

PROMPT_TEMPLATES = {
    "daily": [
        "你正在写一段 {persona} 与用户的日常闲聊对话，话题是 {topic}。生成 5 条用户消息。",
        "{persona} 正在和用户聊 {topic}，写出用户可能说的 5 句话。",
    ],
    "emotional": [
        "生成 5 条用户在向 {persona} 倾诉情绪的消息，情绪是 {emotion}。",
        "{persona} 收到用户的情感支持请求，情绪是 {emotion}，写出 5 条可能的用户消息。",
    ],
    "intimate": [
        "生成 5 条用户在和 {persona} 调情、暗示亲密关系的消息。氛围是 {mood}。",
        "{persona} 正在和用户暧昧互动，氛围 {mood}，写出 5 条用户可能说的话。",
    ],
    "explicit": [
        "生成 5 条用户直接向 {persona} 表达 NSFW 意愿的消息。风格是 {style}。",
        "{persona} 和用户进入亲密场景，用户可能说 5 句话表达这个意思，风格 {style}。",
    ],
}

TOPICS = ["今天上班", "晚饭", "电影推荐", "周末计划", "天气"]
EMOTIONS = ["难过", "焦虑", "被骂了", "开心", "委屈"]
MOODS = ["害羞", "试探", "主动", "撒娇"]
STYLES = ["直白", "含蓄", "命令式", "撒娇"]

def generate_batch(count_per_label: int = 300) -> list[dict]:
    samples = []
    for label, templates in PROMPT_TEMPLATES.items():
        for i in range(count_per_label):
            params = random.sample(
                [PROMPT_TEMPLATES[label][0], PROMPT_TEMPLATES[label][1]], 1
            )[0]
            message = client.chat.completions.create(
                model="hauhaucs-qwen3.5-35b",
                messages=[{"role": "user", "content": params.format(
                    persona="虚拟陪伴角色",
                    topic=random.choice(TOPICS),
                    emotion=random.choice(EMOTIONS),
                    mood=random.choice(MOODS),
                    style=random.choice(STYLES),
                )}],
                temperature=0.95,
                max_tokens=800,
            ).choices[0].message.content
            
            # 拆出多条用户消息
            for line in message.strip().split("\n"):
                line = line.strip(" ·•·")
                if len(line) >= 5 and len(line) <= 200:
                    samples.append({"text": line, "label": label})
    return samples

if __name__ == "__main__":
    samples = generate_batch(300)
    os.makedirs("data/nsfw_classifier", exist_ok=True)
    with open("data/nsfw_classifier/train_raw.jsonl", "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"生成 {len(samples)} 条原始样本")
```

**预期产出**：4 类 × 300 条 prompt × 5 条消息/prompt ≈ **6000 条原始样本**。

### 3.2 人工清洗与增强（必须做）

原始生成数据有噪声，必须清洗：

```python
# scripts/clean_nsfw_dataset.py
"""
清洗生成的样本：
1. 去重（相似度 > 0.9 的合并）
2. 长度过滤（<5 或 >200 字符丢弃）
3. 边界样本人工审核
4. 平衡各类别数量
"""
import json
from difflib import SequenceMatcher

def dedup(samples, threshold=0.9):
    unique = []
    for s in samples:
        is_dup = False
        for u in unique:
            if SequenceMatcher(None, s["text"], u["text"]).ratio() > threshold:
                is_dup = True
                break
        if not is_dup:
            unique.append(s)
    return unique

def filter_length(samples, min_len=5, max_len=200):
    return [s for s in samples if min_len <= len(s["text"]) <= max_len]

def balance(samples, target_per_label):
    from collections import defaultdict
    by_label = defaultdict(list)
    for s in samples:
        by_label[s["label"]].append(s)
    balanced = []
    for label, group in by_label.items():
        random.shuffle(group)
        balanced.extend(group[:target_per_label])
    return balanced
```

**建议比例**：训练集每类 1500 条，测试集每类 500 条，验证集每类 300 条，共 **12000 条训练 + 4000 测试 + 2400 验证**。

### 3.3 真实用户数据回灌（v2 迭代）

上线后收集路由决策日志（见项目内 `route_log`），人工标注误判样本，追加到训练集。每次迭代 200-500 条新数据即可显著降误差。

**注意**：真实用户消息要脱敏后再入库（去除用户名、地址、敏感个人信息），只保留对话内容本身。

---

## 4. 训练流程

### 4.1 依赖

```bash
cd /home/wjqawa/virtual_life
uv add transformers torch datasets scikit-learn
# 或 CPU 版 torch
uv add torch --index-url https://download.pytorch.org/whl/cpu
```

### 4.2 训练脚本

```python
# scripts/train_nsfw_classifier.py
"""
训练 NSFW 场景分类器。
目标：CPU 推理 <50ms，测试集 accuracy > 0.88。
"""
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from transformers import BertTokenizer, BertModel, Trainer, TrainingArguments
from datasets import Dataset as HFDataset
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support
import json

LABELS = ["daily", "emotional", "intimate", "explicit"]

class TextDataset(Dataset):
    def __init__(self, texts, labels, tokenizer, max_len=128):
        encodings = tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=max_len,
            return_tensors="pt",
        )
        self.encodings = encodings
        self.labels = torch.tensor(labels)

    def __getitem__(self, idx):
        item = {k: v[idx] for k, v in self.encodings.items()}
        item["labels"] = self.labels[idx]
        return item

    def __len__(self):
        return len(self.labels)


class ClassifierModel(nn.Module):
    """BERT encoder + 分类头"""
    def __init__(self, model_name, num_labels):
        super().__init__()
        self.bert = BertModel.from_pretrained(model_name)
        # 冻结部分层，只微调后 N 层 + 分类头（加速 CPU 训练）
        for name, param in self.bert.named_parameters():
            if "layer.0." in name or "layer.1." in name:
                param.requires_grad = False
        self.classifier = nn.Sequential(
            nn.Dropout(0.1),
            nn.Linear(self.bert.config.hidden_size, num_labels),
        )

    def forward(self, input_ids, attention_mask):
        outputs = self.bert(
            input_ids=input_ids,
            attention_mask=attention_mask,
        )
        # 用 [CLS] token 做分类
        cls_output = outputs.last_hidden_state[:, 0, :]
        return self.classifier(cls_output)


def main():
    # 1. 加载数据
    train_data = [json.loads(l) for l in open("data/nsfw_classifier/train.jsonl", encoding="utf-8")]
    val_data = [json.loads(l) for l in open("data/nsfw_classifier/val.jsonl", encoding="utf-8")]
    test_data = [json.loads(l) for l in open("data/nsfw_classifier/test.jsonl", encoding="utf-8")]
    
    tokenizer = BertTokenizer.from_pretrained("hfl/chinese-roberta-wwm-ext")
    model_name = "hfl/chinese-roberta-wwm-ext"
    num_labels = len(LABELS)
    
    train_ds = TextDataset(
        [d["text"] for d in train_data],
        [LABELS.index(d["label"]) for d in train_data],
        tokenizer,
    )
    val_ds = TextDataset(
        [d["text"] for d in val_data],
        [LABELS.index(d["label"]) for d in val_data],
        tokenizer,
    )
    
    model = ClassifierModel(model_name, num_labels)
    model.to("cpu")  # P40 弃用，CPU 训练
    
    # 2. 训练配置
    args = TrainingArguments(
        output_dir="data/nsfw_classifier/model",
        num_train_epochs=5,
        per_device_train_batch_size=32,
        per_device_eval_batch_size=64,
        learning_rate=2e-5,        # 微调小模型，学习率不能太大
        weight_decay=0.01,
        warmup_ratio=0.1,
        fp16=False,                # CPU 无 fp16
        logging_steps=50,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="eval_f1",
        greater_is_better=True,
        report_to="none",
    )
    
    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        preds = torch.argmax(torch.tensor(logits), dim=1).numpy()
        acc = accuracy_score(labels, preds)
        f1 = f1_score(labels, preds, average="macro")
        return {"accuracy": acc, "f1": f1}
    
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        compute_metrics=compute_metrics,
    )
    
    # 3. 训练
    trainer.train()
    trainer.save_model("data/nsfw_classifier/model/final")
    tokenizer.save_pretrained("data/nsfw_classifier/model/final")
    
    # 4. 测试集评估
    test_ds = TextDataset(
        [d["text"] for d in test_data],
        [LABELS.index(d["label"]) for d in test_data],
        tokenizer,
    )
    eval_results = trainer.evaluate(eval_dataset=test_ds)
    print(f"测试集结果: {eval_results}")


if __name__ == "__main__":
    main()
```

### 4.3 训练超参调优

**CPU 训练 chinese-roberta-wwm-ext 12000 条数据的经验值**：

| 参数 | 值 | 说明 |
|---|---|---|
| learning_rate | 2e-5 | 微调起点，若 loss 不降可试 3e-5 |
| batch_size | 32 | 32 核 CPU 可承受 |
| epochs | 5 | 一般 3-5 epoch 收敛 |
| max_length | 128 | NSFW 判定不需要长上下文 |
| warmup_ratio | 0.1 | 前 10% step 线性预热 |
| weight_decay | 0.01 | 常规正则 |

**训练时长**：CPU 32 核，12000 条 × 5 epoch ≈ **5-8 小时**（chinese-roberta-wwm-ext 102M 比蒸馏版慢 2 倍左右）。想加速可以：（1）冻结前 6 层只训后 6 层 + 分类头；（2）max_length 从 128 降到 96；（3）batch_size 提到 64。

**冻结策略**：默认冻结前 2 层（`layer.0.*` `layer.1.*`），只训后 1 层 + 分类头，速度 ×2-3。

### 4.4 早停与过拟合监控

```python
# 监控指标：
# - train_loss: 应平稳下降
# - eval_accuracy: 应上升
# - eval_f1 (macro): 关键指标，反映各类别平衡
# - train/eval loss gap: 超过 0.5 说明过拟合

# 过拟合信号：
# - 单类别 recall > 0.99 但其他类 recall < 0.7 → 类别不平衡
# - val loss 上升而 train loss 继续降 → 停止训练

# 处理：
# 1. 增大 dropout 到 0.2
# 2. 减小学习率到 1e-5
# 3. 增加数据量
# 4. 减少冻结层数（训更多层）→ 一般不建议，会过拟合更快
```

---

## 5. 评估指标

不要只看 accuracy，四分类任务下必须看：

```python
# scripts/evaluate_classifier.py
from sklearn.metrics import classification_report, confusion_matrix

def full_eval(preds, labels, labels_names=LABELS):
    print(classification_report(labels, preds, target_names=labels_names))
    print("混淆矩阵:")
    print(confusion_matrix(labels, preds, labels=range(len(labels_names))))
    print("各类别样本数:", {l: labels.count(l) for l in labels_names})
```

**验收标准**：

| 指标 | 目标值 |
|---|---|
| 总体 accuracy | > 0.88 |
| explicit 类 recall | > 0.92（关键：漏判 = 敏感内容走在线） |
| daily 类 precision | > 0.85（关键：误判 = 正常内容走本地，浪费资源） |
| macro F1 | > 0.85 |
| 最坏类别 F1 | > 0.75 |

**混淆矩阵重点看**：
- `daily → intimate`（误升）：浪费资源但不严重
- `explicit → daily`（误降）：**灾难**，敏感内容走了在线 API 会被拒，且泄露
- `intimate → daily`：亲密对话漏掉，人设崩塌

**优先级**：`explicit` 类的 recall 必须最高。如果它低于 0.9，重训或调阈值。

---

## 6. 阈值调整（关键）

默认 `argmax` 判定太粗暴，路由器需要**不同类用不同阈值**：

```python
# scripts/tune_thresholds.py
"""
针对每个类别学习一个判定阈值，使该类别的 F1 最大化。
"""
import numpy as np
from sklearn.metrics import f1_score

def find_thresholds(probs, labels, labels_names, default_f1_target=0.85):
    """
    对每个类别，用验证集搜索使 F1 最大的阈值。
    probs: (N, num_labels) 概率矩阵
    labels: (N,) 真实标签索引
    """
    thresholds = []
    for i, name in enumerate(labels_names):
        scores = []
        for t in np.arange(0.1, 0.9, 0.05):
            preds = np.argmax(probs > t, axis=1)
            f1 = f1_score(labels, preds, average="macro")
            scores.append((t, f1))
        best_t, best_f1 = max(scores, key=lambda x: x[1])
        thresholds.append(best_t)
        print(f"{name}: threshold={best_t:.2f}, f1={best_f1:.4f}")
    return thresholds
```

**路由决策伪代码**：

```python
def predict_with_thresholds(logits, thresholds):
    probs = softmax(logits)
    scores = probs * thresholds  # 除以阈值（等价），或按下面写
    
    # 硬规则：explicit 阈值最低（最敏感）
    if probs[3] > thresholds[3]:
        return "explicit"
    if probs[2] > thresholds[2]:
        return "intimate"
    if probs[1] > thresholds[1]:
        return "emotional"
    return "daily"
```

**经验值**：
- `daily` 阈值高（0.7+）：避免误判
- `explicit` 阈值低（0.3-0.4）：宁可误升不漏判
- `intimate` 阈值中（0.5-0.6）

---

## 7. 推理集成

### 7.1 独立推理服务（推荐）

用 TEI 或直接起 FastAPI 服务，CPU 上跑：

```python
# scripts/nsfw_server.py
from fastapi import FastAPI
import torch
from transformers import BertTokenizer
from scripts.train_nsfw_classifier import ClassifierModel, LABELS

app = FastAPI()
tokenizer = BertTokenizer.from_pretrained("data/nsfw_classifier/model/final")
model = ClassifierModel.from_pretrained("data/nsfw_classifier/model/final", num_labels=4)
model.eval()
model.to("cpu")

THRESHOLDS = [0.72, 0.55, 0.52, 0.35]  # 训练后调优得到

@app.post("/classify")
async def classify(text: str):
    with torch.no_grad():
        enc = tokenizer(text, return_tensors="pt", truncation=True, max_length=128)
        logits = model(**enc)
        probs = torch.softmax(logits, dim=-1)[0].numpy()
    
    # 带阈值决策
    for i in reversed(range(len(LABELS))):
        if probs[i] > THRESHOLDS[i]:
            return {
                "label": LABELS[i],
                "scores": {LABELS[j]: float(probs[j]) for j in range(4)},
                "confidence": float(probs[i]),
            }
    return {"label": "daily", "scores": {...}, "confidence": float(probs[0])}
```

### 7.2 嵌入主进程（更简单）

如果不想多起一个服务，直接加载进 virtual-life 主进程：

```python
# 在感知层初始化时加载
self.classifier = ClassifierModel.from_pretrained(...)
self.classifier.eval()
self.classifier.to("cpu")

# 感知调用
def perceive_scene(...):
    ...
    if needs_classifier:  # 只在边界场景调用
        with torch.no_grad():
            result = self.classifier.predict(message)
        return result
```

**注意**：加载 chinese-roberta-wwm-ext 到内存约 400MB，冷启动 5-8 秒。首次调用前预热一下（加载后用几条 dummy 文本走一遍 forward 触发算子预热）。

---

## 8. 部署与运维

### 8.1 目录结构

```
virtual-life/
├── docs/nsfw-classifier-training.md    # 本文档
├── scripts/
│   ├── generate_nsfw_training_data.py  # 数据生成
│   ├── clean_nsfw_dataset.py           # 数据清洗
│   ├── train_nsfw_classifier.py        # 训练
│   ├── evaluate_classifier.py          # 评估
│   ├── tune_thresholds.py              # 阈值调优
│   └── nsfw_server.py                  # 推理服务
└── data/
    └── nsfw_classifier/
        ├── train.jsonl
        ├── val.jsonl
        ├── test.jsonl
        └── model/
            └── final/                  # 训练产物
```

### 8.2 版本管理

**分类器模型不要 git add**：

```gitignore
data/nsfw_classifier/model/
data/nsfw_classifier/*.jsonl
!data/nsfw_classifier/example.jsonl
```

模型产物用版本号标记：
- `model/v1-2025-10-22/`（初始训练）
- `model/v2-2026-01-15/`（数据回灌后重训）

配置里明确指定版本：
```toml
[scene_router.classifier]
model_path = "data/nsfw_classifier/model/v1-2025-10-22"
thresholds = [0.72, 0.55, 0.52, 0.35]
```

### 8.3 迭代流程

```
[初始训练]
  ↓
[上线 v1] → 收集路由决策日志 + 用户反馈
  ↓
[每周筛选 200-500 条误判样本]
  ↓
[人工审核 + 打标签]
  ↓
[追加到 train.jsonl]
  ↓
[重新训练 v2]
  ↓
[A/B 测试：新老模型并行跑，比对结果]
  ↓
[切换]
```

**注意**：不要在线更新模型权重。所有迭代都是**离线重训 + 版本切换**。

---

## 9. 常见坑

1. **Tokenizer 加载方式错**：HFL 的 `chinese-roberta-wwm-ext` 必须用 `BertTokenizer`（或 `RobertaTokenizer`），用 `AutoTokenizer` 有时会 fallback 到错的实现<ref>fb03.1</ref>。

2. **数据不均衡**：如果 explicit 类样本远少于 daily 类，训练会偏向多数类。**必须做类别平衡**（下采样多数类 + 上采样少数类）。

3. **训练 loss 降得很快但 eval 不涨**：过拟合。减少 epoch 或增大 dropout。

4. **测试集分布和真实分布不一致**：真实用户消息短、口语化、有错别字。训练数据里最好混入 20% 真实用户消息（脱敏后）。

5. **CPU 推理延迟高**：
   - 检查 `max_length`，不用给太长
   - 用 `torch.set_num_threads(cpu_count)`
   - 批量推理合并请求
   - 极端情况：ONNX Runtime 再压一层，CPU 推理还能快 2-3 倍

6. **模型文件大**：chinese-roberta-wwm-ext 全精度约 400MB，ONNX INT8 后 ~130MB。生产环境建议 ONNX 化：

```python
# scripts/export_onnx.py
from transformers import BertTokenizer, TFGPT2TokenizerFast
import onnx
import torch

tokenizer = BertTokenizer.from_pretrained("hfl/chinese-roberta-wwm-ext")
model = ClassifierModel.from_pretrained("data/nsfw_classifier/model/final", num_labels=4)
model.eval()

dummy = tokenizer("测试文本", return_tensors="pt", padding=True, truncation=True, max_length=128)

torch.onnx.export(
    model,
    (dummy["input_ids"], dummy["attention_mask"]),
    "data/nsfw_classifier/model/final/model.onnx",
    input_names=["input_ids", "attention_mask"],
    output_names=["logits"],
    dynamic_axes={
        "input_ids": {0: "batch", 1: "seq"},
        "attention_mask": {0: "batch", 1: "seq"},
        "logits": {0: "batch"},
    },
    opset_version=14,
)
```

---

## 10. 完整跑通 checklist

给 opencode 执行任务时用这个 checklist 逐项验证：

- [ ] `uv add transformers torch datasets scikit-learn` 完成
- [ ] `hfl/chinese-roberta-wwm-ext` tokenizer 能加载，中文句子能正确分词
- [ ] `generate_nsfw_training_data.py` 生成 ≥6000 条原始样本
- [ ] `clean_nsfw_dataset.py` 清洗后 ≥12000 训练 / 4000 测试 / 2400 验证
- [ ] `train_nsfw_classifier.py` 训练 5 epoch，val_f1 单调上升
- [ ] `evaluate_classifier.py` 测试集 accuracy > 0.88，macro_f1 > 0.85
- [ ] `explicit` 类 recall > 0.92
- [ ] `daily` 类 precision > 0.85
- [ ] `tune_thresholds.py` 输出四个阈值
- [ ] 单次推理 CPU 延迟 < 50ms（P99）
- [ ] `nsfw_server.py` 起服务，`POST /classify` 返回正确结构
- [ ] 模型导出 ONNX 成功，ONNX 推理结果与 PyTorch 一致（差值 < 1e-5）
- [ ] `.gitignore` 已配置
- [ ] 模型版本 tag 已记录

**全部通过后**，再考虑接入 `router.py` / `perceive_scene()`。

---

## 附录 A：数据来源合规

生成的训练数据仅存储在本地 `data/nsfw_classifier/` 目录，用于训练本项目的分类器。**不上传任何外部服务**。真实用户回灌数据必须先脱敏（去除用户名、地址、联系方式、身份证等 PII）。

**不训练**：
- 真实用户的完整对话历史（隐私）
- 涉及未成年人的任何内容（红线，永久禁止）
- 涉及政治敏感、暴恐、违法内容

## 附录 B：参考

- HFL 中文 BERT 系列：<https://huggingface.co/hfl/chinese-roberta-wwm-ext>
- HFL 组织主页：<https://huggingface.co/hfl>
- 谐音语料参考：Project-Gutenberg 中文语料 + 自建谐音表
- DistilBERT 论文（作为方法论参考）：<https://arxiv.org/abs/1910.01108>
- Transformers Trainer 文档：<https://huggingface.co/docs/transformers/main_classes/trainer>
- HFL 加载方式警告：<ref>fb03.1</ref>

---

*文档版本：v1（2026-10-22）*
*适用范围：virtual-life 项目场景路由 Layer 4 分类器*
*负责人：项目维护者*
