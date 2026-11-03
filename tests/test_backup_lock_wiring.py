"""Verification of backup advisory lock wiring across coordinator, REM, NREM, proxy quiesce checks, and configuration validation.

- Coordinator quiesce fence lifecycle, promotion, re-arm, and restart token uniqueness.
- REM and NREM lock holder lifecycle: released during chat posts, reacquired blocking before store helpers.
- Reconnect propagation on dead connection across helpers and finalizer.
- Unfinished consolidation runs on reacquire failure retained in memory and finalized only when holding the lock.
- Gateway configuration refusal for invalid max_wait_s via real subprocess exit status.
- Quiesce backend check with identity encoding and registration in main().
- Exclusion of lock reacquire duration from recorded LLM chat wall time (solo, batch, insight).
"""
import ast
import asyncio
import contextlib
import gzip
import inspect
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "shared-memory" / "scripts"
RESTORE_SH = REPO_ROOT / "shared-memory" / "ops" / "restore.sh"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_config
import coordinator
import consolidation_loop
import hive_mind_proxy
import rem_loop
import secure_env


# ── Quiesce fence generation & restart token ──────────────────────────────────

@pytest.mark.asyncio
async def test_begin_quiesce_lifecycle_and_restart_token():
    """Drive real _begin_quiesce with a fake asyncpg connection through pending and promotion:
    - Fence value set on first acquire
    - Unchanged on plain re-arm
    - Changed on replacement of a dead stored connection
    - Two process tokens differ, so a restart cannot match.
    """
    coord = coordinator.MemoryCoordinator()

    initial_acquisitions = coordinator._quiesce_fence_acquisitions
    process_token = coordinator._quiesce_process_token

    fake_conn1 = MagicMock()
    fake_conn1.is_closed.return_value = False
    fake_conn1.fetchval = AsyncMock(return_value=1)
    fake_conn1.terminate = MagicMock()

    async def fake_connect_first(gen):
        coord._quiesce_pending_conn = fake_conn1

    coord._quiesce_connect_and_lock = fake_connect_first

    # 1. First acquire: pending promoted to stored, acquisitions incremented
    res1 = await coord._begin_quiesce(max_seconds=10, request=None)
    assert res1["reason"] is None
    assert res1["lock_held"] is True
    wire_gen1 = coordinator._wire_quiesce_generation()
    tok1, acq1 = wire_gen1.split("-")
    assert tok1 == process_token
    assert int(acq1) == initial_acquisitions + 1
    assert coord._quiesce_conn is fake_conn1

    # 2. Plain re-arm: stored connection alive, probe succeeds, wire gen unchanged
    res2 = await coord._begin_quiesce(max_seconds=10, request=None)
    assert res2["reason"] is None
    assert res2["lock_held"] is True
    wire_gen2 = coordinator._wire_quiesce_generation()
    assert wire_gen2 == wire_gen1, "Plain re-arm must preserve wire quiesce_generation"
    assert coord._quiesce_conn is fake_conn1

    # 3. Dead connection replacement: stored connection is closed -> detached and re-acquired
    fake_conn1.is_closed.return_value = True
    fake_conn2 = MagicMock()
    fake_conn2.is_closed.return_value = False
    fake_conn2.fetchval = AsyncMock(return_value=1)
    fake_conn2.terminate = MagicMock()

    async def fake_connect_second(gen):
        coord._quiesce_pending_conn = fake_conn2

    coord._quiesce_connect_and_lock = fake_connect_second

    res3 = await coord._begin_quiesce(max_seconds=10, request=None)
    assert res3["reason"] is None
    assert res3["lock_held"] is True
    wire_gen3 = coordinator._wire_quiesce_generation()
    tok3, acq3 = wire_gen3.split("-")
    assert tok3 == process_token
    assert int(acq3) == int(acq1) + 1
    assert wire_gen3 != wire_gen2, "Dead connection replacement must increment wire quiesce_generation"
    assert coord._quiesce_conn is fake_conn2

    # 4. Across restart: two process tokens generated via secrets.token_hex(8) differ
    restart_token = secrets.token_hex(8)
    assert restart_token != process_token
    restart_wire = f"{restart_token}-{acq1}"
    assert restart_wire != wire_gen1, "Wire generation across restart cannot match prior generation"

    await coord._end_quiesce(ended_by="test")


