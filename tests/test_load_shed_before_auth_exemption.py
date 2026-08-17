"""S-11 (Credential_Custody_Plan PR A5): the outer in-flight load-shed valve
(GATEWAY_INFLIGHT_MAX) used to be checked only on the fully-authenticated
request path -- an anonymous /health or /pool/status hit (and, on an
auth-unset install, EVERY request) could never be shed no matter how
saturated the gateway already was, because both exemptions returned before
the check ever ran. It now runs FIRST, ahead of both exemptions.

Uses tests/test_auth.py's load_coordinator() helper pattern (isolated
per-test module via spec_from_file_location) since this only needs
coordinator.auth_middleware, not the full gateway."""
import importlib.util
import os
import sys

import pytest


def load_coordinator(agent_tokens: str = "", gateway_inflight_max: str = ""):
    """Mirrors tests/test_auth.py's load_coordinator() exactly (kept local
    to avoid a cross-test-file import dependency) -- fresh, isolated module
    per call so GATEWAY_INFLIGHT_MAX/_inflight state never leaks between
    tests or from any other file's shared `coordinator` import."""
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
    if gateway_inflight_max:
        os.environ["GATEWAY_INFLIGHT_MAX"] = gateway_inflight_max
    else:
        os.environ.pop("GATEWAY_INFLIGHT_MAX", None)

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_shed_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_request(path: str, method: str = "GET"):
    from unittest.mock import MagicMock
    req = MagicMock()
    req.path = path
    req.method = method
    req.headers = {}
    req.get = MagicMock(return_value=None)
    req.__setitem__ = MagicMock()
    return req


async def _noop_handler(request):
    from aiohttp import web
    return web.json_response({"ok": True})


@pytest.mark.asyncio
async def test_saturated_gateway_sheds_anonymous_health_hit():
    """MUTATION TARGET: the core S-11 fix. /health is in _UNPROTECTED_PATHS
    -- before this change it always passed regardless of _inflight."""
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1  # already at the cap

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    req = _make_request("/health")
    with pytest.raises(HTTPServiceUnavailable) as exc_info:
        await mod.auth_middleware(req, _noop_handler)
    assert exc_info.value.headers.get("Retry-After") == "1"
    mod._inflight = 0


@pytest.mark.asyncio
async def test_saturated_gateway_sheds_pool_status_hit():
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    req = _make_request("/pool/status")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)
    mod._inflight = 0


@pytest.mark.asyncio
async def test_saturated_gateway_sheds_even_when_auth_entirely_disabled():
    """The valve applies unconditionally -- ahead of the AUTH_CONFIGURED_AT_
    STARTUP bypass too, not just the per-path exemption. An auth-unset
    install that sets GATEWAY_INFLIGHT_MAX still gets protected."""
    mod = load_coordinator("", gateway_inflight_max="1")
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False
    mod._inflight = 1

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    req = _make_request("/memory/save", method="POST")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)
    mod._inflight = 0


@pytest.mark.asyncio
async def test_below_cap_health_still_passes():
    """Sanity: the valve only sheds AT/OVER the cap -- /health keeps working
    normally under normal load."""
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="5")
    mod._inflight = 1
    req = _make_request("/health")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_disabled_valve_never_sheds_anything():
    """GATEWAY_INFLIGHT_MAX=0 (default) means the valve is off entirely --
    unaffected by this reordering."""
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="0")
    mod._inflight = 999999
    req = _make_request("/health")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200
    mod._inflight = 0


@pytest.mark.asyncio
async def test_authenticated_write_route_still_shed_as_before():
    """Regression: the pre-existing behaviour (shed on the authenticated
    write path) must still hold after the reordering."""
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    req = _make_request("/memory/save", method="POST")
    req.headers = {"Authorization": "Bearer tok_abc"}
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)
    mod._inflight = 0


@pytest.mark.asyncio
async def test_shed_request_does_not_itself_bump_inflight_counter():
    """A request shed by the valve was never admitted -- _inflight is
    untouched by the shed itself (only the full authenticated dispatch
    increments/decrements it), matching the documented design choice in
    coordinator.py's _inflight comment block."""
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    req = _make_request("/health")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)
    assert mod._inflight == 1, "a shed request must not itself move the counter"
    mod._inflight = 0


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: reverting the load-shed check to
# its old position (after both exemptions) makes
# test_saturated_gateway_sheds_anonymous_health_hit and
# test_saturated_gateway_sheds_even_when_auth_entirely_disabled both fail
# (no HTTPServiceUnavailable raised -- the request passes straight through).
