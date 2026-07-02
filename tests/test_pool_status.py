"""Pool-availability gate: gateway /pool/status + the daemon client helper.
Replaces the global nvtop gate that self-deferred to our own dream work."""
import asyncio
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def test_pool_status_reports_free_slots(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:4000")
    import hive_mind_proxy as g
    importlib.reload(g)
    g._llm_inflight["http://a:5000"] = 1          # A busy, B free
    resp = asyncio.run(g.handle_pool_status(None))
    d = json.loads(resp.body)
    assert d["free_slots"] == 1
    assert d["backends"]["http://b:4000"]["available"] is True
    assert d["backends"]["http://a:5000"]["available"] is False


def test_pool_status_none_free_when_all_busy(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:4000")
    import hive_mind_proxy as g
    importlib.reload(g)
    g._llm_inflight["http://a:5000"] = 1
    g._llm_inflight["http://b:4000"] = 2
    d = json.loads(asyncio.run(g.handle_pool_status(None)).body)
    assert d["free_slots"] == 0


def test_pool_status_excludes_reserved(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:4000")
    import hive_mind_proxy as g
    importlib.reload(g)
    g._llm_reserved.add("http://b:4000")          # reserved → not available
    d = json.loads(asyncio.run(g.handle_pool_status(None)).body)
    assert d["backends"]["http://b:4000"]["available"] is False
    assert d["free_slots"] == 1                    # only A


def test_client_fail_open_when_gateway_unreachable(monkeypatch):
    monkeypatch.setenv("POOL_STATUS_URL", "http://127.0.0.1:1/pool/status")
    import pool_status
    importlib.reload(pool_status)
    # unreachable gateway → assume available so dreaming is never permanently blocked
    assert asyncio.run(pool_status.pool_has_free_slot(timeout=0.5)) is True
