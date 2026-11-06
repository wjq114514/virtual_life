"""Autobiography chapters: generation, prompt injection, dirty rebuild."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_api import attach_desktop, companion_source
from test_inner_life import Provider, send
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.settings import Settings


def build_app(tmp_path: Path, clock: Clock, provider: Provider):
    database = tmp_path / "autobiography.sqlite"
    app = create_app(
        Settings(
            database_path=str(database),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            embedding_enabled=False,
            lease_ttl_seconds=1_000_000,
            node_ttl_seconds=1_000_000,
        ),
        model_provider=provider,
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.dialogue,
        app.state.memory,
    ):
        service.clock = clock
    app.state.autobiography.memory.clock = clock
    return database, app


def rows(database: Path, query: str, params=()):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def insert_episode(app, client, content: str, tag: str):
    return app.state.memory.write(
        "sui",
        {
            "command_id": "mw-" + tag,
            "kind": "episode",
            "content": content,
            "source_event_id": companion_source(client),
        },
    )


def insert_fact(app, client, content: str, tag: str):
    return app.state.memory.write(
        "sui",
        {
            "command_id": "mw-" + tag,
            "kind": "fact",
            "content": content,
            "importance": 0.8,
            "source_event_id": companion_source(client),
        },
    )


def _sync_state(client, state):
    fresh = client.get("/v1/companions/sui").json()
    return {**state, "revision": fresh["revision"]}


def test_first_chapter_generated_from_episodes(tmp_path):
    clock = Clock()
    provider = Provider()
    database, app = build_app(tmp_path, clock, provider)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        # No episodes yet: nothing to narrate.
        assert app.state.autobiography.process("sui") is None
        insert_episode(
            app, client, "User: 我这周在准备考试，压力有点大。\nAssistant: 那我们慢慢来。", "a1"
        )
        result = app.state.autobiography.process("sui")
        assert result is not None and result["chapter_index"] == 1
        chapter = rows(database, "SELECT * FROM autobiography_chapters")
        assert len(chapter) == 1 and chapter[0]["status"] == "clean"
        assert json.loads(chapter[0]["provenance_json"])
        assert any(r.purpose == "autobiography" for r in provider.requests)


def test_chapter_note_reaches_dialogue_prompt(tmp_path):
    clock = Clock()
    provider = Provider()
    database, app = build_app(tmp_path, clock, provider)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        insert_episode(app, client, "User: 我开始学吉他了。\nAssistant: 很期待听你弹。", "b1")
        app.state.autobiography.process("sui")
        state = _sync_state(client, state)
        send(client, state, "send-1", "在忙什么呢")
        assert "自我叙事" in provider.requests[-1].context_text


def test_source_correction_marks_chapter_dirty_and_rebuilds(tmp_path):
    clock = Clock()
    provider = Provider()
    database, app = build_app(tmp_path, clock, provider)
    memory = app.state.memory
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        insert_episode(app, client, "User: 我养了一只猫叫团子。\nAssistant: 替我摸摸它。", "c1")
        fact = insert_fact(app, client, "我养的猫叫团子。", "c2")
        memory_id = fact["memory_id"]
        app.state.autobiography.process("sui")
        # Correcting the source bumps its revision; the chapter is now stale.
        memory.correct(
            "sui",
            memory_id,
            {
                "command_id": "fix-1",
                "expected_revision": fact["revision"],
                "content": "我养的猫其实叫麻薯。",
                "source_event_id": companion_source(client),
            },
        )
        with memory._connection() as connection:
            assert app.state.autobiography.note(connection, "sui") is None
        assert rows(database, "SELECT status FROM autobiography_chapters")[0]["status"] == "dirty"
        app.state.autobiography.process("sui")
        chapters = rows(database, "SELECT * FROM autobiography_chapters ORDER BY chapter_index")
        assert len(chapters) == 2
        assert chapters[0]["status"] == "dirty"
        assert chapters[1]["status"] == "clean"
        with memory._connection() as connection:
            assert app.state.autobiography.note(connection, "sui") is not None


def test_due_gate_skips_recent_complete_chapter(tmp_path):
    clock = Clock()
    provider = Provider()
    database, app = build_app(tmp_path, clock, provider)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        attach_desktop(client)
        insert_episode(app, client, "User: 一次就够了。\nAssistant: 好。", "d1")
        app.state.autobiography.process("sui")
        before = len(provider.requests)
        # Fresh chapter covering all episodes: not due again within the gap window.
        assert app.state.autobiography.process("sui") is None
        assert len(provider.requests) == before
        # After the 7-day window with the same coverage there is nothing new to
        # narrate, so the service still stays quiet.
        clock.value = clock.value + timedelta(days=8)
        assert app.state.autobiography.process("sui") is None
        assert len(provider.requests) == before
