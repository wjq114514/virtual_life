"""Synchronize wheel resources and emit portable artifacts (run with uv run python)."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESOURCE = ROOT / "backend/virtual_life/client_assets/system_node"


def main():
    RESOURCE.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        Path(__file__).parent / "system_node",
        RESOURCE / "system_node",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copyfile(Path(__file__).parent / "installer.py", RESOURCE / "installer.py")
    sys.path.insert(0, str(ROOT / "backend"))
    from virtual_life.linux_bundle import client_artifacts, get_linux_bundle

    output = Path(__file__).parent / "dist"
    output.mkdir(exist_ok=True)
    artifacts = client_artifacts()
    artifacts["bundle.tar.gz"] = get_linux_bundle().data
    artifacts["manifest.json"] = json.dumps(
        {
            "version": get_linux_bundle().version,
            "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in artifacts.items()},
        },
        sort_keys=True,
    ).encode()
    for name, data in artifacts.items():
        (output / name).write_bytes(data)
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n"
            for name, data in sorted(artifacts.items())
        )
    )


if __name__ == "__main__":
    main()
