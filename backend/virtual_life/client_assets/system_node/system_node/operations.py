"""Accept only short, explicit operation metadata from a local file."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ConfigError, integer, object_fields, text
from .json_utils import JsonError, read_json

LOGGER = logging.getLogger(__name__)
MAX_OPERATION_BYTES = 4096
OPERATION_FIELDS = {
    "operation_id",
    "subject_node_id",
    "origin_node_id",
    "kind",
    "phase",
    "label",
    "updated_at",
    "ttl_seconds",
    "user_initiated",
}
OPERATION_KINDS = {"maintenance", "deployment", "backup", "network_change", "other"}
OPERATION_PHASES = {"started", "progress", "completed", "failed", "cancelled"}


def utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OperationReader:
    def __init__(self, path: Path | None, managed_ids: set[str]):
        self._path = path
        self._managed_ids = managed_ids
        self._last_error: str | None = None

    def read(self, now: datetime) -> dict[str, Any] | None:
        if self._path is None:
            return None
        try:
            data = read_json(self._path, max_bytes=MAX_OPERATION_BYTES, max_depth=2)
            result = self._validate(data, now)
        except FileNotFoundError:
            self._error("operation_missing")
            return None
        except OSError:
            self._error("operation_unreadable")
            return None
        except (ConfigError, JsonError) as exc:
            self._error(f"operation_{exc}")
            return None
        self._last_error = None
        return result

    def _error(self, code: str) -> None:
        if code != self._last_error:
            LOGGER.warning("operation_unavailable reason=%s", code)
            self._last_error = code

    def _validate(self, value: Any, now: datetime) -> dict[str, Any]:
        data = object_fields(value, allowed=OPERATION_FIELDS, required=OPERATION_FIELDS)
        result = {
            "operation_id": text(data["operation_id"], limit=128),
            "subject_node_id": text(data["subject_node_id"], limit=128, node_id=True),
            "origin_node_id": text(data["origin_node_id"], limit=128, node_id=True),
            "label": text(data["label"], limit=256),
            "ttl_seconds": integer(data["ttl_seconds"], minimum=1, maximum=86400),
        }
        if result["subject_node_id"] not in self._managed_ids:
            raise ConfigError("subject_unmanaged")
        if not isinstance(data["kind"], str) or data["kind"] not in OPERATION_KINDS:
            raise ConfigError("kind_invalid")
        if not isinstance(data["phase"], str) or data["phase"] not in OPERATION_PHASES:
            raise ConfigError("phase_invalid")
        if type(data["user_initiated"]) is not bool:
            raise ConfigError("user_initiated_invalid")
        timestamp = text(data["updated_at"], limit=64)
        try:
            updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if updated.tzinfo is None or updated.utcoffset().total_seconds() != 0:
                raise ValueError("not_utc")
            age = (now - updated).total_seconds()
        except (ValueError, OverflowError) as exc:
            raise ConfigError("timestamp_invalid") from exc
        if age < 0:
            raise ConfigError("timestamp_future")
        if age >= result["ttl_seconds"]:
            raise ConfigError("expired")
        result.update(
            kind=data["kind"],
            phase=data["phase"],
            updated_at=utc_timestamp(updated),
            user_initiated=data["user_initiated"],
        )
        return result
