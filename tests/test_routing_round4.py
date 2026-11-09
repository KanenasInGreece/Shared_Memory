"""Each test dies when its guard is removed.

A non-JSON 2xx is unparseable unless the media type is text/html. A free
batch fault sends every fact alone next cycle. A busy backend, paid or
local, gives way only to a free self-hosted peer at the same priority.
Odd usage still stores or backlogs connection_lost, and a planted key in
usage or an error label is not echoed.
"""
import asyncio
import gzip
import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from aiohttp import web as aw
from unittest.mock import AsyncMock

from test_reasoning_routing import (
    LOCAL,
    MID,
    PAID,
    _Session,
    _Upstream,
    _backend,
    _body,
    _fresh,
    _down,
    _req,
    _run,
    _stop,
)
from test_backlog_cycle_guard import _Result, _arm_backlog, _fold_daemon

pytestmark = pytest.mark.usefixtures("chat_caller_is_consolidation")

KEY = "sk-planted-key-9f3a"
FOLD_TEXT = "SLOT 245: Distillate one.\nSLOT 267: Distillate two.\nPRINCIPLE: A principle.\n"


def _page(status, body, headers=None):
    up = _Upstream(status, body)
    if headers:
        up.headers.update(headers)
    return up


def _summary(usage):
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "kept"}'}}],
        "usage": usage,
    }


def _jsonl(usage):
    text = '{"idx": 0, "summary": "one"}\n{"idx": 1, "summary": "two"}'
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": text}}],
        "usage": usage,
    }


def _fold_ok(usage):
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": FOLD_TEXT}}],
        "usage": usage,
    }


class _Graph:
    """In-memory records. A processed id drops out of the next fetch."""

    def __init__(self, ids, label):
        self.rows = [
            {"pg_id": i, "rem_passed_over": 0, "labels": [label]} for i in ids
        ]
        self.processed = set()
        self.cleared = []

    def session(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def run(self, query, **params):
        if "rem_processed = false" in query:
            ids = list(params.get("ids") or [])
            self.cleared.append(ids)
            for i in ids:
                self.processed.discard(i)
            return _Result([])
        if "rem_processed = true" in query and params.get("pg_id") is not None:
            self.processed.add(params["pg_id"])
            return _Result([])
        if "NOT n.pg_id IN $blocked" in query:
            blocked = set(params.get("blocked") or [])
            kept = [
                r for r in self.rows
                if r["pg_id"] not in blocked and r["pg_id"] not in self.processed
            ]
            return _Result(kept)
        return _Result([])


class _FakeCur:
    def execute(self, *a, **k): pass
    def fetchone(self): return (1,)
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *a): return False

class _FakeConn:
    def cursor(self): return _FakeCur()
    def close(self): pass

def _rem(monkeypatch, contents, url):
    import rem_loop as rem

    mem = _arm_backlog(monkeypatch)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(rem, "REASONER_URL", url)
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    daemon._last_llm_failure = None
    daemon._last_backlog = None
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    graph = _Graph(list(contents), rem.ONT.fact)
    daemon.driver = graph
    daemon._open_pg_conn = lambda: _FakeConn()
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._filter_applied_in_outbox = AsyncMock(side_effect=lambda ids, *a: list(ids))
    daemon._batch_fetch_content = AsyncMock(return_value={
        pg_id: {"content": text, "kind": rem.KIND_FACT, "created_at": None}
        for pg_id, text in contents.items()
    })
    daemon._bump_rem_pickups = AsyncMock()
    daemon._bump_rem_passed_over = AsyncMock()
    daemon._mark_node_invalid = AsyncMock()
    daemon._fact_is_consistent = AsyncMock(return_value=True)
    daemon._mark_outbox_rem_reviewed = AsyncMock()
    daemon._notify_nrem = AsyncMock()
    monkeypatch.setattr(rem, "_take_shared_backup_lock", lambda c: True)
    monkeypatch.setattr(rem, "pool_roster_state", AsyncMock(return_value="free"))
    monkeypatch.setattr(rem, "_nrem_is_queuing", lambda c: False)
    return daemon, mem, graph


def _arm_fold_store(monkeypatch, daemon):
    import consolidation_loop as cl

    daemon.get_embedding = AsyncMock(return_value=[0.1, 0.2])
    daemon._mark_insight_in_graph = AsyncMock()
    daemon._stored_once = False
    real = daemon._find_fresh_insight_clusters

    async def _once():
        if daemon._stored_once:
            return []
        return await real()

    daemon._find_fresh_insight_clusters = _once

    def _write(*args, **kwargs):
        daemon._stored_once = True
        return 7

    monkeypatch.setattr(cl, "write_insight_summary", _write)
    monkeypatch.setattr(cl, "supersede_covered_summaries", lambda *a, **k: [])
    monkeypatch.setattr(cl, "close_ledger_rows_by_id", lambda *a, **k: 0)


class _UpstreamServer:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.bodies = []
        self.server = None

    async def _handle(self, reader, writer):
        try:
            head, body = await _read_http(reader)
            self.calls += 1
            self.bodies.append(body)
            spec = self.reply(body) if callable(self.reply) else self.reply
            if spec[0] == "raw":
                writer.write(spec[1])
            else:
                status, headers, payload = spec
                writer.write(_http_bytes(status, headers, payload))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


def _http_bytes(status, headers, body):
    reasons = {
        200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found",
        429: "Too Many Requests", 500: "Internal Server Error",
        503: "Service Unavailable",
    }
    hdrs = dict(headers or {})
    if "Content-Length" not in hdrs and status != 204:
        hdrs["Content-Length"] = str(len(body or b""))
    hdrs.setdefault("Connection", "close")
    lines = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
    return (
        f"HTTP/1.1 {status} {reasons.get(status, 'X')}\r\n{lines}\r\n".encode()
        + (body or b"")
    )


async def _read_http(reader):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(65536)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" not in buf:
        return buf, b""
    head, rest = buf.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip() or 0)
    while len(rest) < length:
        chunk = await reader.read(length - len(rest))
        if not chunk:
            break
        rest += chunk
    return head, rest[:length]


