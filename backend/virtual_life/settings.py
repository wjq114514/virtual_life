"""Validated TOML configuration with explicit environment overrides."""

import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import get_args
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from .delegation import DelegationConfig
from .emotion import EmotionPolicy
from .inner_life import InnerLifePolicy
from .memory import MemoryDecayPolicy, MemoryPolicy
from .model_provider import PROTECTED_BODY_FIELDS, CompatibleEmbeddingProvider
from .model_routing import RoutingConfig, routing_config
from .models import JsonObject, bounded_json
from .personality_evolution import EvolutionPolicy
from .personality_prompt import PersonalityPolicy
from .proactivity import SchedulePolicy
from .scene_config import SceneClassifierConfig, scene_classifier_config
from .screen_config import ScreenVisionConfig
from .slow_variables import SlowVariablePolicy
from .voice_config import SpeechConfig, VoiceConfig, speech_config, voice_config

DEFAULT_CONFIG_FILE = "virtual-life.toml"
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_SCREEN_VISION_FIELDS = {item.name: item for item in fields(ScreenVisionConfig)}
_SCREEN_VISION_KEYS = {name: name for name in _SCREEN_VISION_FIELDS if name != "model_name"}
_SCREEN_VISION_KEYS["name"] = "model_name"
_SPEECH_FIELDS = {item.name: item for item in fields(SpeechConfig)}
_VOICE_FIELDS = {item.name: item for item in fields(VoiceConfig)}
_ROUTING_FIELDS = {item.name: item for item in fields(RoutingConfig)}
_SCENE_FIELDS = {item.name: item for item in fields(SceneClassifierConfig)}
_CONFIG_SECTIONS = {
    "server": {"host": "server_host", "port": "server_port"},
    "logging": {
        "level": "log_level",
        "console": "log_console",
        "file_enabled": "log_file_enabled",
        "file_path": "log_file_path",
        "max_mb": "log_file_max_mb",
        "backup_count": "log_file_backup_count",
        "access": "log_access",
    },
    "runtime": {
        "database_path": "database_path",
        "api_token": "api_token",
        "node_ttl_seconds": "node_ttl_seconds",
        "lease_ttl_seconds": "lease_ttl_seconds",
        "transfer_ttl_seconds": "transfer_ttl_seconds",
        "auto_follow_rooms": "auto_follow_rooms",
        "auto_follow_quiet_seconds": "auto_follow_quiet_seconds",
        "event_poll_seconds": "event_poll_seconds",
        "reconcile_seconds": "reconcile_seconds",
        "websocket_auth_timeout_seconds": "websocket_auth_timeout_seconds",
        "webui_session_days": "webui_session_days",
    },
    "model": {
        "base_url": "model_base_url",
        "name": "model_name",
        "api_key": "model_api_key",
        "timeout_seconds": "model_timeout_seconds",
        "extra_body": "model_extra_body",
    },
    "automatic_memory": {
        "enabled": "auto_memory_enabled",
        "batch_size": "auto_memory_batch_size",
        "max_wait_seconds": "auto_memory_max_wait_seconds",
        "poll_seconds": "auto_memory_poll_seconds",
        "output_tokens": "auto_memory_output_tokens",
    },
    "proactivity": {"poll_seconds": "proactivity_poll_seconds"},
    "inner_life": {"poll_seconds": "inner_life_poll_seconds"},
    "dialogue": {"debounce_seconds": "dialogue_debounce_seconds"},
    "tools": {
        "enabled": "tools_enabled",
        "max_calls_per_turn": "tools_max_calls_per_turn",
        "plugins": "tools_plugins",
    },
    "delegation": {
        "enabled": "enabled",
        "command": "command",
        "working_dir": "working_dir",
        "timeout_seconds": "timeout_seconds",
        "max_output_chars": "max_output_chars",
        "env_allowlist": "env_allowlist",
    },
    "proactive_schedule": {
        "min_delay_seconds": "min_delay_seconds",
        "max_delay_seconds": "max_delay_seconds",
        "min_idle_seconds": "min_idle_seconds",
    },
    "embedding": {
        "enabled": "embedding_enabled",
        "base_url": "embedding_base_url",
        "name": "embedding_model_name",
        "api_key": "embedding_api_key",
        "timeout_seconds": "embedding_timeout_seconds",
        "dimensions": "embedding_dimensions",
        "extra_body": "embedding_extra_body",
        "batch_size": "embedding_batch_size",
        "max_input_tokens": "embedding_max_input_tokens",
        "poll_seconds": "embedding_poll_seconds",
        "candidate_limit": "embedding_candidate_limit",
        "similarity_threshold": "embedding_similarity_threshold",
        "query_cache_size": "embedding_query_cache_size",
        "query_cache_ttl_seconds": "embedding_query_cache_ttl_seconds",
        "dialogue_recall": "embedding_dialogue_recall",
        "space_identity": "embedding_space_identity",
        "local": "embedding_local",
    },
    "rerank": {
        "enabled": "rerank_enabled",
        "base_url": "rerank_base_url",
        "name": "rerank_model_name",
        "api_key": "rerank_api_key",
        "timeout_seconds": "rerank_timeout_seconds",
        "candidate_limit": "rerank_candidate_limit",
        "min_candidates": "rerank_min_candidates",
        "rrf_weight": "rerank_rrf_weight",
    },
    "knowledge_graph": {
        "max_hops": "graph_max_hops",
        "candidate_limit": "graph_candidate_limit",
    },
    "memory": {name: name for name in MemoryPolicy.model_fields},
    "personality": {name: name for name in PersonalityPolicy.model_fields},
    "emotion": {name: name for name in EmotionPolicy.model_fields},
    "screen_vision": _SCREEN_VISION_KEYS,
    "model_routing": {name: name for name in _ROUTING_FIELDS},
    "scene_classifier": {name: name for name in _SCENE_FIELDS},
    "speech": {name: name for name in _SPEECH_FIELDS},
    "voice": {name: name for name in _VOICE_FIELDS},
    "dist": {
        "directory": "dist_directory",
        "upload_secret": "dist_upload_secret",
        "max_bytes": "dist_max_bytes",
        "max_packages": "dist_max_packages",
    },
}
_SECTION_KINDS = {
    "logging": {
        "level": str,
        "console": bool,
        "file_enabled": bool,
        "file_path": str,
        "max_mb": int,
        "backup_count": int,
        "access": bool,
    },
    "tools": {"enabled": bool, "max_calls_per_turn": int, "plugins": list},
    "delegation": {
        "enabled": bool,
        "command": list,
        "working_dir": str,
        "timeout_seconds": float,
        "max_output_chars": int,
        "env_allowlist": list,
    },
    "proactive_schedule": {
        "min_delay_seconds": int,
        "max_delay_seconds": int,
        "min_idle_seconds": int,
    },
}
_STRING_FIELDS = (
    "database_path",
    "api_token",
    "model_base_url",
    "model_name",
    "model_api_key",
    "server_host",
    "embedding_base_url",
    "embedding_model_name",
    "embedding_api_key",
    "embedding_space_identity",
    "rerank_base_url",
    "rerank_model_name",
    "rerank_api_key",
    "log_level",
    "log_file_path",
    "dist_directory",
    "dist_upload_secret",
)
_INTEGER_FIELDS = (
    "node_ttl_seconds",
    "lease_ttl_seconds",
    "transfer_ttl_seconds",
    "auto_follow_quiet_seconds",
    "auto_memory_batch_size",
    "auto_memory_output_tokens",
    "server_port",
    "embedding_batch_size",
    "embedding_max_input_tokens",
    "embedding_candidate_limit",
    "embedding_query_cache_size",
    "rerank_candidate_limit",
    "rerank_min_candidates",
    "graph_max_hops",
    "graph_candidate_limit",
    "log_file_max_mb",
    "log_file_backup_count",
    "dist_max_bytes",
    "dist_max_packages",
    "webui_session_days",
)
_NUMBER_FIELDS = (
    "proactivity_poll_seconds",
    "inner_life_poll_seconds",
    "event_poll_seconds",
    "reconcile_seconds",
    "websocket_auth_timeout_seconds",
    "model_timeout_seconds",
    "auto_memory_max_wait_seconds",
    "auto_memory_poll_seconds",
    "embedding_timeout_seconds",
    "embedding_poll_seconds",
    "embedding_query_cache_ttl_seconds",
    "rerank_timeout_seconds",
)
_NONNEGATIVE_NUMBER_FIELDS = ("dialogue_debounce_seconds",)
_WEIGHT_FIELDS = ("rerank_rrf_weight",)


