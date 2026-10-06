"""Tests for read hydration: Postgres text returned whole or marked cut on all read surfaces.

Each test catches a specific failure mode (F1 to F20) across graph, lineage, and search expansion.
Grounded in decision:180 (enrich results naming a record from Postgres without new endpoints) and
decision:1032 (graph holds the key, Postgres holds what a reader renders).
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from neo4j.graph import Graph, Node, Relationship, Path

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

import coordinator as coordinator_mod  # noqa: E402
from coordinator import (  # noqa: E402
    MemoryCoordinator, ONT, allocate_full_text,
    READ_FULL_TEXT_BUDGET_CHARS, GRAPH_QUERY_ROW_CAP,
    _json_safe,
)


class _AsyncCtx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *args):
        pass


class _FakeRecord:
    """Simulates a neo4j driver Record, with .data() converting Nodes to property dicts."""
    def __init__(self, mapping: dict):
        self._mapping = dict(mapping)

    def keys(self):
        return self._mapping.keys()

    def __getitem__(self, key):
        return self._mapping[key]

    def data(self):
        try:
            from neo4j import Record
            return Record(tuple(self._mapping.keys()), tuple(self._mapping.values())).data()
        except Exception:
            out = {}
            for k, v in self._mapping.items():
                if isinstance(v, Node):
                    out[k] = dict(v)
                elif isinstance(v, list):
                    out[k] = [dict(item) if isinstance(item, Node) else item for item in v]
                elif isinstance(v, dict):
                    out[k] = {ik: (dict(iv) if isinstance(iv, Node) else iv) for ik, iv in v.items()}
                elif isinstance(v, Relationship):
                    start_props = dict(getattr(v, "start_node", getattr(v, "_start_node", {})))
                    end_props = dict(getattr(v, "end_node", getattr(v, "_end_node", {})))
                    rel_type = getattr(v, "type", getattr(type(v), "__name__", "RELATES_TO"))
                    out[k] = (start_props, rel_type, end_props)
                elif isinstance(v, Path):
                    start_props = dict(getattr(v, "start_node", getattr(v, "_start_node", {})))
                    end_props = dict(getattr(v, "end_node", getattr(v, "_end_node", {})))
                    rel_type = v.relationships[0].type if getattr(v, "relationships", None) else "RELATES_TO"
                    out[k] = [start_props, rel_type, end_props]
                else:
                    out[k] = v
            return out


def _make_node(labels: list[str], properties: dict, element_id: str = "1", id_: int = 1) -> Node:
    """Construct a real neo4j.graph.Node instance."""
    try:
        g = Graph()
        return Node(g, element_id, id_, frozenset(labels), properties)
    except Exception:
        return Node(None, element_id, id_, frozenset(labels), properties)


def _make_rel(type_: str, properties: dict, start_node: Node, end_node: Node, element_id: str = "r1", id_: int = 1) -> Relationship:
    """Construct a real neo4j.graph.Relationship instance."""
    g = Graph()
    rel = g.relationship_type(type_)(g, element_id, id_, properties)
    rel._start_node = start_node
    rel._end_node = end_node
    return rel


def _make_path(start_node: Node, rel: Relationship, end_node: Node = None) -> Path:
    """Construct a real neo4j.graph.Path instance."""
    return Path(start_node, rel)


def _coord_with_mocks(records=(), pool_fetch=None, pool_fetchrow=None):
    c = MemoryCoordinator()

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=pool_fetch or [])
    mock_conn.fetchrow = AsyncMock(return_value=pool_fetchrow)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtx(mock_conn))
    c._pool = mock_pool

    session = MagicMock()
    tx_result = MagicMock()
    tx_result.fetch = AsyncMock(return_value=list(records))
    session.run = AsyncMock(return_value=tx_result)

    async def _exec_read(fn, *a, **kw):
        return await fn(session, *a, **kw)
    session.execute_read = AsyncMock(side_effect=_exec_read)

    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j

    return c, mock_conn, session


def _graph_request(cypher="MATCH (n) RETURN n", params=None, agent=None, body_extra=None):
    req = MagicMock()
    body_data = {"cypher": cypher, "params": params or {}}
    if body_extra:
        body_data.update(body_extra)
    req.json = AsyncMock(return_value=body_data)
    state = {"authenticated_agent": agent}
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    return req


def _status_request(ref_str, agent=None):
    req = MagicMock()
    req.match_info = {"pg_id": ref_str}
    req.rel_url.query = {}
    state = {"authenticated_agent": agent}
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    return req


# ── F1: Summary node and fact node with same id get each other's text ─────────

@pytest.mark.asyncio
async def test_f1_summary_and_fact_nodes_with_same_id_do_not_swap_text():
    """F1: A summary node and a fact node with the SAME id get each other's text."""
    n_fact = _make_node(["Fact"], {"pg_id": 42, "content": "graph fact copy"}, element_id="1", id_=1)
    n_summary = _make_node(["CommunitySummary"], {"pg_id": 42, "content": "graph summary copy"}, element_id="2", id_=2)
    records = [_FakeRecord({"n": n_fact}), _FakeRecord({"n": n_summary})]

    c, mock_conn, _ = _coord_with_mocks(records)

    async def _fake_fetch(sql, ids):
        if "technical_docs" in sql and "length(content)" in sql:
            return [{"id": 42, "content_chars": 20, "type": "fact", "superseded": False,
                     "visibility": "global", "agent_id": None, "scope": None}]
        if "community_summaries" in sql and "length(content)" in sql:
            return [{"id": 42, "content_chars": 23, "snippet": "summary snippet",
                     "superseded": False, "metadata": {}}]
        if "technical_docs" in sql and "content FROM technical_docs" in sql:
            return [{"id": 42, "content": "Postgres fact text"}]
        if "community_summaries" in sql and "content FROM community_summaries" in sql:
            return [{"id": 42, "content": "Postgres summary text"}]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    r0 = body["records"][0]["n"]
    r1 = body["records"][1]["n"]

    assert r0["content"] == "Postgres fact text"
    assert r0["ref"] == "fact:42"
    assert r0["content_source"] == "postgres"

    assert r1["content"] == "Postgres summary text"
    assert r1["ref"] == "summary:42"
    assert r1["content_source"] == "postgres"


