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
    r"(?:请)?(?:先|现在)?"
    r"(?:安静(?:陪我)?(?:读(?:一会儿|一会)?|一会)?"
    r"|别说话"
    r"|不要说话"
    r"|保持安静"
    r"|别出声"
    r"|别打扰(?:我)?"
    r"|静一静"
    r"|让我静一静)"
)
_THANKS = re.compile(
    r"谢(?:谢)?(?:你)?"
    r"(?:的陪伴|听我说话|的倾听|陪我(?:读书|阅读|共读|读会儿书|说说话))?"
    r"|感谢(?:你)?(?:的陪伴)?"
    r"|(?:有|多亏)你(?:在|陪着|陪伴)?(?:真好)?"
    r"|一起(?:读书|阅读|共读)(?:很开心|好开心)?"
    r"|thanks?(?: for reading with me)?(?: so much)?"
    r"|thank you"
    r"|i appreciate you"
    r"|i'?m grateful",
    re.I,
)
_DISTRESS = re.compile(
    r"(?:我)?"
    r"(?:今天|最近|现在|这会儿|这两天|这段时间)?"
    r"(?P<degree>有一点|有点|挺|很|非常|特别|十分|蛮)?"
    r"(?:难过|难受|焦虑|累|疲惫|低落|郁闷|崩溃|心累|烦躁|烦|想哭|心里堵)"
)
_EXPLORATION = re.compile(
    r"(?:我(?:有点|有些|很|超)?"
    r"(?:好奇|想知道|想了解|想看看|想看|想问问|问问)"
    r"|(?:让|帮)(?:我们|我|你们)?(?:一起)?(?:看看|聊聊)"
    r"|我们(?:一起)?(?:看看|聊聊)"
    r"|一起(?:看看|聊聊))"
    r"[^\n]{0,200}"
)
_UNCERTAIN_EXPLORATION = re.compile(r"[‘’“”\"'「」『』]|如果|假如|要是|据说|他说|她说|才怪|呵呵")

# Richer emotional cues. Each is a complete anchored phrase (fullmatch), so a
# complex sentence never accidentally triggers a single-emotion reading. These
# extend, never replace, the conservative v2 set.
_AMUSEMENT = re.compile(
    r"(?:哈哈+|嘿嘿|呵呵)"
    r"|(?:哈哈+|嘿嘿|呵呵)*(?:(?:笑死(?:我了|我了)?|太好笑了?|好搞笑|笑不活了|有被笑到|逗死我了)"
    r"|(?:开个玩笑|开玩笑了?|逗你玩(?:呢)?|逗你(?:的)?)"
    r"|(?:你|这)(?:太|真|好)?(?:好笑了|逗|幽默)"
    r"|(?:这个|那)(?:段子|笑话|视频|图)(?:真|太|好)?(?:好笑|逗|有意思|搞笑))"
)
_EXCITEMENT = re.compile(
    r"(?:太棒了|太好了|好耶|真棒|太开心了|太激动了|好期待|真期待|太好了吧)"
    r"|(?:我)?(?:超|好|真)?(?:开心|激动|期待)(?:死了|坏了|极了)?"
)
_SURPRISE = re.compile(r"(?:竟然|居然|没想到|真的假的|不会吧|这么突然|天哪|哇|我去|好突然|太突然)")
_SYMPATHY = re.compile(
    r"(?:好可怜|太惨了?|真惨|好心疼|太心疼了|心疼(?:你|他|她|它)?|替他(?:难过|担心)|为她(?:难过|担心))"
)
_ANNOYANCE = re.compile(r"(?:我)?(?:烦死了|烦透了|气死我了|气死|真无语|服了|受不了|好烦|烦得慌)")
_PRIDE = re.compile(
    r"(?:我)?(?:终于)?(?:做到了|成功了|赢了|通过了|完成了|达成了|考过了|拿到了)"
    r"|我(?:厉害吧|好棒|棒不棒|牛不牛)"
    r"|看(?:我)?(?:多)?(?:的)?厉害"
)
_INTENSITY = {
    None: 1.0,
    "有一点": 0.6,
    "有点": 0.6,
    "挺": 1.1,
    "蛮": 1.1,
    "很": 1.0,
    "非常": 1.25,
    "特别": 1.4,
    "十分": 1.25,
}