def _environment_value(name: str, kind: type) -> object:
    """Parse an override without including secret or malformed values in errors."""
    value = os.environ[name]
    alternatives = get_args(kind)
    if type(None) in alternatives:
        if not value.strip():
            return None
        kind = next(option for option in alternatives if option is not type(None))
    if kind is str:
        return value
    if kind is bool:
        normalized = value.strip().lower()
        if normalized in ("true", "1", "false", "0"):
            return normalized in ("true", "1")
        raise ValueError(f"{name} must be true/false or 1/0")
    try:
        if kind in (dict, list):
            parsed = json.loads(value)
            if not isinstance(parsed, kind):
                raise ValueError
            return parsed
        return kind(value)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(f"{name} has an invalid {kind.__name__} value") from None


def _finite_positive(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _load_config(path: Path, *, required: bool) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        if required:
            raise ValueError(f"Configuration file not found: {path}") from None
        return {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError):
        raise ValueError(f"Configuration file must contain valid UTF-8 TOML: {path}") from None
    except OSError:
        raise ValueError(f"Configuration file cannot be read: {path}") from None
    values: dict[str, object] = {}
    for section, entries in document.items():
        if section not in _CONFIG_SECTIONS:
            raise ValueError(f"Unknown configuration section: {section}")
        if not isinstance(entries, dict):
            raise ValueError(f"Configuration section {section} must be a table")
        mapping = _CONFIG_SECTIONS[section]
        for key, value in entries.items():
            if section == "personality" and key == "evolution":
                if not isinstance(value, dict):
                    raise ValueError("Configuration section personality.evolution must be a table")
                values["evolution_policy"] = value
                continue
            if section == "personality" and key == "slow_variables":
                if not isinstance(value, dict):
                    raise ValueError(
                        "Configuration section personality.slow_variables must be a table"
                    )
                values["slow_variable_policy"] = value
                continue
            if section == "inner_life" and key != "poll_seconds":
                values.setdefault("inner_life_policy", {})[key] = value
                continue
            if key not in mapping:
                raise ValueError(f"Unknown configuration field: {section}.{key}")
            if section in ("memory", "personality", "emotion"):
                values.setdefault(f"{section}_policy", {})[key] = value
            elif section == "screen_vision":
                values.setdefault("screen_vision", {})[mapping[key]] = value
            elif section == "model_routing":
                values.setdefault("routing", {})[mapping[key]] = value
            elif section == "scene_classifier":
                values.setdefault("scene_classifier", {})[mapping[key]] = value
            elif section in ("speech", "voice"):
                values.setdefault(section, {})[mapping[key]] = value
            elif section in ("delegation", "proactive_schedule"):
                values.setdefault(section, {})[mapping[key]] = value
            else:
                values[mapping[key]] = value
    return values


def _policy(
    model: type[MemoryPolicy]
    | type[PersonalityPolicy]
    | type[EmotionPolicy]
    | type[EvolutionPolicy]
    | type[SlowVariablePolicy]
    | type[InnerLifePolicy],
    values: object,
    section: str,
) -> (
    MemoryPolicy
    | PersonalityPolicy
    | EmotionPolicy
    | EvolutionPolicy
    | SlowVariablePolicy
    | InnerLifePolicy
):
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(part) for part in error["loc"])
        field_name = f"{section}.{location}" if location else section
        raise ValueError(f"Invalid configuration for {field_name}: {error['msg']}") from None


