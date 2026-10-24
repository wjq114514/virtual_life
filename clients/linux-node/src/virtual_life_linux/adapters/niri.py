"""Niri compositor attention source.

Only structural data is read: workspace ids/indices and output names. Named
workspaces and window titles are never sent.
"""

from __future__ import annotations

import asyncio
import json
import shutil

from . import AttentionObservation


def parse_workspaces(workspaces: list[dict]) -> AttentionObservation | None:
    focused = next((item for item in workspaces if item.get("is_focused")), None)
    if focused is None:
        return None
    return AttentionObservation(
        source="niri",
        kind="workspace",
        data={
            "workspace_id": focused.get("id"),
            "workspace_idx": focused.get("idx"),
            "output": focused.get("output"),
        },
    )


class NiriAdapter:
    name = "niri"

    def __init__(self, binary: str = "niri"):
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def _json(self, *args: str) -> object:
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "msg",
            "--json",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"niri msg {' '.join(args)} failed")
        return json.loads(stdout.decode("utf-8"))

    async def collect(self) -> list[AttentionObservation]:
        workspaces = await self._json("workspaces")
        if not isinstance(workspaces, list):
            return []
        observation = parse_workspaces(workspaces)
        return [observation] if observation is not None else []
