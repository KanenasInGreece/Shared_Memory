"""Search by ref: the second read step. Each test names the failure it catches.

Fixtures use demo-project and invented text. metadata arrives as a JSON string
and created_at as a datetime, so a missing coercion or isoformat fails.
"""
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

import coordinator as coordinator_mod  # noqa: E402
from coordinator import MemoryCoordinator, ONT  # noqa: E402


FACT_TEXT = "The demo ledger sorts lock names before either agent acquires them."
SUMMARY_TEXT = "The demo project keeps one lock order and writes it down once."
DECISION_TEXT = "The demo project ships the sorted lock order and retires the global lock."
SECRET = "SECRET-PRIVATE-demo-ledger-text-must-not-leak"
RETRO_SECRET = "SECRET-RETRO-the private retrospective that reversed the demo decision"
CREATED = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
CREATED_ISO = "2026-10-06T12:00:00+00:00"
DECISION_META = json.dumps({
    "type": "decision",
    "decision": {"alternatives": ["keep the ledger", "drop the ledger"]},
})


class _AsyncCtx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *args):
        return None


def _request(body, agent=None):
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
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    c._pool = pool
    session = MagicMock()
    session.run = AsyncMock()
    neo = MagicMock()
    neo.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo
    c._embed = AsyncMock(return_value=[0.1, 0.2])
    return c, conn


def _doc(pg_id, content, **extra):
    row = {
        "id": pg_id,
        "content": content,
        "metadata": json.dumps({"type": "fact", "project": "demo-project"}),
        "created_at": CREATED,
        "superseded": False,
        "superseded_by": None,
        "visibility": "global",
        "agent_id": None,
        "scope": None,
    }
    row.update(extra)
    return row


def _summary(pg_id, content, kind="thematic", **extra):
    row = {
        "id": pg_id,
        "content": content,
        "metadata": json.dumps({"kind": kind, "project": "demo-project"}),
        "source_pg_ids": [],
        "superseded": False,
        "superseded_by": None,
        "visibility": "global",
        "agent_id": None,
        "scope": None,
    }
    row.update(extra)
    return row


def _asked(rows, args):
    """Rows whose id was requested. A fetch that ignores its ids cannot prove a second lookup."""
    if not rows:
        return []
    if args and isinstance(args[0], (list, tuple, set)):
        wanted = set(args[0])
        return [row for row in rows if row.get("id") in wanted]
    return list(rows)


def _route(docs, summaries, *, retro_rows=None, stale_rows=None, retired_rows=None,
           succ_rows=None, ref_rows=None):
    """One fetch dispatcher. Tables are chosen from the SQL, never from the id."""

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "metadata->>'type' AS type" in text:
            return list(succ_rows or [])
        if text.startswith("SELECT id, metadata FROM community_summaries"):
            return _asked(ref_rows, args)
        if "superseded_reason" in text and "community_summaries" in text:
            return _asked(retired_rows, args)
        if "superseded_by" in text and "AND superseded" in text and "technical_docs" in text:
            return list(stale_rows or [])
        if text.startswith("SELECT id, content FROM technical_docs"):
            return list(retro_rows or [])
        if "SELECT id, content, visibility" in text:
            return list(retro_rows or [])
        if "FROM technical_docs" in text and "created_at" in text:
            return [docs[i] for i in docs if i in set(args[0])] if args else list(docs.values())
        if "FROM community_summaries" in text and "source_pg_ids" in text and "visibility" in text:
            return [summaries[i] for i in summaries if i in set(args[0])] if args else list(summaries.values())
        if "ORDER BY embedding" in text or "embedding <=>" in text:
            return list(docs.values())[:1]
        return []

    return fetch


async def _by_ref(body, agent=None, docs=None, summaries=None, **route):
    c, conn = _coord()
    conn.fetch = AsyncMock(side_effect=_route(docs or {}, summaries or {}, **route))
    resp = await c.handle_search(_request(body, agent=agent))
    return resp, c, conn


# ── R1 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r1_by_ref_skips_embed_rerank_and_nearest_summary():
    """R1: A by-ref search calls the embedder or the reranker, or runs a nearest-summary query."""
    docs = {1: _doc(1, FACT_TEXT)}
    with patch("httpx.AsyncClient", new_callable=AsyncMock) as mock_cls:
        resp, c, conn = await _by_ref({"refs": ["fact:1"]}, docs=docs)
        assert mock_cls.await_count == 0
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["results"][0]["content"] == FACT_TEXT
    assert body["results"][0]["ref"] == "fact:1"
    assert c._embed.await_count == 0
    for call in conn.fetch.await_args_list:
        assert "embedding" not in call.args[0]