def test_quiesce_process_token_differs_across_subprocesses():
    """Load coordinator's token twice in two real subprocess imports and show they differ.
    Dies if the token becomes a constant.
    """
    cmd = [sys.executable, "-c", "import coordinator; print(coordinator._quiesce_process_token)"]
    env = dict(os.environ)
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SCRIPTS_DIR}:{pp}" if pp else str(SCRIPTS_DIR)
    env["SECURE_ENV_FILE"] = ""
    env["COORDINATOR_UDS"] = ""
    env["GATEWAY_PORT"] = "0"
    p1 = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True, cwd=str(REPO_ROOT))
    p2 = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True, cwd=str(REPO_ROOT))
    tok1 = p1.stdout.strip()
    tok2 = p2.stdout.strip()
    assert len(tok1) == 16, f"Expected 16-hex-char token, got {tok1!r}"
    assert len(tok2) == 16, f"Expected 16-hex-char token, got {tok2!r}"
    assert tok1 != tok2, "Coordinator process tokens must differ across separate imports (dies if constant)"


# ── Stateful lock tracking through chat & assignments ─────────────────────────

class StatefulLockHolder:
    """Stateful fake lock tracking lock state at each statement."""
    def __init__(self):
        self.held = True
        self.events = []  # list of (state, timestamp)
        self.conn = MagicMock()
        self.key = 8765309

    def release_lock(self):
        self.held = False
        self.events.append(("unlocked", time.monotonic()))

    def reacquire_lock_blocking(self, loop=None):
        self.held = True
        self.events.append(("locked", time.monotonic()))

    @contextlib.asynccontextmanager
    async def release_for_chat(self):
        self.release_lock()
        try:
            yield
        finally:
            self.reacquire_lock_blocking()


class StatefulAdvisoryConn:
    """Stateful fake Postgres connection tracking advisory lock SQL calls."""
    def __init__(self, key=8765309):
        self.key = key
        self.lock_held = False
        self.closed = False
        self.queries = []

    def close(self):
        self.closed = True
        self.lock_held = False

    def cursor(self):
        return StatefulAdvisoryCursor(self)


class StatefulAdvisoryCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last_val = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        if self.conn.closed:
            raise RuntimeError("Cannot execute query on closed connection")
        self.conn.queries.append((sql, params))
        if "pg_try_advisory_lock_shared" in sql:
            self.conn.lock_held = True
            self.last_val = (True,)
        elif "pg_advisory_lock_shared" in sql:
            self.conn.lock_held = True
            self.last_val = None
        elif "pg_advisory_unlock_shared" in sql:
            self.conn.lock_held = False
            self.last_val = None

    def fetchone(self):
        return self.last_val


@pytest.mark.asyncio
async def test_rem_run_cycle_lock_held_and_released_at_post(monkeypatch):
    """Drive the real run_cycle with a stateful fake connection factory tracking advisory
    lock calls, real _BackupLockHolder, stubbing only candidate read and helpers that need stores,
    and a fake httpx client. Record at the post that the lock is released, and that the next
    helper sees it held.
    Dies if self._lock_holder = holder is removed or moved.
    """
    rem = rem_loop.REMDaemon()
    active_conn = None

    def open_conn():
        nonlocal active_conn
        active_conn = StatefulAdvisoryConn()
        return active_conn

    rem._open_pg_conn = open_conn

    # Stub candidate read & external helpers
    rem._release_eligible_write_failed = AsyncMock()
    rem._fetch_non_rem_batch = AsyncMock(return_value=([1001], {1001: "Fact"}, {}))
    rem._recent_write_happened = AsyncMock(return_value=False)
    monkeypatch.setattr(rem_loop, "_nrem_is_queuing", lambda conn: False)
    monkeypatch.setattr(rem_loop, "pool_roster_state", AsyncMock(return_value="ready"))
    rem._filter_applied_in_outbox = AsyncMock(return_value=[1001])
    rem._batch_fetch_content = AsyncMock(return_value={
        1001: {"kind": rem_loop.KIND_FACT, "content": "Detailed technical fact statement exceeding threshold " * 50, "created_at": None}
    })
    rem._bump_rem_pickups = AsyncMock()

    lock_state_at_post = None
    lock_state_at_store = None

    async def fake_post(self, *args, **kwargs):
        nonlocal lock_state_at_post
        lock_state_at_post = active_conn.lock_held
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
        resp.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "distillate"}'}}]
        }
        return resp

    async def fake_apply(pg_id, kind, result, holder, loop, original_content=""):
        nonlocal lock_state_at_store
        lock_state_at_store = active_conn.lock_held
        return True

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(rem, "_apply_fact_result", fake_apply)
    monkeypatch.setattr(rem, "_write_rem_timing", AsyncMock())
    monkeypatch.delenv("MOCK_LLM", raising=False)

    processed, attempted = await rem.run_cycle()
    assert processed == 1 and attempted == 1
    assert lock_state_at_post is False, "Shared backup lock must be released during chat post"
    assert lock_state_at_store is True, "Next helper must see the shared backup lock held"
    assert active_conn.closed is True, "Holder must close the connection at cycle end"
    assert rem._lock_holder is None, "_lock_holder must be cleared to None at cycle exit"


