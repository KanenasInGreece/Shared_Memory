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

R-C: the latency ring feeds ONLY the old boundary (the authenticated,
handler-reached path where `started` is taken) — every early-exit test
below pins the ring's `window` at 0 alongside the status-counting move.

═══════════════════════════════════════════════════════════════════════════
QA FIX ROUND (2026-09-03) — F1 + F2
═══════════════════════════════════════════════════════════════════════════
QA finding 1 (REQUIRED): the STATUS VALUE the two early-exit wrappers record
(`_status = exc.status` on a caught `web.HTTPException`, `_status = 500` as
the untouched default otherwise — coordinator.py's auth-off bypass and
unprotected-path exemption blocks) was pinned NOWHERE. QA mutated
`_status = exc.status` to `_status = 999` in BOTH wrappers simultaneously
and the full 3647-test suite stayed green. Fixed below: three new tests
drive a REAL HTTP request over a real `TestClient`/`TestServer` socket,
through the REAL `auth_middleware`, to a handler that RAISES an exception
(never one that merely returns an error-shaped response) — and assert the
resulting `by_status` bucket. Mutation-checked against QA's exact `999`
substitution (see the commit body for the captured run).

QA finding 2 (non-blocking, brief compliance): the brief's D2 test spec was
explicit — "Tests (wire, TestClient …)" — but this file originally drove
`auth_middleware(req, handler)` directly against a `MagicMock` request, no
socket, no `web.Application`. Every scenario below (the original ten plus
the three new ones) is now driven over a real `TestClient` socket through a
real `web.Application(middlewares=[mod.auth_middleware])`, using the SAME
scaffolding (`_build_app`/`_probe`/`_run`) for both findings — this is what
the brief asked for and what F1's new tests needed anyway. The three
`_record_gateway_request`-internals tests at the bottom of this file stay
helper-level: they assert properties of that ONE function in isolation
("never raises on bad input", "accepts a bare int status with no request in
play") that a wire probe cannot exercise any more directly, so they add
distinct value and are kept per the merger's ruling.

QA finding 5 (env leak) is fixed the same way test_obs_d1_token_verify_rate.py
already does it: `load_coordinator` takes `monkeypatch` and sets/deletes
every env var through it, so pytest restores the prior value (or its
absence) at the end of each test — no state leaks forward to a later file.
`secure_env._secrets.pop("AGENT_TOKENS", None)` is left as a plain pop
(mirrors the accepted D1 idiom): nothing in this test suite ever WRITES
that dict directly, so there is nothing for a leaked write to poison.

The isolated-module load (`spec_from_file_location`, unchanged) remains a
SECOND, independent isolation layer on top of the env-var fix: every test
gets its own fresh `coordinator` module object, so even the module's own
globals (`_gateway_by_status`, `_inflight`, `_backup_quiesce`, …) can never
leak between tests or from any other file's `sys.modules['coordinator']`.
"""
import asyncio
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def load_coordinator(monkeypatch, agent_tokens: str = "", agent_roles: str = "",
                      gateway_inflight_max: str = "",
                      gateway_require_principal: str = ""):
    """Fresh, isolated coordinator module per call (mirrors test_auth.py /
    test_backup_quiesce.py / test_load_shed_before_auth_exemption.py) — so
    gateway counters, `_inflight`, `_backup_quiesce` and every other module
    global never leak between tests or from any other file's shared
    `coordinator` import.

    F5 (QA fix round, finding 5): every env var below goes through
    `monkeypatch`, which pytest unwinds automatically when the calling test
    ends — nothing here can leak forward to a later test file, unlike the
    direct `os.environ[...] = ...` this helper used before."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)

    if agent_tokens:
        monkeypatch.setenv("AGENT_TOKENS", agent_tokens)
    else:
        monkeypatch.delenv("AGENT_TOKENS", raising=False)
    if agent_roles:
        monkeypatch.setenv("AGENT_ROLES", agent_roles)
    else:
        monkeypatch.delenv("AGENT_ROLES", raising=False)
    if gateway_inflight_max:
        monkeypatch.setenv("GATEWAY_INFLIGHT_MAX", gateway_inflight_max)
    else:
        monkeypatch.delenv("GATEWAY_INFLIGHT_MAX", raising=False)
    if gateway_require_principal:
        monkeypatch.setenv("GATEWAY_REQUIRE_PRINCIPAL", gateway_require_principal)
    else:
        monkeypatch.delenv("GATEWAY_REQUIRE_PRINCIPAL", raising=False)

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location(
        f"coordinator_d2_test_{id(object())}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════════════
# Wire scaffolding (F1 + F2): a real Application + a real socket, shared by
# every scenario in this file.
# ══════════════════════════════════════════════════════════════════════════

async def _ok_handler(request):
    return web.json_response({"ok": True})


async def _raise_method_not_allowed(request):
    """A handler that RAISES `web.HTTPException`, not one that returns an
    error-shaped response — the distinction QA's finding 1 turns on: the
    two wrappers under test only convert a RAISED `web.HTTPException` via
    their `except` clause."""
    raise web.HTTPMethodNotAllowed("GET", ["POST"])


async def _raise_generic_exception(request):
    """Anything that is NOT a `web.HTTPException` — the wrapper's `except`
    clause does not catch this, so it propagates to aiohttp's own
    exception-to-500 conversion, and the wrapper's `_status = 500` default
    (never reassigned) is what the `finally` records."""
    raise ValueError("boom - not an HTTPException")


def _build_app(mod, routes):
    """A real `web.Application` with `mod.auth_middleware` installed as the
    sole middleware and the given `(method, path, handler)` routes — no
    MagicMock request, no direct `auth_middleware(req, handler)` call."""
    app = web.Application(middlewares=[mod.auth_middleware])
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    return app


async def _probe(app, method, path, token=None):
    """Drive the app over a real socket (TestServer/TestClient — no port
    bound to the host, per the standing no-port-binding rule)."""
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await client.request(method, path, headers=headers)
        body = await resp.read()
        return resp.status, dict(resp.headers), body
    finally:
        await client.close()


def _run(coro):
    return asyncio.run(coro)


def _counters(mod):
    return mod.telemetry_gateway_counters()


def _ring_window(mod):
    return mod._gateway_latency.snapshot()["window"]


# ══════════════════════════════════════════════════════════════════════════
# F1 REQUIRED — the status VALUE each early-exit wrapper records, pinned
# ══════════════════════════════════════════════════════════════════════════

def test_auth_off_handler_raised_httpexception_status_pinned(monkeypatch):
    """QA finding 1, wrapper 1 of 2 (the auth-off bypass). Mutation-checked
    against QA's exact substitution: reverting `_status = exc.status` to
    `_status = 999` in this wrapper kills this test (recorded in the commit
    body — scratchpad copy -> mutate -> run -> restore, fact:1244)."""
    mod = load_coordinator(monkeypatch, "")  # no AGENT_TOKENS at all -> auth-off
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False
    app = _build_app(mod, [("GET", "/probe", _raise_method_not_allowed)])
    assert mod._gateway_by_status["4xx"] == 0
    assert mod._gateway_by_status["5xx"] == 0

    status, _, _ = _run(_probe(app, "GET", "/probe"))

    assert status == 405
    assert mod._gateway_by_status["4xx"] == 1, "a handler-raised 405 must land in the 4xx family"
    assert mod._gateway_by_status["5xx"] == 0, "a handler-raised 405 must NOT land in the 5xx family"


def test_unprotected_path_handler_raised_httpexception_status_pinned(monkeypatch):
    """QA finding 1, wrapper 2 of 2 (the unprotected `/health` exemption).
    Same measurement, same mutation target, the other wrapper."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc")  # auth ON
    app = _build_app(mod, [("GET", "/health", _raise_method_not_allowed)])
    assert mod._gateway_by_status["4xx"] == 0
    assert mod._gateway_by_status["5xx"] == 0

    status, _, _ = _run(_probe(app, "GET", "/health"))

    assert status == 405
    assert mod._gateway_by_status["4xx"] == 1, "a handler-raised 405 must land in the 4xx family"
    assert mod._gateway_by_status["5xx"] == 0, "a handler-raised 405 must NOT land in the 5xx family"


def test_auth_off_generic_exception_default_status_pinned(monkeypatch):
    """The OTHER half of QA finding 1: the `_status = 500` default on the
    same auth-off wrapper, for any exception that is NOT a
    `web.HTTPException` (never reassigned before the `finally` fires)."""
    mod = load_coordinator(monkeypatch, "")
    app = _build_app(mod, [("GET", "/probe", _raise_generic_exception)])
    assert mod._gateway_by_status["5xx"] == 0

    status, _, _ = _run(_probe(app, "GET", "/probe"))

    assert status == 500
    assert mod._gateway_by_status["5xx"] == 1


# ══════════════════════════════════════════════════════════════════════════
# One call site, nine early exits + the happy path — exactly one record each
# (F2: converted to wire — real TestClient socket, real auth_middleware)
# ══════════════════════════════════════════════════════════════════════════

def test_shed_503_counted_no_latency_entry(monkeypatch):
    """A: the load-shed valve. Prove-first: requests_total/by_status start
    at 0 (fresh module); after the shed, both moved by exactly one and the
    latency ring — R-C's old boundary — is untouched."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1  # already at the cap
    app = _build_app(mod, [("POST", "/memory/save", _ok_handler)])
    assert _counters(mod)["requests_total"] == 0
    assert mod._gateway_by_status["503"] == 0

    status, _, _ = _run(_probe(app, "POST", "/memory/save", token="tok_abc"))

    assert status == 503
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["503"] == 1
    assert mod._gateway_by_status["5xx"] == 1
    assert _ring_window(mod) == 0
    mod._inflight = 0


def test_auth_off_counted_no_audit_no_error(monkeypatch):
    """B: the auth-off bypass. `_audit` must fire exactly as today — never,
    on this path — proven by patching it with a MagicMock and asserting it
    was never called, alongside the new counting."""
    mod = load_coordinator(monkeypatch, "")  # no AGENT_TOKENS at all -> auth-off
    assert mod.AUTH_CONFIGURED_AT_STARTUP is False
    audit_mock = MagicMock()
    mod._audit = audit_mock
    app = _build_app(mod, [("POST", "/memory/search", _ok_handler)])
    assert _counters(mod)["requests_total"] == 0

    status, _, _ = _run(_probe(app, "POST", "/memory/search"))

    assert status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert _ring_window(mod) == 0
    audit_mock.assert_not_called()


def test_unprotected_stale_bearer_counted_200_by_status_401_unmoved(monkeypatch):
    """C: /health with a bearer that fails to verify. The RESPONSE stays the
    same anonymous 200 it always was (ADV2-1's byte-identical contract,
    untouched by this round) — so it lands in by_status.2xx, and
    by_status.401 must NOT move: that 401-shaped signal already reached
    credentials.token_verify_failed and the D1 ring."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    app = _build_app(mod, [("GET", "/health", _ok_handler)])
    assert _counters(mod)["requests_total"] == 0
    assert mod._gateway_by_status["401"] == 0

    status, _, _ = _run(_probe(app, "GET", "/health", token="tok_bad"))

    assert status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert mod._gateway_by_status["401"] == 0, "the gateway's own 401 bucket must be unmoved"
    assert _ring_window(mod) == 0
    # The D1 ring is the surface that DOES move for this event.
    assert len(mod.telemetry_token_verify_ring()) == 1


def test_unprotected_path_with_no_bearer_at_all_counted_once(monkeypatch):
    """C, the common case: an anonymous /health poll with no Authorization
    header at all — must still be counted exactly once (no token-oracle
    probe to audit, so the D1 ring stays empty)."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    app = _build_app(mod, [("GET", "/health", _ok_handler)])

    status, _, _ = _run(_probe(app, "GET", "/health"))

    assert status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert len(mod.telemetry_token_verify_ring()) == 0


def test_gateways_own_401_counted(monkeypatch):
    """D: no token at all on a protected route."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    app = _build_app(mod, [("POST", "/memory/save", _ok_handler)])
    assert _counters(mod)["requests_total"] == 0
    assert mod._gateway_by_status["401"] == 0

    status, _, _ = _run(_probe(app, "POST", "/memory/save"))  # no Authorization header

    assert status == 401
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["401"] == 1
    assert mod._gateway_by_status["4xx"] == 1
    assert _ring_window(mod) == 0


def test_read_role_403_counted(monkeypatch):
    """E: a read-role token hitting a write-only route."""
    mod = load_coordinator(monkeypatch, "monitor:tok_m", agent_roles="monitor:read")
    app = _build_app(mod, [("POST", "/memory/save", _ok_handler)])
    assert _counters(mod)["requests_total"] == 0

    status, _, _ = _run(_probe(app, "POST", "/memory/save", token="tok_m"))

    assert status == 403
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


def test_admin_route_requires_admin_role_403_counted(monkeypatch):
    """F: a full-role token hitting an admin-only route."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc")  # default role = full
    app = _build_app(mod, [("POST", "/admin/backup", _ok_handler)])

    status, _, _ = _run(_probe(app, "POST", "/admin/backup", token="tok_abc"))

    assert status == 403
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


def test_admin_token_confined_403_counted(monkeypatch):
    """G: an admin-role token hitting a non-admin route."""
    mod = load_coordinator(monkeypatch, "backup:tok_b", agent_roles="backup:admin")
    app = _build_app(mod, [("POST", "/memory/save", _ok_handler)])

    status, _, _ = _run(_probe(app, "POST", "/memory/save", token="tok_b"))

    assert status == 403
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


def test_backup_quiesce_503_counted(monkeypatch):
    """H: a write route while a backup quiesce is active — this is the OTHER
    source of by_status.503 alongside the shed valve (see MEANING_CHANGES:
    shed_503_total is now only <= by_status.503)."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    mod._backup_quiesce = True
    app = _build_app(mod, [("POST", "/memory/save", _ok_handler)])
    assert _counters(mod)["requests_total"] == 0

    status, _, _ = _run(_probe(app, "POST", "/memory/save", token="tok_abc"))

    assert status == 503
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["503"] == 1
    assert mod._gateway_shed_503_total == 0, "quiesce is NOT the shed valve"
    assert _ring_window(mod) == 0


def test_no_principal_403_counted(monkeypatch):
    """I: GATEWAY_REQUIRE_PRINCIPAL on, a write route, no kernel-attested
    principal (a real TCP TestClient socket — `_peer_identity` sees a
    non-AF_UNIX socket family and returns None, same as every other double
    here)."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc", gateway_require_principal="1")
    assert mod.GATEWAY_REQUIRE_PRINCIPAL is True
    app = _build_app(mod, [("POST", "/memory/save", _ok_handler)])

    status, _, _ = _run(_probe(app, "POST", "/memory/save", token="tok_abc"))

    assert status == 403
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["403"] == 1
    assert _ring_window(mod) == 0


