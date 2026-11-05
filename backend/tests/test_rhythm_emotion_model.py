"""Local-time rhythm helpers, inner-life rhythm modulation, and the
model-assisted emotion appraisal channel (legal / invalid / timeout)."""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from virtual_life.emotion import EmotionPolicy, EmotionService
from virtual_life.inner_life import InnerLifePolicy, InnerLifeService
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.rhythm import day_part, rhythm_snapshot

# ---------------------------------------------------------------- rhythm ----


def test_day_part_boundaries():
    assert day_part(0) == "深夜"
    assert day_part(4) == "深夜"
    assert day_part(5) == "清晨"
    assert day_part(8) == "上午"
    assert day_part(12) == "下午"
    assert day_part(17) == "傍晚"
    assert day_part(19) == "晚上"
    assert day_part(23) == "深夜"


def test_rhythm_snapshot_none_now():
    assert rhythm_snapshot(None, "Asia/Shanghai") is None


def test_rhythm_snapshot_label_and_flags():
    # 02:05 local (UTC 18:05 the previous day) -> predawn deep night.
    now = datetime(2026, 1, 1, 18, 5, tzinfo=timezone.utc)
    snap = rhythm_snapshot(now, "Asia/Shanghai")
    assert snap["hour"] == 2
    assert snap["minute"] == 5
    assert snap["day_part"] == "深夜"
    assert snap["is_deep_night"] is True
    assert snap["is_predawn"] is True
    assert snap["label"] == "现在是深夜（02:05）"


def test_rhythm_snapshot_daytime():
    now = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)  # 14:00 local
    snap = rhythm_snapshot(now, "Asia/Shanghai")
    assert snap["is_deep_night"] is False
    assert snap["is_predawn"] is False
    assert snap["day_part"] == "下午"


def test_rhythm_snapshot_invalid_timezone_falls_back_to_host():
    now = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    snap = rhythm_snapshot(now, "Not/AZone")
    assert snap is not None
    assert 0 <= snap["hour"] <= 23
    assert snap["label"].startswith("现在是")


# -------------------------------------------------- inner_life modulation ----

_TRAITS = {"curiosity": 0.8, "initiative": 0.7, "warmth": 0.6}


def _inner_service(rhythm_enabled):
    # _state() only reads self.policy; a bare instance avoids the full
    # service construction (store, schema migration, coordinator wiring).
    service = InnerLifeService.__new__(InnerLifeService)
    service.policy = InnerLifePolicy(rhythm_enabled=rhythm_enabled, rhythm_timezone="Asia/Shanghai")
    return service


def _inner_context(now):
    return {
        "profile": {"definition": {"traits": _TRAITS}},
        "emotion": {
            "effective_state": {"social_battery": 0.8, "energy": 0.8, "mood": "calm"},
            "state": {"valence": 0.1},
        },
        "slow": {"familiarity": 0.9, "valence_inertia": 0.5, "relationship_warmth": 0.7},
        "idle_seconds": 1800,
        "attention": None,
        "now": now,
    }


def test_inner_state_deep_night_quiets_social_and_raises_rest():
    # 14:00 local vs 02:00 local (deep night + predawn).
    day = _inner_service(True)._state(
        _inner_context(datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc))
    )
    night = _inner_service(True)._state(
        _inner_context(datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc))
    )
    assert night["needs"].social < day["needs"].social - 0.2
    assert night["needs"].rest > day["needs"].rest + 0.1


def test_inner_state_rhythm_disabled_is_time_invariant():
    day = _inner_service(False)._state(
        _inner_context(datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc))
    )
    night = _inner_service(False)._state(
        _inner_context(datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc))
    )
    assert night["needs"] == day["needs"]


# ----------------------------------------------- model appraise channel ----


