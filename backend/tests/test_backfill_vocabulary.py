"""Backfill script verification on a temporary database.

Runs ``scripts/backfill_vocabulary.py`` as a subprocess against a temp store
seeded with ``companion.dialogue_completed`` events and checks that the
vocabulary bank is populated exactly like live observation would.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from virtual_life.storage import SQLiteStore
from virtual_life.vocabulary import VocabularyBank

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "backfill_vocabulary.py"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _seed(store: SQLiteStore) -> None:
    rows = [
        ("evt-1", "c-alpha", {"user_message": "今晚吃火锅吗，想吃辣的那种火锅"}),
        ("evt-2", "c-alpha", {"user_message": "火锅记得加毛肚，上次火锅没加"}),
        ("evt-3", "c-beta", {"user_message": "帮我看看 nginx config 的 ssl 部分"}),
        # silent/discarded shapes must be ignored (wrong type, empty text).
        ("evt-4", "c-beta", {"user_message": ""}),
    ]
    with store.transaction(write=True) as connection:
        for event_id, companion_id, payload in rows:
            connection.execute(
                "INSERT INTO events (event_id, protocol_version, type, companion_id, "
                "node_id, recipient_node_id, payload_json, occurred_at) "
                "VALUES (?, ?, 'companion.dialogue_completed', ?, NULL, NULL, ?, ?)",
                (event_id, "1", companion_id, json.dumps(payload), _now()),
            )
        connection.execute(
            "INSERT INTO events (event_id, protocol_version, type, companion_id, "
            "node_id, recipient_node_id, payload_json, occurred_at) "
            "VALUES ('evt-5', '1', 'companion.dialogue_silent', 'c-alpha', NULL, NULL, "
            "?, ?)",
            (json.dumps({"user_message": "被防抖吸收的消息"}), _now()),
        )


def _run(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


def test_backfill_populates_vocabulary_from_history(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    store = SQLiteStore(db_path)
    _seed(store)

    result = _run(tmp_path, "--database", str(db_path))
    assert result.returncode == 0, result.stderr
    assert "replayed 2 messages" in result.stdout
    assert "backfill complete" in result.stdout

    bank = VocabularyBank(SQLiteStore(db_path), enabled=True)
    connection = bank.store.connect()
    try:
        rows = connection.execute(
            "SELECT companion_id, word, count FROM vocabulary ORDER BY word"
        ).fetchall()
    finally:
        connection.close()
    words = {(row["companion_id"], row["word"]) for row in rows}
    # 火锅 appears 5 times across the two alpha messages (2/3-gram counting),
    # so it must be in the bank; the silent event's absorbed text must not be.
    assert ("c-alpha", "火锅") in words
    counts = {row["word"]: row["count"] for row in rows if row["companion_id"] == "c-alpha"}
    assert counts["火锅"] == 4  # 2-gram occurrences across the two messages
    assert not any(row["companion_id"] == "c-alpha" and "吸收" in row["word"] for row in rows)
    assert any(row["companion_id"] == "c-beta" for row in rows)


def test_backfill_dry_run_writes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    store = SQLiteStore(db_path)
    _seed(store)

    result = _run(tmp_path, "--database", str(db_path), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry run: nothing written" in result.stdout
    assert "c-alpha: 2 messages" in result.stdout
    assert "火锅" in result.stdout

    connection = store.connect()
    try:
        exists = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='vocabulary'"
        ).fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM vocabulary").fetchone()[0] if exists else 0
    finally:
        connection.close()
    assert count == 0
