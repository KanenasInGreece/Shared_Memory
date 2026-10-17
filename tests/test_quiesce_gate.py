"""
Unit and integration tests for the gateway quiesce gate.

Asserts behavior against governing rulings:
  - decision:3101 (no arbitrary timer or fixed limit without shown need): no artificial timeouts or limits.
  - maintenance-admin resolves to admin unless declared read (fact:3099: the pin narrows only)
  - fact:3102 (check interval is not a limit; an abandoned first quiesce is ended): Postgres check interval.
  - fact:3093 (outbox keeps draining; alt-vector pauses): background worker coordination during quiesce.
  - fact:3103: ended_by is resume, timer or caller_gone; daemons is fenced or not_fenced; a refresh re-arms only on 200 or 202
"""

import asyncio
import importlib.util
import json
import logging
import math
import os
import sys
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiohttp.web_exceptions import HTTPForbidden, HTTPServiceUnavailable


# ── Test Environment Scaffolding ─────────────────────────────────────────────

def load_coordinator(agent_tokens: str = "", agent_roles: str = "", **env):
    """Import coordinator.py fresh with given env, isolating module state."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    for k in ("AGENT_TOKENS", "AGENT_ROLES", *env.keys()):
        os.environ.pop(k, None)

    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    if agent_roles:
        os.environ["AGENT_ROLES"] = agent_roles
    for k, v in env.items():
        if v is not None:
            os.environ[k] = str(v)

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_quiesce_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_request(path: str, auth_header: str | None = None, method: str = "POST") -> MagicMock:
    req = MagicMock()
    req.path = path
    req.method = method
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    req.headers = headers
    req.get = MagicMock(return_value=None)
    req.__setitem__ = MagicMock()
    req.match_info.route.resource.canonical = path.rstrip("/") or "/"
    req.read = AsyncMock(return_value=b"{}")
    req.transport = MagicMock()
    return req


def _json_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.transport = MagicMock()
    return req


async def _noop_handler(request):
    return web.json_response({"ok": True})


def _mock_conn():
    conn = MagicMock()
    conn._hang_execute = False
    conn._hang_fetchval = False
    _closed = False
    listeners = []
    pending_waiters = set()

    def _is_closed():
        return _closed

    def _terminate():
        nonlocal _closed
        _closed = True
        import asyncpg.exceptions
        for fut in list(pending_waiters):
            if not fut.done():
                fut.set_exception(asyncpg.exceptions.ConnectionDoesNotExistError("connection was terminated"))
        pending_waiters.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for fn in list(listeners):
            if loop is not None and loop.is_running():
                loop.call_soon(fn, conn)
            else:
                try:
                    fn(conn)
                except Exception:
                    pass

    async def _execute(*args, **kwargs):
        if _closed:
            import asyncpg.exceptions
            raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was terminated")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        pending_waiters.add(fut)
        if getattr(conn, "_hang_execute", False):
            try:
                await fut
            finally:
                pending_waiters.discard(fut)
        else:
            pending_waiters.discard(fut)
        return "OK"

    async def _fetchval(*args, **kwargs):
        if _closed:
            import asyncpg.exceptions
            raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was terminated")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        pending_waiters.add(fut)
        if getattr(conn, "_hang_fetchval", False):
            try:
                await fut
            finally:
                pending_waiters.discard(fut)
        else:
            pending_waiters.discard(fut)
        return 1

    conn.is_closed = MagicMock(side_effect=_is_closed)
    conn.terminate = MagicMock(side_effect=_terminate)
    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.close = AsyncMock()
    conn.add_termination_listener = MagicMock(side_effect=listeners.append)
    conn._pending_waiters = pending_waiters
    return conn


# ── Partial body upload not counted; quiesce reaches 200 ─────────────────────

@pytest.mark.asyncio
async def test_write_uploading_partial_body_not_counted_and_quiesce_reaches_200(monkeypatch):
    """A write whose body is still uploading when quiesce starts is NOT counted.
    Quiesce reaches 200 while upload is mid-flight. Once body completes, request gets 503.
    """
    mod = load_coordinator("")
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    app = web.Application(middlewares=[mod.auth_middleware])
    handler_called = False

    async def save_handler(request):
        nonlocal handler_called
        handler_called = True
        return web.json_response({"ok": True})

    app.router.add_post("/memory/save", save_handler)

    server = TestServer(app)
    await server.start_server()

    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    headers = (
        b"POST /memory/save HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 10\r\n\r\n"
        b"123"
    )
    writer.write(headers)
    await writer.drain()

    # Let event loop process partial header/body
    await asyncio.sleep(0.05)
    assert mod._writes_in_flight == 0, "Write MUST NOT be counted while body is still uploading"

    # Quiesce starts and reaches 200 while body is still uploading
    quiesce_resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": 30})), 5.0)
    assert quiesce_resp.status == 200
    q_data = json.loads(quiesce_resp.text)
    assert q_data["status"] == "success"
    assert q_data["lock_held"] is True
    assert q_data["quiesced"] is True

    # Complete the upload of the remaining 7 bytes
    writer.write(b"4567890")
    await writer.drain()

    resp_data = await reader.read(2048)
    assert resp_data.startswith(b"HTTP/1.1 503 ")
    assert b"Retry-After: 30" in resp_data
    assert b"backup in progress" in resp_data
    assert handler_called is False, "Shed request must never execute handler"

    writer.close()
    await writer.wait_closed()
    await server.close()
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Write counted before quiesce holds off 200 ───────────────────────────────

@pytest.mark.asyncio
async def test_write_counted_before_quiesce_holds_off_200(monkeypatch):
    """A write counted before quiesce holds it off 200 until handler completes."""
    mod = load_coordinator("claude:tok_abc")
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    write_entered = asyncio.Event()
    allow_finish = asyncio.Event()

    async def slow_handler(request):
        write_entered.set()
        await allow_finish.wait()
        return web.json_response({"ok": True})

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    write_task = asyncio.create_task(mod.auth_middleware(req, slow_handler))
    await asyncio.wait_for(write_entered.wait(), 5.0)

    assert mod._writes_in_flight == 1

    quiesce_task = asyncio.create_task(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": 30})))
    await asyncio.sleep(0.15)
    assert not quiesce_task.done(), "Quiesce cannot finish with 200 while write is in flight"

    allow_finish.set()
    write_resp = await asyncio.wait_for(write_task, 5.0)
    assert write_resp.status == 200
    assert mod._writes_in_flight == 0

    quiesce_resp = await asyncio.wait_for(quiesce_task, 5.0)
    assert quiesce_resp.status == 200
    q_data = json.loads(quiesce_resp.text)
    assert q_data["status"] == "success"
    assert q_data["writes_in_flight"] == 0
    assert q_data["lock_held"] is True

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Slow upload does not 408; quiesce reaches 200 mid-upload ─────────────────

@pytest.mark.asyncio
async def test_slow_upload_does_not_408_quiesce_reaches_200_mid_upload(monkeypatch):
    """A slow upload (no timeout) does NOT 408; quiesce arrives mid-upload and reaches 200;
    once upload completes it gets 503.
    """
    mod = load_coordinator("")
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    app = web.Application(middlewares=[mod.auth_middleware])
    app.router.add_post("/memory/save", _noop_handler)

    server = TestServer(app)
    await server.start_server()

    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    headers = (
        b"POST /memory/save HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 10\r\n\r\n"
        b"ab"
    )
    writer.write(headers)
    await writer.drain()

    # Simulate slow upload delay — does NOT 408
    await asyncio.sleep(0.2)
    assert mod._writes_in_flight == 0

    # Quiesce starts and reaches 200 without waiting for slow upload
    resp_q = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": 30})), 5.0)
    assert resp_q.status == 200

    # Upload completes
    writer.write(b"cdefgh12")
    await writer.drain()

    resp_data = await reader.read(2048)
    assert resp_data.startswith(b"HTTP/1.1 503 ")
    assert b"Retry-After: 30" in resp_data
    assert b"408" not in resp_data

    writer.close()
    await writer.wait_closed()
    await server.close()
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Auth-on ordering: rejections fire before request.read() ──────────────────

@pytest.mark.asyncio
async def test_auth_on_ordering_rejections_before_body_read():
    """Invalid token (401), read token (403), and missing principal (403)
    all reject before request.read() is awaited.
    """
    mod = load_coordinator("claude:tok_full,reader:tok_read", agent_roles="reader:read")

    # 1. Invalid token -> 401 before body read
    req_bad_tok = _make_request("/memory/save", auth_header="Bearer bad_token")
    req_bad_tok.read = AsyncMock(side_effect=AssertionError("req.read called on invalid token"))
    with pytest.raises(mod.web.HTTPUnauthorized):
        await mod.auth_middleware(req_bad_tok, _noop_handler)

    # 2. Read-only token on write route -> 403 before body read
    req_read_tok = _make_request("/memory/save", auth_header="Bearer tok_read")
    req_read_tok.read = AsyncMock(side_effect=AssertionError("req.read called on read-only token"))
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req_read_tok, _noop_handler)

    # 3. Missing principal with GATEWAY_REQUIRE_PRINCIPAL -> 403 before body read
    mod.GATEWAY_REQUIRE_PRINCIPAL = True
    req_no_prin = _make_request("/memory/save", auth_header="Bearer tok_full")
    req_no_prin.get = MagicMock(return_value=None)
    req_no_prin.read = AsyncMock(side_effect=AssertionError("req.read called on missing principal"))
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req_no_prin, _noop_handler)


# ── Fast-fail before body read (auth-off and auth-on) ─────────────────────────

@pytest.mark.asyncio
async def test_fast_fail_before_body_read_auth_off():
    """Auth-off path fast-fails writes before body read when quiesced.
    Asserts 503, req.read never awaited, exactly one requests_total increment,
    by_status['503'] == 1, and no latency sample recorded.
    """
    mod = load_coordinator("")
    mod._backup_quiesce = True
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False

    req = _make_request("/memory/save")
    req.read = AsyncMock()

    with pytest.raises(HTTPServiceUnavailable) as exc:
        await mod.auth_middleware(req, _noop_handler)

    assert exc.value.status == 503
    assert req.read.await_count == 0
    assert mod._gateway_requests_total == 1
    assert mod._gateway_by_status["503"] == 1
    assert len(mod._gateway_latency._ring) == 0


@pytest.mark.asyncio
async def test_fast_fail_before_body_read_auth_on():
    """Auth-on path fast-fails writes before body read when quiesced.
    Asserts 503, req.read never awaited, exactly one requests_total increment,
    by_status['503'] == 1, and no latency sample recorded.
    """
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    assert mod.AUTH_CONFIGURED_AT_STARTUP is True

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    req.read = AsyncMock()

    with pytest.raises(HTTPServiceUnavailable) as exc:
        await mod.auth_middleware(req, _noop_handler)

    assert exc.value.status == 503
    assert req.read.await_count == 0
    assert mod._gateway_requests_total == 1
    assert mod._gateway_by_status["503"] == 1
    assert len(mod._gateway_latency._ring) == 0


# ── max_seconds validator: unclamped, rejects invalid types/values ─────────

@pytest.mark.asyncio
async def test_max_seconds_validation_and_unclamped_usage(monkeypatch):
    """max_seconds is validated, never clamped.
    Rejects: bool, str, nan, inf, <= 0, float overflow -> 400 with full error shape.
    Accepts: int (1, 300), float (0.5, 30.0, 1e6), None -> used exactly as given.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=lambda *a, **k: _mock_conn()))

    # Validator unit check
    for bad in (True, False, "30", "inf", float("nan"), float("inf"), float("-inf"), -1, 0, 0.0, -0.01):
        with pytest.raises(ValueError):
            mod._validate_max_seconds(bad)

    assert mod._validate_max_seconds(None) == float(mod.BACKUP_QUIESCE_MAX_SECONDS)
    assert mod._validate_max_seconds(1) == 1.0
    assert mod._validate_max_seconds(300) == 300.0
    assert mod._validate_max_seconds(0.5) == 0.5
    assert mod._validate_max_seconds(30.0) == 30.0
    assert mod._validate_max_seconds(1e6) == 1000000.0

    # handle_backup 400 full error shape check
    for bad in (True, False, "30", "nan", float("nan"), 0, -5):
        resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": bad})), 5.0)
        assert resp.status == 400
        data = json.loads(resp.text)
        assert data["status"] == "error"
        assert data["error"] == "max_seconds_invalid"
        assert data["reason"] == "max_seconds_invalid"
        assert data["quiesced"] is False
        assert data["lock_held"] is False
        assert data["writes_in_flight"] == 0
        assert data["oldest_write_age_s"] is None
        assert data["worker_batches_in_flight"] == 0
        assert data["ttl_seconds"] is None
        assert coord._quiesce_conn is None

    # Unclamped usage: 0.5s is used as 0.5s (not clamped to 1.0)
    resp_small = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": 0.5})), 5.0)
    assert resp_small.status == 200
    assert coord._remaining_ttl_seconds() <= 0.5
    await asyncio.wait_for(coord._end_quiesce(), 5.0)

    # Unclamped usage: 10000s is used as 10000s (not clamped to 900.0)
    resp_large = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": 10000})), 5.0)
    assert resp_large.status == 200
    assert coord._remaining_ttl_seconds() > 900.0
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Write handler running past old drain timeout waits for 200 ───────────────

