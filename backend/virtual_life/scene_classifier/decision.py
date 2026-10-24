"""Thresholded decisions and offline threshold tuning.

The router does not use a plain ``argmax``.  It walks the classes from most to
least sensitive and accepts the first probability above that class threshold;
when nothing crosses, the turn stays on the cheap ``daily`` route.  Keeping the
decision rule here (rather than in the HTTP service) means training, evaluation
and serving all share exactly the same semantics.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral
from typing import Any

import numpy as np

from .labels import LABEL_INDEX, LABELS, SceneDecision

# ``daily`` is the fallback and its threshold is intentionally unused.  It is
# still stored so artifacts remain self-describing and compatible with the
# four-value thresholds described in the training guide.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "daily": 0.0,
    "emotional": 0.55,
    "intimate": 0.55,
    "explicit": 0.35,
}

# Ordered from most to least sensitive.  ``daily`` is never tested directly.
DECISION_ORDER: tuple[str, ...] = ("explicit", "intimate", "emotional")


@dataclass(frozen=True, slots=True)
class Thresholds:
    daily: float = 0.0
    emotional: float = 0.55
    intimate: float = 0.55
    explicit: float = 0.35

    def __post_init__(self) -> None:
        for label in LABELS:
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"threshold for {label} must be a number")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"threshold for {label} must be between 0 and 1")

    def for_class(self, label: str) -> float:
        if label not in LABEL_INDEX:
            raise ValueError(f"unknown label: {label}")
        return float(getattr(self, label))

    def as_dict(self) -> dict[str, float]:
        return {label: float(getattr(self, label)) for label in LABELS}

    def as_list(self) -> list[float]:
        """Ordered like :data:`LABELS` for older integration code."""

        return [float(getattr(self, label)) for label in LABELS]

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "Thresholds":
        unknown = set(values) - set(LABELS)
        if unknown:
            raise ValueError(f"unknown threshold label: {sorted(unknown)[0]}")
        return cls(**{label: float(values[label]) for label in LABELS if label in values})

    @classmethod
    def from_value(cls, value: object) -> "Thresholds":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls.from_mapping(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != len(LABELS):
                raise ValueError(f"thresholds must contain {len(LABELS)} values")
            return cls(**dict(zip(LABELS, (float(item) for item in value), strict=True)))
        raise ValueError("thresholds must be an object or an ordered list")


@dataclass(frozen=True, slots=True)
class TuningResult:
    thresholds: Thresholds
    metrics: dict[str, Any]
    feasible: bool
    evaluated: int
    target_explicit_recall: float
    target_daily_precision: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "thresholds": self.thresholds.as_dict(),
            "metrics": self.metrics,
            "feasible": self.feasible,
            "evaluated": self.evaluated,
            "target_explicit_recall": self.target_explicit_recall,
            "target_daily_precision": self.target_daily_precision,
        }


def softmax(logits: Sequence[float], *, temperature: float = 1.0) -> list[float]:
    """Numerically stable softmax for a single row."""

    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("temperature must be a finite positive number")
    values = [float(value) / temperature for value in logits]
    if not values:
        raise ValueError("logits must not be empty")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("logits must be finite")
    largest = max(values)
    exponents = [math.exp(value - largest) for value in values]
    total = sum(exponents)
    if total <= 0:
        raise ValueError("logits produced an invalid distribution")
    return [value / total for value in exponents]


def _normalized_scores(scores: object) -> list[float]:
    if isinstance(scores, Mapping):
        missing = [label for label in LABELS if label not in scores]
        if missing:
            raise ValueError(f"missing score for label: {missing[0]}")
        values = [float(scores[label]) for label in LABELS]
    else:
        values = [float(value) for value in scores]  # type: ignore[union-attr]
    if len(values) != len(LABELS):
        raise ValueError(f"scores must contain {len(LABELS)} values")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("scores must be finite and non-negative")
    total = sum(values)
    if total <= 0:
        raise ValueError("scores must have a positive sum")
    return [value / total for value in values]


def decide(
    scores: Mapping[str, float] | Sequence[float],
    thresholds: Thresholds | Mapping[str, float] | Sequence[float] | None = None,
) -> SceneDecision:
    """Apply the ordered threshold rule to one probability distribution."""

    limits = Thresholds.from_value(thresholds) if thresholds is not None else Thresholds()
    probabilities = _normalized_scores(scores)
    by_label = dict(zip(LABELS, probabilities, strict=True))
    for label in DECISION_ORDER:
        limit = limits.for_class(label)
        if by_label[label] > limit:
            return SceneDecision(
                label=label,
                confidence=by_label[label],
                scores=by_label,
                reason="threshold",
                threshold=limit,
            )
    return SceneDecision(
        label="daily",
        confidence=by_label["daily"],
        scores=by_label,
        reason="fallback",
        threshold=limits.daily,
    )


def confusion_matrix(
    true_labels: Sequence[str | int], predicted_labels: Sequence[str | int]
) -> list[list[int]]:
    """Return a rows=true, columns=predicted matrix in :data:`LABELS` order."""

    if len(true_labels) != len(predicted_labels):
        raise ValueError("true and predicted labels must have the same length")
    matrix = [[0] * len(LABELS) for _ in LABELS]
    for truth, prediction in zip(true_labels, predicted_labels, strict=True):
        matrix[_label_index(truth)][_label_index(prediction)] += 1
    return matrix


def accuracy(true_labels: Sequence[str | int], predicted_labels: Sequence[str | int]) -> float:
    if not true_labels:
        return 0.0
    if len(true_labels) != len(predicted_labels):
        raise ValueError("true and predicted labels must have the same length")
    matches = sum(
        _label_index(truth) == _label_index(prediction)
        for truth, prediction in zip(true_labels, predicted_labels, strict=True)
    )
    return matches / len(true_labels)


def classification_report(
    true_labels: Sequence[str | int], predicted_labels: Sequence[str | int]
) -> dict[str, Any]:
    matrix = confusion_matrix(true_labels, predicted_labels)
    classes: dict[str, dict[str, float | int]] = {}
    supported_f1: list[float] = []
    for index, label in enumerate(LABELS):
        true_positive = matrix[index][index]
        support = sum(matrix[index])
        predicted = sum(row[index] for row in matrix)
        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
        classes[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
            "predicted": predicted,
        }
        if support:
            supported_f1.append(f1)
    return {
        "accuracy": accuracy(true_labels, predicted_labels),
        "macro_f1": sum(supported_f1) / len(supported_f1) if supported_f1 else 0.0,
        "worst_f1": min(supported_f1) if supported_f1 else 0.0,
        "classes": classes,
        "confusion_matrix": matrix,
        "samples": len(true_labels),
    }


def macro_f1(true_labels: Sequence[str | int], predicted_labels: Sequence[str | int]) -> float:
    return float(classification_report(true_labels, predicted_labels)["macro_f1"])


def tune_thresholds(
    probabilities: Sequence[Sequence[float]] | np.ndarray,
    true_labels: Sequence[str | int],
    *,
    target_explicit_recall: float = 0.92,
    target_daily_precision: float = 0.85,
    explicit_grid: Sequence[float] | None = None,
    intimate_grid: Sequence[float] | None = None,
    emotional_grid: Sequence[float] | None = None,
) -> TuningResult:
    """Grid-search the deployment decision rule on validation predictions.

    Unlike a per-class one-vs-rest sweep, this evaluates the actual ordered
    rule, so the three thresholds are not optimized independently.  The search
    first requires ``explicit`` recall and ``daily`` precision targets; only
    then does it maximize macro F1.  If the targets are unreachable it falls
    back to the candidate with the highest explicit recall (the documented
    failure direction: never silently downgrade sensitive turns).
    """

    _validate_targets(target_explicit_recall, target_daily_precision)
    matrix = np.asarray(probabilities, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != len(LABELS):
        raise ValueError(f"probabilities must have shape (n, {len(LABELS)})")
    if matrix.shape[0] != len(true_labels):
        raise ValueError("probabilities and labels must have the same length")
    if matrix.shape[0] == 0:
        raise ValueError("cannot tune thresholds without validation samples")
    if not np.isfinite(matrix).all() or (matrix < 0).any():
        raise ValueError("probabilities must be finite and non-negative")
    row_sums = matrix.sum(axis=1, keepdims=True)
    if (row_sums <= 0).any():
        raise ValueError("each probability row must have a positive sum")
    matrix = matrix / row_sums
    truth = np.asarray([_label_index(label) for label in true_labels], dtype=np.int64)

    explicit_values = explicit_grid or _grid(0.15, 0.80, 0.05)
    intimate_values = intimate_grid or _grid(0.20, 0.85, 0.05)
    emotional_values = emotional_grid or _grid(0.20, 0.85, 0.05)

    daily_index, emotional_index, intimate_index, explicit_index = range(len(LABELS))
    best_key: tuple[float, ...] | None = None
    best: tuple[float, float, float] | None = None
    best_metrics: dict[str, Any] | None = None
    evaluated = 0

    for explicit_limit in explicit_values:
        explicit_mask = matrix[:, explicit_index] > explicit_limit
        for intimate_limit in intimate_values:
            intimate_mask = matrix[:, intimate_index] > intimate_limit
            for emotional_limit in emotional_values:
                predicted = np.full(matrix.shape[0], daily_index, dtype=np.int64)
                predicted[matrix[:, emotional_index] > emotional_limit] = emotional_index
                predicted[intimate_mask] = intimate_index
                predicted[explicit_mask] = explicit_index
                metrics = _metrics_from_arrays(truth, predicted)
                evaluated += 1
                feasible = (
                    metrics["classes"]["explicit"]["recall"] >= target_explicit_recall
                    and metrics["classes"]["daily"]["precision"] >= target_daily_precision
                )
                prior_distance = (
                    abs(explicit_limit - DEFAULT_THRESHOLDS["explicit"])
                    + abs(intimate_limit - DEFAULT_THRESHOLDS["intimate"])
                    + abs(emotional_limit - DEFAULT_THRESHOLDS["emotional"])
                )
                if feasible:
                    key = (
                        1.0,
                        metrics["macro_f1"],
                        metrics["classes"]["explicit"]["recall"],
                        metrics["classes"]["daily"]["precision"],
                        -prior_distance,
                    )
                else:
                    key = (
                        0.0,
                        metrics["classes"]["explicit"]["recall"],
                        metrics["macro_f1"],
                        metrics["classes"]["daily"]["precision"],
                        -prior_distance,
                    )
                if best_key is None or key > best_key:
                    best_key = key
                    best = (explicit_limit, intimate_limit, emotional_limit)
                    best_metrics = metrics

    assert best is not None and best_metrics is not None
    thresholds = Thresholds(
        daily=0.0,
        emotional=round(best[2], 4),
        intimate=round(best[1], 4),
        explicit=round(best[0], 4),
    )
    feasible = bool(
        best_metrics["classes"]["explicit"]["recall"] >= target_explicit_recall
        and best_metrics["classes"]["daily"]["precision"] >= target_daily_precision
    )
    return TuningResult(
        thresholds=thresholds,
        metrics=best_metrics,
        feasible=feasible,
        evaluated=evaluated,
        target_explicit_recall=target_explicit_recall,
        target_daily_precision=target_daily_precision,
    )


def _label_index(label: str | int) -> int:
    if isinstance(label, bool):
        raise ValueError(f"invalid label: {label!r}")
    if isinstance(label, Integral):
        if not 0 <= label < len(LABELS):
            raise ValueError(f"label index out of range: {label}")
        return label
    if label not in LABEL_INDEX:
        raise ValueError(f"unknown label: {label}")
    return LABEL_INDEX[label]


def _grid(start: float, stop: float, step: float) -> tuple[float, ...]:
    count = int(round((stop - start) / step)) + 1
    return tuple(round(start + index * step, 4) for index in range(count))


def _validate_targets(explicit_recall: float, daily_precision: float) -> None:
    for name, value in (
        ("target_explicit_recall", explicit_recall),
        ("target_daily_precision", daily_precision),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")


def _metrics_from_arrays(truth: np.ndarray, predicted: np.ndarray) -> dict[str, Any]:
    counts = np.bincount(truth * len(LABELS) + predicted, minlength=len(LABELS) ** 2).reshape(
        len(LABELS), len(LABELS)
    )
    classes: dict[str, dict[str, float | int]] = {}
    supported_f1: list[float] = []
    for index, label in enumerate(LABELS):
        true_positive = int(counts[index, index])
        support = int(counts[index].sum())
        predicted_count = int(counts[:, index].sum())
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
        classes[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
            "predicted": predicted_count,
        }
        if support:
            supported_f1.append(f1)
    samples = int(truth.size)
    return {
        "accuracy": float(np.trace(counts) / samples) if samples else 0.0,
        "macro_f1": float(sum(supported_f1) / len(supported_f1)) if supported_f1 else 0.0,
        "worst_f1": float(min(supported_f1)) if supported_f1 else 0.0,
        "classes": classes,
        "confusion_matrix": counts.tolist(),
        "samples": samples,
    }