def _screen_vision(values: object) -> ScreenVisionConfig:
    if values is None or values == {}:
        return ScreenVisionConfig()
    if isinstance(values, ScreenVisionConfig):
        config = values
    elif isinstance(values, dict):
        unknown = set(values) - set(_SCREEN_VISION_FIELDS)
        if unknown:
            raise ValueError(f"Unknown configuration field: screen_vision.{sorted(unknown)[0]}")
        try:
            config = ScreenVisionConfig(**values)
        except TypeError as exc:
            raise ValueError(f"Invalid configuration for screen_vision: {exc}") from None
    else:
        raise ValueError("Invalid configuration for screen_vision: must be a table")
    for name in ("enabled", "json_mode", "enrichment"):
        if type(getattr(config, name)) is not bool:
            raise ValueError(f"screen_vision.{name} must be a boolean")
    if config.mode not in ("two_stage", "single_pass"):
        raise ValueError("screen_vision.mode must be one of ['single_pass', 'two_stage']")
    for name in (
        "base_url",
        "model_name",
        "api_key",
        "storage_path",
        "injection_gate",
        "injection_base_url",
        "injection_api_key",
        "injection_model",
    ):
        if not isinstance(getattr(config, name), str):
            raise ValueError(f"screen_vision.{name} must be a string")
    if config.injection_gate not in ("off", "deterministic", "decision", "jev"):
        raise ValueError(
            "screen_vision.injection_gate must be one of ['decision', 'deterministic', 'jev', 'off']"
        )
    for name in (
        "timeout_seconds",
        "worker_poll_seconds",
        "cleanup_interval_seconds",
        "injection_timeout_seconds",
    ):
        if not _finite_positive(getattr(config, name)):
            raise ValueError(f"screen_vision.{name} must be a finite positive number")
    if not _finite_positive(config.injection_threshold) or config.injection_threshold > 1:
        raise ValueError("screen_vision.injection_threshold must be a number in (0, 1]")
    integer_bounds = {
        "session_ttl_seconds": (60, 86400),
        "frame_ttl_seconds": (5, 300),
        "max_image_bytes": (1024, 268435456),
        "max_image_pixels": (1, 100_000_000),
        "max_image_side": (16, 100_000),
        "global_concurrency": (1, 32),
        "max_pending_global": (1, 4096),
        "max_pending_per_stream": (1, 64),
        "max_uploads_per_minute_per_node": (1, 100_000),
        "max_frames_per_minute_per_node": (1, 100_000),
        "rate_burst": (1, 1000),
        "max_pending_manual_per_node": (1, 64),
        "analysis_cache_ttl_seconds": (0, 86400),
        "evidence_wait_ms": (0, 5000),
        "comparison_window_frames": (1, 512),
        "upload_retention_seconds": (30, 86400),
        "raw_retention_seconds": (30, 604800),
        "result_retention_seconds": (60, 2_592_000),
        "receipt_retention_seconds": (60, 31_536_000),
        "max_storage_bytes": (1_048_576, 2**63 - 1),
        "output_tokens": (16, 32000),
        "max_model_images": (1, 16),
        "daily_model_call_limit": (0, 1_000_000),
        "daily_vision_token_limit": (0, 1_000_000_000),
        "max_context_windows": (0, 64),
        "max_context_chars": (0, 8192),
    }
    for name, (minimum, maximum) in integer_bounds.items():
        value = getattr(config, name)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(
                f"screen_vision.{name} must be an integer between {minimum} and {maximum}"
            )
    if (
        config.enabled
        and config.mode == "two_stage"
        and not (config.base_url.strip() and config.model_name.strip())
    ):
        raise ValueError(
            "Enabled two-stage screen vision requires screen_vision.base_url and screen_vision.name"
        )
    if bool(config.base_url.strip()) != bool(config.model_name.strip()):
        raise ValueError("Configure screen_vision.base_url and screen_vision.name together")
    if len(config.model_name.strip()) > 256:
        raise ValueError("screen_vision.name must not exceed 256 characters")
    if not config.storage_path.strip() or config.storage_path == ":memory:":
        raise ValueError("screen_vision.storage_path must name a directory")
    if not isinstance(config.extra_body, dict):
        raise ValueError("screen_vision.extra_body must be an object")
    try:
        bounded_json(config.extra_body)
    except ValueError:
        raise ValueError(
            "screen_vision.extra_body must contain bounded finite JSON values"
        ) from None
    if PROTECTED_BODY_FIELDS.intersection(config.extra_body):
        raise ValueError("screen_vision.extra_body cannot override execution or budget fields")
    base_url = config.base_url.strip()
    if base_url:
        try:
            url = urlsplit(base_url)
            valid = (
                url.scheme in ("http", "https")
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
            )
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Screen vision base URL must use HTTP(S) without credentials or query")
        base_url = base_url.rstrip("/")
    injection_base_url = config.injection_base_url.strip()
    if injection_base_url:
        try:
            url = urlsplit(injection_base_url)
            valid = (
                url.scheme in ("http", "https")
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
            )
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Screen vision injection base URL must use HTTP(S) without credentials or query"
            )
        injection_base_url = injection_base_url.rstrip("/")
    return ScreenVisionConfig(
        **{
            **{name: getattr(config, name) for name in _SCREEN_VISION_FIELDS},
            "base_url": base_url,
            "model_name": config.model_name.strip(),
            "injection_base_url": injection_base_url,
        }
    )


