"""
Tests for coordinator.py — Phase A: decision provenance validation and outbox dispatch.

Coverage:
  - handle_save: decision ingress validation (missing fields → 400)
  - handle_save: plain fact saves unchanged (no regression)
  - handle_save: valid decision save passes validation
  - _apply_outbox_row: dispatches to _apply_decision_outbox_row for type=decision
  - _apply_outbox_row: standard Fact path unchanged for plain facts
  - _apply_decision_outbox_row: writes correct Neo4j nodes and marks outbox applied
"""

import asyncio
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ── Dynamic import (mirrors test_vector_skill.py pattern) ─────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_request(body: dict, authenticated_agent: str | None = None) -> MagicMock:
    """Minimal aiohttp Request mock with an async .json() method.

    authenticated_agent: simulates the value set by auth_middleware after token validation.
    Defaults to None so existing tests run without auth overwrite.
    """
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(return_value=authenticated_agent)
    req.__getitem__ = MagicMock(return_value=authenticated_agent)
    return req


def _coordinator_with_mocks():
    """Return a MemoryCoordinator whose pool and neo4j are mocked out."""
    c = MemoryCoordinator()

    # asyncpg connection mock — transaction() must return an async ctx manager
    mock_conn = AsyncMock()
    mock_conn.fetchrow   = AsyncMock(return_value={"id": 99})
    mock_conn.execute    = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=_async_ctx(None))

    # asyncpg pool mock — acquire() must return an async ctx manager
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_async_ctx(mock_conn))
    c._pool = mock_pool

    # neo4j mock
    mock_session = AsyncMock()
    mock_session.run = AsyncMock()
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_async_ctx(mock_session))
    c._neo4j = mock_neo4j

    return c, mock_conn, mock_session


class _async_ctx:
    """Minimal async context manager wrapping a value."""
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


# ── Ingress validation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decision_save_missing_all_required_fields_returns_400():
    c = MemoryCoordinator()
    req = _make_request({
        "content": "some decision content",
        "metadata": {
            "source": "claude-code",
            "type": "decision",
            "decision": {},          # missing decided_by, project, rationale
        },
    })
    resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["status"] == "error"
    assert "decided_by" in body["message"]
    assert "project"    in body["message"]
    assert "rationale"  in body["message"]


@pytest.mark.asyncio
async def test_decision_save_missing_one_field_names_it_in_error():
    c = MemoryCoordinator()
    req = _make_request({
        "content": "decision without rationale",
        "metadata": {
            "source": "claude-code",
            "type": "decision",
            "decision": {"decided_by": "Xenofon", "project": "shared_memory"},
        },
    })
    resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    # The dynamic missing-fields list should contain only 'rationale'
    assert "['rationale']" in body["message"]


@pytest.mark.asyncio
async def test_plain_fact_save_skips_decision_validation():
    """A save without type=decision must not be blocked by decision validation."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "plain fact content",
            "metadata": {"source": "claude-code", "entities": ["SharedMemory"]},
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["status"] == "success"


@pytest.mark.asyncio
async def test_valid_decision_save_passes_validation():
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "We decided to add a consolidation daemon.",
            "metadata": {
                "source": "claude-code",
                "type": "decision",
                "entities": ["Consolidator", "SharedMemory"],
                "decision": {
                    "title": "Add consolidation daemon",
                    "decided_by": "Xenofon",
                    "project": "shared_memory",
                    "rationale": "simulate dreaming; reduce hot-path latency",
                    "assisted_by": ["claude-code"],
                    "date": "2026-05-20",
                },
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["status"] == "success"
    assert "pg_id" in body


# ── Outbox dispatch ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_outbox_row_dispatches_decision_type():
    """_apply_outbox_row must delegate to _apply_decision_outbox_row for type=decision."""
    c = MemoryCoordinator()
    c._pool  = MagicMock()
    c._neo4j = MagicMock()

    params = {
        "type": "decision",
        "decision": {
            "decided_by": "Xenofon",
            "project": "shared_memory",
            "rationale": "simulate dreaming",
            "assisted_by": ["claude-code"],
        },
        "entities": ["Consolidator"],
        "source": "claude-code",
        "content_snippet": "We decided to add a consolidation daemon.",
    }

    with patch.object(c, "_apply_decision_outbox_row", new=AsyncMock()) as mock_dec:
        await c._apply_outbox_row(outbox_id=1, pg_id=42, params=params, retries=0)
        mock_dec.assert_awaited_once_with(1, 42, params)


@pytest.mark.asyncio
async def test_apply_outbox_row_plain_fact_does_not_call_decision_path():
    """_apply_outbox_row must NOT call _apply_decision_outbox_row for plain facts."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "type": "fact",
        "entities": ["SharedMemory"],
        "source": "claude-code",
        "content_snippet": "plain fact",
    }

    with patch.object(c, "_apply_decision_outbox_row", new=AsyncMock()) as mock_dec:
        await c._apply_outbox_row(outbox_id=2, pg_id=43, params=params, retries=0)
        mock_dec.assert_not_awaited()
        mock_session.run.assert_awaited()   # standard path ran


