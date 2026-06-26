"""Unit tests for the consolidation quality/coverage signal (ADR-018, PR-1).

Liveness safety-net: every consolidation/insight cycle records one
consolidation_runs row AND leaves a corroborating log line, a crash is captured
as state (not just a journal line — the failure class that ran ~12 days
unnoticed), orphaned in-flight rows are reaped on restart, and the stall verdict
is a pure, DB-free rule.

All Postgres I/O is stubbed — no live infrastructure required.
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import consolidation_loop as cl
from consolidation_loop import ConsolidationDaemon, _CycleRec
import coordinator as co


# ── _CycleRec fold tally (pure) ──────────────────────────────────────────────

def test_cyclerec_fold_counts():
    r = _CycleRec()
    r.fold(True); r.fold(False); r.fold(True)
    assert (r.attempted, r.succeeded, r.failed) == (3, 2, 1)


def test_cyclerec_add_derives_failed():
    r = _CycleRec()
    r.add(5, 3)
    assert (r.attempted, r.succeeded, r.failed) == (5, 3, 2)


# ── Stall verdict (pure, DB-free) ────────────────────────────────────────────

def test_stall_verdict():
    T = 9000
    # eligible backlog, never succeeded, nothing in-flight → stalled (the bug)
    assert co._consolidation_stall_verdict(None, False, True, T) is True
    # backlog + stale success → stalled
    assert co._consolidation_stall_verdict(T + 1, False, True, T) is True
    # backlog + recent success → healthy
    assert co._consolidation_stall_verdict(100, False, True, T) is False
    # in-flight fold (slow LLM) → never stalled
    assert co._consolidation_stall_verdict(None, True, True, T) is False
    # no eligible backlog (idle, caught up) → never stalled
    assert co._consolidation_stall_verdict(None, False, False, T) is False


def test_backlog_prefers_gate_census_over_nrem():
    """Regression: the insight stall backlog must come from the cycle's own gate
    census (eligible_clusters), not the looser nrem density count — else a dense
    cluster the strict insight gate rejects falsely reads as a stall (caught live
    on first deploy)."""
    # gate found 0 foldable clusters even though nrem density count is 1 → no backlog
    assert co._consolidation_backlog(0, 1) == 0
    # gate found work → that is the backlog
    assert co._consolidation_backlog(3, 1) == 3
    # fresh deploy, no census recorded yet → fall back to nrem (never blind)
    assert co._consolidation_backlog(None, 2) == 2


# ── _record_cycle context manager ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_cycle_completed(monkeypatch):
    finished = {}
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finished.update(args=a, kwargs=k))
    daemon = ConsolidationDaemon()

    async with daemon._record_cycle("insight") as rec:
        rec.fold(True); rec.fold(True)

    # _crun_finish(run_id, outcome, attempted, succeeded, failed)
    assert finished["args"][0] == 42
    assert finished["args"][1] == "completed"
    assert finished["args"][2:5] == (2, 2, 0)


@pytest.mark.asyncio
async def test_record_cycle_crash_is_recorded_and_reraised(monkeypatch):
    """Regression guard for the projects= kwarg class: a raised exception inside
    a cycle must be captured as a 'crashed' row AND propagate to the caller's
    existing handler. (An isolated _fold_insight test could not catch the
    original call-site crash; this exercises the wrapper boundary.)"""
    finished = {}
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 7)
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finished.update(args=a))
    daemon = ConsolidationDaemon()

    with pytest.raises(TypeError):
        async with daemon._record_cycle("insight") as rec:
            rec.fold(False)
            raise TypeError("got an unexpected keyword argument 'projects'")

    assert finished["args"][1] == "crashed"
    assert finished["args"][5] == "TypeError"          # error_class
    assert "projects" in finished["args"][6]           # error_msg


@pytest.mark.asyncio
async def test_record_cycle_survives_ledger_write_failure(monkeypatch):
    """If consolidation_runs is unreachable the cycle still runs — observability
    must never break consolidation. _crun_start returns None (its failsafe), and
    the real _crun_finish no-ops on a None run_id, so the body completes."""
    monkeypatch.setattr(cl, "_crun_start", lambda ct: None)  # simulate DB down
    daemon = ConsolidationDaemon()
    async with daemon._record_cycle("insight") as rec:
        rec.fold(True)
    assert rec.succeeded == 1


# ── Startup orphan recovery + prune (stubbed psycopg2) ───────────────────────

class _StubCursor:
    def __init__(self, ret_rows):
        self.executed = []
        self._ret_rows = ret_rows
        self._last = []
    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        # The orphan UPDATE ... RETURNING id is the only fetched statement.
        self._last = self._ret_rows if "RETURNING id" in sql else []
    def fetchall(self):
        return self._last
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _StubConn:
    def __init__(self, ret_rows):
        self.cur = _StubCursor(ret_rows)
        self.committed = False
        self.closed = False
    def cursor(self):
        return self.cur
    def commit(self):
        self.committed = True
    def close(self):
        self.closed = True


# ── Coverage census: K-th-oldest anchor (PR-2, pure) ─────────────────────────

def test_kth_oldest_age_uses_eligibility_onset():
    """The K-th-oldest member dates eligibility onset, not the oldest member —
    an old solo decision that only just crossed the threshold should not read as
    ancient. With K=2: cluster's age is anchored on the 2nd-oldest member."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # One cluster: member A very old, member B (the one that tipped K=2) recent.
    ts_map = {
        1: now - timedelta(days=30),   # old solo decision
        2: now - timedelta(hours=1),   # the K-th (2nd) member → eligibility onset
    }
    age = cl._kth_oldest_age_seconds([[1, 2]], ts_map, k=2)
    assert 3000 < age < 7200            # ~1h, NOT ~30d (min(member) would say 30d)


