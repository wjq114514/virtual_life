"""Validate explicit endpoints and bounded local configuration."""

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .json_utils import JsonError, read_json

MAX_CONFIG_BYTES = 65_536
MAX_TARGETS = 64
DEVICE_CLASSES = {"server", "workstation", "gateway", "router", "nas", "other"}


class ConfigError(ValueError):
    pass


def text(value: Any, *, limit: int, node_id: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError("string_required")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError("string_control_character")
    value = value.strip()
    if not value or len(value) > limit:
        raise ConfigError("string_length_invalid")
    if node_id and ("/" in value or "\\" in value or value in {".", ".."}):
        raise ConfigError("node_id_path_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ConfigError("string_encoding_invalid") from exc
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


def integer(value: Any, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise ConfigError("integer_required")
    if not minimum <= value <= maximum:
        raise ConfigError("integer_out_of_bounds")
    return value


def object_fields(value: Any, *, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("object_required")
    if value.keys() - allowed:
        raise ConfigError("unknown_field")
    if required - value.keys():
        raise ConfigError("required_field_missing")
    return value


def choice(value: Any, options: set[str]) -> str:
    if not isinstance(value, str) or value not in options:
        raise ConfigError("choice_invalid")
    return value


def safe_url(value: Any, *, server: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ConfigError("url_invalid")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ConfigError("url_control_character")
    if "\\" in value or "#" in value:
        raise ConfigError("url_fragment_or_backslash")
    try:
        value.encode("ascii")
        parsed = urlsplit(value)
        port = parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError("url_invalid")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ConfigError("url_credentials_forbidden")
        if "%" in parsed.netloc or (port is not None and not 1 <= port <= 65535):
            raise ConfigError("url_host_invalid")
        parameters = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=64)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("url_invalid") from exc
    sensitive_names = {
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "apitoken",
        "key",
        "auth",
        "authorization",
        "bearer",
        "password",
        "secret",
        "credential",
        "credentials",
        "signature",
        "sig",
        "sessionid",
        "subscriptionkey",
    }
    for key, _ in parameters:
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if normalized in sensitive_names or normalized.endswith(
            ("token", "apikey", "password", "secret", "credential", "signature")
        ):
            raise ConfigError("url_sensitive_query_forbidden")
    if server and ("?" in value or parsed.path not in {"", "/"}):
        raise ConfigError("server_url_must_be_origin")
    return value.rstrip("/") if server else value


def local_path(value: Any, directory: Path) -> Path:
    path = Path(text(value, limit=4096)).expanduser()
    return (path if path.is_absolute() else directory / path).resolve()


@dataclass(frozen=True)
class Node:
    node_id: str
    label: str
    device_class: str
    adapter: str = "local"
    host: str | None = None
    port: int | None = None
    url: str | None = None
    timeout_seconds: float = 3.0


@dataclass(frozen=True)
class AttentionConfig:
    enabled: bool = False
    session_id: str | None = None


@dataclass(frozen=True)
class Config:
    server_url: str
    token_env: str
    node: Node
    deployment_mode: str
    disk_path: Path
    operation_file: Path | None
    targets: tuple[Node, ...]
    interval_seconds: float
    ttl_seconds: int
    request_timeout_seconds: float
    observation_min_interval_seconds: float
    pending_observations_limit: int
    backoff_initial_seconds: float
    backoff_max_seconds: float
    attention: AttentionConfig = AttentionConfig()

    @property
    def managed_nodes(self) -> tuple[Node, ...]:
        return (self.node, *self.targets)

    @property
    def observation_budget_seconds(self) -> float:
        return min(self.interval_seconds / 4, self.request_timeout_seconds)

    @property
    def reporting_budget_seconds(self) -> float:
        return (
            self.interval_seconds
            + sum(target.timeout_seconds for target in self.targets)
            + (len(self.managed_nodes) + 1) * self.request_timeout_seconds
            + self.observation_budget_seconds
            + (2.0 + self.request_timeout_seconds if self.attention.enabled else 0)
        )


def _target(value: Any, default_timeout: float) -> Node:
    common = {"node_id", "label", "device_class", "adapter", "timeout_seconds"}
    target = object_fields(
        value,
        allowed=common | {"host", "port", "url"},
        required={"node_id", "label", "adapter"},
    )
    adapter = choice(target["adapter"], {"tcp", "http"})
    host = None
    port = None
    url = None
    if adapter == "tcp":
        if "url" in target or "host" not in target or "port" not in target:
            raise ConfigError("tcp_fields_invalid")
        host = text(target["host"], limit=253)
        if any(character.isspace() or character in "/?#@\\%" for character in host):
            raise ConfigError("tcp_host_invalid")
        port = integer(target["port"], minimum=1, maximum=65535)
    else:
        if "host" in target or "port" in target or "url" not in target:
            raise ConfigError("http_fields_invalid")
        url = safe_url(target["url"])
    return Node(
        node_id=text(target["node_id"], limit=128, node_id=True),
        label=text(target["label"], limit=256),
        device_class=choice(target.get("device_class", "other"), DEVICE_CLASSES),
        adapter=adapter,
        host=host,
        port=port,
        url=url,
        timeout_seconds=number(
            target.get("timeout_seconds", default_timeout), minimum=0.1, maximum=30
        ),
    )


def load_config(path: Path) -> Config:
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
        "device_class",
        "deployment_mode",
        "disk_path",
        "operation_file",
        "targets",
        "interval_seconds",
        "ttl_seconds",
        "request_timeout_seconds",
        "probe_timeout_seconds",
        "observation_min_interval_seconds",
        "pending_observations_limit",
        "backoff_initial_seconds",
        "backoff_max_seconds",
        "attention",
    }
    data = object_fields(source, allowed=allowed, required={"server_url", "node_id", "label"})
    mode = choice(data.get("deployment_mode", "direct"), {"direct", "gateway"})
    node = Node(
        node_id=text(data["node_id"], limit=128, node_id=True),
        label=text(data["label"], limit=256),
        device_class=choice(data.get("device_class", "other"), DEVICE_CLASSES),
    )
    raw_targets = data.get("targets", [])
    if not isinstance(raw_targets, list) or len(raw_targets) > MAX_TARGETS:
        raise ConfigError("targets_out_of_bounds")
    if raw_targets and mode != "gateway":
        raise ConfigError("targets_require_gateway")
    probe_timeout = number(data.get("probe_timeout_seconds", 3), minimum=0.1, maximum=30)
    targets = tuple(_target(target, probe_timeout) for target in raw_targets)
    ids = [node.node_id, *(target.node_id for target in targets)]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate_node_id")
    token_env = text(data.get("token_env", "VIRTUAL_LIFE_API_TOKEN"), limit=128)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ConfigError("token_env_invalid")
    initial = number(data.get("backoff_initial_seconds", 1), minimum=0.1, maximum=3600)
    maximum = number(data.get("backoff_max_seconds", 60), minimum=0.1, maximum=3600)
    if initial > maximum:
        raise ConfigError("backoff_order_invalid")
    operation_file = data.get("operation_file")
    try:
        directory = path.resolve().parent
        disk_path = local_path(data.get("disk_path", "/"), directory)
        operation_path = (
            local_path(operation_file, directory) if operation_file is not None else None
        )
    except (OSError, RuntimeError) as exc:
        raise ConfigError("local_path_invalid") from exc
    raw_attention = object_fields(
        data.get("attention", {}), allowed={"enabled", "session_id"}, required=set()
    )
    enabled = raw_attention.get("enabled", False)
    if type(enabled) is not bool:
        raise ConfigError("attention_enabled_boolean_required")
    attention_session = raw_attention.get("session_id")
    if attention_session is not None and (
        not isinstance(attention_session, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", attention_session)
    ):
        raise ConfigError("attention_session_invalid")
    config = Config(
        server_url=safe_url(data["server_url"], server=True),
        token_env=token_env,
        node=node,
        deployment_mode=mode,
        disk_path=disk_path,
        operation_file=operation_path,
        targets=targets,
        interval_seconds=number(data.get("interval_seconds", 20), minimum=1, maximum=3600),
        ttl_seconds=integer(data.get("ttl_seconds", 60), minimum=1, maximum=86400),
        request_timeout_seconds=number(
            data.get("request_timeout_seconds", 5), minimum=0.1, maximum=30
        ),
        observation_min_interval_seconds=number(
            data.get("observation_min_interval_seconds", 60), minimum=1, maximum=86400
        ),
        pending_observations_limit=integer(
            data.get("pending_observations_limit", 128), minimum=1, maximum=4096
        ),
        backoff_initial_seconds=initial,
        backoff_max_seconds=maximum,
        attention=AttentionConfig(enabled, attention_session),
    )
    if enabled and (config.interval_seconds > 30 or config.reporting_budget_seconds >= 90):
        raise ConfigError("attention_reporting_budget_invalid")
    if config.ttl_seconds <= config.reporting_budget_seconds:
        raise ConfigError("ttl_not_above_reporting_budget")
    return config


def read_token(config: Config) -> str:
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
