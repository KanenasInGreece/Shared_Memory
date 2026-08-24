"""The coordinator's own embed/rerank calls honour EMBEDDER_URL / RERANKER_URL.

Before this, coordinator.py held two literals (localhost:8070 / :8071) while the
gateway's routing map read the env — so pointing EMBEDDER_URL at a remote host
moved only the raw /v1/embeddings passthrough, and every real save/search
embedding kept going to the local container. One env setting must move both.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _reloaded(monkeypatch, **env):
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Other tests evict `coordinator` from sys.modules; import fresh, then reload
    # so the module-level constants are re-evaluated under THIS env.
    mod = importlib.import_module("coordinator")
    return importlib.reload(mod)


def test_defaults_are_the_local_encoders(monkeypatch):
    mod = _reloaded(monkeypatch)
    assert mod.EMBED_URL == "http://localhost:8070/v1/embeddings"
    assert mod.RERANK_URL == "http://localhost:8071/v1/reranking"


def test_env_base_moves_the_coordinator_call_not_only_the_passthrough(monkeypatch):
    mod = _reloaded(monkeypatch, EMBEDDER_URL="http://embed.example:1234/",
                    RERANKER_URL="http://rerank.example:9/")
    assert mod.EMBED_URL == "http://embed.example:1234/v1/embeddings"
    assert mod.RERANK_URL == "http://rerank.example:9/v1/reranking"


def test_empty_env_value_falls_back_to_default(monkeypatch):
    mod = _reloaded(monkeypatch, EMBEDDER_URL="")
    assert mod.EMBED_URL == "http://localhost:8070/v1/embeddings"


def test_helper_is_pure():
    assert importlib.import_module("coordinator")._encoder_url("NO_SUCH_ENV_VAR_X", "http://h:1/", "/p") == "http://h:1/p"