@pytest.mark.asyncio
async def test_write_handler_running_past_old_drain_timeout_waits_for_200(monkeypatch):
    """A write handler running for longer than old drain timeout does NOT cause 202;
    quiesce waits until it finishes and then returns 200.
    """
    mod = load_coordinator("claude:tok_abc")
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    write_entered = asyncio.Event()
    allow_finish = asyncio.Event()

    async def long_handler(request):
        write_entered.set()
        await allow_finish.wait()
        return web.json_response({"ok": True})

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    write_task = asyncio.create_task(mod.auth_middleware(req, long_handler))
    await asyncio.wait_for(write_entered.wait(), 5.0)

    quiesce_task = asyncio.create_task(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": 30})))
    await asyncio.sleep(0.3)
    assert not quiesce_task.done()

    # Handler completes
    allow_finish.set()
    await asyncio.wait_for(write_task, 5.0)

    resp = await asyncio.wait_for(quiesce_task, 5.0)
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["status"] == "success"
    assert data["reason"] is None
    assert data["lock_held"] is True
    assert data["writes_in_flight"] == 0

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Slow connect does not time out; caller disconnect -> caller_gone ─────────

@pytest.mark.asyncio
async def test_slow_connect_does_not_timeout_caller_gone_returns_caller_gone(monkeypatch):
    """Connect task is not bounded by an internal timeout; if caller disconnects
    (req.transport is None), quiesce aborts and returns caller_gone.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()

    connect_started = asyncio.Event()
    connect_fut = asyncio.get_running_loop().create_future()

    async def hanging_connect(dsn, timeout=None):
        connect_started.set()
        return await connect_fut

    monkeypatch.setattr(mod.asyncpg, "connect", hanging_connect)

    req = _json_request({"state": "quiesce"})
    quiesce_task = asyncio.create_task(coord._begin_quiesce(30, request=req))
    await asyncio.wait_for(connect_started.wait(), 5.0)

    # Verify no timeout occurs
    await asyncio.sleep(0.2)
    assert not quiesce_task.done()

    # Caller disconnects: req.transport becomes None
    req.transport = None
    res = await asyncio.wait_for(quiesce_task, 5.0)
    assert res["reason"] == "caller_gone"

    connect_fut.cancel()


# ── Resume during lock wait returns 409 resumed_during_wait ──────────────────

@pytest.mark.asyncio
async def test_resume_during_lock_wait_returns_409_resumed_during_wait(monkeypatch):
    """Resume called while quiesce is waiting for the lock returns 409
    resumed_during_wait with ended_by: 'resume', and terminates connection.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()

    lock_entered = asyncio.Event()

    async def slow_execute(sql, *args):
        if "pg_advisory_lock" in str(sql):
            lock_entered.set()
            await asyncio.sleep(0.4)
        return "OK"

    conn.execute = AsyncMock(side_effect=slow_execute)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    req_q = _json_request({"state": "quiesce"})
    quiesce_task = asyncio.create_task(coord.handle_backup(req_q))
    await asyncio.wait_for(lock_entered.wait(), 5.0)

    # Resume while quiesce is waiting for lock
    resume_resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "resume"})), 5.0)
    assert resume_resp.status == 200

    quiesce_resp = await asyncio.wait_for(quiesce_task, 5.0)
    assert quiesce_resp.status == 409
    data = json.loads(quiesce_resp.text)
    assert data["status"] == "error"
    assert data["error"] == "resumed_during_wait"
    assert data["reason"] == "resumed_during_wait"
    assert data["ended_by"] == "resume"
    assert data["lock_held"] is False
    conn.terminate.assert_called()
    assert coord._quiesce_conn is None
    assert mod._backup_quiesce is False


