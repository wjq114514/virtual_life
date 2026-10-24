"""Strict validation and evidence binding for model-produced screen analyses.

An invalid model payload never becomes an empty successful analysis. Every
localized conclusion keeps an evidence reference and a bbox in the normalized
upload coordinate system; unlocalized conclusions are marked as such.
"""

import json
from collections.abc import Mapping, Sequence

from .screen_images import ALGORITHM_VERSION, PREPROCESSING_VERSION
from .screen_models import (
    MAX_ANALYSIS_CHANGES,
    MAX_ANALYSIS_ELEMENTS,
    MAX_ANALYSIS_SCENE_SUMMARY_CHARS,
    MAX_ANALYSIS_TEXT_BLOCKS,
    MAX_ANALYSIS_TEXT_CHARS,
    SCREEN_SCHEMA_VERSION,
    dump_bounded_analysis,
)

PROMPT_VERSION = "screen-prompt-v5"
ACTIVITIES = frozenset(
    {
        "code",
        "docs",
        "chat",
        "meeting",
        "video",
        "game",
        "media",
        "terminal",
        "browser",
        "unknown",
    }
)


class AnalysisValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: object, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AnalysisValidationError("screen_analysis_invalid", "Text fields must be strings")
    return value[:limit]


def _activity(value: object) -> str:
    return value if isinstance(value, str) and value in ACTIVITIES else "unknown"


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0.0 <= float(value) <= 1.0:
        return None
    return float(value)


def _bbox(value: object) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    box: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not 0.0 <= number <= 1.0:
            return None
        box.append(round(number, 6))
    if not box[0] < box[2] or not box[1] < box[3]:
        return None
    return box


