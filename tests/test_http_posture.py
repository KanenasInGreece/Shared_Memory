"""POSTURE round (`decision:2113` on `fact:2109`) — every httpx client the
framework constructs passes `trust_env=False`, the gateway's one aiohttp
session states the same explicitly, and no escape-hatch keyword or aliased
import can slip either posture back in unnoticed.

Two kinds of test, per surface:

  T-2  CENSUS (AST, never a string grep) — the next unmarked constructor,
       the next escape-hatch keyword, or the next aliased `httpx` import
       becomes a red test here, without anyone having to remember to update
       this file. Five assertions, one test function each so the mutation
       table below can name exactly which one a given mutation kills.

  T-1  VALUE tests — class-patch the client class itself (never a bound
       method), then drive the REAL call sites (the CLI's `search` action,
       the MCP server's `_search_payload`, the daemon's `pool_has_free_slot`,
       the coordinator's `handle_search`, the gateway's `start_session`) and
       read `trust_env` off the constructor's own `call_args`. This is what
       proves the posture is not just present in the source text the census
       reads, but reaches the object that would actually make the request.

  ENV  One end-to-end test with two real local TCP listeners standing in for
       a proxy and the gateway: with `trust_env=False` a proxy variable in
       the environment is provably never consulted (0 proxy hits, 1 gateway
       hit); a `trust_env=True` control under the SAME environment IS routed
       through the "proxy" (1 proxy hit) and fails once it gets there — this
       is the behaviour the whole round exists to remove.

Mutation table (measured in a scratchpad copy, never in this checkout,
`fact:1244`) is reproduced test-by-test in each docstring below; the summary:

| mutation | must kill | must NOT kill |
|---|---|---|
| remove `trust_env=False` from `memory_bridge.py:369` (sync TCP) | `test_census_no_escape_hatch_and_literal_trust_env`, `test_cli_sync_client_tcp_trust_env_false`, `test_environment_proxy_variables_ignored_with_trust_env_false` | aiohttp/MCP/coordinator value tests; `test_census_total_constructor_count_is_29` |
| remove it from `memory_bridge.py:362` (async TCP) | `test_census_no_escape_hatch_and_literal_trust_env`, `test_cli_async_client_tcp_trust_env_false`, `test_cli_real_search_action_trust_env_false` | the environment leg; every other value test |
| remove it from `hive_mind_proxy.py:1845`'s `ClientSession(...)` | `test_census_aiohttp_clientsession_trust_env`, `test_aiohttp_start_session_trust_env_false` | every httpx test |
| add `proxy="http://127.0.0.1:1"` beside an existing `trust_env=False` | `test_census_no_escape_hatch_and_literal_trust_env` only | everything else |
| add `httpx.get("http://127.0.0.1:1/")` as an unreachable statement inside `pool_has_free_slot` (after its final `return True`) | `test_census_no_bare_verb_calls` only | all value tests, `test_census_total_constructor_count_is_29` |

The census COUNT assertion and its PER-CALL keyword assertion are different
guards — a keyword removal never changes how many constructor calls exist,
so they are deliberately two separate test functions, not one.
"""
import ast
import importlib.util
import os
import socket
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "shared-memory", "scripts")

sys.path.insert(0, SCRIPTS_DIR)

import memory_bridge  # noqa: E402
import pool_status  # noqa: E402
import hive_mind_proxy as g  # noqa: E402


# ── dynamic loads (the suite's established idioms) ───────────────────────────

def load_vector_skill():
    """Exactly `tests/test_vector_skill.py:12-19`'s form — a fresh load also
    gives fresh `_CAPABILITY_CACHE`/`_CAPACITY_CACHE` module globals."""
    path = os.path.join(REPO_ROOT, "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vector_skill"] = module
    spec.loader.exec_module(module)
    return module


vector_skill = load_vector_skill()


