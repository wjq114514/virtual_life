"""Exercise desktop -> Android -> desktop travel against a running HTTP runtime.

Both nodes are simulated clients; this script does not create an Android overlay.
Run: uv run python examples/two_nodes.py
"""

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


class RuntimeClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None):
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=raw,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc

    def travel(self, companion_id: str, target: str) -> dict:
        self.request("POST", f"/v1/nodes/{target}/heartbeat", {"context": {"app_foreground": True}})
        state = self.request("GET", f"/v1/companions/{companion_id}")
        transfer = self.request(
            "POST",
            f"/v1/companions/{companion_id}/transfers",
            {
                "command_id": str(uuid4()),
                "target_node_id": target,
                "expected_revision": state["revision"],
                "reason": "demo_user_attention",
            },
        )
        transfer_id = transfer["transfer_id"]
        transfer = self.request(
            "POST", f"/v1/transfers/{transfer_id}/ack", {"node_id": target, "phase": "ready"}
        )
        source = state["active_node_id"]
        if source is not None and transfer["status"] == "prepared":
            # In a real host: stop avatar/audio output before acknowledging released.
            transfer = self.request(
                "POST",
                f"/v1/transfers/{transfer_id}/ack",
                {"node_id": source, "phase": "released"},
            )
        assert transfer["status"] == "committed", transfer
        result = self.request("GET", f"/v1/companions/{companion_id}")
        assert result["active_node_id"] == target
        print(f"来到 {target}：revision={result['revision']} epoch={result['epoch']}")
        return result

    def find_event_id(self, companion_id: str, event_type: str) -> str:
        cursor = 0
        while True:
            page = self.request("GET", f"/v1/events?after_sequence={cursor}&limit=500")
            for event in page["events"]:
                if event["type"] == event_type and event["companion_id"] == companion_id:
                    return event["event_id"]
            next_cursor = page["next_sequence"]
            if next_cursor <= cursor:
                raise RuntimeError(f"No {event_type} event found for {companion_id}")
            cursor = next_cursor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--token", default=os.environ.get("VIRTUAL_LIFE_API_TOKEN", "local-dev-token")
    )
    args = parser.parse_args()
    client = RuntimeClient(args.base_url, args.token)
    run_id = uuid4().hex[:8]
    desktop, android, companion_id = f"desktop-{run_id}", f"android-{run_id}", f"sui-{run_id}"
    for node_id, kind, label, capabilities in (
        (desktop, "desktop", "书桌电脑", ["avatar.render", "text.display", "reading.context"]),
        (android, "android", "随身安卓", ["avatar.render", "text.display", "audio.output"]),
    ):
        client.request(
            "POST",
            "/v1/nodes",
            {"node_id": node_id, "kind": kind, "label": label, "capabilities": capabilities},
        )
    client.request("POST", "/v1/companions", {"companion_id": companion_id, "name": "随"})
    state = client.travel(companion_id, desktop)
    activity = {"type": "co_reading", "book_id": "夜航船", "paragraph": 12}
    state = client.request(
        "POST",
        f"/v1/companions/{companion_id}/context",
        {
            "command_id": str(uuid4()),
            "node_id": desktop,
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "activity": activity,
            "interaction_mode": "quiet",
            "context": {"topic": "今天一起读的这一段"},
        },
    )
    source_event = client.find_event_id(companion_id, "companion.context_updated")
    episode = client.request(
        "POST",
        f"/v1/companions/{companion_id}/memories",
        {
            "command_id": str(uuid4()),
            "kind": "episode",
            "content": "我们一起读《夜航船》，你在第十二段停了一会儿，说想明天继续读。",
            "source_event_id": source_event,
            "source_node_id": desktop,
            "importance": 0.9,
            "confidence": 1.0,
            "confirmed": True,
            "tags": ["共读", "夜航船"],
        },
    )
    state = client.request(
        "POST",
        f"/v1/companions/{companion_id}/context",
        {
            "command_id": str(uuid4()),
            "node_id": desktop,
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "memory_refs": [episode["memory_id"]],
        },
    )
    state = client.travel(companion_id, android)
    assert state["activity"] == activity
    assert state["memory_refs"] == [episode["memory_id"]]
    recall = client.request(
        "POST",
        f"/v1/companions/{companion_id}/memories/recall",
        {"query": "夜航船", "token_budget": 800, "max_items": 4},
    )
    assert any(item["memory_id"] == episode["memory_id"] for item in recall["items"]), recall
    assert recall["estimated_tokens"] <= recall["token_budget"]
    assert recall["model_calls"] == 0
    print(f"安卓节点记得共读经历，召回估算 token={recall['estimated_tokens']}，模型调用=0")
    state = client.travel(companion_id, desktop)
    assert state["activity"] == activity and state["context"]["topic"] == "今天一起读的这一段"
    print(
        json.dumps(
            {
                "companion_id": companion_id,
                "active_node_id": state["active_node_id"],
                "activity": state["activity"],
                "memory_refs": state["memory_refs"],
                "message": "完成电脑 → 安卓 → 电脑接力，共读上下文与共同记忆保留。",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
