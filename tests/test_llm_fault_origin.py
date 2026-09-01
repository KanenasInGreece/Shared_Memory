"""Credential_Custody_Plan_2026-08-14, PR A3 — client-facing fault signalling
and credential-fault recording on the proxy path (hive_mind_proxy.handle_proxy).

Coverage:
  1. X-SM-Fault-Origin: upstream (a proxied backend itself returned the fault
     status, body passed through verbatim) vs gateway (the gateway
     constructed the error response) — never set on success.
  2. The passthrough contract: an upstream error body/status survive the
     proxy byte-for-byte (a stated contract, per the brief — this is the
     regression test for it).
  3. record_llm_upstream_fault / record_llm_gateway_fault actually fire from
     the real proxy code paths, not just in isolation (test_credential_audit_
     trail.py covers the recorder functions themselves).
  4. request["backend"] / request["key_attached"] are stashed for the
     gateway's own per-request audit line to read back.
  5. None of this ever requires a full aiohttp Request/transport — response
     writer methods are patched the same way a bare-object test double
     would need them to be, since no existing test in this repo drives a
     real prepare()/write() cycle either.

See tests/test_llm_backend_secrets.py for the sibling "no secret leaks"
suite this complements (that file proves credentials never leak; this one
proves the recording/signalling built on top of the same call sites).
"""
import asyncio
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from aiohttp import web  # noqa: E402


# ── test doubles ──────────────────────────────────────────────────────────────

class _FakeReq:
    """Plain stand-in mirroring the ones in test_llm_backend_secrets.py /
    test_pool_status.py — no mapping interface, proving the request["backend"]
    stash is truly best-effort and never breaks a caller that lacks one."""
    method = "POST"
    path = "/v1/chat/completions"        # not in ROUTING_MAP -> the LLM pool branch
    rel_url = "/v1/chat/completions"
    headers = {}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model"}'


class _DictReq(dict):
    """A request double that DOES support item assignment / .get(), modelling
    what auth_middleware hands handle_proxy in the real app (a real aiohttp
    Request is a MutableMapping) — used to verify the backend/key_attached/
    request_id stash actually lands somewhere a reader could retrieve it."""
    method = "POST"
    path = "/v1/chat/completions"
    rel_url = "/v1/chat/completions"
    headers = {}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model"}'


class _OneShotAsyncIter:
    """Minimal stand-in for aiohttp's StreamReader.iter_any() — yields the
    whole body as one chunk, matching how a small JSON error body actually
    arrives from a real LLM API in practice (never split mid-object)."""
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
    """A successful CONNECTION at the aiohttp session level, but the upstream
    RESPONSE itself carries a fault status + body — models a real 401/429
    from an LLM provider, as opposed to a connection-level failure (which
    _FailSession-style classes elsewhere in this repo already cover)."""
    closed = False

    def __init__(self, status, body: bytes, headers=None):
        self._status = status
        self._body = body
        self._headers = headers or {"Content-Type": "application/json"}

    def request(self, *a, **kw):
        return _StatusBodyCM(self._status, self._body, self._headers)


class _FailSession:
    """Raises the given exception before any response exists — models an
    unreachable/erroring backend at the connection level (mirrors the
    identically-named class in test_llm_backend_secrets.py)."""
    closed = False

    def __init__(self, exc):
        self._exc = exc

    def request(self, *a, **kw):
        raise self._exc


def _patch_stream_response(monkeypatch):
    """StreamResponse.prepare()/write()/write_eof() need a real transport
    that none of this repo's lightweight request doubles provide (confirmed:
    no existing test drives this path either — every existing proxy test
    aborts before prepare() by raising inside .request()). Patched at the
    class level so handle_proxy's own internally-constructed StreamResponse
    instances are captured without needing dependency injection."""
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


