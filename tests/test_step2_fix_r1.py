"""Fix-round failures from the step-2 review (brief v6 section 6).

Each test names the F-row or review item and the failure it kills. Handlers
are stubbed. SQL text is pinned because a stub never asks Postgres to parse it.
"""
import inspect
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
SECRET = "SECRET-private-successor-text"


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


class _RaiseOnEnter:
    async def __aenter__(self):
        raise RuntimeError("neo4j session refused")

    async def __aexit__(self, *_):
        return None


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


def _retired_sql():
    src = inspect.getsource(MemoryCoordinator._annotate_retired_summaries)
    start = src.index("WITH decisions AS")
    return src[start:src.index('"""', start)]


def test_item1_retired_summary_decisions_cte_projects_visibility():
    """F25 / Codex 1: the decisions CTE selects only id, and the final SELECT reads d.visibility, so Postgres rejects the query.

    Mutation: drop visibility, agent_id and scope from the decisions CTE.
    """
    sql = _retired_sql()
    cte = sql[sql.index("WITH decisions AS"):sql.index("decision_facts AS")]
    assert "visibility" in cte
    assert "agent_id" in cte
    assert "scope" in cte
    final = sql[sql.index("SELECT d.id AS decision_id"):sql.index("FROM decisions d")]
    assert "d.visibility" in final or "max(d.visibility)" in final
    assert "d.agent_id" in final or "max(d.agent_id)" in final
    assert "d.scope" in final or "max(d.scope)" in final


def test_item2_stale_join_qualifies_every_column():
    """F25 / Codex 2: `AND superseded` is ambiguous after technical_docs t JOIN technical_docs s, so Postgres rejects the stale-source query.

    Mutation: change `AND t.superseded` back to `AND superseded`.
    """
    src = inspect.getsource(MemoryCoordinator._load_stale_map)
    assert "SELECT t.id, t.superseded_by, t.visibility, t.agent_id, t.scope" in src
    assert "s.visibility AS successor_visibility" in src
    assert "s.agent_id AS successor_agent_id" in src
    assert "s.scope AS successor_scope" in src
    assert "LEFT JOIN technical_docs s ON s.id = t.superseded_by" in src
    assert "WHERE t.id = ANY($1) AND t.superseded" in src
    assert "AND superseded\"" not in src


def _status(ref, agent="bob"):
    req = MagicMock()
    req.match_info = {"pg_id": ref}
    state = {"authenticated_agent": agent}
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    return req


def _hydrate_row(pid, *, kind="fact", private=False, table_meta=None):
    meta = table_meta or {"type": kind}
    return {
        "id": pid, "content": f"body-{pid}", "content_chars": 6,
        "metadata": meta, "type": kind, "superseded": False,
        "superseded_by": None,
        "visibility": "private" if private else "global",
        "agent_id": "zoe" if private else "alice",
        "scope": "global",
    }


def _arm_hydrate(conn, catalog):
    """Route successor_id fetches to catalog rows. Other fetches return []."""

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "successor_id" in text:
            wanted = set(args[0]) if args else set()
            return [catalog[i] for i in catalog if i in wanted]
        if "ILIKE" in text:
            return list(catalog.get("_keyword", []))
        if "source_pg_ids" in text and "community_summaries" in text:
            return []
        return []

    conn.fetch = AsyncMock(side_effect=fetch)


@pytest.mark.asyncio
async def test_item3_lineage_ack_hides_a_private_decision_and_successor():
    """F25: supersession_ack names a private decision, quotes its answer, or names a private successor in `by`.

    Whole serialized lineage reply. Mutation: stop filtering answers or stop dropping `by`.
    """
    c, conn, _session = _coord()
    public = _hydrate_row(21, kind="decision", table_meta={
        "type": "decision", "decision": {"rationale": "public rationale"}})
    private_decision = _hydrate_row(20, kind="decision", private=True, table_meta={
        "type": "decision", "decision": {"rationale": "SECRET-DECISION"}})
    private_by = _hydrate_row(30, private=True)
    fact = _hydrate_row(10)
    catalog = {10: fact, 21: public, 20: private_decision, 30: private_by}
    _arm_hydrate(conn, catalog)
    rec = {
        "type": "fact", "created_at": None, "superseded": False,
        "superseded_by": None, "grounded_in": None,
        "visibility": "global", "agent_id": "alice", "scope": "global",
        "supersession_ack": {
            "decisions": [21, 20],
            "answers": {"21": "public answer", "20": "SECRET-ACK-ANSWER"},
            "acknowledged_by": "operator",
            "by": 30,
        },
    }
    conn.fetchrow = AsyncMock(side_effect=[rec, None])
    resp = await c.handle_status(_status("fact:10", agent="bob"))
    raw = resp.text
    body = json.loads(raw)
    assert "SECRET-ACK-ANSWER" not in raw
    assert "SECRET-DECISION" not in raw
    ack = body["supersession_ack"]
    assert ack["decisions"] == [21]
    assert ack["answers"] == {"21": "public answer"}
    assert "by" not in ack
    assert "fact:30" not in raw
    assert "decision:20" not in raw


