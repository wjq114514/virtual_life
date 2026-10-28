"""Rule-based activity classification for host-reported scene data.

This is a *rule* layer, not a model: every decision must be traceable back to an
input field (app id, title, role) so misclassification can be debugged. It is
deliberately coarse (the same labels the server already accepts) and returns
``unknown`` instead of guessing when no rule fires.

The labels match ``AnalysisActivity`` / ``AttentionSample.activity`` on the
server: code, docs, chat, meeting, video, game, media, terminal, browser, unknown.
"""

from __future__ import annotations

_TERMINAL_APPS = {
    "kitty",
    "alacritty",
    "foot",
    "footclient",
    "ghostty",
    "wezterm",
    "gnome-terminal",
    "xfce4-terminal",
    "konsole",
    "tilix",
    "ptyxis",
    "xterm",
    "urxvt",
    "st",
}

_CODE_APPS = {
    "code",
    "codium",
    "vscodium",
    "cursor",
    "zed",
    "zed-editor",
    "sublime_text",
    "sublime",
    "idea",
    "pycharm",
    "webstorm",
    "goland",
    "intellij-idea-ultimate",
    "neovim",
    "nvim",
    "vim",
    "emacs",
    "helix",
    "micro",
}

_BROWSER_APPS = {
    "firefox",
    "chromium",
    "chromium-browser",
    "chrome",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "microsoft-edge",
    "opera",
    "vivaldi",
    "zen",
    "epiphany",
}

_VIDEO_APPS = {
    "mpv",
    "vlc",
    "celluloid",
    "gnome-mpv",
    "io.mpv.mpv",
    "org.videolan.vlc",
}

_MEETING_APPS = {
    "zoom",
    "us.zoom.zoom",
    "teams",
    "teams-for-linux",
    "com.microsoft.teams",
    "webex",
    "gather",
    "jitsi",
}

_GAME_APPS = {
    "steam",
    "com.valvesoftware.steam",
    "lutris",
    "heroic",
    "bottles",
    "gamescope",
    "prismlauncher",
}

_MEDIA_APPS = {
    "spotify",
    "com.spotify.client",
    "rhythmbox",
    "clementine",
    "audacious",
    "ncmpcpp",
}

_CHAT_APPS = {
    "telegramdesktop",
    "org.telegram.desktop",
    "discord",
    "com.discordapp.discord",
    "slack",
    "whatsapp",
    "wechat",
    "signal-desktop",
    "element",
    "neochat",
    "org.gnome.evolution",
}

_DOCS_APPS = {
    "evince",
    "org.gnome.evince",
    "okular",
    "org.kde.okular",
    "zathura",
    "xreader",
    "eog",
    "org.gnome.eog",
    "sioyek",
    "org.onlyoffice",
    "libreoffice",
    "soffice",
    "org.libreoffice.libreoffice",
}

_CODE_TITLE_MARKERS = (
    " — Visual Studio Code",
    " — VSCodium",
    " — Zed",
    " — Sublime Text",
    " — PyCharm",
    " — IntelliJ IDEA",
    " — GoLand",
    " — WebStorm",
    " — Neovim",
    " — Vim",
    " — Emacs",
)
_CODE_EXTENSIONS = (
    ".py",
    ".rs",
    ".go",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".kt",
    ".kts",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cs",
    ".rb",
    ".php",
    ".lua",
    ".sh",
    ".bash",
    ".zsh",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".sql",
)
_MEETING_TITLE_MARKERS = ("zoom", "meet", "google meet", "teams", "webex", "jitsi", "gather")
_VIDEO_TITLE_MARKERS = ("youtube", "bilibili", "netflix", "twitch", " - mpv")
_BROWSER_TITLE_MARKERS = ("mozilla firefox", "chromium", "google chrome", "brave")
_DOCS_TITLE_EXTENSIONS = (".pdf", ".epub", ".djvu", ".odt", ".doc", ".docx", ".xlsx", ".pptx")
_CHAT_TITLE_MARKERS = ("discord", "slack", "telegram", "whatsapp", "signal", "微信", "element")


def classify_activity(app_id: str, title: str = "") -> str:
    """Classify one focused window into a coarse activity label.

    Order matters: terminal and code are checked before browser/docs because a
    terminal can carry a browser-like title, and an editor can be titled with a
    document name. Returns ``unknown`` when no rule fires.
    """
    app = (app_id or "").strip().lower()
    raw_title = (title or "").strip()
    title_lower = raw_title.lower()

    if app in _GAME_APPS or app.startswith("steam"):
        return "game"
    if app in _MEETING_APPS or any(marker in title_lower for marker in _MEETING_TITLE_MARKERS):
        return "meeting"
    if app in _VIDEO_APPS or any(marker in title_lower for marker in _VIDEO_TITLE_MARKERS):
        return "video"
    if app in _CODE_APPS or any(marker in title_lower for marker in _CODE_TITLE_MARKERS):
        return "code"
    if app in _CODE_APPS:
        return "code"
    if app in _TERMINAL_APPS:
        return "terminal"
    if app in _BROWSER_APPS:
        return "browser"
    if app in _CHAT_APPS or any(marker in title_lower for marker in _CHAT_TITLE_MARKERS):
        return "chat"
    if app in _MEDIA_APPS:
        return "media"
    if app in _DOCS_APPS or raw_title.endswith(_DOCS_TITLE_EXTENSIONS):
        return "docs"
    # A bare source filename without a known editor app id: still likely code.
    if raw_title.endswith(_CODE_EXTENSIONS) and "/" not in raw_title:
        return "code"
    return "unknown"


def classify_focus(has_focused_window: bool) -> str:
    """Map the presence of a focused window to a server ``focus`` value."""
    return "focused" if has_focused_window else "unknown"