# Inclusion-based fallback cues: a keyword inside a longer sentence still
# carries an emotional signal, but weaker. The fullmatch rules above stay the
# primary, high-confidence path; these catch "今天好累啊，不想动" or
# "你猜怎么着？" that strict anchoring would leave neutral. Order = priority.
_INCLUSION_CUES: tuple[tuple[re.Pattern, str, str, float, AppraisalVector], ...] = (
    (
        re.compile(r"(累|疲惫|好困|困死|没精神|提不起劲|不想动|好丧|心累|烦人|压力大|好难)"),
        "concern",
        "explicit_distress",
        0.55,
        AppraisalVector(
            pleasantness=-0.25, effort=0.4, attention=0.4, agency=0.8, uncertainty=0.35
        ),
    ),
    (
        re.compile(r"(哈哈|嘿嘿|笑死|好笑|逗死|搞笑|有意思|逗你玩|开玩笑)"),
        "amusement",
        "explicit_appreciation",
        0.5,
        AppraisalVector(pleasantness=0.6, attention=0.5, agency=-0.6, uncertainty=0.3, control=0.4),
    ),
    (
        # Question particles (吗/呢/吧) alone do not count as a cue: a trailing
        # question invalidates the anchored reading rather than enabling one.
        re.compile(r"(怎么|为什么|什么|多少|哪|是不是|有没有|啥)"),
        "curiosity",
        "explicit_question",
        0.45,
        AppraisalVector(pleasantness=0.15, attention=0.5, agency=0.8, uncertainty=0.4),
    ),
)


# Guards for the inclusion fallback: the cue is only trusted when the message
# reads as a genuine, present, first-person, unhedged expression. Negated,
# third-person, quoted, hypothetical or meta-linguistic sentences stay neutral
# (this is the deliberate conservative contract, not a false-negative).
# A negation only blocks when it directly modifies an emotion word ("不累",
# "没这么开心"); a bare "不" inside "不想动" is part of the expression itself.
_NEGATION = re.compile(
    r"(不|没|别|算不上|并不|未必|才怪)[^，。！？]{0,3}(累|困|烦|丧|难|开心|高兴|好笑|有意思)"
)
# Third-person reference to someone else's state ("她说她好累"). "你" is
# excluded: the user speaking to the companion naturally addresses them.
_THIRD_PERSON = re.compile(r"他说|她说|他们|她们|他|她|它")
_QUOTED = re.compile(r"[‘’“”\"'「」『』]")
_HEDGE = re.compile(r"可能|也许|大概|说不清|好像|似乎|有点难说|如果|假如|要是|以前|之前|曾经")
# Meta-linguistic / self-referential questions about an emotion word itself
# ("难过这个词是什么意思", "我很难过吗", "累不累", "是不是讽刺") are not
# an emotional expression — they ask about the word, not about the feeling.
# Only complete meta phrases match; a bare "这" or "吗" is far too common in
# ordinary Chinese sentences to disqualify on its own.
_META_QUESTION = re.compile(
    r"(什么意思|啥意思|是什么意思|.{0,4}是什么.{0,3}词|难不难|累不累|烦不烦|讽刺吗|算不算"
    r"|(累|困|烦|丧|难过|开心|高兴|好笑|有意思)(吗|呢|吧))"
)


def _inclusion_cue(text: str):
    """Return (reaction, reason, confidence, vector) for a keyword embedded in a
    longer sentence, or None. Kept deliberately below fullmatch confidence so a
    clean exact cue always wins; this only rescues previously-neutral input that
    is a genuine first-person, present, unhedged emotional expression."""
    if (
        _NEGATION.search(text)
        or _THIRD_PERSON.search(text)
        or _QUOTED.search(text)
        or _HEDGE.search(text)
        or _META_QUESTION.search(text)
    ):
        return None
    for pattern, reaction, reason, confidence, vector in _INCLUSION_CUES:
        if pattern.search(text):
            return reaction, reason, confidence, vector
    return None


