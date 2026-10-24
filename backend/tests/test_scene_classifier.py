from __future__ import annotations

import json
import random
from collections import Counter

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from virtual_life.model_routing import HttpChoiceRouter, Route, RoutingConfig
from virtual_life.scene_classifier.artifact import (
    read_thresholds,
    write_thresholds,
)
from virtual_life.scene_classifier.dataset import (
    Sample,
    clean_records,
    contains_minor_content,
    normalize_text,
    read_jsonl,
    sanitize_pii,
    split_samples,
    write_jsonl,
)
from virtual_life.scene_classifier.decision import (
    Thresholds,
    classification_report,
    decide,
    macro_f1,
    softmax,
    tune_thresholds,
)
from virtual_life.scene_classifier.generation import (
    CONSULTATION_TOPICS,
    build_prompt,
    parse_messages,
)
from virtual_life.scene_classifier.inference import FakePredictor
from virtual_life.scene_classifier.labels import LABELS
from virtual_life.scene_classifier.service import create_app


def test_softmax_is_stable_and_normalized():
    values = softmax([1000.0, 1001.0, 1002.0])
    assert sum(values) == pytest.approx(1.0)
    assert values[2] > values[1] > values[0]
    with pytest.raises(ValueError):
        softmax([float("inf")])


def test_decision_uses_ordered_thresholds():
    explicit = decide([0.1, 0.1, 0.2, 0.6])
    assert explicit.label == "explicit"
    assert explicit.reason == "threshold"

    intimate = decide([0.1, 0.1, 0.6, 0.1])
    assert intimate.label == "intimate"

    emotional = decide([0.1, 0.6, 0.1, 0.1])
    assert emotional.label == "emotional"

    daily = decide([0.7, 0.1, 0.1, 0.1])
    assert daily.label == "daily"
    assert daily.reason == "fallback"

    # A sensitive class always wins over a less sensitive one once it crosses
    # its own threshold, even when the latter has a larger probability.
    overridden = decide([0.05, 0.1, 0.45, 0.4])
    assert overridden.label == "explicit"


def test_thresholds_validation():
    with pytest.raises(ValueError):
        Thresholds(explicit=1.2)
    with pytest.raises(ValueError):
        Thresholds.from_mapping({"nope": 0.5})
    assert Thresholds.from_value([0.7, 0.5, 0.5, 0.3]).explicit == pytest.approx(0.3)


def test_threshold_tuning_finds_feasible_rule():
    rng = random.Random(7)
    probabilities: list[list[float]] = []
    truth: list[str] = []
    for label in LABELS:
        for _ in range(120):
            scores = [rng.uniform(0.01, 0.08) for _ in LABELS]
            scores[LABELS.index(label)] = rng.uniform(0.72, 0.9)
            probabilities.append(scores)
            truth.append(label)
    result = tune_thresholds(
        np.asarray(probabilities),
        truth,
        target_explicit_recall=0.9,
        target_daily_precision=0.85,
    )
    assert result.feasible
    assert result.metrics["classes"]["explicit"]["recall"] >= 0.9
    assert result.thresholds.explicit <= 0.8
    assert result.metrics["macro_f1"] > 0.9


def test_classification_report_and_macro_f1():
    truth = ["daily", "daily", "explicit", "intimate"]
    predicted = ["daily", "daily", "explicit", "intimate"]
    report = classification_report(truth, predicted)
    assert report["classes"]["daily"]["recall"] == pytest.approx(1.0)
    assert report["classes"]["explicit"]["precision"] == pytest.approx(1.0)
    assert report["macro_f1"] == pytest.approx(macro_f1(truth, predicted))
    assert len(report["confusion_matrix"]) == len(LABELS)


