"""A raw encoder passthrough call is counted in /health and ends when its caller
hangs up, per decision:3731 (the caller sets its own deadline and the gateway sets
no total timeout). The gateway stops waiting and closes its connection; whether the
encoder stops its own work depends on the server and build.
"""
import asyncio
import logging
import sys
from pathlib import Path

import pytest
from aiohttp import ClientConnectionError, web
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"))

import coordinator as coord
from tests.test_await_encoder import Transport
from tests.test_telemetry_contract import _StubCoordinator, _OkSession, gateway  # noqa: F401


class _Content:
    def __init__(self, chunks, stall, stall_read=False):
        self._chunks, self._stall, self._stall_read = chunks, stall, stall_read
        self.stall_cancelled = False

    async def iter_any_gen(self):
        for chunk in self._chunks:
            yield chunk
        if self._stall:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.stall_cancelled = True
                raise

    def iter_any(self):
        return self.iter_any_gen()

    async def read(self, n=-1):
        if self._stall_read:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.stall_cancelled = True
                raise
        return b'{"error":"x"}'


class _Upstream:
    def __init__(self, status=200, chunks=(b"ok",), stall=False, stall_read=False):
        self.status = status
        self.headers = {}
        self.content = _Content(chunks, stall, stall_read)


class _Ctx:
    """Simulate one upstream request: `hang` never answers, `answer` answers after
    `delay`, and `fail` raises an error.
    """

    def __init__(self, mode="answer", upstream=None, delay=0.0, exc=None, on_acquired=None):
        self.mode, self.upstream, self.delay, self.exc = mode, upstream or _Upstream(), delay, exc
        self.on_acquired = on_acquired
        self.entered = asyncio.Event()
        self.cancelled = False
        self.exited = False

    async def __aenter__(self):
        self.entered.set()
        if self.mode == "fail":
            raise self.exc
        try:
            if self.mode == "hang":
                await asyncio.Future()
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.on_acquired:
            self.on_acquired()
        return self.upstream

    async def __aexit__(self, *exc_info):
        self.exited = True


class _Session:
    def __init__(self, *contexts):
        self._contexts = list(contexts)
        self.issued = 0

    def request(self, **kwargs):
        self.issued += 1
        return self._contexts.pop(0)


class _Req:
    method = "POST"
    headers = {}
    can_read_body = False
    content_length = None

    def __init__(self, path="/v1/embeddings"):
        self.rel_url = URL(path, encoded=True)
        self.transport = Transport()


@pytest.fixture
def stream(monkeypatch):
    """StreamResponse needs a real transport that the request doubles lack; record
    what it is asked to do.
    """
    seen = {"prepared": 0, "chunks": [], "eof": 0}

    async def prepare(self, request):
        seen["prepared"] += 1

    async def write(self, data):
        seen["chunks"].append(data)

    async def write_eof(self, data=b""):
        seen["eof"] += 1

    monkeypatch.setattr(web.StreamResponse, "prepare", prepare)
    monkeypatch.setattr(web.StreamResponse, "write", write)
    monkeypatch.setattr(web.StreamResponse, "write_eof", write_eof)
    return seen


@pytest.fixture
def disconnects(gateway, monkeypatch):
    counted = []
    monkeypatch.setattr(gateway, "record_llm_client_disconnect", lambda: counted.append(1))
    return counted


def _proxy(g, *contexts):
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _Session(*contexts)
    return proxy


def _snap(proxy, kind="embedder"):
    return proxy._passthrough_calls[kind].snapshot()


async def _hang_up(request, task):
    request.transport.closing = True
    return await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_disconnect_before_headers_cancels_upstream_releases_count_and_answers_499(gateway, disconnects):
    ctx = _Ctx("hang")
    proxy = _proxy(gateway, ctx)
    request = _Req()
    task = asyncio.create_task(proxy.handle_encoder(request))
    await asyncio.wait_for(ctx.entered.wait(), 5)
    # The call is counted once before the upstream request is awaited.
    assert _snap(proxy)["outstanding_count"] == 1 and _snap(proxy, "reranker")["outstanding_count"] == 0
    resp = await _hang_up(request, task)
    assert resp.status == 499
    assert ctx.cancelled
    snap = _snap(proxy)
    assert snap["outstanding_count"] == 0 and snap["last_completed_at"] is None
    assert snap["last_deadline_cancelled_at"] is None
    assert disconnects == [1]


@pytest.mark.asyncio
async def test_disconnect_after_headers_while_upstream_stalls(gateway, disconnects, stream, caplog):
    upstream = _Upstream(chunks=(b"part",), stall=True)
    ctx = _Ctx("answer", upstream)
    proxy = _proxy(gateway, ctx)
    request = _Req("/v1/reranking")
    caplog.set_level(logging.DEBUG)
    task = asyncio.create_task(proxy.handle_encoder(request))
    while not stream["chunks"]:
        await asyncio.sleep(0.01)
    assert _snap(proxy, "reranker")["outstanding_count"] == 1
    resp = await _hang_up(request, task)
    assert isinstance(resp, web.StreamResponse) and resp.status == 200
    assert stream["prepared"] == 1 and stream["eof"] == 0
    assert upstream.content.stall_cancelled and ctx.exited
    snap = _snap(proxy, "reranker")
    assert snap["outstanding_count"] == 0 and snap["last_completed_at"] is not None
    assert disconnects == [1]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "Handler task cancelled during stream" not in caplog.text


