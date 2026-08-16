"""The insight cycle's SINGLETON-COMPONENT deferral (operator ruling
2026-08-16) — the third application of the I7/decision:1121 class ("a
deliberate skip must not read as a stall"), following the exact precedent of
`dead_lettered_clusters` (fact:1189, D1) and `unchanged_clusters` (fact:1240,
v0.9.0).

The defect this fixes: the insight cycle's eligible census counted
single-judgement components the fold has never been able to act on — a
component whose judgement reach is exactly 1 has nothing to relate yet, so it
was never attempted, yet it stayed inside `eligible_clusters` forever. A
permanently-stranded singleton (no second judgement ever joins its component)
therefore reads as backlog the fold "failed" to clear, and
`_consolidation_stall_verdict` (coordinator.py) can never see the backlog
clear. Measured live before the fix: 48 fold attempts / 0 successes in 24h
against 2 permanent singleton clusters, `consolidation.stalled=true`,
`stalled_types=['insight']`.

The contract under test, mirroring
`test_run_insight_cycle_dead_lettered_cluster_excluded_from_eligible_census`
in test_insight_consolidation.py:

  * a component whose `judgement_ids` has length < 2 is excluded from
    `rec.eligible_clusters`, counted under the NEW `singleton_clusters` key
    (never an alias for `eligible_clusters`), and `_fold_insight` is never
    invoked for it;
  * a two-judgement component in the SAME pass is unaffected — the partition
    does not over-filter;
  * the `consolidation_runs.extra` JSONB (`_CycleRec.extra()`) carries
    `singleton_clusters`.

No DB, no Neo4j, no LLM — conventions of test_insight_consolidation.py /
test_nrem_confidence.py.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
# ⚠ Module reference captured HERE, at COLLECTION time — see
# test_insight_consolidation.py's identical warning: test_rem_loop.py
# dynamically re-execs consolidation_loop.py and overwrites
# sys.modules["consolidation_loop"] at ITS OWN collection time. A local
# `import consolidation_loop as cl` done later, inside a test function body,
# would silently rebind to that swapped-in copy.
import consolidation_loop as cl
from consolidation_loop import ConsolidationDaemon, _CycleRec


class StubCursor:
    def __init__(self, script, executed):
        self._script = script
        self.executed = executed
        self._current = {"rowcount": 0, "rows": []}

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self._current = self._script.pop(0) if self._script else {"rowcount": 0, "rows": []}

    @property
    def rowcount(self):
        return self._current["rowcount"]

    def fetchall(self):
        return self._current["rows"]

    def fetchone(self):
        rows = self._current["rows"]
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubConn:
    def __init__(self, script=None):
        self._script = script or []
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return StubCursor(self._script, self.executed)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class _AsyncCtx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


class FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def data(self):
        return self._rows


class FakeSession:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    async def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        return self._results.pop(0) if self._results else FakeResult()


def daemon_with_fake_graph(results=None):
    daemon = ConsolidationDaemon()
    session = FakeSession(results)
    daemon.driver = MagicMock()
    daemon.driver.session = MagicMock(return_value=_AsyncCtx(session))
    return daemon, session


def _wire_common(monkeypatch):
    """Wiring shared by every run_insight_cycle test in this file — no
    reconciliation, no re-folds, no active insights to match identity
    against, no dead-letter history."""
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: StubConn())
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_decision_ids", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: [])
    monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda conn, p, d: None)
    monkeypatch.setattr(cl, "fetch_fold_dead_letter_counts", lambda: {})
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)


# ── run_insight_cycle: the partition ───────────────────────────────────────

@pytest.mark.asyncio
async def test_singleton_component_excluded_from_eligible_census_and_never_folded(monkeypatch):
    """THE fix. A lone-judgement component ([245]) is excluded from
    eligible_clusters, counted under singleton_clusters=1, and _fold_insight
    is never called for it."""
    _wire_common(monkeypatch)
    finish = {}
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finish.update(args=a, kwargs=k))

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "shared-memory-GitHub/architecture", "decision_ids": [245],
         "judgement_ids": [245], "judgement_types": {245: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "architecture"},
    ])
    daemon._fold_insight = AsyncMock(return_value=True)

    await daemon.run_insight_cycle()

    assert daemon._fold_insight.await_count == 0
    assert finish["kwargs"]["eligible_clusters"] == 0
    assert finish["kwargs"]["extra"]["singleton_clusters"] == 1
    # Never folded into eligible_clusters' own meaning.
    assert "dead_lettered_clusters" not in finish["kwargs"]["extra"] \
        or finish["kwargs"]["extra"]["dead_lettered_clusters"] == 0


@pytest.mark.asyncio
async def test_two_judgement_component_still_folds_in_the_same_pass(monkeypatch):
    """The partition does not over-filter: a two-judgement component in the
    SAME pass as a singleton still folds, and eligible_clusters counts only
    the non-singleton one."""
    _wire_common(monkeypatch)
    finish = {}
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finish.update(args=a, kwargs=k))

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "shared-memory-GitHub/architecture", "decision_ids": [245, 267],
         "judgement_ids": [245, 267], "judgement_types": {245: "Decision", 267: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "architecture"},
        {"entity": "shared-memory-GitHub/infrastructure", "decision_ids": [345],
         "judgement_ids": [345], "judgement_types": {345: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "infrastructure"},
    ])
    daemon._fold_insight = AsyncMock(return_value=True)

    await daemon.run_insight_cycle()

    assert daemon._fold_insight.await_count == 1
    assert daemon._fold_insight.await_args.args[2] == [245, 267]
    assert finish["kwargs"]["eligible_clusters"] == 1
    assert finish["kwargs"]["extra"]["singleton_clusters"] == 1


@pytest.mark.asyncio
async def test_no_singletons_reports_zero_not_absence(monkeypatch):
    """A cycle that deferred nothing this pass reports singleton_clusters=0
    (a census ran), not absence — same presence contract as
    dead_lettered_clusters."""
    _wire_common(monkeypatch)
    finish = {}
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finish.update(args=a, kwargs=k))

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "shared-memory-GitHub/architecture", "decision_ids": [245, 267],
         "judgement_ids": [245, 267], "judgement_types": {245: "Decision", 267: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "architecture"},
    ])
    daemon._fold_insight = AsyncMock(return_value=True)

    await daemon.run_insight_cycle()

    assert finish["kwargs"]["eligible_clusters"] == 1
    # A census ran (singleton_clusters is a NEW additive key, always present
    # once a cycle body executes) and reports 0 — a census that ran and found
    # nothing to defer, not the absence of a census.
    assert finish["kwargs"]["extra"]["singleton_clusters"] == 0


# ── _CycleRec.extra() ───────────────────────────────────────────────────────

def test_cycle_rec_extra_carries_singleton_clusters():
    rec = _CycleRec()
    rec.singleton_clusters = 3
    extra = rec.extra()
    assert extra is not None
    assert extra["singleton_clusters"] == 3


def test_cycle_rec_extra_is_none_when_nothing_counted():
    rec = _CycleRec()
    assert rec.extra() is None
