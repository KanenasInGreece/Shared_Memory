"""
Tests for the retrieval read contract (REM rebuild stage 1) — the graph
expansion in handle_search must honor everything capture writes.

The contract: "context without relation properties is noise disguised as fact —
a bare MENTIONS in a result cannot be weighed and so poisons decisions at equal
rank with asserted evidence."

Coverage:
  - expansion anchors on ALL record labels (Fact, Decision, Retrospective)
  - pg_id-keyed neighbors (Decision/Retrospective/Fact/CommunitySummary) are
    surfaced with label + pg_id + snippet instead of being silently dropped
  - every edge entry carries direction + the FULL edge property map
    (asserted_by / confidence / role / ...)
  - name-keyed Entity neighbors keep the legacy {rel_type, name, label, aliases}
    shape (backward compatibility) plus the new keys
  - temporal edge properties serialize to JSON (no TypeError)
  - expansion cap is env-tunable (GRAPH_EXPANSION_LIMIT, default 15) and the
    Cypher orders provenance-bearing / non-MENTIONS edges ahead of bare MENTIONS
  - summary results carry pg_id and a graph_context populated from the
    CommunitySummary node's edges (SUMMARIZED_BY walk)
  - a Neo4j failure degrades to empty graph_context, never a failed search
"""

import datetime
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Dynamic import (mirrors test_coordinator.py pattern) ─────────────────────

def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod

coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator


# ── Helpers (test_coordinator.py conventions) ─────────────────────────────────

class _async_ctx:
    """Minimal async context manager wrapping a value."""
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


class _AsyncRows:
    """Async iterable yielding the given record dicts — simulates a Neo4j result."""
    def __init__(self, rows=()):
        self._rows = list(rows)
    def __aiter__(self):
        return self
    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


class _TagRows:
    """Batch rows need anchor_pg_id. Older fixtures only set pg_id."""
    def __init__(self, inner, anchor):
        self._inner = inner
        self._anchor = anchor
    def __aiter__(self):
        return self
    async def __anext__(self):
        rec = await self._inner.__anext__()
        if isinstance(rec, dict):
            rec = dict(rec)
            rec.setdefault("anchor_pg_id", self._anchor)
            if rec.get("rel_pg_id") is None and rec.get("pg_id") is not None:
                rec["rel_pg_id"] = rec["pg_id"]
        return rec


async def _expand_live(c, session, pg, labels, viewer="alice", ranked=True):
    """The live read: batch edges, then one compose. Marks use a separate session."""
    real = session.run

    async def run(*args, **kwargs):
        result = real(*args, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return _TagRows(result, pg)

    session.run = run
    raw = await c._expand_graph_context_batch(session, [pg], tuple(labels))
    if tuple(labels) == (coordinator_mod.ONT.community_summary,):
        kind = "summary"
    else:
        kind = coordinator_mod._anchor_kind(labels)
    hit = {
        "pg_id": pg, "record_type": kind, "ref": f"{kind}:{pg}",
        "metadata": {}, "graph_context": raw.get(pg, []),
    }
    marks = MagicMock()
    marks.run = AsyncMock(return_value=_AsyncRows([]))
    await c._compose_search_hits(
        [hit], ranked=ranked, viewer=viewer, viewer_scope=None, session=marks)
    return hit["graph_context"]


async def _compose_map(c, raw, labels, viewer="alice", ranked=True):
    """Render a batch map the way search does, after the graph read."""
    if tuple(labels) == (coordinator_mod.ONT.community_summary,):
        kind = "summary"
    else:
        kind = coordinator_mod._anchor_kind(labels)
    hits = [{
        "pg_id": pid, "record_type": kind, "ref": f"{kind}:{pid}",
        "metadata": {}, "graph_context": edges,
    } for pid, edges in raw.items()]
    marks = MagicMock()
    marks.run = AsyncMock(return_value=_AsyncRows([]))
    await c._compose_search_hits(
        hits, ranked=ranked, viewer=viewer, viewer_scope=None, session=marks)
    return {hit["pg_id"]: hit["graph_context"] for hit in hits}


def _make_request(body: dict, authenticated_agent: str | None = None) -> MagicMock:
    state = {"authenticated_agent": authenticated_agent, "principal": None}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator_with_mocks():
    """MemoryCoordinator whose pool and neo4j are mocked out."""
    c = MemoryCoordinator()

    mock_conn = AsyncMock()
    mock_conn.fetchrow   = AsyncMock(return_value=None)
    mock_conn.fetch      = AsyncMock(return_value=[])
    mock_conn.execute    = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_async_ctx(None))

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_async_ctx(mock_conn))
    c._pool = mock_pool

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncRows())
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_async_ctx(mock_session))
    c._neo4j = mock_neo4j

    return c, mock_conn, mock_session


def _row(**overrides) -> dict:
    """One expansion-result record with sane defaults."""
    row = {
        "labels": ["Entity"], "name": "Neo4j", "pg_id": None,
        "rel_type": "MENTIONS", "direction": "out",
        "rel_props": {}, "snippet": None, "aliases": [],
    }
    row.update(overrides)
    return row


def _search_mocks(c, mock_session, rows_per_call, score=2.0):
    """Patch _embed + the reranker for one handle_search call."""
    mock_session.run = AsyncMock(side_effect=[_AsyncRows(r) for r in rows_per_call])
    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": 0, "relevance_score": score}]
    })
    return mock_reranker


