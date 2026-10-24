"""Readiness uses real Unix datagrams after successful reporting, never a timer."""

import importlib
import importlib.util
import json
import socket
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "clients/system-node/system_node"


@pytest.fixture(scope="module")
def client():
    name = "readiness_test_system_node"
    spec = importlib.util.spec_from_file_location(
        name, SOURCE / "__init__.py", submodule_search_locations=[str(SOURCE)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return SimpleNamespace(
        **{
            name: importlib.import_module(f"{package.__name__}.{name}")
            for name in ("runtime", "notify", "config", "cli", "transport")
        }
    )


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        address = str(tmp_path / "notify.sock")
        channel.bind(address)
        channel.setblocking(False)
        monkeypatch.setenv("NOTIFY_SOCKET", address)
        yield channel


def empty(receiver):
    with pytest.raises(BlockingIOError):
        receiver.recv(4096)


@pytest.fixture
def configured(client, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"server_url": "http://127.0.0.1:1", "node_id": "node", "label": "Node"})
    )
    config = client.config.load_config(path)
    # Keep the real reporting cycle but never inspect the host's devices.
    collector = SimpleNamespace(collect=lambda: ({"quality": "fresh"}, {"status": "reachable"}, {}))
    monkeypatch.setattr(client.runtime, "LocalCollector", lambda disk: collector)
    return config, path


class StopAfterWait:
    def __init__(self, on_wait):
        self.event = threading.Event()
        self.on_wait = on_wait
        self.waits = 0

    def is_set(self):
        return self.event.is_set()

    def wait(self, delay):
        self.waits += 1
        self.on_wait(self, delay)
        return self.is_set()


def test_ready_after_real_successful_cycle_once_only(client, configured, receiver):
    calls = []

    def post(path, body):
        empty(receiver)
        calls.append(path)
        return {}

    def wait(stop, delay):
        if stop.waits == 1:
            assert calls[0] == "/v1/nodes"
            assert "/v1/nodes/node/heartbeat" in calls
            assert receiver.recv(4096) == b"READY=1"
        else:
            empty(receiver)
            stop.event.set()

    stop = StopAfterWait(wait)
    runtime = client.runtime.Runtime(
        configured[0], SimpleNamespace(post=post, post_encoded=post), stop
    )
    assert runtime.run(once=False) == 0
    assert stop.waits == 2
    assert calls.count("/v1/nodes/node/heartbeat") == 2
    empty(receiver)


def test_retry_does_not_notify_until_success(client, configured, receiver):
    heartbeat_attempts = 0

    def post(path, body):
        nonlocal heartbeat_attempts
        if path.endswith("heartbeat"):
            heartbeat_attempts += 1
            if heartbeat_attempts == 1:
                raise client.transport.TransportError("unavailable", retryable=True)
        return {}

    def wait(stop, delay):
        if stop.waits == 1:
            empty(receiver)
        else:
            assert receiver.recv(4096) == b"READY=1"
            stop.event.set()

    stop = StopAfterWait(wait)
    runtime = client.runtime.Runtime(
        configured[0], SimpleNamespace(post=post, post_encoded=post), stop
    )
    assert runtime.run(once=False) == 0
    assert heartbeat_attempts == 2


@pytest.mark.parametrize("mode", ["once", "fatal", "stopped", "stop_during_cycle"])
def test_no_readiness_for_once_failed_or_stopped(client, configured, receiver, mode):
    stop = threading.Event()
    api = SimpleNamespace(post=lambda *args: {}, post_encoded=lambda *args: {})
    runtime = client.runtime.Runtime(configured[0], api, stop)
    if mode == "fatal":

        def fail():
            raise client.transport.TransportError("unauthorized", status=401)

        runtime._cycle = fail
        with pytest.raises(client.transport.TransportError):
            runtime.run(once=False)
    else:
        if mode == "stopped":
            stop.set()
        elif mode == "stop_during_cycle":
            runtime._cycle = stop.set
        assert runtime.run(once=mode == "once") == 0
    empty(receiver)


def test_check_config_never_notifies_or_reads_token(client, configured, receiver, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline config check must not read credentials or start runtime")

    monkeypatch.setattr(client.cli, "read_token", forbidden)
    monkeypatch.setattr(client.cli, "Runtime", forbidden)
    assert client.cli.main(["--config", str(configured[1]), "--check-config"]) == 0
    empty(receiver)


def test_abstract_socket_supported(client, monkeypatch):
    address = "virtual-life-readiness-test-" + uuid.uuid4().hex
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.bind("\0" + address)
        channel.setblocking(False)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + address)
        assert client.notify.notify_ready()
        assert channel.recv(4096) == b"READY=1"


def test_missing_environment_does_not_open_socket(client, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("No systemd environment must not create a socket")

    monkeypatch.setattr(client.notify.socket, "socket", forbidden)
    assert not client.notify.notify_ready()


def test_failed_send_closes_socket_and_retries_until_sent(
    client, configured, receiver, monkeypatch, tmp_path
):
    real_socket = socket.socket
    created = []

    def tracked(*args, **kwargs):
        channel = real_socket(*args, **kwargs)
        created.append(channel)
        return channel

    monkeypatch.setattr(client.notify.socket, "socket", tracked)
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "missing.sock"))

    def wait(stop, delay):
        if stop.waits == 1:
            assert not runtime._ready_sent
            empty(receiver)
            monkeypatch.setenv("NOTIFY_SOCKET", receiver.getsockname())
        else:
            assert runtime._ready_sent
            assert receiver.recv(4096) == b"READY=1"
            stop.event.set()

    stop = StopAfterWait(wait)
    runtime = client.runtime.Runtime(
        configured[0], SimpleNamespace(post=lambda *args: {}, post_encoded=lambda *args: {}), stop
    )
    assert runtime.run(once=False) == 0
    assert len(created) == 2 and all(channel.fileno() == -1 for channel in created)
    empty(receiver)


def test_heartbeat_without_completed_cycle_does_not_notify(client, configured, receiver):
    calls = []

    def post(path, body):
        calls.append(path)
        return {}

    def wait(stop, delay):
        assert "/v1/nodes/node/heartbeat" in calls
        empty(receiver)
        stop.event.set()

    stop = StopAfterWait(wait)
    runtime = client.runtime.Runtime(configured[0], SimpleNamespace(post=post), stop)

    def incomplete():
        raise client.transport.TransportError("observation_failed", retryable=True)

    runtime._flush = incomplete
    assert runtime.run(once=False) == 0
    assert not runtime._ready_sent
    empty(receiver)
