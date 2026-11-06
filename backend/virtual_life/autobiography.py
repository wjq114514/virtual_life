"""Autobiographical self-narrative for companions.

Compresses the rolling history of episodes, stable facts, emotion statistics
and personality revisions into dated chapters. Chapters are written on a
background path (never inside a dialogue transaction) through one model call,
or a deterministic template when no model is configured. Dialogue prompts only
receive the current chapter summary, bounded by a character cap. Chapters are
provenance-checked: when a source memory is corrected or tombstoned, the
chapter is marked dirty and rebuilt on the next background pass.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from .memory import MemoryError
from .ports import ModelRequest

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS autobiography_chapters (
        chapter_id TEXT PRIMARY KEY,
        companion_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL CHECK (chapter_index >= 1),
        summary TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('clean','dirty')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS autobiography_chapters_latest
        ON autobiography_chapters(companion_id, chapter_index DESC)""",
)


class AutobiographyService:
    def __init__(
        self,
        memory_service,
        *,
        provider=None,
        model_name: str = "",
        enabled: bool = True,
        chapter_episodes: int = 8,
        chapter_gap_days: int = 7,
        output_token_limit: int = 600,
    ) -> None:
        self.memory = memory_service
        self.provider = provider
        self.model_name = model_name
        self.enabled = enabled
        if not 1 <= chapter_episodes <= 64:
            raise ValueError("Autobiography chapter episode count must be 1..64")
        if not 1 <= chapter_gap_days <= 90:
            raise ValueError("Autobiography chapter gap must be 1..90 days")
        if not 64 <= output_token_limit <= 8192:
            raise ValueError("Autobiography output token limit must be 64..8192")
        self.chapter_episodes = chapter_episodes
        self.chapter_gap_days = chapter_gap_days
        self.output_token_limit = output_token_limit
        with self.memory._connection(write=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    # -- data gathering ---------------------------------------------------

    def _personality_revision(self, connection, companion_id) -> int:
        row = connection.execute(
            "SELECT revision FROM companions WHERE companion_id=?", (companion_id,)
        ).fetchone()
        return int(row["revision"]) if row is not None else 0

    def _sources(self, connection, companion_id):
        episodes = connection.execute(
            """SELECT memory_id, content, revision, occurred_at, updated_at FROM memory_records
            WHERE companion_id=? AND kind='episode' AND active=1
            ORDER BY COALESCE(occurred_at, updated_at) DESC LIMIT ?""",
            (companion_id, self.chapter_episodes),
        ).fetchall()
        facts = connection.execute(
            """SELECT memory_id, content, revision FROM memory_records
            WHERE companion_id=? AND kind='fact' AND active=1 AND (confirmed=1 OR importance>=0.5)
            ORDER BY importance DESC, updated_at DESC LIMIT 10""",
            (companion_id,),
        ).fetchall()
        return episodes, facts

    def _emotion_stats(self, connection, companion_id, now: datetime):
        since = self.memory._timestamp(now - timedelta(days=14))
        rows = connection.execute(
            """SELECT appraisal_json FROM emotion_experiences
            WHERE companion_id=? AND created_at>=?""",
            (companion_id, since),
        ).fetchall()
        valence = 0.0
        arousal = 0.0
        for row in rows:
            try:
                delta = json.loads(row["appraisal_json"]).get("delta") or {}
                valence += float(delta.get("valence") or 0.0)
                arousal += float(delta.get("arousal") or 0.0)
            except (ValueError, TypeError):
                continue
        count = len(rows)
        if count:
            valence /= count
            arousal /= count
        return count, round(valence, 3), round(arousal, 3)

    # -- fingerprint / provenance ------------------------------------------

    def _fingerprint(self, connection, memory_ids, personality_revision: int) -> str:
        entries = []
        for memory_id in memory_ids:
            row = connection.execute(
                "SELECT revision, active FROM memory_records WHERE memory_id=?", (memory_id,)
            ).fetchone()
            entries.append([memory_id, int(row["revision"]) if row else -1, bool(row["active"]) if row else False])
        return json.dumps([personality_revision, entries], ensure_ascii=False, sort_keys=True)

    def current_chapter(self, connection, companion_id: str) -> dict | None:
        row = connection.execute(
            """SELECT * FROM autobiography_chapters WHERE companion_id=? AND status='clean'
            ORDER BY chapter_index DESC LIMIT 1""",
            (companion_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            provenance = json.loads(row["provenance_json"])
            memory_ids = [item for item in provenance if isinstance(item, str)]
            personality_revision = int(provenance[-1]) if provenance and isinstance(provenance[-1], int) else 0
        except (ValueError, TypeError):
            memory_ids, personality_revision = [], 0
        if self._fingerprint(connection, memory_ids, personality_revision) != row["fingerprint"]:
            connection.execute(
                "UPDATE autobiography_chapters SET status='dirty', updated_at=? WHERE chapter_id=?",
                (self.memory._timestamp(self.memory._now()), row["chapter_id"]),
            )
            return None
        return dict(row)

    def note(self, connection, companion_id: str) -> str | None:
        chapter = self.current_chapter(connection, companion_id)
        if chapter is None:
            return None
        text = " ".join(chapter["summary"].split())[:200]
        return f"（自我叙事·当前章节）她最近对自己经历的叙述：{text}"

    # -- chapter generation -------------------------------------------------

    def _context(self, companion_id, episodes, facts, emotion_stats, personality_revision, now):
        lines = [f"companion_id: {companion_id}", f"personality_revision: {personality_revision}"]
        for row in episodes:
            when = row["occurred_at"] or row["updated_at"]
            lines.append(f"episode [{row['memory_id']} @ {when}]: {row['content'][:300]}")
        for row in facts:
            lines.append(f"fact [{row['memory_id']}]: {row['content'][:300]}")
        count, valence, arousal = emotion_stats
        lines.append(
            f"emotion(last 14d): experiences={count}, mean_delta(valence={valence}, arousal={arousal})"
        )
        lines.append(f"chapter_date: {self.memory._timestamp(now)}")
        return "\n".join(lines)

    def _template(self, companion_id, episodes, facts, emotion_stats, now):
        count, valence, arousal = emotion_stats
        parts = [
            f"到 {now.date().isoformat()} 为止，我经历了 {len(episodes)} 段和你的对话，"
            f"记住了 {len(facts)} 条稳定的事。"
        ]
        if facts:
            parts.append("最近我在意的是：" + "；".join(row["content"].strip()[:40] for row in facts[:3]) + "。")
        if count:
            parts.append(f"这两周我的情绪整体偏{'轻松' if valence >= 0 else '沉静'}、{'起伏较大' if abs(arousal) >= 0.05 else '平稳'}。")
        return "".join(parts)

    def _generate(self, companion_id, context, task_id) -> str | None:
        if self.provider is None or not self.model_name.strip():
            return None
        try:
            self.memory.reserve_model_tokens(
                companion_id,
                {
                    "task_id": task_id,
                    "purpose": "autobiography",
                    "input_tokens": 4096,
                    "output_tokens": self.output_token_limit,
                },
            )
        except MemoryError:
            return None
        answer = self.provider.complete(
            ModelRequest(
                task_id=task_id,
                companion_id=companion_id,
                purpose="autobiography",
                model=self.model_name,
                context_text=context,
                input_token_limit=4096,
                output_token_limit=self.output_token_limit,
            )
        )
        usage = (answer.usage.input_tokens, answer.usage.output_tokens)
        if any(type(count) is not int or not 0 <= count <= 1_000_000_000 for count in usage):
            return None
        self.memory.settle_model_tokens(companion_id, task_id, {"input_tokens": usage[0], "output_tokens": usage[1]})
        text = " ".join(answer.text.split())
        if not text or len(text) > 4000:
            return None
        return text

    def _due(self, connection, companion_id, episode_count: int, now: datetime) -> bool:
        row = connection.execute(
            "SELECT * FROM autobiography_chapters WHERE companion_id=? ORDER BY chapter_index DESC LIMIT 1",
            (companion_id,),
        ).fetchone()
        if row is None:
            return True
        if row["status"] == "dirty":
            return True
        try:
            provenance = json.loads(row["provenance_json"])
            covered = len([item for item in provenance if isinstance(item, str)])
        except (ValueError, TypeError):
            covered = 0
        if episode_count - covered >= self.chapter_episodes:
            return True
        created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        return (now - created) >= timedelta(days=self.chapter_gap_days) and episode_count > covered

    def process(self, companion_id: str | None = None, *, force: bool = False):
        """Background entry point: builds one due chapter per companion.

        Reads in a read transaction, calls the model with no transaction held
        (settle_model_tokens opens its own write connection), then commits the
        chapter in a short write transaction so a nested write never happens.
        """
        if not self.enabled:
            return None
        now = self.memory._now()
        with self.memory._connection() as connection:
            if companion_id is None:
                rows = connection.execute(
                    """SELECT DISTINCT companion_id FROM memory_records
                    WHERE kind='episode' AND active=1 ORDER BY companion_id"""
                ).fetchall()
                targets = [row["companion_id"] for row in rows]
            else:
                targets = [companion_id]
            target = None
            for candidate in targets:
                episodes, facts = self._sources(connection, candidate)
                if not episodes:
                    continue
                if not force and not self._due(connection, candidate, len(episodes), now):
                    continue
                target = candidate
                emotion_stats = self._emotion_stats(connection, target, now)
                personality_revision = self._personality_revision(connection, target)
                index = 1 + (
                    connection.execute(
                        "SELECT MAX(chapter_index) AS max_index FROM autobiography_chapters WHERE companion_id=?",
                        (target,),
                    ).fetchone()["max_index"]
                    or 0
                )
                chapter_id = "abio_" + uuid.uuid4().hex
                provenance = [row["memory_id"] for row in episodes] + [row["memory_id"] for row in facts]
                provenance.append(personality_revision)
                fingerprint = self._fingerprint(
                    connection,
                    [row["memory_id"] for row in episodes] + [row["memory_id"] for row in facts],
                    personality_revision,
                )
                context = self._context(target, episodes, facts, emotion_stats, personality_revision, now)
                break
        if target is None:
            return None
        summary = self._generate(target, context, chapter_id)
        if summary is None:
            summary = self._template(target, episodes, facts, emotion_stats, now)
        timestamp = self.memory._timestamp(now)
        with self.memory._connection(write=True) as connection:
            connection.execute(
                """INSERT INTO autobiography_chapters
                (chapter_id, companion_id, chapter_index, summary, provenance_json,
                fingerprint, status, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    chapter_id,
                    target,
                    index,
                    summary,
                    json.dumps(provenance, ensure_ascii=False),
                    fingerprint,
                    "clean",
                    timestamp,
                    timestamp,
                ),
            )
        return {"chapter_id": chapter_id, "companion_id": target, "chapter_index": index}

    def chapters(self, companion_id: str, limit: int = 20):
        with self.memory._connection() as connection:
            rows = connection.execute(
                """SELECT chapter_id, companion_id, chapter_index, summary, status,
                created_at, updated_at FROM autobiography_chapters
                WHERE companion_id=? ORDER BY chapter_index DESC LIMIT ?""",
                (companion_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
