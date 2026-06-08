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
    assert memory_bridge._request_headers() == _VER


# ── Version contract — check_gateway_compat ───────────────────────────────────

def _health(payload):
    return MagicMock(json=lambda: payload)


@pytest.mark.asyncio
async def test_compat_ok_when_versions_match():
    payload = {"status": "ok", "version": "0.4.1", "api_version": memory_bridge.API_VERSION}
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
