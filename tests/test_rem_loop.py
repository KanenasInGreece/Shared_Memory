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
    daemon, _ = _make_daemon()
    with patch.dict(os.environ, {"MOCK_LLM": "1"}):
        result = await daemon._llm_process("some fact content", False, [])
    assert "summary" in result
    assert "relationships" in result
    assert isinstance(result["relationships"], list)
    assert "considered" not in result   # decision-only key


@pytest.mark.asyncio
async def test_llm_process_mock_decision_includes_extras():
    daemon, _ = _make_daemon()
    with patch.dict(os.environ, {"MOCK_LLM": "1"}):
        result = await daemon._llm_process("decision content", True, [])
    assert "considered" in result
    assert "rejected"   in result
    assert "under_conditions" in result
    assert "produces_insight" in result


# ── _fetch_non_rem_batch ordering ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_non_rem_batch_orders_by_pg_id_asc():
    """Query must include ORDER BY pg_id ASC to process oldest facts first."""
    daemon, mock_session = _make_daemon()
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[{"pg_id": 1}, {"pg_id": 2}])
    mock_session.run = AsyncMock(return_value=mock_result)

    result = await daemon._fetch_non_rem_batch()

    assert result == [1, 2]
    cypher_call = mock_session.run.call_args.args[0]
    assert "ORDER BY" in cypher_call
    assert "ASC"      in cypher_call


# ── _write_neo4j_rem — rem_processed set last ─────────────────────────────────

@pytest.mark.asyncio
async def test_write_neo4j_rem_sets_rem_processed_last():
    """rem_processed=true must be SET in the final session.run() call,
    after all entity MERGE calls, so partial failures leave the fact unprocessed."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    registry = {"Xenofon": {"label": "Human", "default_rel": ONT.was_attributed_to}}
    relationships = [{"name": "Xenofon", "rel_type": ONT.was_attributed_to}]

    await daemon._write_neo4j_rem(42, "summary text", relationships, registry, None)

    calls = mock_session.run.call_args_list
    assert len(calls) >= 2, "Expected at least one entity MERGE + one SET call"
    last_cypher = calls[-1].args[0]
    assert "rem_processed" in last_cypher
    assert "SET" in last_cypher
    # Entity MERGE must appear in an earlier call
    earlier_cyphers = [c.args[0] for c in calls[:-1]]
    assert any("MERGE" in c for c in earlier_cyphers)


@pytest.mark.asyncio
async def test_write_neo4j_rem_applies_entity_sublabels():
    """Stage 1.3: LLM-assigned sub-types become a SECOND label on :Entity
    (:Entity:Component), validated against the sub-label set before interpolation
    (Cypher-injection guard); rem_processed still SET last."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    relationships = [{"name": "coordinator", "rel_type": ONT.entity_link},
                     {"name": "Neo4j", "rel_type": ONT.entity_link}]
    # includes an invalid sub-type that must be rejected, not interpolated
    entity_types = {"coordinator": ONT.component, "Neo4j": ONT.system, "x": "Bogus"}

    await daemon._write_neo4j_rem(7, "summary", relationships, {}, None,
                                  entity_types=entity_types)

    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    assert any(f"SET e:{ONT.component}" in c for c in cyphers)
    assert any(f"SET e:{ONT.system}" in c for c in cyphers)
    assert not any("Bogus" in c for c in cyphers), "invalid sub-label must not interpolate"
    assert "rem_processed" in cyphers[-1], "rem_processed must still be set last"


@pytest.mark.asyncio
async def test_fetch_non_rem_batch_selects_facts_and_decisions():
    """Selection must consider both :Fact and :Decision so decisions get enriched."""
    daemon, mock_session = _make_daemon()
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[{"pg_id": 7}])
    mock_session.run = AsyncMock(return_value=mock_result)

    from rem_loop import ONT
    await daemon._fetch_non_rem_batch()

    cypher = mock_session.run.call_args.args[0]
    assert ONT.fact in cypher and ONT.decision in cypher
    assert "OR" in cypher
    assert "ORDER BY" in cypher and "ASC" in cypher


@pytest.mark.asyncio
async def test_write_neo4j_rem_decision_anchors_on_decision_and_keeps_rationale():
    """For a decision: anchor edges + the rem_processed mark on the :Decision
    node, write the CONSIDERED/PRODUCES_INSIGHT extras, and never overwrite the
    rationale (summary goes to d.rem_summary, not d.content)."""
    daemon, mock_session = _make_daemon()
    mock_session.run = AsyncMock()

    from rem_loop import ONT
    registry = {"BGE-M3": {"label": ONT.entity, "default_rel": ONT.entity_link}}
    relationships = [{"name": "BGE-M3", "rel_type": ONT.entity_link}]
    decision_extras = {
        ONT.considered:       ["synchronous writes"],
        ONT.rejected:         ["no consolidation"],
        ONT.under_conditions: [],                       # empty → skipped
        ONT.produces_insight: ["outbox decouples write latency"],
    }

    await daemon._write_neo4j_rem(
        42, "rem summary", relationships, registry, decision_extras, is_decision=True,
    )

    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    # Step 1 — entity edges anchored on the Decision node, never on a Fact
    assert any(f"(a:{ONT.decision}" in c and "MERGE (a)-[" in c for c in cyphers)
    assert not any(f"MATCH (f:{ONT.fact}" in c for c in cyphers)
    # Step 2 — decision extras written (non-empty ones only)
    assert any(ONT.considered in c for c in cyphers)
    assert any(ONT.produces_insight in c for c in cyphers)
    # Step 3 (last) — mark on Decision; rationale/content untouched
    last = cyphers[-1]
    assert f"MATCH (d:{ONT.decision}" in last
    assert "rem_processed" in last and "rem_summary" in last
    assert "d.content" not in last and "d.rationale" not in last


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
        def json(self):
            return {"choices": [{"message": {"content": '{"summary":"s","relationships":[]}'}}]}
    async def _fake_post(self, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _Resp()
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    await daemon._llm_process("some content", False, [])

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
