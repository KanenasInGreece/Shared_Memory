"""v1.0.46 insight readiness and the REM-processed thematic seed.

Each test names the failure that brings it back. Fixtures use proj-a / dom-x.
"""
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import consolidation_loop as cl
from insight_gate import unprocessed_carried
from nrem_gate import eligible_domain_level_clusters, project_domain_maps
from ontology import ONT


class _Result:
    def __init__(self, rows):
        self._rows = rows

    async def data(self):
        return self._rows


class _Conn:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        if getattr(self, "sql", "") and "FROM technical_docs WHERE id" in self.sql:
            ids = self.params[0]
            return [(i, f"body {i}", "proj-a", "decision", {}) for i in ids]
        return []

    def fetchone(self):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _Graph:
    """Answers the readiness read. ``raise_on`` is the 0-based readiness call
    that fails. ``rows_for`` builds the rows for the ids the query asked."""

    def __init__(self, rows_for=None, raise_on=None):
        self.rows_for = rows_for
        self.raise_on = raise_on
        self.readiness_calls = []
        self._n = 0

    def session(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def run(self, query, **params):
        if "AS rem_invalid" not in query:
            return _Result([])
        ids = list(params.get("ids") or [])
        n = self._n
        self._n += 1
        self.readiness_calls.append(ids)
        if self.raise_on is not None and n == self.raise_on:
            raise RuntimeError("graph down")
        if self.rows_for is not None:
            return _Result(self.rows_for(ids))
        return _Result([
            {"pg_id": i, "rem_processed": True, "superseded": False,
             "rem_invalid": False}
            for i in ids
        ])


def _processed(ids, bad=(), superseded=()):
    return [
        {"pg_id": i, "rem_processed": i not in bad, "superseded": i in superseded,
         "rem_invalid": False}
        for i in ids
    ]


def _cluster(ids, entity="proj-a/dom-x"):
    return {
        "entity": entity,
        "decision_ids": list(ids),
        "judgement_ids": list(ids),
        "judgement_types": {i: "Decision" for i in ids},
        "projects": ["proj-a"],
        "domain": "dom-x",
    }


def _arm(monkeypatch, graph, *, refolds=None, fresh=None, reversals=None,
         blocked_rem=None, active=None, raise_fresh=False, raise_active=False):
    finishes = []
    rev_calls = []

    def _blocked(kind):
        if kind == "rem":
            if blocked_rem is None:
                return set()
            if blocked_rem == "raise":
                raise RuntimeError("backlog unreadable")
            return set(blocked_rem)
        return set()

    def _rev(conn, ids):
        rev_calls.append(tuple(sorted(int(i) for i in ids)))
        if reversals is None:
            return []
        return list(reversals(ids))

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_record_ids", lambda conn: [1] if refolds else [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: list(refolds or []))
    monkeypatch.setattr(cl, "fetch_reversal_context", _rev)
    monkeypatch.setattr(cl, "fetch_insight_outbox_rows", lambda conn, ids: [])
    monkeypatch.setattr(cl, "close_refold_ledger_rows", lambda conn, context: None)
    monkeypatch.setattr(cl, "_fetch_outbox_created_at", lambda ids: {})
    monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda conn, p, d: None)
    monkeypatch.setattr(cl.llm_backlog, "blocked_keys", _blocked)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 7)
    monkeypatch.setattr(
        cl, "_crun_finish", lambda *a, **k: finishes.append((a, k)))

    if raise_active:
        def _active(conn):
            raise RuntimeError("active rows down")
        monkeypatch.setattr(cl, "fetch_active_insight_rows", _active)
    else:
        monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: list(active or []))

    daemon = cl.ConsolidationDaemon()
    daemon.driver = graph
    if raise_fresh:
        async def _boom():
            raise RuntimeError("walk down")
        daemon._find_fresh_insight_clusters = _boom
    elif fresh is not None:
        daemon._find_fresh_insight_clusters = AsyncMock(return_value=list(fresh))
    daemon._fold_insight = AsyncMock(return_value=True)
    return daemon, finishes, rev_calls


def _extra(finishes):
    assert finishes, "the cycle recorded no consolidation_runs row"
    return finishes[-1][0][1], finishes[-1][1].get("extra"), finishes[-1][1]


