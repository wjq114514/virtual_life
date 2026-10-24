"""Start the runtime using the unified server and service configuration."""

import argparse
import logging
from pathlib import Path

import uvicorn

from .api import create_app
from .log import configure_logging
from .settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Virtual Life runtime")
    parser.add_argument("--config", type=Path, help="Path to a TOML configuration file")
    arguments = parser.parse_args()
    try:
        settings = Settings.load(arguments.config)
    except ValueError as exc:
        parser.error(str(exc))
    configure_logging(settings)
    logging.getLogger("virtual_life").info(
        "Starting Virtual Life on %s:%s", settings.server_host, settings.server_port
    )
    uvicorn.run(
        create_app(settings),
        host=settings.server_host,
        port=settings.server_port,
        log_config=None,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
