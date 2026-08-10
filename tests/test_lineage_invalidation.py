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
import inspect
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
    drop_out_of_scan_refold_rows,
    fetch_combined_fact_backlog,
    fetch_invalidated_summaries,
    fetch_refold_backlog,
    resolve_standing_ids,
    retire_invalidated_summaries,
    supersede_covered_summaries,
)

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "shared-memory", "migrations")


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


@pytest.mark.asyncio
async def test_leg3_cascades_using_the_real_summary_ids_c4s_fold_actually_writes():
    """Criterion F, end to end across BOTH modules — never exercised until
    C4, because `summary_ids` did not exist as a written field before it.

    Step 1: run the REAL `ConsolidationDaemon._fold_insight` (from
    `test_insight_consolidation`'s own stub conventions) with
    `summary_ids=[173]` and capture the ACTUAL JSON string it writes to
    `community_summaries.metadata`.
    Step 2: feed the `summary_ids` value FROM THAT REAL OUTPUT into a
    StubConn simulating `fetch_invalidated_summaries`'s leg 3 (which reads
    `jsonb_array_elements_text(metadata->'summary_ids')`), proving the exact
    thing the fold writes is the exact thing leg 3 can find — not a
    hand-typed approximation of either side.
    """
    import json as _json
    from unittest.mock import AsyncMock, MagicMock

    class _StubCursor:
        def __init__(self, script, executed):
            self._script, self.executed = script, executed
            self._current = {"rowcount": 0, "rows": []}
        def execute(self, sql, params=None):
            self.executed.append((" ".join(sql.split()), params))
            self._current = self._script.pop(0) if self._script else {"rowcount": 0, "rows": []}
        def fetchall(self): return self._current["rows"]
        def fetchone(self):
            rows = self._current["rows"]
            return rows[0] if rows else None
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _StubConn:
        def __init__(self, script=None):
            self._script, self.executed = script or [], []
            self.commits = 0
        def cursor(self): return _StubCursor(self._script, self.executed)
        def commit(self): self.commits += 1
        def rollback(self): pass

    class _AsyncCtx:
        def __init__(self, val): self._val = val
        async def __aenter__(self): return self._val
        async def __aexit__(self, *_): pass

    daemon = ConsolidationDaemon()
    daemon.driver = MagicMock()
    daemon.driver.session = MagicMock(return_value=_AsyncCtx(MagicMock(
        run=AsyncMock(return_value=MagicMock()))))
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    fold_conn = _StubConn(script=[
        {"rowcount": 2, "rows": [
            (245, "Decision A\n\nrationale", "shared-memory-GitHub", "decision", {}),
            (267, "Decision B\n\nrationale", "shared-memory-GitHub", "decision", {}),
        ]},
        {"rowcount": 0, "rows": []},                          # reversal leg 1
        {"rowcount": 2, "rows": [(101,), (102,)]},             # outbox snapshot
        {"rowcount": 1, "rows": [(77,)]},                      # INSERT
        {"rowcount": 2, "rows": []},                           # outbox flip
        {"rowcount": 0, "rows": []},                           # supersession SELECT
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},     # close
    ])
    import os
    os.environ["MOCK_LLM"] = "1"
    try:
        ok = await daemon._fold_insight(
            fold_conn, "OutboxPattern", [245, 267], summary_ids=[173])
    finally:
        os.environ.pop("MOCK_LLM", None)
    assert ok is True

    insert_sql, insert_params = next(
        (s, p) for s, p in fold_conn.executed if s.startswith("INSERT INTO community_summaries"))
    written_meta = _json.loads(insert_params[1])
    assert written_meta["summary_ids"] == [173]      # what the fold ACTUALLY wrote

    # Step 2 — leg 3 with that REAL value as its trigger-match input. The
    # fold wrote summary_ids=[173]; leg 1 must retire thematic summary 173
    # specifically for leg 3 to fire on THIS insight.
    leg_conn = StubConn(script=[
        {"rowcount": 1, "rows": [(173, [900, 901], 3)]},                      # leg1 retires summary 173
        {"rowcount": 0, "rows": []},                                            # leg2
        {"rowcount": 1, "rows": [(77, written_meta["source_pg_ids"], 173)]}, # leg3 — insight 77 cites 173
    ])
    out = fetch_invalidated_summaries(leg_conn)
    assert {"summary_id": 77, "source_pg_ids": [245, 267], "kind": "insight",
           "trigger_kind": "community_summaries", "trigger_id": 173} in out


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


