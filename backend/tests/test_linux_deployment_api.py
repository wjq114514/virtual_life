"""HTTP boundaries for Linux deployment credentials and deployment administration."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from virtual_life.api import create_app
from virtual_life.settings import Settings


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "api.sqlite"), api_token="admin"))
    with TestClient(app, headers={"Authorization": "Bearer admin"}) as client:
        yield client


def reporter(client):
    issued = client.app.state.node_credentials.issue("linux", "deploy")
    headers = {"Authorization": "Bearer " + issued["token"]}
    body = {
        "node_id": "linux",
        "label": "Linux",
        "kind": "other",
        "capabilities": ["device.state.report"],
        "device": {"device_id": "linux"},
    }
    response = client.post("/v1/nodes", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return issued, headers, body


def test_reporter_http_lifecycle_and_revocation(client):
    issued, headers, _ = reporter(client)
    credentials = client.app.state.node_credentials
    assert not credentials.has_reported(issued["credential_id"])
    response = client.post("/v1/nodes/linux/heartbeat", json={"context": {}}, headers=headers)
    assert response.status_code == 200, response.text
    assert credentials.has_reported(issued["credential_id"])
    observation = {
        "command_id": "sample",
        "modality": "device_state",
        "data": {"subject_node_id": "linux", "observer_node_id": "linux"},
    }
    response = client.post("/v1/nodes/linux/observations", json=observation, headers=headers)
    assert response.status_code == 201, response.text
    observation["data"]["subject_node_id"] = "other"
    assert (
        client.post("/v1/nodes/linux/observations", json=observation, headers=headers).status_code
        == 403
    )
    credentials.revoke_for_deployment("deploy")
    assert client.post("/v1/nodes/linux/heartbeat", json={}, headers=headers).status_code == 403
    assert not credentials.has_reported(issued["credential_id"])


@pytest.mark.parametrize(
    "path",
    [
        "/v1/nodes",
        "/v1/events",
        "/v1/capabilities",
        "/v1/companions",
        "/v1/deployments/linux/bundle",
        "/v1/deployments/linux/unknown",
    ],
)
def test_reporter_cannot_read_or_administer(client, path):
    _, headers, _ = reporter(client)
    assert client.get(path, headers=headers).status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"node_id": "other"},
        {"kind": "desktop"},
        {"capabilities": ["device.state.report", "avatar.render"]},
        {"capabilities": ["device.state.report", "device.state.observe"]},
        {"device": {"device_id": "victim"}},
    ],
)
def test_reporter_http_cannot_expand_registration(client, changes):
    _, headers, body = reporter(client)
    assert client.post("/v1/nodes", json={**body, **changes}, headers=headers).status_code == 403
    assert client.post("/v1/nodes/other/heartbeat", json={}, headers=headers).status_code == 403


def test_reporter_cannot_open_websocket(client):
    issued, _, _ = reporter(client)
    with client.websocket_connect("/v1/nodes/linux/events") as socket:
        socket.send_json({"type": "auth", "token": issued["token"]})
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_json()
        assert caught.value.code == 4401


@pytest.mark.parametrize("path", ["/host-key", "/inspect", ""])
def test_deployment_routes_require_admin(client, path):
    _, headers, _ = reporter(client)
    assert client.post("/v1/deployments/linux" + path, json={}, headers=headers).status_code == 403


def ssh_request(**changes):
    return {
        "host": "192.0.2.10",
        "username": "root",
        "password": "ssh-test-secret",
        "host_key_fingerprint": "SHA256:" + "A" * 43,
        **changes,
    }


def deployment_request(**changes):
    return ssh_request(
        **{
            "command_id": "install-1",
            "server_url": "https://api.example.test",
            "label": "Linux",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/deployments/linux/inspect", ssh_request(password={"secret": "NEVER-ECHO"})),
        ("/v1/deployments/linux/inspect", ssh_request(private_key="NEVER-ECHO")),
        ("/v1/deployments/linux", deployment_request(server_url="https://user:NEVER-ECHO@host")),
        ("/v1/deployments/linux", deployment_request(unknown_secret="NEVER-ECHO")),
    ],
)
def test_deployment_validation_does_not_echo_secrets(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 422, response.text
    assert "NEVER-ECHO" not in response.text
    assert "ssh-test-secret" not in response.text


def test_admin_bundle_and_capabilities(client):
    import hashlib
    import io
    import tarfile

    response = client.get("/v1/deployments/linux/bundle")
    assert response.status_code == 200, response.text
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        assert {"installer.py", "system-node.pyz", "manifest.json"} <= set(archive.getnames())
    assert "admin".encode() not in response.content
    capability = client.get("/v1/capabilities")
    assert capability.status_code == 200
    linux = capability.json()["linux_deployment"]
    assert linux["ssh"] and linux["host_key_pinning"] and linux["scoped_reporter_credentials"]
    assert linux["deployment_mode"] == "direct"
    assert linux["python_minimum"] == "3.12"
    # Artifact content remains deterministic across downloads.
    again = client.get("/v1/deployments/linux/bundle")
    assert hashlib.sha256(response.content).digest() == hashlib.sha256(again.content).digest()


class FakeSSH:
    def __init__(self, access, control):
        self.access = access
        self.control = control

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def inspect(self):
        return {
            "ready": True,
            "python_version": "3.12.10",
            "systemd": True,
            "privileged": True,
            "existing_node_id": None,
            "existing_server_url": None,
            "reason_codes": [],
        }

    def install(self, bundle, config, token):
        self.control["calls"].append((config["node_id"], token))
        if self.control.get("fail"):
            raise RuntimeError("ssh-test-secret NEVER-ECHO")
        client = self.control["client"]
        headers = {"Authorization": "Bearer " + token}
        node_id = config["node_id"]
        response = client.post(
            "/v1/nodes",
            headers=headers,
            json={
                "node_id": node_id,
                "label": config["label"],
                "kind": "other",
                "capabilities": ["device.state.report"],
                "device": {"device_id": node_id},
            },
        )
        assert response.status_code == 201, response.text
        response = client.post(f"/v1/nodes/{node_id}/heartbeat", headers=headers, json={})
        assert response.status_code == 200, response.text


@pytest.fixture
def deploying_client(tmp_path):
    from virtual_life.linux_bundle import get_linux_bundle

    control = {"calls": []}
    app = create_app(
        Settings(database_path=str(tmp_path / "deploy.sqlite"), api_token="admin"),
        linux_ssh_connector=lambda access: FakeSSH(access, control),
        linux_bundle_factory=get_linux_bundle,
    )
    with TestClient(app, headers={"Authorization": "Bearer admin"}) as client:
        control["client"] = client
        yield client, control


def wait_job(client, command_id):
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get("/v1/deployments/linux/" + command_id)
        assert response.status_code == 200, response.text
        record = response.json()
        if record["status"] not in {"running", "queued"}:
            return record
        time.sleep(0.01)
    pytest.fail("deployment fake did not finish")


def test_deployment_job_reports_and_is_idempotent(deploying_client):
    client, control = deploying_client
    request = deployment_request()
    response = client.post("/v1/deployments/linux", json=request)
    assert response.status_code == 202, response.text
    record = wait_job(client, request["command_id"])
    assert record["status"] == "completed", record
    assert len(control["calls"]) == 1
    token = control["calls"][0][1]
    repeated = client.post("/v1/deployments/linux", json=request)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["command_id"] == request["command_id"]
    assert len(control["calls"]) == 1
    assert token not in repeated.text
    assert "ssh-test-secret" not in repeated.text
    with client.app.state.coordinator.store.transaction() as connection:
        dump = "\n".join(connection.iterdump())
    assert "ssh-test-secret" not in dump
    assert token not in dump
    conflict = client.post("/v1/deployments/linux", json={**request, "label": "Changed"})
    assert conflict.status_code == 409
    revoked = client.delete("/v1/deployments/linux/install-1/credential")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["credential_revoked"]
    assert (
        client.post(
            f"/v1/nodes/{record['node_id']}/heartbeat",
            json={},
            headers={"Authorization": "Bearer " + token},
        ).status_code
        == 403
    )


def test_deployment_failure_is_safe(deploying_client):
    client, control = deploying_client
    control["fail"] = True
    response = client.post("/v1/deployments/linux", json=deployment_request())
    assert response.status_code == 202, response.text
    record = wait_job(client, "install-1")
    assert record["status"] == "failed"
    assert record["error"]["code"] == "deployment_failed"
    text = str(record)
    assert "NEVER-ECHO" not in text
    assert "ssh-test-secret" not in text


def test_deployment_large_or_malformed_body_is_safe(client):
    secret = "NEVER-ECHO"
    too_large = client.post(
        "/v1/deployments/linux/inspect", json=ssh_request(password=secret * 20_000)
    )
    assert too_large.status_code == 413
    assert secret not in too_large.text
    malformed = client.post(
        "/v1/deployments/linux",
        content='{"password":"NEVER-ECHO",',
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 422
    assert secret not in malformed.text
    with client.app.state.coordinator.store.transaction() as connection:
        dump = "\n".join(connection.iterdump())
    assert secret not in dump


def test_deployment_bundle_requires_authentication(client):
    response = client.get("/v1/deployments/linux/bundle", headers={"Authorization": ""})
    assert response.status_code == 401


def test_inspection_is_injected_and_returns_safe_public_result(deploying_client):
    client, _ = deploying_client
    response = client.post("/v1/deployments/linux/inspect", json=ssh_request())
    assert response.status_code == 200, response.text
    assert response.json()["ready"]
    assert response.json()["python_version"] == "3.12.10"
    assert "ssh-test-secret" not in response.text


@pytest.mark.parametrize("enabled", [False, True])
def test_attention_reporter_http_scope_and_validation(client, enabled):
    from datetime import datetime, timezone

    credentials = client.app.state.node_credentials
    issued = credentials.issue("linux", "attention-deploy", attention_enabled=enabled)
    headers = {"Authorization": "Bearer " + issued["token"]}
    capabilities = ["device.state.report", "attention.observe"]
    body = {
        "node_id": "linux",
        "label": "Linux",
        "kind": "other",
        "capabilities": capabilities if enabled else capabilities[:1],
        "device": {"device_id": "linux"},
    }
    assert client.post("/v1/nodes", json=body, headers=headers).status_code == 201
    sample = {
        "command_id": "attention-1",
        "sample": {
            "session_id": "session",
            "sequence": 0,
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "focus": "focused",
            "user_present": True,
        },
    }
    response = client.post("/v1/nodes/linux/attention", json=sample, headers=headers)
    assert response.status_code == (200 if enabled else 403), response.text
    assert client.post("/v1/nodes/other/attention", json=sample, headers=headers).status_code == 403
    assert client.get("/v1/nodes/linux/attention", headers=headers).status_code == 403
    assert client.get("/v1/capabilities", headers=headers).status_code == 403
    if enabled:
        sample["sample"]["user_present"] = "true"
        assert (
            client.post("/v1/nodes/linux/attention", json=sample, headers=headers).status_code
            == 422
        )
        credentials.revoke_for_deployment("attention-deploy")
        assert (
            client.post("/v1/nodes/linux/attention", json=sample, headers=headers).status_code
            == 403
        )
    else:
        assert (
            client.post(
                "/v1/nodes", json={**body, "capabilities": capabilities}, headers=headers
            ).status_code
            == 403
        )


def test_attention_revocation_is_rechecked_before_atomic_write(client, monkeypatch):
    from datetime import datetime, timezone

    credentials = client.app.state.node_credentials
    token = credentials.issue("linux", "attention-deploy", attention_enabled=True)["token"]
    headers = {"Authorization": "Bearer " + token}
    assert (
        client.post(
            "/v1/nodes",
            json={
                "node_id": "linux",
                "label": "Linux",
                "kind": "other",
                "capabilities": ["device.state.report", "attention.observe"],
                "device": {"device_id": "linux"},
            },
            headers=headers,
        ).status_code
        == 201
    )
    proactivity = client.app.state.proactivity
    original = proactivity.observe

    def revoke_then_observe(*args, **kwargs):
        credentials.revoke_for_deployment("attention-deploy")
        return original(*args, **kwargs)

    monkeypatch.setattr(proactivity, "observe", revoke_then_observe)
    response = client.post(
        "/v1/nodes/linux/attention",
        json={
            "command_id": "revoked-attention",
            "sample": {
                "session_id": "session",
                "sequence": 0,
                "sampled_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert proactivity.get_attention("linux")["sample"] is None
