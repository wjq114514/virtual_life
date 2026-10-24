"""Scoped reporter credentials cannot read, seize identities, or survive revocation."""

import hashlib

import pytest
from virtual_life.coordinator import Coordinator, DomainError
from virtual_life.node_credentials import NodeCredentialService


@pytest.fixture
def service(tmp_path):
    return NodeCredentialService(Coordinator(tmp_path / "credentials.sqlite"))


def registration(node_id="linux", **changes):
    return {
        "node_id": node_id,
        "label": "Linux",
        "kind": "other",
        "capabilities": ["device.state.report"],
        "device": {"device_id": node_id},
        **changes,
    }


def forbidden(action, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        action(*args, **kwargs)
    assert caught.value.status_code == 403
    assert caught.value.code == "forbidden"
    assert str(caught.value) == "Reporter credential access denied"


def registered(service):
    token = service.issue("linux", "deploy")["token"]
    body = registration()
    assert service.authorize(token, "POST", "/v1/nodes", body)
    service.coordinator.register_node(body)
    return token


def test_reporter_lifecycle_and_storage(service):
    token = registered(service)
    assert service.authorize(token, "POST", "/v1/nodes/linux/heartbeat", {"context": {}})
    observation = {"data": {"subject_node_id": "linux", "observer_node_id": "linux"}}
    assert service.authorize(token, "POST", "/v1/nodes/linux/observations", observation)
    with service.store.transaction() as connection:
        row = connection.execute("SELECT * FROM node_credentials").fetchone()
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(dict(row))
    assert token.encode() not in open(service.store.database_path, "rb").read()
    reopened = NodeCredentialService(Coordinator(service.store.database_path))
    assert reopened.authorize(token, "POST", "/v1/nodes", registration())
    assert not reopened.is_revoked("deploy")
    assert not reopened.is_revoked("missing")
    reopened.revoke_for_deployment("deploy")
    assert reopened.is_revoked("deploy")
    forbidden(service.authorize, token, "POST", "/v1/nodes", registration())
    reopened_again = NodeCredentialService(Coordinator(service.store.database_path))
    forbidden(reopened_again.authorize, token, "POST", "/v1/nodes", registration())


@pytest.mark.parametrize(
    "changes",
    [
        {"node_id": "other"},
        {"kind": "desktop"},
        {"capabilities": ["device.state.report", "avatar.render"]},
        {"capabilities": ["device.state.report", "device.state.observe"]},
        {"capabilities": ["device.state.report", "device.state.report"]},
        {"device": {"device_id": "other"}},
        {"device": None},
        {"device": {"device_id": "linux", "mac_addresses": ["02:11:22:33:44:55"]}},
    ],
)
def test_registration_scope(service, changes):
    token = service.issue("linux", "deploy")["token"]
    forbidden(service.authorize, token, "POST", "/v1/nodes", registration(**changes))


@pytest.mark.parametrize(
    "path",
    [
        "/v1/nodes",
        "/v1/nodes/other/heartbeat",
        "/v1/nodes/linux/events",
        "/v1/nodes/linux/heartbeat/",
        "/v1/nodes/%6cinux/heartbeat",
        "/v1/nodes/linux%2Fother/heartbeat",
        "/v1/nodes/linux/../other/heartbeat",
        "/v1/nodes/linux/heartbeat?x=1",
        "/v1/companions",
        "/v1/models",
        "/v1/nodes/linux/ws",
        "/v1/memory",
    ],
)
def test_routes_are_exact_and_write_only(service, path):
    token = registered(service)
    forbidden(service.authorize, token, "POST", path, {})
    forbidden(service.authorize, token, "GET", "/v1/nodes", {})


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"subject_node_id": "linux", "observer_node_id": "other"},
        {"subject_node_id": "other", "observer_node_id": "linux"},
        {"subject_node_id": "linux"},
        None,
    ],
)
def test_observation_targets(service, data):
    token = registered(service)
    forbidden(service.authorize, token, "POST", "/v1/nodes/linux/observations", {"data": data})


def test_cannot_claim_existing_identity_or_presentation_node(service):
    runtime = service.coordinator
    runtime.register_node(registration(kind="desktop", capabilities=["avatar.render"]))
    forbidden(service.issue, "linux", "deploy")
    runtime.register_node(registration("another", device={"device_id": "reserved"}))
    forbidden(service.issue, "reserved", "deploy")
    runtime.register_node(registration("unbound", device=None))
    forbidden(service.issue, "unbound", "deploy")
    web_node = runtime.devices.open_web({"browser_id": "browser", "access_id": "access"})
    forbidden(service.issue, web_node["node"]["node_id"], "deploy")


def test_preissued_credential_cannot_seize_subsequently_created_node(service):
    token = service.issue("linux", "deploy")["token"]
    service.coordinator.register_node(registration(kind="android", capabilities=["avatar.render"]))
    forbidden(service.authorize, token, "POST", "/v1/nodes", registration())


def test_transaction_guard_rechecks_revocation_and_identity(service):
    token = registered(service)
    with service.mutation_guard(token, "POST", "/v1/nodes", registration()):
        service.revoke_for_deployment("deploy")
        with service.store.transaction(write=True) as connection:
            forbidden(service.guard_mutation, connection)
    # Context cleanup preserves unscoped administrator writes.
    with service.store.transaction(write=True) as connection:
        service.guard_mutation(connection)


@pytest.mark.parametrize("token", [None, "", "admin", "vl_reporter_invalid", 2])
def test_invalid_tokens_use_fixed_error(service, token):
    forbidden(service.authorize, token, "POST", "/v1/nodes", registration())


