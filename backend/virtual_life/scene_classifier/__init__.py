"""Local scene classifier used as an optional routing signal.

The package is deliberately free of heavyweight imports at module import time.
Training scripts depend on ``torch``/``transformers``; the runtime service can
use either PyTorch or ONNX Runtime and imports the chosen backend lazily.
"""

from .artifact import read_manifest, read_thresholds, write_manifest, write_thresholds
from .decision import (
    DEFAULT_THRESHOLDS,
    Thresholds,
    TuningResult,
    accuracy,
    classification_report,
    confusion_matrix,
    decide,
    macro_f1,
    softmax,
    tune_thresholds,
)
from .labels import LABEL_INDEX, LABELS, SceneDecision

__all__ = [
    "DEFAULT_THRESHOLDS",
    "LABELS",
    "LABEL_INDEX",
    "SceneDecision",
    "Thresholds",
    "TuningResult",
    "accuracy",
    "classification_report",
    "confusion_matrix",
    "decide",
    "macro_f1",
    "read_manifest",
    "read_thresholds",
    "softmax",
    "tune_thresholds",
    "write_manifest",
    "write_thresholds",
]
