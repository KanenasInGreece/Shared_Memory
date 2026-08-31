"""W0 item ② — the startup journal must never render a URL's userinfo/query
unscrubbed.

Before this, the gateway's own startup routing announcement
("### /v1/embeddings->%s | /v1/reranking->%s | default->LLM pool") rendered
EMBEDDER_URL/RERANKER_URL verbatim -- the same class of leak
scrub_url_credentials (log_hygiene.py) already exists to close everywhere
else a URL might carry a credential (userinfo or a `?key=...` query
parameter) into a log file. This test targets the routing line specifically:
the render call is split into a pure helper, `_encoder_routing_log_line()`
(hive_mind_proxy.py), precisely so a test can assert on the exact string
without capturing logging output -- same shape as
tests/test_coordinator_encoder_urls.py's `_reloaded()` pattern, reused here.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _reloaded(monkeypatch, **env):
    for k in ("EMBEDDER_URL", "RERANKER_URL", "LLM_BACKENDS_JSON", "LLM_BACKENDS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module("hive_mind_proxy")
    return importlib.reload(mod)


def test_userinfo_absent_scrubbed_form_present(monkeypatch):
    mod = _reloaded(
        monkeypatch,
        EMBEDDER_URL="http://user:s3cr3t-embed-key@embed.example:8070",
        RERANKER_URL="http://user:s3cr3t-rerank-key@rerank.example:8071",
    )
    line = mod._encoder_routing_log_line()
    assert "s3cr3t-embed-key" not in line
    assert "s3cr3t-rerank-key" not in line
    assert "user:" not in line
    assert "http://embed.example:8070" in line
    assert "http://rerank.example:8071" in line


def test_default_urls_render_unchanged(monkeypatch):
    mod = _reloaded(monkeypatch)
    line = mod._encoder_routing_log_line()
    assert "http://localhost:8070" in line
    assert "http://localhost:8071" in line


def test_line_shape_unchanged_apart_from_scrubbing(monkeypatch):
    mod = _reloaded(monkeypatch)
    line = mod._encoder_routing_log_line()
    assert line.startswith("### /v1/embeddings->")
    assert " | /v1/reranking->" in line
    assert line.endswith(" | default->LLM pool")


@pytest.fixture(autouse=True)
def _restore_module(monkeypatch):
    """Reloading hive_mind_proxy rebinds the module object process-wide --
    restore a clean-env reload after every test so later test files that
    import it fresh do not inherit this file's env (same convention as
    tests/test_coordinator_encoder_urls.py)."""
    yield
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(importlib.import_module("hive_mind_proxy"))
