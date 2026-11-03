"""v1.0.46 Part R. A node REM retired as invalid stays processed, and /health
names that refusal on rem_daemon.

Fixtures use proj-a / dom-x. No token is minted.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import llm_backlog
import rem_loop
from rem_loop import REMDaemon


class _NodeSession:
    """In-memory nodes. The SET applies to a node the MATCH selected.

    The rem_invalid exclusion is applied only when the executed query text
    carries it. Removing the clause from the MATCH makes an invalid node
    lose rem_processed, and the test dies.
    """

    def __init__(self, nodes):
        self.nodes = nodes
        self.queries = []

    def session(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def run(self, query, **params):
        self.queries.append(query)
        respects_invalid = "coalesce(n.rem_invalid, false) = false" in query
        ids = params.get("ids")
        pg_id = params.get("pg_id")
        for node in self.nodes:
            if ids is not None and node["pg_id"] not in ids:
                continue
            if pg_id is not None and node["pg_id"] != pg_id:
                continue
            if respects_invalid and node.get("rem_invalid") is True:
                continue
            if "rem_processed = false" in query:
                node["rem_processed"] = False
        return None


def _daemon(nodes):
    daemon = REMDaemon.__new__(REMDaemon)
    daemon._withheld = set()
    daemon.driver = _NodeSession(nodes)
    return daemon


@pytest.mark.asyncio
async def test_r1_release_leaves_an_invalid_node_processed(monkeypatch):
    """Dies when _release_eligible_write_failed drops the rem_invalid MATCH
    clause: the fake then resets the retired node and it is no longer processed.
    """
    monkeypatch.setattr(llm_backlog, "eligible_write_failed", lambda kind: {"7", "8"})
    invalid = {"pg_id": 7, "rem_processed": True, "rem_invalid": True}
    healthy = {"pg_id": 8, "rem_processed": True, "rem_invalid": False}
    daemon = _daemon([invalid, healthy])
    await daemon._release_eligible_write_failed()
    assert invalid["rem_processed"] is True
    assert healthy["rem_processed"] is False


@pytest.mark.asyncio
async def test_r2_revert_leaves_an_invalid_node_processed():
    """Dies when _revert_rem_mark drops the rem_invalid MATCH clause."""
    invalid = {"pg_id": 7, "rem_processed": True, "rem_invalid": True}
    daemon = _daemon([invalid])
    await daemon._revert_rem_mark(7, rem_loop.KIND_FACT)
    assert invalid["rem_processed"] is True
    assert "coalesce(n.rem_invalid, false) = false" in daemon.driver.queries[0]


def test_r3_invalid_nodes_degrade_rem_daemon():
    """Dies when _rem_dependency ignores invalid_nodes: a retired node would
    read ok, and a count beside dead letters would name only one of them.
    None is not a refusal.
    """
    import hive_mind_proxy as g

    assert g._rem_dependency(True, None, invalid_nodes=2) == {
        "state": "degraded", "reason": "invalid_nodes:2",
    }
    assert g._rem_dependency(True, 3, invalid_nodes=2) == {
        "state": "degraded", "reason": "dead_letters:3, invalid_nodes:2",
    }
    assert g._rem_dependency(True, None, invalid_nodes=None) == {
        "state": "ok", "reason": None,
    }
    assert g._rem_dependency(True, None) == {"state": "ok", "reason": None}


def test_r4_health_reports_cached_invalid_nodes(monkeypatch):
    """Dies when the /health handler does not pass the cached
    graph_invalid_nodes into _rem_dependency: rem_daemon would stay ok
    while the cache says 2 retired nodes.
    """
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import importlib
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    g._rem_healthy = True

    class _Coord:
        pgvector_version = None
        hnsw_iterative_scan = False
        _axis_registry_read_failures = None
        _registry_census_failures = 0

        def consolidation_health(self):
            return {"fresh": True, "graph_invalid_nodes": 2}

        def dependency_snapshot(self):
            return {"rem": {"dead_lettered": 0}}

    class _S:
        def get(self, *a, **k):
            raise RuntimeError("no encoder")

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _S()
    checks = asyncio.run(g._build_health_checks(proxy, _Coord()))
    rem = checks["dependencies"]["rem_daemon"]
    assert rem["state"] == "degraded"
    assert rem["reason"] == "invalid_nodes:2"
