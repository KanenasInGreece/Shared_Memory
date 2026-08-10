"""C3 — cascading (lineage) supersession tests (Dreaming Cycle Plan to v2,
§5's AMENDED 2026-08-10 block; `retrospective:1178` refining `decision:384`).

THE RULE under test:

    Invalidation is identified from the stored lists. Re-gating is
    re-derived from the graph. The ledger is only the clock.

Three units, six invariants (I11-I16):
  - fetch_invalidated_summaries / resolve_standing_ids / retire_invalidated_
    summaries — U2/U3/U4, identification + retirement + the ledger clock.
  - close_refold_ledger_rows / drop_below_density_refold_rows — U4's close
    side (CLOSE, never DELETE) + I7 (a candidate that does not gate is not
    backlog).
  - fetch_refold_backlog / fetch_combined_fact_backlog — the widened due-ness
    input set, unchanged predicate.
  - supersede_covered_summaries' new explicit `kind` param — U5, kind
    isolation now unconditional (companion tests also live in
    test_insight_consolidation.py, updated for the new required-in-spirit
    parameter on the insight call site).

All Postgres/Neo4j I/O is stubbed — no live infrastructure required. Every
new/changed SQL string here was ALSO run verbatim against the live database
(see the C3 build report) — a green suite proves nothing about a query on
its own (CLAUDE.md).
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import consolidation_loop as cl
from consolidation_loop import (
    ConsolidationDaemon,
    close_refold_ledger_rows,
    drop_below_density_refold_rows,
    fetch_combined_fact_backlog,
    fetch_invalidated_summaries,
    fetch_refold_backlog,
    resolve_standing_ids,
    retire_invalidated_summaries,
    supersede_covered_summaries,
)


# ── Stubs (same shape as test_outbox_ledger.py / test_insight_consolidation.py) ─

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

    def cursor(self):
        return StubCursor(self._script, self.executed)

    def commit(self):
        self.commits += 1


class _AsyncCtx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        return False


class FakeSession:
    """Captures every (query, params) run against the fake Neo4j driver."""
    def __init__(self):
        self.calls = []

    async def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        class _R:
            async def data(self):
                return []
        return _R()


def daemon_with_fake_graph():
    daemon = ConsolidationDaemon()
    session = FakeSession()
    daemon.driver = MagicMock()
    daemon.driver.session = MagicMock(return_value=_AsyncCtx(session))
    return daemon, session


# ── fetch_invalidated_summaries — U2, three legs ──────────────────────────────

def test_leg1_thematic_holding_superseded_fact():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(50, [1, 2, 3], 3)]},   # leg 1
        {"rowcount": 0, "rows": []},                       # leg 2
        {"rowcount": 0, "rows": []},                       # leg 3 (leg1 non-empty)
    ])
    out = fetch_invalidated_summaries(conn)
    assert out == [{"summary_id": 50, "source_pg_ids": [1, 2, 3], "kind": "thematic",
                    "trigger_kind": "technical_docs", "trigger_id": 3}]
    leg1_sql, _ = conn.executed[0]
    assert "kind', 'thematic') <> 'insight'" in leg1_sql
    assert "COALESCE(t.superseded, false) = true" in leg1_sql
    leg3_sql, leg3_params = conn.executed[2]
    assert leg3_params == ([50],)


def test_leg3_never_runs_when_leg1_found_nothing():
    conn = StubConn(script=[
        {"rowcount": 0, "rows": []},   # leg1
        {"rowcount": 0, "rows": []},   # leg2
    ])
    assert fetch_invalidated_summaries(conn) == []
    assert len(conn.executed) == 2   # leg 3 never ran — nothing to cascade from


def test_leg2_insight_holding_reversed_decision_directly():
    conn = StubConn(script=[
        {"rowcount": 0, "rows": []},                         # leg1
        {"rowcount": 1, "rows": [(80, [10, 11, 12], 12)]},   # leg2
    ])
    out = fetch_invalidated_summaries(conn)
    assert out == [{"summary_id": 80, "source_pg_ids": [10, 11, 12], "kind": "insight",
                    "trigger_kind": "technical_docs", "trigger_id": 12}]
    leg2_sql, _ = conn.executed[1]
    assert "cs.metadata->>'kind' = 'insight'" in leg2_sql


def test_leg3_cascades_from_leg1_retired_summary_ids():
    # summary_ids is C4's field — 0 rows carry it on the live corpus, so this
    # leg is exercised only by this seeded fixture until C4 ships. "0 matches
    # on live data" is NOT proof this leg works — this test is the proof.
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(50, [1, 2, 3], 3)]},        # leg1
        {"rowcount": 0, "rows": []},                            # leg2
        {"rowcount": 1, "rows": [(90, [20, 21], 50)]},        # leg3
    ])
    out = fetch_invalidated_summaries(conn)
    assert {"summary_id": 90, "source_pg_ids": [20, 21], "kind": "insight",
           "trigger_kind": "community_summaries", "trigger_id": 50} in out
    leg3_sql, leg3_params = conn.executed[2]
    assert "jsonb_array_elements_text" in leg3_sql
    assert "summary_ids" in leg3_sql
    assert leg3_params == ([50],)


# ── resolve_standing_ids ───────────────────────────────────────────────────────

def test_resolve_standing_ids_empty_short_circuits():
    conn = StubConn()
    assert resolve_standing_ids(conn, []) == {}
    assert conn.executed == []


def test_resolve_standing_ids_maps_chain_terminal_state():
    # 1 -> superseded_by 2, 2 stands (live successor). 3 stands on its own
    # (no chain). 4 is a dead end: superseded, no successor (e.g. a reversed
    # decision — coordinator.py's reversal path never sets superseded_by).
    conn = StubConn(script=[
        {"rowcount": 3, "rows": [(1, 2, False), (3, 3, False), (4, 4, True)]},
    ])
    out = resolve_standing_ids(conn, [1, 3, 4])
    assert out == {1: (2, False), 3: (3, False), 4: (4, True)}
    sql, params = conn.executed[0]
    assert "WITH RECURSIVE" in sql
    assert "superseded_by" in sql
    assert params == ([1, 3, 4],)


# ── retire_invalidated_summaries — U3 + U4 ────────────────────────────────────

def test_retire_no_matches_is_a_no_op():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}, {"rowcount": 0, "rows": []}])
    assert retire_invalidated_summaries(conn) == ([], 0)
    assert conn.commits == 0


def test_retire_thematic_summary_opens_row_for_live_constituent_only():
    # Fact 2 is the leg-1 trigger (superseded, dead end — no successor). Fact
    # 1 is an ordinary still-live constituent that must rejoin the backlog.
    # I15: the trigger's OWN pg_id (2) must never appear in an opened row.
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(50, [1, 2], 2)]},               # leg1
        {"rowcount": 0, "rows": []},                                # leg2
        {"rowcount": 0, "rows": []},                                # leg3
        {"rowcount": 1, "rows": []},                                # UPDATE community_summaries
        {"rowcount": 2, "rows": [(1, 1, False), (2, 2, True)]},   # resolve_standing_ids
        {"rowcount": 1, "rows": []},                                # INSERT refold_ledger
    ])
    retired, opened = retire_invalidated_summaries(conn)
    assert retired == [(50, "thematic", [1, 2])]
    assert opened == 1
    assert conn.commits == 1

    update_sql, update_params = conn.executed[3]
    assert "SET superseded = true" in update_sql
    assert "superseded_reason = 'lineage'" in update_sql
    assert update_params == (50,)

    insert_sql, insert_params = conn.executed[5]
    assert insert_sql.startswith("INSERT INTO refold_ledger")
    assert insert_params == (1, 50, "thematic", "technical_docs", 2)


def test_retire_all_constituents_superseded_still_opens_row_for_standing_successor():
    # §5 defect #4, the motivating case: a retired summary whose ONLY
    # constituent is itself superseded (fact 5, the trigger) but has a live
    # successor (fact 7 via superseded_by) must still raise a row — for the
    # successor, resolved, not for 5.
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(60, [5], 5)]},   # leg1 — 5 is both constituent and trigger
        {"rowcount": 0, "rows": []},                 # leg2
        {"rowcount": 0, "rows": []},                 # leg3
        {"rowcount": 1, "rows": []},                 # UPDATE
        {"rowcount": 1, "rows": [(5, 7, False)]},   # resolve: 5 stands as 7
        {"rowcount": 1, "rows": []},                 # INSERT
    ])
    retired, opened = retire_invalidated_summaries(conn)
    assert opened == 1
    insert_sql, insert_params = conn.executed[5]
    assert insert_params[0] == 7   # the STANDING successor, never 5 itself


def test_retire_with_no_eligible_constituents_still_retires_opens_zero_rows():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(70, [9], 9)]},
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 1, "rows": []},               # UPDATE
        {"rowcount": 1, "rows": [(9, 9, True)]},  # dead end, no successor
    ])
    retired, opened = retire_invalidated_summaries(conn)
    assert retired == [(70, "thematic", [9])]
    assert opened == 0
    assert conn.commits == 1


def test_retire_skips_ledger_write_when_already_retired_concurrently():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(80, [1], 1)]},
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},   # UPDATE affected 0 rows — already retired
    ])
    retired, opened = retire_invalidated_summaries(conn)
    assert (retired, opened) == ([], 0)
    assert len(conn.executed) == 4   # no resolve_standing_ids call, no insert


def test_retire_multiple_summaries_commit_once_not_per_summary():
    # I14: retirement + ledger rows are atomic — ONE Postgres transaction for
    # the whole pass, not one commit per summary.
    conn = StubConn(script=[
        {"rowcount": 2, "rows": [(50, [1], 1), (60, [2], 2)]},   # leg1: two summaries
        {"rowcount": 0, "rows": []},                               # leg2
        {"rowcount": 0, "rows": []},                               # leg3
        {"rowcount": 1, "rows": []},                               # UPDATE 50
        {"rowcount": 1, "rows": [(1, 1, True)]},                 # resolve 50 -> dead end
        {"rowcount": 1, "rows": []},                               # UPDATE 60
        {"rowcount": 1, "rows": [(2, 2, True)]},                 # resolve 60 -> dead end
    ])
    retired, opened = retire_invalidated_summaries(conn)
    assert len(retired) == 2
    assert opened == 0
    assert conn.commits == 1


# ── I11 — reverse lookup, never set comparison (§5.2) ────────────────────────

def test_i11_reverse_lookup_retires_what_subset_coverage_structurally_cannot():
    """A reversal makes the covered set SMALLER: old {1,2,3} vs a refold's
    new {1,2} is NOT a subset relationship, so Mechanism A
    (supersede_covered_summaries) can never retire the old insight — proven
    directly below. Only reverse lookup (Mechanism B) can. This test makes
    NO assertion about any refold succeeding — it is the one the plan
    requires: "must fail if the explicit lineage path is removed EVEN
    THOUGH a refold still succeeds"."""
    # Mechanism A cannot do it:
    conn_a = StubConn(script=[{"rowcount": 1, "rows": [(70, [1, 2, 3], "", "insight")]}])
    assert supersede_covered_summaries(conn_a, 99, [1, 2], kind="insight") == []

    # Mechanism B (this unit) retires it directly, by reverse lookup.
    conn_b = StubConn(script=[
        {"rowcount": 0, "rows": []},                                        # leg1
        {"rowcount": 1, "rows": [(70, [1, 2, 3], 3)]},                     # leg2
        {"rowcount": 1, "rows": []},                                         # UPDATE
        {"rowcount": 3, "rows": [(1, 1, False), (2, 2, False), (3, 3, True)]},  # resolve
        {"rowcount": 1, "rows": []},                                         # INSERT pg_id=1
        {"rowcount": 1, "rows": []},                                         # INSERT pg_id=2
    ])
    retired, opened = retire_invalidated_summaries(conn_b)
    assert retired == [(70, "insight", [1, 2, 3])]
    assert opened == 2


