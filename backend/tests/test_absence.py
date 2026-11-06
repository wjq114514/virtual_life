"""Absence reunion: long gaps between dialogues produce one reunion signal.

A gap of days is not "same as yesterday": the companion records a single
companion.absence event, applies a bounded one-shot emotion adjustment, and
injects a reunion note into the next prompt. Short gaps stay silent.
"""

import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_inner_life import Provider, send
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.inner_life import InnerLifePolicy
from virtual_life.settings import Settings


def build_app(tmp_path):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "absence.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        model_provider=provider,
        inner_life_policy=InnerLifePolicy(
            min_activity_gap_seconds=30,
            activity_ttl_seconds=600,
            min_evidence_items=1,
            proactive_urge_threshold=0.2,
            proactive_min_idle_seconds=60,
            idle_saturation_seconds=120,
            max_history=10,
        ),
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.dialogue,
        app.state.memory,
    ):
        service.clock = clock
    return app, clock, provider


def rows(db_path, query, params=()):
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, params)]


def _sync_state(client, state):
    fresh = client.get("/v1/companions/sui").json()
    return {**state, "revision": fresh["revision"]}


@pytest.fixture
def runtime(tmp_path):
    app, clock, provider = build_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        yield client, app, clock, provider, state


def test_short_gap_does_not_trigger_absence(tmp_path, runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "在吗")
    state = _sync_state(client, state)
    clock.value += timedelta(hours=1)
    send(client, state, "two", "还在吗")
    events = rows(
        app.state.settings.database_path,
        "SELECT * FROM events WHERE type='companion.absence'",
    )
    assert events == []
    reasons = rows(
        app.state.settings.database_path,
        "SELECT appraisal_json FROM emotion_experiences",
    )
    assert all(json.loads(row["appraisal_json"])["reason"] != "absence_reunion" for row in reasons)


def test_seven_day_gap_reunion_event_and_emotion(tmp_path, runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "我先去忙了")
    state = _sync_state(client, state)
    clock.value += timedelta(days=7)
    send(client, state, "two", "我回来了")
    events = rows(
        app.state.settings.database_path,
        "SELECT * FROM events WHERE type='companion.absence'",
    )
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert abs(payload["days"] - 7.0) < 0.01
    reasons = rows(
        app.state.settings.database_path,
        "SELECT appraisal_json FROM emotion_experiences",
    )
    reunion = [row for row in reasons if json.loads(row["appraisal_json"])["reason"] == "absence_reunion"]
    assert len(reunion) == 1
    appraisal = json.loads(reunion[0]["appraisal_json"])
    delta = appraisal["delta"]
    # valence dips, arousal rises, both bounded by the caps
    assert -0.15 < delta["valence"] <= -0.10
    assert 0.05 < delta["arousal"] <= 0.11


def test_reunion_note_reaches_the_prompt_once(tmp_path, runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "我先去忙了")
    state = _sync_state(client, state)
    clock.value += timedelta(days=5)
    send(client, state, "two", "好久不见")
    assert "重逢" in provider.requests[-1].context_text
    state = _sync_state(client, state)
    # a follow-up message within the note window still carries it...
    send(client, state, "three", "嗯")
    # ...and the absence event itself is emitted exactly once for the gap
    events = rows(
        app.state.settings.database_path,
        "SELECT * FROM events WHERE type='companion.absence'",
    )
    assert len(events) == 1


def test_second_long_gap_after_reunion_triggers_again(tmp_path, runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "你好")
    state = _sync_state(client, state)
    clock.value += timedelta(days=4)
    send(client, state, "two", "回来了")
    state = _sync_state(client, state)
    clock.value += timedelta(days=4)
    send(client, state, "three", "又回来了")
    events = rows(
        app.state.settings.database_path,
        "SELECT * FROM events WHERE type='companion.absence'",
    )
    assert len(events) == 2
