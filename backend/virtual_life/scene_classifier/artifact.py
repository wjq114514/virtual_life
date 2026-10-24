"""Small, atomic JSON artifacts shared by training and serving."""

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .decision import Thresholds

MANIFEST_NAME = "manifest.json"
THRESHOLDS_NAME = "thresholds.json"
SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o644)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def write_thresholds(
    path: str | Path,
    thresholds: Thresholds | Mapping[str, float],
    *,
    metrics: Mapping[str, Any] | None = None,
    targets: Mapping[str, Any] | None = None,
) -> Path:
    limits = Thresholds.from_value(thresholds)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "thresholds": limits.as_dict(),
        "decision_order": ["explicit", "intimate", "emotional"],
        "fallback": "daily",
    }
    if metrics is not None:
        payload["metrics"] = dict(metrics)
    if targets is not None:
        payload["targets"] = dict(targets)
    return _write_json(path, payload)


def read_thresholds(path: str | Path) -> Thresholds:
    document = _read_json(path)
    raw = document.get("thresholds", document)
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: thresholds must be an object")
    return Thresholds.from_mapping(raw)


def write_manifest(path: str | Path, payload: Mapping[str, Any]) -> Path:
    document = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        **dict(payload),
    }
    return _write_json(path, document)


def read_manifest(path: str | Path) -> dict[str, Any]:
    document = _read_json(path)
    version = document.get("schema_version")
    if version is not None and version != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported manifest schema version {version!r}")
    return document


def load_artifact(
    artifact_dir: str | Path, *, thresholds_path: str | Path | None = None
) -> tuple[dict[str, Any], Thresholds]:
    directory = Path(artifact_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"classifier artifact directory not found: {directory}")
    manifest_path = directory / MANIFEST_NAME
    manifest = read_manifest(manifest_path) if manifest_path.exists() else {}
    limits_path = (
        Path(thresholds_path) if thresholds_path is not None else directory / THRESHOLDS_NAME
    )
    limits = read_thresholds(limits_path) if limits_path.exists() else Thresholds()
    return manifest, limits


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except FileNotFoundError:
        raise FileNotFoundError(f"artifact not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc.msg}") from None
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return document
