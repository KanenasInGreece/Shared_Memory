"""Security fix A1 (v0.9.76) — the auth exemption is a property of the
RESOLVED ROUTE, and a near-miss spelling of a gateway-owned path never
reaches the LLM proxy.

THE DEFECT THIS FILE EXISTS FOR
------------------------------
`coordinator.auth_middleware` exempted `request.path.rstrip("/")` from
authentication, while `hive_mind_proxy.AsyncHiveMindProxy._route_guard`
compared the path with `==`. The two normalisations disagreed, and the gap
between them fell through the catch-all `("*", "/{tail:.*}")` into the LLM
dispatch. Measured on the running gateway before the fix::

    $ curl -sS -D- -o/dev/null http://localhost:8888/health/
    HTTP/1.1 404 Not Found
    Server: llama.cpp
    X-SM-LLM-Backend: http://localhost:5000
    X-SM-Fault-Origin: upstream

— an anonymous, unaudited, uncounted request to a reasoning-LLM backend, on
a gateway whose whole security posture is "a bearer token on every route".

WHY THE OLD TEST COULD NOT SEE IT
---------------------------------
`tests/test_auth.py` asserted the exemption with a `_noop_handler`, so it
answered "does the middleware let `/health/` through" — which is the wrong
question. The middleware letting it through IS the defect; what matters is
where the ROUTER then sends it. A test that supplies its own handler is
structurally incapable of observing that. This file therefore drives the
REAL router: the gateway's actual route table, the real `auth_middleware`,
the real `_route_guard`, and a catch-all that makes "reached LLM dispatch"
observable as a distinct status + the `X-SM-LLM-Backend` header the live
gateway stamps.

TWO LAYERS, AND WHY BOTH MUST BE HERE (adversarial review A-02)
---------------------------------------------------------------
Layer 1 — the auth exemption matches the resolved route's canonical, plus
          the byte-identical path. Covers installs that have turned auth ON.
Layer 2 — `_route_guard` refuses a trailing-slash near-miss of an owned
          static route. This is the ONLY half that covers the SHIPPED
          DEFAULT: with `AGENT_TOKENS` unset, `auth_middleware` returns
          before its exemption is ever consulted, so Layer 1 is inert on a
          stranger's fresh install and Layer 2 is all that stands between an
          anonymous `GET /health/` and the LLM backend.

⚠ A WEAK TEST CANNOT TELL THIS FIX FROM A ONE-CHARACTER ONE. Measured:
deleting only the `rstrip` limb yields `/health` → 200 and `/health/` → 401
IDENTICALLY to the full fix, and so does a naive `match_info` read that
500s on `/health%0a`. Status-pair assertions certify three materially
different implementations as equivalent. The assertions below are therefore
on VALUES (`fact:1309`): which handler served it, which header is absent,
which `Allow` was offered, and 401-not-500.
"""
import asyncio
import ast
import importlib
import os
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock
from yarl import URL

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "shared-memory", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


# ── Harness ───────────────────────────────────────────────────────────────────
#
# Sentinel bodies, not the real payloads: what is under test is ROUTING +
# AUTH + the guard, and the identity of the handler that ran is the whole
# question. `handle_health`'s payload is pinned by tests/test_telemetry_contract.py.

_LLM_DISPATCH_STATUS = 299          # "the request reached the LLM proxy dispatch"
_LLM_BACKEND_HEADER = "X-SM-LLM-Backend"


async def _sentinel_health(request):
    return web.json_response({"served_by": "handle_health"})


async def _sentinel_pool_status(request):
    return web.json_response({"served_by": "handle_pool_status"})


def _load_gateway(agent_tokens: str):
    """Reload coordinator + hive_mind_proxy with AGENT_TOKENS pre-set, so
    AUTH_CONFIGURED_AT_STARTUP is captured from the env this test means.

    Same isolation idiom as tests/test_route_guard.py::_fresh_gateway. The
    modules are reloaded under their canonical names (not a private spec)
    because hive_mind_proxy binds `auth_middleware` from `coordinator` at
    import time — the app's middleware and the module the test asserts
    against must be the same object.
    """
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return coordinator, g


