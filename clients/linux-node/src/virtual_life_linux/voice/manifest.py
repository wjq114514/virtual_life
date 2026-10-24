"""Pinned, local-only sherpa-onnx model manifest validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_REQUIRED_FILES = {"tokens", "encoder", "decoder", "joiner", "vad"}
PINNED_SHERPA_VERSION = "1.13.8"


class VoiceModelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelManifest:
    path: Path
    model_id: str
    language: str
    sample_rate: int
    runtime_version: str
    revision: str
    source_url: str
    files: dict[str, Path]

    @classmethod
    def load(cls, path: Path, *, verify_hashes: bool = True) -> "ModelManifest":
        path = Path(path).expanduser().resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceModelError(f"cannot read ASR manifest {path}: {exc}") from exc
        expected = {
            "schema_version",
            "engine",
            "model_id",
            "language",
            "sample_rate",
            "runtime_version",
            "revision",
            "source_url",
            "files",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise VoiceModelError("ASR manifest fields do not match schema version 1")
        if raw["schema_version"] != 1 or raw["engine"] != "sherpa-onnx":
            raise VoiceModelError("ASR manifest must use schema 1 and sherpa-onnx")
        if raw["runtime_version"] != PINNED_SHERPA_VERSION:
            raise VoiceModelError(
                f"manifest runtime must be pinned to sherpa-onnx {PINNED_SHERPA_VERSION}"
            )
        if raw["sample_rate"] != 16_000:
            raise VoiceModelError("the configured streaming ASR sample rate must be 16000 Hz")
        if not all(
            isinstance(raw[key], str) and raw[key] for key in ("model_id", "revision", "source_url")
        ):
            raise VoiceModelError("model_id, revision, and source_url must be nonempty strings")
        if raw["language"] != "zh":
            raise VoiceModelError("the pinned Linux model catalog currently supports zh only")
        raw_files = raw["files"]
        if not isinstance(raw_files, dict) or set(raw_files) != _REQUIRED_FILES:
            raise VoiceModelError(f"manifest files must contain exactly {sorted(_REQUIRED_FILES)}")
        root = path.parent.resolve()
        files: dict[str, Path] = {}
        for name, item in raw_files.items():
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise VoiceModelError(f"manifest entry {name} must contain path and sha256")
            rel = item["path"]
            digest = item["sha256"]
            if (
                not isinstance(rel, str)
                or not rel
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
            ):
                raise VoiceModelError(f"manifest entry {name} has an invalid path or SHA-256")
            file_path = (root / rel).resolve()
            try:
                file_path.relative_to(root)
            except ValueError as exc:
                raise VoiceModelError(f"manifest file escapes its directory: {name}") from exc
            if not file_path.is_file():
                raise VoiceModelError(f"model file is missing: {file_path}")
            if verify_hashes and _sha256(file_path) != digest:
                raise VoiceModelError(f"model file SHA-256 mismatch: {file_path}")
            files[name] = file_path
        return cls(
            path=path,
            model_id=raw["model_id"],
            language=raw["language"],
            sample_rate=raw["sample_rate"],
            runtime_version=raw["runtime_version"],
            revision=raw["revision"],
            source_url=raw["source_url"],
            files=files,
        )

    def check_runtime(self) -> str:
        try:
            installed = version("sherpa-onnx")
        except PackageNotFoundError as exc:
            raise VoiceModelError(
                f"sherpa-onnx {PINNED_SHERPA_VERSION} is not installed; install the voice extra"
            ) from exc
        if installed != self.runtime_version:
            raise VoiceModelError(
                f"sherpa-onnx runtime is {installed}, manifest requires {self.runtime_version}"
            )
        return installed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
