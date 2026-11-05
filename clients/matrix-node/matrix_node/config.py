"""Validate Matrix node configuration: bounded explicit endpoints and mapping."""

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
MAX_ROOMS = 64
MAX_SENDERS = 64


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


def safe_url(value: Any, *, server: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ConfigError("url_invalid")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ConfigError("url_invalid")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigError("url_invalid")
    if server and (parts.query or parts.fragment):
        raise ConfigError("url_invalid")
    if not server and parts.path not in {"", "/"}:
        raise ConfigError("url_invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class RoomBinding:
    room_id: str
    companion_id: str
    senders: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatrixConfig:
    server_url: str
    token_env: str
    node_id: str
    label: str
    homeserver: str
    user_id: str
    auth_method: str
    password: str | None
    access_token: str | None
    device_name: str
    rooms: tuple[RoomBinding, ...]
    interval_seconds: float
    sync_timeout_ms: int
    request_timeout_seconds: float
    max_history: int
    max_output_tokens: int
    screen_observer_node: str | None
    maintain_node: bool
    send_interval_min_seconds: float
    send_interval_jitter_seconds: float
    typing_before_send: bool
    typing_min_seconds: float
    typing_chars_per_second: float
    send_max_retries: int
    retry_backoff_initial_seconds: float
    merge_on_limit: bool
    max_messages_per_reply: int
    e2ee: bool
    e2ee_sidecar: str | None
    e2ee_config: dict[str, Any] | None


def _parse_rooms(value: Any) -> tuple[RoomBinding, ...]:
    if not isinstance(value, list) or len(value) > MAX_ROOMS:
        raise ConfigError("rooms_out_of_bounds")
    bindings: list[RoomBinding] = []
    seen_rooms: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("rooms_item_invalid")
        entry = object_fields(
            item, allowed={"room", "companion", "senders"}, required={"room", "companion"}
        )
        room = text(entry["room"], limit=512)
        companion = text(entry["companion"], limit=128)
        if room in seen_rooms:
            raise ConfigError("rooms_duplicate")
        seen_rooms.add(room)
        senders = _parse_senders(entry.get("senders"))
        bindings.append(RoomBinding(room_id=room, companion_id=companion, senders=senders))
    return tuple(bindings)


def _parse_senders(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_SENDERS:
        raise ConfigError("senders_out_of_bounds")
    senders: list[str] = []
    seen: set[str] = set()
    for item in value:
        sender = text(item, limit=512)
        if sender in seen:
            raise ConfigError("senders_duplicate")
        seen.add(sender)
        senders.append(sender)
    return tuple(senders)


def load_config(path: Path) -> MatrixConfig:
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
        "homeserver",
        "user_id",
        "auth_method",
        "password",
        "access_token",
        "device_name",
        "rooms",
        "interval_seconds",
        "sync_timeout_ms",
        "request_timeout_seconds",
        "max_history",
        "max_output_tokens",
        "screen_observer_node",
        "maintain_node",
        "send_interval_min_seconds",
        "send_interval_jitter_seconds",
        "typing_before_send",
        "typing_min_seconds",
        "typing_chars_per_second",
        "send_max_retries",
        "retry_backoff_initial_seconds",
        "merge_on_limit",
        "max_messages_per_reply",
        "e2ee",
        "e2ee_sidecar",
        "e2ee_config",
    }
    data = object_fields(
        source,
        allowed=allowed,
        required={"server_url", "node_id", "label", "homeserver", "user_id", "auth_method"},
    )
    token_env = text(data.get("token_env", "VIRTUAL_LIFE_API_TOKEN"), limit=128)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ConfigError("token_env_invalid")
    auth_method = choice(data["auth_method"], {"password", "token"})
    password = data.get("password")
    if password is not None and not isinstance(password, str):
        raise ConfigError("password_invalid")
    access_token = data.get("access_token")
    if access_token is not None and not isinstance(access_token, str):
        raise ConfigError("access_token_invalid")
    if auth_method == "password" and not password:
        raise ConfigError("password_required_for_password_auth")
    if auth_method == "token" and not access_token:
        raise ConfigError("access_token_required_for_token_auth")
    rooms = _parse_rooms(data.get("rooms", []))
    e2ee = data.get("e2ee", False)
    if not isinstance(e2ee, bool):
        raise ConfigError("e2ee_invalid")
    e2ee_sidecar = data.get("e2ee_sidecar")
    if e2ee_sidecar is not None:
        e2ee_sidecar = text(e2ee_sidecar, limit=512)
    maintain_node = data.get("maintain_node", True)
    if not isinstance(maintain_node, bool):
        raise ConfigError("maintain_node_invalid")
    typing_before_send = data.get("typing_before_send", True)
    if not isinstance(typing_before_send, bool):
        raise ConfigError("typing_before_send_invalid")
    merge_on_limit = data.get("merge_on_limit", True)
    if not isinstance(merge_on_limit, bool):
        raise ConfigError("merge_on_limit_invalid")
    e2ee_config = data.get("e2ee_config")
    if e2ee_config is not None:
        object_fields(
            e2ee_config,
            allowed={
                "baseUrl",
                "userId",
                "accessToken",
                "deviceId",
                "password",
                "deviceName",
                "syncTimeoutMs",
                "bootstrapCrossSigning",
                "verification",
                "autoVerify",
            },
            required=set(),
        )
        for key in ("baseUrl", "userId", "accessToken", "password", "deviceName"):
            if key in e2ee_config and not isinstance(e2ee_config[key], str):
                raise ConfigError("e2ee_config_invalid")
    return MatrixConfig(
        server_url=safe_url(data["server_url"], server=True),
        token_env=token_env,
        node_id=text(data["node_id"], limit=128),
        label=text(data["label"], limit=256),
        homeserver=safe_url(data["homeserver"]),
        user_id=text(data["user_id"], limit=512),
        auth_method=auth_method,
        password=password,
        access_token=access_token,
        device_name=text(data.get("device_name", "virtual-life-matrix-node"), limit=128),
        rooms=rooms,
        interval_seconds=number(data.get("interval_seconds", 30), minimum=1, maximum=3600),
        sync_timeout_ms=int(
            number(data.get("sync_timeout_ms", 30000), minimum=1000, maximum=60000)
        ),
        request_timeout_seconds=number(
            data.get("request_timeout_seconds", 40), minimum=0.1, maximum=90
        ),
        max_history=int(number(data.get("max_history", 6), minimum=0, maximum=20)),
        max_output_tokens=int(number(data.get("max_output_tokens", 512), minimum=1, maximum=1024)),
        screen_observer_node=(
            text(data["screen_observer_node"], limit=128)
            if data.get("screen_observer_node") is not None
            else None
        ),
        maintain_node=maintain_node,
        send_interval_min_seconds=number(
            data.get("send_interval_min_seconds", 0.8), minimum=0, maximum=60
        ),
        send_interval_jitter_seconds=number(
            data.get("send_interval_jitter_seconds", 1.2), minimum=0, maximum=60
        ),
        typing_before_send=typing_before_send,
        typing_min_seconds=number(data.get("typing_min_seconds", 0.5), minimum=0.1, maximum=60),
        typing_chars_per_second=number(
            data.get("typing_chars_per_second", 12), minimum=0.5, maximum=100
        ),
        send_max_retries=int(number(data.get("send_max_retries", 3), minimum=0, maximum=10)),
        retry_backoff_initial_seconds=number(
            data.get("retry_backoff_initial_seconds", 2), minimum=0.1, maximum=60
        ),
        merge_on_limit=merge_on_limit,
        max_messages_per_reply=int(
            number(data.get("max_messages_per_reply", 3), minimum=1, maximum=20)
        ),
        e2ee=e2ee,
        e2ee_sidecar=e2ee_sidecar,
        e2ee_config=e2ee_config,
    )


def read_token(config: MatrixConfig) -> str:
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
