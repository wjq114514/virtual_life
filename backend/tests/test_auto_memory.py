"""Evidence constrained automatic extraction with scripted, free model responses."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_memory import Clock
from virtual_life.auto_memory import AutoMemoryService, _birthday_evidence
from virtual_life.memory import MemoryService
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.storage import SQLiteStore


class Extractor:
    def __init__(self):
        self.output = {"memories": [], "summary": "", "relations": []}
        self.calls = 0
        self.action = lambda: None

    def complete(self, request):
        self.calls += 1
        self.action()
        return ModelResult(
            json.dumps(self.output, ensure_ascii=False), "scripted", ModelUsage(20, 10)
        )


def environment(tmp_path, batch_size=1):
    clock = Clock()
    memory = MemoryService(tmp_path / "automatic.sqlite", clock=clock)
    SQLiteStore(memory.database_path)
    provider = Extractor()
    auto = AutoMemoryService(
        memory, provider=provider, model_name="scripted", batch_size=batch_size
    )
    memory.automatic_memory = auto
    return memory, auto, provider, clock


def enqueue(memory, auto, clock, event_id, text, assistant="明白了"):
    event = {
        "event_id": event_id,
        "type": "companion.dialogue_completed",
        "companion_id": "sui",
        "occurred_at": memory._timestamp(clock.value),
        "payload": {"user_message": text, "assistant_message": assistant},
    }
    with memory._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO events(event_id,protocol_version,type,companion_id,payload_json,occurred_at) VALUES(?,'1',?,?,?,?)",
            (event_id, event["type"], "sui", json.dumps(event["payload"]), event["occurred_at"]),
        )
        auto.enqueue(connection, event)
    return event


def fact(event_id, text, key="preferred_drink", **kwargs):
    return {
        "kind": "fact",
        "content": text,
        "semantic_key": key,
        "evidence": text,
        "source_event_id": event_id,
        "confidence": 0.8,
        "importance": 0.5,
        **kwargs,
    }


def extract(provider, *items, summary="", relations=None):
    provider.output = {"memories": list(items), "summary": summary, "relations": relations or []}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我的生日是6月18日", True),
        ("妹妹生日十一月三日", True),
        ("My birthday is June 18th", True),
        ("born 2000-06-18", True),
        ("6月18日去公园", False),
        ("生日还没有告诉你", False),
        ("我喜欢乌龙茶", False),
    ],
)
def test_birthdate_requires_marker_and_date(text, expected):
    assert _birthday_evidence(text) is expected


@pytest.mark.parametrize(
    "bad",
    ["assistant_quote", "foreign_event", "invented_quote", "conflicting_slot", "relation_index"],
)
def test_invalid_model_evidence_and_conflicts_are_atomic(tmp_path, bad):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "e1", "我喜欢乌龙茶", assistant="你的生日是6月18日")
    item = fact("e1", "我喜欢乌龙茶")
    items = [item]
    relations = []
    if bad == "assistant_quote":
        item.update(content="你的生日是6月18日", evidence="你的生日是6月18日")
    elif bad == "foreign_event":
        item["source_event_id"] = "other-event"
    elif bad == "invented_quote":
        item["evidence"] = "我喜欢咖啡"
    elif bad == "conflicting_slot":
        items.append({**item, "content": "我喜欢薄荷茶"})
    else:
        relations = [
            {"subject": "林舟", "predicate": "喜欢", "object": "乌龙茶", "memory_index": 1}
        ]
    extract(provider, *items, summary="不得提交的摘要", relations=relations)
    result = auto.process_next("sui", force=True)
    assert result["status"] == "failed" and result["error_code"] == "invalid_extraction"
    assert all(r["kind"] == "episode" for r in memory.list("sui")["items"])
    assert auto.summaries("sui")["items"] == []
    assert memory.budget_snapshot("sui")["spent_tokens"] == 30


def test_birthday_guard_and_subject_qualified_permanence(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    text = "林舟生日6月18日，妹妹小禾生日11月3日。我喜欢乌龙茶"
    enqueue(memory, auto, clock, "e1", text)
    extract(
        provider,
        fact("e1", "林舟生日6月18日", "user.birthday"),
        fact("e1", "妹妹小禾生日11月3日", "sister.birthday"),
        fact("e1", "我喜欢乌龙茶", "tea.birthday"),
    )
    assert auto.process_next("sui", force=True)["status"] == "completed"
    records = {r["semantic_key"]: r for r in memory.list("sui")["items"] if r["kind"] == "fact"}
    assert records["auto:tea.birthday"]["retention_policy"] == "stable"
    for slot in ("auto:user.birthday", "auto:sister.birthday"):
        recalled = memory.recall("sui", {"semantic_key": slot})["items"][0]
        assert recalled["effective_retention_policy"] == "permanent"
    assert len(records) == 3


def test_newer_evidence_corrects_but_older_evidence_needs_review(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "old", "我喜欢乌龙茶")
    extract(provider, fact("old", "我喜欢乌龙茶"))
    assert auto.process_next("sui", force=True)["status"] == "completed"
    clock.value += timedelta(days=2)
    enqueue(memory, auto, clock, "new", "我改喝无咖啡因薄荷茶")
    extract(provider, fact("new", "我改喝无咖啡因薄荷茶"))
    assert auto.process_next("sui", force=True)["status"] == "completed"
    current = memory.recall("sui", {"semantic_key": "auto:preferred_drink"})["items"][0]
    assert current["content"] == "我改喝无咖啡因薄荷茶" and current["supersedes_id"]
    clock.value -= timedelta(days=1)
    enqueue(memory, auto, clock, "stale", "我喜欢乌龙茶")
    clock.value += timedelta(days=2)
    extract(provider, fact("stale", "我喜欢乌龙茶"), summary="过期事实摘要")
    result = auto.process_next("sui", force=True)
    assert result["status"] == "needs_review"
    assert result["summary_skipped_reason"] == "unresolved_conflict"
    assert (
        memory.recall("sui", {"semantic_key": "auto:preferred_drink"})["items"][0]["memory_id"]
        == current["memory_id"]
    )


def test_duplicate_evidence_derivative_invalidation_and_no_replay(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    event = enqueue(memory, auto, clock, "e1", "顾言是我的室友")
    extract(
        provider,
        fact("e1", "顾言是我的室友", "roommate", kind="relationship"),
        summary="用户说顾言是室友。",
        relations=[{"subject": "用户", "predicate": "室友", "object": "顾言", "memory_index": 0}],
    )
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    assert len(auto.summaries("sui")["items"]) == 1
    with memory._connection(write=True) as connection:
        auto.enqueue(connection, event)
    assert auto.process_next("sui", force=True) is None and provider.calls == 1
    source_id = result["source_memory_versions"][0]["memory_id"]
    memory.delete("sui", source_id)
    assert auto.summaries("sui")["items"] == []
    assert memory.recall("sui", {"semantic_key": "auto:roommate"})["items"] == []


def test_source_deleted_during_model_call_discards_output_but_bills(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "e1", "我喜欢乌龙茶")
    extract(provider, fact("e1", "我喜欢乌龙茶"), summary="茶偏好")

    def delete_source():
        record = next(r for r in memory.list("sui")["items"] if r["kind"] == "episode")
        memory.delete("sui", record["memory_id"])

    provider.action = delete_source
    result = auto.process_next("sui", force=True)
    assert result["status"] == "discarded"
    assert memory.recall("sui", {"semantic_key": "auto:preferred_drink"})["items"] == []
    assert memory.budget_snapshot("sui")["spent_tokens"] == 30


def test_duplicate_sources_merge_without_creating_duplicate_fact(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    for i in range(2):
        clock.value += timedelta(days=1)
        enqueue(memory, auto, clock, f"e{i}", "我不喜欢咖啡")
        extract(provider, fact(f"e{i}", "我不喜欢咖啡", "disliked_drink", kind="preference"))
        assert auto.process_next("sui", force=True)["status"] == "completed"
    facts = [r for r in memory.list("sui")["items"] if r["kind"] == "preference"]
    assert len(facts) == 1 and facts[0]["content"] == "我不喜欢咖啡"
    assert facts[0]["source_event_ids"] == ["e0", "e1"]
    assert facts[0]["revision"] == 2


def edge(object_text, subject="用户", predicate="prefers_drink"):
    return {"subject": subject, "predicate": predicate, "object": object_text, "memory_index": 0}


def test_correction_replaces_edge_without_replay_or_extra_calls(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "old", "我喜欢乌龙茶")
    extract(provider, fact("old", "我喜欢乌龙茶"), relations=[edge("乌龙茶")])
    assert auto.process_next("sui", force=True)["status"] == "completed"
    clock.value += timedelta(days=1)
    text = "更正一下：我现在改喝无咖啡因薄荷茶，不再喝乌龙茶。"
    event = enqueue(memory, auto, clock, "new", text)
    extract(
        provider,
        fact("new", "用户现在喝无咖啡因薄荷茶", evidence=text),
        relations=[edge("无咖啡因薄荷茶"), edge("乌龙茶")],
    )
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    assert result["prompt_version"] == "auto-memory-v2"
    assert result["relations_skipped"] == [{"relation_index": 1, "reason": "unsupported_endpoint"}]
    graph = auto.graph("sui")
    assert [r["object"] for r in graph["relations"]] == ["无咖啡因薄荷茶"]
    assert auto.graph("other")["relations"] == []
    with memory._connection(write=True) as connection:
        auto.enqueue(connection, event)
    assert auto.process_next("sui", force=True) is None
    assert provider.calls == 2
    memory.delete("sui", result["source_memory_versions"][0]["memory_id"])
    assert auto.graph("sui")["relations"] == []


@pytest.mark.parametrize(
    "text,relation,reason",
    [
        ("我喜欢乌龙茶", edge("乌龙茶", subject="林舟"), "unsupported_endpoint"),
        ("我喜欢乌龙茶", edge("咖啡"), "unsupported_endpoint"),
        ("小禾生日11月3日", edge("11月3日", predicate="has_birthday"), "unsupported_user_subject"),
        (
            "我妹妹小禾生日11月3日",
            edge("11月3日", predicate="has_birthday"),
            "unsupported_birthday_owner",
        ),
        ("我不再喝乌龙茶", edge("乌龙茶"), "negated_object"),
    ],
)
def test_unsupported_edges_skip_without_losing_stable_memory(tmp_path, text, relation, reason):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "e1", text, assistant="林舟喜欢咖啡")
    extract(provider, fact("e1", text), relations=[relation])
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    assert result["relations_skipped"] == [{"relation_index": 0, "reason": reason}]
    assert auto.graph("sui")["relations"] == []
    assert memory.recall("sui", {"semantic_key": "auto:preferred_drink"})["items"]
    assert provider.calls == 1


def test_named_birthday_and_historical_prompt_version(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "e1", "我妹妹小禾生日11月3日")
    extract(
        provider,
        fact("e1", "小禾生日11月3日", "xiaohe.birthday"),
        relations=[edge("11月3日", subject="小禾", predicate="has_birthday")],
    )
    result = auto.process_next("sui", force=True)
    assert [r["subject"] for r in auto.graph("sui")["relations"]] == ["小禾"]
    with memory._connection(write=True) as connection:
        saved = {**result, "prompt_version": "auto-memory-v1"}
        connection.execute(
            "UPDATE auto_memory_jobs SET result_json=? WHERE job_id=?",
            (json.dumps(saved), result["job_id"]),
        )
        assert auto._job_result(connection, result["job_id"])["prompt_version"] == "auto-memory-v1"


@pytest.mark.parametrize("object_text,allowed", [("乌龙茶", True), ("咖啡", False)])
def test_contrast_clause_keeps_positive_current_object(tmp_path, object_text, allowed):
    memory, auto, provider, clock = environment(tmp_path)
    text = "我不喝咖啡但喜欢乌龙茶"
    enqueue(memory, auto, clock, "e1", text)
    extract(provider, fact("e1", text), relations=[edge(object_text)])
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    assert bool(auto.graph("sui")["relations"]) is allowed
    if not allowed:
        assert result["relations_skipped"][0]["reason"] == "negated_object"


def test_birthday_owner_must_support_this_object_clause(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    text = "我的生日6月18日。小禾生日11月3日"
    enqueue(memory, auto, clock, "e1", text)
    extract(
        provider,
        fact("e1", "用户生日11月3日", "user.birthday", evidence=text),
        relations=[edge("11月3日", predicate="has_birthday")],
    )
    result = auto.process_next("sui", force=True)
    assert result["relations_skipped"][0]["reason"] == "unsupported_birthday_owner"
    assert auto.graph("sui")["relations"] == []


@pytest.mark.parametrize(
    "text,object_text,allowed",
    [
        ("My birthday is Nov. 3.", "Nov. 3", True),
        ("My birthday is 2000.06.18.", "2000.06.18", True),
        ("My birthday is June 18. Xiaohe's birthday is Nov. 3.", "Nov. 3", False),
        ("我的公历生日是6月18日", "6月18日", True),
        ("我的农历生日是五月初五", "五月初五", True),
        ("我的阳历生日是6月18日", "6月18日", True),
        ("我的阴历生日是五月初五", "五月初五", True),
    ],
)
def test_birthday_date_dots_preserve_owner_clause(tmp_path, text, object_text, allowed):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "e1", text)
    extract(
        provider,
        fact("e1", f"用户生日 {object_text}", "user.birthday", evidence=text),
        relations=[edge(object_text, predicate="has_birthday")],
    )
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    assert bool(auto.graph("sui")["relations"]) is allowed
    if allowed:
        assert result.get("relations_skipped", []) == []
    else:
        assert result["relations_skipped"][0]["reason"] == "unsupported_birthday_owner"


def test_first_person_exception_does_not_ground_user_object(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "e1", "我喜欢乌龙茶")
    extract(
        provider, fact("e1", "用户喜欢乌龙茶", evidence="我喜欢乌龙茶"), relations=[edge("用户")]
    )
    result = auto.process_next("sui", force=True)
    assert result["relations_skipped"][0]["reason"] == "unsupported_endpoint"
    assert auto.graph("sui")["relations"] == []


def test_birthday_reminder_scheduled_within_window(tmp_path):
    clock = Clock()
    clock.value = datetime(2026, 10, 28, tzinfo=UTC)
    memory = MemoryService(tmp_path / "birthday.sqlite", clock=clock)
    SQLiteStore(memory.database_path)
    provider = Extractor()
    reminders = []
    auto = AutoMemoryService(
        memory,
        provider=provider,
        model_name="scripted",
        batch_size=1,
        birthday_reminder=lambda companion_id, command_id, title, details, delay: reminders.append(
            (companion_id, command_id, title, details, delay)
        ),
    )
    memory.automatic_memory = auto
    enqueue(memory, auto, clock, "b1", "我的生日是11月3日")
    extract(provider, fact("b1", "我的生日是11月3日", "user.birthday"))
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    assert len(reminders) == 1
    companion_id, command_id, title, details, delay = reminders[0]
    assert companion_id == "sui"
    assert command_id.startswith("bday-")
    assert title == "11月3日生日提醒"
    assert "11" in details and "3" in details
    assert 60 <= delay <= 2_592_000
    assert delay > 0


def test_birthday_reminder_skipped_outside_window(tmp_path):
    clock = Clock()
    clock.value = datetime(2026, 10, 28, tzinfo=UTC)
    memory = MemoryService(tmp_path / "birthday-far.sqlite", clock=clock)
    SQLiteStore(memory.database_path)
    provider = Extractor()
    reminders = []
    auto = AutoMemoryService(
        memory,
        provider=provider,
        model_name="scripted",
        batch_size=1,
        birthday_reminder=lambda companion_id, command_id, title, details, delay: reminders.append(
            delay
        ),
    )
    memory.automatic_memory = auto
    enqueue(memory, auto, clock, "b2", "我的生日是6月18日")
    extract(provider, fact("b2", "我的生日是6月18日", "user.birthday"))
    auto.process_next("sui", force=True)
    assert reminders == []


def test_birthday_reminder_no_callback_is_noop(tmp_path):
    memory, auto, provider, clock = environment(tmp_path)
    enqueue(memory, auto, clock, "b3", "我的生日是11月3日")
    extract(provider, fact("b3", "我的生日是11月3日", "user.birthday"))
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"


def test_birthday_reminder_uses_exact_seconds(tmp_path):
    from virtual_life.auto_memory import _seconds_until_birthday, parse_birthday_month_day

    parsed = parse_birthday_month_day("我的生日是11月3日")
    assert parsed == (11, 3)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    seconds = _seconds_until_birthday(11, 3, now)
    # 11月3日 00:00 UTC minus 10月28日 12:00 UTC = 5.5 days
    assert seconds == int(5.5 * 86400)


def test_stale_summary_rebuild_re_enqueues_live_sources(tmp_path):
    memory, auto, provider, clock = environment(tmp_path, batch_size=2)
    # Two sources processed in one batch produce one summary depending on both.
    enqueue(memory, auto, clock, "e1", "顾言是我的室友")
    enqueue(memory, auto, clock, "e2", "我喜欢乌龙茶", assistant="记住了")
    extract(
        provider,
        fact("e1", "顾言是我的室友", "roommate", kind="relationship"),
        fact("e2", "我喜欢乌龙茶", "preferred_drink"),
        summary="用户有室友，且喜欢乌龙茶。",
        relations=[{"subject": "用户", "predicate": "室友", "object": "顾言", "memory_index": 0}],
    )
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    summary = auto.summaries("sui")["items"]
    assert len(summary) == 1
    # The summary depends on both source episodes (and the resolved facts).
    assert set(summary[0]["source_event_ids"]) == {"e1", "e2"}
    assert {v["memory_id"] for v in summary[0]["memory_versions"]} == {
        v["memory_id"] for v in result["source_memory_versions"]
    } | {v["memory_id"] for v in result["memory_versions"]}
    # Delete one source episode: the summary that depended on it becomes stale.
    episodes = [r for r in memory.list("sui")["items"] if r["kind"] == "episode"]
    deleted_id = next(r["memory_id"] for r in episodes if "乌龙茶" in r["content"])
    memory.delete("sui", deleted_id)
    assert auto.summaries("sui")["items"] == []
    # Rebuild: the stale summary's surviving source is re-enqueued.
    pruned = auto.prune_derivatives("sui", rebuild=True)
    assert pruned == 1
    with memory._connection() as connection:
        pending = connection.execute(
            "SELECT COUNT(*) FROM auto_memory_queue WHERE companion_id='sui' AND status='pending'"
        ).fetchone()[0]
    assert pending == 1
    # The worker regenerates a fresh summary from the surviving source.
    extract(
        provider,
        fact("e1", "顾言是我的室友", "roommate", kind="relationship"),
        summary="用户说顾言是室友。",
    )
    result = auto.process_next("sui", force=True)
    assert result["status"] == "completed"
    assert len(auto.summaries("sui")["items"]) == 1
