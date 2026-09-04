"""Pool-availability gate: gateway /pool/status + the daemon client helper.
Replaces the global nvtop gate that self-deferred to our own dream work."""
import asyncio
import importlib
import json
import os
import sys

from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _auth_off_gateway(monkeypatch):
    """SEC-A5-01/03 (PR A5 fix round): /pool/status's roster/pool-state now
    gates on AUTH_CONFIGURED_AT_STARTUP, so these tests (which pass request=
    None and never present a token) need it reliably False regardless of
    what an earlier test file left in os.environ/secure_env's cache --
    mirrors tests/test_health_anonymous_slimming.py's _load_gateway."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    return g


def test_pool_status_reports_free_slots(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": True},
        {"url": "http://b:4000", "private_ok": True},
    ]))
    g = _auth_off_gateway(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1          # A busy, B free
    resp = asyncio.run(g.handle_pool_status(None))
    d = json.loads(resp.body)
    assert d["free_slots"] == 1
    assert d["backends"]["http://b:4000"]["available"] is True
    assert d["backends"]["http://a:5000"]["available"] is False


def test_pool_status_none_free_when_all_busy(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:4000")
    g = _auth_off_gateway(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1
    g._llm_inflight["http://b:4000"] = 2
    d = json.loads(asyncio.run(g.handle_pool_status(None)).body)
    assert d["free_slots"] == 0


def test_pool_status_excludes_reserved(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": True},
        {"url": "http://b:4000", "private_ok": True},
    ]))
    g = _auth_off_gateway(monkeypatch)
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
    path = "/v1/chat/completions"        # the catch-all → the LLM pool branch
    # T-1 (HYG round): a REAL yarl.URL — the credentialed-route gates read
    # rel_url.path_safe / .query_string, the values actually forwarded.
    rel_url = URL("/v1/chat/completions", encoded=True)
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


# ── stale-connection retry: exact exception type matters ────────────────────
# Regression guard for a real production bug: the retry was originally written
# to catch ServerDisconnectedError, but the actual exception aiohttp 3.14
# raises for "Cannot write to closing transport" is ClientConnectionResetError
# — a sibling class, not a subclass. The retry never fired in production
# despite two merged, released PRs claiming to fix it (verified live: zero
# "retrying once" log lines across a 24h window with 15 occurrences of the
# error it was meant to catch). This test exercises the actual exception type,
# not a stand-in, so a future regression back to the wrong class fails here
# instead of silently shipping again.

class _ResetOnceThenBoomSession:
    """First .request() call raises the real stale-connection exception
    (proving the retry path engages); second call raises a plain ClientError
    (proving it gives up after exactly one retry, not infinitely). Takes the
    exception classes as constructor args rather than importing them at
    module scope, so it always exercises whatever hive_mind_proxy actually
    imported — not a copy that could drift from it."""
    closed = False
    def __init__(self, reset_exc, client_exc):
        self.calls = 0
        self._reset_exc = reset_exc
        self._client_exc = client_exc
    def request(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            raise self._reset_exc("Cannot write to closing transport")
        raise self._client_exc("still down")


def test_retries_once_on_client_connection_reset(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    session = _ResetOnceThenBoomSession(g.ClientConnectionResetError, g.ClientError)
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert session.calls == 2                    # retried exactly once, not zero, not looped
    assert resp.status == 503                     # then failed normally, like before the fix
    assert g._llm_inflight["http://a:5000"] == 0   # slot still released, not leaked


def test_embed_body_buffered_under_cap_is_retry_eligible(monkeypatch):
    """Embeddings/reranking requests get the same protection as LLM traffic
    when their body is small enough to buffer (the gap PR #145 was meant to
    close) — verified via the same real exception type as the LLM-path test."""
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    class _EmbedReq:
        method = "POST"
        path = "/v1/embeddings"           # its own registered route -> handle_encoder
        rel_url = URL("/v1/embeddings", encoded=True)
        headers = {}
        can_read_body = True
        content_length = 40               # small, well under EMBED_RERANK_BUFFER_CAP
        async def read(self):
            return b'{"input":"hello","model":"bge-m3"}'

    proxy = g.AsyncHiveMindProxy()
    session = _ResetOnceThenBoomSession(g.ClientConnectionResetError, g.ClientError)
    proxy.session = session
    # R-A (HYG round): the encoder path is served by its OWN handler now, so
    # the retry pin follows it there — driving it through handle_proxy would
    # exercise the LLM pool, not the embedder.
    resp = asyncio.run(proxy.handle_encoder(_EmbedReq()))

    assert session.calls == 2             # the embeddings leg also got the retry
    assert resp.status == 503
