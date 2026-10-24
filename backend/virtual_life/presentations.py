"""Durable native terminal presentation sessions.

A presentation records that a native renderer (the kitty TUI) is alive for a
node and is the single place where proactive native output may be shown. It is
intentionally separate from Web access sessions: opening one entry point must
not silently authorize the other. Presentation TTL never refreshes the node
heartbeat and never grants companion ownership or an output lease.
"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .coordinator import DomainError, _identifier, _instant, _timestamp
from .models import Identifier, RequestModel

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS native_presentations (
        presentation_id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL REFERENCES nodes(node_id),
        kind TEXT NOT NULL,
        opened_sequence INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        released INTEGER NOT NULL DEFAULT 0)""",
    """CREATE INDEX IF NOT EXISTS native_presentation_node
        ON native_presentations(node_id, released, expires_at)""",
)

MAX_VERSION = 2**63 - 1


PresentationKind = Literal["terminal", "android"]


class PresentationOpen(RequestModel):
    presentation_id: Identifier
    kind: PresentationKind = "terminal"


class PresentationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: Identifier
    presentation_id: Identifier
    kind: PresentationKind
    expires_at: str
    ttl_seconds: float
    server_time: str
    opened_sequence: int = Field(ge=0)


class NativeOutputItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sequence: int = Field(ge=0)
    event_id: Identifier
    command_id: Identifier
    assistant_message: str
    created_at: str


class NativeOutputPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    companion_id: Identifier
    node_id: Identifier
    presentation_id: Identifier
    epoch: int = Field(ge=0, le=MAX_VERSION)
    companion_revision: int = Field(ge=0, le=MAX_VERSION)
    items: list[NativeOutputItem]
    next_sequence: int = Field(ge=0, le=MAX_VERSION)


class PresentationService:
    """Owns one active terminal presentation per native node."""

    def __init__(self, coordinator, ttl_seconds: float = 15) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
        ):
            raise ValueError("Presentation TTL must be a finite positive number")
        self.coordinator = coordinator
        self.store = coordinator.store
        self.ttl_seconds = float(ttl_seconds)
        with self.store.transaction(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)
        # Registered after construction so DeviceService can reject a Web
        # session whose node already has a live native presentation.
        coordinator.presentations = self

    @staticmethod
    def _live(row, now: datetime) -> bool:
        return not row["released"] and _instant(row["expires_at"]) > now

    def _snapshot(self, node_id, presentation_id, kind, expires_at, opened_sequence, now):
        return {
            "node_id": node_id,
            "presentation_id": presentation_id,
            "kind": kind,
            "expires_at": expires_at,
            "ttl_seconds": self.ttl_seconds,
            "server_time": _timestamp(now),
            "opened_sequence": opened_sequence,
        }

    def _live_presentations(self, connection, node_id: str, now: datetime):
        return connection.execute(
            """SELECT * FROM native_presentations WHERE node_id=? AND released=0
            AND expires_at>? ORDER BY created_at""",
            (node_id, _timestamp(now)),
        ).fetchall()

    def assert_web_allowed(self, connection, node_id: str, now: datetime) -> None:
        """Reject a Web access while a native presentation is live for the node."""
        if self._live_presentations(connection, node_id, now):
            raise DomainError(
                409,
                "presentation_busy",
                "A native presentation is active on this device; close it first",
            )

    def open(self, node_id: str, payload: dict):
        node_id = _identifier(node_id)
        data = PresentationOpen.model_validate(payload).model_dump(mode="json")

        def action(connection, now):
            node = self.coordinator._node(
                self.coordinator._row(connection, "nodes", "node_id", node_id), now, connection
            )
            if not node["online"] or "text.display" not in node["capabilities"]:
                raise DomainError(
                    409,
                    "output_not_permitted",
                    "The node must be online with a text display capability",
                )
            busy = connection.execute(
                """SELECT 1 FROM device_web_accesses WHERE node_id=? AND released=0
                AND expires_at>? LIMIT 1""",
                (node_id, _timestamp(now)),
            ).fetchone()
            if busy is not None:
                raise DomainError(
                    409,
                    "presentation_busy",
                    "A Web access is active on this device; close it before attaching",
                )
            # A newer renderer explicitly supersedes any earlier live one.
            connection.execute(
                "UPDATE native_presentations SET released=1 WHERE node_id=? AND released=0",
                (node_id,),
            )
            opened_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events"
            ).fetchone()[0]
            expires_at = _timestamp(now + timedelta(seconds=self.ttl_seconds))
            connection.execute(
                """INSERT INTO native_presentations
                (presentation_id,node_id,kind,opened_sequence,created_at,last_seen_at,expires_at,released)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    data["presentation_id"],
                    node_id,
                    data["kind"],
                    opened_sequence,
                    _timestamp(now),
                    _timestamp(now),
                    expires_at,
                ),
            )
            return self._snapshot(
                node_id, data["presentation_id"], data["kind"], expires_at, opened_sequence, now
            )

        return self.coordinator._write(action, reconcile_first=False)

    def heartbeat(self, node_id: str, presentation_id: str):
        node_id, presentation_id = _identifier(node_id), _identifier(presentation_id)

        def action(connection, now):
            row = connection.execute(
                "SELECT * FROM native_presentations WHERE node_id=? AND presentation_id=?",
                (node_id, presentation_id),
            ).fetchone()
            if row is None or not self._live(row, now):
                raise DomainError(
                    409,
                    "presentation_expired",
                    "The native presentation is released or expired",
                )
            expires_at = _timestamp(now + timedelta(seconds=self.ttl_seconds))
            connection.execute(
                "UPDATE native_presentations SET last_seen_at=?, expires_at=? WHERE presentation_id=?",
                (_timestamp(now), expires_at, presentation_id),
            )
            return self._snapshot(
                node_id, presentation_id, row["kind"], expires_at, row["opened_sequence"], now
            )

        return self.coordinator._write(action, reconcile_first=False)

    def release(self, node_id: str, presentation_id: str):
        node_id, presentation_id = _identifier(node_id), _identifier(presentation_id)

        def action(connection, now):
            row = connection.execute(
                "SELECT * FROM native_presentations WHERE node_id=? AND presentation_id=?",
                (node_id, presentation_id),
            ).fetchone()
            if row is None:
                raise DomainError(404, "not_found", "The native presentation was not found")
            connection.execute(
                "UPDATE native_presentations SET released=1 WHERE presentation_id=?",
                (presentation_id,),
            )
            return self._snapshot(
                node_id,
                presentation_id,
                row["kind"],
                row["expires_at"],
                row["opened_sequence"],
                now,
            )

        return self.coordinator._write(action, reconcile_first=False)

    def validate(self, connection, node_id: str, presentation_id: str, now: datetime):
        """Return a live presentation row; raise if released, expired or mismatched."""
        row = connection.execute(
            "SELECT * FROM native_presentations WHERE node_id=? AND presentation_id=?",
            (node_id, presentation_id),
        ).fetchone()
        if row is None or not self._live(row, now):
            raise DomainError(
                409, "presentation_expired", "The native presentation is no longer valid"
            )
        return row

    def expire(self, connection, node_id: str, presentation_id: str, now: datetime):
        """Mark one presentation released (used when a bound transfer is abandoned)."""
        connection.execute(
            "UPDATE native_presentations SET released=1 WHERE node_id=? AND presentation_id=? AND released=0",
            (node_id, presentation_id),
        )
