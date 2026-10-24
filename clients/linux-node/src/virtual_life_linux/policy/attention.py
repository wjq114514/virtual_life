"""Time-bounded attention overrides.

Overrides are explicit, user-set and short lived. They never persist across a
daemon restart and always expire on their own.
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_STATES = ("focus", "away", "break")


@dataclass
class AttentionPolicy:
    ttl_seconds: int = 45

    def __post_init__(self) -> None:
        self._override: dict | None = None
        self._expires_at: float = 0.0

    def set_override(self, state: str, seconds: int, now: float) -> dict:
        if state not in VALID_STATES:
            raise ValueError(f"state must be one of {VALID_STATES}")
        if not 1 <= seconds <= 86400:
            raise ValueError("seconds must be between 1 and 86400")
        self._override = {"state": state, "expires_in_seconds": seconds}
        self._expires_at = now + min(seconds, self.ttl_seconds)
        return dict(self._override)

    def current(self, now: float) -> dict | None:
        if self._override is None:
            return None
        if now >= self._expires_at:
            self._override = None
            return None
        return dict(self._override)

    def clear(self) -> None:
        self._override = None
        self._expires_at = 0.0

    def effective_state(self, default: str = "quiet", now: float = 0.0) -> str:
        override = self.current(now)
        return override["state"] if override else default
