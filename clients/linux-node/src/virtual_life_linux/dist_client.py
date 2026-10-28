"""Client for the runtime's distribution endpoints.

Fetches ``/v1/dist/manifest.json`` and a published wheel, verifying the published
sha256 before anything is installed. Works against a live runtime (bearer token)
or against a plain location that mirrors the same layout (``file://`` or a
tokenless static server).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

MANIFEST_SUFFIX = "/dist/manifest.json"
PACKAGE_PREFIX = "/dist/packages/"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)([a-zA-Z0-9.\-+]*)$")
# Release asset names, kept independent of the server's own name filter.
WHEEL_NAME_RE = re.compile(
    r"^virtual_life_linux-(?P<version>\d+\.\d+\.\d+[a-zA-Z0-9.\-+]*?)-py3-none-any\.whl$"
)
GITHUB_API_BASE = "https://api.github.com"
GITHUB_PER_PAGE = 50
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
CHUNK_SIZE = 1024 * 1024


class DistError(RuntimeError):
    """A distribution request failed. Messages are safe to show the user."""


@dataclass(frozen=True, slots=True)
class DistPackage:
    version: str
    wheel: str
    sha256: str
    size_bytes: int = 0
    released_at: str = ""
    # Populated only by github_manifest(). A server manifest cannot set it, so the
    # runtime token is never sent to a host the manifest did not come from.
    download_url: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> "DistPackage":
        return cls(
            version=str(raw["version"]),
            wheel=str(raw["wheel"]),
            sha256=str(raw["sha256"]),
            size_bytes=int(raw.get("size_bytes", 0)),
            released_at=str(raw.get("released_at", "")),
        )


@dataclass(frozen=True, slots=True)
class DistManifest:
    default_version: str = ""
    packages: tuple[DistPackage, ...] = ()

    @classmethod
    def from_json(cls, raw: dict) -> "DistManifest":
        packages = tuple(DistPackage.from_json(item) for item in raw.get("packages", ()))
        packages = tuple(sorted(packages, key=lambda package: _version_key(package.version)))
        default = str(raw.get("default_version", "")) or (packages[-1].version if packages else "")
        return cls(default_version=default, packages=packages)

    @property
    def latest(self) -> DistPackage | None:
        """The newest package with a published checksum.

        ``latest`` is the default choice, so it must never point at a package
        that cannot be verified; packages without a checksum are still
        reachable through an explicit version.
        """
        available = [package for package in self.packages if package.sha256]
        return available[-1] if available else None

    def select(self, spec: str | None) -> DistPackage:
        if not self.packages:
            raise DistError("No client packages are published yet")
        spec = str(spec).strip().lstrip("=") if spec is not None else ""
        if not spec or spec == "latest":
            return self.latest
        wanted = spec
        for package in self.packages:
            if package.version == wanted:
                return package
        available = ", ".join(package.version for package in self.packages)
        raise DistError(f"No published package matches version {wanted!r}; available: {available}")


def is_file_source(server_url: str) -> bool:
    return urlparse(server_url.strip()).scheme == "file"


def _normalized_base(server_url: str) -> str:
    """The runtime root: a documented /v1 suffix is not a deployment prefix."""
    base = server_url.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def manifest_url(server_url: str) -> str:
    base = _normalized_base(server_url)
    if is_file_source(base):
        return base + MANIFEST_SUFFIX
    return base + "/v1" + MANIFEST_SUFFIX


def package_url(server_url: str, wheel: str) -> str:
    base = _normalized_base(server_url)
    if is_file_source(base):
        return base + PACKAGE_PREFIX + wheel
    return base + "/v1" + PACKAGE_PREFIX + wheel


def package_location(server_url: str, package: DistPackage) -> str:
    """Where a wheel is fetched from: its own URL when it has one, else the source."""
    if package.download_url:
        return package.download_url
    return package_url(server_url, package.wheel)


def _version_key(version: str) -> tuple[int, int, int, int, str]:
    """A final release outranks its own prereleases, matching the server."""
    match = _VERSION_RE.match(version)
    if match is None:
        return (0, 0, 0, 1, version)
    tail = match.group(4)
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), 1 if tail else 2, tail


def _headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token.strip()}"}


def _explain(exc: Exception, action: str) -> DistError:
    if isinstance(exc, httpx.TimeoutException):
        return DistError(f"{action}: the server did not answer in time")
    if isinstance(exc, httpx.TransportError):
        return DistError(f"{action}: could not reach the server")
    return DistError(f"{action}: {exc}")


async def fetch_manifest(
    server_url: str,
    token: str | None = None,
    *,
    timeout: httpx.Timeout | float | None = None,
) -> DistManifest:
    """Download and parse the package manifest."""
    effective = DEFAULT_TIMEOUT if timeout is None else timeout
    manifest_location = manifest_url(server_url)
    action = f"Fetching the package manifest from {manifest_location}"
    if is_file_source(server_url):
        path = Path(urlparse(manifest_location).path)
        if not path.is_file():
            raise DistError(f"{action}: no manifest at {path}")
        try:
            return DistManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DistError(f"{action}: unreadable manifest ({exc})") from exc
    try:
        async with httpx.AsyncClient(timeout=effective) as client:
            response = await client.get(manifest_url(server_url), headers=_headers(token))
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _explain(exc, action) from exc
    if response.status_code == 401 or response.status_code == 403:
        raise DistError("Fetching the manifest: the server rejected the token")
    if response.status_code == 404:
        raise DistError("Fetching the manifest: the server publishes no client packages yet")
    if response.status_code != 200:
        raise DistError(f"Fetching the manifest: the server returned HTTP {response.status_code}")
    try:
        return DistManifest.from_json(response.json())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DistError(f"Fetching the manifest: unreadable manifest ({exc})") from exc


async def download_package(
    server_url: str,
    package: DistPackage,
    destination: Path,
    token: str | None = None,
    *,
    timeout: httpx.Timeout | float | None = None,
) -> Path:
    """Download a wheel and verify the published sha256 before it counts."""
    effective = DEFAULT_TIMEOUT if timeout is None else timeout
    action = f"Downloading {package.wheel}"
    location = package_location(server_url, package)
    if not package.sha256:
        raise DistError(
            f"Verifying {package.wheel}: no checksum was published, refusing to install it"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if not package.download_url and is_file_source(server_url):
        source = Path(urlparse(location).path)
        if not source.is_file():
            raise DistError(f"{action}: no file at {source}")
        temporary.write_bytes(source.read_bytes())
    else:
        try:
            async with httpx.AsyncClient(timeout=effective, follow_redirects=True) as client:
                async with client.stream("GET", location, headers=_headers(token)) as response:
                    if response.status_code != 200:
                        raise DistError(
                            f"{action}: the server returned HTTP {response.status_code}"
                        )
                    with temporary.open("wb") as handle:
                        async for chunk in response.aiter_bytes(CHUNK_SIZE):
                            handle.write(chunk)
        except DistError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _explain(exc, action) from exc
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    if digest != package.sha256:
        temporary.unlink(missing_ok=True)
        raise DistError(
            f"Verifying {package.wheel}: sha256 mismatch "
            f"(expected {package.sha256[:16]}..., got {digest[:16]}...); "
            f"the published checksum does not match, so nothing was installed. "
            f"Pick another version explicitly or republish the release."
        )
    temporary.replace(destination)
    return destination


def github_repo(value: str) -> str:
    """Normalize a repository reference to 'owner/repo'.

    Accepts a bare name, an https URL, an ssh URL, or a URL with a fragment.
    """
    text = value.strip()
    for prefix in (
        "git@github.com:",
        "https://github.com/",
        "http://github.com/",
        "https://www.github.com/",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    owner, _, repo = text.partition("/")
    if not owner or not repo or "/" in repo:
        raise DistError(f"--github must name a repository as owner/repo, got {value!r}")
    return f"{owner}/{repo}"


def _is_checksum_asset(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered in {
        "sha256sums",
        "sha256sums.txt",
        "checksums.txt",
        "sums",
        "sha256.txt",
    } or lowered.endswith(".sha256")


def _parse_checksums(text: str) -> dict[str, str]:
    """Read 'sha256  filename' lines, tolerating the '*' binary marker."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            continue
        sums[parts[1].lstrip("*").strip()] = parts[0]
    return sums


