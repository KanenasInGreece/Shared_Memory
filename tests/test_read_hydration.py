"""Tests for read hydration: two-step reads with index entries on graph and on lineage.

Each test catches a specific failure mode across graph, lineage, and search expansion.
Grounded in decision:2862 (retrieval is two steps: an index first, then the agent expands
the records it needs from Postgres).
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
    MemoryCoordinator, ONT, GRAPH_QUERY_ROW_CAP,
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


# ── G1: Graph node content is never replaced or extended ──────────────────────

@pytest.mark.asyncio
async def test_g1_graph_node_content_never_replaced_or_extended_by_postgres_text():
    """F3, F4c: stored text is removed and the whole authoritative text is returned."""
    n = _make_node(["Fact"], {"pg_id": 1, "content": "node copy"})
    c, conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])
    conn.fetch = AsyncMock(return_value=[{"id": 1, "metadata": {}, "visibility": "global", "content": "whole Postgres", "content_chars": 14}])
    response = await c.handle_graph(_graph_request())
    result = json.loads(response.text)["records"][0]["n"]
    assert result["text"] == "whole Postgres"
    assert "content" not in result and "node copy" not in response.text


# ── G2: Whole text, no legacy truncation keys ─────────────────────────────────

@pytest.mark.asyncio
async def test_g2_readable_node_whole_text_without_legacy_keys():
    """F3, F4c: readable nodes carry whole text and no legacy text keys."""
    n_short = _make_node(["Fact"], {"pg_id": 1, "content": "short"}, element_id="1", id_=1)
    n_equal = _make_node(["Fact"], {"pg_id": 2, "content": "equal len"}, element_id="2", id_=2)
    n_long = _make_node(["Fact"], {"pg_id": 3, "content": "longer copy here"}, element_id="3", id_=3)
    n_dec = _make_node(["Decision"], {"pg_id": 4, "title": "ADR 4"}, element_id="4", id_=4)

    records = [
        _FakeRecord({"n": n_short}),
        _FakeRecord({"n": n_equal}),
        _FakeRecord({"n": n_long}),
        _FakeRecord({"n": n_dec}),
    ]
    c, mock_conn, _ = _coord_with_mocks(records)

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content_chars": 100, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
        {"id": 2, "content_chars": 9, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
        {"id": 3, "content_chars": 5, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
        {"id": 4, "content_chars": 46, "type": "decision", "content": "whole Postgres", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    r1 = body["records"][0]["n"]
    r2 = body["records"][1]["n"]
    r3 = body["records"][2]["n"]
    r4 = body["records"][3]["n"]

    assert r1["ref"] == "fact:1"
    assert r1["full_record"]["chars"] == 100
    assert r1["text_is"] == "full_record"
    assert "content_truncated" not in r1

    assert r2["ref"] == "fact:2"
    assert r2["full_record"]["chars"] == 9
    assert r2["text_is"] == "full_record"
    assert "content_truncated" not in r2

    assert r3["ref"] == "fact:3"
    assert r3["full_record"]["chars"] == 5
    assert r3["text_is"] == "full_record"
    assert "content_truncated" not in r3

    assert r4["ref"] == "decision:4"
    assert r4["full_record"]["chars"] == 46
    assert r4["text_is"] == "full_record"
    assert all(r["text"] == "whole Postgres" for r in (r1, r2, r3, r4))
    assert "content_truncated" not in r4


# ── G3: Summary and fact same ID do not swap ref/chars, insight ref ───────────

@pytest.mark.asyncio
async def test_g3_summary_and_fact_nodes_with_same_id_do_not_swap_ref_or_chars_and_insight_ref():
    """G3: A summary node and a fact node with the SAME id get each other's ref or content_chars; an insight summary's ref is insight:N."""
    n_fact = _make_node(["Fact"], {"pg_id": 42, "content": "graph fact copy"}, element_id="1", id_=1)
    n_summary = _make_node(["CommunitySummary"], {"pg_id": 42}, element_id="2", id_=2)
    n_insight = _make_node(["CommunitySummary"], {"pg_id": 70}, element_id="3", id_=3)
    records = [_FakeRecord({"n": n_fact}), _FakeRecord({"n": n_summary}), _FakeRecord({"n": n_insight})]

    c, mock_conn, _ = _coord_with_mocks(records)

    async def _fake_fetch(sql, ids):
        if "technical_docs" in sql and "length(t.content)" in sql:
            return [{"id": 42, "content_chars": 200, "type": "fact", "content": "whole Postgres", "superseded": False,
                     "visibility": "global", "agent_id": None, "scope": None}]
        if "community_summaries" in sql and "length(t.content)" in sql:
            return [
                {"id": 42, "content_chars": 500, "snippet": "summary snippet 42",
                 "superseded": False, "visibility": "global", "metadata": {}},
                {"id": 70, "content_chars": 700, "snippet": "insight snippet 70",
                 "superseded": False, "visibility": "global", "metadata": {"kind": "insight"}},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    r_fact = body["records"][0]["n"]
    r_sum = body["records"][1]["n"]
    r_ins = body["records"][2]["n"]

    assert r_fact["ref"] == "fact:42"
    assert r_fact["full_record"]["chars"] == 200

    assert r_sum["ref"] == "summary:42"
    assert r_sum["full_record"]["chars"] == 500
    assert r_sum["text_is"] == "pointer"
    assert "text" not in r_sum and "snippet" not in r_sum

    assert r_ins["ref"] == "insight:70"
    assert r_ins["full_record"]["chars"] == 700
    assert r_ins["text_is"] == "pointer"
    assert "text" not in r_ins and "snippet" not in r_ins


# ── G4: Unreadable node gains no index keys, owner gets them ──────────────────

@pytest.mark.asyncio
async def test_g4_unreadable_node_gains_no_index_keys_and_owner_is_not_refused():
    """G4: A node the caller cannot read (private non-owner; scope; unauthenticated caller) gains ref, full_record, text_is or text; or a readable node beside it is also withheld; or the owner is refused them."""
    n_priv = _make_node(["Fact"], {"pg_id": 1, "content": "private fact"}, element_id="1", id_=1)
    n_scope = _make_node(["Fact"], {"pg_id": 2, "content": "scope fact"}, element_id="2", id_=2)
    n_readable = _make_node(["Fact"], {"pg_id": 3, "content": "global fact"}, element_id="3", id_=3)

    c, mock_conn, _ = _coord_with_mocks([
        _FakeRecord({"n": n_priv}),
        _FakeRecord({"n": n_scope}),
        _FakeRecord({"n": n_readable}),
    ])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content_chars": 100, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "private", "agent_id": "alice", "scope": "global"},
        {"id": 2, "content_chars": 200, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "scope", "agent_id": "alice", "scope": "ops"},
        {"id": 3, "content_chars": 300, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    # Unauthenticated caller
    resp_anon = await c.handle_graph(_graph_request(agent=None))
    assert resp_anon.status == 200
    b_anon = json.loads(resp_anon.text)["records"]
    for idx in (0, 1):
        for k in ("ref", "content_chars", "content_truncated", "snippet", "content_source"):
            assert k not in b_anon[idx]["n"]
    assert b_anon[2]["n"]["ref"] == "fact:3"
    assert b_anon[2]["n"]["full_record"]["chars"] == 300

    # Non-owner bob
    resp_bob = await c.handle_graph(_graph_request(agent="bob"))
    assert resp_bob.status == 200
    b_bob = json.loads(resp_bob.text)["records"]
    for idx in (0, 1):
        for k in ("ref", "content_chars", "content_truncated", "snippet", "content_source"):
            assert k not in b_bob[idx]["n"]
    assert b_bob[2]["n"]["ref"] == "fact:3"
    assert b_bob[2]["n"]["full_record"]["chars"] == 300

    # Owner alice: gets index keys for n_priv, n_scope withheld (viewer_scope is None on graph)
    resp_alice = await c.handle_graph(_graph_request(agent="alice"))
    assert resp_alice.status == 200
    b_alice = json.loads(resp_alice.text)["records"]
    assert b_alice[0]["n"]["ref"] == "fact:1"
    assert b_alice[0]["n"]["full_record"]["chars"] == 100
    assert b_alice[0]["n"]["text_is"] == "full_record"
    assert "content_truncated" not in b_alice[0]["n"]
    for k in ("ref", "content_chars", "content_truncated", "snippet", "content_source"):
        assert k not in b_alice[1]["n"]
    assert b_alice[2]["n"]["ref"] == "fact:3"


# ── G5: Graph hydration selects whole content and one successor join per table ─

@pytest.mark.asyncio
async def test_g5_graph_hydration_selects_whole_content_and_successors_once_per_table():
    """F3, F17: hydration reads full text and self-joins successors once per table."""
    nodes = [_make_node([label], {"pg_id": i}) for i, label in enumerate(["Fact", "CommunitySummary"], 1)]
    c, conn, _ = _coord_with_mocks([_FakeRecord({"nodes": nodes})])
    response = await c.handle_graph(_graph_request())
    assert response.status == 200
    assert conn.fetch.await_count == 2
    for call in conn.fetch.await_args_list:
        sql = call.args[0]
        assert "t.content" in sql and "LEFT JOIN" in sql
        assert "s.visibility" in sql and "t.visibility" in sql
        assert "left(" not in sql.lower()


# ── G6: CommunitySummary node on graph has snippet and no content key ─────────

@pytest.mark.asyncio
async def test_g6_community_summary_node_on_graph_is_a_pointer_without_stored_text():
    """F5: graph summary nodes are pointers without stored text."""
    n = _make_node(["CommunitySummary"], {"pg_id": 10}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 10, "content_chars": 500, "snippet": "first 120 chars snippet",
         "superseded": False, "visibility": "global", "metadata": {}},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert "content" not in rec
    assert rec["text_is"] == "pointer"
    assert "text" not in rec and "snippet" not in rec
    assert rec["ref"] == "summary:10"
    assert rec["full_record"]["chars"] == 500
    assert "content_truncated" not in rec


# ── G7: Deleted names absent from module, coordinator, and env.example ────────

def test_g7_deleted_names_absent_from_module_coordinator_and_env_example():
    """G7: A deleted name (allocate_full_text, _record_text_whole, READ_FULL_TEXT_BUDGET_CHARS) remains imported, defined on MemoryCoordinator / module, or in .env.example."""
    deleted_names = ("allocate_full_text", "_record_text_whole", "READ_FULL_TEXT_BUDGET_CHARS")
    for name in deleted_names:
        assert not hasattr(coordinator_mod, name), f"{name} is still in coordinator module"
        assert not hasattr(MemoryCoordinator, name), f"{name} is still on MemoryCoordinator"

    env_example_path = os.path.join(os.path.dirname(__file__), "..", "shared-memory", ".env.example")
    with open(env_example_path, "r", encoding="utf-8") as f:
        env_content = f.read()
    for name in deleted_names:
        assert name not in env_content, f"{name} is still in .env.example"


@pytest.mark.asyncio
async def test_resolve_marks_drops_a_partial_row_when_the_cursor_raises():
    """A marks cursor that yields one decision and then raises must not keep that partial row."""

    class _Boom:
        def __init__(self):
            self._n = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self._n += 1
            if self._n == 1:
                return {"decision_id": 1,
                        "outcomes": [{"pg_id": 2, "rating": "validated"}],
                        "grounds": []}
            raise RuntimeError("cursor died")

    c, _, session = _coord_with_mocks()
    session.run = AsyncMock(return_value=_Boom())
    marks = await c.resolve_marks(session, [1], [])
    assert marks.get("failed") is True
    assert marks["decisions"] == {}
    assert marks["retrospectives"] == {}


_LINEAGE_TEXT_KEYS = (
    "content", "alternatives", "confidence", "content_withheld", "content_truncated",
)

_LONG_LINEAGE = (
    "A demo ledger keeps the names in sorted order so two agents cannot deadlock "
    "on the same pair of locks when they acquire them from opposite ends of the list."
)


def _live_edge(entry, anchor):
    """Turn an older direction/label neighbour into a stored-direction edge.

    Compose reads start and end, not direction. The anchor end keeps empty
    labels so an unknown table still counts as this anchor.
    """
    labels = entry.get("labels") or ([entry["label"]] if entry.get("label") else [])
    far = entry.get("pg_id")
    inbound = entry.get("direction") == "in"
    return {
        "rel_type": entry["rel_type"],
        "rel_props": entry.get("rel_props") or {},
        "labels": labels,
        "pg_id": far,
        "start_pg_id": far if inbound else anchor,
        "end_pg_id": anchor if inbound else far,
        "start_labels": labels if inbound else [],
        "end_labels": [] if inbound else labels,
    }


class _EmptyRows:
    """An empty Neo4j result. Marks have nothing to read."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _AsyncRows:
    """Async iterator of dict rows, the shape session.run() yields."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._rows):
            raise StopAsyncIteration
        row = self._rows[self._i]
        self._i += 1
        return row


def _doc_status_row(pg_id, rtype, text, **extra):
    row = {
        "id": pg_id, "type": rtype, "created_at": None, "superseded": False,
        "superseded_by": None, "grounded_in": None, "supersession_ack": None,
        "content_chars": len(text), "snippet": text[:120],
        "visibility": "global", "agent_id": None, "scope": None,
    }
    row.update(extra)
    return row


def _summary_status_row(pg_id, text, kind="thematic", **extra):
    row = {
        "id": pg_id, "metadata": json.dumps({"kind": kind}), "source_pg_ids": [],
        "created_at": None, "superseded": False, "superseded_reason": None,
        "superseded_by": None, "run_id": 1,
        "content_chars": len(text), "snippet": text[:120],
        "visibility": "global", "agent_id": None, "scope": None,
    }
    row.update(extra)
    return row


def _assert_no_lineage_text_keys(body):
    for key in _LINEAGE_TEXT_KEYS:
        assert key not in body


# ── L1: lineage replies carry no record text ─────────────────────────────────

@pytest.mark.asyncio
async def test_l1_lineage_replies_omit_record_text_keys():
    """L1: A lineage reply contains content, alternatives, confidence, content_withheld, or content_truncated."""
    cases = [
        ("fact:14", [_doc_status_row(14, "fact", _LONG_LINEAGE), None], False),
        ("decision:15", [_doc_status_row(15, "decision", _LONG_LINEAGE), None], False),
        ("retrospective:16", [_doc_status_row(16, "retrospective", _LONG_LINEAGE), None], False),
        ("summary:17", [_summary_status_row(17, _LONG_LINEAGE)], True),
        ("summary:18", [_summary_status_row(
            18, _LONG_LINEAGE, superseded=True, superseded_by=20,
            superseded_reason="lineage")], True),
    ]
    for ref, rows, summary in cases:
        c, mock_conn, _ = _coord_with_mocks()
        mock_conn.fetch = AsyncMock(return_value=[])
        if ref == "summary:18":
            async def _fetch_successor(sql, *args):
                if "content_chars" in sql and "LEFT JOIN" in sql:
                    return [{
                        "id": 18, "content": _LONG_LINEAGE,
                        "content_chars": len(_LONG_LINEAGE),
                        "metadata": {"kind": "thematic"}, "superseded": True,
                        "superseded_by": 20, "visibility": "global",
                        "agent_id": "", "scope": "global",
                        "successor_id": 20,
                        "successor_metadata": {"kind": "thematic"},
                        "successor_visibility": "global",
                        "successor_agent_id": "", "successor_scope": "global",
                    }]
                return []
            mock_conn.fetch = AsyncMock(side_effect=_fetch_successor)
        if summary:
            mock_conn.fetchrow = AsyncMock(return_value=rows[0])
        else:
            mock_conn.fetchrow = AsyncMock(side_effect=rows)
        resp = await c.handle_status(_status_request(ref))
        assert resp.status == 200, ref
        body = json.loads(resp.text)
        _assert_no_lineage_text_keys(body)
        if ref == "summary:18":
            assert body["superseded"] is True
            assert body["superseded_by"] == "summary:20"


# ── L2: status SQL never selects the whole text column ───────────────────────

@pytest.mark.asyncio
async def test_l2_status_sql_selects_length_and_left_only():
    """L2: Status SQL selects the text column other than as length(content) or left(content, 120)."""
    c, mock_conn, _ = _coord_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(8, "fact", _LONG_LINEAGE), None,
    ])
    await c.handle_status(_status_request("fact:8"))

    c2, mock_conn2, _ = _coord_with_mocks()
    mock_conn2.fetchrow = AsyncMock(return_value=_summary_status_row(9, _LONG_LINEAGE))
    await c2.handle_status(_status_request("summary:9"))

    sqls = [mock_conn.fetchrow.await_args_list[0].args[0],
            mock_conn2.fetchrow.await_args.args[0]]
    assert len(sqls) == 2
    # A wider cut is still the text column. The strip must not hide it.
    for sql in sqls:
        cleaned = (
            sql.replace("length(content)", "")
            .replace("left(content, 120)", "")
            .replace("content_chars", "")
        )
        assert "content" not in cleaned.lower(), sql


# ── L3: readable records carry snippet and content_chars; unreadable do not ─

def _lineage_text_row(pid, text, **extra):
    row = {
        "id": pid, "content": text, "content_chars": len(text),
        "metadata": {"type": "fact"}, "type": "fact", "superseded": False,
        "visibility": "global", "agent_id": None, "scope": None,
    }
    row.update(extra)
    return row


@pytest.mark.asyncio
async def test_l3_snippet_and_content_chars_follow_visibility():
    """L3: A readable lineage lacks the whole text, or an unreadable record is not the missing-record answer."""
    text = _LONG_LINEAGE
    assert len(text) > 120

    def _fetch_for(pid, owner=None):
        async def _fetch(sql, *args):
            if "content_chars" in sql and "LEFT JOIN" in sql:
                extra = {}
                if owner:
                    extra = {"visibility": "private", "agent_id": owner}
                return [_lineage_text_row(pid, text, **extra)]
            return []
        return _fetch

    c, mock_conn, _ = _coord_with_mocks()
    mock_conn.fetch = AsyncMock(side_effect=_fetch_for(21))
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(21, "fact", text), None,
    ])
    resp = await c.handle_status(_status_request("fact:21"))
    body = json.loads(resp.text)
    assert body["text"] == text
    assert body["text_is"] == "full_record"
    assert body["full_record"]["chars"] == len(text)
    assert body["full_record"]["read_with"] == "search --ref fact:21"
    assert "snippet" not in body
    assert "content_chars" not in body

    c_owner, conn_owner, _ = _coord_with_mocks()
    conn_owner.fetch = AsyncMock(side_effect=_fetch_for(22, owner="alice"))
    conn_owner.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(22, "fact", text, visibility="private", agent_id="alice"),
        None,
    ])
    owned = json.loads((await c_owner.handle_status(
        _status_request("fact:22", agent="alice"))).text)
    assert owned["text"] == text
    assert owned["text_is"] == "full_record"
    assert "snippet" not in owned

    withheld = [
        ("fact:23", "bob", _doc_status_row(
            23, "fact", text, visibility="private", agent_id="alice")),
        ("fact:24", "alice", _doc_status_row(
            24, "fact", text, visibility="scope", agent_id="alice", scope="ops")),
    ]
    for ref, agent, row in withheld:
        c_w, conn_w, session_w = _coord_with_mocks()
        conn_w.fetch = AsyncMock(return_value=[])
        conn_w.fetchrow = AsyncMock(side_effect=[row, None])
        resp_w = await c_w.handle_status(_status_request(ref, agent=agent))
        hidden = json.loads(resp_w.text)
        assert hidden == {
            "pg_id": int(ref.split(":")[1]), "exists": False, "neo4j": "unknown"}
        assert text not in json.dumps(hidden)
        assert session_w.run.await_count == 0

    c_sum, conn_sum, _ = _coord_with_mocks()
    conn_sum.fetchrow = AsyncMock(return_value=_summary_status_row(
        25, text, visibility="private", agent_id="alice"))
    resp_sum = await c_sum.handle_status(_status_request("summary:25", agent="bob"))
    assert resp_sum.status == 404
    hidden_sum = json.loads(resp_sum.text)
    assert hidden_sum["exists"] is False
    assert hidden_sum["record_type"] == "summary"
    assert "content_chars" not in hidden_sum
    assert "snippet" not in hidden_sum
    assert text not in json.dumps(hidden_sum)


# ── L5: related names the direction of each direct neighbour ─────────────────

@pytest.mark.asyncio
async def test_l5_related_direction_matches_who_points_at_whom():
    """L5: related reverses from/to, or still names direction with dir."""
    fact_snippet = "Names are sorted before the lock is taken in the demo ledger."
    decision_snippet = "Ship the sorted lock order for the demo ledger."

    async def _lineage(ref, neighbours, index_rows):
        c, mock_conn, session = _coord_with_mocks()
        mock_conn.fetchrow = AsyncMock(side_effect=[
            _doc_status_row(5 if ref.startswith("fact") else 9,
                            "fact" if ref.startswith("fact") else "decision",
                            _LONG_LINEAGE),
            None,
        ])

        async def _fetch(sql, *args):
            if "community_summaries" in sql and "LEFT JOIN" not in sql:
                return []
            if "content_chars" in sql and "LEFT JOIN" in sql:
                return index_rows
            return []

        mock_conn.fetch = AsyncMock(side_effect=_fetch)
        session.run = AsyncMock(return_value=_AsyncRows(neighbours))
        resp = await c.handle_status(_status_request(ref))
        assert resp.status == 200
        return json.loads(resp.text), session

    fact_body, _ = await _lineage(
        "fact:5",
        [{
            "rel": "GROUNDED_IN", "outward": False, "labels": ["Decision"],
            "pg_id": 9, "snippet": decision_snippet,
        }],
        [{"id": 9, "content_chars": 48, "type": "decision", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None}],
    )
    assert fact_body["related"][0]["relation"]["from"] == "decision:9"
    assert fact_body["related"][0]["relation"]["to"] == "fact:5"
    assert fact_body["related"][0]["relation"]["relation"] == "GROUNDED_IN"
    assert fact_body["related"][0]["record"]["ref"] == "decision:9"
    assert "dir" not in fact_body["related"][0]
    assert decision_snippet not in json.dumps(fact_body["related"])

    decision_body, _ = await _lineage(
        "decision:9",
        [{
            "rel": "GROUNDED_IN", "outward": True, "labels": ["Fact"],
            "pg_id": 5, "snippet": fact_snippet,
        }],
        [{"id": 5, "content_chars": 60, "type": "fact", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None}],
    )
    assert decision_body["related"][0]["relation"]["from"] == "decision:9"
    assert decision_body["related"][0]["relation"]["to"] == "fact:5"
    assert decision_body["related"][0]["relation"]["relation"] == "GROUNDED_IN"
    assert decision_body["related"][0]["record"]["ref"] == "fact:5"
    assert "dir" not in decision_body["related"][0]
    assert fact_snippet not in json.dumps(decision_body["related"])


# ── L6: related drops summaries, entities, and unreadable neighbours ─────────

@pytest.mark.asyncio
async def test_l6_related_omits_summaries_entities_and_unreadable_neighbours():
    """L6: related names a CommunitySummary, an Entity, or an unreadable neighbour, or drops that neighbour's relation."""
    secret = "SECRET-NEIGHBOUR-SNIPPET-must-not-leak"
    c, mock_conn, session = _coord_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(5, "fact", _LONG_LINEAGE), None,
    ])

    async def _fetch(sql, *args):
        if "community_summaries" in sql and "source_pg_ids" in sql:
            return []
        if "content_chars" in sql and "LEFT JOIN" in sql:
            return [
                {"id": 9, "content": "visible decision", "content_chars": 16,
                 "type": "decision", "metadata": {"type": "decision"},
                 "superseded": False, "visibility": "global",
                 "agent_id": None, "scope": None},
                {"id": 4, "content": secret, "content_chars": len(secret),
                 "type": "fact", "metadata": {"type": "fact"},
                 "superseded": False, "visibility": "private",
                 "agent_id": "alice", "scope": "global"},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch)
    session.run = AsyncMock(return_value=_AsyncRows([
        {"rel": "GROUNDED_IN", "outward": False, "labels": ["Decision"],
         "pg_id": 9, "snippet": "visible decision"},
        {"rel": "MENTIONS", "outward": True, "labels": ["CommunitySummary"],
         "pg_id": 3, "snippet": "summary neighbour"},
        {"rel": "MENTIONS", "outward": True, "labels": ["Entity"],
         "pg_id": 2, "snippet": "entity neighbour"},
        {"rel": "REFERENCES", "outward": True, "labels": ["Fact"],
         "pg_id": 4, "snippet": secret},
    ]))
    resp = await c.handle_status(_status_request("fact:5", agent="bob"))
    body = json.loads(resp.text)
    named = [item for item in body["related"] if item.get("record")]
    assert [item["record"]["ref"] for item in named] == ["decision:9"]
    assert named[0]["relation"]["from"] == "decision:9"
    assert named[0]["relation"]["to"] == "fact:5"
    unnamed = [item for item in body["related"] if "record" not in item]
    assert len(unnamed) == 1
    assert unnamed[0]["relation"]["to"] is None
    assert secret not in resp.text
    assert "fact:4" not in resp.text
    cypher = next(
        call.args[0] for call in session.run.await_args_list
        if "OPTIONAL MATCH" in call.args[0])
    assert "CommunitySummary" in cypher
    assert "NOT m:" in cypher
    assert "startNode(r).pg_id" in cypher
    assert "ORDER BY" in cypher
    assert cypher.index("ORDER BY") < cypher.index("LIMIT")