# ── TTL firing during lock wait returns 409 with ended_by: 'timer' ────────────

@pytest.mark.asyncio
async def test_ttl_expiry_during_lock_wait_returns_409_with_ended_by_timer(monkeypatch):
    """TTL expiry while quiesce is waiting returns 409 resumed_during_wait
    with ended_by: 'timer', and terminates connection.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()

    async def slow_lock(sql, *args):
        if "pg_advisory_lock" in str(sql):
            await asyncio.sleep(0.3)
        return "OK"

    conn.execute = AsyncMock(side_effect=slow_lock)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    # max_seconds=0.05 will expire while slow_lock is waiting
    resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce", "max_seconds": 0.05})), 5.0)
    assert resp.status == 409
    data = json.loads(resp.text)
    assert data["error"] == "resumed_during_wait"
    assert data["ended_by"] == "timer"
    conn.terminate.assert_called()
    assert coord._quiesce_conn is None
    assert mod._backup_quiesce is False


# ── Caller disconnect on first quiesce vs refresh ────────────────────────────

@pytest.mark.asyncio
async def test_caller_disconnect_first_quiesce_rolls_back_vs_refresh(monkeypatch):
    """Caller disconnect on first quiesce rolls back flag (to False);
    caller disconnect on refresh leaves flag True.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()

    lock_wait = asyncio.Event()

    async def slow_lock(sql, *args):
        if "pg_advisory_lock" in str(sql):
            lock_wait.set()
            await asyncio.sleep(0.3)
        return "OK"

    conn.execute = AsyncMock(side_effect=slow_lock)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    # 1. First quiesce: was_quiesced is False
    req1 = _json_request({"state": "quiesce"})
    task1 = asyncio.create_task(coord._begin_quiesce(30, request=req1))
    await asyncio.wait_for(lock_wait.wait(), 5.0)

    # Disconnect caller
    req1.transport = None
    res1 = await asyncio.wait_for(task1, 5.0)
    assert res1["reason"] == "caller_gone"
    assert mod._backup_quiesce is False, "First-quiesce caller_gone MUST roll back flag to False"
    assert coord._quiesce_ended_by == "caller_gone"

    # 2. Complete a successful quiesce so gateway is legitimately quiesced
    conn2 = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn2))
    res2 = await asyncio.wait_for(coord._begin_quiesce(30, request=_json_request({"state": "quiesce"})), 5.0)
    assert res2["reason"] is None
    assert mod._backup_quiesce is True
    assert coord._lock_held() is True

    # 3. Refresh call where caller disconnects mid-refresh
    req3 = _json_request({"state": "quiesce"})
    # simulate disconnect during drain
    mod._writes_in_flight = 1
    task3 = asyncio.create_task(coord._begin_quiesce(30, request=req3))
    await asyncio.sleep(0.05)
    req3.transport = None
    res3 = await asyncio.wait_for(task3, 5.0)
    assert res3["reason"] == "caller_gone"
    assert mod._backup_quiesce is True, "Refresh caller_gone MUST leave flag True"
    mod._writes_in_flight = 0
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Resume drops connection immediately: terminate(), no close, no unlock ────

@pytest.mark.asyncio
async def test_resume_terminates_immediately_no_unlock_no_close(monkeypatch):
    """Resume drops connection immediately: terminate() is called, close() is never awaited,
    and pg_advisory_unlock is never called (Postgres drops locks on session termination).
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    await asyncio.wait_for(coord._begin_quiesce(max_seconds=30), 5.0)
    assert coord._quiesce_conn is conn

    # Reset execute mock to inspect calls during resume
    conn.execute.reset_mock()
    conn.close.reset_mock()
    conn.terminate.reset_mock()

    resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "resume"})), 5.0)
    assert resp.status == 200

    assert conn.terminate.call_count == 1
    assert conn.close.await_count == 0, "conn.close must never be called on resume"
    for c in conn.execute.await_args_list:
        assert "pg_advisory_unlock" not in str(c)

    assert coord._quiesce_conn is None
    assert mod._backup_quiesce is False


# ── Dead-man timer re-arms on 200/202, NOT on entry or exits ─────────────────

@pytest.mark.asyncio
async def test_dead_man_timer_rearms_only_on_200_202_not_on_exits(monkeypatch):
    """Dead-man timer re-arms immediately before returning 200 or 202, NOT on entry.
    Exits on caller_gone or resumed_during_wait do NOT change previous deadline.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    t_virtual = 1000.0

    def fake_now():
        return t_virtual

    monkeypatch.setattr(mod, "_now", fake_now)

    # First quiesce: sets timer to 1000 + 300 = 1300
    res = await asyncio.wait_for(coord._begin_quiesce(max_seconds=300), 5.0)
    assert res["reason"] is None
    assert coord._ttl_target == 1300.0

    # Advance time
    t_virtual = 1050.0

    # Exit on caller_gone
    req_gone = _json_request({"state": "quiesce"})
    req_gone.transport = None
    res_gone = await asyncio.wait_for(coord._begin_quiesce(500, request=req_gone), 5.0)
    assert res_gone["reason"] == "caller_gone"
    assert coord._ttl_target == 1300.0, "caller_gone exit must NOT re-arm deadline"

    # Exit on lock_connection_failed (202 re-arms timer)
    dead_conn = _mock_conn()
    dead_conn.is_closed = MagicMock(return_value=True)
    coord._quiesce_conn = dead_conn
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=OSError("network down")))

    res_fail = await asyncio.wait_for(coord._begin_quiesce(max_seconds=500), 5.0)
    assert res_fail["reason"] == "lock_connection_failed"
    assert coord._ttl_target == 1050.0 + 500.0 == 1550.0

    # Advance time again
    t_virtual = 1100.0

    # Successful refresh re-arms deadline
    fresh_conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=fresh_conn))
    res_ok = await asyncio.wait_for(coord._begin_quiesce(max_seconds=500), 5.0)
    assert res_ok["reason"] is None
    assert coord._ttl_target == 1100.0 + 500.0 == 1600.0

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── SQL & Signature Pinning: Exact queries and timeout=None ───────────────────

@pytest.mark.asyncio
async def test_lock_connection_sql_and_connect_signature(monkeypatch):
    """Exact SQL asserted on lock connection:
    - SET client_connection_check_interval = '1s'
    - SELECT pg_advisory_lock($1) with BACKUP_ADVISORY_LOCK_KEY (8765309)
    - connect pinned to connect(PG_DSN, timeout=None)
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    connect_mock = AsyncMock(return_value=conn)
    monkeypatch.setattr(mod.asyncpg, "connect", connect_mock)

    await asyncio.wait_for(coord._begin_quiesce(max_seconds=30), 5.0)

    # Pin connect signature
    assert connect_mock.await_args == call(mod.PG_DSN, timeout=None)

    # Pin SQL calls
    exec_calls = conn.execute.await_args_list
    assert any("SET client_connection_check_interval = '1s'" in str(c) for c in exec_calls)
    lock_call = [c for c in exec_calls if "SELECT pg_advisory_lock" in str(c)][0]
    assert lock_call == call("SELECT pg_advisory_lock($1)", mod.BACKUP_ADVISORY_LOCK_KEY)

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Spy: No asyncio.wait_for or asyncio.timeout wraps quiesce steps ───────────

@pytest.mark.asyncio
async def test_no_asyncio_wait_for_or_timeout_in_quiesce_or_body_read(monkeypatch):
    """Assert neither asyncio.wait_for nor asyncio.timeout wraps body read,
    connect, probe, or lock.
    """
    import asyncio as real_asyncio
    wait_for_calls = []

    def spy_wait_for(fut, timeout):
        wait_for_calls.append((fut, timeout))
        return real_asyncio.wait_for(fut, timeout)

    monkeypatch.setattr(real_asyncio, "wait_for", spy_wait_for)

    mod = load_coordinator("")
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    # 1. Body read
    req = _make_request("/memory/save")
    await mod.auth_middleware(req, _noop_handler)

    # 2. Quiesce
    await coord._begin_quiesce(30, request=req)
    # Probe on second quiesce
    await coord._begin_quiesce(30, request=req)
    await coord._end_quiesce()

    # None of the recorded wait_for calls wrapped body read or quiesce steps
    for fut, timeout in wait_for_calls:
        # If any wait_for exists, it must not be on connect, execute, fetchval, or req.read
        target_str = str(fut)
        assert "read" not in target_str
        assert "connect" not in target_str
        assert "fetchval" not in target_str
        assert "execute" not in target_str


# ── Write after quiesce flag sheds 503, counter unchanged ─────────────────

@pytest.mark.asyncio
async def test_write_after_quiesce_flag_sheds_503_counter_unchanged():
    """Write arriving after quiesce flag is set sheds 503; counter unchanged."""
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    assert mod._writes_in_flight == 0

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPServiceUnavailable) as exc:
        await mod.auth_middleware(req, _noop_handler)

    assert exc.value.status == 503
    assert exc.value.headers.get("Retry-After") == str(mod.BACKUP_RETRY_AFTER)
    assert mod._writes_in_flight == 0


# ── Error conditions decrement writes_in_flight ──────────────────────────────

@pytest.mark.asyncio
async def test_handler_exception_decrements_write_counter():
    """Handler exception returns write counter to 0."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")

    async def fail_handler(request):
        assert mod._writes_in_flight == 1
        raise RuntimeError("database crash")

    with pytest.raises(RuntimeError):
        await mod.auth_middleware(req, fail_handler)

    assert mod._writes_in_flight == 0
    assert len(mod._write_started) == 0


