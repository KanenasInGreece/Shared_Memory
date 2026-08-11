"""
Tests for search axis filters (v0.8.74) — `project`/`domains`/`since` as
FILTERS on the search candidate set, never as query text.

WHY THIS EXISTS. The write/fold side of the framework keys everything on
operator-asserted axes (project, domain(s), entities — canonical top-level
metadata keys since v0.8.73/decision:1214), but the read side collected none
of it: `search` accepted only query text + limit. Measured motivating
failure: a human ask naming WHERE + WHEN + WHAT could only be searched by
WHAT — its weakest signal — landing the right facts at ranks 4/7/8 below the
`limit` cut, while adding the project name as query TEXT ranked records that
MENTION the project above records that BELONG to it. A named place/time is a
FILTER, not query text.

Coverage:
  - `_axis_filter_predicate` (pure) — SQL fragment + positional params for
    project / domains (OR semantics) / since, individually and combined
  - the predicate reaches EVERY candidate query `handle_search` runs: the
    Tier-1 vector query (+ its pre-migration fallback), both Tier-3 queries
    (insight + thematic, + the pre-006 fallback), and the keyword-ILIKE
    fallback used when the embedder is down
  - filters restrict the candidate set the RERANKER sees — no widening after
    the DB call
  - an empty filtered match returns the honest {results: []} shape; the
    reranker is never invoked and there is no unfiltered retry
  - unfiltered search is unchanged: no extra SQL text, no extra bound params
  - shape validation only — an unregistered project/domain name is not
    refused, it simply matches nothing
  - CLI flag passthrough (`--project`, `--domain` repeatable, `--since`),
    both tracked memory_bridge.py copies byte-identical, MCP parity
"""

import datetime
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


# ── Dynamic import (mirrors test_read_contract.py / test_coordinator.py) ─────

def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(_ROOT, "shared-memory", "scripts")
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
_axis_filter_predicate = coordinator_mod._axis_filter_predicate


def load_memory_bridge():
    path = os.path.join(_ROOT, "shared-memory", "scripts", "memory_bridge.py")
    spec = importlib.util.spec_from_file_location("memory_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge"] = mod
    spec.loader.exec_module(mod)
    return mod


def load_vector_skill():
    path = os.path.join(_ROOT, "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vector_skill"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Helpers (test_read_contract.py conventions) ───────────────────────────────

class _async_ctx:
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


def _make_request(body: dict, authenticated_agent=None) -> MagicMock:
    state = {"authenticated_agent": authenticated_agent, "principal": None}
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

    class _AsyncRows:
        def __init__(self, rows=()):
            self._rows = list(rows)
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not self._rows:
                raise StopAsyncIteration
            return self._rows.pop(0)

    mock_session.run = AsyncMock(return_value=_AsyncRows())
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_async_ctx(mock_session))
    c._neo4j = mock_neo4j

    return c, mock_conn, mock_session


def _reranker_mock(n_docs, score=2.0):
    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": i, "relevance_score": score - i}
                    for i in range(n_docs)]
    })
    return mock_reranker


async def _run_search(c, mock_reranker, body, capture=None):
    """Patch _embed + the reranker for one handle_search call (test_read_contract.py
    pattern). Optionally captures the mocked httpx client into `capture['http']` so
    a caller can inspect what was posted to the reranker."""
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            resp = await c.handle_search(_make_request(body))
    if capture is not None:
        capture["http"] = mock_http
    assert resp.status == 200, resp.text
    return json.loads(resp.text)


def _fetch_calls_matching(mock_conn, needle):
    return [call for call in mock_conn.fetch.call_args_list if needle in call.args[0]]


def _fetchrow_calls_matching(mock_conn, needle):
    return [call for call in mock_conn.fetchrow.call_args_list if needle in call.args[0]]


# ══════════════════════════════════════════════════════════════════════════
# (a1) `_axis_filter_predicate` — pure, mutation-friendly unit tests
# ══════════════════════════════════════════════════════════════════════════

