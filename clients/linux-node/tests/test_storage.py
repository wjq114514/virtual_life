from virtual_life_linux.storage import ClientStorage


def make(tmp_path) -> ClientStorage:
    return ClientStorage(tmp_path / "state.sqlite")


def test_identity_is_persisted_once(tmp_path):
    store = make(tmp_path)
    try:
        node, device = store.ensure_identity("node-a", "device-a")
        assert (node, device) == ("node-a", "device-a")
        node2, device2 = store.ensure_identity("node-b", "device-b")
        assert (node2, device2) == ("node-a", "device-a")
    finally:
        store.close()


def test_cursor_roundtrip(tmp_path):
    store = make(tmp_path)
    try:
        assert store.get_cursor() == (0, "")
        store.set_cursor(42, "gen-1")
        assert store.get_cursor() == (42, "gen-1")
    finally:
        store.close()


def test_command_recovery(tmp_path):
    store = make(tmp_path)
    try:
        store.record_command("cmd-1", "comp", "fp", "2026-01-01T00:00:00Z")
        store.record_command("cmd-1", "comp", "fp", "2026-01-01T00:00:00Z")
        pending = store.pending_commands()
        assert [row.command_id for row in pending] == ["cmd-1"]
        store.save_command_result("cmd-1", {"status": "completed"}, "2026-01-01T00:00:01Z")
        assert store.pending_commands() == []
        row = store.get_command("cmd-1")
        assert row is not None and row.status == "settled"
        assert row.result_json == '{"status": "completed"}'
    finally:
        store.close()


def test_dedup_mark(tmp_path):
    store = make(tmp_path)
    try:
        assert store.mark("k", "2026-01-01T00:00:00Z") is True
        assert store.mark("k", "2026-01-01T00:00:00Z") is False
        assert store.seen("k") is True
        assert store.seen("other") is False
    finally:
        store.close()