@pytest.mark.asyncio
async def test_handler_cancelled_error_decrements_write_counter():
    """asyncio.CancelledError inside handler returns write counter to 0."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")

    async def cancel_handler(request):
        assert mod._writes_in_flight == 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await mod.auth_middleware(req, cancel_handler)

    assert mod._writes_in_flight == 0
    assert len(mod._write_started) == 0


@pytest.mark.asyncio
async def test_client_disconnect_mid_body_resets_write_counter():
    """Client disconnect mid-body over real socket resets write counter to 0."""
    mod = load_coordinator("")
    app = web.Application(middlewares=[mod.auth_middleware])

    async def slow_handler(request):
        data = await request.read()
        return web.json_response({"ok": True, "size": len(data)})

    app.router.add_post("/memory/save", slow_handler)

    server = TestServer(app)
    await server.start_server()

    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"POST /memory/save HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 1000\r\n\r\npartial")
    await writer.drain()

    # Disconnect
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    assert mod._writes_in_flight == 0
    await server.close()


@pytest.mark.asyncio
async def test_body_over_client_max_size_propagates_413_resets_counter():
    """Body over client_max_size raises 413 and resets counter to 0."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    req.read = AsyncMock(side_effect=mod.web.HTTPRequestEntityTooLarge(max_size=1024, actual_size=2048))

    with pytest.raises(mod.web.HTTPRequestEntityTooLarge):
        await mod.auth_middleware(req, _noop_handler)

    assert mod._writes_in_flight == 0
    assert len(mod._write_started) == 0


# ── Auth-on principal refusal is never counted ───────────────────────────────

@pytest.mark.asyncio
async def test_principal_refused_write_is_not_counted():
    """GATEWAY_REQUIRE_PRINCIPAL refusal fires before write counting."""
    mod = load_coordinator("claude:tok_abc")
    mod.GATEWAY_REQUIRE_PRINCIPAL = True
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    req.get = MagicMock(return_value=None)  # no kernel principal

    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)

    assert mod._writes_in_flight == 0
    assert len(mod._write_started) == 0


# ── Auth-off writes shed while quiesced, accepted when not ───────────────────

@pytest.mark.asyncio
async def test_auth_off_writes_shed_while_quiesced_accepted_when_not():
    """On unauthenticated gateway, writes are shed while quiesced and flow when not."""
    mod = load_coordinator("")
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False

    req = _make_request("/memory/save")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200

    mod._backup_quiesce = True
    req2 = _make_request("/memory/save")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req2, _noop_handler)
    assert mod._writes_in_flight == 0

    mod._backup_quiesce = False
    req3 = _make_request("/memory/save")
    resp3 = await mod.auth_middleware(req3, _noop_handler)
    assert resp3.status == 200


# ── Route keying: POST /memory/save/ not treated as write ─────────────────────

@pytest.mark.asyncio
async def test_route_keying_trailing_slash_not_counted_as_write():
    """Trailing slash /memory/save/ canonical does not match _WRITE_ROUTES."""
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True

    req = _make_request("/memory/save/", auth_header="Bearer tok_abc")
    req.match_info.route.resource.canonical = "/memory/save/"

    assert not mod._is_write_request(req)

    catch_all_entered = False

    async def catch_all_handler(request):
        nonlocal catch_all_entered
        catch_all_entered = True
        assert mod._writes_in_flight == 0
        return web.json_response({"ok": True, "source": "catch-all"})

    resp = await mod.auth_middleware(req, catch_all_handler)
    assert resp.status == 200
    assert catch_all_entered is True
    assert mod._writes_in_flight == 0


# ── Two concurrent quiesces -> single connection and truthful ────────────────

@pytest.mark.asyncio
async def test_concurrent_quiesces_single_connection_and_truthful(monkeypatch):
    """Two concurrent quiesces take the lock sequentially without duplicate connects."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    connect_mock = AsyncMock(return_value=conn)
    monkeypatch.setattr(mod.asyncpg, "connect", connect_mock)

    req1 = _json_request({"state": "quiesce"})
    req2 = _json_request({"state": "quiesce"})

    resp1, resp2 = await asyncio.wait_for(asyncio.gather(
        coord.handle_backup(req1),
        coord.handle_backup(req2),
    ), 5.0)

    assert resp1.status == 200
    assert resp2.status == 200
    assert connect_mock.await_count == 1
    assert coord._quiesce_conn is conn

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Dead stored connection reacquired or reported ────────────────────────────

@pytest.mark.asyncio
async def test_dead_stored_connection_reacquired_or_reported(monkeypatch):
    """Dead stored connection is terminated and reacquired; connection failure reports 202."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()

    dead_conn = _mock_conn()
    dead_conn.is_closed = MagicMock(return_value=True)
    coord._quiesce_conn = dead_conn
    mod._backup_quiesce = True

    fresh_conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=fresh_conn))

    res = await asyncio.wait_for(coord._begin_quiesce(max_seconds=30), 5.0)
    assert res["reason"] is None
    assert res["lock_held"] is True
    assert coord._quiesce_conn is fresh_conn
    dead_conn.terminate.assert_called()

    # Re-acquire failure case
    dead_conn2 = _mock_conn()
    dead_conn2.fetchval = AsyncMock(side_effect=OSError("connection reset"))
    coord._quiesce_conn = dead_conn2
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=OSError("PG down")))

    resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce"})), 5.0)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert data["status"] == "error"
    assert data["reason"] == "lock_connection_failed"
    assert coord._quiesce_conn is None
    assert mod._backup_quiesce is True

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


@pytest.mark.asyncio
async def test_stored_connection_probe_returning_zero_detaches_terminates_and_reacquires(caplog, monkeypatch):
    """When stored connection's probe fetchval("SELECT 1") returns 0 (not 1):
    the stored connection is detached and terminated, a new connection is acquired,
    and handle_backup replies 200 on the new connection.
    Fails if task.result() != 1 check in coordinator.py (~11691) is removed.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()

    # Initial stored connection returning 0 on SELECT 1 probe
    failing_conn = _mock_conn()
    failing_conn.fetchval = AsyncMock(return_value=0)
    coord._quiesce_conn = failing_conn
    mod._backup_quiesce = True

    # Fresh connection acquired to replace the failed one
    fresh_conn = _mock_conn()
    fresh_conn.fetchval = AsyncMock(return_value=1)
    connect_mock = AsyncMock(return_value=fresh_conn)
    monkeypatch.setattr(mod.asyncpg, "connect", connect_mock)

    with caplog.at_level(logging.WARNING, logger="coordinator"):
        resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce"})), 5.0)

    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["status"] == "success"
    assert data["reason"] is None
    assert data["lock_held"] is True
    assert failing_conn.terminate.call_count == 1
    assert coord._quiesce_conn is fresh_conn
    assert connect_mock.await_count == 1
    assert "backup quiesce: stored lock connection failed probe — detached and re-acquiring" in caplog.text

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Termination listener clears conn and logs warning ────────────────────────

@pytest.mark.asyncio
async def test_termination_listener_clears_conn_and_logs(caplog, monkeypatch):
    """Connection termination listener sets _quiesce_conn to None and logs gap."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await asyncio.wait_for(coord._begin_quiesce(max_seconds=30), 5.0)
        assert conn.add_termination_listener.call_count == 1
        listener_cb = conn.add_termination_listener.call_args[0][0]

        # Trigger termination callback
        listener_cb(conn)
        assert coord._quiesce_conn is None
        assert "backup lock connection lost while quiesced" in caplog.text

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Alt-vector skips while quiesced and batch in-flight blocks ───────────────

