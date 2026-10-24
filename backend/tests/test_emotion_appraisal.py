"""Local appraisal tests exercise evidence boundaries rather than word counts."""

import json

import pytest
from pydantic import ValidationError
from virtual_life.emotion_appraisal import AppraisalVector, appraise

TRAITS = {"warmth": 0.8, "curiosity": 0.6}


@pytest.mark.parametrize(
    "message",
    [
        "我今天不难过",
        "他说：我很难过",
        "谢谢你？",
        "我不想说谢谢你",
        "她今天很累",
        "谢谢你把事情搞砸了",
        "如果我很难过",
        "我很难过吗",
        "我可能有点难过，但也说不清",
        "“我很难过”",
        "我很难过才怪",
        "难过这个词是什么意思",
        "我以前说过谢谢你",
        "我想知道‘谢谢你’是不是讽刺",
        "我想知道如果我很难过会怎样",
        "我很难过\n谢谢你",
        "thanks for ruining everything",
        "",
    ],
)
def test_complex_or_unattributed_text_stays_neutral(message):
    result = appraise(message, TRAITS)
    assert result["reaction"] == "neutral"
    assert result["reason"] == "no_explicit_cue"
    assert result["confidence"] == 0
    assert result["uncertainty"] == result["appraisal"]["uncertainty"] == 1
    assert result["attribution"] == "unspecified"
    assert all(value == 0 for value in result["delta"].values())


@pytest.mark.parametrize("message", ["我最近有点难过", "我现在非常难过", "我特别焦虑"])
def test_explicit_self_expression_has_bounded_vector_and_no_resource_charge(message):
    result = appraise(message, TRAITS)
    assert result["reaction"] == "concern"
    assert result["attribution"] == "self"
    assert result["appraisal_version"] == "rules_v2"
    vector = AppraisalVector.model_validate(result["appraisal"])
    assert vector.pleasantness < 0 and vector.attention > 0
    assert 0 < result["uncertainty"] < 1
    assert result["delta"]["energy"] == result["delta"]["social_battery"] == 0
    assert all(-0.3 <= value <= 0.3 for value in result["delta"].values())
    assert message not in json.dumps(result, ensure_ascii=False)


def test_degree_modifies_appraisal_then_continuous_response():
    mild = appraise("我最近有点难过", TRAITS)
    strong = appraise("我现在非常难过", TRAITS)
    assert abs(mild["appraisal"]["pleasantness"]) < abs(strong["appraisal"]["pleasantness"])
    assert mild["appraisal"]["effort"] < strong["appraisal"]["effort"]
    assert abs(mild["delta"]["valence"]) < abs(strong["delta"]["valence"])
    assert mild["delta"]["arousal"] < strong["delta"]["arousal"]


def test_clear_legacy_phrases_keep_response_magnitudes():
    scale = 0.7 + 0.3 * TRAITS["warmth"]
    thanks = appraise("谢谢你陪我读书", TRAITS)
    assert thanks["delta"]["valence"] == pytest.approx(0.24 * scale)
    assert thanks["delta"]["arousal"] == pytest.approx(0.08)
    assert thanks["delta"]["control"] == pytest.approx(0.03)
    distress = appraise("我今天很难过", TRAITS)
    assert distress["delta"]["valence"] == pytest.approx(-0.06 * scale)
    assert distress["delta"]["arousal"] == pytest.approx(0.1 * scale)
    curious = appraise("我想知道为什么海是蓝的？", TRAITS)
    assert curious["reaction"] == "curiosity"
    assert curious["delta"]["valence"] == pytest.approx(0.04)
    assert curious["delta"]["arousal"] == pytest.approx(0.12 * TRAITS["curiosity"])


@pytest.mark.parametrize("message", ["先安静陪我读一会儿", "请不要说话", "保持安静！"])
def test_quiet_is_an_explicit_request_not_distress(message):
    result = appraise(message, TRAITS)
    assert result["reaction"] == "quiet"
    assert result["reason"] == "quiet_request"
    assert result["confidence"] == 1
    assert result["delta"]["arousal"] == pytest.approx(-0.15)
    assert result["delta"]["valence"] == 0


def test_thanks_punctuation_and_english_are_supported_without_broad_substrings():
    assert appraise("  谢谢你！  ", TRAITS)["reaction"] == "joy"
    assert appraise("Thanks for reading with me!", TRAITS)["reaction"] == "joy"
    assert appraise("我很好奇为什么我今天很难过", TRAITS)["reaction"] == "curiosity"


def test_default_vector_is_neutral_and_complete():
    assert AppraisalVector().model_dump() == {
        "pleasantness": 0,
        "effort": 0,
        "uncertainty": 0,
        "attention": 0,
        "agency": 0,
        "control": 0.5,
    }


@pytest.mark.parametrize("dimension", AppraisalVector.model_fields)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "0.5", True])
def test_vector_rejects_nonfinite_and_coerced_values(dimension, value):
    with pytest.raises(ValidationError):
        AppraisalVector.model_validate({dimension: value})


@pytest.mark.parametrize("dimension", AppraisalVector.model_fields)
def test_vector_limits_each_dimension(dimension):
    lower = -1 if dimension in ("pleasantness", "agency") else 0
    for value in (lower - 0.01, 1.01):
        with pytest.raises(ValidationError):
            AppraisalVector.model_validate({dimension: value})
    assert getattr(AppraisalVector.model_validate({dimension: lower}), dimension) == lower
    assert getattr(AppraisalVector.model_validate({dimension: 1}), dimension) == 1
    with pytest.raises(ValidationError):
        AppraisalVector.model_validate({"unknown": 0.5})
