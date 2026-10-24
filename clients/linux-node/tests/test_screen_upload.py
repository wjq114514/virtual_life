import asyncio
from types import SimpleNamespace

import pytest

from virtual_life_linux.capture import CapturedFrame, CaptureError, GrimCapture, png_dimensions
from virtual_life_linux.config import load_config
from virtual_life_linux.screen_client import ScreenStreamer

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png(width=64, height=48):
    ihdr = (
        (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    return PNG_SIGNATURE + ihdr


class FakeCapture:
    name = "fake"

    def __init__(self, frames=None, error=None):
        self.frames = list(frames or [])
        self.error = error
        self.calls = 0

    def available(self):
        return True

    async def capture(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        frame = self.frames[min(self.calls - 1, len(self.frames) - 1)]
        return frame


class FakeTransport:
    def __init__(self, advice=None):
        self.advice = advice or {"sample_interval_ms": 10, "min_upload_interval_ms": 10}
        self.sessions = []
        self.uploads = []
        self.contents = []
        self.frames = []
        self.stopped = []

    async def screen_create_session(self, node_id, payload):
        self.sessions.append(payload)
        return {"session_id": "session-1", "generation": 1, "advice": self.advice}

    async def screen_stop_session(self, node_id, session_id, command_id):
        self.stopped.append(session_id)
        return {"status": "stopped"}

    async def screen_register_upload(self, node_id, payload):
        self.uploads.append(payload)
        return {"upload_id": f"upload-{len(self.uploads)}"}

    async def screen_upload_content(self, node_id, upload_id, data, media_type):
        self.contents.append((upload_id, data, media_type))
        return {"upload_id": upload_id, "status": "ready"}

    async def screen_submit_frame(self, node_id, payload):
        self.frames.append(payload)
        return {"frame_id": f"frame-{len(self.frames)}", "status": "accepted"}


class FakeScene:
    def __init__(self):
        self.client_features = {"schema_version": "1.0", "extractor": "niri", "ui_elements": []}
        self.host_metadata = {"app_id": "firefox"}

    async def collect(self):
        return SimpleNamespace(
            client_features=self.client_features, host_metadata=self.host_metadata
        )


def make_streamer(transport, capture, *, mode="continuous", scene=None):
    streamer = ScreenStreamer(
        transport,
        "linux-workstation",
        "sui",
        mode=mode,
        capture=capture,
        scene_collector=scene,
    )
    streamer.state.session_id = "session-1"
    streamer.state.generation = 1
    return streamer


def test_png_dimensions_reads_ihdr():
    assert png_dimensions(png(128, 72)) == (128, 72)
    with pytest.raises(CaptureError):
        png_dimensions(b"not a png")


def test_grim_capture_reports_missing_binary():
    assert GrimCapture("definitely-not-a-real-binary").available() is False


async def test_one_shot_session_publishes_and_stops():
    transport = FakeTransport()
    capture = FakeCapture([CapturedFrame(data=png(), width=64, height=48)])
    streamer = ScreenStreamer(
        transport,
        "linux-workstation",
        "sui",
        mode="one_shot",
        capture=capture,
        scene_collector=FakeScene(),
    )
    await streamer.start()
    await asyncio.wait_for(streamer._task, timeout=5)

    assert len(transport.sessions) == 1
    assert transport.sessions[0]["subject_node_id"] == "linux-workstation"
    assert transport.sessions[0]["requested_profile"] == "balanced"
    assert transport.frames[0]["trigger"] == "first_frame"
    assert transport.frames[0]["client_features"]["extractor"] == "niri"
    assert transport.frames[0]["host_metadata"] == {"app_id": "firefox"}
    assert transport.uploads[0]["sha256"]
    assert transport.contents[0][2] == "image/png"

    result = await streamer.stop()
    assert result["running"] is False
    assert transport.stopped == ["session-1"]


async def test_triggers_switch_between_first_changed_and_refresh():
    transport = FakeTransport()
    streamer = make_streamer(transport, FakeCapture([CapturedFrame(png(), 64, 48)]))
    same = CapturedFrame(data=png(), width=64, height=48)
    other = CapturedFrame(data=png() + b"tail", width=64, height=48)

    await streamer._publish(same)
    await streamer._publish(same)
    await streamer._publish(other)

    triggers = [frame["trigger"] for frame in transport.frames]
    assert triggers == ["first_frame", "refresh", "changed"]


async def test_capture_failure_submits_unavailable_frame():
    transport = FakeTransport()
    capture = FakeCapture(error=CaptureError("grim failed"))
    streamer = ScreenStreamer(
        transport, "linux-workstation", "sui", mode="one_shot", capture=capture
    )
    await streamer.start()
    await asyncio.wait_for(streamer._task, timeout=5)

    assert transport.frames[0]["input"]["kind"] == "unavailable"
    assert transport.frames[0]["input"]["reason"] == "capture_failed"


def test_screen_config_section_is_parsed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                'server_url = "http://127.0.0.1:8038"',
                'node_id = "linux-workstation"',
                'label = "ws"',
                "[screen]",
                "enabled = true",
                'companion_id = "sui"',
                'profile = "detail"',
                'mode = "one_shot"',
                'capture_binary = "grim"',
                'capture_source = "wayland_portal"',
            ]
        )
    )
    config = load_config(path)
    assert config.screen.enabled is True
    assert config.screen.companion_id == "sui"
    assert config.screen.profile == "detail"
    assert config.screen.mode == "one_shot"
    assert config.screen.capture_source == "wayland_portal"