@pytest.mark.asyncio
async def test_item3_and_item5_hit_metadata_redacts_every_named_identity():
    """F25: hit metadata keeps a private ack, grounded_roles key, supersedes id, reviewed pair, or an insight cypher_query that names an unreadable source.

    Mutation: leave any one of those fields unfiltered.
    """
    c, conn, session = _coord()
    readable = {
        4: _hydrate_row(4),
        6: _hydrate_row(6),
        8: _hydrate_row(8),
        9: _hydrate_row(9),
        21: _hydrate_row(21, kind="decision"),
    }
    hidden = {
        5: _hydrate_row(5, private=True),
        7: _hydrate_row(7, private=True),
        10: _hydrate_row(10, private=True),
        20: _hydrate_row(20, kind="decision", private=True),
        30: _hydrate_row(30, private=True),
    }
    catalog = {**readable, **hidden}
    _arm_hydrate(conn, catalog)
    session.run = AsyncMock(return_value=_Rows([]))
    leak = {
        "pg_id": 1, "record_type": "insight", "ref": "insight:1",
        "metadata": {
            "kind": "insight",
            "supersession_ack": {
                "decisions": [21, 20],
                "answers": {"21": "ok", "20": "SECRET-ACK-ANSWER"},
                "by": 30,
            },
            "grounded_roles": {"4": "INFORMED_BY", "5": "GROUNDED_IN"},
            "supersedes": [6, 7],
            "reviewed_supersessions": [
                {"old": 8, "by": 9},
                {"old": 8, "by": 10},
            ],
            "src_ids": [4, 5],
            "cypher_query": "MATCH (n) WHERE n.pg_id IN [4, 5] RETURN SECRET-CYPHER",
        },
        "source_pg_ids": [4, 5],
        "graph_context": [],
    }
    clean = {
        "pg_id": 2, "record_type": "insight", "ref": "insight:2",
        "metadata": {
            "kind": "insight",
            "src_ids": [4],
            "cypher_query": "MATCH (n) WHERE n.pg_id = 4 RETURN n",
        },
        "source_pg_ids": [4],
        "graph_context": [],
    }
    await c._compose_search_hits(
        [leak, clean], ranked=True, viewer="bob", viewer_scope=None, session=session)
    raw = json.dumps([leak, clean])
    assert "SECRET-ACK-ANSWER" not in raw
    assert "SECRET-CYPHER" not in raw
    ack = leak["metadata"]["supersession_ack"]
    assert ack["decisions"] == [21]
    assert list(ack["answers"]) == ["21"]
    assert "by" not in ack
    assert leak["metadata"]["grounded_roles"] == {"4": "INFORMED_BY"}
    assert leak["metadata"]["supersedes"] == [6]
    assert leak["metadata"]["reviewed_supersessions"] == [{"old": 8, "by": 9}]
    assert "cypher_query" not in leak["metadata"]
    assert clean["metadata"]["cypher_query"].startswith("MATCH")
    assert leak["source_pg_ids"] == [4]
    assert leak["metadata"]["src_ids"] == [4]
    assert '"src_ids": [4, 5]' not in raw


