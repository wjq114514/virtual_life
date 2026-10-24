# DMS bridge (status-only)

This directory holds the DankMaterialShell integration. The bridge is a thin,
read-only surface: it shells out to `vl status` (which returns JSON from the
daemon's `status.get`) and renders online state, companion and lease countdown.

It does **not** hold the owner token, does not send dialogue, and does not
attach a renderer. Sending chat is the job of the kitty/Textual TUI so that only
one native writer exists per device.

## Status

The manifest and layout are frozen. The QML widget itself is pending a pinned
DMS/Quickshell version and a real smoke test on the target desktop; see
`docs/linux-client-validation.md`. Do not wire a widget to an unpinned shell API.