# ── F2: Budget overflow or skip-then-fill ──────────────────────────────────────

def test_f2_allocate_full_text_respects_budget_and_fills_after_skip():
    """F2: Whole texts returned exceed the budget; or a record that fits after a skipped one is not filled."""
    # Budget is 100. Record 1 (80), Record 2 (50 - skipped), Record 3 (15 - fits in remaining 20).
    lengths = [
        (("technical_docs", 1), 80),
        (("technical_docs", 2), 50),
        (("technical_docs", 3), 15),
    ]
    chosen = allocate_full_text(lengths, budget=100)
    assert ("technical_docs", 1) in chosen
    assert ("technical_docs", 2) not in chosen
    assert ("technical_docs", 3) in chosen


@pytest.mark.asyncio
async def test_f2_graph_hydration_skips_large_and_fills_subsequent():
    """F2: Whole texts returned on graph do not exceed budget, and a later record that fits is filled."""
    n1 = _make_node(["Fact"], {"pg_id": 1, "content": "graph 1"}, element_id="1", id_=1)
    n2 = _make_node(["Fact"], {"pg_id": 2, "content": "graph 2"}, element_id="2", id_=2)
    n3 = _make_node(["Fact"], {"pg_id": 3, "content": "graph 3"}, element_id="3", id_=3)
    records = [_FakeRecord({"n": n1}), _FakeRecord({"n": n2}), _FakeRecord({"n": n3})]

    c, mock_conn, _ = _coord_with_mocks(records)

    async def _fake_fetch(sql, ids):
        if "length(content)" in sql:
            return [
                {"id": 1, "content_chars": 15000, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
                {"id": 2, "content_chars": 5000, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
                {"id": 3, "content_chars": 500, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
            ]
        if "content FROM" in sql:
            out = []
            if 1 in ids:
                out.append({"id": 1, "content": "X" * 15000})
            if 3 in ids:
                out.append({"id": 3, "content": "Z" * 500})
            return out
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    # Default budget is 16000: n1 (15000) fits, n2 (5000) skips, n3 (500) fits!
    assert body["records"][0]["n"]["content_source"] == "postgres"
    assert body["records"][1]["n"]["content_source"] == "graph"
    assert body["records"][1]["n"]["content_truncated"] is True
    assert body["records"][2]["n"]["content_source"] == "postgres"


# ── F3: Unchosen node flags, ref, content_chars, and content preserved ────────

@pytest.mark.asyncio
async def test_f3_unchosen_node_preserves_content_and_gains_source_chars_ref():
    """F3: A node not chosen lacks content_truncated: true, its true content_chars, ref, or content_source: 'graph'; or its graph content was shortened."""
    n1 = _make_node(["Fact"], {"pg_id": 10, "content": "original graph content"}, element_id="1", id_=1)
    records = [_FakeRecord({"n": n1})]

    c, mock_conn, _ = _coord_with_mocks(records)

    async def _fake_fetch(sql, ids):
        if "length(content)" in sql:
            return [{"id": 10, "content_chars": 50000, "type": "fact", "superseded": False,
                     "visibility": "global", "agent_id": None, "scope": None}]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    rec = body["records"][0]["n"]
    assert rec["content"] == "original graph content"
    assert rec["content_chars"] == 50000
    assert rec["content_truncated"] is True
    assert rec["ref"] == "fact:10"
    assert rec["content_source"] == "graph"


# ── F4: Private record visibility on lineage and graph ────────────────────────

@pytest.mark.asyncio
async def test_f4_private_record_withheld_from_non_owner_on_lineage_and_graph():
    """F4: A private record's text reaches a non-owner on lineage or on graph; or the owner is refused it."""
    c, mock_conn, _ = _coord_with_mocks()

    rec_row = {
        "id": 55, "type": "fact", "created_at": None, "superseded": False,
        "superseded_by": None, "grounded_in": None, "supersession_ack": None,
        "content": "secret private postgres fact",
        "visibility": "private", "agent_id": "alice", "scope": "global",
    }
    mock_conn.fetchrow = AsyncMock(side_effect=[rec_row, None])
    mock_conn.fetch = AsyncMock(return_value=[])

    # 1. Non-owner bob on lineage
    req_bob = _status_request("fact:55", agent="bob")
    resp_bob = await c.handle_status(req_bob)
    assert resp_bob.status == 200
    b_bob = json.loads(resp_bob.text)
    assert b_bob["content"] is None
    assert b_bob["content_withheld"] == "not_visible"
    assert "content_chars" not in b_bob

    # 2. Owner alice on lineage
    mock_conn.fetchrow = AsyncMock(side_effect=[rec_row, None])
    req_alice = _status_request("fact:55", agent="alice")
    resp_alice = await c.handle_status(req_alice)
    assert resp_alice.status == 200
    b_alice = json.loads(resp_alice.text)
    assert b_alice["content"] == "secret private postgres fact"
    assert b_alice["content_chars"] == 28
    assert b_alice["content_truncated"] is False

    # 3. Non-owner bob on graph
    n_priv = _make_node(["Fact"], {"pg_id": 55, "content": "graph copy"}, element_id="1", id_=1)
    c_graph, mock_conn_g, _ = _coord_with_mocks([_FakeRecord({"n": n_priv})])
    async def _fake_fetch_priv(sql, ids):
        if "length(content)" in sql:
            return [{"id": 55, "content_chars": len(rec_row["content"]), "type": "fact", "superseded": False,
                     "visibility": "private", "agent_id": "alice", "scope": "global"}]
        if "content FROM" in sql:
            return [{"id": 55, "content": rec_row["content"]}]
        return []
    mock_conn_g.fetch = AsyncMock(side_effect=_fake_fetch_priv)

    resp_g_bob = await c_graph.handle_graph(_graph_request(agent="bob"))
    assert resp_g_bob.status == 200
    rec_g_bob = json.loads(resp_g_bob.text)["records"][0]["n"]
    assert rec_g_bob["content"] == "graph copy"
    assert rec_g_bob["content_source"] == "graph"
    assert "content_chars" not in rec_g_bob
    assert rec_row["content"] not in resp_g_bob.text

    # 4. Owner alice on graph
    resp_g_alice = await c_graph.handle_graph(_graph_request(agent="alice"))
    assert resp_g_alice.status == 200
    rec_g_alice = json.loads(resp_g_alice.text)["records"][0]["n"]
    assert rec_g_alice["content"] == rec_row["content"]
    assert rec_g_alice["content_source"] == "postgres"
    assert rec_g_alice["content_chars"] == len(rec_row["content"])
    assert rec_g_alice["content_truncated"] is False
    assert rec_row["content"] in resp_g_alice.text


# ── F5: Fetch error or TimeoutError fails read instead of 200 degrade ─────────

@pytest.mark.asyncio
async def test_f5_fetch_error_or_timeout_degrades_gracefully_on_graph_and_search():
    """F5: A fetch error, or asyncio.TimeoutError from _acquire, fails the graph read or the search instead of returning 200 with untouched nodes marked content_source: 'graph'."""
    n1 = _make_node(["Fact"], {"pg_id": 77, "content": "graph content intact"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1})])
    c._acquire = MagicMock(side_effect=asyncio.TimeoutError("pool acquire timeout"))

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    rec = body["records"][0]["n"]
    assert rec["content"] == "graph content intact"
    assert rec["content_source"] == "graph"


@pytest.mark.asyncio
async def test_f5_fetch_runtime_error_degrades_gracefully_with_warning(caplog):
    """F5: When Postgres fetch raises RuntimeError, handle_graph returns 200, nodes untouched plus content_source: 'graph', exactly one warning logged."""
    import logging
    n1 = _make_node(["Fact"], {"pg_id": 77, "content": "graph content intact"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1})])
    mock_conn.fetch = AsyncMock(side_effect=RuntimeError("database read failed"))

    with caplog.at_level(logging.WARNING):
        caplog.clear()
        resp = await c.handle_graph(_graph_request())

    assert resp.status == 200
    body = json.loads(resp.text)
    rec = body["records"][0]["n"]
    assert rec["content"] == "graph content intact"
    assert rec["content_source"] == "graph"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "graph query text hydration failed" in warnings[0].message

    # Search expansion failure degrade
    mock_session = AsyncMock()
    ctx_entries = [{"pg_id": 77, "label": ONT.fact, "snippet": "cypher snippet"}]
    # Single anchor expansion
    c_search, _, _ = _coord_with_mocks()
    c_search._acquire = MagicMock(side_effect=asyncio.TimeoutError("timeout"))
    anchor_map = {1: list(ctx_entries)}
    await c_search._hydrate_and_filter_neighbors(anchor_map, viewer="agent")
    assert anchor_map[1][0]["snippet"] == "cypher snippet"
    assert "content_chars" not in anchor_map[1][0]


