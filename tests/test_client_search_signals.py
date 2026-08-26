"""
Tests for PR-B units B2 (unranked-results warning) and B3 (doctor surfaces
agent/role).

WHY THIS EXISTS. B1 (tests/test_search_ceiling.py) fixed the client-side
TIMEOUT so a slow-but-alive gateway is not misdiagnosed as down. These two
units are about the two other client blind spots in the same area:

  B2  When the gateway serves a search in vector order (the reranker timed
      out), each result row already carries `ranked: false` — but nothing
      told the operator. A positional result list printed silently in that
      state reads as ranked when it is not.

  B3  `doctor`/`check_memory_health` is the one place an operator can see
      which agent identity and role a token resolves to. A server PR (not
      this one) adds `agent`/`role` to the authenticated /health payload;
      this unit makes both clients surface them, with an honest fallback for
      an older gateway that does not send `role` at all.

Both front doors, mirroring the parity discipline of test_search_ceiling.py.
"""
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
pytest.importorskip("fastmcp")


def _load(name, *parts):
    path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", *parts))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


memory_bridge = _load(
    "memory_bridge_signals",
    "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py",
)
vector_skill = _load("vector_skill_signals", "mcp", "vector-skill.py")

CLIENTS = [
    pytest.param(memory_bridge, id="cli"),
    pytest.param(vector_skill, id="mcp"),
]


# ── B2: pure helper, memory_bridge._unranked_warning ──────────────────────────

def test_unranked_warning_none_when_all_ranked():
    results = [{"pg_id": 1, "ranked": True}, {"pg_id": 2, "ranked": True}]
    assert memory_bridge._unranked_warning(results) is None


def test_unranked_warning_none_on_empty_results():
    assert memory_bridge._unranked_warning([]) is None


def test_unranked_warning_none_on_non_list_payload():
    """An error payload ({"status": "error", ...}) is not a row list — the
    warning must not misfire on it."""
    assert memory_bridge._unranked_warning({"status": "error", "message": "x"}) is None


def test_unranked_warning_counts_n_of_m():
    results = [{"pg_id": 1, "ranked": True}, {"pg_id": 2, "ranked": False},
               {"pg_id": 3, "ranked": False}, {"pg_id": 4}]  # missing key = not False
    warning = memory_bridge._unranked_warning(results)
    assert warning is not None
    assert warning.startswith("2 of 4 results are UNRANKED")
    assert "vector order" in warning
    assert "backend_capability on /health" in warning


# ── B2: CLI integration — stderr note, stdout JSON unchanged ─────────────────

@pytest.mark.asyncio
async def test_cli_search_prints_stderr_note_and_leaves_stdout_json_unchanged(capsys):
    results = [{"pg_id": 1, "content": "a", "ranked": True},
              {"pg_id": 2, "content": "b", "ranked": False}]

    # v0.9.62: main() now calls `_search_payload` directly (not
    # `search_and_rerank`) so it can derive both the unranked and the
    # fallback warning from one payload without a second HTTP round trip.
    async def fake_payload(*_a, **_kw):
        return {"status": "success", "results": results}

    argv_backup = sys.argv
    try:
        sys.argv = ["memory_bridge.py", "search", "some query"]
        with patch.object(memory_bridge, "_search_payload", side_effect=fake_payload):
            await memory_bridge.main()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    assert "1 of 2 results are UNRANKED" in captured.err
    assert "EMBEDDING UNAVAILABLE" not in captured.err
    assert captured.err.strip().count("\n") == 0   # exactly one line to stderr
    # stdout carries the JSON, unchanged — no warning text mixed in
    assert "UNRANKED" not in captured.out
    import json as _json
    assert _json.loads(captured.out) == results


@pytest.mark.asyncio
async def test_cli_search_prints_no_stderr_note_when_fully_ranked(capsys):
    results = [{"pg_id": 1, "content": "a", "ranked": True}]

    async def fake_payload(*_a, **_kw):
        return {"status": "success", "results": results}

    argv_backup = sys.argv
    try:
        sys.argv = ["memory_bridge.py", "search", "some query"]
        with patch.object(memory_bridge, "_search_payload", side_effect=fake_payload):
            await memory_bridge.main()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    assert captured.err == ""


