"""Per-user installation of the desktop integration assets and runtime files.

Shared by ``packaging/install.py`` (run from a checkout) and ``vl setup`` (run
from an installed wheel). Assets are resolved from the source tree first and from
the installed package data second, so this module works with no checkout present.

Nothing here contacts the server and nothing is ever overwritten without being
asked to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ASSET_PACKAGE = "virtual_life_linux"
EXAMPLE_CONFIG = "config.example.toml"
SERVICE_NAME = "virtual-life-linux.service"
CREDS_MODE = 0o600

# Asset relative path -> (user base kind, destination relative to that base).
ASSET_DESTINATIONS = {
    "systemd/virtual-life-linux.service": ("config", "systemd/user/virtual-life-linux.service"),
    "fish/conf.d/virtual-life.fish": ("config", "fish/conf.d/virtual-life.fish"),
    "fish/completions/vl.fish": ("config", "fish/completions/vl.fish"),
    "niri/virtual-life.kdl": ("config", "niri/virtual-life.kdl"),
    "desktop/dev.virtuallife.Linux.desktop": ("data", "applications/dev.virtuallife.Linux.desktop"),
}


class InstallError(RuntimeError):
    """Raised when an installation step cannot be completed."""


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def runtime_config_dir() -> Path:
    return config_home() / "virtual-life" / "linux"


def stable_device_id(config_dir: Path, *, write: bool = True) -> str:
    """Return the installation id, persisting it so a reinstall keeps the identity."""
    marker = config_dir / ".device-id"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"install-{uuid.uuid4().hex[:12]}"
    if not write:
        return value
    config_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(value + "\n", encoding="utf-8")
    marker.chmod(CREDS_MODE)
    return value


def check_python() -> None:
    if sys.version_info < (3, 12):
        raise InstallError(f"Python 3.12+ is required, found {sys.version.split()[0]}")


def source_root() -> Path | None:
    """The clients/linux-node checkout, when this module runs from source."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "integrations").is_dir() and (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def _package_data(relative: str) -> Path | None:
    try:
        from importlib import resources

        target = resources.files(ASSET_PACKAGE).joinpath(relative)
    except (ModuleNotFoundError, TypeError, ValueError, OSError):
        return None
    candidate = Path(target)
    return candidate if candidate.exists() else None


def asset_dir() -> Path | None:
    root = source_root()
    if root is not None and (root / "integrations").is_dir():
        return root / "integrations"
    return _package_data("integrations")


def example_config() -> Path | None:
    root = source_root()
    if root is not None:
        candidate = root / "packaging" / EXAMPLE_CONFIG
        if candidate.is_file():
            return candidate
    return _package_data(f"assets/{EXAMPLE_CONFIG}")


def collect_assets() -> dict[str, bytes]:
    """Read every bundled asset as (relative path, bytes) so it is path-agnostic."""
    root = asset_dir()
    if root is None or not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def render_config(
    server_url: str,
    node_id: str,
    device_id: str,
    label: str,
    *,
    backend: str = "niri",
    attention_enabled: bool = False,
) -> str:
    """Emit a minimal config.toml; every other key keeps its documented default."""
    return (
        f"schema_version = 1\n"
        f'server_url = "{server_url}"\n'
        f'node_id = "{node_id}"\n'
        f'device_id = "{device_id}"\n'
        f'label = "{label}"\n'
        f'credential_file = "credentials"\n'
        f"\n[desktop]\n"
        f'backend = "{backend}"\n'
        f"attention_enabled = {'true' if attention_enabled else 'false'}\n"
    )


def _write(target: Path, payload: bytes, *, dry_run: bool, clobber: bool) -> str:
    if dry_run:
        return "would write"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not clobber:
        return "kept (existing)"
    target.write_bytes(payload)
    return "wrote"


def install_assets(
    *,
    dry_run: bool = False,
    with_systemd: bool = True,
    clobber: bool = False,
    config_root: Path | None = None,
    data_root: Path | None = None,
) -> list[str]:
    """Copy the desktop integration files. Returns one status line per file."""
    base_config = config_root if config_root is not None else config_home()
    base_data = data_root if data_root is not None else data_home()
    notes: list[str] = []
    assets = collect_assets()
    if not assets:
        notes.append("warning: no bundled integration assets found; skipping desktop files")
        return notes
    for name, payload in assets.items():
        if name.startswith("dms/"):
            target = base_data / "virtual-life" / "dms" / name.removeprefix("dms/")
            label = "dms asset"
        elif name in ASSET_DESTINATIONS:
            base_kind, relative = ASSET_DESTINATIONS[name]
            target = (base_config if base_kind == "config" else base_data) / relative
            label = name.split("/")[0]
        else:
            continue
        if name == "systemd/virtual-life-linux.service" and not with_systemd:
            continue
        notes.append(f"{label}: {_write(target, payload, dry_run=dry_run, clobber=clobber)}")
    if "niri/virtual-life.kdl" in assets:
        snippet = base_config / "niri" / "virtual-life.kdl"
        notes.append(f"  append the block from {snippet} to your niri config manually")
    return notes


def install_config(
    text: str,
    *,
    dry_run: bool = False,
    clobber: bool = False,
    config_root: Path | None = None,
) -> str:
    target = (config_root if config_root is not None else runtime_config_dir()) / "config.toml"
    return _write(target, text.encode("utf-8"), dry_run=dry_run, clobber=clobber)


def install_credentials(
    token: str,
    *,
    dry_run: bool = False,
    clobber: bool = False,
    config_root: Path | None = None,
) -> str:
    target = (config_root if config_root is not None else runtime_config_dir()) / "credentials"
    state = _write(target, f"{token.strip()}\n".encode("utf-8"), dry_run=dry_run, clobber=clobber)
    if state == "wrote":
        target.chmod(CREDS_MODE)
        return "wrote (chmod 600)"
    return state


def install_python_package(source: Path, *, dry_run: bool = False) -> str:
    """Install the CLI/daemon from a checkout or a wheel file."""
    uv = shutil.which("uv")
    if uv:
        command = [uv, "tool", "install", "--force", str(source)]
    elif shutil.which("pip") or Path(sys.prefix, "bin", "pip").exists():
        command = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", str(source)]
    else:
        raise InstallError("Neither uv nor pip is available; install Python packaging tools first")
    if dry_run:
        return f"would run: {' '.join(command)}"
    subprocess.run(command, check=True)
    return f"installed via: {' '.join(command)}"


def enable_systemd(*, dry_run: bool = False) -> str:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "warning: systemctl not found; start the daemon manually"
    if dry_run:
        return "would reload and enable the user service"
    environment = {**os.environ, "XDG_CONFIG_HOME": str(config_home())}
    subprocess.run([systemctl, "--user", "daemon-reload"], check=False, env=environment)
    result = subprocess.run(
        [systemctl, "--user", "enable", "--now", SERVICE_NAME], check=False, env=environment
    )
    return "enabled" if result.returncode == 0 else "warning: systemctl enable failed"