@pytest.mark.asyncio
async def test_disconnect_while_the_error_body_stalls_answers_499(gateway, disconnects):
    upstream = _Upstream(status=503, stall_read=True)
    ctx = _Ctx("answer", upstream)
    proxy = _proxy(gateway, ctx)
    request = _Req()
    task = asyncio.create_task(proxy.handle_encoder(request))
    await asyncio.wait_for(ctx.entered.wait(), 5)
    await asyncio.sleep(0.05)  # the headers are in and the body read is stalled
    assert not task.done() and _snap(proxy)["outstanding_count"] == 1
    resp = await _hang_up(request, task)
    assert resp.status == 499
    assert upstream.content.stall_cancelled and ctx.exited
    assert _snap(proxy)["outstanding_count"] == 0 and disconnects == [1]


@pytest.mark.asyncio
async def test_a_response_acquired_as_the_task_is_cancelled_is_released(gateway, disconnects):
    holder = {}
    ctx = _Ctx("answer", on_acquired=lambda: holder["task"].cancel())
    proxy = _proxy(gateway, ctx)
    holder["task"] = asyncio.create_task(proxy.handle_encoder(_Req()))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(holder["task"], 5)
    assert ctx.exited and disconnects == []
    assert _snap(proxy)["outstanding_count"] == 0


@pytest.mark.asyncio
async def test_real_cancellation_is_not_a_disconnect_and_still_propagates(gateway, disconnects, stream, caplog):
    upstream = _Upstream(chunks=(b"part",), stall=True)
    proxy = _proxy(gateway, _Ctx("answer", upstream))
    task = asyncio.create_task(proxy.handle_encoder(_Req()))
    while not stream["chunks"]:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert "Handler task cancelled during stream" in caplog.text
    assert disconnects == [] and _snap(proxy)["outstanding_count"] == 0


