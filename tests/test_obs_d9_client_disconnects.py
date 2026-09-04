"""OBS D9 — the `prepare()` window charged client aborts to healthy backends;
a mid-stream abort charged them the opposite way. Both are the CALLER's
event, never a verdict on the backend.

THE DEFECT (verified against `hive_mind_proxy.py`'s dispatch loop)
--------------------------------------------------------------------
1. `await proxy_resp.prepare(request)` sits OUTSIDE any except scoped to a
   client-side disconnect. If the downstream client's transport is already
   closing, aiohttp's own web-writer (`http_writer.StreamWriter._write`,
   shared by both the client session and the web response writer) raises
   `aiohttp.client_exceptions.ClientConnectionResetError` — which IS-A
   builtin `ConnectionResetError` (checked via its MRO). Before this fix that
   exception fell through, uncaught, to the per-attempt
   `except (ClientConnectionResetError, ServerDisconnectedError)` clause
   meant for the UPSTREAM connection-reuse race: `proxy_resp is not None`
   (already constructed), so no retry, and it re-raises into the outer
   `except ClientError` handler — an "Upstream unreachable" ERROR log,
   `_llm_mark_fail` (2 aborts = a 300s cooldown on a perfectly healthy card),
   a recorded gateway fault, and a failed-latency sample, for an event the
   backend never saw at all.
2. The MID-STREAM abort handler (`except (ConnectionResetError, IOError)`
   around the chunk-forwarding loop) does NOT return — it falls through to
   `_llm_mark_ok`, clearing the backend's fail streak on a partial write
   that was never actually "served".

THE FIX
-------
A narrow except scoped to `prepare()` ONLY, catching `(ConnectionResetError,
IOError)` — the same classes the write path already catches. On an abort in
EITHER window: `client_disconnects_total += 1` (via
`coordinator.record_llm_client_disconnect()`), a WARNING log naming no
backend fault, NEITHER `_llm_mark_fail` NOR `_llm_mark_ok`, no gateway fault
recorded, and `_record_llm_latency` skipped entirely (an abort's duration is
not a service time — accepted survivor-bias cost: this hides "clients hang
up because the backend is slow" from the latency ring; no second ring is
added to recover it).

PROVE-FAILING-FIRST (recorded in the commit body): both abort tests below
were run against a REVERTED copy of the fix (the narrow prepare() try/except
removed; the mid-stream `_llm_mark_ok` guard removed) and failed — the
prepare abort produced `_llm_fail_total == 1` + an "Upstream unreachable"
ERROR log, and the mid-stream abort cleared `_llm_fail_times` — exactly the
two defects this file exists to catch.
"""
import asyncio
import importlib
import json
import logging
import os
import sys
import time

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError, ServerDisconnectedError
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


# ── test doubles (mirrors tests/test_llm_fault_origin.py's shapes) ──────────

class _Req:
    method = "POST"
    path = "/v1/chat/completions"        # not in ROUTING_MAP -> the LLM pool branch
    # T-1 (HYG round): a REAL yarl.URL — the credentialed-route gates read
    # rel_url.raw_path / .query_string, the values actually forwarded.
    rel_url = URL("/v1/chat/completions", encoded=True)
    headers = {}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model"}'


class _OneShotAsyncIter:
    """Yields a body then optionally raises — models a clean success (no
    raise) or a mid-stream client abort (raise after the first chunk)."""
    def __init__(self, chunks, raise_after: Exception | None = None):
        self._chunks = chunks
        self._raise_after = raise_after

    def iter_any(self):
        return self._agen()

    async def _agen(self):
        for chunk in self._chunks:
            yield chunk
        if self._raise_after is not None:
            raise self._raise_after


class _UpstreamResp:
    def __init__(self, status, content):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self.content = content


class _UpstreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _OneShotSession:
    """A single successful CONNECTION at the aiohttp session level — the
    content iterator decides whether the "stream" completes cleanly or
    raises a client-abort mid-way."""
    closed = False

    def __init__(self, content):
        self._content = content

    def request(self, *a, **kw):
        return _UpstreamCtx(_UpstreamResp(200, self._content))


def _patch_stream_response(monkeypatch, *, prepare_raises: Exception | None = None):
    """Patches web.StreamResponse's I/O so no real transport is needed.
    `prepare_raises`, when given, makes `prepare()` raise it instead of
    succeeding — simulating a client disconnect BEFORE headers can be sent
    (no real socket can be made to do this deterministically in a unit
    test, so this is the same class of test double
    tests/test_llm_fault_origin.py already relies on for this exact I/O)."""
    async def fake_prepare(self, request):
        if prepare_raises is not None:
            raise prepare_raises
        return None

    async def fake_write(self, data):
        return None

    async def fake_write_eof(self, data=b""):
        return None

    monkeypatch.setattr(web.StreamResponse, "prepare", fake_prepare)
    monkeypatch.setattr(web.StreamResponse, "write", fake_write)
    monkeypatch.setattr(web.StreamResponse, "write_eof", fake_write_eof)


def _reload(monkeypatch, backend="http://a:5000"):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": backend, "private_ok": True}]))
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return coordinator, g


BACKEND = "http://a:5000"


# ═══════════════════════════════════════════════════════════════════════════
# 1 — abort AT prepare(): no backend fault, counted as a client disconnect
# ═══════════════════════════════════════════════════════════════════════════

