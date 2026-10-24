#!/usr/bin/env python3
"""Install the debug APK and optionally push a verified model to one Android device."""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = "dev.virtuallife.visiondemo"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    parser.add_argument(
        "--apk", type=Path, default=PROJECT / "app/build/outputs/apk/debug/app-debug.apk"
    )
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    if not args.apk.is_file():
        parser.error(f"APK does not exist: {args.apk}; run ./build-demo.sh first")
    if args.model:
        if args.model.name != "Qwen3-VL-2B-Instruct-MNN":
            parser.error("--model must be the Qwen3-VL-2B-Instruct-MNN directory")
        subprocess.run(
            [
                sys.executable,
                str(PROJECT / "scripts/download-model.py"),
                "--verify-only",
                "--output",
                str(args.model),
            ],
            check=True,
        )
    adb = ["adb"]
    if args.serial:
        adb += ["-s", args.serial]
    else:
        lines = subprocess.check_output(["adb", "devices"], text=True).splitlines()[1:]
        devices = [
            line.split()[0]
            for line in lines
            if len(line.split()) > 1 and line.split()[1] == "device"
        ]
        if len(devices) != 1:
            parser.error("Connect exactly one authorized Android device, or pass --serial")
        adb += ["-s", devices[0]]
    subprocess.run(adb + ["install", "-r", str(args.apk)], check=True)
    subprocess.run(adb + ["shell", "am", "start", "-n", f"{PACKAGE}/.app.MainActivity"], check=True)
    if args.model:
        remote = f"/sdcard/Android/data/{PACKAGE}/files/models"
        subprocess.run(adb + ["shell", "mkdir", "-p", remote], check=True)
        subprocess.run(adb + ["push", str(args.model), remote + "/"], check=True)
    print("Open the demo, grant Shizuku access, then load the model.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
