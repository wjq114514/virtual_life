"""Self-monitoring of the companion's own reply style.

A closed feedback loop in the maiBot spirit: instead of only prompting the
model to "be natural", the runtime periodically measures the companion's
recent replies with deterministic heuristics (length, bubble shape, opener
repetition, question rate, light kaomoji seasoning) and stores a verdict.
Dialogue prompts then receive the latest verdict as a short aside so the
companion self-corrects in later turns — "你最近的回复平均 90 字，偏长" is a
concrete instruction the model can act on, unlike a generic "be more casual".

All metrics are computed from completed dialogue events (evidence, never
instructions); no model call is required, so the loop also runs in
model-less deployments and in tests.
"""

from __future__ import annotations

import json
import uuid

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS style_evaluations (
        evaluation_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL,
        last_event_sequence INTEGER NOT NULL,
        sample_count INTEGER NOT NULL,
        metrics_json TEXT NOT NULL,
        verdict TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS style_evaluations_latest
        ON style_evaluations(companion_id, created_at DESC)""",
)

# Metrics thresholds — tuned for zh-CN short-message conversation.
_LONG_REPLY_CHARS = 80
_OPENER_CHARS = 2
_KAOMOJI_HINTS = ("（", "(", "～", "~", "w")


class StyleEvalService:
    def __init__(
        self,
        memory_service,
        *,
        enabled: bool = True,
        sample_size: int = 20,
        min_sample: int = 4,
    ) -> None:
        self.memory = memory_service
        self.enabled = enabled
        if not 4 <= sample_size <= 200:
            raise ValueError("Style evaluation sample size must be 4..200")
        if not 2 <= min_sample <= sample_size:
            raise ValueError("Style evaluation minimum sample must fit the window")
        self.sample_size = sample_size
        self.min_sample = min_sample
        with self.memory._connection(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def process(self, companion_id: str | None = None) -> dict | None:
        """Background entry point: evaluate one companion's recent replies.

        Writes at most one evaluation row per call, and only when new
        completed replies exist beyond the last recorded event sequence.
        Pure heuristics — deterministic, no model usage.
        """
        if not self.enabled:
            return None
        now = self.memory._now()
        with self.memory._connection() as connection:
            if companion_id is None:
                rows = connection.execute(
                    """SELECT DISTINCT companion_id FROM events
                    WHERE type='companion.dialogue_completed' ORDER BY companion_id"""
                ).fetchall()
                targets = [row["companion_id"] for row in rows]
            else:
                targets = [companion_id]
            for target in targets:
                last = connection.execute(
                    """SELECT last_event_sequence FROM style_evaluations
                    WHERE companion_id=? ORDER BY created_at DESC LIMIT 1""",
                    (target,),
                ).fetchone()
                last_sequence = int(last["last_event_sequence"]) if last else 0
                rows = connection.execute(
                    """SELECT sequence, payload_json FROM events
                    WHERE companion_id=? AND type='companion.dialogue_completed'
                    AND sequence > ? ORDER BY sequence DESC LIMIT ?""",
                    (target, last_sequence, self.sample_size),
                ).fetchall()
                replies = []
                top_sequence = last_sequence
                for row in rows:
                    top_sequence = max(top_sequence, int(row["sequence"]))
                    try:
                        payload = json.loads(row["payload_json"])
                    except (ValueError, TypeError):
                        continue
                    text = payload.get("assistant_message")
                    if isinstance(text, str) and text.strip():
                        replies.append(text.strip())
                if len(replies) < self.min_sample:
                    continue
                metrics = self._metrics(replies)
                verdict = self._verdict(metrics)
                evaluation = {
                    "evaluation_id": f"se_{uuid.uuid4().hex}",
                    "companion_id": target,
                    "last_event_sequence": top_sequence,
                    "sample_count": len(replies),
                    "metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    "verdict": verdict,
                    "created_at": self.memory._timestamp(now),
                }
                with self.memory._connection(write=True) as write_connection:
                    write_connection.execute(
                        """INSERT INTO style_evaluations
                        (evaluation_id, companion_id, last_event_sequence,
                         sample_count, metrics_json, verdict, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            evaluation["evaluation_id"],
                            evaluation["companion_id"],
                            evaluation["last_event_sequence"],
                            evaluation["sample_count"],
                            evaluation["metrics_json"],
                            evaluation["verdict"],
                            evaluation["created_at"],
                        ),
                    )
                return evaluation
            return None

    def note(self, connection, companion_id: str) -> str | None:
        row = connection.execute(
            """SELECT verdict FROM style_evaluations WHERE companion_id=?
            ORDER BY created_at DESC LIMIT 1""",
            (companion_id,),
        ).fetchone()
        if row is None:
            return None
        return f"（风格反馈·对自己最近回复的体检）{row['verdict'][:200]}"

    # -- metrics ------------------------------------------------------------

    def _metrics(self, replies: list[str]) -> dict:
        count = len(replies)
        lengths = [len(reply) for reply in replies]
        avg_chars = round(sum(lengths) / count, 1) if count else 0.0
        long_ratio = round(sum(1 for length in lengths if length > _LONG_REPLY_CHARS) / count, 3)
        question_ratio = round(
            sum(1 for reply in replies if reply.rstrip().endswith(("？", "?"))) / count, 3
        )
        openers = [reply[:_OPENER_CHARS] for reply in replies]
        opener_diversity = round(len(set(openers)) / count, 3)
        kaomoji_ratio = round(
            sum(1 for reply in replies if any(hint in reply for hint in _KAOMOJI_HINTS)) / count,
            3,
        )
        duplicate_openers = sorted(
            {opener: openers.count(opener) for opener in set(openers)}.items(),
            key=lambda item: (-item[1], item[0]),
        )
        return {
            "sample_count": count,
            "avg_chars": avg_chars,
            "long_reply_ratio": long_ratio,
            "question_ratio": question_ratio,
            "opener_diversity": opener_diversity,
            "kaomoji_ratio": kaomoji_ratio,
            "top_openers": [
                {"opener": opener, "count": hits}
                for opener, hits in duplicate_openers[:3]
                if hits > 1
            ],
        }

    def _verdict(self, metrics: dict) -> str:
        parts: list[str] = []
        avg = metrics["avg_chars"]
        if avg > _LONG_REPLY_CHARS:
            parts.append(f"最近平均 {avg:.0f} 字，偏长——下一条先给结论，细节等对方追问")
        elif avg < 6:
            parts.append(f"最近平均 {avg:.0f} 字，太短到敷衍——补半句真实想法")
        else:
            parts.append(f"最近平均 {avg:.0f} 字，长度自然")
        top = metrics["top_openers"]
        if top and top[0]["count"] >= max(3, metrics["sample_count"] // 3):
            opener = top[0]["opener"]
            parts.append(f"开头「{opener}」用了 {top[0]['count']} 次——换着开口")
        if metrics["opener_diversity"] >= 0.8:
            parts.append("开口方式多样")
        if metrics["question_ratio"] > 0.6:
            parts.append("连续追问太多，留点自己的判断")
        elif metrics["question_ratio"] < 0.1:
            parts.append("几乎没有反问，偶尔好奇一句更自然")
        if metrics["kaomoji_ratio"] > 0.5:
            parts.append("表情符号偏密，收一收")
        return "；".join(parts)
