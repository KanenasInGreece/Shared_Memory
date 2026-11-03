"""
Unit tests verifying Part A and Part F invariants for v1.0.45.

Invariants tested:
- I1: Shared lock held at every daemon store write, exemption named.
  (Failure: leave REM recovery write outside; skip reacquire)
- I2: No shared lock during a chat call.
  (Failure: skip the release)
- I3: A reply arriving mid-dump is written after the dump, not discarded.
  (Failure: try-lock on reacquire)
- I4: Lock conn dead during a call: reconnect, or write nothing with no paid row.
  (Failure: ignore the failure; let it become unparseable)
- I10: After a reconnect, a later exclusive acquire succeeds once the cycle ends;
       a failed reacquire aborts the cycle.
  (Failure: close original conn only; continue the cycle)
- I9: One member set is bought at most once per cycle across both paths.
  (Failure: separate registries)

All Neo4j and Postgres I/O is mocked; no live databases or external services.
"""

import asyncio
import os
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch, call
import httpx
import pytest

_SCRIPTS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import rem_loop
import consolidation_loop
from rem_loop import (
    REMDaemon,
    _BackupLockHolder,
    _ChatLockReleaseContext,
    LockReacquireFailed,
    BACKUP_ADVISORY_LOCK_KEY,
)
from consolidation_loop import (
    ConsolidationDaemon,
    _fold_backlog_key,
)


# ── Fixtures & Mocks ──────────────────────────────────────────────────────────

class _AsyncIter:
    def __aiter__(self):
        return self
    async def __anext__(self):
        raise StopAsyncIteration


class _AsyncCtx:
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *args):
        pass


def _make_mock_conn():
    conn = MagicMock()
    conn.closed = False
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


# ── Invariant I1: Shared lock held at store write, exemption named ────────────

