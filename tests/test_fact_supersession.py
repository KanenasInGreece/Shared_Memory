"""Tests for fact supersession (decisions 381, 384, 389).

Covers:
  - handle_supersede: validation (int / self / not-found / already) + bare-retract
    success path (UPDATE + outbox mirror row + immediate GC purge of the orphan row)
  - handle_save: `supersedes` ingress validation + atomic flag + outbox piggyback
  - handle_review_hold: validation + appends metadata.reviewed_supersessions (8e)
  - _apply_outbox_row fact branch: piggybacked supersession MERGE mirror
  - _apply_supersede_outbox_row: one-shot mirror that self-deletes its row
  - consolidation_loop (SQL contract, stub conn):
      * close_ledger_rows transitive superseded-predecessor purge + logging (389 GC)
      * fetch_ledger_backlog excludes superseded facts (census safety)
      * _FACT_ROW excludes 'supersede' rows
"""
import importlib.util
import json
import os
import sys

import pytest

# ── Dynamic import (mirrors test_coordinator.py) ──────────────────────────────

def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from consolidation_loop import close_ledger_rows, fetch_ledger_backlog, _FACT_ROW  # noqa: E402


class _async_ctx:
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


def _make_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: d)
    req.__getitem__ = MagicMock(side_effect=lambda k: None)
    return req


def _coord():
    """MemoryCoordinator with mocked pool + neo4j. Caller tunes conn.* mocks."""
    c = MemoryCoordinator()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    # fetchval also answers the project-registry lookup at ingress (v0.8.33);
    # 1 = 'this project is registered', so these tests reach the behaviour
    # they are actually about instead of stopping at the project gate.
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_async_ctx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    c._pool = pool
    session = AsyncMock()
    session.run = AsyncMock()
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_async_ctx(session))
    c._neo4j = neo4j
    return c, conn, session


def _executed_sql(conn):
    return [str(call.args[0]) for call in conn.execute.call_args_list]


# ── handle_supersede — validation ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supersede_requires_int_pg_id():
    c, _, _ = _coord()
    resp = await c.handle_supersede(_make_request({"pg_id": "5"}))
    assert resp.status == 400
    assert "pg_id" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_supersede_by_must_be_int():
    c, _, _ = _coord()
    resp = await c.handle_supersede(_make_request({"pg_id": 5, "by": "x"}))
    assert resp.status == 400
    assert "by must be" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_supersede_cannot_supersede_itself():
    c, _, _ = _coord()
    resp = await c.handle_supersede(_make_request({"pg_id": 5, "by": 5}))
    assert resp.status == 400
    assert "itself" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_supersede_target_not_found():
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value=None)
    resp = await c.handle_supersede(_make_request({"pg_id": 5}))
    assert resp.status == 400
    assert "not found" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_supersede_already_superseded():
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"superseded": True, "type": None})
    resp = await c.handle_supersede(_make_request({"pg_id": 5}))
    assert resp.status == 400
    assert "already superseded" in json.loads(resp.text)["message"]


# ── handle_supersede — bare retract success + GC ──────────────────────────────

@pytest.mark.asyncio
async def test_supersede_bare_retract_flags_writes_outbox_and_purges():
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": None})
    conn.fetch = AsyncMock(return_value=[(101,)])  # one orphan fact row purged
    resp = await c.handle_supersede(_make_request({"pg_id": 5}))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["superseded"] == 5
    assert body["superseded_by"] is None
    assert body["purged_outbox"] == 1
    sqls = " | ".join(_executed_sql(conn))
    assert "UPDATE technical_docs SET superseded = true, superseded_by = $2" in sqls
    assert "INSERT INTO neo4j_outbox" in sqls
    # the orphan-row purge is a DELETE ... RETURNING, issued via conn.fetch
    fetch_sqls = " | ".join(str(call.args[0]) for call in conn.fetch.call_args_list)
    assert "DELETE FROM neo4j_outbox WHERE pg_id = $1" in fetch_sqls
    # outbox mirror row carries the supersede type
    mirror = next(call for call in conn.execute.call_args_list
                  if "INSERT INTO neo4j_outbox" in str(call.args[0]))
    assert mirror.args[2]["type"] == "supersede"
    assert mirror.args[2]["old_pg_id"] == 5


@pytest.mark.asyncio
async def test_supersede_with_live_successor_rides_along_no_purge():
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": None})
    # successor exists (fetchval #1) AND has a live fact outbox row (fetchval #2)
    conn.fetchval = AsyncMock(side_effect=[1, 1])
    resp = await c.handle_supersede(_make_request({"pg_id": 5, "by": 9}))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["superseded_by"] == 9
    assert body["purged_outbox"] == 0      # rides along; no eager purge
    assert not any("DELETE FROM neo4j_outbox WHERE pg_id" in s for s in _executed_sql(conn))


