"""Bounded delegation to a configured external agent.

The default executor never uses a shell and inherits only allowlisted environment
variables. Queueing, terminal receipts and events stay in this service so custom
executors cannot bypass idempotency or restart recovery.
"""

import hashlib
import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Protocol, runtime_checkable

MAX_INSTRUCTION_CHARS = 8_000
MAX_AGENT_CHARS = 64
MAX_OUTPUT_CHARS = 65_536
MAX_EVENT_OUTPUT_CHARS = 4_000
MAX_DELEGATION_STATUSES = frozenset({"completed", "failed"})

logger = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class DelegationConfig:
    enabled: bool = False
    command: tuple[str, ...] = ()
    working_dir: str = ""
    timeout_seconds: float = 600.0
    max_output_chars: int = 16_000
    env_allowlist: tuple[str, ...] = ("PATH", "HOME")

    def __post_init__(self) -> None:
        command = tuple(self.command)
        allowlist = tuple(self.env_allowlist)
        if any(not isinstance(part, str) or not part or "\x00" in part for part in command):
            raise ValueError("Delegation command arguments must be nonempty strings")
        if len(command) > 64:
            raise ValueError("Delegation command has too many arguments")
        if command and not any("{prompt}" in part for part in command):
            raise ValueError("Delegation command must contain a {prompt} argument")
        if not isinstance(self.working_dir, str) or "\x00" in self.working_dir:
            raise ValueError("Delegation working directory must be a string without null bytes")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1 <= self.timeout_seconds <= 86_400
        ):
            raise ValueError("Delegation timeout must be between 1 and 86400 seconds")
        if (
            type(self.max_output_chars) is not int
            or not 256 <= self.max_output_chars <= MAX_OUTPUT_CHARS
        ):
            raise ValueError("Delegation output limit must be between 256 and 65536 characters")
        if len(allowlist) > 32 or len(set(allowlist)) != len(allowlist):
            raise ValueError("Delegation environment allowlist must be unique and bounded")
        if any(not isinstance(name, str) or not name.isidentifier() for name in allowlist):
            raise ValueError("Delegation environment names must be identifiers")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "env_allowlist", allowlist)


@dataclass(frozen=True, slots=True)
class DelegationOutcome:
    status: str
    output: str
    error_code: str | None = None
    truncated: bool = False


@runtime_checkable
class DelegationExecutor(Protocol):
    """Replaceable agent backend; the service owns queueing and receipts."""

    def run(self, instruction: str, agent: str | None) -> DelegationOutcome: ...


