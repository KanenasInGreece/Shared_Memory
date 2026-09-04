"""HYG round (v0.9.89) — the gateway's request/response EDGE, pinned as values.

Three things live here, all of them about what the gateway does at the wire:

  S14 (R-A / R-A')  the encoder paths are REGISTERED routes with a dedicated
                    handler, so aiohttp's own router decides what reaches the
                    embedder — and every near spelling is refused instead of
                    being forwarded to a reasoning LLM.
  S13/S16e/S16f     which headers cross the gateway in each direction, and the
                    `Server` identity every response carries.
  the MIRROR pin    the two hand-written copies of main()'s route table that
                    live in other test files stay equal to main() itself.

⚠ WHY A REAL ROUTER. The registration is the whole point of S14: binding the
encoder paths to `handle_proxy` (or to anything that runs `_route_guard`
first) would 405 every legitimate `POST /v1/embeddings`, because the guard
returns 405 for ANY known key including one whose method is allowed — the
branch that is load-bearing for security fix A1. A bare proxy with a
hand-stuffed `_known_routes` cannot see that: it is a mocked router agreeing
with a mocked expectation. Every registration assertion below therefore goes
through a real `web.Application` built the way `main()` builds it, and the
proof of registration is a WRONG-METHOD 405 arriving from aiohttp's own
dispatch.
"""
import ast
import asyncio
import importlib
import json
import os
import re
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "shared-memory", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

PROXY_SRC_PATH = os.path.join(SCRIPTS_DIR, "hive_mind_proxy.py")
ROUTE_GUARD_TEST_PATH = os.path.join(os.path.dirname(__file__), "test_route_guard.py")
AUTH_EXEMPTION_TEST_PATH = os.path.join(
    os.path.dirname(__file__), "test_auth_exemption_route_resolution.py")

_LLM_BACKEND = "http://llm.invalid:5000"


# ══════════════════════════════════════════════════════════════════════════
# Doubles
# ══════════════════════════════════════════════════════════════════════════

class _CapturingSession:
    """Records the (method, url, headers) handed to .request() and raises —
    proving control reached the real upstream-dispatch code. The raise is
    turned into a 500 by the forward's own exception handling, which is how a
    HIT is told apart from a refusal (404/405/403). Same idiom as
    tests/test_route_guard.py::_CapturingSession."""
    closed = False

    def __init__(self):
        self.captured = None
        self.headers = None

    def request(self, method, url, **kw):
        self.captured = (method, url)
        self.headers = kw.get("headers")
        raise RuntimeError("captured — proves dispatch reached the upstream call")


class _MustNotCallSession:
    """.request() raising AssertionError proves the refusal fired before any
    upstream call was attempted. Deliberately NOT usable for a hit: an
    AssertionError here would be reported as a refusal that passed, which is
    the false green this class exists to prevent (idiom from
    tests/test_credentialed_route_allowlist.py:36-44)."""
    closed = False

    def request(self, *a, **kw):
        raise AssertionError(
            "must not reach the upstream call — the gateway should have refused first")


# ══════════════════════════════════════════════════════════════════════════
# The app, built the way main() builds it
# ══════════════════════════════════════════════════════════════════════════

def _load_gateway(monkeypatch, *, backends=None):
    """Reload coordinator + hive_mind_proxy with AGENT_TOKENS unset — the
    SHIPPED DEFAULT (.env.example ships it unset), which is also the install
    where routing is the only containment there is."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        backends if backends is not None
        else [{"url": _LLM_BACKEND, "private_ok": True}]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return coordinator, g


def _build_app_like_main(c, g, proxy):
    """main()'s registration window, in main()'s order:
      attach_coordinator → /health → /pool/status → /v1/embeddings →
      /v1/reranking → set_known_routes →
      require_unprotected_paths_are_plain_routes → catch-all.

    Kept equal to main() by
    test_every_main_route_registration_has_a_counterpart_in_each_test_mirror
    below, which reads main()'s source rather than trusting this comment."""
    from unittest.mock import AsyncMock
    app = web.Application(middlewares=[c.auth_middleware])
    app["proxy"] = proxy                 # handle_health reads it
    app["coordinator"] = AsyncMock()
    app.on_response_prepare.append(g._set_server_header)
    g.attach_coordinator(app, AsyncMock())
    app.router.add_get("/health", g.handle_health)
    app.router.add_get("/pool/status", g.handle_pool_status)
    app.router.add_post("/v1/embeddings", proxy.handle_encoder)
    app.router.add_post("/v1/reranking", proxy.handle_encoder)
    proxy.set_known_routes(app.router)
    c.require_unprotected_paths_are_plain_routes(app.router)
    app.router.add_route("*", "/{tail:.*}", proxy.handle_proxy)
    return app


