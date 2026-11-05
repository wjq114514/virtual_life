"""Unit tests for the conservative typo generator."""

from virtual_life.typo import (
    TYPO_MOODS,
    apply_typo,
    maybe_typo,
    should_typo,
    typo_candidates,
)

# Long-ish texts whose candidates are unambiguous.
LONG_TEXT = "这句话长得足够，而且还有几个常见的同音字可以替换。"


def test_maybe_typo_is_deterministic():
    """Same (text, error_rate, seed) must always produce the same output."""
    for seed in ("a", "b", "companion:cmd-1", ""):
        first = maybe_typo(LONG_TEXT, error_rate=1.0, seed=seed)
        second = maybe_typo(LONG_TEXT, error_rate=1.0, seed=seed)
        assert first == second


def test_maybe_typo_changes_at_most_one_character():
    """At most one substitution: length and all other positions unchanged."""
    for index in range(20):
        result = maybe_typo(LONG_TEXT, error_rate=1.0, seed=f"seed-{index}")
        if result == LONG_TEXT:
            continue
        assert len(result) == len(LONG_TEXT)
        diffs = [
            (position, LONG_TEXT[position], result[position])
            for position in range(len(LONG_TEXT))
            if LONG_TEXT[position] != result[position]
        ]
        assert len(diffs) == 1
        _, original, replacement = diffs[0]
        # The swapped pair must actually be a curated homophone pair.
        assert original != replacement


def test_maybe_typo_returns_text_unchanged_when_roll_fails():
    """error_rate=0 must never rewrite the text."""
    assert maybe_typo(LONG_TEXT, error_rate=0.0, seed="x") == LONG_TEXT


def test_maybe_typo_skips_short_texts():
    """Short replies have no room for a believable slip."""
    assert maybe_typo("短的", error_rate=1.0, seed="x") == "短的"
    assert maybe_typo("", error_rate=1.0, seed="x") == ""


def test_maybe_typo_without_candidates_is_unchanged():
    """Text made only of characters without partners is never touched."""
    plain = "改日未时三点整欢迎光临寒舍一叙呀"
    assert typo_candidates(plain) == []
    assert maybe_typo(plain, error_rate=1.0, seed="x") == plain


def test_should_typo_only_for_high_arousal_moods():
    assert should_typo("cheerful")
    assert should_typo("amused")
    assert should_typo("excited")
    for mood in ("calm", "tired", "concerned", "sympathetic", "", "unknown"):
        assert not should_typo(mood)
    assert TYPO_MOODS == frozenset({"cheerful", "amused", "excited"})


def test_apply_typo_respects_enabled_and_mood_gates():
    text = LONG_TEXT
    # Disabled flag: untouched even in an eligible mood.
    assert apply_typo(text, mood="cheerful", enabled=False, error_rate=1.0, seed="s") == text
    # Low-arousal mood: untouched even when enabled.
    for mood in ("calm", "tired", "concerned", ""):
        assert apply_typo(text, mood=mood, enabled=True, error_rate=1.0, seed="s") == text


def test_typo_frequency_is_bounded_by_error_rate():
    """Empirical fire rate must stay close to the configured rate."""
    trials = 2000
    rate = 0.03
    changed = sum(
        1
        for index in range(trials)
        if maybe_typo(LONG_TEXT, error_rate=rate, seed=f"f-{index}") != LONG_TEXT
    )
    # 2000 trials at 3%: expected 60, generous ±5 sigma band (sigma ~ 7.6).
    assert 20 <= changed <= 110, f"typo fired {changed}/{trials} times at rate {rate}"


def test_apply_typo_is_deterministic_per_seed():
    first = apply_typo(LONG_TEXT, mood="amused", enabled=True, error_rate=1.0, seed="c:cmd")
    second = apply_typo(LONG_TEXT, mood="amused", enabled=True, error_rate=1.0, seed="c:cmd")
    assert first == second
    assert first != LONG_TEXT