# ── R2 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r2_same_integer_is_two_records_when_the_tables_differ():
    """R2: A named record comes back cut, or from the wrong table."""
    docs = {7: _doc(7, FACT_TEXT)}
    summaries = {7: _summary(7, SUMMARY_TEXT)}
    resp, _, _ = await _by_ref(
        {"refs": ["fact:7", "summary:7"]}, docs=docs, summaries=summaries)
    results = json.loads(resp.text)["results"]
    assert results[0]["content"] == FACT_TEXT
    assert results[0]["ref"] == "fact:7"
    assert results[0]["tier"] == "fact"
    assert results[1]["content"] == SUMMARY_TEXT
    assert results[1]["ref"] == "summary:7"
    assert results[1]["tier"] == "community_summary"


# ── R3 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r3_order_is_the_order_asked_and_duplicates_collapse_on_the_string():
    """R3: Results are not in the order asked, a repeated ref is emitted twice, or a bare id and its qualified form collapse."""
    docs = {7: _doc(7, FACT_TEXT)}
    summaries = {3: _summary(3, SUMMARY_TEXT)}
    resp, _, _ = await _by_ref(
        {"refs": ["summary:3", "fact:7", "fact:7", "7"]},
        docs=docs, summaries=summaries)
    results = json.loads(resp.text)["results"]
    assert [r["ref"] for r in results] == ["summary:3", "fact:7", "fact:7"]
    assert results[1]["content"] == FACT_TEXT
    assert results[2]["content"] == FACT_TEXT
    assert len(results) == 3


# ── R4 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r4_unreadable_records_return_not_visible_and_nothing_else():
    """R4: Text, metadata, or type of an unreadable record reaches the caller."""
    docs = {
        7: _doc(7, SECRET, visibility="private", agent_id="alice"),
        8: _doc(8, SECRET, visibility="scope", agent_id="alice", scope="ops"),
    }
    summaries = {4: _summary(4, SECRET, visibility="private", agent_id="alice")}
    seen = []

    async def _expand(session, pg_ids, labels, viewer=None, viewer_scope=None):
        seen.extend(list(pg_ids))
        return {}

    c, conn = _coord()
    c._expand_graph_context_batch = _expand
    conn.fetch = AsyncMock(side_effect=_route(docs, summaries))

    async def _one(body, agent):
        resp = await c.handle_search(_request(body, agent=agent))
        assert resp.status == 200
        return json.loads(resp.text), resp.text

    hidden, raw = await _one({"refs": ["fact:7"]}, agent="bob")
    assert hidden["results"][0] == {
        "ref": "fact:7", "by_ref": True, "found": False, "reason": "not_visible",
    }
    assert SECRET not in raw
    assert 7 not in seen

    anon, raw_anon = await _one({"refs": ["fact:7"]}, agent=None)
    assert anon["results"][0] == {
        "ref": "fact:7", "by_ref": True, "found": False, "reason": "not_visible",
    }
    assert SECRET not in raw_anon

    scoped, raw_scoped = await _one({"refs": ["fact:8"]}, agent="bob")
    assert scoped["results"][0] == {
        "ref": "fact:8", "by_ref": True, "found": False, "reason": "not_visible",
    }
    assert SECRET not in raw_scoped

    private_sum, raw_sum = await _one({"refs": ["summary:4"]}, agent="bob")
    assert private_sum["results"][0] == {
        "ref": "summary:4", "by_ref": True, "found": False, "reason": "not_visible",
    }
    assert SECRET not in raw_sum

    wrong, raw_wrong = await _one({"refs": ["decision:7"]}, agent="bob")
    assert wrong["results"][0] == {
        "ref": "decision:7", "by_ref": True, "found": False, "reason": "not_visible",
    }
    assert "actual_ref" not in wrong["results"][0]
    assert SECRET not in raw_wrong
    assert 7 not in seen

    owner, _ = await _one({"refs": ["fact:7"]}, agent="alice")
    assert owner["results"][0]["content"] == SECRET
    assert owner["results"][0]["by_ref"] is True
    assert "found" not in owner["results"][0]

    scope_ok, _ = await _one({"refs": ["fact:8"], "scope": "ops"}, agent="bob")
    assert scope_ok["results"][0]["content"] == SECRET
    assert scope_ok["results"][0]["ref"] == "fact:8"


# ── R5 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r5_missing_and_wrong_type_on_a_readable_record():
    """R5: A missing id is not reason missing, or a wrong-type ref on a readable record returns text or lacks actual_ref."""
    docs = {7: _doc(7, FACT_TEXT, metadata=DECISION_META)}
    resp, _, _ = await _by_ref({"refs": ["fact:99", "fact:7"]}, docs=docs)
    results = json.loads(resp.text)["results"]
    assert results[0] == {
        "ref": "fact:99", "by_ref": True, "found": False, "reason": "missing",
    }
    assert results[1]["reason"] == "wrong_type"
    assert results[1]["actual_ref"] == "decision:7"
    assert "content" not in results[1]
    assert FACT_TEXT not in json.dumps(results[1])


