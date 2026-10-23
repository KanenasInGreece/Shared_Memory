"""Batch F: every client record-id input uses the gateway's id rule (ASCII digits, 1..2**63-1).

`int()` and `isdigit()` alone resolve "+5", "1_6", "-5" or a non-ASCII digit to a real but
wrong row. These tests pin the shared helper, the four grounded-in loops, both acknowledge
normalisers, the six CLI flags and the five MCP parameters.
"""
import asyncio
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastmcp")
from fastmcp import Client  # noqa: E402

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "shared-memory", "scripts"))

import record_ref  # noqa: E402

BRIDGE_PATH = os.path.join(ROOT, "shared-memory", "scripts", "memory_bridge.py")
TRACKED_PATH = os.path.join(ROOT, "shared-memory-skill", "shared-memory", "scripts",
                            "memory_bridge.py")
VECTOR_PATH = os.path.join(ROOT, "mcp", "vector-skill.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = _load("batch_f_bridge", BRIDGE_PATH)
vector = _load("batch_f_vector", VECTOR_PATH)
CLIENTS = [("bridge", bridge), ("vector", vector)]

MAXID = 2**63 - 1
CASES = [
    ("16", 16), (" 16 ", 16), ("016", 16), (16, 16), ("0", None), (0, None),
    ("-5", None), (-5, None), ("+5", None), ("1_6", None), ("٣", None), ("１２", None),
    (str(2**63), None), (2**63, None), (str(MAXID), MAXID), (True, None), (False, None),
    (3.0, None), ("3.0", None), ("", None), (None, None), ("9" * 4301, None),
]
BAD_TOKENS = ["1_6", "-5", "+5", "٣", "１２", "0", str(2**63)]


# ── 1 · the helper ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,mod", CLIENTS)
def test_parse_record_id_table(name, mod):
    assert [mod._parse_record_id(v) for v, _ in CASES] == [e for _, e in CASES]


@pytest.mark.parametrize("name,mod", CLIENTS)
def test_parse_record_id_agrees_with_the_server_on_strings(name, mod):
    for v, _ in CASES:
        if not isinstance(v, str):
            continue
        try:
            server = record_ref._parse_id(v)
        except ValueError:
            server = None
        assert mod._parse_record_id(v) == server, v


# ── 2 · grounded-in, all four loops ──────────────────────────────────────────

GROUNDED = "٣:based_on, 12, １２, 0:informed_by, 1_6"


def _decision_cli():
    _, md = bridge.build_decision_metadata(
        title="t", decided_by="o", project="p", rationale="r", grounded_in=GROUNDED)
    return md


def _retro_cli():
    return bridge.build_retrospective_payload(
        pg_id=1, rating="validated", notes="n", grounded_in=GROUNDED)


def _http(status, body):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body)
    r.text = json.dumps(body)
    return r


async def _mcp_posted(coro_fn, **kwargs):
    with patch("httpx.AsyncClient.post", return_value=_http(200, {"status": "success"})) as post:
        await coro_fn(**kwargs)
    return post.call_args.kwargs["json"]


def _decision_mcp():
    payload = asyncio.run(_mcp_posted(
        vector.save_decision, title="t", decided_by="o", project="p", rationale="r",
        source="s", grounded_in=GROUNDED))
    return payload["metadata"]


def _retro_mcp():
    return asyncio.run(_mcp_posted(
        vector.save_retrospective, pg_id=1, rating="validated", notes="n", source="s",
        grounded_in=GROUNDED))


@pytest.mark.parametrize("build", [_decision_cli, _retro_cli, _decision_mcp, _retro_mcp])
def test_grounded_in_skips_what_the_gateway_would_refuse(build):
    out = build()
    assert out["grounded_in"] == [12]
    assert "grounded_roles" not in out


# ── 3 · acknowledge-standing keys ────────────────────────────────────────────

CLI_ACK_MSG = ("acknowledge_standing form is --acknowledge-standing ID \"operator's words\". "
               "Got {k!r}. There is no id:answer syntax.")
MCP_ACK_MSG = ("acknowledge_standing key {k!r} is not a decision id; the form is "
               "{{decision_id: the operator's words}}.")


@pytest.mark.parametrize("k", ["1_6", "+5", "-5", "٣", "0"])
def test_ack_refuses_with_each_doors_existing_message(k):
    with pytest.raises(ValueError) as cli:
        bridge._normalize_cli_ack([[k, "the operator said so"]])
    assert str(cli.value) == CLI_ACK_MSG.format(k=k)
    with pytest.raises(ValueError) as mcp:
        vector._normalize_ack({k: "the operator said so"})
    assert str(mcp.value) == MCP_ACK_MSG.format(k=k)


def test_ack_valid_key_still_works():
    assert bridge._normalize_cli_ack([["16", "the operator said so"]]) == {
        16: "the operator said so"}
    assert vector._normalize_ack({"16": "the operator said so"}) == {16: "the operator said so"}


# ── 4 · the six CLI flags ────────────────────────────────────────────────────

class _FakeClient:
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **k):
        type(self).calls.append(("post", a, k))
        return _http(200, {"status": "success"})

    async def get(self, *a, **k):
        type(self).calls.append(("get", a, k))
        return _http(200, {"status": "success"})


