"""Clock interfaces used by the deterministic voice state machine."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def now_ns(self) -> int: ...


class MonotonicClock:
    def now_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(slots=True)
class VirtualClock:
    current_ns: int = 0

    def now_ns(self) -> int:
        return self.current_ns

    def advance(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("clock advance must be finite and nonnegative")
        self.current_ns += int(seconds * 1_000_000_000)
