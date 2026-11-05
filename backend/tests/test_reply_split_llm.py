"""LLM semantic reply split (MaiBot llm_sentence_splitter, conservative port).

The model may only choose cut points; its answer must reproduce the original
text verbatim (whitespace-insensitive) or the split is rejected and the caller
falls back to the deterministic rule split.
"""

from __future__ import annotations

import json

from virtual_life.ports import ModelRequest, ModelResult, ModelUsage
from virtual_life.reply_split import llm_split_reply, split_reply

TEXT = (
    "今天走了不少路，腿有点酸，不过路上的风很舒服。傍晚的时候云被染成了橘色，"
    "我在桥上站了一会儿，看河水把颜色揉碎又拼回去。回来的路上买了两个桃子，"
    "很甜，想留一个给你。"
)


class FakeProvider:
    def __init__(self, payload: str):
        self.payload = payload
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return ModelResult(
            text=self.payload,
            model=request.model,
            usage=ModelUsage(input_tokens=100, output_tokens=20),
        )


class FailingProvider:
    def complete(self, request: ModelRequest) -> ModelResult:
        raise RuntimeError("provider down")


def _bubbles(parts: list[str]) -> str:
    return json.dumps({"messages": parts}, ensure_ascii=False)


def test_verbatim_split_is_accepted() -> None:
    parts = [TEXT[:14], TEXT[14:34], TEXT[34:]]
    provider = FakeProvider(_bubbles(parts))
    assert llm_split_reply(TEXT, provider, model="m", companion_id="c") == parts
    # One model call, purpose dialogue, prompt carries the full original text.
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.purpose == "dialogue"
    assert TEXT in request.context_text


def test_model_rewrite_is_rejected() -> None:
    # A model that paraphrases (drops a character) must be rejected.
    parts = [TEXT[:14], TEXT[14:33], TEXT[34:]]
    provider = FakeProvider(_bubbles(parts))
    assert llm_split_reply(TEXT, provider, model="m", companion_id="c") is None


def test_too_many_bubbles_is_rejected() -> None:
    parts = [TEXT[i : i + 10] for i in range(0, len(TEXT), 10)]
    assert len(parts) > 3
    provider = FakeProvider(_bubbles(parts))
    assert (
        llm_split_reply(
            TEXT,
            provider,
            model="m",
            companion_id="c",
            max_messages=3,
        )
        is None
    )


def test_fenced_or_prose_wrapped_json_is_parsed() -> None:
    parts = [TEXT[:20], TEXT[20:]]
    provider = FakeProvider("好的，切分如下：\n```json\n" + _bubbles(parts) + "\n```")
    assert llm_split_reply(TEXT, provider, model="m", companion_id="c") == parts


def test_provider_error_returns_none() -> None:
    assert llm_split_reply(TEXT, FailingProvider(), model="m", companion_id="c") is None


def test_empty_or_unparseable_output_returns_none() -> None:
    assert llm_split_reply(TEXT, FakeProvider(""), model="m", companion_id="c") is None
    assert (
        llm_split_reply(TEXT, FakeProvider("not json at all"), model="m", companion_id="c") is None
    )
    assert (
        llm_split_reply(
            TEXT, FakeProvider(json.dumps({"messages": "nope"})), model="m", companion_id="c"
        )
        is None
    )


def test_fallback_matches_rule_split() -> None:
    # The dialogue integration falls back to split_reply when the LLM answer is
    # unusable; the rule split must therefore still handle the same text.
    provider = FakeProvider(_bubbles(["完全不相干的回复"]))
    assert llm_split_reply(TEXT, provider, model="m", companion_id="c") is None
    rules = split_reply(TEXT, max_messages=3, max_chars=120)
    assert rules and "".join(rules) == TEXT


def test_short_or_missing_inputs_return_none() -> None:
    provider = FakeProvider(_bubbles([""]))
    assert llm_split_reply("", provider, model="m", companion_id="c") is None
    assert llm_split_reply(TEXT, None, model="m", companion_id="c") is None
    assert llm_split_reply(TEXT, provider, model="", companion_id="c") is None
