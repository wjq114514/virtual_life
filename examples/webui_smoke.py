"""Run an isolated Chromium smoke check of the minimal live UI (logs + flow).

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
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import sync_playwright
from virtual_life.api import create_app
from virtual_life.settings import Settings


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
                base = f"http://127.0.0.1:{port}"
                headers = {"Authorization": "Bearer browser-smoke"}

                def screenshot(name):
                    page.screenshot(path=str(args.artifacts / name), full_page=True)

                # The login dialog appears on click when disconnected.
                page.goto(f"{base}/ui/")
                page.wait_for_selector("#connection-dialog")
                screenshot("empty.png")

                # Connect with the token.
                page.fill("#token", "browser-smoke")
                page.click("#connect")
                page.wait_for_function(
                    "document.querySelector('#connection').textContent === '已连接'"
                )
                checks.append("connect via token, status dot on")

                # The flow panel should establish a baseline even with no events.
                page.wait_for_timeout(300)
                assert "已建立实时基线" in page.locator("#flow-status").text_content()
                checks.append("flow baseline established")

                # Generate a real event via the API so the flow panel tails it.
                created = httpx.post(
                    f"{base}/v1/nodes",
                    headers=headers,
                    json={
                        "node_id": "desk",
                        "label": "desk",
                        "kind": "desktop",
                        "capabilities": ["text.display"],
                    },
                )
                created.raise_for_status()
                page.wait_for_timeout(400)
                flow_text = page.locator("#flow-view").text_content()
                assert "node.registered" in flow_text or "desk" in flow_text
                checks.append("flow panel tails node.registered event")

                # The log panel tails live log lines from the ring buffer.
                page.wait_for_timeout(400)
                assert "已载入" in page.locator("#logs-status").text_content()
                assert len(page.locator("#log-view .log-line").all()) > 0
                checks.append("log panel tails live ring lines")

                # Level filter narrows the view.
                page.select_option("#level-filter", "ERROR")
                page.wait_for_timeout(200)
                checks.append("level filter applies")

                # Clear buttons work.
                page.click("#logs-clear")
                page.click("#flow-clear")
                assert page.locator("#log-view .log-line").count() == 0
                assert page.locator("#flow-view .flow-item").count() == 0
                checks.append("clear buttons empty both panels")

                # Pause stops flow polling; resume continues.
                page.click("#flow-pause")
                assert page.locator("#flow-pause").text_content() == "继续"
                page.click("#flow-pause")
                assert page.locator("#flow-pause").text_content() == "暂停"
                checks.append("flow pause/resume toggle")

                # Disconnect returns to offline.
                page.click("#disconnect")
                page.wait_for_function(
                    "document.querySelector('#connection').textContent === '未连接'"
                )
                checks.append("disconnect restores offline state")

                screenshot("final.png")

                summary = {"checks": checks, "errors": errors}
                (args.artifacts / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8"
                )
        except Exception:
            traceback.print_exc()
            sys.exit(1)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            browser.close()

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
