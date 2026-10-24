"""Lazy PyTorch / ONNX inference adapters for the scene classifier."""

import importlib.util
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .artifact import MANIFEST_NAME, THRESHOLDS_NAME, read_manifest, read_thresholds
from .decision import Thresholds, decide, softmax
from .labels import LABELS, SceneDecision


@runtime_checkable
class Predictor(Protocol):
    backend: str
    version: str
    model_name: str

    def predict_proba(self, text: str) -> tuple[float, ...]: ...

    def predict_proba_many(
        self, texts: Sequence[str], *, batch_size: int = 32
    ) -> list[tuple[float, ...]]: ...

    def predict(self, text: str, thresholds: Thresholds | None = None) -> SceneDecision: ...

    def warmup(self) -> None: ...


class TorchPredictor:
    """``BertForSequenceClassification`` checkpoint on CPU (or CUDA)."""

    backend = "torch"

    def __init__(
        self,
        artifact_dir: str | Path,
        *,
        max_length: int = 128,
        threads: int | None = None,
        device: str = "cpu",
    ) -> None:
        try:
            import torch
            from transformers import AutoTokenizer, BertForSequenceClassification, BertTokenizer
        except ImportError as exc:  # pragma: no cover - exercised only without train deps
            raise RuntimeError(
                "PyTorch inference requires the training extras: uv sync --group train"
            ) from exc

        self.directory = Path(artifact_dir)
        if not self.directory.is_dir():
            raise FileNotFoundError(f"classifier artifact directory not found: {self.directory}")
        self.max_length = max_length
        self.device = device
        self._torch = torch
        if threads is not None:
            if threads < 1:
                raise ValueError("threads must be positive")
            torch.set_num_threads(threads)
        self._lock = threading.Lock()

        manifest_path = self.directory / MANIFEST_NAME
        self.manifest = read_manifest(manifest_path) if manifest_path.exists() else {}
        self.version = str(self.manifest.get("version") or self.directory.name)
        self.model_name = str(
            self.manifest.get("base_model") or self.manifest.get("model_name") or "unknown"
        )
        try:
            self.tokenizer = BertTokenizer.from_pretrained(str(self.directory))
        except (OSError, ValueError):
            # HFL checkpoints are BERT-style; AutoTokenizer is only a fallback
            # for locally saved checkpoints that include a fast tokenizer.
            self.tokenizer = AutoTokenizer.from_pretrained(str(self.directory), use_fast=True)
        self.model = BertForSequenceClassification.from_pretrained(str(self.directory))
        self.model.to(device)
        self.model.eval()

    def predict_proba(self, text: str) -> tuple[float, ...]:
        return self.predict_proba_many([text])[0]

    def predict_proba_many(
        self, texts: Sequence[str], *, batch_size: int = 32
    ) -> list[tuple[float, ...]]:
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return []
        cleaned = [_validate_text(text) for text in texts]
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        output: list[tuple[float, ...]] = []
        torch = self._torch
        with self._lock, torch.inference_mode():
            for start in range(0, len(cleaned), batch_size):
                batch = cleaned[start : start + batch_size]
                encoded = self.tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                encoded = {key: value.to(self.device) for key, value in encoded.items()}
                logits = self.model(**encoded).logits
                probabilities = torch.softmax(logits, dim=-1).cpu().tolist()
                output.extend(tuple(float(value) for value in row) for row in probabilities)
        return output

    def predict(self, text: str, thresholds: Thresholds | None = None) -> SceneDecision:
        return decide(self.predict_proba(text), thresholds)

    def warmup(self) -> None:
        self.predict_proba("你好，今天过得怎么样？")


