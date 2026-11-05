"""Backfill the vocabulary bank from historical dialogue events.

The runtime's VocabularyBank (MaiBot jargon_miner port) only counts user
messages observed after ``dialogue.vocabulary_enabled`` was turned on. This
script replays the *history* through the same counting logic so the bank
reflects the whole relationship, not just recent turns.

Source of truth: ``events`` rows of type ``companion.dialogue_completed``
whose ``payload_json`` carries the original ``user_message`` (the same
projection the dialogue history API uses). Each companion's messages are
replayed oldest-first through ``VocabularyBank.observe`` inside one write
transaction, exactly like live dialogue does, so counts merge into any
existing bank rather than replacing it.

Safety: the script never targets the live database by accident — pass
``--database`` explicitly (default ``.data/runtime.sqlite3`` is printed in
dry-run mode only). ``--dry-run`` opens the database read-only, reports the
companions found, message counts, and the top candidate words per companion,
and writes nothing.

Example::

    uv run python scripts/backfill_vocabulary.py --dry-run
    uv run python scripts/backfill_vocabulary.py --database .data/runtime.sqlite3
    uv run python scripts/backfill_vocabulary.py --database copy.sqlite3 --companion proact-demo
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Make the backend package importable when the script is run from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from virtual_life.storage import SQLiteStore  # noqa: E402
from virtual_life.vocabulary import VocabularyBank, _frequent_groups  # noqa: E402

DEFAULT_DATABASE = ".data/runtime.sqlite3"
COMPLETED_EVENT = "companion.dialogue_completed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay historical user messages into the vocabulary bank."
    )
    parser.add_argument("--database", "--db", default=DEFAULT_DATABASE, help="runtime SQLite path")
    parser.add_argument("--companion", default="", help="only backfill this companion_id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be backfilled without writing anything",
    )
    return parser.parse_args()


def fetch_messages(
    connection: sqlite3.Connection, companion_id: str
) -> dict[str, list[tuple[str, str]]]:
    """Per-companion (event_id, user_message) pairs, oldest first.

    Only completed turns are replayed: silent (debounce-suppressed) and
    discarded turns never produced a reply, and their absorbed texts stay
    inside the surviving batch entry.
    """
    query = (
        "SELECT event_id, companion_id, payload_json FROM events "
        "WHERE type = ? ORDER BY sequence ASC"
    )
    rows = connection.execute(query, (COMPLETED_EVENT,)).fetchall()
    messages: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        owner = row["companion_id"] or ""
        if companion_id and owner != companion_id:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            continue
        text = payload.get("user_message") or ""
        if text.strip():
            messages.setdefault(owner, []).append((row["event_id"], text))
    return messages


def main() -> int:
    args = parse_args()
    database = Path(args.database)
    if not database.exists():
        print(f"database not found: {database}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Read-only: never construct SQLiteStore (it opens a write transaction
        # to ensure the schema), so a dry run cannot touch the file at all.
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            messages = fetch_messages(connection, args.companion)
        finally:
            connection.close()
        if not messages:
            print(
                "no completed dialogue user messages found"
                + (f" for companion {args.companion}" if args.companion else "")
            )
            return 0
        for owner, entries in sorted(messages.items()):
            preview = Counter()
            for _, text in entries:
                preview.update(_frequent_groups(text[:4096]))
            top = ", ".join(f"{word}×{count}" for word, count in preview.most_common(8))
            print(f"{owner}: {len(entries)} messages, top candidates: {top}")
        print("dry run: nothing written")
        return 0

    store = SQLiteStore(database)
    bank = VocabularyBank(store, enabled=True)
    connection = store.connect()
    try:
        messages = fetch_messages(connection, args.companion)
    finally:
        connection.close()
    if not messages:
        print("no completed dialogue user messages found")
        return 0
    total_words = 0
    with store.transaction(write=True) as connection:
        for owner, entries in sorted(messages.items()):
            touched = 0
            for _, text in entries:
                touched += bank.observe(connection, owner, text)
            print(f"{owner}: replayed {len(entries)} messages, touched {touched} words")
            total_words += touched
    print(f"backfill complete: {total_words} word updates across {len(messages)} companions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
