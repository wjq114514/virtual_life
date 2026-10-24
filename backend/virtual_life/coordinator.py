"""Durable node ownership, companion state, and two-party travel coordination."""

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from .models import (
    CompanionCreate,
    ContextUpdate,
    Identifier,
    LeaseRenew,
    NodeHeartbeat,
    NodeRegister,
    ObservationCreate,
    TransferAck,
    TransferPrepare,
    bounded_json,
)
from .storage import SQLiteStore

PROTOCOL_VERSION = "1.0"
OUTPUT_CAPABILITIES = frozenset({"avatar.render", "text.display", "audio.output"})
MAX_VERSION = 2**63 - 1
Request = TypeVar("Request", bound=BaseModel)
JsonDict = dict[str, Any]
Mutation = Callable[[sqlite3.Connection, datetime], JsonDict]
RoutingPolicy = Callable[[JsonDict | None, JsonDict], tuple[bool, str]]


def default_routing_policy(source: JsonDict | None, target: JsonDict) -> tuple[bool, str]:
    if not target["online"]:
        return False, "target_offline"
    if not OUTPUT_CAPABILITIES.intersection(target["capabilities"]):
        return False, "target_no_output"
    if source is not None and not source["online"]:
        return True, "source_offline_wait_for_lease"
    return True, "available"


class DomainError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _validated(model: type[Request], payload: JsonDict) -> Request:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(item) for item in error["loc"]) or "payload"
        raise DomainError(422, "validation_error", f"{location}: {error['msg']}") from exc


def _identifier(value: str) -> str:
    try:
        return TypeAdapter(Identifier).validate_python(value, strict=True)
    except ValidationError as exc:
        raise DomainError(
            422, "validation_error", "Identifiers must be nonempty strings up to 128 characters"
        ) from exc


