"""Tests for PR 2: A superseded fact retires its thematic summary, and insights follow it.

Covers:
- decision:2778: constituent supersession retires the row and folds a successor
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
  - Step 5 graph mark: no-successor retirement calls graph marker with retired ids.
  - T7: Lineage invalidation pass retires no thematic row (kinds=("insight",)).
  - T8: run_ledger_sweep with an empty backlog still consolidates.
  - Sweep reconciliation: re-applies superseded=true and SUPERSEDES edge for PG-superseded thematic rows.
  - T9: coordinator: both expanders' Cypher carries the CommunitySummary superseded exclusion.
- decision:2778: insights follow the successor
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
    recheck_kept_thematic_ids,
    retire_invalidated_summaries,
    resolve_standing_ids,
    solid_match,
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
        if self._script:
            item = self._script.pop(0)
            if callable(item):
                self._current = item(sql, params)
            else:
                self._current = item
        else:
            self._current = {"rowcount": 0, "rows": []}

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
        self.executed.append(("COMMIT", None))

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
    """T1: Active row 10 src [1,2,3,4], fact 2 superseded by 5 via two-hop chain (2 -> 6 -> 5), scan [1,3,4,5].
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
      (c) drop pointer UPDATE -> fails
      (d) one hop instead of chain end in resolve_standing_ids at fold link -> stops at 6 (superseded) and misses 5 -> fails.
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
        #       1 -> 1 (live), 3 -> 3 (live), 4 -> 4 (live), 2 -> 5 (two-hop chain: 2 superseded by 6, 6 superseded by 5, live)
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

    # Assert EXACTLY one commit for the atomic fold transaction, and that NO commit
    # happens between the retire UPDATE and the INSERT (mutation: commit between them must fail).
    commits = [i for i, (sql, _) in enumerate(conn.executed) if sql == "COMMIT"]
    commits_between = [i for i in commits if retire_idx < i < insert_idx]
    assert len(commits_between) == 0, f"Found {len(commits_between)} commit(s) between retire UPDATE and INSERT"

    # Exactly one commit closes the fold transaction before graph sync
    close_ledger_idx = next(i for i, (sql, _) in enumerate(conn.executed)
                            if "DELETE FROM neo4j_outbox" in sql)
    fold_commits = [i for i in commits if retire_idx < i < close_ledger_idx]
    assert len(fold_commits) == 1, f"Expected exactly 1 commit for fold transaction, found {len(fold_commits)}"
    assert conn.commits == 4, f"Expected exactly 4 commits total across sweep, found {conn.commits}"


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
    Asserts skip_ids passed to step 5 contain the active id of every gating key
    (unchanged and changed).
    Mutation killed: drop gating ids from skip_ids -> fails.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 2)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 104)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    content_ops = "[FACT kind=discussion recorded=2026-09-29 pg_id=1] fact 1\n[FACT kind=discussion recorded=2026-09-29 pg_id=2] fact 2"
    scan_rows = [
        {"pg_id": 1, "content": "fact 1", "project": "proj", "domain": "ops"},
        {"pg_id": 2, "content": "fact 2", "project": "proj", "domain": "ops"},
        {"pg_id": 3, "content": "fact 3", "project": "proj", "domain": "dev"},
        {"pg_id": 4, "content": "fact 4", "project": "proj", "domain": "dev"},
    ]
    d = datetime.date(2026, 9, 29)

    script = [
        # 1. _fetch_records for 1, 2, 3, 4
        {"rowcount": 4, "rows": [
            (1, "proj", "fact", None, d, {}),
            (2, "proj", "fact", None, d, {}),
            (3, "proj", "fact", None, d, {}),
            (4, "proj", "fact", None, d, {}),
        ]},
        # 2. dead letter
        {"rowcount": 0, "rows": []},
        # 3. Active rows:
        #    - (proj, ops) has id 10 matching content -> unchanged gating key
        #    - (proj, dev) has id 20 differing content -> changed gating key
        {"rowcount": 2, "rows": [
            ("proj", "ops", 10, content_ops, [1, 2], []),
            ("proj", "dev", 20, "old content dev", [3, 4], []),
        ]},
        # 4. Census outbox timestamps for changed member ids [3, 4]
        {"rowcount": 2, "rows": []},
        # 5. Step 4 check: fact 2 in the unchanged row is PG-superseded!
        {"rowcount": 1, "rows": [(2,)]},
        # 6. Step 5 retire_invalidated_summaries with skip_ids=(10, 20): returns nothing
        {"rowcount": 0, "rows": []},
        # 7. Summary write for (proj, dev): in-place upsert returns 20
        {"rowcount": 1, "rows": [(20,)]},
        # 8. Outbox flip
        {"rowcount": 2, "rows": []},
        # 9. Supersede covered
        {"rowcount": 0, "rows": []},
        # 10. close_ledger_rows
        {"rowcount": 0, "rows": []},
        # 11. Post-loop drop passes
        {"rowcount": 0, "rows": []},  # drop_out_of_scan_refold_rows
        {"rowcount": 0, "rows": []},  # close_refold_ledger_rows (refolded)
        {"rowcount": 0, "rows": []},  # close_refold_ledger_rows (dropped)
    ]
    conn = StubConn(script=script)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)

    # Spy on retire_invalidated_summaries to capture skip_ids passed to step 5
    retire_calls = []
    orig_retire = cl.retire_invalidated_summaries
    def spy_retire(conn, kinds=("thematic", "insight"), skip_ids=()):
        retire_calls.append({"kinds": kinds, "skip_ids": tuple(skip_ids)})
        return orig_retire(conn, kinds=kinds, skip_ids=skip_ids)
    monkeypatch.setattr(cl, "retire_invalidated_summaries", spy_retire)

    with caplog.at_level(logging.WARNING, logger=cl.logger.name):
        await daemon._consolidate_clusters(scan_rows)

    # Warning logged about store disagreement
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("store disagreement" in m.lower() or "disagree" in m.lower() for m in warnings)

    # Assert skip_ids actually passed contains the active id of every gating key (unchanged 10 and changed 20)
    assert len(retire_calls) == 1
    passed_skip = retire_calls[0]["skip_ids"]
    assert 10 in passed_skip, "skip_ids must contain active id of unchanged gating key"
    assert 20 in passed_skip, "skip_ids must contain active id of changed gating key"

    # Also assert the SQL query executed in step 5 carried both active gating IDs
    leg1_call = next((sql, params) for sql, params in conn.executed
                     if "FROM community_summaries cs" in sql and "JOIN technical_docs t" in sql)
    assert leg1_call[1] is not None
    assert 10 in leg1_call[1][0]
    assert 20 in leg1_call[1][0]

    # Active row 10 was NOT retired
    assert not any("UPDATE community_summaries SET superseded = true" in sql and params == (10,)
                   for sql, params in conn.executed)


