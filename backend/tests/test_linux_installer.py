import importlib.util
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from virtual_life.linux_bundle import get_linux_bundle

MODULE_PATH = Path(__file__).resolve().parents[2] / "clients/system-node/installer.py"
spec = importlib.util.spec_from_file_location("linux_installer", MODULE_PATH)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(get_linux_bundle().data)
    config = tmp_path / "input.json"
    config.write_text(
        json.dumps({"server_url": "http://localhost:8000", "node_id": "node-a", "label": "Node"})
    )
    token = tmp_path / "token"
    token.write_text("url_safe-token_123")
    monkeypatch.setattr(installer.os, "chown", lambda *args: None)
    return tmp_path / "root", bundle, config, token


def invoke(setup, runner=lambda args: None):
    root, bundle, config, token = setup
    return installer.install(
        bundle,
        config,
        token,
        root=root,
        runner=runner,
        account=SimpleNamespace(pw_uid=123, pw_gid=123),
    )


def test_install_private_token_and_fixed_service(setup):
    commands = []
    assert invoke(setup, commands.append)["status"] == "installed"
    root = setup[0]
    token = root / "etc/virtual-life-system-node/token.env"
    assert token.stat().st_mode & 0o777 == 0o600
    assert (root / "opt/virtual-life-system-node/current").is_symlink()
    assert not any("url_safe-token" in str(command) for command in commands)
    assert ["systemctl", "stop", installer.SERVICE] not in commands
    unit = (root / "etc/systemd/system" / installer.SERVICE).read_text()
    for setting in (
        "Group=123",
        "Type=notify",
        "NotifyAccess=main",
        "TimeoutStartSec=30",
        "RestartPreventExitStatus=2 3",
    ):
        assert setting + "\n" in unit


def test_upgrade_preserves_identity_and_rolls_back(setup):
    invoke(setup)
    root, _, config, token = setup
    current = root / "opt/virtual-life-system-node/current"
    original = os.readlink(current)
    old_token = (root / "etc/virtual-life-system-node/token.env").read_bytes()
    config_path = root / "etc/virtual-life-system-node/config.json"
    old_config = config_path.read_bytes()
    changed = json.loads(config.read_text())
    changed["label"] = "New label"
    config.write_text(json.dumps(changed))
    token.write_text("new_token")

    def fail_start(args):
        if args[:2] == ["systemctl", "is-active"]:
            raise RuntimeError("secret_must_not_escape")

    with pytest.raises(installer.InstallError, match="service_failed"):
        invoke(setup, fail_start)
    assert os.readlink(current) == original
    assert (root / "etc/virtual-life-system-node/token.env").read_bytes() == old_token
    assert config_path.read_bytes() == old_config
    data = json.loads(config.read_text())
    data["node_id"] = "other"
    config.write_text(json.dumps(data))
    with pytest.raises(installer.InstallError, match="identity_mismatch"):
        invoke(setup)


def test_reject_symlink_and_oversize(setup, tmp_path):
    root = setup[0]
    (root / "opt").mkdir(parents=True)
    (root / "opt/virtual-life-system-node").symlink_to(tmp_path)
    with pytest.raises(installer.InstallError, match="unsafe_path"):
        invoke(setup)
    setup[1].write_bytes(b"x" * (installer.MAX_BUNDLE + 1))
    with pytest.raises(installer.InstallError, match="input_too_large"):
        installer.unpack(setup[1])


def test_reject_tar_traversal(setup):
    with tarfile.open(setup[1], "w:gz") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(installer.InstallError, match="bundle_invalid"):
        installer.unpack(setup[1])


def test_reject_hash_mismatch(setup):
    root, bundle, _, _ = setup
    with tarfile.open(bundle) as archive:
        entries = {member.name: archive.extractfile(member).read() for member in archive}
    entries["system-node.pyz"] += b"corrupt"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    with pytest.raises(installer.InstallError, match="bundle_invalid"):
        invoke(setup)
    assert not (root / "etc/virtual-life-system-node/config.json").exists()