def test_no_filters_returns_empty_sql_and_params():
    sql, params = _axis_filter_predicate(5, None, None, None)
    assert sql == ""
    assert params == []


def test_project_only_predicate():
    sql, params = _axis_filter_predicate(5, "alpha-project", None, None)
    assert sql == " AND metadata->>'project' = $5"
    assert params == ["alpha-project"]


def test_domains_only_predicate_or_semantics():
    sql, params = _axis_filter_predicate(3, None, ["ops", "security"], None)
    assert sql == " AND metadata->'domains' ?| $3::text[]"
    assert params == [["ops", "security"]]


def test_single_domain_predicate():
    sql, params = _axis_filter_predicate(1, None, ["ops"], None)
    assert sql == " AND metadata->'domains' ?| $1::text[]"
    assert params == [["ops"]]


def test_since_only_predicate():
    dt = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    sql, params = _axis_filter_predicate(2, None, None, dt)
    assert sql == " AND created_at >= $2::timestamptz"
    assert params == [dt]


def test_combined_predicate_sequential_placeholders():
    dt = datetime.datetime(2026, 8, 1)
    sql, params = _axis_filter_predicate(10, "alpha", ["ops"], dt)
    assert sql == (
        " AND metadata->>'project' = $10"
        " AND metadata->'domains' ?| $11::text[]"
        " AND created_at >= $12::timestamptz"
    )
    assert params == ["alpha", ["ops"], dt]


def test_falsy_project_and_empty_domains_are_no_filter():
    """"" and [] must behave exactly like None — a caller that sends an empty
    string/list (rather than omitting the key) gets no predicate either."""
    sql, params = _axis_filter_predicate(1, "", [], None)
    assert sql == ""
    assert params == []


# ══════════════════════════════════════════════════════════════════════════
# (a2) the predicate reaches EVERY candidate query handle_search runs
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_project_filter_reaches_tier1_query():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact about alpha",
         "metadata": {"project": "alpha"}, "created_at": None},
    ])
    reranker = _reranker_mock(1)
    await _run_search(c, reranker, {"query": "status", "limit": 5, "project": "alpha"})

    calls = _fetch_calls_matching(mock_conn, "FROM technical_docs")
    assert calls, "Tier-1 candidate query never ran"
    for call in calls:
        assert "metadata->>'project' = $" in call.args[0]
        assert "alpha" in call.args


@pytest.mark.asyncio
async def test_project_filter_reaches_both_tier3_queries():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    reranker = _reranker_mock(0)
    await _run_search(c, reranker, {"query": "status", "limit": 5, "project": "alpha"})

    for needle in ("kind' = 'insight'", "<> 'insight'"):
        rows = _fetchrow_calls_matching(mock_conn, needle)
        assert rows, f"Tier-3 query for {needle!r} never ran"
        for call in rows:
            assert "metadata->>'project' = $" in call.args[0]
            assert "alpha" in call.args


@pytest.mark.asyncio
async def test_domains_filter_or_semantics_reaches_tier1_and_tier3():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    reranker = _reranker_mock(0)
    await _run_search(c, reranker,
                       {"query": "status", "limit": 5, "domains": ["ops", "security"]})

    tier1 = _fetch_calls_matching(mock_conn, "FROM technical_docs")
    assert tier1
    for call in tier1:
        assert "metadata->'domains' ?| $" in call.args[0]
        assert ["ops", "security"] in call.args

    for needle in ("kind' = 'insight'", "<> 'insight'"):
        rows = _fetchrow_calls_matching(mock_conn, needle)
        assert rows
        for call in rows:
            assert "metadata->'domains' ?| $" in call.args[0]
            assert ["ops", "security"] in call.args


