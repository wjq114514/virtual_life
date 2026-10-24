"""Client-side screen-vision session for the Linux host.

Owns one continuous (or one-shot) capture session: it creates the server session,
captures frames, registers + uploads image content, and submits frames carrying
the niri window scene. It never uses a non-owner token and never touches the
database; it is driven by the daemon, which holds the owner token.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .capture import CaptureError, ScreenshotCapture

FALLBACK_INTERVAL_SECONDS = 5.0
MAX_INTERVAL_SECONDS = 30.0


class ScreenTransport(Protocol):
    async def screen_create_session(self, node_id: str, payload: dict) -> dict: ...

    async def screen_stop_session(self, node_id: str, session_id: str, command_id: str) -> dict: ...

    async def screen_register_upload(self, node_id: str, payload: dict) -> dict: ...

    async def screen_upload_content(
        self, node_id: str, upload_id: str, data: bytes, media_type: str
    ) -> dict: ...

    async def screen_submit_frame(self, node_id: str, payload: dict) -> dict: ...


@dataclass
class ScreenState:
    running: bool = False
    companion_id: str | None = None
    session_id: str | None = None
    generation: int | None = None
    sequence: int = 0
    uploads: int = 0
    frames: int = 0
    last_frame_id: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "companion_id": self.companion_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "sequence": self.sequence,
            "uploads": self.uploads,
            "frames": self.frames,
            "last_frame_id": self.last_frame_id,
            "last_error": self.last_error,
        }


class ScreenStreamer:
    def __init__(
        self,
        transport: ScreenTransport,
        node_id: str,
        companion_id: str,
        *,
        profile: str = "balanced",
        mode: str = "continuous",
        display_id: str = "default",
        capture: ScreenshotCapture | None = None,
        scene_collector=None,
        capture_source: str = "other",
        user_initiated: bool = True,
    ) -> None:
        self.transport = transport
        self.node_id = node_id
        self.companion_id = companion_id
        self.profile = profile
        self.mode = mode
        self.display_id = display_id
        self.capture = capture
        self.scene_collector = scene_collector
        self.capture_source = capture_source
        self.user_initiated = user_initiated
        self.state = ScreenState()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._interval = FALLBACK_INTERVAL_SECONDS
        self._last_digest: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return self.state.as_dict()

    async def start(self) -> dict:
        if self.running:
            raise RuntimeError("a screen session is already running")
        if self.capture is None or not self.capture.available():
            raise RuntimeError("no screenshot backend is available")
        payload = {
            "command_id": uuid.uuid4().hex,
            "schema_version": "1.0",
            "companion_id": self.companion_id,
            "subject_node_id": self.node_id,
            "display_id": self.display_id,
            "mode": self.mode,
            "user_initiated": self.user_initiated,
            "capture_source": self.capture_source,
            "requested_profile": self.profile,
        }
        receipt = await self.transport.screen_create_session(self.node_id, payload)
        self.state = ScreenState(
            running=True,
            companion_id=self.companion_id,
            session_id=receipt["session_id"],
            generation=receipt["generation"],
        )
        self._interval = self._interval_from(receipt.get("advice") or {})
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        return self.status()

    def _interval_from(self, advice: dict) -> float:
        sample = advice.get("sample_interval_ms")
        minimum = advice.get("min_upload_interval_ms")
        seconds = FALLBACK_INTERVAL_SECONDS
        if isinstance(sample, (int, float)) and sample > 0:
            seconds = float(sample) / 1000.0
        if isinstance(minimum, (int, float)) and minimum > 0:
            seconds = max(seconds, float(minimum) / 1000.0)
        return min(max(seconds, 0.5), MAX_INTERVAL_SECONDS)

    async def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    frame = await self.capture.capture()
                    await self._publish(frame)
                    self.state.last_error = None
                except CaptureError as exc:
                    self.state.last_error = str(exc)
                    await self._submit_unavailable("capture_failed")
                if self.mode == "one_shot" and self.state.frames:
                    break
                await self._sleep(self._interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state.last_error = str(exc)
        finally:
            self.state.running = False

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _publish(self, frame) -> None:
        digest = hashlib.sha256(frame.data).hexdigest()
        if self.state.sequence == 0:
            trigger = "first_frame"
        elif digest != self._last_digest:
            trigger = "changed"
        else:
            trigger = "refresh"
        register = await self.transport.screen_register_upload(
            self.node_id,
            {
                "command_id": uuid.uuid4().hex,
                "session_id": self.state.session_id,
                "generation": self.state.generation,
                "media_type": frame.media_type,
                "byte_length": len(frame.data),
                "sha256": digest,
                "width": frame.width,
                "height": frame.height,
            },
        )
        upload_id = register["upload_id"]
        await self.transport.screen_upload_content(
            self.node_id, upload_id, frame.data, frame.media_type
        )
        self.state.uploads += 1
        client_features, host_metadata = await self._scene()
        body = {
            "command_id": uuid.uuid4().hex,
            "schema_version": "1.0",
            "session_id": self.state.session_id,
            "generation": self.state.generation,
            "sequence": self.state.sequence,
            "sampled_at": _utc_now(),
            "ttl_seconds": 90,
            "trigger": trigger,
            "input": {
                "kind": "image",
                "upload_id": upload_id,
                "capture_width": frame.width,
                "capture_height": frame.height,
                "rotation_degrees": 0,
            },
        }
        if client_features is not None:
            body["client_features"] = client_features
        if host_metadata:
            body["host_metadata"] = host_metadata
        receipt = await self.transport.screen_submit_frame(self.node_id, body)
        self._advance(receipt.get("frame_id"), digest)

    async def _submit_unavailable(self, reason: str) -> None:
        body = {
            "command_id": uuid.uuid4().hex,
            "schema_version": "1.0",
            "session_id": self.state.session_id,
            "generation": self.state.generation,
            "sequence": self.state.sequence,
            "sampled_at": _utc_now(),
            "ttl_seconds": 90,
            "trigger": "changed",
            "input": {"kind": "unavailable", "reason": reason},
        }
        try:
            receipt = await self.transport.screen_submit_frame(self.node_id, body)
        except Exception:
            return
        self._advance(receipt.get("frame_id"), None)

    def _advance(self, frame_id, digest: str | None) -> None:
        self.state.frames += 1
        self.state.sequence += 1
        if frame_id:
            self.state.last_frame_id = frame_id
        if digest is not None:
            self._last_digest = digest

    async def _scene(self):
        if self.scene_collector is None:
            return None, None
        try:
            scene = await self.scene_collector.collect()
        except Exception:
            return None, None
        return scene.client_features, (scene.host_metadata or None)

    async def stop(self) -> dict:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        session_id = self.state.session_id
        if session_id:
            try:
                await self.transport.screen_stop_session(self.node_id, session_id, uuid.uuid4().hex)
            except Exception as exc:
                self.state.last_error = str(exc)
        self.state.running = False
        return self.status()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
