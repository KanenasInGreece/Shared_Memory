"""Lineage failures from brief v6 section 6 (step 2b).

Each test names the F-row it kills. The handler is stubbed; the Cypher
shape is asserted here and the rig runs the real query.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from coordinator import MemoryCoordinator, ONT  # noqa: E402

WHOLE = (
    "Postgres holds the whole fact, longer than the graph copy, so a cut "
    "snippet cannot be this entry."
)
REM = "shorter rem summary of the anchor"


class _Ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        return None


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


def _status(ref, agent="alice"):
    req = MagicMock()
    req.match_info = {"pg_id": ref}
    state = {"authenticated_agent": agent}
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    return req


def _coord(rows, graph_rows):
    c = MemoryCoordinator()
    conn = AsyncMock()

    async def _fetch(sql, *params):
        if "content_chars" in sql and "LEFT JOIN" in sql:
            wanted = list(params[0] or []) if params else []
            return [dict(rows[i]) for i in wanted if i in rows]
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchrow = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    c._pool = pool
    session = MagicMock()
    session.run = AsyncMock(return_value=_Rows(graph_rows))
    neo = MagicMock()
    neo.session = MagicMock(return_value=_Ctx(session))
    c._neo4j = neo
    return c, conn, session


def _doc(pid, content, **extra):
    row = {
        "id": pid, "content": content, "metadata": {"type": "fact"},
        "type": "fact", "superseded": False, "superseded_by": None,
        "visibility": "global", "agent_id": "alice", "scope": "global",
        "created_at": None, "grounded_in": None, "supersession_ack": None,
    }
    row.update(extra)
    return row


def _edge(anchor, far, labels, rel, *, inbound=False, rem=None, props=None):
    start, end = (far, anchor) if inbound else (anchor, far)
    start_labels = labels if inbound else [ONT.fact]
    end_labels = [ONT.fact] if inbound else labels
    return {
        "anchor_rem_summary": rem,
        "rel": rel,
        "start_pg_id": start,
        "start_labels": start_labels,
        "end_pg_id": end,
        "end_labels": end_labels,
        "labels": labels,
        "pg_id": far,
        "rel_props": props or {"notes": "legacy retrospective text"},
    }


@pytest.mark.asyncio
async def test_f21_anchor_superseded_by_is_a_qualified_ref_and_rem_summary_is_text():
    """F21: the lineage anchor's superseded_by is still a bare integer, or the anchor ignores its rem_summary."""
    successor = _doc(9, "the successor fact")
    anchor = _doc(
        5, WHOLE, superseded=True, superseded_by=9,
        successor_id=9, successor_metadata={"type": "fact"},
        successor_visibility="global", successor_agent_id="alice",
        successor_scope="global")
    c, conn, session = _coord({5: anchor, 9: successor}, [
        _edge(5, 9, [ONT.fact], "SUPERSEDES", rem=REM),
    ])
    conn.fetchrow = AsyncMock(side_effect=[anchor, None])
    body = json.loads((await c.handle_status(_status("fact:5"))).text)
    assert body["text"] == REM
    assert body["text_is"] == "summary"
    assert body["superseded_by"] == "fact:9"
    assert isinstance(body["superseded_by"], str)
    assert "snippet" not in body
    cypher = session.run.await_args_list[0].args[0]
    assert cypher.index("n.rem_summary") < cypher.index("OPTIONAL MATCH")


@pytest.mark.asyncio
async def test_f24_unreadable_anchor_answers_as_missing_before_type():
    """F24: lineage of an unreadable record discloses its real type or any field."""
    hidden = _doc(
        5, "SECRET-ANCHOR", visibility="private", agent_id="alice",
        superseded=True, superseded_by=9)
    hidden["type"] = "decision"
    hidden["metadata"] = {"type": "decision"}
    hidden["grounded_in"] = [3]
    c, conn, session = _coord({}, [])
    conn.fetchrow = AsyncMock(side_effect=[hidden, None])
    resp = await c.handle_status(_status("fact:5", agent="bob"))
    body = json.loads(resp.text)
    assert body == {"pg_id": 5, "exists": False, "neo4j": "unknown"}
    assert "decision" not in resp.text
    assert "SECRET-ANCHOR" not in resp.text
    assert session.run.await_count == 0

    owned = _doc(8, WHOLE, grounded_in=[4])
    secret_source = _doc(4, "SECRET-SOURCE", visibility="private", agent_id="zoe")
    c2, conn2, _ = _coord({8: owned, 4: secret_source}, [
        {"anchor_rem_summary": None, "rel": None},
    ])
    conn2.fetchrow = AsyncMock(side_effect=[
        owned, None,
    ])
    # The status query returns grounded_in on the anchor row.
    owned_status = dict(owned)
    owned_status["grounded_in"] = [4]
    conn2.fetchrow = AsyncMock(side_effect=[owned_status, None])
    visible = json.loads((await c2.handle_status(_status("fact:8"))).text)
    assert visible["grounded_in"] == [] or 4 not in (visible.get("grounded_in") or [])
    assert "SECRET-SOURCE" not in json.dumps(visible)


