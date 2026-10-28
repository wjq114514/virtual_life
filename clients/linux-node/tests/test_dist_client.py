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
    github_manifest,
    github_repo,
    is_file_source,
    manifest_url,
    package_url,
)

TOKEN = "owner-token"
V10 = "virtual_life_linux-0.1.0-py3-none-any.whl"
V11 = "virtual_life_linux-0.1.1-py3-none-any.whl"
V99 = "virtual_life_linux-9.9.9-py3-none-any.whl"
PAYLOADS = {
    V10: b"wheel-bytes-0.1.0",
    V11: b"wheel-bytes-0.1.1",
    V99: b"wheel-bytes-9.9.9",
}
PACKAGES = [
    {
        "version": version,
        "wheel": wheel,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    for version, wheel, payload in (("0.1.0", V10, PAYLOADS[V10]), ("0.1.1", V11, PAYLOADS[V11]))
]
GITHUB_REPO = "wjq114514/virtual_life"
GITHUB = {"releases": [], "bodies": {}, "auth": []}


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
    assert manifest.select("=0.1.1").version == "0.1.1"
    assert manifest.select("latest").version == "0.1.1"
    assert manifest.select(None).version == "0.1.1"
    assert manifest.select("").version == "0.1.1"
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
    with pytest.raises(DistError, match="client packages"):
        manifest.select(None)
    with pytest.raises(DistError, match="no manifest"):
        await fetch_manifest("file:///definitely/not/here", None)


def _digest(name: str) -> str:
    return hashlib.sha256(PAYLOADS[name]).hexdigest()


def _checksums(*names: str) -> bytes:
    return "".join(f"{_digest(name)}  {name}\n" for name in names).encode()


def load_github(releases: list) -> None:
    """Install release fixtures: (tag, published_at, [(name, body[, digest]), ...])."""
    GITHUB["releases"] = []
    GITHUB["bodies"] = {}
    for tag, published, assets in releases:
        spec = []
        for entry in assets:
            name, body = entry[0], entry[1]
            digest = entry[2] if len(entry) > 2 else None
            GITHUB["bodies"][(tag, name)] = body
            spec.append({"name": name, "size": len(body), "digest": digest})
        GITHUB["releases"].append(
            {
                "tag_name": tag,
                "published_at": published,
                "draft": False,
                "assets": spec,
            }
        )


class GitHubHandler(BaseHTTPRequestHandler):
    """A minimal stand-in for the GitHub releases API and asset downloads."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization")
        if auth:
            GITHUB["auth"].append(auth)
        if self.path.startswith("/repos/"):
            if auth and auth != "Bearer ghp-test-token":
                self.send_error(401)
                return
            if not self.path.startswith(f"/repos/{GITHUB_REPO}/releases"):
                self.send_error(404)
                return
            host = self.headers.get("Host", "127.0.0.1")
            releases = []
            for release in GITHUB["releases"]:
                assets = [
                    {
                        **asset,
                        "browser_download_url": f"http://{host}/assets/{release['tag_name']}/{asset['name']}",
                    }
                    for asset in release["assets"]
                ]
                releases.append({**release, "assets": assets})
            body = json.dumps(releases).encode()
        elif self.path.startswith("/assets/"):
            parts = self.path[len("/assets/") :].split("/", 1)
            body = GITHUB["bodies"].get(tuple(parts)) if len(parts) == 2 else None
        else:
            self.send_error(404)
            return
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def github():
    load_github([])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), GitHubHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_github_repo_normalizes_references():
    for value in (
        "owner/repo",
        "owner/repo.git",
        "https://github.com/owner/repo",
        "https://github.com/owner/repo/",
        "https://github.com/owner/repo#subdirectory=clients/linux-node",
        "git@github.com:owner/repo.git",
    ):
        assert github_repo(value) == "owner/repo"
    for value in ("just-a-name", "a/b/c", "/repo"):
        with pytest.raises(DistError, match="owner/repo"):
            github_repo(value)


async def test_server_manifest_cannot_supply_a_download_url(server):
    # Only the GitHub path sets download_url, so a runtime manifest can never
    # redirect a wheel download -- and the runtime token with it.
    manifest = await fetch_manifest(server, TOKEN)
    assert not any(package.download_url for package in manifest.packages)


async def test_github_manifest_builds_packages_from_releases(github):
    load_github(
        [
            (
                "v0.1.0",
                "2026-10-01T00:00:00Z",
                [(V10, PAYLOADS[V10]), ("sha256sums", _checksums(V10))],
            ),
            (
                "v0.1.1",
                "2026-10-02T00:00:00Z",
                [(V11, PAYLOADS[V11]), ("SHA256SUMS", _checksums(V11))],
            ),
        ]
    )
    manifest = await github_manifest(GITHUB_REPO, api_base=github)
    assert [package.version for package in manifest.packages] == ["0.1.0", "0.1.1"]
    assert manifest.default_version == "0.1.1"
    newest = manifest.select("latest")
    assert newest.version == "0.1.1"
    assert newest.released_at == "2026-10-02T00:00:00Z"
    assert newest.size_bytes == len(PAYLOADS[V11])
    assert newest.download_url.endswith(f"/assets/v0.1.1/{V11}")
    assert newest.sha256 == _digest(V11)


async def test_latest_skips_a_package_without_a_published_checksum(github, tmp_path):
    load_github(
        [
            (
                "v0.1.0",
                "2026-10-01T00:00:00Z",
                [(V10, PAYLOADS[V10]), ("sha256sums", _checksums(V10))],
            ),
            ("v9.9.9", "2026-10-02T00:00:00Z", [(V99, PAYLOADS[V99])]),
        ]
    )
    manifest = await github_manifest(GITHUB_REPO, api_base=github)
    # The unverifiable wheel is still listed, so it can be named explicitly --
    # but it must never be the default choice.
    assert [package.version for package in manifest.packages] == ["0.1.0", "9.9.9"]
    assert manifest.default_version == "0.1.0"
    assert manifest.latest.version == "0.1.0"
    assert manifest.select("latest").version == "0.1.0"
    assert manifest.select("9.9.9").sha256 == ""
    with pytest.raises(DistError, match="no checksum was published"):
        await download_package("", manifest.select("9.9.9"), tmp_path / "out.whl", None)


async def test_github_manifest_uses_the_asset_digest_without_a_checksums_file(github):
    load_github(
        [("0.1.1", "2026-10-02T00:00:00Z", [(V11, PAYLOADS[V11], "SHA256:" + _digest(V11))])]
    )
    manifest = await github_manifest(GITHUB_REPO, api_base=github)
    assert [package.version for package in manifest.packages] == ["0.1.1"]
    assert manifest.select("latest").sha256 == _digest(V11)


async def test_github_manifest_refuses_a_release_without_checksums(github):
    load_github([("v0.1.0", "2026-10-01T00:00:00Z", [(V10, PAYLOADS[V10])])])
    with pytest.raises(DistError, match="no published checksums"):
        await github_manifest(GITHUB_REPO, api_base=github)


async def test_github_manifest_ignores_draft_releases_and_unknown_repos(github):
    load_github(
        [
            (
                "v9.9.9",
                "2026-10-01T00:00:00Z",
                [(V10, PAYLOADS[V10]), ("sha256sums", _checksums(V10))],
            )
        ]
    )
    published = await github_manifest(GITHUB_REPO, api_base=github)
    assert [package.version for package in published.packages] == ["0.1.0"]

    GITHUB["releases"][0]["draft"] = True
    drafts = await github_manifest(GITHUB_REPO, api_base=github)
    assert drafts.packages == ()
    assert drafts.default_version == ""
    with pytest.raises(DistError, match="client packages"):
        drafts.select(None)

    with pytest.raises(DistError, match="no such repository"):
        await github_manifest("nobody/nothing", api_base=github)


async def test_github_manifest_reports_a_rejected_token(github):
    GITHUB["releases"] = []
    with pytest.raises(DistError, match="rejected the token"):
        await github_manifest("private/repo", "ghp-not-a-real-token", api_base=github)
    assert GITHUB["auth"] == ["Bearer ghp-not-a-real-token"]


async def test_github_download_uses_the_asset_url_and_verifies(github, tmp_path):
    load_github(
        [
            (
                "v0.1.1",
                "2026-10-02T00:00:00Z",
                [(V11, PAYLOADS[V11]), ("sha256sums", _checksums(V11))],
            )
        ]
    )
    manifest = await github_manifest(GITHUB_REPO, api_base=github)
    destination = tmp_path / "out" / V11
    result = await download_package("", manifest.select("latest"), destination, None)
    assert result == destination
    assert destination.read_bytes() == PAYLOADS[V11]
    assert not destination.with_suffix(".whl.part").exists()


async def test_github_download_rejects_a_tampered_wheel(github, tmp_path):
    body = PAYLOADS[V11] + b"tampered"
    load_github(
        [("v0.1.1", "2026-10-02T00:00:00Z", [(V11, body), ("sha256sums", _checksums(V11))])]
    )
    manifest = await github_manifest(GITHUB_REPO, api_base=github)
    destination = tmp_path / "out" / V11
    with pytest.raises(DistError, match="sha256 mismatch"):
        await download_package("", manifest.select("latest"), destination, None)
    assert not destination.exists()
