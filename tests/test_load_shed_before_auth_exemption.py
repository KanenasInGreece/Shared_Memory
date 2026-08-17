"""S-11 (Credential_Custody_Plan PR A5): the outer in-flight load-shed valve
(GATEWAY_INFLIGHT_MAX) used to be checked only on the fully-authenticated
request path -- an anonymous /health or /pool/status hit (and, on an
auth-unset install, EVERY request) could never be shed no matter how
saturated the gateway already was, because both exemptions returned before
the check ever ran. It now runs FIRST, ahead of both exemptions.

Uses tests/test_auth.py's load_coordinator() helper pattern (isolated
per-test module via spec_from_file_location) since this only needs
coordinator.auth_middleware, not the full gateway."""
import asyncio
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
    """A request shed by the valve was never ADMITTED -- _inflight is
    untouched by the shed itself. SEC-A5-05a (PR A5 fix round) made every
    ADMITTED path (bypass/exemption/authenticated alike) increment it
    uniformly; a request that never gets past the cap check above still
    never reaches that increment at all."""
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    req = _make_request("/health")
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req, _noop_handler)
    assert mod._inflight == 1, "a shed request must not itself move the counter"
    mod._inflight = 0


# ── SEC-A5-05a (PR A5 fix round): uniform in-flight counting ─────────────────
# Security review finding: the pre-fix-round code claimed the reordered valve
# protected anonymous/auth-off traffic, but `_inflight` was only ever
# incremented deep inside the authenticated-only branch — an anonymous flood
# could never trip the cap regardless of its own volume, because it never
# moved the counter it was being checked against. These tests prove the fix:
# an anonymous request that is ADMITTED (not itself shed) now counts, so a
# second concurrent one genuinely gets shed by it.

@pytest.mark.asyncio
async def test_concurrent_anonymous_health_flood_trips_the_valve():
    """MUTATION TARGET (SEC-A5-05a): two concurrent anonymous /health
    requests against a cap of 1 -- the first is admitted and, while its
    handler is still running, increments _inflight to 1; the second must
    observe that and shed. No auth-off/authenticated traffic is involved at
    all, proving the valve now genuinely governs anonymous load by itself
    rather than by accident of concurrent authenticated traffic."""
    mod = load_coordinator("claude:tok_abc", gateway_inflight_max="1")

    first_admitted = asyncio.Event()
    release_first = asyncio.Event()

    async def _slow_handler(request):
        from aiohttp import web
        first_admitted.set()
        await release_first.wait()
        return web.json_response({"ok": True})

    req1 = _make_request("/health")
    req2 = _make_request("/health")

    task1 = asyncio.create_task(mod.auth_middleware(req1, _slow_handler))
    await first_admitted.wait()
    assert mod._inflight == 1, "the first admitted anonymous request must count"

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req2, _noop_handler)

    release_first.set()
    resp1 = await task1
    assert resp1.status == 200
    assert mod._inflight == 0


@pytest.mark.asyncio
async def test_concurrent_auth_off_flood_trips_the_valve():
    """Same proof, auth-off install -- the review's other failure mode ('on
    an auth-off install nothing ever contributes')."""
    mod = load_coordinator("", gateway_inflight_max="1")
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False

    first_admitted = asyncio.Event()
    release_first = asyncio.Event()

    async def _slow_handler(request):
        from aiohttp import web
        first_admitted.set()
        await release_first.wait()
        return web.json_response({"ok": True})

    req1 = _make_request("/memory/save", method="POST")
    req2 = _make_request("/memory/save", method="POST")

    task1 = asyncio.create_task(mod.auth_middleware(req1, _slow_handler))
    await first_admitted.wait()
    assert mod._inflight == 1

    from aiohttp.web_exceptions import HTTPServiceUnavailable
    with pytest.raises(HTTPServiceUnavailable):
        await mod.auth_middleware(req2, _noop_handler)

    release_first.set()
    await task1
    assert mod._inflight == 0


@pytest.mark.asyncio
async def test_inflight_decrements_even_when_handler_raises():
    """Exception safety (review: 'watch the exemption/bypass branches for
    leaks on exception') -- the auth-disabled BYPASS branch calls the
    handler directly with no inner try/finally of its own; confirm the
    OUTER one still decrements when that handler raises."""
    mod = load_coordinator("", gateway_inflight_max="5")  # auth off -> bypass branch

    async def _raising_handler(request):
        raise RuntimeError("boom")

    req = _make_request("/memory/save", method="POST")
    with pytest.raises(RuntimeError):
        await mod.auth_middleware(req, _raising_handler)
    assert mod._inflight == 0, "the bypass branch must not leak an in-flight slot on exception"


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: reverting the load-shed check to
# its old position (after both exemptions) makes
# test_saturated_gateway_sheds_anonymous_health_hit and
# test_saturated_gateway_sheds_even_when_auth_entirely_disabled both fail
# (no HTTPServiceUnavailable raised -- the request passes straight through).
# Reverting the uniform-counting fix (moving `_inflight += 1`/`-= 1` back
# inside only the authenticated branch) makes
# test_concurrent_anonymous_health_flood_trips_the_valve and
# test_concurrent_auth_off_flood_trips_the_valve both fail (the second
# concurrent request is never shed).
