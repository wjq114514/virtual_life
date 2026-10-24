"""Immutable scene-classifier configuration shared by settings and runtime.

The classifier is optional. ``mode`` keeps the safe default ``off``; ``route``
uses the predicted label to override the reply model, while ``observe`` records
the prediction without changing model, prompt, streaming or output behavior.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from .scene_classifier.labels import LABELS

SCENE_MODES: tuple[str, ...] = ("off", "observe", "route")
SCENE_BACKENDS: tuple[str, ...] = ("auto", "onnx", "torch")
MAX_ARTIFACT_CHARS = 4096
MAX_MODEL_CHARS = 256
MIN_TIMEOUT_SECONDS = 0.01
MAX_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SceneClassifierConfig:
    mode: str = "off"
    artifact: str = ".data/nsfw_classifier/model/final"
    backend: str = "auto"
    max_length: int = 128
    threads: int = 0
    prefer_int8: bool = True
    max_chars: int = 2048
    timeout_seconds: float = 1.0
    models: dict[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def route_model(self, label: str) -> str:
        return self.models.get(label, "")


def scene_classifier_config(value: object) -> SceneClassifierConfig:
    """Validate a Settings field, including models parsed from TOML or JSON."""

    if value is None or value == {}:
        return SceneClassifierConfig()
    if isinstance(value, SceneClassifierConfig):
        config = value
    elif isinstance(value, Mapping):
        unknown = set(value) - set(SceneClassifierConfig.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown configuration field: scene_classifier.{sorted(unknown)[0]}")
        raw_models = value.get("models", {})
        if isinstance(raw_models, str):
            try:
                raw_models = json.loads(raw_models)
            except ValueError:
                raise ValueError("scene_classifier.models must be valid JSON") from None
        if not isinstance(raw_models, Mapping):
            raise ValueError("scene_classifier.models must be an object")
        config = SceneClassifierConfig(**{**value, "models": dict(raw_models)})
    else:
        raise ValueError("scene_classifier must be a table")

    if not isinstance(config.mode, str):
        raise ValueError("scene_classifier.mode must be a string")
    if config.mode not in SCENE_MODES:
        raise ValueError(f"scene_classifier.mode must be one of {list(SCENE_MODES)}")
    if not isinstance(config.artifact, str):
        raise ValueError("scene_classifier.artifact must be a string")
    artifact = config.artifact.strip()
    if len(artifact) > MAX_ARTIFACT_CHARS:
        raise ValueError(
            f"scene_classifier.artifact must not exceed {MAX_ARTIFACT_CHARS} characters"
        )
    if config.mode != "off" and not artifact:
        raise ValueError("scene_classifier.artifact must not be empty when enabled")
    if not isinstance(config.backend, str):
        raise ValueError("scene_classifier.backend must be a string")
    if config.backend not in SCENE_BACKENDS:
        raise ValueError(f"scene_classifier.backend must be one of {list(SCENE_BACKENDS)}")
    for name, minimum, maximum in (
        ("max_length", 16, 512),
        ("threads", 0, 64),
        ("max_chars", 1, 4096),
    ):
        value = getattr(config, name)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(
                f"scene_classifier.{name} must be an integer between {minimum} and {maximum}"
            )
    if type(config.prefer_int8) is not bool:
        raise ValueError("scene_classifier.prefer_int8 must be a boolean")
    timeout = config.timeout_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not MIN_TIMEOUT_SECONDS <= float(timeout) <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "scene_classifier.timeout_seconds must be a finite number between "
            f"{MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
        )
    if not isinstance(config.models, Mapping):
        raise ValueError("scene_classifier.models must be an object")
    models: dict[str, str] = {}
    for label, model in config.models.items():
        if label not in LABELS:
            raise ValueError(f"scene_classifier.models contains an unknown label: {label}")
        if not isinstance(model, str):
            raise ValueError("scene_classifier.models values must be strings")
        model = model.strip()
        if len(model) > MAX_MODEL_CHARS:
            raise ValueError(
                f"scene_classifier.models values must not exceed {MAX_MODEL_CHARS} characters"
            )
        if model:
            models[label] = model
    if config.mode == "route" and not models:
        raise ValueError("scene_classifier.mode=route requires at least one models entry")
    return SceneClassifierConfig(
        mode=config.mode,
        artifact=artifact,
        backend=config.backend,
        max_length=config.max_length,
        threads=config.threads,
        prefer_int8=config.prefer_int8,
        max_chars=config.max_chars,
        timeout_seconds=float(timeout),
        models=models,
    )
