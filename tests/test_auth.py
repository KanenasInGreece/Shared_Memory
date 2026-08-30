"""
Focused unit tests for Phase 2C auth middleware and token loading.

Coverage:
  - _load_agent_tokens: empty env, valid pairs, malformed entries, duplicates
  - auth_middleware: disabled (no tokens), allowlisted paths, valid token,
    missing header, wrong scheme, unknown token
  - /health and /pool/status trailing-slash spellings are NOT exempt
    (security fix A1, v0.9.76 — the exemption is exact on both limbs; the
    real-router behaviour lives in test_auth_exemption_route_resolution.py)
  - source overwrite via authenticated_agent on request
"""

import hashlib
import importlib.util
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


def _digest(token: str) -> str:
    """Matches coordinator._token_digest() — used by these tests to assert
    against the digest-keyed _AGENT_TOKENS shape (PR A2) without importing
    the module-under-test's private helper directly."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
    assert mod._AGENT_TOKENS == {_digest("tok_abc"): "claude"}


def test_load_agent_tokens_multiple_pairs():
    mod = load_coordinator("claude:tok_abc,gemini:tok_xyz")
    assert mod._AGENT_TOKENS[_digest("tok_abc")] == "claude"
    assert mod._AGENT_TOKENS[_digest("tok_xyz")] == "gemini"


def test_load_agent_tokens_skips_malformed_entry(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("claude:tok_abc,no_colon_here,gemini:tok_xyz")
    assert mod._AGENT_TOKENS[_digest("tok_abc")] == "claude"
    assert mod._AGENT_TOKENS[_digest("tok_xyz")] == "gemini"
    assert len(mod._AGENT_TOKENS) == 2
    assert "malformed" in caplog.text


def test_load_agent_tokens_skips_empty_entries():
    mod = load_coordinator("claude:tok_abc,,gemini:tok_xyz,")
    assert len(mod._AGENT_TOKENS) == 2


def test_load_agent_tokens_duplicate_token_logs_warning_first_wins(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("claude:tok_dup,gemini:tok_dup")
    # Same plaintext token -> same digest -> first mapping (claude) wins,
    # gemini is discarded as a digest collision.
    assert mod._AGENT_TOKENS[_digest("tok_dup")] == "claude"
    assert len(mod._AGENT_TOKENS) == 1
    assert "duplicate" in caplog.text.lower() or "ignoring" in caplog.text.lower()


# ── PR A2: digest-form entries, mixed registry, plaintext HARD REFUSAL ──────
# RULED (Operator, 2026-08-14): there is no accept+warn window — a plaintext
# AGENT_TOKENS entry makes the gateway refuse to START (require_no_plaintext_
# agent_tokens(), called from hive_mind_proxy.main() only). Parsing itself
# still ACCEPTS the shape (so the refusal can name exactly which agents need
# converting) — see coordinator._PLAINTEXT_AGENT_TOKENS_SEEN.

def test_load_agent_tokens_accepts_digest_form_entry():
    mod = load_coordinator(f"claude:sha256:{_digest('tok_abc')}")
    assert mod._AGENT_TOKENS == {_digest("tok_abc"): "claude"}


def test_load_agent_tokens_mixed_digest_and_plaintext():
    mod = load_coordinator(f"claude:sha256:{_digest('tok_abc')},gemini:tok_xyz")
    assert mod._AGENT_TOKENS[_digest("tok_abc")] == "claude"
    assert mod._AGENT_TOKENS[_digest("tok_xyz")] == "gemini"
    assert len(mod._AGENT_TOKENS) == 2


def test_load_agent_tokens_malformed_digest_entry_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = load_coordinator("claude:sha256:not-64-hex,gemini:tok_xyz")
    assert _digest("tok_xyz") in mod._AGENT_TOKENS
    assert len(mod._AGENT_TOKENS) == 1
    assert "malformed digest" in caplog.text.lower()


def test_load_agent_tokens_plaintext_entry_still_parses_and_verifies():
    """Parsing a plaintext entry still succeeds (and still authenticates) —
    only STARTING the real gateway with one present is refused. A bare
    `import coordinator` (every test in this repo) must never crash on a
    plaintext-configured checkout."""
    mod = load_coordinator("claude:tok_abc")
    assert mod._AGENT_TOKENS == {_digest("tok_abc"): "claude"}
    assert mod._lookup_agent_by_token("tok_abc") == "claude"


def test_load_agent_tokens_plaintext_token_containing_a_colon_is_recorded_as_plaintext():
    """Required fix (A2 security review, finding 6): a plaintext token that
    itself contains a colon (e.g. "tok:with:colons") makes split(":", 2)
    produce 3 parts, the same shape as a genuine digest entry -- the middle
    segment just isn't "sha256". This must be treated as PLAINTEXT (name =
    part 0, token = everything after the first colon, colons included),
    not dropped as malformed -- a dropped entry never reaches
    _PLAINTEXT_AGENT_TOKENS_SEEN, so the startup refusal never names it and
    the operator gets an unexplained 401 instead of the one-line fix."""
    mod = load_coordinator("claude:tok:with:colons")
    assert mod._AGENT_TOKENS == {_digest("tok:with:colons"): "claude"}
    assert mod._lookup_agent_by_token("tok:with:colons") == "claude"
    assert mod._PLAINTEXT_AGENT_TOKENS_SEEN == ["claude"]
    with pytest.raises(SystemExit, match="plaintext"):
        mod.require_no_plaintext_agent_tokens()


def test_load_agent_tokens_records_plaintext_names_seen():
    mod = load_coordinator("claude:tok_abc,gemini:tok_xyz")
    assert sorted(mod._PLAINTEXT_AGENT_TOKENS_SEEN) == ["claude", "gemini"]


def test_load_agent_tokens_digest_only_entry_records_no_plaintext_names():
    mod = load_coordinator(f"claude:sha256:{_digest('tok_abc')}")
    assert mod._PLAINTEXT_AGENT_TOKENS_SEEN == []


def test_require_no_plaintext_agent_tokens_refuses_with_plaintext_present():
    mod = load_coordinator("claude:tok_abc")
    with pytest.raises(SystemExit, match="plaintext"):
        mod.require_no_plaintext_agent_tokens()


def test_require_no_plaintext_agent_tokens_names_the_conversion_command():
    mod = load_coordinator("claude:tok_abc")
    with pytest.raises(SystemExit, match="generate_tokens.py --convert-digests"):
        mod.require_no_plaintext_agent_tokens()


def test_require_no_plaintext_agent_tokens_names_every_offending_agent():
    mod = load_coordinator("claude:tok_abc,gemini:tok_xyz")
    with pytest.raises(SystemExit) as exc_info:
        mod.require_no_plaintext_agent_tokens()
    assert "claude" in str(exc_info.value)
    assert "gemini" in str(exc_info.value)


def test_require_no_plaintext_agent_tokens_passes_with_digest_only_registry():
    mod = load_coordinator(f"claude:sha256:{_digest('tok_abc')}")
    mod.require_no_plaintext_agent_tokens()  # must not raise


def test_require_no_plaintext_agent_tokens_passes_when_auth_disabled():
    mod = load_coordinator("")
    mod.require_no_plaintext_agent_tokens()  # must not raise — nothing to refuse


def test_require_no_plaintext_agent_tokens_passes_with_mixed_registry_all_digest_after_conversion():
    """Sanity: a fully-converted registry (every entry digest form) starts
    clean even when it once had multiple agents."""
    mod = load_coordinator(
        f"claude:sha256:{_digest('tok_abc')},gemini:sha256:{_digest('tok_xyz')}"
    )
    mod.require_no_plaintext_agent_tokens()  # must not raise


# ── auth_middleware — helpers ─────────────────────────────────────────────────

def _make_request(path: str, auth_header: str | None = None, method: str = "POST") -> MagicMock:
    from yarl import URL
    req = MagicMock()
    req.path = path
    # Security fix A1 (v0.9.76 fix round): the _UNPROTECTED_PATHS exemption
    # compares `rel_url.path_safe` — the string aiohttp's router matches on —
    # not `request.path`. A double that stamps only `.path` would leave a
    # MagicMock auto-attribute here, `_router_match_path` would deny on the
    # non-str, and every exemption test would pass for the wrong reason (or
    # fail for one). Build it from a REAL yarl URL so the double derives
    # path_safe exactly as production does and cannot drift from it.
    req.rel_url = URL(path, encoded=True)
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


@pytest.mark.asyncio
async def test_auth_middleware_stays_disabled_after_daemon_token_mint():
    """CRITICAL fix (A2 security review, finding 1): an auth-unset install
    must stay UNAUTHENTICATED even after hive_mind_proxy._mint_daemon_token()
    mutates coordinator._AGENT_TOKENS in place, seconds after boot, when the
    REM/NREM watchdogs first spawn their daemons (SEC-10). The middleware's
    bypass must gate on AUTH_CONFIGURED_AT_STARTUP -- captured once, before
    any daemon token exists -- never on whether _AGENT_TOKENS happens to be
    non-empty right now.

    This mutates _AGENT_TOKENS directly (exactly what _mint_daemon_token()
    does) rather than importing hive_mind_proxy, so the assertion is about
    the middleware's gate in isolation, not the daemon-spawn plumbing
    (covered in tests/test_token_registry_digests_and_daemon_fd.py) -- and,
    unlike that file's autouse fixture, this test does NOT clear
    _AGENT_TOKENS around itself, because clearing it is exactly what masked
    this defect."""
    mod = load_coordinator("")  # auth unset at startup
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False
    assert mod._AGENT_TOKENS == {}

    # Simulate what hive_mind_proxy._mint_daemon_token() does when a daemon
    # watchdog first spawns its subprocess.
    mod._AGENT_TOKENS[_digest("ephemeral-daemon-token")] = "consolidation"
    assert mod._AGENT_TOKENS, "sanity: the registry really is non-empty now"

    req = _make_request("/memory/save")  # no Authorization header at all
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200, (
        "an auth-unset install must stay unauthenticated after daemon "
        "minting -- gating the bypass on _AGENT_TOKENS emptiness instead "
        "of AUTH_CONFIGURED_AT_STARTUP breaks every unauthenticated "
        "client the instant a daemon watchdog fires"
    )


# ── auth_middleware — allowlisted paths ───────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_middleware_health_passes_without_token():
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/health")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_auth_middleware_health_trailing_slash_is_NOT_exempt():
    """Security fix A1 (v0.9.76), REPLACING a test that CERTIFIED the defect.

    The old test asserted `/health/` passed the middleware, with a
    `_noop_handler` — so it answered "does the middleware let it through",
    which is the wrong question: the middleware letting it through IS the
    defect. The real router does not send `/health/` to handle_health; it
    falls through the catch-all into the LLM proxy, and a test that supplies
    its own handler is structurally incapable of seeing that. Measured live:
    an anonymous `GET /health/` returned llama.cpp's 404 with
    `X-SM-LLM-Backend: http://localhost:5000` and left no audit line.

    The exemption is now EXACT on both limbs — the resolved route's
    canonical, and the byte-identical path — so a trailing slash is a 401
    here. The full behaviour (including the 404 the route guard gives an
    AUTHENTICATED caller, and the auth-OFF install where this middleware
    never reaches its exemption at all) is pinned against the real router in
    tests/test_auth_exemption_route_resolution.py.
    """
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/health/")
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_auth_middleware_pool_status_trailing_slash_is_NOT_exempt():
    """The other member of _UNPROTECTED_PATHS, same rule."""
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/pool/status/")
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)


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


# ── PR A2: digest-registry verification end to end (right token / wrong token) ─

@pytest.mark.asyncio
async def test_auth_middleware_digest_entry_right_token_passes():
    mod = load_coordinator(f"claude:sha256:{_digest('tok_abc')}")
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_auth_middleware_digest_entry_wrong_token_401s():
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator(f"claude:sha256:{_digest('tok_abc')}")
    req = _make_request("/memory/save", auth_header="Bearer tok_wrong")
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_auth_middleware_mixed_registry_both_forms_authenticate():
    """A registry with one digest-form and one plaintext-legacy entry
    authenticates correctly through BOTH forms. SEC-11: plaintext is
    accepted at PARSE level (so require_no_plaintext_agent_tokens() can
    name exactly which agents need converting) — the refusal itself is a
    separate, startup-time check at the gateway entrypoint, not something
    _load_agent_tokens()/auth_middleware() enforce."""
    mod = load_coordinator(f"claude:sha256:{_digest('tok_abc')},gemini:tok_xyz")
    req1 = _make_request("/memory/save", auth_header="Bearer tok_abc")
    resp1 = await mod.auth_middleware(req1, _noop_handler)
    assert resp1.status == 200

    req2 = _make_request("/memory/save", auth_header="Bearer tok_xyz")
    resp2 = await mod.auth_middleware(req2, _noop_handler)
    assert resp2.status == 200


def test_lookup_agent_by_token_uses_hmac_compare_digest(monkeypatch):
    """Belt-and-braces (SEC-07): the digest comparison itself goes through
    hmac.compare_digest, not `==` — patch it to a spy and confirm it is
    actually invoked on a lookup, not bypassed by some other comparison."""
    mod = load_coordinator("claude:tok_abc")
    calls = []
    import hmac as hmac_module
    real_compare = hmac_module.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(mod.hmac, "compare_digest", _spy)
    assert mod._lookup_agent_by_token("tok_abc") == "claude"
    assert len(calls) >= 1


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
async def test_read_role_allows_search():
    """Search is a READ: the quiesce classification in coordinator.py already
    says so, and the allowed read-only Cypher can reach every record search
    can. Measured 2026-08-24: the first read-only MCP client on the fleet was
    403'd on search while graph_query would have answered — this pins the
    alignment. Mutation target: dropping ("POST", "/memory/search") from
    _READ_ROLE_ROUTES must kill exactly this test."""
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    req = _make_request("/memory/search", auth_header="Bearer tok_m", method="POST")
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


# ── RFC 6750 WWW-Authenticate + token_verify_failed (PR A3) ─────────────────

@pytest.mark.asyncio
async def test_missing_token_gets_bare_www_authenticate_challenge():
    """No Authorization header at all -> no token was PRESENTED, so RFC 6750
    gets the bare challenge (no error param)."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save")
    with pytest.raises(mod.web.HTTPUnauthorized) as exc_info:
        await mod.auth_middleware(req, _noop_handler)
    assert exc_info.value.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_unknown_token_gets_invalid_token_www_authenticate_challenge():
    """A token WAS presented but did not verify -> error="invalid_token"."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_wrong")
    with pytest.raises(mod.web.HTTPUnauthorized) as exc_info:
        await mod.auth_middleware(req, _noop_handler)
    assert exc_info.value.headers.get("WWW-Authenticate") == 'Bearer error="invalid_token"'


@pytest.mark.asyncio
async def test_wrong_scheme_gets_bare_www_authenticate_challenge():
    """'Basic ...' never extracts a bearer token at all -> the bare challenge,
    same as no header."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Basic tok_abc")
    with pytest.raises(mod.web.HTTPUnauthorized) as exc_info:
        await mod.auth_middleware(req, _noop_handler)
    assert exc_info.value.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_token_verify_failure_bumps_the_credential_counter():
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_wrong")
    with pytest.raises(mod.web.HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)
    assert mod._credential_counters["token_verify_failed"] == 1