class OnnxPredictor:
    """ONNX Runtime classifier, optionally using a verified dynamic UInt8 export."""

    backend = "onnx"

    def __init__(
        self,
        artifact_dir: str | Path,
        *,
        max_length: int = 128,
        threads: int | None = None,
        threads_affinity: str = "sequential",
        prefer_int8: bool = True,
    ) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from transformers import BertTokenizer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ONNX inference requires onnxruntime and transformers: uv sync --group train"
            ) from exc

        self.directory = Path(artifact_dir)
        manifest_path = self.directory / MANIFEST_NAME
        self.manifest = read_manifest(manifest_path) if manifest_path.exists() else {}
        onnx_info = self.manifest.get("onnx")
        int8_verified = isinstance(onnx_info, dict) and onnx_info.get("int8_verified") is True
        # Never select a quantized graph merely because the file exists.  The
        # exporter must have validated it against FP32 first; otherwise a bad
        # quantization silently degrades live routing.
        use_int8 = prefer_int8 and int8_verified
        candidates = (
            [self.directory / "model.int8.onnx", self.directory / "model.onnx"]
            if use_int8
            else [self.directory / "model.onnx", self.directory / "model.int8.onnx"]
        )
        model_path = next((path for path in candidates if path.exists()), None)
        if model_path is None:
            raise FileNotFoundError(f"no ONNX model in {self.directory}")
        self.version = str(self.manifest.get("version") or self.directory.name)
        self.model_name = str(self.manifest.get("base_model") or "unknown")
        self.max_length = max_length
        self.model_path = model_path
        self._np = np
        self._lock = threading.Lock()
        self.tokenizer = BertTokenizer.from_pretrained(str(self.directory))
        options = ort.SessionOptions()
        if threads is not None:
            if threads < 1:
                raise ValueError("threads must be positive")
            options.intra_op_num_threads = threads
        try:
            options.execution_mode = (
                ort.ExecutionMode.ORT_PARALLEL
                if threads_affinity == "parallel"
                else ort.ExecutionMode.ORT_SEQUENTIAL
            )
        except AttributeError:  # pragma: no cover - older onnxruntime
            pass
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {item.name for item in self.session.get_inputs()}

    def predict_proba(self, text: str) -> tuple[float, ...]:
        return self.predict_proba_many([text])[0]

    def predict_proba_many(
        self, texts: Sequence[str], *, batch_size: int = 32
    ) -> list[tuple[float, ...]]:
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return []
        cleaned = [_validate_text(text) for text in texts]
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        np = self._np
        output: list[tuple[float, ...]] = []
        with self._lock:
            for start in range(0, len(cleaned), batch_size):
                batch = cleaned[start : start + batch_size]
                encoded = self.tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="np",
                )
                feed = {
                    key: np.asarray(value, dtype=np.int64)
                    for key, value in encoded.items()
                    if key in self.input_names
                }
                logits = self.session.run(None, feed)[0]
                if np.asarray(logits).ndim == 1:
                    logits = np.asarray(logits).reshape(1, -1)
                for row in np.asarray(logits, dtype=np.float64).tolist():
                    output.append(tuple(softmax(row)))
        return output

    def predict(self, text: str, thresholds: Thresholds | None = None) -> SceneDecision:
        return decide(self.predict_proba(text), thresholds)

    def warmup(self) -> None:
        self.predict_proba("你好，今天过得怎么样？")


class FakePredictor:
    """Deterministic predictor used by tests and dry runs."""

    backend = "fake"

    def __init__(
        self,
        scores: Sequence[float] | Callable[[str], Sequence[float]] = (0.7, 0.1, 0.1, 0.1),
        *,
        version: str = "fake",
        model_name: str = "fake",
    ) -> None:
        self._scores = scores
        self.version = version
        self.model_name = model_name
        self.calls: list[str] = []

    def _row(self, text: str) -> tuple[float, ...]:
        values = self._scores(text) if callable(self._scores) else self._scores
        row = tuple(float(value) for value in values)
        if len(row) != len(LABELS):
            raise ValueError(f"scores must contain {len(LABELS)} values")
        return row

    def predict_proba(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        return self._row(text)

    def predict_proba_many(
        self, texts: Sequence[str], *, batch_size: int = 32
    ) -> list[tuple[float, ...]]:
        return [self.predict_proba(text) for text in texts]

    def predict(self, text: str, thresholds: Thresholds | None = None) -> SceneDecision:
        return decide(self.predict_proba(text), thresholds)

    def warmup(self) -> None:
        return None


def build_predictor(
    artifact_dir: str | Path,
    *,
    backend: str = "auto",
    max_length: int = 128,
    threads: int | None = None,
    prefer_int8: bool = True,
) -> Predictor:
    if backend not in ("auto", "torch", "onnx"):
        raise ValueError("backend must be one of auto, torch, onnx")
    directory = Path(artifact_dir)
    if backend == "torch":
        return TorchPredictor(directory, max_length=max_length, threads=threads)
    if backend == "onnx":
        return OnnxPredictor(
            directory, max_length=max_length, threads=threads, prefer_int8=prefer_int8
        )
    onnx_available = importlib.util.find_spec("onnxruntime") is not None
    has_onnx = (directory / "model.onnx").exists() or (directory / "model.int8.onnx").exists()
    if onnx_available and has_onnx:
        return OnnxPredictor(
            directory, max_length=max_length, threads=threads, prefer_int8=prefer_int8
        )
    return TorchPredictor(directory, max_length=max_length, threads=threads)


def load_thresholds(artifact_dir: str | Path, path: str | Path | None = None) -> Thresholds:
    directory = Path(artifact_dir)
    candidate = Path(path) if path is not None else directory / THRESHOLDS_NAME
    return read_thresholds(candidate) if candidate.exists() else Thresholds()


def read_predictions(path: str | Path) -> list[dict[str, Any]]:
    """Load the JSONL written by the evaluation script."""

    from .dataset import read_jsonl

    return read_jsonl(path)


def manifest_for(artifact_dir: str | Path) -> dict[str, Any]:
    path = Path(artifact_dir) / MANIFEST_NAME
    return read_manifest(path) if path.exists() else {}


def _validate_text(text: object) -> str:
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    text = text.strip()
    if not text:
        raise ValueError("text must not be empty")
    if len(text) > 100_000:
        raise ValueError("text is too long for the scene classifier")
    return text