async def _run_search(c, mock_reranker, query="anything"):
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)
            resp = await c.handle_search(_make_request({"query": query, "limit": 5}))
    assert resp.status == 200
    return json.loads(resp.text)["results"]


# ── (a) anchor covers ALL record labels ───────────────────────────────────────

@pytest.mark.asyncio
async def test_expansion_anchors_fact_decision_and_retrospective():
    """The expansion Cypher must anchor on Fact OR Decision OR Retrospective —
    Decision and Retrospective results get graph context, not just Facts."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    # handle_search now batches all ranked hits' expansion into ONE Neo4j call
    # (_expand_graph_context_batch) — rows carry anchor_pg_id (the grouping
    # key) plus rel_pg_id for the related node's own id.
    rows = [_row(labels=["Retrospective"], name=None, anchor_pg_id=577, rel_pg_id=719,
                 rel_type="HAD_OUTCOME", direction="in",
                 rel_props={"trigger": True}, snippet="held up well")]
    mock_reranker = _search_mocks(c, mock_session, [rows])

    # One Tier-1 candidate that is a DECISION record.
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 577, "content": "we decided X",
         "metadata": {"entities": [], "source": "claude", "type": "decision"}},
    ])

    results = await _run_search(c, mock_reranker, "decision X")
    ctx = results[0]["graph_context"]
    assert ctx, "a Decision anchor must produce graph context"

    cypher = mock_session.run.await_args_list[0].args[0]
    ont = coordinator_mod.ONT
    assert f"n:{ont.fact}" in cypher
    assert f"n:{ont.decision}" in cypher
    assert f"n:{ont.retrospective}" in cypher
    assert mock_session.run.await_args_list[0].kwargs["pg_ids"] == [577]


# ── batched expansion — one Neo4j round-trip per search, not one per result ──

@pytest.mark.asyncio
async def test_search_expansion_is_batched_not_one_call_per_hit():
    """Code-review finding: handle_search issued one _expand_graph_context
    round-trip PER Tier-1 hit (an N+1 pattern, up to ~102 sequential queries
    at limit=100). With 3 ranked hits, Neo4j must be called exactly ONCE for
    the fact/decision/retrospective expansion — via UNWIND, not a loop — and
    each hit's graph_context must still be attributed to the right anchor.

    ⛔ RE-RULED (`decision:1736`): this counted TOTAL session.run calls, which
    made "the expansion is batched" and "the search issues exactly one Cypher"
    the same assertion. They are not the same, and conflating them means the
    next query added for any reason reads as a broken batch. The property is
    PER PURPOSE and PER HIT COUNT: the expansion is one round trip for three
    anchors. Counted by which query, not how many."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact one",   "metadata": {"entities": [], "source": "claude"}},
        {"id": 2, "content": "fact two",   "metadata": {"entities": [], "source": "claude"}},
        {"id": 3, "content": "fact three", "metadata": {"entities": [], "source": "claude"}},
    ])
    # One batched result set covering all three anchors, out of anchor order
    # (2, then 1, then 3) to prove grouping is by anchor_pg_id, not row order.
    rows = [
        _row(labels=["Entity"], name="B", anchor_pg_id=2, rel_type="MENTIONS"),
        _row(labels=["Entity"], name="A", anchor_pg_id=1, rel_type="MENTIONS"),
        _row(labels=["Entity"], name="C", anchor_pg_id=3, rel_type="MENTIONS"),
        _row(labels=["Entity"], name="A2", anchor_pg_id=1, rel_type="MENTIONS"),
    ]
    mock_session.run = AsyncMock(return_value=_AsyncRows(rows))

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={"results": [
        {"index": 0, "relevance_score": 3.0},
        {"index": 1, "relevance_score": 2.0},
        {"index": 2, "relevance_score": 1.0},
    ]})

    results = await _run_search(c, mock_reranker, "three facts")

    # Exactly one EXPANSION round-trip for the whole batch of 3 hits — not 3.
    expansion = [call for call in mock_session.run.await_args_list
                 if "OPTIONAL MATCH (n)-[r]-(related)" in call.args[0]]
    assert len(expansion) == 1
    assert sorted(expansion[0].kwargs["pg_ids"]) == [1, 2, 3]
    # Whatever else the search asks the graph, it asks per SEARCH, never per
    # hit: no query may carry a single anchor when three were ranked.
    for call in mock_session.run.await_args_list:
        assert sorted(call.kwargs["pg_ids"]) == [1, 2, 3]

    by_pg_id = {r["pg_id"]: r for r in results if r["tier"] == "fact"}

    def _names(ctx):
        return {e["relation"]["to"]["name"] for e in ctx if "relation" in e}

    assert _names(by_pg_id[1]["graph_context"]) == {"A", "A2"}
    assert _names(by_pg_id[2]["graph_context"]) == {"B"}
    assert _names(by_pg_id[3]["graph_context"]) == {"C"}


# ── (b) pg_id-keyed neighbors are never dropped ───────────────────────────────