# ── I12 — retiring an INSIGHT clears consolidated on its judgement nodes ─────

@pytest.mark.asyncio
async def test_i12_retiring_insight_clears_consolidated_on_graph_nodes(monkeypatch):
    daemon, session = daemon_with_fake_graph()
    fake_conn = MagicMock()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: fake_conn)
    monkeypatch.setattr(
        cl, "retire_invalidated_summaries",
        lambda conn: ([(70, "insight", [245, 267])], 2),
    )
    await daemon.run_lineage_invalidation_pass()
    assert len(session.calls) == 1
    cypher, params = session.calls[0]
    assert "Decision" in cypher and "Retrospective" in cypher
    assert "SET d.consolidated = false" in cypher
    assert params["ids"] == [245, 267]


@pytest.mark.asyncio
async def test_i12_thematic_retirement_never_touches_the_graph(monkeypatch):
    """Facts: do NOT clear f.consolidated — _find_grounded_fact_groups has no
    reader for it (its own docstring: "A fact's own `consolidated` flag
    plays NO part here")."""
    daemon, session = daemon_with_fake_graph()
    fake_conn = MagicMock()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: fake_conn)
    monkeypatch.setattr(
        cl, "retire_invalidated_summaries",
        lambda conn: ([(50, "thematic", [1, 2, 3])], 3),
    )
    await daemon.run_lineage_invalidation_pass()
    assert session.calls == []