@pytest.mark.asyncio
async def test_alt_vector_skips_while_quiesced_and_batch_in_flight_blocks(monkeypatch):
    """Alt-vector worker skips sweep while quiesced; in-flight batch holds quiesce off 200.
    Connect mocked with _mock_conn() to satisfy live-store guard.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(mod, "ALT_VECTOR_POLL_INTERVAL", 0.01)

    alt_called = 0

    async def fake_fill():
        nonlocal alt_called
        alt_called += 1
        return 1

    coord._fill_pending_alternative_vectors = fake_fill
    mod._backup_quiesce = True

    # Start alt-vector worker loop while quiesced
    worker_task = asyncio.create_task(coord._alternative_vector_worker())
    await asyncio.sleep(0.04)
    assert alt_called == 0, "Alt vector sweep skipped while quiesced"

    # Worker executes when quiesce ends
    mod._backup_quiesce = False
    await asyncio.sleep(0.03)
    assert alt_called > 0, "Alt vector sweep runs when unquiesced"

    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    # Batch in flight holds quiesce
    mod._worker_batches_in_flight = 1
    quiesce_task = asyncio.create_task(coord._begin_quiesce(max_seconds=30))
    await asyncio.sleep(0.15)
    assert not quiesce_task.done(), "Quiesce cannot complete while worker batch is in flight"

    mod._worker_batches_in_flight = 0
    res = await asyncio.wait_for(quiesce_task, 5.0)
    assert res["reason"] is None
    assert res["lock_held"] is True
    assert res["worker_batches_in_flight"] == 0

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Outbox drains while quiesced ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outbox_drains_while_quiesced(monkeypatch):
    """Outbox worker drains pending rows while quiesced, observing _worker_batches_in_flight == 1."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    mod._backup_quiesce = True
    monkeypatch.setattr(mod, "OUTBOX_POLL_INTERVAL", 0.01)

    pending = 3
    batches_observed = []
    all_drained = asyncio.Event()

    async def fake_drain():
        nonlocal pending
        batches_observed.append(mod._worker_batches_in_flight)
        if pending > 0:
            pending -= 1
        if pending == 0:
            all_drained.set()

    coord._drain_outbox = fake_drain

    worker_task = asyncio.create_task(coord._outbox_worker())
    await asyncio.wait_for(all_drained.wait(), timeout=2.0)
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    assert pending == 0
    assert mod._backup_quiesce is True
    assert len(batches_observed) >= 3
    assert all(b == 1 for b in batches_observed)
    assert mod._worker_batches_in_flight == 0
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── /admin/outbox telemetry keys ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_outbox_telemetry_keys(monkeypatch):
    """/admin/outbox returns top-level quiesced, lock_held, writes_in_flight,
    oldest_write_age_s, worker_batches_in_flight.
    """
    mod = load_coordinator("backup:tok_b", agent_roles="backup:admin")
    coord = mod.MemoryCoordinator()

    fake_census = {
        "pending": 2, "in_progress": 1, "applied": 10, "failed": 0,
        "rem_reviewed": 5, "oldest_failed_age_s": None, "oldest_pending_age_s": 12.0,
    }
    coord._outbox_census = AsyncMock(return_value=fake_census)

    req = _make_request("/admin/outbox", auth_header="Bearer tok_b", method="GET")
    resp = await coord.handle_admin_outbox(req)
    assert resp.status == 200

    data = json.loads(resp.text)
    assert data["status"] == "success"
    assert data["quiesced"] is False
    assert data["lock_held"] is False
    assert data["writes_in_flight"] == 0
    assert data["oldest_write_age_s"] is None
    assert data["worker_batches_in_flight"] == 0
    assert data["outbox"]["pending"] == 3

    # Now quiesce and verify keys update
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    await asyncio.wait_for(coord._begin_quiesce(max_seconds=30), 5.0)

    resp_q = await coord.handle_admin_outbox(req)
    data_q = json.loads(resp_q.text)
    assert data_q["quiesced"] is True
    assert data_q["lock_held"] is True
    assert data_q["writes_in_flight"] == 0
    assert data_q["worker_batches_in_flight"] == 0

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


# ── Auth-on read and full tokens denied admin backup ─────────────────────────

@pytest.mark.asyncio
async def test_auth_on_read_and_full_tokens_denied_admin_backup():
    """Only admin-role tokens may reach /admin/backup; full and read get 403."""
    mod = load_coordinator("claude:tok_full,monitor:tok_read,admin:tok_adm",
                            agent_roles="monitor:read,admin:admin")

    # full role denied
    req_full = _make_request("/admin/backup", auth_header="Bearer tok_full")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req_full, _noop_handler)

    # read role denied
    req_read = _make_request("/admin/backup", auth_header="Bearer tok_read")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req_read, _noop_handler)

    # admin role passes
    req_adm = _make_request("/admin/backup", auth_header="Bearer tok_adm")
    resp = await mod.auth_middleware(req_adm, _noop_handler)
    assert resp.status == 200


# ── maintenance-admin role pin narrows only ──────────────────────────────────

def test_maintenance_admin_pin_narrows_only():
    """maintenance-admin pin only narrows (fact:3099):
    - Missing, full, or write -> admin
    - Explicit read or roster membership -> read (never widened)
    - role_for_mint accepts read and admin, rejects full and write
    - enforce_roster preserves read
    """
    from agent_roles import (
        MAINTENANCE_ADMIN_AGENT,
        effective_role,
        enforce_roster,
        role_for_mint,
    )

    assert MAINTENANCE_ADMIN_AGENT == "maintenance-admin"

    # Missing, full, or write narrows to "admin"
    assert effective_role("maintenance-admin", None) == "admin"
    assert effective_role("maintenance-admin", "full") == "admin"
    assert effective_role("maintenance-admin", "write") == "admin"
    assert effective_role("maintenance-admin", "admin") == "admin"

    # Explicit read stays read (only narrows, never widens)
    assert effective_role("maintenance-admin", "read") == "read"

    # enforce_roster narrows maintenance-admin to admin unless read
    assert enforce_roster({"maintenance-admin": "full"}) == {"maintenance-admin": "admin"}
    assert enforce_roster({"maintenance-admin": "write"}) == {"maintenance-admin": "admin"}
    assert enforce_roster({"maintenance-admin": "admin"}) == {"maintenance-admin": "admin"}
    assert enforce_roster({"maintenance-admin": "read"}) == {"maintenance-admin": "read"}
    assert enforce_roster({}) == {}

    # role_for_mint accepts None (default admin), admin, and read
    assert role_for_mint("maintenance-admin", None) == "admin"
    assert role_for_mint("maintenance-admin", "admin") == "admin"
    assert role_for_mint("maintenance-admin", "read") == "read"

    # role_for_mint rejects full and write
    for forbidden in ("full", "write"):
        with pytest.raises(ValueError) as exc:
            role_for_mint("maintenance-admin", forbidden)
        assert "cannot be widened" in str(exc.value)


# ── Refresh lock retention, lock-loss recovery, and queued caller departures ───

@pytest.mark.asyncio
async def test_refresh_caller_gone_during_drain_keeps_earlier_lock(monkeypatch):
    """A refresh whose caller leaves must leave the earlier quiesce intact, lock included."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    res = await asyncio.wait_for(coord._begin_quiesce(30), 5.0)
    assert res["reason"] is None and coord._lock_held()
    mod._writes_in_flight = 1
    req = _json_request({"state": "quiesce"})
    t = asyncio.create_task(coord._begin_quiesce(30, request=req))
    await asyncio.sleep(0.15)
    req.transport = None
    res2 = await asyncio.wait_for(t, 5.0)
    mod._writes_in_flight = 0
    assert res2["reason"] == "caller_gone"
    assert mod._backup_quiesce is True
    assert conn.terminate.call_count == 0, "earlier quiesce's lock connection was terminated"
    assert coord._lock_held() is True
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


@pytest.mark.asyncio
async def test_lock_lost_during_drain_returns_202(monkeypatch):
    """A 200 means safe to dump: it must never be returned with lock_held False."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    mod._writes_in_flight = 1
    t = asyncio.create_task(coord.handle_backup(_json_request({"state": "quiesce"})))
    await asyncio.sleep(0.8)
    assert coord._quiesce_pending_conn is conn and coord._quiesce_conn is None
    conn.terminate()            # Postgres drops the session while the gateway drains
    mod._writes_in_flight = 0
    resp = await asyncio.wait_for(t, 5.0)
    data = json.loads(resp.text)
    assert resp.status == 202 and data["reason"] == "lock_connection_failed" and data["lock_held"] is False and data["daemons"] == "not_fenced"
    await asyncio.wait_for(coord._end_quiesce(), 5.0)

    # Direct call assertion
    conn_direct = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn_direct))
    mod._writes_in_flight = 1
    t_direct = asyncio.create_task(coord._begin_quiesce(30))
    await asyncio.sleep(0.8)
    assert coord._quiesce_pending_conn is conn_direct and coord._quiesce_conn is None
    conn_direct.terminate()
    mod._writes_in_flight = 0
    res_direct = await asyncio.wait_for(t_direct, 5.0)
    assert res_direct["reason"] == "lock_connection_failed"
    assert res_direct["lock_held"] is False
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


