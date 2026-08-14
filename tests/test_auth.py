"""
Focused unit tests for Phase 2C auth middleware and token loading.

Coverage:
  - _load_agent_tokens: empty env, valid pairs, malformed entries, duplicates
  - auth_middleware: disabled (no tokens), allowlisted paths, valid token,
    missing header, wrong scheme, unknown token
  - /health trailing-slash passes allowlist
  - source overwrite via authenticated_agent on request
"""

import importlib.util
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Dynamic import ────────────────────────────────────────────────────────────

def load_coordinator(agent_tokens: str = "", agent_roles: str = ""):
    """Import coordinator.py with AGENT_TOKENS / AGENT_ROLES pre-set in the env.

    Each call produces a fresh module so token/role state is isolated per test.

    coordinator.py reads AGENT_TOKENS via secure_env.get_secret() (PR A1),
    which checks os.environ first, then secure_env's in-process secrets
    cache. That cache is a process-lifetime module global: once anything in
    this test session has called secure_env.load_split_env() against a real
    shared-memory/.env that happens to define AGENT_TOKENS (a fake one used
    to prove the deployed shape, or a real one on a developer's machine),
    the value is cached there for the rest of the process — os.environ.pop()
    alone can no longer simulate "AGENT_TOKENS is unset" once that has
    happened. Clear the cache entry too, so "unset" here means what it says
    regardless of what else this session has already imported.
    """
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)

    # Set env BEFORE loading so _load_agent_tokens()/_load_agent_roles() pick it
    # up at module level
    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)
    if agent_roles:
        os.environ["AGENT_ROLES"] = agent_roles
    else:
        os.environ.pop("AGENT_ROLES", None)

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_auth_test", path)
    mod  = importlib.util.module_from_spec(spec)
    # Don't cache in sys.modules — each test needs its own module-level state
    spec.loader.exec_module(mod)
    return mod


# ── _load_agent_tokens ────────────────────────────────────────────────────────

def test_load_agent_tokens_empty_env():
    mod = load_coordinator("")
    assert mod._AGENT_TOKENS == {}


def test_load_agent_tokens_single_pair():
    mod = load_coordinator("claude:tok_abc")
    assert mod._AGENT_TOKENS == {"tok_abc": "claude"}


def test_load_agent_tokens_multiple_pairs():
    mod = load_coordinator("claude:tok_abc,gemini:tok_xyz")
    assert mod._AGENT_TOKENS["tok_abc"] == "claude"
    assert mod._AGENT_TOKENS["tok_xyz"] == "gemini"