@pytest.mark.asyncio
async def test_item5_cypher_query_uses_unredacted_source_lists():
    """F25: a thematic or insight cypher_query keeps a private source after metadata.source_pg_ids is filtered, or metadata.src_ids keeps it.

    Production stores metadata.source_pg_ids and no src_ids. Mutation: decide the query from the lists after filtering, or skip src_ids.
    """
    c, conn, session = _coord()
    catalog = {
        4: _hydrate_row(4),
        5: _hydrate_row(5, private=True),
    }
    _arm_hydrate(conn, catalog)
    session.run = AsyncMock(return_value=_Rows([]))

    def hit(pid, record_type, kind, sources, query, *, src_ids=None):
        meta = {"kind": kind, "source_pg_ids": list(sources), "cypher_query": query}
        if src_ids is not None:
            meta["src_ids"] = list(src_ids)
        return {
            "pg_id": pid, "record_type": record_type, "ref": f"{record_type}:{pid}",
            "metadata": meta, "source_pg_ids": list(sources), "graph_context": [],
        }

    thematic = hit(
        1, "summary", "thematic", [4, 5],
        "MATCH (n) WHERE n.pg_id IN [4, 5] RETURN SECRET-THEMATIC")
    insight = hit(
        2, "insight", "insight", [4, 5],
        "MATCH (n) WHERE n.pg_id IN [4, 5] RETURN SECRET-INSIGHT")
    control = hit(
        3, "summary", "thematic", [4],
        "MATCH (n) WHERE n.pg_id = 4 RETURN PUBLIC-CYPHER")
    src = hit(
        8, "insight", "insight", [4],
        "MATCH (n) WHERE n.pg_id IN [4, 5] RETURN SECRET-SRC",
        src_ids=[4, 5])
    src["source_pg_ids"] = [4]
    # Private id 5 is only on the hit's source_pg_ids. metadata lists the
    # readable id, so a check that ignores the top-level list would keep the query.
    top_only = hit(
        9, "insight", "insight", [4],
        "MATCH (n) RETURN SECRET-TOP-ONLY")
    top_only["source_pg_ids"] = [5]
    await c._compose_search_hits(
        [thematic, insight, control, src, top_only], ranked=True, viewer="bob",
        viewer_scope=None, session=session)
    raw = json.dumps([thematic, insight, control, src, top_only])
    assert "SECRET-THEMATIC" not in raw
    assert "SECRET-INSIGHT" not in raw
    assert "SECRET-SRC" not in raw
    assert "SECRET-TOP-ONLY" not in raw
    assert "5" not in raw
    assert "PUBLIC-CYPHER" in raw
    assert thematic["metadata"]["source_pg_ids"] == [4]
    assert insight["metadata"]["source_pg_ids"] == [4]
    assert "cypher_query" not in thematic["metadata"]
    assert "cypher_query" not in insight["metadata"]
    assert "cypher_query" not in top_only["metadata"]
    assert top_only["metadata"]["source_pg_ids"] == [4]
    assert top_only["source_pg_ids"] == []
    assert control["metadata"]["cypher_query"].startswith("MATCH")
    assert src["metadata"]["src_ids"] == [4]


def _stale_summary_row():
    """One thematic summary: 4 acknowledged with a private successor, 6 not, 8 retracted."""
    return {
        "id": 3,
        "content": "thematic narrative",
        "metadata": {
            "kind": "thematic",
            "source_pg_ids": [4, 6, 8],
            "reviewed_supersessions": [{"old": 4, "by": 9}],
        },
        "source_pg_ids": [4, 6, 8],
        "superseded": False,
        "visibility": "global",
        "agent_id": "alice",
        "scope": "global",
    }


def _stale_rows():
    def row(pid, successor, *, private_successor=False, retracted=False):
        return {
            "id": pid,
            "superseded_by": None if retracted else successor,
            "visibility": "global",
            "agent_id": "",
            "scope": "global",
            "successor_visibility": None if retracted else (
                "private" if private_successor else "global"),
            "successor_agent_id": "zoe" if private_successor else "",
            "successor_scope": "global",
        }
    return [
        row(4, 9, private_successor=True),
        row(6, 11, private_successor=True),
        row(8, None, retracted=True),
    ]