class Estimator:
    def estimate(self, text):
        return max(1, len(text) // 4)


class FakeMemory:
    def __init__(self):
        self.token_estimator = Estimator()
        self.reserved = []
        self.settled = []
        self.released = []

    def reserve_model_tokens(self, companion_id, reservation):
        self.reserved.append((companion_id, reservation))
        return {"new_reservation": True}

    def settle_model_tokens(self, companion_id, task_id, usage):
        self.settled.append((companion_id, task_id, usage))

    def release_model_tokens(self, companion_id, task_id):
        self.released.append((companion_id, task_id))


LEGAL = json.dumps(
    {
        "reaction": "sympathy",
        "confidence": 0.8,
        "appraisal": {
            "pleasantness": -0.2,
            "effort": 0.3,
            "attention": 0.5,
            "agency": 1.0,
            "uncertainty": 0.2,
            "control": 0.4,
        },
        "uncertainty": 0.2,
        "attribution": "other",
        "rationale": "用户表达疲惫与无力感",
    },
    ensure_ascii=False,
)


class Provider:
    def __init__(self, text=LEGAL, delay=0.0):
        self.text = text
        self.delay = delay
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        return ModelResult(self.text, "fake", ModelUsage(50, 10))


def _emotion_service(provider, policy):
    memory = FakeMemory()
    service = EmotionService.__new__(EmotionService)
    service.personality = None
    service.memory = memory
    service.policy = policy
    service.provider = provider
    service.model_name = "fake"
    return service, memory


_EVENT = {
    "event_id": "evt_rhythm_1",
    "companion_id": "comp",
    "payload": {"user_message": "今天累坏了，什么都做不动"},
}
_PROFILE = {"definition": {"traits": _TRAITS}}
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _policy(**kwargs):
    return EmotionPolicy(appraise_model_enabled=True, **kwargs)


def test_appraise_model_legal_json_wins():
    provider = Provider()
    service, memory = _emotion_service(provider, _policy())
    result = service._appraise(_EVENT, _PROFILE, _NOW)
    assert result["reason"] == "model_v1"
    assert result["appraisal_version"] == "model_v1"
    assert result["reaction"] == "sympathy"
    assert result["confidence"] == 0.8
    assert set(result["delta"]) == {
        "valence",
        "arousal",
        "control",
        "energy",
        "social_battery",
    }
    assert result["attribution"] == "other"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.purpose == "emotion_appraise"
    assert request.task_id.startswith("emo-")
    assert memory.settled and not memory.released


def test_appraise_model_invalid_json_falls_back_to_rules():
    provider = Provider(text="这不是 JSON {")
    service, memory = _emotion_service(provider, _policy())
    result = service._appraise(_EVENT, _PROFILE, _NOW)
    assert result["appraisal_version"].startswith("rules_")
    assert result["reason"] != "model_v1"
    assert provider.requests  # the model was attempted, tokens settled


def test_appraise_model_invalid_reaction_falls_back_to_rules():
    payload = json.loads(LEGAL)
    payload["reaction"] = "hysteria"
    provider = Provider(text=json.dumps(payload, ensure_ascii=False))
    service, _ = _emotion_service(provider, _policy())
    result = service._appraise(_EVENT, _PROFILE, _NOW)
    assert result["appraisal_version"].startswith("rules_")


def test_appraise_model_timeout_falls_back_to_rules():
    provider = Provider(delay=0.5)
    service, _ = _emotion_service(provider, _policy(appraise_model_timeout_seconds=0.05))
    result = service._appraise(_EVENT, _PROFILE, _NOW)
    assert result["appraisal_version"].startswith("rules_")
    assert provider.requests  # attempt was made, then abandoned


def test_appraise_model_disabled_does_not_call_provider():
    provider = Provider()
    service, _ = _emotion_service(provider, EmotionPolicy())
    result = service._appraise(_EVENT, _PROFILE, _NOW)
    assert result["appraisal_version"].startswith("rules_")
    assert provider.requests == []


def test_inner_life_policy_rhythm_defaults():
    policy = InnerLifePolicy()
    assert policy.rhythm_enabled is True
    assert policy.rhythm_timezone == "Asia/Shanghai"


def test_prompt_builder_accepts_rhythm_params():
    from virtual_life.personality_prompt import PersonalityPromptBuilder

    builder = PersonalityPromptBuilder(
        SimpleNamespace(token_estimator=Estimator()),
        token_estimator=Estimator(),
        rhythm_enabled=True,
        rhythm_timezone="Asia/Shanghai",
    )
    assert builder.rhythm_enabled is True
    assert builder.rhythm_timezone == "Asia/Shanghai"


def test_appraise_model_name_override_reaches_service(tmp_path):
    from fastapi.testclient import TestClient
    from virtual_life.api import create_app
    from virtual_life.emotion import EmotionPolicy
    from virtual_life.settings import Settings

    def build(name_override):
        app = create_app(
            Settings(
                database_path=str(tmp_path / f"emo-{name_override or 'default'}.sqlite"),
                api_token="test",
                model_name="slow-model",
                model_base_url="http://model.invalid",
                emotion_policy=EmotionPolicy(
                    appraise_model_enabled=True,
                    appraise_model_name=name_override,
                ),
            ),
            model_provider=Provider(),
        )
        with TestClient(app):
            return app.state.emotion.model_name

    assert build("") == "slow-model"
    assert build("fast-model") == "fast-model"


# ------------------------------------------------------- sleep phase ----


def test_sleep_phase_predawn_and_falling_asleep_and_day():
    service = _inner_service(True)
    # 02:00 local (UTC 18:00) -> core sleep.
    assert service.sleep_phase(now=datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)) == "asleep"
    # 23:30 local (UTC 15:30) -> falling asleep.
    assert service.sleep_phase(now=datetime(2025, 12, 31, 15, 30, tzinfo=timezone.utc)) == "drowsy"
    # 14:00 local -> awake.
    assert service.sleep_phase(now=datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)) == "awake"


def test_sleep_phase_ignores_wake_threshold():
    # The gate phase never reads urge: she does not wake herself up.
    service = _inner_service(True)
    now = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)  # 02:00 local
    assert service.sleep_phase(now=now) == "asleep"


def test_effective_phase_can_be_woken_by_strong_urge():
    service = _inner_service(True)
    now = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)  # 02:00 local
    assert service._phase({"urge": 0.3}, now) == "asleep"
    assert service._phase({"urge": 0.7}, now) == "drowsy"
    # 23:30 local is drowsy regardless of urge.
    evening = datetime(2025, 12, 31, 15, 30, tzinfo=timezone.utc)
    assert service._phase({"urge": 0.9}, evening) == "drowsy"
    # Daytime is awake.
    day = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    assert service._phase({"urge": 0.1}, day) == "awake"


def test_sleep_phase_rhythm_disabled_never_sleeps():
    service = _inner_service(False)
    assert service.sleep_phase(now=datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)) == "awake"
    assert (
        service._phase({"urge": 0.1}, datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)) == "awake"
    )
