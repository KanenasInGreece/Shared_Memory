"""Security fix A1 (v0.9.76) — the auth exemption compares the string
aiohttp's ROUTER compares, and a near-miss spelling of a gateway-owned path
never reaches the LLM proxy.

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
Layer 1 — the auth exemption compares `request.rel_url.path_safe`, the exact
          string `Resource.resolve` passes to `PlainResource._match`, against
          `_UNPROTECTED_PATHS`. Covers installs that have turned auth ON.
Layer 2 — `_route_guard` refuses a trailing-slash near-miss of an owned
          static route. This is the ONLY half that covers the SHIPPED
          DEFAULT: with `AGENT_TOKENS` unset, `auth_middleware` returns
          before its exemption is ever consulted, so Layer 1 is inert on a
          stranger's fresh install and Layer 2 is all that stands between an
          anonymous `GET /health/` and the LLM backend.

WHAT THE FIX ROUND CHANGED, AND WHY THIS FILE GREW
--------------------------------------------------
The first round of Layer 1 compared `request.path` and a `match_info`-derived
canonical. Two adversarial reviewers measured, independently, that:

  * the canonical limb was DEAD — deleting it left every test green, and
    `PlainResource._match` comparing `path_safe` makes it a strict subset of
    the path comparison by construction; and
  * the surviving `request.path` comparison is percent-DECODED, so
    `GET /pool%2fstatus` was auth-EXEMPTED while the router sent it to the
    catch-all LLM proxy — A1's exact shape, alive inside A1's own fix, held
    back only by an unconditional 405 that reads like a bug and that a
    plausible "cleanup" removes with all 3110 tests staying green.

So Layer 1 is now ONE comparison, and it is the router's own. The tests added
for it are `test_encoded_slash_spelling_is_not_auth_exempt` (the exemption)
and `test_encoded_slash_on_auth_off_is_the_routers_404` (the
containment that the auth-off default depends on).

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
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
# Imported, never spelled as a string: this is the class aiohttp 3.14.3
# actually exports (`aiohttp.web_exceptions.NotAppKeyWarning`, re-exported
# from `aiohttp.web`) — verified by import, so a rename upstream fails loudly
# here instead of leaving a filter that quietly matches nothing.
from aiohttp.web import NotAppKeyWarning
from unittest.mock import AsyncMock, patch
from yarl import URL

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "shared-memory", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# D8b / R-I (HYG round) — the three NotAppKeyWarnings this module raises are
# EXPLAINED, not chased. aiohttp 3.14 asks for `web.RequestKey` instances
# instead of plain string keys in `request[...]`; the gateway writes
# `request["authenticated_agent"]`, `request["principal"]` and
# `request["request_id"]` in coordinator.auth_middleware, and the proxy writes
# `request["backend"]` / `request["key_attached"]`. Converting them is a
# DEPENDENCY-CURRENCY item (decision:1586), deferred and recorded in
# THIRD_PARTY.md's aiohttp row: 10 production and 12 test sites, spanning the
# auth middleware and the person-identity plumbing — a security surface that
# does not move as a side effect of a log-hygiene round. The filter is scoped
# to THIS module (never a global `filterwarnings` in a config file) and the
# class is imported rather than spelled as a string, so a rename in a future
# aiohttp is an ImportError here rather than a filter that silently stops
# matching. ⭐ When the conversion ships, this pin flips to "error".
pytestmark = pytest.mark.filterwarnings(
    f"ignore::{NotAppKeyWarning.__module__}.{NotAppKeyWarning.__qualname__}")


# ── Harness ───────────────────────────────────────────────────────────────────
#
# Sentinel bodies, not the real payloads: what is under test is ROUTING +
# AUTH + the guard, and the identity of the handler that ran is the whole
# question. `handle_health`'s payload is pinned by tests/test_telemetry_contract.py.

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
    """main()'s registration window: auth middleware, then the routes main() adds, then the unprotected-path assertion.

    No wildcard. tests/test_gateway_edge_hygiene.py::test_every_main_route_registration_has_a_counterpart_in_each_test_mirror
    checks this helper against that window. The app main() itself builds is
    tests/test_route_guard.py::test_no_catchall_is_registered.
    """
    app = web.Application(middlewares=[c.auth_middleware])
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _sentinel_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)

    proxy = g.AsyncHiveMindProxy()
    app.router.add_post("/v1/embeddings", proxy.handle_encoder)
    app.router.add_post("/v1/reranking", proxy.handle_encoder)
    app.router.add_post("/v1/chat/completions", proxy.handle_proxy)
    c.require_unprotected_paths_are_plain_routes(app.router)
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
def test_near_miss_spelling_requires_a_token(spelling):
    """Auth on. A trailing-slash or encoded spelling is not an unprotected path, so the middleware answers 401 before the router."""
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, _headers, body = _run(_probe(app, "GET", spelling))
    assert status == 401, f"{spelling} must require a token, got {status} {body!r}"


# ══════════════════════════════════════════════════════════════════════════
# Assertion 2b — SEC-1 (FIX ROUND). THE HOLE THE FIRST ROUND LEFT OPEN.
#
# `request.path` is fully percent-DECODED, so `/pool%2fstatus` reads there as
# `/pool/status` — a complete member of _UNPROTECTED_PATHS. The first round
# compared it and GRANTED the exemption, while aiohttp had matched the
# request on `rel_url.path_safe` (`/pool%2Fstatus`), which no PlainResource
# accepts, and routed it to the catch-all LLM proxy. Auth-exempt AND proxied:
# A1's exact shape, inside A1's own fix. Measured by the security reviewer:
#   /pool%2fstatus  anon-> 405  req_path='/pool/status'  canonical='/{tail}'
#                               LIMB2=True (exemption GRANTED)
# Only an unconditional 405 in _route_guard kept it out of the LLM.
#
# Note `/health%2f` is NOT the same case: it decodes to `/health/`, which is
# not a member, so it was already denied. The encoded slash has to fall
# INSIDE a path segment of a multi-segment route for the decode to
# reconstitute a complete key — which is why only /pool/status can express it
# and why a parametrisation over /health spellings could never have found it.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/pool%2fstatus", "/pool%2Fstatus"])
def test_encoded_slash_spelling_is_not_auth_exempt(spelling):
    """SEC-1. Under auth ON this must be a plain 401: the router did not
    resolve it to handle_pool_status, so the middleware must not exempt it.

    Asserting 401 rather than "not proxied" is deliberate — before the fix
    this was a 405, i.e. REFUSED, and a "did it reach the LLM" assertion
    would have passed on the defect. What is wrong with a 405 here is that it
    is issued by the catch-all's guard AFTER the exemption already waived
    authentication; the containment is accidental (see the auth-off test
    below), and this test pins the exemption itself."""
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, _headers, body = _run(_probe(app, "GET", spelling))
    assert status == 401, (
        f"{spelling} → {status} {body!r}: an encoded slash decodes to a "
        f"complete exempt key while the ROUTER matched something no route "
        f"owns. The exemption must compare rel_url.path_safe, not request.path"
    )


@pytest.mark.parametrize("spelling", ["/pool%2fstatus", "/pool%2Fstatus"])
def test_encoded_slash_on_auth_off_is_the_routers_404(spelling):
    """Auth off does not consult the exemption. The encoded slash matches no route, so the answer is aiohttp's own 404."""
    c, g = _load_gateway("")
    assert c.AUTH_CONFIGURED_AT_STARTUP is False, "sanity: this is the auth-off shape"
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", spelling))
    assert status == 404, body
    assert body.startswith(b"404:"), body
    assert headers.get("X-SM-Fault-Origin") == "gateway"


