"""Check optional Linux desktop collection UI without real API, SSH or database.

Run: uv run --with playwright python examples/webui_attention_smoke.py
"""

import argparse
import json
import os
import shutil
import tempfile
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from webui_linux_smoke import QuietStaticHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/webui-attention-review"))
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, errors, deployments, reads = [], [], [], []
    state = {"offline": False, "unknown": False}
    with tempfile.TemporaryDirectory(prefix="virtual-life-attention-ui-") as temporary:
        served = Path(temporary) / "webui"
        shutil.copytree(Path(__file__).resolve().parents[1] / "backend/virtual_life/webui", served)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietStaticHandler, directory=str(served))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, executable_path=args.browser_executable
                )
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))

                def route_api(route):
                    request = route.request
                    assert request.url.startswith(base + "/"), "Unexpected external request"
                    path = urlparse(request.url).path
                    if not path.startswith("/v1/"):
                        route.continue_()
                        return
                    if path.endswith("/host-key"):
                        route.fulfill(json={"key_type": "ed25519", "fingerprint": "SHA256:test"})
                    elif path.endswith("/inspect"):
                        assert "attention_enabled" not in request.post_data_json
                        route.fulfill(
                            json={
                                "ready": True,
                                "python_version": "3.12",
                                "systemd": True,
                                "privileged": True,
                                "reason_codes": [],
                            }
                        )
                    elif request.method == "POST":
                        assert path == "/v1/deployments/linux", (
                            "No node registration or attention writes allowed"
                        )
                        deployments.append(request.post_data_json)
                        if state["offline"]:
                            route.abort("failed")
                        else:
                            route.fulfill(status=202, json=job())
                    elif path.startswith("/v1/deployments/linux/"):
                        if state["offline"]:
                            route.abort("failed")
                        else:
                            route.fulfill(json=job())
                    elif path.startswith("/v1/companions/"):
                        assert request.method == "GET"
                        if path.endswith("/proactivity"):
                            route.fulfill(
                                json={
                                    "revision": 0,
                                    "policy": {
                                        "enabled": False,
                                        "daily_limit": 3,
                                        "cooldown_seconds": 600,
                                    },
                                }
                            )
                        elif path.endswith("/decisions"):
                            route.fulfill(json={"decisions": []})
                        else:
                            route.fulfill(json={"active_node_id": "linux-test"})
                    elif path.startswith("/v1/nodes/linux-test"):
                        assert request.method == "GET"
                        if path.endswith(("/attention", "/focus")):
                            route.fulfill(
                                json={"sample": None, "state": "unknown", "recent_segments": []}
                            )
                        else:
                            route.fulfill(
                                json={
                                    "context": {
                                        "attention_collection": {
                                            "status": "unknown",
                                            "source": "logind",
                                            "reason": "no_session",
                                            "session_type": None,
                                            "sampled_at": "raw-secret-do-not-render",
                                        }
                                    }
                                }
                            )
                    else:
                        reads.append(path)
                        assert request.method == "GET"
                        if path == "/v1/nodes":
                            route.fulfill(
                                json=[
                                    {
                                        "node_id": "linux-test",
                                        "label": "Linux",
                                        "kind": "other",
                                        "capabilities": ["device.state.report"],
                                        "context": {
                                            "attention_collection": {
                                                "status": "unknown" if state["unknown"] else "ok",
                                                "source": "logind+x11",
                                                "session_type": "x11",
                                                "sampled_at": "2026-10-15T12:00:00Z",
                                                "reason": "raw-secret-do-not-render"
                                                if state["unknown"]
                                                else None,
                                            }
                                        },
                                    }
                                ]
                            )
                        else:
                            assert path == "/v1/graph"
                            route.fulfill(json={"nodes": [], "edges": []})

                def job():
                    return {
                        "command_id": deployments[-1]["command_id"],
                        "status": "completed",
                        "stage": "completed",
                        "node_id": "linux-test",
                        "host": "test-host",
                        "port": 22,
                    }

                page.route("**/*", route_api)
                page.goto(base)
                page.evaluate("session.token = 'mock-admin'")
                page.click("#connection-open")
                page.click("#linux-open-connection")
                assert not page.is_checked("#linux-attention")
                page.fill("#linux-host", "test-host")
                page.click("#linux-probe")
                page.check("#linux-trust")
                page.fill("#linux-password", "test-only-password")
                page.click("#linux-inspect")
                page.wait_for_function("!linuxDeployment.busy")
                page.fill("#linux-password", "test-only-password")
                page.click("#linux-deploy")
                page.wait_for_function("linuxDeployment.job?.status === 'completed'")
                assert deployments[0]["attention_enabled"] is False
                page.click("#linux-new")
                page.check("#linux-attention")
                page.fill("#linux-password", "test-only-password")
                state["offline"] = True
                page.click("#linux-deploy")
                page.wait_for_function("!linuxDeployment.busy")
                identifier = deployments[-1]["command_id"]
                assert deployments[-1]["attention_enabled"] is True
                assert page.is_disabled("#linux-attention")
                assert page.input_value("#linux-password") == ""
                page.click("#linux-close")
                page.evaluate("session.companion = 'other-companion'")
                state["offline"] = False
                page.click("#connection-open")
                page.click("#linux-open-connection")
                page.wait_for_function("linuxDeployment.job?.status === 'completed'")
                assert page.evaluate("linuxDeployment.commandId") == identifier
                assert len(deployments) == 2 and deployments[-1]["attention_enabled"] is True
                checks.append(
                    "default off and explicit bool; inspect excludes option; unknown outcome recovers same ID without changed POST"
                )
                for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    assert not page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth || document.querySelector('#linux-dialog').scrollWidth > document.querySelector('#linux-dialog').clientWidth"
                    )
                    page.screenshot(path=str(args.artifacts / f"{name}.png"), full_page=True)
                page.click("#linux-close")
                page.evaluate("loadDevices()")
                assert "系统会话服务与 X11" in page.text_content("#devices-list")
                state["unknown"] = True
                page.evaluate("loadDevices()")
                diagnostic = page.text_content("#devices-list")
                assert "未知" in diagnostic and "raw-secret" not in diagnostic
                assert reads == ["/v1/nodes", "/v1/graph"] * 2
                page.evaluate("refreshBackground()")
                background = page.text_content("#background-attention")
                assert "未找到本地桌面会话" in background
                assert "系统会话服务" in background and "raw-secret" not in background
                assert not page.is_checked("#background-enabled")
                assert not errors, errors
                checks.append(
                    "read-only node diagnostics localize source and unknown reason; no raw output, attention write or proactive enable"
                )
                checks.append("desktop/mobile layout, secret clearing and no JavaScript errors")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    report = {"checks": checks, "errors": errors}
    (args.artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
