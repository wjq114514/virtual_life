"""Evidence-backed personality evolution proposals.

Nothing in this module ever rewrites the stable personality by itself. It only
generates bounded, field-level proposals from local evidence; an owner must
explicitly accept a proposal before the profile changes. Every proposal keeps
the revision it was based on and the sources that support it so an owner can
see why it was suggested and can reproduce the decision.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError

from .coordinator import MAX_VERSION
from .memory import ApproximateTokenEstimator, MemoryError
from .model_provider import ProviderError, estimate_chat_input_tokens
from .models import Identifier, JsonObject, RequestModel, Version
from .personality import (
    PersonalityDefinition,
    PersonalityError,
    PersonalitySnapshot,
    _json,
    _timestamp,
)
from .ports import ModelRequest

ProposalStatus = Literal["proposed", "accepted", "rejected", "superseded", "expired"]
Generator = Literal["rules_v1", "model_v1"]
SkipReason = Literal[
    "insufficient_evidence",
    "below_threshold",
    "open_proposal_exists",
    "cooldown_active",
    "cumulative_drift_exceeded",
    "field_at_bound",
    "calibration_requires_phase_c",
    "model_semantics_required",
    "invalid_model_proposal",
]
TraitName = Literal["warmth", "curiosity", "assertiveness", "playfulness", "initiative"]
TextName = Literal["description", "personality", "scenario", "first_message", "example_dialogue"]
StyleName = Literal["tone", "response_length"]
ItemName = Literal["values", "boundaries", "habits"]
Metric = Literal[
    "quiet_request_rate",
    "proactive_silent_rate",
    "correction_rate",
    "preference_repeat",
]
_CANDIDATE_LIMIT = 512
_GENERATOR_VERSION = "1"
_MODEL_CONFIDENCE_CAP = 0.7


class EvolutionPolicy(RequestModel):
    enabled: bool = True
    max_open_proposals: int = Field(default=5, ge=1, le=32)
    proposal_ttl_seconds: int = Field(default=2_592_000, ge=3_600, le=31_536_000)
    max_traits_per_proposal: int = Field(default=2, ge=1, le=5)
    max_operations_per_proposal: int = Field(default=4, ge=1, le=8)
    max_trait_step: float = Field(default=0.05, ge=0, le=0.2)
    trait_cooldown_seconds: int = Field(default=604_800, ge=0, le=31_536_000)
    max_cumulative_drift: float = Field(default=0.2, ge=0, le=1)
    trait_window_seconds: int = Field(default=7_776_000, ge=86_400, le=63_072_000)
    min_evidence_items: int = Field(default=3, ge=1, le=64)
    quiet_request_rate_threshold: float = Field(default=0.4, ge=0.1, le=1)
    proactive_silent_rate_threshold: float = Field(default=0.6, ge=0.1, le=1)
    preference_repeat_threshold: int = Field(default=3, ge=2, le=16)
    model_assisted: bool = False
    output_token_limit: int = Field(default=800, ge=1, le=8_000)


class TraitAdjustment(RequestModel):
    op: Literal["adjust_trait"]
    field: TraitName
    delta: float = Field(ge=-0.2, le=0.2)
    before: float = Field(ge=0, le=1)
    after: float = Field(ge=0, le=1)


class TextReplacement(RequestModel):
    op: Literal["set_text"]
    field: TextName
    before: str = Field(max_length=4_096)
    after: str = Field(max_length=4_096)


class StyleReplacement(RequestModel):
    op: Literal["set_style"]
    field: StyleName
    before: str = Field(max_length=512)
    after: str = Field(max_length=512)


class ItemAddition(RequestModel):
    op: Literal["add_item"]
    field: ItemName
    item: str = Field(min_length=1, max_length=160)


PatchOperation = Annotated[
    TraitAdjustment | TextReplacement | StyleReplacement | ItemAddition,
    Field(discriminator="op"),
]

_PATCH_ADAPTER = TypeAdapter(list[PatchOperation])
_ALLOWED_SOURCE_KEYS = ("event_refs", "memory_refs", "correction_refs")


class EvolutionAggregate(RequestModel):
    metric: Metric
    value: float
    threshold: float
    samples: int = Field(ge=0)


class MemoryEvidenceReference(RequestModel):
    memory_id: Identifier
    revision: Version


class CorrectionEvidenceReference(RequestModel):
    event_id: Identifier
    kind: Literal["direction", "intensity", "attribution"]
    revision: int = Field(ge=1)


class EvolutionEvidence(RequestModel):
    window_days: int = Field(ge=1)
    sample_size: int = Field(ge=0)
    memory_refs: list[MemoryEvidenceReference] = Field(default_factory=list, max_length=16)
    event_refs: list[Identifier] = Field(default_factory=list, max_length=16)
    correction_refs: list[CorrectionEvidenceReference] = Field(default_factory=list, max_length=16)
    aggregates: list[EvolutionAggregate] = Field(default_factory=list, max_length=8)


class PersonalityEvolutionProposal(RequestModel):
    proposal_id: Identifier
    companion_id: Identifier
    revision: Version
    status: ProposalStatus
    base_personality_revision: Version
    generator: Generator
    generator_version: str = Field(max_length=32)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=1_024)
    patch: list[PatchOperation] = Field(min_length=1, max_length=8)
    evidence: EvolutionEvidence
    model: JsonObject | None = None
    created_at: str
    updated_at: str
    expires_at: str
    decided_at: str | None = None


class PersonalityEvolutionPage(RequestModel):
    companion_id: Identifier
    items: list[PersonalityEvolutionProposal]
    next_sequence: int = Field(ge=0)
    has_more: bool


class PersonalityEvolutionRefresh(RequestModel):
    command_id: Identifier
    use_model: bool = False
    max_proposals: int = Field(default=1, ge=1, le=5)


class EvolutionSkipped(RequestModel):
    reason: SkipReason
    metric: Metric | None = None


class PersonalityEvolutionRefreshResult(RequestModel):
    companion_id: Identifier
    base_personality_revision: Version
    generated: int = Field(ge=0)
    skipped: list[EvolutionSkipped]
    proposals: list[PersonalityEvolutionProposal]
    model_calls: int = Field(default=0, ge=0, le=1)
    created_at: str


class PersonalityEvolutionDecision(RequestModel):
    command_id: Identifier
    expected_personality_revision: Version
    expected_proposal_revision: Version


class PersonalityEvolutionRejection(PersonalityEvolutionDecision):
    reason: str | None = Field(default=None, max_length=256)


class PersonalityEvolutionDecisionResult(RequestModel):
    companion_id: Identifier
    proposal: PersonalityEvolutionProposal
    personality: PersonalitySnapshot
    decided_at: str


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS personality_evolution_proposals (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL UNIQUE,
        companion_id TEXT NOT NULL REFERENCES companions(companion_id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        status TEXT NOT NULL CHECK (status IN
            ('proposed','accepted','rejected','superseded','expired')),
        base_personality_revision INTEGER NOT NULL CHECK (base_personality_revision >= 0),
        generator TEXT NOT NULL,
        generator_version TEXT NOT NULL,
        confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
        rationale TEXT NOT NULL,
        patch_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        model_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS personality_evolution_open
        ON personality_evolution_proposals(companion_id, status, sequence DESC)""",
)

