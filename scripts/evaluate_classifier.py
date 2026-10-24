#!/usr/bin/env python
"""Evaluate a trained scene classifier on a held-out JSONL split."""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from virtual_life.scene_classifier.artifact import read_thresholds, utc_now
from virtual_life.scene_classifier.dataset import read_jsonl
from virtual_life.scene_classifier.decision import classification_report, decide
from virtual_life.scene_classifier.inference import build_predictor, load_thresholds
from virtual_life.scene_classifier.labels import LABELS


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--data", default=str(Path(".data/nsfw_classifier") / "test.jsonl"))
    parser.add_argument("--thresholds", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--backend", choices=("auto", "torch", "onnx"), default="auto")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--latency-repeats", type=int, default=20)
    parser.add_argument("--no-int8", dest="prefer_int8", action="store_false", default=True)
    parser.add_argument("--fail-under", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.latency_repeats < 1:
        parser.error("batch-size and latency-repeats must be positive")
    return args


def acceptance(report: dict, latency: dict) -> dict[str, bool]:
    return {
        "accuracy>0.88": report["accuracy"] > 0.88,
        "macro_f1>0.85": report["macro_f1"] > 0.85,
        "explicit_recall>0.92": report["classes"]["explicit"]["recall"] > 0.92,
        "daily_precision>0.85": report["classes"]["daily"]["precision"] > 0.85,
        "worst_f1>0.75": report["worst_f1"] > 0.75,
        "p99<50ms": latency["p99_ms"] < 50,
    }


def latency_summary(predictor, texts: list[str], repeats: int) -> dict:
    for text in texts[:3]:
        predictor.predict(text)
    timings: list[float] = []
    for index in range(repeats):
        text = texts[index % len(texts)]
        started = time.perf_counter()
        predictor.predict(text)
        timings.append((time.perf_counter() - started) * 1000)
    return {
        "p50_ms": round(float(np.percentile(timings, 50)), 3),
        "p95_ms": round(float(np.percentile(timings, 95)), 3),
        "p99_ms": round(float(np.percentile(timings, 99)), 3),
        "repeats": repeats,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_path = Path(args.data)
    records = read_jsonl(data_path)
    if args.max_samples:
        records = records[: args.max_samples]
    if not records:
        print(f"no samples in {data_path}", file=sys.stderr)
        return 2
    texts: list[str] = []
    truth: list[str] = []
    for record in records:
        text, label = record.get("text"), record.get("label")
        if not isinstance(text, str) or label not in LABELS:
            print(f"invalid record: {record!r}", file=sys.stderr)
            return 2
        texts.append(text)
        truth.append(label)

    limits = read_thresholds(args.thresholds) if args.thresholds else load_thresholds(args.artifact)
    threads = args.threads or None
    predictor = build_predictor(
        args.artifact,
        backend=args.backend,
        threads=threads,
        prefer_int8=args.prefer_int8,
    )
    predictor.warmup()
    rows: list[list[float]] = []
    for start in range(0, len(texts), args.batch_size):
        batch = texts[start : start + args.batch_size]
        rows.extend(
            list(row) for row in predictor.predict_proba_many(batch, batch_size=args.batch_size)
        )
    probabilities = np.asarray(rows, dtype=np.float64)
    argmax_predictions = [LABELS[int(np.argmax(row))] for row in probabilities]
    threshold_predictions = [decide(row, limits).label for row in probabilities]
    threshold_report = classification_report(truth, threshold_predictions)
    argmax_report = classification_report(truth, argmax_predictions)
    latency = latency_summary(predictor, texts, args.latency_repeats)
    gates = acceptance(threshold_report, latency)

    report = {
        "created_at": utc_now(),
        "artifact": str(args.artifact),
        "backend": predictor.backend,
        "version": predictor.version,
        "thresholds": limits.as_dict(),
        "samples": len(texts),
        "thresholded": threshold_report,
        "argmax": argmax_report,
        "latency": latency,
        "acceptance": gates,
    }
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for text, label, row in zip(texts, truth, probabilities, strict=True):
                stream.write(
                    json.dumps(
                        {
                            "text": text,
                            "label": label,
                            "probs": [float(value) for value in row],
                            "argmax": LABELS[int(np.argmax(row))],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
    if args.json_out:
        json_out = Path(args.json_out)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_under and not all(gates.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
