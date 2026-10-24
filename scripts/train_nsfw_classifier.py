#!/usr/bin/env python
"""Fine-tune ``hfl/chinese-roberta-wwm-ext`` for scene routing on CPU.

The checkpoint is a standard ``BertForSequenceClassification`` saved with its
tokenizer, thresholds and a manifest.  ``--freeze-layers`` defaults to 8 to keep
CPU training practical; set it to 0 for full fine-tuning when time allows.
"""

import argparse
import inspect
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

try:
    import torch
    import transformers
    from torch.utils.data import Dataset
    from transformers import (
        BertForSequenceClassification,
        BertTokenizer,
        DataCollatorWithPadding,
        Trainer,
        TrainingArguments,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Training dependencies are missing. Run: uv sync --group train") from exc

from virtual_life.scene_classifier.artifact import (
    MANIFEST_NAME,
    THRESHOLDS_NAME,
    utc_now,
    write_manifest,
    write_thresholds,
)
from virtual_life.scene_classifier.dataset import read_jsonl, write_jsonl
from virtual_life.scene_classifier.decision import (
    Thresholds,
    classification_report,
    decide,
)
from virtual_life.scene_classifier.inference import TorchPredictor
from virtual_life.scene_classifier.labels import LABEL_INDEX, LABELS

DEFAULT_DATA = Path(".data/nsfw_classifier")
DEFAULT_OUTPUT = DEFAULT_DATA / "model" / "final"
MAX_LENGTH = 128


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--model", default="hfl/chinese-roberta-wwm-ext")
    parser.add_argument("--version", default="")
    parser.add_argument("--epochs", type=float, default=5.0)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--eval-batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--max-length", type=int, default=MAX_LENGTH)
    parser.add_argument("--freeze-layers", type=int, default=8)
    parser.add_argument("--label-smoothing", type=float, default=0.05)
    parser.add_argument(
        "--no-class-weights", dest="class_weights", action="store_false", default=True
    )
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--threads", type=int, default=0, help="0 = all available CPUs")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--max-train-samples", type=int, default=0)
    parser.add_argument("--max-eval-samples", type=int, default=0)
    parser.add_argument("--logging-steps", type=int, default=25)
    parser.add_argument(
        "--fail-under", action="store_true", help="exit non-zero when acceptance gates fail"
    )
    parser.add_argument("--skip-latency", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.eval_batch_size < 1:
        parser.error("batch sizes must be positive")
    if args.max_length < 16:
        parser.error("max-length must be at least 16")
    if not 0 <= args.freeze_layers <= 64:
        parser.error("freeze-layers must be between 0 and 64")
    if not 0 <= args.label_smoothing < 1:
        parser.error("label-smoothing must be in [0, 1)")
    return args


class TextDataset(Dataset):
    def __init__(self, texts, labels, tokenizer, max_length: int):
        encoded = tokenizer(
            list(texts),
            truncation=True,
            max_length=max_length,
            padding=False,
        )
        self.encodings = dict(encoded)
        self.labels = list(labels)

    def __getitem__(self, index):
        item = {key: value[index] for key, value in self.encodings.items()}
        item["labels"] = self.labels[index]
        return item

    def __len__(self):
        return len(self.labels)


class WeightedTrainer(Trainer):
    def __init__(self, *args, class_weights=None, label_smoothing: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_weights = class_weights
        self.label_smoothing = label_smoothing

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        logits = outputs.logits
        weight = self.class_weights
        if weight is not None:
            weight = weight.to(logits.device)
        loss = torch.nn.functional.cross_entropy(
            logits,
            labels,
            weight=weight,
            label_smoothing=self.label_smoothing,
        )
        return (loss, outputs) if return_outputs else loss


def load_split(data_dir: Path, name: str, limit: int = 0) -> tuple[list[str], list[int], list[str]]:
    path = data_dir / f"{name}.jsonl"
    if not path.exists():
        raise SystemExit(f"missing split: {path}. Run scripts/clean_nsfw_dataset.py first.")
    records = read_jsonl(path)
    if limit:
        records = records[:limit]
    texts: list[str] = []
    labels: list[int] = []
    raw_labels: list[str] = []
    for record in records:
        text = record.get("text")
        label = record.get("label")
        if not isinstance(text, str) or not isinstance(label, str) or label not in LABEL_INDEX:
            raise SystemExit(f"invalid record in {path}: {record!r}")
        texts.append(text)
        labels.append(LABEL_INDEX[label])
        raw_labels.append(label)
    if not texts:
        raise SystemExit(f"split is empty: {path}")
    return texts, labels, raw_labels


def freeze_encoder_layers(model, count: int) -> tuple[int, int]:
    pattern = re.compile(r"(?:^|\.)encoder\.layer\.(\d+)\.")
    total = 0
    trainable = 0
    for name, parameter in model.named_parameters():
        match = pattern.search(name)
        if match and int(match.group(1)) < count:
            parameter.requires_grad = False
        total += parameter.numel()
        if parameter.requires_grad:
            trainable += parameter.numel()
    return total, trainable


def class_weights_for(labels: list[int]) -> torch.Tensor:
    counts = np.bincount(labels, minlength=len(LABELS)).astype(np.float64)
    counts = np.maximum(counts, 1.0)
    weights = len(labels) / (len(LABELS) * counts)
    weights = np.clip(weights, 0.25, 4.0)
    return torch.tensor(weights, dtype=torch.float32)


class MetricsComputer:
    def __call__(self, eval_pred):
        logits, labels = eval_pred
        predictions = np.argmax(logits, axis=-1)
        report = classification_report(labels.tolist(), predictions.tolist())
        return {
            "accuracy": float(report["accuracy"]),
            "macro_f1": float(report["macro_f1"]),
            "explicit_recall": float(report["classes"]["explicit"]["recall"]),
            "daily_precision": float(report["classes"]["daily"]["precision"]),
        }


def probabilities(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exponents = np.exp(shifted)
    return exponents / exponents.sum(axis=1, keepdims=True)


def predictions_for(trainer: Trainer, dataset: TextDataset) -> np.ndarray:
    output = trainer.predict(dataset)
    logits = output.predictions
    if isinstance(logits, tuple):
        logits = logits[0]
    return probabilities(np.asarray(logits, dtype=np.float64))


def threshold_metrics(truth: list[str], rows: np.ndarray, limits: Thresholds) -> dict:
    predictions = [decide(row, limits).label for row in rows]
    return classification_report(truth, predictions)


def write_predictions(path: Path, texts: list[str], truth: list[str], rows: np.ndarray) -> None:
    records = []
    for text, label, row in zip(texts, truth, rows, strict=True):
        probabilities_list = [float(value) for value in row]
        records.append(
            {
                "text": text,
                "label": label,
                "probs": probabilities_list,
                "argmax": LABELS[int(np.argmax(row))],
            }
        )
    write_jsonl(path, records)


def latency_summary(predictor: TorchPredictor, samples: list[str], repeats: int = 20) -> dict:
    for text in samples[:3]:
        predictor.predict(text)
    timings: list[float] = []
    for index in range(repeats):
        text = samples[index % len(samples)]
        started = time.perf_counter()
        predictor.predict(text)
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    return {
        "p50_ms": round(float(np.percentile(timings, 50)), 3),
        "p95_ms": round(float(np.percentile(timings, 95)), 3),
        "p99_ms": round(float(np.percentile(timings, 99)), 3),
        "repeats": repeats,
    }


def acceptance(report: dict, latency: dict | None = None) -> dict[str, bool]:
    gates = {
        "accuracy>0.88": report["accuracy"] > 0.88,
        "macro_f1>0.85": report["macro_f1"] > 0.85,
        "explicit_recall>0.92": report["classes"]["explicit"]["recall"] > 0.92,
        "daily_precision>0.85": report["classes"]["daily"]["precision"] > 0.85,
        "worst_f1>0.75": report["worst_f1"] > 0.75,
    }
    if latency is not None:
        gates["p99<50ms"] = latency["p99_ms"] < 50
    return gates


def training_arguments(args: argparse.Namespace, output: Path) -> TrainingArguments:
    desired = {
        "output_dir": str(output),
        "num_train_epochs": args.epochs,
        "per_device_train_batch_size": args.batch_size,
        "per_device_eval_batch_size": args.eval_batch_size,
        "learning_rate": args.learning_rate,
        "weight_decay": args.weight_decay,
        "warmup_ratio": args.warmup_ratio,
        "logging_steps": args.logging_steps,
        "logging_first_step": True,
        "eval_strategy": "epoch",
        "evaluation_strategy": "epoch",
        "save_strategy": "epoch",
        "save_total_limit": 2,
        "load_best_model_at_end": True,
        "metric_for_best_model": "macro_f1",
        "greater_is_better": True,
        "fp16": False,
        "bf16": False,
        "report_to": "none",
        "dataloader_num_workers": 0,
        "gradient_accumulation_steps": 1,
        "max_grad_norm": 1.0,
        "seed": args.seed,
        "data_seed": args.seed,
        "use_cpu": args.device == "cpu",
        "no_cuda": args.device == "cpu",
        "disable_tqdm": False,
    }
    available = set(inspect.signature(TrainingArguments.__init__).parameters)
    filtered = {key: value for key, value in desired.items() if key in available}
    return TrainingArguments(**filtered)


def build_trainer(
    model,
    args: argparse.Namespace,
    training_args: TrainingArguments,
    train_dataset: TextDataset,
    val_dataset: TextDataset,
    tokenizer,
    class_weights,
) -> WeightedTrainer:
    kwargs = {
        "model": model,
        "args": training_args,
        "train_dataset": train_dataset,
        "eval_dataset": val_dataset,
        "compute_metrics": MetricsComputer(),
        "data_collator": DataCollatorWithPadding(tokenizer),
    }
    parameters = inspect.signature(Trainer.__init__).parameters
    if "processing_class" in parameters:
        kwargs["processing_class"] = tokenizer
    elif "tokenizer" in parameters:
        kwargs["tokenizer"] = tokenizer
    return WeightedTrainer(
        **kwargs,
        class_weights=class_weights,
        label_smoothing=args.label_smoothing,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    threads = args.threads or (os.cpu_count() or 1)
    torch.set_num_threads(max(1, threads))

    data_dir = Path(args.data_dir)
    train_texts, train_labels, _ = load_split(data_dir, "train", args.max_train_samples)
    val_texts, val_labels, val_truth = load_split(data_dir, "val", args.max_eval_samples)
    test_texts, test_labels, test_truth = load_split(data_dir, "test", args.max_eval_samples)
    if not train_texts or not val_texts or not test_texts:
        raise SystemExit("train, val and test splits must all be non-empty")

    tokenizer = BertTokenizer.from_pretrained(args.model)
    model = BertForSequenceClassification.from_pretrained(
        args.model,
        num_labels=len(LABELS),
        id2label={index: label for index, label in enumerate(LABELS)},
        label2id=LABEL_INDEX,
        problem_type="single_label_classification",
        ignore_mismatched_sizes=True,
    )
    total_params, trainable_params = freeze_encoder_layers(model, args.freeze_layers)
    print(
        f"model={args.model} params={total_params:,} trainable={trainable_params:,} "
        f"frozen_layers={args.freeze_layers}"
    )

    train_dataset = TextDataset(train_texts, train_labels, tokenizer, args.max_length)
    val_dataset = TextDataset(val_texts, val_labels, tokenizer, args.max_length)
    test_dataset = TextDataset(test_texts, test_labels, tokenizer, args.max_length)
    training_args = training_arguments(args, Path(args.output))
    weights = class_weights_for(train_labels) if args.class_weights else None
    if weights is not None:
        print("class weights:", [round(float(value), 3) for value in weights])
    trainer = build_trainer(
        model, args, training_args, train_dataset, val_dataset, tokenizer, weights
    )
    trainer.train()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(output))
    tokenizer.save_pretrained(str(output))
    version = args.version or f"v1-{utc_now()[:10]}"
    default_limits = Thresholds()
    write_thresholds(output / THRESHOLDS_NAME, default_limits)

    val_rows = predictions_for(trainer, val_dataset)
    test_rows = predictions_for(trainer, test_dataset)
    write_predictions(output / "val_predictions.jsonl", val_texts, val_truth, val_rows)
    write_predictions(output / "test_predictions.jsonl", test_texts, test_truth, test_rows)
    val_report = threshold_metrics(val_truth, val_rows, default_limits)
    test_report = threshold_metrics(test_truth, test_rows, default_limits)
    raw_test_report = classification_report(
        test_truth, [LABELS[int(np.argmax(row))] for row in test_rows]
    )

    latency = None
    if not args.skip_latency:
        try:
            predictor = TorchPredictor(output, max_length=args.max_length, threads=threads)
            latency = latency_summary(predictor, test_texts)
        except Exception as exc:  # pragma: no cover - artifact issues are reported, not fatal
            print(f"warning: latency benchmark failed: {exc}", file=sys.stderr)

    gates = acceptance(test_report, latency)
    manifest = {
        "version": version,
        "base_model": args.model,
        "labels": list(LABELS),
        "max_length": args.max_length,
        "freeze_layers": args.freeze_layers,
        "parameters": total_params,
        "trainable_parameters": trainable_params,
        "threads": threads,
        "device": args.device,
        "training": {
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "learning_rate": args.learning_rate,
            "label_smoothing": args.label_smoothing,
            "class_weights": (
                [round(float(value), 4) for value in weights] if weights is not None else None
            ),
            "seed": args.seed,
        },
        "data": {
            "train": dataset_stats_from_labels(train_labels),
            "val": dataset_stats_from_labels(val_labels),
            "test": dataset_stats_from_labels(test_labels),
        },
        "metrics": {
            "validation_thresholded": val_report,
            "test_thresholded": test_report,
            "test_argmax": raw_test_report,
        },
        "latency_ms": latency,
        "acceptance": gates,
        "thresholds": default_limits.as_dict(),
        "environment": {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "transformers": transformers.__version__,
        },
    }
    write_manifest(output / MANIFEST_NAME, manifest)

    print(
        json.dumps(
            {"version": version, "test": test_report, "latency": latency},
            ensure_ascii=False,
            indent=2,
        )
    )
    print("acceptance:", gates)
    if args.fail_under and not all(gates.values()):
        return 1
    return 0


def dataset_stats_from_labels(labels: list[int]) -> dict:
    counts = Counter(labels)
    return {
        "total": len(labels),
        "by_label": {label: counts.get(index, 0) for index, label in enumerate(LABELS)},
    }


if __name__ == "__main__":
    raise SystemExit(main())