# ── Decision Neo4j writes ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_decision_outbox_row_writes_correct_nodes():
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "decision": {
            "title": "Add consolidation daemon",
            "decided_by": "Xenofon",
            "project": "shared_memory",
            "rationale": "simulate dreaming",
            "assisted_by": ["claude-code"],
            "date": "2026-05-20",
        },
        "entities": ["Consolidator", "SharedMemory"],
        "source": "claude-code",
        "content_snippet": "We decided to add a consolidation daemon.",
    }

    await c._apply_decision_outbox_row(outbox_id=1, pg_id=42, params=params)

    # Neo4j session.run should have been called with the Decision Cypher
    assert mock_session.run.await_count == 1
    cypher_call = mock_session.run.call_args
    cypher = cypher_call.args[0]
    assert "Decision" in cypher
    assert "Human"    in cypher
    assert "Project"  in cypher
    assert "AIAgent"  in cypher
    assert "WAS_ATTRIBUTED_TO" in cypher
    assert "PROJECT_OF"        in cypher
    assert "WAS_ASSISTED_BY"   in cypher

    # Kwargs should carry all required values
    kwargs = cypher_call.kwargs
    assert kwargs["decided_by"] == "Xenofon"
    assert kwargs["project"]    == "shared_memory"
    assert kwargs["rationale"]  == "simulate dreaming"
    assert kwargs["assisted_by"] == ["claude-code"]
    assert kwargs["entities"]    == ["Consolidator", "SharedMemory"]

    # Outbox row should be marked applied
    mock_conn.execute.assert_awaited()
    execute_sql = mock_conn.execute.call_args.args[0]
    assert "applied" in execute_sql


@pytest.mark.asyncio
async def test_apply_decision_outbox_row_handles_empty_assisted_by():
    """Empty assisted_by must not crash (FOREACH handles empty lists in Cypher)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "decision": {
            "decided_by": "Xenofon",
            "project": "shared_memory",
            "rationale": "test",
            "assisted_by": [],
        },
        "entities": [],
        "source": "Xenofon",
        "content_snippet": "manual decision",
    }

    await c._apply_decision_outbox_row(outbox_id=2, pg_id=50, params=params)
    assert mock_session.run.await_count == 1
    mock_conn.execute.assert_awaited()


# ── Retrospective outbox dispatch and Neo4j writes (Phase C) ─────────────────

@pytest.mark.asyncio
async def test_apply_outbox_row_dispatches_retrospective_type():
    """_apply_outbox_row must delegate to _apply_retrospective_outbox_row for type=retrospective."""
    c = MemoryCoordinator()
    c._pool  = MagicMock()
    c._neo4j = MagicMock()

    params = {
        "type": "retrospective",
        "target_pg_id": 42,
        "retrospective": {"rating": "high", "date": "2026-05-29", "notes": "Held up well."},
        "source": "claude-code",
    }

    with patch.object(c, "_apply_retrospective_outbox_row", new=AsyncMock()) as mock_retro:
        await c._apply_outbox_row(outbox_id=10, pg_id=42, params=params, retries=0)
        mock_retro.assert_awaited_once_with(10, 42, params)


@pytest.mark.asyncio
async def test_apply_retrospective_outbox_row_creates_had_outcome():
    """_apply_retrospective_outbox_row must issue a HAD_OUTCOME CREATE and mark the outbox row applied."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "type": "retrospective",
        "target_pg_id": 42,
        "retrospective": {"rating": "high", "date": "2026-05-29", "notes": "Held up well."},
        "source": "claude-code",
    }

    await c._apply_retrospective_outbox_row(outbox_id=10, pg_id=42, params=params)

    assert mock_session.run.await_count == 1
    cypher_call = mock_session.run.call_args
    cypher = cypher_call.args[0]
    assert "Decision" in cypher
    assert "HAD_OUTCOME" in cypher
    assert "CREATE" in cypher

    kwargs = cypher_call.kwargs
    assert kwargs["pg_id"]  == 42
    assert kwargs["rating"] == "high"
    assert kwargs["notes"]  == "Held up well."

    mock_conn.execute.assert_awaited()
    execute_sql = mock_conn.execute.call_args.args[0]
    assert "applied" in execute_sql


