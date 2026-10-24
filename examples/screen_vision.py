"""Run the server-side screen vision loop against a running runtime.

The runtime must have ``[screen_vision]`` enabled *and* a vision provider
configured (see ``virtual-life.example.toml``); otherwise the frame resolves to
a terminal failure and no analysis is produced. This script never performs
model inference itself: it only talks HTTP to the runtime.

Run: uv run python examples/screen_vision.py [--base-url URL] [--token TOKEN]
"""

import argparse
import hashlib
import io
import json
import os
import time
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


class RuntimeClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        raw: bytes | None = None,
        content_type: str = "application/json",
    ):
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
            with urlopen(request, timeout=15) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc


def synthetic_png() -> bytes:
    """A small deterministic screenshot substitute that needs no external file."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 360), (24, 28, 38))
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 24, 616, 96), fill=(40, 46, 64))
    draw.text((40, 48), "Screen vision smoke test", fill=(235, 238, 245))
    draw.rectangle((24, 120, 300, 336), fill=(32, 36, 50))
    draw.rectangle((324, 120, 616, 336), fill=(32, 36, 50))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def wait_for_terminal(client: RuntimeClient, node_id: str, frame_id: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = client.request("GET", f"/v1/nodes/{node_id}/screen/frames/{frame_id}")
        if snapshot["status"] in ("succeeded", "failed"):
            return snapshot
        time.sleep(0.25)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--token", default=os.environ.get("VIRTUAL_LIFE_API_TOKEN", "local-dev-token")
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    client = RuntimeClient(args.base_url, args.token)

    run_id = uuid4().hex[:8]
    observer, companion = f"screen-{run_id}", f"sui-{run_id}"
    client.request(
        "POST",
        "/v1/nodes",
        {
            "node_id": observer,
            "kind": "android",
            "label": "屏幕采集端",
            "capabilities": ["vision.capture", "text.display"],
        },
    )
    client.request("POST", "/v1/companions", {"companion_id": companion, "name": "随"})

    session = client.request(
        "POST",
        f"/v1/nodes/{observer}/screen/sessions",
        {
            "command_id": str(uuid4()),
            "companion_id": companion,
            "subject_node_id": observer,
            "mode": "one_shot",
            "user_initiated": True,
            "capture_source": "other",
            "requested_profile": "balanced",
        },
    )
    session_id = session["session_id"]
    print(f"会话 {session_id} 生效 profile={session['effective_profile']}")

    png = synthetic_png()
    upload = client.request(
        "POST",
        f"/v1/nodes/{observer}/screen/uploads",
        {
            "command_id": str(uuid4()),
            "session_id": session_id,
            "generation": session["generation"],
            "media_type": "image/png",
            "byte_length": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "width": 640,
            "height": 360,
        },
    )
    client.request(
        "PUT",
        f"/v1/nodes/{observer}/screen/uploads/{upload['upload_id']}/content",
        raw=png,
        content_type="image/png",
    )

    frame = client.request(
        "POST",
        f"/v1/nodes/{observer}/screen/frames",
        {
            "command_id": str(uuid4()),
            "session_id": session_id,
            "generation": session["generation"],
            "sequence": 0,
            "sampled_at": datetime.now(UTC).isoformat(),
            "ttl_seconds": 90,
            "trigger": "manual",
            "input": {
                "kind": "image",
                "upload_id": upload["upload_id"],
                "capture_width": 640,
                "capture_height": 360,
                "rotation_degrees": 0,
            },
        },
    )
    print(f"帧已接收，查询路径 {frame['status_path']}")

    snapshot = wait_for_terminal(client, observer, frame["frame_id"], args.timeout)
    analysis = None
    if snapshot.get("analysis_id"):
        analysis = client.request(
            "GET", f"/v1/nodes/{observer}/screen/analyses/{snapshot['analysis_id']}"
        )
    state = client.request(
        "GET",
        f"/v1/nodes/{observer}/screen/state?companion_id={companion}&display_id=default",
    )
    client.request(
        "POST",
        f"/v1/nodes/{observer}/screen/sessions/{session_id}/stop",
        {
            "command_id": str(uuid4()),
        },
    )
    client.request(
        "POST",
        f"/v1/nodes/{observer}/screen/sessions/{session_id}/purge",
        {
            "command_id": str(uuid4()),
        },
    )

    if analysis is None:
        raise SystemExit(
            f"帧状态 {snapshot['status']} error={snapshot.get('error_code')}；"
            "请确认运行时已配置视觉 provider。"
        )
    print(
        json.dumps(
            {
                "companion_id": companion,
                "session_id": session_id,
                "frame_id": frame["frame_id"],
                "analysis_id": analysis["analysis_id"],
                "analysis_path": analysis["analysis_path"],
                "quality": analysis["quality"],
                "scene_summary": analysis["scene_summary"],
                "state": state["state"],
                "observable": state["observable"],
                "message": "完成上传 → 帧 → 后台分析 → 状态查询 → 停止/清除闭环。",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