def test_normalize_and_pii_redaction():
    assert normalize_text("  1. 用户： “今天天气不错。”  ") == "今天天气不错。"
    assert normalize_text("- 我：你好") == "你好"
    assert normalize_text("《三体》很好看") == "《三体》很好看"
    assert normalize_text("1.5米高") == "1.5米高"
    text = sanitize_pii("联系我 test@example.com 或 13800138000，见 https://example.com")
    assert "test@example.com" not in text
    assert "13800138000" not in text
    assert "https://example.com" not in text
    assert "[邮箱]" in text and "[电话]" in text and "[链接]" in text


def test_minor_content_guard_is_contextual():
    assert contains_minor_content("我17岁，想和你做爱")
    assert contains_minor_content("未成年也可以吧，脱衣服")
    assert not contains_minor_content("今天看了未成年人保护法的新闻")
    assert not contains_minor_content("我今年17岁，最近考试压力很大")


def test_clean_records_filters_and_deduplicates():
    records = [
        {"text": "1. 今天上班真的好累啊，想早点回家休息", "label": "daily"},
        {"text": "今天上班真的好累啊，想早点回家休息", "label": "daily"},
        {"text": "今天上班真的好累啊，想早点回家休息一下", "label": "daily"},
        {"text": "短", "label": "daily"},
        {"text": "我17岁，想和你做爱", "label": "explicit"},
        {"text": "你好呀", "label": "unknown"},
    ]
    stats = Counter()
    cleaned = clean_records(records, stats=stats)
    assert [sample.text for sample in cleaned] == ["今天上班真的好累啊，想早点回家休息"]
    assert stats["exact_duplicate"] == 1
    assert stats["near_duplicate"] == 1
    assert stats["minor_content_rejected"] == 1
    assert stats["invalid_record"] == 1


def test_split_is_stratified_and_disjoint():
    samples = [
        Sample(text=f"{label}-{index}", label=label) for label in LABELS for index in range(20)
    ]
    splits = split_samples(samples, ratios=(0.6, 0.2, 0.2), seed=3)
    assert sum(len(group) for group in splits.values()) == len(samples)
    assert all(splits[name] for name in ("train", "val", "test"))
    for label in LABELS:
        assert any(sample.label == label for sample in splits["train"])
        assert any(sample.label == label for sample in splits["val"])
        assert any(sample.label == label for sample in splits["test"])
    keys = {name: {sample.text for sample in group} for name, group in splits.items()}
    assert not keys["train"] & keys["val"]
    assert not keys["train"] & keys["test"]
    assert not keys["val"] & keys["test"]


def test_jsonl_roundtrip(tmp_path):
    path = tmp_path / "data.jsonl"
    assert write_jsonl(path, [{"text": "你好", "label": "daily"}]) == 1
    assert read_jsonl(path) == [{"text": "你好", "label": "daily"}]


def test_threshold_artifact_roundtrip(tmp_path):
    path = tmp_path / "thresholds.json"
    write_thresholds(path, Thresholds(explicit=0.3, intimate=0.5, emotional=0.6))
    loaded = read_thresholds(path)
    assert loaded.explicit == pytest.approx(0.3)
    assert loaded.intimate == pytest.approx(0.5)


def test_generation_parser_handles_json_and_lines():
    assert parse_messages('{"messages": ["今天好累", "想吃点好的"]}') == [
        "今天好累",
        "想吃点好的",
    ]
    assert parse_messages('```json\n{"messages": ["今天好累"]}\n```') == ["今天好累"]
    assert parse_messages("1. 今天好累\n2. - 想吃点好的\n说明：忽略我") == [
        "今天好累",
        "想吃点好的",
    ]
    assert parse_messages("[]") == []


def test_prompt_is_json_only_and_adult():
    prompt = build_prompt("intimate", random.Random(1), count=5)
    assert len(prompt) == 2
    assert "成年人" in prompt[0]["content"]
    assert "JSON" in prompt[1]["content"]
    assert "5 条" in prompt[1]["content"]


