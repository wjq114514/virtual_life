"""Deterministic focus continuity, release hysteresis and bounded durable history."""

from datetime import timedelta

import pytest
import test_proactivity
from test_proactivity import decisions, observe, policy, sample
from virtual_life.focus import FocusService
from virtual_life.proactivity import ProactivityService

runtime = test_proactivity.runtime


def focus(runtime):
    return runtime[1].state.proactivity.focus.get("desk")


def advance(runtime, seconds):
    runtime[2].value += timedelta(seconds=seconds)


def focused(runtime, sequence=1, **changes):
    return observe(runtime, sequence, focus="focused", boundary="none", boundary_id=None, **changes)


def unfocused(runtime, sequence, **changes):
    return observe(
        runtime, sequence, focus="unfocused", boundary="none", boundary_id=None, **changes
    )


def confirm(runtime, sequence=1):
    focused(runtime, sequence)
    advance(runtime, 60)
    focused(runtime, sequence + 1)
    assert focus(runtime)["state"] == "focused"


def test_initial_unknown_and_unfocused_boundary_remains_eligible(runtime):
    state = focus(runtime)
    assert state["state"] == "unknown" and state["stale"]
    assert state["source"] == "host_advisory"
    assert not state["protection_active"] and state["completed_count"] == 0
    policy(runtime)
    observe(runtime)
    assert focus(runtime)["state"] == "unfocused"
    assert decisions(runtime)[0]["status"] == "queued"
    assert runtime[3].requests == []
    assert runtime[1].state.memory.budget_snapshot("sui")["spent_tokens"] == 0


def test_focused_protection_immediate_but_confirmation_requires_sixty_seconds(runtime):
    focused(runtime)
    first = focus(runtime)
    assert first["state"] == "candidate" and first["protection_active"]
    assert first["confirmed_at"] is None
    advance(runtime, 59)
    focused(runtime, 2)
    assert focus(runtime)["state"] == "candidate"
    advance(runtime, 1)
    focused(runtime, 3)
    state = focus(runtime)
    assert state["state"] == "focused"
    assert state["started_at"] == first["started_at"]
    assert state["duration_seconds"] == 60
    assert state["confirmed_at"] is not None and state["completed_count"] == 0
    assert runtime[3].requests == []


def test_unfocused_release_hysteresis_and_duration_excludes_release_grace(runtime):
    confirm(runtime)
    advance(runtime, 10)
    unfocused(runtime, 3)
    release = focus(runtime)
    assert release["state"] == "releasing" and release["protection_active"]
    assert release["duration_seconds"] == 70
    advance(runtime, 29)
    unfocused(runtime, 4)
    assert focus(runtime)["protection_active"]
    assert focus(runtime)["duration_seconds"] == 70
    advance(runtime, 1)
    unfocused(runtime, 5)
    done = focus(runtime)
    assert done["state"] == "unfocused" and not done["protection_active"]
    assert done["completed_count"] == 1
    segment = done["recent_segments"][0]
    assert segment["duration_seconds"] == 70 and segment["reason"] == "unfocused"
    assert segment["ended_at"] == release["release_started_at"]


def test_brief_unfocused_fluctuation_cancels_release_without_new_segment(runtime):
    confirm(runtime)
    start = focus(runtime)["started_at"]
    advance(runtime, 10)
    unfocused(runtime, 3)
    advance(runtime, 15)
    focused(runtime, 4)
    state = focus(runtime)
    assert state["state"] == "focused" and state["release_started_at"] is None
    assert state["started_at"] == start and state["completed_count"] == 0
    assert state["recent_segments"] == []
    advance(runtime, 1)
    observe(runtime, 5)
    assert focus(runtime)["completed_count"] == 1
    assert focus(runtime)["recent_segments"][0]["started_at"] == start


def test_candidate_fluctuation_resets_confirmation_continuity(runtime):
    focused(runtime)
    advance(runtime, 40)
    unfocused(runtime, 2)
    advance(runtime, 10)
    focused(runtime, 3)
    advance(runtime, 20)
    focused(runtime, 4)
    assert focus(runtime)["state"] == "candidate"
    advance(runtime, 40)
    focused(runtime, 5)
    assert focus(runtime)["state"] == "focused"
    assert focus(runtime)["duration_seconds"] == 110


