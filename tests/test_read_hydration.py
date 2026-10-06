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
    """G1: A graph node's content is replaced with, or extended by, Postgres text: the fixture's Postgres text differs from the node copy; the node copy comes back byte-identical and the Postgres string appears nowhere in the response."""
    node_copy = "original graph node copy"
    postgres_text = "DIFFERENT whole postgres text that must not appear in graph response"
    n = _make_node(["Fact"], {"pg_id": 1, "content": node_copy}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content_chars": 68, "content": postgres_text, "type": "fact",
         "superseded": False, "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body_text = resp.text
    body = json.loads(body_text)
    rec = body["records"][0]["n"]

    assert rec["content"] == node_copy
    assert postgres_text not in body_text


# ── G2: Content truncated rules and index keys ─────────────────────────────────

@pytest.mark.asyncio
async def test_g2_readable_node_index_keys_and_content_truncated_flag_rules():
    """G2: A readable node lacks ref, content_chars or content_truncated; or the flag is wrong for: copy shorter than the record -> True; copy equal -> False; copy LONGER than the record -> False; a Decision node with no content and content_chars 46 -> True."""
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
        {"id": 1, "content_chars": 100, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
        {"id": 2, "content_chars": 9, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
        {"id": 3, "content_chars": 5, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
        {"id": 4, "content_chars": 46, "type": "decision", "superseded": False,
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
    assert r1["content_chars"] == 100
    assert r1["content_truncated"] is True

    assert r2["ref"] == "fact:2"
    assert r2["content_chars"] == 9
    assert r2["content_truncated"] is False

    assert r3["ref"] == "fact:3"
    assert r3["content_chars"] == 5
    assert r3["content_truncated"] is False

    assert r4["ref"] == "decision:4"
    assert r4["content_chars"] == 46
    assert r4["content_truncated"] is True


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
        if "technical_docs" in sql and "length(content)" in sql:
            return [{"id": 42, "content_chars": 200, "type": "fact", "superseded": False,
                     "visibility": "global", "agent_id": None, "scope": None}]
        if "community_summaries" in sql and "length(content)" in sql:
            return [
                {"id": 42, "content_chars": 500, "snippet": "summary snippet 42",
                 "superseded": False, "metadata": {}},
                {"id": 70, "content_chars": 700, "snippet": "insight snippet 70",
                 "superseded": False, "metadata": {"kind": "insight"}},
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
    assert r_fact["content_chars"] == 200

    assert r_sum["ref"] == "summary:42"
    assert r_sum["content_chars"] == 500
    assert r_sum["snippet"] == "summary snippet 42"

    assert r_ins["ref"] == "insight:70"
    assert r_ins["content_chars"] == 700
    assert r_ins["snippet"] == "insight snippet 70"


# ── G4: Unreadable node gains no index keys, owner gets them ──────────────────

@pytest.mark.asyncio
async def test_g4_unreadable_node_gains_no_index_keys_and_owner_is_not_refused():
    """G4: A node the caller cannot read (private non-owner; scope; unauthenticated caller) gains ref, content_chars, content_truncated or snippet; or a readable node beside it is also withheld; or the owner is refused them."""
    n_priv = _make_node(["Fact"], {"pg_id": 1, "content": "private fact"}, element_id="1", id_=1)
    n_scope = _make_node(["Fact"], {"pg_id": 2, "content": "scope fact"}, element_id="2", id_=2)
    n_readable = _make_node(["Fact"], {"pg_id": 3, "content": "global fact"}, element_id="3", id_=3)

    c, mock_conn, _ = _coord_with_mocks([
        _FakeRecord({"n": n_priv}),
        _FakeRecord({"n": n_scope}),
        _FakeRecord({"n": n_readable}),
    ])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content_chars": 100, "type": "fact", "superseded": False,
         "visibility": "private", "agent_id": "alice", "scope": "global"},
        {"id": 2, "content_chars": 200, "type": "fact", "superseded": False,
         "visibility": "scope", "agent_id": "alice", "scope": "ops"},
        {"id": 3, "content_chars": 300, "type": "fact", "superseded": False,
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
    assert b_anon[2]["n"]["content_chars"] == 300

    # Non-owner bob
    resp_bob = await c.handle_graph(_graph_request(agent="bob"))
    assert resp_bob.status == 200
    b_bob = json.loads(resp_bob.text)["records"]
    for idx in (0, 1):
        for k in ("ref", "content_chars", "content_truncated", "snippet", "content_source"):
            assert k not in b_bob[idx]["n"]
    assert b_bob[2]["n"]["ref"] == "fact:3"
    assert b_bob[2]["n"]["content_chars"] == 300

    # Owner alice: gets index keys for n_priv, n_scope withheld (viewer_scope is None on graph)
    resp_alice = await c.handle_graph(_graph_request(agent="alice"))
    assert resp_alice.status == 200
    b_alice = json.loads(resp_alice.text)["records"]
    assert b_alice[0]["n"]["ref"] == "fact:1"
    assert b_alice[0]["n"]["content_chars"] == 100
    assert b_alice[0]["n"]["content_truncated"] is True
    for k in ("ref", "content_chars", "content_truncated", "snippet", "content_source"):
        assert k not in b_alice[1]["n"]
    assert b_alice[2]["n"]["ref"] == "fact:3"


# ── G5: Graph SQL never selects whole content ─────────────────────────────────

@pytest.mark.asyncio
async def test_g5_graph_sql_never_selects_whole_content():
    """G5: A SQL statement sent by handle_graph selects content as a whole value rather than length(content) or left(content, 120)."""
    n1 = _make_node(["Fact"], {"pg_id": 1, "content": "graph fact"}, element_id="1", id_=1)
    n2 = _make_node(["CommunitySummary"], {"pg_id": 2}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1}), _FakeRecord({"n": n2})])

    mock_conn.fetch = AsyncMock(return_value=[])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200

    def _assert_no_whole_content(sql):
        cleaned_sql = (
            sql.replace("length(content)", "")
            .replace("left(content, 120)", "")
            .replace("content_chars", "")
        )
        assert "content" not in cleaned_sql.lower(), f"Unexpected whole content in query: {sql}"

    for call in mock_conn.fetch.await_args_list:
        _assert_no_whole_content(call.args[0])
    for call in mock_conn.fetchrow.await_args_list:
        _assert_no_whole_content(call.args[0])


# ── G6: CommunitySummary node on graph has snippet and no content key ─────────

@pytest.mark.asyncio
async def test_g6_community_summary_node_on_graph_has_snippet_and_no_content_key():
    """G6: A CommunitySummary node returned on graph gains a content key (it must have snippet from Postgres and NO content)."""
    n = _make_node(["CommunitySummary"], {"pg_id": 10}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 10, "content_chars": 500, "snippet": "first 120 chars snippet",
         "superseded": False, "metadata": {}},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert "content" not in rec
    assert rec["snippet"] == "first 120 chars snippet"
    assert rec["ref"] == "summary:10"
    assert rec["content_chars"] == 500
    assert rec["content_truncated"] is True


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


_LINEAGE_TEXT_KEYS = (
    "content", "alternatives", "confidence", "content_withheld", "content_truncated",
)

_LONG_LINEAGE = (
    "A demo ledger keeps the names in sorted order so two agents cannot deadlock "
    "on the same pair of locks when they acquire them from opposite ends of the list."
)


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

@pytest.mark.asyncio
async def test_l3_snippet_and_content_chars_follow_visibility():
    """L3: A readable lineage lacks content_chars or snippet, or an unreadable record carries either."""
    text = _LONG_LINEAGE
    assert len(text) > 120
    snippet = text[:120]

    c, mock_conn, _ = _coord_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(21, "fact", text), None,
    ])
    resp = await c.handle_status(_status_request("fact:21"))
    body = json.loads(resp.text)
    assert body["content_chars"] == len(text)
    assert body["snippet"] == snippet
    keys = list(body.keys())
    assert keys[keys.index("ref") + 1] == "content_chars"
    assert keys[keys.index("ref") + 2] == "snippet"

    c_owner, conn_owner, _ = _coord_with_mocks()
    conn_owner.fetch = AsyncMock(return_value=[])
    conn_owner.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(22, "fact", text, visibility="private", agent_id="alice"),
        None,
    ])
    owned = json.loads((await c_owner.handle_status(
        _status_request("fact:22", agent="alice"))).text)
    assert owned["content_chars"] == len(text)
    assert owned["snippet"] == snippet

    withheld = [
        ("fact:23", "bob", _doc_status_row(
            23, "fact", text, visibility="private", agent_id="alice"), False),
        ("fact:24", "alice", _doc_status_row(
            24, "fact", text, visibility="scope", agent_id="alice", scope="ops"), False),
    ]
    for ref, agent, row, _summary in withheld:
        c_w, conn_w, _ = _coord_with_mocks()
        conn_w.fetch = AsyncMock(return_value=[])
        conn_w.fetchrow = AsyncMock(side_effect=[row, None])
        hidden = json.loads((await c_w.handle_status(_status_request(ref, agent=agent))).text)
        assert "content_chars" not in hidden, ref
        assert "snippet" not in hidden, ref
        assert text not in json.dumps(hidden)

    c_sum, conn_sum, _ = _coord_with_mocks()
    conn_sum.fetchrow = AsyncMock(return_value=_summary_status_row(
        25, text, visibility="private", agent_id="alice"))
    hidden_sum = json.loads((await c_sum.handle_status(
        _status_request("summary:25", agent="bob"))).text)
    assert "content_chars" not in hidden_sum
    assert "snippet" not in hidden_sum
    assert text not in json.dumps(hidden_sum)


