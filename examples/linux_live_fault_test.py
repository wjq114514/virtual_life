"""Verify real installer rollback in the explicitly disposable vl-linux-live-test VM.

Only creates this test's API/database; it reinstalls and finally stops the guest service.
Run after linux_live_test.py has stopped its temporary API. No SSH, installer, HTTP,
SQLite, systemd or heartbeat transport is mocked. The package alone injects a fault.
"""

import argparse
import errno
import hashlib
import io
import json
import secrets
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from virtual_life.api import create_app
from virtual_life.linux_bundle import LinuxBundle, get_linux_bundle
from virtual_life.linux_deployment import SSHAccess, SSHHost, SSHSession, probe_host_key
from virtual_life.settings import Settings

SERVICE = "virtual-life-system-node.service"
CONFIG = "/etc/virtual-life-system-node/config.json"
TOKEN = "/etc/virtual-life-system-node/token.env"
CURRENT = "/opt/virtual-life-system-node/current"
PROCESS_PATTERN = (
    r"^/[^ ]*python[^ ]* /opt/virtual-life-system-node/current/system-node[.]pyz "
    r"--config /etc/virtual-life-system-node/config[.]json --state-dir "
    r"/var/lib/virtual-life-system-node$"
)
SNAPSHOT_SCRIPT = r"""
import json, os, pathlib
config=pathlib.Path("/etc/virtual-life-system-node/config.json")
token=pathlib.Path("/etc/virtual-life-system-node/token.env")
with config.open("rb") as stream: config_raw=stream.read(65537)
with token.open("rb") as stream: token_raw=stream.read(4097)
assert len(config_raw)<=65536 and len(token_raw)<=4096
print(json.dumps(dict(config=config_raw.decode(),token=token_raw.decode(),
    current=os.readlink("/opt/virtual-life-system-node/current"))))
"""