@pytest.mark.asyncio
async def test_valid_token_never_bumps_the_token_verify_failed_counter():
    """MUTATION TARGET: a successful auth must leave the failure counter at
    zero — proves the counter is gated on the actual rejection branch, not
    bumped unconditionally."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200
    assert mod._credential_counters["token_verify_failed"] == 0


@pytest.mark.asyncio
async def test_token_verify_failure_logs_digest_prefix_never_the_raw_token(tmp_path):
    """SEC-08 end to end: drive a real rejected request through auth_middleware
    and confirm the presented secret never lands on disk, only its digest
    prefix — mirrors test_audit_log_never_records_the_raw_gateway_token above,
    for the credential-events log instead of the gateway audit log."""
    log_path = tmp_path / "credential-audit.jsonl"
    os.environ["CREDENTIAL_AUDIT_LOG_PATH"] = str(log_path)
    try:
        mod = load_coordinator("claude:tok_abc")
        req = _make_request("/memory/save", auth_header="Bearer tok_super_secret_presented_value")
        with pytest.raises(mod.web.HTTPUnauthorized):
            await mod.auth_middleware(req, _noop_handler)

        await mod._credential_audit_writer.flush()
        content = log_path.read_text()
        assert "tok_super_secret_presented_value" not in content
        assert '"event":"token_verify_failed"' in content
        assert f'"digest_prefix":"{_digest("tok_super_secret_presented_value")[:8]}"' in content
    finally:
        os.environ.pop("CREDENTIAL_AUDIT_LOG_PATH", None)


@pytest.mark.asyncio
async def test_missing_token_writes_no_credential_audit_line(tmp_path):
    """⚑ Security review C-1, end to end through the real middleware: a
    no-token 401 (the fully anonymous, zero-cost-to-repeat case) must never
    write a credential-audit line — only the counter moves. MUTATION
    TARGET: this is the disk-fill DoS finding; removing the no-token gate
    makes this test fail."""
    log_path = tmp_path / "credential-audit.jsonl"
    os.environ["CREDENTIAL_AUDIT_LOG_PATH"] = str(log_path)
    try:
        mod = load_coordinator("claude:tok_abc")
        req = _make_request("/memory/save")  # no Authorization header at all
        with pytest.raises(mod.web.HTTPUnauthorized):
            await mod.auth_middleware(req, _noop_handler)
        await mod._credential_audit_writer.flush()
        assert not log_path.exists()
        assert mod._credential_counters["token_verify_failed"] == 1
    finally:
        os.environ.pop("CREDENTIAL_AUDIT_LOG_PATH", None)


@pytest.mark.asyncio
async def test_repeated_no_token_requests_never_write_any_credential_audit_line(tmp_path):
    """Same property under a flood — the scenario the finding actually
    describes (a loop of anonymous 401s)."""
    log_path = tmp_path / "credential-audit.jsonl"
    os.environ["CREDENTIAL_AUDIT_LOG_PATH"] = str(log_path)
    try:
        mod = load_coordinator("claude:tok_abc")
        for _ in range(50):
            req = _make_request("/memory/save")
            with pytest.raises(mod.web.HTTPUnauthorized):
                await mod.auth_middleware(req, _noop_handler)
        await mod._credential_audit_writer.flush()
        assert not log_path.exists()
        assert mod._credential_counters["token_verify_failed"] == 50
    finally:
        os.environ.pop("CREDENTIAL_AUDIT_LOG_PATH", None)


# ── O-5: the gateway's own 401 also carries X-SM-Fault-Origin: gateway ──────

@pytest.mark.asyncio
async def test_missing_token_401_carries_gateway_fault_origin_header():
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save")
    with pytest.raises(mod.web.HTTPUnauthorized) as exc_info:
        await mod.auth_middleware(req, _noop_handler)
    assert exc_info.value.headers.get("X-SM-Fault-Origin") == "gateway"


@pytest.mark.asyncio
async def test_unknown_token_401_carries_gateway_fault_origin_header():
    """MUTATION TARGET: both the WWW-Authenticate header AND
    X-SM-Fault-Origin must be present together, not one or the other."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", auth_header="Bearer tok_wrong")
    with pytest.raises(mod.web.HTTPUnauthorized) as exc_info:
        await mod.auth_middleware(req, _noop_handler)
    assert exc_info.value.headers.get("X-SM-Fault-Origin") == "gateway"
    assert exc_info.value.headers.get("WWW-Authenticate") == 'Bearer error="invalid_token"'


