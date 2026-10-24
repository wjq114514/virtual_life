"""Voice execution, irreversible delivery gates, cancellation and durable billing."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from test_memory_dialogue import Clock
from virtual_life.api import create_app
from virtual_life.coordinator import DomainError
from virtual_life.ports import ModelResult, ModelUsage, SpeechResult
from virtual_life.screen_provider import FakeVisionProvider
from virtual_life.settings import Settings


class Model:
    def __init__(self):
        self.calls = 0
        self.requests = []
        self.action = lambda emit: emit("你好，我在这里陪你。")

    def stream(self, request, emit):
        self.calls += 1
        self.requests.append(request)
        self.action(emit)
        return ModelResult("你好，我在这里陪你。", "fake", ModelUsage(40, 10))

    def complete(self, request):
        return self.stream(request, lambda _: None)


class Speech:
    def __init__(self):
        self.calls = []
        self.action = lambda: None

    def synthesize(self, request):
        self.calls.append(request)
        self.action()
        return SpeechResult(
            b"\x00\x01" * 2400, "audio/pcm;format=s16le", {"sample_rate": 24000, "channels": 1}
        )


@pytest.fixture
def voice_runtime(tmp_path):
    clock, model, speech = Clock(), Model(), Speech()
    app = create_app(
        Settings(
            database_path=str(tmp_path / "voice.sqlite"),
            api_token="test",
            model_name="fake",
            model_base_url="http://model.invalid",
            auto_memory_enabled=False,
            lease_ttl_seconds=1000,
            node_ttl_seconds=1000,
            voice={"enabled": True},
        ),
        model_provider=model,
        speech_provider=speech,
    )
    for service in (
        app.state.coordinator,
        app.state.personality,
        app.state.memory,
        app.state.dialogue,
        app.state.voice,
    ):
        service.clock = clock
    # No lifespan workers: tests control exactly when each queued task executes.
    client = TestClient(app, headers={"Authorization": "Bearer test"})
    state = attach_desktop(client)
    app.state.coordinator.register_node(
        {
            "node_id": "desk",
            "label": "desk",
            "kind": "desktop",
            "capabilities": [
                "text.display",
                "audio.output",
                "audio.input",
                "speech.recognize.local",
            ],
        }
    )
    assert (
        client.post("/v1/nodes/desk/presentations", json={"presentation_id": "p"}).status_code
        == 201
    )
    service = app.state.voice
    session = service.create_session(
        "desk",
        {
            "command_id": "voice-start",
            "companion_id": "sui",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "presentation_id": "p",
            "mode": "text",
        },
    )
    yield client, app, clock, model, speech, session
    service.close()
    client.close()


def submit(runtime, name="turn", **changes):
    _, app, _, _, _, session = runtime
    state = app.state.coordinator.get_companion("sui")
    current = app.state.voice.get_session("desk", session["session_id"])
    body = {
        "command_id": name,
        "turn_id": name,
        "session_generation": session["session_generation"],
        "expected_output_generation": current["output_generation"],
        "epoch": state["epoch"],
        "expected_revision": state["revision"],
        "text": "你好",
        "end_reason": "manual",
        "input": {"kind": "text"},
        **changes,
    }
    return app.state.voice.submit_turn("desk", session["session_id"], body), body


def interrupt(runtime, turn, command="stop-turn"):
    _, app, _, _, _, session = runtime
    return app.state.voice.interrupt(
        "desk",
        session["session_id"],
        {
            "command_id": command,
            "session_generation": session["session_generation"],
            "target_turn_id": turn["turn_id"],
            "expected_output_generation": turn["output_generation"],
            "reason": "user_speech",
        },
    )


def snapshot(runtime, turn="turn"):
    _, app, _, _, _, session = runtime
    return app.state.voice.get_turn("desk", session["session_id"], turn)


def test_text_to_speech_reuses_dialogue_and_replays_without_a_second_call(voice_runtime):
    client, app, _, model, speech, session = voice_runtime
    accepted, body = submit(voice_runtime)
    assert app.state.voice.process_next()
    result = snapshot(voice_runtime)
    assert result["status"] == "completed" and result["segment_count"] == 1, result
    assert model.calls == 1 and len(speech.calls) == 1
    assert app.state.voice.submit_turn("desk", session["session_id"], body) == accepted
    assert not app.state.voice.process_next()
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 50
    audio = app.state.voice.segment_audio("desk", session["session_id"], "turn", 1)
    assert hashlib.sha256(audio["bytes"]).hexdigest() == audio["sha256"]
    assert len(audio["bytes"]) == audio["sample_count"] * 2
    events = app.state.voice.list_events("turn")
    assert all(e["payload"]["turn_id"] == "turn" for e in events)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["kind"] == "voice_end"
    # Caller cannot attach internal voice provenance through public text APIs.
    response = client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": "forged",
            "node_id": "desk",
            "epoch": body["epoch"],
            "expected_revision": body["expected_revision"],
            "user_message": "x",
            "voice": {"turn_id": "turn"},
        },
    )
    assert response.status_code == 422


def test_stop_before_worker_claim_never_dispatches(voice_runtime):
    _, app, _, model, speech, session = voice_runtime
    submit(voice_runtime)
    stopped = app.state.voice.stop_session("desk", session["session_id"], {"command_id": "stop"})
    assert stopped["status"] == "stopped"
    assert (
        app.state.voice.stop_session("desk", session["session_id"], {"command_id": "stop"})
        == stopped
    )
    assert not app.state.voice.process_next()
    assert snapshot(voice_runtime)["status"] == "interrupted"
    assert model.calls == 0 and not speech.calls


def test_completed_text_is_immutable_but_unplayed_audio_is_revoked(voice_runtime):
    _, app, _, _, _, session = voice_runtime
    turn, _ = submit(voice_runtime)
    app.state.voice.process_next()
    original = app.state.dialogue.get_result("sui", "turn")
    assert original["status"] == "completed"
    stopped = interrupt(voice_runtime, turn)
    assert stopped["output_generation"] > turn["output_generation"]
    assert app.state.dialogue.get_result("sui", "turn") == original
    assert snapshot(voice_runtime)["status"] == "completed"
    with pytest.raises(DomainError) as error:
        app.state.voice.audio_metadata("desk", session["session_id"], "turn", 1)
    assert error.value.code == "speech_cancelled"


def test_interrupted_model_drains_while_next_turn_can_complete(voice_runtime):
    _, app, _, model, _, _ = voice_runtime
    entered, release = threading.Event(), threading.Event()

    def stream(request, emit):
        model.calls += 1
        emit("你好，我在这里陪你。")
        if model.calls == 1:
            entered.set()
            assert release.wait(5)
        return ModelResult("你好，我在这里陪你。", "fake", ModelUsage(40, 10))

    model.stream = stream
    turn, _ = submit(voice_runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(app.state.voice.process_next)
        try:
            assert entered.wait(5)
            stopped = interrupt(voice_runtime, turn)
            assert stopped["status"] == "interrupted"
            submit(voice_runtime, "next", continuation_of_turn_id="turn", text="我是说，继续。")
            second = pool.submit(app.state.voice.process_next)
            assert second.result(timeout=5)
            assert snapshot(voice_runtime, "next")["status"] == "completed"
        finally:
            release.set()
        assert first.result(timeout=5)
    assert app.state.dialogue.get_result("sui", "turn")["status"] == "discarded"
    assert snapshot(voice_runtime)["status"] == "interrupted"
    assert model.calls == 2
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 100


def test_tts_does_not_block_llm_stream_or_lease_tasks(voice_runtime):
    _, app, _, model, speech, _ = voice_runtime
    synthesizing, release, generated = threading.Event(), threading.Event(), threading.Event()

    def synthesize():
        synthesizing.set()
        assert release.wait(5)

    def stream(request, emit):
        model.calls += 1
        emit("你好，我在这里陪你。")
        assert synthesizing.wait(5)
        generated.set()
        return ModelResult("你好，我在这里陪你。", "fake", ModelUsage(40, 10))

    speech.action, model.stream = synthesize, stream
    submit(voice_runtime)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(app.state.voice.process_next)
        try:
            assert generated.wait(5), "TTS must not run synchronously inside the LLM callback"
        finally:
            release.set()
        assert task.result(timeout=5)
    assert snapshot(voice_runtime)["status"] == "completed"


def test_audio_chunks_recheck_revocation_after_headers(voice_runtime):
    _, app, _, _, _, session = voice_runtime
    turn, _ = submit(voice_runtime)
    app.state.voice.process_next()
    blocks = app.state.voice.iter_audio("desk", session["session_id"], "turn", 1, chunk_bytes=40)
    assert len(next(blocks)) == 40
    interrupt(voice_runtime, turn)
    with pytest.raises(DomainError):
        next(blocks)


def test_late_playback_telemetry_does_not_grant_resume_permission(voice_runtime):
    _, app, _, _, _, session = voice_runtime
    turn, _ = submit(voice_runtime)
    app.state.voice.process_next()
    interrupt(voice_runtime, turn)
    result = app.state.voice.record_playback(
        "desk",
        session["session_id"],
        {
            "command_id": "late-playback",
            "turn_id": "turn",
            "output_generation": turn["output_generation"],
            "segment_sequence": 1,
            "played_samples": 2400,
            "status": "completed",
        },
    )
    assert result["recorded"] is True and result["valid"] is False


@pytest.mark.parametrize("invalid", ["language", "stale", "discontinuity"])
def test_asr_source_must_match_the_live_session(voice_runtime, invalid):
    _, app, clock, model, _, session = voice_runtime
    app.state.voice.stop_session("desk", session["session_id"], {"command_id": "switch"})
    state = app.state.coordinator.get_companion("sui")
    replacement = app.state.voice.create_session(
        "desk",
        {
            "command_id": "asr-start",
            "companion_id": "sui",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "presentation_id": "p",
            "mode": "continuous",
            "asr": {"engine": "sherpa_onnx", "model_id": "zipformer", "language": "zh"},
        },
    )
    session.clear()
    session.update(replacement)
    ended = clock.value - timedelta(seconds=11 if invalid == "stale" else 1)
    source = {
        "kind": "asr",
        "started_at": (ended - timedelta(seconds=2)).isoformat(),
        "ended_at": ended.isoformat(),
        "audio_duration_ms": 2000,
        "asr_model_id": "zipformer",
        "language": "en" if invalid == "language" else "zh",
        "discontinuity": invalid == "discontinuity",
    }
    with pytest.raises(DomainError) as error:
        submit(voice_runtime, input=source, end_reason="adaptive_endpoint")
    assert error.value.code == (
        "voice_input_stale" if invalid == "stale" else "voice_input_invalid"
    )
    assert not app.state.voice.process_next() and model.calls == 0


def test_idle_timeout_and_heartbeat_cannot_revive_old_session(voice_runtime):
    _, app, clock, _, _, session = voice_runtime
    clock.value += timedelta(seconds=121)
    assert app.state.voice.maintain()["expired_sessions"] == 1
    with pytest.raises(DomainError) as error:
        app.state.voice.heartbeat("desk", session["session_id"], {"command_id": "late"})
    assert error.value.code == "voice_session_inactive"


def test_playback_reports_validate_segment_size_generation_and_monotonicity(voice_runtime):
    _, app, _, _, _, session = voice_runtime
    turn, _ = submit(voice_runtime)
    app.state.voice.process_next()
    body = {
        "command_id": "played",
        "turn_id": "turn",
        "output_generation": turn["output_generation"],
        "segment_sequence": 1,
        "played_samples": 2400,
        "status": "completed",
    }
    report = app.state.voice.record_playback("desk", session["session_id"], body)
    assert report["recorded"]
    assert app.state.voice.record_playback("desk", session["session_id"], body) == report
    with pytest.raises(DomainError):
        app.state.voice.record_playback(
            "desk",
            session["session_id"],
            {**body, "command_id": "too-long", "played_samples": 2500},
        )
    with pytest.raises(DomainError):
        app.state.voice.record_playback(
            "desk",
            session["session_id"],
            {**body, "command_id": "backwards", "played_samples": 1, "status": "progress"},
        )


def test_global_command_namespace_blocks_other_operations(voice_runtime):
    client, app, _, _, _, session = voice_runtime
    state = app.state.coordinator.get_companion("sui")
    response = client.post(
        "/v1/companions/sui/context",
        json={
            "command_id": "voice-start",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "context": {"topic": "conflict"},
        },
    )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "idempotency_conflict"
    )
    assert session["status"] == "active"


def test_stale_interrupt_does_not_revoke_new_turn(voice_runtime):
    _, app, _, _, _, session = voice_runtime
    first, _ = submit(voice_runtime)
    app.state.voice.process_next()
    second, _ = submit(voice_runtime, "next")
    assert interrupt(voice_runtime, first)["status"] == "stale"
    app.state.voice.process_next()
    assert snapshot(voice_runtime, "next")["status"] == "completed"
    assert (
        app.state.voice.get_session("desk", session["session_id"])["output_generation"]
        == second["output_generation"]
    )


def test_late_synthesis_after_interrupt_never_publishes(voice_runtime):
    _, app, _, _, speech, session = voice_runtime
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(5)

    speech.action = slow
    turn, _ = submit(voice_runtime)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(app.state.voice.process_next)
        try:
            assert entered.wait(5)
            interrupt(voice_runtime, turn)
        finally:
            release.set()
        assert task.result(timeout=5)
    assert snapshot(voice_runtime)["segment_count"] == 0
    assert not list(app.state.voice.audio_dir.glob("*/*.pcm"))
    with pytest.raises(DomainError):
        app.state.voice.audio_metadata("desk", session["session_id"], "turn", 1)


def test_recovery_never_replays_queued_or_completed_output(voice_runtime):
    _, app, _, model, _, session = voice_runtime
    submit(voice_runtime)
    app.state.voice.process_next()
    submit(voice_runtime, "queued")
    receipt = app.state.dialogue.get_result("sui", "turn")
    app.state.voice.recover()
    assert not app.state.voice.process_next()
    assert model.calls == 1
    assert app.state.dialogue.get_result("sui", "turn") == receipt
    assert app.state.voice.get_session("desk", session["session_id"])["status"] == "expired"
    assert not list(app.state.voice.audio_dir.glob("*/*.pcm"))
    with pytest.raises(DomainError):
        app.state.voice.audio_metadata("desk", session["session_id"], "turn", 1)


def test_repeated_interrupts_cannot_exceed_two_dispatched_models(voice_runtime):
    _, app, _, model, _, _ = voice_runtime
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    lock = threading.Lock()

    def stream(request, emit):
        with lock:
            index = model.calls
            model.calls += 1
        entered[index].set()
        assert release.wait(5)
        emit("你好，我在这里陪你。")
        return ModelResult("你好，我在这里陪你。", "fake", ModelUsage(40, 10))

    model.stream = stream
    first, _ = submit(voice_runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(app.state.voice.process_next)
        try:
            assert entered[0].wait(5)
            interrupt(voice_runtime, first)
            second, _ = submit(voice_runtime, "second")
            next_task = pool.submit(app.state.voice.process_next)
            assert entered[1].wait(5)
            interrupt(voice_runtime, second, command="stop-second")
            with pytest.raises(DomainError) as error:
                submit(voice_runtime, "third")
            assert error.value.code == "voice_busy_draining"
            assert model.calls == 2
        finally:
            release.set()
        assert task.result(timeout=5)
        assert next_task.result(timeout=5)
    assert app.state.memory.budget_snapshot("sui")["spent_tokens"] == 100


def test_unconsumed_playback_has_a_bounded_wait_and_keeps_text(voice_runtime):
    _, app, _, model, _, _ = voice_runtime
    emitted, continue_model = threading.Event(), threading.Event()
    original = app.state.voice._emit_segment

    def emit_segment(*args):
        original(*args)
        emitted.set()

    def stream(request, emit):
        model.calls += 1
        emit("你好，我在这里陪你。")
        assert emitted.wait(5)
        emit("接着还有第二句。")
        continue_model.set()
        return ModelResult("你好，我在这里陪你。接着还有第二句。", "fake", ModelUsage(40, 20))

    app.state.voice.speech_config = replace(
        app.state.voice.speech_config, max_buffered_playback_seconds=0.05
    )
    app.state.voice._emit_segment = emit_segment
    model.stream = stream
    submit(voice_runtime)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(app.state.voice.process_next)
        assert continue_model.wait(5)
        assert task.result(timeout=5)
    result = snapshot(voice_runtime)
    assert result["status"] == "completed", str(result)
    assert result["speech_status"] == "degraded"
    assert result["assistant_message"].endswith("第二句。")


def test_voice_resource_paths_are_relative_to_toml_and_env_retains_cwd(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[speech]\nmodel_dir="models"\nmodel_manifest="manifest.json"\n'
        'worker_python="./venv/bin/python"\n',
        encoding="utf8",
    )
    for name in ("MODEL_DIR", "MODEL_MANIFEST", "WORKER_PYTHON"):
        monkeypatch.delenv("VIRTUAL_LIFE_SPEECH_" + name, raising=False)
    configured = Settings.load(path).speech
    assert configured.model_dir == str(tmp_path / "models")
    assert configured.model_manifest == str(tmp_path / "manifest.json")
    assert configured.worker_python == str(tmp_path / "venv/bin/python")
    monkeypatch.setenv("VIRTUAL_LIFE_SPEECH_MODEL_DIR", "env-models")
    assert Settings.load(path).speech.model_dir == "env-models"


@pytest.fixture
def screen_voice_runtime(voice_runtime, tmp_path, monkeypatch):
    import test_screen_vision as screen_tests

    client, app, clock, _, _, _ = voice_runtime
    service, worker = app.state.screen_vision, app.state.screen_worker
    service.config = replace(
        service.config, enabled=True, base_url="http://vision.invalid", model_name="fake"
    )
    service.storage_path = tmp_path / "screens"
    for name in ("uploads", "tmp"):
        (service.storage_path / name).mkdir(parents=True, exist_ok=True)
    service.clock = worker.clock = clock
    worker.config, worker.provider = service.config, FakeVisionProvider()
    node = app.state.coordinator.get_node("desk")
    app.state.coordinator.register_node(
        {
            "node_id": "desk",
            "label": "desk",
            "kind": "desktop",
            "capabilities": [*node["capabilities"], "vision.capture"],
        }
    )
    monkeypatch.setattr(screen_tests, "CAPTURE_NODE", "desk")
    runtime = screen_tests.Runtime(client, app, clock, worker.provider)
    session = runtime.create_session(capture_source="x11")
    upload = runtime.upload(session, screen_tests.png_bytes())
    frame = runtime.frame(session, 0, screen_tests.image_frame(session, upload))
    assert frame.status_code == 202, frame.text
    return voice_runtime, runtime, session, frame.json()


@pytest.mark.parametrize("invalidation", ["purge", "evidence_ttl"])
def test_selected_screen_evidence_is_revoked_at_audio_delivery(screen_voice_runtime, invalidation):
    voice_runtime, screens, screen_session, frame = screen_voice_runtime
    client, app, clock, model, _, voice_session = voice_runtime
    assert screens.run_worker()
    submit(
        voice_runtime,
        screen_reference={
            "node_id": "desk",
            "session_id": screen_session["session_id"],
            "generation": screen_session["generation"],
            "frame_id": frame["frame_id"],
        },
    )
    app.state.voice.process_next()
    assert snapshot(voice_runtime)["status"] == "completed"
    assert "fake analysis" in model.requests[0].context_text
    assert "untrusted observation" in model.requests[0].context_text
    if invalidation == "purge":
        response = client.post(
            f"/v1/nodes/desk/screen/sessions/{screen_session['session_id']}/purge",
            json={"command_id": "purge-screen"},
        )
        assert response.status_code == 202, response.text
    else:
        with app.state.voice.store.transaction(write=True) as connection:
            connection.execute(
                "UPDATE screen_states SET evidence_expires_at=? WHERE companion_id='sui'",
                ((clock.value - timedelta(seconds=1)).isoformat(),),
            )
    with pytest.raises(DomainError) as error:
        app.state.voice.audio_metadata("desk", voice_session["session_id"], "turn", 1)
    assert error.value.code == "screen_evidence_expired"
    assert app.state.dialogue.get_result("sui", "turn")["status"] == "completed"


def test_fresh_screen_pending_does_not_block_or_claim_current_vision(screen_voice_runtime):
    voice_runtime, _, screen_session, _ = screen_voice_runtime
    _, app, _, model, _, _ = voice_runtime
    submit(
        voice_runtime,
        screen_reference={
            "node_id": "desk",
            "session_id": screen_session["session_id"],
            "generation": screen_session["generation"],
            "policy": "request_fresh",
        },
    )
    app.state.voice.process_next()
    assert snapshot(voice_runtime)["status"] == "completed"
    assert "No current screen image has been observed" in model.requests[0].context_text
    assert "fake analysis" not in model.requests[0].context_text
