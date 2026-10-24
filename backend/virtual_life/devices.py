"""Durable device identity and bounded, exclusive browser access sessions."""

import ipaddress
import uuid
from datetime import timedelta

from .coordinator import DomainError, _identifier, _instant, _json, _timestamp, _validated
from .models import WebAccessHeartbeat, WebAccessRequest

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS device_identity_keys (
        kind TEXT NOT NULL, value TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES nodes(node_id),
        PRIMARY KEY(kind, value))""",
    """CREATE TABLE IF NOT EXISTS device_ips (
        node_id TEXT NOT NULL REFERENCES nodes(node_id), ip TEXT NOT NULL, observed_at TEXT NOT NULL,
        PRIMARY KEY(node_id, ip))""",
    """CREATE TABLE IF NOT EXISTS device_browsers (
        browser_id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(node_id))""",
    """CREATE TABLE IF NOT EXISTS device_web_only (
        node_id TEXT PRIMARY KEY REFERENCES nodes(node_id))""",
    """CREATE TABLE IF NOT EXISTS device_access_ids (
        access_id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(node_id),
        browser_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS device_web_accesses (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        access_id TEXT UNIQUE NOT NULL REFERENCES device_access_ids(access_id),
        node_id TEXT NOT NULL REFERENCES nodes(node_id), browser_id TEXT NOT NULL,
        last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        context_json TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS device_access_node ON device_web_accesses(node_id, sequence DESC)",
    "CREATE INDEX IF NOT EXISTS device_ip_recent ON device_ips(ip, observed_at)",
)


def _observed_ip(peer_ip):
    if not isinstance(peer_ip, str) or "%" in peer_ip:
        return None
    try:
        address = ipaddress.ip_address(peer_ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return None if address.is_unspecified or address.is_multicast else str(address)


def _lan_ip(peer_ip):
    """Only directly observed RFC1918/ULA addresses can aid identity resolution."""
    normalized = _observed_ip(peer_ip)
    if normalized is None:
        return None
    address = ipaddress.ip_address(normalized)
    networks = (
        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16") if address.version == 4 else ("fc00::/7",)
    )
    return normalized if any(address in ipaddress.ip_network(net) for net in networks) else None


class DeviceService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store
        with self.store.transaction(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    @staticmethod
    def _keys(data):
        identity = data.get("device") or {}
        keys = [("mac", value) for value in identity.get("mac_addresses", [])]
        if identity.get("device_id") is not None:
            keys.append(("device", identity["device_id"]))
        return keys

    def check_registration(self, connection, data):
        existing = connection.execute(
            "SELECT kind FROM nodes WHERE node_id=?", (data["node_id"],)
        ).fetchone()
        if data["kind"] == "web" and (existing is None or existing["kind"] != "web"):
            raise DomainError(409, "web_access_required", "Web clients must open a device access")
        owners = {
            row["node_id"]
            for kind, value in self._keys(data)
            if (
                row := connection.execute(
                    "SELECT node_id FROM device_identity_keys WHERE kind=? AND value=?",
                    (kind, value),
                ).fetchone()
            )
            is not None
        }
        if len(owners) > 1:
            raise DomainError(
                409, "device_identity_conflict", "Identity keys identify different nodes"
            )
        if owners and owners != {data["node_id"]}:
            canonical = next(iter(owners))
            raise DomainError(
                409,
                "device_already_registered",
                f"Device is already registered as node {canonical}",
            )

    def record_registration(self, connection, data, now, peer_ip):
        for kind, value in self._keys(data):
            connection.execute(
                "INSERT OR IGNORE INTO device_identity_keys VALUES (?, ?, ?)",
                (kind, value, data["node_id"]),
            )
        connection.execute("DELETE FROM device_web_only WHERE node_id=?", (data["node_id"],))
        self.record_native_ip(connection, data["node_id"], now, peer_ip)

    def _record_ip(self, connection, node_id, now, peer_ip):
        address = _observed_ip(peer_ip)
        if address is not None:
            connection.execute(
                """INSERT INTO device_ips VALUES (?, ?, ?)
                ON CONFLICT(node_id, ip) DO UPDATE SET observed_at=excluded.observed_at""",
                (node_id, address, _timestamp(now)),
            )

    def record_native_ip(self, connection, node_id, now, peer_ip):
        row = connection.execute("SELECT kind FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if row is None:
            return
        identified = connection.execute(
            "SELECT 1 FROM device_identity_keys WHERE node_id=? LIMIT 1", (node_id,)
        ).fetchone()
        if row["kind"] in ("desktop", "android") or (row["kind"] == "other" and identified):
            self._record_ip(connection, node_id, now, peer_ip)

    def _live(self, row, now):
        return not row["released"] and _instant(row["expires_at"]) > now

    def _assert_no_native(self, connection, node_id, now):
        # A native presentation is an exclusive on-device output entry point.
        presentations = getattr(self.coordinator, "presentations", None)
        if presentations is not None:
            presentations.assert_web_allowed(connection, node_id, now)

    def _snapshot(self, connection, node_id, access_id, expires_at, resolution, now):
        row = self.coordinator._row(connection, "nodes", "node_id", node_id)
        return {
            "node": self.coordinator._node(row, now, connection),
            "access_id": access_id,
            "expires_at": expires_at,
            "resolution": resolution,
        }

    def _resolve(self, connection, data, peer_ip, now):
        if data["bind_node_id"] is not None:
            self.coordinator._row(connection, "nodes", "node_id", data["bind_node_id"])
            return data["bind_node_id"], "bound"
        browser = connection.execute(
            "SELECT node_id FROM device_browsers WHERE browser_id=?", (data["browser_id"],)
        ).fetchone()
        if browser is not None:
            return browser["node_id"], "browser_id"
        address = _lan_ip(peer_ip)
        if address is not None:
            candidates = connection.execute(
                "SELECT node_id FROM device_ips WHERE ip=? AND observed_at>? ORDER BY node_id",
                (address, _timestamp(now - timedelta(seconds=self.coordinator.node_ttl_seconds))),
            ).fetchall()
            if len(candidates) > 1:
                raise DomainError(
                    409, "device_ambiguous", "Multiple devices match; bind a node explicitly"
                )
            if candidates:
                return candidates[0]["node_id"], "lan_ip"
        return "device-" + str(uuid.uuid4()), "created"

    def open_web(self, payload, *, peer_ip=None):
        data = _validated(WebAccessRequest, payload).model_dump(mode="json")

        def action(connection, now):
            previous_id = connection.execute(
                "SELECT * FROM device_access_ids WHERE access_id=?", (data["access_id"],)
            ).fetchone()
            previous = connection.execute(
                "SELECT * FROM device_web_accesses WHERE access_id=?", (data["access_id"],)
            ).fetchone()
            # A retry is valid only while the original session is live. Resolve
            # explicit rebinding before any browser or node record is changed.
            if previous_id is not None:
                if previous is None or not self._live(previous, now):
                    raise DomainError(409, "node_access_expired", "Open a new access session")
                if previous_id["browser_id"] != data["browser_id"] or (
                    data["bind_node_id"] is not None
                    and previous_id["node_id"] != data["bind_node_id"]
                ):
                    raise DomainError(409, "device_identity_conflict", "Access cannot be rebound")
                browser = connection.execute(
                    "SELECT node_id FROM device_browsers WHERE browser_id=?", (data["browser_id"],)
                ).fetchone()
                if browser is not None and browser["node_id"] != previous_id["node_id"]:
                    raise DomainError(409, "device_identity_conflict", "Access cannot be rebound")
                self._assert_no_native(connection, previous_id["node_id"], now)
                self.coordinator._reconcile(connection, now, allow_commits=False)
                expires_at = _timestamp(now + timedelta(seconds=self.coordinator.node_ttl_seconds))
                connection.execute(
                    "UPDATE device_web_accesses SET last_seen_at=?,expires_at=? WHERE access_id=?",
                    (_timestamp(now), expires_at, data["access_id"]),
                )
                self._record_ip(connection, previous["node_id"], now, peer_ip)
                self.coordinator._reconcile(connection, now)
                return self._snapshot(
                    connection,
                    previous["node_id"],
                    data["access_id"],
                    expires_at,
                    "bound" if data["bind_node_id"] else "browser_id",
                    now,
                )
            node_id, resolution = self._resolve(connection, data, peer_ip, now)
            self._assert_no_native(connection, node_id, now)
            busy = connection.execute(
                """SELECT 1 FROM device_web_accesses WHERE node_id=? AND released=0
                AND expires_at>? LIMIT 1""",
                (node_id, _timestamp(now)),
            ).fetchone()
            if busy:
                raise DomainError(409, "device_access_busy", "Device already has a live Web access")
            self.coordinator._reconcile(connection, now, allow_commits=False)
            timestamp = _timestamp(now)
            expires_at = _timestamp(now + timedelta(seconds=self.coordinator.node_ttl_seconds))
            if resolution == "created":
                connection.execute(
                    """INSERT INTO nodes(node_id,label,kind,capabilities_json,context_json,created_at,last_seen_at)
                    VALUES (?, ?, ?, '[]', '{}', ?, ?)""",
                    (node_id, data["label"], data["kind"], timestamp, timestamp),
                )
                connection.execute("INSERT INTO device_web_only VALUES (?)", (node_id,))
            connection.execute(
                """INSERT INTO device_browsers VALUES (?, ?) ON CONFLICT(browser_id)
                DO UPDATE SET node_id=excluded.node_id""",
                (data["browser_id"], node_id),
            )
            connection.execute(
                "INSERT INTO device_access_ids VALUES (?, ?, ?)",
                (data["access_id"], node_id, data["browser_id"]),
            )
            connection.execute(
                """INSERT INTO device_web_accesses
                (access_id,node_id,browser_id,last_seen_at,expires_at,context_json)
                VALUES (?, ?, ?, ?, ?, '{}')""",
                (data["access_id"], node_id, data["browser_id"], timestamp, expires_at),
            )
            self._record_ip(connection, node_id, now, peer_ip)
            self._prune(connection, node_id, now)
            self.coordinator._reconcile(connection, now)
            result = self._snapshot(
                connection, node_id, data["access_id"], expires_at, resolution, now
            )
            self.coordinator._emit(
                connection, now, "node.web_access_opened", {"access": result}, node_id=node_id
            )
            return result

        return self.coordinator._write(action, reconcile_first=False)

    def _prune(self, connection, node_id, now):
        connection.execute(
            """DELETE FROM device_web_accesses WHERE node_id=?
            AND (released=1 OR expires_at<=?) AND sequence NOT IN
            (SELECT sequence FROM device_web_accesses WHERE node_id=? ORDER BY sequence DESC LIMIT 64)""",
            (node_id, _timestamp(now), node_id),
        )

    def validate_access(self, connection, node_id, access_id, now):
        row = connection.execute(
            "SELECT * FROM device_web_accesses WHERE node_id=? AND access_id=?",
            (node_id, access_id),
        ).fetchone()
        if row is None or not self._live(row, now):
            raise DomainError(
                409, "node_access_expired", "Web access is no longer valid for this node"
            )
        return row

    def heartbeat(self, node_id, access_id, payload, *, peer_ip=None):
        node_id, access_id = _identifier(node_id), _identifier(access_id)
        data = _validated(WebAccessHeartbeat, payload).model_dump(mode="json")

        def action(connection, now):
            self.validate_access(connection, node_id, access_id, now)
            self.coordinator._reconcile(connection, now, allow_commits=False)
            expires_at = _timestamp(now + timedelta(seconds=self.coordinator.node_ttl_seconds))
            connection.execute(
                "UPDATE device_web_accesses SET context_json=?,last_seen_at=?,expires_at=? WHERE access_id=?",
                (_json(data["context"]), _timestamp(now), expires_at, access_id),
            )
            self._record_ip(connection, node_id, now, peer_ip)
            self.coordinator._reconcile(connection, now)
            return self._snapshot(connection, node_id, access_id, expires_at, "browser_id", now)

        return self.coordinator._write(action, reconcile_first=False)

    def release(self, node_id, access_id):
        node_id, access_id = _identifier(node_id), _identifier(access_id)

        def action(connection, now):
            row = self.coordinator._row(connection, "nodes", "node_id", node_id)
            self.coordinator._reconcile(connection, now, allow_commits=False)
            closing = connection.execute(
                "SELECT 1 FROM device_web_accesses WHERE node_id=? AND access_id=? AND released=0",
                (node_id, access_id),
            ).fetchone()
            connection.execute(
                "UPDATE device_web_accesses SET released=1 WHERE node_id=? AND access_id=?",
                (node_id, access_id),
            )
            # Only a real close is audited; the endpoint itself stays idempotent.
            if closing is not None:
                self.coordinator._emit(
                    connection,
                    now,
                    "node.web_access_released",
                    {"access_id": access_id},
                    node_id=node_id,
                )
            self._prune(connection, node_id, now)
            self.coordinator._reconcile(connection, now)
            return self.coordinator._node(row, now, connection)

        return self.coordinator._write(action, reconcile_first=False)

    def project(self, connection, row, now, snapshot):
        node_id = row["node_id"]
        keys = connection.execute(
            "SELECT kind,value FROM device_identity_keys WHERE node_id=? ORDER BY kind,value",
            (node_id,),
        ).fetchall()
        ips = connection.execute(
            "SELECT ip FROM device_ips WHERE node_id=? ORDER BY ip", (node_id,)
        ).fetchall()
        snapshot["device"] = {
            "device_id": next((key["value"] for key in keys if key["kind"] == "device"), None),
            "mac_addresses": [key["value"] for key in keys if key["kind"] == "mac"],
            "ip_addresses": [ip["ip"] for ip in ips],
        }
        accesses = connection.execute(
            """SELECT * FROM device_web_accesses WHERE node_id=? AND released=0 AND expires_at>?
            ORDER BY sequence""",
            (node_id, _timestamp(now)),
        ).fetchall()
        native = (
            connection.execute(
                "SELECT 1 FROM device_web_only WHERE node_id=?", (node_id,)
            ).fetchone()
            is None
        )
        native_live = native and snapshot["online"]
        snapshot["capabilities"] = snapshot["capabilities"] if native_live else []
        snapshot["online"] = bool(native_live or accesses)
        snapshot["accesses"] = [
            {
                "access_id": access["access_id"],
                "kind": "web",
                "last_seen_at": access["last_seen_at"],
                "expires_at": access["expires_at"],
            }
            for access in accesses
        ]
        if accesses:
            snapshot["capabilities"] = sorted(set(snapshot["capabilities"]) | {"text.display"})
            snapshot["last_seen_at"] = max(
                snapshot["last_seen_at"], *(access["last_seen_at"] for access in accesses)
            )
        return snapshot
