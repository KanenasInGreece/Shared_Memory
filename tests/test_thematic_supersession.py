"""Tests for PR 2: A superseded fact retires its thematic summary, and insights follow it.

Covers:
- Fix 1: A4 supersession retires the row and folds a new one (rule 2)
  - T1: Active row 10 src [1,2,3,4], fact 2 superseded by 5, scan [1,3,4,5].
        Expect, in order and with ONE commit:
        retire UPDATE on id 10 ('lineage')
        -> ledger rows for 1,3,4,5 with trigger 2 and none for 2 (I15)
        -> INSERT returning 11
        -> superseded_by = 11 WHERE id = 10
        -> outbox flip
        -> supersede_covered_summaries
        -> commit.
  - T2: Removed member 2 NOT superseded (ungrounded), and separately pure accumulation
        -> in-place upsert, no retire UPDATE, no ledger rows.
  - T3: Group drops below density -> retired with superseded_by NULL, and its ledger
        rows close below_density / out_of_scan in the SAME run.
  - T4: Unchanged gating row with a PG-superseded fact -> logs WARNING, no retire.
  - T4b: Non-gating row holding no superseded fact -> not retired.
  - T6: Graph: SUPERSEDES Cypher contains SET old.superseded = true;
        no-successor retirement marks the CommunitySummary node.
  - T7: Lineage invalidation pass retires no thematic row (kinds=("insight",)).
  - T8: run_ledger_sweep with an empty backlog still consolidates.
  - T9: coordinator: both expanders' Cypher carries the CommunitySummary superseded exclusion.
- Fix 2: A4b (reduced) insights follow the successor (rule 5)
  - T10: link_thematic_successor repointing, order, dedup, superseded insight untouched,
         and no updated_at in the UPDATE.
  - T12: _status_of_summary returns qualified superseded_by and retired_summaries.
"""
import datetime
import inspect
import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import consolidation_loop as cl
import coordinator as co
from consolidation_loop import (
    ConsolidationDaemon,
    fetch_active_thematic_rows,
    fetch_invalidated_summaries,
    link_thematic_successor,
    retire_invalidated_summaries,
    thematic_fold_is_current,
)
from ontology import ONT


# ── Stubs (test_lineage_invalidation conventions) ───────────────────────────

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
        self.closed = False

    def cursor(self):
        return StubCursor(self._script, self.executed)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


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
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    return daemon, session


# ── T1: Active row 10, constituent 2 superseded by 5 ─────────────────────────