# ── F6: Non-eligible graph types altered or top-level list unhydrated ──────────

@pytest.mark.asyncio
async def test_f6_scalar_projection_entity_node_untouched_and_list_hydrated():
    """F6: A scalar projection, a Path, a relationship, a map value or an Entity node is altered; or a node in a top-level list is not hydrated."""
    node_in_list = _make_node(["Fact"], {"pg_id": 7, "content": "graph 7"}, element_id="1", id_=1)
    entity_node = _make_node(["Entity"], {"name": "Postgres"}, element_id="2", id_=2)
    node_in_map = _make_node(["Fact"], {"pg_id": 99, "content": "map fact"}, element_id="3", id_=3)
    fake_map = {"n": node_in_map}
    scalar_proj = "scalar value"

    n_start = _make_node(["Entity"], {"name": "StartNode"}, element_id="10", id_=10)
    n_end = _make_node(["Entity"], {"name": "EndNode"}, element_id="11", id_=11)
    rel = _make_rel("RELATES_TO", {"weight": 1.0}, n_start, n_end, element_id="r1", id_=1)
    path = _make_path(n_start, rel)

    rec = _FakeRecord({
        "scalar": scalar_proj,
        "ent": entity_node,
        "m": fake_map,
        "nlist": [node_in_list],
        "rel": rel,
        "path": path,
    })
    expected_data = rec.data()
    c, mock_conn, _ = _coord_with_mocks([rec])

    async def _fake_fetch(sql, ids):
        if "length(content)" in sql:
            return [{"id": 7, "content_chars": 22, "type": "fact", "superseded": False,
                     "visibility": "global", "agent_id": None, "scope": None}]
        if "content FROM" in sql:
            return [{"id": 7, "content": "full postgres text 7"}]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    row = body["records"][0]
    assert row["scalar"] == expected_data["scalar"]
    assert row["ent"] == expected_data["ent"]

    start_props = {"name": "StartNode"}
    end_props = {"name": "EndNode"}
    node_props = {"pg_id": 99, "content": "map fact"}

    # Exact shapes on Record.data():
    # p as list [start_props, "RELATES_TO", end_props]
    # r as tuple (start_props, "RELATES_TO", end_props)
    # m as {"n": node_props}
    assert expected_data["path"] == [start_props, "RELATES_TO", end_props]
    assert expected_data["rel"] == (start_props, "RELATES_TO", end_props)
    assert expected_data["m"] == {"n": node_props}

    # Deserialized JSON shapes from handle_graph:
    assert row["path"] == [start_props, "RELATES_TO", end_props]
    assert row["rel"] == [start_props, "RELATES_TO", end_props]
    assert row["m"] == {"n": node_props}

    for d in (
        row["ent"],
        row["m"]["n"],
        row["rel"][0],
        row["rel"][2],
        row["path"][0],
        row["path"][2],
        expected_data["ent"],
        expected_data["m"]["n"],
        expected_data["rel"][0],
        expected_data["rel"][2],
        expected_data["path"][0],
        expected_data["path"][2],
    ):
        assert "content_source" not in d

    assert row["nlist"][0]["content"] == "full postgres text 7"
    assert row["nlist"][0]["content_source"] == "postgres"