def load_coordinator():
    """Exactly `tests/test_rerank_contract.py`'s form."""
    path = os.path.join(SCRIPTS_DIR, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator


# ══════════════════════════════════════════════════════════════════════════
# T-2 — CENSUS (AST)
# ══════════════════════════════════════════════════════════════════════════

WALKED_ROOTS = [
    SCRIPTS_DIR,
    os.path.join(REPO_ROOT, "mcp"),
    os.path.join(REPO_ROOT, "shared-memory-skill", "shared-memory", "scripts"),
]

_BARE_VERBS = ("get", "post", "put", "patch", "delete", "head", "options",
               "stream", "request")

# Measured directly at this base (both `memory_bridge.py` copies count
# separately — they are byte-identical files, not one file counted twice):
# shared-memory/scripts/{coordinator,consolidation_loop,memory_bridge,
# migrate_retro_edges,pool_status,rem_loop}.py, mcp/vector-skill.py, and
# shared-memory-skill/shared-memory/scripts/memory_bridge.py — EIGHT files.
# The brief's fold text states this walked-tree import count as 7; a fresh
# AST count at v0.9.93 (`fact:2106`'s anchor) gives 8, so this test pins the
# MEASURED value on both sides of the equality (`fact:1309`), not the
# brief's stated one — a new httpx importer updates this set in the same PR.
_EXPECTED_HTTPX_IMPORTERS = frozenset({
    "shared-memory/scripts/coordinator.py",
    "shared-memory/scripts/consolidation_loop.py",
    "shared-memory/scripts/memory_bridge.py",
    "shared-memory/scripts/migrate_retro_edges.py",
    "shared-memory/scripts/pool_status.py",
    "shared-memory/scripts/rem_loop.py",
    "mcp/vector-skill.py",
    "shared-memory-skill/shared-memory/scripts/memory_bridge.py",
})


def _walked_py_files():
    files = []
    for root in WALKED_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__")]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(os.path.join(dirpath, fn))
    return files


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _httpx_constructor_calls():
    """[(path, lineno, attr, ast.Call node)] for every `httpx.Client` /
    `httpx.AsyncClient` call in the walked trees."""
    calls = []
    for path in _walked_py_files():
        for node in ast.walk(_parse(path)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "httpx"
                    and node.func.attr in ("Client", "AsyncClient")):
                calls.append((path, node.lineno, node.func.attr, node))
    return calls


def test_census_total_constructor_count_is_29():
    """The 29 sites `fact:2106`/`decision:2113` measured (12 in
    `coordinator.py`+daemons+CLI, 11 in `vector-skill.py`, 4 in the two
    `memory_bridge.py` copies + the 2 tooling scripts — see the brief's
    table) are the ONLY `httpx.Client`/`httpx.AsyncClient` constructions in
    the walked trees. A LITERAL 29 (`fact:1309`): a 30th unmarked
    constructor is a new httpx client the framework grew without this rule.

    Mutation: removing a `trust_env=False` keyword from an existing call
    never changes this count — that failure belongs to
    `test_census_no_escape_hatch_and_literal_trust_env` instead. Adding the
    `pool_has_free_slot` bare-verb mutation also does not change this count
    (a bare `httpx.get(...)` is not a `Client`/`AsyncClient` construction) —
    that failure belongs to `test_census_no_bare_verb_calls`.
    """
    calls = _httpx_constructor_calls()
    assert len(calls) == 29, (
        f"expected exactly 29 httpx.Client/AsyncClient constructor calls "
        f"under {WALKED_ROOTS}, found {len(calls)}: "
        f"{[(os.path.relpath(p, REPO_ROOT), ln) for p, ln, _, _ in calls]}"
    )


def test_census_no_bare_verb_calls():
    """No walked file may call `httpx.get/post/put/patch/delete/head/options/
    stream/request(...)` directly — a bare module-level verb call builds its
    own transient client with httpx's library default (`trust_env=True`),
    which the census over constructor calls would never see.

    Mutation: adding `httpx.get("http://127.0.0.1:1/")` as an unreachable
    statement inside `pool_has_free_slot` (after its own `return True`, never
    at module level — the daemons import this module) kills ONLY this test;
    the constructor count in `test_census_total_constructor_count_is_29`
    is unaffected because a bare verb call is not a `Client`/`AsyncClient`
    construction.
    """
    offenders = []
    for path in _walked_py_files():
        for node in ast.walk(_parse(path)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "httpx"
                    and node.func.attr in _BARE_VERBS):
                offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{node.lineno}: "
                                  f"httpx.{node.func.attr}(...)")
    assert offenders == [], f"bare httpx verb call(s) bypass the census: {offenders}"