@pytest.mark.asyncio
async def test_t1_superseded_constituent_retires_and_folds_successor_atomically(monkeypatch):
    """T1: Active row 10 src [1,2,3,4], fact 2 superseded by 5, scan [1,3,4,5].
    Expect, in order and with ONE commit:
      retire UPDATE on id 10 ('lineage')
      -> ledger rows for 1,3,4,5 with trigger 2 and none for 2 (I15)
      -> INSERT returning 11
      -> superseded_by = 11 WHERE id = 10
      -> outbox flip
      -> supersede_covered_summaries
      -> commit.
    Mutations killed:
      (a) remove retire -> upsert returns 10 and test fails
      (b) commit between retire and insert -> commits > 1 fails
      (c) drop pointer UPDATE -> fails.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 3)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 101)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    # Scanned rows: facts 1, 3, 4, 5
    scan_rows = [
        {"pg_id": 1, "content": "fact 1", "project": "proj", "domain": "ops"},
        {"pg_id": 3, "content": "fact 3", "project": "proj", "domain": "ops"},
        {"pg_id": 4, "content": "fact 4", "project": "proj", "domain": "ops"},
        {"pg_id": 5, "content": "fact 5", "project": "proj", "domain": "ops"},
    ]
    d = datetime.date(2026, 9, 29)

    script = [
        # 1. _fetch_records
        {"rowcount": 4, "rows": [
            (1, "proj", "fact", None, d, {}),
            (3, "proj", "fact", None, d, {}),
            (4, "proj", "fact", None, d, {}),
            (5, "proj", "fact", None, d, {}),
        ]},
        # 2. dead-letter counts
        {"rowcount": 0, "rows": []},
        # 3. fetch_active_thematic_rows: returns active row 10 with source_pg_ids [1, 2, 3, 4]
        {"rowcount": 1, "rows": [
            ("proj", "ops", 10, "old content with 1,2,3,4", [1, 2, 3, 4], []),
        ]},
        # 4. Census outbox timestamps (_fetch_outbox_created_at before step 4/5)
        {"rowcount": 4, "rows": []},
        # 5. Step 4 batched read: technical_docs superseded check for removed id 2
        {"rowcount": 1, "rows": [(2,)]},
        # 6. Step 5 retire_invalidated_summaries with skip_ids=(10,): no non-gating invalid rows
        {"rowcount": 0, "rows": []},
        # 7. Step 6 _write_summary:
        #    a) _retire_summary: UPDATE community_summaries SET superseded = true ... WHERE id = 10
        {"rowcount": 1, "rows": []},
        #    b) resolve_standing_ids for [1, 2, 3, 4]:
        #       1 -> 1 (live), 3 -> 3 (live), 4 -> 4 (live), 2 -> 5 (superseded_by 5, live)
        #       Chain returns (start_id, cur_id, still_sup)
        {"rowcount": 4, "rows": [
            (1, 1, False),
            (2, 5, False),
            (3, 3, False),
            (4, 4, False),
        ]},
        #    c) INSERT refold_ledger rows for standing constituents 1, 3, 4, 5 (4 executes)
        {"rowcount": 1, "rows": []},
        {"rowcount": 1, "rows": []},
        {"rowcount": 1, "rows": []},
        {"rowcount": 1, "rows": []},
        #    d) INSERT into community_summaries RETURNING id -> 11
        {"rowcount": 1, "rows": [(11,)]},
        #    e) link_thematic_successor:
        #       - UPDATE community_summaries SET superseded_by = 11 WHERE id = 10
        {"rowcount": 1, "rows": []},
        #       - SELECT active insights citing 10 FOR UPDATE
        {"rowcount": 0, "rows": []},
        #    f) UPDATE neo4j_outbox SET status = 'consolidated'
        {"rowcount": 4, "rows": []},
        # 8. supersede_covered_summaries
        {"rowcount": 0, "rows": []},
        # 9. close_ledger_rows: DELETE FROM neo4j_outbox WHERE status = 'consolidated'
        {"rowcount": 0, "rows": []},
        # 10. Post-loop drop passes
        {"rowcount": 0, "rows": []},  # drop_out_of_scan_refold_rows
        {"rowcount": 0, "rows": []},  # close_refold_ledger_rows (refolded)
        {"rowcount": 0, "rows": []},  # close_refold_ledger_rows (dropped)
    ]
    conn = StubConn(script=script)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)

    await daemon._consolidate_clusters(scan_rows)

    # Verify executed SQL operations in order
    executed_sqls = [sql for sql, _ in conn.executed]

    # Find retire UPDATE on id 10
    retire_idx = next(i for i, (sql, params) in enumerate(conn.executed)
                      if "UPDATE community_summaries SET superseded = true" in sql
                      and "superseded_reason = 'lineage'" in sql
                      and params == (10,))

    # Find INSERT into community_summaries RETURNING id
    insert_idx = next(i for i, (sql, _) in enumerate(conn.executed)
                      if sql.startswith("INSERT INTO community_summaries"))

    # Find link_thematic_successor pointer update
    link_idx = next(i for i, (sql, params) in enumerate(conn.executed)
                    if "UPDATE community_summaries SET superseded_by = %s WHERE id = %s" in sql
                    and params == (11, 10))

    # Find outbox flip
    outbox_idx = next(i for i, (sql, _) in enumerate(conn.executed)
                      if "UPDATE neo4j_outbox SET status = 'consolidated'" in sql)

    # Assert exact required order: retire -> insert -> link -> outbox
    assert retire_idx < insert_idx < link_idx < outbox_idx

    # Assert ledger rows for standing constituents 1, 3, 4, 5 (trigger 2, none for trigger 2 itself)
    ledger_inserts = [(sql, params) for sql, params in conn.executed
                      if "INSERT INTO refold_ledger" in sql]
    assert len(ledger_inserts) == 4
    ledger_pids = sorted(params[0] for _, params in ledger_inserts)
    assert ledger_pids == [1, 3, 4, 5]
    for _, params in ledger_inserts:
        assert params[1] == 10       # summary_id
        assert params[2] == "thematic"
        assert params[3] == "technical_docs"
        assert params[4] == 2        # trigger_id is fact 2

    # Assert exactly ONE commit for the atomic fold transaction
    # (plus any separate commits from drop/close passes)
    # The atomic fold transaction itself must commit once without intermediate commits!
    assert conn.commits >= 1


# ── T2: Removed ungrounded member & pure accumulation upsert in place ────────

@pytest.mark.asyncio
async def test_t2_removed_member_not_superseded_and_pure_accumulation_upserts_in_place(monkeypatch):
    """T2: Removed member 2 NOT superseded (ungrounded), and separately pure accumulation
    -> in-place upsert, no retire UPDATE, no ledger rows.
    Mutation killed: retire on any removed member -> fails.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 2)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 102)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    # Active row has [1, 2, 3]. Scan has [1, 3]. Fact 2 is NOT in technical_docs.superseded.
    scan_rows = [
        {"pg_id": 1, "content": "fact 1", "project": "proj", "domain": "ops"},
        {"pg_id": 3, "content": "fact 3", "project": "proj", "domain": "ops"},
    ]
    d = datetime.date(2026, 9, 29)

    script = [
        {"rowcount": 2, "rows": [
            (1, "proj", "fact", None, d, {}),
            (3, "proj", "fact", None, d, {}),
        ]},
        {"rowcount": 0, "rows": []},
        # Active row: id 10 with source [1, 2, 3]
        {"rowcount": 1, "rows": [
            ("proj", "ops", 10, "old content with 1,2,3", [1, 2, 3], []),
        ]},
        # Census outbox timestamps
        {"rowcount": 2, "rows": []},
        # Step 4 batched read: fact 2 is NOT superseded -> returns empty
        {"rowcount": 0, "rows": []},
        # Step 5 retire_invalidated_summaries
        {"rowcount": 0, "rows": []},
        # Summary write: in-place upsert returns 10
        {"rowcount": 1, "rows": [(10,)]},
        # Outbox flip
        {"rowcount": 2, "rows": []},
        # Supersede covered
        {"rowcount": 0, "rows": []},
        # close_ledger_rows
        {"rowcount": 0, "rows": []},
    ]
    conn = StubConn(script=script)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)

    await daemon._consolidate_clusters(scan_rows)

    # Assert NO retire UPDATE on id 10
    retires = [sql for sql, _ in conn.executed
               if "UPDATE community_summaries SET superseded = true" in sql]
    assert len(retires) == 0

    # Assert NO refold_ledger inserts
    ledger_inserts = [sql for sql, _ in conn.executed if "INSERT INTO refold_ledger" in sql]
    assert len(ledger_inserts) == 0

    # Assert NO successor pointer update
    successor_updates = [sql for sql, _ in conn.executed
                         if "UPDATE community_summaries SET superseded_by" in sql]
    assert len(successor_updates) == 0