def test_d_superseded_row_is_exempt():
    """N3. A carried reversed Decision with superseded set and rem_processed
    false is exempt. Dies when that flag is read as unprocessed."""
    rows = [{"pg_id": 10, "rem_processed": False, "superseded": True,
             "rem_invalid": False}]
    unprocessed, duplicated = unprocessed_carried([10], rows)
    assert unprocessed == []
    assert duplicated == []


def test_f_missing_row_is_unprocessed():
    """N4. A carried id with no graph node is unprocessed. Dies when a
    missing row is read as processed."""
    unprocessed, duplicated = unprocessed_carried([10, 11], [
        {"pg_id": 10, "rem_processed": True, "superseded": False,
         "rem_invalid": False},
    ])
    assert unprocessed == [11]
    assert duplicated == []


def test_i_duplicate_node_defers_and_is_named():
    """N7. Two rows for one pg_id, in either order and with either
    superseded value, come back in unprocessed and in duplicated. Dies
    when the rows are merged or the superseded one exempts the id."""
    base = [
        {"pg_id": 10, "rem_processed": True, "superseded": False,
         "rem_invalid": False},
        {"pg_id": 10, "rem_processed": False, "superseded": False,
         "rem_invalid": False},
    ]
    for flip in (False, True):
        for order in (base, list(reversed(base))):
            rows = [dict(r) for r in order]
            rows[0]["superseded"] = flip
            unprocessed, duplicated = unprocessed_carried([10], rows)
            assert unprocessed == [10]
            assert duplicated == [10]


def test_rows_outside_the_carried_set_are_ignored():
    """A row for an id the fold does not carry changes neither list."""
    unprocessed, duplicated = unprocessed_carried([10], [
        {"pg_id": 99, "rem_processed": False, "superseded": False,
         "rem_invalid": False},
        {"pg_id": 10, "rem_processed": True, "superseded": False,
         "rem_invalid": False},
    ])
    assert unprocessed == []
    assert duplicated == []


@pytest.mark.asyncio
async def test_n1b_unprocessed_fact_drops_the_group_below_threshold():
    """N1. A group sitting on the density threshold yields no cluster while
    one grounding fact is not REM-processed, and yields one cluster once
    that fact is processed. The fake keeps the unprocessed row unless the
    executed query contains the predicate, so deleting the predicate makes
    the group stay at the threshold and the "no cluster" assertion die."""
    threshold = ONT.density_threshold
    facts = [
        {"pg_id": i, "content": f"f{i}", "project": "proj-a", "domain": "dom-x",
         "rem_processed": i != threshold}
        for i in range(1, threshold + 1)
    ]

    class _Seed:
        def session(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def run(self, query, **params):
            rows = facts
            if "coalesce(f.rem_processed, false) = true" in query:
                rows = [r for r in facts if r["rem_processed"]]
            return _Result(rows)

    daemon = cl.ConsolidationDaemon()
    daemon.driver = _Seed()

    rows = await daemon._find_grounded_fact_groups()
    project_map, domains_map, registered, pg_ids = project_domain_maps(rows)
    found = eligible_domain_level_clusters(
        [""] * len(pg_ids), pg_ids, project_map, domains_map,
        threshold, registered)
    assert found == []

    facts[-1]["rem_processed"] = True
    rows = await daemon._find_grounded_fact_groups()
    project_map, domains_map, registered, pg_ids = project_domain_maps(rows)
    found = eligible_domain_level_clusters(
        [""] * len(pg_ids), pg_ids, project_map, domains_map,
        threshold, registered)
    assert len(found) == 1
    assert found[0][0] == ("proj-a", "dom-x")


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.asyncio
async def test_a_fresh_deferral_names_the_id_and_the_withheld_clause(monkeypatch, caplog):
    """N2. A fresh set with one unprocessed carried id makes no LLM call,
    stays out of eligible_clusters, and is logged once. The withheld clause
    names the id when blocked_keys('rem') returns it, and is absent when
    that set is empty. Dies when the fresh path skips the check."""
    caplog.set_level("WARNING")
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    fresh = [_cluster([10, 11])]
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, fresh=fresh, blocked_rem={"11"})
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    outcome, extra, kwargs = _extra(finishes)
    assert outcome == "completed"
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is False
    assert kwargs["eligible_clusters"] == 0
    withheld = (
        "NREM insight: 'proj-a/dom-x' (2 judgements) deferred — "
        "REM has not processed pg_id(s) [11]; folds when REM finishes them; "
        "[11] withheld by a REM backlog row; see /health rem_daemon."
    )
    assert withheld in _warnings(caplog)

    caplog.clear()
    graph2 = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    daemon2, finishes2, _r = _arm(
        monkeypatch, graph2, fresh=fresh, blocked_rem=set())
    await daemon2.run_insight_cycle()
    plain = (
        "NREM insight: 'proj-a/dom-x' (2 judgements) deferred — "
        "REM has not processed pg_id(s) [11]; folds when REM finishes them; "
        "see /health rem_daemon."
    )
    assert plain in _warnings(caplog)
    assert "withheld by a REM backlog row" not in " ".join(_warnings(caplog))


