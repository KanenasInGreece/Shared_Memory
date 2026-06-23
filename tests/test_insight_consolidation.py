"""Unit tests for Phase 3a insight consolidation (decision pg_id 276).

NREM's second cluster path: decision clusters spanning ≥2 projects, grounded
in a shared Fact via a non-mega-hub Entity, with at least one HAD_OUTCOME
edge (existence — never rating valence). Insights are always-INSERT
kind='insight' community_summaries; supersession is the dedup; the ledger's
decision and retrospective rows flip to 'consolidated' transactionally with
the insight and close (by row id) after the graph marking.

All Postgres/Neo4j/LLM I/O is stubbed — no live infrastructure required.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from consolidation_loop import (
    ConsolidationDaemon,
    INSIGHT_THRESHOLD,
    INSIGHT_HUB_DEGREE_CAP,
    close_ledger_rows_by_id,
    fetch_insight_outbox_rows,
    fetch_open_retro_decision_ids,
    fetch_refold_insights,
    fetch_unreconciled_insights,
    supersede_covered_summaries,
    write_insight_summary,
)


# ── Stubs (extends the test_outbox_ledger pattern with fetchone) ─────────────

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
    """Captures every (query, params) run against the fake Neo4j driver."""
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


# ── fetch_open_retro_decision_ids ─────────────────────────────────────────────

def test_open_retro_ids_selects_retro_type_only():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(245,), (267,)]}])
    assert fetch_open_retro_decision_ids(conn) == [245, 267]
    sql, _ = conn.executed[0]
    assert "SELECT DISTINCT pg_id" in sql
    assert "= 'retrospective'" in sql


def test_open_retro_ids_excludes_pending_and_failed_rows():
    # pending/failed rows still owe the outbox worker a Neo4j write — the
    # HAD_OUTCOME edge does not exist yet, so they are not fold triggers.
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    fetch_open_retro_decision_ids(conn)
    sql, _ = conn.executed[0]
    assert "status IN ('applied', 'rem_reviewed')" in sql


# ── fetch_refold_insights ─────────────────────────────────────────────────────

def test_refold_targets_active_insights_overlapping_retro_ids():
    row = (70, "OutboxPattern", [245, 267], "old insight text")
    conn = StubConn(script=[{"rowcount": 1, "rows": [row]}])
    assert fetch_refold_insights(conn, [245]) == [row]
    sql, params = conn.executed[0]
    assert "metadata->>'kind' = 'insight'" in sql
    assert "NOT superseded" in sql
    assert "source_pg_ids &&" in sql
    assert params == ([245],)


def test_refold_no_retro_ids_runs_no_query():
    conn = StubConn()
    assert fetch_refold_insights(conn, []) == []
    assert conn.executed == []


# ── fetch_insight_outbox_rows ─────────────────────────────────────────────────

def test_consumable_rows_snapshot_by_id_decision_and_retro_types():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(101,), (102,)]}])
    assert fetch_insight_outbox_rows(conn, [245, 267]) == [101, 102]
    sql, _ = conn.executed[0]
    assert "SELECT id FROM neo4j_outbox" in sql
    assert "IN ('decision', 'retrospective')" in sql
    assert "status IN ('applied', 'rem_reviewed')" in sql


def test_consumable_rows_empty_pg_ids_runs_no_query():
    conn = StubConn()
    assert fetch_insight_outbox_rows(conn, []) == []
    assert conn.executed == []


# ── write_insight_summary ─────────────────────────────────────────────────────

def test_insight_write_is_always_insert_never_upsert():
    # Resurrection trap (decision 276): a conflict-UPDATE would resurrect a
    # superseded row in place — the insight INSERT must carry NO ON CONFLICT.
    conn = StubConn(script=[{"rowcount": 1, "rows": [(77,)]}, {"rowcount": 2, "rows": []}])
    sid = write_insight_summary(conn, "insight", "{}", [0.1], [245, 267], [101, 102])
    assert sid == 77
    insert_sql, _ = conn.executed[0]
    assert insert_sql.startswith("INSERT INTO community_summaries")
    assert "ON CONFLICT" not in insert_sql


def test_insight_write_flips_consumed_rows_in_same_transaction():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(77,)]}, {"rowcount": 2, "rows": []}])
    write_insight_summary(conn, "insight", "{}", [0.1], [245, 267], [101, 102])
    flip_sql, flip_params = conn.executed[1]
    assert "SET status = 'consolidated'" in flip_sql
    assert "id = ANY(%s)" in flip_sql          # by row id, never by pg_id
    assert flip_params == ([101, 102],)
    assert conn.commits == 0                   # commit is the caller's job


def test_insight_write_no_rows_skips_flip():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(78,)]}])
    write_insight_summary(conn, "insight", "{}", [0.1], [245], [])
    assert len(conn.executed) == 1


# ── supersede_covered_summaries ───────────────────────────────────────────────

def _supersession_conn(old_rows):
    return StubConn(script=[{"rowcount": len(old_rows), "rows": old_rows}]
                    + [{"rowcount": 1, "rows": []} for _ in old_rows])


def test_equal_source_set_supersedes_prior_insight():
    # A re-fold writes the SAME source_pg_ids — the equal set must ride the
    # covered-subset rule and replace its predecessor.
    conn = _supersession_conn([(70, [245, 267])])
    assert supersede_covered_summaries(conn, 77, [245, 267]) == [70]


def test_strict_subset_supersedes():
    conn = _supersession_conn([(70, [245])])
    assert supersede_covered_summaries(conn, 77, [245, 267]) == [70]


def test_disjoint_and_superset_sources_survive():
    conn = _supersession_conn([(70, [1, 2, 3]), (71, [245, 267, 999])])
    assert supersede_covered_summaries(conn, 77, [245, 267]) == []
    assert conn.commits == 0


# ── close_ledger_rows_by_id ───────────────────────────────────────────────────

def test_close_by_id_deletes_only_consolidated_rows():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(101, 245), (102, 267)]}])
    assert close_ledger_rows_by_id(conn, [101, 102]) == 2
    sql, params = conn.executed[0]
    assert sql.startswith("DELETE FROM neo4j_outbox")
    assert "status = 'consolidated'" in sql
    assert "id = ANY(%s)" in sql
    assert params == ([101, 102],)
    assert conn.commits == 1


def test_close_by_id_empty_list_is_noop():
    conn = StubConn()
    assert close_ledger_rows_by_id(conn, []) == 0
    assert conn.executed == []


# ── fetch_unreconciled_insights ───────────────────────────────────────────────

def test_unreconciled_insights_query_contract():
    row = (77, "OutboxPattern", [245, 267])
    conn = StubConn(script=[{"rowcount": 1, "rows": [row]}])
    assert fetch_unreconciled_insights(conn) == [row]
    sql, _ = conn.executed[0]
    assert "cs.metadata->>'kind' = 'insight'" in sql
    assert "o.status = 'consolidated'" in sql
    assert "IN ('decision', 'retrospective')" in sql
    assert "NOT cs.superseded" in sql


# ── Fresh-cluster gate (Cypher contract) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_fresh_cluster_gate_encodes_ratified_rules():
    daemon, session = daemon_with_fake_graph([FakeResult([])])
    out = await daemon._find_fresh_insight_clusters()
    assert out == []
    query, params = session.calls[0]
    # Existence of a HAD_OUTCOME edge — never its rating.
    assert "HAD_OUTCOME" in query
    assert "rating" not in query
    # ≥2 distinct projects; threshold and mega-hub cap parameterised.
    assert "size(projects) >= 2" in query
    assert params["threshold"] == INSIGHT_THRESHOLD
    assert params["hub_cap"] == INSIGHT_HUB_DEGREE_CAP
    # Reversed decisions never seed a fresh cluster; re-folds keep them
    # (boundary evidence) by keying on the insight's source_pg_ids instead.
    assert "coalesce(d.superseded, false) = false" in query
    # Grounding: the shared entity must carry at least one Fact.
    assert "(f:Fact)" in query.replace("`", "")


@pytest.mark.asyncio
async def test_generate_insight_mock_mode(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    out = await daemon.generate_insight("OutboxPattern", ["[DECISION] a", "[DECISION] b"])
    assert "OutboxPattern" in out and "2" in out


# ── _fold_insight (stubbed end-to-end) ────────────────────────────────────────

def _fold_script():
    """Postgres responses in _fold_insight's execution order."""
    return [
        # 1. decision content fetch
        {"rowcount": 2, "rows": [
            (245, "Decision A\n\nrationale A", "shared-memory-GitHub"),
            (267, "Decision B\n\nrationale B", "tier3-cloe"),
        ]},
        # 2. fetch_insight_outbox_rows snapshot
        {"rowcount": 2, "rows": [(101,), (102,)]},
        # 3. write_insight_summary INSERT
        {"rowcount": 1, "rows": [(77,)]},
        # 4. write_insight_summary ledger flip
        {"rowcount": 2, "rows": []},
        # 5. supersession SELECT (one prior insight, same set)
        {"rowcount": 1, "rows": [(70, [245, 267])]},
        # 6. supersession UPDATE
        {"rowcount": 1, "rows": []},
        # 7. close_ledger_rows_by_id DELETE
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},
    ]