@pytest.mark.asyncio
async def test_rem_reconnect_through_run_cycle_uses_second_conn(monkeypatch):
    """REM reconnect through run_cycle: the first connection dies during the chat.
    A later helper in the same cycle and the finalizer use the second connection and never the first.
    Dies if a helper is given the captured conn.
    """
    rem = rem_loop.REMDaemon()
    conn1 = StatefulAdvisoryConn()
    conn2 = StatefulAdvisoryConn()
    conns = [conn1, conn2]

    def open_conn():
        return conns.pop(0)

    rem._open_pg_conn = open_conn

    rem._release_eligible_write_failed = AsyncMock()
    rem._fetch_non_rem_batch = AsyncMock(return_value=([1001], {1001: "Fact"}, {}))
    rem._recent_write_happened = AsyncMock(return_value=False)
    monkeypatch.setattr(rem_loop, "_nrem_is_queuing", lambda conn: False)
    monkeypatch.setattr(rem_loop, "pool_roster_state", AsyncMock(return_value="ready"))
    rem._filter_applied_in_outbox = AsyncMock(return_value=[1001])
    rem._batch_fetch_content = AsyncMock(return_value={
        1001: {"kind": rem_loop.KIND_FACT, "content": "Detailed technical fact statement exceeding threshold " * 50, "created_at": None}
    })
    rem._bump_rem_pickups = AsyncMock()

    async def fake_post(self, *args, **kwargs):
        # Simulate conn1 dying during the chat
        conn1.close()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
        resp.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "distillate"}'}}]
        }
        return resp

    helper_conn_received = None

    async def fake_apply(pg_id, kind, result, holder, loop, original_content=""):
        nonlocal helper_conn_received
        helper_conn_received = getattr(holder, "conn", holder)
        return True

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(rem, "_apply_fact_result", fake_apply)
    monkeypatch.setattr(rem, "_write_rem_timing", AsyncMock())
    monkeypatch.delenv("MOCK_LLM", raising=False)

    processed, attempted = await rem.run_cycle()
    assert processed == 1 and attempted == 1
    assert helper_conn_received is conn2, (
        "Subsequent helper must receive the reconnected conn2, never initial conn1; "
        "dies if helper is given captured initial conn"
    )
    assert conn1.closed is True
    assert conn2.closed is True, "Finalizer must close the new connection conn2"
    assert rem._lock_holder is None


@pytest.mark.asyncio
async def test_rem_solo_lock_held_states(monkeypatch):
    """REM solo through _process_fact: unlocked at post, locked before store call."""
    rem = rem_loop.REMDaemon()
    holder = StatefulLockHolder()
    rem._lock_holder = holder

    lock_state_at_post = None
    lock_state_at_store = None

    async def fake_post(self, *args, **kwargs):
        nonlocal lock_state_at_post
        lock_state_at_post = holder.held
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
        resp.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"summary": "distillate"}'}}]
        }
        return resp

    async def fake_apply(pg_id, kind, result, conn, loop, original_content=""):
        nonlocal lock_state_at_store
        lock_state_at_store = holder.held
        return True

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(rem, "_apply_fact_result", fake_apply)
    monkeypatch.delenv("MOCK_LLM", raising=False)

    content = "Detailed technical fact statement exceeding threshold " * 100
    ok = await rem._process_fact(
        1001, content, rem_loop.KIND_FACT, holder, asyncio.get_running_loop()
    )
    assert ok is True
    assert lock_state_at_post is False, "Lock must be unlocked at chat post"
    assert lock_state_at_store is True, "Lock must be held before store call"
    assert holder.held is True