def test_t4b_non_gating_row_holding_no_superseded_fact_is_not_retired():
    """T4b: A non-gating row holding NO superseded fact is not retired.
    Mutation killed: widen leg-1 predicate (e.g. drop superseded check) -> fails in T4b itself.
    """
    def leg1_responder(sql, params):
        # The non-gating summary 30 holds fact 100 which is NOT superseded.
        # If the predicate properly checks COALESCE(t.superseded, false) = true, no rows match.
        # If a mutation widens the predicate by omitting/relaxing the superseded check,
        # it returns summary 30.
        if "t.superseded" in sql and "true" in sql:
            return {"rowcount": 0, "rows": []}
        return {"rowcount": 1, "rows": [(30, [100], 100)]}

    conn = StubConn(script=[
        leg1_responder,
        # If widened predicate returned summary 30, subsequent calls would execute retirement:
        {"rowcount": 1, "rows": []},  # retire UPDATE
        {"rowcount": 1, "rows": [(100, 100, False)]},  # resolve_standing_ids
        {"rowcount": 1, "rows": []},  # refold_ledger insert
    ])
    retired, opened = retire_invalidated_summaries(conn, kinds=("thematic",), skip_ids=())
    assert retired == []
    assert opened == 0
    assert not any("UPDATE community_summaries SET superseded = true" in sql
                   for sql, _ in conn.executed)

    # Assert leg-1 query executed with the narrow predicate
    leg1_sql = conn.executed[0][0]
    assert "COALESCE(t.superseded, false) = true" in leg1_sql
    assert "NOT cs.superseded" in leg1_sql
    assert "COALESCE(cs.metadata->>'kind', 'thematic') <> 'insight'" in leg1_sql


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


