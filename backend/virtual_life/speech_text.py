"""Deterministic text segmentation and speech normalization for TTS.

The dialogue transport delivers *cumulative* assistant drafts: every delta event
contains the whole reply so far. Synthesizing each delta would repeat already
spoken text, so :class:`SpeechTextAssembler` keeps the observed text and turns it
into non-overlapping natural chunks exactly once.

This module contains no provider calls and no I/O; it is deliberately pure so it
can be replayed deterministically under a virtual clock.
"""

import re
from dataclasses import dataclass

# Sentence terminators that reliably end a spoken clause. A full stop in the
# middle of "3.14" or "example.com" is protected below.
_SENTENCE_END = "。！？!?；;."
# Softer pauses that may end the opening clause early to reduce time-to-first-audio.
_SOFT_PAUSE = "，、,:："
_OPEN_QUOTES = "“”‘’「」『』()（）[]【】《》"
_ABBREVIATION = re.compile(r"(?:[A-Za-z]\.){2,}$|(?:\d)\.$")

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_URL = re.compile(r"(?:https?://|www\.)\S+")
_MARKDOWN_MARKERS = re.compile(r"[*_~#>]+")
_HTML_TAG = re.compile(r"<[^>]{1,64}>")

CODE_PLACEHOLDER = "代码已显示在屏幕上"


def _is_decimal_or_abbreviation(text: str, index: int) -> bool:
    """Return True when a terminator at ``index`` does not end a sentence."""
    if text[index] not in ".。":
        return False
    # ASCII '.' inside decimals (3.14) or domain-like tokens (example.com).
    if text[index] == ".":
        before = text[max(0, index - 1)]
        after = text[index + 1] if index + 1 < len(text) else ""
        if before.isdigit() and after.isdigit():
            return True
        if before.isalnum() and after.isalnum():
            return True
        if _ABBREVIATION.search(text[max(0, index - 4) : index + 1]):
            return True
    return False


@dataclass(frozen=True, slots=True)
class SpeechChunk:
    """One synthesized unit mapped back to a slice of the display text."""

    text: str
    spoken_text: str
    char_start: int
    char_end: int
    first: bool = False


def normalize_spoken(text: str) -> str:
    """Render display text into something a TTS voice can read sensibly.

    Markdown decoration, links and large code blocks are removed. This is a
    rendering rule, never a second model call rewriting the answer.
    """
    if not isinstance(text, str):
        return ""
    value = _FENCE.sub(CODE_PLACEHOLDER, text)
    value = _INLINE_CODE.sub(r"\1", value)
    value = _MARKDOWN_LINK.sub(r"\1", value)
    value = _URL.sub("", value)
    value = _HTML_TAG.sub("", value)
    value = _MARKDOWN_MARKERS.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