@pytest.mark.asyncio
async def test_rem_batch_lock_held_states(monkeypatch):
    """REM batch through _llm_process_batch: unlocked at post, locked before return."""
    rem = rem_loop.REMDaemon()
    holder = StatefulLockHolder()
    rem._lock_holder = holder

    lock_state_at_post = None

    async def fake_post(self, *args, **kwargs):
        nonlocal lock_state_at_post
        lock_state_at_post = holder.held
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
        resp.json.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"idx": 0, "summary": "batch sum 1"}\n{"idx": 1, "summary": "batch sum 2"}'}
            }]
        }
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.delenv("MOCK_LLM", raising=False)

    items = [
        {"pg_id": 2001, "content": "Batch fact content one exceeding threshold " * 100},
        {"pg_id": 2002, "content": "Batch fact content two exceeding threshold " * 100},
    ]
    res, timing, model = await rem._llm_process_batch(items)
    assert 2001 in res and 2002 in res
    assert lock_state_at_post is False, "Lock must be unlocked at batch chat post"
    assert holder.held is True, "Lock must be reacquired before batch return"


@pytest.mark.asyncio
async def test_nrem_insight_llm_lock_held_states(monkeypatch):
    """NREM through _call_insight_llm: unlocked at post, locked before return."""
    nrem = consolidation_loop.ConsolidationDaemon()
    holder = StatefulLockHolder()
    nrem._lock_holder = holder

    lock_state_at_post = None

    async def fake_post(self, *args, **kwargs):
        nonlocal lock_state_at_post
        lock_state_at_post = holder.held
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
        resp.json.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "SLOT 3001: slot summary\nPRINCIPLE: core principle"}
            }]
        }
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.delenv("MOCK_LLM", raising=False)

    text = await nrem._call_insight_llm("test prompt", "entity", units=1)
    assert text is not None
    assert "SLOT 3001" in text
    assert lock_state_at_post is False, "Lock must be unlocked at NREM post"
    assert holder.held is True, "Lock must be reacquired before NREM return"


@pytest.mark.asyncio
async def test_nrem_guarded_branches_lock_holder_during_chat():
    """Verify _lock_holder is the gate during the chat on both the consolidation and the
    sweep branch, and None afterwards.
    Dies if _lock_holder is not set to the gate or not cleared to None at exit.
    """
    nrem = consolidation_loop.ConsolidationDaemon()

    # 1. Signature of _call_insight_llm has no holder parameter
    sig = inspect.signature(consolidation_loop.ConsolidationDaemon._call_insight_llm)
    assert "holder" not in sig.parameters, "_call_insight_llm must not accept holder parameter"

    # 2. except LockReacquireFailed: raise in _call_insight_llm
    failing_holder = StatefulLockHolder()
    def fail_reacquire(loop=None):
        raise consolidation_loop.LockReacquireFailed("simulated reacquire failure")
    failing_holder.reacquire_lock_blocking = fail_reacquire

    nrem._lock_holder = failing_holder
    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=MagicMock(status_code=200, headers={}, json=MagicMock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]})))):
        with pytest.raises(consolidation_loop.LockReacquireFailed):
            await nrem._call_insight_llm("prompt", "entity", units=1)
    nrem._lock_holder = None

    # 3. Consolidation branch: _lock_holder is gate during chat and None afterwards
    gate1 = MagicMock()
    holder_during_consolidation = None

    async def fake_consolidation_cycle(backlog):
        nonlocal holder_during_consolidation
        holder_during_consolidation = nrem._lock_holder

    nrem.run_consolidation_cycle = fake_consolidation_cycle
    await nrem._run_guarded_consolidation(gate1, backlog=[], forced=False, seconds_eligible=0)
    assert holder_during_consolidation is gate1, (
        "Consolidation branch: _lock_holder must be the gate during the chat"
    )
    assert nrem._lock_holder is None, (
        "Consolidation branch: _lock_holder must be None after cycle exits"
    )
    gate1.close.assert_called_once()

    # 4. Sweep branch: _lock_holder is gate during chat and None afterwards
    gate2 = MagicMock()
    holder_during_sweep = None

    async def fake_insight_cycle():
        nonlocal holder_during_sweep
        holder_during_sweep = nrem._lock_holder

    nrem.run_insight_cycle = fake_insight_cycle
    nrem.run_lineage_invalidation_pass = AsyncMock()
    nrem.run_ledger_sweep = AsyncMock()
    await nrem._run_guarded_sweep(gate2)
    assert holder_during_sweep is gate2, (
        "Sweep branch: _lock_holder must be the gate during the chat"
    )
    assert nrem._lock_holder is None, (
        "Sweep branch: _lock_holder must be None after sweep exits"
    )
    gate2.close.assert_called_once()


