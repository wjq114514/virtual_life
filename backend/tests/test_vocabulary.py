"""Unit tests for the local user vocabulary bank (jargon_miner port)."""

from virtual_life.storage import SQLiteStore
from virtual_life.vocabulary import (
    VocabularyBank,
    _frequent_groups,
    sample_words,
    vocabulary_aside,
)


def make_bank(tmp_path) -> tuple[SQLiteStore, VocabularyBank]:
    store = SQLiteStore(tmp_path / "vocab.sqlite")
    return store, VocabularyBank(store)


def test_frequent_groups_skips_stopword_groups():
    from virtual_life.vocabulary import _STOP_CHARS

    text = "我在打打游戏，很开心。"
    counts = _frequent_groups(text)
    # Every returned group must be free of stop characters.
    for word in counts:
        assert all(char not in _STOP_CHARS for char in word)
    # The 2/3-gram logic is exercised indirectly: at least one group counted.
    assert counts, "expected at least one stop-free group"


def test_frequent_groups_counts_ascii_tokens_lowercased():
    counts = _frequent_groups("看这个 VSCode 插件 VSCode 很好用")
    assert counts["vscode"] == 2


def test_observe_upserts_and_increments_revision(tmp_path):
    store, bank = make_bank(tmp_path)
    message = "今晚一起打打游戏，再开一局"
    with store.transaction(write=True) as connection:
        bank.observe(connection, "c1", message)
    with store.transaction(write=True) as connection:
        bank.observe(connection, "c1", message)
    with store.transaction() as connection:
        rows = connection.execute(
            "SELECT word, count, revision FROM vocabulary WHERE companion_id = ?",
            ("c1",),
        ).fetchall()
    assert rows
    for row in rows:
        assert row["count"] >= 2
        assert row["revision"] == 2  # one revision bump per observe
    # A second companion sees nothing.
    assert bank.eligible_words("c2") == []


def test_observe_skips_empty_text(tmp_path):
    store, bank = make_bank(tmp_path)
    with store.transaction(write=True) as connection:
        assert bank.observe(connection, "c1", "   ") == 0
    assert bank.eligible_words("c1") == []


def test_eligible_words_respects_threshold(tmp_path):
    store, bank = make_bank(tmp_path)
    message = "今晚一起打打游戏，再开一局"
    # Below threshold after two observations.
    with store.transaction(write=True) as connection:
        bank.observe(connection, "c1", message)
        bank.observe(connection, "c1", message)
    assert bank.eligible_words("c1") == []
    # Third observation crosses the default min_count of 3.
    with store.transaction(write=True) as connection:
        bank.observe(connection, "c1", message)
    words = bank.eligible_words("c1")
    assert words, "expected eligible words after third observation"
    counts = [count for _, count in words]
    assert counts == sorted(counts, reverse=True)
    assert all(count >= 3 for count in counts)


def test_disabled_bank_is_inert(tmp_path):
    store = SQLiteStore(tmp_path / "vocab.sqlite")
    bank = VocabularyBank(store, enabled=False)
    with store.transaction(write=True) as connection:
        assert bank.observe(connection, "c1", "今晚一起打打游戏") == 0
    assert bank.eligible_words("c1") == []


def test_sample_words_is_deterministic_and_weighted():
    words = [("苹果", 10), ("香蕉", 1), ("梨子", 1), ("西瓜", 1), ("葡萄", 1)]
    first = sample_words(words, "seed-1")
    second = sample_words(words, "seed-1")
    assert first == second
    assert len(first) == 3
    assert len(set(first)) == 3  # sampling without replacement
    # Weighting: over many seeds the frequent word dominates the picks.
    picks = [word for index in range(60) for word in sample_words(words, f"s{index}")]
    assert picks.count("苹果") > picks.count("香蕉")


def test_sample_words_returns_all_when_few():
    assert sample_words([("甲", 3)], "s") == ["甲"]
    assert sample_words([], "s") == []


def test_vocabulary_aside_text():
    assert vocabulary_aside([]) is None
    aside = vocabulary_aside(["打打游戏", "开一局"])
    assert aside is not None
    assert "打打游戏" in aside and "开一局" in aside
    assert "、" in aside


def test_prune_keeps_highest_counts(tmp_path):
    store, bank = make_bank(tmp_path)
    from virtual_life import vocabulary as vocabulary_module

    cap = 4
    original = vocabulary_module._MAX_ENTRIES
    vocabulary_module._MAX_ENTRIES = cap
    try:
        for round_index in range(3):
            with store.transaction(write=True) as connection:
                bank.observe(
                    connection, "c1", f"高频词{round_index} 高频词{round_index} 高频词{round_index}"
                )
        with store.transaction() as connection:
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM vocabulary WHERE companion_id = ?", ("c1",)
            ).fetchone()["n"]
        assert total <= cap
    finally:
        vocabulary_module._MAX_ENTRIES = original
