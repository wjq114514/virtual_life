"""Ownership and crash recovery checks for the node runtime."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from virtual_life.coordinator import Coordinator, DomainError


@dataclass
class Clock:
    value: datetime = datetime(2026, 10, 11, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def runtime(tmp_path, clock):
    return Coordinator(tmp_path / "runtime.sqlite3", clock=clock)


def register(runtime, node_id, capabilities=None):
    return runtime.register_node(
        {
            "node_id": node_id,
            "label": node_id,
            "kind": "android" if node_id == "phone" else "desktop",
            "capabilities": capabilities
            if capabilities is not None
            else ["avatar.render", "reading.context"],
        }
    )


def attach(runtime):
    register(runtime, "desk")
    register(runtime, "phone")
    runtime.create_companion({"companion_id": "sui", "name": "随"})
    transfer = runtime.prepare_transfer(
        "sui", {"command_id": "initial", "target_node_id": "desk", "expected_revision": 0}
    )
    assert (
        runtime.ack_transfer(transfer["transfer_id"], {"node_id": "desk", "phase": "ready"})[
            "status"
        ]
        == "committed"
    )
    return runtime.get_companion("sui")


@pytest.fixture
def attached(runtime):
    attach(runtime)
    return runtime


def context_payload(state, command_id="reading", **fields):
    return {
        "command_id": command_id,
        "node_id": state["active_node_id"],
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        **fields,
    }


def prepare(runtime, command_id="to-phone", target="phone"):
    return runtime.prepare_transfer(
        "sui",
        {
            "command_id": command_id,
            "target_node_id": target,
            "expected_revision": runtime.get_companion("sui")["revision"],
        },
    )


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(DomainError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == code
    assert exc.value.status_code in (409, 422, 404)


def test_initial_snapshots_and_node_metadata(runtime, clock):
    first = register(runtime, "desk", ["text.display", "text.display", "reading.context"])
    assert first["online"] and first["context"] == {}
    assert first["capabilities"] == ["reading.context", "text.display"]
    assert runtime.list_nodes() == [first]
    assert runtime.list_companions() == []
    state = runtime.create_companion({"companion_id": "sui", "name": "随"})
    assert state["active_node_id"] is None and state["lease_expires_at"] is None
    assert state["epoch"] == state["revision"] == 0
    assert state["activity"] == {"type": "idle"}
    assert state["interaction_mode"] == "quiet" and state["memory_refs"] == []
    assert runtime.list_companions() == [state]
    clock.advance(1)
    runtime.heartbeat("desk", {"context": {"foreground": "reader", "attention": "unknown"}})
    refreshed = register(runtime, "desk", ["text.display"])
    assert refreshed["created_at"] == first["created_at"]
    assert refreshed["context"]["attention"] == "unknown"
    clock.advance(60)
    assert not runtime.get_node("desk")["online"]


@pytest.mark.parametrize("phases", [("ready", "released"), ("released", "ready")])
def test_both_acknowledgment_orders_preserve_context_and_single_ownership(attached, phases):
    state = attached.patch_context(
        "sui",
        context_payload(
            attached.get_companion("sui"),
            activity={"type": "co_reading", "book": "夜航船", "paragraph": 12},
            interaction_mode="focus",
            context={"topic": "窗外的雨"},
            memory_refs=["shared-reading"],
        ),
    )
    transfer = prepare(attached)
    assert transfer["snapshot"] == state
    assert attached.get_companion("sui") == state
    assert_error(
        "transfer_in_progress",
        attached.patch_context,
        "sui",
        context_payload(state, command_id="during-travel", context={"topic": "changed"}),
    )
    assert_error(
        "transfer_in_progress",
        attached.renew_lease,
        "sui",
        {"node_id": "desk", "epoch": state["epoch"]},
    )
    first_node = "phone" if phases[0] == "ready" else "desk"
    first = attached.ack_transfer(
        transfer["transfer_id"], {"node_id": first_node, "phase": phases[0]}
    )
    assert first["status"] == "prepared"
    assert attached.get_companion("sui")["active_node_id"] == "desk"
    second_node = "phone" if phases[1] == "ready" else "desk"
    committed = attached.ack_transfer(
        transfer["transfer_id"], {"node_id": second_node, "phase": phases[1]}
    )
    assert committed["status"] == "committed"
    assert committed["source_released"] and committed["target_ready"]
    current = attached.get_companion("sui")
    assert current["active_node_id"] == "phone"
    assert current["epoch"] == state["epoch"] + 1
    assert current["revision"] == state["revision"] + 1
    for field in ("activity", "context", "memory_refs", "interaction_mode"):
        assert current[field] == state[field]
    assert (
        attached.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})
        == committed
    )
    assert (
        attached.ack_transfer(transfer["transfer_id"], {"node_id": "desk", "phase": "released"})
        == committed
    )
    assert attached.get_companion("sui") == current
    commit_events = [
        event
        for event in attached.events()["events"]
        if event["type"] == "companion.transferred"
        and event["payload"]["transfer"]["transfer_id"] == transfer["transfer_id"]
    ]
    assert len(commit_events) == 1


def test_acknowledgments_validate_actor_even_after_commit(attached):
    transfer = prepare(attached)
    register(attached, "other")
    for node_id, phase in (
        ("desk", "ready"),
        ("phone", "released"),
        ("other", "ready"),
        ("other", "released"),
    ):
        assert_error(
            "acknowledgment_conflict",
            attached.ack_transfer,
            transfer["transfer_id"],
            {"node_id": node_id, "phase": phase},
        )
    attached.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})
    attached.ack_transfer(transfer["transfer_id"], {"node_id": "desk", "phase": "released"})
    for node_id, phase in (("desk", "ready"), ("phone", "released"), ("other", "ready")):
        assert_error(
            "acknowledgment_conflict",
            attached.ack_transfer,
            transfer["transfer_id"],
            {"node_id": node_id, "phase": phase},
        )


def test_same_node_and_duplicate_pending_requests_do_not_change_state(attached):
    state = attached.get_companion("sui")
    assert_error("same_node", prepare, attached, "same", "desk")
    assert attached.get_companion("sui") == state
    transfer = prepare(attached)
    assert_error("transfer_in_progress", prepare, attached, "duplicate")
    assert attached.get_transfer(transfer["transfer_id"])["status"] == "prepared"


def test_lease_expiry_alone_does_not_commit_without_target_ready(attached, clock):
    transfer = prepare(attached)
    clock.advance(10)
    assert attached.reconcile()["pending"] == 1
    assert attached.get_transfer(transfer["transfer_id"])["status"] == "prepared"
    assert attached.get_companion("sui")["active_node_id"] == "desk"
    assert (
        attached.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})[
            "status"
        ]
        == "committed"
    )


def test_offline_source_does_not_imply_release_and_graph_explains_recovery(tmp_path, clock):
    runtime = Coordinator(
        tmp_path / "offline.sqlite3", node_ttl_seconds=4, lease_ttl_seconds=10, clock=clock
    )
    initial = attach(runtime)
    clock.advance(5)
    runtime.heartbeat("phone", {})
    edge = next(edge for edge in runtime.graph()["edges"] if edge["source_node_id"] == "desk")
    assert edge["available"] and edge["reason"] == "source_offline_wait_for_lease"
    transfer = prepare(runtime)
    assert (
        runtime.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})[
            "status"
        ]
        == "prepared"
    )
    assert runtime.get_companion("sui")["lease_expires_at"] == initial["lease_expires_at"]
    clock.advance(3)
    runtime.heartbeat("phone", {})
    clock.advance(1.9)
    runtime.heartbeat("phone", {})
    assert runtime.get_transfer(transfer["transfer_id"])["status"] == "prepared"
    clock.advance(0.1)
    assert runtime.reconcile()["committed"] == [transfer["transfer_id"]]
    assert runtime.get_companion("sui")["active_node_id"] == "phone"


@pytest.mark.parametrize("released", [False, True])
def test_destination_offline_aborts_safely_and_unfreezes_source(tmp_path, clock, released):
    runtime = Coordinator(tmp_path / "unavailable.sqlite3", node_ttl_seconds=5, clock=clock)
    state = attach(runtime)
    transfer = prepare(runtime)
    if released:
        runtime.ack_transfer(transfer["transfer_id"], {"node_id": "desk", "phase": "released"})
    clock.advance(6)
    result = runtime.reconcile()
    assert result["aborted"] == [transfer["transfer_id"]]
    current = runtime.get_companion("sui")
    assert current["active_node_id"] == "desk"
    assert current["epoch"] == state["epoch"] and current["revision"] == state["revision"]
    if released:
        assert_error(
            "lease_expired",
            runtime.patch_context,
            "sui",
            context_payload(current, context={"resumed": True}),
        )
    else:
        assert current["lease_expires_at"] == state["lease_expires_at"]
    runtime.heartbeat("desk", {})
    lease = runtime.renew_lease("sui", {"node_id": "desk", "epoch": current["epoch"]})
    assert lease["epoch"] == current["epoch"]
    assert runtime.patch_context("sui", context_payload(current, context={"resumed": True}))[
        "context"
    ] == {"resumed": True}
    aborted = [event for event in runtime.events()["events"] if event["type"] == "transfer.aborted"]
    assert aborted[-1]["payload"]["reason"] == "target_offline"


def test_destination_capability_loss_aborts_immediately(attached):
    state = attached.get_companion("sui")
    transfer = prepare(attached)
    attached.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})
    register(attached, "phone", ["reading.context"])
    assert attached.get_transfer(transfer["transfer_id"])["status"] == "aborted"
    assert attached.get_companion("sui") == state
    assert_error("target_no_output", prepare, attached, "unsupported")
    edge = next(edge for edge in attached.graph()["edges"] if edge["source_node_id"] == "desk")
    assert not edge["available"] and edge["reason"] == "target_no_output"
    attached.renew_lease("sui", {"node_id": "desk", "epoch": state["epoch"]})


@pytest.mark.parametrize("refresh", ["heartbeat", "registration"])
def test_reconnected_target_cannot_reuse_readiness_from_offline_session(tmp_path, clock, refresh):
    runtime = Coordinator(tmp_path / "reconnect.sqlite3", node_ttl_seconds=5, clock=clock)
    attach(runtime)
    transfer = prepare(runtime)
    runtime.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})
    clock.advance(6)
    if refresh == "heartbeat":
        runtime.heartbeat("phone", {})
    else:
        register(runtime, "phone")
    assert runtime.get_transfer(transfer["transfer_id"])["status"] == "aborted"
    assert runtime.get_companion("sui")["active_node_id"] == "desk"
    assert runtime.get_node("phone")["online"]


def test_capability_removal_wins_over_expired_source_lease(attached, clock):
    state = attached.get_companion("sui")
    transfer = prepare(attached)
    attached.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})
    clock.advance(10)
    register(attached, "phone", ["reading.context"])
    assert attached.get_transfer(transfer["transfer_id"])["status"] == "aborted"
    assert attached.get_companion("sui") == state


def test_transfer_timeout_clears_freeze_and_terminal_ack_is_inert(attached, clock):
    state = attached.get_companion("sui")
    transfer = prepare(attached)
    clock.advance(30)
    assert attached.reconcile()["expired"] == [transfer["transfer_id"]]
    assert attached.get_transfer(transfer["transfer_id"])["status"] == "expired"
    lease = attached.renew_lease("sui", {"node_id": "desk", "epoch": state["epoch"]})
    assert lease["epoch"] == state["epoch"]
    current = attached.get_companion("sui")
    assert (
        attached.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})[
            "status"
        ]
        == "expired"
    )
    assert attached.get_companion("sui") == current
    assert attached.patch_context("sui", context_payload(current, context={"recovered": True}))[
        "context"
    ]["recovered"]


def test_context_merge_lease_expiry_and_stale_versions(attached, clock):
    state = attached.get_companion("sui")
    first = attached.patch_context(
        "sui", context_payload(state, context={"book": "夜航船", "paragraph": 5})
    )
    second = attached.patch_context(
        "sui", context_payload(first, "position", context={"paragraph": 6})
    )
    assert second["context"] == {"book": "夜航船", "paragraph": 6}
    assert_error(
        "revision_conflict",
        attached.patch_context,
        "sui",
        context_payload(first, "stale-revision", context={"paragraph": 7}),
    )
    stale_epoch = context_payload(second, "stale-epoch", context={"paragraph": 7})
    stale_epoch["epoch"] = 0
    assert_error("epoch_conflict", attached.patch_context, "sui", stale_epoch)
    clock.advance(10)
    assert_error(
        "lease_expired",
        attached.patch_context,
        "sui",
        context_payload(second, "expired", context={"paragraph": 7}),
    )
    attached.renew_lease("sui", {"node_id": "desk", "epoch": second["epoch"]})
    assert attached.get_companion("sui")["revision"] == second["revision"]
    assert (
        attached.patch_context("sui", context_payload(second, "expired", context={"paragraph": 7}))[
            "context"
        ]["paragraph"]
        == 7
    )


def test_previous_node_and_epoch_cannot_write_or_renew_after_transfer(attached):
    state = attached.get_companion("sui")
    transfer = prepare(attached)
    attached.ack_transfer(transfer["transfer_id"], {"node_id": "desk", "phase": "released"})
    attached.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})
    current = attached.get_companion("sui")
    stale = context_payload(state, "stale-owner", context={"x": 1})
    stale["expected_revision"] = current["revision"]
    assert_error("ownership_conflict", attached.patch_context, "sui", stale)
    assert_error(
        "ownership_conflict",
        attached.renew_lease,
        "sui",
        {"node_id": "desk", "epoch": state["epoch"]},
    )
    assert_error(
        "epoch_conflict", attached.renew_lease, "sui", {"node_id": "phone", "epoch": state["epoch"]}
    )


def test_command_replay_is_original_result_with_no_new_events(attached, clock):
    payload = context_payload(attached.get_companion("sui"), context={"chapter": 3})
    result = attached.patch_context("sui", payload)
    attached.patch_context("sui", context_payload(result, "later", context={"chapter": 4}))
    clock.advance(2)
    attached.renew_lease("sui", {"node_id": "desk", "epoch": result["epoch"]})
    cursor = attached.events()["next_sequence"]
    current = attached.get_companion("sui")
    assert attached.patch_context("sui", payload) == result
    assert attached.get_companion("sui") == current
    assert attached.events()["next_sequence"] == cursor
    changed = {**payload, "context": {"chapter": 5}}
    assert_error("idempotency_conflict", attached.patch_context, "sui", changed)
    assert_error(
        "idempotency_conflict",
        attached.observe,
        "desk",
        {"command_id": payload["command_id"], "modality": "reading", "data": {}},
    )
    assert attached.events()["next_sequence"] == cursor


def test_restart_recovers_prepared_transfer_and_keeps_replay_results(tmp_path, clock):
    path = tmp_path / "restart.sqlite3"
    first = Coordinator(path, clock=clock)
    state = attach(first)
    patched = first.patch_context("sui", context_payload(state, context={"book": "夜航船"}))
    transfer_payload = {
        "command_id": "to-phone",
        "target_node_id": "phone",
        "expected_revision": patched["revision"],
    }
    prepared = first.prepare_transfer("sui", transfer_payload)
    first.ack_transfer(prepared["transfer_id"], {"node_id": "phone", "phase": "ready"})
    cursor = first.events()["next_sequence"]
    clock.advance(10)
    second = Coordinator(path, clock=clock)
    current = second.get_companion("sui")
    assert current["active_node_id"] == "phone" and current["context"] == patched["context"]
    assert current["revision"] == patched["revision"] + 1
    assert second.get_transfer(prepared["transfer_id"])["status"] == "committed"
    assert second.events(after_sequence=cursor)["events"][0]["type"] == "companion.transferred"
    assert second.prepare_transfer("sui", transfer_payload) == prepared
    assert (
        second.patch_context("sui", context_payload(state, context={"book": "夜航船"})) == patched
    )
    clock.advance(10)
    expired_state = second.get_companion("sui")
    assert second.prepare_transfer("sui", transfer_payload) == prepared
    assert (
        second.ack_transfer(prepared["transfer_id"], {"node_id": "desk", "phase": "released"})[
            "status"
        ]
        == "committed"
    )
    assert second.get_companion("sui") == expired_state
    assert_error(
        "lease_expired",
        second.patch_context,
        "sui",
        context_payload(expired_state, "after-replay", context={"new": True}),
    )


def test_old_terminal_ack_cannot_touch_a_later_owner(attached, clock):
    first = prepare(attached)
    attached.ack_transfer(first["transfer_id"], {"node_id": "desk", "phase": "released"})
    attached.ack_transfer(first["transfer_id"], {"node_id": "phone", "phase": "ready"})
    clock.advance(1)
    second = prepare(attached, "back", "desk")
    attached.ack_transfer(second["transfer_id"], {"node_id": "phone", "phase": "released"})
    attached.ack_transfer(second["transfer_id"], {"node_id": "desk", "phase": "ready"})
    state = attached.get_companion("sui")
    attached.ack_transfer(first["transfer_id"], {"node_id": "desk", "phase": "released"})
    attached.abort_transfer(first["transfer_id"])
    assert attached.get_companion("sui") == state


def test_manual_abort_preserves_source_and_prepared_snapshot(attached):
    state = attached.get_companion("sui")
    transfer = prepare(attached)
    result = attached.abort_transfer(transfer["transfer_id"])
    assert result["status"] == "aborted" and result["snapshot"] == state
    assert attached.get_companion("sui") == state
    assert attached.abort_transfer(transfer["transfer_id"]) == result
    assert attached.patch_context("sui", context_payload(state, context={"continuing": True}))[
        "context"
    ]["continuing"]


def test_concurrent_stale_context_writes_have_one_winner(attached, clock):
    second = Coordinator(attached.store.database_path, clock=clock)
    state = attached.get_companion("sui")
    barrier = Barrier(2)

    def change(runtime, value):
        barrier.wait(timeout=5)
        try:
            return runtime.patch_context(
                "sui", context_payload(state, f"parallel-{value}", context={"paragraph": value})
            )
        except DomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(change, attached, 1), executor.submit(change, second, 2)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    failures = [result for result in results if isinstance(result, DomainError)]
    assert failures[0].code == "revision_conflict"
    assert attached.get_companion("sui")["revision"] == state["revision"] + 1
    assert (
        len(
            [
                event
                for event in attached.events()["events"]
                if event["type"] == "companion.context_updated"
            ]
        )
        == 1
    )


def test_concurrent_identical_command_has_one_durable_effect(attached, clock):
    second = Coordinator(attached.store.database_path, clock=clock)
    payload = context_payload(attached.get_companion("sui"), context={"shared": True})
    barrier = Barrier(2)

    def change(runtime):
        barrier.wait(timeout=5)
        return runtime.patch_context("sui", payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(change, runtime) for runtime in (attached, second)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    assert (
        len(
            [
                event
                for event in attached.events()["events"]
                if event["type"] == "companion.context_updated"
            ]
        )
        == 1
    )


def test_concurrent_transfer_preparations_and_acks_are_serialized(attached, clock):
    second = Coordinator(attached.store.database_path, clock=clock)
    barrier = Barrier(2)

    def begin(runtime, command):
        barrier.wait(timeout=5)
        try:
            return prepare(runtime, command)
        except DomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(begin, attached, "one"), executor.submit(begin, second, "two")]
        results = [future.result(timeout=10) for future in futures]
    transfers = [result for result in results if isinstance(result, dict)]
    assert len(transfers) == 1
    assert (
        next(result for result in results if isinstance(result, DomainError)).code
        == "transfer_in_progress"
    )
    transfer_id = transfers[0]["transfer_id"]
    barrier = Barrier(2)

    def acknowledge(runtime, node_id, phase):
        barrier.wait(timeout=5)
        return runtime.ack_transfer(transfer_id, {"node_id": node_id, "phase": phase})

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(acknowledge, attached, "desk", "released"),
            executor.submit(acknowledge, second, "phone", "ready"),
        ]
        results = [future.result(timeout=10) for future in futures]
    assert "committed" in {result["status"] for result in results}
    assert attached.get_companion("sui")["revision"] == 2
    assert (
        len(
            [
                event
                for event in attached.events()["events"]
                if event["type"] == "companion.transferred"
                and event["payload"]["transfer"]["transfer_id"] == transfer_id
            ]
        )
        == 1
    )


def test_event_failure_rolls_back_state_command_and_event(attached, monkeypatch):
    state = attached.get_companion("sui")
    cursor = attached.events()["next_sequence"]
    payload = context_payload(state, context={"atomic": True})
    original = attached._emit

    def fail_after_event(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(attached, "_emit", fail_after_event)
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        attached.patch_context("sui", payload)
    assert attached.get_companion("sui") == state
    assert attached.events()["next_sequence"] == cursor
    monkeypatch.setattr(attached, "_emit", original)
    assert attached.patch_context("sui", payload)["revision"] == state["revision"] + 1


def test_filtered_events_advance_over_each_scanned_row(attached):
    register(attached, "sensor", ["vision.capture"])
    cursor = attached.events()["next_sequence"]
    transfer = prepare(attached)
    first = attached.events(after_sequence=cursor, node_id="sensor", limit=1)
    assert first["events"] == [] and first["next_sequence"] > cursor
    second = attached.events(after_sequence=first["next_sequence"], node_id="sensor", limit=1)
    assert second["events"] == [] and second["next_sequence"] > first["next_sequence"]
    assert attached.events(after_sequence=second["next_sequence"], node_id="sensor", limit=1) == {
        "events": [],
        "next_sequence": second["next_sequence"],
    }
    target = attached.events(after_sequence=cursor, node_id="phone")
    assert len(target["events"]) == 1
    assert target["events"][0]["payload"] == {"transfer": transfer, "role": "target"}
    source = attached.events(after_sequence=cursor, node_id="desk")
    assert source["events"][0]["payload"]["role"] == "source"


def test_observation_metadata_vision_gating_and_dedup(runtime):
    register(runtime, "phone")
    payload = {
        "command_id": "vision",
        "modality": "vision",
        "data": {"subject": "植物"},
        "user_initiated": True,
    }
    assert_error("capability_required", runtime.observe, "phone", payload)
    register(runtime, "phone", ["text.display", "vision.capture"])
    assert_error(
        "user_initiated_required", runtime.observe, "phone", {**payload, "user_initiated": False}
    )
    result = runtime.observe("phone", payload)
    assert result["type"] == "node.observation" and result["node_id"] == "phone"
    assert result["payload"]["data"] == {"subject": "植物"}
    assert result["companion_id"] is None
    cursor = runtime.events()["next_sequence"]
    assert runtime.observe("phone", payload) == result
    assert runtime.events()["next_sequence"] == cursor
    assert_error(
        "idempotency_conflict", runtime.observe, "phone", {**payload, "data": {"subject": "书"}}
    )


def test_routing_policy_is_shared_by_graph_prepare_and_commit(tmp_path, clock):
    allowed = True

    def policy(source, target):
        if source is not None and target["node_id"] == "phone" and not allowed:
            return False, "topology_denied"
        return True, "available"

    runtime = Coordinator(tmp_path / "policy.sqlite3", clock=clock, routing_policy=policy)
    attach(runtime)
    allowed = False
    edge = next(edge for edge in runtime.graph()["edges"] if edge["source_node_id"] == "desk")
    assert not edge["available"] and edge["reason"] == "topology_denied"
    assert_error("topology_denied", prepare, runtime, "denied")
    allowed = True
    transfer = prepare(runtime)
    runtime.ack_transfer(transfer["transfer_id"], {"node_id": "phone", "phase": "ready"})
    allowed = False
    assert runtime.reconcile()["aborted"] == [transfer["transfer_id"]]
    assert runtime.get_companion("sui")["active_node_id"] == "desk"


def test_policy_cannot_allow_an_offline_or_incapable_target(tmp_path, clock):
    runtime = Coordinator(
        tmp_path / "policy-safety.sqlite3",
        clock=clock,
        routing_policy=lambda source, target: (True, "available"),
    )
    attach(runtime)
    register(runtime, "phone", [])
    assert_error("target_no_output", prepare, runtime)
    register(runtime, "phone")
    clock.advance(60)
    assert_error("target_offline", prepare, runtime)


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", True),
        ("expected_revision", "1"),
        ("epoch", -1),
        ("epoch", 2**63),
        ("context", None),
        ("context", {"x": float("nan")}),
        ("context", {"x": "字" * 30_000}),
        ("command_id", " " * 2),
    ],
)
def test_payload_and_version_validation(attached, field, value):
    payload = context_payload(attached.get_companion("sui"), context={"x": 1})
    payload[field] = value
    assert_error("validation_error", attached.patch_context, "sui", payload)


def test_merged_context_cannot_grow_past_payload_bound(attached):
    state = attached.patch_context(
        "sui", context_payload(attached.get_companion("sui"), context={"one": "x" * 40_000})
    )
    assert_error(
        "validation_error",
        attached.patch_context,
        "sui",
        context_payload(state, "oversize-merge", context={"two": "x" * 40_000}),
    )
    assert attached.get_companion("sui") == state


def test_missing_resources_and_event_validation(runtime):
    assert_error("not_found", runtime.get_node, "missing")
    assert_error("not_found", runtime.get_companion, "missing")
    assert_error("not_found", runtime.get_transfer, "missing")
    assert_error("not_found", runtime.events, node_id="missing")
    for kwargs in (
        {"after_sequence": -1},
        {"after_sequence": True},
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
    ):
        assert_error("validation_error", runtime.events, **kwargs)
