"""Save cancellation must stop before document and outbox writes."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.save_mocks import save_fetchrow
from tests.test_coordinator import (
    _async_ctx, _coordinator_with_mocks, _make_request, coordinator_mod,
)


@pytest.fixture
def save():
    c, conn, _ = _coordinator_with_mocks()
    conn.fetchval.return_value = None
    conn.fetchrow = save_fetchrow(return_value={"id": 99})
    c._project_ingress_error = AsyncMock(return_value=None)
    c._commit_axis_registrations = AsyncMock()
    return c, conn


def request(**extra):
    req = _make_request({
        "content": "A save deadline test fact.",
        "metadata": {"project": "shared-memory-GitHub", "entities": [], "source": "test_agent"},
        **extra,
    })
    req.transport.is_closing.return_value = False
    return req


async def run_save(c, req):
    return await asyncio.wait_for(c.handle_save(req), 10)


def inserts(conn, table):
    return [call for call in conn.mock_calls
            if call.args and isinstance(call.args[0], str)
            and f"INSERT INTO {table}" in " ".join(call.args[0].split())]


def no_inserts(conn):
    assert not inserts(conn, "technical_docs")
    assert not inserts(conn, "neo4j_outbox")


class SlowEmbedder:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False
        self.finished = False

    async def __call__(self, *args):
        self.started.set()
        try:
            await asyncio.sleep(1)
            self.finished = True
            return [0.1] * 1024
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def deadline_response(response):
    assert response.status == 503
    body = json.loads(response.text)
    assert body["status"] == "error"
    assert body["reason"] == "embed_deadline_exceeded"
    assert "wait_s" in body["message"]
    assert "nothing was saved" in body["message"]


@pytest.mark.asyncio
async def test_slow_embed_is_cancelled_without_inserts(save):
    c, conn = save
    embed = SlowEmbedder()
    with patch.object(c, "_embed", new=embed):
        response = await run_save(c, request(wait_s=0.1))
    deadline_response(response)
    assert embed.cancelled
    no_inserts(conn)


@pytest.mark.asyncio
async def test_expired_embed_cannot_write_late(save):
    c, conn = save
    embed = SlowEmbedder()
    with patch.object(c, "_embed", new=embed):
        response = await run_save(c, request(wait_s=0.1))
        deadline_response(response)
        no_inserts(conn)
        await asyncio.sleep(1.2)
        no_inserts(conn)
        assert embed.cancelled
        assert not embed.finished


@pytest.mark.asyncio
async def test_deadline_interrupts_real_embed_retry_backoff(save):
    c, conn = save
    reply = MagicMock(status_code=200)
    reply.json.return_value = {"data": [{"embedding": [0.1] * 1024}]}
    client = AsyncMock()
    client.post.side_effect = [OSError("encoder unavailable"), reply]
    with patch("httpx.AsyncClient", return_value=_async_ctx(client)), \
            patch.object(coordinator_mod, "EMBED_BACKOFF", 0.5):
        response = await run_save(c, request(wait_s=0.1))
    deadline_response(response)
    assert client.post.await_count == 1
    assert client.post.await_count < coordinator_mod.EMBED_RETRIES
    no_inserts(conn)


@pytest.mark.asyncio
async def test_disconnect_cancels_embed_without_inserts(save):
    c, conn = save
    embed = SlowEmbedder()
    req = request()
    with patch.object(c, "_embed", new=embed):
        task = asyncio.create_task(run_save(c, req))
        try:
            await asyncio.wait_for(embed.started.wait(), 10)
            req.transport.is_closing.return_value = True
            response = await asyncio.wait_for(task, 10)
            assert response.status == 499
            assert embed.cancelled
            no_inserts(conn)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"wait_s": "invalid"}])
async def test_fast_save_without_valid_deadline_keeps_response_and_writes(save, extra):
    c, conn = save

    async def original_await(request, call, deadline, calls=None):
        return await call

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch.object(c, "_await_encoder", side_effect=original_await):
            before = await run_save(c, request(**extra))
        conn.reset_mock()
        after = await run_save(c, request(**extra))
    assert before.status == after.status == 200
    assert json.loads(after.text) == json.loads(before.text)
    assert json.loads(after.text)["status"] == "success"
    assert len(inserts(conn, "technical_docs")) == 1
    assert len(inserts(conn, "neo4j_outbox")) == 1


@pytest.mark.asyncio
async def test_real_encoder_failure_preserves_message_without_reason(save):
    c, conn = save
    with patch.object(c, "_embed", new=AsyncMock(side_effect=RuntimeError("encoder failed"))):
        response = await run_save(c, request(wait_s=1))
    assert response.status == 503
    assert json.loads(response.text) == {"status": "error", "message": "encoder failed"}
    no_inserts(conn)


@pytest.fixture
def retrospective():
    c, conn, _ = _coordinator_with_mocks()
    conn.fetchval.return_value = 1
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 240, "type": "decision", "project": "project-b"},
        None,
        {"id": 912},
    ])
    conn.fetch = AsyncMock(return_value=[
        {"id": 601, "type": None, "source_ref": "tests/test_outbox_ledger.py"},
    ])
    return c, conn


def retrospective_request(**extra):
    req = _make_request({"pg_id": 240, "rating": "mixed", "notes": "partly held",
                         "grounded_in": [601], "elicited": True, **extra})
    req.transport.is_closing.return_value = False
    return req


@pytest.mark.asyncio
async def test_retrospective_deadline_cancels_embed_without_inserts(retrospective):
    c, conn = retrospective
    embed = SlowEmbedder()
    with patch.object(c, "_embed", new=embed):
        response = await asyncio.wait_for(
            c.handle_retrospective(retrospective_request(wait_s=0.1)), 10)
    deadline_response(response)
    assert embed.cancelled
    no_inserts(conn)


@pytest.mark.asyncio
async def test_retrospective_disconnect_cancels_embed_without_inserts(retrospective):
    c, conn = retrospective
    embed = SlowEmbedder()
    req = retrospective_request()
    with patch.object(c, "_embed", new=embed):
        task = asyncio.create_task(c.handle_retrospective(req))
        try:
            await asyncio.wait_for(embed.started.wait(), 10)
            req.transport.is_closing.return_value = True
            response = await asyncio.wait_for(task, 10)
            assert response.status == 499
            assert embed.cancelled
            no_inserts(conn)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_retrospective_without_deadline_keeps_response_and_writes(retrospective):
    c, conn = retrospective
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        response = await asyncio.wait_for(c.handle_retrospective(retrospective_request()), 10)
    assert response.status == 200
    body = json.loads(response.text)
    assert body["pg_id"] == 912 and body["target_pg_id"] == 240
    assert len(inserts(conn, "technical_docs")) == 1
    assert len(inserts(conn, "neo4j_outbox")) == 1