# ── B2: MCP — note prepended to the rendered text when it returns text ───────

MOCK_QUERY = "mcp unranked test"


@pytest.mark.asyncio
async def test_mcp_search_prepends_unranked_note_when_any_row_is_unranked():
    payload = {"results": [
        {"pg_id": 1, "ref": "fact:1", "record_type": "fact", "content": "a",
         "ranked": True, "score": 0.9},
        {"pg_id": 2, "ref": "fact:2", "record_type": "fact", "content": "b",
         "ranked": False, "score": 0.5},
        {"pg_id": 3, "ref": "fact:3", "record_type": "fact", "content": "c",
         "ranked": False, "score": 0.4},
    ]}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)

    assert result.startswith("NOTE: 2 of 3 results are UNRANKED")
    assert "backend_capability on /health" in result
    assert "Unified Memory Results" in result   # rendering still happens after


@pytest.mark.asyncio
async def test_mcp_search_no_note_when_all_ranked():
    payload = {"results": [
        {"pg_id": 1, "ref": "fact:1", "record_type": "fact", "content": "a",
         "ranked": True, "score": 0.9},
    ]}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)
    assert "UNRANKED" not in result


# ── v0.9.62 (fact:1609): the keyword-fallback headline ───────────────────────
# When the embedder is unavailable the gateway does not fail a search — it
# serves a KEYWORD (substring) fallback and answers honestly with
# {"status":"success","fallback":"keyword","results":[...]}. Both clients
# used to strip the envelope down to the results list before this, so the
# `fallback` marker never reached the operator; a natural-language query
# almost never ILIKE-matches, so the common shape of that silence was an
# EMPTY list — the one case `_unranked_warning` can never fire on.

def _keyword_fallback_payload(n):
    rows = [{"pg_id": i, "content": f"row {i}", "score": 0.0,
              "score_normalized": 0.5} for i in range(n)]
    return {"status": "success", "fallback": "keyword", "results": rows}


# (a) pure helper — None cases

def test_fallback_warning_none_on_normal_payload():
    payload = {"status": "success", "results": [{"pg_id": 1}]}
    assert memory_bridge._fallback_warning(payload) is None


def test_fallback_warning_none_on_error_payload():
    payload = {"status": "error", "message": "boom"}
    assert memory_bridge._fallback_warning(payload) is None


def test_fallback_warning_none_on_non_dict():
    assert memory_bridge._fallback_warning([{"pg_id": 1}]) is None
    assert memory_bridge._fallback_warning(None) is None
    assert memory_bridge._fallback_warning("nope") is None


# (b) fires, including the empty-list case that is the whole point

def test_fallback_warning_fires_with_zero_results():
    warning = memory_bridge._fallback_warning(_keyword_fallback_payload(0))
    assert warning is not None
    assert warning.startswith("EMBEDDING UNAVAILABLE")
    assert "0 result(s)" in warning
    assert "unranked" in warning
    assert "embedder on /health" in warning


def test_fallback_warning_fires_with_two_results():
    warning = memory_bridge._fallback_warning(_keyword_fallback_payload(2))
    assert warning is not None
    assert "2 result(s)" in warning


# (c) parity — both clients return the identical sentence for the identical input

@pytest.mark.parametrize("payload", [
    _keyword_fallback_payload(0),
    _keyword_fallback_payload(2),
    {"status": "success", "results": [{"pg_id": 1}]},
    {"status": "error", "message": "boom"},
    None,
    [{"pg_id": 1}],
])
def test_fallback_warning_parity_between_clients(payload):
    assert memory_bridge._fallback_warning(payload) == vector_skill._fallback_warning(payload)


# (d) CLI door — stderr carries the headline, stdout JSON is exactly the (empty) results