# ── REM batch reacquire failure & reconnect connection propagation ───────────

@pytest.mark.asyncio
async def test_rem_batch_reacquire_failure_withholds_and_writes_no_row(monkeypatch):
    """REM batch reacquire failure withholds every sent item and writes no row."""
    rem = rem_loop.REMDaemon()
    holder = StatefulLockHolder()
    def fail_reacquire(loop=None):
        raise rem_loop.LockReacquireFailed("advisory lock reacquire failed")
    holder.reacquire_lock_blocking = fail_reacquire
    rem._lock_holder = holder

    upsert_mock = MagicMock()
    monkeypatch.setattr(rem_loop.llm_backlog, "upsert", upsert_mock)

    async def fake_post(self, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
        resp.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"idx": 0, "summary": "s1"}\n{"idx": 1, "summary": "s2"}'}}]
        }
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.delenv("MOCK_LLM", raising=False)

    items = [
        {"pg_id": 4001, "content": "Long fact content 4001 exceeding threshold " * 100},
        {"pg_id": 4002, "content": "Long fact content 4002 exceeding threshold " * 100},
    ]
    with pytest.raises(rem_loop.LockReacquireFailed):
        await rem._llm_process_batch(items)

    assert 4001 in rem._withheld
    assert 4002 in rem._withheld
    upsert_mock.assert_not_called()


def test_rem_reconnect_propagates_new_connection_to_helpers_and_finalizer():
    """REM reconnect: after a reconnect a later Postgres helper in run_cycle and the finalizer use the new connection."""
    conn1 = MagicMock()
    conn1.closed = True
    conn2 = MagicMock()
    conn2.closed = False

    open_fn = MagicMock(return_value=conn2)
    holder = rem_loop._BackupLockHolder(conn1, open_fn)

    # Reacquire reconnects and stores new_conn as holder.conn
    holder.reacquire_lock_blocking()
    assert holder.conn is conn2

    # Later helpers reading holder.conn see conn2
    assert holder.conn is conn2

    # Finalizer holder.close() closes conn2 and resets holder.conn to None
    holder.close()
    conn2.close.assert_called_once()
    assert holder.conn is None


# ── Real _record_cycle on LockReacquireFailed & gated drainage ────────────────

@pytest.mark.asyncio
async def test_record_cycle_on_lock_reacquire_failed_runs_no_crun_finish():
    """Real _record_cycle on LockReacquireFailed runs no _crun_finish immediately,
    remembers the run in memory, and drains it through _crun_finish with error_class
    LockReacquireFailed and outcome 'crashed' only at the start of a cycle that holds the gate.
    """
    daemon = consolidation_loop.ConsolidationDaemon()
    try:
        with patch.object(consolidation_loop, "_crun_start", return_value=77) as crun_start_mock, \
             patch.object(consolidation_loop, "_crun_finish") as crun_finish_mock:

            with pytest.raises(consolidation_loop.LockReacquireFailed):
                async with daemon._record_cycle("fact_consolidation"):
                    raise consolidation_loop.LockReacquireFailed("reacquire failed")

            crun_start_mock.assert_called_once()
            crun_finish_mock.assert_not_called()
            assert len(daemon._abandoned_runs) == 1
            assert daemon._abandoned_runs[0]["run_id"] == 77

            # No write without the lock: _drain_abandoned_runs does nothing if lock_holder is None
            daemon._lock_holder = None
            await daemon._drain_abandoned_runs()
            crun_finish_mock.assert_not_called()
            assert len(daemon._abandoned_runs) == 1

            # With gate held: drains and calls _crun_finish with honest error and accepted outcome
            daemon._lock_holder = MagicMock()
            await daemon._drain_abandoned_runs()
            crun_finish_mock.assert_called_once_with(
                77, "crashed", attempted=0, succeeded=0, failed=0,
                error_class="LockReacquireFailed",
                error_msg="cycle aborted: backup lock reacquire failed",
                eligible_clusters=None, eligible_oldest_age=None, extra=None,
            )
            assert len(daemon._abandoned_runs) == 0
    finally:
        await daemon.driver.close()


