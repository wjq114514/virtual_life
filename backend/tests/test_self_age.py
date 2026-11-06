"""Self age and user birthday countdown are plain prompt facts.

The companion knows how long it has existed (days since companions.created_at)
and, when a protected birthday-slot fact exists, how many days remain until
the user's next birthday. Facts only — no scripted lines.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop, companion_source
from test_inner_life import Provider, send
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.settings import Settings


@pytest.fixture
def runtime(tmp_path):
    clock, provider = Clock(), Provider()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "self_age.sqlite"),
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
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        yield client, app, clock, provider, state


def _sync_state(client, state):
    fresh = client.get("/v1/companions/sui").json()
    return {**state, "revision": fresh["revision"]}


def _write_birthday_fact(app, client, tag, content):
    return app.state.memory.write(
        "sui",
        {
            "command_id": "birthday-" + tag,
            "kind": "fact",
            "content": content,
            "semantic_key": "birthday",
            "source_event_id": companion_source(client),
        },
    )


def test_self_age_injected_into_prompt(runtime):
    client, app, clock, provider, state = runtime
    send(client, state, "one", "我们认识多久了")
    context = provider.requests[-1].context_text
    assert "时间感" in context
    assert "你诞生于 2025-01-01" in context
    assert "相识的第 1 天" in context
    # no birthday fact: no birthday line at all
    assert "生日" not in context


def test_birthday_countdown_within_window(runtime):
    client, app, clock, provider, state = runtime
    _write_birthday_fact(app, client, "near", "用户生日是 1月10日")
    state = _sync_state(client, state)
    send(client, state, "two", "最近有什么日子吗")
    context = provider.requests[-1].context_text
    assert "用户的生日是 1月10日" in context
    assert "还有 9 天" in context


def test_far_birthday_is_omitted_while_self_age_counts(runtime):
    client, app, clock, provider, state = runtime
    _write_birthday_fact(app, client, "far", "7月20日")
    state = _sync_state(client, state)
    clock.value += timedelta(days=10)
    send(client, state, "three", "还在吗")
    context = provider.requests[-1].context_text
    # far-away birthday is omitted; self age keeps counting
    assert "还有" not in context
    assert "相识的第 11 天" in context


def test_birthday_today(runtime):
    client, app, clock, provider, state = runtime
    _write_birthday_fact(app, client, "today", "1月11日")
    state = _sync_state(client, state)
    clock.value += timedelta(days=10)
    send(client, state, "four", "今天怎么样")
    context = provider.requests[-1].context_text
    assert "今天是用户的生日" in context
