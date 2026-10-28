"""Asset and runtime file installation."""

import stat

import pytest

from virtual_life_linux import installer
from virtual_life_linux.config import load_config


def test_asset_collection_reads_the_bundled_tree():
    assets = installer.collect_assets()
    assert "systemd/virtual-life-linux.service" in assets
    assert "fish/conf.d/virtual-life.fish" in assets
    assert "fish/completions/vl.fish" in assets
    assert "niri/virtual-life.kdl" in assets
    assert "desktop/dev.virtuallife.Linux.desktop" in assets


def test_install_assets_never_overwrites_without_clobber(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    payload = installer.collect_assets()["fish/completions/vl.fish"]
    target = tmp_path / "cfg" / "fish" / "completions" / "vl.fish"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("USER EDITED\n", encoding="utf-8")

    notes = installer.install_assets()
    assert any(note.startswith("fish: kept (existing)") for note in notes)
    assert target.read_text(encoding="utf-8") == "USER EDITED\n"

    installer.install_assets(clobber=True)
    assert target.read_bytes() == payload


def test_install_assets_skips_the_unit_when_systemd_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    installer.install_assets(with_systemd=False)
    assert not (tmp_path / "cfg" / "systemd").exists()


def test_install_assets_reports_each_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    notes = installer.install_assets(dry_run=True)
    assert any(note.startswith("systemd: would write") for note in notes)
    assert any(note.startswith("desktop: would write") for note in notes)
    assert not (tmp_path / "cfg").exists()


def test_install_credentials_sets_0600(tmp_path):
    state = installer.install_credentials("owner-token", config_root=tmp_path)
    path = tmp_path / "credentials"
    assert state == "wrote (chmod 600)"
    assert path.read_text(encoding="utf-8") == "owner-token\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_credentials_are_kept(tmp_path):
    target = tmp_path / "credentials"
    target.write_text("old-token\n", encoding="utf-8")
    assert installer.install_credentials("new-token", config_root=tmp_path) == "kept (existing)"
    assert target.read_text(encoding="utf-8") == "old-token\n"
    assert (
        installer.install_credentials("new-token", config_root=tmp_path, clobber=True)
        == "wrote (chmod 600)"
    )
    assert target.read_text(encoding="utf-8") == "new-token\n"


def test_stable_device_id_survives_a_reinstall(tmp_path):
    first = installer.stable_device_id(tmp_path)
    second = installer.stable_device_id(tmp_path)
    assert first == second
    assert first.startswith("install-")
    assert stat.S_IMODE((tmp_path / ".device-id").stat().st_mode) == 0o600


def test_stable_device_id_can_preview_without_writing(tmp_path):
    value = installer.stable_device_id(tmp_path, write=False)
    assert value.startswith("install-")
    assert not (tmp_path / ".device-id").exists()
    # The previewed id is never persisted, so it is not reused later.
    assert installer.stable_device_id(tmp_path, write=False) != value


def test_render_config_is_loadable(tmp_path):
    text = installer.render_config(
        "http://127.0.0.1:8038", "seat-1", "install-abc", "工位", attention_enabled=True
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    config = load_config(path)
    assert config.node_id == "seat-1"
    assert config.device_id == "install-abc"
    assert config.desktop.backend == "niri"
    assert config.desktop.attention_enabled is True


def test_check_python_reports_the_floor(monkeypatch):
    monkeypatch.setattr("virtual_life_linux.installer.sys.version_info", (3, 11, 0))
    with pytest.raises(installer.InstallError, match="Python 3.12"):
        installer.check_python()