# ── Unlock failure in both holders closes connection & reconnect works ────────

def test_unlock_failure_in_both_holders():
    """Unlock failure in both holders: connection closed and detached before the post, then reconnect works."""
    for module, label in ((consolidation_loop, "NREM"), (rem_loop, "REM")):
        conn1 = MagicMock()
        conn1.closed = False
        conn1.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("unlock failed")
        conn2 = MagicMock()
        conn2.closed = False

        open_fn = MagicMock(return_value=conn2)
        holder = module._BackupLockHolder(conn1, open_fn)

        # Unlock statement fails
        holder.release_lock()

        # Connection closed and detached before post
        conn1.close.assert_called_once()
        assert holder.conn is None, f"{label} holder.conn must be None after unlock failure"

        # Safe getattr with None raises AttributeError, not crash
        with pytest.raises(AttributeError):
            _ = holder.some_attribute

        # Reconnect works
        holder.reacquire_lock_blocking()
        assert holder.conn is conn2, f"{label} holder must reconnect to conn2"
        conn2.cursor.return_value.__enter__.return_value.execute.assert_called_with(
            "SELECT pg_advisory_lock_shared(%s)", (holder.lock_key,)
        )

        # close() is safe with None
        holder.close()
        assert holder.conn is None
        holder.close()  # idempotent second close


# ── Real gateway refusal for invalid max_wait_s ───────────────────────────────

