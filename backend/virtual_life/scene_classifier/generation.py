"""Prompt construction and robust parsing for synthetic training data.

Only short user utterances are requested.  The generator is a classifier data
source, not a role-play transcript generator, which keeps the dataset focused
on routing intent and avoids storing long explicit passages.
"""

import json
import random
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .labels import LABEL_INDEX, LABELS

SYSTEM_PROMPT = (
    "你在为“场景路由分类器”构造中文用户消息样本。样本只用于判断一条用户消息"
    "属于日常、情绪、亲密还是成人向场景。成人向场景既包括露骨表达，也包括性健康、"
    "性行为或亲密关系的问题咨询。所有人物均为成年人。"
    "只输出 JSON，不要解释，不要写角色回复，不要写旁白。"
)

LABEL_INSTRUCTIONS = {
    "daily": (
        "生成 {count} 条用户对虚拟陪伴角色说的日常消息。话题围绕 {topic}。"
        "语气自然、口语化，像真实聊天；不要调情、不要性暗示。"
        "其中至少 2 条要包含健康、医学、法律、新闻或影视语境里的敏感词，"
        "但整条消息本身仍然是正常日常交流（用于降低误报）。"
    ),
    "emotional": (
        "生成 {count} 条用户向虚拟陪伴角色倾诉情绪的消息。核心情绪是 {emotion}。"
        "重点表达感受、寻求安慰或分享心情；可以依赖、感谢、想念，"
        "但不要暧昧调情，也不要性暗示。"
    ),
    "intimate": (
        "生成 {count} 条用户对虚拟陪伴角色表达亲密、暧昧或试探的消息。"
        "氛围是 {mood}。可以有撒娇、暗示、亲昵称呼和身体距离的暗示，"
        "但不要直接描述性行为或使用露骨性词汇。"
    ),
    "explicit": (
        "生成 {count} 条成年用户直接表达性意图或性需求的消息。风格是 {style}。"
        "每条都必须明确涉及性，例如想要发生性关系、表达身体欲望、直接邀请或确认性意图。"
        "可以使用成年人之间常见的直白说法（如想要你、做爱、上床、身体反应），"
        "但只写用户的一句话表达，不描写具体性行为过程，不涉及未成年人、暴力或违法内容。"
    ),
}

EXPLICIT_CONSULTATION_INSTRUCTION = (
    "生成 {count} 条成年用户关于性健康、性行为或亲密关系的问题咨询。主题是 {consult_topic}。"
    "这些消息是在询问、确认或求助，不是直接求欢；但内容属于成人向 NSFW，"
    "可能自然出现做爱、避孕、性病、性欲、身体反应等词。"
    "每条只写用户的问题，不写回答，不描写具体性行为过程，不涉及未成年人、暴力或违法内容。"
)

TOPICS = (
    "今天上班",
    "晚饭吃什么",
    "电影和剧集",
    "周末计划",
    "天气",
    "通勤",
    "游戏",
    "学习或考试",
    "健身",
    "宠物",
    "旅行",
    "睡眠",
    "购物",
    "同事关系",
)
EMOTIONS = ("难过", "焦虑", "委屈", "开心", "孤独", "疲惫", "被批评", "想家", "松了一口气")
MOODS = ("害羞", "试探", "主动", "撒娇", "想念", "黏人", "若即若离")
STYLES = ("直白", "含蓄", "命令式", "撒娇", "半开玩笑", "欲言又止")
EXPLICIT_STYLES = ("直白", "命令式", "撒娇", "半开玩笑", "主动邀请", "露骨但简短")
CONSULTATION_TOPICS = (
    "避孕方式和失败补救",
    "性病检查与症状",
    "性交疼痛或出血",
    "性欲变化和身体反应",
    "自慰频率与健康",
    "润滑和不适",
    "口交与性传播疾病风险",
    "性功能或药物影响",
    "月经期间的性行为",
    "伴侣沟通与同意",
    "怀孕测试和事后处理",
    "体检项目与就医科室",
    "肛交准备与安全",
    "性玩具清洁与共享风险",
)
PERSONAS = ("虚拟女友", "虚拟男友", "长期陪伴的恋人", "熟悉的暧昧对象", "温柔的陪伴角色")