# ══════════════════════════════════════════════════════════════════════════
# Assertion 3 — LAYER 2. The only assertion that dies if the guard extension
# is dropped: an AUTHENTICATED near-miss must still never reach the proxy.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/health/", "/health//", "/health%2f", "/pool/status/"])
def test_authenticated_near_miss_is_the_routers_404(spelling):
    """A valid token does not create a route. The spelling matches nothing, so aiohttp's 404 answers."""
    c, g = _load_gateway("claude:tok_abc")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", spelling, token="tok_abc"))
    assert status == 404, f"{spelling} → {status} {body!r}"
    assert body.startswith(b"404:"), body
    assert headers.get("X-SM-Fault-Origin") == "gateway"


# ══════════════════════════════════════════════════════════════════════════
# Assertion 4 — LAYER 2 ON THE SHIPPED DEFAULT (auth off). The only
# assertion that covers a stranger's fresh install (adversarial review A-02).
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/health/", "/health//", "/health%2f", "/pool/status/"])
def test_near_miss_on_auth_off_is_the_routers_404(spelling):
    """Auth off reaches the router. These spellings are not routes, so the body is aiohttp's 404."""
    c, g = _load_gateway("")
    assert c.AUTH_CONFIGURED_AT_STARTUP is False, "sanity: this is the auth-off shape"
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", spelling))
    assert status == 404, f"{spelling} → {status} {body!r}"
    assert body.startswith(b"404:"), body
    assert headers.get("X-SM-Fault-Origin") == "gateway"


