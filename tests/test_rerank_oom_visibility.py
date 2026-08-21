"""
Tests for W2' — degraded-mode/OOM visibility on the rerank fallback path.

Context: a reranker OOM-killed by the kernel on a memory-constrained host
presents to the coordinator as a dropped connection mid-request. The gateway
already falls back honestly (vector order, no fabricated score — see
test_rerank_contract.py), but until this change the outcome counters
(_rerank_successes / _rerank_failures) were incremented and never read, and
the fallback's log line named no probable cause. This is pure visibility —
the mem_limit stays reported-only (decision:1424); nothing here changes what
the coordinator DOES on a rerank failure, only what it reports.

Two things are covered:

  T1  GET /memory/telemetry surfaces rerank_successes_total /
      rerank_fallbacks_total / rerank_fallbacks_last_ts as flat additive
      keys, matching the credentials/llm_faults convention (paired
      last-event timestamp, None until the counter first moves).

  T2  The fallback log line names OOM as a probable cause ONLY when the
      failure is transport-shaped (RemoteProtocolError / ConnectError /
      ReadError — the httpx exceptions a dropped connection actually
      raises), never for a timeout (a slow/busy reranker is not a dead one).

All I/O mocked; no live infrastructure.
"""

import importlib.util
import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ── Dynamic import (mirrors test_rerank_contract.py) ──────────────────────

def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator


class _async_ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


class _AsyncRows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


def _make_request(body: dict) -> MagicMock:
    state = {"authenticated_agent": None, "principal": None}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator_with_mocks():
    c = MemoryCoordinator()

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=_async_ctx(None))

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_async_ctx(mock_conn))
    c._pool = mock_pool

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncRows())
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_async_ctx(mock_session))
    c._neo4j = mock_neo4j

    return c, mock_conn, mock_session


def _candidates(n: int) -> list[dict]:
    return [
        {"id": 100 + i, "content": f"candidate {i}",
         "metadata": {"entities": [], "source": "claude"}}
        for i in range(n)
    ]


async def _search(c, mock_post, limit: int, query="anything"):
    """Run handle_search with the rerank POST patched."""
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = mock_post
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            resp = await c.handle_search(_make_request({"query": query, "limit": limit}))
    assert resp.status == 200
    return json.loads(resp.text)


async def _telemetry(c) -> dict:
    resp = await c.handle_telemetry(_make_request({}))
    assert resp.status == 200
    return json.loads(resp.text)["telemetry"]


# ── T1 — telemetry surfaces the counters as flat additive keys ────────────

@pytest.mark.asyncio
async def test_zero_fallbacks_reports_zero_and_null_ts():
    c, _, _ = _coordinator_with_mocks()
    snap = await _telemetry(c)

    assert snap["rerank_fallbacks_total"] == 0
    assert snap["rerank_fallbacks_last_ts"] is None
    assert snap["rerank_successes_total"] == 0


@pytest.mark.asyncio
async def test_n_fallbacks_are_counted_and_timestamped():
    """After N simulated rerank failures the telemetry payload carries the
    VALUE N, not merely a non-zero count — and a non-null last_ts."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    failing = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    N = 3
    for _ in range(N):
        await _search(c, failing, limit=5)

    snap = await _telemetry(c)
    assert snap["rerank_fallbacks_total"] == N, (
        f"expected exactly {N} counted fallbacks, got "
        f"{snap['rerank_fallbacks_total']}"
    )
    assert snap["rerank_fallbacks_last_ts"] is not None
    assert snap["rerank_successes_total"] == 0


@pytest.mark.asyncio
async def test_successes_and_fallbacks_are_counted_independently():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    ok_resp = MagicMock()
    ok_resp.raise_for_status = MagicMock()
    ok_resp.json = MagicMock(return_value={
        "results": [{"index": i, "relevance_score": float(20 - i)} for i in range(20)]
    })
    succeeding = AsyncMock(return_value=ok_resp)

    await _search(c, succeeding, limit=5)
    await _search(c, succeeding, limit=5)

    snap = await _telemetry(c)
    assert snap["rerank_successes_total"] == 2
    assert snap["rerank_fallbacks_total"] == 0
    assert snap["rerank_fallbacks_last_ts"] is None, (
        "a success must never move the fallback timestamp"
    )


# ── T2 — the log line names OOM only for transport-drop failures ──────────

@pytest.mark.asyncio
async def test_dropped_connection_names_oom_as_probable_cause(caplog):
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    failing = AsyncMock(side_effect=httpx.RemoteProtocolError("peer closed connection"))
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await _search(c, failing, limit=5)

    assert any("OOM-killing the reranker" in r.message for r in caplog.records), (
        "a dropped-connection-shaped failure must name OOM as a probable cause"
    )


@pytest.mark.asyncio
async def test_connect_error_also_names_oom(caplog):
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    failing = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await _search(c, failing, limit=5)

    assert any("OOM-killing the reranker" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_read_error_also_names_oom(caplog):
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    failing = AsyncMock(side_effect=httpx.ReadError("connection reset"))
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await _search(c, failing, limit=5)

    assert any("OOM-killing the reranker" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_timeout_does_not_name_oom(caplog):
    """A timeout is not a dropped-connection signature — the reranker may just
    be slow or busy, not gone. Mutation-check anchor for the discrimination:
    a blanket `except Exception` appending the OOM sentence unconditionally
    would satisfy every OOM-shaped test above and die here."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    failing = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await _search(c, failing, limit=5)

    assert any("rerank failed" in r.message for r in caplog.records), (
        "the fallback must still be logged for a timeout"
    )
    assert not any("OOM-killing the reranker" in r.message for r in caplog.records), (
        "a timeout is not a dropped-connection signature and must not be "
        "misattributed to OOM"
    )


@pytest.mark.asyncio
async def test_generic_failure_does_not_name_oom(caplog):
    """A non-httpx, non-transport exception (e.g. a malformed JSON body) must
    not be misattributed to OOM either — only the specific transport-drop
    family qualifies."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    failing = AsyncMock(side_effect=RuntimeError("unexpected"))
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await _search(c, failing, limit=5)

    assert not any("OOM-killing the reranker" in r.message for r in caplog.records)
