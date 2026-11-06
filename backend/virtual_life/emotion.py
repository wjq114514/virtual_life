"""Evidence-backed, time-decayed affect. Reads and previews never add stimuli."""

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .emotion_appraisal import AppraisalVector, _project, appraise
from .memory import MemoryError
from .model_provider import estimate_chat_input_tokens
from .models import Identifier, RequestModel, Version
from .personality import PersonalityError, PersonalityState, _json, _timestamp
from .ports import ModelRequest
from .responses import MemorySourceVersion

_LOGGER = logging.getLogger(__name__)


class EmotionPolicy(RequestModel):
    enabled: bool = True
    half_life_seconds: int = Field(default=1200, ge=60, le=7200)
    valence_half_life_seconds: int | None = Field(default=None, ge=60, le=7200)
    arousal_half_life_seconds: int | None = Field(default=None, ge=60, le=7200)
    control_half_life_seconds: int | None = Field(default=None, ge=60, le=7200)
    resource_half_life_seconds: int | None = Field(default=None, ge=60, le=7200)
    # Negative emotions (concern/sympathy/annoyance) decay slower than positive
    # ones, reflecting negativity bias (Baumeister 2001) and the asymmetric
    # decay used by WASABI (Becker-Asano 2008). When set, these reactions use
    # this half-life instead of the shared base; per-dimension overrides still
    # win when explicitly configured.
    negative_half_life_seconds: int | None = Field(default=None, ge=60, le=7200)
    candidate_limit: int = Field(default=256, ge=1, le=2048)
    resonance_strength: float = Field(default=0.2, ge=0, le=0.3)
    # Model-assisted appraisal: when enabled AND a provider is attached, the
    # appraise() entry point is wrapped by EmotionService.observe to try the
    # model first (strict JSON, bounded by the timeout below) and fall back
    # to the local rules on failure, timeout, or invalid output. The appraise
    # function itself stays pure and rule-based.
    appraise_model_enabled: bool = False
    # Optional per-purpose model override for appraisal; empty keeps the shared [model].
    appraise_model_name: str = Field(default="", max_length=256)
    appraise_model_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    appraise_model_output_tokens: int = Field(default=200, ge=1, le=1_000)


class Affect(RequestModel):
    valence: float = Field(default=0.1, ge=-1, le=1)
    arousal: float = Field(default=0.25, ge=0, le=1)
    control: float = Field(default=0.6, ge=0, le=1)


class AffectDelta(RequestModel):
    valence: float = Field(default=0, ge=-0.3, le=0.3)
    arousal: float = Field(default=0, ge=-0.3, le=0.3)
    control: float = Field(default=0, ge=-0.3, le=0.3)
    energy: float = Field(default=0, ge=-0.3, le=0.3)
    social_battery: float = Field(default=0, ge=-0.3, le=0.3)


Reaction = Literal[
    "neutral",
    "joy",
    "concern",
    "curiosity",
    "quiet",
    "amusement",
    "excitement",
    "surprise",
    "sympathy",
    "annoyance",
    "pride",
]
Reason = Literal[
    "no_explicit_cue",
    "explicit_appreciation",
    "explicit_distress",
    "explicit_question",
    "quiet_request",
    "model_v1",
    "proactive_replied",
    "proactive_unanswered",
    "loneliness_idle",
    "loneliness_neglect",
]

# Reactions whose experiences decay more slowly when negative_half_life_seconds
# is configured. Concern and sympathy are empathetic-distress reactions; annoyance
# is irritability. Together these capture the negativity-bias asymmetry.
_NEGATIVE_REACTIONS = frozenset({"concern", "sympathy", "annoyance"})


class RecoveryHalfLives(RequestModel):
    valence: int
    arousal: int
    control: int
    energy: int
    social_battery: int


class EmotionCorrectionInfo(RequestModel):
    kind: Literal["direction", "intensity", "attribution"]
    retained_weight: float | None = None
    attribution: Literal["self", "other", "situation", "unspecified"] | None = None
    revision: int
    corrected_at: str


class EmotionSnapshot(RequestModel):
    companion_id: Identifier
    revision: Version
    enabled: bool
    state: Affect
    baseline: Affect
    effective_state: PersonalityState
    reaction: Reaction
    quiet_requested: bool = False
    context_version: str
    source_event_id: Identifier | None = None
    evaluated_at: str
    updated_at: str | None = None
    active_experiences: int = Field(ge=0)
    truncated: bool
    half_life_seconds: int
    recovery_half_lives: RecoveryHalfLives | None = None
    model_calls: Literal[0] = 0


