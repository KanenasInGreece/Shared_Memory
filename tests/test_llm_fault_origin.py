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
    _isolated_agent_tokens_registry fixture)."""
    import coordinator
    coordinator._llm_fault_counters.clear()
    coordinator._credential_counters["token_verify_failed"] = 0
    coordinator._credential_counters["daemon_tokens_issued"] = 0
    yield
    coordinator._llm_fault_counters.clear()
    coordinator._credential_counters["token_verify_failed"] = 0
    coordinator._credential_counters["daemon_tokens_issued"] = 0


def _credentialed_backend(monkeypatch, url="http://a:5000", token_var="SM_TEST_TOKEN"):
    """Configure a single LLM backend WITH a provider key attached, via
    LLM_BACKENDS_JSON/token_env — never a literal secret in config."""
    monkeypatch.setenv(token_var, "sk-test-credential")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": url, "token_env": token_var}]))


# ── 1 + 2. X-SM-Fault-Origin header + verbatim passthrough ──────────────────

def test_upstream_fault_gets_upstream_origin_header_and_verbatim_body(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
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
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
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
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
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
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(g.ClientError("connection refused"))
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 503
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"


def test_gateway_timeout_gets_gateway_fault_origin_header(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(asyncio.TimeoutError())
    resp = asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert resp.status == 504
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"


def test_gateway_generic_exception_gets_gateway_fault_origin_header(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
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
    monkeypatch.delenv("CREDENTIAL_AUDIT_LOG_PATH", raising=False)
    importlib.reload(coordinator)


def test_uncredentialed_401_still_counted_but_not_logged(monkeypatch, tmp_path):
    """MUTATION TARGET: telemetry counts every backend; the high-signal log
    line is credentialed-calls-only."""
    log_path = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(log_path))
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")   # no token_env -> not credentialed
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
    monkeypatch.delenv("CREDENTIAL_AUDIT_LOG_PATH", raising=False)
    importlib.reload(coordinator)


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
    monkeypatch.delenv("CREDENTIAL_AUDIT_LOG_PATH", raising=False)
    importlib.reload(coordinator)


def test_empty_body_fault_response_still_classified(monkeypatch):
    """An empty-bodied 401 (no chunks at all from iter_any()) must still be
    recorded — the fallback classification after the streaming loop."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
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
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
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
