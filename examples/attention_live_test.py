"""Exercise Linux collection over real X11/HTTP; optionally call the configured real LLM.

The logind desktop is an explicit fixture, not a physical desktop lock test.
--real-model loads configured model credentials and makes two billable dialogue calls.
All application writes use a temporary database, never the configured runtime database.
"""

import argparse
import ctypes as c
import importlib
import json
import os
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from virtual_life.api import create_app
from virtual_life.model_provider import ProviderError
from virtual_life.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients/system-node"))
node = importlib.import_module("system_node")
attention = importlib.import_module("system_node.attention")
config = importlib.import_module("system_node.config")
runtime_module = importlib.import_module("system_node.runtime")
transport = importlib.import_module("system_node.transport")


class AuditedProvider:
    def __init__(self, provider, records):
        self.provider, self.records = provider, records

    def call(self, method, request, *args):
        record = {"stream": method == "stream", "purpose": request.purpose}
        self.records.append(record)
        try:
            result = getattr(self.provider, method)(request, *args)
        except ProviderError as exc:
            record.update(status="failed", code=exc.code)
            raise
        record.update(
            status="completed",
            usage={
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
        )
        return result

    def complete(self, request):
        return self.call("complete", request)

    def stream(self, request, on_delta):
        return self.call("stream", request, on_delta)


@contextmanager
def x11():
    """Only a new Xvfb with synthetic content; never connect to the user's display."""
    reader, writer = os.pipe()
    process = subprocess.Popen(
        ["Xvfb", "-displayfd", str(writer), "-screen", "0", "800x600x24", "-nolisten", "tcp"],
        pass_fds=(writer,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(writer)
    display = None
    try:
        assert select.select([reader], [], [], 10)[0], "Xvfb did not start"
        name = ":" + os.read(reader, 32).decode().strip()
        lib = c.CDLL("libX11.so.6")
        lib.XOpenDisplay.argtypes = [c.c_char_p]
        lib.XOpenDisplay.restype = c.c_void_p
        display = lib.XOpenDisplay(name.encode())
        assert display, "X11 connection failed"
        lib.XDefaultRootWindow.argtypes = [c.c_void_p]
        lib.XDefaultRootWindow.restype = c.c_ulong
        root = lib.XDefaultRootWindow(display)
        lib.XCreateSimpleWindow.argtypes = [
            c.c_void_p,
            c.c_ulong,
            c.c_int,
            c.c_int,
            c.c_uint,
            c.c_uint,
            c.c_uint,
            c.c_ulong,
            c.c_ulong,
        ]
        lib.XCreateSimpleWindow.restype = c.c_ulong
        window = lib.XCreateSimpleWindow(display, root, 0, 0, 100, 100, 0, 0, 0)
        lib.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
        lib.XInternAtom.restype = c.c_ulong
        lib.XChangeProperty.argtypes = [
            c.c_void_p,
            c.c_ulong,
            c.c_ulong,
            c.c_ulong,
            c.c_int,
            c.c_int,
            c.POINTER(c.c_ubyte),
            c.c_int,
        ]
        active = c.c_ulong(window)
        lib.XChangeProperty(
            display,
            root,
            lib.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0),
            33,
            32,
            0,
            c.cast(c.pointer(active), c.POINTER(c.c_ubyte)),
            1,
        )
        lib.XStoreName.argtypes = [c.c_void_p, c.c_ulong, c.c_char_p]
        lib.XStoreName(display, window, b"PRIVATE_TEST_TITLE_MUST_NOT_UPLOAD")

        class ClassHint(c.Structure):
            _fields_ = [("res_name", c.c_char_p), ("res_class", c.c_char_p)]

        lib.XSetClassHint.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(ClassHint)]
        lib.XSync.argtypes = [c.c_void_p, c.c_int]

        def application(value):
            hint = ClassHint(value.encode(), value.encode())
            lib.XSetClassHint(display, window, c.byref(hint))
            lib.XSync(display, 0)

        application("Code")
        yield name, application
    finally:
        os.close(reader)
        if display:
            lib.XCloseDisplay.argtypes = [c.c_void_p]
            lib.XCloseDisplay(display)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def server(settings):
    app = create_app(settings)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error", proxy_headers=False))
    thread = threading.Thread(target=runner.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not runner.started:
            assert thread.is_alive() and time.monotonic() < deadline, "API failed to start"
            time.sleep(0.02)
        yield app, f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        runner.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "API failed to stop"


def checked(api, method, path, body=None, expected=200):
    response = api.request(method, "/v1" + path, json=body)
    assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}"
    return response.json()


def collection(app, base, api, directory, report):
    report["host_probe"] = attention.AttentionCollector().collect().diagnostics
    identity = "collection-test"
    granted = app.state.node_credentials.issue(
        identity, "collection-fixture", attention_enabled=True
    )
    path = directory / "node.json"
    path.write_text(
        json.dumps(
            {
                "server_url": base,
                "node_id": identity,
                "label": "Collection test",
                "attention": {"enabled": True},
            }
        )
    )
    worker = runtime_module.Runtime(
        config.load_config(path), transport.ApiClient(base, granted["token"], 5), threading.Event()
    )
    with x11() as (display, application):
        fixture = directory / "session.json"
        props = {
            "Type": "x11",
            "Class": "user",
            "Remote": "no",
            "Active": "yes",
            "State": "active",
            "LockedHint": "no",
            "IdleHint": "no",
            "User": str(os.getuid()),
            "Display": display,
        }
        programs = directory / "bin"
        programs.mkdir()
        loginctl = programs / "loginctl"
        loginctl.write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            "p=json.load(open(os.environ['VL_ATTENTION_FIXTURE']))\n"
            "print('test-session 1 fixture seat0' if sys.argv[1]=='list-sessions' "
            "else '\\n'.join(k+'='+v for k,v in p.items()))\n"
        )
        loginctl.chmod(0o700)
        snapshots = []
        with patch.dict(
            os.environ,
            {
                "DISPLAY": display,
                "VL_ATTENTION_FIXTURE": str(fixture),
                "PATH": str(programs) + os.pathsep + os.environ.get("PATH", ""),
            },
        ):

            def cycle(**changes):
                props.update(changes)
                fixture.write_text(json.dumps(props))
                worker._cycle()
                data = checked(api, "GET", f"/nodes/{identity}/attention")
                snapshots.append(data["sample"])
                return data["sample"]

            sample = cycle()
            assert sample["activity"] == "code" and sample["user_present"] is True
            checked(api, "POST", "/companions", {"companion_id": "live", "name": "随"}, 201)
            checked(
                api,
                "POST",
                "/nodes/access/web",
                {
                    "browser_id": "fixture-browser",
                    "access_id": "fixture-access",
                    "bind_node_id": identity,
                },
                201,
            )
            transfer = checked(
                api,
                "POST",
                "/companions/live/transfers",
                {
                    "command_id": "attach",
                    "target_node_id": identity,
                    "expected_revision": 0,
                },
                201,
            )
            checked(
                api,
                "POST",
                f"/transfers/{transfer['transfer_id']}/ack",
                {
                    "node_id": identity,
                    "phase": "ready",
                },
            )
            state = checked(api, "GET", "/companions/live")
            checked(
                api,
                "POST",
                "/companions/live/context",
                {
                    "command_id": "chat",
                    "node_id": identity,
                    "epoch": state["epoch"],
                    "expected_revision": state["revision"],
                    "interaction_mode": "chat",
                },
            )
            policy = checked(api, "GET", "/companions/live/proactivity")
            checked(
                api,
                "POST",
                "/companions/live/proactivity",
                {
                    "command_id": "opt-in",
                    "expected_revision": policy["revision"],
                    "policy": {**policy["policy"], "enabled": True},
                },
            )
            sample = cycle(IdleHint="yes")
            assert sample["user_present"] is None and sample["boundary"] == "none"
            assert cycle(LockedHint="yes")["locked"] is True
            sample = cycle(LockedHint="no", IdleHint="no")
            assert sample["boundary"] == "user_returned"
            rows = checked(api, "GET", "/companions/live/proactivity/decisions")["decisions"]
            assert rows[-1]["reason"] == "attention_unknown" and rows[-1]["status"] == "suppressed"
            application("XTerm")
            assert cycle()["activity"] == "terminal"
            node_state = checked(api, "GET", f"/nodes/{identity}")
            assert node_state["context"]["attention_collection"]["source"] == "logind+x11"
            assert len(checked(api, "GET", "/nodes")) == 1
            assert len({sample["session_id"] for sample in snapshots}) == 1
            assert [sample["sequence"] for sample in snapshots] == [1, 2, 3, 4, 5]
            assert all(
                sample["meeting"] is None and sample["focus"] == "unknown" for sample in snapshots
            )
            events = checked(api, "GET", "/events?limit=500")["events"]
            assert "PRIVATE_TEST_TITLE_MUST_NOT_UPLOAD" not in json.dumps(events)
            checked(api, "DELETE", f"/nodes/{identity}/access/fixture-access")
    report["collection"] = {
        "status": "passed",
        "transport": "real Xvfb + xprop + Runtime + Uvicorn/SQLite/HTTP",
        "logind": "explicit command fixture, not physical desktop validation",
        "samples": snapshots,
        "checks": [
            "Code/XTerm classified without uploading title; one physical node",
            "idle reading stays unknown, lock/unlock yields one returned boundary",
            "unknown meeting/focus suppresses proactivity even when enabled",
            "fresh sequences and bounded diagnostics persisted through scoped HTTP credential",
        ],
    }


