"""Isolated live evaluation; queue responses must come from fresh Luna medium agents.

Run with uv run python examples/memory_live_test.py. An external orchestrator must
launch a fresh Luna medium agent for each printed request and write its response.
This script alone does not launch agents. No production DB is opened.
Model usage is UNKNOWN: zero adapter placeholders are not measured token usage.
"""

import argparse
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from virtual_life.api import create_app
from virtual_life.ports import ModelRequest, ModelResult, ModelUsage
from virtual_life.settings import Settings

RUN = Path(".data/memory-tests") / (
    datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class FileModelProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        stem = RUN / f"request-{self.calls:02}"
        save(stem.with_suffix(".json"), asdict(request))
        print(f"WAITING {stem.name}", flush=True)
        response = RUN / f"response-{self.calls:02}.json"
        deadline = time.monotonic() + 600
        while not response.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Luna response deadline expired")
            time.sleep(0.2)
        raw = json.loads(response.read_text())
        text = raw["text"] if "text" in raw else json.dumps(raw, ensure_ascii=False)
        return ModelResult(
            text,
            "gpt-6-luna",
            ModelUsage(0, 0),
            {
                "usage_status": "unknown_subagent_usage",
                "adapter_usage": "synthetic_zero_placeholder",
                "reasoning_effort": "medium",
            },
        )


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    if any(RUN.iterdir()):
        raise SystemExit("Refusing existing nonempty run directory; choose a new --run-dir")
    provider = FileModelProvider()
    settings = replace(
        Settings.load(), database_path=str(RUN / "runtime.sqlite3"), auto_memory_batch_size=1
    )
    app = create_app(settings, model_provider=provider)
    state = app.state
    memory, auto, embeddings = state.memory, state.automatic_memory, state.embeddings
    auto.model_name = "gpt-6-luna"
    cid = "live-memory-test"
    state.coordinator.create_companion({"companion_id": cid, "name": "记忆测试员"})
    profile = state.personality.get(cid)
    state.personality.import_card(
        cid,
        {
            "command_id": "import-neutral-" + str(profile["revision"]),
            "expected_revision": profile["revision"],
            "card": json.loads(Path("examples/character_cards/memory-test.v2.json").read_text()),
        },
    )
    results = {
        "run_directory": str(RUN),
        "model": "gpt-6-luna",
        "reasoning_effort": "medium",
        "model_usage": "unknown; adapter ledger zeros are synthetic placeholders",
        "dialogue_sources": "synthetic completed-dialogue fixtures; not DialogueService E2E",
        "embedding_model": settings.embedding_model_name,
        "checks": [],
        "answers": [],
    }

    def check(name, passed, evidence):
        results["checks"].append({"name": name, "passed": bool(passed), "evidence": evidence})
        save(RUN / "results.json", results)

    def enqueue(event_id, text):
        event = {
            "event_id": event_id,
            "type": "companion.dialogue_completed",
            "companion_id": cid,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {
                "user_message": text,
                "assistant_message": "已收到这条测试资料。",
                "test_fixture": True,
            },
        }
        with memory._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO events(event_id,protocol_version,type,companion_id,payload_json,occurred_at) VALUES(?,'1',?,?,?,?)",
                (event_id, event["type"], cid, json.dumps(event["payload"]), event["occurred_at"]),
            )
            auto.enqueue(connection, event)
        outcome = auto.process_next(cid, force=True)
        results.setdefault("extraction_jobs", []).append(outcome)
        check(event_id + ":extraction", outcome["status"] == "completed", outcome)

    def answer(name, question):
        preview = state.personality_prompt_builder.preview(
            state.coordinator.get_companion(cid),
            state.personality.get(cid),
            state.personality.get_state(cid),
            {
                "user_message": question,
                "history": [],
                "user_name": "林舟",
                "memory_token_budget": 1800,
                "max_memory_items": 16,
            },
            output_permitted=True,
        )
        save(RUN / f"preview-{name}.json", preview)
        result = provider.complete(
            ModelRequest(
                name, cid, "dialogue_preview", "gpt-6-luna", preview["context_text"], 6000, 700
            )
        )
        results["answers"].append(
            {"name": name, "question": question, "text": result.text, "history": []}
        )
        save(RUN / "results.json", results)
        return result.text

    enqueue(
        "fixture-initial",
        "我叫林舟，我的生日是6月18日。妹妹小禾的生日是11月3日。我喜欢乌龙茶。顾言是我的室友。",
    )
    graph = auto.graph(cid)
    save(RUN / "graph-initial.json", graph)
    check("graph_has_relations", bool(graph["relations"]), graph)
    initial_index = embeddings.run(cid)
    results["index_initial"] = initial_index
    check("real_bge_index", embeddings.status(cid)["stored_vectors"] > 0, embeddings.status(cid))
    text = answer("birthdays", "我的生日和妹妹小禾的生日分别是哪一天？请区分两个人。")
    check("birthdays_distinct", all(x in text for x in ["6月18", "11月3", "小禾"]), text)
    text = answer("tea_initial", "我喜欢喝什么茶？")
    check("initial_tea", "乌龙茶" in text, text)
    query = "我平时偏爱的饮品是什么"
    first = embeddings.prepare_query(cid, query)
    repeated = embeddings.prepare_query(cid, query)
    recalled = embeddings.semantic_recall(
        cid, {"query": query, "token_budget": 1200, "max_items": 12}
    )
    results["semantic"] = {"first": first, "repeat": repeated, "recall": recalled}
    check("semantic_tea", any("乌龙茶" in item["content"] for item in recalled["items"]), recalled)
    enqueue("fixture-correction", "更正一下：我现在改喝无咖啡因薄荷茶，不再喝乌龙茶。")
    results["index_correction"] = embeddings.run(cid)
    text = answer("tea_corrected", "我现在喜欢喝什么茶？只说当前偏好。")
    check("latest_preference", "薄荷茶" in text and "乌龙茶" not in text, text)
    text = answer("unknown", "我的昵称和我养的狗叫什么名字？不知道就直接说不知道。")
    check(
        "unknown_acknowledgement",
        any(x in text for x in ["不知道", "还不知道", "没有", "不清楚", "未"]),
        text,
    )
    results["memory_records"] = memory.list(cid, limit=100)
    results["graph_final"] = auto.graph(cid)
    results["embedding_jobs"] = embeddings.jobs(cid)
    results["embedding_status"] = embeddings.status(cid)
    results["model_calls"] = provider.calls
    results["finished_at"] = datetime.now(UTC).isoformat()
    save(RUN / "results.json", results)
    print("FINISHED " + str(RUN / "results.json"), flush=True)


