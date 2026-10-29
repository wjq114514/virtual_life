"""Strict JSON handling shared by the Matrix node (mirrors qq-node's json_utils)."""

import json
from typing import Any

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 131_072
MAX_JSON_DEPTH = 32


class JsonError(ValueError):
    """Only a stable reason code is exposed to callers."""


def _check_encoded_depth(raw: bytes, limit: int) -> None:
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


def decode_json(raw: bytes, *, max_bytes: int, max_depth: int) -> Any:
    if len(raw) > max_bytes:
        raise JsonError("json_too_large")
    _check_encoded_depth(raw, max_depth)
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise JsonError("json_invalid") from exc


def encode_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise JsonError("json_invalid") from exc


def read_json(path, *, max_bytes: int, max_depth: int) -> Any:
    data = path.read_bytes()
    return decode_json(data, max_bytes=max_bytes, max_depth=max_depth)
