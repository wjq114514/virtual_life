"""Command-line entry point for the Matrix node bridge."""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .api import ApiClient
from .config import ConfigError, MatrixConfig, load_config, read_token
from .e2ee_bridge import E2eeBridge, SidecarError
from .matrix import MatrixClient
from .runtime import MatrixRuntime

LOGGER = logging.getLogger(__name__)
VERSION = "0.1.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bridge a Matrix account into Virtual Life.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.version_info < (3, 12):
        LOGGER.error("matrix_node_stopped reason=python_312_required")
        return 2
    try:
        config: MatrixConfig = load_config(args.config)
        if args.check_config:
            return 0
        token = read_token(config)
    except ConfigError as exc:
        LOGGER.error("configuration_invalid reason=%s", exc)
        return 2
    api = ApiClient(config.server_url, token, config.request_timeout_seconds)
    client = MatrixClient(
        config.homeserver,
        config.user_id,
        auth_method=config.auth_method,
        password=config.password,
        access_token=config.access_token,
        device_name=config.device_name,
        timeout_seconds=config.request_timeout_seconds,
    )
    bridge = None
    if config.e2ee:
        sidecar_dir = (
            Path(config.e2ee_sidecar)
            if config.e2ee_sidecar
            else Path(__file__).resolve().parent.parent / "e2ee"
        )
        e2ee_cfg = dict(config.e2ee_config or {})
        e2ee_cfg.setdefault("baseUrl", config.homeserver)
        e2ee_cfg.setdefault("userId", config.user_id)
        e2ee_cfg.setdefault("accessToken", config.access_token or "")
        bridge = E2eeBridge(sidecar_dir, config=e2ee_cfg)
        try:
            bridge.start()
        except SidecarError as exc:
            LOGGER.error("e2ee_sidecar_start_failed reason=%s", exc)
            return 3
    runtime = MatrixRuntime(config, api, client, e2ee_bridge=bridge)
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
        if bridge is not None:
            bridge.stop()
        LOGGER.info("matrix_node_stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