def _string_list(value: object, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[str] = []
    for item in value[:limit]:
        if isinstance(item, str):
            result.append(item[:item_limit])
    return result


class _EvidenceBuilder:
    def __init__(self, frame_id: str) -> None:
        self.frame_id = frame_id
        self.items: list[dict] = []

    def add(
        self,
        *,
        source: str,
        bbox: list[float] | None,
        extractor: str | None = None,
        extractor_version: str | None = None,
        score: float | None = None,
    ) -> int:
        self.items.append(
            {
                "source": source,
                "frame_id": self.frame_id,
                "bbox": bbox,
                "extractor": extractor,
                "extractor_version": extractor_version,
                "score": score,
                "unlocalized": bbox is None,
            }
        )
        return len(self.items) - 1


def build_model_analysis(
    raw: object,
    *,
    frame_id: str,
    source_frame_ids: Sequence[str],
    companion_id: str,
    node_id: str,
    session_id: str,
    generation: int,
    sequence: int,
    sampled_at: str,
    completed_at: str,
    expires_at: str,
    model_identity: Mapping[str, object],
    preprocessing_version: str = PREPROCESSING_VERSION,
    algorithm_version: str = ALGORITHM_VERSION,
    client_features: Mapping[str, object] | None = None,
) -> dict:
    if not isinstance(raw, Mapping):
        raise AnalysisValidationError(
            "screen_analysis_invalid", "Model output must be a JSON object"
        )
    evidence = _EvidenceBuilder(frame_id)
    text_blocks: list[dict] = []
    for item in list(raw.get("text_blocks", []) or [])[:MAX_ANALYSIS_TEXT_BLOCKS]:
        if not isinstance(item, Mapping):
            continue
        text = _text(item.get("text"), MAX_ANALYSIS_TEXT_CHARS).strip()
        if not text:
            continue
        bbox = _bbox(item.get("bbox"))
        ref = evidence.add(
            source="server_vlm",
            bbox=bbox,
            extractor="vlm",
            extractor_version=str(model_identity.get("model", "")) or None,
            score=_score(item.get("confidence")),
        )
        text_blocks.append(
            {
                "text": text,
                "bbox": bbox,
                "confidence": _score(item.get("confidence")),
                "language": _text(item.get("language"), 64) or None,
                "evidence_refs": [ref],
            }
        )
    elements: list[dict] = []
    for item in list(raw.get("elements", []) or [])[:MAX_ANALYSIS_ELEMENTS]:
        if not isinstance(item, Mapping):
            continue
        role = _text(item.get("role"), 128).strip()
        if not role:
            continue
        bbox = _bbox(item.get("bbox"))
        ref = evidence.add(
            source="server_vlm",
            bbox=bbox,
            extractor="vlm",
            extractor_version=str(model_identity.get("model", "")) or None,
            score=_score(item.get("confidence")),
        )
        elements.append(
            {
                "element_id": _text(item.get("element_id"), 128) or f"vlm-{len(elements)}",
                "parent_id": _text(item.get("parent_id"), 128) or None,
                "role": role,
                "text": _text(item.get("text"), MAX_ANALYSIS_TEXT_CHARS),
                "bbox": bbox,
                "confidence": _score(item.get("confidence")),
                "evidence_refs": [ref],
            }
        )
    changes: list[dict] = []
    for item in list(raw.get("changes", []) or [])[:MAX_ANALYSIS_CHANGES]:
        if not isinstance(item, Mapping):
            continue
        description = _text(item.get("description"), MAX_ANALYSIS_TEXT_CHARS).strip()
        kind = _text(item.get("kind"), 64).strip()
        if not description and not kind:
            continue
        bbox = _bbox(item.get("bbox"))
        ref = evidence.add(
            source="server_vlm",
            bbox=bbox,
            extractor="vlm",
            extractor_version=str(model_identity.get("model", "")) or None,
            score=None,
        )
        changes.append(
            {
                "kind": kind or "unknown",
                "description": description or kind,
                "bbox": bbox,
                "evidence_refs": [ref],
            }
        )
    if client_features:
        _merge_client_evidence(client_features, frame_id, evidence, text_blocks, elements)
    limitations = _string_list(raw.get("limitations"), 32, 256)
    if not text_blocks and not elements and not changes:
        limitations.append("no_localized_content")
    payload = {
        "analysis_id": "",
        "schema_version": SCREEN_SCHEMA_VERSION,
        "frame_id": frame_id,
        "source_frame_ids": list(source_frame_ids),
        "companion_id": companion_id,
        "node_id": node_id,
        "session_id": session_id,
        "generation": generation,
        "sequence": sequence,
        "sampled_at": sampled_at,
        "completed_at": completed_at,
        "expires_at": expires_at,
        "status": "succeeded",
        "quality": _quality(raw.get("quality")),
        "limitations": limitations[:32],
        "coverage": raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {},
        "scene_summary": _text(raw.get("scene_summary"), MAX_ANALYSIS_SCENE_SUMMARY_CHARS),
        "activity": _activity(raw.get("activity")),
        "activity_score": _score(raw.get("activity_score")),
        "text_blocks": text_blocks,
        "elements": elements,
        "changes": changes,
        "evidence": evidence.items,
        "analysis_path": "vlm",
        "preprocessing_version": preprocessing_version,
        "algorithm_version": algorithm_version,
        "prompt_version": PROMPT_VERSION,
        "model_identity": dict(model_identity),
        "usage": {},
    }
    try:
        dump_bounded_analysis(payload)
    except ValueError as exc:
        raise AnalysisValidationError("screen_analysis_invalid", str(exc)) from exc
    return payload


def _quality(value: object) -> str:
    if isinstance(value, str) and value in (
        "ok",
        "partial",
        "unreadable",
        "unavailable",
        "unknown",
    ):
        return value
    return "ok"


def _merge_client_evidence(
    client_features: Mapping[str, object],
    frame_id: str,
    evidence: _EvidenceBuilder,
    text_blocks: list[dict],
    elements: list[dict],
) -> None:
    extractor = client_features.get("extractor")
    extractor_version = client_features.get("extractor_version")
    raw_blocks = client_features.get("ocr_blocks")
    if isinstance(raw_blocks, Sequence) and not isinstance(raw_blocks, (str, bytes)):
        for item in raw_blocks:
            if not isinstance(item, Mapping):
                continue
            text = _text(item.get("text"), MAX_ANALYSIS_TEXT_CHARS).strip()
            bbox = _bbox(item.get("bbox"))
            if not text or bbox is None:
                continue
            ref = evidence.add(
                source="client_ocr",
                bbox=bbox,
                extractor=str(extractor) if extractor else None,
                extractor_version=str(extractor_version) if extractor_version else None,
                score=_score(item.get("confidence")),
            )
            text_blocks.append(
                {
                    "text": text,
                    "bbox": bbox,
                    "confidence": _score(item.get("confidence")),
                    "language": _text(item.get("language"), 64) or None,
                    "evidence_refs": [ref],
                }
            )
    raw_elements = client_features.get("ui_elements")
    if isinstance(raw_elements, Sequence) and not isinstance(raw_elements, (str, bytes)):
        for item in raw_elements:
            if not isinstance(item, Mapping):
                continue
            role = _text(item.get("role"), 128).strip()
            bbox = _bbox(item.get("bbox"))
            if not role or bbox is None:
                continue
            ref = evidence.add(
                source="client_ui",
                bbox=bbox,
                extractor=str(extractor) if extractor else None,
                extractor_version=str(extractor_version) if extractor_version else None,
                score=_score(item.get("confidence")),
            )
            elements.append(
                {
                    "element_id": _text(item.get("element_id"), 128) or f"client-{len(elements)}",
                    "parent_id": _text(item.get("parent_id"), 128) or None,
                    "role": role,
                    "text": _text(item.get("text"), MAX_ANALYSIS_TEXT_CHARS),
                    "bbox": bbox,
                    "confidence": _score(item.get("confidence")),
                    "evidence_refs": [ref],
                }
            )


def minimal_analysis(
    *,
    frame_id: str,
    companion_id: str,
    node_id: str,
    session_id: str,
    generation: int,
    sequence: int,
    sampled_at: str,
    completed_at: str,
    expires_at: str,
    status: str,
    quality: str,
    analysis_path: str,
    limitations: Sequence[str],
    source_frame_ids: Sequence[str] | None = None,
    scene_summary: str = "",
) -> dict:
    payload = {
        "analysis_id": "",
        "schema_version": SCREEN_SCHEMA_VERSION,
        "frame_id": frame_id,
        "source_frame_ids": list(source_frame_ids or [frame_id]),
        "companion_id": companion_id,
        "node_id": node_id,
        "session_id": session_id,
        "generation": generation,
        "sequence": sequence,
        "sampled_at": sampled_at,
        "completed_at": completed_at,
        "expires_at": expires_at,
        "status": status,
        "quality": quality,
        "limitations": list(limitations),
        "coverage": {},
        "scene_summary": scene_summary[:MAX_ANALYSIS_SCENE_SUMMARY_CHARS],
        "activity": "unknown",
        "activity_score": None,
        "text_blocks": [],
        "elements": [],
        "changes": [],
        "evidence": [],
        "analysis_path": analysis_path,
        "preprocessing_version": PREPROCESSING_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model_identity": {},
        "usage": {},
    }
    dump_bounded_analysis(payload)
    return payload


def load_payload(payload_json: str) -> dict:
    try:
        value = json.loads(payload_json)
    except (ValueError, TypeError):
        raise AnalysisValidationError(
            "screen_analysis_invalid", "Stored analysis is corrupt"
        ) from None
    if not isinstance(value, dict):
        raise AnalysisValidationError("screen_analysis_invalid", "Stored analysis is not an object")
    return value
