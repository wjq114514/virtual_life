"""Verify live SSE in isolated Chromium with a paused local provider.

Run: uv run --with playwright python examples/webui_stream_smoke.py
"""

import argparse
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import traceback
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import sync_playwright
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings


class PausedModel:
    def __init__(self):
        self.calls = 0
        self.release = threading.Event()
        self.started = threading.Event()

    def arm(self):
        self.release.clear()
        self.started.clear()

    def complete(self, request):
        raise AssertionError("Browser must use the streaming provider")

    def stream(self, request, on_delta):
        self.calls += 1
        on_delta("临时中文首段")
        self.started.set()
        if not self.release.wait(25):
            raise AssertionError("Browser did not release the paused provider")
        on_delta("，最终回复。")
        return ModelResult("临时中文首段，最终回复。", "stream-browser-fake", ModelUsage(37, 11))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/webui-stream-review"))
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, errors, captured = [], [], []
    model = PausedModel()
    report = {"status": "failed", "checks": checks, "page_errors": errors}
    with tempfile.TemporaryDirectory(prefix="virtual-life-stream-browser-") as temporary:
        database = Path(temporary) / "stream.sqlite3"
        app = create_app(
            Settings(
                database_path=str(database),
                api_token="stream-browser-smoke",
                model_name="stream-browser-fake",
                model_base_url="http://127.0.0.1:1",
                auto_memory_enabled=False,
                embedding_enabled=False,
            ),
            model_provider=model,
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        headers = {"Authorization": "Bearer stream-browser-smoke"}
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
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "request",
                    lambda request: (
                        captured.append(request.post_data_json)
                        if request.method == "POST" and request.url.endswith("/dialogue/stream")
                        else None
                    ),
                )
                page.goto(f"{base}/ui/")
                page.click("#connection-open")
                page.fill("#token", "stream-browser-smoke")
                page.click("#connect")
                page.wait_for_function("!document.querySelector('#companions').disabled")
                for identifier in ("stream-one", "stream-two"):
                    response = httpx.post(
                        f"{base}/v1/companions",
                        headers=headers,
                        json={"companion_id": identifier, "name": identifier},
                    )
                    response.raise_for_status()
                # Refresh the existing read-only connection so options include both fixtures.
                page.evaluate("companions()")
                page.wait_for_function("document.querySelector('#companions').options.length >= 2")
                page.select_option("#companions", "stream-one")
                page.wait_for_function("!document.querySelector('#activate').disabled")
                page.click("#activate")
                page.wait_for_function("session.active")

                def send(message):
                    previous_assistants = page.evaluate(
                        "session.history.filter(x => x.role === 'assistant').length"
                    )
                    model.arm()
                    page.wait_for_function("!document.querySelector('#send').disabled")
                    page.fill("#message", message)
                    with page.expect_response(
                        lambda response: response.url.endswith("/dialogue/stream")
                    ) as event:
                        page.click("#send")
                    response = event.value
                    assert response.status == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    assert "no-store" in response.headers["cache-control"]
                    assert response.headers["x-accel-buffering"] == "no"
                    page.wait_for_function(
                        "document.querySelector('#messages .streaming')?.textContent.includes('临时中文首段')"
                    )
                    assert model.started.is_set() and not model.release.is_set()
                    command = captured[-1]["command_id"]
                    with sqlite3.connect(database) as connection:
                        row = connection.execute(
                            "SELECT status, result_json FROM dialogue_commands WHERE command_id=?",
                            (command,),
                        ).fetchone()
                    assert row == ("pending", None), row
                    assert (
                        page.evaluate("session.history.filter(x => x.role === 'assistant').length")
                        == previous_assistants
                    )
                    return command

                def wait_receipt(command):
                    for _ in range(100):
                        response = httpx.get(
                            f"{base}/v1/companions/stream-one/dialogue/{command}", headers=headers
                        )
                        if response.status_code == 200:
                            return response.json()
                        page.wait_for_timeout(50)
                    raise AssertionError(f"No committed receipt: {response.text}")

                command = send("首轮")
                assert page.locator("#messages .assistant:not(.streaming)").count() == 0
                assert (
                    page.evaluate(
                        "conversation().messages.filter(x => x.role === 'assistant').length"
                    )
                    == 0
                )
                page.screenshot(
                    path=str(args.artifacts / "pending-first-chunk.png"), full_page=True
                )
                checks.append(
                    "live Chinese first chunk appears while SQLite command is pending and formal assistant history is empty; SSE anti-buffering headers"
                )
                model.release.set()
                page.wait_for_function("!session.busy && !activeStream")
                assert page.locator("#messages .assistant").count() == 1
                assert page.evaluate("session.history.length") == 2
                assert wait_receipt(command)["assistant_message"] == "临时中文首段，最终回复。"
                page.click("#receipt")
                page.wait_for_function("!session.busy")
                assert page.locator("#messages .assistant").count() == 1 and model.calls == 1
                checks.append(
                    "release commits exactly one reply, history pair and idempotently displayed original receipt"
                )

                command = send("中途断流")
                page.evaluate("activeStream.controller.abort()")
                page.wait_for_function(
                    "!session.busy && !document.querySelector('#messages .streaming')"
                )
                assert (
                    page.evaluate("session.history.filter(x => x.role === 'assistant').length") == 1
                )
                assert page.is_disabled("#send")
                model.release.set()
                wait_receipt(command)
                page.click("#receipt")
                page.wait_for_function("!session.busy && session.history.length === 4")
                assert model.calls == 2 and page.locator("#messages .assistant").count() == 2
                checks.append(
                    "midstream connection loss clears temporary reply, keeps send locked and recovers original receipt without another model call"
                )

                # Subsequent rounds retain the two committed assistant entries.
                command = send("停止输出")
                page.click("#access-stop")
                page.wait_for_function(
                    "!session.active && !document.querySelector('#messages .streaming')"
                )
                model.release.set()
                wait_receipt(command)
                page.wait_for_function("!session.busy")
                assert page.locator("#messages .assistant").count() == 2
                assert (
                    page.evaluate("session.history.filter(x => x.role === 'assistant').length") == 2
                )
                checks.append(
                    "stop immediately removes temporary reply and late provider completion never enters formal history"
                )
                page.click("#receipt")
                page.wait_for_function("!session.busy")
                page.click("#activate")
                page.wait_for_function("session.active")

                command = send("切换伙伴")
                page.select_option("#companions", "stream-two")
                page.wait_for_function("!document.querySelector('#messages .streaming')")
                model.release.set()
                wait_receipt(command)
                page.wait_for_function("!session.busy")
                assert page.locator("#messages .assistant").count() == 0
                assert page.evaluate("session.history.length") == 0
                page.screenshot(path=str(args.artifacts / "switch-cleared.png"), full_page=True)
                checks.append(
                    "partner switch removes temporary reply and late old-provider output cannot contaminate the new partner"
                )
                assert not errors, errors
                browser.close()
            report["status"] = "passed"
        except BaseException:
            report["failure"] = traceback.format_exc()
            raise
        finally:
            model.release.set()
            server.should_exit = True
            thread.join(timeout=10)
            report.update(
                provider="paused deterministic fake; no real model or production service",
                model_calls=model.calls,
            )
            (args.artifacts / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
