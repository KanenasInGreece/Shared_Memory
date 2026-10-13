"""The MCP server and the CLI skill give the same outcome for the same input.

Both are thin clients of one gateway. The same input must reach the gateway as
the same request body, over the same kind of connection, and come back to the
caller as the same JSON text, whichever door was used. Each test here drives
the CLI through its real ``main()`` entry point and the MCP server through its
tool function against one stubbed gateway, then compares what the gateway
received and what the caller was shown. Nothing here contacts a real gateway:
the base URL is on a name that never resolves.
"""
import asyncio
import importlib.util
import json
import os
import socket
import sys
import tempfile
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("fastmcp")

_HERE = os.path.dirname(__file__)
_REPO = os.path.normpath(os.path.join(_HERE, ".."))
_BRIDGE = os.path.join(_REPO, "shared-memory", "scripts", "memory_bridge.py")
_BRIDGE_COPY = os.path.join(_REPO, "shared-memory-skill", "shared-memory", "scripts",
                            "memory_bridge.py")
_VECTOR = os.path.join(_REPO, "mcp", "vector-skill.py")

BASE = "http://gateway.invalid:1"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mb = _load("memory_bridge_parity", _BRIDGE)
vs = _load("vector_skill_parity", _VECTOR)


def _bridges():
    out = []
    for i, path in enumerate((_BRIDGE, _BRIDGE_COPY)):
        out.append(_load(f"memory_bridge_p5_{i}", path))
    return out


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _Gateway:
    """One stub for both clients: records every request, answers by path."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def answer(self, url, body):
        path = url.split(BASE, 1)[1]
        self.calls.append((path, body))
        status, reply = self.routes.get(path, self.routes.get("*", (200, {"status": "success"})))
        if isinstance(reply, Exception):
            raise reply
        return _Resp(status, reply)


@pytest.fixture
def gateway(monkeypatch):
    holder = {}

    def install(routes):
        gw = _Gateway(routes)

        async def a_post(self, url, **kw):
            return gw.answer(url, kw.get("json"))

        async def a_get(self, url, **kw):
            return gw.answer(url, None)

        def s_post(self, url, **kw):
            return gw.answer(url, kw.get("json"))

        def s_get(self, url, **kw):
            return gw.answer(url, None)

        monkeypatch.setattr(httpx.AsyncClient, "post", a_post)
        monkeypatch.setattr(httpx.AsyncClient, "get", a_get)
        monkeypatch.setattr(httpx.Client, "post", s_post)
        monkeypatch.setattr(httpx.Client, "get", s_get)
        holder["gw"] = gw
        return gw

    monkeypatch.setenv("COORDINATOR_UDS", "")
    for mod in (mb, vs):
        monkeypatch.setattr(mod, "COORDINATOR_BASE", BASE)
        monkeypatch.setattr(mod, "AGENT_ID", "shared_agent")
        monkeypatch.setattr(mod, "_CAPABILITY_CACHE", {})
        monkeypatch.setattr(mod, "_CAPACITY_CACHE", None)

    async def _compat_ok():
        return {"compat": "ok"}

    monkeypatch.setattr(mb, "check_gateway_compat", _compat_ok)
    monkeypatch.setattr(vs, "check_gateway_compat", _compat_ok)
    return install


async def _cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["memory_bridge.py", *argv])
    capsys.readouterr()
    try:
        await mb.main()
    except SystemExit:
        pass
    captured = capsys.readouterr()
    _cli.err = captured.err
    return captured.out.strip()


# ── P1: no attribute the skill does not send ────────────────────────────────

@pytest.mark.asyncio
async def test_p1a_save_artifact_adds_no_timestamp_or_model(gateway):
    gw = gateway({})
    await vs.save_artifact("c", json.dumps({"source": "m1", "project": "proj-a"}))
    sent = gw.calls[0][1]["metadata"]
    assert sent == {"source": "m1", "project": "proj-a"}


_META = {"source": "m1", "project": "proj-a", "entities": ["E"], "domain": "ops"}
_META_FLAGS = {"source": "m1", "project": "proj-a"}

_CASES = {
    "save": (
        ["save", "fact text", json.dumps(_META)],
        lambda: vs.save_artifact("fact text", json.dumps(_META)),
    ),
    "save_with_flags": (
        ["save", "fact text", json.dumps(_META_FLAGS), "--supersedes", "9",
         "--domain", "a,b", "--domain", "c",
         "--acknowledge-standing", "5", "still stands"],
        lambda: vs.save_artifact("fact text", json.dumps({
            **_META_FLAGS, "supersedes": 9, "domains": ["a,b", "c"],
            "acknowledge_standing": {"5": "still stands"}})),
    ),
    "save_decision": (
        ["save_decision", "--title", "T", "--decided-by", "D", "--project", "pj",
         "--rationale", "R", "--source", "src", "--assisted-by", "a, b",
         "--alternatives", "x, y", "--alternatives", "z", "--confidence", "high",
         "--grounded-in", "601:based_on,602", "--elicited", "--new-project",
         "--distinct-from", "p1, p2", "--domain", "ops", "--domain", "dev",
         "--new-domain"],
        lambda: vs.save_decision(
            title="T", decided_by="D", project="pj", rationale="R", source="src",
            assisted_by="a, b", alternatives=["x, y", "z"], confidence="high",
            grounded_in="601:based_on,602", elicited=True, new_project=True,
            confirm_distinct_from="p1, p2", domain=["ops", "dev"], new_domain=True),
    ),
    "save_decision_minimal": (
        ["save_decision", "--title", "T", "--decided-by", "D", "--project", "pj",
         "--rationale", "R", "--source", ""],
        lambda: vs.save_decision(title="T", decided_by="D", project="pj",
                                 rationale="R", source=""),
    ),
    "save_decision_one_domain_name": (
        ["save_decision", "--title", "T", "--decided-by", "D", "--project", "pj",
         "--rationale", "R", "--source", "s", "--domain", "ops,dev"],
        lambda: vs.save_decision(title="T", decided_by="D", project="pj",
                                 rationale="R", source="s", domain="ops,dev"),
    ),
    "save_retrospective": (
        ["save_retrospective", "--pg-id", "5", "--rating", " Validated ", "--notes", "N",
         "--date", "2026-01-02", "--source", "src", "--grounded-in", "7:based_on,8",
         "--elicited"],
        lambda: vs.save_retrospective(
            pg_id=5, rating=" Validated ", notes="N", source="src", date="2026-01-02",
            grounded_in="7:based_on,8", elicited=True),
    ),
    "supersede": (
        ["supersede", "--pg-id", "7", "--by", "8",
         "--acknowledge-standing", "5", "  still stands  "],
        lambda: vs.supersede(7, 8, {"5": "  still stands  "}),
    ),
    "save_no_source": (
        ["save", "fact text", json.dumps({"project": "proj-a", "model": "qwen3"})],
        lambda: vs.save_artifact("fact text", json.dumps({"project": "proj-a", "model": "qwen3"})),
    ),
    "supersede_negative_by": (
        ["supersede", "--pg-id", "7", "--by=-1"],
        lambda: vs.supersede(7, -1),
    ),
    "supersede_no_successor": (
        ["supersede", "--pg-id", "7"],
        lambda: vs.supersede(7),
    ),
    "review_hold": (
        ["review-hold", "--summary-id", "3", "--pg-id", "5"],
        lambda: vs.review_hold(3, 5),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_CASES))
async def test_p1b_the_five_write_tools_post_the_body_the_cli_posts(
        case, gateway, monkeypatch, capsys):
    """Differences left on purpose: none of the bodies above. (Not compared: the
    CLI derives a project from the working directory when none is given, and
    MCP requires one instead; the CLI's retrospective can carry --source-ref,
    which the MCP tool has no argument for. Neither is an input here.)"""
    argv, call = _CASES[case]
    gw = gateway({})
    await _cli(monkeypatch, capsys, *argv)
    cli_calls = list(gw.calls)
    gw.calls.clear()
    await call()
    assert cli_calls, "the CLI posted nothing"
    assert gw.calls == cli_calls


# ── P4: the same reply as the skill ─────────────────────────────────────────

_DOMAIN_UNKNOWN = {
    "status": "error", "error": "domain_unknown",
    "message": "domain 'opz' is not registered for project pj",
    "proposals": [{"name": "ops", "score": 0.9}],
}
_LOSES_GROUND = {
    "status": "error", "error": "decision_loses_last_ground",
    "message": "superseding fact 7 leaves decision 2522 without a ground",
    "decisions": [{"ref": "decision:2522", "pg_id": 2522, "title": "T",
                   "rationale": "why"}],
    "decisions_needing_answer": [2522],
}
_REPLIES = {
    "success": (200, {"status": "success", "pg_id": 34, "neo4j": "pending",
                      "message": "stored", "project_resolved": "pj",
                      "target_pg_id": 5}),
    "gateway_error_body": (200, {"status": "error", "message": "fact 7 is already superseded"}),
    "domain_unknown": (400, _DOMAIN_UNKNOWN),
    "loses_last_ground": (409, _LOSES_GROUND),
    "not_visible": (403, {"status": "error", "error": "decision_not_visible",
                          "message": "decision 2522 is not readable"}),
    "plain_text_500": (500, "boom"),
    "unreachable": (200, httpx.ConnectError("refused")),
    "timeout": (200, httpx.ReadTimeout("")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", sorted(_REPLIES))
@pytest.mark.parametrize("case", ["save", "save_decision", "save_retrospective",
                                  "supersede", "review_hold"])
async def test_p4a_write_tools_return_the_object_the_cli_prints(
        case, reply, gateway, monkeypatch, capsys):
    argv, call = _CASES[case]
    gateway({"*": _REPLIES[reply]})
    printed = await _cli(monkeypatch, capsys, *argv)
    returned = await call()
    assert isinstance(returned, str)
    assert returned == printed
    assert isinstance(json.loads(returned), dict)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["success", "domain_unknown", "unreachable"])
async def test_p4b_ranked_search_returns_the_cli_record_list(
        reply, gateway, monkeypatch, capsys):
    rows = [{"pg_id": 1, "ref": "fact:1", "record_type": "fact", "content": "a",
             "score": 0.9, "ranked": True, "graph_context": "X -> Y",
             "lifecycle": {"state": "current"}, "obsolete": None,
             "stale_sources": [{"ref": "fact:9"}]}]
    routes = {"success": (200, {"results": rows}),
              "domain_unknown": _REPLIES["domain_unknown"],
              "unreachable": _REPLIES["unreachable"]}
    gateway({"*": routes[reply]})
    printed = await _cli(monkeypatch, capsys, "search", "the query", "3",
                         "--project", "pj", "--domain", "ops", "--since", "2026-01-01")
    returned = await vs.hybrid_search_and_rerank(
        "the query", 3, project="pj", domains=["ops"], since="2026-01-01")
    assert returned == printed
    if reply == "success":
        assert json.loads(returned) == rows


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["success", "refs_invalid", "unreachable"])
async def test_p4b_search_by_ref_returns_the_cli_record_list(
        reply, gateway, monkeypatch, capsys):
    rows = [{"ref": "fact:1", "by_ref": True, "content": "whole", "obsolete": "superseded",
             "superseded_by": "fact:9"}]
    routes = {"success": (200, {"results": rows}),
              "refs_invalid": (400, {"error": "refs_invalid", "invalid": ["widget:1"]}),
              "unreachable": _REPLIES["unreachable"]}
    gw = gateway({"*": routes[reply]})
    printed = await _cli(monkeypatch, capsys, "search", "--ref", "fact:1", "--ref", "decision:2")
    cli_calls = list(gw.calls)
    gw.calls.clear()
    returned = await vs.hybrid_search_and_rerank(refs=["fact:1", "decision:2"])
    assert returned == printed
    assert gw.calls == cli_calls
    assert gw.calls[0][1] == {"refs": ["fact:1", "decision:2"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["success", "not_visible", "unreachable"])
async def test_p4c_lineage_graph_and_telemetry_return_the_cli_object(
        reply, gateway, monkeypatch, capsys):
    lineage = {"pg_id": 5, "exists": True, "related": [{"ref": "fact:6", "snippet": "s"}]}
    graph = {"records": [{"n": {"ref": "fact:1"}}]}
    telemetry = {"status": "success", "telemetry": {"postgres": {"technical_docs": 3}}}
    for_reply = {
        "success": {"/memory/status/fact:5": (200, lineage),
                    "/memory/graph": (200, graph),
                    "/memory/telemetry": (200, telemetry)},
        "not_visible": {"*": _REPLIES["not_visible"]},
        "unreachable": {"*": _REPLIES["unreachable"]},
    }[reply]
    gateway(for_reply)

    printed = await _cli(monkeypatch, capsys, "lineage", "fact:5")
    # The CLI's lineage prints a transport failure on one line, without the
    # indent every other reply gets; the document is the same, so compare it.
    assert json.loads(await vs.record_lineage("fact:5")) == json.loads(printed)

    printed = await _cli(monkeypatch, capsys, "graph", "MATCH (n) RETURN n")
    assert await vs.graph_query("MATCH (n) RETURN n") == printed

    expected = json.dumps(mb.get_telemetry(), indent=2)
    assert await vs.memory_telemetry() == expected
    if reply == "success":
        assert json.loads(expected) == telemetry


@pytest.mark.asyncio
async def test_p4c_health_refusal_is_the_cli_error_object(gateway):
    gateway({"*": _REPLIES["not_visible"]})
    with pytest.raises(mb.GatewayReplyError) as caught:
        mb._reply_json(_Resp(403, _REPLIES["not_visible"][1]))
    assert json.loads(await vs.check_memory_health()) == caught.value.payload


# ── P2: the same connection as the skill ────────────────────────────────────

_TOOL_CALLS = {
    "hybrid_search_and_rerank": lambda: vs.hybrid_search_and_rerank("q"),
    "hybrid_search_by_ref": lambda: vs.hybrid_search_and_rerank(refs=["fact:1"]),
    "save_artifact": lambda: vs.save_artifact("c", json.dumps({"source": "s", "project": "p"})),
    "save_decision": lambda: vs.save_decision(title="T", decided_by="D", project="p",
                                              rationale="R", source="s"),
    "save_retrospective": lambda: vs.save_retrospective(pg_id=1, rating="validated",
                                                        notes="n", source="s"),
    "supersede": lambda: vs.supersede(1),
    "review_hold": lambda: vs.review_hold(1, 2),
    "check_memory_health": lambda: vs.check_memory_health(),
    "memory_telemetry": lambda: vs.memory_telemetry(),
    "record_lineage": lambda: vs.record_lineage("fact:1"),
    "graph_query": lambda: vs.graph_query("MATCH (n) RETURN n"),
}


def _routing_handler(seen):
    def handler(request):
        seen.append(str(request.url))
        path = request.url.path
        if path == "/memory/search":
            return httpx.Response(200, json={"results": []})
        if path == "/memory/graph":
            return httpx.Response(200, json={"records": []})
        return httpx.Response(200, json={"status": "success"})
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(_TOOL_CALLS))
async def test_p2a_with_a_socket_configured_every_gateway_call_uses_it(
        tool, monkeypatch, tmp_path):
    sock = str(tmp_path / "gw.sock")
    monkeypatch.setenv("COORDINATOR_UDS", sock)
    monkeypatch.setattr(vs, "COORDINATOR_BASE", BASE)
    monkeypatch.setattr(vs, "_CAPABILITY_CACHE", None)
    monkeypatch.setattr(vs, "_CAPACITY_CACHE", None)
    seen, built = [], []

    def fake_transport(**kw):
        built.append(kw)
        return httpx.MockTransport(_routing_handler(seen))

    monkeypatch.setattr(vs.httpx, "AsyncHTTPTransport", fake_transport)
    out = await _TOOL_CALLS[tool]()
    assert json.loads(out) is not None
    assert seen, f"{tool} made no call through the socket transport"
    assert all(kw["uds"] == sock for kw in built)
    # The health probe that sizes a search goes the same way.
    if tool.startswith("hybrid_search"):
        assert any(u.endswith("/health") for u in seen)


@pytest.mark.parametrize("env", ["empty", "absent_default"])
def test_p2b_falls_back_to_tcp(env, monkeypatch, tmp_path):
    if env == "empty":
        monkeypatch.setenv("COORDINATOR_UDS", "")
        # A socket sitting at the default place does not matter once the value is empty.
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        (tmp_path / "shared-memory-gw.sock").write_text("")
    else:
        monkeypatch.delenv("COORDINATOR_UDS", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert vs._uds_path() is None
    assert mb._uds_path() is None

    def refuse(**kw):
        raise AssertionError("a socket transport was built for a TCP call")

    monkeypatch.setattr(vs.httpx, "AsyncHTTPTransport", refuse)
    client = vs._async_client(5.0)
    assert isinstance(client, httpx.AsyncClient)


def _uds_modules():
    return [vs] + _bridges()


@pytest.fixture
def short_dir():
    d = tempfile.mkdtemp(prefix="smp-")
    yield d
    for name in os.listdir(d):
        os.unlink(os.path.join(d, name))
    os.rmdir(d)


@pytest.mark.parametrize("mod", _uds_modules(), ids=["mcp", "cli", "cli_copy"])
def test_p2b_the_default_socket_is_used_only_as_a_socket_this_user_owns(
        mod, monkeypatch, short_dir):
    monkeypatch.delenv("COORDINATOR_UDS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", short_dir)
    path = os.path.join(short_dir, "shared-memory-gw.sock")
    assert mod._uds_path() is None                      # nothing there

    open(path, "w").close()                              # a plain file is not a socket
    assert mod._uds_path() is None
    os.unlink(path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    try:
        assert mod._uds_path() == path                   # a socket of ours is used

        real_stat = os.stat

        def other_owner(p, *a, **kw):
            st = real_stat(p, *a, **kw)
            if p == path:
                return SimpleNamespace(st_mode=st.st_mode, st_uid=os.getuid() + 1)
            return st

        monkeypatch.setattr(os, "stat", other_owner)
        assert mod._uds_path() is None                   # a socket of another user is not
        monkeypatch.undo()
    finally:
        srv.close()


@pytest.mark.parametrize("mod", _uds_modules(), ids=["mcp", "cli", "cli_copy"])
def test_p2b_an_explicit_socket_is_used_as_given(mod, monkeypatch, short_dir):
    path = os.path.join(short_dir, "named.sock")
    open(path, "w").close()          # not even a socket: an explicit value is the operator's word
    monkeypatch.setenv("COORDINATOR_UDS", path)
    assert mod._uds_path() == path


# ── An unusable socket is named, and never silently replaced by TCP ─────────

@pytest.mark.asyncio
async def test_a_dead_socket_error_names_the_socket_and_the_way_out(
        monkeypatch, capsys, short_dir):
    path = os.path.join(short_dir, "stale.sock")      # never created: connect fails
    monkeypatch.setenv("COORDINATOR_UDS", path)
    for mod in (mb, vs):
        monkeypatch.setattr(mod, "COORDINATOR_BASE", BASE)
        monkeypatch.setattr(mod, "AGENT_ID", "shared_agent")
        monkeypatch.setattr(mod, "_CAPABILITY_CACHE", {})
        monkeypatch.setattr(mod, "_CAPACITY_CACHE", None)

        async def _ok():
            return {"compat": "ok"}

        monkeypatch.setattr(mod, "check_gateway_compat", _ok)
    arg = json.dumps({"source": "s", "project": "pj"})
    printed = json.loads(await _cli(monkeypatch, capsys, "save", "c", arg))
    returned = json.loads(await vs.save_artifact("c", arg))
    assert returned == printed
    assert path in printed["message"]
    assert "COORDINATOR_UDS= (empty)" in printed["message"]
    assert BASE in printed["message"]                   # the TCP target is named only as the way out
    assert "is hive_mind_proxy.py running" not in printed["message"]


@pytest.mark.parametrize("mod", _uds_modules(), ids=["mcp", "cli", "cli_copy"])
def test_no_socket_means_the_old_unreachable_wording(mod, monkeypatch):
    monkeypatch.setenv("COORDINATOR_UDS", "")
    message = mod._coordinator_unavailable(httpx.ConnectError("refused"))["message"]
    assert "is hive_mind_proxy.py running" in message
    assert "COORDINATOR_UDS" not in message


# ── The warnings a CLI agent sees on stderr reach the MCP model in the reply ─

@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["fallback_empty", "unranked", "both"])
async def test_a_search_with_a_note_returns_the_warnings_with_the_results(
        shape, gateway, monkeypatch, capsys):
    rows = {"fallback_empty": [],
            "unranked": [{"pg_id": 1, "ref": "fact:1", "content": "a", "ranked": False}],
            "both": [{"pg_id": 1, "ref": "fact:1", "content": "a", "ranked": False}]}[shape]
    body = {"status": "success", "results": rows}
    if shape != "unranked":
        body["fallback"] = "keyword"
    gateway({"*": (200, body)})
    await _cli(monkeypatch, capsys, "search", "q")
    cli_stderr = [line for line in _cli.err.splitlines() if line]
    result = json.loads(await vs.hybrid_search_and_rerank("q"))
    assert cli_stderr, "the CLI printed no note for this reply"
    assert result == {"warnings": cli_stderr, "results": rows}


@pytest.mark.asyncio
async def test_a_search_without_a_note_returns_the_bare_list(gateway):
    rows = [{"pg_id": 1, "ref": "fact:1", "content": "a", "ranked": True}]
    gateway({"*": (200, {"results": rows})})
    assert json.loads(await vs.hybrid_search_and_rerank("q")) == rows


# ── The one allowed difference: the 401 wording ─────────────────────────────

@pytest.mark.asyncio
async def test_a_401_is_the_one_reply_whose_wording_differs(gateway, monkeypatch, capsys):
    gateway({"*": (401, "Authorization required")})
    for mod in (mb, vs):
        monkeypatch.setattr(mod, "_AGENT_TOKEN_FROM_FILE", "")
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    arg = json.dumps({"source": "s", "project": "pj"})
    cli = json.loads(await _cli(monkeypatch, capsys, "save", "c", arg))
    mcp = json.loads(await vs.save_artifact("c", arg))
    assert cli["status"] == mcp["status"] == "error"
    assert cli["message"].startswith("No AGENT_TOKEN was sent and this gateway requires")
    assert "this agent's .env" in cli["message"]
    assert mcp["message"].startswith("No AGENT_TOKEN was sent and the gateway requires")
    assert "VECTOR_SKILL_ENV" in mcp["message"]
    assert cli != mcp


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(_TOOL_CALLS))
@pytest.mark.parametrize("uds", ["", "socket"])
async def test_p2c_no_client_is_built_with_trust_env_enabled(
        tool, uds, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_UDS", str(tmp_path / "gw.sock") if uds else "")
    monkeypatch.setattr(vs, "COORDINATOR_BASE", BASE)
    monkeypatch.setattr(vs, "_CAPABILITY_CACHE", None)
    monkeypatch.setattr(vs, "_CAPACITY_CACHE", None)
    clients, transports = [], []
    real_client, real_transport = httpx.AsyncClient, httpx.AsyncHTTPTransport

    def client_factory(*a, **kw):
        clients.append(kw)
        kw = {**kw, "transport": httpx.MockTransport(_routing_handler([]))}
        return real_client(*a, **kw)

    def transport_factory(*a, **kw):
        transports.append(kw)
        return real_transport(*a, **kw)

    monkeypatch.setattr(vs.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(vs.httpx, "AsyncHTTPTransport", transport_factory)
    await _TOOL_CALLS[tool]()
    assert clients, f"{tool} built no client"
    assert all(kw.get("trust_env") is False for kw in clients)
    assert all(kw.get("trust_env") is False for kw in transports)
    assert bool(transports) is bool(uds)


# ── P5: both doors pass on the refusal code ─────────────────────────────────

_REFUSAL = {
    "status": "error", "error": "domain_unknown",
    "message": "domain 'opz' is not registered for project pj",
    "proposals": [{"name": "ops", "score": 0.9}],
    "decisions_needing_answer": [2522],
}


@pytest.mark.parametrize("bridge", _bridges(), ids=["scripts", "skill_copy"])
@pytest.mark.parametrize("status", [400, 403, 409, 422])
def test_p5a_cli_error_object_carries_the_gateway_code(bridge, status):
    body = {**_REFUSAL}
    if status == 409:
        body["error"] = "some_conflict"
    with pytest.raises(bridge.GatewayReplyError) as caught:
        bridge._reply_json(_Resp(status, body))
    payload = caught.value.payload
    assert payload["error"] == body["error"]
    assert payload["status"] == "error"
    assert str(status) in payload["message"]


@pytest.mark.parametrize("bridge", _bridges(), ids=["scripts", "skill_copy"])
def test_p5c_cli_keeps_the_other_machine_readable_fields(bridge):
    with pytest.raises(bridge.GatewayReplyError) as caught:
        bridge._reply_json(_Resp(400, _REFUSAL))
    payload = caught.value.payload
    assert payload["proposals"] == _REFUSAL["proposals"]
    assert payload["decisions_needing_answer"] == [2522]


@pytest.mark.parametrize("bridge", _bridges(), ids=["scripts", "skill_copy"])
def test_p5_cli_adds_no_code_when_the_gateway_sent_a_sentence_or_none(bridge):
    for body in ({"error": "something went wrong here"}, {"message": "plain"}, "not json"):
        with pytest.raises(bridge.GatewayReplyError) as caught:
            bridge._reply_json(_Resp(400, body))
        assert "error" not in caught.value.payload


@pytest.mark.asyncio
async def test_p5a_through_the_cli_save_entry_point(gateway, monkeypatch, capsys):
    gateway({"*": (400, _REFUSAL)})
    printed = json.loads(await _cli(
        monkeypatch, capsys, "save", "c", json.dumps({"source": "s", "project": "pj"})))
    assert printed["error"] == "domain_unknown"
    assert printed["proposals"] == _REFUSAL["proposals"]


@pytest.mark.asyncio
async def test_p5b_through_the_mcp_save_tool(gateway):
    gateway({"*": (400, _REFUSAL)})
    result = json.loads(await vs.save_artifact(
        "c", json.dumps({"source": "s", "project": "pj"})))
    assert result["error"] == "domain_unknown"
    assert result["status"] == "error"
    assert "opz" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["save_decision", "save_retrospective", "supersede",
                                  "review_hold", "search"])
async def test_p5c_through_every_mcp_door_the_other_fields_survive(call, gateway):
    gateway({"*": (400, _REFUSAL)})
    if call == "search":
        out = await vs.hybrid_search_and_rerank("q")
    else:
        out = await _TOOL_CALLS[call]()
    result = json.loads(out)
    assert result["error"] == "domain_unknown"
    assert result["proposals"] == _REFUSAL["proposals"]
    assert result["decisions_needing_answer"] == [2522]


def test_p5d_the_two_memory_bridge_copies_are_identical():
    with open(_BRIDGE, "rb") as a, open(_BRIDGE_COPY, "rb") as b:
        assert a.read() == b.read()


def test_the_two_default_agent_ids_are_the_documented_ones(monkeypatch):
    monkeypatch.delenv("AGENT_ID", raising=False)
    assert _load("memory_bridge_default_id", _BRIDGE).AGENT_ID == "memory_bridge"
    assert _load("vector_skill_default_id", _VECTOR).AGENT_ID == "vector_skill"


@pytest.mark.asyncio
async def test_check_memory_health_on_success_returns_health_with_role_and_client(gateway):
    health = {"status": "ok", "version": "1.0.14", "api_version": 4,
              "agent": "a1", "dependencies": {"postgres": {"state": "ok"}},
              "warnings": []}
    gateway({"/health": (200, health)})
    result = json.loads(await vs.check_memory_health())
    assert result["role"] == "not reported (token not accepted — anonymous payload)"
    assert result == {**health, "role": result["role"],
                      "client": {"tool": "vector-skill", "version": vs.VERSION,
                                 "api_version": vs.API_VERSION}}


@pytest.mark.asyncio
async def test_search_and_telemetry_wait_as_long_as_the_cli_does(gateway, monkeypatch):
    gateway({"*": (200, {"results": []})})
    seen = []
    real = vs._async_client
    monkeypatch.setattr(vs, "_async_client", lambda t: (seen.append(t), real(t))[1])
    await vs.hybrid_search_and_rerank("q")
    await vs.memory_telemetry()
    ceiling = mb.search_ceiling(None, None)
    assert seen[0] == ceiling          # one value for connect and read, as the CLI
    assert seen[1] == 15.0             # the CLI's telemetry wait
