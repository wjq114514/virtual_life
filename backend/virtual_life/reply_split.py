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

import hashlib
import json
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


def _squash(text: str) -> str:
    """Remove all whitespace so a split can be compared to its source verbatim."""
    return "".join(text.split())


def typing_delays(
    bubbles: list[str],
    *,
    companion_id: str = "",
) -> list[int]:
    """Per-bubble typing delays in milliseconds, matching ``bubbles`` 1:1.

    Models the pause a person takes between sending several short messages:
    the first bubble answers what was just read (short pause), later bubbles
    feel "typed" at a steady per-character rate. The value is the delay to
    wait *after the previous bubble* before sending this one.

    Deterministic on (companion_id, index, bubble text) so the same reply
    always gets the same rhythm — IM clients may replay the receipt — and no
    wall clock or RNG state is involved. Single-bubble replies return a small
    value so clients that honor it do not stall a normal one-message reply.
    """
    if not bubbles:
        return []
    delays: list[int] = []
    for index, bubble in enumerate(bubbles):
        chars = len(bubble.strip()) if bubble else 0
        base = 260 + 55 * chars
        if index > 0:
            base += 180  # picking the phone back up between bubbles
        base = min(base, 4200)
        seed = hashlib.sha256(f"{companion_id}|{index}|{bubble}".encode("utf-8")).digest()
        jitter = 0.85 + (int.from_bytes(seed[:2], "big") / 65535.0) * 0.3  # 0.85..1.15
        delays.append(max(60, min(int(base * jitter), 4200)))
    return delays


def _llm_split_prompt(text: str, max_messages: int, max_chars: int) -> str:
    return (
        "把下面的回复原文切分为若干条短消息，像聊天软件里连发几条那样自然。\n"
        "必须遵守的规则：\n"
        "1. 只能在原文的字符之间切分：每条消息都必须是原文的逐字片段，"
        "不得改写、增删、调换标点或语气。\n"
        "2. 顺序保持不变，所有消息按顺序拼接后必须等于原文（忽略首尾空白）。\n"
        f"3. 至多 {max_messages} 条；每条尽量不超过 {max_chars} 字。\n"
        '4. 只输出 JSON 对象：{{"messages": ["第一条", "第二条", ...]}}，不要输出其他内容。\n'
        f"原文：\n{text}"
    )


def llm_split_reply(
    text: str,
    provider,
    *,
    model: str,
    companion_id: str,
    task_id: str = "dialogue_llm_split",
    max_messages: int = 3,
    max_chars: int = 120,
    input_token_limit: int = 4096,
    output_token_limit: int = 400,
) -> list[str] | None:
    """Model-assisted semantic split of a long reply into short bubbles.

    Conservative port of MaiBot's ``llm_sentence_splitter``: the model is only
    allowed to choose *where* to cut the original text. The returned list is
    accepted only after a verbatim check — the bubbles, concatenated and
    stripped of whitespace, must reproduce the original text exactly. Any
    provider error, unparseable output, or failed verification returns ``None``
    so the caller falls back to the deterministic ``split_reply`` rules.
    """
    if not text or not text.strip() or not model.strip() or provider is None:
        return None
    if max_messages < 1:
        max_messages = 1
    from .ports import ModelRequest  # local import keeps module import side-effect free

    request = ModelRequest(
        task_id=task_id,
        companion_id=companion_id,
        purpose="dialogue",
        model=model,
        context_text=_llm_split_prompt(text, max_messages, max_chars),
        input_token_limit=input_token_limit,
        output_token_limit=output_token_limit,
    )
    try:
        result = provider.complete(request)
        raw = result.text
    except Exception:  # noqa: BLE001 - any provider failure falls back to rules
        return None
    if not raw or not raw.strip():
        return None

    # The model may wrap the JSON in prose or fences; find the outermost object.
    candidates = []
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    candidates.append(raw)
    messages: list[str] = []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            value = parsed.get("messages")
        elif isinstance(parsed, list):
            value = parsed
        else:
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            messages = [item.strip() for item in value]
            break
    if not messages or len(messages) > max_messages:
        return None
    # Verbatim verification: joined bubbles must reproduce the source text.
    if _squash("".join(messages)) != _squash(text):
        return None
    return messages
