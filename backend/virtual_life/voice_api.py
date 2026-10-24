"""HTTP transport for the server-side voice contract."""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from .coordinator import DomainError
from .voice_models import (
    MAX_VERSION,
    VoiceHeartbeat,
    VoiceInterruptReceipt,
    VoiceInterruptRequest,
    VoicePlaybackReceipt,
    VoicePlaybackReport,
    VoiceSessionCreate,
    VoiceSessionSnapshot,
    VoiceStopRequest,
    VoiceTurnReceipt,
    VoiceTurnSnapshot,
    VoiceTurnSubmit,
)
from .voice_stream import voice_turn_response

MAX_VOICE_JSON_BYTES = 24 * 1024
AUDIO_CHUNK_BYTES = 8192


class VoiceBodyLimitRoute(APIRoute):
    """Count voice POST bytes before FastAPI parses their JSON bodies."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        if "POST" not in self.methods:
            return handler

        async def limited_handler(request: Request):
            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_VOICE_JSON_BYTES:
                    raise DomainError(
                        413,
                        "request_too_large",
                        "Voice JSON request exceeds the 24 KiB limit",
                    )
                chunks.append(chunk)
            request._body = b"".join(chunks)
            return await handler(request)

        return limited_handler


def build_voice_router(voice_service) -> APIRouter:
    router = APIRouter(
        prefix="/nodes/{node_id}/voice",
        tags=["voice"],
        route_class=VoiceBodyLimitRoute,
    )

    def _enabled() -> None:
        if not voice_service.config.enabled:
            raise DomainError(503, "voice_disabled", "Voice is not enabled on this runtime")

    @router.post("/sessions", status_code=201, response_model=VoiceSessionSnapshot)
    def create_session(node_id: str, body: VoiceSessionCreate):
        _enabled()
        return voice_service.create_session(node_id, body.model_dump(mode="json"))

    @router.get("/sessions/{session_id}", response_model=VoiceSessionSnapshot)
    def get_session(node_id: str, session_id: str):
        _enabled()
        return voice_service.get_session(node_id, session_id)

    @router.post(
        "/sessions/{session_id}/heartbeat",
        response_model=VoiceSessionSnapshot,
    )
    def heartbeat_session(node_id: str, session_id: str, body: VoiceHeartbeat):
        _enabled()
        return voice_service.heartbeat(node_id, session_id, body.model_dump(mode="json"))

    @router.post(
        "/sessions/{session_id}/stop",
        response_model=VoiceSessionSnapshot,
    )
    def stop_session(node_id: str, session_id: str, body: VoiceStopRequest):
        _enabled()
        return voice_service.stop_session(node_id, session_id, body.model_dump(mode="json"))

    @router.post(
        "/sessions/{session_id}/turns",
        status_code=202,
        response_model=VoiceTurnReceipt,
    )
    def submit_turn(node_id: str, session_id: str, body: VoiceTurnSubmit):
        _enabled()
        return voice_service.submit_turn(node_id, session_id, body.model_dump(mode="json"))

    @router.get(
        "/sessions/{session_id}/turns/{turn_id}",
        response_model=VoiceTurnSnapshot,
    )
    def get_turn(node_id: str, session_id: str, turn_id: str):
        _enabled()
        return voice_service.get_turn(node_id, session_id, turn_id)

    @router.get(
        "/sessions/{session_id}/turns/{turn_id}/events",
        response_class=StreamingResponse,
        response_model=None,
        responses={
            200: {
                "description": "UTF-8 server-sent voice events",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
    )
    def turn_events(
        node_id: str,
        session_id: str,
        turn_id: str,
        after_sequence: Annotated[int, Query(ge=0, le=MAX_VERSION)] = 0,
    ) -> StreamingResponse:
        _enabled()
        return voice_turn_response(voice_service, node_id, session_id, turn_id, after_sequence)

    @router.get(
        "/sessions/{session_id}/turns/{turn_id}/segments/{sequence}/audio",
        response_class=StreamingResponse,
        response_model=None,
        responses={
            200: {
                "description": "Bounded PCM audio stream",
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    def segment_audio(
        node_id: str,
        session_id: str,
        turn_id: str,
        sequence: Annotated[int, Path(ge=0, le=MAX_VERSION)],
    ) -> StreamingResponse:
        _enabled()
        metadata = voice_service.audio_metadata(node_id, session_id, turn_id, sequence)
        headers = {
            "Cache-Control": "no-store",
            "Content-Length": str(metadata["byte_length"]),
            "X-Audio-SHA256": metadata["sha256"],
            "X-Audio-Sample-Rate": str(metadata["sample_rate"]),
            "X-Audio-Channels": str(metadata["channels"]),
            "X-Audio-Sample-Count": str(metadata["sample_count"]),
        }
        chunks = voice_service.iter_audio(
            node_id,
            session_id,
            turn_id,
            sequence,
            chunk_bytes=AUDIO_CHUNK_BYTES,
        )
        return StreamingResponse(
            chunks,
            media_type=metadata["media_type"],
            headers=headers,
        )

    @router.post(
        "/sessions/{session_id}/interrupt",
        response_model=VoiceInterruptReceipt,
    )
    def interrupt(node_id: str, session_id: str, body: VoiceInterruptRequest):
        _enabled()
        return voice_service.interrupt(node_id, session_id, body.model_dump(mode="json"))

    @router.post(
        "/sessions/{session_id}/playback",
        response_model=VoicePlaybackReceipt,
    )
    def playback(node_id: str, session_id: str, body: VoicePlaybackReport):
        _enabled()
        return voice_service.record_playback(node_id, session_id, body.model_dump(mode="json"))

    return router
