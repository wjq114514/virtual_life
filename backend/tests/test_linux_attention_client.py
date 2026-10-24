"""Local attention capture stays conservative; fixtures never inspect the host desktop."""

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "clients/system-node/system_node"


@pytest.fixture(scope="module")
def client():
    name = "attention_test_system_node"
    spec = importlib.util.spec_from_file_location(
        name, SOURCE / "__init__.py", submodule_search_locations=[str(SOURCE)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return SimpleNamespace(
        **{
            part: importlib.import_module(f"{name}.{part}")
            for part in ("attention", "config", "runtime", "transport")
        }
    )


def properties(**changes):
    return {
        "Type": "wayland",
        "Class": "user",
        "Remote": "no",
        "Active": "yes",
        "State": "active",
        "LockedHint": "no",
        "IdleHint": "no",
        "User": str(os.getuid()),
        "Display": "",
        **changes,
    }


class FixtureCommands:
    def __init__(self, sessions=None):
        self.sessions = {"c1": properties()} if sessions is None else sessions
        self.commands = []
        self.active = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x123\n"
        self.wm_class = 'WM_CLASS(STRING) = "code", "Code"\n'

    def __call__(self, argv, timeout):
        assert 0 < timeout <= 2
        self.commands.append(argv)
        if argv[:2] == ["loginctl", "list-sessions"]:
            return "".join(
                f"{identity} 1000 PRIVATE-USER seat0 tty2\n" for identity in self.sessions
            )
        if argv[:2] == ["loginctl", "show-session"]:
            return "\n".join(f"{key}={value}" for key, value in self.sessions[argv[2]].items())
        if argv == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
            return self.active
        assert argv == ["xprop", "-id", "0x123", "WM_CLASS"]
        return self.wm_class


def collect(client, sessions, *, selected=None):
    commands = FixtureCommands(sessions)
    return client.attention.AttentionCollector(selected, runner=commands).collect(), commands


def test_unique_desktop_no_inferred_focus_or_availability(client):
    reading, commands = collect(client, {"desktop-private-session": properties()})
    assert reading.sample["user_present"] is True and reading.sample["locked"] is False
    assert reading.sample["focus"] == reading.sample["availability"] == "unknown"
    assert reading.sample["meeting"] is None and reading.sample["screen_sharing"] is None
    assert reading.sample["boundary"] == "none"
    assert reading.diagnostics["status"] == "ok"
    assert not any(
        value in json.dumps(reading.sample) + json.dumps(reading.diagnostics)
        for value in ("PRIVATE-USER", "seat0", "desktop-private-session")
    )
    assert commands.commands[0] == ["loginctl", "list-sessions", "--no-legend", "--no-pager"]


@pytest.mark.parametrize(
    "sessions",
    [
        {},
        {"a": properties(), "b": properties()},
        {"a": properties(Type="tty")},
        {"a": properties(Class="greeter")},
        {"a": properties(Remote="yes")},
        {"a": properties(Active="no", State="online")},
        {"a": {key: value for key, value in properties().items() if key != "IdleHint"}},
        {"a": properties(LockedHint="unknown")},
        {"a": properties(State="closing")},
        {"-option": properties()},
    ],
)
def test_absent_ambiguous_remote_or_incomplete_is_unknown(client, sessions):
    reading, _ = collect(client, sessions)
    assert reading.diagnostics["status"] == "unknown"
    assert reading.sample["user_present"] is None and reading.sample["locked"] is None
    assert reading.sample["boundary"] == "none"


@pytest.mark.parametrize("changes", [{"LockedHint": "yes"}, {"Active": "no", "State": "online"}])
def test_explicit_away_never_defaults_present(client, changes):
    reading, commands = collect(client, {"a": properties(**changes)}, selected="a")
    assert reading.sample["user_present"] is False
    assert commands.commands[0][1] == "show-session"


def test_other_sessions_do_not_override_unique_local_desktop(client):
    reading, _ = collect(
        client,
        {
            "ssh": properties(Type="tty", Remote="yes"),
            "greeter": properties(Class="greeter"),
            "desktop": properties(),
        },
    )
    assert reading.sample["user_present"] is True


def test_only_known_away_to_present_in_same_session_emits_boundary(client):
    commands = FixtureCommands()
    collector = client.attention.AttentionCollector(runner=commands)
    first = collector.collect()
    assert first.sample["boundary"] == "none"
    commands.sessions["c1"]["LockedHint"] = "yes"
    assert collector.collect().sample["user_present"] is False
    commands.sessions["c1"]["LockedHint"] = "no"
    returned = collector.collect()
    assert returned.sample["boundary"] == "user_returned" and returned.sample["boundary_id"]
    assert returned.session_id == first.session_id
    assert collector.collect().sample["boundary"] == "none"
    commands.sessions["c1"]["LockedHint"] = "yes"
    collector.collect()
    commands.sessions = {"c2": properties()}
    switched = collector.collect()
    assert switched.sample["boundary"] == "none" and switched.session_id != first.session_id
    commands.sessions["c2"]["LockedHint"] = "yes"
    collector.collect()
    commands.sessions = {}
    assert collector.collect().sample["user_present"] is None
    commands.sessions = {"c2": properties()}
    assert collector.collect().sample["boundary"] == "none"


@pytest.mark.parametrize(
    "failure", [OSError("private"), subprocess.TimeoutExpired("private", 2), ValueError("private")]
)
def test_command_failure_is_fixed_unknown(client, failure):
    def runner(*args):
        raise failure

    reading = client.attention.AttentionCollector(runner=runner).collect()
    assert reading.diagnostics["reason"] == "probe_failed"
    assert "private" not in json.dumps(reading.diagnostics)
    assert reading.sample["user_present"] is None


def test_total_budget_and_session_bound(client):
    clock = iter([0, 0, 3])
    commands = FixtureCommands()
    reading = client.attention.AttentionCollector(
        runner=commands, monotonic=lambda: next(clock)
    ).collect()
    assert reading.diagnostics["reason"] == "probe_failed"
    assert len(commands.commands) == 1
    reading, commands = collect(client, {str(index): properties() for index in range(17)})
    assert reading.diagnostics["status"] == "unknown" and len(commands.commands) == 1


def test_real_command_runner_timeout_and_output_bound(client):
    with pytest.raises((client.attention.ProbeError, subprocess.TimeoutExpired)):
        client.attention.run_command([sys.executable, "-c", "import time; time.sleep(30)"], 0.05)
    with pytest.raises(client.attention.ProbeError):
        client.attention.run_command(
            [sys.executable, "-c", 'import sys; sys.stdout.write("x"*70000)'], 2
        )
    assert (
        client.attention.run_command([sys.executable, "-c", 'print("fixture")'], 2) == "fixture\n"
    )


def test_x11_only_reads_whitelisted_properties_and_never_uploads_class(client, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":77")
    commands = FixtureCommands({"c1": properties(Type="x11", Display=":77")})
    reading = client.attention.AttentionCollector(runner=commands).collect()
    assert reading.sample["activity"] == "code"
    assert reading.diagnostics["source"] == "logind+x11"
    assert "Code" not in json.dumps(reading.sample) + json.dumps(reading.diagnostics)
    assert [argv[-1] for argv in commands.commands if argv[0] == "xprop"] == [
        "_NET_ACTIVE_WINDOW",
        "WM_CLASS",
    ]


@pytest.mark.parametrize(
    "changes,display",
    [
        ({"User": str(os.getuid() + 1)}, ":77"),
        ({"Display": ":78"}, ":77"),
        ({}, ""),
        ({}, "localhost:10.0"),
    ],
)
def test_x11_never_crosses_user_or_display(client, monkeypatch, changes, display):
    monkeypatch.setenv("DISPLAY", display)
    commands = (
        FixtureCommands({"c1": properties(Type="x11", Display=":77", **changes)})
        if "Display" not in changes
        else FixtureCommands({"c1": properties(Type="x11", **changes)})
    )
    reading = client.attention.AttentionCollector(runner=commands).collect()
    assert reading.sample["activity"] == "unknown"
    assert not any(argv[0] == "xprop" for argv in commands.commands)


def config(client, tmp_path, **changes):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"server_url": "http://127.0.0.1:1", "node_id": "self", "label": "Self", **changes}
        )
    )
    return client.config.load_config(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"attention": True},
        {"attention": {"enabled": "true"}},
        {"attention": {"session_id": "-a"}},
        {"attention": {"enabled": True, "extra": 1}},
        {"attention": {"enabled": True}, "interval_seconds": 31},
        {"attention": {"enabled": True}, "ttl_seconds": 31},
        {"attention": {"enabled": True}, "request_timeout_seconds": 30, "ttl_seconds": 300},
    ],
)
def test_attention_config_rejects_invalid_or_unbounded(client, tmp_path, changes):
    with pytest.raises(client.config.ConfigError):
        config(client, tmp_path, **changes)


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail_attention = False
        self.fail_heartbeat = False

    def post(self, path, body):
        self.calls.append((path, body))
        if (self.fail_attention and path.endswith("/attention")) or (
            self.fail_heartbeat and path.endswith("/heartbeat")
        ):
            raise self.error("unavailable", retryable=True)
        return {}

    def post_encoded(self, path, body):
        return {}


