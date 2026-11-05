"""Inner-action (experimental adb look/do) gates, safety and event emission.

No real emulator is touched: the adb subprocess helpers are monkeypatched.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life import inner_life as inner_life_module
from virtual_life.api import create_app
from virtual_life.inner_life import InnerLifePolicy
from virtual_life.settings import Settings

PNG = b"\x89PNG" + b"\x00" * 32


class FakeResult:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class AdbFake:
    def __init__(self, online=True, screencap=PNG, monkey_rc=0, dumpsys=b""):
        self.online = online
        self.screencap = screencap
        self.monkey_rc = monkey_rc
        self.dumpsys = dumpsys
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if "devices" in args:
            return FakeResult(
                0,
                b"List of devices attached\nemulator-5554\tdevice\n"
                if self.online
                else b"List of devices attached\n",
            )
        if "screencap" in args:
            return FakeResult(0, self.screencap)
        if "monkey" in args:
            return FakeResult(self.monkey_rc, b"", b"monkey failed" if self.monkey_rc else b"")
        if "dumpsys" in args:
            return FakeResult(0, self.dumpsys)
        return FakeResult(0, b"")


def build_app(tmp_path, policy):
    clock = Clock()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "action.sqlite"),
            api_token="test",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        inner_life_policy=policy,
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.dialogue,
        app.state.memory,
    ):
        service.clock = clock
    return app, clock


def seed_activity(app, kind, salience, activity_id="ina_test0001"):
    """Insert an active inner activity directly, deterministically."""
    from virtual_life.coordinator import _timestamp

    now = app.state.personality._now()
    with app.state.inner_life.store.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO inner_activities
               (activity_id, companion_id, kind, trigger, content, salience, urge,
                attention_session_id, attention_boundary_id, context_version,
                source_event_id, generator, model_json, thinking, status,
                created_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                activity_id,
                "sui",
                kind,
                "social_need",
                "想看看屏幕那边的动静。",
                salience,
                0.5,
                None,
                None,
                "ctx_1",
                None,
                "rules_v1",
                None,
                None,
                "active",
                _timestamp(now),
                _timestamp(now + timedelta(seconds=600)),
            ),
        )


def action_policy(tmp_path, **overrides):
    values = dict(
        min_activity_gap_seconds=30,
        activity_ttl_seconds=600,
        min_evidence_items=1,
        proactive_urge_threshold=0.2,
        proactive_min_idle_seconds=60,
        idle_saturation_seconds=120,
        max_history=10,
        action_enabled=True,
        action_adb_serial="emulator-5554",
        action_open_app_whitelist=("com.android.chrome",),
        action_screenshot_dir=str(tmp_path / "shots"),
        action_min_interval_seconds=60,
        action_daily_limit=2,
        action_curious_min_salience=0.45,
    )
    values.update(overrides)
    return InnerLifePolicy(**values)


def events(client):
    rows = client.get("/v1/events?limit=500").json()["events"]
    return [row for row in rows if row["type"] == "companion.inner_action"]


@pytest.fixture
def runtime(tmp_path):
    policy = action_policy(tmp_path)
    app, clock = build_app(tmp_path, policy)
    client = TestClient(app, headers={"Authorization": "Bearer test"})
    attach_desktop(client)
    app.state.coordinator.register_node(
        {
            "node_id": "desk",
            "label": "desk",
            "kind": "desktop",
            "capabilities": ["text.display", "attention.observe"],
        }
    )
    yield client, app, clock
    client.close()


def test_look_action_saves_screenshot_and_emits_event(runtime, tmp_path, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    fake = AdbFake()
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload is not None
    assert payload["action"] == "look"
    assert payload["outcome"] == "ok"
    assert payload["screenshot_path"].endswith(".png")
    assert Path(payload["screenshot_path"]).read_bytes() == PNG
    assert payload["screenshot_bytes"] == len(PNG)
    assert any("screencap" in call for call in fake.calls)
    emitted = events(client)
    assert len(emitted) == 1
    assert emitted[0]["payload"] == payload


def test_open_app_action_uses_whitelist(monkeypatch, runtime):
    client, app, clock = runtime
    seed_activity(app, "restless", 0.8)
    fake = AdbFake()
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload["action"] == "open_app"
    assert payload["target"] == "com.android.chrome"
    assert payload["outcome"] == "ok"
    monkey_call = next(call for call in fake.calls if "monkey" in call)
    assert monkey_call == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "monkey",
        "-p",
        "com.android.chrome",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    ]
    assert len(events(client)) == 1


def test_non_whitelisted_package_refused(runtime):
    client, app, clock = runtime
    seed_activity(app, "restless", 0.8)
    activity = {"activity_id": "ina_test0001", "kind": "restless", "companion_id": "sui"}
    payload = app.state.inner_life._execute_action("open_app", "com.evil.package", activity)
    assert payload["outcome"] == "refused"
    assert payload["reason"] == "package_not_in_whitelist"
    assert events(client) == []


def test_disabled_policy_never_runs_adb(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.9)
    fake = AdbFake()
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)
    app.state.inner_life.policy.action_enabled = False

    assert app.state.inner_life._maybe_action("sui", clock()) is None
    assert fake.calls == []


def test_low_salience_and_wrong_kind_skip(runtime, monkeypatch):
    client, app, clock = runtime
    fake = AdbFake()
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    seed_activity(app, "curious", 0.2, activity_id="ina_low")
    assert app.state.inner_life._maybe_action("sui", clock()) is None

    seed_activity(app, "contentment", 0.9, activity_id="ina_calm")
    assert app.state.inner_life._maybe_action("sui", clock()) is None
    assert fake.calls == []
    assert events(client) == []


def test_interval_gate_blocks_second_action(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake())
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    assert app.state.inner_life._maybe_action("sui", clock())["outcome"] == "ok"
    # Not enough time has passed for the second action.
    assert app.state.inner_life._maybe_action("sui", clock()) is None
    clock.value += timedelta(seconds=61)
    assert app.state.inner_life._maybe_action("sui", clock())["outcome"] == "ok"
    assert len(events(client)) == 2


def test_daily_limit_gate(runtime, monkeypatch):
    client, app, clock = runtime
    app.state.inner_life.policy.action_daily_limit = 1
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake())
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    assert app.state.inner_life._maybe_action("sui", clock())["outcome"] == "ok"
    clock.value += timedelta(seconds=61)
    assert app.state.inner_life._maybe_action("sui", clock()) is None
    assert len(events(client)) == 1


def test_offline_emulator_skips_silently(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    fake = AdbFake()
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: False)

    assert app.state.inner_life._maybe_action("sui", clock()) is None
    assert fake.calls == []
    assert events(client) == []


def test_adb_failure_emits_failed_event_without_raising(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake(screencap=b"busybox"))
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())
    assert payload["outcome"] == "failed"
    assert len(events(client)) == 1


def test_adb_exception_is_contained(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)

    def explode(args, timeout=None):
        raise inner_life_module.subprocess.TimeoutExpired("adb", 30)

    monkeypatch.setattr(inner_life_module, "_adb_run", explode)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())
    assert payload["outcome"] == "failed"
    assert "timed out" in payload["detail"]
    assert len(events(client)) == 1


def test_tick_runs_action_for_active_companions(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "restless", 0.8)
    fake = AdbFake()
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    app.state.inner_life.tick()

    assert any("monkey" in call for call in fake.calls)
    assert len(events(client)) == 1


def test_settings_four_way_linkage(tmp_path):
    config = tmp_path / "virtual-life.toml"
    config.write_text(
        "[inner_life]\n"
        'action_adb_serial = "emulator-5554"\n'
        'action_open_app_whitelist = ["com.android.chrome", "com.android.settings"]\n'
        "action_daily_limit = 5\n"
    )
    settings = Settings.load(config)
    policy = settings.inner_life_policy
    assert policy.action_adb_serial == "emulator-5554"
    assert policy.action_open_app_whitelist == (
        "com.android.chrome",
        "com.android.settings",
    )
    assert policy.action_daily_limit == 5
    assert policy.action_enabled is True
    assert policy.action_screenshot_dir == ".data/inner_actions"
    assert policy.action_min_interval_seconds == 1800
    assert policy.action_curious_min_salience == 0.35

    import os

    os.environ["VIRTUAL_LIFE_INNER_LIFE_ACTION_DAILY_LIMIT"] = "3"
    os.environ["VIRTUAL_LIFE_INNER_LIFE_ACTION_OPEN_APP_WHITELIST"] = '["com.android.camera2"]'
    try:
        settings = Settings.load(config)
    finally:
        os.environ.pop("VIRTUAL_LIFE_INNER_LIFE_ACTION_DAILY_LIMIT")
        os.environ.pop("VIRTUAL_LIFE_INNER_LIFE_ACTION_OPEN_APP_WHITELIST")
    policy = settings.inner_life_policy
    assert policy.action_daily_limit == 3
    assert policy.action_open_app_whitelist == ("com.android.camera2",)


def memories(client, companion="sui"):
    rows = client.get(f"/v1/companions/{companion}/memories?limit=50").json()
    return [row for row in rows["items"] if "inner_action" in row.get("tags", [])]


def test_ok_action_writes_episodic_memory(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake())
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload["outcome"] == "ok"
    written = memories(client)
    assert len(written) == 1
    record = written[0]
    assert record["kind"] == "episode"
    assert record["source_event_ids"] == [events(client)[0]["event_id"]]
    assert "手机屏幕" in record["content"]


def test_failed_action_writes_memory_and_refused_does_not(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "restless", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake(monkey_rc=1))
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload["outcome"] == "failed"
    written = memories(client)
    assert len(written) == 1
    assert "浏览器" in written[0]["content"]
    assert written[0]["source_event_ids"] == [events(client)[0]["event_id"]]

    # A refused (non-whitelisted) action is an administrative skip, not an
    # experience the companion would remember as its own doing.
    clock.value += timedelta(seconds=61)
    seed_activity(app, "restless", 0.8, activity_id="ina_test0002")
    app.state.inner_life._choose_action = lambda activity: (
        "open_app",
        "com.evil.package",
    )
    payload = app.state.inner_life._maybe_action("sui", clock())
    assert payload["outcome"] == "refused"
    assert len(memories(client)) == 1


def test_action_memory_failure_is_contained(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake())
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    def boom(companion_id, payload):
        raise RuntimeError("memory down")

    app.state.memory.write = boom
    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload is not None
    assert payload["outcome"] == "ok"
    assert len(events(client)) == 1


def test_action_memory_uses_model_line_when_attached(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake())
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    from virtual_life.ports import ModelResult, ModelUsage

    calls = []

    class LineProvider:
        def complete(self, request):
            calls.append(request)
            return ModelResult(
                "顺手把时钟点开又关上了，像替他顺手拨了一下发条。",
                "fake",
                ModelUsage(90, 14),
            )

    app.state.inner_life.attach_model(LineProvider(), "fake")
    app.state.inner_life.policy.model_assisted = True

    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload["outcome"] == "ok"
    assert [r.purpose for r in calls] == ["inner_action_memory"]
    written = memories(client)
    assert len(written) == 1
    assert written[0]["content"] == "顺手把时钟点开又关上了，像替他顺手拨了一下发条。"
    assert "model_v1" in written[0]["tags"]

    # Model refusal or garbage (empty line) falls back to the template pool.
    clock.value += timedelta(seconds=61)
    seed_activity(app, "curious", 0.8, activity_id="ina_test0003")

    class EmptyProvider:
        def complete(self, request):
            return ModelResult("", "fake", ModelUsage(40, 4))

    app.state.inner_life.attach_model(EmptyProvider(), "fake")
    payload = app.state.inner_life._maybe_action("sui", clock())
    assert payload["outcome"] == "ok"
    written = memories(client)
    assert len(written) == 2
    # Newest first: the fallback write lands on top.
    assert written[0]["tags"][-1] == "rules_v1"
    assert "手机屏幕" in written[0]["content"]


def _context_of(app, companion_id):
    service = app.state.inner_life
    with service.store.transaction() as connection:
        return service._context(connection, companion_id, service.personality._now())


def test_recent_action_colors_context_after_action(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake())
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    assert _context_of(app, "sui")["recent_action"] is None
    app.state.inner_life._maybe_action("sui", clock())

    recent = _context_of(app, "sui")["recent_action"]
    assert recent is not None
    assert recent["action"] == "look"
    assert recent["outcome"] == "ok"
    assert recent["age_seconds"] < 3 * 3600


def test_recent_action_expires_outside_window(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    monkeypatch.setattr(inner_life_module, "_adb_run", AdbFake())
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)
    app.state.inner_life._maybe_action("sui", clock())

    clock.value += timedelta(seconds=3 * 3600 + 60)
    assert _context_of(app, "sui")["recent_action"] is None


def test_look_action_records_foreground_app(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    fake = AdbFake(
        dumpsys=(
            b"mLastPausedActivity: null\n"
            b"  topResumedActivity=ComponentInfo{com.android.settings/.Settings} "
            b"userId=0\n"
        )
    )
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload["action"] == "look"
    assert payload["outcome"] == "ok"
    assert payload["foreground_app"] == "com.android.settings"
    assert any("dumpsys" in call for call in fake.calls)
    recent = _context_of(app, "sui")["recent_action"]
    assert recent["foreground_app"] == "com.android.settings"
    # The follow-up channel sees the friendly label too, via the memory line.
    written = memories(client)
    assert written, "look action should write an episodic memory"
    assert all("{app}" not in m["content"] for m in written)
    assert any(("设置" in m["content"]) or ("屏幕" in m["content"]) for m in written)


def test_look_action_without_dumpsys_match_still_succeeds(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "curious", 0.8)
    fake = AdbFake(dumpsys=b"no resumed activity here")
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)

    payload = app.state.inner_life._maybe_action("sui", clock())

    assert payload["outcome"] == "ok"
    assert "foreground_app" not in payload
    recent = _context_of(app, "sui")["recent_action"]
    assert "foreground_app" not in recent


def test_press_home_and_back_actions_run_keyevent(runtime, monkeypatch):
    client, app, clock = runtime
    seed_activity(app, "restless", 0.8)
    fake = AdbFake()
    monkeypatch.setattr(inner_life_module, "_adb_run", fake)
    monkeypatch.setattr(inner_life_module, "_adb_device_online", lambda serial: True)
    service = app.state.inner_life
    monkeypatch.setattr(service, "_choose_action", lambda activity: ("press_home", None))

    payload = service._maybe_action("sui", clock())

    assert payload["action"] == "press_home"
    assert payload["outcome"] == "ok"
    assert ["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "3"] in fake.calls

    clock.value += timedelta(seconds=61)
    seed_activity(app, "restless", 0.8, activity_id="ina_test0002")
    monkeypatch.setattr(service, "_choose_action", lambda activity: ("press_back", None))
    payload = service._maybe_action("sui", clock())

    assert payload["action"] == "press_back"
    assert payload["outcome"] == "ok"
    assert ["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "4"] in fake.calls


def test_choose_action_restless_variety(tmp_path):
    app, _ = build_app(tmp_path, action_policy(tmp_path))
    service = app.state.inner_life
    counts: dict[str, int] = {}
    for i in range(200):
        action, _ = service._choose_action({"kind": "restless", "activity_id": f"ina_var{i:04d}"})
        counts[action] = counts.get(action, 0) + 1
    assert counts["open_app"] > 100
    assert counts.get("press_home", 0) + counts.get("press_back", 0) >= 10

    # Without a whitelist, restless can only press home/back — never a
    # non-whitelisted app.
    service.policy.action_open_app_whitelist = ()
    for i in range(20):
        action, target = service._choose_action(
            {"kind": "restless", "activity_id": f"ina_nv{i:04d}"}
        )
        assert action in ("press_home", "press_back")
        assert target is None