# ── R6 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r6_obsolete_records_are_returned_and_named():
    """R6: A superseded fact is hidden or its pointer is not the qualified ref; a retraction carries superseded_by; a live record carries obsolete."""
    docs = {
        7: _doc(7, FACT_TEXT, superseded=True, superseded_by=9),
        3: _doc(3, "retracted demo fact with no successor", superseded=True, superseded_by=None),
        4: _doc(4, "live demo fact"),
    }
    summaries = {
        8: _summary(8, SUMMARY_TEXT, superseded=True, superseded_by=12),
    }
    resp, _, _ = await _by_ref(
        {"refs": ["fact:7", "summary:8", "fact:3", "fact:4"]},
        docs=docs, summaries=summaries,
        succ_rows=[{"id": 9, "type": "fact"}],
    )
    results = {r["ref"]: r for r in json.loads(resp.text)["results"]}
    assert results["fact:7"]["content"] == FACT_TEXT
    assert results["fact:7"]["obsolete"] == "superseded"
    assert results["fact:7"]["superseded_by"] == "fact:9"
    assert results["summary:8"]["obsolete"] == "superseded"
    assert results["summary:8"]["superseded_by"] == "summary:12"
    assert results["fact:3"]["obsolete"] == "superseded"
    assert "superseded_by" not in results["fact:3"]
    assert "obsolete" not in results["fact:4"]


# ── R7 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r7_by_ref_entries_are_unranked_and_carry_by_ref():
    """R7: A found entry carries a rank field, or a not-found entry lacks by_ref, or the body carries fallback or filters_resolved."""
    docs = {1: _doc(1, FACT_TEXT)}
    resp, _, _ = await _by_ref({"refs": ["fact:1", "fact:404"]}, docs=docs)
    body = json.loads(resp.text)
    assert "fallback" not in body
    assert "filters_resolved" not in body
    found, missing = body["results"]
    for key in (
        "score", "score_normalized", "ranked", "rerank_payload_chars",
        "rerank_payload_docs", "matched_entities", "fallback", "filters_resolved",
    ):
        assert key not in found
        assert key not in missing
    assert found["by_ref"] is True
    assert missing["by_ref"] is True
    assert missing["found"] is False


# ── R8 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r8_refs_invalid_before_any_database_call():
    """R8: A bad refs list is not 400 refs_invalid, or the database is touched, or a bad limit beside a valid refs list refuses the call."""
    bad = [
        {"refs": "fact:1"},
        {"refs": None},
        {"refs": []},
        {"refs": [7]},
        {"refs": ["not a ref"]},
        {"refs": ["fact:1"] * 17},
        {"refs": ["fact:" + "9" * 30]},
        {"refs": ["fact:0"]},
    ]
    for body in bad:
        c, conn = _coord()
        c._acquire = MagicMock(side_effect=AssertionError("acquire"))
        resp = await c.handle_search(_request(body))
        assert resp.status == 400, body
        payload = json.loads(resp.text)
        assert payload["error"] == "refs_invalid"
        assert payload["status"] == "error"
        assert c._acquire.call_count == 0
        assert conn.fetch.await_count == 0

    ok, c_ok, _ = await _by_ref({"refs": ["fact:1"], "limit": "x"}, docs={1: _doc(1, FACT_TEXT)})
    assert ok.status == 200
    assert json.loads(ok.text)["results"][0]["content"] == FACT_TEXT
    assert c_ok._embed.await_count == 0


# ── R9 ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r9_found_decision_keeps_its_metadata_object():
    """R9: A found decision lacks its metadata, or the alternatives list was left a string."""
    docs = {7: _doc(7, DECISION_TEXT, metadata=DECISION_META)}
    resp, _, _ = await _by_ref({"refs": ["decision:7"]}, docs=docs)
    hit = json.loads(resp.text)["results"][0]
    assert hit["metadata"]["decision"]["alternatives"] == ["keep the ledger", "drop the ledger"]
    assert hit["created_at"] == CREATED_ISO
    assert hit["content"] == DECISION_TEXT


# ── R10 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r10_graph_context_is_filled_and_a_closed_graph_does_not_fail_the_read():
    """R10: A found entry lacks graph_context, the two anchor labels are not separate calls, or opening Neo4j fails the read."""
    docs = {7: _doc(7, FACT_TEXT)}
    summaries = {8: _summary(8, SUMMARY_TEXT)}
    calls = []

    async def _expand(session, pg_ids, labels, viewer=None, viewer_scope=None):
        calls.append((labels, list(pg_ids)))
        return {pid: [{"name": "SENTINEL-NEIGHBOUR"}] for pid in pg_ids}

    c, conn = _coord()
    c._expand_graph_context_batch = _expand
    conn.fetch = AsyncMock(side_effect=_route(docs, summaries))
    resp = await c.handle_search(_request({"refs": ["fact:7", "summary:8"]}))
    results = json.loads(resp.text)["results"]
    assert results[0]["graph_context"] == [{"name": "SENTINEL-NEIGHBOUR"}]
    assert results[1]["graph_context"] == [{"name": "SENTINEL-NEIGHBOUR"}]
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]
    assert (ONT.community_summary,) in (calls[0][0], calls[1][0])
    assert (ONT.fact, ONT.decision, ONT.retrospective) in (calls[0][0], calls[1][0])

    c2, conn2 = _coord()
    c2._neo4j.session = MagicMock(side_effect=RuntimeError("graph down"))
    conn2.fetch = AsyncMock(side_effect=_route(docs, {}))
    resp2 = await c2.handle_search(_request({"refs": ["fact:7"]}))
    assert resp2.status == 200
    hit = json.loads(resp2.text)["results"][0]
    assert hit["content"] == FACT_TEXT
    assert hit["graph_context"] == []


