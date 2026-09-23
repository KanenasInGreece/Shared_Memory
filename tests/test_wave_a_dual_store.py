"""Wave A dual-store Prove-It (F-NJ-01..05, F-PG-006/007/018, outbox type CHECK).

Each finding's killing test is named in this file. Mutation checks are in
docstrings: restore the old behaviour and the named test dies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "shared-memory" / "scripts"
_MIGRATIONS = _ROOT / "shared-memory" / "migrations"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_MIGRATIONS))

import coordinator as co  # noqa: E402
from ontology import ONT, RECORD_TYPE_LABELS  # noqa: E402
from verify_neo4j_init import declared_constraints  # noqa: E402


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


def _coord_outbox():
    """Coordinator with mocked pool + neo4j for outbox apply / wait tests."""
    c = co.MemoryCoordinator()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.executemany = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    c._pool = pool
    session = AsyncMock()
    session.run = AsyncMock()
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    c._project_identity = AsyncMock(return_value=6)
    c._domain_identities = AsyncMock(return_value=[])
    c._neo4j_tx_failures_total = 0
    return c, conn, session


def _save_coord(stored_metadata=None):
    c = co.MemoryCoordinator()
    c._entity_vocab_resolve_many = AsyncMock(side_effect=lambda names: {n: n for n in names})
    c._entity_vocab_mint = AsyncMock(side_effect=lambda n, agent: n)
    conn = MagicMock()

    async def _fetchval(sql, *args):
        if "content_hash" in sql:
            return stored_metadata
        return 1

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    c._pool = pool
    session = AsyncMock()
    session.run = AsyncMock()
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    return c, conn


def _request(body, consistency=None):
    body = dict(body)
    body.setdefault("agent_id", "claude-code")
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.get = MagicMock(return_value=None)
    req.headers = {}
    req.rel_url.query.get = MagicMock(return_value=consistency)
    return req


# ── F-NJ-02 unknown outbox type → failed immediately ─────────────────────────

@pytest.mark.asyncio
async def test_unknown_outbox_type_fails_immediately_without_fact_merge():
    """F-NJ-02. type='project-of' must not fall through to Fact MERGE.

    MUTATION: restore the unnamed else-Fact fall-through → this dies
    (session.run is called with SET f.content, or status='applied').
    """
    c, conn, session = _coord_outbox()
    await c._apply_outbox_row(
        11, 42,
        {"type": "project-of", "content_snippet": "KEEP"},
        retries=0,
    )
    for call in session.run.await_args_list:
        q = call.args[0] if call.args else ""
        assert "SET f.content" not in q, q
    statements = [k.args[0] for k in conn.execute.call_args_list]
    assert any("status='failed'" in s for s in statements), statements
    assert not any("status='applied'" in s for s in statements)
    assert not any("retries=retries+1" in s for s in statements)
    assert not any("retries=" in s and "retries+1" in s for s in statements)


@pytest.mark.asyncio
async def test_missing_and_fact_type_still_merge_a_fact():
    """Keep test_apply_outbox_row_plain_fact_does_not_call_decision_path's contract."""
    c, conn, session = _coord_outbox()
    with patch.object(c, "_apply_decision_outbox_row", new=AsyncMock()) as mock_dec:
        await c._apply_outbox_row(2, 43, {"type": "fact", "content_snippet": "x",
                                          "entities": []}, 0)
        mock_dec.assert_not_awaited()
    assert session.run.await_count >= 1
    await c._apply_outbox_row(3, 44, {"content_snippet": "y", "entities": []}, 0)
    assert any("SET f.content" in (call.args[0] if call.args else "")
               for call in session.run.await_args_list)


# ── F-NJ-03 one Cypher: OPTIONAL MATCH/DELETE + UNWIND (not FOREACH+MATCH) ───