def test_auth_off_install_still_serves_exact_health_and_refuses_anonymous_chat():
    """Health stays anonymous. Chat does not: on an auth-off install only a spawned daemon passes."""
    c, g = _load_gateway("")
    app, _ = _build_gateway_app(c, g)
    status, _, body = _run(_probe(app, "GET", "/health"))
    assert status == 200 and b"handle_health" in body

    os.environ["SM_TEST_REAL_CHAT_IDENTITY"] = "1"
    try:
        app, _ = _build_gateway_app(c, g)
        status, headers, body = _run(_probe(app, "POST", "/v1/chat/completions"))
    finally:
        os.environ.pop("SM_TEST_REAL_CHAT_IDENTITY", None)
    assert status == 403, body
    assert b"framework_only" in body
    assert headers.get("X-SM-Fault-Origin") == "gateway"
    assert _LLM_BACKEND_HEADER not in headers


# ══════════════════════════════════════════════════════════════════════════
# Assertion 4b — SEC-3 (FIX ROUND). Branch ORDER inside _route_guard.
#
# The near-miss branch runs AFTER the reserved-prefix branch. When it ran
# first it also answered for `/memory/save/` and `/admin/backup/` — paths the
# prefix branch had always closed — and silently changed their 404 body. On an
# auth-off install that let an anonymous caller tell a REGISTERED route name
# from an unregistered one by appending a slash, a new route-existence oracle
# nobody named and no test pinned. Both messages are now pinned, so the order
# cannot flip back unremarked.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", ["/memory/save/", "/admin/backup/", "/memory/graph/"])
def test_trailing_slash_under_a_framework_prefix_is_not_a_route(spelling):
    """A trailing slash is a different path. Nothing is registered there, so aiohttp's 404 answers."""
    c, g = _load_gateway("")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", spelling))
    assert status == 404, f"{spelling} → {status} {body!r}"
    assert body.startswith(b"404:"), body
    assert headers.get("X-SM-Fault-Origin") == "gateway"


def test_reserved_prefix_registered_and_unregistered_are_indistinguishable():
    """The oracle, stated directly: appending a slash to a registered
    reserved-prefix route must not produce a different answer from appending
    it to a name the framework never registered."""
    c, g = _load_gateway("")
    app, _ = _build_gateway_app(c, g)
    _, _, registered = _run(_probe(app, "GET", "/memory/save/"))
    app, _ = _build_gateway_app(c, g)
    _, _, unregistered = _run(_probe(app, "GET", "/memory/no-such-route/"))
    assert registered.replace(b"/memory/save/", b"X") == \
           unregistered.replace(b"/memory/no-such-route/", b"X"), (
        "the reply distinguishes a registered reserved-prefix route from an "
        f"unregistered one:\n  {registered!r}\n  {unregistered!r}"
    )


def test_trailing_slash_on_health_is_not_a_route():
    """`/health/` is not the unprotected `/health` route."""
    c, g = _load_gateway("")
    app, _ = _build_gateway_app(c, g)
    status, headers, body = _run(_probe(app, "GET", "/health/"))
    assert status == 404, body
    assert body.startswith(b"404:"), body
    assert headers.get("X-SM-Fault-Origin") == "gateway"