@pytest.mark.asyncio
async def test_pgid_keyed_neighbor_surfaces_with_label_pgid_snippet():
    """F4: A Retrospective with no Postgres row keeps its HAD_OUTCOME relation and leaves the far end unnamed. The node snippet is not the entry."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Retrospective"], name=None, pg_id=719,
             rel_type="HAD_OUTCOME", direction="out",
             rel_props={"asserted_by": "operator"},
             snippet="validated: held up in production"),
    ]))

    ctx = await _expand_live(c, 
        mock_session, 577,
        (coordinator_mod.ONT.fact, coordinator_mod.ONT.decision,
         coordinator_mod.ONT.retrospective))

    assert len(ctx) == 1
    rel = ctx[0]["relation"]
    assert rel["relation"] == "HAD_OUTCOME"
    assert rel["from"] == "fact:577"
    assert rel["to"] is None
    assert rel["props"]["asserted_by"] == "operator"
    assert "record" not in ctx[0]
    assert "validated: held up in production" not in json.dumps(ctx)


@pytest.mark.asyncio
async def test_pgid_keyed_neighbor_without_text_gets_null_snippet():
    """A live summary neighbour is a pointer entry: its ref and chars, and no text."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["CommunitySummary"], name=None, pg_id=88,
             rel_type="SUMMARIZED_BY", rel_props={}, snippet=None),
    ]))
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 88, "content": "Postgres whole summary", "content_chars": 45,
         "metadata": {"kind": "thematic"}, "visibility": "global",
         "agent_id": None, "scope": "global", "superseded": False},
    ])
    ctx = await _expand_live(c, 
        mock_session, 42, (coordinator_mod.ONT.fact,))
    record = ctx[0]["record"]
    assert record["ref"] == "summary:88"
    assert record["text_is"] == "pointer"
    assert "text" not in record
    assert record["full_record"]["chars"] == 45
    assert "snippet" not in record


# ── (b′) ADR node props on the one-hop neighbor (decision 909) ────────────────

def test_fact_kind_is_derived_from_source_ref():
    """F28: fact_kind is derived from source_ref. Node adr copies are not a reader.

    Mutation: read adr_fact_kind off the graph row again.
    """
    fn = coordinator_mod.fact_kind_from_source_ref
    assert fn("coordinator.py#L42") == "measured"
    assert fn(None) == "discussion"
    assert fn("") == "discussion"
    assert not hasattr(coordinator_mod, "_neighbor_adr_props")
    assert not hasattr(coordinator_mod, "_decision_payload_props")


def test_a_string_alternatives_value_is_one_entry_not_a_pile_of_letters():
    """The guard fact 910 asked for, now on the store that actually holds the
    value.

    Postgres holds a JSON array for every decision that has the key today, so a
    bare `list()` is a passthrough and nothing looks wrong. But this value HAS
    been stored as a JSON string before — and `list()` on a string shreds it
    into single characters, so three alternatives become several hundred
    one-character ones and every reader downstream renders garbage. The trap
    moved stores when the read moved; the guard moved with it.

    A string is ONE entry. Asserted on the value, not on the source text: a
    guard disabled with `if False and …` leaves its own text in the file.
    """
    shreddable = '["flat GROUNDED_IN", "no typing"]'
    entry = coordinator_mod.render_record_entry(
        {"content": "stored body", "content_chars": 11, "type": "decision",
         "ref": "decision:9", "readable": True, "superseded": False,
         "metadata": {"decision": {"rationale": "because",
                                   "alternatives": shreddable,
                                   "confidence": "high"}}},
        None, "decision:9", {"rows": {}}, ranked=False, search_hit=False)
    assert entry["text"] == "because"
    assert "alternatives" not in entry
    assert "confidence" not in entry
    assert shreddable[0] not in entry["text"]


@pytest.mark.asyncio
async def test_decision_neighbor_payload_comes_from_postgres_not_the_graph():
    """F6: A decision neighbour's text is its whole rationale. Alternatives stay off the entry, and adr_props is absent."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Decision"], name=None, pg_id=579,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="NODE TITLE MUST NOT LEAD"),
    ]))
    rationale = "Grounding relations should be role-typed"
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 579, "content": "the whole decision row, longer than the rationale",
         "content_chars": 52,
         "metadata": {"type": "decision", "decision": {
             "rationale": rationale,
             "alternatives": ["keep the flat GROUNDED_IN"],
             "confidence": "high"}},
         "visibility": "global", "agent_id": None, "scope": "global",
         "superseded": False},
    ])
    ctx = await _expand_live(c, 
        mock_session, 123, (coordinator_mod.ONT.community_summary,))
    record = ctx[0]["record"]
    assert record["ref"] == "decision:579"
    assert record["text"] == rationale
    assert record["text_is"] == "summary"
    assert "adr_props" not in record
    assert "keep the flat GROUNDED_IN" not in record["text"]
    assert "NODE TITLE MUST NOT LEAD" not in json.dumps(ctx)
    assert mock_conn.fetch.await_count == 2
    doc_calls = [call for call in mock_conn.fetch.await_args_list
                 if "technical_docs" in call.args[0]]
    assert len(doc_calls) == 1
    assert "id = ANY($1::bigint[])" in doc_calls[0].args[0]
    assert doc_calls[0].args[1] == [579]


@pytest.mark.asyncio
async def test_only_decision_neighbors_are_dereferenced():
    """F28: A fact neighbour takes fact_kind from Postgres metadata, not the node, and an entity is not a record."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Fact"], name=None, pg_id=601, rel_type="GROUNDED_IN",
             direction="out", rel_props={}, snippet="tests pass",
             adr_fact_kind="tested", adr_source_ref="from-the-node"),
        _row(labels=["Entity"], name="Postgres", pg_id=None,
             rel_type="MENTIONS", direction="out", rel_props={}, snippet=None),
    ]))
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 601, "content": "tests pass", "content_chars": 10,
         "metadata": {"type": "fact", "source_ref": "src/lock.py"},
         "visibility": "global", "agent_id": None, "scope": "global",
         "superseded": False},
    ])
    ctx = await _expand_live(c, 
        mock_session, 123, (coordinator_mod.ONT.community_summary,))
    record = ctx[0]["record"]
    assert record["source_ref"] == "src/lock.py"
    assert record["fact_kind"] == "measured"
    assert "adr_props" not in record
    assert "from-the-node" not in json.dumps(ctx)
    assert ctx[1]["relation"]["to"]["name"] == "Postgres"
    assert "record" not in ctx[1]
    assert not any("metadata->'decision'" in call.args[0] for call in mock_conn.fetch.await_args_list)
    assert any("technical_docs" in call.args[0] for call in mock_conn.fetch.await_args_list)