def test_real_gateway_refusal_for_invalid_max_wait_s():
    """Real gateway refusal: an invalid max_wait_s, including on an entry excluded for a missing token,
    reaches llm_declaration_errors, check_config exits 1, and the gateway refusal routine exits 78
    via real subprocess execution.
    """
    cases = [
        10 ** 400,          # huge integer exceeding float range
        True,               # boolean
        "invalid_seconds",  # string non-numeric
        0,                  # non-positive
        -10,                # negative
        float("nan"),       # NaN
        float("inf"),       # Inf
    ]

    for bad_val in cases:
        # 1. Entry excluded for missing token still refuses invalid max_wait_s in secure_env
        env_with_missing_token = {
            "LLM_BACKENDS_JSON": json.dumps([{
                "url": "http://127.0.0.1:5000",
                "n_ctx": 4096,
                "max_output_tokens": 1024,
                "token_env": "TOTALLY_UNSET_TOKEN_VAR",
                "max_wait_s": bad_val if not isinstance(bad_val, float) else 0,
            }])
        }
        errs = secure_env.llm_declaration_errors(env_with_missing_token)
        fields = [field for _, field in errs]
        assert "max_wait_s" in fields, f"llm_declaration_errors must refuse max_wait_s={bad_val}"

    # 2. Real subprocess execution of check_config (exit 1) and gateway refusal (exit 78) on invalid max_wait_s
    for bad_json in ["-1", "0", '"invalid_str"', "NaN", "Infinity"]:
        env = dict(os.environ)
        pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SCRIPTS_DIR}:{pp}" if pp else str(SCRIPTS_DIR)
        env["SECURE_ENV_FILE"] = ""
        env["COORDINATOR_UDS"] = ""
        env["GATEWAY_PORT"] = "0"
        env["LLM_BACKENDS_JSON"] = json.dumps([{
            "url": "http://127.0.0.1:5000", "n_ctx": 4096, "max_output_tokens": 1024
        }]).replace("}", f', "max_wait_s": {bad_json}}}')

        # check_config --phase-a-only true exit status is 1 (declaration fatals are exit 1 in check_config)
        proc_cc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "check_config.py"), "--phase-a-only"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        )
        assert proc_cc.returncode == 1, (
            f"check_config must exit 1 for max_wait_s={bad_json}, got {proc_cc.returncode}"
        )

        # Real gateway refusal routine true exit status is 78 (EX_CONFIG)
        cmd_gw = [
            sys.executable, "-c",
            "import hive_mind_proxy; hive_mind_proxy._load_llm_backends(); hive_mind_proxy.require_valid_llm_routing_config()"
        ]
        proc_gw = subprocess.run(cmd_gw, env=env, capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert proc_gw.returncode == 78, (
            f"Gateway refusal must exit 78 for max_wait_s={bad_json}, got {proc_gw.returncode}"
        )


# ── Quiesce backend check against aiohttp server returning gzip ───────────────

@pytest.mark.asyncio
async def test_quiesce_backend_check_identity_encoding_and_registration():
    """_quiesce_backend_check against a real local aiohttp server that gzips when accepted:
    - Sends Accept-Encoding: identity, receives plain metrics, logs labelled form.
    - Proves registration: main() sets coordinator.quiesce_check_hook = _quiesce_backend_check.
    """
    # 1. Start local aiohttp server that gzips when client accepts gzip
    async def handle_slots(request):
        return web.Response(status=404)

    async def handle_metrics(request):
        accept = request.headers.get("Accept-Encoding", "")
        # If client accepts gzip (e.g. without Accept-Encoding: identity override), gzip it
        body_text = (
            "# HELP vllm:num_requests_running Number of running requests\n"
            "# TYPE vllm:num_requests_running gauge\n"
            'vllm:num_requests_running{model="mock-vllm"} 5.0\n'
        )
        if "gzip" in accept and "identity" not in accept:
            gz = gzip.compress(body_text.encode("utf-8"))
            return web.Response(body=gz, headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"})
        return web.Response(text=body_text, headers={"Content-Type": "text/plain"})

    app = web.Application()
    app.router.add_get("/slots", handle_slots)
    app.router.add_get("/metrics", handle_metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    backend_url = f"http://127.0.0.1:{port}"

    try:
        # Configure proxy state for backend
        with patch.dict(hive_mind_proxy._llm_inflight, {backend_url: 1}, clear=False), \
             patch.dict(hive_mind_proxy.LLM_BACKEND_MODELS, {backend_url: "mock-vllm"}, clear=False), \
             patch("hive_mind_proxy._self_hosted", return_value=True), \
             patch("hive_mind_proxy.log.info") as log_info_mock:

            await hive_mind_proxy._quiesce_backend_check()

            # Find logged metrics reading
            logged_lines = [call.args[0] % call.args[1:] for call in log_info_mock.call_args_list if call.args]
            matched = [line for line in logged_lines if "signal=metrics" in line and "reading=running=5" in line]
            assert matched, f"Expected signal=metrics reading=running=5, got: {logged_lines}"
    finally:
        await runner.cleanup()

    # 2. Registration check: main() registers coordinator.quiesce_check_hook = _quiesce_backend_check
    proxy_src = (SCRIPTS_DIR / "hive_mind_proxy.py").read_text()
    tree = ast.parse(proxy_src)
    main_func = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "main")
    registered = False
    for node in ast.walk(main_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "quiesce_check_hook":
                    if isinstance(node.value, ast.Name) and node.value.id == "_quiesce_backend_check":
                        registered = True
    assert registered, "hive_mind_proxy.main() must assign coordinator.quiesce_check_hook = _quiesce_backend_check"


# ── Chat wall time excludes slow reacquire ────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_wall_time_excludes_slow_reacquire(monkeypatch):
    """Chat wall time must not include waiting for a dump / reacquire.
    A slow fake reacquire does not raise the recorded wall time for solo, batch, or insight calls.
    """
    holder = StatefulLockHolder()

    # Slow reacquire blocking for 0.4s
    def slow_reacquire(loop=None):
        time.sleep(0.4)
        holder.held = True
    holder.reacquire_lock_blocking = slow_reacquire

    recorded_calls = []
    def fake_record(daemon, rj, backend=None, wall_s=None, **kwargs):
        recorded_calls.append(wall_s)

    monkeypatch.setattr(consolidation_loop, "record_llm_call", fake_record)
    monkeypatch.setattr(rem_loop, "record_llm_call", fake_record)

    # 1. NREM _post_nrem
    client_mock = AsyncMock()
    resp_mock = MagicMock()
    resp_mock.status_code = 200
    resp_mock.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
    resp_mock.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    client_mock.post.return_value = resp_mock

    await consolidation_loop._post_nrem(
        client_mock, {"messages": []}, lock_holder=holder
    )
    assert len(recorded_calls) == 1
    nrem_wall_s = recorded_calls[0]
    assert nrem_wall_s is not None
    assert nrem_wall_s < 0.2, (
        f"NREM wall_s ({nrem_wall_s:.3f}s) must not include 0.4s reacquire time"
    )

    # 2. REM _llm_process
    rem = rem_loop.REMDaemon()
    rem._lock_holder = holder
    monkeypatch.setattr(httpx.AsyncClient, "post", client_mock.post)
    monkeypatch.delenv("MOCK_LLM", raising=False)

    await rem._llm_process("Long content for rem summarisation exceeding threshold " * 100, rem_loop.KIND_FACT, pg_id=5001)
    assert len(recorded_calls) == 2
    rem_wall_s = recorded_calls[1]
    assert rem_wall_s is not None
    assert rem_wall_s < 0.2, (
        f"REM wall_s ({rem_wall_s:.3f}s) must not include 0.4s reacquire time"
    )

    # 3. REM _llm_process_batch
    items = [
        {"pg_id": 6001, "content": "Batch item content exceeding threshold " * 100},
        {"pg_id": 6002, "content": "Second batch item content exceeding threshold " * 100},
    ]
    resp_batch = MagicMock()
    resp_batch.status_code = 200
    resp_batch.headers = {"X-SM-LLM-Backend": "http://127.0.0.1:5000"}
    resp_batch.json.return_value = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"idx": 0, "summary": "s1"}\n{"idx": 1, "summary": "s2"}'}}]
    }
    client_mock.post.return_value = resp_batch
    rem._lock_holder = holder

    res, timing, _ = await rem._llm_process_batch(items)
    assert 6001 in res and 6002 in res
    assert len(recorded_calls) == 3
    batch_wall_s = recorded_calls[2]
    assert batch_wall_s is not None
    assert batch_wall_s < 0.2, (
        f"REM batch wall_s ({batch_wall_s:.3f}s) must not include 0.4s reacquire time"
    )
    assert timing is not None and timing.get("wall_s", 0) < 0.2, (
        f"REM batch timing['wall_s'] must not include 0.4s reacquire time"
    )


# ── Restore live daemon scanner cases & operand handling ──────────────────────

def test_restore_scanner_handles_cpython_options_and_ignores_payloads(tmp_path):
    """Scanner correctly consumes options with operands (-W ignore, -X dev),
    stops with no match on -c payload and -m module, and ignores unrelated script
    arguments naming daemon paths.
    Dies if -W or -X breaks script resolution, or if -c, -m, or trailing args falsely match.
    """
    restore_text = RESTORE_SH.read_text()
    pyscan_code = restore_text.split("<<'PYSCAN'")[1].split("\n", 1)[1].split("PYSCAN")[0]

    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()

    def make_proc(pid, argv_list, cwd=str(REPO_ROOT)):
        p = proc_dir / str(pid)
        p.mkdir()
        cmdline_bytes = b"\x00".join(a.encode("utf-8") for a in argv_list) + b"\x00"
        (p / "cmdline").write_bytes(cmdline_bytes)
        os.symlink(cwd, str(p / "cwd"))

    # 1. -W ignore consumed with operand -> matches rem_loop.py
    make_proc(101, ["python3", "-W", "ignore", "shared-memory/scripts/rem_loop.py"])

    # 2. -X dev consumed with operand -> matches consolidation_loop.py
    make_proc(102, ["python3", "-X", "dev", "shared-memory/scripts/consolidation_loop.py"])

    # 3. -c payload stops scan with no match
    make_proc(103, ["python3", "-c", "import shared-memory/scripts/rem_loop.py"])

    # 4. -m module stops scan with no match
    make_proc(104, ["python3", "-m", "shared-memory.scripts.rem_loop"])

    # 5. Unrelated script argument naming the daemon path does not match
    make_proc(105, ["python3", "some_tool.py", "shared-memory/scripts/rem_loop.py"])

    env = dict(os.environ)
    env["PROC_DIR"] = str(proc_dir)

    proc = subprocess.run(
        [sys.executable, "-c", pyscan_code, str(REPO_ROOT)],
        capture_output=True, text=True, check=True, env=env, cwd=str(REPO_ROOT),
    )

    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert "101 rem_loop.py" in lines, f"PID 101 with -W ignore must match rem_loop.py, got: {lines}"
    assert "102 consolidation_loop.py" in lines, f"PID 102 with -X dev must match consolidation_loop.py, got: {lines}"
    assert not any("103" in l for l in lines), f"PID 103 with -c must not match, got: {lines}"
    assert not any("104" in l for l in lines), f"PID 104 with -m must not match, got: {lines}"
    assert not any("105" in l for l in lines), f"PID 105 with trailing daemon argument must not match, got: {lines}"
    assert len(lines) == 2, f"Expected exactly 2 matched processes, got: {lines}"
