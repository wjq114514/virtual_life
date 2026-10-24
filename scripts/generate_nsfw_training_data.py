#!/usr/bin/env python
"""Generate synthetic scene-classifier samples with the configured local model.

Examples
--------
Dry run (prints one prompt per label, no model calls)::

    uv run python scripts/generate_nsfw_training_data.py --dry-run

Small smoke run::

    uv run python scripts/generate_nsfw_training_data.py --batches-per-label 1 --limit 4

The output stays under ``.data/`` and is ignored by git.  Every generated line
is normalized and filtered by the same code used by the cleaning step.
"""

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
from virtual_life.scene_classifier.dataset import (
    contains_minor_content,
    is_usable,
    normalize_text,
    read_jsonl,
    write_jsonl,
)
from virtual_life.scene_classifier.generation import (
    CONSULTATION_TOPICS,
    build_prompt,
    parse_messages,
)
from virtual_life.scene_classifier.labels import LABELS
from virtual_life.settings import Settings

DEFAULT_DIR = Path(".data/nsfw_classifier")
DEFAULT_RAW = DEFAULT_DIR / "raw"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", default="virtual-life.toml", help="runtime TOML used for [model]"
    )
    parser.add_argument(
        "--base-url", default="", help="override model base URL (OpenAI compatible)"
    )
    parser.add_argument("--model", default="", help="override model name")
    parser.add_argument("--api-key", default="", help="override API key")
    parser.add_argument("--output-dir", default=str(DEFAULT_DIR))
    parser.add_argument(
        "--label", choices=LABELS, action="append", help="only generate these labels"
    )
    parser.add_argument(
        "--explicit-mode",
        choices=("mix", "direct", "consultation"),
        default="mix",
        help="explicit samples: direct intent, NSFW consultation, or both",
    )
    parser.add_argument(
        "--consultation-ratio",
        type=float,
        default=0.3,
        help="consultation share when --explicit-mode=mix",
    )
    parser.add_argument(
        "--consultation-topic",
        choices=CONSULTATION_TOPICS,
        default="",
        help="force one consultation topic for balanced generation",
    )
    parser.add_argument("--batches-per-label", type=int, default=150)
    parser.add_argument("--messages-per-batch", type=int, default=10)
    parser.add_argument(
        "--limit", type=int, default=0, help="stop after this many model calls (smoke test)"
    )
    parser.add_argument("--temperature", type=float, default=0.95)
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--sleep", type=float, default=0.0, help="pause between model calls")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--json-mode", dest="json_mode", action="store_true", default=True)
    parser.add_argument("--no-json-mode", dest="json_mode", action="store_false")
    parser.add_argument("--overwrite", action="store_true", help="ignore existing raw files")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.batches_per_label < 0 or not 1 <= args.messages_per_batch <= 50:
        parser.error("batches-per-label must be >= 0 and messages-per-batch must be in 1..50")
    if args.limit < 0:
        parser.error("limit must be >= 0")
    if not 0 <= args.consultation_ratio <= 1:
        parser.error("consultation-ratio must be between 0 and 1")
    return args


def explicit_subtype(args: argparse.Namespace, rng: random.Random) -> str | None:
    if args.explicit_mode == "consultation":
        return "consultation"
    if args.explicit_mode == "direct":
        return "direct"
    return "consultation" if rng.random() < args.consultation_ratio else "direct"


def resolve_endpoint(args: argparse.Namespace) -> tuple[str, str, str, float]:
    if args.base_url and args.model:
        return args.base_url.rstrip("/"), args.model, args.api_key, args.timeout
    settings = Settings.load(args.config)
    base_url = (args.base_url or settings.model_base_url).rstrip("/")
    model = args.model or settings.model_name
    api_key = args.api_key or settings.model_api_key
    timeout = args.timeout or settings.model_timeout_seconds
    if not base_url or not model:
        raise SystemExit(
            "No model configured. Fill [model] in virtual-life.toml or pass --base-url and --model."
        )
    return base_url, model, api_key, timeout


