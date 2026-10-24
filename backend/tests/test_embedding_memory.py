"""Semantic retrieval without network access using deterministic provider vectors."""

import json
from datetime import timedelta

import pytest
from test_memory import Clock, write
from virtual_life.embedding_memory import EmbeddingMemoryService, _hash
from virtual_life.memory import MemoryService, _normalize
from virtual_life.ports import EmbeddingResult, ModelUsage
from virtual_life.vector_codec import decode_vector, encode_vector


class VectorProvider:
    base_url = "http://embedding.invalid"

    def __init__(self):
        self.calls = []
        self.vectors = None
        self.action = lambda: None

    def embed(self, request):
        self.calls.append(request)
        self.action()
        vectors = self.vectors or [
            [1.0, 0.0] if "茶" in text else [0.0, 1.0] for text in request.texts
        ]
        return EmbeddingResult(vectors, request.model, ModelUsage(5, 0))


def setup(tmp_path, **kwargs):
    clock = Clock()
    memory = MemoryService(tmp_path / "vectors.sqlite", clock=clock)
    provider = VectorProvider()
    embeddings = EmbeddingMemoryService(
        memory, provider=provider, model_name="test-bge", enabled=True, dimensions=2, **kwargs
    )
    return memory, embeddings, provider, clock


def test_index_query_cache_expiry_and_billing(tmp_path):
    memory, embeddings, provider, clock = setup(tmp_path, query_cache_ttl_seconds=10)
    tea = write(memory, "tea", "乌龙茶", kind="preference")
    write(memory, "walk", "公园散步")
    assert embeddings.run("sui")["status"] == "completed"
    assert embeddings.run("sui")["status"] == "idle"
    assert embeddings.prepare_query("sui", "茶")["model_calls"] == 1
    assert embeddings.prepare_query("sui", " 茶 ")["cache_hit"]
    with memory._connection() as connection:
        rows, hit = embeddings.retrieve_cached(connection, "sui", "茶")
    assert hit and [row["memory_id"] for row in rows] == [tea["memory_id"]]
    assert memory.budget_snapshot("sui")["spent_tokens"] == 10
    clock.value += timedelta(seconds=11)
    assert embeddings.prepare_query("sui", "茶")["model_calls"] == 1
    assert len(provider.calls) == 3


@pytest.mark.parametrize("vector", [[0.0, 0.0], [float("nan"), 1.0], [1.0, 0.0, 0.0]])
def test_invalid_dimensions_or_vectors_never_persist(tmp_path, vector):
    memory, embeddings, provider, _ = setup(tmp_path)
    write(memory, "tea", "乌龙茶", kind="preference")
    provider.vectors = [vector]
    result = embeddings.run("sui")
    assert result["status"] == "failed"
    assert embeddings.status("sui")["stored_vectors"] == 0
    assert memory.budget_snapshot("sui")["spent_tokens"] == 5
    assert embeddings.run("sui")["status"] == "idle"


def test_revision_delete_companion_and_space_filters(tmp_path):
    memory, embeddings, provider, _ = setup(tmp_path)
    record = write(memory, "tea", "乌龙茶", kind="preference")
    write(memory, "other", "别人的茶", companion="other", kind="preference")
    assert embeddings.run("sui")["status"] == "completed"
    embeddings.prepare_query("sui", "茶")
    assert embeddings.prepare_query("other", "茶")["model_calls"] == 1
    with memory._connection() as connection:
        assert embeddings.retrieve_cached(connection, "other", "茶")[0] == []
    memory.update_metadata(
        "sui",
        record["memory_id"],
        {"command_id": "update", "expected_revision": 1, "importance": 0.9},
    )
    with memory._connection() as connection:
        assert embeddings.retrieve_cached(connection, "sui", "茶")[0] == []
    assert embeddings.run("sui")["status"] == "completed"
    second = EmbeddingMemoryService(
        memory, provider=provider, model_name="other-space", enabled=True, dimensions=2
    )
    with memory._connection() as connection:
        assert second.retrieve_cached(connection, "sui", "茶") == ([], False)
    memory.delete("sui", record["memory_id"])
    with memory._connection() as connection:
        assert embeddings.retrieve_cached(connection, "sui", "茶")[0] == []


