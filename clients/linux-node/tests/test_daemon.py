from datetime import datetime, timedelta, timezone

import pytest

from virtual_life_linux.adapters import AttentionObservation
from virtual_life_linux.config import load_config
from virtual_life_linux.daemon import DISPLAY_CAPABILITY, LinuxDaemon
from virtual_life_linux.ipc import IpcError
from virtual_life_linux.storage import ClientStorage
from virtual_life_linux.transport import ServerError

CONFIG = """
schema_version = 1
server_url = "http://127.0.0.1:8038"
node_id = "node-a"
device_id = "device-a"
label = "WS"

[desktop]
backend = "niri"
attention_enabled = true
"""


def _iso(offset_seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


class FakeTransport:
    def __init__(self):
        self.registered: list[dict] = []
        self.released: list[str] = []
        self.stream_events: list[tuple[str, dict]] = [
            ("status", {"stage": "preparing"}),
            ("delta", {"sequence": 1, "text": "你好"}),
            ("result", {"status": "completed", "assistant_message": "你好"}),
        ]
        self.companion = {"companion_id": "comp", "active_node_id": "node-a", "epoch": 3}
        self.closed = False
        self.attention_payloads: list[dict] = []
        self.heartbeat_presentation_calls: list[str] = []

    async def require_native_presentation(self):
        return None

    async def register_node(self, **kwargs):
        self.registered.append(kwargs)
        return {
            "node_id": kwargs["node_id"],
            "label": kwargs["label"],
            "online": True,
            "capabilities": kwargs["capabilities"],
        }

    async def heartbeat(self, node_id, context=None):
        return {"node_id": node_id, "online": True, "capabilities": []}

    async def events(self, sequence, *, node_id=None, limit=100):
        return {"events": [], "next_sequence": sequence}

    async def open_presentation(self, node_id, presentation_id):
        return {
            "node_id": node_id,
            "presentation_id": presentation_id,
            "kind": "terminal",
            "expires_at": _iso(15),
            "ttl_seconds": 15,
            "server_time": _iso(0),
            "opened_sequence": 42,
        }

    async def heartbeat_presentation(self, node_id, presentation_id):
        self.heartbeat_presentation_calls.append(presentation_id)
        return {
            "node_id": node_id,
            "presentation_id": presentation_id,
            "expires_at": _iso(15),
            "ttl_seconds": 15,
            "server_time": _iso(0),
        }

    async def release_presentation(self, node_id, presentation_id):
        self.released.append(presentation_id)
        return {"released": True}

    async def get_companion(self, companion_id):
        return self.companion

    async def renew_lease(self, companion_id, node_id, epoch):
        return {
            "companion_id": companion_id,
            "node_id": node_id,
            "epoch": epoch,
            "expires_at": _iso(15),
            "ttl_seconds": 15,
            "server_time": _iso(0),
        }

    async def list_companions(self):
        return [self.companion]

    async def stream_dialogue(self, companion_id, payload):
        for event in self.stream_events:
            yield event

    async def dialogue_result(self, companion_id, command_id):
        return {"command_id": command_id, "status": "completed"}

    async def dialogue_history(self, companion_id, *, before_sequence=None, limit=20):
        return {"items": [], "next_before_sequence": None}

    async def prepare_transfer(self, companion_id, payload):
        return {"transfer_id": "t1"}

    async def ack_transfer(self, transfer_id, payload):
        return {"transfer_id": transfer_id, "status": "committed"}

    async def get_transfer(self, transfer_id):
        return {"transfer_id": transfer_id}

    async def abort_transfer(self, transfer_id):
        return {"transfer_id": transfer_id, "status": "aborted"}

    async def aclose(self):
        self.closed = True

    async def attention(self, node_id, payload):
        self.attention_payloads.append(payload)
        return {"node_id": node_id, "source_event_id": "evt-1"}


class FakeAdapter:
    name = "fake"

    def __init__(self, observations: list):
        self.observations = observations
        self.calls = 0

    def available(self) -> bool:
        return True

    async def collect(self):
        self.calls += 1
        return list(self.observations)


class BoomAdapter:
    name = "boom"

    def available(self) -> bool:
        return True

    async def collect(self):
        raise RuntimeError("boom")


@pytest.fixture
def daemon(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    config = load_config(config_path)
    storage = ClientStorage(tmp_path / "state.sqlite")
    transport = FakeTransport()
    instance = LinuxDaemon(
        config, storage, transport, runtime_dir=tmp_path / "run", generation="gen-x"
    )
    yield instance
    storage.close()


async def test_capabilities_projection(daemon):
    assert DISPLAY_CAPABILITY not in daemon.capabilities()
    assert "attention.observe" in daemon.capabilities()


async def test_attach_opens_presentation_and_projects_capability(daemon):
    result = await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    assert result["presentation_id"]
    assert result["opened_sequence"] == 42
    assert DISPLAY_CAPABILITY in daemon.capabilities()
    assert daemon.transport.registered[-1]["capabilities"].count(DISPLAY_CAPABILITY) == 1


async def test_detach_releases_presentation(daemon):
    attach = daemon.router()["renderer.attach"]
    await attach({"companion_id": "comp"}, object())
    pid = daemon.renderer.presentation_id
    await daemon.router()["renderer.detach"]({"renderer_id": daemon.renderer.renderer_id}, object())
    assert daemon.renderer is None
    assert daemon.transport.released == [pid]


async def test_replaced_generation_rejected(daemon):
    await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    with pytest.raises(IpcError) as excinfo:
        await daemon.router()["renderer.heartbeat"]({"renderer_id": "other"}, object())
    assert excinfo.value.code == "renderer_replaced"


async def test_connect_blocks_when_companion_elsewhere(daemon):
    await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    daemon.transport.companion = {"companion_id": "comp", "active_node_id": "other", "epoch": 3}
    with pytest.raises(IpcError) as excinfo:
        await daemon.router()["companion.connect"]({"companion_id": "comp"}, object())
    assert excinfo.value.code == "companion_elsewhere"


async def test_dialogue_send_returns_receipt(daemon):
    await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    daemon.renderer.epoch = 3
    result = await daemon.router()["dialogue.send"](
        {"text": "你好", "command_id": "cmd-1"}, object()
    )
    assert result["result"]["status"] == "completed"
    row = daemon.storage.get_command("cmd-1")
    assert row is not None and row.status == "settled"


async def test_dialogue_send_requires_renderer(daemon):
    with pytest.raises(IpcError) as excinfo:
        await daemon.router()["dialogue.send"]({"text": "hi"}, object())
    assert excinfo.value.code == "renderer_not_attached"


async def test_dialogue_send_rejects_expired_lease(daemon):
    await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    daemon.renderer.epoch = 3
    daemon.renderer.render_deadline_ns = 0
    with pytest.raises(IpcError) as excinfo:
        await daemon.router()["dialogue.send"]({"text": "hi"}, object())
    assert excinfo.value.code == "lease_expired"


async def test_open_web_only_advertises_configured_origin(daemon):
    result = await daemon.router()["webui.open"]({}, object())
    assert result["url"].startswith(daemon.config.normalized_server_url)
    assert result["opened"] is False


# -- attention ------------------------------------------------------------
def _idle_session() -> list[AttentionObservation]:
    return [
        AttentionObservation(
            source="logind",
            kind="session",
            data={"locked_hint": False, "idle_hint": True},
        )
    ]


def _focused_workspace() -> list[AttentionObservation]:
    return [
        AttentionObservation(
            source="niri",
            kind="workspace",
            data={"workspace_id": 2, "workspace_idx": 2, "output": "DP-1"},
        )
    ]


async def test_attention_sample_is_deduped(daemon):
    adapter = FakeAdapter(_idle_session())
    daemon.attention_source = [adapter]

    sample = await daemon._emit_attention_sample()
    assert sample is not None
    assert sample["session_id"] == "node-a-attn"
    assert sample["sequence"] == 1
    assert sample["focus"] == "unknown"
    assert sample["availability"] == "available"
    assert sample["locked"] is False
    assert len(daemon.transport.attention_payloads) == 1
    payload = daemon.transport.attention_payloads[0]
    assert payload["command_id"]
    assert payload["sample"] == sample

    # Unchanged content is not re-sent and does not consume another sequence.
    assert await daemon._emit_attention_sample() is None
    assert len(daemon.transport.attention_payloads) == 1
    assert daemon.attention_sequence == 1
    assert daemon.storage.get("attention:node-a-attn") == "1"
    assert adapter.calls == 2


async def test_attention_sequence_survives_restart(tmp_path, daemon):
    daemon.attention_source = [FakeAdapter(_focused_workspace())]
    sample = await daemon._emit_attention_sample()
    assert sample["focus"] == "focused"

    storage = ClientStorage(tmp_path / "state.sqlite")
    try:
        restarted = LinuxDaemon(
            daemon.config,
            storage,
            FakeTransport(),
            runtime_dir=tmp_path / "run2",
            generation="gen-y",
        )
        assert restarted.attention_sequence == daemon.attention_sequence
    finally:
        storage.close()


async def test_attention_boundary_is_emitted_immediately(daemon):
    daemon.attention_source = [FakeAdapter(_idle_session())]
    result = await daemon.router()["attention.boundary"]({"boundary": "task_completed"}, object())
    assert result == {"boundary": "task_completed", "accepted": True, "sampled": True}
    sample = daemon.transport.attention_payloads[0]["sample"]
    assert sample["boundary"] == "task_completed"
    assert sample["boundary_id"]
    assert daemon._pending_boundary is None


async def test_attention_boundary_reports_failure_and_stays_queued(daemon):
    daemon.attention_source = [FakeAdapter(_idle_session())]

    async def stale(node_id, payload):
        raise ServerError("attention_stale", "sample expired")

    daemon.transport.attention = stale
    result = await daemon.router()["attention.boundary"]({"boundary": "task_completed"}, object())
    assert result["sampled"] is False
    assert daemon.last_attention_error["code"] == "attention_stale"
    assert daemon._pending_boundary["boundary"] == "task_completed"


async def test_attention_bad_boundary_rejected(daemon):
    with pytest.raises(IpcError) as excinfo:
        await daemon.router()["attention.boundary"]({"boundary": "napping"}, object())
    assert excinfo.value.code == "bad_boundary"


async def test_attention_sample_stays_within_server_contract(daemon):
    sample = daemon._attention_sample([], None)
    assert sample["schema_version"] == "1.0"
    assert sample["ttl_seconds"] <= 300
    assert sample["activity"] in {
        "code",
        "docs",
        "chat",
        "meeting",
        "video",
        "game",
        "media",
        "terminal",
        "browser",
        "unknown",
    }
    assert sample["focus"] in {"focused", "unfocused", "suspected_stuck", "unknown"}
    assert sample["availability"] in {"available", "busy", "unknown"}
    assert sample["boundary"] == "none"
    assert sample["boundary_id"] is None


async def test_attention_disabled(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG.replace("attention_enabled = true", "attention_enabled = false"))
    config = load_config(config_path)
    storage = ClientStorage(tmp_path / "state.sqlite")
    instance = LinuxDaemon(
        config, storage, FakeTransport(), runtime_dir=tmp_path / "run", generation="gen-x"
    )
    try:
        assert "attention.observe" not in instance.capabilities()
        assert instance._attention_enabled() is False
        with pytest.raises(IpcError) as excinfo:
            await instance.router()["attention.boundary"]({"boundary": "task_completed"}, object())
        assert excinfo.value.code == "attention_disabled"
    finally:
        storage.close()


# -- presentation ownership -------------------------------------------------
async def test_presentation_heartbeat_refreshes_deadline(daemon):
    await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    pid = daemon.renderer.presentation_id
    assert daemon.renderer.render_deadline_ns is not None

    await daemon._heartbeat_presentation_once()
    assert daemon.transport.heartbeat_presentation_calls == [pid]
    assert daemon.renderer.presentation_id == pid
    assert daemon.renderer.render_deadline_ns is not None


async def test_presentation_expiry_drops_display_capability(daemon):
    await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    assert DISPLAY_CAPABILITY in daemon.capabilities()

    async def expired(node_id, presentation_id):
        raise ServerError("presentation_expired", "session expired")

    daemon.transport.heartbeat_presentation = expired
    await daemon._heartbeat_presentation_once()
    assert daemon.renderer.presentation_id is None
    assert daemon.renderer.render_deadline_ns is None
    assert DISPLAY_CAPABILITY not in daemon.capabilities()
    assert daemon.last_error["code"] == "presentation_expired"


async def test_status_and_diagnostics_report_attention(daemon):
    daemon.attention_source = [FakeAdapter(_idle_session())]
    await daemon._emit_attention_sample()
    status = daemon._status()
    assert status["attention"] == {
        "enabled": True,
        "session_id": "node-a-attn",
        "sequence": 1,
        "last_error": None,
    }
    diagnostics = await daemon.router()["diagnostics.get"]({}, object())
    assert diagnostics["attention"]["adapters"] == ["fake"]
    assert diagnostics["last_error"] is None


# -- companion selection ---------------------------------------------------
async def test_companion_list_autoselects_the_first(daemon):
    result = await daemon.router()["companion.list"]({}, object())
    assert result["companions"][0]["companion_id"] == "comp"
    assert result["selected"] == "comp"
    assert daemon.selected_companion == "comp"


async def test_companion_select_only_changes_the_selection(daemon):
    result = await daemon.router()["companion.select"]({"companion_id": "comp"}, object())
    assert result == {"selected": "comp"}
    assert daemon.selected_companion == "comp"
    assert daemon.renderer is None  # selection alone does not open a presentation


async def test_attach_establishes_the_lease(daemon):
    """Attach is the entry point, so it must bind a real epoch."""
    result = await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    assert result["companion_id"] == "comp"
    assert result["epoch"] == 3
    assert result["render_deadline_ns"] is not None
    assert daemon.lease is not None

    # dialogue.send carries the real epoch instead of 0.
    receipt = await daemon.router()["dialogue.send"]({"text": "你好"}, object())
    assert receipt["result"]["status"] == "completed"


async def test_attach_rejects_a_companion_elsewhere(daemon):
    async def elsewhere(companion_id):
        return {"companion_id": companion_id, "active_node_id": "node-b", "epoch": 1}

    daemon.transport.get_companion = elsewhere
    with pytest.raises(IpcError) as excinfo:
        await daemon.router()["renderer.attach"]({"companion_id": "comp"}, object())
    assert excinfo.value.code == "companion_elsewhere"
