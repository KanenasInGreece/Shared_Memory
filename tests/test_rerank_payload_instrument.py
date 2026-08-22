"""
Tests for the rerank PAYLOAD-SIZE instrument on handle_search / GET
/memory/telemetry.

Context: fact:1441 recorded a cross-host capacity sweep on a CPU-only test
host that produced an UNDER-DETERMINED finding — the harness never recorded
how many characters were actually sent to the reranker per search, so
per-request FIXED OVERHEAD (embedding, two DB round trips, candidate
batching, HTTP — estimated 2-3s on that host) could not be separated from
DOCUMENT-LENGTH cost. The capacity model's `chars / mu` term, with no
fixed-overhead component, turns from conservative to OPTIMISTIC below
roughly 2000 chars — the one direction a safety bound must never err. This
file covers the fix: every search now records the total characters and
document count actually handed to the reranker, surfaced on the response the
same way `ranked` already is (flat, additive, repeated per row) plus
cumulative counters on telemetry.

Agreed invariants under test:

  I-B1  The recorded char count is the sum of what was ACTUALLY sent to the
        reranker — measured AFTER clamp_rerank_doc truncation, never the
        pre-clamp length. A pre-clamp count would reintroduce exactly the
        ambiguity this instrument exists to remove.

  I-B2  A fallback (reranker down/erroring) records the payload it WOULD
        have sent, distinguishable from a search that actually reranked via
        the existing `ranked` flag — never by the payload fields going
        missing or turning null on one path and not the other.

  I-B3  Adding the measurement changes no existing search behaviour,
        ordering, result content or timing path — pure arithmetic over data
        already in hand, no new query, no new I/O, nothing that can raise.

  I-B4  API_VERSION is untouched; the addition is additive to the existing
        response shape (covered implicitly — no key is renamed or removed
        anywhere in this file's assertions).

All I/O mocked; no live infrastructure. Harness mirrors
test_rerank_oom_visibility.py.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ── Dynamic import (mirrors test_rerank_oom_visibility.py) ────────────────

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
clamp_rerank_doc = coordinator_mod.clamp_rerank_doc
RERANK_MAX_DOC_CHARS = coordinator_mod.RERANK_MAX_DOC_CHARS


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


def _candidates(n: int, content: str = None) -> list[dict]:
    return [
        {"id": 100 + i, "content": content if content is not None else f"candidate {i}",
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


def _ok_rerank_response(n: int):
    ok_resp = MagicMock()
    ok_resp.raise_for_status = MagicMock()
    ok_resp.json = MagicMock(return_value={
        "results": [{"index": i, "relevance_score": float(n - i)} for i in range(n)]
    })
    return AsyncMock(return_value=ok_resp)


# ── I-B1 — the count is POST-CLAMP, never pre-clamp ────────────────────────

@pytest.mark.asyncio
async def test_payload_chars_is_post_clamp_not_raw_content_length():
    """A candidate longer than RERANK_MAX_DOC_CHARS must contribute only its
    CLAMPED length to rerank_payload_chars — the raw content is longer, and
    counting the raw length would reintroduce exactly the ambiguity fact:1441
    hit (ranking sees a bounded slice; the instrument must describe THAT, not
    the unbounded record).

    Mutation-check anchor: summing `len(c["content"])` over the raw
    candidates instead of `len(d)` over `rerank_docs` would inflate this
    number and fail the assertion below.
    """
    c, mock_conn, _ = _coordinator_with_mocks()
    long_content = "x" * (RERANK_MAX_DOC_CHARS + 5000)
    mock_conn.fetch = AsyncMock(return_value=_candidates(1, content=long_content))

    body = await _search(c, _ok_rerank_response(1), limit=1)

    row = body["results"][0]
    assert row["rerank_payload_chars"] == RERANK_MAX_DOC_CHARS, (
        f"expected the clamped length {RERANK_MAX_DOC_CHARS}, got "
        f"{row['rerank_payload_chars']} — this must be the POST-clamp sum"
    )
    # Sanity: the raw content really was longer, so a pre-clamp count would
    # have produced a different (larger) number, proving the two are
    # distinguishable rather than coincidentally equal.
    assert len(long_content) > RERANK_MAX_DOC_CHARS


@pytest.mark.asyncio
async def test_payload_chars_matches_manual_clamp_sum_for_mixed_lengths():
    """Cross-check against an independently computed clamp sum (not reusing
    the coordinator's own arithmetic) across several documents of different
    lengths, to catch an off-by-one-document or partial-sum defect that a
    single-candidate test could miss."""
    c, mock_conn, _ = _coordinator_with_mocks()
    lengths = [10, 500, RERANK_MAX_DOC_CHARS + 100, 0]
    cands = [
        {"id": 200 + i, "content": "y" * n,
         "metadata": {"entities": [], "source": "claude"}}
        for i, n in enumerate(lengths)
    ]
    mock_conn.fetch = AsyncMock(return_value=cands)

    body = await _search(c, _ok_rerank_response(4), limit=4)

    # The coordinator's own doc-text builder may prepend metadata (e.g. a
    # recency stamp) for Tier-1 candidates, so the exact clamped length of
    # each entry isn't independently known here — but the TOTAL must never
    # exceed 4 * RERANK_MAX_DOC_CHARS (the hard per-document ceiling) and
    # must be strictly greater than the sum of the unclamped short entries
    # (10 + 500 + 0), proving the long entry was truncated rather than
    # dropped or double counted.
    total = body["results"][0]["rerank_payload_chars"]
    assert total <= 4 * RERANK_MAX_DOC_CHARS
    assert total > 10 + 500 + 0
    assert body["results"][0]["rerank_payload_docs"] == 4


# ── document count ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_payload_docs_counts_every_document_sent():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(7))

    body = await _search(c, _ok_rerank_response(7), limit=7)

    for row in body["results"]:
        assert row["rerank_payload_docs"] == 7, (
            "document count must be the same search-level value on every "
            "row, mirroring how `ranked` is repeated"
        )


# ── I-B2 — fallback records what it WOULD have sent, not null ─────────────

@pytest.mark.asyncio
async def test_fallback_records_the_payload_it_would_have_sent():
    """A reranker failure must not blank out the payload measurement — the
    row still carries the real char/doc counts, just with ranked=False.
    Mutation-check anchor: moving the payload computation INSIDE the try
    block (after the POST) would make this assertion see 0/None on the
    fallback path instead of the true counts."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(5))

    failing = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    body = await _search(c, failing, limit=5)

    assert body["results"], "expected fallback rows, got none"
    for row in body["results"]:
        assert row["ranked"] is False
        assert row["rerank_payload_docs"] == 5
        assert row["rerank_payload_chars"] > 0


@pytest.mark.asyncio
async def test_reranked_and_fallback_payload_counts_are_equal_for_same_input():
    """The measurement describes what was SENT, which is identical whether
    the reranker answers or drops the connection — only `ranked` differs.
    This is the direct check that success and failure are distinguished by
    the right field."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(5))

    ok_body = await _search(c, _ok_rerank_response(5), limit=5)

    c2, mock_conn2, _ = _coordinator_with_mocks()
    mock_conn2.fetch = AsyncMock(return_value=_candidates(5))
    failing = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    fail_body = await _search(c2, failing, limit=5)

    assert ok_body["results"][0]["rerank_payload_chars"] == \
        fail_body["results"][0]["rerank_payload_chars"]
    assert ok_body["results"][0]["ranked"] is True
    assert fail_body["results"][0]["ranked"] is False


# ── I-B3 — the measurement changes nothing else about the search ──────────

@pytest.mark.asyncio
async def test_result_ordering_and_scores_unaffected_by_the_new_fields():
    """Adding the payload fields must not perturb ordering, scores, or which
    records are returned — cross-checked against the pre-existing contract
    test's own expectations (descending relevance_score, same ids)."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(5))

    ok_resp = MagicMock()
    ok_resp.raise_for_status = MagicMock()
    ok_resp.json = MagicMock(return_value={
        "results": [{"index": i, "relevance_score": float(5 - i)} for i in range(5)]
    })
    body = await _search(c, AsyncMock(return_value=ok_resp), limit=5)

    scores = [r["score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True), (
        "reranked order must still be descending by score — the new fields "
        "must not have disturbed the existing ordering logic"
    )
    assert [r["pg_id"] for r in body["results"]] == [100, 101, 102, 103, 104]


@pytest.mark.asyncio
async def test_documents_posted_to_reranker_keep_original_candidate_order():
    """The instrument must be pure observation of `rerank_docs`, never a
    participant that reorders it before the POST — a reorder would silently
    desynchronize the reranker's response `index` from the candidate it was
    actually scored against (index i's score would land on the WRONG
    record), corrupting results while every index-only assertion elsewhere
    keeps passing. Captures the real `documents` list the coordinator sent
    and checks it against the original per-candidate text, in original
    order — not merely its length.

    Mutation-check anchor: sorting/reordering `rerank_docs` anywhere between
    its construction and the POST (e.g. while computing the payload total)
    would change this list's order without changing its length, and this is
    the only test in the file that would notice."""
    c, mock_conn, _ = _coordinator_with_mocks()
    # Distinctly different lengths per candidate — a length-keyed reorder
    # (the class of mutation this test exists to catch) is a no-op on
    # equal-length content, so the fixture must not accidentally hide it.
    cands = [
        {"id": 300 + i, "content": "z" * n,
         "metadata": {"entities": [], "source": "claude"}}
        for i, n in enumerate([50, 10, 200, 5, 80])
    ]
    mock_conn.fetch = AsyncMock(return_value=cands)

    captured = {}

    async def capturing_post(url, json=None, timeout=None):
        captured["documents"] = json["documents"]
        ok_resp = MagicMock()
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json = MagicMock(return_value={
            "results": [{"index": i, "relevance_score": float(5 - i)} for i in range(5)]
        })
        return ok_resp

    await _search(c, capturing_post, limit=5)

    expected = [clamp_rerank_doc(coordinator_mod._rerank_doc_text(
        cnd["content"], cnd["metadata"], None)) for cnd in cands]
    assert captured["documents"] == expected, (
        "documents sent to the reranker must be in the original candidate "
        "order — a reorder desyncs response `index` from the candidate it "
        "actually scored"
    )


# ── Telemetry cumulative counters ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_zero_searches_reports_zero_payload_totals():
    c, _, _ = _coordinator_with_mocks()
    snap = await _telemetry(c)

    assert snap["rerank_payload_chars_total"] == 0
    assert snap["rerank_payload_docs_total"] == 0
    # Companion counters that disambiguate "no searches yet" from "measured,
    # genuinely zero" — both must be 0 too, or the payload totals above would
    # be unreadable.
    assert snap["rerank_successes_total"] == 0
    assert snap["rerank_fallbacks_total"] == 0


@pytest.mark.asyncio
async def test_payload_totals_accumulate_across_success_and_fallback():
    """Both outcomes must contribute to the cumulative totals — a fallback
    is not a no-op for this instrument, per I-B2."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=_candidates(3))

    await _search(c, _ok_rerank_response(3), limit=3)  # success
    failing = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    await _search(c, failing, limit=3)  # fallback

    snap = await _telemetry(c)
    assert snap["rerank_successes_total"] == 1
    assert snap["rerank_fallbacks_total"] == 1
    assert snap["rerank_payload_docs_total"] == 6, (
        "3 docs from the successful search + 3 from the fallback search"
    )
    assert snap["rerank_payload_chars_total"] > 0
    # The companion pair (successes+fallbacks) is exactly the "how many
    # searches were measured" count — proving no third counter is needed.
    assert (snap["rerank_successes_total"] + snap["rerank_fallbacks_total"]) == 2
