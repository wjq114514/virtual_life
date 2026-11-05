"""Local-time day-part rhythm helpers.

Shared by inner life state scoring and the personality prompt so both sides
describe the same wall clock: a quiet late-night social need and a prompt that
mentions "现在是深夜（02:14）" must agree. Pure functions, no I/O, no model
calls; a bad or missing timezone degrades to the server's local time instead
of failing the turn.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# 23:00-05:00. Late-night hours when a companion should lean quieter.
_DEEP_NIGHT_START = 23
_DEEP_NIGHT_END = 5
# 00:00-05:00. The small hours specifically, when rest should dominate.
_PREDAWN_END = 5


def local_parts(now: datetime, timezone_name: str) -> tuple[int, int]:
    """Return (hour, minute) of ``now`` in ``timezone_name``.

    Falls back to the host's local timezone when the name is unusable so a
    typo in config never breaks dialogue.
    """
    try:
        local = now.astimezone(ZoneInfo(timezone_name))
    except Exception:
        local = now.astimezone()
    return local.hour, local.minute


def day_part(hour: int) -> str:
    """Map a local hour to a coarse Chinese day part."""
    if _DEEP_NIGHT_START <= hour or hour < _DEEP_NIGHT_END:
        return "深夜"
    if hour < 8:
        return "清晨"
    if hour < 12:
        return "上午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    return "晚上"


def rhythm_snapshot(now: datetime | None, timezone_name: str) -> dict | None:
    """Return the local-time snapshot for ``now``, or ``None`` if unknown.

    The label is a plain time fact ("现在是深夜（02:14）") — no scripted
    lines; wording about the hour stays the model's own choice.
    """
    if now is None:
        return None
    hour, minute = local_parts(now, timezone_name)
    deep_night = _DEEP_NIGHT_START <= hour or hour < _DEEP_NIGHT_END
    return {
        "hour": hour,
        "minute": minute,
        "day_part": day_part(hour),
        "is_deep_night": deep_night,
        "is_predawn": hour < _PREDAWN_END,
        "label": f"现在是{day_part(hour)}（{hour:02d}:{minute:02d}）",
    }