def test_census_no_escape_hatch_and_literal_trust_env():
    """Assertion 2: keyword PRESENCE alone does not enforce the ruling —
    `trust_env=False, proxy=os.environ.get(...)` would pass a
    keyword-presence-only check while still reading the environment through
    `proxy=`. Every one of the 29 calls must carry `trust_env` as a LITERAL
    `ast.Constant(False)` (never a `Name`/`Call`/`Subscript`, which could
    vary at runtime) and must carry NONE of `proxy`, `proxies`, `mounts`,
    `verify`, `cert` as a keyword.

    Mutation: adding `proxy="http://127.0.0.1:1"` beside an existing
    `trust_env=False` kills ONLY this test — the count and bare-verb
    assertions above are untouched, and so is every value test (the escape
    hatch keyword is inert unless something actually calls out to it).
    Also kills on: removing `trust_env=False` from `memory_bridge.py:369`
    or `:362` (a missing keyword is `trust_kw is None`, caught here too).
    """
    escape_hatch = {"proxy", "proxies", "mounts", "verify", "cert"}
    failures = []
    for path, lineno, attr, node in _httpx_constructor_calls():
        rel = os.path.relpath(path, REPO_ROOT)
        kwnames = {kw.arg for kw in node.keywords if kw.arg}
        trust_kw = next((kw for kw in node.keywords if kw.arg == "trust_env"), None)
        if trust_kw is None:
            failures.append(f"{rel}:{lineno}: {attr}(...) has no trust_env keyword")
        elif not (isinstance(trust_kw.value, ast.Constant) and trust_kw.value.value is False):
            failures.append(
                f"{rel}:{lineno}: trust_env is not literally False "
                f"(got {ast.dump(trust_kw.value)})")
        hatch = kwnames & escape_hatch
        if hatch:
            failures.append(f"{rel}:{lineno}: escape-hatch keyword(s) present: {sorted(hatch)}")
    assert failures == [], "\n".join(failures)