def _project(vector, reaction, traits):
    """Project the cue's interpretation into the companion's response.

    These small cue-specific engineering gains preserve legacy clear phrases.
    Distress elicits mild concern, rather than copying the speaker's mood.
    Effort/agency/uncertainty describe interpretation; they do not charge resources
    or assert causality. Actual reply consumption is recorded elsewhere.
    """
    warmth = 0.7 + 0.3 * traits["warmth"]
    playfulness = 0.7 + 0.3 * traits.get("playfulness", 0.5)
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
    elif reaction == "amusement":
        # Laughter lifts mood and activation; playfulness amplifies it.
        valence = 0.3 * vector.pleasantness * playfulness
        arousal = 0.25 * vector.attention * playfulness
        control = 0.15 * (vector.control - 0.5)
    elif reaction == "excitement":
        # Anticipation is high-energy positive; curiosity also raises it.
        valence = 0.25 * vector.pleasantness
        arousal = 0.3 * vector.attention * (0.5 + 0.5 * traits["curiosity"])
        control = 0.1 * (vector.control - 0.5)
    elif reaction == "surprise":
        # Surprise is brief arousal without a settled valence or control.
        valence = 0.05 * vector.pleasantness
        arousal = 0.3 * vector.attention
        control = -0.15 * vector.uncertainty
    elif reaction == "sympathy":
        # Empathy for another softens mood and lowers control, but stays kind.
        valence = 0.15 * vector.pleasantness * warmth
        arousal = 0.1 * vector.attention
        control = -0.1 * vector.uncertainty
    elif reaction == "annoyance":
        # Irritation is negative and high-arousal, with reduced pleasantness.
        valence = 0.25 * vector.pleasantness
        arousal = 0.2 * vector.attention
        control = -0.15 * (1 - vector.control)
    elif reaction == "pride":
        # Shared accomplishment raises valence and control, modest arousal.
        valence = 0.25 * vector.pleasantness * warmth
        arousal = 0.15 * vector.attention
        control = 0.2 * (vector.control - 0.4)
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
    appraisal_version = "rules_v2"
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
    elif _AMUSEMENT.fullmatch(text):
        reaction, reason, confidence = "amusement", "explicit_appreciation", 0.8
        uncertainty, attribution = 0.2, "other"
        vector = AppraisalVector(
            pleasantness=0.8, attention=0.7, uncertainty=uncertainty, agency=-1.0, control=0.4
        )
    elif _EXCITEMENT.fullmatch(text):
        reaction, reason, confidence = "excitement", "explicit_appreciation", 0.75
        uncertainty, attribution = 0.2, "situation"
        vector = AppraisalVector(
            pleasantness=0.7, attention=0.8, uncertainty=uncertainty, agency=1.0, control=0.5
        )
    elif _SURPRISE.fullmatch(text):
        reaction, reason, confidence = "surprise", "explicit_question", 0.6
        uncertainty, attribution = 0.4, "situation"
        vector = AppraisalVector(
            pleasantness=0.1, attention=0.9, uncertainty=0.4, agency=0.0, control=0.4
        )
    elif _SYMPATHY.fullmatch(text):
        reaction, reason, confidence = "sympathy", "explicit_distress", 0.7
        uncertainty, attribution = 0.25, "other"
        vector = AppraisalVector(
            pleasantness=0.2, attention=0.5, uncertainty=uncertainty, agency=-0.5, control=0.4
        )
    elif _ANNOYANCE.fullmatch(text):
        reaction, reason, confidence = "annoyance", "explicit_distress", 0.75
        uncertainty, attribution = 0.2, "situation"
        vector = AppraisalVector(
            pleasantness=-0.6, attention=0.6, uncertainty=uncertainty, agency=1.0, control=0.3
        )
    elif _PRIDE.fullmatch(text):
        reaction, reason, confidence = "pride", "explicit_appreciation", 0.8
        uncertainty, attribution = 0.2, "self"
        vector = AppraisalVector(
            pleasantness=0.7, attention=0.5, uncertainty=uncertainty, agency=1.0, control=0.7
        )
    else:
        # Inclusion fallback: rescue previously-neutral complex sentences so a
        # multi-clause message with an embedded emotional cue is not read as
        # emotionless. Lower confidence than the fullmatch path by design.
        cue = _inclusion_cue(text)
        if cue is not None:
            reaction, reason, confidence, vector = cue
            uncertainty = vector.uncertainty
            attribution = (
                "self" if vector.agency > 0 else ("other" if vector.agency < 0 else "situation")
            )
            appraisal_version = "rules_v3"
    return {
        "reaction": reaction,
        "reason": reason,
        "confidence": confidence,
        "delta": _project(vector, reaction, traits),
        "appraisal": vector.model_dump(),
        "appraisal_version": appraisal_version,
        "uncertainty": uncertainty,
        "attribution": attribution,
    }
