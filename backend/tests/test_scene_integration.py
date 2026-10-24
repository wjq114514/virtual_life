"""Scene-classifier routing and observe-mode integration contracts."""

import os
import time

import pytest
from fastapi.testclient import TestClient
from test_api import attach_desktop
from virtual_life.api import create_app
from virtual_life.ports import ModelResult, ModelUsage
from virtual_life.scene_classifier.decision import Thresholds, decide
from virtual_life.scene_classifier.inference import FakePredictor
from virtual_life.scene_classifier.runtime import SceneClassifier
from virtual_life.scene_config import SceneClassifierConfig
from virtual_life.settings import Settings


class Provider:
    def __init__(self):
        self.calls = 0
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        return ModelResult("好。", "fake", ModelUsage(10, 5))


def make_app(tmp_path, classifier=None):
    provider = Provider()
    settings = Settings(
        database_path=str(tmp_path / "scene.sqlite"),
        api_token="test",
        model_base_url="http://model.invalid",
        model_name="fake",
        auto_memory_enabled=False,
        lease_ttl_seconds=10000,
        node_ttl_seconds=10000,
    )
    return create_app(settings, model_provider=provider, scene_classifier=classifier), provider


def send(client, state, message="陪我聊会儿天"):
    return client.post(
        "/v1/companions/sui/dialogue",
        json={
            "command_id": "turn",
            "node_id": "desk",
            "epoch": state["epoch"],
            "expected_revision": state["revision"],
            "user_message": message,
        },
    )


def test_route_mode_switches_only_the_reply_model(tmp_path):
    classifier = SceneClassifier(
        SceneClassifierConfig(mode="route", models={"explicit": "local-explicit"}),
        predictor=FakePredictor((0.01, 0.02, 0.03, 0.94), version="test-v1"),
        thresholds=Thresholds(),
    )
    app, provider = make_app(tmp_path, classifier)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        response = send(client, state, "我想继续刚才那种事")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed"
        assert result["assistant_message"] == "好。"
        assert result["model"] == "local-explicit"
        assert result["scene"]["mode"] == "route"
        assert result["scene"]["label"] == "explicit"
        assert result["scene"]["routed_model"] == "local-explicit"
        assert result["scene"]["classifier_version"] == "test-v1"
        assert set(result["scene"]["scores"]) == {"daily", "emotional", "intimate", "explicit"}
        assert provider.requests[0].model == "local-explicit"
        capabilities = client.get("/v1/capabilities").json()["scene_classifier"]
        assert capabilities["mode"] == "route"
        assert capabilities["available"] is True
        assert capabilities["route_models"] == {"explicit": "local-explicit"}


def test_observe_mode_records_without_changing_the_model(tmp_path):
    classifier = SceneClassifier(
        SceneClassifierConfig(mode="observe", models={"explicit": "local-explicit"}),
        predictor=FakePredictor((0.01, 0.02, 0.03, 0.94), version="test-v1"),
        thresholds=Thresholds(),
    )
    app, provider = make_app(tmp_path, classifier)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        response = send(client, state, "我想继续刚才那种事")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed"
        assert result["model"] == "fake"
        assert result["scene"]["mode"] == "observe"
        assert result["scene"]["label"] == "explicit"
        assert result["scene"]["routed_model"] is None
        assert provider.requests[0].model == "fake"
        assert result["assistant_message"] == "好。"


