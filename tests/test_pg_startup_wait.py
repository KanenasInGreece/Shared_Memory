"""Coordinator.start() waits (bounded) for Postgres to start accepting
connections, instead of crashing on the first ConnectionRefusedError
(fact:1609): the shipped unit's Restart=on-failure was masking a boot-order
race, not tolerating it.

Covers `_connect_with_startup_wait` (used to wrap both the pgvector version
probe and `asyncpg.create_pool` in `start()`):
  - a transient "not ready" failure is retried, logged, and start() proceeds
    once the underlying call eventually succeeds
  - the retry window is bounded by WALL CLOCK (C2/C3, merger fix round: a
    caller-supplied `deadline`, read via the module-patchable `_monotonic`,
    not an accumulated-sleep count) -- once it passes, the last exception
    propagates so systemd's Restart= is the real backstop
  - probe and create_pool share ONE deadline (C3/C4) -- a probe that spends
    the whole window leaves create_pool a single, already-expired attempt,
    never a fresh second budget that masks a permanently-disabled
    hnsw_iterative_scan behind an eventually-successful pool
  - PG_STARTUP_RETRY_S is clamped to a 0.1s floor -- a 0 or negative
    operator value must not busy-loop
  - a non-startup error (e.g. bad credentials) is never retried -- it raises
    on the very first attempt

All mocked -- no live Postgres/Neo4j. `asyncio.create_task` is stubbed to
close the coroutine it is given rather than schedule it, so the outbox/
health/alt-vector background loops never actually run in this test.
"""
import asyncio
import importlib.util
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest


def load_coordinator(**env):
    """Fresh coordinator module (mirrors test_backup_quiesce.load_coordinator),
    optionally with env vars pre-set before module-level constants evaluate."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    for k, v in env.items():
        os.environ[k] = v
    try:
        path = os.path.join(scripts_dir, "coordinator.py")
        spec = importlib.util.spec_from_file_location("coordinator_pg_startup_wait_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k in env:
            os.environ.pop(k, None)


class _FakeClock:
    """A `time.monotonic`-shaped fake: `__call__()` reads the current fake
    time, `sleep()` (installed as `asyncio.sleep`) advances it by exactly the
    duration requested -- so the retry loop's own wall-clock arithmetic runs
    for real against a controllable clock, rather than the test faking the
    loop's behaviour by counting mocked sleep calls.

    `max_sleeps` (T8, merger fix round) is a safety cap, not part of the
    production contract under test: if the retry loop's own bound were ever
    removed by a future edit (create_pool here always fails, so an unbounded
    loop truly never returns), the fake clock's own advancing would let it
    spin forever with NO real wall-clock time passing -- a genuine test hang,
    not a fast failure. Capping sleep calls turns that failure mode into a
    prompt `RuntimeError` instead."""

    def __init__(self, start: float = 0.0, max_sleeps: int | None = None):
        self.now = start
        self.max_sleeps = max_sleeps
        self.sleep_calls = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, duration: float) -> None:
        self.sleep_calls += 1
        if self.max_sleeps is not None and self.sleep_calls > self.max_sleeps:
            raise RuntimeError(
                f"fake sleep called {self.sleep_calls} times (cap "
                f"{self.max_sleeps}) -- the startup-wait retry loop looks "
                "unbounded")
        self.now += duration


def _fake_probe_conn():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="0.8.0")
    conn.close = AsyncMock()
    return conn


def _fake_pool():
    """A fake asyncpg pool: `.acquire(timeout=...)` is an async context
    manager yielding a connection whose `.execute()` returns the same shape
    asyncpg gives for an UPDATE ("UPDATE <n>"), which start()'s outbox
    recovery step parses with `int(result.split()[-1])`."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")

    class _AcquireCtx:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *exc):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCtx())
    return pool