# ── The read-only roster is ENFORCED, not merely declared ─────────────────────
#
# Operator ruling, 2026-08-23: "always read only, enforced", and then — because
# the roster will grow — "we may have other agents read only. so keep the code
# enforcing anything that is supposed to be read only."
#
# The defect this replaces: READ_ONLY_AGENTS lived only in generate_tokens.py, so
# being read-only was a MINTING CONVENTION. The gateway believed AGENT_ROLES, and
# absence from that line means FULL read/write — so an identity registered before
# the rule was honoured on a given path, a partial write, or an older tool
# rewriting the line all silently WIDENED a read-only agent. A guarantee a file
# edit can switch off is not a guarantee.
#
# These tests assert the REQUIREMENT (a roster agent cannot write) rather than
# the mechanism, so they survive any reshuffling of where the roster is stored.


@pytest.mark.asyncio
async def test_roster_agent_is_denied_writes_with_no_agent_roles_entry():
    """THE regression. No declaration at all — the widest possible file state."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="")
    req = _make_request("/memory/save", auth_header="Bearer tok_m", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_roster_agent_is_denied_writes_even_when_declared_full():
    """A .env that claims the monitor is unconfined does not make it so."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:full")
    req = _make_request("/memory/save", auth_header="Bearer tok_m", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_roster_agent_is_denied_writes_even_when_declared_admin():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:admin")
    req = _make_request("/memory/save", auth_header="Bearer tok_m", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_roster_agent_still_reaches_its_read_routes_with_no_entry():
    """Enforcement must confine, not lock out — the monitor still has a job."""
    mod = load_coordinator("monitor:tok_m", agent_roles="")
    req = _make_request("/memory/telemetry", auth_header="Bearer tok_m", method="GET")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_an_agent_confined_by_the_environment_roster_is_enforced(monkeypatch):
    """The roster is extensible: a deployment confines an identity this
    framework has never heard of, without editing a shipped file."""
    from aiohttp.web_exceptions import HTTPForbidden
    monkeypatch.setenv("SHARED_MEMORY_READ_ONLY_AGENTS", "dashboard")
    mod = load_coordinator("dashboard:tok_d", agent_roles="")
    req = _make_request("/memory/save", auth_header="Bearer tok_d", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_an_operator_declared_read_agent_is_still_confined():
    """Declaration remains a way in — the roster cannot enumerate every name."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("dashboard:tok_d", agent_roles="dashboard:read")
    req = _make_request("/memory/save", auth_header="Bearer tok_d", method="POST")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_an_ordinary_agent_is_not_confined_by_the_roster():
    """The counterweight: enforcement must not quietly confine everyone. Without
    this, pinning every agent to 'read' would pass every test above."""
    mod = load_coordinator("claude:tok_c", agent_roles="")
    req = _make_request("/memory/save", auth_header="Bearer tok_c", method="POST")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200
