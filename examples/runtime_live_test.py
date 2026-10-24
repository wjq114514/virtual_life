"""Exercise isolated real HTTP runtime/provider servers without an external LLM.

Run: uv run python examples/runtime_live_test.py
Only a temporary SQLite database is used; the JSON report contains no API token.
"""

import json
import secrets
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn
from virtual_life.api import create_app
from virtual_life.settings import Settings


class LocalModel:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.first_sent = threading.Event()
        self.release = threading.Event()

    def count(self):
        with self.lock:
            return len(self.calls)

    def handler(self):
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.path == "/v1/chat/completions"
                with model.lock:
                    model.calls.append({"stream": body.get("stream", False)})
                self.send_response(200)
                if not body.get("stream"):
                    raw = json.dumps(
                        {
                            "model": "local-deterministic",
                            "choices": [
                                {
                                    "message": {"role": "assistant", "content": "我在，陪你。"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"prompt_tokens": 80, "completion_tokens": 12},
                        },
                        ensure_ascii=False,
                    ).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                def frame(value):
                    self.wfile.write(
                        ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()
                    )
                    self.wfile.flush()

                frame(
                    {
                        "choices": [
                            {"index": 0, "delta": {"content": "首段中文"}, "finish_reason": None}
                        ]
                    }
                )
                model.first_sent.set()
                if not model.release.wait(20):
                    return
                frame(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "，继续陪你。"},
                                "finish_reason": None,
                            }
                        ]
                    }
                )
                frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                frame({"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 12}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler


@contextmanager
def servers(database, token):
    model = LocalModel()
    model_server = ThreadingHTTPServer(("127.0.0.1", 0), model.handler())
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    app = create_app(
        Settings(
            database_path=str(database),
            api_token=token,
            model_base_url=f"http://127.0.0.1:{model_server.server_port}/v1",
            model_name="local-deterministic",
            auto_memory_enabled=False,
            lease_ttl_seconds=300,
            node_ttl_seconds=300,
            proactivity_poll_seconds=0.1,
            model_timeout_seconds=25,
        )
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Uvicorn did not start"
            time.sleep(0.02)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}", model
    finally:
        model.release.set()
        server.should_exit = True
        thread.join(timeout=10)
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "Runtime did not shut down"


def request(client, method, path, payload=None, expected=200):
    response = client.request(method, "/v1" + path, json=payload)
    assert response.status_code == expected, (
        f"{method} {path}: {response.status_code} {response.text}"
    )
    return response.json()


def setup(client, companion, node):
    request(
        client,
        "POST",
        "/nodes",
        {
            "node_id": node,
            "label": node,
            "kind": "desktop",
            "capabilities": ["text.display", "attention.observe"],
        },
        201,
    )
    request(client, "POST", "/companions", {"companion_id": companion, "name": companion}, 201)
    transfer = request(
        client,
        "POST",
        f"/companions/{companion}/transfers",
        {
            "command_id": companion + "-attach",
            "target_node_id": node,
            "expected_revision": 0,
        },
        201,
    )
    assert (
        request(
            client,
            "POST",
            f"/transfers/{transfer['transfer_id']}/ack",
            {
                "node_id": node,
                "phase": "ready",
            },
        )["status"]
        == "committed"
    )
    state = request(client, "GET", f"/companions/{companion}")
    state = mode(client, companion, node, state, "chat")
    request(
        client, "POST", f"/companions/{companion}/lease", {"node_id": node, "epoch": state["epoch"]}
    )
    request(
        client,
        "POST",
        f"/companions/{companion}/personality",
        {
            "command_id": companion + "-initiative",
            "expected_revision": 0,
            "definition": {"traits": {"initiative": 0.8}},
        },
    )
    return state


def mode(client, companion, node, state, value):
    return request(
        client,
        "POST",
        f"/companions/{companion}/context",
        {
            "command_id": f"{companion}-mode-{state['revision']}",
            "node_id": node,
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "interaction_mode": value,
        },
    )


def observe(client, sequence, **changes):
    sample = {
        "session_id": "live-attention",
        "sequence": sequence,
        "sampled_at": datetime.now(UTC).isoformat(),
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
    }
    return request(
        client,
        "POST",
        "/nodes/desk/attention",
        {"command_id": f"attention-{sequence}", "sample": sample},
    )


def decisions(client):
    return request(client, "GET", "/companions/proactive/proactivity/decisions")["decisions"]


def run_checks(client, model):
    results = []
    proactive = setup(client, "proactive", "desk")
    request(
        client,
        "POST",
        "/nodes/access/web",
        {"browser_id": "live-browser", "access_id": "live-access", "bind_node_id": "desk"},
        201,
    )
    output_path = (
        "/companions/proactive/proactivity/output?node_id=desk&access_id=live-access"
        f"&epoch={proactive['epoch']}"
    )
    baseline = request(client, "GET", output_path)
    assert baseline["items"] == []
    output_cursor = baseline["next_sequence"]
    current = request(client, "GET", "/companions/proactive/proactivity")
    request(
        client,
        "POST",
        "/companions/proactive/proactivity",
        {
            "command_id": "enable-proactive",
            "expected_revision": current["revision"],
            "policy": {**current["policy"], "enabled": True},
        },
    )
    for sequence, changes, reason in (
        (1, {"focus": "focused"}, "focused"),
        (2, {"meeting": True}, "meeting_or_sharing"),
    ):
        observe(client, sequence, **changes)
        assert decisions(client)[-1]["reason"] == reason
        assert model.count() == 0
    proactive = mode(client, "proactive", "desk", proactive, "quiet")
    observe(client, 3)
    assert decisions(client)[-1]["reason"] == "waiting_for_user"
    assert model.count() == 0
    proactive = mode(client, "proactive", "desk", proactive, "chat")
    observe(client, 4)
    deadline = time.monotonic() + 10
    while decisions(client)[-1]["status"] != "completed":
        assert time.monotonic() < deadline, (
            f"Background worker did not complete: {decisions(client)}"
        )
        time.sleep(0.05)
    assert model.count() == 1
    events = request(client, "GET", "/events?limit=500")["events"]
    assert sum(event["type"] == "companion.proactive_completed" for event in events) == 1
    assert request(client, "GET", "/companions/proactive/emotion/experiences")["items"] == []
    observe(client, 5, boundary_id="boundary-4")
    assert len(decisions(client)) == 4
    assert model.count() == 1
    output = request(client, "GET", output_path + f"&after_sequence={output_cursor}")
    assert len(output["items"]) == 1
    assert output["items"][0]["assistant_message"] == "我在，陪你。"
    assert "user_message" not in output["items"][0]
    assert (
        request(client, "GET", output_path + f"&after_sequence={output['next_sequence']}")["items"]
        == []
    )
    observe(client, 6, focus="focused", boundary="none", boundary_id=None)
    focus = request(client, "GET", "/nodes/desk/focus")
    assert focus["state"] == "candidate" and focus["protection_active"]
    assert focus["source"] == "host_advisory" and focus["confirmed_at"] is None
    assert request(client, "GET", output_path + f"&after_sequence={output_cursor}")["items"] == []
    observe(client, 7, boundary_id="boundary-4")
    assert not request(client, "GET", "/nodes/desk/focus")["protection_active"]
    assert request(client, "GET", output_path + f"&after_sequence={output_cursor}")["items"] == []
    latest = request(
        client, "GET", "/companions/proactive/proactivity/decisions?latest=true&limit=2"
    )
    assert len(latest["decisions"]) == 2
    assert latest["next_sequence"] == latest["decisions"][-1]["sequence"]
    assert model.count() == 1
    results.append(
        "Real lifespan worker consumed attention boundary once; focused/meeting/quiet gates made zero model requests"
    )
    results.append(
        "Real HTTP focus protection and live-access output baseline/delivery passed; old remark never replayed after focus recovered"
    )

    state = setup(client, "emotion", "user-desk")
    body = {
        "command_id": "thanks",
        "node_id": "user-desk",
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "user_message": "谢谢你陪我读书",
    }
    turn = request(client, "POST", "/companions/emotion/dialogue", body)
    assert turn["status"] == "completed" and turn["emotion"]["reaction"] == "joy"
    item = request(client, "GET", "/companions/emotion/emotion/experiences")["items"][0]
    assert item["appraisal_version"] == "rules_v2" and item["delta"]["valence"] > 0
    correction = {
        "command_id": "clear-thanks",
        "expected_revision": turn["emotion"]["revision"],
        "kind": "direction",
    }
    path = f"/companions/emotion/emotion/experiences/{turn['accepted_event_id']}/correct"
    cleared = request(client, "POST", path, correction)
    assert cleared["state"] == cleared["baseline"]
    assert request(client, "POST", path, correction) == cleared
    stale = request(client, "POST", path, {**correction, "command_id": "stale"}, 409)
    assert stale["error"]["code"] == "revision_conflict"
    quiet = request(
        client,
        "POST",
        "/companions/emotion/dialogue",
        {
            **body,
            "command_id": "quiet",
            "user_message": "先安静陪我读一会儿",
        },
    )
    assert quiet["emotion"]["quiet_requested"]
    restored = request(
        client,
        "POST",
        f"/companions/emotion/emotion/experiences/{quiet['accepted_event_id']}/correct",
        {
            "command_id": "clear-quiet",
            "expected_revision": quiet["emotion"]["revision"],
            "kind": "direction",
        },
    )
    assert not restored["quiet_requested"]
    results.append(
        "Real HTTP dialogue, rules-v2 experience, direction correction, stale CAS, idempotency and quiet解除 passed"
    )

    stream_body = {**body, "command_id": "live-stream", "user_message": "继续陪我读书"}
    before_count = model.count()
    final = None
    draft = None
    try:
        with client.stream(
            "POST", "/v1/companions/emotion/dialogue/stream", json=stream_body
        ) as response:
            assert response.status_code == 200
            kind = None
            for line in response.iter_lines():
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    assert kind != "error", data
                    if kind == "delta" and draft is None:
                        draft = data["text"]
                        assert draft == "首段中文" and model.first_sent.is_set()
                        assert not model.release.is_set()
                        pending = request(
                            client, "GET", "/companions/emotion/dialogue/live-stream", expected=409
                        )
                        assert pending["error"]["code"] == "dialogue_in_progress"
                        model.release.set()
                    if kind == "result":
                        final = data
    finally:
        model.release.set()
    assert draft == "首段中文" and final["status"] == "completed"
    assert final["assistant_message"] == "首段中文，继续陪你。"
    assert request(client, "GET", "/companions/emotion/dialogue/live-stream") == final
    assert request(client, "POST", "/companions/emotion/dialogue", stream_body) == final
    assert model.count() == before_count + 1
    events = request(client, "GET", "/events?limit=500")["events"]
    assert sum(event["event_id"] == final["source_event_id"] for event in events) == 1
    results.append(
        "Chinese SSE first draft arrived over both TCP hops while model tail paused; receipt pending then single completion and idempotent replay"
    )
    return results


def main():
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "model": "deterministic localhost HTTP model, not an actual LLM",
        "transport": "real loopback Uvicorn + httpx + OpenAI-compatible HTTP JSON/SSE",
        "production_resources_touched": False,
    }
    destination = Path(".data/runtime-live-review/runtime-live-test.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="virtual-life-live-") as temporary:
            token = secrets.token_urlsafe(32)
            with servers(Path(temporary) / "runtime.sqlite", token) as (url, model):
                with httpx.Client(
                    base_url=url,
                    headers={"Authorization": "Bearer " + token},
                    timeout=30,
                    trust_env=False,
                ) as client:
                    report["checks"] = run_checks(client, model)
                report["model_requests"] = model.count()
        report["status"] = "passed"
        report["cleanup"] = "Both servers stopped; temporary SQLite directory removed"
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
