"""Focused tests for encoder window probing, one-shot verification, and capability dependency."""
import os
import sys
import pytest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import dream_telemetry
import encoder_window
import hive_mind_proxy as g


def test_config_snapshot_reports_real_embed_max_chars(monkeypatch):
    monkeypatch.delenv("EMBED_MAX_CHARS", raising=False)
    monkeypatch.delenv("EMBED_SPECIAL_TOKEN_RESERVE", raising=False)
    # Reload dream_telemetry and hive_mind_proxy to see unset defaults
    import importlib
    importlib.reload(dream_telemetry)
    importlib.reload(g)
    cfg = g._config_snapshot()
    assert cfg["embed_max_chars"] == 24570
    assert cfg["embed_max_chars"] == dream_telemetry.EMBED_MAX_CHARS


def test_encoder_dependency_window_short_degraded_not_down():
    # 512 advertised tokens on required 8192 -> degraded window_short, NOT down
    window = {"advertised_tokens": 512, "source": "v1_models", "full_payload_ok": False}
    dep = g._encoder_dependency("ok", {"status": "ok"}, window)
    assert dep["state"] == "degraded"
    assert dep["reason"] == "window_short:512<8192"


def test_encoder_dependency_window_overrun_degraded():
    window = {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": False}
    dep = g._encoder_dependency("ok", {"status": "ok"}, window)
    assert dep["state"] == "degraded"
    assert dep["reason"] == "window_overrun"


def test_encoder_dependency_unreachable_stays_down():
    window = {"advertised_tokens": 512, "source": "v1_models", "full_payload_ok": False}
    dep = g._encoder_dependency("down", {"status": "failing"}, window)
    assert dep["state"] == "down"
    assert dep["reason"] == "probe:down"


@pytest.mark.asyncio
async def test_probe_matches_served_model_id_not_data_zero():
    # /v1/models returns multiple models where data[0] is NOT bge-m3
    class FakeResponse:
        def __init__(self, status, data):
            self.status = status
            self._data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return self._data

        async def read(self):
            return b""

    class FakeSession:
        def __init__(self):
            self.posts = []

        def get(self, url, timeout=None):
            if url.endswith("/v1/models"):
                return FakeResponse(200, {
                    "data": [
                        {"id": "other-model", "max_model_len": 4096},
                        {"id": "bge-m3", "max_model_len": 8192},
                    ]
                })
            elif url.endswith("/props"):
                return FakeResponse(404, {})
            return FakeResponse(404, {})

        def post(self, url, json=None, timeout=None):
            self.posts.append((url, json, timeout))
            return FakeResponse(200, {"data": [{"embedding": [0.1]}]})

    session = FakeSession()
    res = await encoder_window.probe_encoder(
        session,
        base_url="http://localhost:8070",
        route="/v1/embeddings",
        model_id="bge-m3",
    )
    assert res["advertised_tokens"] == 8192
    assert res["source"] == "v1_models"
    assert res["full_payload_ok"] is True
    # Verify embed one-shot payload size
    assert len(session.posts) == 1
    post_url, post_json, post_timeout = session.posts[0]
    assert post_url.endswith("/v1/embeddings")
    assert len(post_json["input"]) == dream_telemetry.EMBED_MAX_CHARS
    assert post_timeout.total == dream_telemetry.embed_ceiling(dream_telemetry.EMBED_MAX_CHARS)


@pytest.mark.asyncio
async def test_rerank_one_shot_leaves_query_and_specials_in_window():
    class FakeResponse:
        def __init__(self, status, data):
            self.status = status
            self._data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return self._data

        async def read(self):
            return b""

    class FakeSession:
        def __init__(self):
            self.posts = []

        def get(self, url, timeout=None):
            if url.endswith("/v1/models"):
                return FakeResponse(404, {})
            elif url.endswith("/props"):
                return FakeResponse(200, {"default_generation_settings": {"n_ctx": 8192}})
            return FakeResponse(404, {})

        def post(self, url, json=None, timeout=None):
            self.posts.append((url, json, timeout))
            return FakeResponse(200, {"results": []})

    session = FakeSession()
    res = await encoder_window.probe_encoder(
        session,
        base_url="http://localhost:8071",
        route="/v1/reranking",
        model_id="bge-reranker-v2-m3",
    )
    assert res["advertised_tokens"] == 8192
    assert res["source"] == "props"
    assert res["full_payload_ok"] is True
    assert len(session.posts) == 1
    post_url, post_json, post_timeout = session.posts[0]
    assert post_url.endswith("/v1/reranking")
    assert post_json["query"] == "encoder window probe"
    doc = post_json["documents"][0]
    # Doc length must leave room for query and specials
    expected_doc_len = dream_telemetry.RERANK_MAX_DOC_CHARS - len("encoder window probe") - int(dream_telemetry.EMBED_SPECIAL_TOKEN_RESERVE * dream_telemetry.EMBED_CHARS_PER_TOKEN)
    assert len(doc) == expected_doc_len


@pytest.mark.asyncio
async def test_probe_cache_and_carry_forward():
    # If encoder fails on subsequent cycle, carry forward cached advertised and full_payload_ok
    encoder_window.set_encoder_cache("embedder", {
        "advertised_tokens": 8192,
        "source": "v1_models",
        "full_payload_ok": True,
    })

    class FailingSession:
        def get(self, url, timeout=None):
            raise ConnectionRefusedError("down")

        def post(self, url, json=None, timeout=None):
            raise ConnectionRefusedError("down")

    session = FailingSession()
    res = await encoder_window.probe_encoder(
        session,
        base_url="http://localhost:8070",
        route="/v1/embeddings",
        model_id="bge-m3",
    )
    # Carried forward across failing cycle
    assert res["advertised_tokens"] == 8192
    assert res["full_payload_ok"] is True


@pytest.mark.asyncio
async def test_build_health_checks_includes_encoder_window():
    encoder_window.set_encoder_cache("embedder", {
        "advertised_tokens": 512,
        "source": "v1_models",
        "full_payload_ok": False,
    })
    encoder_window.set_encoder_cache("reranker", {
        "advertised_tokens": 8192,
        "source": "v1_models",
        "full_payload_ok": True,
    })

    class FakeCoordinator:
        pgvector_version = "0.7.0"
        hnsw_iterative_scan = True
        def dependency_snapshot(self):
            return {
                "postgres": {"state": "ok"},
                "neo4j": {"state": "ok"},
            }

    class FakeResp:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def get(self, url, timeout=None):
            return FakeResp()

    class FakeProxy:
        session = FakeSession()

    checks = await g._build_health_checks(FakeProxy(), FakeCoordinator())
    assert "encoder_window" in checks
    assert checks["encoder_window"]["required_tokens"] == 8192
    assert checks["encoder_window"]["embed_max_chars"] == 24570
    assert checks["encoder_window"]["embedder"]["advertised_tokens"] == 512
    # 512 < 8192 -> degraded with window_short:512<8192, NOT down
    embedder_dep = checks["dependencies"]["embedder"]
    assert embedder_dep["state"] == "degraded"
    assert embedder_dep["reason"] == "window_short:512<8192"
    assert checks["dependencies"]["reranker"]["state"] == "ok"
    # Critical down check for 503: neither is down!
    critical_down = any(
        checks["dependencies"][name]["state"] == g._STATE_DOWN
        for name in ("embedder", "reranker")
    )
    assert critical_down is False