class SpeechTextAssembler:
    """Turn a cumulative assistant draft into ordered, non-repeating chunks."""

    def __init__(
        self,
        *,
        first_min_chars: int = 10,
        target_min_chars: int = 15,
        target_max_chars: int = 45,
        max_segment_chars: int = 80,
        pending_cap_seconds: float = 0.4,
        clock=None,
    ) -> None:
        for name, value in (
            ("first_min_chars", first_min_chars),
            ("target_min_chars", target_min_chars),
            ("target_max_chars", target_max_chars),
            ("max_segment_chars", max_segment_chars),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_segment_chars < target_max_chars:
            raise ValueError("max_segment_chars must be >= target_max_chars")
        self.first_min_chars = first_min_chars
        self.target_min_chars = target_min_chars
        self.target_max_chars = target_max_chars
        self.max_segment_chars = max_segment_chars
        self.pending_cap_seconds = float(pending_cap_seconds)
        self._clock = clock
        self._observed = ""
        self._emitted = 0
        self._first_emitted = False
        self._pending_since: float | None = None
        self._revision_anomaly: str | None = None

    @property
    def observed_text(self) -> str:
        return self._observed

    @property
    def emitted_text(self) -> str:
        return self._observed[: self._emitted]

    @property
    def pending_text(self) -> str:
        return self._observed[self._emitted :]

    @property
    def revision_anomaly(self) -> str | None:
        """``spoken_prefix_changed`` when a provider rewrote already-spoken text."""
        return self._revision_anomaly

    def _now(self) -> float:
        if self._clock is None:
            return 0.0
        return float(self._clock())

    def _boundary(self, region: str, *, allow_soft: bool, force: bool) -> int:
        """Return a split index in ``region`` or 0 when no safe boundary exists."""
        limit = self.max_segment_chars if force else self.target_max_chars
        minimum = self.first_min_chars if not self._first_emitted and allow_soft else 1
        last_soft = 0
        for index, char in enumerate(region[:limit]):
            if char in _SENTENCE_END and not _is_decimal_or_abbreviation(region, index):
                return index + 1
            if char in _SOFT_PAUSE and index + 1 >= minimum:
                last_soft = index + 1
        if last_soft:
            return last_soft
        if force and len(region) >= self.max_segment_chars:
            return self.max_segment_chars
        if force:
            floor = self.first_min_chars if not self._first_emitted else self.target_min_chars
            if len(region) >= floor:
                return len(region)
        return 0

    def _emit(self, region: str, cutoff: int) -> SpeechChunk | None:
        raw = region[:cutoff]
        if not raw.strip():
            # Only whitespace/punctuation: consume it without speaking.
            self._emitted += cutoff
            return None
        start = self._emitted
        self._emitted += cutoff
        first = not self._first_emitted
        self._first_emitted = True
        return SpeechChunk(
            text=raw,
            spoken_text=normalize_spoken(raw),
            char_start=start,
            char_end=self._emitted,
            first=first,
        )

    def observe(self, full_text: str, *, now: float | None = None) -> list[SpeechChunk]:
        """Feed the cumulative draft; return any newly finalizable chunks."""
        if not isinstance(full_text, str):
            raise TypeError("Assistant text must be a string")
        if not full_text.startswith(self.emitted_text):
            self._revision_anomaly = "spoken_prefix_changed"
            return []
        if not full_text.startswith(self._observed):
            self._pending_since = None
        self._observed = full_text
        chunks: list[SpeechChunk] = []
        while True:
            region = self._observed[self._emitted :]
            if not region:
                self._pending_since = None
                break
            bounded = region[: self.max_segment_chars]
            cutoff = self._boundary(region, allow_soft=True, force=False)
            if not cutoff:
                instant = self._now() if now is None else float(now)
                if self._pending_since is None:
                    self._pending_since = instant
                waited = instant - self._pending_since
                if len(bounded) >= self.max_segment_chars or (
                    self.pending_cap_seconds >= 0 and waited >= self.pending_cap_seconds
                ):
                    cutoff = self._boundary(region, allow_soft=False, force=True)
            if not cutoff:
                break
            chunk = self._emit(region, cutoff)
            self._pending_since = None
            if chunk is not None:
                chunks.append(chunk)
            if cutoff < len(bounded):
                # We only split inside the bounded window; remaining text is
                # handled on the next iteration or a later observe call.
                if chunk is not None:
                    break
        return chunks

    def flush(self) -> list[SpeechChunk]:
        """Finalize the remaining pending text; call only on normal completion."""
        chunks: list[SpeechChunk] = []
        while True:
            region = self._observed[self._emitted :]
            if not region:
                break
            bounded = region[: self.max_segment_chars]
            cutoff = self._boundary(region, allow_soft=True, force=False)
            if not cutoff:
                if len(bounded) >= self.max_segment_chars:
                    cutoff = self._boundary(region, allow_soft=False, force=True)
                else:
                    cutoff = len(region)
            chunk = self._emit(region, cutoff)
            if chunk is not None:
                chunks.append(chunk)
            if cutoff == 0:
                break
        self._pending_since = None
        return chunks
