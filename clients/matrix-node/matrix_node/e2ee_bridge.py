"""Bridge to the Node E2EE sidecar over stdio.

The sidecar owns the Matrix session (login + sync + RustCrypto). This bridge
spawns it, sends newline-delimited JSON commands, and reads back events.

Encrypted rooms: when the sidecar decrypts an m.room.encrypted event it emits a
plaintext `message` event identical in shape to the direct client path, so the
runtime treats both the same way.

Disabled path: if `e2ee` is not configured, this class is a no-op and the
runtime uses the plain stdlib MatrixClient (plaintext rooms only).
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class SidecarError(RuntimeError):
    pass


class E2eeBridge:
    def __init__(
        self,
        sidecar_dir: Path,
        sidecar_command: list[str] | None = None,
        config: dict[str, Any] | None = None,
    ):
        self._sidecar_dir = Path(sidecar_dir)
        self._config = config or {}
        self._command = sidecar_command or self._default_command()
        self._proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._handlers: list[Any] = []

    def _default_command(self) -> list[str]:
        sidecar = self._sidecar_dir / "sidecar.js"
        config_path = self._sidecar_dir / "sidecar.config.json"
        config_path.write_text(json.dumps(self._config), encoding="utf-8")
        return ["node", str(sidecar), str(config_path)]

    def start(self, timeout_seconds: float = 20.0) -> None:
        if self._proc is not None:
            return
        env = dict(os.environ)
        env.setdefault("NODE_PATH", str(self._sidecar_dir / "node_modules"))
        LOGGER.info("starting e2ee sidecar: %s", " ".join(self._command))
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        reader = threading.Thread(target=self._read_loop, daemon=True)
        reader.start()
        stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        stderr_reader.start()
        if not self._ready.wait(timeout=timeout_seconds):
            raise SidecarError("sidecar did not become ready in time")
        LOGGER.info("e2ee sidecar ready")

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            LOGGER.warning("sidecar stderr: %s", line.rstrip())

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                LOGGER.warning("sidecar bad line: %s", line[:200])
                continue
            if event.get("event") == "ready":
                self._ready.set()
                continue
            if event.get("event") == "log":
                LOGGER.info("sidecar: %s", event.get("message"))
                continue
            if event.get("event") == "error":
                LOGGER.warning("sidecar error: %s", event.get("message"))
                continue
            if event.get("event") == "message":
                for handler in list(self._handlers):
                    handler(event)
                continue
            LOGGER.debug("sidecar event: %s", event)

    def send_command(self, command: dict[str, Any], timeout_seconds: float = 30.0) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise SidecarError("sidecar not started")
        with self._lock:
            proc.stdin.write(json.dumps(command) + "\n")
            proc.stdin.flush()
        # Sidecar replies are async; for command acks we do not block here.
        return {}

    def on_message(self, handler: Any) -> None:
        self._handlers.append(handler)

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None
        LOGGER.info("e2ee sidecar stopped")
