"""SEC B (SEC round, second wall behind A) — every client-facing surface in
hive_mind_proxy.py that renders a backend-URL string or dict key must pass
through the fixed (item C) scrub_url_credentials before it reaches a
response, a log line, or the gateway audit JSONL.

Item A (fatal on userinfo) makes it impossible for a NORMAL ingest path to
ever populate the pool with a credentialed URL — so several tests here seed
one directly into the internal pool STRUCTURES (bypassing A entirely), per
the brief's own test guidance for item B: "seed a credentialed URL directly
into the pool structures (monkeypatch, bypassing A)". A query-string
credential (R-2, NOT refused by A) is used where the test can go through
the normal, legitimate ingest path instead.

Byte-identity claims (item C's guarantee) are pinned with a mixed-case host
and a bracketed IPv6 host, both loaded through the NORMAL ingest path (they
carry no credential, so A never touches them).
"""
import asyncio
import importlib
import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from aiohttp import web  # noqa: E402

SECRET = "sm-b-render-scrub-secret-4d21"


# ── 1. _scrub_backend_keyed_dict — the shared helper, in isolation ─────────

def _load():
    import hive_mind_proxy as g
    return g


def test_scrub_backend_keyed_dict_clean_is_byte_identical(monkeypatch):
    g = _load()
    src = {"http://MyHost:8000": {"a": 1}, "http://[fd00::1]:9": {"b": 2}}
    out = g._scrub_backend_keyed_dict(src, context="test")
    assert out == src
    assert set(out.keys()) == set(src.keys())


def test_scrub_backend_keyed_dict_scrubs_credentialed_keys(monkeypatch):
    g = _load()
    src = {f"https://leakuser:{SECRET}@backend.example.test:9443/v1": {"a": 1}}
    out = g._scrub_backend_keyed_dict(src, context="test")
    assert list(out.keys()) == ["https://backend.example.test:9443/v1"]
    assert SECRET not in json.dumps(out)


def test_scrub_backend_keyed_dict_collapse_guard_keeps_raw_and_logs(monkeypatch, caplog):
    g = _load()
    src = {
        f"https://u1:{SECRET}@backend.example.test/v1": {"a": 1},
        f"https://u2:{SECRET}@backend.example.test/v1": {"b": 2},
    }
    with caplog.at_level(logging.ERROR, logger="hive-proxy"):
        out = g._scrub_backend_keyed_dict(src, context="collapse-guard-test")
    # Both entries survive (raw, unscrubbed) — nothing silently lost.
    assert out == src
    assert len(out) == 2
    assert any("collapsed" in r.getMessage() for r in caplog.records)


# ── 2. _llm_runtime_snapshot — the single builder for backends/reserved/
#      pool/affinity/routing/token_usage/latency ───────────────────────────

