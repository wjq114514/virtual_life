"""Host-advisory focus episodes; sampling is transactional and reads never write."""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .coordinator import _identifier, _instant, _json, _timestamp
from .models import Identifier


class FocusSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: Identifier
    session_id: Identifier
    started_at: str
    ended_at: str
    duration_seconds: int = Field(ge=0)
    reason: str


class FocusSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: Identifier
    state: Literal["unknown", "unfocused", "candidate", "focused", "releasing"] = "unknown"
    source: Literal["host_advisory"] = "host_advisory"
    session_id: Identifier | None = None
    started_at: str | None = None
    confirmed_at: str | None = None
    release_started_at: str | None = None
    last_sampled_at: str | None = None
    expires_at: str | None = None
    stale: bool = True
    protection_active: bool = False
    duration_seconds: int = Field(default=0, ge=0)
    completed_count: int = Field(default=0, ge=0)
    recent_segments: list[FocusSegment] = Field(default_factory=list)


class FocusService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        with self.store.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS focus_states (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
                    state_json TEXT NOT NULL, completed_count INTEGER NOT NULL DEFAULT 0)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS focus_segments (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL REFERENCES nodes(node_id), segment_json TEXT NOT NULL)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS focus_recent ON focus_segments(node_id, sequence DESC)"
            )

    @staticmethod
    def _row(connection, node_id):
        return connection.execute(
            "SELECT * FROM focus_states WHERE node_id=?", (node_id,)
        ).fetchone()

    @staticmethod
    def _safe(sample):
        return (
            sample["focus"] in ("focused", "unfocused")
            and sample["availability"] == "available"
            and sample["user_present"] is True
            and sample["locked"] is False
            and sample["meeting"] is False
            and sample["screen_sharing"] is False
            and sample["activity"] != "meeting"
        )

    @staticmethod
    def _duration(state, until):
        if state.get("started_at") is None:
            return 0
        end = min(until, _instant(state["expires_at"]))
        if state.get("release_started_at"):
            end = min(end, _instant(state["release_started_at"]))
        return max(0, int((end - _instant(state["started_at"])).total_seconds()))

    def _finish(self, connection, node_id, state, until, reason):
        if state.get("confirmed_at") is None:
            return 0
        end = min(until, _instant(state["expires_at"]))
        if state.get("release_started_at"):
            end = min(end, _instant(state["release_started_at"]))
        segment = FocusSegment(
            node_id=node_id,
            session_id=state["session_id"],
            started_at=state["started_at"],
            ended_at=_timestamp(end),
            duration_seconds=self._duration(state, end),
            reason=reason,
        ).model_dump(mode="json")
        connection.execute(
            "INSERT INTO focus_segments(node_id,segment_json) VALUES (?, ?)",
            (node_id, _json(segment)),
        )
        connection.execute(
            """DELETE FROM focus_segments WHERE node_id=? AND sequence NOT IN
            (SELECT sequence FROM focus_segments WHERE node_id=? ORDER BY sequence DESC LIMIT 100)""",
            (node_id, node_id),
        )
        return 1

    def observe(self, connection, node_id, sample, expires_at, now):
        """Consume only an already accepted attention sample in its write transaction."""
        row = self._row(connection, node_id)
        state = json.loads(row["state_json"]) if row else {}
        completed = row["completed_count"] if row else 0
        sampled = min(_instant(sample["sampled_at"]), now)
        expiry = _instant(expires_at) if isinstance(expires_at, str) else expires_at
        if state:
            interrupted = (
                "sample_gap"
                if sampled > _instant(state["expires_at"])
                else "session_changed"
                if state["session_id"] != sample["session_id"]
                else None
            )
            if interrupted:
                completed += self._finish(connection, node_id, state, sampled, interrupted)
                state = {}
        safe = self._safe(sample)
        if not safe:
            completed += self._finish(connection, node_id, state, sampled, "attention_invalid")
            state = {"state": "unknown"}
        elif sample["focus"] == "focused":
            if state.get("started_at") is None:
                state = {
                    "state": "candidate",
                    "started_at": _timestamp(sampled),
                    "focused_since": _timestamp(sampled),
                }
            elif state.get("release_started_at"):
                state["release_started_at"] = None
                state["focused_since"] = _timestamp(sampled)
            state["state"] = "focused" if state.get("confirmed_at") else "candidate"
            if (
                state.get("confirmed_at") is None
                and (sampled - _instant(state["focused_since"])).total_seconds() >= 60
            ):
                state["confirmed_at"] = _timestamp(sampled)
                state["state"] = "focused"
        elif state.get("started_at"):
            explicit = sample["boundary"] in ("task_completed", "break_started")
            if explicit:
                completed += self._finish(connection, node_id, state, sampled, "explicit_boundary")
                state = {"state": "unfocused"}
            else:
                state.setdefault("release_started_at", _timestamp(sampled))
                if state["release_started_at"] is None:
                    state["release_started_at"] = _timestamp(sampled)
                state["state"] = "releasing"
                if (sampled - _instant(state["release_started_at"])).total_seconds() >= 30:
                    completed += self._finish(connection, node_id, state, sampled, "unfocused")
                    state = {"state": "unfocused"}
        else:
            state = {"state": "unfocused"}
        state.update(
            session_id=sample["session_id"],
            last_sampled_at=sample["sampled_at"],
            expires_at=_timestamp(expiry),
        )
        connection.execute(
            """INSERT INTO focus_states VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET state_json=excluded.state_json,
            completed_count=excluded.completed_count""",
            (node_id, _json(state), completed),
        )

    def snapshot(self, connection, node_id, now: datetime):
        row = self._row(connection, node_id)
        state = json.loads(row["state_json"]) if row else {}
        stale = not state or _instant(state["expires_at"]) <= now
        recent = [
            json.loads(item["segment_json"])
            for item in connection.execute(
                "SELECT segment_json FROM focus_segments WHERE node_id=? ORDER BY sequence DESC LIMIT 20",
                (node_id,),
            )
        ]
        return FocusSnapshot(
            node_id=node_id,
            state="unknown" if stale else state["state"],
            session_id=state.get("session_id"),
            started_at=state.get("started_at"),
            confirmed_at=state.get("confirmed_at"),
            release_started_at=state.get("release_started_at"),
            last_sampled_at=state.get("last_sampled_at"),
            expires_at=state.get("expires_at"),
            stale=stale,
            protection_active=not stale and state["state"] in ("candidate", "focused", "releasing"),
            duration_seconds=self._duration(state, now),
            completed_count=row["completed_count"] if row else 0,
            recent_segments=recent,
        ).model_dump(mode="json")

    def get(self, node_id):
        node_id = _identifier(node_id)
        with self.store.transaction() as connection:
            self.coordinator._row(connection, "nodes", "node_id", node_id)
            return self.snapshot(connection, node_id, self.coordinator._now())
