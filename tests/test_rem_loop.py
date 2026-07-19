"""
Unit tests for rem_loop.py — REMDaemon and pure helper functions.

All Neo4j and Postgres I/O is mocked; no live infrastructure required.
Set MOCK_LLM=1 (done inline per test) to bypass LLM calls.
"""

import asyncio
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ── Dynamic import (mirrors test_coordinator.py pattern) ─────────────────────

def load_rem_loop():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "rem_loop.py")
    spec = importlib.util.spec_from_file_location("rem_loop", path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["rem_loop"] = mod
    spec.loader.exec_module(mod)
    return mod

rem_mod = load_rem_loop()
REMDaemon        = rem_mod.REMDaemon
_safe_label      = rem_mod._safe_label
_build_entity_registry = rem_mod._build_entity_registry
_resolve_rel     = rem_mod._resolve_rel


# ── Helpers ───────────────────────────────────────────────────────────────────

class _AsyncIter:
    """Minimal async iterable yielding zero rows — empty Neo4j result."""
    def __aiter__(self):  return self
    async def __anext__(self): raise StopAsyncIteration


class _async_ctx:
    """Minimal async context manager wrapping a fixed value."""
    def __init__(self, val):  self._val = val
    async def __aenter__(self): return self._val
    async def __aexit__(self, *_): pass


def _make_daemon():
    """Return a REMDaemon with driver mocked out."""
    d = REMDaemon.__new__(REMDaemon)
    d.is_running = True
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncIter())
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_async_ctx(mock_session))
    d.driver = mock_driver
    return d, mock_session


# ── Pure helper tests ─────────────────────────────────────────────────────────

def test_safe_label_returns_first_known():
    from rem_loop import _KNOWN_LABELS
    # Human is in _KNOWN_LABELS
    assert _safe_label(["Human", "SomethingElse"]) == "Human"


def test_safe_label_falls_back_to_entity_on_unknown():
    from rem_loop import ONT
    assert _safe_label(["UnknownLabel"]) == ONT.entity


def test_safe_label_empty_list_returns_entity():
    from rem_loop import ONT
    assert _safe_label([]) == ONT.entity


def test_build_entity_registry_typed_nodes():
    from rem_loop import ONT
    closed_set = [
        {"name": "Xenofon",           "labels": ["Human"]},
        {"name": "claude-sonnet-4-6", "labels": ["AIAgent"]},
        {"name": "shared-memory",     "labels": ["Project"]},
        {"name": "OutboxPattern",     "labels": ["Entity"]},
    ]
    reg = _build_entity_registry(closed_set)
    assert reg["Xenofon"]["label"]           == "Human"
    assert reg["Xenofon"]["default_rel"]     == ONT.was_attributed_to
    assert reg["claude-sonnet-4-6"]["label"] == "AIAgent"
    assert reg["OutboxPattern"]["label"]     == "Entity"


def test_build_entity_registry_skips_nameless():
    reg = _build_entity_registry([{"name": None, "labels": ["Human"]}])
    assert len(reg) == 0


def test_resolve_rel_known_human_compatible_rel():
    from rem_loop import ONT
    reg = {"Xenofon": {"label": "Human", "default_rel": ONT.was_attributed_to}}
    label, rel = _resolve_rel("Xenofon", ONT.was_attributed_to, reg)
    assert label == "Human"
    assert rel   == ONT.was_attributed_to


def test_resolve_rel_known_human_incompatible_rel_falls_back():
    from rem_loop import ONT
    reg = {"Xenofon": {"label": "Human", "default_rel": ONT.was_attributed_to}}
    # PROJECT_OF is not valid for Human → should fall back to WAS_ATTRIBUTED_TO
    label, rel = _resolve_rel("Xenofon", ONT.project_of, reg)
    assert label == "Human"
    assert rel   == ONT.was_attributed_to


def test_resolve_rel_unknown_name_always_entity_mentions():
    from rem_loop import ONT
    label, rel = _resolve_rel("NewUnknown", "WAS_ATTRIBUTED_TO", {})
    assert label == ONT.entity
    assert rel   == ONT.entity_link


# ── LLM mock mode ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_process_mock_plain_fact():
    """Short fact (<= REM_SUMMARY_THRESHOLD): the summary is prompt-gated OFF —
    the mock (like the prompt) produces no summary at all. Returns (result, model)."""
    daemon, _ = _make_daemon()
    with patch.dict(os.environ, {"MOCK_LLM": "1"}):
        result, model = await daemon._llm_process("some fact content", rem_mod.KIND_FACT, [])
    assert model == "mock"
    assert "summary" not in result          # short record → no summary requested
    assert "relationships" in result
    assert isinstance(result["relationships"], list)
    assert "considered" not in result   # decision-only key


