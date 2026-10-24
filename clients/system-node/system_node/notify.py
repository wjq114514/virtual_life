"""Send systemd readiness only when the caller has completed real reporting."""

import logging
import os
import socket

LOGGER = logging.getLogger(__name__)


def notify_ready() -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.settimeout(0.5)
            channel.sendto(b"READY=1", address)
    except (OSError, ValueError):
        LOGGER.warning("readiness_notification_failed")
        return False
    return True