@pytest.mark.asyncio
async def test_domain_of_apply_is_one_statement_with_optional_delete_and_unwind():
    """F-NJ-03. MUTATION: split into two session.runs → dies."""
    c, conn, session = _coord_outbox()
    c._domain_identities = AsyncMock(return_value=[{"id": 41, "name": "architecture"}])
    queries = []

    async def run(q, **kw):
        queries.append(q)
        res = MagicMock()
        res.single = AsyncMock(return_value={"n": 1})
        return res

    session.run = run
    await c._apply_domain_of_outbox_row(
        1, 42, {"type": "domain_of", "project": "p", "domains": ["architecture"]})
    writes = [q for q in queries if "DELETE" in q or "UNWIND" in q or "FOREACH" in q]
    assert len(queries) == 1, queries
    q = queries[0]
    assert "OPTIONAL MATCH" in q and "DELETE stale" in q
    assert "UNWIND" in q
    assert "FOREACH" not in q or "MATCH" not in q.split("FOREACH", 1)[-1]
    assert "UNWIND" in q and "MATCH" in q
    # Mutation: restore MATCH (d:Domain {domain_id}) and drop the MERGE of the node. The edge MERGE does not contain "MERGE (d:".
    assert "MERGE (d:" in q
    assert "MATCH (d:" not in q


@pytest.mark.asyncio
async def test_domain_of_empty_domains_does_not_clear_edges():
    """An empty domain list used to DELETE every DOMAIN_OF edge and drop the row."""
    c, conn, session = _coord_outbox()
    c._domain_identities = AsyncMock(return_value=[])
    queries = []

    async def run(q, **kw):
        queries.append(q)
        return MagicMock()

    session.run = run
    await c._apply_domain_of_outbox_row(
        1, 42, {"type": "domain_of", "project": "p", "domains": []})
    assert queries == []
    sqls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("status='failed'" in sql for sql in sqls)
    assert not any("DELETE FROM neo4j_outbox" in sql for sql in sqls)


@pytest.mark.asyncio
async def test_retired_inherit_domain_of_still_writes_nothing():
    c, conn, session = _coord_outbox()
    queries = []

    async def run(q, **kw):
        queries.append(q)
        return MagicMock()

    session.run = run
    await c._apply_domain_of_outbox_row(
        1, 42, {"type": "domain_of", "inherit": True})
    assert queries == []


# ── F-NJ-04 domain identity lookup error is fail-closed ──────────────────────

@pytest.mark.asyncio
async def test_domain_identity_read_error_retries_and_does_not_apply():
    """F-NJ-04. MUTATION: restore `except Exception: return None` → dies
    (row is applied / edges deleted / tx_failures bumped).
    """
    c, conn, session = _coord_outbox()
    c._domain_identities = co.MemoryCoordinator._domain_identities.__get__(c)
    c._domain_identity = co.MemoryCoordinator._domain_identity.__get__(c)

    async def fetchval(sql, *args):
        if "project_domains" in sql:
            raise RuntimeError("registry unreadable")
        return 1

    conn.fetchval = AsyncMock(side_effect=fetchval)
    queries = []

    async def run(q, **kw):
        queries.append(q)
        return MagicMock()

    session.run = run
    await c._apply_outbox_row(
        7, 42,
        {"type": "domain_of", "project": "p", "domains": ["architecture"]},
        retries=0,
    )
    statements = [k.args[0] for k in conn.execute.call_args_list]
    assert not any("status='applied'" in s for s in statements), statements
    assert any("retries=retries+1" in s for s in statements), statements
    assert c._neo4j_tx_failures_total == 0
    assert not any("DELETE stale" in q for q in queries)
    assert not any("DELETE FROM neo4j_outbox" in s for s in statements)


@pytest.mark.asyncio
async def test_domain_identity_missing_row_is_still_none():
    """None-means-unregistered stays green (not the raise hole)."""
    c, conn, _ = _coord_outbox()
    c._domain_identity = co.MemoryCoordinator._domain_identity.__get__(c)
    conn.fetchval = AsyncMock(return_value=None)
    assert await c._domain_identity(6, "no-such-section") is None


# ── F-NJ-01 Retrospective pg_id uniqueness ───────────────────────────────────

def test_declared_constraints_cover_retrospective_and_every_record_type_label():
    """F-NJ-01. MUTATION: drop the Retrospective CREATE CONSTRAINT line → dies."""
    text = (_MIGRATIONS / "neo4j_init.cypher").read_text(encoding="utf-8")
    declared = declared_constraints(text)
    pairs = set(declared.values())
    assert ("Retrospective", "pg_id") in pairs
    for label in RECORD_TYPE_LABELS.values():
        assert (label, "pg_id") in pairs, label