def test_census_import_spelling_and_file_set():
    """Import-spelling guard: every `httpx` import in the walked trees is a
    plain `import httpx` — no `import httpx as h` (which would defeat the
    AST match keyed on the name `httpx` used by every assertion above) and
    no `from httpx import ...` (ditto, and also invisible to a `httpx.X`
    attribute match). Tree guard: the set of files importing httpx repo-wide
    (excluding `tests/` and `.claude/`) equals exactly the walked-tree set —
    no ninth importer hiding outside the three walked roots.

    Mutation: aliasing any of the 8 known imports, or adding a 9th importer
    anywhere in the repo (in or out of the walked trees), kills this test
    without touching the constructor-count or per-call assertions above —
    they only ever look at calls whose `func.value` is literally named
    `httpx`, so an aliased import silently drops that file's calls from
    every other census assertion instead of failing loudly.
    """
    walked_importers = set()
    for path in _walked_py_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "httpx":
                        assert alias.asname is None, (
                            f"{os.path.relpath(path, REPO_ROOT)}: "
                            f"`import httpx as {alias.asname}` defeats the "
                            "AST match on the bare name `httpx`")
                        walked_importers.add(os.path.relpath(path, REPO_ROOT))
            if isinstance(node, ast.ImportFrom) and node.module == "httpx":
                pytest.fail(f"{os.path.relpath(path, REPO_ROOT)}:{node.lineno}: "
                            "`from httpx import ...` defeats the attribute-call AST match")

    assert walked_importers == set(_EXPECTED_HTTPX_IMPORTERS), (
        f"walked-tree httpx importers changed: {sorted(walked_importers)}"
    )

    repo_wide_importers = set()
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        rel_dir = os.path.relpath(dirpath, REPO_ROOT)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        if "tests" in parts or ".claude" in parts or ".git" in parts:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                        if d not in (".git", ".claude", "tests", "__pycache__")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            for node in ast.walk(_parse(path)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "httpx":
                            repo_wide_importers.add(os.path.relpath(path, REPO_ROOT))
                if isinstance(node, ast.ImportFrom) and node.module == "httpx":
                    repo_wide_importers.add(os.path.relpath(path, REPO_ROOT))

    assert repo_wide_importers == set(_EXPECTED_HTTPX_IMPORTERS), (
        f"repo-wide httpx importers (excl. tests/, .claude/) diverge from the "
        f"walked-tree set: {sorted(repo_wide_importers)}"
    )


def test_census_aiohttp_clientsession_trust_env():
    """`hive_mind_proxy.py` imports `ClientSession` bare (`from aiohttp import
    ..., ClientSession, ...`, line 17) — NOT as `aiohttp.ClientSession` — so
    the matching AST shape is `ast.Call(func=ast.Name('ClientSession'))`,
    not an attribute call. Exactly one such call must exist, and it must
    carry `trust_env=Constant(False)`.

    Mutation: removing `trust_env=False` from the `ClientSession(...)` call
    at `hive_mind_proxy.py:1845` kills this test and
    `test_aiohttp_start_session_trust_env_false`; no httpx test is affected
    (aiohttp and httpx are unrelated code paths).
    """
    path = os.path.join(SCRIPTS_DIR, "hive_mind_proxy.py")
    calls = [n for n in ast.walk(_parse(path))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "ClientSession"]
    assert len(calls) == 1, (
        f"expected exactly one bare ClientSession(...) call in hive_mind_proxy.py, "
        f"found {len(calls)} at lines {[c.lineno for c in calls]}"
    )
    kw = {k.arg: k.value for k in calls[0].keywords if k.arg}
    assert "trust_env" in kw, "hive_mind_proxy.py's ClientSession(...) has no trust_env keyword"
    assert isinstance(kw["trust_env"], ast.Constant) and kw["trust_env"].value is False, (
        f"trust_env is not literally False (got {ast.dump(kw['trust_env'])})"
    )


# ══════════════════════════════════════════════════════════════════════════
# T-1 — VALUE tests (class-patch, never method-patch)
# ══════════════════════════════════════════════════════════════════════════

def _force_tcp(monkeypatch, tmp_path):
    """`COORDINATOR_UDS` present-but-empty short-circuits `_uds_path` to
    `None` (`memory_bridge.py`/`.env.example`'s documented form,
    `:350-356`) — never rely on the socket being absent: on the gateway
    host `_uds_path()` returns the LIVE production socket."""
    monkeypatch.setenv("COORDINATOR_UDS", "")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def _force_uds(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_UDS", str(tmp_path / "gw.sock"))


def test_cli_async_client_tcp_trust_env_false(monkeypatch, tmp_path):
    """`memory_bridge._async_client`, TCP branch (`:362`).

    Mutation: removing `trust_env=False` from `memory_bridge.py:362` kills
    this test (and the census per-call assertion, and
    `test_cli_real_search_action_trust_env_false`); it does not touch the
    environment leg (which drives the SYNC client) or any other value test.
    """
    _force_tcp(monkeypatch, tmp_path)
    with patch.object(memory_bridge.httpx, "AsyncClient") as mock_cls:
        memory_bridge._async_client(5.0)
    assert mock_cls.call_args.kwargs["trust_env"] is False
    assert "transport" not in mock_cls.call_args.kwargs


def test_cli_async_client_uds_trust_env_false(monkeypatch, tmp_path):
    """`memory_bridge._async_client`, UDS branch (`:361`) — the socket path
    need not exist; `_uds_path()` returns an explicit `COORDINATOR_UDS`
    verbatim with no existence check."""
    _force_uds(monkeypatch, tmp_path)
    with patch.object(memory_bridge.httpx, "AsyncClient") as mock_cls:
        memory_bridge._async_client(5.0)
    assert mock_cls.call_args.kwargs["trust_env"] is False
    assert "transport" in mock_cls.call_args.kwargs


def test_cli_sync_client_tcp_trust_env_false(monkeypatch, tmp_path):
    """`memory_bridge._sync_client`, TCP branch (`:369`) — the exact branch
    the environment leg below drives through a real socket.

    Mutation: removing `trust_env=False` from `memory_bridge.py:369` kills
    this test, the census per-call assertion, and the environment leg; it
    does not touch the async-TCP test above or any other value test.
    """
    _force_tcp(monkeypatch, tmp_path)
    with patch.object(memory_bridge.httpx, "Client") as mock_cls:
        memory_bridge._sync_client(5.0)
    assert mock_cls.call_args.kwargs["trust_env"] is False
    assert "transport" not in mock_cls.call_args.kwargs


def test_cli_sync_client_uds_trust_env_false(monkeypatch, tmp_path):
    """`memory_bridge._sync_client`, UDS branch (`:368`)."""
    _force_uds(monkeypatch, tmp_path)
    with patch.object(memory_bridge.httpx, "Client") as mock_cls:
        memory_bridge._sync_client(5.0)
    assert mock_cls.call_args.kwargs["trust_env"] is False
    assert "transport" in mock_cls.call_args.kwargs


@pytest.mark.asyncio
async def test_cli_real_search_action_trust_env_false(monkeypatch, tmp_path):
    """Drives `_search_payload` — the function the CLI's `search` action
    (`memory_bridge.py` `main()`, `action == "search"`) calls — under the
    class patch, forcing the TCP arm and pre-filling the capability/capacity
    caches so `_fetch_health_blocks` short-circuits without its own client
    construction (it returns early on a warm cache and constructs nothing),
    leaving exactly one `_async_client` call to inspect: the search POST.

    Mutation: removing `trust_env=False` from `memory_bridge.py:362` (async
    TCP, the branch this real action takes) kills this test and the census
    per-call assertion; the environment leg (sync client) is unaffected.
    """
    _force_tcp(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_bridge, "_CAPABILITY_CACHE", {})
    monkeypatch.setattr(memory_bridge, "_CAPACITY_CACHE", None)
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"results": []}
    with patch.object(memory_bridge.httpx, "AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        await memory_bridge._search_payload("posture probe", 5)
    assert mock_cls.call_args.kwargs["trust_env"] is False


@pytest.mark.asyncio
async def test_mcp_search_payload_trust_env_false():
    """MCP surface: drives `_search_payload` (`mcp/vector-skill.py:889`, its
    httpx site at `:918`). Resets both health-probe caches first — a warm
    cache short-circuits `_fetch_health_blocks` and it constructs nothing,
    same reasoning as the CLI real-action test above."""
    vector_skill._CAPABILITY_CACHE = {}
    vector_skill._CAPACITY_CACHE = None
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"results": []}
    with patch.object(vector_skill.httpx, "AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        await vector_skill._search_payload("posture probe", 5)
    assert mock_cls.call_args.kwargs["trust_env"] is False


@pytest.mark.asyncio
async def test_daemon_pool_has_free_slot_trust_env_false():
    """Daemon surface: `pool_status.pool_has_free_slot()` (`:40`), consumed
    by `rem_loop.py`/`consolidation_loop.py` to gate dream-cycle scheduling."""
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"free_slots": 1}
    with patch.object(pool_status.httpx, "AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await pool_status.pool_has_free_slot()
    assert mock_cls.call_args.kwargs["trust_env"] is False
    assert result is True


# ── coordinator harness (mirrors tests/test_rerank_contract.py:33-105) ──────

class _AsyncCtx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


class _AsyncRows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


def _make_search_request(body: dict) -> MagicMock:
    state = {"authenticated_agent": None, "principal": None}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator_for_search() -> MemoryCoordinator:
    c = MemoryCoordinator()

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_AsyncCtx(None))

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtx(mock_conn))
    c._pool = mock_pool

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncRows())
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_AsyncCtx(mock_session))
    c._neo4j = mock_neo4j

    return c