# ── I17 — the FACT clock never counts an insight-kind ledger row ──────────────
# Found in review of the C3 delivery, not by the build. §5's amendment says the
# ledger is the clock FOR THE FACT PATH and that "the insight path needs no
# clock work at all". An insight-kind row carries a decision/retrospective
# pg_id; on the fact path that value can never be consumed (the density check
# means nothing for it), never be dropped (drop_below_density_refold_rows reads
# _find_grounded_fact_groups' FACT scan, which never yields a decision id), and
# only ever closes if a later insight fold happens to cover it. It is an
# attribution row, not a clock entry.

def test_i17_fact_backlog_excludes_insight_kind_refold_rows():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(7,)]}])
    assert fetch_refold_backlog(conn) == [7]
    sql, _ = conn.executed[0]
    assert "summary_kind = 'thematic'" in sql, (
        "an insight-kind row carries a decision pg_id and would inflate the "
        "FACT density clock forever — nothing on that path can ever close it"
    )
    assert "status = 'open'" in sql
    assert "DISTINCT pg_id" in sql


# ── The retirement stamp is a PAIR — both columns, on BOTH mechanisms ─────────

def test_coverage_retirement_stamps_superseded_at_not_only_the_reason():
    # Mechanism A (subset coverage). Migration 031 defines superseded_at +
    # superseded_reason as one stamp; writing the reason alone makes a coverage
    # retirement indistinguishable from a pre-031 row to "what was retired
    # since the stamp existed?" — the only question the pair answers.
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(9, [1], None, "thematic")]},   # candidate scan
        {"rowcount": 1, "rows": []},                              # UPDATE
    ])
    assert supersede_covered_summaries(conn, 10, [1, 2]) == [9]
    update_sql, params = conn.executed[1]
    assert "superseded_reason = 'coverage'" in update_sql
    assert "superseded_at = now()" in update_sql, (
        "the lineage path stamps both columns; coverage must too, or the pair "
        "reports only half the retirements it exists to explain"
    )
    assert params == (9,)


# ── C3.1 F0 — the resurrection gap: migration 032 + the ON CONFLICT arbiter ──
#
# Migration 029's axis unique index does not exclude superseded rows, and the
# thematic fold's INSERT ... ON CONFLICT ... DO UPDATE never touches
# superseded/superseded_at/superseded_reason. A lineage-retired (project,
# domain) row is therefore UPDATED IN PLACE by the next fold on that axis key
# and stays superseded forever. Migration 032 rebuilds the index with
# "AND NOT superseded"; the ON CONFLICT arbiter in _write_summary
# (`ConsolidationDaemon._consolidate_clusters`) must carry the SAME added
# predicate, or the index and the arbiter disagree and the INSERT raises a
# "no unique or exclusion constraint matching the ON CONFLICT specification"
# error the moment a fold hits a retired row on that axis key.

def test_f0_migration_032_rebuilds_the_axis_index_excluding_superseded():
    """MUTATION-CHECKED: removing "AND NOT superseded" from the CREATE UNIQUE
    INDEX statement below (restoring migration 029's predicate verbatim)
    makes this test fail. Restored after."""
    with open(os.path.join(_MIGRATIONS_DIR,
                            "032_axis_index_excludes_superseded.sql"),
              encoding="utf-8") as f:
        sql = " ".join(f.read().split())

    assert "BEGIN;" in sql and "COMMIT;" in sql, (
        "the drop + recreate must be one transaction — a crash between them "
        "would leave the axis key with NO unique index at all"
    )
    assert "DROP INDEX IF EXISTS community_summaries_axis_level_unique" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_axis_level_unique" in sql
    # Idempotent — safe to re-run against a database that already has it.
    create_clause = sql.split("CREATE UNIQUE INDEX IF NOT EXISTS", 1)[1]
    assert "COALESCE(metadata->>'kind', 'thematic') <> 'insight'" in create_clause
    assert "AND NOT superseded" in create_clause, (
        "the index must exclude superseded rows or a retired row on the same "
        "axis key keeps blocking a fresh INSERT from ever landing"
    )


def test_f0_write_summary_on_conflict_arbiter_excludes_superseded():
    """MUTATION-CHECKED: deleting the "AND NOT superseded" line from the
    ON CONFLICT WHERE clause in `_consolidate_clusters` (reverting to the
    v0.8.67 predicate) makes this test fail. Restored after.

    The index (migration 032) and the ON CONFLICT arbiter must name the
    IDENTICAL predicate — Postgres matches an ON CONFLICT target against an
    existing index definition exactly; a mismatch either fails to match (raising
    "no unique or exclusion constraint matching") or, worse, silently matches
    the WRONG index if one without the predicate still existed."""
    source = inspect.getsource(ConsolidationDaemon._consolidate_clusters)
    on_conflict = source.split("ON CONFLICT (", 1)[1]
    on_conflict_where = on_conflict.split("DO UPDATE", 1)[0]
    assert "COALESCE(metadata->>'kind', 'thematic') <> 'insight'" in on_conflict_where
    assert "AND NOT superseded" in on_conflict_where, (
        "the arbiter must match migration 032's index predicate exactly, or "
        "a retired row on the same axis key still blocks the fresh INSERT"
    )


