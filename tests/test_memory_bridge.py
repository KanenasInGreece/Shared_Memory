import pytest
import json
import asyncio
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch, AsyncMock

# Dynamic load of memory_bridge.py
def load_memory_bridge():
    path = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")
    spec = importlib.util.spec_from_file_location("memory_bridge", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge"] = module
    spec.loader.exec_module(module)
    return module

memory_bridge = load_memory_bridge()

# Mock data
MOCK_EMBEDDING = [0.1] * 1024
MOCK_PG_ID = 42
MOCK_CONTENT = "Test content"
MOCK_QUERY = "Test query"

@pytest.mark.asyncio
async def test_save_artifact_coordinator_unreachable():
    with patch("httpx.AsyncClient.post", side_effect=Exception("coordinator down")):
        result = await memory_bridge.save_artifact(MOCK_CONTENT)
    assert result["status"] == "error"
    assert "coordinator" in result["message"].lower() or "unreachable" in result["message"].lower()

@pytest.mark.asyncio
async def test_save_artifact_bad_metadata_json():
    result = await memory_bridge.save_artifact(MOCK_CONTENT, "not-valid-json")
    assert result["status"] == "error"
    assert "metadata" in result["message"].lower()

@pytest.mark.asyncio
async def test_save_artifact_success():
    mock_resp = MagicMock(json=lambda: {"status": "success", "pg_id": MOCK_PG_ID})
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        result = await memory_bridge.save_artifact(MOCK_CONTENT, '{"source":"test","entities":["E1"]}')
    assert result["status"] == "success"
    assert result["pg_id"] == MOCK_PG_ID

@pytest.mark.asyncio
async def test_save_artifact_coordinator_error_response():
    mock_resp = MagicMock(json=lambda: {"status": "error", "message": "internal error"})
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        result = await memory_bridge.save_artifact(MOCK_CONTENT)
    assert result["status"] == "error"

@pytest.mark.asyncio
async def test_search_and_rerank_full_success():
    mock_results = [{"pg_id": MOCK_PG_ID, "content": MOCK_CONTENT, "score": 0.95,
                     "tier": "fact", "score_normalized": 0.72, "matched_entities": [],
                     "graph_context": []}]
    mock_resp = MagicMock(json=lambda: {"status": "success", "results": mock_results})
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        results = await memory_bridge.search_and_rerank(MOCK_QUERY)
    assert len(results) == 1
    assert results[0]["content"] == MOCK_CONTENT
    assert results[0]["score"] == 0.95

@pytest.mark.asyncio
async def test_search_and_rerank_coordinator_unreachable():
    with patch("httpx.AsyncClient.post", side_effect=Exception("coordinator down")):
        result = await memory_bridge.search_and_rerank(MOCK_QUERY)
    assert isinstance(result, dict)
    assert result["status"] == "error"


# ── Request headers — Phase 2C auth + version contract ────────────────────────
# _request_headers() always advertises the client API_VERSION; the Bearer token
# is added only when AGENT_TOKEN is set.

_VER = {memory_bridge.CLIENT_VERSION_HEADER: str(memory_bridge.API_VERSION)}


def test_request_headers_version_only_when_no_token(monkeypatch):
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    # Neutralise whatever this module parsed out of a real on-disk .env at
    # import time (S-18: no longer exported to os.environ, so delenv alone
    # can't clear it) -- a dev machine's real skill install must not make
    # this test's "no token configured" premise flaky.
    monkeypatch.setattr(memory_bridge, "_AGENT_TOKEN_FROM_FILE", "")
    assert memory_bridge._request_headers() == _VER


def test_request_headers_adds_bearer_header_when_token_set(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "tok_testtoken123")
    headers = memory_bridge._request_headers()
    assert headers == {**_VER, "Authorization": "Bearer tok_testtoken123"}


def test_request_headers_strips_whitespace(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "  tok_abc  ")
    headers = memory_bridge._request_headers()
    assert headers == {**_VER, "Authorization": "Bearer tok_abc"}


def test_request_headers_empty_token_is_version_only(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "")
    monkeypatch.setattr(memory_bridge, "_AGENT_TOKEN_FROM_FILE", "")
    assert memory_bridge._request_headers() == _VER


# ── S-18: dotenv scope + never exported to os.environ (PR A2) ────────────────
# These import a FRESH copy of memory_bridge.py from a controlled tmp skill
# layout, because _ENV_CANDIDATES is computed from __file__ at import time --
# the already-imported `memory_bridge` module above can't be re-pointed at a
# different directory tree after the fact.

def _load_memory_bridge_from(skill_dir: str):
    import shutil
    import uuid
    scripts_dir = os.path.join(skill_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    src = os.path.join(
        os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory",
        "scripts", "memory_bridge.py",
    )
    dest = os.path.join(scripts_dir, "memory_bridge.py")
    shutil.copy(src, dest)
    mod_name = f"memory_bridge_isolated_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, dest)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dotenv_scope_stray_home_level_env_is_not_picked_up(tmp_path, monkeypatch):
    """A .env ABOVE the skill root (the $HOME-walk find_dotenv() used to
    reach) must be invisible -- S-18's whole point."""
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (tmp_path / ".env").write_text("AGENT_TOKEN=tok_from_stray_home_env\n")

    mod = _load_memory_bridge_from(str(skill_dir))

    assert mod._AGENT_TOKEN_FROM_FILE == ""
    assert os.environ.get("AGENT_TOKEN") != "tok_from_stray_home_env"


def test_dotenv_scope_skill_root_env_is_picked_up(tmp_path, monkeypatch):
    # A real AGENT_TOKEN sitting in this SESSION's os.environ (e.g. a
    # pre-existing, unrelated script's own naive env loader having already
    # run at collection time) must not shadow the value this test is
    # actually exercising -- os.environ is checked first by design, so an
    # ambient leak from elsewhere would silently pass this test for the
    # wrong reason. See the class of bug CLAUDE.md's "run the suite twice,
    # once with a real .env present" rule exists to catch.
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / ".env").write_text("AGENT_TOKEN=tok_from_skill_root\n")

    mod = _load_memory_bridge_from(str(skill_dir))

    assert mod._AGENT_TOKEN_FROM_FILE == "tok_from_skill_root"
    assert mod._request_headers().get("Authorization") == "Bearer tok_from_skill_root"


def test_dotenv_scope_scripts_adjacent_env_is_picked_up(tmp_path, monkeypatch):
    """The other documented candidate: an .env living beside memory_bridge.py
    itself (scripts/.env), not just the skill root."""
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    skill_dir = tmp_path / "skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / ".env").write_text("AGENT_TOKEN=tok_from_scripts_dir\n")

    mod = _load_memory_bridge_from(str(skill_dir))

    assert mod._AGENT_TOKEN_FROM_FILE == "tok_from_scripts_dir"


def test_dotenv_load_never_exports_agent_token_to_os_environ(tmp_path, monkeypatch):
    """The core A1-deferred fix: AGENT_TOKEN sourced from the .env file must
    never land in this process's own os.environ, even though it IS used for
    this client's own outbound requests."""
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / ".env").write_text(
        "AGENT_TOKEN=tok_from_skill_root\nCOORDINATOR_URL=http://example.invalid:9999\n"
    )

    mod = _load_memory_bridge_from(str(skill_dir))

    assert "AGENT_TOKEN" not in os.environ
    # Other (non-secret) config keys still flow through as before.
    assert mod.COORDINATOR_BASE == "http://example.invalid:9999"