def _build_gateway_app(c, g):
    """The gateway's REAL route table, in main()'s order, with the real auth
    middleware and the real route guard.

    Deliberately mirrors hive_mind_proxy.main():
      attach_coordinator → /health → /pool/status → set_known_routes →
      require_unprotected_paths_are_plain_routes → catch-all.
    """
    app = web.Application(middlewares=[c.auth_middleware])
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _sentinel_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)

    proxy = g.AsyncHiveMindProxy()
    proxy.set_known_routes(app.router)
    c.require_unprotected_paths_are_plain_routes(app.router)

    async def _catchall(request):
        # handle_proxy's FIRST statement is this guard call — pinned by
        # test_route_guard_is_the_first_statement_of_handle_proxy below, so
        # stopping here instead of running the real upstream dispatch does
        # not weaken what these assertions mean.
        guard = proxy._route_guard(request)
        if guard is not None:
            return guard
        return web.json_response(
            {"served_by": "LLM_PROXY_DISPATCH"},
            status=_LLM_DISPATCH_STATUS,
            headers={_LLM_BACKEND_HEADER: "http://llm.invalid:5000"},
        )

    app.router.add_route("*", "/{tail:.*}", _catchall)
    return app, proxy


async def _probe(app, method, spelling, *, token=None):
    """Drive the app over a real socket. `encoded=True` stops yarl
    re-encoding, so `/health%2f`, `/health//` and `/health%0a` arrive on the
    wire exactly as written — measured, so no raw socket is needed."""
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await client.request(method, URL(spelling, encoded=True), headers=headers)
        body = await resp.read()
        return resp.status, dict(resp.headers), body
    finally:
        await client.close()


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
# Assertion 1 — /health is still SERVED by the gateway's own handler
# ══════════════════════════════════════════════════════════════════════════

def test_exact_health_is_served_anonymously_by_the_health_handler():
    """Not merely "200": which handler ran. A broken router that proxied
    /health to an upstream returning 200 would satisfy a status assertion
    and fail this one."""
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", "/health"))
    assert status == 200
    assert b"handle_health" in body, (
        "GET /health must be served by the gateway's own health route, "
        f"anonymously — got {body!r}"
    )
    assert _LLM_BACKEND_HEADER not in headers


def test_exact_pool_status_is_served_anonymously_by_its_own_handler():
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", "/pool/status"))
    assert status == 200
    assert b"handle_pool_status" in body
    assert _LLM_BACKEND_HEADER not in headers


# ══════════════════════════════════════════════════════════════════════════
# Assertion 2 + 7 — every near-miss spelling is refused, and NONE is proxied
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/health/", "/health//", "/health%2f", "/pool/status/"])
def test_near_miss_spelling_is_never_proxied_anonymously(spelling):
    """THE DEFECT. Each of these was served anonymously and forwarded to the
    LLM backend before the fix. The status must not be 200 and must not be
    the LLM-dispatch sentinel, and the response must not carry the
    X-SM-LLM-Backend header the gateway stamps on a proxied reply — status
    alone is satisfiable by an upstream that happened to answer 401."""
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", spelling))
    assert status == 401, f"{spelling} must require a token, got {status} {body!r}"
    assert status != _LLM_DISPATCH_STATUS
    assert _LLM_BACKEND_HEADER not in headers, (
        f"{spelling} reached the LLM proxy dispatch — the response carries "
        f"the gateway's backend header"
    )
    assert b"LLM_PROXY_DISPATCH" not in body


# ══════════════════════════════════════════════════════════════════════════
# Assertion 3 — LAYER 2. The only assertion that dies if the guard extension
# is dropped: an AUTHENTICATED near-miss must still never reach the proxy.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/health/", "/health//", "/health%2f", "/pool/status/"])
def test_authenticated_near_miss_is_refused_by_the_gateway_not_proxied(spelling):
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", spelling, token="tok_abc"))
    assert status == 404, (
        f"a valid token must not buy a near-miss spelling of an owned route "
        f"a trip to the LLM pool: {spelling} → {status} {body!r}"
    )
    assert headers.get("X-SM-Fault-Origin") == "gateway"
    assert _LLM_BACKEND_HEADER not in headers
    assert b"NOT forwarded to any LLM backend" in body


# ══════════════════════════════════════════════════════════════════════════
# Assertion 4 — LAYER 2 ON THE SHIPPED DEFAULT (auth off). The only
# assertion that covers a stranger's fresh install (adversarial review A-02).
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/health/", "/health//", "/health%2f", "/pool/status/"])
def test_near_miss_is_refused_on_an_auth_off_install(spelling):
    """`.env.example` ships AGENT_TOKENS unset — "the backward-compatible
    default". On that install auth_middleware returns before its exemption
    ever runs, so Layer 1 is a NO-OP and this is the only thing closing the
    hole. If this test passes while the guard extension is gone, the fix
    would be recorded as closed while every fresh install still had it."""
    c, g = _load_gateway("")
    assert c.AUTH_CONFIGURED_AT_STARTUP is False, "sanity: this is the auth-off shape"
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", spelling))
    assert status == 404, (
        f"auth-off install: {spelling} → {status} {body!r} — an anonymous "
        f"near-miss reached the LLM proxy on the SHIPPED DEFAULT"
    )
    assert headers.get("X-SM-Fault-Origin") == "gateway"
    assert _LLM_BACKEND_HEADER not in headers


