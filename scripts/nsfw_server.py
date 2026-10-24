#!/usr/bin/env python
"""Run the scene-classifier HTTP service.

Examples::

    uv run python scripts/nsfw_server.py --artifact .data/nsfw_classifier/model/final
    curl -s localhost:8039/classify -H 'content-type: application/json' \
         -d '{"text":"今天好累，想抱抱你"}'

For ``model_routing`` integration, point its Jev gate at
``http://127.0.0.1:8039/v1/decide`` and name routes after scene labels.
"""

import argparse
import os
import sys
from pathlib import Path

from virtual_life.scene_classifier.inference import build_predictor, load_thresholds
from virtual_life.scene_classifier.service import create_app

DEFAULT_ARTIFACT = Path(".data/nsfw_classifier") / "model" / "final"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--thresholds", default="")
    parser.add_argument("--backend", choices=("auto", "torch", "onnx"), default="auto")
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8039)
    parser.add_argument("--token", default="")
    parser.add_argument("--max-chars", type=int, default=4096)
    parser.add_argument("--no-int8", dest="prefer_int8", action="store_false", default=True)
    parser.add_argument("--check", action="store_true", help="load and warm up, then exit")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.threads < 0:
        parser.error("threads must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    artifact = Path(args.artifact)
    try:
        limits = load_thresholds(artifact, args.thresholds or None)
        predictor = build_predictor(
            artifact,
            backend=args.backend,
            max_length=args.max_length,
            threads=args.threads or None,
            prefer_int8=args.prefer_int8,
        )
        predictor.warmup()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"cannot start classifier: {exc}", file=sys.stderr)
        return 2

    print(
        f"loaded {predictor.backend} backend, version={predictor.version}, "
        f"thresholds={limits.as_dict()}"
    )
    if args.check:
        decision = predictor.predict("今天好累，想抱抱你", limits)
        print(f"smoke: {decision.as_dict()}")
        return 0

    token = args.token or os.environ.get("VIRTUAL_LIFE_SCENE_CLASSIFIER_TOKEN", "")
    app = create_app(
        predictor,
        thresholds=limits,
        token=token,
        max_chars=args.max_chars,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