class _Gateway:
    def __init__(self, monkeypatch, reply, *, inflight=None):
        self.monkeypatch = monkeypatch
        self.up = _UpstreamServer(reply)
        self.inflight = inflight
        self.proxy = None
        self.runner = None
        self.g = None
        self.url = ""

    async def __aenter__(self):
        port = await self.up.start()
        base = f"http://127.0.0.1:{port}/v1"
        row = _backend(base, model="shared-backend", n_ctx=100000, max_output=4096)
        if self.inflight is not None:
            row["max_inflight"] = self.inflight
        self.g = _fresh(self.monkeypatch, [row])
        self.proxy = self.g.AsyncHiveMindProxy()
        await self.proxy.start_session()
        app = aw.Application()
        app.router.add_post("/v1/chat/completions", self.proxy.handle_proxy)
        self.runner = aw.AppRunner(app)
        await self.runner.setup()
        site = aw.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        gw = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{gw}/v1/chat/completions"
        return self

    async def __aexit__(self, *args):
        if self.proxy is not None:
            await self.proxy.cleanup()
        if self.runner is not None:
            await self.runner.cleanup()
        await self.up.close()


def _long(marker):
    # A test length, not a product limit (decision:3101). Every record is sent.
    return marker + (" z" * 1004)


async def _run_path(monkeypatch, path, reply, cycles=3, store=False):
    async with _Gateway(monkeypatch, reply) as gw:
        if path == "solo":
            daemon, mem, graph = _rem(monkeypatch, {8: _long("SOLOFACT ")}, gw.url)
            for _ in range(cycles):
                await daemon.run_cycle()
            return gw.up.calls, mem, graph.processed
        if path == "batch":
            daemon, mem, graph = _rem(
                monkeypatch, {9: _long("BATCHNINE "), 10: _long("BATCHTEN ")}, gw.url)
            for _ in range(cycles):
                await daemon.run_cycle()
            return gw.up.calls, mem, graph.processed
        daemon, mem, _pg, key = _fold_daemon(monkeypatch, None)
        import consolidation_loop as cl
        monkeypatch.setattr(cl, "REASONER_URL", gw.url)
        monkeypatch.delenv("MOCK_LLM", raising=False)
        if store:
            _arm_fold_store(monkeypatch, daemon)
        for _ in range(cycles):
            await daemon.run_insight_cycle()
        return gw.up.calls, mem, key


def _const(status, headers, body):
    return lambda _body: (status, headers, body)


def _json_const(status, obj):
    raw = json.dumps(obj).encode()
    return _const(status, {"Content-Type": "application/json"}, raw)


# --- R1 -------------------------------------------------------------------