class Coordinator:
    """All mutations, their events, and accepted command responses commit together."""

    def __init__(
        self,
        database_path: str | Path,
        node_ttl_seconds: float = 60,
        lease_ttl_seconds: float = 10,
        transfer_ttl_seconds: float = 30,
        clock: Callable[[], datetime] | None = None,
        routing_policy: RoutingPolicy | None = None,
    ) -> None:
        for name, value in (
            ("node_ttl_seconds", node_ttl_seconds),
            ("lease_ttl_seconds", lease_ttl_seconds),
            ("transfer_ttl_seconds", transfer_ttl_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        self.node_ttl_seconds = float(node_ttl_seconds)
        self.lease_ttl_seconds = float(lease_ttl_seconds)
        self.transfer_ttl_seconds = float(transfer_ttl_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.routing_policy = routing_policy or default_routing_policy
        self.store = SQLiteStore(database_path)
        with self.store.transaction(write=True) as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(transfers)")}
            if "target_presentation_id" not in columns:
                connection.execute("ALTER TABLE transfers ADD COLUMN target_presentation_id TEXT")
        from .devices import DeviceService

        self.devices = DeviceService(self)
        self.reconcile()

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime):
            raise TypeError("The runtime clock must return a UTC datetime")
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    def _write(
        self,
        action: Mutation,
        *,
        operation: str | None = None,
        subject: str | None = None,
        command: JsonDict | None = None,
        reconcile_first: bool = True,
    ) -> JsonDict:
        failure: DomainError | None = None
        result: JsonDict | None = None
        fingerprint = None
        if command is not None:
            fingerprint = hashlib.sha256(
                _json({"operation": operation, "subject": subject, "payload": command}).encode(
                    "utf-8"
                )
            ).hexdigest()
        with self.store.transaction(write=True) as connection:
            from .node_credentials import guard_mutation, record_reported

            guard_mutation(connection)
            if command is not None:
                previous = connection.execute(
                    "SELECT fingerprint, result_json FROM runtime_commands WHERE command_id = ?",
                    (command["command_id"],),
                ).fetchone()
                if previous is not None:
                    if previous["fingerprint"] != fingerprint:
                        raise DomainError(
                            409,
                            "idempotency_conflict",
                            "command_id has already been used for another request",
                        )
                    return json.loads(previous["result_json"])
            now = self._now()
            if reconcile_first:
                self._reconcile(connection, now)
            try:
                result = action(connection, now)
            except DomainError as exc:
                # Safe maintenance still commits when the requested action is rejected.
                # Actions perform all domain validation before changing their own state.
                failure = exc
            else:
                record_reported(connection)
                if command is not None:
                    connection.execute(
                        "INSERT INTO runtime_commands(command_id, fingerprint, operation, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            command["command_id"],
                            fingerprint,
                            operation,
                            _json(result),
                            _timestamp(now),
                        ),
                    )
        if failure is not None:
            raise failure
        assert result is not None
        return result

    @staticmethod
    def _row(connection: sqlite3.Connection, table: str, key: str, value: str) -> sqlite3.Row:
        row = connection.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,)).fetchone()
        if row is None:
            raise DomainError(404, "not_found", f"{table.rstrip('s')} {value!r} was not found")
        return row

    def _node(self, row: sqlite3.Row, now: datetime, connection: sqlite3.Connection) -> JsonDict:
        snapshot = {
            "node_id": row["node_id"],
            "label": row["label"],
            "kind": row["kind"],
            "capabilities": json.loads(row["capabilities_json"]),
            "context": json.loads(row["context_json"]),
            "online": now
            < _instant(row["last_seen_at"]) + timedelta(seconds=self.node_ttl_seconds),
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
        }

        return self.devices.project(connection, row, now, snapshot)

    @staticmethod
    def _companion(row: sqlite3.Row) -> JsonDict:
        return {
            "companion_id": row["companion_id"],
            "name": row["name"],
            "revision": row["revision"],
            "active_node_id": row["active_node_id"],
            "epoch": row["epoch"],
            "lease_expires_at": row["lease_expires_at"],
            "activity": json.loads(row["activity_json"]),
            "interaction_mode": row["interaction_mode"],
            "context": json.loads(row["context_json"]),
            "memory_refs": json.loads(row["memory_refs_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _transfer(row: sqlite3.Row) -> JsonDict:
        return {
            "transfer_id": row["transfer_id"],
            "companion_id": row["companion_id"],
            "source_node_id": row["source_node_id"],
            "target_node_id": row["target_node_id"],
            "status": row["status"],
            "source_released": bool(row["source_released"]),
            "target_ready": bool(row["target_ready"]),
            "base_revision": row["base_revision"],
            "snapshot": json.loads(row["snapshot_json"]),
            "reason": row["reason"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "committed_revision": row["committed_revision"],
            "target_presentation_id": row["target_presentation_id"],
        }

    @staticmethod
    def _event(row: sqlite3.Row) -> JsonDict:
        return {
            "event_id": row["event_id"],
            "sequence": row["sequence"],
            "protocol_version": row["protocol_version"],
            "type": row["type"],
            "companion_id": row["companion_id"],
            "node_id": row["node_id"],
            "recipient_node_id": row["recipient_node_id"],
            "payload": json.loads(row["payload_json"]),
            "occurred_at": row["occurred_at"],
        }

    def _emit(
        self,
        connection: sqlite3.Connection,
        now: datetime,
        event_type: str,
        payload: JsonDict,
        *,
        companion_id: str | None = None,
        node_id: str | None = None,
        recipient_node_id: str | None = None,
    ) -> JsonDict:
        event_id = str(uuid.uuid4())
        cursor = connection.execute(
            "INSERT INTO events(event_id, protocol_version, type, companion_id, node_id, recipient_node_id, payload_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                PROTOCOL_VERSION,
                event_type,
                companion_id,
                node_id,
                recipient_node_id,
                _json(payload),
                _timestamp(now),
            ),
        )
        return self._event(
            connection.execute(
                "SELECT * FROM events WHERE sequence = ?", (cursor.lastrowid,)
            ).fetchone()
        )

    def register_node(self, payload: JsonDict, *, peer_ip: str | None = None) -> JsonDict:
        data = _validated(NodeRegister, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            self.devices.check_registration(connection, data)
            # A late reconnect cannot reuse readiness from an expired node session.
            # Defer commits until the incoming capabilities are also applied.
            self._reconcile(connection, now, allow_commits=False)
            timestamp = _timestamp(now)
            connection.execute(
                """INSERT INTO nodes(node_id, label, kind, capabilities_json, context_json, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, '{}', ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET label=excluded.label, kind=excluded.kind,
                    capabilities_json=excluded.capabilities_json, last_seen_at=excluded.last_seen_at""",
                (
                    data["node_id"],
                    data["label"],
                    data["kind"],
                    _json(data["capabilities"]),
                    timestamp,
                    timestamp,
                ),
            )
            self.devices.record_registration(connection, data, now, peer_ip)
            result = self._node(
                self._row(connection, "nodes", "node_id", data["node_id"]), now, connection
            )
            self._emit(
                connection, now, "node.registered", {"node": result}, node_id=data["node_id"]
            )
            self._reconcile(connection, now)
            return result

        return self._write(action, reconcile_first=False)

    def list_nodes(self) -> list[JsonDict]:
        with self.store.transaction() as connection:
            now = self._now()
            return [
                self._node(row, now, connection)
                for row in connection.execute("SELECT * FROM nodes ORDER BY node_id")
            ]

    def get_node(self, node_id: str) -> JsonDict:
        node_id = _identifier(node_id)
        with self.store.transaction() as connection:
            return self._node(
                self._row(connection, "nodes", "node_id", node_id), self._now(), connection
            )

    def heartbeat(self, node_id: str, payload: JsonDict, *, peer_ip: str | None = None) -> JsonDict:
        node_id = _identifier(node_id)
        data = _validated(NodeHeartbeat, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            self._row(connection, "nodes", "node_id", node_id)
            self._reconcile(connection, now, allow_commits=False)
            connection.execute(
                "UPDATE nodes SET context_json = ?, last_seen_at = ? WHERE node_id = ?",
                (_json(data["context"]), _timestamp(now), node_id),
            )
            self.devices.record_native_ip(connection, node_id, now, peer_ip)
            self._reconcile(connection, now)
            return self._node(self._row(connection, "nodes", "node_id", node_id), now, connection)

        return self._write(action, reconcile_first=False)

    def graph(self) -> JsonDict:
        nodes = self.list_nodes()
        edges = []
        for source in nodes:
            for target in nodes:
                if source["node_id"] == target["node_id"]:
                    continue
                available, reason = self._evaluate_route(source, target)
                edges.append(
                    {
                        "source_node_id": source["node_id"],
                        "target_node_id": target["node_id"],
                        "available": available,
                        "reason": reason,
                    }
                )
        return {"nodes": nodes, "edges": edges}

    def create_companion(self, payload: JsonDict) -> JsonDict:
        data = _validated(CompanionCreate, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            if (
                connection.execute(
                    "SELECT 1 FROM companions WHERE companion_id = ?", (data["companion_id"],)
                ).fetchone()
                is not None
            ):
                raise DomainError(
                    409, "already_exists", "The companion identifier is already registered"
                )
            timestamp = _timestamp(now)
            connection.execute(
                """INSERT INTO companions(companion_id, name, revision, active_node_id, epoch, lease_expires_at,
                    activity_json, interaction_mode, context_json, memory_refs_json, created_at, updated_at)
                    VALUES (?, ?, 0, NULL, 0, NULL, '{"type":"idle"}', 'quiet', '{}', '[]', ?, ?)""",
                (data["companion_id"], data["name"], timestamp, timestamp),
            )
            result = self._companion(
                self._row(connection, "companions", "companion_id", data["companion_id"])
            )
            self._emit(
                connection,
                now,
                "companion.created",
                {"companion": result},
                companion_id=data["companion_id"],
            )
            return result

        return self._write(action)

    def list_companions(self) -> list[JsonDict]:
        with self.store.transaction() as connection:
            return [
                self._companion(row)
                for row in connection.execute("SELECT * FROM companions ORDER BY companion_id")
            ]

    def get_companion(self, companion_id: str) -> JsonDict:
        companion_id = _identifier(companion_id)
        with self.store.transaction() as connection:
            return self._companion(
                self._row(connection, "companions", "companion_id", companion_id)
            )

    @staticmethod
    def _check_revision(companion: JsonDict, expected_revision: int) -> None:
        if companion["revision"] != expected_revision:
            raise DomainError(
                409,
                "revision_conflict",
                "expected_revision does not match the current companion revision",
            )
        if companion["revision"] >= MAX_VERSION:
            raise DomainError(
                409, "version_exhausted", "The companion revision cannot be incremented"
            )

    @staticmethod
    def _check_owner(
        companion: JsonDict, node_id: str, epoch: int, now: datetime, *, require_lease: bool = True
    ) -> None:
        if companion["active_node_id"] != node_id:
            raise DomainError(409, "ownership_conflict", "Only the active node owns this companion")
        if companion["epoch"] != epoch:
            raise DomainError(409, "epoch_conflict", "The output epoch is stale")
        lease = companion["lease_expires_at"]
        if require_lease and (lease is None or _instant(lease) <= now):
            raise DomainError(409, "lease_expired", "The output lease is no longer valid")

    @staticmethod
    def _pending(connection: sqlite3.Connection, companion_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM transfers WHERE companion_id = ? AND status = 'prepared'",
            (companion_id,),
        ).fetchone()

    def _target_reason(
        self, connection: sqlite3.Connection, target_node_id: str, now: datetime
    ) -> str | None:
        row = connection.execute(
            "SELECT * FROM nodes WHERE node_id = ?", (target_node_id,)
        ).fetchone()
        if row is None:
            return "target_missing"
        node = self._node(row, now, connection)
        if not node["online"]:
            return "target_offline"
        if not OUTPUT_CAPABILITIES.intersection(node["capabilities"]):
            return "target_no_output"
        return None

    def _evaluate_route(self, source: JsonDict | None, target: JsonDict) -> tuple[bool, str]:
        # A policy can narrow topology; it cannot bypass output safety requirements.
        available, reason = default_routing_policy(source, target)
        return self.routing_policy(source, target) if available else (available, reason)

    def _travel_reason(
        self,
        connection: sqlite3.Connection,
        source_node_id: str | None,
        target_node_id: str,
        now: datetime,
    ) -> str | None:
        reason = self._target_reason(connection, target_node_id, now)
        if reason is not None:
            return reason
        target = self._node(
            self._row(connection, "nodes", "node_id", target_node_id), now, connection
        )
        source = (
            self._node(self._row(connection, "nodes", "node_id", source_node_id), now, connection)
            if source_node_id is not None
            else None
        )
        available, reason = self._evaluate_route(source, target)
        return None if available else reason

    def patch_context(self, companion_id: str, payload: JsonDict) -> JsonDict:
        companion_id = _identifier(companion_id)
        data = _validated(ContextUpdate, payload).model_dump(mode="json", exclude_unset=True)

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            companion = self._companion(
                self._row(connection, "companions", "companion_id", companion_id)
            )
            self._check_revision(companion, data["expected_revision"])
            self._check_owner(companion, data["node_id"], data["epoch"], now)
            if self._pending(connection, companion_id) is not None:
                raise DomainError(
                    409,
                    "transfer_in_progress",
                    "Companion context is frozen during prepared travel",
                )
            context = {**companion["context"], **data.get("context", {})}
            try:
                bounded_json(context)
            except ValueError as exc:
                raise DomainError(422, "validation_error", str(exc)) from exc
            connection.execute(
                """UPDATE companions SET revision = revision + 1, activity_json = ?, interaction_mode = ?,
                    context_json = ?, memory_refs_json = ?, updated_at = ? WHERE companion_id = ?""",
                (
                    _json(data.get("activity", companion["activity"])),
                    data.get("interaction_mode", companion["interaction_mode"]),
                    _json(context),
                    _json(data.get("memory_refs", companion["memory_refs"])),
                    _timestamp(now),
                    companion_id,
                ),
            )
            result = self._companion(
                self._row(connection, "companions", "companion_id", companion_id)
            )
            self._emit(
                connection,
                now,
                "companion.context_updated",
                {"companion": result},
                companion_id=companion_id,
                node_id=data["node_id"],
            )
            return result

        return self._write(action, operation="patch_context", subject=companion_id, command=data)

    def prepare_transfer(self, companion_id: str, payload: JsonDict) -> JsonDict:
        companion_id = _identifier(companion_id)
        data = _validated(TransferPrepare, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            companion = self._companion(
                self._row(connection, "companions", "companion_id", companion_id)
            )
            self._check_revision(companion, data["expected_revision"])
            if companion["epoch"] >= MAX_VERSION:
                raise DomainError(
                    409, "version_exhausted", "The companion epoch cannot be incremented"
                )
            self._row(connection, "nodes", "node_id", data["target_node_id"])
            if companion["active_node_id"] == data["target_node_id"]:
                raise DomainError(
                    409, "same_node", "The companion is already assigned to the requested node"
                )
            if self._pending(connection, companion_id) is not None:
                raise DomainError(
                    409,
                    "transfer_in_progress",
                    "A prepared transfer already exists for this companion",
                )
            reason = self._travel_reason(
                connection, companion["active_node_id"], data["target_node_id"], now
            )
            if reason is not None:
                raise DomainError(
                    409, reason, "The target must be online and provide an output capability"
                )
            transfer_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO transfers(transfer_id, companion_id, source_node_id, target_node_id, status,
                    source_released, target_ready, base_revision, snapshot_json, reason, created_at, expires_at, committed_revision)
                    VALUES (?, ?, ?, ?, 'prepared', 0, 0, ?, ?, ?, ?, ?, NULL)""",
                (
                    transfer_id,
                    companion_id,
                    companion["active_node_id"],
                    data["target_node_id"],
                    companion["revision"],
                    _json(companion),
                    data["reason"],
                    _timestamp(now),
                    _timestamp(now + timedelta(seconds=self.transfer_ttl_seconds)),
                ),
            )
            transfer = self._transfer(
                self._row(connection, "transfers", "transfer_id", transfer_id)
            )
            if transfer["source_node_id"] is not None:
                self._emit(
                    connection,
                    now,
                    "transfer.prepared",
                    {"transfer": transfer, "role": "source"},
                    companion_id=companion_id,
                    node_id=transfer["source_node_id"],
                    recipient_node_id=transfer["source_node_id"],
                )
            self._emit(
                connection,
                now,
                "transfer.prepared",
                {"transfer": transfer, "role": "target"},
                companion_id=companion_id,
                node_id=transfer["target_node_id"],
                recipient_node_id=transfer["target_node_id"],
            )
            return transfer

        return self._write(action, operation="prepare_transfer", subject=companion_id, command=data)

    def get_transfer(self, transfer_id: str) -> JsonDict:
        transfer_id = _identifier(transfer_id)
        with self.store.transaction() as connection:
            return self._transfer(self._row(connection, "transfers", "transfer_id", transfer_id))

    def ack_transfer(self, transfer_id: str, payload: JsonDict) -> JsonDict:
        transfer_id = _identifier(transfer_id)
        data = _validated(TransferAck, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            transfer = self._transfer(
                self._row(connection, "transfers", "transfer_id", transfer_id)
            )
            authorized_node = (
                transfer["target_node_id"]
                if data["phase"] == "ready"
                else transfer["source_node_id"]
            )
            if authorized_node is None or authorized_node != data["node_id"]:
                raise DomainError(
                    409,
                    "acknowledgment_conflict",
                    "ready must come from the target; released must come from the source",
                )
            if transfer["status"] != "prepared":
                return transfer
            if data["phase"] == "ready":
                presentation_id = data.get("presentation_id")
                if (
                    transfer["target_ready"]
                    and transfer["target_presentation_id"] != presentation_id
                ):
                    raise DomainError(
                        409,
                        "acknowledgment_conflict",
                        "The target already reported ready with another presentation",
                    )
                if presentation_id is not None:
                    presentations = getattr(self, "presentations", None)
                    if presentations is None:
                        raise DomainError(
                            409, "presentation_required", "Native presentations are not available"
                        )
                    # Bind only a live renderer session; a stale id cannot stand in
                    # for an actually initialized native target.
                    presentations.validate(
                        connection, transfer["target_node_id"], presentation_id, now
                    )
                connection.execute(
                    "UPDATE transfers SET target_ready = 1, target_presentation_id = ? WHERE transfer_id = ?",
                    (presentation_id, transfer_id),
                )
            elif not transfer["source_released"]:
                connection.execute(
                    "UPDATE transfers SET source_released = 1 WHERE transfer_id = ?", (transfer_id,)
                )
                # The host must stop output before this acknowledgment. Revoke the
                # stored lease too, so a failed destination needs a fresh renewal.
                connection.execute(
                    "UPDATE companions SET lease_expires_at = ? WHERE companion_id = ?",
                    (_timestamp(now), transfer["companion_id"]),
                )
            self._resolve_transfer(connection, transfer_id, now)
            return self._transfer(self._row(connection, "transfers", "transfer_id", transfer_id))

        return self._write(action)

    def abort_transfer(self, transfer_id: str) -> JsonDict:
        transfer_id = _identifier(transfer_id)

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            transfer = self._transfer(
                self._row(connection, "transfers", "transfer_id", transfer_id)
            )
            if transfer["status"] == "prepared":
                self._end_transfer(connection, transfer, "aborted", "manual", now)
            return self._transfer(self._row(connection, "transfers", "transfer_id", transfer_id))

        return self._write(action)

    def renew_lease(self, companion_id: str, payload: JsonDict) -> JsonDict:
        companion_id = _identifier(companion_id)
        data = _validated(LeaseRenew, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            companion = self._companion(
                self._row(connection, "companions", "companion_id", companion_id)
            )
            self._check_owner(companion, data["node_id"], data["epoch"], now, require_lease=False)
            if self._pending(connection, companion_id) is not None:
                raise DomainError(
                    409,
                    "transfer_in_progress",
                    "The source lease cannot be renewed during prepared travel",
                )
            reason = self._target_reason(connection, data["node_id"], now)
            if reason is not None:
                raise DomainError(
                    409,
                    "node_unavailable",
                    "The active node must be online and provide an output capability",
                )
            expires_at = _timestamp(now + timedelta(seconds=self.lease_ttl_seconds))
            connection.execute(
                "UPDATE companions SET lease_expires_at = ? WHERE companion_id = ?",
                (expires_at, companion_id),
            )
            return {
                "companion_id": companion_id,
                "node_id": data["node_id"],
                "epoch": companion["epoch"],
                "expires_at": expires_at,
                "ttl_seconds": self.lease_ttl_seconds,
                "server_time": _timestamp(now),
            }

        return self._write(action)

    def observe(self, node_id: str, payload: JsonDict) -> JsonDict:
        node_id = _identifier(node_id)
        data = _validated(ObservationCreate, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime) -> JsonDict:
            node = self._node(self._row(connection, "nodes", "node_id", node_id), now, connection)
            if data["modality"] == "vision":
                if "vision.capture" not in node["capabilities"]:
                    raise DomainError(
                        409, "capability_required", "Vision observations require vision.capture"
                    )
                if not data["user_initiated"]:
                    raise DomainError(
                        409,
                        "user_initiated_required",
                        "Vision observations require an explicit user action",
                    )
            return self._emit(
                connection,
                now,
                "node.observation",
                {
                    "modality": data["modality"],
                    "data": data["data"],
                    "user_initiated": data["user_initiated"],
                },
                node_id=node_id,
            )

        return self._write(action, operation="observe", subject=node_id, command=data)

    def events(
        self, after_sequence: int = 0, node_id: str | None = None, limit: int = 100
    ) -> JsonDict:
        if type(after_sequence) is not int or not 0 <= after_sequence <= MAX_VERSION:
            raise DomainError(
                422, "validation_error", "after_sequence must be a nonnegative 64-bit integer"
            )
        if type(limit) is not int or not 1 <= limit <= 500:
            raise DomainError(422, "validation_error", "limit must be an integer between 1 and 500")
        if node_id is not None:
            node_id = _identifier(node_id)
        with self.store.transaction() as connection:
            if node_id is not None:
                self._row(connection, "nodes", "node_id", node_id)
            rows = connection.execute(
                "SELECT * FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (after_sequence, limit),
            ).fetchall()
            visible = [
                self._event(row)
                for row in rows
                if node_id is None
                or row["recipient_node_id"] is None
                or row["recipient_node_id"] == node_id
            ]
            return {
                "events": visible,
                "next_sequence": rows[-1]["sequence"] if rows else after_sequence,
            }

    def connection_event(
        self,
        event_type: str,
        payload: JsonDict,
        *,
        node_id: str,
        companion_id: str | None = None,
        recipient_node_id: str | None = None,
    ) -> JsonDict:
        """Append a durable long-connection lifecycle event.

        WebSocket auth rejections and stream disconnections are connection logs,
        not command receipts; they commit through the same durable event store so
        the event trail stays the single source for connection diagnostics.
        """
        node_id = _identifier(node_id)
        with self.store.transaction(write=True) as connection:
            return self._emit(
                connection,
                self._now(),
                event_type,
                payload,
                companion_id=companion_id,
                node_id=node_id,
                recipient_node_id=recipient_node_id,
            )

    def _end_transfer(
        self,
        connection: sqlite3.Connection,
        transfer: JsonDict,
        status: str,
        reason: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "UPDATE transfers SET status = ? WHERE transfer_id = ? AND status = 'prepared'",
            (status, transfer["transfer_id"]),
        )
        updated = self._transfer(
            self._row(connection, "transfers", "transfer_id", transfer["transfer_id"])
        )
        self._emit(
            connection,
            now,
            f"transfer.{status}",
            {"transfer": updated, "reason": reason},
            companion_id=transfer["companion_id"],
            node_id=transfer["source_node_id"],
        )

    def _resolve_transfer(
        self,
        connection: sqlite3.Connection,
        transfer_id: str,
        now: datetime,
        *,
        allow_commits: bool = True,
    ) -> str:
        transfer = self._transfer(self._row(connection, "transfers", "transfer_id", transfer_id))
        if transfer["status"] != "prepared":
            return transfer["status"]
        if _instant(transfer["expires_at"]) <= now:
            self._end_transfer(connection, transfer, "expired", "transfer_timeout", now)
            return "expired"
        reason = self._travel_reason(
            connection, transfer["source_node_id"], transfer["target_node_id"], now
        )
        if reason is not None:
            self._end_transfer(connection, transfer, "aborted", reason, now)
            return "aborted"
        companion = self._companion(
            self._row(connection, "companions", "companion_id", transfer["companion_id"])
        )
        if (
            companion["revision"] != transfer["base_revision"]
            or companion["active_node_id"] != transfer["source_node_id"]
            or companion["epoch"] != transfer["snapshot"]["epoch"]
        ):
            self._end_transfer(connection, transfer, "aborted", "companion_changed", now)
            return "aborted"
        lease = companion["lease_expires_at"]
        source_stopped = (
            transfer["source_node_id"] is None
            or transfer["source_released"]
            or lease is None
            or _instant(lease) <= now
        )
        if not allow_commits or not transfer["target_ready"] or not source_stopped:
            return "prepared"
        # A bound native target must still have a live renderer at commit time.
        # Releasing or expiring that session invalidates the earlier ready.
        if transfer["target_presentation_id"] is not None:
            presentations = getattr(self, "presentations", None)
            try:
                if presentations is None:
                    raise DomainError(409, "presentation_expired", "No presentation service")
                presentations.validate(
                    connection,
                    transfer["target_node_id"],
                    transfer["target_presentation_id"],
                    now,
                )
            except DomainError:
                self._end_transfer(
                    connection, transfer, "aborted", "target_presentation_invalid", now
                )
                return "aborted"
        if companion["revision"] >= MAX_VERSION or companion["epoch"] >= MAX_VERSION:
            self._end_transfer(connection, transfer, "aborted", "version_exhausted", now)
            return "aborted"
        # Output ownership changes exactly once under the SQLite write lock.
        connection.execute(
            "UPDATE companions SET active_node_id = ?, epoch = epoch + 1, revision = revision + 1, lease_expires_at = ?, updated_at = ? WHERE companion_id = ?",
            (
                transfer["target_node_id"],
                _timestamp(now + timedelta(seconds=self.lease_ttl_seconds)),
                _timestamp(now),
                transfer["companion_id"],
            ),
        )
        connection.execute(
            "UPDATE transfers SET status = 'committed', committed_revision = ? WHERE transfer_id = ?",
            (companion["revision"] + 1, transfer_id),
        )
        updated = self._transfer(self._row(connection, "transfers", "transfer_id", transfer_id))
        companion = self._companion(
            self._row(connection, "companions", "companion_id", transfer["companion_id"])
        )
        self._emit(
            connection,
            now,
            "companion.transferred",
            {"transfer": updated, "companion": companion},
            companion_id=transfer["companion_id"],
            node_id=transfer["target_node_id"],
        )
        return "committed"

    def _reconcile(
        self, connection: sqlite3.Connection, now: datetime, *, allow_commits: bool = True
    ) -> JsonDict:
        results: JsonDict = {
            "committed": [],
            "aborted": [],
            "expired": [],
            "pending": 0,
            "server_time": _timestamp(now),
        }
        transfers = connection.execute(
            "SELECT transfer_id FROM transfers WHERE status = 'prepared' ORDER BY created_at, transfer_id"
        ).fetchall()
        for row in transfers:
            status = self._resolve_transfer(
                connection, row["transfer_id"], now, allow_commits=allow_commits
            )
            if status == "prepared":
                results["pending"] += 1
            else:
                results[status].append(row["transfer_id"])
        return results

    def reconcile(self) -> JsonDict:
        with self.store.transaction(write=True) as connection:
            return self._reconcile(connection, self._now())
