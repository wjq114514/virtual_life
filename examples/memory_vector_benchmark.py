"""Benchmark the local vector retrieval path without a network call.

Examples:
    uv run python examples/memory_vector_benchmark.py
    uv run python examples/memory_vector_benchmark.py --records 1000 --repeat 10
    uv run python examples/memory_vector_benchmark.py --legacy
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from pathlib import Path

from virtual_life.embedding_memory import EmbeddingMemoryService, _hash
from virtual_life.memory import MemoryService, _normalize
from virtual_life.vector_codec import encode_vector

COMPANION = "benchmark"


def build(memory, embeddings, records, dimensions, legacy):
    texts = [f"合成记忆片段 {index}：一起讨论项目进展和周末安排。" for index in range(records)]
    now = memory._timestamp(memory._now())
    with memory._connection(write=True) as connection:
        # MemoryService normally shares this table with the coordinator store.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, companion_id TEXT)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO embedding_spaces VALUES(?,?)",
            (embeddings.space, dimensions),
        )
        connection.executemany(
            """INSERT INTO memory_records
            (memory_id, companion_id, kind, content, normalized_content, semantic_key,
             tags_json, importance, confidence, confirmed, pinned, active, created_at,
             updated_at, revision, retention_policy)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    f"mem_{index:05d}",
                    COMPANION,
                    "episode",
                    text,
                    _normalize(text),
                    None,
                    "[]",
                    0.5,
                    0.8,
                    0,
                    0,
                    1,
                    now,
                    now,
                    1,
                    "auto",
                )
                for index, text in enumerate(texts)
            ],
        )
        vectors = []
        for index, text in enumerate(texts):
            angle = index / records * math.tau
            vector = [math.cos(angle), math.sin(angle)] + [0.0] * (dimensions - 2)
            vectors.append(
                (
                    embeddings.space,
                    f"mem_{index:05d}",
                    1,
                    _hash(text),
                    COMPANION,
                    "" if not legacy else json.dumps(vector, separators=(",", ":")),
                    None if legacy else encode_vector(vector),
                )
            )
        connection.executemany(
            """INSERT INTO embedding_vectors
            (space,memory_id,revision,content_hash,companion_id,vector_json,vector_blob)
            VALUES(?,?,?,?,?,?,?)""",
            vectors,
        )
        query_vector = [1.0] + [0.0] * (dimensions - 1)
        connection.execute(
            """INSERT OR REPLACE INTO embedding_query_cache
            (space,companion_id,query_hash,vector_json,expires_at,created_at)
            VALUES(?,?,?,?,?,?)""",
            (
                embeddings.space,
                COMPANION,
                _hash(_normalize("项目进展")),
                json.dumps(query_vector),
                "2099-01-01T00:00:00.000000Z",
                now,
            ),
        )


def measure(memory, embeddings, repeat):
    samples = []
    row_counts = []
    for _ in range(repeat):
        with memory._connection() as connection:
            started = time.perf_counter()
            rows, hit = embeddings.retrieve_cached(connection, COMPANION, "项目进展")
            samples.append((time.perf_counter() - started) * 1000)
            row_counts.append(len(rows))
    ordered = sorted(samples)
    midpoint = len(ordered) // 2
    return {
        "hit": hit,
        "rows": row_counts[0],
        "min_ms": min(samples),
        "p50_ms": ordered[midpoint],
        "max_ms": max(samples),
    }


def run(records, dimensions, repeat, legacy):
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryService(Path(directory) / "benchmark.sqlite")
        embeddings = EmbeddingMemoryService(
            memory,
            provider=None,
            model_name="benchmark",
            enabled=True,
            dimensions=dimensions,
            candidate_limit=records,
            similarity_threshold=-1.0,
        )
        build(memory, embeddings, records, dimensions, legacy)
        result = measure(memory, embeddings, repeat)
        label = "legacy_json" if legacy else "float32_blob"
        print(
            f"{label}: records={records} dims={dimensions} n={repeat} "
            f"min={result['min_ms']:.1f}ms p50={result['p50_ms']:.1f}ms "
            f"max={result['max_ms']:.1f}ms rows={result['rows']} hit={result['hit']}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=5000, choices=(500, 1000, 2000, 5000))
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--legacy", action="store_true", help="write legacy JSON vectors")
    args = parser.parse_args()
    run(args.records, args.dimensions, args.repeat, args.legacy)


if __name__ == "__main__":
    main()
