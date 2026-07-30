"""
Unit tests for REM grounding recall — the semantic prompt slice and the
uncapped accept registry.

Covers the defect these two split apart: one capped closed set used to serve as
BOTH the names shown to the LLM and the names accepted from it, so past the cap
a known entity whose name sorted late was neither offered nor allowed, and every
mention of it was dropped.

All I/O mocked; no live infrastructure.
"""

import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


def load_rem_loop():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "rem_loop.py")
    spec = importlib.util.spec_from_file_location("rem_loop", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rem_loop"] = mod
    spec.loader.exec_module(mod)
    return mod


rem_mod = load_rem_loop()
select_prompt_slice = rem_mod.select_prompt_slice
_build_entity_registry = rem_mod._build_entity_registry
plan_edges = rem_mod.plan_edges
ONT = rem_mod.ONT


class _async_ctx:
    def __init__(self, val): self._val = val
    async def __aenter__(self): return self._val
    async def __aexit__(self, *_): pass


def _rows(*names):
    return [{"name": n, "labels": [ONT.entity], "pg_id": None} for n in names]


def _make_daemon():
    d = rem_mod.REMDaemon.__new__(rem_mod.REMDaemon)
    d.is_running = True
    mock_session = AsyncMock()
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_async_ctx(mock_session))
    d.driver = mock_driver
    return d, mock_session


# ── select_prompt_slice (pure) ────────────────────────────────────────────────

def test_slice_returns_ranked_order_not_alphabetical():
    """The whole point: relevance order, not the alphabet."""
    closed = _rows("alpha", "beta", "gamma")
    rows, mode = select_prompt_slice(closed, ["gamma", "alpha"], k=10,
                                     fallback_limit=99)
    assert mode == "knn"
    assert [r["name"] for r in rows] == ["gamma", "alpha"]


def test_slice_caps_at_k():
    closed = _rows(*[f"e{i}" for i in range(50)])
    rows, mode = select_prompt_slice(closed, [f"e{i}" for i in range(50)], k=5,
                                     fallback_limit=99)
    assert mode == "knn"
    assert len(rows) == 5


def test_slice_ghost_filter_discards_names_with_no_live_node():
    """entity_embeddings is insert-only and outlives its nodes — a ranked name
    the graph no longer has must never be offered, or the link gate drops the
    edge the LLM was invited to propose."""
    closed = _rows("live_one")
    rows, mode = select_prompt_slice(closed, ["ghost_a", "live_one", "ghost_b"],
                                     k=10, fallback_limit=99)
    assert mode == "knn"
    assert [r["name"] for r in rows] == ["live_one"]


def test_slice_deduplicates_repeated_ranked_names():
    closed = _rows("a", "b")
    rows, _ = select_prompt_slice(closed, ["a", "a", "b", "a"], k=10,
                                 fallback_limit=99)
    assert [r["name"] for r in rows] == ["a", "b"]


def test_slice_empty_recall_falls_back_to_alphabetical_not_empty():
    """An empty SHOW set would make the gate drop nearly everything, so a
    recall outage must degrade to the previous behaviour."""
    closed = _rows("a", "b", "c", "d")
    rows, mode = select_prompt_slice(closed, [], k=2, fallback_limit=3)
    assert mode == "fallback"
    assert [r["name"] for r in rows] == ["a", "b", "c"]


def test_slice_all_ghosts_falls_back_rather_than_returning_empty():
    closed = _rows("a", "b")
    rows, mode = select_prompt_slice(closed, ["ghost1", "ghost2"], k=5,
                                    fallback_limit=5)
    assert mode == "fallback"
    assert len(rows) == 2


def test_slice_fallback_respects_fallback_limit_not_k():
    closed = _rows(*[f"e{i:03d}" for i in range(20)])
    rows, mode = select_prompt_slice(closed, [], k=2, fallback_limit=7)
    assert mode == "fallback"
    assert len(rows) == 7


# ── The registry is NOT the prompt slice (the defect regression) ─────────────

def test_accept_set_covers_names_the_prompt_never_showed():
    """A name outside the shown slice is still ACCEPTED. This is what makes a
    small prompt safe: recall loss in the slice costs prompt relevance, never a
    dropped link, because the gate resolves against the full registry."""
    closed = _rows(*[f"e{i:04d}" for i in range(2000)], "zzz_late_sorter")
    registry = _build_entity_registry(closed)

    shown, mode = select_prompt_slice(closed, ["e0001"], k=1, fallback_limit=1500)
    assert [r["name"] for r in shown] == ["e0001"]
    assert "zzz_late_sorter" not in {r["name"] for r in shown}

    plan = plan_edges(
        {"relationships": [{"name": "zzz_late_sorter", "rel_type": ONT.entity_link}]},
        registry, rem_mod.KIND_FACT, {},
    )
    assert plan["mint_dropped"] == []
    assert [e["name"] for e in plan["edges"]] == ["zzz_late_sorter"]


