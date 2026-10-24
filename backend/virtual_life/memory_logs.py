"""Bounded diagnostic records independent of memory content and provenance."""

import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

OPERATIONS = frozenset(
    {
        "write",
        "update_metadata",
        "correct",
        "delete",
        "recall",
        "reinforcement",
        "automation",
        "embedding_index",
        "embedding_query",
        "graph",
        "dialogue",
    }
)
STATUSES = frozenset(
    {
        "completed",
        "failed",
        "skipped",
        "blocked",
        "retrying",
        "unknown",
        "cancelled",
        "cached",
        "needs_review",
        "discarded",
        "local_only",
        "deferred",
        "silent",
        "refused",
    }
)
_KEYS = frozenset(
    {
        "query",
        "strategy",
        "candidate_count",
        "selected",
        "memory_id",
        "revision",
        "retention_policy",
        "effective_retention_policy",
        "decay_factor",
        "effective_importance",
        "importance",
        "omitted_count",
        "truncated",
        "token_budget",
        "estimated_tokens",
        "max_items",
        "usage",
        "input_tokens",
        "output_tokens",
        "error_code",
        "source_count",
        "review_ids",
        "memory_versions",
        "source_memory_versions",
        "source_event_ids",
        "model_calls",
        "entity_id",
        "entity_versions",
        "relation_count",
        "relations_skipped",
        "relation_index",
        "reason",
        "entity_count",
        "hops",
        "action",
        "reinforced",
        "exposed",
        "semantic_cache_hit",
        "semantic_candidates",
        "details_truncated",
        "model",
        "prompt_version",
        "status",
        "deleted",
        "alias_count",
        "streaming",
        "timings_ms",
        "claim_ms",
        "preview_ms",
        "recall_ms",
        "resonance_ms",
        "screen_ms",
        "reserve_ms",
        "dispatch_gate_ms",
        "first_text_ms",
        "provider_ms",
        "commit_ms",
        "total_ms",
    }
)
_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[a-z0-9_-]+|(?:api[_ -]?key|token|password|secret|authorization)\s*[:=]\s*\S+)"
)
_LOGGER = logging.getLogger("virtual_life.memory")


_CORRELATION = ContextVar("memory_log_correlation", default=(None, None))


def _safe(value, depth=0):
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {key: _safe(item, depth + 1) for key, item in value.items() if key in _KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _SECRET.sub("[redacted]", value[:512])
    if value is None or type(value) in (bool, int, float):
        return value
    return None


class MemoryLogService:
    def __init__(self, database_path, clock):
        self.database_path = database_path
        self.clock = clock
        with sqlite3.connect(database_path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS memory_logs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id TEXT NOT NULL UNIQUE, companion_id TEXT NOT NULL,
                operation TEXT NOT NULL, status TEXT NOT NULL,
                command_id TEXT, job_id TEXT, created_at TEXT NOT NULL,
                duration_ms REAL, details_json TEXT NOT NULL
            )""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS memory_logs_scope ON memory_logs(companion_id, sequence)"
            )

    @contextmanager
    def correlate(self, *, command_id=None, job_id=None):
        previous = _CORRELATION.get()
        token = _CORRELATION.set((command_id or previous[0], job_id or previous[1]))
        try:
            yield
        finally:
            _CORRELATION.reset(token)

    def record(
        self,
        companion_id,
        operation,
        status="completed",
        *,
        command_id=None,
        job_id=None,
        duration_ms=None,
        details=None,
        connection=None,
    ):
        if operation not in OPERATIONS or status not in STATUSES:
            raise ValueError("Unknown memory log operation or status")
        inherited_command, inherited_job = _CORRELATION.get()
        command_id = command_id or inherited_command
        job_id = job_id or inherited_job
        safe = _safe(details or {})
        encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 16384:
            safe = {
                "details_truncated": True,
                **{
                    key: value for key, value in safe.items() if not isinstance(value, (list, dict))
                },
            }
            encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False)
        while len(encoded.encode("utf-8")) > 16384:
            safe.pop(next(key for key in reversed(safe) if key != "details_truncated"))
            encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False)
        value = self.clock()
        now = value if isinstance(value, datetime) else datetime.fromtimestamp(value, UTC)
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        values = (
            "mlog_" + uuid.uuid4().hex,
            companion_id,
            operation,
            status,
            command_id,
            job_id,
            now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            duration_ms,
            encoded,
        )
        sql = "INSERT INTO memory_logs(log_id, companion_id, operation, status, command_id, job_id, created_at, duration_ms, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        if connection is not None:
            connection.execute(sql, values)
        else:
            with sqlite3.connect(self.database_path, timeout=1) as owned:
                owned.execute(sql, values)
        # The message carries safe metadata because standard console formatters
        # commonly discard LogRecord extras. Mutating records are still pending
        # their caller's commit when this line is emitted.
        _LOGGER.info(
            json.dumps(
                {
                    "type": "memory.operation",
                    "log_id": values[0],
                    "companion_id": _safe(companion_id),
                    "operation": operation,
                    "status": status,
                    "command_id": _safe(command_id),
                    "job_id": _safe(job_id),
                    "created_at": values[6],
                    "duration_ms": duration_ms,
                    "transaction_pending": connection is not None,
                    "details": safe,
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        )

    def best_effort(self, *args, **kwargs):
        try:
            self.record(*args, **kwargs)
        except Exception:
            _LOGGER.warning("memory diagnostic persistence failed")

    def list(
        self,
        companion_id,
        after_sequence=0,
        limit=100,
        operation=None,
        status=None,
        command_id=None,
    ):
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or type(limit) is not int
            or not 1 <= limit <= 500
        ):
            raise ValueError("Expected nonnegative cursor and limit between 1 and 500")
        if operation is not None and operation not in OPERATIONS:
            raise ValueError("Unknown memory log operation")
        if status is not None and status not in STATUSES:
            raise ValueError("Unknown memory log status")
        conditions, values = ["companion_id = ?", "sequence > ?"], [companion_id, after_sequence]
        for key, value in (
            ("operation", operation),
            ("status", status),
            ("command_id", command_id),
        ):
            if value is not None:
                conditions.append(f"{key} = ?")
                values.append(value)
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM memory_logs WHERE "
                + " AND ".join(conditions)
                + " ORDER BY sequence LIMIT ?",
                [*values, limit + 1],
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            items.append(item)
        return {
            "items": items,
            "next_sequence": items[-1]["sequence"] if items else after_sequence,
            "has_more": len(rows) > limit,
        }
