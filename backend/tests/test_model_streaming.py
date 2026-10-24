"""Network framing and conservative settlement for streaming chat providers."""

import json
from dataclasses import replace

import httpx
import pytest
from virtual_life.model_provider import CompatibleChatProvider, ProviderError
from virtual_life.ports import (
    ModelImage,
    ModelRequest,
    ModelUsage,
    StreamingModelProvider,
    ToolDefinition,
)

REQUEST = ModelRequest("task", "companion", "dialogue", "test-model", "hello", 100, 50)
USAGE = {"prompt_tokens": 12, "completion_tokens": 4}


def event(value):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\r\n\r\n"


def choice(content=None, reason=None, **delta):
    return {
        "choices": [{"index": 0, "delta": {"content": content, **delta}, "finish_reason": reason}]
    }


def success():
    return (
        event(choice("你好"))
        + event(choice("!", "stop"))
        + event({"choices": [], "usage": USAGE})
        + b"data: [DONE]\r\n\r\n"
    )


class BytesStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def setup_provider(monkeypatch, chunks, status=200):
    stream = BytesStream(chunks)
    seen = []
    real_client = httpx.Client

    def handle(request):
        seen.append(request)
        return httpx.Response(status, stream=stream)

    clients = []

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handle)
        result = real_client(**kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, "Client", client)
    provider = CompatibleChatProvider("https://example.test/v1", "sensitive-key")
    assert isinstance(provider, StreamingModelProvider)
    return provider, stream, seen, clients


def test_incremental_multibyte_frames_and_usage(monkeypatch):
    drafts = []
    first = event(choice("你好", role="assistant", reasoning_content="secret thought"))

    def chunks():
        # Split inside UTF-8 and SSE delimiters, and prove callback ran before completion arrived.
        for byte in b": heartbeat\r\n\r\n" + first:
            yield bytes([byte])
        assert drafts == ["你好"]
        yield (
            b'data: {"choices": [],\r\ndata: "usage": ' + json.dumps(USAGE).encode() + b"}\r\n\r\n"
        )
        yield event(choice("!", "length"))
        yield event({"choices": [], "usage": None})
        yield b"data: [DONE]\n\n"

    provider, stream, seen, clients = setup_provider(monkeypatch, chunks())
    result = provider.stream(REQUEST, drafts.append)
    assert result.text == "你好!"
    assert result.usage == ModelUsage(12, 4)
    assert stream.closed and clients[0].is_closed
    body = json.loads(seen[0].content)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 50
    assert "Do not call tools" in body["messages"][0]["content"]


def test_image_is_sent_as_data_uri_before_generation(monkeypatch):
    provider, _, seen, clients = setup_provider(monkeypatch, [success()])
    request = replace(
        REQUEST,
        images=(ModelImage(b"\x89PNG\r\n\x1a\nfake", "image/png", "high"),),
    )
    result = provider.stream(request, lambda _: None)
    assert result.text == "你好!"
    body = json.loads(seen[0].content)
    content = body["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "hello"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["detail"] == "high"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert clients[0].is_closed


def test_cumulative_streaming_usage_uses_last_value(monkeypatch):
    wire = (
        event(choice("你"))
        + event({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 1}})
        + event(choice("好", "stop"))
        + event({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2}})
        + b"data: [DONE]\n\n"
    )
    provider, _, _, clients = setup_provider(monkeypatch, [wire])
    result = provider.stream(REQUEST, lambda _: None)
    assert result.text == "你好"
    assert result.usage == ModelUsage(12, 2)
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "wire,usage",
    [
        (success().replace(b"data: [DONE]\r\n\r\n", b""), ModelUsage(12, 4)),
        (event(choice("hello", "stop")) + b"data: [DONE]\n\n", None),
        (
            event(choice("hello")) + event({"choices": [], "usage": USAGE}) + b"data: [DONE]\n\n",
            ModelUsage(12, 4),
        ),
        (
            event({"choices": [], "usage": USAGE}) + b"data: invalid sensitive-key\n\n",
            ModelUsage(12, 4),
        ),
        (
            event({"choices": [], "usage": USAGE})
            + event({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 4}}),
            ModelUsage(12, 4),
        ),
        (event(choice([{"type": "text", "text": "hello"}])), None),
        (event(choice("hello", tool_calls=[{"name": "tool"}])), None),
        (event({"choices": [choice("a")["choices"][0]] * 2, "usage": USAGE}), ModelUsage(12, 4)),
        (event(choice("hello", "tool_calls")), None),
        (event(choice("a" * 65537)), None),
        (b"data: " + b"a" * (256 * 1024), None),
        (b": " + b"a" * (2 * 1024 * 1024), None),
    ],
)
def test_invalid_stream_never_commits_and_preserves_usage(monkeypatch, wire, usage):
    provider, stream, _, clients = setup_provider(monkeypatch, [wire])
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.usage == usage
    assert caught.value.dispatched and not caught.value.releasable
    assert "sensitive-key" not in str(caught.value)
    assert stream.closed and clients[0].is_closed


