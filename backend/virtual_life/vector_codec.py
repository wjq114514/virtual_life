"""Compact vector storage and batched cosine scoring.

Vectors are persisted as little-endian float32 BLOBs.  The previous JSON
representation is still accepted on read so existing databases can be migrated
without a blocking rewrite.  ``numpy`` is used when available; the pure Python
fallback keeps the runtime usable without optional acceleration.
"""

from __future__ import annotations

import math
import sys
from array import array
from collections.abc import Iterable, Sequence

try:  # Optional acceleration for the local cosine scan.
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without numpy installed
    np = None

VECTOR_DTYPE = "<f4"
VECTOR_ITEMSIZE = 4


def normalize_vector(values: Iterable[float]) -> list[float]:
    """Validate and unit-normalize a dense vector."""
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("Vector must be a nonempty list")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("Vector values must be finite numbers")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Vector must have a finite nonzero norm")
    return [value / norm for value in values]


def encode_vector(values: Iterable[float]) -> bytes:
    """Return a normalized little-endian float32 BLOB."""
    normalized = normalize_vector(values)
    packed = array("f", normalized)
    if packed.itemsize != VECTOR_ITEMSIZE:  # pragma: no cover - platform guard
        raise ValueError("float32 is not available on this platform")
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


def vector_dimensions(blob: bytes | bytearray | memoryview) -> int:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("Stored vector must be bytes")
    if len(blob) % VECTOR_ITEMSIZE:
        raise ValueError("Stored vector has an invalid byte length")
    return len(blob) // VECTOR_ITEMSIZE


def decode_vector(blob: bytes | bytearray | memoryview, dimensions: int | None = None):
    """Decode a BLOB into a numpy array or ``array('f')`` without re-normalizing."""
    count = vector_dimensions(blob)
    if dimensions is not None and count != dimensions:
        raise ValueError("Stored vector dimensions changed")
    if np is not None:
        return np.frombuffer(blob, dtype=VECTOR_DTYPE)
    decoded = array("f")
    decoded.frombytes(bytes(blob))
    if sys.byteorder != "little":
        decoded.byteswap()
    return decoded


def cosine_scores(query, vectors: Sequence) -> list[float]:
    """Compute cosine scores for already normalized vectors."""
    if not vectors:
        return []
    if np is not None:
        matrix = np.stack([np.asarray(vector, dtype=np.float32) for vector in vectors])
        query_array = np.asarray(query, dtype=np.float32)
        scores = matrix @ query_array
        return [float(score) for score in scores]
    return [
        float(sum(left * right for left, right in zip(query, vector, strict=True)))
        for vector in vectors
    ]


def cosine_scores_from_blobs(query, blobs: Sequence[bytes], dimensions: int) -> list[float]:
    """Fast path for a homogeneous set of stored BLOBs."""
    if not blobs:
        return []
    if np is not None:
        matrix = np.frombuffer(b"".join(blobs), dtype=VECTOR_DTYPE).reshape(len(blobs), dimensions)
        query_array = np.asarray(query, dtype=np.float32)
        scores = matrix @ query_array
        return [float(score) for score in scores]
    return cosine_scores(query, [decode_vector(blob, dimensions) for blob in blobs])
