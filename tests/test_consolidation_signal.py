"""Unit tests for the consolidation quality/coverage signal (ADR-018, PR-1).

Liveness safety-net: every consolidation/insight cycle records one
consolidation_runs row AND leaves a corroborating log line, a crash is captured
as state (not just a journal line — the failure class that ran ~12 days
unnoticed), orphaned in-flight rows are reaped on restart, and the stall verdict
is a pure, DB-free rule.

All Postgres I/O is stubbed — no live infrastructure required.
"""
import asyncio
import inspect
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock

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


def test_backlog_is_the_gate_census_only():
    """I7 (decision:1121): backlog = the cycle's OWN recorded gate census
    (eligible_clusters) and NOTHING else. Consolidation is SELECTIVE BY
    DESIGN — a cycle that folds nothing because nothing GATED is a correct
    outcome, not a stall. So absence of a recorded census is NOT evidence of
    backlog; it must read as 0, never as a substitute count from anywhere
    else. (This supersedes the old nrem-fallback contract, which the plan
    ruled must be removed, not preserved.)"""
    # gate found 0 foldable clusters → no backlog.
    assert co._consolidation_backlog(0) == 0
    # gate found work → that is the backlog.
    assert co._consolidation_backlog(3) == 3
    # fresh deploy / no census ever recorded → 0, not "unknown", not a
    # substitute count. This is the I7 line: not-gating is not backlog.
    assert co._consolidation_backlog(None) == 0


def _no_census_row(cycle_type):
    """A consolidation_runs roll-up row for a cycle type that has never
    recorded a gate census — every ``eligible_clusters`` value on its rows was
    NULL (fresh deploy, or every run so far crashed before reaching the gate).
    Column set matches _compute_consolidation_health's SELECT exactly."""
    return {
        "cycle_type": cycle_type, "last_success": None, "last_outcome": None,
        "last_success_age": None, "last_started": None, "cycle_seconds_avg": None,
        "runs_24h": 0, "deferred_24h": 0, "idle_24h": 0,
        "folds_succeeded_24h": 0, "folds_attempted_24h": 0,
        "inflight": 0, "consec_fail": 0, "last_error_class": None,
        "last_error_msg": None, "last_error_at": None, "eligible_clusters": None,
        "eligible_oldest_age": None, "last_deferred_reason": None,
        # D1 (fact:1189) — dead_lettered_clusters, like eligible_clusters,
        # is NULL when no census has ever recorded it.
        "dead_lettered_clusters": None,
        # AR-01 (v0.8.75) — same NULL-until-recorded contract as
        # dead_lettered_clusters for the latest-value pair; the 24h sums
        # follow folds_succeeded_24h/folds_attempted_24h's 0-means-none shape.
        "truncation_failures": None, "slot_failures": None,
        "truncation_failures_24h": 0, "slot_failures_24h": 0,
        # Output-identity skip (operator ruling 2026-08-11) — same
        # NULL-until-recorded contract as dead_lettered_clusters.
        "unchanged_clusters": None,
        # Singleton-component deferral (operator ruling 2026-08-16) — same
        # NULL-until-recorded contract as dead_lettered_clusters.
        "singleton_clusters": None,
    }


