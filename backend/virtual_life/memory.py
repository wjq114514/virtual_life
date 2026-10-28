"""Durable local companion memory, bounded recall and a model-token quota ledger.

No method in this module invokes an external model. The token estimator is an
approximation, not a tokenizer or a price calculator. Quotas are global to one
runtime database and reset by UTC reservation day; settlement records real
observed usage even when a provider exceeded the original reservation.
"""

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .memory_logs import MemoryLogService
from .ports import TokenEstimator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
RetentionPolicy = Literal["auto", "permanent", "stable", "significant_episode", "routine_episode"]
MemoryKind = Literal["episode", "fact", "preference", "relationship"]
TokenCount = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
_KINDS = ("episode", "fact", "preference", "relationship")
_Model = TypeVar("_Model", bound=BaseModel)
_RECALL_HEADER = "Stored companion memories:\n"
_CONSOLIDATION_HEADER = "Source episodes for a derived summary:\n"


class MemoryError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _utc_time(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
        ):
            raise ValueError("RFC3339 format required")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or parsed.tzinfo is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ValueError("Expected RFC3339 timestamp with timezone") from exc


class MemoryMetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Identifier
    expected_revision: int = Field(ge=1, le=2**63 - 1)
    tags: list[Tag] | None = Field(default=None, max_length=32)
    importance: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    confirmed: bool | None = None
    pinned: bool | None = None
    retention_policy: RetentionPolicy | None = None

    @model_validator(mode="after")
    def nonempty(self) -> "MemoryMetadataUpdate":
        for key in ("tags", "importance", "confidence", "confirmed", "pinned", "retention_policy"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        if type(self) is MemoryMetadataUpdate and not any(
            getattr(self, key) is not None
            for key in (
                "tags",
                "importance",
                "confidence",
                "confirmed",
                "pinned",
                "retention_policy",
            )
        ):
            raise ValueError("At least one metadata field must be nonnull")
        return self


class MemoryCorrection(MemoryMetadataUpdate):
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768)
    ]
    source_event_id: Identifier
    source_node_id: Identifier | None = None
    valid_from: str | None = Field(default=None, max_length=64)
    valid_to: str | None = Field(default=None, max_length=64)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def timestamps(cls, value: str | None) -> str | None:
        return _utc_time(value)

    @model_validator(mode="after")
    def interval(self) -> "MemoryCorrection":
        for key in ("valid_from", "valid_to"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command_id: Identifier
    kind: MemoryKind
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768)
    ]
    source_event_id: Identifier
    source_node_id: Identifier | None = None
    semantic_key: Identifier | None = None
    occurred_at: str | None = Field(default=None, max_length=64)
    episode_key: Identifier | None = None
    valid_from: str | None = Field(default=None, max_length=64)
    valid_to: str | None = Field(default=None, max_length=64)

    @field_validator("occurred_at", "valid_from", "valid_to")
    @classmethod
    def timestamps(cls, value: str | None) -> str | None:
        return _utc_time(value)

    @model_validator(mode="after")
    def time_scope(self) -> "MemoryWrite":
        if self.kind == "episode" and self.semantic_key is not None:
            raise ValueError("semantic_key is only valid for stable memories")
        if self.kind != "episode" and self.episode_key is not None:
            raise ValueError("episode_key is only valid for episodes")
        if self.kind == "episode" and (self.valid_from or self.valid_to):
            raise ValueError("Validity intervals apply to stable memories")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self

    tags: list[Tag] = Field(default_factory=list, max_length=32)
    importance: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    confidence: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    confirmed: bool = False
    pinned: bool = False
    retention_policy: RetentionPolicy = "auto"

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, values: list[str]) -> list[str]:
        return _merge_tags([], values)


class MemoryRecall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    layer: Literal["episodic", "semantic"] | None = None
    query: str = Field(default="", max_length=2048)
    semantic_key: Identifier | None = None
    token_budget: int = Field(default=800, ge=0, le=64_000)
    max_items: int = Field(default=8, ge=1, le=100)


class MemoryDecayPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = True
    routine_half_life_days: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    routine_floor: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    significant_half_life_days: float = Field(default=180.0, gt=0, allow_inf_nan=False)
    significant_floor: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)
    stable_half_life_days: float = Field(default=365.0, gt=0, allow_inf_nan=False)
    stable_floor: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    permanent_candidate_limit: int = Field(default=128, ge=1, le=10_000)
    semantic_salience_weight: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    reinforcement_min_coverage: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)


def _effective_retention_policy(row) -> str:
    if row["pinned"]:
        return "permanent"
    policy = row["retention_policy"]
    if policy != "auto":
        return policy
    key = _normalize(row["semantic_key"] or "")
    if row["kind"] == "fact" and re.search(
        r"(?:^|[:._/\-])(?:birthday|birth_date|date_of_birth|生日|出生日期)$", key
    ):
        return "permanent"
    return "routine_episode" if row["kind"] == "episode" else "stable"


def _permanent_clause(alias: str = "m") -> str:
    """SQL protected lane predicate; callers supply an internal table alias."""
    key = f"memory_normalize(COALESCE({alias}.semantic_key, ''))"
    suffixes = ("birthday", "birth_date", "date_of_birth", "生日", "出生日期")
    birthday = []
    for suffix in suffixes:
        birthday.append(f"{key} = '{suffix}'")
        for delimiter in (":", ".", "_", "/", "-"):
            ending = delimiter + suffix
            birthday.append(f"substr({key}, -{len(ending)}) = '{ending}'")
    return (
        f"({alias}.pinned = 1 OR {alias}.retention_policy = 'permanent' OR "
        f"({alias}.retention_policy = 'auto' AND {alias}.kind = 'fact' AND "
        f"({' OR '.join(birthday)})))"
    )


class MemoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decay: MemoryDecayPolicy = Field(default_factory=MemoryDecayPolicy)
    default_recall_tokens: int = Field(default=800, ge=0, le=64_000)
    max_recall_tokens: int = Field(default=4096, ge=0, le=64_000)
    default_max_items: int = Field(default=8, ge=1, le=100)
    max_recall_items: int = Field(default=50, ge=1, le=100)
    lexical_candidate_limit: int = Field(default=512, ge=1, le=10_000)
    background_candidate_limit: int = Field(default=128, ge=1, le=10_000)
    recency_half_life_days: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    consolidation_threshold: int = Field(default=20, ge=2, le=10_000)
    consolidation_batch_size: int = Field(default=32, ge=1, le=256)
    consolidation_input_token_limit: int = Field(default=4000, ge=1, le=64_000)
    consolidation_output_token_limit: int = Field(default=400, ge=0, le=64_000)
    daily_model_token_limit: TokenCount = 10_000
    per_task_model_token_limit: TokenCount = 5000

    @model_validator(mode="after")
    def consistent_defaults(self) -> "MemoryPolicy":
        if self.default_recall_tokens > self.max_recall_tokens:
            raise ValueError("default_recall_tokens must fit max_recall_tokens")
        if self.default_max_items > self.max_recall_items:
            raise ValueError("default_max_items must fit max_recall_items")
        return self


class _Reservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: Identifier
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    input_tokens: TokenCount
    output_tokens: TokenCount

    @model_validator(mode="after")
    def nonempty_reservation(self) -> "_Reservation":
        if self.input_tokens + self.output_tokens == 0:
            raise ValueError("Reserve at least one model token")
        return self


class _Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input_tokens: TokenCount
    output_tokens: TokenCount


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _merge_tags(old: list[str], new: list[str]) -> list[str]:
    result = []
    seen = set()
    for tag in old + new:
        normalized = _normalize(tag)
        if normalized not in seen:
            result.append(tag)
            seen.add(normalized)
    return result


def _is_cjk(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x323AF
        or 0x3040 <= value <= 0x30FF
        or 0xAC00 <= value <= 0xD7AF
    )


def _terms(text: str) -> set[str]:
    """Latin words and CJK uni/bi/trigrams, requiring no segmentation dependency."""
    result: set[str] = set()
    run: list[str] = []
    cjk_run = False

    def flush() -> None:
        if not run:
            return
        word = "".join(run)
        if cjk_run:
            for width in (1, 2, 3):
                result.update(word[index : index + width] for index in range(len(word) - width + 1))
        else:
            result.add(word)
        run.clear()

    for character in _normalize(text):
        if not character.isalnum():
            flush()
            continue
        is_cjk = _is_cjk(character)
        if run and is_cjk != cjk_run:
            flush()
        cjk_run = is_cjk
        run.append(character)
    flush()
    return result


def _term_weight(term: str) -> float:
    if _is_cjk(term[0]):
        return {1: 0.25, 2: 1.0, 3: 1.5}.get(len(term), 1.5)
    return 2.0