def runtime(client, tmp_path, monkeypatch, *, enabled=True, gateway=False):
    settings = {"attention": {"enabled": enabled}}
    if gateway:
        settings.update(
            deployment_mode="gateway",
            targets=[
                {
                    "node_id": "target",
                    "label": "Target",
                    "adapter": "tcp",
                    "host": "127.0.0.1",
                    "port": 1,
                }
            ],
        )
    parsed = config(client, tmp_path, **settings)
    api = FakeAPI()
    api.error = client.transport.TransportError
    local = SimpleNamespace(collect=lambda: ({"quality": "fresh"}, {"status": "reachable"}, {}))
    monkeypatch.setattr(client.runtime, "LocalCollector", lambda _: local)
    monkeypatch.setattr(client.runtime, "collect_target", lambda _: local.collect())
    instance = client.runtime.Runtime(parsed, api, threading.Event())
    return instance, api


def test_runtime_disabled_no_probe_no_attention_network(client, tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("Disabled must not construct or probe attention collector")

    monkeypatch.setattr(client.runtime, "AttentionCollector", forbidden)
    instance, api = runtime(client, tmp_path, monkeypatch, enabled=False)
    instance._cycle()
    assert [path for path, _ in api.calls] == ["/v1/nodes", "/v1/nodes/self/heartbeat"]
    assert api.calls[0][1]["capabilities"] == ["device.state.report"]
    assert set(api.calls[1][1]["context"]) == {"device_state"}


def test_runtime_fresh_sequence_gateway_scope_and_heartbeat_context(client, tmp_path, monkeypatch):
    instance, api = runtime(client, tmp_path, monkeypatch, gateway=True)
    commands = FixtureCommands()
    instance._attention = client.attention.AttentionCollector(runner=commands)
    instance._cycle()
    instance._cycle()
    registrations = [body for path, body in api.calls if path == "/v1/nodes"]
    assert registrations[0]["capabilities"] == [
        "device.state.report",
        "device.state.observe",
        "attention.observe",
    ]
    assert registrations[1]["capabilities"] == ["device.state.report"]
    samples = [body for path, body in api.calls if path.endswith("/attention")]
    assert [body["sample"]["sequence"] for body in samples] == [1, 2]
    assert samples[0]["command_id"] != samples[1]["command_id"]
    assert samples[0]["sample"]["session_id"] == samples[1]["sample"]["session_id"]
    assert samples[0]["sample"]["sampled_at"] <= samples[1]["sample"]["sampled_at"]
    assert all(body["sample"]["ttl_seconds"] == 90 for body in samples)
    assert not any(path == "/v1/nodes/target/attention" for path, _ in api.calls)
    heartbeats = [body["context"] for path, body in api.calls if path == "/v1/nodes/self/heartbeat"]
    assert set(heartbeats[0]) == {"device_state"}
    assert "attention_collection" in heartbeats[1]
    assert "device_state" in heartbeats[1]


def test_runtime_failure_never_replays_old_sample_or_claims_success(client, tmp_path, monkeypatch):
    instance, api = runtime(client, tmp_path, monkeypatch)
    instance._attention = client.attention.AttentionCollector(runner=FixtureCommands())
    api.fail_attention = True
    with pytest.raises(client.transport.TransportError):
        instance._cycle()
    first = api.calls[-1][1]
    assert len(instance._pending) == 0
    api.fail_attention = False
    instance._cycle()
    second = [body for path, body in api.calls if path.endswith("/attention")][-1]
    assert second["sample"]["sequence"] == first["sample"]["sequence"] + 1
    assert second["command_id"] != first["command_id"]


def test_failed_heartbeat_does_not_collect_or_submit_attention(client, tmp_path, monkeypatch):
    instance, api = runtime(client, tmp_path, monkeypatch)
    instance._attention = SimpleNamespace(
        collect=lambda: pytest.fail("Heartbeat failed: no fresh collection")
    )
    api.fail_heartbeat = True
    with pytest.raises(client.transport.TransportError):
        instance._cycle()
    assert not any(path.endswith("/attention") for path, _ in api.calls)


def test_idle_reading_is_not_absence_or_return_boundary(client):
    commands = FixtureCommands()
    collector = client.attention.AttentionCollector(runner=commands)
    assert collector.collect().sample["user_present"] is True
    commands.sessions["c1"]["IdleHint"] = "yes"
    idle = collector.collect()
    assert idle.sample["user_present"] is None
    assert idle.sample["locked"] is False
    assert idle.diagnostics["idle_hint"] is True
    assert idle.diagnostics["active"] is True
    assert idle.diagnostics["locked_hint"] is False
    commands.sessions["c1"]["IdleHint"] = "no"
    active = collector.collect()
    assert active.sample["user_present"] is True
    assert active.sample["boundary"] == "none"


def test_explicit_inactive_selection_retains_identity_and_can_report_return(client):
    commands = FixtureCommands({"c1": properties(Active="no", State="online")})
    collector = client.attention.AttentionCollector("c1", runner=commands)
    away = collector.collect()
    assert away.sample["user_present"] is False
    assert away.diagnostics["active"] is False
    commands.sessions["c1"].update(Active="yes", State="active")
    returned = collector.collect()
    assert returned.session_id == away.session_id
    assert returned.sample["user_present"] is True
    assert returned.sample["boundary"] == "user_returned"


def test_sample_clock_cannot_retreat_or_collide_across_source_sessions(client):
    from datetime import UTC, datetime, timedelta

    initial = datetime(2026, 10, 15, tzinfo=UTC)
    clock = iter([initial, initial, initial - timedelta(seconds=1)])
    commands = FixtureCommands()
    collector = client.attention.AttentionCollector(runner=commands, now=lambda: next(clock))
    first = collector.collect()
    second = collector.collect()
    commands.sessions = {"different": properties()}
    third = collector.collect()
    assert (
        first.diagnostics["sampled_at"]
        < second.diagnostics["sampled_at"]
        < third.diagnostics["sampled_at"]
    )
    assert third.session_id != first.session_id


def test_runtime_rotates_source_session_without_resuming_retired_sequence(
    client, tmp_path, monkeypatch
):
    instance, api = runtime(client, tmp_path, monkeypatch)
    commands = FixtureCommands()
    instance._attention = client.attention.AttentionCollector(runner=commands)
    instance._cycle()
    commands.sessions = {"other": properties()}
    instance._cycle()
    commands.sessions = {"c1": properties()}
    instance._cycle()
    samples = [body["sample"] for path, body in api.calls if path.endswith("/attention")]
    assert len({sample["session_id"] for sample in samples}) == 3
    assert [sample["sequence"] for sample in samples] == [1, 1, 1]
    assert all(sample["boundary"] == "none" for sample in samples)
    assert samples[0]["sampled_at"] < samples[1]["sampled_at"] < samples[2]["sampled_at"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('WM_CLASS(STRING) = "firefox", "Firefox"\n', "browser"),
        ('WM_CLASS(STRING) = "unknown-private-application", "SecretRawClass"\n', "unknown"),
        ("WM_CLASS: no such atom on any window.\n", "unknown"),
        ('WM_NAME(STRING) = "PRIVATE-TITLE"\n', "unknown"),
    ],
)
def test_x11_unknown_class_is_not_a_title_or_permission_signal(client, monkeypatch, raw, expected):
    monkeypatch.setenv("DISPLAY", ":77")
    commands = FixtureCommands({"c1": properties(Type="x11", Display=":77")})
    commands.wm_class = raw
    reading = client.attention.AttentionCollector(runner=commands).collect()
    assert reading.sample["activity"] == expected
    assert reading.sample["focus"] == reading.sample["availability"] == "unknown"
    assert all(
        private not in json.dumps(reading.sample) + json.dumps(reading.diagnostics)
        for private in ("SecretRawClass", "PRIVATE-TITLE", "unknown-private-application")
    )


def test_missing_session_selector_never_falls_back_to_another_user(client):
    commands = FixtureCommands({"visible": properties()})

    def runner(argv, timeout):
        if argv[:3] == ["loginctl", "show-session", "missing"]:
            raise client.attention.ProbeError()
        return commands(argv, timeout)

    reading = client.attention.AttentionCollector("missing", runner=runner).collect()
    assert reading.diagnostics["status"] == "unknown"
    assert reading.sample["user_present"] is None
    assert not commands.commands
