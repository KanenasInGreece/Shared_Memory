"""Search and search-by-ref failures from brief v6 section 6 (step 2a).

Each test names the F-row it kills. Handlers are stubbed; the Cypher-bound
rows stay on the rig.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

import coordinator as coordinator_mod  # noqa: E402
from coordinator import MemoryCoordinator, ONT  # noqa: E402


CREATED = datetime(2026, 10, 6, tzinfo=timezone.utc)
RATIONALE = "The lock order is total, so the two agents cannot deadlock."
FACT_TEXT = "Postgres holds the whole fact, longer than any graph copy."
NODE_SUMMARY = "shorter rem summary"
SECRET = "SECRET-private-retrospective-text"


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


def _request(body, agent="alice"):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    state = {"authenticated_agent": agent}
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    return req


def _coord():
    c = MemoryCoordinator()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    c._pool = pool
    session = MagicMock()
    session.run = AsyncMock(return_value=_Rows([]))
    neo = MagicMock()
    neo.session = MagicMock(return_value=_Ctx(session))
    c._neo4j = neo
    c._embed = AsyncMock(return_value=[0.1])
    return c, conn, session


def _doc(pid, content, **extra):
    row = {
        "id": pid, "content": content, "content_chars": len(content),
        "metadata": {"type": "fact"}, "type": "fact",
        "superseded": False, "superseded_by": None,
        "visibility": "global", "agent_id": None, "scope": "global",
        "created_at": CREATED,
    }
    row.update(extra)
    return row


def _edge(anchor, rel, far_id, labels, *, inbound=False, rem_summary=None,
          props=None, name=None, aliases=None):
    start_pg, end_pg = (far_id, anchor) if inbound else (anchor, far_id)
    start_labels = labels if inbound else ["Fact"]
    end_labels = ["Fact"] if inbound else labels
    return {
        "anchor_pg_id": anchor,
        "rel_type": rel,
        "rel_props": props or {},
        "rem_summary": rem_summary,
        "rem_processed": True if rem_summary else False,
        "rem_prompt_version": 2 if rem_summary else None,
        "labels": labels,
        "name": name,
        "pg_id": far_id,
        "rel_pg_id": far_id,
        "aliases": aliases or [],
        "direction": "in" if inbound else "out",
        "start_pg_id": start_pg,
        "start_labels": start_labels,
        "start_name": name if inbound else None,
        "end_pg_id": end_pg,
        "end_labels": end_labels,
        "end_name": None if inbound else name,
        "snippet": "GRAPH-NODE-TEXT-MUST-NOT-LEAK",
        "adr_fact_kind": "tested",
        "adr_source_ref": "from-the-node",
    }


def _marks_row(decision_id, outcomes, grounds):
    return {
        "decision_id": decision_id,
        "outcomes": outcomes,
        "grounds": grounds,
    }


def _run_queries(conn, *, candidates=None, docs=None, summaries=None,
                 stale=None, extra=None):
    docs = docs or {}
    summaries = summaries or {}
    candidates = candidates or []

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if extra is not None:
            hit = extra(text, args)
            if hit is not None:
                return hit
        if "successor_id" in text or "AS content_chars" in text:
            table = summaries if "community_summaries" in text else docs
            wanted = set(args[0]) if args else set(table)
            return [table[i] for i in table if i in wanted]
        if "ORDER BY embedding" in text:
            return list(candidates)
        if "t.superseded" in text:
            return list(stale or [])
        if "superseded_reason" in text:
            return []
        if text.startswith("SELECT id, metadata"):
            return []
        if "FROM technical_docs" in text and "created_at" in text:
            wanted = set(args[0]) if args and isinstance(args[0], (list, tuple, set)) else set(docs)
            return [docs[i] for i in docs if i in wanted]
        if "FROM community_summaries" in text and "source_pg_ids" in text:
            wanted = set(args[0]) if args and isinstance(args[0], (list, tuple, set)) else set(summaries)
            return [summaries[i] for i in summaries if i in wanted]
        if "SELECT id, content FROM technical_docs" in text or (
                "SELECT id, content, visibility" in text):
            wanted = set(args[0]) if args and isinstance(args[0], (list, tuple, set)) else set()
            return [docs[i] for i in docs if i in wanted]
        return []

    async def fetchrow(sql, *args):
        return None

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


async def _search(c, body, agent="alice", ranked_indexes=None):
    ranked_indexes = ranked_indexes or [{"index": 0, "relevance_score": 1.0}]
    rerank = MagicMock()
    rerank.raise_for_status = MagicMock()
    rerank.json = MagicMock(return_value={"results": ranked_indexes})
    with patch("httpx.AsyncClient") as mock_cls:
        http = AsyncMock()
        http.post = AsyncMock(return_value=rerank)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = await c.handle_search(_request(body, agent=agent))
    return resp


def _whole(resp):
    return resp.text


@pytest.mark.asyncio
async def test_f4_unreadable_neighbour_keeps_a_null_end_without_text():
    """F4: an unreadable or row-less neighbour is named or quoted, its relation is lost, or a private superseded neighbour is dropped while a private live one is kept."""
    c, conn, session = _coord()
    docs = {
        7: _doc(7, FACT_TEXT, metadata={"type": "fact", "source_ref": "notes/lock.py"}),
        8: _doc(8, "private live", visibility="private", agent_id="zoe"),
        9: _doc(9, "private retired", visibility="private", agent_id="zoe",
              superseded=True),
    }
    _run_queries(conn, candidates=[docs[7]], docs=docs)
    session.run = AsyncMock(side_effect=[
        _Rows([_edge(7, "MENTIONS", 8, [ONT.fact]),
               _edge(7, "MENTIONS", 9, [ONT.fact]),
               _edge(7, "MENTIONS", 404, [ONT.fact])]),
        _Rows([]),
    ])
    resp = await _search(c, {"query": "locks", "limit": 5})
    assert resp.status == 200
    ctx = json.loads(resp.text)["results"][0]["graph_context"]
    assert len(ctx) == 3
    raw = _whole(resp)
    assert "private live" not in raw and "private retired" not in raw
    assert "fact:8" not in raw and "fact:9" not in raw and "fact:404" not in raw
    assert "GRAPH-NODE-TEXT" not in raw
    for item in ctx:
        assert item["relation"]["to"] is None
        assert "record" not in item


@pytest.mark.asyncio
async def test_f4a_postgres_failure_drops_record_ends_and_keeps_entities(caplog):
    """F4a: a failed lookup renders node text, names a record far end, or keeps a record-ended relation."""
    c, conn, session = _coord()
    hit = _doc(7, FACT_TEXT)
    _run_queries(conn, candidates=[hit], docs={7: hit})

    real_fetch = conn.fetch

    async def fetch(sql, *args):
        if "successor_id" in sql or "AS content_chars" in sql:
            raise RuntimeError("postgres down")
        return await real_fetch(sql, *args)

    conn.fetch = AsyncMock(side_effect=fetch)
    session.run = AsyncMock(side_effect=[
        _Rows([
            _edge(7, "MENTIONS", 8, [ONT.fact]),
            _edge(7, "MENTIONS", None, [ONT.entity], name="LockOrder"),
        ]),
        _Rows([]),
    ])
    with caplog.at_level(logging.WARNING):
        resp = await _search(c, {"query": "locks", "limit": 5})
    assert resp.status == 200
    ctx = json.loads(resp.text)["results"][0]["graph_context"]
    assert len(ctx) == 1
    assert ctx[0]["relation"]["to"]["name"] == "LockOrder"
    assert "record" not in ctx[0]
    assert "GRAPH-NODE-TEXT" not in resp.text
    assert "fact:8" not in resp.text
    assert any("record hydration failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_f4b_ranked_drops_retired_and_by_ref_marks_them():
    """F4b: a superseded neighbour survives ranked search, or a by-ref neighbour lacks obsolete, or a reversed decision is marked superseded."""
    c, conn, session = _coord()
    live = _doc(7, FACT_TEXT, metadata={"type": "fact"})
    retired = _doc(8, "retired fact body", superseded=True, superseded_by=10,
                   successor_id=10, successor_metadata={"type": "fact"},
                   successor_visibility="global", successor_agent_id=None,
                   successor_scope="global", successor_readable=True)
    decision = _doc(
        3, "decision body", type="decision",
        metadata={"type": "decision", "decision": {"rationale": RATIONALE}},
        superseded=True)
    _run_queries(conn, candidates=[live], docs={7: live, 8: retired, 3: decision, 10: _doc(10, "next")})
    session.run = AsyncMock(side_effect=[
        _Rows([_edge(7, "INFORMED_BY", 8, [ONT.fact])]),
        _Rows([]),
    ])
    ranked = await _search(c, {"query": "locks", "limit": 5})
    assert json.loads(ranked.text)["results"][0]["graph_context"] == []
    assert "retired fact body" not in ranked.text

    c2, conn2, session2 = _coord()
    docs = {3: decision, 8: retired}
    _run_queries(conn2, docs=docs)
    session2.run = AsyncMock(side_effect=[
        _Rows([_edge(3, "INFORMED_BY", 8, [ONT.fact], inbound=False)]),
        _Rows([_marks_row(3, [{"pg_id": 11, "rating": "validated"}], [])]),
    ])
    # The anchor label on a decision edge must be Decision, not Fact.
    resp = await c2.handle_search(_request({"refs": ["decision:3"]}))
    assert resp.status == 200
    hit = json.loads(resp.text)["results"][0]
    assert hit["obsolete"] == "reversed"
    neighbour = hit["graph_context"][0]["record"]
    assert neighbour["obsolete"] == "superseded"
    assert neighbour["ref"] == "fact:8"


@pytest.mark.asyncio
async def test_f6_decision_neighbour_text_is_the_whole_rationale():
    """F6: a decision entry's text is not the whole rationale, adr_props is present, or a blank rationale yields text ''."""
    c, conn, session = _coord()
    blank = _doc(4, "legacy decision content", type="decision",
                 metadata={"type": "decision", "decision": {"rationale": "  "}})
    full = _doc(5, "title plus body", type="decision",
                metadata={"type": "decision", "decision": {"rationale": RATIONALE}})
    anchor = _doc(7, FACT_TEXT)
    _run_queries(conn, candidates=[anchor], docs={7: anchor, 4: blank, 5: full})
    session.run = AsyncMock(side_effect=[
        _Rows([
            _edge(7, "INFORMED_BY", 5, [ONT.decision]),
            _edge(7, "INFORMED_BY", 4, [ONT.decision]),
        ]),
        _Rows([]),
    ])
    resp = await _search(c, {"query": "locks", "limit": 5})
    ctx = json.loads(resp.text)["results"][0]["graph_context"]
    by_ref = {item["record"]["ref"]: item["record"] for item in ctx}
    assert by_ref["decision:5"]["text"] == RATIONALE
    assert by_ref["decision:5"]["text_is"] == "summary"
    assert by_ref["decision:4"]["text"] == "legacy decision content"
    assert by_ref["decision:4"]["text"] != ""
    raw = resp.text
    assert "adr_props" not in raw
    assert "alternatives" not in raw