@pytest.mark.asyncio
async def test_i1_rem_recovery_write_runs_inside_shared_backup_lock(monkeypatch):
    """Invariant I1: a daemon holds the shared lock at every Postgres and Neo4j
    write. In particular, rem_loop's _release_eligible_write_failed must execute
    AFTER taking the shared backup lock, never before it.
    Failure to re-introduce: leave the REM recovery write outside.
    """
    daemon = REMDaemon.__new__(REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    daemon._lock_holder = None

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncIter())
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_AsyncCtx(mock_session))
    daemon.driver = mock_driver

    order = []

    def _fake_take_lock(conn):
        order.append("take_shared_lock")
        return True

    async def _fake_release_eligible():
        order.append("release_eligible_write_failed")

    monkeypatch.setattr(rem_loop, "_take_shared_backup_lock", _fake_take_lock)
    daemon._release_eligible_write_failed = _fake_release_eligible
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._fetch_non_rem_batch = AsyncMock(return_value=([], {}, {}))

    mock_conn, _ = _make_mock_conn()
    daemon._open_pg_conn = lambda: mock_conn

    processed, attempted = await daemon.run_cycle()

    assert "take_shared_lock" in order
    assert "release_eligible_write_failed" in order
    lock_idx = order.index("take_shared_lock")
    recovery_idx = order.index("release_eligible_write_failed")
    assert recovery_idx > lock_idx, (
        f"_release_eligible_write_failed ({recovery_idx}) must execute AFTER "
        f"_take_shared_backup_lock ({lock_idx})"
    )
    assert processed == 0
    assert attempted == 0
    assert daemon._lock_holder is None, "holder must be reset on early return"
    mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_d1_rem_recovery_write_runs_even_with_no_candidates(monkeypatch):
    """D1: _release_eligible_write_failed must run even when _fetch_non_rem_batch
    returns 0 candidates (no candidates -> recovery write still runs).
    If it returned early before _release_eligible_write_failed, recovery was stranded.
    Also verifies holder is closed and _lock_holder is reset to None on the early return path.
    """
    daemon = REMDaemon.__new__(REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    daemon._lock_holder = None

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncIter())
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_AsyncCtx(mock_session))
    daemon.driver = mock_driver

    recovery_ran = False
    async def _fake_recovery():
        nonlocal recovery_ran
        recovery_ran = True

    daemon._release_eligible_write_failed = _fake_recovery
    daemon._recent_write_happened = AsyncMock(return_value=False)
    # Zero candidates:
    daemon._fetch_non_rem_batch = AsyncMock(return_value=([], {}, {}))

    mock_conn, _ = _make_mock_conn()
    daemon._open_pg_conn = lambda: mock_conn
    monkeypatch.setattr(rem_loop, "_take_shared_backup_lock", lambda c: True)

    processed, attempted = await daemon.run_cycle()

    assert processed == 0
    assert attempted == 0
    assert recovery_ran is True, (
        "_release_eligible_write_failed must run even when there are no candidates"
    )
    assert daemon._lock_holder is None, "_lock_holder must reset to None"
    mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_d1_rem_read_failure_returns_0_0_and_cleans_up(monkeypatch):
    """D1: A read failure during _fetch_non_rem_batch logs a warning, returns 0, 0,
    closes holder, and resets _lock_holder to None."""
    daemon = REMDaemon.__new__(REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    daemon._lock_holder = None

    daemon._release_eligible_write_failed = AsyncMock()
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._fetch_non_rem_batch = AsyncMock(side_effect=Exception("DB read failed"))

    mock_conn, _ = _make_mock_conn()
    daemon._open_pg_conn = lambda: mock_conn
    monkeypatch.setattr(rem_loop, "_take_shared_backup_lock", lambda c: True)

    processed, attempted = await daemon.run_cycle()

    assert processed == 0
    assert attempted == 0
    assert daemon._lock_holder is None
    mock_conn.close.assert_called_once()


# ── Invariant I2: No shared lock during a chat call ───────────────────────────

@pytest.mark.asyncio
async def test_i2_no_shared_lock_during_chat_call_rem():
    """Invariant I2: the shared lock is released before the chat HTTP call
    and reacquired only after it returns.
    Failure to re-introduce: skip the release.
    """
    mock_conn, mock_cur = _make_mock_conn()
    holder = _BackupLockHolder(mock_conn, lambda: _make_mock_conn()[0])

    lock_state = {"locked": True}

    def _exec(sql, params=None):
        if "pg_advisory_unlock_shared" in sql:
            lock_state["locked"] = False
        elif "pg_advisory_lock_shared" in sql:
            lock_state["locked"] = True

    mock_cur.execute.side_effect = _exec

    observed_state_during_post = []

    async def _fake_post():
        observed_state_during_post.append(lock_state["locked"])
        return "response"

    async with holder.release_for_chat():
        await _fake_post()

    assert observed_state_during_post == [False], (
        "Shared lock must NOT be held during the HTTP post call"
    )
    assert lock_state["locked"] is True, "Shared lock must be reacquired after post call"


@pytest.mark.asyncio
async def test_i2_no_shared_lock_during_chat_call_nrem():
    """Invariant I2 on NREM: _post_nrem releases the lock around client.post.
    Failure to re-introduce: skip the release.
    """
    mock_conn, mock_cur = _make_mock_conn()
    holder = consolidation_loop._BackupLockHolder(mock_conn, lambda: _make_mock_conn()[0])

    lock_state = {"locked": True}

    def _exec(sql, params=None):
        if "pg_advisory_unlock_shared" in sql:
            lock_state["locked"] = False
        elif "pg_advisory_lock_shared" in sql:
            lock_state["locked"] = True

    mock_cur.execute.side_effect = _exec

    observed_state_during_post = []

    mock_client = AsyncMock()
    async def _post_side_effect(*args, **kwargs):
        observed_state_during_post.append(lock_state["locked"])
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        return resp

    mock_client.post.side_effect = _post_side_effect

    with patch.dict(os.environ, {"MOCK_LLM": "0"}):
        resp = await consolidation_loop._post_nrem(
            mock_client, {"model": "test-model"}, lock_holder=holder
        )

    assert observed_state_during_post == [False], (
        "NREM shared lock must be released during client.post"
    )
    assert lock_state["locked"] is True, "NREM shared lock must be reacquired after post"


# ── Invariant I3: Reply arriving mid-dump uses blocking reacquire ─────────────

def test_i3_blocking_advisory_lock_shared_used_on_reacquire():
    """Invariant I3: A reply arriving mid-dump is written after the dump,
    not discarded. Reacquire must use blocking pg_advisory_lock_shared,
    NEVER non-blocking pg_try_advisory_lock_shared.
    Failure to re-introduce: try-lock on reacquire.
    """
    mock_conn, mock_cur = _make_mock_conn()
    holder = _BackupLockHolder(mock_conn, lambda: _make_mock_conn()[0])

    executed_queries = []
    mock_cur.execute.side_effect = lambda sql, params=None: executed_queries.append(sql)

    holder.reacquire_lock_blocking()

    assert any("pg_advisory_lock_shared" in q for q in executed_queries), (
        "Reacquire must call pg_advisory_lock_shared"
    )
    assert not any("pg_try_advisory_lock_shared" in q for q in executed_queries), (
        "Reacquire must NOT use try-lock (pg_try_advisory_lock_shared)"
    )


@pytest.mark.asyncio
async def test_i3_reply_waits_behind_held_exclusive_event_before_store_helper(monkeypatch):
    """Invariant I3 behavioural test: when an exclusive lock (e.g. backup dump)
    is held, a reply arriving mid-dump waits behind the exclusive event during
    reacquire, and the store helper executes only after the exclusive lock is released.
    Failure to re-introduce: store helper executes before exclusive lock is released.
    """
    exclusive_dump_held = threading.Event()
    exclusive_dump_held.clear()

    reacquire_started = threading.Event()
    store_helper_executed = False

    mock_conn, mock_cur = _make_mock_conn()

    def _execute(sql, params=None):
        if "pg_advisory_lock_shared" in sql:
            reacquire_started.set()
            exclusive_dump_held.wait()

    mock_cur.execute.side_effect = _execute

    holder = _BackupLockHolder(mock_conn, lambda: _make_mock_conn()[0])
    rem = REMDaemon()
    rem._lock_holder = holder

    async def fake_post(self, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
        resp.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "test summary"}'}}]
        }
        return resp

    async def fake_apply(pg_id, kind, result, conn, loop, original_content=""):
        nonlocal store_helper_executed
        assert exclusive_dump_held.is_set(), "Store helper ran while exclusive lock was still held!"
        store_helper_executed = True
        return True

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(rem, "_apply_fact_result", fake_apply)
    monkeypatch.delenv("MOCK_LLM", raising=False)

    content = "Detailed technical fact statement exceeding threshold " * 100
    loop = asyncio.get_running_loop()

    task = asyncio.create_task(
        rem._process_fact(1001, content, rem_loop.KIND_FACT, holder, loop)
    )
    try:
        await asyncio.to_thread(reacquire_started.wait, timeout=2.0)
        assert reacquire_started.is_set(), "Reacquire should have started"

        await asyncio.sleep(0.05)

        assert store_helper_executed is False, (
            "Store helper must not run while reacquire is waiting behind exclusive dump event"
        )
    finally:
        # Release the blocked executor thread on every path, so a failed assertion cannot hang the run.
        exclusive_dump_held.set()

    ok = await asyncio.wait_for(task, timeout=10)
    assert ok is True
    assert store_helper_executed is True, "Store helper must run after exclusive lock is released"


