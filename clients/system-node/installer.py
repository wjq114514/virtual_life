"""Root-only, standard-library system service installer. Never accepts secrets in argv."""

import argparse
import fcntl
import gzip
import hashlib
import io
import json
import os
import pwd
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

MAX_BUNDLE = 4 * 1024 * 1024
SERVICE = "virtual-life-system-node.service"


class InstallError(Exception):
    pass


def bounded(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise InstallError("input_too_large")
    return data


def safe_path(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InstallError("unsafe_path")
    if path.exists() and not (path.is_dir() or path.is_file()):
        raise InstallError("unsafe_path")


def atomic(path, data, mode=0o644):
    safe_path(path)
    fd, name = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_command(args):
    subprocess.run(
        args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45
    )


def unpack(bundle):
    data = bounded(bundle, MAX_BUNDLE)
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        expanded = stream.read(MAX_BUNDLE + 1)
    if len(expanded) > MAX_BUNDLE:
        raise InstallError("bundle_invalid")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        members = archive.getmembers()
        expected = {"manifest.json", "system-node.pyz", "installer.py"}
        if len(members) != 3 or {m.name for m in members} != expected:
            raise InstallError("bundle_invalid")
        files = {}
        for member in members:
            if not member.isfile() or not 0 < member.size <= MAX_BUNDLE:
                raise InstallError("bundle_invalid")
            files[member.name] = archive.extractfile(member).read(MAX_BUNDLE + 1)
    manifest = json.loads(files["manifest.json"])
    version = manifest["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise InstallError("bundle_invalid")
    for name in ("system-node.pyz", "installer.py"):
        if hashlib.sha256(files[name]).hexdigest() != manifest["sha256"][name]:
            raise InstallError("bundle_invalid")
    return version, files["system-node.pyz"]


def install(bundle, config_file, token_file, *, root=Path("/"), runner=run_command, account=None):
    """root and runner are test seams; production CLI always uses fixed system paths."""
    base = root / "opt/virtual-life-system-node"
    config_dir = root / "etc/virtual-life-system-node"
    state = root / "var/lib/virtual-life-system-node"
    unit = root / "etc/systemd/system" / SERVICE
    for path in (base, config_dir, state, unit):
        safe_path(path)
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / "install.lock"
    safe_path(lock_path)
    lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _install(
            bundle, config_file, token_file, base, config_dir, state, unit, runner, account
        )
    finally:
        os.close(lock)


def _install(bundle, config_file, token_file, base, config_dir, state, unit, runner, account):
    version, pyz = unpack(bundle)
    raw = bounded(config_file, 65536)
    token = bounded(token_file, 4096).decode("ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", token):
        raise InstallError("token_invalid")
    config = json.loads(raw)
    if (
        not isinstance(config, dict)
        or config.get("token_env", "VIRTUAL_LIFE_API_TOKEN") != "VIRTUAL_LIFE_API_TOKEN"
    ):
        raise InstallError("config_invalid")
    config["token_env"] = "VIRTUAL_LIFE_API_TOKEN"
    config_dir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    unit.parent.mkdir(parents=True, exist_ok=True)
    versions = base / "versions"
    safe_path(versions)
    versions.mkdir(exist_ok=True)
    destination = versions / (version + "-" + hashlib.sha256(pyz).hexdigest()[:16])
    safe_path(destination)
    destination.mkdir(exist_ok=True)
    executable = destination / "system-node.pyz"
    if executable.exists() and bounded(executable, MAX_BUNDLE) != pyz:
        raise InstallError("version_conflict")
    atomic(executable, pyz)
    # Validate offline using the exact packaged parser, without loading a token or collecting samples.
    with tempfile.NamedTemporaryFile(dir=config_dir, suffix=".json") as candidate:
        candidate.write(raw)
        candidate.flush()
        runner([sys.executable, str(executable), "--config", candidate.name, "--check-config"])
    raw = json.dumps(config, ensure_ascii=True).encode()
    config_path = config_dir / "config.json"
    token_path = config_dir / "token.env"
    for path in (config_path, token_path):
        safe_path(path)
    if config_path.exists():
        old_config = json.loads(bounded(config_path, 65536))
        if any(old_config.get(key) != config.get(key) for key in ("node_id", "server_url")):
            raise InstallError("identity_mismatch")
    current = base / "current"
    previous = None
    if current.is_symlink():
        previous = os.readlink(current)
        if not re.fullmatch(r"versions/[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{16}", previous):
            raise InstallError("unsafe_path")
    elif current.exists():
        raise InstallError("unsafe_path")
    if account is None:
        try:
            account = pwd.getpwnam("virtual-life")
        except KeyError:
            runner(
                [
                    "useradd",
                    "--system",
                    "--user-group",
                    "--no-create-home",
                    "--home-dir",
                    "/var/lib/virtual-life-system-node",
                    "--shell",
                    "/usr/sbin/nologin",
                    "virtual-life",
                ]
            )
            account = pwd.getpwnam("virtual-life")
    if account.pw_uid == 0:
        raise InstallError("account_invalid")
    os.chown(state, account.pw_uid, account.pw_gid)
    os.chmod(state, 0o700)
    unit_data = (
        "[Unit]\nDescription=Virtual Life system node\nAfter=network-online.target\n"
        f"[Service]\nType=notify\nNotifyAccess=main\nTimeoutStartSec=30\nUser=virtual-life\nGroup={account.pw_gid}\n"
        "EnvironmentFile=/etc/virtual-life-system-node/token.env\n"
        f"ExecStart={sys.executable} /opt/virtual-life-system-node/current/system-node.pyz --config /etc/virtual-life-system-node/config.json --state-dir /var/lib/virtual-life-system-node\n"
        "Restart=on-failure\nRestartPreventExitStatus=2 3\nRestartSec=5\nNoNewPrivileges=true\nPrivateTmp=true\nUMask=0077\nProtectSystem=strict\nProtectHome=true\nReadWritePaths=/var/lib/virtual-life-system-node\n"
        "[Install]\nWantedBy=multi-user.target\n"
    ).encode()
    backups = {
        path: bounded(path, 65536) if path.exists() else None
        for path in (config_path, token_path, unit)
    }
    transaction_started = False
    start_attempted = False

    def switch(target):
        pending = base / ".current-next"
        if pending.exists() or pending.is_symlink():
            raise InstallError("unsafe_path")
        pending.symlink_to(target)
        os.replace(pending, current)

    try:
        if previous or backups[unit] is not None:
            runner(["systemctl", "stop", SERVICE])
        transaction_started = True
        atomic(config_path, raw)
        atomic(token_path, ("VIRTUAL_LIFE_API_TOKEN=" + token + "\n").encode(), 0o600)
        atomic(unit, unit_data)
        switch("versions/" + destination.name)
        runner(["systemctl", "daemon-reload"])
        runner(["systemctl", "enable", SERVICE])
        start_attempted = True
        runner(["systemctl", "start", SERVICE])
        runner(["systemctl", "is-active", "--quiet", SERVICE])
    except Exception as exc:
        if transaction_started:
            try:
                if start_attempted:
                    runner(["systemctl", "stop", SERVICE])
                for path, data in backups.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, data, 0o600 if path == token_path else 0o644)
                if previous:
                    switch(previous)
                else:
                    current.unlink(missing_ok=True)
                runner(["systemctl", "daemon-reload"])
                if previous:
                    runner(["systemctl", "start", SERVICE])
            except Exception:
                raise InstallError("rollback_failed") from None
        raise InstallError("service_failed") from exc
    return {"node_id": config["node_id"], "version": version, "status": "installed"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--config-file", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0 or sys.version_info < (3, 12):
            raise InstallError("root_python312_required")
        result = install(args.bundle, args.config_file, args.token_file)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, InstallError) else "install_failed"
        print(json.dumps({"status": "failed", "reason": reason}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