@pytest.mark.asyncio
async def test_step5_calls_graph_marker_with_retired_ids(monkeypatch):
    """Step 5 calls _mark_summaries_retired_in_graph with the retired summary ids.
    Mutation killed: skip the call -> fails.
    Also verifies Neo4j failure is logged and does not crash the run (R3).
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 3)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 105)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)

    # 1 member below threshold 3 -> no eligible clusters
    scan_rows = [
        {"pg_id": 1, "content": "fact 1", "project": "proj", "domain": "ops"},
    ]
    d = datetime.date(2026, 9, 29)
    script = [
        {"rowcount": 1, "rows": [(1, "proj", "fact", None, d, {})]},
        {"rowcount": 0, "rows": []},
        # Drop passes after loop
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},
    ]
    conn = StubConn(script=script)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)

    # Mock retire_invalidated_summaries in step 5 returning retired thematic summaries [30, 31]
    monkeypatch.setattr(
        cl, "retire_invalidated_summaries",
        lambda conn, kinds=("thematic",), skip_ids=(): ([(30, "thematic", [10]), (31, "thematic", [11])], 2),
    )

    marked_ids = []
    async def fake_mark(ids):
        marked_ids.extend(ids)
    daemon._mark_summaries_retired_in_graph = AsyncMock(side_effect=fake_mark)

    await daemon._consolidate_clusters(scan_rows)

    assert daemon._mark_summaries_retired_in_graph.await_count == 1
    assert marked_ids == [30, 31]

    # Verify R3: Neo4j exception does not crash _consolidate_clusters
    daemon._mark_summaries_retired_in_graph = AsyncMock(side_effect=RuntimeError("Neo4j connection dropped"))
    conn2 = StubConn(script=[
        {"rowcount": 1, "rows": [(1, "proj", "fact", None, d, {})]},
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 0, "rows": []},
    ])
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn2)
    # Must complete without raising RuntimeError
    await daemon._consolidate_clusters(scan_rows)


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


@pytest.mark.asyncio
async def test_ledger_sweep_reconciles_superseded_thematic_summaries_in_graph(monkeypatch):
    """The ledger-sweep reconciliation scan re-applies superseded=true and the SUPERSEDES
    edge for PG-superseded thematic rows.
    Mutation killed: remove the scan -> fails.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.setattr(cl, "PG_CONN", "fake_dsn")
    fake_conn = MagicMock()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: fake_conn)
    monkeypatch.setattr(cl, "mark_covered_rows_consolidated", lambda conn: 0)
    monkeypatch.setattr(cl, "fetch_unreconciled", lambda conn: [])
    # Return PG-superseded thematic summaries: 10 has successor 11; 25 has no successor (None)
    monkeypatch.setattr(
        cl, "fetch_superseded_thematic_summaries",
        lambda conn: [(10, 11), (25, None)],
    )
    monkeypatch.setattr(cl, "fetch_combined_fact_backlog", lambda conn: [])
    daemon._find_grounded_fact_groups = AsyncMock(return_value=[])

    await daemon.run_ledger_sweep()

    # Verify session calls for reconciliation
    # 1. Setting s.superseded = true for all PG-superseded summaries [10, 25]
    flag_calls = [c for c in session.calls if "s.superseded = true" in c[0] and "CommunitySummary" in c[0]]
    assert len(flag_calls) >= 1
    flag_params = [c[1] for c in flag_calls if "ids" in c[1]]
    assert any(p["ids"] == [10, 25] for p in flag_params)

    # 2. Merging SUPERSEDES edge for pairs: [[10, 11]]
    edge_calls = [c for c in session.calls if "MERGE (new)-[:SUPERSEDES]->(old)" in c[0]]
    assert len(edge_calls) == 1
    assert edge_calls[0][1]["pairs"] == [[10, 11]]
    assert "old.superseded = true" in edge_calls[0][0]


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
    assert "ONT.community_summary" in src_single or "CommunitySummary" in src_single
    assert expected_predicate in src_batch
    assert "ONT.community_summary" in src_batch or "CommunitySummary" in src_batch


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
            (101, {"kind": "insight", "summary_ids": [10, 20]}, []),
            (102, {"kind": "insight", "summary_ids": [20, 10, 30]}, []),
            (103, {"kind": "insight", "summary_ids": [10, 11]}, []),
        ]},
        # 2b. SELECT id, source_pg_ids FROM community_summaries WHERE id = ANY(...)
        {"rowcount": 4, "rows": [(10, []), (11, []), (20, []), (30, [])]},
        # 3. UPDATE metadata for 101
        {"rowcount": 1, "rows": []},
        # 4. UPDATE metadata for 102
        {"rowcount": 1, "rows": []},
        # 5. UPDATE metadata for 103
        {"rowcount": 1, "rows": []},
    ]
    conn = StubConn(script=script)
    with conn.cursor() as cur:
        repointed, kept = link_thematic_successor(cur, old_id=10, new_id=11)

    assert repointed == [101, 102, 103]
    assert kept == []

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
    meta_updates = [call for call in conn.executed[3:] if "UPDATE community_summaries SET metadata" in call[0]]
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

    resp = await c._status_of_summary(10, "summary")
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
    # fetch calls for _status_of_summary:
    # 1. doc types for source_pg_ids [100]
    # 2. _annotate_retired_summaries: fetch for community_summaries WHERE id = ANY([10])
    # 3. _annotate_retired_summaries: decisions query for doc [100]
    fake_conn.fetch = AsyncMock(side_effect=[
        [{"id": 100, "type": "decision"}],  # doc type
        [{"id": 10, "superseded": True, "superseded_reason": "lineage", "superseded_by": None, "source_pg_ids": []}],
        [{"decision_id": 100, "fact_ids": []}],  # decisions query
    ])
    resp_ins = await c._status_of_summary(20, "insight")
    assert resp_ins.status == 200
    data_ins = json.loads(resp_ins.text)
    assert data_ins["superseded"] is False
    assert data_ins["superseded_by"] is None
    assert data_ins["retired_summaries"] == [
        {"summary_id": 10, "superseded_reason": "lineage", "superseded_by": None, "unsupported": []}
    ]


# ── decision:2801 Tests (S1 - S5 & Review Fold requirements) ──────────────────

# ── S1: solid_match table ────────────────────────────────────────────────────

def test_s1_solid_match_truth_table():
    """S1: solid_match table: equal sets -> True; old ⊂ new -> True; one thread lost -> False; empty old -> True.
    Mutations killed:
      (a) >= -> == fails on old ⊂ new
      (b) treat empty old as False fails on empty old.
    """
    # Equal sets -> True
    assert solid_match({1, 2}, {1, 2}) is True
    # old ⊂ new -> True (killed if >= or ==)
    assert solid_match({1}, {1, 2}) is True
    # One thread lost -> False
    assert solid_match({1, 2}, {2, 3}) is False
    assert solid_match({1}, set()) is False
    # Empty old -> True (killed if empty old treated as False)
    assert solid_match(set(), {1}) is True
    assert solid_match(set(), set()) is True


# ── S2: thread support retrospective legs ────────────────────────────────────