class EmotionExperience(RequestModel):
    event_id: Identifier
    completed_event_id: Identifier | None = None
    companion_id: Identifier
    reaction: Reaction
    reason: Reason
    confidence: float = Field(ge=0, le=1)
    delta: AffectDelta
    resonance_delta: AffectDelta
    memory_versions: list[MemorySourceVersion]
    resonance_event_ids: list[Identifier]
    appraisal: AppraisalVector | None = None
    appraisal_version: str = "legacy_v1"
    uncertainty: float = Field(default=1, ge=0, le=1)
    attribution: Literal["self", "other", "situation", "unspecified"] = "unspecified"
    original_delta: AffectDelta | None = None
    correction: EmotionCorrectionInfo | None = None
    status: Literal["active", "corrected", "invalidated", "forgotten"]
    resonance_valid: bool
    created_at: str


class EmotionExperiencePage(RequestModel):
    items: list[EmotionExperience]
    next_sequence: int
    has_more: bool


class EmotionInvalidation(RequestModel):
    command_id: Identifier
    expected_revision: Version


class EmotionCorrection(EmotionInvalidation):
    kind: Literal["direction", "intensity", "attribution"]
    retained_weight: float | None = Field(default=None, ge=0, le=1)
    attribution: Literal["self", "other", "situation", "unspecified"] | None = None

    @model_validator(mode="after")
    def applicable_fields(self):
        if self.kind == "intensity":
            if "attribution" in self.model_fields_set:
                raise ValueError("attribution is only applicable to attribution corrections")
            if self.retained_weight is None:
                if "retained_weight" in self.model_fields_set:
                    raise ValueError("retained_weight must be a number")
                self.retained_weight = 0.5
        elif "retained_weight" in self.model_fields_set:
            raise ValueError("retained_weight is only applicable to intensity corrections")
        if self.kind == "attribution":
            if self.attribution is None:
                raise ValueError("attribution is required")
        elif "attribution" in self.model_fields_set:
            raise ValueError("attribution is only applicable to attribution corrections")
        return self


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS emotion_states (
        companion_id TEXT PRIMARY KEY REFERENCES companions(companion_id),
        revision INTEGER NOT NULL, source_event_id TEXT REFERENCES events(event_id),
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS emotion_experiences (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE NOT NULL REFERENCES events(event_id),
        companion_id TEXT NOT NULL REFERENCES companions(companion_id),
        completed_event_id TEXT UNIQUE REFERENCES events(event_id),
        appraisal_json TEXT NOT NULL, resonance_json TEXT,
        invalidated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS emotion_corrections (
        event_id TEXT PRIMARY KEY REFERENCES emotion_experiences(event_id),
        weights_json TEXT NOT NULL, info_json TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS emotion_recent ON emotion_experiences(companion_id, created_at DESC, sequence DESC)",
    "CREATE INDEX IF NOT EXISTS emotion_window ON emotion_experiences(companion_id, sequence DESC)",
)


class EmotionService:
    def __init__(self, personality, memory, *, policy=None):
        self.personality = personality
        self.memory = memory
        self.policy = policy or EmotionPolicy()
        self.provider = None
        self.model_name = ""
        self.store = personality.store
        with self.store.transaction(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def attach_model(self, provider, model_name: str) -> None:
        """Attach the shared model channel used for appraise_model_enabled."""
        self.provider = provider
        self.model_name = model_name or ""

    @staticmethod
    def _row(connection, companion_id):
        return connection.execute(
            "SELECT * FROM emotion_states WHERE companion_id=?", (companion_id,)
        ).fetchone()

    def _bump(self, connection, companion_id, event_id, now):
        row = self._row(connection, companion_id)
        revision = self.personality._check_revision(
            row,
            row["revision"] if row else 0,
            "emotion",
        )
        connection.execute(
            """INSERT INTO emotion_states VALUES (?, ?, ?, ?)
            ON CONFLICT(companion_id) DO UPDATE SET revision=excluded.revision,
            source_event_id=excluded.source_event_id, updated_at=excluded.updated_at""",
            (companion_id, revision, event_id, _timestamp(now)),
        )
        self.personality._emit(
            connection,
            companion_id,
            now,
            "companion.emotion_updated",
            {"emotion_revision": revision, "source_event_id": event_id},
        )

    def observe(self, connection, event, profile, now):
        if not self.policy.enabled:
            return
        if event["type"] != "companion.dialogue_received":
            raise ValueError("Affect requires an accepted user event")
        if connection.execute(
            "SELECT 1 FROM emotion_experiences WHERE event_id=?", (event["event_id"],)
        ).fetchone():
            return
        appraisal = self._appraise(event, profile, now, connection=connection)
        connection.execute(
            """INSERT INTO emotion_experiences
            (event_id, companion_id, appraisal_json, created_at) VALUES (?, ?, ?, ?)""",
            (event["event_id"], event["companion_id"], _json(appraisal), _timestamp(now)),
        )
        self._bump(connection, event["companion_id"], event["event_id"], now)

    def proactive_feedback(self, connection, companion_id, event_id, now, *, replied):
        """Record affect for whether the user answered a proactive message.

        Reaching out is itself an experience with an outcome: a reply is a
        small joy attributed to the other person, prolonged silence a mild dip
        attributed to the situation. ``event_id`` is the companion's
        ``companion.proactive_feedback`` event, so command replays and the
        timeout scan cannot double-charge the same outreach.
        """
        if not self.policy.enabled:
            return
        if connection.execute(
            "SELECT 1 FROM emotion_experiences WHERE event_id=?", (event_id,)
        ).fetchone():
            return
        if replied:
            reaction, reason, attribution = "joy", "proactive_replied", "other"
            delta = {
                "valence": 0.25,
                "arousal": 0.3,
                "control": 0.05,
                "energy": 0.0,
                "social_battery": 0.02,
            }
        else:
            reaction, reason, attribution = "concern", "proactive_unanswered", "situation"
            delta = {
                "valence": -0.12,
                "arousal": 0.08,
                "control": 0.0,
                "energy": 0.0,
                "social_battery": -0.01,
            }
        appraisal = {
            "reaction": reaction,
            "reason": reason,
            "confidence": 0.85,
            "delta": delta,
            "appraisal": {"uncertainty": 0.1},
            "appraisal_version": "proactive_feedback_v1",
            "uncertainty": 0.1,
            "attribution": attribution,
        }
        connection.execute(
            """INSERT INTO emotion_experiences
            (event_id, companion_id, appraisal_json, created_at) VALUES (?, ?, ?, ?)""",
            (event_id, companion_id, _json(appraisal), _timestamp(now)),
        )
        self._bump(connection, companion_id, event_id, now)

    def loneliness(self, connection, companion_id, event_id, now, *, level):
        """Record the felt cost of a long silence from the user.

        A day of quiet is a mild dip ("she's just busy"); three days reads as
        being neglected. ``event_id`` is the companion's ``companion.loneliness``
        event, so the poll that scans idle spans cannot double-charge the same
        stretch, and any new user dialogue naturally starts a fresh span.
        """
        if not self.policy.enabled:
            return
        if connection.execute(
            "SELECT 1 FROM emotion_experiences WHERE event_id=?", (event_id,)
        ).fetchone():
            return
        if level == "neglect":
            reaction, reason, attribution = "concern", "loneliness_neglect", "other"
            delta = {
                "valence": -0.20,
                "arousal": 0.10,
                "control": 0.0,
                "energy": -0.02,
                "social_battery": -0.05,
            }
        else:
            reaction, reason, attribution = "quiet", "loneliness_idle", "situation"
            delta = {
                "valence": -0.08,
                "arousal": 0.0,
                "control": 0.0,
                "energy": -0.01,
                "social_battery": -0.02,
            }
        appraisal = {
            "reaction": reaction,
            "reason": reason,
            "confidence": 0.8,
            "delta": delta,
            "appraisal": {"uncertainty": 0.15},
            "appraisal_version": "loneliness_v1",
            "uncertainty": 0.15,
            "attribution": attribution,
        }
        connection.execute(
            """INSERT INTO emotion_experiences
            (event_id, companion_id, appraisal_json, created_at) VALUES (?, ?, ?, ?)""",
            (event_id, companion_id, _json(appraisal), _timestamp(now)),
        )
        self._bump(connection, companion_id, event_id, now)

    _EMOTION_MODEL_PURPOSE = "emotion_appraise"
    _REACTIONS = (
        "neutral",
        "joy",
        "concern",
        "curiosity",
        "quiet",
        "amusement",
        "excitement",
        "surprise",
        "sympathy",
        "annoyance",
        "pride",
    )
    _ATTRIBUTIONS = ("self", "other", "situation", "unspecified")

    def _appraise(self, event, profile, now, *, connection=None):
        """Local rules first, model refinement when enabled and attached.

        The model path returns None on any failure (quota, timeout, invalid
        JSON) and observe then uses the rule result, so emotion never breaks
        a dialogue turn because the model channel hiccuped.
        """
        local = appraise(event["payload"]["user_message"], profile["definition"]["traits"])
        if not self.policy.appraise_model_enabled:
            return local
        if self.provider is None or not self.model_name:
            _LOGGER.info(
                "emotion appraise model gate: provider=%s model_name=%r",
                self.provider,
                self.model_name,
            )
            return local
        refined = None
        try:
            refined = self._appraise_with_model(event, profile, connection=connection)
        except Exception as exc:  # noqa: BLE001 - diagnose silent model fallbacks
            _LOGGER.warning("emotion appraise model failed: %r", exc)
            refined = None
        if refined is None:
            _LOGGER.info(
                "emotion appraise model skipped/failed; using local rules (provider=%s model=%s)",
                self.provider is not None,
                self.model_name,
            )
        return refined if refined is not None else local

    def _appraise_with_model(self, event, profile, *, connection=None):
        message = event["payload"]["user_message"]
        traits = profile["definition"]["traits"]
        prompt = (
            "You appraise the emotional reaction of a companion to ONE user "
            "message. Judge only from the message text and the traits.\n"
            f"traits: {json.dumps(traits, ensure_ascii=False, default=str)}\n"
            f"user message: {message!r}\n"
            f"reaction must be one of: {', '.join(self._REACTIONS)}\n"
            "appraisal fields (each -1..1): pleasantness, effort, attention, "
            "agency, uncertainty, control\n"
            "Return STRICT JSON only, no markdown fence, no extra keys:\n"
            '{"reaction": "...", "confidence": 0.0, "uncertainty": 0.0, '
            '"attribution": "self|other|situation|unspecified", "appraisal": '
            '{"pleasantness": 0.0, "effort": 0.0, "attention": 0.0, '
            '"agency": 0.0, "uncertainty": 0.0, "control": 0.0}, '
            '"rationale": "one short sentence"}'
        )
        companion_id = event["companion_id"]
        task_id = "emo-" + event["event_id"]
        estimated = estimate_chat_input_tokens(
            self.memory.token_estimator, prompt, purpose=self._EMOTION_MODEL_PURPOSE
        )
        try:
            hold = self.memory.reserve_model_tokens(
                companion_id,
                {
                    "task_id": task_id,
                    "purpose": self._EMOTION_MODEL_PURPOSE,
                    "input_tokens": estimated,
                    "output_tokens": self.policy.appraise_model_output_tokens,
                },
                **({"via": connection} if connection is not None else {}),
            )
        except MemoryError:
            return None
        if not hold["new_reservation"]:
            return None
        request = ModelRequest(
            task_id=task_id,
            companion_id=companion_id,
            purpose=self._EMOTION_MODEL_PURPOSE,
            model=self.model_name,
            context_text=prompt,
            input_token_limit=estimated,
            output_token_limit=self.policy.appraise_model_output_tokens,
        )
        answer = None
        try:
            # Bounded wait: the observe call sits on the dialogue accept path,
            # so a slow provider must not stall the turn. The worker thread is
            # abandoned (not joined) on timeout; it settles or releases on its
            # own completion path via the usage handling below only when it
            # returns in time — otherwise the reservation is released here.
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(self.provider.complete, request)
                answer = future.result(timeout=self.policy.appraise_model_timeout_seconds)
            finally:
                executor.shutdown(wait=False)
        except Exception:
            self.memory.release_model_tokens(
                companion_id, task_id, **({"via": connection} if connection is not None else {})
            )
            return None
        usage = getattr(answer, "usage", None)
        if (
            usage is None
            or not isinstance(usage.input_tokens, int)
            or not isinstance(usage.output_tokens, int)
        ):
            self.memory.release_model_tokens(
                companion_id, task_id, **({"via": connection} if connection is not None else {})
            )
            return None
        self.memory.settle_model_tokens(
            companion_id,
            task_id,
            {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            **({"via": connection} if connection is not None else {}),
        )
        return self._parse_model_appraisal(answer.text, traits)

    @staticmethod
    def _parse_model_payload(text):
        payload = text.strip()
        if payload.startswith("```"):
            payload = payload.strip("`")
        try:
            return json.loads(payload)
        except ValueError:
            start, end = payload.find("{"), payload.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                return json.loads(payload[start : end + 1])
            except ValueError:
                return None

    def _parse_model_appraisal(self, text, traits):
        if not text:
            return None
        parsed = self._parse_model_payload(text)
        if parsed is None:
            _LOGGER.info(
                "emotion appraise model output unparseable: %r",
                text[:200],
            )
            return None
        if not isinstance(parsed, dict):
            return None
        reaction = parsed.get("reaction")
        if reaction not in self._REACTIONS:
            _LOGGER.info(
                "emotion appraise model reaction %r not in enum",
                reaction,
            )
            return None
        appraisal = parsed.get("appraisal")
        if not isinstance(appraisal, dict):
            return None
        try:
            vector = AppraisalVector.model_validate(appraisal)
        except ValidationError:
            _LOGGER.info(
                "emotion appraise model appraisal fields invalid: %r",
                appraisal,
            )
            return None
        confidence = parsed.get("confidence", 0.5)
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.5
        uncertainty = parsed.get("uncertainty", vector.uncertainty)
        try:
            uncertainty = min(1.0, max(0.0, float(uncertainty)))
        except (TypeError, ValueError):
            uncertainty = vector.uncertainty
        attribution = parsed.get("attribution", "unspecified")
        if attribution not in self._ATTRIBUTIONS:
            attribution = "unspecified"
        return {
            "reaction": reaction,
            "reason": "model_v1",
            "confidence": confidence,
            "delta": _project(vector, reaction, traits),
            "appraisal": vector.model_dump(),
            "appraisal_version": "model_v1",
            "uncertainty": uncertainty,
            "attribution": attribution,
        }

    @staticmethod
    def _forgotten(connection, companion_id, event_id):
        return (
            event_id is not None
            and connection.execute(
                "SELECT 1 FROM memory_source_tombstones WHERE companion_id=? AND source_event_id=?",
                (companion_id, event_id),
            ).fetchone()
            is not None
        )

    def _base_live(self, connection, row):
        return not row["invalidated"] and not self._forgotten(
            connection, row["companion_id"], row["completed_event_id"]
        )

    @staticmethod
    def _correction(connection, row):
        record = connection.execute(
            "SELECT * FROM emotion_corrections WHERE event_id=?", (row["event_id"],)
        ).fetchone()
        weights = {key: 1.0 for key in ("valence", "arousal", "control", "vector", "agency")}
        weights["attribution"] = None
        weights["neutral"] = False
        if record is None:
            return weights, None
        return json.loads(record["weights_json"]), json.loads(record["info_json"])

    def _appraisal(self, connection, row):
        original = json.loads(row["appraisal_json"])
        result = dict(original)
        result.setdefault("appraisal", None)
        result.setdefault("appraisal_version", "legacy_v1")
        result.setdefault("uncertainty", 1.0)
        result.setdefault("attribution", "unspecified")
        weights, correction = self._correction(connection, row)
        result["delta"] = dict(original["delta"])
        for key in ("valence", "arousal", "control"):
            result["delta"][key] *= weights[key]
        result["confidence"] *= weights["vector"]
        result["uncertainty"] = 1 - (1 - result["uncertainty"]) * weights["vector"]
        if weights["neutral"]:
            result["reaction"] = "neutral"
        if weights["attribution"] is not None:
            result["attribution"] = weights["attribution"]
        if result["appraisal"] is not None:
            vector = dict(result["appraisal"])
            for key, value in vector.items():
                neutral = 0.5 if key == "control" else 1.0 if key == "uncertainty" else 0.0
                weight = weights["agency"] if key in ("control", "agency") else weights["vector"]
                vector[key] = neutral + (value - neutral) * weight
            result["appraisal"] = vector
        return result, original["delta"], correction, weights

    def _half_lives(self):
        return {
            key: getattr(self.policy, f"{dimension}_half_life_seconds")
            or self.policy.half_life_seconds
            for key, dimension in (
                ("valence", "valence"),
                ("arousal", "arousal"),
                ("control", "control"),
                ("energy", "resource"),
                ("social_battery", "resource"),
            )
        }

    def _resonance_live(self, connection, row, resonance, now):
        if not resonance or not resonance["event_ids"] or not self._base_live(connection, row):
            return False
        try:
            self.memory.validate_references(
                connection, row["companion_id"], resonance["memory_versions"], now
            )
        except MemoryError:
            return False
        for event_id in resonance["event_ids"]:
            source = connection.execute(
                "SELECT * FROM emotion_experiences WHERE event_id=? AND companion_id=?",
                (event_id, row["companion_id"]),
            ).fetchone()
            if source is None or not self._base_live(connection, source):
                return False
            _, correction = self._correction(connection, source)
            revision = correction["revision"] if correction else 0
            if resonance.get("correction_revisions", {}).get(event_id, 0) != revision:
                return False
        return True

    def resonate(self, connection, companion_id, event_id, references, query, now):
        if not self.policy.enabled or self.policy.resonance_strength == 0:
            return
        row = connection.execute(
            "SELECT * FROM emotion_experiences WHERE event_id=? AND companion_id=?",
            (event_id, companion_id),
        ).fetchone()
        # NULL means not evaluated; even an empty evaluation is durable, so retry
        # and repeated recall cannot re-stimulate the same event.
        if row is None or row["resonance_json"] is not None or not self._base_live(connection, row):
            return
        self.memory.validate_references(connection, companion_id, references, now)
        sources, versions, correction_revisions = {}, [], {}
        for reference in references:
            memory = connection.execute(
                "SELECT * FROM memory_records WHERE memory_id=? AND companion_id=?",
                (reference["memory_id"], companion_id),
            ).fetchone()
            if memory is None or memory["kind"] != "episode":
                continue
            coverage, phrase = self.memory._query_relevance(memory, query)
            if not phrase and coverage < max(
                0.1, self.memory.policy.decay.reinforcement_min_coverage
            ):
                continue
            candidates = connection.execute(
                """SELECT e.* FROM emotion_experiences e
                JOIN memory_provenance p ON p.source_event_id=e.completed_event_id
                WHERE p.memory_id=? AND e.companion_id=? AND e.event_id!=?
                ORDER BY e.sequence DESC LIMIT 16""",
                (reference["memory_id"], companion_id, event_id),
            ).fetchall()
            used = False
            for candidate in candidates:
                if len(sources) >= 16 and candidate["event_id"] not in sources:
                    continue
                if self._base_live(connection, candidate):
                    appraisal, _, correction, _ = self._appraisal(connection, candidate)
                    if appraisal["confidence"] > 0:
                        sources[candidate["event_id"]] = appraisal["delta"]
                        correction_revisions[candidate["event_id"]] = (
                            correction["revision"] if correction else 0
                        )
                        used = True
            if used:
                versions.append(reference)
        delta = AffectDelta().model_dump()
        if sources:
            for key in ("valence", "arousal", "control"):
                delta[key] = (
                    self.policy.resonance_strength
                    * sum(s[key] for s in sources.values())
                    / len(sources)
                )
        resonance = {
            "delta": delta,
            "memory_versions": versions,
            "event_ids": list(sources),
            "correction_revisions": correction_revisions,
        }
        connection.execute(
            "UPDATE emotion_experiences SET resonance_json=? WHERE event_id=?",
            (_json(resonance), event_id),
        )
        if sources:
            self._bump(connection, companion_id, event_id, now)

    def complete(self, connection, event_id, completed_event_id, reply, now):
        row = connection.execute(
            "SELECT * FROM emotion_experiences WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None or row["completed_event_id"] is not None:
            return
        appraisal = json.loads(row["appraisal_json"])
        appraisal["delta"]["energy"] = -min(0.05, len(reply) / 20000)
        appraisal["delta"]["social_battery"] = -min(0.03, len(reply) / 20000)
        connection.execute(
            "UPDATE emotion_experiences SET completed_event_id=? WHERE event_id=?",
            (completed_event_id, event_id),
        )
        connection.execute(
            "UPDATE emotion_experiences SET appraisal_json=? WHERE event_id=?",
            (_json(appraisal), event_id),
        )
        self._bump(connection, row["companion_id"], event_id, now)

    def snapshot(self, connection, companion_id, profile, legacy, now):
        traits = profile["definition"]["traits"]
        baseline = Affect(
            valence=round(0.2 * traits["warmth"] - 0.04, 6),
            arousal=round(0.18 + 0.1 * traits["curiosity"], 6),
            control=traits["assertiveness"],
        ).model_dump()
        total = {
            **baseline,
            "energy": legacy["state"]["energy"],
            "social_battery": legacy["state"]["social_battery"],
        }
        state_row = self._row(connection, companion_id)
        # The most recent bounded ledger window has stable membership between
        # writes. Time recovery never changes its evidence fingerprint.
        rows = (
            connection.execute(
                """SELECT * FROM emotion_experiences
                    WHERE companion_id=? ORDER BY sequence DESC LIMIT ?""",
                (
                    companion_id,
                    self.policy.candidate_limit + 1,
                ),
            ).fetchall()
            if self.policy.enabled
            else []
        )
        truncated = len(rows) > self.policy.candidate_limit
        rows = rows[: self.policy.candidate_limit]
        validity, active, reaction = [], 0, "neutral"
        quiet = False
        half_lives = self._half_lives()
        # Negative reactions persist longer (negativity bias). Per-dimension
        # overrides, when explicitly configured, still win over the reaction
        # adjustment; only otherwise-defaulted dimensions are slowed.
        negative = self.policy.negative_half_life_seconds
        explicit = {
            key: getattr(self.policy, f"{dimension}_half_life_seconds") is not None
            for key, dimension in (
                ("valence", "valence"),
                ("arousal", "arousal"),
                ("control", "control"),
                ("energy", "resource"),
                ("social_battery", "resource"),
            )
        }
        for row in rows:
            live = self._base_live(connection, row)
            resonance = json.loads(row["resonance_json"]) if row["resonance_json"] else None
            resonance_live = self._resonance_live(connection, row, resonance, now)
            appraisal, _, correction, weights = self._appraisal(connection, row)
            validity.append(
                (row["event_id"], live, resonance_live, correction["revision"] if correction else 0)
            )
            if not live:
                continue
            elapsed = max(0, (now - datetime.fromisoformat(row["created_at"])).total_seconds())
            # After 24 half-lives the omitted impulse is below 6e-8 of its
            # original size; historical experience remains queryable.
            if elapsed > max(half_lives.values()) * 24:
                continue
            active += 1
            slow = (
                negative
                if negative is not None and appraisal["reaction"] in _NEGATIVE_REACTIONS
                else None
            )
            factors = {
                key: 2
                ** (-elapsed / (negative if slow is not None and not explicit[key] else value))
                for key, value in half_lives.items()
            }
            factor = 2 ** (-elapsed / self.policy.half_life_seconds)
            for key in total:
                total[key] += factors[key] * (
                    appraisal["delta"][key]
                    + (resonance["delta"][key] * weights.get(key, 1) if resonance_live else 0)
                )
            strength = max(factors[key] * abs(appraisal["delta"][key]) for key in baseline)
            if reaction == "neutral" and strength > 0.025 and appraisal["reaction"] != "neutral":
                reaction = appraisal["reaction"]
            if (
                reaction == "neutral"
                and resonance_live
                and factors["valence"] * abs(resonance["delta"]["valence"] * weights["valence"])
                > 0.025
            ):
                reaction = "joy" if resonance["delta"]["valence"] > 0 else "concern"
            if appraisal["reaction"] == "quiet" and factor > 0.5:
                quiet = True
        for key in total:
            total[key] = round(max(-1 if key == "valence" else 0, min(1, total[key])), 6)
        effective = dict(legacy["state"])
        # Explicit legacy state retains its old TTL semantics. Automatic affect
        # controls mood only when no unexpired manual state is present.
        if legacy["recorded_state"] is None or legacy["expired"]:
            effective["mood"] = {
                "joy": "cheerful",
                "concern": "concerned",
                "curiosity": "curious",
                "amusement": "amused",
                "excitement": "excited",
                "surprise": "surprised",
                "sympathy": "sympathetic",
                "annoyance": "annoyed",
                "pride": "proud",
            }.get(reaction, "calm")
        effective.update(energy=total["energy"], social_battery=total["social_battery"])
        if reaction == "quiet":
            effective["mood"] = "calm"
        context_version = hashlib.sha256(
            _json(
                {
                    "revision": state_row["revision"] if state_row else 0,
                    "validity": validity,
                    "legacy_revision": legacy["revision"],
                    "legacy_expired": legacy["expired"],
                    "policy": self.policy.model_dump(),
                    "profile_revision": profile["revision"],
                }
            ).encode()
        ).hexdigest()
        return EmotionSnapshot(
            companion_id=companion_id,
            revision=state_row["revision"] if state_row else 0,
            enabled=self.policy.enabled,
            state=Affect(**{key: total[key] for key in baseline}),
            baseline=Affect(**baseline),
            effective_state=PersonalityState(**effective),
            reaction=reaction,
            quiet_requested=quiet,
            context_version=context_version,
            source_event_id=state_row["source_event_id"] if state_row else None,
            evaluated_at=_timestamp(now),
            updated_at=state_row["updated_at"] if state_row else None,
            active_experiences=active,
            truncated=truncated,
            half_life_seconds=self.policy.half_life_seconds,
            recovery_half_lives=RecoveryHalfLives(**half_lives),
        ).model_dump(mode="json")

    def get(self, companion_id):
        companion_id = self.personality._identifier(companion_id)
        with self.store.transaction() as connection:
            self.personality._companion(connection, companion_id)
            now = self.personality._now()
            profile = self.personality._profile_snapshot(
                companion_id, self.personality._profile_row(connection, companion_id)
            )
            legacy = self.personality._state_snapshot(
                companion_id, self.personality._state_row(connection, companion_id), now
            )
            return self.snapshot(connection, companion_id, profile, legacy, now)

    def experiences(self, companion_id, *, after_sequence=0, limit=50):
        companion_id = self.personality._identifier(companion_id)
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(after_sequence) is not int
            or after_sequence < 0
        ):
            raise PersonalityError(422, "validation_error", "Invalid experience page bounds")
        with self.store.transaction() as connection:
            self.personality._companion(connection, companion_id)
            rows = connection.execute(
                """SELECT * FROM emotion_experiences WHERE companion_id=? AND sequence>?
                ORDER BY sequence LIMIT ?""",
                (companion_id, after_sequence, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                appraisal, original_delta, correction, weights = self._appraisal(connection, row)
                resonance = json.loads(row["resonance_json"]) if row["resonance_json"] else {}
                forgotten = self._forgotten(connection, companion_id, row["completed_event_id"])
                # No message excerpts are copied into the affect ledger. Forgotten
                # source events additionally hide appraisal and resonance details.
                items.append(
                    EmotionExperience(
                        event_id=row["event_id"],
                        completed_event_id=row["completed_event_id"],
                        companion_id=companion_id,
                        reaction="neutral" if forgotten else appraisal["reaction"],
                        reason="no_explicit_cue" if forgotten else appraisal["reason"],
                        confidence=0 if forgotten else appraisal["confidence"],
                        delta=AffectDelta(**({} if forgotten else appraisal["delta"])),
                        appraisal=None if forgotten else appraisal["appraisal"],
                        appraisal_version="legacy_v1"
                        if forgotten
                        else appraisal["appraisal_version"],
                        uncertainty=1 if forgotten else appraisal["uncertainty"],
                        attribution="unspecified" if forgotten else appraisal["attribution"],
                        original_delta=None if forgotten else AffectDelta(**original_delta),
                        correction=None if forgotten else correction,
                        resonance_delta=AffectDelta(
                            **(
                                {}
                                if forgotten
                                else {
                                    key: value * weights.get(key, 1)
                                    for key, value in resonance.get("delta", {}).items()
                                }
                            )
                        ),
                        memory_versions=[] if forgotten else resonance.get("memory_versions", []),
                        resonance_event_ids=[] if forgotten else resonance.get("event_ids", []),
                        status="forgotten"
                        if forgotten
                        else "invalidated"
                        if row["invalidated"]
                        else "corrected"
                        if correction
                        else "active",
                        resonance_valid=self._resonance_live(
                            connection, row, resonance, self.personality._now()
                        ),
                        created_at=row["created_at"],
                    ).model_dump(mode="json")
                )
            return {
                "items": items,
                "next_sequence": rows[min(limit, len(rows)) - 1]["sequence"]
                if rows
                else after_sequence,
                "has_more": len(rows) > limit,
            }

    def invalidate(self, companion_id, event_id, payload):
        companion_id = self.personality._identifier(companion_id)
        event_id = self.personality._identifier(event_id)
        data = self.personality._validate(EmotionInvalidation, payload).model_dump(mode="json")

        def action(connection, now, _):
            state = self._row(connection, companion_id)
            self.personality._check_revision(state, data["expected_revision"], "emotion")
            row = connection.execute(
                "SELECT * FROM emotion_experiences WHERE companion_id=? AND event_id=?",
                (companion_id, event_id),
            ).fetchone()
            if row is None:
                raise PersonalityError(404, "not_found", "Emotion experience was not found")
            if not row["invalidated"]:
                connection.execute(
                    "UPDATE emotion_experiences SET invalidated=1 WHERE event_id=?", (event_id,)
                )
                self._bump(connection, companion_id, state["source_event_id"], now)
            profile = self.personality._profile_snapshot(
                companion_id, self.personality._profile_row(connection, companion_id)
            )
            legacy = self.personality._state_snapshot(
                companion_id, self.personality._state_row(connection, companion_id), now
            )
            return self.snapshot(connection, companion_id, profile, legacy, now)

        return self.personality._write(
            companion_id, "emotion.invalidate", {**data, "event_id": event_id}, action
        )

    def correct(self, companion_id, event_id, payload):
        companion_id = self.personality._identifier(companion_id)
        event_id = self.personality._identifier(event_id)
        data = self.personality._validate(EmotionCorrection, payload).model_dump(mode="json")

        def action(connection, now, _):
            state = self._row(connection, companion_id)
            revision = self.personality._check_revision(state, data["expected_revision"], "emotion")
            row = connection.execute(
                "SELECT * FROM emotion_experiences WHERE companion_id=? AND event_id=?",
                (companion_id, event_id),
            ).fetchone()
            if row is None:
                raise PersonalityError(404, "not_found", "Emotion experience was not found")
            if row["invalidated"]:
                raise PersonalityError(409, "experience_invalidated", "Experience is invalidated")
            if self._forgotten(connection, companion_id, row["completed_event_id"]):
                raise PersonalityError(
                    409, "experience_forgotten", "Experience source was forgotten"
                )
            weights, _ = self._correction(connection, row)
            if data["kind"] == "direction":
                for key in ("valence", "arousal", "control", "vector", "agency"):
                    weights[key] = 0.0
                weights["neutral"] = True
            elif data["kind"] == "intensity":
                for key in ("valence", "arousal", "control", "vector", "agency"):
                    weights[key] *= data["retained_weight"]
            else:
                weights["control"] *= 0.5
                weights["agency"] *= 0.5
                weights["attribution"] = data["attribution"]
            info = EmotionCorrectionInfo(
                kind=data["kind"],
                retained_weight=data["retained_weight"],
                attribution=data["attribution"],
                revision=revision,
                corrected_at=_timestamp(now),
            ).model_dump(mode="json")
            connection.execute(
                """INSERT INTO emotion_corrections VALUES (?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET weights_json=excluded.weights_json,
                info_json=excluded.info_json""",
                (event_id, _json(weights), _json(info)),
            )
            self.personality._emit(
                connection,
                companion_id,
                now,
                "companion.emotion_corrected",
                {"source_event_id": event_id, "correction": info, "weights": weights},
            )
            self._bump(connection, companion_id, event_id, now)
            profile = self.personality._profile_snapshot(
                companion_id, self.personality._profile_row(connection, companion_id)
            )
            legacy = self.personality._state_snapshot(
                companion_id, self.personality._state_row(connection, companion_id), now
            )
            return self.snapshot(connection, companion_id, profile, legacy, now)

        return self.personality._write(
            companion_id, "emotion.correct", {**data, "event_id": event_id}, action
        )