@pytest.mark.asyncio
async def test_a_withheld_read_failure_omits_the_clause(monkeypatch, caplog):
    """N2. A failed blocked_keys('rem') read omits the withheld clause and
    still defers. Dies when that failure drops the deferral."""
    caplog.set_level("WARNING")
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, fresh=[_cluster([10, 11])], blocked_rem="raise")
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert "withheld by a REM backlog row" not in " ".join(_warnings(caplog))


@pytest.mark.asyncio
async def test_b_refold_defers_when_a_carried_id_is_unprocessed(monkeypatch, caplog):
    """N2. A refold whose carried set is unprocessed is not folded and is
    counted. Dies when the refold path skips the readiness check."""
    caplog.set_level("WARNING")
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    daemon, finishes, _rev = _arm(monkeypatch, graph, refolds=refolds, fresh=[])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    _outcome, extra, kwargs = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is False
    assert kwargs["eligible_clusters"] == 0
    assert "pg_id(s) [11]" in " ".join(_warnings(caplog))


@pytest.mark.asyncio
async def test_c1_unprocessed_reversing_retrospective_defers_a_fresh_fold(monkeypatch, caplog):
    """N2. A fresh fold waits when its reversing retrospective is not
    REM-processed. Dies when that path omits reversal ids from the check."""
    caplog.set_level("WARNING")
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={20}))

    def _rev(ids):
        return [{"decision_id": 8, "decision_title": "old",
                 "retro_id": 20, "retro_content": "why"}]

    daemon, finishes, _calls = _arm(
        monkeypatch, graph, fresh=[_cluster([10, 11])], reversals=_rev)
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    assert any(20 in ids for ids in graph.readiness_calls)
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert "pg_id(s) [20]" in " ".join(_warnings(caplog))


@pytest.mark.asyncio
async def test_c2_unprocessed_reversing_retrospective_defers_a_refold(monkeypatch, caplog):
    """N2. A refold waits when its reversing retrospective is not
    REM-processed. Dies when the refold path omits reversal ids."""
    caplog.set_level("WARNING")
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={20}))

    def _rev(ids):
        return [{"decision_id": 8, "decision_title": "old",
                 "retro_id": 20, "retro_content": "why"}]

    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    daemon, finishes, _calls = _arm(
        monkeypatch, graph, refolds=refolds, fresh=[], reversals=_rev)
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    assert any(20 in ids for ids in graph.readiness_calls)
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is False


@pytest.mark.asyncio
async def test_d_superseded_carried_decision_still_folds(monkeypatch):
    """N3. A carried Decision whose node is superseded and not
    rem_processed does not defer the fold. Dies when that node is treated
    as blocking."""
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={10}, superseded={10}))
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, fresh=[_cluster([10, 11])])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 1
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 0
    assert extra["rem_pending_incomplete"] is False


