import pytest

from virtual_life_linux.config import ConfigError
from virtual_life_linux.tui import app

CONFIG = """
schema_version = 1
server_url = "http://127.0.0.1:8038"
node_id = "node-a"
device_id = "device-a"
label = "WS"
"""


def test_run_resolves_socket_from_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    seen: dict = {}

    class FakeApp:
        def __init__(self, socket_path, companion_id=None):
            seen["socket"] = socket_path
            seen["companion"] = companion_id

        def run(self):
            seen["ran"] = True

    monkeypatch.setattr(app, "VirtualLifeApp", FakeApp)
    app.run(config_path, "companion")
    assert seen["ran"] is True
    assert seen["companion"] == "companion"
    # server-key / node-id scoped so two runtimes on one seat do not collide.
    assert seen["socket"].name == "daemon.sock"
    assert seen["socket"].parent.name == "node-a"


def test_run_rejects_a_missing_config(tmp_path):
    with pytest.raises(ConfigError):
        app.run(tmp_path / "missing.toml")
