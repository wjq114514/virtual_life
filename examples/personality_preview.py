"""Create isolated demo data and preview personality without calling a model."""

import argparse
import json
import os
import uuid
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--card", type=Path, default=Path(__file__).parent / "character_cards" / "sui.v2.json"
    )
    parser.add_argument("--show-prompt", action="store_true", help="Print each complete prompt")
    args = parser.parse_args()
    card = json.loads(args.card.read_text(encoding="utf-8"))
    suffix = uuid.uuid4().hex[:12]
    companion_id = f"sui-personality-{suffix}"
    node_id = f"personality-desktop-{suffix}"
    token = os.environ.get("VIRTUAL_LIFE_API_TOKEN", "local-dev-token")

    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
        follow_redirects=False,
    ) as client:

        def request(method: str, path: str, body: dict | None = None) -> dict:
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        def command_id() -> str:
            return str(uuid.uuid4())

        request(
            "POST",
            "/v1/nodes",
            {
                "node_id": node_id,
                "label": "人格预览的模拟电脑",
                "kind": "desktop",
                "capabilities": ["text.display", "avatar.render", "reading.context"],
            },
        )
        companion = request("POST", "/v1/companions", {"companion_id": companion_id, "name": "随"})
        base = f"/v1/companions/{companion_id}"
        profile = request(
            "POST",
            f"{base}/personality/import",
            {"command_id": command_id(), "expected_revision": 0, "card": card},
        )
        # Make participation in chat explicit configuration; quiet/focus still wait.
        definition = profile["definition"]
        definition["traits"]["initiative"] = 0.75
        profile = request(
            "POST",
            f"{base}/personality",
            {
                "command_id": command_id(),
                "expected_revision": profile["revision"],
                "definition": definition,
            },
        )
        state = request(
            "POST",
            f"{base}/personality/state",
            {
                "command_id": command_id(),
                "expected_revision": 0,
                "state": {"mood": "curious", "energy": 0.8, "social_battery": 0.7},
                "ttl_seconds": 1800,
            },
        )
        transfer = request(
            "POST",
            f"{base}/transfers",
            {
                "command_id": command_id(),
                "expected_revision": companion["revision"],
                "target_node_id": node_id,
                "reason": "personality_preview_example",
            },
        )
        request(
            "POST",
            f"/v1/transfers/{transfer['transfer_id']}/ack",
            {"node_id": node_id, "phase": "ready"},
        )
        companion = request("GET", base)
        cursor = request("GET", "/v1/events?limit=1")["next_sequence"]
        request(
            "POST",
            f"{base}/context",
            {
                "command_id": command_id(),
                "node_id": node_id,
                "epoch": companion["epoch"],
                "expected_revision": companion["revision"],
                "activity": {
                    "type": "reading",
                    "book_id": "demo-book",
                    "chapter": 2,
                    "paragraph": 7,
                },
            },
        )
        source = None
        while source is None:
            page = request("GET", f"/v1/events?after_sequence={cursor}&limit=500")
            source = next(
                (
                    event
                    for event in page["events"]
                    if event["companion_id"] == companion_id
                    and event["type"] == "companion.context_updated"
                ),
                None,
            )
            if source is None and page["next_sequence"] == cursor:
                raise RuntimeError("The companion reading event was not found")
            cursor = page["next_sequence"]
        request(
            "POST",
            f"{base}/memories",
            {
                "command_id": command_id(),
                "kind": "episode",
                "content": "这次示例里，我们一起读到了第二章第七段。",
                "source_event_id": source["event_id"],
                "source_node_id": node_id,
                "occurred_at": source["occurred_at"],
                "tags": ["共读", "demo-book"],
            },
        )
        print(
            json.dumps(
                {
                    "companion_id": companion_id,
                    "personality_revision": profile["revision"],
                    "state_revision": state["revision"],
                    "import_warnings": profile["import_warnings"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        scenarios = [
            ("quiet", False, "继续一起读这一段。"),
            ("quiet", True, "我们刚才一起读到哪了？"),
            ("focus", False, "继续一起读这一段。"),
            ("chat", False, "聊聊刚才一起读的内容。"),
        ]
        for mode, user_initiated, message in scenarios:
            request("POST", f"/v1/nodes/{node_id}/heartbeat", {"context": {}})
            companion = request("GET", base)
            request(
                "POST",
                f"{base}/lease",
                {"node_id": node_id, "epoch": companion["epoch"]},
            )
            request(
                "POST",
                f"{base}/context",
                {
                    "command_id": command_id(),
                    "node_id": node_id,
                    "epoch": companion["epoch"],
                    "expected_revision": companion["revision"],
                    "interaction_mode": mode,
                    "activity": {
                        "type": "reading",
                        "book_id": "demo-book",
                        "chapter": 2,
                        "paragraph": 7,
                    },
                },
            )
            preview = request(
                "POST",
                f"{base}/personality/preview",
                {
                    "user_message": message,
                    "user_initiated": user_initiated,
                    "token_budget": 3072,
                    "memory_token_budget": 800,
                },
            )
            print(
                json.dumps(
                    {
                        "mode": mode,
                        "user_initiated": user_initiated,
                        "participation": preview["participation"],
                        "estimated_tokens": preview["estimated_tokens"],
                        "memory_refs": preview["memory_refs"],
                        "omitted_sections": preview["omitted_sections"],
                        "model_calls": preview["model_calls"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if args.show_prompt:
                print(preview["context_text"])


if __name__ == "__main__":
    main()