@pytest.mark.parametrize("node_id", ["../linux", "linux/other", "linux%2fother", " linux", ""])
def test_unsafe_node_ids_are_rejected(service, node_id):
    forbidden(service.issue, node_id, "deploy")


def test_reported_is_credential_specific_and_requires_committed_heartbeat(service):
    from virtual_life.node_credentials import record_reported

    first = service.issue("linux", "old")
    second = service.issue("linux", "new")
    service.coordinator.register_node(registration())
    assert not service.has_reported(first["credential_id"])
    with service.mutation_guard(first["token"], "POST", "/v1/nodes", registration()):
        with service.store.transaction(write=True) as connection:
            record_reported(connection)
    assert not service.has_reported(first["credential_id"])
    with service.mutation_guard(first["token"], "POST", "/v1/nodes/linux/heartbeat", {}):
        with pytest.raises(RuntimeError), service.store.transaction(write=True) as connection:
            record_reported(connection)
            raise RuntimeError("rollback")
    assert not service.has_reported(first["credential_id"])
    with service.mutation_guard(first["token"], "POST", "/v1/nodes/linux/heartbeat", {}):
        with service.store.transaction(write=True) as connection:
            record_reported(connection)
    assert service.has_reported(first["credential_id"])
    assert not service.has_reported(second["credential_id"])
    reopened = NodeCredentialService(Coordinator(service.store.database_path))
    assert reopened.has_reported(first["credential_id"])
    service.revoke_for_deployment("old")
    assert not service.has_reported(first["credential_id"])
    assert not service.has_reported("unknown")


def test_reported_schema_migrates_existing_credentials(tmp_path):
    runtime = Coordinator(tmp_path / "old.sqlite")
    with runtime.store.transaction(write=True) as connection:
        connection.execute("""CREATE TABLE node_credentials (
            credential_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
            node_id TEXT NOT NULL, deployment_id TEXT NOT NULL,
            created_at TEXT NOT NULL, revoked_at TEXT)""")
        connection.execute(
            "INSERT INTO node_credentials VALUES ('old',?,'linux','d','t',NULL)",
            (hashlib.sha256(("vl_reporter_" + "A" * 43).encode()).hexdigest(),),
        )
    service = NodeCredentialService(runtime)
    assert not service.has_reported("old")
    with service.store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT attention_enabled FROM node_credentials WHERE credential_id='old'"
            ).fetchone()["attention_enabled"]
            == 0
        )
    old_token = "vl_reporter_" + "A" * 43
    assert service.authorize(old_token, "POST", "/v1/nodes", registration())
    forbidden(
        service.authorize,
        old_token,
        "POST",
        "/v1/nodes",
        registration(capabilities=["device.state.report", "attention.observe"]),
    )
    assert service.issue("new", "deploy")["token"]


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_attention_grant_requires_bool(service, value):
    forbidden(service.issue, "linux", "deploy", attention_enabled=value)


@pytest.mark.parametrize("original_enabled", [False, True])
@pytest.mark.parametrize("replacement_enabled", [False, True])
def test_attention_grants_stay_fixed_and_old_installation_can_rollback(
    service, original_enabled, replacement_enabled
):
    def body(enabled):
        return registration(
            capabilities=["device.state.report", "attention.observe"]
            if enabled
            else ["device.state.report"]
        )

    original = service.issue("linux", "old", attention_enabled=original_enabled)["token"]
    service.coordinator.register_node(body(original_enabled))
    replacement = service.issue("linux", "new", attention_enabled=replacement_enabled)["token"]
    assert service.authorize(replacement, "POST", "/v1/nodes", body(replacement_enabled))
    service.coordinator.register_node(body(replacement_enabled))
    # A failed upgrade/downgrade restores the previous config and credential.
    assert service.authorize(original, "POST", "/v1/nodes", body(original_enabled))
    service.coordinator.register_node(body(original_enabled))
    assert service.authorize(original, "POST", "/v1/nodes/linux/heartbeat", {})
    for token, enabled in ((original, original_enabled), (replacement, replacement_enabled)):
        forbidden(service.authorize, token, "POST", "/v1/nodes", body(not enabled))
        if enabled:
            assert service.authorize(token, "POST", "/v1/nodes/linux/attention", {})
        else:
            forbidden(service.authorize, token, "POST", "/v1/nodes/linux/attention", {})


def test_attention_grant_persists_and_revokes(service):
    token = service.issue("linux", "enabled", attention_enabled=True)["token"]
    body = registration(capabilities=["device.state.report", "attention.observe"])
    service.coordinator.register_node(body)
    reopened = NodeCredentialService(Coordinator(service.store.database_path))
    assert reopened.authorize(token, "POST", "/v1/nodes", body)
    for method, path in [
        ("GET", "/v1/nodes/linux/attention"),
        ("POST", "/v1/nodes/other/attention"),
        ("POST", "/v1/nodes/%6cinux/attention"),
        ("POST", "/v1/companions"),
    ]:
        forbidden(reopened.authorize, token, method, path, {})
    forbidden(
        reopened.authorize,
        token,
        "POST",
        "/v1/nodes",
        registration(capabilities=["attention.observe", "device.state.report"]),
    )
    with reopened.mutation_guard(token, "POST", "/v1/nodes/linux/attention", {}):
        reopened.revoke_for_deployment("enabled")
        with reopened.store.transaction(write=True) as connection:
            forbidden(reopened.guard_mutation, connection)