@pytest.mark.asyncio
async def test_i12_nothing_retired_touches_neither_store_further(monkeypatch):
    daemon, session = daemon_with_fake_graph()
    fake_conn = MagicMock()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: fake_conn)
    monkeypatch.setattr(cl, "retire_invalidated_summaries", lambda conn: ([], 0))
    await daemon.run_lineage_invalidation_pass()
    assert session.calls == []


# ── I13 — kind isolation is UNCONDITIONAL (companion tests also live in
#          test_insight_consolidation.py, beside the function's other tests) ──

def test_i13_kind_param_defaults_to_thematic_and_is_always_checked():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(70, [1, 2], "", "insight")]}])
    # Default kind ("thematic", the fact-fold caller's own kind) must not
    # match a stored insight row even with a covering subset and no level.
    assert supersede_covered_summaries(conn, 99, [1, 2]) == []


# ── close_refold_ledger_rows / drop_below_density_refold_rows — U4 close, I15 ─

def test_close_refold_ledger_marks_refolded_and_dropped_independently():
    conn = StubConn(script=[
        {"rowcount": 2, "rows": []},   # refolded UPDATE
        {"rowcount": 1, "rows": []},   # dropped/constituent_superseded UPDATE
    ])
    refolded, dropped = close_refold_ledger_rows(conn, context="test")
    assert (refolded, dropped) == (2, 1)
    assert conn.commits == 1
    refolded_sql, _ = conn.executed[0]
    assert "status = 'refolded'" in refolded_sql
    assert "closed_reason = 'constituent_folded'" in refolded_sql
    dropped_sql, _ = conn.executed[1]
    assert "status = 'dropped'" in dropped_sql
    assert "closed_reason = 'constituent_superseded'" in dropped_sql
    # CLOSE, never DELETE (migration 031's model) — the row is the
    # attribution trail.
    assert "DELETE" not in refolded_sql
    assert "DELETE" not in dropped_sql


