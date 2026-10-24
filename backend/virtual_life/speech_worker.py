"""One-shot, bounded sherpa-onnx Kokoro worker process."""

from __future__ import annotations

import array
import base64
import json
import math
import sys
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 16 * 1024
MAX_TEXT_CHARS = 1024
MAX_TEXT_BYTES = 4096
MAX_AUDIO_SECONDS = 60.0
MAX_THREADS = 8


class WorkerRequestError(ValueError):
    """Invalid request or model output from the isolated worker."""


def _safe_path(model_dir: Path, value: Any, *, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise WorkerRequestError(f"manifest field {name} must be a relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkerRequestError(f"manifest field {name} escapes model_dir")
    path = (model_dir / relative).resolve()
    if model_dir.resolve() not in path.parents:
        raise WorkerRequestError(f"manifest field {name} escapes model_dir")
    return path


def _load_paths(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Path]]:
    model_dir_value = request.get("model_dir")
    if not isinstance(model_dir_value, str) or not model_dir_value:
        raise WorkerRequestError("model_dir must be a non-empty path")
    model_dir = Path(model_dir_value).expanduser().resolve()
    if not model_dir.is_dir():
        raise WorkerRequestError("model directory is missing")

    manifest_path = request.get("manifest")
    if not isinstance(manifest_path, str) or not manifest_path:
        raise WorkerRequestError("a model manifest is required")
    try:
        manifest = json.loads(Path(manifest_path).expanduser().read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkerRequestError("model manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise WorkerRequestError("model manifest must be an object")

    names = {
        "model": manifest.get("model", "model.onnx"),
        "voices": manifest.get("voices", "voices.bin"),
        "tokens": manifest.get("tokens", "tokens.txt"),
        "espeak_data": manifest.get("espeak_data", "espeak-ng-data"),
    }
    paths = {key: _safe_path(model_dir, value, name=key) for key, value in names.items()}
    for name in ("model", "voices", "tokens"):
        if not paths[name].is_file():
            raise WorkerRequestError(f"required model file is missing: {name}")
    if not paths["espeak_data"].is_dir():
        raise WorkerRequestError("required eSpeak data directory is missing")

    sample_rate = manifest.get("sample_rate")
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
        raise WorkerRequestError("manifest sample_rate is invalid")
    return manifest, paths


def validate_request(request: Any) -> tuple[str, dict[str, Any], dict[str, Path]]:
    if not isinstance(request, dict):
        raise WorkerRequestError("request must be an object")
    operation = request.get("operation", "synthesize")
    if operation not in {"probe", "synthesize"}:
        raise WorkerRequestError("operation must be probe or synthesize")
    manifest, paths = _load_paths(request)

    threads = request.get("num_threads", 1)
    if type(threads) is not int or not 1 <= threads <= MAX_THREADS:
        raise WorkerRequestError(f"num_threads must be between 1 and {MAX_THREADS}")
    if operation == "synthesize":
        text = request.get("text")
        if not isinstance(text, str) or not text.strip():
            raise WorkerRequestError("text must be a non-empty string")
        if len(text) > MAX_TEXT_CHARS:
            raise WorkerRequestError("text exceeds the character limit")
        try:
            encoded = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise WorkerRequestError("text is not valid UTF-8") from exc
        if len(encoded) > MAX_TEXT_BYTES:
            raise WorkerRequestError("text exceeds the UTF-8 byte limit")
        seconds = request.get("max_audio_seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0 < seconds <= MAX_AUDIO_SECONDS
        ):
            raise WorkerRequestError(
                f"max_audio_seconds must be greater than 0 and at most {MAX_AUDIO_SECONDS:g}"
            )
        voice = request.get("voice")
        if (
            not isinstance(voice, str)
            or not voice
            or len(voice) > 64
            or any(ord(char) < 32 for char in voice)
        ):
            raise WorkerRequestError("voice must be a non-empty identifier of at most 64 chars")
    return operation, manifest, paths


def _build_tts(sherpa_onnx: Any, manifest: dict[str, Any], paths: dict[str, Path], threads: int):
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(paths["model"]),
                voices=str(paths["voices"]),
                tokens=str(paths["tokens"]),
                data_dir=str(paths["espeak_data"]),
            ),
            num_threads=threads,
        ),
        max_num_sentences=1,
    )
    return sherpa_onnx.OfflineTts(config)


def _speaker_id(voice: str, manifest: dict[str, Any]) -> int:
    voices = manifest.get("voice_ids")
    if not isinstance(voices, dict):
        raise WorkerRequestError("manifest voice_ids is missing")
    speaker = voices.get(voice)
    if type(speaker) is not int or not 0 <= speaker < 103:
        raise WorkerRequestError("voice is not present in the model manifest")
    return speaker


def _samples_to_s16le(samples: Any, max_samples: int) -> bytes:
    try:
        count = len(samples)
    except TypeError as exc:
        raise WorkerRequestError("model returned samples without a length") from exc
    if count < 1:
        raise WorkerRequestError("model returned empty audio")
    if count > max_samples:
        raise WorkerRequestError("model output exceeds max_audio_seconds")

    pcm = array.array("h")
    for sample in samples:
        value = float(sample)
        if not math.isfinite(value):
            raise WorkerRequestError("model returned a non-finite sample")
        scale = 32768 if value < 0 else 32767
        pcm.append(max(-32768, min(32767, round(value * scale))))
    if pcm.itemsize != 2:
        raise WorkerRequestError("host does not provide 16-bit signed samples")
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


def execute(request: dict[str, Any], sherpa_onnx: Any) -> dict[str, Any]:
    operation, manifest, paths = validate_request(request)
    threads = request.get("num_threads", 1)
    tts = _build_tts(sherpa_onnx, manifest, paths, threads)
    source_rate = manifest["sample_rate"]
    if operation == "probe":
        return {"ok": True, "sample_rate": source_rate}

    speaker = _speaker_id(request["voice"], manifest)
    audio = tts.generate(request["text"], sid=speaker, speed=1.0)
    sample_rate = getattr(audio, "sample_rate", None)
    if type(sample_rate) is not int or sample_rate != source_rate:
        raise WorkerRequestError("model output sample_rate does not match the manifest")
    max_samples = math.floor(request["max_audio_seconds"] * sample_rate)
    pcm = _samples_to_s16le(audio.samples, max_samples)
    sample_count = len(pcm) // 2
    return {
        "ok": True,
        "sample_rate": sample_rate,
        "channels": 1,
        "sample_count": sample_count,
        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
    }


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        sys.stderr.write("request exceeds byte limit\n")
        return 2
    try:
        request = json.loads(raw)
        import sherpa_onnx  # type: ignore

        response = execute(request, sherpa_onnx)
    except (ValueError, OSError, ImportError) as exc:
        sys.stderr.write(f"worker request failed: {exc}\n")
        return 2
    except Exception as exc:  # noqa: BLE001 - native runtime errors have no stable type
        sys.stderr.write(f"sherpa-onnx failed: {exc}\n")
        return 3
    sys.stdout.write(json.dumps(response, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
