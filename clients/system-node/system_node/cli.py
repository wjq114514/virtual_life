"""Command-line entry point; --once makes real API writes."""

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config, read_token
from .runtime import Runtime
from .transport import ApiClient, TransportError

LOGGER = logging.getLogger(__name__)
VERSION = "0.1.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report local or configured gateway device state.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--config", required=True, type=Path, help="Path to a JSON configuration file"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Register, sample, heartbeat and submit observations once (makes real API writes)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.version_info < (3, 12):
        LOGGER.error("reporter_stopped reason=python_312_required")
        return 2
    try:
        config = load_config(args.config)
        if args.check_config:
            return 0
        token = read_token(config)
    except ConfigError as exc:
        LOGGER.error("configuration_invalid reason=%s", exc)
        return 2
    lock = None
    if args.state_dir is not None:
        try:
            import fcntl

            lock = os.open(
                args.state_dir / "reporter.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            if lock is not None:
                os.close(lock)
            LOGGER.error("reporter_stopped reason=state_locked")
            return 2
    stop = threading.Event()
    previous_handlers = {}

    def request_stop(_signum, _frame):
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)
    try:
        api = ApiClient(config.server_url, token, config.request_timeout_seconds)
        return Runtime(config, api, stop).run(once=args.once)
    except TransportError as exc:
        LOGGER.error("reporter_stopped reason=%s status=%s", exc.code, exc.status)
        return 3 if exc.status in {401, 403} else 4
    except KeyboardInterrupt:
        return 0
    finally:
        if lock is not None:
            os.close(lock)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
