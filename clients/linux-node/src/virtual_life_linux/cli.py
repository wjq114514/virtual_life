"""`vl` command line front end. It never holds the server token itself."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

from . import __version__
from .config import ConfigError, LinuxConfig, default_runtime_dir, load_config
from .ipc import IpcClient, IpcClientError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_OFFLINE = 4
EXIT_CONFLICT = 5
EXIT_UNKNOWN = 6
EXIT_UNSUPPORTED = 7

# The one config this process talks to. The socket is server-key scoped, so a
# config path must be resolved before the socket path can be known.
_CONFIG_PATH: Path | None = None
_CONFIGS: dict[str, LinuxConfig] = {}


def active_config_path(path: Path | None) -> None:
    global _CONFIG_PATH
    _CONFIG_PATH = path


def resolve_config(path: Path | None = None) -> LinuxConfig:
    effective = path if path is not None else _CONFIG_PATH
    key = str(effective) if effective is not None else "<default>"
    cached = _CONFIGS.get(key)
    if cached is None:
        cached = load_config(effective)
        _CONFIGS[key] = cached
    return cached


def resolve_runtime_dir(path: Path | None = None) -> Path:
    """Runtime dir from the config, falling back to the unscoped legacy dir."""
    try:
        return resolve_config(path).runtime_dir
    except ConfigError:
        return default_runtime_dir()


_CODE_EXIT = {
    "unauthorized": EXIT_AUTH,
    "auth_failed": EXIT_AUTH,
    "offline": EXIT_OFFLINE,
    "presentation_busy": EXIT_CONFLICT,
    "renderer_required": EXIT_CONFLICT,
    "renderer_not_attached": EXIT_CONFLICT,
    "renderer_replaced": EXIT_CONFLICT,
    "lease_expired": EXIT_CONFLICT,
    "dialogue_context_changed": EXIT_CONFLICT,
    "companion_elsewhere": EXIT_CONFLICT,
    "presentation_busy_conflict": EXIT_CONFLICT,
    "result_unknown": EXIT_UNKNOWN,
    "native_presentation_unsupported": EXIT_UNSUPPORTED,
    "feature_unsupported": EXIT_UNSUPPORTED,
    "voice_disabled": EXIT_UNSUPPORTED,
    "voice_model_missing": EXIT_UNSUPPORTED,
    "voice_unavailable": EXIT_UNSUPPORTED,
    "voice_session_busy": EXIT_CONFLICT,
    "voice_session_required": EXIT_CONFLICT,
    "bad_voice_mode": EXIT_USAGE,
    "bad_voice_text": EXIT_USAGE,
}


def _exit_for(code: str) -> int:
    return _CODE_EXIT.get(code, EXIT_FAILURE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vl", description="Virtual Life Linux host client")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show aggregated daemon state")
    sub.add_parser("open", help="open the Web UI for this node")
    sub.add_parser("tui", help="open the terminal renderer")
    chat = sub.add_parser("chat", help="open the terminal renderer")
    chat.add_argument("companion", nargs="?", default=None)

    doctor = sub.add_parser("doctor", help="report diagnostics")
    doctor.add_argument("--json", action="store_true")

    setup = sub.add_parser(
        "setup",
        help="fetch the latest client from the server, then install config, assets and the service",
    )
    setup.add_argument(
        "--server", default=None, help="runtime URL, also used as the download source"
    )
    setup.add_argument(
        "--dist-url",
        default=None,
        help="download source (defaults to --server); file://, or tokenless HTTP",
    )
    setup.add_argument("--version", default=None, help="package version, or 'latest'")
    setup.add_argument("--wheel", type=Path, default=None, help="install this local wheel instead")
    setup.add_argument("--token", default=None, help="runtime bearer token")
    setup.add_argument("--token-file", type=Path, default=None, help="read the token from a file")
    setup.add_argument("--node-id", default=None)
    setup.add_argument("--device-id", default=None)
    setup.add_argument("--label", default=None)
    setup.add_argument("--backend", choices=["niri", "none"], default=None)
    setup.add_argument(
        "--attention",
        action="store_true",
        help="enable attention observation in the written config",
    )
    setup.add_argument("--no-systemd", action="store_true")
    setup.add_argument(
        "--clobber",
        action="store_true",
        help="overwrite an existing config, credentials or desktop file",
    )
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--json", action="store_true", help="print the result as JSON")

    renderer = sub.add_parser("renderer", help="renderer lifecycle")
    renderer_sub = renderer.add_subparsers(dest="action", required=True)
    renderer_sub.add_parser("status")
    attach = renderer_sub.add_parser("attach")
    attach.add_argument("--companion", default=None)
    detach = renderer_sub.add_parser("detach")
    detach.add_argument("--renderer-id", default=None)

    connect = sub.add_parser("connect", help="connect a companion")
    connect.add_argument("companion", nargs="?", default=None)
    sub.add_parser("disconnect", help="release the current presentation")
    companion = sub.add_parser("companion", help="list and select companions")
    companion_sub = companion.add_subparsers(dest="action", required=True)
    companion_sub.add_parser("list", help="list companions and the current selection")
    select = companion_sub.add_parser("select", help="select without connecting")
    select.add_argument("companion_id")
    mode = sub.add_parser("mode", help="set interaction mode")
    mode.add_argument("value", choices=["quiet", "chat", "focus", "away"])
    sub.add_parser("break", help="signal a break boundary")
    task = sub.add_parser("task", help="task controls")
    task_sub = task.add_subparsers(dest="action", required=True)
    task_sub.add_parser("done")

    ask = sub.add_parser("ask", help="send one message")
    ask.add_argument("text", nargs="*", help="message text; read from stdin when omitted")
    ask.add_argument("--stdin", action="store_true", help="read the message from stdin")

    travel = sub.add_parser("travel", help="prepare a handoff")
    travel.add_argument("target_node_id")

    sub.add_parser("web", help="open the Web UI")

    attention = sub.add_parser("attention", help="attention overrides")
    attention_sub = attention.add_subparsers(dest="action", required=True)
    override = attention_sub.add_parser("override")
    override.add_argument("state", choices=["focus", "away", "break"])
    override.add_argument("--seconds", type=int, default=900)
    attention_sub.add_parser("clear")

    shell = sub.add_parser("shell", help="report shell activity")
    shell_sub = shell.add_subparsers(dest="action", required=True)
    shell_sub.add_parser("register")
    shell_sub.add_parser("started")
    shell_sub.add_parser("finished")
    shell_sub.add_parser("closed")

    screen = sub.add_parser("screen", help="screen vision capture session")
    screen_sub = screen.add_subparsers(dest="action", required=True)
    screen_start = screen_sub.add_parser("start")
    screen_start.add_argument("--companion", default=None)
    screen_start.add_argument("--profile", choices=["economy", "balanced", "detail"], default=None)
    screen_start.add_argument("--mode", choices=["continuous", "one_shot"], default=None)
    screen_sub.add_parser("stop")
    screen_sub.add_parser("status")

    voice = sub.add_parser("voice", help="continuous voice session controls")
    voice_sub = voice.add_subparsers(dest="action", required=True)
    voice_doctor = voice_sub.add_parser("doctor", help="report voice readiness")
    voice_doctor.add_argument("--json", action="store_true")
    voice_sub.add_parser("devices", help="list capture / playback devices")
    voice_start = voice_sub.add_parser("start", help="start a voice session")
    voice_start.add_argument(
        "--mode",
        choices=["continuous", "push_to_talk", "text"],
        default="continuous",
    )
    voice_sub.add_parser("stop", help="stop the voice session")
    voice_sub.add_parser("status", help="show voice session state")
    voice_commit = voice_sub.add_parser("commit", help="commit a typed turn")
    voice_commit.add_argument("text", nargs="?", default=None)
    voice_commit.add_argument("--stdin", action="store_true", help="read the text from stdin")
    voice_commit.add_argument("--release", action="store_true", help="release a push-to-talk turn")
    voice_sub.add_parser("interrupt", help="stop the current reply playback")

    daemon = sub.add_parser("daemon", help="run the host daemon")
    daemon_sub = daemon.add_subparsers(dest="action", required=True)
    daemon_sub.add_parser("run")

    sub.add_parser("version")
    return parser


async def _client_call(method: str, params: dict | None = None) -> dict:
    client = IpcClient(resolve_runtime_dir() / "daemon.sock")
    try:
        return await client.call(method, params)
    finally:
        await client.close()


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def _dispatch(args: argparse.Namespace) -> int:
    command = args.command
    if command == "version":
        print(__version__)
        return EXIT_OK
    if command == "daemon":
        return await _run_daemon(args)
    if command == "doctor":
        return await _run_doctor(args)

    if command == "status":
        _print(await _client_call("status.get"))
        return EXIT_OK
    if command == "open" or command == "web":
        _print(await _client_call("webui.open"))
        return EXIT_OK
    if command == "renderer":
        if args.action == "attach":
            _print(await _client_call("renderer.attach", {"companion_id": args.companion}))
        elif args.action == "detach":
            _print(await _client_call("renderer.detach", {"renderer_id": args.renderer_id}))
        else:
            _print((await _client_call("status.get")).get("renderer"))
        return EXIT_OK
    if command == "connect":
        _print(await _client_call("companion.connect", {"companion_id": args.companion}))
        return EXIT_OK
    if command == "companion":
        if args.action == "select":
            _print(await _client_call("companion.select", {"companion_id": args.companion_id}))
        else:
            _print(await _client_call("companion.list"))
        return EXIT_OK
    if command == "disconnect":
        _print(await _client_call("renderer.detach", {}))
        return EXIT_OK
    if command == "mode":
        _print(await _client_call("companion.mode.set", {"mode": args.value}))
        return EXIT_OK
    if command == "break":
        _print(await _client_call("attention.boundary", {"boundary": "break_started"}))
        return EXIT_OK
    if command == "task":
        _print(await _client_call("attention.boundary", {"boundary": "task_completed"}))
        return EXIT_OK
    if command == "ask":
        if args.stdin or not args.text:
            text = sys.stdin.read().strip()
        else:
            text = " ".join(args.text).strip()
        if not text:
            print("error: message is empty", file=sys.stderr)
            return EXIT_USAGE
        _print(await _client_call("dialogue.send", {"text": text}))
        return EXIT_OK
    if command == "travel":
        _print(await _client_call("travel.prepare", {"target_node_id": args.target_node_id}))
        return EXIT_OK
    if command == "shell":
        params: dict = {}
        if args.action == "register":
            params["session_id"] = os.environ.get("VIRTUAL_LIFE_SESSION_ID", "")
        _print(await _client_call(f"shell.{args.action}", params))
        return EXIT_OK
    if command == "screen":
        if args.action == "start":
            params: dict = {}
            if args.companion:
                params["companion_id"] = args.companion
            if args.profile:
                params["profile"] = args.profile
            if args.mode:
                params["mode"] = args.mode
            _print(await _client_call("screen.start", params))
        elif args.action == "stop":
            _print(await _client_call("screen.stop", {}))
        else:
            _print(await _client_call("screen.status", {}))
        return EXIT_OK
    if command == "voice":
        if args.action == "doctor":
            _print(await _client_call("voice.doctor"))
        elif args.action == "devices":
            _print(await _client_call("voice.devices"))
        elif args.action == "start":
            _print(await _client_call("voice.start", {"mode": args.mode}))
        elif args.action == "stop":
            _print(await _client_call("voice.stop"))
        elif args.action == "status":
            _print(await _client_call("voice.status"))
        elif args.action == "commit":
            if args.release:
                _print(await _client_call("voice.commit", {}))
            else:
                text = sys.stdin.read() if args.stdin or args.text is None else args.text
                _print(await _client_call("voice.commit", {"text": text}))
        else:
            _print(await _client_call("voice.interrupt"))
        return EXIT_OK
    if command == "attention":
        if args.action == "override":
            _print(
                await _client_call(
                    "attention.override.set", {"state": args.state, "seconds": args.seconds}
                )
            )
        else:
            _print(await _client_call("attention.override.clear"))
        return EXIT_OK
    raise SystemExit(EXIT_USAGE)


async def _run_daemon(args: argparse.Namespace) -> int:
    from .daemon import LinuxDaemon
    from .storage import ClientStorage
    from .transport import ServerTransport

    config = load_config(args.config)
    token = config.load_token()
    storage = ClientStorage(config.state_dir / "state.sqlite")
    transport = ServerTransport(config.normalized_server_url, token)
    daemon = LinuxDaemon(config, storage, transport, runtime_dir=config.runtime_dir)
    try:
        await daemon.run_forever()
    finally:
        storage.close()
    return EXIT_OK


async def _run_doctor(args: argparse.Namespace) -> int:
    report: dict = {"version": __version__}
    config: LinuxConfig | None = None
    try:
        config = resolve_config(args.config)
        report["runtime_dir"] = str(config.runtime_dir)
        report["config"] = {
            "path": str(config.config_path),
            "server_url": config.normalized_server_url,
            "server_key": config.server_key,
            "node_id": config.node_id,
            "desktop_backend": config.desktop.backend,
        }
        token_ok = True
        try:
            config.load_token()
        except ConfigError as exc:
            token_ok = False
            report["credential_error"] = str(exc)
        report["credential_ok"] = token_ok
    except ConfigError as exc:
        report["config_error"] = str(exc)
        report["runtime_dir"] = str(resolve_runtime_dir())
    if config is not None:
        try:
            report["daemon"] = await _client_call("status.get")
        except IpcClientError as exc:
            report["daemon_error"] = str(exc)
        report["server"] = await _probe_server(config)
    if args.json:
        _print(report)
    else:
        _print(_doctor_summary(report))
    return EXIT_OK if not report.get("config_error") else EXIT_USAGE


async def _probe_server(config: LinuxConfig) -> dict:
    """Short reachability check; never used for a long-running client."""
    from .transport import ServerTransport

    try:
        token = config.load_token()
    except ConfigError as exc:
        return {"reachable": False, "error": str(exc)}
    transport = ServerTransport(config.normalized_server_url, token)
    try:
        capabilities = await asyncio.wait_for(transport.capabilities(), timeout=6.0)
        return {
            "reachable": True,
            "protocol_version": capabilities.get("protocol_version"),
            "native_presentation": bool(capabilities.get("native_presentation")),
            "streaming": bool(capabilities.get("streaming")),
        }
    except Exception as exc:  # noqa: BLE001 - the probe must report, not raise
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with contextlib.suppress(Exception):
            await transport.aclose()


def _doctor_summary(report: dict) -> dict:
    """Compact form for humans; `--json` keeps the full structure."""
    config = report.get("config") or {}
    server = report.get("server") or {}
    daemon = report.get("daemon") or {}
    renderer = daemon.get("renderer") if isinstance(daemon.get("renderer"), dict) else None
    summary: dict = {
        "version": report.get("version"),
        "runtime_dir": report.get("runtime_dir"),
        "config_ok": "config_error" not in report,
        "server_url": config.get("server_url"),
        "node_id": config.get("node_id"),
        "credential_ok": report.get("credential_ok"),
        "server_reachable": server.get("reachable"),
        "native_presentation": server.get("native_presentation"),
        "daemon_running": bool(daemon),
        "node_online": daemon.get("online"),
        "capabilities": (daemon.get("node") or {}).get("capabilities"),
        "presentation_id": daemon.get("presentation_id"),
        "companion_id": daemon.get("companion_id"),
        "renderer_generation": (renderer or {}).get("generation"),
        "attention": daemon.get("attention"),
        "voice": daemon.get("voice"),
        "last_error": daemon.get("last_error"),
    }
    if report.get("credential_error"):
        summary["credential_error"] = report["credential_error"]
    if report.get("config_error"):
        summary["config_error"] = report["config_error"]
    if report.get("daemon_error"):
        summary["daemon_error"] = report["daemon_error"]
    if not server.get("reachable") and server.get("error"):
        summary["server_error"] = server["error"]
    return summary


def _setup_token(args: argparse.Namespace, config_dir: Path) -> str | None:
    """Resolve the bearer token without ever printing it."""
    if args.token:
        return args.token.strip()
    if args.token_file:
        try:
            value = args.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise installer_error(f"Cannot read --token-file {args.token_file}: {exc}")
        if value:
            return value
    if os.environ.get("VIRTUAL_LIFE_TOKEN"):
        return os.environ["VIRTUAL_LIFE_TOKEN"].strip()
    existing = config_dir / "credentials"
    if existing.is_file():
        value = existing.read_text(encoding="utf-8").strip()
        if value and value != "replace-me":
            return value
    if sys.stdin.isatty():
        value = getpass.getpass("Runtime bearer token: ").strip()
        if value:
            return value
    return None


def _setup_prompt(question: str, default: str | None = None) -> str | None:
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else ""
    value = input(f"{question}{suffix}: ").strip()
    return value or default


def _existing_server_url(config_dir: Path) -> str | None:
    config_path = config_dir / "config.toml"
    if not config_path.is_file():
        return None
    try:
        return load_config(config_path).normalized_server_url
    except ConfigError:
        return None


def installer_error(message: str) -> Exception:
    from .installer import InstallError

    return InstallError(message)


def _run_setup(args: argparse.Namespace) -> int:
    """Fetch a published client from the server, then write the runtime files."""
    from . import dist_client, installer

    installer.check_python()
    config_dir = installer.runtime_config_dir()
    notes: list[str] = []
    report: dict = {"steps": notes, "config_dir": str(config_dir), "dry_run": args.dry_run}

    token = _setup_token(args, config_dir)
    source = args.dist_url or args.server
    wheel = args.wheel

    if wheel is None and source:
        manifest = asyncio.run(dist_client.fetch_manifest(source, token))
        package = manifest.select(args.version)
        report["packages"] = [item.version for item in manifest.packages]
        report["version"] = package.version
        notes.append(f"selected: {package.version} ({package.wheel})")
        destination = Path(tempfile.mkdtemp(prefix="virtual-life-setup-")) / package.wheel
        asyncio.run(dist_client.download_package(source, package, destination, token))
        wheel = destination
    elif wheel is not None:
        notes.append(f"using local wheel: {wheel}")
    else:
        notes.append("no download source given; keeping the installed package")

    if wheel is not None:
        notes.append(installer.install_python_package(wheel, dry_run=args.dry_run))

    if config_dir.joinpath("config.toml").exists() and not args.clobber:
        notes.append("config: kept (existing)")
        notes.append("credentials: kept (existing)")
        report["config_written"] = False
    else:
        server_url = args.server or _setup_prompt("Runtime URL", _existing_server_url(config_dir))
        if not server_url:
            raise installer_error("--server is required to write a new config.toml")
        node_id = args.node_id or _setup_prompt("Node id", socket.gethostname() or "linux-seat")
        if not node_id:
            raise installer_error("--node-id is required when stdin is not a terminal")
        label = args.label or node_id
        device_id = args.device_id or installer.stable_device_id(config_dir)
        backend = args.backend or "niri"
        text = installer.render_config(
            server_url, node_id, device_id, label, backend=backend, attention_enabled=args.attention
        )
        notes.append(
            f"config: "
            f"{installer.install_config(text, dry_run=args.dry_run, clobber=args.clobber, config_root=config_dir)}"
        )
        report["config_written"] = True
        if token:
            notes.append(
                f"credentials: "
                f"{installer.install_credentials(token, dry_run=args.dry_run, clobber=args.clobber, config_root=config_dir)}"
            )
            report["credentials_written"] = True
        else:
            notes.append("credentials: skipped (no token provided)")

    notes.extend(
        installer.install_assets(
            dry_run=args.dry_run, with_systemd=not args.no_systemd, clobber=args.clobber
        )
    )
    if not args.no_systemd:
        notes.append(f"systemd: {installer.enable_systemd(dry_run=args.dry_run)}")

    if args.json:
        report["steps"] = notes
        _print(report)
    else:
        for line in notes:
            print(line)
        if not args.dry_run:
            print("\nNext: run `vl doctor`, then `vl chat`.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "config", None) is None and os.environ.get("VIRTUAL_LIFE_CONFIG"):
        args.config = Path(os.environ["VIRTUAL_LIFE_CONFIG"])
    active_config_path(args.config)
    if args.command in ("tui", "chat"):
        # The terminal renderer owns its own event loop, so it cannot be started
        # from inside asyncio.run().
        from .tui.app import run as run_tui

        try:
            run_tui(args.config, getattr(args, "companion", None))
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK
    if args.command == "setup":
        # Interactive prompts and subprocess installs: keep this out of the daemon
        # event loop too.
        from .dist_client import DistError
        from .installer import InstallError

        try:
            return _run_setup(args)
        except (DistError, InstallError) as exc:
            print(f"setup error: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return EXIT_USAGE
    try:
        return asyncio.run(_dispatch(args))
    except IpcClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = str(exc).split(":", 1)[0].strip()
        return _exit_for(code)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