@pytest.mark.asyncio
async def test_e_unprocessed_pass_through_fact_does_not_defer(monkeypatch):
    """N3. An unprocessed fact that the walk only passes through does not
    defer a fold of processed judgements. Dies when the walked set is
    checked instead of the carried set: fact 50 would then be in the
    readiness ids and the fold would not run."""
    class _Walk:
        def __init__(self):
            self.readiness_ids = []

        def session(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def run(self, query, **params):
            if "AS rem_invalid" in query:
                ids = list(params.get("ids") or [])
                self.readiness_ids.append(ids)
                return _Result([
                    {"pg_id": i, "rem_processed": i != 50,
                     "superseded": False, "rem_invalid": False}
                    for i in ids
                ])
            if "UNWIND $ids" in query:
                frontier = set(params.get("ids") or [])
                seeds = set(range(1, ONT.density_threshold + 1))
                if frontier & seeds:
                    return _Result([
                        {"src": 1, "dst": 10, "dst_label": ONT.decision,
                         "dst_consolidated": False},
                        {"src": 1, "dst": 11, "dst_label": ONT.retrospective,
                         "dst_consolidated": False},
                        {"src": 1, "dst": 50, "dst_label": ONT.fact,
                         "dst_consolidated": False},
                    ])
                return _Result([])
            if "f.pg_id AS pg_id" in query:
                return _Result([
                    {"pg_id": i, "content": f"f{i}", "project": "proj-a",
                     "domain": "dom-x"}
                    for i in range(1, ONT.density_threshold + 1)
                ])
            return _Result([])

    graph = _Walk()
    daemon, finishes, _rev = _arm(monkeypatch, graph, fresh=None)
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 1
    assert daemon._fold_insight.await_args.args[2] == [10, 11]
    assert all(50 not in ids for ids in graph.readiness_ids)
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 0


@pytest.mark.asyncio
async def test_f_missing_node_makes_no_llm_call(monkeypatch):
    """N4. A carried id with no readiness row makes no LLM call. Dies when
    a missing row is read as processed and the fold runs."""
    graph = _Graph(rows_for=lambda ids: _processed([i for i in ids if i != 11]))
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, fresh=[_cluster([10, 11])])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is False


@pytest.mark.asyncio
async def test_g1_check1_failure_folds_nothing_with_and_without_refolds(monkeypatch, caplog):
    """N5. Check 1 runs on every cycle. A failed readiness read, with
    refold candidates and with none, makes no LLM call and leaves
    rem_pending_* unset. The no-refold run issues the empty-id read once.
    Dies when the error is swallowed or check 1 is skipped on an empty list."""
    caplog.set_level("WARNING")
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    graph = _Graph(raise_on=0)
    daemon, finishes, _rev = _arm(monkeypatch, graph, refolds=refolds, fresh=[_cluster([12, 13])])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    outcome, extra, _kw = _extra(finishes)
    assert outcome == "completed"
    assert extra is None
    assert any("readiness check 1 (refolds) failed" in m for m in _warnings(caplog))

    caplog.clear()
    graph2 = _Graph(raise_on=0)
    daemon2, finishes2, _r = _arm(monkeypatch, graph2, fresh=[_cluster([12, 13])])
    await daemon2.run_insight_cycle()
    assert daemon2._fold_insight.await_count == 0
    _outcome, extra2, _kw = _extra(finishes2)
    assert extra2 is None
    assert graph2.readiness_calls == [[]]
    assert any("readiness check 1 (refolds) failed" in m for m in _warnings(caplog))


@pytest.mark.asyncio
async def test_g2a_check2_failure_keeps_a_zero_count(monkeypatch, caplog):
    """N5. Check 2 failing after check 1 saw no deferral leaves the refold
    that already ran, makes no fresh LLM call, and records
    rem_pending_clusters 0 with rem_pending_incomplete true. Gate skips and
    eligible_clusters stay unset. Dies when the error is swallowed or the
    flag is cleared."""
    caplog.set_level("WARNING")
    graph = _Graph(raise_on=1)
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, refolds=refolds, fresh=[_cluster([12, 13])])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 1
    assert daemon._fold_insight.await_args.args[2] == [10, 11]
    outcome, extra, kwargs = _extra(finishes)
    assert outcome == "completed"
    assert extra["rem_pending_clusters"] == 0
    assert extra["rem_pending_incomplete"] is True
    assert "insight_gate_skips" not in extra
    assert kwargs["eligible_clusters"] is None
    assert any("readiness check 2 (fresh clusters) failed" in m for m in _warnings(caplog))


@pytest.mark.asyncio
async def test_g2b_check2_failure_keeps_the_observed_deferral(monkeypatch):
    """N5. A refold deferral counted before check 2 fails stays at 1, and
    rem_pending_incomplete stays true. Dies when the count is reset or the
    flag is left false."""
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}), raise_on=1)
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, refolds=refolds, fresh=[_cluster([12, 13])])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    _outcome, extra, kwargs = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is True
    assert kwargs["eligible_clusters"] is None


@pytest.mark.asyncio
async def test_g3_walk_crash_keeps_the_observed_count(monkeypatch):
    """N5. A walk that raises after check 1 leaves the crashed row's extra
    with the count observed so far and rem_pending_incomplete true. Dies
    when the crash drops the count."""
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, refolds=refolds, raise_fresh=True)
    await daemon.run_insight_cycle()
    outcome, extra, _kw = _extra(finishes)
    assert outcome == "crashed"
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is True


