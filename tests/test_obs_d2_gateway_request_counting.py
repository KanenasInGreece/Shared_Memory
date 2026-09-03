"""D2 (OBS round, v2 brief) — count every request; time only real work (R-C).

THE DEFECT (verified by both adversarial reviewers against main=0714ebd):
nine early exits in `auth_middleware` (the load-shed valve, the auth-off
bypass, the unprotected-path exemption, the gateway's own 401, three role/
admin 403s, the backup-quiesce 503, and the no-principal 403) all preceded
the single `_record_gateway_request` call — so the gateway's OWN 401/403/503
responses, and all auth-off traffic, were invisible to `gateway.requests_
total`/`gateway.by_status.*`.

PROVE-FIRST evidence for the by_status zeros is embedded in each test below:
every scenario asserts the pre-condition (fresh counters at 0) before
driving the request, so a reader can see the counters were genuinely zero
going in — on the OLD code these would stay zero after a shed/401/403/503
exit; on the FIXED code (what actually runs here) they move by exactly one.

Uses the same `load_coordinator()` (isolated module via
spec_from_file_location) + `auth_middleware(req, handler)` pattern already
established by tests/test_backup_quiesce.py and
tests/test_load_shed_before_auth_exemption.py — this is `auth_middleware`
in-process, no server, no port bound.

R-C: the latency ring feeds ONLY the old boundary (the authenticated,
handler-reached path where `started` is taken) — every early-exit test
below pins the ring's `window` at 0 alongside the status-counting move.
"""
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def load_coordinator(agent_tokens: str = "", agent_roles: str = "",
                      gateway_inflight_max: str = "",
                      gateway_require_principal: str = ""):
    """Fresh, isolated coordinator module per call (mirrors test_auth.py /
    test_backup_quiesce.py / test_load_shed_before_auth_exemption.py) — so
    gateway counters, `_inflight`, `_backup_quiesce` and every other module
    global never leak between tests or from any other file's shared
    `coordinator` import."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)

    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)
    if agent_roles:
        os.environ["AGENT_ROLES"] = agent_roles
    else:
        os.environ.pop("AGENT_ROLES", None)
    if gateway_inflight_max:
        os.environ["GATEWAY_INFLIGHT_MAX"] = gateway_inflight_max
    else:
        os.environ.pop("GATEWAY_INFLIGHT_MAX", None)
    if gateway_require_principal:
        os.environ["GATEWAY_REQUIRE_PRINCIPAL"] = gateway_require_principal
    else:
        os.environ.pop("GATEWAY_REQUIRE_PRINCIPAL", None)

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location(
        f"coordinator_d2_test_{id(object())}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_request(path: str, auth_header: str | None = None, method: str = "POST",
                   unprotected: bool = False) -> MagicMock:
    """Mirrors test_backup_quiesce.py's `_make_request`, plus
    test_load_shed_before_auth_exemption.py's `rel_url` stamping when the
    scenario needs the _UNPROTECTED_PATHS exemption to actually grant
    (`_router_match_path` reads `request.rel_url.path_safe` defensively —
    a bare MagicMock auto-attribute there is not a `str` and correctly
    DENIES the exemption, so a test exercising /health's unprotected
    branch must stamp a real `yarl.URL`)."""
    req = MagicMock()
    req.path = path
    req.method = method
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    req.headers = headers
    req.get = MagicMock(return_value=None)
    req.__setitem__ = MagicMock()
    req.transport = None  # TCP/no kernel principal, same as every other double here
    if unprotected:
        from yarl import URL
        req.rel_url = URL(path, encoded=True)
    return req


async def _noop_handler(request):
    from aiohttp import web
    return web.json_response({"ok": True})


def _counters(mod):
    return mod.telemetry_gateway_counters()


def _ring_window(mod):
    return mod._gateway_latency.snapshot()["window"]


# ══════════════════════════════════════════════════════════════════════════
# One call site, nine early exits + the happy path — exactly one record each
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_shed_503_counted_no_latency_entry():
    """A: the load-shed valve. Prove-first: requests_total/by_status start
    at 0 (fresh module); after the shed, both moved by exactly one and the
    latency ring — R-C's old boundary — is untouched."""
    from aiohttp.web_exceptions import HTTPServiceUnavailable
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1  # already at the cap
    assert _counters(mod)["requests_total"] == 0
    assert mod._gateway_by_status["503"] == 0

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)

    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["503"] == 1
    assert mod._gateway_by_status["5xx"] == 1
    assert _ring_window(mod) == 0
    mod._inflight = 0


