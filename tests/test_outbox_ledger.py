"""Unit tests for the outbox dream-cycle ledger — fact path (decision pg_id 267).

The ledger completes the outbox lifecycle:

    pending → applied → rem_reviewed → consolidated → row DELETED

'consolidated' commits atomically with the community-summary INSERT; deletion
happens only after the Neo4j marking succeeds, so a row's absence means both
stores are conclusively synced. These tests pin the SQL contract with a stub
connection — which statuses and row types each ledger function may touch.
Decision and retrospective rows must never be affected: their lifecycle is
downstream of the unratified decision-NREM design (see pg_id 269).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from consolidation_loop import (
    mark_covered_rows_consolidated,
    fetch_ledger_backlog,
    fetch_unreconciled,
    close_ledger_rows,
)


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


# ── mark_covered_rows_consolidated ────────────────────────────────────────────

def test_backfill_advances_rows_and_commits():
    conn = StubConn(script=[{"rowcount": 7, "rows": []}])
    assert mark_covered_rows_consolidated(conn) == 7
    assert conn.commits == 1
    sql, _ = conn.executed[0]
    assert sql.startswith("UPDATE neo4j_outbox")
    assert "DELETE" not in sql  # advance, never destroy, before graph sync


def test_backfill_touches_only_applied_and_rem_reviewed():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    mark_covered_rows_consolidated(conn)
    sql, _ = conn.executed[0]
    # pending rows still owe a Neo4j write; failed rows need investigation.
    assert "status IN ('applied', 'rem_reviewed')" in sql


def test_backfill_excludes_decision_and_retrospective_rows():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    mark_covered_rows_consolidated(conn)
    sql, _ = conn.executed[0]
    assert "NOT IN ('retrospective', 'decision')" in sql


def test_backfill_requires_active_covering_summary():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    mark_covered_rows_consolidated(conn)
    sql, _ = conn.executed[0]
    assert "NOT cs.superseded" in sql
    assert "ANY(cs.source_pg_ids)" in sql


# ── fetch_ledger_backlog ──────────────────────────────────────────────────────

def test_backlog_returns_distinct_rem_reviewed_fact_ids():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(11,), (42,)]}])
    assert fetch_ledger_backlog(conn) == [11, 42]
    sql, _ = conn.executed[0]
    assert "SELECT DISTINCT pg_id" in sql
    assert "status = 'rem_reviewed'" in sql


def test_backlog_excludes_decision_and_retro_rows_by_type():
    # Retro rows can sit at rem_reviewed (REM's mark targets the latest
    # applied row for a pg_id, and a retro shares its decision's pg_id) —
    # the type filter, not status, keeps them out of the fact path.
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    fetch_ledger_backlog(conn)
    sql, _ = conn.executed[0]
    assert "NOT IN ('retrospective', 'decision')" in sql


# ── fetch_unreconciled ────────────────────────────────────────────────────────

def test_unreconciled_returns_covering_summary_tuples():
    row = (12, "Neo4j", "general", [1, 2, 3, 4, 5])
    conn = StubConn(script=[{"rowcount": 1, "rows": [row]}])
    assert fetch_unreconciled(conn) == [row]
    sql, _ = conn.executed[0]
    assert "o.status = 'consolidated'" in sql
    assert "NOT cs.superseded" in sql


# ── close_ledger_rows ─────────────────────────────────────────────────────────

def test_close_deletes_only_consolidated_status():
    rows = [(101, 1), (102, 2), (103, 3), (104, 4), (105, 5)]
    conn = StubConn(script=[{"rowcount": 5, "rows": rows}])
    assert close_ledger_rows(conn, [1, 2, 3, 4, 5]) == 5
    assert conn.commits == 1
    sql, params = conn.executed[0]
    assert sql.startswith("DELETE FROM neo4j_outbox")
    assert "status = 'consolidated'" in sql
    assert "RETURNING id, pg_id" in sql
    assert params == ([1, 2, 3, 4, 5],)


def test_close_always_logs_deletions_to_gateway_log(caplog):
    # The row is the only record of the dream lifecycle — its destruction
    # must always leave a trace in the gateway log, with the actual rows
    # deleted (not the requested list).
    import logging
    rows = [(101, 1), (103, 3)]
    conn = StubConn(script=[{"rowcount": 2, "rows": rows}])
    with caplog.at_level(logging.INFO, logger="ConsolidationDaemon"):
        assert close_ledger_rows(conn, [1, 2, 3], context="reconciliation") == 2
    record = next(r for r in caplog.records if "Ledger close" in r.getMessage())
    msg = record.getMessage()
    assert "[reconciliation]" in msg
    # Each deletion is a traceable pair: outbox row id ↔ consolidated fact pg_id.
    assert "outbox_id=101→pg_id=1" in msg
    assert "outbox_id=103→pg_id=3" in msg
    assert "pg_id=2" not in msg  # 2 was requested but not present — not logged


def test_close_with_no_ids_is_a_noop(caplog):
    import logging
    conn = StubConn()
    with caplog.at_level(logging.INFO, logger="ConsolidationDaemon"):
        assert close_ledger_rows(conn, []) == 0
    assert conn.executed == []
    assert conn.commits == 0
    assert not any("Ledger close" in r.getMessage() for r in caplog.records)
