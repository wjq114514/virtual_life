# Virtual Life — Linux host client

User-level daemon, `vl` CLI and Textual TUI that make a Niri/DMS/kitty/fish
desktop a native runtime node. The daemon is the only native writer: it holds
the owner token, registers the node, renews its lease and owns the presentation
session. Local clients reach it over a Unix-socket JSON Lines IPC and never see
the token.

See [the frozen contract](../../docs/linux-client-contract.md) and the
[runtime protocol](../../docs/protocol.md) for the exact interfaces.

## Layout

| Path | Purpose |
| --- | --- |
| `src/virtual_life_linux/config.py` | strict TOML config, XDG paths, credentials |
| `src/virtual_life_linux/storage.py` | per-node SQLite: identity, cursor, recovery, dedup |
| `src/virtual_life_linux/lease.py` | conservative boot-time lease deadlines |
| `src/virtual_life_linux/render_guard.py` | per-paint generation and deadline gate |
| `src/virtual_life_linux/transport.py` | bearer HTTP + SSE client |
| `src/virtual_life_linux/ipc.py` | local Unix-socket IPC server and client |
| `src/virtual_life_linux/daemon.py` | lifecycle, presentation, dialogue, travel, attention |
| `src/virtual_life_linux/cli.py` | `vl` entry point |
| `src/virtual_life_linux/tui/` | Textual renderer |
| `src/virtual_life_linux/voice/` | daemon-owned capture, local ASR, turn state machine and fenced playback |
| `src/virtual_life_linux/state.py`, `policy/` | aggregated state and attention overrides |
| `src/virtual_life_linux/adapters/` | opt-in niri/logind/DMS attention sources and the niri window scene |
| `integrations/`, `packaging/` | systemd/fish/niri/DMS/desktop assets, `install.py`, `build.py` |

## Usage

```fish
vl doctor --json                 # diagnostics (no secrets)
vl status                        # aggregated daemon state
vl chat                          # open the terminal renderer
vl chat <companion>              # ...for a specific companion
vl companion list                # ids and the current selection
vl companion select <companion>  # change the selection without connecting
vl connect <companion>           # requires an attached renderer
vl mode focus
vl travel <node-id>
vl ask "你好"                     # or: echo "你好" | vl ask --stdin
vl attention override focus --seconds 900
vl attention clear
vl screen start --companion <companion> --profile balanced   # stream screenshots
vl screen status
vl screen stop
vl voice doctor --json           # voice readiness (model, PipeWire, AEC)
vl voice devices                 # capture / playback device inventory
vl voice start --mode continuous # continuous | push_to_talk | text
vl voice commit "读出来的一句话"  # typed turn (single sender per reply)
vl voice commit --release        # release a push_to_talk turn
vl voice interrupt               # stop the current reply playback
vl voice status
vl voice stop
vl daemon run                    # foreground daemon (normally via systemd)
```

`vl screen start` asks the daemon to open a server-side screen session and stream
`grim` captures (with the optional niri window scene) until `vl screen stop`. It
requires `screen.enabled=true` and `grim` on `PATH`; only the daemon's owner token
can reach the `/screen/*` routes.

## Attention telemetry

Only when `desktop.attention_enabled = true` does the daemon register
`attention.observe` and start sampling. Every `reporting.attention_seconds` it
merges the niri focused workspace and the logind idle/locked hints into one
bounded sample: `activity` is always `unknown`, `focus` is only `focused` when a
focused workspace exists, and `availability`/`locked` come from those booleans
alone. No window titles, command lines or shell arguments are ever sent.

The sample sequence is persisted per session and always increases, so a failed
request is never replayed with the same number. Identical content is not
re-sent, and `vl attention boundary ...` emits a sample immediately instead of
waiting for the next tick. The DMS adapter only probes shell presence.

## Continuous voice

Voice needs `voice.enabled=true` plus an attached presentation and an active
lease; the daemon owns capture, local ASR and playback and is the only server
writer. `vl voice start` refuses to run without those prerequisites and reports
an actionable error. Continuous mode loads the local sherpa-onnx ASR model named
by `voice.asr_manifest`; `vl voice doctor` verifies the model files, recognizer,
VAD, `pw-cat`/`pw-dump` and the AEC verdict. When no model is installed, use
`--mode text` to type turns or keep voice disabled and use `vl chat`.

`vl chat` mirrors the same controls in the Textual TUI: `ctrl+o` toggles a
continuous session, `ctrl+p` starts push-to-talk, `ctrl+g` commits the current
push-to-talk / typed turn, `ctrl+k` interrupts playback, and the input line
routes typed text through the voice turns API while a session is active (never
both entry points for one message). The status line shows the voice state,
playback progress, AEC verdict and the local transcript draft.

Microphone audio stays on the host: only frozen final text and bounded metadata
are submitted. The daemon plays reply segments through `pw-cat` and obeys the
server output generation, so an interrupted or superseded reply is never played.
Echo cancellation is not claimed: `doctor` reports `unverified`, and headset
full-duplex is the first real validation path.

## Install

```sh
uv run --project clients/linux-node python clients/linux-node/packaging/install.py
```

The installer is re-runnable and never overwrites user files. `--dry-run`
prints what would change, `--skip-package` only refreshes the assets, and
`--no-systemd` leaves the user unit untouched. It copies the systemd unit, the
fish config and completions, the niri snippet (which you still have to append to
your own `config.kdl`), the `.desktop` entry and the DMS manifest, then writes
`config.toml` plus a 0600 `credentials` placeholder that you replace with your
own token.

## Development

```sh
uv run --project clients/linux-node pytest clients/linux-node/tests
uv run ruff check clients/linux-node/src clients/linux-node/tests
uv run --project clients/linux-node python clients/linux-node/packaging/build.py
```

Validation status is tracked in
[docs/linux-client-validation.md](../../docs/linux-client-validation.md).

## Safety properties

- No user content leaves the host: attention adapters send workspace ids/output
  names and idle/locked booleans only; fish sends opaque command tokens, never
  arguments.
- The window scene is opt-in and separate from attention. `scene_enabled`
  reports app ids and geometry only; sending window titles (user content) also
  requires `scene_titles_enabled`, and every title is stripped of invisible and
  control characters before it leaves the host. The server additionally runs the
  titles through its injection gate.
- The TUI re-checks a monotonic deadline on every paint, so a frozen renderer
  cannot display stale drafts.
- Web and native entry points are mutually exclusive server-side; a busy device
  returns `presentation_busy`.
- Unsupported (legacy) servers keep telemetry working but gate full chat behind
  exit code 7 instead of silently bypassing presentation isolation.
