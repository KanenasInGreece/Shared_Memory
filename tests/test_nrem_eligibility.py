"""NREM due-ness reads the DURABLE ledger predicate, not the save-notification set.

The defect these cover: consolidation fired on `pending_pg_ids` — the in-memory
set fed by the `new_artifact` NOTIFY — which answers "was a record written?",
while the cycle's work needs "is a record ENRICHED into a dense cluster?". Every
save was therefore a claim of eligibility the daemon could not honour: it took
the exclusive LLM slot and then discovered it had nothing to do. Three further
symptoms of the same cause are covered here too — the idle clock that could not
see REM, the cycle that discarded its own entry points on the no-cluster path,
and the idle cycle that could not say it was idle (and so was reported stalled).

No DB, no Neo4j, no LLM.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import consolidation_loop as cl
from consolidation_loop import consolidation_due, ConsolidationDaemon

NOW = datetime(2026, 7, 20, 12, 0, 0)
T = 5           # density threshold used throughout
IDLE = 900
DEFER = 2700


def _due(seconds_since_activity, seconds_eligible, backlog_size):
    return consolidation_due(seconds_since_activity, seconds_eligible, backlog_size,
                             density_threshold=T, idle_threshold=IDLE,
                             max_deferral=DEFER)


# ── the pure gate ────────────────────────────────────────────────────────────

def test_thin_backlog_is_never_due_however_long_the_system_has_been_quiet():
    """THE defect. A long-idle system with saves but no enriched cluster used to
    fire; now the durable count decides, and 4 < 5 can form no cluster at all."""
    assert _due(seconds_since_activity=10 * IDLE, seconds_eligible=10 * DEFER,
                backlog_size=T - 1) == (False, False)


def test_empty_backlog_is_never_due():
    assert _due(10 * IDLE, 10 * DEFER, 0) == (False, False)


def test_due_unforced_once_eligible_and_idle():
    assert _due(IDLE, 1, T) == (True, False)


def test_not_due_while_the_system_is_still_busy():
    assert _due(IDLE - 1, 1, T) == (False, False)


def test_backstop_forces_on_ELIGIBILITY_age_not_on_notification_age():
    """The idle clock can now be held open indefinitely by REM, so the backstop
    must anchor on how long real work has been waiting."""
    assert _due(0, DEFER, T) == (True, True)
    assert _due(0, DEFER - 1, T) == (False, False)


def test_backstop_never_arms_while_ineligible():
    """seconds_eligible is None exactly while the backlog is below threshold —
    an un-eligible backlog must not age its way into a forced cycle."""
    assert _due(0, None, T) == (False, False)


def test_backstop_is_still_gated_by_the_durable_count():
    assert _due(10 * IDLE, 10 * DEFER, T - 1) == (False, False)


# ── the durable probe ────────────────────────────────────────────────────────

def _daemon(monkeypatch, backlogs):
    """Daemon whose ledger read returns the next list from `backlogs` (a value
    may be an Exception to simulate an unreadable ledger). Returns (daemon, calls)."""
    d = ConsolidationDaemon()
    calls = []

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Conn())

    def _fetch(conn):
        calls.append(1)
        nxt = backlogs.pop(0) if backlogs else []
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    # C3 widened the read `_refresh_backlog` actually calls to
    # fetch_combined_fact_backlog (outbox UNION lineage-invalidation ledger,
    # deduped) — patch that entry point rather than the outbox-only half, so
    # these tests keep exercising the RECHECK_SEC cache / eligibility clock /
    # failure handling unchanged by which backlog SOURCE feeds them.
    monkeypatch.setattr(cl, "fetch_combined_fact_backlog", _fetch)
    return d, calls


@pytest.mark.asyncio
async def test_probe_is_rate_limited_and_force_overrides(monkeypatch):
    d, calls = _daemon(monkeypatch, [[1, 2, 3], [4, 5, 6, 7], [8]])
    monkeypatch.setattr(cl, "NREM_ELIGIBILITY_RECHECK_SEC", 60)

    assert await d._refresh_backlog(NOW) == [1, 2, 3]
    # inside the window: the cached observation, no second read
    assert await d._refresh_backlog(NOW + timedelta(seconds=59)) == [1, 2, 3]
    assert len(calls) == 1
    # window elapsed
    assert await d._refresh_backlog(NOW + timedelta(seconds=60)) == [4, 5, 6, 7]
    assert len(calls) == 2
    # force ignores the window
    assert await d._refresh_backlog(NOW + timedelta(seconds=61), force=True) == [8]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_eligibility_clock_starts_when_the_backlog_crosses_the_threshold(monkeypatch):
    d, _ = _daemon(monkeypatch, [list(range(cl.DENSITY_THRESHOLD - 1)),
                                 list(range(cl.DENSITY_THRESHOLD))])
    monkeypatch.setattr(cl, "NREM_ELIGIBILITY_RECHECK_SEC", 0)

    await d._refresh_backlog(NOW)
    assert d._backlog_eligible_since is None

    later = NOW + timedelta(seconds=30)
    await d._refresh_backlog(later)
    assert d._backlog_eligible_since == later


@pytest.mark.asyncio
async def test_eligibility_clock_does_not_restart_while_it_stays_eligible(monkeypatch):
    full = list(range(cl.DENSITY_THRESHOLD))
    d, _ = _daemon(monkeypatch, [full, full])
    monkeypatch.setattr(cl, "NREM_ELIGIBILITY_RECHECK_SEC", 0)

    await d._refresh_backlog(NOW)
    await d._refresh_backlog(NOW + timedelta(seconds=30))
    assert d._backlog_eligible_since == NOW


@pytest.mark.asyncio
async def test_eligibility_clock_clears_when_the_backlog_drains(monkeypatch):
    d, _ = _daemon(monkeypatch, [list(range(cl.DENSITY_THRESHOLD)), []])
    monkeypatch.setattr(cl, "NREM_ELIGIBILITY_RECHECK_SEC", 0)

    await d._refresh_backlog(NOW)
    assert d._backlog_eligible_since is not None
    await d._refresh_backlog(NOW + timedelta(seconds=30))
    assert d._backlog_eligible_since is None


@pytest.mark.asyncio
async def test_unreadable_ledger_keeps_the_previous_observation(monkeypatch):
    """Fail CLOSED on the eligibility question but do not reset the clock: a
    transient DB blip must neither invent work nor rearm the backstop at zero."""
    full = list(range(cl.DENSITY_THRESHOLD))
    d, _ = _daemon(monkeypatch, [full, RuntimeError("pg down")])
    monkeypatch.setattr(cl, "NREM_ELIGIBILITY_RECHECK_SEC", 0)

    await d._refresh_backlog(NOW)
    assert await d._refresh_backlog(NOW + timedelta(seconds=30)) == full
    assert d._backlog_eligible_since == NOW


@pytest.mark.asyncio
async def test_unreadable_ledger_is_recorded_not_merely_logged(monkeypatch):
    """A daemon acting on a stale eligibility view reports "not due" — exactly
    what a daemon with nothing to do reports. Without a durable record the
    operator cannot tell a quiet system from a blind one."""
    d, _ = _daemon(monkeypatch, [RuntimeError("pg down")])
    deferrals = []
    monkeypatch.setattr(cl, "_crun_record_deferred",
                        lambda ct, reason: deferrals.append((ct, reason)))

    await d._refresh_backlog(NOW)
    assert deferrals == [("fact_consolidation", "eligibility_read_failed")]


@pytest.mark.asyncio
async def test_a_readable_ledger_records_no_deferral(monkeypatch):
    d, _ = _daemon(monkeypatch, [[1, 2]])
    deferrals = []
    monkeypatch.setattr(cl, "_crun_record_deferred",
                        lambda ct, reason: deferrals.append((ct, reason)))

    await d._refresh_backlog(NOW)
    assert deferrals == []


@pytest.mark.asyncio
async def test_first_probe_on_an_empty_ledger_leaves_nothing_due(monkeypatch):
    d, _ = _daemon(monkeypatch, [[]])
    assert await d._refresh_backlog(NOW) == []
    assert d._backlog_eligible_since is None


# ── the idle clock can see REM ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_busy_pool_refreshes_the_consolidation_clock(monkeypatch):
    """The consolidation clock used to be written in exactly one place — the
    notify handler — so REM could hold the LLM slot for twenty minutes while it
    ran on and NREM became due mid-batch. No threshold value fixes a clock that
    cannot see the largest consumer of the resource it guards."""
    d = ConsolidationDaemon()
    stale = NOW - timedelta(hours=1)
    d.last_activity = d.last_busy = stale

    async def _busy(**kwargs):
        return False
    monkeypatch.setattr(cl, "pool_has_free_slot", _busy)

    await d._note_pool_activity(NOW)
    assert d.last_busy == NOW
    assert d._quiet_since(NOW) == 0


@pytest.mark.asyncio
async def test_busy_pool_does_NOT_touch_the_sweep_clock(monkeypatch):
    """The two clocks are split on purpose. The hygiene sweep (backfill,
    reconciliation, insight pass) has NO backstop, so gating it on a clock a
    busy pool can hold open indefinitely would let a continuously-loaded system
    suppress it forever — the 5.2-day insight drought, rebuilt."""
    d = ConsolidationDaemon()
    stale = NOW - timedelta(hours=1)
    d.last_activity = d.last_busy = stale

    async def _busy(**kwargs):
        return False
    monkeypatch.setattr(cl, "pool_has_free_slot", _busy)

    await d._note_pool_activity(NOW)
    assert d.last_activity == stale
    assert cl.sweep_due(NOW, last_sweep_time=NOW - timedelta(hours=2),
                        last_activity=d.last_activity, has_pending=False,
                        idle_threshold=900, sweep_interval=3600)


@pytest.mark.asyncio
async def test_free_pool_leaves_both_clocks_running(monkeypatch):
    d = ConsolidationDaemon()
    stale = NOW - timedelta(hours=1)
    d.last_activity = d.last_busy = stale

    async def _free(**kwargs):
        return True
    monkeypatch.setattr(cl, "pool_has_free_slot", _free)

    await d._note_pool_activity(NOW)
    assert d.last_busy == stale and d.last_activity == stale
    assert d._quiet_since(NOW) == 3600


def test_quiet_since_takes_the_LATER_of_the_two_clocks():
    """A save during a busy pool, or a busy pool after a save — either way the
    system was not quiet since the more recent of them."""
    d = ConsolidationDaemon()
    d.last_activity = NOW - timedelta(seconds=30)
    d.last_busy = NOW - timedelta(seconds=600)
    assert d._quiet_since(NOW) == 30
    d.last_activity = NOW - timedelta(seconds=600)
    d.last_busy = NOW - timedelta(seconds=30)
    assert d._quiet_since(NOW) == 30


@pytest.mark.asyncio
async def test_pool_probe_is_rate_limited(monkeypatch):
    """One /pool/status GET per NREM_POOL_PROBE_SEC, not one per 1s listen tick."""
    d = ConsolidationDaemon()
    probes = []

    async def _busy(**kwargs):
        probes.append(1)
        return False
    monkeypatch.setattr(cl, "pool_has_free_slot", _busy)
    monkeypatch.setattr(cl, "NREM_POOL_PROBE_SEC", 15)

    await d._note_pool_activity(NOW)
    await d._note_pool_activity(NOW + timedelta(seconds=14))
    assert len(probes) == 1
    await d._note_pool_activity(NOW + timedelta(seconds=15))
    assert len(probes) == 2


# ── the cycle no longer eats its own entry points ────────────────────────────

def _cycle_daemon(monkeypatch, rows):
    """Daemon wired for run_consolidation_cycle: the (project, domain)
    discovery step (`_find_grounded_fact_groups`) returns `rows`, and the
    idle-record + fold body are captured, not executed.

    v2 (C1): discovery takes NO ids any more — it is always an unrestricted
    scan of the whole grounded-fact population (see
    `_find_grounded_fact_groups`'s docstring: a group's density must be judged
    on its WHOLE current membership, not on whichever facts triggered this
    pass). So what is observable here is WHETHER discovery ran
    (`seen["discovery_calls"]`), not which ids it was handed — that entire
    class of assertion (`test_cycle_anchors_on_the_durable_backlog` /
    `test_cycle_unions_requeued_ids_with_the_ledger`, below) moved from
    "which ids reached the query" to "did the durable-backlog gate still let
    the cycle proceed to discovery at all"."""
    d = ConsolidationDaemon()
    seen = {"discovery_calls": 0, "idle": [], "folded": []}

    async def _find():
        seen["discovery_calls"] += 1
        return rows
    d._find_grounded_fact_groups = _find

    async def _consolidate(rs):
        seen["folded"].append(rs)
    d._consolidate_clusters = _consolidate

    monkeypatch.setattr(cl, "_crun_record_idle",
                        lambda ct, eligible_clusters=0: seen["idle"].append((ct, eligible_clusters)))
    return d, seen


