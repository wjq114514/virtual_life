"""Persistent companion personality, archived V2 JSON cards, and expiring state.

The card importer accepts a bounded JSON subset. Archived prompt overrides,
character books, and extensions are never activated by this service.
"""

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .coordinator import MAX_VERSION, PROTOCOL_VERSION
from .models import MAX_PAYLOAD_DEPTH, Identifier, JsonObject, RequestModel, Version, bounded_json
from .storage import SQLiteStore

_Model = TypeVar("_Model", bound=BaseModel)
_JsonDict = dict[str, Any]
_Mutation = Callable[[sqlite3.Connection, datetime, sqlite3.Row], _JsonDict]
_IdentifierAdapter = TypeAdapter(Identifier)

_ValueText = Annotated[str, StringConstraints(max_length=128)]
_HabitText = Annotated[str, StringConstraints(max_length=160)]
_GreetingText = Annotated[str, StringConstraints(max_length=1024)]
ImportWarning = Literal[
    "card_name_differs",
    "system_prompt_ignored",
    "post_history_instructions_ignored",
    "character_book_ignored",
    "extensions_ignored",
]


class PersonalityError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class _BoundedModel(RequestModel):
    @model_validator(mode="before")
    @classmethod
    def bound_input(cls, value: Any) -> Any:
        if isinstance(value, dict):
            try:
                bounded_json(value)
            except RecursionError as exc:
                raise ValueError(f"Payload nesting must not exceed {MAX_PAYLOAD_DEPTH}") from exc
        return value

    @model_validator(mode="after")
    def bound_complete_value(self) -> "_BoundedModel":
        bounded_json(self.model_dump(mode="json"))
        return self


class PersonalityTraits(RequestModel):
    warmth: float = Field(default=0.7, ge=0, le=1)
    curiosity: float = Field(default=0.7, ge=0, le=1)
    assertiveness: float = Field(default=0.6, ge=0, le=1)
    playfulness: float = Field(default=0.3, ge=0, le=1)
    initiative: float = Field(default=0.3, ge=0, le=1)


class PersonalitySpeechStyle(RequestModel):
    language: str = Field(default="zh-CN", max_length=64)
    tone: str = Field(default="温和、自然，有自己的观点", max_length=512)
    response_length: Literal["brief", "balanced", "detailed"] = "brief"
    habits: list[_HabitText] = Field(default_factory=list, max_length=8)


class PersonalityDefinition(_BoundedModel):
    description: str = Field(
        default="{{char}} 是陪你共读、生活和照看设备的虚拟伙伴。", max_length=4096
    )
    personality: str = Field(default="温和而独立，对你的想法和周围事物保持好奇。", max_length=2048)
    values: list[_ValueText] = Field(
        default_factory=lambda: ["尊重你的节奏", "诚实表达自己的判断", "珍惜共同经历"],
        max_length=8,
    )
    boundaries: list[_ValueText] = Field(
        default_factory=lambda: [
            "不编造共同经历或未取得的视觉信息",
            "不把设备在线等同于你正在关注它",
        ],
        max_length=8,
    )
    traits: PersonalityTraits = Field(default_factory=PersonalityTraits)
    speech_style: PersonalitySpeechStyle = Field(default_factory=PersonalitySpeechStyle)
    scenario: str = Field(default="随着你在设备间移动，接续当前活动。", max_length=2048)
    first_message: str = Field(default="我在。想一起读一会儿，还是先安静待着？", max_length=1024)
    example_dialogue: str = Field(
        default=(
            "<START>\n{{user}}: 今天有点累。\n{{char}}: 那我们先安静待一会儿，不急着追下一件事。"
        ),
        max_length=4096,
    )
    alternate_greetings: list[_GreetingText] = Field(default_factory=list, max_length=8)


class PersonalitySnapshot(RequestModel):
    companion_id: Identifier
    revision: Version
    configured: bool
    definition: PersonalityDefinition
    source_card: JsonObject | None = None
    import_warnings: list[ImportWarning] = Field(default_factory=list, max_length=5)
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("source_card")
    @classmethod
    def bound_source_card(cls, value: JsonObject | None) -> JsonObject | None:
        return bounded_json(value) if value is not None else None