@pytest.mark.asyncio
async def test_f15_ranked_lifecycle_hides_an_unreadable_retrospective():
    """F15: ranked-search lifecycle names or quotes a retrospective this caller cannot read."""
    c, conn, session = _coord()
    decision = _doc(7, "decision body", type="decision",
                    metadata={"type": "decision", "decision": {"rationale": RATIONALE}})
    retro = _doc(11, SECRET, type="retrospective",
                 metadata={"type": "retrospective"},
                 visibility="private", agent_id="zoe")
    _run_queries(conn, candidates=[decision], docs={7: decision, 11: retro})

    async def run(cypher, **kwargs):
        # The pre-1.1 lifecycle query must still be able to leak. A marks-shaped
        # row alone never reaches that query, so the test would stay green.
        if "ORDER BY r.pg_id DESC" in cypher:
            return _Rows([{"pg_id": 7, "rating": "refined", "retro_pg_id": 11}])
        if "outcomes" in cypher or "grounds" in cypher:
            return _Rows([_marks_row(7, [{"pg_id": 11, "rating": "refined"}], [])])
        return _Rows([])

    session.run = AsyncMock(side_effect=run)
    resp = await _search(c, {"query": "locks", "limit": 5}, agent="alice")
    hit = json.loads(resp.text)["results"][0]
    assert "lifecycle" not in hit
    assert SECRET not in resp.text
    assert "retrospective:11" not in resp.text


