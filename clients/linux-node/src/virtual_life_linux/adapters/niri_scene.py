"""Niri window-scene collector for prompt enrichment.

Unlike the attention adapter, which never reads window titles, this collector
can describe the visible windows so the server can build a cheaper, faster
vision prompt. Titles are user content, so the collector is opt-in and titles
are sent only when ``scene_titles_enabled`` is set; otherwise only app ids are
reported. Every title is sanitized before it can leave the host.

The output is shaped as the two frame fields the server already accepts:
``client_features.ui_elements`` for the visible windows and ``host_metadata`` for
the focused application.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import unicodedata
from dataclasses import dataclass, field

DEFAULT_MAX_WINDOWS = 12
DEFAULT_TITLE_CHARS = 160
WINDOW_ROLES = ("focused_window", "window")
_APPROXIMATE_CONFIDENCE = 0.3
_EXACT_CONFIDENCE = 1.0


def sanitize_title(text: str, *, limit: int = DEFAULT_TITLE_CHARS) -> str:
    """Drop invisible/control characters, collapse whitespace and bound length."""

    normalized = unicodedata.normalize("NFKC", text)
    cleaned: list[str] = []
    for character in normalized:
        if character in ("\t", "\n", "\r"):
            cleaned.append(" ")
            continue
        category = unicodedata.category(character)
        if category in ("Cf", "Cc", "Cs", "Co", "Cn") or "\ue0000" <= character <= "\ue007f":
            continue
        cleaned.append(character)
    collapsed = " ".join("".join(cleaned).split())
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip()
    return collapsed


@dataclass(frozen=True, slots=True)
class NiriScene:
    windows: list[dict] = field(default_factory=list)
    client_features: dict = field(default_factory=dict)
    host_metadata: dict = field(default_factory=dict)


def _output_sizes(outputs) -> dict[str, tuple[float, float]]:
    sizes: dict[str, tuple[float, float]] = {}
    items = list(outputs.values()) if isinstance(outputs, dict) else outputs
    if not isinstance(items, (list, tuple)):
        return sizes
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        logical = item.get("logical") or {}
        width = logical.get("width")
        height = logical.get("height")
        if (
            isinstance(name, str)
            and isinstance(width, (int, float))
            and isinstance(height, (int, float))
        ):
            sizes[name] = (float(width), float(height))
    return sizes


def _active_workspaces(workspaces) -> set:
    if not isinstance(workspaces, (list, tuple)):
        return set()
    return {
        item.get("id")
        for item in workspaces
        if isinstance(item, dict) and (item.get("is_active") or item.get("is_focused"))
    }


def _visible_windows(windows, active: set) -> list[dict]:
    if not isinstance(windows, (list, tuple)):
        return []
    visible = [
        item for item in windows if isinstance(item, dict) and item.get("workspace_id") in active
    ]

    def sort_key(item: dict):
        layout = item.get("layout") or {}
        position = layout.get("pos_in_scrolling_layout")
        col = position[0] if isinstance(position, (list, tuple)) and position else 0
        row = position[1] if isinstance(position, (list, tuple)) and len(position) > 1 else 0
        return (str(item.get("output") or ""), col, row, str(item.get("id")))

    return sorted(visible, key=sort_key)


def _bbox(window: dict, output_size: tuple[float, float], column: int, column_count: int):
    layout = window.get("layout") or {}
    width, height = output_size
    position = layout.get("tile_pos_in_workspace_view")
    size = layout.get("window_size")
    if (
        isinstance(position, (list, tuple))
        and len(position) == 2
        and isinstance(size, (list, tuple))
        and len(size) == 2
        and width
        and height
    ):
        x, y = float(position[0]), float(position[1])
        w, h = float(size[0]), float(size[1])
        box = [x / width, y / height, (x + w) / width, (y + h) / height]
        return _clamp(box), _EXACT_CONFIDENCE
    columns = max(1, column_count)
    box = [column / columns, 0.0, (column + 1) / columns, 1.0]
    return _clamp(box), _APPROXIMATE_CONFIDENCE


def _clamp(box: list[float]) -> list[float]:
    return [min(1.0, max(0.0, value)) for value in box]


def _column_of(window: dict):
    position = (window.get("layout") or {}).get("pos_in_scrolling_layout")
    if isinstance(position, (list, tuple)) and position:
        return position[0]
    return 0


def _window_text(window: dict, include_titles: bool) -> str:
    if include_titles:
        return sanitize_title(str(window.get("title") or ""))
    return sanitize_title(str(window.get("app_id") or ""), limit=64)


def build_scene(
    windows,
    workspaces,
    outputs,
    *,
    include_titles: bool = False,
    max_windows: int = DEFAULT_MAX_WINDOWS,
) -> NiriScene:
    active = _active_workspaces(workspaces)
    visible = _visible_windows(windows, active)[: max(0, max_windows)]
    sizes = _output_sizes(outputs)
    columns = sorted({_column_of(item) for item in visible})
    column_count = len(columns) or 1

    elements: list[dict] = []
    focused: dict | None = None
    for item in visible:
        col_index = columns.index(_column_of(item))
        output_size = sizes.get(str(item.get("output") or ""), (0.0, 0.0))
        box, confidence = _bbox(item, output_size, col_index, column_count)
        is_focused = bool(item.get("is_focused"))
        if is_focused:
            focused = item
        elements.append(
            {
                "element_id": f"window:{item.get('id')}",
                "role": "focused_window" if is_focused else "window",
                "text": _window_text(item, include_titles),
                "bbox": box,
                "confidence": confidence,
            }
        )

    client_features = {
        "schema_version": "1.0",
        "extractor": "niri",
        "extractor_version": "1",
        "ui_elements": elements,
    }
    host_metadata: dict = {}
    if focused is not None:
        app_id = sanitize_title(str(focused.get("app_id") or ""), limit=128)
        if app_id:
            host_metadata["app_id"] = app_id
        if focused.get("id") is not None:
            host_metadata["window_id"] = str(focused["id"])[:128]
    return NiriScene(windows=visible, client_features=client_features, host_metadata=host_metadata)


def frame_fields(scene: NiriScene) -> dict:
    """Shape a scene into the ``client_features``/``host_metadata`` frame fields."""

    fields = {"client_features": scene.client_features}
    if scene.host_metadata:
        fields["host_metadata"] = scene.host_metadata
    return fields


class NiriSceneCollector:
    name = "niri-scene"

    def __init__(
        self,
        binary: str = "niri",
        *,
        include_titles: bool = False,
        max_windows: int = DEFAULT_MAX_WINDOWS,
    ) -> None:
        self.binary = binary
        self.include_titles = include_titles
        self.max_windows = max_windows

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def _json(self, *args: str) -> object:
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "msg",
            "--json",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"niri msg {' '.join(args)} failed")
        return json.loads(stdout.decode("utf-8"))

    async def collect(self) -> NiriScene:
        windows = await self._json("windows")
        workspaces = await self._json("workspaces")
        outputs = await self._json("outputs")
        return build_scene(
            windows,
            workspaces,
            outputs,
            include_titles=self.include_titles,
            max_windows=self.max_windows,
        )


def from_desktop(desktop) -> NiriSceneCollector:
    """Build a collector honoring the opt-in desktop scene flags."""

    return NiriSceneCollector(
        include_titles=bool(getattr(desktop, "scene_titles_enabled", False)),
        max_windows=int(getattr(desktop, "scene_max_windows", DEFAULT_MAX_WINDOWS)),
    )
