"""Validate QQ node configuration: bounded explicit endpoints and mapping."""

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .json_utils import JsonError, read_json

MAX_CONFIG_BYTES = 65_536
MAX_COMPANIONS = 16


class ConfigError(ValueError):
    pass


def text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ConfigError("string_required")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError("string_control_character")
    value = value.strip()
    if not value or len(value) > limit:
        raise ConfigError("string_length_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ConfigError("string_encoding_invalid") from exc
    return value


def choice(value: Any, options: set[str]) -> str:
    if not isinstance(value, str) or value not in options:
        raise ConfigError("choice_invalid")
    return value


def number(value: Any, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise ConfigError("number_required")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ConfigError("number_out_of_bounds") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ConfigError("number_out_of_bounds")
    return result


def object_fields(value: Any, *, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("object_required")
    if value.keys() - allowed:
        raise ConfigError("unknown_field")
    if required - value.keys():
        raise ConfigError("required_field_missing")
    return value


def safe_url(
    value: Any, *, server: bool = False, websocket: bool = False, http: bool = False
) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ConfigError("url_invalid")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ConfigError("url_invalid")
    parts = urlsplit(value)
    schemes = {"ws", "wss"} if websocket else {"http", "https"}
    if parts.scheme not in schemes or not parts.netloc:
        raise ConfigError("url_invalid")
    if (server or http) and (parts.query or parts.fragment):
        raise ConfigError("url_invalid")
    if not websocket and not server and not http and parts.path not in {"", "/"}:
        raise ConfigError("url_invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class QQConfig:
    server_url: str
    token_env: str
    node_id: str
    label: str
    websocket_url: str
    http_url: str
    access_token: str | None
    companions: tuple[str, ...]
    test_user_id: int | None
    interval_seconds: float
    request_timeout_seconds: float
    max_history: int
    max_output_tokens: int


def load_config(path: Path) -> QQConfig:
    try:
        source = read_json(path, max_bytes=MAX_CONFIG_BYTES, max_depth=8)
    except (OSError, ValueError) as exc:
        if isinstance(exc, JsonError):
            raise ConfigError(str(exc)) from exc
        raise ConfigError("config_unreadable") from exc
    allowed = {
        "server_url",
        "token_env",
        "node_id",
        "label",
        "websocket_url",
        "http_url",
        "access_token",
        "companions",
        "test_user_id",
        "interval_seconds",
        "request_timeout_seconds",
        "max_history",
        "max_output_tokens",
    }
    data = object_fields(
        source,
        allowed=allowed,
        required={"server_url", "node_id", "label", "websocket_url", "http_url"},
    )
    token_env = text(data.get("token_env", "VIRTUAL_LIFE_API_TOKEN"), limit=128)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ConfigError("token_env_invalid")
    raw_companions = data.get("companions", [])
    if not isinstance(raw_companions, list) or len(raw_companions) > MAX_COMPANIONS:
        raise ConfigError("companions_out_of_bounds")
    companions = tuple(text(item, limit=128) for item in raw_companions)
    access_token = data.get("access_token")
    if access_token is not None and not isinstance(access_token, str):
        raise ConfigError("access_token_invalid")
    test_user_id = data.get("test_user_id")
    if test_user_id is not None and (
        type(test_user_id) is not int or not 1 <= test_user_id <= 2**63 - 1
    ):
        raise ConfigError("test_user_id_invalid")
    return QQConfig(
        server_url=safe_url(data["server_url"], server=True),
        token_env=token_env,
        node_id=text(data["node_id"], limit=128),
        label=text(data["label"], limit=256),
        websocket_url=safe_url(data["websocket_url"], websocket=True),
        http_url=safe_url(data["http_url"], http=True),
        access_token=access_token,
        companions=companions,
        test_user_id=test_user_id,
        interval_seconds=number(data.get("interval_seconds", 30), minimum=1, maximum=3600),
        request_timeout_seconds=number(
            data.get("request_timeout_seconds", 10), minimum=0.1, maximum=60
        ),
        max_history=int(number(data.get("max_history", 6), minimum=0, maximum=20)),
        max_output_tokens=int(number(data.get("max_output_tokens", 512), minimum=1, maximum=1024)),
    )


def read_token(config: QQConfig) -> str:
    token = os.environ.get(config.token_env)
    if not token or len(token) > 4096:
        raise ConfigError("token_missing_or_length_invalid")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in token
    ):
        raise ConfigError("token_control_character")
    try:
        token.encode("ascii")
    except UnicodeError as exc:
        raise ConfigError("token_encoding_invalid") from exc
    return token