@pytest.mark.asyncio
async def test_fold_insight_full_path(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    outcome = {"pg_id": 245, "rating": "good", "date": "2026-06-10",
               "notes": "held under load"}
    daemon, session = daemon_with_fake_graph([FakeResult([outcome])])
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script())

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 267]) is True

    sqls = [s for s, _ in conn.executed]
    insert = next(s for s in sqls if s.startswith("INSERT INTO community_summaries"))
    assert "ON CONFLICT" not in insert
    # metadata carries the insight contract
    meta = json.loads(conn.executed[2][1][1])
    assert meta["kind"] == "insight"
    assert meta["source_pg_ids"] == [245, 267]
    assert sorted(meta["projects"]) == ["shared-memory-GitHub", "tier3-cloe"]
    # three commits: the read-tx close before the LLM call, the insight write
    # tx, and the one inside close_ledger_rows_by_id.
    assert conn.commits == 3
    # graph marking ran: consolidated flags + kind='insight' summary node
    mark_query = session.calls[-1][0]
    assert "SET d.consolidated = true" in mark_query or "SUPERSEDES" in mark_query


@pytest.mark.asyncio
async def test_fold_insight_aborts_when_llm_fails(monkeypatch):
    # No insight → no Postgres write, ledger rows stay open (durable retry).
    monkeypatch.delenv("MOCK_LLM", raising=False)
    outcome = {"pg_id": 245, "rating": "good", "date": "d", "notes": "n"}
    daemon, _ = daemon_with_fake_graph([FakeResult([outcome])])
    daemon.generate_insight = AsyncMock(return_value=None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script())

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 267]) is False

    assert not any(s.startswith("INSERT INTO community_summaries")
                   for s, _ in conn.executed)
    # Only the read-transaction close ran before the LLM aborted the fold.
    assert conn.commits == 1