@pytest.fixture(autouse=True)
def _isolated_fault_counters():
    """coordinator._llm_fault_counters / _credential_counters are process-
    lifetime module globals mutated directly by the recorder functions these
    tests exercise through the real proxy code path — clear them before and
    after every test so one test's faults never leak into the next (mirrors
    tests/test_token_registry_digests_and_daemon_fd.py's
    _isolated_agent_tokens_registry fixture).

    Also unconditionally disarms coordinator._credential_audit_writer at
    teardown (security review R-1 fix round): several tests here reload the
    SHARED coordinator singleton with CREDENTIAL_AUDIT_LOG_PATH pointed at a
    tmp_path to inspect a real file write, and — unlike test_credential_
    audit_trail.py's independently-loaded modules — that reload mutates the
    ONE coordinator module every other test in this session also imports.
    Setting the writer to None here (never re-arming it against any path,
    real or not) is a stronger guarantee than restoring "whatever it was
    before", and doesn't depend on any individual test remembering its own
    cleanup."""
    import coordinator
    coordinator._llm_fault_counters.clear()
    coordinator._credential_counters["token_verify_failed"] = 0
    coordinator._credential_counters["daemon_tokens_issued"] = 0
    yield
    coordinator._llm_fault_counters.clear()
    coordinator._credential_counters["token_verify_failed"] = 0
    coordinator._credential_counters["daemon_tokens_issued"] = 0
    coordinator._credential_audit_writer = None


def _credentialed_backend(monkeypatch, url="http://a:5000", token_var="SM_TEST_TOKEN"):
    """Configure a single LLM backend WITH a provider key attached, via
    LLM_BACKENDS_JSON/token_env — never a literal secret in config.

    "private_ok": true (Model_Atributes_Routing_Plan_2026-08-18, M-5): a
    credentialed backend with neither `roles` nor an explicit `private_ok`
    now REFUSES STARTUP — this file's tests are about the credential/fault-
    classification/route-allowlist mechanics on ROLE-LESS traffic reaching a
    credentialed backend, not about the M-5 startup refusal itself (that has
    its own coverage in tests/test_model_attributes_routing.py), so every
    call site here makes the M-5 choice explicitly to keep testing what it
    was already testing."""
    monkeypatch.setenv(token_var, "sk-test-credential")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": url, "token_env": token_var, "private_ok": True}]))


# ── 1 + 2. X-SM-Fault-Origin header + verbatim passthrough ──────────────────

def test_upstream_fault_gets_upstream_origin_header_and_verbatim_body(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    body = b'{"error":{"message":"bad key","type":"invalid_request_error","code":"invalid_api_key"}}'
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(401, body)
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 401
    assert b"".join(written["chunks"]) == body, (
        "the upstream error body must pass through byte-for-byte — this is "
        "the stated passthrough contract"
    )
    assert written["headers"]["X-SM-Fault-Origin"] == "upstream"


def test_successful_response_never_gets_fault_origin_header(monkeypatch):
    """MUTATION TARGET: the header must be additive on faults only — a 200
    must never carry it."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, b'{"choices":[]}')
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 200
    assert "X-SM-Fault-Origin" not in written["headers"]


def test_embedding_route_fault_also_gets_upstream_origin_header(monkeypatch):
    """The header is not LLM-pool-only — any proxied backend's fault status
    (embeddings/reranking included) is upstream-origin too."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    class _EmbedReq:
        method = "POST"
        path = "/v1/embeddings"           # IS in ROUTING_MAP -> not the LLM branch
        rel_url = "/v1/embeddings"
        headers = {}
        can_read_body = True
        content_length = 40

        async def read(self):
            return b'{"input":"hello","model":"bge-m3"}'

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(500, b'{"error":"embedder down"}')
    resp = asyncio.run(proxy.handle_proxy(_EmbedReq()))

    assert resp.status == 500
    assert written["headers"]["X-SM-Fault-Origin"] == "upstream"


def test_gateway_origin_error_gets_gateway_fault_origin_header(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(g.ClientError("connection refused"))
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 503
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"


def test_gateway_timeout_gets_gateway_fault_origin_header(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(asyncio.TimeoutError())
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 504
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"


def test_gateway_generic_exception_gets_gateway_fault_origin_header(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(RuntimeError("boom"))
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 500
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"


# ── 3. record_llm_upstream_fault / record_llm_gateway_fault fire for real ───

def test_credentialed_401_records_upstream_credential_fault(monkeypatch, tmp_path):
    log_path = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(log_path))
    _credentialed_backend(monkeypatch)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    body = b'{"error":{"code":"invalid_api_key","type":"invalid_request_error"}}'
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(401, body)
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 401
    entry = coordinator._llm_fault_counters["http://a:5000"]["llm"]["credential"]
    assert entry["count"] == 1
    assert entry["last"]["error_type"] == "invalid_api_key"
    asyncio.run(coordinator._credential_audit_writer.flush())
    line = json.loads(log_path.read_text().strip())
    assert line["event"] == "upstream_credential_fault"
    assert line["backend"] == "http://a:5000"
    # cleanup: the autouse fixture disarms coordinator._credential_audit_writer
    # at teardown regardless — no manual delenv+reload needed (and a manual
    # reload here would itself re-arm the writer against the real default
    # path for whatever runs between here and teardown; see R-1 fix round).


def test_uncredentialed_401_still_counted_but_not_logged(monkeypatch, tmp_path):
    """MUTATION TARGET: telemetry counts every backend; the high-signal log
    line is credentialed-calls-only."""
    log_path = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(log_path))
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))   # no token_env -> not credentialed
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(401, b'{"error":{"code":"invalid_api_key"}}')
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert coordinator._llm_fault_counters["http://a:5000"]["llm"]["credential"]["count"] == 1
    asyncio.run(coordinator._credential_audit_writer.flush())
    assert not log_path.exists(), "an uncredentialed call's fault must not reach the credential-events log"