def test_auth_off_install_still_serves_exact_health_and_still_proxies_llm_paths():
    """The auth-off counterweight: Layer 2 must refuse the near-miss WITHOUT
    breaking the passthrough contract. `/v1/chat/completions` on an auth-off
    install is a supported anonymous LLM call, and it must still dispatch —
    a guard that 404s it would be a far worse regression than the hole."""
    c, g = _load_gateway("")
    # A fresh Application per probe: aiohttp binds an Application to the
    # event loop of its first runner, and each _run() is its own loop.
    app, _ = _build_gateway_app(c, g)
    status, _, body = _run(_probe(app, "GET", "/health"))
    assert status == 200 and b"handle_health" in body

    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "POST", "/v1/chat/completions"))
    assert status == _LLM_DISPATCH_STATUS, (
        "the LLM passthrough must be untouched by the near-miss guard"
    )
    assert headers.get(_LLM_BACKEND_HEADER)


# ══════════════════════════════════════════════════════════════════════════
# Assertion 5 — the exact-path limb keeps fact:1535's anonymous 405
# ══════════════════════════════════════════════════════════════════════════

def test_options_on_health_is_405_with_allow_header_not_401():
    """`OPTIONS /health` resolves to the CATCH-ALL (canonical "/{tail}"), so
    a canonical-only exemption would turn it into 401 and repeal the precise
    405 fact:1535 requires — for the one caller class /health exists to
    serve, the unauthenticated one. "Get a token" is not why that request
    failed. Measured live before the fix: 405 + `Allow: GET, HEAD`."""
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "OPTIONS", "/health"))
    assert status == 405, f"got {status} {body!r}"
    assert headers.get("Allow") == "GET, HEAD"
    assert headers.get("X-SM-Fault-Origin") == "gateway"
    assert _LLM_BACKEND_HEADER not in headers


def test_post_on_health_is_405_not_401():
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "POST", "/health"))
    assert status == 405, f"got {status} {body!r}"
    assert headers.get("Allow") == "GET, HEAD"
    assert _LLM_BACKEND_HEADER not in headers


# ══════════════════════════════════════════════════════════════════════════
# Assertion 6 — a request that resolves to NO resource must stay 401, not 500
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/health%0a", "/%0a", "/a%0ab"])
def test_unresolvable_path_stays_401_and_never_500s(spelling):
    """The catch-all `"/{tail:.*}"` compiles to `/(?P<tail>.*)` and `.`
    excludes `\\n` without re.DOTALL, so a decoded newline matches NO
    resource: aiohttp hands the middleware a MatchInfoError whose route is a
    SystemRoute with `resource is None`. An unguarded
    `request.match_info.route.resource.canonical` raises AttributeError →
    500, and it raises BEFORE the audit block, so the fix would ship a
    one-byte anonymous unlogged-500 primitive INSIDE a security fix.
    Measured on the running gateway: 401 today, and it must stay 401."""
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, _, body = _run(_probe(app, "GET", spelling))
    assert status == 401, (
        f"{spelling} → {status} {body!r}: an unresolvable path must be a "
        f"clean 401, never a 500 raised out of the exemption read"
    )
    assert status != 500


# ══════════════════════════════════════════════════════════════════════════
# Assertion 7 — the startup assertion for the NEW invariant (A-08)
# ══════════════════════════════════════════════════════════════════════════

def test_startup_assertion_accepts_the_real_route_table():
    c, g = _load_gateway("claude:tok_abc")
    app = web.Application()
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _sentinel_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)
    c.require_unprotected_paths_are_plain_routes(app.router)  # must not raise


def test_startup_assertion_refuses_a_dynamic_canonical_in_the_exempt_set():
    """`canonical` is many-to-one for a dynamic resource: /memory/status/1
    and /memory/status/2 both report /memory/status/{pg_id}. An exempt entry
    naming a dynamic canonical would make the WHOLE pattern family
    anonymous, and the string looks innocent in a diff. Fail at startup."""
    c, g = _load_gateway("claude:tok_abc")
    app = web.Application()
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _sentinel_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)
    c._UNPROTECTED_PATHS.add("/memory/status/{pg_id}")
    try:
        with pytest.raises(RuntimeError) as exc:
            c.require_unprotected_paths_are_plain_routes(app.router)
        assert "/memory/status/{pg_id}" in str(exc.value)
        assert "many-to-one" in str(exc.value)
    finally:
        c._UNPROTECTED_PATHS.discard("/memory/status/{pg_id}")


