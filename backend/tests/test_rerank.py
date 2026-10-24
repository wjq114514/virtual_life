"""CPU reranking is bounded, fail-open and never part of ordinary recall."""

import pytest
from virtual_life.model_provider import ProviderError
from virtual_life.ports import RerankResult
from virtual_life.rerank import RerankService


class FakeProvider:
    def __init__(self, scores=None, error=None):
        self.scores = scores
        self.error = error
        self.calls = []

    def rerank(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return RerankResult(self.scores, request.model)


def rows(*identifiers):
    return [
        {"memory_id": identifier, "content": f"memory {identifier}"} for identifier in identifiers
    ]


def test_rerank_reorders_only_the_bounded_head():
    provider = FakeProvider([0.1, 0.9, 0.5])
    service = RerankService(
        provider,
        model_name="reranker",
        enabled=True,
        candidate_limit=3,
        min_candidates=1,
        rrf_weight=0.0,
    )
    result, info = service.rerank("sui", "茶", rows("a", "b", "c", "d"))
    assert [row["memory_id"] for row in result] == ["b", "c", "a", "d"]
    assert info["applied"] is True
    assert info["candidates"] == 3
    assert info["error_code"] is None
    assert len(provider.calls[0].texts) == 3


def test_rerank_failure_and_circuit_breaker_fail_open():
    provider = FakeProvider(error=ProviderError("rerank_transport_error", "timeout"))
    service = RerankService(
        provider,
        model_name="reranker",
        enabled=True,
        candidate_limit=8,
        min_candidates=1,
        rrf_weight=0.15,
        failure_cooldown_seconds=60,
    )
    original = rows("a", "b")
    first, first_info = service.rerank("sui", "茶", original)
    second, second_info = service.rerank("sui", "茶", original)
    third, third_info = service.rerank("sui", "茶", original)
    assert first is original and second is original and third is original
    assert first_info["error_code"] == "rerank_transport_error"
    assert second_info["error_code"] == "rerank_transport_error"
    assert third_info["error_code"] == "rerank_circuit_open"
    assert len(provider.calls) == 2


def test_rerank_skips_short_candidate_sets_and_disabled_service():
    provider = FakeProvider([0.1, 0.2])
    service = RerankService(
        provider,
        model_name="reranker",
        enabled=True,
        candidate_limit=8,
        min_candidates=2,
        rrf_weight=0.15,
    )
    original = rows("a")
    result, info = service.rerank("sui", "茶", original)
    assert result is original and info["applied"] is False
    assert not provider.calls

    disabled = RerankService(provider, enabled=False)
    result, info = disabled.rerank("sui", "茶", rows("a", "b"))
    assert result is not None and info["enabled"] is False
    assert not provider.calls


@pytest.mark.parametrize(
    "scores",
    [[0.1], [0.1, float("nan")], [0.1, "0.2"]],
)
def test_invalid_rerank_scores_fail_open(scores):
    provider = FakeProvider(scores)
    service = RerankService(
        provider,
        model_name="reranker",
        enabled=True,
        candidate_limit=8,
        min_candidates=1,
        rrf_weight=0.15,
    )
    original = rows("a", "b")
    result, info = service.rerank("sui", "茶", original)
    assert result is original and info["applied"] is False
    assert info["error_code"] == "rerank_invalid_scores"