_R1_FREE = ("html", _const(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>maintenance</html>"))

_R1_ROWS = [
    ("plain", _const(200, {"Content-Type": "text/plain"}, b"not json")),
    ("none", _const(200, {}, b"not json")),
    ("empty", _const(200, {}, b"")),
    ("204", _const(204, {}, b"")),
    ("utf8", _const(200, {"Content-Type": "application/json"}, b'{"usage":{"completion_tokens":1},"x":"\xff"}')),
    ("cut", ("raw", b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{"usa')),
    ("sse", _const(200, {"Content-Type": "text/event-stream"}, b'data: {"usage":{"completion_tokens":1}}\n\n')),
]


@pytest.mark.parametrize("path", ["solo", "batch", "fold"])
@pytest.mark.asyncio
async def test_r1_html_queues_with_no_row(monkeypatch, path):
    calls, mem, _extra = await _run_path(monkeypatch, path, _R1_FREE[1])
    assert mem.rows == {}
    # HTML queues. The batch stays one batch, so each cycle is one call.
    assert calls == 3


@pytest.mark.parametrize("path", ["solo", "batch", "fold"])
@pytest.mark.parametrize("name,reply", _R1_ROWS, ids=[row[0] for row in _R1_ROWS])
@pytest.mark.asyncio
async def test_r1_other_non_json_is_one_unparseable(monkeypatch, path, name, reply):
    calls, mem, extra = await _run_path(monkeypatch, path, reply)
    assert calls == 1
    reasons = [row[0] for row in mem.rows.values()]
    assert reasons
    assert set(reasons) == {"unparseable"}
    if path == "fold":
        assert list(mem.rows) == [("fold", extra)]


def test_r1_gateway_html_does_not_try_the_next_backend(monkeypatch):
    page = _page(200, b"<html>maintenance</html>", {"Content-Type": "Text/HTML; charset=utf-8"})
    _g, session, resp = _run(monkeypatch, [page, _stop()])
    assert len(session.calls) == 1
    assert resp.status == 503
    assert _body(resp)["type"] == "queued"


# --- R2 / F4 --------------------------------------------------------------

@pytest.mark.asyncio
async def test_r2_free_batch_fault_solos_the_batchmates_next_cycle(monkeypatch):
    """Fact 8 keeps failing alone. Facts 9 and 10 are stored. No row."""
    import rem_loop as rem

    too_long = json.dumps({
        "error": {"type": "invalid_request_error",
                  "message": "Input is too long for the requested model"},
    }).encode()

    def reply(body):
        if b"FACTEIGHT" in body and (b"FACTNINE" in body or b"FACTTEN" in body):
            return (400, {"Content-Type": "application/json"}, too_long)
        if b"FACTEIGHT" in body:
            return (400, {"Content-Type": "application/json"}, too_long)
        return (200, {"Content-Type": "application/json"}, json.dumps(_summary({"completion_tokens": 2})).encode())

    async with _Gateway(monkeypatch, reply) as gw:
        contents = {
            8: _long("FACTEIGHT "),
            9: _long("FACTNINE "),
            10: _long("FACTTEN "),
        }
        daemon, mem, graph = _rem(monkeypatch, contents, gw.url)
        await daemon.run_cycle()
        assert gw.up.calls == 1
        assert graph.processed == set()
        assert mem.rows == {}
        assert daemon._solo_only == {8, 9, 10}
        await daemon.run_cycle()
        assert graph.processed == {9, 10}
        assert 8 not in graph.processed
        assert mem.rows == {}
        calls_after = gw.up.calls
        await daemon.run_cycle()
        await daemon.run_cycle()
        assert graph.processed == {9, 10}
        assert mem.rows == {}
        assert gw.up.calls == calls_after + 2
        assert all(b"FACTEIGHT" in b and b"FACTNINE" not in b for b in gw.up.bodies[calls_after:])
        assert rem.LLM_FAIL_CLIENT == "client"


# --- R3 -------------------------------------------------------------------

def test_r3_wait_logs_one_line_across_polls(monkeypatch, caplog):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512, max_inflight=1),
    ])
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01
    g._llm_inflight[LOCAL] = 1
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.llm_session = session

    async def _free():
        await asyncio.sleep(0.08)
        g._llm_inflight[LOCAL] = 0

    async def _go():
        task = asyncio.create_task(_free())
        resp = await proxy.handle_proxy(_req())
        await task
        return resp

    with caplog.at_level(logging.WARNING):
        resp = asyncio.run(_go())
    assert resp.status == 200
    assert caplog.text.count("Waiting for model shared-backend: at capacity") == 1


# --- R4 -------------------------------------------------------------------

def _busy_paid(monkeypatch, *, peer_priority=1, cool_peer=False):
    g = _fresh(monkeypatch, [
        _backend(PAID, model="paid-small", n_ctx=4000, max_output=512, priority=1,
                 max_inflight=1, token_env="ROUTE_KEY_B"),
        _backend(LOCAL, model="local-peer", n_ctx=8000, max_output=512, priority=peer_priority),
    ])
    monkeypatch.setenv("ROUTE_KEY_B", "sk-route-b")
    g = __import__("importlib").reload(g)
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01
    g._llm_inflight[PAID] = 1
    if cool_peer:
        import time
        g._llm_unhealthy_until[LOCAL] = time.monotonic() + 60
    return g


def test_r4_busy_paid_uses_a_free_local_peer(monkeypatch):
    g = _busy_paid(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.llm_session = session
    resp = asyncio.run(asyncio.wait_for(proxy.handle_proxy(_req()), 0.4))
    assert resp.status == 200
    assert len(session.calls) == 1
    assert "9101" in str(session.calls[0]["url"])
    assert "example.test" not in str(session.calls[0]["url"])


def test_r4_cooled_peer_is_not_the_destination(monkeypatch):
    g = _busy_paid(monkeypatch, cool_peer=True)
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.llm_session = session

    async def _free():
        await asyncio.sleep(0.04)
        g._llm_inflight[PAID] = 0

    async def _go():
        task = asyncio.create_task(_free())
        resp = await proxy.handle_proxy(_req())
        await task
        return resp

    resp = asyncio.run(asyncio.wait_for(_go(), 0.5))
    assert resp.status == 200
    assert "example.test" in str(session.calls[0]["url"])
    assert all("9101" not in str(c["url"]) for c in session.calls)


def test_r4_peer_at_another_priority_is_skipped(monkeypatch):
    g = _busy_paid(monkeypatch, peer_priority=2)
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.llm_session = session

    async def _free():
        await asyncio.sleep(0.04)
        g._llm_inflight[PAID] = 0

    async def _go():
        task = asyncio.create_task(_free())
        resp = await proxy.handle_proxy(_req())
        await task
        return resp

    resp = asyncio.run(asyncio.wait_for(_go(), 0.5))
    assert resp.status == 200
    assert "example.test" in str(session.calls[0]["url"])
    assert all("9101" not in str(c["url"]) for c in session.calls)


def test_r4_waiter_does_not_pass_a_full_backend(monkeypatch):
    """A single check would send the call while the slot is still held."""
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512, max_inflight=1),
    ])
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01
    g._llm_inflight[LOCAL] = 1
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.llm_session = session

    async def _go():
        task = asyncio.create_task(proxy.handle_proxy(_req()))
        await asyncio.sleep(0.08)
        still_waiting = session.calls == []
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return still_waiting

    assert asyncio.run(_go()) is True
    assert g._llm_inflight[LOCAL] == 1


# --- C1 -------------------------------------------------------------------

_ODD = [("string", "nope"), ("list", ["tok"]), ("int", 3)]


@pytest.mark.parametrize("path", ["solo", "batch", "fold"])
@pytest.mark.parametrize("name,usage", _ODD, ids=[row[0] for row in _ODD])
@pytest.mark.asyncio
async def test_c1_odd_usage_on_a_stop_is_stored(monkeypatch, path, name, usage):
    if path == "batch":
        reply = _json_const(200, _jsonl(usage))
    elif path == "fold":
        reply = _json_const(200, _fold_ok(usage))
    else:
        reply = _json_const(200, _summary(usage))
    calls, mem, extra = await _run_path(monkeypatch, path, reply, store=True)
    assert calls == 1
    assert mem.rows == {}
    if path == "solo":
        assert extra == {8}
    elif path == "batch":
        assert extra == {9, 10}


@pytest.mark.parametrize("path", ["solo", "batch", "fold"])
@pytest.mark.parametrize("code", [429, 500, 503])
@pytest.mark.parametrize("name,usage", _ODD, ids=[row[0] for row in _ODD])
@pytest.mark.asyncio
async def test_c1_odd_usage_on_a_fault_is_connection_lost(monkeypatch, path, code, name, usage):
    body = {"error": {"type": "server_error", "message": "no"}, "usage": usage}
    calls, mem, extra = await _run_path(monkeypatch, path, _json_const(code, body))
    assert calls == 1
    reasons = [row[0] for row in mem.rows.values()]
    assert reasons
    assert set(reasons) == {"connection_lost"}
    if path == "fold":
        assert mem.rows[("fold", extra)][0] == "connection_lost"


def test_c1_numeric_usage_survives_and_a_bool_does_not(monkeypatch):
    usage = {"prompt_tokens": 8, "completion_tokens": 4, "nested": {"a": 1, "b": "x"}, "flag": True}
    g, _session, resp = _run(
        monkeypatch,
        [_page(500, {"error": {"type": "server_error"}, "usage": usage})],
        [_backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512)],
    )
    assert _body(resp)["usage"] == {"prompt_tokens": 8, "completion_tokens": 4, "nested": {"a": 1}}
    assert g._llm_tokens_prompt_total[LOCAL] == 8
    assert g._llm_tokens_completion_total[LOCAL] == 4


