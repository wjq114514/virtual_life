"""In-process scene-classifier runtime for dialogue routing and observation.

Two integration modes are supported:

``route``
    Classify the user message before provider dispatch and, when the predicted
    label has a configured model, override only the reply model.  The existing
    owner, lease, budget, prompt and output gates are untouched.

``observe``
    Classify the same message and attach the result to the dialogue receipt,
    but never change the model, prompt, streaming or final output.

Inference is bounded by a single worker and a short timeout.  Missing
dependencies, a missing artifact, a timeout, or any prediction error degrade to
an observation with an ``error_code`` and leave the dialogue on its normal path.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from math import isfinite
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..scene_config import SceneClassifierConfig, scene_classifier_config
from .artifact import load_artifact
from .decision import Thresholds
from .inference import Predictor, build_predictor
from .labels import LABELS

logger = logging.getLogger(__name__)

_ERROR_UNAVAILABLE = "classifier_unavailable"
_ERROR_BUSY = "classifier_busy"
_ERROR_TIMEOUT = "classifier_timeout"
_ERROR_FAILED = "classifier_failed"
_ERROR_EMPTY = "classifier_empty_text"


class SceneObservation(BaseModel):
    """Diagnostic scene metadata attached to one dialogue receipt.

    It carries no raw user text and never grants output authority.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["route", "observe"]
    label: Literal["daily", "emotional", "intimate", "explicit"] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    scores: dict[str, float] | None = None
    reason: str | None = Field(default=None, max_length=64)
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    routed_model: str | None = Field(default=None, max_length=256)
    classifier_version: str | None = Field(default=None, max_length=128)
    backend: str | None = Field(default=None, max_length=32)
    latency_ms: float | None = Field(default=None, ge=0.0)
    error_code: str | None = Field(default=None, max_length=64)

    @field_validator("scores")
    @classmethod
    def _bounded_scores(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        if set(value) != set(LABELS):
            raise ValueError("scores must contain exactly the scene labels")
        normalized: dict[str, float] = {}
        for label in LABELS:
            score = value[label]
            if isinstance(score, bool) or not isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError("scores must be finite numbers between 0 and 1")
            normalized[label] = float(score)
        return normalized


class SceneClassifier:
    """Fail-open wrapper around one local classifier artifact."""

    def __init__(
        self,
        config: SceneClassifierConfig | object,
        *,
        predictor: Predictor | None = None,
        thresholds: Thresholds | object | None = None,
    ) -> None:
        self.config = scene_classifier_config(config)
        self.mode = self.config.mode
        self._predictor = predictor
        self._thresholds = None if thresholds is None else Thresholds.from_value(thresholds)
        self._error_code: str | None = None
        self._backend = getattr(predictor, "backend", None) if predictor is not None else None
        self._version = getattr(predictor, "version", None) if predictor is not None else None
        self._executor: ThreadPoolExecutor | None = None
        self._semaphore: threading.BoundedSemaphore | None = None
        if self.mode == "off":
            return
        if self._predictor is None:
            self._load()
        else:
            if self._thresholds is None:
                self._thresholds = Thresholds()
            self._start_worker()

    def _load(self) -> None:
        try:
            _, limits = load_artifact(self.config.artifact)
            predictor = build_predictor(
                self.config.artifact,
                backend=self.config.backend,
                max_length=self.config.max_length,
                threads=self.config.threads or None,
                prefer_int8=self.config.prefer_int8,
            )
            predictor.warmup()
        except Exception as exc:
            # Classifier setup is advisory.  A missing optional dependency or a
            # bad artifact must not stop the API or block dialogue.
            self._error_code = _ERROR_UNAVAILABLE
            logger.warning(
                "Scene classifier is unavailable; dialogue will continue without it: %s", exc
            )
            return
        self._predictor = predictor
        self._thresholds = limits
        self._backend = getattr(predictor, "backend", None)
        self._version = getattr(predictor, "version", None)
        self._start_worker()

    def _start_worker(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scene-classifier")
        self._semaphore = threading.BoundedSemaphore(1)

    @property
    def available(self) -> bool:
        return self.mode != "off" and self._predictor is not None and self._error_code is None

    def classify(self, text: object) -> SceneObservation | None:
        """Return a bounded observation; never raise for inference failures."""

        if self.mode == "off":
            return None
        started = perf_counter()
        if not isinstance(text, str) or not text.strip():
            return self._degraded(_ERROR_EMPTY, started)
        if self._predictor is None or self._thresholds is None or self._error_code is not None:
            return self._degraded(self._error_code or _ERROR_UNAVAILABLE, started)
        executor, semaphore = self._executor, self._semaphore
        if executor is None or semaphore is None:
            return self._degraded(_ERROR_UNAVAILABLE, started)
        # Do not build a queue behind a slow inference.  A busy classifier is
        # an observation-only failure, never a reason to wait for dialogue.
        if not semaphore.acquire(blocking=False):
            return self._degraded(_ERROR_BUSY, started)
        try:
            future = executor.submit(self._predict, text[: self.config.max_chars])
        except Exception:
            semaphore.release()
            return self._degraded(_ERROR_FAILED, started)
        future.add_done_callback(lambda _: semaphore.release())
        try:
            decision = future.result(timeout=self.config.timeout_seconds)
        except FutureTimeoutError:
            return self._degraded(_ERROR_TIMEOUT, started)
        except Exception as exc:
            logger.warning("Scene classifier inference failed: %s", exc)
            return self._degraded(_ERROR_FAILED, started)
        return SceneObservation(
            mode=self.mode,
            label=decision.label,
            confidence=round(float(decision.confidence), 6),
            scores={label: round(float(score), 6) for label, score in decision.scores.items()},
            reason=decision.reason,
            threshold=(None if decision.threshold is None else round(float(decision.threshold), 6)),
            classifier_version=self._version,
            backend=self._backend,
            latency_ms=round((perf_counter() - started) * 1000, 3),
        )

    def _predict(self, text: str):
        assert self._predictor is not None
        assert self._thresholds is not None
        return self._predictor.predict(text, self._thresholds)

    def _degraded(self, code: str, started: float) -> SceneObservation:
        return SceneObservation(
            mode=self.mode,
            classifier_version=self._version,
            backend=self._backend,
            latency_ms=round((perf_counter() - started) * 1000, 3),
            error_code=code,
        )

    def route_model(self, label: str) -> str:
        """Return the configured model for a label, or ``""`` for the default."""

        return self.config.route_model(label)

    def capabilities(self) -> dict:
        result = {
            "mode": self.mode,
            "enabled": self.mode != "off",
            "available": self.available,
            "backend": self._backend,
            "version": self._version,
            "labels": list(LABELS),
            "route_models": dict(self.config.models),
            "max_length": self.config.max_length,
            "max_chars": self.config.max_chars,
            "timeout_seconds": self.config.timeout_seconds,
        }
        if self._error_code is not None:
            result["error_code"] = self._error_code
        return result

    def close(self) -> None:
        if self._executor is None:
            return
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = None
        self._semaphore = None
