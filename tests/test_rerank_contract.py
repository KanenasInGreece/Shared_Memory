"""
Tests for the rerank stage of handle_search — the contract between the
coordinator and whatever reranking server is configured.

Why this file exists: the reranker is a SEPARATE PROCESS, so nothing about it
is proven by the rest of the suite. Every existing rerank stub in this suite
returns exactly as many entries as the test expects, which models a
well-behaved server rather than a real one — so two defects lived here
undetected, and each one hid the other.

The invariants:

  R1  The caller's `limit` is enforced by the COORDINATOR, never delegated to
      the reranking server. A server that returns more entries than asked for
      (because it ignores the truncation parameter, or ignores it on some
      versions) must not inflate the result set.

  R2  A rerank FAILURE is distinguishable from a rerank SUCCESS in the
      response. The fallback used to emit relevance_score 1.0 for every
      candidate — a value a working reranker can legitimately produce — so a
      totally dead reranker was indistinguishable from a confident one.
      FAILURE != a plausible score (the Group 3 rule: a behaviour with no
      metric is unfalsifiable).

  R3  The rerank timeout is DERIVED from the size of the payload against a
      throughput floor, not a constant. Constant timeouts under-provision
      exactly the large requests that need them most (the rule established for
      the embedder and the LLM backend, which the reranker was never covered by).
"""

import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Dynamic import (mirrors test_coordinator.py / test_read_contract.py) ─────

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
    """n Tier-1 candidate rows, exactly as the vector search yields them."""
    return [
        {"id": 100 + i, "content": f"candidate {i}",
         "metadata": {"entities": [], "source": "claude"}}
        for i in range(n)
    ]


async def _search(c, mock_post, limit: int, query="anything"):
    """Run handle_search with the rerank POST patched, and return the payload."""
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = mock_post
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            resp = await c.handle_search(_make_request({"query": query, "limit": limit}))
    assert resp.status == 200
    return json.loads(resp.text)


def _reranker_returning(n: int) -> AsyncMock:
    """A reranking server that scores and returns ALL n candidates handed to it,
    ignoring any truncation parameter — the observed behaviour of llama.cpp's
    /v1/reranking when the parameter it honours is not the one that was sent."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "results": [{"index": i, "relevance_score": float(n - i)} for i in range(n)]
    })
    return AsyncMock(return_value=resp)


# ── R1 — the coordinator enforces the caller's limit ─────────────────────────

@pytest.mark.asyncio
async def test_result_count_never_exceeds_limit_when_reranker_over_returns():
    """A reranking server that returns every candidate it was given must not be
    able to inflate the result set past the caller's limit.

    This is the live shape: the coordinator fetches a fixed 20 Tier-1
    candidates and asks the server to truncate. When the server does not
    truncate, EVERY search returns 20 rows instead of the requested limit.
    """
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    payload = await _search(c, _reranker_returning(20), limit=5)
    fact_rows = [r for r in payload["results"] if r["tier"] == "fact"]

    assert len(fact_rows) <= 5, (
        f"the caller asked for 5 and got {len(fact_rows)} — the limit is being "
        "delegated to the reranking server instead of enforced here"
    )


@pytest.mark.asyncio
async def test_limit_enforced_keeps_the_reranker_ordering():
    """Truncation must keep the TOP entries by the reranker's own ordering —
    the server returns them ranked, so the cut is a prefix, not a sample."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    payload = await _search(c, _reranker_returning(20), limit=3)
    fact_rows = [r for r in payload["results"] if r["tier"] == "fact"]

    assert [r["pg_id"] for r in fact_rows] == [100, 101, 102]


# ── R2 — a dead reranker must not look like a working one ────────────────────

@pytest.mark.asyncio
async def test_rerank_failure_is_visible_in_the_response():
    """When the rerank call fails the results are in VECTOR order, not relevance
    order. That must be stated, not disguised as a uniform score of 1.0 —
    which is a value a working reranker can legitimately emit, making a
    permanently dead reranker indistinguishable from a confident one."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    failing = AsyncMock(side_effect=RuntimeError("connection refused"))
    payload = await _search(c, failing, limit=5)
    fact_rows = [r for r in payload["results"] if r["tier"] == "fact"]

    assert len(fact_rows) == 5
    assert all(r.get("ranked") is False for r in fact_rows), (
        "a fallback result must declare that it was NOT reranked"
    )
    assert all(r.get("score") is None for r in fact_rows), (
        "a fallback result must not carry a fabricated relevance score"
    )


@pytest.mark.asyncio
async def test_successful_rerank_is_marked_ranked():
    """The positive half of the same invariant — mutation-check anchor: a
    blanket `ranked: False` would satisfy the test above and die here."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(20))

    payload = await _search(c, _reranker_returning(20), limit=5)
    fact_rows = [r for r in payload["results"] if r["tier"] == "fact"]

    assert all(r.get("ranked") is True for r in fact_rows)
    assert all(isinstance(r.get("score"), float) for r in fact_rows)


# ── R3 — the timeout is derived from the payload, not constant ───────────────

def test_rerank_timeout_scales_with_the_payload():
    """A constant timeout under-provisions exactly the large requests that need
    it most. The reranker scores each (query, document) pair, so its cost grows
    with the total size of the candidate set — the timeout must follow it.

    Named and shaped to match embed_ceiling: the embedder is the sibling CPU
    backend that was already brought under this rule, and the two must not
    drift apart."""
    from dream_telemetry import rerank_ceiling

    small = rerank_ceiling(["short doc"] * 4)
    large = rerank_ceiling(["x" * 12000] * 20)

    assert large > small, "the timeout must grow with the payload"
    assert small >= 5.0, "a small payload still needs a sane floor"
    assert large >= 60.0, (
        "a full 20-candidate set measured ~64s at the reference deployment's "
        "thread count — a ceiling below that guarantees a timeout"
    )


def test_rerank_payload_is_bounded_so_the_ceiling_is_finite():
    """The ceiling is only a known quantity because the payload is clamped —
    the same relationship EMBED_MAX_CHARS has with embed_ceiling. Without the
    clamp a single huge record makes the timeout unbounded."""
    import dream_telemetry

    cap = dream_telemetry.RERANK_MAX_DOC_CHARS
    assert len(dream_telemetry.clamp_rerank_doc("x" * 99999)) == cap
    # Doubling the size of already-oversized documents must not move the ceiling.
    a = dream_telemetry.rerank_ceiling(["x" * 50000] * 20)
    b = dream_telemetry.rerank_ceiling(["x" * 100000] * 20)
    assert a == b, "the ceiling must be bounded by the clamp, not by the input"


def test_rerank_throughput_floor_is_env_overridable():
    """Portability: the throughput floor is the REFERENCE deployment's measured
    number, so it is an env-overridable default and never a literal baked into
    a code path. A GPU-backed reranker raises it; a slower box lowers it."""
    import dream_telemetry

    baseline = dream_telemetry.rerank_ceiling(["x" * 4000] * 20)
    with patch.dict(os.environ, {"RERANK_MIN_CHARS_S": "50"}):
        importlib.reload(dream_telemetry)
        slower = dream_telemetry.rerank_ceiling(["x" * 4000] * 20)
    importlib.reload(dream_telemetry)   # restore for later tests

    assert slower > baseline, (
        "a lower throughput floor must produce a longer timeout"
    )
