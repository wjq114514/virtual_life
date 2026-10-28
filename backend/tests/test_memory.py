"""Memory retention, useful retrieval and durable cost-control behavior."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from virtual_life.memory import MemoryError, MemoryPolicy, MemoryService
from virtual_life.ports import ParticipationContext, QuietParticipationPolicy
from virtual_life.storage import SQLiteStore


class Clock:
    def __init__(self):
        self.value = datetime(2020, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value


class CharacterEstimator:
    name = "test_character_estimate"

    def estimate(self, text):
        return len(text)


def source(service, event_id, companion="sui", node_id=None):
    store = SQLiteStore(service.database_path)
    with store.transaction() as connection:
        if node_id is not None:
            connection.execute(
                "INSERT OR IGNORE INTO nodes VALUES (?, '', 'test', '{}', '{}', '', '')", (node_id,)
            )
        connection.execute(
            "INSERT OR IGNORE INTO events(event_id, protocol_version, type, companion_id, node_id, payload_json, occurred_at) VALUES (?, '1', 'test', ?, ?, '{}', '2020-01-01T00:00:00Z')",
            (event_id, companion, node_id),
        )


def write(service, command, content, companion="sui", **kwargs):
    source(
        service,
        kwargs.get("source_event_id", f"event-{command}"),
        companion,
        kwargs.get("source_node_id"),
    )
    return service.write(
        companion,
        {
            "command_id": command,
            "kind": "episode",
            "content": content,
            "source_event_id": f"event-{command}",
            **kwargs,
        },
    )


def reservation(task_id, input_tokens=20, output_tokens=10):
    return {
        "task_id": task_id,
        "purpose": "consolidation",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(MemoryError) as captured:
        function(*args, **kwargs)
    assert captured.value.code == code
    return captured.value


def test_restart_keeps_memory_provenance_and_original_idempotent_result(tmp_path):
    path = tmp_path / "memory.sqlite3"
    service = MemoryService(path)
    first = write(service, "first", "一起读夜航船。", kind="fact", source_node_id="desk")
    merged = write(
        service, "second", "一起读夜航船。", kind="fact", source_node_id="phone", importance=0.9
    )
    restarted = MemoryService(path)

    assert restarted.list("sui")["total"] == 1
    assert merged["memory_id"] == first["memory_id"]
    assert restarted.list("sui")["items"][0]["source_event_ids"] == ["event-first", "event-second"]
    # Replays return the result originally committed with the command, even if
    # another source has since strengthened that same memory.
    assert write(restarted, "first", "一起读夜航船。", kind="fact", source_node_id="desk") == first
    assert (
        restarted.recall("sui", {"query": "夜航船"})["items"][0]["memory_id"] == first["memory_id"]
    )


def test_normalized_duplicate_merges_evidence_but_keeps_distinct_kinds(tmp_path):
    service = MemoryService(tmp_path / "dedup.sqlite3")
    first = write(
        service, "a", "Loves  Ｔｅａ", kind="preference", tags=["Evening"], confidence=0.6
    )
    second = write(
        service,
        "b",
        "  loves tea  ",
        kind="preference",
        tags=["evening", "drinks"],
        importance=0.95,
        confidence=0.8,
        confirmed=True,
        pinned=True,
    )
    separate = write(service, "c", "loves tea", kind="fact")

    assert first["memory_id"] == second["memory_id"]
    assert separate["memory_id"] != second["memory_id"]
    assert second["tags"] == ["Evening", "drinks"]
    assert second["source_event_ids"] == ["event-a", "event-b"]
    assert second["importance"] == 0.95 and second["confidence"] == 0.8
    assert second["confirmed"] and second["pinned"]
    assert service.stats("sui")["total_records"] == 2


def test_semantic_correction_preserves_audit_and_excludes_old_fact(tmp_path):
    service = MemoryService(tmp_path / "correction.sqlite3")
    old = write(
        service, "old", "喜欢科幻小说", kind="preference", semantic_key="reading.genre", pinned=True
    )
    source(service, "event-current")
    current = service.correct(
        "sui",
        old["memory_id"],
        {
            "command_id": "current",
            "expected_revision": old["revision"],
            "content": "现在喜欢历史小说",
            "source_event_id": "event-current",
            "confirmed": True,
        },
    )

    records = {item["memory_id"]: item for item in service.list("sui")["items"]}
    assert not records[old["memory_id"]]["active"]
    assert records[current["memory_id"]]["active"]
    recall = service.recall("sui", {"query": "喜欢科幻小说"})
    assert old["memory_id"] not in {item["memory_id"] for item in recall["items"]}
    assert "喜欢科幻小说" not in recall["context_text"]

    # A later correction back to a former value creates a new active record,
    # preserving the prior superseded record's history.
    source(service, "event-restored")
    restored = service.correct(
        "sui",
        current["memory_id"],
        {
            "command_id": "restored",
            "expected_revision": current["revision"],
            "content": "喜欢科幻小说",
            "source_event_id": "event-restored",
        },
    )
    assert restored["memory_id"] != old["memory_id"]
    assert service.stats("sui")["active_records"] == 1
    assert service.stats("sui")["inactive_records"] == 2


def test_old_chinese_and_latin_records_survive_a_recency_shortlist(tmp_path):
    clock = Clock()
    policy = MemoryPolicy(lexical_candidate_limit=8, background_candidate_limit=4)
    service = MemoryService(tmp_path / "old.sqlite3", policy=policy, clock=clock)
    old_chinese = write(service, "old-cn", "那晚一起在海边读夜航船，聊起灯塔。", importance=0.1)
    old_latin = write(service, "old-en", "We read DUNE beside the quiet bay.", importance=0.1)
    stable = write(
        service, "stable", "用户希望安静陪伴。", kind="preference", pinned=True, importance=0.9
    )
    clock.value += timedelta(days=365 * 8)
    for index in range(48):
        write(service, f"recent-{index}", f"今天观察花盆，第 {index} 次。", importance=0.8)
        clock.value += timedelta(seconds=1)

    chinese = service.recall("sui", {"query": "夜航船 灯塔", "max_items": 1})
    latin = service.recall("sui", {"query": "dune", "max_items": 1})
    empty = service.recall("sui", {"query": "", "max_items": 1})
    assert chinese["items"][0]["memory_id"] == old_chinese["memory_id"]
    assert latin["items"][0]["memory_id"] == old_latin["memory_id"]
    assert empty["items"][0]["memory_id"] == stable["memory_id"]
    assert chinese["model_calls"] == latin["model_calls"] == 0
    assert service.stats("sui")["total_records"] == 51


def test_companion_isolation_delete_and_command_reuse_are_safe(tmp_path):
    service = MemoryService(tmp_path / "scope.sqlite3")
    injection = "'; DROP TABLE memory_records; --"
    original = write(service, injection, "安静读书", companion="one", kind="preference")
    other = write(service, "other", "热闹聊天", companion="two", kind="preference")
    recalled = service.recall("two", {"query": "读书"})["items"]
    assert [{key: item[key] for key in other} for item in recalled] == [other]
    assert all(0.5 <= item["decay_factor"] <= 1 for item in recalled)
    assert_error(
        "idempotency_conflict",
        write,
        service,
        injection,
        "安静读书",
        companion="two",
        kind="preference",
    )
    assert_error(
        "idempotency_conflict",
        write,
        service,
        injection,
        "换成另一个内容",
        companion="one",
        kind="preference",
    )
    assert_error("memory_not_found", service.delete, "two", original["memory_id"])
    assert_error("memory_not_found", service.delete, "one", injection)
    assert service.list("one")["items"] == [original]
    assert service.delete("one", original["memory_id"])["deleted"]
    assert service.recall("one", {"query": "读书"})["items"] == []
    assert service.stats("two")["total_records"] == 1


def test_budget_counts_formatted_labels_and_skips_oversized_records(tmp_path):
    estimator = CharacterEstimator()
    service = MemoryService(tmp_path / "budget.sqlite3", token_estimator=estimator)
    oversized = write(service, "long", "茶" * 200, importance=1.0, pinned=True)
    small = write(service, "short", "茶", importance=0.1)
    another = write(service, "another", "饭", importance=0.1)

    for budget in (0, 1, 16, 100, 250):
        recalled = service.recall("sui", {"query": "茶", "token_budget": budget, "max_items": 2})
        assert recalled["estimated_tokens"] == len(recalled["context_text"])
        assert recalled["estimated_tokens"] <= budget
        assert len(recalled["items"]) <= 2
        for item in recalled["items"]:
            assert item["memory_id"] in recalled["context_text"]
            assert item["kind"] in recalled["context_text"]
            assert item["content"] in recalled["context_text"]
        assert recalled["truncated"]
        assert recalled["model_calls"] == 0
    medium = service.recall("sui", {"query": "茶", "token_budget": 160, "max_items": 2})
    assert [{key: item[key] for key in small} for item in medium["items"]] == [small]
    assert oversized["memory_id"] not in medium["context_text"]
    assert another["memory_id"] not in medium["context_text"]
    one = service.recall("sui", {"query": "茶", "max_items": 1})
    assert len(one["items"]) == 1
    assert_error("recall_policy_limit", service.recall, "sui", {"token_budget": 4097})


def test_consolidation_plans_bounded_batch_without_mutating_sources(tmp_path):
    policy = MemoryPolicy(
        consolidation_threshold=3,
        consolidation_batch_size=2,
        consolidation_input_token_limit=300,
        consolidation_output_token_limit=20,
        daily_model_token_limit=500,
        per_task_model_token_limit=500,
    )
    service = MemoryService(tmp_path / "plan.sqlite3", policy=policy)
    write(service, "fact", "喜欢温茶", kind="preference", confirmed=True)
    episodes = [write(service, str(index), f"共同读书的第 {index} 个夜晚") for index in range(2)]
    assert not service.plan_consolidation("sui")["triggered"]
    episodes.append(write(service, "2", "共同读书的第三个夜晚"))
    before = service.list("sui")
    plan = service.plan_consolidation("sui")

    assert plan["triggered"]
    assert len(plan["source_memory_ids"]) == 2
    assert set(plan["source_memory_ids"]) <= {item["memory_id"] for item in episodes}
    assert 0 < plan["estimated_input_tokens"] <= policy.consolidation_input_token_limit
    assert plan["estimated_total_tokens"] == plan["estimated_input_tokens"] + 20
    assert plan["planned_model_calls"] == 1 and plan["model_calls"] == 0
    assert not plan["execution_enabled"] and not plan["derived_summary_persisted"]
    assert plan["preserve_source_records"] and plan["budget_available"]
    assert service.list("sui") == before
    service.reserve_model_tokens("another-companion", reservation("quota-held", 480, 0))
    blocked = service.plan_consolidation("sui")
    assert not blocked["budget_available"] and blocked["reason"] == "daily_token_limit"
    assert service.policy_snapshot()["money_pricing_configured"] is False


def test_quota_reservation_release_settlement_and_restart(tmp_path):
    clock = Clock()
    policy = MemoryPolicy(daily_model_token_limit=100, per_task_model_token_limit=100)
    path = tmp_path / "quota.sqlite3"
    service = MemoryService(path, policy=policy, clock=clock)
    first = service.reserve_model_tokens("sui", reservation("task"))
    assert first["new_reservation"] and first["token_quota"]["remaining_tokens"] == 70
    replay = service.reserve_model_tokens("sui", reservation("task"))
    assert not replay["new_reservation"]
    assert service.budget_snapshot("sui")["reserved_tokens"] == 30
    assert_error("idempotency_conflict", service.reserve_model_tokens, "other", reservation("task"))
    assert_error(
        "idempotency_conflict", service.reserve_model_tokens, "sui", reservation("task", 21, 10)
    )
    assert_error(
        "model_task_not_found",
        service.settle_model_tokens,
        "other",
        "task",
        {"input_tokens": 1, "output_tokens": 1},
    )

    restarted = MemoryService(path, policy=policy, clock=clock)
    result = restarted.settle_model_tokens("sui", "task", {"input_tokens": 15, "output_tokens": 5})
    assert result["status"] == "settled" and result["actual_tokens"] == 20
    assert result["token_quota"]["remaining_tokens"] == 80
    assert (
        restarted.settle_model_tokens("sui", "task", {"input_tokens": 15, "output_tokens": 5})
        == result
    )
    assert_error(
        "idempotency_conflict",
        restarted.settle_model_tokens,
        "sui",
        "task",
        {"input_tokens": 16, "output_tokens": 5},
    )
    restarted.reserve_model_tokens("sui", reservation("canceled"))
    released = restarted.release_model_tokens("sui", "canceled")
    assert released["status"] == "released" and released["token_quota"]["remaining_tokens"] == 80
    assert restarted.release_model_tokens("sui", "canceled") == released
    assert restarted.reserve_model_tokens("sui", reservation("canceled"))["status"] == "released"
    # Late billing must still be recorded if a provider ran after cancellation.
    late = restarted.settle_model_tokens("sui", "canceled", {"input_tokens": 5, "output_tokens": 1})
    assert late["settled_after_release"] and late["token_quota"]["spent_tokens"] == 26
    assert_error("model_task_already_settled", restarted.release_model_tokens, "sui", "task")
    clock.value += timedelta(days=1)
    assert restarted.budget_snapshot("sui")["remaining_tokens"] == 100
    assert restarted.reserve_model_tokens("sui", reservation("task"))["status"] == "settled"
    assert restarted.budget_snapshot("sui", "2020-01-01")["spent_tokens"] == 26


def test_actual_provider_overrun_is_recorded_and_blocks_new_jobs(tmp_path):
    policy = MemoryPolicy(daily_model_token_limit=100, per_task_model_token_limit=100)
    path = tmp_path / "overrun.sqlite3"
    service = MemoryService(path, policy=policy)
    service.reserve_model_tokens("first", reservation("overrun"))
    settled = service.settle_model_tokens(
        "first", "overrun", {"input_tokens": 80, "output_tokens": 40}
    )

    assert settled["actual_tokens"] == 120
    assert settled["overrun"] and settled["overrun_tokens"] == 90
    assert settled["over_budget"] and settled["token_quota"]["over_budget_tokens"] == 20
    assert service.budget_snapshot("second")["remaining_tokens"] == 0
    assert_error(
        "model_token_budget_exceeded",
        service.reserve_model_tokens,
        "second",
        reservation("next", 1, 0),
    )
    restarted = MemoryService(path, policy=policy)
    assert (
        restarted.settle_model_tokens("first", "overrun", {"input_tokens": 80, "output_tokens": 40})
        == settled
    )
    assert restarted.stats("first")["token_quota"]["spent_tokens"] == 120
    assert_error("model_task_already_settled", restarted.release_model_tokens, "first", "overrun")


def test_concurrent_reservations_share_global_quota_across_companions(tmp_path):
    policy = MemoryPolicy(daily_model_token_limit=100, per_task_model_token_limit=100)
    service = MemoryService(tmp_path / "concurrent.sqlite3", policy=policy)
    barrier = Barrier(8)

    def reserve(index):
        barrier.wait()
        try:
            return service.reserve_model_tokens(
                f"companion-{index % 2}", reservation(f"job-{index}", 30, 10)
            )
        except MemoryError as error:
            assert error.code == "model_token_budget_exceeded"
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(8)))
    successful = [result for result in results if result is not None]
    assert len(successful) == 2
    quota = service.budget_snapshot("companion-0")
    assert quota["reserved_tokens"] == 80 and quota["remaining_tokens"] == 20
    assert sum(item["reserved_tokens"] for item in quota["by_companion"]) == 80
    assert quota["scope"] == "runtime_global"
    # An idempotent retry from any successful task must not reserve again.
    for result in successful:
        retry = service.reserve_model_tokens(
            result["companion_id"], reservation(result["task_id"], 30, 10)
        )
        assert not retry["new_reservation"]
    assert service.budget_snapshot("companion-1")["reserved_tokens"] == 80


def test_quiet_behavior_requires_participation_or_explicit_user_request():
    policy = QuietParticipationPolicy()
    assert not policy.decide(ParticipationContext("quiet", False, True)).respond
    assert not policy.decide(ParticipationContext("focus", False, True)).respond
    assert not policy.decide(ParticipationContext("chat", True, False)).respond
    assert not policy.decide(ParticipationContext("away", True, True)).respond
    decision = policy.decide(ParticipationContext("quiet", True, True, True, True))
    assert decision.respond and decision.allow_audio
    assert policy.decide(ParticipationContext("chat", False, True)).respond


def test_erase_companion_clears_all_memory_artifacts_and_is_idempotent(tmp_path):
    service = MemoryService(tmp_path / "erase.sqlite3")
    write(service, "e1", "我的生日是11月3日", kind="fact", semantic_key="user.birthday")
    write(service, "e2", "喜欢喝茶", kind="preference", semantic_key="drink.preference")
    service.erase_companion("sui", "erase-1")
    assert service.list("sui")["total"] == 0
    assert service.stats("sui")["total_records"] == 0
    # Replay with the same command_id is idempotent and returns the same receipt.
    replay = service.erase_companion("sui", "erase-1")
    assert replay["erased"] is True
    # A fresh command_id on an already-cleared companion still succeeds.
    again = service.erase_companion("sui", "erase-2")
    assert again["erased"] is True
    # Another companion is untouched.
    write(service, "e3", "别人的记忆", companion="two", kind="preference")
    assert service.list("two")["total"] == 1


def test_erase_companion_rejects_conflicting_command_id(tmp_path):
    service = MemoryService(tmp_path / "erase-conflict.sqlite3")
    service.erase_companion("sui", "erase-x")
    # Same command_id for a different companion is an idempotency conflict,
    # because the fingerprint is bound to the erased companion.
    assert_error(
        "idempotency_conflict",
        service.erase_companion,
        "two",
        "erase-x",
    )
