"""Real Paramiko handshakes bind only to loopback; no device or system service is touched."""

import socket
import threading
import time
from contextlib import contextmanager

import paramiko
import pytest
from virtual_life.coordinator import DomainError
from virtual_life.linux_deployment import (
    SSHAccess,
    SSHHost,
    SSHSession,
    fingerprint,
    probe_host_key,
)


@pytest.fixture(scope="module")
def host_key():
    return paramiko.RSAKey.generate(1024)


@contextmanager
def loopback_server(key, *, password_ok=True, hold_exec=False):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    stop = threading.Event()
    exec_seen = threading.Event()
    authenticated = []
    transports = []
    errors = []

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            authenticated.append((username, password))
            return paramiko.AUTH_SUCCESSFUL if password_ok else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, chanid):
            return (
                paramiko.OPEN_SUCCEEDED
                if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            )

        def check_channel_exec_request(self, channel, command):
            exec_seen.set()
            if hold_exec:
                stop.wait(3)
            return True

    def serve():
        try:
            connection, _ = listener.accept()
            transport = paramiko.Transport(connection)
            transports.append(transport)
            transport.add_server_key(key)
            transport.start_server(server=Server())
            stop.wait(4)
        except (OSError, EOFError, paramiko.SSHException) as exc:
            # Closing before authentication is expected for pin mismatch and probes.
            errors.append(type(exc).__name__)
        finally:
            for transport in transports:
                transport.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], authenticated, exec_seen
    finally:
        stop.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(4)
        assert not thread.is_alive()


def access(port, pin):
    return SSHAccess(
        host="127.0.0.1",
        port=port,
        username="root",
        password="LOOPBACK-ONLY-SECRET",
        host_key_fingerprint=pin,
    )


def test_actual_pin_mismatch_blocks_before_password_authentication(host_key):
    with loopback_server(host_key) as (port, authenticated, _):
        with pytest.raises(DomainError) as caught:
            with SSHSession(access(port, "SHA256:" + "A" * 43)):
                pytest.fail("pin mismatch must not establish session")
        assert caught.value.code == "ssh_host_key_changed"
        assert "LOOPBACK" not in str(caught.value)
        assert authenticated == []


def test_actual_pinned_handshake_and_probe(host_key):
    with loopback_server(host_key) as (port, authenticated, _):
        with SSHSession(access(port, fingerprint(host_key))):
            assert authenticated == [("root", "LOOPBACK-ONLY-SECRET")]
    with loopback_server(host_key) as (port, authenticated, _):
        result = probe_host_key(SSHHost(host="127.0.0.1", port=port))
        assert result["fingerprint"] == fingerprint(host_key)
        assert authenticated == []


def test_actual_auth_error_has_fixed_message(host_key):
    with loopback_server(host_key, password_ok=False) as (port, _, _):
        with pytest.raises(DomainError) as caught:
            with SSHSession(access(port, fingerprint(host_key))):
                pytest.fail("invalid password must fail")
        assert caught.value.code == "ssh_auth_failed"
        assert str(caught.value) == "SSH authentication failed"


def test_exec_acknowledgement_has_deadline(host_key):
    with loopback_server(host_key, hold_exec=True) as (port, _, exec_seen):
        with SSHSession(access(port, fingerprint(host_key))) as session:
            started = time.monotonic()
            with pytest.raises(DomainError) as caught:
                session.run(["echo", "fixed-command"], timeout=0.15)
            assert exec_seen.is_set()
            assert time.monotonic() - started < 1.5
            assert caught.value.code == "remote_outcome_unknown"