def test_registry_fetch_uses_registry_limit_not_prompt_limit():
    """The accept set must be bounded by its OWN (high) safety valve. Bounding it
    by the prompt-sized ENTITY_SET_LIMIT is exactly the defect."""
    assert rem_mod.ENTITY_REGISTRY_LIMIT > rem_mod.ENTITY_SET_LIMIT
    daemon, session = _make_daemon()
    result = MagicMock()
    result.data = AsyncMock(return_value=[])
    session.run = AsyncMock(return_value=result)

    asyncio.run(daemon._fetch_closed_entity_set())

    _, kwargs = session.run.call_args
    assert kwargs["limit"] == rem_mod.ENTITY_REGISTRY_LIMIT


# ── _grounding_slice (recall orchestration) ──────────────────────────────────

def test_grounding_slice_merges_batch_round_robin():
    """Each batch member contributes its best candidates under the shared k
    budget — concatenation would leave the last record ungrounded."""
    daemon, _ = _make_daemon()
    closed = _rows("a1", "a2", "b1", "b2")
    daemon._embed = AsyncMock(return_value=[0.0] * 8)
    daemon._nearest_entity_names = AsyncMock(
        side_effect=[["a1", "a2"], ["b1", "b2"]])

    rows, mode = asyncio.run(
        daemon._grounding_slice(["text A", "text B"], closed, MagicMock(),
                               asyncio.new_event_loop()))

    assert mode == "knn"
    assert [r["name"] for r in rows] == ["a1", "b1", "a2", "b2"]


def test_grounding_slice_falls_back_when_embedder_unavailable():
    daemon, _ = _make_daemon()
    closed = _rows("a", "b", "c")
    daemon._embed = AsyncMock(return_value=None)
    daemon._nearest_entity_names = AsyncMock()

    rows, mode = asyncio.run(
        daemon._grounding_slice(["text"], closed, MagicMock(),
                               asyncio.new_event_loop()))

    assert mode == "fallback"
    assert len(rows) == 3
    daemon._nearest_entity_names.assert_not_called()


def test_grounding_slice_falls_back_when_knn_returns_nothing():
    daemon, _ = _make_daemon()
    closed = _rows("a", "b")
    daemon._embed = AsyncMock(return_value=[0.0] * 8)
    daemon._nearest_entity_names = AsyncMock(return_value=[])

    _, mode = asyncio.run(
        daemon._grounding_slice(["text"], closed, MagicMock(),
                               asyncio.new_event_loop()))
    assert mode == "fallback"


def test_grounding_slice_under_mock_llm_makes_no_network_call():
    """MOCK_LLM=1 means no model traffic, and the embedder is model traffic over
    the same gateway — a mocked test must not reach the network."""
    daemon, _ = _make_daemon()
    daemon._embed = AsyncMock(side_effect=AssertionError("embedder called under MOCK_LLM"))
    os.environ["MOCK_LLM"] = "1"
    try:
        rows, mode = asyncio.run(
            daemon._grounding_slice(["text"], _rows("a", "b"), MagicMock(),
                                   asyncio.new_event_loop()))
    finally:
        del os.environ["MOCK_LLM"]
    assert mode == "fallback"
    assert len(rows) == 2
    daemon._embed.assert_not_called()


def test_grounding_slice_embeds_batch_concurrently():
    """The embeddings are independent HTTP calls, so a batch issues them together
    rather than paying N sequential round trips."""
    daemon, _ = _make_daemon()
    inflight = {"now": 0, "max": 0}

    async def _slow_embed(_text):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.01)
        inflight["now"] -= 1
        return [0.0] * 8

    daemon._embed = _slow_embed
    daemon._nearest_entity_names = AsyncMock(return_value=["a"])
    asyncio.run(daemon._grounding_slice(["t1", "t2", "t3"], _rows("a"),
                                       MagicMock(), asyncio.new_event_loop()))
    assert inflight["max"] == 3


def test_grounding_slice_survives_one_embed_raising():
    """One record's embedding failing must not lose the others' candidates."""
    daemon, _ = _make_daemon()
    calls = {"n": 0}

    async def _flaky(_text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gateway hiccup")
        return [0.0] * 8

    daemon._embed = _flaky
    daemon._nearest_entity_names = AsyncMock(return_value=["b"])
    rows, mode = asyncio.run(
        daemon._grounding_slice(["t1", "t2"], _rows("a", "b"), MagicMock(),
                               asyncio.new_event_loop()))
    assert mode == "knn"
    assert [r["name"] for r in rows] == ["b"]


def test_grounding_slice_empty_closed_set_is_fallback_not_crash():
    daemon, _ = _make_daemon()
    rows, mode = asyncio.run(
        daemon._grounding_slice(["text"], [], MagicMock(),
                               asyncio.new_event_loop()))
    assert rows == []
    assert mode == "fallback"


def test_nearest_entity_names_returns_empty_on_query_failure():
    """A recall failure degrades to the fallback slice; it never breaks the cycle."""
    daemon, _ = _make_daemon()
    loop = asyncio.new_event_loop()
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("pgvector unavailable")
    names = loop.run_until_complete(
        daemon._nearest_entity_names([0.0] * 8, 5, conn, loop))
    assert names == []
