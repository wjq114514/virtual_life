import pytest

from virtual_life_linux.config import load_config
from virtual_life_linux.policy import AttentionPolicy
from virtual_life_linux.state import Renderer, build_node_context, build_status

CONFIG = """
schema_version = 1
server_url = "http://127.0.0.1:8038"
node_id = "node-a"
device_id = "device-a"
label = "WS"

[reporting]
attention_ttl_seconds = 30
"""


def make_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return load_config(path)


def test_attention_override_expires(tmp_path):
    policy = AttentionPolicy(ttl_seconds=45)
    policy.set_override("focus", 900, now=100.0)
    assert policy.current(100.0) == {"state": "focus", "expires_in_seconds": 900}
    # capped by the policy ttl rather than the requested 900s
    assert policy.current(100.0 + 44.0) is not None
    assert policy.current(100.0 + 46.0) is None


def test_attention_override_validation():
    policy = AttentionPolicy()
    with pytest.raises(ValueError):
        policy.set_override("sleeping", 10, now=0.0)
    with pytest.raises(ValueError):
        policy.set_override("focus", 0, now=0.0)
    policy.set_override("away", 10, now=0.0)
    policy.clear()
    assert policy.current(0.0) is None


def test_effective_state_defaults(tmp_path):
    policy = AttentionPolicy(ttl_seconds=30)
    assert policy.effective_state(default="quiet", now=0.0) == "quiet"
    policy.set_override("break", 30, now=0.0)
    assert policy.effective_state(default="quiet", now=1.0) == "break"


def test_build_status_shape(tmp_path):
    config = make_config(tmp_path)
    renderer = Renderer(renderer_id="r1", generation="g1", node_id="node-a", companion_id="comp")
    status = build_status(
        generation="g1",
        config=config,
        node={"node_id": "node-a", "label": "WS", "capabilities": ["text.display"], "online": True},
        renderer=renderer,
        selected_companion="comp",
        lease=None,
        attention_override=None,
        last_error=None,
    )
    assert status["online"] is True
    assert status["presentation_id"] is None
    assert status["renderer"]["renderer_id"] == "r1"
    assert status["server"]["key"] == config.server_key


def test_build_node_context_omits_empty(tmp_path):
    context = build_node_context(generation="g1", renderer=None, attention_override=None)
    assert context == {"client": "virtual-life-linux", "generation": "g1"}
    renderer = Renderer(renderer_id="r1", generation="g1", node_id="node-a", companion_id="comp")
    context = build_node_context(
        generation="g1", renderer=renderer, attention_override={"state": "focus"}
    )
    assert context["renderer"] == {"companion_id": "comp"}
    assert context["attention_override"] == "focus"
