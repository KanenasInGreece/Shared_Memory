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

    async def fake_search(*_a, **_kw):
        return results

    argv_backup = sys.argv
    try:
        sys.argv = ["memory_bridge.py", "search", "some query"]
        with patch.object(memory_bridge, "search_and_rerank", side_effect=fake_search):
            await memory_bridge.main()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr()
    assert "1 of 2 results are UNRANKED" in captured.err
    assert captured.err.strip().count("\n") == 0   # exactly one line to stderr
    # stdout carries the JSON, unchanged — no warning text mixed in
    assert "UNRANKED" not in captured.out
    import json as _json
    assert _json.loads(captured.out) == results


@pytest.mark.asyncio
async def test_cli_search_prints_no_stderr_note_when_fully_ranked(capsys):
    results = [{"pg_id": 1, "content": "a", "ranked": True}]

    async def fake_search(*_a, **_kw):
        return results

    argv_backup = sys.argv
    try:
        sys.argv = ["memory_bridge.py", "search", "some query"]
        with patch.object(memory_bridge, "search_and_rerank", side_effect=fake_search):
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


# ── B3: doctor / check_gateway_compat surfaces agent/role ────────────────────

def _health(payload):
    return MagicMock(status_code=200, json=lambda: payload)


@pytest.mark.asyncio
async def test_cli_doctor_surfaces_agent_and_role_when_present():
    payload = {"status": "ok", "version": "0.9.52",
              "api_version": memory_bridge.API_VERSION,
              "agent": "claude-code", "role": "write"}
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["agent"] == "claude-code"
    assert diag["role"] == "write"


@pytest.mark.asyncio
async def test_cli_doctor_role_predates_when_gateway_is_genuinely_old():
    """T-04 (PR #310 review), case 2: the gateway's OWN reported version is
    below ROLE_REPORTING_MIN_VERSION — it genuinely never sends `role`."""
    payload = {"status": "ok", "version": "0.9.40",
              "api_version": memory_bridge.API_VERSION}
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert "agent" not in diag   # only shown when present
    assert diag["role"] == "not reported (gateway 0.9.40 predates 0.9.52)"


@pytest.mark.asyncio
async def test_cli_doctor_role_anonymous_when_gateway_is_current():
    """T-04 (PR #310 review), case 3: gateway_version says CURRENT (>=
    0.9.52) but `role` is absent — that combination is not "old gateway", it
    is "this token was not accepted" (anonymous-slim payload on a current
    gateway). The old single fallback text asserted the version floor even
    here, contradicting gateway_version in the SAME payload."""
    payload = {"status": "ok", "version": "0.9.52",
              "api_version": memory_bridge.API_VERSION}
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["gateway_version"] == "0.9.52"
    assert "agent" not in diag
    assert diag["role"] == "not reported (token not accepted — anonymous payload)"


@pytest.mark.asyncio
async def test_cli_doctor_role_unparseable_version_treated_as_predates():
    """No parseable version at all (very old, pre-version-contract gateway,
    or a malformed string) is treated the same as "predates" — conservative,
    since a gateway too old to report even a parseable version is certainly
    too old to report role."""
    payload = {"status": "ok"}   # no `version` key
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["role"] == "not reported (gateway version unknown, predates 0.9.52 assumed)"


@pytest.mark.asyncio
async def test_mcp_check_memory_health_surfaces_agent_and_role_when_present():
    payload = {"status": "ok", "version": "0.9.52",
              "api_version": vector_skill.API_VERSION,
              "agent": "vector-skill-agent", "role": "read"}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await vector_skill.check_memory_health()
    import json as _json
    parsed = _json.loads(result)
    assert parsed["agent"] == "vector-skill-agent"
    assert parsed["role"] == "read"


@pytest.mark.asyncio
async def test_mcp_check_memory_health_role_predates_when_gateway_is_genuinely_old():
    """T-04, case 2, MCP door."""
    payload = {"status": "ok", "version": "0.9.40", "api_version": vector_skill.API_VERSION}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await vector_skill.check_memory_health()
    import json as _json
    parsed = _json.loads(result)
    assert "agent" not in parsed
    assert parsed["role"] == "not reported (gateway 0.9.40 predates 0.9.52)"


@pytest.mark.asyncio
async def test_mcp_check_memory_health_role_anonymous_when_gateway_is_current():
    """T-04, case 3, MCP door: current gateway, absent role → this token was
    not accepted, not "old gateway"."""
    payload = {"status": "ok", "version": "0.9.52", "api_version": vector_skill.API_VERSION}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await vector_skill.check_memory_health()
    import json as _json
    parsed = _json.loads(result)
    assert parsed["version"] == "0.9.52"
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
    assert parsed["role"] == "not reported (gateway version unknown, predates 0.9.52 assumed)"


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
