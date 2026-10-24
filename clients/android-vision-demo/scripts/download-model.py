#!/usr/bin/env python3
"""Download the pinned official MNN visual model, checking every file's digest."""

import argparse
import concurrent.futures
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((PROJECT / "models/recommended.json").read_text())
DEFAULT_OUTPUT = PROJECT.parents[1] / ".data/android-vision-demo/models" / MANIFEST["directory"]


def valid(path, item):
    if not path.is_file() or path.stat().st_size != item["size"]:
        return False
    if "sha256" in item:
        digest = hashlib.sha256()
        expected = item["sha256"]
    else:
        digest = hashlib.sha1(f"blob {item['size']}\0".encode())
        expected = item["git_blob_sha1"]
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def download(output, item, verify_only):
    path = output / item["name"]
    if valid(path, item):
        print(f"verified {item['name']}", flush=True)
        return
    if verify_only:
        raise ValueError(f"Missing or invalid file: {path}")
    partial = path.with_name(path.name + ".part")
    route = "resolve" if "sha256" in item else "raw"
    url = (
        f"https://huggingface.co/{MANIFEST['repository']}/{route}/"
        f"{MANIFEST['revision']}/{item['name']}?download=true"
    )
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= item["size"]:
            if valid(partial, item):
                partial.replace(path)
                return
            partial.unlink()
            offset = 0
        headers = {"User-Agent": "virtual-life-vision-demo/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            print(f"download {item['name']} ({offset}/{item['size']} bytes)", flush=True)
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 206:
                    if not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("Unexpected partial response range")
                elif response.status == 200:
                    offset = 0
                else:
                    raise ValueError(f"Unexpected HTTP status: {response.status}")
                written = offset
                next_progress = written + 64 * 1024 * 1024
                with partial.open("ab" if offset else "wb") as target:
                    while chunk := response.read(1024 * 1024):
                        written += len(chunk)
                        if written > item["size"]:
                            raise ValueError("Download exceeds pinned file size")
                        target.write(chunk)
                        if written >= next_progress:
                            print(f"{item['name']}: {written}/{item['size']} bytes", flush=True)
                            next_progress = written + 64 * 1024 * 1024
            if not valid(partial, item):
                if partial.stat().st_size == item["size"]:
                    partial.unlink()
                raise ValueError("Downloaded file failed integrity verification")
            partial.replace(path)
            print(f"verified {item['name']}", flush=True)
            return
        except (OSError, ValueError) as error:
            if attempt == 2:
                raise RuntimeError(f"Failed to download {item['name']}: {error}") from error
            print(f"retry {item['name']}: {error}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(download, args.output, item, args.verify_only) for item in MANIFEST["files"]
        ]
        for future in futures:
            future.result()
    print(f"Ready: {args.output / 'config.json'}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(str(error))