@pytest.mark.asyncio
async def test_item_stale_ranked_and_by_ref_agree_on_a_private_successor():
    """F25: by-ref computes stale_sources after redaction, so an acknowledged pair with a private successor returns as a retraction, and an unacknowledged private successor is null.

    Ranked and by-ref must agree. null stays a retraction. Mutation: attach stale from the redacted metadata, or keep superseded_by when the successor is unreadable.
    """
    async def search(body):
        summary = _stale_summary_row()
        fact = _hydrate_row(1)
        catalog = {
            3: _hydrate_row(3, kind="summary", table_meta={"kind": "thematic"}),
            4: _hydrate_row(4),
            6: _hydrate_row(6),
            8: _hydrate_row(8),
            9: _hydrate_row(9, private=True),
            11: _hydrate_row(11, private=True),
            1: fact,
        }
        stale_rows = _stale_rows()

        async def fetch(sql, *args):
            text = " ".join(sql.split())
            if "successor_id" in text:
                wanted = set(args[0]) if args else set()
                if "community_summaries" in text:
                    row = catalog[3]
                    return [row] if row["id"] in wanted else []
                return [catalog[i] for i in catalog if i in wanted]
            if "AND t.superseded" in text:
                return stale_rows
            if "ORDER BY embedding" in text and "technical_docs" in text:
                return [fact]
            if "FROM community_summaries" in text and "source_pg_ids" in text:
                return [summary]
            return []

        async def fetchrow(sql, *args):
            text = " ".join(sql.split())
            if "kind' = 'insight'" in text:
                return None
            if "community_summaries" in text and "source_pg_ids" in text:
                return summary
            return None

        c, conn, session = _coord()
        conn.fetch = AsyncMock(side_effect=fetch)
        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        session.run = AsyncMock(return_value=_Rows([]))
        if "refs" in body:
            resp = await c.handle_search(_request(body, agent="bob"))
        else:
            with patch("httpx.AsyncClient") as mock_cls:
                rerank = MagicMock()
                rerank.raise_for_status = MagicMock()
                rerank.json = MagicMock(return_value={"results": [
                    {"index": 0, "relevance_score": 2.0},
                    {"index": 1, "relevance_score": 1.0},
                ]})
                mock_http = AsyncMock()
                mock_http.post = AsyncMock(return_value=rerank)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
                resp = await c.handle_search(_request(body, agent="bob"))
        assert resp.status == 200, resp.text
        hits = json.loads(resp.text)["results"]
        found = [h for h in hits if h.get("pg_id") == 3]
        if not found:
            raise AssertionError(resp.text[:2000])
        return found[0], resp.text

    expected = [{"old": 6}, {"old": 8, "superseded_by": None}]
    ranked_hit, ranked_raw = await search({"query": "locks", "limit": 5})
    by_ref_hit, by_ref_raw = await search({"refs": ["summary:3"]})
    assert ranked_hit.get("stale_sources") == expected
    assert by_ref_hit.get("stale_sources") == expected
    assert ranked_hit["stale_sources"] == by_ref_hit["stale_sources"]
    for raw in (ranked_raw, by_ref_raw):
        assert ": 9" not in raw and ": 11" not in raw
        assert "fact:9" not in raw and "fact:11" not in raw


@pytest.mark.asyncio
async def test_item4_retired_summary_omits_unreadable_successor_and_private_row():
    """F25 / M14: a retired summary emits a bare superseded_by when the successor is private, or keeps a private summary the viewer cannot read.

    Mutation: always set superseded_by, or delete the visibility skip on the retired row.
    """
    c, _conn, _session = _coord()
    fake = MagicMock()

    async def fetch(sql, *args):
        ids = list(args[0]) if args else []
        if 11 in ids:
            return [{
                "id": 11, "metadata": {"kind": "thematic"},
                "superseded": False, "superseded_by": None, "source_pg_ids": [],
                "visibility": "private", "agent_id": "zoe", "scope": "global",
            }]
        return [
            {"id": 10, "metadata": {"kind": "thematic"}, "superseded": True,
             "superseded_reason": "coverage", "superseded_by": 11,
             "source_pg_ids": [], "visibility": "global", "agent_id": "alice",
             "scope": "global"},
            {"id": 12, "metadata": {"kind": "thematic"}, "superseded": True,
             "superseded_reason": "coverage", "superseded_by": None,
             "source_pg_ids": [], "visibility": "private", "agent_id": "zoe",
             "scope": "global"},
        ]

    fake.fetch = AsyncMock(side_effect=fetch)
    fake.fetchrow = AsyncMock(return_value=None)
    out = await c._annotate_retired_summaries(
        fake, [{"id": 1, "summary_ids": [10, 12], "source_pg_ids": []}],
        viewer="bob")
    raw = json.dumps(out)
    assert "12" not in raw
    assert len(out[1]) == 1
    entry = out[1][0]
    assert entry["summary_id"] == 10
    assert "superseded_by" not in entry
    assert "superseded_by_ref" not in entry