@pytest.mark.asyncio
async def test_llm_process_mock_long_fact_has_summary():
    """A fact over REM_SUMMARY_THRESHOLD gets a summary — the only case one
    is requested since the rebuild."""
    daemon, _ = _make_daemon()
    long_content = "x" * (rem_mod.REM_SUMMARY_THRESHOLD + 1)
    with patch.dict(os.environ, {"MOCK_LLM": "1"}):
        result, _ = await daemon._llm_process(long_content, rem_mod.KIND_FACT, [])
    assert result.get("summary")


@pytest.mark.asyncio
async def test_llm_process_mock_decision_includes_extras():
    daemon, _ = _make_daemon()
    with patch.dict(os.environ, {"MOCK_LLM": "1"}):
        result, _ = await daemon._llm_process("decision content", rem_mod.KIND_DECISION, [])
    assert "considered" in result
    assert "rejected"   in result
    assert "under_conditions" in result
    assert "produces_insight" in result


@pytest.mark.asyncio
async def test_llm_process_mock_retrospective_no_extras():
    """A retrospective anchor is enriched like a fact (delta relationships) —
    the decision-only extras must not appear."""
    daemon, _ = _make_daemon()
    with patch.dict(os.environ, {"MOCK_LLM": "1"}):
        result, _ = await daemon._llm_process("retro notes content", rem_mod.KIND_RETRO, [])
    assert "relationships" in result
    assert "considered" not in result


# ── _fetch_non_rem_batch ordering ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_non_rem_batch_orders_attempts_first_then_pg_id():
    """Queue rotation: fresh records first (attempts ASC), oldest-first within
    the same attempt count; dead-lettered records (>= REM_MAX_ATTEMPTS)
    excluded. Returns (pg_ids, attempts_map) so run_cycle can demote."""
    daemon, mock_session = _make_daemon()
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[
        {"pg_id": 1, "rem_attempts": 0}, {"pg_id": 2, "rem_attempts": 1}])
    mock_session.run = AsyncMock(return_value=mock_result)

    pg_ids, attempts = await daemon._fetch_non_rem_batch()

    assert pg_ids == [1, 2]
    assert attempts == {1: 0, 2: 1}
    fetch_call = mock_session.run.call_args_list[0]
    cypher = fetch_call.args[0]
    assert "ORDER BY coalesce(n.rem_attempts, 0) ASC, n.pg_id ASC" in cypher
    assert "coalesce(n.rem_attempts, 0) < $max_attempts" in cypher
    assert fetch_call.kwargs["max_attempts"] == rem_mod.REM_MAX_ATTEMPTS
    # a second query counts the dead-lettered records for the cycle log
    dead_cypher = mock_session.run.call_args_list[1].args[0]
    assert ">= $max_attempts" in dead_cypher and "count(n)" in dead_cypher


# ── _write_neo4j_rem — rem_processed set last ─────────────────────────────────

def _edge(name, label, rel_type, props=None):
    """Planned-edge helper matching the rebuild's _write_neo4j_rem input."""
    return {"name": name, "label": label, "rel_type": rel_type,
            "props": props or {"asserted_by": "rem", "confidence": 0.9,
                               "model": "m", "run_id": "r"}}


@pytest.mark.asyncio
async def test_write_neo4j_rem_sets_rem_processed_last():
    """rem_processed=true must be SET in the final session.run() call,
    after all entity MERGE calls, so partial failures leave the fact unprocessed."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    edges = [_edge("Xenofon", "Human", ONT.was_attributed_to)]

    await daemon._write_neo4j_rem(42, "", edges, original_content="the original fact")

    calls = mock_session.run.call_args_list
    assert len(calls) >= 2, "Expected at least one entity MERGE + one SET call"
    last_cypher = calls[-1].args[0]
    assert "rem_processed" in last_cypher
    assert "SET" in last_cypher
    # Entity MERGE must appear in an earlier call
    earlier_cyphers = [c.args[0] for c in calls[:-1]]
    assert any("MERGE" in c for c in earlier_cyphers)


@pytest.mark.asyncio
async def test_write_neo4j_rem_stamps_provenance_on_create_only():
    """726 §2: edge provenance is applied via ON CREATE SET — a newly minted
    edge is stamped, an EXISTING edge (e.g. operator grounding) is never
    overwritten. The props must travel as parameters, never bare SET."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    props = {"asserted_by": "rem", "confidence": 0.83, "model": "m1", "run_id": "cycle-1"}
    edges = [_edge("Neo4j", ONT.entity, ONT.entity_link, props)]

    await daemon._write_neo4j_rem(42, "", edges, original_content="orig")

    merge_call = mock_session.run.call_args_list[0]
    cypher = merge_call.args[0]
    assert "ON CREATE SET" in cypher, "provenance must be ON CREATE-only"
    assert "r += row.props" in cypher
    assert "r.created_at = datetime()" in cypher
    # no unconditional SET on the relationship (would downgrade operator edges)
    after_merge_rel = cypher.split("MERGE (a)-[r:")[1]
    assert " SET r" not in after_merge_rel.replace("ON CREATE SET", "")
    assert merge_call.kwargs["rows"] == [{"name": "Neo4j", "props": props}]