@pytest.mark.parametrize("boundary", ["task_completed", "break_started"])
def test_explicit_safe_boundary_ends_protection_immediately(runtime, boundary):
    policy(runtime)
    confirm(runtime)
    observe(runtime, 3, boundary=boundary)
    state = focus(runtime)
    assert state["completed_count"] == 1 and not state["protection_active"]
    assert state["recent_segments"][0]["reason"] == "explicit_boundary"
    assert decisions(runtime)[-1]["status"] == "queued"


def test_user_returned_boundary_does_not_bypass_focused_or_release_protection(runtime):
    policy(runtime)
    focused(runtime)
    advance(runtime, 1)
    observe(runtime, 2, focus="focused", boundary="user_returned")
    assert decisions(runtime)[-1]["reason"] == "focused"
    advance(runtime, 1)
    observe(runtime, 3, boundary="user_returned")
    assert focus(runtime)["state"] == "releasing"
    assert decisions(runtime)[-1]["reason"] == "focus_protection"
    assert runtime[1].state.proactivity.process_next() is None
    assert runtime[3].requests == []


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"focus": "unknown"}, "attention_unknown"),
        ({"focus": "suspected_stuck"}, "suspected_stuck"),
        ({"user_present": False}, "user_away"),
        ({"locked": True}, "user_away"),
        ({"meeting": True}, "meeting_or_sharing"),
        ({"screen_sharing": True}, "meeting_or_sharing"),
        ({"activity": "meeting"}, "meeting_or_sharing"),
        ({"availability": "busy"}, "busy"),
        ({"locked": None}, "attention_unknown"),
    ],
)
def test_hard_invalid_inputs_end_segment_and_keep_existing_reason_priority(
    runtime, changes, reason
):
    policy(runtime)
    confirm(runtime)
    advance(runtime, 1)
    observe(runtime, 3, **changes)
    state = focus(runtime)
    assert state["state"] == "unknown" and not state["protection_active"]
    assert state["completed_count"] == 1
    assert state["recent_segments"][0]["reason"] == "attention_invalid"
    assert decisions(runtime)[-1]["reason"] == reason
    assert runtime[3].requests == []


def test_stale_snapshot_read_only_caps_duration_and_next_sample_closes_gap(runtime):
    confirm(runtime)
    service = runtime[1].state.proactivity.focus
    advance(runtime, 300)
    state = focus(runtime)
    assert state["state"] == "unknown" and state["stale"]
    assert not state["protection_active"] and state["duration_seconds"] == 150
    assert state["completed_count"] == 0
    with service.store.transaction() as connection:
        before = connection.total_changes
        assert service.snapshot(connection, "desk", runtime[2].value) == state
        assert connection.total_changes == before
    focused(runtime, 3)
    current = focus(runtime)
    assert current["state"] == "candidate" and current["duration_seconds"] == 0
    assert current["completed_count"] == 1
    assert current["recent_segments"][0]["duration_seconds"] == 150
    assert current["recent_segments"][0]["reason"] == "sample_gap"


def test_expired_candidate_never_becomes_confirmed_from_gap(runtime):
    focused(runtime, ttl_seconds=30)
    advance(runtime, 61)
    focused(runtime, 2)
    assert focus(runtime)["state"] == "candidate"
    assert focus(runtime)["duration_seconds"] == 0
    assert focus(runtime)["completed_count"] == 0
    assert focus(runtime)["recent_segments"] == []


def test_session_change_interrupts_without_synthesizing_boundaries(runtime):
    confirm(runtime)
    advance(runtime, 1)
    focused(runtime, 3, session_id="new-session")
    state = focus(runtime)
    assert state["state"] == "candidate" and state["duration_seconds"] == 0
    assert state["completed_count"] == 1
    assert state["recent_segments"][0]["reason"] == "session_changed"
    assert decisions(runtime) == []
    assert runtime[3].requests == []


