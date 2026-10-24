"""Unified console and rotating-file logging with request correlation.

The operator-facing format follows AstrBot's idea of one compact line per event:
a timestamp, a short level, the emitting module and an optional request id.
The standard library is used directly so embedded runtimes stay dependency-free.

Configuration is deliberately centralized here.  Application modules keep using
``logging.getLogger(__name__)`` and never install handlers of their own.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SHORT_LEVELS = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "CRIT",
}
_LEVEL_COLORS = {
    "DEBUG": "\x1b[36m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
# Third-party loggers that are chatty at INFO or DEBUG and rarely useful.
_NOISY_LOGGERS = (
    "asyncio",
    "httpcore",
    "httpx",
    "multipart",
    "python_multipart",
    "urllib3",
)
_HANDLER_FLAG = "_virtual_life_handler"
_REQUEST_ID = ContextVar("virtual_life_request_id", default=None)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_PREVIOUS_LEVELS: dict[str, int] | None = None


def current_request_id() -> str | None:
    """Return the request id bound to the current async context, if any."""
    return _REQUEST_ID.get()


@contextmanager
def request_context(request_id: str):
    """Bind a request id for background work started by an HTTP request."""
    token = _REQUEST_ID.set(request_id)
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)


class RequestContextFilter(logging.Filter):
    """Attach the request id to every record handled by the root handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = _REQUEST_ID.get()
        return True


def _short_level(level_name: str) -> str:
    return _SHORT_LEVELS.get(level_name, level_name[:5].upper())


def _source(record: logging.LogRecord) -> str:
    name = record.name
    if name.startswith("virtual_life."):
        name = name[len("virtual_life.") :]
    elif name == "virtual_life":
        name = "runtime"
    return f"{name}:{record.lineno}"


class _RuntimeFormatter(logging.Formatter):
    """Console/file formatter with optional ANSI colors and request tags."""

    def __init__(self, *, color: bool, full_date: bool):
        super().__init__()
        self.color = color
        self.full_date = full_date

    def formatTime(self, record: logging.LogRecord, _datefmt: str | None = None) -> str:
        stamp = time.strftime(
            "%Y-%m-%d %H:%M:%S" if self.full_date else "%H:%M:%S", time.localtime(record.created)
        )
        return f"{stamp}.{int(record.msecs):03d}"

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record)
        level = _short_level(record.levelname)
        source = _source(record)
        if self.color:
            level = f"{_LEVEL_COLORS.get(record.levelname, '')}{level:<5}{_RESET}"
            timestamp = f"{_DIM}{timestamp}{_RESET}"
            source = f"{_DIM}{source}{_RESET}"
        else:
            level = f"{level:<5}"
        line = f"[{timestamp}] [{level}] [{source}]"
        request_id = getattr(record, "request_id", None)
        if request_id:
            line += f" [req={request_id}]"
        message = record.getMessage()
        if self.color and record.levelno >= logging.ERROR:
            message = f"{_LEVEL_COLORS[record.levelname]}{message}{_RESET}"
        line += f" {message}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


def _color_enabled(stream, requested: bool | None) -> bool:
    if requested is not None:
        return requested
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _remember_level(name: str) -> None:
    global _PREVIOUS_LEVELS
    if _PREVIOUS_LEVELS is None:
        _PREVIOUS_LEVELS = {}
    if name not in _PREVIOUS_LEVELS:
        _PREVIOUS_LEVELS[name] = logging.getLogger("" if name == "root" else name).level


def _set_level(name: str, value: int) -> None:
    _remember_level(name)
    logging.getLogger("" if name == "root" else name).setLevel(value)


def _remove_handlers() -> None:
    root = logging.getLogger()
    for handler in tuple(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)
            handler.close()


def _normalize_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    value = logging.getLevelName(str(level).strip().upper())
    if not isinstance(value, int):
        raise ValueError(f"Unknown log level: {level}")
    return value


