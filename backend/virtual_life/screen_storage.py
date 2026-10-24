"""Dedicated SQLite tables for screen-vision sessions, jobs and results.

The runtime keeps a single writer; this module only owns the new tables and the
migration needed to create or extend them re-entrantly. Decoding, OCR and model
calls always happen outside these transactions.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .storage import SQLiteStore

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS screen_sessions (
        session_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL,
        observer_node_id TEXT NOT NULL,
        subject_node_id TEXT NOT NULL,
        display_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        mode TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','stopped','superseded','expired','purged')),
        capture_source TEXT NOT NULL,
        requested_profile TEXT NOT NULL,
        effective_profile TEXT NOT NULL,
        user_initiated INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_sequence INTEGER,
        latest_frame_id TEXT,
        latest_analysis_id TEXT,
        purge_status TEXT NOT NULL DEFAULT 'none',
        purge_requested_at TEXT,
        purge_completed_at TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS screen_one_active_stream
        ON screen_sessions(companion_id, observer_node_id, display_id)
        WHERE status = 'active'""",
    """CREATE INDEX IF NOT EXISTS screen_active_sessions
        ON screen_sessions(status, expires_at)""",
    """CREATE TABLE IF NOT EXISTS screen_uploads (
        upload_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        command_id TEXT NOT NULL,
        media_type TEXT NOT NULL,
        declared_byte_length INTEGER NOT NULL,
        declared_sha256 TEXT NOT NULL,
        declared_width INTEGER NOT NULL,
        declared_height INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','writing','ready')),
        writer_token TEXT,
        path TEXT,
        actual_sha256 TEXT,
        actual_byte_length INTEGER,
        actual_width INTEGER,
        actual_height INTEGER,
        pixel_sha256 TEXT,
        normalization_version TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        upload_expires_at TEXT NOT NULL,
        referenced_at TEXT,
        retain_until TEXT,
        frame_id TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS screen_uploads_session
        ON screen_uploads(session_id, created_at)""",
    """CREATE TABLE IF NOT EXISTS screen_frames (
        frame_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        sequence INTEGER NOT NULL,
        kind TEXT NOT NULL,
        command_id TEXT NOT NULL,
        sampled_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        ttl_seconds INTEGER NOT NULL,
        trigger TEXT NOT NULL,
        upload_id TEXT,
        reference_frame_id TEXT,
        client_features_json TEXT,
        host_metadata_json TEXT,
        redacted_regions_json TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (session_id, sequence)
    )""",
    """CREATE INDEX IF NOT EXISTS screen_frames_stream
        ON screen_frames(session_id, generation, sequence)""",
    """CREATE TABLE IF NOT EXISTS screen_jobs (
        job_id TEXT PRIMARY KEY,
        frame_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        companion_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        sequence INTEGER NOT NULL,
        priority TEXT NOT NULL,
        state TEXT NOT NULL,
        worker_owner TEXT,
        claim_generation INTEGER NOT NULL DEFAULT 0,
        claim_expires_at TEXT,
        dispatch_state TEXT NOT NULL DEFAULT 'pending',
        task_id TEXT,
        analysis_id TEXT,
        reuse_from_frame_id TEXT,
        error_code TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        processing_stage TEXT,
        usage_json TEXT,
        stages_json TEXT,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS screen_jobs_claimable
        ON screen_jobs(state, priority, queued_at)""",
    """CREATE INDEX IF NOT EXISTS screen_jobs_session
        ON screen_jobs(session_id, sequence)""",
    """CREATE TABLE IF NOT EXISTS screen_analyses (
        analysis_id TEXT PRIMARY KEY,
        frame_id TEXT NOT NULL,
        root_analysis_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        companion_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        display_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        sequence INTEGER NOT NULL,
        schema_version TEXT NOT NULL,
        status TEXT NOT NULL,
        quality TEXT NOT NULL,
        analysis_path TEXT NOT NULL,
        scene_summary TEXT NOT NULL DEFAULT '',
        activity TEXT NOT NULL DEFAULT 'unknown',
        activity_score REAL,
        payload_json TEXT NOT NULL,
        pixel_sha256 TEXT,
        scope_fingerprint TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        deleted_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS screen_analysis_reuse
        ON screen_analyses(scope_fingerprint, created_at)""",
    """CREATE INDEX IF NOT EXISTS screen_analysis_stream
        ON screen_analyses(session_id, generation, sequence)""",
    """CREATE TABLE IF NOT EXISTS screen_states (
        companion_id TEXT NOT NULL,
        observer_node_id TEXT NOT NULL,
        display_id TEXT NOT NULL,
        latest_observation_frame_id TEXT,
        latest_analysis_id TEXT,
        latest_analysis_frame_id TEXT,
        observable INTEGER NOT NULL DEFAULT 0,
        barrier_sequence INTEGER,
        pending_newer_frame INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'unknown',
        evidence_sampled_at TEXT,
        evidence_expires_at TEXT,
        version INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (companion_id, observer_node_id, display_id)
    )""",
    """CREATE TABLE IF NOT EXISTS screen_content_tombstones (
        session_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL,
        status TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        completed_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS screen_vision_usage (
        day TEXT PRIMARY KEY,
        reserved_calls INTEGER NOT NULL DEFAULT 0,
        settled_calls INTEGER NOT NULL DEFAULT 0,
        released_calls INTEGER NOT NULL DEFAULT 0,
        reserved_tokens INTEGER NOT NULL DEFAULT 0,
        settled_tokens INTEGER NOT NULL DEFAULT 0
    )""",
)


class ScreenStore:
    """Schema owner for the screen-vision tables, sharing the runtime database."""

    def __init__(self, database_path: str | Path) -> None:
        self.sqlite = SQLiteStore(database_path)
        with self.sqlite.transaction(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator:
        with self.sqlite.transaction(write=write) as connection:
            yield connection

    def connect(self):
        return self.sqlite.connect()

    @staticmethod
    def row(connection, table: str, key: str, value):
        row = connection.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,)).fetchone()
        return row