def test_c1_record_llm_call_never_raises_and_drops_a_planted_key(monkeypatch, tmp_path, caplog):
    import dream_telemetry as dt

    path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(dt, "DREAM_METRICS_PATH", str(path))
    with caplog.at_level(logging.INFO):
        rec = dt.record_llm_call(
            "REM",
            {"usage": {"completion_tokens": KEY, "prompt_tokens": 3}, "timings": "nope", "model": "m"},
            wall_s="slow",
        )
        dt.record_llm_call("REM", "nope")
        dt.record_llm_call("REM", ["nope"])
        dt.record_llm_call("REM", {"usage": 3})
        dt.record_llm_call("REM", {"usage": ["tok"]})
        dt.call_timing_summary("nope", "slow")
        dt.call_timing_summary({"usage": KEY, "timings": {"prompt_ms": True, "predicted_ms": 2}}, 1.0)
    assert rec["predicted_n"] is None
    assert rec["prompt_n"] == 3
    assert rec["model"] == "m"
    blob = caplog.text + path.read_text()
    assert KEY not in blob


@pytest.mark.parametrize("path", ["solo", "batch", "fold"])
@pytest.mark.parametrize("kind", ["stop", "fault"])
@pytest.mark.asyncio
async def test_c1_planted_usage_key_is_not_echoed(monkeypatch, tmp_path, caplog, path, kind):
    import dream_telemetry as dt

    path_metrics = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(dt, "DREAM_METRICS_PATH", str(path_metrics))
    usage = {"completion_tokens": KEY, "prompt_tokens": 5}
    if kind == "stop":
        if path == "batch":
            payload = _jsonl(usage)
        elif path == "fold":
            payload = _fold_ok(usage)
        else:
            payload = _summary(usage)
        reply = _json_const(200, payload)
    else:
        reply = _json_const(500, {"error": {"type": "server_error"}, "usage": usage})
    with caplog.at_level(logging.DEBUG):
        calls, mem, _extra = await _run_path(monkeypatch, path, reply, store=(kind == "stop"))
    blob = caplog.text
    if path_metrics.exists():
        blob += path_metrics.read_text()
    for row in mem.rows.values():
        blob += json.dumps(row)
    assert KEY not in blob
    assert calls == 1
    if kind == "fault":
        assert {row[0] for row in mem.rows.values()} == {"connection_lost"}
    else:
        assert mem.rows == {}


# --- C2 -------------------------------------------------------------------

def test_c2_error_label_is_redacted_before_reply_fault_and_audit(monkeypatch, caplog):
    import coordinator

    monkeypatch.setenv("ROUTE_KEY_A", KEY)
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, token_env="ROUTE_KEY_A"),
        _backend(MID, model="next", n_ctx=32768, max_output=256),
    ])
    audit = []

    def _capture(event, *, origin, **fields):
        audit.append((event, fields))

    monkeypatch.setattr(coordinator, "_write_credential_audit_line", _capture)
    url_type = f"https://user:{KEY}@llm.example.test/v1/secret"
    proxy = g.AsyncHiveMindProxy()
    session = _Session([
        _page(401, {"error": {"type": KEY, "message": "no"}}),
    ])
    proxy.llm_session = session
    with caplog.at_level(logging.DEBUG):
        resp = asyncio.run(proxy.handle_proxy(_req()))
    rendered = resp.body.decode() + caplog.text + json.dumps(audit)
    assert KEY not in rendered
    assert "user:" not in rendered or KEY not in rendered
    assert audit and audit[0][0] == "upstream_credential_fault"
    assert audit[0][1]["error_type"] == "[redacted]"
    assert _body(resp)["type"] == "[redacted]"

    session = _Session([_page(401, {"error": {"type": url_type}})])
    proxy.llm_session = session
    caplog.clear()
    audit.clear()
    with caplog.at_level(logging.DEBUG):
        resp = asyncio.run(proxy.handle_proxy(_req()))
    rendered = resp.body.decode() + caplog.text + json.dumps(audit)
    assert KEY not in rendered
    assert "example.test" in _body(resp)["type"]

    session = _Session([
        _page(429, {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}),
        _stop(),
    ])
    proxy.llm_session = session
    audit.clear()
    asyncio.run(proxy.handle_proxy(_req()))
    assert any(item[1].get("error_type") == "insufficient_quota" for item in audit)
    assert len(session.calls) == 2


def test_c2_control_characters_do_not_survive_the_label(monkeypatch):
    monkeypatch.setenv("ROUTE_KEY_A", KEY)
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, token_env="ROUTE_KEY_A"),
    ])
    proxy = g.AsyncHiveMindProxy()
    dirty = KEY + "\nmore\x01"
    session = _Session([_page(401, {"error": {"type": dirty}})])
    proxy.llm_session = session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    text = resp.body.decode()
    assert KEY not in text
    assert "\n" not in _body(resp)["type"]
    assert "\x01" not in _body(resp)["type"]


# --- C3 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c3_failed_delete_after_store_does_not_pay_again(monkeypatch):
    import llm_backlog
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return type("R", (), {
            "status_code": 200,
            "headers": {},
            "text": "",
            "json": lambda self: _summary({"completion_tokens": 2}),
        })()

    long = _long("STORED ")
    daemon, mem, graph = _rem(monkeypatch, {8: long}, "http://127.0.0.1:9/unused")
    monkeypatch.setattr("httpx.AsyncClient.post", _post)
    monkeypatch.setenv("SM_GATEWAY_BOOT_ID", "boot-b")
    mem.rows[("rem", "8")] = ("write_failed", 1, 1, "pool-a", "boot-a", "")

    def _boom(*args, **kwargs):
        raise RuntimeError("delete down")

    monkeypatch.setattr(llm_backlog, "delete", _boom)
    await daemon.run_cycle()
    assert calls == [1]
    assert 8 in graph.processed
    assert 8 in daemon._withheld
    graph.cleared.clear()
    for _ in range(4):
        await daemon.run_cycle()
    assert calls == [1]
    assert 8 in graph.processed
    assert all(8 not in ids for ids in graph.cleared)
    assert rem is not None


# --- C4 / C5 --------------------------------------------------------------

