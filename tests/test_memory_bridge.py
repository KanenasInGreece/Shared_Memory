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


# ── Auth headers — Phase 2C ───────────────────────────────────────────────────

def test_auth_headers_returns_empty_when_no_token(monkeypatch):
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    assert memory_bridge._auth_headers() == {}


def test_auth_headers_returns_bearer_header_when_token_set(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "tok_testtoken123")
    headers = memory_bridge._auth_headers()
    assert headers == {"Authorization": "Bearer tok_testtoken123"}


def test_auth_headers_strips_whitespace(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "  tok_abc  ")
    headers = memory_bridge._auth_headers()
    assert headers == {"Authorization": "Bearer tok_abc"}


def test_auth_headers_empty_string_returns_empty(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "")
    assert memory_bridge._auth_headers() == {}


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
