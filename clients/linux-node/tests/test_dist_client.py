"""Distribution manifest fetch and verified wheel download."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from virtual_life_linux.dist_client import (
    DistError,
    DistManifest,
    download_package,
    fetch_manifest,
    is_file_source,
    manifest_url,
    package_url,
)

TOKEN = "owner-token"
V10 = "virtual_life_linux-0.1.0-py3-none-any.whl"
V11 = "virtual_life_linux-0.1.1-py3-none-any.whl"
PAYLOADS = {V10: b"wheel-bytes-0.1.0", V11: b"wheel-bytes-0.1.1"}
PACKAGES = [
    {
        "version": version,
        "wheel": wheel,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    for version, wheel, payload in (("0.1.0", V10, PAYLOADS[V10]), ("0.1.1", V11, PAYLOADS[V11]))
]


def manifest_body(packages=PACKAGES, default="0.1.1") -> dict:
    return {"dist_version": "1.0", "default_version": default, "packages": packages}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/dist/manifest.json":
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_error(401)
                return
            body = json.dumps(manifest_body()).encode()
        elif self.path.startswith("/v1/dist/packages/"):
            wheel = self.path.removeprefix("/v1/dist/packages/")
            if wheel not in PAYLOADS:
                self.send_error(404)
                return
            body = PAYLOADS[wheel]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def file_source(tmp_path: Path, packages=PACKAGES, default="0.1.1") -> str:
    dist = tmp_path / "dist"
    (dist / "packages").mkdir(parents=True)
    (dist / "manifest.json").write_text(
        json.dumps(manifest_body(packages, default)), encoding="utf-8"
    )
    for wheel, payload in PAYLOADS.items():
        (dist / "packages" / wheel).write_bytes(payload)
    return f"file://{tmp_path}"


def test_url_helpers():
    assert manifest_url("http://h:8038") == "http://h:8038/v1/dist/manifest.json"
    assert manifest_url("http://h:8038/") == "http://h:8038/v1/dist/manifest.json"
    assert manifest_url("http://h:8038/v1") == "http://h:8038/v1/dist/manifest.json"
    assert package_url("http://h:8038", V11) == f"http://h:8038/v1/dist/packages/{V11}"
    assert package_url("http://h:8038/v1", V11) == f"http://h:8038/v1/dist/packages/{V11}"
    assert manifest_url("file:///srv") == "file:///srv/dist/manifest.json"
    assert is_file_source("file:///srv") and not is_file_source("http://h")


async def test_manifest_requires_the_runtime_token(server):
    with pytest.raises(DistError, match="rejected the token"):
        await fetch_manifest(server, "wrong")


async def test_fetch_manifest_parses_and_sorts(server):
    manifest = await fetch_manifest(server, TOKEN)
    assert [package.version for package in manifest.packages] == ["0.1.0", "0.1.1"]
    assert manifest.default_version == "0.1.1"
    assert manifest.latest.version == "0.1.1"


async def test_select_matches_exactly_or_fails_clearly(server):
    manifest = await fetch_manifest(server, TOKEN)
    assert manifest.select("0.1.0").version == "0.1.0"
    assert manifest.select("=").version == "0.1.1"
    assert manifest.select("latest").version == "0.1.1"
    with pytest.raises(DistError, match="No published package matches"):
        manifest.select("9.9.9")
    assert "0.1.0" in str(manifest)


async def test_download_verifies_the_published_sha256(server, tmp_path):
    manifest = await fetch_manifest(server, TOKEN)
    destination = tmp_path / "downloaded.whl"
    result = await download_package(server, manifest.select("0.1.1"), destination, TOKEN)
    assert result == destination
    assert destination.read_bytes() == PAYLOADS[V11]
    assert not destination.with_suffix(".whl.part").exists()


async def test_download_rejects_a_hash_mismatch(server, tmp_path):
    tampered = DistManifest.from_json(
        manifest_body([{**PACKAGES[0], "sha256": "0" * 64}], default="0.1.0")
    )
    destination = tmp_path / "downloaded.whl"
    with pytest.raises(DistError, match="sha256 mismatch"):
        await download_package(server, tampered.select(None), destination, TOKEN)
    assert not destination.exists()
    assert not destination.with_suffix(".whl.part").exists()


async def test_file_source_needs_no_token(tmp_path):
    source = file_source(tmp_path)
    manifest = await fetch_manifest(source, None)
    assert manifest.latest.version == "0.1.1"
    destination = tmp_path / "out" / "wheel.whl"
    await download_package(source, manifest.select("0.1.0"), destination, None)
    assert destination.read_bytes() == PAYLOADS[V10]


async def test_empty_manifest_is_explicit(tmp_path):
    manifest = DistManifest.from_json(manifest_body([], default=""))
    assert manifest.latest is None
    with pytest.raises(DistError, match="no client packages"):
        manifest.select(None)
    with pytest.raises(DistError, match="no manifest"):
        await fetch_manifest("file:///definitely/not/here", None)