def test_dotenv_load_never_exports_gateway_secrets_to_os_environ(tmp_path, monkeypatch):
    """Required fix (A2 security review, finding 7): candidate 2 (the skill
    root .env) IS the gateway .env when this client is invoked from the
    repo root in admin mode -- so PG_PASSWORD/NEO4J_PASSWORD/AGENT_TOKENS/
    provider keys must never reach this client process's own os.environ,
    the same leak class S-18 already closed for AGENT_TOKEN specifically,
    one level broader."""
    for _k in ("PG_PASSWORD", "NEO4J_PASSWORD", "SOME_PROVIDER_API_KEY"):
        monkeypatch.delenv(_k, raising=False)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / ".env").write_text(
        "PG_PASSWORD=super_secret_pg\n"
        "NEO4J_PASSWORD=super_secret_neo4j\n"
        "SOME_PROVIDER_API_KEY=super_secret_key\n"
        "COORDINATOR_URL=http://example.invalid:9999\n"
    )

    mod = _load_memory_bridge_from(str(skill_dir))

    assert "PG_PASSWORD" not in os.environ
    assert "NEO4J_PASSWORD" not in os.environ
    assert "SOME_PROVIDER_API_KEY" not in os.environ
    # Non-secret config keeps flowing through, unaffected.
    assert mod.COORDINATOR_BASE == "http://example.invalid:9999"