@pytest.mark.asyncio
async def test_f16_superseded_by_omits_an_unreadable_successor():
    """F16: superseded_by names a successor this caller cannot read, or a reversed decision has no pointer to its readable reversing retrospective."""
    c, conn, session = _coord()
    fact = _doc(7, FACT_TEXT, superseded=True, superseded_by=9,
                successor_id=9, successor_metadata={"type": "fact"},
                successor_visibility="private", successor_agent_id="zoe",
                successor_scope="global")
    decision = _doc(3, "decision body", type="decision", superseded=True,
                    metadata={"type": "decision", "decision": {"rationale": RATIONALE}})
    retro = _doc(11, "the reversal", type="retrospective",
                 metadata={"type": "retrospective"})
    _run_queries(conn, docs={7: fact, 3: decision, 11: retro, 9: _doc(9, "hidden next", visibility="private", agent_id="zoe")})

    async def run(cypher, **kwargs):
        text = " ".join(cypher.split())
        if "AS decision_id" in text:
            return _Rows([_marks_row(3, [{"pg_id": 11, "rating": "reversed"}], [])])
        return _Rows([])

    session.run = AsyncMock(side_effect=run)
    resp = await c.handle_search(_request({"refs": ["fact:7", "decision:3"]}))
    hits = {h["ref"]: h for h in json.loads(resp.text)["results"]}
    assert hits["fact:7"]["obsolete"] == "superseded"
    assert "superseded_by" not in hits["fact:7"]
    assert "fact:9" not in resp.text
    assert hits["decision:3"]["obsolete"] == "reversed"
    assert hits["decision:3"]["superseded_by"] == "retrospective:11"


