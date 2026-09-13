"""Regression tests for fact:2448 — outbox `conn`-shadow bug in coordinator.py.

Verifies:
  1. R1 & R2: When _apply_outbox_row receives a held batch connection (conn=batch_conn)
     and the Fact carries entities, the inner acquisition for entity_registry does not
     shadow/rebind the `conn` parameter. The status UPDATE (status='applied') is executed
     on `batch_conn` (not on a released connection), and _neo4j_tx_failures_total remains 0.
  2. R3: Postgres-origin errors (asyncpg.PostgresError, asyncpg.InterfaceError) do not
     bump _neo4j_tx_failures_total, while genuine Neo4j errors still do.
  3. Test seam: When conn=None and entities are present, `conn` remains None after
     the entity registry insertion so the `if conn is None:` branch correctly acquires
     a connection for the status update.
"""

from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"))

import coordinator as co


# ── Test Doubles ─────────────────────────────────────────────────────────────

class FakeConnection:
    """Fake asyncpg Connection simulating release-on-exit semantics."""

    def __init__(self, name: str = "conn"):
        self.name = name
        self.released = False
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args):
        if self.released:
            raise asyncpg.InterfaceError(
                "cannot call Connection.execute(): connection has been released back to the pool"
            )
        self.executed.append((query, args))
        return "UPDATE 1"

    async def executemany(self, query: str, args):
        if self.released:
            raise asyncpg.InterfaceError(
                "cannot call Connection.executemany(): connection has been released back to the pool"
            )
        self.executed.append((query, tuple(args)))
        return None


class FakeAcquireContext:
    """Async context manager simulating asyncpg pool.acquire()."""

    def __init__(self, conn: FakeConnection):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.conn.released = True
        return False


def _async_ctx(value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outbox_row_applies_on_held_conn_with_entities():
    """R1 & R2: inner entity acquisition must not shadow held batch conn."""
    c = co.MemoryCoordinator()
    c._project_identity = AsyncMock(return_value=None)
    c._domain_identities = AsyncMock(return_value=[])

    session = AsyncMock()
    session.run = AsyncMock(return_value=AsyncMock())
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=_async_ctx(session))

    batch_conn = FakeConnection(name="batch")
    acquired_inner_conns: list[FakeConnection] = []

    def _acquire():
        conn = FakeConnection(name=f"inner_{len(acquired_inner_conns)}")
        acquired_inner_conns.append(conn)
        return FakeAcquireContext(conn)

    c._acquire = MagicMock(side_effect=_acquire)

    await c._apply_outbox_row(
        outbox_id=1,
        pg_id=42,
        params={"content_snippet": "x", "entities": ["SharedMemory"], "type": "fact"},
        retries=0,
        conn=batch_conn,
    )

    # 1. Assert status='applied' UPDATE reached batch_conn
    applied_calls = [args for q, args in batch_conn.executed if "status='applied'" in q]
    assert len(applied_calls) == 1, f"Expected status='applied' on batch_conn, got {batch_conn.executed}"
    assert applied_calls[0] == (1,)

    # 2. Assert batch_conn was never marked released by the outbox row apply
    assert batch_conn.released is False

    # 3. Assert _neo4j_tx_failures_total is 0 on this success path
    assert c._neo4j_tx_failures_total == 0

    # 4. Verify the inner entity-registry connection was acquired, executed, and released
    assert len(acquired_inner_conns) == 1
    assert acquired_inner_conns[0].released is True
    assert any("INSERT INTO entity_registry" in q for q, _ in acquired_inner_conns[0].executed)


@pytest.mark.asyncio
async def test_outbox_postgres_error_does_not_bump_neo4j_tx_failures():
    """R3: asyncpg.PostgresError must not bump _neo4j_tx_failures_total."""
    c = co.MemoryCoordinator()
    c._project_identity = AsyncMock(return_value=None)
    c._domain_identities = AsyncMock(return_value=[])

    session = AsyncMock()
    session.run = AsyncMock(return_value=AsyncMock())
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=_async_ctx(session))

    batch_conn = FakeConnection(name="batch")

    class PostgresErrorConnection(FakeConnection):
        async def executemany(self, query: str, args):
            raise asyncpg.PostgresError("simulated postgres error in entity registry")

    calls = 0

    def _acquire():
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeAcquireContext(PostgresErrorConnection(name="inner_err"))
        # Subsequent acquisition for retry bookkeeping in except block
        return FakeAcquireContext(FakeConnection(name=f"retry_{calls}"))

    c._acquire = MagicMock(side_effect=_acquire)

    assert c._neo4j_tx_failures_total == 0
    await c._apply_outbox_row(
        outbox_id=1,
        pg_id=42,
        params={"content_snippet": "x", "entities": ["SharedMemory"], "type": "fact"},
        retries=0,
        conn=batch_conn,
    )

    # Postgres error must not bump Neo4j failure counter
    assert c._neo4j_tx_failures_total == 0
    # Retry bookkeeping occurred
    assert calls >= 2