# ── Invariant I4: Lock conn dead: reconnect or withhold with no unparseable ───

def test_i4_reconnect_succeeds_on_dead_conn():
    """When the existing connection died during the chat call,
    reacquire_lock_blocking reconnects and reacquires the lock.
    """
    old_conn, old_cur = _make_mock_conn()
    new_conn, new_cur = _make_mock_conn()

    # old_conn fails on cursor execute
    old_cur.execute.side_effect = Exception("connection closed by server")

    reconnected = {"called": False}
    def _open():
        reconnected["called"] = True
        return new_conn

    holder = _BackupLockHolder(old_conn, _open)
    holder.reacquire_lock_blocking()

    assert reconnected["called"] is True
    assert holder.conn is new_conn
    # Verify new_conn acquired the lock
    new_cur.execute.assert_called_with("SELECT pg_advisory_lock_shared(%s)", (BACKUP_ADVISORY_LOCK_KEY,))


@pytest.mark.asyncio
async def test_i4_rem_reacquire_failed_withholds_and_does_not_mark_unparseable(monkeypatch):
    """Invariant I4: If lock reacquire fails in REM, the item is withheld
    in memory (self._withheld), NOT marked unparseable, and raises
    LockReacquireFailed to abort the cycle.
    Failure to re-introduce: ignore failure; let it become unparseable.
    """
    daemon = REMDaemon.__new__(REMDaemon)
    daemon._withheld = set()
    daemon._last_llm_failure = None
    daemon._last_backlog = None

    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.execute.side_effect = Exception("network down")
    def _fail_open():
        raise Exception("cannot connect")

    holder = _BackupLockHolder(mock_conn, _fail_open)
    daemon._lock_holder = holder

    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.setattr(rem_loop, "MOCK_LLM", "0", raising=False)

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=MagicMock(
        status_code=200,
        headers={"X-SM-LLM-Backend": "local-test"},
        json=lambda: {"choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "ok"}'}}]}
    ))
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("rem_loop.httpx.AsyncClient", lambda **kw: fake_cm)

    long_content = "x" * (rem_loop.REM_SUMMARY_THRESHOLD + 10)

    with pytest.raises(LockReacquireFailed):
        await daemon._llm_process(long_content, "fact", pg_id=42)

    assert 42 in daemon._withheld, "Failed reacquire must add pg_id to self._withheld"
    assert daemon._last_llm_failure != "unparseable", "Must not be marked unparseable"