@pytest.mark.asyncio
async def test_fold_insight_skips_singleton_cluster(monkeypatch):
    # Fewer than two source decisions found in Postgres → no fold (a solitary
    # decision round-trip is pure duplication — decision 245).
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    conn = StubConn(script=[{"rowcount": 1, "rows": [(245, "Decision A", "p1")]}])

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 999]) is False
    assert len(conn.executed) == 1  # only the content fetch ran
    assert conn.commits == 0


# ── run_insight_cycle wiring (regression for the fresh-cluster call site) ──────

@pytest.mark.asyncio
async def test_run_insight_cycle_calls_fold_with_compatible_signature(monkeypatch):
    """Regression — the fresh-cluster path in run_insight_cycle must call
    _fold_insight with arguments its signature accepts.

    A real bug (fixed 2026-06-23) passed projects=c.get("projects") to
    _fold_insight(), which has no such parameter, raising a TypeError that
    the cycle's try/except swallowed — so EVERY fresh fold crashed silently
    and insight stayed at 0 for ~12 days. The isolated _fold_insight tests
    above could not catch it because run_insight_cycle is the only place that
    call site is wired. create_autospec enforces the real signature, so
    reintroducing an unexpected kwarg makes the call raise (swallowed),
    dropping await_count to 0 and failing this test."""
    import consolidation_loop as cl
    from unittest.mock import create_autospec

    class _Conn(StubConn):
        def close(self):
            pass

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_decision_ids", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: [])

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "OutboxPattern", "decision_ids": [245, 267],
         "projects": ["shared-memory-GitHub", "tier3-cloe"]},
    ])
    daemon._fold_insight = create_autospec(daemon._fold_insight, return_value=True)

    await daemon.run_insight_cycle()

    assert daemon._fold_insight.await_count == 1
    assert "projects" not in daemon._fold_insight.await_args.kwargs
