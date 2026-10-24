import json

from virtual_life_linux.adapters.niri_scene import (
    NiriSceneCollector,
    build_scene,
    frame_fields,
    from_desktop,
    sanitize_title,
)

OUTPUTS = [
    {"name": "DP-1", "logical": {"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0}}
]
WORKSPACES = [
    {"id": 1, "output": "DP-1", "is_active": True, "is_focused": True},
    {"id": 2, "output": "DP-1", "is_active": False, "is_focused": False},
]
WINDOWS = [
    {
        "id": 10,
        "app_id": "firefox",
        "title": "GitHub – Pull requests",
        "workspace_id": 1,
        "is_focused": True,
        "output": "DP-1",
        "layout": {
            "pos_in_scrolling_layout": [0, 0],
            "tile_size": [960, 1080],
            "window_size": [960, 1080],
        },
    },
    {
        "id": 11,
        "app_id": "kitty",
        "title": "~/proj",
        "workspace_id": 1,
        "is_focused": False,
        "output": "DP-1",
        "layout": {"pos_in_scrolling_layout": [1, 0], "window_size": [960, 1080]},
    },
    {
        "id": 12,
        "app_id": "hidden",
        "title": "secret-doc",
        "workspace_id": 2,
        "is_focused": False,
        "output": "DP-1",
        "layout": {"pos_in_scrolling_layout": [0, 0]},
    },
]


def test_sanitize_title_drops_invisible_characters():
    assert sanitize_title("hel\u200blo\u202e world") == "hello world"


def test_scene_without_titles_never_leaks_title_text():
    scene = build_scene(WINDOWS, WORKSPACES, OUTPUTS, include_titles=False)
    encoded = json.dumps(scene.client_features, ensure_ascii=False)
    assert "GitHub" not in encoded
    assert "firefox" in encoded and "kitty" in encoded
    assert [item["role"] for item in scene.client_features["ui_elements"]] == [
        "focused_window",
        "window",
    ]


def test_scene_with_titles_describes_only_visible_windows():
    scene = build_scene(WINDOWS, WORKSPACES, OUTPUTS, include_titles=True)
    encoded = json.dumps(scene.client_features, ensure_ascii=False)
    assert "GitHub – Pull requests" in encoded
    assert "secret-doc" not in encoded
    assert scene.host_metadata == {"app_id": "firefox", "window_id": "10"}


def test_exact_geometry_uses_workspace_view_position():
    windows = [
        {
            "id": 20,
            "app_id": "mpv",
            "title": "clip",
            "workspace_id": 1,
            "is_focused": True,
            "output": "DP-1",
            "layout": {
                "pos_in_scrolling_layout": [0, 0],
                "tile_pos_in_workspace_view": [100, 50],
                "window_size": [400, 300],
            },
        }
    ]
    scene = build_scene(windows, WORKSPACES, OUTPUTS, include_titles=True)
    element = scene.client_features["ui_elements"][0]
    assert element["confidence"] == 1.0
    assert element["bbox"] == [100 / 1920, 50 / 1080, 500 / 1920, 350 / 1080]


def test_frame_fields_shape_matches_frame_payload():
    scene = build_scene(WINDOWS, WORKSPACES, OUTPUTS, include_titles=True)
    fields = frame_fields(scene)
    assert set(fields) == {"client_features", "host_metadata"}
    assert fields["client_features"]["extractor"] == "niri"


def test_from_desktop_honors_opt_in_flags():
    class Desktop:
        scene_titles_enabled = True
        scene_max_windows = 4

    collector = from_desktop(Desktop())
    assert isinstance(collector, NiriSceneCollector)
    assert collector.include_titles is True
    assert collector.max_windows == 4
