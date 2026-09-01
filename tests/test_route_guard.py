"""route-guard (fact:1535, corrects fact:1534's HIGH): a mistyped or
wrong-method framework request must FAIL AND SAY WHY — never fall through
the catch-all into a reasoning-LLM dispatch. A shipped MCP tool once did
exactly this — GET against a path the gateway registers POST-only — and the
request was silently forwarded to the LLM pool as if it were a chat
completion.

Two parts:
  Part 1 — hive_mind_proxy.AsyncHiveMindProxy._route_guard / set_known_routes:
           405 on a known path + wrong method, 404 on an unregistered path
           under a reserved framework prefix, byte-for-byte unchanged
           passthrough for everything else.
  Part 2 — the contract test: every HTTP call BOTH clients make must exist,
           with that method, in the gateway's registered route set — proven
           (below) to catch the exact wrong-method defect this guard exists
           for.
"""
import ast
import asyncio
import importlib
import importlib.util
import json
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "shared-memory", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

VECTOR_SKILL_PATH = os.path.join(REPO_ROOT, "mcp", "vector-skill.py")
# The framework-side source of truth (shared-memory-skill/ is the delivered
# copy, held in parity by test_sync_skills.py) — this is the file the
# gateway host actually runs against.
MEMORY_BRIDGE_SRC_PATH = os.path.join(SCRIPTS_DIR, "memory_bridge.py")


def _fresh_gateway(monkeypatch):
    """Reload coordinator + hive_mind_proxy fresh, matching the isolation
    idiom in tests/test_routing_fix_round.py."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def _req(method: str, path: str, headers: dict | None = None, body: bytes = b""):
    """Same shape as tests/test_routing_fix_round.py's `_req` — a dict
    subclass so request['authenticated_agent']-style reads default to None.
    `content_length` is needed by the ROUTING_MAP (embeddings/reranking)
    buffering branch in handle_proxy, which the guard's no-op tests exercise."""
    class _Req(dict):
        pass
    r = _Req()
    r.method = method
    r.path = path
    r.rel_url = path
    r.headers = headers or {}
    r.can_read_body = True
    r.content_length = len(body)

    async def read():
        return body
    r.read = read
    return r


def _build_real_gateway_app(g) -> web.Application:
    """The gateway's actual registered route set (attach_coordinator +
    /health + /pool/status), EXCLUDING the catch-all — exactly what
    set_known_routes() is meant to see, and exactly what main() builds
    before adding the catch-all route. `coordinator` is a MagicMock: attach()
    only needs the handler ATTRIBUTES to exist for registration, it never
    calls them."""
    app = web.Application()
    # AsyncMock, not MagicMock: attach() registers coordinator.handle_* as
    # aiohttp route handlers, and aiohttp warns ("bare functions are
    # deprecated") when a registered handler isn't a coroutine function —
    # cosmetic only (never invoked in these tests), but AsyncMock's
    # auto-speccing attributes are coroutine functions, so it's free to avoid.
    mock_coordinator = AsyncMock()
    g.attach_coordinator(app, mock_coordinator)
    app.router.add_get("/health", g.handle_health)
    app.router.add_get("/pool/status", g.handle_pool_status)
    return app


def _build_proxy_with_known_routes(g, *, with_catchall: bool = False):
    app = _build_real_gateway_app(g)
    proxy = g.AsyncHiveMindProxy()
    if with_catchall:
        # Deliberately the WORST-CASE order: the catch-all is already
        # registered when the snapshot is taken. set_known_routes() must be
        # correct anyway (its wildcard-method filter, not startup ordering,
        # is what excludes the catch-all) — RG-2/RG-3 review fix.
        app.router.add_route("*", "/{tail:.*}", proxy.handle_proxy)
    proxy.set_known_routes(app.router)
    return app, proxy


# ══════════════════════════════════════════════════════════════════════════
# Part 1 — the gateway guard
# ══════════════════════════════════════════════════════════════════════════