@pytest.mark.asyncio
async def test_cli_search_prints_fallback_warning_on_empty_keyword_fallback(capsys):
    payload = _keyword_fallback_payload(0)

    async def fake_payload(*_a, **_kw):
        return payload

    argv_backup = sys.argv
    try:
        sys.argv = ["memory_bridge.py", "search", "some query"]
        with patch.object(memory_bridge, "_search_payload", side_effect=fake_payload):
            await memory_bridge.main()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    assert "EMBEDDING UNAVAILABLE" in captured.err
    assert "0 result(s)" in captured.err
    import json as _json
    assert _json.loads(captured.out) == []


# (e) MCP door — rendered text starts with the NOTE-prefixed headline

@pytest.mark.asyncio
async def test_mcp_search_prepends_fallback_note_on_empty_keyword_fallback():
    payload = _keyword_fallback_payload(0)
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)
    assert result.startswith("NOTE: EMBEDDING UNAVAILABLE")
    assert "0 result(s)" in result


# ── B3: doctor / check_gateway_compat surfaces agent/role ────────────────────

def _health(payload):
    return MagicMock(status_code=200, json=lambda: payload)


# R2-01 (PR #310 review round 2 delta) fixtures. agent/role ship on the
# AUTHENTICATED /health payload in a LATER server PR (0.9.54) than the one
# merged so far (0.9.52, PR-A). So the "predates" role case is not
# hypothetical -- it is the shape a valid token gets against TODAY's real
# merged gateway. The two fixtures below are distinguishable by BOTH version
# AND shape, matching what handle_health actually sends: a full authenticated
# body (many operational keys) is what a valid-but-pre-0.9.54 token receives;
# the anonymous-slim triple is what a rejected/absent token receives
# regardless of gateway version.

def _authenticated_full_payload(version, api_version, **extra):
    """A realistic AUTHENTICATED /health body on a gateway that predates
    ROLE_REPORTING_MIN_VERSION: full operational detail, just no agent/role
    keys yet (they don't exist server-side before 0.9.54)."""
    payload = {
        "status": "ok",
        "version": version,
        "api_version": api_version,
        "backend_capability": {"reranker": {"status": "ok"}, "embedder": {"status": "ok"}},
        "capacity": None,
        "daemon": {"rem": {"status": "running"}, "consolidation": {"status": "idle"}},
    }
    payload.update(extra)
    return payload


def _anonymous_slim_payload(version, api_version):
    """The REAL anonymous-slim shape `handle_health` sends an unauthenticated
    or rejected-token caller: exactly {status, version, api_version}, none of
    the operational detail above -- regardless of how current the gateway is."""
    return {"status": "ok", "version": version, "api_version": api_version}


@pytest.mark.asyncio
async def test_cli_doctor_surfaces_agent_and_role_when_present():
    payload = _authenticated_full_payload("0.9.54", memory_bridge.API_VERSION,
                                          agent="claude-code", role="write")
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["agent"] == "claude-code"
    assert diag["role"] == "write"


@pytest.mark.asyncio
async def test_cli_doctor_role_predates_when_gateway_is_genuinely_old():
    """T-04/R2-01: 0.9.52 is TODAY's real merged gateway version (PR-A) --
    it genuinely does not ship `role` yet (that's PR #311, 0.9.54). A valid
    token still gets the FULL authenticated shape, just without agent/role."""
    payload = _authenticated_full_payload("0.9.52", memory_bridge.API_VERSION)
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert "agent" not in diag   # only shown when present
    assert diag["role"] == "not reported (gateway 0.9.52 predates 0.9.54)"


@pytest.mark.asyncio
async def test_cli_doctor_role_anonymous_when_gateway_is_current():
    """T-04/R2-01, case 3: gateway_version says CURRENT (>= 0.9.54) but the
    payload is the SLIM anonymous shape -- that combination is not "old
    gateway", it is "this token was not accepted". Distinguishable by shape
    as well as version, matching what the real server actually sends."""
    payload = _anonymous_slim_payload("0.9.54", memory_bridge.API_VERSION)
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["gateway_version"] == "0.9.54"
    assert "agent" not in diag
    assert diag["role"] == "not reported (token not accepted — anonymous payload)"