# ── F7: _record_text_whole gets unchosen id or called from search ─────────────

@pytest.mark.asyncio
async def test_f7_record_text_whole_receives_only_chosen_ids_and_never_called_by_search():
    """F7: _record_text_whole is sent an id allocate_full_text did not choose; or the search path calls it at all."""
    n1 = _make_node(["Fact"], {"pg_id": 1, "content": "g1"}, element_id="1", id_=1)
    n2 = _make_node(["Fact"], {"pg_id": 2, "content": "g2"}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1}), _FakeRecord({"n": n2})])

    mock_conn.fetch = AsyncMock(side_effect=[
        # index query: id 1 (10 chars), id 2 (50000 chars - exceeds budget)
        [
            {"id": 1, "content_chars": 10, "type": "fact", "superseded": False,
             "visibility": "global", "agent_id": None, "scope": None},
            {"id": 2, "content_chars": 50000, "type": "fact", "superseded": False,
             "visibility": "global", "agent_id": None, "scope": None},
        ],
        # whole query
        [{"id": 1, "content": "whole pg 1"}],
    ])

    orig_whole = c._record_text_whole
    spy_whole = AsyncMock(side_effect=orig_whole)
    c._record_text_whole = spy_whole

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    assert spy_whole.await_count == 1
    # Check arguments: doc_ids must be exactly [1]
    args = spy_whole.await_args.args
    assert args[1] == [1]
    assert args[2] == []

    # Check search expansion never calls _record_text_whole
    spy_whole.reset_mock()
    anchor_map = {99: [{"pg_id": 1, "label": ONT.fact, "snippet": "snip"}]}
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content_chars": 10, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])
    await c._hydrate_and_filter_neighbors(anchor_map, viewer="agent")
    assert spy_whole.await_count == 0


# ── F8: Summary neighbour with null/missing/retired snippet handling ──────────

