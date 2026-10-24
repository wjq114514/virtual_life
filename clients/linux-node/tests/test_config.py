import os
from pathlib import Path

import pytest

from virtual_life_linux.config import (
    ConfigError,
    default_runtime_dir,
    load_config,
    normalize_server_url,
    runtime_dir_for,
    server_key,
)

MINIMAL = """
schema_version = 1
server_url = "http://127.0.0.1:8038"
node_id = "linux-workstation"
device_id = "install-abc"
label = "Linux 工作站"

[desktop]
backend = "niri"

[reporting]
heartbeat_seconds = 20
attention_ttl_seconds = 45
"""


def write_config(tmp_path: Path, text: str = MINIMAL) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_defaults(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.node_id == "linux-workstation"
    assert config.device_id == "install-abc"
    assert config.desktop.backend == "niri"
    assert config.desktop.attention_enabled is False
    assert config.interaction.notifications == "status_only"
    assert config.reporting.heartbeat_seconds == 20
    assert config.normalized_server_url == "http://127.0.0.1:8038"


def test_unknown_field_rejected(tmp_path):
    text = MINIMAL + "\nunknown_knob = true\n"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_device_id_defaults_to_node(tmp_path):
    text = MINIMAL.replace('device_id = "install-abc"\n', "")
    config = load_config(write_config(tmp_path, text))
    assert config.device_id == "linux-workstation"


def test_server_key_isolates_nodes(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.server_key == server_key("http://127.0.0.1:8038")
    other = server_key("http://127.0.0.1:8039")
    assert config.server_key != other


def test_normalize_rejects_credentials_and_query():
    with pytest.raises(ConfigError):
        normalize_server_url("http://user:pass@host:1")
    with pytest.raises(ConfigError):
        normalize_server_url("http://host:1/?a=1")
    assert normalize_server_url("https://example.com/") == "https://example.com:443"


def test_credential_must_stay_inside_config_dir(tmp_path):
    text = MINIMAL.replace(
        'label = "Linux 工作站"', 'label = "Linux 工作站"\ncredential_file = "../secret"'
    )
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_load_token_enforces_permissions(tmp_path):
    config = load_config(write_config(tmp_path))
    path = config.credential_path
    path.write_text("token-123", encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load_token()
    os.chmod(path, 0o600)
    assert config.load_token() == "token-123"


def test_reporting_bounds_enforced(tmp_path):
    text = MINIMAL.replace("heartbeat_seconds = 20", "heartbeat_seconds = 100")
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


def test_runtime_dir_is_scoped_by_server_and_node(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    config = load_config(write_config(tmp_path))
    expected = tmp_path / "run" / "virtual-life" / config.server_key / "linux-workstation"
    assert runtime_dir_for(config) == config.runtime_dir == expected

    other_path = tmp_path / "other.toml"
    other_path.write_text(MINIMAL.replace("127.0.0.1:8038", "127.0.0.1:9000"), encoding="utf-8")
    other = load_config(other_path)
    assert runtime_dir_for(other) != runtime_dir_for(config)


def test_default_runtime_dir_is_the_unscoped_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert default_runtime_dir() == tmp_path / "run" / "virtual-life"


def test_runtime_dir_requires_xdg_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    config = load_config(write_config(tmp_path))
    with pytest.raises(ConfigError):
        runtime_dir_for(config)
