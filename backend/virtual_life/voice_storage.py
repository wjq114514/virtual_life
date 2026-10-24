"""Durable schema and small helpers for the voice subsystem.

The tables are deliberately separate from ``runtime_commands`` and
``dialogue_commands``. A voice turn's ``command_id`` doubles as the dialogue
command id, so ``claim_command`` refuses an id that already exists in another
command table with a different owner.
"""

import json
from datetime import UTC, datetime

from .coordinator import DomainError

VOICE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS voice_sessions (
        session_id TEXT PRIMARY KEY,
        command_id TEXT NOT NULL,
        companion_id TEXT NOT NULL REFERENCES companions(companion_id),
        node_id TEXT NOT NULL REFERENCES nodes(node_id),
        presentation_id TEXT NOT NULL,
        mode TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        session_generation INTEGER NOT NULL,
        output_generation INTEGER NOT NULL,
        asr_json TEXT,
        voice_id TEXT NOT NULL,
        audio_enabled INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        idle_deadline_at TEXT NOT NULL,
        absolute_expires_at TEXT NOT NULL,
        idle_timeout_seconds REAL NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS voice_session_active
        ON voice_sessions(companion_id, status, expires_at)""",
    """CREATE TABLE IF NOT EXISTS voice_output_fences (
        companion_id TEXT PRIMARY KEY,
        output_generation INTEGER NOT NULL,
        active_turn_id TEXT,
        invalidated_reason TEXT,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS voice_turns (
        turn_id TEXT PRIMARY KEY,
        command_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        companion_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        session_generation INTEGER NOT NULL,
        output_generation INTEGER NOT NULL,
        text TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        input_json TEXT NOT NULL,
        end_reason TEXT NOT NULL,
        continuation_of_turn_id TEXT,
        screen_reference_json TEXT,
        status TEXT NOT NULL,
        discard_reason TEXT,
        assistant_message TEXT,
        model TEXT,
        usage_json TEXT,
        error_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT)""",
    """CREATE INDEX IF NOT EXISTS voice_turn_session
        ON voice_turns(session_id, created_at)""",
    """CREATE TABLE IF NOT EXISTS voice_jobs (
        turn_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        state TEXT NOT NULL,
        owner TEXT,
        claim_generation INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS speech_segments (
        turn_id TEXT NOT NULL,
        segment_sequence INTEGER NOT NULL,
        text TEXT NOT NULL,
        spoken_text TEXT NOT NULL,
        char_start INTEGER NOT NULL,
        char_end INTEGER NOT NULL,
        media_type TEXT NOT NULL,
        sample_rate INTEGER NOT NULL,
        channels INTEGER NOT NULL,
        sample_count INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        audio_path TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (turn_id, segment_sequence))""",
    """CREATE TABLE IF NOT EXISTS voice_playback (
        turn_id TEXT NOT NULL,
        segment_sequence INTEGER NOT NULL,
        output_generation INTEGER NOT NULL,
        played_samples INTEGER NOT NULL,
        status TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (turn_id, segment_sequence))""",
    """CREATE TABLE IF NOT EXISTS speech_usage (
        turn_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        chars INTEGER NOT NULL,
        sample_seconds REAL NOT NULL,
        compute_ms REAL NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS voice_turn_events (
        turn_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (turn_id, sequence))""",
    """CREATE TABLE IF NOT EXISTS voice_commands (
        command_id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        result_json TEXT,
        created_at TEXT NOT NULL)""",
)

OWNER_TABLES = ("runtime_commands", "dialogue_commands")


def claim_command(connection, command_id: str, operation: str, fingerprint: str) -> str | None:
    """Reserve ``command_id`` for a voice operation.

    Returns the stored result JSON when this exact operation was already
    accepted (idempotent replay). Raises on a cross-table or fingerprint clash.
    """
    existing = connection.execute(
        "SELECT operation, fingerprint, result_json FROM voice_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    if existing is not None:
        if existing["operation"] != operation or existing["fingerprint"] != fingerprint:
            raise DomainError(
                409, "idempotency_conflict", "Command ID was used with another request"
            )
        return existing["result_json"]
    for table in OWNER_TABLES:
        row = connection.execute(
            f"SELECT fingerprint FROM {table} WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is not None:
            raise DomainError(
                409, "idempotency_conflict", "Command ID is already used by another operation"
            )
    connection.execute(
        "INSERT INTO voice_commands(command_id, operation, fingerprint, result_json, created_at) "
        "VALUES (?, ?, ?, NULL, ?)",
        (command_id, operation, fingerprint, datetime.now(UTC).isoformat()),
    )
    return None


def store_command_result(connection, command_id: str, result: dict) -> None:
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    connection.execute(
        "UPDATE voice_commands SET result_json=? WHERE command_id=?",
        (encoded, command_id),
    )
    # Reserve in the runtime's shared namespace too. Other mutations must reject
    # a voice command ID even when they do not know about the voice tables.
    row = connection.execute(
        "SELECT * FROM voice_commands WHERE command_id=?", (command_id,)
    ).fetchone()
    connection.execute(
        "INSERT INTO runtime_commands(command_id, fingerprint, operation, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (command_id, row["fingerprint"], row["operation"], encoded, row["created_at"]),
    )


def migrate_voice(connection) -> None:
    """Add fields to databases created by the initial, unfinished voice branch."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(voice_turns)")}
    for name, definition in {
        "expected_revision": "INTEGER NOT NULL DEFAULT 0",
        "preview_json": "TEXT",
        "dialogue_result_json": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE voice_turns ADD COLUMN {name} {definition}")