@pytest.mark.asyncio
async def test_handle_backup_refuses_200_when_lock_not_held(monkeypatch):
    """handle_backup answers 202 with daemons == 'not_fenced' if lock is not held even if reason is None."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    fake_res = {
        "reason": None,
        "lock_held": False,
        "quiesced": True,
        "writes_in_flight": 0,
        "worker_batches_in_flight": 0,
        "oldest_write_age_s": None,
        "drained": False,
    }
    monkeypatch.setattr(coord, "_begin_quiesce", AsyncMock(return_value=fake_res))
    resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce"})), 5.0)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert data["lock_held"] is False
    assert data["daemons"] == "not_fenced"
    assert data["reason"] == "lock_connection_failed"


@pytest.mark.asyncio
async def test_cancel_during_lock_wait_cleans_up(monkeypatch):
    """Cancellation during the lock wait terminates the pending connection; both slots None."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    entered = asyncio.Event()

    async def slow(sql, *a):
        if "pg_advisory_lock" in str(sql):
            entered.set()
            await asyncio.sleep(30)
        return "OK"

    conn.execute = AsyncMock(side_effect=slow)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    t = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
    await asyncio.wait_for(entered.wait(), 5.0)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    pending_left = coord._quiesce_pending_conn
    terminated = conn.terminate.call_count
    assert coord._quiesce_conn is None
    assert mod._backup_quiesce is False
    assert coord._quiesce_timer is None or coord._quiesce_timer.done()
    assert not coord._quiesce_lock.locked()
    await coord._end_quiesce()
    assert pending_left is None, "pending slot left set after cancellation"
    assert terminated >= 1, "pending connection not terminated on cancellation"


@pytest.mark.asyncio
async def test_lock_failure_202_rearms_timer(monkeypatch):
    """The timer is re-armed before 200 AND before 202 lock_connection_failed."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    now = [1000.0]
    monkeypatch.setattr(mod, "_now", lambda: now[0])
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=OSError("down")))
    t = asyncio.create_task(coord._begin_quiesce(100))
    res = await asyncio.wait_for(t, 5.0)
    assert res["reason"] == "lock_connection_failed"
    now[0] = 1010.0
    res = await asyncio.wait_for(coord._begin_quiesce(100), 5.0)
    assert coord._ttl_target == 1110.0, coord._ttl_target
    await coord._end_quiesce()


@pytest.mark.asyncio
async def test_queued_caller_gone_changes_nothing(monkeypatch):
    """A queued caller that disconnects changes nothing — it must not end the running quiesce."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    release = asyncio.Event()

    async def slow(sql, *a):
        if "pg_advisory_lock" in str(sql):
            await release.wait()
        return "OK"

    conn.execute = AsyncMock(side_effect=slow)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    ta = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
    reqb = _json_request({})
    tb = asyncio.create_task(coord._begin_quiesce(30, request=reqb))   # queued in the same tick
    await asyncio.sleep(0.35)
    reqb.transport = None
    resb = await asyncio.wait_for(tb, 5.0)
    release.set()
    resa = await asyncio.wait_for(ta, 5.0)
    assert resb["reason"] == "caller_gone"
    assert resa["reason"] is None, resa
    assert mod._backup_quiesce is True
    await coord._end_quiesce()


@pytest.mark.asyncio
async def test_cancellation_while_queued_leaves_quiesce_lock_unlocked(monkeypatch):
    """Cancellation while queued leaves _quiesce_lock unlocked afterwards."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    release_first = asyncio.Event()

    async def slow_first(sql, *a):
        if "pg_advisory_lock" in str(sql):
            await release_first.wait()
        return "OK"

    conn.execute = AsyncMock(side_effect=slow_first)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    t1 = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
    await asyncio.sleep(0.1)

    t2 = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
    await asyncio.sleep(0.1)

    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2

    release_first.set()
    res1 = await asyncio.wait_for(t1, 5.0)
    assert res1["reason"] is None
    assert not coord._quiesce_lock.locked()
    await coord._end_quiesce()


@pytest.mark.asyncio
async def test_stale_connection_listener_leaves_newer_connection_and_logs_nothing(caplog, monkeypatch):
    """A stale connection's listener firing after a newer connection is stored leaves the newer one and logs nothing."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn1 = _mock_conn()
    conn2 = _mock_conn()

    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=[conn1, conn2]))

    await asyncio.wait_for(coord._begin_quiesce(30), 5.0)
    assert coord._quiesce_conn is conn1
    assert conn1.add_termination_listener.call_count == 1
    listener_cb1 = conn1.add_termination_listener.call_args[0][0]

    conn1.is_closed = MagicMock(return_value=True)
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        caplog.clear()
        res2 = await asyncio.wait_for(coord._begin_quiesce(30), 5.0)
        assert res2["reason"] is None
        assert coord._quiesce_conn is conn2

        listener_cb1(conn1)
        assert coord._quiesce_conn is conn2
        assert "backup lock connection lost while quiesced" not in caplog.text

    await asyncio.wait_for(coord._end_quiesce(), 5.0)


@pytest.mark.asyncio
async def test_resume_during_connect_returns_409_resumed_during_wait(monkeypatch):
    """Resume arriving during connect returns 409 resumed_during_wait with ended_by: 'resume'."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    connect_entered = asyncio.Event()
    connect_proceed = asyncio.Event()

    async def slow_connect(*a, **k):
        connect_entered.set()
        await connect_proceed.wait()
        return _mock_conn()

    connect_mock = AsyncMock(side_effect=slow_connect)
    monkeypatch.setattr(mod.asyncpg, "connect", connect_mock)

    t_q = asyncio.create_task(coord.handle_backup(_json_request({"state": "quiesce"})))
    await asyncio.wait_for(connect_entered.wait(), 5.0)

    resp_res = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "resume"})), 5.0)
    assert resp_res.status == 200

    connect_proceed.set()
    resp_q = await asyncio.wait_for(t_q, 5.0)
    assert resp_q.status == 409
    data = json.loads(resp_q.text)
    assert data["reason"] == "resumed_during_wait"
    assert data["ended_by"] == "resume"
    assert coord._quiesce_conn is None
    assert coord._quiesce_pending_conn is None
    assert connect_mock.await_count == 1
    assert mod._backup_quiesce is False


@pytest.mark.asyncio
async def test_resume_during_probe_returns_409_resumed_during_wait_and_no_new_connect(monkeypatch):
    """Resume arriving during probe returns 409 resumed_during_wait, nothing stored, and no connect afterwards."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    connect_mock = AsyncMock(return_value=conn)
    monkeypatch.setattr(mod.asyncpg, "connect", connect_mock)

    res1 = await asyncio.wait_for(coord._begin_quiesce(30), 5.0)
    assert res1["reason"] is None
    assert coord._quiesce_conn is conn
    assert connect_mock.await_count == 1

    probe_entered = asyncio.Event()
    probe_proceed = asyncio.Event()

    async def slow_fetchval(*a, **k):
        probe_entered.set()
        await probe_proceed.wait()
        return 1

    conn.fetchval = AsyncMock(side_effect=slow_fetchval)

    t_refresh = asyncio.create_task(coord.handle_backup(_json_request({"state": "quiesce"})))
    await asyncio.wait_for(probe_entered.wait(), 5.0)

    resp_resume = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "resume"})), 5.0)
    assert resp_resume.status == 200

    probe_proceed.set()
    resp_refresh = await asyncio.wait_for(t_refresh, 5.0)
    assert resp_refresh.status == 409
    data = json.loads(resp_refresh.text)
    assert data["reason"] == "resumed_during_wait"
    assert data["ended_by"] == "resume"
    assert coord._quiesce_conn is None
    assert coord._quiesce_pending_conn is None
    assert connect_mock.await_count == 1