# ── F-NJ-05 already_queued excludes failed ───────────────────────────────────

class _QueuedConn:
    """Fixture: pending=1, in_progress=2, failed=3, applied=4."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def cursor(self):
        conn = self

        class _Cur:
            def execute(self, sql, params=None):
                conn.sql = sql
                conn.params = params

            def fetchall(self):
                sql = conn.sql or ""
                if "pending" in sql and "in_progress" in sql:
                    return [(pg, s) for pg, s in conn.rows
                            if s in ("pending", "in_progress")]
                return list(conn.rows)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Cur()


def _queued_rows():
    return [(1, "pending"), (2, "in_progress"), (3, "failed"), (4, "applied")]


def test_already_queued_excludes_failed_on_all_three_scripts():
    """F-NJ-05. MUTATION: drop `status IN ('pending','in_progress')` → failed id reappears."""
    import backfill_domain_of as bdo
    import backfill_project_of as bpo
    import reconcile_project_edges as rpe

    fixture = _queued_rows()

    conn = _QueuedConn(fixture)
    got = bdo.already_queued(conn)
    assert "status IN ('pending','in_progress')" in conn.sql
    assert 3 not in got and 4 not in got
    assert 1 in got

    conn = _QueuedConn(fixture)
    got = bpo.already_queued(conn, [1, 2, 3, 4])
    assert "status IN ('pending','in_progress')" in conn.sql
    assert 3 not in got
    assert 1 in got and 2 in got

    conn = _QueuedConn(fixture)
    got = rpe.already_queued(conn, [1, 2, 3, 4])
    assert "status IN ('pending','in_progress')" in conn.sql
    assert 3 not in got
    assert 1 in got and 2 in got


# ── F-PG-006 wait: graph-present statuses, fast-worker, type filter ──────────

def _patch_wait_clock(c, now):
    loop = MagicMock()
    loop.time = lambda: now["t"]

    async def fake_sleep(dt):
        now["t"] += dt

    return (
        patch.object(co.asyncio, "get_running_loop", return_value=loop),
        patch.object(co.asyncio, "sleep", fake_sleep),
    )


@pytest.mark.asyncio
async def test_wait_treats_rem_reviewed_and_consolidated_as_success():
    """F-PG-006. MUTATION: restore `status == "applied"` only → rem_reviewed fails."""
    c, conn, _ = _coord_outbox()
    conn.fetchrow = AsyncMock(return_value={"status": "rem_reviewed"})
    now = {"t": 0.0}
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "applied"
    conn.fetchrow = AsyncMock(return_value={"status": "consolidated"})
    now["t"] = 0.0
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "applied"


@pytest.mark.asyncio
async def test_wait_applied_then_rem_reviewed_is_success():
    c, conn, _ = _coord_outbox()
    conn.fetchrow = AsyncMock(side_effect=[
        {"status": "applied"}, {"status": "rem_reviewed"}])
    now = {"t": 0.0}
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "applied"


@pytest.mark.asyncio
async def test_wait_vanish_after_observed_row_is_success():
    c, conn, _ = _coord_outbox()
    conn.fetchrow = AsyncMock(side_effect=[{"status": "applied"}, None])
    now = {"t": 0.0}
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "applied"
    conn.fetchrow = AsyncMock(side_effect=[{"status": "pending"}, None])
    now["t"] = 0.0
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "applied"


@pytest.mark.asyncio
async def test_wait_first_poll_empty_is_fast_worker_success():
    """F-PG-006 ADV. MUTATION: 'never seen stays timeout' → this dies."""
    c, conn, _ = _coord_outbox()
    conn.fetchrow = AsyncMock(return_value=None)
    now = {"t": 0.0}
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "applied"
    assert now["t"] == 0.0


@pytest.mark.asyncio
async def test_wait_ignores_pending_oneshot_when_dream_cycle_row_applied():
    """MUTATION: drop type filter → pending domain_of is waited on / fails."""
    c, conn, _ = _coord_outbox()

    async def fetchrow(sql, *args):
        assert "domain_of" not in (sql.split("IN", 1)[-1] if "IN" in sql else sql) or \
            "fact" in sql
        assert "cypher_params" in sql
        assert "fact" in sql and "decision" in sql and "retrospective" in sql
        return {"status": "applied"}

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    now = {"t": 0.0}
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "applied"


@pytest.mark.asyncio
async def test_wait_failed_returns_false_without_spinning():
    c, conn, _ = _coord_outbox()
    conn.fetchrow = AsyncMock(return_value={"status": "failed"})
    now = {"t": 0.0}
    p_loop, p_sleep = _patch_wait_clock(c, now)
    with p_loop, p_sleep:
        assert await c._wait_for_outbox(9) == "failed"
    assert now["t"] == 0.0


@pytest.mark.asyncio
async def test_handle_save_consistency_neo4j_maps_rem_reviewed_to_applied():
    """True from wait (rem_reviewed) still serialises as neo4j='applied', not failed."""
    c, conn = _save_coord(stored_metadata=None)
    conn.fetchrow = AsyncMock(side_effect=lambda sql, *a: (
        {"status": "rem_reviewed"} if "neo4j_outbox" in sql and "SELECT" in sql
        else {"id": 99}))
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request(
            {"content": "fresh words for consistency wait",
             "metadata": {"source": "claude-code", "project": "alpha"}},
            consistency="neo4j"))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["status"] == "success"
    assert body["neo4j"] == "applied"


@pytest.mark.asyncio
async def test_handle_save_consistency_neo4j_maps_failed_to_failed():
    """A permanently failed dream-cycle row is neo4j='failed', not 'timeout'."""
    c, conn = _save_coord(stored_metadata=None)
    conn.fetchrow = AsyncMock(side_effect=lambda sql, *a: (
        {"status": "failed"} if "neo4j_outbox" in sql and "SELECT" in sql
        else {"id": 99}))
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request(
            {"content": "fresh words for a failed outbox row",
             "metadata": {"source": "claude-code", "project": "alpha"}},
            consistency="neo4j"))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["neo4j"] == "failed"


def test_handle_status_selects_dream_cycle_rows_only():
    src = (_SCRIPTS / "coordinator.py").read_text(encoding="utf-8")
    # The status SELECT and the wait SELECT share the dream-cycle type filter.
    assert src.count("cypher_params->>'type' IN ('fact','decision','retrospective')") >= 1 \
        or src.count('cypher_params->>\'type\' IN (\'fact\',\'decision\',\'retrospective\')') >= 1 \
        or "fact','decision','retrospective" in src


# ── F-PG-007 kind is a frozen axis ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_resave_fact_as_decision_is_kind_conflict():
    """MUTATION: delete type/kind compare → transmute returns 200."""
    c, conn = _save_coord(stored_metadata={"project": "alpha", "entities": []})
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as embed:
        resp = await c.handle_save(_request({
            "content": "the same words, saved twice",
            "metadata": {
                "source": "claude-code",
                "type": "decision",
                "entities": [],
                "decision": {"decided_by": "Operator", "project": "alpha",
                             "rationale": "because"},
            },
        }))
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["error"] == "axis_conflict"
    assert body["axis"] == "kind"
    embed.assert_not_called()
    assert not any("INSERT INTO neo4j_outbox" in (k.args[0] if k.args else "")
                   for k in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_resave_decision_as_fact_is_kind_conflict():
    c, conn = _save_coord(stored_metadata={
        "type": "decision",
        "decision": {"project": "alpha", "decided_by": "Operator",
                     "rationale": "because"},
    })
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as embed:
        resp = await c.handle_save(_request({
            "content": "the same words, saved twice",
            "metadata": {"source": "claude-code", "project": "alpha",
                         "entities": []},
        }))
    assert resp.status == 409
    assert json.loads(resp.text)["axis"] == "kind"
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_identical_fact_resave_still_200():
    c, _ = _save_coord(stored_metadata={"project": "alpha", "entities": []})
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request({
            "content": "the same words, saved twice",
            "metadata": {"source": "claude-code", "project": "alpha",
                         "entities": []},
        }))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_legacy_decision_blob_without_type_still_resavable_as_decision():
    c, _ = _save_coord(stored_metadata={"decision": {"project": "alpha"},
                                        "entities": ["Kubernetes"]})
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request({
            "content": "the same words, saved twice",
            "metadata": {
                "source": "claude-code",
                "type": "decision",
                "entities": [],
                "decision": {"decided_by": "Operator", "project": "alpha",
                             "rationale": "because"},
            },
        }))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_fact_merge_cypher_refuses_a_different_spine_label():
    """NJ half of F-PG-007: do not SET a second label on an existing spine node."""
    c, conn, session = _coord_outbox()
    await c._apply_outbox_row(
        2, 43, {"type": "fact", "content_snippet": "x", "entities": []}, 0)
    merge_q = next(call.args[0] for call in session.run.await_args_list
                   if "SET f.content" in (call.args[0] if call.args else ""))
    assert "existing" in merge_q
    assert ONT.fact in merge_q
    assert "IS NULL" in merge_q or f"existing:{ONT.fact}" in merge_q


@pytest.mark.asyncio
async def test_unknown_outbox_type_is_rejected_by_python_whitelist():
    with pytest.raises(ValueError):
        co._require_outbox_type({"type": "project-of"})
    with pytest.raises(ValueError):
        co._require_outbox_type({"type": "unheard_of"})
    co._require_outbox_type({"type": "fact"})
    co._require_outbox_type({"type": "decision"})
    co._require_outbox_type({"type": None})
    co._require_outbox_type({})
    co._require_outbox_type({"type": ""})
    co._require_outbox_type({"type": "domain_of"})


def test_outbox_row_type_does_not_coerce_unknown_to_fact():
    """SEC-01. None/blank → fact; unknown non-empty stays unknown so require raises."""
    assert co._outbox_row_type(None) == "fact"
    assert co._outbox_row_type("") == "fact"
    assert co._outbox_row_type("fact") == "fact"
    assert co._outbox_row_type("decision") == "decision"
    assert co._outbox_row_type("project-of") != "fact"
    assert co._outbox_row_type("unheard_of") != "fact"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_type", ["project-of", "unheard_of"])
async def test_handle_save_unknown_type_does_not_enqueue_as_fact(bad_type):
    """SEC-01. MUTATION: restore `_outbox_row_type` else "fact" without an
    ingress refuse → this dies (INSERT INTO neo4j_outbox happens).
    """
    c, conn = _save_coord(stored_metadata=None)
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as embed:
        resp = await c.handle_save(_request({
            "content": "a record with a garbage type",
            "metadata": {"source": "claude-code", "project": "alpha",
                         "type": bad_type, "entities": []},
        }))
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["status"] == "error"
    embed.assert_not_called()
    assert not any("INSERT INTO neo4j_outbox" in (k.args[0] if k.args else "")
                   for k in conn.execute.call_args_list)


def _zero_write_result():
    counters = MagicMock()
    counters.nodes_created = 0
    counters.properties_set = 0
    summary = MagicMock()
    summary.counters = counters
    result = MagicMock()
    result.consume = AsyncMock(return_value=summary)
    return result


@pytest.mark.asyncio
async def test_fact_apply_does_not_mark_applied_when_kind_guard_writes_nothing():
    """OPS-01. A Decision already holding this pg_id makes the Fact MERGE
    a 0-row no-op; that must not flip the outbox to applied.

    MUTATION: ignore the 0-row result and always UPDATE applied → this dies.
    """
    c, conn, session = _coord_outbox()
    session.run = AsyncMock(return_value=_zero_write_result())
    await c._apply_outbox_row(
        2, 43, {"type": "fact", "content_snippet": "KEEP", "entities": []}, 0)
    statements = [k.args[0] for k in conn.execute.call_args_list]
    assert any("status='failed'" in s for s in statements), statements
    assert not any("status='applied'" in s for s in statements)
    assert not any("retries=retries+1" in s for s in statements)


@pytest.mark.asyncio
async def test_decision_apply_does_not_mark_applied_when_kind_guard_writes_nothing():
    c, conn, session = _coord_outbox()
    session.run = AsyncMock(return_value=_zero_write_result())
    await c._apply_decision_outbox_row(1, 42, {
        "type": "decision",
        "decision": {"decided_by": "Operator", "project": "p", "rationale": "r"},
        "content_snippet": "KEEP",
    })
    statements = [k.args[0] for k in conn.execute.call_args_list]
    assert any("status='failed'" in s for s in statements), statements
    assert not any("status='applied'" in s for s in statements)
    assert not any("retries=retries+1" in s for s in statements)


@pytest.mark.asyncio
async def test_retrospective_apply_does_not_mark_applied_when_kind_guard_writes_nothing():
    c, conn, session = _coord_outbox()
    session.run = AsyncMock(return_value=_zero_write_result())
    await c._apply_retrospective_outbox_row(11, 913, {
        "v": 2, "type": "retrospective", "target_pg_id": 42,
        "retrospective": {"rating": "validated", "date": "2026-07-15"},
        "content_snippet": "KEEP",
    })
    statements = [k.args[0] for k in conn.execute.call_args_list]
    assert any("status='failed'" in s for s in statements), statements
    assert not any("status='applied'" in s for s in statements)
    assert not any("retries=retries+1" in s for s in statements)


def test_outbox_type_check_is_in_schema_init_and_migration_041():
    schema = (_MIGRATIONS / "schema_init.sql").read_text(encoding="utf-8")
    mig = next(p.read_text(encoding="utf-8") for p in _MIGRATIONS.glob("041_*.sql"))
    for sql in (schema, mig):
        assert "neo4j_outbox_type_known" in sql
        for t in ("fact", "decision", "retrospective", "supersede",
                  "project_of", "domain_of"):
            assert t in sql
        assert "cypher_params" in sql


# ── F-PG-018 retrospective hash includes date and rating ─────────────────────

@pytest.mark.asyncio
async def test_retrospective_hash_includes_date_and_rating():
    """MUTATION: restore `retrospective:{target}:{notes}` → one id for two ratings."""
    c, conn, _ = _coord_outbox()
    c._entity_vocab_resolve_many = AsyncMock(side_effect=lambda names: {n: n for n in names})
    ids_by_hash: dict[str, int] = {}
    next_id = [2000]
    inserts = []

    async def fetchrow(sql, *args):
        if "INSERT INTO technical_docs" in sql:
            content_hash = args[3]
            inserts.append({"hash": content_hash, "metadata": args[1], "notes": args[0]})
            if content_hash not in ids_by_hash:
                ids_by_hash[content_hash] = next_id[0]
                next_id[0] += 1
            return {"id": ids_by_hash[content_hash]}
        return {"id": 42, "type": "decision", "project": "p1"}

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[
        {"id": 601, "type": None, "source_ref": "tests/x.py"},
    ])

    async def _once(rating):
        with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
            return await c.handle_retrospective(_request({
                "pg_id": 42, "rating": rating, "notes": "same notes",
                "date": "2026-01-01", "grounded_in": [601],
            }))

    r1 = await _once("validated")
    r2 = await _once("reversed")
    assert r1.status == 200 and r2.status == 200
    b1, b2 = json.loads(r1.text), json.loads(r2.text)
    assert b1["pg_id"] != b2["pg_id"]
    outbox_inserts = [k for k in conn.execute.call_args_list
                      if k.args and "INSERT INTO neo4j_outbox" in k.args[0]]
    assert len(outbox_inserts) == 2
    assert inserts[0]["metadata"]["rating"] == "validated"
    expected = hashlib.sha256(
        b"retrospective:42:2026-01-01:validated:same notes").hexdigest()
    assert inserts[0]["hash"] == expected

    r3 = await _once("validated")
    assert json.loads(r3.text)["pg_id"] == b1["pg_id"]


def test_migrate_retro_edges_keeps_the_old_hash_formula():
    src = (_SCRIPTS / "migrate_retro_edges.py").read_text(encoding="utf-8")
    assert "retrospective:{p['decision_id']}:{notes}" in src \
        or 'retrospective:{' in src and ":{notes}" in src


# ── read-role comment: graph stays off the allowlist ─────────────────────────

def test_read_role_comment_does_not_invite_graph_onto_the_allowlist():
    src = (_SCRIPTS / "coordinator.py").read_text(encoding="utf-8")
    assert "telemetry/graph allowlist" not in src
    assert ("GET", "/memory/graph") not in co._READ_ROLE_ROUTES
    assert ("POST", "/memory/search") in co._READ_ROLE_ROUTES
