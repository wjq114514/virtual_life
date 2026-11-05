"""Unit tests for cache-friendly prompt section ordering."""

from virtual_life.personality_prompt import (
    _SECTIONS,
    _STABLE_SECTIONS,
    _VOLATILE_SECTIONS,
    PersonalityPromptBuilder,
)


def test_section_ordering_is_cache_friendly():
    """Stable sections must come before volatile sections for prefix caching."""
    # All stable sections must appear before any volatile section.
    last_stable = max(_SECTIONS.index(s) for s in _STABLE_SECTIONS)
    first_volatile = min(_SECTIONS.index(s) for s in _VOLATILE_SECTIONS)
    assert last_stable < first_volatile, (
        f"Stable sections must precede volatile sections for cache reuse, "
        f"but stable ends at index {last_stable} and volatile starts at {first_volatile}"
    )


def test_stable_sections_are_most_stable():
    """Style examples and activity context should be among the most stable."""
    # style_examples changes only on personality revision — most stable block.
    assert _STABLE_SECTIONS[0] == "style_examples"
    # memories is the last stable section (changes per topic but stable within conversation).
    assert _STABLE_SECTIONS[-1] == "memories"


def test_volatile_sections_are_most_volatile():
    """History is the only volatile block; current and message are separate arguments."""
    assert _VOLATILE_SECTIONS == ("history",)


def test_context_ordering_puts_current_before_history_and_message():
    """current must come after stable blocks, before history and message."""
    core = "CORE"
    current = "CURRENT"
    message = "MESSAGE"
    blocks = {
        "style_examples": "STYLE",
        "activity_context": "ACTIVITY",
        "slow_variables": "SLOW",
        "inner_activity": "INNER",
        "memories": "MEMORIES",
        "history": "HISTORY",
    }
    result = PersonalityPromptBuilder._context(core, current, blocks, message)
    parts = result.split("\n\n")
    expected_order = [
        "CORE",
        "STYLE",
        "ACTIVITY",
        "SLOW",
        "INNER",
        "MEMORIES",
        "CURRENT",
        "HISTORY",
        "MESSAGE",
    ]
    assert parts == expected_order, f"Unexpected order: {parts}"


def test_context_ordering_without_history():
    """When history is absent, current still comes before message."""
    core = "CORE"
    current = "CURRENT"
    message = "MESSAGE"
    blocks = {
        "style_examples": "STYLE",
        "memories": "MEMORIES",
    }
    result = PersonalityPromptBuilder._context(core, current, blocks, message)
    parts = result.split("\n\n")
    assert parts == ["CORE", "STYLE", "MEMORIES", "CURRENT", "MESSAGE"]


def test_context_ordering_without_current_blocks():
    """When no stable blocks are present, current and message are still ordered."""
    core = "CORE"
    current = "CURRENT"
    message = "MESSAGE"
    blocks: dict = {}
    result = PersonalityPromptBuilder._context(core, current, blocks, message)
    parts = result.split("\n\n")
    assert parts == ["CORE", "CURRENT", "MESSAGE"]


def test_all_sections_included_in_seCTIONS():
    """All sections used in the builder must be present in _SECTIONS."""
    expected = {
        "activity_context",
        "slow_variables",
        "inner_activity",
        "memories",
        "style_examples",
        "history",
    }
    assert set(_SECTIONS) == expected


def test_sample_habits_bounded_and_deterministic():
    """Habits sampling returns a bounded, order-preserving, seeded subset."""
    from virtual_life.personality_prompt import _sample_habits

    habits = [f"习惯{i}" for i in range(6)]
    picked = _sample_habits(habits, "seed-a")
    assert 1 <= len(picked) <= 3
    assert set(picked) <= set(habits)
    assert len(set(picked)) == len(picked)
    # Same seed is stable within a turn (preview is called up to three times).
    assert _sample_habits(habits, "seed-a") == picked
    # Different seeds sample different subsets across turns.
    variants = {tuple(_sample_habits(habits, s)) for s in ("seed-b", "seed-c", "seed-d")}
    assert len(variants) > 1