@pytest.mark.asyncio
async def test_f17_one_hydration_pass_and_no_per_hit_cypher():
    """F17: more than two hydration queries, or a marks query per decision hit.

    Two decision hits plus one summary. A per-hit marks loop calls session.run
    once per decision and this count is no longer one.
    """
    c, conn, session = _coord()
    first = _doc(7, FACT_TEXT, type="decision",
                 metadata={"type": "decision", "decision": {"rationale": "one"}})
    second = _doc(9, FACT_TEXT, type="decision",
                  metadata={"type": "decision", "decision": {"rationale": "two"}})
    summary = {
        "id": 8, "content": "thematic text", "content_chars": 13,
        "metadata": {"kind": "thematic"}, "source_pg_ids": [],
        "superseded": False, "visibility": "global", "agent_id": None,
        "scope": "global",
    }
    calls = {"fetch": [], "run": 0, "marks": 0}

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        calls["fetch"].append(text)
        if "ORDER BY embedding" in text and "community_summaries" not in text:
            return [first, second]
        if "successor_id" in text and "community_summaries" in text:
            return [summary]
        if "successor_id" in text:
            return [first, second]
        return []

    async def fetchrow(sql, *args):
        text = " ".join(sql.split())
        calls["fetch"].append(text)
        if "kind' = 'insight'" in text:
            return summary
        return None

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    async def run(cypher, **kwargs):
        calls["run"] += 1
        if "decision_id" in cypher or "outcomes" in cypher:
            calls["marks"] += 1
        return _Rows([])

    session.run = AsyncMock(side_effect=run)
    resp = await _search(
        c, {"query": "locks", "limit": 5},
        ranked_indexes=[{"index": 0, "relevance_score": 3.0},
                        {"index": 1, "relevance_score": 2.0},
                        {"index": 2, "relevance_score": 1.0}])
    assert resp.status == 200
    body = json.loads(resp.text)
    decision_hits = [h for h in body["results"] if h.get("record_type") == "decision"]
    assert len(decision_hits) >= 2
    hydration = [sql for sql in calls["fetch"] if "successor_id" in sql or "AS content_chars" in sql]
    assert 1 <= len(hydration) <= 2
    assert calls["marks"] == 1
    assert calls["run"] <= 4
    assert conn.fetchval.await_count == 0
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_f17_by_ref_hydration_and_neo4j_stay_bounded():
    """F17: search by ref of two decisions hydrates more than twice or opens more than four Neo4j runs.

    A per-hit marks loop makes the marks count two.
    """
    c, conn, session = _coord()
    first = _doc(7, FACT_TEXT, type="decision",
                 metadata={"type": "decision", "decision": {"rationale": "one"}})
    second = _doc(9, FACT_TEXT, type="decision",
                  metadata={"type": "decision", "decision": {"rationale": "two"}})
    _run_queries(conn, docs={7: first, 9: second})
    calls = {"run": 0, "marks": 0}

    async def run(cypher, **kwargs):
        calls["run"] += 1
        if "decision_id" in cypher or "outcomes" in cypher:
            calls["marks"] += 1
        return _Rows([])

    session.run = AsyncMock(side_effect=run)
    resp = await c.handle_search(_request({"refs": ["decision:7", "decision:9"]}))
    assert resp.status == 200
    hydration = [
        " ".join(call.args[0].split())
        for call in conn.fetch.await_args_list
        if "successor_id" in call.args[0]
    ]
    assert len(hydration) <= 2
    assert calls["marks"] == 1
    assert calls["run"] <= 4


