"""Canonical graph identities and explicit, versioned entity aliases."""

import hashlib
import json
import unicodedata
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .memory import MemoryError


def normalize_entity(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def entity_id(name):
    return "ent_" + hashlib.sha256(normalize_entity(name).encode()).hexdigest()[:24]


def edge_id(subject, predicate, object_):
    key = json.dumps([subject, normalize_entity(predicate), object_], ensure_ascii=False)
    return "rel_" + hashlib.sha256(key.encode()).hexdigest()[:24]


GraphText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class GraphAliasUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: GraphText
    expected_revision: int = Field(ge=1, le=2**63 - 2)
    aliases: list[GraphText] = Field(max_length=64)
    source_event_ids: list[GraphText] = Field(default_factory=list, max_length=64)


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS graph_entities (
        companion_id TEXT NOT NULL, entity_id TEXT NOT NULL, name TEXT NOT NULL,
        normalized_name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]',
        revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT,
        source_events_json TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(companion_id,entity_id), UNIQUE(companion_id,normalized_name))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS graph_aliases (
        companion_id TEXT NOT NULL, normalized_alias TEXT NOT NULL, entity_id TEXT NOT NULL,
        PRIMARY KEY(companion_id,normalized_alias))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS graph_alias_receipts (
        companion_id TEXT NOT NULL, command_id TEXT NOT NULL, request_json TEXT NOT NULL,
        response_json TEXT NOT NULL, PRIMARY KEY(companion_id,command_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS graph_schema_versions (
        version INTEGER PRIMARY KEY)""")
    if connection.execute("SELECT 1 FROM graph_schema_versions WHERE version=1").fetchone():
        return
    for row in connection.execute("SELECT * FROM auto_memory_entities").fetchall():
        ensure_entity(connection, row["companion_id"], row["name"])
    for row in connection.execute(
        "SELECT companion_id,data_json FROM auto_memory_derivatives WHERE kind='relation'"
    ).fetchall():
        data = json.loads(row["data_json"])
        for key in ("subject", "object"):
            ensure_entity(connection, row["companion_id"], data[key])
    connection.execute("INSERT INTO graph_schema_versions(version) VALUES(1)")


def ensure_entity(connection, companion_id, name):
    identity = entity_id(name)
    owner = connection.execute(
        "SELECT entity_id FROM graph_aliases WHERE companion_id=? AND normalized_alias=?",
        (companion_id, normalize_entity(name)),
    ).fetchone()
    if owner:
        return owner["entity_id"]
    connection.execute(
        """INSERT OR IGNORE INTO graph_entities
        (companion_id,entity_id,name,normalized_name) VALUES(?,?,?,?)""",
        (
            companion_id,
            identity,
            " ".join(unicodedata.normalize("NFKC", name).split()),
            normalize_entity(name),
        ),
    )
    return identity


def node(row):
    return {
        "entity_id": row["entity_id"],
        "name": row["name"],
        "aliases": json.loads(row["aliases_json"]),
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "source_event_ids": json.loads(row["source_events_json"]),
    }


def update_aliases(service, companion_id, identity, payload):
    memory = service.memory
    companion_id = memory._identifier(companion_id, "companion_id")
    identity = memory._identifier(identity, "entity_id")
    payload = memory._validate(GraphAliasUpdate, payload)
    if (
        not payload.command_id.strip()
        or any(not normalize_entity(value) or len(value) > 128 for value in payload.aliases)
        or any(not value.strip() or len(value) > 128 for value in payload.source_event_ids)
    ):
        raise MemoryError(422, "validation_error", "Invalid alias or source event identifier")
    request = json.dumps({"entity_id": identity, **payload.model_dump()}, sort_keys=True)
    with memory._connection(write=True) as connection:
        receipt = connection.execute(
            "SELECT * FROM graph_alias_receipts WHERE companion_id=? AND command_id=?",
            (companion_id, payload.command_id),
        ).fetchone()
        if receipt:
            if receipt["request_json"] != request:
                raise MemoryError(409, "idempotency_conflict", "command_id already used")
            return json.loads(receipt["response_json"])
        row = connection.execute(
            "SELECT * FROM graph_entities WHERE companion_id=? AND entity_id=?",
            (companion_id, identity),
        ).fetchone()
        if row is None:
            raise MemoryError(404, "entity_not_found", "Entity does not exist")
        if row["revision"] != payload.expected_revision:
            raise MemoryError(409, "entity_revision_conflict", "Entity revision changed")
        aliases = {}
        for value in payload.aliases:
            normalized = normalize_entity(value)
            owner = connection.execute(
                """SELECT entity_id FROM graph_entities WHERE companion_id=? AND normalized_name=?
                UNION SELECT entity_id FROM graph_aliases
                WHERE companion_id=? AND normalized_alias=?""",
                (companion_id, normalized, companion_id, normalized),
            ).fetchall()
            if any(owner_row["entity_id"] != identity for owner_row in owner):
                raise MemoryError(409, "entity_alias_conflict", "Alias belongs to another entity")
            if normalized != row["normalized_name"]:
                aliases[normalized] = " ".join(unicodedata.normalize("NFKC", value).split())
        for source in payload.source_event_ids:
            if (
                connection.execute(
                    "SELECT 1 FROM events WHERE companion_id=? AND event_id=?",
                    (companion_id, source),
                ).fetchone()
                is None
            ):
                raise MemoryError(422, "invalid_source_event", "Source event does not exist")
        removed = {normalize_entity(value) for value in json.loads(row["aliases_json"])} - set(
            aliases
        )
        connection.execute(
            "DELETE FROM graph_aliases WHERE companion_id=? AND entity_id=?",
            (companion_id, identity),
        )
        connection.executemany(
            "INSERT INTO graph_aliases VALUES(?,?,?)",
            [(companion_id, alias, identity) for alias in aliases],
        )
        if removed:
            # Withdrawal immediately restores independently named endpoints already
            # present in evidence; it creates metadata, never additional relation rows.
            connection.create_function("graph_normalize", 1, normalize_entity)
            used = connection.execute(
                """WITH spellings AS (
                    SELECT json_extract(data_json,'$.subject') AS name
                    FROM auto_memory_derivatives WHERE companion_id=? AND kind='relation'
                    UNION SELECT json_extract(data_json,'$.object') AS name
                    FROM auto_memory_derivatives WHERE companion_id=? AND kind='relation')
                    SELECT name FROM spellings WHERE graph_normalize(name)
                    IN (SELECT value FROM json_each(?))""",
                (companion_id, companion_id, json.dumps(sorted(removed))),
            ).fetchall()
            for spelling in used:
                ensure_entity(connection, companion_id, spelling["name"])
        connection.execute(
            """UPDATE graph_entities SET aliases_json=?,revision=revision+1,updated_at=?,
            source_events_json=? WHERE companion_id=? AND entity_id=?""",
            (
                json.dumps(list(aliases.values()), ensure_ascii=False),
                memory._timestamp(memory._now()),
                json.dumps(sorted(set(payload.source_event_ids))),
                companion_id,
                identity,
            ),
        )
        result = node(
            connection.execute(
                "SELECT * FROM graph_entities WHERE companion_id=? AND entity_id=?",
                (companion_id, identity),
            ).fetchone()
        )
        connection.execute(
            "INSERT INTO graph_alias_receipts VALUES(?,?,?,?)",
            (companion_id, payload.command_id, request, json.dumps(result, ensure_ascii=False)),
        )
        memory.logs.record(
            companion_id,
            "graph",
            command_id=payload.command_id,
            details={
                "action": "update_aliases",
                "entity_id": identity,
                "revision": result["revision"],
                "alias_count": len(result["aliases"]),
            },
            connection=connection,
        )
        return result


def build(
    service, connection, companion_id, query, limit, identity, hops, *, support_memory_ids=None
):
    normalized = normalize_entity(query)
    support_memory_ids = list(dict.fromkeys(support_memory_ids or []))[:8]
    # SQL functions resolve only user-declared aliases, never inferred identities.
    aliases = {
        row["normalized_alias"]: row["entity_id"]
        for row in connection.execute(
            "SELECT * FROM graph_aliases WHERE companion_id=?", (companion_id,)
        )
    }

    def resolve(name):
        return aliases.get(normalize_entity(name), entity_id(name))

    def endpoints(raw):
        data = json.loads(raw)
        return resolve(data["subject"]), resolve(data["object"])

    connection.create_function("graph_subject", 1, lambda raw: endpoints(raw)[0])
    connection.create_function("graph_object", 1, lambda raw: endpoints(raw)[1])
    connection.create_function(
        "graph_key",
        1,
        lambda raw: edge_id(endpoints(raw)[0], json.loads(raw)["predicate"], endpoints(raw)[1]),
    )
    connection.create_function(
        "graph_match", 1, lambda raw: int(service._score(json.loads(raw), query) > 0)
    )
    seeds = set()
    if identity is not None:
        row = connection.execute(
            "SELECT entity_id FROM graph_entities WHERE companion_id=? AND entity_id=?",
            (companion_id, identity),
        ).fetchone()
        if row is None:
            old = connection.execute(
                "SELECT name FROM auto_memory_entities WHERE companion_id=? AND entity_id=?",
                (companion_id, identity),
            ).fetchone()
            identity = resolve(old["name"]) if old else identity
            row = connection.execute(
                "SELECT entity_id FROM graph_entities WHERE companion_id=? AND entity_id=?",
                (companion_id, identity),
            ).fetchone()
        if row is None:
            raise MemoryError(404, "entity_not_found", "Entity does not exist")
        seeds.add(identity)
    if normalized:
        row = connection.execute(
            "SELECT entity_id FROM graph_entities WHERE companion_id=? AND normalized_name=?",
            (companion_id, normalized),
        ).fetchone()
        if row:
            seeds.add(row["entity_id"])
        elif normalized in aliases:
            seeds.add(aliases[normalized])
    live = """SELECT d.*, graph_key(d.data_json) AS edge_key,
        graph_subject(d.data_json) AS subject_id, graph_object(d.data_json) AS object_id
        FROM auto_memory_derivatives d WHERE d.companion_id=? AND d.kind='relation'
        AND NOT EXISTS (SELECT 1 FROM json_each(d.versions_json) v
        LEFT JOIN memory_records m ON m.memory_id=json_extract(v.value,'$.memory_id')
        AND m.companion_id=d.companion_id WHERE m.memory_id IS NULL OR m.active!=1
        OR m.revision!=json_extract(v.value,'$.revision') OR NOT ({supported})
        OR (m.kind!='episode' AND ((m.valid_from IS NOT NULL AND m.valid_from>?)
        OR (m.valid_to IS NOT NULL AND m.valid_to<=?))))
        AND NOT EXISTS (SELECT 1 FROM json_each(d.source_events_json) e
        WHERE NOT EXISTS (SELECT 1 FROM events original WHERE original.event_id=e.value
        AND original.companion_id=d.companion_id) OR EXISTS
        (SELECT 1 FROM memory_source_tombstones t WHERE t.companion_id=d.companion_id
        AND t.source_event_id=e.value))""".format(supported=service.memory._supported_clause("m"))
    now = service.memory._timestamp(service.memory._now())
    edges = {}
    frontier = set(seeds)
    visited = set(seeds)
    for hop in range(hops):
        remaining = service.graph_candidate_limit - len(edges)
        if remaining <= 0:
            break
        params = [companion_id, now, now]
        conditions = []
        if frontier:
            conditions.append(
                "(subject_id IN (SELECT value FROM json_each(?)) "
                "OR object_id IN (SELECT value FROM json_each(?)))"
            )
            params.extend([json.dumps(sorted(frontier))] * 2)
        elif hop == 0 and normalized and identity is None:
            conditions.append("graph_match(data_json)=1")
        elif hop > 0 or normalized or identity is not None:
            if not (hop == 0 and support_memory_ids):
                break
        if hop == 0 and support_memory_ids:
            support_condition = (
                "EXISTS (SELECT 1 FROM json_each(versions_json) graph_version "
                "WHERE json_extract(graph_version.value,'$.memory_id') "
                "IN (SELECT value FROM json_each(?)))"
            )
            if conditions:
                conditions = ["(" + " AND ".join(conditions) + " OR " + support_condition + ")"]
            else:
                conditions = [support_condition]
            params.append(json.dumps(support_memory_ids))
        if edges:
            conditions.append("edge_key NOT IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(list(edges)))
        filtered = " WHERE " + " AND ".join(conditions) if conditions else ""
        # Distinct edge selection precedes the per-edge evidence bound. Dead sources
        # are filtered first, and unrelated newer edges cannot consume search slots.
        sql = "WITH live AS (" + live + "), candidates AS (SELECT edge_key FROM live" + filtered
        sql += " GROUP BY edge_key ORDER BY MAX(created_at) DESC,edge_key LIMIT ?), "
        sql += """evidence AS (SELECT live.*,
            ROW_NUMBER() OVER (PARTITION BY edge_key,versions_json,source_events_json
            ORDER BY created_at DESC,derivative_id) AS duplicate_rank
            FROM live JOIN candidates USING(edge_key)), distinct_evidence AS (
            SELECT * FROM evidence WHERE duplicate_rank=1), ranked AS (
            SELECT *, COUNT(*) OVER (PARTITION BY edge_key) AS evidence_count,
            ROW_NUMBER() OVER (PARTITION BY edge_key ORDER BY created_at DESC,derivative_id)
            AS evidence_rank FROM distinct_evidence)
            SELECT * FROM ranked WHERE evidence_rank<=64 ORDER BY created_at DESC,derivative_id"""
        rows = connection.execute(sql, [*params, remaining]).fetchall()
        following = set()
        for row in rows:
            data = json.loads(row["data_json"])
            key = row["edge_key"]
            if key not in edges:
                edges[key] = {
                    **data,
                    "relation_id": key,
                    "edge_id": key,
                    "subject_entity_id": row["subject_id"],
                    "object_entity_id": row["object_id"],
                    "predicate": normalize_entity(data["predicate"]),
                    "kind": "relation",
                    "created_at": row["created_at"],
                    "job_id": row["job_id"],
                    "sources": [],
                    "relation_ids": [],
                    "memory_versions": [],
                    "source_event_ids": [],
                    "evidence_count": row["evidence_count"],
                    "evidence_truncated": row["evidence_count"] > 64,
                }
            edge = edges[key]
            versions = json.loads(row["versions_json"])
            events = json.loads(row["source_events_json"])
            # Bound every reference array, including malformed oversized legacy data.
            edge["sources"].append(
                {
                    "relation_id": row["derivative_id"],
                    "memory_versions": versions[:64],
                    "source_event_ids": events[:64],
                    "created_at": row["created_at"],
                    "job_id": row["job_id"],
                }
            )
            edge["relation_ids"].append(row["derivative_id"])
            for version in versions:
                if version not in edge["memory_versions"]:
                    if len(edge["memory_versions"]) < 64:
                        edge["memory_versions"].append(version)
                    else:
                        edge["evidence_truncated"] = True
            for event in events:
                if event not in edge["source_event_ids"]:
                    if len(edge["source_event_ids"]) < 64:
                        edge["source_event_ids"].append(event)
                    else:
                        edge["evidence_truncated"] = True
            if len(versions) > 64 or len(events) > 64:
                edge["evidence_truncated"] = True
            following.update((row["subject_id"], row["object_id"]))
        frontier = following - visited
        visited.update(following)
        if not frontier:
            break
    selected = list(edges.values())[:limit]
    included = {
        endpoint
        for edge in selected
        for endpoint in (edge["subject_entity_id"], edge["object_entity_id"])
    }
    if identity is not None:
        included.add(identity)
    nodes = {}
    for key in sorted(included):
        row = connection.execute(
            "SELECT * FROM graph_entities WHERE companion_id=? AND entity_id=?",
            (companion_id, key),
        ).fetchone()
        if row:
            nodes[key] = node(row)
    # A withdrawn alias may leave a raw legacy spelling without an independently
    # established canonical node. Such a row is not usable evidence until resolved.
    selected = [
        edge
        for edge in selected
        if all(
            endpoint in nodes for endpoint in (edge["subject_entity_id"], edge["object_entity_id"])
        )
    ]
    for edge in selected:
        edge["entity_versions"] = [
            {"entity_id": key, "revision": nodes[key]["revision"]}
            for key in sorted({edge["subject_entity_id"], edge["object_entity_id"]})
        ]
        edge["subject"] = nodes.get(edge["subject_entity_id"], {}).get("name", edge["subject"])
        edge["object"] = nodes.get(edge["object_entity_id"], {}).get("name", edge["object"])
    live_node_ids = {
        endpoint
        for edge in selected
        for endpoint in (edge["subject_entity_id"], edge["object_entity_id"])
    }
    if identity is not None:
        live_node_ids.add(identity)
    return {
        "companion_id": companion_id,
        "entities": [value for key, value in nodes.items() if key in live_node_ids],
        "relations": selected,
        "limit": limit,
        "hops": hops,
    }