def test_gateway_connect_failure_on_credentialed_call_records_gateway_fault(monkeypatch, tmp_path):
    log_path = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(log_path))
    _credentialed_backend(monkeypatch)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(g.ClientError("connection refused"))
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    entry = coordinator._llm_fault_counters["http://a:5000"]["gateway"]
    assert entry["count"] == 1
    assert entry["last"]["class"] == "ClientError"
    asyncio.run(coordinator._credential_audit_writer.flush())
    line = json.loads(log_path.read_text().strip())
    assert line["event"] == "gateway_fault"


def test_empty_body_fault_response_still_classified(monkeypatch):
    """An empty-bodied 401 (no chunks at all from iter_any()) must still be
    recorded — the fallback classification after the streaming loop."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import coordinator
    import hive_mind_proxy as g
    importlib.reload(g)
    _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(403, b"")   # empty body -> iter_any yields nothing
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 403
    assert coordinator._llm_fault_counters["http://a:5000"]["llm"]["credential"]["count"] == 1


# ── 4. request["backend"] / request["key_attached"] stash ───────────────────

def test_backend_and_key_attached_stashed_when_credentialed(monkeypatch):
    _credentialed_backend(monkeypatch)
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(RuntimeError("stop before any real call"))
    req = _DictReq()
    asyncio.run(proxy.handle_proxy(req))

    assert req.get("backend") == "http://a:5000"
    assert req.get("key_attached") is True


def test_key_attached_not_stashed_when_backend_has_no_token(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(RuntimeError("stop before any real call"))
    req = _DictReq()
    asyncio.run(proxy.handle_proxy(req))

    assert req.get("backend") == "http://a:5000"
    assert req.get("key_attached") is None


def test_backend_stash_is_best_effort_on_a_mapping_less_request(monkeypatch):
    """MUTATION TARGET: a request double with no __setitem__/.get() (every
    other proxy test's _FakeReq) must not crash handle_proxy — the stash is
    wrapped in try/except precisely so it never breaks a caller."""
    _credentialed_backend(monkeypatch)
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(RuntimeError("stop before any real call"))
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))   # _FakeReq has no __setitem__
    assert resp.status == 500   # reached the generic-exception branch, did not raise TypeError


# ── 5. O-1: the X-SM- namespace + WWW-Authenticate are gateway-owned on the
#           RESPONSE direction — a hostile/misconfigured upstream cannot
#           spoof either on a passthrough response ──────────────────────────

def test_upstream_success_cannot_spoof_fault_origin_header(monkeypatch):
    """⚑ Security review O-1, empirically confirmed by the reviewer's own
    probe: an upstream 200 carrying X-SM-Fault-Origin: gateway reached the
    client with the header intact, because the gateway only ever ASSIGNS
    the header on a fault status — on success nothing overwrites an
    upstream-supplied value. MUTATION TARGET: drop strip_gateway_namespace
    from the response-direction _filter_headers call and this fails."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(200, b'{"choices":[]}', headers={
        "Content-Type": "application/json",
        "X-SM-Fault-Origin": "gateway",
        "X-SM-LLM-Backend": "http://attacker-controlled:9999",
        "WWW-Authenticate": 'Bearer realm="provider"',
    })
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 200
    assert "X-SM-Fault-Origin" not in written["headers"]
    assert "WWW-Authenticate" not in written["headers"]
    # The gateway's OWN assignment (observability, not spoofable via this
    # path since it's set unconditionally right after the strip) still wins:
    assert written["headers"]["X-SM-LLM-Backend"] == "http://a:5000"


