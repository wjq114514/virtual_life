"""Command-line entry point for the QQ node bridge."""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .api import ApiClient
from .config import ConfigError, QQConfig, load_config, read_token
from .runtime import QQRuntime

LOGGER = logging.getLogger(__name__)
VERSION = "0.1.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bridge a QQ account into Virtual Life.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.version_info < (3, 12):
        LOGGER.error("qq_node_stopped reason=python_312_required")
        return 2
    try:
        config: QQConfig = load_config(args.config)
        if args.check_config:
            return 0
        token = read_token(config)
    except ConfigError as exc:
        LOGGER.error("configuration_invalid reason=%s", exc)
        return 2
    api = ApiClient(config.server_url, token, config.request_timeout_seconds)
    runtime = QQRuntime(config, api)
    stop = threading.Event()
    previous_handlers = {}

    def handle(signum, _frame):
        previous_handlers.get(signum, signal.SIG_DFL)(signum, _frame)
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.signal(signum, handle)
        except (ValueError, OSError):
            pass
    try:
        return runtime.run(stop)
    finally:
        LOGGER.info("qq_node_stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
