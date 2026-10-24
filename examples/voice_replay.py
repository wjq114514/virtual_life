"""Deterministic client-side voice turn replay (no server, no model, no audio).

This drives the Linux host's pure turn state machine with a scripted transcript
and a virtual clock so endpoint decisions are reproducible without a microphone
or ASR model. It complements ``examples/voice_live_test.py`` (which exercises the
server-side HTTP/SSE/audio contract): here we show the *client* half — when a
turn is kept open, when it commits, when a late flush is discarded and when new
speech becomes a barge-in.

Real ASR replay needs a verified sherpa-onnx model manifest on the host; this
tool intentionally starts from frozen text so it can run anywhere.

Usage:
    uv run python examples/voice_replay.py [--script JSON] [--json]

A script is ``[{"text": "...", "pause_after": 1.0}, ...]``; omit ``--script`` for
the built-in two-turn demo.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "linux-node" / "src"))

from virtual_life_linux.voice.clock import VirtualClock  # noqa: E402
from virtual_life_linux.voice.turns import TurnManager, TurnState  # noqa: E402

DEFAULT_SCRIPT = [
    {"text": "帮我查一下", "pause_after": 0.4},
    {"text": "明天北京的天气。", "pause_after": 1.2},
    {"text": "去上海，不是，去北京。", "pause_after": 1.2},
    {"text": "继续。", "pause_after": 1.2},
]

SAMPLE_RATE = 16_000


def replay(script, *, verbose=False):
    clock = VirtualClock()
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return f"turn-{counter['n']}"

    manager = TurnManager(clock=clock, sample_rate=SAMPLE_RATE, id_factory=next_id)
    events = []
    events.extend((action.kind, action.data) for action in manager.arm())

    sample_index = 0
    for index, turn in enumerate(script):
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        # Speaking: begin or continue the turn at this sample cursor.
        clock.advance(0.2)
        sample_index += int(0.2 * SAMPLE_RATE)
        speech_actions = manager.on_audio_activity(True, sample_index=sample_index)
        barge_in = any(action.kind == "barge_in" for action in speech_actions)
        events.extend((action.kind, action.data) for action in speech_actions)
        segment_id = f"seg-{index}"
        manager.on_asr_event(
            "hypothesis", segment_id=segment_id, revision=1, sample_start=sample_index, text=text
        )
        events.append((f"partial[{segment_id}]", {"draft": manager.draft}))
        manager.on_asr_event(
            "segment_end", segment_id=segment_id, revision=2, sample_start=sample_index, text=text
        )

        pause = max(0.0, float(turn.get("pause_after", 1.0)))
        steps = max(1, int(pause / 0.1))
        for _ in range(steps):
            clock.advance(0.1)
            sample_index += int(0.1 * SAMPLE_RATE)
            for action in manager.on_audio_activity(False, sample_index=sample_index):
                events.append((action.kind, action.data))

        if manager.state == TurnState.FINALIZING:
            generation = manager.generation
            for action in manager.flush_complete(generation, final_text=text):
                if action.kind == "commit":
                    commit = dict(action.data)
                    commit["committed_at_ns"] = clock.now_ns()
                    events.append(("commit", commit))
                else:
                    events.append((action.kind, action.data))
        elif manager.state == TurnState.NEEDS_CONFIRMATION:
            events.append(("needs_confirmation", {"draft": manager.draft}))
            # The host would ask the user; here we cancel and re-arm.
            for action in manager.cancel("replay_confirmation"):
                events.append((action.kind, action.data))
            for action in manager.arm():
                events.append((action.kind, action.data))

        if verbose:
            note = " (barge-in continuation)" if barge_in else ""
            print(f"after {text!r}: state={manager.state.value}{note}")

    commits = [data for kind, data in events if kind == "commit"]
    return {
        "commits": commits,
        "final_state": manager.state.value,
        "event_kinds": [kind for kind, _ in events],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", help="path to a JSON script of turns")
    parser.add_argument("--json", action="store_true", help="emit the replay as JSON")
    args = parser.parse_args()

    script = DEFAULT_SCRIPT
    if args.script:
        script = json.loads(Path(args.script).read_text(encoding="utf-8"))

    result = replay(script, verbose=not args.json)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\ncommitted {len(result['commits'])} turn(s), final state {result['final_state']}")
        for commit in result["commits"]:
            print(f"  - {commit['text']!r} ({commit['end_reason']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