@pytest.mark.asyncio
async def test_handle_retrospective_missing_fields_returns_400():
    """handle_retrospective must return 400 when rating or notes are absent."""
    c, mock_conn, _ = _coordinator_with_mocks()

    req = _make_request({"pg_id": 42, "rating": "", "notes": ""})
    resp = await c.handle_retrospective(req)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["status"] == "error"


# ── Pure function tests — Fix 1: retrieval visibility ─────────────────────────

def test_sigmoid_midpoint():
    assert coordinator_mod._sigmoid(0.0) == pytest.approx(0.5)


def test_sigmoid_large_positive_approaches_one():
    assert coordinator_mod._sigmoid(10.0) > 0.99


def test_sigmoid_large_negative_approaches_zero():
    assert coordinator_mod._sigmoid(-10.0) < 0.01


def test_sigmoid_score_2_is_in_unit_interval():
    v = coordinator_mod._sigmoid(2.0)
    assert 0.0 < v < 1.0


def test_matched_entities_single_match():
    meta = {"entities": ["OutboxPattern", "Neo4j", "Postgres"]}
    result = coordinator_mod._matched_entities("query about Neo4j consolidation", meta)
    assert result == ["Neo4j"]


def test_matched_entities_empty_entity_list():
    assert coordinator_mod._matched_entities("anything", {"entities": []}) == []


def test_matched_entities_none_metadata():
    assert coordinator_mod._matched_entities("anything", None) == []


def test_matched_entities_missing_key():
    assert coordinator_mod._matched_entities("anything", {}) == []


def test_matched_entities_multiple_matches():
    meta = {"entities": ["Neo4j", "Postgres", "BGE-M3"]}
    result = coordinator_mod._matched_entities("Neo4j and Postgres together", meta)
    assert "Neo4j"   in result
    assert "Postgres" in result
    assert "BGE-M3"  not in result


def test_matched_entities_case_insensitive():
    meta = {"entities": ["SharedMemory"]}
    result = coordinator_mod._matched_entities("query about sharedmemory", meta)
    assert result == ["SharedMemory"]


# ── source_ref propagation — Fix 3: lineage ──────────────────────────────────

