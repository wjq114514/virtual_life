"""DankMaterialShell presence probe.

DMS is used as a notification/status surface. It is not an attention source, so
``collect`` is intentionally empty; the adapter only answers whether the shell
is reachable so integrations can decide whether to register a surface.
"""

from __future__ import annotations

from . import AttentionObservation

DMS_BUS_NAME = "org.danklinux.DankMaterialShell"


class DmsAdapter:
    name = "dms"

    def available(self) -> bool:
        try:
            import dbus_next  # noqa: F401
        except ImportError:
            return False
        return True

    async def reachable(self) -> bool:
        try:
            from dbus_next.aio import MessageBus
        except ImportError:
            return False
        bus = await MessageBus().connect()
        try:
            reply = await bus.call(_name_has_owner_request())
            return bool(reply.body[0])
        finally:
            bus.disconnect()

    async def collect(self) -> list[AttentionObservation]:
        return []


def _name_has_owner_request():
    from dbus_next import Message

    return Message(
        destination="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        member="NameHasOwner",
        signature="s",
        body=[DMS_BUS_NAME],
    )