def test_wrong_method_on_a_post_only_memory_route_405(monkeypatch):
    """The shape the guard exists for: a GET at a path the gateway registers
    POST-only. It must 405 and name the method AND the path, never fall
    through to the LLM pool."""
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/memory/save")))
    assert resp.status == 405
    assert resp.headers["Allow"] == "POST"
    body = json.loads(resp.body.decode())
    assert "GET" in body["error"]
    assert "/memory/save" in body["error"]
    assert "NOT forwarded" in body["error"]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unknown_path_under_memory_prefix_404(monkeypatch, method):
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    resp = asyncio.run(proxy.handle_proxy(_req(method, "/memory/no-such-route")))
    assert resp.status == 404
    body = json.loads(resp.body.decode())
    assert "/memory/no-such-route" in body["error"]
    assert "NOT forwarded" in body["error"]


def test_unknown_path_under_admin_prefix_404(monkeypatch):
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/admin/no-such-route")))
    assert resp.status == 404
    assert "/admin/no-such-route" in json.loads(resp.body.decode())["error"]


def test_wrong_method_on_health_405(monkeypatch):
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/health")))
    assert resp.status == 405
    assert resp.headers["Allow"] == "GET, HEAD"


def test_wrong_method_on_memory_telemetry_405(monkeypatch):
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/memory/telemetry")))
    assert resp.status == 405
    assert resp.headers["Allow"] == "GET, HEAD"


def test_dynamic_status_route_wrong_method_405(monkeypatch):
    """/memory/status/{pg_id} is a DynamicResource — the guard must match it
    by resource PATTERN, not string equality, per the brief."""
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/memory/status/42")))
    assert resp.status == 405
    assert resp.headers["Allow"] == "GET, HEAD"


class _CapturingSession:
    """Records the (method, url) handed to .request() and raises — proving
    control reached the real upstream-dispatch code, never intercepted by
    the guard. Mirrors the _RaisingSession idiom in test_routing_fix_round.py."""
    closed = False

    def __init__(self):
        self.captured = None

    def request(self, method, url, **kw):
        self.captured = (method, url)
        raise RuntimeError("captured — proves dispatch reached the upstream call")


def test_v1_embeddings_still_dispatches_unchanged(monkeypatch):
    """A non-framework path (ROUTING_MAP passthrough) must reach exactly the
    same upstream-call attempt as before the guard existed — the guard is a
    pure no-op here, guard field populated exactly as it is in production."""
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    session = _CapturingSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/embeddings")))
    assert session.captured is not None, "guard intercepted a non-framework path"
    assert session.captured[1].startswith(g.EMBEDDER_URL)
    assert resp.status not in (404, 405)


def test_v1_chat_completions_still_dispatches_unchanged(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "private_ok": True}]))
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g)
    session = _CapturingSession()
    proxy.session = session
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}],
                        "model": "local-model"}).encode()
    req = _req("POST", "/v1/chat/completions", body=body)
    resp = asyncio.run(proxy.handle_proxy(req))
    assert session.captured is not None, "guard intercepted the LLM-pool path"
    assert session.captured[1].startswith("http://a:5000")
    assert resp.status not in (404, 405)


def test_route_view_derived_from_router_not_hand_written(monkeypatch):
    """decision:1032 class — set_known_routes must derive from the actual
    router, so a route this test never lists anywhere is still caught."""
    g = _fresh_gateway(monkeypatch)
    app = web.Application()

    async def _stub_handler(request):
        return web.json_response({})
    app.router.add_put("/some/new/framework/route", _stub_handler)
    proxy = g.AsyncHiveMindProxy()
    proxy.set_known_routes(app.router)
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/some/new/framework/route")))
    assert resp.status == 405
    assert resp.headers["Allow"] == "PUT"


