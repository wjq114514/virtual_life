"""Measure the screenshot -> dialogue first-character latency.

The script talks HTTP to a running runtime that has ``[screen_vision]`` enabled
and a dialogue model configured. It creates an isolated node/companion, uploads a
real screenshot and submits a frame. In the default two-stage mode it waits for
the durable vision worker and verifies the injected ``[screen_evidence]`` marker;
with ``--single-pass`` it skips analysis and verifies that the latest image is
attached to the dialogue request via the ``[screen_image]`` marker.

Run: uv run python examples/screen_vision_latency.py \
        --base-url http://127.0.0.1:8038 --image /path/to/screenshot.png
     uv run python examples/screen_vision_latency.py --single-pass ...
"""

import argparse
import hashlib
import io
import json
import os
import statistics
import time
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

TERMINAL_STATUSES = ("succeeded", "reused", "failed", "partial", "unknown")


class RuntimeClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method, path, body=None, raw=None, content_type="application/json"):
        data = raw
        if data is None and body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": content_type},
        )
        try:
            with urlopen(request, timeout=120) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc

    def stream(self, path, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        return urlopen(request, timeout=180)


def register(client: RuntimeClient, node_id: str, companion_id: str) -> dict:
    client.request(
        "POST",
        "/v1/nodes",
        {
            "node_id": node_id,
            "label": node_id,
            "kind": "desktop",
            "capabilities": ["vision.capture", "text.display"],
        },
    )
    client.request("POST", "/v1/companions", {"companion_id": companion_id, "name": companion_id})
    transfer = client.request(
        "POST",
        f"/v1/companions/{companion_id}/transfers",
        {
            "command_id": f"{companion_id}-attach",
            "target_node_id": node_id,
            "expected_revision": 0,
        },
    )
    client.request(
        "POST",
        f"/v1/transfers/{transfer['transfer_id']}/ack",
        {"node_id": node_id, "phase": "ready"},
    )
    state = client.request("GET", f"/v1/companions/{companion_id}")
    state = client.request(
        "POST",
        f"/v1/companions/{companion_id}/context",
        {
            "command_id": f"{companion_id}-mode",
            "node_id": node_id,
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "interaction_mode": "chat",
        },
    )
    client.request(
        "POST",
        f"/v1/companions/{companion_id}/lease",
        {"node_id": node_id, "epoch": state["epoch"]},
    )
    client.request(
        "POST",
        f"/v1/companions/{companion_id}/personality",
        {
            "command_id": f"{companion_id}-personality",
            "expected_revision": 0,
            "definition": {"traits": {"initiative": 0.7}},
        },
    )
    return client.request("GET", f"/v1/companions/{companion_id}")


def load_variant(path: str, index: int) -> bytes:
    from PIL import Image, ImageDraw

    with Image.open(path) as source:
        image = source.convert("RGB")
        draw = ImageDraw.Draw(image)
        accent = (40 + index * 53) % 256
        draw.rectangle((0, 0, 96, 96), fill=(accent, 60, 120))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def create_session(client, node_id, companion_id, profile):
    return client.request(
        "POST",
        f"/v1/nodes/{node_id}/screen/sessions",
        {
            "command_id": str(uuid4()),
            "companion_id": companion_id,
            "subject_node_id": node_id,
            "display_id": "default",
            "mode": "continuous",
            "user_initiated": True,
            "capture_source": "other",
            "requested_profile": profile,
        },
    )


def submit_frame(client, node_id, session, png, sequence):
    from PIL import Image

    with Image.open(io.BytesIO(png)) as probe:
        width, height = probe.size
    upload = client.request(
        "POST",
        f"/v1/nodes/{node_id}/screen/uploads",
        {
            "command_id": str(uuid4()),
            "session_id": session["session_id"],
            "generation": session["generation"],
            "media_type": "image/png",
            "byte_length": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "width": width,
            "height": height,
        },
    )
    client.request(
        "PUT",
        f"/v1/nodes/{node_id}/screen/uploads/{upload['upload_id']}/content",
        raw=png,
        content_type="image/png",
    )
    started = time.perf_counter()
    frame = client.request(
        "POST",
        f"/v1/nodes/{node_id}/screen/frames",
        {
            "command_id": str(uuid4()),
            "session_id": session["session_id"],
            "generation": session["generation"],
            "sequence": sequence,
            "sampled_at": datetime.now(UTC).isoformat(),
            "ttl_seconds": 90,
            "trigger": "changed",
            "input": {
                "kind": "image",
                "upload_id": upload["upload_id"],
                "capture_width": width,
                "capture_height": height,
                "rotation_degrees": 0,
            },
        },
    )
    return frame, started


def wait_for_analysis(client, node_id, frame_id, timeout):
    deadline = time.monotonic() + timeout
    snapshot = {}
    while time.monotonic() < deadline:
        snapshot = client.request("GET", f"/v1/nodes/{node_id}/screen/frames/{frame_id}")
        if snapshot["status"] in TERMINAL_STATUSES:
            break
        time.sleep(0.1)
    ready = time.perf_counter()
    analysis = None
    if snapshot.get("analysis_id"):
        analysis = client.request(
            "GET", f"/v1/nodes/{node_id}/screen/analyses/{snapshot['analysis_id']}"
        )
    return snapshot, analysis, ready


def dialogue_first_char(client, companion_id, node_id, state, prompt):
    body = {
        "command_id": str(uuid4()),
        "node_id": node_id,
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "user_message": prompt,
        "max_output_tokens": 256,
    }
    started = time.perf_counter()
    first_char = None
    final = None
    with client.stream(f"/v1/companions/{companion_id}/dialogue/stream", body) as response:
        if response.status != 200:
            raise RuntimeError(response.read().decode("utf-8"))
        kind = None
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if kind == "error":
                    raise RuntimeError(json.dumps(data, ensure_ascii=False))
                if kind == "delta" and first_char is None and data.get("text"):
                    first_char = time.perf_counter()
                if kind == "result":
                    final = data
                    break
    return first_char, started, final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8038")
    parser.add_argument(
        "--token", default=os.environ.get("VIRTUAL_LIFE_API_TOKEN", "local-dev-token")
    )
    parser.add_argument("--image", default="/home/wjqawa/1792306635399199017.png")
    parser.add_argument("--profile", default="balanced")
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="skip the vision worker and submit the frame image with the dialogue",
    )
    args = parser.parse_args()

    client = RuntimeClient(args.base_url, args.token)
    run_id = uuid4().hex[:8]
    node_id, companion_id = f"e2e-{run_id}", f"e2e-sui-{run_id}"
    state = register(client, node_id, companion_id)
    session = create_session(client, node_id, companion_id, args.profile)
    print(
        f"node={node_id} companion={companion_id} session={session['session_id']} "
        f"profile={session['effective_profile']}"
    )

    rows = []
    for index in range(args.trials):
        png = load_variant(args.image, index)
        frame, submitted = submit_frame(client, node_id, session, png, index)
        if args.single_pass:
            snapshot = client.request(
                "GET", f"/v1/nodes/{node_id}/screen/frames/{frame['frame_id']}"
            )
            analysis = None
            ready = time.perf_counter()
        else:
            snapshot, analysis, ready = wait_for_analysis(
                client, node_id, frame["frame_id"], args.timeout
            )
            if analysis is None:
                rows.append(
                    {
                        "trial": index,
                        "status": snapshot["status"],
                        "error": snapshot.get("error_code"),
                    }
                )
                print(
                    f"trial {index}: vision {snapshot['status']} error={snapshot.get('error_code')}"
                )
                continue

        state = client.request("GET", f"/v1/companions/{companion_id}")
        client.request(
            "POST",
            f"/v1/companions/{companion_id}/lease",
            {"node_id": node_id, "epoch": state["epoch"]},
        )
        state = client.request("GET", f"/v1/companions/{companion_id}")
        summary = str((analysis or {}).get("scene_summary", ""))[:400]
        prompt = "用一句话说说你从屏幕上注意到了什么。"
        first_char, dialogue_started, final = dialogue_first_char(
            client, companion_id, node_id, state, prompt
        )
        vision_seconds = None if args.single_pass else round(ready - submitted, 3)
        dialogue_seconds = (first_char - dialogue_started) if first_char else None
        full_seconds = (first_char - submitted) if first_char else None
        usage = (analysis or {}).get("usage") or {}
        result_usage = (final or {}).get("usage") or {}
        context_text = ((final or {}).get("preview") or {}).get("context_text") or ""
        screen_evidence = "[screen_evidence" in context_text
        screen_image = "[screen_image: current screen frame" in context_text
        row = {
            "trial": index,
            "frame_status": snapshot["status"],
            "analysis_path": (analysis or {}).get("analysis_path"),
            "quality": (analysis or {}).get("quality"),
            "screen_evidence": screen_evidence,
            "screen_image": screen_image,
            "vision_seconds": vision_seconds,
            "model_ms": usage.get("model_ms"),
            "image_count": usage.get("image_count") if analysis else int(screen_image),
            "input_tokens": result_usage.get("input_tokens"),
            "output_tokens": result_usage.get("output_tokens"),
            "dialogue_ttft_seconds": round(dialogue_seconds, 3) if dialogue_seconds else None,
            "full_chain_seconds": round(full_seconds, 3) if full_seconds else None,
            "reply_status": final.get("status") if final else None,
        }
        rows.append(row)
        vision_text = "single-pass" if vision_seconds is None else f"{vision_seconds:6.2f}s"
        print(
            f"trial {index}: frame={row['frame_status']} path={row['analysis_path']} "
            f"vision={vision_text} model_ms={row['model_ms']} "
            f"dialogue_ttft={row['dialogue_ttft_seconds']}s "
            f"full_chain={row['full_chain_seconds']}s status={row['reply_status']} "
            f"screen_evidence={screen_evidence} screen_image={screen_image} "
            f"tokens={row['input_tokens']}/{row['output_tokens']} summary={summary[:80]!r}"
        )
        time.sleep(0.4)

    client.request(
        "POST",
        f"/v1/nodes/{node_id}/screen/sessions/{session['session_id']}/stop",
        {"command_id": str(uuid4())},
    )
    client.request(
        "POST",
        f"/v1/nodes/{node_id}/screen/sessions/{session['session_id']}/purge",
        {"command_id": str(uuid4())},
    )

    def median(key):
        values = [row[key] for row in rows if row.get(key) is not None]
        return round(statistics.median(values), 3) if values else None

    print(
        json.dumps(
            {
                "trials": len(rows),
                "median_vision_seconds": median("vision_seconds"),
                "median_dialogue_ttft_seconds": median("dialogue_ttft_seconds"),
                "median_full_chain_seconds": median("full_chain_seconds"),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