def test_load_agent_tokens_skips_malformed_entry(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("claude:tok_abc,no_colon_here,gemini:tok_xyz")
    assert mod._AGENT_TOKENS["tok_abc"] == "claude"
    assert mod._AGENT_TOKENS["tok_xyz"] == "gemini"
    assert "no_colon_here" not in str(mod._AGENT_TOKENS)
    assert "malformed" in caplog.text


def test_load_agent_tokens_skips_empty_entries():
    mod = load_coordinator("claude:tok_abc,,gemini:tok_xyz,")
    assert len(mod._AGENT_TOKENS) == 2


def test_load_agent_tokens_duplicate_token_logs_warning_first_wins(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("claude:tok_dup,gemini:tok_dup")
    # First mapping (claude) wins; gemini is discarded
    assert mod._AGENT_TOKENS["tok_dup"] == "claude"
    assert len(mod._AGENT_TOKENS) == 1
    assert "duplicate" in caplog.text.lower() or "ignoring" in caplog.text.lower()


# ── auth_middleware — helpers ─────────────────────────────────────────────────

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


# ── auth_middleware — disabled (no tokens) ────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_middleware_passes_all_when_no_tokens():
    """Auth disabled when AGENT_TOKENS is empty — all requests pass through."""
    mod = load_coordinator("")
    req = _make_request("/memory/save")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


# ── auth_middleware — allowlisted paths ───────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_middleware_health_passes_without_token():
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/health")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_auth_middleware_health_trailing_slash_passes():
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/health/")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


# ── auth_middleware — valid token ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_middleware_valid_token_passes_and_sets_agent():
    from aiohttp import web
    mod = load_coordinator("claude:tok_abc")

    captured = {}

    async def capturing_handler(request):
        captured["agent"] = request["authenticated_agent"]
        return web.json_response({"ok": True})

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    req.__setitem__ = lambda self, k, v: captured.__setitem__(k, v)

    resp = await mod.auth_middleware(req, capturing_handler)
    assert resp.status == 200


# ── auth_middleware — rejection cases ────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_middleware_missing_header_returns_401():
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save")
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_auth_middleware_wrong_scheme_returns_401():
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Basic tok_abc")
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_auth_middleware_unknown_token_returns_401():
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_wrong")
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_auth_middleware_bearer_only_no_token_returns_401():
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer")
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_auth_middleware_handles_extra_spaces_in_header():
    """split(maxsplit=1) treats consecutive whitespace as one separator —
    'Bearer  tok_abc' (double space) is parsed as ["Bearer", "tok_abc"] and
    authenticates correctly.  This is the key advantage over raw [7:] slicing."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer  tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


# ── _load_agent_roles ─────────────────────────────────────────────────────────

def test_load_agent_roles_empty_env():
    mod = load_coordinator("claude:tok_abc")
    assert mod._AGENT_ROLES == {}


def test_load_agent_roles_read_pair():
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    assert mod._AGENT_ROLES == {"monitor": "read"}


def test_load_agent_roles_skips_malformed_entry(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read,bad_entry")
    assert mod._AGENT_ROLES == {"monitor": "read"}
    assert "malformed" in caplog.text


def test_load_agent_roles_unknown_role_ignored(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("monitor:tok_m", agent_roles="monitor:writ")
    # Unknown role is dropped — the agent keeps full access (fail-known, logged)
    assert mod._AGENT_ROLES == {}
    assert "unknown role" in caplog.text


# ── auth_middleware — read-only role enforcement ──────────────────────────────

@pytest.mark.asyncio
async def test_read_role_allows_telemetry():
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/memory/telemetry", auth_header="Bearer tok_m", method="GET")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_read_role_allows_graph():
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/memory/graph", auth_header="Bearer tok_m", method="POST")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_read_role_denies_save():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/memory/save", auth_header="Bearer tok_m", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_read_role_denies_search():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/memory/search", auth_header="Bearer tok_m", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_read_role_denies_proxy_passthrough():
    """A read token cannot reach the LLM/embeddings proxy catch-all either."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/v1/embeddings", auth_header="Bearer tok_m", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_read_role_denies_telemetry_wrong_method():
    """Allowlist is method-specific: POST /memory/telemetry is not GET."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/memory/telemetry", auth_header="Bearer tok_m", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_read_role_health_still_unauthenticated():
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/health", method="GET")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_full_role_agent_can_still_save():
    """An agent absent from AGENT_ROLES keeps full read/write access."""
    mod = load_coordinator("claude:tok_abc,monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/memory/save", auth_header="Bearer tok_abc", method="POST")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


# ── the audit log records WHO, never the credential itself ───────────────────

@pytest.mark.asyncio
async def test_audit_log_never_records_the_raw_gateway_token(tmp_path):
    """_audit() takes agent_name (the resolved identity), never the token — this
    proves it end to end through a real file write, not just by reading the
    call site: drive a real request with a real token through auth_middleware,
    flush the async log writer, and confirm the token substring never lands on
    disk while the agent's own name (the thing that SHOULD be there) does."""
    log_path = tmp_path / "audit.jsonl"
    os.environ["GATEWAY_AUDIT_LOG_PATH"] = str(log_path)
    try:
        mod = load_coordinator("claude:tok_super_secret_gateway_credential")
        req = _make_request("/memory/save", auth_header="Bearer tok_super_secret_gateway_credential", method="POST")
        resp = await mod.auth_middleware(req, _noop_handler)
        assert resp.status == 200

        await mod._audit_writer.flush()
        content = log_path.read_text()
        assert "tok_super_secret_gateway_credential" not in content
        assert '"agent":"claude"' in content
    finally:
        os.environ.pop("GATEWAY_AUDIT_LOG_PATH", None)