# ── R11 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r11_a_body_without_refs_is_a_normal_search():
    """R11: A body without a refs key takes the by-ref path, or its hits carry by_ref."""
    c, conn = _coord()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": FACT_TEXT,
         "metadata": {"type": "fact", "project": "demo-project"},
         "created_at": CREATED},
    ])
    rerank = MagicMock()
    rerank.raise_for_status = MagicMock()
    rerank.json = MagicMock(return_value={"results": [{"index": 0, "relevance_score": 1.5}]})
    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=rerank)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = await c.handle_search(_request({"query": "demo ledger", "limit": 5}))
    assert resp.status == 200
    assert c._embed.await_count == 1
    hit = json.loads(resp.text)["results"][0]
    assert "by_ref" not in hit
    assert hit["content"] == FACT_TEXT


# ── R12 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r12_a_bare_integer_is_a_technical_docs_row():
    """R12: A bare integer ref is looked up in community_summaries."""
    docs = {7: _doc(7, FACT_TEXT)}
    summaries = {7: _summary(7, SUMMARY_TEXT)}
    resp, _, conn = await _by_ref({"refs": ["7"]}, docs=docs, summaries=summaries)
    hit = json.loads(resp.text)["results"][0]
    assert hit["content"] == FACT_TEXT
    assert hit["tier"] == "fact"
    sqls = [call.args[0] for call in conn.fetch.await_args_list]
    assert any("technical_docs" in sql for sql in sqls)
    assert not any("community_summaries" in sql for sql in sqls)


# ── R13 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r13_a_found_decision_carries_lifecycle_and_reversed_wins():
    """R13: A found decision lacks lifecycle, or a reversed verdict lacks obsolete reversed."""
    docs = {7: _doc(7, DECISION_TEXT, metadata=DECISION_META, superseded=True, superseded_by=9)}
    c, conn = _coord()
    c._resolve_decision_lifecycle = AsyncMock(return_value={
        7: {"rating": "reversed", "retrospective_pg_id": 11},
    })
    conn.fetch = AsyncMock(side_effect=_route(
        docs, {},
        retro_rows=[{"id": 11, "content": "the reversing note",
                     "visibility": "global", "agent_id": None, "scope": None}],
        succ_rows=[{"id": 9, "type": "fact"}],
    ))
    resp = await c.handle_search(_request({"refs": ["decision:7"]}, agent="alice"))
    hit = json.loads(resp.text)["results"][0]
    assert hit["lifecycle"]["rating"] == "reversed"
    assert hit["lifecycle"]["ref"] == "retrospective:11"
    assert hit["obsolete"] == "reversed"


# ── R14 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r14_found_summaries_carry_stale_sources_and_retired_summaries():
    """R14: A found summary with a superseded source lacks stale_sources, or a found insight lacks retired_summaries."""
    summaries = {
        8: _summary(8, SUMMARY_TEXT, source_pg_ids=[501]),
        9: _summary(
            9, "An insight over a retired demo summary.",
            kind="insight",
            metadata=json.dumps({
                "kind": "insight", "summary_ids": [12], "project": "demo-project",
            }),
            source_pg_ids=[],
        ),
    }
    resp, _, _ = await _by_ref(
        {"refs": ["summary:8", "insight:9"]},
        summaries=summaries,
        stale_rows=[{"id": 501, "superseded_by": 900}],
        retired_rows=[{
            "id": 12, "superseded": True, "superseded_reason": "coverage",
            "superseded_by": None, "source_pg_ids": [],
        }],
    )
    results = json.loads(resp.text)["results"]
    assert results[0]["stale_sources"] == [{"old": 501, "superseded_by": 900}]
    assert results[1]["retired_summaries"] == [{
        "summary_id": 12, "superseded_reason": "coverage",
        "superseded_by": None, "unsupported": [],
        "ref": "summary:12",
    }]


# ── R15 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r15_a_not_found_entry_has_no_record_fields():
    """R15: A not-found entry carries content, metadata, or graph_context, or the response carries filters_resolved or fallback."""
    resp, _, _ = await _by_ref({"refs": ["fact:404"]})
    body = json.loads(resp.text)
    assert set(body) == {"status", "results"}
    assert body["results"][0] == {
        "ref": "fact:404", "by_ref": True, "found": False, "reason": "missing",
    }


