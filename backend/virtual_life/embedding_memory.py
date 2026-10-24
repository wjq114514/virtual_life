"""Durable, budgeted embeddings and bounded local semantic retrieval."""

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .memory import MemoryError, _normalize, _permanent_clause, _term_weight, _terms
from .model_provider import ProviderError, validate_embedding_vector
from .ports import EmbeddingRequest
from .vector_codec import (
    cosine_scores,
    cosine_scores_from_blobs,
    decode_vector,
    encode_vector,
    vector_dimensions,
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class EmbeddingMemoryService:
    def __init__(
        self,
        memory,
        *,
        provider=None,
        model_name="",
        enabled=False,
        batch_size=16,
        max_input_tokens=4000,
        poll_seconds=5,
        dimensions=None,
        space_identity="",
        candidate_limit=5000,
        similarity_threshold=0.25,
        query_cache_size=256,
        query_cache_ttl_seconds=300,
        local=False,
        rerank=None,
    ):
        if not 1 <= batch_size <= 64 or not 1 <= max_input_tokens <= 64000:
            raise ValueError("Invalid embedding batch limits")
        if not 1 <= candidate_limit <= 100000 or not 1 <= query_cache_size <= 10000:
            raise ValueError("Invalid embedding retrieval limits")
        if not math.isfinite(similarity_threshold) or not -1 <= similarity_threshold <= 1:
            raise ValueError("Invalid similarity threshold")
        if not 1 <= query_cache_ttl_seconds <= 86400:
            raise ValueError("Invalid embedding cache TTL")
        if dimensions is not None and (type(dimensions) is not int or not 1 <= dimensions <= 65536):
            raise ValueError("Invalid embedding dimensions")
        self.memory, self.provider, self.model_name = memory, provider, model_name
        self.enabled, self.batch_size, self.max_input_tokens = enabled, batch_size, max_input_tokens
        self.dimensions, self.candidate_limit = dimensions, candidate_limit
        self.similarity_threshold = similarity_threshold
        self.local = bool(local)
        self.rerank = rerank
        self.query_cache_size, self.query_cache_ttl_seconds = (
            query_cache_size,
            query_cache_ttl_seconds,
        )
        self.space = _hash(
            json.dumps(
                [
                    space_identity,
                    getattr(provider, "base_url", ""),
                    model_name,
                    dimensions,
                    getattr(provider, "extra_body", {}),
                ],
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        with memory._connection(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS embedding_spaces (space TEXT PRIMARY KEY, dimensions INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS embedding_deferred (space TEXT NOT NULL, companion_id TEXT NOT NULL, until_at TEXT NOT NULL, PRIMARY KEY(space,companion_id))"
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS embedding_vectors (
                space TEXT NOT NULL, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                content_hash TEXT NOT NULL, companion_id TEXT NOT NULL, vector_json TEXT NOT NULL,
                vector_blob BLOB,
                PRIMARY KEY(space,memory_id))""")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(embedding_vectors)")
            }
            if "vector_blob" not in columns:
                # Migrate the old JSON-only table in place.  The column stays
                # nullable so a database written by an older process is readable.
                connection.execute("ALTER TABLE embedding_vectors ADD COLUMN vector_blob BLOB")
            connection.execute("""CREATE TABLE IF NOT EXISTS embedding_jobs (
                job_id TEXT PRIMARY KEY, companion_id TEXT NOT NULL, space TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, versions_json TEXT NOT NULL, error_code TEXT,
                usage_json TEXT)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS embedding_attempts (
                space TEXT NOT NULL, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                content_hash TEXT NOT NULL, job_id TEXT NOT NULL,
                PRIMARY KEY(space,memory_id,revision,content_hash))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS embedding_query_cache (
                space TEXT NOT NULL, companion_id TEXT NOT NULL, query_hash TEXT NOT NULL,
                vector_json TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(space,companion_id,query_hash))""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS embedding_vector_scope ON embedding_vectors(space,companion_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS embedding_job_scope ON embedding_jobs(space,companion_id,created_at DESC,job_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS embedding_job_recovery ON embedding_jobs(status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS embedding_cache_expiry ON embedding_query_cache(expires_at)"
            )
            self._backfill_vector_blobs(connection)
        memory.embedding_memory = self

    def _backfill_vector_blobs(self, connection, limit=256):
        """Convert a bounded number of legacy JSON vectors to float32 BLOBs.

        Migration is intentionally incremental: startup and indexing stay
        responsive, while every read still supports the legacy representation.
        """
        rows = connection.execute(
            """SELECT space,memory_id,vector_json FROM embedding_vectors
            WHERE vector_blob IS NULL AND vector_json IS NOT NULL AND vector_json != ''
            LIMIT ?""",
            (limit,),
        ).fetchall()
        for row in rows:
            try:
                vector = validate_embedding_vector(json.loads(row["vector_json"]))
                blob = encode_vector(vector)
            except (TypeError, ValueError, OverflowError, RecursionError):
                continue
            connection.execute(
                """UPDATE embedding_vectors SET vector_blob=?, vector_json=''
                WHERE space=? AND memory_id=? AND vector_blob IS NULL""",
                (blob, row["space"], row["memory_id"]),
            )
        return len(rows)

    @contextmanager
    def _lock(self):
        try:
            import fcntl
        except ImportError:
            raise MemoryError(
                503, "embedding_lock_unavailable", "Embedding requires POSIX file locks"
            ) from None
        with open(
            str(Path(self.memory.database_path).resolve()) + ".embedding.lock", "a+b"
        ) as file:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MemoryError(409, "embedding_busy", "An embedding worker is active") from None
            try:
                with self.memory._connection(write=True) as connection:
                    # The same lock covers the entire dispatch. Pending receipts cannot
                    # have reached the provider; recover their holds atomically.
                    now = self.memory._timestamp(self.memory._now())
                    for interrupted in connection.execute(
                        "SELECT companion_id, job_id, kind, status FROM embedding_jobs WHERE status IN ('pending', 'dispatching')"
                    ).fetchall():
                        pending = interrupted["status"] == "pending"
                        self.memory.logs.record(
                            interrupted["companion_id"],
                            "embedding_index"
                            if interrupted["kind"] == "index"
                            else "embedding_query",
                            "failed" if pending else "unknown",
                            job_id=interrupted["job_id"],
                            details={
                                "error_code": "interrupted_before_dispatch"
                                if pending
                                else "worker_interrupted"
                            },
                            connection=connection,
                        )
                    connection.execute(
                        "UPDATE memory_token_tasks SET status='released',updated_at=?,released_at=? WHERE status='reserved' AND task_id IN (SELECT job_id FROM embedding_jobs WHERE status='pending')",
                        (now, now),
                    )
                    connection.execute(
                        "UPDATE embedding_jobs SET status='failed',error_code='interrupted_before_dispatch',updated_at=? WHERE status='pending'",
                        (now,),
                    )
                    connection.execute(
                        "UPDATE embedding_jobs SET status='unknown',error_code='worker_interrupted',updated_at=? WHERE status='dispatching'",
                        (now,),
                    )
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def _valid(self, connection, row):
        return bool(
            row
            and row["active"]
            and self.memory._time_valid(row, self.memory._now())
            and self.memory._source_supported(connection, row["companion_id"], row["memory_id"])
        )

    def _finish(self, job, status, error=None, usage=None, *, model_calls=0):
        with self.memory._connection(write=True) as connection:
            connection.execute(
                "UPDATE embedding_jobs SET status=?,error_code=?,usage_json=?,updated_at=? WHERE job_id=?",
                (
                    status,
                    error,
                    _json(usage) if usage else None,
                    self.memory._timestamp(self.memory._now()),
                    job,
                ),
            )
            row = connection.execute(
                "SELECT companion_id, kind FROM embedding_jobs WHERE job_id=?", (job,)
            ).fetchone()
            self.memory.logs.record(
                row["companion_id"],
                "embedding_index" if row["kind"] == "index" else "embedding_query",
                status,
                job_id=job,
                details={"usage": usage, "error_code": error, "model_calls": model_calls},
                connection=connection,
            )
        return {
            "job_id": job,
            "status": status,
            "error_code": error,
            "usage": usage,
            "model_calls": model_calls,
        }

    def _call(self, companion_id, texts, versions, kind, commit, task_id=None):
        job = "query_" + _hash(task_id) if task_id is not None else "emb_" + uuid.uuid4().hex
        estimated = sum(self.memory._estimate(text) for text in texts)
        if estimated > min(self.max_input_tokens, self.memory.policy.per_task_model_token_limit):
            raise MemoryError(422, "embedding_input_limit", "Embedding input exceeds token limit")
        now = self.memory._timestamp(self.memory._now())
        with self.memory._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO embedding_jobs VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                (job, companion_id, self.space, kind, "pending", now, now, _json(versions)),
            )
        reserved = False
        dispatched = False
        model_calls = 0
        usage = None
        tracked = not self.local
        try:
            if tracked:
                hold = self.memory.reserve_model_tokens(
                    companion_id,
                    {
                        "task_id": job,
                        "purpose": "memory_embedding" if kind == "index" else "query_embedding",
                        "input_tokens": estimated,
                        "output_tokens": 0,
                    },
                )
                if not hold["new_reservation"]:
                    return self._finish(job, "unknown", "reservation_exists")
                reserved = True
            with self.memory._connection(write=True) as connection:
                connection.execute(
                    "UPDATE embedding_jobs SET status='dispatching' WHERE job_id=?", (job,)
                )
                for version in versions:
                    connection.execute(
                        "INSERT INTO embedding_attempts VALUES(?,?,?,?,?)",
                        (
                            self.space,
                            version["memory_id"],
                            version["revision"],
                            version["hash"],
                            job,
                        ),
                    )
            if versions:
                with self.memory._connection() as connection:
                    for version in versions:
                        source = connection.execute(
                            "SELECT * FROM memory_records WHERE memory_id=? AND companion_id=?",
                            (version["memory_id"], companion_id),
                        ).fetchone()
                        if (
                            not self._valid(connection, source)
                            or source["revision"] != version["revision"]
                            or _hash(source["content"]) != version["hash"]
                        ):
                            if tracked:
                                self.memory.release_model_tokens(companion_id, job)
                            reserved = False
                            return self._finish(job, "discarded", "source_invalid")
            model_calls = 1
            dispatched = True
            answer = self.provider.embed(
                EmbeddingRequest(job, companion_id, self.model_name, texts, estimated)
            )
            counts = (answer.usage.input_tokens, answer.usage.output_tokens)
            if (
                any(type(count) is not int or not 0 <= count <= 1000000000 for count in counts)
                or counts[1] != 0
            ):
                raise ProviderError("embedding_usage_missing", "Invalid embedding usage")
            usage = {"input_tokens": counts[0], "output_tokens": counts[1]}
            with self.memory._connection(write=True) as connection:
                connection.execute(
                    "UPDATE embedding_jobs SET usage_json=? WHERE job_id=?", (_json(usage), job)
                )
            if tracked:
                self.memory.settle_model_tokens(companion_id, job, usage)
            reserved = False
            if answer.model != self.model_name or len(answer.vectors) != len(texts):
                raise ValueError("Invalid embedding result")
            vectors = [
                validate_embedding_vector(vector, self.dimensions) for vector in answer.vectors
            ]
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("Inconsistent embedding dimensions")
            with self.memory._connection(write=True) as connection:
                known = connection.execute(
                    "SELECT dimensions FROM embedding_spaces WHERE space=?", (self.space,)
                ).fetchone()
                if known and known[0] != len(vectors[0]):
                    raise ValueError(
                        "Embedding space dimensions changed; configure a new space identity"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO embedding_spaces VALUES(?,?)",
                    (self.space, len(vectors[0])),
                )
                commit(connection, vectors)
                connection.execute(
                    "UPDATE embedding_jobs SET status='completed',error_code=NULL,usage_json=?,updated_at=? WHERE job_id=?",
                    (_json(usage), self.memory._timestamp(self.memory._now()), job),
                )
                self.memory.logs.record(
                    companion_id,
                    "embedding_index" if kind == "index" else "embedding_query",
                    job_id=job,
                    details={
                        "usage": usage,
                        "source_count": len(versions),
                        "memory_versions": versions,
                        "model_calls": 1,
                    },
                    connection=connection,
                )
            return {
                "job_id": job,
                "status": "completed",
                "error_code": None,
                "usage": usage,
                "model_calls": 1,
            }
        except ProviderError as error:
            try:
                if error.usage is not None:
                    counts = (error.usage.input_tokens, error.usage.output_tokens)
                    if all(type(count) is int and 0 <= count <= 1000000000 for count in counts):
                        usage = {"input_tokens": counts[0], "output_tokens": counts[1]}
                        if tracked:
                            self.memory.settle_model_tokens(companion_id, job, usage)
                        reserved = False
                elif error.releasable:
                    model_calls = 0
                    if tracked:
                        self.memory.release_model_tokens(companion_id, job)
                    reserved = False
                    with self.memory._connection(write=True) as connection:
                        connection.execute("DELETE FROM embedding_attempts WHERE job_id=?", (job,))
            except Exception:
                return self._finish(
                    job, "unknown", "embedding_usage_unsettled", usage, model_calls=model_calls
                )
            in_doubt = reserved or (not tracked and dispatched and usage is None)
            return self._finish(
                job,
                "unknown" if in_doubt else "failed",
                error.code,
                usage,
                model_calls=model_calls,
            )
        except Exception as error:
            if (
                tracked
                and isinstance(error, MemoryError)
                and error.code == "model_token_budget_exceeded"
                and not reserved
            ):
                until = self.memory._timestamp(
                    self.memory._now().replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1)
                )
                with self.memory._connection(write=True) as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO embedding_deferred VALUES(?,?,?)",
                        (self.space, companion_id, until),
                    )
                return self._finish(job, "deferred", error.code, model_calls=0)
            in_doubt = reserved or (not tracked and dispatched and usage is None)
            return self._finish(
                job,
                "unknown" if in_doubt else "failed",
                error.code if isinstance(error, MemoryError) else "embedding_invalid_result",
                usage,
                model_calls=model_calls,
            )

    def index_pending(self):
        return self.run()

    def run(self, companion_id=None, limit=None):
        if not self.enabled or self.provider is None or not self.model_name:
            return {"status": "disabled", "jobs": []}
        if companion_id is not None:
            companion_id = self.memory._identifier(companion_id, "companion_id")
        limit = self.batch_size if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= 64:
            raise MemoryError(422, "embedding_limit", "Invalid embedding batch limit")
        with self._lock():
            with self.memory._connection(write=True) as connection:
                self._backfill_vector_blobs(connection, 512)
            with self.memory._connection() as connection:
                scope = " AND m.companion_id=?" if companion_id else ""
                rows = connection.execute(
                    f"SELECT m.* FROM memory_records m WHERE active=1 {scope} ORDER BY updated_at,memory_id",
                    (companion_id,) if companion_id else (),
                )
                selected, tokens, owner = [], 0, companion_id
                for row in rows:
                    if (
                        not self.local
                        and connection.execute(
                            "SELECT 1 FROM embedding_deferred WHERE space=? AND companion_id=? AND until_at>?",
                            (
                                self.space,
                                row["companion_id"],
                                self.memory._timestamp(self.memory._now()),
                            ),
                        ).fetchone()
                    ):
                        continue
                    if owner and row["companion_id"] != owner or not self._valid(connection, row):
                        continue
                    digest = _hash(row["content"])
                    if connection.execute(
                        "SELECT 1 FROM embedding_attempts WHERE space=? AND memory_id=? AND revision=? AND content_hash=?",
                        (self.space, row["memory_id"], row["revision"], digest),
                    ).fetchone():
                        continue
                    estimate = self.memory._estimate(row["content"])
                    if tokens + estimate > min(
                        self.max_input_tokens, self.memory.policy.per_task_model_token_limit
                    ):
                        continue
                    selected.append(dict(row))
                    tokens += estimate
                    owner = row["companion_id"]
                    if len(selected) >= min(limit, self.batch_size):
                        break
            if not selected:
                return {"status": "idle", "jobs": []}
            versions = [
                {
                    "memory_id": row["memory_id"],
                    "revision": row["revision"],
                    "hash": _hash(row["content"]),
                }
                for row in selected
            ]

            def commit(connection, vectors):
                for version, vector in zip(versions, vectors, strict=True):
                    current = connection.execute(
                        "SELECT * FROM memory_records WHERE memory_id=? AND companion_id=?",
                        (version["memory_id"], owner),
                    ).fetchone()
                    if (
                        self._valid(connection, current)
                        and current["revision"] == version["revision"]
                        and _hash(current["content"]) == version["hash"]
                    ):
                        connection.execute(
                            """INSERT OR REPLACE INTO embedding_vectors
                            (space,memory_id,revision,content_hash,companion_id,vector_json,vector_blob)
                            VALUES(?,?,?,?,?,?,?)""",
                            (
                                self.space,
                                version["memory_id"],
                                version["revision"],
                                version["hash"],
                                owner,
                                "",
                                encode_vector(vector),
                            ),
                        )

            result = self._call(
                owner, [row["content"] for row in selected], versions, "index", commit
            )
            return {"status": result["status"], "jobs": [result]}

    def _cached(self, connection, companion_id, query):
        if not self.enabled:
            return None
        try:
            row = connection.execute(
                "SELECT vector_json FROM embedding_query_cache WHERE space=? AND companion_id=? AND query_hash=? AND expires_at>?",
                (
                    self.space,
                    companion_id,
                    _hash(_normalize(query)),
                    self.memory._timestamp(self.memory._now()),
                ),
            ).fetchone()
            known = connection.execute(
                "SELECT dimensions FROM embedding_spaces WHERE space=?", (self.space,)
            ).fetchone()
            if row is None or known is None:
                return None
            return validate_embedding_vector(json.loads(row[0]), known[0])
        except (sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
            return None

    def prepare_query(self, companion_id, query, *, task_id=None):
        try:
            result = self._prepare_query(companion_id, query, task_id=task_id)
            if not result.get("job_id"):
                self.memory.logs.best_effort(
                    companion_id,
                    "embedding_query",
                    "cached" if result.get("cache_hit") else "skipped",
                    details={
                        "model_calls": result["model_calls"],
                        "semantic_cache_hit": result.get("cache_hit", False),
                    },
                )
            return result
        except MemoryError as error:
            if not hasattr(error, "memory_retrieval"):
                error.memory_retrieval = {
                    "model_calls": 0,
                    "cache_hit": False,
                    "usage": None,
                    "degraded": True,
                    "error_code": error.code,
                }
            raise

    def _prepare_query(self, companion_id, query, *, task_id=None):
        companion_id = self.memory._identifier(companion_id, "companion_id")
        if not isinstance(query, str) or len(query) > 4096:
            raise MemoryError(
                422, "embedding_query_invalid", "Query must be at most 4096 characters"
            )
        query = query.strip()
        if not self.enabled or not self.provider or not self.model_name or not query.strip():
            return {"model_calls": 0, "cache_hit": False, "usage": None}
        with self.memory._connection() as connection:
            if self._cached(connection, companion_id, query) is not None:
                return {"model_calls": 0, "cache_hit": True, "usage": None}
        with self._lock():
            with self.memory._connection() as connection:
                cached = self._cached(connection, companion_id, query)
                if cached is not None:
                    return {"model_calls": 0, "cache_hit": True, "usage": None}
                if task_id is not None:
                    existing = connection.execute(
                        "SELECT * FROM embedding_jobs WHERE job_id=?", ("query_" + _hash(task_id),)
                    ).fetchone()
                    if existing:
                        error = MemoryError(
                            409,
                            "embedding_query_already_attempted",
                            "Query embedding task already has a receipt",
                        )
                        usage = (
                            json.loads(existing["usage_json"]) if existing["usage_json"] else None
                        )
                        error.memory_retrieval = {
                            "model_calls": int(
                                existing["status"] in ("unknown", "completed") or usage is not None
                            ),
                            "cache_hit": False,
                            "usage": usage,
                            "degraded": True,
                            "error_code": error.code,
                        }
                        raise error

            def commit(connection, vectors):
                now = self.memory._now()
                connection.execute(
                    "INSERT OR REPLACE INTO embedding_query_cache VALUES(?,?,?,?,?,?)",
                    (
                        self.space,
                        companion_id,
                        _hash(_normalize(query)),
                        _json(vectors[0]),
                        self.memory._timestamp(
                            now + timedelta(seconds=self.query_cache_ttl_seconds)
                        ),
                        self.memory._timestamp(now),
                    ),
                )
                connection.execute(
                    "DELETE FROM embedding_query_cache WHERE rowid IN (SELECT rowid FROM embedding_query_cache ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
                    (self.query_cache_size,),
                )

            result = self._call(companion_id, [query], [], "query", commit, task_id=task_id)
            if result["status"] != "completed":
                error = MemoryError(
                    503,
                    result["error_code"] or "embedding_failed",
                    "Query embedding failed; consult embedding job receipt",
                )
                error.memory_retrieval = {
                    "model_calls": result["model_calls"],
                    "cache_hit": False,
                    "usage": result["usage"],
                    "degraded": True,
                    "error_code": error.code,
                }
                raise error
            return {"model_calls": 1, "cache_hit": False, "usage": result["usage"]}

    def semantic_recall(self, companion_id, payload):
        # Validate every recall policy limit before any paid work.
        initial = self.memory.recall(companion_id, payload)
        if payload.get("semantic_key") is not None:
            # Structured fact lookup already identifies its slot locally.
            return initial
        result = self.prepare_query(companion_id, payload.get("query", ""))
        recalled = self.memory.recall(companion_id, payload, allow_rerank=True)
        recalled["model_calls"] = result["model_calls"]
        recalled["embedding_usage"] = result["usage"]
        return recalled

    def rerank_rows(self, companion_id, query, rows):
        """Rerank a fused candidate list only when explicitly allowed by the caller."""
        if self.rerank is None:
            return rows, None
        return self.rerank.rerank(companion_id, query, rows)

    def retrieve_cached(self, connection, companion_id, query, layer=None):
        try:
            return self._retrieve_cached(connection, companion_id, query, layer)
        except (sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
            return [], False

    def _retrieve_cached(self, connection, companion_id, query, layer=None):
        vector = self._cached(connection, companion_id, query)
        if vector is None:
            return [], False
        timestamp = self.memory._timestamp(self.memory._now())
        layer_clause = (
            ""
            if layer is None
            else (" AND m.kind='episode'" if layer == "episodic" else " AND m.kind!='episode'")
        )
        rows = connection.execute(
            f"""SELECT m.*,v.vector_blob,v.vector_json,v.content_hash,
            a.last_reinforced_at AS last_reinforced_at
            FROM embedding_vectors v
            JOIN memory_records m ON m.memory_id=v.memory_id AND m.companion_id=v.companion_id
            LEFT JOIN memory_access_state a ON a.memory_id=m.memory_id
            WHERE v.space=? AND m.companion_id=? AND m.active=1 AND m.revision=v.revision
            AND (m.kind='episode' OR ((m.valid_from IS NULL OR m.valid_from<=?) AND (m.valid_to IS NULL OR m.valid_to>?)))
            {layer_clause} AND {self.memory._supported_clause()}
            ORDER BY m.updated_at DESC,m.memory_id LIMIT ?""",
            (self.space, companion_id, timestamp, timestamp, self.candidate_limit),
        ).fetchall()
        if self.memory.policy.decay.enabled:
            terms = sorted(_terms(query), key=lambda term: (-_term_weight(term), term))[:256]
            match_order = "0.0"
            parameters = [self.space, companion_id, timestamp, timestamp]
            if terms:
                placeholders = ",".join("?" for _ in terms)
                match_order = (
                    "(SELECT COUNT(*) FROM memory_terms pt WHERE pt.memory_id=m.memory_id "
                    f"AND pt.term IN ({placeholders}))"
                )
                parameters.extend(terms)
            parameters.append(self.memory.policy.decay.permanent_candidate_limit)
            protected = connection.execute(
                f"""SELECT m.*,v.vector_blob,v.vector_json,v.content_hash,
                a.last_reinforced_at AS last_reinforced_at
                FROM embedding_vectors v
                JOIN memory_records m ON m.memory_id=v.memory_id AND m.companion_id=v.companion_id
                LEFT JOIN memory_access_state a ON a.memory_id=m.memory_id
                WHERE v.space=? AND m.companion_id=? AND m.active=1 AND m.revision=v.revision
                AND (m.kind='episode' OR ((m.valid_from IS NULL OR m.valid_from<=?) AND (m.valid_to IS NULL OR m.valid_to>?)))
                {layer_clause} AND {self.memory._supported_clause()} AND {_permanent_clause()}
                ORDER BY {match_order} DESC,m.importance DESC,m.memory_id LIMIT ?""",
                parameters,
            ).fetchall()
            candidates = {row["memory_id"]: row for row in rows}
            candidates.update((row["memory_id"], row) for row in protected)
            rows = list(candidates.values())
        now = self.memory._now()
        decoded = []
        blobs = []
        all_blobs = True
        for row in rows:
            if _hash(row["content"]) != row["content_hash"]:
                continue
            try:
                if row["vector_blob"] is not None:
                    blob = row["vector_blob"]
                    if vector_dimensions(blob) != len(vector):
                        continue
                    decoded.append((row, blob, None))
                    blobs.append(blob)
                elif row["vector_json"]:
                    # Legacy rows remain readable until the bounded backfill
                    # converts them to float32 BLOBs.
                    stored = validate_embedding_vector(json.loads(row["vector_json"]), len(vector))
                    decoded.append((row, None, stored))
                    all_blobs = False
                else:
                    continue
            except (ValueError, TypeError, OverflowError, RecursionError):
                continue
        if decoded and all_blobs:
            cosines = cosine_scores_from_blobs(vector, blobs, len(vector))
        else:
            cosines = cosine_scores(
                vector,
                [
                    stored if stored is not None else decode_vector(blob, len(vector))
                    for _, blob, stored in decoded
                ],
            )
        scored = []
        for (row, _, _), cosine in zip(decoded, cosines, strict=True):
            if not math.isfinite(cosine) or cosine < self.similarity_threshold:
                continue
            salience = (
                self.memory.policy.decay.semantic_salience_weight
                * row["importance"]
                * self.memory._decay_factor(row, now, row["last_reinforced_at"])
                if self.memory.policy.decay.enabled
                else 0
            )
            scored.append((cosine + salience, row))
        scored.sort(key=lambda value: (-value[0], value[1]["memory_id"]))
        return [row for _, row in scored], True

    def status(self, companion_id):
        companion_id = self.memory._identifier(companion_id, "companion_id")
        with self.memory._connection() as connection:
            counts = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT status,COUNT(*) FROM embedding_jobs WHERE companion_id=? AND space=? GROUP BY status",
                    (companion_id, self.space),
                )
            }
            vectors = connection.execute(
                "SELECT COUNT(*) FROM embedding_vectors WHERE companion_id=? AND space=?",
                (companion_id, self.space),
            ).fetchone()[0]
            indexed, pending, oversized = 0, 0, 0
            for row in connection.execute(
                "SELECT * FROM memory_records WHERE companion_id=? AND active=1", (companion_id,)
            ):
                if not self._valid(connection, row):
                    continue
                digest = _hash(row["content"])
                vector = connection.execute(
                    "SELECT 1 FROM embedding_vectors WHERE space=? AND memory_id=? AND revision=? AND content_hash=?",
                    (self.space, row["memory_id"], row["revision"], digest),
                ).fetchone()
                if vector:
                    indexed += 1
                elif self.memory._estimate(row["content"]) > min(
                    self.max_input_tokens, self.memory.policy.per_task_model_token_limit
                ):
                    oversized += 1
                elif not connection.execute(
                    "SELECT 1 FROM embedding_attempts WHERE space=? AND memory_id=? AND revision=? AND content_hash=?",
                    (self.space, row["memory_id"], row["revision"], digest),
                ).fetchone():
                    pending += 1
        return {
            "stored_vectors": vectors,
            "pending_records": pending,
            "skipped_oversized_records": oversized,
            "enabled": self.enabled,
            "configured": bool(self.provider and self.model_name),
            "local": self.local,
            "vector_space": self.space,
            "indexed_records": indexed,
            "jobs": counts,
            "token_quota": self.memory.budget_snapshot(companion_id),
            "rerank": self.rerank.status() if self.rerank is not None else None,
        }

    def jobs(self, companion_id, limit=50, offset=0):
        companion_id = self.memory._identifier(companion_id, "companion_id")
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise MemoryError(422, "embedding_pagination", "Invalid job pagination")
        with self.memory._connection() as connection:
            rows = connection.execute(
                "SELECT job_id,kind,status,created_at,updated_at,error_code,usage_json FROM embedding_jobs WHERE companion_id=? AND space=? ORDER BY created_at DESC,job_id LIMIT ? OFFSET ?",
                (companion_id, self.space, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    **{key: row[key] for key in row.keys() if key != "usage_json"},
                    "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
                }
                for row in rows
            ],
            "limit": limit,
            "offset": offset,
        }