def test_threshold_before_salience_and_protected_old_vectors(tmp_path):
    memory, embeddings, provider, clock = setup(
        tmp_path, candidate_limit=1, similarity_threshold=0.8
    )
    birthday = write(
        memory,
        "birthday",
        "茶友生日6月18日",
        kind="fact",
        semantic_key="user.birthday",
        importance=1.0,
    )
    assert embeddings.run("sui")["status"] == "completed"
    clock.value += timedelta(days=180)
    write(memory, "walk", "公园散步", importance=1.0)
    embeddings.run("sui")
    embeddings.prepare_query("sui", "茶")
    with memory._connection() as connection:
        rows, _ = embeddings.retrieve_cached(connection, "sui", "茶")
    assert [r["memory_id"] for r in rows] == [birthday["memory_id"]]
    assert embeddings.prepare_query("sui", "公园")["model_calls"] == 1
    with memory._connection() as connection:
        rows, _ = embeddings.retrieve_cached(connection, "sui", "公园")
    assert birthday["memory_id"] not in [r["memory_id"] for r in rows]


@pytest.mark.parametrize("failure", ["usage_known", "unknown", "before_dispatch"])
def test_provider_failure_keeps_correct_quota_and_receipts(tmp_path, failure):
    from virtual_life.model_provider import ProviderError

    memory, embeddings, provider, _ = setup(tmp_path)
    write(memory, "tea", "乌龙茶", kind="preference")

    def fail():
        raise ProviderError(
            "test_failure",
            "failure",
            usage=ModelUsage(3, 0) if failure == "usage_known" else None,
            dispatched=failure != "before_dispatch",
            releasable=failure == "before_dispatch",
        )

    provider.action = fail
    result = embeddings.run("sui")
    assert result["status"] == ("unknown" if failure == "unknown" else "failed")
    assert embeddings.status("sui")["stored_vectors"] == 0
    quota = memory.budget_snapshot("sui")
    assert quota["spent_tokens"] == (3 if failure == "usage_known" else 0)
    assert (quota["reserved_tokens"] > 0) == (failure == "unknown")
    # A known failure before dispatch can safely be retried.
    assert embeddings.run("sui")["status"] == ("failed" if failure == "before_dispatch" else "idle")
    assert len(provider.calls) == (2 if failure == "before_dispatch" else 1)


def test_local_embedding_records_usage_without_daily_quota(tmp_path):
    memory, embeddings, provider, _ = setup(tmp_path, local=True)
    write(memory, "tea", "乌龙茶", kind="preference")
    result = embeddings.run("sui")
    assert result["status"] == "completed"
    assert embeddings.status("sui")["local"] is True
    assert memory.budget_snapshot("sui")["spent_tokens"] == 0
    assert embeddings.prepare_query("sui", "茶")["model_calls"] == 1
    assert memory.budget_snapshot("sui")["spent_tokens"] == 0
    with memory._connection() as connection:
        jobs = connection.execute(
            "SELECT status,usage_json FROM embedding_jobs WHERE companion_id='sui'"
        ).fetchall()
    assert {row["status"] for row in jobs} == {"completed"}
    assert all(row["usage_json"] for row in jobs)


def test_semantic_recall_reranks_only_when_allowed(tmp_path):
    from virtual_life.ports import RerankResult
    from virtual_life.rerank import RerankService

    class FakeRerank:
        base_url = "http://rerank.invalid"

        def __init__(self):
            self.calls = 0

        def rerank(self, request):
            self.calls += 1
            return RerankResult([0.1 + index for index in range(len(request.texts))], request.model)

    memory, embeddings, provider, _ = setup(tmp_path)
    fake = FakeRerank()
    embeddings.rerank = RerankService(
        fake,
        model_name="test-rerank",
        enabled=True,
        candidate_limit=8,
        min_candidates=1,
        rrf_weight=0.0,
    )
    write(memory, "tea", "乌龙茶", kind="preference")
    write(memory, "flower", "花茶", kind="preference")
    assert embeddings.run("sui")["status"] == "completed"
    assert embeddings.prepare_query("sui", "茶")["model_calls"] == 1
    assert fake.calls == 0

    ordinary = memory.recall("sui", {"query": "茶", "max_items": 2})
    assert ordinary["rerank"] is None
    assert fake.calls == 0

    recalled = embeddings.semantic_recall("sui", {"query": "茶", "max_items": 2})
    assert recalled["rerank"]["applied"] is True
    assert fake.calls == 1
    assert recalled["retrieval_strategy"].endswith("_reranked")