class PersonalityUpdate(_BoundedModel):
    command_id: Identifier
    expected_revision: Version
    definition: PersonalityDefinition


def _metadata_types(
    value: JsonObject,
    location: str,
    *,
    strings: tuple[str, ...] = (),
    booleans: tuple[str, ...] = (),
    numbers: tuple[str, ...] = (),
    objects: tuple[str, ...] = (),
    string_arrays: tuple[str, ...] = (),
) -> None:
    """Check present standard metadata without dropping unknown archive fields."""
    for names, expected, label in (
        (strings, str, "a string"),
        (booleans, bool, "a boolean"),
        (objects, dict, "an object"),
    ):
        for name in names:
            if name in value and not isinstance(value[name], expected):
                raise ValueError(f"{location}.{name} must be {label}")
    for name in numbers:
        if name not in value:
            continue
        number = value[name]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or (isinstance(number, float) and not math.isfinite(number))
        ):
            raise ValueError(f"{location}.{name} must be a finite number")
    for name in string_arrays:
        if name in value and (
            not isinstance(value[name], list)
            or any(not isinstance(item, str) for item in value[name])
        ):
            raise ValueError(f"{location}.{name} must be an array of strings")


def _validate_card(card: JsonObject) -> None:
    if card.get("spec") != "chara_card_v2" or card.get("spec_version") != "2.0":
        raise ValueError("card requires spec='chara_card_v2' and spec_version='2.0'")
    data = card.get("data")
    if not isinstance(data, dict):
        raise ValueError("card.data must be an object")
    for name in ("name", "description", "personality", "scenario", "first_mes", "mes_example"):
        if name not in data or not isinstance(data[name], str):
            raise ValueError(f"card.data.{name} must be a string")
    _metadata_types(
        data,
        "card.data",
        strings=(
            "creator_notes",
            "system_prompt",
            "post_history_instructions",
            "creator",
            "character_version",
        ),
        objects=("extensions", "character_book"),
        string_arrays=("alternate_greetings", "tags"),
    )
    book = data.get("character_book")
    if book is None:
        return
    _metadata_types(
        book,
        "card.data.character_book",
        strings=("name", "description"),
        numbers=("scan_depth", "token_budget"),
        booleans=("recursive_scanning",),
        objects=("extensions",),
    )
    if "entries" not in book:
        return
    entries = book["entries"]
    if not isinstance(entries, list):
        raise ValueError("card.data.character_book.entries must be an array of objects")
    for index, entry in enumerate(entries):
        location = f"card.data.character_book.entries.{index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{location} must be an object")
        _metadata_types(
            entry,
            location,
            strings=("content", "name", "comment", "position"),
            booleans=("enabled", "case_sensitive", "selective", "constant"),
            numbers=("insertion_order", "priority", "id"),
            objects=("extensions",),
            string_arrays=("keys", "secondary_keys"),
        )
        if "position" in entry and entry["position"] not in ("before_char", "after_char"):
            raise ValueError(f"{location}.position must be 'before_char' or 'after_char'")


def _definition_from_card(card: JsonObject) -> PersonalityDefinition:
    data = card["data"]
    return PersonalityDefinition.model_validate(
        {
            "description": data["description"],
            "personality": data["personality"],
            "scenario": data["scenario"],
            "first_message": data["first_mes"],
            "example_dialogue": data["mes_example"],
            "alternate_greetings": data.get("alternate_greetings", []),
        }
    )


class PersonalityCardImport(_BoundedModel):
    command_id: Identifier
    expected_revision: Version
    card: JsonObject

    @field_validator("card")
    @classmethod
    def validate_card(cls, value: JsonObject) -> JsonObject:
        bounded_json(value)
        _validate_card(value)
        _definition_from_card(value)
        return value


class PersonalityState(RequestModel):
    mood: Literal[
        "calm",
        "cheerful",
        "curious",
        "concerned",
        "tired",
        "amused",
        "excited",
        "surprised",
        "sympathetic",
        "annoyed",
        "proud",
    ] = "calm"
    energy: float = Field(default=0.8, ge=0, le=1)
    social_battery: float = Field(default=0.7, ge=0, le=1)


