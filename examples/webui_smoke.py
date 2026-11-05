"""Run an isolated Chromium smoke check of the `/ui/` workspace.

The workspace has seven views (总览 / 日志 / 事件 / 记忆 / 知识图谱 / 内心活动 / 设备).
The check starts a throwaway runtime on a temporary SQLite database, seeds one
companion and one node, then walks every view and the shared chrome controls
(token dialog, theme toggle, companion switch, level filter, row expansion).

Run: uv run --with playwright python examples/webui_smoke.py
First run: uv run --with playwright playwright install chromium
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
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import sync_playwright
from virtual_life.api import create_app
from virtual_life.settings import Settings

VIEWS = ["overview", "logs", "events", "memories", "graph", "inner", "devices"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/webui-review"))
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    with tempfile.TemporaryDirectory(prefix="virtual-life-browser-") as temporary:
        app = create_app(
            Settings(
                database_path=str(Path(temporary) / "browser.sqlite3"),
                api_token="browser-smoke",
                model_name="browser-fake",
                model_base_url="http://127.0.0.1:1",
                auto_memory_enabled=False,
                embedding_enabled=False,
            ),
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            base = f"http://127.0.0.1:{port}"
            headers = {"Authorization": "Bearer browser-smoke"}

            # Seed a companion and a node so the views render real data instead
            # of empty states. Both calls are ordinary public API calls.
            with httpx.Client(base_url=base, headers=headers, timeout=10.0) as client:
                client.post(
                    "/v1/companions",
                    json={"companion_id": "browser-demo", "name": "浏览器伙伴"},
                ).raise_for_status()
                client.post(
                    "/v1/nodes",
                    json={
                        "node_id": "desk",
                        "label": "desk",
                        "kind": "desktop",
                        "capabilities": ["text.display"],
                    },
                ).raise_for_status()

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=args.browser_executable)
                context = browser.new_context(viewport={"width": 1440, "height": 900})
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

                def screenshot(name):
                    page.screenshot(path=str(args.artifacts / name), full_page=True)

                def visit(view):
                    page.click(f'.nav-item[data-view="{view}"]')
                    page.wait_for_function("document.querySelector('#view').children.length > 0")
                    page.wait_for_timeout(350)

                # No session yet: the token dialog opens on the first 401.
                page.goto(f"{base}/ui/")
                page.wait_for_selector("#auth-dialog[open]")
                screenshot("00-auth.png")
                checks.append("unauthenticated visit opens the token dialog")

                page.fill("#auth-token", "browser-smoke")
                page.click("#auth-submit")
                page.wait_for_function(
                    "document.querySelector('#conn-text').textContent === '已连接'"
                )
                checks.append("POST /v1/session connects and the status dot turns on")

                # Chrome: seven nav entries, the companion switch is populated.
                assert page.locator(".nav-item").count() == len(VIEWS)
                page.wait_for_function(
                    "document.querySelectorAll('#companion-select option').length > 0"
                )
                assert page.locator("#companion-select option").count() >= 2
                checks.append("seven nav entries and a populated companion switch")

                # 总览: summary cards plus the recent inner-activity timeline.
                page.wait_for_timeout(700)
                assert page.locator("#view .card").count() >= 4
                assert "总览" in page.locator("#view-title").text_content()
                screenshot("01-overview.png")
                checks.append("overview renders summary cards")

                # 日志: the ring buffer tails live lines; a row expands on click.
                visit("logs")
                page.wait_for_function("document.querySelectorAll('.logrow').length > 0")
                assert page.locator("#view-title").text_content() == "日志"
                first_row = page.locator(".logrow").first
                first_row.click()
                page.wait_for_selector(".logrow.is-open .log-exp")
                checks.append("log view tails live lines and expands a row")

                # Level chips toggle, which re-filters without losing the buffer.
                error_chip = page.locator(".chip", has_text="ERROR").first
                error_chip.click()
                page.wait_for_timeout(250)
                assert "is-on" not in (error_chip.get_attribute("class") or "")
                error_chip.click()
                page.wait_for_timeout(250)
                checks.append("level chips toggle the log filter")
                screenshot("02-logs.png")

                # 事件: an event created after the baseline shows up.
                visit("events")
                assert page.locator("#view-title").text_content() == "事件"
                page.wait_for_function("document.querySelectorAll('#view .evt').length > 0")
                checks.append("event view tails runtime events")
                screenshot("03-events.png")

                # 记忆: empty on a fresh database, but the tabs and the stats
                # sidebar still render (no exception path).
                visit("memories")
                assert page.locator("#view .tab").count() == 3
                assert page.locator("#view .card, #view .empty").count() > 0
                page.click("#view .tab >> nth=1")
                page.wait_for_timeout(400)
                page.click("#view .tab >> nth=2")
                page.wait_for_timeout(400)
                checks.append("memory tabs switch between records, summaries and diagnostics")
                screenshot("04-memories.png")

                # 知识图谱: no distilled graph yet, so the empty state explains it.
                visit("graph")
                assert page.locator("#view .empty, #view .graphwrap").count() > 0
                checks.append("knowledge graph view renders a graph or its empty state")
                screenshot("05-graph.png")

                # 内心活动: inner-life state plus the activity timeline.
                visit("inner")
                assert page.locator("#view .card").count() >= 1
                checks.append("inner-life view renders its state cards")
                screenshot("06-inner.png")

                # 设备: the seeded node appears and search narrows the table.
                visit("devices")
                page.wait_for_function("document.querySelectorAll('#view tbody tr').length > 0")
                rows_before = page.locator("#view tbody tr").count()
                page.fill("#view .search input", "no-such-node")
                page.wait_for_timeout(400)
                rows_after = page.locator("#view tbody tr").count()
                assert rows_after < rows_before
                checks.append("device table lists nodes and the search narrows it")
                screenshot("07-devices.png")

                # Theme toggle flips the document attribute both ways.
                before = page.evaluate("document.documentElement.dataset.theme || ''")
                page.click("#btn-theme")
                after = page.evaluate("document.documentElement.dataset.theme || ''")
                assert before != after
                checks.append("theme toggle flips light/dark")

                # The switcher re-renders the active view for another companion.
                page.select_option("#companion-select", "browser-demo")
                page.wait_for_timeout(500)
                assert page.locator("#view .card, #view .empty, #view table").count() > 0
                checks.append("companion switch re-renders the active view")

                # Manual refresh and the auth dialog round-trip.
                page.click("#btn-refresh")
                page.wait_for_timeout(600)
                page.click("#btn-session")
                page.wait_for_selector("#auth-dialog[open]")
                page.click("#auth-cancel")
                page.wait_for_selector("#auth-dialog[open]", state="detached")
                checks.append("refresh and the token dialog round-trip")

                screenshot("08-final.png")

                summary = {"checks": checks, "errors": errors}
                (args.artifacts / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8"
                )
                browser.close()
        except Exception:
            traceback.print_exc()
            sys.exit(1)
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    print("WebUI smoke checks:")
    for check in checks:
        print(f"  ✓ {check}")
    if errors:
        print("Errors observed:")
        for error in errors:
            print(f"  ✗ {error}")
        sys.exit(1)
    print(f"All {len(checks)} checks passed.")


if __name__ == "__main__":
    main()