@pytest.mark.asyncio
async def test_i4_nrem_reacquire_failed_withholds_and_does_not_mark_unparseable(monkeypatch):
    """Invariant I4: If lock reacquire fails in NREM _fold_insight, the fold
    key is added to self._withheld_folds, no backlog upsert is made for unparseable,
    and LockReacquireFailed is raised to abort the cycle.
    Failure to re-introduce: ignore failure; let it become unparseable.
    """
    daemon = ConsolidationDaemon.__new__(ConsolidationDaemon)
    daemon._withheld_folds = set()
    daemon._lock_holder = None

    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.execute.side_effect = Exception("network down")
    def _fail_open():
        raise Exception("cannot reconnect")

    holder = consolidation_loop._BackupLockHolder(mock_conn, _fail_open)
    daemon._lock_holder = holder

    # Mock Postgres fetches in _fold_insight
    pg_conn, pg_cur = _make_mock_conn()
    pg_cur.fetchall.return_value = [
        (101, "Decision 1", "sample_proj", "decision", {}),
        (102, "Decision 2", "sample_proj", "decision", {}),
    ]

    monkeypatch.setattr(consolidation_loop, "fetch_reversal_context", lambda c, ids: [])
    monkeypatch.setattr(consolidation_loop, "fetch_insight_outbox_rows", lambda c, ids: [])

    with patch.object(daemon, "generate_insight_slots", AsyncMock(side_effect=consolidation_loop.LockReacquireFailed("lock lost"))), \
         patch.object(consolidation_loop, "llm_backlog") as mock_backlog:

        with pytest.raises(consolidation_loop.LockReacquireFailed):
            await daemon._fold_insight(
                pg_conn, "test_entity", [101, 102]
            )

        expected_key = _fold_backlog_key([101, 102])
        assert expected_key in daemon._withheld_folds, (
            "Fold key must be withheld in self._withheld_folds"
        )
        mock_backlog.upsert.assert_not_called()


# ── Invariant I10: Close reconnected conn & abort on reacquire failure ────────

