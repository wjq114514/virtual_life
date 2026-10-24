"""Bounded Server-Sent Events for one voice turn."""

import asyncio
import json

from fastapi.responses import StreamingResponse

_TERMINAL_STATUS = frozenset({"completed", "discarded", "interrupted", "failed"})
_IDENTITY_FIELDS = (
    "session_id",
    "session_generation",
    "turn_id",
    "output_generation",
)


def _frame(kind: str, data: dict) -> bytes:
    return (
        f"event: {kind}\ndata: "
        + json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")


def _event_sequence(item: dict) -> int | None:
    payload = item.get("payload")
    value = item.get("sequence")
    if value is None and isinstance(payload, dict):
        value = payload.get("sequence")
    return value if type(value) is int and value >= 0 else None


def _event_data(item: dict, state: dict, sequence: int) -> dict:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        payload = item.get("data")
    if not isinstance(payload, dict):
        payload = {
            key: value for key, value in item.items() if key not in {"kind", "event", "created_at"}
        }
    data = dict(payload)
    nested_identity = data.pop("identity", None)
    if isinstance(nested_identity, dict):
        data.update(nested_identity)
    for key in _IDENTITY_FIELDS:
        data.setdefault(key, state.get(key))
    data["sequence"] = sequence
    return data


def _stream_state(voice_service, node_id: str, session_id: str, turn_id: str, after_sequence: int):
    stream_snapshot = getattr(voice_service, "stream_snapshot", None)
    if callable(stream_snapshot):
        return stream_snapshot(node_id, session_id, turn_id, after_sequence)

    turn = voice_service.get_turn(node_id, session_id, turn_id)
    session = voice_service.get_session(node_id, session_id)
    return {
        "turn": turn,
        "sequence": turn["sequence"],
        "replay_gap": False,
        "session_id": session_id,
        "session_generation": session.get("session_generation"),
        "turn_id": turn_id,
        "output_generation": turn["output_generation"],
    }


def _snapshot_data(state: dict, replay_gap: bool, cursor: int) -> dict:
    identity = {key: state.get(key) for key in _IDENTITY_FIELDS}
    identity["sequence"] = cursor
    return {
        **identity,
        "turn": state["turn"],
        "replay_gap": replay_gap,
        "cursor": cursor,
    }


def _initial_state(voice_service, node_id: str, session_id: str, turn_id: str, after_sequence: int):
    state = _stream_state(voice_service, node_id, session_id, turn_id, after_sequence)

    # This read happens before StreamingResponse starts, so an unknown node,
    # session, or turn still returns its normal HTTP error status.
    events = voice_service.list_events(turn_id, after_sequence)
    latest = state.get("sequence", state["turn"].get("sequence", 0))
    first = _event_sequence(events[0]) if events else None
    replay_gap = bool(state.get("replay_gap", False))
    if after_sequence > latest or (first is not None and first > after_sequence + 1):
        replay_gap = True
    elif not events and after_sequence < latest:
        replay_gap = True

    state = {**state, "replay_gap": replay_gap, "sequence": latest}
    return state, [] if replay_gap else events


def voice_turn_response(
    voice_service,
    node_id: str,
    session_id: str,
    turn_id: str,
    after_sequence: int,
) -> StreamingResponse:
    state, initial_events = _initial_state(
        voice_service, node_id, session_id, turn_id, after_sequence
    )
    latest_cursor = state["sequence"]
    cursor = latest_cursor if state["replay_gap"] else min(max(0, after_sequence), latest_cursor)
    identity = {key: state.get(key) for key in _IDENTITY_FIELDS}
    identity["sequence"] = latest_cursor

    async def events():
        loop = asyncio.get_running_loop()
        current_cursor = cursor
        current_identity = dict(identity)
        initial = True
        yield _frame("snapshot", _snapshot_data(state, state["replay_gap"], latest_cursor))
        last_frame = loop.time()

        while True:
            if initial:
                items = initial_events
                initial = False
            else:
                items = await asyncio.to_thread(voice_service.list_events, turn_id, current_cursor)
                first = _event_sequence(items[0]) if items else None
                if first is not None and first > current_cursor + 1:
                    refreshed = await asyncio.to_thread(
                        _stream_state,
                        voice_service,
                        node_id,
                        session_id,
                        turn_id,
                        current_cursor,
                    )
                    latest = refreshed.get(
                        "sequence", refreshed["turn"].get("sequence", current_cursor)
                    )
                    refreshed = {**refreshed, "sequence": latest, "replay_gap": True}
                    yield _frame("snapshot", _snapshot_data(refreshed, True, latest))
                    last_frame = loop.time()
                    current_identity = {key: refreshed.get(key) for key in _IDENTITY_FIELDS}
                    current_cursor = latest
                    items = []
            emitted = False
            for item in items:
                sequence = _event_sequence(item)
                kind = item.get("kind", item.get("event"))
                if sequence is None or not isinstance(kind, str) or sequence <= current_cursor:
                    continue
                current_cursor = sequence
                emitted = True
                yield _frame(kind, _event_data(item, state, sequence))
                if kind == "voice_end":
                    return

            current = await asyncio.to_thread(voice_service.get_turn, node_id, session_id, turn_id)
            if current["status"] in _TERMINAL_STATUS and current["sequence"] <= current_cursor:
                yield _frame(
                    "voice_end",
                    {
                        **current_identity,
                        "sequence": current["sequence"],
                        "status": current["status"],
                    },
                )
                return
            if current["status"] in _TERMINAL_STATUS and not emitted:
                # The event ring moved past this subscriber while it was slow.
                latest = await asyncio.to_thread(
                    _stream_state,
                    voice_service,
                    node_id,
                    session_id,
                    turn_id,
                    current_cursor,
                )
                latest_sequence = latest.get(
                    "sequence", latest["turn"].get("sequence", current_cursor)
                )
                yield _frame(
                    "snapshot",
                    _snapshot_data({**latest, **current_identity}, True, latest_sequence),
                )
                yield _frame(
                    "voice_end",
                    {
                        **current_identity,
                        "sequence": latest_sequence,
                        "status": current["status"],
                    },
                )
                return
            if loop.time() - last_frame >= 10:
                yield b": ping\n\n"
                last_frame = loop.time()
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