# ── R16 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r16_normal_search_still_attaches_lifecycle_and_stale_sources():
    """R16: The two extractions change a normal search: a decision hit lacks lifecycle, or a summary hit lacks stale_sources.

    The pre-existing pins, left unedited, are
    tests/test_coordinator.py::test_search_retired_summaries_never_reads_from_a_colliding_technical_docs_id
    and the source checks in tests/test_lifecycle_retrieval.py
    (test_lifecycle_attaches_and_does_not_add_rows).
    """
    c, conn = _coord()
    c._resolve_decision_lifecycle = AsyncMock(return_value={
        7: {"rating": "validated", "retrospective_pg_id": 11},
    })
    conn.fetchrow = AsyncMock(side_effect=[
        None,
        {"id": 8, "content": SUMMARY_TEXT,
         "metadata": {"kind": "thematic", "project": "demo-project"},
         "source_pg_ids": [501]},
    ])

    async def fetch(sql, *args):
        if "ORDER BY embedding" in sql:
            return [{
                "id": 7, "content": DECISION_TEXT,
                "metadata": {"type": "decision", "project": "demo-project"},
                "created_at": CREATED,
            }]
        if "superseded_by" in sql and "AND superseded" in sql:
            return [{"id": 501, "superseded_by": 900}]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    rerank = MagicMock()
    rerank.raise_for_status = MagicMock()
    rerank.json = MagicMock(return_value={
        "results": [
            {"index": 0, "relevance_score": 2.0},
            {"index": 1, "relevance_score": 1.0},
        ],
    })
    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=rerank)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = await c.handle_search(_request({"query": "demo ledger", "limit": 5}))
    assert resp.status == 200
    results = json.loads(resp.text)["results"]
    summary = next(r for r in results if r["tier"] == "community_summary")
    decision = next(r for r in results if r["record_type"] == "decision")
    assert summary["stale_sources"] == [{"old": 501, "superseded_by": 900}]
    assert decision["lifecycle"]["rating"] == "validated"
    assert decision["lifecycle"]["ref"] == "retrospective:11"
    assert "by_ref" not in summary
    assert "by_ref" not in decision


# ── R17 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r17_by_ref_hides_a_private_retrospective_and_normal_search_does_not():
    """R17: A found decision's retrospective text is returned to a non-owner on the by-ref path, or withheld from the owner, or a normal search of the same fixture hides it."""
    docs = {7: _doc(7, DECISION_TEXT, metadata=DECISION_META)}
    retro = {
        "id": 11, "content": RETRO_SECRET, "visibility": "private",
        "agent_id": "alice", "scope": "global",
    }

    async def _by(agent, rows):
        c, conn = _coord()
        c._resolve_decision_lifecycle = AsyncMock(return_value={
            7: {"rating": "refined", "retrospective_pg_id": 11},
        })
        conn.fetch = AsyncMock(side_effect=_route(docs, {}, retro_rows=rows))
        resp = await c.handle_search(_request({"refs": ["decision:7"]}, agent=agent))
        return json.loads(resp.text)["results"][0], resp.text

    bob, bob_raw = await _by("bob", [retro])
    assert bob["lifecycle"]["rating"] == "refined"
    assert bob["lifecycle"]["ref"] == "retrospective:11"
    assert "retrospective_content" not in bob["lifecycle"]
    assert RETRO_SECRET not in bob_raw

    alice, _ = await _by("alice", [retro])
    assert alice["lifecycle"]["retrospective_content"] == RETRO_SECRET

    c, conn = _coord()
    c._resolve_decision_lifecycle = AsyncMock(return_value={
        7: {"rating": "refined", "retrospective_pg_id": 11},
    })
    conn.fetchrow = AsyncMock(return_value=None)

    async def fetch(sql, *args):
        if "ORDER BY embedding" in sql:
            return [{
                "id": 7, "content": DECISION_TEXT,
                "metadata": json.loads(DECISION_META),
                "created_at": CREATED,
            }]
        if sql.strip().startswith("SELECT id, content FROM technical_docs"):
            return [{"id": 11, "content": RETRO_SECRET}]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    rerank = MagicMock()
    rerank.raise_for_status = MagicMock()
    rerank.json = MagicMock(return_value={"results": [{"index": 0, "relevance_score": 1.0}]})
    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=rerank)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = await c.handle_search(_request({"query": "demo ledger", "limit": 5}, agent="bob"))
    normal = json.loads(resp.text)["results"][0]
    assert normal["lifecycle"]["retrospective_content"] == RETRO_SECRET


