"""Conservative local cue appraisal; parameters are engineering choices, not measurements."""

import re

from pydantic import Field

from .models import RequestModel


class AppraisalVector(RequestModel):
    pleasantness: float = Field(default=0, ge=-1, le=1)
    effort: float = Field(default=0, ge=0, le=1)
    uncertainty: float = Field(default=0, ge=0, le=1)
    attention: float = Field(default=0, ge=0, le=1)
    agency: float = Field(default=0, ge=-1, le=1)
    control: float = Field(default=0.5, ge=0, le=1)


# Anchored templates establish who is speaking and reject extra clauses. Never
# strip negation, question marks, quotation marks, or a sarcastic trailing clause.
_QUIET = re.compile(
    r"(?:请)?(?:先|现在)?(?:安静陪我(?:读(?:一会儿)?|一会儿)?|别说话|不要说话|保持安静)"
)
_THANKS = re.compile(
    r"谢谢(?:你)?(?:陪我(?:读书|阅读|共读)?)?|有你陪着真好|一起读书很开心"
    r"|thanks(?: for reading with me)?",
    re.I,
)
_DISTRESS = re.compile(
    r"(?:我)?(?:今天|最近|现在|这会儿)?(?P<degree>有一点|有点|很|非常|特别|十分)?"
    r"(?:难过|难受|焦虑|累|疲惫)"
)
_EXPLORATION = re.compile(r"(?:我很好奇|我想知道|一起看看)[^\n]{0,200}")
_UNCERTAIN_EXPLORATION = re.compile(r"[‘’“”\"'「」『』]|如果|假如|要是|据说|他说|她说|才怪|呵呵")
_INTENSITY = {
    None: 1.0,
    "有一点": 0.6,
    "有点": 0.6,
    "很": 1.0,
    "非常": 1.25,
    "特别": 1.4,
    "十分": 1.25,
}


def _project(vector, reaction, traits):
    """Project the cue's interpretation into the companion's response.

    These small cue-specific engineering gains preserve legacy clear phrases.
    Distress elicits mild concern, rather than copying the speaker's mood.
    Effort/agency/uncertainty describe interpretation; they do not charge resources
    or assert causality. Actual reply consumption is recorded elsewhere.
    """
    warmth = 0.7 + 0.3 * traits["warmth"]
    valence = arousal = control = 0.0
    if reaction == "joy":
        valence = 0.3 * vector.pleasantness * warmth
        arousal = 0.2 * vector.attention
        control = 0.3 * (vector.control - 0.5)
    elif reaction == "concern":
        valence = 0.2 * vector.pleasantness * warmth
        arousal = 0.2 * vector.attention * warmth
    elif reaction == "curiosity":
        valence = 0.2 * vector.pleasantness
        arousal = 0.2 * vector.attention * traits["curiosity"]
    elif reaction == "quiet":
        arousal = -0.3 * vector.attention
    return {
        "valence": max(-0.3, min(0.3, valence)),
        "arousal": max(-0.3, min(0.3, arousal)),
        "control": max(-0.3, min(0.3, control)),
        "energy": 0.0,
        "social_battery": 0.0,
    }


def appraise(message, traits):
    """Return bounded, text-free evidence for an explicit whole-message cue.

    Uncertainty is rule interpretation uncertainty, not a psychological score.
    Attribution identifies the source of the cue, not a verified causal claim.
    Unrecognized or complex statements deliberately remain neutral.
    """
    text = message.strip().rstrip("。！! ")
    reaction, reason, confidence = "neutral", "no_explicit_cue", 0.0
    uncertainty, attribution = 1.0, "unspecified"
    vector = AppraisalVector(uncertainty=1.0)
    distress = _DISTRESS.fullmatch(text)
    if _QUIET.fullmatch(text):
        reaction, reason, confidence = "quiet", "quiet_request", 1.0
        uncertainty, attribution = 0.0, "self"
        vector = AppraisalVector(attention=0.5, agency=1.0)
    elif distress:
        intensity = _INTENSITY[distress["degree"]]
        reaction, reason, confidence = "concern", "explicit_distress", 0.85
        uncertainty, attribution = 0.15, "self"
        vector = AppraisalVector(
            pleasantness=-0.3 * intensity,
            effort=0.5 * intensity,
            uncertainty=uncertainty,
            attention=0.5 * intensity,
            agency=1.0,
        )
    elif _THANKS.fullmatch(text):
        reaction, reason, confidence = "joy", "explicit_appreciation", 0.9
        uncertainty, attribution = 0.1, "other"
        vector = AppraisalVector(
            pleasantness=0.8, attention=0.4, uncertainty=uncertainty, agency=-1.0, control=0.6
        )
    elif _EXPLORATION.fullmatch(text) and not _UNCERTAIN_EXPLORATION.search(text):
        reaction, reason, confidence = "curiosity", "explicit_question", 0.7
        uncertainty, attribution = 0.3, "self"
        vector = AppraisalVector(
            pleasantness=0.2, uncertainty=uncertainty, attention=0.6, agency=1.0
        )
    return {
        "reaction": reaction,
        "reason": reason,
        "confidence": confidence,
        "delta": _project(vector, reaction, traits),
        "appraisal": vector.model_dump(),
        "appraisal_version": "rules_v2",
        "uncertainty": uncertainty,
        "attribution": attribution,
    }