@pytest.mark.asyncio
async def test_cli_doctor_role_unparseable_version_treated_as_predates():
    """No parseable version at all (very old, pre-version-contract gateway,
    or a malformed string) is treated the same as "predates" -- conservative,
    since a gateway too old to report even a parseable version is certainly
    too old to report role."""
    payload = {"status": "ok"}   # no `version` key
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["role"] == "not reported (gateway version unknown, predates 0.9.54 assumed)"


@pytest.mark.asyncio
async def test_mcp_check_memory_health_surfaces_agent_and_role_when_present():
    payload = _authenticated_full_payload("0.9.54", vector_skill.API_VERSION,
                                          agent="vector-skill-agent", role="read")
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await vector_skill.check_memory_health()
    import json as _json
    parsed = _json.loads(result)
    assert parsed["agent"] == "vector-skill-agent"
    assert parsed["role"] == "read"


@pytest.mark.asyncio
async def test_mcp_check_memory_health_role_predates_when_gateway_is_genuinely_old():
    """T-04/R2-01, MCP door: today's real merged gateway version (0.9.52),
    full authenticated shape, no agent/role yet."""
    payload = _authenticated_full_payload("0.9.52", vector_skill.API_VERSION)
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await vector_skill.check_memory_health()
    import json as _json
    parsed = _json.loads(result)
    assert "agent" not in parsed
    assert parsed["role"] == "not reported (gateway 0.9.52 predates 0.9.54)"


@pytest.mark.asyncio
async def test_mcp_check_memory_health_role_anonymous_when_gateway_is_current():
    """T-04/R2-01, case 3, MCP door: current gateway, SLIM anonymous shape ->
    this token was not accepted, not "old gateway"."""
    payload = _anonymous_slim_payload("0.9.54", vector_skill.API_VERSION)
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await vector_skill.check_memory_health()
    import json as _json
    parsed = _json.loads(result)
    assert parsed["version"] == "0.9.54"
    assert "agent" not in parsed
    assert parsed["role"] == "not reported (token not accepted — anonymous payload)"


@pytest.mark.asyncio
async def test_mcp_check_memory_health_role_unparseable_version_treated_as_predates():
    payload = {"status": "ok", "api_version": vector_skill.API_VERSION}   # no version key
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await vector_skill.check_memory_health()
    import json as _json
    parsed = _json.loads(result)
    assert parsed["role"] == "not reported (gateway version unknown, predates 0.9.54 assumed)"


# ── T-03 (PR #310 review): the auth header is load-bearing — pin it ─────────
# Removing `headers=_request_headers()`/`_auth_headers()` from either
# /health fetch leaves the suite fully green (measured by the review) since
# every stub accepted `*_a, **_kw` and never inspected what was sent. On an
# auth-configured install that silently and permanently pins EVERY client at
# the constant fallback and EVERY doctor at the "old gateway" line — exactly
# the "unknown cost" case this whole PR exists to fix, just forever instead
# of only when the gateway is genuinely old/unreachable.

@pytest.mark.asyncio
async def test_cli_health_fetch_sends_auth_headers(monkeypatch):
    monkeypatch.setenv("COORDINATOR_UDS", "")
    monkeypatch.setenv("AGENT_TOKEN", "tok_test_t03_cli")
    monkeypatch.setattr(memory_bridge, "_CAPABILITY_CACHE", None)
    monkeypatch.setattr(memory_bridge, "_CAPACITY_CACHE", None, raising=False)

    captured = {}

    async def fake_get(self, url, *, headers=None, **kw):
        captured["headers"] = headers
        return _health({})

    import httpx as _httpx
    monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

    await memory_bridge._gateway_capability()

    assert captured.get("headers") is not None, "no headers kwarg reached the /health GET at all"
    assert captured["headers"].get("Authorization") == "Bearer tok_test_t03_cli"


@pytest.mark.asyncio
async def test_cli_doctor_health_fetch_sends_auth_headers(monkeypatch):
    """check_gateway_compat's own /health GET, separately from
    _fetch_health_blocks — both fetches carry the header, tested separately
    since they are two different call sites."""
    monkeypatch.setenv("COORDINATOR_UDS", "")
    monkeypatch.setenv("AGENT_TOKEN", "tok_test_t03_doctor")
    captured = {}

    async def fake_get(self, url, *, headers=None, **kw):
        captured["headers"] = headers
        return _health({"status": "ok"})

    import httpx as _httpx
    monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

    await memory_bridge.check_gateway_compat()

    assert captured.get("headers") is not None
    assert captured["headers"].get("Authorization") == "Bearer tok_test_t03_doctor"