def test_dotenv_scope_operator_export_still_wins_over_file(tmp_path, monkeypatch):
    """An operator's own real `export AGENT_TOKEN=...` (or a test's
    monkeypatch.setenv) must still take precedence over the file value --
    the client never becomes LESS configurable than before."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / ".env").write_text("AGENT_TOKEN=tok_from_file\n")
    monkeypatch.setenv("AGENT_TOKEN", "tok_from_operator_export")

    mod = _load_memory_bridge_from(str(skill_dir))

    assert mod._request_headers().get("Authorization") == "Bearer tok_from_operator_export"


# ── Version contract — check_gateway_compat ───────────────────────────────────

def _health(payload):
    return MagicMock(json=lambda: payload)


@pytest.mark.asyncio
async def test_compat_ok_when_versions_match():
    payload = {"status": "ok", "version": "0.4.6", "api_version": memory_bridge.API_VERSION}
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["compat"] == "ok"
    assert "warning" not in diag


@pytest.mark.asyncio
async def test_compat_incompatible_names_side_to_upgrade():
    # Gateway ahead of the client → the client should be told to upgrade.
    payload = {"status": "ok", "api_version": memory_bridge.API_VERSION + 1}
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["compat"] == "incompatible"
    assert "client" in diag["warning"].lower()


@pytest.mark.asyncio
async def test_compat_unknown_for_old_gateway_without_field():
    payload = {"status": "ok"}  # predates the version contract
    with patch("httpx.AsyncClient.get", return_value=_health(payload)):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["compat"] == "unknown"
    assert "warning" in diag


@pytest.mark.asyncio
async def test_compat_unreachable_never_raises():
    with patch("httpx.AsyncClient.get", side_effect=Exception("gateway down")):
        diag = await memory_bridge.check_gateway_compat()
    assert diag["reachable"] is False
    assert diag["compat"] == "unknown"


@pytest.mark.asyncio
async def test_save_artifact_returns_error_on_401():
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        result = await memory_bridge.save_artifact(MOCK_CONTENT, '{"source":"test"}')
    assert result["status"] == "error"
    assert "token" in result["message"].lower() or "AGENT_TOKEN" in result["message"]


@pytest.mark.asyncio
async def test_search_and_rerank_returns_error_on_401():
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        result = await memory_bridge.search_and_rerank(MOCK_QUERY)
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert "token" in result["message"].lower() or "AGENT_TOKEN" in result["message"]


# ── status / telemetry rendering ──────────────────────────────────────────────

def test_format_status_renders_sections():
    """format_status turns a telemetry payload into a compact readable report."""
    payload = {
        "status": "success",
        "telemetry": {
            "timestamp": "2026-06-09T20:00:00+00:00",
            "postgres": {
                "technical_docs": 171,
                "outbox": {"applied": 10, "rem_reviewed": 3},
                "community_summaries": {"total": 2, "superseded": 0, "insight": 0},
            },
            "neo4j": {
                "facts_total": 97, "facts_rem_pending": 1, "facts_unconsolidated": 20,
                "decisions_total": 75, "decisions_rem_pending": 71,
            },
        },
    }
    out = memory_bridge.format_status(payload)
    assert "technical_docs:      171" in out
    assert "applied" in out and "rem_reviewed" in out
    assert "decisions: 75 total" in out
    assert "REM pending 71" in out


def test_format_status_passes_through_errors():
    err = {"status": "error", "message": "Coordinator rejected token."}
    assert "rejected token" in memory_bridge.format_status(err)


def test_format_status_names_the_stalled_type_and_the_successful_one():
    """The reporting defect this guards: a headline reading "STALLED, last
    success 456107s ago" while a sibling cycle type folded 4 hours ago. The
    render must name WHICH type is stalled and which the age belongs to."""
    payload = {"status": "success", "telemetry": {"consolidation": {
        "stalled": True,
        "stalled_types": ["insight"],
        "last_outcome": "completed",
        "last_success_age_seconds": 14863,
        "last_success_cycle_type": "fact_consolidation",
        "insight": {"last_outcome": "completed", "stalled": True,
                    "runs_24h": 16, "cycle_seconds_avg": 0.1,
                    "folds_succeeded_24h": 0, "folds_attempted_24h": 0},
        "fact_consolidation": {"last_outcome": "completed", "stalled": False,
                               "runs_24h": 39, "cycle_seconds_avg": 192.4,
                               "folds_succeeded_24h": 17, "folds_attempted_24h": 69},
    }}}
    out = memory_bridge.format_status(payload)
    assert "STALLED ⚠ [insight]" in out
    assert "14863s ago (fact_consolidation)" in out
    # Per-type cost + throughput must both render — this is what prices a slot.
    assert "16 runs/24h avg 0.1s, folds 0/0" in out
    assert "39 runs/24h avg 192.4s, folds 17/69" in out


def test_format_status_falls_back_when_gateway_predates_stalled_types():
    """An older gateway sends `stalled` with no `stalled_types`. The client must
    still report the stall rather than silently rendering it as ok."""
    out = memory_bridge.format_status(
        {"status": "success", "telemetry": {"consolidation": {
            "stalled": True, "last_outcome": "completed",
            "last_success_age_seconds": 99}}})
    assert "STALLED ⚠" in out
    assert "99s ago" in out


def test_tracked_client_copies_are_byte_identical():
    """The client ships as TWO tracked files — the development source under the
    server tree and the skill copy agents install — kept in agreement only by
    sync_skills.sh. This suite imports the SKILL COPY, so an edit to the source
    that was never synced would be validated against the stale file and pass,
    reporting coverage for code that is not the code under test. (Found by three
    mutations surviving against the source while dying instantly against the
    copy.) Fail loudly here instead: if this test fails, run sync_skills.sh.
    """
    root = os.path.join(os.path.dirname(__file__), "..")
    source = os.path.join(root, "shared-memory", "scripts", "memory_bridge.py")
    shipped = os.path.join(root, "shared-memory-skill", "shared-memory",
                           "scripts", "memory_bridge.py")
    with open(source, "rb") as f_src, open(shipped, "rb") as f_ship:
        assert f_src.read() == f_ship.read(), (
            "memory_bridge.py copies have diverged — the tests import the skill "
            "copy, so the source edit is UNTESTED. Run: bash "
            "shared-memory/scripts/sync_skills.sh"
        )
