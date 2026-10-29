"""Bounded, event-driven participation from explicit host attention signals.

Sampling never invokes a model. Claims, decisions and receipts share SQLite;
the dialogue executor rechecks this service's gate before dispatch and commit.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .coordinator import DomainError
from .dialogue import DialogueError
from .focus import FocusService
from .models import Identifier


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class AttentionSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    schema_version: Literal["1.0"] = "1.0"
    session_id: Identifier
    sequence: int = Field(ge=0, le=2**63 - 1)
    sampled_at: str
    ttl_seconds: int = Field(default=90, ge=5, le=300)
    activity: Literal[
        "code",
        "docs",
        "chat",
        "meeting",
        "video",
        "game",
        "media",
        "terminal",
        "browser",
        "unknown",
    ] = "unknown"
    focus: Literal["focused", "unfocused", "suspected_stuck", "unknown"] = "unknown"
    availability: Literal["available", "busy", "unknown"] = "unknown"
    user_present: bool | None = None
    locked: bool | None = None
    meeting: bool | None = None
    screen_sharing: bool | None = None
    boundary: Literal["none", "task_completed", "break_started", "user_returned"] = "none"
    boundary_id: Identifier | None = None

    @field_validator("sampled_at")
    @classmethod
    def timestamp(cls, value):
        if len(value) > 40:
            raise ValueError("sampled_at must be an ISO 8601 timestamp with timezone")
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError("Missing timezone")
            return _timestamp(instant)
        except (ValueError, OverflowError):
            raise ValueError("sampled_at must be an ISO 8601 timestamp with timezone") from None

    @model_validator(mode="after")
    def boundary_identity(self):
        if (self.boundary == "none") != (self.boundary_id is None):
            raise ValueError("A boundary requires boundary_id; omit it when boundary is none")
        return self


class AttentionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Identifier
    sample: AttentionSample


class AttentionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: Identifier
    source_event_id: Identifier | None = None
    sample: AttentionSample | None = None
    expires_at: str | None = None
    stale: bool = True


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    min_delay_seconds: int = 600
    max_delay_seconds: int = 86_400
    min_idle_seconds: int = 3_600

    def __post_init__(self) -> None:
        for name in ("min_delay_seconds", "max_delay_seconds", "min_idle_seconds"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if not 60 <= self.min_delay_seconds <= self.max_delay_seconds <= 604_800:
            raise ValueError("Schedule delays must be between 60 and 604800 seconds")
        if not 60 <= self.min_idle_seconds <= 604_800:
            raise ValueError("Schedule idle threshold must be between 60 and 604800 seconds")


MIN_TASK_DELAY_SECONDS = 60
MAX_TASK_DELAY_SECONDS = 2_592_000
MAX_TASK_TITLE_CHARS = 200
MAX_TASK_DETAILS_CHARS = 2_000
TASK_STATUSES = (
    "pending",
    "queued",
    "completed",
    "silent",
    "suppressed",
    "discarded",
    "failed",
    "cancelled",
    "missed",
)
TASK_STATUS_FILTERS = ("active", *TASK_STATUSES)
TaskStatusFilter = Literal[
    "active",
    "pending",
    "queued",
    "completed",
    "silent",
    "suppressed",
    "discarded",
    "failed",
    "cancelled",
    "missed",
]


class ProactivityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    daily_limit: int = Field(default=1, ge=1, le=20)
    cooldown_seconds: int = Field(default=3600, ge=30, le=86400)
    candidate_ttl_seconds: int = Field(default=120, ge=5, le=600)
    token_budget: int = Field(default=3072, ge=1, le=4096)
    memory_token_budget: int = Field(default=400, ge=0, le=4096)
    max_output_tokens: int = Field(default=128, ge=1, le=256)


class ProactivityPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Identifier
    expected_revision: int = Field(ge=0, le=2**63 - 1)
    policy: ProactivityPolicy


class ProactivityPolicySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    companion_id: Identifier
    revision: int
    policy: ProactivityPolicy
    updated_at: str | None = None


class ProactivityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sequence: int
    candidate_id: Identifier
    companion_id: Identifier
    node_id: Identifier
    epoch: int
    source_event_id: Identifier
    boundary: str
    policy_revision: int
    status: Literal["queued", "suppressed", "running", "completed", "silent", "discarded", "failed"]
    reason: str
    command_id: Identifier
    created_at: str
    expires_at: str
    origin: Literal["attention", "inner", "schedule", "task"] = "attention"
    source_activity_id: Identifier | None = None
    scheduled_task_id: Identifier | None = None
    started_at: str | None = None
    finished_at: str | None = None


class ProactivityDecisionPage(BaseModel):
    decisions: list[ProactivityDecision]
    next_sequence: int


class ProactivityService:
    def __init__(self, dialogue, *, recovery_seconds=300, inner_life=None, schedule_policy=None):
        self.dialogue = dialogue
        self.coordinator = dialogue.coordinator
        self.store = dialogue.store
        self.recovery_seconds = recovery_seconds
        self.inner_life = inner_life
        self.schedule_policy = schedule_policy or SchedulePolicy()
        self.focus = FocusService(self.coordinator)
        with self.store.transaction(write=True) as connection:
            for statement in (
                """CREATE TABLE IF NOT EXISTS attention_samples (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
                    source_event_id TEXT NOT NULL, sample_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS attention_sessions (
                    node_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    PRIMARY KEY(node_id, session_id))""",
                """CREATE TABLE IF NOT EXISTS proactivity_policies (
                    companion_id TEXT PRIMARY KEY REFERENCES companions(companion_id),
                    revision INTEGER NOT NULL, policy_json TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS proactivity_candidates (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE,
                    companion_id TEXT NOT NULL REFERENCES companions(companion_id),
                    node_id TEXT NOT NULL, epoch INTEGER NOT NULL, source_event_id TEXT NOT NULL,
                    boundary TEXT NOT NULL, policy_revision INTEGER NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, command_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'attention',
                    source_activity_id TEXT,
                    scheduled_task_id TEXT,
                    started_at TEXT, finished_at TEXT)""",
                """CREATE UNIQUE INDEX IF NOT EXISTS one_proactive_execution
                    ON proactivity_candidates(companion_id) WHERE status = 'running'""",
                """CREATE INDEX IF NOT EXISTS proactive_queue
                    ON proactivity_candidates(status, sequence)""",
                """CREATE INDEX IF NOT EXISTS proactive_attempts
                    ON proactivity_candidates(companion_id, started_at)""",
                """CREATE TABLE IF NOT EXISTS proactive_schedules (
                    companion_id TEXT PRIMARY KEY REFERENCES companions(companion_id),
                    command_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL, due_at TEXT NOT NULL,
                    reason TEXT, status TEXT NOT NULL, candidate_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(command_id, tool_call_id))""",
                """CREATE INDEX IF NOT EXISTS proactive_schedule_queue
                    ON proactive_schedules(status, due_at)""",
                """CREATE TABLE IF NOT EXISTS proactive_schedule_receipts (
                    command_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(command_id, tool_call_id))""",
                """CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    task_id TEXT PRIMARY KEY,
                    companion_id TEXT NOT NULL REFERENCES companions(companion_id),
                    command_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL, title TEXT NOT NULL,
                    details TEXT, due_at TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT, candidate_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    fired_at TEXT,
                    UNIQUE(command_id, tool_call_id))""",
                """CREATE INDEX IF NOT EXISTS scheduled_task_queue
                    ON scheduled_tasks(status, due_at)""",
                """CREATE INDEX IF NOT EXISTS scheduled_task_companion
                    ON scheduled_tasks(companion_id, created_at)""",
            ):
                connection.execute(statement)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(proactivity_candidates)")
            }
            if "origin" not in columns:
                connection.execute(
                    "ALTER TABLE proactivity_candidates "
                    "ADD COLUMN origin TEXT NOT NULL DEFAULT 'attention'"
                )
            if "source_activity_id" not in columns:
                connection.execute(
                    "ALTER TABLE proactivity_candidates ADD COLUMN source_activity_id TEXT"
                )
            if "scheduled_task_id" not in columns:
                connection.execute(
                    "ALTER TABLE proactivity_candidates ADD COLUMN scheduled_task_id TEXT"
                )
        dialogue.proactivity_gate = self.gate
        dialogue.proactive_finalized = self._finalized

    def attach_inner_life(self, inner_life):
        self.inner_life = inner_life

    def _policy(self, connection, companion_id):
        row = connection.execute(
            "SELECT * FROM proactivity_policies WHERE companion_id=?", (companion_id,)
        ).fetchone()
        return {
            "companion_id": companion_id,
            "revision": row["revision"] if row else 0,
            "policy": json.loads(row["policy_json"]) if row else ProactivityPolicy().model_dump(),
            "updated_at": row["updated_at"] if row else None,
        }

    def get_policy(self, companion_id):
        self.coordinator.get_companion(companion_id)
        with self.store.transaction() as connection:
            return self._policy(connection, companion_id)

    def update_policy(self, companion_id, payload):
        data = ProactivityPolicyUpdate.model_validate(payload).model_dump(mode="json")

        def action(connection, now):
            self.coordinator._row(connection, "companions", "companion_id", companion_id)
            current = self._policy(connection, companion_id)
            if current["revision"] != data["expected_revision"]:
                raise DomainError(409, "revision_conflict", "Proactivity policy revision changed")
            if current["revision"] == 2**63 - 1:
                raise DomainError(409, "version_exhausted", "Proactivity revision exhausted")
            connection.execute(
                "INSERT OR REPLACE INTO proactivity_policies VALUES (?, ?, ?, ?)",
                (companion_id, current["revision"] + 1, _json(data["policy"]), _timestamp(now)),
            )
            self.coordinator._emit(
                connection,
                now,
                "companion.proactivity_policy_updated",
                {"policy_revision": current["revision"] + 1},
                companion_id=companion_id,
            )
            return self._policy(connection, companion_id)

        return self.coordinator._write(
            action, operation="proactivity_policy", subject=companion_id, command=data
        )

    def schedule(
        self,
        companion_id,
        command_id,
        tool_call_id,
        delay_seconds,
        reason=None,
    ):
        if type(delay_seconds) is not int:
            raise ValueError("Proactive delay must be an integer")
        policy = self.schedule_policy
        if not policy.min_delay_seconds <= delay_seconds <= policy.max_delay_seconds:
            raise ValueError(
                f"Proactive delay must be between {policy.min_delay_seconds} and "
                f"{policy.max_delay_seconds} seconds"
            )
        if reason is not None:
            if not isinstance(reason, str):
                raise ValueError("Proactive reason must be text")
            reason = reason.strip()[:200] or None
        with self.store.transaction(write=True) as connection:
            self.coordinator._row(connection, "companions", "companion_id", companion_id)
            receipt = connection.execute(
                """SELECT result_json FROM proactive_schedule_receipts
                    WHERE command_id=? AND tool_call_id=?""",
                (command_id, tool_call_id),
            ).fetchone()
            if receipt is not None:
                return json.loads(receipt["result_json"])
            now = self.coordinator._now()
            due = now + timedelta(seconds=delay_seconds)
            self._cancel_pending(connection, companion_id, now, "replaced")
            event = self.coordinator._emit(
                connection,
                now,
                "companion.proactive_scheduled",
                {
                    "delay_seconds": delay_seconds,
                    "due_at": _timestamp(due),
                    "reason": reason,
                },
                companion_id=companion_id,
            )
            connection.execute(
                """INSERT INTO proactive_schedules
                    (companion_id, command_id, tool_call_id, source_event_id, due_at,
                     reason, status, candidate_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                    ON CONFLICT(companion_id) DO UPDATE SET
                        command_id=excluded.command_id,
                        tool_call_id=excluded.tool_call_id,
                        source_event_id=excluded.source_event_id,
                        due_at=excluded.due_at, reason=excluded.reason, status='pending',
                        candidate_id=NULL, created_at=excluded.created_at,
                        updated_at=excluded.updated_at""",
                (
                    companion_id,
                    command_id,
                    tool_call_id,
                    event["event_id"],
                    _timestamp(due),
                    reason,
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            result = {
                "status": "scheduled",
                "delay_seconds": delay_seconds,
                "due_at": _timestamp(due),
                "source_event_id": event["event_id"],
            }
            connection.execute(
                """INSERT INTO proactive_schedule_receipts
                    (command_id, tool_call_id, result_json, created_at)
                    VALUES (?, ?, ?, ?)""",
                (command_id, tool_call_id, _json(result), _timestamp(now)),
            )
            return result

    def snapshot_schedule(self, companion_id):
        self.coordinator.get_companion(companion_id)
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM proactive_schedules WHERE companion_id=?", (companion_id,)
            ).fetchone()
            if row is None:
                return {"companion_id": companion_id, "schedule": None}
            return {
                "companion_id": companion_id,
                "schedule": {
                    "command_id": row["command_id"],
                    "origin": "schedule",
                    "status": row["status"],
                    "due_at": row["due_at"],
                    "reason": row["reason"],
                    "candidate_id": row["candidate_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                },
            }

    def schedule_task(
        self,
        companion_id,
        command_id,
        tool_call_id,
        title,
        details,
        delay_seconds,
    ):
        if type(delay_seconds) is not int or not (
            MIN_TASK_DELAY_SECONDS <= delay_seconds <= MAX_TASK_DELAY_SECONDS
        ):
            raise ValueError(
                f"Task delay must be between {MIN_TASK_DELAY_SECONDS} and "
                f"{MAX_TASK_DELAY_SECONDS} seconds"
            )
        if not isinstance(title, str):
            raise ValueError("Task title must be text")
        title = title.strip()
        if not 1 <= len(title) <= MAX_TASK_TITLE_CHARS:
            raise ValueError(f"Task title must contain 1 to {MAX_TASK_TITLE_CHARS} characters")
        if details is not None:
            if not isinstance(details, str):
                raise ValueError("Task details must be text")
            details = details.strip() or None
            if details is not None and len(details) > MAX_TASK_DETAILS_CHARS:
                raise ValueError(
                    f"Task details must contain at most {MAX_TASK_DETAILS_CHARS} characters"
                )
        with self.store.transaction(write=True) as connection:
            self.coordinator._row(connection, "companions", "companion_id", companion_id)
            existing = connection.execute(
                """SELECT * FROM scheduled_tasks
                    WHERE command_id=? AND tool_call_id=?""",
                (command_id, tool_call_id),
            ).fetchone()
            if existing is not None:
                if existing["companion_id"] != companion_id:
                    raise ValueError("Scheduled task command belongs to another companion")
                return self._task_snapshot(existing)
            now = self.coordinator._now()
            due = now + timedelta(seconds=delay_seconds)
            identity = _json(["task", companion_id, command_id, tool_call_id])
            task_id = hashlib.sha256(identity.encode()).hexdigest()
            event = self.coordinator._emit(
                connection,
                now,
                "companion.scheduled_task_created",
                {
                    "task_id": task_id,
                    "title": title,
                    "due_at": _timestamp(due),
                    "delay_seconds": delay_seconds,
                },
                companion_id=companion_id,
            )
            connection.execute(
                """INSERT INTO scheduled_tasks
                    (task_id, companion_id, command_id, tool_call_id, source_event_id,
                     title, details, due_at, status, reason, candidate_id,
                     created_at, updated_at, fired_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, NULL)""",
                (
                    task_id,
                    companion_id,
                    command_id,
                    tool_call_id,
                    event["event_id"],
                    title,
                    details,
                    _timestamp(due),
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._task_snapshot(row)

    def list_tasks(self, companion_id, *, status=None, limit=20, offset=0):
        if status is not None and status not in TASK_STATUS_FILTERS:
            raise ValueError("Unknown scheduled task status filter")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Scheduled task list limit must be between 1 and 50")
        if type(offset) is not int or offset < 0:
            raise ValueError("Scheduled task offset must be nonnegative")
        self.coordinator.get_companion(companion_id)
        where = "companion_id=?"
        parameters: list[object] = [companion_id]
        if status == "active":
            where += " AND status IN ('pending', 'queued')"
        elif status is not None:
            where += " AND status=?"
            parameters.append(status)
        with self.store.transaction() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM scheduled_tasks WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT * FROM scheduled_tasks WHERE {where}
                    ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "companion_id": companion_id,
                "status": status,
                "items": [self._task_snapshot(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def snapshot_task(self, companion_id, task_id):
        self.coordinator.get_companion(companion_id)
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE companion_id=? AND task_id=?",
                (companion_id, task_id),
            ).fetchone()
            if row is None:
                raise DomainError(404, "scheduled_task_not_found", "Scheduled task was not found")
            return self._task_snapshot(row)

    def cancel_task(self, companion_id, task_id):
        with self.store.transaction(write=True) as connection:
            self.coordinator._row(connection, "companions", "companion_id", companion_id)
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE companion_id=? AND task_id=?",
                (companion_id, task_id),
            ).fetchone()
            if row is None:
                raise DomainError(404, "scheduled_task_not_found", "Scheduled task was not found")
            return self._cancel_task_row(connection, row, self.coordinator._now())

    def cancel_task_by_title(self, companion_id, title):
        if not isinstance(title, str):
            raise ValueError("Scheduled task title must be text")
        title = title.strip()
        if not 1 <= len(title) <= MAX_TASK_TITLE_CHARS:
            raise ValueError(
                f"Scheduled task title must contain 1 to {MAX_TASK_TITLE_CHARS} characters"
            )
        with self.store.transaction(write=True) as connection:
            self.coordinator._row(connection, "companions", "companion_id", companion_id)
            rows = connection.execute(
                """SELECT * FROM scheduled_tasks
                    WHERE companion_id=? AND title=? COLLATE NOCASE
                    AND status IN ('pending', 'queued')
                    ORDER BY created_at, task_id""",
                (companion_id, title),
            ).fetchall()
            if not rows:
                raise DomainError(404, "scheduled_task_not_found", "Scheduled task was not found")
            if len(rows) > 1:
                raise DomainError(
                    409,
                    "scheduled_task_ambiguous",
                    "Multiple active scheduled tasks match that title",
                )
            return self._cancel_task_row(connection, rows[0], self.coordinator._now())

    def _cancel_task_row(self, connection, row, now):
        if row["status"] == "cancelled":
            return self._task_snapshot(row)
        if row["status"] not in ("pending", "queued"):
            raise DomainError(
                409, "scheduled_task_not_cancellable", "Scheduled task already finished"
            )
        if row["candidate_id"] is not None:
            candidate = connection.execute(
                "SELECT * FROM proactivity_candidates WHERE candidate_id=?",
                (row["candidate_id"],),
            ).fetchone()
            if candidate is not None and candidate["status"] == "running":
                raise DomainError(
                    409, "scheduled_task_in_progress", "Scheduled task is already running"
                )
            if candidate is not None and candidate["status"] == "queued":
                self._decision(connection, candidate, "suppressed", "task_cancelled", now)
        connection.execute(
            """UPDATE scheduled_tasks SET status='cancelled', reason='user_cancelled',
                updated_at=? WHERE task_id=?""",
            (_timestamp(now), row["task_id"]),
        )
        self.coordinator._emit(
            connection,
            now,
            "companion.scheduled_task_cancelled",
            {"task_id": row["task_id"], "candidate_id": row["candidate_id"]},
            companion_id=row["companion_id"],
        )
        current = connection.execute(
            "SELECT * FROM scheduled_tasks WHERE task_id=?", (row["task_id"],)
        ).fetchone()
        return self._task_snapshot(current)

    @staticmethod
    def _task_snapshot(row):
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "details": row["details"],
            "status": row["status"],
            "reason": row["reason"],
            "due_at": row["due_at"],
            "candidate_id": row["candidate_id"],
            "source_event_id": row["source_event_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "fired_at": row["fired_at"],
        }

    def cancel_schedules(self, connection, companion_id, now, reason="user_interacting"):
        return self._cancel_pending(connection, companion_id, now, reason)

    def _cancel_pending(self, connection, companion_id, now, reason):
        row = connection.execute(
            "SELECT * FROM proactive_schedules WHERE companion_id=? AND status='pending'",
            (companion_id,),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "UPDATE proactive_schedules SET status='cancelled', updated_at=? WHERE companion_id=?",
            (_timestamp(now), companion_id),
        )
        self.coordinator._emit(
            connection,
            now,
            "companion.proactive_schedule_cancelled",
            {"due_at": row["due_at"], "reason": reason},
            companion_id=companion_id,
        )
        return True

    def _enqueue_tasks(self, connection, now):
        rows = connection.execute(
            """SELECT * FROM scheduled_tasks
                WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 20""",
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            companion_row = connection.execute(
                "SELECT * FROM companions WHERE companion_id=?", (row["companion_id"],)
            ).fetchone()
            if companion_row is None or not companion_row["active_node_id"]:
                self._miss_task(connection, row, now, "no_active_node")
                continue
            idle = self._idle_reason(connection, row["companion_id"], now)
            if idle is not None:
                self._miss_task(connection, row, now, idle)
                continue
            companion = self.coordinator._companion(companion_row)
            policy = self._policy(connection, row["companion_id"])
            identity = _json(["task", row["task_id"]])
            candidate_id = hashlib.sha256(identity.encode()).hexdigest()
            expires = now + timedelta(seconds=policy["policy"]["candidate_ttl_seconds"])
            cursor = connection.execute(
                """INSERT OR IGNORE INTO proactivity_candidates
                    (candidate_id, companion_id, node_id, epoch, source_event_id, boundary,
                     policy_revision, status, reason, command_id, created_at, expires_at,
                     origin, source_activity_id, scheduled_task_id)
                    VALUES (?, ?, ?, ?, ?, 'scheduled_task', ?, 'queued', 'eligible', ?, ?, ?,
                            'task', NULL, ?)""",
                (
                    candidate_id,
                    row["companion_id"],
                    companion["active_node_id"],
                    companion["epoch"],
                    row["source_event_id"],
                    policy["revision"],
                    "proactive-" + candidate_id,
                    _timestamp(now),
                    _timestamp(expires),
                    row["task_id"],
                ),
            )
            connection.execute(
                """UPDATE scheduled_tasks SET status='queued', candidate_id=?, fired_at=?,
                    updated_at=? WHERE task_id=? AND status='pending'""",
                (candidate_id, _timestamp(now), _timestamp(now), row["task_id"]),
            )
            if not cursor.rowcount:
                continue
            candidate = connection.execute(
                "SELECT * FROM proactivity_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            reason = self._reason(connection, candidate, companion, now)
            status = "queued" if reason == "eligible" else "suppressed"
            self._decision(connection, candidate, status, reason, now)
            self._update_task_status(connection, candidate, status, reason, now)

    def _miss_task(self, connection, row, now, reason):
        cursor = connection.execute(
            """UPDATE scheduled_tasks SET status='missed', reason=?, fired_at=?,
                updated_at=? WHERE task_id=? AND status='pending'""",
            (reason, _timestamp(now), _timestamp(now), row["task_id"]),
        )
        if not cursor.rowcount:
            return
        self.coordinator._emit(
            connection,
            now,
            "companion.scheduled_task_missed",
            {"task_id": row["task_id"], "reason": reason},
            companion_id=row["companion_id"],
        )

    def _update_task_status(self, connection, candidate, status, reason, now):
        task_id = candidate["scheduled_task_id"]
        if task_id is None:
            return
        connection.execute(
            """UPDATE scheduled_tasks SET status=?, reason=?, updated_at=?
                WHERE task_id=? AND status IN ('pending', 'queued')""",
            (status, reason, _timestamp(now), task_id),
        )

    def _enqueue_scheduled(self, connection, now):
        rows = connection.execute(
            """SELECT * FROM proactive_schedules
                WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 20""",
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            companion_row = connection.execute(
                "SELECT * FROM companions WHERE companion_id=?", (row["companion_id"],)
            ).fetchone()
            if companion_row is None or not companion_row["active_node_id"]:
                self._cancel_pending(connection, row["companion_id"], now, "no_active_node")
                continue
            idle = self._idle_reason(connection, row["companion_id"], now)
            if idle is not None:
                self._cancel_pending(connection, row["companion_id"], now, idle)
                continue
            companion = self.coordinator._companion(companion_row)
            policy = self._policy(connection, row["companion_id"])
            identity = _json(
                ["schedule", row["companion_id"], row["command_id"], row["tool_call_id"]]
            )
            candidate_id = hashlib.sha256(identity.encode()).hexdigest()
            expires = now + timedelta(seconds=policy["policy"]["candidate_ttl_seconds"])
            cursor = connection.execute(
                """INSERT OR IGNORE INTO proactivity_candidates
                    (candidate_id, companion_id, node_id, epoch, source_event_id, boundary,
                     policy_revision, status, reason, command_id, created_at, expires_at,
                     origin, source_activity_id)
                    VALUES (?, ?, ?, ?, ?, 'scheduled', ?, 'queued', 'eligible', ?, ?, ?,
                            'schedule', NULL)""",
                (
                    candidate_id,
                    row["companion_id"],
                    companion["active_node_id"],
                    companion["epoch"],
                    row["source_event_id"],
                    policy["revision"],
                    "proactive-" + candidate_id,
                    _timestamp(now),
                    _timestamp(expires),
                ),
            )
            connection.execute(
                """UPDATE proactive_schedules SET status='fired', candidate_id=?, updated_at=?
                    WHERE companion_id=?""",
                (candidate_id, _timestamp(now), row["companion_id"]),
            )
            if not cursor.rowcount:
                continue
            candidate = connection.execute(
                "SELECT * FROM proactivity_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            reason = self._reason(connection, candidate, companion, now)
            self._decision(
                connection,
                candidate,
                "queued" if reason == "eligible" else "suppressed",
                reason,
                now,
            )

    def _idle_reason(self, connection, companion_id, now):
        recent = connection.execute(
            """SELECT 1 FROM events WHERE companion_id=?
                AND type='companion.dialogue_received' AND occurred_at > ? LIMIT 1""",
            (
                companion_id,
                _timestamp(now - timedelta(seconds=self.schedule_policy.min_idle_seconds)),
            ),
        ).fetchone()
        if recent is not None:
            return "user_interacting"
        if not connection.execute(
            """SELECT 1 FROM events WHERE companion_id=?
                AND type='companion.dialogue_received' LIMIT 1""",
            (companion_id,),
        ).fetchone():
            return "no_dialogue_history"
        return None

    def _attention(self, connection, node_id, now):
        row = connection.execute(
            "SELECT * FROM attention_samples WHERE node_id=?", (node_id,)
        ).fetchone()
        return {
            "node_id": node_id,
            "source_event_id": row["source_event_id"] if row else None,
            "sample": json.loads(row["sample_json"]) if row else None,
            "expires_at": row["expires_at"] if row else None,
            "stale": row is None or _instant(row["expires_at"]) <= now,
        }

    def get_attention(self, node_id):
        self.coordinator.get_node(node_id)
        with self.store.transaction() as connection:
            return self._attention(connection, node_id, self.coordinator._now())

    def observe(self, node_id, payload):
        data = AttentionObservation.model_validate(payload).model_dump(mode="json")
        sample = data["sample"]

        def action(connection, now):
            c = self.coordinator
            node = c._node(c._row(connection, "nodes", "node_id", node_id), now, connection)
            if "attention.observe" not in node["capabilities"]:
                raise DomainError(
                    409, "capability_required", "Attention requires attention.observe"
                )
            sampled = _instant(sample["sampled_at"])
            expires = min(sampled, now) + timedelta(seconds=sample["ttl_seconds"])
            if sampled > now + timedelta(seconds=5) or expires <= now:
                raise DomainError(
                    409, "attention_stale", "Attention sample is expired or future dated"
                )
            previous = self._attention(connection, node_id, now)["sample"]
            if previous:
                same_session = previous["session_id"] == sample["session_id"]
                retired = connection.execute(
                    "SELECT 1 FROM attention_sessions WHERE node_id=? AND session_id=?",
                    (node_id, sample["session_id"]),
                ).fetchone()
                if (
                    (same_session and sample["sequence"] <= previous["sequence"])
                    or (not same_session and retired)
                    or sample["sampled_at"] < previous["sampled_at"]
                    or (not same_session and sample["sampled_at"] == previous["sampled_at"])
                ):
                    raise DomainError(
                        409, "attention_out_of_order", "Attention session or sequence is old"
                    )
            connection.execute(
                "INSERT OR IGNORE INTO attention_sessions VALUES (?, ?)",
                (node_id, sample["session_id"]),
            )
            volatile = {"sequence", "sampled_at", "ttl_seconds"}
            changed = previous is None or (
                {key: value for key, value in previous.items() if key not in volatile}
                != {key: value for key, value in sample.items() if key not in volatile}
            )
            if changed:
                event = c._emit(
                    connection,
                    now,
                    "node.observation",
                    {"modality": "ambient", "data": {"attention": sample}, "user_initiated": False},
                    node_id=node_id,
                )
            else:
                previous_event_id = self._attention(connection, node_id, now)["source_event_id"]
                event = {"event_id": previous_event_id, "node_id": node_id}
            connection.execute(
                "INSERT OR REPLACE INTO attention_samples VALUES (?, ?, ?, ?)",
                (node_id, event["event_id"], _json(sample), _timestamp(expires)),
            )
            self.focus.observe(connection, node_id, sample, expires, now)
            if changed and sample["boundary"] != "none":
                companions = connection.execute(
                    "SELECT * FROM companions WHERE active_node_id=?", (node_id,)
                ).fetchall()
                for row in companions:
                    self._enqueue(connection, c._companion(row), event, sample, now)
            return self._attention(connection, node_id, now)

        result = self.coordinator._write(
            action, operation="attention", subject=node_id, command=data
        )
        # A fresh attention sample with the user present is room activity: the
        # lifeform flows to whichever room last saw the user, even when the
        # companion was parked in an empty room before.
        if self.coordinator.auto_follow_rooms and sample.get("user_present"):
            try:
                self.coordinator.record_room_activity(node_id, self.coordinator._now())
            except Exception:
                pass
        return result

    def _enqueue(self, connection, companion, event, sample, now):
        identity = _json(
            [
                companion["companion_id"],
                event["node_id"],
                sample["session_id"],
                sample["boundary_id"],
            ]
        )
        candidate_id = hashlib.sha256(identity.encode()).hexdigest()
        policy = self._policy(connection, companion["companion_id"])
        expires = now + timedelta(seconds=policy["policy"]["candidate_ttl_seconds"])
        cursor = connection.execute(
            """INSERT OR IGNORE INTO proactivity_candidates
                (candidate_id, companion_id, node_id, epoch, source_event_id, boundary,
                 policy_revision, status, reason, command_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'eligible', ?, ?, ?)""",
            (
                candidate_id,
                companion["companion_id"],
                event["node_id"],
                companion["epoch"],
                event["event_id"],
                sample["boundary"],
                policy["revision"],
                "proactive-" + candidate_id,
                _timestamp(now),
                _timestamp(expires),
            ),
        )
        if not cursor.rowcount:
            return
        for previous in connection.execute(
            """SELECT * FROM proactivity_candidates WHERE companion_id=?
                AND status='queued' AND candidate_id!=?""",
            (companion["companion_id"], candidate_id),
        ).fetchall():
            self._decision(connection, previous, "suppressed", "boundary_superseded", now)
        row = connection.execute(
            "SELECT * FROM proactivity_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        reason = self._reason(connection, row, companion, now)
        self._decision(
            connection, row, "queued" if reason == "eligible" else "suppressed", reason, now
        )

    def _reason(self, connection, candidate, companion, now):
        policy = self._policy(connection, companion["companion_id"])
        if not policy["policy"]["enabled"]:
            return "disabled"
        if policy["revision"] != candidate["policy_revision"]:
            return "policy_changed"
        if _instant(candidate["expires_at"]) <= now:
            return "candidate_expired"
        if (
            companion["active_node_id"] != candidate["node_id"]
            or companion["epoch"] != candidate["epoch"]
        ):
            return "ownership_changed"
        if companion["interaction_mode"] != "chat":
            return "waiting_for_user"
        if self.coordinator._pending(connection, companion["companion_id"]) is not None:
            return "transfer_in_progress"
        if not companion["lease_expires_at"] or _instant(companion["lease_expires_at"]) <= now:
            return "output_not_permitted"
        node = self.coordinator._node(
            self.coordinator._row(connection, "nodes", "node_id", candidate["node_id"]),
            now,
            connection,
        )
        if not node["online"] or not {"text.display", "attention.observe"}.issubset(
            node["capabilities"]
        ):
            return "output_not_permitted"
        attention = self._attention(connection, candidate["node_id"], now)
        if attention["stale"]:
            return "attention_stale"
        sample = attention["sample"]
        origin = candidate["origin"] or "attention"
        if origin == "inner":
            if self.inner_life is None or not self.inner_life.policy.enabled:
                return "inner_life_disabled"
            activity = connection.execute(
                "SELECT * FROM inner_activities WHERE activity_id=?",
                (candidate["source_activity_id"],),
            ).fetchone()
            if (
                activity is None
                or activity["status"] != "active"
                or _instant(activity["expires_at"]) <= now
            ):
                return "activity_expired"
            if (
                activity["attention_session_id"] is not None
                and sample["session_id"] != activity["attention_session_id"]
            ):
                return "attention_session_changed"
            if (
                sample["boundary"] != "none"
                and sample["boundary_id"] != activity["attention_boundary_id"]
            ):
                return "boundary_superseded"
            if activity["urge"] < self.inner_life.policy.proactive_urge_threshold:
                return "below_urge_threshold"
        elif origin == "schedule":
            schedule = connection.execute(
                """SELECT * FROM proactive_schedules
                    WHERE companion_id=? AND candidate_id=? AND status='fired'""",
                (companion["companion_id"], candidate["candidate_id"]),
            ).fetchone()
            if schedule is None:
                return "schedule_cancelled"
            idle = self._idle_reason(connection, companion["companion_id"], now)
            if idle is not None:
                return idle
        elif origin == "task":
            task = connection.execute(
                """SELECT * FROM scheduled_tasks
                    WHERE task_id=? AND companion_id=? AND candidate_id=?
                    AND status IN ('queued', 'completed', 'silent', 'discarded', 'failed')""",
                (
                    candidate["scheduled_task_id"],
                    companion["companion_id"],
                    candidate["candidate_id"],
                ),
            ).fetchone()
            if task is None:
                return "task_cancelled"
            idle = self._idle_reason(connection, companion["companion_id"], now)
            if idle is not None:
                return idle
        else:
            source = connection.execute(
                "SELECT sequence, payload_json FROM events WHERE event_id=?",
                (candidate["source_event_id"],),
            ).fetchone()
            original = json.loads(source["payload_json"])["data"]["attention"]
            if sample["session_id"] != original["session_id"]:
                return "attention_session_changed"
            if sample["boundary"] != "none" and sample["boundary_id"] != original["boundary_id"]:
                return "boundary_superseded"
        if sample["locked"] is True or sample["user_present"] is False:
            return "user_away"
        if (
            sample["meeting"] is True
            or sample["screen_sharing"] is True
            or sample["activity"] == "meeting"
        ):
            return "meeting_or_sharing"
        if sample["focus"] == "focused":
            return "focused"
        if sample["focus"] == "suspected_stuck":
            return "suspected_stuck"
        if sample["availability"] == "busy":
            return "busy"
        if (
            sample["focus"] != "unfocused"
            or sample["availability"] != "available"
            or sample["user_present"] is not True
            or sample["locked"] is not False
            or sample["meeting"] is not False
            or sample["screen_sharing"] is not False
        ):
            return "attention_unknown"
        if self.focus.snapshot(connection, candidate["node_id"], now)["protection_active"]:
            return "focus_protection"
        if origin == "inner":
            idle_seconds = self.inner_life.policy.proactive_min_idle_seconds
            recent_user = connection.execute(
                """SELECT 1 FROM events WHERE companion_id=?
                    AND type='companion.dialogue_received' AND occurred_at > ?
                    LIMIT 1""",
                (
                    companion["companion_id"],
                    _timestamp(now - timedelta(seconds=idle_seconds)),
                ),
            ).fetchone()
            if recent_user:
                return "user_interacting"
            if not connection.execute(
                """SELECT 1 FROM events WHERE companion_id=?
                    AND type='companion.dialogue_received' LIMIT 1""",
                (companion["companion_id"],),
            ).fetchone():
                return "no_dialogue_history"
        elif origin in ("schedule", "task"):
            pass
        else:
            recent_user = connection.execute(
                """SELECT 1 FROM events WHERE companion_id=? AND type='companion.dialogue_received'
                    AND (sequence > ? OR occurred_at > ?) LIMIT 1""",
                (
                    companion["companion_id"],
                    source["sequence"],
                    _timestamp(now - timedelta(seconds=30)),
                ),
            ).fetchone()
            if recent_user:
                return "user_interacting"
        p = self.dialogue.personality_service
        profile = p._profile_snapshot(
            companion["companion_id"], p._profile_row(connection, companion["companion_id"])
        )
        state = p._state_snapshot(
            companion["companion_id"], p._state_row(connection, companion["companion_id"]), now
        )
        effective_state = state["state"]
        if self.dialogue.emotion_service:
            emotion = self.dialogue.emotion_service.snapshot(
                connection, companion["companion_id"], profile, state, now
            )
            if emotion["quiet_requested"]:
                return "quiet_requested"
            if emotion["enabled"]:
                effective_state = emotion["effective_state"]
        if profile["definition"]["traits"]["initiative"] < 0.65:
            return "initiative_below_threshold"
        if effective_state["energy"] < 0.35:
            return "energy_below_threshold"
        if effective_state["social_battery"] < 0.25:
            return "social_battery_below_threshold"
        attempts = connection.execute(
            """SELECT COUNT(*) AS total, MAX(started_at) AS last FROM proactivity_candidates
                WHERE companion_id=? AND candidate_id!=? AND started_at >= ?""",
            (
                companion["companion_id"],
                candidate["candidate_id"],
                _timestamp(now - timedelta(days=1)),
            ),
        ).fetchone()
        if (
            attempts["last"]
            and (now - _instant(attempts["last"])).total_seconds()
            < policy["policy"]["cooldown_seconds"]
        ):
            return "cooldown"
        daily = connection.execute(
            """SELECT COUNT(*) FROM proactivity_candidates WHERE companion_id=?
                AND candidate_id!=? AND started_at >= ?""",
            (
                companion["companion_id"],
                candidate["candidate_id"],
                _timestamp(now.replace(hour=0, minute=0, second=0, microsecond=0)),
            ),
        ).fetchone()[0]
        if daily >= policy["policy"]["daily_limit"]:
            return "daily_limit"
        # The proactive reply is generated by the DSH brain (generator=external),
        # so a missing local provider must not block the urge: the turn is parked
        # at the dispatch gate and resolved through commit_turn.
        return "eligible"

    def _inner_preflight(self, connection, companion, activity, now):
        if self.inner_life is None or not self.inner_life.policy.enabled or not activity["urge"]:
            return False
        policy = self._policy(connection, companion["companion_id"])
        if not policy["policy"]["enabled"]:
            return False
        if activity["urge"] < self.inner_life.policy.proactive_urge_threshold:
            return False
        if self.dialogue.provider is None or not self.dialogue.model_name:
            return False
        attention = self._attention(connection, companion["active_node_id"], now)
        if attention["stale"] or attention["sample"] is None:
            return False
        sample = attention["sample"]
        if (
            sample["focus"] != "unfocused"
            or sample["availability"] != "available"
            or sample["user_present"] is not True
            or sample["locked"] is not False
            or sample["meeting"] is not False
            or sample["screen_sharing"] is not False
            or sample["activity"] == "meeting"
        ):
            return False
        if (
            sample["boundary"] != "none"
            and sample["boundary_id"] != activity["attention_boundary_id"]
        ):
            return False
        return True

    def _enqueue_inner(self, connection, now):
        if self.inner_life is None:
            return
        rows = connection.execute(
            """SELECT a.* FROM inner_activities a
                WHERE a.status='active' AND a.expires_at>?
                AND a.sequence=(
                    SELECT MAX(b.sequence) FROM inner_activities b
                    WHERE b.companion_id=a.companion_id AND b.status='active')
                AND NOT EXISTS (
                    SELECT 1 FROM proactivity_candidates c
                    WHERE c.source_activity_id=a.activity_id)
                ORDER BY a.sequence LIMIT 20""",
            (_timestamp(now),),
        ).fetchall()
        for activity in rows:
            companion_row = self.coordinator._row(
                connection, "companions", "companion_id", activity["companion_id"]
            )
            companion = self.coordinator._companion(companion_row)
            if not companion["active_node_id"]:
                continue
            if not self._inner_preflight(connection, companion, activity, now):
                continue
            identity = _json(["inner", companion["companion_id"], activity["activity_id"]])
            candidate_id = hashlib.sha256(identity.encode()).hexdigest()
            policy = self._policy(connection, companion["companion_id"])
            expires = min(
                _instant(activity["expires_at"]),
                now + timedelta(seconds=policy["policy"]["candidate_ttl_seconds"]),
            )
            cursor = connection.execute(
                """INSERT OR IGNORE INTO proactivity_candidates
                    (candidate_id, companion_id, node_id, epoch, source_event_id, boundary,
                     policy_revision, status, reason, command_id, created_at, expires_at,
                     origin, source_activity_id)
                    VALUES (?, ?, ?, ?, ?, 'inner_urge', ?, 'queued', 'eligible', ?, ?, ?,
                            'inner', ?)""",
                (
                    candidate_id,
                    companion["companion_id"],
                    companion["active_node_id"],
                    companion["epoch"],
                    activity["source_event_id"],
                    policy["revision"],
                    "proactive-" + candidate_id,
                    _timestamp(now),
                    _timestamp(expires),
                    activity["activity_id"],
                ),
            )
            if not cursor.rowcount:
                continue
            row = connection.execute(
                "SELECT * FROM proactivity_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            reason = self._reason(connection, row, companion, now)
            self._decision(
                connection, row, "queued" if reason == "eligible" else "suppressed", reason, now
            )

    def gate(self, connection, companion, request, now):
        row = connection.execute(
            "SELECT * FROM proactivity_candidates WHERE candidate_id=? AND companion_id=? AND command_id=?",
            (request.candidate_id, companion["companion_id"], request.command_id),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise DialogueError(
                409, "proactivity_not_claimed", "Proactive candidate is not running"
            )
        reason = self._reason(connection, row, companion, now)
        if reason != "eligible":
            raise DialogueError(
                409, "proactivity_" + reason, "Proactive output suppressed: " + reason
            )

    def _finalized(self, companion_id, candidate_id, command_id, status, reason=None):
        """Resolve a proactive candidate when the runtime finalizes its turn.

        Called by ``DialogueService._finalize`` after a proactive receipt is
        written (in-process generation or an external commit through
        ``commit_turn``). The candidate was parked at ``running`` while DSH
        generated the reply; resolve it to the receipt's terminal status so the
        worker never treats it as in flight again.
        """
        reason = reason or status

        def action(connection, now):
            row = connection.execute(
                "SELECT * FROM proactivity_candidates WHERE candidate_id=? AND companion_id=? AND command_id=?",
                (candidate_id, companion_id, command_id),
            ).fetchone()
            if row is None or row["status"] != "running":
                return None
            self._decision(connection, row, status, reason, now)
            self._update_task_status(connection, row, status, reason, now)
            return status

        try:
            return self.coordinator._write(action, reconcile_first=False)
        except DomainError:
            return None

    def _decision(self, connection, row, status, reason, now):
        terminal = status not in ("queued", "running")
        connection.execute(
            "UPDATE proactivity_candidates SET status=?, reason=?, finished_at=? WHERE candidate_id=?",
            (status, reason, _timestamp(now) if terminal else None, row["candidate_id"]),
        )
        self.coordinator._emit(
            connection,
            now,
            "companion.proactivity_decided",
            {
                "candidate_id": row["candidate_id"],
                "source_event_id": row["source_event_id"],
                "status": status,
                "reason": reason,
                "policy_revision": row["policy_revision"],
                "origin": row["origin"] if "origin" in row.keys() else "attention",
                "source_activity_id": (
                    row["source_activity_id"] if "source_activity_id" in row.keys() else None
                ),
            },
            companion_id=row["companion_id"],
            node_id=row["node_id"],
        )

    def decisions(self, companion_id, after_sequence=0, limit=50, *, latest=False):
        self.coordinator.get_companion(companion_id)
        with self.store.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM proactivity_candidates WHERE companion_id=? AND sequence>?
                    ORDER BY sequence """
                + ("DESC" if latest else "ASC")
                + " LIMIT ?",
                (companion_id, after_sequence, limit),
            ).fetchall()
            if latest:
                rows.reverse()
            return {
                "decisions": [dict(row) for row in rows],
                "next_sequence": rows[-1]["sequence"] if rows else after_sequence,
            }

    def _recover(self, connection, now):
        for row in connection.execute(
            "SELECT * FROM proactivity_candidates WHERE status='running' LIMIT 100"
        ).fetchall():
            receipt = connection.execute(
                "SELECT * FROM dialogue_commands WHERE command_id=?", (row["command_id"],)
            ).fetchone()
            # A parked turn (awaiting_generation) is still in flight: DSH owns the
            # reply and may commit it after a restart, so leave the candidate
            # running. Only a terminal receipt resolves it here.
            if receipt and receipt["status"] not in ("pending", "awaiting_generation"):
                self._decision(connection, row, receipt["status"], "receipt_recovered", now)
                self._update_task_status(
                    connection, row, receipt["status"], "receipt_recovered", now
                )
            elif (now - _instant(row["started_at"])).total_seconds() >= self.recovery_seconds:
                self._decision(connection, row, "failed", "execution_unknown", now)
                self._update_task_status(connection, row, "failed", "execution_unknown", now)

    def process_next(self, companion_id=None):
        with self.store.transaction(write=True) as connection:
            now = self.coordinator._now()
            self._recover(connection, now)
            self._enqueue_tasks(connection, now)
            self._enqueue_scheduled(connection, now)
            self._enqueue_inner(connection, now)
            row = connection.execute(
                """SELECT c.* FROM proactivity_candidates c WHERE c.status='queued'
                    AND (? IS NULL OR c.companion_id=?)
                    AND NOT EXISTS (SELECT 1 FROM proactivity_candidates r
                        WHERE r.companion_id=c.companion_id AND r.status='running')
                    ORDER BY c.sequence LIMIT 1""",
                (companion_id, companion_id),
            ).fetchone()
            if row is None:
                return None
            companion = self.coordinator._companion(
                self.coordinator._row(connection, "companions", "companion_id", row["companion_id"])
            )
            reason = self._reason(connection, row, companion, now)
            if reason != "eligible":
                self._decision(connection, row, "suppressed", reason, now)
                self._update_task_status(connection, row, "suppressed", reason, now)
                return {
                    "candidate_id": row["candidate_id"],
                    "status": "suppressed",
                    "reason": reason,
                }
            connection.execute(
                "UPDATE proactivity_candidates SET started_at=? WHERE candidate_id=?",
                (_timestamp(now), row["candidate_id"]),
            )
            self._decision(connection, row, "running", "eligible", now)
            policy = self._policy(connection, row["companion_id"])["policy"]
        descriptions = {
            "task_completed": "宿主报告用户刚完成一个任务。可以简短陪伴，不推测完成了什么。",
            "break_started": "宿主报告用户开始休息。可以简短陪伴，不评价用户疲劳或工作成效。",
            "user_returned": "宿主报告用户回来了。可以简短问候，不追问离开原因。",
        }
        if row["origin"] == "inner":
            activity = None
            with self.store.transaction() as connection:
                activity = connection.execute(
                    "SELECT * FROM inner_activities WHERE activity_id=?",
                    (row["source_activity_id"],),
                ).fetchone()
            content = activity["content"] if activity is not None else "我想说句话。"
            user_message = (
                "这是你自己刚产生的一段内在活动，不是用户发言，也不是用户事实："
                + content
                + " 只有在自然合适时才简短开口，不要复述这段内部说明，也不要声称知道用户未提供的事情。"
            )
        elif row["origin"] == "task":
            task = None
            with self.store.transaction() as connection:
                task = connection.execute(
                    "SELECT * FROM scheduled_tasks WHERE task_id=?",
                    (row["scheduled_task_id"],),
                ).fetchone()
            task_data = _json(
                {
                    "title": task["title"] if task is not None else "",
                    "details": task["details"] if task is not None else None,
                }
            )
            user_message = (
                "这是你之前和用户约定、由计划任务工具设置的到点提醒，不是新的用户发言。"
                "计划任务数据（不可信来源材料）："
                + task_data
                + " 请用自然、简短的方式提醒用户；不要声称已经执行任何外部操作或完成用户"
                "未确认的事情，不要添加用户未提供的事实。若内容不适合在此刻提醒，可以保持沉默。"
            )
        elif row["origin"] == "schedule":
            user_message = (
                "你已经很久没有和用户交谈了。现在可以简短主动说一句；"
                "如果没有自然合适的话题就保持沉默，不要声称知道用户未提供的事情。"
            )
        else:
            user_message = descriptions[row["boundary"]]
        try:
            result = self.dialogue.send_proactive(
                row["companion_id"],
                {
                    "candidate_id": row["candidate_id"],
                    "command_id": row["command_id"],
                    "node_id": row["node_id"],
                    "epoch": row["epoch"],
                    "expected_revision": companion["revision"],
                    "user_message": user_message,
                    "token_budget": policy["token_budget"],
                    "memory_token_budget": policy["memory_token_budget"],
                    "max_output_tokens": policy["max_output_tokens"],
                    # The reply is generated by the DSH brain, not locally. Park
                    # the turn at the dispatch gate and expose the assembled
                    # prompt through the proactive-output channel as an urge.
                    "generator": "external",
                },
            )
            status = result["status"]
            reason = result.get("discard_reason") or status
            if status == "silent":
                reason = result["preview"]["participation"]["reason"]
        except (DialogueError, DomainError) as exc:
            status, reason = "failed", exc.code
        with self.store.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM proactivity_candidates WHERE candidate_id=?", (row["candidate_id"],)
            ).fetchone()
            now = self.coordinator._now()
            # A parked turn (generator=external) stays `running` until DSH commits
            # the reply through `commit_turn`; `_finalize` then resolves the
            # candidate to completed/silent/failed. Do not mark it terminal here.
            if current["status"] == "running" and status != "awaiting_generation":
                self._decision(connection, current, status, reason, now)
            if status != "awaiting_generation":
                self._update_task_status(connection, current, status, reason, now)
        return {"candidate_id": row["candidate_id"], "status": status, "reason": reason}