def test_abort_at_prepare_no_backend_fault_no_upstream_unreachable_log(monkeypatch, caplog):
    coordinator, g = _reload(monkeypatch)
    _patch_stream_response(monkeypatch,
                            prepare_raises=ClientConnectionResetError("Cannot write to closing transport"))

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OneShotSession(_OneShotAsyncIter([b'{"ok":true}']))
    before_disc = coordinator._gateway_client_disconnects_total

    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        asyncio.run(proxy.handle_proxy(_Req()))

    assert g._llm_fail_total[BACKEND] == 0
    assert g._llm_requests_failed_total[BACKEND] == 0
    assert BACKEND not in g._llm_unhealthy_until or g._llm_unhealthy_until[BACKEND] == 0.0
    assert coordinator._gateway_client_disconnects_total == before_disc + 1
    assert not any("Upstream unreachable" in r.message for r in caplog.records)
    assert any("disconnect" in r.message.lower() for r in caplog.records)


def test_abort_at_prepare_latency_max_unmoved(monkeypatch):
    coordinator, g = _reload(monkeypatch)
    _patch_stream_response(monkeypatch,
                            prepare_raises=ClientConnectionResetError("Cannot write to closing transport"))
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OneShotSession(_OneShotAsyncIter([b'{"ok":true}']))

    before_latency_max = g._llm_latency_max_s[BACKEND]
    asyncio.run(proxy.handle_proxy(_Req()))
    assert g._llm_latency_max_s[BACKEND] == before_latency_max


# ═══════════════════════════════════════════════════════════════════════════
# 2 — abort MID-STREAM: neither ok nor fail; fail-streak preserved
# ═══════════════════════════════════════════════════════════════════════════

def test_abort_mid_stream_no_mark_ok_fail_streak_preserved(monkeypatch):
    coordinator, g = _reload(monkeypatch)
    _patch_stream_response(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OneShotSession(_OneShotAsyncIter(
        [b'{"partial":'], raise_after=ConnectionResetError("client hung up mid-stream")))

    # Pre-seed an existing fail streak — _llm_mark_ok (if wrongly called)
    # clears this to []; the fix must leave it untouched.
    seeded = [time.monotonic()]
    g._llm_fail_times[BACKEND] = list(seeded)
    before_disc = coordinator._gateway_client_disconnects_total

    asyncio.run(proxy.handle_proxy(_Req()))

    assert coordinator._gateway_client_disconnects_total == before_disc + 1
    assert len(g._llm_fail_times[BACKEND]) == 1, (
        "_llm_mark_ok must NOT have cleared the pre-seeded fail streak")
    assert g._llm_fail_total[BACKEND] == 0, "not a fail either — neither verdict fires"


def test_abort_mid_stream_latency_max_unmoved(monkeypatch):
    coordinator, g = _reload(monkeypatch)
    _patch_stream_response(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OneShotSession(_OneShotAsyncIter(
        [b'{"partial":'], raise_after=ConnectionResetError("client hung up mid-stream")))

    before_latency_max = g._llm_latency_max_s[BACKEND]
    asyncio.run(proxy.handle_proxy(_Req()))
    assert g._llm_latency_max_s[BACKEND] == before_latency_max


# ═══════════════════════════════════════════════════════════════════════════
# 3 — regression pins: genuine failures / the legitimate retry survive
# ═══════════════════════════════════════════════════════════════════════════

class _FailSession:
    closed = False

    def __init__(self, exc):
        self._exc = exc

    def request(self, *a, **kw):
        raise self._exc


def test_genuine_connect_failure_still_marks_fail(monkeypatch):
    coordinator, g = _reload(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FailSession(g.ClientError("connection refused"))
    before_disc = coordinator._gateway_client_disconnects_total

    resp = asyncio.run(proxy.handle_proxy(_Req()))

    assert resp.status == 503
    assert g._llm_fail_total[BACKEND] == 1
    assert coordinator._gateway_client_disconnects_total == before_disc, (
        "a genuine connect failure is not a client disconnect")


class _RaisingCtx:
    async def __aenter__(self):
        raise ClientConnectionResetError("Cannot write to closing transport")

    async def __aexit__(self, *exc):
        return False


class _ResetOnceThenSucceedSession:
    """The LEGITIMATE first-attempt-only retry (:2342-2364 in the brief) —
    proxy_resp is still None when this fires (the failure happens entering
    the upstream connection, before any StreamResponse is constructed), so
    it must NOT be counted as a client disconnect: this is an UPSTREAM
    connection-reuse race, structurally indistinguishable in exception CLASS
    from D9's own prepare()-window abort, but distinguished by WHEN it fires
    (before vs. after `proxy_resp` exists)."""
    closed = False

    def __init__(self):
        self.calls = 0

    def request(self, *a, **kw):
        self.calls += 1
        if self.calls == 1:
            return _RaisingCtx()
        return _UpstreamCtx(_UpstreamResp(200, _OneShotAsyncIter([b'{"ok":true}'])))


def test_first_attempt_upstream_reset_retry_not_counted_as_client_disconnect(monkeypatch):
    coordinator, g = _reload(monkeypatch)
    _patch_stream_response(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _ResetOnceThenSucceedSession()
    proxy.session = session
    before_disc = coordinator._gateway_client_disconnects_total

    resp = asyncio.run(proxy.handle_proxy(_Req()))

    assert resp.status == 200
    assert session.calls == 2, "the legitimate retry must still fire"
    assert coordinator._gateway_client_disconnects_total == before_disc, (
        "the pre-existing upstream connection-reuse retry is not a client "
        "disconnect and must not be counted as one")
