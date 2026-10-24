"""Bounded PipeWire raw-PCM capture and playback."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioDevice:
    node_id: str
    name: str
    direction: str


async def list_pipewire_devices(binary: str = "pw-dump") -> dict:
    executable = shutil.which(binary)
    if executable is None:
        return {
            "available": False,
            "binary": binary,
            "inputs": [],
            "outputs": [],
            "aec": "unknown",
            "error": f"{binary} is not installed",
        }
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, TimeoutError) as exc:
        return {
            "available": False,
            "binary": binary,
            "inputs": [],
            "outputs": [],
            "aec": "unknown",
            "error": str(exc),
        }
    if process.returncode != 0:
        return {
            "available": False,
            "binary": binary,
            "inputs": [],
            "outputs": [],
            "aec": "unknown",
            "error": stderr.decode("utf-8", "replace").strip() or "pw-dump failed",
        }
    try:
        objects = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "binary": binary,
            "inputs": [],
            "outputs": [],
            "aec": "unknown",
            "error": f"pw-dump returned invalid JSON: {exc}",
        }
    inputs: list[dict] = []
    outputs: list[dict] = []
    if not isinstance(objects, list):
        raise AudioError("pw-dump JSON root must be an array")
    for item in objects:
        if not isinstance(item, dict):
            continue
        info = item.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if not isinstance(props, dict):
            continue
        media_class = props.get("media.class")
        device = {
            "id": str(item.get("id", "")),
            "name": str(props.get("node.description") or props.get("node.nick") or "Unnamed"),
            "node_name": str(props.get("node.name") or ""),
        }
        if media_class == "Audio/Source":
            inputs.append(device)
        elif media_class == "Audio/Sink":
            outputs.append(device)
    return {
        "available": True,
        "binary": binary,
        "inputs": inputs,
        "outputs": outputs,
        "aec": "unverified",
        "note": "AEC availability and acoustic performance have not been verified",
    }


class PipeWireCapture:
    def __init__(self, process: asyncio.subprocess.Process, *, frame_bytes: int = 1024):
        self.process = process
        self.frame_bytes = frame_bytes
        self._stopped = False

    @classmethod
    async def start(
        cls,
        *,
        binary: str = "pw-cat",
        device_id: str | None = None,
        sample_rate: int = 16_000,
        frame_bytes: int = 1024,
    ) -> "PipeWireCapture":
        executable = shutil.which(binary)
        if executable is None:
            raise AudioError(f"{binary} is not installed; install PipeWire client tools")
        if sample_rate != 16_000 or frame_bytes != 1024:
            raise ValueError("capture requires 16 kHz mono PCM16 frames of 512 samples")
        command = [
            executable,
            "--record",
            "--raw",
            "--rate=16000",
            "--channels=1",
            "--format=s16",
            "--latency=50ms",
        ]
        if device_id:
            command.append(f"--target={device_id}")
        command.append("-")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioError(f"could not start PipeWire capture: {exc}") from exc
        if process.stdout is None:
            process.kill()
            raise AudioError("PipeWire capture did not provide a PCM stream")
        return cls(process, frame_bytes=frame_bytes)

    async def read_frame(self) -> bytes:
        if self._stopped or self.process.stdout is None:
            raise AudioError("capture is stopped")
        try:
            frame = await self.process.stdout.readexactly(self.frame_bytes)
        except asyncio.IncompleteReadError as exc:
            detail = ""
            if self.process.stderr is not None and self.process.returncode is not None:
                detail = (await self.process.stderr.read()).decode("utf-8", "replace").strip()
            raise AudioError(detail or "PipeWire capture ended unexpectedly") from exc
        return frame

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


class PipeWirePlayer:
    def __init__(self, *, binary: str = "pw-cat", max_bytes: int = 384_000):
        self.binary = binary
        self.max_bytes = max_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False

    @property
    def playing(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def play(self, pcm: bytes, *, sample_rate: int, device_id: str | None = None) -> int:
        if not pcm or len(pcm) % 2:
            raise AudioError("playback requires nonempty PCM16 data")
        if len(pcm) > self.max_bytes:
            raise AudioError("audio segment exceeds the configured playback bound")
        if not 8_000 <= sample_rate <= 96_000:
            raise AudioError("audio sample rate is outside 8-96 kHz")
        executable = shutil.which(self.binary)
        if executable is None:
            raise AudioError(f"{self.binary} is not installed; install PipeWire client tools")
        command = [
            executable,
            "--playback",
            "--raw",
            f"--rate={sample_rate}",
            "--channels=1",
            "--format=s16",
            "--latency=50ms",
        ]
        if device_id:
            command.append(f"--target={device_id}")
        command.append("-")
        self._stopping = False
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioError(f"could not start PipeWire playback: {exc}") from exc
        self._process = process
        if process.stdin is None:
            await self.stop()
            raise AudioError("PipeWire playback did not provide an input stream")
        try:
            for offset in range(0, len(pcm), 8192):
                if self._stopping or process.returncode is not None:
                    raise AudioError("playback was interrupted")
                process.stdin.write(pcm[offset : offset + 8192])
                await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
            return_code = await process.wait()
            if return_code != 0:
                detail = (await process.stderr.read()).decode("utf-8", "replace").strip()
                raise AudioError(detail or f"pw-cat exited with status {return_code}")
            return len(pcm) // 2
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            if self._process is process:
                self._process = None

    async def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            process.kill()
            await process.wait()