@pytest.mark.asyncio
async def test_no_census_is_not_reported_as_a_stall_composition():
    """I7 COMPOSITION test (`decision:1121`) — bites the whole path from the
    consolidation_runs roll-up into the stall verdict, not just the pure
    _consolidation_backlog function (the plan's explicit instruction: a unit
    test on the pure function alone is insufficient, because the original
    defect was in how the census flows from the roll-up into the verdict).

    Scenario: a cycle type has never recorded its own gate census
    (eligible_clusters IS NULL on every consolidation_runs row — e.g. before
    the daemon's first pass through the gate). Per I7, that must NOT be
    reported as a stall, and it must not become one just because a looser
    density count elsewhere happens to be nonzero.

    Mutation-checked: reintroducing the old nrem-fallback (calling
    _nrem_cycle_counts() inside _compute_consolidation_health and using its
    count when eligible_clusters is None) makes this test fail two ways —
    (a) the assertion that _nrem_cycle_counts is never even consulted by this
    composition, and (b) with the fallback restored, a nonzero nrem count
    plus no recorded success flips `stalled` to True. See HANDOFF.md for the
    exact mutation applied and confirmation this test was the one that died."""
    coord = co.MemoryCoordinator()

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_no_census_row("insight")])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)

    # A looser density count DOES exist and IS nonzero. If the composition
    # ever falls back to it, this is exactly what would wrongly manufacture
    # a stall out of "no census recorded yet".
    nrem_spy = AsyncMock(return_value={"decision_cycles": 5, "fact_cycles": 5})
    coord._nrem_cycle_counts = nrem_spy

    out = await coord._compute_consolidation_health()

    assert out["insight"]["eligible_clusters"] is None   # no census recorded
    assert out["insight"]["backlog"] == 0                 # I7: not-gating ≠ backlog
    assert out["insight"]["stalled"] is False              # therefore: not a stall
    nrem_spy.assert_not_called()                           # composition never consults it


def test_eligible_oldest_age_paired_with_same_row_as_census():
    """R1 (review finding, 2026-08-16) — eligible_oldest_age_seconds must be
    read from the SAME row as eligible_clusters, not merely the latest row
    where the age column itself happened to be non-null. Before the fix, a
    cycle whose LATEST census recorded eligible_clusters=0 (no oldest
    eligible cluster exists — that row's own eligible_oldest_age_seconds is
    NULL) still reported the age carried over from an OLDER row whose census
    had not yet dropped to zero, producing the impossible pair "eligible 0
    (oldest 263684s)" — live-proven 2026-08-15 on fact_consolidation
    (eligible_clusters=0, frozen eligible_oldest_age=80067 from a stale row).

    Static check: the roll-up query's eligible_oldest_age FILTER clause must
    use the SAME predicate as eligible_clusters' own FILTER
    (`eligible_clusters IS NOT NULL`) — not `eligible_oldest_age_seconds IS
    NOT NULL`, which lets array_agg's [1] pick a value from a DIFFERENT row
    than eligible_clusters' own [1], silently pairing two unrelated rows.
    This pins the ANCHOR COLUMN, not merely the equality of the two
    predicates: both predicates must equal `eligible_clusters IS NOT NULL`
    specifically, so a mutation that flips BOTH predicates to
    `eligible_oldest_age_seconds IS NOT NULL` in lockstep (still identical
    to each other, still pairing the same row) is caught too — that
    mutation excludes every zero-census row from both arrays, freezing
    eligible_clusters at its last non-zero value (a permanent phantom
    backlog / stall-verdict defect). Mutation-checked: reverting either
    FILTER predicate to `eligible_oldest_age_seconds IS NOT NULL` (alone or
    both together) makes this assertion fail.

    Composition check: feeding _compute_consolidation_health() a row shaped
    exactly as the FIXED query now produces it (eligible_clusters=0 paired
    with THAT row's own eligible_oldest_age=None, never a stale non-null
    value from elsewhere) must surface eligible_oldest_age_seconds as None —
    the stale value must not leak."""
    src = inspect.getsource(co)
    m = re.search(
        r"array_agg\(eligible_clusters ORDER BY started_at DESC\)\s*"
        r"FILTER \(WHERE (?P<clusters_pred>[^)]+)\)\)\[1\] AS eligible_clusters,\s*"
        r".*?"
        r"array_agg\(eligible_oldest_age_seconds ORDER BY started_at DESC\)\s*"
        r"FILTER \(WHERE (?P<age_pred>[^)]+)\)\)\[1\] AS eligible_oldest_age,",
        src, re.DOTALL)
    assert m, "could not locate the eligible_clusters / eligible_oldest_age pair in the roll-up query"
    assert m.group("age_pred").strip() == m.group("clusters_pred").strip(), (
        "eligible_oldest_age must be FILTERed on the SAME predicate as "
        f"eligible_clusters (paired to the same row); got age={m.group('age_pred')!r} "
        f"vs clusters={m.group('clusters_pred')!r}"
    )
    # Pin WHICH column anchors the pair — equality alone would still pass if
    # both predicates were flipped in lockstep to eligible_oldest_age_seconds
    # IS NOT NULL, which silently excludes zero-census rows from both arrays
    # and freezes eligible_clusters at the last non-zero census (the
    # phantom-backlog stall this fix exists to prevent).
    assert m.group("clusters_pred").strip() == "eligible_clusters IS NOT NULL", (
        "the pair must anchor on eligible_clusters IS NOT NULL specifically "
        f"(not merely on matching each other); got clusters={m.group('clusters_pred')!r}"
    )

    coord = co.MemoryCoordinator()
    # Shaped as the FIXED query would now emit it: latest census row has
    # eligible_clusters=0, and THAT SAME row's own eligible_oldest_age is
    # NULL — never a stale non-null value carried over from an older row.
    row = dict(_no_census_row("fact_consolidation"), eligible_clusters=0,
               eligible_oldest_age=None)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)

    out = asyncio.run(coord._compute_consolidation_health())

    assert out["fact_consolidation"]["eligible_clusters"] == 0
    assert out["fact_consolidation"]["eligible_oldest_age_seconds"] is None