@pytest.mark.asyncio
async def test_item6_keyword_fallback_composes_when_neo4j_cannot_open(caplog):
    """F23 / F31: Neo4j refusing the session returns keyword hits with no hydration, so a private grounded_in id stays and the marks warning is absent.

    Mutation: return the hits from the except without compose, or drop the marks warning.
    """
    c, conn, _session = _coord()
    c._embed = AsyncMock(side_effect=RuntimeError("embedder down"))
    c._neo4j.session = MagicMock(return_value=_RaiseOnEnter())
    private = _hydrate_row(99, private=True)
    fact = _hydrate_row(7)
    fact["metadata"] = {"type": "fact", "grounded_in": [99]}
    catalog = {7: fact, 99: private, "_keyword": [fact]}

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "ILIKE" in text:
            return [fact]
        if "successor_id" in text:
            wanted = set(args[0]) if args else set()
            return [catalog[i] for i in catalog if isinstance(i, int) and i in wanted]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    with caplog.at_level(logging.WARNING):
        resp = await c.handle_search(_request({"query": "locks", "limit": 5}, agent="bob"))
    raw = resp.text
    assert "body-99" not in raw
    hit = json.loads(raw)["results"][0]
    assert hit["metadata"]["grounded_in"] == []
    assert any(
        "retired_grounds, latest_verdict and lifecycle unavailable" in r.message
        for r in caplog.records if r.levelno >= logging.WARNING)


def test_item7_viewer_is_keyword_required_and_a_missing_successor_is_unreadable():
    """F16: a caller can omit viewer, or a successor id with no visibility row is treated as readable.

    Mutation: give viewer a default, or treat a null successor_visibility as global.
    """
    import inspect as inspect_mod
    for name in ("_load_stale_map", "_annotate_narrative_entries",
                 "_annotate_retired_summaries"):
        sig = inspect_mod.signature(getattr(MemoryCoordinator, name))
        param = sig.parameters["viewer"]
        assert param.default is inspect_mod.Parameter.empty
        assert param.kind is inspect_mod.Parameter.KEYWORD_ONLY
    missing = {"superseded_by": 900, "successor_id": None}
    assert coordinator_mod._successor_unreadable(missing, "bob", None) is True
    null_vis = {"successor_id": 900, "successor_visibility": None}
    assert coordinator_mod._successor_unreadable(null_vis, "bob", None) is True
    named = {"successor_id": 900, "successor_visibility": "global",
             "successor_agent_id": "", "successor_scope": "global"}
    assert coordinator_mod._successor_unreadable(named, "bob", None) is False
    none = {"superseded_by": None}
    assert coordinator_mod._successor_unreadable(none, "bob", None) is False


