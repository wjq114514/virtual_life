"""Read-only long-term slow variables (L2).

These aggregates describe a companion's slow affective drift and relational
context. They are computed from the existing evidence ledger every time they are
read; nothing here writes a row, changes the stable personality, or calls a
model. L2 only modulates the prompt and participation parameters, and it never
becomes L0 without an owner-accepted proposal.

All weights, floors, and half-lives are engineering parameters, not
psychological measurements.
"""

import hashlib
import math
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field

from .coordinator import _instant, _timestamp
from .models import Identifier, RequestModel
from .personality import _json

_ToneHint = Literal["neutral", "warm", "cautious"]
_INERTIA_SCALE = 0.4
_NEUTRAL_ENGAGEMENT = 0.5


class SlowVariablePolicy(RequestModel):
    enabled: bool = True
    half_life_seconds: int = Field(default=2_592_000, ge=3_600, le=31_536_000)
    window_seconds: int = Field(default=31_536_000, ge=86_400, le=94_608_000)
    candidate_limit: int = Field(default=256, ge=1, le=2_048)
    min_evidence_items: int = Field(default=3, ge=1, le=256)
    warm_valence_threshold: float = Field(default=0.1, ge=-1, le=1)
    concern_valence_threshold: float = Field(default=-0.05, ge=-1, le=1)
    warmth_memory_scale: float = Field(default=3.0, gt=0, le=64)
    familiarity_scale: float = Field(default=12.0, gt=0, le=256)
    warmth_weight: float = Field(default=0.8, ge=0, le=1)
    engagement_min: float = Field(default=0.2, ge=0, le=1)
    engagement_max: float = Field(default=0.8, ge=0, le=1)


class SlowVariableSnapshot(RequestModel):
    companion_id: Identifier
    enabled: bool
    computed_at: str
    window_start: str
    sample_size: int = Field(ge=0)
    valence_mean: float = Field(ge=-1, le=1)
    valence_inertia: float = Field(ge=0, le=1)
    relationship_warmth: float = Field(ge=0, le=1)
    familiarity: float = Field(ge=0, le=1)
    engagement: float = Field(ge=0, le=1)
    tone_hint: _ToneHint
    context_version: str
    model_calls: Literal[0] = 0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


class SlowVariableService:
    """Compute a deterministic, read-only slow-variable snapshot."""

    def __init__(self, personality, emotion, *, policy=None) -> None:
        self.personality = personality
        self.emotion = emotion
        self.policy = policy or SlowVariablePolicy()
        self.store = personality.store

    def get(self, companion_id: str) -> dict:
        companion_id = self.personality._identifier(companion_id)
        with self.store.transaction() as connection:
            self.personality._companion(connection, companion_id)
            return self.snapshot(connection, companion_id, self.personality._now())

    def snapshot(self, connection, companion_id: str, now: datetime) -> dict:
        policy = self.policy
        window_start = now - timedelta(seconds=policy.window_seconds)
        if not policy.enabled:
            return self._empty(companion_id, now, window_start).model_dump(mode="json")

        rows = connection.execute(
            """SELECT * FROM emotion_experiences WHERE companion_id=? AND invalidated=0
                AND created_at >= ? ORDER BY sequence DESC LIMIT ?""",
            (companion_id, _timestamp(window_start), policy.candidate_limit),
        ).fetchall()
        rows = [row for row in rows if self.emotion._base_live(connection, row)]

        values: list[float] = []
        weights: list[float] = []
        fingerprint: list[list] = []
        half_life = float(policy.half_life_seconds)
        for row in rows:
            appraisal, _, correction, _ = self.emotion._appraisal(connection, row)
            elapsed = max(0.0, (now - _instant(row["created_at"])).total_seconds())
            decay = 2 ** (-elapsed / half_life)
            confidence = max(0.0, float(appraisal["confidence"]))
            values.append(float(appraisal["delta"]["valence"]))
            weights.append(decay * confidence)
            fingerprint.append(
                [row["event_id"], correction["revision"] if correction else 0, row["created_at"]]
            )

        total_weight = sum(weights)
        if total_weight > 0:
            valence_mean = sum(w * v for w, v in zip(weights, values)) / total_weight
            variance = (
                sum(w * (v - valence_mean) ** 2 for w, v in zip(weights, values)) / total_weight
            )
            inertia = _clamp(1 - math.sqrt(max(0.0, variance)) / _INERTIA_SCALE)
        else:
            valence_mean = 0.0
            inertia = 0.0
        valence_mean = _clamp(valence_mean, -1.0, 1.0)

        memories = connection.execute(
            """SELECT COUNT(*) FROM memory_records WHERE companion_id=? AND active=1
                AND kind IN ('relationship','preference')""",
            (companion_id,),
        ).fetchone()[0]
        # Absent memories are neutral (0.5), not a negative signal; each retained
        # relationship/preference memory pushes warmth up toward 1.
        memory_signal = _NEUTRAL_ENGAGEMENT + 0.5 * (
            1 - math.exp(-memories / policy.warmth_memory_scale)
        )
        affect_signal = (valence_mean + 1) / 2 if total_weight > 0 else _NEUTRAL_ENGAGEMENT
        warmth = _clamp((memory_signal + affect_signal) / 2)

        sample_size = len(rows)
        familiarity = _clamp(1 - math.exp(-sample_size / policy.familiarity_scale))
        # Without enough evidence, participation stays exactly neutral so a new
        # relationship is never penalized for a slow signal it has not earned.
        if sample_size < policy.min_evidence_items:
            engagement = _NEUTRAL_ENGAGEMENT
        else:
            engagement = _clamp(
                _NEUTRAL_ENGAGEMENT + policy.warmth_weight * (warmth - _NEUTRAL_ENGAGEMENT),
                policy.engagement_min,
                policy.engagement_max,
            )

        if sample_size < policy.min_evidence_items:
            tone: _ToneHint = "neutral"
        elif valence_mean <= policy.concern_valence_threshold:
            tone = "cautious"
        elif valence_mean >= policy.warm_valence_threshold:
            tone = "warm"
        else:
            tone = "neutral"

        return SlowVariableSnapshot(
            companion_id=companion_id,
            enabled=True,
            computed_at=_timestamp(now),
            window_start=_timestamp(window_start),
            sample_size=sample_size,
            valence_mean=round(valence_mean, 6),
            valence_inertia=round(inertia, 6),
            relationship_warmth=round(warmth, 6),
            familiarity=round(familiarity, 6),
            engagement=round(engagement, 6),
            tone_hint=tone,
            context_version=self._version(companion_id, fingerprint),
        ).model_dump(mode="json")

    def _empty(
        self, companion_id: str, now: datetime, window_start: datetime
    ) -> SlowVariableSnapshot:
        return SlowVariableSnapshot(
            companion_id=companion_id,
            enabled=False,
            computed_at=_timestamp(now),
            window_start=_timestamp(window_start),
            sample_size=0,
            valence_mean=0.0,
            valence_inertia=0.0,
            relationship_warmth=_NEUTRAL_ENGAGEMENT,
            familiarity=0.0,
            engagement=_NEUTRAL_ENGAGEMENT,
            tone_hint="neutral",
            context_version=self._version(companion_id, []),
        )

    def _version(self, companion_id: str, fingerprint: list) -> str:
        payload = {
            "companion_id": companion_id,
            "fingerprint": fingerprint,
            "policy": self.policy.model_dump(mode="json"),
        }
        return hashlib.sha256(_json(payload).encode()).hexdigest()
