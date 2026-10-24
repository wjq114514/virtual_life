"""Install and verify a real Linux reporter in an explicitly disposable SSH guest.

Requires: uv run --with playwright python examples/linux_live_test.py --help
The target must have no existing Virtual Life installation. This test installs and stops a service.
No production configuration is read. SSH secrets and reporter tokens remain in process memory.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import socket
import sqlite3
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import httpx
import paramiko
import uvicorn
from cryptography.hazmat.primitives import serialization
from playwright.sync_api import sync_playwright
from virtual_life.api import create_app
from virtual_life.settings import Settings

SERVICE = "virtual-life-system-node.service"
CONFIG = "/etc/virtual-life-system-node/config.json"
TOKEN = "/etc/virtual-life-system-node/token.env"


def eventually(check, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for live verification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--private-key-file", required=True, type=Path)
    parser.add_argument("--ssh-password-env")
    parser.add_argument(
        "--sudo-user", help="Also deploy using this guest's passwordless sudo account"
    )
    parser.add_argument("--guest-server-host", default="10.0.2.2")
    parser.add_argument("--artifacts", type=Path, default=Path(".data/linux-live-review"))
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    private_key = args.private_key_file.read_text()
    password = os.environ[args.ssh_password_env] if args.ssh_password_env else None
    admin = secrets.token_urlsafe(40)
    report = {"checks": [], "evidence": {}, "success": False}
    sensitive = [private_key, admin]
    if password:
        sensitive.append(password)
    ssh = paramiko.SSHClient()
    server = None
    thread = None
    owns_installation = False

    def command(text, *, check=True):
        _, stdout, stderr = ssh.exec_command(text, timeout=30)
        raw = stdout.read().decode()
        status = stdout.channel.recv_exit_status()
        stderr.read()
        if check:
            assert status == 0, "Remote verification command failed"
        return raw.strip()

    def checked(name):
        report["checks"].append(name)
        print(f"PASS {name}", flush=True)

    try:
        with socket.create_connection((args.ssh_host, args.ssh_port), timeout=15) as sock:
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=15)
            key = transport.get_remote_server_key()
            fingerprint = "SHA256:" + base64.b64encode(
                hashlib.sha256(key.asbytes()).digest()
            ).decode().rstrip("=")
            transport.close()
        report["evidence"]["host_key_fingerprint"] = fingerprint
        host_key_name = (
            args.ssh_host if args.ssh_port == 22 else f"[{args.ssh_host}]:{args.ssh_port}"
        )
        ssh.get_host_keys().add(host_key_name, key.get_name(), key)
        ssh.connect(
            args.ssh_host,
            port=args.ssh_port,
            username="root",
            key_filename=str(args.private_key_file),
            allow_agent=False,
            look_for_keys=False,
            timeout=15,
        )
        assert (
            command(
                f"test ! -e {CONFIG} && test ! -e /opt/virtual-life-system-node; echo $?",
                check=False,
            )
            == "0"
        ), "Use a fresh disposable guest; existing installation detected"
        checked("fresh disposable guest and independently verified SSH host key")
        with ExitStack() as stack:
            temporary = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="virtual-life-linux-live-")
            )
            database = Path(temporary) / "live.sqlite3"
            app = create_app(
                Settings(
                    database_path=str(database),
                    api_token=admin,
                    model_base_url="",
                    model_name="",
                    auto_memory_enabled=False,
                    embedding_enabled=False,
                )
            )
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()

            def stop_runtime():
                server.should_exit = True
                thread.join(timeout=240)
                assert not thread.is_alive(), "Temporary API did not stop"

            stack.callback(stop_runtime)
            eventually(lambda: server.started, 15)
            base = f"http://127.0.0.1:{port}"
            guest_origin = f"http://{args.guest_server_host}:{port}"
            with httpx.Client(
                base_url=base, headers={"Authorization": f"Bearer {admin}"}, timeout=60
            ) as api:

                def get(path):
                    response = api.get(path)
                    response.raise_for_status()
                    return response.json()

                def job_wait(identifier):
                    def complete():
                        response = api.get(f"/v1/deployments/linux/{identifier}")
                        # The UI allocates its ID before the browser POST is accepted.
                        if response.status_code == 404:
                            return None
                        response.raise_for_status()
                        job = response.json()
                        if job["status"] in {"failed", "interrupted"}:
                            report["failure_code"] = job["error"]["code"]
                            raise AssertionError("Real deployment failed: " + job["error"]["code"])
                        return job if job["status"] == "completed" else None

                    return eventually(complete, 90)

                def credentials_count():
                    with sqlite3.connect(database) as db:
                        return db.execute("SELECT COUNT(*) FROM node_credentials").fetchone()[0]

                def service_state():
                    return dict(
                        line.split("=", 1)
                        for line in command(
                            f"systemctl show {SERVICE} -p MainPID -p User -p Group -p RestartPreventExitStatus -p NRestarts -p ActiveState"
                        ).splitlines()
                    )

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=True, executable_path=args.browser_executable
                    )
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 1000}, accept_downloads=True
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(type(error).__name__))
                    page.goto(base + "/ui/")
                    page.click("#connection-open")
                    page.fill("#token", admin)
                    page.click("#connect")
                    page.wait_for_function(
                        "document.querySelector('#connection').textContent === '已连接'"
                    )
                    page.click("#connection-open")
                    page.click("#linux-open-connection")
                    page.fill("#linux-host", args.ssh_host)
                    page.fill("#linux-port", str(args.ssh_port))
                    page.select_option("#linux-auth", "key")
                    page.fill("#linux-key", private_key)
                    page.fill("#linux-server", guest_origin)
                    page.fill("#linux-label", "Disposable live Linux test")
                    page.click("#linux-probe")
                    page.wait_for_function("!linuxDeployment.busy")
                    assert page.evaluate("linuxDeployment.fingerprint") == fingerprint
                    page.check("#linux-trust")
                    page.click("#linux-inspect")
                    page.wait_for_function("!linuxDeployment.busy")
                    assert "环境就绪" in page.text_content("#linux-environment")
                    assert page.input_value("#linux-key") == ""
                    checked(
                        "real WebUI connection, pinned SSH probe and ready inspection with secret clearing"
                    )
                    body = {
                        "host": args.ssh_host,
                        "port": args.ssh_port,
                        "username": "root",
                        "private_key": private_key,
                        "python": "python3",
                        "host_key_fingerprint": fingerprint,
                        "server_url": guest_origin,
                        "label": "Disposable live Linux test",
                    }
                    page.fill("#linux-key", private_key)
                    owns_installation = True
                    page.click("#linux-deploy")
                    page.wait_for_function("linuxDeployment.commandId !== null")
                    first_id = page.evaluate("linuxDeployment.commandId")
                    first = job_wait(first_id)
                    page.wait_for_function(
                        "linuxDeployment.job?.status === 'completed'", timeout=90000
                    )
                    assert page.input_value("#linux-key") == ""
                    node_id = first["node_id"]
                    nodes = get("/v1/nodes")
                    assert len(nodes) == 1 and nodes[0]["node_id"] == node_id
                    assert nodes[0]["capabilities"] == ["device.state.report"]
                    with sqlite3.connect(database) as db:
                        credential = db.execute(
                            "SELECT credential_id FROM node_credentials WHERE deployment_id=?",
                            (first_id,),
                        ).fetchone()[0]
                    assert app.state.node_credentials.has_reported(credential)
                    original_sample = nodes[0]["context"]["device_state"]["sampled_at"]

                    def real_sample():
                        node = get(f"/v1/nodes/{node_id}")
                        state = node["context"]["device_state"]
                        metrics = state["metrics"]
                        return (
                            state
                            if state["sampled_at"] != original_sample
                            and metrics["cpu_usage_percent"] is not None
                            else None
                        )

                    sample = eventually(real_sample, 50)
                    assert sample["source"]["adapter"] == "linux_proc"
                    assert sample["metrics"]["memory_total_bytes"] > 0
                    assert sample["metrics"]["disk_total_bytes"] > 0
                    checked(
                        "unique reporter registers, credential reports, and real proc CPU/memory/disk heartbeat advances"
                    )
                    report["evidence"]["node_id"] = node_id
                    report["evidence"]["sample"] = sample
                    before = service_state()
                    report["evidence"]["initial_service"] = before
                    assert before["ActiveState"] == "active" and int(before["MainPID"]) > 0
                    assert before["User"] == "virtual-life" and before["Group"]
                    assert (
                        "2" in before["RestartPreventExitStatus"]
                        and "3" in before["RestartPreventExitStatus"]
                    )
                    assert command(f"stat -c %a {TOKEN}") == "600"
                    assert not command("find /tmp -maxdepth 1 -name 'virtual-life-deploy.*' -print")
                    config_raw = command(f"cat {CONFIG}")
                    token_raw = command(f"cat {TOKEN}")
                    assert admin not in config_raw + token_raw
                    reporter_token = token_raw.split("=", 1)[1].strip()
                    with page.expect_download() as download:
                        page.click("#linux-bundle")
                    bundle_path = Path(temporary) / "download.tar.gz"
                    download.value.save_as(bundle_path)
                    bundle = bundle_path.read_bytes()
                    assert hashlib.sha256(bundle).hexdigest() == first["package_sha256"]
                    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
                        manifest = json.load(archive.extractfile("manifest.json"))
                    remote_hash = command(
                        "sha256sum /opt/virtual-life-system-node/current/system-node.pyz"
                    ).split()[0]
                    assert remote_hash == manifest["sha256"]["system-node.pyz"]
                    checked(
                        "real service hardening, 0600 token, package/download hashes, admin separation and staging cleanup"
                    )
                    report["evidence"]["bundle_sha256"] = first["package_sha256"]
                    report["evidence"]["program_sha256"] = remote_hash
                    count = credentials_count()
                    response = api.post(
                        "/v1/deployments/linux", json={**body, "command_id": first_id}
                    )
                    assert response.status_code == 202
                    assert response.json()["status"] == "completed"
                    assert service_state()["MainPID"] == before["MainPID"]
                    assert credentials_count() == count
                    checked("same command ID is idempotent without PID or credential changes")
                    second_body = {**body, "command_id": str(uuid.uuid4())}
                    if password:
                        second_body.pop("private_key")
                        second_body["password"] = password
                    response = api.post("/v1/deployments/linux", json=second_body)
                    assert response.status_code == 202
                    second = job_wait(second_body["command_id"])
                    assert second["node_id"] == node_id and len(get("/v1/nodes")) == 1
                    assert service_state()["MainPID"] != before["MainPID"]
                    report["evidence"]["reinstalled_service"] = service_state()
                    with sqlite3.connect(database) as db:
                        credential = db.execute(
                            "SELECT credential_id FROM node_credentials WHERE deployment_id=?",
                            (second["command_id"],),
                        ).fetchone()[0]
                    assert app.state.node_credentials.has_reported(credential)
                    checked(
                        "real reinstall retains node identity, replaces service PID and confirms new credential report"
                    )
                    active_id = second["command_id"]
                    if args.sudo_user:
                        phrase = secrets.token_urlsafe(24)
                        loader = (
                            serialization.load_ssh_private_key
                            if "BEGIN OPENSSH PRIVATE KEY" in private_key
                            else serialization.load_pem_private_key
                        )
                        encrypted = (
                            loader(private_key.encode(), password=None)
                            .private_bytes(
                                serialization.Encoding.PEM,
                                serialization.PrivateFormat.OpenSSH,
                                serialization.BestAvailableEncryption(phrase.encode()),
                            )
                            .decode()
                        )
                        sensitive.extend([phrase, encrypted])
                        page.click("#linux-new")
                        page.fill("#linux-user", args.sudo_user)
                        page.fill("#linux-key", encrypted)
                        page.fill("#linux-passphrase", phrase)
                        page.click("#linux-inspect")
                        page.wait_for_function("!linuxDeployment.busy")
                        assert "环境就绪" in page.text_content("#linux-environment")
                        assert page.input_value("#linux-key") == ""
                        assert page.input_value("#linux-passphrase") == ""
                        previous_pid = service_state()["MainPID"]
                        page.fill("#linux-key", encrypted)
                        page.fill("#linux-passphrase", phrase)
                        page.click("#linux-deploy")
                        page.wait_for_function("linuxDeployment.commandId !== null")
                        active_id = page.evaluate("linuxDeployment.commandId")
                        third = job_wait(active_id)
                        page.wait_for_function(
                            "linuxDeployment.job?.status === 'completed'", timeout=90000
                        )
                        assert third["username"] == args.sudo_user
                        assert third["node_id"] == node_id and len(get("/v1/nodes")) == 1
                        assert service_state()["MainPID"] != previous_pid
                        assert credentials_count() == count + 2
                        report["evidence"]["sudo_service"] = service_state()
                        checked(
                            "real encrypted OpenSSH private key and non-root passwordless sudo install via WebUI"
                        )
                    stable_config = command(f"cat {CONFIG}")
                    stable_token = command(f"cat {TOKEN}")
                    reporter_token = stable_token.split("=", 1)[1].strip()
                    sensitive.append(reporter_token)
                    bad_cases = [
                        {"host_key_fingerprint": "SHA256:" + "A" * 43},
                        {"private_key": None, "password": secrets.token_urlsafe(32)},
                        {"node_id": "unexpected-remote-identity"},
                        {"server_url": "http://127.0.0.1:1"},
                    ]
                    for change in bad_cases:
                        invalid = {**body, **change, "command_id": str(uuid.uuid4())}
                        invalid = {
                            name: value for name, value in invalid.items() if value is not None
                        }
                        response = api.post("/v1/deployments/linux", json=invalid)
                        if response.status_code == 202:
                            rejected = eventually(
                                lambda: (
                                    job
                                    if (
                                        job := get(f"/v1/deployments/linux/{invalid['command_id']}")
                                    )["status"]
                                    in {"failed", "completed"}
                                    else None
                                ),
                                90,
                            )
                            assert rejected["status"] == "failed"
                        else:
                            assert 400 <= response.status_code < 500
                        assert command(f"cat {CONFIG}") == stable_config
                        assert command(f"cat {TOKEN}") == stable_token
                    checked(
                        "real pin/auth/remote-ID/remote-origin rejection preserves remote identity and token"
                    )
                    reporter_headers = {"Authorization": f"Bearer {reporter_token}"}
                    assert api.get("/v1/nodes", headers=reporter_headers).status_code == 403
                    assert api.post(
                        f"/v1/nodes/{node_id}/heartbeat",
                        headers=reporter_headers,
                        json={"context": {}},
                    ).is_success
                    close_code = page.evaluate(
                        """({url, token}) => new Promise(resolve => {
                        const ws = new WebSocket(url);
                        ws.onopen = () => ws.send(JSON.stringify({type:'auth', token}));
                        ws.onclose = event => resolve(event.code);
                    })""",
                        {
                            "url": base.replace("http:", "ws:") + f"/v1/nodes/{node_id}/events",
                            "token": reporter_token,
                        },
                    )
                    assert close_code == 4401
                    checked(
                        "real reporter token permits own heartbeat and denies management GET and WebSocket"
                    )
                    page.wait_for_function("!linuxDeployment.busy")
                    for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                        page.set_viewport_size({"width": width, "height": height})
                        assert not page.evaluate(
                            "document.documentElement.scrollWidth > innerWidth || document.querySelector('#linux-dialog').scrollWidth > document.querySelector('#linux-dialog').clientWidth"
                        )
                        assert all(
                            page.input_value(selector) == ""
                            for selector in ("#linux-password", "#linux-key", "#linux-passphrase")
                        )
                        page.screenshot(path=str(args.artifacts / f"{name}.png"), full_page=True)
                    storage = page.evaluate(
                        "JSON.stringify([Object.values(localStorage), Object.values(sessionStorage)])"
                    )
                    assert all(value not in storage for value in sensitive)
                    for path in Path(temporary).glob("live.sqlite3*"):
                        data = path.read_bytes()
                        assert all(value.encode() not in data for value in sensitive)
                        assert b"PRIVATE KEY-----" not in data
                    assert not errors
                    checked(
                        "desktop/mobile overflow, clear-secret screenshots, browser storage and SQLite secret checks"
                    )
                    response = api.delete(f"/v1/deployments/linux/{active_id}/credential")
                    assert response.is_success and response.json()["credential_revoked"]
                    eventually(lambda: service_state()["ActiveState"] in {"failed", "inactive"}, 25)
                    stopped = service_state()
                    report["evidence"]["revoked_service"] = stopped
                    assert stopped["MainPID"] == "0"
                    time.sleep(6)
                    assert service_state()["MainPID"] == "0"
                    assert service_state()["NRestarts"] == stopped["NRestarts"]
                    assert (
                        api.post(
                            f"/v1/nodes/{node_id}/heartbeat",
                            headers=reporter_headers,
                            json={"context": {}},
                        ).status_code
                        == 403
                    )
                    checked("real revocation denies reports and stops service without restart")
                    browser.close()
            report["success"] = True
    except Exception as error:
        # Do not serialize raw exception/HTTP request text: it can carry SSH credentials.
        report["failure_type"] = type(error).__name__
        print("FAIL " + type(error).__name__, flush=True)
        raise
    finally:
        if owns_installation:
            try:
                command(f"systemctl disable --now {SERVICE}", check=False)
            except Exception:
                report["cleanup_failed"] = True
        ssh.close()
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        (args.artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
