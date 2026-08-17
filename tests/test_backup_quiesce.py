"""
Unit tests for the backup quiesce seam (step 1 of the cross-DB backup feature).

Coverage:
  - _load_agent_roles accepts the new "admin" role
  - backup_quiesce_active() mirrors the module flag
  - auth_middleware while quiesced: write routes shed 503 + Retry-After,
    read routes still flow
  - admin-route gating: only an admin-role token reaches /admin/backup, and an
    admin token is confined to /admin/* (cannot save/search)
  - MemoryCoordinator._begin_quiesce / _end_quiesce flag + advisory-lock lifecycle
    (asyncpg.connect mocked — no live DB), idempotent re-quiesce
  - TTL auto-resume clears the flag if no resume arrives
  - handle_backup status routing (200 drained / 202 drain_timeout / 400 bad state)

All mocked — no live infrastructure.
"""

import asyncio
import importlib.util
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Dynamic import (mirrors test_auth.load_coordinator) ───────────────────────

def load_coordinator(agent_tokens: str = "", agent_roles: str = ""):
    """Fresh coordinator module with AGENT_TOKENS / AGENT_ROLES pre-set."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)
    if agent_roles:
        os.environ["AGENT_ROLES"] = agent_roles
    else:
        os.environ.pop("AGENT_ROLES", None)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_backup_test", path)
    mod  = importlib.util.module_from_spec(spec)
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
    return req


async def _noop_handler(request):
    from aiohttp import web
    return web.json_response({"ok": True})


# ── role parsing ──────────────────────────────────────────────────────────────

def test_load_agent_roles_accepts_admin():
    mod = load_coordinator("backup:tok_b", agent_roles="backup:admin")
    assert mod._AGENT_ROLES == {"backup": "admin"}


def test_load_agent_roles_unknown_still_rejected(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("x:tok_x", agent_roles="x:superuser")
    assert mod._AGENT_ROLES == {}
    assert "unknown role" in caplog.text


# ── backup_quiesce_active accessor ────────────────────────────────────────────

def test_backup_quiesce_active_mirrors_flag():
    mod = load_coordinator("claude:tok_abc")
    assert mod.backup_quiesce_active() is False
    mod._backup_quiesce = True
    assert mod.backup_quiesce_active() is True


# ── write-shed while quiesced ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiesced_sheds_save_with_retry_after():
    from aiohttp.web_exceptions import HTTPServiceUnavailable
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPServiceUnavailable) as exc:
        await mod.auth_middleware(req, _noop_handler)
    assert exc.value.headers["Retry-After"] == str(mod.BACKUP_RETRY_AFTER)


@pytest.mark.asyncio
async def test_quiesced_sheds_retrospective():
    from aiohttp.web_exceptions import HTTPServiceUnavailable
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    req = _make_request("/memory/retrospective", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_quiesced_allows_search_read():
    """Reads must keep flowing while a backup runs."""
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    req = _make_request("/memory/search", auth_header="Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_quiesced_allows_telemetry_read():
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    req = _make_request("/memory/telemetry", auth_header="Bearer tok_abc", method="GET")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_not_quiesced_allows_save():
    mod = load_coordinator("claude:tok_abc")
    assert mod._backup_quiesce is False
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


# ── admin-route gating ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_route_requires_admin_role_full_denied():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("claude:tok_abc")  # default role = full
    req = _make_request("/admin/backup", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_admin_route_read_denied():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/admin/backup", auth_header="Bearer tok_m")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_admin_route_admin_token_passes():
    mod = load_coordinator("backup:tok_b", agent_roles="backup:admin")
    req = _make_request("/admin/backup", auth_header="Bearer tok_b")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_admin_token_confined_cannot_save():
    """Least privilege: an admin token reaches only /admin/* — not /memory/save."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("backup:tok_b", agent_roles="backup:admin")
    req = _make_request("/memory/save", auth_header="Bearer tok_b")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


# ── coordinator quiesce lifecycle (asyncpg.connect mocked) ────────────────────

def _mock_conn():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_begin_quiesce_sets_flag_and_holds_lock(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    drained = await coord._begin_quiesce(max_seconds=30)
    assert drained is True
    assert mod._backup_quiesce is True
    assert coord._quiesce_conn is conn
    # Exclusive advisory lock acquired on the dedicated connection.
    calls = [c.args[0] for c in conn.execute.await_args_list]
    assert any("pg_advisory_lock" in s for s in calls)

    await coord._end_quiesce()
    assert mod._backup_quiesce is False
    assert coord._quiesce_conn is None
    conn.close.assert_awaited()


@pytest.mark.asyncio
async def test_begin_quiesce_idempotent_single_connection(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    conn = _mock_conn()
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(mod.asyncpg, "connect", connect)
    coord = mod.MemoryCoordinator()

    assert await coord._begin_quiesce(30) is True
    assert await coord._begin_quiesce(30) is True  # re-entrant — must NOT reconnect
    assert connect.await_count == 1
    await coord._end_quiesce()


@pytest.mark.asyncio
async def test_begin_quiesce_drain_timeout_returns_false(monkeypatch):
    """lock_timeout firing on the exclusive acquire → drain_timeout, no held conn."""
    mod = load_coordinator("claude:tok_abc")
    conn = _mock_conn()

    async def execute(sql, *args):
        if "pg_advisory_lock" in sql:
            raise mod.asyncpg.PostgresError("canceling statement due to lock timeout")
        return "SET"

    conn.execute = AsyncMock(side_effect=execute)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    drained = await coord._begin_quiesce(30)
    assert drained is False
    assert mod._backup_quiesce is True          # client writes still shed
    assert coord._quiesce_conn is None          # no half-held lock
    conn.close.assert_awaited()
    await coord._end_quiesce()


@pytest.mark.asyncio
async def test_ttl_auto_resume_clears_flag(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    conn = _mock_conn()
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    coord = mod.MemoryCoordinator()

    await coord._begin_quiesce(max_seconds=0.05)
    assert mod._backup_quiesce is True
    await asyncio.sleep(0.15)                    # TTL expires → auto-resume
    assert mod._backup_quiesce is False
    assert coord._quiesce_conn is None


# ── handle_backup status routing ──────────────────────────────────────────────

def _json_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


@pytest.mark.asyncio
async def test_handle_backup_quiesce_drained_returns_200(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    coord._begin_quiesce = AsyncMock(return_value=True)
    resp = await coord.handle_backup(_json_request({"state": "quiesce"}))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_handle_backup_quiesce_drain_timeout_returns_202(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    coord._begin_quiesce = AsyncMock(return_value=False)
    resp = await coord.handle_backup(_json_request({"state": "quiesce"}))
    assert resp.status == 202


@pytest.mark.asyncio
async def test_handle_backup_resume_returns_200():
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    coord._end_quiesce = AsyncMock()
    resp = await coord.handle_backup(_json_request({"state": "resume"}))
    assert resp.status == 200
    coord._end_quiesce.assert_awaited()


@pytest.mark.asyncio
async def test_handle_backup_bad_state_returns_400():
    mod = load_coordinator("claude:tok_abc")
    coord = mod.MemoryCoordinator()
    resp = await coord.handle_backup(_json_request({"state": "nope"}))
    assert resp.status == 400


# ── /health surfaces backup_in_progress (step 2) ──────────────────────────────

@pytest.mark.asyncio
async def test_health_surfaces_backup_in_progress():
    """hive_mind_proxy.handle_health reflects the real coordinator quiesce flag.

    `backup_in_progress` is authenticated-only as of S-10 (v0.9.9) — the
    request needs a token that resolves against a real _AGENT_TOKENS entry,
    same technique as tests/test_llm_backend_secrets.py's health tests."""
    import hashlib
    import json as _json

    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import coordinator as real_coord
    import hive_mind_proxy as proxy_mod

    class _Resp:
        status = 200

    class _Cm:
        async def __aenter__(self):
            return _Resp()
        async def __aexit__(self, *a):
            return False

    class _Session:
        def get(self, url, timeout=None):
            return _Cm()

    proxy = MagicMock()
    proxy.session = _Session()
    req = MagicMock()
    req.app = {"proxy": proxy}
    req.headers = {"Authorization": "Bearer tok_backup_quiesce_test"}
    digest = hashlib.sha256(b"tok_backup_quiesce_test").hexdigest()
    real_coord._AGENT_TOKENS[digest] = "claude"

    try:
        # S-11's TTL cache (proxy_mod._health_cache) sits UNDER backup_in_progress
        # too — reset it before each call so this test observes the flag flip
        # immediately rather than a cached probe from moments earlier.
        real_coord._backup_quiesce = False
        proxy_mod._health_cache["checks"] = None
        body = _json.loads((await proxy_mod.handle_health(req)).body.decode())
        assert body["backup_in_progress"] is False

        real_coord._backup_quiesce = True
        proxy_mod._health_cache["checks"] = None
        body = _json.loads((await proxy_mod.handle_health(req)).body.decode())
        assert body["backup_in_progress"] is True
    finally:
        real_coord._backup_quiesce = False
        real_coord._AGENT_TOKENS.pop(digest, None)
        proxy_mod._health_cache["checks"] = None


# ── daemon advisory-lock gate (step 3) ────────────────────────────────────────

def _scripts_on_path():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


class _FakeCursor:
    def __init__(self, val):
        self._val = val
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        pass
    def fetchone(self):
        return [self._val]


def test_advisory_lock_key_matches_across_modules():
    """The whole fence breaks if the gateway and daemons disagree on the key."""
    _scripts_on_path()
    import coordinator, rem_loop, consolidation_loop
    assert (
        coordinator.BACKUP_ADVISORY_LOCK_KEY
        == rem_loop.BACKUP_ADVISORY_LOCK_KEY
        == consolidation_loop.BACKUP_ADVISORY_LOCK_KEY
        == 8765309
    )


def test_rem_take_shared_lock_acquired():
    _scripts_on_path()
    import rem_loop

    class _Conn:
        def cursor(self):
            return _FakeCursor(True)

    assert rem_loop._take_shared_backup_lock(_Conn()) is True


def test_rem_take_shared_lock_denied_when_backup_holds_exclusive():
    _scripts_on_path()
    import rem_loop

    class _Conn:
        def cursor(self):
            return _FakeCursor(False)

    assert rem_loop._take_shared_backup_lock(_Conn()) is False


def test_consolidation_try_lock_returns_conn_when_free(monkeypatch):
    _scripts_on_path()
    import consolidation_loop as cl

    class _Conn:
        closed = False
        def set_isolation_level(self, *a):
            pass
        def cursor(self):
            return _FakeCursor(True)
        def close(self):
            self.closed = True

    conn = _Conn()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)
    got = cl._try_backup_shared_lock()
    assert got is conn
    assert conn.closed is False          # caller releases by closing later


def test_consolidation_try_lock_returns_none_and_closes_when_held(monkeypatch):
    _scripts_on_path()
    import consolidation_loop as cl

    closed = {"n": 0}

    class _Conn:
        def set_isolation_level(self, *a):
            pass
        def cursor(self):
            return _FakeCursor(False)
        def close(self):
            closed["n"] += 1

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Conn())
    assert cl._try_backup_shared_lock() is None
    assert closed["n"] == 1               # no leaked connection when skipping