@pytest.mark.asyncio
async def test_mcp_health_fetch_sends_auth_headers(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "tok_test_t03_mcp")
    monkeypatch.setattr(vector_skill, "_CAPABILITY_CACHE", None)
    monkeypatch.setattr(vector_skill, "_CAPACITY_CACHE", None, raising=False)

    captured = {}

    async def fake_get(self, url, *, headers=None, **kw):
        captured["headers"] = headers
        return MagicMock(status_code=200, json=lambda: {})

    import httpx as _httpx
    monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

    await vector_skill._gateway_capability()

    assert captured.get("headers") is not None
    assert captured["headers"].get("Authorization") == "Bearer tok_test_t03_mcp"


@pytest.mark.asyncio
async def test_mcp_doctor_health_fetch_sends_auth_headers(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "tok_test_t03_mcp_doctor")
    captured = {}

    async def fake_get(self, url, *, headers=None, **kw):
        captured["headers"] = headers
        return MagicMock(status_code=200, json=lambda: {"status": "ok"})

    import httpx as _httpx
    monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

    await vector_skill.check_memory_health()

    assert captured.get("headers") is not None
    assert captured["headers"].get("Authorization") == "Bearer tok_test_t03_mcp_doctor"


# ── T-09: the all-unranked case ──────────────────────────────────────────────

def test_unranked_warning_all_unranked():
    results = [{"pg_id": 1, "ranked": False}, {"pg_id": 2, "ranked": False},
               {"pg_id": 3, "ranked": False}]
    warning = memory_bridge._unranked_warning(results)
    assert warning == "3 of 3 results are UNRANKED — the reranker timed out, this is vector order (see backend_capability on /health)"


@pytest.mark.asyncio
async def test_mcp_search_note_when_all_unranked():
    payload = {"results": [
        {"pg_id": 1, "ref": "fact:1", "record_type": "fact", "content": "a",
         "ranked": False, "score": 0.5},
    ]}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)
    assert result.startswith("NOTE: 1 of 1 results are UNRANKED")


# ── T-07: both doors share the identical unranked SENTENCE ──────────────────

@pytest.mark.parametrize("results", [
    [],
    [{"pg_id": 1, "ranked": True}],
    [{"pg_id": 1, "ranked": True}, {"pg_id": 2, "ranked": False}],
    [{"pg_id": 1, "ranked": False}, {"pg_id": 2, "ranked": False}, {"pg_id": 3}],
    {"status": "error", "message": "x"},
])
def test_unranked_warning_parity_between_both_doors(results):
    assert memory_bridge._unranked_warning(results) == vector_skill._unranked_warning(results)


# ── T-08: --help / usage text actually mentions the two env vars ────────────

def test_top_level_docstring_mentions_env_overrides():
    assert "SEARCH_TIMEOUT_S" in memory_bridge.__doc__
    assert "SHARED_MEMORY_PROJECT" in memory_bridge.__doc__


def test_search_subparser_help_mentions_search_timeout_s():
    help_text = memory_bridge._search_argparser().format_help()
    assert "SEARCH_TIMEOUT_S" in help_text


def test_save_subparser_help_mentions_shared_memory_project():
    help_text = memory_bridge._save_argparser().format_help()
    assert "SHARED_MEMORY_PROJECT" in help_text


def test_mcp_search_docstring_mentions_search_timeout_s():
    assert "SEARCH_TIMEOUT_S" in vector_skill.hybrid_search_and_rerank.__doc__


# R2-02 (PR #310 review round 2 delta): _stale_projection_note had ZERO test
# coverage -- M7 (making it return None unconditionally in all three files)
# left the suite fully green. The two fixtures below are the REAL shapes
# PR-A's merged server produces via `_merge_capability_projection()` /
# `_projection_age_s()`, not hand-written minimal dicts (same fixtures as
# tests/test_search_ceiling.py's R2-02 block, duplicated here per this
# suite's own convention -- LIVE_CAPABILITY is duplicated the same way).

