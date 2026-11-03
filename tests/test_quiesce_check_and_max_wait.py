"""Tests for Part C (logged check at quiesce) and Part D (optional max_wait_s).

Invariants tested:
- I5: The check never ends a request and never delays the quiesce (failure to re-introduce: await the read before the lock)
- I7: max_wait_s absent = no limit; expiry = one row, no move (failure to re-introduce: move on expiry)
- I11: One read per quiesce, none on a re-arm or a caller gone while queued; no paid endpoint contacted (failure to re-introduce: start at the top of _begin_quiesce)
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web, ClientTimeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"))

import check_config
import coordinator as co
import hive_mind_proxy as proxy
import secure_env
import telemetry_contract as tc


# ══════════════════════════════════════════════════════════════════════════════
# Invariant I5: The check never ends a request and never delays the quiesce
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_invariant_i5_check_never_delays_quiesce_nor_ends_request(caplog):
    """The quiesce check runs in the background. Even if the probe hangs,
    the advisory lock acquire completes without delay and the in-flight
    request is never ended or cancelled."""
    co_inst = co.MemoryCoordinator()
    fake_conn = MagicMock()
    fake_conn.is_closed = MagicMock(return_value=False)
    fake_conn.fetchval = AsyncMock(return_value=1)
    co_inst._quiesce_conn = fake_conn

    check_started = asyncio.Event()
    check_finished = asyncio.Event()

    async def slow_hook():
        check_started.set()
        try:
            await asyncio.sleep(10.0)  # Simulates a slow / wedged probe
            check_finished.set()
        except asyncio.CancelledError:
            raise

    co_inst.quiesce_check_hook = slow_hook

    # Quiesce starts
    res = await co_inst._begin_quiesce(max_seconds=120)
    assert res["quiesced"] is True
    assert res["reason"] is None
    # Check task was started in background, not awaited before the lock
    assert check_started.is_set()
    assert not check_finished.is_set()
    assert co_inst._quiesce_check_task is not None
    assert not co_inst._quiesce_check_task.done()

    # Quiesce ends — task is cancelled and collected
    await co_inst._end_quiesce(ended_by="resume")
    assert co_inst._quiesce_check_task is None


# ══════════════════════════════════════════════════════════════════════════════
# Invariant I11: One read per quiesce, none on re-arm or caller gone while queued;
#                no paid endpoint contacted
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_invariant_i11_quiesce_check_lifecycle_and_rearm():
    """One read per quiesce. A re-arm (started_here=False) does not start a second read.
    A caller gone while queued starts none."""
    co_inst = co.MemoryCoordinator()
    fake_conn = MagicMock()
    fake_conn.is_closed = MagicMock(return_value=False)
    fake_conn.fetchval = AsyncMock(return_value=1)
    co_inst._quiesce_conn = fake_conn

    hook_calls = 0

    async def counting_hook():
        nonlocal hook_calls
        hook_calls += 1

    co_inst.quiesce_check_hook = counting_hook

    # First quiesce: started_here is True
    res1 = await co_inst._begin_quiesce(max_seconds=120)
    assert res1["quiesced"] is True
    assert hook_calls == 1

    # Second quiesce (re-arm): started_here is False, no second read
    res2 = await co_inst._begin_quiesce(max_seconds=120)
    assert res2["quiesced"] is True
    assert hook_calls == 1

    await co_inst._end_quiesce(ended_by="resume")


@pytest.mark.asyncio
async def test_invariant_i11_caller_gone_while_queued_starts_none():
    """A caller disconnecting while queued before started_here starts no check."""
    co_inst = co.MemoryCoordinator()
    hook_calls = 0

    async def counting_hook():
        nonlocal hook_calls
        hook_calls += 1

    co_inst.quiesce_check_hook = counting_hook

    # Hold the queue lock for real; the caller starts connected, queues behind it, then leaves.
    await co_inst._quiesce_lock.acquire()
    try:
        req = MagicMock()
        req.transport = MagicMock()
        task = asyncio.create_task(co_inst._begin_quiesce(max_seconds=120, request=req))
        await asyncio.sleep(0.3)
        assert not task.done(), "the caller must still be queued behind the held lock"
        req.transport = None
        res = await asyncio.wait_for(task, timeout=5)
        assert res["reason"] == "caller_gone"
        assert hook_calls == 0
        assert co._backup_quiesce is False, "a caller that left while queued must not start a quiesce"
    finally:
        co_inst._quiesce_lock.release()


@pytest.mark.asyncio
async def test_invariant_i11_no_paid_endpoint_contacted(caplog):
    """A paid backend with requests in flight logs 'not_checked' and makes
    NO network requests."""
    backend_url = "https://api.openai.com/v1"
    proxy._llm_inflight[backend_url] = 2
    proxy._llm_inflight_started[backend_url] = [time.monotonic()]
    proxy.LLM_BACKEND_MODELS[backend_url] = "gpt-4o"

    try:
        with patch.object(proxy, "_self_hosted", return_value=False):
            with patch("aiohttp.ClientSession.get") as mock_get:
                with caplog.at_level("INFO", logger="hive-proxy"):
                    await proxy._quiesce_backend_check()

                mock_get.assert_not_called()
                assert any("signal=not_checked" in r.message for r in caplog.records)
                assert any("quiesce-backend-check" in r.message for r in caplog.records)
    finally:
        proxy._llm_inflight.clear()
        proxy._llm_inflight_started.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Invariant I7: max_wait_s absent = no limit; expiry = one row, no move
# ══════════════════════════════════════════════════════════════════════════════

class _MockCtx:
    def __init__(self, resp):
        self.resp = resp
    async def __aenter__(self):
        return self.resp
    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_invariant_i7_max_wait_s_absent_means_no_limit():
    """When max_wait_s is absent (None), no wait_for timeout is imposed."""
    p = proxy.AsyncHiveMindProxy()
    backend = "http://127.0.0.1:8000"
    proxy.LLM_BACKEND_MAX_WAIT_S[backend] = None
    proxy.LLM_BACKEND_MODELS[backend] = "local-m"

    req = MagicMock()
    req.method = "POST"
    req.rel_url = "/v1/chat/completions"
    req.headers = {}

    p.llm_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.content = MagicMock()
    mock_resp.content.read = AsyncMock(return_value=b'{"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}')
    mock_resp.headers = {"Content-Type": "application/json"}
    p.llm_session.request = MagicMock(return_value=_MockCtx(mock_resp))

    outcome = await p._one_llm_try(req, backend, {"messages": []}, est=10.0)
    assert outcome["kind"] == "complete"
    assert proxy._llm_max_wait_expired.get(backend, 0) == 0


@pytest.mark.asyncio
async def test_invariant_i7_expiry_returns_lost_and_does_not_move(caplog):
    """When max_wait_s expires:
    1. Returns result dict of kind 'lost' (reason 'connection_lost')
    2. Increments llm.pool.*.max_wait_expired and pairs last timestamp
    3. In _walk_llm, it backlogs and DOES NOT move to the next backend."""
    p = proxy.AsyncHiveMindProxy()
    b1 = "http://127.0.0.1:8001"
    b2 = "http://127.0.0.1:8002"

    proxy.LLM_POOL = [b1, b2]
    proxy.LLM_BACKENDS = [b1, b2]
    proxy.LLM_BACKEND_MAX_WAIT_S[b1] = 0.05
    proxy.LLM_BACKEND_MAX_WAIT_S[b2] = 0.05
    proxy.LLM_BACKEND_MODELS[b1] = "model-1"
    proxy.LLM_BACKEND_MODELS[b2] = "model-2"
    proxy.LLM_BACKEND_NCTX[b1] = 8192
    proxy.LLM_BACKEND_NCTX[b2] = 8192
    proxy.LLM_BACKEND_MAX_OUTPUT[b1] = 2048
    proxy.LLM_BACKEND_MAX_OUTPUT[b2] = 2048
    proxy.LLM_BACKEND_PRIORITY[b1] = 1
    proxy.LLM_BACKEND_PRIORITY[b2] = 2

    req = MagicMock()
    req.method = "POST"
    req.rel_url = "/v1/chat/completions"
    req.headers = {}

    p.llm_session = MagicMock()

    # b1 hangs until timeout
    async def slow_read():
        await asyncio.sleep(0.5)
        return b"{}"

    mock_resp = MagicMock()
    mock_resp.content = MagicMock()
    mock_resp.content.read = slow_read
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    p.llm_session.request = MagicMock(return_value=_MockCtx(mock_resp))

    with caplog.at_level("ERROR", logger="hive-proxy"):
        resp = await p._walk_llm(req, b'{"prompt": "hi"}', {"messages": []})

    # Response is backlog with connection_lost
    assert resp.status == 422
    body = json.loads(resp.text)
    assert body["error"] == "backlog"
    assert body["reason"] == "connection_lost"

    # Crucial: Did NOT move on to b2! Only b1 was tried.
    assert p.llm_session.request.call_count == 1
    call_url = p.llm_session.request.call_args.kwargs["url"]
    assert b1 in call_url

    # Counter and timestamp were recorded
    assert proxy._llm_max_wait_expired[b1] == 1
    assert proxy._llm_max_wait_expired_last_ts[b1] is not None
    assert any("max_wait_s expired" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_d3_connect_timeout_under_max_wait_s_takes_connect_failure_path():
    """D3: aiohttp connect timeout subclasses asyncio.TimeoutError.
    Under a set max_wait_s, if connect timeout raises before max_wait_s expires,
    cm.expired() is False, so it re-raises and takes the ConnectionTimeoutError path (504/down)
    without incrementing _llm_max_wait_expired.
    """
    from aiohttp import ConnectionTimeoutError

    p = proxy.AsyncHiveMindProxy()
    backend = "http://127.0.0.1:8000"
    proxy.LLM_BACKEND_MAX_WAIT_S[backend] = 10.0  # 10s wait limit
    proxy.LLM_BACKEND_MODELS[backend] = "m"
    proxy._llm_max_wait_expired[backend] = 0

    req = MagicMock()
    req.method = "POST"
    req.rel_url = "/v1/chat/completions"
    req.headers = {}

    class _ConnectTimeoutCtx:
        async def __aenter__(self):
            # Connect timeout raised immediately (well before 10s max_wait_s expires)
            raise ConnectionTimeoutError("Connection timed out")
        async def __aexit__(self, *a):
            return False

    p.llm_session = MagicMock()
    p.llm_session.request = MagicMock(return_value=_ConnectTimeoutCtx())

    outcome = await p._one_llm_try(req, backend, {"messages": []}, est=10.0)
    assert outcome["kind"] == "down"
    assert outcome["status"] == 504
    assert outcome["accepted"] is False
    assert proxy._llm_max_wait_expired.get(backend, 0) == 0, (
        "connect timeout must not increment max_wait_expired"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Invalid max_wait_s Refusal at Load (Gateway exit 78, check_config exit 1)
# ══════════════════════════════════════════════════════════════════════════════

def test_invalid_max_wait_s_values_refused_at_load():
    """Invalid max_wait_s (non-numeric, <=0, nan, inf, bool) are refused."""
    invalid_examples = ["fast", 0, -10, float("nan"), float("inf"), True, False]
    for bad_val in invalid_examples:
        picture = {
            "LLM_BACKENDS_JSON": json.dumps([{
                "url": "http://127.0.0.1:8000",
                "n_ctx": 8192,
                "max_output_tokens": 2048,
                "max_wait_s": bad_val,
            }])
        }
        errs = secure_env.llm_declaration_errors(picture)
        assert len(errs) == 1, f"Expected refusal for bad value {bad_val!r}"
        assert errs[0][1] == "max_wait_s"


def test_check_config_phase_a_mirrors_max_wait_s_refusal():
    """check_config._phase_a_declaration_lines names the entry and field max_wait_s."""
    picture = {
        "LLM_BACKENDS_JSON": json.dumps([{
            "url": "http://127.0.0.1:8000",
            "n_ctx": 8192,
            "max_output_tokens": 2048,
            "max_wait_s": "non-numeric",
        }])
    }
    with patch.object(check_config, "_gateway_picture", return_value=picture):
        lines = check_config._phase_a_declaration_lines()
        assert any("field max_wait_s" in line for line in lines)
        assert any("exit 78" in line for line in lines)


# ══════════════════════════════════════════════════════════════════════════════
# Quiesce check: Signals, derived counts, and cancellation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_quiesce_check_llama_slots_derived_counts_no_prompt_leak(caplog):
    """llama.cpp /slots logs derived counts only and never leaks prompt text."""
    backend_url = "http://127.0.0.1:8080"
    proxy._llm_inflight[backend_url] = 1
    proxy._llm_inflight_started[backend_url] = [time.monotonic()]
    proxy.LLM_BACKEND_MODELS[backend_url] = "qwen-27b"

    slots_payload = [
        {"id": 0, "is_processing": True, "prompt": "SUPER_SECRET_PROMPT_THAT_MUST_NOT_LEAK"},
        {"id": 1, "is_processing": False},
        {"id": 2, "is_processing": True, "prompt": "ANOTHER_SECRET"},
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=json.dumps(slots_payload).encode("utf-8"))

    try:
        with patch.object(proxy, "_self_hosted", return_value=True):
            with patch("aiohttp.ClientSession.get", return_value=mock_resp):
                mock_resp.__aenter__.return_value = mock_resp
                with caplog.at_level("INFO", logger="hive-proxy"):
                    await proxy._quiesce_backend_check()

                assert any("signal=slots" in r.message for r in caplog.records)
                assert any("reading=processing=2/3" in r.message for r in caplog.records)
                # Confirm prompt text is NEVER in any log line
                for r in caplog.records:
                    assert "SUPER_SECRET_PROMPT" not in r.message
                    assert "ANOTHER_SECRET" not in r.message
    finally:
        proxy._llm_inflight.clear()
        proxy._llm_inflight_started.clear()


@pytest.mark.asyncio
async def test_quiesce_check_vllm_metrics_derived_counts(caplog):
    """vLLM /metrics logs running requests derived count, parsing line by line,
    accepting labelled and unlabelled metrics, ignoring comments, and summing across models."""
    backend_url = "http://127.0.0.1:8000"
    proxy._llm_inflight[backend_url] = 1
    proxy._llm_inflight_started[backend_url] = [time.monotonic()]
    proxy.LLM_BACKEND_MODELS[backend_url] = "vllm-model"

    # First call to /slots returns 404, second to /metrics returns 200 prometheus text
    resp_slots = MagicMock()
    resp_slots.status = 404

    class _SlotsCtx:
        async def __aenter__(self): return resp_slots
        async def __aexit__(self, *a): return False

    metrics_text = """# HELP vllm:num_requests_running Number of requests running
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="m1"} 2.0
# middle comment
vllm:num_requests_running{model_name="m2"} 3.5
num_requests_running 1.5
"""
    resp_metrics = MagicMock()
    resp_metrics.status = 200
    resp_metrics.text = AsyncMock(return_value=metrics_text)

    class _MetricsCtx:
        async def __aenter__(self): return resp_metrics
        async def __aexit__(self, *a): return False

    def mock_get(url, **kwargs):
        if url.endswith("/slots"):
            return _SlotsCtx()
        return _MetricsCtx()

    try:
        with patch.object(proxy, "_self_hosted", return_value=True):
            with patch("aiohttp.ClientSession.get", side_effect=mock_get):
                with caplog.at_level("INFO", logger="hive-proxy"):
                    await proxy._quiesce_backend_check()

                assert any("signal=metrics" in r.message for r in caplog.records)
                # 2.0 + 3.5 + 1.5 = 7.0 -> reading=running=7
                assert any("reading=running=7" in r.message for r in caplog.records)
    finally:
        proxy._llm_inflight.clear()
        proxy._llm_inflight_started.clear()


@pytest.mark.asyncio
async def test_quiesce_check_no_signal_when_endpoints_absent(caplog):
    """When neither /slots nor /metrics answers 200, logs no_signal."""
    backend_url = "http://127.0.0.1:8000"
    proxy._llm_inflight[backend_url] = 1
    proxy._llm_inflight_started[backend_url] = [time.monotonic()]
    proxy.LLM_BACKEND_MODELS[backend_url] = "other-model"

    resp_404 = AsyncMock()
    resp_404.status = 404
    resp_404.__aenter__.return_value = resp_404

    try:
        with patch.object(proxy, "_self_hosted", return_value=True):
            with patch("aiohttp.ClientSession.get", return_value=resp_404):
                with caplog.at_level("INFO", logger="hive-proxy"):
                    await proxy._quiesce_backend_check()

                assert any("signal=no_signal" in r.message for r in caplog.records)
    finally:
        proxy._llm_inflight.clear()
        proxy._llm_inflight_started.clear()


@pytest.mark.asyncio
async def test_quiesce_check_idle_backup_logs_no_request_in_flight(caplog):
    """With nothing in flight, logs no_request_in_flight."""
    proxy._llm_inflight.clear()
    with caplog.at_level("INFO", logger="hive-proxy"):
        await proxy._quiesce_backend_check()
    assert any("quiesce-backend-check: no_request_in_flight" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_quiesce_check_cancelled_at_end_quiesce_logs_no_answer(caplog):
    """When quiesce ends while probe is waiting, logs 'no answer before quiesce ended'."""
    backend_url = "http://127.0.0.1:8000"
    proxy._llm_inflight[backend_url] = 1
    proxy._llm_inflight_started[backend_url] = [time.monotonic()]
    proxy.LLM_BACKEND_MODELS[backend_url] = "slow-server"

    class _HangingCtx:
        async def __aenter__(self):
            await asyncio.sleep(100.0)
        async def __aexit__(self, *args):
            return False

    try:
        with patch.object(proxy, "_self_hosted", return_value=True):
            with patch("aiohttp.ClientSession.get", side_effect=lambda *a, **kw: _HangingCtx()):
                with caplog.at_level("INFO", logger="hive-proxy"):
                    task = asyncio.create_task(proxy._quiesce_backend_check())
                    await asyncio.sleep(0.02)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task

                assert any("no answer before quiesce ended" in r.message for r in caplog.records)
    finally:
        proxy._llm_inflight.clear()
        proxy._llm_inflight_started.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Telemetry Contract & Pool Descriptor Rows
# ══════════════════════════════════════════════════════════════════════════════

def test_telemetry_contract_defines_max_wait_expired_stamped():
    """max_wait_expired and max_wait_expired_last_ts are in telemetry contract at 1.0.45."""
    assert "llm.pool.*.max_wait_expired" in tc.TELEMETRY
    assert "llm.pool.*.max_wait_expired_last_ts" in tc.TELEMETRY
    assert tc.TELEMETRY["llm.pool.*.max_wait_expired"]["since"] == "1.0.45"
    assert tc.TELEMETRY["llm.pool.*.max_wait_expired_last_ts"]["since"] == "1.0.45"


def test_llm_runtime_snapshot_includes_max_wait_expired_for_telemetry():
    """_llm_runtime_snapshot(for_telemetry=True) emits max_wait_expired."""
    backend = "http://127.0.0.1:8000"
    proxy.LLM_BACKENDS = [backend]
    proxy._llm_max_wait_expired[backend] = 3
    ts = datetime.now(timezone.utc).isoformat()
    proxy._llm_max_wait_expired_last_ts[backend] = ts

    snap = proxy._llm_runtime_snapshot(for_telemetry=True)
    pool_entry = snap["pool"][backend]
    assert pool_entry["max_wait_expired"] == 3
    assert pool_entry["max_wait_expired_last_ts"] == ts


def test_pool_descriptor_rows_excludes_max_wait_s():
    """_pool_descriptor_rows excludes max_wait_s so tuning it never alters pool hash."""
    backend = "http://127.0.0.1:8000"
    proxy.LLM_POOL = [backend]
    proxy.LLM_BACKEND_MAX_WAIT_S[backend] = 30.0
    proxy.LLM_BACKEND_MODELS[backend] = "m"
    proxy.LLM_BACKEND_NCTX[backend] = 8192
    proxy.LLM_BACKEND_MAX_OUTPUT[backend] = 2048
    proxy.LLM_BACKEND_EXTRAS[backend] = None

    rows = proxy._pool_descriptor_rows()
    assert len(rows) == 1
    assert "max_wait_s" not in rows[0]