def test_catchall_excluded_from_known_routes(monkeypatch):
    """The snapshot must exclude the catch-all's wildcard resource EVEN WHEN
    it is taken after the catch-all is registered (the helper registers it
    first on purpose) — the guard's correctness must come from
    set_known_routes() itself, never from main()'s call ordering. A swapped
    ordering in main() must be harmless; removing the wildcard-method filter
    must kill this test (RG-2/RG-3 review fix)."""
    g = _fresh_gateway(monkeypatch)
    _, proxy = _build_proxy_with_known_routes(g, with_catchall=True)
    for key, entry in proxy._known_routes.items():
        assert "*" not in entry["methods"], (
            f"wildcard method captured for {key} — the catch-all leaked into "
            "the known-routes snapshot")
        assert "tail" not in key, (
            f"catch-all resource {key} leaked into the known-routes snapshot")
    # Behavioural proof in the same worst-case order: the LLM passthrough
    # still dispatches, and the guard still refuses what it should.
    session = _CapturingSession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_req("POST", "/v1/embeddings")))
    assert session.captured is not None
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/memory/save")))
    assert resp.status == 405 and resp.headers["Allow"] == "POST"
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/memory/no-such-route")))
    assert resp.status == 404


# ══════════════════════════════════════════════════════════════════════════
# Part 2 — the contract test
# ══════════════════════════════════════════════════════════════════════════

def _extract_client_http_calls(src_path: str) -> list[tuple[str, str]]:
    """AST-extract every (METHOD, path_template) an httpx client call makes,
    reading `client.get(...)` / `client.post(...)` calls where the first
    positional arg is an f-string. The f-string's FIRST interpolation is
    always the base-URL variable (COORDINATOR_BASE, or a local alias of it —
    both clients use this shape throughout) and is dropped; any LATER
    interpolation is a dynamic path segment, rendered as the wildcard "{*}"
    (matches aiohttp's `{name}` dynamic resources one-for-one, since both
    are exactly one path segment)."""
    src = open(src_path).read()
    tree = ast.parse(src)
    calls: list[tuple[str, str]] = []

    def render_path(node):
        if isinstance(node, ast.JoinedStr):
            parts, seen_base = [], False
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    if not seen_base:
                        seen_base = True
                    else:
                        parts.append("{*}")
                else:
                    parts.append("{*}")
            return "".join(parts)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            self.generic_visit(node)
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("get", "post"):
                return
            if not (isinstance(func.value, ast.Name) and func.value.id == "client"):
                return
            if not node.args:
                return
            template = render_path(node.args[0])
            if template is None or not template.startswith("/"):
                return
            calls.append((func.attr.upper(), template))

    V().visit(tree)
    return calls