class PersonalityStateUpdate(_BoundedModel):
    command_id: Identifier
    expected_revision: Version
    state: PersonalityState
    ttl_seconds: int = Field(default=1800, ge=30, le=86_400)
    source_event_id: Identifier | None = None


class PersonalityStateSnapshot(RequestModel):
    companion_id: Identifier
    revision: Version
    state: PersonalityState
    recorded_state: PersonalityState | None = None
    expired: bool
    source_event_id: Identifier | None = None
    expires_at: str | None = None
    updated_at: str | None = None


ProfileRevisionSource = Literal["owner", "import", "evolution", "pre_migration"]


class PersonalityRevisionEntry(RequestModel):
    companion_id: Identifier
    revision: Version
    definition: PersonalityDefinition
    source: ProfileRevisionSource
    source_ref: str | None = Field(default=None, max_length=128)
    created_at: str


class PersonalityHistoryPage(RequestModel):
    companion_id: Identifier
    items: list[PersonalityRevisionEntry]
    next_before_revision: Version | None = None
    has_more: bool


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS personality_profiles (
        companion_id TEXT PRIMARY KEY REFERENCES companions(companion_id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        definition_json TEXT NOT NULL,
        source_card_json TEXT,
        import_warnings_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS personality_states (
        companion_id TEXT PRIMARY KEY REFERENCES companions(companion_id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        state_json TEXT NOT NULL,
        source_event_id TEXT REFERENCES events(event_id),
        expires_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS personality_commands (
        command_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL REFERENCES companions(companion_id),
        operation TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS personality_profile_revisions (
        companion_id TEXT NOT NULL REFERENCES companions(companion_id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        definition_json TEXT NOT NULL,
        source TEXT NOT NULL,
        source_ref TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (companion_id, revision)
    )""",
)


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _has_extensions(data: JsonObject) -> bool:
    if data.get("extensions"):
        return True
    book = data.get("character_book")
    if not isinstance(book, dict):
        return False
    return bool(book.get("extensions")) or any(
        bool(entry.get("extensions")) for entry in book.get("entries", [])
    )


class PersonalityService:
    """Independent revisions with atomic state, event, and command receipt writes."""

    def __init__(
        self,
        database_path: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = SQLiteStore(database_path)
        self.clock = clock or (lambda: datetime.now(UTC))
        with self.store.transaction(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise TypeError("The personality clock must return a datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _identifier(value: str) -> str:
        try:
            return _IdentifierAdapter.validate_python(value, strict=True)
        except ValidationError as exc:
            raise PersonalityError(
                422,
                "validation_error",
                "Identifiers must be nonempty strings up to 128 characters",
            ) from exc

    @staticmethod
    def _validate(model: type[_Model], payload: _JsonDict) -> _Model:
        if not isinstance(payload, dict):
            raise PersonalityError(422, "validation_error", "Payload must be a JSON object")
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            error = exc.errors(include_url=False, include_input=False)[0]
            location = ".".join(str(part) for part in error["loc"]) or "payload"
            raise PersonalityError(422, "validation_error", f"{location}: {error['msg']}") from exc

    @staticmethod
    def _companion(connection: sqlite3.Connection, companion_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT companion_id, name FROM companions WHERE companion_id = ?", (companion_id,)
        ).fetchone()
        if row is None:
            raise PersonalityError(404, "not_found", f"companion {companion_id!r} was not found")
        return row

    @staticmethod
    def _profile_row(connection: sqlite3.Connection, companion_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM personality_profiles WHERE companion_id = ?", (companion_id,)
        ).fetchone()

    @staticmethod
    def _state_row(connection: sqlite3.Connection, companion_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM personality_states WHERE companion_id = ?", (companion_id,)
        ).fetchone()

    @staticmethod
    def _profile_snapshot(companion_id: str, row: sqlite3.Row | None) -> _JsonDict:
        return {
            "companion_id": companion_id,
            "revision": row["revision"] if row is not None else 0,
            "configured": row is not None,
            "definition": (
                json.loads(row["definition_json"])
                if row is not None
                else PersonalityDefinition().model_dump(mode="json")
            ),
            "source_card": (
                json.loads(row["source_card_json"])
                if row is not None and row["source_card_json"] is not None
                else None
            ),
            "import_warnings": json.loads(row["import_warnings_json"]) if row is not None else [],
            "created_at": row["created_at"] if row is not None else None,
            "updated_at": row["updated_at"] if row is not None else None,
        }

    @staticmethod
    def _state_snapshot(companion_id: str, row: sqlite3.Row | None, now: datetime) -> _JsonDict:
        recorded_state = json.loads(row["state_json"]) if row is not None else None
        expired = (
            row is not None and datetime.fromisoformat(row["expires_at"]).astimezone(UTC) <= now
        )
        return {
            "companion_id": companion_id,
            "revision": row["revision"] if row is not None else 0,
            "state": (
                recorded_state
                if recorded_state is not None and not expired
                else PersonalityState().model_dump(mode="json")
            ),
            "recorded_state": recorded_state,
            "expired": expired,
            "source_event_id": row["source_event_id"] if row is not None else None,
            "expires_at": row["expires_at"] if row is not None else None,
            "updated_at": row["updated_at"] if row is not None else None,
        }

    @staticmethod
    def _check_revision(row: sqlite3.Row | None, expected_revision: int, subject: str) -> int:
        revision = row["revision"] if row is not None else 0
        if revision != expected_revision:
            raise PersonalityError(
                409,
                "revision_conflict",
                f"expected_revision does not match the current {subject} revision",
            )
        if revision >= MAX_VERSION:
            raise PersonalityError(
                409, "version_exhausted", f"The {subject} revision cannot be incremented"
            )
        return revision + 1

    @staticmethod
    def _emit(
        connection: sqlite3.Connection,
        companion_id: str,
        now: datetime,
        event_type: str,
        payload: _JsonDict,
    ) -> None:
        connection.execute(
            """INSERT INTO events(event_id, protocol_version, type, companion_id, node_id,
                recipient_node_id, payload_json, occurred_at)
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)""",
            (
                str(uuid.uuid4()),
                PROTOCOL_VERSION,
                event_type,
                companion_id,
                _json(payload),
                _timestamp(now),
            ),
        )

    @staticmethod
    def _write_profile(
        connection: sqlite3.Connection,
        companion_id: str,
        revision: int,
        definition: _JsonDict,
        now: datetime,
    ) -> None:
        """Replace the definition while preserving any archived import card."""
        timestamp = _timestamp(now)
        connection.execute(
            """INSERT INTO personality_profiles(companion_id, revision, definition_json,
                source_card_json, import_warnings_json, created_at, updated_at)
                VALUES (?, ?, ?, NULL, '[]', ?, ?)
                ON CONFLICT(companion_id) DO UPDATE SET revision = excluded.revision,
                definition_json = excluded.definition_json, updated_at = excluded.updated_at""",
            (companion_id, revision, _json(definition), timestamp, timestamp),
        )

    @staticmethod
    def _record_revision(
        connection: sqlite3.Connection,
        companion_id: str,
        revision: int,
        definition: _JsonDict,
        source: str,
        source_ref: str | None,
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO personality_profile_revisions
                (companion_id, revision, definition_json, source, source_ref, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
            (companion_id, revision, _json(definition), source, source_ref, _timestamp(now)),
        )

    def _write(
        self,
        companion_id: str,
        operation: str,
        command: _JsonDict,
        action: _Mutation,
    ) -> _JsonDict:
        fingerprint = hashlib.sha256(
            _json(
                {"companion_id": companion_id, "operation": operation, "payload": command}
            ).encode("utf-8")
        ).hexdigest()
        with self.store.transaction(write=True) as connection:
            previous = connection.execute(
                "SELECT fingerprint, result_json FROM personality_commands WHERE command_id = ?",
                (command["command_id"],),
            ).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise PersonalityError(
                        409,
                        "idempotency_conflict",
                        "command_id has already been used for another personality request",
                    )
                return json.loads(previous["result_json"])
            companion = self._companion(connection, companion_id)
            now = self._now()
            result = action(connection, now, companion)
            connection.execute(
                """INSERT INTO personality_commands(command_id, companion_id, operation,
                    fingerprint, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    command["command_id"],
                    companion_id,
                    operation,
                    fingerprint,
                    _json(result),
                    _timestamp(now),
                ),
            )
            return result

    def get(self, companion_id: str) -> _JsonDict:
        companion_id = self._identifier(companion_id)
        with self.store.transaction() as connection:
            self._companion(connection, companion_id)
            return self._profile_snapshot(companion_id, self._profile_row(connection, companion_id))

    def update(self, companion_id: str, payload: _JsonDict) -> _JsonDict:
        companion_id = self._identifier(companion_id)
        data = self._validate(PersonalityUpdate, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime, _: sqlite3.Row) -> _JsonDict:
            row = self._profile_row(connection, companion_id)
            revision = self._check_revision(row, data["expected_revision"], "personality")
            timestamp = _timestamp(now)
            connection.execute(
                """INSERT INTO personality_profiles(companion_id, revision, definition_json,
                    source_card_json, import_warnings_json, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, '[]', ?, ?)
                    ON CONFLICT(companion_id) DO UPDATE SET revision = excluded.revision,
                    definition_json = excluded.definition_json, updated_at = excluded.updated_at""",
                (companion_id, revision, _json(data["definition"]), timestamp, timestamp),
            )
            self._record_revision(
                connection,
                companion_id,
                revision,
                data["definition"],
                "owner",
                data["command_id"],
                now,
            )
            self._emit(
                connection,
                companion_id,
                now,
                "companion.personality_updated",
                {"personality_revision": revision},
            )
            return self._profile_snapshot(companion_id, self._profile_row(connection, companion_id))

        return self._write(companion_id, "update", data, action)

    def import_card(self, companion_id: str, payload: _JsonDict) -> _JsonDict:
        companion_id = self._identifier(companion_id)
        data = self._validate(PersonalityCardImport, payload).model_dump(mode="json")
        card = data["card"]
        definition = _definition_from_card(card).model_dump(mode="json")

        def action(
            connection: sqlite3.Connection, now: datetime, companion: sqlite3.Row
        ) -> _JsonDict:
            row = self._profile_row(connection, companion_id)
            revision = self._check_revision(row, data["expected_revision"], "personality")
            card_data = card["data"]
            warnings: list[ImportWarning] = []
            if card_data["name"] != companion["name"]:
                warnings.append("card_name_differs")
            for field, warning in (
                ("system_prompt", "system_prompt_ignored"),
                ("post_history_instructions", "post_history_instructions_ignored"),
                ("character_book", "character_book_ignored"),
            ):
                if card_data.get(field):
                    warnings.append(warning)
            if _has_extensions(card_data):
                warnings.append("extensions_ignored")
            timestamp = _timestamp(now)
            connection.execute(
                """INSERT INTO personality_profiles(companion_id, revision, definition_json,
                    source_card_json, import_warnings_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(companion_id) DO UPDATE SET revision = excluded.revision,
                    definition_json = excluded.definition_json,
                    source_card_json = excluded.source_card_json,
                    import_warnings_json = excluded.import_warnings_json,
                    updated_at = excluded.updated_at""",
                (
                    companion_id,
                    revision,
                    _json(definition),
                    _json(card),
                    _json(warnings),
                    timestamp,
                    timestamp,
                ),
            )
            self._record_revision(
                connection,
                companion_id,
                revision,
                definition,
                "import",
                data["command_id"],
                now,
            )
            self._emit(
                connection,
                companion_id,
                now,
                "companion.personality_updated",
                {"personality_revision": revision},
            )
            return self._profile_snapshot(companion_id, self._profile_row(connection, companion_id))

        return self._write(companion_id, "import_card", data, action)

    def get_state(self, companion_id: str) -> _JsonDict:
        companion_id = self._identifier(companion_id)
        with self.store.transaction() as connection:
            self._companion(connection, companion_id)
            return self._state_snapshot(
                companion_id, self._state_row(connection, companion_id), self._now()
            )

    def update_state(self, companion_id: str, payload: _JsonDict) -> _JsonDict:
        companion_id = self._identifier(companion_id)
        data = self._validate(PersonalityStateUpdate, payload).model_dump(mode="json")

        def action(connection: sqlite3.Connection, now: datetime, _: sqlite3.Row) -> _JsonDict:
            row = self._state_row(connection, companion_id)
            revision = self._check_revision(row, data["expected_revision"], "personality state")
            if data["source_event_id"] is not None:
                event = connection.execute(
                    "SELECT companion_id FROM events WHERE event_id = ?", (data["source_event_id"],)
                ).fetchone()
                if event is None:
                    raise PersonalityError(
                        404, "not_found", f"event {data['source_event_id']!r} was not found"
                    )
                if event["companion_id"] not in (None, companion_id):
                    raise PersonalityError(
                        409,
                        "source_event_conflict",
                        "The source event belongs to another companion",
                    )
            timestamp = _timestamp(now)
            expires_at = _timestamp(now + timedelta(seconds=data["ttl_seconds"]))
            connection.execute(
                """INSERT INTO personality_states(companion_id, revision, state_json,
                    source_event_id, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(companion_id) DO UPDATE SET revision = excluded.revision,
                    state_json = excluded.state_json, source_event_id = excluded.source_event_id,
                    expires_at = excluded.expires_at, updated_at = excluded.updated_at""",
                (
                    companion_id,
                    revision,
                    _json(data["state"]),
                    data["source_event_id"],
                    expires_at,
                    timestamp,
                ),
            )
            self._emit(
                connection,
                companion_id,
                now,
                "companion.personality_state_updated",
                {"state_revision": revision, "expires_at": expires_at},
            )
            return self._state_snapshot(
                companion_id, self._state_row(connection, companion_id), now
            )

        return self._write(companion_id, "update_state", data, action)

    def history(
        self, companion_id: str, *, before_revision: int | None = None, limit: int = 20
    ) -> _JsonDict:
        """Read-only timeline of profile revisions, newest first."""
        companion_id = self._identifier(companion_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise PersonalityError(422, "validation_error", "limit must be between 1 and 100")
        if before_revision is not None and (
            type(before_revision) is not int or before_revision < 0
        ):
            raise PersonalityError(
                422, "validation_error", "before_revision must be a nonnegative integer"
            )
        with self.store.transaction() as connection:
            self._companion(connection, companion_id)
            count = connection.execute(
                "SELECT COUNT(*) FROM personality_profile_revisions WHERE companion_id = ?",
                (companion_id,),
            ).fetchone()[0]
            if count == 0:
                # Pre-migration databases have no timeline rows. Present the current
                # profile as a synthetic entry without writing during a read.
                row = self._profile_row(connection, companion_id)
                if row is None:
                    return {
                        "companion_id": companion_id,
                        "items": [],
                        "next_before_revision": None,
                        "has_more": False,
                    }
                items = [
                    {
                        "companion_id": companion_id,
                        "revision": row["revision"],
                        "definition": json.loads(row["definition_json"]),
                        "source": "pre_migration",
                        "source_ref": None,
                        "created_at": row["updated_at"],
                    }
                ]
                return {
                    "companion_id": companion_id,
                    "items": items,
                    "next_before_revision": row["revision"],
                    "has_more": False,
                }
            clause = " AND revision < ?" if before_revision is not None else ""
            params: list = [companion_id]
            if before_revision is not None:
                params.append(before_revision)
            rows = connection.execute(
                f"""SELECT * FROM personality_profile_revisions WHERE companion_id = ?{clause}
                    ORDER BY revision DESC LIMIT ?""",
                (*params, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            items = [
                {
                    "companion_id": companion_id,
                    "revision": row["revision"],
                    "definition": json.loads(row["definition_json"]),
                    "source": row["source"],
                    "source_ref": row["source_ref"],
                    "created_at": row["created_at"],
                }
                for row in rows[:limit]
            ]
            return {
                "companion_id": companion_id,
                "items": items,
                "next_before_revision": items[-1]["revision"] if items else None,
                "has_more": has_more,
            }