@pytest.mark.asyncio
async def test_save_propagates_source_ref_to_outbox_cypher_params():
    """source_ref in metadata must appear in the outbox cypher_params JSON."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "Fact with sub-document reference",
            "metadata": {
                "source": "claude-code",
                "entities": ["SharedMemory"],
                "source_ref": "design-doc.pdf#p12",
            },
        })
        resp = await c.handle_save(req)

    assert resp.status == 200

    # Find the outbox INSERT among the execute() calls and verify source_ref is present.
    outbox_call = next(
        (c for c in mock_conn.execute.call_args_list
         if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert outbox_call is not None, "outbox INSERT not found in execute() calls"
    # args: (sql, pg_id, cypher_params) — cypher_params is args[2], bound as a dict
    params = outbox_call.args[2]   # bound as a dict; asyncpg jsonb codec serialises it
    assert params["source_ref"] == "design-doc.pdf#p12"


@pytest.mark.asyncio
async def test_save_without_source_ref_stores_none_in_outbox():
    """Saves without source_ref must not crash — outbox params carry source_ref=None."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "Plain fact with no source reference",
            "metadata": {"source": "claude-code", "entities": ["SharedMemory"]},
        })
        resp = await c.handle_save(req)

    assert resp.status == 200

    outbox_call = next(
        (c for c in mock_conn.execute.call_args_list
         if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert outbox_call is not None
    params = outbox_call.args[2]   # bound as a dict; asyncpg jsonb codec serialises it
    assert params["source_ref"] is None


# ── Search response shape — Fix 1: retrieval visibility ──────────────────────

class _AsyncIter:
    """Minimal async iterable that yields zero items — simulates empty Neo4j result."""
    def __aiter__(self):
        return self
    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_search_response_fact_carries_tier_and_normalized_score():
    """Fact results must include tier='fact', score_normalized in (0,1), matched_entities list."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    # Tier 3: top community summary (now carries metadata + source_pg_ids)
    mock_conn.fetchrow = AsyncMock(return_value={
        "content": "Global context summary",
        "metadata": {"entity": "Neo4j", "domain": "general"},
        "source_pg_ids": [10, 11, 12],
    })
    # Tier 1: one candidate
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact about Neo4j outbox", "metadata": {"entities": ["Neo4j"], "source": "claude-code"}},
    ])
    # Neo4j expansion: no related nodes (empty async iterator)
    mock_session.run = AsyncMock(return_value=_AsyncIter())

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": 0, "relevance_score": 2.0}]
    })

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)

            req = _make_request({"query": "Neo4j outbox", "limit": 5})
            resp = await c.handle_search(req)

    assert resp.status == 200
    body    = json.loads(resp.text)
    results = body["results"]

    # Community summary is prepended
    assert results[0]["tier"] == "community_summary"
    assert results[0]["graph_context"] == []

    # Fact result shape
    fact = results[1]
    assert fact["tier"] == "fact"
    assert isinstance(fact["score_normalized"], float)
    assert 0.0 < fact["score_normalized"] < 1.0
    assert isinstance(fact["matched_entities"], list)
    assert "Neo4j" in fact["matched_entities"]
    assert isinstance(fact["graph_context"], list)


@pytest.mark.asyncio
async def test_search_response_community_summary_has_tier_field():
    """The community summary prepended to results must carry tier='community_summary'."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetchrow = AsyncMock(return_value={
        "content": "A community narrative",
        "metadata": {"entity": "Anything", "domain": "general"},
        "source_pg_ids": [1, 2, 3],
    })
    # Provide one candidate so the early-return path is not taken
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact content", "metadata": {"entities": [], "source": "claude-code"}},
    ])
    mock_session.run = AsyncMock(return_value=_AsyncIter())

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": 0, "relevance_score": 0.0}]
    })

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)

            req = _make_request({"query": "anything", "limit": 5})
            resp = await c.handle_search(req)

    assert resp.status == 200
    results = json.loads(resp.text)["results"]
    assert results[0]["tier"] == "community_summary"