_TRAIT_FIELDS = ("warmth", "curiosity", "assertiveness", "playfulness", "initiative")
_TEXT_FIELDS = (
    "description",
    "personality",
    "scenario",
    "first_message",
    "example_dialogue",
)


class PersonalityEvolutionService:
    """Generate, list and decide proposals; only accept() changes the profile."""

    def __init__(
        self,
        personality,
        emotion,
        memory,
        *,
        policy: EvolutionPolicy | None = None,
        provider=None,
        model_name: str = "",
        token_estimator=None,
    ):
        self.personality = personality
        self.emotion = emotion
        self.memory = memory
        self.policy = policy or EvolutionPolicy()
        self.provider = provider
        self.model_name = model_name
        self.token_estimator = token_estimator or ApproximateTokenEstimator()
        self.store = personality.store
        with self.store.transaction(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def _now(self) -> datetime:
        return self.personality._now()

    # -- reads -------------------------------------------------------------

    def list(
        self,
        companion_id: str,
        *,
        status: str | None = None,
        after_sequence: int = 0,
        limit: int = 20,
    ) -> dict:
        companion_id = self.personality._identifier(companion_id)
        if status is not None and status not in (
            "proposed",
            "accepted",
            "rejected",
            "superseded",
            "expired",
        ):
            raise PersonalityError(422, "validation_error", "Invalid proposal status filter")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise PersonalityError(422, "validation_error", "limit must be between 1 and 50")
        if type(after_sequence) is not int or after_sequence < 0:
            raise PersonalityError(
                422, "validation_error", "after_sequence must be a nonnegative integer"
            )
        with self.store.transaction(write=True) as connection:
            self.personality._companion(connection, companion_id)
            now = self._now()
            self._reap(connection, companion_id, now)
            clause = " AND status = ?" if status is not None else ""
            params: list = [companion_id]
            if status is not None:
                params.append(status)
            rows = connection.execute(
                f"""SELECT * FROM personality_evolution_proposals
                    WHERE companion_id = ?{clause} AND sequence > ?
                    ORDER BY sequence LIMIT ?""",
                (*params, after_sequence, limit + 1),
            ).fetchall()
            items = [self._snapshot(row) for row in rows[:limit]]
            return {
                "companion_id": companion_id,
                "items": items,
                "next_sequence": rows[min(limit, len(rows)) - 1]["sequence"]
                if rows
                else after_sequence,
                "has_more": len(rows) > limit,
            }

    def get(self, companion_id: str, proposal_id: str) -> dict:
        companion_id = self.personality._identifier(companion_id)
        proposal_id = self.personality._identifier(proposal_id)
        with self.store.transaction(write=True) as connection:
            self.personality._companion(connection, companion_id)
            self._reap(connection, companion_id, self._now())
            row = self._proposal_row(connection, companion_id, proposal_id)
            return self._snapshot(row)

    @staticmethod
    def _proposal_row(connection, companion_id, proposal_id):
        row = connection.execute(
            "SELECT * FROM personality_evolution_proposals WHERE companion_id = ? AND proposal_id = ?",
            (companion_id, proposal_id),
        ).fetchone()
        if row is None:
            raise PersonalityError(404, "proposal_not_found", "Evolution proposal was not found")
        return row

    @staticmethod
    def _snapshot(row) -> dict:
        return {
            "proposal_id": row["proposal_id"],
            "companion_id": row["companion_id"],
            "revision": row["revision"],
            "status": row["status"],
            "base_personality_revision": row["base_personality_revision"],
            "generator": row["generator"],
            "generator_version": row["generator_version"],
            "confidence": row["confidence"],
            "rationale": row["rationale"],
            "patch": json.loads(row["patch_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "model": json.loads(row["model_json"]) if row["model_json"] is not None else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "decided_at": row["decided_at"],
        }

    def _reap(self, connection, companion_id, now) -> None:
        timestamp = _timestamp(now)
        profile = self.personality._profile_row(connection, companion_id)
        current = profile["revision"] if profile is not None else 0
        connection.execute(
            """UPDATE personality_evolution_proposals SET status = 'expired', updated_at = ?
                WHERE companion_id = ? AND status = 'proposed' AND expires_at <= ?""",
            (timestamp, companion_id, timestamp),
        )
        connection.execute(
            """UPDATE personality_evolution_proposals SET status = 'superseded', updated_at = ?
                WHERE companion_id = ? AND status = 'proposed'
                AND base_personality_revision != ?""",
            (timestamp, companion_id, current),
        )

    # -- generation --------------------------------------------------------

    def refresh(self, companion_id: str, payload: dict) -> dict:
        companion_id = self.personality._identifier(companion_id)
        data = self.personality._validate(PersonalityEvolutionRefresh, payload).model_dump(
            mode="json"
        )
        if not self.policy.enabled:
            raise PersonalityError(422, "evolution_disabled", "Personality evolution is disabled")
        if data["use_model"]:
            if not self.policy.model_assisted:
                raise PersonalityError(
                    422, "model_assisted_disabled", "Model-assisted proposals are disabled"
                )
            if self.provider is None or not self.model_name.strip():
                raise PersonalityError(
                    422,
                    "model_assisted_unavailable",
                    "Model-assisted proposals need a configured provider and model",
                )
            return self._refresh_with_model(companion_id, data)

        def action(connection, now, _):
            self._reap(connection, companion_id, now)
            base_revision = self._profile_revision(connection, companion_id)
            definition = self._definition(connection, companion_id)
            candidates, skipped = self._generate(connection, companion_id, definition, now)
            return self._persist(
                connection, companion_id, data, base_revision, candidates, skipped, now, 0
            )

        return self.personality._write(companion_id, "personality.evolution.refresh", data, action)

    def _refresh_with_model(self, companion_id: str, data: dict) -> dict:
        """Ask the model for one bounded patch, then persist under the usual fences.

        The provider call runs outside any database transaction so the reserved token
        ledger and the durable command receipt are never held behind a network request.
        Any model output that violates the schema, bounds or source set is discarded as
        ``invalid_model_proposal``; a provider failure produces no proposal.
        """
        fingerprint = hashlib.sha256(
            _json(
                {
                    "companion_id": companion_id,
                    "operation": "personality.evolution.refresh",
                    "payload": data,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.store.transaction() as connection:
            self.personality._companion(connection, companion_id)
            previous = connection.execute(
                "SELECT fingerprint, result_json FROM personality_commands WHERE command_id = ?",
                (data["command_id"],),
            ).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise PersonalityError(
                        409,
                        "idempotency_conflict",
                        "command_id has already been used for another personality request",
                    )
                return json.loads(previous["result_json"])
            profile = self.personality._profile_row(connection, companion_id)
            definition = (
                json.loads(profile["definition_json"])
                if profile is not None
                else PersonalityDefinition().model_dump(mode="json")
            )
            now = self._now()
            allowed, prompt, aggregates = self._model_inputs(
                connection, companion_id, definition, now
            )

        estimated = estimate_chat_input_tokens(
            self.token_estimator, prompt, purpose="personality_evolution"
        )
        task_id = "pev-task-" + data["command_id"]
        hold = self.memory.reserve_model_tokens(
            companion_id,
            {
                "task_id": task_id,
                "purpose": "personality_evolution",
                "input_tokens": estimated,
                "output_tokens": self.policy.output_token_limit,
            },
        )
        if not hold["new_reservation"]:
            raise PersonalityError(
                409, "evolution_in_progress", "Model-assisted refresh already reserved this task"
            )
        model_request = ModelRequest(
            task_id=task_id,
            companion_id=companion_id,
            purpose="personality_evolution",
            model=self.model_name,
            context_text=prompt,
            input_token_limit=estimated,
            output_token_limit=self.policy.output_token_limit,
        )
        try:
            answer = self.provider.complete(model_request)
        except ProviderError as exc:
            if exc.usage is not None:
                self.memory.settle_model_tokens(
                    companion_id,
                    task_id,
                    {
                        "input_tokens": exc.usage.input_tokens,
                        "output_tokens": exc.usage.output_tokens,
                    },
                )
            elif exc.releasable:
                self.memory.release_model_tokens(companion_id, task_id)
            raise PersonalityError(
                502, "model_execution_failed", "Personality evolution model call failed"
            ) from exc
        except Exception as exc:
            raise PersonalityError(
                502, "model_execution_failed", "Personality evolution model call failed"
            ) from exc
        usage = getattr(answer, "usage", None)
        if (
            usage is None
            or not isinstance(usage.input_tokens, int)
            or not isinstance(usage.output_tokens, int)
        ):
            raise PersonalityError(
                502, "model_usage_missing", "Model provider did not return valid token usage"
            )
        self.memory.settle_model_tokens(
            companion_id,
            task_id,
            {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
        )
        raw_text = answer.text
        model_meta = {
            "model": self.model_name,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }

        def action(connection, now, _):
            self._reap(connection, companion_id, now)
            base_revision = self._profile_revision(connection, companion_id)
            definition = self._definition(connection, companion_id)
            candidates, skipped = self._generate(connection, companion_id, definition, now)
            candidate, skip = self._model_candidate(
                connection, companion_id, definition, now, raw_text, allowed, aggregates, model_meta
            )
            if skip is not None:
                skipped.append(skip)
            elif candidate is not None:
                field = candidate["field"]
                if any(item["field"] == field for item in candidates):
                    skipped.append({"reason": "open_proposal_exists", "metric": None})
                else:
                    blocked = self._guardrail(connection, companion_id, candidate, now)
                    if blocked is not None:
                        skipped.append(blocked)
                    else:
                        candidates.append(candidate)
            return self._persist(
                connection, companion_id, data, base_revision, candidates, skipped, now, 1
            )

        return self.personality._write(companion_id, "personality.evolution.refresh", data, action)

    def _profile_revision(self, connection, companion_id) -> int:
        profile = self.personality._profile_row(connection, companion_id)
        return profile["revision"] if profile is not None else 0

    def _definition(self, connection, companion_id) -> dict:
        profile = self.personality._profile_row(connection, companion_id)
        return (
            json.loads(profile["definition_json"])
            if profile is not None
            else PersonalityDefinition().model_dump(mode="json")
        )

    def _persist(
        self, connection, companion_id, data, base_revision, candidates, skipped, now, model_calls
    ) -> dict:
        open_count = connection.execute(
            """SELECT COUNT(*) FROM personality_evolution_proposals
                WHERE companion_id = ? AND status = 'proposed'""",
            (companion_id,),
        ).fetchone()[0]
        created_at = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=self.policy.proposal_ttl_seconds))
        proposals = []
        for candidate in candidates:
            if len(proposals) >= data["max_proposals"]:
                break
            if open_count + len(proposals) >= self.policy.max_open_proposals:
                skipped.append({"reason": "open_proposal_exists", "metric": candidate["metric"]})
                break
            proposal_id = "pev_" + uuid.uuid4().hex
            generator = candidate.get("generator", "rules_v1")
            model_json = _json(candidate["model"]) if candidate.get("model") else None
            connection.execute(
                """INSERT INTO personality_evolution_proposals
                    (proposal_id, companion_id, revision, status, base_personality_revision,
                     generator, generator_version, confidence, rationale, patch_json,
                     evidence_json, model_json, created_at, updated_at, expires_at, decided_at)
                    VALUES (?, ?, 1, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    proposal_id,
                    companion_id,
                    base_revision,
                    generator,
                    _GENERATOR_VERSION,
                    candidate["confidence"],
                    candidate["rationale"],
                    _json(candidate["patch"]),
                    _json(candidate["evidence"]),
                    model_json,
                    created_at,
                    created_at,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM personality_evolution_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            proposals.append(self._snapshot(row))
            self.personality._emit(
                connection,
                companion_id,
                now,
                "companion.personality_evolution_proposed",
                {
                    "proposal_id": proposal_id,
                    "personality_revision": base_revision,
                    "generator": generator,
                    "confidence": candidate["confidence"],
                },
            )
        return {
            "companion_id": companion_id,
            "base_personality_revision": base_revision,
            "generated": len(proposals),
            "skipped": skipped,
            "proposals": proposals,
            "model_calls": model_calls,
            "created_at": created_at,
        }

    def _generate(self, connection, companion_id, definition, now):
        """Return (candidates, skipped) in priority order, already guardrailed."""
        skipped: list[dict] = []
        candidates: list[dict] = []
        used_fields: set[str] = set()
        for builder in (self._quiet_candidate, self._proactive_candidate):
            candidate, skip = builder(connection, companion_id, definition, now)
            if skip is not None:
                skipped.append(skip)
                continue
            if candidate is None:
                continue
            field = candidate["field"]
            if field in used_fields:
                continue
            skip = self._guardrail(connection, companion_id, candidate, now)
            if skip is not None:
                skipped.append(skip)
                continue
            candidates.append(candidate)
            used_fields.add(field)
        for extra in (self._calibration_skip, self._preference_skip):
            skip = extra(connection, companion_id, now)
            if skip is not None:
                skipped.append(skip)
        return candidates, skipped

    def _window_start(self, now) -> datetime:
        return now - timedelta(seconds=self.policy.trait_window_seconds)

    def _window_days(self) -> int:
        return max(1, self.policy.trait_window_seconds // 86_400)

    def _live_experiences(self, connection, companion_id, since):
        rows = connection.execute(
            """SELECT * FROM emotion_experiences WHERE companion_id = ? AND invalidated = 0
                AND created_at >= ? ORDER BY sequence DESC LIMIT ?""",
            (companion_id, _timestamp(since), _CANDIDATE_LIMIT),
        ).fetchall()
        return [row for row in rows if self.emotion._base_live(connection, row)]

    def _initiative_patch(self, definition):
        before = definition["traits"]["initiative"]
        after = round(max(0.0, min(1.0, before - self.policy.max_trait_step)), 6)
        if after == before:
            return None
        return [
            {
                "op": "adjust_trait",
                "field": "initiative",
                "delta": round(after - before, 6),
                "before": before,
                "after": after,
            }
        ]

    def _quiet_candidate(self, connection, companion_id, definition, now):
        rows = self._live_experiences(connection, companion_id, self._window_start(now))
        total = len(rows)
        if total < self.policy.min_evidence_items:
            return None, {"reason": "insufficient_evidence", "metric": "quiet_request_rate"}
        quiet = [
            row for row in rows if json.loads(row["appraisal_json"]).get("reaction") == "quiet"
        ]
        rate = len(quiet) / total
        if rate < self.policy.quiet_request_rate_threshold:
            return None, {"reason": "below_threshold", "metric": "quiet_request_rate"}
        patch = self._initiative_patch(definition)
        if patch is None:
            return None, {"reason": "field_at_bound", "metric": "quiet_request_rate"}
        days = self._window_days()
        return (
            {
                "field": "initiative",
                "metric": "quiet_request_rate",
                "confidence": round(min(0.9, 0.5 + rate * 0.4), 6),
                "rationale": (
                    f"最近 {days} 天内 {total} 次对话中有 {len(quiet)} 次明确请求安静，"
                    "主动参与倾向据此下调。"
                ),
                "patch": patch,
                "evidence": {
                    "window_days": days,
                    "sample_size": total,
                    "event_refs": [row["event_id"] for row in quiet][:16],
                    "aggregates": [
                        {
                            "metric": "quiet_request_rate",
                            "value": round(rate, 6),
                            "threshold": self.policy.quiet_request_rate_threshold,
                            "samples": total,
                        }
                    ],
                },
            },
            None,
        )

    def _proactive_candidate(self, connection, companion_id, definition, now):
        rows = connection.execute(
            """SELECT status, source_event_id FROM proactivity_candidates
                WHERE companion_id = ? AND finished_at IS NOT NULL AND finished_at >= ?
                ORDER BY sequence DESC LIMIT ?""",
            (companion_id, _timestamp(self._window_start(now)), _CANDIDATE_LIMIT),
        ).fetchall()
        considered = [row for row in rows if row["status"] in ("silent", "discarded", "completed")]
        total = len(considered)
        if total < self.policy.min_evidence_items:
            return None, {"reason": "insufficient_evidence", "metric": "proactive_silent_rate"}
        silent = [row for row in considered if row["status"] in ("silent", "discarded")]
        rate = len(silent) / total
        if rate < self.policy.proactive_silent_rate_threshold:
            return None, {"reason": "below_threshold", "metric": "proactive_silent_rate"}
        patch = self._initiative_patch(definition)
        if patch is None:
            return None, {"reason": "field_at_bound", "metric": "proactive_silent_rate"}
        days = self._window_days()
        return (
            {
                "field": "initiative",
                "metric": "proactive_silent_rate",
                "confidence": round(min(0.9, 0.5 + rate * 0.4), 6),
                "rationale": (
                    f"最近 {days} 天内主动发言 {total} 次中有 {len(silent)} 次被判定为无需开口，"
                    "主动参与倾向据此下调。"
                ),
                "patch": patch,
                "evidence": {
                    "window_days": days,
                    "sample_size": total,
                    "event_refs": [row["source_event_id"] for row in silent][:16],
                    "aggregates": [
                        {
                            "metric": "proactive_silent_rate",
                            "value": round(rate, 6),
                            "threshold": self.policy.proactive_silent_rate_threshold,
                            "samples": total,
                        }
                    ],
                },
            },
            None,
        )

    def _calibration_skip(self, connection, companion_id, now):
        rows = self._live_experiences(connection, companion_id, self._window_start(now))
        if len(rows) < self.policy.min_evidence_items:
            return None
        corrected = 0
        for row in rows:
            _, info = self.emotion._correction(connection, row)
            if info is not None:
                corrected += 1
        if corrected < self.policy.min_evidence_items:
            return None
        return {"reason": "calibration_requires_phase_c", "metric": "correction_rate"}

    def _preference_skip(self, connection, companion_id, now):
        row = connection.execute(
            """SELECT COUNT(*) AS total FROM memory_records
                WHERE companion_id = ? AND kind = 'preference' AND active = 1
                GROUP BY COALESCE(semantic_key, normalized_content)
                ORDER BY total DESC LIMIT 1""",
            (companion_id,),
        ).fetchone()
        if row is None or row["total"] < self.policy.preference_repeat_threshold:
            return None
        return {"reason": "model_semantics_required", "metric": "preference_repeat"}

    # -- model-assisted generation ----------------------------------------

    def _model_inputs(self, connection, companion_id, definition, now):
        """Bounded evidence digest plus the only sources the model may cite."""
        experiences = self._live_experiences(connection, companion_id, self._window_start(now))
        total = len(experiences)
        quiet = sum(
            1 for row in experiences if json.loads(row["appraisal_json"]).get("reaction") == "quiet"
        )
        corrections = []
        for row in experiences:
            _, info = self.emotion._correction(connection, row)
            if info is not None:
                corrections.append((row["event_id"], info["kind"], info["revision"]))
        preference_rows = connection.execute(
            """SELECT memory_id, revision FROM memory_records
                WHERE companion_id = ? AND kind = 'preference' AND active = 1
                ORDER BY updated_at DESC LIMIT 8""",
            (companion_id,),
        ).fetchall()
        allowed = {
            "event_refs": [row["event_id"] for row in experiences][:16],
            "correction_refs": [
                {"event_id": event_id, "kind": kind, "revision": revision}
                for event_id, kind, revision in corrections[:8]
            ],
            "memory_refs": [
                {"memory_id": row["memory_id"], "revision": row["revision"]}
                for row in preference_rows
            ],
        }
        aggregates = {
            "window_days": self._window_days(),
            "experience_samples": total,
            "quiet_request_rate": round(quiet / total, 4) if total else 0.0,
            "correction_rate": round(len(corrections) / total, 4) if total else 0.0,
            "preference_memories": len(preference_rows),
        }
        style = definition.get("speech_style", {})
        profile = {
            "traits": definition.get("traits", {}),
            "speech_style": {
                "tone": style.get("tone"),
                "response_length": style.get("response_length"),
                "habits": style.get("habits", []),
            },
            "values": definition.get("values", []),
            "boundaries": definition.get("boundaries", []),
        }
        limits = {
            "max_operations": self.policy.max_operations_per_proposal,
            "max_trait_changes": self.policy.max_traits_per_proposal,
            "max_trait_step": self.policy.max_trait_step,
            "trait_fields": list(_TRAIT_FIELDS),
            "text_fields": list(_TEXT_FIELDS),
            "style_fields": ["tone", "response_length"],
            "item_fields": ["values", "boundaries", "habits"],
            "confidence_cap": _MODEL_CONFIDENCE_CAP,
        }
        prompt = _json(
            {
                "task": "propose_one_personality_patch",
                "limits": limits,
                "current_definition": profile,
                "evidence_aggregates": aggregates,
                "available_sources": allowed,
                "output_schema": {
                    "confidence": "number in 0..1",
                    "rationale": "short sentence citing the aggregates",
                    "patch": "list of operations, each op is adjust_trait|set_text|set_style|add_item",
                    "sources": "optional subset of available_sources",
                },
            }
        )
        return allowed, prompt, aggregates

    def _model_candidate(
        self, connection, companion_id, definition, now, raw_text, allowed, aggregates, model_meta
    ):
        invalid = {"reason": "invalid_model_proposal", "metric": None}
        if not isinstance(raw_text, str) or len(raw_text.encode("utf-8")) > 65_536:
            return None, invalid
        try:
            payload = json.loads(raw_text)
        except (ValueError, RecursionError):
            return None, invalid
        if not isinstance(payload, dict):
            return None, invalid
        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None, invalid
        confidence = min(float(confidence), _MODEL_CONFIDENCE_CAP)
        if not 0 <= confidence <= 1:
            return None, invalid
        rationale = payload.get("rationale")
        if not isinstance(rationale, str):
            return None, invalid
        rationale = rationale.strip()
        if not rationale or len(rationale) > 1_024:
            return None, invalid
        try:
            operations = _PATCH_ADAPTER.validate_python(payload.get("patch"))
        except ValidationError:
            return None, invalid
        if not operations:
            return None, invalid
        patch = [operation.model_dump(mode="json") for operation in operations]
        sources = self._model_sources(payload.get("sources"), allowed)
        if sources is None:
            return None, invalid
        try:
            self._validate_patch_bounds(patch)
            new_definition = self._apply_patch(definition, patch)
            PersonalityDefinition.model_validate(new_definition)
        except (PersonalityError, ValidationError):
            return None, invalid
        evidence = {
            "window_days": aggregates["window_days"],
            "sample_size": aggregates["experience_samples"],
            "memory_refs": sources["memory_refs"],
            "event_refs": sources["event_refs"],
            "correction_refs": sources["correction_refs"],
            "aggregates": [],
        }
        return (
            {
                "field": patch[0]["field"],
                "metric": None,
                "generator": "model_v1",
                "confidence": round(confidence, 6),
                "rationale": rationale,
                "patch": patch,
                "evidence": evidence,
                "model": model_meta,
            },
            None,
        )

    @staticmethod
    def _model_sources(raw, allowed):
        """Keep only generator-provided sources; the model can never widen the set."""
        if raw is None:
            return {
                "event_refs": list(allowed["event_refs"]),
                "memory_refs": list(allowed["memory_refs"]),
                "correction_refs": list(allowed["correction_refs"]),
            }
        if not isinstance(raw, dict):
            return None
        events = raw.get("event_refs", [])
        memory = raw.get("memory_refs", [])
        corrections = raw.get("correction_refs", [])
        if not all(isinstance(item, list) for item in (events, memory, corrections)):
            return None
        if len(events) > 16 or len(memory) > 16 or len(corrections) > 16:
            return None
        allowed_events = set(allowed["event_refs"])
        allowed_memory = {ref["memory_id"]: ref["revision"] for ref in allowed["memory_refs"]}
        allowed_corrections = {
            (ref["event_id"], ref["kind"]): ref["revision"] for ref in allowed["correction_refs"]
        }
        for event_id in events:
            if event_id not in allowed_events:
                return None
        for ref in memory:
            if not isinstance(ref, dict) or allowed_memory.get(ref.get("memory_id")) != ref.get(
                "revision"
            ):
                return None
        for ref in corrections:
            if not isinstance(ref, dict) or allowed_corrections.get(
                (ref.get("event_id"), ref.get("kind"))
            ) != ref.get("revision"):
                return None
        return {"event_refs": events, "memory_refs": memory, "correction_refs": corrections}

    def _guardrail(self, connection, companion_id, candidate, now):
        field = candidate["field"]
        for row in connection.execute(
            """SELECT patch_json FROM personality_evolution_proposals
                WHERE companion_id = ? AND status = 'proposed'""",
            (companion_id,),
        ).fetchall():
            if self._patch_fields(json.loads(row["patch_json"])) & {field}:
                return {"reason": "open_proposal_exists", "metric": candidate["metric"]}
        if self._cooldown_active(connection, companion_id, field, now):
            return {"reason": "cooldown_active", "metric": candidate["metric"]}
        step = abs(
            sum(
                operation["delta"]
                for operation in candidate["patch"]
                if operation["op"] == "adjust_trait"
            )
        )
        if (
            self._drift(connection, companion_id, field, now) + step
            > self.policy.max_cumulative_drift
        ):
            return {"reason": "cumulative_drift_exceeded", "metric": candidate["metric"]}
        return None

    @staticmethod
    def _patch_fields(patch) -> set[str]:
        fields = set()
        for operation in patch:
            if operation["op"] == "adjust_trait":
                fields.add(operation["field"])
            elif operation["op"] in ("set_text", "set_style"):
                fields.add(operation["field"])
            else:
                fields.add(operation["field"])
        return fields

    def _accepted_patches(self, connection, companion_id, since):
        rows = connection.execute(
            """SELECT patch_json, decided_at FROM personality_evolution_proposals
                WHERE companion_id = ? AND status = 'accepted' AND decided_at >= ?""",
            (companion_id, _timestamp(since)),
        ).fetchall()
        return [(json.loads(row["patch_json"]), row["decided_at"]) for row in rows]

    def _cooldown_active(self, connection, companion_id, field, now) -> bool:
        if self.policy.trait_cooldown_seconds == 0:
            return False
        since = now - timedelta(seconds=self.policy.trait_cooldown_seconds)
        for patch, _ in self._accepted_patches(connection, companion_id, since):
            if field in self._patch_fields(patch):
                return True
        return False

    def _drift(self, connection, companion_id, field, now) -> float:
        since = now - timedelta(seconds=self.policy.trait_window_seconds)
        total = 0.0
        for patch, _ in self._accepted_patches(connection, companion_id, since):
            for operation in patch:
                if operation["op"] == "adjust_trait" and operation["field"] == field:
                    total += abs(operation["delta"])
        return total

    # -- decisions ---------------------------------------------------------

    def accept(self, companion_id: str, proposal_id: str, payload: dict) -> dict:
        companion_id = self.personality._identifier(companion_id)
        proposal_id = self.personality._identifier(proposal_id)
        data = self.personality._validate(PersonalityEvolutionDecision, payload).model_dump(
            mode="json"
        )
        if not self.policy.enabled:
            raise PersonalityError(422, "evolution_disabled", "Personality evolution is disabled")

        def action(connection, now, _):
            self._reap(connection, companion_id, now)
            row = self._proposal_row(connection, companion_id, proposal_id)
            if row["status"] != "proposed":
                raise PersonalityError(409, "proposal_not_proposed", "Proposal is not open")
            if row["revision"] != data["expected_proposal_revision"]:
                raise PersonalityError(409, "revision_conflict", "Proposal revision changed")
            profile = self.personality._profile_row(connection, companion_id)
            current = profile["revision"] if profile is not None else 0
            if current != data["expected_personality_revision"]:
                raise PersonalityError(409, "revision_conflict", "Personality revision changed")
            if row["base_personality_revision"] != current:
                raise PersonalityError(
                    409, "proposal_stale", "Proposal is based on an older profile"
                )
            if datetime.fromisoformat(row["expires_at"]).astimezone(now.tzinfo) <= now:
                raise PersonalityError(409, "proposal_stale", "Proposal has expired")
            patch = json.loads(row["patch_json"])
            self._validate_patch_bounds(patch)
            evidence = json.loads(row["evidence_json"])
            self._validate_evidence(connection, companion_id, evidence, now)
            definition = (
                json.loads(profile["definition_json"])
                if profile is not None
                else PersonalityDefinition().model_dump(mode="json")
            )
            new_definition = self._apply_patch(definition, patch)
            try:
                validated = PersonalityDefinition.model_validate(new_definition).model_dump(
                    mode="json"
                )
            except ValidationError as exc:
                raise PersonalityError(
                    409,
                    "patch_rejected",
                    f"Patch produced an invalid definition: {exc.errors()[0]['msg']}",
                ) from exc
            revision = current + 1
            if revision > MAX_VERSION:
                raise PersonalityError(
                    409, "version_exhausted", "The personality revision cannot grow"
                )
            self.personality._write_profile(connection, companion_id, revision, validated, now)
            self.personality._record_revision(
                connection, companion_id, revision, validated, "evolution", proposal_id, now
            )
            self.personality._emit(
                connection,
                companion_id,
                now,
                "companion.personality_updated",
                {"personality_revision": revision},
            )
            decided_at = _timestamp(now)
            connection.execute(
                """UPDATE personality_evolution_proposals SET status = 'accepted', decided_at = ?,
                    updated_at = ? WHERE proposal_id = ?""",
                (decided_at, decided_at, proposal_id),
            )
            self.personality._emit(
                connection,
                companion_id,
                now,
                "companion.personality_evolution_accepted",
                {
                    "proposal_id": proposal_id,
                    "personality_revision": revision,
                    "proposal_revision": row["revision"],
                },
            )
            accepted = self._proposal_row(connection, companion_id, proposal_id)
            updated = self.personality._profile_row(connection, companion_id)
            return {
                "companion_id": companion_id,
                "proposal": self._snapshot(accepted),
                "personality": self.personality._profile_snapshot(companion_id, updated),
                "decided_at": decided_at,
            }

        return self.personality._write(
            companion_id,
            "personality.evolution.accept",
            {**data, "proposal_id": proposal_id},
            action,
        )

    def reject(self, companion_id: str, proposal_id: str, payload: dict) -> dict:
        companion_id = self.personality._identifier(companion_id)
        proposal_id = self.personality._identifier(proposal_id)
        data = self.personality._validate(PersonalityEvolutionRejection, payload).model_dump(
            mode="json"
        )

        def action(connection, now, _):
            self._reap(connection, companion_id, now)
            row = self._proposal_row(connection, companion_id, proposal_id)
            if row["status"] != "proposed":
                raise PersonalityError(409, "proposal_not_proposed", "Proposal is not open")
            if row["revision"] != data["expected_proposal_revision"]:
                raise PersonalityError(409, "revision_conflict", "Proposal revision changed")
            decided_at = _timestamp(now)
            connection.execute(
                """UPDATE personality_evolution_proposals SET status = 'rejected', decided_at = ?,
                    updated_at = ? WHERE proposal_id = ?""",
                (decided_at, decided_at, proposal_id),
            )
            self.personality._emit(
                connection,
                companion_id,
                now,
                "companion.personality_evolution_rejected",
                {
                    "proposal_id": proposal_id,
                    "proposal_revision": row["revision"],
                    "reason": data.get("reason"),
                },
            )
            rejected = self._proposal_row(connection, companion_id, proposal_id)
            profile = self.personality._profile_row(connection, companion_id)
            return {
                "companion_id": companion_id,
                "proposal": self._snapshot(rejected),
                "personality": self.personality._profile_snapshot(companion_id, profile),
                "decided_at": decided_at,
            }

        return self.personality._write(
            companion_id,
            "personality.evolution.reject",
            {**data, "proposal_id": proposal_id},
            action,
        )

    # -- patch and evidence ------------------------------------------------

    def _validate_patch_bounds(self, patch) -> None:
        if len(patch) > self.policy.max_operations_per_proposal:
            raise PersonalityError(409, "patch_rejected", "Too many operations in the patch")
        traits = [operation for operation in patch if operation["op"] == "adjust_trait"]
        if len(traits) > self.policy.max_traits_per_proposal:
            raise PersonalityError(409, "patch_rejected", "Too many trait changes in the patch")
        for operation in traits:
            if abs(operation["delta"]) > self.policy.max_trait_step + 1e-9:
                raise PersonalityError(409, "patch_rejected", "Trait step exceeds the policy limit")

    def _apply_patch(self, definition, patch):
        result = json.loads(json.dumps(definition))
        for operation in patch:
            kind = operation["op"]
            if kind == "adjust_trait":
                field = operation["field"]
                current = result["traits"][field]
                if abs(current - operation["before"]) > 1e-9:
                    raise PersonalityError(409, "proposal_stale", "Trait base value changed")
                after = current + operation["delta"]
                if not 0 <= after <= 1:
                    raise PersonalityError(409, "patch_rejected", "Trait change leaves 0..1")
                result["traits"][field] = round(after, 6)
            elif kind == "set_text":
                field = operation["field"]
                if result[field] != operation["before"]:
                    raise PersonalityError(409, "proposal_stale", "Text base value changed")
                result[field] = operation["after"]
            elif kind == "set_style":
                field = operation["field"]
                if result["speech_style"][field] != operation["before"]:
                    raise PersonalityError(409, "proposal_stale", "Style base value changed")
                result["speech_style"][field] = operation["after"]
            else:
                field = operation["field"]
                target = result["speech_style"]["habits"] if field == "habits" else result[field]
                if field == "boundaries" and self._removes_item(patch, "boundaries"):
                    raise PersonalityError(409, "patch_rejected", "Boundaries cannot be removed")
                if operation["item"] in target:
                    raise PersonalityError(409, "patch_rejected", "Item already present")
                target.append(operation["item"])
        return result

    @staticmethod
    def _removes_item(patch, field) -> bool:
        return any(
            operation["op"] == "remove_item" and operation["field"] == field for operation in patch
        )

    def _validate_evidence(self, connection, companion_id, evidence, now) -> None:
        references = evidence.get("memory_refs", [])
        if references:
            try:
                self.memory.validate_references(connection, companion_id, references, now)
            except MemoryError as exc:
                raise PersonalityError(
                    409, "evidence_invalidated", "Cited memory is no longer valid"
                ) from exc
        for event_id in evidence.get("event_refs", []):
            found = connection.execute(
                "SELECT 1 FROM events WHERE event_id = ? AND companion_id = ?",
                (event_id, companion_id),
            ).fetchone()
            if found is None:
                raise PersonalityError(
                    409, "evidence_invalidated", "A cited event no longer exists"
                )
        for reference in evidence.get("correction_refs", []):
            row = connection.execute(
                "SELECT * FROM emotion_experiences WHERE event_id = ? AND companion_id = ?",
                (reference["event_id"], companion_id),
            ).fetchone()
            if row is None or row["invalidated"]:
                raise PersonalityError(
                    409, "evidence_invalidated", "A cited experience was invalidated"
                )
            if self.emotion._forgotten(connection, companion_id, row["completed_event_id"]):
                raise PersonalityError(
                    409, "evidence_invalidated", "A cited experience source was forgotten"
                )
            _, info = self.emotion._correction(connection, row)
            if info is None or info["revision"] != reference["revision"]:
                raise PersonalityError(409, "evidence_invalidated", "A cited correction changed")
