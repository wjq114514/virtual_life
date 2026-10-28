"""The per-device host daemon: the only native writer and owner-token holder."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .adapters import AttentionAdapter, AttentionObservation, collect_all
from .adapters.dms import DmsAdapter
from .adapters.logind import LogindAdapter
from .adapters.niri import NiriAdapter
from .adapters.niri_scene import NiriSceneCollector
from .adapters.niri_scene import from_desktop as scene_from_desktop
from .capture import GrimCapture
from .config import LinuxConfig
from .ipc import IpcError, IpcServer, IpcSession
from .lease import ClockUnavailable, LeaseGate
from .policy import AttentionPolicy
from .scene_classify import classify_activity, classify_focus
from .screen_client import ScreenStreamer
from .state import Renderer, build_node_context, build_status, now_iso
from .storage import ClientStorage
from .transport import ServerError, ServerTransport
from .voice.controller import VoiceController, VoiceControllerError

BASE_CAPABILITIES = ["device.state.report"]
DISPLAY_CAPABILITY = "text.display"
ATTENTION_CAPABILITY = "attention.observe"
VISION_CAPTURE_CAPABILITY = "vision.capture"


def _fingerprint(payload: dict) -> str:
    encoded = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SingleInstanceLock:
    """Advisory flock so only one daemon serves a runtime directory."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as exc:
            os.close(self._fd)
            self._fd = None
            raise IpcError(
                "already_running", "Another daemon owns this runtime directory", 409
            ) from exc
        os.ftruncate(self._fd, 0)
        os.write(self._fd, str(os.getpid()).encode("ascii"))

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


class _SceneObservationAdapter:
    """Adapter that replays the latest scene-derived attention observation.

    The daemon keeps a single observation built from the newest scene sample and
    hands it back to the attention loop, so the activity/focus classification
    flows into the attention sample without a second compositor query.
    """

    name = "scene"

    def __init__(self, observation: AttentionObservation) -> None:
        self.observation = observation

    def available(self) -> bool:
        return True

    async def collect(self) -> list[AttentionObservation]:
        return [self.observation]