@pytest.mark.asyncio
async def test_search_community_summary_surfaces_traceback_pointers():
    """The Tier-3 community summary result must surface source_pg_ids and metadata
    so agents can trace the narrative back to its source facts (issue d)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetchrow = AsyncMock(return_value={
        "content": "Synthesised narrative about the outbox pattern",
        "metadata": {"entity": "OutboxPattern", "domain": "shared-memory"},
        "source_pg_ids": [42, 43, 44],
    })
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact", "metadata": {"entities": [], "source": "claude-code"}},
    ])
    mock_session.run = AsyncMock(return_value=_AsyncIter())

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={"results": [{"index": 0, "relevance_score": 1.0}]})

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)

            req = _make_request({"query": "outbox pattern", "limit": 5})
            resp = await c.handle_search(req)

    assert resp.status == 200
    cs = json.loads(resp.text)["results"][0]
    assert cs["tier"] == "community_summary"
    # Trace-back pointers are now present (previously dropped — metadata was None)
    assert cs["source_pg_ids"] == [42, 43, 44]
    assert cs["metadata"]["entity"] == "OutboxPattern"
    assert cs["metadata"]["domain"] == "shared-memory"


# ── Auth source overwrite — Phase 2C ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_save_source_overwritten_by_authenticated_agent():
    """When auth is active the coordinator must stamp source with the verified agent name,
    not the value the client supplied."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request(
            {
                "content": "some content",
                "metadata": {"source": "imposter", "entities": ["Entity1"]},
            },
            authenticated_agent="claude",
        )
        resp = await c.handle_save(req)

    assert resp.status == 200

    # The outbox INSERT carries the server-verified source, not "imposter"
    outbox_call = next(
        (c for c in mock_conn.execute.call_args_list
         if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert outbox_call is not None
    params = outbox_call.args[2]   # bound as a dict; asyncpg jsonb codec serialises it
    assert params["source"] == "claude"


# ── JSONB double-encoding regression (v0.4.2) ────────────────────────────────
#
# The asyncpg pool registers a jsonb codec with encoder=json.dumps, so jsonb
# params must be bound as Python objects, never pre-serialised strings. A manual
# json.dumps() here double-encodes the value into a string scalar
# (jsonb_typeof='string'), which makes metadata->>'key' return NULL. These tests
# pin the contract: handle_save / handle_retrospective bind dicts, and a client
# that sends metadata as a JSON string is coerced back to an object.

def _outbox_params(mock_conn):
    call = next(
        (c for c in mock_conn.execute.call_args_list if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert call is not None, "outbox INSERT not found"
    return call.args[2]


@pytest.mark.asyncio
async def test_save_binds_metadata_as_object_not_stringified():
    """Regression: the technical_docs INSERT and the outbox INSERT must bind
    dicts, not JSON strings — otherwise the codec double-encodes them."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "fact for encoding check",
            "metadata": {"source": "claude-code", "entities": ["SharedMemory"]},
        })
        resp = await c.handle_save(req)
    assert resp.status == 200

    # technical_docs INSERT: (sql, content, metadata, embedding, hash, ...)
    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert isinstance(metadata_arg, dict), (
        f"metadata must bind as a dict, got {type(metadata_arg).__name__} "
        "(a str would be double-encoded by the jsonb codec)"
    )
    assert isinstance(_outbox_params(mock_conn), dict)


@pytest.mark.asyncio
async def test_save_coerces_stringified_metadata_to_object():
    """A client that sends metadata as a JSON string must still be stored as a
    queryable object, not a jsonb string scalar."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "fact with stringified metadata",
            "metadata": json.dumps({"source": "grok", "entities": ["X"]}),
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert isinstance(metadata_arg, dict)
    assert metadata_arg["source"] == "grok"
    assert metadata_arg["entities"] == ["X"]


@pytest.mark.asyncio
async def test_retrospective_binds_cypher_params_as_object():
    """Regression: the retrospective outbox payload must bind as a dict."""
    c, mock_conn, _ = _coordinator_with_mocks()
    req = _make_request({"pg_id": 240, "rating": "High", "notes": "held up well"})
    resp = await c.handle_retrospective(req)
    assert resp.status == 200
    params = _outbox_params(mock_conn)
    assert isinstance(params, dict)
    assert params["type"] == "retrospective"
    assert params["retrospective"]["rating"] == "High"


# ── GET /memory/telemetry rollup ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_telemetry_rolls_up_postgres_and_neo4j():
    """handle_telemetry returns a combined Postgres + Neo4j operational snapshot."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"status": "applied", "n": 10}, {"status": "rem_reviewed", "n": 3},
    ])
    mock_conn.fetchval = AsyncMock(return_value=171)
    mock_conn.fetchrow = AsyncMock(return_value={"total": 2, "superseded": 0, "insight": 0})

    def _result(rows):
        r = MagicMock(); r.data = AsyncMock(return_value=rows); return r
    mock_session.run = AsyncMock(side_effect=[
        _result([{"rem": True, "con": True, "n": 96}, {"rem": False, "con": False, "n": 1}]),
        _result([{"rem": True, "n": 4}, {"rem": False, "n": 71}]),
    ])

    resp = await c.handle_telemetry(_make_request({}))
    assert resp.status == 200
    t = json.loads(resp.text)["telemetry"]
    assert t["postgres"]["technical_docs"] == 171
    assert t["postgres"]["outbox"] == {"applied": 10, "rem_reviewed": 3}
    assert t["postgres"]["community_summaries"]["insight"] == 0
    assert t["neo4j"]["facts_total"] == 97
    assert t["neo4j"]["facts_rem_pending"] == 1
    assert t["neo4j"]["facts_unconsolidated"] == 0   # only rem=True & con=False counts; here 96 are consolidated
    assert t["neo4j"]["decisions_total"] == 75
    assert t["neo4j"]["decisions_rem_pending"] == 71


@pytest.mark.asyncio
async def test_handle_telemetry_survives_partial_backend_failure():
    """A Postgres error must not sink the Neo4j section (and vice versa)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(side_effect=Exception("pg down"))

    def _result(rows):
        r = MagicMock(); r.data = AsyncMock(return_value=rows); return r
    mock_session.run = AsyncMock(side_effect=[
        _result([{"rem": True, "con": False, "n": 5}]),
        _result([{"rem": False, "n": 2}]),
    ])

    resp = await c.handle_telemetry(_make_request({}))
    t = json.loads(resp.text)["telemetry"]
    assert "error" in t["postgres"]
    assert t["neo4j"]["facts_total"] == 5
