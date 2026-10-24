from virtual_life_linux.adapters import AttentionObservation, collect_all
from virtual_life_linux.adapters.logind import parse_session_properties
from virtual_life_linux.adapters.niri import parse_workspaces


def test_parse_workspaces_ignores_names():
    observation = parse_workspaces(
        [
            {"id": 1, "idx": 1, "name": "secret-project", "output": "DP-1", "is_focused": False},
            {"id": 2, "idx": 2, "name": "private", "output": "DP-1", "is_focused": True},
        ]
    )
    assert observation is not None
    assert observation.data == {"workspace_id": 2, "workspace_idx": 2, "output": "DP-1"}
    assert "secret-project" not in str(observation.data)


def test_parse_workspaces_without_focus():
    assert parse_workspaces([{"id": 1, "idx": 1}]) is None


def test_parse_session_properties_is_bounded():
    observation = parse_session_properties({"IdleHint": 1, "LockedHint": 0, "Extra": "data"})
    assert observation.data == {"idle_hint": True, "locked_hint": False}


class _Broken:
    name = "broken"

    def available(self) -> bool:
        return True

    async def collect(self):
        raise RuntimeError("boom")


class _Good:
    name = "good"

    def available(self) -> bool:
        return True

    async def collect(self):
        return [AttentionObservation(source="good", kind="tick", data={})]


class _Absent:
    name = "absent"

    def available(self) -> bool:
        return False

    async def collect(self):
        return [AttentionObservation(source="absent", kind="tick", data={})]


async def test_collect_all_isolates_failures():
    observations = await collect_all([_Broken(), _Good(), _Absent()])
    assert [item.source for item in observations] == ["good"]
