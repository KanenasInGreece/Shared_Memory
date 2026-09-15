"""
Tests for read-path visibility enforcement (v0.6.2).

The `visibility` column (global | scope | private) is stamped on save but was
never read at retrieval — a private/scoped row was returned to any caller.
handle_search now composes _visibility_filter() into every read, on both
Tier-1 (technical_docs) and Tier-3 (community_summaries), so:

  - global  → visible to everyone, including anonymous callers;
  - private → only to the owning agent_id (the server-verified viewer);
  - scope   → only when the viewer asserts the matching scope;
  - anonymous (no verified identity) → 'global' only (fail closed).

Coverage:
  - _visibility_filter: the three viewer cases produce the right SQL + params
  - handle_search: authenticated read binds the predicate + viewer to every read
  - handle_search: anonymous read collapses to visibility='global' (no owner param)
  - handle_search: Tier-3 community-summary reads are gated too (no leak-through)
  - handle_search: keyword fallback (embeddings down) is gated too
"""

import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Dynamic import (mirrors test_coordinator.py pattern) ──────────────────────

def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator
_visibility_filter = coordinator_mod._visibility_filter


# ── Helpers ───────────────────────────────────────────────────────────────────

class _async_ctx:
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


def _make_request(body: dict, authenticated_agent: str | None = None) -> MagicMock:
    state = {"authenticated_agent": authenticated_agent, "principal": None}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator_with_mocks():
    """MemoryCoordinator whose reads return nothing — search short-circuits on
    empty candidates *after* issuing every read, so we can assert on the SQL
    without mocking the reranker or Neo4j."""
    c = MemoryCoordinator()
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])        # Tier-1 candidates → empty
    mock_conn.fetchrow = AsyncMock(return_value=None)   # Tier-3 insight/summary → none
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_async_ctx(mock_conn))
    c._pool = mock_pool
    c._neo4j = MagicMock()
    return c, mock_conn


def _all_read_sql(mock_conn) -> list[str]:
    calls = list(mock_conn.fetch.call_args_list) + list(mock_conn.fetchrow.call_args_list)
    return [ca.args[0] for ca in calls if ca.args]


def _all_bound_params(mock_conn) -> list:
    params: list = []
    for ca in list(mock_conn.fetch.call_args_list) + list(mock_conn.fetchrow.call_args_list):
        params.extend(ca.args[1:])
    return params


# ── _visibility_filter unit tests ─────────────────────────────────────────────

def test_visibility_filter_anonymous_is_global_only():
    sql, params = _visibility_filter(None, None, 2)
    assert sql == "visibility = 'global'"
    assert params == []


def test_visibility_filter_authenticated_no_scope():
    sql, params = _visibility_filter("claude_code", None, 2)
    assert "visibility = 'global'" in sql
    assert "visibility = 'private' AND agent_id = $2" in sql
    assert "'scope'" not in sql          # no asserted scope → no scope disjunct
    assert params == ["claude_code"]


def test_visibility_filter_authenticated_with_scope():
    sql, params = _visibility_filter("claude_code", "proj_x", 2)
    assert "visibility = 'private' AND agent_id = $2" in sql
    assert "visibility = 'scope' AND scope = $3" in sql
    assert params == ["claude_code", "proj_x"]


def test_visibility_filter_respects_start_index():
    sql, params = _visibility_filter("grok", "s", 5)
    assert "agent_id = $5" in sql
    assert "scope = $6" in sql


# ── handle_search enforcement ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_authenticated_binds_predicate_to_every_read():
    c, mock_conn = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({"query": "outbox pattern"}, authenticated_agent="claude_code")
        resp = await c.handle_search(req)

    assert resp.status == 200
    reads = _all_read_sql(mock_conn)
    # Tier-3 insight, Tier-3 summary, Tier-1 candidates — all three issued.
    assert len(reads) >= 3
    assert all("visibility" in sql for sql in reads), "every read must gate on visibility"
    # The private-owner branch must reference the viewer's agent_id, and the
    # verified viewer value must be bound.
    assert all("agent_id =" in sql for sql in reads)
    assert "claude_code" in _all_bound_params(mock_conn)