@pytest.mark.asyncio
async def test_item8_cross_table_same_id_is_not_a_self_loop():
    """F32: fact:42 and summary:42 compare equal as bare integers, so each direction is dropped as a self-loop on ranked and by-ref search.

    Mutation: compare pg_id alone in the self-loop or anchor check. The anchor check dies when the neighbour name, which lives only on the raw edge, is not copied onto the other table's end.
    """
    summary = _hydrate_row(42, kind="summary", table_meta={"kind": "thematic"})
    fact = _hydrate_row(42)

    def edge(start_labels, end_labels):
        return {
            "rel_type": "MENTIONS", "rel_props": {},
            "start_pg_id": 42, "start_labels": start_labels,
            "end_pg_id": 42, "end_labels": end_labels,
            "labels": end_labels, "pg_id": 42,
        }

    async def one(ranked, record_type, ref, start_labels, end_labels):
        c, conn, session = _coord()

        async def fetch(sql, *args):
            if "successor_id" not in " ".join(sql.split()):
                return []
            if "community_summaries" in sql:
                return [summary]
            return [fact]

        conn.fetch = AsyncMock(side_effect=fetch)
        session.run = AsyncMock(return_value=_Rows([]))
        hit = {
            "pg_id": 42, "record_type": record_type, "ref": ref,
            "metadata": {}, "graph_context": [edge(start_labels, end_labels)],
        }
        await c._compose_search_hits(
            [hit], ranked=ranked, viewer="bob", viewer_scope=None, session=session)
        return hit

    cases = [
        (True, "fact", "fact:42", [ONT.fact], [ONT.community_summary], "summary:42"),
        (True, "summary", "summary:42", [ONT.community_summary], [ONT.fact], "fact:42"),
        (False, "fact", "fact:42", [ONT.fact], [ONT.community_summary], "summary:42"),
        (False, "summary", "summary:42", [ONT.community_summary], [ONT.fact], "fact:42"),
    ]
    for ranked, record_type, ref, start_labels, end_labels, far in cases:
        hit = await one(ranked, record_type, ref, start_labels, end_labels)
        assert hit["graph_context"], (ranked, ref, far)
        assert hit["graph_context"][0]["relation"]["to"] == far

    start, end = coordinator_mod._search_edge_ends({
        "start_pg_id": 42, "start_labels": [ONT.fact],
        "end_pg_id": 42, "end_labels": [ONT.community_summary],
        "name": "NEIGHBOUR-NAME",
    }, 42, "technical_docs")
    assert end["name"] == "NEIGHBOUR-NAME"
    assert start["name"] is None
    start, end = coordinator_mod._search_edge_ends({
        "start_pg_id": 42, "start_labels": [ONT.fact],
        "end_pg_id": 42, "end_labels": [ONT.community_summary],
        "name": "NEIGHBOUR-NAME",
    }, 42, "community_summaries")
    assert start["name"] == "NEIGHBOUR-NAME"
    assert end["name"] is None


def test_item9_expansion_where_does_not_trust_the_graph_superseded_flag():
    """F4b: the expansion WHERE drops a superseded summary before Postgres is read.

    Mutation: put `coalesce(related.superseded,false)` back on either expander.
    """
    for fn in (MemoryCoordinator._expand_graph_context_batch,):
        src = inspect.getsource(fn)
        assert "coalesce(related.superseded,false)" not in src
        assert "'REFERENCES'" in src


@pytest.mark.asyncio
async def test_item9_by_ref_keeps_a_superseded_summary_neighbour():
    """F4b: search by ref drops a superseded summary neighbour, or leaves it unmarked.

    Mutation: ranked=True on the by-ref compose, or drop superseded summaries after hydration on an explicit read.
    """
    c, conn, session = _coord()
    anchor = _hydrate_row(7)
    neighbour = _hydrate_row(8, kind="summary", table_meta={"kind": "thematic"})
    neighbour["superseded"] = True
    neighbour["content"] = "retired summary body"

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "FROM community_summaries" in text and "source_pg_ids" in text and "successor_id" not in text:
            return []
        if "FROM technical_docs" in text and "successor_id" not in text and "superseded_by, visibility" in text:
            return [anchor]
        if "successor_id" in text and "community_summaries" in text:
            return [neighbour]
        if "successor_id" in text:
            return [anchor]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    edge = {
        "rel_type": "MENTIONS", "rel_props": {}, "pg_id": 8, "rel_pg_id": 8,
        "anchor_pg_id": 7,
        "labels": [ONT.community_summary],
        "start_pg_id": 7, "start_labels": [ONT.fact],
        "end_pg_id": 8, "end_labels": [ONT.community_summary],
    }
    session.run = AsyncMock(return_value=_Rows([edge]))
    resp = await c.handle_search(_request({"refs": ["fact:7"]}, agent="bob"))
    hit = json.loads(resp.text)["results"][0]
    assert hit["graph_context"]
    record = hit["graph_context"][0]["record"]
    assert record["ref"] == "summary:8"
    assert record["obsolete"] == "superseded"


