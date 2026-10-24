"""Run an isolated voice HTTP/SSE/download/cancel rehearsal, never the live DB.

Default: deterministic fake LLM and fake PCM (not an acoustic quality test).
--tts-config FILE: use that file's configured real TTS, still a fake LLM.
Run with development dependencies: uv run python examples/voice_live_test.py
"""

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage, SpeechResult
from virtual_life.settings import Settings


class RehearsalModel:
    def stream(self, request, emit):
        text = "你好，我们可以开始聊天了。"
        emit(text)
        return ModelResult(text, "rehearsal", ModelUsage(32, 12))

    def complete(self, request):
        return self.stream(request, lambda _: None)


class RehearsalSpeech:
    def synthesize(self, request):
        return SpeechResult(
            b"\x00\x00" * 2400,
            "audio/pcm;format=s16le",
            {"sample_rate": 24000, "channels": 1},
        )


def run(directory: Path, tts_config: str | None) -> dict:
    settings = Settings.load(tts_config) if tts_config else Settings()
    settings = replace(
        settings,
        database_path=str(directory / "rehearsal.sqlite"),
        api_token="isolated-rehearsal",
        model_base_url="http://unused.invalid",
        model_name="rehearsal",
        model_api_key="",
        auto_memory_enabled=False,
        embedding_enabled=False,
        lease_ttl_seconds=180,
        node_ttl_seconds=180,
        screen_vision=replace(settings.screen_vision, enabled=False),
        voice=replace(settings.voice, enabled=True, session_ttl_seconds=120),
    )
    app = create_app(
        settings,
        model_provider=RehearsalModel(),
        speech_provider=None if tts_config else RehearsalSpeech(),
    )
    # Only the isolated voice workers run; no real server or live database is used.
    with TestClient(app, headers={"Authorization": "Bearer isolated-rehearsal"}) as client:

        def request(method, path, body=None, expected=200):
            response = (
                client.request(method, path, json=body)
                if body is not None
                else client.request(method, path)
            )
            assert response.status_code == expected, (path, response.status_code, response.text)
            return response.json()

        request(
            "POST",
            "/v1/nodes",
            {
                "node_id": "rehearsal",
                "label": "isolated",
                "kind": "desktop",
                "capabilities": ["text.display", "audio.output"],
            },
            201,
        )
        request("POST", "/v1/companions", {"companion_id": "rehearsal", "name": "随"}, 201)
        transfer = request(
            "POST",
            "/v1/companions/rehearsal/transfers",
            {
                "command_id": "attach",
                "target_node_id": "rehearsal",
                "expected_revision": 0,
            },
            201,
        )
        request(
            "POST",
            f"/v1/transfers/{transfer['transfer_id']}/ack",
            {
                "node_id": "rehearsal",
                "phase": "ready",
            },
        )
        state = request("GET", "/v1/companions/rehearsal")
        request("POST", "/v1/nodes/rehearsal/presentations", {"presentation_id": "rehearsal"}, 201)
        base = "/v1/nodes/rehearsal/voice/sessions"
        session = request(
            "POST",
            base,
            {
                "command_id": "start",
                "companion_id": "rehearsal",
                "epoch": state["epoch"],
                "expected_revision": state["revision"],
                "presentation_id": "rehearsal",
                "mode": "text",
            },
            201,
        )
        base += "/" + session["session_id"]
        body = {
            "command_id": "turn",
            "turn_id": "turn",
            "session_generation": session["session_generation"],
            "expected_output_generation": session["output_generation"],
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "text": "你好",
            "end_reason": "manual",
            "input": {"kind": "text"},
        }
        accepted = request("POST", base + "/turns", body, 202)
        # SSE blocks until the lifespan worker finishes this one queued turn.
        events = client.get(base + "/turns/turn/events")
        assert events.status_code == 200, (events.status_code, events.text)
        assert "event: voice_end" in events.text, events.text
        assert request("POST", base + "/turns", body, 202) == accepted
        result = request("GET", base + "/turns/turn")
        assert result["status"] == "completed", result
        assert result["segment_count"] > 0, result
        audio = client.get(base + "/turns/turn/segments/1/audio")
        assert audio.status_code == 200
        digest = hashlib.sha256(audio.content).hexdigest()
        request(
            "POST",
            base + "/interrupt",
            {
                "command_id": "interrupt",
                "session_generation": session["session_generation"],
                "target_turn_id": "turn",
                "expected_output_generation": accepted["output_generation"],
                "reason": "stop_button",
            },
        )
        revoked = client.get(base + "/turns/turn/segments/1/audio")
        assert revoked.status_code in (409, 410)
        request("POST", base + "/stop", {"command_id": "stop"})
        return {
            "llm": "fake",
            "tts": "configured-real" if tts_config else "fake-silent-pcm",
            "isolated_database": True,
            "status": result["status"],
            "segments": result["segment_count"],
            "first_segment_bytes": len(audio.content),
            "first_segment_sha256": digest,
            "revoked_http_status": revoked.status_code,
            "sse_voice_end": True,
            "idempotent_replay": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tts-config", help="Use real TTS from this TOML; no live DB access")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="virtual-life-voice-") as directory:
        print(json.dumps(run(Path(directory), args.tts_config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
