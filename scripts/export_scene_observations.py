"""Export recorded scene-classifier observations for manual review.

The runtime stores each observation inside the durable dialogue receipt
(``dialogue_commands.result_json``) and the matching user text in the
``companion.dialogue_received`` event.  This script joins the two tables and
writes one JSONL row per observed turn in the training-data shape used by
``train.jsonl`` (``text`` + ``label`` first, plus review metadata), so a
hand-corrected file can be fed straight back into
``scripts/train_nsfw_classifier.py``.

The database is opened read-only and never modified.

Example::

    uv run python scripts/export_scene_observations.py \
        --output .data/nsfw_classifier/observe_review.jsonl --sensitive-only
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DEFAULT_DATABASE = ".data/runtime.sqlite3"
DEFAULT_OUTPUT = ".data/nsfw_classifier/observe_review.jsonl"
RECEIVED_EVENT = "companion.dialogue_received"
LABELS = ("daily", "emotional", "intimate", "explicit")
SENSITIVE_LABELS = ("emotional", "intimate", "explicit")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export observed scene labels with their user text for manual correction."
    )
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="runtime SQLite path")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="JSONL output path")
    parser.add_argument("--since", default="", help="only receipts created_at >= this ISO time")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="skip observations below this label confidence",
    )
    parser.add_argument(
        "--labels",
        default="",
        help=f"comma separated label filter, one of {','.join(LABELS)}",
    )
    parser.add_argument(
        "--sensitive-only",
        action="store_true",
        help="keep only emotional/intimate/explicit observations",
    )
    parser.add_argument(
        "--dedupe",
        action="store_true",
        help="keep the first observation per unique user text",
    )
    return parser.parse_args()


def user_messages(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT json_extract(payload_json, '$.command_id') AS command_id,
               MIN(json_extract(payload_json, '$.user_message')) AS user_message
        FROM events
        WHERE type = ?
        GROUP BY command_id
        """,
        (RECEIVED_EVENT,),
    )
    return {
        command_id: message
        for command_id, message in rows
        if isinstance(command_id, str) and isinstance(message, str)
    }


def select_labels(args: argparse.Namespace) -> set[str] | None:
    if args.sensitive_only:
        return set(SENSITIVE_LABELS)
    if not args.labels:
        return None
    selected = {label.strip() for label in args.labels.split(",") if label.strip()}
    unknown = selected - set(LABELS)
    if unknown:
        raise SystemExit(f"unknown label filter: {sorted(unknown)}")
    return selected or None


def collect(args: argparse.Namespace) -> tuple[list[dict], int, int]:
    """Return (rows, skipped_degraded, missing_text) for the configured filters."""

    database = Path(args.database)
    rows_out: list[dict] = []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        messages = user_messages(connection)
        wanted = select_labels(args)
        seen_texts: set[str] = set()
        skipped = missing_text = 0
        for command_id, companion_id, created_at, result_json in connection.execute(
            "SELECT command_id, companion_id, created_at, result_json FROM dialogue_commands"
        ):
            if not result_json:
                continue
            if args.since and str(created_at) < args.since:
                continue
            try:
                result = json.loads(result_json)
            except json.JSONDecodeError:
                continue
            observation = result.get("scene") if isinstance(result, dict) else None
            if not isinstance(observation, dict) or not observation:
                continue
            text = messages.get(str(command_id), "")
            if not text:
                missing_text += 1
                continue
            label = observation.get("label")
            confidence = observation.get("confidence") or 0.0
            if not isinstance(label, str) or label not in LABELS:
                # Degraded observation: the classifier produced no usable label.
                skipped += 1
                continue
            if wanted is not None and label not in wanted:
                continue
            if confidence < args.min_confidence:
                continue
            if args.dedupe and text in seen_texts:
                continue
            seen_texts.add(text)
            row = {
                "text": text,
                "label": label,
                "confidence": round(float(confidence), 6),
                "scores": observation.get("scores"),
                "source": "runtime:scene_classifier",
                "command_id": str(command_id),
                "companion_id": str(companion_id),
                "created_at": str(created_at),
                "classifier_version": observation.get("classifier_version"),
                "routed_model": observation.get("routed_model"),
                "error_code": observation.get("error_code"),
            }
            rows_out.append(row)
    return rows_out, skipped, missing_text


def main() -> int:
    args = parse_args()
    database = Path(args.database)
    if not database.exists():
        raise SystemExit(f"database not found: {database}")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows, skipped, missing_text = collect(args)
    with output.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        f"wrote {len(rows)} observation(s) to {output}; "
        f"skipped {skipped} degraded; {missing_text} without user text"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
