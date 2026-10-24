"""Deterministic retention, protected retrieval and successful-context access checks."""

from datetime import timedelta

import pytest
from test_memory import CharacterEstimator, Clock, source, write
from virtual_life.memory import MemoryService, _effective_retention_policy


@pytest.mark.parametrize(
    "retention,half_life,floor",
    [
        ("routine_episode", 30, 0.1),
        ("significant_episode", 180, 0.3),
        ("stable", 365, 0.5),
        ("permanent", 30, 1),
    ],
)
@pytest.mark.parametrize("days", [0, 30, 90, 180, 365, 100000, -1])
def test_floor_exponential_boundaries(tmp_path, retention, half_life, floor, days):
    clock = Clock()
    service = MemoryService(tmp_path / "decay.sqlite", clock=clock)
    row = write(service, "record", "公园散步", retention_policy=retention)
    factor = service._decay_factor(row, clock.value + timedelta(days=days))
    assert factor == pytest.approx(floor + (1 - floor) * 2 ** (-max(days, 0) / half_life))
    assert floor <= factor <= 1


@pytest.mark.parametrize(
    "kind,key,policy,pinned,expected",
    [
        ("fact", "user.birthday", "auto", False, "permanent"),
        ("fact", "小禾/出生日期", "auto", False, "permanent"),
        ("fact", "user.birth_date", "stable", False, "stable"),
        ("fact", "birthday_party", "auto", False, "stable"),
        ("preference", "birthday", "auto", False, "stable"),
        ("episode", None, "auto", False, "routine_episode"),
        ("episode", None, "significant_episode", False, "significant_episode"),
        ("episode", None, "routine_episode", True, "permanent"),
    ],
)
def test_retention_classification(tmp_path, kind, key, policy, pinned, expected):
    service = MemoryService(tmp_path / "classification.sqlite")
    record = write(
        service,
        "fact",
        "已明确告知的事实",
        kind=kind,
        semantic_key=key,
        retention_policy=policy,
        pinned=pinned,
    )
    assert _effective_retention_policy(record) == expected


def test_metadata_and_correction_have_distinct_decay_anchors(tmp_path):
    clock = Clock()
    service = MemoryService(tmp_path / "anchors.sqlite", clock=clock)
    old = write(service, "old", "喜欢乌龙茶", kind="preference", semantic_key="drink")
    clock.value += timedelta(days=90)
    edited = service.update_metadata(
        "sui",
        old["memory_id"],
        {"command_id": "metadata", "expected_revision": 1, "importance": 0.9},
    )
    assert service._decay_factor(edited, clock.value) < 1
    source(service, "correction-source")
    current = service.correct(
        "sui",
        old["memory_id"],
        {
            "command_id": "correct",
            "expected_revision": 2,
            "content": "改喝无咖啡因薄荷茶",
            "source_event_id": "correction-source",
        },
    )
    assert service._decay_factor(current, clock.value) == 1
    assert current["supersedes_id"] == old["memory_id"]


def test_usage_idempotent_once_per_utc_day_and_relevance(tmp_path):
    clock = Clock()
    service = MemoryService(tmp_path / "usage.sqlite", clock=clock)
    record = write(service, "record", "公园散步")
    refs = [{"memory_id": record["memory_id"], "revision": 1}]
    for command, query in [
        ("a", "公园散步"),
        ("a", "公园散步"),
        ("b", "公园散步"),
        ("c", "深海潜水"),
    ]:
        with service._connection(write=True) as connection:
            service.record_dialogue_usage(connection, "sui", command, refs, query, clock.value)
    with service._connection() as connection:
        state = dict(connection.execute("SELECT * FROM memory_access_state").fetchone())
        assert state["exposure_count"] == 3
        assert (
            connection.execute("SELECT SUM(reinforced) FROM memory_usage_receipts").fetchone()[0]
            == 1
        )
    clock.value += timedelta(days=1)
    with service._connection(write=True) as connection:
        service.record_dialogue_usage(connection, "sui", "next-day", refs, "公园散步", clock.value)
        assert (
            connection.execute("SELECT SUM(reinforced) FROM memory_usage_receipts").fetchone()[0]
            == 2
        )


def test_exact_slot_survives_candidate_limits_and_respects_budget(tmp_path):
    clock = Clock()
    service = MemoryService(
        tmp_path / "exact.sqlite",
        clock=clock,
        token_estimator=CharacterEstimator(),
        policy={
            "lexical_candidate_limit": 1,
            "background_candidate_limit": 1,
            "decay": {"permanent_candidate_limit": 1},
        },
    )
    birthday = write(
        service, "birthday", "林舟生日6月18日", kind="fact", semantic_key="user.birthday"
    )
    for i in range(8):
        clock.value += timedelta(days=1)
        write(service, f"noise{i}", f"无关日常{i}", importance=1.0)
    found = service.recall("sui", {"semantic_key": "user.birthday", "token_budget": 200})
    assert [x["memory_id"] for x in found["items"]] == [birthday["memory_id"]]
    assert service.recall("other", {"semantic_key": "user.birthday"})["items"] == []
    assert (
        service.recall("sui", {"semantic_key": "user.birthday", "token_budget": 0})["items"] == []
    )
    with service._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_access_state").fetchone()[0] == 0


def test_previous_schema_and_receipt_default_retention_upgrade(tmp_path):
    import json

    clock = Clock()
    path = tmp_path / "legacy.sqlite"
    memory = MemoryService(path, clock=clock)
    record = write(memory, "legacy", "旧事实", kind="fact")
    with memory._connection(write=True) as connection:
        connection.execute("ALTER TABLE memory_records DROP COLUMN retention_policy")
        connection.execute("DELETE FROM memory_schema_versions WHERE version=2")
        receipt = {
            key: value
            for key, value in record.items()
            if key not in ("retention_policy", "effective_retention_policy")
        }
        connection.execute(
            "UPDATE memory_commands SET result_json=? WHERE command_id=?",
            (json.dumps(receipt), "legacy"),
        )
    upgraded = MemoryService(path, clock=clock)
    replay = write(upgraded, "legacy", "旧事实", kind="fact", retention_policy="auto")
    # Command receipts preserve the original snapshot; response models add defaults.
    assert replay == receipt
    from virtual_life.responses import MemoryRecord

    assert MemoryRecord.model_validate(replay).retention_policy == "auto"
    assert upgraded.list("sui")["items"][0]["retention_policy"] == "auto"
    assert write(upgraded, "legacy", "旧事实", kind="fact") == replay