@pytest.mark.asyncio
async def test_dead_lettered_clusters_surfaced_per_cycle_type():
    """D1 (fact:1189) — dead_lettered_clusters (written by the daemon into
    consolidation_runs.extra via _CycleRec.extra()) must be read back and
    surfaced per cycle type — a NEW key, distinct from eligible_clusters,
    read from the latest row that actually carries it (`extra ?
    'dead_lettered_clusters'`). A cycle type with no row this pass reads
    None (absence), never 0 — the same "no census recorded" discipline
    eligible_clusters already follows."""
    coord = co.MemoryCoordinator()
    row = dict(_no_census_row("insight"), eligible_clusters=5,
               dead_lettered_clusters=2)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)

    out = await coord._compute_consolidation_health()

    assert out["insight"]["eligible_clusters"] == 5
    assert out["insight"]["dead_lettered_clusters"] == 2
    # fact_consolidation got no row at all this pass — None, not 0.
    assert out["fact_consolidation"]["dead_lettered_clusters"] is None


@pytest.mark.asyncio
async def test_singleton_clusters_surfaced_per_cycle_type():
    """Operator ruling 2026-08-16 — singleton_clusters (written by the daemon
    into consolidation_runs.extra via _CycleRec.extra()) must be read back and
    surfaced per cycle type, same shape/contract as dead_lettered_clusters: a
    NEW key, distinct from eligible_clusters, read from the latest row that
    actually carries it (`extra ? 'singleton_clusters'`). A cycle type with no
    row this pass reads None (absence), never 0."""
    coord = co.MemoryCoordinator()
    row = dict(_no_census_row("insight"), eligible_clusters=1,
               singleton_clusters=2)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)

    out = await coord._compute_consolidation_health()

    assert out["insight"]["eligible_clusters"] == 1
    assert out["insight"]["singleton_clusters"] == 2
    # fact_consolidation got no row at all this pass — None, not 0.
    assert out["fact_consolidation"]["singleton_clusters"] is None


# ── last_error: age + superseded (fact:1609 companion, live 2026-08-26) ──────
#
# GET /memory/telemetry's consolidation.<cycle_type>.last_error used to be the
# most recent outcome='crashed' row in the WHOLE retention window, never
# bounded by later successes — live it showed a v0.8-era OrphanedRun that had
# been superseded by hundreds of completed runs since, and memory_bridge.py's
# `status` rendered it as a bare "err OrphanedRun", indistinguishable from a
# CURRENT failure. consec_fail (crashes since last success) already existed
# on the same query and was correctly 0 in this exact scenario — last_error
# just never checked it.

async def _run_health_with_row(row):
    coord = co.MemoryCoordinator()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)
    return await coord._compute_consolidation_health()


