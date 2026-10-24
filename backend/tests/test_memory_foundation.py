"""Evidence, revision and temporal foundations without external providers."""

import sqlite3
from datetime import UTC, datetime

import pytest
from test_memory import Clock, assert_error, source, write
from virtual_life.memory import _SCHEMA, MemoryError, MemoryService


def test_episode_identity_and_source_validation(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite")
    first = write(service, "one", "same day", occurred_at="2020-01-01T08:00:00+08:00")
    second = write(service, "two", "same day", occurred_at="2020-01-02T00:00:00Z")
    assert first["memory_id"] != second["memory_id"]
    assert first["occurred_at"] == "2020-01-01T00:00:00.000000Z"
    again = write(service, "three", "same day", source_event_id="event-one")
    assert again["memory_id"] == first["memory_id"]
    assert_error(
        "episode_identity_conflict",
        write,
        service,
        "four",
        "different",
        source_event_id="event-one",
    )
    payload = {"command_id": "bad", "kind": "fact", "content": "fact", "source_event_id": "missing"}
    assert_error("invalid_memory_source", service.write, "sui", payload)
    source(service, "foreign", "other")
    payload["source_event_id"] = "foreign"
    assert_error("invalid_memory_source", service.write, "sui", payload)
    payload["source_event_id"] = "event-one"
    payload["source_node_id"] = "unregistered"
    assert_error("invalid_memory_source", service.write, "sui", payload)


def test_correction_metadata_references_and_temporal_recall(tmp_path):
    clock = Clock()
    service = MemoryService(tmp_path / "memory.sqlite", clock=clock)
    old = write(service, "old", "likes tea", kind="preference", semantic_key="drink", pinned=True)
    assert_error(
        "memory_correction_required",
        write,
        service,
        "conflict",
        "likes coffee",
        kind="preference",
        semantic_key="drink",
    )
    source(service, "correction")
    payload = {
        "command_id": "correct",
        "expected_revision": 1,
        "content": "likes coffee",
        "source_event_id": "correction",
    }
    new = service.correct("sui", old["memory_id"], payload)
    assert new["supersedes_id"] == old["memory_id"] and new["pinned"]
    assert service.correct("sui", old["memory_id"], payload) == new
    assert_error(
        "memory_revision_conflict",
        service.correct,
        "sui",
        old["memory_id"],
        {**payload, "command_id": "stale"},
    )
    edited = service.update_metadata(
        "sui",
        new["memory_id"],
        {
            "command_id": "edit",
            "expected_revision": 1,
            "confidence": 0.1,
            "importance": 0.0,
            "pinned": False,
            "confirmed": False,
            "tags": [],
        },
    )
    assert edited["revision"] == 2 and edited["confidence"] == 0.1 and not edited["pinned"]
    with service._connection() as connection:
        assert_error(
            "memory_context_changed",
            service.validate_references,
            connection,
            "sui",
            [{"memory_id": new["memory_id"], "revision": 1}],
        )
        service.validate_references(
            connection, "sui", [{"memory_id": edited["memory_id"], "revision": 2}]
        )
    write(
        service,
        "future",
        "future fact",
        kind="fact",
        valid_from="2030-01-01T00:00:00Z",
        pinned=True,
    )
    write(
        service,
        "expired",
        "expired fact",
        kind="fact",
        valid_to="2019-01-01T00:00:00Z",
        pinned=True,
    )
    episode = write(service, "episode", "episode")
    assert [
        item["memory_id"] for item in service.recall("sui", {"layer": "semantic"})["items"]
    ] == [new["memory_id"]]
    assert [
        item["memory_id"] for item in service.recall("sui", {"layer": "episodic"})["items"]
    ] == [episode["memory_id"]]
    service.delete("sui", new["memory_id"])
    assert service.correct("sui", old["memory_id"], payload) == new
    assert new["memory_id"] not in {item["memory_id"] for item in service.list("sui")["items"]}


def test_legacy_schema_migration_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as connection:
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO memory_records VALUES ('legacy', 'sui', 'episode', 'untouched', 'untouched', NULL, '[]', .5, 1, 0, 0, 1, '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
        )
        connection.execute("INSERT INTO memory_provenance VALUES ('legacy', 'old-event', '')")
    for _ in range(2):
        service = MemoryService(path, clock=lambda: datetime(2020, 1, 1, tzinfo=UTC))
        record = service.list("sui")["items"][0]
        assert record["revision"] == 1 and record["occurred_at"] is None
        assert record["content"] == "untouched" and record["source_event_ids"] == ["old-event"]
    with service._connection() as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT version FROM memory_schema_versions ORDER BY version"
            )
        ] == [1, 2]
        assert record["retention_policy"] == "auto"