@pytest.mark.asyncio
async def test_item10_related_more_counts_rows_before_self_loops_drop():
    """F32: related_more is computed after self-loops are removed, so a full page of real neighbours plus a dropped loop says the list was not cut.

    Mutation: set more from len(edges) after the self-loop filter.
    """
    c, _conn, session = _coord()
    cap = coordinator_mod.LINEAGE_RELATED_CAP
    rows = []
    for i in range(cap):
        far = 1000 + i
        rows.append({
            "anchor_rem_summary": None, "rel": "MENTIONS",
            "start_pg_id": 5, "start_labels": [ONT.fact],
            "end_pg_id": far, "end_labels": [ONT.fact],
            "labels": [ONT.fact], "pg_id": far, "rel_props": {},
        })
    rows.append({
        "anchor_rem_summary": None, "rel": "HAD_OUTCOME",
        "start_pg_id": 5, "start_labels": [ONT.fact],
        "end_pg_id": 5, "end_labels": [ONT.fact],
        "labels": [ONT.fact], "pg_id": 5, "rel_props": {"notes": "loop"},
    })
    session.run = AsyncMock(return_value=_Rows(rows))
    out = await c._lineage_related(5, "bob", "fact")
    assert out["more"] is True
    assert len(out["edges"]) == cap


@pytest.mark.asyncio
async def test_item12_by_ref_compose_failure_warns_and_marks_stay_closed(caplog):
    """F31: a by-ref compose failure is logged at debug, and the session=None retry does not log that marks are unavailable.

    Mutation: log.debug on the except, or skip the marks warning when session is None.
    """
    c, conn, _session = _coord()
    c._neo4j.session = MagicMock(return_value=_RaiseOnEnter())
    fact = _hydrate_row(7)
    fact["metadata"] = {"type": "fact", "grounded_in": [99]}
    private = _hydrate_row(99, private=True)

    async def fetch(sql, *args):
        text = " ".join(sql.split())
        if "successor_id" in text:
            wanted = set(args[0]) if args else set()
            rows = []
            if 7 in wanted:
                rows.append(fact)
            if 99 in wanted:
                rows.append(private)
            return rows
        if "FROM technical_docs" in text:
            return [fact]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    with caplog.at_level(logging.WARNING):
        resp = await c.handle_search(_request({"refs": ["fact:7"]}, agent="bob"))
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("graph expansion unavailable" in m for m in warnings)
    assert any("retired_grounds, latest_verdict and lifecycle unavailable" in m
               for m in warnings)
    hit = json.loads(resp.text)["results"][0]
    assert hit["metadata"]["grounded_in"] == []


@pytest.mark.asyncio
async def test_item15_load_stale_map_skips_an_unreadable_source():
    """F25 / M8: _load_stale_map keeps a private source in the map.

    Mutation: delete the visibility skip in _load_stale_map.
    """
    c, conn, _session = _coord()
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "superseded_by": 8, "visibility": "global", "agent_id": "",
         "scope": "global", "successor_visibility": "global",
         "successor_agent_id": "", "successor_scope": "global"},
        {"id": 2, "superseded_by": 9, "visibility": "private", "agent_id": "zoe",
         "scope": "global", "successor_visibility": "global",
         "successor_agent_id": "", "successor_scope": "global"},
    ])
    stale = await c._load_stale_map({1, 2}, viewer="alice")
    assert stale == {1: 8}


@pytest.mark.asyncio
async def test_item15_stale_source_omits_an_unreadable_successor():
    """F25 / M13: a public stale source names a private successor.

    Mutation: keep superseded_by when _successor_unreadable is true.
    """
    c, conn, _session = _coord()
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "superseded_by": 9, "visibility": "global", "agent_id": "",
         "scope": "global", "successor_visibility": "private",
         "successor_agent_id": "zoe", "successor_scope": "global"},
    ])
    stale = await c._load_stale_map({1}, viewer="alice")
    assert list(stale) == [1]
    assert stale[1] is coordinator_mod._HIDDEN_SUCCESSOR
    assert 9 not in stale.values()