@pytest.mark.asyncio
async def test_a_failed_payload_dereference_never_fails_the_walk():
    """F4a: A Postgres failure drops every record-ended neighbour and keeps an entity relation. The walk does not raise."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Decision"], name=None, pg_id=579,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="Grounding relations should be role-typed"),
        _row(labels=["Entity"], name="LockOrder", rel_type="MENTIONS",
             direction="out", rel_props={}),
    ]))
    mock_conn.fetch = AsyncMock(side_effect=RuntimeError("pool exhausted"))
    ctx = await _expand_live(c, 
        mock_session, 123, (coordinator_mod.ONT.community_summary,))
    assert len(ctx) == 1
    assert ctx[0]["relation"]["to"]["name"] == "LockOrder"
    assert "decision:579" not in json.dumps(ctx)
    assert "role-typed" not in json.dumps(ctx)

    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Decision"], name=None, anchor_pg_id=91, rel_pg_id=579,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="Grounding relations should be role-typed"),
        _row(labels=["Entity"], name="LockOrder", anchor_pg_id=91,
             rel_type="MENTIONS", direction="out", rel_props={}),
    ]))
    raw = await c._expand_graph_context_batch(
        mock_session, [91], (coordinator_mod.ONT.community_summary,))
    out = await _compose_map(c, raw, (coordinator_mod.ONT.community_summary,))
    assert len(out[91]) == 1
    assert out[91][0]["relation"]["to"]["name"] == "LockOrder"
    assert "decision:579" not in json.dumps(out)


@pytest.mark.asyncio
async def test_batched_expansion_dereferences_every_anchor_in_one_query():
    """F6: Two decision neighbours hydrate in one query. Each entry's text is its rationale, and adr_props is absent."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Decision"], name=None, anchor_pg_id=91, rel_pg_id=579,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="role-typed grounding"),
        _row(labels=["Decision"], name=None, anchor_pg_id=92, rel_pg_id=580,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="the outbox is atomic"),
    ]))

    def _decision(pid, rationale, confidence):
        return {"id": pid, "content": "stored decision body",
                "content_chars": 20,
                "metadata": {"type": "decision", "decision": {
                    "rationale": rationale, "confidence": confidence,
                    "alternatives": ["keep the flat GROUNDED_IN"]}},
                "visibility": "global", "agent_id": None, "scope": "global",
                "superseded": False}

    mock_conn.fetch = AsyncMock(return_value=[
        _decision(579, "role-typed grounding", "high"),
        _decision(580, "the outbox is atomic", "medium"),
    ])
    raw = await c._expand_graph_context_batch(
        mock_session, [91, 92], (coordinator_mod.ONT.community_summary,))
    out = await _compose_map(c, raw, (coordinator_mod.ONT.community_summary,))
    assert out[91][0]["record"]["text"] == "role-typed grounding"
    assert out[92][0]["record"]["text"] == "the outbox is atomic"
    assert "adr_props" not in out[91][0]["record"]
    assert "adr_props" not in out[92][0]["record"]
    assert "keep the flat GROUNDED_IN" not in out[91][0]["record"]["text"]
    assert mock_conn.fetch.await_count == 2
    doc_calls = [call for call in mock_conn.fetch.await_args_list
                 if "technical_docs" in call.args[0]]
    sum_calls = [call for call in mock_conn.fetch.await_args_list
                 if "community_summaries" in call.args[0]]
    assert len(doc_calls) == 1 and doc_calls[0].args[1] == [579, 580]
    assert len(sum_calls) == 1 and sum_calls[0].args[1] == [91, 92]