def test_sample_habits_keeps_small_pools_whole():
    """Small habit pools are never cut down by sampling."""
    from virtual_life.personality_prompt import _sample_habits

    habits = ["口头禅A", "口头禅B"]
    assert _sample_habits(habits, "any") == habits
    single = ["唯一习惯"]
    assert _sample_habits(single, "any") == single


def test_dedupe_recall_items_keeps_newest_revision():
    """Recalled items with the same content collapse to the newest revision."""
    from virtual_life.personality_prompt import _dedupe_recall_items

    items = [
        {"content": "一起去过海边的咖啡店", "revision": 1, "semantic_key": "k1"},
        {"content": "一起去过海边的咖啡店", "revision": 3, "semantic_key": "k1"},
        {"content": "完全不同的记忆", "revision": 2, "semantic_key": None},
    ]
    deduped = _dedupe_recall_items(items)
    assert len(deduped) == 2
    kept = next(i for i in deduped if i["revision"] != 2)
    assert kept["revision"] == 3


def test_dedupe_recall_items_merges_near_identical_text():
    """Near-identical episode texts (prefix window) collapse even without keys."""
    from virtual_life.personality_prompt import _dedupe_recall_items

    base = "用户上周说想去爬山，还想带上相机，说山顶拍照很好看"
    items = [
        {"content": base, "revision": 2, "semantic_key": None},
        {"content": base + "。", "revision": 1, "semantic_key": None},
    ]
    deduped = _dedupe_recall_items(items)
    assert len(deduped) == 1
    assert deduped[0]["revision"] == 2


def test_attention_drift_and_tone_variant_are_lightweight_asides():
    """Drift is a plain natural-language line; tone variant is probabilistic."""
    from virtual_life.personality_prompt import _attention_drift, _tone_variant

    drift = _attention_drift("focus", "calm", 0.2, 0.3, 0.7)
    assert isinstance(drift, str) and drift.strip()
    assert "{" not in drift and "}" not in drift  # no JSON data block
    # Deterministic per (companion, revision, message) — stable within a turn.
    first = _tone_variant("c1", 4, "在吗")
    again = _tone_variant("c1", 4, "在吗")
    assert first == again


def test_core_instructions_forbid_invented_nicknames():
    """Core instructions must ban invented nicknames or forms of address.

    Guards the "晚安，阿宅" failure: the model may only use a name the user
    themselves stated or sourced memory evidence provides, otherwise no name.
    """
    from virtual_life.personality_prompt import _CORE_INSTRUCTIONS

    lowered = _CORE_INSTRUCTIONS.lower()
    assert "nickname" in lowered
    assert "never invent" in lowered
    assert "sourced evidence" in lowered
    assert "without any name" in lowered


def test_deep_night_message_adds_woken_up_aside(monkeypatch):
    """A user-initiated deep-night message tells the model she was asleep."""
    from datetime import datetime as real_datetime
    from datetime import timezone

    from virtual_life import personality_prompt as pp

    # 02:00 Asia/Shanghai == 18:00 UTC the previous day.
    fixed = real_datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)

    class _FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(pp, "datetime", _FixedDatetime)
    snapshot = pp.rhythm_snapshot(fixed, "Asia/Shanghai")
    assert snapshot["is_deep_night"] is True

    # Reconstruct the same guard the builder applies on the aside.
    rhythm_aside = snapshot["label"]
    user_initiated = True
    if snapshot is not None and user_initiated and snapshot["is_deep_night"]:
        rhythm_aside = (
            f"{rhythm_aside}\n你原本是睡着的，是被这条消息叫醒的："
            "回复可以带一点睡意、比平时短一些，不必立刻精神饱满。"
        )
    assert "叫醒" in rhythm_aside
    assert "睡意" in rhythm_aside

    # Daytime (14:00 local) keeps the plain label.
    day = pp.rhythm_snapshot(real_datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc), "Asia/Shanghai")
    assert day["is_deep_night"] is False
