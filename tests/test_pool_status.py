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


# ── F11: the in-flight slot must never leak ──────────────────────────────────
# The slot is reserved INSIDE the try whose finally releases it. Reserving before
# the try left a window (url/header construction, or a CancelledError from an early
# client disconnect) where a slot leaked permanently — and a leaked slot makes the
# pool read busy forever, starving the idle-gated dream daemons with no recovery
# short of a gateway restart.

class _BoomSession:
    closed = False
    def request(self, *a, **k):
        raise RuntimeError("upstream boom")


class _FakeReq:
    method = "POST"
    path = "/v1/chat/completions"        # not in ROUTING_MAP → the LLM pool branch
    rel_url = "/v1/chat/completions"
    headers = {}
    can_read_body = True
    async def read(self):
        return b'{"messages":[]}'


def test_inflight_slot_released_when_dispatch_raises(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _BoomSession()
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert g._llm_inflight["http://a:5000"] == 0          # released, not leaked
    assert g._llm_inflight_started["http://a:5000"] == []  # start-stamp cleared too


def test_inflight_not_reserved_when_request_never_dispatches(monkeypatch):
    """A failure BEFORE the try (header/url construction) must not reserve a slot
    at all — nothing to leak."""
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _BoomSession()
    monkeypatch.setattr(proxy, "_filter_headers",
                        lambda h: (_ for _ in ()).throw(RuntimeError("header boom")))
    try:
        asyncio.run(proxy.handle_proxy(_FakeReq()))
    except RuntimeError:
        pass                                              # raised before the try
    assert g._llm_inflight["http://a:5000"] == 0