@pytest.mark.asyncio
async def test_write_neo4j_rem_refuses_unknown_label_or_rel():
    """Injection guard: an edge whose label or rel_type is outside the known
    sets is dropped, never interpolated into Cypher."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    edges = [_edge("X", "EvilLabel", "MENTIONS"),
             _edge("Y", "Entity", "EVIL_REL")]
    await daemon._write_neo4j_rem(42, "", edges, original_content="orig")

    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    assert not any("EvilLabel" in c or "EVIL_REL" in c for c in cyphers)
    # only the final rem_processed SET should have run
    assert len(cyphers) == 1 and "rem_processed" in cyphers[0]


@pytest.mark.asyncio
async def test_write_neo4j_rem_applies_entity_sublabels():
    """Stage 1.3: LLM-assigned sub-types become a SECOND label on :Entity
    (:Entity:Component), validated against the sub-label set before interpolation
    (Cypher-injection guard); rem_processed still SET last."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    edges = [_edge("coordinator", ONT.entity, ONT.entity_link),
             _edge("Neo4j", ONT.entity, ONT.entity_link)]
    # includes an invalid sub-type that must be rejected, not interpolated
    entity_types = {"coordinator": ONT.component, "Neo4j": ONT.system, "x": "Bogus"}

    await daemon._write_neo4j_rem(7, "", edges, entity_types=entity_types,
                                  original_content="orig")

    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    assert any(f"SET e:{ONT.component}" in c for c in cyphers)
    assert any(f"SET e:{ONT.system}" in c for c in cyphers)
    assert not any("Bogus" in c for c in cyphers), "invalid sub-label must not interpolate"
    assert "rem_processed" in cyphers[-1], "rem_processed must still be set last"


@pytest.mark.asyncio
async def test_fetch_non_rem_batch_selects_all_three_anchor_kinds():
    """Selection must consider :Fact, :Decision AND :Retrospective (retro-as-node
    session) so all three record types get enriched."""
    daemon, mock_session = _make_daemon()
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[{"pg_id": 7}])
    mock_session.run = AsyncMock(return_value=mock_result)

    from rem_loop import ONT
    await daemon._fetch_non_rem_batch()

    cypher = mock_session.run.call_args_list[0].args[0]
    assert ONT.fact in cypher and ONT.decision in cypher
    assert ONT.retrospective in cypher
    assert "OR" in cypher
    assert "ORDER BY" in cypher and "ASC" in cypher


@pytest.mark.asyncio
async def test_write_neo4j_rem_decision_anchors_on_decision_and_keeps_rationale():
    """For a decision: anchor edges + the rem_processed mark on the :Decision
    node, extras edges (CONSIDERED/PRODUCES_INSIGHT — now unified planned
    edges), and never overwrite the rationale (summary goes to d.rem_summary,
    not d.content)."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    edges = [
        _edge("BGE-M3", ONT.entity, ONT.entity_link),
        _edge("OutboxPattern", ONT.entity, ONT.considered),
        _edge("WritePathInsight", ONT.entity, ONT.produces_insight),
    ]

    await daemon._write_neo4j_rem(
        42, "rem summary", edges, kind=rem_mod.KIND_DECISION,
        original_content="the decision rationale",
    )

    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    # Step 1 — entity edges anchored on the Decision node, never on a Fact
    assert any(f"(a:{ONT.decision}" in c and "MERGE (a)-[" in c for c in cyphers)
    assert not any(f"MATCH (f:{ONT.fact}" in c for c in cyphers)
    # Step 2 — decision extras written as ordinary provenance-stamped edges
    assert any(ONT.considered in c for c in cyphers)
    assert any(ONT.produces_insight in c for c in cyphers)
    # Step 3 (last) — mark on Decision; rationale/content untouched
    last = cyphers[-1]
    assert f"MATCH (a:{ONT.decision}" in last
    assert "rem_processed" in last and "rem_summary" in last
    assert "a.content" not in last and "a.rationale" not in last


@pytest.mark.asyncio
async def test_write_neo4j_rem_decision_without_summary_marks_only():
    """A short decision (no summary requested) must still be marked
    rem_processed — without writing any rem_summary."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    await daemon._write_neo4j_rem(42, "", [], kind=rem_mod.KIND_DECISION,
                                  original_content="short rationale")

    last = mock_session.run.call_args_list[-1]
    assert "rem_processed" in last.args[0]
    assert "rem_summary" not in last.args[0]


# ── Non-destructive content policy (retro-as-node session) ────────────────────