def _store_legacy_vector(memory, embeddings, record, query_vector, body=None):
    with memory._connection(write=True) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO embedding_spaces VALUES(?,?)",
            (embeddings.space, len(query_vector)),
        )
        connection.execute(
            """INSERT OR REPLACE INTO embedding_vectors
            (space,memory_id,revision,content_hash,companion_id,vector_json,vector_blob)
            VALUES(?,?,?,?,?,?,NULL)""",
            (
                embeddings.space,
                record["memory_id"],
                record["revision"],
                _hash(record["content"]),
                record["companion_id"],
                body if body is not None else json.dumps(query_vector, separators=(",", ":")),
            ),
        )
        connection.execute(
            """INSERT OR REPLACE INTO embedding_query_cache
            (space,companion_id,query_hash,vector_json,expires_at,created_at)
            VALUES(?,?,?,?,?,?)""",
            (
                embeddings.space,
                record["companion_id"],
                _hash(_normalize("茶")),
                json.dumps(query_vector),
                "2099-01-01T00:00:00.000000Z",
                memory._timestamp(memory._now()),
            ),
        )


def test_legacy_json_vectors_remain_readable_and_are_backfilled(tmp_path):
    memory, embeddings, _, _ = setup(tmp_path)
    record = write(memory, "tea", "乌龙茶", kind="preference")
    _store_legacy_vector(memory, embeddings, record, [1.0, 0.0])

    with memory._connection() as connection:
        rows, hit = embeddings.retrieve_cached(connection, "sui", "茶")
    assert hit and [row["memory_id"] for row in rows] == [record["memory_id"]]

    with memory._connection(write=True) as connection:
        assert embeddings._backfill_vector_blobs(connection) == 1
        stored = connection.execute(
            "SELECT vector_blob,vector_json FROM embedding_vectors WHERE memory_id=?",
            (record["memory_id"],),
        ).fetchone()
    assert stored["vector_blob"] is not None
    assert stored["vector_json"] == ""
    assert list(decode_vector(stored["vector_blob"], 2)) == pytest.approx([1.0, 0.0])

    with memory._connection() as connection:
        rows, hit = embeddings.retrieve_cached(connection, "sui", "茶")
    assert hit and [row["memory_id"] for row in rows] == [record["memory_id"]]


def test_corrupt_legacy_vector_is_skipped_without_breaking_recall(tmp_path):
    memory, embeddings, _, _ = setup(tmp_path)
    record = write(memory, "tea", "乌龙茶", kind="preference")
    _store_legacy_vector(memory, embeddings, record, [1.0, 0.0], body="{not-json")

    with memory._connection() as connection:
        assert embeddings.retrieve_cached(connection, "sui", "茶") == ([], True)
        with memory._connection(write=True) as write_connection:
            assert embeddings._backfill_vector_blobs(write_connection) == 1
            assert (
                write_connection.execute(
                    "SELECT vector_blob FROM embedding_vectors WHERE memory_id=?",
                    (record["memory_id"],),
                ).fetchone()["vector_blob"]
                is None
            )


def test_vector_codec_roundtrip_and_dimension_guard():
    blob = encode_vector([3.0, 4.0])
    assert len(blob) == 8
    assert list(decode_vector(blob, 2)) == pytest.approx([0.6, 0.8])
    with pytest.raises(ValueError):
        decode_vector(blob, 3)
    with pytest.raises(ValueError):
        encode_vector([0.0, 0.0])
    with pytest.raises(ValueError):
        encode_vector([float("nan"), 1.0])