def eventually(check, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError("Live verification deadline exceeded")


def fault_bundle(good, mode="exit"):
    """Keep the real installer, replacing only the reporter with a failing zipapp."""
    with tarfile.open(fileobj=io.BytesIO(good.data), mode="r:gz") as archive:
        installer = archive.extractfile("installer.py").read()
        manifest = json.load(archive.extractfile("manifest.json"))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        program = "import sys, time\nif '--check-config' in sys.argv: raise SystemExit(0)\n"
        program += "raise SystemExit(2)\n" if mode == "exit" else "time.sleep(120)\n"
        archive.writestr("__main__.py", program)
    contents = {"installer.py": installer, "system-node.pyz": stream.getvalue()}
    manifest["version"] = "0.1.1"
    manifest["sha256"] = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    contents["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    data = stream.getvalue()
    return LinuxBundle(data=data, sha256=hashlib.sha256(data).hexdigest(), version="0.1.1")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default="127.0.0.1")
    parser.add_argument("--ssh-port", type=int, default=50922)
    parser.add_argument("--private-key-file", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/linux-live-fault"))
    parser.add_argument("--fault-mode", choices=("exit", "no-ready"), default="exit")
    args = parser.parse_args(argv)
    if (args.ssh_host, args.ssh_port) != ("127.0.0.1", 50922):
        parser.error("This destructive acceptance script only permits 127.0.0.1:50922")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {"success": False, "checks": [], "evidence": {"fault_mode": args.fault_mode}}
    private_key = args.private_key_file.read_text()
    admin = secrets.token_urlsafe(40)
    sensitive = [private_key, admin]
    service_owned = False
    server = None
    thread = None
    listener = None
    app = None
    temporary = None
    api = None
    ssh = None
    submitted_ids = []

    def checked(name):
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    def command(argv, *, check=True):
        status, raw = ssh.run(argv, timeout=30)
        if check:
            assert status == 0, "Guest verification command failed"
        return status, raw.decode().strip()

    def snapshot():
        _, raw = command(["python3", "-c", SNAPSHOT_SCRIPT])
        result = json.loads(raw)
        sensitive.append(result["token"])
        token = result["token"].split("=", 1)[1].strip()
        sensitive.append(token)
        result["reporter_token"] = token
        return result

    def service():
        _, raw = command(["systemctl", "show", SERVICE, "-p", "MainPID", "-p", "ActiveState"])
        state = dict(line.split("=", 1) for line in raw.splitlines())
        state["MainPID"] = int(state["MainPID"])
        return state

    def process_ids():
        status, raw = command(["pgrep", "-f", PROCESS_PATTERN], check=False)
        assert status in {0, 1}, "pgrep failed"
        return [int(line) for line in raw.splitlines()] if raw else []

    def get(path):
        response = api.get(path)
        assert response.is_success, "Live API read failed"
        return response.json()

    def deploy(body):
        identifier = str(uuid.uuid4())
        response = api.post("/v1/deployments/linux", json={**body, "command_id": identifier})
        assert response.status_code == 202, "Live deployment submission rejected"
        submitted_ids.append(identifier)

        def terminal():
            record = get("/v1/deployments/linux/" + identifier)
            return record if record["status"] not in {"queued", "running"} else None

        return eventually(terminal, 120)

    def credential(deployment_id):
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT credential_id,token_hash,reported_at,revoked_at FROM node_credentials "
                "WHERE deployment_id=?",
                (deployment_id,),
            ).fetchone()
        assert row is not None, "Deployment did not issue its reporter credential"
        return dict(row)

    def sample(node_id):
        return get("/v1/nodes/" + node_id)["context"].get("device_state", {})

    def public_sample(state):
        return {key: state[key] for key in ("session_id", "sequence", "sampled_at")}

    try:
        pin = probe_host_key(SSHHost(host=args.ssh_host, port=args.ssh_port))
        access = SSHAccess(
            host=args.ssh_host,
            port=args.ssh_port,
            username="root",
            private_key=private_key,
            host_key_fingerprint=pin["fingerprint"],
        )
        ssh = SSHSession(access).__enter__()
        _, hostname = command(["hostname"])
        assert hostname == "vl-linux-live-test", (
            "Refusing a guest other than the disposable test VM"
        )
        report["evidence"]["hostname"] = hostname
        report["evidence"]["host_key_fingerprint"] = pin["fingerprint"]
        initial = snapshot()
        original_config = json.loads(initial["config"])
        node_id = original_config["node_id"]
        origin = original_config["server_url"]
        parsed = urlsplit(origin)
        assert parsed.scheme == "http" and parsed.hostname == "10.0.2.2"
        assert parsed.port is not None and parsed.path in {"", "/"}
        assert not parsed.query and not parsed.fragment and not parsed.username
        port = parsed.port
        checked("pinned SSH and disposable hostname guard; existing node/origin retained")

        # Bind before creating the API: an existing live-test API must never be replaced.
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        def bind_original_port():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    raise AssertionError("Previous test API is still listening; stop it first")
            except ConnectionRefusedError:
                pass
            try:
                listener.bind(("127.0.0.1", port))
                return True
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                # The previous pre-bound test listener may have left TIME_WAIT.
                return False

        eventually(bind_original_port, 75)
        listener.listen(128)
        temporary = Path(tempfile.mkdtemp(prefix="virtual-life-live-fault-"))
        database = temporary / "fault.sqlite3"
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
        good = get_linux_bundle()
        assert app.state.linux_deployments.connector is SSHSession
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        eventually(lambda: server.started, 15)
        api = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + admin},
            timeout=60,
        )
        body = {
            "host": args.ssh_host,
            "port": args.ssh_port,
            "username": "root",
            "private_key": private_key,
            "host_key_fingerprint": pin["fingerprint"],
            "server_url": origin,
            "node_id": node_id,
            "label": original_config["label"],
        }
        service_owned = True
        first = deploy(body)
        assert first["status"] == "completed", (
            "Good package did not establish real reporter baseline"
        )
        assert first["node_id"] == node_id
        baseline = snapshot()
        baseline_config = json.loads(baseline["config"])
        assert baseline_config["node_id"] == node_id and baseline_config["server_url"] == origin
        baseline_service = service()
        assert baseline_service["ActiveState"] == "active" and baseline_service["MainPID"] > 0
        assert process_ids() == [baseline_service["MainPID"]], (
            "Expected exactly one baseline reporter"
        )
        baseline_sample = sample(node_id)
        old_credential = credential(first["command_id"])
        assert old_credential["reported_at"] is not None
        assert (
            old_credential["token_hash"]
            == hashlib.sha256(baseline["reporter_token"].encode()).hexdigest()
        )
        report["evidence"].update(
            node_id=node_id,
            server_url=origin,
            baseline_pid=baseline_service["MainPID"],
            baseline_sample=public_sample(baseline_sample),
            good_package_sha256=good.sha256,
        )
        checked("real good-package reinstall and exact credential heartbeat establish baseline")

        broken = fault_bundle(good, args.fault_mode)
        app.state.linux_deployments.bundle_factory = lambda: broken
        failed = deploy(body)
        assert failed["status"] == "failed", "Fault package unexpectedly completed"
        report["evidence"]["fault_job"] = {
            key: failed[key]
            for key in ("command_id", "status", "error", "version", "package_sha256")
        }
        assert failed["error"]["code"] == "remote_install_failed", (
            "Fault did not cause installer rollback"
        )
        restored = snapshot()
        assert all(restored[key] == baseline[key] for key in ("config", "token", "current")), (
            "Rollback did not restore exact current/config/token"
        )
        restored_service = service()
        assert restored_service["ActiveState"] == "active"
        assert restored_service["MainPID"] != baseline_service["MainPID"]
        assert process_ids() == [restored_service["MainPID"]]
        assert (
            command(["test", "!", "-e", f"/proc/{baseline_service['MainPID']}"], check=False)[0]
            == 0
        )
        failed_credential = credential(failed["command_id"])
        assert failed_credential["reported_at"] is None
        assert not app.state.node_credentials.has_reported(failed_credential["credential_id"])

        def resumed():
            state = sample(node_id)
            return (
                state
                if state.get("session_id") != baseline_sample["session_id"]
                and state.get("sampled_at", "") > baseline_sample["sampled_at"]
                else None
            )

        resumed_sample = eventually(resumed, 75)
        assert app.state.node_credentials.has_reported(old_credential["credential_id"])
        report["evidence"].update(
            rollback_pid=restored_service["MainPID"],
            rollback_sample=public_sample(resumed_sample),
            exact_current_config_token_restored=True,
            failed_credential_reported=False,
        )
        checked(
            "fault package triggers real rollback; old token authenticates fresh reporter session"
        )

        app.state.linux_deployments.bundle_factory = lambda: good
        third = deploy(body)
        assert third["status"] == "completed" and third["node_id"] == node_id
        final = snapshot()
        final_config = json.loads(final["config"])
        assert final_config["node_id"] == node_id and final_config["server_url"] == origin
        final_service = service()
        assert final_service["ActiveState"] == "active" and final_service["MainPID"] > 0
        assert final_service["MainPID"] not in {
            baseline_service["MainPID"],
            restored_service["MainPID"],
        }
        assert process_ids() == [final_service["MainPID"]], "Duplicate reporter process detected"
        for old_pid in (baseline_service["MainPID"], restored_service["MainPID"]):
            assert command(["test", "!", "-e", f"/proc/{old_pid}"], check=False)[0] == 0
        final_credential = credential(third["command_id"])
        assert final_credential["reported_at"] is not None
        assert (
            final_credential["token_hash"]
            == hashlib.sha256(final["reporter_token"].encode()).hexdigest()
        )
        assert len(get("/v1/nodes")) == 1
        report["evidence"].update(
            final_pid=final_service["MainPID"],
            final_sample=public_sample(sample(node_id)),
            exact_single_mainpid=True,
            identity_retained=True,
        )
        checked(
            "subsequent good deployment succeeds with unchanged identity and exactly one new process"
        )
        report["success"] = True
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        print("FAIL " + type(exc).__name__, flush=True)
    finally:
        cleanup_ok = True
        if api is not None and app is not None:
            for identifier in submitted_ids:
                try:
                    record = get("/v1/deployments/linux/" + identifier)
                    if record["status"] in {"queued", "running"}:
                        continue
                    response = api.delete(f"/v1/deployments/linux/{identifier}/credential")
                    assert response.is_success
                except Exception:
                    cleanup_ok = False
        if api is not None:
            api.close()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=240)
            if thread.is_alive():
                cleanup_ok = False
        runtime_stopped = thread is None or not thread.is_alive()
        if app is not None and runtime_stopped:
            # A timed-out HTTP waiter may leave a worker finishing during API
            # shutdown. Revoke its real SQLite credential only after it finishes.
            for identifier in submitted_ids:
                try:
                    app.state.node_credentials.revoke_for_deployment(identifier)
                except Exception:
                    cleanup_ok = False
        if service_owned and ssh is not None:
            try:
                command(["systemctl", "stop", SERVICE])
                stopped = service()
                assert stopped["MainPID"] == 0
                assert stopped["ActiveState"] in {"inactive", "failed"}
                assert process_ids() == []
            except Exception:
                cleanup_ok = False
        if listener is not None:
            listener.close()
        # Preserve the DB if shutdown could not be confirmed; never remove an
        # active runtime's storage (mkdtemp has no automatic finalizer).
        if temporary is not None and runtime_stopped:
            shutil.rmtree(temporary)
        if ssh is not None:
            ssh.__exit__(None, None, None)
        report["cleanup_complete"] = cleanup_ok
        report["success"] = report["success"] and cleanup_ok
        serialized = json.dumps(report, indent=2)
        assert not any(value and value in serialized for value in sensitive), (
            "Refusing secret-bearing artifact"
        )
        (args.artifacts / "report.json").write_text(serialized + "\n")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