def _run_main(argv, capsys):
    _FakeClient.calls = []
    with patch.object(sys, "argv", ["memory_bridge.py"] + argv), \
            patch.object(bridge, "_async_client", lambda *a, **k: _FakeClient()), \
            patch.object(bridge.httpx, "AsyncClient", _FakeClient):
        try:
            asyncio.run(bridge.main())
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, capsys.readouterr(), list(_FakeClient.calls)


RETRO_TAIL = ["--rating", "validated", "--notes", "n", "--grounded-in", "5"]
FLAGS = {
    "supersedes": lambda v: ["save", "content", "--supersedes", v],
    "supersede.pg-id": lambda v: ["supersede", "--pg-id", v],
    "supersede.by": lambda v: ["supersede", "--pg-id", "16", "--by", v],
    "review-hold.summary-id": lambda v: ["review-hold", "--summary-id", v, "--pg-id", "16"],
    "review-hold.pg-id": lambda v: ["review-hold", "--summary-id", "16", "--pg-id", v],
    "retro.pg-id": lambda v: ["save_retrospective", "--pg-id", v] + RETRO_TAIL,
}


@pytest.mark.parametrize("flag", sorted(FLAGS))
@pytest.mark.parametrize("bad", ["1_6", "-5", "٣", "0", str(2**63)])
def test_cli_flag_refuses_a_bad_id_before_any_http(flag, bad, capsys):
    code, cap, calls = _run_main(FLAGS[flag](bad), capsys)
    assert code == 2
    assert "invalid record id value" in cap.err
    assert calls == []


@pytest.mark.parametrize("flag", sorted(FLAGS))
def test_cli_flag_accepts_a_valid_id(flag, capsys):
    code, cap, calls = _run_main(FLAGS[flag]("16"), capsys)
    assert code == 0, cap.err
    assert calls, "a valid id should reach the HTTP layer"


# ── 5 · MCP tool parameters, through the real validation path ────────────────

MCP_PARAMS = [
    ("save_retrospective", "pg_id",
     dict(rating="validated", notes="n", source="s", grounded_in="5")),
    ("supersede", "pg_id", {}),
    ("supersede", "by", {"pg_id": 16}),
    ("review_hold", "summary_id", {"pg_id": 16}),
    ("review_hold", "pg_id", {"summary_id": 16}),
]
MCP_BAD = ["1_6", "16", True, 3.0, 0, -5, 2**63]


async def _call(tool, args):
    with patch("httpx.AsyncClient.post", return_value=_http(200, {"status": "success"})) as post, \
            patch("httpx.AsyncClient.get", return_value=_http(200, {"status": "success"})):
        async with Client(vector.mcp) as c:
            try:
                await c.call_tool(tool, args)
                err = None
            except Exception as exc:  # ToolError
                err = exc
    return err, post.call_args_list



@pytest.mark.parametrize("tool,param,extra", MCP_PARAMS)
@pytest.mark.parametrize("bad", MCP_BAD, ids=repr)
def test_mcp_id_parameters_refuse_non_ids(tool, param, extra, bad):
    if tool == "supersede" and param == "by" and bad == 0:
        pytest.skip("by=0 is the omit sentinel; see the next test")
    args = dict(extra)
    args[param] = bad
    if tool == "review_hold":
        args.setdefault("summary_id", 16)
        args.setdefault("pg_id", 16)
    err, posts = asyncio.run(_call(tool, args))
    assert err is not None, (tool, param, bad)
    assert posts == []


@pytest.mark.parametrize("tool,param,extra", MCP_PARAMS)
def test_mcp_integer_id_reaches_the_gateway(tool, param, extra):
    args = dict(extra)
    args[param] = 16
    if tool == "review_hold":
        args.setdefault("summary_id", 16)
        args.setdefault("pg_id", 16)
    err, posts = asyncio.run(_call(tool, args))
    assert err is None, err
    assert len(posts) == 1
    body = posts[0].kwargs["json"]
    assert 16 in body.values()


def test_mcp_supersede_by_zero_still_omits_by():
    err, posts = asyncio.run(_call("supersede", {"pg_id": 16, "by": 0}))
    assert err is None, err
    assert posts[0].kwargs["json"] == {"pg_id": 16}


@pytest.mark.parametrize("tool,param,extra", MCP_PARAMS)
def test_mcp_schema_still_integer(tool, param, extra):
    async def schema():
        async with Client(vector.mcp) as c:
            return {t.name: (getattr(t, 'input_schema', None) or t.inputSchema) for t in await c.list_tools()}
    props = asyncio.run(schema())[tool]["properties"]
    assert props[param]["type"] == "integer"


# ── 6 · structure ────────────────────────────────────────────────────────────

def test_bridge_copies_are_byte_identical():
    with open(BRIDGE_PATH, "rb") as a, open(TRACKED_PATH, "rb") as b:
        assert a.read() == b.read()


@pytest.mark.parametrize("path", [BRIDGE_PATH, VECTOR_PATH])
def test_no_client_imports_a_server_module(path):
    src = open(path, encoding="utf-8").read()
    assert "import record_ref" not in src and "from record_ref" not in src
