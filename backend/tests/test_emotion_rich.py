"""Richer appraisal: the six new emotional reactions and their affect signatures.

These lock in the extended cue vocabulary and the trait-modulated projection so
future refactors cannot silently collapse the richer emotion space back to five
reactions.
"""

import pytest
from virtual_life.emotion_appraisal import appraise

TRAITS = {"warmth": 0.8, "curiosity": 0.6, "playfulness": 0.9}
BASE = {"warmth": 0.8, "curiosity": 0.6}  # minimal traits (public contract)


@pytest.mark.parametrize(
    "message,reaction",
    [
        ("哈哈哈哈笑死我了", "amusement"),
        ("太好笑了", "amusement"),
        ("开个玩笑", "amusement"),
        ("逗你玩呢", "amusement"),
        ("太棒了", "excitement"),
        ("好期待", "excitement"),
        ("我激动坏了", "excitement"),
        ("竟然", "surprise"),
        ("没想到", "surprise"),
        ("哇", "surprise"),
        ("好可怜", "sympathy"),
        ("太心疼了", "sympathy"),
        ("气死我了", "annoyance"),
        ("真无语", "annoyance"),
        ("我做到了", "pride"),
        ("我终于做到了", "pride"),
        ("看我的厉害", "pride"),
    ],
)
def test_new_cues_recognised(message, reaction):
    result = appraise(message, TRAITS)
    assert result["reaction"] == reaction
    assert result["confidence"] > 0
    assert result["appraisal_version"] == "rules_v2"


def test_minimal_traits_still_work_for_new_reactions():
    # The public appraisal contract passes only warmth/curiosity; a missing
    # playfulness key must not crash the projection.
    result = appraise("哈哈哈哈", BASE)
    assert result["reaction"] == "amusement"
    assert result["delta"]["valence"] != 0


@pytest.mark.parametrize(
    "message",
    [
        "我今天不难过",
        "他说：我很可怜",
        "气死我了？",  # question marks invalidate an annoyance claim
        "我做到了才怪",
        "哈哈这个词是什么意思",
        "好期待你回来吗",  # trailing question is not a clean excitement cue
        "没想到你也这样",  # trailing clause makes it non-anchored
    ],
)
def test_new_cues_require_anchored_whole_message(message):
    result = appraise(message, TRAITS)
    assert result["reaction"] == "neutral"
    assert result["confidence"] == 0


@pytest.mark.parametrize(
    "message,axis,higher",
    [
        ("哈哈哈哈", "valence", "joy"),  # amusement vs joy: check magnitude sign
        ("气死我了", "valence", None),
        ("好期待", "arousal", None),
    ],
)
def test_new_reactions_have_bounded_signed_deltas(message, axis, higher):
    result = appraise(message, TRAITS)
    for key, value in result["delta"].items():
        assert -0.3 <= value <= 0.3
    if message == "气死我了":
        assert result["delta"]["valence"] < 0
    if message == "好期待":
        assert result["delta"]["arousal"] > 0


def test_annoyance_is_negative_but_not_distress_graded():
    annoyance = appraise("气死我了", TRAITS)
    assert annoyance["delta"]["valence"] < 0
    assert annoyance["attribution"] == "situation"


def test_sympathy_stays_kind_but_softens_control():
    sympathy = appraise("好可怜", TRAITS)
    assert sympathy["delta"]["valence"] > 0
    assert sympathy["delta"]["control"] <= 0.3