def test_install_lock_rejects_concurrent_installer(setup):
    import fcntl

    base = setup[0] / "opt/virtual-life-system-node"
    base.mkdir(parents=True)
    with (base / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            invoke(setup)


def test_first_install_does_not_stop_nonexistent_unit(setup):
    unit = setup[0] / "etc/systemd/system" / installer.SERVICE
    commands = []

    def realistic_runner(args):
        commands.append(args)
        if args[:2] == ["systemctl", "stop"] and not unit.exists():
            raise RuntimeError("Unit not found")

    assert invoke(setup, realistic_runner)["status"] == "installed"
    assert ["systemctl", "stop", installer.SERVICE] not in commands


def test_upgrade_stops_before_atomic_current_switch(setup, monkeypatch):
    invoke(setup)
    commands = []
    original_replace = installer.os.replace
    current = setup[0] / "opt/virtual-life-system-node/current"

    def replace(source, target):
        if Path(target) == current:
            assert ["systemctl", "stop", installer.SERVICE] in commands
        return original_replace(source, target)

    monkeypatch.setattr(installer.os, "replace", replace)
    assert invoke(setup, commands.append)["status"] == "installed"
    assert commands.index(["systemctl", "stop", installer.SERVICE]) < commands.index(
        ["systemctl", "start", installer.SERVICE]
    )


def test_failed_first_start_stops_new_service_and_removes_new_config(setup):
    root = setup[0]
    unit = root / "etc/systemd/system" / installer.SERVICE
    commands = []

    def runner(args):
        commands.append(args)
        if args[:2] == ["systemctl", "stop"]:
            assert unit.exists(), "cannot stop an unknown first-install unit"
        if args[:2] == ["systemctl", "start"]:
            raise RuntimeError("failed first startup")

    with pytest.raises(installer.InstallError, match="service_failed"):
        invoke(setup, runner)
    assert commands.index(["systemctl", "start", installer.SERVICE]) < commands.index(
        ["systemctl", "stop", installer.SERVICE]
    )
    assert not unit.exists()
    assert not (root / "etc/virtual-life-system-node/config.json").exists()
    assert not (root / "etc/virtual-life-system-node/token.env").exists()
    assert not (root / "opt/virtual-life-system-node/current").exists()


def test_user_creation_requests_dedicated_group(setup, monkeypatch):
    calls = []
    count = 0

    def lookup(name):
        nonlocal count
        count += 1
        if count == 1:
            raise KeyError(name)
        return SimpleNamespace(pw_uid=321, pw_gid=654)

    monkeypatch.setattr(installer.pwd, "getpwnam", lookup)
    root, bundle, config, token = setup
    installer.install(bundle, config, token, root=root, runner=calls.append)
    useradd = next(command for command in calls if command[0] == "useradd")
    assert "--user-group" in useradd
    assert "Group=654\n" in (root / "etc/systemd/system" / installer.SERVICE).read_text()


def test_notify_start_failure_restores_upgrade_files_before_restarting_old_service(setup):
    invoke(setup)
    root, _, config, token = setup
    current = root / "opt/virtual-life-system-node/current"
    config_path = root / "etc/virtual-life-system-node/config.json"
    token_path = root / "etc/virtual-life-system-node/token.env"
    unit_path = root / "etc/systemd/system" / installer.SERVICE
    original = {path: path.read_bytes() for path in (config_path, token_path, unit_path)}
    old_current = os.readlink(current)
    changed = json.loads(config.read_text())
    changed["label"] = "New revision"
    config.write_text(json.dumps(changed))
    token.write_text("new_test_token")
    starts = 0

    def runner(args):
        nonlocal starts
        if args[:2] == ["systemctl", "start"]:
            starts += 1
            if starts == 1:
                assert "Type=notify\n" in unit_path.read_text()
                assert config_path.read_bytes() != original[config_path]
                raise RuntimeError("simulated missing READY timeout or immediate process exit")
            assert os.readlink(current) == old_current
            assert all(path.read_bytes() == data for path, data in original.items())

    with pytest.raises(installer.InstallError, match="service_failed"):
        invoke(setup, runner)
    assert starts == 2
    assert all(path.read_bytes() == data for path, data in original.items())
