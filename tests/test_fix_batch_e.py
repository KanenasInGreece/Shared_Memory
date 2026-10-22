"""Batch E: the clients' lineage ref check mirrors the server grammar (rr-3), and the
clients' domain fold is pinned at outcome parity with the server (da-8)."""
import asyncio
import copy
import importlib.util
import itertools
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastmcp")

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "shared-memory", "scripts"))

import coordinator  # noqa: E402
import domain_axis  # noqa: E402
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


bridge = _load("batch_e_bridge", BRIDGE_PATH)
vector = _load("batch_e_vector", VECTOR_PATH)

CORPUS = [
    "9" * 4301, "fact:" + "9" * 4301,
    "12", " 12 ", "012", "0", "-5", "--5", "+5", "1_6", "٣", "１２", str(2**63),
    str(2**63 - 1), "fact:12", "Fact:12", " Fact:12 ", "fact:012", " fact : 12 ",
    "fact: 12", "fact:-5", "fact:-0", "fact:0", "fact:٣", "summary:87", "insight:3",
    "retro:5", "retrospective:5", "fact:", ":12", "fact:12:3", "",
]
HELPERS = [("bridge", bridge._canonical_ref), ("vector", vector._canonical_ref)]


def _server(x):
    try:
        rtype, pid = record_ref.parse_ref(x)
    except ValueError:
        return None
    return str(pid) if rtype is None else record_ref.make_ref(rtype, pid)


@pytest.mark.parametrize("name,helper", HELPERS)
@pytest.mark.parametrize("x", CORPUS)
def test_canonical_ref_matches_server_grammar(name, helper, x):
    assert helper(x) == _server(x)


@pytest.mark.parametrize("name,helper", HELPERS)
def test_canonical_ref_refuses_negative(name, helper):
    assert helper("-5") is None
    assert helper("fact:-5") is None
    assert helper(" Fact : 012 ") == "fact:12"


USAGE = {"error": (
    "Usage: memory_bridge.py lineage <pg_id|type:id> — type is one of "
    "fact, decision, retrospective, summary, insight")}
MCP_ERR = {"error": (
    "ref must be a bare id or type:id, where type is one of "
    + ", ".join(vector.RECORD_TYPES))}


def _run_cli(ref):
    argv = sys.argv
    sys.argv = ["memory_bridge.py", "lineage", ref]
    resp = MagicMock(status_code=200, json=lambda: {"pg_id": 12, "exists": True})
    code = None
    try:
        with patch("httpx.AsyncClient.get", return_value=resp) as get:
            try:
                asyncio.run(bridge.main())
            except SystemExit as e:
                code = e.code
    finally:
        sys.argv = argv
    return get, code


def test_cli_lineage_sends_canonical_ref():
    get, code = _run_cli(" fact : 12 ")
    assert code is None
    assert get.call_args.args[0].endswith("/memory/status/fact:12")


def test_cli_lineage_refuses_negative_locally(capsys):
    get, code = _run_cli("fact:-5")
    assert code == 1
    assert capsys.readouterr().out == json.dumps(USAGE) + "\n"
    assert not get.called


def test_mcp_lineage_sends_canonical_ref():
    resp = MagicMock(status_code=200, json=lambda: {"pg_id": 12, "exists": True})
    with patch("httpx.AsyncClient.get", return_value=resp) as get:
        asyncio.run(vector.record_lineage(" fact : 12 "))
    assert get.call_args.args[0].endswith("/memory/status/fact:12")


def test_mcp_lineage_refuses_negative_locally():
    with patch("httpx.AsyncClient.get") as get:
        out = asyncio.run(vector.record_lineage("fact:-5"))
    assert json.loads(out) == MCP_ERR
    assert not get.called


@pytest.mark.parametrize("path", [BRIDGE_PATH, TRACKED_PATH, VECTOR_PATH])
def test_clients_do_not_import_server_modules(path):
    text = open(path, encoding="utf-8").read()
    assert "import record_ref" not in text
    assert "from record_ref" not in text


def test_memory_bridge_copies_byte_identical():
    assert open(BRIDGE_PATH, "rb").read() == open(TRACKED_PATH, "rb").read()


VALUES = [None, "ops", " ops ", "", ["ops", "dev"], ["ops", 3], [" ", "ops"],
          ["dev", "ops", "dev"], 3, {"x": 1}, ("ops",), "Ops"]
BLOBS = [None, {"decision": {}}, {"decision": {"title": "t"}},
         {"decision": {"domain": "blobsec"}}]


def _cases():
    for dom, doms, blob in itertools.product(VALUES, VALUES, BLOBS):
        m = {}
        if dom is not None:
            m["domain"] = dom
        if doms is not None:
            m["domains"] = doms
        if blob is not None:
            m.update(copy.deepcopy(blob))
        yield m


@pytest.mark.parametrize("name,fold", [("bridge", bridge._normalize_domains),
                                       ("vector", vector._normalize_domains)])
def test_client_domain_fold_keeps_server_outcome(name, fold):
    shape = coordinator.MemoryCoordinator._domain_shape_error
    n = 0
    for m in _cases():
        folded = fold(copy.deepcopy(m))
        assert domain_axis.resolve_domains(m) == domain_axis.resolve_domains(folded), m
        assert domain_axis.names_a_domain(m) == domain_axis.names_a_domain(folded), m
        assert shape(m) == shape(folded), m
        n += 1
    assert n == len(VALUES) ** 2 * len(BLOBS)