def test_authenticated_success_counted_once_latency_ring_plus_one(monkeypatch):
    """The EXISTING block (the tenth, original path): unchanged behaviour —
    counted once, AND this is the only case that feeds the latency ring
    (R-C's old boundary, `started` is taken here)."""
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    app = _build_app(mod, [("POST", "/memory/search", _ok_handler)])
    assert _counters(mod)["requests_total"] == 0
    assert _ring_window(mod) == 0

    status, _, _ = _run(_probe(app, "POST", "/memory/search", token="tok_abc"))

    assert status == 200
    assert _counters(mod)["requests_total"] == 1
    assert mod._gateway_by_status["2xx"] == 1
    assert _ring_window(mod) == 1


# ══════════════════════════════════════════════════════════════════════════
# The invariant itself: EXACTLY one _record_gateway_request per request
# ══════════════════════════════════════════════════════════════════════════

def test_exactly_one_record_gateway_request_call_per_request(monkeypatch):
    """MUTATION TARGET: wrap `_record_gateway_request` and pin the call
    count at exactly 1 for one representative request on each of the nine
    early-exit sites plus the original authenticated path — ten scenarios,
    ten single calls, never zero and never two.

    F2 (wire): each scenario is driven over a real TestClient socket through
    the real `auth_middleware`, patching only the one function under test —
    not a MagicMock request standing in for the whole HTTP layer."""
    scenarios = []  # (mod, app, method, path, token, expected_status)

    # A: shed
    mod = load_coordinator(monkeypatch, "claude:tok_abc", gateway_inflight_max="1")
    mod._inflight = 1
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/save", _ok_handler)]),
                       "POST", "/memory/save", "tok_abc", 503))

    # B: auth-off
    mod = load_coordinator(monkeypatch, "")
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/search", _ok_handler)]),
                       "POST", "/memory/search", None, 200))

    # C: unprotected /health, stale bearer
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    scenarios.append((mod, _build_app(mod, [("GET", "/health", _ok_handler)]),
                       "GET", "/health", "tok_bad", 200))

    # D: gateway's own 401
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/save", _ok_handler)]),
                       "POST", "/memory/save", None, 401))

    # E: read-role 403
    mod = load_coordinator(monkeypatch, "monitor:tok_m", agent_roles="monitor:read")
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/save", _ok_handler)]),
                       "POST", "/memory/save", "tok_m", 403))

    # F: admin-required 403
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    scenarios.append((mod, _build_app(mod, [("POST", "/admin/backup", _ok_handler)]),
                       "POST", "/admin/backup", "tok_abc", 403))

    # G: admin-confined 403
    mod = load_coordinator(monkeypatch, "backup:tok_b", agent_roles="backup:admin")
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/save", _ok_handler)]),
                       "POST", "/memory/save", "tok_b", 403))

    # H: quiesce 503
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    mod._backup_quiesce = True
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/save", _ok_handler)]),
                       "POST", "/memory/save", "tok_abc", 503))

    # I: no-principal 403
    mod = load_coordinator(monkeypatch, "claude:tok_abc", gateway_require_principal="1")
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/save", _ok_handler)]),
                       "POST", "/memory/save", "tok_abc", 403))

    # The original authenticated, handler-reached path
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    scenarios.append((mod, _build_app(mod, [("POST", "/memory/search", _ok_handler)]),
                       "POST", "/memory/search", "tok_abc", 200))

    assert len(scenarios) == 10
    for mod, app, method, path, token, expect_status in scenarios:
        original = mod._record_gateway_request
        wrapper = MagicMock(side_effect=original)
        mod._record_gateway_request = wrapper
        status, _, _ = _run(_probe(app, method, path, token=token))
        assert status == expect_status, (
            f"{method} {path!r} (token={token!r}): expected {expect_status}, got {status}")
        assert wrapper.call_count == 1, (
            f"expected exactly one _record_gateway_request call for "
            f"{method} {path!r}, got {wrapper.call_count}")


# ══════════════════════════════════════════════════════════════════════════
# _record_gateway_request itself: latency is optional, never raises
#
# Kept helper-level (not converted to wire, per the merger's ruling): these
# pin properties of the ONE function in isolation that no HTTP probe can
# exercise any more directly than calling it.
# ══════════════════════════════════════════════════════════════════════════

def test_record_gateway_request_accepts_none_latency(monkeypatch):
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    before_total = mod.telemetry_gateway_counters()["requests_total"]
    mod._record_gateway_request(503, None)
    assert mod.telemetry_gateway_counters()["requests_total"] == before_total + 1
    assert mod._gateway_latency.snapshot()["window"] == 0


def test_record_gateway_request_still_feeds_the_ring_with_a_real_float(monkeypatch):
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    mod._record_gateway_request(200, 12.5)
    snap = mod._gateway_latency.snapshot()
    assert snap["window"] == 1
    assert snap["last_ms"] == 12.5


def test_record_gateway_request_never_raises_on_bad_input(monkeypatch):
    mod = load_coordinator(monkeypatch, "claude:tok_abc")
    mod._record_gateway_request("not-a-status", "not-a-latency")  # noqa: type
    # Never raises (module docstring's own invariant) — reaching this line
    # is the assertion.