@pytest.mark.asyncio
async def test_f23_keyword_fallback_applies_marks_with_the_viewer():
    """F23: the keyword fallback returns a decision without retired_grounds, a retrospective without latest_verdict, or lifecycle.ref other than the latest readable retrospective."""
    c, conn, session = _coord()
    c._embed = AsyncMock(return_value=None)
    decision = _doc(7, "decision body", type="decision",
                    metadata={"type": "decision", "decision": {"rationale": RATIONALE}})
    ground = _doc(4, "old evidence", superseded=True, superseded_by=5,
                  successor_id=5, successor_metadata={"type": "fact"},
                  successor_visibility="global", successor_scope="global")
    nxt = _doc(5, "new evidence")
    retro = _doc(2, "older verdict", type="retrospective",
                 metadata={"type": "retrospective"})
    later = _doc(3, "the verdict that counts", type="retrospective",
                 metadata={"type": "retrospective"})
    hidden = _doc(9, SECRET, type="retrospective", metadata={"type": "retrospective"},
                  visibility="private", agent_id="zoe")
    docs = {7: decision, 4: ground, 5: nxt, 2: retro, 3: later, 9: hidden}

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "ILIKE" in text:
            return [decision, retro]
        if "successor_id" in text or "AS content_chars" in text:
            wanted = set(args[0])
            return [docs[i] for i in docs if i in wanted]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    session.run = AsyncMock(return_value=_Rows([
        _marks_row(7, [{"pg_id": 9, "rating": "refined"}],
                   [{"pg_id": 4, "relation": "INFORMED_BY"}]),
    ]))
    # Retrospective 2's siblings are produced from the decision's outcomes.
    # Add the later readable sibling on the same decision.
    session.run = AsyncMock(return_value=_Rows([
        _marks_row(
            7,
            [{"pg_id": 2, "rating": "validated"},
             {"pg_id": 3, "rating": "refined"},
             {"pg_id": 9, "rating": "refined"}],
            [{"pg_id": 4, "relation": "INFORMED_BY"}]),
    ]))
    resp = await c.handle_search(_request({"query": "locks", "limit": 5}, agent="alice"))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["fallback"] == "keyword"
    hits = {h["ref"]: h for h in body["results"]}
    assert hits["decision:7"]["retired_grounds"] == [{
        "ref": "fact:4", "relation": "INFORMED_BY", "superseded_by": "fact:5",
    }]
    assert hits["decision:7"]["lifecycle"]["ref"] == "retrospective:3"
    assert SECRET not in resp.text
    assert hits["retrospective:2"]["latest_verdict"] == "retrospective:3"


