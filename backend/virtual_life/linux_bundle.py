"""Build reproducible Linux client artifacts from resources shipped inside the wheel."""

import gzip
import hashlib
import io
import json
import tarfile
import zipfile
from dataclasses import dataclass
from importlib.resources import files

VERSION = "0.1.0"


@dataclass(frozen=True)
class LinuxBundle:
    data: bytes
    sha256: str
    version: str


def _resources():
    root = files("virtual_life").joinpath("client_assets/system_node")
    result = {}
    for child in root.joinpath("system_node").iterdir():
        if child.name.endswith(".py"):
            result["system_node/" + child.name] = child.read_bytes()
    result["__main__.py"] = b"from system_node.cli import main\nraise SystemExit(main())\n"
    return root, result


def _tar(contents):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(stream.getvalue(), mtime=0)


def client_artifacts():
    root, source = _resources()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(source.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return {
        "source.tar.gz": _tar(source),
        "system-node.pyz": stream.getvalue(),
        "installer.py": root.joinpath("installer.py").read_bytes(),
    }


def get_linux_bundle() -> LinuxBundle:
    artifacts = client_artifacts()
    contents = {key: artifacts[key] for key in ("system-node.pyz", "installer.py")}
    manifest = {
        "version": VERSION,
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }
    contents["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    data = _tar(contents)
    return LinuxBundle(data, hashlib.sha256(data).hexdigest(), VERSION)