@pytest.mark.asyncio
async def test_fact_neighbor_surfaces_fact_kind_and_source_ref():
    """F28: fact_kind and source_ref come from the Postgres row. The node's adr copies do not."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Fact"], name=None, pg_id=804,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="the embedding model contract is fixed",
             adr_fact_kind="tested", adr_source_ref="from-the-node"),
    ]))
    mock_conn.fetch = AsyncMock(return_value=[{
        "id": 804, "content": "the embedding model contract is fixed in full",
        "content_chars": 48,
        "metadata": {"type": "fact", "source_ref": "design-doc.pdf"},
        "visibility": "global", "agent_id": None, "scope": "global",
        "superseded": False,
    }])
    ctx = await _expand_live(c, 
        mock_session, 88, (coordinator_mod.ONT.community_summary,))
    record = ctx[0]["record"]
    assert record["source_ref"] == "design-doc.pdf"
    assert record["fact_kind"] == "researched"
    assert "adr_props" not in record
    assert "from-the-node" not in json.dumps(ctx)


@pytest.mark.asyncio
async def test_neighbor_without_adr_props_omits_the_key():
    """A neighbor carrying no ADR node property gets no adr_props key at all —
    additive, so existing consumers are unaffected."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["CommunitySummary"], name=None, pg_id=88,
             rel_type="SUMMARIZED_BY", rel_props={}, snippet=None),
    ]))
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 88, "content": "live summary", "content_chars": 50,
         "metadata": {"kind": "thematic"}, "visibility": "global",
         "agent_id": None, "scope": "global", "superseded": False},
    ])
    ctx = await _expand_live(c, 
        mock_session, 42, (coordinator_mod.ONT.fact,))
    assert len(ctx) == 1
    assert "adr_props" not in ctx[0]["record"]
    assert ctx[0]["record"]["text_is"] == "pointer"


def test_expansion_cypher_projects_adr_node_props_both_forms():
    """Stubs never execute Cypher, so pin the projection: rem_summary, properties(r), both ends, and no REFERENCES. ADR node copies stay out (decision:909: provenance is the record's, not a graph copy)."""
    import inspect
    src_batch = inspect.getsource(
        coordinator_mod.MemoryCoordinator._expand_graph_context_batch)
    for body in (src_batch,):
        assert "related.rem_summary AS rem_summary" in body
        assert "related.rem_processed AS rem_processed" in body
        assert "related.rem_prompt_version AS rem_prompt_version" in body
        assert "properties(r) AS rel_props" in body
        assert "startNode(r).pg_id AS start_pg_id" in body
        assert "endNode(r).pg_id AS end_pg_id" in body
        assert "'REFERENCES'" in body
        assert "adr_fact_kind" not in body
        assert "adr_confidence" not in body
        assert "adr_alternatives" not in body
    assert "start_pg_id, start_labels, start_name" in src_batch


# ── (c) direction + full edge property map ────────────────────────────────────

@pytest.mark.asyncio
async def test_edge_entries_carry_direction_and_full_properties():
    """Typed grounding edges surface their FULL property map — asserted_by,
    confidence, role — plus the edge direction. A bare relation name cannot
    be weighed."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Fact"], name=None, pg_id=601,
             rel_type="GROUNDED_IN", direction="out",
             rel_props={"asserted_by": "operator", "confidence": 0.9,
                        "role": "based_on"},
             snippet="tests pass with the new schema"),
        _row(labels=["Entity"], name="OutboxPattern",
             rel_type="MENTIONS", direction="out",
             rel_props={"asserted_by": "rem", "confidence": 0.55,
                        "model": "gemma-4-12b", "run_id": "r-77"}),
    ]))

    ctx = await _expand_live(c, 
        mock_session, 577, (coordinator_mod.ONT.decision,))

    grounded = ctx[0]["relation"]
    assert grounded["from"] == "decision:577"
    assert grounded["props"]["asserted_by"] == "operator"
    assert grounded["props"]["confidence"]  == 0.9
    assert grounded["props"]["role"]        == "based_on"

    machine = ctx[1]["relation"]
    assert machine["props"]["asserted_by"] == "rem"
    assert machine["props"]["confidence"]  == 0.55
    assert machine["props"]["model"]       == "gemma-4-12b"
    assert machine["props"]["run_id"]      == "r-77"


# ── (d) Entity neighbors keep the legacy shape ────────────────────────────────

@pytest.mark.asyncio
async def test_entity_neighbor_keeps_legacy_keys_plus_additive_ones():
    """An entity end keeps name, label and aliases inside the triple. Snippet does not."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Entity"], name="Neo4j", rel_type="MENTIONS",
             direction="out", rel_props={}, aliases=["neo4j", "Neo4J"]),
    ]))
    ctx = await _expand_live(c, 
        mock_session, 42, (coordinator_mod.ONT.fact,))

    end = ctx[0]["relation"]["to"]
    assert ctx[0]["relation"]["relation"] == "MENTIONS"
    assert ctx[0]["relation"]["from"] == "fact:42"
    assert end["name"] == "Neo4j"
    assert end["label"] == "Entity"
    assert end["aliases"] == ["neo4j", "Neo4J"]
    assert ctx[0]["relation"]["props"] == {}
    assert "snippet" not in json.dumps(ctx[0])


