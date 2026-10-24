"""Local SQLite state: identity, event cursor, command recovery and dedup."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sequence INTEGER NOT NULL,
    generation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dialogue_commands (
    command_id TEXT PRIMARY KEY,
    companion_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dedup (
    key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class DialogueCommandRow:
    command_id: str
    companion_id: str
    fingerprint: str
    status: str
    result_json: str | None
    created_at: str
    updated_at: str


class ClientStorage:
    """Thread-safe wrapper around the per-node SQLite state file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ClientStorage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- identity ---------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def ensure_identity(self, node_id: str, device_id: str) -> tuple[str, str]:
        """Persist the first node/device ids and return the durable pair."""
        stored_node = self.get("node_id")
        stored_device = self.get("device_id")
        if stored_node is None:
            self.set("node_id", node_id)
            stored_node = node_id
        if stored_device is None:
            self.set("device_id", device_id)
            stored_device = device_id
        return stored_node, stored_device

    # -- event cursor -----------------------------------------------------
    def get_cursor(self) -> tuple[int, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT sequence, generation FROM event_cursor WHERE id = 1"
            ).fetchone()
        if row is None:
            return 0, ""
        return int(row["sequence"]), str(row["generation"])

    def set_cursor(self, sequence: int, generation: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO event_cursor(id, sequence, generation) VALUES(1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET sequence = excluded.sequence, "
                "generation = excluded.generation",
                (int(sequence), generation),
            )
            self._conn.commit()

    # -- command recovery -------------------------------------------------
    def record_command(
        self, command_id: str, companion_id: str, fingerprint: str, created_at: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dialogue_commands"
                "(command_id, companion_id, fingerprint, status, result_json, created_at, updated_at) "
                "VALUES(?, ?, ?, 'pending', NULL, ?, ?) "
                "ON CONFLICT(command_id) DO NOTHING",
                (command_id, companion_id, fingerprint, created_at, created_at),
            )
            self._conn.commit()

    def get_command(self, command_id: str) -> DialogueCommandRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM dialogue_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        return self._row(row)

    def finish_command(
        self, command_id: str, status: str, result_json: str | None, now: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE dialogue_commands SET status = ?, result_json = ?, updated_at = ? "
                "WHERE command_id = ?",
                (status, result_json, now, command_id),
            )
            self._conn.commit()

    def pending_commands(self, companion_id: str | None = None) -> list[DialogueCommandRow]:
        query = "SELECT * FROM dialogue_commands WHERE status = 'pending'"
        args: list = []
        if companion_id is not None:
            query += " AND companion_id = ?"
            args.append(companion_id)
        query += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        result = [self._row(row) for row in rows]
        return [row for row in result if row is not None]

    def save_command_result(self, command_id: str, result: dict, now: str) -> None:
        self.finish_command(command_id, "settled", json.dumps(result, ensure_ascii=False), now)

    # -- dedup ------------------------------------------------------------
    def mark(self, key: str, created_at: str) -> bool:
        """Return True when the key was newly marked, False when already present."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO dedup(key, created_at) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, created_at),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def seen(self, key: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM dedup WHERE key = ?", (key,)).fetchone()
        return row is not None

    @staticmethod
    def _row(row: sqlite3.Row | None) -> DialogueCommandRow | None:
        if row is None:
            return None
        return DialogueCommandRow(
            command_id=row["command_id"],
            companion_id=row["companion_id"],
            fingerprint=row["fingerprint"],
            status=row["status"],
            result_json=row["result_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
