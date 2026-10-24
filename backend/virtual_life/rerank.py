"""Bounded, fail-open reranking on top of local retrieval fusion."""

import math
import threading
import time

from .model_provider import ProviderError
from .ports import RerankRequest


class RerankService:
    """Rerank only the head of an existing fusion result.

    The embedding scan and the lexical/graph fusion already produce a useful
    ordering.  A CPU reranker is allowed to improve that ordering only when it
    can finish inside the configured timeout; otherwise the original rows are
    returned unchanged.  This service is used by explicit semantic recall, not
    by ordinary recall or the interactive dialogue path.
    """

    def __init__(
        self,
        provider=None,
        *,
        model_name="",
        enabled=False,
        candidate_limit=8,
        min_candidates=4,
        rrf_weight=0.15,
        timeout_seconds=0.5,
        failure_cooldown_seconds=30.0,
    ):
        if type(candidate_limit) is not int or not 1 <= candidate_limit <= 64:
            raise ValueError("Invalid rerank candidate limit")
        if type(min_candidates) is not int or not 1 <= min_candidates <= candidate_limit:
            raise ValueError("Invalid rerank minimum candidates")
        if (
            isinstance(rrf_weight, bool)
            or not isinstance(rrf_weight, (int, float))
            or not math.isfinite(rrf_weight)
            or not 0 <= rrf_weight <= 1
        ):
            raise ValueError("Invalid rerank fusion weight")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Invalid rerank timeout")
        if (
            isinstance(failure_cooldown_seconds, bool)
            or not math.isfinite(failure_cooldown_seconds)
            or failure_cooldown_seconds < 0
        ):
            raise ValueError("Invalid rerank cooldown")
        self.provider = provider
        self.model_name = model_name
        self.enabled = bool(enabled)
        self.candidate_limit = candidate_limit
        self.min_candidates = min_candidates
        self.rrf_weight = float(rrf_weight)
        self.timeout_seconds = float(timeout_seconds)
        self.failure_cooldown_seconds = float(failure_cooldown_seconds)
        self._failures = 0
        self._skip_until = 0.0
        self._state_lock = threading.Lock()

    def _circuit_open(self):
        with self._state_lock:
            return time.monotonic() < self._skip_until

    def _record_failure(self):
        with self._state_lock:
            self._failures += 1
            if self._failures >= 2:
                self._skip_until = time.monotonic() + self.failure_cooldown_seconds

    def _record_success(self):
        with self._state_lock:
            self._failures = 0
            self._skip_until = 0.0

    def rerank(self, companion_id, query, rows):
        """Return ``(reordered_rows, diagnostics)`` without raising provider errors."""
        info = {
            "enabled": self.enabled,
            "applied": False,
            "candidates": 0,
            "latency_ms": 0.0,
            "error_code": None,
        }
        if not self.enabled:
            return rows, info
        if self.provider is None or not self.model_name.strip():
            info["error_code"] = "rerank_not_configured"
            return rows, info
        if not query.strip() or len(rows) < self.min_candidates:
            return rows, info
        if self._circuit_open():
            info["error_code"] = "rerank_circuit_open"
            return rows, info

        candidates = list(rows[: self.candidate_limit])
        info["candidates"] = len(candidates)
        if len(candidates) < self.min_candidates:
            return rows, info
        started = time.perf_counter()
        try:
            result = self.provider.rerank(
                RerankRequest(
                    "rerank",
                    companion_id,
                    self.model_name,
                    query,
                    [row["content"] for row in candidates],
                )
            )
            scores = list(result.scores)
            if len(scores) != len(candidates) or any(
                type(score) not in (int, float) or not math.isfinite(score) for score in scores
            ):
                raise ProviderError("rerank_invalid_scores", "Rerank scores are invalid")
        except ProviderError as error:
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            info["error_code"] = error.code
            self._record_failure()
            return rows, info
        except Exception:
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            info["error_code"] = "rerank_failed"
            self._record_failure()
            return rows, info

        info["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        low, high = min(scores), max(scores)
        span = high - low
        count = len(candidates)
        if span <= 1e-12:
            normalized = [0.5] * count
        else:
            normalized = [(score - low) / span for score in scores]
        base = [1.0 - index / (count - 1) if count > 1 else 1.0 for index in range(count)]
        combined = [
            (1.0 - self.rrf_weight) * normalized[index] + self.rrf_weight * base[index]
            for index in range(count)
        ]
        order = sorted(range(count), key=lambda index: (-combined[index], index))
        reordered = [candidates[index] for index in order] + list(rows[count:])
        info["applied"] = True
        self._record_success()
        return reordered, info

    def status(self):
        with self._state_lock:
            remaining = max(0.0, self._skip_until - time.monotonic())
            return {
                "enabled": self.enabled,
                "configured": self.provider is not None and bool(self.model_name.strip()),
                "model": self.model_name or None,
                "candidate_limit": self.candidate_limit,
                "failures": self._failures,
                "circuit_open_seconds": round(remaining, 3),
            }
