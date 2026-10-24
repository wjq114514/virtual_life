#!/usr/bin/env python3
"""Per-user installer for the Virtual Life Linux host.

Thin shim over :mod:`virtual_life_linux.installer`, which also powers
``vl setup``. Run it from a checkout to install the CLI, the desktop integration
assets, a user systemd unit and a starter config. It never contacts the server
and never overwrites an existing user file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # clients/linux-node
sys.path.insert(0, str(ROOT / "src"))

from virtual_life_linux import installer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Virtual Life Linux host")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-package", action="store_true")
    parser.add_argument("--no-systemd", action="store_true")
    args = parser.parse_args(argv)

    try:
        installer.check_python()
        if not args.skip_package:
            print(installer.install_python_package(ROOT, dry_run=args.dry_run))
        example = installer.example_config()
        if example is None:
            print(
                "warning: config.example.toml not found; write the config manually",
                file=sys.stderr,
            )
        else:
            state = installer.install_config(
                example.read_text(encoding="utf-8"), dry_run=args.dry_run
            )
            print(f"config: {state}")
        notes = installer.install_assets(
            dry_run=args.dry_run, with_systemd=not args.no_systemd
        )
        for note in notes:
            print(note)
        if not args.no_systemd:
            print(f"systemd: {installer.enable_systemd(dry_run=args.dry_run)}")
        print(
            "Next: set your owner token in credentials, then run `vl doctor` and `vl chat`. "
            "The credentials file must stay 0600."
        )
        return 0
    except installer.InstallError as exc:
        print(f"install error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
