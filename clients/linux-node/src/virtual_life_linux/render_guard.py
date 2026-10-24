"""Client-side output gate.

The daemon already refuses to start work past a lease deadline, but a frozen
renderer can still hold a buffered draft. Every paint is re-checked here against
the current generation and the monotonic boot-time deadline, so stale output is
dropped even if the socket went quiet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lease import BootTimeClock, ClockUnavailable, LeaseAuthorization


@dataclass
class RenderGuard:
    generation: str
    clock: BootTimeClock = field(default_factory=BootTimeClock)
    authorization: LeaseAuthorization | None = None

    def bind(self, generation: str, authorization: LeaseAuthorization | None) -> None:
        self.generation = generation
        self.authorization = authorization

    def accept(self, generation: str, now_ns: int | None = None) -> bool:
        """Return True only for the current generation with a live deadline."""
        if generation != self.generation:
            return False
        if self.authorization is None:
            return False
        try:
            current = self.clock.now_ns() if now_ns is None else now_ns
        except ClockUnavailable:
            return False
        return self.authorization.valid(current)

    def remaining_ns(self, now_ns: int | None = None) -> int:
        if self.authorization is None:
            return 0
        try:
            current = self.clock.now_ns() if now_ns is None else now_ns
        except ClockUnavailable:
            return 0
        return max(0, self.authorization.remaining_ns(current))