@pytest.mark.asyncio
async def test_real_cancellation_before_headers_releases_the_count(gateway, disconnects):
    ctx = _Ctx("hang")
    proxy = _proxy(gateway, ctx)
    task = asyncio.create_task(proxy.handle_encoder(_Req()))
    await asyncio.wait_for(ctx.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert ctx.cancelled and disconnects == []
    assert _snap(proxy)["outstanding_count"] == 0


@pytest.mark.asyncio
async def test_two_simultaneous_calls_are_cancelled_independently(gateway, disconnects):
    first, second = _Ctx("hang"), _Ctx("hang")
    proxy = _proxy(gateway, first, second)
    req_a, req_b = _Req(), _Req()
    task_a = asyncio.create_task(proxy.handle_encoder(req_a))
    await asyncio.wait_for(first.entered.wait(), 5)
    task_b = asyncio.create_task(proxy.handle_encoder(req_b))
    await asyncio.wait_for(second.entered.wait(), 5)
    assert _snap(proxy)["outstanding_count"] == 2
    assert (await _hang_up(req_a, task_a)).status == 499
    assert _snap(proxy)["outstanding_count"] == 1 and not second.cancelled
    assert (await _hang_up(req_b, task_b)).status == 499
    assert _snap(proxy)["outstanding_count"] == 0 and disconnects == [1, 1]


@pytest.mark.asyncio
async def test_a_connected_caller_is_served_after_a_slow_answer(gateway, disconnects, stream):
    ctx = _Ctx("answer", _Upstream(chunks=(b"a", b"b")), delay=0.6)
    proxy = _proxy(gateway, ctx)
    resp = await asyncio.wait_for(proxy.handle_encoder(_Req()), 10)
    assert resp.status == 200
    assert stream["chunks"] == [b"a", b"b"] and stream["eof"] == 1
    snap = _snap(proxy)
    assert snap["outstanding_count"] == 0 and snap["last_completed_at"] is not None
    assert disconnects == []


@pytest.mark.asyncio
async def test_an_upstream_error_is_still_the_typed_refusal_and_counts_as_completed(gateway):
    proxy = _proxy(gateway, _Ctx("answer", _Upstream(status=503)))
    resp = await proxy.handle_encoder(_Req())
    assert resp.status == 503 and resp.headers["X-SM-Fault-Origin"] == "upstream"
    snap = _snap(proxy)
    assert snap["outstanding_count"] == 0 and snap["last_completed_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("exc, status", [(ClientConnectionError("down"), 503), (asyncio.TimeoutError(), 504)])
async def test_the_gateways_own_503_and_504_are_not_completions(gateway, exc, status):
    proxy = _proxy(gateway, _Ctx("fail", exc=exc))
    resp = await proxy.handle_encoder(_Req())
    assert resp.status == status
    snap = _snap(proxy)
    assert snap["outstanding_count"] == 0 and snap["last_completed_at"] is None


@pytest.mark.asyncio
async def test_a_credential_refusal_before_any_upstream_call_is_not_counted(gateway, monkeypatch):
    monkeypatch.setattr(gateway, "encoder_auth_headers", lambda kind, base: {"Authorization": "Bearer x"})
    monkeypatch.setattr(gateway, "_credentialed_route_refusal",
                        lambda request, base: web.json_response({"error": "no"}, status=403))
    proxy = _proxy(gateway)
    resp = await proxy.handle_encoder(_Req())
    assert resp.status == 403 and proxy.session.issued == 0
    snap = _snap(proxy)
    assert snap["outstanding_count"] == 0 and snap["last_completed_at"] is None


# Exercise the helper that combines coordinator and passthrough encoder figures.

def _view(count=0, age=None, done=None, deadline=None):
    return {"outstanding_count": count, "oldest_outstanding_age_s": age,
            "last_completed_at": done, "last_deadline_cancelled_at": deadline}


def test_combine_sums_counts_and_takes_the_larger_age():
    out = coord.combine_encoder_calls(_view(2, 5.0), _view(1, 9.5))
    assert out["outstanding_count"] == 3 and out["oldest_outstanding_age_s"] == 9.5


@pytest.mark.parametrize("mine, theirs, expected", [
    (_view(1, 4.0), _view(), 4.0), (_view(), _view(1, 4.0), 4.0), (_view(), _view(), None)])
def test_combine_handles_one_null_and_both_null_ages(mine, theirs, expected):
    assert coord.combine_encoder_calls(mine, theirs)["oldest_outstanding_age_s"] == expected


def test_combine_compares_completion_times_as_times_not_strings():
    later, earlier = "2026-11-19T10:00:00+00:00", "2026-11-19T11:30:00+02:00"  # 09:30 UTC
    assert earlier > later  # the string order is the wrong way round
    assert coord.combine_encoder_calls(_view(done=later), _view(done=earlier))["last_completed_at"] == later
    assert coord.combine_encoder_calls(_view(done=earlier), _view(done=later))["last_completed_at"] == later


@pytest.mark.parametrize("mine, theirs, expected", [
    ("2026-11-19T10:00:00+00:00", None, "2026-11-19T10:00:00+00:00"),
    (None, "2026-11-19T10:00:00+00:00", "2026-11-19T10:00:00+00:00"),
    (None, None, None), ("not a time", "2026-11-19T10:00:00+00:00", "2026-11-19T10:00:00+00:00")])
def test_combine_handles_missing_and_unreadable_completion_times(mine, theirs, expected):
    assert coord.combine_encoder_calls(_view(done=mine), _view(done=theirs))["last_completed_at"] == expected


@pytest.mark.parametrize("naive, aware, expected", [
    ("2026-11-19T10:00:00", "2026-11-19T09:00:00+00:00", "2026-11-19T10:00:00"),
    ("2026-11-19T08:00:00", "2026-11-19T09:00:00+00:00", "2026-11-19T09:00:00+00:00")])
def test_a_stamp_without_a_timezone_is_read_as_utc_and_never_raises(naive, aware, expected):
    assert coord.combine_encoder_calls(_view(done=naive), _view(done=aware))["last_completed_at"] == expected
    assert coord.combine_encoder_calls(_view(done=aware), _view(done=naive))["last_completed_at"] == expected


def test_a_disconnect_is_not_a_deadline_so_only_the_coordinators_stamp_counts():
    stamp = "2026-11-19T10:00:00+00:00"
    assert coord.combine_encoder_calls(_view(), _view(deadline=stamp))["last_deadline_cancelled_at"] is None
    assert coord.combine_encoder_calls(_view(deadline=stamp), _view())["last_deadline_cancelled_at"] == stamp


def test_combine_without_a_coordinator_view_keeps_the_passthrough_figures():
    out = coord.combine_encoder_calls(None, _view(2, 3.0, "2026-11-19T10:00:00+00:00"))
    assert out == _view(2, 3.0, "2026-11-19T10:00:00+00:00")
    assert set(out) == {"outstanding_count", "oldest_outstanding_age_s",
                        "last_completed_at", "last_deadline_cancelled_at"}


def test_health_adds_passthrough_calls_to_the_coordinators(gateway):
    stub = _StubCoordinator()
    stub.encoder_calls_snapshot = lambda: {"embedder": _view(2, 7.0), "reranker": _view(0)}
    proxy = gateway.AsyncHiveMindProxy()
    proxy.session = _OkSession()
    token = proxy._passthrough_calls["embedder"].begin()
    try:
        deps = asyncio.run(gateway._build_health_checks(proxy, stub))["dependencies"]
    finally:
        proxy._passthrough_calls["embedder"].end(token, completed=False)
    assert deps["embedder"]["outstanding_count"] == 3
    assert deps["embedder"]["oldest_outstanding_age_s"] == 7.0
    assert deps["reranker"]["outstanding_count"] == 0
    assert set(deps["embedder"]) >= {"outstanding_count", "oldest_outstanding_age_s",
                                     "last_completed_at", "last_deadline_cancelled_at"}
