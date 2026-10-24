#!/usr/bin/env python3
"""Live smoke test of the Linux client against a running runtime.

Usage:
    VIRTUAL_LIFE_CONFIG=~/.config/virtual-life/linux/config.toml \
        uv run --project clients/linux-node python examples/linux_desktop_live_test.py

Registers the configured node, opens and heartbeats a terminal presentation,
then releases it. Requires a reachable server and a valid owner token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "linux-node" / "src"))

from virtual_life_linux.config import load_config  # noqa: E402
from virtual_life_linux.transport import ServerTransport  # noqa: E402


async def main(config_path: Path) -> int:
    config = load_config(config_path)
    token = config.load_token()
    transport = ServerTransport(config.normalized_server_url, token)
    try:
        node = await transport.register_node(
            node_id=config.node_id,
            label=config.label,
            capabilities=["device.state.report", "text.display"],
            device_id=config.device_id,
        )
        print("registered:", json.dumps({"node_id": node["node_id"], "online": node["online"]}))
        await transport.heartbeat(config.node_id, {"client": "linux-live-test"})

        presentation_id = uuid.uuid4().hex
        snapshot = await transport.open_presentation(config.node_id, presentation_id)
        print("presentation:", json.dumps(snapshot))
        await transport.heartbeat_presentation(config.node_id, presentation_id)
        released = await transport.release_presentation(config.node_id, presentation_id)
        print("released:", json.dumps(released))
    finally:
        await transport.aclose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    default = os.environ.get("VIRTUAL_LIFE_CONFIG")
    parser.add_argument("--config", type=Path, default=Path(default) if default else None)
    args = parser.parse_args()
    if args.config is None:
        raise SystemExit("pass --config or set VIRTUAL_LIFE_CONFIG")
    raise SystemExit(asyncio.run(main(args.config)))