def _clean_two_backend_gateway(monkeypatch):
    """Mixed-case + IPv6 hosts, both clean (no credential) — loads through
    the normal ingest path unaffected by item A."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://MyHost:8000,http://[fd00::1]:9443")
    g = _load()
    importlib.reload(g)
    return g


def test_llm_runtime_snapshot_clean_mixed_case_and_ipv6_byte_identical(monkeypatch):
    g = _clean_two_backend_gateway(monkeypatch)
    rt = g._llm_runtime_snapshot({b: "ok" for b in g.LLM_BACKENDS})
    assert set(rt["pool"].keys()) == {"http://MyHost:8000", "http://[fd00::1]:9443"}
    assert set(rt["token_usage"].keys()) == {"http://MyHost:8000", "http://[fd00::1]:9443"}
    assert set(rt["latency"].keys()) == {"http://MyHost:8000", "http://[fd00::1]:9443"}
    assert set(rt["backends"].keys()) == {"http://MyHost:8000", "http://[fd00::1]:9443"}


def _seed_credentialed_backend(g, url):
    """Bypass item A entirely: inject `url` directly into every pool
    structure a render might read, exactly as the brief's own SEC-B test
    guidance directs ('seed a credentialed URL directly into the pool
    structures, monkeypatching, bypassing A')."""
    g.LLM_BACKENDS.append(url)
    g.LLM_POOL.append(url)
    g.LLM_WEIGHTS[url] = 1.0
    g._llm_inflight[url] = 0
    g._llm_inflight_started[url] = []


def test_llm_runtime_snapshot_scrubs_a_directly_seeded_credentialed_backend(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _load()
    importlib.reload(g)
    cred_url = f"https://leakuser:{SECRET}@backend.example.test/v1"
    _seed_credentialed_backend(g, cred_url)

    rt = g._llm_runtime_snapshot({cred_url: "ok"})
    dumped = json.dumps(rt)
    assert SECRET not in dumped
    assert "backend.example.test" in dumped
    assert "https://backend.example.test/v1" in rt["pool"]
    assert "https://backend.example.test/v1" in rt["token_usage"]
    assert "https://backend.example.test/v1" in rt["latency"]
    assert "https://backend.example.test/v1" in rt["backends"]


def test_llm_runtime_snapshot_reserved_list_is_scrubbed(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _load()
    importlib.reload(g)
    cred_url = f"https://leakuser:{SECRET}@backend.example.test/v1"
    _seed_credentialed_backend(g, cred_url)
    g._llm_reserved.add(cred_url)

    rt = g._llm_runtime_snapshot({})
    assert SECRET not in json.dumps(rt["reserved"])
    assert "https://backend.example.test/v1" in rt["reserved"]


def test_llm_runtime_snapshot_hot_prefix_backend_field_is_scrubbed(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _load()
    importlib.reload(g)
    cred_url = f"https://leakuser:{SECRET}@backend.example.test/v1"
    _seed_credentialed_backend(g, cred_url)
    g._llm_affinity["deadbeef12345678"] = [cred_url, g.time.monotonic(), 3]

    rt = g._llm_runtime_snapshot({})
    dumped = json.dumps(rt["affinity"])
    assert SECRET not in dumped
    assert rt["affinity"]["hot_prefixes"]["deadbeef"]["backend"] == "https://backend.example.test/v1"


# ── 3. /pool/status ──────────────────────────────────────────────────────

def _auth_off(monkeypatch, g):
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False


def test_pool_status_clean_mixed_case_and_ipv6_byte_identical(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://MyHost:8000,http://[fd00::1]:9443")
    g = _load()
    _auth_off(monkeypatch, g)
    d = json.loads(asyncio.run(g.handle_pool_status(None)).body)
    assert set(d["backends"].keys()) == {"http://MyHost:8000", "http://[fd00::1]:9443"}


def test_pool_status_scrubs_a_directly_seeded_credentialed_backend(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _load()
    _auth_off(monkeypatch, g)
    cred_url = f"https://leakuser:{SECRET}@backend.example.test/v1"
    _seed_credentialed_backend(g, cred_url)

    resp = asyncio.run(g.handle_pool_status(None))
    raw = resp.body.decode()
    assert SECRET not in raw
    d = json.loads(raw)
    assert "https://backend.example.test/v1" in d["backends"]


def test_pool_status_key_collapse_guard(monkeypatch, caplog):
    """Two distinct credentialed backends that scrub to the SAME key must
    not silently merge — ADV1-14."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _load()
    _auth_off(monkeypatch, g)
    url1 = f"https://u1:{SECRET}@backend.example.test/v1"
    url2 = f"https://u2:{SECRET}@backend.example.test/v1"
    _seed_credentialed_backend(g, url1)
    _seed_credentialed_backend(g, url2)

    with caplog.at_level(logging.ERROR, logger="hive-proxy"):
        resp = asyncio.run(g.handle_pool_status(None))
    d = json.loads(resp.body)
    # Collapse guard: BOTH raw (unscrubbed) keys survive rather than one
    # silently overwriting the other.
    assert url1 in d["backends"]
    assert url2 in d["backends"]
    assert any("collapsed" in r.getMessage() for r in caplog.records)


# ── 4. _config_snapshot ─────────────────────────────────────────────────

def test_config_snapshot_clean_mixed_case_byte_identical(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://MyHost:8000")
    g = _load()
    importlib.reload(g)
    cfg = g._config_snapshot()
    assert cfg["llm_backends"][0]["url"] == "http://MyHost:8000"


def test_config_snapshot_scrubs_a_directly_seeded_credentialed_backend(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _load()
    importlib.reload(g)
    cred_url = f"https://leakuser:{SECRET}@backend.example.test/v1"
    _seed_credentialed_backend(g, cred_url)
    g.LLM_BACKEND_TOKENS[cred_url] = None
    g.LLM_BACKEND_MODELS[cred_url] = None
    g.LLM_BACKEND_ROLES[cred_url] = None
    g.LLM_BACKEND_NCTX[cred_url] = None
    g.LLM_BACKEND_PRIVATE_OK[cred_url] = True
    g.LLM_BACKEND_MAX_INFLIGHT[cred_url] = None
    g.LLM_BACKEND_PRICE_IN[cred_url] = None
    g.LLM_BACKEND_PRICE_OUT[cred_url] = None

    cfg = g._config_snapshot()
    urls = [e["url"] for e in cfg["llm_backends"]]
    assert SECRET not in json.dumps(cfg)
    assert "https://backend.example.test/v1" in urls


# ── 5. X-SM-LLM-Backend response header (handle_proxy) ──────────────────

class _FakeReq:
    method = "POST"
    path = "/v1/chat/completions"
    rel_url = "/v1/chat/completions"
    headers = {}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model"}'


class _OneShotAsyncIter:
    """Mirrors tests/test_llm_fault_origin.py's helper of the same name —
    yields the whole body as one chunk."""
    def __init__(self, body: bytes):
        self._body = body

    def iter_any(self):
        return self._agen()

    async def _agen(self):
        if self._body:
            yield self._body


class _OkResp:
    def __init__(self, body: bytes):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self.content = _OneShotAsyncIter(body)


class _OkCm:
    def __init__(self, body: bytes):
        self._resp = _OkResp(body)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _OkSession:
    closed = False

    def request(self, *a, **kw):
        return _OkCm(b'{"choices":[]}')


def _patch_stream_response(monkeypatch):
    written = {"headers": None, "status": None}

    async def fake_prepare(self, request):
        written["headers"] = dict(self.headers)
        written["status"] = self.status
        return None

    async def fake_write(self, data):
        pass

    async def fake_write_eof(self, data=b""):
        pass

    monkeypatch.setattr(web.StreamResponse, "prepare", fake_prepare)
    monkeypatch.setattr(web.StreamResponse, "write", fake_write)
    monkeypatch.setattr(web.StreamResponse, "write_eof", fake_write_eof)
    return written


def test_x_sm_llm_backend_header_clean_mixed_case_byte_identical(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://MyHost:8000", "private_ok": True}]))
    g = _load()
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert written["headers"]["X-SM-LLM-Backend"] == "http://MyHost:8000"


def test_x_sm_llm_backend_header_clean_ipv6_byte_identical(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://[fd00::1]:9443", "private_ok": True}]))
    g = _load()
    importlib.reload(g)
    written = _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    assert written["headers"]["X-SM-LLM-Backend"] == "http://[fd00::1]:9443"


def test_x_sm_llm_backend_header_scrubs_a_directly_seeded_credentialed_backend(monkeypatch):
    """Bypasses item A: the sole backend is injected directly into the pool
    structures rather than through LLM_BACKENDS/LLM_BACKENDS_JSON, so it is
    the only candidate handle_proxy's selection can pick."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://placeholder-unused:1")
    g = _load()
    importlib.reload(g)
    cred_url = f"https://leakuser:{SECRET}@backend.example.test/v1"
    g.LLM_BACKENDS[:] = [cred_url]
    g.LLM_POOL[:] = [cred_url]
    g.LLM_WEIGHTS.clear()
    g.LLM_WEIGHTS[cred_url] = 1.0
    g.LLM_BACKEND_TOKENS.clear()
    g.LLM_BACKEND_TOKENS[cred_url] = None
    g.LLM_BACKEND_MODELS.clear()
    g.LLM_BACKEND_MODELS[cred_url] = None
    g.LLM_BACKEND_EXTRAS.clear()
    g.LLM_BACKEND_EXTRAS[cred_url] = None
    g.LLM_BACKEND_ROLES.clear()
    g.LLM_BACKEND_ROLES[cred_url] = None
    g.LLM_BACKEND_NCTX.clear()
    g.LLM_BACKEND_NCTX[cred_url] = None
    g.LLM_BACKEND_PRIVATE_OK.clear()
    g.LLM_BACKEND_PRIVATE_OK[cred_url] = True
    g.LLM_BACKEND_PRIVATE_OK_EXPLICIT.clear()
    g.LLM_BACKEND_PRIVATE_OK_EXPLICIT[cred_url] = True
    g.LLM_BACKEND_MAX_INFLIGHT.clear()
    g.LLM_BACKEND_MAX_INFLIGHT[cred_url] = None
    g.LLM_BACKEND_PRICE_IN.clear()
    g.LLM_BACKEND_PRICE_IN[cred_url] = None
    g.LLM_BACKEND_PRICE_OUT.clear()
    g.LLM_BACKEND_PRICE_OUT[cred_url] = None
    g._llm_inflight.clear()
    g._llm_inflight[cred_url] = 0
    g._llm_inflight_started.clear()
    g._llm_inflight_started[cred_url] = []

    written = _patch_stream_response(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()
    asyncio.run(proxy.handle_proxy(_FakeReq()))

    header = written["headers"].get("X-SM-LLM-Backend")
    assert header is not None
    assert SECRET not in header
    assert header == "https://backend.example.test/v1"


# ── 6. _emit_token_lifecycle_sums ────────────────────────────────────────

def test_emit_token_lifecycle_sums_scrubs_query_credential(monkeypatch, tmp_path, caplog):
    """R-2: a query-string credential loads through the NORMAL ingest path
    (not refused by item A) — the lifecycle-sum journal line and the
    gateway audit JSONL must both render it query-less."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    query_url = f"http://a:5000?key={SECRET}"
    monkeypatch.setenv("LLM_BACKENDS", query_url)
    audit_path = tmp_path / "gateway-audit.jsonl"
    monkeypatch.setenv("GATEWAY_AUDIT_LOG_PATH", str(audit_path))
    g = _load()
    importlib.reload(g)
    assert query_url in g.LLM_BACKENDS

    g._llm_tokens_prompt_total[query_url] = 10
    g._llm_tokens_completion_total[query_url] = 5

    with caplog.at_level(logging.INFO, logger="hive-proxy"):
        g._emit_token_lifecycle_sums("test")

    assert SECRET not in caplog.text
    assert "http://a:5000" in caplog.text

    audit_text = audit_path.read_text()
    assert SECRET not in audit_text
    line = json.loads(audit_text.strip().splitlines()[0])
    assert line["backend"] == "http://a:5000"


# ── 7. telemetry_extras()'s faults (coordinator._llm_faults_snapshot) ────

def test_telemetry_extras_faults_scrubs_a_directly_seeded_credentialed_backend(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import coordinator
    importlib.reload(coordinator)
    g = _load()
    importlib.reload(g)

    cred_url = f"https://leakuser:{SECRET}@backend.example.test/v1"
    coordinator._fault_entry(cred_url)["llm"]["transient"]["count"] += 1

    extras = g.telemetry_extras()
    dumped = json.dumps(extras["llm"]["faults"])
    assert SECRET not in dumped
    assert "https://backend.example.test/v1" in extras["llm"]["faults"]

    # cleanup — this module-global dict outlives the reload
    coordinator._llm_fault_counters.clear()


@pytest.fixture(autouse=True)
def _restore_module(monkeypatch):
    """Same convention as tests/test_gateway_startup_journal_scrub.py's
    _restore_module: reloading hive_mind_proxy/coordinator rebinds shared
    module objects process-wide — restore a clean-env reload after every
    test so a later test file does not inherit this file's env or seeded
    pool state."""
    yield
    for k in ("EMBEDDER_URL", "RERANKER_URL", "LLM_BACKENDS_JSON", "LLM_BACKENDS",
              "AGENT_TOKENS", "GATEWAY_AUDIT_LOG_PATH"):
        monkeypatch.delenv(k, raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    coordinator._llm_fault_counters.clear()
    importlib.reload(coordinator)
    importlib.reload(importlib.import_module("hive_mind_proxy"))
