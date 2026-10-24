"""TEI rerank wire adapter tests without network access."""

import json

import httpx
import pytest
from virtual_life.model_provider import CompatibleRerankProvider, ProviderError
from virtual_life.ports import RerankRequest


def _request():
    return RerankRequest("task", "sui", "bge-reranker-base", "茶", ["乌龙茶", "公园散步"])


def test_tei_array_response_is_mapped_back_to_input_order(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {"index": 1, "score": 0.9},
                {"index": 0, "score": 0.2},
            ],
        )

    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    provider = CompatibleRerankProvider("http://rerank.invalid", timeout_seconds=0.5)
    result = provider.rerank(_request())
    assert result.scores == [0.2, 0.9]
    assert seen["body"]["query"] == "茶"
    assert seen["body"]["texts"] == ["乌龙茶", "公园散步"]
    assert seen["body"]["raw_scores"] is False
    assert seen["body"]["truncate"] is True


@pytest.mark.parametrize(
    "payload",
    [
        [{"index": 0, "score": 0.2}],
        [{"index": 0, "score": 0.2}, {"index": 0, "score": 0.3}],
        [{"index": 0, "score": 0.2}, {"index": 2, "score": 0.3}],
        [{"index": 0, "score": 0.2}, {"index": 1, "score": "0.3"}],
    ],
)
def test_invalid_tei_response_is_rejected(monkeypatch, payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    provider = CompatibleRerankProvider("http://rerank.invalid")
    with pytest.raises(ProviderError) as error:
        provider.rerank(_request())
    assert error.value.code == "rerank_invalid_response"
