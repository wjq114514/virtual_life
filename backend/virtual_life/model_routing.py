"""Content-based model routing behind a typed decision gate.

The router picks a reply model and a vision model from a bounded catalog before
the corresponding provider call. When ``gate`` selects JEV (or a compatible HTTP
decision endpoint), the judge answers one ``choice`` question over the configured
route criteria. Every failure mode is non-blocking: missing configuration, a
timeout, a malformed answer or low confidence falls back to the default route,
which may leave both model fields empty to mean "use the configured model".

Routing sends the turn's text (user message, bounded screen evidence, window
context) to the judge endpoint. It is disabled by default and never grants
output authority, changes prompt content or bypasses the existing output gates.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

import httpx

MAX_ROUTES = 16
MAX_ROUTE_ID = 64
MAX_CRITERIA_CHARS = 512
MAX_MODEL_CHARS = 256
MAX_KEYWORD_CHARS = 64
MAX_KEYWORDS = 16
MAX_STATE_CHARS = 4096
MIN_TIMEOUT_SECONDS = 0.05
MAX_TIMEOUT_SECONDS = 30.0
ROUTE_KINDS = ("reply", "vision")
GATE_MODES = ("off", "deterministic", "decision", "jev")


@dataclass(frozen=True, slots=True)
class Route:
    """One routing outcome: a criteria description plus optional model overrides."""

    id: str
    criteria: str
    reply_model: str = ""
    vision_model: str = ""
    keywords: tuple[str, ...] = ()

    def model_for(self, kind: str) -> str:
        if kind == "reply":
            return self.reply_model
        if kind == "vision":
            return self.vision_model
        raise ValueError(f"Unknown route kind: {kind}")


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    enabled: bool = False
    gate: str = "off"
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    model: str = "typesafe/jev-latest"
    timeout_seconds: float = 2.0
    min_confidence: float = 0.5
    default_route: str = "default"
    routes: tuple[Route, ...] = ()

    @property
    def provider_configured(self) -> bool:
        return bool(self.base_url.strip())

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.routes)

    def route(self, route_id: str) -> Route | None:
        for route in self.routes:
            if route.id == route_id:
                return route
        return None

    @property
    def fallback(self) -> Route:
        return self.route(self.default_route) or Route(
            id=self.default_route or "default", criteria="default route"
        )


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_id: str
    reply_model: str = ""
    vision_model: str = ""
    reason: str = "default_route"
    confidence: float | None = None
    judge_model: str | None = None

    @property
    def used_default(self) -> bool:
        return self.reason != "judge_choice" and self.reason != "keyword_match"

    def model_for(self, kind: str) -> str:
        return self.reply_model if kind == "reply" else self.vision_model


@runtime_checkable
class ModelRouter(Protocol):
    """Choose a route for one provider call without granting output rights."""

    def choose(self, kind: Literal["reply", "vision"], state: object) -> RouteDecision: ...


class StaticRouter:
    """Always return the configured default route."""

    def __init__(self, config: RoutingConfig) -> None:
        self.config = config

    def choose(self, kind: str, state: object) -> RouteDecision:
        route = self.config.fallback
        return RouteDecision(
            route_id=route.id,
            reply_model=route.reply_model,
            vision_model=route.vision_model,
            reason="default_route",
        )


class DeterministicRouter:
    """Offline router: score bounded keywords, otherwise fall back."""

    def __init__(self, config: RoutingConfig) -> None:
        self.config = config
        self.fallback = StaticRouter(config)

    def choose(self, kind: str, state: object) -> RouteDecision:
        text = _state_text(state).casefold()
        best: Route | None = None
        best_score = 0
        for route in self.config.routes:
            if not route.keywords or not route.model_for(kind):
                continue
            score = sum(text.count(keyword.casefold()) for keyword in route.keywords)
            if score > best_score:
                best, best_score = route, score
        if best is None:
            return self.fallback.choose(kind, state)
        return RouteDecision(
            route_id=best.id,
            reply_model=best.reply_model,
            vision_model=best.vision_model,
            reason="keyword_match",
            confidence=float(min(1.0, best_score / max(1, len(best.keywords)))),
        )


class HttpChoiceRouter:
    """Typed ``choice`` judge with a static fallback.

    The request/response shape follows the TypeSafe Jev Decisions API: one
    ``choice`` question whose criteria keys are route IDs. The parser accepts
    both mapping and sequence answer shapes so older endpoint versions keep
    working.
    """

    def __init__(
        self,
        config: RoutingConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        fallback: ModelRouter | None = None,
        request_builder=None,
        response_parser=None,
    ) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.timeout_seconds = config.timeout_seconds
        self.transport = transport
        self.fallback = fallback or StaticRouter(config)
        self._request_builder = request_builder or _jev_choice_request
        self._response_parser = response_parser or _parse_choice_answer

    def choose(self, kind: str, state: object) -> RouteDecision:
        if kind not in ROUTE_KINDS:
            raise ValueError(f"Unknown route kind: {kind}")
        candidates = [route for route in self.config.routes if route.model_for(kind)]
        if len(candidates) <= 1:
            return self.fallback.choose(kind, state)
        criteria = {route.id: route.criteria for route in candidates}
        text = _state_text(state)
        likely = {
            "reply": "reply model",
            "vision": "screen vision model",
        }[kind]
        body = self._request_builder(
            self.config.model,
            f"Which {likely} should handle this turn?",
            text,
            criteria,
        )
        headers = {"content-type": "application/json"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = client.post(self.base_url, json=body, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            return _fallback(self.fallback, kind, state, "judge_unavailable")
        if not isinstance(payload, Mapping):
            return _fallback(self.fallback, kind, state, "judge_malformed")
        answer = self._response_parser(payload, criteria)
        if answer is None:
            return _fallback(self.fallback, kind, state, "judge_malformed")
        route_id, confidence = answer
        route = self.config.route(route_id)
        if route is None or not route.model_for(kind):
            return _fallback(self.fallback, kind, state, "judge_unknown_route")
        if confidence is not None and confidence < self.config.min_confidence:
            return _fallback(self.fallback, kind, state, "judge_low_confidence")
        return RouteDecision(
            route_id=route.id,
            reply_model=route.reply_model,
            vision_model=route.vision_model,
            reason="judge_choice",
            confidence=confidence,
            judge_model=self.config.model,
        )


def _fallback(fallback: ModelRouter, kind: str, state: object, reason: str) -> RouteDecision:
    decision = fallback.choose(kind, state)
    return RouteDecision(
        route_id=decision.route_id,
        reply_model=decision.reply_model,
        vision_model=decision.vision_model,
        reason=reason,
        confidence=decision.confidence,
        judge_model=decision.judge_model,
    )


def _state_text(state: object) -> str:
    if isinstance(state, str):
        text = state
    else:
        try:
            text = json.dumps(
                state, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str
            )
        except (TypeError, ValueError, RecursionError):
            text = str(state)
    return text[:MAX_STATE_CHARS]


def _jev_choice_request(
    model: str, instructions: str, text: str, criteria: Mapping[str, str]
) -> dict:
    return {
        "model": model,
        "state": {"text": text},
        "questions": {
            "route": {
                "type": "choice",
                "instructions": instructions,
                "criteria": dict(criteria),
            }
        },
    }


def _parse_choice_answer(
    payload: Mapping[str, object], criteria: Mapping[str, str]
) -> tuple[str, float | None] | None:
    answers = payload.get("answers")
    answer: Mapping[str, object] | None = None
    if isinstance(answers, Mapping):
        candidate = answers.get("route")
        if isinstance(candidate, Mapping):
            answer = candidate
    elif isinstance(answers, Sequence) and not isinstance(answers, (str, bytes)):
        for item in answers:
            if isinstance(item, Mapping) and item.get("id") == "route":
                answer = item
                break
    if answer is None:
        return None
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in criteria:
        return None
    confidence: float | None = None
    raw = answer.get("confidence")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        confidence = float(raw)
    else:
        probabilities = answer.get("probabilities")
        if isinstance(probabilities, Mapping):
            value = probabilities.get(choice)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                confidence = float(value)
    return choice, confidence


def build_model_router(
    config: RoutingConfig | None, *, transport: httpx.BaseTransport | None = None
) -> ModelRouter:
    """Build the configured router. Callers always receive a usable object."""

    config = config or RoutingConfig()
    static = StaticRouter(config)
    if not config.active:
        return static
    if config.gate == "deterministic":
        return DeterministicRouter(config)
    if config.gate in ("decision", "jev") and config.provider_configured:
        return HttpChoiceRouter(config, transport=transport, fallback=static)
    return static


def routing_capabilities(config: RoutingConfig) -> dict:
    """Public, secret-free routing status for ``/v1/capabilities``."""

    return {
        "enabled": config.enabled,
        "gate": config.gate,
        "judge_configured": config.provider_configured,
        "default_route": config.default_route,
        "routes": [
            {
                "id": route.id,
                "reply_model": route.reply_model,
                "vision_model": route.vision_model,
            }
            for route in config.routes
        ],
    }


def _optional_string(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("route fields must be strings")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"route strings must not exceed {limit} characters")
    return value


def _route(value: object) -> Route:
    if not isinstance(value, Mapping):
        raise ValueError("each route must be an object")
    unknown = set(value) - {"id", "criteria", "reply_model", "vision_model", "keywords"}
    if unknown:
        raise ValueError(f"unknown route field: {sorted(unknown)[0]}")
    route_id = _optional_string(value.get("id", ""), MAX_ROUTE_ID)
    if not route_id:
        raise ValueError("route id must not be empty")
    if not route_id.replace("-", "").replace("_", "").isalnum() or not route_id.isascii():
        raise ValueError("route id must contain only ASCII letters, digits, '-' and '_'")
    criteria = _optional_string(value.get("criteria", ""), MAX_CRITERIA_CHARS)
    if not criteria:
        raise ValueError("route criteria must not be empty")
    reply_model = _optional_string(value.get("reply_model", ""), MAX_MODEL_CHARS)
    vision_model = _optional_string(value.get("vision_model", ""), MAX_MODEL_CHARS)
    raw_keywords = value.get("keywords", ())
    if isinstance(raw_keywords, str) or not isinstance(raw_keywords, Sequence):
        raise ValueError("route keywords must be an array of strings")
    if len(raw_keywords) > MAX_KEYWORDS:
        raise ValueError(f"route keywords must contain at most {MAX_KEYWORDS} entries")
    keywords = tuple(
        keyword
        for keyword in (_optional_string(item, MAX_KEYWORD_CHARS) for item in raw_keywords)
        if keyword
    )
    return Route(
        id=route_id,
        criteria=criteria,
        reply_model=reply_model,
        vision_model=vision_model,
        keywords=keywords,
    )


def routing_config(value: object) -> RoutingConfig:
    """Validate a Settings field, including routes parsed from TOML or JSON."""

    if value is None or value == {}:
        return RoutingConfig()
    if isinstance(value, RoutingConfig):
        config = value
    elif isinstance(value, Mapping):
        unknown = set(value) - set(RoutingConfig.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown configuration field: model_routing.{sorted(unknown)[0]}")
        raw_routes = value.get("routes", ())
        if isinstance(raw_routes, str):
            try:
                raw_routes = json.loads(raw_routes)
            except ValueError:
                raise ValueError("model_routing.routes must be valid JSON") from None
        if not isinstance(raw_routes, Sequence) or isinstance(raw_routes, (str, bytes)):
            raise ValueError("model_routing.routes must be an array of route objects")
        routes = tuple(_route(item) for item in raw_routes)
        config = RoutingConfig(**{**value, "routes": routes})
    else:
        raise ValueError("model_routing must be a table")
    if type(config.enabled) is not bool:
        raise ValueError("model_routing.enabled must be a boolean")
    if config.gate not in GATE_MODES:
        raise ValueError(f"model_routing.gate must be one of {list(GATE_MODES)}")
    for name in ("base_url", "api_key", "model", "default_route"):
        if not isinstance(getattr(config, name), str):
            raise ValueError(f"model_routing.{name} must be a string")
    if isinstance(config.timeout_seconds, bool) or not (
        MIN_TIMEOUT_SECONDS <= float(config.timeout_seconds) <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"model_routing.timeout_seconds must be between {MIN_TIMEOUT_SECONDS} "
            f"and {MAX_TIMEOUT_SECONDS}"
        )
    if isinstance(config.min_confidence, bool) or not (0 <= float(config.min_confidence) <= 1):
        raise ValueError("model_routing.min_confidence must be between 0 and 1")
    if not 0 <= len(config.routes) <= MAX_ROUTES:
        raise ValueError(f"model_routing.routes must contain at most {MAX_ROUTES} routes")
    ids = [route.id for route in config.routes]
    if len(ids) != len(set(ids)):
        raise ValueError("model_routing route ids must be unique")
    if config.enabled and not config.routes:
        raise ValueError("Enabled model_routing requires at least one route")
    if config.routes and config.default_route not in ids:
        raise ValueError("model_routing.default_route must name a configured route")
    if config.gate in ("decision", "jev") and config.enabled and not config.provider_configured:
        raise ValueError(
            "Enabled model_routing with decision/jev gate requires model_routing.base_url"
        )
    if not config.model.strip() or len(config.model) > MAX_MODEL_CHARS:
        raise ValueError("model_routing.model must contain 1 to 256 characters")
    return RoutingConfig(
        enabled=config.enabled,
        gate=config.gate,
        base_url=config.base_url.strip().rstrip("/"),
        api_key=config.api_key,
        model=config.model.strip(),
        timeout_seconds=float(config.timeout_seconds),
        min_confidence=float(config.min_confidence),
        default_route=config.default_route,
        routes=config.routes,
    )
