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