@pytest.mark.asyncio
async def test_h_three_fresh_clusters_of_one_set_defer_once(monkeypatch, caplog):
    """N6. Three fresh clusters that share one unprocessed judgement set
    reach the readiness partition (active insights are empty, so identity
    keeps them) and register one deferral. Dies when the registry is
    removed and the count becomes 3, or when identity drops the clusters
    before the partition."""
    caplog.set_level("WARNING")
    seen = []
    real = cl.unprocessed_carried

    def _spy(ids, rows):
        seen.append(sorted(ids))
        return real(ids, rows)

    monkeypatch.setattr(cl, "unprocessed_carried", _spy)
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    fresh = [_cluster([10, 11], entity=f"proj-a/dom-x") for _ in range(3)]
    daemon, finishes, _rev = _arm(monkeypatch, graph, fresh=fresh, active=[])
    await daemon.run_insight_cycle()
    assert seen == [[10, 11], [10, 11], [10, 11]]
    assert daemon._fold_insight.await_count == 0
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is False
    deferred = [m for m in _warnings(caplog) if "deferred —" in m]
    assert len(deferred) == 1


@pytest.mark.asyncio
async def test_h_b_refold_and_three_fresh_clusters_defer_once(monkeypatch, caplog):
    """N6. The same unprocessed set as a refold and as three fresh clusters
    is one deferral and one log line. The readiness function runs once for
    the refold and once per fresh cluster. Dies when each sighting counts."""
    caplog.set_level("WARNING")
    seen = []
    real = cl.unprocessed_carried

    def _spy(ids, rows):
        seen.append(sorted(ids))
        return real(ids, rows)

    monkeypatch.setattr(cl, "unprocessed_carried", _spy)
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    fresh = [_cluster([10, 11]) for _ in range(3)]
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, refolds=refolds, fresh=fresh, active=[])
    await daemon.run_insight_cycle()
    assert seen == [[10, 11], [10, 11], [10, 11], [10, 11]]
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    deferred = [m for m in _warnings(caplog) if "deferred —" in m]
    assert len(deferred) == 1


@pytest.mark.asyncio
async def test_h2_crash_after_a_refold_deferral_keeps_the_count(monkeypatch):
    """N6. A refold deferral is written onto the record at once. A later
    crash in fetch_active_insight_rows leaves rem_pending_clusters 1 and
    rem_pending_incomplete true. Dies when the count is assigned only at
    the end of the cycle."""
    graph = _Graph(rows_for=lambda ids: _processed(ids, bad={11}))
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, refolds=refolds, fresh=[_cluster([12, 13])],
        raise_active=True)
    await daemon.run_insight_cycle()
    outcome, extra, _kw = _extra(finishes)
    assert outcome == "crashed"
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is True


@pytest.mark.asyncio
async def test_i_duplicate_node_is_logged_as_a_graph_defect(monkeypatch, caplog):
    """N7. A carried id with two nodes is deferred, and the log names it in
    the integrity clause and in one ERROR line. Dies when duplicates are
    exempted or duplicated comes back empty."""
    caplog.set_level("WARNING")

    def _rows(ids):
        out = []
        for i in ids:
            out.append({"pg_id": i, "rem_processed": True, "superseded": False,
                         "rem_invalid": False})
            if i == 10:
                out.append({"pg_id": i, "rem_processed": False, "superseded": True,
                             "rem_invalid": False})
        return out

    graph = _Graph(rows_for=_rows)
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, fresh=[_cluster([10, 11])])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    text = " ".join(_warnings(caplog))
    assert "graph-integrity defect: more than one node for pg_id(s) [10]" in text
    assert any("graph-integrity defect" in m and "[10]" in m for m in _errors(caplog))
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1