def test_i10_holder_closes_new_conn_after_reconnect():
    """Invariant I10: After a reconnect, holder.close() closes the CURRENT
    (new) connection, so the shared lock is freed and a later exclusive acquire
    succeeds once the cycle ends.
    Failure to re-introduce: close original conn only.
    """
    old_conn, old_cur = _make_mock_conn()
    new_conn, new_cur = _make_mock_conn()

    old_cur.execute.side_effect = Exception("closed")

    holder = _BackupLockHolder(old_conn, lambda: new_conn)
    holder.reacquire_lock_blocking()

    assert holder.conn is new_conn

    holder.close()
    new_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_i10_failed_reacquire_aborts_rem_cycle(monkeypatch):
    """Invariant I10: In rem_loop, a failed reacquire in run_cycle aborts the
    cycle immediately and returns (processed, attempted).
    Failure to re-introduce: continue the cycle.
    """
    daemon = REMDaemon.__new__(REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncIter())
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_AsyncCtx(mock_session))
    daemon.driver = mock_driver

    monkeypatch.setattr(rem_loop, "_take_shared_backup_lock", lambda c: True)
    monkeypatch.setattr(rem_loop, "pool_roster_state", AsyncMock(return_value="ready"))
    daemon._release_eligible_write_failed = AsyncMock()
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._fetch_non_rem_batch = AsyncMock(return_value=(
        [1, 2],
        {1: rem_loop.ONT.decision, 2: rem_loop.ONT.decision},
        {1: 0, 2: 0},
    ))
    daemon._filter_applied_in_outbox = AsyncMock(return_value=[1, 2])
    daemon._batch_fetch_content = AsyncMock(return_value={
        1: {"content": "c1", "kind": rem_loop.KIND_DECISION, "created_at": None},
        2: {"content": "c2", "kind": rem_loop.KIND_DECISION, "created_at": None},
    })
    daemon._bump_rem_pickups = AsyncMock()
    daemon._bump_rem_passed_over = AsyncMock()
    daemon._mark_node_invalid = AsyncMock()

    processed_ids = []
    async def _mock_process_fact(pg_id, *args, **kwargs):
        if pg_id == 1:
            raise LockReacquireFailed("lock conn killed")
        processed_ids.append(pg_id)
        return True

    daemon._process_fact = _mock_process_fact

    mock_conn, _ = _make_mock_conn()
    daemon._open_pg_conn = lambda: mock_conn

    processed, attempted = await daemon.run_cycle()

    assert 2 not in processed_ids, "Second fact must NOT be processed after lock reacquire failure"
    assert processed == 0
    assert daemon._lock_holder is None, "Lock holder must be cleared in finally"


