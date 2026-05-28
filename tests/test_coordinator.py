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

def _make_request(body: dict) -> MagicMock:
    """Minimal aiohttp Request mock with an async .json() method."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
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
