"""Pinned SSH installation jobs; credentials exist only in the submitting worker."""

import base64
import hashlib
import io
import json
import re
import shlex
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import urlsplit

import paramiko
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .coordinator import DomainError
from .linux_bundle import get_linux_bundle
from .models import Identifier

MESSAGES = {
    "ssh_connection_failed": "SSH connection failed; check the host and port",
    "ssh_auth_failed": "SSH authentication failed",
    "ssh_host_key_changed": "SSH host key does not match the verified fingerprint",
    "ssh_key_invalid": "SSH private key or passphrase is invalid",
    "remote_check_failed": "Remote check failed; require Python 3.12+, systemd and root or passwordless sudo",
    "remote_not_ready": "Remote requirements are not satisfied",
    "remote_identity_changed": "Remote node identity differs from this request",
    "remote_server_changed": "Remote installation belongs to another server",
    "remote_install_failed": "Installer failed; inspect the target service before retrying",
    "remote_outcome_unknown": "SSH disconnected or timed out; remote installation outcome is unknown",
    "node_not_reporting": "Installation returned successfully but a new reporter heartbeat was not verified",
    "deployment_interrupted": "Server restarted; inspect the target before starting another deployment",
    "deployment_failed": "Deployment failed; inspect the target before retrying",
}


def fail(code):
    raise DomainError(502, code, MESSAGES.get(code, MESSAGES["deployment_failed"]))


