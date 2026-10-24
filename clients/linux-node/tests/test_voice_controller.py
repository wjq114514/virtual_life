"""Controller session lifecycle with a fake transport (no audio hardware)."""

import pytest

from virtual_life_linux.config import load_config
from virtual_life_linux.voice.controller import VoiceController, VoiceControllerError

CONFIG = """
schema_version = 1
server_url = "http://127.0.0.1:8038"
node_id = "node-a"
device_id = "device-a"
label = "WS"
"""

OWNER = {
    "companion_id": "sui",
    "epoch": 3,
    "revision": 5,
    "presentation_id": "pres-1",
}


def make_config(tmp_path, *, voice_enabled=True):
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG + f"\n[voice]\nenabled = {str(voice_enabled).lower()}\n", encoding="utf-8"
    )
    return load_config(path)


class FakeTransport:
    def __init__(self):
        self.created = []
        self.submitted = []
        self.stopped = []
        self.heartbeats = 0

    async def voice_create_session(self, node_id, payload):
        self.created.append(payload)
        return {
            "session_id": "sess-1",
            "command_id": payload["command_id"],
            "companion_id": payload["companion_id"],
            "node_id": node_id,
            "presentation_id": payload["presentation_id"],
            "mode": payload["mode"],
            "epoch": payload["epoch"],
            "session_generation": 1,
            "output_generation": 0,
            "status": "active",
            "audio_enabled": True,
            "effective_voice": "default",
            "asr": payload.get("asr"),
            "heartbeat_interval_seconds": 10,
        }

    async def voice_submit_turn(self, node_id, session_id, payload):
        self.submitted.append(payload)
        return {
            "turn_id": payload["turn_id"],
            "session_generation": payload["session_generation"],
            "output_generation": 1,
        }

    async def voice_session_heartbeat(self, node_id, session_id, command_id):
        self.heartbeats += 1
        return {"session_id": session_id, "status": "active", "session_generation": 1}

    async def voice_stop_session(self, node_id, session_id, command_id):
        self.stopped.append((node_id, session_id))

    async def stream_voice_turn_events(self, node_id, session_id, turn_id):
        yield "voice_end", {"sequence": 1}


def make_controller(tmp_path, *, voice_enabled=True):
    transport = FakeTransport()
    controller = VoiceController(
        make_config(tmp_path, voice_enabled=voice_enabled),
        transport,
        owner_context=lambda: _owner(),
        authorization_error=lambda: None,
        publish=_publish,
        lock_state=_unlocked,
    )
    return controller, transport


async def _owner():
    return dict(OWNER)


async def _publish(_event, _data):
    return None


async def _unlocked():
    return False


async def test_text_session_commit_and_stop(tmp_path):
    controller, transport = make_controller(tmp_path)
    try:
        status = await controller.start("text")
        assert status["active"] is True
        assert status["mode"] == "text"
        assert transport.created[0]["mode"] == "text"
        assert transport.created[0]["asr"] is None

        result = await controller.commit("你好，今天天气不错。")
        assert result["submitted"] is True
        payload = transport.submitted[0]
        assert payload["input"] == {"kind": "text", "mode": "manual"}
        assert payload["end_reason"] == "manual"
        assert payload["text"] == "你好，今天天气不错。"
    finally:
        await controller.stop()
    assert transport.stopped == [("node-a", "sess-1")]
    assert controller.active is False


async def test_commit_without_text_requires_push_to_talk(tmp_path):
    controller, _ = make_controller(tmp_path)
    try:
        await controller.start("text")
        with pytest.raises(VoiceControllerError) as error:
            await controller.commit(None)
        assert error.value.code == "bad_voice_text"
    finally:
        await controller.stop()


async def test_push_to_talk_requires_a_local_model(tmp_path):
    controller, _ = make_controller(tmp_path)
    with pytest.raises(VoiceControllerError) as error:
        await controller.start("push_to_talk")
    assert error.value.code == "voice_model_missing"


async def test_unknown_mode_is_rejected(tmp_path):
    controller, _ = make_controller(tmp_path)
    with pytest.raises(VoiceControllerError) as error:
        await controller.start("singing")
    assert error.value.code == "bad_voice_mode"


async def test_disabled_voice_is_rejected(tmp_path):
    controller, _ = make_controller(tmp_path, voice_enabled=False)
    with pytest.raises(VoiceControllerError) as error:
        await controller.start("text")
    assert error.value.code == "voice_disabled"