# ── L7: a down graph does not fail lineage ───────────────────────────────────

@pytest.mark.asyncio
async def test_l7_neo4j_down_keeps_the_postgres_lineage():
    """L7: A Neo4j failure fails lineage instead of a 200 reply with the Postgres fields and no related key."""
    c, mock_conn, _ = _coord_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(5, "fact", _LONG_LINEAGE, superseded=True, superseded_by=9),
        None,
    ])

    async def _fetch(sql, *args):
        if "content_chars" in sql and "LEFT JOIN" in sql:
            return [{
                "id": 5, "content": _LONG_LINEAGE, "content_chars": len(_LONG_LINEAGE),
                "metadata": {"type": "fact"}, "type": "fact", "superseded": True,
                "superseded_by": 9, "visibility": "global", "agent_id": "",
                "scope": "global", "successor_id": 9,
                "successor_metadata": {"type": "fact"},
                "successor_visibility": "global", "successor_agent_id": "",
                "successor_scope": "global",
            }]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch)
    c._neo4j.session = MagicMock(side_effect=RuntimeError("graph down"))
    resp = await c.handle_status(_status_request("fact:5"))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["pg_id"] == 5
    assert body["ref"] == "fact:5"
    assert body["exists"] is True
    assert body["superseded"] is True
    assert body["superseded_by"] == "fact:9"
    assert body["text"] == _LONG_LINEAGE
    assert body["text_is"] == "full_record"
    assert "snippet" not in body
    assert "content_chars" not in body
    assert "related" not in body
    _assert_no_lineage_text_keys(body)


