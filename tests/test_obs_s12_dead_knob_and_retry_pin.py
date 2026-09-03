"""OBS S12 — delete the dead LLM_MAX_TRIES knob; pin the property its name
obscured (per ruling R-B).

THE DEFECT
----------
`LLM_MAX_TRIES` was read by nothing but the `/memory/telemetry` config
render — no retry loop ever consulted it. Two comments (the pool-selection
block near the top of the file, and the constant's own declaration) promised
cross-backend failover ("requests retry on the next-best backend") that this
pool never implemented: the real retry is SAME-TARGET only, gated on the
request body being buffered (`have_buffered_body`/`max_attempts` in the
dispatch loop), and fires on exactly one class of connection-level race — a
pooled connection reused just as the backend started closing it. A genuine
failure 503s straight to the caller; it is never silently re-routed to a
different backend. `tests/test_llm_backend_secrets.py:55/79` (ADV2's own
citation) never reach this retry loop at all — both fixtures raise from
`.request()` itself, before any `async with` context is even entered, so
they prove the NO-LEAK property on a single attempt and say nothing about
what a SECOND attempt would carry.

THE FIX
-------
1. `LLM_MAX_TRIES` and its `/memory/telemetry` render are deleted; both
   comments rewritten to describe reality (this file's own diff).
2. Contract bookkeeping (telemetry_contract.py, R-B): both `max_tries`
   entries (health + telemetry) are DELETED outright and recorded in a new
   `REMOVED_IN_0_9_88` tuple, copying `REMOVED_IN_0_9_74`'s
   declaration/consumption/rendering shape exactly — never stamped-and-kept,
   never moved to CONDITIONAL (`required_paths()` ignores `removed_in`
   entirely, so a documented-but-unemitted key would fail
   `test_every_documented_health_key_is_emitted`/
   `test_every_documented_telemetry_key_is_emitted`).
3. THIS FILE pins the property the dead knob's name obscured: a REAL retry
   exercise. The first attempt raises `ClientConnectionResetError` before any
   response exists (`proxy_resp is None` — the legitimate first-attempt-only
   retry at `hive_mind_proxy.py`'s per-attempt except clause); the second
   attempt succeeds. Both attempts must hit the SAME url with a SINGLE
   `Authorization` header carrying the PROVIDER key, never the client's own
   gateway bearer.

`tests/test_llm_fault_origin.py:274`/`:373` (`test_gateway_origin_error_gets_
gateway_fault_origin_header` / the connect-failure-on-credentialed-call
tests, using `_FailSession` which raises synchronously from `.request()`
itself, before any `async with` block) are UNTOUCHED by this file — they pin
a genuinely different event (a connect failure with NO retry-eligible
exception class involved) and must keep doing exactly that.
"""
import asyncio
import importlib
import json
import os
import sys

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


# ═══════════════════════════════════════════════════════════════════════════
# 1 — the dead knob is actually gone
# ═══════════════════════════════════════════════════════════════════════════

