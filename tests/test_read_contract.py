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
    each hit's graph_context must still be attributed to the right anchor."""
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

    # Exactly one Neo4j round-trip for the whole batch of 3 hits — not 3.
    assert mock_session.run.await_count == 1
    called_pg_ids = mock_session.run.await_args.kwargs["pg_ids"]
    assert sorted(called_pg_ids) == [1, 2, 3]

    by_pg_id = {r["pg_id"]: r for r in results if r["tier"] == "fact"}
    assert {e["name"] for e in by_pg_id[1]["graph_context"]} == {"A", "A2"}
    assert {e["name"] for e in by_pg_id[2]["graph_context"]} == {"B"}
    assert {e["name"] for e in by_pg_id[3]["graph_context"]} == {"C"}


# ── (b) pg_id-keyed neighbors are never dropped ───────────────────────────────

@pytest.mark.asyncio
async def test_pgid_keyed_neighbor_surfaces_with_label_pgid_snippet():
    """A Retrospective hanging off a Decision via HAD_OUTCOME (no name property)
    must appear as {rel_type, direction, properties, label, pg_id, snippet} —
    the old `if rec['name']` filter silently dropped it."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Retrospective"], name=None, pg_id=719,
             rel_type="HAD_OUTCOME", direction="out",
             rel_props={"asserted_by": "operator"},
             snippet="validated: held up in production"),
    ]))

    ctx = await c._expand_graph_context(
        mock_session, 577,
        (coordinator_mod.ONT.fact, coordinator_mod.ONT.decision,
         coordinator_mod.ONT.retrospective))

    assert len(ctx) == 1
    entry = ctx[0]
    assert entry["rel_type"] == "HAD_OUTCOME"
    assert entry["label"]    == "Retrospective"
    assert entry["pg_id"]    == 719
    assert entry["snippet"]  == "validated: held up in production"
    assert entry["direction"] == "out"
    assert "name" not in entry


@pytest.mark.asyncio
async def test_pgid_keyed_neighbor_without_text_gets_null_snippet():
    """A pg_id-keyed neighbor with no text-bearing property (e.g. a bare
    CommunitySummary node) surfaces with snippet=None — still not dropped."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["CommunitySummary"], name=None, pg_id=88,
             rel_type="SUMMARIZED_BY", rel_props={}, snippet=None),
    ]))
    ctx = await c._expand_graph_context(
        mock_session, 42, (coordinator_mod.ONT.fact,))
    assert ctx[0]["pg_id"] == 88
    assert ctx[0]["snippet"] is None


# ── (b′) ADR node props on the one-hop neighbor (decision 909) ────────────────

def test_neighbor_adr_props_collects_only_set_keys():
    """_neighbor_adr_props packs a folded decision's confidence/alternatives and
    a folded fact's fact_kind/source_ref, dropping unset keys and JSON-safing
    the alternatives list. A neighbor carrying none returns {}."""
    fn = coordinator_mod._neighbor_adr_props
    dec = fn({"adr_confidence": "high",
              "adr_alternatives": ["flat GROUNDED_IN", "no typing"],
              "adr_fact_kind": None, "adr_source_ref": None})
    assert dec == {"confidence": "high",
                   "alternatives": ["flat GROUNDED_IN", "no typing"]}

    fact = fn({"adr_confidence": None, "adr_alternatives": None,
               "adr_fact_kind": "measured", "adr_source_ref": "coordinator.py#L42"})
    assert fact == {"fact_kind": "measured", "source_ref": "coordinator.py#L42"}

    # A bare neighbor (e.g. a CommunitySummary) carries none → no adr_props.
    assert fn({"adr_confidence": None, "adr_alternatives": None,
               "adr_fact_kind": None, "adr_source_ref": None}) == {}
    # Missing columns entirely (older single-anchor rows / stubs) are tolerated.
    assert fn({}) == {}


def test_a_string_alternatives_property_is_one_entry_not_a_pile_of_letters():
    """The guard fact 910 asked for, never built until now.

    `Decision.alternatives` holds a Neo4j LIST OF STRING on all 223 nodes today,
    so a bare `list()` is a passthrough and nothing looks wrong. But this same
    property HAS been written as a JSON string before — and `list()` on a string
    shreds it into single characters, so three alternatives become several
    hundred one-character ones and every reader downstream renders garbage.

    A string is ONE entry. Asserted on the value, not on the source text: a
    guard disabled with `if False and …` leaves its own text in the file.
    """
    fn = coordinator_mod._neighbor_adr_props
    shreddable = '["flat GROUNDED_IN", "no typing"]'
    assert fn({"adr_alternatives": shreddable}) == {"alternatives": [shreddable]}
    # A real list is still passed through unchanged.
    assert fn({"adr_alternatives": ["a", "b"]}) == {"alternatives": ["a", "b"]}
    # A tuple (driver variation) is a sequence, not a scalar.
    assert fn({"adr_alternatives": ("a", "b")}) == {"alternatives": ["a", "b"]}