@pytest.mark.asyncio
async def test_search_anonymous_sees_only_global():
    c, mock_conn = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({"query": "outbox pattern"}, authenticated_agent=None)
        resp = await c.handle_search(req)

    assert resp.status == 200
    reads = _all_read_sql(mock_conn)
    assert reads, "reads must still be issued"
    for sql in reads:
        assert "visibility = 'global'" in sql
        assert "agent_id =" not in sql   # anonymous never unlocks private rows


@pytest.mark.asyncio
async def test_search_tier3_reads_are_gated():
    """Regression guard: a private fact is filtered from Tier-1, so its
    synthesized community summary must not leak through Tier-3."""
    c, mock_conn = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({"query": "anything"}, authenticated_agent="grok")
        await c.handle_search(req)

    tier3_sql = [sql for sql in (ca.args[0] for ca in mock_conn.fetchrow.call_args_list)
                 if "community_summaries" in sql]
    assert len(tier3_sql) >= 2                      # insight + thematic
    assert all("visibility" in sql for sql in tier3_sql)


@pytest.mark.asyncio
async def test_keyword_fallback_is_gated():
    """When the embedder is down, the keyword fallback path must also enforce
    visibility rather than returning every ILIKE match."""
    c, mock_conn = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(side_effect=RuntimeError("embedder down"))):
        req = _make_request({"query": "proxy"}, authenticated_agent="claude_code")
        resp = await c.handle_search(req)

    body = json.loads(resp.text)
    assert body.get("fallback") == "keyword"
    reads = _all_read_sql(mock_conn)
    assert reads and all("visibility" in sql for sql in reads)
    assert "claude_code" in _all_bound_params(mock_conn)


# ── S4: handle_save visibility validation ─────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_save_visibility_typo_refused():
    """S4 Prove-It (RED on 06b892b): save with visibility: 'typo' -> 400."""
    c = MemoryCoordinator()
    req = _make_request({
        "content": "some fact content",
        "metadata": {"source": "claude_code", "project": "test_project", "entities": ["TestEntity"]},
        "visibility": "typo",
    })
    resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["status"] == "error"
    assert "visibility" in body["message"]


@pytest.mark.asyncio
async def test_handle_save_visibility_scope_missing_or_non_string_refused():
    """S4: if visibility=scope, scope must be present and a string."""
    c = MemoryCoordinator()

    # Missing scope
    req1 = _make_request({
        "content": "some fact content",
        "metadata": {"source": "claude_code", "project": "test_project", "entities": ["TestEntity"]},
        "visibility": "scope",
    })
    resp1 = await c.handle_save(req1)
    assert resp1.status == 400
    body1 = json.loads(resp1.text)
    assert "scope" in body1["message"]

    # Non-string scope (int)
    req2 = _make_request({
        "content": "some fact content",
        "metadata": {"source": "claude_code", "project": "test_project", "entities": ["TestEntity"]},
        "visibility": "scope",
        "scope": 123,
    })
    resp2 = await c.handle_save(req2)
    assert resp2.status == 400
    body2 = json.loads(resp2.text)
    assert "scope" in body2["message"]

    # Non-string scope (None)
    req3 = _make_request({
        "content": "some fact content",
        "metadata": {"source": "claude_code", "project": "test_project", "entities": ["TestEntity"]},
        "visibility": "scope",
        "scope": None,
    })
    resp3 = await c.handle_save(req3)
    assert resp3.status == 400
    body3 = json.loads(resp3.text)
    assert "scope" in body3["message"]


@pytest.mark.asyncio
async def test_handle_save_visibility_valid_options_accepted():
    """S4: valid visibility values ('global', 'private', 'scope') pass validation."""
    c, mock_conn = _coordinator_with_mocks()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.fetchrow = AsyncMock(return_value={"id": 100})
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=False)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        for vis, sc in [("global", "global"), ("private", "global"), ("scope", "my-scope")]:
            req_data = {
                "content": f"fact with {vis}",
                "metadata": {"source": "claude_code", "project": "general_discussion", "entities": []},
                "visibility": vis,
            }


            if vis == "scope":
                req_data["scope"] = sc
            req = _make_request(req_data, authenticated_agent="claude_code")
            resp = await c.handle_save(req)
            assert resp.status == 200, f"Expected 200 for {vis}, got {resp.status}"
            body = json.loads(resp.text)
            assert body["status"] == "success"