# ── L5: related names the direction of each direct neighbour ─────────────────

@pytest.mark.asyncio
async def test_l5_related_direction_matches_who_points_at_whom():
    """L5: related misses a direction or mislabels it."""
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
            if "community_summaries" in sql:
                return []
            if "length(content)" in sql:
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
    assert fact_body["related"] == [{
        "ref": "decision:9", "rel": "GROUNDED_IN", "dir": "in",
        "snippet": decision_snippet,
    }]

    decision_body, _ = await _lineage(
        "decision:9",
        [{
            "rel": "GROUNDED_IN", "outward": True, "labels": ["Fact"],
            "pg_id": 5, "snippet": fact_snippet,
        }],
        [{"id": 5, "content_chars": 60, "type": "fact", "superseded": False,
          "visibility": "global", "agent_id": None, "scope": None}],
    )
    assert decision_body["related"] == [{
        "ref": "fact:5", "rel": "GROUNDED_IN", "dir": "out",
        "snippet": fact_snippet,
    }]


# ── L6: related drops summaries, entities, and unreadable neighbours ─────────

@pytest.mark.asyncio
async def test_l6_related_omits_summaries_entities_and_unreadable_neighbours():
    """L6: related contains a CommunitySummary, an Entity, an unreadable neighbour, or an extra key."""
    secret = "SECRET-NEIGHBOUR-SNIPPET-must-not-leak"
    c, mock_conn, session = _coord_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(5, "fact", _LONG_LINEAGE), None,
    ])

    async def _fetch(sql, *args):
        if "community_summaries" in sql and "source_pg_ids" in sql:
            return []
        if "length(content)" in sql:
            return [
                {"id": 9, "content_chars": 10, "type": "decision", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
                {"id": 4, "content_chars": 10, "type": "fact", "superseded": False,
                 "visibility": "private", "agent_id": "alice", "scope": "global"},
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
    assert body["related"] == [{
        "ref": "decision:9", "rel": "GROUNDED_IN", "dir": "in",
        "snippet": "visible decision",
    }]
    assert secret not in resp.text
    for item in body["related"]:
        assert set(item) == {"ref", "rel", "dir", "snippet"}
    cypher = session.run.await_args.args[0]
    assert "CommunitySummary" in cypher
    assert "NOT m:" in cypher
    assert "startNode(r) = n" in cypher
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
    mock_conn.fetch = AsyncMock(return_value=[])
    c._neo4j.session = MagicMock(side_effect=RuntimeError("graph down"))
    resp = await c.handle_status(_status_request("fact:5"))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["pg_id"] == 5
    assert body["ref"] == "fact:5"
    assert body["exists"] is True
    assert body["superseded"] is True
    assert body["superseded_by"] == 9
    assert body["content_chars"] == len(_LONG_LINEAGE)
    assert body["snippet"] == _LONG_LINEAGE[:120]
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
        if "length(content)" in sql:
            return [
                {"id": 11, "content_chars": 4, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
                {"id": 12, "content_chars": 4, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
                {"id": 13, "content_chars": 4, "type": "fact", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch)
    session.run = AsyncMock(return_value=_AsyncRows(neighbours))
    body = json.loads((await c.handle_status(_status_request("fact:5"))).text)
    assert body["related_more"] is True
    assert [item["ref"] for item in body["related"]] == ["fact:11", "fact:12"]

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
    """L9: A graph neighbour with no technical_docs row is still named in related."""
    c, mock_conn, session = _coord_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _doc_status_row(5, "fact", _LONG_LINEAGE), None,
    ])

    async def _fetch(sql, *args):
        if "length(content)" in sql:
            return [
                {"id": 9, "content_chars": 10, "type": "decision", "superseded": False,
                 "visibility": "global", "agent_id": None, "scope": None},
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
    assert [item["ref"] for item in body["related"]] == ["decision:9"]
    assert "fact:4" not in json.dumps(body["related"])


# ── F3: Node preserves content and gains chars/ref, no content_source ─────────

@pytest.mark.asyncio
async def test_f3_unchosen_node_preserves_content_and_gains_source_chars_ref():
    """F3: A node keeps its graph content and gains ref, content_chars, content_truncated; content_source is never written."""
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
    mock_conn.fetchrow = AsyncMock(side_effect=[rec_row, None])
    mock_conn.fetch = AsyncMock(return_value=[])

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
    assert b_alice["content_chars"] == len(private_text)
    assert b_alice["snippet"] == private_text[:120]

    # 3. Non-owner bob on graph
    n_priv = _make_node(["Fact"], {"pg_id": 55, "content": "graph copy"}, element_id="1", id_=1)
    c_graph, mock_conn_g, _ = _coord_with_mocks([_FakeRecord({"n": n_priv})])
    async def _fake_fetch_priv(sql, ids):
        if "length(content)" in sql:
            return [{"id": 55, "content_chars": 28, "type": "fact", "superseded": False,
                     "visibility": "private", "agent_id": "alice", "scope": "global"}]
        return []
    mock_conn_g.fetch = AsyncMock(side_effect=_fake_fetch_priv)

    resp_g_bob = await c_graph.handle_graph(_graph_request(agent="bob"))
    assert resp_g_bob.status == 200
    rec_g_bob = json.loads(resp_g_bob.text)["records"][0]["n"]
    assert rec_g_bob["content"] == "graph copy"
    assert "content_source" not in rec_g_bob
    assert "content_chars" not in rec_g_bob
    assert "ref" not in rec_g_bob
    assert "content_truncated" not in rec_g_bob
    assert private_text not in resp_g_bob.text

    # 4. Owner alice on graph
    resp_g_alice = await c_graph.handle_graph(_graph_request(agent="alice"))
    assert resp_g_alice.status == 200
    rec_g_alice = json.loads(resp_g_alice.text)["records"][0]["n"]
    assert rec_g_alice["content"] == "graph copy"
    assert "content_source" not in rec_g_alice
    assert rec_g_alice["ref"] == "fact:55"
    assert rec_g_alice["content_chars"] == 28
    assert rec_g_alice["content_truncated"] is True
    assert private_text not in resp_g_alice.text


# ── F5: Fetch error or TimeoutError degrades gracefully ───────────────────────

@pytest.mark.asyncio
async def test_f5_fetch_error_or_timeout_degrades_gracefully_on_graph_and_search():
    """F5: A fetch error, or asyncio.TimeoutError from _acquire, fails the graph read or the search instead of returning 200 with untouched nodes."""
    n1 = _make_node(["Fact"], {"pg_id": 77, "content": "graph content intact"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1})])
    c._acquire = MagicMock(side_effect=asyncio.TimeoutError("pool acquire timeout"))

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    rec = body["records"][0]["n"]
    assert rec["content"] == "graph content intact"
    assert "content_source" not in rec
    assert "ref" not in rec
    assert "content_chars" not in rec
    assert "content_truncated" not in rec


@pytest.mark.asyncio
async def test_f5_fetch_runtime_error_degrades_gracefully_with_warning(caplog):
    """F5: When Postgres fetch raises RuntimeError, handle_graph returns 200, nodes untouched, exactly one warning logged."""
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
    assert "content_source" not in rec
    assert "ref" not in rec
    assert "content_chars" not in rec
    assert "content_truncated" not in rec

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "graph query text hydration failed" in warnings[0].message

    ctx_entries = [{"pg_id": 77, "label": ONT.fact, "snippet": "cypher snippet"}]
    c_search, _, _ = _coord_with_mocks()
    c_search._acquire = MagicMock(side_effect=asyncio.TimeoutError("timeout"))
    anchor_map = {1: list(ctx_entries)}
    await c_search._hydrate_and_filter_neighbors(anchor_map, viewer="agent")
    assert anchor_map[1][0]["snippet"] == "cypher snippet"
    assert "content_chars" not in anchor_map[1][0]
    assert "content_source" not in anchor_map[1][0]


# ── F6: Non-eligible graph types untouched, list hydrated ─────────────────────

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

    assert expected_data["path"] == [start_props, "RELATES_TO", end_props]
    assert expected_data["rel"] == (start_props, "RELATES_TO", end_props)
    assert expected_data["m"] == {"n": node_props}

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

    assert row["nlist"][0]["content"] == "graph 7"
    assert row["nlist"][0]["ref"] == "fact:7"
    assert row["nlist"][0]["content_chars"] == 22
    assert row["nlist"][0]["content_truncated"] is True
    assert "content_source" not in row["nlist"][0]


# ── F8: Summary neighbour with null/missing/retired snippet handling ──────────

@pytest.mark.asyncio
async def test_f8_summary_neighbor_removal_and_snippet_replacement():
    """F8: A summary neighbour is returned with a null snippet; a retired or missing one is returned; or removing one skips the entry after it."""
    c, mock_conn, _ = _coord_with_mocks()
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
    """F11: The same record in two rows: both nodes keep the node copy, both get the same ref and the same content_chars."""
    node_a = _make_node(["Fact"], {"pg_id": 5, "content": "graph 5"}, element_id="1", id_=1)
    node_b = _make_node(["Fact"], {"pg_id": 5, "content": "graph 5"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": node_a}), _FakeRecord({"n": node_b})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 5, "content_chars": 10000, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    r0 = body["records"][0]["n"]
    r1 = body["records"][1]["n"]
    assert "content_source" not in r0
    assert "content_source" not in r1
    assert r0["content"] == "graph 5"
    assert r1["content"] == "graph 5"
    assert r0["ref"] == "fact:5"
    assert r1["ref"] == "fact:5"
    assert r0["content_chars"] == 10000
    assert r1["content_chars"] == 10000
    assert r0["content_truncated"] is True
    assert r1["content_truncated"] is True


# ── F14: Superseded nodes hydrated on graph ───────────────────────────────────

@pytest.mark.asyncio
async def test_f14_superseded_node_is_hydrated_on_graph():
    """F14: A superseded Fact node keeps what it stores and gets index keys."""
    n = _make_node(["Fact"], {"pg_id": 9, "content": "graph copy"}, element_id="1", id_=1)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 9, "content_chars": 30, "type": "fact", "superseded": True,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "graph copy"
    assert "content_source" not in rec
    assert rec["ref"] == "fact:9"
    assert rec["content_chars"] == 30
    assert rec["content_truncated"] is True


@pytest.mark.asyncio
async def test_f14_superseded_summary_node_is_hydrated_on_graph():
    """F14: A superseded CommunitySummary node keeps what it stores and gets index keys and snippet."""
    n_sum = _make_node(["CommunitySummary"], {"pg_id": 99}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n_sum})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 99, "content_chars": 38, "snippet": "snip", "superseded": True, "metadata": {}},
    ])

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert "content" not in rec
    assert "content_source" not in rec
    assert rec["ref"] == "summary:99"
    assert rec["content_chars"] == 38
    assert rec["content_truncated"] is True
    assert rec["snippet"] == "snip"


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
        {"id": 15, "content_chars": len(scope_secret), "type": "fact", "superseded": False,
         "visibility": "scope", "agent_id": "alice", "scope": "ops"},
    ])

    resp_g = await c_g.handle_graph(_graph_request(agent="alice"))
    assert resp_g.status == 200
    rec_g = json.loads(resp_g.text)["records"][0]["n"]
    assert rec_g["content"] == "graph copy"
    assert "content_source" not in rec_g
    assert "content_chars" not in rec_g
    assert "ref" not in rec_g
    assert "content_truncated" not in rec_g
    assert scope_secret not in resp_g.text