@pytest.mark.asyncio
async def test_last_error_superseded_false_when_crash_is_the_latest_event():
    """No success has landed since the crash (or none ever has) — the crash
    is current, `superseded` must read False, and age_seconds must be
    computed from `last_error_at`."""
    now = datetime.now(timezone.utc)
    crash_at = now - timedelta(seconds=45)
    row = dict(_no_census_row("insight"), last_error_class="OrphanedRun",
               last_error_msg="reaped on restart", last_error_at=crash_at,
               last_success=None)
    out = await _run_health_with_row(row)
    err = out["insight"]["last_error"]
    assert err["class"] == "OrphanedRun"
    assert err["superseded"] is False
    assert 44 <= err["age_seconds"] <= 46


@pytest.mark.asyncio
async def test_last_error_superseded_true_when_a_success_landed_after_it():
    """A completed run landed AFTER the crash — the crash is history.
    `superseded` must read True; age_seconds still reports how old the crash
    itself is (not how old the success is)."""
    now = datetime.now(timezone.utc)
    crash_at = now - timedelta(days=23)
    success_at = now - timedelta(days=1)
    row = dict(_no_census_row("insight"), last_error_class="OrphanedRun",
               last_error_msg="reaped on restart", last_error_at=crash_at,
               last_success=success_at)
    out = await _run_health_with_row(row)
    err = out["insight"]["last_error"]
    assert err["superseded"] is True
    expected_age = int((now - crash_at).total_seconds())
    assert abs(err["age_seconds"] - expected_age) <= 1


@pytest.mark.asyncio
async def test_last_error_is_none_when_no_crash_ever_recorded():
    row = dict(_no_census_row("insight"), last_error_class=None,
               last_error_msg=None, last_error_at=None, last_success=None)
    out = await _run_health_with_row(row)
    assert out["insight"]["last_error"] is None


@pytest.mark.asyncio
async def test_truncation_and_slot_failures_surfaced_per_cycle_type():
    """AR-01 (v0.8.75, six-role milestone audit, Critical) — truncation_failures
    and slot_failures (written by the daemon into consolidation_runs.extra via
    _CycleRec.extra(), same shape as dead_lettered_clusters) must be read back
    and surfaced per cycle type, both as the latest recorded value (None when
    no row this pass carried the key — the same discipline dead_lettered_clusters
    follows) and as a 24h sum (0 when none occurred — the same discipline
    folds_succeeded_24h/folds_attempted_24h follow).

    Before this fix, slot_failed — the PROTOCOL-failure class (a scaffold slot
    still missing after its one bounded retry) — was written to the ledger and
    never read back anywhere: the first live occurrence would have been
    invisible to any monitor. truncation_failures had the identical gap and is
    fixed alongside it rather than shipping a half-fix that leaves an
    analogous blind spot.

    Mutation check: drop either extraction (the array_agg/sum column, or the
    dict key below it) and this test dies."""
    coord = co.MemoryCoordinator()
    row = dict(_no_census_row("insight"), eligible_clusters=5,
               truncation_failures=3, slot_failures=1,
               truncation_failures_24h=7, slot_failures_24h=2)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)

    out = await coord._compute_consolidation_health()

    assert out["insight"]["truncation_failures"] == 3
    assert out["insight"]["slot_failures"] == 1
    assert out["insight"]["truncation_failures_24h"] == 7
    assert out["insight"]["slot_failures_24h"] == 2
    # fact_consolidation got no row at all this pass — None for the
    # latest-value pair (no census recorded it), 0 for the 24h sums (a real
    # window with nothing in it).
    assert out["fact_consolidation"]["truncation_failures"] is None
    assert out["fact_consolidation"]["slot_failures"] is None
    assert out["fact_consolidation"]["truncation_failures_24h"] == 0
    assert out["fact_consolidation"]["slot_failures_24h"] == 0


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


# ── Top-level roll-up across cycle types (pure) ──────────────────────────────
# Regression cover for the reporting defect: the headline keys were mirrored
# from the insight cycle alone, so a fact-consolidation cycle that folded
# minutes ago was reported as "stalled, last success 5.3 days ago" — insight's
# number wearing the whole system's label.

from datetime import datetime, timedelta, timezone


def _ct(age, stalled, outcome="completed", deferred=None):
    return {"last_success_age_seconds": age, "stalled": stalled,
            "last_outcome": outcome, "last_deferred_reason": deferred}