@pytest.mark.asyncio
async def test_since_filter_parses_and_reaches_tier1_and_tier3():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    reranker = _reranker_mock(0)
    await _run_search(c, reranker,
                       {"query": "status", "limit": 5, "since": "2026-08-01"})

    tier1 = _fetch_calls_matching(mock_conn, "FROM technical_docs")
    assert tier1
    for call in tier1:
        assert "created_at >= $" in call.args[0]
        assert any(isinstance(a, datetime.datetime) for a in call.args)

    for needle in ("kind' = 'insight'", "<> 'insight'"):
        rows = _fetchrow_calls_matching(mock_conn, needle)
        assert rows
        for call in rows:
            assert "created_at >= $" in call.args[0]


@pytest.mark.asyncio
async def test_since_accepts_full_iso_datetime_and_z_suffix():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    reranker = _reranker_mock(0)
    result = await _run_search(
        c, reranker,
        {"query": "status", "limit": 5, "since": "2026-08-01T12:30:00Z"})
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_combined_filters_all_three_present_together():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "x",
         "metadata": {"project": "alpha", "domains": ["ops"]},
         "created_at": None},
    ])
    reranker = _reranker_mock(1)
    await _run_search(c, reranker, {
        "query": "status", "limit": 5,
        "project": "alpha", "domains": ["ops", "security"], "since": "2026-08-01",
    })

    call = _fetch_calls_matching(mock_conn, "FROM technical_docs")[0]
    sql = call.args[0]
    assert "metadata->>'project' = $" in sql
    assert "metadata->'domains' ?| $" in sql
    assert "created_at >= $" in sql
    bound = call.args[1:]
    assert "alpha" in bound
    assert ["ops", "security"] in bound
    assert any(isinstance(b, datetime.datetime) for b in bound)


@pytest.mark.asyncio
async def test_pre006_tier3_fallback_variant_also_carries_the_predicate():
    """The pre-migration-006 fallback (no `kind` column) is a THIRD, separate
    query text — prove the predicate reaches it too, not just the two primary
    variants."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[None, Exception("column \"kind\" does not exist"), None])
    mock_conn.fetch = AsyncMock(return_value=[])
    reranker = _reranker_mock(0)
    await _run_search(c, reranker, {"query": "status", "limit": 5, "project": "alpha"})

    fallback_call = mock_conn.fetchrow.call_args_list[-1]
    sql = fallback_call.args[0]
    assert "kind" not in sql
    assert "metadata->>'project' = $" in sql
    assert "alpha" in fallback_call.args


@pytest.mark.asyncio
async def test_keyword_fallback_path_also_applies_the_filter():
    """When the embedder is unavailable, handle_search degrades to a keyword
    ILIKE search — a filtered search must not silently drop its filter just
    because the embedder happens to be down."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    with patch.object(c, "_embed",
                      new=AsyncMock(side_effect=RuntimeError("embedder down"))):
        resp = await c.handle_search(_make_request(
            {"query": "status", "limit": 5, "project": "alpha"}))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["fallback"] == "keyword"
    call = mock_conn.fetch.call_args
    assert "metadata->>'project' = $" in call.args[0]
    assert "alpha" in call.args