async def _probe(app, method, spelling, *, headers=None):
    """Drive the app over a real socket. `encoded=True` stops yarl
    re-encoding, so `/v1%2fembeddings` and `/v1/embeddings/..%2fx` arrive on
    the wire exactly as written."""
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.request(method, URL(spelling, encoded=True),
                                    headers=headers or {}, data=b"{}")
        body = await resp.read()
        return resp.status, dict(resp.headers), body
    finally:
        await client.close()


def _run(coro):
    return asyncio.run(coro)


def _probe_with(monkeypatch, session, method, spelling, *, backends=None, headers=None):
    c, g = _load_gateway(monkeypatch, backends=backends)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    app = _build_app_like_main(c, g, proxy)
    return _run(_probe(app, method, spelling, headers=headers)), g


# ══════════════════════════════════════════════════════════════════════════
# S14 — the encoder paths are real routes (R-A)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path,url_attr", [
    ("/v1/embeddings", "EMBEDDER_URL"),
    ("/v1/reranking", "RERANKER_URL"),
])
def test_post_to_an_encoder_path_reaches_the_encoder_backend(monkeypatch, path, url_attr):
    """THE OUTAGE TEST. Bound to handle_proxy, or wrapped in _route_guard,
    this is a 405 — and every mocked test in the suite would still be green,
    because only a real router produces the 405. A capturing session, never
    the must-not-call one: this case is a HIT."""
    session = _CapturingSession()
    (status, headers, body), g = _probe_with(monkeypatch, session, "POST", path)
    assert session.captured is not None, (
        f"POST {path} never reached the upstream call — got {status} {body!r}. "
        f"The encoder route is not registered to a dedicated handler.")
    assert session.captured[0] == "POST"
    assert session.captured[1] == f"{getattr(g, url_attr)}{path}"
    assert status not in (404, 405), f"{path} was refused: {status} {body!r}"