def test_upstream_fault_cannot_spoof_a_different_backend_label(monkeypatch):
    """Same property on the fault path — the gateway's own
    X-SM-Fault-Origin: upstream assignment must not be pre-empted by
    whatever the upstream itself sent under that name."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(401, b'{"error":{"code":"x"}}', headers={
        "Content-Type": "application/json",
        "X-SM-Fault-Origin": "gateway",  # upstream tries to claim it's the gateway
    })
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert written["headers"]["X-SM-Fault-Origin"] == "upstream"


def test_request_direction_headers_unaffected_by_gateway_namespace_strip(monkeypatch):
    """The stricter filtering is RESPONSE-direction only — a client sending
    an X-SM-* header upstream (unusual, but must not silently vanish and
    break some future legitimate use) is unaffected."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    filtered = proxy._filter_headers({"X-SM-Custom": "client-value", "Content-Type": "application/json"})
    assert filtered["X-SM-Custom"] == "client-value"


# ── 6. O-2: the classification call must never truncate the passthrough ─────

def test_classification_exception_never_truncates_the_passthrough(monkeypatch):
    """⚑ Security review O-2, mutation-checked in the review itself: make
    the recorder raise and confirm the body still arrives byte-for-byte.
    MUTATION TARGET: remove the try/except around either
    record_llm_upstream_fault call site and this fails with a truncated
    body (today: only the in-loop site is exercised here, since a body-
    bearing response always hits that site, never the fallback)."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    monkeypatch.setattr(g, "record_llm_upstream_fault",
                         lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("recorder boom")))
    written = _patch_stream_response(monkeypatch)

    body = b'{"error":{"code":"invalid_api_key"}}'
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(401, body)
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 401
    assert b"".join(written["chunks"]) == body, (
        "a raising recorder must never truncate the passthrough — "
        "classification is best-effort, the response is not"
    )


def test_classification_exception_on_empty_body_fallback_never_breaks_the_response(monkeypatch):
    """Same property for the FALLBACK call site (empty-bodied fault)."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    monkeypatch.setattr(g, "record_llm_upstream_fault",
                         lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("recorder boom")))
    _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(403, b"")
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 403  # did not raise out to the generic exception handler


# ── 7. R-3: compressed error bodies still classify correctly end to end ─────

def test_gzip_compressed_429_body_classifies_as_credential_end_to_end(monkeypatch):
    """⚑ Security review R-3, end to end through the real proxy path
    (test_credential_audit_trail.py covers the decompression helper in
    isolation): a gzip-compressed insufficient_quota body on a 429, with
    Content-Encoding: gzip on the upstream response, must still classify as
    "credential" — the exact case the reviewer's probe showed silently
    degrading to "transient" (auto_decompress=False means the peek would
    otherwise see gzip framing bytes, not JSON). MUTATION TARGET: remove the
    _decompress_prefix_for_parse call in handle_proxy and this fails."""
    import gzip
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import coordinator
    import hive_mind_proxy as g
    importlib.reload(g)
    _patch_stream_response(monkeypatch)

    body = json.dumps({"error": {"code": "insufficient_quota"}}).encode()
    compressed = gzip.compress(body)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(429, compressed, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
    })
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 429
    entry = coordinator._llm_fault_counters["http://a:5000"]["llm"]
    assert entry["credential"]["count"] == 1
    assert entry["transient"]["count"] == 0