# ── R18 ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r18_a_bare_integer_is_never_wrong_type():
    """R18: A bare integer of a decision row is wrong_type, or fact:7 of that row returns the text."""
    docs = {7: _doc(7, DECISION_TEXT, metadata=DECISION_META)}
    resp, _, _ = await _by_ref({"refs": ["7", "fact:7"]}, docs=docs)
    bare, qualified = json.loads(resp.text)["results"]
    assert bare["record_type"] == "decision"
    assert bare["ref"] == "decision:7"
    assert bare["content"] == DECISION_TEXT
    assert "reason" not in bare
    assert qualified["reason"] == "wrong_type"
    assert qualified["actual_ref"] == "decision:7"
    assert "content" not in qualified
    assert DECISION_TEXT not in json.dumps(qualified)


_SCORE_KEYS = (
    "score", "score_normalized", "ranked", "rerank_payload_chars",
    "rerank_payload_docs", "matched_entities", "fallback", "filters_resolved",
)


# ── review fixes ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_overlong_refs_item_is_shortened_once():
    """A 5,000-character refs item comes back quoted twice, or the message is 200 characters or more."""
    c, _conn = _coord()
    c._acquire = MagicMock(side_effect=AssertionError("acquire"))
    resp = await c.handle_search(_request({"refs": ["9" * 5000]}))
    assert resp.status == 400
    payload = json.loads(resp.text)
    assert payload["error"] == "refs_invalid"
    message = payload["message"]
    assert len(message) < 200
    assert message[0].isalpha()
    assert '"' not in message
    assert "''" not in message
    assert message.count("…[truncated]") == 1
    assert message.startswith("a refs item does not parse (")
    assert c._acquire.call_count == 0


@pytest.mark.asyncio
async def test_a_decision_successor_is_named_decision():
    """A superseded fact whose successor row is a decision is labelled fact."""
    docs = {7: _doc(7, FACT_TEXT, superseded=True, superseded_by=9)}
    resp, _, _ = await _by_ref(
        {"refs": ["fact:7"]}, docs=docs, succ_rows=[{"id": 9, "type": "decision"}])
    hit = json.loads(resp.text)["results"][0]
    assert hit["obsolete"] == "superseded"
    assert hit["superseded_by"] == "decision:9"


@pytest.mark.asyncio
async def test_a_missing_successor_row_omits_superseded_by():
    """A superseded fact whose successor row does not exist still carries superseded_by."""
    docs = {7: _doc(7, FACT_TEXT, superseded=True, superseded_by=9)}
    resp, _, _ = await _by_ref({"refs": ["fact:7"]}, docs=docs, succ_rows=[])
    hit = json.loads(resp.text)["results"][0]
    assert hit["content"] == FACT_TEXT
    assert hit["obsolete"] == "superseded"
    assert "superseded_by" not in hit


@pytest.mark.asyncio
async def test_a_wrong_type_entry_has_exactly_those_keys():
    """A wrong_type entry carries a key other than ref, by_ref, found, reason, and actual_ref."""
    docs = {7: _doc(7, FACT_TEXT, metadata=DECISION_META)}
    resp, _, _ = await _by_ref({"refs": ["fact:7"]}, docs=docs)
    hit = json.loads(resp.text)["results"][0]
    assert set(hit) == {"ref", "by_ref", "found", "reason", "actual_ref"}
    assert hit["actual_ref"] == "decision:7"
    assert FACT_TEXT not in json.dumps(hit)


@pytest.mark.asyncio
async def test_a_found_summary_carries_no_score_keys():
    """A found summary carries a rank or score key."""
    summaries = {8: _summary(8, SUMMARY_TEXT)}
    resp, _, _ = await _by_ref({"refs": ["summary:8"]}, summaries=summaries)
    hit = json.loads(resp.text)["results"][0]
    assert hit["content"] == SUMMARY_TEXT
    for key in _SCORE_KEYS:
        assert key not in hit


@pytest.mark.asyncio
async def test_expansion_and_lifecycle_see_only_the_readable_id():
    """One call naming a readable and an unreadable decision hands the unreadable id to expansion or lifecycle."""
    docs = {
        7: _doc(7, SECRET, metadata=DECISION_META, visibility="private", agent_id="alice"),
        8: _doc(8, DECISION_TEXT, metadata=DECISION_META),
    }
    expanded = []
    lifecycle = []

    async def _expand(session, pg_ids, labels, viewer=None, viewer_scope=None):
        expanded.append(list(pg_ids))
        return {}

    async def _life(session, pg_ids):
        lifecycle.append(list(pg_ids))
        return {}

    c, conn = _coord()
    c._expand_graph_context_batch = _expand
    c._resolve_decision_lifecycle = _life
    conn.fetch = AsyncMock(side_effect=_route(docs, {}))
    resp = await c.handle_search(_request(
        {"refs": ["decision:7", "decision:8"]}, agent="bob"))
    assert resp.status == 200
    assert expanded == [[], [8]]
    assert lifecycle == [[8]]
    raw = resp.text
    assert SECRET not in raw


