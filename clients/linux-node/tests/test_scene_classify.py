import json

from virtual_life_linux.scene_classify import (
    classify_activity,
    classify_focus,
)


def test_classify_activity_by_app_id():
    assert classify_activity("kitty") == "terminal"
    assert classify_activity("code") == "code"
    assert classify_activity("codium") == "code"
    assert classify_activity("firefox") == "browser"
    assert classify_activity("mpv") == "video"
    assert classify_activity("zoom", "Zoom Meeting") == "meeting"
    assert classify_activity("org.telegram.desktop") == "chat"
    assert classify_activity("org.gnome.Evince") == "docs"
    assert classify_activity("steam") == "game"


def test_classify_activity_by_title():
    assert classify_activity("", "GitHub - Pull requests") == "unknown"
    assert classify_activity("unknown", "meeting - Google Meet") == "meeting"
    assert classify_activity("", "file.py") == "code"
    assert classify_activity("", "document.pdf") == "docs"


def test_classify_activity_fallback_unknown():
    assert classify_activity("weird-app", "something strange") == "unknown"


def test_classify_focus():
    assert classify_focus(True) == "focused"
    assert classify_focus(False) == "unknown"


def test_scene_encoding_is_json_safe():
    # Ensure the module never produces non-JSON values.
    encoded = json.dumps(
        {
            "activity": classify_activity("firefox", "Mozilla Firefox"),
            "focus": classify_focus(True),
        }
    )
    assert "activity" in encoded
