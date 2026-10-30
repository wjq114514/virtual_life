#!/usr/bin/env python3
"""Long-term adb server keeper + device offline auto-reconnect.

Pure stdlib (Python 3.12+). Mirrors the repo's `system-node` client conventions:
stdlib-only, subprocess-driven adb, optional observation-style state change
reporting. Never fakes state — it only keeps adb healthy and reconnects a
dropped/offline device, then lets the DSH `@zseven-w/dsh-android` plugin's own
keep-alive restart the screencap loop.

Resolution order for adb matches the plugin exactly:
    ADB env var -> `adb` on PATH -> ANDROID_HOME/ANDROID_SDK_ROOT ->
    ~/Android/Sdk (+/platform-tools/adb).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("adb-keep")

# Device state values adb `devices -l` reports, matching the plugin's AdbDeviceState.
DEVICE_STATES = ("device", "offline", "unauthorized", "recovery", "sideload", "unknown")

# Rotate the log file when it exceeds this size (a flapping device would
# otherwise grow it unbounded: one INFO line per cycle).
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
# adb discovery
# --------------------------------------------------------------------------- #
def resolve_adb(explicit: str | None) -> str | None:
    """Resolve the adb executable, matching the DSH plugin's tier order."""
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        LOGGER.error("adb path from --adb is not executable: %s", explicit)
        return None
    env = os.environ.get("ADB", "").strip()
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        LOGGER.error("ADB env var points at a non-executable file: %s", env)
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    home = Path.home()
    roots = []
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(key, "").strip()
        if val:
            roots.append(Path(val))
    roots.append(home / "Android" / "Sdk")
    for root in roots:
        candidate = root / "platform-tools" / "adb"
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# --------------------------------------------------------------------------- #
# adb invocation
# --------------------------------------------------------------------------- #
def run_adb(adb: str, args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run an adb command, returning the CompletedProcess (never raises on rc)."""
    try:
        return subprocess.run(
            [adb, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess([adb, *args], 124, exc.stdout or "", exc.stderr or "")


def ensure_server(adb: str, timeout: float = 30.0) -> bool:
    """Ensure the adb server is running (`adb start-server` is idempotent)."""
    proc = run_adb(adb, ["start-server"], timeout=timeout)
    if proc.returncode == 0:
        return True
    LOGGER.warning("adb start-server failed rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:200])
    return False


def list_devices(adb: str, timeout: float = 30.0) -> dict[str, str]:
    """Return {serial: state} for `adb devices -l` rows. Empty on failure."""
    proc = run_adb(adb, ["devices", "-l"], timeout=timeout)
    if proc.returncode != 0:
        LOGGER.warning("adb devices failed rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:200])
        return {}
    devices: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if not parts:
            continue
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        devices[serial] = state
    return devices


def reconnect_device(adb: str, serial: str, timeout: float = 30.0) -> bool:
    """Kick one device from the host side to force reconnect.

    NOTE: `adb reconnect <serial>` is NOT valid syntax on adb 1.0.41
    (only `adb reconnect [device|offline]` is). Per-serial kicking is
    therefore a no-op on this adb; we log it and let `reconnect_offline`
    do the real work. This function is kept so callers stay explicit.
    """
    proc = run_adb(adb, ["reconnect", "device"], timeout=timeout)
    if proc.returncode == 0:
        LOGGER.debug("adb reconnect device ok (covering %s)", serial)
        return True
    LOGGER.warning("adb reconnect device failed rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:200])
    return False


def reconnect_offline(adb: str, timeout: float = 30.0) -> bool:
    """Reset offline/unauthorized devices to force reconnect."""
    proc = run_adb(adb, ["reconnect", "offline"], timeout=timeout)
    if proc.returncode == 0:
        return True
    LOGGER.warning("adb reconnect offline failed rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:200])
    return False


# --------------------------------------------------------------------------- #
# optional state-change reporting (Virtual Life observation style)
# --------------------------------------------------------------------------- #
def _request(
    url: str,
    method: str,
    token: str | None,
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> int | None:
    """One HTTP request against the runtime. Returns status, or None on failure."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        LOGGER.warning("http %s %s -> %d %s", method, url, exc.code, exc.reason)
        return exc.code
    except urllib.error.URLError as exc:
        LOGGER.warning("http %s %s failed: %s", method, url, exc.reason)
        return None
    except (TimeoutError, socket.timeout) as exc:
        LOGGER.warning("http %s %s timed out: %s", method, url, exc)
        return None
    except OSError as exc:
        LOGGER.warning("http %s %s socket error: %s", method, url, exc)
        return None


def base_url(url: str) -> str:
    """Normalize a server origin; tolerate a trailing slash or a stray path."""
    return url.rstrip("/")


def ensure_observer_registered(
    server_url: str, token: str | None, node_id: str, label: str
) -> bool:
    """Register (or refresh) the observer node that receives device-state reports."""
    status = _request(
        f"{base_url(server_url)}/v1/nodes",
        "POST",
        token,
        {
            "node_id": node_id,
            "label": label,
            "kind": "other",
            "capabilities": ["device.state.observe"],
            "device": {"device_id": node_id},
        },
    )
    return status is not None and status < 400


def heartbeat_observer(server_url: str, token: str | None, node_id: str) -> bool:
    """Keep the observer node online so its observation lane stays authoritative."""
    status = _request(
        f"{base_url(server_url)}/v1/nodes/{node_id}/heartbeat",
        "POST",
        token,
        {"context": {}},
    )
    return status is not None and status < 400


def post_device_observation(
    server_url: str,
    token: str | None,
    observer_node_id: str,
    subject_node_id: str,
    previous: str,
    current: str,
) -> bool:
    """POST one device_state observation for a serial whose adb state changed."""
    status = _request(
        f"{base_url(server_url)}/v1/nodes/{observer_node_id}/observations",
        "POST",
        token,
        {
            "command_id": str(uuid.uuid4()),
            "modality": "device_state",
            "user_initiated": False,
            "data": {
                "schema_version": "1.0",
                "subject_node_id": subject_node_id,
                "observer_node_id": observer_node_id,
                "changes": [
                    {"field": "adb_state", "previous": previous, "current": current}
                ],
            },
        },
    )
    return status is not None and status < 400


# --------------------------------------------------------------------------- #
# keeper
# --------------------------------------------------------------------------- #
@dataclass
class KeeperConfig:
    interval: float = 10.0
    reconnect_offline: bool = True
    restart_server: bool = True
    max_restarts: int = 5
    once: bool = False
    adb: str | None = None
    notify_url: str | None = None
    notify_token: str | None = None
    observer_node_id: str | None = None
    observer_label: str | None = None
    report_observer: bool = True
    state_file: str | None = None


@dataclass
class KeeperState:
    adb: str | None = None
    previous: dict[str, str] = field(default_factory=dict)
    consecutive_failures: int = 0
    reconnects: int = 0
    restarts: int = 0
    observer_registered: bool = False


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_previous_state(path: str | None) -> dict[str, str]:
    """Load the last-known device states from a state file (best-effort)."""
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    except (OSError, ValueError):
        pass
    return {}


def save_previous_state(path: str | None, devices: dict[str, str]) -> None:
    """Persist device states so a restart keeps its transition baseline."""
    if not path:
        return
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(devices, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def cycle(cfg: KeeperConfig, state: KeeperState) -> None:
    """One keep-alive cycle: ensure server, enumerate, reconnect, report changes."""
    adb = state.adb
    if adb is None:
        LOGGER.error("adb not resolved; nothing to do")
        state.consecutive_failures += 1
        return

    # Register + heartbeat the observer node once per cycle when reporting is on.
    # Doing this at startup (not only on change) keeps the node's capabilities
    # correct even while no device is attached, so a late transition is accepted.
    if cfg.notify_url and cfg.observer_node_id and cfg.report_observer:
        if not state.observer_registered:
            state.observer_registered = ensure_observer_registered(
                cfg.notify_url,
                cfg.notify_token,
                cfg.observer_node_id,
                cfg.observer_label or cfg.observer_node_id,
            )
        if state.observer_registered:
            heartbeat_observer(cfg.notify_url, cfg.notify_token, cfg.observer_node_id)

    if cfg.restart_server and not ensure_server(adb):
        state.consecutive_failures += 1
        # A dead server makes enumeration meaningless; stop the cycle here.
        if state.consecutive_failures >= cfg.max_restarts:
            LOGGER.error("adb server unhealthy for %d cycles; backing off", state.consecutive_failures)
        return

    devices = list_devices(adb)
    if not devices and state.consecutive_failures >= cfg.max_restarts:
        LOGGER.warning("no devices listed; skipping reconnect (backoff active)")
        return

    # Reconnect offline/unauthorized/missing devices.
    if cfg.reconnect_offline:
        bad = [s for s, st in devices.items() if st in ("offline", "unauthorized")]
        if bad:
            LOGGER.info("offline/unauthorized devices: %s — reconnecting", ", ".join(bad))
            reconnect_offline(adb)
            for serial in bad:
                reconnect_device(adb, serial)
            state.reconnects += 1

    # Detect transitions for reporting.
    current = devices
    changed: list[tuple[str, str, str]] = []
    for serial, now_state in current.items():
        prev = state.previous.get(serial)
        if prev is not None and prev != now_state:
            changed.append((serial, prev, now_state))
    for serial in state.previous:
        if serial not in current:
            changed.append((serial, state.previous[serial], "missing"))
    if changed and cfg.notify_url and cfg.observer_node_id and cfg.report_observer:
        for serial, prev, now_state in changed:
            post_device_observation(
                cfg.notify_url,
                cfg.notify_token,
                cfg.observer_node_id,
                serial,
                prev,
                now_state,
            )
    elif changed:
        LOGGER.info("device state changes: %s", "; ".join(f"{s}: {p}->{c}" for s, p, c in changed))

    state.previous = dict(current)
    save_previous_state(cfg.state_file, current)
    state.consecutive_failures = 0
    LOGGER.debug("cycle ok devices=%d", len(current))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keep the adb server alive and auto-reconnect devices.")
    parser.add_argument("--interval", type=float, default=10.0, help="check interval seconds (default 10)")
    parser.add_argument("--reconnect-offline", action="store_true", default=True, help="reconnect offline devices")
    parser.add_argument("--no-reconnect-offline", dest="reconnect_offline", action="store_false")
    parser.add_argument("--restart-server", action="store_true", default=True, help="restart adb server when dead")
    parser.add_argument("--no-restart-server", dest="restart_server", action="store_false")
    parser.add_argument("--max-restarts", type=int, default=5, help="failures before backing off a cycle")
    parser.add_argument("--once", action="store_true", help="run a single cycle then exit")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--log-file", default=None, help="append log file (default stderr)")
    parser.add_argument("--adb", default=None, help="explicit adb path")
    parser.add_argument("--notify-url", default=None, help="optional observation endpoint to POST state changes")
    parser.add_argument("--notify-token", default=os.environ.get("VIRTUAL_LIFE_API_TOKEN"),
                        help="Bearer token for --notify-url (default: VIRTUAL_LIFE_API_TOKEN env)")
    parser.add_argument("--observer-node-id", default=None, help="observer node id for device-state reports")
    parser.add_argument("--observer-label", default=None, help="observer node label (default: node id)")
    parser.add_argument("--state-file", default=None, help="persist last-known device states here (survive restart)")
    parser.add_argument("--no-report-observer", dest="report_observer", action="store_false",
                        help="disable observer registration/heartbeat when reporting")
    args = parser.parse_args(argv)

    log_kwargs: dict[str, Any] = {
        "level": logging.DEBUG if args.debug else logging.INFO,
        "format": "%(asctime)s %(levelname)-5s %(message)s",
    }
    if args.log_file:
        log_kwargs["filename"] = args.log_file
    else:
        log_kwargs["stream"] = sys.stderr
    logging.basicConfig(**log_kwargs)

    cfg = KeeperConfig(
        interval=max(1.0, args.interval),
        reconnect_offline=args.reconnect_offline,
        restart_server=args.restart_server,
        max_restarts=max(1, args.max_restarts),
        once=args.once,
        adb=args.adb,
        notify_url=args.notify_url,
        notify_token=args.notify_token,
        observer_node_id=args.observer_node_id,
        observer_label=args.observer_label,
        report_observer=args.report_observer,
        state_file=args.state_file,
    )

    state = KeeperState()
    state.previous = load_previous_state(args.state_file)
    state.adb = resolve_adb(args.adb)
    if state.adb is None:
        LOGGER.error("adb not found — set ADB, add it to PATH, or install platform-tools")
        return 2
    LOGGER.info("using adb: %s", state.adb)

    if cfg.once:
        cycle(cfg, state)
        return 0 if state.consecutive_failures == 0 else 1

    LOGGER.info("adb keeper starting (interval=%ss)", cfg.interval)
    while True:
        try:
            rotate_log_if_needed(args.log_file)
            cycle(cfg, state)
        except KeyboardInterrupt:
            LOGGER.info("stopped by interrupt")
            return 0
        except Exception:
            LOGGER.exception("cycle failed")
            state.consecutive_failures += 1
        time.sleep(cfg.interval)


if __name__ == "__main__":
    raise SystemExit(main())
