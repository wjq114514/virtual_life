"""Strict JSON parsing with byte and nesting limits."""

import json
import math
import os
import stat
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 131_072
MAX_JSON_DEPTH = 32


class JsonError(ValueError):
    """Only a stable reason code is exposed to callers."""


def _check_encoded_depth(raw: bytes, limit: int) -> None:
    # Check before json.loads so deeply nested inputs cannot exhaust its recursion.
    depth = 0
    in_string = False
    escaped = False
    for character in raw:
        if in_string:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                in_string = False
        elif character == 34:
            in_string = True
        elif character in (91, 123):
            depth += 1
            if depth > limit:
                raise JsonError("json_too_deep")
        elif character in (93, 125):
            depth -= 1


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonError("json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise JsonError("json_nonfinite")


def _check_value(value: Any, max_depth: int) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > max_depth:
            raise JsonError("json_too_deep")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise JsonError("json_nonfinite")


def decode_json(raw: bytes, *, max_bytes: int, max_depth: int) -> Any:
    if len(raw) > max_bytes:
        raise JsonError("json_too_large")
    _check_encoded_depth(raw, max_depth)
    try:
        result = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except JsonError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise JsonError("json_invalid") from exc
    _check_value(result, max_depth)
    return result


def encode_json(value: dict[str, Any]) -> bytes:
    _check_value(value, MAX_JSON_DEPTH)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise JsonError("json_invalid") from exc
    if len(raw) > MAX_REQUEST_BYTES:
        raise JsonError("json_too_large")
    return raw


def read_json(path: Path, *, max_bytes: int, max_depth: int) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise JsonError("file_not_regular")
        if metadata.st_size > max_bytes:
            raise JsonError("json_too_large")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    return decode_json(raw, max_bytes=max_bytes, max_depth=max_depth)