@pytest.mark.asyncio
async def test_write_neo4j_rem_short_fact_keeps_verbatim_no_summary():
    """A fact at or under REM_SUMMARY_THRESHOLD keeps its ORIGINAL text as
    f.content and stores NO rem_summary — curated short facts stay verbatim."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    original = "short curated fact text"
    await daemon._write_neo4j_rem(42, "an llm summary", [],
                                  original_content=original)

    last = mock_session.run.call_args_list[-1]
    assert "rem_summary" not in last.args[0]
    assert "f.content = $orig" in last.args[0]
    assert last.kwargs["orig"] == original
    assert "rem_processed" in last.args[0]


@pytest.mark.asyncio
async def test_write_neo4j_rem_long_fact_verbatim_plus_summary():
    """A fact over REM_SUMMARY_THRESHOLD keeps its original text (capped at
    2000) in f.content AND stores the LLM summary in f.rem_summary."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    original = "x" * (rem_mod.REM_SUMMARY_THRESHOLD + 500)
    await daemon._write_neo4j_rem(42, "condensed summary", [],
                                  original_content=original)

    last = mock_session.run.call_args_list[-1]
    assert "f.rem_summary = $summary" in last.args[0]
    assert "f.content = $orig" in last.args[0]
    assert last.kwargs["orig"] == original[:2000]
    assert last.kwargs["summary"] == "condensed summary"


@pytest.mark.asyncio
async def test_write_neo4j_rem_retrospective_anchor_non_destructive():
    """A Retrospective anchor gets entity edges + rem_summary, never a content
    overwrite — the notes are the record."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    edges = [_edge("OutboxPattern", ONT.entity, ONT.entity_link)]
    await daemon._write_neo4j_rem(99, "retro summary", edges,
                                  kind=rem_mod.KIND_RETRO,
                                  original_content="the retro notes")

    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    assert any(f"(a:{ONT.retrospective}" in c and "MERGE (a)-[" in c for c in cyphers)
    last = cyphers[-1]
    assert f"MATCH (a:{ONT.retrospective}" in last
    assert "rem_summary" in last and "rem_processed" in last
    assert ".content =" not in last


# ── _fact_is_consistent full string comparison ────────────────────────────────

@pytest.mark.asyncio
async def test_fact_is_consistent_full_match():
    daemon, mock_session = _make_daemon()
    summary = "This is the REM-generated summary for the fact."
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[{"content": summary}])
    mock_session.run = AsyncMock(return_value=mock_result)

    assert await daemon._fact_is_consistent(42, summary) is True


@pytest.mark.asyncio
async def test_fact_is_consistent_mismatch_returns_false():
    daemon, mock_session = _make_daemon()
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[{"content": "different content stored"}])
    mock_session.run = AsyncMock(return_value=mock_result)

    assert await daemon._fact_is_consistent(42, "expected summary") is False


@pytest.mark.asyncio
async def test_fact_is_consistent_missing_node_returns_false():
    daemon, mock_session = _make_daemon()
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[])
    mock_session.run = AsyncMock(return_value=mock_result)

    assert await daemon._fact_is_consistent(42, "any summary") is False


# ── Supersession in consolidation ────────────────────────────────────────────

def load_consolidation():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    path = os.path.join(scripts_dir, "consolidation_loop.py")
    spec = importlib.util.spec_from_file_location("consolidation_loop", path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["consolidation_loop"] = mod
    spec.loader.exec_module(mod)
    return mod

cons_mod = load_consolidation()
ConsolidationDaemon = cons_mod.ConsolidationDaemon


def test_nrem_cluster_query_requires_rem_processed():
    """The NREM cluster query must filter on rem_processed=true so raw
    (non-REM-processed) facts are never consolidated directly."""
    import inspect
    source = inspect.getsource(ConsolidationDaemon.run_consolidation_cycle)
    assert "rem_processed" in source, (
        "consolidation cluster query must include rem_processed guard"
    )


# ── Coordinator search supersession filter ────────────────────────────────────

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


def test_search_tier3_query_excludes_superseded():
    """handle_search must not surface superseded community summaries."""
    import inspect
    source = inspect.getsource(MemoryCoordinator.handle_search)
    assert "superseded" in source, (
        "Tier 3 search must filter WHERE NOT superseded"
    )


# ── configurable REM temperature (v0.4.4) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_process_uses_configured_temperature(monkeypatch):
    """REM must send REM_TEMPERATURE (default 0.6, Gemma-friendly), not a hardcoded 0.1."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)

    captured = {}
    class _Resp:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"message": {"content": '{"summary":"s","relationships":[]}'}}]}
    async def _fake_post(self, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _Resp()
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    await daemon._llm_process("some content", rem_mod.KIND_FACT, [], {})

    from rem_loop import REM_TEMPERATURE
    assert "temperature" in captured
    assert captured["temperature"] == REM_TEMPERATURE
    assert captured["temperature"] != 0.1   # no longer the Qwen-tuned constant
    assert isinstance(REM_TEMPERATURE, float)


@pytest.mark.asyncio
async def test_mark_outbox_rem_reviewed_excludes_retro_rows():
    """A retrospective shares its target decision's pg_id with a HIGHER row
    id — without the type filter the mark lands on the retro row and the
    decision row stays 'applied' (fact pg_id 269 gotcha; ledger statuses
    must stay honest for the insight triggers)."""
    daemon, _ = _make_daemon()

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_Cur())

    await daemon._mark_outbox_rem_reviewed(42, conn, asyncio.get_running_loop())

    sql, params = executed[0]
    assert "!= 'retrospective'" in sql
    assert "status = 'applied'" in sql
    assert params == (42,)


