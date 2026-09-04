"""Group-3 telemetry fixes found by live debugging on a single-cloud-backend
fleet (the VRAM-constrained configuration our own docs recommend, exercised
live against api.deepseek.com):

  1. `capture_usage` required `not content_encoding` — any gzip-compressed
     upstream (measured live: api.deepseek.com sets Content-Encoding: gzip)
     silently skipped token accounting. The cost meter read 0 tokens on
     every billable call to that backend.
  2. `if len(LLM_BACKENDS) > 1:` hid llm_backends/llm_pool/llm_reserved/
     llm_affinity from /health for exactly the single-backend (cloud-only)
     configuration our docs steer VRAM-constrained operators toward — the
     gate's original "single backend = legacy default" reading inverted.
  3. Nothing recorded per-backend LLM request latency, so a local-vs-online
     backend comparison had no data — new llm_latency /health section.

Request/session/StreamResponse test doubles mirror the patterns established
in tests/test_llm_fault_origin.py (usage/latency path) and
tests/test_model_attributes_routing.py (_build_health_checks path)."""
import asyncio
import gzip
import importlib
import json
import os
import sys
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from aiohttp import web  # noqa: E402


# ── shared test doubles ──────────────────────────────────────────────────────

class _FakeReq:
    """Mirrors test_llm_fault_origin.py's _FakeReq — a plain stand-in with no
    mapping interface, path not in ROUTING_MAP so handle_proxy takes the LLM
    pool branch and sets llm_backend."""
    method = "POST"
    path = "/v1/chat/completions"
    rel_url = URL("/v1/chat/completions", encoded=True)
    headers = {}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model"}'


class _StreamReq:
    """Same as _FakeReq but the CLIENT's own request body sets stream:true —
    exercises the pre-existing SSE skip, which must survive the gzip fix
    untouched."""
    method = "POST"
    path = "/v1/chat/completions"
    rel_url = URL("/v1/chat/completions", encoded=True)
    headers = {}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model","stream":true}'


class _EmbedReq:
    """Mirrors test_llm_fault_origin.py's _EmbedReq — its own registered route,
    served by handle_encoder, so llm_backend stays None throughout: the
    non-pool-route case."""
    method = "POST"
    path = "/v1/embeddings"
    rel_url = URL("/v1/embeddings", encoded=True)
    headers = {}
    can_read_body = True
    content_length = 40

    async def read(self):
        return b'{"input":"hello","model":"bge-m3"}'


class _OneShotAsyncIter:
    """Minimal stand-in for aiohttp's StreamReader.iter_any() — yields the
    whole body as one chunk."""
    def __init__(self, body: bytes):
        self._body = body

    def iter_any(self):
        return self._agen()

    async def _agen(self):
        if self._body:
            yield self._body


class _StatusBodyResp:
    def __init__(self, status, body, headers):
        self.status = status
        self.headers = headers
        self.content = _OneShotAsyncIter(body)


class _StatusBodyCM:
    def __init__(self, status, body, headers):
        self._resp = _StatusBodyResp(status, body, headers)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _StatusBodySession:
    """A successful CONNECTION whose RESPONSE carries the given status/body/
    headers — models a real proxied LLM response."""
    closed = False

    def __init__(self, status, body: bytes, headers=None):
        self._status = status
        self._body = body
        self._headers = headers or {"Content-Type": "application/json"}

    def request(self, *a, **kw):
        return _StatusBodyCM(self._status, self._body, self._headers)


class _FailSession:
    """Raises before any response exists — a connection-level failure."""
    closed = False

    def __init__(self, exc):
        self._exc = exc

    def request(self, *a, **kw):
        raise self._exc


class _HealthProbeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FixedStatusSession:
    """session.get() double for _build_health_checks' liveness probes
    (embedder/reranker/each LLM backend) — mirrors test_model_attributes_
    routing.py's _FixedStatusSession."""
    closed = False

    def __init__(self, status=200):
        self._status = status

    def get(self, url, timeout=None, headers=None, **_kw):
        return _HealthProbeResp(self._status)