def test_c4_identity_encoding_stores_and_a_free_404_queues(monkeypatch):
    import coordinator

    assert coordinator._decompress_full_for_usage(b"abc", "Identity") == b"abc"
    good = _page(200, _summary({"completion_tokens": 4}), {"Content-Encoding": "IDENTITY"})
    _g, session, resp = _run(monkeypatch, [good])
    assert len(session.calls) == 1
    assert resp.status == 200
    assert _body(resp)["choices"][0]["message"]["content"] == '{"summary": "kept"}'
    missing = _page(404, {"error": {"type": "missing"}}, {"Content-Encoding": "identity"})
    _g, session, resp = _run(
        monkeypatch, [missing, _stop()],
        [_backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512)],
    )
    assert len(session.calls) == 1
    assert resp.status == 404
    assert _body(resp)["error"] == "upstream_fault"


def test_c5_corrupt_compressed_non_2xx_keeps_its_status(monkeypatch):
    bad = b"\x1f\x8bnot-gzip"
    missing = _page(404, bad, {"Content-Encoding": "gzip"})
    _g, session, resp = _run(monkeypatch, [missing, _stop()])
    assert len(session.calls) == 1
    assert resp.status == 404
    assert _body(resp).get("reason") != "unparseable"

    limited = _page(429, bad, {"Content-Encoding": "gzip"})
    _g, session, resp = _run(monkeypatch, [limited, _stop()])
    assert len(session.calls) == 2
    assert resp.status == 200

    busy = _page(503, bad, {"Content-Encoding": "gzip"})
    _g, session, resp = _run(
        monkeypatch, [busy, _stop()],
        [_backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512)],
    )
    assert len(session.calls) == 1
    assert resp.status == 503
    assert _body(resp).get("error") != "backlog"

    cut = _page(200, bad, {"Content-Encoding": "gzip"})
    _g, session, resp = _run(monkeypatch, [cut, _stop()])
    assert len(session.calls) == 1
    assert resp.status == 422
    assert _body(resp)["reason"] == "unparseable"


def test_c5_over_cap_compressed_404_queues(monkeypatch):
    raw = gzip.compress(b'{"error":{"type":"missing"}}')
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
        _backend(MID, model="next", n_ctx=32768, max_output=512),
    ])
    g.LLM_USAGE_CAPTURE_CAP_BYTES = 4
    proxy = g.AsyncHiveMindProxy()
    up = _page(404, raw, {"Content-Encoding": "gzip"})
    session = _Session([up, _stop()])
    proxy.llm_session = session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert len(session.calls) == 1
    assert resp.status == 404


# --- C6 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c6_hangup_during_a_wait_does_not_call_the_backend(monkeypatch):
    held = asyncio.Event()
    calls = []

    async def upstream(_request):
        calls.append(1)
        await held.wait()
        return aw.json_response(_summary({"completion_tokens": 1}))

    up = aw.Application()
    up.router.add_post("/v1/chat/completions", upstream)
    up_runner = aw.AppRunner(up)
    await up_runner.setup()
    up_site = aw.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]
    g = _fresh(monkeypatch, [
        _backend(f"http://127.0.0.1:{up_port}/v1", model="shared-backend",
                 n_ctx=8192, max_output=512, max_inflight=1),
    ])
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01
    proxy = g.AsyncHiveMindProxy()
    await proxy.start_session()
    app = aw.Application()
    app.router.add_post("/v1/chat/completions", proxy.handle_proxy)
    gw_runner = aw.AppRunner(app)
    await gw_runner.setup()
    gw_site = aw.TCPSite(gw_runner, "127.0.0.1", 0)
    await gw_site.start()
    gw_port = gw_site._server.sockets[0].getsockname()[1]

    body = b'{"messages":[{"role":"user","content":"hello"}],"model":"local-model"}'
    req = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n" + body
    )

    async def _client(close_after):
        reader, writer = await asyncio.open_connection("127.0.0.1", gw_port)
        writer.write(req)
        await writer.drain()
        if close_after is None:
            data = await reader.read()
            writer.close()
            await writer.wait_closed()
            return data
        await asyncio.sleep(close_after)
        writer.close()
        await writer.wait_closed()
        return b""

    try:
        first = asyncio.create_task(_client(None))
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls == [1]
        await _client(0.05)
        await asyncio.sleep(0.05)
        assert calls == [1]
        held.set()
        await asyncio.wait_for(first, 1)
        await asyncio.sleep(0.05)
        assert calls == [1]
    finally:
        await proxy.cleanup()
        await gw_runner.cleanup()
        await up_runner.cleanup()


# --- C7 -------------------------------------------------------------------

def test_c7_consolidation_helpers_log_the_exception_class(monkeypatch, caplog):
    import consolidation_loop as cl

    def _boom(*args, **kwargs):
        raise RuntimeError("password=sekret")

    monkeypatch.setattr(cl.psycopg2, "connect", _boom)
    with caplog.at_level(logging.WARNING):
        assert cl._crun_start("insight") is None
        cl._crun_finish(1, "ok")
        cl._crun_record_terminal("insight", "idle")
        cl._crun_recover_and_prune()
        assert cl._fetch_outbox_created_at([1]) == {}
    assert "RuntimeError" in caplog.text
    assert "password=sekret" not in caplog.text


# --- C8 -------------------------------------------------------------------

def test_c8_string_error_moves_and_a_sibling_message_does_not(monkeypatch):
    moved = {"error": "maximum context length exceeded"}
    _g, session, resp = _run(monkeypatch, [_Upstream(400, moved), _stop()])
    assert len(session.calls) == 2
    assert resp.status == 200
    echoed = {"error": "validation", "message": "echoed prompt: context length"}
    _g, session, resp = _run(monkeypatch, [_Upstream(400, echoed), _stop()])
    assert len(session.calls) == 1
    assert resp.status == 400
    sibling = {"error": 1, "message": "maximum context length exceeded"}
    _g, session, resp = _run(monkeypatch, [_Upstream(400, sibling), _stop()])
    assert len(session.calls) == 1
    assert resp.status == 400


# --- M10, M18, M19, M20, M24, M25 -----------------------------------------

def test_m10_free_fault_logs_the_queued_line(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        _g, session, resp = _run(
            monkeypatch,
            [_page(400, {"error": {"type": "invalid_request_error", "message": "no"}})],
            [_backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512)],
        )
    assert resp.status == 400
    assert len(session.calls) == 1
    assert "Upstream reply queued status=400 backend=shared-backend" in caplog.text