def test_gzip_compressed_body_passthrough_stays_compressed_bytes(monkeypatch):
    """The decompression is for CLASSIFICATION only — the client must still
    receive the original compressed bytes, unchanged, with the original
    Content-Encoding header (the client, not the gateway, decompresses)."""
    import gzip
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    body = json.dumps({"error": {"code": "insufficient_quota"}}).encode()
    compressed = gzip.compress(body)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _StatusBodySession(429, compressed, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
    })
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert b"".join(written["chunks"]) == compressed
    assert written["headers"]["Content-Encoding"] == "gzip"


# ── 8. O-6: proxy error bodies/logs never echo raw exception text ───────────

class _InvalidURLLikeSession:
    """Raises a ClientError whose __str__ renders a full URL carrying a
    credential in userinfo AND in a query parameter — mirrors what aiohttp's
    real InvalidURL renders for a malformed/credentialed configured URL."""
    closed = False

    def __init__(self, exc_cls, credentialed_url: str):
        self._exc_cls = exc_cls
        self._url = credentialed_url

    def request(self, *a, **kw):
        raise self._exc_cls(f"Invalid URL: {self._url}")


def test_client_error_body_never_echoes_exception_text(monkeypatch):
    """O-6: the client-visible body uses the exception's CLASS NAME only."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    credentialed_url = "https://user:sk-provider-secret-abc123@evil.example.com/v1/x?key=sk-query-secret-xyz789"
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _InvalidURLLikeSession(g.ClientError, credentialed_url)
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 503
    body = json.loads(resp.body.decode())
    assert body["error"] == "Backend unreachable: ClientError"
    assert "sk-provider-secret-abc123" not in body["error"]
    assert "sk-query-secret-xyz789" not in body["error"]


def test_client_error_log_line_scrubs_url_credentials(monkeypatch, caplog):
    """O-6: the gateway LOG line (not the client body) still carries some
    diagnostic text, but userinfo/query are stripped from any URL in it —
    ⚑ security review scenario: a credentialed LLM_BACKENDS_JSON URL must
    never reach the journal via an exception's rendered text. MUTATION
    TARGET: remove the _scrub_url_credentials wrapping and this fails."""
    import logging
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    credentialed_url = "https://user:sk-provider-secret-abc123@evil.example.com/v1/x?key=sk-query-secret-xyz789"
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _InvalidURLLikeSession(g.ClientError, credentialed_url)
    with caplog.at_level(logging.ERROR):
        asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert "sk-provider-secret-abc123" not in caplog.text
    assert "sk-query-secret-xyz789" not in caplog.text
    assert "evil.example.com" in caplog.text  # host survives — still useful for debugging


def test_generic_exception_body_and_log_never_echo_text(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    credentialed_url = "https://user:sk-provider-secret-abc123@evil.example.com/v1/x?key=sk-query-secret-xyz789"
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _InvalidURLLikeSession(RuntimeError, credentialed_url)
    with caplog.at_level(logging.ERROR):
        resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 500
    body = json.loads(resp.body.decode())
    assert body["error"] == "Proxy error: RuntimeError"
    assert "sk-provider-secret-abc123" not in body["error"]
    assert "sk-provider-secret-abc123" not in caplog.text
    assert "sk-query-secret-xyz789" not in caplog.text


def test_scrub_url_credentials_strips_userinfo_and_query():
    monkeypatch_free_url = "https://user:sk-secret@example.com:8443/v1/path?key=sk-other-secret&x=1"
    import hive_mind_proxy as g
    scrubbed = g._scrub_url_credentials(monkeypatch_free_url)
    assert "sk-secret" not in scrubbed
    assert "sk-other-secret" not in scrubbed
    assert "example.com" in scrubbed
    assert "8443" in scrubbed


def test_scrub_url_credentials_leaves_non_url_text_alone():
    import hive_mind_proxy as g
    assert g._scrub_url_credentials("plain connection refused, no url here") == \
        "plain connection refused, no url here"