# ══════════════════════════════════════════════════════════════════════════
# (b) filters restrict the CANDIDATE SET the reranker sees — no widening
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_filtered_candidates_flow_straight_to_reranker_no_widening():
    """The SQL-text assertions above prove the predicate reaches the query
    (mutation check: delete `_axis_filter_predicate`'s call from handle_search
    and those tests fail because the clause vanishes from the SQL text). This
    test proves the OTHER half of "before reranking": whatever conn.fetch
    returns — standing in for what a real, filtered Postgres query would hand
    back — goes straight into the reranker payload, with no widen-and-refilter
    step and no second Tier-1 fetch."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "belongs to alpha",
         "metadata": {"project": "alpha"}, "created_at": None},
        {"id": 2, "content": "also alpha",
         "metadata": {"project": "alpha"}, "created_at": None},
    ])
    reranker = _reranker_mock(2)
    capture = {}
    await _run_search(c, reranker,
                       {"query": "status", "limit": 5, "project": "alpha"},
                       capture=capture)

    # Exactly one Tier-1 fetch call — no retry / widen-and-refilter step.
    assert len(_fetch_calls_matching(mock_conn, "FROM technical_docs")) == 1

    post_call = capture["http"].post.await_args
    assert post_call is not None
    sent_docs = post_call.kwargs["json"]["documents"]
    # n_t3 (0, Tier-3 fetchrow defaults to None) + the 2 filtered candidates —
    # never the wider SEARCH_CANDIDATE_FLOOR pool.
    assert len(sent_docs) == 2


# ══════════════════════════════════════════════════════════════════════════
# (c) empty filtered match — honest empty shape, no unfiltered fallback
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_empty_filtered_match_returns_honest_empty_shape():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])  # DB: nothing matches the filter
    reranker = _reranker_mock(0)
    capture = {}
    result = await _run_search(
        c, reranker,
        {"query": "status", "limit": 5, "project": "nonexistent-project"},
        capture=capture)

    assert result == {"status": "success", "results": []}
    # No widen-and-retry: exactly one Tier-1 fetch call.
    assert len(_fetch_calls_matching(mock_conn, "FROM technical_docs")) == 1
    # Mutation check: an unfiltered fallback added after an empty candidate
    # set would call the reranker (or re-fetch) — it must not.
    capture["http"].post.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_project_and_domain_names_are_not_refused():
    """Read path never blocks on registry state — a searcher may probe. An
    unregistered name is not a 400, it simply matches nothing."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])
    reranker = _reranker_mock(0)
    result = await _run_search(c, reranker, {
        "query": "status", "limit": 5,
        "project": "totally-made-up-project",
        "domains": ["not-a-real-section"],
    })
    assert result["status"] == "success"
    assert result["results"] == []


# ══════════════════════════════════════════════════════════════════════════
# (e) unfiltered search is unchanged — regression pin
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unfiltered_search_appends_no_extra_sql_or_params():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "anything", "metadata": {}, "created_at": None},
    ])
    reranker = _reranker_mock(1)
    await _run_search(c, reranker, {"query": "status", "limit": 5})

    call = _fetch_calls_matching(mock_conn, "FROM technical_docs")[0]
    sql = call.args[0]
    assert "metadata->>'project'" not in sql
    assert "metadata->'domains'" not in sql
    assert "created_at >=" not in sql
    # No viewer (anonymous) → visibility_filter contributes zero params, so
    # the bound args are exactly [q_vec, pool] — nothing appended for the
    # (unrequested) axis filter.
    assert len(call.args) - 1 == 2  # args[0] is the SQL text itself


