"""Deterministic continuous-speech turn transitions under a virtual clock."""

import itertools
from datetime import UTC, datetime

import pytest

from virtual_life_linux.voice.clock import VirtualClock
from virtual_life_linux.voice.turns import TurnManager, TurnState, is_obviously_incomplete

WALL = datetime(2026, 1, 1, tzinfo=UTC)


def make_manager(**kwargs) -> tuple[VirtualClock, TurnManager]:
    clock = VirtualClock()
    counter = itertools.count(1)
    manager = TurnManager(
        clock=clock,
        sample_rate=16_000,
        wall_clock=lambda: WALL,
        id_factory=lambda: f"turn-{next(counter)}",
        **kwargs,
    )
    return clock, manager


def audio(clock, manager, sample_index, *, active=True, seconds=0.2):
    clock.advance(seconds)
    return manager.on_audio_activity(active, sample_index=sample_index)


def asr(manager, text, *, segment_id="s1", revision=1, sample_start=0, kind="hypothesis"):
    return manager.on_asr_event(
        kind, segment_id=segment_id, revision=revision, sample_start=sample_start, text=text
    )


def kinds(actions):
    return [action.kind for action in actions]


def silence_until_flush(clock, manager, *, steps=8, seconds=0.25, sample_index=8000):
    collected = []
    for _ in range(steps):
        collected.extend(audio(clock, manager, sample_index, active=False, seconds=seconds))
        if any(action.kind == "flush" for action in collected):
            break
    return collected


def test_sentence_with_pause_commits_once():
    clock, manager = make_manager()
    assert kinds(manager.arm()) == ["state"]
    audio(clock, manager, 0)
    assert manager.state == TurnState.SPEAKING
    audio(clock, manager, 1600)
    asr(manager, "你好")
    asr(manager, "你好。", revision=2, kind="segment_end")

    actions = silence_until_flush(clock, manager)
    flush = next(action for action in actions if action.kind == "flush")
    assert manager.state == TurnState.FINALIZING
    commit_actions = manager.flush_complete(flush.generation, final_text="你好。")
    commit = next(action for action in commit_actions if action.kind == "commit")
    assert commit.data["text"] == "你好。"
    assert commit.data["end_reason"] == "adaptive_endpoint"
    assert manager.state == TurnState.AWAITING_REPLY


def test_incomplete_expression_waits_then_asks_for_confirmation():
    clock, manager = make_manager()
    manager.arm()
    audio(clock, manager, 0)
    asr(manager, "我想")

    early = silence_until_flush(clock, manager, steps=4)
    assert not any(action.kind == "flush" for action in early)

    later = []
    for _ in range(8):
        later.extend(audio(clock, manager, 3200, active=False, seconds=0.3))
        if manager.state == TurnState.NEEDS_CONFIRMATION:
            break
    assert manager.state == TurnState.NEEDS_CONFIRMATION
    assert any(action.kind == "needs_confirmation" for action in later)


def test_repeated_partial_revision_does_not_duplicate_text():
    clock, manager = make_manager()
    manager.arm()
    audio(clock, manager, 0)
    first = asr(manager, "你好", revision=1)
    assert kinds(first) == ["partial"]
    repeat = asr(manager, "你好", revision=1)
    assert repeat == []
    updated = asr(manager, "你好世界", revision=2)
    partial = next(action for action in updated if action.kind == "partial")
    assert partial.data["text"] == "你好世界"


def test_speech_during_finalizing_cancels_the_stale_flush():
    clock, manager = make_manager()
    manager.arm()
    audio(clock, manager, 0)
    asr(manager, "第一句。")
    actions = silence_until_flush(clock, manager)
    flush = next(action for action in actions if action.kind == "flush")
    assert manager.state == TurnState.FINALIZING

    resumed = audio(clock, manager, 8000)
    assert any(
        action.kind == "cancel_flush" and action.generation == flush.generation
        for action in resumed
    )
    assert manager.state == TurnState.SPEAKING
    # The late flush from the abandoned generation must not commit.
    assert manager.flush_complete(flush.generation) == []


def test_self_correction_stays_in_one_turn():
    clock, manager = make_manager()
    manager.arm()
    audio(clock, manager, 0)
    asr(manager, "去北京", revision=1)
    asr(manager, "去北京，不是", revision=2)
    asr(manager, "去北京，不是，去上海", revision=3)
    actions = silence_until_flush(clock, manager)
    flush = next(action for action in actions if action.kind == "flush")
    commit = next(
        action for action in manager.flush_complete(flush.generation) if action.kind == "commit"
    )
    assert commit.data["text"] == "去北京，不是，去上海"


def test_speech_after_commit_triggers_barge_in_with_continuation():
    clock, manager = make_manager()
    manager.arm()
    audio(clock, manager, 0)
    asr(manager, "谢谢你。")
    flush = next(action for action in silence_until_flush(clock, manager) if action.kind == "flush")
    commit = next(
        action for action in manager.flush_complete(flush.generation) if action.kind == "commit"
    )
    committed_turn = commit.data["turn_id"]
    committed_generation = flush.generation

    resumed = audio(clock, manager, 12_000)
    barge = next(action for action in resumed if action.kind == "barge_in")
    assert barge.data["target_turn_id"] == committed_turn
    assert barge.data["output_generation"] == committed_generation
    manager.on_asr_event(
        "hypothesis", segment_id="s2", revision=1, sample_start=12_000, text="补充"
    )
    assert manager.draft == "补充"


@pytest.mark.parametrize("text", ["好", "不", "继续", "算了吧。"])
def test_short_answers_are_not_treated_as_incomplete(text):
    assert not is_obviously_incomplete(text)


@pytest.mark.parametrize("text", ["我想", "因为", "然后"])
def test_trailing_connectors_are_incomplete(text):
    assert is_obviously_incomplete(text)


def test_idle_timeout_disarms_an_unused_session():
    clock, manager = make_manager(idle_timeout_seconds=1.0)
    manager.arm()
    clock.advance(1.1)
    actions = manager.tick()
    assert "idle_timeout" in kinds(actions)
    assert manager.state == TurnState.OFF


def test_discontinuous_audio_requires_confirmation():
    clock, manager = make_manager()
    manager.arm()
    audio(clock, manager, 0)
    actions = audio(clock, manager, 512, active=True)
    actions.extend(manager.on_audio_activity(True, sample_index=1024, discontinuity=True))
    assert manager.state == TurnState.NEEDS_CONFIRMATION


def test_max_turn_duration_requires_confirmation():
    clock, manager = make_manager(max_turn_seconds=1.0)
    manager.arm()
    audio(clock, manager, 0, seconds=0.1)
    asr(manager, "很长的回答")
    actions = audio(clock, manager, 16_000, seconds=1.2)
    assert "needs_confirmation" in kinds(actions)


def test_push_to_talk_commits_only_on_manual_release():
    clock, manager = make_manager(auto_commit=False)
    manager.arm()
    audio(clock, manager, 0)
    asr(manager, "按住说话")

    quiet = []
    for sample in (3200, 6400, 9600, 12_800):
        quiet.extend(audio(clock, manager, sample, active=False, seconds=0.4))
    assert not any(action.kind == "flush" for action in quiet)
    assert manager.state == TurnState.POSSIBLE_END

    release = manager.manual_commit(end_reason="push_to_talk")
    assert release[0].kind == "flush"
    assert release[0].data["end_reason"] == "push_to_talk"
    commit = next(
        action
        for action in manager.flush_complete(release[0].generation)
        if action.kind == "commit"
    )
    assert commit.data["text"] == "按住说话"
    assert commit.data["end_reason"] == "push_to_talk"
