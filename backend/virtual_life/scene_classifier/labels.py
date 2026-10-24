"""Label definitions shared by training and serving."""

from dataclasses import dataclass

LABELS: tuple[str, ...] = ("daily", "emotional", "intimate", "explicit")
LABEL_INDEX: dict[str, int] = {label: index for index, label in enumerate(LABELS)}

LABEL_DESCRIPTIONS: dict[str, str] = {
    "daily": "日常闲聊、工作学习、天气美食、事务性沟通",
    "emotional": "情感倾诉、安慰、孤独、焦虑、开心等情绪表达",
    "intimate": "亲密、暧昧、调情、暗示，但尚未直接描述性内容",
    "explicit": "直接性意图、露骨内容，或成人向性健康/性行为问题咨询",
}


@dataclass(frozen=True, slots=True)
class SceneDecision:
    """One routing decision with the full score distribution."""

    label: str
    confidence: float
    scores: dict[str, float]
    reason: str = "threshold"
    threshold: float | None = None

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "confidence": self.confidence,
            "scores": dict(self.scores),
            "reason": self.reason,
            "threshold": self.threshold,
        }
