"""Screen evidence routing through an explicit screen_reference.

The companion may reside on an IM node (QQ/Matrix) while a separate observer
node (e.g. Android) captures the screen. A ``screen_reference`` on the text
dialogue request names that observer; without it the dialogue node itself is
used (legacy single-node behaviour).
"""

import hashlib
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.screen_config import ScreenVisionConfig
from virtual_life.screen_provider import FakeVisionProvider
from virtual_life.settings import Settings

CAPTURE_NODE = "android-phone"
DIALOGUE_NODE = "qq-room"


def png_bytes(width=64, height=48, color=(30, 60, 90), mode="RGB"):
    buffer = io.BytesIO()
    Image.new(mode, (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def register_nodes(client):
    # Observer: capture + attention, no output capability (cannot host).
    response = client.post(
        "/v1/nodes",
        json={
            "node_id": CAPTURE_NODE,
            "label": CAPTURE_NODE,
            "kind": "android",
            "capabilities": ["vision.capture", "attention.observe"],
        },
    )
    assert response.status_code == 201, response.text
    # Dialogue node: output-capable, no vision (the IM client).
    response = client.post(
        "/v1/nodes",
        json={
            "node_id": DIALOGUE_NODE,
            "label": DIALOGUE_NODE,
            "kind": "other",
            "capabilities": ["text.display"],
        },
    )
    assert response.status_code == 201, response.text
    response = client.post("/v1/companions", json={"companion_id": "sui", "name": "随"})
    assert response.status_code == 201, response.text


def session_body(command_id="start-1", mode="continuous", **changes):
    return {
        "command_id": command_id,
        "schema_version": "1.0",
        "companion_id": "sui",
        "subject_node_id": CAPTURE_NODE,
        "display_id": "default",
        "mode": mode,
        "user_initiated": True,
        "capture_source": "android_media_projection",
        "requested_profile": "balanced",
        **changes,
    }


class Runtime:
    def __init__(self, client, app, clock, provider):
        self.client = client
        self.app = app
        self.clock = clock
        self.provider = provider

    def create_session(self, **changes):
        response = self.client.post(
            f"/v1/nodes/{CAPTURE_NODE}/screen/sessions", json=session_body(**changes)
        )
        assert response.status_code == 201, response.text
        return response.json()

    def upload(self, session, payload, media_type="image/png", command_id="up"):
        digest = hashlib.sha256(payload).hexdigest()
        with Image.open(io.BytesIO(payload)) as probe:
            width, height = probe.size
        response = self.client.post(
            f"/v1/nodes/{CAPTURE_NODE}/screen/uploads",
            json={
                "command_id": command_id,
                "session_id": session["session_id"],
                "generation": session["generation"],
                "media_type": media_type,
                "byte_length": len(payload),
                "sha256": digest,
                "width": width,
                "height": height,
            },
        )
        assert response.status_code == 201, response.text
        receipt = response.json()
        response = self.client.put(
            f"/v1/nodes/{CAPTURE_NODE}/screen/uploads/{receipt['upload_id']}/content",
            content=payload,
            headers={"content-type": media_type},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def frame(self, session, sequence, image_input, trigger="changed", command_id=None, **extra):
        body = {
            "command_id": command_id or f"frame-{sequence}",
            "schema_version": "1.0",
            "session_id": session["session_id"],
            "generation": session["generation"],
            "sequence": sequence,
            "sampled_at": self.clock.value.isoformat(),
            "ttl_seconds": 90,
            "trigger": trigger,
            "input": image_input,
            **extra,
        }
        return self.client.post(f"/v1/nodes/{CAPTURE_NODE}/screen/frames", json=body)

    def run_worker(self):
        return self.app.state.screen_worker.process_next()


class DialogueProvider:
    def __init__(self):
        self.calls = 0
        self.contexts = []
        self.images = []

    def complete(self, request):
        self.calls += 1
        self.contexts.append(request.context_text)
        self.images.append(request.images)
        return ModelResult("屏幕里是 scp 命令指南。", "fake", ModelUsage(80, 20))


@pytest.fixture
def dialogue_runtime(tmp_path):
    clock = Clock()
    vision = FakeVisionProvider()
    model = DialogueProvider()
    settings = Settings(
        database_path=str(tmp_path / "screen-ref.sqlite"),
        api_token="test",
        node_ttl_seconds=100000,
        lease_ttl_seconds=100000,
        auto_memory_enabled=False,
        model_base_url="http://model.invalid",
        model_name="fake",
        screen_vision=ScreenVisionConfig(
            enabled=True,
            mode="single_pass",
            base_url="http://vision.invalid/v1",
            model_name="fake-vision",
            storage_path=str(tmp_path / "screen-ref-storage"),
        ),
    )
    app = create_app(settings, model_provider=model, vision_provider=vision)
    app.state.coordinator.clock = clock
    app.state.screen_vision.clock = clock
    app.state.screen_worker.clock = clock
    app.state.dialogue.clock = clock
    client = TestClient(app, headers={"Authorization": "Bearer test"})
    register_nodes(client)
    yield Runtime(client, app, clock, vision), model
    client.close()


def dialogue_body(state, command_id="dialogue", **changes):
    body = {
        "command_id": command_id,
        "node_id": DIALOGUE_NODE,
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "user_message": "屏幕上是什么？",
        "max_output_tokens": 64,
    }
    body.update(changes)
    return body


def renew_dialogue_lease(client):
    """Move the companion to the IM (dialogue) node and take a fresh lease.

    The companion is created without an active node; follow_room moves it to
    the output-capable IM node, then lease makes it the owner for dialogue.
    """
    state = client.get("/v1/companions/sui").json()
    if state.get("active_node_id") != DIALOGUE_NODE:
        response = client.post(
            "/v1/companions/sui/follow",
            json={
                "command_id": "follow-dialogue",
                "target_node_id": DIALOGUE_NODE,
                "expected_revision": state["revision"],
            },
        )
        assert response.status_code == 200, response.text
        state = client.get("/v1/companions/sui").json()
    response = client.post(
        "/v1/companions/sui/lease",
        json={"node_id": DIALOGUE_NODE, "epoch": state["epoch"]},
    )
    assert response.status_code == 200, response.text
    return client.get("/v1/companions/sui").json()


def image_frame(session, upload, **extra):
    return {
        "kind": "image",
        "upload_id": upload["upload_id"],
        "capture_width": upload["width"],
        "capture_height": upload["height"],
        "rotation_degrees": 0,
        **extra,
    }


def test_screen_reference_routes_to_observer_node(dialogue_runtime):
    """With an explicit screen_reference the dialogue node differs from the
    capture node, yet the fresh frame is still injected."""
    runtime, model = dialogue_runtime
    # Companion starts at the dialogue (IM) node.
    state = renew_dialogue_lease(runtime.client)

    session = runtime.create_session()
    upload = runtime.upload(session, png_bytes(2048, 1200))
    response = runtime.frame(
        session,
        0,
        image_frame(session, upload),
        trigger="first_frame",
        host_metadata={"app_id": "com.tencent.mobileqq", "activity": "chat"},
    )
    assert response.status_code == 202, response.text

    response = runtime.client.post(
        "/v1/companions/sui/dialogue",
        json=dialogue_body(state, screen_reference={"node_id": CAPTURE_NODE}),
    )
    assert response.status_code == 200, response.text
    assert model.calls == 1
    images = model.images[-1]
    assert len(images) == 1
    assert images[0].media_type == "image/jpeg"
    assert images[0].data.startswith(b"\xff\xd8")
    assert "[screen_image: current screen frame" in model.contexts[-1]
    assert "当前前台应用: com.tencent.mobileqq（chat）" in model.contexts[-1]


def test_without_screen_reference_misses_observer_frame(dialogue_runtime):
    """Without a screen_reference the dialogue node is the observer, so the
    observer node's frame is not injected (legacy behaviour)."""
    runtime, model = dialogue_runtime
    state = renew_dialogue_lease(runtime.client)

    session = runtime.create_session()
    upload = runtime.upload(session, png_bytes(2048, 1200))
    response = runtime.frame(session, 0, image_frame(session, upload), trigger="first_frame")
    assert response.status_code == 202, response.text

    response = runtime.client.post(
        "/v1/companions/sui/dialogue",
        json=dialogue_body(state),
    )
    assert response.status_code == 200, response.text
    assert model.calls == 1
    assert model.images[-1] == ()
    assert "[screen_image" not in model.contexts[-1]


def test_screen_reference_uses_captured_frame_after_stale(dialogue_runtime):
    """A stale observer frame is not injected even with a screen_reference."""
    runtime, model = dialogue_runtime
    state = renew_dialogue_lease(runtime.client)

    session = runtime.create_session()
    upload = runtime.upload(session, png_bytes(64, 48))
    response = runtime.frame(session, 0, image_frame(session, upload), trigger="first_frame")
    assert response.status_code == 202, response.text

    runtime.clock.value += timedelta(seconds=120)
    state = renew_dialogue_lease(runtime.client)
    response = runtime.client.post(
        "/v1/companions/sui/dialogue",
        json=dialogue_body(
            state,
            command_id="dialogue-stale",
            screen_reference={"node_id": CAPTURE_NODE},
        ),
    )
    assert response.status_code == 200, response.text
    assert model.images[-1] == ()
    assert "[screen_image" not in model.contexts[-1]