def _stub_post_pool_start(mod, monkeypatch):
    """Neutralise everything start() does AFTER the pool is up, so the test
    stays scoped to the startup-wait retry behaviour."""
    monkeypatch.setattr(mod.AsyncGraphDatabase, "driver", MagicMock(return_value=MagicMock()))

    def _fake_create_task(coro, name=None):
        coro.close()  # never scheduled -- avoids a real background loop running
        return MagicMock()

    monkeypatch.setattr(mod.asyncio, "create_task", _fake_create_task)


def _install_fake_clock(mod, monkeypatch, start: float = 0.0,
                         max_sleeps: int | None = None) -> _FakeClock:
    clock = _FakeClock(start, max_sleeps=max_sleeps)
    monkeypatch.setattr(mod, "_monotonic", clock)
    monkeypatch.setattr(mod.asyncio, "sleep", clock.sleep)
    return clock


@pytest.mark.asyncio
async def test_transient_failure_retried_and_start_proceeds(monkeypatch, caplog):
    """create_pool raises ConnectionRefusedError twice, then returns a real
    pool -- start() must retry past both and complete, logging a WARNING
    for each retried attempt."""
    mod = load_coordinator()
    _install_fake_clock(mod, monkeypatch)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=_fake_probe_conn()))

    pool = _fake_pool()
    create_pool = AsyncMock(side_effect=[
        ConnectionRefusedError("refused"),
        ConnectionRefusedError("refused"),
        pool,
    ])
    monkeypatch.setattr(mod.asyncpg, "create_pool", create_pool)
    _stub_post_pool_start(mod, monkeypatch)

    coord = mod.MemoryCoordinator()
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await coord.start()

    assert coord._pool is pool
    assert create_pool.await_count == 3
    retry_lines = [r.message for r in caplog.records if "not ready yet" in r.message]
    assert len(retry_lines) == 2


@pytest.mark.asyncio
async def test_window_exhausted_raises_after_the_expected_attempt_count(monkeypatch):
    """T8 (merger fix round) -- create_pool never succeeds; once the WALL-
    CLOCK deadline has passed, the last exception propagates instead of
    retrying forever. Asserting the exact attempt count (not just that SOME
    exception eventually propagates) is the point: without it, a mutation
    that deletes the giveup check would spin the loop forever -- and since
    create_pool NEVER succeeds here, that is a genuine infinite loop, not
    just a slow one. `_install_fake_clock`'s `max_sleeps` cap turns that
    failure mode into a fast, deterministic `RuntimeError` (caught by
    `pytest.raises(ConnectionRefusedError)` failing on the wrong exception
    type) instead of a hung test process."""
    mod = load_coordinator()
    monkeypatch.setattr(mod, "PG_STARTUP_WAIT_S", 4.0)
    monkeypatch.setattr(mod, "PG_STARTUP_RETRY_S", 2.0)
    _install_fake_clock(mod, monkeypatch, max_sleeps=20)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=_fake_probe_conn()))
    create_pool = AsyncMock(side_effect=ConnectionRefusedError("refused"))
    monkeypatch.setattr(mod.asyncpg, "create_pool", create_pool)
    _stub_post_pool_start(mod, monkeypatch)

    coord = mod.MemoryCoordinator()
    with pytest.raises(ConnectionRefusedError):
        await coord.start()
    # deadline=4.0, retry=2.0, clock starts at 0 (probe consumes none of it):
    # t=0 fail->sleep(2), t=2 fail->sleep(2), t=4 fail->now>=deadline->raise.
    assert create_pool.await_count == 3


@pytest.mark.asyncio
async def test_non_startup_error_is_not_retried(monkeypatch):
    """A non-startup error (bad credentials) is not a boot-order race -- it
    must raise on the very first attempt, with no retry/sleep at all."""
    mod = load_coordinator()
    _install_fake_clock(mod, monkeypatch)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=_fake_probe_conn()))
    create_pool = AsyncMock(side_effect=asyncpg.InvalidPasswordError("bad password"))
    monkeypatch.setattr(mod.asyncpg, "create_pool", create_pool)
    _stub_post_pool_start(mod, monkeypatch)

    coord = mod.MemoryCoordinator()
    with pytest.raises(asyncpg.InvalidPasswordError):
        await coord.start()
    assert create_pool.await_count == 1