def test_no_credential_is_ever_attached_on_the_encoder_path(monkeypatch):
    """The encoders are framework-local. Even with a fully credentialed LLM
    fleet configured, nothing on this path may carry a provider key —
    `llm_backend` stays None, which is what gates the token block."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-encoder-must-not-see-this")
    session = _CapturingSession()
    (status, _, _), _ = _probe_with(
        monkeypatch, session, "POST", "/v1/embeddings",
        backends=[{"url": "https://api.deepseek.com/v1",
                   "token_env": "DEEPSEEK_API_KEY", "private_ok": True}])
    assert session.captured is not None
    assert "Authorization" not in (session.headers or {})
    assert "sk-encoder-must-not-see-this" not in json.dumps(dict(session.headers or {}))


def test_query_string_on_an_encoder_path_is_still_forwarded(monkeypatch):
    """R-B is for CREDENTIALED routes only (ADV2-8). The encoder path is not
    one — a query here is forwarded verbatim, exactly as before."""
    session = _CapturingSession()
    (status, _, body), g = _probe_with(monkeypatch, session, "POST", "/v1/embeddings?a=b")
    assert session.captured is not None, f"refused instead of forwarded: {status} {body!r}"
    assert session.captured[1] == f"{g.EMBEDDER_URL}/v1/embeddings?a=b"


def test_wrong_method_on_an_encoder_path_is_405_with_allow_post(monkeypatch):
    """THE REGISTRATION PROOF. This 405 can only come from a real router
    dispatching a known path to the catch-all, whose guard then reads the
    route snapshot. An unregistered /v1/embeddings would be an LLM dispatch."""
    (status, headers, body), _ = _probe_with(
        monkeypatch, _MustNotCallSession(), "GET", "/v1/embeddings")
    assert status == 405, f"got {status} {body!r} — the route is not registered"
    assert headers.get("Allow") == "POST"
    assert headers.get("X-SM-Fault-Origin") == "gateway"
    assert b"NOT forwarded" in body


def test_trailing_slash_encoder_spelling_is_404_not_forwarded(monkeypatch):
    """CLIENT-VISIBLE CHANGE (CHANGELOG line owed): `POST /v1/embeddings/`
    reached the embedder before this round, because handle_proxy prefix-
    matched it. Now /v1/embeddings is a known key, so the guard's
    trailing-slash near-miss branch answers it."""
    (status, headers, body), _ = _probe_with(
        monkeypatch, _MustNotCallSession(), "POST", "/v1/embeddings/")
    assert status == 404, f"got {status} {body!r}"
    assert headers.get("X-SM-Fault-Origin") == "gateway"
    assert b"near-miss spelling" in body
    assert b"registers /v1/embeddings exactly" in body


@pytest.mark.parametrize("spelling", ["/v1%2fembeddings", "/v1%2Fembeddings"])
def test_encoded_slash_encoder_spelling_is_405_never_the_encoder(monkeypatch, spelling):
    """A1 containment, now covering the encoder names too: aiohttp matched on
    `rel_url.path_safe` (`/v1%2fembeddings`), which no route owns, so this
    lands on the catch-all — where the guard reads the DECODED path, finds
    the known key, and stops it unconditionally."""
    (status, headers, body), _ = _probe_with(
        monkeypatch, _MustNotCallSession(), "POST", spelling)
    assert status == 405, f"{spelling} → {status} {body!r}"
    assert headers.get("Allow") == "POST"
    assert b"NOT forwarded" in body


@pytest.mark.parametrize("spelling", [
    "/v1/embeddingsX",
    "/v1/embeddings/anything",
    "/v1/embeddings/..%2fx",
    "/v1/rerankingX",
])
def test_encoder_prefix_near_miss_is_404_never_an_llm_dispatch(monkeypatch, spelling):
    """R-A' (ruled over ADV1-A4). These spellings were prefix-matched to the
    encoder before this round. Once the exact path became a registered route
    they would have fallen through to the REASONING-LLM pool — a mistyped
    framework call answered by a chat model, which is precisely the fact:1535
    defect. `_encoder_near_miss` turns them into 404s in the guard's voice."""
    (status, headers, body), _ = _probe_with(
        monkeypatch, _MustNotCallSession(), "POST", spelling)
    assert status == 404, f"{spelling} → {status} {body!r}"
    assert headers.get("X-SM-Fault-Origin") == "gateway"
    assert b"NOT forwarded to any LLM backend" in body
    assert b"X-SM-LLM-Backend" not in body
    assert headers.get("X-SM-LLM-Backend") is None


