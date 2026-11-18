"""The gateway publishes how long its oldest encoder call has waited (decision:3731).

It sets no timeout of its own; the age on /health replaces the guess.
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"))

import coordinator as coord
from coordinator import MemoryCoordinator, OutstandingCalls, _tracked
from tests.test_await_encoder import Request
from tests.test_rerank_contract import (
    _async_ctx, _candidates, _coordinator_with_mocks, _make_request,
)
from tests.test_search_deadline import SleepingEncoder, run_search, search  # noqa: F401
from tests.test_telemetry_contract import (  # noqa: F401
    _StubCoordinator, _OkSession, gateway,
)
import telemetry_contract as tc


def test_oldest_age_grows_and_end_clears(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(coord.time, "monotonic", lambda: clock[0])
    calls = OutstandingCalls()
    assert calls.snapshot()["oldest_outstanding_age_s"] is None
    first = calls.begin()
    clock[0] = 110.0
    second = calls.begin()
    clock[0] = 125.0
    snap = calls.snapshot()
    assert snap["outstanding_count"] == 2
    assert snap["oldest_outstanding_age_s"] == 25.0
    calls.end(first, completed=True)
    assert calls.snapshot()["oldest_outstanding_age_s"] == 15.0
    assert calls.snapshot()["last_completed_at"] is not None
    calls.end(second, completed=False)
    final = calls.snapshot()
    assert final["outstanding_count"] == 0 and final["oldest_outstanding_age_s"] is None


def test_unknown_token_and_failed_call_leave_no_completion_stamp():
    calls = OutstandingCalls()
    calls.end(999, completed=True)
    assert calls.snapshot()["last_completed_at"] is None
    token = calls.begin()
    calls.end(token, completed=False)
    assert calls.snapshot()["last_completed_at"] is None


@pytest.mark.asyncio
async def test_tracked_clears_when_cancelled_and_not_marked_completed():
    calls = OutstandingCalls()

    async def hang():
        await asyncio.Future()

    task = asyncio.create_task(_tracked(calls, hang()))
    await asyncio.sleep(0.01)
    assert calls.snapshot()["outstanding_count"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snap = calls.snapshot()
    assert snap["outstanding_count"] == 0 and snap["last_completed_at"] is None


@pytest.mark.asyncio
async def test_deadline_cancel_stamps_but_disconnect_does_not():
    c = MemoryCoordinator.__new__(MemoryCoordinator)
    calls = OutstandingCalls()

    async def hang():
        await asyncio.Future()

    with pytest.raises(coord.EncoderDeadlineExceeded):
        await asyncio.wait_for(c._await_encoder(
            Request(), _tracked(calls, hang()), time.monotonic() + 0.05, calls=calls), 5)
    snap = calls.snapshot()
    assert snap["outstanding_count"] == 0
    assert snap["last_deadline_cancelled_at"] is not None

    other = OutstandingCalls()
    request = Request()
    request.transport.closing = True
    with pytest.raises(coord.CallerDisconnected):
        await asyncio.wait_for(c._await_encoder(
            request, _tracked(other, hang()), None, calls=other), 5)
    assert other.snapshot()["last_deadline_cancelled_at"] is None
    assert other.snapshot()["outstanding_count"] == 0


@pytest.mark.asyncio
async def test_hanging_embed_is_visible_while_waiting_then_cleared(search):
    c, _conn = search
    hang = SleepingEncoder()
    seen = {}

    async def embed(*args, **kwargs):
        token = c._embed_calls.begin()
        try:
            return await hang()
        finally:
            c._embed_calls.end(token, False)

    async def watcher():
        await hang.started.wait()
        seen.update(c.encoder_calls_snapshot()["embedder"])

    task = asyncio.create_task(watcher())
    await run_search(c, {"query": "q", "encoder_wait_s": 0.3, "wait_s": 20},
                     embed=embed)
    await task
    assert seen["outstanding_count"] == 1
    assert seen["oldest_outstanding_age_s"] is not None
    after = c.encoder_calls_snapshot()["embedder"]
    assert after["outstanding_count"] == 0
    assert after["last_deadline_cancelled_at"] is not None


@pytest.mark.asyncio
async def test_real_embed_post_is_tracked_and_stamps_completion():
    c = MemoryCoordinator.__new__(MemoryCoordinator)
    c._embed_calls = OutstandingCalls()
    c._embed_ring = MagicMock()
    seen = {}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.0] * 3}]}

    async def post(*args, **kwargs):
        seen.update(c._embed_calls.snapshot())
        return Resp()

    client = MagicMock()
    client.post = post
    assert await c._embed("hello", client) == [0.0] * 3
    assert seen["outstanding_count"] == 1
    final = c._embed_calls.snapshot()
    assert final["outstanding_count"] == 0 and final["last_completed_at"] is not None


@pytest.mark.asyncio
async def test_rerank_wait_is_tracked_through_handle_search(search):
    c, _conn = search
    seen = {}

    async def slow_post(*args, **kwargs):
        seen.update(c.encoder_calls_snapshot()["reranker"])
        await asyncio.sleep(60)

    await run_search(c, {"query": "q", "encoder_wait_s": 0.5, "wait_s": 20},
                     post=slow_post)
    assert seen["outstanding_count"] == 1
    after = c.encoder_calls_snapshot()["reranker"]
    assert after["outstanding_count"] == 0
    assert after["last_deadline_cancelled_at"] is not None


def _health(g, coordinator_stub):
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()
    return asyncio.run(g._build_health_checks(proxy, coordinator_stub))


def test_health_carries_the_values_and_state_is_unchanged(gateway):
    quiet = _health(gateway, _StubCoordinator())
    busy_stub = _StubCoordinator()
    busy_stub.encoder_calls_snapshot = lambda: {
        "embedder": {"outstanding_count": 2, "oldest_outstanding_age_s": 86400.0,
                     "last_completed_at": "2026-11-18T10:00:00+00:00",
                     "last_deadline_cancelled_at": None},
        "reranker": {"outstanding_count": 0, "oldest_outstanding_age_s": None,
                     "last_completed_at": None,
                     "last_deadline_cancelled_at": "2026-11-18T10:01:00+00:00"},
    }
    busy = _health(gateway, busy_stub)
    emb = busy["dependencies"]["embedder"]
    assert emb["outstanding_count"] == 2
    assert emb["oldest_outstanding_age_s"] == 86400.0
    assert emb["last_completed_at"] == "2026-11-18T10:00:00+00:00"
    assert busy["dependencies"]["reranker"]["last_deadline_cancelled_at"] == "2026-11-18T10:01:00+00:00"
    # A day-long wait changes no state, reason or warning: the age is evidence, not a verdict.
    for name in ("embedder", "reranker"):
        assert busy["dependencies"][name]["state"] == quiet["dependencies"][name]["state"]
        assert busy["dependencies"][name]["reason"] == quiet["dependencies"][name]["reason"]
    assert busy["warnings"] == quiet["warnings"]
    assert busy["status"] == quiet["status"]


def test_health_survives_a_coordinator_without_the_snapshot(gateway):
    del_stub = type("NoSnap", (_StubCoordinator,), {"encoder_calls_snapshot": None})()
    payload = _health(gateway, del_stub)
    assert "outstanding_count" not in payload["dependencies"]["embedder"]
    assert payload["dependencies"]["embedder"]["state"] == "ok"

    class Broken(_StubCoordinator):
        def encoder_calls_snapshot(self):
            raise RuntimeError("boom")

    payload = _health(gateway, Broken())
    assert "oldest_outstanding_age_s" not in payload["dependencies"]["reranker"]
    assert payload["status"]


def test_every_new_key_is_declared_in_the_contract():
    for kind in ("embedder", "reranker"):
        for key in ("outstanding_count", "oldest_outstanding_age_s",
                    "last_completed_at", "last_deadline_cancelled_at"):
            assert f"dependencies.{kind}.{key}" in tc.HEALTH