@pytest.mark.asyncio
async def test_mark_outbox_rem_reviewed_retro_kind_targets_retro_row():
    """For a Retrospective anchor (v2: row carries the retro's OWN pg_id) the
    row to mark IS the retrospective-typed one — the filter inverts."""
    daemon, _ = _make_daemon()

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_Cur())

    await daemon._mark_outbox_rem_reviewed(
        99, conn, asyncio.get_running_loop(), kind=rem_mod.KIND_RETRO)

    sql, params = executed[0]
    assert "= 'retrospective'" in sql and "!= 'retrospective'" not in sql
    assert params == (99,)


def test_adaptive_poll_sleep_backoff():
    """Adaptive cadence: BASE when working, exponential backoff to MAX when idle."""
    rem = load_rem_loop()
    assert rem.adaptive_poll_sleep(0) == rem.BASE_POLL_SEC          # just did work
    assert rem.adaptive_poll_sleep(1) == rem.BASE_POLL_SEC          # first idle = BASE
    assert rem.adaptive_poll_sleep(2) == rem.BASE_POLL_SEC * 2      # doubling
    assert rem.adaptive_poll_sleep(3) == rem.BASE_POLL_SEC * 4
    # deep idle is capped at MAX, never below MIN
    assert rem.adaptive_poll_sleep(50) == rem.MAX_POLL_SEC
    assert rem.adaptive_poll_sleep(99) <= rem.MAX_POLL_SEC
    assert rem.adaptive_poll_sleep(99) >= rem.MIN_POLL_SEC


def test_parse_llm_json_clean():
    rem = load_rem_loop()
    obj = rem._parse_llm_json('{"summary": "ok", "relationships": [{"name": "X", "rel_type": "MENTIONS"}]}')
    assert obj["summary"] == "ok" and len(obj["relationships"]) == 1


def test_parse_llm_json_salvages_missing_comma():
    """Mirrors the real Gemma-4 failure: missing comma between array items."""
    rem = load_rem_loop()
    bad = '{"summary": "ok", "relationships": [{"name": "X", "rel_type": "MENTIONS"} {"name": "Y", "rel_type": "MENTIONS"}]}'
    obj = rem._parse_llm_json(bad)
    assert isinstance(obj, dict) and obj.get("summary") == "ok"
    assert len(obj.get("relationships", [])) == 2


def test_parse_llm_json_salvages_unescaped_newline():
    rem = load_rem_loop()
    bad = '{"summary": "line one\nline two in the same string", "relationships": []}'
    obj = rem._parse_llm_json(bad)
    assert isinstance(obj, dict) and "line one" in obj["summary"]


def test_parse_llm_json_hopeless_returns_none():
    rem = load_rem_loop()
    assert rem._parse_llm_json("not json at all !!!") is None


def test_build_batch_prompt_numbered_and_strict():
    rem = load_rem_loop()
    items = [{"pg_id": 1, "content": "fact one", "manifest": {}},
             {"pg_id": 2, "content": "fact two", "manifest": {}}]
    p = rem.REMDaemon._build_batch_prompt(None, items, [{"labels": ["Entity"], "name": "Neo4j"}])
    assert "[FACT 0]" in p and "[FACT 1]" in p
    assert "[MANIFEST 0]" in p and "[MANIFEST 1]" in p     # per-fact capture manifest
    assert "EXACTLY 2 lines" in p and '"idx"' in p
    assert "Neo4j" in p          # shared grounding included
    # short facts → no summary requested at all
    assert 'Do NOT include a "summary" field for any fact' in p


def test_build_batch_prompt_requests_summary_only_for_long_facts():
    rem = load_rem_loop()
    long = "y" * (rem.REM_SUMMARY_THRESHOLD + 1)
    items = [{"pg_id": 1, "content": "short", "manifest": {}},
             {"pg_id": 2, "content": long, "manifest": {}}]
    p = rem.REMDaemon._build_batch_prompt(None, items, [])
    assert "Facts [1] exceed the storage threshold" in p
    assert "for THOSE lines only" in p


