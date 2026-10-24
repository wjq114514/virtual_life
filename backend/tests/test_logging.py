"""Unified logging configuration, rotation fields and request correlation."""

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from virtual_life.log import RequestLogMiddleware, configure_logging, reset_logging
from virtual_life.settings import Settings


def _settings(tmp_path, **overrides):
    values = {
        "database_path": str(tmp_path / "runtime.sqlite3"),
        "api_token": "test-token",
    }
    values.update(overrides)
    return Settings(**values)


def test_logging_section_paths_and_environment_precedence(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("VIRTUAL_LIFE_"):
            monkeypatch.delenv(key)
    path = tmp_path / "settings.toml"
    path.write_text(
        """
[logging]
level = "debug"
console = false
file_enabled = true
file_path = "logs/app.log"
max_mb = 5
backup_count = 2
access = false
""",
        encoding="utf8",
    )
    settings = Settings.load(path)
    assert settings.log_level == "DEBUG"
    assert settings.log_console is False
    assert settings.log_file_enabled is True
    assert Path(settings.log_file_path) == path.parent / "logs/app.log"
    assert (settings.log_file_max_mb, settings.log_file_backup_count) == (5, 2)
    assert settings.log_access is False

    monkeypatch.setenv("VIRTUAL_LIFE_LOGGING_LEVEL", "warning")
    monkeypatch.setenv("VIRTUAL_LIFE_LOGGING_FILE_PATH", str(tmp_path / "override.log"))
    overridden = Settings.load(path)
    assert overridden.log_level == "WARNING"
    assert overridden.log_file_path == str(tmp_path / "override.log")


def test_log_level_is_normalized(tmp_path):
    assert _settings(tmp_path, log_level=" info ").log_level == "INFO"


@pytest.mark.parametrize(
    "overrides",
    [
        {"log_level": "TRACE"},
        {"log_console": "yes"},
        {"log_file_path": ""},
        {"log_file_max_mb": 0},
        {"log_file_max_mb": 1025},
        {"log_file_backup_count": 0},
        {"log_file_backup_count": 101},
    ],
)
def test_logging_settings_validation(tmp_path, overrides):
    with pytest.raises(ValueError):
        _settings(tmp_path, **overrides)


def test_console_and_file_output_are_idempotent(tmp_path, capsys):
    log_file = tmp_path / "app.log"
    settings = _settings(
        tmp_path,
        log_level="DEBUG",
        log_console=True,
        log_file_enabled=True,
        log_file_path=str(log_file),
        log_file_max_mb=1,
        log_file_backup_count=2,
    )
    logger = logging.getLogger("virtual_life.test_logging")
    try:
        configure_logging(settings, color=False)
        configure_logging(settings, color=False)
        logger.debug("hello-idempotent")
        logger.warning("warn-line")
        for handler in logging.getLogger().handlers:
            handler.flush()
        console = capsys.readouterr().out
        assert console.count("hello-idempotent") == 1
        assert "[WARN ]" in console and "[test_logging:" in console
        content = log_file.read_text(encoding="utf8")
        assert content.count("hello-idempotent") == 1
        assert "[WARN ]" in content and "\x1b[" not in content
    finally:
        reset_logging()


def test_request_id_header_and_access_record(caplog):
    caplog.set_level(logging.INFO)
    app = FastAPI()

    @app.get("/hello")
    def hello():
        return {"ok": True}

    app.add_middleware(RequestLogMiddleware, access=True)
    with TestClient(app) as client:
        response = client.get("/hello", headers={"X-Request-ID": "callback-123"})
        generated = client.get("/hello", headers={"X-Request-ID": "bad id\n"})

    assert response.headers["x-request-id"] == "callback-123"
    assert len(generated.headers["x-request-id"]) == 12
    records = [record for record in caplog.records if "-> 200" in record.getMessage()]
    assert [record.request_id for record in records] == [
        "callback-123",
        generated.headers["x-request-id"],
    ]


def test_static_and_health_paths_are_debug_only(caplog):
    caplog.set_level(logging.INFO)
    app = FastAPI()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/ui/index.html")
    def static():
        return "page"

    app.add_middleware(RequestLogMiddleware, access=True)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/ui/index.html").status_code == 200
    assert not [record for record in caplog.records if record.name == "virtual_life.http"]