@pytest.mark.asyncio
async def test_cycle_calls_discovery_when_the_durable_backlog_is_non_empty(monkeypatch):
    d, seen = _cycle_daemon(monkeypatch, rows=[])
    d._backlog = [11, 12, 13]
    await d.run_consolidation_cycle()
    assert seen["discovery_calls"] == 1


@pytest.mark.asyncio
async def test_cycle_still_runs_when_only_requeued_ids_are_pending(monkeypatch):
    """Requeued ids (from a fold that failed last cycle) still count toward
    "is there anything to look at" even with an empty ledger backlog — the
    union with pending_pg_ids that used to be threaded into the cluster-finder
    query (pre-v2) now only ever feeds this go/no-go decision."""
    d, seen = _cycle_daemon(monkeypatch, rows=[])
    d._backlog = []
    d._requeue([12, 99])
    await d.run_consolidation_cycle()
    assert seen["discovery_calls"] == 1


@pytest.mark.asyncio
async def test_no_cluster_run_does_not_consume_its_entry_points(monkeypatch):
    """The old cycle cleared `pending_pg_ids` BEFORE finding clusters and the
    no-cluster path returned without requeueing (`_requeue` was exception-only),
    so a no-op run threw its entry points away and the facts behind them went
    unconsidered until some unrelated save happened to re-trigger the cycle.
    The durable backlog cannot be consumed by a run that folded nothing —
    still true post-v2: a second empty pass still re-runs discovery."""
    d, seen = _cycle_daemon(monkeypatch, rows=[])
    d._backlog = [11, 12, 13]

    await d.run_consolidation_cycle()
    await d.run_consolidation_cycle()

    assert seen["discovery_calls"] == 2
    assert seen["folded"] == []


