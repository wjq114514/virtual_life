"""Read current proactive output through a live browser access, never a replay grant."""

import json

from pydantic import BaseModel, ConfigDict, Field

from .coordinator import DomainError
from .memory import MemoryError
from .models import Identifier
from .proactivity import _instant


class ProactivityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sequence: int = Field(ge=0)
    event_id: Identifier
    command_id: Identifier
    assistant_message: str
    created_at: str


class ProactivityOutputPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    companion_id: Identifier
    node_id: Identifier
    access_id: Identifier
    epoch: int = Field(ge=0)
    companion_revision: int = Field(ge=0)
    items: list[ProactivityOutput]
    next_sequence: int = Field(ge=0)


class ProactivityOutputService:
    def __init__(self, proactivity, presentations=None):
        self.proactivity = proactivity
        self.dialogue = proactivity.dialogue
        self.coordinator = proactivity.coordinator
        self.store = proactivity.store
        self.presentations = (
            presentations
            if presentations is not None
            else getattr(proactivity.coordinator, "presentations", None)
        )
        with self.store.transaction(write=True) as connection:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS attention_output_events ON events(node_id,type,sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS companion_output_events ON events(companion_id,type,sequence)"
            )

    @staticmethod
    def _interrupted(connection, candidate, event_sequence):
        if candidate["origin"] in ("schedule", "task"):
            # Scheduled origins have no attention sample to compare against.
            # Eligibility is rechecked by _current before this method is called.
            return False
        if candidate["origin"] == "inner":
            activity = connection.execute(
                "SELECT attention_session_id, attention_boundary_id FROM inner_activities "
                "WHERE activity_id=?",
                (candidate["source_activity_id"],),
            ).fetchone()
            if activity is None:
                return True
            session_id = activity["attention_session_id"]
            boundary_id = activity["attention_boundary_id"]
        else:
            original = connection.execute(
                "SELECT payload_json FROM events WHERE event_id=?", (candidate["source_event_id"],)
            ).fetchone()
            if original is None:
                return True
            sample = json.loads(original["payload_json"])["data"]["attention"]
            session_id = sample["session_id"]
            boundary_id = sample["boundary_id"]
        # A remark that became inappropriate must not reappear after attention
        # later recovers. Other ambient/device observations are not attention.
        return (
            connection.execute(
                """SELECT 1 FROM events WHERE node_id=? AND type='node.observation' AND sequence>?
            AND json_type(payload_json, '$.data.attention')='object' AND (
                json_extract(payload_json, '$.data.attention.session_id') IS NOT ? OR
                json_extract(payload_json, '$.data.attention.focus') IS NOT 'unfocused' OR
                json_extract(payload_json, '$.data.attention.availability') IS NOT 'available' OR
                json_extract(payload_json, '$.data.attention.user_present') IS NOT 1 OR
                json_extract(payload_json, '$.data.attention.locked') IS NOT 0 OR
                json_extract(payload_json, '$.data.attention.meeting') IS NOT 0 OR
                json_extract(payload_json, '$.data.attention.screen_sharing') IS NOT 0 OR
                json_extract(payload_json, '$.data.attention.activity')='meeting' OR
                (json_extract(payload_json, '$.data.attention.boundary')!='none' AND
                 json_extract(payload_json, '$.data.attention.boundary_id') IS NOT ?))
            LIMIT 1""",
                (candidate["node_id"], event_sequence, session_id, boundary_id),
            ).fetchone()
            is not None
        )

    def _current(self, connection, candidate, companion, result, now):
        # The dialogue receipt/event commits before the worker updates its
        # denormalized candidate status. Do not lose output in that short window.
        if candidate["status"] not in {"completed", "running"}:
            return False
        if self.proactivity._reason(connection, candidate, companion, now) != "eligible":
            return False
        preview = result["preview"]
        if (
            result["status"] != "completed"
            or not result["assistant_message"]
            or preview["epoch"] != companion["epoch"]
            or preview["companion_revision"] != companion["revision"]
            or preview["active_node_id"] != companion["active_node_id"]
        ):
            return False
        p = self.dialogue.personality_service
        identity = companion["companion_id"]
        profile = p._profile_snapshot(identity, p._profile_row(connection, identity))
        state = p._state_snapshot(identity, p._state_row(connection, identity), now)
        if (
            preview["personality_revision"] != profile["revision"]
            or preview["state_revision"] != state["revision"]
        ):
            return False
        # A recorded short-term state can expire without changing its revision.
        if (
            state["expired"]
            and state["expires_at"]
            and _instant(result["created_at"]) < _instant(state["expires_at"])
        ):
            return False
        if self.dialogue.emotion_service is not None:
            emotion = self.dialogue.emotion_service.snapshot(
                connection, identity, profile, state, now
            )
            original = preview.get("emotion")
            if original is None or original["context_version"] != emotion["context_version"]:
                return False
        try:
            self.dialogue._validate_references(connection, identity, preview, now)
        except MemoryError:
            return False
        return True

    def poll(self, companion_id, node_id, access_id, epoch, after_sequence=None):
        return self._poll(companion_id, node_id, epoch, after_sequence, access_id=access_id)

    def poll_native(self, companion_id, node_id, presentation_id, epoch, after_sequence=None):
        return self._poll(
            companion_id,
            node_id,
            epoch,
            after_sequence,
            presentation_id=presentation_id,
        )

    def _poll(
        self, companion_id, node_id, epoch, after_sequence, *, access_id=None, presentation_id=None
    ):
        c = self.coordinator
        with self.store.transaction() as connection:
            now = c._now()
            companion = c._companion(c._row(connection, "companions", "companion_id", companion_id))
            c._check_owner(companion, node_id, epoch, now)
            baseline = 0
            if presentation_id is not None:
                if self.presentations is None:
                    raise DomainError(
                        409, "presentation_required", "Native presentations are not available"
                    )
                row = self.presentations.validate(connection, node_id, presentation_id, now)
                baseline = row["opened_sequence"]
            else:
                c.devices.validate_access(connection, node_id, access_id, now)
            node = c._node(c._row(connection, "nodes", "node_id", node_id), now, connection)
            if (
                not node["online"]
                or "text.display" not in node["capabilities"]
                or c._pending(connection, companion_id) is not None
            ):
                raise DomainError(409, "output_not_permitted", "Renderer cannot display output now")
            head = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]
            page = {
                "companion_id": companion_id,
                "node_id": node_id,
                "epoch": epoch,
                "companion_revision": companion["revision"],
                "items": [],
                "next_sequence": head,
            }
            if presentation_id is not None:
                page["presentation_id"] = presentation_id
            else:
                page["access_id"] = access_id
            # A fresh subscription establishes a cursor, without showing old remarks.
            if after_sequence is None:
                return page
            if after_sequence > head:
                raise DomainError(409, "output_cursor_invalid", "Output cursor is ahead of runtime")
            if presentation_id is not None:
                # Never replay output produced before this session was opened.
                start = max(after_sequence, baseline)
            else:
                access_start = connection.execute(
                    """SELECT COALESCE(MAX(sequence), 0) FROM events
                    WHERE type='node.web_access_opened' AND node_id=?
                    AND json_extract(payload_json, '$.access.access_id')=?""",
                    (node_id, access_id),
                ).fetchone()[0]
                start = max(after_sequence, access_start)
            rows = connection.execute(
                """SELECT * FROM events WHERE sequence>? AND sequence<=?
                AND companion_id=? AND recipient_node_id=? AND type='companion.proactive_completed'
                ORDER BY sequence LIMIT 20""",
                (start, head, companion_id, node_id),
            ).fetchall()
            for event in rows:
                payload = json.loads(event["payload_json"])
                candidate = connection.execute(
                    """SELECT * FROM proactivity_candidates
                    WHERE candidate_id=? AND companion_id=? AND command_id=?""",
                    (payload["candidate_id"], companion_id, payload["command_id"]),
                ).fetchone()
                receipt = connection.execute(
                    "SELECT * FROM dialogue_commands WHERE command_id=? AND companion_id=?",
                    (payload["command_id"], companion_id),
                ).fetchone()
                if candidate is None or receipt is None or receipt["result_json"] is None:
                    continue
                result = json.loads(receipt["result_json"])
                if result["source_event_id"] != event["event_id"]:
                    continue
                if not self._current(connection, candidate, companion, result, now):
                    continue
                if self._interrupted(connection, candidate, event["sequence"]):
                    continue
                page["items"].append(
                    {
                        "sequence": event["sequence"],
                        "event_id": event["event_id"],
                        "command_id": payload["command_id"],
                        "assistant_message": result["assistant_message"],
                        "created_at": event["occurred_at"],
                    }
                )
            if len(rows) == 20:
                page["next_sequence"] = rows[-1]["sequence"]
            return page
