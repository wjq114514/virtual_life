"""Bounded sherpa-onnx Kokoro speech provider and worker process control."""

from __future__ import annotations

import array
import base64
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ports import SpeechProvider, SpeechRequest, SpeechResult

PCM_S16LE = "audio/pcm;format=s16le"
DEFAULT_SAMPLE_RATE = 24000
MAX_TEXT_CHARS = 1024
MAX_TEXT_BYTES = 4096
MAX_AUDIO_SECONDS = 60.0
MAX_WORKER_THREADS = 8
MAX_WORKERS = 8
MAX_WORKER_REQUEST_BYTES = 16 * 1024
MAX_WORKER_STDERR_BYTES = 64 * 1024
WORKER_POLL_SECONDS = 0.05

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKER_SCRIPT = Path(__file__).with_name("speech_worker.py")
_RUNTIME_CHECK = (
    "import importlib.metadata, sherpa_onnx; print(importlib.metadata.version('sherpa-onnx'))"
)


class SpeechProviderError(Exception):
    def __init__(self, code: str, message: str, *, usage_known: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.usage_known = usage_known


def _pcm_seconds(data: bytes, sample_rate: int) -> float:
    if sample_rate <= 0:
        return 0.0
    return len(data) / (2 * sample_rate)


def resample_pcm_s16le(
    data: bytes,
    source_rate: int,
    target_rate: int,
    *,
    channels: int = 1,
) -> bytes:
    """Linearly resample a complete mono S16LE segment."""
    if not isinstance(data, bytes):
        raise TypeError("PCM data must be bytes")
    if type(channels) is not int or channels != 1:
        raise ValueError("TTS PCM must be mono")
    if type(source_rate) is not int or source_rate <= 0:
        raise ValueError("source_rate must be a positive integer")
    if type(target_rate) is not int or target_rate <= 0:
        raise ValueError("target_rate must be a positive integer")
    if len(data) % 2:
        raise ValueError("S16LE PCM must contain complete 16-bit samples")
    if not data or source_rate == target_rate:
        return data

    samples = array.array("h")
    samples.frombytes(data)
    if samples.itemsize != 2:
        raise RuntimeError("host does not provide 16-bit signed samples")
    if sys.byteorder != "little":
        samples.byteswap()

    source_count = len(samples)
    target_count = max(1, round(source_count * target_rate / source_rate))
    output = array.array("h")
    for index in range(target_count):
        position = index * source_rate / target_rate
        low = min(int(position), source_count - 1)
        high = min(low + 1, source_count - 1)
        fraction = position - low
        value = samples[low] * (1.0 - fraction) + samples[high] * fraction
        output.append(max(-32768, min(32767, round(value))))
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


@dataclass(slots=True)
class SpeechProviderStatus:
    provider: str
    available: bool
    reason: str | None
    sample_rate: int
    voices: tuple[str, ...] = ()


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path.resolve()
    return (_PROJECT_ROOT / path).resolve()


def _resolve_worker_python(value: str | None) -> str:
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute() or "/" in value or "\\" in value:
            return str(_repo_path(candidate))
        return shutil.which(value) or value
    isolated = _PROJECT_ROOT / ".data" / "speech" / "venv" / "bin" / "python"
    return str(isolated) if isolated.is_file() else sys.executable


def _worker_environment(num_threads: int) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in (
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    for name in (
        "OMP_NUM_THREADS",
        "OMP_THREAD_LIMIT",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        environment[name] = str(num_threads)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class SherpaOnnxKokoroProvider:
    """Offline Kokoro provider; every synthesis runs in a killable child process."""

    name = "sherpa_onnx_kokoro"
    waveform_streaming = False

    def __init__(
        self,
        *,
        model_dir: str | Path,
        manifest_path: str | Path | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        num_threads: int = 2,
        default_voice_id: str = "default",
        worker_python: str | None = None,
        max_workers: int = 1,
        worker_wait_seconds: float = 5.0,
    ) -> None:
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("sample_rate must be between 8000 and 192000")
        if type(num_threads) is not int or not 1 <= num_threads <= 32:
            raise ValueError("num_threads must be between 1 and 32")
        if type(max_workers) is not int or not 1 <= max_workers <= MAX_WORKERS:
            raise ValueError(f"max_workers must be between 1 and {MAX_WORKERS}")
        if (
            isinstance(worker_wait_seconds, bool)
            or not isinstance(worker_wait_seconds, (int, float))
            or not math.isfinite(worker_wait_seconds)
            or not 0 < worker_wait_seconds <= 30
        ):
            raise ValueError("worker_wait_seconds must be greater than 0 and at most 30")
        if not isinstance(default_voice_id, str) or not default_voice_id:
            raise ValueError("default_voice_id must be a non-empty string")

        default_manifest = _PROJECT_ROOT / "tools" / "speech" / "kokoro-v1_1.manifest.json"
        self.model_dir = _repo_path(model_dir)
        self.manifest_path = _repo_path(manifest_path or default_manifest)
        self.sample_rate = sample_rate
        self.num_threads = min(num_threads, MAX_WORKER_THREADS)
        self.default_voice_id = default_voice_id
        self.worker_python = _resolve_worker_python(worker_python)
        self.max_workers = max_workers
        self.worker_wait_seconds = float(worker_wait_seconds)

        self._manifest: dict = {}
        self._manifest_error: str | None = None
        self._reason: str | None = None
        self._runtime_checked_at = 0.0
        self._runtime_ok = False
        self._runtime_reason: str | None = None
        self._model_signature: tuple[tuple[str, int, int], ...] | None = None
        self._probe_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active: dict[tuple[str, str], set[subprocess.Popen]] = {}
        self._cancelled_pids: set[int] = set()
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self._load_manifest()

    def _load_manifest(self) -> None:
        try:
            value = json.loads(self.manifest_path.read_text("utf-8"))
        except (OSError, ValueError):
            self._manifest = {}
            self._manifest_error = "model_manifest_missing_or_invalid"
            return
        if not isinstance(value, dict):
            self._manifest = {}
            self._manifest_error = "model_manifest_invalid"
            return
        self._manifest = value
        self._manifest_error = None

    def _manifest_path_for(self, value: str) -> Path | None:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        resolved = (self.model_dir / relative).resolve()
        if self.model_dir.resolve() not in resolved.parents:
            return None
        return resolved

    def _verify_model_files(self) -> str | None:
        files = self._manifest.get("files")
        if not isinstance(files, list) or not files:
            return "model_manifest_files_missing"
        entries: list[tuple[Path, int, str]] = []
        signature: list[tuple[str, int, int]] = []
        for entry in files:
            if not isinstance(entry, dict):
                return "model_manifest_invalid"
            relative = entry.get("path")
            digest = entry.get("sha256")
            size = entry.get("size")
            if (
                not isinstance(relative, str)
                or not isinstance(digest, str)
                or len(digest) != 64
                or type(size) is not int
                or size < 0
            ):
                return "model_manifest_invalid"
            path = self._manifest_path_for(relative)
            if path is None:
                return "model_manifest_invalid"
            try:
                stat = path.stat()
            except OSError:
                return "model_files_missing"
            if not path.is_file() or stat.st_size != size:
                return "model_files_missing"
            entries.append((path, size, digest.lower()))
            signature.append((str(path), stat.st_size, stat.st_mtime_ns))
        signature_tuple = tuple(signature)
        if self._model_signature == signature_tuple:
            return None
        for path, _size, expected in entries:
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError:
                return "model_files_missing"
            if digest.hexdigest() != expected:
                self._model_signature = None
                return "model_checksum_mismatch"
        self._model_signature = signature_tuple

        required = self._manifest.get("required_files", [])
        if not isinstance(required, list):
            return "model_manifest_invalid"
        for relative in required:
            if not isinstance(relative, str):
                return "model_manifest_invalid"
            path = self._manifest_path_for(relative)
            if path is None or not path.is_file():
                return "model_files_missing"
        data_dir = self._manifest.get("espeak_data")
        if not isinstance(data_dir, str):
            return "model_manifest_invalid"
        data_path = self._manifest_path_for(data_dir)
        if data_path is None or not data_path.is_dir():
            return "model_files_missing"
        return None

    def _probe_runtime(self) -> tuple[bool, str | None]:
        now = time.monotonic()
        if self._runtime_checked_at and now - self._runtime_checked_at < 10.0:
            return self._runtime_ok, self._runtime_reason
        expected = self._manifest.get("runtime_version")
        if not isinstance(expected, str) or not expected:
            self._runtime_checked_at = now
            self._runtime_ok = False
            self._runtime_reason = "runtime_version_missing"
            return self._runtime_ok, self._runtime_reason
        try:
            completed = subprocess.run(
                [self.worker_python, "-c", _RUNTIME_CHECK],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
                env=_worker_environment(self.num_threads),
            )
        except (OSError, subprocess.TimeoutExpired):
            reason = "worker_python_unavailable"
            ok = False
        else:
            actual = completed.stdout.strip() if completed.returncode == 0 else ""
            ok = actual == expected
            reason = (
                None if ok else ("runtime_version_mismatch" if actual else "runtime_unavailable")
            )
        self._runtime_checked_at = now
        self._runtime_ok = ok
        self._runtime_reason = reason
        return ok, reason

    @property
    def available(self) -> bool:
        return self._probe()

    def _probe(self) -> bool:
        with self._probe_lock:
            self._reason = self._manifest_error
            if self._reason:
                return False
            if not self.model_dir.is_dir():
                self._reason = "model_directory_missing"
                return False
            self._reason = self._verify_model_files()
            if self._reason:
                return False
            runtime_ok, runtime_reason = self._probe_runtime()
            if not runtime_ok:
                self._reason = runtime_reason
                return False
            self._reason = None
            return True

    def status(self) -> SpeechProviderStatus:
        available = self.available
        voice_ids = self._manifest.get("voice_ids", {})
        voices = tuple(sorted(voice_ids)) if isinstance(voice_ids, dict) else ()
        return SpeechProviderStatus(
            provider=self.name,
            available=available,
            reason=None if available else self._reason,
            sample_rate=self.sample_rate,
            voices=voices,
        )

    def _validate(self, request: SpeechRequest) -> None:
        if not isinstance(request, SpeechRequest):
            raise SpeechProviderError("voice_input_invalid", "request must be a SpeechRequest")
        text = request.text
        if not isinstance(text, str) or not text.strip():
            raise SpeechProviderError("voice_input_invalid", "text must be non-empty")
        if len(text) > MAX_TEXT_CHARS:
            raise SpeechProviderError("voice_input_invalid", "text exceeds the character limit")
        try:
            text_bytes = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise SpeechProviderError("voice_input_invalid", "text is not valid UTF-8") from exc
        if len(text_bytes) > MAX_TEXT_BYTES:
            raise SpeechProviderError("voice_input_invalid", "text exceeds the UTF-8 byte limit")
        seconds = request.max_audio_seconds
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0 < seconds <= MAX_AUDIO_SECONDS
        ):
            raise SpeechProviderError("voice_input_invalid", "max_audio_seconds is outside limits")
        if (
            not isinstance(request.voice, str)
            or not request.voice
            or len(request.voice) > 64
            or any(ord(char) < 32 for char in request.voice)
        ):
            raise SpeechProviderError("voice_input_invalid", "voice is not a valid identifier")
        for name, value in (("companion_id", request.companion_id), ("node_id", request.node_id)):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise SpeechProviderError("voice_input_invalid", f"{name} is invalid")

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        return self.synthesize_cancellable(request, lambda: False)

    def synthesize_cancellable(
        self,
        request: SpeechRequest,
        should_cancel: Callable[[], bool],
    ) -> SpeechResult:
        """Synthesize in a child process and kill it when the caller is cancelled."""
        self._validate(request)
        if not callable(should_cancel):
            raise SpeechProviderError("voice_input_invalid", "should_cancel must be callable")
        if self._cancel_requested(should_cancel):
            raise SpeechProviderError("speech_cancelled", "speech synthesis was cancelled")
        if not self.available:
            raise SpeechProviderError(
                "voice_provider_unavailable",
                f"TTS provider is unavailable ({self._reason or 'unknown'})",
            )

        if not self._acquire_worker_slot(should_cancel):
            if self._cancel_requested(should_cancel):
                raise SpeechProviderError("speech_cancelled", "speech synthesis was cancelled")
            raise SpeechProviderError("speech_backpressure", "all TTS workers are busy")
        try:
            if self._cancel_requested(should_cancel):
                raise SpeechProviderError("speech_cancelled", "speech synthesis was cancelled")
            envelope = self._run_worker(request, should_cancel)
            if self._cancel_requested(should_cancel):
                raise SpeechProviderError("speech_cancelled", "speech synthesis was cancelled")
            audio, sample_rate, channels = self._decode_worker_output(envelope, request)
            return self._result(audio, sample_rate, channels, request)
        finally:
            self._worker_slots.release()

    @staticmethod
    def _cancel_requested(should_cancel: Callable[[], bool]) -> bool:
        try:
            return bool(should_cancel())
        except Exception as exc:  # noqa: BLE001 - a broken cancel callback must fail closed
            raise SpeechProviderError(
                "speech_cancelled", f"cancellation check failed: {type(exc).__name__}"
            ) from exc

    def _acquire_worker_slot(self, should_cancel: Callable[[], bool]) -> bool:
        deadline = time.monotonic() + self.worker_wait_seconds
        while True:
            if self._cancel_requested(should_cancel):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._worker_slots.acquire(timeout=min(WORKER_POLL_SECONDS, remaining)):
                return True

    def _worker_payload(self, request: SpeechRequest) -> bytes:
        payload = json.dumps(
            {
                "operation": "synthesize",
                "model_dir": str(self.model_dir),
                "manifest": str(self.manifest_path),
                "text": request.text,
                "voice": request.voice,
                "num_threads": self.num_threads,
                "max_audio_seconds": float(request.max_audio_seconds),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_WORKER_REQUEST_BYTES:
            raise SpeechProviderError("voice_input_invalid", "worker request exceeds byte limit")
        return payload

    def _run_worker(
        self,
        request: SpeechRequest,
        should_cancel: Callable[[], bool],
    ) -> bytes:
        payload = self._worker_payload(request)
        source_rate = self._manifest.get("sample_rate")
        if type(source_rate) is not int or not 8000 <= source_rate <= 192000:
            raise SpeechProviderError(
                "voice_provider_unavailable", "manifest sample_rate is invalid"
            )
        max_pcm_bytes = math.floor(request.max_audio_seconds * source_rate) * 2
        max_stdout_bytes = math.ceil(max_pcm_bytes * 4 / 3) + 4096
        timeout_seconds = min(120.0, max(15.0, 10.0 + request.max_audio_seconds * 3.0))
        command = [self.worker_python, str(_WORKER_SCRIPT)]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.model_dir,
                env=_worker_environment(self.num_threads),
                close_fds=True,
                start_new_session=(os.name == "posix"),
                bufsize=0,
            )
        except OSError as exc:
            raise SpeechProviderError(
                "voice_provider_unavailable", "TTS worker could not be started"
            ) from exc

        key = (request.companion_id, request.node_id)
        with self._active_lock:
            self._active.setdefault(key, set()).add(process)

        stdout = bytearray()
        stderr = bytearray()
        output_overflow = threading.Event()
        thread_errors: list[BaseException] = []

        def drain(pipe, buffer: bytearray, limit: int) -> None:
            try:
                while True:
                    block = pipe.read(64 * 1024)
                    if not block:
                        return
                    remaining = limit - len(buffer)
                    if remaining > 0:
                        buffer.extend(block[:remaining])
                    if len(block) > remaining:
                        output_overflow.set()
            except BaseException as exc:  # noqa: BLE001 - report reader failure to parent
                thread_errors.append(exc)

        def write_request() -> None:
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        stdout_reader = threading.Thread(
            target=drain,
            args=(process.stdout, stdout, max_stdout_bytes),
            name="speech-worker-stdout",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=drain,
            args=(process.stderr, stderr, MAX_WORKER_STDERR_BYTES),
            name="speech-worker-stderr",
            daemon=True,
        )
        input_writer = threading.Thread(
            target=write_request,
            name="speech-worker-stdin",
            daemon=True,
        )
        deadline = time.monotonic() + timeout_seconds
        cancelled = False
        timed_out = False
        overflowed = False
        try:
            stdout_reader.start()
            stderr_reader.start()
            input_writer.start()
            while process.poll() is None:
                if self._cancel_requested(should_cancel):
                    cancelled = True
                    self._terminate_process(process)
                    break
                if output_overflow.is_set():
                    overflowed = True
                    self._terminate_process(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate_process(process)
                    break
                try:
                    process.wait(timeout=WORKER_POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._terminate_process(process, force=True)
            for thread in (input_writer, stdout_reader, stderr_reader):
                thread.join(timeout=1.0)
            if any(thread.is_alive() for thread in (input_writer, stdout_reader, stderr_reader)):
                self._terminate_process(process, force=True)
                raise SpeechProviderError(
                    "voice_provider_unavailable", "TTS worker pipes did not close"
                )
            if thread_errors:
                raise SpeechProviderError(
                    "voice_provider_unavailable", "TTS worker pipe read failed"
                ) from thread_errors[0]
            with self._active_lock:
                cancelled = cancelled or process.pid in self._cancelled_pids
                self._cancelled_pids.discard(process.pid)
            if cancelled:
                raise SpeechProviderError("speech_cancelled", "speech synthesis was cancelled")
            if overflowed or output_overflow.is_set():
                raise SpeechProviderError(
                    "speech_output_limit", "TTS worker output exceeded its limit"
                )
            if timed_out:
                raise SpeechProviderError("speech_timeout", "TTS worker exceeded its time limit")
            if process.returncode != 0:
                raise SpeechProviderError(
                    "voice_provider_unavailable", "TTS worker returned an error"
                )
            return bytes(stdout)
        finally:
            if process.poll() is None:
                self._terminate_process(process, force=True)
            with self._active_lock:
                active = self._active.get(key)
                if active is not None:
                    active.discard(process)
                    if not active:
                        self._active.pop(key, None)
                self._cancelled_pids.discard(process.pid)
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass

    @staticmethod
    def _terminate_process(process: subprocess.Popen, *, force: bool = False) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        except OSError:
            if force:
                process.kill()
            else:
                process.terminate()
        try:
            process.wait(timeout=0.25 if not force else 1.0)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    def cancel(self, companion_id: str, node_id: str) -> int:
        """Terminate active workers for a turn's owner, if the caller has a key."""
        key = (companion_id, node_id)
        with self._active_lock:
            processes = tuple(self._active.get(key, ()))
            self._cancelled_pids.update(process.pid for process in processes)
        for process in processes:
            self._terminate_process(process)
        return len(processes)

    def _decode_worker_output(
        self,
        output: bytes,
        request: SpeechRequest,
    ) -> tuple[bytes, int, int]:
        try:
            envelope = json.loads(output)
            if not isinstance(envelope, dict) or envelope.get("ok") is not True:
                raise ValueError("invalid worker response")
            sample_rate = envelope["sample_rate"]
            channels = envelope["channels"]
            sample_count = envelope["sample_count"]
            pcm = base64.b64decode(envelope["pcm_b64"], validate=True)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SpeechProviderError(
                "voice_provider_unavailable", "TTS worker returned an invalid response"
            ) from exc
        if type(sample_rate) is not int or sample_rate != self._manifest.get("sample_rate"):
            raise SpeechProviderError("speech_output_invalid", "worker sample_rate is invalid")
        if type(channels) is not int or channels != 1:
            raise SpeechProviderError("speech_output_invalid", "TTS output must be mono")
        if type(sample_count) is not int or sample_count <= 0 or len(pcm) != sample_count * 2:
            raise SpeechProviderError("speech_output_invalid", "worker sample count is invalid")
        max_samples = math.floor(request.max_audio_seconds * sample_rate)
        if sample_count > max_samples or len(pcm) > max_samples * 2:
            raise SpeechProviderError("speech_output_limit", "TTS audio exceeded max_audio_seconds")
        return pcm, sample_rate, channels

    def _result(
        self,
        audio: bytes,
        sample_rate: int,
        channels: int,
        request: SpeechRequest,
    ) -> SpeechResult:
        if channels != 1:
            raise SpeechProviderError("speech_output_invalid", "TTS output must be mono")
        if sample_rate != self.sample_rate:
            try:
                audio = resample_pcm_s16le(audio, sample_rate, self.sample_rate, channels=channels)
            except (TypeError, ValueError, RuntimeError) as exc:
                raise SpeechProviderError("speech_output_invalid", "PCM resampling failed") from exc
        if len(audio) % 2:
            raise SpeechProviderError("speech_output_invalid", "PCM ended with a partial sample")
        sample_count = len(audio) // 2
        max_samples = math.floor(request.max_audio_seconds * self.sample_rate)
        if sample_count <= 0 or sample_count > max_samples:
            raise SpeechProviderError("speech_output_limit", "TTS audio exceeded max_audio_seconds")
        digest = hashlib.sha256(audio).hexdigest()
        return SpeechResult(
            audio=audio,
            media_type=PCM_S16LE,
            metadata={
                "voice": request.voice,
                "sample_rate": self.sample_rate,
                "channels": 1,
                "sample_count": sample_count,
                "seconds": _pcm_seconds(audio, self.sample_rate),
                "sha256": digest,
                "waveform_streaming": False,
            },
        )


class SpeechProviderRegistry:
    """Holds the configured provider and exposes a stable status snapshot."""

    def __init__(self, provider: SpeechProvider | None) -> None:
        self._provider = provider

    @property
    def provider(self) -> SpeechProvider | None:
        return self._provider

    @property
    def configured(self) -> bool:
        return self._provider is not None

    @property
    def available(self) -> bool:
        provider = self._provider
        if provider is None:
            return False
        status = getattr(provider, "status", None)
        if callable(status):
            return bool(status().available)
        return True

    def status(self) -> dict:
        provider = self._provider
        if provider is None:
            return {"provider": None, "available": False, "reason": "not_configured"}
        status = getattr(provider, "status", None)
        if callable(status):
            snapshot = status()
            return {
                "provider": snapshot.provider,
                "available": snapshot.available,
                "reason": snapshot.reason,
                "sample_rate": snapshot.sample_rate,
            }
        return {"provider": getattr(provider, "name", "custom"), "available": True, "reason": None}


def build_speech_provider(config) -> SpeechProviderRegistry:
    """Construct the configured local provider without importing its runtime."""
    if config is None or not getattr(config, "enabled", False):
        return SpeechProviderRegistry(None)
    if config.provider == "sherpa_onnx_kokoro":
        provider = SherpaOnnxKokoroProvider(
            model_dir=config.model_dir,
            manifest_path=config.model_manifest or None,
            sample_rate=config.sample_rate,
            num_threads=config.worker_threads,
            default_voice_id=config.default_voice_id,
            worker_python=config.worker_python or None,
            max_workers=getattr(config, "global_concurrency", 1),
        )
        return SpeechProviderRegistry(provider)
    return SpeechProviderRegistry(None)
