#!/usr/bin/env python
"""Clean raw samples and produce train/val/test JSONL splits.

The cleaning order matters: normalize, redact if requested, length-filter,
reject minor-related content, de-duplicate globally, then split.  De-duplicating
before splitting prevents near-identical messages from leaking across splits.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from virtual_life.scene_classifier.artifact import utc_now
from virtual_life.scene_classifier.dataset import (
    DEFAULT_DEDUP_THRESHOLD,
    MAX_TEXT_CHARS,
    MIN_TEXT_CHARS,
    balance_samples,
    clean_records,
    dataset_stats,
    read_jsonl,
    split_samples,
    write_jsonl,
)
from virtual_life.scene_classifier.labels import LABELS

DEFAULT_DIR = Path(".data/nsfw_classifier")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", action="append", default=[], help="raw JSONL (repeatable)")
    parser.add_argument("--output-dir", default=str(DEFAULT_DIR))
    parser.add_argument("--min-chars", type=int, default=MIN_TEXT_CHARS)
    parser.add_argument("--max-chars", type=int, default=MAX_TEXT_CHARS)
    parser.add_argument("--dedup-threshold", type=float, default=DEFAULT_DEDUP_THRESHOLD)
    parser.add_argument("--ratios", type=float, nargs=3, default=(0.7, 0.15, 0.15))
    parser.add_argument(
        "--per-label", type=int, default=0, help="cap samples per label before splitting"
    )
    parser.add_argument(
        "--min-per-label",
        type=int,
        default=0,
        help="require at least this many train samples per label",
    )
    parser.add_argument(
        "--redact", action="store_true", help="redact emails, phones, IDs and links"
    )
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--stats-out", default="")
    args = parser.parse_args(argv)
    if args.min_chars < 1 or args.max_chars < args.min_chars:
        parser.error("require 1 <= min-chars <= max-chars")
    if not 0 <= args.dedup_threshold <= 1:
        parser.error("dedup-threshold must be between 0 and 1")
    if args.per_label < 0 or args.min_per_label < 0:
        parser.error("per-label and min-per-label must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = [Path(item) for item in (args.input or [DEFAULT_DIR / "train_raw.jsonl"])]
    missing = [str(path) for path in inputs if not path.exists()]
    if missing:
        print(f"input not found: {', '.join(missing)}", file=sys.stderr)
        print("Run scripts/generate_nsfw_training_data.py first.", file=sys.stderr)
        return 2

    records: list[dict] = []
    for path in inputs:
        records.extend(read_jsonl(path))

    filters: Counter[str] = Counter()
    samples = clean_records(
        records,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
        redact=args.redact,
        dedup_threshold=args.dedup_threshold,
        stats=filters,
    )
    if args.per_label:
        samples = balance_samples(samples, args.per_label, seed=args.seed)
    if not samples:
        print("no usable samples after filtering", file=sys.stderr)
        return 1

    splits = split_samples(samples, ratios=args.ratios, seed=args.seed)
    seen: dict[str, set[str]] = {}
    for name, group in splits.items():
        keys = {sample.text.casefold() for sample in group}
        for other_name, other_keys in seen.items():
            overlap = keys & other_keys
            if overlap:
                print(
                    f"internal error: {len(overlap)} texts leak between {other_name} and {name}",
                    file=sys.stderr,
                )
                return 1
        seen[name] = keys
        write_jsonl(Path(args.output_dir) / f"{name}.jsonl", (sample.as_dict() for sample in group))

    output_stats = {
        "created_at": utc_now(),
        "inputs": [str(path) for path in inputs],
        "raw_records": len(records),
        "filters": dict(filters),
        "cleaned": dataset_stats(samples),
        "splits": {name: dataset_stats(group) for name, group in splits.items()},
        "ratios": list(args.ratios),
        "seed": args.seed,
        "dedup_threshold": args.dedup_threshold,
        "redacted": args.redact,
    }
    stats_path = (
        Path(args.stats_out) if args.stats_out else Path(args.output_dir) / "dataset_stats.json"
    )
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        json.dumps(output_stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("raw:", len(records), "kept:", len(samples), "filters:", dict(filters))
    for name, group in splits.items():
        counts = {label: sum(1 for sample in group if sample.label == label) for label in LABELS}
        print(f"{name}: {len(group)} {counts}")
    print(f"stats: {stats_path}")

    for label in LABELS:
        train_count = sum(1 for sample in splits["train"] if sample.label == label)
        if train_count < args.min_per_label:
            print(
                f"train split has only {train_count} {label} samples "
                f"(minimum {args.min_per_label})",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
