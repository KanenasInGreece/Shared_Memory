"""
Unit tests for _spine_telemetry — per-record-type first-write completeness.

The defect these cover: `facts` meant "everything that is not a decision", so all
155 retrospectives were counted inside the facts total. Retrospective quality was
unmeasurable, and the facts figure was diluted by records held to different
required fields.

⚠ These stub the SQL — StubConn replays scripted rows and never plans a query. They
prove the assembly around the aggregates, NOT the aggregates. The queries in this
module were run verbatim against the live database before merge; see the PR.

All I/O mocked; no live infrastructure.
"""

import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


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


coord_mod = load_coordinator()


class _async_ctx:
    def __init__(self, val): self._val = val
    async def __aenter__(self): return self._val
    async def __aexit__(self, *_): pass


def _spine(decisions, facts, retros, keys=(), alias_total=0, alias_split=()):
    """Run _spine_telemetry against scripted aggregate rows.

    fetchrow is called in source order: decisions, facts, retrospectives.
    """
    c = coord_mod.MemoryCoordinator()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[decisions, facts, retros])
    conn.fetch = AsyncMock(side_effect=[list(keys), list(alias_split)])
    conn.fetchval = AsyncMock(return_value=alias_total)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    c._pool = pool
    return asyncio.run(c._spine_telemetry())


D = {"n": 243, "grounded": 56, "alts": 198, "conf": 211, "elicited": 43}
F = {"n": 441, "sref": 441, "elicited": 72}
R = {"n": 155, "rating": 155, "target": 155, "grounded": 27, "elicited": 24}


# ── The retrospectives block exists and reports its own required fields ──────

def test_retrospectives_block_is_present():
    out = _spine(D, F, R)
    assert "retrospectives" in out


def test_retrospectives_reports_its_own_required_fields():
    """rating (the outcome state), target_pg_id (the decision judged), and
    grounded_in (what measured the outcome) — a retrospective's spine, not a
    fact's."""
    r = _spine(D, F, R)["retrospectives"]
    assert r["total"] == 155
    assert r["rating_pct"] == 100.0
    assert r["target_pg_id_pct"] == 100.0
    assert r["grounded_in_pct"] == 17.4
    assert r["elicited_pct"] == 15.5


def test_grounded_in_pct_is_the_metric_that_carries_signal():
    """rating/target are set by every write path, so they pin at 100 and read as a
    regression alarm. grounded_in varies — it is the trend worth watching."""
    r = _spine(D, F, R)["retrospectives"]
    assert r["rating_pct"] == r["target_pg_id_pct"] == 100.0
    assert 0 < r["grounded_in_pct"] < 100


# ── The three blocks PARTITION the spine — no record counted twice ───────────

def test_blocks_partition_the_spine_without_double_counting():
    """The property the old surface could not hold: adding a 155-retrospective
    block while `facts` still absorbed them would have totalled 839 records as
    994. Facts must exclude retrospectives for the blocks to be summable."""
    out = _spine(D, F, R)
    total = (out["decisions"]["total"] + out["facts"]["total"]
             + out["retrospectives"]["total"])
    assert total == 243 + 441 + 155 == 839


def test_facts_query_excludes_retrospectives_and_decisions():
    """Pin the predicate itself — this is the line whose omission WAS the defect,
    and a stubbed row can never reveal it."""
    c = coord_mod.MemoryCoordinator()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[D, F, R])
    conn.fetch = AsyncMock(side_effect=[[], []])
    conn.fetchval = AsyncMock(return_value=0)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    c._pool = pool
    asyncio.run(c._spine_telemetry())

    facts_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "NOT IN ('decision', 'retrospective')" in facts_sql
    retro_sql = conn.fetchrow.await_args_list[2].args[0]
    assert "metadata->>'type'='retrospective'" in retro_sql
    assert "NOT superseded" in retro_sql


# ── Projected keys leave the promotion-candidate list ────────────────────────

def test_projected_retro_keys_drop_out_of_emergent_fields():
    """`emergent_unprojected_fields` means "captured but NOT projected". Now that
    rating/target_pg_id are measured, listing them as unmet opportunities would
    advertise the very metric that just landed."""
    keys = [{"k": "rating", "n": 155}, {"k": "target_pg_id", "n": 155},
            {"k": "principal", "n": 450}, {"k": "grounded_roles", "n": 56}]
    out = _spine(D, F, R, keys=keys)
    emergent = {e["key"] for e in out["emergent_unprojected_fields"]}
    assert "rating" not in emergent
    assert "target_pg_id" not in emergent
    assert {"principal", "grounded_roles"} <= emergent


# ── Degenerate inputs ────────────────────────────────────────────────────────

def test_zero_retrospectives_yields_zero_not_division_error():
    out = _spine(D, F, {"n": 0, "rating": 0, "target": 0, "grounded": 0,
                        "elicited": 0})
    r = out["retrospectives"]
    assert r["total"] == 0
    assert r["rating_pct"] == 0.0
    assert r["grounded_in_pct"] == 0.0


def test_existing_blocks_keep_their_shape():
    """The monitor already renders these — adding a block must not rename or drop
    a field it consumes."""
    out = _spine(D, F, R)
    assert set(out["decisions"]) == {
        "total", "grounded_in_pct", "alternatives_pct", "confidence_pct",
        "elicited_pct"}
    assert set(out["facts"]) == {"total", "source_ref_pct", "elicited_pct"}
    assert "emergent_unprojected_fields" in out and "alias" in out
