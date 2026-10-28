#!/usr/bin/env python3
"""Build the Linux client wheel/sdist and print artifact hashes.

Used by the plan's verification command:
    uv run --project clients/linux-node python clients/linux-node/packaging/build.py

The wheel is checked to contain the desktop integration assets and the example
config, otherwise the installer would silently skip them.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

REQUIRED_WHEEL_ENTRIES = {
    "virtual_life_linux/__init__.py",
    "virtual_life_linux/cli.py",
    "virtual_life_linux/daemon.py",
    "virtual_life_linux/dist_client.py",
    "virtual_life_linux/installer.py",
    "virtual_life_linux/integrations/systemd/virtual-life-linux.service",
    "virtual_life_linux/integrations/fish/conf.d/virtual-life.fish",
    "virtual_life_linux/integrations/fish/completions/vl.fish",
    "virtual_life_linux/integrations/niri/virtual-life.kdl",
    "virtual_life_linux/integrations/desktop/dev.virtuallife.Linux.desktop",
    "virtual_life_linux/integrations/dms/VirtualLife/manifest.json",
    "virtual_life_linux/assets/config.example.toml",
}


def _run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def build() -> list[Path]:
    if DIST.exists():
        shutil.rmtree(DIST)
    uv = shutil.which("uv")
    if uv:
        _run([uv, "build", "--out-dir", str(DIST)])
    else:
        _run([sys.executable, "-m", "build", "--outdir", str(DIST)])
    return sorted(DIST.iterdir())


def missing_entries(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        names = {name for name in archive.namelist() if not name.endswith("/")}
    return REQUIRED_WHEEL_ENTRIES - names


def write_checksums(artifacts: list[Path]) -> Path:
    """Write SHA256SUMS so a downloaded wheel can be verified before install."""
    lines = []
    for artifact in sorted(artifacts, key=lambda path: path.name):
        if artifact.name.startswith(".") or artifact.is_dir():
            continue
        if artifact.name == "SHA256SUMS":
            continue
        lines.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
    path = DIST / "SHA256SUMS"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    artifacts = build()
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    if not wheels:
        print("error: no wheel was produced", file=sys.stderr)
        return 1
    for wheel in wheels:
        missing = missing_entries(wheel)
        if missing:
            print(f"error: wheel {wheel.name} is missing bundled assets:", file=sys.stderr)
            for entry in sorted(missing):
                print(f"  {entry}", file=sys.stderr)
            return 1
    checksums = write_checksums(artifacts)
    for artifact in artifacts:
        if artifact.name.startswith(".") or artifact.is_dir():
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        print(f"{artifact.name}  sha256={digest}")
    print(
        f"wrote {checksums} "
        f"({len(checksums.read_text(encoding='utf-8').splitlines())} entries); "
        "upload it next to the wheel for GitHub releases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
