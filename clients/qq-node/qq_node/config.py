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


MAX_GROUPS = 16
MAX_GROUP_SENDERS = 64


@dataclass(frozen=True)
class GroupBinding:
    group_id: int
    companion_id: str
    senders: tuple[int, ...] = ()


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
    groups: tuple[GroupBinding, ...]
    test_user_id: int | None
    interval_seconds: float
    request_timeout_seconds: float
    max_history: int
    max_output_tokens: int
    screen_observer_node: str | None
    maintain_node: bool
    send_interval_min_seconds: float
    send_interval_jitter_seconds: float
    send_max_retries: int
    retry_backoff_initial_seconds: float
    merge_on_limit: bool
    max_messages_per_reply: int


def _group_id(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ConfigError("group_id_invalid")
    return value


def _parse_groups(value: Any) -> tuple[GroupBinding, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_GROUPS:
        raise ConfigError("groups_out_of_bounds")
    bindings: list[GroupBinding] = []
    seen: set[int] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("groups_item_invalid")
        entry = object_fields(
            item, allowed={"group_id", "companion", "senders"}, required={"group_id", "companion"}
        )
        group_id = _group_id(entry["group_id"])
        companion = text(entry["companion"], limit=128)
        if group_id in seen:
            raise ConfigError("groups_duplicate")
        seen.add(group_id)
        raw_senders = entry.get("senders", [])
        if not isinstance(raw_senders, list) or len(raw_senders) > MAX_GROUP_SENDERS:
            raise ConfigError("group_senders_out_of_bounds")
        senders: list[int] = []
        seen_senders: set[int] = set()
        for sender in raw_senders:
            sender_id = _group_id(sender)
            if sender_id in seen_senders:
                raise ConfigError("group_senders_duplicate")
            seen_senders.add(sender_id)
            senders.append(sender_id)
        bindings.append(
            GroupBinding(group_id=group_id, companion_id=companion, senders=tuple(senders))
        )
    return tuple(bindings)


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
        "groups",
        "test_user_id",
        "interval_seconds",
        "request_timeout_seconds",
        "max_history",
        "max_output_tokens",
        "screen_observer_node",
        "maintain_node",
        "send_interval_min_seconds",
        "send_interval_jitter_seconds",
        "send_max_retries",
        "retry_backoff_initial_seconds",
        "merge_on_limit",
        "max_messages_per_reply",
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
    groups = _parse_groups(data.get("groups"))
    merge_on_limit = data.get("merge_on_limit", True)
    if not isinstance(merge_on_limit, bool):
        raise ConfigError("merge_on_limit_invalid")
    access_token = data.get("access_token")
    if access_token is not None and not isinstance(access_token, str):
        raise ConfigError("access_token_invalid")
    test_user_id = data.get("test_user_id")
    if test_user_id is not None and (
        type(test_user_id) is not int or not 1 <= test_user_id <= 2**63 - 1
    ):
        raise ConfigError("test_user_id_invalid")
    screen_observer_node = data.get("screen_observer_node")
    if screen_observer_node is not None:
        screen_observer_node = text(screen_observer_node, limit=128)
    maintain_node = data.get("maintain_node", True)
    if not isinstance(maintain_node, bool):
        raise ConfigError("maintain_node_invalid")
    return QQConfig(
        server_url=safe_url(data["server_url"], server=True),
        token_env=token_env,
        node_id=text(data["node_id"], limit=128),
        label=text(data["label"], limit=256),
        websocket_url=safe_url(data["websocket_url"], websocket=True),
        http_url=safe_url(data["http_url"], http=True),
        access_token=access_token,
        companions=companions,
        groups=groups,
        test_user_id=test_user_id,
        interval_seconds=number(data.get("interval_seconds", 30), minimum=1, maximum=3600),
        request_timeout_seconds=number(
            data.get("request_timeout_seconds", 40), minimum=0.1, maximum=90
        ),
        max_history=int(number(data.get("max_history", 6), minimum=0, maximum=20)),
        max_output_tokens=int(number(data.get("max_output_tokens", 512), minimum=1, maximum=1024)),
        screen_observer_node=screen_observer_node,
        maintain_node=maintain_node,
        send_interval_min_seconds=number(
            data.get("send_interval_min_seconds", 0.8), minimum=0, maximum=60
        ),
        send_interval_jitter_seconds=number(
            data.get("send_interval_jitter_seconds", 1.2), minimum=0, maximum=60
        ),
        send_max_retries=int(number(data.get("send_max_retries", 3), minimum=0, maximum=10)),
        retry_backoff_initial_seconds=number(
            data.get("retry_backoff_initial_seconds", 2), minimum=0.1, maximum=60
        ),
        merge_on_limit=merge_on_limit,
        max_messages_per_reply=int(
            number(data.get("max_messages_per_reply", 3), minimum=1, maximum=20)
        ),
        # `typing_before_send` is intentionally absent: OneBot v11 (and NapCat's
        # implementation of it) has no typing/input-status action for personal QQ
        # chats; the only typing signal is the *inbound* `notice_type=typing`
        # event, which this node already reports as an attention sample.
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