@pytest.mark.asyncio
async def test_f25_reference_fields_omit_unreadable_ids():
    """F25: a hit metadata id list, summary_refs, a retired-summary ref, or stale_sources names a record this caller cannot read."""
    c, conn, session = _coord()
    summary = {
        "id": 8, "content": "thematic", "content_chars": 8,
        "metadata": {"kind": "insight", "summary_ids": [12, 13], "source_pg_ids": [1, 2]},
        "source_pg_ids": [1, 2],
        "superseded": False, "visibility": "global", "agent_id": None, "scope": "global",
    }
    visible = {"id": 12, "metadata": {"kind": "summary"}, "visibility": "global",
               "agent_id": None, "scope": "global", "content": "seen", "content_chars": 4,
               "superseded": False}
    hidden = {"id": 13, "metadata": {"kind": "summary"}, "visibility": "private",
              "agent_id": "zoe", "scope": "global", "content": "SECRET-SUMMARY",
              "content_chars": 14, "superseded": True, "superseded_reason": "coverage",
              "superseded_by": 99, "source_pg_ids": []}
    src_hidden = _doc(2, "secret source", visibility="private", agent_id="zoe", superseded=True,
                      superseded_by=50)
    src_seen = _doc(1, "seen source", superseded=True, superseded_by=40)

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "ORDER BY embedding" in text and "technical_docs" in text:
            return [src_seen]
        if "successor_id" in text and "community_summaries" in text:
            return [summary, visible, hidden]
        if "successor_id" in text:
            return [src_seen, src_hidden]
        if "t.superseded" in text:
            return [src_seen, src_hidden]
        if "superseded_reason" in text:
            return [visible, hidden]
        if text.startswith("SELECT id, metadata"):
            return [visible, hidden]
        return []

    async def fetchrow(sql, *args):
        text = " ".join(sql.split())
        if "kind' = 'insight'" in text:
            return summary
        if "community_summaries" in text:
            return None
        return None

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    session.run = AsyncMock(return_value=_Rows([]))
    resp = await _search(
        c, {"query": "locks", "limit": 5}, agent="alice",
        ranked_indexes=[{"index": 0, "relevance_score": 1.0}])
    assert resp.status == 200
    raw = resp.text
    assert "SECRET-SUMMARY" not in raw
    assert "summary:13" not in raw
    assert "secret source" not in raw
    hit = json.loads(raw)["results"][0]
    assert 13 not in (hit["metadata"].get("summary_ids") or [])
    assert 2 not in (hit.get("source_pg_ids") or [])
    assert all(item.get("old") != 2 for item in hit.get("stale_sources") or [])


@pytest.mark.asyncio
async def test_f26_summary_successor_uses_its_own_kind_and_visibility():
    """F26: a summary successor is given the predecessor's kind, named when private, or named when the successor row is missing."""
    c, conn, session = _coord()
    thematic = {
        "id": 8, "content": "old thematic", "content_chars": 12,
        "metadata": {"kind": "thematic"}, "source_pg_ids": [],
        "superseded": True, "superseded_by": 9,
        "visibility": "global", "agent_id": None, "scope": "global",
        "successor_id": 9, "successor_metadata": {"kind": "insight"},
        "successor_visibility": "global", "successor_agent_id": None,
        "successor_scope": "global",
    }
    private_succ = dict(thematic)
    private_succ["successor_visibility"] = "private"
    private_succ["successor_agent_id"] = "zoe"
    missing = dict(thematic)
    missing.pop("successor_id")
    session.run = AsyncMock(return_value=_Rows([]))

    async def one(row):
        c1, conn1, session1 = _coord()
        session1.run = AsyncMock(return_value=_Rows([]))

        async def fetch(sql, *args):
            if "community_summaries" in sql:
                return [row]
            return []

        conn1.fetch = AsyncMock(side_effect=fetch)
        resp = await c1.handle_search(_request({"refs": ["summary:8"]}))
        return json.loads(resp.text)["results"][0]

    marked = await one(thematic)
    assert marked["obsolete"] == "superseded"
    assert marked["superseded_by"] == "insight:9"
    hidden = await one(private_succ)
    assert "superseded_by" not in hidden
    assert "insight:9" not in json.dumps(hidden)
    gone = await one(missing)
    assert "superseded_by" not in gone


@pytest.mark.asyncio
async def test_f28_fact_provenance_comes_from_postgres_metadata():
    """F28: a fact entry carries adr_props, or fact_kind and source_ref taken from the node, or no fact_kind when metadata holds only source_ref."""
    c, conn, session = _coord()
    anchor = _doc(7, FACT_TEXT, metadata={"type": "fact", "source_ref": "src/lock.py"})
    neighbour = _doc(8, "x" * 50, metadata={"type": "fact", "source_ref": "src/lock.py"})
    _run_queries(conn, candidates=[anchor], docs={7: anchor, 8: neighbour})
    session.run = AsyncMock(side_effect=[
        _Rows([_edge(7, "MENTIONS", 8, [ONT.fact], rem_summary=NODE_SUMMARY)]),
        _Rows([]),
    ])
    resp = await _search(c, {"query": "locks", "limit": 5})
    record = json.loads(resp.text)["results"][0]["graph_context"][0]["record"]
    assert record["source_ref"] == "src/lock.py"
    assert record["fact_kind"] == "measured"
    assert record["text"] == NODE_SUMMARY
    assert record["text_is"] == "summary"
    assert "adr_props" not in record
    assert "from-the-node" not in resp.text
    assert "tested" not in json.dumps(record)