# ── L8: the neighbour cap, and no related on a refusal ───────────────────────

@pytest.mark.asyncio
async def test_l8_related_cap_and_refusals_omit_related(monkeypatch):
    """L8: More neighbours than LINEAGE_RELATED_CAP are all returned, or related_more is missing; an unreadable record, the 404, or exists:false carries related."""
    monkeypatch.setattr(coordinator_mod, "LINEAGE_RELATED_CAP", 2)
    neighbours = [
        {"rel": "REFERENCES", "outward": True, "labels": ["Fact"],
         "pg_id": 11, "snippet": "first neighbour"},
        {"rel": "REFERENCES", "outward": True, "labels": ["Fact"],
         "pg_id": 12, "snippet": "second neighbour"},
        {"rel": "REFERENCES", "outward": True, "labels": ["Fact"],
         "pg_id": 13, "snippet": "third neighbour"},
    ]
    c, mock_conn, session = _coord_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(5, "fact", _LONG_LINEAGE), None,
    ])

    async def _fetch(sql, *args):
        if "content_chars" in sql and "LEFT JOIN" in sql:
            return [
                {"id": 11, "content": "one", "content_chars": 3, "type": "fact",
                 "metadata": {"type": "fact"}, "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
                {"id": 12, "content": "two", "content_chars": 3, "type": "fact",
                 "metadata": {"type": "fact"}, "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
                {"id": 13, "content": "three", "content_chars": 5, "type": "fact",
                 "metadata": {"type": "fact"}, "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch)
    session.run = AsyncMock(return_value=_AsyncRows(neighbours))
    body = json.loads((await c.handle_status(_status_request("fact:5"))).text)
    assert body["related_more"] is True
    assert [item["record"]["ref"] for item in body["related"]] == ["fact:11", "fact:12"]

    c_priv, conn_priv, session_priv = _coord_with_mocks()
    conn_priv.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(6, "fact", _LONG_LINEAGE, visibility="private", agent_id="alice"),
        None,
    ])
    conn_priv.fetch = AsyncMock(return_value=[])
    hidden = json.loads((await c_priv.handle_status(
        _status_request("fact:6", agent="bob"))).text)
    assert "related" not in hidden
    assert session_priv.run.await_count == 0

    c_miss, conn_miss, _ = _coord_with_mocks()
    conn_miss.fetchrow = AsyncMock(side_effect=[None, None])
    missing = json.loads((await c_miss.handle_status(_status_request("fact:9999"))).text)
    assert missing["exists"] is False
    assert "related" not in missing

    c_404, conn_404, _ = _coord_with_mocks()
    conn_404.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(12, "decision", _LONG_LINEAGE), None,
    ])
    wrong = json.loads((await c_404.handle_status(_status_request("fact:12"))).text)
    assert "related" not in wrong