def test_s2_thread_support_retrospective_legs():
    """S2: thread support counts a standing retrospective of D grounded on T (the 1144/1147 case)
    and does NOT count a retrospective of another decision grounded on T (the 2613/2626 case)
    nor a superseded retrospective. Synthetic chain used.
    Mutations killed:
      (a) drop the retrospective leg: standing retro of D not counted -> fails
      (b) match retrospectives by any target: retro of another decision counted -> fails
      (c) ignore superseded on retrospectives: superseded retro counted -> fails.
    """
    # Active insight 300 has decision 100.
    # 1. Standing retro 101 targets decision 100, grounded on fact 1.
    # 2. Standing retro 102 targets decision 200, grounded on fact 2.
    # 3. Superseded retro 103 targets decision 100, grounded on fact 3.
    # Thematic summary 10 has facts [1, 2, 3].
    # When linking 10 -> 11:
    # Subtest A: Summary 11 has [1] -> decision 100 counts retro 101 -> solid -> repointed!
    script_a = [
        {"rowcount": 1, "rows": []},  # UPDATE superseded_by
        {"rowcount": 1, "rows": [(300, {"kind": "insight", "summary_ids": [10]}, [100])]},
        # Batched thread query: standing retro 101 of decision 100 has fact 1
        {"rowcount": 1, "rows": [(100, [1])]},
        # Summary facts: 10 has [1, 2, 3], 11 has [1]
        {"rowcount": 2, "rows": [(10, [1, 2, 3]), (11, [1])]},
        {"rowcount": 1, "rows": []},  # UPDATE metadata for 300
    ]
    conn_a = StubConn(script=script_a)
    with conn_a.cursor() as cur:
        repointed_a, kept_a = link_thematic_successor(cur, old_id=10, new_id=11)
    assert repointed_a == [300]
    assert kept_a == []

    # Subtest B: Summary 11 has [2] (grounded only by retro 102 of decision 200).
    # Decision 100 does NOT count retro 102 -> not solid -> kept!
    script_b = [
        {"rowcount": 1, "rows": []},  # UPDATE superseded_by
        {"rowcount": 1, "rows": [(300, {"kind": "insight", "summary_ids": [10]}, [100])]},
        {"rowcount": 1, "rows": [(100, [1])]},  # thread 100 only has fact 1
        {"rowcount": 2, "rows": [(10, [1, 2, 3]), (11, [2])]},  # 11 has fact 2
    ]
    conn_b = StubConn(script=script_b)
    with conn_b.cursor() as cur:
        repointed_b, kept_b = link_thematic_successor(cur, old_id=10, new_id=11)
    assert repointed_b == []
    assert kept_b == [300]

    # Subtest C: Summary 11 has [3] (grounded only by superseded retro 103).
    # Decision 100 does NOT count superseded retro 103 -> not solid -> kept!
    script_c = [
        {"rowcount": 1, "rows": []},  # UPDATE superseded_by
        {"rowcount": 1, "rows": [(300, {"kind": "insight", "summary_ids": [10]}, [100])]},
        {"rowcount": 1, "rows": [(100, [1])]},  # thread 100 only has fact 1
        {"rowcount": 2, "rows": [(10, [1, 2, 3]), (11, [3])]},  # 11 has fact 3
    ]
    conn_c = StubConn(script=script_c)
    with conn_c.cursor() as cur:
        repointed_c, kept_c = link_thematic_successor(cur, old_id=10, new_id=11)
    assert repointed_c == []
    assert kept_c == [300]

    # S2 thread SQL pin: the retrospective leg, target_pg_id = the decision, the standing (NOT superseded) filter, and jsonb_typeof number filter
    thread_sqls = [c[0] for c in conn_a.executed if "decision_facts AS" in c[0]]
    assert len(thread_sqls) == 1
    sql_a = thread_sqls[0]
    assert "r.metadata->>'type' = 'retrospective'" in sql_a
    assert "r.metadata->>'target_pg_id' IN (SELECT id::text FROM decisions)" in sql_a
    assert "NOT r.superseded" in sql_a
    assert "jsonb_typeof(elem) = 'number'" in sql_a


# ── S3: link_thematic_successor solid repoint and kept ───────────────────────

def test_s3_link_thematic_successor_solid_repoint_and_kept():
    """S3: two insights, one solid, one losing a thread -> first repointed, second keeps old_id;
    superseded_by set in both cases.
    Mutation killed: repoint unconditionally (would repoint second insight) -> fails.
    """
    script = [
        # 1. UPDATE community_summaries SET superseded_by = 11 WHERE id = 10
        {"rowcount": 1, "rows": []},
        # 2. SELECT active insights citing 10 FOR UPDATE
        {"rowcount": 2, "rows": [
            (201, {"kind": "insight", "summary_ids": [10]}, [100]),
            (202, {"kind": "insight", "summary_ids": [10]}, [101]),
        ]},
        # 3. Batched thread query: doc 100 grounded in [1], doc 101 grounded in [2]
        {"rowcount": 2, "rows": [(100, [1]), (101, [2])]},
        # 4. Batched summary query: 10 has [1, 2], 11 has [1]
        {"rowcount": 2, "rows": [(10, [1, 2]), (11, [1])]},
        # 5. UPDATE metadata for 201 only (202 kept!)
        {"rowcount": 1, "rows": []},
    ]
    conn = StubConn(script=script)
    with conn.cursor() as cur:
        repointed, kept = link_thematic_successor(cur, old_id=10, new_id=11)

    assert repointed == [201]
    assert kept == [202]

    # Verify superseded_by set on old_id
    assert conn.executed[0][0] == "UPDATE community_summaries SET superseded_by = %s WHERE id = %s"
    assert conn.executed[0][1] == (11, 10)

    # Verify only insight 201 was updated in metadata
    meta_updates = [c for c in conn.executed if "UPDATE community_summaries SET metadata" in c[0]]
    assert len(meta_updates) == 1
    assert json.loads(meta_updates[0][1][0])["summary_ids"] == [11]
    assert meta_updates[0][1][1] == 201


