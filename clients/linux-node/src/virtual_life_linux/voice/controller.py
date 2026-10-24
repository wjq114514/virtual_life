"""Daemon-owned voice session lifecycle and fenced audio output."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .asr import SherpaOnnxEngine
from .audio import AudioError, PipeWireCapture, PipeWirePlayer, list_pipewire_devices
from .manifest import ModelManifest, VoiceModelError
from .turns import TurnAction, TurnManager


class VoiceControllerError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class VoiceController:
    def __init__(
        self,
        config,
        transport,
        *,
        owner_context: Callable[[], Awaitable[dict]],
        authorization_error: Callable[[], str | None],
        publish: Callable[[str, dict], Awaitable[None]],
        lock_state: Callable[[], Awaitable[bool | None]] | None = None,
    ):
        self.config = config
        self.transport = transport
        self.owner_context = owner_context
        self.authorization_error = authorization_error
        self.publish = publish
        self.lock_state = lock_state or _session_locked
        self.session: dict | None = None
        self.mode: str | None = None
        self.engine: SherpaOnnxEngine | None = None
        self.capture: PipeWireCapture | None = None
        self.player = PipeWirePlayer(
            binary="pw-cat",
            max_bytes=96_000 * config.voice.max_buffered_audio_seconds,
        )
        self.turns: TurnManager | None = None
        self.output_generation: int | None = None
        self.current_turn_id: str | None = None
        self.current_turn_generation: int | None = None
        self.model_id: str | None = None
        self.asr_lock = asyncio.Lock()
        self.interrupt_task: asyncio.Task | None = None
        self.capture_task: asyncio.Task | None = None
        self.timer_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.watchdog_task: asyncio.Task | None = None
        self.lock_task: asyncio.Task | None = None
        self.turn_watch_tasks: dict[str, asyncio.Task] = {}
        self.interrupted_turns: set[str] = set()
        self.reply_text: dict[str, str] = {}
        self.dialogue_receipts: dict[str, dict] = {}
        self.last_error: dict | None = None
        self.state = "off"
        self.play_state = "idle"
        self._stopping = False

    @property
    def active(self) -> bool:
        return self.session is not None and self.session.get("status") == "active"

    def status(self) -> dict:
        return {
            "enabled": self.config.voice.enabled,
            "active": self.active,
            "mode": self.mode,
            "session_id": self.session.get("session_id") if self.session else None,
            "session_generation": self.session.get("session_generation") if self.session else None,
            "output_generation": self.output_generation,
            "state": self.state,
            "playback": self.play_state,
            "capture_active": self.capture is not None,
            "model_id": self.model_id,
            "aec": "unverified",
            "barge_in_mode": self.config.voice.barge_in_mode,
            "last_error": self.last_error,
        }

    async def devices(self) -> dict:
        report = await list_pipewire_devices()
        report["capture_binary_available"] = shutil.which("pw-cat") is not None
        report["playback_binary_available"] = report["capture_binary_available"]
        report["aec"] = "unverified" if report.get("available") else "unknown"
        return report

    async def doctor(self) -> dict:
        report = {
            "enabled": self.config.voice.enabled,
            "runtime_version_required": "1.13.8",
            "runtime_version": None,
            "model_manifest": str(self.config.voice_manifest_path)
            if self.config.voice_manifest_path
            else None,
            "model_id": None,
            "model_files_valid": False,
            "recognizer_loaded": False,
            "vad_loaded": False,
            "audio_capture_tool": shutil.which("pw-cat"),
            "audio_device_probe": shutil.which("pw-dump"),
            "audio_tested": False,
            "aec": "unverified",
            "errors": [],
        }
        manifest_path = self.config.voice_manifest_path
        if manifest_path is None:
            report["errors"].append("voice.asr_manifest is not configured")
        else:
            try:
                manifest = await asyncio.to_thread(ModelManifest.load, manifest_path)
                report["model_files_valid"] = True
                report["model_id"] = manifest.model_id
                report["runtime_version"] = await asyncio.to_thread(manifest.check_runtime)
                engine = SherpaOnnxEngine(manifest, num_threads=self.config.voice.model_threads)
                await asyncio.to_thread(engine.load)
                report["recognizer_loaded"] = engine._recognizer is not None
                report["vad_loaded"] = engine._vad is not None
            except (VoiceModelError, OSError, ValueError) as exc:
                report["errors"].append(str(exc))
        devices = await self.devices()
        report["pipewire"] = devices
        report["ready_for_voice"] = bool(
            self.config.voice.enabled
            and report["model_files_valid"]
            and report["recognizer_loaded"]
            and report["vad_loaded"]
            and report["audio_capture_tool"]
        )
        return report

    async def start(self, mode: str = "continuous") -> dict:
        if mode not in ("continuous", "push_to_talk", "text"):
            raise VoiceControllerError(
                "bad_voice_mode", "mode must be continuous, push_to_talk or text", 400
            )
        if not self.config.voice.enabled:
            raise VoiceControllerError("voice_disabled", "Enable voice.enabled in the Linux config")
        if self.active:
            if self.mode == mode:
                return self.status()
            raise VoiceControllerError("voice_session_busy", "Stop the active voice session first")
        if self.authorization_error():
            raise VoiceControllerError("lease_expired", self.authorization_error())

        owner = await self.owner_context()
        engine: SherpaOnnxEngine | None = None
        capture: PipeWireCapture | None = None
        if mode != "text":
            manifest_path = self.config.voice_manifest_path
            if manifest_path is None:
                raise VoiceControllerError(
                    "voice_model_missing",
                    "Set voice.asr_manifest to a verified local model manifest",
                    503,
                )
            try:
                manifest = await asyncio.to_thread(ModelManifest.load, manifest_path)
                engine = SherpaOnnxEngine(manifest, num_threads=self.config.voice.model_threads)
                await asyncio.to_thread(engine.load)
                capture = await PipeWireCapture.start(
                    device_id=self.config.voice.input_device or None
                )
            except (VoiceModelError, AudioError, OSError, ValueError) as exc:
                if capture is not None:
                    await capture.stop()
                raise VoiceControllerError("voice_unavailable", str(exc), 503) from exc

        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "companion_id": owner["companion_id"],
            "epoch": owner["epoch"],
            "expected_revision": owner["revision"],
            "presentation_id": owner["presentation_id"],
            "user_initiated": True,
            "mode": mode,
            "asr": (
                {
                    "engine": "sherpa_onnx",
                    "model_id": engine.manifest.model_id,
                    "language": engine.manifest.language,
                }
                if engine
                else None
            ),
            "output": {"audio_enabled": True, "voice_id": "default"},
        }
        try:
            snapshot = await self.transport.voice_create_session(self.config.node_id, payload)
        except Exception:
            if capture is not None:
                await capture.stop()
            raise
        if snapshot.get("status") != "active" or snapshot.get("node_id") != self.config.node_id:
            if capture is not None:
                await capture.stop()
            raise VoiceControllerError(
                "voice_session_invalid", "Server returned an inactive session", 502
            )

        self.session = snapshot
        self.mode = mode
        self.engine = engine
        self.capture = capture
        self.model_id = engine.manifest.model_id if engine else None
        self.output_generation = int(snapshot["output_generation"])
        self.turns = TurnManager(sample_rate=16_000, auto_commit=(mode == "continuous"))
        self.state = "text" if mode == "text" else "armed"
        self.play_state = "idle"
        self.last_error = None
        self._stopping = False
        if mode != "text":
            await self._apply_actions(self.turns.arm())
            self.capture_task = asyncio.create_task(self._capture_loop(), name="voice-capture")
            self.timer_task = asyncio.create_task(self._timer_loop(), name="voice-turn-timer")
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="voice-heartbeat")
        self.watchdog_task = asyncio.create_task(
            self._lease_watchdog(), name="voice-lease-watchdog"
        )
        self.lock_task = asyncio.create_task(self._lock_watchdog(), name="voice-lock-watchdog")
        await self._emit_state()
        return self.status()

    async def stop(self, reason: str = "user_stop", *, notify_server: bool = True) -> dict:
        session = self.session
        if not session:
            await self._stop_local(reason)
            return self.status()
        await self._stop_local(reason)
        self.session = {**session, "status": "stopped"}
        if notify_server:
            try:
                await self.transport.voice_stop_session(
                    self.config.node_id, session["session_id"], uuid.uuid4().hex
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = {
                    "code": getattr(exc, "code", "voice_stop_failed"),
                    "message": str(exc),
                }
        await self._emit_state()
        return self.status()

    async def commit(self, text: str | None = None) -> dict:
        if text is None:
            return await self._commit_push_to_talk()
        text = text.strip()
        if not text or len(text) > 4096:
            raise VoiceControllerError("bad_voice_text", "text must contain 1-4096 characters", 400)
        if not self.active or self.session is None:
            raise VoiceControllerError("voice_session_required", "Start a voice session first")
        if self.current_turn_id:
            await self._interrupt_current("text_input")
        now = datetime.now(UTC)
        ended_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return await self._submit_turn(
            {
                "turn_id": uuid.uuid4().hex,
                "text": text,
                "end_reason": "manual",
                "started_at": ended_at,
                "ended_at": ended_at,
                "audio_duration_ms": 0,
                "discontinuity": False,
                "continuation_of_turn_id": None,
                "manual_text": True,
            }
        )

    async def _commit_push_to_talk(self) -> dict:
        if not self.active or self.session is None:
            raise VoiceControllerError("voice_session_required", "Start a voice session first")
        if self.mode != "push_to_talk" or self.turns is None:
            raise VoiceControllerError(
                "bad_voice_text", "text is required unless the session is push-to-talk", 400
            )
        actions = self.turns.manual_commit(end_reason="push_to_talk")
        flushed = False
        for action in actions:
            if action.kind == "flush":
                flushed = True
                await self._flush_generation(action.generation)
            else:
                await self._apply_actions([action])
        return {"committed": flushed, "status": self.status()}

    async def interrupt(self) -> dict:
        if not self.active or self.current_turn_id is None:
            await self.player.stop()
            self.play_state = "stopped"
            return {"interrupted": False, "status": self.status()}
        receipt = await self._interrupt_current("stop_button")
        if self.turns is not None:
            await self._apply_actions(self.turns.reply_finished(self.current_turn_id or ""))
        return {"interrupted": receipt is not None, "receipt": receipt, "status": self.status()}

    async def _stop_local(self, reason: str) -> None:
        self._stopping = True
        await self.player.stop()
        self.play_state = "stopped"
        if self.capture is not None:
            await self.capture.stop()
            self.capture = None
        if self.turns is not None:
            self.turns.cancel(reason)
        current = asyncio.current_task()
        tasks = [
            self.capture_task,
            self.timer_task,
            self.heartbeat_task,
            self.watchdog_task,
            self.lock_task,
            self.interrupt_task,
            *self.turn_watch_tasks.values(),
        ]
        for task in tasks:
            if task is not None and task is not current and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not None and task is not current),
            return_exceptions=True,
        )
        self.capture_task = None
        self.timer_task = None
        self.heartbeat_task = None
        self.watchdog_task = None
        self.lock_task = None
        self.interrupt_task = None
        self.turn_watch_tasks.clear()
        self.state = reason

    async def _capture_loop(self) -> None:
        assert self.capture is not None and self.engine is not None and self.turns is not None
        try:
            while self.active and not self._stopping:
                pcm = await self.capture.read_frame()
                async with self.asr_lock:
                    active, updates = await asyncio.to_thread(self.engine.accept_pcm, pcm)
                sample_index = self.engine.sample_cursor
                actions = self.turns.on_audio_activity(active, sample_index=sample_index)
                for update in updates:
                    actions.extend(
                        self.turns.on_asr_event(
                            update.kind,
                            segment_id=update.segment_id,
                            revision=update.revision,
                            sample_start=update.sample_start,
                            text=update.text,
                        )
                    )
                await self._apply_actions(actions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._fail_closed("capture_failed", str(exc))

    async def _timer_loop(self) -> None:
        assert self.turns is not None
        try:
            while self.active and not self._stopping:
                await asyncio.sleep(0.05)
                await self._apply_actions(self.turns.tick())
        except asyncio.CancelledError:
            raise

    async def _apply_actions(self, actions: list[TurnAction]) -> None:
        for action in actions:
            if action.kind in {"partial", "candidate_end", "needs_confirmation"}:
                await self.publish(
                    f"voice.{action.kind}", {**action.data, "generation": action.generation}
                )
            elif action.kind == "state":
                self.state = action.data.get("state", self.state)
                await self._emit_state()
            elif action.kind == "flush":
                asyncio.create_task(
                    self._flush_generation(action.generation),
                    name=f"voice-flush-{action.generation}",
                )
            elif action.kind == "commit":
                asyncio.create_task(self._submit_turn(action.data), name="voice-submit-turn")
            elif action.kind == "barge_in":
                await self.player.stop()
                self.play_state = "stopped"
                target = action.data.get("target_turn_id")
                if target:
                    self.interrupted_turns.add(target)
                    watcher = self.turn_watch_tasks.get(target)
                    if watcher is not None and not watcher.done():
                        watcher.cancel()
                    self.interrupt_task = asyncio.create_task(
                        self._send_interrupt(
                            target,
                            action.data.get("output_generation"),
                            "user_speech",
                        ),
                        name="voice-barge-in-interrupt",
                    )
            elif action.kind == "idle_timeout":
                asyncio.create_task(self.stop("idle_timeout"), name="voice-idle-stop")
            elif action.kind == "cancel":
                self.last_error = {"code": action.data.get("reason", "cancelled")}

    async def _flush_generation(self, generation: int) -> None:
        if not self.engine or not self.turns or not self.active:
            return
        try:
            async with self.asr_lock:
                final_text, updates = await asyncio.to_thread(self.engine.flush)
            for update in updates:
                await self._apply_actions(
                    self.turns.on_asr_event(
                        update.kind,
                        segment_id=update.segment_id,
                        revision=update.revision,
                        sample_start=update.sample_start,
                        text=update.text,
                    )
                )
            await self._apply_actions(self.turns.flush_complete(generation, final_text=final_text))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = {"code": "asr_flush_failed", "message": str(exc)}
            if self.turns is not None and self.turns.generation == generation:
                await self._apply_actions(self.turns.cancel("asr_flush_failed"))
            await self._emit_state()

    async def _submit_turn(self, data: dict) -> dict:
        if not self.active or self.session is None:
            return {"submitted": False, "reason": "session_inactive"}
        if self.interrupt_task is not None:
            try:
                await self.interrupt_task
            except asyncio.CancelledError:
                return {"submitted": False, "reason": "interrupt_cancelled"}
        authorization_error = self.authorization_error()
        if authorization_error:
            await self.stop("lease_expired", notify_server=False)
            return {"submitted": False, "reason": authorization_error}
        try:
            owner = await self.owner_context()
        except Exception as exc:  # noqa: BLE001
            await self._fail_closed("owner_context_lost", str(exc))
            return {"submitted": False, "reason": str(exc)}
        if (
            owner["companion_id"] != self.session["companion_id"]
            or owner["epoch"] != self.session["epoch"]
        ):
            await self._fail_closed("owner_changed", "voice owner or epoch changed")
            return {"submitted": False, "reason": "owner_changed"}
        turn_id = data.get("turn_id") or uuid.uuid4().hex
        command_id = uuid.uuid4().hex
        is_manual_text = bool(data.get("manual_text"))
        payload = {
            "command_id": command_id,
            "turn_id": turn_id,
            "session_generation": self.session["session_generation"],
            "expected_output_generation": self.output_generation,
            "epoch": self.session["epoch"],
            "expected_revision": owner["revision"],
            "text": data["text"],
            "user_initiated": True,
            "end_reason": "manual"
            if is_manual_text
            else data.get("end_reason", "adaptive_endpoint"),
            "input": {"kind": "text", "mode": "manual"}
            if is_manual_text
            else {
                "kind": "asr",
                "started_at": data["started_at"],
                "ended_at": data["ended_at"],
                "audio_duration_ms": data["audio_duration_ms"],
                "asr_model_id": self.model_id,
                "language": "zh",
                "discontinuity": data.get("discontinuity", False),
            },
            "continuation_of_turn_id": data.get("continuation_of_turn_id"),
        }
        try:
            receipt = await self.transport.voice_submit_turn(
                self.config.node_id, self.session["session_id"], payload
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_closed("turn_submit_failed", str(exc))
            return {"submitted": False, "reason": str(exc)}
        if (
            receipt.get("turn_id") != turn_id
            or receipt.get("session_generation") != self.session["session_generation"]
        ):
            await self._fail_closed("turn_receipt_mismatch", "server returned a mismatched turn")
            return {"submitted": False, "reason": "turn_receipt_mismatch"}
        self.current_turn_id = turn_id
        self.current_turn_generation = int(receipt["output_generation"])
        self.output_generation = self.current_turn_generation
        watcher = asyncio.create_task(self._watch_turn(turn_id), name=f"voice-events-{turn_id}")
        self.turn_watch_tasks[turn_id] = watcher
        await self._emit_state()
        return {"submitted": True, "receipt": receipt}

    async def _watch_turn(self, turn_id: str) -> None:
        assert self.session is not None
        ended = False
        event_cursor = 0
        session_id = self.session["session_id"]
        try:
            async for event_name, data in self.transport.stream_voice_turn_events(
                self.config.node_id, session_id, turn_id
            ):
                if not self.active:
                    return
                if event_name != "snapshot":
                    sequence = data.get("sequence") if isinstance(data, dict) else None
                    if type(sequence) is int:
                        if sequence <= event_cursor:
                            continue
                        event_cursor = sequence
                if event_name == "voice_end":
                    ended = True
                    await self._handle_turn_end(turn_id)
                    return
                if event_name == "speech_segment":
                    await self._play_segment(turn_id, data)
                elif event_name == "text_delta":
                    if self._event_matches(turn_id, data):
                        text = data.get("text")
                        if not isinstance(text, str) or len(text) > 4096:
                            await self._fail_closed(
                                "voice_text_invalid", "reply text exceeds its bound"
                            )
                            return
                        self.reply_text[turn_id] = text
                        await self.publish(
                            "voice.text_delta",
                            {
                                "turn_id": turn_id,
                                "text": text,
                                "sequence": data.get("sequence"),
                                "output_generation": data.get("output_generation"),
                            },
                        )
                elif event_name == "dialogue_result":
                    if self._event_matches(turn_id, data):
                        receipt = _dialogue_receipt(data)
                        self.dialogue_receipts[turn_id] = receipt
                        if receipt.get("assistant_message"):
                            self.reply_text[turn_id] = receipt["assistant_message"]
                        await self.publish(
                            "voice.dialogue_result",
                            {"turn_id": turn_id, "receipt": receipt},
                        )
                elif event_name in ("status", "snapshot"):
                    await self.publish(
                        "voice.turn_event",
                        {"kind": event_name, "turn_id": turn_id, "data": _bounded_event(data)},
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if turn_id not in self.interrupted_turns and self.active:
                await self._fail_closed("voice_events_disconnected", str(exc))
        finally:
            if not ended and turn_id not in self.interrupted_turns and self.active:
                await self._fail_closed("voice_events_disconnected", "voice event stream ended")
            self.turn_watch_tasks.pop(turn_id, None)

    async def _play_segment(self, turn_id: str, data: dict) -> None:
        if not self._event_matches(turn_id, data):
            return
        sequence = data.get("segment_sequence")
        sample_rate = data.get("sample_rate")
        channels = data.get("channels")
        samples = data.get("sample_count")
        digest = data.get("sha256")
        if (
            type(sequence) is not int
            or sequence < 0
            or type(sample_rate) is not int
            or not 8_000 <= sample_rate <= 96_000
            or channels != 1
            or type(samples) is not int
            or samples <= 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            await self._fail_closed("voice_audio_invalid", "audio metadata is invalid")
            return
        expected_bytes = samples * channels * 2
        max_bytes = sample_rate * 2 * self.config.voice.max_buffered_audio_seconds
        if expected_bytes > max_bytes:
            await self._fail_closed("voice_audio_too_large", "audio segment exceeds the bound")
            return
        expected_path = (
            f"/v1/nodes/{self.config.node_id}/voice/sessions/{self.session['session_id']}"
            f"/turns/{turn_id}/segments/{sequence}/audio"
        )
        if data.get("audio_path") != expected_path:
            await self._fail_closed(
                "voice_audio_path_invalid", "audio path does not match this turn"
            )
            return
        try:
            pcm, headers = await self.transport.voice_fetch_audio(
                self.config.node_id,
                self.session["session_id"],
                turn_id,
                sequence,
                data["audio_path"],
                max_bytes=max_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_closed("voice_audio_download_failed", str(exc))
            return
        if (
            len(pcm) != expected_bytes
            or hashlib.sha256(pcm).hexdigest() != digest
            or headers.get("x-audio-sha256") != digest
            or headers.get("x-audio-sample-rate") != str(sample_rate)
            or headers.get("x-audio-channels") != str(channels)
            or headers.get("x-audio-sample-count") != str(samples)
            or headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != str(data.get("media_type", "")).split(";", 1)[0].strip().lower()
        ):
            await self._fail_closed(
                "voice_audio_invalid", "downloaded audio failed metadata checks"
            )
            return
        report = {
            "command_id": uuid.uuid4().hex,
            "turn_id": turn_id,
            "output_generation": data["output_generation"],
            "segment_sequence": sequence,
            "played_samples": 0,
            "status": "started",
        }
        if not await self._report_playback(report):
            return
        self.play_state = "playing"
        await self._emit_state()
        status = "completed"
        played_samples = samples
        try:
            await self.player.play(
                pcm,
                sample_rate=sample_rate,
                device_id=self.config.voice.output_device or None,
            )
        except (AudioError, OSError) as exc:
            status = "interrupted" if turn_id in self.interrupted_turns else "failed"
            played_samples = 0
            if status == "failed":
                self.last_error = {"code": "playback_failed", "message": str(exc)}
        finally:
            self.play_state = "idle" if status == "completed" else "stopped"
        report.update(
            {
                "command_id": uuid.uuid4().hex,
                "played_samples": played_samples,
                "status": status,
            }
        )
        if self.active:
            await self._report_playback(report)
        await self._emit_state()

    async def _report_playback(self, payload: dict) -> bool:
        if not self.session:
            return False
        try:
            receipt = await self.transport.voice_playback(
                self.config.node_id, self.session["session_id"], payload
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_closed("playback_report_failed", str(exc))
            return False
        if receipt and not receipt.get("valid", True):
            await self.player.stop()
            self.play_state = "stopped"
            return False
        return True

    async def _handle_turn_end(self, turn_id: str) -> None:
        if self.turns is not None:
            await self._apply_actions(self.turns.reply_finished(turn_id))
        if self.current_turn_id == turn_id:
            self.current_turn_id = None
            self.current_turn_generation = None
        await self._emit_state()

    def _event_matches(self, turn_id: str, data: dict) -> bool:
        if not self.session or not isinstance(data, dict):
            return False
        return (
            data.get("session_id") == self.session.get("session_id")
            and data.get("session_generation") == self.session.get("session_generation")
            and data.get("turn_id") == turn_id
            and data.get("output_generation") == self.current_turn_generation
            and turn_id == self.current_turn_id
        )

    async def _interrupt_current(self, reason: str) -> dict | None:
        turn_id = self.current_turn_id
        generation = self.current_turn_generation
        if not self.session or turn_id is None or generation is None:
            await self.player.stop()
            self.play_state = "stopped"
            return None
        await self.player.stop()
        self.play_state = "stopped"
        self.interrupted_turns.add(turn_id)
        watcher = self.turn_watch_tasks.get(turn_id)
        if watcher is not None and not watcher.done():
            watcher.cancel()
        receipt = await self._send_interrupt(turn_id, generation, reason)
        if receipt is not None:
            self.current_turn_id = None
            self.current_turn_generation = None
        await self._emit_state()
        return receipt

    async def _send_interrupt(
        self, turn_id: str, generation: int | None, reason: str
    ) -> dict | None:
        if not self.session or generation is None:
            return None
        payload = {
            "command_id": uuid.uuid4().hex,
            "session_generation": self.session["session_generation"],
            "target_turn_id": turn_id,
            "expected_output_generation": generation,
            "reason": reason,
        }
        try:
            receipt = await self.transport.voice_interrupt(
                self.config.node_id, self.session["session_id"], payload
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_closed("voice_interrupt_failed", str(exc))
            return None
        if receipt.get("status") == "stale":
            await self._fail_closed("voice_interrupt_stale", "server output generation changed")
            return None
        self.output_generation = int(receipt["output_generation"])
        return receipt

    async def _heartbeat_loop(self) -> None:
        assert self.session is not None
        session_id = self.session["session_id"]
        interval = float(
            self.session.get("heartbeat_interval_seconds", self.config.voice.heartbeat_seconds)
        )
        try:
            while self.active and not self._stopping:
                await asyncio.sleep(max(1.0, min(interval, self.config.voice.heartbeat_seconds)))
                snapshot = await self.transport.voice_session_heartbeat(
                    self.config.node_id, session_id, uuid.uuid4().hex
                )
                if snapshot.get("status") != "active":
                    await self._fail_closed(
                        "voice_session_expired", "voice session is no longer active"
                    )
                    return
                self.session = snapshot
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._fail_closed("voice_session_disconnected", str(exc))

    async def _lease_watchdog(self) -> None:
        try:
            while self.active and not self._stopping:
                await asyncio.sleep(0.1)
                reason = self.authorization_error()
                if reason:
                    await self.stop("lease_expired", notify_server=False)
                    self.last_error = {"code": "lease_expired", "message": reason}
                    await self._emit_state()
                    return
        except asyncio.CancelledError:
            raise

    async def _lock_watchdog(self) -> None:
        try:
            while self.active and not self._stopping:
                await asyncio.sleep(1.0)
                locked = await self.lock_state()
                if locked is True:
                    await self.stop("session_locked")
                    return
        except asyncio.CancelledError:
            raise

    async def _fail_closed(self, code: str, message: str) -> None:
        self.last_error = {"code": code, "message": message}
        await self.stop(code, notify_server=False)

    async def _emit_state(self) -> None:
        await self.publish("voice.state", self.status())


async def _session_locked() -> bool | None:
    session_id = os.environ.get("XDG_SESSION_ID")
    loginctl = shutil.which("loginctl")
    if not session_id or not loginctl:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            loginctl,
            "show-session",
            session_id,
            "--property=LockedHint",
            "--value",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
    except (OSError, TimeoutError):
        return None
    if process.returncode != 0:
        return None
    value = stdout.decode("ascii", "ignore").strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def _bounded_event(data: dict) -> dict:
    allowed = {
        "sequence",
        "status",
        "stage",
        "output_generation",
        "session_generation",
        "session_id",
        "turn_id",
        "replay_gap",
    }
    return {key: value for key, value in data.items() if key in allowed}


def _dialogue_receipt(data: dict) -> dict:
    nested = data.get("receipt") or data.get("result")
    source = nested if isinstance(nested, dict) else data
    allowed = {
        "command_id",
        "status",
        "assistant_message",
        "discard_reason",
        "speech_status",
        "segment_count",
        "sequence",
        "output_generation",
        "model",
        "usage",
    }
    receipt = {key: source[key] for key in allowed if key in source}
    message = receipt.get("assistant_message")
    if isinstance(message, str):
        receipt["assistant_message"] = message[:4096]
    usage = receipt.get("usage")
    if usage is not None and not isinstance(usage, dict):
        receipt.pop("usage")
    return receipt