@pytest.mark.asyncio
async def test_l9_a_related_neighbour_with_no_postgres_row_is_left_out():
    """L9: A graph neighbour with no technical_docs row loses its relation, or the reply names that end."""
    c, mock_conn, session = _coord_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(5, "fact", _LONG_LINEAGE), None,
    ])

    async def _fetch(sql, *args):
        if "content_chars" in sql and "LEFT JOIN" in sql:
            return [
                {"id": 9, "content": "visible decision", "content_chars": 16,
                 "type": "decision", "metadata": {"type": "decision"},
                 "superseded": False, "visibility": "global",
                 "agent_id": None, "scope": None},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch)
    session.run = AsyncMock(return_value=_AsyncRows([
        {"rel": "GROUNDED_IN", "outward": False, "labels": ["Decision"],
         "pg_id": 9, "snippet": "visible decision"},
        {"rel": "REFERENCES", "outward": True, "labels": ["Fact"],
         "pg_id": 4, "snippet": "no postgres row"},
    ]))
    body = json.loads((await c.handle_status(_status_request("fact:5"))).text)
    assert [item["record"]["ref"] for item in body["related"] if "record" in item] == ["decision:9"]
    assert any(item["relation"]["to"] is None for item in body["related"])
    assert "fact:4" not in json.dumps(body["related"])


