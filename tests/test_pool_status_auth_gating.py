"""SEC-A5-01 (PR A5 fix round): /pool/status used to disclose the full
backend roster + per-backend pool state (an idle/busy oracle) to any
anonymous caller — S-10 moved this detail behind auth on /health but left
/pool/status, the OTHER member of coordinator._UNPROTECTED_PATHS, untouched.
Gated the same way as /health, including SEC-A5-03's condition (slimming
applies ONLY when AUTH_CONFIGURED_AT_STARTUP is true).

Also covers the HARD REQUIREMENT: every real internal caller of /pool/status
(pool_status.pool_has_free_slot(), consumed by rem_loop.py and
consolidation_loop.py) must send its daemon/agent token, so slot-awareness is
never silently lost."""
import asyncio
import importlib
import json
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _load_gateway(monkeypatch, agent_tokens: str = ""):
    """Same reload-coordinator-first pattern as test_health_anonymous_
    slimming.py's _load_gateway — AUTH_CONFIGURED_AT_STARTUP must reflect
    `agent_tokens` exactly, not whatever an earlier test file left behind."""
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    if agent_tokens:
        monkeypatch.setenv("AGENT_TOKENS", agent_tokens)
    else:
        monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def _req(agent_token=None):
    class _Req(dict):
        pass
    r = _Req()
    r.headers = {"Authorization": f"Bearer {agent_token}"} if agent_token else {}
    r.app = {}
    return r


def test_anonymous_caller_gets_empty_object_when_auth_configured(monkeypatch):
    """MUTATION TARGET (SEC-A5-01): the core fix."""
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_pool_gate_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    body = json.loads(asyncio.run(g.handle_pool_status(_req())).body)
    assert body == {}
    assert "backends" not in body
    assert "free_slots" not in body


def test_authenticated_caller_gets_full_roster_when_auth_configured(monkeypatch):
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_pool_full_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    body = json.loads(asyncio.run(g.handle_pool_status(_req("tok_pool_full_test"))).body)
    assert "free_slots" in body
    assert "backends" in body
    assert "http://a:5000" in body["backends"]


def test_auth_off_install_gets_full_roster_for_everyone(monkeypatch):
    """SEC-A5-03's condition applied to /pool/status too: an auth-off
    install has no token registry, so slimming would lock it out
    permanently — and there is nothing on it for the slimming to protect."""
    g = _load_gateway(monkeypatch, agent_tokens="")
    assert g.AUTH_CONFIGURED_AT_STARTUP is False

    body = json.loads(asyncio.run(g.handle_pool_status(_req())).body)
    assert "free_slots" in body
    assert "backends" in body


def test_none_request_still_works_when_auth_off():
    """Existing unit-test callers (tests/test_pool_status.py) invoke this
    handler with request=None directly -- must not crash regardless of
    auth state; covered for the auth-off default here."""
    import hive_mind_proxy as g
    body = json.loads(asyncio.run(g.handle_pool_status(None)).body)
    assert isinstance(body, dict)


# ── HARD REQUIREMENT: every real internal caller sends its token ────────────

def test_pool_has_free_slot_forwards_headers_to_the_gateway(monkeypatch):
    """Functional proof for pool_status.pool_has_free_slot() itself -- the
    one function both rem_loop.py and consolidation_loop.py call through."""
    import pool_status
    importlib.reload(pool_status)

    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"free_slots": 1}

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None):
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(pool_status.httpx, "AsyncClient", lambda timeout: _FakeClient())
    result = asyncio.run(pool_status.pool_has_free_slot(headers={"Authorization": "Bearer daemon-tok"}))
    assert result is True
    assert captured["headers"] == {"Authorization": "Bearer daemon-tok"}


def test_pool_has_free_slot_defaults_to_empty_headers_when_omitted(monkeypatch):
    """Backward compatible for a standalone/debug caller with no token --
    matches pool_status.py's own documented fallback story."""
    import pool_status
    importlib.reload(pool_status)

    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"free_slots": 1}

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None):
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(pool_status.httpx, "AsyncClient", lambda timeout: _FakeClient())
    asyncio.run(pool_status.pool_has_free_slot())
    assert captured["headers"] == {}


def _code_lines(relpath: str) -> list[str]:
    """Source lines with comments/blank lines stripped -- a call site is a
    real invocation, never a mention inside a comment."""
    path = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", relpath)
    with open(path) as f:
        lines = f.readlines()
    return [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _real_call_sites(relpath: str) -> list[str]:
    calls = []
    for ln in _code_lines(relpath):
        code = ln.split("#", 1)[0]  # drop a trailing inline comment too
        calls.extend(re.findall(r"pool_has_free_slot\([^)]*\)", code))
    return calls


def test_every_pool_has_free_slot_call_site_in_rem_loop_sends_headers():
    """Source-level completeness check (call-site inventory, PR A5 fix
    round hard requirement) -- every REAL pool_has_free_slot(...) call in
    rem_loop.py must pass headers=, so REM never silently loses real
    slot-awareness. Complements the functional proof above (that the
    shared function actually forwards whatever it's given). Comment
    mentions are excluded -- this counts invocations, not prose."""
    calls = _real_call_sites("rem_loop.py")
    assert calls, "expected at least one pool_has_free_slot(...) call site in rem_loop.py"
    for call in calls:
        assert "headers=" in call, f"call site missing headers=: {call!r}"


def test_every_pool_has_free_slot_call_site_in_consolidation_loop_sends_headers():
    calls = _real_call_sites("consolidation_loop.py")
    assert len(calls) >= 4, f"expected >=4 pool_has_free_slot(...) call sites, found {len(calls)}"
    for call in calls:
        assert "headers=" in call, f"call site missing headers=: {call!r}"


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: dropping the `AUTH_CONFIGURED_AT_
# STARTUP and not bool(...)` gate in handle_pool_status (always returning the
# full body) makes test_anonymous_caller_gets_empty_object_when_auth_
# configured fail (the roster appears anonymously again).
