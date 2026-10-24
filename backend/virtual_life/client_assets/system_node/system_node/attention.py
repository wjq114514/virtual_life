"""Conservative, bounded local desktop advice; never titles, URLs, input or screenshots."""

import os
import re
import selectors
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .operations import utc_timestamp

PROBE_BUDGET_SECONDS = 2.0
MAX_OUTPUT_BYTES = 65_536
MAX_SESSIONS = 16
PROPERTIES = (
    "Type",
    "Class",
    "Remote",
    "Active",
    "State",
    "LockedHint",
    "IdleHint",
    "User",
    "Display",
)
APPLICATIONS = {
    "code": "code",
    "code-oss": "code",
    "vscodium": "code",
    "jetbrains-idea": "code",
    "jetbrains-pycharm": "code",
    "gnome-terminal-server": "terminal",
    "konsole": "terminal",
    "xterm": "terminal",
    "kitty": "terminal",
    "alacritty": "terminal",
    "firefox": "browser",
    "chromium": "browser",
    "google-chrome": "browser",
    "brave-browser": "browser",
    "evince": "docs",
    "org.gnome.evince": "docs",
    "okular": "docs",
    "libreoffice-writer": "docs",
    "vlc": "media",
    "mpv": "media",
}


class ProbeError(Exception):
    pass


def run_command(argv: list[str], timeout: float) -> str:
    """Bound both pipe memory and wall time, without a shell or arbitrary arguments."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ProbeError()
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT_BYTES:
                        raise ProbeError()
            if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                raise ProbeError()
            return output.decode("utf-8", errors="strict")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


@dataclass(frozen=True)
class AttentionReading:
    session_id: str
    sample: dict[str, Any]
    diagnostics: dict[str, Any]


class AttentionCollector:
    def __init__(
        self,
        session_id: str | None = None,
        *,
        runner=run_command,
        monotonic=time.monotonic,
        now=lambda: datetime.now(UTC),
    ):
        self.requested_session = session_id
        self.runner = runner
        self.monotonic = monotonic
        self.now = now
        self._last_sampled = None
        self._selected = None
        self._session = str(uuid.uuid4())
        self._presence = None

    def _run(self, argv, deadline):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise ProbeError()
        output = self.runner(argv, remaining)
        if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ProbeError()
        if self.monotonic() > deadline:
            raise ProbeError()
        return output

    def _sessions(self, deadline):
        if self.requested_session:
            ids = [self.requested_session]
        else:
            raw = self._run(["loginctl", "list-sessions", "--no-legend", "--no-pager"], deadline)
            ids = [line.split()[0] for line in raw.splitlines() if line.strip()]
        if len(ids) > MAX_SESSIONS or len(set(ids)) != len(ids):
            raise ProbeError()
        sessions = []
        for identity in ids:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identity):
                raise ProbeError()
            raw = self._run(
                [
                    "loginctl",
                    "show-session",
                    identity,
                    "--no-pager",
                    "--property=" + ",".join(PROPERTIES),
                ],
                deadline,
            )
            properties = {}
            for line in raw.splitlines():
                key, separator, value = line.partition("=")
                if not separator or key in properties:
                    raise ProbeError()
                properties[key] = value
            if any(not properties.get(key) for key in ("Type", "Class", "Remote")):
                raise ProbeError()
            if properties["Remote"] not in {"yes", "no"}:
                raise ProbeError()
            if (
                properties["Type"] not in {"x11", "wayland"}
                or properties["Class"] != "user"
                or properties["Remote"] != "no"
            ):
                continue
            if any(
                properties.get(key) not in {"yes", "no"}
                for key in ("Active", "LockedHint", "IdleHint")
            ):
                raise ProbeError()
            if properties.get("State") not in {"active", "online", "closing"}:
                raise ProbeError()
            if (properties["Active"] == "yes") != (properties["State"] == "active"):
                raise ProbeError()
            if not re.fullmatch(r"[0-9]{1,10}", properties.get("User", "")):
                raise ProbeError()
            if self.requested_session or properties["Active"] == "yes":
                sessions.append((identity, properties))
        return sessions

    def _activity(self, properties, present, deadline):
        display = os.environ.get("DISPLAY", "")
        if (
            properties["Type"] != "x11"
            or present is not True
            or int(properties["User"]) != os.getuid()
        ):
            return "unknown", "logind"
        # Only the invoking user's inherited, local display. Never locate or copy Xauthority.
        if (
            not re.fullmatch(r":[0-9]+(?:\.[0-9]+)?", display)
            or properties.get("Display") != display
        ):
            return "unknown", "logind"
        raw = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], deadline)
        match = re.fullmatch(r"_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9a-fA-F]+)\s*", raw)
        if not match or int(match[1], 16) == 0:
            return "unknown", "logind"
        raw = self._run(["xprop", "-id", match[1], "WM_CLASS"], deadline)
        match = re.fullmatch(
            r'WM_CLASS\(STRING\) = "([^"\\\n]{1,256})", "([^"\\\n]{1,256})"\s*', raw
        )
        if not match:
            return "unknown", "logind"
        classes = {value.lower() for value in match.groups()}
        activities = {APPLICATIONS[value] for value in classes if value in APPLICATIONS}
        return (activities.pop() if len(activities) == 1 else "unknown"), "logind+x11"

    def collect(self) -> AttentionReading:
        sample = {
            "activity": "unknown",
            "focus": "unknown",
            "availability": "unknown",
            "user_present": None,
            "locked": None,
            "meeting": None,
            "screen_sharing": None,
            "boundary": "none",
            "boundary_id": None,
        }
        diagnostics = {
            "status": "unknown",
            "source": "logind",
            "session_type": None,
            "idle_hint": None,
            "active": None,
            "locked_hint": None,
        }
        deadline = self.monotonic() + PROBE_BUDGET_SECONDS
        try:
            sessions = self._sessions(deadline)
            if len(sessions) != 1:
                diagnostics["reason"] = "ambiguous_session" if sessions else "no_session"
                self._presence = None
            else:
                identity, properties = sessions[0]
                selected = (identity, properties["User"])
                changed = selected != self._selected
                if changed:
                    self._selected = selected
                    self._session = str(uuid.uuid4())
                    self._presence = None
                locked = properties["LockedHint"] == "yes"
                active = properties["Active"] == "yes"
                idle = properties["IdleHint"] == "yes"
                # Idle includes quiet reading, not evidence that the user went away.
                present = False if locked or not active else None if idle else True
                diagnostics.update(idle_hint=idle, active=active, locked_hint=locked)
                sample.update(user_present=present, locked=locked)
                if not changed and self._presence is False and present:
                    sample.update(boundary="user_returned", boundary_id=str(uuid.uuid4()))
                self._presence = present
                activity, source = self._activity(properties, present, deadline)
                sample["activity"] = activity
                diagnostics.update(status="ok", source=source, session_type=properties["Type"])
        except (OSError, ValueError, ProbeError, subprocess.SubprocessError):
            self._presence = None
            sample.update(
                activity="unknown",
                user_present=None,
                locked=None,
                boundary="none",
                boundary_id=None,
            )
            diagnostics.update(status="unknown", reason="probe_failed")
        sampled = self.now()
        if self._last_sampled is not None:
            sampled = max(sampled, self._last_sampled + timedelta(milliseconds=1))
        diagnostics["sampled_at"] = utc_timestamp(sampled)
        self._last_sampled = datetime.fromisoformat(
            diagnostics["sampled_at"].replace("Z", "+00:00")
        )
        return AttentionReading(self._session, sample, diagnostics)