# ── F3: Node preserves content and gains chars/ref, no content_source ─────────

@pytest.mark.asyncio
async def test_f3_unchosen_node_preserves_content_and_gains_source_chars_ref():
    """F3: unchosen graph nodes still receive authoritative text."""
    n1 = _make_node(["Fact"], {"pg_id": 10, "content": "original graph content"}, element_id="1", id_=1)
    records = [_FakeRecord({"n": n1})]

    c, mock_conn, _ = _coord_with_mocks(records)

    async def _fake_fetch(sql, ids):
        if "length(t.content)" in sql:
            return [{"id": 10, "content_chars": 50000, "type": "fact", "content": "whole Postgres", "superseded": False,
                     "visibility": "global", "agent_id": None, "scope": None}]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    rec = body["records"][0]["n"]
    assert rec["text"] == "whole Postgres"
    assert "content" not in rec
    assert rec["full_record"]["chars"] == 50000
    assert rec["text_is"] == "full_record"
    assert "content_truncated" not in rec
    assert rec["ref"] == "fact:10"
    assert "content_source" not in rec


# ── F4: Private record visibility on lineage and graph ────────────────────────

@pytest.mark.asyncio
async def test_f4_private_record_withheld_from_non_owner_on_lineage_and_graph():
    """F4: A private record's text reaches a non-owner on lineage or on graph; or the owner is refused it."""
    c, mock_conn, _ = _coord_with_mocks()

    private_text = "secret private postgres fact for the demo ledger and nothing else here"
    rec_row = {
        "id": 55, "type": "fact", "created_at": None, "superseded": False,
        "superseded_by": None, "grounded_in": None, "supersession_ack": None,
        "content_chars": len(private_text), "snippet": private_text[:120],
        "visibility": "private", "agent_id": "alice", "scope": "global",
    }
    async def _fetch_owner(sql, *args):
        if "content_chars" in sql and "LEFT JOIN" in sql:
            return [{
                "id": 55, "content": private_text, "content_chars": len(private_text),
                "metadata": {"type": "fact"}, "type": "fact", "superseded": False,
                "visibility": "private", "agent_id": "alice", "scope": "global",
            }]
        return []

    mock_conn.fetchrow = AsyncMock(side_effect=[rec_row, None])
    mock_conn.fetch = AsyncMock(side_effect=_fetch_owner)

    # 1. Non-owner bob on lineage
    req_bob = _status_request("fact:55", agent="bob")
    resp_bob = await c.handle_status(req_bob)
    assert resp_bob.status == 200
    b_bob = json.loads(resp_bob.text)
    assert "content" not in b_bob
    assert "content_withheld" not in b_bob
    assert "content_chars" not in b_bob
    assert "snippet" not in b_bob
    assert "related" not in b_bob
    assert private_text not in resp_bob.text

    # 2. Owner alice on lineage
    mock_conn.fetchrow = AsyncMock(side_effect=[rec_row, None])
    req_alice = _status_request("fact:55", agent="alice")
    resp_alice = await c.handle_status(req_alice)
    assert resp_alice.status == 200
    b_alice = json.loads(resp_alice.text)
    assert "content" not in b_alice
    assert b_alice["text"] == private_text
    assert b_alice["text_is"] == "full_record"
    assert b_alice["full_record"]["chars"] == len(private_text)
    assert "snippet" not in b_alice
    assert "content_chars" not in b_alice

    # 3. Non-owner bob on graph
    n_priv = _make_node(["Fact"], {"pg_id": 55, "content": "graph copy"}, element_id="1", id_=1)
    c_graph, mock_conn_g, _ = _coord_with_mocks([_FakeRecord({"n": n_priv})])
    async def _fake_fetch_priv(sql, ids):
        if "length(t.content)" in sql:
            return [{"id": 55, "content_chars": 28, "type": "fact", "content": "whole Postgres", "superseded": False,
                     "visibility": "private", "agent_id": "alice", "scope": "global"}]
        return []
    mock_conn_g.fetch = AsyncMock(side_effect=_fake_fetch_priv)

    resp_g_bob = await c_graph.handle_graph(_graph_request(agent="bob"))
    assert resp_g_bob.status == 200
    rec_g_bob = json.loads(resp_g_bob.text)["records"][0]["n"]
    assert rec_g_bob == {"label": "Fact"}
    assert "content_source" not in rec_g_bob
    assert "content_chars" not in rec_g_bob
    assert "ref" not in rec_g_bob
    assert "content_truncated" not in rec_g_bob
    assert private_text not in resp_g_bob.text

    # 4. Owner alice on graph
    resp_g_alice = await c_graph.handle_graph(_graph_request(agent="alice"))
    assert resp_g_alice.status == 200
    rec_g_alice = json.loads(resp_g_alice.text)["records"][0]["n"]
    assert rec_g_alice["text"] == "whole Postgres"
    assert "content" not in rec_g_alice
    assert "content_source" not in rec_g_alice
    assert rec_g_alice["ref"] == "fact:55"
    assert rec_g_alice["full_record"]["chars"] == 28
    assert rec_g_alice["text_is"] == "full_record"
    assert "content_truncated" not in rec_g_alice
    assert "graph copy" not in resp_g_alice.text


