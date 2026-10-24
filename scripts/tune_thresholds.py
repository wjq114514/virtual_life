#!/usr/bin/env python
"""Tune the ordered decision thresholds on validation predictions."""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from virtual_life.scene_classifier.artifact import THRESHOLDS_NAME, write_thresholds
from virtual_life.scene_classifier.dataset import read_jsonl
from virtual_life.scene_classifier.decision import classification_report, decide, tune_thresholds
from virtual_life.scene_classifier.labels import LABELS


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--artifact", default=str(Path(".data/nsfw_classifier") / "model" / "final")
    )
    parser.add_argument("--predictions", default="")
    parser.add_argument("--test-predictions", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--target-explicit-recall", type=float, default=0.92)
    parser.add_argument("--target-daily-precision", type=float, default=0.85)
    parser.add_argument("--fail-under", action="store_true")
    args = parser.parse_args(argv)
    for name, value in (
        ("target-explicit-recall", args.target_explicit_recall),
        ("target-daily-precision", args.target_daily_precision),
    ):
        if not 0 <= value <= 1:
            parser.error(f"{name} must be between 0 and 1")
    return args


def load_predictions(path: Path) -> tuple[list[str], np.ndarray]:
    records = read_jsonl(path)
    labels: list[str] = []
    rows: list[list[float]] = []
    for record in records:
        label = record.get("label")
        probs = record.get("probs")
        if label not in LABELS or not isinstance(probs, list) or len(probs) != len(LABELS):
            raise SystemExit(f"invalid prediction record in {path}: {record!r}")
        labels.append(label)
        rows.append([float(value) for value in probs])
    if not rows:
        raise SystemExit(f"no predictions in {path}")
    return labels, np.asarray(rows, dtype=np.float64)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    artifact = Path(args.artifact)
    predictions_path = (
        Path(args.predictions) if args.predictions else artifact / "val_predictions.jsonl"
    )
    if not predictions_path.exists():
        print(
            f"missing validation predictions: {predictions_path}\n"
            "Run scripts/train_nsfw_classifier.py first.",
            file=sys.stderr,
        )
        return 2
    truth, probabilities = load_predictions(predictions_path)
    result = tune_thresholds(
        probabilities,
        truth,
        target_explicit_recall=args.target_explicit_recall,
        target_daily_precision=args.target_daily_precision,
    )
    output_path = Path(args.output) if args.output else artifact / THRESHOLDS_NAME
    write_thresholds(
        output_path,
        result.thresholds,
        metrics=result.metrics,
        targets={
            "explicit_recall": result.target_explicit_recall,
            "daily_precision": result.target_daily_precision,
            "feasible": result.feasible,
        },
    )
    summary = {
        "thresholds": result.thresholds.as_dict(),
        "feasible": result.feasible,
        "evaluated": result.evaluated,
        "validation": result.metrics,
        "output": str(output_path),
    }
    test_path = (
        Path(args.test_predictions)
        if args.test_predictions
        else artifact / "test_predictions.jsonl"
    )
    if test_path.exists() and test_path != predictions_path:
        test_truth, test_probabilities = load_predictions(test_path)
        predictions = [decide(row, result.thresholds).label for row in test_probabilities]
        summary["test"] = classification_report(test_truth, predictions)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.fail_under and not result.feasible:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
