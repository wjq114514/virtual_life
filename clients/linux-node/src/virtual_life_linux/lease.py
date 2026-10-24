"""Conservative output-lease deadlines.

The host must stop drawing before any server deadline. The deadline is computed
from the moment the request was *started*, never from the moment the response
arrived, and it is capped by the advertised TTL and an optional local maximum.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


class ClockUnavailable(RuntimeError):
    """Raised when a monotonic boot clock cannot be read (fail closed)."""


def parse_timestamp(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class BootTimeClock:
    """Monotonic clock that is immune to wall-clock jumps."""

    def now_ns(self) -> int:
        try:
            return time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        except (AttributeError, OSError) as exc:  # pragma: no cover - platform guard
            raise ClockUnavailable("CLOCK_BOOTTIME is unavailable") from exc


@dataclass(frozen=True, slots=True)
class LeaseAuthorization:
    request_started_ns: int
    deadline_ns: int

    def remaining_ns(self, now_ns: int) -> int:
        return self.deadline_ns - now_ns

    def valid(self, now_ns: int) -> bool:
        return now_ns < self.deadline_ns


class LeaseGate:
    """Derive a local deadline from a server lease snapshot."""

    def __init__(
        self,
        *,
        safety_margin_ms: int = 250,
        local_max_seconds: float | None = None,
        clock: BootTimeClock | None = None,
    ):
        if safety_margin_ms < 0:
            raise ValueError("safety_margin_ms must not be negative")
        self.safety_margin_ns = int(safety_margin_ms) * 1_000_000
        self.local_max_ns = None if local_max_seconds is None else int(local_max_seconds * 1e9)
        self._clock = clock or BootTimeClock()

    def authorize(
        self,
        request_started_ns: int,
        *,
        expires_at: str,
        server_time: str,
        ttl_seconds: float,
        clock_now_ns: int | None = None,
        local_max_seconds: float | None = None,
    ) -> LeaseAuthorization | None:
        """Return a deadline, or None when the lease is already unsafe to use."""
        server_remaining_ns = int(
            (parse_timestamp(expires_at) - parse_timestamp(server_time)) * 1e9
        )
        budget_ns = int(ttl_seconds * 1e9)
        candidates = [server_remaining_ns, budget_ns]
        cap = self.local_max_ns
        if local_max_seconds is not None:
            local_cap = int(local_max_seconds * 1e9)
            cap = local_cap if cap is None else min(cap, local_cap)
        if cap is not None:
            candidates.append(cap)
        remaining_ns = min(candidates)
        if remaining_ns <= self.safety_margin_ns:
            return None
        deadline_ns = request_started_ns + remaining_ns - self.safety_margin_ns
        if clock_now_ns is not None and deadline_ns <= clock_now_ns:
            return None
        return LeaseAuthorization(request_started_ns=request_started_ns, deadline_ns=deadline_ns)

    def now_ns(self) -> int:
        return self._clock.now_ns()

    def start(self) -> int:
        return self._clock.now_ns()
