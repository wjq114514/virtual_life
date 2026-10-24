"""Pluggable model tools with a small built-in scheduling/delegation surface.

Tools are trusted in-process plugins. The registry owns model-facing schemas,
per-purpose availability and bounded result handling; it is deliberately
separate from the dialogue runner and from provider transport.
"""

import importlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .coordinator import DomainError
from .ports import (
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_CONTENT_BYTES,
    MAX_TOOL_DEFINITIONS,
    TOOL_NAME_PATTERN,
    ToolCall,
    ToolDefinition,
)
from .proactivity import (
    MAX_TASK_DELAY_SECONDS,
    MAX_TASK_DETAILS_CHARS,
    MAX_TASK_TITLE_CHARS,
    MIN_TASK_DELAY_SECONDS,
    TASK_STATUS_FILTERS,
)

logger = logging.getLogger(__name__)

TOOL_PURPOSES = ("dialogue", "proactive")
SCHEDULE_TOOL = "schedule_next_proactive"
SCHEDULE_TASK_TOOL = "schedule_task"
LIST_TASKS_TOOL = "list_scheduled_tasks"
CANCEL_TASK_TOOL = "cancel_scheduled_task"
DELEGATE_TOOL = "delegate_task"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """The model-facing contract and routing metadata for one plugin."""

    name: str
    description: str
    parameters: Mapping[str, object]
    purposes: tuple[str, ...] = TOOL_PURPOSES
    source: str = "plugin"


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Trusted invocation context; adapters must still validate arguments."""

    companion_id: str
    command_id: str
    purpose: str
    call: ToolCall
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    content: str
    is_error: bool = False


@runtime_checkable
class ToolPlugin(Protocol):
    """The stable extension point for in-process tools.

    ``describe`` may be called for discovery before every dialogue; it must not
    perform side effects. ``available`` is a cheap per-purpose gate and may use
    runtime configuration. ``execute`` runs only after the dialogue runner has
    accepted a provider tool call.
    """

    def describe(self) -> ToolDescriptor: ...

    def available(self, purpose: str) -> bool: ...

    def execute(self, context: ToolExecutionContext) -> ToolOutcome: ...


@runtime_checkable
class UserTurnHook(Protocol):
    """Optional plugin hook for state that must react to an accepted user turn."""

    def on_user_turn(self, connection, companion_id: str, now, reason: str) -> object: ...


@dataclass(frozen=True, slots=True)
class ToolPluginContext:
    """Services available to configured plugin factories.

    Keep plugins behind this explicit context instead of importing or mutating
    the FastAPI app graph directly.
    """

    coordinator: object
    memory: object
    personality: object
    proactivity: object
    delegation: object
    dialogue: object
    settings: object


def _validate_descriptor(value: object) -> ToolDescriptor:
    if not isinstance(value, ToolDescriptor):
        raise ValueError("Tool plugin describe() must return ToolDescriptor")
    if not isinstance(value.name, str) or not TOOL_NAME_PATTERN.fullmatch(value.name):
        raise ValueError("Tool names must match ^[A-Za-z0-9_-]{1,64}$")
    if not isinstance(value.description, str) or not 1 <= len(value.description) <= 1024:
        raise ValueError("Tool descriptions must contain 1 to 1024 characters")
    if not isinstance(value.source, str) or not 1 <= len(value.source) <= 64:
        raise ValueError("Tool sources must contain 1 to 64 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value.source):
        raise ValueError("Tool sources must not contain control characters")
    try:
        encoded = json.dumps(
            value.parameters,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        parameters = json.loads(encoded)
        encoded_bytes = encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Tool parameters must be bounded JSON") from None
    if not isinstance(parameters, dict):
        raise ValueError("Tool parameters must be a JSON object")
    if len(encoded_bytes) > MAX_TOOL_ARGUMENT_BYTES:
        raise ValueError("Tool parameters exceed the size limit")
    if isinstance(value.purposes, str):
        raise ValueError("Tool purposes must be a sequence")
    try:
        purposes = tuple(value.purposes)
    except TypeError:
        raise ValueError("Tool purposes must be a sequence") from None
    if not purposes or len(set(purposes)) != len(purposes):
        raise ValueError("Tool purposes must be nonempty and unique")
    if any(purpose not in TOOL_PURPOSES for purpose in purposes):
        raise ValueError("Tool purposes must be dialogue and/or proactive")
    return ToolDescriptor(
        name=value.name,
        description=value.description,
        parameters=parameters,
        purposes=purposes,
        source=value.source,
    )


def _validate_outcome(value: object) -> ToolOutcome:
    if not isinstance(value, ToolOutcome) or not isinstance(value.content, str):
        return ToolOutcome(_json({"error": "tool_invalid_outcome"}), True)
    try:
        size = len(value.content.encode("utf-8"))
    except UnicodeError:
        return ToolOutcome(_json({"error": "tool_invalid_outcome"}), True)
    if size > MAX_TOOL_CONTENT_BYTES:
        return ToolOutcome(_json({"error": "tool_result_too_large"}), True)
    return ToolOutcome(value.content, bool(value.is_error))


class ToolRegistry:
    """Bounded registry shared by dialogue, proactive turns and introspection."""

    def __init__(self, *, max_calls_per_turn: int = 1, tools: Iterable[ToolPlugin] = ()):
        if type(max_calls_per_turn) is not int or not 1 <= max_calls_per_turn <= 2:
            raise ValueError("Tool calls per turn must be between 1 and 2")
        self.max_calls_per_turn = max_calls_per_turn
        self._tools: dict[str, ToolPlugin] = {}
        for tool in tools:
            self.register(tool)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def register(self, tool: ToolPlugin, *, replace: bool = False) -> ToolDescriptor:
        if not isinstance(tool, ToolPlugin):
            raise ValueError("Tool must implement describe, available and execute")
        try:
            descriptor = _validate_descriptor(tool.describe())
        except Exception as exc:
            raise ValueError("Tool plugin descriptor is invalid") from exc
        if descriptor.name in self._tools and not replace:
            raise ValueError(f"Tool {descriptor.name!r} is already registered")
        if descriptor.name not in self._tools and len(self._tools) >= MAX_TOOL_DEFINITIONS:
            raise ValueError("Too many tool plugins are registered")
        self._tools[descriptor.name] = tool
        return descriptor

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def descriptors(self, purpose: str | None = None) -> tuple[ToolDescriptor, ...]:
        values = []
        for name, tool in tuple(self._tools.items()):
            try:
                descriptor = _validate_descriptor(tool.describe())
            except Exception:
                logger.exception("Tool plugin %s returned an invalid descriptor", name)
                continue
            if descriptor.name != name:
                logger.error("Tool plugin %s changed its registered name", name)
                continue
            if purpose is not None and purpose not in descriptor.purposes:
                continue
            values.append(descriptor)
        return tuple(values)

    def definitions(self, purpose: str) -> tuple[ToolDefinition, ...]:
        if purpose not in TOOL_PURPOSES:
            return ()
        values = []
        for descriptor in self.descriptors(purpose):
            tool = self._tools[descriptor.name]
            if not self._available(tool, purpose):
                continue
            values.append(
                ToolDefinition(
                    name=descriptor.name,
                    description=descriptor.description,
                    parameters=descriptor.parameters,
                )
            )
        return tuple(values)

    def snapshot(self, purpose: str = "dialogue") -> tuple[dict, ...]:
        values = []
        for descriptor in self.descriptors(purpose):
            tool = self._tools[descriptor.name]
            values.append(
                {
                    "name": descriptor.name,
                    "description": descriptor.description,
                    "parameters": descriptor.parameters,
                    "purposes": list(descriptor.purposes),
                    "source": descriptor.source,
                    "available": self._available(tool, purpose),
                }
            )
        return tuple(values)

    def execute(
        self,
        companion_id: str,
        command_id: str,
        purpose: str,
        call: ToolCall,
    ) -> ToolOutcome:
        if not self._tools:
            return ToolOutcome(_json({"error": "tools_disabled"}), True)
        if purpose not in TOOL_PURPOSES:
            return ToolOutcome(_json({"error": "unknown_tool"}), True)
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolOutcome(_json({"error": "unknown_tool"}), True)
        try:
            descriptor = _validate_descriptor(tool.describe())
        except Exception:
            logger.exception("Tool plugin %s could not describe itself", call.name)
            return ToolOutcome(_json({"error": "tool_failed", "tool": call.name}), True)
        if descriptor.name != call.name:
            logger.error("Tool plugin %s changed its registered name", call.name)
            return ToolOutcome(_json({"error": "tool_failed", "tool": call.name}), True)
        if purpose not in descriptor.purposes or not self._available(tool, purpose):
            return ToolOutcome(_json({"error": "tool_unavailable", "tool": call.name}), True)
        try:
            arguments = json.loads(call.arguments)
        except (ValueError, TypeError, RecursionError):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if not isinstance(arguments, dict):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        context = ToolExecutionContext(
            companion_id=companion_id,
            command_id=command_id,
            purpose=purpose,
            call=call,
            arguments=arguments,
        )
        try:
            outcome = tool.execute(context)
        except Exception:
            logger.exception("Tool plugin %s failed", call.name)
            return ToolOutcome(_json({"error": "tool_failed", "tool": call.name}), True)
        return _validate_outcome(outcome)

    def on_user_turn(self, connection, companion_id: str, now, reason: str = "user_interacting"):
        """Run optional lifecycle hooks in one existing transaction."""
        invoked = 0
        for tool in tuple(self._tools.values()):
            hook = getattr(tool, "on_user_turn", None)
            if not callable(hook):
                continue
            # Hooks share the dialogue transaction. Let failures roll it back
            # rather than committing a partially applied plugin side effect.
            hook(connection, companion_id, now, reason)
            invoked += 1
        return invoked

    def cancel_schedules(self, connection, companion_id, now, reason="user_interacting"):
        """Backward-compatible alias for the pre-plugin lifecycle hook."""
        return self.on_user_turn(connection, companion_id, now, reason) > 0

    @staticmethod
    def _available(tool: ToolPlugin, purpose: str) -> bool:
        try:
            return bool(tool.available(purpose))
        except Exception:
            logger.exception("Tool plugin availability check failed")
            return False


class ScheduleTool:
    """Built-in durable proactive scheduling tool."""

    source = "builtin.schedule"

    def __init__(self, proactivity):
        self.proactivity = proactivity

    def describe(self) -> ToolDescriptor:
        policy = self.proactivity.schedule_policy
        return ToolDescriptor(
            name=SCHEDULE_TOOL,
            description=(
                "Ask the runtime to start one proactive companion remark after a delay. "
                "Only use this when the user has been silent for a long time and a later "
                "remark would be natural. The runtime still applies attention, mode, "
                "budget, lease and cooldown gates."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "delay_seconds": {
                        "type": "integer",
                        "minimum": policy.min_delay_seconds,
                        "maximum": policy.max_delay_seconds,
                        "description": "Delay before the next proactive attempt.",
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "Brief private reason for the delay.",
                    },
                },
                "required": ["delay_seconds"],
                "additionalProperties": False,
            },
            purposes=TOOL_PURPOSES,
            source=self.source,
        )

    def available(self, purpose: str) -> bool:
        return purpose in TOOL_PURPOSES

    def execute(self, context: ToolExecutionContext) -> ToolOutcome:
        if set(context.arguments) - {"delay_seconds", "reason"}:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        delay = context.arguments.get("delay_seconds")
        reason = context.arguments.get("reason")
        if type(delay) is not int:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if reason is not None and not isinstance(reason, str):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        try:
            result = self.proactivity.schedule(
                context.companion_id,
                context.command_id,
                context.call.call_id,
                delay,
                reason,
            )
        except ValueError as exc:
            return ToolOutcome(_json({"error": "invalid_arguments", "detail": str(exc)}), True)
        except Exception:
            logger.exception("Proactive schedule tool failed")
            return ToolOutcome(_json({"error": "schedule_failed"}), True)
        return ToolOutcome(_json(result))

    def on_user_turn(self, connection, companion_id, now, reason):
        return self.proactivity.cancel_schedules(connection, companion_id, now, reason)


class ScheduleTaskTool:
    """Built-in durable planned-task reminder tool."""

    source = "builtin.scheduled_task"

    def __init__(self, proactivity):
        self.proactivity = proactivity

    def describe(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=SCHEDULE_TASK_TOOL,
            description=(
                "Set one durable planned task or reminder for this companion. Use only when "
                "the user asks to be reminded or asks you to mention/do something later. "
                "The task is delivered in a later proactive turn under the normal attention, "
                "presence, lease, budget and quiet gates. It does not execute external actions; "
                "use delegate_task for background work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TASK_TITLE_CHARS,
                        "description": "Short reminder text shown when the task is due.",
                    },
                    "details": {
                        "type": "string",
                        "maxLength": MAX_TASK_DETAILS_CHARS,
                        "description": "Optional extra context for the reminder.",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "minimum": MIN_TASK_DELAY_SECONDS,
                        "maximum": MAX_TASK_DELAY_SECONDS,
                        "description": "Seconds from now until the reminder is due.",
                    },
                },
                "required": ["title", "delay_seconds"],
                "additionalProperties": False,
            },
            purposes=TOOL_PURPOSES,
            source=self.source,
        )

    def available(self, purpose: str) -> bool:
        return purpose in TOOL_PURPOSES

    def execute(self, context: ToolExecutionContext) -> ToolOutcome:
        if set(context.arguments) - {"title", "details", "delay_seconds"}:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        title = context.arguments.get("title")
        details = context.arguments.get("details")
        delay = context.arguments.get("delay_seconds")
        if not isinstance(title, str) or not title.strip():
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if details is not None and not isinstance(details, str):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if type(delay) is not int:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        try:
            result = self.proactivity.schedule_task(
                context.companion_id,
                context.command_id,
                context.call.call_id,
                title,
                details,
                delay,
            )
        except ValueError as exc:
            return ToolOutcome(_json({"error": "invalid_arguments", "detail": str(exc)}), True)
        except Exception:
            logger.exception("Scheduled task tool failed")
            return ToolOutcome(_json({"error": "scheduled_task_failed"}), True)
        return ToolOutcome(_json(result))


class ListScheduledTasksTool:
    """Built-in bounded scheduled-task inspection tool."""

    source = "builtin.scheduled_task_list"

    def __init__(self, proactivity):
        self.proactivity = proactivity

    def describe(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=LIST_TASKS_TOOL,
            description=(
                "List durable planned tasks or reminders for this companion. Use when the "
                "user asks what reminders exist, asks for a task id, or needs the current "
                "task status. Terminal tasks are included unless a status filter is given."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": list(TASK_STATUS_FILTERS),
                        "description": (
                            "Optional status filter. 'active' means pending or queued."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum number of recent tasks to return.",
                    },
                },
                "additionalProperties": False,
            },
            purposes=TOOL_PURPOSES,
            source=self.source,
        )

    def available(self, purpose: str) -> bool:
        return purpose in TOOL_PURPOSES

    def execute(self, context: ToolExecutionContext) -> ToolOutcome:
        if set(context.arguments) - {"status", "limit"}:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        status = context.arguments.get("status")
        limit = context.arguments.get("limit", 10)
        if status is not None and (
            not isinstance(status, str) or status not in TASK_STATUS_FILTERS
        ):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if type(limit) is not int or not 1 <= limit <= 20:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        try:
            result = self.proactivity.list_tasks(context.companion_id, status=status, limit=limit)
        except ValueError as exc:
            return ToolOutcome(_json({"error": "invalid_arguments", "detail": str(exc)}), True)
        except Exception:
            logger.exception("Scheduled task list tool failed")
            return ToolOutcome(_json({"error": "scheduled_task_list_failed"}), True)
        summary = {
            "status": result["status"],
            "total": result["total"],
            "limit": result["limit"],
            "tasks": [
                {
                    "task_id": item["task_id"],
                    "title": item["title"],
                    "status": item["status"],
                    "reason": item["reason"],
                    "due_at": item["due_at"],
                }
                for item in result["items"]
            ],
        }
        return ToolOutcome(_json(summary))


class CancelScheduledTaskTool:
    """Built-in planned-task cancellation tool."""

    source = "builtin.scheduled_task_cancel"

    def __init__(self, proactivity):
        self.proactivity = proactivity

    def describe(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=CANCEL_TASK_TOOL,
            description=(
                "Cancel one active planned task or reminder. Supply task_id when known; "
                "otherwise supply the exact title. If multiple active tasks share the title, "
                "the tool returns scheduled_task_ambiguous, so list tasks or ask the user to "
                "disambiguate. Cancelling an already cancelled task is idempotent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "Exact scheduled task id, when already known.",
                    },
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TASK_TITLE_CHARS,
                        "description": (
                            "Exact title of the active task to cancel, case-insensitive."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            purposes=TOOL_PURPOSES,
            source=self.source,
        )

    def available(self, purpose: str) -> bool:
        return purpose in TOOL_PURPOSES

    def execute(self, context: ToolExecutionContext) -> ToolOutcome:
        if set(context.arguments) - {"task_id", "title"}:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        task_id = context.arguments.get("task_id")
        title = context.arguments.get("title")
        if (task_id is None) == (title is None):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if task_id is not None and (
            not isinstance(task_id, str) or not 1 <= len(task_id.strip()) <= 128
        ):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if title is not None and (
            not isinstance(title, str) or not 1 <= len(title.strip()) <= MAX_TASK_TITLE_CHARS
        ):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        try:
            if task_id is not None:
                result = self.proactivity.cancel_task(context.companion_id, task_id.strip())
            else:
                result = self.proactivity.cancel_task_by_title(context.companion_id, title)
        except ValueError as exc:
            return ToolOutcome(_json({"error": "invalid_arguments", "detail": str(exc)}), True)
        except DomainError as exc:
            return ToolOutcome(_json({"error": exc.code, "message": exc.message}), True)
        except Exception:
            logger.exception("Scheduled task cancel tool failed")
            return ToolOutcome(_json({"error": "scheduled_task_cancel_failed"}), True)
        return ToolOutcome(_json(result))


class DelegationTool:
    """Built-in enqueue-only external delegation tool."""

    source = "builtin.delegation"

    def __init__(self, delegation):
        self.delegation = delegation

    def describe(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=DELEGATE_TOOL,
            description=(
                "Hand a self-contained background task to the configured external agent. "
                "The task is queued and runs without blocking this reply. Use it only for "
                "work the user explicitly asked for."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8000,
                        "description": "Complete, self-contained task instructions.",
                    },
                    "agent": {
                        "type": "string",
                        "maxLength": 64,
                        "description": "Optional informational agent label.",
                    },
                },
                "required": ["instruction"],
                "additionalProperties": False,
            },
            purposes=TOOL_PURPOSES,
            source=self.source,
        )

    def available(self, purpose: str) -> bool:
        return purpose in TOOL_PURPOSES and bool(self.delegation.config.enabled)

    def execute(self, context: ToolExecutionContext) -> ToolOutcome:
        if set(context.arguments) - {"instruction", "agent"}:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        instruction = context.arguments.get("instruction")
        agent = context.arguments.get("agent")
        if not isinstance(instruction, str) or not instruction.strip():
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        if agent is not None and not isinstance(agent, str):
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        try:
            task = self.delegation.enqueue(
                context.companion_id,
                context.command_id,
                context.call.call_id,
                instruction,
                agent,
            )
        except ValueError:
            return ToolOutcome(_json({"error": "invalid_arguments"}), True)
        except Exception:
            logger.exception("Delegation tool failed")
            return ToolOutcome(_json({"error": "delegation_failed"}), True)
        return ToolOutcome(
            _json(
                {
                    "status": task["status"],
                    "task_id": task["task_id"],
                    "message": "Task queued for the configured agent.",
                }
            )
        )


def build_tool_plugins(paths: Iterable[str], context: ToolPluginContext) -> tuple[ToolPlugin, ...]:
    """Load ``module:factory`` entries from trusted configuration.

    Each factory receives the explicit service context and returns one plugin or
    a finite iterable of plugins. Import and factory failures abort startup with
    a bounded message rather than silently disabling configured behavior.
    """
    plugins = []
    for path in paths:
        module_name, separator, attribute = path.partition(":")
        if not separator:
            raise ValueError(f"Invalid tool plugin path: {path}")
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise ValueError(f"Unable to import tool plugin {path}") from exc
        factory = getattr(module, attribute, None)
        if not callable(factory):
            raise ValueError(f"Tool plugin factory is missing: {path}")
        try:
            produced = factory(context)
        except Exception as exc:
            raise ValueError(f"Tool plugin factory failed: {path}") from exc
        if isinstance(produced, ToolPlugin):
            plugins.append(produced)
            continue
        try:
            entries = list(produced)
        except TypeError:
            raise ValueError(f"Tool plugin factory returned no plugins: {path}") from None
        for entry in entries:
            if not isinstance(entry, ToolPlugin):
                raise ValueError(f"Tool plugin factory returned an invalid plugin: {path}")
            plugins.append(entry)
    return tuple(plugins)
