"""Bounded SSE subscriptions; disconnecting does not cancel durable execution."""

import asyncio
import json
import threading
from concurrent.futures import CancelledError as FutureCancelled
from concurrent.futures import TimeoutError as FutureTimeout

from fastapi.responses import StreamingResponse

from .coordinator import DomainError
from .dialogue import DialogueError
from .memory import MemoryError
from .personality import PersonalityError


def stream_response(dialogue, companion_id, payload, workers):
    async def events():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=32)
        detached = threading.Event()

        def emit(kind, data):
            if detached.is_set():
                return
            frame = (
                f"event: {kind}\ndata: "
                + json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                + "\n\n"
            ).encode("utf-8")
            future = asyncio.run_coroutine_threadsafe(queue.put(frame), loop)
            try:
                # A blocked subscriber cannot retain a model worker indefinitely.
                future.result(timeout=2)
            except (FutureTimeout, FutureCancelled, RuntimeError):
                future.cancel()
                detached.set()

        def execute():
            try:
                receipt = dialogue.send_stream(companion_id, payload, emit)
                emit("result", receipt)
            except (DialogueError, DomainError, PersonalityError, MemoryError) as exc:
                emit(
                    "error",
                    {
                        "command_id": payload["command_id"],
                        "companion_id": companion_id,
                        "error": {
                            "status_code": exc.status_code,
                            "code": exc.code,
                            "message": exc.message,
                        },
                    },
                )
            except Exception:
                emit(
                    "error",
                    {
                        "command_id": payload["command_id"],
                        "companion_id": companion_id,
                        "error": {
                            "status_code": 500,
                            "code": "dialogue_execution_failed",
                            "message": "Dialogue execution failed; query the original receipt",
                        },
                    },
                )

        worker = asyncio.create_task(asyncio.to_thread(execute), name="streaming-dialogue")
        workers.add(worker)
        worker.add_done_callback(workers.discard)
        last_frame = loop.time()
        try:
            while not detached.is_set():
                # Poll only the local queue. Provider calls run in their own worker;
                # heartbeat, leases and other API requests remain responsive.
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    if worker.done():
                        break
                    if loop.time() - last_frame >= 10:
                        yield b": ping\n\n"
                        last_frame = loop.time()
                    continue
                yield frame
                last_frame = loop.time()
                if frame.startswith((b"event: result\n", b"event: error\n")):
                    break
        finally:
            detached.set()
            # Do not cancel/await this task here: it must drain provider usage and
            # commit the ordinary receipt even after the browser has gone away.

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