def _extractor_finds_every_call_site(src_path: str, calls: list) -> None:
    """Instrument check (fact:1321 class): a query that runs is not a query
    that answers what was asked — prove the AST extractor didn't silently
    drop (or double-count) a call site, via an independent textual count."""
    src = open(src_path).read()
    tree = ast.parse(src)
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncWith, ast.With)):
            for item in n.items:
                ce = item.context_expr
                fn = ce.func if isinstance(ce, ast.Call) else None
                label = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if "client" in label.lower() and isinstance(
                        item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
    independent_count = sum(
        len(re.findall(rf"\b{re.escape(n)}\.(?:get|post)\(", src))
        for n in names)
    assert len(calls) == independent_count, (
        f"{src_path}: AST extractor found {len(calls)} client.get/post call(s), "
        f"independent regex count found {independent_count} — the extractor "
        f"silently dropped or double-counted a call site.")


async def _resolve(app: web.Application, method: str, path: str) -> bool:
    req = make_mocked_request(method, path, app=app)
    match_info = await app.router.resolve(req)
    return getattr(match_info, "http_exception", "missing") is None


def _router_accepts(app: web.Application, method: str, template: str) -> bool:
    path = template.replace("{*}", "42")
    return asyncio.run(_resolve(app, method, path))


def test_vector_skill_calls_all_registered(monkeypatch):
    g = _fresh_gateway(monkeypatch)
    app = _build_real_gateway_app(g)
    calls = _extract_client_http_calls(VECTOR_SKILL_PATH)
    _extractor_finds_every_call_site(VECTOR_SKILL_PATH, calls)
    assert calls, "extractor found nothing — the pattern probably drifted"
    for method, template in calls:
        assert _router_accepts(app, method, template), (
            f"mcp/vector-skill.py calls {method} {template!r}, which the "
            f"gateway's registered route set does not accept")


def test_memory_bridge_calls_all_registered(monkeypatch):
    g = _fresh_gateway(monkeypatch)
    app = _build_real_gateway_app(g)
    calls = _extract_client_http_calls(MEMORY_BRIDGE_SRC_PATH)
    _extractor_finds_every_call_site(MEMORY_BRIDGE_SRC_PATH, calls)
    assert calls, "extractor found nothing — the pattern probably drifted"
    for method, template in calls:
        assert _router_accepts(app, method, template), (
            f"memory_bridge.py calls {method} {template!r}, which the "
            f"gateway's registered route set does not accept")


def test_contract_catches_the_wrong_method_defect_it_exists_for(monkeypatch):
    """Proof the check has teeth (measured, re-runnable): the defect FORM —
    a GET at a path registered POST-only — must be rejected by the same
    router-resolution the two tests above use for the real client calls,
    while the right method is accepted. A check that has only ever passed
    has not been tested."""
    g = _fresh_gateway(monkeypatch)
    app = _build_real_gateway_app(g)
    assert not _router_accepts(app, "GET", "/memory/save")
    assert _router_accepts(app, "POST", "/memory/save")


def test_contract_catches_an_unregistered_future_call(monkeypatch):
    g = _fresh_gateway(monkeypatch)
    app = _build_real_gateway_app(g)
    assert not _router_accepts(app, "POST", "/memory/does-not-exist")


# ══════════════════════════════════════════════════════════════════════════
# Part 3 — the registered route set, pinned exhaustively
# ══════════════════════════════════════════════════════════════════════════

# Every (METHOD, path) the gateway registers. EXHAUSTIVE, and a set rather than
# a subset check for the same reason the CLI action set is: the contract runs in
# BOTH directions. A route appearing is a surface a token can now reach and an
# auth table (`_WRITE_ROUTES`/`_READ_ROLE_ROUTES`/`_ADMIN_ROUTES`) that must
# have an entry for it; a route disappearing is a client command that will start
# 404-ing. Neither is visible in a diff of the file that registers them.
_REGISTERED_ROUTES = {
    ("POST", "/memory/save"),
    ("POST", "/memory/retrospective"),
    ("POST", "/memory/supersede"),
    ("POST", "/memory/review_hold"),
    ("POST", "/memory/search"),
    ("POST", "/memory/graph"),
    ("GET",  "/memory/status/{pg_id}"),
    ("GET",  "/memory/telemetry"),
    ("POST", "/admin/backup"),
    ("GET",  "/health"),
    ("GET",  "/pool/status"),
}


def _registered_route_set(app: web.Application) -> set:
    """(METHOD, canonical path) per registered route.

    The catch-all (method `*`) is excluded for the same reason
    set_known_routes() excludes it. HEAD is excluded because aiohttp's
    `add_get` registers a HEAD companion for free — it is not a surface
    anyone declared, so pinning it would make this set a record of aiohttp's
    internals rather than of the gateway's own decisions."""
    out = set()
    for route in app.router.routes():
        if route.method in ("*", "HEAD"):
            continue
        out.add((route.method, route.resource.canonical))
    return out


def test_the_gateway_registers_exactly_this_route_set(monkeypatch):
    g = _fresh_gateway(monkeypatch)
    app = _build_real_gateway_app(g)
    got = _registered_route_set(app)
    assert got == _REGISTERED_ROUTES, (
        f"registered routes are {sorted(got)}, pinned {sorted(_REGISTERED_ROUTES)}. "
        "Adding a route means an auth-table entry and a client that calls it; "
        "removing one means a client command that starts 404-ing. Update this "
        "set deliberately, never to make the test pass."
    )