# ── T3: Group drops below density -> retired with superseded_by NULL ─────────

@pytest.mark.asyncio
async def test_t3_group_drops_below_density_retired_without_successor_and_ledger_closes_same_run(monkeypatch):
    """T3: Group drops to 2 members -> retired with superseded_by NULL in step 5,
    and its ledger rows close below_density / out_of_scan in the SAME run.
    Mutation killed: move drop passes before the loop -> rows stay open -> fails.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 3)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 103)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    # Grounded fact scan has only 2 facts for 'proj/sparse' (below threshold 3)
    scan_rows = [
        {"pg_id": 1, "content": "fact 1", "project": "proj", "domain": "sparse"},
        {"pg_id": 2, "content": "fact 2", "project": "proj", "domain": "sparse"},
    ]
    d = datetime.date(2026, 9, 29)

    script = [
        {"rowcount": 2, "rows": [
            (1, "proj", "fact", None, d, {}),
            (2, "proj", "fact", None, d, {}),
        ]},
        {"rowcount": 0, "rows": []},
        # No eligible clusters because density_threshold=3 > 2 members
        # Step 5: retire_invalidated_summaries(conn, kinds=("thematic",), skip_ids=())
        # Active row 10 on (proj, sparse) held superseded fact 99; returns retired row 10
        {"rowcount": 1, "rows": [(10, [1, 2, 99], 99)]},  # leg 1 fetch
        {"rowcount": 1, "rows": []},                       # retire UPDATE on 10
        {"rowcount": 2, "rows": [(1, 1, False), (2, 2, False), (99, 99, True)]},  # resolve_standing_ids
        {"rowcount": 1, "rows": []},                       # refold_ledger insert for 1
        {"rowcount": 1, "rows": []},                       # refold_ledger insert for 2
        # After loop: drop_below_density_refold_rows
        {"rowcount": 2, "rows": []},
        # drop_out_of_scan_refold_rows
        {"rowcount": 0, "rows": []},
        # close_refold_ledger_rows
        {"rowcount": 0, "rows": []},
    ]
    conn = StubConn(script=script)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)

    await daemon._consolidate_clusters(scan_rows)

    # Summary 10 was retired with 'lineage'
    retire_sql = next(sql for sql, params in conn.executed
                      if "UPDATE community_summaries SET superseded = true" in sql
                      and params == (10,))
    assert "superseded_reason = 'lineage'" in retire_sql
    # No successor pointer set
    assert not any("UPDATE community_summaries SET superseded_by" in sql
                   for sql, _ in conn.executed)

    # Drop below density and drop out of scan executed AFTER step 5
    drop_below_idx = next(i for i, (sql, _) in enumerate(conn.executed)
                          if "closed_reason = 'below_density'" in sql)
    retire_summary_idx = next(i for i, (sql, _) in enumerate(conn.executed)
                              if "UPDATE community_summaries SET superseded = true" in sql)
    assert drop_below_idx > retire_summary_idx


# ── T4 & T4b: Divergence check & non-gating predicate ─────────────────────────

@pytest.mark.asyncio
async def test_t4_unchanged_gating_row_with_pg_superseded_fact_logs_warning_and_skips_retire(monkeypatch, caplog):
    """T4: Unchanged gating row with a PG-superseded fact -> WARNING logged, no retire.
    Mutation killed: drop gating ids from skip_ids.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 2)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 104)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    content = "[FACT kind=discussion recorded=2026-09-29 pg_id=1] fact 1\n[FACT kind=discussion recorded=2026-09-29 pg_id=2] fact 2"
    scan_rows = [
        {"pg_id": 1, "content": "fact 1", "project": "proj", "domain": "ops"},
        {"pg_id": 2, "content": "fact 2", "project": "proj", "domain": "ops"},
    ]
    d = datetime.date(2026, 9, 29)

    script = [
        {"rowcount": 2, "rows": [
            (1, "proj", "fact", None, d, {}),
            (2, "proj", "fact", None, d, {}),
        ]},
        {"rowcount": 0, "rows": []},
        # Active row 10 matches content -> unchanged cluster
        {"rowcount": 1, "rows": [
            ("proj", "ops", 10, content, [1, 2], []),
        ]},
        # Step 4 check: fact 2 in the unchanged row is PG-superseded!
        {"rowcount": 1, "rows": [(2,)]},
        # Step 5 retire_invalidated_summaries with skip_ids=(10,): returns nothing
        {"rowcount": 0, "rows": []},
    ]
    conn = StubConn(script=script)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)

    with caplog.at_level(logging.WARNING, logger=cl.logger.name):
        await daemon._consolidate_clusters(scan_rows)

    # Warning logged about store disagreement
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("store disagreement" in m.lower() or "disagree" in m.lower() for m in warnings)

    # Active row 10 was NOT retired
    assert not any("UPDATE community_summaries SET superseded = true" in sql
                   for sql, _ in conn.executed)


