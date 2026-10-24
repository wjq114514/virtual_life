"""Desktop attention adapters.

Attention adapters are opt-in, read-only and never collect user content (window
titles, message bodies, shell arguments); they emit small, bounded structures
that the daemon forwards as attention observations.

The separate niri scene collector (``adapters.niri_scene``) exists only to feed
the server-side vision prompt. It is also opt-in and, by default, sends app ids
only; window titles require ``desktop.scene_titles_enabled`` and are sanitized
before they can leave the host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AttentionObservation:
    source: str
    kind: str
    data: dict = field(default_factory=dict)


class AttentionAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    async def collect(self) -> list[AttentionObservation]: ...


async def collect_all(adapters: list[AttentionAdapter]) -> list[AttentionObservation]:
    observations: list[AttentionObservation] = []
    for adapter in adapters:
        if not adapter.available():
            continue
        try:
            observations.extend(await adapter.collect())
        except Exception:  # noqa: BLE001 - a broken adapter must not stop the others
            continue
    return observations
