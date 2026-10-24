"""Deployment transactions and SSH staging use isolated stores and fake remote commands."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from virtual_life.coordinator import Coordinator, DomainError
from virtual_life.linux_deployment import (
    LinuxDeploymentRequest,
    LinuxDeploymentService,
    SSHSession,
    fail,
)
from virtual_life.node_credentials import NodeCredentialService


class DeferredExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, function, *args):
        self.pending.append((function, args))

    def run(self):
        function, args = self.pending.pop(0)
        function(*args)

    def shutdown(self, **kwargs):
        pass


def request(**changes):
    return LinuxDeploymentRequest.model_validate(
        {
            "command_id": "deploy-1",
            "host": "192.0.2.1",
            "username": "root",
            "password": "NEVER-PERSIST-SSH",
            "host_key_fingerprint": "SHA256:" + "A" * 43,
            "server_url": "https://api.example.test",
            "label": "Linux",
            **changes,
        }
    )


@pytest.fixture
def deployment(tmp_path):
    coordinator = Coordinator(tmp_path / "deploy.sqlite")
    credentials = NodeCredentialService(coordinator)
    control = {
        "installed": [],
        "inspection": {"ready": True, "existing_node_id": None, "existing_server_url": None},
    }

    class FakeSSH:
        def __init__(self, access):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def inspect(self):
            return control["inspection"]

        def install(self, bundle, config, token):
            control["installed"].append((config, token))
            if control.get("failure"):
                raise control["failure"]
            if control.get("report", True):
                with coordinator.store.transaction(write=True) as connection:
                    connection.execute(
                        "UPDATE node_credentials SET reported_at='heartbeat' WHERE token_hash=?",
                        (hashlib.sha256(token.encode()).hexdigest(),),
                    )

    bundle = SimpleNamespace(data=b"package", version="0.1.0", sha256="a" * 64)
    service = LinuxDeploymentService(
        coordinator, credentials, connector=FakeSSH, bundle_factory=lambda: bundle
    )
    service.executor.shutdown()
    service.executor = DeferredExecutor()
    yield service, control
    service.close()


def execute(deployment, **changes):
    service, _ = deployment
    service.submit(request(**changes))
    service.executor.run()
    return service.get(changes.get("command_id", "deploy-1"))


def test_idempotency_conflict_and_secret_free_persistence(deployment):
    service, control = deployment
    service.submit(request())
    service.submit(request(password="different-secret"))
    assert len(service.executor.pending) == 1
    with pytest.raises(DomainError, match="another request") as caught:
        service.submit(request(label="changed"))
    assert caught.value.code == "idempotency_conflict"
    service.executor.run()
    record = service.get("deploy-1")
    assert record["status"] == "completed"
    service.submit(request())
    assert len(control["installed"]) == 1
    assert not service.executor.pending
    raw = Path(service.store.database_path).read_bytes()
    assert b"NEVER-PERSIST-SSH" not in raw
    assert control["installed"][0][1].encode() not in raw
    assert "password" not in json.dumps(record)


def test_target_concurrency_and_revocation_while_running(deployment):
    service, _ = deployment
    service.submit(request())
    with pytest.raises(DomainError) as caught:
        service.submit(request(command_id="another", username="other"))
    assert caught.value.code == "deployment_busy"
    with pytest.raises(DomainError) as caught:
        service.revoke("deploy-1")
    assert caught.value.code == "deployment_in_progress"
    service.executor.run()
    assert service.revoke("deploy-1")["credential_revoked"]
    assert not service.credentials.has_reported("missing")


def test_queue_is_bounded(deployment):
    service, _ = deployment
    for index in range(4):
        service.submit(request(command_id=f"job-{index}", host=f"192.0.2.{index + 1}"))
    with pytest.raises(DomainError) as caught:
        service.submit(request(command_id="overflow", host="192.0.2.5"))
    assert caught.value.status_code == 429


def test_restart_marks_unfinished_without_reexecuting(deployment):
    service, _ = deployment
    service.submit(request())
    reopened = LinuxDeploymentService(
        service.coordinator,
        service.credentials,
        connector=lambda access: pytest.fail("must not reconnect"),
    )
    try:
        record = reopened.get("deploy-1")
        assert record["status"] == "interrupted"
        assert record["error"]["code"] == "deployment_interrupted"
        assert reopened.submit(request())["status"] == "interrupted"
    finally:
        reopened.close()


def test_existing_identity_retained(deployment):
    service, control = deployment
    control["inspection"].update(
        existing_node_id="original-node", existing_server_url="https://api.example.test/"
    )
    record = execute(deployment)
    assert record["status"] == "completed"
    assert record["node_id"] == "original-node"
    assert control["installed"][0][0]["node_id"] == "original-node"


@pytest.mark.parametrize(
    "inspection,changes,code",
    [
        ({"ready": False}, {}, "remote_not_ready"),
        ({"existing_node_id": "original"}, {"node_id": "other"}, "remote_identity_changed"),
        ({"existing_server_url": "https://other.example.test"}, {}, "remote_server_changed"),
    ],
)
def test_identity_and_readiness_fail_before_issuing_token(deployment, inspection, changes, code):
    service, control = deployment
    control["inspection"].update(inspection)
    record = execute(deployment, **changes)
    assert record["error"]["code"] == code
    assert not control["installed"]
    with service.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM node_credentials").fetchone()[0] == 0


def test_failure_fixed_error_and_explicit_revocation(deployment):
    service, control = deployment
    control["failure"] = RuntimeError("NEVER-ECHO-REMOTE")
    record = execute(deployment)
    assert record["status"] == "failed"
    assert record["error"]["code"] == "deployment_failed"
    assert "NEVER-ECHO" not in str(record)
    assert service.revoke("deploy-1")["credential_revoked"]


def test_success_requires_exact_new_credential_heartbeat(deployment, monkeypatch):
    service, control = deployment
    old = service.credentials.issue("old-node", "old-deployment")
    with service.store.transaction(write=True) as connection:
        connection.execute(
            "UPDATE node_credentials SET reported_at='old' WHERE credential_id=?",
            (old["credential_id"],),
        )
    assert service.credentials.has_reported(old["credential_id"])
    control["report"] = False
    # Fast deterministic timeout; an old heartbeat must not complete this job.
    readings = iter([0, 0, 21])
    monkeypatch.setattr("virtual_life.linux_deployment.time.monotonic", lambda: next(readings))
    monkeypatch.setattr("virtual_life.linux_deployment.time.sleep", lambda _: None)
    record = execute(deployment)
    assert record["status"] == "failed"
    assert record["error"]["code"] == "node_not_reporting"


class UploadFile:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def settimeout(self, value):
        pass

    def write(self, value):
        pass


class FakeSFTP(UploadFile):
    def get_channel(self):
        return self

    def open(self, path, mode):
        assert mode == "wx"
        return UploadFile()

    def chmod(self, path, mode):
        assert mode == 0o600


@pytest.mark.parametrize(
    "failure,expect_cleanup,expected_code",
    [
        ("upload", True, "remote_install_failed"),
        ("extract", True, "remote_install_failed"),
        ("install_timeout", False, "remote_outcome_unknown"),
        ("install_failed", True, "remote_install_failed"),
        ("malformed_result", True, "remote_outcome_unknown"),
        (None, True, None),
    ],
)
def test_staging_cleanup_respects_installer_lifetime(failure, expect_cleanup, expected_code):
    session = SSHSession(request())
    session.prefix = ["sudo", "-n"]

    def sftp():
        if failure == "upload":
            raise OSError("NEVER-ECHO-SFTP")
        return FakeSFTP()

    session.client = SimpleNamespace(open_sftp=sftp)
    commands = []

    def run(argv, timeout=25):
        commands.append(argv)
        if argv[0] == "mktemp":
            return 0, b"/tmp/virtual-life-deploy.ABCDE12345\n"
        if argv[0] == "rm":
            return 0, b""
        if "-c" in argv:
            return (1 if failure == "extract" else 0), b""
        if failure == "install_timeout":
            fail("remote_outcome_unknown")
        if failure == "install_failed":
            return 1, b"NEVER-ECHO-STDERR"
        if failure == "malformed_result":
            return 0, b"NEVER-ECHO-MALFORMED"
        return 0, b'{"status":"installed","node_id":"node","version":"0.1.0"}'

    session.run = run
    if expected_code:
        with pytest.raises(DomainError) as caught:
            session.install(
                SimpleNamespace(data=b"zip", sha256="a" * 64, version="0.1.0"),
                {"node_id": "node"},
                "TOKEN-SECRET",
            )
        assert caught.value.code == expected_code
        assert "NEVER-ECHO" not in str(caught.value)
    else:
        session.install(
            SimpleNamespace(data=b"zip", sha256="a" * 64, version="0.1.0"),
            {"node_id": "node"},
            "TOKEN-SECRET",
        )
    assert any(command[0] == "rm" for command in commands) == expect_cleanup
    assert "TOKEN-SECRET" not in str(commands)


def test_channel_disconnect_without_exit_status_is_unknown():
    class ClosedChannel:
        def settimeout(self, value):
            pass

        def exec_command(self, command):
            pass

        def recv_ready(self):
            return False

        recv_stderr_ready = recv_ready

        def exit_status_ready(self):
            return True

        def recv_exit_status(self):
            return -1

        def close(self):
            pass

    session = SSHSession(request())
    session.client = SimpleNamespace(
        get_transport=lambda: SimpleNamespace(open_session=lambda **kwargs: ClosedChannel())
    )
    with pytest.raises(DomainError) as caught:
        session.run(["fixed"])
    assert caught.value.code == "remote_outcome_unknown"


def test_channel_open_failure_is_sanitized():
    def broken(**kwargs):
        raise RuntimeError("NEVER-ECHO-CHANNEL")

    session = SSHSession(request())
    session.client = SimpleNamespace(get_transport=lambda: SimpleNamespace(open_session=broken))
    with pytest.raises(DomainError) as caught:
        session.run(["fixed"])
    assert caught.value.code == "remote_outcome_unknown"
    assert "NEVER-ECHO" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"python": "-c"},
        {"python": "../python3"},
        {"python": "python3;id"},
        {"server_url": "https://example.test/?"},
        {"server_url": "https://example.test/#"},
        {"server_url": "https://example.test\x7f"},
    ],
)
def test_invalid_program_and_server_rejected_before_ssh(changes):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        request(**changes)


@pytest.mark.parametrize("enabled", [False, True])
def test_deployment_attention_is_opt_in_and_token_scope_is_fixed(deployment, enabled):
    service, control = deployment
    assert execute(deployment, attention_enabled=enabled)["status"] == "completed"
    config, token = control["installed"][0]
    assert config.get("attention") == ({"enabled": True} if enabled else None)
    assert config["deployment_mode"] == "direct"
    body = {
        "node_id": config["node_id"],
        "label": "Linux",
        "kind": "other",
        "device": {"device_id": config["node_id"]},
        "capabilities": ["device.state.report", "attention.observe"]
        if enabled
        else ["device.state.report"],
    }
    assert service.credentials.authorize(token, "POST", "/v1/nodes", body)
    body["capabilities"] = (
        ["device.state.report"] if enabled else ["device.state.report", "attention.observe"]
    )
    with pytest.raises(DomainError):
        service.credentials.authorize(token, "POST", "/v1/nodes", body)


@pytest.mark.parametrize("value", [1, "true", None])
def test_deployment_attention_requires_strict_bool(value):
    with pytest.raises(ValueError):
        request(attention_enabled=value)


def test_attention_option_participates_in_deployment_idempotency(deployment):
    service, _ = deployment
    service.submit(request())
    with pytest.raises(DomainError) as caught:
        service.submit(request(attention_enabled=True))
    assert caught.value.code == "idempotency_conflict"
