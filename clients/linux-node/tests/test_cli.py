from pathlib import Path

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


def test_setup_from_github_removes_the_downloaded_wheel(tmp_path, monkeypatch):
    """The wheel is consumed by the installer, so it must never linger in /tmp."""
    wheel = "virtual_life_linux-0.2.0-py3-none-any.whl"

    async def manifest(repo, token, *, api_base):
        return cli_module_manifest()

    async def download(server_url, package, destination, auth):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"wheel-bytes")
        assert server_url == ""  # the release supplies its own download URL
        return destination

    def install(source, *, dry_run):
        assert source.name == wheel
        assert source.read_bytes() == b"wheel-bytes"
        return "installed via: fake"

    monkeypatch.setattr("virtual_life_linux.dist_client.github_manifest", manifest)
    monkeypatch.setattr("virtual_life_linux.dist_client.download_package", download)
    monkeypatch.setattr("virtual_life_linux.installer.install_python_package", install)
    monkeypatch.setattr("virtual_life_linux.installer.runtime_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    args = cli.build_parser().parse_args(
        [
            "setup",
            "--github",
            "wjq114514/virtual_life",
            "--github-api",
            "http://127.0.0.1:9",
            "--server",
            "http://127.0.0.1:8038",
            "--node-id",
            "seat",
            "--version",
            "0.2.0",
            "--dry-run",
            "--no-systemd",
            "--backend",
            "none",
        ]
    )
    assert cli._run_setup(args) == cli.EXIT_OK
    assert [path for path in Path("/tmp").glob("virtual-life-setup-*")] == []


def cli_module_manifest():
    from virtual_life_linux.dist_client import DistManifest, DistPackage

    return DistManifest(
        default_version="0.2.0",
        packages=(
            DistPackage(
                version="0.2.0",
                wheel="virtual_life_linux-0.2.0-py3-none-any.whl",
                sha256="a" * 64,
                download_url="http://127.0.0.1:9/assets/v0.2.0/wheel",
            ),
        ),
    )