def _patch_stream_response(monkeypatch):
    """Mirrors test_llm_fault_origin.py's _patch_stream_response — no
    existing lightweight request double drives a real prepare()/write()
    cycle, so StreamResponse itself is patched at the class level."""
    written = {"headers": None, "status": None, "chunks": [], "eof": False}

    async def fake_prepare(self, request):
        written["headers"] = dict(self.headers)
        written["status"] = self.status
        return None

    async def fake_write(self, data):
        written["chunks"].append(data)

    async def fake_write_eof(self, data=b""):
        written["eof"] = True

    monkeypatch.setattr(web.StreamResponse, "prepare", fake_prepare)
    monkeypatch.setattr(web.StreamResponse, "write", fake_write)
    monkeypatch.setattr(web.StreamResponse, "write_eof", fake_write_eof)
    return written


def _fresh(monkeypatch, backends="http://a:5000"):
    """Auth-off single-backend reload (mirrors test_model_attributes_routing.
    py's _fresh / test_pool_status.py's _auth_off_gateway). Every module-
    level counter dict (_llm_tokens_*, _llm_requests_*, _llm_latency_*) is
    re-created fresh by the reload, so no manual reset is needed between
    tests."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    # W4 default-deny: this fixture is about telemetry counters, not privacy
    # — declare every backend with an explicit private_ok opt-in so role-less
    # traffic still reaches it (mirrors test_llm_affinity.py's fixture).
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": u.strip(), "private_ok": True} for u in backends.split(",") if u.strip()]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    return g


def _usage_body(prompt=100, completion=50):
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }).encode()


# ── Change 1: gzip usage capture (the cost-meter defect) ────────────────────

def test_gzip_usage_capture_end_to_end(monkeypatch):
    """MUTATION TARGET: revert capture_usage's encoding gate back to
    `not content_encoding` and this fails — a gzip-compressed, successful
    LLM response (measured live: api.deepseek.com sets Content-Encoding:
    gzip) must still update the per-backend token counters instead of
    silently reading 0."""
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)

    body = _usage_body(prompt=123, completion=45)
    compressed = gzip.compress(body)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, compressed, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
    })
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 200
    assert g._llm_tokens_prompt_total["http://a:5000"] == 123
    assert g._llm_tokens_completion_total["http://a:5000"] == 45
    assert g._llm_tokens_last_ts["http://a:5000"] is not None


def test_identity_uncompressed_usage_capture_still_works(monkeypatch):
    """Regression: the pre-existing uncompressed path must be unaffected by
    the encoding-gate change."""
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)

    body = _usage_body(prompt=10, completion=5)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, body)
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 200
    assert g._llm_tokens_prompt_total["http://a:5000"] == 10
    assert g._llm_tokens_completion_total["http://a:5000"] == 5


def test_gzip_passthrough_stays_compressed_bytes(monkeypatch):
    """The decompression is for the internal usage PARSE only — the client
    must still receive the original compressed bytes, unchanged."""
    g = _fresh(monkeypatch)
    written = _patch_stream_response(monkeypatch)

    compressed = gzip.compress(_usage_body())
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, compressed, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
    })
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert b"".join(written["chunks"]) == compressed
    assert written["headers"]["Content-Encoding"] == "gzip"


def test_over_cap_compressed_body_abandons_capture_without_error(monkeypatch):
    """LLM_USAGE_CAPTURE_CAP_BYTES bounds the COMPRESSED bytes accumulated —
    a compressed body over the cap must abandon capture silently, exactly
    like today's parse failure: never raise, never break the proxy path."""
    monkeypatch.setenv("LLM_USAGE_CAPTURE_CAP_BYTES", "8")
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)

    compressed = gzip.compress(_usage_body(prompt=999, completion=999))
    assert len(compressed) > 8
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, compressed, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
    })
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 200
    assert g._llm_tokens_prompt_total["http://a:5000"] == 0
    assert g._llm_tokens_completion_total["http://a:5000"] == 0
    assert g._llm_tokens_last_ts["http://a:5000"] is None


