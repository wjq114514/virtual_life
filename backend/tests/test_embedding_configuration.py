"""CPU TEI embedding and fail-open rerank configuration."""

import pytest
from virtual_life.settings import Settings


def _clear_environment(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("VIRTUAL_LIFE_"):
            monkeypatch.delenv(key)


def test_cpu_tei_embedding_and_rerank_round_trip(tmp_path, monkeypatch):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text(
        """
[embedding]
enabled = true
base_url = "http://127.0.0.1:8080/v1"
name = "BAAI/bge-base-zh-v1.5"
dimensions = 768
max_input_tokens = 512
local = true
space_identity = "tei-bge-zh-768-v1"

[rerank]
enabled = true
base_url = "http://127.0.0.1:8081"
name = "BAAI/bge-reranker-base"
timeout_seconds = 0.35
candidate_limit = 8
min_candidates = 4
rrf_weight = 0.2
""",
        encoding="utf8",
    )
    settings = Settings.load(path)
    assert settings.embedding_local is True
    assert settings.embedding_dimensions == 768
    assert settings.embedding_max_input_tokens == 512
    assert settings.rerank_enabled is True
    assert settings.rerank_base_url == "http://127.0.0.1:8081"
    assert settings.rerank_model_name == "BAAI/bge-reranker-base"
    assert settings.rerank_timeout_seconds == 0.35
    assert settings.rerank_candidate_limit == 8
    assert settings.rerank_min_candidates == 4
    assert settings.rerank_rrf_weight == 0.2


def test_rerank_environment_overrides_use_the_documented_names(tmp_path, monkeypatch):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text("[rerank]\nenabled = false\n", encoding="utf8")
    monkeypatch.setenv("VIRTUAL_LIFE_EMBEDDING_LOCAL", "true")
    monkeypatch.setenv("VIRTUAL_LIFE_RERANK_ENABLED", "true")
    monkeypatch.setenv("VIRTUAL_LIFE_RERANK_BASE_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("VIRTUAL_LIFE_RERANK_MODEL", "BAAI/bge-reranker-base")
    monkeypatch.setenv("VIRTUAL_LIFE_RERANK_CANDIDATE_LIMIT", "12")
    monkeypatch.setenv("VIRTUAL_LIFE_RERANK_MIN_CANDIDATES", "6")
    monkeypatch.setenv("VIRTUAL_LIFE_RERANK_RRF_WEIGHT", "0")
    settings = Settings.load(path)
    assert settings.embedding_local is True
    assert settings.rerank_enabled is True
    assert settings.rerank_model_name == "BAAI/bge-reranker-base"
    assert settings.rerank_candidate_limit == 12
    assert settings.rerank_min_candidates == 6
    assert settings.rerank_rrf_weight == 0


@pytest.mark.parametrize(
    "payload",
    [
        "[rerank]\nenabled = true\n",
        "[rerank]\ncandidate_limit = 4\nmin_candidates = 8\n",
        "[rerank]\nrrf_weight = 1.1\n",
        "[rerank]\ntimeout_seconds = 0\n",
        "[embedding]\nlocal = 1\n",
    ],
)
def test_invalid_cpu_retrieval_configuration_is_rejected(tmp_path, monkeypatch, payload):
    _clear_environment(monkeypatch)
    path = tmp_path / "settings.toml"
    path.write_text(payload, encoding="utf8")
    with pytest.raises(ValueError):
        Settings.load(path)