def chat_completion(
    client: httpx.Client,
    *,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    retries: int,
) -> str:
    url = f"{base_url}/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code == 400 and "response_format" in payload:
                # llama.cpp / older OpenAI-compatible servers may reject JSON mode.
                payload.pop("response_format", None)
                response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("model returned an empty message")
            return content
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(min(20.0, 2.0 ** (attempt - 1)))
    raise RuntimeError(f"model call failed after {retries} attempts: {last_error}")


def append_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            stream.write("\n")
        stream.flush()
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    raw_dir = output_dir / "raw"
    selected = tuple(args.label or LABELS)
    rng = random.Random(args.seed)

    if args.dry_run:
        for label in selected:
            subtype = explicit_subtype(args, rng) if label == "explicit" else None
            prompt = build_prompt(
                label,
                rng,
                count=args.messages_per_batch,
                persona="虚拟陪伴角色",
                subtype=subtype,
                consult_topic=args.consultation_topic or None,
            )
            print(f"=== {label} ===")
            print(prompt[1]["content"])
        return 0

    base_url, model, api_key, timeout = resolve_endpoint(args)
    stats: Counter[str] = Counter()
    existing: dict[str, set[str]] = {label: set() for label in selected}
    if args.overwrite:
        for label in selected:
            (raw_dir / f"{label}.jsonl").unlink(missing_ok=True)
    else:
        for label in selected:
            path = raw_dir / f"{label}.jsonl"
            if not path.exists():
                continue
            try:
                for record in read_jsonl(path):
                    text = record.get("text")
                    if isinstance(text, str):
                        existing[label].add(normalize_text(text).casefold())
            except ValueError as exc:
                print(f"warning: cannot resume from {path}: {exc}", file=sys.stderr)

    calls = 0
    with httpx.Client(timeout=timeout) as client:
        for label in selected:
            target = args.batches_per_label * args.messages_per_batch
            already = len(existing[label])
            if already >= target:
                print(f"{label}: already has {already} samples, skipping")
                continue
            batches = (target - already + args.messages_per_batch - 1) // args.messages_per_batch
            for batch_index in range(batches):
                if args.limit and calls >= args.limit:
                    break
                prompt = build_prompt(
                    label,
                    rng,
                    count=args.messages_per_batch,
                    persona="虚拟陪伴角色",
                    subtype=explicit_subtype(args, rng) if label == "explicit" else None,
                    consult_topic=args.consultation_topic or None,
                )
                raw = chat_completion(
                    client,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    messages=prompt,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    json_mode=args.json_mode,
                    retries=args.retries,
                )
                calls += 1
                messages = parse_messages(raw, max_messages=args.messages_per_batch * 2)
                records: list[dict] = []
                for message in messages:
                    text = normalize_text(message)
                    stats["parsed"] += 1
                    if not is_usable(text):
                        stats["length_rejected"] += 1
                        continue
                    if contains_minor_content(text):
                        stats["minor_content_rejected"] += 1
                        continue
                    key = text.casefold()
                    if key in existing[label]:
                        stats["duplicate"] += 1
                        continue
                    existing[label].add(key)
                    records.append(
                        {
                            "text": text,
                            "label": label,
                            "source": f"model:{model}",
                            "batch_seed": args.seed + calls,
                        }
                    )
                    stats["kept"] += 1
                if records:
                    append_jsonl(raw_dir / f"{label}.jsonl", records)
                print(
                    f"{label} batch {batch_index + 1}/{batches}: "
                    f"+{len(records)} kept, {len(existing[label])}/{target} total"
                )
                if args.sleep:
                    time.sleep(args.sleep)
            if args.limit and calls >= args.limit:
                break

    combined: list[dict] = []
    for label in LABELS:
        path = raw_dir / f"{label}.jsonl"
        if not path.exists():
            continue
        for record in read_jsonl(path):
            # A crash during append can duplicate the last line; write_jsonl
            # keeps the combined file deterministic but does not deduplicate.
            combined.append(record)
    destination = output_dir / "train_raw.jsonl"
    write_jsonl(destination, combined)
    print(f"\nWrote {len(combined)} raw samples to {destination}")
    print(f"Stats: {dict(stats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
