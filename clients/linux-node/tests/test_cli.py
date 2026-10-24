from virtual_life_linux import cli

CONFIG = """
schema_version = 1
server_url = "http://127.0.0.1:8038"
node_id = "node-a"
device_id = "device-a"
label = "WS"
"""


def test_build_parser_handles_subcommands():
    parser = cli.build_parser()
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["doctor", "--json"]).json is True
    assert parser.parse_args(["ask", "你好"]).text == ["你好"]
    assert parser.parse_args(["chat", "companion"]).companion == "companion"
    assert parser.parse_args(["mode", "focus"]).value == "focus"
    assert parser.parse_args(["companion", "list"]).action == "list"
    assert parser.parse_args(["companion", "select", "c-1"]).companion_id == "c-1"


def test_doctor_summary_reports_health_and_errors():
    summary = cli._doctor_summary(
        {
            "version": "0.1.0",
            "runtime_dir": "/run/user/1000/virtual-life/abc/node-a",
            "config": {"server_url": "http://h:1", "node_id": "node-a"},
            "server": {"reachable": False, "error": "OfflineError: offline"},
            "daemon_error": "connection refused",
        }
    )
    assert summary["runtime_dir"] == "/run/user/1000/virtual-life/abc/node-a"
    assert summary["config_ok"] is True
    assert summary["server_reachable"] is False
    assert summary["server_error"].startswith("OfflineError")
    assert summary["daemon_error"] == "connection refused"
    assert summary["capabilities"] is None


def test_doctor_summary_reports_config_error():
    summary = cli._doctor_summary({"version": "0.1.0", "config_error": "bad field"})
    assert summary["config_ok"] is False
    assert summary["config_error"] == "bad field"


def test_main_dispatches_tui_without_a_running_loop(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG, encoding="utf-8")
    calls: list[tuple] = []

    def fake_run(config_path, companion):
        calls.append((str(config_path), companion))

    monkeypatch.setattr("virtual_life_linux.tui.app.run", fake_run)
    assert cli.main(["--config", str(config), "chat", "companion"]) == cli.EXIT_OK
    assert calls == [(str(config), "companion")]


def test_exit_code_mapping():
    assert cli._exit_for("presentation_busy") == cli.EXIT_CONFLICT
    assert cli._exit_for("native_presentation_unsupported") == cli.EXIT_UNSUPPORTED
    assert cli._exit_for("offline") == cli.EXIT_OFFLINE
    assert cli._exit_for("unauthorized") == cli.EXIT_AUTH
    assert cli._exit_for("unknown_code") == cli.EXIT_FAILURE


def test_resolve_config_caches_by_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_CONFIG_PATH", None)
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    cli.active_config_path(path)
    first = cli.resolve_config()
    assert cli.resolve_config() is first
    assert cli.resolve_config(path) is first
    assert cli.resolve_runtime_dir().name == "node-a"


def test_resolve_runtime_dir_falls_back_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert cli.resolve_runtime_dir(tmp_path / "missing.toml") == tmp_path / "run" / "virtual-life"