def test_sse_stream_still_skipped_for_gzip(monkeypatch):
    """stream:true is an SSE response — the accumulated bytes never parse as
    a single JSON object, so capture must be skipped up front for a
    compressed response exactly as for an uncompressed one."""
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)

    compressed = gzip.compress(_usage_body())
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, compressed, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
    })
    resp = asyncio.run(proxy.handle_proxy(_StreamReq()))

    assert resp.status == 200
    assert g._llm_tokens_prompt_total["http://a:5000"] == 0
    assert g._llm_tokens_last_ts["http://a:5000"] is None


def test_decompress_bomb_abandons_capture_without_error(monkeypatch):
    """Security fix round (Opus Required finding): LLM_USAGE_CAPTURE_CAP_BYTES
    bounds only the COMPRESSED accumulation — a small compressed body can
    inflate ~1000× (measured 1028:1 on this box). LLM_USAGE_DECOMPRESS_CAP_BYTES
    must bound the inflation itself: over it, capture is abandoned silently
    (counters stay 0) and the request is untouched.

    MUTATION TARGET: drop the max_length/len(out) bound in
    _decompress_full_for_usage and this fails — the bomb below carries a
    valid trailing usage object, so an unbounded decompress would happily
    record its tokens."""
    monkeypatch.setenv("LLM_USAGE_DECOMPRESS_CAP_BYTES", "4096")
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)

    bomb = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "a" * 200_000}}],
        "usage": {"prompt_tokens": 777, "completion_tokens": 888},
    }).encode()
    compressed = gzip.compress(bomb)
    assert len(compressed) < 4096 < len(bomb)  # under the compressed cap, over the decompress cap
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, compressed, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
    })
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 200
    assert g._llm_tokens_prompt_total["http://a:5000"] == 0
    assert g._llm_tokens_completion_total["http://a:5000"] == 0
    assert g._llm_tokens_last_ts["http://a:5000"] is None


def test_decompress_full_raises_over_cap_and_returns_under_cap(monkeypatch):
    """Unit pin on the helper itself: an over-cap gzip OR deflate body raises
    (never returns partial/compressed bytes — the caller's contract is
    fail-clean-at-the-point-of-trouble), an under-cap body round-trips."""
    monkeypatch.setenv("LLM_USAGE_DECOMPRESS_CAP_BYTES", "1024")
    import coordinator
    importlib.reload(coordinator)
    import zlib
    import pytest

    small = b'{"usage":{"prompt_tokens":1}}'
    assert coordinator._decompress_full_for_usage(gzip.compress(small), "gzip") == small
    assert coordinator._decompress_full_for_usage(zlib.compress(small), "deflate") == small

    big = b"x" * 100_000
    with pytest.raises(ValueError):
        coordinator._decompress_full_for_usage(gzip.compress(big), "gzip")
    with pytest.raises(ValueError):
        coordinator._decompress_full_for_usage(zlib.compress(big), "deflate")


# ── Change 2: single-backend visibility on /health ───────────────────────────

def test_single_backend_health_sections_present(monkeypatch):
    """MUTATION TARGET: restore `if len(LLM_BACKENDS) > 1:` and this fails —
    a single-backend (cloud-only) fleet must show llm_backends/llm_pool/
    llm_affinity in /health, not only multi-backend fleets."""
    g = _fresh(monkeypatch, backends="http://a:5000")
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(200)

    checks = asyncio.run(g._build_health_checks(proxy, None))

    assert checks.get("llm_backends") == {"http://a:5000": "ok"}
    assert "llm_pool" in checks and "http://a:5000" in checks["llm_pool"]
    assert "llm_affinity" in checks