@pytest.mark.asyncio
async def test_by_ref_keeps_a_decision_and_its_retrospectives_in_the_order_asked():
    """A decision and two retrospectives come back newest-retrospective-first instead of the order asked."""
    retro_meta = json.dumps({"type": "retrospective", "target_pg_id": 1})
    docs = {
        1: _doc(1, DECISION_TEXT, metadata=DECISION_META),
        2: _doc(2, "older retrospective", metadata=retro_meta,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        3: _doc(3, "newer retrospective", metadata=retro_meta,
                created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)),
    }
    resp, _, _ = await _by_ref(
        {"refs": ["retrospective:2", "decision:1", "retrospective:3"]}, docs=docs)
    refs = [hit["ref"] for hit in json.loads(resp.text)["results"]]
    assert refs == ["retrospective:2", "decision:1", "retrospective:3"]


@pytest.mark.asyncio
async def test_by_ref_never_awaits_fetchrow():
    """A by-ref read awaits fetchrow."""
    docs = {7: _doc(7, FACT_TEXT)}
    _resp, _c, conn = await _by_ref({"refs": ["fact:7"]}, docs=docs)
    assert conn.fetchrow.await_count == 0


@pytest.mark.asyncio
async def test_a_stripped_ref_and_its_bare_form_are_one_entry():
    """' fact:7 ' and fact:7 are two entries."""
    docs = {7: _doc(7, FACT_TEXT)}
    resp, _, _ = await _by_ref({"refs": [" fact:7 ", "fact:7"]}, docs=docs)
    results = json.loads(resp.text)["results"]
    assert len(results) == 1
    assert results[0]["ref"] == "fact:7"
    assert results[0]["content"] == FACT_TEXT


@pytest.mark.asyncio
async def test_a_ref_past_signed_bigint_is_refs_invalid_and_the_max_is_not():
    """fact:2**63 is accepted, or fact:2**63-1 is refs_invalid."""
    c, conn = _coord()
    c._acquire = MagicMock(side_effect=AssertionError("acquire"))
    over = await c.handle_search(_request({"refs": ["fact:9223372036854775808"]}))
    assert over.status == 400
    assert json.loads(over.text)["error"] == "refs_invalid"
    assert c._acquire.call_count == 0

    edge, _, _ = await _by_ref({"refs": ["fact:9223372036854775807"]})
    assert edge.status == 200
    assert json.loads(edge.text)["results"][0]["reason"] == "missing"


@pytest.mark.asyncio
async def test_an_insight_ref_of_a_thematic_row_is_wrong_type():
    """insight:N asked of a thematic row is returned as an insight."""
    summaries = {7: _summary(7, SUMMARY_TEXT, kind="thematic")}
    resp, _, _ = await _by_ref({"refs": ["insight:7"]}, summaries=summaries)
    hit = json.loads(resp.text)["results"][0]
    assert hit["reason"] == "wrong_type"
    assert hit["actual_ref"] == "summary:7"
    assert SUMMARY_TEXT not in json.dumps(hit)


@pytest.mark.asyncio
async def test_a_stale_or_retired_fault_on_by_ref_omits_that_key():
    """A stale-map or retired-summaries fault on the by-ref path fails the read, or the entry still carries that key."""
    summaries = {
        9: _summary(
            9, "An insight over a retired demo summary.",
            kind="insight",
            metadata=json.dumps({
                "kind": "insight", "summary_ids": [12], "project": "demo-project",
            }),
            source_pg_ids=[501],
        ),
    }
    base = _route(docs={}, summaries=summaries)

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "superseded_reason" in text and "community_summaries" in text:
            raise RuntimeError("retired down")
        if "AND superseded" in text and "technical_docs" in text:
            raise RuntimeError("stale down")
        return await base(sql, *args)

    c, conn = _coord()
    conn.fetch = AsyncMock(side_effect=fetch)
    resp = await c.handle_search(_request({"refs": ["insight:9"]}))
    assert resp.status == 200
    hit = json.loads(resp.text)["results"][0]
    assert hit["content"] == "An insight over a retired demo summary."
    assert "stale_sources" not in hit
    assert "retired_summaries" not in hit