def test_parse_jsonl_batch_maps_by_idx_empty_delta_ok():
    """Alignment is idx-echo only: an empty relationships delta (no summary)
    is a COMPLETE answer for a short fact — the null-summary sentinel is gone."""
    rem = load_rem_loop()
    idx_to_pg = {0: 101, 1: 102, 2: 103}
    raw = (
        '{"idx": 0, "relationships": []}\n'
        '{"idx": 1, "relationships": [{"name":"X","rel_type":"MENTIONS","type":"System"}]}\n'
        '{"idx": 2, "summary": "unsolicited", "relationships": []}\n'
    )
    out = rem.REMDaemon._parse_jsonl_batch(None, raw, idx_to_pg)
    assert set(out.keys()) == {101, 102, 103}
    assert out[101]["relationships"] == [] and len(out[102]["relationships"]) == 1


def test_parse_jsonl_batch_drops_required_summary_missing():
    """A fact over the threshold (idx in require_summary) whose line carries no
    summary is dropped → retried solo next cycle; short facts are unaffected."""
    rem = load_rem_loop()
    idx_to_pg = {0: 101, 1: 102}
    raw = (
        '{"idx": 0, "relationships": []}\n'
        '{"idx": 1, "summary": null, "relationships": []}\n'
    )
    out = rem.REMDaemon._parse_jsonl_batch(None, raw, idx_to_pg, require_summary={1})
    assert set(out.keys()) == {101}


def test_parse_jsonl_batch_salvages_and_isolates_failures():
    rem = load_rem_loop()
    idx_to_pg = {0: 201, 1: 202}
    raw = (
        '{"idx": 0, "summary": "ok" "relationships": []}\n'    # missing comma → json_repair salvages
        'garbage line, not json at all\n'                       # idx 1 missing → retry next cycle
    )
    out = rem.REMDaemon._parse_jsonl_batch(None, raw, idx_to_pg)
    assert 201 in out and 202 not in out           # partial success, isolated


def test_parse_jsonl_batch_rejects_out_of_range_idx():
    rem = load_rem_loop()
    out = rem.REMDaemon._parse_jsonl_batch(None, '{"idx": 9, "summary": "x"}', {0: 1})
    assert out == {}


def test_llm_process_batch_mock(monkeypatch):
    import asyncio
    monkeypatch.setenv("MOCK_LLM", "1")
    rem = load_rem_loop()
    long = "z" * (rem.REM_SUMMARY_THRESHOLD + 1)
    items = [{"pg_id": 1, "content": "a", "manifest": {}},
             {"pg_id": 2, "content": long, "manifest": {}}]
    out, timing, model = asyncio.run(rem.REMDaemon._llm_process_batch(None, items, []))
    assert set(out.keys()) == {1, 2}
    assert "summary" not in out[1]        # short → not requested, not produced
    assert out[2]["summary"]              # long → produced
    assert timing is None          # MOCK_LLM path runs no real call → no timing
    assert model == "mock"


# ── Fix-wave: truncation FAILS the unit (finish_reason='length') ──────────────
# "a bound that processes but gives incomplete saves is worse than no bound at
# all" — every max_tokens ships WITH length-detection; a truncated body is never
# parsed/repaired, and each site's max_tokens is the per-site bound.

def test_finish_reason_reads_choice_level():
    from rem_loop import _finish_reason
    assert _finish_reason({"choices": [{"finish_reason": "length", "message": {"content": ""}}]}) == "length"
    assert _finish_reason({"choices": [{"finish_reason": "stop"}]}) == "stop"
    assert _finish_reason({"choices": [{}]}) is None
    assert _finish_reason({"choices": []}) is None
    assert _finish_reason({}) is None


def test_truncated_true_only_on_length():
    from rem_loop import _truncated
    assert _truncated({"choices": [{"finish_reason": "length"}]}) is True
    assert _truncated({"choices": [{"finish_reason": "stop"}]}) is False
    assert _truncated({"choices": [{}]}) is False
    assert _truncated({}) is False


def _length_resp(content):
    class _Resp:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"finish_reason": "length", "message": {"content": content}}]}
    return _Resp()


def _ok_resp(content):
    class _Resp:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    return _Resp()