def test_http_failure_preserves_usage_without_body(monkeypatch):
    provider, stream, _, _ = setup_provider(
        monkeypatch, [json.dumps({"usage": USAGE, "error": "sensitive-key"}).encode()], 429
    )
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.code == "model_http_error"
    assert caught.value.usage == ModelUsage(12, 4)
    assert "sensitive-key" not in str(caught.value)
    assert stream.closed


def test_callback_failure_has_separate_code_and_usage(monkeypatch):
    provider, stream, _, _ = setup_provider(
        monkeypatch, [event({"choices": [], "usage": USAGE}), event(choice("hello"))]
    )

    def fail(_):
        raise ValueError("sensitive-key")

    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, fail)
    assert caught.value.code == "model_callback_error"
    assert caught.value.usage == ModelUsage(12, 4)
    assert "sensitive-key" not in str(caught.value)
    assert stream.closed


def test_transport_failure_after_usage_preserves_usage(monkeypatch):
    def chunks():
        yield event({"choices": [], "usage": USAGE})
        raise httpx.ReadError("sensitive-key")

    provider, stream, _, _ = setup_provider(monkeypatch, chunks())
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.code == "model_transport_error"
    assert caught.value.usage == ModelUsage(12, 4)
    assert "sensitive-key" not in str(caught.value)
    assert stream.closed


def test_oversized_single_network_chunk_preserves_earlier_usage(monkeypatch):
    provider, _, _, _ = setup_provider(
        monkeypatch,
        [event({"choices": [], "usage": USAGE}) + b": " + b"a" * (2 * 1024 * 1024)],
    )
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.usage == ModelUsage(12, 4)


def test_invalid_header_fails_before_dispatch(monkeypatch):
    provider, _, seen, clients = setup_provider(monkeypatch, [success()])
    provider.api_key = "invalid\nsecret"
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.code == "model_request_invalid"
    assert not caught.value.dispatched and caught.value.releasable
    assert seen == []
    assert clients[0].is_closed


def test_complete_keeps_nonstreaming_contract(monkeypatch):
    payload = {"choices": [{"message": {"content": "hello"}}], "usage": USAGE}
    provider, stream, seen, _ = setup_provider(monkeypatch, [json.dumps(payload).encode()])
    result = provider.complete(REQUEST)
    assert result.text == "hello"
    assert json.loads(seen[0].content)["stream"] is False
    assert stream.closed


def test_complete_advertises_registry_beyond_two_tools(monkeypatch):
    payload = {"choices": [{"message": {"content": "hello"}}], "usage": USAGE}
    provider, _, seen, _ = setup_provider(monkeypatch, [json.dumps(payload).encode()])
    tools = tuple(
        ToolDefinition(f"plugin_{index}", "A plugin tool.", {"type": "object"})
        for index in range(3)
    )
    result = provider.complete(replace(REQUEST, tools=tools))
    assert result.text == "hello"
    assert len(json.loads(seen[0].content)["tools"]) == 3


def test_empty_finish_reason_on_role_and_content_chunks(monkeypatch):
    wire = (
        event(choice(reason="", role="assistant"))
        + event(choice("你好", ""))
        + event(choice("!", ""))
        + event(choice(reason="stop"))
        + event({"choices": [], "usage": USAGE})
        + b"data: [DONE]\n\n"
    )
    provider, stream, _, clients = setup_provider(monkeypatch, [wire])
    drafts = []
    result = provider.stream(REQUEST, drafts.append)
    assert drafts == ["你好", "!"]
    assert result.text == "你好!"
    assert result.usage == ModelUsage(12, 4)
    assert stream.closed and clients[0].is_closed


@pytest.mark.parametrize("done", [b"", b"data: [DONE]\n\n"])
def test_empty_finish_reason_never_completes_stream(monkeypatch, done):
    wire = event(choice("hello", "")) + event({"choices": [], "usage": USAGE}) + done
    provider, _, _, _ = setup_provider(monkeypatch, [wire])
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.code == "model_invalid_response"
    assert caught.value.usage == ModelUsage(12, 4)


@pytest.mark.parametrize("reason", [False, 0, [], {}, "unknown", "content_filter", "tool_calls"])
def test_invalid_finish_reason_remains_rejected(monkeypatch, reason):
    wire = (
        event({"choices": [], "usage": USAGE})
        + event(choice("hello", ""))
        + event(choice(reason=reason))
        + event(choice(reason="stop"))
        + b"data: [DONE]\n\n"
    )
    provider, _, _, _ = setup_provider(monkeypatch, [wire])
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.code == "model_invalid_response"
    assert caught.value.usage == ModelUsage(12, 4)


@pytest.mark.parametrize("extra", [choice("late", ""), choice(reason="stop")])
def test_empty_finish_reason_keeps_post_completion_guards(monkeypatch, extra):
    wire = (
        event(choice("hello", ""))
        + event(choice(reason="stop"))
        + event({"choices": [], "usage": USAGE})
        + event(extra)
        + b"data: [DONE]\n\n"
    )
    provider, _, _, _ = setup_provider(monkeypatch, [wire])
    with pytest.raises(ProviderError) as caught:
        provider.stream(REQUEST, lambda _: None)
    assert caught.value.code == "model_invalid_response"
    assert caught.value.usage == ModelUsage(12, 4)