async def _raw_probe(app, method, target):
    """Write the request line VERBATIM onto the socket.

    ⚠ INSTRUMENT NOTE (fact:1321 — check the instrument, not just the result).
    aiohttp's own CLIENT resolves dot segments before it sends: driven through
    TestClient, `POST /v1/embeddings/../x` leaves as `POST /v1/x` and the
    gateway never sees the traversal spelling at all — measured, and the
    reason this helper exists. A conforming client therefore cannot express
    that spelling; a hand-rolled one, a scanner or a mis-templated URL can,
    and this is what the gateway does when it arrives."""
    server = TestServer(app)
    await server.start_server()
    try:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.write(
            f"{method} {target} HTTP/1.1\r\n"
            f"Host: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            .encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
        return raw
    finally:
        await server.close()


def test_a_raw_traversal_spelling_of_an_encoder_path_is_404(monkeypatch):
    """R-A' against the spelling only a raw socket can send. The gateway's
    own `request.path` is `/v1/embeddings/../x` here — under the encoder
    path, not equal to it — so `_encoder_near_miss` answers it."""
    c, g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    app = _build_app_like_main(c, g, proxy)
    raw = _run(_raw_probe(app, "POST", "/v1/embeddings/../x"))
    assert raw.startswith(b"HTTP/1.1 404"), raw[:200]
    assert b"NOT forwarded to any LLM backend" in raw
    assert b"X-SM-LLM-Backend" not in raw


def test_llm_passthrough_is_untouched(monkeypatch):
    """The counterweight. A 404-happy edge would be a far worse regression
    than the hole: /v1/chat/completions is a supported passthrough and must
    still dispatch to the pool."""
    session = _CapturingSession()
    (status, headers, body), _ = _probe_with(
        monkeypatch, session, "POST", "/v1/chat/completions")
    assert session.captured is not None, f"the LLM passthrough was refused: {status} {body!r}"
    assert session.captured[1].startswith(_LLM_BACKEND)


def test_a_non_framework_path_outside_the_encoder_prefixes_still_dispatches(monkeypatch):
    """`_encoder_near_miss` must key on the encoder paths only — an ordinary
    passthrough path that merely lives under /v1/ is not a near-miss."""
    session = _CapturingSession()
    (status, _, body), _ = _probe_with(monkeypatch, session, "POST", "/v1/models")
    assert session.captured is not None, f"refused a legitimate passthrough: {status} {body!r}"


def test_encoder_near_miss_helper_is_pure_and_total(monkeypatch):
    """The helper on its own, so a mutation of the pure function is visible
    without a socket. Values, not shapes (fact:1309)."""
    _, g = _load_gateway(monkeypatch)
    assert g._encoder_near_miss("/v1/embeddings") is None      # the exact path is a ROUTE
    assert g._encoder_near_miss("/v1/chat/completions") is None
    assert g._encoder_near_miss("/") is None
    assert g._encoder_near_miss("/v1/embeddingsX") == "/v1/embeddings"
    assert g._encoder_near_miss("/v1/embeddings/") == "/v1/embeddings"
    assert g._encoder_near_miss("/v1/embeddings/../x") == "/v1/embeddings"
    assert g._encoder_near_miss("/v1/rerankingZ") == "/v1/reranking"


def test_the_encoder_routes_are_plain_resources(monkeypatch):
    """The guard's near-miss branch only looks at STATIC keys, and
    require_unprotected_paths_are_plain_routes has the same requirement of
    everything it inspects. A dynamic encoder route would silently drop out
    of the trailing-slash refusal above."""
    c, g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    app = _build_app_like_main(c, g, proxy)
    for key in ("/v1/embeddings", "/v1/reranking"):
        assert key in proxy._known_routes, f"{key} missing from the route snapshot"
        assert proxy._known_routes[key]["pattern"] is None, (
            f"{key} registered as a DynamicResource — the guard's near-miss "
            f"branch skips patterns, so its trailing-slash spelling would be "
            f"forwarded to the LLM pool")
        assert proxy._known_routes[key]["methods"] == {"POST"}


def test_handle_proxy_no_longer_routes_on_a_path_prefix(monkeypatch):
    """The deletion, pinned. A `startswith` over ROUTING_MAP inside
    handle_proxy is what made every near-miss reach the embedder; rewriting
    it as an exact match would put a second opinion about what
    /v1/embeddings means next to the router's."""
    src = open(PROXY_SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_proxy":
            fn = node
    assert fn is not None
    body_src = ast.unparse(fn)
    assert "ROUTING_MAP" not in body_src, (
        "handle_proxy still consults ROUTING_MAP — the encoder target belongs "
        "to handle_encoder, reached through its own registration")


# ══════════════════════════════════════════════════════════════════════════
# The MIRROR pin (ADV1-A6)
# ══════════════════════════════════════════════════════════════════════════

# Named verbs only. `add_route("*", "/{tail:.*}", ...)` is the CATCH-ALL, which
# lives outside main()'s registration window by construction (it is added after
# set_known_routes) — matching it here would make every mirror that installs a
# catch-all differ from main() for a reason that is not a mirror defect.
_ADD_ROUTE_RE = re.compile(
    r'app\.router\.add_(get|post|put|patch|delete|head|options)\(\s*"([^"]+)"')


def _registrations(source: str) -> set:
    """(verb, path) for every literal app.router.add_*() in `source`."""
    return {(verb.lower(), path) for verb, path in _ADD_ROUTE_RE.findall(source)}


def _main_registration_window() -> str:
    src = open(PROXY_SRC_PATH, encoding="utf-8").read()
    start = src.index("attach_coordinator(app, coordinator)")
    end = src.index("proxy.set_known_routes(app.router)")
    assert start < end
    return src[start:end]


def _function_source(path: str, name: str) -> str:
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in {path}")


@pytest.mark.parametrize("path,helper", [
    (ROUTE_GUARD_TEST_PATH, "_build_real_gateway_app"),
    (AUTH_EXEMPTION_TEST_PATH, "_build_gateway_app"),
    (os.path.join(os.path.dirname(__file__), "test_gateway_edge_hygiene.py"),
     "_build_app_like_main"),
])
def test_every_main_route_registration_has_a_counterpart_in_each_test_mirror(path, helper):
    """ADV1-A6. Three test files hand-write main()'s registration window. A
    route added to main() and not to a mirror does not fail those tests — it
    makes every assertion in them run against a route table the gateway does
    not have, and pass. That is how a registration defect ships green."""
    expected = _registrations(_main_registration_window())
    assert ("get", "/health") in expected, "instrument check: the window is empty"
    assert ("post", "/v1/embeddings") in expected
    got = _registrations(_function_source(path, helper))
    assert got == expected, (
        f"{os.path.basename(path)}::{helper} registers {sorted(got)}, "
        f"hive_mind_proxy.main() registers {sorted(expected)} between "
        f"attach_coordinator and set_known_routes. Mirror it exactly.")


# ══════════════════════════════════════════════════════════════════════════
# S13 / S16f — which headers cross the gateway
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("header", [
    "Cookie", "X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host", "X-Real-IP",
])
def test_client_origin_headers_are_not_forwarded_upstream(monkeypatch, header):
    """A backend has no business learning who our caller is or what network
    they came from — least-disclosure at the gateway boundary (R-C)."""
    session = _CapturingSession()
    (_, _, _), _ = _probe_with(
        monkeypatch, session, "POST", "/v1/chat/completions",
        headers={header: "leak-me"})
    assert session.headers is not None
    lowered = {k.lower() for k in session.headers}
    assert header.lower() not in lowered, (
        f"{header} reached the upstream backend: {sorted(session.headers)}")


@pytest.mark.parametrize("header", ["Content-Type", "Accept", "User-Agent", "Referer"])
def test_headers_r_c_deliberately_left_alone_still_reach_upstream(monkeypatch, header):
    """The counterweight to the strips: R-C is a DENYLIST, and Referer /
    User-Agent were deliberately left on it (§5 FYI). If a later change turns
    the denylist into an allowlist, this dies rather than silently narrowing
    what providers receive."""
    session = _CapturingSession()
    (_, _, _), _ = _probe_with(
        monkeypatch, session, "POST", "/v1/chat/completions",
        headers={header: "kept"})
    assert session.headers is not None
    lowered = {k.lower() for k in session.headers}
    assert header.lower() in lowered, (
        f"{header} was stripped — R-C added no allowlist: {sorted(session.headers)}")


def test_exactly_one_authorization_header_reaches_a_credentialed_upstream(monkeypatch):
    """`upstream_headers["Authorization"] = ...` is __setitem__ on a plain
    dict, so the client's own gateway token (already stripped by
    _filter_headers) can never survive alongside the provider key. Pinned as
    a COUNT: two Authorization values on the wire is a credential-confusion
    bug no status code would show."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-one-only")
    session = _CapturingSession()
    (_, _, _), _ = _probe_with(
        monkeypatch, session, "POST", "/v1/chat/completions",
        backends=[{"url": "https://api.deepseek.com/v1",
                   "token_env": "DEEPSEEK_API_KEY", "private_ok": True}],
        headers={"Authorization": "Bearer client-gateway-token"})
    assert session.headers is not None
    auth = [k for k in session.headers if k.lower() == "authorization"]
    assert len(auth) == 1, f"expected exactly one Authorization, got {auth}"
    assert session.headers[auth[0]] == "Bearer sk-one-only", (
        "the client's own gateway token reached the provider")


def test_filter_headers_strips_set_cookie_on_the_response_direction(monkeypatch):
    """R-C'. A backend that sets a cookie must not be able to plant one in
    our caller's browser through us. Request direction is unaffected —
    Cookie is stripped there by its own rule, above."""
    _, g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    filtered = proxy._filter_headers(
        {"Set-Cookie": "sid=1", "Content-Type": "application/json"},
        strip_gateway_namespace=True)
    assert "Content-Type" in filtered
    assert not any(k.lower() == "set-cookie" for k in filtered), filtered


def test_cors_headers_are_deliberately_not_stripped(monkeypatch):
    """DEFERRED, not forgotten: the CORS strip went to the small-rulings
    bundle because the monitor's dependency on it is unverified (R-C'). This
    pins the deferral so shipping it later is a deliberate act."""
    _, g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    filtered = proxy._filter_headers(
        {"Access-Control-Allow-Origin": "*"}, strip_gateway_namespace=True)
    assert filtered.get("Access-Control-Allow-Origin") == "*"


# ══════════════════════════════════════════════════════════════════════════
# S16e — the Server identity
# ══════════════════════════════════════════════════════════════════════════

def test_health_reports_the_gateways_own_server_header(monkeypatch):
    """/health is unauthenticated on the shipped default, and it used to
    answer `Python/3.14 aiohttp/3.14.3` — a free version-disclosure to any
    anonymous caller. aiohttp 3.14.3 has no `server_header` parameter
    (measured), so the single mechanism is an on_response_prepare handler."""
    c, g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    app = _build_app_like_main(c, g, proxy)
    status, headers, _ = _run(_probe(app, "GET", "/health"))
    assert headers.get("Server") == "shared-memory-gateway", (
        f"got Server={headers.get('Server')!r} on /health")


def test_a_proxied_response_does_not_relay_the_upstream_server_header(monkeypatch):
    """The other half: a refusal and a relayed response must carry the same
    identity, or the header itself tells a caller which path answered."""
    (status, headers, _), _ = _probe_with(
        monkeypatch, _MustNotCallSession(), "GET", "/v1/embeddings")
    assert status == 405
    assert headers.get("Server") == "shared-memory-gateway"


def test_main_registers_the_server_header_handler(monkeypatch):
    """The handler is only worth anything if the gateway entrypoint installs
    it — source-level, because main() cannot be executed in a unit test.
    _build_app_like_main above installs it via the same helper name."""
    src = open(PROXY_SRC_PATH, encoding="utf-8").read()
    assert "app.on_response_prepare.append(" in src, (
        "main() must register the Server-header handler on on_response_prepare")
    assert '"shared-memory-gateway"' in src


# ══════════════════════════════════════════════════════════════════════════
# D6 — the gateway half of the httpx log-hygiene item
# ══════════════════════════════════════════════════════════════════════════

def test_hive_mind_proxy_pins_the_httpx_logger_to_warning(monkeypatch):
    """hive_mind_proxy.py never imports httpx; coordinator.py does and runs in
    this process under this module's root config. Lane α' (step 2) owns the
    three-module version of this assertion; this one keeps the gateway half
    honest on its own."""
    import logging
    _, g = _load_gateway(monkeypatch)
    assert logging.getLogger("httpx").level == logging.WARNING
