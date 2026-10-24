"""Local Unix-socket JSON Lines IPC between the daemon and local clients."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IPC_VERSION = 1
MAX_FRAME_BYTES = 256 * 1024
MAX_CONNECTIONS = 8

Handler = Callable[[dict, "IpcSession"], Awaitable[Any]]


class IpcError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(eq=False)
class IpcSession:
    """Metadata and push channel for one accepted local connection."""

    server: "IpcServer"
    writer: asyncio.StreamWriter
    methods: set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def subscribe(self) -> None:
        self.server._subscribers.add(self)

    def unsubscribe(self) -> None:
        self.server._subscribers.discard(self)

    async def send_event(self, event: str, data: dict, sequence: int, generation: str) -> None:
        frame = {
            "version": IPC_VERSION,
            "event": event,
            "sequence": sequence,
            "generation": generation,
            "data": data,
        }
        await self._send(frame)

    async def _send(self, frame: dict) -> None:
        payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._lock:
            self.writer.write(payload)
            await self.writer.drain()


class IpcServer:
    def __init__(
        self,
        path: Path,
        router: dict[str, Handler],
        *,
        generation: str,
        uid: int | None = None,
    ):
        self.path = Path(path)
        self.router = dict(router)
        self.generation = generation
        self.uid = os.getuid() if uid is None else uid
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: set[IpcSession] = set()
        self._sequence = 0
        self._connections = 0

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        if self.path.exists():
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for session in list(self._subscribers):
            session.unsubscribe()
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    async def publish(self, event: str, data: dict) -> None:
        self._sequence += 1
        sequence = self._sequence
        dead: list[IpcSession] = []
        for session in list(self._subscribers):
            try:
                await session.send_event(event, data, sequence, self.generation)
            except (ConnectionError, OSError):
                dead.append(session)
        for session in dead:
            session.unsubscribe()

    @property
    def sequence(self) -> int:
        return self._sequence

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int | None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return None
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        except (OSError, AttributeError):
            return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.uid is not None:
            peer = self._peer_uid(writer)
            if peer is not None and peer != self.uid:
                writer.close()
                return
        if self._connections >= MAX_CONNECTIONS:
            writer.close()
            return
        self._connections += 1
        session = IpcSession(server=self, writer=writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await session._send(
                        _error_frame(None, "frame_too_large", "IPC frame exceeds 256 KiB", 413)
                    )
                    break
                await self._dispatch(session, line)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            session.unsubscribe()
            self._connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _dispatch(self, session: IpcSession, line: bytes) -> None:
        request_id: str | None = None
        try:
            frame = json.loads(line.decode("utf-8"))
            request_id = frame.get("request_id")
            version = frame.get("version")
            if version != IPC_VERSION:
                raise IpcError("bad_version", "Unsupported IPC version", 400)
            method = frame.get("method")
            if not isinstance(method, str):
                raise IpcError("bad_request", "method is required", 400)
            params = frame.get("params") or {}
            if not isinstance(params, dict):
                raise IpcError("bad_request", "params must be an object", 400)
            handler = self.router.get(method)
            if handler is None:
                raise IpcError("unknown_method", f"Unknown method: {method}", 404)
            result = await handler(params, session)
            frame_out = {"version": IPC_VERSION, "request_id": request_id, "result": result}
        except IpcError as exc:
            frame_out = _error_frame(request_id, exc.code, exc.message, exc.status_code)
        except Exception as exc:  # noqa: BLE001 - never leak internals to clients
            frame_out = _error_frame(request_id, "internal_error", str(exc), 500)
        await session._send(frame_out)


def _error_frame(request_id: str | None, code: str, message: str, status_code: int | None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if status_code is not None:
        error["status_code"] = status_code
    return {"version": IPC_VERSION, "request_id": request_id, "error": error}


class IpcClientError(RuntimeError):
    pass


class IpcClient:
    """Blocking-free async client used by the CLI and TUI.

    The client connects lazily and reconnects on demand. It owns no server
    token and cannot perform privileged operations beyond the router surface.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._counter = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._subscribers: list[asyncio.Queue] = []

    async def connect(self) -> None:
        async with self._lock:
            if self._writer is not None:
                return
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(str(self.path))
            except (ConnectionError, OSError) as exc:
                raise IpcClientError(f"cannot reach daemon socket {self.path}: {exc}") from exc
            self._reader_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        async with self._lock:
            task, self._reader_task = self._reader_task, None
            writer, self._writer = self._writer, None
            self._reader = None
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if writer is not None:
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()

    async def _read_loop(self) -> None:
        reader = self._reader
        assert reader is not None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                reply = json.loads(line.decode("utf-8"))
                if "event" in reply:
                    for queue in list(self._subscribers):
                        with contextlib.suppress(asyncio.QueueFull):
                            queue.put_nowait(reply)
                    continue
                future = self._pending.pop(reply.get("request_id"), None)
                if future is not None and not future.done():
                    future.set_result(reply)
        except (ConnectionError, OSError, ValueError, asyncio.CancelledError):
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(IpcClientError("daemon closed the connection"))
            self._pending.clear()

    async def call(self, method: str, params: dict | None = None) -> Any:
        await self.connect()
        assert self._writer is not None
        self._counter += 1
        request_id = str(self._counter)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = {
            "version": IPC_VERSION,
            "request_id": request_id,
            "method": method,
            "params": params or {},
        }
        self._writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await self._writer.drain()
        reply = await future
        if "error" in reply:
            error = reply["error"]
            raise IpcClientError(f"{error['code']}: {error['message']}")
        return reply.get("result")

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.append(queue)
        await self.call("status.subscribe", {})
        return queue
