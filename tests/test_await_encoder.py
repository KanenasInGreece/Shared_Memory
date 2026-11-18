"""Caller-owned encoder lifetime, including cancellation inside the call."""
import asyncio
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared-memory' / 'scripts'))

from coordinator import (
    CallerDisconnected, EncoderDeadlineExceeded, MemoryCoordinator, encoder_deadline,
)


class Transport:
    closing = False

    def is_closing(self):
        return self.closing


class Request:
    def __init__(self):
        self.transport = Transport()


@pytest.fixture
def coordinator():
    return MemoryCoordinator.__new__(MemoryCoordinator)


class PendingCall:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False
        self.cleaned = False

    async def run(self):
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            await asyncio.sleep(0.01)
            self.cleaned = True
            raise


@pytest.mark.asyncio
async def test_result_returned(coordinator):
    result = object()

    async def call():
        return result

    assert await coordinator._await_encoder(Request(), call(), time.monotonic() + 1) is result


@pytest.mark.asyncio
async def test_runtime_error_unchanged(coordinator):
    error = RuntimeError('encoder failed')

    async def call():
        raise error

    with pytest.raises(RuntimeError) as caught:
        await coordinator._await_encoder(Request(), call(), None)
    assert caught.value is error


@pytest.mark.asyncio
async def test_deadline_cancels_inner_and_awaits_cleanup(coordinator):
    call = PendingCall()
    with pytest.raises(EncoderDeadlineExceeded):
        await asyncio.wait_for(
            coordinator._await_encoder(Request(), call.run(), time.monotonic() + 0.05),
            timeout=5,
        )
    assert call.cancelled
    assert call.cleaned


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [False, True])
async def test_disconnect_cancels_inner_and_awaits_cleanup(coordinator, missing):
    request = Request()
    call = PendingCall()
    outer = asyncio.create_task(coordinator._await_encoder(request, call.run(), None))
    await asyncio.wait_for(call.started.wait(), 5)
    if missing:
        request.transport = None
    else:
        request.transport.closing = True
    with pytest.raises(CallerDisconnected):
        await asyncio.wait_for(outer, timeout=5)
    assert call.cancelled
    assert call.cleaned


@pytest.mark.asyncio
async def test_no_deadline_waits_for_call(coordinator):
    async def call():
        await asyncio.sleep(0.6)
        return 'finished'

    assert await coordinator._await_encoder(Request(), call(), None) == 'finished'


@pytest.mark.asyncio
async def test_parent_cancellation_reaches_inner_and_awaits_cleanup(coordinator):
    call = PendingCall()
    outer = asyncio.create_task(coordinator._await_encoder(Request(), call.run(), None))
    await asyncio.wait_for(call.started.wait(), 5)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert call.cancelled
    assert call.cleaned


@pytest.mark.parametrize('seconds', [None, True, False, '1', '', float('nan'),
                                     float('inf'), float('-inf'), 10 ** 400, -1, -0.5, 0, 0.0])
def test_invalid_budget_has_no_deadline(seconds):
    assert encoder_deadline(100.0, seconds) is None


@pytest.mark.parametrize('seconds', [1, 0.5])
def test_positive_budget_sets_deadline(seconds):
    assert encoder_deadline(100.0, seconds) == 100.0 + seconds


def test_caller_termination_is_not_encoder_failure():
    assert not issubclass(EncoderDeadlineExceeded, RuntimeError)
    assert not issubclass(CallerDisconnected, RuntimeError)


def test_overflowing_deadline_sum_has_no_deadline():
    assert encoder_deadline(1e308, 1e308) is None
    assert encoder_deadline(100.0, 1e308) == 100.0 + 1e308