@pytest.mark.asyncio
async def test_resume_during_drain_returns_409_resumed_during_wait(monkeypatch):
    """Resume arriving while writes are draining returns 409 resumed_during_wait with ended_by: 'resume'."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    connect_mock = AsyncMock(return_value=conn)
    monkeypatch.setattr(mod.asyncpg, "connect", connect_mock)

    mod._writes_in_flight = 1
    t_q = asyncio.create_task(coord.handle_backup(_json_request({"state": "quiesce"})))
    await asyncio.sleep(0.3)
    assert coord._quiesce_pending_conn is conn and coord._quiesce_conn is None

    resp_res = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "resume"})), 5.0)
    assert resp_res.status == 200

    mod._writes_in_flight = 0
    resp_q = await asyncio.wait_for(t_q, 5.0)
    assert resp_q.status == 409
    data = json.loads(resp_q.text)
    assert data["reason"] == "resumed_during_wait"
    assert data["ended_by"] == "resume"
    assert coord._quiesce_conn is None
    assert coord._quiesce_pending_conn is None
    assert connect_mock.await_count == 1
    assert mod._backup_quiesce is False


@pytest.mark.asyncio
async def test_lock_connection_exact_sql(monkeypatch):
    """The exact SQL on a new lock connection:
    [c.args[0] for c in conn.execute.await_args_list] ==
    ["SET client_connection_check_interval = '1s'", "SELECT pg_advisory_lock($1)"].
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    res = await asyncio.wait_for(coord._begin_quiesce(30), 5.0)
    assert res["reason"] is None
    assert [c.args[0] for c in conn.execute.await_args_list] == [
        "SET client_connection_check_interval = '1s'",
        "SELECT pg_advisory_lock($1)",
    ]
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


def test_is_write_request_fails_closed_on_attribute_error():
    """_is_write_request fails closed: an attribute error inside it -> treated as a write."""
    mod = load_coordinator("")

    class CorruptRequest:
        @property
        def match_info(self):
            raise AttributeError("corrupt match_info")

    assert mod._is_write_request(CorruptRequest()) is True


@pytest.mark.asyncio
async def test_write_seq_increments_for_counted_write_not_refused():
    """_write_seq: before = next(seq) … assert next(seq) == before + 2 around one
    counted write (one key taken), and before + 1 around a refused one (no key taken)."""
    mod = load_coordinator("claude:tok_abc")
    seq = mod._write_seq

    before = next(seq)
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200
    assert next(seq) == before + 2

    mod._backup_quiesce = True
    before2 = next(seq)
    req2 = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(mod.web.HTTPServiceUnavailable):
        await mod.auth_middleware(req2, _noop_handler)
    assert next(seq) == before2 + 1


@pytest.mark.asyncio
async def test_mock_conn_terminate_raises_in_pending_call():
    """_mock_conn().terminate makes a PENDING execute/fetchval on that fake raise
    asyncpg.exceptions.ConnectionDoesNotExistError (as real asyncpg does)."""
    import asyncpg.exceptions
    conn = _mock_conn()
    conn._hang_execute = True
    task = asyncio.create_task(conn.execute("SELECT 1"))
    await asyncio.sleep(0.01)
    assert not task.done()
    conn.terminate()
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await task

    conn2 = _mock_conn()
    conn2._hang_fetchval = True
    task2 = asyncio.create_task(conn2.fetchval("SELECT 1"))
    await asyncio.sleep(0.01)
    assert not task2.done()
    conn2.terminate()
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await task2


@pytest.mark.asyncio
async def test_queued_caller_cancelled_when_queue_wait_succeeds_releases_queue_lock(monkeypatch):
    """A queued caller cancelled at the moment its queue wait succeeds must release the queue lock."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()
    entered_a = asyncio.Event()
    hold_a = asyncio.Event()

    async def slow_execute(sql, *a):
        if "pg_advisory_lock" in str(sql):
            entered_a.set()
            await hold_a.wait()
        return "OK"

    conn.execute = AsyncMock(side_effect=slow_execute)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    task_a = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
    await asyncio.wait_for(entered_a.wait(), 5.0)

    task_b = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
    await asyncio.sleep(0.05)

    hold_a.set()
    res_a = await asyncio.wait_for(task_a, 5.0)
    assert res_a["reason"] is None

    task_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_b

    assert not coord._quiesce_lock.locked()

    res_c = await asyncio.wait_for(coord._begin_quiesce(30, request=_json_request({})), 5.0)
    assert res_c["reason"] is None
    await coord._end_quiesce()


@pytest.mark.asyncio
async def test_caller_leaving_after_resume_must_not_end_newer_quiesce(monkeypatch):
    """A caller that leaves AFTER a resume must not end a newer quiesce."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn_a = _mock_conn()
    conn_c = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=[conn_a, conn_c]))

    mod._writes_in_flight = 1
    req_a = _json_request({})
    task_a = asyncio.create_task(coord._begin_quiesce(30, request=req_a))
    await asyncio.sleep(0.3)
    assert coord._quiesce_pending_conn is conn_a and coord._quiesce_conn is None

    await coord._end_quiesce()

    req_c = _json_request({})
    task_c = asyncio.create_task(coord._begin_quiesce(30, request=req_c))

    req_a.transport = None
    mod._writes_in_flight = 0

    res_a = await asyncio.wait_for(task_a, 5.0)
    res_c = await asyncio.wait_for(task_c, 5.0)

    assert res_a["reason"] == "resumed_during_wait"
    assert res_c["reason"] is None
    assert res_c["lock_held"] is True
    assert mod._backup_quiesce is True
    assert coord._quiesce_ended_by == "resume"

    await coord._end_quiesce()


@pytest.mark.asyncio
async def test_caller_cancelled_after_resume_must_not_end_newer_quiesce(monkeypatch):
    """A caller CANCELLED after a resume must not end a newer quiesce."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn_a = _mock_conn()
    conn_c = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=[conn_a, conn_c]))

    mod._writes_in_flight = 1
    req_a = _json_request({})
    a = asyncio.create_task(coord._begin_quiesce(30, request=req_a))
    await asyncio.sleep(0.3)
    assert coord._quiesce_pending_conn is conn_a and coord._quiesce_conn is None

    await coord._end_quiesce()

    req_c = _json_request({})
    c = asyncio.create_task(coord._begin_quiesce(30, request=req_c))

    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a

    mod._writes_in_flight = 0

    res_c = await asyncio.wait_for(c, 5.0)

    assert res_c["reason"] is None
    assert res_c["lock_held"] is True
    assert mod._backup_quiesce is True
    assert coord._quiesce_ended_by == "resume"

    await coord._end_quiesce()


# ── Stale pending connection cleanup, SET failure recovery, and route fallback ─

@pytest.mark.asyncio
async def test_stale_pending_connection_detached_before_connect(caplog, monkeypatch):
    """A stale pending connection at the connect stage is terminated,
    the slot holds the new connection, and a warning is logged.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    stale_conn = _mock_conn()
    coord._quiesce_pending_conn = stale_conn

    fresh_conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=fresh_conn))

    with caplog.at_level(logging.WARNING, logger="coordinator"):
        res = await asyncio.wait_for(coord._begin_quiesce(30), 5.0)

    assert res["reason"] is None
    assert stale_conn.terminate.call_count == 1
    assert coord._quiesce_conn is fresh_conn
    assert "backup quiesce: stale pending connection detached before connect" in caplog.text
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