def test_close_refold_ledger_refolded_check_is_kind_scoped():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}, {"rowcount": 0, "rows": []}])
    close_refold_ledger_rows(conn)
    sql, _ = conn.executed[0]
    assert "summary_kind = 'thematic'" in sql
    assert "summary_kind = 'insight'" in sql


def test_drop_below_density_closes_with_reason_and_commits():
    conn = StubConn(script=[{"rowcount": 3, "rows": []}])
    dropped = drop_below_density_refold_rows(conn, [1, 2, 3], context="test")
    assert dropped == 3
    sql, params = conn.executed[0]
    assert "closed_reason = 'below_density'" in sql
    assert "DELETE" not in sql
    assert params == ([1, 2, 3],)
    assert conn.commits == 1


def test_drop_below_density_empty_short_circuits_no_query():
    conn = StubConn()
    assert drop_below_density_refold_rows(conn, []) == 0
    assert conn.executed == []


# ── I16 — due-ness counts DISTINCT open pg_id ─────────────────────────────────

def test_fetch_refold_backlog_reads_distinct_open_rows():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(1,), (2,)]}])
    assert fetch_refold_backlog(conn) == [1, 2]
    sql, _ = conn.executed[0]
    assert "DISTINCT pg_id" in sql
    assert "status = 'open'" in sql


def test_fetch_combined_fact_backlog_dedupes_across_both_sources(monkeypatch):
    monkeypatch.setattr(cl, "fetch_ledger_backlog", lambda conn: [1, 2, 3])
    monkeypatch.setattr(cl, "fetch_refold_backlog", lambda conn: [3, 4])
    assert fetch_combined_fact_backlog(None) == [1, 2, 3, 4]
