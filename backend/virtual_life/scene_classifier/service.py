"""HTTP surface for the local scene classifier.

Two endpoints are exposed:

``POST /classify``
    Simple JSON interface used by tooling and manual checks.

``POST /v1/decide``
    TypeSafe Jev ``choice``-compatible endpoint.  The existing
    ``model_routing.HttpChoiceRouter`` can consume it directly when every route
    id is one of ``daily``/``emotional``/``intimate``/``explicit``.

No request text is logged.  The service only reports labels and scores.
"""

import json
import secrets
import time
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .decision import Thresholds
from .inference import Predictor
from .labels import LABELS

MAX_REQUEST_CHARS = 4096


class ClassifyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_REQUEST_CHARS)


def create_app(
    predictor: Predictor,
    *,
    thresholds: Thresholds | None = None,
    token: str = "",
    max_chars: int = MAX_REQUEST_CHARS,
    title: str = "Virtual Life scene classifier",
) -> FastAPI:
    if not 1 <= max_chars <= 100_000:
        raise ValueError("max_chars must be between 1 and 100000")
    limits = thresholds or Thresholds()
    app = FastAPI(title=title, version=predictor.version)
    app.state.predictor = predictor
    app.state.thresholds = limits

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not token:
            return
        expected = f"Bearer {token}"
        if not secrets.compare_digest(authorization or "", expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def check_text(text: str) -> str:
        text = text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="text must not be empty")
        if len(text) > max_chars:
            raise HTTPException(status_code=413, detail=f"text exceeds {max_chars} characters")
        return text

    def run(text: str) -> dict[str, Any]:
        started = time.perf_counter()
        decision = predictor.predict(text, limits)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            **decision.as_dict(),
            "model": predictor.model_name,
            "version": predictor.version,
            "backend": predictor.backend,
            "latency_ms": round(elapsed_ms, 3),
        }

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "backend": predictor.backend,
            "version": predictor.version,
        }

    @app.post("/classify", dependencies=[Depends(authorize)])
    def classify(
        payload: ClassifyRequest | None = None,
        text: Annotated[str | None, Query(max_length=MAX_REQUEST_CHARS)] = None,
    ) -> dict[str, Any]:
        value = payload.text if payload is not None else text
        if value is None:
            raise HTTPException(status_code=422, detail="text is required")
        return run(check_text(value))

    @app.post("/v1/decide", dependencies=[Depends(authorize)])
    def decide_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        text = _jev_text(payload)
        criteria = _jev_criteria(payload)
        decision = predictor.predict(check_text(text), limits)
        candidates = [label for label in LABELS if label in criteria]
        if not candidates:
            raise HTTPException(
                status_code=422,
                detail="criteria keys must be scene labels: " + ", ".join(LABELS),
            )
        choice = (
            decision.label
            if decision.label in candidates
            else max(candidates, key=lambda label: decision.scores.get(label, 0.0))
        )
        confidence = float(decision.scores.get(choice, 0.0))
        return {
            "answers": {
                "route": {
                    "choice": choice,
                    "confidence": confidence,
                    "probabilities": {
                        label: float(decision.scores.get(label, 0.0)) for label in LABELS
                    },
                }
            },
            "model": predictor.model_name,
            "version": predictor.version,
        }

    return app


def _jev_text(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=422, detail="request body must be an object")
    state = payload.get("state")
    if isinstance(state, str):
        text = state
    elif isinstance(state, Mapping):
        for key in ("text", "message", "content", "input"):
            value = state.get(key)
            if isinstance(value, str):
                text = value
                break
        else:
            text = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)
    elif state is None:
        raise HTTPException(status_code=422, detail="state is required")
    else:
        text = str(state)
    if not text.strip():
        raise HTTPException(status_code=422, detail="state text must not be empty")
    return text


def _jev_criteria(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    questions = payload.get("questions")
    if not isinstance(questions, Mapping):
        raise HTTPException(status_code=422, detail="questions must be an object")
    route = questions.get("route")
    if not isinstance(route, Mapping):
        raise HTTPException(status_code=422, detail="questions.route must be an object")
    criteria = route.get("criteria")
    if not isinstance(criteria, Mapping) or not criteria:
        raise HTTPException(status_code=422, detail="questions.route.criteria must be an object")
    return criteria
