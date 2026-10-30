#!/usr/bin/env python3
"""Keep one Virtual Life node online and, optionally, its companion's flow state.

Pure stdlib (Python 3.12+). Mirrors the repo's `adb-supervisor` / `system-node`
conventions: a dataclass config, stdlib urllib transport, atomic state file,
and a systemd unit that passes `VIRTUAL_LIFE_API_TOKEN` via the environment.

Role (per the 2-role split): a node's *state* — register, heartbeat, and (for
the node that owns a companion) lease renewal + follow/transfer — is maintained
by exactly one dedicated client (this daemon). Interaction clients (qq-node /
matrix-node) only send and receive messages; they never touch node liveness or
flow state.

Why node-level concerns live together: node liveness is computed solely from
`last_seen_at` (coordinator.py), refreshed only by POST /v1/nodes/{id}/heartbeat
— a capture client busy rendering frames or an IM client idle in a sync loop
silently goes offline after `node_ttl_seconds` even though it is alive. The
companion's output lease is similarly short (default 10s TTL) and expires while
an IM client is idle between messages, so dialogue would hit `lease_expired`
without a manual renew. Both are per-node state that must stay warm at all
times; keeping them in one daemon makes liveness and flow first-class.

Config file (strict JSON, same conventions as system-node):
    {
      "server_url": "http://127.0.0.1:8038",
      "token_env": "VIRTUAL_LIFE_API_TOKEN",
      "node_id": "qq-room",
      "label": "QQ 房间",
      "kind": "other",
      "capabilities": ["text.display", "attention.observe", "qq.message"],
      "context": {"presence": {"source": "qq", "status": "online"}},
      "interval_seconds": 20,
      "timeout_seconds": 10,
      "companion": {                       // optional: flow state for the owned companion
        "companion_id": "proact-demo",
        "lease_renew_seconds": 5,
        "flow_interval_seconds": 5,
        "follow_to": null,
        "follow_reason": "manual",
        "follow_expected_revision": 0,
        "transfer_to": null,
        "transfer_reason": "manual",
        "transfer_expected_revision": 0
      }
    }

`kind` and `capabilities` are optional and default to the node-keep role
(`kind="other"`, `capabilities=["text.display"]`); supply them when the node
owns extra capabilities so they are not clobbered by a later registration.
`context` is an optional JSON object merged into every heartbeat. Token is
read from the environment variable named by `token_env` (falling back to
`--token`, or `VIRTUAL_LIFE_API_TOKEN`).

`companion` is optional. When present the daemon also drives the companion's
flow state on a fast cadence (`flow_interval_seconds`, default = the lease
renewal interval):

  * Lease renewal: while the companion is owned by this node
    (`active_node_id == node_id`), keep POST /v1/companions/{id}/lease fresh
    so dialogue from the IM client never hits `lease_expired`.
    `lease_renew_seconds` must be < the server lease TTL (default 10s).
  * Room follow: when `follow_to` is set and the companion is not there,
    POST /v1/companions/{id}/follow to migrate it.
  * Transfer: when `transfer_to` is set and the companion is not there,
    prepare a two-party transfer. Transfer wins over follow if both are set
    (avoids racing migrations).

Run:
    python3.12 node_keep.py --config /path/to/config.json
    python3.12 node_keep.py --config /path/to/config.json --once   # one register+heartbeat, then exit
    python3.12 node_keep.py --check-config   # offline validation only

Exit codes: 0 = clean stop / --once success, 1 = config error (never retried),
2 = auth rejected (401/403), 3 = persistent transport failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("node-keep")

# Config file limits, matching system-node's strict conventions.
MAX_CONFIG_BYTES = 65_536
MAX_CONFIG_DEPTH = 8

# Rotate the log file when it exceeds this size (a flapping network would
# otherwise grow it unbounded: one line per cycle).
LOG_ROTATE_BYTES = 5 * 1024 * 1024


def rotate_log_if_needed(path: str | None) -> None:
    """Best-effort size-based log rotation (rename to .1, keep one old copy)."""
    if not path:
        return
    try:
        if os.path.getsize(path) < LOG_ROTATE_BYTES:
            return
        backup = f"{path}.1"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(path, backup)
    except OSError:
        # Rotation is best-effort; never take the keeper down for it.
        pass


# --------------------------------------------------------------------------- #
# strict config
# --------------------------------------------------------------------------- #
def _validate_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_CONFIG_DEPTH:
        raise ValueError("config too deeply nested")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("config keys must be strings")
            _validate_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_depth(item, depth + 1)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not (value == value and value not in (float("inf"), float("-inf"))):
            raise ValueError("config contains a non-finite number")
    elif value is not None and not isinstance(value, (str, bool)):
        raise ValueError(f"unsupported config value type: {type(value).__name__}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _load_config(path: str) -> dict[str, Any]:
    """Load and strictly validate a config file. Raises ValueError on any issue."""
    file_path = Path(path)
    raw = file_path.read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"config exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("config is not valid UTF-8") from exc
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    _validate_depth(data)
    return data


def _get_required(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"missing required field: {key}")
    return data[key]


def _get_str(data: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"field {key} must be a string")
    return value


def _get_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"field {key} must be an integer")
    return value


def _get_float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"field {key} must be a number")
    return float(value)


def _get_list_of_str(data: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = data.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"field {key} must be a list of strings")
    return value


def _get_dict(data: dict[str, Any], key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = data.get(key, default)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"field {key} must be a JSON object")
    return value


def _get_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"field {key} must be a boolean")
    return value


@dataclass
class CompanionConfig:
    """Flow-state configuration for the companion owned by this node."""

    companion_id: str
    lease_renew_seconds: float
    flow_interval_seconds: float
    follow_to: str | None
    follow_reason: str
    follow_expected_revision: int
    transfer_to: str | None
    transfer_reason: str
    transfer_expected_revision: int
    ack_released: bool = True
    ack_ready: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompanionConfig":
        companion_id = _get_str(data, "companion_id")
        if not companion_id or len(companion_id) > 128:
            raise ValueError("companion.companion_id is required, 1-128 chars")
        lease_renew = _get_float(data, "lease_renew_seconds", 5.0)
        if not (0.5 <= lease_renew <= 30.0):
            raise ValueError("companion.lease_renew_seconds must be in 0.5..30 (and < server lease TTL)")
        flow_interval = _get_float(data, "flow_interval_seconds", lease_renew)
        if not (0.5 <= flow_interval <= 300.0):
            raise ValueError("companion.flow_interval_seconds must be in 0.5..300")
        follow_to = _get_str(data, "follow_to")
        follow_reason = _get_str(data, "follow_reason", "manual") or "manual"
        follow_rev = _get_int(data, "follow_expected_revision", 0)
        if follow_rev < 0:
            raise ValueError("companion.follow_expected_revision must be >= 0")
        transfer_to = _get_str(data, "transfer_to")
        transfer_reason = _get_str(data, "transfer_reason", "manual") or "manual"
        transfer_rev = _get_int(data, "transfer_expected_revision", 0)
        if transfer_rev < 0:
            raise ValueError("companion.transfer_expected_revision must be >= 0")
        return cls(
            companion_id=companion_id,
            lease_renew_seconds=lease_renew,
            flow_interval_seconds=flow_interval,
            follow_to=follow_to,
            follow_reason=follow_reason,
            follow_expected_revision=follow_rev,
            transfer_to=transfer_to,
            transfer_reason=transfer_reason,
            transfer_expected_revision=transfer_rev,
            ack_released=_get_bool(data, "ack_released", True),
            ack_ready=_get_bool(data, "ack_ready", True),
        )


@dataclass
class Config:
    server_url: str
    node_id: str
    label: str
    kind: str = "other"
    capabilities: list[str] = field(default_factory=lambda: ["text.display"])
    context: dict[str, Any] = field(default_factory=dict)
    interval_seconds: float = 20.0
    timeout_seconds: float = 10.0
    token: str | None = None
    once: bool = False
    companion: CompanionConfig | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], token: str | None, once: bool) -> "Config":
        server_url = _get_str(data, "server_url")
        if not server_url:
            raise ValueError("server_url is required and must be a non-empty string")
        node_id = _get_str(data, "node_id")
        if not node_id or len(node_id) > 128:
            raise ValueError("node_id is required, 1-128 chars")
        label = _get_str(data, "label")
        if not label or len(label) > 256:
            raise ValueError("label is required, 1-256 chars")
        kind = _get_str(data, "kind", "other") or "other"
        if kind not in {"other", "desktop", "android", "web"}:
            raise ValueError(f"unsupported kind: {kind}")
        capabilities = _get_list_of_str(data, "capabilities", ["text.display"])
        context = _get_dict(data, "context")
        interval = _get_float(data, "interval_seconds", 20.0)
        if not (1.0 <= interval <= 3600.0):
            raise ValueError("interval_seconds must be in 1..3600")
        timeout = _get_float(data, "timeout_seconds", 10.0)
        if not (1.0 <= timeout <= 120.0):
            raise ValueError("timeout_seconds must be in 1..120")
        companion_raw = data.get("companion")
        companion: CompanionConfig | None = None
        if companion_raw is not None:
            if not isinstance(companion_raw, dict):
                raise ValueError("companion must be a JSON object")
            companion = CompanionConfig.from_dict(companion_raw)
        return cls(
            server_url=server_url.rstrip("/"),
            node_id=node_id,
            label=label,
            kind=kind,
            capabilities=capabilities,
            context=context,
            interval_seconds=interval,
            timeout_seconds=timeout,
            token=token,
            once=once,
            companion=companion,
        )


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _request(
    url: str,
    method: str,
    token: str | None,
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int | None, dict[str, Any] | None]:
    """One HTTP request against the runtime. Returns (status, parsed_json) or (None, None)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = {}
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        LOGGER.warning("http %s %s -> %d %s", method, url, exc.code, exc.reason)
        try:
            parsed = json.loads(exc.read().decode("utf-8")) if exc.fp else {}
        except Exception:
            parsed = {}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        LOGGER.warning("http %s %s failed: %s", method, url, exc.reason)
        return None, None
    except (TimeoutError, socket.timeout) as exc:
        LOGGER.warning("http %s %s timed out: %s", method, url, exc)
        return None, None
    except OSError as exc:
        LOGGER.warning("http %s %s socket error: %s", method, url, exc)
        return None, None


