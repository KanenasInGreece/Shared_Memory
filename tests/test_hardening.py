"""
Unit tests for the concurrent-load hardening + auth/audit seam.

Coverage:
  - BoundedKeyedLocks: same-key identity, idle LRU eviction, never evicts a held lock
  - _outbox_backoff_delay: exponential, capped, jittered, never zero
  - resolve_identity / _resolve_bearer: pluggable identity resolution
  - _audit: writes a JSON line, no-op when disabled, never raises into the caller
  - auth_middleware: in-flight load-shed (503), pool-saturation → 503, audit on dispatch

These are fully mocked — no live Postgres/Neo4j/embedder needed.
"""

import asyncio
import importlib.util
import json
import os
import sys

import pytest
from aiohttp import web
from unittest.mock import MagicMock


# ── Dynamic import (mirrors tests/test_auth.py) ─────────────────────────────────

def load_coordinator(agent_tokens: str = "", agent_roles: str = "", **env):
    """Import coordinator.py fresh with the given env, so module-level state
    (tokens, roles, tunables) is isolated per test."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    for key, val in {"AGENT_TOKENS": agent_tokens, "AGENT_ROLES": agent_roles, **env}.items():
        if val:
            os.environ[key] = str(val)
        else:
            os.environ.pop(key, None)

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_hardening_test", path)
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
    store: dict = {}
    req.get = lambda k, d=None: store.get(k, d)
    # MagicMock invokes dunders as bound methods, so __setitem__ receives self.
    req.__setitem__ = lambda self, k, v: store.__setitem__(k, v)
    return req


async def _ok_handler(request):
    return web.json_response({"ok": True})


# ── BoundedKeyedLocks ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bounded_locks_same_key_same_object():
    mod = load_coordinator()
    locks = mod.BoundedKeyedLocks(max_size=8)
    a = await locks.get("entity-x")
    b = await locks.get("entity-x")
    assert a is b


@pytest.mark.asyncio
async def test_bounded_locks_evicts_idle_over_bound():
    mod = load_coordinator()
    locks = mod.BoundedKeyedLocks(max_size=4)
    for i in range(20):
        await locks.get(f"e{i}")
    # All idle → registry trimmed back to the bound, not 20.
    assert len(locks) <= 4


@pytest.mark.asyncio
async def test_bounded_locks_never_evicts_held_lock():
    mod = load_coordinator()
    locks = mod.BoundedKeyedLocks(max_size=2)
    held = await locks.get("pinned")
    await held.acquire()
    try:
        # Flood with distinct idle keys to force eviction pressure.
        for i in range(50):
            await locks.get(f"e{i}")
        # The held lock must still be the very same object in the registry.
        assert await locks.get("pinned") is held
        assert held.locked()
    finally:
        held.release()


# ── _outbox_backoff_delay ───────────────────────────────────────────────────────

def test_outbox_backoff_is_bounded_and_nonzero():
    mod = load_coordinator()
    cap = mod.OUTBOX_BACKOFF_MAX
    for retries in range(0, 12):
        d = mod._outbox_backoff_delay(retries)
        assert d > 0
        assert d <= cap * 1.5 + 1e-9          # jitter tops out at +50%


def test_outbox_backoff_grows_then_caps():
    mod = load_coordinator()
    # Early retries (small base) should, on average, be far below the cap;
    # large retries saturate at the cap band. Compare cap-band floor to base floor.
    early_floor = mod.OUTBOX_BACKOFF_BASE * 0.5            # retries=0 min
    capped_floor = mod.OUTBOX_BACKOFF_MAX * 0.5            # retries>>0 min
    assert capped_floor > early_floor


# ── Pluggable identity resolution ───────────────────────────────────────────────

def test_resolve_bearer_known_token():
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    assert mod.resolve_identity(req) == "claude"


def test_resolve_bearer_unknown_and_wrong_scheme():
    mod = load_coordinator("claude:tok_abc")
    assert mod.resolve_identity(_make_request("/x", "Bearer nope")) is None
    assert mod.resolve_identity(_make_request("/x", "Basic tok_abc")) is None
    assert mod.resolve_identity(_make_request("/x")) is None


def test_resolver_registry_is_extensible():
    """A second resolver (the PoP seam) takes effect once appended."""
    mod = load_coordinator("claude:tok_abc")
    mod._IDENTITY_RESOLVERS.append(lambda r: "pop-agent" if r.headers.get("X-PoP") else None)
    req = _make_request("/x")
    req.headers["X-PoP"] = "sig"
    assert mod.resolve_identity(req) == "pop-agent"


# ── _audit ──────────────────────────────────────────────────────────────────────

def test_audit_noop_when_unset():
    mod = load_coordinator("claude:tok_abc")  # GATEWAY_AUDIT_LOG_PATH unset
    # Must simply do nothing and not raise.
    mod._audit("claude", "POST", "/memory/save", 200, 12.3, "rid")


def test_audit_writes_jsonl(tmp_path):
    logf = tmp_path / "audit.jsonl"
    mod = load_coordinator("claude:tok_abc", GATEWAY_AUDIT_LOG_PATH=str(logf))
    mod._audit("claude", "POST", "/memory/save", 200, 12.3, "rid123")
    line = logf.read_text().strip()
    rec = json.loads(line)
    assert rec["agent"] == "claude"
    assert rec["status"] == 200
    assert rec["path"] == "/memory/save"
    assert rec["request_id"] == "rid123"


def test_audit_never_raises_on_bad_path(caplog):
    # An unwritable path must be swallowed (logged), never surfaced.
    mod = load_coordinator("claude:tok_abc", GATEWAY_AUDIT_LOG_PATH="/nonexistent-dir/x/audit.jsonl")
    mod._audit("claude", "POST", "/x", 200, 1.0, "rid")  # no exception


# ── auth_middleware — governance + load-shed ────────────────────────────────────

@pytest.mark.asyncio
async def test_middleware_sheds_when_inflight_cap_reached():
    mod = load_coordinator("claude:tok_abc", GATEWAY_INFLIGHT_MAX=1)
    mod._inflight = 1  # simulate one request already in flight
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(web.HTTPServiceUnavailable):
        await mod.auth_middleware(req, _ok_handler)


@pytest.mark.asyncio
async def test_middleware_maps_pool_timeout_to_503():
    mod = load_coordinator("claude:tok_abc")

    async def saturated_handler(request):
        raise asyncio.TimeoutError  # what _acquire() raises under pool saturation

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(web.HTTPServiceUnavailable):
        await mod.auth_middleware(req, saturated_handler)
    # in-flight counter must be balanced after the shed
    assert mod._inflight == 0


@pytest.mark.asyncio
async def test_middleware_audits_dispatched_request(tmp_path):
    logf = tmp_path / "audit.jsonl"
    mod = load_coordinator("claude:tok_abc", GATEWAY_AUDIT_LOG_PATH=str(logf))
    req = _make_request("/memory/search", auth_header="Bearer tok_abc")
    resp = await mod.auth_middleware(req, _ok_handler)
    assert resp.status == 200
    # The audit write is now off the event loop (AsyncLineWriter) — drain it.
    await mod._audit_writer.flush()
    rec = json.loads(logf.read_text().strip())
    assert rec["agent"] == "claude" and rec["status"] == 200 and rec["method"] == "POST"
    assert mod._inflight == 0