@pytest.mark.asyncio
async def test_f24_owner_wrong_kind_on_the_summary_table_names_the_real_ref():
    """F24: a readable summary asked as an insight answers exists:false, or returns the row.

    The owner still gets the real ref. An unreadable row would have stopped
    before this type comparison.
    """
    row = {
        "id": 10, "metadata": {"kind": "thematic"}, "source_pg_ids": [],
        "created_at": None, "superseded": False, "superseded_reason": None,
        "superseded_by": None, "run_id": 1, "content": "SECRET-SUMMARY-BODY",
        "content_chars": 18, "visibility": "global", "agent_id": "alice",
        "scope": "global",
    }
    c, conn, _ = _coord({}, [])
    conn.fetchrow = AsyncMock(return_value=row)
    resp = await c.handle_status(_status("insight:10", agent="alice"))
    assert resp.status == 404
    body = json.loads(resp.text)
    assert body["status"] == "error"
    assert "summary:10" in body["message"]
    assert "SECRET-SUMMARY-BODY" not in resp.text


@pytest.mark.asyncio
async def test_f17_lineage_marks_query_is_one_for_two_decision_neighbours():
    """F17: lineage runs the marks query once per decision neighbour.

    Two decision neighbours on one anchor. More than two session.run calls
    means the marks read left the single batch.
    """
    anchor = _doc(5, WHOLE)
    c, conn, session = _coord({}, [])
    conn.fetchrow = AsyncMock(side_effect=[anchor, None])
    neighbours = [
        {"anchor_rem_summary": None, "rel": "INFORMED_BY",
         "start_pg_id": 5, "start_labels": [ONT.fact],
         "end_pg_id": 7, "end_labels": [ONT.decision],
         "rel_props": {}, "labels": [ONT.decision], "pg_id": 7, "rem_summary": None},
        {"anchor_rem_summary": None, "rel": "INFORMED_BY",
         "start_pg_id": 5, "start_labels": [ONT.fact],
         "end_pg_id": 8, "end_labels": [ONT.decision],
         "rel_props": {}, "labels": [ONT.decision], "pg_id": 8, "rem_summary": None},
    ]
    calls = {"run": 0, "marks": 0}

    async def run(cypher, **kwargs):
        calls["run"] += 1
        if "decision_id" in cypher or "outcomes" in cypher:
            calls["marks"] += 1
            return _Rows([])
        return _Rows(neighbours)

    session.run = AsyncMock(side_effect=run)
    resp = await c.handle_status(_status("fact:5"))
    assert resp.status == 200
    assert calls["marks"] == 1
    assert calls["run"] <= 2


@pytest.mark.asyncio
async def test_f27_anchor_with_no_neighbour_still_uses_its_rem_summary():
    """F27: a lineage anchor with no qualifying neighbour is shown as full_record although its rem_summary passes rule 5."""
    anchor = _doc(5, WHOLE)
    c, _, session = _coord({5: anchor}, [{
        "anchor_rem_summary": REM, "rel": None, "pg_id": None, "labels": None,
    }])
    conn_row = dict(anchor)
    c._pool.acquire = MagicMock(return_value=_Ctx(c._pool.acquire.return_value))
    # Rebind fetchrow on the connection the pool yields.
    session.run = AsyncMock(return_value=_Rows([{
        "anchor_rem_summary": REM, "rel": None,
    }]))
    # The coordinator built above already holds the connection.
    c2, conn2, session2 = _coord({5: anchor}, [{
        "anchor_rem_summary": REM, "rel": None,
    }])
    conn2.fetchrow = AsyncMock(side_effect=[anchor, None])
    body = json.loads((await c2.handle_status(_status("fact:5"))).text)
    assert body["text"] == REM
    assert body["text_is"] == "summary"
    assert body["related"] == []
    cypher = session2.run.await_args.args[0]
    assert "OPTIONAL MATCH (n)-[r]-(m)" in cypher
    assert cypher.index("MATCH (n)") < cypher.index("OPTIONAL MATCH")
    assert cypher.index("n.rem_summary") < cypher.index("OPTIONAL MATCH")


@pytest.mark.asyncio
async def test_f32_lineage_drops_a_legacy_self_loop_and_marks_a_superseded_neighbour():
    """F32: a legacy self-loop emits the anchor as its own neighbour."""
    anchor = _doc(5, WHOLE)
    neighbour = _doc(9, "retired neighbour text", superseded=True, superseded_by=11,
                     successor_id=11, successor_metadata={"type": "fact"},
                     successor_visibility="global", successor_agent_id="alice",
                     successor_scope="global")
    successor = _doc(11, "live successor")
    loop = _edge(5, 5, [ONT.fact], "HAD_OUTCOME")
    loop["start_pg_id"] = 5
    loop["end_pg_id"] = 5
    far = _edge(5, 9, [ONT.fact], "REFERENCES")
    c, conn, _ = _coord({5: anchor, 9: neighbour, 11: successor}, [loop, far])
    conn.fetchrow = AsyncMock(side_effect=[anchor, None])
    body = json.loads((await c.handle_status(_status("fact:5"))).text)
    assert len(body["related"]) == 1
    item = body["related"][0]
    assert item["relation"]["to"] == "fact:9"
    assert item["relation"]["from"] == "fact:5"
    assert item["record"]["obsolete"] == "superseded"
    assert item["record"]["superseded_by"] == "fact:11"
    assert "fact:5" != item["relation"]["to"]
    dumped = json.dumps(body["related"])
    assert "legacy retrospective text" not in dumped