def test_route_mode_fails_open_when_inference_fails(tmp_path):
    predictor = FakePredictor(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    classifier = SceneClassifier(
        SceneClassifierConfig(mode="route", models={"explicit": "local-explicit"}),
        predictor=predictor,
        thresholds=Thresholds(),
    )
    app, provider = make_app(tmp_path, classifier)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        response = send(client, state, "随便说点什么")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed"
        assert result["model"] == "fake"
        assert result["scene"]["mode"] == "route"
        assert result["scene"]["label"] is None
        assert result["scene"]["error_code"] == "classifier_failed"
        assert provider.requests[0].model == "fake"


def test_route_mode_times_out_without_blocking_dialogue(tmp_path):
    class SlowPredictor:
        backend = "fake"
        version = "slow"
        model_name = "slow"

        def predict(self, text, thresholds=None):
            time.sleep(0.2)
            return decide((0.01, 0.02, 0.03, 0.94), thresholds)

        def predict_proba(self, text):
            return (0.01, 0.02, 0.03, 0.94)

        def predict_proba_many(self, texts, *, batch_size=32):
            return [self.predict_proba(text) for text in texts]

        def warmup(self):
            return None

    classifier = SceneClassifier(
        SceneClassifierConfig(
            mode="route",
            models={"explicit": "local-explicit"},
            timeout_seconds=0.01,
        ),
        predictor=SlowPredictor(),
        thresholds=Thresholds(),
    )
    app, provider = make_app(tmp_path, classifier)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        response = send(client, state, "随便说点什么")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed"
        assert result["model"] == "fake"
        assert result["scene"]["error_code"] == "classifier_timeout"
        assert provider.requests[0].model == "fake"


def test_disabled_classifier_keeps_the_existing_contract(tmp_path):
    app, provider = make_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test"}) as client:
        state = attach_desktop(client)
        response = send(client, state)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["scene"] is None
        assert result["model"] == "fake"
        assert provider.requests[0].model == "fake"
        assert client.get("/v1/capabilities").json()["scene_classifier"]["mode"] == "off"


def test_missing_artifact_is_available_as_a_degraded_runtime(tmp_path):
    classifier = SceneClassifier(
        SceneClassifierConfig(mode="observe", artifact=str(tmp_path / "missing"))
    )
    assert classifier.capabilities()["available"] is False
    assert classifier.capabilities()["error_code"] == "classifier_unavailable"
    observation = classifier.classify("你好")
    assert observation is not None
    assert observation.error_code == "classifier_unavailable"
    classifier.close()


@pytest.fixture
def clean_scene_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("VIRTUAL_LIFE_SCENE_CLASSIFIER_"):
            monkeypatch.delenv(name, raising=False)


def test_scene_settings_paths_environment_and_validation(
    tmp_path, monkeypatch, clean_scene_environment
):
    assert Settings().scene_classifier.mode == "off"
    path = tmp_path / "config.toml"
    path.write_text(
        '[scene_classifier]\nmode = "observe"\nartifact = "model"\n',
        encoding="utf8",
    )
    settings = Settings.load(path)
    assert settings.scene_classifier.mode == "observe"
    assert settings.scene_classifier.artifact == str(tmp_path / "model")
    assert settings.scene_classifier.models == {}

    monkeypatch.setenv("VIRTUAL_LIFE_SCENE_CLASSIFIER_MODE", "route")
    monkeypatch.setenv("VIRTUAL_LIFE_SCENE_CLASSIFIER_MODELS", '{"explicit": "local-explicit"}')
    monkeypatch.setenv("VIRTUAL_LIFE_SCENE_CLASSIFIER_MAX_LENGTH", "64")
    settings = Settings.load(path)
    assert settings.scene_classifier.mode == "route"
    assert settings.scene_classifier.models == {"explicit": "local-explicit"}
    assert settings.scene_classifier.max_length == 64

    monkeypatch.setenv("VIRTUAL_LIFE_SCENE_CLASSIFIER_ARTIFACT", "relative-model")
    settings = Settings.load(path)
    assert settings.scene_classifier.artifact == "relative-model"


@pytest.mark.parametrize(
    "document",
    [
        '[scene_classifier]\nmode = "route"\n',
        '[scene_classifier]\nmode = "observe"\nmodels = { unknown = "x" }\n',
        '[scene_classifier]\nmode = "observe"\nartifact = ""\n',
        '[scene_classifier]\nmode = "observe"\nmax_length = 9999\n',
        "[scene_classifier]\nunknown = true\n",
    ],
)
def test_scene_settings_reject_invalid_configuration(tmp_path, document, clean_scene_environment):
    path = tmp_path / "invalid.toml"
    path.write_text(document, encoding="utf8")
    with pytest.raises(ValueError):
        Settings.load(path)