def test_rejected_and_replayed_attention_never_change_focus(runtime):
    accepted = focused(runtime)
    state = focus(runtime)
    client = runtime[0]
    request = {
        "command_id": "attention-1",
        "sample": sample(runtime, focus="focused", boundary="none", boundary_id=None),
    }
    assert client.post("/v1/nodes/desk/attention", json=request).json() == accepted
    assert focus(runtime) == state
    for changes in (
        {"sequence": 0},
        {"sampled_at": (runtime[2].value + timedelta(seconds=10)).isoformat()},
    ):
        body = {
            "command_id": "bad-" + str(len(changes)) + str(changes),
            "sample": sample(runtime, **{"sequence": 2, **changes}),
        }
        assert client.post("/v1/nodes/desk/attention", json=body).status_code == 409
        assert focus(runtime) == state


def test_focus_observation_rolls_back_with_attention_and_candidate(runtime, monkeypatch):
    policy(runtime)
    service = runtime[1].state.proactivity
    original = service.focus.observe

    def fail(*args):
        original(*args)
        raise RuntimeError("injected focus failure")

    monkeypatch.setattr(service.focus, "observe", fail)
    with pytest.raises(RuntimeError, match="injected focus failure"):
        focused(runtime)
    assert focus(runtime)["stale"]
    assert service.get_attention("desk")["sample"] is None
    assert decisions(runtime) == []
    assert runtime[3].requests == []
    with service.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attention_sessions").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runtime_commands WHERE command_id='attention-1'"
            ).fetchone()[0]
            == 0
        )


def test_restart_preserves_focus_and_completed_history(runtime):
    confirm(runtime)
    service = runtime[1].state.proactivity
    state = focus(runtime)
    restarted = ProactivityService(runtime[1].state.dialogue)
    assert restarted.focus.get("desk") == state
    observe(runtime, 3)
    assert FocusService(service.coordinator).get("desk") == focus(runtime)
    assert focus(runtime)["completed_count"] == 1


def test_history_retention_preserves_lifetime_count_and_recent_twenty(runtime):
    sequence = 1
    for _ in range(105):
        focused(runtime, sequence)
        advance(runtime, 60)
        focused(runtime, sequence + 1)
        observe(runtime, sequence + 2)
        advance(runtime, 1)
        sequence += 3
    state = focus(runtime)
    assert state["completed_count"] == 105
    assert len(state["recent_segments"]) == 20
    assert state["recent_segments"][0]["started_at"] > state["recent_segments"][-1]["started_at"]
    with runtime[1].state.proactivity.store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM focus_segments WHERE node_id='desk'"
            ).fetchone()[0]
            == 100
        )
    assert runtime[3].requests == []


def test_latest_decisions_are_bounded_ascending_and_preserve_after_sequence(runtime):
    service = runtime[1].state.proactivity
    for sequence in range(1, 7):
        observe(runtime, sequence)
    all_items = service.decisions("sui")["decisions"]
    assert len(all_items) == 6
    assert service.decisions("sui", limit=2)["decisions"] == all_items[:2]
    latest = service.decisions("sui", limit=2, latest=True)
    assert latest["decisions"] == all_items[-2:]
    assert latest["next_sequence"] == all_items[-1]["sequence"]
    after = service.decisions(
        "sui", after_sequence=all_items[-2]["sequence"], limit=20, latest=True
    )
    assert after["decisions"] == all_items[-1:]
    empty = service.decisions("sui", after_sequence=all_items[-1]["sequence"], latest=True)
    assert empty == {"decisions": [], "next_sequence": all_items[-1]["sequence"]}


def test_unconfirmed_candidate_end_does_not_count_as_completed_focus(runtime):
    focused(runtime)
    advance(runtime, 59)
    observe(runtime, 2)
    state = focus(runtime)
    assert state["state"] == "unfocused" and not state["protection_active"]
    assert state["completed_count"] == 0 and state["recent_segments"] == []


def test_rollback_preserves_confirmed_segment_and_completion_counter(runtime, monkeypatch):
    confirm(runtime)
    before = focus(runtime)
    service = runtime[1].state.proactivity
    original = service.focus.observe

    def fail(*args):
        original(*args)
        raise RuntimeError("injected completion failure")

    monkeypatch.setattr(service.focus, "observe", fail)
    with pytest.raises(RuntimeError, match="injected completion failure"):
        observe(runtime, 3)
    assert focus(runtime) == before
    assert decisions(runtime) == []
    with service.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM focus_segments").fetchone()[0] == 0
    assert runtime[1].state.memory.budget_snapshot("sui")["spent_tokens"] == 0
    assert runtime[3].requests == []