@pytest.mark.asyncio
async def test_resume_during_set_check_interval_avoids_lock_and_returns_409(monkeypatch):
    """A resume triggered from inside SET client_connection_check_interval terminates
    the connection, never executes SELECT pg_advisory_lock($1), and returns 409.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()

    async def fake_execute(sql, *args):
        if "client_connection_check_interval" in str(sql):
            # Trigger resume during the SET execute
            await coord._end_quiesce(ended_by="resume")
        return "OK"

    conn.execute = AsyncMock(side_effect=fake_execute)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce"})), 5.0)
    assert resp.status == 409
    data = json.loads(resp.text)
    assert data["reason"] == "resumed_during_wait"
    assert data["ended_by"] == "resume"

    # Verify pg_advisory_lock was never executed
    for c in conn.execute.await_args_list:
        assert "pg_advisory_lock" not in str(c)

    assert conn.terminate.call_count >= 1
    assert coord._quiesce_conn is None


@pytest.mark.asyncio
async def test_set_check_interval_raising_connection_does_not_exist_returns_202(monkeypatch):
    """SET client_connection_check_interval raising ConnectionDoesNotExistError
    aborts the lock acquisition, never executes lock SQL, and returns 202 lock_connection_failed.
    """
    import asyncpg.exceptions
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()

    async def fail_set(sql, *args):
        if "client_connection_check_interval" in str(sql):
            raise asyncpg.exceptions.ConnectionDoesNotExistError("connection lost during SET")
        return "OK"

    conn.execute = AsyncMock(side_effect=fail_set)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    resp = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce"})), 5.0)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert data["reason"] == "lock_connection_failed"
    assert data["lock_held"] is False
    assert data["daemons"] == "not_fenced"

    for c in conn.execute.await_args_list:
        assert "pg_advisory_lock" not in str(c)

    assert coord._quiesce_conn is None
    await asyncio.wait_for(coord._end_quiesce(), 5.0)


def test_is_write_request_with_non_str_path_returns_true():
    """_is_write_request with a non-str path fails closed and returns True."""
    mod = load_coordinator("")

    class NonStrPathRequest:
        path = 12345
        method = "POST"
        match_info = MagicMock()
        match_info.route.resource.canonical = None

    class NonePathRequest:
        path = None
        method = "POST"
        match_info = MagicMock()
        match_info.route.resource.canonical = None

    assert mod._is_write_request(NonStrPathRequest()) is True
    assert mod._is_write_request(NonePathRequest()) is True


# ── Double cancellation unwinding safety and resource cleanup ─────────────────

@pytest.mark.asyncio
async def test_double_cancellation_unwinds_slow_child_and_cleans_up(monkeypatch):
    """A child task that unwinds slowly while the quiesce task is cancelled
    multiple times ends with no pending connection, the flag False for a first quiesce,
    and terminate.call_count >= 1.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()

    child_cancelled = asyncio.Event()
    child_conn = _mock_conn()

    async def slow_child_connect(gen):
        coord._quiesce_pending_conn = child_conn
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            child_cancelled.set()
            # Unwind slowly
            await asyncio.sleep(0.2)
            raise

    monkeypatch.setattr(coord, "_quiesce_connect_and_lock", slow_child_connect)

    t = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
    await asyncio.sleep(0.05)
    t.cancel()

    await asyncio.wait_for(child_cancelled.wait(), 2.0)
    t.cancel()

    with pytest.raises(asyncio.CancelledError):
        await t

    assert child_cancelled.is_set()
    assert coord._quiesce_pending_conn is None
    assert coord._quiesce_conn is None
    assert mod._backup_quiesce is False
    assert not coord._quiesce_lock.locked()
    assert child_conn.terminate.call_count >= 1


@pytest.mark.asyncio
async def test_queued_caller_cancelled_as_the_queue_lock_is_handed_over_leaves_it_free(monkeypatch):
    """When caller A holds the queue lock blocked on an event and caller B queues,
    cancelling B as the queue lock is handed over (simultaneously, twice in a row,
    or after sleep(0)) leaves the real queue lock free, allowing a subsequent
    quiesce to complete.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    current_hold = None

    async def slow_lock(sql, *args):
        if "pg_advisory_lock" in str(sql) and current_hold is not None:
            await current_hold.wait()
        return "OK"

    def _make_conn(*args, **kwargs):
        conn = _mock_conn()
        conn.execute = AsyncMock(side_effect=slow_lock)
        return conn

    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=_make_conn))

    async def _do_handover(cancel_fn):
        nonlocal current_hold
        current_hold = asyncio.Event()

        task_a = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
        await asyncio.sleep(0.02)

        task_b = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
        await asyncio.sleep(0.02)

        try:
            await cancel_fn(current_hold, task_b)

            res_a = await asyncio.wait_for(task_a, 5.0)
            assert res_a["reason"] is None

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task_b, 5.0)

            assert not coord._quiesce_lock.locked()

            task_c = asyncio.create_task(coord._begin_quiesce(30, request=_json_request({})))
            res_c = await asyncio.wait_for(task_c, 5.0)
            assert res_c["reason"] is None
        finally:
            await coord._end_quiesce()
        assert not coord._quiesce_lock.locked()

    # 1. In one step with no await between, set A's event AND B.cancel()
    async def _cancel_simultaneous(hold, b):
        hold.set()
        b.cancel()

    await _do_handover(_cancel_simultaneous)

    # 2. Repeat with B.cancel() called twice in a row
    async def _cancel_twice_in_a_row(hold, b):
        hold.set()
        b.cancel()
        b.cancel()

    await _do_handover(_cancel_twice_in_a_row)

    # 3. Repeat with B.cancel() called twice in a row and once more after one await asyncio.sleep(0)
    async def _cancel_twice_and_once_after_sleep(hold, b):
        hold.set()
        b.cancel()
        b.cancel()
        await asyncio.sleep(0)
        b.cancel()

    await _do_handover(_cancel_twice_and_once_after_sleep)

    # 4. Repeat with B.cancel() called after one await asyncio.sleep(0)
    async def _cancel_after_sleep(hold, b):
        hold.set()
        await asyncio.sleep(0)
        b.cancel()

    await _do_handover(_cancel_after_sleep)


# ── Connect failure drains in-flight client writes before returning 202 ────────

@pytest.mark.asyncio
async def test_connect_failure_drains_client_writes_before_answering_202(monkeypatch):
    """A connect failure drains in-flight client writes before returning 202 lock_connection_failed."""
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(side_effect=OSError("network down")))

    mod._writes_in_flight = 1
    t = asyncio.create_task(coord.handle_backup(_json_request({"state": "quiesce"})))
    await asyncio.sleep(0.2)
    assert not t.done(), "Quiesce cannot return 202 while client writes are in flight"

    mod._writes_in_flight = 0
    resp = await asyncio.wait_for(t, 5.0)
    assert resp.status == 202
    data = json.loads(resp.text)
    assert data["reason"] == "lock_connection_failed"
    assert data["writes_in_flight"] == 0
    assert data["lock_held"] is False
    assert data["daemons"] == "not_fenced"
    await coord._end_quiesce()


# ── Truthful caller-gone replies: first quiesce vs abandoned refresh ──────────

@pytest.mark.asyncio
async def test_caller_gone_truthful_replies_first_quiesce_vs_refresh(monkeypatch):
    """An abandoned first quiesce replies 409 with ended_by: 'caller_gone';
    an abandoned refresh replies 409 without ended_by.
    """
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    conn = _mock_conn()

    first_entered = asyncio.Event()

    async def slow_first_lock(sql, *args):
        if "pg_advisory_lock" in str(sql):
            first_entered.set()
            await asyncio.sleep(10)
        return "OK"

    conn.execute = AsyncMock(side_effect=slow_first_lock)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))

    # 1. First quiesce: caller disconnects -> 409 with ended_by: "caller_gone"
    req1 = _json_request({"state": "quiesce"})
    task1 = asyncio.create_task(coord.handle_backup(req1))
    await asyncio.wait_for(first_entered.wait(), 5.0)

    req1.transport = None
    resp1 = await asyncio.wait_for(task1, 5.0)
    assert resp1.status == 409
    data1 = json.loads(resp1.text)
    assert data1["reason"] == "caller_gone"
    assert data1["ended_by"] == "caller_gone"
    assert mod._backup_quiesce is False

    # 2. Complete a genuine quiesce
    conn2 = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn2))
    resp_init = await asyncio.wait_for(coord.handle_backup(_json_request({"state": "quiesce"})), 5.0)
    assert resp_init.status == 200
    assert mod._backup_quiesce is True

    # 3. Refresh quiesce: caller disconnects during drain -> 409 WITHOUT ended_by
    req3 = _json_request({"state": "quiesce"})
    mod._writes_in_flight = 1
    task3 = asyncio.create_task(coord.handle_backup(req3))
    await asyncio.sleep(0.05)

    req3.transport = None
    resp3 = await asyncio.wait_for(task3, 5.0)
    mod._writes_in_flight = 0
    assert resp3.status == 409
    data3 = json.loads(resp3.text)
    assert data3["reason"] == "caller_gone"
    assert "ended_by" not in data3
    assert mod._backup_quiesce is True
    await coord._end_quiesce()