class ApproximateTokenEstimator:
    """CJK characters plus roughly four UTF-8 bytes per other token.

    This bounds *estimated* tokens only. A real model tokenizer may differ and
    can replace this implementation through the TokenEstimator contract.
    """

    name = "mixed_unicode_approx_v1"

    def estimate(self, text: str) -> int:
        cjk = sum(_is_cjk(character) for character in text)
        other_bytes = sum(
            len(character.encode("utf-8")) for character in text if not _is_cjk(character)
        )
        return cjk + math.ceil(other_bytes / 4)


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memory_records (
        memory_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('episode','fact','preference','relationship')),
        content TEXT NOT NULL,
        normalized_content TEXT NOT NULL,
        semantic_key TEXT,
        tags_json TEXT NOT NULL,
        importance REAL NOT NULL,
        confidence REAL NOT NULL,
        confirmed INTEGER NOT NULL,
        pinned INTEGER NOT NULL,
        active INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS memory_active_content
        ON memory_records(companion_id, kind, normalized_content) WHERE active = 1""",
    """CREATE UNIQUE INDEX IF NOT EXISTS memory_active_semantic_key
        ON memory_records(companion_id, semantic_key)
        WHERE active = 1 AND semantic_key IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS memory_record_scope
        ON memory_records(companion_id, active, updated_at)""",
    """CREATE TABLE IF NOT EXISTS memory_provenance (
        memory_id TEXT NOT NULL REFERENCES memory_records(memory_id) ON DELETE CASCADE,
        source_event_id TEXT NOT NULL,
        source_node_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(memory_id, source_event_id, source_node_id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_terms (
        companion_id TEXT NOT NULL,
        term TEXT NOT NULL,
        memory_id TEXT NOT NULL REFERENCES memory_records(memory_id) ON DELETE CASCADE,
        PRIMARY KEY(companion_id, term, memory_id)
    )""",
    """CREATE INDEX IF NOT EXISTS memory_terms_record ON memory_terms(memory_id)""",
    """CREATE TABLE IF NOT EXISTS memory_commands (
        command_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS memory_source_tombstones (
        companion_id TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        PRIMARY KEY(companion_id, source_event_id)
    )""",
    """CREATE TABLE IF NOT EXISTS auto_memory_owned (
        memory_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS auto_memory_evidence (
        memory_id TEXT NOT NULL, source_event_id TEXT NOT NULL, evidence TEXT NOT NULL,
        source_memory_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
        PRIMARY KEY(memory_id, source_event_id, evidence)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_token_tasks (
        task_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL,
        purpose TEXT NOT NULL,
        day TEXT NOT NULL,
        reserved_input_tokens INTEGER NOT NULL,
        reserved_output_tokens INTEGER NOT NULL,
        actual_input_tokens INTEGER,
        actual_output_tokens INTEGER,
        status TEXT NOT NULL CHECK(status IN ('reserved','settled','released')),
        fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        released_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS memory_token_day
        ON memory_token_tasks(day, companion_id, status)""",
)


class MemoryService:
    def __init__(
        self,
        database_path: str | Path,
        policy: MemoryPolicy | dict | None = None,
        clock: Callable[[], datetime | float] | None = None,
        *,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        if str(database_path) == ":memory:" or not str(database_path).strip():
            raise ValueError("MemoryService requires a durable database file")
        self.database_path = str(Path(database_path).expanduser())
        Path(self.database_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.policy = (
            policy
            if isinstance(policy, MemoryPolicy)
            else MemoryPolicy.model_validate(policy or {})
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.token_estimator = token_estimator or ApproximateTokenEstimator()
        self.automatic_memory = None
        self.embedding_memory = None
        connection = self._open()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()
        with self._connection(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_schema_versions(version INTEGER PRIMARY KEY)"
            )
            if not connection.execute(
                "SELECT 1 FROM memory_schema_versions WHERE version = 1"
            ).fetchone():
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(memory_records)")
                }
                for name, definition in (
                    ("revision", "INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)"),
                    ("occurred_at", "TEXT"),
                    ("episode_key", "TEXT"),
                    ("valid_from", "TEXT"),
                    ("valid_to", "TEXT"),
                    ("supersedes_id", "TEXT"),
                    ("episode_identity", "TEXT"),
                ):
                    if name not in columns:
                        connection.execute(
                            f"ALTER TABLE memory_records ADD COLUMN {name} {definition}"
                        )
                connection.execute("DROP INDEX IF EXISTS memory_active_content")
                connection.execute(
                    "CREATE UNIQUE INDEX memory_active_content ON memory_records(companion_id, kind, normalized_content) WHERE active = 1 AND kind != 'episode'"
                )
                connection.execute(
                    "UPDATE memory_records SET episode_identity = (SELECT 'event:' || MIN(source_event_id) FROM memory_provenance p WHERE p.memory_id = memory_records.memory_id) WHERE kind = 'episode'"
                )
                # Old versions permitted several episodes from one event. Preserve
                # these ambiguous historical records without inventing identities.
                connection.execute(
                    "UPDATE memory_records SET episode_identity = NULL WHERE kind = 'episode' AND active = 1 AND memory_id NOT IN (SELECT MIN(memory_id) FROM memory_records WHERE kind = 'episode' AND active = 1 GROUP BY companion_id, episode_identity)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX memory_episode_identity ON memory_records(companion_id, episode_identity) WHERE kind = 'episode' AND active = 1 AND episode_identity IS NOT NULL"
                )
                connection.execute("INSERT INTO memory_schema_versions VALUES (1)")
            if not connection.execute(
                "SELECT 1 FROM memory_schema_versions WHERE version = 2"
            ).fetchone():
                connection.execute(
                    "ALTER TABLE memory_records ADD COLUMN retention_policy TEXT NOT NULL DEFAULT 'auto'"
                )
                connection.execute("INSERT INTO memory_schema_versions VALUES (2)")
            connection.execute("""CREATE TABLE IF NOT EXISTS memory_access_state (
                memory_id TEXT PRIMARY KEY REFERENCES memory_records(memory_id) ON DELETE CASCADE,
                exposure_count INTEGER NOT NULL DEFAULT 0,
                last_exposed_at TEXT,
                last_reinforced_at TEXT
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS memory_usage_receipts (
                command_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                reinforced INTEGER NOT NULL,
                PRIMARY KEY(command_id, memory_id)
            )""")

        self.logs = MemoryLogService(self.database_path, self._now)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function(
            "memory_normalize", 1, lambda value: _normalize(value or ""), deterministic=True
        )
        return connection

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _now(self) -> datetime:
        value = self.clock()
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return datetime.fromtimestamp(value, UTC)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _validate(model: type[_Model], payload: object) -> _Model:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            error = exc.errors(include_input=False)[0]
            location = ".".join(str(part) for part in error["loc"]) or "payload"
            raise MemoryError(422, "validation_error", f"{location}: {error['msg']}") from exc

    @staticmethod
    def _identifier(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise MemoryError(
                422,
                "validation_error",
                f"{label} must be a nonempty string of at most 128 characters",
            )
        return value.strip()

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _estimate(self, text: str) -> int:
        result = self.token_estimator.estimate(text)
        if type(result) is not int or result < 0:
            raise MemoryError(
                500, "invalid_token_estimator", "Token estimator must return a nonnegative integer"
            )
        return result

    def _estimator_snapshot(self) -> dict:
        return {"name": self.token_estimator.name, "approximate": True}

    @staticmethod
    def _record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        sources = connection.execute(
            "SELECT DISTINCT source_event_id FROM memory_provenance WHERE memory_id = ? ORDER BY source_event_id",
            (row["memory_id"],),
        ).fetchall()
        return {
            "layer": "episodic" if row["kind"] == "episode" else "semantic",
            "revision": row["revision"],
            "occurred_at": row["occurred_at"],
            "episode_key": row["episode_key"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "supersedes_id": row["supersedes_id"],
            "memory_id": row["memory_id"],
            "companion_id": row["companion_id"],
            "kind": row["kind"],
            "content": row["content"],
            "source_event_ids": [source["source_event_id"] for source in sources],
            "semantic_key": row["semantic_key"],
            "tags": json.loads(row["tags_json"]),
            "retention_policy": row["retention_policy"],
            "effective_retention_policy": _effective_retention_policy(row),
            "importance": row["importance"],
            "confidence": row["confidence"],
            "confirmed": bool(row["confirmed"]),
            "pinned": bool(row["pinned"]),
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _index_record(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        connection.execute("DELETE FROM memory_terms WHERE memory_id = ?", (row["memory_id"],))
        text = " ".join([row["content"], *json.loads(row["tags_json"]), row["semantic_key"] or ""])
        connection.executemany(
            "INSERT INTO memory_terms(companion_id, term, memory_id) VALUES (?, ?, ?)",
            [(row["companion_id"], term, row["memory_id"]) for term in sorted(_terms(text))],
        )

    def write(self, companion_id: str, payload: dict) -> dict:
        companion_id = self._identifier(companion_id, "companion_id")
        request = self._validate(MemoryWrite, payload)
        fingerprint = self._fingerprint(
            {
                "companion_id": companion_id,
                **{
                    key: value
                    for key, value in request.model_dump(mode="json").items()
                    if (
                        key not in ("occurred_at", "episode_key", "valid_from", "valid_to")
                        or value is not None
                    )
                    and (key != "retention_policy" or value != "auto")
                },
            }
        )
        now = self._timestamp(self._now())
        with self._connection(write=True) as connection:
            command = connection.execute(
                "SELECT * FROM memory_commands WHERE command_id = ?", (request.command_id,)
            ).fetchone()
            if command is not None:
                if command["fingerprint"] != fingerprint:
                    raise MemoryError(
                        409,
                        "idempotency_conflict",
                        "command_id was already used with another payload",
                    )
                return json.loads(command["result_json"])

            self._validate_source(
                connection, companion_id, request.source_event_id, request.source_node_id
            )
            duplicate = connection.execute(
                """SELECT * FROM memory_records
                   WHERE companion_id = ? AND kind = ? AND normalized_content = ? AND active = 1""",
                (companion_id, request.kind, _normalize(request.content)),
            ).fetchone()
            if request.kind == "episode":
                duplicate = connection.execute(
                    "SELECT * FROM memory_records WHERE companion_id = ? AND kind = 'episode' AND active = 1 AND episode_identity = ?",
                    (companion_id, self._episode_identity(request)),
                ).fetchone()
                if duplicate is not None and duplicate["normalized_content"] != _normalize(
                    request.content
                ):
                    raise MemoryError(
                        409,
                        "episode_identity_conflict",
                        "Episode identity already has different content",
                    )
            if duplicate is not None:
                if duplicate["revision"] >= 2**63 - 1:
                    raise MemoryError(409, "memory_revision_conflict", "Memory revision exhausted")
                for field in ("occurred_at", "valid_from", "valid_to"):
                    if (
                        field in request.model_fields_set
                        and getattr(request, field) != duplicate[field]
                    ):
                        if (
                            request.kind == "episode"
                            and field == "occurred_at"
                            and duplicate[field] is None
                        ):
                            connection.execute(
                                "UPDATE memory_records SET occurred_at = ? WHERE memory_id = ?",
                                (request.occurred_at, duplicate["memory_id"]),
                            )
                        else:
                            raise MemoryError(
                                409,
                                "episode_identity_conflict"
                                if request.kind == "episode"
                                else "memory_correction_required",
                                "Existing memory has different time metadata",
                            )
            if (
                duplicate is not None
                and duplicate["semantic_key"] is not None
                and request.semantic_key is not None
                and duplicate["semantic_key"] != request.semantic_key
            ):
                raise MemoryError(
                    409,
                    "semantic_key_conflict",
                    "Identical active content already belongs to another semantic_key",
                )
            memory_id = (
                duplicate["memory_id"] if duplicate is not None else f"mem_{uuid.uuid4().hex}"
            )
            semantic_key = request.semantic_key or (
                duplicate["semantic_key"] if duplicate is not None else None
            )
            if semantic_key is not None:
                conflict = connection.execute(
                    "SELECT memory_id FROM memory_records WHERE companion_id = ? AND semantic_key = ? AND active = 1 AND memory_id != ?",
                    (companion_id, semantic_key, memory_id),
                ).fetchone()
                if conflict:
                    raise MemoryError(
                        409,
                        "memory_correction_required",
                        "Use explicit correction for a changed stable memory",
                    )
            if duplicate is None:
                connection.execute(
                    """INSERT INTO memory_records(
                           memory_id, companion_id, kind, content, normalized_content,
                           semantic_key, tags_json, importance, confidence, confirmed,
                           pinned, active, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        memory_id,
                        companion_id,
                        request.kind,
                        request.content,
                        _normalize(request.content),
                        semantic_key,
                        json.dumps(request.tags, ensure_ascii=False),
                        request.importance,
                        request.confidence,
                        int(request.confirmed),
                        int(request.pinned),
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE memory_records SET semantic_key = ?, tags_json = ?, importance = ?,
                           confidence = ?, confirmed = ?, pinned = ?, updated_at = ?
                       WHERE memory_id = ?""",
                    (
                        semantic_key,
                        json.dumps(
                            _merge_tags(json.loads(duplicate["tags_json"]), request.tags),
                            ensure_ascii=False,
                        ),
                        max(duplicate["importance"], request.importance),
                        max(duplicate["confidence"], request.confidence),
                        int(bool(duplicate["confirmed"]) or request.confirmed),
                        int(bool(duplicate["pinned"]) or request.pinned),
                        now,
                        memory_id,
                    ),
                )
            retention = request.retention_policy
            if duplicate is not None and retention == "auto":
                retention = duplicate["retention_policy"]
            connection.execute(
                "UPDATE memory_records SET retention_policy = ? WHERE memory_id = ?",
                (retention, memory_id),
            )
            if duplicate is None:
                connection.execute(
                    "UPDATE memory_records SET occurred_at = ?, episode_key = ?, valid_from = ?, valid_to = ?, episode_identity = ? WHERE memory_id = ?",
                    (
                        request.occurred_at,
                        request.episode_key,
                        request.valid_from,
                        request.valid_to,
                        self._episode_identity(request) if request.kind == "episode" else None,
                        memory_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE memory_records SET revision = revision + 1 WHERE memory_id = ?",
                    (memory_id,),
                )
            connection.execute(
                """INSERT OR IGNORE INTO memory_provenance(memory_id, source_event_id, source_node_id)
                   VALUES (?, ?, ?)""",
                (memory_id, request.source_event_id, request.source_node_id or ""),
            )
            row = connection.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            self._index_record(connection, row)
            result = self._record(connection, row)
            connection.execute(
                "INSERT INTO memory_commands(command_id, companion_id, fingerprint, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    request.command_id,
                    companion_id,
                    fingerprint,
                    json.dumps(result, ensure_ascii=False),
                    now,
                ),
            )
            self.logs.record(
                companion_id,
                "write",
                command_id=request.command_id,
                details={
                    key: result.get(key)
                    for key in (
                        "memory_id",
                        "revision",
                        "source_event_ids",
                        "retention_policy",
                        "importance",
                    )
                },
                connection=connection,
            )
            return result

    @staticmethod
    def _episode_identity(request):
        return (
            f"key:{request.episode_key}"
            if request.episode_key
            else f"event:{request.source_event_id}"
        )

    @staticmethod
    def _validate_source(connection, companion_id, event_id, node_id):
        try:
            event = connection.execute(
                "SELECT companion_id, node_id FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            valid = event is not None and event["companion_id"] == companion_id
            if node_id is not None:
                valid = (
                    valid
                    and event["node_id"] == node_id
                    and connection.execute(
                        "SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)
                    ).fetchone()
                    is not None
                )
        except sqlite3.OperationalError:
            valid = False
        if not valid:
            raise MemoryError(
                422,
                "invalid_memory_source",
                "Source event must belong to this companion and match the supplied node",
            )

    @staticmethod
    def _time_valid(row, now):
        if row["kind"] == "episode":
            return True
        timestamp = MemoryService._timestamp(now)
        return (row["valid_from"] is None or row["valid_from"] <= timestamp) and (
            row["valid_to"] is None or timestamp < row["valid_to"]
        )

    def validate_references(self, connection, companion_id, references, now=None):
        now = now or self._now()
        for reference in references:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE companion_id = ? AND memory_id = ?",
                (companion_id, reference["memory_id"]),
            ).fetchone()
            if (
                row is None
                or not row["active"]
                or row["revision"] != reference["revision"]
                or not self._time_valid(row, now)
                or not self._source_supported(connection, companion_id, row["memory_id"])
            ):
                raise MemoryError(
                    409,
                    "memory_context_changed",
                    "A referenced memory changed or is no longer valid",
                )

    @staticmethod
    def _supported_clause(alias: str = "m") -> str:
        # Automatic semantic records remain derived from explicit episode versions.
        # Independent manual records and raw episodes need no automatic evidence.
        return f"""({alias}.kind = 'episode' OR NOT EXISTS (
            SELECT 1 FROM auto_memory_owned owned WHERE owned.memory_id = {alias}.memory_id
        ) OR EXISTS (
            SELECT 1 FROM auto_memory_evidence evidence
            JOIN memory_records source ON source.memory_id = evidence.source_memory_id
            JOIN events original ON original.event_id = evidence.source_event_id
            WHERE evidence.memory_id = {alias}.memory_id
              AND source.companion_id = {alias}.companion_id AND source.kind = 'episode'
              AND source.active = 1 AND source.revision = evidence.source_revision
              AND original.companion_id = {alias}.companion_id
        ))"""

    def _source_supported(self, connection, companion_id: str, memory_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM memory_records m WHERE m.companion_id = ? AND m.memory_id = ? AND "
                + self._supported_clause(),
                (companion_id, memory_id),
            ).fetchone()
            is not None
        )

    def _edit(self, companion_id, memory_id, payload, *, correction):
        companion_id = self._identifier(companion_id, "companion_id")
        memory_id = self._identifier(memory_id, "memory_id")
        request = self._validate(MemoryCorrection if correction else MemoryMetadataUpdate, payload)
        fingerprint = self._fingerprint(
            {
                "operation": "correct" if correction else "metadata",
                "companion_id": companion_id,
                "memory_id": memory_id,
                **request.model_dump(mode="json", exclude_unset=True),
            }
        )
        now = self._timestamp(self._now())
        with self._connection(write=True) as connection:
            receipt = connection.execute(
                "SELECT * FROM memory_commands WHERE command_id = ?", (request.command_id,)
            ).fetchone()
            if receipt is not None:
                if receipt["fingerprint"] != fingerprint:
                    raise MemoryError(409, "idempotency_conflict", "command_id already used")
                return json.loads(receipt["result_json"])
            old = connection.execute(
                "SELECT * FROM memory_records WHERE companion_id = ? AND memory_id = ?",
                (companion_id, memory_id),
            ).fetchone()
            if old is None:
                raise MemoryError(
                    404, "memory_not_found", "Memory does not exist for this companion"
                )
            if not old["active"] or old["revision"] != request.expected_revision:
                raise MemoryError(409, "memory_revision_conflict", "Memory revision changed")
            if old["revision"] >= 2**63 - 1:
                raise MemoryError(409, "memory_revision_conflict", "Memory revision exhausted")
            metadata = {
                key: getattr(request, key)
                if getattr(request, key) is not None
                else (json.loads(old["tags_json"]) if key == "tags" else old[key])
                for key in (
                    "tags",
                    "importance",
                    "confidence",
                    "confirmed",
                    "pinned",
                    "retention_policy",
                )
            }
            metadata["tags"] = _merge_tags([], metadata["tags"])
            target_id = memory_id
            if correction:
                if old["kind"] == "episode":
                    raise MemoryError(
                        409,
                        "memory_correction_not_supported",
                        "Correction requires a stable memory",
                    )
                self._validate_source(
                    connection, companion_id, request.source_event_id, request.source_node_id
                )
                valid_from = request.valid_from if "valid_from" in request.model_fields_set else now
                valid_to = (
                    request.valid_to if "valid_to" in request.model_fields_set else old["valid_to"]
                )
                if valid_from > now:
                    raise MemoryError(
                        422, "validation_error", "Future corrections are not supported"
                    )
                if (
                    old["valid_from"] and (valid_from is None or valid_from < old["valid_from"])
                ) or (valid_from and valid_to and valid_to <= valid_from):
                    raise MemoryError(
                        422, "validation_error", "Correction validity interval is inconsistent"
                    )
                conflict = connection.execute(
                    "SELECT memory_id FROM memory_records WHERE companion_id = ? AND kind = ? AND normalized_content = ? AND active = 1 AND memory_id != ?",
                    (companion_id, old["kind"], _normalize(request.content), memory_id),
                ).fetchone()
                if conflict:
                    raise MemoryError(
                        409,
                        "semantic_key_conflict",
                        "Corrected content already belongs to another memory",
                    )
                connection.execute(
                    "UPDATE memory_records SET active = 0, revision = revision + 1, valid_to = ?, updated_at = ? WHERE memory_id = ?",
                    (valid_from, now, memory_id),
                )
                target_id = f"mem_{uuid.uuid4().hex}"
                connection.execute(
                    """INSERT INTO memory_records(memory_id, companion_id, kind, content, normalized_content, semantic_key, tags_json, importance, confidence, confirmed, pinned, active, created_at, updated_at, revision, valid_from, valid_to, supersedes_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?, ?, ?)""",
                    (
                        target_id,
                        companion_id,
                        old["kind"],
                        request.content,
                        _normalize(request.content),
                        old["semantic_key"],
                        json.dumps(metadata["tags"], ensure_ascii=False),
                        metadata["importance"],
                        metadata["confidence"],
                        metadata["confirmed"],
                        metadata["pinned"],
                        now,
                        now,
                        valid_from,
                        valid_to,
                        memory_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO memory_provenance VALUES (?, ?, ?)",
                    (target_id, request.source_event_id, request.source_node_id or ""),
                )
            else:
                connection.execute(
                    "UPDATE memory_records SET tags_json = ?, importance = ?, confidence = ?, confirmed = ?, pinned = ?, revision = revision + 1, updated_at = ? WHERE memory_id = ?",
                    (
                        json.dumps(_merge_tags([], metadata["tags"]), ensure_ascii=False),
                        metadata["importance"],
                        metadata["confidence"],
                        metadata["confirmed"],
                        metadata["pinned"],
                        now,
                        memory_id,
                    ),
                )
            connection.execute(
                "UPDATE memory_records SET retention_policy = ? WHERE memory_id = ?",
                (metadata["retention_policy"], target_id),
            )
            row = connection.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?", (target_id,)
            ).fetchone()
            self._index_record(connection, row)
            result = self._record(connection, row)
            connection.execute(
                "INSERT INTO memory_commands VALUES (?, ?, ?, ?, ?)",
                (
                    request.command_id,
                    companion_id,
                    fingerprint,
                    json.dumps(result, ensure_ascii=False),
                    now,
                ),
            )
            self.logs.record(
                companion_id,
                "correct" if correction else "update_metadata",
                command_id=request.command_id,
                details={
                    key: result.get(key)
                    for key in (
                        "memory_id",
                        "revision",
                        "source_event_ids",
                        "retention_policy",
                        "importance",
                    )
                },
                connection=connection,
            )
            return result

    def correct(self, companion_id: str, memory_id: str, payload: dict) -> dict:
        return self._edit(companion_id, memory_id, payload, correction=True)

    def update_metadata(self, companion_id: str, memory_id: str, payload: dict) -> dict:
        return self._edit(companion_id, memory_id, payload, correction=False)

    def list(self, companion_id: str, limit: int = 50, offset: int = 0) -> dict:
        companion_id = self._identifier(companion_id, "companion_id")
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise MemoryError(
                422, "validation_error", "limit must be 1..200 and offset must be nonnegative"
            )
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE companion_id = ? ORDER BY updated_at DESC, memory_id LIMIT ? OFFSET ?",
                (companion_id, limit, offset),
            ).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) FROM memory_records WHERE companion_id = ?", (companion_id,)
            ).fetchone()[0]
            return {
                "items": [self._record(connection, row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @staticmethod
    def _format_context(items: list[dict], *, header: str = _RECALL_HEADER) -> str:
        if not items:
            return ""
        blocks = [
            f"[{item['kind']} | {item['memory_id']} | revision={item['revision']} | occurred_at={item['occurred_at']} | valid_from={item['valid_from']} | valid_to={item['valid_to']}]\n{item['content']}"
            for item in items
        ]
        return header + "\n\n".join(blocks)

    @staticmethod
    def _derived_context(summaries: list[dict], relations: list[dict]) -> str:
        sections = []
        for label, values, fields in (
            (
                "Derived summaries (check source evidence)",
                summaries,
                ("summary_id", "content", "memory_versions"),
            ),
            (
                "Supported knowledge graph relations",
                relations,
                ("relation_id", "subject", "predicate", "object", "confidence", "memory_versions"),
            ),
        ):
            if values:
                sections.append(
                    label
                    + ":\n"
                    + json.dumps(
                        [{key: item[key] for key in fields} for item in values],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
        return "\n\n".join(sections)

    def _fused_context(
        self, items: list[dict], summaries: list[dict], relations: list[dict]
    ) -> str:
        return "\n\n".join(
            part
            for part in (self._format_context(items), self._derived_context(summaries, relations))
            if part
        )

    def _candidates(
        self,
        connection: sqlite3.Connection,
        companion_id: str,
        query: str,
        layer: str | None = None,
        now: datetime | None = None,
    ) -> list[sqlite3.Row]:
        candidates: dict[str, sqlite3.Row] = {}
        layer_filter = (
            ""
            if layer is None
            else (" AND m.kind = 'episode'" if layer == "episodic" else " AND m.kind != 'episode'")
        )
        timestamp = self._timestamp(now or self._now())
        layer_filter += " AND (m.kind = 'episode' OR ((m.valid_from IS NULL OR m.valid_from <= ?) AND (m.valid_to IS NULL OR m.valid_to > ?)))"
        layer_filter += " AND " + self._supported_clause()
        terms = sorted(_terms(query), key=lambda term: (-_term_weight(term), term))[:256]
        if terms:
            placeholders = ",".join("?" for _ in terms)
            rows = connection.execute(
                f"""SELECT m.*, COUNT(t.term) AS lexical_hits
                    FROM memory_terms t JOIN memory_records m ON m.memory_id = t.memory_id
                    WHERE t.companion_id = ? AND m.companion_id = ? AND m.active = 1
                          {layer_filter} AND t.term IN ({placeholders})
                    GROUP BY m.memory_id
                    ORDER BY lexical_hits DESC, m.pinned DESC, m.importance DESC, m.memory_id
                    LIMIT ?""",
                (
                    companion_id,
                    companion_id,
                    timestamp,
                    timestamp,
                    *terms,
                    self.policy.lexical_candidate_limit,
                ),
            ).fetchall()
            candidates.update((row["memory_id"], row) for row in rows)
        if self.policy.decay.enabled:
            match_order = "0.0"
            parameters = [companion_id, timestamp, timestamp]
            if terms:
                placeholders = ",".join("?" for _ in terms)
                match_order = (
                    "(SELECT COUNT(*) FROM memory_terms pt WHERE "
                    "pt.memory_id = m.memory_id AND pt.term IN (" + placeholders + "))"
                )
                parameters.extend(terms)
            parameters.append(self.policy.decay.permanent_candidate_limit)
            rows = connection.execute(
                f"SELECT m.* FROM memory_records m WHERE companion_id = ? AND active = 1 {layer_filter} AND {_permanent_clause()} ORDER BY {match_order} DESC, importance DESC, memory_id LIMIT ?",
                parameters,
            ).fetchall()
            candidates.update((row["memory_id"], row) for row in rows)
        # Importance is independent of age, so a stable preference retains its
        # floor even after a long absence. A separate recency pool adds episodes.
        for order in (
            "pinned DESC, importance DESC, confirmed DESC, updated_at DESC",
            "updated_at DESC",
        ):
            rows = connection.execute(
                f"SELECT m.* FROM memory_records m WHERE companion_id = ? AND active = 1 {layer_filter} ORDER BY {order}, memory_id LIMIT ?",
                (companion_id, timestamp, timestamp, self.policy.background_candidate_limit),
            ).fetchall()
            candidates.update((row["memory_id"], row) for row in rows)
        return list(candidates.values())

    def _decay_factor(self, row, now, last_reinforced_at=None):
        policy = self.policy.decay
        retention = _effective_retention_policy(row)
        if not policy.enabled or retention == "permanent":
            return 1.0
        anchor = last_reinforced_at or row["created_at"]
        created = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
        age = max((now - created).total_seconds() / 86_400, 0)
        prefix = {
            "routine_episode": "routine",
            "significant_episode": "significant",
            "stable": "stable",
        }[retention]
        floor = getattr(policy, prefix + "_floor")
        half_life = getattr(policy, prefix + "_half_life_days")
        return floor + (1 - floor) * 2 ** (-age / half_life)

    @staticmethod
    def _query_relevance(row, query):
        searchable = " ".join(
            [
                row["content"],
                *(json.loads(row["tags_json"]) if "tags_json" in row.keys() else row["tags"]),
                row["semantic_key"] or "",
            ]
        )
        terms = _terms(query)
        denominator = sum(_term_weight(term) for term in terms)
        coverage = (
            sum(_term_weight(term) for term in terms & _terms(searchable)) / denominator
            if denominator
            else 0
        )
        phrase = bool(query) and _normalize(query) in _normalize(searchable)
        return coverage, phrase

    def _priority_permanent(self, row, query) -> bool:
        if not self.policy.decay.enabled or not _normalize(query):
            return False
        coverage, phrase = self._query_relevance(row, query)
        return _effective_retention_policy(row) == "permanent" and (
            phrase or coverage >= max(self.policy.decay.reinforcement_min_coverage, 0.01)
        )

    def _exact_slot_candidates(self, connection, companion_id, request, now):
        if request.layer == "episodic":
            return [], 0
        timestamp = self._timestamp(now)
        clause = (
            "m.companion_id = ? AND m.active = 1 AND m.kind != 'episode' "
            "AND memory_normalize(m.semantic_key) = ? "
            "AND (m.valid_from IS NULL OR m.valid_from <= ?) "
            "AND (m.valid_to IS NULL OR m.valid_to > ?) AND " + self._supported_clause()
        )
        parameters = (companion_id, _normalize(request.semantic_key), timestamp, timestamp)
        total = connection.execute(
            f"SELECT COUNT(*) FROM memory_records m WHERE {clause}",
            parameters,
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT m.* FROM memory_records m WHERE {clause} ORDER BY m.pinned DESC, m.importance DESC, m.memory_id LIMIT ?",
            (*parameters, self.policy.max_recall_items),
        ).fetchall()
        return rows, total

    def record_dialogue_usage(
        self, connection, companion_id, command_id, memory_versions, query, now
    ):
        """Record final validated context in the successful dialogue transaction."""
        self.validate_references(connection, companion_id, memory_versions, now)
        timestamp = self._timestamp(now)
        for reference in memory_versions:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE companion_id = ? AND memory_id = ?",
                (companion_id, reference["memory_id"]),
            ).fetchone()
            coverage, phrase = self._query_relevance(row, query)
            relevant = bool(_normalize(query)) and (
                coverage >= self.policy.decay.reinforcement_min_coverage or phrase
            )
            state = connection.execute(
                "SELECT * FROM memory_access_state WHERE memory_id = ?", (row["memory_id"],)
            ).fetchone()
            last = state["last_reinforced_at"] if state else None
            reinforce = (
                self.policy.decay.enabled
                and relevant
                and (last is None or last[:10] != timestamp[:10])
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO memory_usage_receipts VALUES (?, ?, ?, ?, ?)",
                (command_id, row["memory_id"], reference["revision"], timestamp, int(reinforce)),
            )
            if not cursor.rowcount:
                continue
            connection.execute(
                """INSERT INTO memory_access_state VALUES (?, 1, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                   exposure_count = exposure_count + 1, last_exposed_at = excluded.last_exposed_at,
                   last_reinforced_at = COALESCE(excluded.last_reinforced_at, last_reinforced_at)""",
                (row["memory_id"], timestamp, timestamp if reinforce else None),
            )

            self.logs.record(
                companion_id,
                "reinforcement",
                command_id=command_id,
                details={
                    "memory_id": row["memory_id"],
                    "revision": reference["revision"],
                    "reinforced": reinforce,
                    "exposed": True,
                },
                connection=connection,
            )

    def _rank(self, row, query, query_terms, now, last_reinforced_at=None) -> float:
        coverage, phrase_match = self._query_relevance(row, query)
        updated = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
        age_days = max((now - updated).total_seconds() / 86_400, 0)
        recency = 2 ** (-age_days / self.policy.recency_half_life_days)
        stable = 0.35 if row["kind"] in ("fact", "preference", "relationship") else 0
        decay = self._decay_factor(row, now, last_reinforced_at)
        return (
            8.0 * coverage
            + 2.0 * phrase_match
            + 2.0 * row["pinned"]
            + 1.5 * row["importance"] * decay
            + 0.25 * row["confidence"]
            + 0.25 * row["confirmed"]
            + stable
            + (0 if self.policy.decay.enabled else 0.4 * recency)
        )

    def recall(self, companion_id: str, payload: dict, *, allow_rerank: bool = False) -> dict:
        started = perf_counter()
        companion_id = self._identifier(companion_id, "companion_id")
        request = self._validate(MemoryRecall, payload)
        budget = (
            request.token_budget if "token_budget" in payload else self.policy.default_recall_tokens
        )
        max_items = request.max_items if "max_items" in payload else self.policy.default_max_items
        if budget > self.policy.max_recall_tokens or max_items > self.policy.max_recall_items:
            raise MemoryError(
                422, "recall_policy_limit", "Requested recall exceeds the runtime policy limits"
            )
        now = self._now()
        query = _normalize(request.query)
        query_terms = _terms(query)
        items: list[dict] = []
        summaries: list[dict] = []
        relations: list[dict] = []
        rerank_info = None
        with self._connection() as connection:
            exact_total = None
            if request.semantic_key is not None:
                rows, exact_total = self._exact_slot_candidates(
                    connection, companion_id, request, now
                )
                candidates = {row["memory_id"]: row for row in rows}
                local_ids = set(candidates)
                semantic_rows, semantic_hit = [], False
                derivatives = {"summaries": [], "relations": []}
                graph_boost = {}
            else:
                rows = self._candidates(connection, companion_id, query, request.layer, now)
                semantic_rows, semantic_hit = (
                    self.embedding_memory.retrieve_cached(
                        connection, companion_id, query, request.layer
                    )
                    if self.embedding_memory is not None
                    else ([], False)
                )
                derivatives = {"summaries": [], "relations": []}
                if self.automatic_memory is not None:
                    derivatives = self.automatic_memory.recall_derivatives(
                        connection,
                        companion_id,
                        query,
                        limit=min(max_items, 8),
                        support_memory_ids=[row["memory_id"] for row in semantic_rows[:8]],
                    )
                    if request.layer == "episodic":
                        derivatives["relations"] = []
                    elif request.layer == "semantic":
                        derivatives["summaries"] = []
                candidates = {row["memory_id"]: row for row in rows}
                local_ids = set(candidates)
                candidates.update((row["memory_id"], row) for row in semantic_rows)
                graph_boost: dict[str, float] = {}
                for relation in derivatives["relations"]:
                    for reference in relation["memory_versions"]:
                        row = connection.execute(
                            "SELECT * FROM memory_records WHERE companion_id = ? AND memory_id = ?",
                            (companion_id, reference["memory_id"]),
                        ).fetchone()
                        if row is not None and (
                            request.layer is None
                            or (row["kind"] == "episode") == (request.layer == "episodic")
                        ):
                            candidates[row["memory_id"]] = row
                            graph_boost[row["memory_id"]] = 1.5
                            local_ids.add(row["memory_id"])
            rows = list(candidates.values())
            rows = [
                row
                for row in rows
                if row["active"]
                and self._time_valid(row, now)
                and self._source_supported(connection, companion_id, row["memory_id"])
            ]
            access = {}
            for start in range(0, len(rows), 512):
                batch = rows[start : start + 512]
                placeholders = ",".join("?" for _ in batch)
                access.update(
                    {
                        value["memory_id"]: value["last_reinforced_at"]
                        for value in connection.execute(
                            f"SELECT memory_id, last_reinforced_at FROM memory_access_state WHERE memory_id IN ({placeholders})",
                            [row["memory_id"] for row in batch],
                        )
                    }
                )
            rows.sort(
                key=lambda row: (
                    -self._rank(row, query, query_terms, now, access.get(row["memory_id"]))
                    - graph_boost.get(row["memory_id"], 0),
                    row["memory_id"],
                )
            )
            if semantic_rows:
                base_ranks = {
                    row["memory_id"]: rank
                    for rank, row in enumerate(
                        (row for row in rows if row["memory_id"] in local_ids), 1
                    )
                }
                semantic_ranks = {
                    row["memory_id"]: rank for rank, row in enumerate(semantic_rows, 1)
                }
                rows.sort(
                    key=lambda row: (
                        -(
                            (
                                1 / (60 + base_ranks[row["memory_id"]])
                                if row["memory_id"] in base_ranks
                                else 0
                            )
                            + (
                                1 / (60 + semantic_ranks[row["memory_id"]])
                                if row["memory_id"] in semantic_ranks
                                else 0
                            )
                        ),
                        row["memory_id"],
                    )
                )
            if allow_rerank and request.semantic_key is None and self.embedding_memory is not None:
                rows, rerank_info = self.embedding_memory.rerank_rows(companion_id, query, rows)
            rows.sort(key=lambda row: not self._priority_permanent(row, query))
            layer_filter = (
                ""
                if request.layer is None
                else (
                    " AND kind = 'episode'"
                    if request.layer == "episodic"
                    else " AND kind != 'episode'"
                )
            )
            timestamp = self._timestamp(now)
            total = (
                exact_total
                if exact_total is not None
                else connection.execute(
                    f"SELECT COUNT(*) FROM memory_records m WHERE companion_id = ? AND active = 1 {layer_filter} AND (kind = 'episode' OR ((valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to > ?))) AND {self._supported_clause()}",
                    (companion_id, timestamp, timestamp),
                ).fetchone()[0]
            )
            priority_ids = set()
            for row in rows:
                if not self._priority_permanent(row, query) or len(items) >= max_items:
                    continue
                item = self._record(connection, row)
                item["decay_factor"] = 1.0
                item["effective_importance"] = row["importance"]
                if self._estimate(self._fused_context(items + [item], [], [])) <= budget:
                    items.append(item)
                    priority_ids.add(row["memory_id"])
            # Give one summary and one relation a chance before filling the
            # remaining budget with precise source records. Sources stay in storage.
            derived_limit = max(1, max_items // 2)
            for values, target in (
                (derivatives["summaries"], summaries),
                (derivatives["relations"], relations),
            ):
                for value in values:
                    if (
                        len(summaries) + len(relations) >= derived_limit
                        or len(items) + len(summaries) + len(relations) >= max_items
                    ):
                        break
                    proposal = target + [value]
                    summary_proposal = proposal if target is summaries else summaries
                    relation_proposal = proposal if target is relations else relations
                    if (
                        self._estimate(self._derived_context(summary_proposal, relation_proposal))
                        <= int(budget * 0.6)
                        and self._estimate(
                            self._fused_context(items, summary_proposal, relation_proposal)
                        )
                        <= budget
                    ):
                        target.append(value)
                        break
            for row in rows:
                if row["memory_id"] in priority_ids:
                    continue
                if len(items) + len(summaries) + len(relations) >= max_items:
                    break
                item = self._record(connection, row)
                factor = self._decay_factor(row, now, access.get(row["memory_id"]))
                item["decay_factor"] = factor
                item["effective_importance"] = row["importance"] * factor
                if (
                    self._estimate(self._fused_context(items + [item], summaries, relations))
                    <= budget
                ):
                    items.append(item)
            context = self._fused_context(items, summaries, relations)
            result = {
                "items": items,
                "summaries": summaries,
                "relations": relations,
                "context_text": context,
                "estimated_tokens": self._estimate(context),
                "token_budget": budget,
                "truncated": total > len(items)
                or any(
                    len(derivatives[key]) > len(selected)
                    for key, selected in (("summaries", summaries), ("relations", relations))
                ),
                "retrieval_strategy": (
                    "local_semantic_key_lookup"
                    if request.semantic_key is not None
                    else "local_lexical_semantic_graph_rrf"
                    if semantic_rows
                    else "local_lexical_summary_graph_fusion"
                    if derivatives["summaries"] or derivatives["relations"]
                    else "local_lexical_cjk_ngrams_salience_decay"
                    if self.policy.decay.enabled
                    else "local_lexical_cjk_ngrams_importance_recency"
                )
                + ("_reranked" if rerank_info and rerank_info.get("applied") else ""),
                "retrieval": {
                    "semantic_cache_hit": semantic_hit,
                    "semantic_candidates": len(semantic_rows),
                    "vector_space": self.embedding_memory.space if self.embedding_memory else None,
                },
                "rerank": rerank_info,
                "model_calls": 0,
                "token_estimator": self._estimator_snapshot(),
                "candidate_count": len(rows),
            }

        self.logs.best_effort(
            companion_id,
            "recall",
            duration_ms=(perf_counter() - started) * 1000,
            details={
                "query": request.query,
                "strategy": result["retrieval_strategy"],
                "candidate_count": result["candidate_count"],
                "selected": [
                    {
                        key: item.get(key)
                        for key in (
                            "memory_id",
                            "revision",
                            "retention_policy",
                            "effective_retention_policy",
                            "decay_factor",
                            "effective_importance",
                        )
                    }
                    for item in items
                ],
                "omitted_count": max(0, total - len(items)),
                "truncated": result["truncated"],
                "token_budget": budget,
                "estimated_tokens": result["estimated_tokens"],
                "max_items": max_items,
                "rerank": rerank_info,
            },
        )
        return result

    def delete(self, companion_id: str, memory_id: str) -> dict:
        companion_id = self._identifier(companion_id, "companion_id")
        memory_id = self._identifier(memory_id, "memory_id")
        with self._connection(write=True) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO memory_source_tombstones(companion_id, source_event_id)
                   SELECT m.companion_id, p.source_event_id
                   FROM memory_records m JOIN memory_provenance p ON p.memory_id = m.memory_id
                   WHERE m.companion_id = ? AND m.memory_id = ?""",
                (companion_id, memory_id),
            )
            deleted = connection.execute(
                "DELETE FROM memory_records WHERE companion_id = ? AND memory_id = ?",
                (companion_id, memory_id),
            ).rowcount
            if not deleted:
                raise MemoryError(
                    404, "memory_not_found", "Memory does not exist for this companion"
                )
            self.logs.record(
                companion_id,
                "delete",
                details={"memory_id": memory_id, "deleted": True},
                connection=connection,
            )
        return {"memory_id": memory_id, "deleted": True}

    def erase_companion(self, companion_id: str, command_id: str) -> dict:
        """Completely erase every memory-domain artifact for one companion.

        This is the documented "complete forgetting" protocol (distinct from
        ``delete``, which removes one record). It clears the memory records,
        provenance, term index, access state, usage receipts, automatic-memory
        queue/jobs/owned/evidence/derivatives/entities, embeddings, query cache,
        token ledger, and the memory command log. It also records a content-free
        command tombstone so a replayed ``command_id`` cannot resurrect anything,
        and tombstones the underlying source events so the automatic worker will
        not re-extract already-forgotten dialogue.

        The companion row itself, dialogue commands, events, personality,
        emotion, inner life, proactivity and device state are deliberately left
        intact: they belong to other owners and are erased by their own
        protocols. Returns a count summary.
        """
        companion_id = self._identifier(companion_id, "companion_id")
        command_id = self._identifier(command_id, "command_id")
        fingerprint = self._fingerprint(
            {"operation": "erase_companion", "companion_id": companion_id}
        )
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT fingerprint FROM memory_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise MemoryError(
                        409,
                        "idempotency_conflict",
                        "command_id was already used with another payload",
                    )
                return json.loads(
                    connection.execute(
                        "SELECT result_json FROM memory_commands WHERE command_id = ?",
                        (command_id,),
                    ).fetchone()[0]
                )
            counts: dict[str, int] = {}

            def table_exists(name: str) -> bool:
                return (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone()
                    is not None
                )

            def count_memories(table: str, where: str, params: tuple) -> None:
                counts[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", params
                ).fetchone()[0]

            # Source events that fed automatic extraction: remember them as
            # tombstones before deleting, so a replay of an old event cannot
            # re-consolidate already-forgotten dialogue.
            connection.execute(
                """INSERT OR IGNORE INTO memory_source_tombstones(companion_id, source_event_id)
                   SELECT DISTINCT m.companion_id, p.source_event_id
                   FROM memory_provenance p JOIN memory_records m ON m.memory_id = p.memory_id
                   WHERE m.companion_id = ?""",
                (companion_id,),
            )
            # The automatic queue holds payload copies of the source events.
            if table_exists("auto_memory_queue"):
                connection.execute(
                    """INSERT OR IGNORE INTO memory_source_tombstones(companion_id, source_event_id)
                       SELECT companion_id, event_id FROM auto_memory_queue WHERE companion_id = ?""",
                    (companion_id,),
                )
            for table, column in (
                ("memory_terms", "companion_id"),
                ("memory_access_state", None),
                ("memory_usage_receipts", None),
                ("auto_memory_evidence", None),
                ("auto_memory_owned", None),
                ("auto_memory_queue", "companion_id"),
                ("auto_memory_jobs", "companion_id"),
                ("auto_memory_derivatives", "companion_id"),
                ("auto_memory_entities", "companion_id"),
                ("memory_provenance", None),
                ("memory_records", "companion_id"),
                ("memory_commands", "companion_id"),
                ("memory_token_tasks", "companion_id"),
                ("embedding_vectors", "companion_id"),
                ("embedding_attempts", "job_id"),
                ("embedding_jobs", "companion_id"),
                ("embedding_query_cache", "companion_id"),
                ("embedding_deferred", "companion_id"),
            ):
                if not table_exists(table):
                    continue
                if table == "memory_access_state":
                    where = (
                        "memory_id IN (SELECT memory_id FROM memory_records WHERE companion_id = ?)"
                    )
                elif table == "memory_usage_receipts":
                    where = (
                        "memory_id IN (SELECT memory_id FROM memory_records WHERE companion_id = ?)"
                    )
                elif table in ("auto_memory_evidence", "auto_memory_owned"):
                    where = (
                        "memory_id IN (SELECT memory_id FROM memory_records WHERE companion_id = ?)"
                    )
                elif table == "memory_provenance":
                    where = (
                        "memory_id IN (SELECT memory_id FROM memory_records WHERE companion_id = ?)"
                    )
                elif table == "embedding_attempts":
                    where = "job_id IN (SELECT job_id FROM embedding_jobs WHERE companion_id = ?)"
                else:
                    where = f"{column} = ?"
                count_memories(table, where, (companion_id,))
                connection.execute(f"DELETE FROM {table} WHERE {where}", (companion_id,))
            # Graph entities, aliases and alias receipts are companion scoped.
            for table in ("graph_entities", "graph_aliases", "graph_alias_receipts"):
                if not table_exists(table):
                    continue
                count_memories(table, "companion_id = ?", (companion_id,))
                connection.execute(f"DELETE FROM {table} WHERE companion_id = ?", (companion_id,))
            # Content-free command tombstone: the replay is idempotent and cannot
            # resurrect any erased content because no payload is stored.
            now = self._timestamp(self._now())
            connection.execute(
                "INSERT INTO memory_commands(command_id, companion_id, fingerprint, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (command_id, companion_id, fingerprint, json.dumps({"erased": True}), now),
            )
            self.logs.record(
                companion_id,
                "erase",
                details={"command_id": command_id, "erased": True, "counts": counts},
                connection=connection,
            )
            return {"companion_id": companion_id, "erased": True, "counts": counts}

    def policy_snapshot(self) -> dict:
        automation = self.automatic_memory
        embeddings = self.embedding_memory
        embeddings_enabled = bool(embeddings is not None and embeddings.enabled)
        return {
            **self.policy.model_dump(mode="json"),
            "embeddings_enabled": embeddings_enabled,
            "retrieval_strategy": (
                "local_lexical_semantic_graph_rrf"
                if embeddings_enabled
                else "local_lexical_summary_graph_fusion"
                if automation is not None
                else "local_lexical_cjk_ngrams_importance_recency"
            ),
            "token_estimator": self._estimator_snapshot(),
            "automatic_age_deletion": False,
            "inactive_corrections_retained": True,
            "external_model_providers_configured": bool(
                (
                    automation is not None
                    and automation.provider is not None
                    and automation.model_name
                )
                or (
                    embeddings is not None
                    and embeddings.provider is not None
                    and embeddings.model_name
                )
            ),
            "model_calls": 0,
            "consolidation_mode": "durable_dialogue_batches"
            if automation is not None and automation.enabled
            else "planning_only",
            "quota_scope": "runtime_global",
            "quota_unit": "model_tokens",
            "money_pricing_configured": False,
        }

    def stats(self, companion_id: str) -> dict:
        companion_id = self._identifier(companion_id, "companion_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE companion_id = ?", (companion_id,)
            ).fetchall()
            by_kind = {
                kind: {
                    "total": sum(row["kind"] == kind for row in rows),
                    "active": sum(row["kind"] == kind and row["active"] == 1 for row in rows),
                }
                for kind in _KINDS
            }
            source_count = connection.execute(
                """SELECT COUNT(DISTINCT p.source_event_id) FROM memory_provenance p
                   JOIN memory_records m ON m.memory_id = p.memory_id WHERE m.companion_id = ?""",
                (companion_id,),
            ).fetchone()[0]
            return {
                "companion_id": companion_id,
                "total_records": len(rows),
                "active_records": sum(row["active"] == 1 for row in rows),
                "inactive_records": sum(row["active"] == 0 for row in rows),
                "pinned_records": sum(row["pinned"] == 1 and row["active"] == 1 for row in rows),
                "by_kind": by_kind,
                "source_event_count": source_count,
                "content_utf8_bytes": sum(len(row["content"].encode("utf-8")) for row in rows),
                "stored_content_estimated_tokens": sum(
                    self._estimate(row["content"]) for row in rows
                ),
                "automatic_age_deletion": False,
                "token_estimator": self._estimator_snapshot(),
                "token_quota": self._budget_snapshot(
                    connection, companion_id, self._now().date().isoformat()
                ),
            }

    def plan_consolidation(self, companion_id: str) -> dict:
        companion_id = self._identifier(companion_id, "companion_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE companion_id = ? AND active = 1 AND kind = 'episode' ORDER BY created_at, memory_id",
                (companion_id,),
            ).fetchall()
            triggered = len(rows) >= self.policy.consolidation_threshold
            selected: list[dict] = []
            if triggered:
                for row in rows:
                    if len(selected) >= self.policy.consolidation_batch_size:
                        break
                    item = self._record(connection, row)
                    context = self._format_context(selected + [item], header=_CONSOLIDATION_HEADER)
                    if self._estimate(context) <= self.policy.consolidation_input_token_limit:
                        selected.append(item)
            estimated_input = self._estimate(
                self._format_context(selected, header=_CONSOLIDATION_HEADER)
            )
            output_limit = self.policy.consolidation_output_token_limit if selected else 0
            total = estimated_input + output_limit
            quota = self._budget_snapshot(connection, companion_id, self._now().date().isoformat())
            task_fits = total <= self.policy.per_task_model_token_limit
            budget_available = total <= quota["remaining_tokens"]
            if not triggered:
                reason = "below_batch_threshold"
            elif not selected:
                reason = "no_complete_source_fits_input_budget"
            elif not task_fits:
                reason = "per_task_token_limit"
            elif not budget_available:
                reason = "daily_token_limit"
            else:
                reason = "provider_required_for_execution"
            return {
                "companion_id": companion_id,
                "eligible_episode_count": len(rows),
                "threshold": self.policy.consolidation_threshold,
                "batch_size_limit": self.policy.consolidation_batch_size,
                "triggered": triggered,
                "source_memory_ids": [item["memory_id"] for item in selected],
                "estimated_input_tokens": estimated_input,
                "output_token_limit": output_limit,
                "estimated_total_tokens": total,
                "planned_model_calls": 1 if selected else 0,
                "within_task_limit": task_fits,
                "budget_available": budget_available,
                "execution_enabled": False,
                "model_calls": 0,
                "preserve_source_records": True,
                "derived_summary_persisted": False,
                "reason": reason,
                "token_quota": quota,
            }

    def _budget_snapshot(self, connection: sqlite3.Connection, companion_id: str, day: str) -> dict:
        rows = connection.execute(
            """SELECT companion_id,
                      SUM(CASE WHEN status = 'reserved' THEN reserved_input_tokens + reserved_output_tokens ELSE 0 END) AS reserved_tokens,
                      SUM(CASE WHEN status = 'settled' THEN actual_input_tokens + actual_output_tokens ELSE 0 END) AS spent_tokens
               FROM memory_token_tasks WHERE day = ? GROUP BY companion_id ORDER BY companion_id""",
            (day,),
        ).fetchall()
        breakdown = [
            {
                "companion_id": row["companion_id"],
                "reserved_tokens": row["reserved_tokens"],
                "spent_tokens": row["spent_tokens"],
            }
            for row in rows
        ]
        reserved = sum(row["reserved_tokens"] for row in rows)
        spent = sum(row["spent_tokens"] for row in rows)
        accounted = reserved + spent
        own = next((item for item in breakdown if item["companion_id"] == companion_id), None)
        return {
            "day": day,
            "scope": "runtime_global",
            "unit": "model_tokens",
            "daily_token_limit": self.policy.daily_model_token_limit,
            "reserved_tokens": reserved,
            "spent_tokens": spent,
            "remaining_tokens": max(self.policy.daily_model_token_limit - accounted, 0),
            "over_budget": accounted > self.policy.daily_model_token_limit,
            "over_budget_tokens": max(accounted - self.policy.daily_model_token_limit, 0),
            "companion_reserved_tokens": own["reserved_tokens"] if own else 0,
            "companion_spent_tokens": own["spent_tokens"] if own else 0,
            "by_companion": breakdown,
            "money_pricing_configured": False,
        }

    def budget_snapshot(self, companion_id: str, day: str | None = None) -> dict:
        companion_id = self._identifier(companion_id, "companion_id")
        day = day or self._now().date().isoformat()
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("Noncanonical UTC date")
        except (TypeError, ValueError) as exc:
            raise MemoryError(
                422, "validation_error", "day must be a UTC date in YYYY-MM-DD format"
            ) from exc
        with self._connection() as connection:
            return self._budget_snapshot(connection, companion_id, day)

    def _task_result(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        reserved = row["reserved_input_tokens"] + row["reserved_output_tokens"]
        actual = (
            row["actual_input_tokens"] + row["actual_output_tokens"]
            if row["status"] == "settled"
            else None
        )
        quota = self._budget_snapshot(connection, row["companion_id"], row["day"])
        return {
            "task_id": row["task_id"],
            "companion_id": row["companion_id"],
            "purpose": row["purpose"],
            "day": row["day"],
            "status": row["status"],
            "reserved_input_tokens": row["reserved_input_tokens"],
            "reserved_output_tokens": row["reserved_output_tokens"],
            "reserved_tokens": reserved,
            "actual_input_tokens": row["actual_input_tokens"],
            "actual_output_tokens": row["actual_output_tokens"],
            "actual_tokens": actual,
            "overrun": actual is not None and actual > reserved,
            "overrun_tokens": max(actual - reserved, 0) if actual is not None else 0,
            "over_budget": quota["over_budget"],
            "settled_after_release": row["status"] == "settled" and row["released_at"] is not None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "token_quota": quota,
        }

    def reserve_model_tokens(self, companion_id: str, payload: dict) -> dict:
        """Reserve before a future provider job; retries never create another hold.

        new_reservation tells a job runner whether it created the task. The
        ledger itself is not a worker lock or an exactly-once provider runner.
        Released/settled IDs cannot be reused for another job, even on a new day.
        """
        companion_id = self._identifier(companion_id, "companion_id")
        request = self._validate(_Reservation, payload)
        fingerprint = self._fingerprint(
            {"companion_id": companion_id, **request.model_dump(mode="json")}
        )
        tokens = request.input_tokens + request.output_tokens
        now = self._now()
        day = now.date().isoformat()
        timestamp = self._timestamp(now)
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM memory_token_tasks WHERE task_id = ?", (request.task_id,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise MemoryError(
                        409,
                        "idempotency_conflict",
                        "task_id was already used with another reservation",
                    )
                return {**self._task_result(connection, existing), "new_reservation": False}
            if tokens > self.policy.per_task_model_token_limit:
                raise MemoryError(
                    422,
                    "per_task_token_limit",
                    "Reservation exceeds the per-task model token limit",
                )
            quota = self._budget_snapshot(connection, companion_id, day)
            if tokens > quota["remaining_tokens"]:
                raise MemoryError(
                    409,
                    "model_token_budget_exceeded",
                    "Runtime UTC daily model token quota is exhausted",
                )
            connection.execute(
                """INSERT INTO memory_token_tasks(
                       task_id, companion_id, purpose, day, reserved_input_tokens,
                       reserved_output_tokens, status, fingerprint, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)""",
                (
                    request.task_id,
                    companion_id,
                    request.purpose,
                    day,
                    request.input_tokens,
                    request.output_tokens,
                    fingerprint,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_token_tasks WHERE task_id = ?", (request.task_id,)
            ).fetchone()
            return {**self._task_result(connection, row), "new_reservation": True}

    @staticmethod
    def _find_task(connection: sqlite3.Connection, companion_id: str, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM memory_token_tasks WHERE companion_id = ? AND task_id = ?",
            (companion_id, task_id),
        ).fetchone()
        if row is None:
            raise MemoryError(
                404, "model_task_not_found", "Model token task does not exist for this companion"
            )
        return row

    def settle_model_tokens(self, companion_id: str, task_id: str, payload: dict) -> dict:
        """Persist observed usage, including overruns and usage after a release."""
        companion_id = self._identifier(companion_id, "companion_id")
        task_id = self._identifier(task_id, "task_id")
        request = self._validate(_Settlement, payload)
        with self._connection(write=True) as connection:
            row = self._find_task(connection, companion_id, task_id)
            if row["status"] == "settled":
                if (
                    row["actual_input_tokens"] != request.input_tokens
                    or row["actual_output_tokens"] != request.output_tokens
                ):
                    raise MemoryError(
                        409,
                        "idempotency_conflict",
                        "task_id was already settled with another usage",
                    )
                return self._task_result(connection, row)
            connection.execute(
                """UPDATE memory_token_tasks SET status = 'settled', actual_input_tokens = ?,
                       actual_output_tokens = ?, updated_at = ? WHERE task_id = ?""",
                (
                    request.input_tokens,
                    request.output_tokens,
                    self._timestamp(self._now()),
                    task_id,
                ),
            )
            row = self._find_task(connection, companion_id, task_id)
            return self._task_result(connection, row)

    def release_model_tokens(self, companion_id: str, task_id: str) -> dict:
        """Release a canceled job's hold; settled usage cannot be erased."""
        companion_id = self._identifier(companion_id, "companion_id")
        task_id = self._identifier(task_id, "task_id")
        with self._connection(write=True) as connection:
            row = self._find_task(connection, companion_id, task_id)
            if row["status"] == "settled":
                raise MemoryError(
                    409, "model_task_already_settled", "Observed model usage cannot be released"
                )
            if row["status"] == "reserved":
                now = self._timestamp(self._now())
                connection.execute(
                    "UPDATE memory_token_tasks SET status = 'released', updated_at = ?, released_at = ? WHERE task_id = ?",
                    (now, now, task_id),
                )
                row = self._find_task(connection, companion_id, task_id)
            return self._task_result(connection, row)