def real_dialogue(base, api, browser_executable, directory, report):
    from playwright.sync_api import sync_playwright

    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_executable)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/ui/")
        page.click("#connection-open")
        page.fill("#token", api.headers["authorization"].removeprefix("Bearer "))
        page.click("#connect")
        page.wait_for_function("!document.querySelector('#companions').disabled")
        page.select_option("#companions", "live")
        page.locator("#local-device").evaluate(
            "el => {for(let p=el.parentElement;p;p=p.parentElement) if(p.tagName==='DETAILS')p.open=true}"
        )
        page.select_option("#local-device", "collection-test")
        page.click("#activate")
        page.wait_for_function("session.active && !document.querySelector('#send').disabled")
        page.evaluate("document.querySelector('#chat-output-budget').value = '128'")
        page.fill("#message", "你好，随。我在测试真实模型连接，请用一句简短中文打个招呼。")
        page.evaluate("""() => {
            window.realFirstChunkAt = null;
            new MutationObserver(() => {
                if (window.realFirstChunkAt === null &&
                    (document.querySelector('#messages .assistant.streaming.has-text') ||
                     document.querySelector('#messages .assistant:not(.streaming)')))
                    window.realFirstChunkAt = performance.now();
            }).observe(document.querySelector('#messages'), {childList:true, subtree:true, characterData:true, attributes:true});
            window.realSendAt = performance.now();
        }""")
        started = time.monotonic()
        page.click("#send")
        page.wait_for_function(
            "window.realFirstChunkAt !== null || (!session.busy && lastCommand())", timeout=90000
        )
        assert page.evaluate("window.realFirstChunkAt !== null"), "Real stream did not produce text"
        first_visible = page.evaluate("(realFirstChunkAt-realSendAt)/1000")
        page.wait_for_function(
            "!session.busy && conversation().history.length === 2", timeout=90000
        )
        total = time.monotonic() - started
        history = page.evaluate("conversation().history")
        last = page.evaluate("lastCommand()")
        command_id = last["body"]["command_id"]
        receipt = checked(api, "GET", "/companions/live/dialogue/" + command_id)
        assert receipt["status"] == "completed" and receipt["assistant_message"]
        assert not errors, errors
        page.screenshot(path=str(directory / "real-dialogue.png"), full_page=True)
        # A second, nonstreaming request verifies the same provider's JSON path.
        state = checked(api, "GET", "/companions/live")
        body = {
            "command_id": "real-json",
            "node_id": "collection-test",
            "access_id": page.evaluate("session.access"),
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "max_output_tokens": 128,
            "user_message": "再用一句话告诉我：缺少可靠的会议和专注信号时，应当保持什么态度？",
            "history": history,
        }
        response = checked(api, "POST", "/companions/live/dialogue", body)
        assert response["status"] == "completed" and response["assistant_message"]
        assert checked(api, "GET", "/companions/live/dialogue/real-json") == response
        page.click("#access-stop")
        report["real_llm"] = {
            "status": "passed",
            "calls": 2,
            "first_visible_seconds": round(first_visible, 3),
            "stream_total_seconds": round(total, 3),
            "stream_reply": receipt["assistant_message"],
            "stream_usage": receipt["usage"],
            "json_reply": response["assistant_message"],
            "json_usage": response["usage"],
            "page_errors": errors,
            "checks": [
                "real Chromium streamed reply committed once",
                "real JSON reply and receipt replay",
            ],
        }
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path(".data/attention-review/live"))
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--browser-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    assert shutil.which("Xvfb") and shutil.which("xprop"), "Xvfb and xprop required"
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {
        "production_database_used": False,
        "real_model_requested": args.real_model,
        "provider_calls": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="vl-attention-live-") as temporary:
            directory = Path(temporary)
            settings = Settings.load() if args.real_model else Settings()
            if args.real_model:
                assert settings.model_name and settings.model_base_url, (
                    "Configure a real model first"
                )
                report["model"] = settings.model_name
            else:
                settings = replace(settings, model_name="", model_base_url="", model_api_key="")
            settings = replace(
                settings,
                database_path=str(directory / "test.sqlite"),
                api_token=secrets.token_urlsafe(32),
                auto_memory_enabled=False,
                embedding_enabled=False,
                embedding_dialogue_recall=False,
                node_ttl_seconds=300,
                lease_ttl_seconds=300,
            )
            with server(settings) as (app, base):
                if args.real_model:
                    app.state.dialogue.provider = AuditedProvider(
                        app.state.dialogue.provider, report["provider_calls"]
                    )
                with httpx.Client(
                    base_url=base,
                    headers={"Authorization": "Bearer " + settings.api_token},
                    timeout=120,
                ) as api:
                    collection(app, base, api, directory, report)
                    assert not report["provider_calls"], "Unknown attention must never call a model"
                    if args.real_model:
                        real_dialogue(base, api, args.browser_executable, args.artifacts, report)
                        assert len(report["provider_calls"]) == 2
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        report["cleanup"] = "temporary database, API and Xvfb stopped"
        (args.artifacts / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