@pytest.mark.asyncio
async def test_i10_failed_reacquire_aborts_nrem_insight_cycle(monkeypatch):
    """Invariant I10: In consolidation_loop, a failed reacquire in run_insight_cycle
    aborts the insight cycle immediately and closes the connection.
    Failure to re-introduce: continue the cycle.
    """
    daemon = ConsolidationDaemon.__new__(ConsolidationDaemon)
    daemon.is_running = True
    daemon._insight_gate_skips = 0
    daemon._withheld_folds = set()
    daemon._record_cycle = MagicMock(return_value=_AsyncCtx(MagicMock(
        run_id=1, fold_dead_letter=[], fold=MagicMock()
    )))

    mock_conn, _ = _make_mock_conn()
    monkeypatch.setattr("psycopg2.connect", lambda *a, **k: mock_conn)
    monkeypatch.setattr(consolidation_loop, "fetch_unreconciled_insights", lambda c: [])
    monkeypatch.setattr(consolidation_loop, "fetch_open_retro_record_ids", lambda c: [1])
    monkeypatch.setattr(consolidation_loop, "fetch_refold_insights", lambda c, r: [
        (1, "entity_1", [10, 11], "prev_1", {}),
        (2, "entity_2", [20, 21], "prev_2", {}),
    ])
    monkeypatch.setattr(consolidation_loop.llm_backlog, "blocked_keys", lambda kind: set())

    folds_called = []
    async def _mock_fold(conn, entity, src_ids, *args, **kwargs):
        folds_called.append(entity)
        if entity == "entity_1":
            raise consolidation_loop.LockReacquireFailed("lock reacquire failed")
        return True

    daemon._fold_insight = _mock_fold

    class _Ready:
        def session(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def run(self, query, **params):
            ids = list(params.get("ids") or [])

            class _R:
                async def data(_self):
                    return [
                        {"pg_id": i, "rem_processed": True,
                         "superseded": False, "rem_invalid": False}
                        for i in ids
                    ]
            return _R()

    daemon.driver = _Ready()
    monkeypatch.setattr(consolidation_loop, "fetch_reversal_context", lambda c, ids: [])

    await daemon.run_insight_cycle()

    assert folds_called == ["entity_1"], (
        "Cycle must abort on LockReacquireFailed; entity_2 must not be folded"
    )
    mock_conn.close.assert_called_once()


# ── Invariant I9: One member set bought at most once across both paths ────────

@pytest.mark.asyncio
async def test_i9_one_member_set_bought_at_most_once_across_refold_and_fresh(monkeypatch):
    """Invariant I9: One member set is bought at most once per cycle across both
    the refold and fresh cluster paths.
    Failure to re-introduce: separate registries. If the refold fails, the same
    member set must NOT be re-attempted on the fresh cluster path.
    """
    daemon = ConsolidationDaemon.__new__(ConsolidationDaemon)
    daemon.is_running = True
    daemon._insight_gate_skips = 0
    daemon._withheld_folds = set()
    daemon._record_cycle = MagicMock(return_value=_AsyncCtx(MagicMock(
        run_id=1, fold_dead_letter=[], fold=MagicMock(),
        eligible_clusters=0, eligible_oldest_age=0,
        dead_lettered_clusters=0, singleton_clusters=0
    )))

    mock_conn, _ = _make_mock_conn()
    monkeypatch.setattr("psycopg2.connect", lambda *a, **k: mock_conn)
    monkeypatch.setattr(consolidation_loop, "fetch_unreconciled_insights", lambda c: [])
    monkeypatch.setattr(consolidation_loop, "fetch_open_retro_record_ids", lambda c: [100])
    # Refold returns judgements [101, 102]
    monkeypatch.setattr(consolidation_loop, "fetch_refold_insights", lambda c, r: [
        (1, "test_proj/section_a", [101, 102], "prev_insight", {}),
    ])
    monkeypatch.setattr(consolidation_loop.llm_backlog, "blocked_keys", lambda kind: set())

    # Fresh clusters also finds a cluster with the identical judgement IDs [101, 102]
    async def _mock_find_clusters():
        return [{
            "entity": "test_proj/section_a",
            "decision_ids": [101],
            "projects": ["test_proj"],
            "domain": "section_a",
            "judgement_ids": [101, 102],
            "judgement_types": {101: "decision", 102: "retrospective"},
            "has_retrospective": True,
        }]
    daemon._find_fresh_insight_clusters = _mock_find_clusters

    monkeypatch.setattr(consolidation_loop, "fetch_active_insight_rows", lambda c: [])
    monkeypatch.setattr(consolidation_loop, "match_existing_insight", lambda j, e: None)
    monkeypatch.setattr(consolidation_loop, "_fetch_outbox_created_at", lambda ids: {})
    monkeypatch.setattr(consolidation_loop, "fetch_active_thematic_summary_id", lambda c, p, d: None)
    monkeypatch.setattr(consolidation_loop, "close_refold_ledger_rows", lambda c, context: None)

    fold_attempts = []
    async def _mock_fold(conn, entity, src_ids, *args, **kwargs):
        fold_attempts.append(tuple(sorted(src_ids)))
        # Simulate refold failure
        return False

    daemon._fold_insight = _mock_fold

    class _Ready:
        def session(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def run(self, query, **params):
            ids = list(params.get("ids") or [])

            class _R:
                async def data(_self):
                    return [
                        {"pg_id": i, "rem_processed": True,
                         "superseded": False, "rem_invalid": False}
                        for i in ids
                    ]
            return _R()

    daemon.driver = _Ready()
    monkeypatch.setattr(consolidation_loop, "fetch_reversal_context", lambda c, ids: [])

    await daemon.run_insight_cycle()

    assert fold_attempts == [(101, 102)], (
        f"Member set (101, 102) must be attempted at most once, but was attempted {len(fold_attempts)} times: {fold_attempts}"
    )