# --------------------------------------------------------------------------- #
# keeper
# --------------------------------------------------------------------------- #
@dataclass
class Keeper:
    config: Config
    auth_rejected: bool = False
    last_flow_at: float = 0.0

    # -- node state ---------------------------------------------------------
    def register(self) -> int | None:
        """Register (or refresh) the node. A 4xx that is not a conflict is fatal."""
        status, _ = _request(
            f"{self.config.server_url}/v1/nodes",
            "POST",
            self.config.token,
            {
                "node_id": self.config.node_id,
                "label": self.config.label,
                "kind": self.config.kind,
                "capabilities": self.config.capabilities,
            },
            timeout=self.config.timeout_seconds,
        )
        if status is None:
            return None
        if status in {401, 403}:
            self.auth_rejected = True
        return status

    def heartbeat(self) -> int | None:
        """POST a heartbeat to refresh last_seen_at."""
        status, _ = _request(
            f"{self.config.server_url}/v1/nodes/{self.config.node_id}/heartbeat",
            "POST",
            self.config.token,
            {"context": self.config.context},
            timeout=self.config.timeout_seconds,
        )
        if status is None:
            return None
        if status in {401, 403}:
            self.auth_rejected = True
        return status

    # -- companion flow state ----------------------------------------------
    def get_companion(self) -> tuple[int | None, dict[str, Any] | None]:
        comp = self.config.companion
        assert comp is not None
        return _request(
            f"{self.config.server_url}/v1/companions/{comp.companion_id}",
            "GET",
            self.config.token,
            timeout=self.config.timeout_seconds,
        )

    def renew_lease(self, node_id: str, epoch: int) -> tuple[int | None, dict[str, Any] | None]:
        comp = self.config.companion
        assert comp is not None
        return _request(
            f"{self.config.server_url}/v1/companions/{comp.companion_id}/lease",
            "POST",
            self.config.token,
            {"node_id": node_id, "epoch": epoch},
            timeout=self.config.timeout_seconds,
        )

    def follow(self, target_node_id: str, expected_revision: int, reason: str) -> tuple[int | None, dict[str, Any] | None]:
        comp = self.config.companion
        assert comp is not None
        return _request(
            f"{self.config.server_url}/v1/companions/{comp.companion_id}/follow",
            "POST",
            self.config.token,
            {
                "command_id": str(uuid.uuid4()),
                "target_node_id": target_node_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            timeout=self.config.timeout_seconds,
        )

    def prepare_transfer(self, target_node_id: str, expected_revision: int, reason: str) -> tuple[int | None, dict[str, Any] | None]:
        comp = self.config.companion
        assert comp is not None
        return _request(
            f"{self.config.server_url}/v1/companions/{comp.companion_id}/transfers",
            "POST",
            self.config.token,
            {
                "command_id": str(uuid.uuid4()),
                "target_node_id": target_node_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            timeout=self.config.timeout_seconds,
        )

    def acknowledge(self, transfer_id: str, node_id: str, phase: str, presentation_id: str | None = None) -> tuple[int | None, dict[str, Any] | None]:
        body: dict[str, Any] = {"node_id": node_id, "phase": phase}
        if presentation_id is not None:
            body["presentation_id"] = presentation_id
        return _request(
            f"{self.config.server_url}/v1/transfers/{transfer_id}/ack",
            "POST",
            self.config.token,
            body,
            timeout=self.config.timeout_seconds,
        )

    def flow_cycle(self) -> tuple[str, dict[str, Any]]:
        """Run one companion flow cycle. Returns (action, detail).

        action is one of: idle / lease_renewed / lease_failed / followed /
        transfer_prepared / transfer_failed. Transfer wins over follow when both
        targets are set (avoids racing migrations).
        """
        comp = self.config.companion
        assert comp is not None
        status, companion = self.get_companion()
        if status is None:
            return "lease_failed", {"reason": "transport"}
        if status in {401, 403}:
            self.auth_rejected = True
            return "lease_failed", {"reason": "auth"}
        if status >= 400:
            return "lease_failed", {"reason": f"http_{status}"}
        if not isinstance(companion, dict):
            return "lease_failed", {"reason": "invalid_response"}

        active_node = companion.get("active_node_id")
        epoch = companion.get("epoch", 0)
        if not isinstance(active_node, str):
            return "idle", {"detail": "no_active_node"}

        # -- transfer wins over follow (avoid racing migrations) -----------
        if comp.transfer_to and comp.transfer_to != active_node:
            return self._do_transfer(active_node, epoch, companion.get("revision", 0))
        if comp.follow_to and comp.follow_to != active_node:
            return self._do_follow(active_node, epoch, companion.get("revision", 0))

        # -- lease renewal (only while this node owns the companion) --------
        if active_node == self.config.node_id:
            lstatus, lease = self.renew_lease(active_node, epoch)
            if lstatus is None:
                return "lease_failed", {"reason": "transport", "node": active_node, "epoch": epoch}
            if lstatus in {401, 403}:
                self.auth_rejected = True
                return "lease_failed", {"reason": "auth", "node": active_node}
            if lstatus >= 400:
                err = lease.get("detail") or lease.get("error") or str(lease)
                return "lease_failed", {"reason": f"http_{lstatus}", "node": active_node, "epoch": epoch, "detail": err}
            return "lease_renewed", {"node": active_node, "epoch": epoch}

        return "idle", {"detail": "not_owner", "node": active_node}

    def _do_follow(self, active_node: str, epoch: int, revision: int) -> tuple[str, dict[str, Any]]:
        comp = self.config.companion
        assert comp is not None
        if time.monotonic() - self.last_flow_at < 10.0:
            return "idle", {"detail": "follow_throttled"}
        fstatus, body = self.follow(comp.follow_to, comp.follow_expected_revision, comp.follow_reason)
        self.last_flow_at = time.monotonic()
        if fstatus is None:
            return "lease_failed", {"reason": "transport", "action": "follow"}
        if fstatus in {401, 403}:
            self.auth_rejected = True
            return "lease_failed", {"reason": "auth", "action": "follow"}
        if fstatus >= 400:
            err = body.get("detail") if isinstance(body, dict) else None
            return "lease_failed", {"reason": f"http_{fstatus}", "action": "follow", "detail": err}
        return "followed", {"from": active_node, "to": comp.follow_to}

    def _do_transfer(self, active_node: str, epoch: int, revision: int) -> tuple[str, dict[str, Any]]:
        comp = self.config.companion
        assert comp is not None
        if time.monotonic() - self.last_flow_at < 10.0:
            return "idle", {"detail": "transfer_throttled"}
        tstatus, body = self.prepare_transfer(comp.transfer_to, comp.transfer_expected_revision, comp.transfer_reason)
        self.last_flow_at = time.monotonic()
        if tstatus is None:
            return "lease_failed", {"reason": "transport", "action": "transfer"}
        if tstatus in {401, 403}:
            self.auth_rejected = True
            return "lease_failed", {"reason": "auth", "action": "transfer"}
        if tstatus >= 400:
            err = body.get("detail") if isinstance(body, dict) else None
            return "lease_failed", {"reason": f"http_{tstatus}", "action": "transfer", "detail": err}
        transfer = body.get("transfer") if isinstance(body, dict) else None
        transfer_id = (transfer or {}).get("transfer_id") if isinstance(transfer, dict) else None
        if transfer_id:
            if comp.ack_released:
                self.acknowledge(transfer_id, active_node, "released")
            if comp.ack_ready:
                self.acknowledge(transfer_id, comp.transfer_to, "ready")
        return "transfer_prepared", {"from": active_node, "to": comp.transfer_to, "transfer_id": transfer_id}

    # -- main loop ----------------------------------------------------------
    def run(self) -> int:
        """Run the register+heartbeat (and optional flow) loop. Returns an exit code."""
        # -- one cycle ------------------------------------------------------
        register_status = self.register()
        if register_status is None:
            LOGGER.error("register_failed (transport); will retry")
            return 3
        if register_status >= 400 and register_status not in {409}:
            LOGGER.error("register_rejected status=%d", register_status)
            return 2 if self.auth_rejected else 1
        LOGGER.info("registered node=%s status=%s", self.config.node_id, register_status)

        heartbeat_status = self.heartbeat()
        if heartbeat_status is None:
            LOGGER.error("heartbeat_failed (transport)")
            return 3
        if heartbeat_status >= 400:
            LOGGER.error("heartbeat_rejected status=%d", heartbeat_status)
            return 2 if self.auth_rejected else 1
        LOGGER.info("heartbeat ok node=%s status=%s", self.config.node_id, heartbeat_status)

        if self.config.once:
            return 0

        # -- steady-state loop ----------------------------------------------
        consecutive_failures = 0
        last_heartbeat = time.monotonic()
        last_flow = 0.0 if self.config.companion else float("inf")
        comp_interval = self.config.companion.flow_interval_seconds if self.config.companion else 0.0
        while True:
            time.sleep(min(self.config.interval_seconds, comp_interval) if self.config.companion else self.config.interval_seconds)
            now = time.monotonic()

            # Heartbeat on the node cadence.
            if now - last_heartbeat >= self.config.interval_seconds:
                last_heartbeat = now
                status = self.heartbeat()
                if status is None:
                    consecutive_failures += 1
                    LOGGER.warning("heartbeat_failed consecutive=%d", consecutive_failures)
                    if consecutive_failures >= 3:
                        self.register()
                    continue
                if status >= 400:
                    consecutive_failures += 1
                    LOGGER.warning("heartbeat_rejected status=%d consecutive=%d", status, consecutive_failures)
                    if status in {401, 403}:
                        self.auth_rejected = True
                        LOGGER.error("auth_rejected; giving up")
                        return 2
                    continue
                consecutive_failures = 0

            # Flow state on the companion cadence.
            if self.config.companion and now - last_flow >= comp_interval:
                last_flow = now
                action, detail = self.flow_cycle()
                LOGGER.info("flow action=%s detail=%s", action, json.dumps(detail, ensure_ascii=False))
                if action == "lease_failed" and detail.get("reason") == "auth":
                    LOGGER.error("auth_rejected; giving up")
                    return 2


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Keep one Virtual Life node online (and its companion flow).")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--token", default=None, help="bearer token (overrides token_env)")
    parser.add_argument("--once", action="store_true", help="register+heartbeat once, then exit")
    parser.add_argument("--check-config", action="store_true", help="validate config and exit")
    parser.add_argument("--log-file", default=None, help="append logs to this file")
    parser.add_argument("--log-level", default="INFO", help="log level (default INFO)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.log_file:
        rotate_log_if_needed(args.log_file)
        logging.basicConfig(
            filename=args.log_file,
            level=getattr(logging, args.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        logging.basicConfig(
            level=getattr(logging, args.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    if args.check_config:
        if not args.config:
            print("--check-config requires --config", file=sys.stderr)
            return 1
        try:
            data = _load_config(args.config)
            Config.from_dict(data, token=None, once=False)
        except (OSError, ValueError) as exc:
            print(f"config invalid: {exc}", file=sys.stderr)
            return 1
        print("config ok")
        return 0

    if not args.config:
        print("--config is required", file=sys.stderr)
        return 1

    try:
        data = _load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"config invalid: {exc}", file=sys.stderr)
        return 1

    token = args.token
    if token is None:
        token_env = _get_str(data, "token_env", "VIRTUAL_LIFE_API_TOKEN") or "VIRTUAL_LIFE_API_TOKEN"
        token = os.environ.get(token_env, "").strip() or None
    if not token:
        LOGGER.error("no token: set %s env or pass --token", token_env if token is None else "token")
        return 1

    try:
        config = Config.from_dict(data, token=token, once=args.once)
    except ValueError as exc:
        print(f"config invalid: {exc}", file=sys.stderr)
        return 1

    keeper = Keeper(config)
    try:
        return keeper.run()
    except KeyboardInterrupt:
        LOGGER.info("stopped by signal")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