def test_sources_fail_without_runtime_tables_and_naive_time_rejected(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite")
    payload = {
        "command_id": "one",
        "kind": "episode",
        "content": "event",
        "source_event_id": "event",
    }
    assert_error("invalid_memory_source", service.write, "sui", payload)
    assert_error(
        "validation_error", service.write, "sui", {**payload, "occurred_at": "2020-01-01T00:00:00"}
    )
    with pytest.raises(MemoryError):
        service.update_metadata(
            "sui", "missing", {"command_id": "null", "expected_revision": 1, "pinned": None}
        )


def test_concurrent_metadata_compare_and_swap(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service = MemoryService(tmp_path / "memory.sqlite")
    record = write(service, "first", "a fact", kind="fact")
    barrier = Barrier(2)

    def edit(command):
        barrier.wait()
        try:
            return service.update_metadata(
                "sui",
                record["memory_id"],
                {"command_id": command, "expected_revision": 1, "pinned": True},
            )
        except MemoryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["a", "b"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "memory_revision_conflict" in results
    assert service.list("sui")["items"][0]["revision"] == 2


def test_legacy_command_fingerprint_replays_after_delete(tmp_path):
    import json

    from virtual_life.memory import MemoryWrite

    service = MemoryService(tmp_path / "memory.sqlite")
    payload = {
        "command_id": "old",
        "kind": "episode",
        "content": "old body",
        "source_event_id": "old-event",
    }
    old_payload = MemoryWrite.model_validate(payload).model_dump(mode="json")
    for key in ("occurred_at", "episode_key", "valid_from", "valid_to", "retention_policy"):
        del old_payload[key]
    result = {"memory_id": "deleted", "content": "old body"}
    with service._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO memory_commands VALUES (?, ?, ?, ?, ?)",
            (
                "old",
                "sui",
                service._fingerprint({"companion_id": "sui", **old_payload}),
                json.dumps(result),
                "2020-01-01T00:00:00Z",
            ),
        )
    assert service.write("sui", payload) == result
    assert service.list("sui")["total"] == 0


def test_identity_namespaces_and_time_conflicts(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite")
    first = write(service, "a", "episode", source_event_id="same")
    keyed = write(service, "b", "episode", episode_key="same")
    assert first["memory_id"] != keyed["memory_id"]
    filled = write(
        service, "c", "episode", source_event_id="same", occurred_at="2020-01-01T00:00:00Z"
    )
    assert filled["occurred_at"] == "2020-01-01T00:00:00.000000Z"
    assert filled["revision"] == 2
    assert_error(
        "episode_identity_conflict",
        write,
        service,
        "d",
        "episode",
        source_event_id="same",
        occurred_at="2020-01-02T00:00:00Z",
    )
    stable = write(service, "stable", "fact", kind="fact", valid_from="2020-01-01T00:00:00Z")
    assert_error(
        "memory_correction_required",
        write,
        service,
        "changed",
        "fact",
        kind="fact",
        valid_from="2021-01-01T00:00:00Z",
    )
    assert_error(
        "memory_correction_required",
        write,
        service,
        "known-time",
        "fact",
        kind="fact",
        occurred_at="2020-01-01T00:00:00Z",
    )
    assert stable["revision"] == 1
    assert_error("validation_error", write, service, "key", "episode", semantic_key="bad")


def test_revision_bounds_and_correction_window(tmp_path):
    service = MemoryService(tmp_path / "memory.sqlite", clock=Clock())
    record = write(service, "fact", "fact", kind="fact")
    source(service, "correct-source")
    correction = {
        "command_id": "correct",
        "expected_revision": 1,
        "content": "new",
        "source_event_id": "correct-source",
    }
    assert_error(
        "validation_error",
        service.correct,
        "sui",
        record["memory_id"],
        {**correction, "valid_from": "2021-01-01T00:00:00Z"},
    )
    assert_error(
        "validation_error",
        service.correct,
        "sui",
        record["memory_id"],
        {**correction, "valid_from": None},
    )
    new = service.correct("sui", record["memory_id"], {**correction, "tags": ["Tea", "tea"]})
    assert new["tags"] == ["Tea"]
    with service._connection(write=True) as connection:
        connection.execute(
            "UPDATE memory_records SET revision = ? WHERE memory_id = ?",
            (2**63 - 1, new["memory_id"]),
        )
    edit = {"command_id": "edit", "expected_revision": 2**63 - 1, "pinned": True}
    assert_error("memory_revision_conflict", service.update_metadata, "sui", new["memory_id"], edit)
    assert_error(
        "validation_error",
        service.update_metadata,
        "sui",
        new["memory_id"],
        {**edit, "expected_revision": 2**63},
    )
    assert_error(
        "validation_error",
        write,
        service,
        "overflow",
        "fact",
        occurred_at="0001-01-01T00:00:00+23:59",
    )
    assert_error("validation_error", write, service, "huge", "fact", occurred_at="2" * 65)