@pytest.mark.asyncio
async def test_summary_refs_on_a_normal_search_follow_the_referenced_rows():
    """A normal-search insight with summary_ids lacks summary_refs, or those refs are not the referenced rows' kinds."""
    c, conn = _coord()
    insight_meta = {
        "kind": "insight", "summary_ids": [11, 12], "project": "demo-project",
    }
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 9, "content": "An insight that cites two demo summaries.",
         "metadata": insight_meta, "source_pg_ids": []},
        None,
    ])

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if text.startswith("SELECT id, metadata FROM community_summaries"):
            wanted = set(args[0]) if args else set()
            rows = [
                {"id": 11, "metadata": {"kind": "thematic"}},
                {"id": 12, "metadata": {"kind": "thematic"}},
            ]
            return [row for row in rows if row["id"] in wanted]
        if "superseded_reason" in text and "community_summaries" in text:
            return []
        if "ORDER BY embedding" in text or "technical_docs" in text:
            return [{
                "id": 1, "content": FACT_TEXT,
                "metadata": {"type": "fact", "project": "demo-project"},
                "created_at": CREATED,
            }]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    rerank = MagicMock()
    rerank.raise_for_status = MagicMock()
    rerank.json = MagicMock(return_value={
        "results": [
            {"index": 0, "relevance_score": 2.0},
            {"index": 1, "relevance_score": 1.0},
        ],
    })
    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=rerank)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = await c.handle_search(_request({"query": "demo ledger", "limit": 5}))
    assert resp.status == 200
    results = json.loads(resp.text)["results"]
    insight = next(hit for hit in results if hit["record_type"] == "insight")
    fact = next(hit for hit in results if hit["record_type"] == "fact")
    assert insight["summary_refs"] == ["summary:11", "summary:12"]
    assert insight["metadata"]["summary_ids"] == [11, 12]
    assert "summary_refs" not in insight["metadata"]
    assert "summary_refs" not in fact
    assert set(insight) <= {
        "tier", "record_type", "ref", "pg_id", "content", "ranked",
        "rerank_payload_chars", "rerank_payload_docs", "score", "score_normalized",
        "matched_entities", "metadata", "source_pg_ids", "graph_context",
        "stale_sources", "retired_summaries", "summary_refs",
    }


@pytest.mark.asyncio
async def test_summary_refs_by_ref_keep_order_kind_and_drop_a_missing_row():
    """By ref, summary_refs drop a kind, reorder, keep a missing id, or appear without a list of summary_ids."""
    summaries = {
        9: _summary(
            9, "An insight that cites two demo summaries.",
            kind="insight",
            metadata=json.dumps({
                "kind": "insight", "summary_ids": [11, 12, 99],
                "project": "demo-project",
            }),
            source_pg_ids=[],
        ),
        8: _summary(
            8, SUMMARY_TEXT,
            metadata=json.dumps({
                "kind": "thematic", "summary_ids": [11], "project": "demo-project",
            }),
        ),
        7: _summary(7, SUMMARY_TEXT),
        10: _summary(
            10, "An insight whose summary_ids is not a list.",
            kind="insight",
            metadata=json.dumps({
                "kind": "insight", "summary_ids": "11", "project": "demo-project",
            }),
            source_pg_ids=[],
        ),
    }
    resp, _, _ = await _by_ref(
        {"refs": ["insight:9", "summary:8", "summary:7", "insight:10"]},
        summaries=summaries,
        ref_rows=[
            {"id": 11, "metadata": {"kind": "thematic"}},
            {"id": 12, "metadata": json.dumps({"kind": "insight"})},
        ],
    )
    results = {hit["ref"]: hit for hit in json.loads(resp.text)["results"]}
    assert results["insight:9"]["summary_refs"] == ["summary:11", "insight:12"]
    assert results["insight:9"]["metadata"]["summary_ids"] == [11, 12, 99]
    assert "summary_refs" not in results["insight:9"]["metadata"]
    assert results["summary:8"]["summary_refs"] == ["summary:11"]
    assert "summary_refs" not in results["summary:7"]
    assert "summary_refs" not in results["insight:10"]


@pytest.mark.asyncio
async def test_retired_summaries_carry_ref_and_superseded_by_ref():
    """A retired_summaries entry lacks ref, or lacks superseded_by_ref when that row exists, or carries it when the successor is null."""
    summaries = {
        9: _summary(
            9, "An insight over two retired demo summaries.",
            kind="insight",
            metadata=json.dumps({
                "kind": "insight", "summary_ids": [12, 14], "project": "demo-project",
            }),
            source_pg_ids=[],
        ),
    }
    resp, _, _ = await _by_ref(
        {"refs": ["insight:9"]},
        summaries=summaries,
        retired_rows=[
            {"id": 12, "superseded": True, "superseded_reason": "coverage",
             "superseded_by": 13, "source_pg_ids": [],
             "metadata": {"kind": "thematic"}},
            {"id": 14, "superseded": True, "superseded_reason": "coverage",
             "superseded_by": None, "source_pg_ids": [],
             "metadata": {"kind": "thematic"}},
            {"id": 13, "superseded": False, "superseded_reason": None,
             "superseded_by": None, "source_pg_ids": [],
             "metadata": {"kind": "insight"}},
        ],
    )
    retired = json.loads(resp.text)["results"][0]["retired_summaries"]
    assert retired[0]["summary_id"] == 12
    assert retired[0]["superseded_reason"] == "coverage"
    assert retired[0]["superseded_by"] == 13
    assert retired[0]["unsupported"] == []
    assert retired[0]["ref"] == "summary:12"
    assert retired[0]["superseded_by_ref"] == "insight:13"
    assert retired[1]["summary_id"] == 14
    assert retired[1]["ref"] == "summary:14"
    assert retired[1]["superseded_by"] is None
    assert "superseded_by_ref" not in retired[1]