@pytest.mark.asyncio
async def test_llm_process_solo_truncated_fails_unit_no_repair(monkeypatch):
    """A length-finish solo response returns None and NEVER reaches
    _parse_llm_json/json_repair — a partial enrichment is not salvaged.
    The bound is widened once first (F4), but a second length-finish still
    fails the unit and classifies the failure as truncation."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    bounds = []
    async def _fake_post(self, url, **kwargs):
        bounds.append(kwargs.get("json", {})["max_tokens"])
        # plausibly-complete JSON body — the length-finish must still reject it
        return _length_resp('{"summary":"partial","relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with patch.object(rem_mod, "_parse_llm_json") as parse:
        result, _model = await daemon._llm_process("content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    assert result is None
    parse.assert_not_called()
    from rem_loop import REM_MAX_TOKENS_SOLO, REM_TRUNCATION_RETRY_FACTOR
    assert bounds == [REM_MAX_TOKENS_SOLO,
                      int(REM_MAX_TOKENS_SOLO * REM_TRUNCATION_RETRY_FACTOR)]
    assert daemon._last_llm_failure == rem_mod.LLM_FAIL_TRUNCATED


@pytest.mark.asyncio
async def test_llm_process_solo_truncation_retry_succeeds_at_wider_bound(monkeypatch):
    """F4: a record that just needs more room succeeds on the widened retry
    instead of marching toward a silent dead-letter."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    bounds = []
    async def _fake_post(self, url, **kwargs):
        bounds.append(kwargs.get("json", {})["max_tokens"])
        if len(bounds) == 1:
            return _length_resp('{"summary":"cut","relationships":[]}')
        return _ok_resp('{"summary":"complete","relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    result, _model = await daemon._llm_process("content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    assert result == {"summary": "complete", "relationships": []}
    assert len(bounds) == 2 and bounds[1] > bounds[0]
    assert daemon._last_llm_failure is None


@pytest.mark.asyncio
async def test_solo_transport_failure_does_not_charge_an_attempt(monkeypatch):
    """F1: an HTTP failure is evidence about the backend, not the record —
    it must never count toward the dead-letter cap."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    async def _fake_post(self, url, **kwargs):
        class R:
            status_code = 503
            text = "no free slot"
            headers = {}
        return R()
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with patch.object(daemon, "_bump_rem_attempts", new=AsyncMock()) as bump:
        ok = await daemon._process_fact(7, "content", rem_mod.KIND_FACT, [], {},
                                        None, asyncio.get_running_loop())

    assert ok is False
    bump.assert_not_awaited()
    assert daemon._last_llm_failure == rem_mod.LLM_FAIL_TRANSPORT


@pytest.mark.asyncio
async def test_batch_transport_failure_charges_no_record(monkeypatch):
    """F1 core: one pool 503 must not demote a whole batch to solo. The call
    returns None (not {}) so run_cycle can tell 'call failed' from 'this line
    was missing', and no record's attempt counter moves."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    async def _fake_post(self, url, **kwargs):
        class R:
            status_code = 503
            text = "no free slot"
            headers = {}
        return R()
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    items = [{"pg_id": i, "content": "c", "manifest": {}} for i in (1, 2, 3)]
    results, timing, _model = await daemon._llm_process_batch(items, [])

    assert results is None, "a failed CALL must be distinguishable from empty results"
    assert timing is None
    assert daemon._last_llm_failure == rem_mod.LLM_FAIL_TRANSPORT


@pytest.mark.asyncio
async def test_llm_process_batch_max_tokens_scales_per_fact_and_summary(monkeypatch):
    """Batch bound = REM_MAX_TOKENS_PER_FACT×facts + REM_MAX_TOKENS_PER_SUMMARY×(facts needing a summary)."""
    from rem_loop import (REM_MAX_TOKENS_PER_FACT, REM_MAX_TOKENS_PER_SUMMARY,
                          REM_SUMMARY_THRESHOLD)
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    items = [{"pg_id": 1, "content": "short", "manifest": {}},
             {"pg_id": 2, "content": "x" * (REM_SUMMARY_THRESHOLD + 50), "manifest": {}}]
    captured = {}
    class _Resp:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content":
                '{"idx":0,"relationships":[]}\n{"idx":1,"relationships":[],"summary":"s"}'}}]}
    async def _fake_post(self, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _Resp()
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    await daemon._llm_process_batch(items, [])
    assert captured["max_tokens"] == REM_MAX_TOKENS_PER_FACT * 2 + REM_MAX_TOKENS_PER_SUMMARY * 1


def test_parse_jsonl_batch_truncated_drops_final_line_and_is_strict_only():
    """truncated=True drops the final (under-the-knife) line AND parses the rest
    strictly — json_repair NEVER runs, so a repairable-only line is skipped."""
    daemon, _ = _make_daemon()
    idx_to_pg = {0: 10, 1: 11, 2: 12}
    raw = ('{"idx":0,"relationships":[]}\n'
           '{"idx":1,"relationships":[],}\n'     # trailing comma: invalid strict, repairable
           '{"idx":2,"relationships":[')          # incomplete final line
    out = daemon._parse_jsonl_batch(raw, idx_to_pg, truncated=True)
    assert 10 in out          # strict-valid line kept
    assert 11 not in out      # repairable-only line NOT salvaged under truncation
    assert 12 not in out      # final line dropped unconditionally


def test_parse_jsonl_batch_untruncated_repairs_lines():
    """Contrast: without truncation, json_repair salvages the same slip."""
    daemon, _ = _make_daemon()
    idx_to_pg = {0: 10, 1: 11}
    raw = ('{"idx":0,"relationships":[]}\n'
           '{"idx":1,"relationships":[],}')      # trailing comma → repaired
    out = daemon._parse_jsonl_batch(raw, idx_to_pg, truncated=False)
    assert 10 in out and 11 in out


@pytest.mark.asyncio
async def test_llm_verify_call_truncated_returns_none_and_bounds_tokens(monkeypatch):
    """A truncated verification is a FAILED call (returns None → k degrades);
    max_tokens = max(FLOOR, PER_VERIFY_EDGE × n_edges)."""
    from rem_loop import REM_MAX_TOKENS_PER_VERIFY_EDGE, REM_VERIFY_MAX_TOKENS_FLOOR
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = {}
    async def _fake_post(self, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _length_resp('{"idx":0,"confirm":true}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    result = await daemon._llm_verify_call("prompt", pg_id=1, n_edges=5)
    assert result is None
    assert captured["max_tokens"] == max(REM_VERIFY_MAX_TOKENS_FLOOR,
                                         REM_MAX_TOKENS_PER_VERIFY_EDGE * 5)


def test_verify_max_tokens_respects_floor():
    """Few edges → the FLOOR (64) wins over the per-edge product (20×1)."""
    from rem_loop import REM_MAX_TOKENS_PER_VERIFY_EDGE, REM_VERIFY_MAX_TOKENS_FLOOR
    assert max(REM_VERIFY_MAX_TOKENS_FLOOR, REM_MAX_TOKENS_PER_VERIFY_EDGE * 1) == REM_VERIFY_MAX_TOKENS_FLOOR


# ── Fix-wave: F5 stranded-row revert ─────────────────────────────────────────
# A post-write failure must revert rem_processed=false (+1 attempt) so the record
# re-enters the queue under the attempt cap instead of stranding at 'applied'.

@pytest.mark.asyncio
async def test_revert_rem_mark_unstrands_row_and_counts_attempt():
    from rem_loop import ONT, KIND_FACT
    daemon, session = _make_daemon()
    await daemon._revert_rem_mark(42, KIND_FACT)
    session.run.assert_awaited_once()
    query = session.run.call_args.args[0]
    assert f"(n:{ONT.fact}" in query
    assert "rem_processed = false" in query
    assert "rem_attempts = coalesce(n.rem_attempts, 0) + 1" in query
    assert session.run.call_args.kwargs["pg_id"] == 42


@pytest.mark.asyncio
async def test_revert_rem_mark_targets_kind_anchor_label():
    from rem_loop import ONT, KIND_DECISION, KIND_RETRO
    daemon, session = _make_daemon()
    await daemon._revert_rem_mark(7, KIND_DECISION)
    assert f"(n:{ONT.decision}" in session.run.call_args.args[0]
    daemon2, session2 = _make_daemon()
    await daemon2._revert_rem_mark(8, KIND_RETRO)
    assert f"(n:{ONT.retrospective}" in session2.run.call_args.args[0]


# ── F2: the REM/NREM slot arbiter ────────────────────────────────────────────

class _ProbeConn:
    """Minimal psycopg2-shaped conn whose advisory-lock probe returns `got`."""
    def __init__(self, got, raises=False):
        self._got, self._raises = got, raises
        self.executed = []

    def cursor(self):
        outer = self

        class _Cur:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def execute(self_inner, sql, params=None):
                if outer._raises:
                    raise RuntimeError("probe boom")
                outer.executed.append(sql)
            def fetchone(self_inner):
                return (outer._got,)
        return _Cur()


def test_nrem_is_queuing_false_when_lock_is_free():
    """Probe acquires the lock → nobody was queuing → REM proceeds, and the
    probe must RELEASE what it took so it does not block NREM itself."""
    conn = _ProbeConn(got=True)
    assert rem_mod._nrem_is_queuing(conn) is False
    assert any("pg_advisory_unlock" in s for s in conn.executed), \
        "the probe must release the lock it acquired"


def test_nrem_is_queuing_true_when_nrem_holds_it():
    """Probe cannot acquire → NREM is queuing for the slot → REM yields."""
    assert rem_mod._nrem_is_queuing(_ProbeConn(got=False)) is True


def test_nrem_is_queuing_fails_open():
    """A probe error must never block enrichment — the arbiter is an
    optimisation, not a correctness gate."""
    assert rem_mod._nrem_is_queuing(_ProbeConn(got=False, raises=True)) is False


def test_rem_and_nrem_priority_lock_keys_match():
    """The arbiter only works if both daemons name the SAME advisory lock."""
    spec = importlib.util.spec_from_file_location(
        "consolidation_loop_for_key",
        os.path.join(os.path.dirname(__file__), "..", "shared-memory",
                     "scripts", "consolidation_loop.py"))
    cl = importlib.util.module_from_spec(spec)
    sys.modules["consolidation_loop_for_key"] = cl
    spec.loader.exec_module(cl)
    assert (cl.NREM_PRIORITY_ADVISORY_LOCK_KEY
            == rem_mod.NREM_PRIORITY_ADVISORY_LOCK_KEY)
    assert cl.NREM_PRIORITY_ADVISORY_LOCK_KEY != rem_mod.BACKUP_ADVISORY_LOCK_KEY
