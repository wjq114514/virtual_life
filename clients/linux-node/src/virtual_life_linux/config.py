"""Strict configuration, XDG paths and credential loading for the Linux host."""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_SCHEMA_VERSION = 1
DEFAULT_SERVER_URL = "http://127.0.0.1:8038"
DEFAULT_NODE_ID = "linux-workstation"
_TOP_LEVEL_KEYS = {
    "schema_version",
    "server_url",
    "node_id",
    "device_id",
    "label",
    "credential_file",
    "desktop",
    "interaction",
    "reporting",
    "screen",
    "voice",
}
_SCREEN_KEYS = {
    "enabled",
    "companion_id",
    "profile",
    "mode",
    "display_id",
    "capture_binary",
    "capture_source",
}
_VOICE_KEYS = {
    "enabled",
    "asr_manifest",
    "input_device",
    "output_device",
    "model_threads",
    "heartbeat_seconds",
    "max_buffered_audio_seconds",
    "barge_in_mode",
}
_SCREEN_PROFILES = {"economy", "balanced", "detail"}
_SCREEN_MODES = {"continuous", "one_shot"}
_SCREEN_SOURCES = {"x11", "wayland_portal", "other"}
_BARGE_IN_MODES = {"manual", "vad_best_effort"}
_DESKTOP_KEYS = {
    "backend",
    "attention_enabled",
    "dms_enabled",
    "fish_activity_enabled",
    "scene_enabled",
    "scene_titles_enabled",
    "scene_max_windows",
}
_INTERACTION_KEYS = {"terminal", "auto_connect", "notifications"}
_REPORTING_KEYS = {"heartbeat_seconds", "attention_seconds", "attention_ttl_seconds"}
_BACKENDS = {"niri", "none"}
_NOTIFICATIONS = {"off", "status_only", "full"}


class ConfigError(ValueError):
    """A configuration file or credential is missing or invalid."""


def _xdg(name: str, fallback: str) -> Path:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return Path(value).expanduser()
    return Path(fallback).expanduser()


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", "~/.config")


def state_home() -> Path:
    return _xdg("XDG_STATE_HOME", "~/.local/state")


def cache_home() -> Path:
    return _xdg("XDG_CACHE_HOME", "~/.cache")


def runtime_home() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value is None or not value.strip():
        raise ConfigError("XDG_RUNTIME_DIR is required for the local IPC socket")
    return Path(value).expanduser()


def default_config_dir() -> Path:
    return config_home() / "virtual-life" / "linux"


def default_config_path() -> Path:
    return default_config_dir() / "config.toml"


def default_runtime_dir() -> Path:
    return runtime_home() / "virtual-life"


def runtime_dir_for(config: LinuxConfig) -> Path:
    """Per-server, per-node runtime directory for the IPC socket and lock.

    Scoped by the server key so two daemons for two different runtimes on the
    same seat cannot collide on one socket.
    """
    return runtime_home() / "virtual-life" / config.server_key / config.node_id


def default_cache_dir() -> Path:
    return cache_home() / "virtual-life"


def normalize_server_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("server_url must be an absolute http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("server_url must not contain credentials, query or fragment")
    host = parts.hostname.lower()
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{host}:{port}{path}"


def server_key(url: str) -> str:
    normalized = normalize_server_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    backend: str = "niri"
    attention_enabled: bool = False
    dms_enabled: bool = True
    fish_activity_enabled: bool = False
    scene_enabled: bool = False
    scene_titles_enabled: bool = False
    scene_max_windows: int = 12


@dataclass(frozen=True, slots=True)
class InteractionConfig:
    terminal: str = "kitty"
    auto_connect: bool = False
    notifications: str = "status_only"


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    heartbeat_seconds: int = 20
    attention_seconds: int = 15
    attention_ttl_seconds: int = 45


@dataclass(frozen=True, slots=True)
class ScreenConfig:
    enabled: bool = False
    companion_id: str = ""
    profile: str = "balanced"
    mode: str = "continuous"
    display_id: str = "default"
    capture_binary: str = "grim"
    capture_source: str = "other"


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    enabled: bool = False
    asr_manifest: str = ""
    input_device: str = ""
    output_device: str = ""
    model_threads: int = 2
    heartbeat_seconds: int = 10
    max_buffered_audio_seconds: int = 8
    barge_in_mode: str = "vad_best_effort"


@dataclass(frozen=True, slots=True)
class LinuxConfig:
    schema_version: int
    server_url: str
    node_id: str
    device_id: str
    label: str
    credential_file: str
    config_dir: Path
    config_path: Path
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    interaction: InteractionConfig = field(default_factory=InteractionConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)

    @property
    def normalized_server_url(self) -> str:
        return normalize_server_url(self.server_url)

    @property
    def server_key(self) -> str:
        return server_key(self.server_url)

    @property
    def runtime_dir(self) -> Path:
        return runtime_dir_for(self)

    @property
    def voice_manifest_path(self) -> Path | None:
        if not self.voice.asr_manifest:
            return None
        candidate = Path(self.voice.asr_manifest).expanduser()
        if not candidate.is_absolute():
            candidate = self.config_dir / candidate
        return candidate.resolve()

    @property
    def credential_path(self) -> Path:
        candidate = Path(self.credential_file)
        if not candidate.is_absolute():
            candidate = self.config_dir / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.config_dir.resolve())
        except ValueError as exc:
            raise ConfigError("credential_file must stay inside the config directory") from exc
        return candidate

    @property
    def state_dir(self) -> Path:
        return (
            _xdg("XDG_STATE_HOME", "~/.local/state")
            / "virtual-life"
            / "linux"
            / self.server_key
            / self.node_id
        )

    def load_token(self) -> str:
        path = self.credential_path
        if not path.is_file():
            raise ConfigError(f"credential file not found: {path}")
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ConfigError(f"credential file {path} must be 0600")
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ConfigError("credential file is empty")
        return token