_R2_02_STALE_WITH_CARRIED = {
    "status": "ok",
    "probed_at": "2026-08-25T12:00:00+00:00",
    "reranker": {
        "probe_chars": 4000,
        "status": "failing",
        "error": "TimeoutError",
        "projected_full_payload_s": 127.0,
        "ceiling_s": 921.6,
        "throughput_chars_s": 3870,
        "latency_s": 1.03,
        "serves_full_payload": None,
        "projection_stale": True,
        "last_ok_at": "2026-08-25T10:42:29.900000+00:00",
        "projection_age_s": 4650.1,
    },
    "embedder": {
        "probe_chars": 1000,
        "status": "ok",
        "projected_full_payload_s": 6.3,
        "ceiling_s": 122.9,
        "throughput_chars_s": 3906,
        "latency_s": 0.26,
        "serves_full_payload": True,
        "projection_stale": False,
        "last_ok_at": "2026-08-25T12:00:00+00:00",
        "projection_age_s": 0.0,
    },
}

_R2_02_NEVER_MEASURED = {
    "status": "unknown",
    "probed_at": None,
    "reranker": {
        "probe_chars": 4000,
        "status": "failing",
        "error": "ConnectError",
        "serves_full_payload": None,
        "projection_stale": None,   # PR-A's third state: never measured
    },
    "embedder": {
        "probe_chars": 1000,
        "status": "failing",
        "error": "ConnectError",
        "serves_full_payload": None,
        "projection_stale": None,
    },
}


@pytest.mark.parametrize("client", CLIENTS)
def test_stale_projection_note_present_with_carried_stale_number(client):
    """The real fact:1560 shape: reranker carries a stale-but-real number
    (age 4650.1s) forward through a failing cycle. Byte-identical on both
    doors -- the note text itself is part of what T-07's lesson says must be
    pinned, not just its presence."""
    note = client._stale_projection_note(_R2_02_STALE_WITH_CARRIED)
    assert note == "reranker projection stale for 4650s"


@pytest.mark.parametrize("client", CLIENTS)
def test_stale_projection_note_silent_on_projection_stale_null(client):
    """PR-A's third state -- `projection_stale: None` (never measured) -- is
    NOT the same as stale; there is nothing to say "stale" about."""
    assert client._stale_projection_note(_R2_02_NEVER_MEASURED) is None


@pytest.mark.parametrize("client", CLIENTS)
def test_stale_projection_note_silent_on_fresh(client):
    """A backend that was just measured (`projection_stale: False`) with a
    real, current projection gets no note -- freshness is silent, only
    staleness speaks. Uses the embedder half of the real fixture above in
    isolation, so a capability block with ONLY a fresh backend is covered
    too (the mixed fixture always pairs it with the stale reranker)."""
    fresh_only = {"embedder": dict(_R2_02_STALE_WITH_CARRIED["embedder"])}
    assert client._stale_projection_note(fresh_only) is None


@pytest.mark.parametrize("client", CLIENTS)
def test_stale_projection_note_none_on_absent_or_malformed_capability(client):
    assert client._stale_projection_note(None) is None
    assert client._stale_projection_note({}) is None
    assert client._stale_projection_note("not-a-dict") is None


@pytest.mark.parametrize("capability", [
    pytest.param(_R2_02_STALE_WITH_CARRIED, id="stale-with-carried"),
    pytest.param(_R2_02_NEVER_MEASURED, id="never-measured"),
    pytest.param(None, id="none"),
    pytest.param({}, id="empty"),
    pytest.param({"reranker": {"projection_stale": True, "projection_age_s": 12.4},
                  "embedder": {"projection_stale": True}}, id="both-stale-one-ageless"),
])
def test_stale_projection_note_parity_between_both_doors(capability):
    """T-07's lesson, re-applied: the two doors never import each other, so
    this is the only thing holding the copies in step."""
    assert (memory_bridge._stale_projection_note(capability)
            == vector_skill._stale_projection_note(capability))