# ── F5: Fetch error or TimeoutError degrades gracefully ───────────────────────

@pytest.mark.asyncio
async def test_f5_fetch_error_or_timeout_degrades_gracefully_on_graph_and_search():
    """F4a: database failures redact nodes instead of exposing graph text."""
    n1 = _make_node(["Fact"], {"pg_id": 77, "content": "graph content intact"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1})])
    c._acquire = MagicMock(side_effect=asyncio.TimeoutError("pool acquire timeout"))

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    rec = body["records"][0]["n"]
    assert rec == {"label": "Fact"}
    assert "content_source" not in rec
    assert "ref" not in rec
    assert "content_chars" not in rec
    assert "content_truncated" not in rec


@pytest.mark.asyncio
async def test_f5_fetch_runtime_error_degrades_gracefully_with_warning(caplog):
    """F4a: database failures redact nodes and log one warning."""
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
    assert rec == {"label": "Fact"}
    assert "content_source" not in rec
    assert "ref" not in rec
    assert "content_chars" not in rec
    assert "content_truncated" not in rec

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "record hydration failed" in warnings[0].message

    ctx_entries = [{"pg_id": 77, "label": ONT.fact, "rel_type": "MENTIONS",
                    "direction": "out", "snippet": "cypher snippet"}]
    c_search, _, _ = _coord_with_mocks()
    c_search._acquire = MagicMock(side_effect=asyncio.TimeoutError("timeout"))
    hit = {"pg_id": 1, "record_type": "fact", "ref": "fact:1", "metadata": {},
           "graph_context": [_live_edge(ctx_entries[0], 1)]}
    marks = MagicMock()
    marks.run = AsyncMock(return_value=_EmptyRows())
    await c_search._compose_search_hits(
        [hit], ranked=True, viewer="agent", viewer_scope=None, session=marks)
    assert hit["graph_context"] == []
    assert "cypher snippet" not in json.dumps(hit)


# ── F6: Non-eligible graph types untouched, list hydrated ─────────────────────

@pytest.mark.asyncio
async def test_f6_scalar_projection_entity_node_untouched_and_list_hydrated():
    """F19: projections stay intact while nodes in lists are hydrated."""
    n = _make_node(["Fact"], {"pg_id": 7, "content": "node copy"})
    entity = _make_node(["Entity"], {"name": "Postgres"})
    rel = _make_rel("MENTIONS", {"weight": 1}, n, entity)
    c, conn, _ = _coord_with_mocks([_FakeRecord({"scalar": "projected", "ent": entity,
        "m": {"n": n}, "nlist": [n], "rel": rel, "path": _make_path(n, rel)})])
    conn.fetch = AsyncMock(return_value=[{"id": 7, "metadata": {}, "visibility": "global", "content": "whole", "content_chars": 5}])
    response = await c.handle_graph(_graph_request())
    row = json.loads(response.text)["records"][0]
    assert row["scalar"] == "projected"
    assert row["ent"] == {"label": "Entity", "name": "Postgres"}
    assert row["m"]["n"] == row["nlist"][0] == row["path"]["nodes"][0]
    assert row["m"]["n"]["text"] == "whole" and "content" not in row["m"]["n"]
    assert row["rel"] == {"from": "fact:7", "relation": "MENTIONS", "to": row["ent"], "props": {"weight": 1}}
    assert row["path"]["relationships"] == [row["rel"]]


# ── F8: Summary neighbour with null/missing/retired snippet handling ──────────

