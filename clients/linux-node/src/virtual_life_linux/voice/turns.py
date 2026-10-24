"""Deterministic continuous-speech turn management."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable

from .clock import Clock, MonotonicClock

MAX_TURN_SECONDS = 45.0
MAX_TURN_CHARS = 4096
_INCOMPLETE_SUFFIXES = (
    "我想",
    "因为",
    "如果",
    "但是",
    "然后",
    "首先",
    "第一",
    "比如",
    "虽然",
    "要不",
    "也就是说",
    "比如说",
    "可能",
    "等下",
)
_TRAILING_CONTINUATION = re.compile(r"(?:[,，、:：;；…—-]|\b(?:and|or|because|if|but))\s*$", re.I)


class TurnState(StrEnum):
    OFF = "off"
    ARMED = "armed"
    SPEAKING = "speaking"
    POSSIBLE_END = "possible_end"
    FINALIZING = "finalizing"
    AWAITING_REPLY = "awaiting_reply"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass(frozen=True, slots=True)
class TurnAction:
    kind: str
    generation: int
    data: dict = field(default_factory=dict)


@dataclass(slots=True)
class _Segment:
    sample_start: int
    text: str = ""
    revision: int = -1
    final: bool = False


class TurnManager:
    """Pure turn transitions; effects are returned as actions for the daemon."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        sample_rate: int = 16_000,
        candidate_silence_seconds: float = 0.45,
        commit_silence_seconds: float = 0.85,
        incomplete_wait_seconds: float = 2.4,
        stability_seconds: float = 0.25,
        idle_timeout_seconds: float = 120.0,
        max_turn_seconds: float = MAX_TURN_SECONDS,
        max_turn_chars: int = MAX_TURN_CHARS,
        wall_clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        auto_commit: bool = True,
    ):
        if sample_rate <= 0 or max_turn_seconds <= 0 or max_turn_chars <= 0:
            raise ValueError("turn limits must be positive")
        self.clock = clock or MonotonicClock()
        self.auto_commit = bool(auto_commit)
        self.sample_rate = sample_rate
        self.candidate_silence_ns = int(candidate_silence_seconds * 1e9)
        self.commit_silence_ns = int(commit_silence_seconds * 1e9)
        self.incomplete_wait_ns = int(incomplete_wait_seconds * 1e9)
        self.stability_ns = int(stability_seconds * 1e9)
        self.idle_timeout_ns = int(idle_timeout_seconds * 1e9)
        self.max_turn_ns = int(max_turn_seconds * 1e9)
        self.max_turn_samples = int(sample_rate * max_turn_seconds)
        self.max_turn_chars = max_turn_chars
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)

        self.state = TurnState.OFF
        self.generation = 0
        self._armed_ns = 0
        self._last_activity_ns = 0
        self._speech_started_ns: int | None = None
        self._last_voice_ns: int | None = None
        self._last_sample = 0
        self._start_sample = 0
        self._last_text_change_ns: int | None = None
        self._segments: dict[str, _Segment] = {}
        self._active_segment: str | None = None
        self._continuation_of: str | None = None
        self._committed_turn_id: str | None = None
        self._committed_generation: int | None = None
        self._discontinuity = False
        self._finalize_reason = "adaptive_endpoint"

    @property
    def draft(self) -> str:
        ordered = sorted(self._segments.items(), key=lambda item: item[1].sample_start)
        text = "".join(segment.text for _, segment in ordered)
        return text[: self.max_turn_chars]

    @property
    def audio_duration_ms(self) -> int:
        samples = max(0, self._last_sample - self._start_sample)
        return min(45_000, int(samples * 1000 / self.sample_rate))

    def arm(self) -> list[TurnAction]:
        if self.state != TurnState.OFF:
            return []
        self.generation += 1
        now = self.clock.now_ns()
        self.state = TurnState.ARMED
        self._armed_ns = now
        self._last_activity_ns = now
        return [self._action("state", state=self.state.value)]

    def on_audio_activity(
        self,
        active: bool,
        *,
        sample_index: int,
        discontinuity: bool = False,
    ) -> list[TurnAction]:
        actions = self.tick()
        if self.state in (TurnState.OFF, TurnState.NEEDS_CONFIRMATION):
            return actions
        if discontinuity:
            self._discontinuity = True
            return actions + self._needs_confirmation("audio_discontinuity")

        now = self.clock.now_ns()
        self._last_sample = max(self._last_sample, sample_index)
        if active:
            self._last_activity_ns = now
            self._last_voice_ns = now
            if self.state == TurnState.AWAITING_REPLY:
                target_turn_id = self._committed_turn_id
                target_generation = self._committed_generation
                self._begin_turn(sample_index, now)
                self._continuation_of = target_turn_id
                actions.extend(
                    [
                        self._action(
                            "barge_in",
                            target_turn_id=target_turn_id,
                            output_generation=target_generation,
                        ),
                        self._action("state", state=self.state.value),
                    ]
                )
                return actions
            if self.state == TurnState.FINALIZING:
                stale_generation = self.generation
                self.generation += 1
                self.state = TurnState.SPEAKING
                actions.append(self._action_for(stale_generation, "cancel_flush"))
            elif self.state in (TurnState.ARMED, TurnState.POSSIBLE_END):
                if self.state == TurnState.ARMED:
                    self._begin_turn(sample_index, now)
                else:
                    self.state = TurnState.SPEAKING
                    self._last_voice_ns = now
                actions.append(self._action("state", state=self.state.value))
            elif self.state == TurnState.SPEAKING and self._speech_started_ns is None:
                self._begin_turn(sample_index, now)
            if self._speech_started_ns is not None:
                elapsed_ns = now - self._speech_started_ns
                sample_span = max(0, sample_index - self._start_sample)
                if elapsed_ns >= self.max_turn_ns or sample_span > self.max_turn_samples:
                    return actions + self._needs_confirmation("turn_duration_limit")
            return actions

        if self.state in (TurnState.SPEAKING, TurnState.POSSIBLE_END, TurnState.FINALIZING):
            self._last_activity_ns = now
        return actions

    def on_asr_event(
        self,
        kind: str,
        *,
        segment_id: str,
        revision: int,
        sample_start: int,
        text: str,
    ) -> list[TurnAction]:
        actions = self.tick()
        if self.state in (TurnState.OFF, TurnState.ARMED, TurnState.NEEDS_CONFIRMATION):
            return actions
        if kind not in ("hypothesis", "segment_end"):
            return actions
        if not isinstance(segment_id, str) or not segment_id or revision < 0:
            return actions
        if not isinstance(text, str):
            return actions
        segment = self._segments.setdefault(segment_id, _Segment(max(0, sample_start)))
        if revision < segment.revision or (revision == segment.revision and text == segment.text):
            return actions
        segment.revision = revision
        segment.text = text.strip()
        segment.final = kind == "segment_end"
        self._active_segment = segment_id
        self._last_text_change_ns = self.clock.now_ns()
        if len(self.draft) > self.max_turn_chars or len(text) > self.max_turn_chars:
            return actions + self._needs_confirmation("turn_character_limit")
        actions.append(
            self._action("partial", text=self.draft, segment_id=segment_id, revision=revision)
        )
        return actions

    def tick(self) -> list[TurnAction]:
        now = self.clock.now_ns()
        if self.state == TurnState.OFF:
            return []
        if self.state == TurnState.ARMED:
            if now - self._last_activity_ns >= self.idle_timeout_ns:
                self.state = TurnState.OFF
                self.generation += 1
                return [self._action("idle_timeout"), self._action("state", state="off")]
            return []
        if self.state not in (TurnState.SPEAKING, TurnState.POSSIBLE_END):
            return []
        if (
            self._speech_started_ns is not None
            and now - self._speech_started_ns >= self.max_turn_ns
        ):
            return self._needs_confirmation("turn_duration_limit")
        if self._last_voice_ns is None:
            return []
        silence_ns = now - self._last_voice_ns
        actions: list[TurnAction] = []
        if silence_ns >= self.candidate_silence_ns and self.state == TurnState.SPEAKING:
            self.state = TurnState.POSSIBLE_END
            actions.append(self._action("candidate_end", text=self.draft))
            actions.append(self._action("state", state=self.state.value))
        if not self.auto_commit:
            # Push-to-talk: the user, not silence, decides when the turn ends.
            return actions
        if silence_ns < self.commit_silence_ns or not self.draft:
            return actions
        text = self.draft
        incomplete = is_obviously_incomplete(text)
        if incomplete and silence_ns < self.incomplete_wait_ns:
            return actions
        if incomplete:
            return actions + self._needs_confirmation("unfinished_expression")
        stable = (
            self._last_text_change_ns is not None
            and now - self._last_text_change_ns >= self.stability_ns
        )
        if stable:
            return actions + self._start_finalize("adaptive_endpoint")
        return actions

    def flush_complete(self, generation: int, *, final_text: str | None = None) -> list[TurnAction]:
        if self.state != TurnState.FINALIZING or generation != self.generation:
            return []
        if final_text is not None and self._active_segment is not None:
            segment = self._segments[self._active_segment]
            segment.text = final_text.strip()
            segment.revision += 1
            segment.final = True
            self._last_text_change_ns = self.clock.now_ns()
        text = self.draft.strip()
        if not text:
            self.state = TurnState.ARMED
            self._reset_turn()
            return [self._action("state", state=self.state.value)]
        if len(text) > self.max_turn_chars:
            return self._needs_confirmation("turn_character_limit")
        turn_id = self.id_factory()
        self.state = TurnState.AWAITING_REPLY
        self._committed_turn_id = turn_id
        self._committed_generation = generation
        now = self.wall_clock().astimezone(UTC)
        ended_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        started_at = now.timestamp() - self.audio_duration_ms / 1000
        started = datetime.fromtimestamp(started_at, UTC).isoformat(timespec="milliseconds")
        started = started.replace("+00:00", "Z")
        data = {
            "turn_id": turn_id,
            "text": text,
            "end_reason": self._finalize_reason,
            "started_at": started,
            "ended_at": ended_at,
            "audio_duration_ms": self.audio_duration_ms,
            "discontinuity": self._discontinuity,
            "continuation_of_turn_id": self._continuation_of,
        }
        self._reset_turn(keep_continuation=True)
        return [self._action("commit", **data), self._action("state", state=self.state.value)]

    def manual_commit(self, end_reason: str = "manual") -> list[TurnAction]:
        if self.state not in (
            TurnState.SPEAKING,
            TurnState.POSSIBLE_END,
            TurnState.NEEDS_CONFIRMATION,
        ):
            return []
        self.generation += 1
        self.state = TurnState.FINALIZING
        self._finalize_reason = end_reason
        return [self._action("flush", end_reason=end_reason)]

    def reply_finished(self, turn_id: str) -> list[TurnAction]:
        if self.state != TurnState.AWAITING_REPLY or turn_id != self._committed_turn_id:
            return []
        self.state = TurnState.ARMED
        self._committed_turn_id = None
        self._committed_generation = None
        self._continuation_of = None
        self._last_activity_ns = self.clock.now_ns()
        return [self._action("state", state=self.state.value)]

    def cancel(self, reason: str = "cancelled") -> list[TurnAction]:
        stale_generation = self.generation
        self.generation += 1
        self.state = TurnState.OFF
        self._reset_turn()
        self._committed_turn_id = None
        self._committed_generation = None
        self._continuation_of = None
        return [
            self._action_for(stale_generation, "cancel_flush"),
            self._action("cancel", reason=reason),
            self._action("state", state=self.state.value),
        ]

    def _begin_turn(self, sample_index: int, now_ns: int) -> None:
        self.generation += 1
        self._reset_turn()
        self._start_sample = max(0, sample_index)
        self._last_sample = self._start_sample
        self._speech_started_ns = now_ns
        self._last_voice_ns = now_ns
        self._last_activity_ns = now_ns
        self.state = TurnState.SPEAKING

    def _start_finalize(self, reason: str) -> list[TurnAction]:
        if self.state == TurnState.FINALIZING:
            return []
        self.generation += 1
        self.state = TurnState.FINALIZING
        self._finalize_reason = reason
        return [self._action("flush", end_reason=reason)]

    def _needs_confirmation(self, reason: str) -> list[TurnAction]:
        if self.state == TurnState.NEEDS_CONFIRMATION:
            return []
        self.generation += 1
        self.state = TurnState.NEEDS_CONFIRMATION
        return [
            self._action("needs_confirmation", reason=reason, text=self.draft),
            self._action("state", state=self.state.value),
        ]

    def _reset_turn(self, *, keep_continuation: bool = False) -> None:
        self._speech_started_ns = None
        self._last_voice_ns = None
        self._last_text_change_ns = None
        self._segments.clear()
        self._active_segment = None
        self._last_sample = 0
        self._start_sample = 0
        self._discontinuity = False
        self._finalize_reason = "adaptive_endpoint"
        if not keep_continuation:
            self._continuation_of = None

    def _action(self, kind: str, **data) -> TurnAction:
        return TurnAction(kind, self.generation, data)

    @staticmethod
    def _action_for(generation: int, kind: str, **data) -> TurnAction:
        return TurnAction(kind, generation, data)


def is_obviously_incomplete(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return normalized.endswith(_INCOMPLETE_SUFFIXES) or bool(
        _TRAILING_CONTINUATION.search(normalized)
    )