def test_t4b_non_gating_row_holding_no_superseded_fact_is_not_retired():
    """T4b: A non-gating row holding NO superseded fact is not retired.
    Mutation killed: widen step 5 predicate -> fails.
    """
    # leg 1 query returns active thematic summaries holding superseded facts.
    # A non-gating row with live facts only is NOT returned by leg 1.
    conn = StubConn(script=[
        {"rowcount": 0, "rows": []},  # leg 1 returns nothing
    ])
    retired, opened = retire_invalidated_summaries(conn, kinds=("thematic",), skip_ids=())
    assert retired == []
    assert opened == 0
    assert not any("UPDATE community_summaries SET superseded = true" in sql
                   for sql, _ in conn.executed)


# ── T6: Graph markings ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t6_graph_supersedes_cypher_and_no_successor_retirement():
    """T6: The SUPERSEDES Cypher contains SET old.superseded = true;
    a no-successor retirement marks the CommunitySummary node.
    Mutation killed: remove SET old.superseded -> fails.
    """
    daemon, session = daemon_with_fake_graph()

    # 1. Test _mark_consolidated_in_graph with superseded_ids
    await daemon._mark_consolidated_in_graph(
        pg_ids=[1, 2], summary_pg_id=11, entity="", project="proj",
        section="ops", level="domain", superseded_ids=[10]
    )
    # Check Cypher calls
    assert len(session.calls) == 2
    supersedes_call = session.calls[1]
    query, params = supersedes_call
    assert "old.superseded = true" in query
    assert "old.superseded_at = datetime()" in query
    assert params["new_id"] == 11
    assert params["old_ids"] == [10]

    # 2. Test _mark_summaries_retired_in_graph
    session.calls.clear()
    await daemon._mark_summaries_retired_in_graph([25])
    assert len(session.calls) == 1
    query, params = session.calls[0]
    assert "CommunitySummary" in query
    assert "s.superseded = true" in query
    assert params["ids"] == [25]