@pytest.mark.asyncio
async def test_probe_transient_failure_retried_then_succeeds(monkeypatch):
    """T7 (merger fix round) -- the pgvector probe's OWN connect call must go
    through the same retry helper as create_pool, not a bare `asyncpg.connect`.
    Mutation check: reverting the probe call site to a bare
    `await asyncpg.connect(PG_DSN)` kills this test -- the first
    ConnectionRefusedError would propagate straight into the probe's
    surrounding `except Exception` (today's "treat as unknown" fallback),
    start() would proceed to create_pool without ever retrying, and
    `connect.await_count` would be 1, not 3."""
    mod = load_coordinator()
    _install_fake_clock(mod, monkeypatch)
    connect = AsyncMock(side_effect=[
        ConnectionRefusedError("refused"),
        ConnectionRefusedError("refused"),
        _fake_probe_conn(),
    ])
    monkeypatch.setattr(mod.asyncpg, "connect", connect)
    monkeypatch.setattr(mod.asyncpg, "create_pool", AsyncMock(return_value=_fake_pool()))
    _stub_post_pool_start(mod, monkeypatch)

    coord = mod.MemoryCoordinator()
    await coord.start()
    assert connect.await_count == 3


@pytest.mark.asyncio
async def test_probe_and_pool_share_one_deadline(monkeypatch):
    """C3/C4 (merger fix round) -- the probe and create_pool must share ONE
    startup-wait deadline, computed once in start(). If the probe alone
    exhausts the whole window (Postgres never comes up), create_pool must be
    attempted exactly ONCE, on an already-expired deadline, and its
    exception must propagate out of start() -- never a fresh, separate
    budget that would let create_pool go on retrying (and possibly
    eventually succeed) while hnsw_iterative_scan stays silently,
    permanently disabled from the probe's own swallowed failure."""
    mod = load_coordinator()
    monkeypatch.setattr(mod, "PG_STARTUP_WAIT_S", 4.0)
    monkeypatch.setattr(mod, "PG_STARTUP_RETRY_S", 2.0)
    _install_fake_clock(mod, monkeypatch)
    # The probe NEVER succeeds -- consumes the entire shared window.
    monkeypatch.setattr(mod.asyncpg, "connect",
                         AsyncMock(side_effect=ConnectionRefusedError("refused")))
    create_pool = AsyncMock(side_effect=ConnectionRefusedError("refused"))
    monkeypatch.setattr(mod.asyncpg, "create_pool", create_pool)
    _stub_post_pool_start(mod, monkeypatch)

    coord = mod.MemoryCoordinator()
    with pytest.raises(ConnectionRefusedError):
        await coord.start()
    # The probe's own try/except Exception swallows its exhausted-window
    # failure ("treat as unknown") -- so start() reaches create_pool with the
    # SAME deadline already at (or past) its limit: exactly one attempt.
    assert create_pool.await_count == 1


def test_retry_interval_clamped_to_a_floor(monkeypatch):
    """C4 numbering aside (merger fix round, item 5) -- PG_STARTUP_RETRY_S=0
    or negative must not produce a zero-pacing busy-loop against a down DB;
    it clamps to a 0.1s floor. A normal positive value passes through
    unclamped."""
    mod = load_coordinator(PG_STARTUP_RETRY_S="0")
    assert mod.PG_STARTUP_RETRY_S == 0.1
    mod = load_coordinator(PG_STARTUP_RETRY_S="-5")
    assert mod.PG_STARTUP_RETRY_S == 0.1
    mod = load_coordinator(PG_STARTUP_RETRY_S="3.5")
    assert mod.PG_STARTUP_RETRY_S == 3.5