# ══════════════════════════════════════════════════════════════════════════
# Assertion 5 — the exact-path limb keeps fact:1535's anonymous 405
# ══════════════════════════════════════════════════════════════════════════

def test_options_on_health_is_405_with_allow_header_not_401():
    """OPTIONS matches the /health resource and no OPTIONS handler. aiohttp's 405 names GET and HEAD. The path is unprotected, so this is not a 401 (fact:1535, wrong method on an owned route)."""
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
    """A newline in the path matches no resource. The exemption read must not raise, so auth answers 401."""
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


def test_startup_assertion_refuses_a_yarl_without_path_safe():
    """The exemption reads `rel_url.path_safe`, and `_router_match_path`
    DENIES on a non-str. Correct for a hostile input — but if the installed
    yarl simply did not expose the attribute, that same deny would fire on
    EVERY request: /health would 401 on every auth-on install, the daemons
    would lose their tokenless liveness probe, and nothing would say why. A
    silent availability failure wearing the shape of a safe deny is the worst
    kind, and no unit test that builds its own request double would ever see
    it. Refuse to boot instead, where an operator reads the reason."""
    c, g = _load_gateway("claude:tok_abc")
    app = web.Application()
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _sentinel_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)

    class _URLWithoutPathSafe:
        def __init__(self, *a, **kw):
            pass

    stub = types.ModuleType("yarl")
    stub.URL = _URLWithoutPathSafe
    with patch.dict(sys.modules, {"yarl": stub}):
        with pytest.raises(RuntimeError) as exc:
            c.require_unprotected_paths_are_plain_routes(app.router)
    assert "path_safe" in str(exc.value)
    # …and the real yarl still boots the same route table.
    c.require_unprotected_paths_are_plain_routes(app.router)


def test_startup_assertion_refuses_an_exempt_path_no_route_owns():
    """An unprotected path no route owns would be anonymous and then 404, or worse if a later route claimed it. Boot refuses that set."""
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


