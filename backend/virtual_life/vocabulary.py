"""Companion-level user vocabulary bank (MaiBot jargon_miner port, local only).

Counts the user's high-frequency Chinese word groups from ordinary dialogue
messages and exposes a small weighted sample for prompt injection. Pure
stdlib statistics: sentences are split on punctuation, then 2-gram/3-gram
character groups from CJK runs are counted (jieba is not a dependency).
No model calls, no embeddings, no network.

Storage: one SQLite table shared with the dialogue store.

    vocabulary(
        companion_id TEXT NOT NULL,
        word         TEXT NOT NULL,
        count        INTEGER NOT NULL,
        updated_at   TEXT NOT NULL,
        revision     INTEGER NOT NULL,
        PRIMARY KEY (companion_id, word)
    )

Every mutation happens inside the caller's single write transaction; the
per-word ``revision`` increments on each update so later re-mining stays
provable. Injection is prompt-level only: the sampled words appear as a
natural-language aside in personality_prompt, never as a mandated reply
vocabulary.
"""

import random
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime

# A word must reach this many occurrences across observed messages before it
# is eligible for injection (MaiBot jargon_miner threshold, lowered for a
# single-user companion).
DEFAULT_MIN_COUNT = 3
DEFAULT_MAX_WORDS = 3
# N-gram sizes for Chinese character groups (no jieba available).
_NGRAM_SIZES = (2, 3)
# Hard cap on stored words per companion; the lowest-count words are pruned.
_MAX_ENTRIES = 512
# N-grams containing any of these characters are dropped: they glue every
# sentence together and would otherwise dominate the counts.
_STOP_CHARS = frozenset(
    "的了着是在和与就不也都还很但这你们我他她它被把那有个上下里中去到说"
    "要会能可以因为所以但是而且如果虽然然后或者以及之类等啊呀吧呢吗嘛么哦嗯"
)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RUN = re.compile(r"[A-Za-z0-9]{2,12}")


def _frequent_groups(text: str) -> Counter:
    """Count 2-gram/3-gram CJK character groups plus ASCII tokens once each.

    A group containing a stop character is dropped. Runs shorter than the
    n-gram size contribute nothing. Returns a Counter of candidate words.
    """
    counts: Counter = Counter()
    for run in _CJK_RUN.findall(text or ""):
        for size in _NGRAM_SIZES:
            if len(run) < size:
                continue
            for start in range(len(run) - size + 1):
                group = run[start : start + size]
                if any(char in _STOP_CHARS for char in group):
                    continue
                counts[group] += 1
    for token in _ASCII_RUN.findall(text or ""):
        counts[token.lower()] += 1
    return counts


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS vocabulary (
            companion_id TEXT NOT NULL,
            word TEXT NOT NULL,
            count INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL,
            PRIMARY KEY (companion_id, word)
        )"""
    )


class VocabularyBank:
    def __init__(self, store, *, enabled: bool = True, min_count: int = DEFAULT_MIN_COUNT):
        self.store = store
        self.enabled = enabled
        self.min_count = min_count
        with store.transaction(write=True) as connection:
            _table(connection)

    def observe(self, connection: sqlite3.Connection, companion_id: str, text: str) -> int:
        """Merge one observed user message into the bank, inside the caller's
        write transaction. Returns the number of distinct words touched."""
        if not self.enabled or not text or not text.strip():
            return 0
        _table(connection)
        counts = _frequent_groups(text[:4096])
        if not counts:
            return 0
        now = _now_iso()
        rows = connection.execute(
            "SELECT word, count, revision FROM vocabulary WHERE companion_id = ?",
            (companion_id,),
        ).fetchall()
        current = {row["word"]: (row["count"], row["revision"]) for row in rows}
        touched = 0
        for word, addition in counts.items():
            if word in current:
                count, revision = current[word]
                connection.execute(
                    "UPDATE vocabulary SET count = ?, updated_at = ?, revision = ? "
                    "WHERE companion_id = ? AND word = ?",
                    (count + addition, now, revision + 1, companion_id, word),
                )
            else:
                connection.execute(
                    "INSERT INTO vocabulary VALUES (?, ?, ?, ?, ?)",
                    (companion_id, word, addition, now, 1),
                )
            touched += 1
        # Prune the lowest-count tail when the per-companion word set grows
        # past the cap; revisions of surviving words are untouched.
        entries = connection.execute(
            "SELECT word FROM vocabulary WHERE companion_id = ? ORDER BY count DESC",
            (companion_id,),
        ).fetchall()
        overflow = len(entries) - _MAX_ENTRIES
        if overflow > 0:
            for row in entries[_MAX_ENTRIES:]:
                connection.execute(
                    "DELETE FROM vocabulary WHERE companion_id = ? AND word = ?",
                    (companion_id, row["word"]),
                )
        return touched

    def eligible_words(self, companion_id: str, *, limit: int = 24) -> list[tuple[str, int]]:
        """Words that reached the frequency threshold, highest count first.

        Opens its own read transaction; safe to call from the prompt builder
        outside a write transaction.
        """
        if not self.enabled:
            return []
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT word, count FROM vocabulary WHERE companion_id = ? AND count >= ? "
                "ORDER BY count DESC, word LIMIT ?",
                (companion_id, self.min_count, limit),
            ).fetchall()
        return [(row["word"], row["count"]) for row in rows]


def sample_words(
    words: list[tuple[str, int]], seed: str, *, max_items: int = DEFAULT_MAX_WORDS
) -> list[str]:
    """Weighted sampling without replacement, stable for the same seed.

    Mirrors the habits sampling style: count-weighted (frequency), seeded per
    turn so the same prompt rebuild picks the same words while consecutive
    turns rotate the subset.
    """
    if not words:
        return []
    if len(words) <= max_items:
        return [word for word, _ in words]
    rng = random.Random(seed)
    population = list(words)
    picked: list[str] = []
    for _ in range(max_items):
        weights = [count for _, count in population]
        choice = rng.choices(population, weights=weights, k=1)[0]
        picked.append(choice[0])
        population.remove(choice)
    return picked


def vocabulary_aside(words: list[str]) -> str | None:
    """The natural-language prompt aside; None when there is nothing to say."""
    if not words:
        return None
    listed = "、".join(words)
    return (
        f"（你们聊天时常出现的词：{listed}。你偶尔可以自然地用上其中一两个，"
        "像平时说话那样顺口带出来，不必刻意，也不要整段罗列。）"
    )
