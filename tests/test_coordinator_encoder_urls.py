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


import pytest


@pytest.fixture(autouse=True)
def _restore_coordinator_module(monkeypatch):
    """Reloading `coordinator` rebinds the module object process-wide, while
    hive_mind_proxy holds `from coordinator import …` names bound at ITS import.
    Restore a clean-env reload after every test so the end state of this file
    does not depend on test order (review finding F4, PR #307)."""
    yield
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(importlib.import_module("coordinator"))


def test_both_consumers_agree_on_an_empty_value(monkeypatch):
    """`EMBEDDER_URL=` (present but empty) means "the default" for the gateway's
    routing map AND for the coordinator — a split brain here sends the
    passthrough one way and every real save another."""
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.setenv(k, "")
    coord = importlib.reload(importlib.import_module("coordinator"))
    proxy = importlib.reload(importlib.import_module("hive_mind_proxy"))
    assert proxy.EMBEDDER_URL == "http://localhost:8070"
    assert proxy.RERANKER_URL == "http://localhost:8071"
    assert coord.EMBED_URL == proxy.EMBEDDER_URL + "/v1/embeddings"
    assert coord.RERANK_URL == proxy.RERANKER_URL + "/v1/reranking"
    monkeypatch.setenv("EMBEDDER_URL", "  http://h:1/  ")
    coord = importlib.reload(importlib.import_module("coordinator"))
    proxy = importlib.reload(importlib.import_module("hive_mind_proxy"))
    assert proxy.EMBEDDER_URL == "http://h:1"
    assert coord.EMBED_URL == "http://h:1/v1/embeddings"


@pytest.mark.asyncio
async def test_embed_failure_message_never_carries_url_credentials(monkeypatch):
    """The 503 body a client sees is built from str(exc); httpx renders the
    full request URL including user:pass@ — the encoder URL is now operator-
    supplied, so that is a credential-leak path unless scrubbed."""
    import httpx
    monkeypatch.setenv("EMBEDDER_URL", "http://svc:s3cr3t-token@embedder.internal:8070")
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    coord = importlib.reload(importlib.import_module("coordinator"))

    class _Client:
        async def post(self, url, **kw):
            # A real 401: the coordinator's own r.raise_for_status() builds the
            # message, and THAT is what renders the full URL with userinfo.
            return httpx.Response(401, request=httpx.Request("POST", url))

    obj = coord.MemoryCoordinator.__new__(coord.MemoryCoordinator)
    with pytest.raises(RuntimeError) as ei:
        await coord.MemoryCoordinator._embed(obj, "hello", _Client())
    msg = str(ei.value)
    assert "s3cr3t" not in msg and "svc:" not in msg
    assert "embedder.internal:8070" in msg          # host survives — the operator can still locate the fault
    assert "EMBEDDER_URL" in msg


async def _no_sleep(_):
    return None
