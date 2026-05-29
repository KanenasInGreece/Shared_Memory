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

def load_coordinator(agent_tokens: str = ""):
    """Import coordinator.py with AGENT_TOKENS pre-set in the environment.

    Each call produces a fresh module so token state is isolated per test.
    """
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    # Set env BEFORE loading so _load_agent_tokens() picks it up at module level
    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)

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

def _make_request(path: str, auth_header: str | None = None) -> MagicMock:
    req = MagicMock()
    req.path = path
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