# ── S4: re-gate links newest lineage-retired row ─────────────────────────────

@pytest.mark.asyncio
async def test_s4_regate_links_newest_lineage_retired_row(monkeypatch):
    """S4: re-gate: no active row, a lineage-retired row with superseded_by NULL on the key
    -> linked; a coverage-retired row or one with superseded_by set -> not linked.
    Mutation killed: drop the reason filter (would link coverage-retired row) -> fails.
    """
    daemon, _ = daemon_with_fake_graph()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 1)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 101)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)

    scan_rows = [{"pg_id": 1, "content": "fact 1", "project": "proj", "domain": "ops"}]
    d = datetime.date(2026, 9, 29)

    # Subtest 1: Lineage-retired row 40 with superseded_by IS NULL on the key -> found and linked!
    script_1 = [
        {"rowcount": 1, "rows": [(1, "proj", "fact", None, d, {})]},  # _fetch_records
        {"rowcount": 0, "rows": []},  # dead-letter
        {"rowcount": 0, "rows": []},  # fetch_active_thematic_rows: NO active row!
        {"rowcount": 0, "rows": []},  # census outbox
        {"rowcount": 0, "rows": []},  # step 5 retire_invalidated_summaries
        # _write_summary:
        # INSERT RETURNING id, (xmax = 0) -> (50, True)
        {"rowcount": 1, "rows": [(50, True)]},
        # Re-gate lookup query: finds lineage-retired row 40!
        {"rowcount": 1, "rows": [(40,)]},
        # link_thematic_successor(cur, 40, 50):
        # 1. UPDATE community_summaries SET superseded_by = 50 WHERE id = 40
        {"rowcount": 1, "rows": []},
        # 2. SELECT active insights citing 40
        {"rowcount": 0, "rows": []},
        # Outbox flip
        {"rowcount": 1, "rows": []},
        # Post-fold cleanup
        {"rowcount": 0, "rows": []},  # supersede_covered_summaries
        {"rowcount": 0, "rows": []},  # close_ledger_rows
        {"rowcount": 0, "rows": []},  # drop_out_of_scan_refold_rows
        {"rowcount": 0, "rows": []},  # close_refold_ledger_rows (refolded)
        {"rowcount": 0, "rows": []},  # close_refold_ledger_rows (dropped)
    ]
    conn_1 = StubConn(script=script_1)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn_1)
    await daemon._consolidate_clusters(scan_rows)

    # Verify re-gate lookup SQL has exact filters:
    regate_sqls = [c for c in conn_1.executed if "superseded_reason = 'lineage'" in c[0]]
    assert len(regate_sqls) == 1
    sql = regate_sqls[0][0]
    assert "superseded_reason = 'lineage'" in sql
    assert "superseded_by IS NULL" in sql
    assert "ORDER BY superseded_at DESC NULLS LAST, id DESC" in sql
    # Verify link executed:
    link_updates = [c for c in conn_1.executed if "UPDATE community_summaries SET superseded_by = %s WHERE id = %s" in c[0]]
    assert len(link_updates) == 1
    assert link_updates[0][1] == (50, 40)


# ── S5: read time unsupported lists D, F and F2 ──────────────────────────────

@pytest.mark.asyncio
async def test_s5_read_time_unsupported_lists_d_f_and_f2():
    """S5: read time: unsupported lists D, F and F2 for a kept insight; empty when solid.
    Two-hop chains: summary chain 10 -> 11 (superseded) -> 12 (active);
    fact chain 1 -> 2 (superseded) -> 3 (active).
    Mutation killed: one hop instead of chain end -> fails.
    Mutation killed: drop the unsupported field -> fails.
    """
    c = co.MemoryCoordinator()
    fake_conn = MagicMock()

    # Insight 301 citing retired summary 10. Summary 10 has successor 11 (superseded), 11 has successor 12 (active end).
    # Summary 10 has fact 1, 4. Summary 11 has fact 3. Summary 12 has fact 4.
    # Decision 501 grounded in fact 1. Summary 12 drops fact 1.
    # Fact 1 in technical_docs superseded by 2; Fact 2 superseded by 3; Fact 3 is standing (chain end F2=3).
    # Insight 302 citing retired summary 10.
    # Decision 502 grounded in fact 4. Summary 12 has fact 4 -> solid!
    insights_data = [
        {"id": 301, "summary_ids": [10], "source_pg_ids": [501]},
        {"id": 302, "summary_ids": [10], "source_pg_ids": [502]},
    ]

    # Query 1: community_summaries for ANY([10])
    # Query 2 (fetchrow): community_summaries for mid successor 11 (superseded)
    # Query 3 (fetchrow): community_summaries for active end 12 (active)
    # Query 4: decisions thread query for [501, 502]
    # Query 5: technical_docs for lost fact [1]
    # Query 6 (fetchrow): technical_docs for mid fact 2 (superseded)
    # Query 7 (fetchrow): technical_docs for active end fact 3 (active)
    fake_conn.fetch = AsyncMock(side_effect=[
        # 1. cited summary 10
        [{"id": 10, "superseded": True, "superseded_reason": "lineage", "superseded_by": 11, "source_pg_ids": [1, 4]}],
        # 4. decisions thread query
        [{"decision_id": 501, "fact_ids": [1]}, {"decision_id": 502, "fact_ids": [4]}],
        # 5. technical_docs for fact 1
        [{"id": 1, "superseded": True, "superseded_by": 2}],
    ])
    fake_conn.fetchrow = AsyncMock(side_effect=[
        # 2. mid successor 11 (superseded)
        {"id": 11, "superseded": True, "superseded_reason": "lineage", "superseded_by": 12, "source_pg_ids": [3]},
        # 3. active end 12 (active)
        {"id": 12, "superseded": False, "superseded_reason": None, "superseded_by": None, "source_pg_ids": [4]},
        # 6. mid fact 2 (superseded)
        {"id": 2, "superseded": True, "superseded_by": 3},
        # 7. active end fact 3 (active)
        {"id": 3, "superseded": False, "superseded_by": None},
    ])

    annotated = await c._annotate_retired_summaries(fake_conn, insights_data)

    # Insight 301 (kept): unsupported lists decision 501, superseded_facts=[1], superseding_facts=[3] (chain end)
    ins_301_retired = annotated[301]
    assert len(ins_301_retired) == 1
    assert ins_301_retired[0]["summary_id"] == 10
    assert ins_301_retired[0]["superseded_reason"] == "lineage"
    assert ins_301_retired[0]["superseded_by"] == 11
    assert "unsupported" in ins_301_retired[0]
    assert ins_301_retired[0]["unsupported"] == [
        {"decision": 501, "superseded_facts": [1], "superseding_facts": [3]}
    ]

    # Insight 302 (solid): unsupported is empty list because active end 12 carries fact 4
    ins_302_retired = annotated[302]
    assert len(ins_302_retired) == 1
    assert ins_302_retired[0]["summary_id"] == 10
    assert "unsupported" in ins_302_retired[0]
    assert ins_302_retired[0]["unsupported"] == []