def test_consultation_prompt_is_explicit_nsfw():
    prompt = build_prompt("explicit", random.Random(2), count=5, subtype="consultation")
    content = prompt[1]["content"]
    assert "问题咨询" in content
    assert "性健康" in content
    assert "做爱" in content
    assert "不写回答" in content
    direct = build_prompt("explicit", random.Random(2), count=5, subtype="direct")
    assert "问题咨询" not in direct[1]["content"]


def test_consultation_topic_can_be_forced():
    topic = CONSULTATION_TOPICS[4]
    prompt = build_prompt(
        "explicit",
        random.Random(3),
        count=5,
        subtype="consultation",
        consult_topic=topic,
    )
    assert topic in prompt[1]["content"]
    with pytest.raises(ValueError):
        build_prompt(
            "explicit",
            random.Random(3),
            count=5,
            subtype="consultation",
            consult_topic="不存在的主题",
        )


def _client(token: str = "", max_chars: int = 4096) -> TestClient:
    predictor = FakePredictor((0.05, 0.10, 0.15, 0.70), version="test-v1")
    app = create_app(predictor, token=token, max_chars=max_chars)
    return TestClient(app)


def test_classify_endpoint():
    with _client() as client:
        response = client.post("/classify", json={"text": "今晚想和你亲近一点"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["label"] == "explicit"
        assert body["scores"]["explicit"] == pytest.approx(0.7)
        assert body["backend"] == "fake"
        assert body["version"] == "test-v1"

        query = client.post("/classify?text=今天天气不错")
        assert query.status_code == 200


def test_classify_validation_and_auth():
    with _client(token="secret") as client:
        assert client.post("/classify", json={"text": "你好"}).status_code == 401
        headers = {"Authorization": "Bearer secret"}
        assert client.post("/classify", json={"text": "你好"}, headers=headers).status_code == 200
        assert client.post("/classify", json={"text": ""}, headers=headers).status_code == 422
    with _client(max_chars=5) as client:
        assert client.post("/classify", json={"text": "超过五个字符"}).status_code == 413


def test_jev_compatible_endpoint():
    payload = {
        "model": "typesafe/jev-latest",
        "state": {"text": "今晚想和你亲近一点"},
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "choose",
                "criteria": {label: label for label in LABELS},
            }
        },
    }
    with _client() as client:
        response = client.post("/v1/decide", json=payload)
        assert response.status_code == 200, response.text
        answer = response.json()["answers"]["route"]
        assert answer["choice"] == "explicit"
        assert answer["confidence"] == pytest.approx(0.7)
        assert set(answer["probabilities"]) == set(LABELS)

        payload["questions"]["route"]["criteria"] = {
            "daily": "daily",
            "emotional": "emotional",
        }
        fallback = client.post("/v1/decide", json=payload)
        assert fallback.status_code == 200
        assert fallback.json()["answers"]["route"]["choice"] == "emotional"

        payload["questions"]["route"]["criteria"] = {"reply-fast": "fast"}
        invalid = client.post("/v1/decide", json=payload)
        assert invalid.status_code == 422


def test_existing_router_can_consume_jev_endpoint():
    config = RoutingConfig(
        enabled=True,
        gate="jev",
        base_url="http://classifier.test/v1/decide",
        model="scene-classifier",
        min_confidence=0.0,
        default_route="daily",
        routes=(
            Route(id="daily", criteria="日常", reply_model="online-cheap"),
            Route(id="explicit", criteria="露骨", reply_model="local"),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["state"]["text"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "route": {
                        "choice": "explicit",
                        "confidence": 0.7,
                        "probabilities": {"daily": 0.1, "explicit": 0.7},
                    }
                }
            },
        )

    router = HttpChoiceRouter(config, transport=httpx.MockTransport(handler))
    decision = router.choose("reply", {"text": "今晚想和你亲近一点"})
    assert decision.route_id == "explicit"
    assert decision.reply_model == "local"
    assert decision.reason == "judge_choice"