@pytest.mark.asyncio
async def test_i_check2_duplicate_of_a_pending_set_is_still_an_error(monkeypatch, caplog):
    """N7. Check 1 defers a judgement set because one carried id is
    unprocessed. Check 2 then finds a second spine node for another id of
    that same set. The set is not counted or warned again, and the
    duplicate is still one ERROR. Dies when the pending-set return skips
    the integrity error."""
    caplog.set_level("WARNING")
    calls = {"n": 0}

    def _rows(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            return _processed(ids, bad={11})
        rows = _processed(ids, bad={11})
        rows.append({
            "pg_id": 10, "rem_processed": True, "superseded": False,
            "rem_invalid": False,
        })
        return rows

    graph = _Graph(rows_for=_rows)
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    fresh = [_cluster([10, 11])]
    daemon, finishes, _rev = _arm(
        monkeypatch, graph, refolds=refolds, fresh=fresh, active=[])
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    deferred = [m for m in _warnings(caplog) if "deferred —" in m]
    assert len(deferred) == 1
    assert "graph-integrity defect" not in deferred[0]
    errors = [m for m in _errors(caplog) if "graph-integrity defect" in m]
    assert len(errors) == 1 and "[10]" in errors[0]
    _outcome, extra, _kw = _extra(finishes)
    assert extra["rem_pending_clusters"] == 1
    assert extra["rem_pending_incomplete"] is False


@pytest.mark.asyncio
async def test_j_reversal_rows_are_read_once_per_set_and_reach_the_prompt(monkeypatch):
    """N8. fetch_reversal_context runs once per distinct judgement set, and
    every call for a path happens before that path's first fold. The rows
    that reach the prompt are those rows. Dies when _fold_insight reads
    the reversal context again."""
    prompts = []
    rev_at_prompt = []
    rev_calls = []

    def _rev(ids):
        rev_calls.append(tuple(sorted(int(i) for i in ids)))
        return [{
            "decision_id": 9, "decision_title": "Old",
            "retro_id": 90, "retro_content": f"why {tuple(sorted(ids))}",
        }]

    async def _gen(entity, rows, previous_insight=None, reversal_lines=None):
        prompts.append(list(reversal_lines or []))
        rev_at_prompt.append(len(rev_calls))
        return None

    graph = _Graph()
    refolds = [
        (70, "proj-a/dom-x", [10, 11], "previous", {}),
        (71, "proj-a/dom-x", [12, 13], "previous", {}),
    ]
    fresh = [_cluster([14, 15])]
    daemon, _finishes, _ignored = _arm(
        monkeypatch, graph, refolds=refolds, fresh=fresh, reversals=_rev)
    daemon.generate_insight_slots = _gen
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    async def _fold(conn, entity, judgement_ids, **kwargs):
        return await cl.ConsolidationDaemon._fold_insight(
            daemon, conn, entity, judgement_ids, **kwargs)

    daemon._fold_insight = _fold
    await daemon.run_insight_cycle()

    assert rev_calls == [(10, 11), (12, 13), (14, 15)]
    assert rev_at_prompt == [2, 2, 3]
    assert prompts
    assert all(lines and "was REVERTED" in lines[0] and "why " in lines[0]
               for lines in prompts)
    assert "why (10, 11)" in prompts[0][0]
    assert "why (14, 15)" in prompts[-1][0]


@pytest.mark.asyncio
async def test_j_repeated_set_is_fetched_once_and_the_cache_reaches_the_prompt(monkeypatch):
    """N8. One judgement set is both a refold and a fresh cluster. Reversal
    rows are fetched once. Check 1 defers the refold, and the fresh fold's
    prompt renders that cached payload. Dies when a repeated set fetches
    again and the prompt renders the later rows."""
    calls = {"n": 0}

    def _rows(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            return _processed(ids, bad={11})
        return _processed(ids)

    payload_n = {"n": 0}

    def _rev(ids):
        payload_n["n"] += 1
        return [{
            "decision_id": 9, "decision_title": "Old",
            "retro_id": 90, "retro_content": f"payload-{payload_n['n']}",
        }]

    prompts = []

    async def _gen(entity, rows, previous_insight=None, reversal_lines=None):
        prompts.append(list(reversal_lines or []))
        return None

    graph = _Graph(rows_for=_rows)
    refolds = [(70, "proj-a/dom-x", [10, 11], "previous", {})]
    fresh = [_cluster([10, 11])]
    daemon, _finishes, rev_calls = _arm(
        monkeypatch, graph, refolds=refolds, fresh=fresh, reversals=_rev)
    daemon.generate_insight_slots = _gen
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    async def _fold(conn, entity, judgement_ids, **kwargs):
        return await cl.ConsolidationDaemon._fold_insight(
            daemon, conn, entity, judgement_ids, **kwargs)

    daemon._fold_insight = _fold
    await daemon.run_insight_cycle()

    assert rev_calls == [(10, 11)]
    assert len(prompts) == 1
    assert "payload-1" in prompts[0][0]
    assert "payload-2" not in prompts[0][0]
    assert "was REVERTED" in prompts[0][0]