@pytest.mark.asyncio
async def test_auth_off_counted_no_audit_no_error():
    """B: the auth-off bypass. `_audit` must fire exactly as today — never,
    on this path — proven by patching it with a MagicMock and asserting it
    was never called, alongside the new counting."""
    mod = load_coordinator("")  # no AGENT_TOKENS at all -> auth-off
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False
    mod._audit = MagicMock()
    assert _counters(mod)["requests_total"] == 0

    req = _make_request("/memory/search", method="POST")
    resp = await mod.auth_middleware(req, _noop_handler)

    assert resp.status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert _ring_window(mod) == 0
    mod._audit.assert_not_called()


@pytest.mark.asyncio
async def test_unprotected_stale_bearer_counted_200_by_status_401_unmoved():
    """C: /health with a bearer that fails to verify. The RESPONSE stays the
    same anonymous 200 it always was (ADV2-1's byte-identical contract,
    untouched by this round) — so it lands in by_status.2xx, and
    by_status.401 must NOT move: that 401-shaped signal already reached
    credentials.token_verify_failed and the D1 ring."""
    mod = load_coordinator("claude:tok_abc")
    assert _counters(mod)["requests_total"] == 0
    assert mod._gateway_by_status["401"] == 0

    req = _make_request("/health", auth_header="Bearer tok_bad", method="GET",
                         unprotected=True)
    resp = await mod.auth_middleware(req, _noop_handler)

    assert resp.status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert mod._gateway_by_status["401"] == 0, "the gateway's own 401 bucket must be unmoved"
    assert _ring_window(mod) == 0
    # The D1 ring is the surface that DOES move for this event.
    assert len(mod.telemetry_token_verify_ring()) == 1


@pytest.mark.asyncio
async def test_unprotected_path_with_no_bearer_at_all_counted_once():
    """C, the common case: an anonymous /health poll with no Authorization
    header at all — must still be counted exactly once (no token-oracle
    probe to audit, so the D1 ring stays empty)."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/health", method="GET", unprotected=True)
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert len(mod.telemetry_token_verify_ring()) == 0


@pytest.mark.asyncio
async def test_gateways_own_401_counted():
    """D: no token at all on a protected route."""
    from aiohttp.web_exceptions import HTTPUnauthorized
    mod = load_coordinator("claude:tok_abc")
    assert _counters(mod)["requests_total"] == 0
    assert mod._gateway_by_status["401"] == 0

    req = _make_request("/memory/save", method="POST")  # no Authorization header
    with pytest.raises(HTTPUnauthorized):
        await mod.auth_middleware(req, _noop_handler)

    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["401"] == 1
    assert mod._gateway_by_status["4xx"] == 1
    assert _ring_window(mod) == 0


@pytest.mark.asyncio
async def test_read_role_403_counted():
    """E: a read-role token hitting a write-only route."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    assert _counters(mod)["requests_total"] == 0

    req = _make_request("/memory/save", auth_header="Bearer tok_m")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)

    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


@pytest.mark.asyncio
async def test_admin_route_requires_admin_role_403_counted():
    """F: a full-role token hitting an admin-only route."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("claude:tok_abc")  # default role = full
    req = _make_request("/admin/backup", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


@pytest.mark.asyncio
async def test_admin_token_confined_403_counted():
    """G: an admin-role token hitting a non-admin route."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("backup:tok_b", agent_roles="backup:admin")
    req = _make_request("/memory/save", auth_header="Bearer tok_b")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


@pytest.mark.asyncio
async def test_backup_quiesce_503_counted():
    """H: a write route while a backup quiesce is active — this is the OTHER
    source of by_status.503 alongside the shed valve (see MEANING_CHANGES:
    shed_503_total is now only <= by_status.503)."""
    from aiohttp.web_exceptions import HTTPServiceUnavailable
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    assert _counters(mod)["requests_total"] == 0

    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)

    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["503"] == 1
    assert mod._gateway_shed_503_total == 0, "quiesce is NOT the shed valve"
    assert _ring_window(mod) == 0


@pytest.mark.asyncio
async def test_no_principal_403_counted():
    """I: GATEWAY_REQUIRE_PRINCIPAL on, a write route, no kernel-attested
    principal (TCP transport — req.transport is None in every double here)."""
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("claude:tok_abc", gateway_require_principal="1")
    assert mod.GATEWAY_REQUIRE_PRINCIPAL is True
    req = _make_request("/memory/save", auth_header="Bearer tok_abc")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