@pytest.mark.asyncio
async def test_f29_by_ref_reversed_decision_ignores_a_later_readable_rating():
    """F29: a decision read by ref says superseded when the Postgres flag is set and the latest readable retrospective is not reversed."""
    c, conn, session = _coord()
    decision = _doc(3, "decision body", type="decision", superseded=True,
                    metadata={"type": "decision", "decision": {"rationale": RATIONALE}})
    retro = _doc(11, "still useful", type="retrospective",
                 metadata={"type": "retrospective"})
    _run_queries(conn, docs={3: decision, 11: retro})
    session.run = AsyncMock(return_value=_Rows([
        _marks_row(3, [{"pg_id": 11, "rating": "validated"}], []),
    ]))
    resp = await c.handle_search(_request({"refs": ["decision:3"]}))
    hit = json.loads(resp.text)["results"][0]
    assert hit["obsolete"] == "reversed"
    assert hit.get("lifecycle") == {"rating": "reversed"}
    assert "superseded_by" not in hit
    assert "retrospective:11" not in json.dumps(hit.get("lifecycle"))


@pytest.mark.asyncio
async def test_f31_marks_failure_warns_once_and_unreadable_reversal_hides_the_earlier_verdict(caplog):
    """F31: a marks-query failure logs no warning, or a reversed decision whose reversing retrospective is unreadable shows an earlier readable verdict."""
    c, conn, session = _coord()
    decision = _doc(3, "decision body", type="decision", superseded=True,
                    metadata={"type": "decision", "decision": {"rationale": RATIONALE}})
    earlier = _doc(10, "validated earlier", type="retrospective",
                   metadata={"type": "retrospective"})
    hidden = _doc(11, SECRET, type="retrospective", metadata={"type": "retrospective"},
                  visibility="private", agent_id="zoe")
    _run_queries(conn, docs={3: decision, 10: earlier, 11: hidden})

    class _Boom:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("neo4j marks down")

    session.run = AsyncMock(return_value=_Boom())
    with caplog.at_level(logging.WARNING):
        resp = await c.handle_search(_request({"refs": ["decision:3"]}))
    assert resp.status == 200
    hit = json.loads(resp.text)["results"][0]
    assert "lifecycle" not in hit
    assert "retired_grounds" not in hit
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "retired_grounds" in warnings[0].message
    assert "latest_verdict" in warnings[0].message
    assert "lifecycle" in warnings[0].message

    c2, conn2, session2 = _coord()
    _run_queries(conn2, docs={3: decision, 10: earlier, 11: hidden})
    session2.run = AsyncMock(return_value=_Rows([
        _marks_row(3, [{"pg_id": 10, "rating": "validated"},
                       {"pg_id": 11, "rating": "reversed"}], []),
    ]))
    resp2 = await c2.handle_search(_request({"refs": ["decision:3"]}))
    life = json.loads(resp2.text)["results"][0]["lifecycle"]
    assert life == {"rating": "reversed"}
    assert SECRET not in resp2.text
    assert "retrospective:10" not in resp2.text


@pytest.mark.asyncio
async def test_f32_search_drops_a_legacy_self_loop():
    """F32: a legacy self-loop emits the anchor as its own neighbour."""
    c, conn, session = _coord()
    anchor = _doc(7, FACT_TEXT)
    other = _doc(8, "other fact")
    _run_queries(conn, candidates=[anchor], docs={7: anchor, 8: other})
    loop = _edge(7, "HAD_OUTCOME", 7, [ONT.fact])
    loop["start_pg_id"] = 7
    loop["end_pg_id"] = 7
    loop["props_notes"] = "retrospective text that must stay off the edge"
    loop["rel_props"] = {"notes": "retrospective text that must stay off the edge",
                         "asserted_by": "agent"}
    session.run = AsyncMock(side_effect=[
        _Rows([loop, _edge(7, "MENTIONS", 8, [ONT.fact],
                           props={"notes": "nope", "asserted_by": "agent"})]),
        _Rows([]),
    ])
    resp = await _search(c, {"query": "locks", "limit": 5})
    ctx = json.loads(resp.text)["results"][0]["graph_context"]
    assert len(ctx) == 1
    assert ctx[0]["record"]["ref"] == "fact:8"
    assert "notes" not in ctx[0]["relation"]["props"]
    assert ctx[0]["relation"]["props"]["asserted_by"] == "agent"
    assert "retrospective text" not in resp.text