@pytest.mark.asyncio
async def test_no_cluster_run_records_itself_as_idle(monkeypatch):
    """A correctly-idle cycle must be able to SAY it is idle. Fact consolidation
    only ever opened a run row when it had clusters to fold, so it recorded
    eligible_clusters=NULL forever; the health surface reads NULL as "no census"
    and falls back to a looser count, which reported the idle cycle as STALLED."""
    d, seen = _cycle_daemon(monkeypatch, rows=[])
    d._backlog = [11, 12, 13]
    await d.run_consolidation_cycle()
    assert seen["idle"] == [("fact_consolidation", 0)]


@pytest.mark.asyncio
async def test_a_folding_run_records_no_idle_row(monkeypatch):
    row = {"pg_id": 1, "content": "c", "project": "p", "domain": "d"}
    d, seen = _cycle_daemon(monkeypatch, rows=[row])
    d._backlog = [11, 12, 13]
    await d.run_consolidation_cycle()
    assert seen["idle"] == []
    assert seen["folded"] == [[row]]


@pytest.mark.asyncio
async def test_cycle_with_nothing_anchored_does_no_work(monkeypatch):
    d, seen = _cycle_daemon(monkeypatch, rows=[])
    d._backlog = []
    await d.run_consolidation_cycle()
    assert seen["discovery_calls"] == 0 and seen["idle"] == []