def _require_bool(section: str, key: str, value) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{section}.{key} must be a boolean")
    return value


def _require_str(section: str, key: str, value, *, allowed=None, nonempty=True) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{section}.{key} must be a string")
    if nonempty and not value.strip():
        raise ConfigError(f"{section}.{key} must not be empty")
    if allowed is not None and value not in allowed:
        raise ConfigError(f"{section}.{key} must be one of {sorted(allowed)}")
    return value


def _require_int(section: str, key: str, value, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{section}.{key} must be an integer between {minimum} and {maximum}")
    return value


def _identifier(section: str, key: str, value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ConfigError(f"{section}.{key} must be a nonempty string up to 128 characters")
    return value.strip()


def _unknown(section: str, keys: dict, allowed: set) -> None:
    extra = set(keys) - allowed
    if extra:
        raise ConfigError(f"{section} has unknown fields: {sorted(extra)}")


def load_config(path: Path | None = None) -> LinuxConfig:
    config_path = Path(path) if path is not None else default_config_path()
    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}")
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    config_dir = config_path.parent
    _unknown("config", raw, _TOP_LEVEL_KEYS)

    schema_version = raw.get("schema_version", CONFIG_SCHEMA_VERSION)
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version: {schema_version!r}")
    server_url = raw.get("server_url", DEFAULT_SERVER_URL)
    normalized = normalize_server_url(server_url)
    node_id = _identifier("config", "node_id", raw.get("node_id", DEFAULT_NODE_ID))
    # A stable installation identity; omitting it reuses the node id.
    device_id = _identifier("config", "device_id", raw.get("device_id") or node_id)
    label = _require_str("config", "label", raw.get("label", "Linux 工作站"))
    credential_file = _require_str(
        "config", "credential_file", raw.get("credential_file", "credentials")
    )

    desktop_raw = raw.get("desktop", {})
    if not isinstance(desktop_raw, dict):
        raise ConfigError("desktop must be a table")
    _unknown("desktop", desktop_raw, _DESKTOP_KEYS)
    desktop = DesktopConfig(
        backend=_require_str(
            "desktop", "backend", desktop_raw.get("backend", "niri"), allowed=_BACKENDS
        ),
        attention_enabled=_require_bool(
            "desktop", "attention_enabled", desktop_raw.get("attention_enabled", False)
        ),
        dms_enabled=_require_bool("desktop", "dms_enabled", desktop_raw.get("dms_enabled", True)),
        fish_activity_enabled=_require_bool(
            "desktop", "fish_activity_enabled", desktop_raw.get("fish_activity_enabled", False)
        ),
        scene_enabled=_require_bool(
            "desktop", "scene_enabled", desktop_raw.get("scene_enabled", False)
        ),
        scene_titles_enabled=_require_bool(
            "desktop", "scene_titles_enabled", desktop_raw.get("scene_titles_enabled", False)
        ),
        scene_max_windows=_require_int(
            "desktop", "scene_max_windows", desktop_raw.get("scene_max_windows", 12), 0, 64
        ),
    )

    interaction_raw = raw.get("interaction", {})
    if not isinstance(interaction_raw, dict):
        raise ConfigError("interaction must be a table")
    _unknown("interaction", interaction_raw, _INTERACTION_KEYS)
    interaction = InteractionConfig(
        terminal=_require_str("interaction", "terminal", interaction_raw.get("terminal", "kitty")),
        auto_connect=_require_bool(
            "interaction", "auto_connect", interaction_raw.get("auto_connect", False)
        ),
        notifications=_require_str(
            "interaction",
            "notifications",
            interaction_raw.get("notifications", "status_only"),
            allowed=_NOTIFICATIONS,
        ),
    )

    reporting_raw = raw.get("reporting", {})
    if not isinstance(reporting_raw, dict):
        raise ConfigError("reporting must be a table")
    _unknown("reporting", reporting_raw, _REPORTING_KEYS)
    reporting = ReportingConfig(
        heartbeat_seconds=_require_int(
            "reporting", "heartbeat_seconds", reporting_raw.get("heartbeat_seconds", 20), 5, 3600
        ),
        attention_seconds=_require_int(
            "reporting", "attention_seconds", reporting_raw.get("attention_seconds", 15), 5, 3600
        ),
        attention_ttl_seconds=_require_int(
            "reporting",
            "attention_ttl_seconds",
            reporting_raw.get("attention_ttl_seconds", 45),
            5,
            86400,
        ),
    )
    if reporting.heartbeat_seconds >= reporting.attention_ttl_seconds:
        raise ConfigError("reporting.heartbeat_seconds must be smaller than attention_ttl_seconds")
    if reporting.attention_seconds >= reporting.attention_ttl_seconds:
        raise ConfigError("reporting.attention_seconds must be smaller than attention_ttl_seconds")

    screen_raw = raw.get("screen", {})
    if not isinstance(screen_raw, dict):
        raise ConfigError("screen must be a table")
    _unknown("screen", screen_raw, _SCREEN_KEYS)
    screen = ScreenConfig(
        enabled=_require_bool("screen", "enabled", screen_raw.get("enabled", False)),
        companion_id=(
            _identifier("screen", "companion_id", screen_raw["companion_id"])
            if screen_raw.get("companion_id")
            else ""
        ),
        profile=_require_str(
            "screen", "profile", screen_raw.get("profile", "balanced"), allowed=_SCREEN_PROFILES
        ),
        mode=_require_str(
            "screen", "mode", screen_raw.get("mode", "continuous"), allowed=_SCREEN_MODES
        ),
        display_id=_require_str("screen", "display_id", screen_raw.get("display_id", "default")),
        capture_binary=_require_str(
            "screen", "capture_binary", screen_raw.get("capture_binary", "grim")
        ),
        capture_source=_require_str(
            "screen",
            "capture_source",
            screen_raw.get("capture_source", "other"),
            allowed=_SCREEN_SOURCES,
        ),
    )

    voice_raw = raw.get("voice", {})
    if not isinstance(voice_raw, dict):
        raise ConfigError("voice must be a table")
    _unknown("voice", voice_raw, _VOICE_KEYS)
    voice = VoiceConfig(
        enabled=_require_bool("voice", "enabled", voice_raw.get("enabled", False)),
        asr_manifest=_require_str(
            "voice", "asr_manifest", voice_raw.get("asr_manifest", ""), nonempty=False
        ),
        input_device=_require_str(
            "voice", "input_device", voice_raw.get("input_device", ""), nonempty=False
        ),
        output_device=_require_str(
            "voice", "output_device", voice_raw.get("output_device", ""), nonempty=False
        ),
        model_threads=_require_int(
            "voice", "model_threads", voice_raw.get("model_threads", 2), 1, 8
        ),
        heartbeat_seconds=_require_int(
            "voice", "heartbeat_seconds", voice_raw.get("heartbeat_seconds", 10), 3, 60
        ),
        max_buffered_audio_seconds=_require_int(
            "voice",
            "max_buffered_audio_seconds",
            voice_raw.get("max_buffered_audio_seconds", 8),
            1,
            8,
        ),
        barge_in_mode=_require_str(
            "voice",
            "barge_in_mode",
            voice_raw.get("barge_in_mode", "vad_best_effort"),
            allowed=_BARGE_IN_MODES,
        ),
    )

    config = LinuxConfig(
        schema_version=schema_version,
        server_url=normalized,
        node_id=node_id,
        device_id=device_id,
        label=label,
        credential_file=credential_file,
        config_dir=config_dir,
        config_path=config_path,
        desktop=desktop,
        interaction=interaction,
        reporting=reporting,
        screen=screen,
        voice=voice,
    )
    # Resolving the credential path validates containment early.
    config.credential_path
    return config