def test_llm_max_tries_constant_is_deleted(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_MAX_TRIES", raising=False)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert not hasattr(g, "LLM_MAX_TRIES")


def test_config_snapshot_no_longer_renders_max_tries(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    import hive_mind_proxy as g
    importlib.reload(g)
    cfg = g._config_snapshot()
    assert "max_tries" not in cfg["llm_pool_tuning"]
    assert set(cfg["llm_pool_tuning"]) == {"fail_threshold", "fail_window_s", "cooldown_s"}


# ═══════════════════════════════════════════════════════════════════════════
# 2 — contract bookkeeping: deleted + recorded per R-B
# ═══════════════════════════════════════════════════════════════════════════

def _tc():
    import telemetry_contract as tc
    return tc


def test_max_tries_deleted_from_both_contract_dicts():
    tc = _tc()
    assert "config.llm_pool_tuning.max_tries" not in tc.HEALTH
    assert "config.llm_pool_tuning.max_tries" not in tc.TELEMETRY


def test_removed_in_0_9_88_records_both_paths_same_shape_as_0_9_74():
    tc = _tc()
    assert hasattr(tc, "REMOVED_IN_0_9_88")
    entries = tc.REMOVED_IN_0_9_88
    assert isinstance(entries, tuple)
    paths = {(e["endpoint"], e["path"]) for e in entries}
    assert paths == {
        ("health", "config.llm_pool_tuning.max_tries"),
        ("telemetry", "config.llm_pool_tuning.max_tries"),
    }
    # Same shape as REMOVED_IN_0_9_74 — every entry is a 3-key dict.
    for e in entries:
        assert set(e) == {"endpoint", "path", "reason"}
        assert isinstance(e["reason"], str) and e["reason"]


def test_removed_in_0_9_88_renders_into_the_generated_doc():
    tc = _tc()
    doc = tc.render_markdown()
    assert "## Removed outright in 0.9.88" in doc
    assert "config.llm_pool_tuning.max_tries" in doc
    # Both removal sections coexist — 0.9.74's is not clobbered.
    assert "## Removed outright in 0.9.74" in doc


def test_required_paths_never_demand_the_removed_key():
    """`required_paths()` ignores `removed_in` entirely (R-B's own warning) —
    the only way a removed key stays out of the "every documented key is
    emitted" check is for it to be gone from the dict outright, which the
    prior test already pins; this test pins the CONSEQUENCE directly."""
    tc = _tc()
    assert "config.llm_pool_tuning.max_tries" not in tc.required_paths(tc.HEALTH, "health")
    assert "config.llm_pool_tuning.max_tries" not in tc.required_paths(tc.TELEMETRY, "telemetry")


# ═══════════════════════════════════════════════════════════════════════════
# 3 — the REAL property: same-target retry, single provider-key Authorization
# ═══════════════════════════════════════════════════════════════════════════

class _RaisingCtx:
    """The FIRST attempt: `__aenter__` raises before any upstream response
    exists — `proxy_resp` is still None in `handle_proxy` at this point, the
    legitimate first-attempt-only retry condition."""

    async def __aenter__(self):
        raise ClientConnectionResetError("Cannot write to closing transport")

    async def __aexit__(self, *exc):
        return False


class _OneShotAsyncIter:
    def __init__(self, body: bytes):
        self._body = body

    def iter_any(self):
        return self._agen()

    async def _agen(self):
        if self._body:
            yield self._body


class _SucceedingResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self.content = _OneShotAsyncIter(body)


class _SucceedingCtx:
    def __init__(self, status: int, body: bytes):
        self._resp = _SucceedingResp(status, body)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _ResetOnceThenSucceedSession:
    """First `.request()` call returns a context whose `__aenter__` raises
    `ClientConnectionResetError` with NO response ever read; the second call
    succeeds. Records the url + a plain-dict copy of headers for every
    attempt so the test can assert Authorization identity ACROSS the retry —
    this is the whole point ADV2 found missing."""
    closed = False

    def __init__(self, status: int = 200, body: bytes = b'{"ok":true}'):
        self.calls: list[dict] = []
        self._status = status
        self._body = body

    def request(self, *, method, url, headers, data, allow_redirects):
        self.calls.append({"url": url, "headers": dict(headers)})
        if len(self.calls) == 1:
            return _RaisingCtx()
        return _SucceedingCtx(self._status, self._body)


def _patch_stream_response(monkeypatch):
    """No real transport exists in this test — patch StreamResponse's I/O
    methods to no-ops so the (successful) second attempt's prepare()/write()/
    write_eof() calls succeed without one. Mirrors
    tests/test_llm_fault_origin.py's identically-named helper."""
    async def fake_prepare(self, request):
        return None

    async def fake_write(self, data):
        return None

    async def fake_write_eof(self, data=b""):
        return None

    monkeypatch.setattr(web.StreamResponse, "prepare", fake_prepare)
    monkeypatch.setattr(web.StreamResponse, "write", fake_write)
    monkeypatch.setattr(web.StreamResponse, "write_eof", fake_write_eof)


class _Req:
    method = "POST"
    path = "/v1/chat/completions"        # not in ROUTING_MAP -> the LLM pool branch
    rel_url = "/v1/chat/completions"
    headers = {"Authorization": "Bearer client-gateway-token"}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model"}'


def test_first_attempt_reset_retries_same_target_single_provider_authorization(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("SM_TEST_PROVIDER_KEY", "sk-provider-key")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "token_env": "SM_TEST_PROVIDER_KEY", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)
    _patch_stream_response(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    session = _ResetOnceThenSucceedSession()
    proxy.session = session

    resp = asyncio.run(proxy.handle_proxy(_Req()))

    assert resp.status == 200
    assert len(session.calls) == 2, (
        "expected exactly one retry after the first-attempt reset with no "
        "response ever read")
    urls = {c["url"] for c in session.calls}
    assert len(urls) == 1, (
        "the retry must hit the SAME backend — this pool has no cross-"
        "backend failover, dead knob or not")
    assert next(iter(urls)).startswith("http://a:5000"), urls
    for call in session.calls:
        keys_lower = [k.lower() for k in call["headers"]]
        assert keys_lower.count("authorization") == 1, (
            "exactly one Authorization header per attempt")
        assert call["headers"].get("Authorization") == "Bearer sk-provider-key", (
            "the PROVIDER key, never the client's own gateway bearer")