@pytest.mark.asyncio
async def test_entity_neighbor_without_aliases_omits_key():
    """Legacy behavior: the aliases key only appears when siblings exist."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Entity"], name="Postgres", aliases=[]),
    ]))
    ctx = await _expand_live(c, 
        mock_session, 42, (coordinator_mod.ONT.fact,))
    assert "aliases" not in ctx[0]["relation"]["to"]


# ── (e) temporal edge properties serialize ────────────────────────────────────

class _FakeNeo4jDateTime:
    """Mimics neo4j.time.DateTime — not JSON-serialisable, exposes iso_format()."""
    def iso_format(self):
        return "2026-07-15T10:30:00+00:00"


@pytest.mark.asyncio
async def test_temporal_edge_property_serializes_without_typeerror():
    """properties(r) can carry neo4j DateTime / stdlib datetime values —
    json.dumps on the entry must not raise."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Fact"], name=None, pg_id=601, rel_type="GROUNDED_IN",
             rel_props={"asserted_by": "operator",
                        "created_at": _FakeNeo4jDateTime(),
                        "recorded":   datetime.datetime(2026, 7, 15, 10, 30)},
             snippet="evidence"),
    ]))
    ctx = await _expand_live(c, 
        mock_session, 577, (coordinator_mod.ONT.decision,))

    dumped = json.dumps(ctx)   # must not raise TypeError
    props = ctx[0]["relation"]["props"]
    assert props["created_at"] == "2026-07-15T10:30:00+00:00"
    assert props["recorded"].startswith("2026-07-15T10:30")
    assert "2026-07-15" in dumped


def test_json_safe_covers_nested_and_unknown_values():
    js = coordinator_mod._json_safe
    assert js({"a": [1, "x", None, datetime.date(2026, 7, 15)]}) == \
        {"a": [1, "x", None, "2026-07-15"]}
    assert js(_FakeNeo4jDateTime()) == "2026-07-15T10:30:00+00:00"
    assert js(object()).startswith("<object")   # str() fallback, never a raise


# ── cap + ranking ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_expansion_cap_is_env_tunable_and_high_signal_ordered():
    """The Cypher binds the GRAPH_EXPANSION_LIMIT cap (default 15) and orders
    provenance-bearing (asserted_by) / non-MENTIONS edges before bare MENTIONS
    so the highest-signal edges survive the cap."""
    assert coordinator_mod.GRAPH_EXPANSION_LIMIT == 15   # default

    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows())
    await c._expand_graph_context_batch(
        mock_session, [42], (coordinator_mod.ONT.fact,))

    call = mock_session.run.await_args
    cypher = call.args[0]
    assert "LIMIT $cap" in cypher
    assert call.kwargs["cap"] == 15
    assert "ORDER BY" in cypher
    assert "r.asserted_by IS NOT NULL" in cypher
    assert f"type(r) = '{coordinator_mod.ONT.entity_link}'" in cypher

    # env override picked up at module load
    with patch.dict(os.environ, {"GRAPH_EXPANSION_LIMIT": "3"}):
        mod = load_coordinator()
        assert mod.GRAPH_EXPANSION_LIMIT == 3
    load_coordinator()   # restore a clean module for later tests


# ── (f) summary→sources walk ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_result_carries_pgid_and_populated_graph_context():
    """The community_summary result exposes its community_summaries.id as pg_id
    and populates graph_context from the CommunitySummary node's SUMMARIZED_BY
    edges (source facts point AT the summary → direction 'in')."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetchrow = AsyncMock(side_effect=[None, {
        "id": 88,
        "content": "Synthesised narrative about the outbox pattern",
        "metadata": {"entity": "OutboxPattern", "domain": "shared-memory"},
        "source_pg_ids": [42, 43],
    }])
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact", "metadata": {"entities": [], "source": "claude"}},
    ])

    summary_rows = [
        _row(labels=["Fact"], name=None, anchor_pg_id=88, rel_pg_id=42,
             rel_type="SUMMARIZED_BY",
             direction="in", rel_props={}, snippet="outbox rows are atomic"),
        _row(labels=["Fact"], name=None, anchor_pg_id=88, rel_pg_id=43,
             rel_type="SUMMARIZED_BY",
             direction="in", rel_props={}, snippet="worker applies async"),
    ]
    # session.run call order: batched summary walk first, then the batched
    # fact expansion (both are single calls now, not one-per-record).
    mock_reranker = _search_mocks(c, mock_session, [summary_rows, []])

    results = await _run_search(c, mock_reranker, "outbox pattern")

    cs = results[0]
    assert cs["tier"] == "community_summary"
    assert cs["pg_id"] == 88
    assert len(cs["graph_context"]) == 2
    assert {e["relation"]["relation"] for e in cs["graph_context"]} == {"SUMMARIZED_BY"}
    assert all(e["relation"]["to"] == "summary:88" for e in cs["graph_context"])
    assert all(e["relation"]["from"] is None for e in cs["graph_context"])
    assert "outbox rows are atomic" not in json.dumps(cs["graph_context"])

    # the walk anchored on the CommunitySummary label with the summary's id
    walk_call = mock_session.run.await_args_list[0]
    assert f"n:{coordinator_mod.ONT.community_summary}" in walk_call.args[0]
    assert walk_call.kwargs["pg_ids"] == [88]


@pytest.mark.asyncio
async def test_insight_summary_also_carries_pgid_and_walks_graph():
    """The insight_summary result gets the same pg_id + graph walk."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"id": 91, "content": "Cross-project principle",
         "metadata": {"kind": "insight"}, "source_pg_ids": [245]},
        None,
    ])
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact", "metadata": {"entities": [], "source": "claude"}},
    ])
    ins_rows = [_row(labels=["Decision"], name=None, anchor_pg_id=91, rel_pg_id=245,
                     rel_type="SUMMARIZED_BY", direction="in",
                     rel_props={}, snippet="we decided the outbox")]
    mock_reranker = _search_mocks(c, mock_session, [ins_rows, []])

    results = await _run_search(c, mock_reranker, "outbox")
    ins = results[0]
    assert ins["tier"] == "insight_summary"
    assert ins["pg_id"] == 91
    assert ins["graph_context"][0]["relation"]["relation"] == "SUMMARIZED_BY"
    assert ins["graph_context"][0]["relation"]["to"] == "insight:91"
    assert ins["graph_context"][0]["relation"]["from"] is None
    assert "we decided the outbox" not in json.dumps(ins["graph_context"])


