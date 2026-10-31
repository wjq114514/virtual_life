"""Split a long assistant reply into short netizen-style messages.

Modeled on MaiBot's ``[response_splitter]`` (``split_into_sentences_w_remove_punctuation``
+ ``_merge_processed_segments_to_max_count``) but tuned for Virtual Life: the
splitter never edits the reply text, only decides where to break it into
separate bubbles, so ``assistant_message`` stays the canonical full text and the
split list is an additive view for IM clients that want to send several short
messages instead of one wall of text.

The split points are sentence boundaries first (。！？!?；; newline), then clause
boundaries (，,、) as a fallback so a single long sentence is still broken. At
most ``max_messages`` bubbles are produced; any overflow is merged into the last
bubble so no content is dropped.
"""

from __future__ import annotations

import re

# Sentence terminators: CJK full-width, ASCII, and semicolons; newlines are
# always a hard boundary.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
# Clause fallback: CJK comma, ASCII comma, and the Chinese enumeration comma.
_CLAUSE_SPLIT = re.compile(r"(?<=[，,、])")


def split_reply(
    text: str,
    *,
    max_messages: int = 3,
    max_chars: int = 120,
) -> list[str]:
    """Split ``text`` into at most ``max_messages`` non-empty bubbles.

    Short replies (a single sentence under ``max_chars``) return ``[text]`` so a
    normal reply still arrives as one message. Empty or whitespace-only input
    returns ``[]``. ``max_messages`` is clamped to at least 1.
    """
    if not text or not text.strip():
        return []
    if max_messages < 1:
        max_messages = 1

    text = text.strip()
    # Single short message: no splitting needed.
    if len(text) <= max_chars:
        return [text]

    # Sentence-first split, then clause fallback for any segment still too long.
    parts = _split_preserving(text, _SENTENCE_SPLIT)
    if len(parts) == 1:
        parts = _split_preserving(text, _CLAUSE_SPLIT)

    bubbles = _pack(parts, max_chars, max_messages)
    if len(bubbles) == 1 and len(bubbles[0]) > max_chars * max_messages:
        # Even after clause splitting the content is one giant run with no
        # breakable boundary; fall back to a hard char chunk so the reply is
        # never dropped, merging any overflow into the final bubble.
        bubbles = _hard_chunk(text, max_chars, max_messages)
    return bubbles


def _split_preserving(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Split on ``pattern`` but keep the separator attached to the prior part.

    ``re.split`` with a lookbehind-based pattern already keeps the terminator
    in the preceding chunk, so the pieces retain their punctuation and read
    naturally when re-joined.
    """
    parts = [part for part in pattern.split(text) if part]
    return parts or [text]


def _pack(parts: list[str], max_chars: int, max_messages: int) -> list[str]:
    """Greedily pack ``parts`` into at most ``max_messages`` bubbles.

    Each bubble is kept under ``max_chars`` where possible; when a single part
    is longer than ``max_chars`` it is placed alone (later merged by the caller
    if the count exceeds the cap).
    """
    bubbles: list[str] = []
    current = ""
    for part in parts:
        candidate = current + part
        if current and len(candidate) > max_chars:
            bubbles.append(current)
            current = part
        else:
            current = candidate
    if current:
        bubbles.append(current)

    if len(bubbles) <= max_messages:
        return bubbles
    # Overflow: merge all extras into the last bubble (keeps ≤ max_messages and
    # never drops content). If even that single merged bubble is huge, it is
    # left as-is; _hard_chunk handles the no-boundary case.
    head = bubbles[: max_messages - 1]
    tail = "".join(bubbles[max_messages - 1 :])
    return head + [tail]


def _hard_chunk(text: str, max_chars: int, max_messages: int) -> list[str]:
    """Last-resort hard character chunk, merging overflow into the last bubble."""
    chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    if len(chunks) <= max_messages:
        return chunks
    head = chunks[: max_messages - 1]
    tail = "".join(chunks[max_messages - 1 :])
    return head + [tail]