def configure_logging(
    settings=None,
    *,
    stream=None,
    color: bool | None = None,
) -> None:
    """Install console and rotating-file handlers on the root logger.

    ``settings`` is any object exposing the ``log_*`` fields of
    :class:`virtual_life.settings.Settings`.  Repeated calls replace only the
    handlers installed by this module, making startup and reloads idempotent.
    """
    if settings is None:
        level_value = logging.INFO
        console_enabled = True
        file_enabled = False
        file_path = None
        max_mb = 10
        backup_count = 3
    else:
        level_value = _normalize_level(settings.log_level)
        console_enabled = settings.log_console
        file_enabled = settings.log_file_enabled
        file_path = settings.log_file_path
        max_mb = settings.log_file_max_mb
        backup_count = settings.log_file_backup_count

    _remove_handlers()
    _set_level("root", level_value)
    _set_level("virtual_life", level_value)
    # Align uvicorn with the unified format when its own logging config is off.
    _set_level("uvicorn", level_value)
    _set_level("uvicorn.error", level_value)
    _set_level("uvicorn.access", logging.WARNING)
    for name in _NOISY_LOGGERS:
        _set_level(name, logging.WARNING)

    context_filter = RequestContextFilter()
    console_handler = file_handler = None

    if console_enabled:
        target = stream if stream is not None else sys.stdout
        console_handler = logging.StreamHandler(target)
        console_handler.setLevel(level_value)
        console_handler.addFilter(context_filter)
        console_handler.setFormatter(
            _RuntimeFormatter(color=_color_enabled(target, color), full_date=False)
        )
        setattr(console_handler, _HANDLER_FLAG, True)
        logging.getLogger().addHandler(console_handler)

    if file_enabled and file_path:
        try:
            path = Path(file_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=max(1, int(max_mb)) * 1024 * 1024,
                backupCount=max(1, int(backup_count)),
                encoding="utf-8",
            )
        except (OSError, ValueError) as exc:
            # A read-only or invalid path must not prevent the runtime from starting.
            logging.getLogger("virtual_life.log").warning(
                "File logging disabled for %s: %s", file_path, exc
            )
        else:
            file_handler.setLevel(level_value)
            file_handler.addFilter(context_filter)
            file_handler.setFormatter(_RuntimeFormatter(color=False, full_date=True))
            setattr(file_handler, _HANDLER_FLAG, True)
            logging.getLogger().addHandler(file_handler)

    if console_handler is None and file_handler is None:
        # Keep lastResort from printing unformatted warnings to stderr.
        silent = logging.NullHandler()
        setattr(silent, _HANDLER_FLAG, True)
        logging.getLogger().addHandler(silent)

    logging.getLogger("virtual_life").info(
        "Logging ready (level=%s, console=%s, file=%s)",
        logging.getLevelName(level_value),
        console_enabled,
        file_path if file_handler is not None else "off",
    )


def reset_logging() -> None:
    """Remove handlers and restore levels changed by :func:`configure_logging`.

    Intended for tests and embedded reconfiguration; the running service never
    needs to call it.
    """
    global _PREVIOUS_LEVELS
    _remove_handlers()
    for name, previous in (_PREVIOUS_LEVELS or {}).items():
        logging.getLogger("" if name == "root" else name).setLevel(previous)
    _PREVIOUS_LEVELS = None


def _clean(value: str) -> str:
    return _CONTROL_CHARS.sub("?", value)[:512]


def _request_id(headers) -> str:
    for key, value in headers:
        if key.lower() != b"x-request-id":
            continue
        try:
            candidate = value.decode("ascii")
        except UnicodeDecodeError:
            continue
        if _SAFE_REQUEST_ID.fullmatch(candidate):
            return candidate
    return uuid.uuid4().hex[:12]


class RequestLogMiddleware:
    """Pure ASGI middleware that logs one line per HTTP request.

    The middleware also assigns a bounded request id, echoes it in
    ``X-Request-ID`` and binds it to logs emitted while the request runs.  Query
    strings, headers and bodies are never logged.
    """

    def __init__(self, app, *, access: bool = True):
        self.app = app
        self.access = access
        self.logger = logging.getLogger("virtual_life.http")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_id = _request_id(scope.get("headers", ()))
        method = _clean(scope.get("method", ""))
        path = _clean(scope.get("path", ""))
        client = scope.get("client")
        peer = _clean(client[0]) if client else "-"
        started = time.perf_counter()
        status = 0
        token = _REQUEST_ID.set(request_id)

        async def send_with_request_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", ())
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            return await self.app(scope, receive, send_with_request_id)
        except Exception:
            status = 500
            self.logger.exception(
                "%s %s failed after %.1f ms (client=%s, request_id=%s)",
                method,
                path,
                (time.perf_counter() - started) * 1000,
                peer,
                request_id,
            )
            raise
        finally:
            if self.access:
                duration = (time.perf_counter() - started) * 1000
                if path.startswith("/ui/") or path == "/healthz":
                    level = logging.DEBUG
                elif status >= 500:
                    level = logging.ERROR
                elif status >= 400 or status == 0:
                    level = logging.WARNING
                else:
                    level = logging.INFO
                self.logger.log(
                    level,
                    "%s %s -> %s in %.1f ms (client=%s)",
                    method,
                    path,
                    status or "disconnected",
                    duration,
                    peer,
                    extra={"request_id": request_id},
                )
            _REQUEST_ID.reset(token)