@pytest.mark.asyncio
async def test_coordinator_handle_search_trust_env_false():
    """Gateway surface: drives `handle_search` (`coordinator.py:8586`, the
    site's owner) with `_embed` patched (`patch.object(c, "_embed", ...)`) —
    never `_embed` itself, which RECEIVES the client (`coordinator.py:3680`)
    rather than constructing one — and `httpx.AsyncClient` class-patched,
    exactly the `tests/test_rerank_contract.py:127-133` idiom.
    """
    c = _coordinator_for_search()
    mock_rerank_response = MagicMock()
    mock_rerank_response.raise_for_status = MagicMock()
    mock_rerank_response.json = MagicMock(return_value={"results": []})
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_rerank_response)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            resp = await c.handle_search(_make_search_request({"query": "posture probe",
                                                                "limit": 5}))
    assert resp.status == 200
    assert mock_cls.call_args.kwargs["trust_env"] is False


@pytest.mark.asyncio
async def test_aiohttp_start_session_trust_env_false():
    """Gateway session surface: `AsyncHiveMindProxy.start_session`
    (`hive_mind_proxy.py:1845`). Both `ClientSession` and `TCPConnector` are
    patched — `TCPConnector` raises `RuntimeError: no running event loop`
    outside a running loop (measured), and patching it here means this test
    creates no real socket resource regardless.

    Mutation: removing `trust_env=False` from the `ClientSession(...)` call
    kills this test and the census aiohttp assertion; no httpx test is
    affected.
    """
    proxy = g.AsyncHiveMindProxy()
    with patch.object(g, "ClientSession") as mock_session_cls, \
            patch.object(g, "TCPConnector") as mock_tcp_cls:
        await proxy.start_session()
    assert mock_session_cls.call_args.kwargs["trust_env"] is False