def test_main_runs_the_unprotected_path_assertion_after_the_chat_route():
    """The assertion is only worth anything if main() runs it after the chat route is registered."""
    with open(os.path.join(SCRIPTS_DIR, "hive_mind_proxy.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "require_unprotected_paths_are_plain_routes(app.router)" in src, (
        "hive_mind_proxy.main() must run the _UNPROTECTED_PATHS startup "
        "assertion — an invariant nothing checks is an intention"
    )
    chat_at = src.index('add_post("/v1/chat/completions", proxy.handle_proxy)')
    assert_at = src.index("require_unprotected_paths_are_plain_routes(app.router)")
    assert chat_at < assert_at, (
        "the assertion must run after the chat route is registered"
    )
    assert 'add_route("*", "/{tail:.*}"' not in src


# ══════════════════════════════════════════════════════════════════════════
# Adversarial review A-03 + fix round — the exemption's reader must return a
# REAL str for a REAL request, and it must be the string the ROUTER matched
# ══════════════════════════════════════════════════════════════════════════

def test_router_match_path_returns_the_routers_own_string_for_a_real_request():
    """`_router_match_path` DENIES on any non-str, which is correct and is
    what keeps a test double from granting an exemption. But that same
    property means a chain broken for a real request would fail silently in
    the direction that looks safe: /health would 401 everywhere and nothing
    would say why. Assert positively that a real routed request yields the
    string this fix is built on — and that it is `rel_url.path_safe`, the
    exact value aiohttp's `Resource.resolve` passes to `PlainResource._match`."""
    c, g = _load_gateway("claude:tok_abc")
    seen = {}

    async def _capturing_health(request):
        seen["match_path"] = c._router_match_path(request)
        seen["type"] = type(seen["match_path"]).__name__
        seen["router_input"] = request.rel_url.path_safe
        return web.json_response({"served_by": "handle_health"})

    app = web.Application(middlewares=[c.auth_middleware])
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", _capturing_health)
    app.router.add_get("/pool/status", _sentinel_pool_status)
    status, _, _ = _run(_probe(app, "GET", "/health"))
    assert status == 200
    assert isinstance(seen.get("match_path"), str), (
        f"the exemption's reader returned {seen.get('type')}, not str — it is "
        f"denying on every request and /health now requires a token"
    )
    assert seen["match_path"] == "/health"
    assert seen["match_path"] == seen["router_input"], (
        "the exemption must compare the SAME string the router matched on"
    )


def test_router_match_path_is_the_string_aiohttps_router_compares():
    """The equivalence the whole fix rests on, asserted against aiohttp's own
    source rather than against our belief about it (fact:1321 — check the
    instrument). `Resource.resolve` calls `self._match(request.rel_url
    .path_safe)`; `PlainResource._match` is `self._path == path`, and
    `_path` is the resource's `canonical`. If a future aiohttp renames the
    attribute or matches on something else, this dies and the exemption is
    re-examined instead of silently drifting back into the A1 gap."""
    import inspect
    from aiohttp import web_urldispatcher as u
    resolve_src = inspect.getsource(u.Resource.resolve)
    assert "self._match(request.rel_url.path_safe)" in resolve_src, (
        "aiohttp's Resource.resolve no longer matches on rel_url.path_safe — "
        "coordinator._router_match_path must be updated to whatever it now "
        f"compares, or the auth exemption and the router disagree again:\n{resolve_src}"
    )
    match_src = inspect.getsource(u.PlainResource._match)
    assert "self._path == path" in match_src, (
        f"PlainResource._match is no longer exact string equality:\n{match_src}"
    )


def test_router_match_path_denies_on_a_test_double():
    """The other half of the same property, stated so it cannot regress
    quietly: a MagicMock's auto-attribute is not a str and must not grant,
    and neither must a bare object with no rel_url at all (which an unguarded
    read would turn into an AttributeError → an unlogged 500 raised BEFORE
    the audit block)."""
    from unittest.mock import MagicMock
    c, _ = _load_gateway("claude:tok_abc")
    assert c._router_match_path(MagicMock()) is None
    assert c._router_match_path(object()) is None


def test_the_deleted_canonical_limb_is_gone_and_stays_gone():
    """SEC-2 (fix round). `_resolved_route_canonical` and its
    `match_info`-based limb were DEAD CODE: two reviewers independently
    measured that deleting the limb left the whole suite green, and
    `PlainResource._match` comparing `path_safe` makes it a strict subset of
    the surviving comparison by construction. Dead code in a security
    mechanism is worse than absent code — a future maintainer simplifying
    this keeps the documented half and deletes the operative one. Pin the
    deletion so it cannot creep back as "defence in depth"."""
    c, _ = _load_gateway("claude:tok_abc")
    assert not hasattr(c, "_resolved_route_canonical"), (
        "the resolved-route-canonical limb is redundant by construction: for "
        "a PlainResource, canonical == path_safe whenever the router matched. "
        "Reintroducing it adds a second opinion about what /health means, "
        "which is the defect class A1 belongs to."
    )
    with open(os.path.join(SCRIPTS_DIR, "coordinator.py"), encoding="utf-8") as fh:
        src = fh.read()
    exemption = src[src.index("_router_match_path(request) in _UNPROTECTED_PATHS")
                    - 400:src.index("_router_match_path(request) in _UNPROTECTED_PATHS") + 200]
    assert "request.path in _UNPROTECTED_PATHS" not in src, (
        "`request.path` is percent-DECODED — comparing it against the exempt "
        "set is the /pool%2fstatus hole. Compare rel_url.path_safe."
    )
    assert "request.path.rstrip" not in exemption, (
        "a normalised limb must never grant the exemption"
    )


# ══════════════════════════════════════════════════════════════════════════
# The stub-fidelity pin: the catch-all above stops at _route_guard, which is
# only faithful while _route_guard really is handle_proxy's first statement.
# ══════════════════════════════════════════════════════════════════════════

def test_handle_proxy_reads_raw_path_before_attaching_a_credential():
    with open(os.path.join(SCRIPTS_DIR, "hive_mind_proxy.py"), encoding="utf-8") as fh:
        src = fh.read()
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
    assert "raw_path" in ast.unparse(first), (
        "handle_proxy must read the raw path before it attaches a credential. "
        f"Got: {ast.unparse(first)}"
    )