class LinuxDaemon:
    def __init__(
        self,
        config: LinuxConfig,
        storage: ClientStorage,
        transport: ServerTransport,
        *,
        runtime_dir: Path,
        lease_gate: LeaseGate | None = None,
        generation: str | None = None,
        attention_source=None,
        scene_collector: NiriSceneCollector | None = None,
    ):
        self.config = config
        self.storage = storage
        self.transport = transport
        self.runtime_dir = Path(runtime_dir)
        self.lease_gate = lease_gate or LeaseGate()
        self.generation = generation or uuid.uuid4().hex
        self.attention_source = attention_source
        self.scene_collector = scene_collector
        self.attention_policy = AttentionPolicy(ttl_seconds=config.reporting.attention_ttl_seconds)
        self.attention_session_id = self._attention_session_id()
        stored = self.storage.get(f"attention:{self.attention_session_id}", "0")
        self.attention_sequence = int(stored) if stored and stored.isdigit() else 0
        self._attention_last = ""
        self._pending_boundary: dict | None = None
        self._scene_observation: AttentionObservation | None = None

        self.node: dict | None = None
        self.companions: list[dict] = []
        self.selected_companion: str | None = None
        self.renderer: Renderer | None = None
        self.lease: dict | None = None
        self.last_error: dict | None = None
        self.last_attention_error: dict | None = None
        self.screen_streamer: ScreenStreamer | None = None
        self.voice = VoiceController(
            config,
            transport,
            owner_context=self._voice_owner_context,
            authorization_error=self._voice_authorization_error,
            publish=self._publish_voice_event,
        )

        self._lock = SingleInstanceLock(self.runtime_dir / "daemon.lock")
        self._ipc: IpcServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._dialogue_workers: set[asyncio.Task] = set()

    @property
    def attention_override(self) -> dict | None:
        return self.attention_policy.current(time.monotonic())

    # -- lifecycle --------------------------------------------------------
    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / "daemon.sock"

    async def start(self) -> None:
        self._lock.acquire()
        self._ipc = IpcServer(self.socket_path, self.router(), generation=self.generation)
        await self._ipc.start()
        self._tasks.append(asyncio.create_task(self._register_and_heartbeat(), name="heartbeat"))
        self._tasks.append(asyncio.create_task(self._poll_events(), name="events"))
        self._tasks.append(asyncio.create_task(self._presentation_heartbeat(), name="presentation"))
        if self._attention_enabled():
            self._tasks.append(asyncio.create_task(self._attention_loop(), name="attention"))

    async def stop(self) -> None:
        self._stopping.set()
        with contextlib.suppress(Exception):
            await self.voice.stop("daemon_stopping")
        for task in self._tasks:
            task.cancel()
        for task in list(self._dialogue_workers):
            task.cancel()
        await asyncio.gather(*self._tasks, *self._dialogue_workers, return_exceptions=True)
        if self.screen_streamer is not None:
            with contextlib.suppress(Exception):
                await self.screen_streamer.stop()
            self.screen_streamer = None
        if self.renderer is not None:
            with contextlib.suppress(Exception):
                await self._release_presentation()
        if self._ipc is not None:
            await self._ipc.stop()
        await self.transport.aclose()
        self._lock.release()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await self._stopping.wait()
        finally:
            with contextlib.suppress(Exception):
                await self.stop()

    # -- capability projection -------------------------------------------
    def _attention_enabled(self) -> bool:
        return self.config.desktop.attention_enabled and self.config.desktop.backend != "none"

    def capabilities(self) -> list[str]:
        caps = list(BASE_CAPABILITIES)
        if self._attention_enabled():
            caps.append(ATTENTION_CAPABILITY)
        if self.renderer is not None and self.renderer.presentation_id is not None:
            caps.append(DISPLAY_CAPABILITY)
        if self.config.screen.enabled:
            caps.append(VISION_CAPTURE_CAPABILITY)
        return sorted(set(caps))

    # -- background loops -------------------------------------------------
    async def _register_and_heartbeat(self) -> None:
        await self._try_register()
        while not self._stopping.is_set():
            if self.node is None:
                # Registration may have failed at startup (server restart, DNS);
                # retry it before heartbeating so the capability projection is
                # advertised only once the node exists.
                await self._try_register()
            else:
                try:
                    self.node = await self.transport.heartbeat(
                        self.config.node_id, self._node_context()
                    )
                    self.last_error = None
                except Exception as exc:  # noqa: BLE001
                    self.last_error = {
                        "code": getattr(exc, "code", "heartbeat_failed"),
                        "message": str(exc),
                    }
            await self._sleep_or_stop(self.config.reporting.heartbeat_seconds)

    async def _try_register(self) -> None:
        try:
            self.node = await self.transport.register_node(
                node_id=self.config.node_id,
                label=self.config.label,
                capabilities=self.capabilities(),
                device_id=self.config.device_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.node = None
            self.last_error = {"code": getattr(exc, "code", "register_failed"), "message": str(exc)}

    def _node_context(self) -> dict:
        return build_node_context(
            generation=self.generation,
            renderer=self.renderer,
            attention_override=self.attention_override,
        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _poll_events(self) -> None:
        sequence, generation = self.storage.get_cursor()
        if generation != self.generation:
            sequence = 0
        while not self._stopping.is_set():
            try:
                page = await self.transport.events(sequence, limit=200)
            except Exception:  # noqa: BLE001
                await self._sleep_or_stop(2.0)
                continue
            for event in page.get("events", []):
                sequence = max(sequence, int(event.get("sequence", sequence)))
                await self._apply_event(event)
            self.storage.set_cursor(sequence, self.generation)
            await self._sleep_or_stop(1.0)

    async def _apply_event(self, event: dict) -> None:
        if self._ipc is not None:
            await self._ipc.publish("event", event)
        if event.get("type") == "node.transferred":
            await self.voice.stop("node_transferred", notify_server=False)
            if self.renderer is not None:
                await self._publish_state()

    # -- presentation ownership -------------------------------------------
    async def _presentation_heartbeat(self) -> None:
        """Keep the presentation live even when no renderer is attached.

        The daemon owns the presentation id, so a crashed terminal must not leave
        ``text.display`` advertised for a session the server already expired.
        """
        while not self._stopping.is_set():
            interval = self._presentation_interval()
            if self.renderer is not None and self.renderer.presentation_id is not None:
                await self._heartbeat_presentation_once()
            await self._sleep_or_stop(interval)

    def _presentation_interval(self) -> float:
        renderer = self.renderer
        if renderer is None or not renderer.ttl_seconds:
            return 5.0
        return min(5.0, max(1.0, renderer.ttl_seconds / 3.0))

    async def _heartbeat_presentation_once(self) -> None:
        renderer = self.renderer
        assert renderer is not None
        assert renderer.presentation_id is not None
        started_ns = self._lease_start()
        try:
            snapshot = await self.transport.heartbeat_presentation(
                self.config.node_id, renderer.presentation_id
            )
            renderer.expires_at = snapshot.get("expires_at")
            renderer.ttl_seconds = float(snapshot.get("ttl_seconds", renderer.ttl_seconds))
            renderer.render_deadline_ns = self._authorize(snapshot, started_ns)
            await self._publish_state()
        except ServerError as exc:
            await self.voice.stop("presentation_expired", notify_server=False)
            renderer.presentation_id = None
            renderer.render_deadline_ns = None
            self.last_error = {
                "code": getattr(exc, "code", "presentation_expired"),
                "message": str(exc),
            }
            await self._refresh_node_capabilities()
            await self._publish_state()

    # -- attention collection ---------------------------------------------
    def _attention_session_id(self) -> str:
        suffix = "-attn"
        base = self.config.node_id
        return base[: 128 - len(suffix)] + suffix

    def _adapters(self) -> list[AttentionAdapter]:
        if self.attention_source is not None:
            return list(self.attention_source)
        adapters: list[AttentionAdapter] = [NiriAdapter(), LogindAdapter()]
        if self.config.desktop.dms_enabled:
            adapters.append(DmsAdapter())
        if self._scene_observation is not None:
            adapters.append(_SceneObservationAdapter(self._scene_observation))
        return adapters

    async def _attention_loop(self) -> None:
        while not self._stopping.is_set():
            await self._sleep_or_stop(self.config.reporting.attention_seconds)
            if self._stopping.is_set():
                break
            try:
                await self._emit_attention_sample()
                self.last_attention_error = None
            except Exception as exc:  # noqa: BLE001 - a bad sample must not stop the loop
                self.last_attention_error = {
                    "code": getattr(exc, "code", "attention_failed"),
                    "message": str(exc),
                }

    def _attention_sample(self, observations: list, boundary: dict | None) -> dict:
        focus = "unknown"
        locked = None
        idle = None
        has_window = False
        app_id = ""
        title = ""
        for observation in observations:
            data = observation.data or {}
            if observation.kind == "session":
                if data.get("locked_hint") is not None:
                    locked = bool(data["locked_hint"])
                if data.get("idle_hint") is not None:
                    idle = bool(data["idle_hint"])
            elif observation.kind == "workspace" and data.get("workspace_id") is not None:
                focus = "focused"
            elif observation.kind == "scene":
                if data.get("has_focused_window") is not None:
                    has_window = bool(data["has_focused_window"])
                if data.get("app_id"):
                    app_id = str(data["app_id"])
                if data.get("title"):
                    title = str(data["title"])
        if has_window:
            focus = classify_focus(True)
        activity = "unknown"
        if app_id or title:
            activity = classify_activity(app_id, title)
        if locked:
            availability = "busy"
            user_present = False
        elif idle:
            availability = "available"
            user_present = None
        elif idle is False:
            availability = "busy"
            user_present = None
        else:
            availability = "unknown"
            user_present = None
        return {
            "schema_version": "1.0",
            "session_id": self.attention_session_id,
            "sequence": 0,
            "sampled_at": now_iso(),
            "ttl_seconds": min(self.config.reporting.attention_ttl_seconds, 300),
            "activity": activity,
            "focus": focus,
            "availability": availability,
            "user_present": user_present,
            "locked": locked,
            "meeting": activity == "meeting" or None,
            "screen_sharing": None,
            "boundary": boundary.get("boundary") if boundary else "none",
            "boundary_id": boundary.get("boundary_id") if boundary else None,
        }

    async def _refresh_scene_observation(self) -> None:
        """Refresh the cached scene observation from the niri collector."""
        if self.scene_collector is None:
            return
        try:
            scene = await self.scene_collector.collect()
        except Exception:  # noqa: BLE001 - a broken collector must not stop attention
            return
        focused = next(
            (item for item in scene.windows if isinstance(item, dict) and item.get("is_focused")),
            None,
        )
        has_window = focused is not None
        app_id = str(focused.get("app_id") or "") if focused else ""
        title = str(focused.get("title") or "") if focused else ""
        self._scene_observation = AttentionObservation(
            source="scene",
            kind="scene",
            data={
                "has_focused_window": has_window,
                "app_id": app_id,
                "title": title,
            },
        )

    @staticmethod
    def _sample_content(sample: dict) -> dict:
        volatile = {"sequence", "sampled_at", "ttl_seconds"}
        return {key: value for key, value in sample.items() if key not in volatile}

    async def _emit_attention_sample(self, boundary: dict | None = None) -> dict | None:
        """Send one bounded attention sample; return None when nothing changed."""
        candidate = boundary or self._pending_boundary
        await self._refresh_scene_observation()
        observations = await collect_all(self._adapters())
        sample = self._attention_sample(observations, candidate)
        if _fingerprint(self._sample_content(sample)) == self._attention_last:
            return None
        # The sequence is consumed before the request: the server requires a
        # strictly increasing sequence per session and rejects gaps nothing at
        # all, so a failed request must not be replayed with the same number.
        self.attention_sequence += 1
        sample["sequence"] = self.attention_sequence
        self.storage.set(f"attention:{self.attention_session_id}", str(self.attention_sequence))
        self._attention_last = _fingerprint(self._sample_content(sample))
        payload = {"command_id": uuid.uuid4().hex, "sample": sample}
        await self.transport.attention(self.config.node_id, payload)
        self._pending_boundary = None
        if self._ipc is not None:
            await self._ipc.publish("attention.sample", sample)
        return sample

    # -- IPC router -------------------------------------------------------
    def router(self) -> dict:
        return {
            "status.get": self._m_status_get,
            "status.subscribe": self._m_status_subscribe,
            "diagnostics.get": self._m_diagnostics_get,
            "renderer.attach": self._m_renderer_attach,
            "renderer.heartbeat": self._m_renderer_heartbeat,
            "renderer.detach": self._m_renderer_detach,
            "companion.list": self._m_companion_list,
            "companion.select": self._m_companion_select,
            "companion.connect": self._m_companion_connect,
            "companion.mode.set": self._m_companion_mode_set,
            "dialogue.send": self._m_dialogue_send,
            "dialogue.hide": self._m_dialogue_hide,
            "dialogue.receipt.get": self._m_dialogue_receipt,
            "dialogue.history.get": self._m_dialogue_history,
            "travel.prepare": self._m_travel_prepare,
            "travel.status": self._m_travel_status,
            "travel.abort": self._m_travel_abort,
            "attention.override.set": self._m_attention_override_set,
            "attention.override.clear": self._m_attention_override_clear,
            "attention.boundary": self._m_attention_boundary,
            "webui.open": self._m_webui_open,
            "shell.register": self._m_shell_register,
            "shell.started": self._m_shell_started,
            "shell.finished": self._m_shell_finished,
            "shell.closed": self._m_shell_closed,
            "screen.start": self._m_screen_start,
            "screen.stop": self._m_screen_stop,
            "screen.status": self._m_screen_status,
            "voice.doctor": self._m_voice_doctor,
            "voice.devices": self._m_voice_devices,
            "voice.start": self._m_voice_start,
            "voice.stop": self._m_voice_stop,
            "voice.status": self._m_voice_status,
            "voice.commit": self._m_voice_commit,
            "voice.interrupt": self._m_voice_interrupt,
        }

    async def _m_voice_doctor(self, params: dict, session: IpcSession) -> dict:
        return await self.voice.doctor()

    async def _m_voice_devices(self, params: dict, session: IpcSession) -> dict:
        return await self.voice.devices()

    async def _m_voice_start(self, params: dict, session: IpcSession) -> dict:
        try:
            result = await self.voice.start(params.get("mode", "continuous"))
        except VoiceControllerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code) from exc
        except ServerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code or 503) from exc
        await self._publish_state()
        return result

    async def _m_voice_stop(self, params: dict, session: IpcSession) -> dict:
        result = await self.voice.stop("user_stop")
        await self._publish_state()
        return result

    async def _m_voice_status(self, params: dict, session: IpcSession) -> dict:
        return self.voice.status()

    async def _m_voice_commit(self, params: dict, session: IpcSession) -> dict:
        text = params.get("text")
        if text is not None and not isinstance(text, str):
            raise IpcError("bad_request", "text must be a string", 400)
        try:
            return await self.voice.commit(text)
        except VoiceControllerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code) from exc

    async def _m_voice_interrupt(self, params: dict, session: IpcSession) -> dict:
        try:
            return await self.voice.interrupt()
        except VoiceControllerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code) from exc

    async def _publish_voice_event(self, event: str, data: dict) -> None:
        if self._ipc is not None:
            await self._ipc.publish(event, data)

    def _voice_authorization_error(self) -> str | None:
        if self.renderer is None or not self.renderer.presentation_id:
            return "a native presentation must be attached"
        if self.renderer.epoch is None or self.lease is None:
            return "the companion lease is not active"
        deadline = self.renderer.render_deadline_ns
        if deadline is None:
            return "the presentation lease has no trusted deadline"
        try:
            if deadline <= self.lease_gate.now_ns():
                return "the presentation lease expired"
        except ClockUnavailable as exc:
            return str(exc)
        return None

    async def _voice_owner_context(self) -> dict:
        reason = self._voice_authorization_error()
        if reason:
            raise IpcError("lease_expired", reason, 409)
        assert self.renderer is not None
        snapshot = await self.transport.get_companion(self.renderer.companion_id)
        if snapshot.get("active_node_id") not in (None, self.config.node_id):
            raise IpcError("companion_elsewhere", "the companion moved to another node", 409)
        epoch = int(snapshot.get("epoch", -1))
        if epoch != self.renderer.epoch:
            raise IpcError("lease_expired", "the companion epoch changed", 409)
        return {
            "companion_id": self.renderer.companion_id,
            "epoch": epoch,
            "revision": int(snapshot.get("revision", 0)),
            "presentation_id": self.renderer.presentation_id,
        }

    # -- screen vision ----------------------------------------------------
    async def _m_screen_start(self, params: dict, session: IpcSession) -> dict:
        if not self.config.screen.enabled:
            raise IpcError("screen_disabled", "Screen capture is disabled", 409)
        if self.screen_streamer is not None and self.screen_streamer.running:
            return self.screen_streamer.status()
        companion_id = (
            params.get("companion_id") or self.selected_companion or self.config.screen.companion_id
        )
        if not companion_id:
            raise IpcError("companion_required", "companion_id is required", 400)
        capture = GrimCapture(self.config.screen.capture_binary)
        if not capture.available():
            raise IpcError(
                "screen_capture_unavailable",
                f"{capture.binary} is not installed",
                503,
            )
        scene_collector = None
        if self.config.desktop.scene_enabled:
            candidate = scene_from_desktop(self.config.desktop)
            if candidate.available():
                scene_collector = candidate
                self.scene_collector = candidate
        streamer = ScreenStreamer(
            self.transport,
            self.config.node_id,
            companion_id,
            profile=params.get("profile") or self.config.screen.profile,
            mode=params.get("mode") or self.config.screen.mode,
            display_id=self.config.screen.display_id,
            capture=capture,
            scene_collector=scene_collector,
            capture_source=self.config.screen.capture_source,
        )
        try:
            result = await streamer.start()
        except ServerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code or 503) from exc
        self.screen_streamer = streamer
        await self._refresh_node_capabilities()
        return result

    async def _m_screen_stop(self, params: dict, session: IpcSession) -> dict:
        if self.screen_streamer is None:
            return {"running": False}
        result = await self.screen_streamer.stop()
        self.screen_streamer = None
        return result

    async def _m_screen_status(self, params: dict, session: IpcSession) -> dict:
        if self.screen_streamer is None:
            return {"running": False}
        return self.screen_streamer.status()

    # -- status -----------------------------------------------------------
    def _status(self) -> dict:
        payload = build_status(
            generation=self.generation,
            config=self.config,
            node=self.node,
            renderer=self.renderer,
            selected_companion=self.selected_companion,
            lease=self.lease,
            attention_override=self.attention_override,
            last_error=self.last_error,
        )
        payload["screen"] = (
            self.screen_streamer.status()
            if self.screen_streamer is not None
            else {"running": False}
        )
        payload["voice"] = self.voice.status()
        payload["attention"] = {
            "enabled": self._attention_enabled(),
            "session_id": self.attention_session_id,
            "sequence": self.attention_sequence,
            "last_error": self.last_attention_error,
        }
        return payload

    async def _m_status_get(self, params: dict, session: IpcSession) -> dict:
        return self._status()

    async def _m_status_subscribe(self, params: dict, session: IpcSession) -> dict:
        session.subscribe()
        await session.send_event("state", self._status(), self._ipc.sequence, self.generation)
        return {"subscribed": True, "generation": self.generation}

    async def _publish_state(self) -> None:
        if self._ipc is not None:
            await self._ipc.publish("state", self._status())

    async def _m_diagnostics_get(self, params: dict, session: IpcSession) -> dict:
        ctx = self._node_context()
        return {
            "node_context": ctx,
            "cursor": self.storage.get_cursor(),
            "connections": self._ipc._connections if self._ipc else 0,
            "pending_commands": [row.command_id for row in self.storage.pending_commands()],
            "attention": {
                "enabled": self._attention_enabled(),
                "session_id": self.attention_session_id,
                "sequence": self.attention_sequence,
                "adapters": [adapter.name for adapter in self._adapters()],
                "last_error": self.last_attention_error,
            },
            "last_error": self.last_error,
        }

    # -- renderer / presentation -----------------------------------------
    async def _establish_lease(self, companion_id: str) -> int:
        """Fetch the companion, renew the lease and bind it to the renderer."""
        snapshot = await self.transport.get_companion(companion_id)
        if snapshot.get("active_node_id") not in (None, self.config.node_id):
            raise IpcError(
                "companion_elsewhere",
                "This companion is active on another node; use travel first",
                409,
            )
        epoch = int(snapshot.get("epoch", 0))
        lease = await self.transport.renew_lease(companion_id, self.config.node_id, epoch)
        self.lease = lease
        started_ns = self._lease_start()
        if self.renderer is not None:
            self.renderer.companion_id = companion_id
            self.renderer.epoch = epoch
            self.renderer.render_deadline_ns = self._authorize(lease, started_ns)
        self.selected_companion = companion_id
        return epoch

    async def _m_renderer_attach(self, params: dict, session: IpcSession) -> dict:
        companion_id = params.get("companion_id") or self.selected_companion
        if not companion_id:
            raise IpcError("companion_required", "Select a companion before attaching", 409)
        if self.renderer is not None and self.renderer.renderer_id == params.get("renderer_id"):
            return self.renderer.as_dict()
        try:
            await self.transport.require_native_presentation()
        except ServerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code or 503) from exc
        try:
            # Resolve the companion before opening the presentation so a
            # cross-node companion fails fast instead of opening a session we
            # cannot bind to.
            lease_snapshot = await self.transport.get_companion(companion_id)
            if lease_snapshot.get("active_node_id") not in (None, self.config.node_id):
                raise IpcError(
                    "companion_elsewhere",
                    "This companion is active on another node; use travel first",
                    409,
                )
            epoch = int(lease_snapshot.get("epoch", 0))
            presentation_id = self.renderer.presentation_id if self.renderer else None
            if presentation_id is None:
                presentation_id = uuid.uuid4().hex
            snapshot = await self.transport.open_presentation(self.config.node_id, presentation_id)
            lease = await self.transport.renew_lease(companion_id, self.config.node_id, epoch)
            self.lease = lease
        except IpcError:
            raise
        except ServerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code) from exc
        started_ns = self._lease_start()
        self.renderer = Renderer(
            renderer_id=params.get("renderer_id") or uuid.uuid4().hex,
            generation=self.generation,
            node_id=self.config.node_id,
            companion_id=companion_id,
            epoch=epoch,
            presentation_id=snapshot["presentation_id"],
            opened_sequence=int(snapshot.get("opened_sequence", 0)),
            expires_at=snapshot.get("expires_at"),
            ttl_seconds=float(snapshot.get("ttl_seconds", 0.0)),
            render_deadline_ns=self._authorize(lease, started_ns),
        )
        self.selected_companion = companion_id
        await self._refresh_node_capabilities()
        await self._publish_state()
        return self.renderer.as_dict()

    def _lease_start(self) -> int:
        try:
            return self.lease_gate.start()
        except ClockUnavailable as exc:
            raise IpcError("clock_unavailable", str(exc), 503) from exc

    def _authorize(self, snapshot: dict, started_ns: int) -> int | None:
        authorization = self.lease_gate.authorize(
            started_ns,
            expires_at=snapshot["expires_at"],
            server_time=snapshot["server_time"],
            ttl_seconds=float(snapshot.get("ttl_seconds", 0.0)),
        )
        return authorization.deadline_ns if authorization else None

    async def _m_renderer_heartbeat(self, params: dict, session: IpcSession) -> dict:
        renderer = self._require_renderer(params)
        started_ns = self._lease_start()
        try:
            snapshot = await self.transport.heartbeat_presentation(
                self.config.node_id, renderer.presentation_id
            )
        except ServerError as exc:
            await self.voice.stop("presentation_expired", notify_server=False)
            renderer.presentation_id = None
            renderer.render_deadline_ns = None
            await self._publish_state()
            raise IpcError(exc.code, exc.message, exc.status_code) from exc
        renderer.expires_at = snapshot.get("expires_at")
        renderer.ttl_seconds = float(snapshot.get("ttl_seconds", renderer.ttl_seconds))
        renderer.render_deadline_ns = self._authorize(snapshot, started_ns)
        return renderer.as_dict()

    async def _m_renderer_detach(self, params: dict, session: IpcSession) -> dict:
        renderer = self._require_renderer(params)
        await self.voice.stop("presentation_detached")
        await self._release_presentation()
        self.renderer = None
        await self._refresh_node_capabilities()
        await self._publish_state()
        return {"detached": True, "renderer_id": renderer.renderer_id}

    def _require_renderer(self, params: dict) -> Renderer:
        renderer = self.renderer
        if renderer is None:
            raise IpcError("renderer_not_attached", "No renderer is attached", 409)
        expected_id = params.get("renderer_id")
        if expected_id is not None and expected_id != renderer.renderer_id:
            raise IpcError("renderer_replaced", "This renderer generation was replaced", 409)
        expected_gen = params.get("generation")
        if expected_gen is not None and expected_gen != renderer.generation:
            raise IpcError("renderer_replaced", "This renderer generation was replaced", 409)
        return renderer

    async def _release_presentation(self) -> None:
        if self.renderer is None or self.renderer.presentation_id is None:
            return
        with contextlib.suppress(ServerError):
            await self.transport.release_presentation(
                self.config.node_id, self.renderer.presentation_id
            )

    async def _refresh_node_capabilities(self) -> None:
        with contextlib.suppress(ServerError):
            self.node = await self.transport.register_node(
                node_id=self.config.node_id,
                label=self.config.label,
                capabilities=self.capabilities(),
                device_id=self.config.device_id,
            )

    # -- companions -------------------------------------------------------
    async def _m_companion_list(self, params: dict, session: IpcSession) -> dict:
        self.companions = await self.transport.list_companions()
        if self.selected_companion is None and self.companions:
            self.selected_companion = self.companions[0].get("companion_id")
        return {"companions": self.companions, "selected": self.selected_companion}

    async def _m_companion_select(self, params: dict, session: IpcSession) -> dict:
        companion_id = params.get("companion_id")
        if not companion_id:
            raise IpcError("companion_required", "companion_id is required", 400)
        self.selected_companion = companion_id
        await self._publish_state()
        return {"selected": companion_id}

    async def _m_companion_connect(self, params: dict, session: IpcSession) -> dict:
        companion_id = params.get("companion_id") or self.selected_companion
        if not companion_id:
            raise IpcError("companion_required", "companion_id is required", 400)
        if self.renderer is None:
            raise IpcError("renderer_required", "Attach a renderer before connecting", 409)
        if self.voice.active and self.voice.session.get("companion_id") != companion_id:
            await self.voice.stop("companion_changed")
        try:
            epoch = await self._establish_lease(companion_id)
        except ServerError as exc:
            raise IpcError(exc.code, exc.message, exc.status_code) from exc
        await self._publish_state()
        return {"companion_id": companion_id, "epoch": epoch, "lease": self.lease}

    async def _m_companion_mode_set(self, params: dict, session: IpcSession) -> dict:
        mode = params.get("mode")
        if mode not in ("quiet", "chat", "focus", "away"):
            raise IpcError("bad_mode", "mode must be quiet|chat|focus|away", 400)
        return {"mode": mode, "note": "context updates are sent through the coordinating node"}

    # -- dialogue ---------------------------------------------------------
    async def _m_dialogue_send(self, params: dict, session: IpcSession) -> dict:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise IpcError("empty_message", "text must be a nonempty string", 400)
        renderer = self._require_renderer(params)
        companion_id = (
            params.get("companion_id") or self.selected_companion or renderer.companion_id
        )
        if not companion_id or renderer.companion_id != companion_id:
            raise IpcError("companion_required", "Connect a companion before sending", 409)
        command_id = params.get("command_id") or uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "node_id": self.config.node_id,
            "epoch": renderer.epoch or 0,
            "expected_revision": params.get("expected_revision", 0),
            "user_message": text,
            "presentation_id": renderer.presentation_id,
        }
        existing = self.storage.get_command(command_id)
        if existing is None:
            self.storage.record_command(command_id, companion_id, _fingerprint(payload), now_iso())
        if self.renderer is not None:
            deadline = self.renderer.render_deadline_ns
            if deadline is None or deadline <= self._lease_gate_now():
                raise IpcError("lease_expired", "Output lease expired; reattach", 409)
        return await self._run_dialogue(companion_id, command_id, payload)

    def _lease_gate_now(self) -> int:
        try:
            return self.lease_gate.now_ns()
        except ClockUnavailable:
            return 2**63 - 1

    async def _run_dialogue(self, companion_id: str, command_id: str, payload: dict) -> dict:
        result: dict | None = None
        try:
            async for event, data in self.transport.stream_dialogue(companion_id, payload):
                if self._ipc is not None:
                    await self._ipc.publish(f"dialogue.{event}", data)
                if event == "result":
                    result = data
                elif event == "error":
                    error = data.get("error", {})
                    self.storage.finish_command(command_id, "failed", None, now_iso())
                    raise IpcError(
                        error.get("code", "dialogue_failed"),
                        error.get("message", "Dialogue failed"),
                        error.get("status_code"),
                    )
        except ServerError as exc:
            self.storage.finish_command(command_id, "failed", None, now_iso())
            if exc.code == "dialogue_context_changed":
                raise IpcError("dialogue_context_changed", exc.message, 409) from exc
            raise IpcError(exc.code, exc.message, exc.status_code) from exc
        if result is None:
            self.storage.finish_command(command_id, "unknown", None, now_iso())
            raise IpcError("result_unknown", "No receipt was produced", 503)
        self.storage.save_command_result(command_id, result, now_iso())
        return {"command_id": command_id, "result": result}

    async def _m_dialogue_hide(self, params: dict, session: IpcSession) -> dict:
        return {"hidden": params.get("command_id"), "cancelled": False}

    async def _m_dialogue_receipt(self, params: dict, session: IpcSession) -> dict:
        command_id = params.get("command_id")
        if not command_id:
            raise IpcError("bad_request", "command_id is required", 400)
        row = self.storage.get_command(command_id)
        if row is not None and row.result_json:
            return {"command_id": command_id, "result": json.loads(row.result_json)}
        companion_id = params.get("companion_id") or self.selected_companion
        result = await self.transport.dialogue_result(companion_id, command_id)
        self.storage.finish_command(command_id, "settled", None, now_iso())
        return {"command_id": command_id, "result": result}

    async def _m_dialogue_history(self, params: dict, session: IpcSession) -> dict:
        companion_id = params.get("companion_id") or self.selected_companion
        if not companion_id:
            raise IpcError("companion_required", "companion_id is required", 400)
        return await self.transport.dialogue_history(
            companion_id,
            before_sequence=params.get("before_sequence"),
            limit=int(params.get("limit", 20)),
        )

    # -- travel -----------------------------------------------------------
    async def _m_travel_prepare(self, params: dict, session: IpcSession) -> dict:
        companion_id = params.get("companion_id") or self.selected_companion
        target = params.get("target_node_id")
        if not companion_id or not target:
            raise IpcError("bad_request", "companion_id and target_node_id are required", 400)
        await self.voice.stop("travel_prepare")
        if self.renderer is not None:
            await self._release_presentation()
            self.renderer.presentation_id = None
            self.renderer.render_deadline_ns = None
        payload = {
            "command_id": params.get("command_id") or uuid.uuid4().hex,
            "target_node_id": target,
            "expected_revision": params.get("expected_revision", 0),
            "reason": params.get("reason", "manual"),
        }
        snapshot = await self.transport.prepare_transfer(companion_id, payload)
        transfer_id = snapshot["transfer_id"]
        result = await self.transport.ack_transfer(
            transfer_id, {"node_id": self.config.node_id, "phase": "released"}
        )
        return {"transfer": result}

    async def _m_travel_status(self, params: dict, session: IpcSession) -> dict:
        transfer_id = params.get("transfer_id")
        if not transfer_id:
            raise IpcError("bad_request", "transfer_id is required", 400)
        return await self.transport.get_transfer(transfer_id)

    async def _m_travel_abort(self, params: dict, session: IpcSession) -> dict:
        transfer_id = params.get("transfer_id")
        if not transfer_id:
            raise IpcError("bad_request", "transfer_id is required", 400)
        return await self.transport.abort_transfer(transfer_id)

    # -- attention --------------------------------------------------------
    async def _m_attention_override_set(self, params: dict, session: IpcSession) -> dict:
        state = params.get("state")
        seconds = int(params.get("seconds", 0))
        try:
            override = self.attention_policy.set_override(state, seconds, time.monotonic())
        except ValueError as exc:
            raise IpcError("bad_override", str(exc), 400) from exc
        await self._publish_state()
        return override

    async def _m_attention_override_clear(self, params: dict, session: IpcSession) -> dict:
        self.attention_policy.clear()
        await self._publish_state()
        return {"cleared": True}

    async def _m_attention_boundary(self, params: dict, session: IpcSession) -> dict:
        boundary = params.get("boundary")
        if boundary not in ("task_completed", "break_started", "user_returned"):
            raise IpcError("bad_boundary", "unknown boundary", 400)
        if not self._attention_enabled():
            raise IpcError("attention_disabled", "Attention observation is disabled", 409)
        self._pending_boundary = {"boundary": boundary, "boundary_id": uuid.uuid4().hex}
        sampled = False
        try:
            # A manual boundary is a user action: emit it immediately instead of
            # waiting for the next periodic sample.
            sampled = await self._emit_attention_sample() is not None
        except Exception as exc:  # noqa: BLE001 - keep the boundary queued for the loop
            self.last_attention_error = {
                "code": getattr(exc, "code", "attention_failed"),
                "message": str(exc),
            }
        return {"boundary": boundary, "accepted": True, "sampled": sampled}

    # -- misc -------------------------------------------------------------
    async def _m_webui_open(self, params: dict, session: IpcSession) -> dict:
        return {"url": self.config.normalized_server_url + "/ui/", "opened": False}

    async def _m_shell_register(self, params: dict, session: IpcSession) -> dict:
        if not self.config.desktop.fish_activity_enabled:
            raise IpcError("shell_activity_disabled", "Shell activity reporting is disabled", 409)
        session_id = params.get("session_id")
        if not session_id:
            raise IpcError("bad_request", "session_id is required", 400)
        return {"session_id": session_id, "registered": True}

    async def _m_shell_started(self, params: dict, session: IpcSession) -> dict:
        token = params.get("command_token")
        if not token:
            raise IpcError("bad_request", "command_token is required", 400)
        return {"accepted": True}

    async def _m_shell_finished(self, params: dict, session: IpcSession) -> dict:
        return {"accepted": True}

    async def _m_shell_closed(self, params: dict, session: IpcSession) -> dict:
        return {"closed": True}
