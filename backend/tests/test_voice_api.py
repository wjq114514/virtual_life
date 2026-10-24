"""HTTP contracts for the voice routes, using isolated fake-provider runtimes."""

import hashlib
import json
from datetime import timedelta

from test_api import client as client
from test_voice_service import voice_runtime as voice_runtime
from virtual_life.voice_api import AUDIO_CHUNK_BYTES, MAX_VOICE_JSON_BYTES


def _session_body(app, command_id, *, mode="text", audio_enabled=True, asr=None):
    state = app.state.coordinator.get_companion("sui")
    return {
        "command_id": command_id,
        "companion_id": "sui",
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "presentation_id": "p",
        "mode": mode,
        "asr": asr,
        "output": {"audio_enabled": audio_enabled},
    }


def _replace_session(client, app, previous, command_id, *, mode="text", asr=None):
    base = f"/v1/nodes/desk/voice/sessions/{previous['session_id']}"
    stopped = client.post(base + "/stop", json={"command_id": command_id + "-stop"})
    assert stopped.status_code == 200, stopped.text
    response = client.post(
        "/v1/nodes/desk/voice/sessions",
        json=_session_body(
            app,
            command_id,
            mode=mode,
            audio_enabled=False if mode != "text" else True,
            asr=asr,
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _turn_body(app, session, command_id, *, input_value=None, end_reason=None):
    state = app.state.coordinator.get_companion("sui")
    current = app.state.voice.get_session("desk", session["session_id"])
    if input_value is None:
        input_value = {"kind": "text"}
    if end_reason is None:
        end_reason = "manual" if input_value["kind"] == "text" else "adaptive_endpoint"
    return {
        "command_id": command_id,
        "turn_id": command_id,
        "session_generation": session["session_generation"],
        "expected_output_generation": current["output_generation"],
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "text": "你好",
        "end_reason": end_reason,
        "input": input_value,
    }


def _sse_frames(text):
    frames = []
    for block in text.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        lines = block.splitlines()
        kind = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        payload = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        frames.append((kind, json.loads(payload)))
    return frames


def test_voice_routes_return_disabled_status(client):
    body = {
        "command_id": "disabled-voice",
        "companion_id": "sui",
        "epoch": 0,
        "expected_revision": 0,
        "presentation_id": "p",
        "mode": "text",
    }
    response = client.post("/v1/nodes/desk/voice/sessions", json=body)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "voice_disabled"


def test_voice_openapi_has_typed_json_sse_and_audio_responses(voice_runtime):
    client, _, _, _, _, _ = voice_runtime
    document = client.get("/openapi.json").json()
    session = document["paths"]["/v1/nodes/{node_id}/voice/sessions"]["post"]
    assert (
        session["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/VoiceSessionSnapshot"
    )
    turn = document["paths"]["/v1/nodes/{node_id}/voice/sessions/{session_id}/turns"]["post"]
    assert (
        turn["responses"]["202"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/VoiceTurnReceipt"
    )
    events = document["paths"][
        "/v1/nodes/{node_id}/voice/sessions/{session_id}/turns/{turn_id}/events"
    ]["get"]
    assert events["responses"]["200"]["content"]["text/event-stream"]["schema"] == {
        "type": "string"
    }
    audio = document["paths"][
        "/v1/nodes/{node_id}/voice/sessions/{session_id}/turns/{turn_id}/segments/{sequence}/audio"
    ]["get"]
    assert audio["responses"]["200"]["content"]["application/octet-stream"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    assert (
        document["components"]["schemas"]["VoiceSessionSnapshot"]["properties"]["created_at"][
            "format"
        ]
        == "date-time"
    )


def test_reporter_is_forbidden_and_unknown_turn_fails_before_sse(voice_runtime):
    client, _, _, _, _, session = voice_runtime
    path = f"/v1/nodes/desk/voice/sessions/{session['session_id']}/turns/missing-turn/events"
    unauthorized = client.get(path, headers={"Authorization": "Bearer invalid"})
    assert unauthorized.status_code == 401
    reporter = client.get(path, headers={"Authorization": "Bearer vl_reporter_invalid"})
    assert reporter.status_code == 403
    missing = client.get(path)
    assert missing.status_code == 404, missing.text
    assert not missing.headers.get("content-type", "").startswith("text/event-stream")


def test_voice_post_limit_counts_actual_body_and_is_route_local(voice_runtime):
    client, app, _, _, _, session = voice_runtime
    payload = _session_body(app, "voice-start")
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(encoded) < MAX_VOICE_JSON_BYTES
    exact = encoded + b" " * (MAX_VOICE_JSON_BYTES - len(encoded))
    url = "/v1/nodes/desk/voice/sessions"
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(exact)),
    }
    accepted = client.post(url, content=exact, headers=headers)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["session_id"] == session["session_id"]

    oversized = exact + b" "
    response = client.post(
        url,
        content=oversized,
        headers={"Content-Type": "application/json", "Content-Length": str(len(exact))},
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "request_too_large"

    unrelated = client.post(
        "/v1/nodes",
        content=oversized,
        headers={"Content-Type": "application/json", "Content-Length": str(len(exact))},
    )
    assert unrelated.status_code != 413


def test_session_requires_declared_local_microphone_capabilities(voice_runtime):
    client, app, _, _, _, previous = voice_runtime
    base = f"/v1/nodes/desk/voice/sessions/{previous['session_id']}"
    stopped = client.post(base + "/stop", json={"command_id": "stop-before-capability-check"})
    assert stopped.status_code == 200, stopped.text
    app.state.coordinator.register_node(
        {
            "node_id": "desk",
            "label": "desk",
            "kind": "desktop",
            "capabilities": ["text.display"],
        }
    )
    body = _session_body(
        app,
        "requires-local-asr",
        mode="continuous",
        audio_enabled=False,
        asr={"engine": "sherpa_onnx", "model_id": "streaming-zh", "language": "zh"},
    )
    response = client.post("/v1/nodes/desk/voice/sessions", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "voice_input_invalid"


def test_asr_turn_model_must_match_session(voice_runtime):
    client, app, clock, _, _, previous = voice_runtime
    session = _replace_session(
        client,
        app,
        previous,
        "asr-session",
        mode="continuous",
        asr={"engine": "sherpa_onnx", "model_id": "streaming-zh", "language": "zh"},
    )
    ended = clock.value - timedelta(seconds=1)
    started = ended - timedelta(seconds=1)
    asr_input = {
        "kind": "asr",
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "ended_at": ended.isoformat().replace("+00:00", "Z"),
        "audio_duration_ms": 1000,
        "asr_model_id": "different-model",
        "language": "zh",
        "discontinuity": False,
    }
    response = client.post(
        f"/v1/nodes/desk/voice/sessions/{session['session_id']}/turns",
        json=_turn_body(app, session, "bad-asr-model", input_value=asr_input),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "voice_input_invalid"


def test_http_sse_snapshot_after_sequence_and_replay_gap(voice_runtime):
    client, app, _, _, _, previous = voice_runtime
    session = _replace_session(client, app, previous, "http-sse-session")
    body = _turn_body(app, session, "http-sse-turn")
    accepted = client.post(
        f"/v1/nodes/desk/voice/sessions/{session['session_id']}/turns", json=body
    )
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    assert app.state.voice.process_next()

    path = (
        f"/v1/nodes/desk/voice/sessions/{session['session_id']}"
        f"/turns/{receipt['turn_id']}/events?after_sequence={receipt['sequence']}"
    )
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    snapshot = next(data for kind, data in frames if kind == "snapshot")
    assert snapshot["session_id"] == session["session_id"]
    assert snapshot["session_generation"] == session["session_generation"]
    assert snapshot["turn_id"] == receipt["turn_id"]
    assert snapshot["output_generation"] == receipt["output_generation"]
    assert snapshot["sequence"] == snapshot["cursor"] == snapshot["turn"]["sequence"]
    assert snapshot["replay_gap"] is False
    replayed = [data for kind, data in frames if kind != "snapshot"]
    assert replayed and all(item["sequence"] > receipt["sequence"] for item in replayed)
    assert frames[-1][0] == "voice_end"

    for index in range(app.state.voice.config.event_buffer_size + 2):
        app.state.voice._append_event_standalone(
            receipt["turn_id"], "status", {"stage": f"retained-{index}"}
        )
    gap_response = client.get(
        f"/v1/nodes/desk/voice/sessions/{session['session_id']}"
        f"/turns/{receipt['turn_id']}/events?after_sequence=0"
    )
    assert gap_response.status_code == 200, gap_response.text
    gap_frames = _sse_frames(gap_response.text)
    gap_snapshot = next(data for kind, data in gap_frames if kind == "snapshot")
    assert gap_snapshot["replay_gap"] is True
    assert gap_snapshot["cursor"] == gap_snapshot["turn"]["sequence"]
    assert [kind for kind, _ in gap_frames] == ["snapshot", "voice_end"]


def test_audio_get_uses_bounded_chunks_and_metadata_headers(voice_runtime, monkeypatch):
    client, app, _, _, _, previous = voice_runtime
    session = _replace_session(client, app, previous, "http-audio-session")
    body = _turn_body(app, session, "http-audio-turn")
    accepted = client.post(
        f"/v1/nodes/desk/voice/sessions/{session['session_id']}/turns", json=body
    )
    assert accepted.status_code == 202, accepted.text
    assert app.state.voice.process_next()

    voice = app.state.voice
    original_iter_audio = voice.iter_audio
    requested_chunks = []

    def recording_iter_audio(node_id, session_id, turn_id, sequence, chunk_bytes=8192):
        requested_chunks.append(chunk_bytes)
        return original_iter_audio(node_id, session_id, turn_id, sequence, chunk_bytes=chunk_bytes)

    monkeypatch.setattr(voice, "iter_audio", recording_iter_audio)
    response = client.get(
        f"/v1/nodes/desk/voice/sessions/{session['session_id']}"
        "/turns/http-audio-turn/segments/1/audio"
    )
    assert response.status_code == 200, response.text
    assert requested_chunks == [AUDIO_CHUNK_BYTES]
    assert requested_chunks[0] == 8192
    assert response.headers["content-type"].startswith("audio/pcm")
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["x-audio-sha256"] == hashlib.sha256(response.content).hexdigest()