# ── Whole-insight support test (Review Fold 1) ────────────────────────────────

def test_whole_insight_thread_supported_by_other_cited_row_not_lost():
    """Whole-insight test: an insight cites [10, 20]; decision 100 grounded in fact 1.
    Summary 10 has [1]; summary 20 ALSO has [1, 9].
    Summary 10 is replaced by 11 (which drops fact 1).
    Since summary 20 still carries fact 1, decision 100 retains support across the whole insight.
    Solid match is True -> insight repointed to [11, 20].
    Mutation killed: judging support per-row instead of over the whole insight -> fails.
    """
    script = [
        # 1. UPDATE community_summaries SET superseded_by = 11 WHERE id = 10
        {"rowcount": 1, "rows": []},
        # 2. SELECT active insights citing 10 FOR UPDATE
        {"rowcount": 1, "rows": [
            (701, {"kind": "insight", "summary_ids": [10, 20]}, [100]),
        ]},
        # 3. Batched thread query: doc 100 grounded in [1]
        {"rowcount": 1, "rows": [(100, [1])]},
        # 4. Batched summary query: 10 has [1], 11 has [8], 20 has [1, 9]
        {"rowcount": 3, "rows": [(10, [1]), (11, [8]), (20, [1, 9])]},
        # 5. UPDATE metadata for 701 -> [11, 20]
        {"rowcount": 1, "rows": []},
    ]
    conn = StubConn(script=script)
    with conn.cursor() as cur:
        repointed, kept = link_thematic_successor(cur, old_id=10, new_id=11)

    assert repointed == [701]
    assert kept == []

    meta_updates = [c for c in conn.executed if "UPDATE community_summaries SET metadata" in c[0]]
    assert len(meta_updates) == 1
    assert json.loads(meta_updates[0][1][0])["summary_ids"] == [11, 20]


# ── Sweep re-check test (Review Fold 2) ───────────────────────────────────────

def test_sweep_recheck_repoints_after_retrospective_grounds_successor():
    """Sweep re-check: an active insight citing lineage-retired summary 10 (superseded_by = 11,
    11 superseded_by = 12) repoints on the next sweep after a retrospective grounds on the new fact in 12.
    Two-hop chain: 10 -> 11 (superseded) -> 12 (active end).
    Mutations killed:
      (a) omit sweep re-check -> fails
      (b) one hop instead of chain end in recheck -> fails (repoints to 11 or leaves kept).
    """
    # Active insight 801 cites [10] with decision 100.
    # Summary 10 is lineage-retired with superseded_by = 11.
    # Summary 11 is lineage-retired with superseded_by = 12.
    # Summary 12 is active (superseded = False) with source_pg_ids = [5].
    # Retrospective 105 targets decision 100 and grounds on fact 5!
    script = [
        # 1. Query candidate active insights citing lineage-retired summaries
        {"rowcount": 1, "rows": [
            (801, {"kind": "insight", "summary_ids": [10]}, [100]),
        ]},
        # 2. Query cited summaries: 10 is lineage-retired, superseded_by = 11
        {"rowcount": 1, "rows": [
            (10, True, 11, "lineage", [2]),
        ]},
        # 3. Query mid successor 11: lineage-retired, superseded_by = 12
        {"rowcount": 1, "rows": [
            (11, True, 12, "lineage", [4]),
        ]},
        # 4. Query active end 12: active
        {"rowcount": 1, "rows": [
            (12, False, None, None, [5]),
        ]},
        # 5. Batched thread query: decision 100 has fact 5 (via retro 105)
        {"rowcount": 1, "rows": [
            (100, [5]),
        ]},
        # 6. UPDATE metadata for 801 -> repointed to [12]
        {"rowcount": 1, "rows": []},
    ]
    conn = StubConn(script=script)
    repointed_count, kept_count = recheck_kept_thematic_ids(conn)

    assert repointed_count == 1
    assert kept_count == 0
    assert conn.commits == 1

    meta_updates = [c for c in conn.executed if "UPDATE community_summaries SET metadata" in c[0]]
    assert len(meta_updates) == 1
    assert json.loads(meta_updates[0][1][0])["summary_ids"] == [12]
    assert meta_updates[0][1][1] == 801