class SubprocessDelegationExecutor:
    """Bounded argv subprocess backend with a small inherited environment."""

    def __init__(self, config: DelegationConfig, *, working_dir):
        if not isinstance(config, DelegationConfig):
            raise ValueError("A valid delegation configuration is required")
        if config.enabled and not config.command:
            raise ValueError("Enabled delegation requires a command for the subprocess executor")
        self.config = config
        self.working_dir = Path(working_dir)

    def run(self, instruction: str, agent: str | None = None) -> DelegationOutcome:
        if not self.config.enabled:
            return DelegationOutcome("failed", "", "delegation_disabled")
        if not self.working_dir.is_dir():
            return DelegationOutcome("failed", "", "delegation_working_dir_missing")
        argv = [part.replace("{prompt}", instruction) for part in self.config.command]
        environment = {
            name: os.environ[name] for name in self.config.env_allowlist if name in os.environ
        }
        limit = self.config.max_output_chars
        byte_limit = limit * 4
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(self.working_dir),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError):
            return DelegationOutcome("failed", "", "delegation_spawn_failed")
        captured = {"stdout": bytearray(), "stderr": bytearray(), "truncated": False}
        exceeded = threading.Event()

        def drain(stream, key):
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    sink = captured[key]
                    remaining = byte_limit - len(sink)
                    if remaining > 0:
                        sink.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        captured["truncated"] = True
                        exceeded.set()
                        self._terminate(process)
                        return
            except (OSError, ValueError):
                return

        threads = [
            threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill(process)
        for thread in threads:
            thread.join(timeout=5)
        stdout = bytes(captured["stdout"]).decode("utf-8", "replace").strip()
        stderr = bytes(captured["stderr"]).decode("utf-8", "replace").strip()
        if timed_out:
            return DelegationOutcome("failed", stdout[:limit], "delegation_timeout")
        if exceeded.is_set():
            return DelegationOutcome("failed", stdout[:limit], "delegation_output_too_large", True)
        if process.returncode != 0:
            return DelegationOutcome("failed", (stdout or stderr)[:limit], "delegation_failed")
        if not stdout:
            return DelegationOutcome("failed", "", "delegation_empty_output")
        truncated = len(stdout) > limit
        return DelegationOutcome("completed", stdout[:limit], None, truncated)

    @staticmethod
    def _terminate(process):
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return

    @staticmethod
    def _kill(process):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                return


class DelegationService:
    def __init__(
        self,
        coordinator,
        *,
        config: DelegationConfig | None = None,
        executor: DelegationExecutor | None = None,
    ):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.config = config or DelegationConfig()
        default_dir = Path(self.store.database_path).resolve().parent
        self.working_dir = (
            Path(self.config.working_dir).expanduser().resolve()
            if self.config.working_dir.strip()
            else default_dir
        )
        if executor is not None and not isinstance(executor, DelegationExecutor):
            raise ValueError("Delegation executor must implement run(instruction, agent)")
        self.executor = executor or SubprocessDelegationExecutor(
            self.config, working_dir=self.working_dir
        )
        with self.store.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS delegated_tasks (
                    task_id TEXT PRIMARY KEY, companion_id TEXT NOT NULL,
                    command_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                    agent TEXT, instruction TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    result_text TEXT, error_code TEXT, truncated INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(command_id, tool_call_id))"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS delegated_task_queue
                    ON delegated_tasks(status, created_at)"""
            )

    def _now(self):
        now = self.coordinator.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    @staticmethod
    def _task(row):
        return {
            "task_id": row["task_id"],
            "companion_id": row["companion_id"],
            "command_id": row["command_id"],
            "tool_call_id": row["tool_call_id"],
            "agent": row["agent"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "result_text": row["result_text"],
            "error_code": row["error_code"],
            "truncated": bool(row["truncated"]),
        }

    def enqueue(
        self,
        companion_id: str,
        command_id: str,
        tool_call_id: str,
        instruction: str,
        agent: str | None = None,
    ) -> dict:
        instruction = instruction.strip()
        if not 1 <= len(instruction) <= MAX_INSTRUCTION_CHARS:
            raise ValueError("Delegated instruction is outside the allowed length")
        if agent is not None:
            agent = agent.strip()
            if not 1 <= len(agent) <= MAX_AGENT_CHARS:
                raise ValueError("Delegated agent name is outside the allowed length")
        task_id = (
            "delegation-"
            + hashlib.sha256(f"{companion_id}\0{command_id}\0{tool_call_id}".encode()).hexdigest()
        )
        with self.store.transaction(write=True) as connection:
            self.coordinator._row(connection, "companions", "companion_id", companion_id)
            existing = connection.execute(
                "SELECT * FROM delegated_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing is not None:
                return self._task(existing)
            now = self._now()
            connection.execute(
                """INSERT INTO delegated_tasks
                    (task_id, companion_id, command_id, tool_call_id, agent, instruction,
                     status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    task_id,
                    companion_id,
                    command_id,
                    tool_call_id,
                    agent,
                    instruction,
                    _timestamp(now),
                ),
            )
            self.coordinator._emit(
                connection,
                now,
                "companion.delegation_queued",
                {"task_id": task_id, "agent": agent},
                companion_id=companion_id,
            )
            row = connection.execute(
                "SELECT * FROM delegated_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._task(row)

    def recover(self) -> int:
        with self.store.transaction(write=True) as connection:
            rows = connection.execute(
                "SELECT * FROM delegated_tasks WHERE status='running'"
            ).fetchall()
            now = self._now()
            for row in rows:
                connection.execute(
                    """UPDATE delegated_tasks SET status='failed', finished_at=?,
                           error_code='delegation_interrupted' WHERE task_id=?""",
                    (_timestamp(now), row["task_id"]),
                )
                self.coordinator._emit(
                    connection,
                    now,
                    "companion.delegation_completed",
                    {
                        "task_id": row["task_id"],
                        "status": "failed",
                        "agent": row["agent"],
                        "output": "",
                        "truncated": False,
                        "error_code": "delegation_interrupted",
                    },
                    companion_id=row["companion_id"],
                )
        return len(rows)

    def process_next(self) -> bool:
        with self.store.transaction(write=True) as connection:
            row = connection.execute(
                """SELECT * FROM delegated_tasks WHERE status='pending'
                    ORDER BY created_at, task_id LIMIT 1"""
            ).fetchone()
            if row is None:
                return False
            now = self._now()
            connection.execute(
                "UPDATE delegated_tasks SET status='running', started_at=? WHERE task_id=?",
                (_timestamp(now), row["task_id"]),
            )
            task = dict(row)
        if not self.config.enabled:
            outcome = DelegationOutcome("failed", "", "delegation_disabled")
        else:
            try:
                outcome = self.executor.run(task["instruction"], task["agent"])
            except Exception:
                logger.exception("Delegation executor failed for %s", task["task_id"])
                outcome = DelegationOutcome("failed", "", "delegation_executor_error")
        outcome = self._normalize_outcome(outcome)
        with self.store.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM delegated_tasks WHERE task_id=?", (task["task_id"],)
            ).fetchone()
            if current is None or current["status"] != "running":
                return True
            now = self._now()
            connection.execute(
                """UPDATE delegated_tasks SET status=?, finished_at=?, result_text=?,
                       error_code=?, truncated=? WHERE task_id=?""",
                (
                    outcome.status,
                    _timestamp(now),
                    outcome.output,
                    outcome.error_code,
                    int(outcome.truncated),
                    task["task_id"],
                ),
            )
            self.coordinator._emit(
                connection,
                now,
                "companion.delegation_completed",
                {
                    "task_id": task["task_id"],
                    "status": outcome.status,
                    "agent": task["agent"],
                    "output": outcome.output[:MAX_EVENT_OUTPUT_CHARS],
                    "truncated": outcome.truncated,
                    "error_code": outcome.error_code,
                },
                companion_id=task["companion_id"],
            )
        return True

    def _normalize_outcome(self, value: object) -> DelegationOutcome:
        if not isinstance(value, DelegationOutcome):
            return DelegationOutcome("failed", "", "delegation_executor_error")
        status = (
            value.status
            if isinstance(value.status, str) and value.status in MAX_DELEGATION_STATUSES
            else "failed"
        )
        output = value.output if isinstance(value.output, str) else ""
        error_code = value.error_code
        if error_code is not None and not isinstance(error_code, str):
            error_code = "delegation_executor_error"
        if status == "failed" and error_code is None:
            error_code = "delegation_executor_error"
        limit = self.config.max_output_chars
        truncated = bool(value.truncated) or len(output) > limit
        return DelegationOutcome(status, output[:limit], error_code, truncated)

    def list(self, companion_id: str, *, limit: int = 20, offset: int = 0) -> dict:
        self.coordinator.get_companion(companion_id)
        with self.store.transaction() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM delegated_tasks WHERE companion_id=?", (companion_id,)
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT * FROM delegated_tasks WHERE companion_id=?
                    ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?""",
                (companion_id, limit, offset),
            ).fetchall()
            return {
                "items": [self._task(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