@pytest.mark.asyncio
async def test_item15_summary_refs_skip_an_unreadable_summary():
    """F25 / M9: summary_refs names a private summary.

    Mutation: drop the visibility skip in the summary_refs loop.
    """
    c, conn, _session = _coord()
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "metadata": {"kind": "thematic"}, "visibility": "global",
         "agent_id": "", "scope": "global"},
        {"id": 2, "metadata": {"kind": "thematic"}, "visibility": "private",
         "agent_id": "zoe", "scope": "global"},
    ])
    entries = [{
        "record_type": "insight", "pg_id": 20,
        "metadata": {"summary_ids": [1, 2], "kind": "insight"},
        "source_pg_ids": [],
    }]
    await c._annotate_narrative_entries(entries, viewer="alice")
    assert entries[0]["summary_refs"] == ["summary:1"]


@pytest.mark.asyncio
async def test_item15_consolidated_into_skips_an_unreadable_summary():
    """F25 / M17: handle_status consolidated_into names a private summary.

    Mutation: delete the visibility skip on the summary rows.
    """
    c, conn, session = _coord()
    anchor = _doc(5, "visible fact")
    anchor["type"] = "fact"
    anchor["grounded_in"] = None
    anchor["supersession_ack"] = None
    public = {
        "id": 3, "kind": "thematic", "entity": None, "created_at": CREATED,
        "run_id": 1, "visibility": "global", "agent_id": "", "scope": "global",
        "cycle_started": None, "cycle_finished": None,
    }
    private = dict(public)
    private.update(id=4, visibility="private", agent_id="zoe")
    conn.fetchrow = AsyncMock(side_effect=[anchor, None])

    async def fetch(sql, *args):
        if "cs.id" in sql or "community_summaries" in sql and "cycle" in sql:
            return [public, private]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    session.run = AsyncMock(return_value=_Rows([]))
    req = MagicMock()
    req.match_info = {"pg_id": "fact:5"}
    req.get = MagicMock(side_effect=lambda k, d=None: {"authenticated_agent": "alice"}.get(k, d))
    resp = await c.handle_status(req)
    body = json.loads(resp.text)
    assert [item["summary_pg_id"] for item in body["consolidated_into"]] == [3]


@pytest.mark.asyncio
async def test_item15_f4b_each_type_on_ranked_and_by_ref():
    """F4b: a superseded fact, retrospective, decision, summary or insight stays in ranked graph_context, or a by-ref neighbour lacks obsolete.

    Mutation: drop the ranked superseded check, or mark a reversed decision superseded.
    """
    kinds = [
        ("fact", [ONT.fact], "superseded", {"type": "fact"}),
        ("retrospective", [ONT.retrospective], "superseded", {"type": "retrospective"}),
        ("decision", [ONT.decision], "reversed",
         {"type": "decision", "decision": {"rationale": "because"}}),
        ("summary", [ONT.community_summary], "superseded", {"kind": "thematic"}),
        ("insight", [ONT.community_summary], "superseded", {"kind": "insight"}),
    ]
    for index, (kind, labels, obsolete, meta) in enumerate(kinds, start=1):
        c, conn, session = _coord()
        pid = 40 + index
        row = {
            "id": pid, "content": f"body {kind}", "metadata": meta,
            "superseded": True, "superseded_by": None, "visibility": "global",
            "agent_id": "", "scope": "global", "successor_id": None,
            "successor_metadata": None, "successor_visibility": None,
            "successor_agent_id": None, "successor_scope": None,
        }

        async def fetch(sql, *args, row=row):
            if "successor_id" in sql:
                return [row]
            return []

        conn.fetch = AsyncMock(side_effect=fetch)
        edge = {
            "rel_type": "INFORMED_BY", "rel_props": {}, "labels": labels,
            "pg_id": pid, "start_pg_id": 1, "start_labels": [ONT.fact],
            "end_pg_id": pid, "end_labels": labels,
        }
        hit = {"pg_id": 1, "record_type": "fact", "ref": "fact:1",
               "metadata": {}, "graph_context": [dict(edge)]}
        session.run = AsyncMock(return_value=_Rows([]))
        await c._compose_search_hits(
            [hit], ranked=True, viewer="alice", viewer_scope=None, session=session)
        assert hit["graph_context"] == [], kind
        hit["graph_context"] = [dict(edge)]
        await c._compose_search_hits(
            [hit], ranked=False, viewer="alice", viewer_scope=None, session=session)
        record = hit["graph_context"][0]["record"]
        assert record["obsolete"] == obsolete, kind
        assert record["ref"] == f"{kind}:{pid}"