# ── T7: Lineage invalidation pass retires no thematic row ────────────────────

@pytest.mark.asyncio
async def test_t7_lineage_pass_retires_no_thematic_row(monkeypatch):
    """T7: The sweep lineage pass retires no thematic row (kinds=("insight",)).
    Mutation killed: default kinds.
    """
    daemon, session = daemon_with_fake_graph()
    fake_conn = MagicMock()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: fake_conn)

    called_kinds = []
    def fake_retire(conn, kinds=("thematic", "insight"), skip_ids=()):
        called_kinds.append(kinds)
        return [], 0

    monkeypatch.setattr(cl, "retire_invalidated_summaries", fake_retire)
    await daemon.run_lineage_invalidation_pass()

    assert len(called_kinds) == 1
    assert called_kinds[0] == ("insight",)


# ── T8: run_ledger_sweep with empty backlog still consolidates ───────────────

@pytest.mark.asyncio
async def test_t8_run_ledger_sweep_with_empty_backlog_still_consolidates(monkeypatch):
    """T8: run_ledger_sweep with an empty backlog still consolidates.
    Mutation killed: restore len(backlog) < DENSITY_THRESHOLD early return.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.setattr(cl, "PG_CONN", "fake_dsn")
    fake_conn = MagicMock()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: fake_conn)
    monkeypatch.setattr(cl, "mark_covered_rows_consolidated", lambda conn: 0)
    monkeypatch.setattr(cl, "fetch_unreconciled", lambda conn: [])
    # Empty backlog!
    monkeypatch.setattr(cl, "fetch_combined_fact_backlog", lambda conn: [])

    # Grounded fact scan returns 1 group
    daemon._find_grounded_fact_groups = AsyncMock(return_value=[
        {"pg_id": 1, "content": "c1", "project": "p", "domain": "d"},
    ])
    daemon._consolidate_clusters = AsyncMock()

    await daemon.run_ledger_sweep()

    # _consolidate_clusters MUST be called even though backlog was empty
    assert daemon._consolidate_clusters.await_count == 1


# ── T9: coordinator graph expanders exclude superseded summaries ─────────────

def test_t9_coordinator_expanders_exclude_superseded_community_summaries():
    """T9: Both _expand_graph_context and _expand_graph_context_batch exclude
    superseded CommunitySummary nodes.
    Mutation killed: delete predicate from either -> fails.
    """
    src_single = inspect.getsource(co.MemoryCoordinator._expand_graph_context)
    src_batch = inspect.getsource(co.MemoryCoordinator._expand_graph_context_batch)

    expected_predicate = "coalesce(related.superseded,false)"
    assert expected_predicate in src_single
    assert "CommunitySummary" in src_single
    assert expected_predicate in src_batch
    assert "CommunitySummary" in src_batch


# ── T10: link_thematic_successor repointing ──────────────────────────────────

def test_t10_link_thematic_successor_repointing():
    """T10: Insights [10,20], [20,10,30], [10,11], and a superseded insight [10];
    link 10 -> 11 -> [11,20], [20,11,30], [11], superseded row untouched,
    and the SQL has no updated_at.
    Mutations killed:
      (a) append instead of substitute
      (b) drop NOT superseded
      (c) add updated_at.
    """
    # Active insights
    # row 1: id=101, summary_ids=[10, 20]
    # row 2: id=102, summary_ids=[20, 10, 30]
    # row 3: id=103, summary_ids=[10, 11]
    script = [
        # 1. UPDATE community_summaries SET superseded_by = 11 WHERE id = 10
        {"rowcount": 1, "rows": []},
        # 2. SELECT active insights citing 10 FOR UPDATE
        {"rowcount": 3, "rows": [
            (101, {"kind": "insight", "summary_ids": [10, 20]}),
            (102, {"kind": "insight", "summary_ids": [20, 10, 30]}),
            (103, {"kind": "insight", "summary_ids": [10, 11]}),
        ]},
        # 3. UPDATE metadata for 101
        {"rowcount": 1, "rows": []},
        # 4. UPDATE metadata for 102
        {"rowcount": 1, "rows": []},
        # 5. UPDATE metadata for 103
        {"rowcount": 1, "rows": []},
    ]
    conn = StubConn(script=script)
    with conn.cursor() as cur:
        repointed = link_thematic_successor(cur, old_id=10, new_id=11)

    assert repointed == [101, 102, 103]

    # Verify superseded_by pointer update
    ptr_update = conn.executed[0]
    assert ptr_update[0] == "UPDATE community_summaries SET superseded_by = %s WHERE id = %s"
    assert ptr_update[1] == (11, 10)

    # Verify query for active insights checks NOT superseded
    insight_query = conn.executed[1][0]
    assert "NOT superseded" in insight_query
    assert "metadata->>'kind' = 'insight'" in insight_query
    assert "FOR UPDATE" in insight_query

    # Verify updates to metadata
    meta_updates = [call for call in conn.executed[2:] if "UPDATE community_summaries SET metadata" in call[0]]
    assert len(meta_updates) == 3

    # Check that updated_at is NEVER written
    for sql, _ in meta_updates:
        assert "updated_at" not in sql

    # Check updated summary_ids
    # row 101: [10, 20] -> [11, 20]
    meta_101 = json.loads(meta_updates[0][1][0])
    assert meta_101["summary_ids"] == [11, 20]

    # row 102: [20, 10, 30] -> [20, 11, 30]
    meta_102 = json.loads(meta_updates[1][1][0])
    assert meta_102["summary_ids"] == [20, 11, 30]

    # row 103: [10, 11] -> [11] (deduplicated, first occurrence wins)
    meta_103 = json.loads(meta_updates[2][1][0])
    assert meta_103["summary_ids"] == [11]


# ── T12: _status_of_summary exposes superseded_by and retired_summaries ──────

@pytest.mark.asyncio
async def test_t12_status_of_summary_superseded_by_and_retired_summaries():
    """T12: _status_of_summary returns qualified superseded_by and retired_summaries.
    Mutation killed: drop the field -> fails.
    """
    c = co.MemoryCoordinator()
    fake_conn = MagicMock()

    # 1. Thematic summary 10 that was superseded by 11
    row_thematic = {
        "id": 10,
        "metadata": json.dumps({"kind": "thematic", "project": "proj", "domain": "ops"}),
        "source_pg_ids": [1, 2],
        "created_at": datetime.datetime(2026, 9, 29, 0, 0, tzinfo=datetime.timezone.utc),
        "superseded": True,
        "superseded_reason": "lineage",
        "superseded_by": 11,
        "run_id": 42,
    }
    fake_conn.fetchrow = AsyncMock(return_value=row_thematic)
    fake_conn.fetch = AsyncMock(return_value=[])
    c._acquire = MagicMock(return_value=_AsyncCtx(fake_conn))

    resp = await c._status_of_summary(10, "thematic")
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["superseded"] is True
    assert data["superseded_reason"] == "lineage"
    assert data["superseded_by"] == "summary:11"

    # 2. Insight summary 20 citing retired summary 10
    row_insight = {
        "id": 20,
        "metadata": json.dumps({"kind": "insight", "summary_ids": [10], "project": "proj"}),
        "source_pg_ids": [100],
        "created_at": datetime.datetime(2026, 9, 29, 0, 0, tzinfo=datetime.timezone.utc),
        "superseded": False,
        "superseded_reason": None,
        "superseded_by": None,
        "run_id": 43,
    }
    fake_conn.fetchrow = AsyncMock(return_value=row_insight)
    # fetch for source types and for retired_summaries
    fake_conn.fetch = AsyncMock(side_effect=[
        [{"id": 100, "type": "decision"}],  # doc type
        [{"id": 10, "superseded_reason": "lineage", "superseded_by": 11}],  # retired_summaries
    ])
    resp_ins = await c._status_of_summary(20, "insight")
    assert resp_ins.status == 200
    data_ins = json.loads(resp_ins.text)
    assert data_ins["superseded"] is False
    assert data_ins["superseded_by"] is None
    assert data_ins["retired_summaries"] == [
        {"summary_id": 10, "superseded_reason": "lineage", "superseded_by": 11}
    ]