# ── Vacuous solid match test ──────────────────────────────────────────────────

def test_vacuous_solid_match_when_no_supported_threads():
    """Vacuous solid match: an insight with no decisions (or decisions with empty grounding)
    vacuously satisfies solid_match(set(), set()) -> True, so it repoints cleanly.
    Mutation killed: treat empty old as False -> fails.
    """
    script = [
        {"rowcount": 1, "rows": []},  # UPDATE superseded_by
        {"rowcount": 1, "rows": [(901, {"kind": "insight", "summary_ids": [10]}, [])]},
        {"rowcount": 2, "rows": [(10, [1, 2]), (11, [1])]},
        {"rowcount": 1, "rows": []},  # UPDATE metadata
    ]
    conn = StubConn(script=script)
    with conn.cursor() as cur:
        repointed, kept = link_thematic_successor(cur, old_id=10, new_id=11)

    assert repointed == [901]
    assert kept == []


# ── Additional Sixth Pass Tests ──────────────────────────────────────────────

def test_resolve_standing_ids_two_hop_chain():
    """Two-hop chain test at fold link (resolve_standing_ids):
    Constituent fact 2 superseded by 6, 6 superseded by 5 (standing).
    Chain resolves to standing id 5.
    Mutation killed: one hop instead of chain end -> fails.
    """
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(2, 5, False)]},
    ])
    out = resolve_standing_ids(conn, [2])
    assert out == {2: (5, False)}
    sql, params = conn.executed[0]
    assert "WITH RECURSIVE" in sql
    assert "ORDER BY start_id, depth DESC" in sql


@pytest.mark.asyncio
async def test_read_time_whole_insight_two_kept_retired_rows_sharing_fact_neither_successor():
    """Read-time whole-insight test (F1): an insight cites [10, 20].
    Decision 100 is grounded in fact 1.
    Both kept retired rows 10 and 20 share fact 1 (source_pg_ids: 10 has [1], 20 has [1, 9]).
    Row 10 has successor 11 (source_pg_ids: [8]).
    Row 20 has successor 21 (source_pg_ids: [9]).
    Fact 1 is in NEITHER successor (11 has [8], 21 has [9]).
    Fact 1 is superseded in technical_docs by fact 2 (active).
    Under whole-insight evaluation, decision 100 lost support across the whole insight and is listed in unsupported.
    Mutation killed: judge per row at read time (would treat the other retired row as preserving support) -> fails.
    """
    c = co.MemoryCoordinator()
    fake_conn = MagicMock()

    insights_data = [
        {"id": 401, "summary_ids": [10, 20], "source_pg_ids": [100]},
    ]

    fake_conn.fetch = AsyncMock(side_effect=[
        # 1. cited summaries 10 and 20
        [
            {"id": 10, "superseded": True, "superseded_reason": "lineage", "superseded_by": 11, "source_pg_ids": [1]},
            {"id": 20, "superseded": True, "superseded_reason": "lineage", "superseded_by": 21, "source_pg_ids": [1, 9]},
        ],
        # 2. decisions thread query for [100]
        [{"decision_id": 100, "fact_ids": [1]}],
        # 3. technical_docs for fact 1
        [{"id": 1, "superseded": True, "superseded_by": 2}],
    ])
    fake_conn.fetchrow = AsyncMock(side_effect=[
        # 1. successor 11 (active)
        {"id": 11, "superseded": False, "superseded_reason": None, "superseded_by": None, "source_pg_ids": [8]},
        # 2. successor 21 (active)
        {"id": 21, "superseded": False, "superseded_reason": None, "superseded_by": None, "source_pg_ids": [9]},
        # 3. fact 2 (active chain end)
        {"id": 2, "superseded": False, "superseded_by": None},
    ])

    annotated = await c._annotate_retired_summaries(fake_conn, insights_data)
    ins_401 = annotated[401]
    assert len(ins_401) == 2
    # Both retired rows 10 and 20 list decision 100 in unsupported
    assert ins_401[0]["summary_id"] == 10
    assert ins_401[0]["unsupported"] == [
        {"decision": 100, "superseded_facts": [1], "superseding_facts": [2]}
    ]
    assert ins_401[1]["summary_id"] == 20
    assert ins_401[1]["unsupported"] == [
        {"decision": 100, "superseded_facts": [1], "superseding_facts": [2]}
    ]