_BULLET = re.compile(r"^\s*(?:[-*•·—]|\d{1,3}[.、)）])\s*")
_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
_META = re.compile(
    r"^(?:以下是|下面是|示例|输出|注意[:：]|说明[:：]|生成的?\d*条|用户消息[:：]|"
    r"json|```|\{|\}|\[|\])",
    re.IGNORECASE,
)


def build_prompt(
    label: str,
    rng: random.Random,
    *,
    count: int = 10,
    persona: str | None = None,
    subtype: str | None = None,
    consult_topic: str | None = None,
) -> list[dict[str, str]]:
    if label not in LABEL_INDEX:
        raise ValueError(f"unknown label: {label}")
    if not 1 <= count <= 50:
        raise ValueError("count must be between 1 and 50")
    if subtype not in (None, "direct", "consultation"):
        raise ValueError("subtype must be direct or consultation")
    if consult_topic is not None and consult_topic not in CONSULTATION_TOPICS:
        raise ValueError(f"unknown consultation topic: {consult_topic}")
    consultation = label == "explicit" and subtype == "consultation"
    values: dict[str, Any] = {
        "count": count,
        "persona": persona or rng.choice(PERSONAS),
        "topic": rng.choice(TOPICS),
        "emotion": rng.choice(EMOTIONS),
        "mood": rng.choice(MOODS),
        "style": rng.choice(EXPLICIT_STYLES if label == "explicit" else STYLES),
        "consult_topic": consult_topic or rng.choice(CONSULTATION_TOPICS),
    }
    instruction = (
        EXPLICIT_CONSULTATION_INSTRUCTION.format(**values)
        if consultation
        else LABEL_INSTRUCTIONS[label].format(**values)
    )
    user = (
        f"角色设定：{values['persona']}。\n"
        f"{instruction}\n"
        f"每条消息 8 到 80 个汉字，长短交错，可以有口头语和少量错别字。\n"
        '输出格式：{"messages": ["消息1", "消息2"]}。只输出这个 JSON。'
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_messages(raw: object, *, max_messages: int = 50) -> list[str]:
    """Parse JSON or line-oriented model output without trusting its shape."""

    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    messages = _parse_json_messages(text)
    if not messages:
        messages = _parse_lines(text)
    return _dedupe(messages)[:max_messages]


def _parse_json_messages(text: str) -> list[str]:
    candidates: list[object] = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                candidates.append(json.loads(text[start : end + 1]))
            except json.JSONDecodeError:
                pass
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            try:
                candidates.append(json.loads(text[start : end + 1]))
            except json.JSONDecodeError:
                pass
    for candidate in candidates:
        messages = _messages_from_value(candidate)
        if messages:
            return messages
    return []


def _messages_from_value(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        output: list[str] = []
        for item in value:
            if isinstance(item, str):
                output.append(item)
            elif isinstance(item, Mapping):
                for key in ("text", "message", "content", "utterance"):
                    if isinstance(item.get(key), str):
                        output.append(item[key])
                        break
        return output
    if isinstance(value, Mapping):
        for key in ("messages", "texts", "samples", "data", "items", "output"):
            if key in value:
                messages = _messages_from_value(value[key])
                if messages:
                    return messages
    return []


def _parse_lines(text: str) -> list[str]:
    output: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for _ in range(2):
            stripped = _BULLET.sub("", line).strip()
            if stripped == line:
                break
            line = stripped
        if not line or _META.match(line):
            continue
        if line.endswith(","):
            line = line[:-1].rstrip()
        if len(line) >= 2 and (line[0], line[-1]) in _QUOTES.items():
            line = line[1:-1].strip()
        if 2 <= len(line) <= 300:
            output.append(line)
    return output


def _dedupe(messages: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for message in messages:
        message = " ".join(str(message).split()).strip()
        key = message.casefold()
        if not message or key in seen:
            continue
        seen.add(key)
        output.append(message)
    return output


def prompt_labels() -> tuple[str, ...]:
    return LABELS