# ══════════════════════════════════════════════════════════════════════════
# shape validation — 400s, never a silent no-op
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_project_wrong_type_rejected_400():
    c, _, _s = _coordinator_with_mocks()
    resp = await c.handle_search(_make_request({"query": "x", "project": 123}))
    assert resp.status == 400
    assert "project" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_domains_wrong_type_rejected_400():
    c, _, _s = _coordinator_with_mocks()
    resp = await c.handle_search(_make_request({"query": "x", "domains": "ops"}))
    assert resp.status == 400
    assert "domains" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_domains_list_with_non_string_element_rejected_400():
    c, _, _s = _coordinator_with_mocks()
    resp = await c.handle_search(_make_request({"query": "x", "domains": ["ops", 5]}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_domains_at_cap_passes():
    """Exactly `SEARCH_DOMAINS_FILTER_CAP` (16) entries is allowed — the cap
    rejects OVER the limit, it does not shrink the limit itself."""
    c, mock_conn, _ = _coordinator_with_mocks()
    domains = [f"section-{i}" for i in range(coordinator_mod.SEARCH_DOMAINS_FILTER_CAP)]
    mock_conn.fetch = AsyncMock(return_value=[])
    reranker = _reranker_mock(0)
    result = await _run_search(c, reranker,
                               {"query": "status", "limit": 5, "domains": domains})
    assert result == {"status": "success", "results": []}
    call = _fetch_calls_matching(mock_conn, "FROM technical_docs")[0]
    assert "metadata->'domains' ?| $" in call.args[0]


@pytest.mark.asyncio
async def test_domains_over_cap_rejected_400_filters_invalid():
    """Security (PR 235): `domains` binds straight into the jsonb `?|` scan —
    an authenticated caller sending an unbounded list is a DoS vector. Over
    the cap is a clean 400 `filters_invalid`, never a silent truncation (a
    silently truncated filter's empty result would read as authoritative)."""
    c, mock_conn, _ = _coordinator_with_mocks()
    domains = [f"section-{i}"
              for i in range(coordinator_mod.SEARCH_DOMAINS_FILTER_CAP + 1)]
    resp = await c.handle_search(_make_request(
        {"query": "status", "limit": 5, "domains": domains}))
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "filters_invalid"
    assert str(coordinator_mod.SEARCH_DOMAINS_FILTER_CAP) in body["message"]
    # Fail-fast: rejected at ingress, before any DB work (no embed, no fetch).
    mock_conn.fetch.assert_not_called()
    mock_conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_domains_over_cap_400_does_not_leak_into_honest_empty_shape():
    """The cap rejection and the honest-empty-match shape must stay visibly
    distinct — a caller must be able to tell "your filter was rejected" from
    "your filter matched nothing"; conflating them would make a rejected,
    over-cap request look like a normal empty search result."""
    c, _mock_conn, _ = _coordinator_with_mocks()
    domains = [f"section-{i}"
              for i in range(coordinator_mod.SEARCH_DOMAINS_FILTER_CAP + 1)]
    resp = await c.handle_search(_make_request(
        {"query": "status", "limit": 5, "domains": domains}))
    body = json.loads(resp.text)
    assert resp.status != 200
    assert body != {"status": "success", "results": []}
    assert body.get("status") == "error"


@pytest.mark.asyncio
async def test_since_invalid_format_rejected_400():
    c, _, _s = _coordinator_with_mocks()
    resp = await c.handle_search(_make_request({"query": "x", "since": "not-a-date"}))
    assert resp.status == 400
    assert "since" in json.loads(resp.text)["message"]


@pytest.mark.asyncio
async def test_since_wrong_type_rejected_400():
    c, _, _s = _coordinator_with_mocks()
    resp = await c.handle_search(_make_request({"query": "x", "since": 12345}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_since_overlong_value_yields_a_bounded_400_message():
    """SEC-03 (six-role milestone audit, Required) — an invalid `since` value
    is echoed into the 400 message for the caller's benefit, but must not be
    echoed UNBOUNDED: that turns a validation error into an amplification
    vector. `_short()` caps the repr at 200 chars (+ ellipsis marker).

    MUTATION CHECK: replace `_short(since_raw)` back with `since_raw!r` at
    the `since` parse-failure site and this test's length assertion fails —
    the message balloons to the full 5000-char value."""
    c, _, _s = _coordinator_with_mocks()
    overlong = "not-a-date-" + "X" * 5000
    resp = await c.handle_search(_make_request({"query": "x", "since": overlong}))
    assert resp.status == 400
    body = json.loads(resp.text)
    assert "since" in body["message"]
    assert len(body["message"]) < 300


# ══════════════════════════════════════════════════════════════════════════
# (d) client surface — CLI passthrough, copy parity, MCP parity
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cli_search_passes_project_domain_since_through():
    mb = load_memory_bridge()
    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "search", "my query", "7",
            "--project", "alpha", "--domain", "ops", "--domain", "security",
            "--since", "2026-08-01",
        ]
        with patch.object(mb, "search_and_rerank",
                          new=AsyncMock(return_value=[])) as mock_search:
            await mb.main()
        assert mock_search.call_count == 1
        call = mock_search.call_args
        assert call.args[0] == "my query"
        assert call.args[1] == 7
        assert call.kwargs["project"] == "alpha"
        assert call.kwargs["domains"] == ["ops", "security"]
        assert call.kwargs["since"] == "2026-08-01"
    finally:
        sys.argv = argv_backup


@pytest.mark.asyncio
async def test_cli_search_without_filters_passes_none_through():
    """Old callers (bare query [+limit], no flags) must keep working exactly
    as before — the new kwargs default to None and search_and_rerank omits
    them from the wire body."""
    mb = load_memory_bridge()
    argv_backup = sys.argv
    try:
        sys.argv = ["memory_bridge.py", "search", "my query"]
        with patch.object(mb, "search_and_rerank",
                          new=AsyncMock(return_value=[])) as mock_search:
            await mb.main()
        call = mock_search.call_args
        assert call.args[0] == "my query"
        assert call.args[1] == 5  # default limit unchanged
        assert call.kwargs["project"] is None
        assert call.kwargs["domains"] is None
        assert call.kwargs["since"] is None
    finally:
        sys.argv = argv_backup


@pytest.mark.asyncio
async def test_search_and_rerank_body_omits_unset_filters():
    """search_and_rerank must send exactly what it always sent when no filter
    is passed — additive fields only appear when set."""
    mb = load_memory_bridge()
    mock_resp = MagicMock(status_code=200,
                          json=lambda: {"status": "success", "results": []})
    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        await mb.search_and_rerank("q")
    body = mock_post.call_args.kwargs["json"]
    assert "project" not in body
    assert "domains" not in body
    assert "since" not in body


@pytest.mark.asyncio
async def test_search_and_rerank_body_carries_filters_when_set():
    mb = load_memory_bridge()
    mock_resp = MagicMock(status_code=200,
                          json=lambda: {"status": "success", "results": []})
    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        await mb.search_and_rerank("q", project="alpha", domains=["ops"],
                                   since="2026-08-01")
    body = mock_post.call_args.kwargs["json"]
    assert body["project"] == "alpha"
    assert body["domains"] == ["ops"]
    assert body["since"] == "2026-08-01"


def test_both_tracked_memory_bridge_copies_are_byte_identical():
    source = os.path.join(_ROOT, "shared-memory", "scripts", "memory_bridge.py")
    shipped = os.path.join(_ROOT, "shared-memory-skill", "shared-memory",
                           "scripts", "memory_bridge.py")
    with open(source, "rb") as f_src, open(shipped, "rb") as f_ship:
        assert f_src.read() == f_ship.read(), (
            "memory_bridge.py copies have diverged — agents install the skill "
            "copy. Run: bash shared-memory/scripts/sync_skills.sh"
        )


@pytest.mark.asyncio
async def test_mcp_parity_hybrid_search_accepts_the_same_three_filters():
    """The two front doors must offer the same capability — a filter added to
    the CLI and not to MCP is exactly the parity gap Group 1 exists to catch."""
    vs = load_vector_skill()
    mock_resp = MagicMock(status_code=200,
                          json=lambda: {"status": "success", "results": []})
    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        await vs.hybrid_search_and_rerank("q", project="alpha", domains=["ops"],
                                          since="2026-08-01")
    body = mock_post.call_args.kwargs["json"]
    assert body["project"] == "alpha"
    assert body["domains"] == ["ops"]
    assert body["since"] == "2026-08-01"


@pytest.mark.asyncio
async def test_mcp_hybrid_search_without_filters_omits_them_from_the_wire():
    vs = load_vector_skill()
    mock_resp = MagicMock(status_code=200,
                          json=lambda: {"status": "success", "results": []})
    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        await vs.hybrid_search_and_rerank("q")
    body = mock_post.call_args.kwargs["json"]
    assert "project" not in body
    assert "domains" not in body
    assert "since" not in body