def test_rollup_headline_age_is_the_freshest_success_not_insight():
    """The exact live case: insight 5.3 days stale, fact_consolidation folded
    22 minutes ago. The headline must report the RECENT one and name it."""
    now = datetime.now(timezone.utc)
    by_type = {"insight": _ct(456107, True),
               "fact_consolidation": _ct(1320, False)}
    started = {"insight": now - timedelta(seconds=60),
               "fact_consolidation": now - timedelta(seconds=10)}
    out = co._consolidation_rollup(by_type, True, started)
    assert out["last_success_age_seconds"] == 1320
    assert out["last_success_cycle_type"] == "fact_consolidation"


def test_rollup_names_which_types_are_stalled():
    now = datetime.now(timezone.utc)
    by_type = {"insight": _ct(456107, True),
               "fact_consolidation": _ct(1320, False)}
    started = {"insight": now, "fact_consolidation": now}
    out = co._consolidation_rollup(by_type, True, started)
    # stalled stays an OR — a stalled sibling must still raise the flag …
    assert out["stalled"] is True
    # … but it now says WHO, which is what makes the flag actionable.
    assert out["stalled_types"] == ["insight"]


def test_rollup_outcome_comes_from_the_most_recently_STARTED_type():
    """last_outcome must follow actual recency, not a hardcoded type."""
    now = datetime.now(timezone.utc)
    by_type = {"insight": _ct(10, False, outcome="deferred", deferred="pool_busy"),
               "fact_consolidation": _ct(20, False, outcome="completed")}
    # insight ran most recently → its outcome and reason lead.
    out = co._consolidation_rollup(
        by_type, False,
        {"insight": now, "fact_consolidation": now - timedelta(hours=1)})
    assert (out["last_outcome"], out["last_deferred_reason"]) == ("deferred", "pool_busy")
    assert out["last_active_cycle_type"] == "insight"
    # Flip which one is newer → the other type leads. Guards against the
    # hardcoding this whole change removes.
    out2 = co._consolidation_rollup(
        by_type, False,
        {"insight": now - timedelta(hours=1), "fact_consolidation": now})
    assert (out2["last_outcome"], out2["last_deferred_reason"]) == ("completed", None)
    assert out2["last_active_cycle_type"] == "fact_consolidation"


def test_rollup_ordering_is_by_datetime_not_iso_string():
    """A type whose timestamp carries a different UTC offset must still order
    correctly — ISO-string comparison would get this backwards."""
    now = datetime.now(timezone.utc)
    by_type = {"insight": _ct(10, False, outcome="deferred"),
               "fact_consolidation": _ct(20, False, outcome="completed")}
    # Same instant, but expressed at +02:00 — lexicographically LARGER than the
    # +00:00 string while being 30 minutes EARLIER in real time.
    earlier_but_bigger_string = (now - timedelta(minutes=30)).astimezone(
        timezone(timedelta(hours=2)))
    out = co._consolidation_rollup(
        by_type, False,
        {"insight": earlier_but_bigger_string, "fact_consolidation": now})
    assert out["last_active_cycle_type"] == "fact_consolidation"


def test_rollup_survives_types_that_never_ran():
    """No rows at all: no crash, nothing asserted, no type invented."""
    by_type = {"insight": _ct(None, False, outcome=None),
               "fact_consolidation": _ct(None, False, outcome=None)}
    out = co._consolidation_rollup(by_type, False,
                                   {"insight": None, "fact_consolidation": None})
    assert out["last_success_age_seconds"] is None
    assert out["last_success_cycle_type"] is None
    assert out["last_active_cycle_type"] is None
    assert out["stalled_types"] == []


def test_rollup_ignores_a_type_with_no_success_when_another_has_one():
    now = datetime.now(timezone.utc)
    by_type = {"insight": _ct(None, True),
               "fact_consolidation": _ct(900, False)}
    out = co._consolidation_rollup(by_type, True,
                                   {"insight": now, "fact_consolidation": now})
    assert out["last_success_age_seconds"] == 900
    assert out["last_success_cycle_type"] == "fact_consolidation"
