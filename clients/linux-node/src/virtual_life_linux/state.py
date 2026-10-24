"""Aggregated daemon state and the renderer/session record."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import LinuxConfig


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Renderer:
    renderer_id: str
    generation: str
    node_id: str
    companion_id: str | None = None
    epoch: int | None = None
    presentation_id: str | None = None
    opened_sequence: int = 0
    expires_at: str | None = None
    ttl_seconds: float = 0.0
    render_deadline_ns: int | None = None
    attached_at: str = field(default_factory=now_iso)

    def as_dict(self) -> dict:
        return {
            "renderer_id": self.renderer_id,
            "generation": self.generation,
            "node_id": self.node_id,
            "companion_id": self.companion_id,
            "epoch": self.epoch,
            "presentation_id": self.presentation_id,
            "opened_sequence": self.opened_sequence,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "render_deadline_ns": self.render_deadline_ns,
            "attached_at": self.attached_at,
        }


def build_status(
    *,
    generation: str,
    config: LinuxConfig,
    node: dict | None,
    renderer: Renderer | None,
    selected_companion: str | None,
    lease: dict | None,
    attention_override: dict | None,
    last_error: dict | None,
) -> dict:
    node = node or {}
    return {
        "generation": generation,
        "node_id": config.node_id,
        "server": {"url": config.normalized_server_url, "key": config.server_key},
        "online": bool(node.get("online")),
        "node": {
            "node_id": node.get("node_id"),
            "label": node.get("label"),
            "capabilities": node.get("capabilities", []),
        },
        "renderer": renderer.as_dict() if renderer else None,
        "presentation_id": renderer.presentation_id if renderer else None,
        "companion_id": selected_companion,
        "lease": lease,
        "attention_override": attention_override,
        "last_error": last_error,
        "server_time": now_iso(),
    }


def build_node_context(
    *,
    generation: str,
    renderer: Renderer | None,
    attention_override: dict | None,
) -> dict[str, Any]:
    context: dict[str, Any] = {"client": "virtual-life-linux", "generation": generation}
    if renderer is not None and renderer.companion_id:
        context["renderer"] = {"companion_id": renderer.companion_id}
    if attention_override is not None:
        context["attention_override"] = attention_override.get("state")
    return context