def enrich_report(run):
    """Local deterministic probes on the isolated database after all live generations."""
    from datetime import timedelta

    from virtual_life.auto_memory import AutoMemoryService
    from virtual_life.memory import MemoryService

    report_path = run / "results.json"
    report = json.loads(report_path.read_text())
    memory = MemoryService(run / "runtime.sqlite3")
    auto = AutoMemoryService(memory)
    memory.automatic_memory = auto
    cid = "live-memory-test"
    checks = report["checks"]

    def check(name, passed, evidence):
        checks.append({"name": name, "passed": bool(passed), "evidence": evidence})

    graph = auto.graph(cid)
    gu = next((x for x in graph["entities"] if x["name"] == "顾言"), None)
    if gu:
        alias_result = auto.update_entity_aliases(
            cid,
            gu["entity_id"],
            {
                "command_id": "alias-live",
                "expected_revision": 1,
                "aliases": ["小顾"],
                "source_event_ids": ["fixture-initial"],
            },
        )
        alias_graph = auto.graph(cid, query="小顾")
        check(
            "graph_alias",
            any(x["name"] == "顾言" for x in alias_graph["entities"]),
            {"update": alias_result, "query": alias_graph},
        )
    else:
        check("graph_alias", False, "顾言 entity missing")
    graph = auto.graph(cid)
    graph_text = json.dumps(graph["relations"], ensure_ascii=False)
    check("graph_correction_old_edge_absent", "乌龙茶" not in graph_text, graph["relations"])
    check("graph_correction_new_edge_present", "薄荷茶" in graph_text, graph["relations"])
    exact_edges = [
        edge
        for edge in graph["relations"]
        if edge["subject"] == "用户"
        and edge["predicate"] == "prefers_drink"
        and edge["object"] == "无咖啡因薄荷茶"
    ]
    check("graph_correction_canonical_edge", len(exact_edges) == 1, exact_edges)
    birthday_edges = graph["relations"]
    check(
        "graph_birthday_subjects",
        any(
            e["subject"] == "用户" and e["object"] == "6月18日" and e["predicate"] == "has_birthday"
            for e in birthday_edges
        )
        and any(
            e["subject"] == "小禾" and e["object"] == "11月3日" and e["predicate"] == "has_birthday"
            for e in birthday_edges
        ),
        birthday_edges,
    )
    with memory._connection() as connection:
        active_versions = {
            (row["memory_id"], row["revision"])
            for row in connection.execute(
                "SELECT memory_id, revision FROM memory_records WHERE companion_id=? AND active=1",
                (cid,),
            )
        }
        current_preference_versions = {
            (row["memory_id"], row["revision"])
            for row in connection.execute(
                "SELECT memory_id,revision FROM memory_records WHERE companion_id=? AND active=1 AND kind='preference' AND content LIKE '%无咖啡因薄荷茶%'",
                (cid,),
            )
        }
    supported = bool(exact_edges) and all(
        "fixture-correction" in edge["source_event_ids"]
        and bool(edge["memory_versions"])
        and all((v["memory_id"], v["revision"]) in active_versions for v in edge["memory_versions"])
        and any(
            (v["memory_id"], v["revision"]) in current_preference_versions
            for v in edge["memory_versions"]
        )
        for edge in exact_edges
    )
    check("graph_correction_current_source_versions", supported, exact_edges)
    report["graph_correction_evidence"] = exact_edges
    report["extraction_diagnostics"] = auto.list_jobs(cid)
    report["prompt_versions"] = [
        job.get("prompt_version") for job in report.get("extraction_jobs", [])
    ]
    current = memory.list(cid, limit=100)["items"]
    birthdays = [
        x for x in current if x["kind"] == "fact" and "birthday" in (x.get("semantic_key") or "")
    ]
    recall = memory.recall(cid, {"query": "生日", "token_budget": 2000, "max_items": 20})
    check(
        "birthday_permanent",
        len(birthdays) >= 2
        and len(
            [x for x in recall["items"] if x["memory_id"] in {b["memory_id"] for b in birthdays}]
        )
        == len(birthdays)
        and all(
            x.get("effective_retention_policy") == "permanent"
            for x in recall["items"]
            if x["memory_id"] in {b["memory_id"] for b in birthdays}
        ),
        birthdays,
    )
    corrected = [x for x in current if x["kind"] == "preference" and "薄荷茶" in x["content"]]
    check(
        "preference_correction_provenance",
        bool(corrected) and bool(corrected[0].get("supersedes_id")),
        corrected,
    )
    now = datetime.now(UTC)
    records = []
    for retention in ["routine_episode", "significant_episode", "permanent"]:
        records.append(
            memory.write(
                cid,
                {
                    "command_id": "age-" + retention,
                    "kind": "episode",
                    "content": "agingprobe " + retention,
                    "episode_key": "age-" + retention,
                    "source_event_id": "fixture-initial",
                    "importance": 0.8,
                    "retention_policy": retention,
                },
            )
        )
    expiring = memory.write(
        cid,
        {
            "command_id": "validity-probe",
            "kind": "fact",
            "content": "validityprobe 临时安排",
            "semantic_key": "test.temporary",
            "source_event_id": "fixture-initial",
            "valid_from": (now - timedelta(days=1)).isoformat(),
            "valid_to": (now + timedelta(days=1)).isoformat(),
        },
    )
    memory.clock = lambda: now + timedelta(days=365)
    aged = memory.recall(cid, {"query": "agingprobe", "token_budget": 2000, "max_items": 20})
    indexed = {x["memory_id"]: x for x in aged["items"]}
    factors = {
        r["retention_policy"]: indexed.get(r["memory_id"], {}).get("decay_factor") for r in records
    }
    check(
        "deterministic_aging_order",
        factors["routine_episode"] is not None
        and factors["routine_episode"] < factors["significant_episode"] < factors["permanent"],
        factors,
    )
    expired = memory.recall(cid, {"semantic_key": "test.temporary", "token_budget": 1000})
    check(
        "expired_fact_excluded",
        not expired["items"],
        {"memory_id": expiring["memory_id"], "recall": expired},
    )
    tiny = memory.recall(cid, {"query": "生日", "token_budget": 24, "max_items": 1})
    check("recall_caps", len(tiny["items"]) <= 1 and tiny.get("estimated_tokens", 0) <= 24, tiny)
    semantic = report.get("semantic", {})
    check(
        "query_repeat_cached",
        semantic.get("repeat", {}).get("model_calls") == 0,
        semantic.get("repeat"),
    )
    with memory._connection() as connection:
        spaces = [
            dict(row) for row in connection.execute("SELECT space,dimensions FROM embedding_spaces")
        ]
    report["embedding_dimensions"] = spaces
    completed = report.get("embedding_jobs", {}).get("items", [])
    check(
        "real_bge_provider_evidence",
        report.get("embedding_model") == "BAAI/bge-m3"
        and any(
            x["status"] == "completed" and (x.get("usage") or {}).get("input_tokens", 0) > 0
            for x in completed
        )
        and any(x["dimensions"] == 1024 for x in spaces),
        {"jobs": completed, "spaces": spaces},
    )
    report["local_probes_note"] = (
        "Alias update and aging/cap probes local deterministic operations; aging clock advanced only in test MemoryService."
    )
    report["embedding_actual_usage"] = [
        j.get("usage") for j in report.get("embedding_jobs", {}).get("items", [])
    ]
    probe_path = run / "source-deletion-probe.sqlite3"
    with sqlite3.connect(run / "runtime.sqlite3") as original, sqlite3.connect(probe_path) as probe:
        original.backup(probe)
    probe_memory = MemoryService(probe_path)
    probe_auto = AutoMemoryService(probe_memory)
    probe_memory.automatic_memory = probe_auto
    before = probe_auto.graph(cid)
    correction_source = next(
        (
            record
            for record in probe_memory.list(cid, limit=100)["items"]
            if record["kind"] == "episode" and "fixture-correction" in record["source_event_ids"]
        ),
        None,
    )
    if correction_source:
        probe_memory.delete(cid, correction_source["memory_id"])
    after = probe_auto.graph(cid)
    check(
        "graph_source_deletion_invalidates_edge",
        bool(exact_edges)
        and correction_source is not None
        and not any(
            e["predicate"] == "prefers_drink" and e["object"] == "无咖啡因薄荷茶"
            for e in after["relations"]
        ),
        {
            "probe_database": str(probe_path),
            "deleted_source": correction_source,
            "before": before,
            "after": after,
        },
    )
    report["summary"] = {
        "total": len(checks),
        "passed": sum(x["passed"] for x in checks),
        "failed": [x["name"] for x in checks if not x["passed"]],
    }
    save(report_path, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=RUN)
    RUN = parser.parse_args().run_dir.resolve()
    main()
    enrich_report(RUN)
    report = json.loads((RUN / "results.json").read_text())
    failed = [x["name"] for x in report["checks"] if not x["passed"]]
    print(f"CHECKS {len(report['checks']) - len(failed)}/{len(report['checks'])}; failed={failed}")
    raise SystemExit(1 if failed else 0)
