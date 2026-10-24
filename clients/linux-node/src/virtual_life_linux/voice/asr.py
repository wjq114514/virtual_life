"""Optional sherpa-onnx streaming recognizer and Silero VAD adapter."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from .manifest import ModelManifest, VoiceModelError


@dataclass(frozen=True, slots=True)
class AsrUpdate:
    kind: str
    segment_id: str
    revision: int
    sample_start: int
    text: str


class SherpaOnnxEngine:
    def __init__(self, manifest: ModelManifest, *, num_threads: int = 2):
        if not 1 <= num_threads <= 8:
            raise ValueError("ASR num_threads must be between 1 and 8")
        self.manifest = manifest
        self.num_threads = num_threads
        self._sherpa = None
        self._numpy = None
        self._recognizer = None
        self._stream = None
        self._vad = None
        self._segment_number = 0
        self._revision = 0
        self._segment_start = 0
        self._sample_cursor = 0
        self._last_text = ""

    def load(self) -> None:
        self.manifest.check_runtime()
        try:
            self._sherpa = importlib.import_module("sherpa_onnx")
            self._numpy = importlib.import_module("numpy")
        except ImportError as exc:
            raise VoiceModelError("install the optional 'voice' dependencies to load ASR") from exc
        files = self.manifest.files
        try:
            self._recognizer = self._sherpa.OnlineRecognizer.from_transducer(
                tokens=str(files["tokens"]),
                encoder=str(files["encoder"]),
                decoder=str(files["decoder"]),
                joiner=str(files["joiner"]),
                num_threads=self.num_threads,
                sample_rate=self.manifest.sample_rate,
                feature_dim=80,
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=0.7,
                rule2_min_trailing_silence=1.2,
                rule3_min_utterance_length=20.0,
                decoding_method="greedy_search",
            )
            vad_config = self._sherpa.VadModelConfig(
                silero=self._sherpa.SileroVadModelConfig(
                    model=str(files["vad"]),
                    threshold=0.5,
                    min_speech_duration=0.16,
                    min_silence_duration=0.25,
                    window_size=512,
                    max_speech_duration=45.0,
                ),
                sample_rate=self.manifest.sample_rate,
                num_threads=1,
                provider="cpu",
            )
            self._vad = self._sherpa.VoiceActivityDetector(
                vad_config,
                buffer_size_in_seconds=45.0,
            )
            self._stream = self._recognizer.create_stream()
        except Exception as exc:  # noqa: BLE001
            self._recognizer = None
            self._vad = None
            self._stream = None
            raise VoiceModelError(f"sherpa-onnx model load failed: {exc}") from exc

    @property
    def loaded(self) -> bool:
        return self._recognizer is not None and self._vad is not None

    @property
    def sample_cursor(self) -> int:
        return self._sample_cursor

    def accept_pcm(self, pcm_s16le: bytes) -> tuple[bool, list[AsrUpdate]]:
        self._require_loaded()
        if not pcm_s16le or len(pcm_s16le) % 2:
            raise ValueError("PCM input must contain whole signed 16-bit samples")
        samples = self._numpy.frombuffer(pcm_s16le, dtype="<i2").astype("float32") / 32768.0
        updates: list[AsrUpdate] = []
        for start in range(0, len(samples), 512):
            block = samples[start : start + 512]
            if len(block) != 512:
                raise ValueError("PipeWire PCM frames must contain exactly 512 samples")
            self._vad.accept_waveform(block)
            active = bool(self._vad.is_speech_detected())
            self._sample_cursor += len(block)
            self._recognizer.accept_waveform(self._stream, self.manifest.sample_rate, block)
            while self._recognizer.is_ready(self._stream):
                self._recognizer.decode_stream(self._stream)
            result = self._recognizer.get_result(self._stream)
            text = getattr(result, "text", "") or ""
            if text != self._last_text:
                self._revision += 1
                self._last_text = text
                updates.append(
                    AsrUpdate(
                        "hypothesis",
                        str(self._segment_number),
                        self._revision,
                        self._segment_start,
                        text,
                    )
                )
            if self._recognizer.is_endpoint(self._stream):
                if text.strip():
                    self._revision += 1
                    updates.append(
                        AsrUpdate(
                            "segment_end",
                            str(self._segment_number),
                            self._revision,
                            self._segment_start,
                            text,
                        )
                    )
                self._recognizer.reset(self._stream)
                self._segment_number += 1
                self._segment_start = self._sample_cursor
                self._revision = 0
                self._last_text = ""
            if start == 0:
                frame_active = active
        return bool(frame_active), updates

    def flush(self) -> tuple[str, list[AsrUpdate]]:
        self._require_loaded()
        stream = self._stream
        self._recognizer.input_finished(stream)
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        result = self._recognizer.get_result(stream)
        text = getattr(result, "text", "") or ""
        updates: list[AsrUpdate] = []
        if text.strip():
            self._revision += 1
            updates.append(
                AsrUpdate(
                    "segment_end",
                    str(self._segment_number),
                    self._revision,
                    self._segment_start,
                    text,
                )
            )
        self._stream = self._recognizer.create_stream()
        self._segment_number += 1
        self._segment_start = self._sample_cursor
        self._revision = 0
        self._last_text = ""
        return text, updates

    def _require_loaded(self) -> None:
        if not self.loaded:
            raise VoiceModelError("ASR model has not been loaded")