@pytest.mark.asyncio
async def test_m18_apply_exception_writes_one_unparseable_row(monkeypatch):
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return type("R", (), {
            "status_code": 200,
            "headers": {},
            "json": lambda self: _summary({"completion_tokens": 2}),
        })()

    daemon, mem, _graph = _rem(monkeypatch, {8: _long("APPLY ")}, "http://127.0.0.1:9/unused")
    monkeypatch.setattr("httpx.AsyncClient.post", _post)

    async def _raise(*args, **kwargs):
        raise RuntimeError("apply down")

    daemon._apply_fact_result = _raise
    for _ in range(3):
        await daemon.run_cycle()
    assert calls == [1]
    assert mem.rows[("rem", "8")][0] == "unparseable"
    assert rem.LLM_FAIL_PARSE == "parse"


@pytest.mark.asyncio
async def test_m19_batch_apply_exception_writes_a_row_per_fact(monkeypatch):
    calls = []

    async def _post(self, url, **kwargs):
        calls.append(kwargs["json"]["messages"][-1]["content"])
        return type("R", (), {
            "status_code": 200,
            "headers": {},
            "json": lambda self: _jsonl({"completion_tokens": 2}),
        })()

    daemon, mem, _graph = _rem(
        monkeypatch,
        {9: _long("BATCHNINE "), 10: _long("BATCHTEN ")},
        "http://127.0.0.1:9/unused",
    )
    monkeypatch.setattr("httpx.AsyncClient.post", _post)

    async def _raise(*args, **kwargs):
        raise RuntimeError("apply down")

    daemon._apply_fact_result = _raise
    for _ in range(3):
        await daemon.run_cycle()
    assert len(calls) == 1
    assert mem.rows[("rem", "9")][0] == "unparseable"
    assert mem.rows[("rem", "10")][0] == "unparseable"


@pytest.mark.asyncio
async def test_m20_release_skips_an_ineligible_write_failed_row(monkeypatch):
    import rem_loop as rem

    mem = _arm_backlog(monkeypatch)
    mem.rows[("rem", "8")] = ("write_failed", 1, 1, "pool-a", "boot-a", "")
    mem.rows[("rem", "9")] = ("write_failed", 1, 1, "pool-a", "boot-old", "")
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon._withheld = set()
    daemon.driver = _Graph([8, 9], rem.ONT.fact)
    await daemon._release_eligible_write_failed()
    flat = [i for batch in daemon.driver.cleared for i in batch]
    assert flat == [9]