@pytest.mark.asyncio
async def test_f8_summary_neighbor_removal_and_snippet_replacement():
    """F8: A missing summary stays a null end, a retired one is dropped on ranked search, and a live one is a pointer. The fact text is the Postgres row."""
    c, mock_conn, _ = _coord_with_mocks()
    entries = [
        {"pg_id": 1, "label": ONT.community_summary, "rel_type": "SUMMARIZED_BY",
         "direction": "out", "snippet": None},
        {"pg_id": 2, "label": ONT.community_summary, "rel_type": "SUMMARIZED_BY",
         "direction": "out", "snippet": "old Cypher snippet"},
        {"pg_id": 3, "label": ONT.community_summary, "rel_type": "SUMMARIZED_BY",
         "direction": "out", "snippet": "old Cypher snippet"},
        {"pg_id": 4, "label": ONT.fact, "rel_type": "MENTIONS",
         "direction": "out", "snippet": "fact snippet"},
    ]
    anchor_map = {100: entries}

    async def _fake_fetch(sql, ids):
        if "community_summaries" in sql:
            return [
                {"id": 2, "content": "retired body", "content_chars": 80,
                 "superseded": True, "metadata": {"kind": "thematic"},
                 "visibility": "global", "agent_id": None, "scope": "global"},
                {"id": 3, "content": "authoritative Postgres text 3", "content_chars": 120,
                 "superseded": False, "metadata": {"kind": "thematic"},
                 "visibility": "global", "agent_id": None, "scope": "global"},
            ]
        if "technical_docs" in sql:
            return [
                {"id": 4, "content": "postgres fact text", "content_chars": 95,
                 "metadata": {"type": "fact"}, "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": "global"},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    hit = {"pg_id": 100, "record_type": "fact", "ref": "fact:100",
           "metadata": {}, "graph_context": [_live_edge(e, 100) for e in entries]}
    marks = MagicMock()
    marks.run = AsyncMock(return_value=_EmptyRows())
    await c._compose_search_hits(
        [hit], ranked=True, viewer="agent", viewer_scope=None, session=marks)
    kept = hit["graph_context"]
    assert len(kept) == 3
    assert kept[0]["relation"]["to"] is None
    assert "record" not in kept[0]
    assert kept[1]["record"]["ref"] == "summary:3"
    assert kept[1]["record"]["text_is"] == "pointer"
    assert "text" not in kept[1]["record"]
    assert kept[1]["record"]["full_record"]["chars"] == 120
    assert kept[2]["record"]["ref"] == "fact:4"
    assert kept[2]["record"]["text"] == "postgres fact text"
    assert "fact snippet" not in json.dumps(kept)
    assert "old Cypher snippet" not in json.dumps(kept)


@pytest.mark.asyncio
async def test_f8_expand_graph_context_single_anchor_removes_retired_summary():
    """F8: ranked compose drops a superseded summary neighbour and keeps the following fact."""
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
                {"id": 2, "content": "retired body", "content_chars": 80,
                 "superseded": True, "metadata": {"kind": "thematic"},
                 "visibility": "global", "agent_id": None, "scope": "global"},
            ]
        if "technical_docs" in sql:
            return [
                {"id": 4, "content": "postgres fact text", "content_chars": 95,
                 "metadata": {"type": "fact"}, "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": "global"},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    hit = {"pg_id": 100, "record_type": "fact", "ref": "fact:100", "metadata": {},
           "graph_context": [_live_edge(rec_sum, 100), _live_edge(rec_fact, 100)]}
    marks = MagicMock()
    marks.run = AsyncMock(return_value=_EmptyRows())
    await c._compose_search_hits(
        [hit], ranked=True, viewer="agent", viewer_scope=None, session=marks)
    res = hit["graph_context"]
    assert len(res) == 1
    assert res[0]["record"]["ref"] == "fact:4"
    assert res[0]["record"]["text"] == "postgres fact text"
    assert res[0]["record"]["full_record"]["chars"] == 95
    assert "old Cypher snippet" not in json.dumps(res)


# ── F9: Wrong-type 404 or exists: false reply omits text keys ─────────────────

@pytest.mark.asyncio
async def test_f9_lineage_refusal_and_missing_record_omit_text_keys():
    """F9: The wrong-type 404 or the exists: false reply from lineage carries text keys."""
    c, mock_conn, _ = _coord_with_mocks()

    forbidden = (
        "content", "content_chars", "snippet", "content_truncated",
        "content_withheld", "alternatives", "confidence", "related",
    )

    # 1. exists: false
    mock_conn.fetchrow = AsyncMock(side_effect=[None, None])
    resp_missing = await c.handle_status(_status_request("fact:9999"))
    assert resp_missing.status == 200
    b_missing = json.loads(resp_missing.text)
    assert b_missing["exists"] is False
    for k in forbidden:
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
    for k in forbidden:
        assert k not in b_wrong

    # 3. missing summary:N (exists: false)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    resp_sum_missing = await c.handle_status(_status_request("summary:9999"))
    assert resp_sum_missing.status == 404
    b_sum_missing = json.loads(resp_sum_missing.text)
    assert b_sum_missing["exists"] is False
    for k in forbidden:
        assert k not in b_sum_missing

    # 4. summary-versus-insight wrong-type 404
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
    for k in forbidden:
        assert k not in b_sum_wrong


# ── F11: Same record in multiple rows both get index keys ─────────────────────

@pytest.mark.asyncio
async def test_f11_same_node_in_multiple_rows_both_gain_index_keys():
    """F19: repeated node values are hydrated in every row."""
    node_a = _make_node(["Fact"], {"pg_id": 5, "content": "graph 5"}, element_id="1", id_=1)
    node_b = _make_node(["Fact"], {"pg_id": 5, "content": "graph 5"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": node_a}), _FakeRecord({"n": node_b})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 5, "content_chars": 10000, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    r0 = body["records"][0]["n"]
    r1 = body["records"][1]["n"]
    assert "content_source" not in r0
    assert "content_source" not in r1
    assert r0["text"] == "whole Postgres"
    assert "content" not in r0
    assert r1["text"] == "whole Postgres"
    assert "content" not in r1
    assert r0["ref"] == "fact:5"
    assert r1["ref"] == "fact:5"
    assert r0["full_record"]["chars"] == 10000
    assert r1["full_record"]["chars"] == 10000
    assert r0["text_is"] == "full_record"
    assert "content_truncated" not in r0
    assert r1["text_is"] == "full_record"
    assert "content_truncated" not in r1


# ── F14: Superseded nodes hydrated on graph ───────────────────────────────────

@pytest.mark.asyncio
async def test_f14_superseded_node_is_hydrated_on_graph():
    """F4b: retired fact nodes remain readable and marked."""
    n = _make_node(["Fact"], {"pg_id": 9, "content": "graph copy"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 9, "content_chars": 30, "type": "fact", "content": "whole Postgres", "superseded": True,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["text"] == "whole Postgres"
    assert "content" not in rec
    assert rec["obsolete"] == "superseded"
    assert rec["superseded"] is True
    assert "content_source" not in rec
    assert rec["ref"] == "fact:9"
    assert rec["full_record"]["chars"] == 30
    assert rec["text_is"] == "full_record"
    assert "content_truncated" not in rec


@pytest.mark.asyncio
async def test_f14_superseded_summary_node_is_hydrated_on_graph():
    """F4b: retired summary nodes remain marked pointers."""
    n_sum = _make_node(["CommunitySummary"], {"pg_id": 99}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n_sum})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 99, "content_chars": 38, "snippet": "snip", "superseded": True, "visibility": "global", "metadata": {}},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert "content" not in rec
    assert rec["obsolete"] == "superseded"
    assert rec["superseded"] is True
    assert "content_source" not in rec
    assert rec["ref"] == "summary:99"
    assert rec["full_record"]["chars"] == 38
    assert rec["text_is"] == "pointer"
    assert "content_truncated" not in rec
    assert "text" not in rec and "snippet" not in rec


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
    assert "content" not in b
    assert "content_withheld" not in b
    assert "content_chars" not in b
    assert "snippet" not in b
    assert "related" not in b
    assert "alternatives" not in b
    assert "confidence" not in b
    assert scope_secret not in resp.text

    # 2. Graph with scope visibility
    n = _make_node(["Fact"], {"pg_id": 15, "content": "graph copy"}, element_id="1", id_=1)
    c_g, mock_conn_g, _ = _coord_with_mocks([_FakeRecord({"n": n})])
    mock_conn_g.fetch = AsyncMock(return_value=[
        {"id": 15, "content_chars": len(scope_secret), "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "scope", "agent_id": "alice", "scope": "ops"},
    ])

    resp_g = await c_g.handle_graph(_graph_request(agent="alice"))
    assert resp_g.status == 200
    rec_g = json.loads(resp_g.text)["records"][0]["n"]
    assert rec_g == {"label": "Fact"}
    assert "content_source" not in rec_g
    assert "content_chars" not in rec_g
    assert "ref" not in rec_g
    assert "content_truncated" not in rec_g
    assert scope_secret not in resp_g.text


# ── F16: Private unreadable record earlier in walk gains nothing ───────────────

@pytest.mark.asyncio
async def test_f16_private_unreadable_record_spends_no_budget():
    """F19: private nodes cannot prevent readable nodes from hydrating."""
    n1 = _make_node(["Fact"], {"pg_id": 101, "content": "graph 101"}, element_id="1", id_=1)
    n2 = _make_node(["Fact"], {"pg_id": 102, "content": "graph 102"}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1}), _FakeRecord({"n": n2})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 101, "content_chars": 15000, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "private", "agent_id": "other_agent", "scope": "global"},
        {"id": 102, "content_chars": 5000, "type": "fact", "content": "whole Postgres", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request(agent="caller_agent"))
    assert resp.status == 200
    body = json.loads(resp.text)
    r1 = body["records"][0]["n"]
    r2 = body["records"][1]["n"]

    assert r1 == {"label": "Fact"}
    assert "content_source" not in r1
    assert "content_chars" not in r1
    assert "ref" not in r1
    assert "content_truncated" not in r1

    assert r2["text"] == "whole Postgres"
    assert "content" not in r2
    assert "content_source" not in r2
    assert r2["ref"] == "fact:102"
    assert r2["full_record"]["chars"] == 5000
    assert r2["text_is"] == "full_record"
    assert "content_truncated" not in r2


# ── F19: Keys past the old distinct cut are hydrated ──────────────────────────

@pytest.mark.asyncio
async def test_f19_keys_past_old_cut_are_all_hydrated(monkeypatch):
    """F19: every distinct node is hydrated beyond the old key cut."""
    monkeypatch.setattr(coordinator_mod, "GRAPH_QUERY_ROW_CAP", 10)
    nodes = [_make_node(["Fact"], {"pg_id": i, "content": "cut"}) for i in range(1, 13)]
    c, conn, _ = _coord_with_mocks([_FakeRecord({"nodes": nodes})])
    conn.fetch = AsyncMock(return_value=[{"id": i, "metadata": {}, "visibility": "global", "content": f"whole {i}", "content_chars": 8} for i in range(1, 13)])
    response = await c.handle_graph(_graph_request())
    assert response.status == 200
    assert conn.fetch.await_args.args[1] == list(range(1, 13))
    result = json.loads(response.text)["records"][0]["nodes"]
    assert [n["ref"] for n in result] == [f"fact:{i}" for i in range(1, 13)]
    assert [n["text"] for n in result] == [f"whole {i}" for i in range(1, 13)]
    assert all("content" not in n and "content_truncated" not in n for n in result)


# ── F20: Private neighbour visibility for owner vs non-owner ──────────────────

@pytest.mark.asyncio
async def test_f20_private_neighbor_visibility_in_expansion():
    """F4: A private neighbour keeps a null end for a non-owner, and the owner receives the record. The node snippet is not shown."""
    c, mock_conn, _ = _coord_with_mocks()

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 66, "content": "private fact body!!", "content_chars": 28,
         "metadata": {"type": "fact"}, "superseded": False,
         "visibility": "private", "agent_id": "owner_agent", "scope": "global"},
    ])
    edge = {"pg_id": 66, "label": ONT.fact, "rel_type": "MENTIONS",
            "direction": "out", "snippet": "private fact snip"}

    hit_bob = {"pg_id": 1, "record_type": "fact", "ref": "fact:1", "metadata": {},
               "graph_context": [_live_edge(dict(edge), 1)]}
    marks = MagicMock()
    marks.run = AsyncMock(return_value=_EmptyRows())
    await c._compose_search_hits(
        [hit_bob], ranked=True, viewer="other_agent", viewer_scope=None, session=marks)
    entry_bob = hit_bob["graph_context"][0]
    assert entry_bob["relation"]["to"] is None
    assert "record" not in entry_bob
    assert "private fact snip" not in json.dumps(entry_bob)
    assert "fact:66" not in json.dumps(entry_bob)

    hit_alice = {"pg_id": 1, "record_type": "fact", "ref": "fact:1", "metadata": {},
                 "graph_context": [_live_edge(dict(edge), 1)]}
    await c._compose_search_hits(
        [hit_alice], ranked=True, viewer="owner_agent", viewer_scope=None, session=marks)
    record = hit_alice["graph_context"][0]["record"]
    assert record["ref"] == "fact:66"
    assert record["full_record"]["chars"] == 28
    assert record["text"] == "private fact body!!"


