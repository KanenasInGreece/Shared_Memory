"""Coordinator.start() waits (bounded) for Postgres to start accepting
connections, instead of crashing on the first ConnectionRefusedError
(fact:1609): the shipped unit's Restart=on-failure was masking a boot-order
race, not tolerating it.

Covers `_connect_with_startup_wait` (used to wrap both the pgvector version
probe and `asyncpg.create_pool` in `start()`):
  - a transient "not ready" failure is retried, logged, and start() proceeds
    once the underlying call eventually succeeds
  - the retry window is bounded: once PG_STARTUP_WAIT_S of accumulated sleep
    has elapsed, the last exception propagates so systemd's Restart= is the
    real backstop
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


def load_coordinator():
    """Fresh coordinator module (mirrors test_backup_quiesce.load_coordinator)."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_pg_startup_wait_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


async def _no_sleep(_):
    return None


@pytest.mark.asyncio
async def test_transient_failure_retried_and_start_proceeds(monkeypatch, caplog):
    """create_pool raises ConnectionRefusedError twice, then returns a real
    pool -- start() must retry past both and complete, logging a WARNING
    for each retried attempt."""
    mod = load_coordinator()
    monkeypatch.setattr(mod, "PG_STARTUP_RETRY_S", 0.01)
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
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
async def test_window_exhausted_raises(monkeypatch):
    """create_pool never succeeds -- once PG_STARTUP_WAIT_S of accumulated
    retry sleep has elapsed, the last exception propagates out of start()
    instead of retrying forever."""
    mod = load_coordinator()
    monkeypatch.setattr(mod, "PG_STARTUP_WAIT_S", 4.0)
    monkeypatch.setattr(mod, "PG_STARTUP_RETRY_S", 2.0)
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)  # instant -- bound is by accumulated count, not wall clock
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=_fake_probe_conn()))
    monkeypatch.setattr(mod.asyncpg, "create_pool",
                         AsyncMock(side_effect=ConnectionRefusedError("refused")))
    _stub_post_pool_start(mod, monkeypatch)

    coord = mod.MemoryCoordinator()
    with pytest.raises(ConnectionRefusedError):
        await coord.start()


@pytest.mark.asyncio
async def test_non_startup_error_is_not_retried(monkeypatch):
    """A non-startup error (bad credentials) is not a boot-order race -- it
    must raise on the very first attempt, with no retry/sleep at all."""
    mod = load_coordinator()
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=_fake_probe_conn()))
    create_pool = AsyncMock(side_effect=asyncpg.InvalidPasswordError("bad password"))
    monkeypatch.setattr(mod.asyncpg, "create_pool", create_pool)
    _stub_post_pool_start(mod, monkeypatch)

    coord = mod.MemoryCoordinator()
    with pytest.raises(asyncpg.InvalidPasswordError):
        await coord.start()
    assert create_pool.await_count == 1