@pytest.mark.asyncio
async def test_outbox_postgres_interface_error_does_not_bump_neo4j_tx_failures():
    """R3: asyncpg.InterfaceError must not bump _neo4j_tx_failures_total."""
    c = co.MemoryCoordinator()
    c._project_identity = AsyncMock(return_value=None)
    c._domain_identities = AsyncMock(return_value=[])

    session = AsyncMock()
    session.run = AsyncMock(return_value=AsyncMock())
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=_async_ctx(session))

    batch_conn = FakeConnection(name="batch")

    class InterfaceErrorConnection(FakeConnection):
        async def executemany(self, query: str, args):
            raise asyncpg.InterfaceError("simulated connection lost")

    calls = 0

    def _acquire():
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeAcquireContext(InterfaceErrorConnection(name="inner_err"))
        return FakeAcquireContext(FakeConnection(name=f"retry_{calls}"))

    c._acquire = MagicMock(side_effect=_acquire)

    assert c._neo4j_tx_failures_total == 0
    await c._apply_outbox_row(
        outbox_id=1,
        pg_id=42,
        params={"content_snippet": "x", "entities": ["SharedMemory"], "type": "fact"},
        retries=0,
        conn=batch_conn,
    )

    # InterfaceError must not bump Neo4j failure counter
    assert c._neo4j_tx_failures_total == 0
    assert calls >= 2


@pytest.mark.asyncio
async def test_outbox_neo4j_error_still_bumps_neo4j_tx_failures():
    """R3: A genuine Neo4j failure must still bump _neo4j_tx_failures_total."""
    c = co.MemoryCoordinator()
    c._project_identity = AsyncMock(return_value=None)
    c._domain_identities = AsyncMock(return_value=[])

    class _DeadNeo4j:
        def session(self, **kw):
            raise RuntimeError("Neo4j unavailable")

    c._neo4j = _DeadNeo4j()
    batch_conn = FakeConnection(name="batch")
    c._acquire = MagicMock(side_effect=lambda: FakeAcquireContext(FakeConnection(name="retry")))

    assert c._neo4j_tx_failures_total == 0
    await c._apply_outbox_row(
        outbox_id=1,
        pg_id=42,
        params={"content_snippet": "x", "entities": ["SharedMemory"], "type": "fact"},
        retries=0,
        conn=batch_conn,
    )

    assert c._neo4j_tx_failures_total == 1


@pytest.mark.asyncio
async def test_outbox_row_applies_with_conn_none_and_entities():
    """Test seam: when conn=None and entities are present, conn remains None for status update."""
    c = co.MemoryCoordinator()
    c._project_identity = AsyncMock(return_value=None)
    c._domain_identities = AsyncMock(return_value=[])

    session = AsyncMock()
    session.run = AsyncMock(return_value=AsyncMock())
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=_async_ctx(session))

    acquired_conns: list[FakeConnection] = []

    def _acquire():
        conn = FakeConnection(name=f"acq_{len(acquired_conns)}")
        acquired_conns.append(conn)
        return FakeAcquireContext(conn)

    c._acquire = MagicMock(side_effect=_acquire)

    await c._apply_outbox_row(
        outbox_id=1,
        pg_id=42,
        params={"content_snippet": "x", "entities": ["SharedMemory"], "type": "fact"},
        retries=0,
        conn=None,
    )

    assert c._neo4j_tx_failures_total == 0
    # Two acquisitions: 1 for entity_registry, 1 for status='applied'
    assert len(acquired_conns) == 2
    assert any("INSERT INTO entity_registry" in q for q, _ in acquired_conns[0].executed)
    applied_calls = [args for q, args in acquired_conns[1].executed if "status='applied'" in q]
    assert len(applied_calls) == 1
    assert applied_calls[0] == (1,)