@pytest.mark.asyncio
async def test_decision_neighbor_surfaces_confidence_and_alternatives():
    """An insight_summary folds Decisions; the folded Decision one hop away now
    carries its confidence + alternatives in adr_props WITHOUT a second query."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Decision"], name=None, pg_id=579,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="Grounding relations should be role-typed",
             adr_confidence="high",
             adr_alternatives=["keep the flat GROUNDED_IN"]),
    ]))
    ctx = await c._expand_graph_context(
        mock_session, 123, (coordinator_mod.ONT.community_summary,))
    assert ctx[0]["pg_id"] == 579
    assert ctx[0]["adr_props"] == {
        "confidence": "high",
        "alternatives": ["keep the flat GROUNDED_IN"],
    }


@pytest.mark.asyncio
async def test_fact_neighbor_surfaces_fact_kind_and_source_ref():
    """A thematic community_summary folds Facts; the folded Fact one hop away now
    carries its evidence weight (fact_kind + source_ref) in adr_props."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Fact"], name=None, pg_id=804,
             rel_type="SUMMARIZED_BY", direction="in", rel_props={},
             snippet="the embedding model contract is fixed",
             adr_fact_kind="measured", adr_source_ref="design-doc.pdf#p12"),
    ]))
    ctx = await c._expand_graph_context(
        mock_session, 88, (coordinator_mod.ONT.community_summary,))
    assert ctx[0]["adr_props"] == {
        "fact_kind": "measured", "source_ref": "design-doc.pdf#p12"}


@pytest.mark.asyncio
async def test_neighbor_without_adr_props_omits_the_key():
    """A neighbor carrying no ADR node property gets no adr_props key at all —
    additive, so existing consumers are unaffected."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["CommunitySummary"], name=None, pg_id=88,
             rel_type="SUMMARIZED_BY", rel_props={}, snippet=None),
    ]))
    ctx = await c._expand_graph_context(
        mock_session, 42, (coordinator_mod.ONT.fact,))
    assert "adr_props" not in ctx[0]


def test_expansion_cypher_projects_adr_node_props_both_forms():
    """Stubs never execute Cypher, so pin the projection textually: both the
    single-anchor and batched expansion queries must SELECT the ADR node props,
    or the surfacing above silently regresses to snippet-only (decision 909)."""
    import inspect
    src = inspect.getsource(coordinator_mod.MemoryCoordinator._expand_graph_context)
    src_batch = inspect.getsource(
        coordinator_mod.MemoryCoordinator._expand_graph_context_batch)
    for body in (src, src_batch):
        assert "related.confidence AS adr_confidence" in body
        assert "related.alternatives AS adr_alternatives" in body
        assert "related.fact_kind AS adr_fact_kind" in body
        assert "related.source_ref AS adr_source_ref" in body
    # The batch form must also pass the columns through its OUTER return.
    assert "adr_confidence, adr_alternatives, adr_fact_kind, adr_source_ref" in src_batch


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

    ctx = await c._expand_graph_context(
        mock_session, 577, (coordinator_mod.ONT.decision,))

    grounded = ctx[0]
    assert grounded["direction"] == "out"
    assert grounded["properties"]["asserted_by"] == "operator"
    assert grounded["properties"]["confidence"]  == 0.9
    assert grounded["properties"]["role"]        == "based_on"

    machine = ctx[1]
    assert machine["properties"]["asserted_by"] == "rem"
    assert machine["properties"]["confidence"]  == 0.55
    assert machine["properties"]["model"]       == "gemma-4-12b"
    assert machine["properties"]["run_id"]      == "r-77"


# ── (d) Entity neighbors keep the legacy shape ────────────────────────────────

@pytest.mark.asyncio
async def test_entity_neighbor_keeps_legacy_keys_plus_additive_ones():
    """Existing consumers read rel_type/name/label/aliases — those keys must
    survive unchanged; direction/properties are additive."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Entity"], name="Neo4j", rel_type="MENTIONS",
             direction="out", rel_props={}, aliases=["neo4j", "Neo4J"]),
    ]))
    ctx = await c._expand_graph_context(
        mock_session, 42, (coordinator_mod.ONT.fact,))

    entry = ctx[0]
    assert entry["rel_type"] == "MENTIONS"
    assert entry["name"]     == "Neo4j"
    assert entry["label"]    == "Entity"
    assert entry["aliases"]  == ["neo4j", "Neo4J"]
    # additive keys
    assert entry["direction"] == "out"
    assert entry["properties"] == {}
    # pg_id-keyed keys are NOT bolted onto name-keyed neighbors
    assert "snippet" not in entry


@pytest.mark.asyncio
async def test_entity_neighbor_without_aliases_omits_key():
    """Legacy behavior: the aliases key only appears when siblings exist."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_AsyncRows([
        _row(labels=["Entity"], name="Postgres", aliases=[]),
    ]))
    ctx = await c._expand_graph_context(
        mock_session, 42, (coordinator_mod.ONT.fact,))
    assert "aliases" not in ctx[0]


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
    ctx = await c._expand_graph_context(
        mock_session, 577, (coordinator_mod.ONT.decision,))

    dumped = json.dumps(ctx)   # must not raise TypeError
    props = ctx[0]["properties"]
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
    await c._expand_graph_context(mock_session, 42, (coordinator_mod.ONT.fact,))

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
    assert {e["rel_type"] for e in cs["graph_context"]} == {"SUMMARIZED_BY"}
    assert {e["pg_id"] for e in cs["graph_context"]} == {42, 43}
    assert all(e["direction"] == "in" for e in cs["graph_context"])

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
    assert ins["graph_context"][0]["label"] == "Decision"
    assert ins["graph_context"][0]["pg_id"] == 245


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
    ctx = await c._expand_graph_context(
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

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": 0, "relevance_score": 1.0}]
    })
    results = await _run_search(c, mock_reranker)

    assert results[0]["tier"] == "community_summary"
    assert results[0]["graph_context"] == []
    assert results[1]["tier"] == "fact"
    assert results[1]["graph_context"] == []