@pytest.mark.asyncio
async def test_f8_summary_neighbor_removal_and_snippet_replacement():
    """F8: A summary neighbour is returned with a null snippet; a retired or missing one is returned; or removing one skips the entry after it."""
    c, mock_conn, _ = _coord_with_mocks()
    # Neighbors:
    # 0: Summary 1 (missing in Postgres)
    # 1: Summary 2 (superseded in Postgres)
    # 2: Summary 3 (live in Postgres with 120-char snippet)
    # 3: Fact 4 (live in Postgres)
    entries = [
        {"pg_id": 1, "label": ONT.community_summary, "snippet": None},
        {"pg_id": 2, "label": ONT.community_summary, "snippet": "old Cypher snippet"},
        {"pg_id": 3, "label": ONT.community_summary, "snippet": "old Cypher snippet"},
        {"pg_id": 4, "label": ONT.fact, "snippet": "fact snippet"},
    ]
    anchor_map = {100: entries}

    async def _fake_fetch(sql, ids):
        if "community_summaries" in sql:
            return [
                {"id": 2, "content_chars": 80, "snippet": "retired snippet",
                 "superseded": True, "metadata": {}},
                {"id": 3, "content_chars": 120, "snippet": "authoritative Postgres snippet 3",
                 "superseded": False, "metadata": {}},
            ]
        if "technical_docs" in sql:
            return [
                {"id": 4, "content_chars": 95, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    await c._hydrate_and_filter_neighbors(anchor_map, viewer="agent")
    kept = anchor_map[100]
    # Exactly Summary 3 and Fact 4 kept
    assert len(kept) == 2
    assert kept[0]["pg_id"] == 3
    assert kept[0]["snippet"] == "authoritative Postgres snippet 3"
    assert kept[0]["ref"] == "summary:3"
    assert kept[0]["content_chars"] == 120

    assert kept[1]["pg_id"] == 4
    assert kept[1]["snippet"] == "fact snippet"
    assert kept[1]["ref"] == "fact:4"
    assert kept[1]["content_chars"] == 95


@pytest.mark.asyncio
async def test_f8_expand_graph_context_single_anchor_removes_retired_summary():
    """F8: Single-anchor _expand_graph_context returns list with retired summary removed and subsequent entry intact."""
    c, mock_conn, _ = _coord_with_mocks()

    session = MagicMock()
    rec_sum = {
        "rel_type": "SUMMARIZED_BY", "direction": "out", "rel_props": {},
        "labels": [ONT.community_summary], "name": None, "aliases": None,
        "pg_id": 2, "snippet": "old Cypher snippet",
        "adr_fact_kind": None, "adr_source_ref": None,
    }
    rec_fact = {
        "rel_type": "MENTIONS", "direction": "out", "rel_props": {},
        "labels": [ONT.fact], "name": None, "aliases": None,
        "pg_id": 4, "snippet": "fact snippet",
        "adr_fact_kind": None, "adr_source_ref": None,
    }

    class _FakeResult:
        def __init__(self, items):
            self._items = items
        def __aiter__(self):
            return self._iter()
        async def _iter(self):
            for item in self._items:
                yield item

    session.run = AsyncMock(return_value=_FakeResult([rec_sum, rec_fact]))

    async def _fake_fetch(sql, ids):
        if "community_summaries" in sql:
            return [
                {"id": 2, "content_chars": 80, "snippet": "retired snippet",
                 "superseded": True, "metadata": {}},
            ]
        if "technical_docs" in sql:
            return [
                {"id": 4, "content_chars": 95, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    res = await c._expand_graph_context(session, pg_id=100, anchor_labels=(ONT.fact,), viewer="agent")
    assert len(res) == 1
    assert res[0]["pg_id"] == 4
    assert res[0]["ref"] == "fact:4"
    assert res[0]["content_chars"] == 95


# ── F9: Wrong-type 404 or exists: false reply carries text keys ───────────────

@pytest.mark.asyncio
async def test_f9_lineage_refusal_and_missing_record_omit_text_keys():
    """F9: The wrong-type 404 or the exists: false reply from lineage carries text keys."""
    c, mock_conn, _ = _coord_with_mocks()

    # 1. exists: false
    mock_conn.fetchrow = AsyncMock(side_effect=[None, None])
    resp_missing = await c.handle_status(_status_request("fact:9999"))
    assert resp_missing.status == 200
    b_missing = json.loads(resp_missing.text)
    assert b_missing["exists"] is False
    for k in ("content", "content_chars", "content_truncated", "content_withheld"):
        assert k not in b_missing

    # 2. wrong-type 404 (caller asked for fact:12, row is decision)
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"type": "decision", "created_at": None, "superseded": False,
         "superseded_by": None, "grounded_in": None, "supersession_ack": None,
         "content": "dec text", "visibility": "global", "agent_id": None, "scope": None},
        None,
    ])
    resp_wrong = await c.handle_status(_status_request("fact:12"))
    assert resp_wrong.status == 404
    b_wrong = json.loads(resp_wrong.text)
    for k in ("content", "content_chars", "content_truncated", "content_withheld"):
        assert k not in b_wrong

    # 3. missing summary:N (exists: false)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    resp_sum_missing = await c.handle_status(_status_request("summary:9999"))
    assert resp_sum_missing.status == 404
    b_sum_missing = json.loads(resp_sum_missing.text)
    assert b_sum_missing["exists"] is False
    for k in ("content", "content_chars", "content_truncated", "content_withheld"):
        assert k not in b_sum_missing

    # 4. summary-versus-insight wrong-type 404 (caller asked for summary:50, row is insight)
    mock_conn.fetchrow = AsyncMock(return_value={
        "id": 50, "metadata": json.dumps({"kind": "insight"}), "source_pg_ids": [],
        "created_at": None, "superseded": False, "superseded_reason": None,
        "superseded_by": None, "run_id": 1,
        "content": "insight content", "content_chars": 15,
    })
    resp_sum_wrong = await c.handle_status(_status_request("summary:50"))
    assert resp_sum_wrong.status == 404
    b_sum_wrong = json.loads(resp_sum_wrong.text)
    assert b_sum_wrong["status"] == "error"
    for k in ("content", "content_chars", "content_truncated", "content_withheld"):
        assert k not in b_sum_wrong


# ── F10: Chosen Decision node gains content ───────────────────────────────────

@pytest.mark.asyncio
async def test_f10_chosen_decision_node_gains_postgres_content():
    """F10: A chosen Decision node is returned without Postgres content."""
    # Graph decision node has no content property
    d_node = _make_node(["Decision"], {"pg_id": 30, "title": "ADR 30"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": d_node})])

    mock_conn.fetch = AsyncMock(side_effect=[
        [{"id": 30, "content_chars": 46, "type": "decision", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None}],
        [{"id": 30, "content": "Full decision rationale and text from Postgres"}],
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "Full decision rationale and text from Postgres"
    assert rec["content_chars"] == 46
    assert rec["content_truncated"] is False
    assert rec["content_source"] == "postgres"
    assert rec["ref"] == "decision:30"


# ── F11: Duplicate node charged twice or treated differently ──────────────────

@pytest.mark.asyncio
async def test_f11_same_node_in_multiple_rows_charged_once():
    """F11: The same node in two rows is charged to the budget twice, or its second copy is treated differently."""
    # 2 rows with the same node (length 10000). If charged twice against 16000 budget, 2nd would fail.
    node_a = _make_node(["Fact"], {"pg_id": 5, "content": "graph 5"}, element_id="1", id_=1)
    node_b = _make_node(["Fact"], {"pg_id": 5, "content": "graph 5"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": node_a}), _FakeRecord({"n": node_b})])

    mock_conn.fetch = AsyncMock(side_effect=[
        [{"id": 5, "content_chars": 10000, "type": "fact", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None}],
        [{"id": 5, "content": "A" * 10000}],
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    r0 = body["records"][0]["n"]
    r1 = body["records"][1]["n"]
    assert r0["content_source"] == "postgres"
    assert r1["content_source"] == "postgres"
    assert r0["content"] == "A" * 10000
    assert r1["content"] == "A" * 10000


def test_f11_allocate_full_text_direct_dedup_and_first_length():
    """F11: Direct allocate_full_text tests for seen check and first length standing."""
    k1 = ("technical_docs", 1)
    k2 = ("technical_docs", 2)
    # [(k1, 20), (k1, 20), (k2, 20)] at budget 40 -> {k1, k2}
    assert allocate_full_text([(k1, 20), (k1, 20), (k2, 20)], budget=40) == {k1, k2}
    # [(k1, 100), (k1, 10), (k2, 10)] at budget 50 -> {k2} (the first length stands)
    assert allocate_full_text([(k1, 100), (k1, 10), (k2, 10)], budget=50) == {k2}


# ── F12: Lineage summary over budget cut with flag ─────────────────────────────

@pytest.mark.asyncio
async def test_f12_lineage_summary_over_budget_is_cut_and_flagged():
    """F12: lineage summary:N over the budget returns uncut text, or cut text without the flag."""
    c, mock_conn, _ = _coord_with_mocks()
    # Summary of 20,000 characters
    mock_conn.fetchrow = AsyncMock(return_value={
        "id": 14, "metadata": json.dumps({"kind": "thematic"}), "source_pg_ids": [],
        "created_at": None, "superseded": False, "superseded_reason": None,
        "superseded_by": None, "run_id": 1,
        "content": "S" * 16000,
        "content_chars": 20000,
    })
    resp = await c.handle_status(_status_request("summary:14"))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert len(body["content"]) == 16000
    assert body["content_chars"] == 20000
    assert body["content_truncated"] is True

    call_args = mock_conn.fetchrow.await_args
    sql = call_args.args[0]
    assert "left(content, $2)" in sql
    assert call_args.args[2] == READ_FULL_TEXT_BUDGET_CHARS


# ── F13: Technical_docs record longer than budget is whole on lineage ─────────

@pytest.mark.asyncio
async def test_f13_technical_docs_longer_than_budget_never_cut_on_lineage():
    """F13: A technical_docs record LONGER than the budget is cut on lineage (it must be whole there)."""
    c, mock_conn, _ = _coord_with_mocks()
    full_text = "F" * 25000
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": 8, "type": "fact", "created_at": None, "superseded": False,
            "superseded_by": None, "grounded_in": None, "supersession_ack": None,
            "content": full_text, "visibility": "global", "agent_id": None, "scope": None,
        },
        None,
    ])
    mock_conn.fetch = AsyncMock(return_value=[])

    resp = await c.handle_status(_status_request("fact:8"))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert len(body["content"]) == 25000
    assert body["content_chars"] == 25000
    assert body["content_truncated"] is False

    call_args = mock_conn.fetchrow.await_args_list[0]
    sql = call_args.args[0]
    assert "left(" not in sql


# ── F14: Superseded node hydrated on graph ────────────────────────────────────

@pytest.mark.asyncio
async def test_f14_superseded_node_is_hydrated_on_graph():
    """F14: A superseded fact or summary NODE is not hydrated on /memory/graph."""
    n = _make_node(["Fact"], {"pg_id": 9, "content": "graph copy"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    mock_conn.fetch = AsyncMock(side_effect=[
        [{"id": 9, "content_chars": 30, "type": "fact", "superseded": True,
          "visibility": "global", "agent_id": None, "scope": None}],
        [{"id": 9, "content": "Superseded fact Postgres whole text"}],
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "Superseded fact Postgres whole text"
    assert rec["content_source"] == "postgres"


@pytest.mark.asyncio
async def test_f14_superseded_summary_node_is_hydrated_on_graph():
    """F14: A superseded CommunitySummary node is hydrated on /memory/graph."""
    n_sum = _make_node(["CommunitySummary"], {"pg_id": 99, "content": "graph summary copy"}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n_sum})])

    mock_conn.fetch = AsyncMock(side_effect=[
        [{"id": 99, "content_chars": 38, "snippet": "snip", "superseded": True, "metadata": {}}],
        [{"id": 99, "content": "Superseded summary Postgres whole text"}],
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "Superseded summary Postgres whole text"
    assert rec["content_source"] == "postgres"
    assert rec["ref"] == "summary:99"
    assert rec["content_chars"] == 38
    assert rec["content_truncated"] is False


# ── F15: Scope visibility on lineage and graph ─────────────────────────────────

@pytest.mark.asyncio
async def test_f15_scope_visibility_withheld_on_lineage_and_graph():
    """F15: visibility = 'scope' text is returned on lineage or graph; or an unauthenticated caller gets non-global text."""
    c, mock_conn, _ = _coord_with_mocks()

    scope_secret = "<secret scope string>"
    # 1. Lineage with scope visibility
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": 15, "type": "fact", "created_at": None, "superseded": False,
            "superseded_by": None, "grounded_in": None, "supersession_ack": None,
            "content": scope_secret, "visibility": "scope", "agent_id": "alice", "scope": "ops",
        },
        None,
    ])
    mock_conn.fetch = AsyncMock(return_value=[])
    resp = await c.handle_status(_status_request("fact:15", agent="alice"))
    assert resp.status == 200
    b = json.loads(resp.text)
    assert b["content"] is None
    assert b["content_withheld"] == "not_visible"
    assert scope_secret not in resp.text

    # 2. Graph with scope visibility
    n = _make_node(["Fact"], {"pg_id": 15, "content": "graph copy"}, element_id="1", id_=1)
    c_g, mock_conn_g, _ = _coord_with_mocks([_FakeRecord({"n": n})])
    async def _fake_fetch_scope(sql, ids):
        if "length(content)" in sql:
            return [{"id": 15, "content_chars": len(scope_secret), "type": "fact", "superseded": False,
                     "visibility": "scope", "agent_id": "alice", "scope": "ops"}]
        if "content FROM" in sql:
            return [{"id": 15, "content": scope_secret}]
        return []
    mock_conn_g.fetch = AsyncMock(side_effect=_fake_fetch_scope)

    resp_g = await c_g.handle_graph(_graph_request(agent="alice"))
    assert resp_g.status == 200
    rec_g = json.loads(resp_g.text)["records"][0]["n"]
    assert rec_g["content"] == "graph copy"
    assert rec_g["content_source"] == "graph"
    assert "content_chars" not in rec_g
    assert scope_secret not in resp_g.text


# ── F16: Private record does not spend budget ─────────────────────────────────

@pytest.mark.asyncio
async def test_f16_private_unreadable_record_spends_no_budget():
    """F16: A private record earlier in the walk spends budget; or its node gains content / content_chars."""
    # Node 1: Private (not readable by caller), length 15000.
    # Node 2: Global, length 5000.
    # Total budget = 16000. If Node 1 spends budget, Node 2 (5000) does not fit!
    n1 = _make_node(["Fact"], {"pg_id": 101, "content": "graph 101"}, element_id="1", id_=1)
    n2 = _make_node(["Fact"], {"pg_id": 102, "content": "graph 102"}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1}), _FakeRecord({"n": n2})])

    mock_conn.fetch = AsyncMock(side_effect=[
        [
            {"id": 101, "content_chars": 15000, "type": "fact", "superseded": False,
             "visibility": "private", "agent_id": "other_agent", "scope": "global"},
            {"id": 102, "content_chars": 5000, "type": "fact", "superseded": False,
             "visibility": "global", "agent_id": None, "scope": None},
        ],
        [{"id": 102, "content": "Y" * 5000}],
    ])

    resp = await c.handle_graph(_graph_request(agent="caller_agent"))
    assert resp.status == 200
    body = json.loads(resp.text)
    r1 = body["records"][0]["n"]
    r2 = body["records"][1]["n"]

    assert r1["content"] == "graph 101"
    assert r1["content_source"] == "graph"
    assert "content_chars" not in r1

    assert r2["content_source"] == "postgres"
    assert r2["content"] == "Y" * 5000


# ── F17: Insight summary ref is insight:N, not summary:N ──────────────────────

@pytest.mark.asyncio
async def test_f17_insight_summary_gets_insight_ref():
    """F17: A summary whose metadata kind is insight gets a summary:N ref instead of insight:N."""
    n = _make_node(["CommunitySummary"], {"pg_id": 70, "content": "graph insight"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    mock_conn.fetch = AsyncMock(side_effect=[
        [{"id": 70, "content_chars": 20, "snippet": "insight snip",
          "superseded": False, "metadata": json.dumps({"kind": "insight"})}],
        [{"id": 70, "content": "whole insight content"}],
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["ref"] == "insight:70"


# ── F18: content_truncated false when graph content equals whole text ─────────

@pytest.mark.asyncio
async def test_f18_content_truncated_false_when_graph_content_equals_full_length(monkeypatch):
    """F18: content_truncated is true for a not-chosen node whose graph content already equals the whole text."""
    # Graph content is 15 chars, full text is 15 chars. Budget is 0 (not chosen).
    n = _make_node(["Fact"], {"pg_id": 88, "content": "exact 15 chars!"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    monkeypatch.setattr(coordinator_mod, "READ_FULL_TEXT_BUDGET_CHARS", 0)
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 88, "content_chars": 15, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])
    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "exact 15 chars!"
    assert rec["content_chars"] == 15
    assert rec["content_truncated"] is False
    assert rec["content_source"] == "graph"


# ── F19: Row cap bounds distinct keys sent to Postgres ────────────────────────

@pytest.mark.asyncio
async def test_f19_keys_past_cap_are_not_sent_to_postgres(monkeypatch):
    """F19: The 10,001st distinct eligible key is sent to Postgres."""
    # Put 12 nodes in ONE row as a list (the collect(n) shape the cap on keys exists for)
    nodes = [
        _make_node(["Fact"], {"pg_id": i, "content": f"g {i}"}, element_id=str(i), id_=i)
        for i in range(1, 13)
    ]
    records = [_FakeRecord({"nodes": nodes})]
    c, mock_conn, _ = _coord_with_mocks(records)

    monkeypatch.setattr(coordinator_mod, "GRAPH_QUERY_ROW_CAP", 10)
    mock_conn.fetch = AsyncMock(side_effect=[
        # index query
        [{"id": i, "content_chars": 10, "type": "fact", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None} for i in range(1, 11)],
        # whole query
        [{"id": i, "content": f"full {i}"} for i in range(1, 11)],
    ])
    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    # Check first query sent to Postgres: must contain exactly 1..10
    first_call = mock_conn.fetch.await_args_list[0]
    ids_sent = first_call.args[1]
    assert ids_sent == list(range(1, 11))

    # Nodes 11 and 12 carry content_source: "graph", their own graph content, and no content_chars
    body = json.loads(resp.text)
    row_nodes = body["records"][0]["nodes"]
    n11 = row_nodes[10]
    n12 = row_nodes[11]
    assert n11["content_source"] == "graph"
    assert n11["content"] == "g 11"
    assert "content_chars" not in n11
    assert n12["content_source"] == "graph"
    assert n12["content"] == "g 12"
    assert "content_chars" not in n12


# ── F20: Private neighbour visibility for owner vs non-owner ──────────────────

@pytest.mark.asyncio
async def test_f20_private_neighbor_visibility_in_expansion():
    """F20: A neighbour of a private record gains ref/content_chars for a non-owner; or the owner does not get them."""
    c, mock_conn, _ = _coord_with_mocks()

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 66, "content_chars": 28, "type": "fact", "superseded": False,
         "visibility": "private", "agent_id": "owner_agent", "scope": "global"},
    ])

    # 1. Non-owner
    anchor_map_bob = {1: [{"pg_id": 66, "label": ONT.fact, "snippet": "private fact snip"}]}
    await c._hydrate_and_filter_neighbors(anchor_map_bob, viewer="other_agent")
    entry_bob = anchor_map_bob[1][0]
    assert entry_bob["snippet"] == "private fact snip"
    assert "ref" not in entry_bob
    assert "content_chars" not in entry_bob

    # 2. Owner
    anchor_map_alice = {1: [{"pg_id": 66, "label": ONT.fact, "snippet": "private fact snip"}]}
    await c._hydrate_and_filter_neighbors(anchor_map_alice, viewer="owner_agent")
    entry_alice = anchor_map_alice[1][0]
    assert entry_alice["ref"] == "fact:66"
    assert entry_alice["content_chars"] == 28


# ── Finding 3: Chosen key missing from whole-text result ──────────────────────

@pytest.mark.asyncio
async def test_chosen_key_missing_from_whole_text_treated_as_readable_not_chosen():
    """A chosen key missing from the whole-text result is treated as readable, not chosen.

    If a record disappears between the index query and the whole text query,
    whole_map.get(key) is absent. The node must keep its own graph properties
    (its graph content is not overwritten with None), plus content_chars,
    content_truncated, content_source: "graph", and ref.
    """
    node = _make_node(["Fact"], {"pg_id": 42, "content": "graph copy 42"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": node})])

    mock_conn.fetch = AsyncMock(side_effect=[
        # 1. _record_text_index returns the row
        [{"id": 42, "content_chars": 500, "type": "fact", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None}],
        # 2. _record_text_whole returns empty list (row went away between the queries)
        [],
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "graph copy 42"
    assert rec["content_chars"] == 500
    assert rec["content_truncated"] is True
    assert rec["content_source"] == "graph"
    assert rec["ref"] == "fact:42"


# ── Body scope is ignored on graph ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_request_body_scope_is_ignored():
    """A graph request whose JSON body carries 'scope' matching a scope-visibility record still gets graph copy and no content_chars."""
    n = _make_node(["Fact"], {"pg_id": 15, "content": "graph copy"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    secret_text = "<secret scope string>"
    async def _fake_fetch(sql, ids):
        if "length(content)" in sql:
            return [{"id": 15, "content_chars": len(secret_text), "type": "fact", "superseded": False,
                     "visibility": "scope", "agent_id": "alice", "scope": "ops"}]
        if "content FROM" in sql:
            return [{"id": 15, "content": secret_text}]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    # Body carries "scope": "ops" matching the record's scope
    req = _graph_request(agent="alice", body_extra={"scope": "ops"})
    resp = await c.handle_graph(req)
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "graph copy"
    assert rec["content_source"] == "graph"
    assert "content_chars" not in rec
    assert secret_text not in resp.text


# ── Row value whose indexing raises ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_row_value_whose_indexing_raises_still_returns_200():
    """A row value whose indexing raises still returns 200."""
    node = _make_node(["Fact"], {"pg_id": 1, "content": "graph 1"}, element_id="1", id_=1)

    class _BrokenIndexSeq(list):
        def __getitem__(self, idx):
            raise RuntimeError("broken indexing")

    rec = _FakeRecord({"nodes": [node]})
    c, mock_conn, _ = _coord_with_mocks([rec])
    mock_conn.fetch = AsyncMock(side_effect=[
        [{"id": 1, "content_chars": 10, "type": "fact", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None}],
        [{"id": 1, "content": "whole pg 1"}],
    ])

    orig_data = rec.data
    def _broken_data():
        d = orig_data()
        d["nodes"] = _BrokenIndexSeq([node])
        return d
    rec.data = _broken_data

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200