class SSHHost(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def valid_host(cls, value):
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", value):
            raise ValueError("Use an IP address or ASCII hostname without URL or whitespace")
        return value


class SSHAccess(SSHHost):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")
    password: SecretStr | None = Field(default=None, repr=False)
    private_key: SecretStr | None = Field(default=None, repr=False)
    passphrase: SecretStr | None = Field(default=None, repr=False)
    host_key_fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    python: str = Field(default="python3", max_length=256)

    @field_validator("python")
    @classmethod
    def valid_python(cls, value):
        if not re.fullmatch(r"(?:/[a-zA-Z0-9_.+-]+)*/?[a-zA-Z0-9_+-][a-zA-Z0-9_.+-]*", value):
            raise ValueError("Use a Python program name or absolute path")
        if value.startswith("-"):
            raise ValueError("Python program must not be an option")
        if "/" in value and not value.startswith("/"):
            raise ValueError("Python path must be absolute")
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("Python path must not contain traversal")
        return value

    @model_validator(mode="after")
    def valid_credentials(self):
        password = self.password.get_secret_value() if self.password else ""
        key = self.private_key.get_secret_value() if self.private_key else ""
        phrase = self.passphrase.get_secret_value() if self.passphrase else ""
        if bool(password) == bool(key):
            raise ValueError("Provide exactly one SSH password or private key")
        if len(password) > 4096 or len(key.encode("utf-8")) > 65536 or len(phrase) > 4096:
            raise ValueError("SSH credential exceeds the length limit")
        if phrase and not key:
            raise ValueError("Passphrase requires a private key")
        return self


class LinuxDeploymentRequest(SSHAccess):
    command_id: Identifier
    server_url: str = Field(max_length=2048)
    label: str = Field(min_length=1, max_length=256)
    node_id: Identifier | None = None
    attention_enabled: bool = False

    @field_validator("server_url")
    @classmethod
    def valid_url(cls, value):
        try:
            value.encode("ascii")
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or "?" in value
                or "#" in value
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or "\\" in value
                or "%" in url.netloc
                or (url.port is not None and not 1 <= url.port <= 65535)
            ):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ValueError("Server URL must be an HTTP(S) origin without credentials") from None
        return value.rstrip("/")

    @field_validator("label")
    @classmethod
    def valid_label(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Label must be nonempty text without control characters")
        return value.strip()


class SSHHostKey(BaseModel):
    host: str
    port: int
    key_type: str
    fingerprint: str


class LinuxInspection(BaseModel):
    ready: bool
    python_version: str | None = None
    systemd: bool
    privileged: bool
    existing_node_id: str | None = None
    existing_server_url: str | None = None
    reason_codes: list[str]


class DeploymentErrorInfo(BaseModel):
    code: str
    message: str


class LinuxDeploymentJob(BaseModel):
    command_id: str
    status: str
    stage: str
    host: str
    port: int
    username: str
    node_id: str | None
    version: str
    created_at: str
    updated_at: str
    package_sha256: str
    credential_revoked: bool
    error: DeploymentErrorInfo | None


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class PinnedKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        if fingerprint(key) != self.expected:
            fail("ssh_host_key_changed")


# The remote scripts never accept shell fragments. Arguments are passed as JSON
# files or individually shell-quoted program/path arguments.
INSPECT_SCRIPT = r"""
import json, os, pathlib, shutil, stat, sys
reasons=[]
linux=sys.platform == "linux"
systemd=pathlib.Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None
if not linux: reasons.append("linux_required")
if sys.version_info < (3,12): reasons.append("python_312_required")
if not systemd: reasons.append("systemd_required")
if os.geteuid() != 0: reasons.append("privilege_required")
existing={}
path=pathlib.Path("/etc/virtual-life-system-node/config.json")
try:
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        reasons.append("unsafe_existing_config")
    else:
        existing=json.loads(path.read_bytes())
        if not isinstance(existing,dict): raise ValueError()
        if not isinstance(existing.get("node_id"),str) or not isinstance(existing.get("server_url"),str):
            raise ValueError()
except FileNotFoundError: pass
except Exception: reasons.append("existing_config_invalid")
print(json.dumps(dict(ready=not reasons,python_version=".".join(map(str,sys.version_info[:3])),
    systemd=systemd,privileged=os.geteuid()==0,existing_node_id=existing.get("node_id"),
    existing_server_url=existing.get("server_url"),reason_codes=reasons)))
"""

EXTRACT_SCRIPT = r"""
import hashlib, pathlib, sys, tarfile
path=pathlib.Path(sys.argv[1]); expected=sys.argv[2]
with path.open("rb") as stream: data=stream.read(4194305)
if len(data)>4194304 or hashlib.sha256(data).hexdigest()!=expected: raise ValueError("bundle_invalid")
with tarfile.open(path,"r:gz") as bundle:
    member=bundle.getmember("installer.py")
    if not member.isfile() or member.size>262144: raise ValueError("installer_invalid")
    source=bundle.extractfile(member).read(262145)
target=path.parent/"installer.py"
with target.open("xb") as out: out.write(source)
"""


class SSHSession:
    def __init__(self, access):
        self.access = access
        self.client = None
        self.prefix = None

    def __enter__(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(PinnedKey(self.access.host_key_fingerprint))
        self.client = client
        key = None
        try:
            if self.access.private_key:
                phrase = (
                    self.access.passphrase.get_secret_value() if self.access.passphrase else None
                )
                for kind in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                    try:
                        key = kind.from_private_key(
                            io.StringIO(self.access.private_key.get_secret_value()), password=phrase
                        )
                        break
                    except (ValueError, paramiko.SSHException):
                        pass
                if key is None:
                    fail("ssh_key_invalid")
            client.connect(
                hostname=self.access.host,
                port=self.access.port,
                username=self.access.username,
                password=self.access.password.get_secret_value() if self.access.password else None,
                pkey=key,
                allow_agent=False,
                look_for_keys=False,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10,
                channel_timeout=10,
            )
        except DomainError:
            client.close()
            raise
        except paramiko.AuthenticationException:
            client.close()
            fail("ssh_auth_failed")
        except Exception:
            client.close()
            fail("ssh_connection_failed")
        return self

    def __exit__(self, *args):
        with suppress(Exception):
            self.client.close()

    def run(self, argv, timeout=25):
        channel = None
        timer = None
        expired = threading.Event()
        output, errors = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            channel = self.client.get_transport().open_session(timeout=min(10, timeout))
            channel.settimeout(timeout)

            def expire():
                # Paramiko exec_command waits for an acknowledgement without
                # consulting Channel.timeout. Closing releases that event wait.
                expired.set()
                with suppress(Exception):
                    channel.close()

            timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            channel.exec_command(shlex.join(argv))
            while True:
                if expired.is_set() or time.monotonic() > deadline:
                    fail("remote_outcome_unknown")
                while channel.recv_ready():
                    output.extend(channel.recv(4096))
                    if len(output) > 131072:
                        fail("remote_check_failed")
                while channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(4096))
                    if len(errors) > 131072:
                        fail("remote_check_failed")
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    status = channel.recv_exit_status()
                    if status < 0 or expired.is_set() or time.monotonic() > deadline:
                        fail("remote_outcome_unknown")
                    return status, bytes(output)
                time.sleep(0.02)
        except DomainError:
            raise
        except Exception:
            fail("remote_outcome_unknown")
        finally:
            if timer is not None:
                timer.cancel()
            if channel is not None:
                with suppress(Exception):
                    channel.close()

    def inspect(self):
        status, uid = self.run(["id", "-u"])
        if status:
            fail("remote_check_failed")
        self.prefix = [] if uid.strip() == b"0" else ["sudo", "-n"]
        status, raw = self.run([*self.prefix, self.access.python, "-c", INSPECT_SCRIPT])
        if status:
            fail("remote_check_failed")
        try:
            return LinuxInspection.model_validate_json(raw).model_dump()
        except Exception:
            fail("remote_check_failed")

    def install(self, bundle, config, token):
        status, raw = self.run(["mktemp", "-d", "/tmp/virtual-life-deploy.XXXXXXXXXX"])
        directory = raw.decode("ascii", errors="replace").strip()
        if status or not re.fullmatch(r"/tmp/virtual-life-deploy\.[A-Za-z0-9]{10}", directory):
            fail("remote_check_failed")
        outcome_known = False
        installer_started = False
        try:
            with self.client.open_sftp() as sftp:
                sftp.get_channel().settimeout(15)
                for name, data in (
                    ("bundle.tar.gz", bundle.data),
                    ("config.json", json.dumps(config, ensure_ascii=False).encode()),
                    ("token.txt", token.encode("ascii")),
                ):
                    with sftp.open(directory + "/" + name, "wx") as target:
                        target.settimeout(15)
                        target.write(data)
                    sftp.chmod(directory + "/" + name, 0o600)
            status, _ = self.run(
                [
                    self.access.python,
                    "-c",
                    EXTRACT_SCRIPT,
                    directory + "/bundle.tar.gz",
                    bundle.sha256,
                ]
            )
            if status:
                fail("remote_install_failed")
            installer_started = True
            status, raw = self.run(
                [
                    *self.prefix,
                    self.access.python,
                    directory + "/installer.py",
                    "--bundle",
                    directory + "/bundle.tar.gz",
                    "--config-file",
                    directory + "/config.json",
                    "--token-file",
                    directory + "/token.txt",
                ],
                timeout=180,
            )
            outcome_known = True
            if status:
                fail("remote_install_failed")
            try:
                result = json.loads(raw)
                if (
                    result["status"] != "installed"
                    or result["node_id"] != config["node_id"]
                    or result["version"] != bundle.version
                ):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                fail("remote_outcome_unknown")
        except DomainError:
            raise
        except Exception:
            fail("remote_outcome_unknown" if installer_started else "remote_install_failed")
        finally:
            # An interrupted installer may still be using its input files.
            if not installer_started or outcome_known:
                try:
                    self.run(["rm", "-rf", "--", directory], timeout=10)
                except Exception:
                    pass


def probe_host_key(host):
    try:
        with socket.create_connection((host.host, host.port), timeout=10) as sock:
            with paramiko.Transport(sock) as transport:
                transport.start_client(timeout=10)
                key = transport.get_remote_server_key()
                return {
                    "host": host.host,
                    "port": host.port,
                    "key_type": key.get_name(),
                    "fingerprint": fingerprint(key),
                }
    except Exception:
        fail("ssh_connection_failed")


class LinuxDeploymentService:
    def __init__(
        self, coordinator, credentials, *, connector=SSHSession, bundle_factory=get_linux_bundle
    ):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.credentials = credentials
        self.connector = connector
        self.bundle_factory = bundle_factory
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="linux-deploy")
        self.lock = threading.Lock()
        self.closed = False
        with self.store.transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS linux_deployments (command_id TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, target TEXT NOT NULL, record_json TEXT NOT NULL)"
            )
            rows = connection.execute(
                "SELECT command_id, record_json FROM linux_deployments"
            ).fetchall()
            for row in rows:
                record = json.loads(row["record_json"])
                if record["status"] in {"queued", "running"}:
                    record.update(
                        status="interrupted",
                        stage="failed",
                        updated_at=self._now(),
                        error={
                            "code": "deployment_interrupted",
                            "message": MESSAGES["deployment_interrupted"],
                        },
                    )
                    connection.execute(
                        "UPDATE linux_deployments SET record_json=? WHERE command_id=?",
                        (json.dumps(record), row["command_id"]),
                    )

    def _now(self):
        return datetime.now(UTC).isoformat()

    def inspect(self, access):
        with self.connector(access) as ssh:
            return ssh.inspect()

    def get(self, command_id):
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM linux_deployments WHERE command_id=?", (command_id,)
            ).fetchone()
        if row is None:
            raise DomainError(404, "not_found", "Deployment not found")
        record = json.loads(row["record_json"])
        record["credential_revoked"] = self.credentials.is_revoked(command_id)
        return record

    def update(self, command_id, **changes):
        with self.store.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT record_json FROM linux_deployments WHERE command_id=?", (command_id,)
            ).fetchone()
            record = json.loads(row["record_json"])
            record.update(updated_at=self._now(), **changes)
            connection.execute(
                "UPDATE linux_deployments SET record_json=? WHERE command_id=?",
                (json.dumps(record), command_id),
            )

    def submit(self, request):
        bundle = self.bundle_factory()
        public = request.model_dump(mode="json", exclude={"password", "private_key", "passphrase"})
        digest = hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()
        # Pinning prevents DNS aliases from redirecting authentication; target
        # locking prevents concurrent installs through the same host/port.
        target = json.dumps([request.host.lower(), request.port])
        command_id = request.command_id
        with self.lock:
            if self.closed:
                raise DomainError(503, "deployment_unavailable", "Deployment service is stopping")
            with self.store.transaction(write=True) as connection:
                previous = connection.execute(
                    "SELECT fingerprint, record_json FROM linux_deployments WHERE command_id=?",
                    (command_id,),
                ).fetchone()
                if previous:
                    if previous["fingerprint"] != digest:
                        raise DomainError(
                            409,
                            "idempotency_conflict",
                            "Deployment ID was used for another request",
                        )
                    return self.get(command_id)
                active = connection.execute(
                    "SELECT target, record_json FROM linux_deployments"
                ).fetchall()
                pending = [
                    row
                    for row in active
                    if json.loads(row["record_json"])["status"] in {"queued", "running"}
                ]
                if any(row["target"] == target for row in pending):
                    raise DomainError(
                        409, "deployment_busy", "This SSH target already has an active deployment"
                    )
                if len(pending) >= 4:
                    raise DomainError(429, "deployment_busy", "Deployment queue is full")
                record = {
                    "command_id": command_id,
                    "status": "queued",
                    "stage": "queued",
                    "host": request.host,
                    "port": request.port,
                    "username": request.username,
                    "node_id": None,
                    "version": bundle.version,
                    "package_sha256": bundle.sha256,
                    "created_at": self._now(),
                    "updated_at": self._now(),
                    "credential_revoked": False,
                    "error": None,
                }
                connection.execute(
                    "INSERT INTO linux_deployments VALUES (?,?,?,?)",
                    (command_id, digest, target, json.dumps(record)),
                )
            self.executor.submit(self._execute, request, bundle)
        return record

    def _execute(self, request, bundle):
        command_id = request.command_id
        try:
            self.update(command_id, status="running", stage="connecting")
            with self.connector(request) as ssh:
                self.update(command_id, stage="checking")
                inspection = ssh.inspect()
                if not inspection["ready"]:
                    fail("remote_not_ready")
                old_node = inspection["existing_node_id"]
                old_server = inspection["existing_server_url"]
                if old_server and old_server.rstrip("/") != request.server_url:
                    fail("remote_server_changed")
                if old_node and request.node_id and request.node_id != old_node:
                    fail("remote_identity_changed")
                node_id = old_node or request.node_id or "linux-" + uuid.uuid4().hex
                # Scoped credential creation rejects existing display/Web nodes.
                issued = self.credentials.issue(
                    node_id, command_id, attention_enabled=request.attention_enabled
                )
                self.update(command_id, node_id=node_id, stage="uploading")
                config = {
                    "node_id": node_id,
                    "label": request.label,
                    "server_url": request.server_url,
                    "device_class": "server",
                    "deployment_mode": "direct",
                    "token_env": "VIRTUAL_LIFE_API_TOKEN",
                }
                if request.attention_enabled:
                    config["attention"] = {"enabled": True}
                self.update(command_id, stage="installing")
                ssh.install(bundle, config, issued["token"])
                self.update(command_id, stage="verifying")
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    # The service acknowledges this exact credential's usage, not
                    # an unrelated old heartbeat from a previous installation.
                    if self._reported(issued["credential_id"]):
                        self.update(command_id, status="completed", stage="completed")
                        return
                    time.sleep(0.25)
                fail("node_not_reporting")
        except DomainError as exc:
            self.update(
                command_id,
                status="failed",
                stage="failed",
                error={
                    "code": exc.code,
                    "message": MESSAGES.get(
                        exc.code,
                        "Deployment rejected; inspect node identity and target configuration",
                    ),
                },
            )
        except Exception:
            self.update(
                command_id,
                status="failed",
                stage="failed",
                error={"code": "deployment_failed", "message": MESSAGES["deployment_failed"]},
            )

    def _reported(self, credential_id):
        # Filled by the credentials integration; a matching heartbeat must be
        # observed before the job may claim deployment success.
        return self.credentials.has_reported(credential_id)

    def revoke(self, command_id):
        record = self.get(command_id)
        if record["status"] in {"queued", "running"}:
            raise DomainError(
                409,
                "deployment_in_progress",
                "Wait for the deployment before revoking its credential",
            )
        self.credentials.revoke_for_deployment(command_id)
        return self.get(command_id)

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True)