@pytest.mark.asyncio
async def test_authenticated_success_counted_once_latency_ring_plus_one():
    """The EXISTING block (the tenth, original path): unchanged behaviour —
    counted once, AND this is the only case that feeds the latency ring
    (R-C's old boundary, `started` is taken here)."""
    mod = load_coordinator("claude:tok_abc")
    assert _counters(mod)["requests_total"] == 0
    assert _ring_window(mod) == 0

    req = _make_request("/memory/search", auth_header="Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)

    assert resp.status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert _ring_window(mod) == 1


# ══════════════════════════════════════════════════════════════════════════
# The invariant itself: EXACTLY one _record_gateway_request per request
# ══════════════════════════════════════════════════════════════════════════

async def _drive(mod, req, expect_raises=None):
    if expect_raises is not None:
        with pytest.raises(expect_raises):
            await mod.auth_middleware(req, _noop_handler)
    else:
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_exactly_one_record_gateway_request_call_per_request():
    """MUTATION TARGET: wrap `_record_gateway_request` and pin the call
    count at exactly 1 for one representative request on each of the nine
    early-exit sites plus the original authenticated path — ten scenarios,
    ten single calls, never zero and never two."""
    from aiohttp.web_exceptions import (
        HTTPServiceUnavailable, HTTPUnauthorized, HTTPForbidden,
    )

    scenarios = []

    # A: shed
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1
    scenarios.append((mod, _make_request("/memory/save", auth_header="Bearer tok_abc"),
                       HTTPServiceUnavailable))

    # B: auth-off
    mod = load_coordinator("")
    scenarios.append((mod, _make_request("/memory/search"), None))

    # C: unprotected /health, stale bearer
    mod = load_coordinator("claude:tok_abc")
    scenarios.append((mod, _make_request("/health", auth_header="Bearer tok_bad",
                                          method="GET", unprotected=True), None))

    # D: gateway's own 401
    mod = load_coordinator("claude:tok_abc")
    scenarios.append((mod, _make_request("/memory/save"), HTTPUnauthorized))

    # E: read-role 403
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    scenarios.append((mod, _make_request("/memory/save", auth_header="Bearer tok_m"),
                       HTTPForbidden))

    # F: admin-required 403
    mod = load_coordinator("claude:tok_abc")
    scenarios.append((mod, _make_request("/admin/backup", auth_header="Bearer tok_abc"),
                       HTTPForbidden))

    # G: admin-confined 403
    mod = load_coordinator("backup:tok_b", agent_roles="backup:admin")
    scenarios.append((mod, _make_request("/memory/save", auth_header="Bearer tok_b"),
                       HTTPForbidden))

    # H: quiesce 503
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    scenarios.append((mod, _make_request("/memory/save", auth_header="Bearer tok_abc"),
                       HTTPServiceUnavailable))

    # I: no-principal 403
    mod = load_coordinator("claude:tok_abc", gateway_require_principal="1")
    scenarios.append((mod, _make_request("/memory/save", auth_header="Bearer tok_abc"),
                       HTTPForbidden))

    # The original authenticated, handler-reached path
    mod = load_coordinator("claude:tok_abc")
    scenarios.append((mod, _make_request("/memory/search", auth_header="Bearer tok_abc"),
                       None))

    assert len(scenarios) == 10
    for mod, req, expect_raises in scenarios:
        original = mod._record_gateway_request
        wrapper = MagicMock(side_effect=original)
        mod._record_gateway_request = wrapper
        await _drive(mod, req, expect_raises)
        assert wrapper.call_count == 1, (
            f"expected exactly one _record_gateway_request call for "
            f"{req.path!r}, got {wrapper.call_count}")


# ══════════════════════════════════════════════════════════════════════════
# _record_gateway_request itself: latency is optional, never raises
# ══════════════════════════════════════════════════════════════════════════

def test_record_gateway_request_accepts_none_latency(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    before_total = mod.telemetry_gateway_counters()["requests_total"]
    mod._record_gateway_request(503, None)
    assert mod.telemetry_gateway_counters()["requests_total"] == before_total + 1
    assert mod._gateway_latency.snapshot()["window"] == 0


def test_record_gateway_request_still_feeds_the_ring_with_a_real_float():
    mod = load_coordinator("claude:tok_abc")
    mod._record_gateway_request(200, 12.5)
    snap = mod._gateway_latency.snapshot()
    assert snap["window"] == 1
    assert snap["last_ms"] == 12.5


def test_record_gateway_request_never_raises_on_bad_input():
    mod = load_coordinator("claude:tok_abc")
    mod._record_gateway_request("not-a-status", "not-a-latency")  # noqa: type
    # Never raises (module docstring's own invariant) — reaching this line
    # is the assertion.