def _digest_value(value: object) -> str:
    """GitHub digests are 'SHA256:<hex>' or a bare hex string."""
    if not isinstance(value, str) or not value.strip():
        return ""
    digest = value.strip().lower().removeprefix("sha256:")
    return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else ""


def _release_version(tag: object) -> str:
    text = str(tag)
    return text[1:] if text.startswith("v") else text


async def github_manifest(
    repo: str,
    token: str | None = None,
    *,
    api_base: str = GITHUB_API_BASE,
    timeout: httpx.Timeout | float | None = None,
) -> DistManifest:
    """Build a package manifest from a repository's published releases.

    A wheel is only offered when its sha256 is published, either in a
    checksums asset of the same release or in the asset digest. Without one a
    download could not be verified, so the release is refused.
    """
    effective = DEFAULT_TIMEOUT if timeout is None else timeout
    location = f"{api_base.rstrip('/')}/repos/{repo}/releases?per_page={GITHUB_PER_PAGE}"
    action = f"Fetching the GitHub releases of {repo}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "virtual-life-linux",
    }
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"

    try:
        async with httpx.AsyncClient(timeout=effective, follow_redirects=True) as client:
            response = await client.get(location, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _explain(exc, action) from exc
    if response.status_code in (401, 403):
        raise DistError(
            f"{action}: GitHub rejected the token or rate-limited the request "
            "(use --github-token for private repositories)"
        )
    if response.status_code == 404:
        raise DistError(f"{action}: no such repository")
    if response.status_code != 200:
        raise DistError(f"{action}: GitHub returned HTTP {response.status_code}")
    try:
        releases = response.json()
    except json.JSONDecodeError as exc:
        raise DistError(f"{action}: unreadable response ({exc})") from exc
    if not isinstance(releases, list):
        raise DistError(f"{action}: unreadable response")

    packages: list[DistPackage] = []
    try:
        async with httpx.AsyncClient(timeout=effective, follow_redirects=True) as client:
            for release in releases:
                if not isinstance(release, dict) or release.get("draft"):
                    continue
                assets = [item for item in release.get("assets") or () if isinstance(item, dict)]
                wheels = [
                    item for item in assets if WHEEL_NAME_RE.match(str(item.get("name") or ""))
                ]
                if not wheels:
                    continue
                published = str(release.get("published_at") or "")
                sums: dict[str, str] = {}
                for asset in assets:
                    name = str(asset.get("name") or "")
                    address = str(asset.get("browser_download_url") or "")
                    if not _is_checksum_asset(name) or not address:
                        continue
                    try:
                        fetched = await client.get(address, headers=headers)
                    except (httpx.TimeoutException, httpx.TransportError):
                        continue
                    if fetched.status_code == 200:
                        sums.update(_parse_checksums(fetched.text))
                for asset in wheels:
                    name = str(asset["name"])
                    expected = sums.get(name) or _digest_value(asset.get("digest"))
                    packages.append(
                        DistPackage(
                            version=WHEEL_NAME_RE.match(name).group("version"),
                            wheel=name,
                            sha256=expected,
                            size_bytes=int(asset.get("size") or 0),
                            released_at=published,
                            download_url=str(asset.get("browser_download_url") or ""),
                        )
                    )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _explain(exc, action) from exc

    packages.sort(key=lambda package: _version_key(package.version))
    packages = tuple(packages)
    verified = tuple(package for package in packages if package.sha256)
    if packages and not verified:
        raise DistError(
            f"{action}: no published checksums; upload a SHA256SUMS asset next to the wheel "
            f"(packaging/build.py writes one) so a download can be verified"
        )
    return DistManifest(default_version=verified[-1].version if verified else "", packages=packages)