def test_zero_backends_health_sections_absent(monkeypatch):
    """The gate's negative case. An empty LLM_BACKENDS is not reachable
    through real config (the loader always falls back to DEFAULT_TARGET —
    see hive_mind_proxy._load_llm_backends), so it's set directly on the
    already-loaded module to exercise the gate itself, matching the
    pre-fix `len(LLM_BACKENDS) > 1` behaviour for the truly-empty case."""
    g = _fresh(monkeypatch, backends="http://a:5000")
    monkeypatch.setattr(g, "LLM_BACKENDS", [])
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(200)

    checks = asyncio.run(g._build_health_checks(proxy, None))

    assert "llm_backends" not in checks
    assert "llm_pool" not in checks
    assert "llm_reserved" not in checks
    assert "llm_affinity" not in checks
    assert "llm_token_usage" not in checks
    assert "llm_latency" not in checks


# ── Change 3: per-backend LLM request latency (new instrument) ──────────────

def test_latency_counters_ok_and_failing_request(monkeypatch):
    """Two routed requests, one ok (200) and one failing (500 — a fault
    STATUS still returns via the same success return in handle_proxy, body
    passed through verbatim): requests_total counts both, requests_failed_
    total counts only the failing one, sum/max/ts all populate."""
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)
    b = "http://a:5000"

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, _usage_body())
    resp1 = asyncio.run(proxy.handle_proxy(_FakeReq()))
    assert resp1.status == 200

    proxy.session = _StatusBodySession(500, b'{"error":"boom"}')
    resp2 = asyncio.run(proxy.handle_proxy(_FakeReq()))
    assert resp2.status == 500

    assert g._llm_requests_total[b] == 2
    assert g._llm_requests_failed_total[b] == 1
    assert g._llm_latency_sum_s[b] > 0
    assert g._llm_latency_max_s[b] > 0
    assert g._llm_latency_last_ts[b] is not None


def test_latency_counters_on_gateway_connection_failure(monkeypatch):
    """A connection-level failure (upstream unreachable — never even
    produces an upstream.status) must still record as a failed request: the
    finally block runs on every exit path, not only the 'upstream
    answered' one."""
    g = _fresh(monkeypatch)
    b = "http://a:5000"
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(g.ClientError("connection refused"))

    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))
    assert resp.status == 503

    assert g._llm_requests_total[b] == 1
    assert g._llm_requests_failed_total[b] == 1
    assert g._llm_latency_sum_s[b] >= 0
    assert g._llm_latency_last_ts[b] is not None


def test_non_pool_route_records_no_latency(monkeypatch):
    """Embeddings/reranking never set llm_backend — the latency instrument
    must stay entirely untouched for them (requests_total for the one
    configured LLM backend stays 0, since no LLM-pool request was made).

    R-A (HYG round): driven through handle_ENCODER. The prefix loop that used
    to route /v1/embeddings inside handle_proxy is GONE — the path is its own
    registered route now, and an embed request sent to handle_proxy would go
    down the LLM-POOL path instead, which is exactly the case this test
    asserts cannot happen."""
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)
    b = "http://a:5000"

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, b'{"result":"ok"}')
    resp = asyncio.run(proxy.handle_encoder(_EmbedReq()))

    assert resp.status == 200
    assert g._llm_requests_total[b] == 0
    assert g._llm_requests_failed_total[b] == 0
    assert g._llm_latency_sum_s[b] == 0.0
    assert g._llm_latency_last_ts[b] is None


def test_llm_latency_health_section_shape(monkeypatch):
    """The /health surface itself: flat, keyed by backend URL like
    llm_token_usage, with the five documented fields."""
    g = _fresh(monkeypatch)
    _patch_stream_response(monkeypatch)
    b = "http://a:5000"

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, _usage_body())
    asyncio.run(proxy.handle_proxy(_FakeReq()))
    proxy.session = _FixedStatusSession(200)

    checks = asyncio.run(g._build_health_checks(proxy, None))

    assert set(checks["llm_latency"][b]) == {
        "requests_total", "requests_failed_total",
        "latency_sum_s", "latency_max_s", "latency_last_ts",
    }
    assert checks["llm_latency"][b]["requests_total"] == 1
    assert checks["llm_latency"][b]["requests_failed_total"] == 0
