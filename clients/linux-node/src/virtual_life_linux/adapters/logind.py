"""logind session attention source (idle/locked hints only)."""

from __future__ import annotations

from . import AttentionObservation


def parse_session_properties(properties: dict) -> AttentionObservation:
    """Translate the raw logind property map into a bounded observation."""
    return AttentionObservation(
        source="logind",
        kind="session",
        data={
            "idle_hint": bool(properties.get("IdleHint", False)),
            "locked_hint": bool(properties.get("LockedHint", False)),
        },
    )


class LogindAdapter:
    name = "logind"

    def available(self) -> bool:
        try:
            import dbus_next  # noqa: F401
        except ImportError:
            return False
        return True

    async def collect(self) -> list[AttentionObservation]:
        try:
            from dbus_next.aio import MessageBus
        except ImportError:
            return []
        bus = await MessageBus().connect()
        try:
            introspection = await bus.introspect(
                "org.freedesktop.login1", "/org/freedesktop/login1"
            )
            proxy = bus.get_proxy_object(
                "org.freedesktop.login1", "/org/freedesktop/login1", introspection
            )
            manager = proxy.get_interface("org.freedesktop.login1.Manager")
            properties = {}
            for key in ("IdleHint", "LockedHint"):
                getter = getattr(manager, f"get_{_snake(key)}", None)
                if getter is not None:
                    properties[key] = await getter()
            return [parse_session_properties(properties)]
        finally:
            bus.disconnect()


def _snake(pascal: str) -> str:
    out = []
    for index, char in enumerate(pascal):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)