# ── C3.1 F1 — unclosable clock rows: out-of-scan close ───────────────────────
#
# below_density_ids = pg_ids_all - all_member_ids can only ever name a pg_id
# that was ALREADY a member of pg_ids_all (the grounded+domained scan). A
# constituent that is ungrounded or domainless never enters pg_ids_all at
# all, so drop_below_density_refold_rows structurally cannot reach it — its
# open thematic-kind ledger row is a permanent zombie. drop_out_of_scan_
# refold_rows closes that class separately, with a distinct closed_reason.

def test_f1_drop_out_of_scan_closes_rows_never_seen_by_the_scan():
    conn = StubConn(script=[{"rowcount": 2, "rows": []}])
    dropped = drop_out_of_scan_refold_rows(conn, [1, 2, 3], context="test")
    assert dropped == 2
    sql, params = conn.executed[0]
    assert "closed_reason = 'out_of_scan'" in sql
    assert "summary_kind = 'thematic'" in sql, (
        "out-of-scan closing is a FACT-path concept only — an insight-kind "
        "row is never scanned by _find_grounded_fact_groups in the first "
        "place, so it must never be touched here (I17)"
    )
    assert "DELETE" not in sql, "close, never delete (migration 031's model)"
    assert params == ([1, 2, 3],)
    assert conn.commits == 1


def test_f1_drop_out_of_scan_reason_is_distinct_from_below_density():
    """The two zombie classes must stay tellable apart in telemetry —
    same-shaped closes with different closed_reason strings."""
    conn_a = StubConn(script=[{"rowcount": 1, "rows": []}])
    drop_below_density_refold_rows(conn_a, [7], context="test")
    below_sql, _ = conn_a.executed[0]

    conn_b = StubConn(script=[{"rowcount": 1, "rows": []}])
    drop_out_of_scan_refold_rows(conn_b, [7], context="test")
    scan_sql, _ = conn_b.executed[0]

    assert "closed_reason = 'below_density'" in below_sql
    assert "closed_reason = 'out_of_scan'" in scan_sql
    assert below_sql != scan_sql


def test_f1_consolidate_clusters_calls_out_of_scan_close_with_full_scan_set():
    """MUTATION-CHECKED: deleting the `drop_out_of_scan_refold_rows` call
    from `_consolidate_clusters` makes this test fail. Restored after.

    Composition check, not just the unit above: the call site must exist and
    must pass `pg_ids_all` — the full cycle scan — never `all_member_ids`
    (the already-gating subset `below_density_ids` is drawn from), or the
    out-of-scan close would just re-derive the below-density set instead of
    reaching the population it exists to cover."""
    source = inspect.getsource(ConsolidationDaemon._consolidate_clusters)
    assert "drop_out_of_scan_refold_rows(" in source
    call = source.split("drop_out_of_scan_refold_rows(", 1)[1].split(")", 1)[0]
    assert "pg_ids_all" in call
    assert "all_member_ids" not in call
    assert "below_density_ids" not in call


# ── C3.1 F2 — false 'refolded' attribution: the recency predicate ───────────
#
# close_refold_ledger_rows' 'refolded' branch matched ANY active summary
# containing the pg_id, including one that PREDATES the ledger row — closing
# it 'constituent_folded' when nothing was actually folded. The UPSERT sets
# updated_at = now() on every real fold and a fresh INSERT defaults both
# columns together, so requiring the covering summary's
# COALESCE(updated_at, created_at) >= the ledger row's created_at excludes
# exactly the summaries that could not have been the re-fold being waited on.

def test_f2_refolded_close_requires_covering_summary_no_older_than_the_row():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": []},   # refolded UPDATE
        {"rowcount": 0, "rows": []},   # dropped/constituent_superseded UPDATE
    ])
    close_refold_ledger_rows(conn, context="test")
    refolded_sql, _ = conn.executed[0]
    assert "COALESCE(cs.updated_at, cs.created_at) >= o.created_at" in refolded_sql, (
        "MUTATION-CHECKED: removing this predicate (reverting to the shipped "
        "v0.8.67 query) makes this test fail — restored after. Without it a "
        "summary that predates the ledger row can close it 'constituent_folded' "
        "with nothing having actually folded (measured: fact 1149)"
    )
    # Still kind-scoped (I13/U5) and still checks non-superseded — the new
    # predicate is additive, not a replacement of the existing guards.
    assert "NOT cs.superseded" in refolded_sql
    assert "o.summary_kind = 'thematic'" in refolded_sql
    assert "o.summary_kind = 'insight'" in refolded_sql