def test_kth_oldest_age_null_safe_and_max_across_clusters():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    ts_map = {
        10: now - timedelta(hours=2),
        11: now - timedelta(hours=5),   # older cluster member
        # pg 99 has no outbox row (pre-migration) → NULL, must not crash
    }
    # cluster A: [10] (one member, <k) degrades to its oldest available (2h)
    # cluster B: [11, 99] → only 11 timestamped → 5h
    age = cl._kth_oldest_age_seconds([[10], [11, 99]], ts_map, k=2)
    assert 17000 < age < 19000          # ~5h, the most-neglected cluster wins
    # no timestamps at all → None (NULL-safe)
    assert cl._kth_oldest_age_seconds([[99]], ts_map, k=2) is None


def test_orphan_recovery_marks_crashed_and_prunes(monkeypatch):
    conn = _StubConn(ret_rows=[(11,), (12,)])
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)

    cl._crun_recover_and_prune()

    sqls = " | ".join(s for s, _ in conn.cur.executed)
    assert "UPDATE consolidation_runs" in sqls and "outcome='crashed'" in sqls
    assert "finished_at IS NULL" in sqls          # only reap in-flight rows
    assert "DELETE FROM consolidation_runs" in sqls  # retention prune
    assert conn.committed and conn.closed


# ── inference_busy in the cached /health snapshot (nvtop surfacing) ───────────
# The refresher probes the GPU in the background so /health reads a cached value
# and never shells out to nvtop. The "never a false idle" guarantee is enforced
# here: the default is "unknown", a probe says "busy"/"idle", and a refresh
# failure keeps the prior value rather than inventing "idle".


def _stop_after_one_iteration(monkeypatch):
    """Make the refresher's trailing sleep raise CancelledError so a single
    iteration runs, then the loop exits (matches its `except CancelledError`)."""
    async def _sleep(*_a, **_k):
        raise asyncio.CancelledError
    monkeypatch.setattr(co.asyncio, "sleep", _sleep)


def test_cached_snapshot_defaults_inference_busy_unknown():
    c = co.MemoryCoordinator()
    # Before any probe, the surface must read "unknown" — never a false "idle".
    assert c.consolidation_health()["inference_busy"] == "unknown"


@pytest.mark.asyncio
async def test_refresher_stores_inference_busy(monkeypatch):
    c = co.MemoryCoordinator()

    async def _compute():
        return {"stalled": False, "last_outcome": "completed",
                "last_success_age_seconds": 5}

    async def _state():
        return "busy"

    monkeypatch.setattr(c, "_compute_consolidation_health", _compute)
    monkeypatch.setattr(co, "inference_busy_state", _state)
    _stop_after_one_iteration(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await c._consolidation_health_refresher()

    snap = c.consolidation_health()
    assert snap["inference_busy"] == "busy"
    assert snap["fresh"] is True


@pytest.mark.asyncio
async def test_refresh_failure_keeps_inference_busy_not_idle(monkeypatch):
    c = co.MemoryCoordinator()
    c._consolidation_health = {**c._consolidation_health, "inference_busy": "busy"}

    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(c, "_compute_consolidation_health", _boom)
    _stop_after_one_iteration(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await c._consolidation_health_refresher()

    snap = c.consolidation_health()
    # A compute failure must NOT flip the busy signal to "idle"; keep prior + stale.
    assert snap["inference_busy"] == "busy"
    assert snap["fresh"] is False
