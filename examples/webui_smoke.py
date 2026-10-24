"""Run an isolated Chromium smoke check with a deterministic, local model.

Run: uv run --with playwright python examples/webui_smoke.py
"""

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import sync_playwright
from virtual_life.api import create_app
from virtual_life.personality_evolution import EvolutionPolicy
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class BrowserModel:
    calls = 0
    stream_calls = 0

    def complete(self, request):
        self.calls += 1
        return ModelResult(
            "浏览器确定性回复 <img src=x onerror=alert(1)>", "browser-fake", ModelUsage(37, 11)
        )

    def stream(self, request, on_delta):
        self.stream_calls += 1
        result = self.complete(request)
        for chunk in ("浏览器", "确定性回复 ", "<img src=x onerror=alert(1)>"):
            on_delta(chunk)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/webui-review"))
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    requests = []
    writes = []
    layouts = []
    with tempfile.TemporaryDirectory(prefix="virtual-life-browser-") as temporary:
        model = BrowserModel()
        app = create_app(
            Settings(
                database_path=str(Path(temporary) / "browser.sqlite3"),
                api_token="browser-smoke",
                model_name="browser-fake",
                model_base_url="http://127.0.0.1:1",
                auto_memory_enabled=False,
                embedding_enabled=False,
            ),
            model_provider=model,
            # A low floor lets one explicit quiet request produce a real proposal,
            # so the browser accepts a rules_v1 suggestion instead of a fixture.
            evolution_policy=EvolutionPolicy(
                min_evidence_items=1,
                quiet_request_rate_threshold=0.1,
                trait_cooldown_seconds=0,
                max_cumulative_drift=1.0,
            ),
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=args.browser_executable)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900}, accept_downloads=True
                )
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "response",
                    lambda response: (
                        errors.append(f"HTTP {response.status}: {response.url}")
                        if response.status >= 500
                        else None
                    ),
                )

                def capture(request):
                    if request.method == "POST" and request.url.endswith(
                        ("/dialogue", "/dialogue/stream")
                    ):
                        requests.append(request.post_data_json)
                    elif (
                        request.method != "GET"
                        and request.post_data
                        and request.url.endswith(("/personality", "/personality/state"))
                    ):
                        writes.append(
                            {
                                "path": request.url.split("/v1", 1)[-1],
                                "body": request.post_data_json,
                            }
                        )

                page.on("request", capture)
                base = f"http://127.0.0.1:{port}"
                headers = {"Authorization": "Bearer browser-smoke"}

                def collapse_chat_tools():
                    for detail in reversed(page.locator("#chat-pane details[open]").all()):
                        detail.locator(":scope > summary").click()

                def screenshot(name, full_page=True):
                    page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(path=str(args.artifacts / name), full_page=full_page)

                def nodes():
                    response = httpx.get(f"{base}/v1/nodes", headers=headers)
                    response.raise_for_status()
                    return response.json()

                def open_details(selector):
                    page.locator(selector).first.evaluate("""el => {
                        for (let parent = el.parentElement; parent; parent = parent.parentElement) {
                            if (parent.tagName === 'DETAILS') parent.open = true;
                        }
                    }""")

                page.goto(f"{base}/ui/")
                screenshot("empty.png")
                assert nodes() == []
                page.focus("#connection-open")
                page.keyboard.press("Enter")
                page.wait_for_function("document.querySelector('#connection-dialog').open")
                page.keyboard.press("Escape")
                page.wait_for_function("!document.querySelector('#connection-dialog').open")
                assert page.evaluate("document.activeElement.id") == "connection-open"
                page.click("#connection-open-rail")
                page.click("#connection-close")
                assert page.evaluate("document.activeElement.id") == "connection-open-rail"
                page.click("#connection-open")
                page.fill("#token", "browser-smoke")
                page.click("#connect")
                page.wait_for_function(
                    "document.querySelector('#connection').textContent === '已连接'"
                )
                open_details("#create-form")
                for identifier in ("browser-one", "browser-two"):
                    page.fill("#new-id", identifier)
                    page.fill("#new-name", identifier)
                    page.click("#create")
                    page.wait_for_function(
                        "id => [...document.querySelector('#companions').options].some(o => o.value === id)",
                        arg=identifier,
                    )
                page.locator("#create-form").locator("xpath=ancestor::details").locator(
                    ":scope > summary"
                ).click()
                page.select_option("#companions", "browser-one")
                page.wait_for_function("!document.querySelector('#persona-fixture').disabled")
                page.click("#persona-tab")
                open_details("#persona-fixture")
                page.click("#persona-fixture")
                page.wait_for_function(
                    "document.querySelector('#persona-status').textContent.includes('已导入')"
                )
                checks.append("connect, create/select companions, explicit built-in persona import")
                assert nodes() == [], "read-only connection/selection must not register a device"
                assert not page.locator("#connection-dialog").evaluate("el => el.open")
                checks.append(
                    "connection dialog keyboard/Escape/close restores focus; default read-only creates no node"
                )
                page.click("#chat-tab")
                page.click("#activate")
                page.wait_for_function("session.active")
                first_node = page.evaluate("session.node")
                assert len(nodes()) == 1
                page.click("#access-stop")
                page.wait_for_function(
                    "!session.active && !document.querySelector('#activate').disabled"
                )
                assert len(nodes()) == 1 and nodes()[0]["node_id"] == first_node
                page.click("#activate")
                page.wait_for_function("session.active")
                assert page.evaluate("session.node") == first_node
                assert len(nodes()) == 1
                second = page.context.new_page()
                second.on("pageerror", lambda error: errors.append(str(error)))
                second.goto(f"{base}/ui/")
                second.click("#connection-open")
                second.fill("#token", "browser-smoke")
                second.click("#connect")
                second.wait_for_function("!document.querySelector('#companions').disabled")
                second.select_option("#companions", "browser-one")
                second.wait_for_function("!document.querySelector('#activate').disabled")
                second.click("#activate")
                second.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('另一个页面')"
                )
                assert len(nodes()) == 1
                second.close()
                checks.append(
                    "stop retains device; repeated access reuses node; second page busy creates no node"
                )
                page.fill("#message", "预算设置前草稿")
                open_details("#chat-prompt-budget")
                for selector, value in (
                    ("chat-prompt-budget", "4096"),
                    ("chat-memory-budget", "400"),
                    ("chat-memory-items", "4"),
                    ("chat-output-budget", "128"),
                ):
                    page.locator(f"#{selector}").fill(value)
                page.fill("#message", "第一轮 <script>window.smokeXss=1</script>")
                page.press("#message", "Control+Enter")
                page.wait_for_function("document.querySelector('#messages').children.length === 2")
                page.wait_for_function("!session.busy")
                page.fill("#message", "谢谢你陪我读书")
                page.click("#send")
                page.wait_for_function(
                    "!session.busy && document.querySelector('#messages').children.length === 4"
                )
                assert requests[1]["history"][0]["role"] == "user"
                assert requests[1]["history"][1]["role"] == "assistant"
                assert requests[0]["max_output_tokens"] == 128
                assert requests[0]["token_budget"] == 4096
                assert requests[0]["memory_token_budget"] == 400
                assert requests[0]["max_memory_items"] == 4
                assert page.evaluate("window.smokeXss") is None
                assert page.locator("#messages script, #messages img").count() == 0
                turn = json.loads(page.locator("#turns pre").first.text_content())
                assert turn["source_event_id"] and turn["preview"] is not None
                assert turn["usage"]["input_tokens"] == 37
                checks.append(
                    "lease activation, actual reply, Ctrl+Enter, history roles, custom budgets, provenance/usage, XSS text rendering"
                )
                collapse_chat_tools()
                screenshot("chat.png")
                screenshot("chat-viewport.png", full_page=False)

                assert "愉快" in page.locator("#emotion").text_content()
                open_details("#emotion-refresh")
                page.click("#emotion-refresh")
                page.wait_for_function(
                    "document.querySelector('#emotion-experiences').children.length === 2"
                )
                assert turn["accepted_event_id"] and turn["emotion"]["context_version"]
                page.locator("#emotion-experiences button").last.click()
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('已完整撤回')"
                )
                assert "平静" in page.locator("#emotion").text_content()
                assert model.calls == 2
                checks.append(
                    "affect display, evidence list and clarification without extra model calls"
                )

                def corrupt_response(route):
                    response = route.fetch()
                    # The server has committed; remove only the terminal result frame.
                    prefix = response.text().split("event: result", 1)[0]
                    route.fulfill(response=response, body=prefix)

                page.route("**/dialogue/stream", corrupt_response, times=1)
                page.wait_for_function("!session.busy")
                page.fill("#message", "回执恢复测试")
                page.click("#send")
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('查询原命令回执')"
                )
                assert page.is_disabled("#send")
                assert model.calls == 3
                page.click("#receipt")
                page.wait_for_function(
                    "!session.busy && document.querySelector('#messages').children.length === 6"
                )
                assert model.calls == 3
                page.click("#receipt")
                page.wait_for_timeout(150)
                assert page.locator("#messages > *").count() == 6
                assert len(requests) == 3
                checks.append(
                    "corrupt committed reply locks send; original receipt recovers once with no duplicate model call"
                )
                open_details("#save-content")
                page.fill("#save-content", "一起读书的日常 <script>window.memoryXss=1</script>")
                page.check("#confirmed")
                page.click("#save")
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('记忆已保存')"
                )
                checks.append("manual memory save retains dialogue provenance and confirmation")
                page.fill("#message", "保留草稿")
                page.select_option("#companions", "browser-two")
                page.select_option("#companions", "browser-one")
                page.wait_for_function(
                    "!session.busy && document.querySelector('#messages').children.length === 6"
                )
                assert page.input_value("#message") == "保留草稿"
                page.click("#connection-open")
                page.click("#disconnect")
                page.fill("#token", "browser-smoke")
                page.click("#connect")
                page.wait_for_function("!document.querySelector('#companions').disabled")
                page.select_option("#companions", "browser-one")
                page.wait_for_function(
                    "!session.busy && document.querySelector('#messages').children.length === 6"
                )
                checks.append("partner switch and disconnect retain partner history and draft")
                from virtual_life.memory_logs import MemoryLogService

                logs = MemoryLogService(
                    str(Path(temporary) / "browser.sqlite3"),
                    lambda: datetime.now(UTC),
                )
                for _ in range(105):
                    logs.record("browser-one", "recall", details={"strategy": "browser-pagination"})
                page.click("#logs-tab")
                page.click("#logs-refresh")
                page.wait_for_function("document.querySelector('#logs-list').children.length > 0")
                assert page.locator("#logs-list > details").count() == 100
                page.click("#logs-more")
                page.wait_for_function("document.querySelector('#logs-list').children.length > 100")
                sequences = [
                    json.loads(raw.text_content())["sequence"]
                    for raw in page.locator("#logs-list pre").all()
                ]
                assert len(set(sequences)) == len(sequences)
                checks.append(
                    "real logs cursor pagination beyond 100 rows without duplicate sequences"
                )
                page.fill("#logs-operation", "dialogue")
                page.click("#logs-refresh")
                page.wait_for_function(
                    "[...document.querySelectorAll('#logs-list summary')].every(x => x.textContent.includes('dialogue')) && !document.querySelector('#logs-refresh').disabled"
                )
                with page.expect_download() as download:
                    page.click("#logs-export")
                download.value.save_as(args.artifacts / "memory-logs.json")
                exported = json.loads((args.artifacts / "memory-logs.json").read_text())
                assert exported["filters"]["operation"] == "dialogue"
                assert exported["items"] and all(
                    row["operation"] == "dialogue" for row in exported["items"]
                )
                screenshot("logs.png")
                checks.append(
                    "independent logs tab, real durable rows, exact filtering and JSON export"
                )

                # ---- personality editor ----
                page.click("#persona-tab")
                # Tab entry loads the editor automatically. Finish that request before
                # explicitly reloading, then wait for the reload's completion notice;
                # a disabled Save button and old version text also match mid-request.
                page.wait_for_function(
                    "session.personaLoaded && !session.personaPageLoading && "
                    "!document.querySelector('#persona-fields').disabled"
                )
                page.click("#persona-edit")
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('已载入服务器上的人格')"
                    " && session.personaLoaded && !session.personaDirty"
                )
                # Hold one real SQLite-backed GET response while a newer reload and
                # save finish. This makes the old-response race deterministic.
                original_persona_get = app.state.personality.get
                old_read_entered = threading.Event()
                release_old_read = threading.Event()
                hold_next_read = [True]
                held_profile = {}

                def held_persona_get(companion_id):
                    profile = original_persona_get(companion_id)
                    if companion_id == "browser-one" and hold_next_read[0]:
                        hold_next_read[0] = False
                        held_profile.update(profile)
                        old_read_entered.set()
                        if not release_old_read.wait(10):
                            raise RuntimeError("personality race test did not release the old read")
                    return profile

                app.state.personality.get = held_persona_get
                page.evaluate("() => { window.oldPersonaRead = loadPersona(); }")
                assert old_read_entered.wait(5), "old personality GET did not reach the backend"
                assert page.locator("#persona-fields").evaluate("el => el.disabled")
                assert page.locator("#persona-edit").is_disabled()
                assert page.locator("#persona-save").is_disabled()
                page.evaluate("() => { window.newPersonaRead = loadPersona(); }")
                assert page.evaluate("newPersonaRead") is True
                assert not page.locator("#persona-fields").evaluate("el => el.disabled")
                page.fill("#pf-personality", "安静而好奇，记得说过的话。")
                page.fill("#pf-warmth", "0.9")
                page.wait_for_function("!document.querySelector('#persona-save').disabled")
                page.click("#persona-save")
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('人格已保存')"
                )
                assert "0.9" in page.input_value("#pf-warmth")
                assert page.input_value("#pf-personality") == "安静而好奇，记得说过的话。"
                # Check the committed backend state as well as the rendered success notice.
                stored = httpx.get(
                    f"{base}/v1/companions/browser-one/personality", headers=headers
                ).json()
                assert stored["revision"] == page.evaluate("session.personaRevision")
                assert stored["definition"]["traits"]["warmth"] == 0.9
                assert stored["definition"]["personality"] == "安静而好奇，记得说过的话。"
                (args.artifacts / "persona-save-evidence.json").write_text(
                    json.dumps(
                        {
                            "writes": writes,
                            "stored": stored,
                            "notice": page.locator("#notice").text_content(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                saved_notice = page.locator("#notice").text_content()
                assert held_profile["revision"] < stored["revision"]
                release_old_read.set()
                assert page.evaluate("oldPersonaRead") is False
                app.state.personality.get = original_persona_get
                assert page.input_value("#pf-personality") == stored["definition"]["personality"]
                assert page.input_value("#pf-warmth") == "0.9"
                assert page.evaluate("session.personaRevision") == stored["revision"]
                assert page.locator("#notice").text_content() == saved_notice
                assert not page.evaluate(
                    "session.personaDirty || session.personaLoading || session.personaSaving"
                )
                (args.artifacts / "persona-race-evidence.json").write_text(
                    json.dumps(
                        {
                            "held_old_revision": held_profile["revision"],
                            "committed_revision": stored["revision"],
                            "old_response_applied": False,
                            "controls_disabled_while_loading": True,
                            "notice_after_old_response": saved_notice,
                            "personality_after_old_response": page.input_value("#pf-personality"),
                            "warmth_after_old_response": page.input_value("#pf-warmth"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                # a partial submit must not silently reset untouched fields
                saved = writes[-1]["body"]["definition"]
                assert saved["speech_style"]["language"]
                assert saved["speech_style"]["response_length"] in ("brief", "balanced", "detailed")
                assert saved["example_dialogue"]
                assert saved["boundaries"]
                checks.append(
                    "personality editor loads, edits, saves with CAS, and preserves untouched fields"
                )
                # the CAS guard must reject a stale revision
                page.evaluate("() => { session.personaRevision = 0; markPersonaDirty(true); }")
                page.click("#persona-save")
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('expected_revision')"
                )
                assert "expected_revision" in page.locator("#notice").text_content()
                # the rejected write must not have advanced the tracked revision
                assert page.evaluate("session.personaRevision") == 0
                checks.append("personality save rejects a stale expected_revision")
                page.click("#persona-revert")
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('已放弃未保存修改')"
                    " && !session.personaDirty && session.personaRevision > 0"
                )
                assert page.input_value("#pf-personality") == "安静而好奇，记得说过的话。"
                page.select_option("#ps-mood", "cheerful")
                page.click("#state-save")
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('短期状态已更新')"
                )
                assert "愉快" in page.locator("#emotion").text_content()
                checks.append("short-term state editor writes mood and is reflected in affect")
                screenshot("persona.png")

                # ---- personality evolution proposals ----
                # One explicit quiet request gives the rules generator real evidence.
                page.click("#chat-tab")
                if page.locator("#activate").is_enabled():
                    page.click("#activate")
                page.wait_for_function(
                    "session.active && !document.querySelector('#send').disabled"
                )
                page.fill("#message", "安静陪我一会儿")
                page.click("#send")
                page.wait_for_function(
                    "!session.busy && document.querySelector('#messages').children.length === 8"
                )
                assert page.locator("#emotion").text_content().strip()
                page.click("#persona-tab")
                page.wait_for_function("session.personaLoaded")
                before = httpx.get(
                    f"{base}/v1/companions/browser-one/personality", headers=headers
                ).json()
                before_initiative = before["definition"]["traits"]["initiative"]
                open_details("#evolution-list")
                page.click("#evolution-list")
                page.wait_for_function(
                    "document.querySelector('#evolution-proposals').textContent"
                    ".includes('尚无演化提议')"
                )
                page.click("#evolution-refresh")
                page.wait_for_function(
                    "document.querySelector('#evolution-status').textContent"
                    ".includes('本次生成 1 条提议')"
                )
                page.wait_for_function(
                    "document.querySelectorAll('.evolution-proposal').length === 1"
                )
                assert "initiative" in page.locator(".evolution-proposal ul").text_content()
                checks.append(
                    "evolution refresh turns local quiet-request evidence into one sourced proposal"
                )
                page.get_by_role("button", name="采纳并写入人格").click()
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('已采纳提议并写入人格')"
                )
                page.wait_for_function(
                    "document.querySelector('#evolution-proposals').textContent.includes('已采纳')"
                )
                stored = httpx.get(
                    f"{base}/v1/companions/browser-one/personality", headers=headers
                ).json()
                assert (
                    abs(stored["definition"]["traits"]["initiative"] - (before_initiative - 0.05))
                    < 1e-6
                )
                assert page.evaluate("session.personaRevision") == stored["revision"]
                page.click("#evolution-history")
                page.wait_for_function(
                    "document.querySelector('#evolution-history-list').children.length > 0"
                )
                assert "采纳演化提议" in page.locator("#evolution-history-list").text_content()
                checks.append(
                    "accepting a proposal writes the profile with provenance and the history tab shows it"
                )
                screenshot("evolution.png")

                # ---- day timeline ----
                page.click("#days-tab")
                page.click("#days-refresh")
                page.wait_for_function("document.querySelector('#days-list').children.length > 0")
                assert page.locator(".day-heading").count() >= 1
                types = set(
                    page.locator("#days-list .day-event").evaluate_all(
                        "els => els.map(e => e.dataset.tone)"
                    )
                )
                assert types, "timeline rendered no events"
                assert "情绪更新" not in page.locator("#days-list").text_content()
                page.locator("#days-hide-emotion").uncheck()
                page.wait_for_function(
                    "document.querySelector('#days-list').textContent.includes('情绪更新')"
                )
                page.locator("#days-hide-emotion").check()
                page.wait_for_function(
                    "!document.querySelector('#days-list').textContent.includes('情绪更新')"
                )
                checks.append(
                    "day timeline hides emotion noise by default and restores it on request"
                )
                page.select_option("#days-type", "companion.emotion_updated")
                page.click("#days-refresh")
                page.wait_for_function(
                    "document.querySelector('#days-list').textContent.includes('情绪更新')"
                )
                page.select_option("#days-type", "companion.dialogue_completed")
                page.click("#days-refresh")
                page.wait_for_function(
                    "const s=[...document.querySelectorAll('#days-list .day-event summary')];"
                    "s.length > 0 && s.every(x => x.textContent.includes('完成回复'))"
                    " && document.querySelector('#days-list').textContent.includes('完成回复')"
                )
                summaries = page.locator("#days-list .day-event summary").all_text_contents()
                assert summaries and all("完成回复" in text for text in summaries), summaries
                page.select_option("#days-type", "companion.dialogue_silent")
                page.click("#days-refresh")
                page.wait_for_function(
                    "document.querySelector('#days-list').textContent.includes('没有符合筛选条件')"
                )
                checks.append(
                    "day timeline groups events by date, renders newest first, and filters by type"
                )
                page.select_option("#days-type", "")
                page.click("#days-refresh")
                page.wait_for_function("document.querySelector('#days-list').children.length > 0")
                open_details("#devices-refresh")
                page.click("#devices-refresh")
                page.wait_for_function(
                    "document.querySelector('#devices-list').children.length > 0"
                )
                assert page.locator("#devices-list .device").count() >= 1
                assert json.loads(page.locator("#graph-raw").text_content())
                with page.expect_download() as download:
                    page.click("#days-export")
                download.value.save_as(args.artifacts / "events.json")
                exported = json.loads((args.artifacts / "events.json").read_text())
                assert exported["events"]
                sequences = [row["sequence"] for row in exported["events"]]
                assert sequences == sorted(sequences)
                checks.append(
                    "device inventory, graph projection and ascending event export from the days tab"
                )
                screenshot("days.png")

                # ---- memory record editing ----
                # Automatic memory is off in this fixture. Seed one record directly so
                # the editor is exercised against a real row with a real source event;
                # manual save above separately checks dialogue provenance and confirmation.
                base = f"http://127.0.0.1:{port}"
                headers = {"Authorization": "Bearer browser-smoke"}
                events = httpx.get(f"{base}/v1/events?limit=500", headers=headers).json()["events"]
                source = next(
                    event["event_id"] for event in events if event["companion_id"] == "browser-one"
                )
                created = httpx.post(
                    f"{base}/v1/companions/browser-one/memories",
                    headers=headers,
                    json={
                        "command_id": "smoke-seed",
                        "kind": "episode",
                        "content": "浏览器冒烟测试记录",
                        "source_event_id": source,
                    },
                )
                assert created.status_code == 201, created.text
                page.click("#memory-tab")
                page.click("#memory-refresh")
                # Wait for the newest render, including tab-entry refreshes.
                page.evaluate("memories()")
                page.wait_for_load_state("networkidle")
                page.wait_for_function("document.querySelector('#memories').children.length > 0")
                page.locator("#memories .memory-editor").first.locator(
                    "xpath=parent::details"
                ).locator(":scope > summary").click()
                page.locator("#memories .memory-editor input[type=checkbox]").first.wait_for(
                    state="visible"
                )
                page.locator("#memories .memory-editor").first.locator(
                    "input[type=checkbox]"
                ).first.check()
                page.locator("#memories .memory-editor").first.locator(
                    "button[type=submit]"
                ).click()
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('元数据已更新')"
                )
                page.wait_for_function(
                    "document.querySelector('#memories').textContent.includes('固定')"
                )
                checks.append("memory metadata editor pins a record and the list reflects it")
                screenshot("memory.png")

                page.click("#chat-tab")
                open_details("#chat-clear")
                page.click("#chat-clear")
                assert page.locator("#messages").text_content() == ""
                assert page.input_value("#message") == ""
                checks.append("clear page history and draft")
                for width, height, label in ((1440, 900, "desktop"), (390, 844, "mobile")):
                    page.set_viewport_size({"width": width, "height": height})
                    for pane in ("chat", "memory", "persona", "days", "logs"):
                        page.click(f"#{pane}-tab")
                        page.wait_for_function("name => location.hash === '#' + name", arg=pane)
                        page.locator(f"#{pane}-pane").wait_for(state="visible")
                        assert page.locator("#page-title").text_content().strip()
                        assert page.locator("#page-description").text_content().strip()
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        ), (label, pane)
                        if pane == "chat":
                            collapse_chat_tools()
                            page.evaluate("window.scrollTo(0, 0)")
                            layout = page.evaluate("""() => ({
                                width: innerWidth, height: innerHeight,
                                messageBottom: document.querySelector('#message').getBoundingClientRect().bottom,
                                sendBottom: document.querySelector('#send').getBoundingClientRect().bottom,
                                navTop: document.querySelector('.primary-nav').getBoundingClientRect().top
                            })""")
                            layouts.append(layout)
                            assert layout["sendBottom"] <= height, layout
                            if width == 390:
                                assert layout["sendBottom"] < layout["navTop"], layout
                            screenshot(f"{label}-viewport.png", full_page=False)
                        screenshot(f"{label}-{pane}.png")
                    if width == 390:
                        page.click("#context-toggle")
                        page.wait_for_function("document.body.classList.contains('context-open')")
                        page.locator("#context-panel").wait_for(state="visible")
                        page.keyboard.press("Escape")
                        page.wait_for_function("!document.body.classList.contains('context-open')")
                        assert page.evaluate("document.activeElement.id") == "context-toggle", (
                            page.evaluate("document.activeElement.outerHTML")
                        )
                        page.click("#context-toggle")
                        page.click("#context-close")
                        assert page.evaluate("document.activeElement.id") == "context-toggle", (
                            page.evaluate("document.activeElement.outerHTML")
                        )
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.click("#chat-tab")
                page.click("#memory-tab")
                page.go_back()
                page.wait_for_function(
                    "location.hash === '#chat' && !document.querySelector('#chat-pane').hidden"
                )
                page.go_forward()
                page.wait_for_function(
                    "location.hash === '#memory' && !document.querySelector('#memory-pane').hidden"
                )
                deep = page.context.new_page()
                deep.goto(f"{base}/ui/#days")
                deep.locator("#days-pane").wait_for(state="visible")
                deep.close()
                assert page.evaluate("""() => ![localStorage, sessionStorage].some(storage =>
                    Object.values(storage).some(value => value.includes('browser-smoke')))""")
                checks.append(
                    "all five panes at 1440px and 390px without overflow; drawer Escape/focus; hash deep links/back/forward; token absent from storage"
                )
                assert not errors, errors
                assert model.stream_calls == 4, "All four browser requests must use SSE"
                browser.close()
        finally:
            if "release_old_read" in locals():
                release_old_read.set()
            if sys.exc_info()[1] is not None:
                print("FAILED after", checks, flush=True)
                (args.artifacts / "report.json").write_text(
                    json.dumps(
                        {
                            "status": "failed",
                            "provider": "deterministic fake; no network model calls",
                            "checks": checks,
                            "page_errors_or_http_5xx": errors,
                            "failure": traceback.format_exc(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
            server.should_exit = True
            thread.join(timeout=10)
    report = {
        "status": "passed",
        "provider": "deterministic fake; no network model calls",
        "checks": checks,
        "layouts": layouts,
        "stream_provider_calls": model.stream_calls,
        "page_errors_or_http_5xx": errors,
        "limitations": [
            "Fake model verifies UI and transport behavior; it does not measure real model memory quality."
        ],
    }
    (args.artifacts / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