# ══════════════════════════════════════════════════════════════════════════
# ENVIRONMENT LEG — what the ruling is about
# ══════════════════════════════════════════════════════════════════════════

class _CountingServer:
    """A local TCP listener that counts accepted connections and either
    sends nothing before closing (a "count-only" stand-in for a proxy that
    never actually forwards) or answers one minimal `HTTP/1.1 200 OK` (a
    stand-in gateway). No test in this file reaches a socket or port it did
    not itself create — both listeners bind to `127.0.0.1:0` (OS-assigned
    free port)."""

    def __init__(self, respond: bytes | None = None):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self.hits = 0
        self._lock = threading.Lock()
        self._respond = respond
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                self._sock.settimeout(0.2)
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.hits += 1
            try:
                if self._respond is not None:
                    conn.sendall(self._respond)
                # else: count-only — accept and close with no response, the
                # proxy's role in the treatment/control comparison.
            finally:
                conn.close()

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


_GATEWAY_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

_PROXY_ENV_VARS = ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy",
                    "ALL_PROXY", "all_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")


def test_environment_proxy_variables_ignored_with_trust_env_false(monkeypatch, tmp_path):
    """The behaviour the whole POSTURE round exists to remove: before this
    round, an `HTTP_PROXY`/`ALL_PROXY` visible to the gateway routed every
    save and search through it, silently, loopback included.

    Two real local listeners stand in for a proxy and the gateway. TREATMENT
    drives `memory_bridge._sync_client(5.0)` (the real, unpatched httpx
    client — this leg proves behaviour, not call args) with `HTTP_PROXY`/
    `ALL_PROXY` pointed at the "proxy" listener: `trust_env=False` means the
    client must never consult them, so it must connect DIRECT to the
    "gateway" listener. CONTROL, in the same test and same environment,
    builds a bare `httpx.Client(trust_env=True)` — WITHOUT the framework's
    posture — and that one IS routed through the proxy, which (being a
    count-only listener) closes without answering, raising a
    `httpx.TransportError` client-side.

    Assert counters, never statuses (`fact:1321`): both listeners reading 0
    hits means the harness itself is broken, not that the posture held.

    Mutation: removing `trust_env=False` from `memory_bridge.py:369` (the
    sync-TCP branch this test drives) kills this test — the treatment call
    would then also read `HTTP_PROXY`, so `proxy_hits` would read 1 (or the
    call would raise the same TransportError the control raises) instead of
    the expected 0.
    """
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    _force_tcp(monkeypatch, tmp_path)

    proxy = _CountingServer(respond=None)
    gateway = _CountingServer(respond=_GATEWAY_RESPONSE)
    try:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{proxy.port}")

        # TREATMENT — the framework's own client.
        treated = memory_bridge._sync_client(5.0)
        try:
            treated.get(f"http://127.0.0.1:{gateway.port}/")
        finally:
            treated.close()

        assert proxy.hits == 0, (
            "the framework's sync client consulted HTTP_PROXY/ALL_PROXY "
            "despite trust_env=False"
        )
        assert gateway.hits == 1, "the framework's sync client never reached the gateway"

        # CONTROL — a bare client with the posture this round removes,
        # under the exact same environment.
        import httpx
        control = httpx.Client(trust_env=True)
        try:
            with pytest.raises(httpx.TransportError):
                control.get(f"http://127.0.0.1:{gateway.port}/")
        finally:
            control.close()

        assert proxy.hits == 1, (
            "the trust_env=True control did not route through the proxy — "
            "the harness itself is broken, not proving the posture"
        )
    finally:
        proxy.close()
        gateway.close()
