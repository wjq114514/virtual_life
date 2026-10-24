"""Exercise background companionship with Chromium, SQLite and a local fake provider.

No external model, SSH, production database or fabricated browser attention is used.
Host attention is an explicit authenticated test fixture.
"""

import argparse
import json
import os
import socket
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
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class Provider:
    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelResult(
            "歇一会儿，我在。<img src=x onerror=alert(1)>", "local-fake", ModelUsage(40, 12)
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/webui-proactivity"))
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, errors, policy_writes = [], [], []
    provider = Provider()
    with tempfile.TemporaryDirectory(prefix="virtual-life-proactivity-ui-") as temporary:
        app = create_app(
            Settings(
                database_path=str(Path(temporary) / "runtime.sqlite"),
                api_token="ui-proactive-test",
                model_name="local-fake",
                model_base_url="http://127.0.0.1:1",
                auto_memory_enabled=False,
                embedding_enabled=False,
                proactivity_poll_seconds=0.05,
                lease_ttl_seconds=60,
                node_ttl_seconds=120,
            ),
            model_provider=provider,
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            with httpx.Client(
                base_url=base, headers={"Authorization": "Bearer ui-proactive-test"}
            ) as api:

                def post(path, body):
                    response = api.post(path, json=body)
                    assert response.is_success, response.text
                    return response.json()

                post(
                    "/v1/nodes",
                    {
                        "node_id": "host",
                        "label": "宿主测试设备",
                        "kind": "desktop",
                        "capabilities": ["text.display", "attention.observe"],
                        "device": {"device_id": "host"},
                    },
                )
                for companion in ("one", "two", "three"):
                    post("/v1/companions", {"companion_id": companion, "name": companion})
                    post(
                        f"/v1/companions/{companion}/personality",
                        {
                            "command_id": "persona-" + companion,
                            "expected_revision": 0,
                            "definition": {"traits": {"initiative": 0.8}},
                        },
                    )
                policy_path = "/v1/companions/one/proactivity"
                initial = api.get(policy_path).json()
                post(
                    policy_path,
                    {
                        "command_id": "custom-budgets",
                        "expected_revision": initial["revision"],
                        "policy": {
                            **initial["policy"],
                            "token_budget": 1444,
                            "memory_token_budget": 222,
                        },
                    },
                )
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=True, executable_path=args.browser_executable
                    )
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on(
                        "request",
                        lambda request: (
                            policy_writes.append(request.post_data_json)
                            if request.method == "POST" and request.url.endswith("/proactivity")
                            else None
                        ),
                    )
                    page.goto(base + "/ui/")
                    page.click("#connection-open")
                    page.fill("#token", "ui-proactive-test")
                    page.click("#connect")
                    page.wait_for_function("!document.querySelector('#companions').disabled")
                    page.select_option("#companions", "one")
                    page.locator("#background-companionship > summary").click()
                    page.wait_for_function(
                        "session.background.loaded && !session.background.running"
                    )
                    assert not page.is_checked("#background-enabled")
                    assert not policy_writes
                    assert "尚无宿主信号" in page.locator("#background-attention").inner_text()
                    assert len(api.get("/v1/nodes").json()) == 1
                    checks.append(
                        "read-only policy defaults off; no implicit writes, nodes or host signals"
                    )
                    page.fill("#background-daily", "3")
                    page.fill("#background-cooldown", "30")
                    page.check("#background-enabled")
                    page.evaluate("refreshBackground()")
                    assert page.input_value("#background-daily") == "3"
                    assert not policy_writes
                    latest = api.get(policy_path).json()
                    post(
                        policy_path,
                        {
                            "command_id": "concurrent-policy",
                            "expected_revision": latest["revision"],
                            "policy": {**latest["policy"], "daily_limit": 2},
                        },
                    )
                    page.click("#background-save")
                    page.wait_for_function(
                        "document.querySelector('#background-policy-status').textContent.includes('保存失败')"
                    )
                    assert page.is_checked("#background-enabled")
                    assert page.input_value("#background-daily") == "3"
                    page.click("#background-save")
                    page.wait_for_function(
                        "document.querySelector('#background-policy-status').textContent.includes('已保存')"
                    )
                    saved = api.get(policy_path).json()
                    assert saved["policy"]["enabled"] and saved["policy"]["daily_limit"] == 3
                    assert saved["policy"]["token_budget"] == 1444
                    assert saved["policy"]["memory_token_budget"] == 222
                    checks.append(
                        "explicit CAS save, conflict recovery, dirty refresh preservation and untouched budgets"
                    )
                    page.locator("#device-settings > summary").click()
                    page.select_option("#local-device", "host")
                    page.click("#activate")
                    page.wait_for_function("session.active")
                    page.evaluate("pollBackgroundOutput()")
                    page.wait_for_function("session.backgroundOutput.cursor !== null")
                    assert page.evaluate("session.node") == "host"
                    assert len(api.get("/v1/nodes").json()) == 1

                    # Explicit host fixture grants chat participation; page visibility
                    # does not imply host attention or permission to interrupt.
                    state = api.get("/v1/companions/one").json()
                    post(
                        "/v1/companions/one/context",
                        {
                            "command_id": "host-chat-mode",
                            "node_id": "host",
                            "epoch": state["epoch"],
                            "expected_revision": state["revision"],
                            "interaction_mode": "chat",
                        },
                    )

                    def attention(sequence, **changes):
                        return post(
                            "/v1/nodes/host/attention",
                            {
                                "command_id": f"host-attention-{sequence}",
                                "sample": {
                                    "session_id": "explicit-host-fixture",
                                    "sequence": sequence,
                                    "sampled_at": datetime.now(UTC).isoformat(),
                                    "ttl_seconds": 120,
                                    "activity": "docs",
                                    "focus": "unfocused",
                                    "availability": "available",
                                    "user_present": True,
                                    "locked": False,
                                    "meeting": False,
                                    "screen_sharing": False,
                                    "boundary": "break_started",
                                    "boundary_id": f"boundary-{sequence}",
                                    **changes,
                                },
                            },
                        )

                    attention(1, focus="focused")
                    page.evaluate("refreshBackground()")
                    page.wait_for_function(
                        "document.querySelector('#background-focus').textContent.includes('保护中')"
                    )
                    page.wait_for_function(
                        "document.querySelector('#background-reason').textContent.includes('专注')"
                    )
                    assert not provider.requests
                    assert page.locator("#background-decisions small").count() >= 1
                    assert "宿主事件" in page.locator("#background-decisions").inner_text()
                    checks.append(
                        "explicit host focus protects quiet time; decisions expose source and time"
                    )
                    attention(2)
                    page.evaluate("refreshBackground()")
                    page.wait_for_function(
                        "document.querySelectorAll('#messages .proactive').length === 1",
                        timeout=15000,
                    )
                    assert len(provider.requests) == 1
                    assert page.locator("#messages .user").count() == 0
                    assert page.locator("#messages img").count() == 0
                    assert page.evaluate("conversation().history.length") == 0
                    assert page.evaluate("conversation().messages[0].proactive")
                    page.evaluate("pollBackgroundOutput()")
                    assert page.locator("#messages .proactive").count() == 1
                    attention(3)
                    page.evaluate("refreshBackground()")
                    page.wait_for_function(
                        "document.querySelector('#background-reason').textContent.includes('冷却')"
                    )
                    assert len(provider.requests) == 1
                    checks.append(
                        "one autonomous text-only remark; no forged user turn; deduplication and cooldown"
                    )
                    for sequence in range(4, 26):
                        attention(sequence)
                    page.evaluate("refreshBackground()")
                    assert page.locator("#background-decisions li").count() == 20
                    newest_source = api.get("/v1/nodes/host/attention").json()["source_event_id"]
                    assert (
                        newest_source in page.locator("#background-decisions li").first.inner_text()
                    )
                    assert len(provider.requests) == 1
                    page.click("#access-stop")
                    page.wait_for_function("!session.active")
                    page.evaluate("pollBackgroundOutput()")
                    assert page.evaluate("session.backgroundOutput.cursor") is None
                    assert page.locator("#messages .proactive").count() == 1
                    original_policy_get = app.state.proactivity.get_policy
                    old_status_entered, release_old_status = threading.Event(), threading.Event()
                    hold_policy = [True]

                    def delayed_policy_get(companion_id):
                        result = original_policy_get(companion_id)
                        if companion_id == "one" and hold_policy[0]:
                            hold_policy[0] = False
                            old_status_entered.set()
                            assert release_old_status.wait(10), "old policy GET was not released"
                        return result

                    app.state.proactivity.get_policy = delayed_policy_get
                    page.evaluate("() => { window.oldBackgroundRefresh = refreshBackground(); }")
                    assert old_status_entered.wait(5)
                    page.select_option("#companions", "two")
                    page.evaluate("refreshBackground()")
                    page.wait_for_function(
                        "session.background.loaded && !session.background.running"
                    )
                    assert not page.is_checked("#background-enabled")
                    assert page.locator("#messages .proactive").count() == 0
                    assert page.locator("#background-decisions li").count() == 0
                    release_old_status.set()
                    page.evaluate("oldBackgroundRefresh")
                    app.state.proactivity.get_policy = original_policy_get
                    assert not page.is_checked("#background-enabled")
                    assert page.locator("#background-decisions li").count() == 0
                    checks.append(
                        "stop resets the output cursor; late policy responses and companion histories stay isolated"
                    )
                    # A real output response can become unsafe while it travels.
                    # Hold its valid database result, then inject a focused host
                    # sample before letting the browser receive the old result.
                    from virtual_life.proactivity_output import ProactivityOutputService

                    page.check("#background-enabled")
                    page.click("#background-save")
                    page.wait_for_function(
                        "document.querySelector('#background-policy-status').textContent.includes('已保存')"
                    )
                    page.click("#activate")
                    page.wait_for_function("session.active")
                    page.evaluate("pollBackgroundOutput()")
                    page.wait_for_function("session.backgroundOutput.cursor !== null")
                    state = api.get("/v1/companions/two").json()
                    post(
                        "/v1/companions/two/context",
                        {
                            "command_id": "host-chat-mode-two",
                            "node_id": "host",
                            "epoch": state["epoch"],
                            "expected_revision": state["revision"],
                            "interaction_mode": "chat",
                        },
                    )
                    page.evaluate("refreshBackground()")
                    original_output_poll = ProactivityOutputService.poll
                    old_output_entered, release_old_output = threading.Event(), threading.Event()
                    delayed_companion = "two"
                    late_output_calls = []

                    def delayed_output(service, companion_id, *positional, **keyword):
                        result = original_output_poll(service, companion_id, *positional, **keyword)
                        if companion_id == delayed_companion:
                            late_output_calls.append(
                                {"companion_id": companion_id, "items": len(result["items"])}
                            )
                        if (
                            companion_id == delayed_companion
                            and result["items"]
                            and not old_output_entered.is_set()
                        ):
                            old_output_entered.set()
                            assert release_old_output.wait(10), "old output was not released"
                        return result

                    ProactivityOutputService.poll = delayed_output
                    attention(26)
                    page.evaluate("() => { window.oldOutputPoll = pollBackgroundOutput(); }")
                    assert old_output_entered.wait(5), "eligible output was not polled"
                    attention(27, focus="focused", boundary="none", boundary_id=None)
                    release_old_output.set()
                    page.evaluate("oldOutputPoll")
                    page.wait_for_function("!session.backgroundOutput.running")
                    ProactivityOutputService.poll = original_output_poll
                    assert len(provider.requests) == 2
                    assert page.locator("#messages .proactive").count() == 0
                    assert page.evaluate("conversation().history.length") == 0
                    page.evaluate("refreshBackground()")
                    assert "保护中" in page.locator("#background-focus").inner_text()
                    checks.append(
                        "fresh attention/focus recheck drops a real remark when the host enters focus during delivery"
                    )
                    page.click("#access-stop")
                    page.select_option("#companions", "three")
                    page.evaluate("refreshBackground()")
                    page.check("#background-enabled")
                    page.click("#background-save")
                    page.wait_for_function(
                        "document.querySelector('#background-policy-status').textContent.includes('已保存')"
                    )
                    page.click("#activate")
                    page.wait_for_function("session.active")
                    page.evaluate("pollBackgroundOutput()")
                    page.wait_for_function("session.backgroundOutput.cursor !== null")
                    state = api.get("/v1/companions/three").json()
                    post(
                        "/v1/companions/three/context",
                        {
                            "command_id": "host-chat-mode-three",
                            "node_id": "host",
                            "epoch": state["epoch"],
                            "expected_revision": state["revision"],
                            "interaction_mode": "chat",
                        },
                    )
                    delayed_companion = "three"
                    old_output_entered, release_old_output = threading.Event(), threading.Event()
                    ProactivityOutputService.poll = delayed_output
                    attention(28)
                    page.evaluate("refreshBackground()")
                    page.evaluate("() => { window.oldOutputPoll = pollBackgroundOutput(); }")
                    assert old_output_entered.wait(5), "policy-race output was not polled"
                    current_policy = api.get("/v1/companions/three/proactivity").json()
                    post(
                        "/v1/companions/three/proactivity",
                        {
                            "command_id": "disable-during-output",
                            "expected_revision": current_policy["revision"],
                            "policy": {**current_policy["policy"], "enabled": False},
                        },
                    )
                    release_old_output.set()
                    page.evaluate("oldOutputPoll")
                    page.wait_for_function("!session.backgroundOutput.running")
                    ProactivityOutputService.poll = original_output_poll
                    assert len(provider.requests) == 3
                    assert page.locator("#messages .proactive").count() == 0
                    third_calls = [
                        call for call in late_output_calls if call["companion_id"] == "three"
                    ]
                    assert (
                        len(third_calls) >= 2
                        and sum(call["items"] for call in third_calls) == 1
                        and third_calls[-1]["items"] == 0
                    ), third_calls
                    checks.append(
                        "final server revalidation drops a late remark after policy is disabled during delivery"
                    )
                    (args.artifacts / "late-output-evidence.json").write_text(
                        json.dumps(late_output_calls, indent=2) + "\n"
                    )
                    page.click("#access-stop")
                    page.select_option("#companions", "one")
                    assert page.locator("#messages .proactive").count() == 1
                    page.evaluate("refreshBackground()")
                    page.wait_for_function(
                        "session.background.loaded && !session.background.running"
                    )
                    assert page.locator("#background-decisions li").count() == 20
                    for width in (1440, 390):
                        page.set_viewport_size({"width": width, "height": 1000})
                        if page.evaluate("document.body.classList.contains('context-open')"):
                            page.click("#context-close")
                            page.wait_for_function(
                                "!document.body.classList.contains('context-open')"
                            )
                        if width <= 1200:
                            page.locator("#context-panel").wait_for(state="hidden")
                        page.evaluate("window.scrollTo(0, 0)")
                        page.screenshot(
                            path=str(args.artifacts / f"background-{width}.png"),
                            full_page=True,
                            animations="disabled",
                        )
                        if width == 390:
                            page.locator("#background-companionship").evaluate(
                                "el => el.scrollIntoView({block: 'start'})"
                            )
                            page.screenshot(
                                path=str(args.artifacts / "background-390-panel.png"),
                                animations="disabled",
                            )
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 1"
                        )
                    checks.append("collapsible panel and history fit desktop and mobile")
                    assert not errors, errors
                    browser.close()
        except Exception:
            (args.artifacts / "report.json").write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "checks": checks,
                        "errors": errors,
                        "failure": traceback.format_exc(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            raise
        finally:
            if "release_old_status" in locals():
                release_old_status.set()
            if "release_old_output" in locals():
                release_old_output.set()
                ProactivityOutputService.poll = original_output_poll
            server.should_exit = True
            thread.join(timeout=10)
    report = {
        "status": "passed",
        "checks": checks,
        "provider_calls": len(provider.requests),
        "errors": errors,
        "fixture": "real local Uvicorn/SQLite/Chromium; authenticated explicit host attention; deterministic provider",
    }
    (args.artifacts / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
