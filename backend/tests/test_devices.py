"""Device identity, trusted LAN hints and exclusive expiring Web access."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_coordinator import Clock
from virtual_life.coordinator import Coordinator, DomainError


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def runtime(tmp_path, clock):
    return Coordinator(tmp_path / "devices.sqlite", clock=clock)


def register(runtime, node_id="desk", *, peer_ip=None, kind="desktop", device=None):
    return runtime.register_node(
        {
            "node_id": node_id,
            "label": node_id,
            "kind": kind,
            "capabilities": ["audio.output", "reading.context"],
            "device": device,
        },
        peer_ip=peer_ip,
    )


def web(runtime, browser="browser", access="access", *, peer_ip=None, **fields):
    return runtime.devices.open_web(
        {"browser_id": browser, "access_id": access, **fields}, peer_ip=peer_ip
    )


def error(code, action, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        action(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def test_web_reopen_reuses_device_browser_mapping_across_ips_and_restart(runtime, clock):
    first = web(runtime, peer_ip="192.168.1.12")
    node_id = first["node"]["node_id"]
    assert first["resolution"] == "created"
    assert first["node"]["label"] == "本机设备"
    assert first["node"]["kind"] == "other"
    assert first["node"]["capabilities"] == ["text.display"]
    assert web(runtime, peer_ip="192.168.1.12")["node"]["node_id"] == node_id
    runtime.devices.release(node_id, "access")
    restarted = Coordinator(runtime.store.database_path, clock=clock)
    reopened = web(restarted, access="new", peer_ip="10.20.1.40")
    assert reopened["resolution"] == "browser_id"
    assert reopened["node"]["node_id"] == node_id
    assert len(restarted.list_nodes()) == 1
    assert reopened["node"]["device"]["ip_addresses"] == ["10.20.1.40", "192.168.1.12"]


def test_lan_shares_device_between_browsers_and_native_registration(runtime):
    register(runtime, peer_ip="192.168.1.12")
    first = web(runtime, peer_ip="::ffff:192.168.1.12")
    assert first["resolution"] == "lan_ip"
    assert first["node"]["node_id"] == "desk"
    runtime.devices.release("desk", "access")
    other = web(runtime, "other-browser", "other-access", peer_ip="192.168.1.12")
    assert other["resolution"] == "lan_ip"
    assert other["node"]["node_id"] == "desk"
    assert len(runtime.list_nodes()) == 1


@pytest.mark.parametrize(
    "address",
    [
        None,
        "8.8.8.8",
        "127.0.0.1",
        "169.254.1.2",
        "::1",
        "fe80::1",
        "ff02::1",
        "0.0.0.0",
        "::",
        "invalid",
    ],
)
def test_untrusted_addresses_do_not_merge_devices(runtime, address):
    register(runtime, peer_ip=address)
    first = web(runtime, peer_ip=address)
    assert first["resolution"] == "created"
    assert first["node"]["node_id"] != "desk"
    if address in (None, "0.0.0.0", "::", "ff02::1", "invalid"):
        assert first["node"]["device"]["ip_addresses"] == []
    else:
        assert first["node"]["device"]["ip_addresses"] == [address]
    runtime.devices.release(first["node"]["node_id"], "access")
    second = web(runtime, "different", "different", peer_ip=address)
    assert second["node"]["node_id"] != first["node"]["node_id"]


def test_ula_is_allowed_but_peer_none_does_not_infer_proxy_identity(runtime):
    register(runtime, peer_ip="fd12:3456::12")
    assert web(runtime, peer_ip="fd12:3456::12")["node"]["node_id"] == "desk"
    assert web(runtime, "proxied", "proxied", peer_ip=None)["resolution"] == "created"


def test_shared_lan_ip_requires_explicit_binding_and_rolls_back(runtime):
    register(runtime, "desk", peer_ip="10.1.1.1")
    register(runtime, "phone", peer_ip="10.1.1.1", kind="android")
    error("device_ambiguous", web, runtime, peer_ip="10.1.1.1")
    assert len(runtime.list_nodes()) == 2
    with runtime.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM device_browsers").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM device_access_ids").fetchone()[0] == 0
    bound = web(runtime, peer_ip="10.1.1.1", bind_node_id="desk")
    assert bound["resolution"] == "bound"
    assert bound["node"]["node_id"] == "desk"


def test_ip_candidates_expire_and_native_heartbeat_refreshes(runtime, clock):
    register(runtime, peer_ip="10.1.1.1")
    clock.advance(61)
    stale = web(runtime, peer_ip="10.1.1.1")
    assert stale["resolution"] == "created"
    runtime.devices.release(stale["node"]["node_id"], "access")
    clock.advance(61)
    runtime.heartbeat("desk", {}, peer_ip="10.1.1.1")
    assert web(runtime, "fresh", "fresh", peer_ip="10.1.1.1")["node"]["node_id"] == "desk"


def test_gateway_other_and_legacy_web_do_not_record_native_peer_ip(runtime):
    register(runtime, "gateway", kind="other", peer_ip="10.1.1.1")
    assert runtime.get_node("gateway")["device"]["ip_addresses"] == []
    register(
        runtime, "identified", kind="other", peer_ip="10.1.1.2", device={"device_id": "hardware"}
    )
    assert web(runtime, peer_ip="10.1.1.2")["node"]["node_id"] == "identified"
    runtime.heartbeat("gateway", {}, peer_ip="10.1.1.3")
    assert runtime.get_node("gateway")["device"]["ip_addresses"] == []


def test_mac_normalization_additive_identity_and_conflicts(runtime):
    first = register(
        runtime, device={"device_id": "hardware", "mac_addresses": ["02-AA-BB-CC-DD-EE"]}
    )
    assert first["device"]["mac_addresses"] == ["02:aa:bb:cc:dd:ee"]
    register(runtime, device={"mac_addresses": ["02:AA:BB:CC:DD:FF"]})
    assert runtime.get_node("desk")["device"]["device_id"] == "hardware"
    assert runtime.get_node("desk")["device"]["mac_addresses"] == [
        "02:aa:bb:cc:dd:ee",
        "02:aa:bb:cc:dd:ff",
    ]
    caught = error(
        "device_already_registered",
        register,
        runtime,
        "duplicate",
        device={"mac_addresses": ["02:aa:bb:cc:dd:ee"]},
    )
    assert "desk" in caught.message
    assert [node["node_id"] for node in runtime.list_nodes()] == ["desk"]
    register(runtime, "phone", device={"device_id": "phone-hardware"})
    error(
        "device_identity_conflict",
        register,
        runtime,
        "conflict",
        device={"device_id": "phone-hardware", "mac_addresses": ["02:aa:bb:cc:dd:ee"]},
    )
    error("device_already_registered", register, runtime, device={"device_id": "phone-hardware"})
    assert runtime.get_node("desk")["device"]["device_id"] == "hardware"


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"mac_addresses": ["00:00:00:00:00:00"]},
        {"mac_addresses": ["ff:ff:ff:ff:ff:ff"]},
        {"mac_addresses": ["01:00:5e:01:01:01"]},
        {"mac_addresses": ["bad"]},
    ],
)
def test_invalid_device_identity_rejected_before_node_mutation(runtime, identity):
    error("validation_error", register, runtime, device=identity)
    assert runtime.list_nodes() == []


def test_exclusive_access_expiry_and_old_release_cannot_close_new(runtime, clock):
    first = web(runtime)
    node_id = first["node"]["node_id"]
    error("device_access_busy", web, runtime, access="second")
    error("device_access_busy", web, runtime, "other", "other", bind_node_id=node_id)
    clock.advance(60)
    error("node_access_expired", runtime.devices.heartbeat, node_id, "access", {})
    error("node_access_expired", web, runtime)
    second = web(runtime, access="second")
    assert second["node"]["node_id"] == node_id
    assert runtime.devices.release(node_id, "access")["accesses"][0]["access_id"] == "second"
    assert runtime.devices.release(node_id, "access")["accesses"][0]["access_id"] == "second"
    runtime.devices.release(node_id, "second")
    error("node_access_expired", web, runtime, access="second")
    assert runtime.get_node(node_id)["online"] is False
    assert runtime.get_node(node_id)["capabilities"] == []


def test_access_cannot_rebind_or_validate_wrong_device(runtime):
    register(runtime)
    register(runtime, "phone")
    web(runtime, bind_node_id="desk")
    error("device_identity_conflict", web, runtime, bind_node_id="phone")
    error("device_identity_conflict", web, runtime, "other", "access", bind_node_id="desk")
    error("node_access_expired", runtime.devices.heartbeat, "phone", "access", {})
    error("node_access_expired", runtime.devices.heartbeat, "desk", "unknown", {})
    runtime.devices.release("phone", "access")
    assert runtime.get_node("desk")["accesses"][0]["access_id"] == "access"
    runtime.devices.release("desk", "access")
    assert web(runtime, access="new", bind_node_id="phone")["node"]["node_id"] == "phone"
    runtime.devices.release("phone", "new")
    assert web(runtime, access="third")["node"]["node_id"] == "phone"


def test_web_does_not_replace_native_context_identity_or_capabilities(runtime, clock):
    register(runtime, device={"device_id": "native-hardware"})
    runtime.heartbeat("desk", {"context": {"native": "kept"}})
    web(runtime, bind_node_id="desk")
    clock.advance(40)
    result = runtime.devices.heartbeat("desk", "access", {"context": {"page": "private"}})
    assert result["node"]["context"] == {"native": "kept"}
    assert result["node"]["capabilities"] == ["audio.output", "reading.context", "text.display"]
    assert result["node"]["device"]["device_id"] == "native-hardware"
    with runtime.store.transaction() as connection:
        row = connection.execute("SELECT * FROM nodes WHERE node_id='desk'").fetchone()
        assert row["context_json"] == '{"native":"kept"}'
        assert "text.display" not in row["capabilities_json"]
    clock.advance(21)
    node = runtime.get_node("desk")
    assert node["online"]
    assert node["capabilities"] == ["text.display"]
    assert node["last_seen_at"] == result["node"]["last_seen_at"]
    runtime.devices.release("desk", "access")
    assert not runtime.get_node("desk")["online"]
    runtime.heartbeat("desk", {})
    assert runtime.get_node("desk")["capabilities"] == ["audio.output", "reading.context"]


def test_release_never_removes_native_text_capability(runtime):
    runtime.register_node(
        {"node_id": "desk", "label": "desktop", "kind": "desktop", "capabilities": ["text.display"]}
    )
    web(runtime, bind_node_id="desk")
    node = runtime.devices.release("desk", "access")
    assert node["online"]
    assert node["capabilities"] == ["text.display"]


def test_access_history_is_bounded_and_pruned_ids_never_reused(runtime):
    first = web(runtime)
    node_id = first["node"]["node_id"]
    runtime.devices.release(node_id, "access")
    for index in range(70):
        access_id = f"access-{index}"
        web(runtime, access=access_id)
        runtime.devices.release(node_id, access_id)
    with runtime.store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_web_accesses WHERE node_id=?", (node_id,)
            ).fetchone()[0]
            == 64
        )
    error("node_access_expired", web, runtime)
    assert len(runtime.list_nodes()) == 1


def test_access_updates_rollback_on_failure(runtime, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(runtime, "_emit", fail)
    with pytest.raises(RuntimeError, match="injected"):
        web(runtime, peer_ip="10.1.1.2")
    assert runtime.list_nodes() == []
    with runtime.store.transaction() as connection:
        for table in ("device_browsers", "device_web_accesses", "device_access_ids", "device_ips"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_concurrent_open_only_one_access_wins(runtime):
    register(runtime)

    def attempt(access_id):
        try:
            return web(runtime, access=access_id, bind_node_id="desk")
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["first", "second"]))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert "device_access_busy" in outcomes
    assert len(runtime.get_node("desk")["accesses"]) == 1


def test_legacy_native_and_web_rows_keep_stable_ids_without_merging(runtime, clock):
    register(runtime)
    timestamp = clock.value.isoformat()
    with runtime.store.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO nodes(node_id,label,kind,capabilities_json,context_json,created_at,last_seen_at)"
            " VALUES ('old-web','Old web','web','[\"text.display\"]','{}',?,?)",
            (timestamp, timestamp),
        )
    legacy = register(runtime, "old-web", kind="web", peer_ip="10.1.1.1")
    assert legacy["node_id"] == "old-web"
    assert legacy["device"]["ip_addresses"] == []
    error("web_access_required", register, runtime, "new-web", kind="web")
    assert runtime.get_node("desk")["node_id"] == "desk"
    assert len(runtime.list_nodes()) == 2


def test_live_open_retry_refreshes_ttl_without_duplicating_access_or_event(runtime, clock):
    first = web(runtime)
    clock.advance(40)
    retry = web(runtime, peer_ip="10.1.1.9")
    assert retry["node"]["node_id"] == first["node"]["node_id"]
    assert retry["expires_at"] > first["expires_at"]
    assert retry["node"]["last_seen_at"] > first["node"]["last_seen_at"]
    with runtime.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM device_web_accesses").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE type='node.web_access_opened'"
            ).fetchone()[0]
            == 1
        )
    clock.advance(30)
    assert runtime.get_node(first["node"]["node_id"])["online"]


def prepare_ready_web_target(runtime):
    register(runtime)
    runtime.create_companion({"companion_id": "sui", "name": "随"})
    initial = runtime.prepare_transfer(
        "sui", {"command_id": "initial", "expected_revision": 0, "target_node_id": "desk"}
    )
    runtime.ack_transfer(initial["transfer_id"], {"node_id": "desk", "phase": "ready"})
    target = web(runtime)
    state = runtime.get_companion("sui")
    transfer = runtime.prepare_transfer(
        "sui",
        {
            "command_id": "next",
            "expected_revision": state["revision"],
            "target_node_id": target["node"]["node_id"],
        },
    )
    assert (
        runtime.ack_transfer(
            transfer["transfer_id"], {"node_id": target["node"]["node_id"], "phase": "ready"}
        )["status"]
        == "prepared"
    )
    return target["node"]["node_id"], transfer["transfer_id"]


def test_reopening_expired_web_target_cannot_reuse_old_ready_ack(runtime, clock):
    runtime.lease_ttl_seconds = 120
    runtime.transfer_ttl_seconds = 300
    target, transfer_id = prepare_ready_web_target(runtime)
    clock.advance(61)
    reopened = web(runtime, access="new")
    assert reopened["node"]["node_id"] == target
    assert reopened["node"]["online"]
    assert runtime.get_transfer(transfer_id)["status"] == "aborted"
    aborted = [
        event
        for event in runtime.events(limit=500)["events"]
        if event["type"] == "transfer.aborted"
    ]
    assert aborted[-1]["payload"]["reason"] == "target_offline"
    assert runtime.get_companion("sui")["active_node_id"] == "desk"


def test_web_target_release_immediately_aborts_prepared_travel(runtime):
    target, transfer_id = prepare_ready_web_target(runtime)
    runtime.devices.release(target, "access")
    assert runtime.get_transfer(transfer_id)["status"] == "aborted"
    aborted = [
        event
        for event in runtime.events(limit=500)["events"]
        if event["type"] == "transfer.aborted"
    ]
    assert aborted[-1]["payload"]["reason"] == "target_offline"
    assert runtime.get_companion("sui")["active_node_id"] == "desk"


def test_native_registration_identity_and_ip_roll_back_together(runtime, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(runtime, "_emit", fail)
    with pytest.raises(RuntimeError, match="injected"):
        register(runtime, peer_ip="10.1.1.2", device={"device_id": "hardware"})
    assert runtime.list_nodes() == []
    with runtime.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM device_identity_keys").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM device_ips").fetchone()[0] == 0


def test_body_ip_not_accepted_and_access_reads_are_write_free(runtime):
    error(
        "validation_error",
        runtime.devices.open_web,
        {"browser_id": "browser", "access_id": "access", "peer_ip": "10.1.1.2"},
    )
    opened = web(runtime)
    with runtime.store.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM nodes WHERE node_id=?", (opened["node"]["node_id"],)
        ).fetchone()
        before = connection.total_changes
        runtime._node(row, runtime._now(), connection)
        runtime.devices.validate_access(
            connection, opened["node"]["node_id"], "access", runtime._now()
        )
        assert connection.total_changes == before