# ── Body scope is ignored on graph ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_request_body_scope_is_ignored():
    """F19: graph body scope cannot grant access to scoped nodes."""
    n = _make_node(["Fact"], {"pg_id": 15, "content": "graph copy"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    secret_text = "<secret scope string>"
    async def _fake_fetch(sql, ids):
        if "length(t.content)" in sql:
            return [{"id": 15, "content_chars": len(secret_text), "type": "fact", "content": "whole Postgres", "superseded": False,
                     "visibility": "scope", "agent_id": "alice", "scope": "ops"}]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    req = _graph_request(agent="alice", body_extra={"scope": "ops"})
    resp = await c.handle_graph(req)
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec == {"label": "Fact"}
    assert "content" not in rec
    assert "content_source" not in rec
    assert "content_chars" not in rec
    assert "ref" not in rec
    assert "content_truncated" not in rec
    assert secret_text not in resp.text


# ── Row value whose indexing raises ──────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 3])
async def test_graph_row_value_whose_indexing_raises_still_returns_200(caplog, count):
    """A row value whose indexing raises still returns 200."""
    node = _make_node(["Fact"], {"pg_id": 1, "content": "graph 1"}, element_id="1", id_=1)

    class BrokenRecord(_FakeRecord):
        def __getitem__(self, key):
            raise RuntimeError("broken indexing")

    coord, _, _ = _coord_with_mocks([BrokenRecord({f'nodes{i}': [node] for i in range(count)})])
    resp = await coord.handle_graph(_graph_request())
    assert resp.status == 200
    assert json.loads(resp.text)['records'] == [{f'nodes{i}': None for i in range(count)}]
    warnings = [r for r in caplog.records if 'graph query value unavailable' in r.message]
    assert len(warnings) == 1
    assert f'{count} cells' in warnings[0].message


# ── outbox-only ids: the status reply never invents a type ───────────────────

_OUTBOX_ONLY = {"status": "applied", "retries": 1, "applied_at": None,
                "rem_reviewed_at": None, "consolidated_at": None}


@pytest.mark.asyncio
async def test_status_of_an_outbox_only_id_does_not_call_a_decision_ref_a_fact():
    """No technical_docs row, an outbox row present: fact:N and bare N answer the same;
    decision:N is a 200 miss, not a 404 saying id N is a fact."""
    bodies = {}
    for ref in ("fact:31", "31", "decision:31"):
        c, mock_conn, _ = _coord_with_mocks()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(side_effect=[None, dict(_OUTBOX_ONLY)])
        resp = await c.handle_status(_status_request(ref))
        assert resp.status == 200, ref
        bodies[ref] = json.loads(resp.text)
    assert bodies["fact:31"] == bodies["31"]
    assert bodies["fact:31"]["exists"] is False
    assert bodies["fact:31"]["neo4j"] == "applied"
    assert bodies["decision:31"] == {"pg_id": 31, "exists": False, "neo4j": "unknown"}