# ── F16: Private unreadable record earlier in walk gains nothing ───────────────

@pytest.mark.asyncio
async def test_f16_private_unreadable_record_spends_no_budget():
    """F16: A private record earlier in the walk gains nothing and its node copy is unchanged; the next readable node gets its index keys."""
    n1 = _make_node(["Fact"], {"pg_id": 101, "content": "graph 101"}, element_id="1", id_=1)
    n2 = _make_node(["Fact"], {"pg_id": 102, "content": "graph 102"}, element_id="2", id_=2)
    c, mock_conn, _ = _coord_with_mocks([_FakeRecord({"n": n1}), _FakeRecord({"n": n2})])

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 101, "content_chars": 15000, "type": "fact", "superseded": False,
         "visibility": "private", "agent_id": "other_agent", "scope": "global"},
        {"id": 102, "content_chars": 5000, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    resp = await c.handle_graph(_graph_request(agent="caller_agent"))
    assert resp.status == 200
    body = json.loads(resp.text)
    r1 = body["records"][0]["n"]
    r2 = body["records"][1]["n"]

    assert r1["content"] == "graph 101"
    assert "content_source" not in r1
    assert "content_chars" not in r1
    assert "ref" not in r1
    assert "content_truncated" not in r1

    assert r2["content"] == "graph 102"
    assert "content_source" not in r2
    assert r2["ref"] == "fact:102"
    assert r2["content_chars"] == 5000
    assert r2["content_truncated"] is True


# ── F19: Row cap bounds distinct keys sent to Postgres ────────────────────────

@pytest.mark.asyncio
async def test_f19_keys_past_cap_are_not_sent_to_postgres(monkeypatch):
    """F19: Distinct eligible keys past GRAPH_QUERY_ROW_CAP are not sent to Postgres and gain nothing."""
    nodes = [
        _make_node(["Fact"], {"pg_id": i, "content": f"g {i}"}, element_id=str(i), id_=i)
        for i in range(1, 13)
    ]
    records = [_FakeRecord({"nodes": nodes})]
    c, mock_conn, _ = _coord_with_mocks(records)

    monkeypatch.setattr(coordinator_mod, "GRAPH_QUERY_ROW_CAP", 10)
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": i, "content_chars": 10, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None} for i in range(1, 11)
    ])
    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200

    first_call = mock_conn.fetch.await_args_list[0]
    ids_sent = first_call.args[1]
    assert ids_sent == list(range(1, 11))

    body = json.loads(resp.text)
    row_nodes = body["records"][0]["nodes"]
    assert "content_source" not in row_nodes[0]
    assert row_nodes[0]["content"] == "g 1"
    assert row_nodes[0]["ref"] == "fact:1"
    assert row_nodes[0]["content_chars"] == 10
    assert row_nodes[0]["content_truncated"] is True

    n11 = row_nodes[10]
    n12 = row_nodes[11]
    assert "content_source" not in n11
    assert n11["content"] == "g 11"
    assert "content_chars" not in n11
    assert "ref" not in n11
    assert "content_truncated" not in n11

    assert "content_source" not in n12
    assert n12["content"] == "g 12"
    assert "content_chars" not in n12
    assert "ref" not in n12
    assert "content_truncated" not in n12


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
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fake_fetch)

    req = _graph_request(agent="alice", body_extra={"scope": "ops"})
    resp = await c.handle_graph(req)
    assert resp.status == 200
    rec = json.loads(resp.text)["records"][0]["n"]
    assert rec["content"] == "graph copy"
    assert "content_source" not in rec
    assert "content_chars" not in rec
    assert "ref" not in rec
    assert "content_truncated" not in rec
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
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content_chars": 10, "type": "fact", "superseded": False,
         "visibility": "global", "agent_id": None, "scope": None},
    ])

    orig_data = rec.data
    def _broken_data():
        d = orig_data()
        d["nodes"] = _BrokenIndexSeq([node])
        return d
    rec.data = _broken_data

    resp = await c.handle_graph(_graph_request())
    assert resp.status == 200
