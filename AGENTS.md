# Repository Guidelines

## Layout and entrypoints

- `backend/virtual_life/`: FastAPI runtime. `api.py` builds `create_app`; `coordinator.py` owns node, transfer, and lease state; `storage.py` is the SQLite layer; memory is split across `memory.py`, `auto_memory.py`, `embedding_memory.py`, and `knowledge_graph.py`; `dialogue.py` / `model_provider.py` handle model calls; `screen_*`, `voice_*`, and `scene_classifier/` are separate subsystems.
- `backend/virtual_life/webui/`: plain HTML/CSS/JS served by the API at `/ui/`; there is no frontend build step.
- `clients/android-node/`: JVM8 Kotlin SDK and protocol state machine; it must not depend on `android.*`. `clients/android-app/` is the Android host, `clients/linux-node/` is a separate uv project (daemon/TUI), and `clients/system-node/` is a stdlib-only device client.
- `clients/android-vision-demo/`: independent prototype with vendored MNN under `.deps/` and generated `.cxx/`; do not edit third-party/build outputs there.
- `scripts/`: standalone scene-classifier data generation, training, evaluation, ONNX export, and HTTP service. The classifier is not mounted in the main API; `scripts/nsfw_server.py` serves `/classify` and `/v1/decide`.

## Setup and run

- Python 3.12+ and uv. `uv sync` installs runtime/dev dependencies; `uv sync --group train` is required only for classifier training/evaluation/ONNX (torch uses the CPU index).
- `cp virtual-life.example.toml virtual-life.toml` then `uv run python -m virtual_life`. WebUI is `/ui/`, API docs `/docs`, and the default development token is `local-dev-token`.
- Config precedence is defaults < TOML file < explicitly set environment variables. The loader does **not** read `.env`; only the uvicorn `--env-file` path does. Explicit config paths must exist, and unknown sections/fields or invalid values fail startup. File-relative paths anchor to the config file; environment paths use the working directory. `--config` beats `VIRTUAL_LIFE_CONFIG`.
- `./serve.sh start|stop|restart|status|logs` runs `virtual_life.main:app` in the background on `0.0.0.0:8038` and waits for `/healthz`. Host/port come from `VIRTUAL_LIFE_HOST` / `VIRTUAL_LIFE_PORT`, not `[server]` in the TOML file.
- The direct entry `uv run uvicorn virtual_life.main:app --host 127.0.0.1 --port 8000` still loads the unified config.
- `[model]` is optional. Without it, memory inspection and prompt preview work, but dialogue does not.

## Verification

- `uv run pytest` runs `backend/tests`. Focus one file/test with `uv run pytest backend/tests/test_memory.py::test_name -q`.
- `uv run ruff check backend examples clients/system-node scripts` and `uv run ruff format --check backend examples clients/system-node scripts`.
- Linux client commands use its own project: `uv run --project clients/linux-node pytest clients/linux-node/tests` and `uv run ruff check clients/linux-node/src clients/linux-node/tests`.
- No CI, type checker, or Kotlin test suite is configured. Build the SDK with `clients/android-node/build-sdk.sh assemble exampleClasses`; build the Android host app with `clients/android-app/build-app.sh` (requires JDK 17+ and `ANDROID_HOME`).
- Classifier tests: `uv run pytest backend/tests/test_scene_classifier.py -q`. Datasets/models live under `.data/nsfw_classifier/` and are never runtime dependencies or commit candidates.
- After changing the HTTP surface, regenerate the contract with `uv run python examples/export_openapi.py` (writes `docs/openapi.json`).
- Backend tests use temporary databases and fake providers. Do not point tests at `.data/`, the live TOML, or real model/vision services. When touching coordination or memory, cover stale epochs, expired leases, idempotent replays, corrections, and recall budgets.

## Architecture constraints

- This is a single-owner modular monolith on SQLite. Commit state, idempotent command receipts, and events in one transaction. Recheck node ownership, epoch, lease, and presentation state at commit and output time; never bypass the output gates.
- Wire JSON is snake_case, timestamps are UTC ISO 8601, and mutating commands require `command_id` with replay/conflict semantics. Update `docs/protocol.md`, the generated OpenAPI, and Kotlin `@SerialName` types together for interface changes.
- Memory must retain provenance, validity boundaries, revisions, and source-version checks. Keep recall bounded and invalidate derived summaries/graph edges when their sources change. Model usage shares the daily/per-task token ledger.
- Settings are strict. A new config field requires coordinated edits in `settings.py` (section and environment mapping), `virtual-life.example.toml`, `.env.example`, and `docs/configuration.md`.
- Leave live artifacts alone: `virtual-life.toml`, `.env`, `.data/`, `.venv/`, `build/`, `dist/`, `.gradle/`, and `local.properties` are local/ignored. Use the example files as templates.

## Reference docs

- `docs/protocol.md` (wire contract), `docs/configuration.md` (config), `docs/architecture.md` (system flow), `docs/memory.md`, `docs/screen-vision.md`, `docs/voice-contract.md`, `docs/tools.md`, and `docs/nsfw-classifier-implementation.md`.