@pytest.mark.asyncio
async def test_m24_embed_failure_with_a_failed_insert_is_withheld(monkeypatch):
    import llm_backlog

    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mem, _pg, key = _fold_daemon(monkeypatch, None)
    daemon.get_embedding = AsyncMock(return_value=None)
    calls = []
    real = daemon.generate_insight_slots

    async def _count(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    daemon.generate_insight_slots = _count

    def _raise(*args, **kwargs):
        raise RuntimeError("insert down")

    monkeypatch.setattr(llm_backlog, "upsert", _raise)
    await daemon.run_insight_cycle()
    await daemon.run_insight_cycle()
    assert calls == [1]
    assert mem.rows == {}
    assert key in daemon._withheld_folds


@pytest.mark.asyncio
async def test_m25_write_failure_with_a_failed_insert_is_withheld(monkeypatch):
    import consolidation_loop as cl
    import llm_backlog

    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mem, _pg, key = _fold_daemon(monkeypatch, None)
    daemon.get_embedding = AsyncMock(return_value=[0.1])
    calls = []
    real = daemon.generate_insight_slots

    async def _count(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    daemon.generate_insight_slots = _count

    def _raise(*args, **kwargs):
        raise RuntimeError("insert down")

    def _write(*args, **kwargs):
        raise RuntimeError("pg down")

    monkeypatch.setattr(cl, "write_insight_summary", _write)
    monkeypatch.setattr(llm_backlog, "upsert", _raise)
    await daemon.run_insight_cycle()
    await daemon.run_insight_cycle()
    assert calls == [1]
    assert mem.rows == {}
    assert key in daemon._withheld_folds


# --- round 5: R1 and D1-D8 ------------------------------------------------


def _jsonl_ids(n, usage=None):
    lines = "\n".join('{"idx": %d, "summary": "s%d"}' % (i, i) for i in range(n))
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": lines}}],
        "usage": {"completion_tokens": 2} if usage is None else usage,
    }


def _no_fragment(blob, secret):
    window = secret[:12]
    assert window and window not in blob
    for i in range(0, len(secret) - 11, 12):
        assert secret[i:i + 12] not in blob


@pytest.mark.asyncio
@pytest.mark.parametrize("name,first", [
    ("busy", _const(503, {"Content-Type": "application/json"}, b'{"error":"busy"}')),
    ("http500", _const(500, {"Content-Type": "application/json"}, b'{"error":{"type":"server_error"}}')),
    ("http504", _const(504, {"Content-Type": "application/json"}, b'{"error":{"type":"gateway_timeout"}}')),
    ("html", _const(200, {"Content-Type": "text/html"}, b"<html>queued</html>")),
])
async def test_r1_free_non_4xx_stays_one_batch(monkeypatch, name, first):
    """A 503, 500, 504, or HTML queue adds nothing. Recovery is one batch call."""
    ok = _json_const(200, _jsonl_ids(3))
    async with _Gateway(monkeypatch, first) as gw:
        contents = {
            8: _long("FACTEIGHT "),
            9: _long("FACTNINE "),
            10: _long("FACTTEN "),
        }
        daemon, mem, graph = _rem(monkeypatch, contents, gw.url)
        await daemon.run_cycle()
        assert daemon._solo_only == set()
        assert mem.rows == {}
        assert graph.processed == set()
        failed = gw.up.calls
        gw.up.reply = ok
        await daemon.run_cycle()
        assert gw.up.calls == failed + 1
        assert graph.processed == {8, 9, 10}


@pytest.mark.asyncio
async def test_r1_gateway_outage_stays_one_batch(monkeypatch):
    """A connect failure queues the batch. The next cycle stores it in one call."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    dead = f"http://127.0.0.1:{port}/v1"
    g = _fresh(monkeypatch, [
        _backend(dead, model="shared-backend", n_ctx=100000, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    await proxy.start_session()
    app = aw.Application()
    app.router.add_post("/v1/chat/completions", proxy.handle_proxy)
    runner = aw.AppRunner(app)
    await runner.setup()
    site = aw.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    gw_port = site._server.sockets[0].getsockname()[1]
    try:
        daemon, mem, graph = _rem(
            monkeypatch,
            {8: _long("FACTEIGHT "), 9: _long("FACTNINE "), 10: _long("FACTTEN ")},
            f"http://127.0.0.1:{gw_port}/v1/chat/completions",
        )
        await daemon.run_cycle()
        assert daemon._solo_only == set()
        assert mem.rows == {}
        assert graph.processed == set()
    finally:
        await proxy.cleanup()
        await runner.cleanup()
    ok = _json_const(200, _jsonl_ids(3))
    async with _Gateway(monkeypatch, ok) as gw:
        import rem_loop as rem
        rem.REASONER_URL = gw.url
        await daemon.run_cycle()
        assert gw.up.calls == 1
        assert graph.processed == {8, 9, 10}
        assert daemon._solo_only == set()


def _nest_usage(depth):
    node = {"prompt_tokens": 1}
    for _ in range(depth):
        node = {"wrap": node}
    return node


@pytest.mark.parametrize("path", ["solo", "batch", "fold"])
@pytest.mark.asyncio
async def test_d1_deep_usage_stores_once(monkeypatch, path):
    usage = _nest_usage(20000)
    if path == "batch":
        reply = _json_const(200, _jsonl(usage))
    elif path == "fold":
        reply = _json_const(200, _fold_ok(usage))
    else:
        reply = _json_const(200, _summary(usage))
    calls, mem, extra = await _run_path(monkeypatch, path, reply, store=True)
    assert calls == 1
    assert mem.rows == {}
    if path == "solo":
        assert extra == {8}
    elif path == "batch":
        assert extra == {9, 10}


def test_d1_deep_usage_does_not_raise_in_the_walk(monkeypatch):
    usage = _nest_usage(20000)
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
    ])
    assert g._usage_of({"usage": usage}) is usage
    assert g._numeric_usage(usage) == {}
    body = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "kept"}'}}],
        "usage": usage,
    }
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_page(200, body)])
    proxy.llm_session = session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 200
    assert len(session.calls) == 1
    assert _body(resp)["usage"] == {}
    assert "wrap" not in resp.body.decode()


def test_d2_long_key_is_redacted_before_the_cut(monkeypatch):
    import coordinator

    key = "sk-" + ("a1b2c3d4e5f6" * 18) + "wxyz"
    assert len(key) == 223
    monkeypatch.setenv("ROUTE_KEY_A", key)
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, token_env="ROUTE_KEY_A"),
    ])
    audit = []

    def _capture(event, *, origin, **fields):
        audit.append((event, fields))

    monkeypatch.setattr(coordinator, "_write_credential_audit_line", _capture)
    proxy = g.AsyncHiveMindProxy()

    def _once(label):
        audit.clear()
        session = _Session([_page(401, {"error": {"code": label}})])
        proxy.llm_session = session
        resp = asyncio.run(proxy.handle_proxy(_req()))
        fault = json.dumps(coordinator._llm_faults_snapshot())
        blob = resp.body.decode() + fault + json.dumps(audit)
        _no_fragment(blob, key)
        assert audit and audit[0][0] == "upstream_credential_fault"
        return blob

    _once(key)
    _once(("p" * 100) + key)


def test_d3_encoder_401_scrubs_and_400_keeps_overflow(monkeypatch):
    key = "enc-key-a1b2c3d4e5f6"
    monkeypatch.setenv("EMBEDDER_API_KEY", key)
    monkeypatch.setenv("RERANKER_API_KEY", key)
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
    ])
    dirty = "https://user:%s@h.example/x\nNL%s" % (key, key)
    proxy = g.AsyncHiveMindProxy()

    class _Enc:
        method = "POST"
        can_read_body = False
        content_length = None
        headers = {}

        def __init__(self, path):
            self.rel_url = __import__("yarl").URL(path)
            self._store = {}

        def __setitem__(self, k, v):
            self._store[k] = v

        def __getitem__(self, k):
            return self._store[k]

        def __delitem__(self, k):
            self._store.pop(k, None)

    for path in ("/v1/embeddings", "/v1/reranking"):
        session = _Session([_page(401, {"error": {"type": dirty}})])
        proxy.session = session
        resp = asyncio.run(proxy.handle_encoder(_Enc(path)))
        text = resp.body.decode()
        assert resp.status == 401
        _no_fragment(text, key)
        assert "user:" not in text
        assert "\n" not in _body(resp)["type"]

    message = "maximum context length is 8192 tokens. However, you requested 8193 tokens"
    session = _Session([_page(400, {"error": {"message": message, "type": "invalid_request_error"}})])
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(_Enc("/v1/embeddings")))
    body = _body(resp)
    assert body["overflow"]["kind"] == "overrun"
    assert body["overflow"]["advertised"] == 8192
    assert body["overflow"]["requested"] == 8193
    assert body["type"] == "invalid_request_error"


@pytest.mark.parametrize("path", ["solo", "batch"])
@pytest.mark.asyncio
async def test_d4_reply_model_is_redacted_in_jsonl_and_timing(monkeypatch, tmp_path, path):
    import dream_telemetry as dt
    import rem_loop as rem

    metrics = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(dt, "DREAM_METRICS_PATH", str(metrics))
    saved = []

    async def _save(self, pg_id, timing, conn, loop):
        saved.append(json.dumps(timing))

    monkeypatch.setattr(rem.REMDaemon, "_write_rem_timing", _save)
    monkeypatch.setenv("ROUTE_KEY_A", KEY)
    payload = _summary({"completion_tokens": 2}) if path == "solo" else _jsonl({"completion_tokens": 2})
    payload = dict(payload)
    payload["model"] = KEY
    async with _Gateway(monkeypatch, _json_const(200, payload)) as gw:
        urls = list(gw.g.LLM_BACKEND_TOKENS)
        gw.g.LLM_BACKEND_TOKENS[urls[0]] = KEY
        if path == "solo":
            daemon, mem, graph = _rem(monkeypatch, {8: _long("SOLOFACT ")}, gw.url)
        else:
            daemon, mem, graph = _rem(
                monkeypatch, {9: _long("BATCHNINE "), 10: _long("BATCHTEN ")}, gw.url)
        await daemon.run_cycle()
        assert mem.rows == {}
        if path == "solo":
            assert graph.processed == {8}
        else:
            assert graph.processed == {9, 10}
            assert saved
    blob = metrics.read_text() + "".join(saved)
    assert KEY not in blob
    assert "[redacted]" in blob

    plain = _summary({"completion_tokens": 2})
    plain["model"] = "shared-backend"
    saved.clear()
    metrics.write_text("")
    async with _Gateway(monkeypatch, _json_const(200, plain)) as gw:
        daemon, mem, graph = _rem(monkeypatch, {8: _long("SOLOFACT ")}, gw.url)
        await daemon.run_cycle()
        assert graph.processed == {8}
    text = metrics.read_text()
    assert '"model": "shared-backend"' in text or '"model":"shared-backend"' in text.replace(" ", "")


def test_d4_non_string_model_is_absent(monkeypatch):
    import dream_telemetry as dt

    assert dt._model_name({"model": {"k": KEY}}) is None
    assert dt._model_name({"model": "shared-backend"}) == "shared-backend"
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
    ])
    body = {
        "model": {"k": KEY},
        "choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "kept"}'}}],
        "usage": {"completion_tokens": 1},
    }
    proxy = g.AsyncHiveMindProxy()
    proxy.llm_session = _Session([_page(200, body)])
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 200
    assert "model" not in _body(resp)
    assert KEY not in resp.body.decode()


def test_d5_huge_token_count_does_not_break_the_snapshot(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
    ])
    huge = 10 ** 4299
    assert len(str(huge)) == 4300
    for _ in range(12):
        g._record_backend_token_usage(LOCAL, {"prompt_tokens": huge, "completion_tokens": 2})
    blob = json.dumps(g._llm_runtime_snapshot(for_telemetry=True))
    assert str(huge) not in blob
    assert g._llm_tokens_prompt_total.get(LOCAL, 0) == 0
    assert g._llm_tokens_completion_total[LOCAL] == 24
    g._record_backend_token_usage(LOCAL, {
        "prompt_tokens": 17,
        "completion_tokens": 5,
    })
    g._record_backend_token_usage(LOCAL, {
        "prompt_tokens": -3,
        "completion_tokens": float("nan"),
    })
    assert g._llm_tokens_prompt_total[LOCAL] == 17
    assert g._llm_tokens_completion_total[LOCAL] == 29
    json.dumps(g._llm_runtime_snapshot(for_telemetry=True))


@pytest.mark.asyncio
async def test_d6_overflow_and_nan_on_a_good_batch_store(monkeypatch, tmp_path):
    import dream_telemetry as dt

    metrics = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(dt, "DREAM_METRICS_PATH", str(metrics))
    huge = 10 ** 400
    payload = _jsonl({"completion_tokens": huge, "prompt_tokens": 4})
    payload["timings"] = {"prompt_ms": huge, "predicted_ms": 1.5, "predicted_per_second": 3}
    calls, mem, extra = await _run_path(
        monkeypatch, "batch", _json_const(200, payload), store=False)
    assert calls == 1
    assert mem.rows == {}
    assert extra == {9, 10}
    dt.record_llm_call(
        "REM",
        {
            "model": "shared-backend",
            "usage": {"completion_tokens": float("nan"), "prompt_tokens": float("inf")},
            "timings": {"prompt_ms": float("nan"), "predicted_ms": huge, "predicted_n": huge},
        },
        wall_s=huge,
    )
    summary = dt.call_timing_summary(
        {
            "model": "shared-backend",
            "usage": {"completion_tokens": float("nan")},
            "timings": {"prompt_ms": huge, "predicted_ms": float("inf")},
        },
        huge,
    )
    assert summary["completion_tokens"] is None
    assert summary["service_ms"] is None
    assert summary["model"] == "shared-backend"
    for line in metrics.read_text().splitlines():
        json.loads(line)
        assert str(huge) not in line
        assert "NaN" not in line
        assert "Infinity" not in line


def test_d7_disconnect_and_slot_free_in_the_same_poll(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512, max_inflight=1),
    ])
    g._llm_inflight[LOCAL] = 1
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.llm_session = session
    req = _req()
    req.transport = object()

    async def _sleep(_delay):
        req.transport = None
        g._llm_inflight[LOCAL] = 0

    monkeypatch.setattr(g.asyncio, "sleep", _sleep)
    resp = asyncio.run(proxy.handle_proxy(req))
    assert session.calls == []
    assert resp.status == 499


def test_d8_wait_and_move_lines_scrub_the_model(monkeypatch, caplog):
    dirty = "https://user:pw@h.example/v1\nFAKE\x01"
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model=dirty, n_ctx=8192, max_output=512, max_inflight=1),
    ])
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01
    g._llm_inflight[LOCAL] = 1
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.llm_session = session

    async def _free():
        await asyncio.sleep(0.03)
        g._llm_inflight[LOCAL] = 0

    async def _go():
        task = asyncio.create_task(_free())
        resp = await proxy.handle_proxy(_req())
        await task
        return resp

    with caplog.at_level(logging.WARNING):
        resp = asyncio.run(_go())
    assert resp.status == 200
    waiting = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Waiting for model")]
    assert len(waiting) == 1
    assert "user:pw" not in waiting[0]
    assert "\n" not in waiting[0]
    assert "\x01" not in waiting[0]

    caplog.clear()
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model=dirty, n_ctx=8192, max_output=512),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=512),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_down(), _stop()])
    proxy.llm_session = session
    with caplog.at_level(logging.WARNING):
        resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 200
    moving = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Moving to model")]
    assert len(moving) == 1
    assert "deepseek-flash" in moving[0]
    assert "user:pw" not in moving[0]
    assert "\n" not in moving[0]
    assert "\x01" not in moving[0]