def test_startup_assertion_refuses_an_exempt_path_no_route_owns():
    """An exemption for a path the router does not own can only ever be
    honoured by the catch-all — i.e. by forwarding to an LLM backend. That
    is the A1 defect stated as a route-table property."""
    c, g = _load_gateway("claude:tok_abc")
    app = web.Application()
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _sentinel_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)
    c._UNPROTECTED_PATHS.add("/health/")
    try:
        with pytest.raises(RuntimeError) as exc:
            c.require_unprotected_paths_are_plain_routes(app.router)
        assert "/health/" in str(exc.value)
    finally:
        c._UNPROTECTED_PATHS.discard("/health/")


def test_main_calls_the_startup_assertion_next_to_set_known_routes():
    """The assertion is only worth anything if the gateway entrypoint runs
    it. Source-level, because main() cannot be executed in a unit test."""
    src = open(os.path.join(SCRIPTS_DIR, "hive_mind_proxy.py"), encoding="utf-8").read()
    assert "require_unprotected_paths_are_plain_routes(app.router)" in src, (
        "hive_mind_proxy.main() must run the _UNPROTECTED_PATHS startup "
        "assertion — an invariant nothing checks is an intention"
    )
    snapshot_at = src.index("proxy.set_known_routes(app.router)")
    assert_at = src.index("require_unprotected_paths_are_plain_routes(app.router)")
    catchall_at = src.index('add_route("*", "/{tail:.*}"')
    assert snapshot_at < assert_at < catchall_at, (
        "the assertion must run after every real route is registered and "
        "before the catch-all — the same window as the route snapshot"
    )


# ══════════════════════════════════════════════════════════════════════════
# Adversarial review A-03 — the canonical read must return a REAL str for a
# REAL request, or the guarded reader silently absorbs every mock regression
# ══════════════════════════════════════════════════════════════════════════

def test_resolved_canonical_returns_a_real_str_for_a_real_request():
    """`_resolved_route_canonical` DENIES on any non-str, which is correct
    and is what keeps a MagicMock from granting an exemption. But that same
    property means a chain broken for a real request would fail silently in
    the direction that looks safe. Assert positively that a real routed
    request yields the string this fix is built on."""
    c, g = _load_gateway("claude:tok_abc")
    seen = {}

    async def _capturing_health(request):
        seen["canonical"] = c._resolved_route_canonical(request)
        seen["type"] = type(seen["canonical"]).__name__
        return web.json_response({"served_by": "handle_health"})

    app = web.Application(middlewares=[c.auth_middleware])
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _capturing_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)
    proxy = g.AsyncHiveMindProxy()
    proxy.set_known_routes(app.router)
    app.router.add_route("*", "/{tail:.*}", proxy.handle_proxy)

    status, _, _ = _run(_probe(app, "GET", "/health"))
    assert status == 200
    assert isinstance(seen.get("canonical"), str), (
        f"the canonical read returned {seen.get('type')}, not str — the "
        f"guarded reader is denying on every request and the exemption is "
        f"now carried entirely by the exact-path limb"
    )
    assert seen["canonical"] == "/health"


def test_resolved_canonical_denies_on_a_test_double():
    """The other half of the same property, stated so it cannot regress
    quietly: a MagicMock's auto-attribute is not a str and must not grant."""
    from unittest.mock import MagicMock
    c, _ = _load_gateway("claude:tok_abc")
    assert c._resolved_route_canonical(MagicMock()) is None
    assert c._resolved_route_canonical(object()) is None


# ══════════════════════════════════════════════════════════════════════════
# The stub-fidelity pin: the catch-all above stops at _route_guard, which is
# only faithful while _route_guard really is handle_proxy's first statement.
# ══════════════════════════════════════════════════════════════════════════

def test_route_guard_is_the_first_statement_of_handle_proxy():
    src = open(os.path.join(SCRIPTS_DIR, "hive_mind_proxy.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AsyncHiveMindProxy":
            for sub in node.body:
                if isinstance(sub, ast.AsyncFunctionDef) and sub.name == "handle_proxy":
                    fn = sub
    assert fn is not None, "AsyncHiveMindProxy.handle_proxy not found"
    body = [s for s in fn.body if not (isinstance(s, ast.Expr)
                                       and isinstance(s.value, ast.Constant))]
    first = body[0]
    assert isinstance(first, ast.Assign), ast.dump(first)
    assert "_route_guard" in ast.unparse(first.value), (
        "handle_proxy must call _route_guard before anything else — this "
        "file's catch-all stub stops there and would otherwise be lying "
        f"about what a real dispatch does. Got: {ast.unparse(first)}"
    )