# ── handle_save — supersedes ingress ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_supersedes_must_be_int():
    c, _, _ = _coord()
    req = _make_request({"content": "x", "metadata": {"project": "shared-memory-GitHub", "source": "claude", "supersedes": "5"}})
    resp = await c.handle_save(req)
    assert resp.status == 400
    assert "supersedes must be an integer" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_save_supersedes_target_not_found():
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value=None)
    req = _make_request({"content": "x", "metadata": {"project": "shared-memory-GitHub", "source": "claude", "supersedes": 7}})
    resp = await c.handle_save(req)
    assert resp.status == 400
    assert "not found" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_save_supersedes_success_flags_and_piggybacks():
    c, conn, _ = _coord()
    # 1st fetchrow = supersedes target check; 2nd = INSERT RETURNING id
    conn.fetchrow = AsyncMock(side_effect=[{"superseded": False, "type": None}, {"id": 100}])
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({"content": "corrected",
                             "metadata": {"project": "shared-memory-GitHub", "source": "claude", "entities": ["X"],
                                          "supersedes": 7}})
        resp = await c.handle_save(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["superseded"] == 7
    sqls = " | ".join(_executed_sql(conn))
    assert "UPDATE technical_docs SET superseded = true, superseded_by = $2" in sqls
    # the new fact's outbox row carries the supersedes pointer (piggyback)
    outbox = next(call for call in conn.execute.call_args_list
                  if "INSERT INTO neo4j_outbox" in str(call.args[0]))
    assert outbox.args[2]["supersedes"] == 7


# ── handle_review_hold (8e) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_hold_rejects_non_source_fact():
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"source_pg_ids": [1, 2], "metadata": {}})
    resp = await c.handle_review_hold(_make_request({"summary_id": 3, "pg_id": 99}))
    assert resp.status == 400
    assert "not a source" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_review_hold_appends_reviewed_supersession():
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"source_pg_ids": [5], "metadata": {}})
    conn.fetchval = AsyncMock(return_value=6)  # 5.superseded_by = 6
    resp = await c.handle_review_hold(_make_request({"summary_id": 3, "pg_id": 5}))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["reviewed"] == {"old": 5, "by": 6}
    upd = next(call for call in conn.execute.call_args_list
               if "UPDATE community_summaries" in str(call.args[0]))
    # jsonb_set writes only the reviewed_supersessions array (args[2]), in-place
    assert "jsonb_set" in str(upd.args[0])
    assert {"old": 5, "by": 6} in upd.args[2]


# ── outbox mirror application ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fact_outbox_piggyback_marks_old_and_links_supersedes():
    c, conn, session = _coord()
    await c._apply_outbox_row(1, 10, {"entities": [], "supersedes": 5}, 0)
    cyphers = " || ".join(str(call.args[0]) for call in session.run.call_args_list)
    assert "superseded = true" in cyphers
    assert "SUPERSEDES" in cyphers


@pytest.mark.asyncio
async def test_supersede_outbox_row_self_deletes():
    c, conn, session = _coord()
    await c._apply_supersede_outbox_row(1, {"old_pg_id": 5, "new_pg_id": 10})
    cyphers = [str(call.args[0]) for call in session.run.call_args_list]
    # ensure-old, mark-old, ensure-new, link — four statements, and the mark and
    # link steps MATCH across every spine label so a judgement's pg_id can never
    # be answered by a phantom :Fact minted beside the real node.
    assert len(cyphers) == 4
    assert any("SUPERSEDES" in q for q in cyphers)
    spine = "Fact|Decision|Retrospective"
    assert spine in cyphers[1] and "SET o.superseded = true" in cyphers[1]
    assert spine in cyphers[3]
    # the placeholder is only ever minted behind a "no spine node exists" guard
    for q in (cyphers[0], cyphers[2]):
        assert "MERGE (p:Fact {pg_id: $pg_id})" in q
        assert "size(ns) = 0" in q
    assert any("DELETE FROM neo4j_outbox WHERE id" in str(call.args[0])
               for call in conn.execute.call_args_list)


# ── consolidation_loop SQL contracts (stub conn) ──────────────────────────────

class _StubCursor:
    def __init__(self, script, executed):
        self._script = script
        self.executed = executed
        self._cur = {"rowcount": 0, "rows": []}
    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self._cur = self._script.pop(0) if self._script else {"rowcount": 0, "rows": []}
    @property
    def rowcount(self):
        return self._cur["rowcount"]
    def fetchall(self):
        return self._cur["rows"]
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class _StubConn:
    def __init__(self, script=None):
        self._script = script or []
        self.executed = []
        self.commits = 0
    def cursor(self):
        return _StubCursor(self._script, self.executed)
    def commit(self):
        self.commits += 1


def test_close_ledger_transitively_purges_superseded_predecessors(caplog):
    import logging
    # 1st execute = delete consolidated successor rows; 2nd = recursive predecessor purge
    conn = _StubConn(script=[
        {"rowcount": 1, "rows": [(101, 10)]},   # consolidated successor pg_id=10
        {"rowcount": 1, "rows": [(201, 5)]},    # superseded predecessor pg_id=5 purged
    ])
    with caplog.at_level(logging.INFO, logger="ConsolidationDaemon"):
        assert close_ledger_rows(conn, [10]) == 1   # returns successor count
    purge_sql, params = conn.executed[1]
    assert "RECURSIVE preds" in purge_sql
    assert "superseded_by" in purge_sql
    assert params == ([10],)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "superseded-predecessor" in msg
    assert "pg_id=5" in msg


def test_close_ledger_no_purge_when_no_successor_deleted():
    # No consolidated rows → predecessor purge must not run
    conn = _StubConn(script=[{"rowcount": 0, "rows": []}])
    assert close_ledger_rows(conn, [10]) == 0
    assert len(conn.executed) == 1   # only the delete, no recursive purge


def test_ledger_backlog_excludes_superseded_facts():
    conn = _StubConn(script=[{"rowcount": 0, "rows": []}])
    fetch_ledger_backlog(conn)
    sql, _ = conn.executed[0]
    assert "LEFT JOIN technical_docs" in sql
    assert "COALESCE(t.superseded, false) = false" in sql


def test_fact_row_filter_excludes_supersede_type():
    assert "supersede" in _FACT_ROW
    assert "retrospective" in _FACT_ROW and "decision" in _FACT_ROW