_TOOL_PLUGIN_PATH = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")


def _tool_plugin_paths(values: object) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError("Tool plugins must be a list of module:factory paths")
    if len(values) > 16:
        raise ValueError("At most 16 tool plugin factories may be configured")
    paths = tuple(values)
    if len(set(paths)) != len(paths):
        raise ValueError("Tool plugin factories must be unique")
    if any(not isinstance(path, str) or not _TOOL_PLUGIN_PATH.fullmatch(path) for path in paths):
        raise ValueError("Tool plugin factories must use module:callable paths")
    return paths


def _delegation_config(values: object) -> DelegationConfig:
    if isinstance(values, DelegationConfig):
        return values
    if values is None or values == {}:
        return DelegationConfig()
    if not isinstance(values, dict):
        raise ValueError("Invalid configuration for delegation: must be an object")
    try:
        return DelegationConfig(**values)
    except TypeError:
        raise ValueError("Unknown configuration field in delegation") from None
    except ValueError as exc:
        raise ValueError(f"Invalid configuration for delegation: {exc}") from None


def _schedule_policy(values: object) -> SchedulePolicy:
    if isinstance(values, SchedulePolicy):
        return values
    if values is None or values == {}:
        return SchedulePolicy()
    if not isinstance(values, dict):
        raise ValueError("Invalid configuration for proactive_schedule: must be an object")
    try:
        return SchedulePolicy(**values)
    except TypeError:
        raise ValueError("Unknown configuration field in proactive_schedule") from None
    except ValueError as exc:
        raise ValueError(f"Invalid configuration for proactive_schedule: {exc}") from None


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: str = ".data/runtime.sqlite3"
    api_token: str = field(default="local-dev-token", repr=False)
    node_ttl_seconds: int = 60
    lease_ttl_seconds: int = 10
    transfer_ttl_seconds: int = 30
    # Node rooms: the lifeform follows the room that most recently reported
    # activity. The abandoned room stays "empty" but keeps its tools.
    auto_follow_rooms: bool = True
    auto_follow_quiet_seconds: int = 15
    event_poll_seconds: float = 0.2
    reconcile_seconds: float = 0.5
    websocket_auth_timeout_seconds: float = 5.0
    # WebUI session cookie lifetime in days; 0 refuses to remember the token.
    webui_session_days: int = 30
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: str = field(default="", repr=False)
    model_timeout_seconds: float = 60.0
    model_extra_body: JsonObject = field(default_factory=dict, repr=False)
    auto_memory_enabled: bool = True
    auto_memory_batch_size: int = 4
    auto_memory_max_wait_seconds: float = 60.0
    auto_memory_poll_seconds: float = 2.0
    auto_memory_output_tokens: int = 1200
    proactivity_poll_seconds: float = 2.0
    inner_life_poll_seconds: float = 10.0
    dialogue_debounce_seconds: float = 0.0
    tools_enabled: bool = False
    tools_max_calls_per_turn: int = 1
    tools_plugins: tuple[str, ...] = ()
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    proactive_schedule: SchedulePolicy = field(default_factory=SchedulePolicy)
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    log_level: str = "INFO"
    log_console: bool = True
    log_file_enabled: bool = True
    log_file_path: str = ".data/logs/virtual-life.log"
    log_file_max_mb: int = 10
    log_file_backup_count: int = 3
    log_access: bool = True
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)
    personality_policy: PersonalityPolicy = field(default_factory=PersonalityPolicy)
    emotion_policy: EmotionPolicy = field(default_factory=EmotionPolicy)
    evolution_policy: EvolutionPolicy = field(default_factory=EvolutionPolicy)
    slow_variable_policy: SlowVariablePolicy = field(default_factory=SlowVariablePolicy)
    inner_life_policy: InnerLifePolicy = field(default_factory=InnerLifePolicy)
    embedding_enabled: bool = False
    embedding_base_url: str = ""
    embedding_model_name: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_timeout_seconds: float = 60.0
    embedding_dimensions: int | None = None
    embedding_extra_body: JsonObject = field(default_factory=dict, repr=False)
    embedding_batch_size: int = 16
    embedding_max_input_tokens: int = 4000
    embedding_poll_seconds: float = 5.0
    embedding_candidate_limit: int = 5000
    embedding_similarity_threshold: float = 0.25
    embedding_query_cache_size: int = 256
    embedding_query_cache_ttl_seconds: float = 300.0
    embedding_dialogue_recall: bool = True
    embedding_space_identity: str = ""
    embedding_local: bool = False
    rerank_enabled: bool = False
    rerank_base_url: str = ""
    rerank_model_name: str = ""
    rerank_api_key: str = field(default="", repr=False)
    rerank_timeout_seconds: float = 0.5
    rerank_candidate_limit: int = 8
    rerank_min_candidates: int = 4
    rerank_rrf_weight: float = 0.15
    graph_max_hops: int = 2
    graph_candidate_limit: int = 512
    screen_vision: ScreenVisionConfig = field(default_factory=ScreenVisionConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    scene_classifier: SceneClassifierConfig = field(default_factory=SceneClassifierConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    # Distribution artifacts for the desktop host client. Downloads are gated by
    # the regular bearer token; uploads additionally require this secret so a
    # leaked api_token cannot replace a future client.
    dist_directory: str = ".data/dist"
    dist_upload_secret: str = field(default="", repr=False)
    dist_max_bytes: int = 67_108_864
    dist_max_packages: int = 25

    def __post_init__(self) -> None:
        for name in _STRING_FIELDS:
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        for name in _INTEGER_FIELDS:
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        for name in _NUMBER_FIELDS:
            if not _finite_positive(getattr(self, name)):
                raise ValueError(f"{name} must be a finite positive number")
        for name in _NONNEGATIVE_NUMBER_FIELDS:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite nonnegative number")
        if not self.server_host.strip() or len(self.server_host) > 255:
            raise ValueError("Server host must contain 1 to 255 characters")
        if not 1 <= self.server_port <= 65535:
            raise ValueError("Server port must be between 1 and 65535")
        log_level = self.log_level.strip().upper()
        if log_level not in _LOG_LEVELS:
            raise ValueError("logging.level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        if not self.log_file_path.strip():
            raise ValueError("logging.file_path must not be empty")
        for name in ("log_console", "log_file_enabled", "log_access"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"logging.{name.removeprefix('log_')} must be a boolean")
        if not 1 <= self.log_file_max_mb <= 1024:
            raise ValueError("logging.max_mb must be between 1 and 1024")
        if not 1 <= self.log_file_backup_count <= 100:
            raise ValueError("logging.backup_count must be between 1 and 100")
        object.__setattr__(self, "log_level", log_level)
        if not self.dist_directory.strip():
            raise ValueError("dist.directory must not be empty")
        if not 1 <= self.dist_max_bytes <= 2 * 1024 * 1024 * 1024:
            raise ValueError("dist.max_bytes must be between 1 and 2147483648")
        if not 1 <= self.dist_max_packages <= 1000:
            raise ValueError("dist.max_packages must be between 1 and 1000")
        for name in (
            "embedding_enabled",
            "embedding_dialogue_recall",
            "embedding_local",
            "rerank_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name, minimum, maximum in (
            ("embedding_batch_size", 1, 64),
            ("embedding_max_input_tokens", 1, 64000),
            ("embedding_candidate_limit", 1, 100000),
            ("embedding_query_cache_size", 1, 10000),
            ("rerank_candidate_limit", 1, 64),
            ("rerank_min_candidates", 1, 64),
            ("graph_max_hops", 1, 3),
            ("graph_candidate_limit", 1, 10000),
        ):
            if not minimum <= getattr(self, name) <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.rerank_min_candidates > self.rerank_candidate_limit:
            raise ValueError("rerank.min_candidates must not exceed rerank.candidate_limit")
        for name in _WEIGHT_FIELDS:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be a number between 0 and 1")
        if not 1 <= self.embedding_query_cache_ttl_seconds <= 86400:
            raise ValueError("Embedding cache TTL must be between 1 and 86400 seconds")
        if self.embedding_dimensions is not None and (
            type(self.embedding_dimensions) is not int
            or not 1 <= self.embedding_dimensions <= 65536
        ):
            raise ValueError("Embedding dimensions must be an integer between 1 and 65536")
        similarity = self.embedding_similarity_threshold
        if (
            isinstance(similarity, bool)
            or not isinstance(similarity, (int, float))
            or not -1 <= similarity <= 1
        ):
            raise ValueError("Embedding similarity threshold must be finite and between -1 and 1")
        # Reuse the standalone adapter validation, including protected request fields.
        embedding_url, embedding_name = (
            self.embedding_base_url.strip(),
            self.embedding_model_name.strip(),
        )
        if bool(embedding_url) != bool(embedding_name):
            raise ValueError("Configure embedding.base_url and embedding.name together")
        if self.embedding_enabled and not embedding_url:
            raise ValueError("Enabled embeddings require embedding.base_url and embedding.name")
        if len(embedding_name) > 256 or len(self.embedding_space_identity) > 256:
            raise ValueError(
                "Embedding model name and space identity must not exceed 256 characters"
            )
        adapter = CompatibleEmbeddingProvider(
            embedding_url or "http://localhost",
            self.embedding_api_key,
            timeout_seconds=self.embedding_timeout_seconds,
            dimensions=self.embedding_dimensions,
            extra_body=self.embedding_extra_body,
        )
        object.__setattr__(self, "embedding_base_url", embedding_url.rstrip("/"))
        object.__setattr__(self, "embedding_model_name", embedding_name)
        object.__setattr__(self, "embedding_extra_body", adapter.extra_body)
        rerank_url, rerank_name = self.rerank_base_url.strip(), self.rerank_model_name.strip()
        if bool(rerank_url) != bool(rerank_name):
            raise ValueError("Configure rerank.base_url and rerank.name together")
        if self.rerank_enabled and not rerank_url:
            raise ValueError("Enabled reranking requires rerank.base_url and rerank.name")
        if len(rerank_name) > 256:
            raise ValueError("Rerank model name must not exceed 256 characters")
        if rerank_url:
            try:
                url = urlsplit(rerank_url)
                valid = (
                    url.scheme in ("http", "https")
                    and bool(url.hostname)
                    and url.username is None
                    and url.password is None
                    and not url.query
                    and not url.fragment
                )
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Rerank base URL must use HTTP(S) without credentials or query")
        object.__setattr__(self, "rerank_base_url", rerank_url.rstrip("/"))
        object.__setattr__(self, "rerank_model_name", rerank_name)
        object.__setattr__(
            self, "memory_policy", _policy(MemoryPolicy, self.memory_policy, "memory")
        )
        object.__setattr__(
            self,
            "personality_policy",
            _policy(PersonalityPolicy, self.personality_policy, "personality"),
        )
        object.__setattr__(
            self,
            "emotion_policy",
            _policy(EmotionPolicy, self.emotion_policy, "emotion"),
        )
        object.__setattr__(
            self,
            "evolution_policy",
            _policy(EvolutionPolicy, self.evolution_policy, "personality.evolution"),
        )
        object.__setattr__(
            self,
            "slow_variable_policy",
            _policy(SlowVariablePolicy, self.slow_variable_policy, "personality.slow_variables"),
        )
        object.__setattr__(
            self,
            "inner_life_policy",
            _policy(InnerLifePolicy, self.inner_life_policy, "inner_life"),
        )
        object.__setattr__(self, "screen_vision", _screen_vision(self.screen_vision))
        object.__setattr__(self, "routing", routing_config(self.routing))
        object.__setattr__(self, "scene_classifier", scene_classifier_config(self.scene_classifier))
        object.__setattr__(self, "speech", speech_config(self.speech))
        object.__setattr__(self, "voice", voice_config(self.voice))
        if not self.database_path.strip() or self.database_path == ":memory:":
            raise ValueError("Use a file-backed database so all runtime modules share state")
        if not self.api_token.strip():
            raise ValueError("VIRTUAL_LIFE_API_TOKEN must not be empty")
        if type(self.auto_memory_enabled) is not bool:
            raise ValueError("Automatic memory enabled must be a boolean")
        if type(self.tools_enabled) is not bool:
            raise ValueError("Tools enabled must be a boolean")
        if (
            type(self.tools_max_calls_per_turn) is not int
            or not 1 <= self.tools_max_calls_per_turn <= 2
        ):
            raise ValueError("Tool calls per turn must be between 1 and 2")
        tools_plugins = _tool_plugin_paths(self.tools_plugins)
        if tools_plugins and not self.tools_enabled:
            raise ValueError("Configured tool plugins require tools.enabled")
        object.__setattr__(self, "tools_plugins", tools_plugins)
        delegation = _delegation_config(self.delegation)
        schedule = _schedule_policy(self.proactive_schedule)
        if delegation.enabled and not self.tools_enabled:
            raise ValueError("Enabled delegation requires tools.enabled")
        object.__setattr__(self, "delegation", delegation)
        object.__setattr__(self, "proactive_schedule", schedule)
        if not 1 <= self.auto_memory_batch_size <= 16:
            raise ValueError("Automatic memory batch size must be between 1 and 16")
        if not 256 <= self.auto_memory_output_tokens <= 2048:
            raise ValueError("Automatic memory output tokens must be between 256 and 2048")
        for setting_name in (
            "node_ttl_seconds",
            "lease_ttl_seconds",
            "transfer_ttl_seconds",
        ):
            if not _finite_positive(getattr(self, setting_name)):
                raise ValueError(f"{setting_name} must be finite and positive")
        if (
            not _finite_positive(self.auto_follow_quiet_seconds)
            or self.auto_follow_quiet_seconds < 0
        ):
            raise ValueError("auto_follow_quiet_seconds must be finite and nonnegative")
        if not 0 <= self.webui_session_days <= 3650:
            raise ValueError("runtime.webui_session_days must be between 0 and 3650")
        base_url, model_name = self.model_base_url.strip(), self.model_name.strip()
        if bool(base_url) != bool(model_name):
            raise ValueError(
                "Configure VIRTUAL_LIFE_MODEL_BASE_URL and VIRTUAL_LIFE_MODEL together"
            )
        if base_url:
            try:
                url = urlsplit(base_url)
                valid = (
                    url.scheme in ("http", "https")
                    and bool(url.hostname)
                    and url.username is None
                    and url.password is None
                    and not url.query
                    and not url.fragment
                )
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Model base URL must use HTTP(S) without credentials or query")
        if len(model_name) > 256:
            raise ValueError("Model name must not exceed 256 characters")
        try:
            body = TypeAdapter(JsonObject).validate_python(self.model_extra_body, strict=True)
            bounded_json(body)
        except (ValueError, TypeError, ValidationError, RecursionError):
            raise ValueError("Model extra body must be a bounded JSON object") from None
        if PROTECTED_BODY_FIELDS.intersection(body):
            raise ValueError("Model extra body cannot override execution or budget fields")
        object.__setattr__(self, "model_base_url", base_url.rstrip("/"))
        object.__setattr__(self, "model_name", model_name)
        object.__setattr__(self, "server_host", self.server_host.strip())
        object.__setattr__(self, "model_extra_body", json.loads(json.dumps(body, allow_nan=False)))

    @classmethod
    def from_environment(cls) -> "Settings":
        """Compatibility entry point; loads the unified file and environment overrides."""
        return cls.load()

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        """Defaults < TOML file < explicitly set environment variables.

        The default file is optional. Explicit paths must exist. Relative database
        paths supplied by a file are anchored to that file; environment paths keep
        their historical current-working-directory semantics. No .env is read here.
        """
        configured_path = (
            config_path if config_path is not None else os.environ.get("VIRTUAL_LIFE_CONFIG")
        )
        if configured_path is not None and not str(configured_path).strip():
            raise ValueError("Configuration file path must not be empty")
        path = (
            Path(configured_path if configured_path is not None else DEFAULT_CONFIG_FILE)
            .expanduser()
            .resolve()
        )
        values = _load_config(path, required=configured_path is not None)
        for section, section_fields in _CONFIG_SECTIONS.items():
            for key, setting_name in section_fields.items():
                empty_optional = False
                if section == "screen_vision":
                    kind = _SCREEN_VISION_FIELDS[setting_name].type
                    env_name = f"VIRTUAL_LIFE_SCREEN_VISION_{key.upper()}"
                elif section == "model_routing":
                    kind = list if setting_name == "routes" else _ROUTING_FIELDS[setting_name].type
                    env_name = f"VIRTUAL_LIFE_MODEL_ROUTING_{key.upper()}"
                elif section == "scene_classifier":
                    kind = dict if setting_name == "models" else _SCENE_FIELDS[setting_name].type
                    env_name = f"VIRTUAL_LIFE_SCENE_CLASSIFIER_{key.upper()}"
                elif section in ("speech", "voice"):
                    source = _SPEECH_FIELDS if section == "speech" else _VOICE_FIELDS
                    kind = source[setting_name].type
                    env_name = f"VIRTUAL_LIFE_{section.upper()}_{key.upper()}"
                elif section in ("memory", "personality", "emotion"):
                    model = {
                        "memory": MemoryPolicy,
                        "personality": PersonalityPolicy,
                        "emotion": EmotionPolicy,
                    }[section]
                    kind = model.model_fields[key].annotation
                    if section == "memory" and key == "decay":
                        kind = dict
                    env_name = f"VIRTUAL_LIFE_{section.upper()}_{key.upper()}"
                elif section in _SECTION_KINDS:
                    kind = _SECTION_KINDS[section][key]
                    env_name = f"VIRTUAL_LIFE_{section.upper()}_{key.upper()}"
                else:
                    if setting_name in _STRING_FIELDS:
                        kind = str
                    elif setting_name in _INTEGER_FIELDS or setting_name == "embedding_dimensions":
                        kind = int
                    elif (
                        setting_name in _NUMBER_FIELDS
                        or setting_name in _WEIGHT_FIELDS
                        or setting_name == "embedding_similarity_threshold"
                        or setting_name in _NONNEGATIVE_NUMBER_FIELDS
                    ):
                        kind = float
                    else:
                        kind = (
                            bool
                            if setting_name
                            in (
                                "auto_memory_enabled",
                                "embedding_enabled",
                                "embedding_dialogue_recall",
                                "embedding_local",
                                "rerank_enabled",
                                "auto_follow_rooms",
                            )
                            else dict
                        )
                    suffix = {
                        "database_path": "DATABASE",
                        "model_name": "MODEL",
                        "embedding_model_name": "EMBEDDING_MODEL",
                        "rerank_model_name": "RERANK_MODEL",
                    }.get(setting_name, setting_name.upper())
                    env_name = f"VIRTUAL_LIFE_{suffix}"
                    empty_optional = (
                        (
                            setting_name == "embedding_dimensions"
                            and not os.environ[env_name].strip()
                        )
                        if env_name in os.environ
                        else False
                    )
                if env_name not in os.environ:
                    continue
                value = None if empty_optional else _environment_value(env_name, kind)
                if section == "screen_vision":
                    values.setdefault("screen_vision", {})[setting_name] = value
                elif section == "model_routing":
                    values.setdefault("routing", {})[setting_name] = value
                elif section == "scene_classifier":
                    values.setdefault("scene_classifier", {})[setting_name] = value
                elif section in ("speech", "voice"):
                    values.setdefault(section, {})[setting_name] = value
                elif section in ("memory", "personality", "emotion"):
                    values.setdefault(f"{section}_policy", {})[key] = value
                elif section in ("delegation", "proactive_schedule"):
                    values.setdefault(section, {})[setting_name] = value
                else:
                    values[setting_name] = value
        # Individual decay overrides take precedence over the JSON object override.
        for key, definition in MemoryDecayPolicy.model_fields.items():
            env_name = f"VIRTUAL_LIFE_MEMORY_DECAY_{key.upper()}"
            if env_name not in os.environ:
                continue
            policy = values.setdefault("memory_policy", {})
            decay = policy.setdefault("decay", {})
            if not isinstance(decay, dict):
                raise ValueError("Invalid configuration for memory.decay: must be an object")
            decay[key] = _environment_value(env_name, definition.annotation)
        # Personality evolution overrides use the nested section name.
        for key, definition in EvolutionPolicy.model_fields.items():
            env_name = f"VIRTUAL_LIFE_PERSONALITY_EVOLUTION_{key.upper()}"
            if env_name not in os.environ:
                continue
            policy = values.setdefault("evolution_policy", {})
            if not isinstance(policy, dict):
                raise ValueError(
                    "Invalid configuration for personality.evolution: must be an object"
                )
            policy[key] = _environment_value(env_name, definition.annotation)
        # Long-term slow-variable overrides use the nested section name.
        for key, definition in SlowVariablePolicy.model_fields.items():
            env_name = f"VIRTUAL_LIFE_PERSONALITY_SLOW_VARIABLES_{key.upper()}"
            if env_name not in os.environ:
                continue
            policy = values.setdefault("slow_variable_policy", {})
            if not isinstance(policy, dict):
                raise ValueError(
                    "Invalid configuration for personality.slow_variables: must be an object"
                )
            policy[key] = _environment_value(env_name, definition.annotation)
        for key, definition in InnerLifePolicy.model_fields.items():
            env_name = f"VIRTUAL_LIFE_INNER_LIFE_{key.upper()}"
            if env_name not in os.environ:
                continue
            policy = values.setdefault("inner_life_policy", {})
            if not isinstance(policy, dict):
                raise ValueError("Invalid configuration for inner_life: must be an object")
            policy[key] = _environment_value(env_name, definition.annotation)
        if "database_path" in values and "VIRTUAL_LIFE_DATABASE" not in os.environ:
            database = values["database_path"]
            if not isinstance(database, str) or not database.strip() or database == ":memory:":
                raise ValueError("runtime.database_path must name a file-backed database")
            database_path = Path(database).expanduser()
            values["database_path"] = str(
                database_path if database_path.is_absolute() else path.parent / database_path
            )
        if "log_file_path" in values and "VIRTUAL_LIFE_LOGGING_FILE_PATH" not in os.environ:
            configured_log = values["log_file_path"]
            if not isinstance(configured_log, str) or not configured_log.strip():
                raise ValueError("logging.file_path must name a file")
            log_path = Path(configured_log).expanduser()
            values["log_file_path"] = str(
                log_path if log_path.is_absolute() else path.parent / log_path
            )
        if "dist_directory" in values and "VIRTUAL_LIFE_DIST_DIRECTORY" not in os.environ:
            directory = values["dist_directory"]
            if not isinstance(directory, str) or not directory.strip():
                raise ValueError("dist.directory must name a directory")
            resource = Path(directory).expanduser()
            values["dist_directory"] = str(
                resource if resource.is_absolute() else path.parent / resource
            )
        if (
            "scene_classifier" in values
            and "VIRTUAL_LIFE_SCENE_CLASSIFIER_ARTIFACT" not in os.environ
        ):
            scene_values = values["scene_classifier"]
            if not isinstance(scene_values, dict):
                raise ValueError("scene_classifier must be a table")
            artifact = scene_values.get("artifact")
            if isinstance(artifact, str) and artifact.strip():
                resource = Path(artifact).expanduser()
                scene_values["artifact"] = str(
                    resource if resource.is_absolute() else path.parent / resource
                )
        for name in ("model_dir", "model_manifest", "worker_python"):
            if f"VIRTUAL_LIFE_SPEECH_{name.upper()}" in os.environ:
                continue
            value = values.get("speech", {}).get(name)
            if isinstance(value, str) and value.strip():
                # Bare interpreter names resolve through PATH. Explicit paths
                # from TOML resolve beside the configuration file.
                if name == "worker_python" and "/" not in value and not value.startswith("~"):
                    continue
                resource = Path(value).expanduser()
                values["speech"][name] = str(
                    resource if resource.is_absolute() else path.parent / resource
                )
        return cls(**values)

    def prepare_directory(self) -> None:
        Path(self.database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
