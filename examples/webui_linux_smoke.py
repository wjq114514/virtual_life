"""Check Linux deployment UI in Chromium with mocked APIs and no SSH or database.

Run: uv run --with playwright python examples/webui_linux_smoke.py
Install Chromium first with: uv run --with playwright playwright install chromium
"""

import argparse
import json
import os
import shutil
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/webui-linux-review"))
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    state = {"mode": "inspect", "posts": 0, "gets": 0, "id": None, "revoked": False}
    static = Path(__file__).resolve().parents[1] / "backend/virtual_life/webui"
    with tempfile.TemporaryDirectory(prefix="virtual-life-linux-ui-") as temporary:
        # Serve only an isolated copy of the static UI; no application is imported or started.
        served = Path(temporary) / "webui"
        shutil.copytree(static, served)
        handler = partial(QuietStaticHandler, directory=str(served))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, executable_path=args.browser_executable
                )
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000}, accept_downloads=True
                )
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))

                def mock_request(route):
                    request = route.request
                    parsed = urlparse(request.url)
                    if not request.url.startswith(base + "/"):
                        errors.append("Unexpected external request")
                        route.abort()
                        return
                    path = parsed.path
                    if not path.startswith("/v1/"):
                        route.continue_()
                        return
                    assert request.headers.get("authorization") == "Bearer mock-admin-only"
                    assert not parsed.query, "Credentials must not appear in URLs"
                    assert path.startswith("/v1/deployments/linux"), "Unexpected real API request"
                    if path.endswith("/host-key"):
                        assert set(request.post_data_json) == {"host", "port"}
                        route.fulfill(
                            json={
                                **request.post_data_json,
                                "key_type": "ed25519",
                                "fingerprint": "SHA256:verified-test",
                            }
                        )
                    elif path.endswith("/inspect"):
                        route.fulfill(
                            json={
                                "ready": False,
                                "python_version": "3.11",
                                "systemd": True,
                                "privileged": True,
                                "reason_codes": ["python_312_required"],
                            }
                        )
                    elif path.endswith("/bundle"):
                        route.fulfill(content_type="application/gzip", body=b"mock-bundle")
                    elif request.method == "POST":
                        state["posts"] += 1
                        state["id"] = request.post_data_json["command_id"]
                        assert "runtime_token" not in request.post_data_json
                        if state["mode"] in {"reject422", "reject429"}:
                            status = 422 if state["mode"] == "reject422" else 429
                            route.fulfill(
                                status=status,
                                json={"error": {"code": "deployment_busy", "message": "Rejected"}},
                            )
                        else:
                            route.abort("failed")
                    else:
                        assert state["id"] in path
                        state["gets"] += 1
                        if state["mode"] == "ambiguous":
                            route.abort("failed")
                            return
                        if request.method == "DELETE":
                            assert path.endswith("/credential")
                            state["revoked"] = True
                        route.fulfill(
                            json={
                                "command_id": state["id"],
                                "status": "completed",
                                "stage": "completed",
                                "host": "test-host",
                                "port": 22,
                                "node_id": "linux-test",
                                "credential_revoked": state["revoked"],
                            }
                        )

                context.route("**/*", mock_request)
                page.goto(base)
                # Isolate this dialog from connection/companion setup, which has its own smoke test.
                page.evaluate("session.token = 'mock-admin-only'")

                def open_dialog():
                    page.click("#connection-open")
                    page.click("#linux-open-connection")

                def trust_host():
                    page.click("#linux-probe")
                    page.check("#linux-trust")

                def wait_idle():
                    page.wait_for_function("!linuxDeployment.busy")

                open_dialog()
                page.fill("#linux-host", "test-host")
                trust_host()
                page.fill("#linux-password", "not-a-real-password")
                page.click("#linux-inspect")
                page.wait_for_function(
                    "document.querySelector('#linux-environment').textContent"
                    ".includes('需要 Python 3.12+')"
                )
                assert page.input_value("#linux-password") == ""
                page.fill("#linux-host", "different-host")
                assert not page.is_checked("#linux-trust")
                assert page.is_disabled("#linux-deploy")
                assert page.evaluate("linuxDeployment.fingerprint") is None
                trust_host()
                checks.append(
                    "inspect clears password; changing host invalidates trusted fingerprint"
                )

                for mode in ("reject422", "reject429"):
                    state["mode"] = mode
                    page.fill("#linux-password", "not-a-real-password")
                    page.click("#linux-deploy")
                    wait_idle()
                    assert page.evaluate("linuxDeployment.commandId") is None
                    assert not page.is_disabled("#linux-deploy")
                    assert page.input_value("#linux-password") == ""
                checks.append(
                    "explicit HTTP 422/429 rejection clears command ID and permits correction"
                )

                state["mode"] = "ambiguous"
                page.select_option("#linux-auth", "key")
                page.fill("#linux-key", "not-a-real-key")
                page.fill("#linux-passphrase", "not-a-real-passphrase")
                page.click("#linux-deploy")
                wait_idle()
                assert page.input_value("#linux-key") == ""
                assert page.input_value("#linux-passphrase") == ""
                original_id = state["id"]
                assert page.evaluate("linuxDeployment.commandId") == original_id
                assert page.is_disabled("#linux-deploy")
                page.click("#linux-close")
                page.evaluate("session.companion = 'different-companion'")
                state["mode"] = "recover"
                open_dialog()
                page.wait_for_function("linuxDeployment.job?.status === 'completed'")
                assert page.evaluate("linuxDeployment.commandId") == original_id
                assert state["posts"] == 3
                assert state["gets"] >= 2
                checks.append(
                    "unknown POST outcome retains ID; close/companion switch/reopen recovers via GET"
                )

                with page.expect_download() as download:
                    page.click("#linux-bundle")
                downloaded = Path(temporary) / "mock-bundle.tar.gz"
                download.value.save_as(downloaded)
                assert downloaded.read_bytes() == b"mock-bundle"
                wait_idle()
                page.click("#linux-revoke")
                page.wait_for_function("linuxDeployment.job?.credential_revoked === true")
                assert state["revoked"]
                assert page.is_disabled("#linux-revoke")
                checks.append(
                    "authenticated bundle download and terminal-job credential revocation"
                )

                for name, viewport in (
                    ("desktop", {"width": 1440, "height": 1000}),
                    ("mobile", {"width": 390, "height": 844}),
                ):
                    page.set_viewport_size(viewport)
                    assert not page.evaluate("""() => {
                        const dialog = document.querySelector('#linux-dialog');
                        return document.documentElement.scrollWidth > innerWidth ||
                            dialog.scrollWidth > dialog.clientWidth;
                    }""")
                    page.screenshot(path=str(args.artifacts / f"{name}.png"), full_page=True)
                checks.append("desktop and mobile dialog have no horizontal overflow")

                page.select_option("#linux-auth", "password")
                page.fill("#linux-password", "discard-on-close")
                page.click("#linux-close")
                page.wait_for_function("document.querySelector('#linux-password').value === ''")
                assert not errors, errors
                checks.append("close clears remaining secrets; no browser JavaScript errors")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    report = {"checks": checks, "errors": errors}
    (args.artifacts / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