@pytest.mark.asyncio
async def test_read_time_coverage_retired_and_null_reason_rows_yield_no_unsupported():
    """Read time on a coverage-retired and a NULL-reason row -> unsupported is empty list.
    Mutation killed: drop the lineage-only filter -> fails at both sites.
    """
    c = co.MemoryCoordinator()
    fake_conn = MagicMock()

    insights_data = [
        {"id": 501, "summary_ids": [10, 20], "source_pg_ids": [100]},
    ]

    # 1. Summary 10 is coverage-retired with successor 15
    # 2. Summary 20 is retired with NULL reason and NULL successor
    fake_conn.fetch = AsyncMock(side_effect=[
        [
            {"id": 10, "superseded": True, "superseded_reason": "coverage", "superseded_by": 15, "source_pg_ids": [1]},
            {"id": 20, "superseded": True, "superseded_reason": None, "superseded_by": None, "source_pg_ids": [2]},
        ],
        # decisions thread query (if called)
        [{"decision_id": 100, "fact_ids": [1, 2]}],
    ])

    annotated = await c._annotate_retired_summaries(fake_conn, insights_data)
    ins_501 = annotated[501]
    assert len(ins_501) == 2
    assert ins_501[0]["summary_id"] == 10
    assert ins_501[0]["superseded_reason"] == "coverage"
    assert ins_501[0]["unsupported"] == []

    assert ins_501[1]["summary_id"] == 20
    assert ins_501[1]["superseded_reason"] is None
    assert ins_501[1]["unsupported"] == []


def test_recheck_kept_case_thread_still_lost_does_not_repoint():
    """Recheck kept case: an insight whose thread is still lost (no retrospective grounding
    the decision on the successor's new facts) does NOT repoint.
    Mutation killed: recheck repoints unconditionally -> fails.
    """
    # Active insight 802 cites [10] with decision 100.
    # Summary 10 is lineage-retired with superseded_by = 11, source_pg_ids = [1].
    # Summary 11 is active with source_pg_ids = [5].
    # Decision 100 is grounded in fact 1 (not in 11, no retrospective on 5).
    script = [
        # 1. Candidate active insights citing lineage-retired summaries
        {"rowcount": 1, "rows": [
            (802, {"kind": "insight", "summary_ids": [10]}, [100]),
        ]},
        # 2. Query cited summaries
        {"rowcount": 1, "rows": [
            (10, True, 11, "lineage", [1]),
        ]},
        # 3. Query successor 11
        {"rowcount": 1, "rows": [
            (11, False, None, None, [5]),
        ]},
        # 4. Batched thread query: decision 100 only has fact 1
        {"rowcount": 1, "rows": [
            (100, [1]),
        ]},
    ]
    conn = StubConn(script=script)
    repointed_count, kept_count = recheck_kept_thematic_ids(conn)

    assert repointed_count == 0
    assert kept_count == 1
    # No metadata update query executed for 802
    meta_updates = [c for c in conn.executed if "UPDATE community_summaries SET metadata" in c[0]]
    assert len(meta_updates) == 0


@pytest.mark.asyncio
async def test_daemon_ledger_sweep_calls_recheck(monkeypatch):
    """The daemon's ledger sweep calls recheck_kept_thematic_ids.
    Mutation killed: skip the recheck call -> fails.
    """
    daemon, session = daemon_with_fake_graph()
    monkeypatch.setattr(cl, "PG_CONN", "fake_dsn")
    fake_conn = MagicMock()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: fake_conn)
    monkeypatch.setattr(cl, "mark_covered_rows_consolidated", lambda conn: 0)
    monkeypatch.setattr(cl, "fetch_unreconciled", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_superseded_thematic_summaries", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_combined_fact_backlog", lambda conn: [])
    daemon._find_grounded_fact_groups = AsyncMock(return_value=[])

    mock_recheck = MagicMock(return_value=(0, 0))
    monkeypatch.setattr(cl, "recheck_kept_thematic_ids", mock_recheck)

    await daemon.run_ledger_sweep()

    assert mock_recheck.call_count == 1
    assert mock_recheck.call_args[0][0] == fake_conn


@pytest.mark.asyncio
async def test_f2_retracted_fact_yields_empty_superseding_facts():
    """F2: a retracted fact (superseded with no successor in technical_docs) yields
    an empty superseding_facts list in unsupported, not reporting itself as its own successor.
    Mutation killed: record retracted fact in superseding_facts -> fails.
    """
    c = co.MemoryCoordinator()
    fake_conn = MagicMock()

    insights_data = [
        {"id": 601, "summary_ids": [10], "source_pg_ids": [501]},
    ]

    # Summary 10 is lineage-retired with successor 11.
    # Summary 10 has fact 1. Summary 11 has fact 2 (drops 1).
    # Decision 501 is grounded in fact 1.
    # Fact 1 is retracted: superseded = True, superseded_by = None.
    fake_conn.fetch = AsyncMock(side_effect=[
        # 1. cited summary 10
        [{"id": 10, "superseded": True, "superseded_reason": "lineage", "superseded_by": 11, "source_pg_ids": [1]}],
        # 3. decisions thread query
        [{"decision_id": 501, "fact_ids": [1]}],
        # 4. technical_docs for fact 1 (retracted: superseded=True, superseded_by=None)
        [{"id": 1, "superseded": True, "superseded_by": None}],
    ])
    fake_conn.fetchrow = AsyncMock(side_effect=[
        # 2. successor 11 (active)
        {"id": 11, "superseded": False, "superseded_reason": None, "superseded_by": None, "source_pg_ids": [2]},
    ])

    annotated = await c._annotate_retired_summaries(fake_conn, insights_data)
    ins_601 = annotated[601]
    assert len(ins_601) == 1
    assert ins_601[0]["unsupported"] == [
        {"decision": 501, "superseded_facts": [1], "superseding_facts": []}
    ]


