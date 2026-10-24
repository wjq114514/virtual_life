"""Screen capture for the niri/Wayland desktop.

The default backend shells out to ``grim`` (niri implements wlr-screencopy, which
grim speaks). Capture is isolated behind a tiny protocol so the upload loop can
be tested without a compositor.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from typing import Protocol

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    data: bytes
    width: int
    height: int
    media_type: str = "image/png"


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG IHDR chunk without any image library."""

    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise CaptureError("capture did not return a PNG image")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise CaptureError("capture returned an invalid image size")
    return width, height


class ScreenshotCapture(Protocol):
    name: str

    def available(self) -> bool: ...

    async def capture(self) -> CapturedFrame: ...


class GrimCapture:
    name = "grim"

    def __init__(self, binary: str = "grim"):
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def capture(self) -> CapturedFrame:
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "-t",
                "png",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise CaptureError(f"failed to launch {self.binary}: {exc}") from exc
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:200]
            raise CaptureError(f"{self.binary} failed: {detail or process.returncode}")
        if not stdout:
            raise CaptureError(f"{self.binary} returned no image")
        width, height = png_dimensions(stdout)
        return CapturedFrame(data=stdout, width=width, height=height)
