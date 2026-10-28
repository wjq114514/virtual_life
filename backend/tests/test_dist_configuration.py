"""Strict parsing for the client distribution channel configuration."""

import pytest
from virtual_life.settings import Settings


def _clear_environment(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("VIRTUAL_LIFE_"):
            monkeypatch.delenv(key)


def test_distribution_configuration_round_trip(tmp_path, monkeypatch):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text(
        """
[dist]
directory = ".data/dist"
upload_secret = "publish-secret"
max_bytes = 1048576
max_packages = 3
""",
        encoding="utf8",
    )
    settings = Settings.load(path)
    assert settings.dist_upload_secret == "publish-secret"
    assert settings.dist_max_bytes == 1048576
    assert settings.dist_max_packages == 3
    # The file-relative path is anchored to the configuration file.
    assert settings.dist_directory == str((tmp_path / ".data/dist").resolve())


def test_distribution_environment_overrides_use_the_documented_names(tmp_path, monkeypatch):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text('[dist]\ndirectory = "from-file"\nmax_bytes = 16\n', encoding="utf8")
    monkeypatch.setenv("VIRTUAL_LIFE_DIST_DIRECTORY", "/var/lib/virtual-life/dist")
    monkeypatch.setenv("VIRTUAL_LIFE_DIST_MAX_BYTES", "512")
    monkeypatch.setenv("VIRTUAL_LIFE_DIST_UPLOAD_SECRET", "env-secret")
    settings = Settings.load(path)
    # Environment paths keep their historical working-directory semantics.
    assert settings.dist_directory == "/var/lib/virtual-life/dist"
    assert settings.dist_max_bytes == 512
    assert settings.dist_upload_secret == "env-secret"


def test_absolute_distribution_directory_is_kept(tmp_path, monkeypatch):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text('[dist]\ndirectory = "/srv/virtual-life/dist"\n', encoding="utf8")
    assert Settings.load(path).dist_directory == "/srv/virtual-life/dist"


@pytest.mark.parametrize(
    "payload",
    [
        "[dist]\nbogus = 1\n",
        '[dist]\ndirectory = ""\n',
        "[dist]\nmax_bytes = 0\n",
        "[dist]\nmax_bytes = 2147483649\n",
        "[dist]\nmax_packages = 0\n",
        "[dist]\nmax_packages = 1001\n",
        "[dist]\nmax_bytes = true\n",
    ],
)
def test_invalid_distribution_configuration_is_rejected(tmp_path, monkeypatch, payload):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text(payload, encoding="utf8")
    with pytest.raises(ValueError):
        Settings.load(path)


def test_distribution_defaults_are_a_safe_empty_channel(tmp_path, monkeypatch):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text("", encoding="utf8")
    settings = Settings.load(path)
    assert settings.dist_directory == ".data/dist"
    assert settings.dist_upload_secret == ""
    assert settings.dist_max_bytes == 67_108_864
    assert settings.dist_max_packages == 25
