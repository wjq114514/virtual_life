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
        return self.packages[-1] if self.packages else None

    def select(self, spec: str | None) -> DistPackage:
        if not self.packages:
            raise DistError("The server publishes no client packages yet")
        if spec is None or not str(spec).strip() or str(spec).strip() == "latest":
            return self.latest
        wanted = str(spec).strip().lstrip("=")
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


def _version_key(version: str) -> tuple[int, int, int, int, str]:
    parts = version.split(".")
    if len(parts) < 3 or not parts[0].isdigit():
        return (0, 0, 0, 0, version)
    major, minor, patch_tail = parts[0], parts[1], parts[2]
    patch, _, tail = patch_tail.partition("rc")
    if not patch.isdigit():
        patch, tail = "0", patch_tail
    return int(major), int(minor) if minor.isdigit() else 0, int(patch), 1 if tail else 2, tail


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
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if is_file_source(server_url):
        source = Path(urlparse(server_url).path).parent / package.wheel
        if not source.is_file():
            raise DistError(f"{action}: no file at {source}")
        temporary.write_bytes(source.read_bytes())
    else:
        try:
            async with httpx.AsyncClient(timeout=effective, follow_redirects=True) as client:
                async with client.stream(
                    "GET", package_url(server_url, package.wheel), headers=_headers(token)
                ) as response:
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
            f"(expected {package.sha256[:16]}..., got {digest[:16]}...)"
        )
    temporary.replace(destination)
    return destination
