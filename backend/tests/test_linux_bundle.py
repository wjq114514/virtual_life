import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

from virtual_life.linux_bundle import client_artifacts, get_linux_bundle

ROOT = Path(__file__).resolve().parents[2]


def test_resources_match_source():
    source = ROOT / "clients/system-node"
    resource = ROOT / "backend/virtual_life/client_assets/system_node"
    for path in [source / "installer.py", *(source / "system_node").glob("*.py")]:
        assert path.read_bytes() == (resource / path.relative_to(source)).read_bytes()


def test_reproducible_bundle_and_manifest():
    bundle = get_linux_bundle()
    assert bundle == get_linux_bundle()
    assert hashlib.sha256(bundle.data).hexdigest() == bundle.sha256
    with tarfile.open(fileobj=io.BytesIO(bundle.data)) as archive:
        assert set(archive.getnames()) == {"manifest.json", "system-node.pyz", "installer.py"}


def test_zipapp_smoke_and_offline_validation(tmp_path):
    pyz = tmp_path / "node.pyz"
    pyz.write_bytes(client_artifacts()["system-node.pyz"])
    result = subprocess.run([sys.executable, str(pyz), "--version"], capture_output=True)
    assert result.returncode == 0
    assert result.stdout.strip() == b"0.1.0"
    config = tmp_path / "config.json"
    config.write_text('{"server_url":"http://127.0.0.1:1","node_id":"node","label":"Node"}')
    result = subprocess.run(
        [sys.executable, str(pyz), "--config", str(config), "--check-config"],
        capture_output=True,
        env={},
    )
    assert result.returncode == 0
    config.write_text("{}")
    result = subprocess.run(
        [sys.executable, str(pyz), "--config", str(config), "--check-config"], capture_output=True
    )
    assert result.returncode == 2


def test_state_lock_prevents_client_start_before_network(tmp_path):
    import fcntl

    pyz = tmp_path / "node.pyz"
    pyz.write_bytes(client_artifacts()["system-node.pyz"])
    config = tmp_path / "config.json"
    config.write_text('{"server_url":"http://127.0.0.1:1","node_id":"node","label":"Node"}')
    with (tmp_path / "reporter.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                sys.executable,
                str(pyz),
                "--config",
                str(config),
                "--state-dir",
                str(tmp_path),
                "--once",
            ],
            capture_output=True,
            env={"VIRTUAL_LIFE_API_TOKEN": "fake"},
        )
    assert result.returncode == 2
    assert b"state_locked" in result.stderr
    assert b"Traceback" not in result.stderr
