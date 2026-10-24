"""Speaker-label stripping on generated dialogue replies.

Style examples are rendered as "name: utterance" lines and models sometimes
continue that layout into their own answer, leaking a role label into the reply
that reaches the client. These tests pin both the prompt instruction and the
deterministic output normalisation, including the cases that must be left alone.
"""

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from virtual_life.api import create_app
from virtual_life.dialogue import strip_speaker_prefix
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.settings import Settings

NAME = "随"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("随: 阿灰。", "阿灰。"),
        ("随：阿灰。", "阿灰。"),
        ("随:   阿灰。", "阿灰。"),
        ("  随：  阿灰。", "阿灰。"),
        ("随:\n阿灰。", "阿灰。"),
        ("阿灰。", "阿灰。"),
    ],
)
def test_removes_own_name_colon_label(raw, expected):
    assert strip_speaker_prefix(raw, NAME) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "随想起来了——阿灰。",  # bare name, ambiguous with ordinary prose
        "你: 阿灰。",  # a different speaker is not ours to strip
        "阿黛: 你今天累吗。",
        "随:",  # nothing would remain
        "随: ",
        "他说：随：好。",  # only a leading label counts
        "记得，床头右侧。",
    ],
)
def test_leaves_ambiguous_or_foreign_labels_alone(raw):
    assert strip_speaker_prefix(raw, NAME) == raw


def test_name_match_is_case_insensitive_but_whitespace_tolerant():
    assert strip_speaker_prefix("Sui: hello", "sui") == "hello"
    assert strip_speaker_prefix("  sui  :  hello", "Sui") == "hello"


def test_missing_or_non_string_arguments_pass_through():
    assert strip_speaker_prefix("随: hi", "") == "随: hi"
    assert strip_speaker_prefix("随: hi", "   ") == "随: hi"
    assert strip_speaker_prefix(None, NAME) is None


def test_execution_instructions_forbid_reply_labels():
    from virtual_life.model_provider import EXECUTION_INSTRUCTIONS

    assert "speaker name" in EXECUTION_INSTRUCTIONS
    assert "role label" in EXECUTION_INSTRUCTIONS


class PrefixedProvider:
    def __init__(self):
        self.context_text = None

    def complete(self, request):
        self.context_text = request.context_text
        return ModelResult("随: 记得，床头右侧。", "fake", ModelUsage(80, 20))


def test_dialogue_strips_the_label_end_to_end(tmp_path):
    provider = PrefixedProvider()
    settings = Settings(
        database_path=str(tmp_path / "dialogue.sqlite"),
        api_token="test-token",
        model_base_url="http://model.invalid",
        model_name="fake",
    )
    app = create_app(settings, model_provider=provider)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        state = attach_desktop(client)
        response = client.post(
            "/v1/companions/sui/dialogue",
            json={
                "command_id": "prefixed",
                "node_id": "desk",
                "epoch": state["epoch"],
                "expected_revision": state["revision"],
                "user_message": "小夜灯你帮我放在哪边了",
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed"
        assert result["assistant_message"] == "记得，床头右侧。"
        # the durable receipt must replay the normalised text, not the raw provider text
        replay = client.get("/v1/companions/sui/dialogue/prefixed")
        assert replay.status_code == 200, replay.text
        assert replay.json()["assistant_message"] == "记得，床头右侧。"