@pytest.mark.asyncio
async def test_summary_without_id_column_degrades_gracefully():
    """Pre-change stubs / schemas without the id in the row: pg_id is None and
    the walk is skipped — never a KeyError, never a failed search."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[None, {
        "content": "narrative", "metadata": {}, "source_pg_ids": [1],
    }])
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact", "metadata": {"entities": [], "source": "claude"}},
    ])
    mock_reranker = _search_mocks(c, mock_session, [[]])

    results = await _run_search(c, mock_reranker)
    assert results[0]["pg_id"] is None
    assert results[0]["graph_context"] == []


# ── (g) Neo4j failure degrades ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_neo4j_failure_degrades_to_empty_graph_context():
    """A Neo4j error during expansion returns [] — graph context enriches a
    search, it never fails one."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(side_effect=Exception("neo4j down"))
    ctx = await _expand_live(c, 
        mock_session, 42, (coordinator_mod.ONT.fact,))
    assert ctx == []


@pytest.mark.asyncio
async def test_search_survives_neo4j_failure_on_both_walks():
    """handle_search still returns 200 with empty graph_context everywhere when
    every Neo4j call raises (summary walk AND per-record expansion)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[None, {
        "id": 88, "content": "narrative", "metadata": {}, "source_pg_ids": [1],
    }])
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact", "metadata": {"entities": [], "source": "claude"}},
    ])
    mock_session.run = AsyncMock(side_effect=Exception("neo4j down"))

    # One candidate list: index 0 is the summary, index 1 the fact. Both walks
    # must degrade to [] independently when Neo4j is down.
    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": 0, "relevance_score": 2.0},
                    {"index": 1, "relevance_score": 1.0}]
    })
    results = await _run_search(c, mock_reranker)

    assert results[0]["tier"] == "community_summary"
    assert results[0]["graph_context"] == []
    assert results[1]["tier"] == "fact"
    assert results[1]["graph_context"] == []


# ── (e) DERIVED BELONGING on a judgement hit (decision:1736) ──────────────────
#
# C1 removed every writer of a judgement's inherited sections, so "where does
# this decision belong" stopped being answerable from its own edges. It is now
# answered on READ, and graph expansion is where a search hit picks it up. The
# entry is ADDITIVE and is not an edge: no rel_type, no neighbour.
#
# ⚠ These tests stub Cypher like every other test in this file — they prove the
# WIRING (which query is issued, for which anchors, and what the caller does
# with the answer). What the traversal actually returns on the live graph is a
# separate, owed verification (`fact:1194`).

def _belonging_row(pg_id, project, domains):
    return {"anchor_pg_id": pg_id, "project": project, "domains": domains}


def _dispatching_session(edge_rows, belonging_rows, fail_belonging=False):
    """A session that answers each query by WHICH query it is, never by call
    order — an ordered side_effect would make ADDING a query look like a broken
    expansion, and this change adds one."""
    from ontology import derived_belonging_cypher
    calls = []
    belonging_q = derived_belonging_cypher()

    async def run(q, **kw):
        calls.append((q, kw))
        if q == belonging_q:
            if fail_belonging:
                raise RuntimeError("neo4j is having a moment")
            return _AsyncRows(list(belonging_rows))
        return _AsyncRows(list(edge_rows))

    session = MagicMock()
    session.run = run
    return session, calls


@pytest.mark.asyncio
async def test_a_judgement_hit_carries_its_derived_belonging():
    """The worked example's read-out (`fact:1735`): a retrospective resolves to
    its decision's project and to the union of its decision's own section and
    the section of the fact it grounds in. The entry sits AFTER the edges — the
    expansion cap governs edges, and this is not one."""
    c, _, _ = _coordinator_with_mocks()
    session, calls = _dispatching_session(
        edge_rows=[_row(labels=["Decision"], name=None, pg_id=42,
                        rel_type="HAD_OUTCOME", direction="in", rel_props={},
                        snippet="the decision this verdict judges")],
        belonging_rows=[_belonging_row(913, "B", ["literature", "tests"])],
    )
    ctx = await _expand_live(c, 
        session, 913,
        (coordinator_mod.ONT.fact, coordinator_mod.ONT.decision,
         coordinator_mod.ONT.retrospective))

    assert ctx[0]["relation"]["relation"] == "HAD_OUTCOME", "the edges come first, unchanged"
    assert ctx[-1] == {"belonging": {"project": "B",
                                     "domains": ["literature", "tests"]}}
    # Not an edge, and never mistakable for one.
    assert "rel_type" not in ctx[-1] and "direction" not in ctx[-1]


@pytest.mark.asyncio
async def test_the_belonging_query_is_the_ontology_helper_verbatim():
    """One expression of the rule. A query built here — even an identical one —
    is a second copy free to drift from the one the tests in
    `test_derived_belonging.py` pin."""
    from ontology import derived_belonging_cypher
    c, _, _ = _coordinator_with_mocks()
    session, calls = _dispatching_session(
        edge_rows=[], belonging_rows=[_belonging_row(913, "B", [])])
    await _expand_live(c, 
        session, 913, (coordinator_mod.ONT.retrospective,))
    issued = [q for q, _ in calls if q == derived_belonging_cypher()]
    assert issued == [derived_belonging_cypher()]
    assert calls[-1][1]["pg_ids"] == [913]


@pytest.mark.asyncio
async def test_a_fact_hit_gets_no_belonging_and_pays_for_no_query():
    """A fact's belonging IS its own bare edges — already in the list. Deriving
    one would be inventing an answer it already has, and paying for it."""
    c, _, _ = _coordinator_with_mocks()
    session, calls = _dispatching_session(
        edge_rows=[_row(labels=["Entity"], name="Neo4j", rel_type="MENTIONS")],
        belonging_rows=[_belonging_row(42, "B", ["architecture"])],
    )
    ctx = await _expand_live(c, 
        session, 42, (coordinator_mod.ONT.fact,))
    assert all("belonging" not in e for e in ctx)
    assert len(calls) == 1, "a fact-only expansion must issue no second query"


@pytest.mark.asyncio
async def test_a_summary_expansion_pays_for_no_belonging_query_either():
    """A CommunitySummary carries no belonging edges at all (`decision:1736`
    answers it the same way: none, traverse), so it is not an anchor here."""
    c, _, _ = _coordinator_with_mocks()
    session, calls = _dispatching_session(
        edge_rows=[], belonging_rows=[_belonging_row(88, "B", ["ops"])])
    await _expand_live(c, 
        session, 88, (coordinator_mod.ONT.community_summary,))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_batch_form_derives_belonging_in_one_round_trip():
    """The batch form exists to kill an N+1; a bounded query PER judgement hit
    would have reinstated one for exactly the hits a lifecycle-aware search
    returns most of. One query, every anchor, each answer filed against the
    anchor its row names — never by position, because an anchor that resolves
    to nothing returns no row at all."""
    c, _, _ = _coordinator_with_mocks()
    session, calls = _dispatching_session(
        edge_rows=[
            _row(labels=["Fact"], name=None, anchor_pg_id=91, rel_pg_id=601,
                 rel_type="GROUNDED_IN", direction="out", rel_props={},
                 snippet="tests@B"),
            _row(labels=["Fact"], name=None, anchor_pg_id=92, rel_pg_id=602,
                 rel_type="GROUNDED_IN", direction="out", rel_props={},
                 snippet="architecture@A"),
        ],
        # 93 is a FACT anchor: the query returns no row for it. And the two
        # rows arrive OUT OF ANCHOR ORDER (92 before 91) on purpose — filing an
        # answer by its position in `pg_ids` rather than by the anchor its row
        # NAMES would swap them and stay green on an in-order fixture.
        belonging_rows=[_belonging_row(92, "B", ["literature", "tests"]),
                        _belonging_row(91, "B", ["literature"])],
    )
    out = await c._expand_graph_context_batch(
        session, [91, 92, 93],
        (coordinator_mod.ONT.fact, coordinator_mod.ONT.decision,
         coordinator_mod.ONT.retrospective))

    assert out[91][-1] == {"belonging": {"project": "B",
                                         "domains": ["literature"]}}
    assert out[92][-1] == {"belonging": {"project": "B",
                                         "domains": ["literature", "tests"]}}
    assert all("belonging" not in e for e in out[93])
    from ontology import derived_belonging_cypher
    assert [q for q, _ in calls if q == derived_belonging_cypher()] == [
        derived_belonging_cypher()]


@pytest.mark.asyncio
async def test_a_belonging_failure_leaves_the_expansion_intact():
    """Same contract as the expansion it enriches: graph context enriches a
    search, it never fails one. A derivation that throws costs the reader the
    belonging entry and nothing else."""
    c, _, _ = _coordinator_with_mocks()
    session, _calls = _dispatching_session(
        edge_rows=[_row(labels=["Decision"], name=None, pg_id=42,
                        rel_type="HAD_OUTCOME", direction="in", rel_props={},
                        snippet="the decision")],
        belonging_rows=[], fail_belonging=True,
    )
    ctx = await _expand_live(c, 
        session, 913, (coordinator_mod.ONT.retrospective,))
    assert len(ctx) == 1 and ctx[0]["relation"]["relation"] == "HAD_OUTCOME"
    assert all("belonging" not in e for e in ctx)


@pytest.mark.asyncio
async def test_a_judgement_that_resolves_to_no_section_still_says_so():
    """"None" is a valid answer for a decision that asserted nothing and rests
    on facts filed under another project. An EMPTY list is the honest result and
    must reach the reader — dropping the entry would make "no sections" and "not
    computed" indistinguishable."""
    c, _, _ = _coordinator_with_mocks()
    session, _calls = _dispatching_session(
        edge_rows=[], belonging_rows=[_belonging_row(700, "B", [])])
    ctx = await _expand_live(c, 
        session, 700, (coordinator_mod.ONT.decision,))
    assert ctx == [{"belonging": {"project": "B", "domains": []}}]
