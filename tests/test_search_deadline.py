"""Search uses one caller deadline across embedding and reranking."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_rerank_contract import (
    _async_ctx, _candidates, _coordinator_with_mocks, _make_request,
    _reranker_returning,
)


class SleepingEncoder:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self, *args, **kwargs):
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("test encoder was not cancelled")


@pytest.fixture
def search():
    c, conn, _ = _coordinator_with_mocks()
    conn.fetch.return_value = _candidates(2)
    c._resolve_search_filters = AsyncMock(return_value=(None, None, None))
    c._load_stale_map = AsyncMock(return_value={})
    c._expand_graph_context_batch = AsyncMock(return_value={})
    c._compose_search_hits = AsyncMock()
    c._annotate_narrative_entries = AsyncMock()
    return c, conn


async def run_search(c, body, *, embed=None, post=None, request=None):
    request = request if request is not None else _make_request(body)
    request.transport.is_closing.return_value = False
    client = AsyncMock()
    client.post = post if post is not None else _reranker_returning(2)
    with patch.object(c, "_embed", new=embed if embed is not None else
                      AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient", return_value=_async_ctx(client)):
            return await asyncio.wait_for(c.handle_search(request), 5)


def vector_rows(response):
    assert response.status == 200
    rows = json.loads(response.text)["results"]
    assert [row["pg_id"] for row in rows] == [100, 101]
    assert all(row["ranked"] is False for row in rows)
    assert all(row["score"] is None for row in rows)
    return rows


@pytest.mark.asyncio
async def test_embed_deadline_cancels_and_serves_keyword(search):
    c, _ = search
    embed = SleepingEncoder()
    post = _reranker_returning(2)
    response = await run_search(c, {"query": "candidate", "encoder_wait_s": 0.05},
                                embed=embed, post=post)
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["fallback"] == "keyword"
    assert [r["pg_id"] for r in payload["results"]] == [100, 101]
    assert all(r["ranked"] is False for r in payload["results"])
    assert embed.cancelled
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_rerank_deadline_cancels_without_encoder_fault(search):
    c, _ = search
    post = SleepingEncoder()
    response = await run_search(c, {"query": "candidate", "encoder_wait_s": 0.05},
                                post=post)
    vector_rows(response)
    assert post.cancelled
    assert c._rerank_failures == 0
    assert c._rerank_fallback_last_ts is None
    assert c._rerank_deadline_cancelled_total == 1
    assert c._rerank_deadline_last_ts is not None
    assert c._rerank_ring.errors == 0


@pytest.mark.asyncio
async def test_deadline_spent_in_database_never_calls_reranker(search):
    c, conn = search

    async def fetch(*args):
        await asyncio.sleep(0.1)
        return _candidates(2)

    conn.fetch.side_effect = fetch
    post = _reranker_returning(2)
    response = await run_search(c, {"query": "candidate", "encoder_wait_s": 0.05},
                                post=post)
    vector_rows(response)
    post.assert_not_called()
    assert c._rerank_deadline_cancelled_total == 1
    assert c._rerank_failures == 0

    assert c._rerank_calls.snapshot()["last_deadline_cancelled_at"] is not None

@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["embed", "rerank"])
async def test_disconnect_cancels_encoder_and_stops_work(search, stage):
    c, conn = search
    encoder = SleepingEncoder()
    request = _make_request({"query": "candidate"})
    task = asyncio.create_task(run_search(
        c, {}, request=request,
        **({"embed": encoder} if stage == "embed" else {"post": encoder})))
    try:
        await asyncio.wait_for(encoder.started.wait(), 5)
        reads = conn.fetch.await_count
        request.transport.is_closing.return_value = True
        response = await asyncio.wait_for(task, 5)
        assert response.status == 499
        assert encoder.cancelled
        assert conn.fetch.await_count == reads
        c._load_stale_map.assert_not_awaited()
        if stage == "embed":
            conn.fetch.assert_not_awaited()
        assert c._rerank_failures == 0
        assert c._rerank_deadline_cancelled_total == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"encoder_wait_s": "invalid"}])
async def test_no_valid_deadline_preserves_fast_encoder_response(search, extra):
    c, _ = search
    body = {"query": "candidate", **extra}

    async def original_await(request, call, deadline, calls=None):
        return await call

    with patch.object(c, "_await_encoder", side_effect=original_await):
        before = await run_search(c, body)
    after = await run_search(c, body)
    assert after.status == before.status == 200
    assert json.loads(after.text) == json.loads(before.text)
    rows = json.loads(after.text)["results"]
    assert [r["pg_id"] for r in rows] == [100, 101]
    assert all(r["ranked"] is True for r in rows)
    assert c._rerank_failures == c._rerank_deadline_cancelled_total == 0
