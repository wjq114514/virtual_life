"""Revocable, write-only credentials for a single Linux device reporter.

The HTTP dependency checks ``authorize`` and yields inside ``mutation_guard``.
Coordinator must call ``guard_mutation(connection)`` in its write transaction before
executing an action, so revocation and registration checks cannot race a mutation.
"""

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .coordinator import DomainError, _timestamp

TOKEN_PREFIX = "vl_reporter_"
REPORT_CAPABILITIES = ["device.state.report"]
ATTENTION_CAPABILITIES = [*REPORT_CAPABILITIES, "attention.observe"]
_guard = ContextVar("node_credential_mutation_guard", default=None)


def _deny():
    raise DomainError(403, "forbidden", "Reporter credential access denied")


def _safe_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value == value.strip()
        and value not in {".", ".."}
        and not any(c in value for c in "/\\%?#")
        and value.isprintable()
    )


class NodeCredentialService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        with self.store.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS node_credentials (
                    credential_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                    node_id TEXT NOT NULL, deployment_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, revoked_at TEXT, reported_at TEXT,
                    attention_enabled INTEGER NOT NULL DEFAULT 0)"""
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(node_credentials)")
            }
            if "reported_at" not in columns:
                connection.execute("ALTER TABLE node_credentials ADD COLUMN reported_at TEXT")
            if "attention_enabled" not in columns:
                connection.execute(
                    "ALTER TABLE node_credentials ADD COLUMN "
                    "attention_enabled INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS node_credentials_deployment "
                "ON node_credentials(deployment_id)"
            )

    def _check_node(self, connection, node_id, *, allow_missing):
        row = connection.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        identity_owner = connection.execute(
            "SELECT node_id FROM device_identity_keys WHERE kind='device' AND value=?", (node_id,)
        ).fetchone()
        if identity_owner is not None and identity_owner["node_id"] != node_id:
            _deny()
        if row is None:
            if not allow_missing:
                _deny()
            return
        keys = connection.execute(
            "SELECT kind,value FROM device_identity_keys WHERE node_id=?", (node_id,)
        ).fetchall()
        capabilities = json.loads(row["capabilities_json"])
        safe_capabilities = any(
            len(capabilities) == len(allowed) and set(capabilities) == set(allowed)
            for allowed in (REPORT_CAPABILITIES, ATTENTION_CAPABILITIES)
        )
        if (
            row["kind"] != "other"
            or not safe_capabilities
            or [(key["kind"], key["value"]) for key in keys] != [("device", node_id)]
            or connection.execute(
                "SELECT 1 FROM device_web_only WHERE node_id=?", (node_id,)
            ).fetchone()
        ):
            _deny()

    def issue(self, node_id, deployment_id, *, attention_enabled=False):
        if (
            not _safe_id(node_id)
            or not _safe_id(deployment_id)
            or type(attention_enabled) is not bool
        ):
            _deny()
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        credential_id = str(uuid.uuid4())
        with self.store.transaction(write=True) as connection:
            self._check_node(connection, node_id, allow_missing=True)
            connection.execute(
                "INSERT INTO node_credentials "
                "(credential_id,token_hash,node_id,deployment_id,created_at,revoked_at,attention_enabled) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    credential_id,
                    hashlib.sha256(token.encode()).hexdigest(),
                    node_id,
                    deployment_id,
                    _timestamp(self.coordinator._now()),
                    int(attention_enabled),
                ),
            )
        return {"credential_id": credential_id, "token": token}

    def revoke_for_deployment(self, deployment_id):
        with self.store.transaction(write=True) as connection:
            connection.execute(
                "UPDATE node_credentials SET revoked_at=? "
                "WHERE deployment_id=? AND revoked_at IS NULL",
                (_timestamp(self.coordinator._now()), deployment_id),
            )

    def is_revoked(self, deployment_id):
        """Whether a deployment has credentials and none remain active."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END) AS active "
                "FROM node_credentials WHERE deployment_id=?",
                (deployment_id,),
            ).fetchone()
            return bool(row["total"] and not row["active"])

    def has_reported(self, credential_id):
        """A live credential has successfully committed its own heartbeat."""
        with self.store.transaction() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM node_credentials WHERE credential_id=? "
                    "AND revoked_at IS NULL AND reported_at IS NOT NULL",
                    (credential_id,),
                ).fetchone()
                is not None
            )

    def authorize(self, token, method, path, body=None, *, connection=None):
        """Authorize a decoded URL path; never decode or normalize it a second time."""
        if connection is None:
            with self.store.transaction() as connection:
                return self.authorize(token, method, path, body, connection=connection)
        if (
            not isinstance(token, str)
            or re.fullmatch(TOKEN_PREFIX + r"[A-Za-z0-9_-]{43}", token) is None
        ):
            _deny()
        row = connection.execute(
            "SELECT node_id,attention_enabled FROM node_credentials "
            "WHERE token_hash=? AND revoked_at IS NULL",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if row is None or method != "POST" or not isinstance(body, dict):
            _deny()
        node_id = row["node_id"]
        if path == "/v1/nodes":
            if (
                body.get("node_id") != node_id
                or body.get("kind") != "other"
                or body.get("capabilities")
                != (ATTENTION_CAPABILITIES if row["attention_enabled"] else REPORT_CAPABILITIES)
                or not isinstance(body.get("device"), dict)
                or body["device"].get("device_id") != node_id
                or body["device"].get("mac_addresses", []) != []
                or set(body["device"]) - {"device_id", "mac_addresses"}
                or set(body) - {"node_id", "label", "kind", "capabilities", "device"}
            ):
                _deny()
            self._check_node(connection, node_id, allow_missing=True)
        elif path == f"/v1/nodes/{node_id}/heartbeat":
            self._check_node(connection, node_id, allow_missing=False)
            if set(body) - {"context"}:
                _deny()
            context = body.get("context")
            if isinstance(context, dict) and isinstance(context.get("device_state"), dict):
                state = context["device_state"]
                if any(
                    state.get(key, node_id) != node_id
                    for key in ("subject_node_id", "observer_node_id")
                ):
                    _deny()
        elif path == f"/v1/nodes/{node_id}/attention":
            if not row["attention_enabled"]:
                _deny()
            self._check_node(connection, node_id, allow_missing=False)
        elif path == f"/v1/nodes/{node_id}/observations":
            self._check_node(connection, node_id, allow_missing=False)
            data = body.get("data")
            if not isinstance(data, dict) or any(
                data.get(key) != node_id for key in ("subject_node_id", "observer_node_id")
            ):
                _deny()
        else:
            _deny()
        return True

    @contextmanager
    def mutation_guard(self, token, method, path, body=None):
        """Carry authorization into the coordinator transaction via ContextVar."""
        self.authorize(token, method, path, body)
        marker = _guard.set((self, token, method, path, deepcopy(body)))
        try:
            yield
        finally:
            _guard.reset(marker)

    @staticmethod
    def guard_mutation(connection: sqlite3.Connection):
        """Invoke before any reconciliation, command replay, or mutation in _write."""
        guard = _guard.get()
        if guard is not None:
            service, token, method, path, body = guard
            service.authorize(token, method, path, body, connection=connection)


def guard_mutation(connection: sqlite3.Connection):
    """Enforce the current HTTP credential inside a coordinator write transaction."""
    NodeCredentialService.guard_mutation(connection)


def record_reported(connection: sqlite3.Connection):
    """Call only after a successful action, inside the same write transaction."""
    guard = _guard.get()
    if guard is None:
        return
    service, token, method, path, body = guard
    row = connection.execute(
        "SELECT node_id FROM node_credentials WHERE token_hash=? AND revoked_at IS NULL",
        (hashlib.sha256(token.encode()).hexdigest(),),
    ).fetchone()
    if row is not None and method == "POST" and path == f"/v1/nodes/{row['node_id']}/heartbeat":
        service.authorize(token, method, path, body, connection=connection)
        connection.execute(
            "UPDATE node_credentials SET reported_at=COALESCE(reported_at, ?) "
            "WHERE token_hash=? AND revoked_at IS NULL",
            (_timestamp(service.coordinator._now()), hashlib.sha256(token.encode()).hexdigest()),
        )
