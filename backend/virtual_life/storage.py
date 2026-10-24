"""SQLite storage boundary with one connection per durable transaction."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS nodes (
        node_id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        kind TEXT NOT NULL,
        capabilities_json TEXT NOT NULL,
        context_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS companions (
        companion_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        active_node_id TEXT REFERENCES nodes(node_id),
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        lease_expires_at TEXT,
        activity_json TEXT NOT NULL,
        interaction_mode TEXT NOT NULL,
        context_json TEXT NOT NULL,
        memory_refs_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS transfers (
        transfer_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL REFERENCES companions(companion_id),
        source_node_id TEXT REFERENCES nodes(node_id),
        target_node_id TEXT NOT NULL REFERENCES nodes(node_id),
        status TEXT NOT NULL CHECK (status IN ('prepared','committed','aborted','expired')),
        source_released INTEGER NOT NULL CHECK (source_released IN (0,1)),
        target_ready INTEGER NOT NULL CHECK (target_ready IN (0,1)),
        base_revision INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        committed_revision INTEGER,
        target_presentation_id TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_prepared_transfer_per_companion
        ON transfers(companion_id) WHERE status = 'prepared'""",
    """CREATE INDEX IF NOT EXISTS pending_transfers
        ON transfers(status, expires_at)""",
    """CREATE TABLE IF NOT EXISTS runtime_commands (
        command_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        operation TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        protocol_version TEXT NOT NULL,
        type TEXT NOT NULL,
        companion_id TEXT,
        node_id TEXT,
        recipient_node_id TEXT,
        payload_json TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    )""",
)


class SQLiteStore:
    """Single-runtime storage; independent callers share SQLite's write lock."""

    def __init__(self, database_path: str | Path) -> None:
        if str(database_path) == ":memory:" or not str(database_path).strip():
            raise ValueError("Use a file-backed database for durable runtime state")
        self.database_path = str(Path(database_path).expanduser().resolve())
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()
        with self.transaction(write=True) as connection:
            for statement in SCHEMA:
                connection.execute(statement)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
