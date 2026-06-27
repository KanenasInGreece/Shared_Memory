import pytest
import json
import asyncio
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch, AsyncMock

# Dynamic load of vector-skill.py
def load_vector_skill():
    path = os.path.join(os.path.dirname(__file__), "..", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vector_skill"] = module
    spec.loader.exec_module(module)
    return module

vector_skill = load_vector_skill()

# Mock data
MOCK_EMBEDDING = [0.1] * 1024
MOCK_PG_ID = 42
MOCK_CONTENT = "MCP test content"
MOCK_QUERY = "MCP test query"

@pytest.mark.asyncio
async def test_mcp_get_embedding_success():
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"embedding": MOCK_EMBEDDING}]}
        )
        embedding = await vector_skill.get_embedding("hello")
        assert embedding == MOCK_EMBEDDING

@pytest.mark.asyncio
async def test_mcp_save_artifact_success():
    """save_artifact routes through the coordinator (POST /memory/save) — no direct
    Postgres/Neo4j writes — and returns the pg_id from the gateway response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "pg_id": MOCK_PG_ID, "neo4j": "pending",
        "message": f"Artifact stored with ID {MOCK_PG_ID}.",
    }

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.save_artifact(
            MOCK_CONTENT, '{"source":"qwen3-27b","entities":["TestEntity"]}'
        )

    assert "Success" in result
    assert f"pg_id={MOCK_PG_ID}" in result
    # Routed to the gateway save endpoint with metadata as an OBJECT (the codec
    # serialises once — a stringified metadata here would double-encode).
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/save")
    payload = call.kwargs["json"]
    assert isinstance(payload["metadata"], dict)
    assert payload["metadata"]["entities"] == ["TestEntity"]
    # Loaded model name preserved even though auth may overwrite source.
    assert payload["metadata"]["model"] == "qwen3-27b"


@pytest.mark.asyncio
async def test_mcp_save_artifact_gateway_down():
    """save_artifact returns a readable error when the gateway is unreachable."""
    with patch("httpx.AsyncClient.post", side_effect=Exception("connection refused")):
        result = await vector_skill.save_artifact(
            MOCK_CONTENT, '{"source":"qwen3-27b"}'
        )
    assert "Error" in result
    assert "hive_mind_proxy.py" in result


@pytest.mark.asyncio
async def test_mcp_save_artifact_surfaces_coordinator_error():
    """A 503 (embedder down) or other coordinator rejection is surfaced verbatim."""
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.json = lambda: {
        "status": "error",
        "message": "Embedding service unreachable after 4 attempts.",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.save_artifact(
            MOCK_CONTENT, '{"source":"qwen3-27b"}'
        )
    assert "Error" in result
    assert "Embedding service unreachable" in result


@pytest.mark.asyncio
async def test_mcp_save_artifact_missing_source_rejected_client_side():
    """source is required — rejected before any network call."""
    with patch("httpx.AsyncClient.post") as mock_post:
        result = await vector_skill.save_artifact(MOCK_CONTENT, '{"entities":["X"]}')
    assert "Error" in result
    assert "source is required" in result
    mock_post.assert_not_called()

@pytest.mark.asyncio
async def test_mcp_hybrid_search_expanded_success():
    with patch("vector_skill.get_embedding", return_value=MOCK_EMBEDDING), \
         patch("vector_skill.get_pg_conn") as mock_pg_conn, \
         patch("vector_skill.release_pg_conn") as mock_pg_release, \
         patch("httpx.AsyncClient.post") as mock_rerank_post, \
         patch("vector_skill.get_neo4j") as mock_neo4j:
        
        # Postgres mock for both summaries and candidates
        mock_cur = mock_pg_conn.return_value.cursor.return_value.__enter__.return_value
        # First call for summary, second for candidates
        mock_cur.fetchone.return_value = ["Global summary text"]
        mock_cur.fetchall.return_value = [(MOCK_PG_ID, MOCK_CONTENT, {"source": "mcp"})]
        
        # Reranker
        mock_rerank_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"results": [{"index": 0, "relevance_score": 0.88}]}
        )
        
        # Neo4j Expansion
        mock_session = mock_neo4j.return_value.session.return_value.__enter__.return_value
        mock_session.run.return_value = [
            {"labels": ["Project"], "name": "SharedMem", "rel_type": "BELONGS_TO"}
        ]
        
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)
        
        assert "Global Context Summary" in result
        assert "Global summary text" in result
        assert "Unified Memory Results" in result
        assert "Score: 0.88" in result
        assert "BELONGS_TO -> SharedMem" in result
        assert mock_pg_release.call_count >= 2

@pytest.mark.asyncio
async def test_mcp_archive_reasoning_trace_success():
    with patch("vector_skill.get_embedding", return_value=MOCK_EMBEDDING), \
         patch("vector_skill.get_neo4j") as mock_neo4j:
        
        mock_session = mock_neo4j.return_value.session.return_value.__enter__.return_value
        
        steps = [{"thought": "research", "tool": "grep", "result": "found"}]
        result = await vector_skill.archive_reasoning_trace("sess_1", "test task", steps)
        
        assert "Success" in result
        assert "1 steps" in result
        # Check that MERGE/CREATE were called for root and steps
        assert mock_session.run.call_count >= 2

@pytest.mark.asyncio
async def test_mcp_save_decision_success():
    """save_decision routes through coordinator and returns pg_id on success."""
    mock_response = MagicMock()
    mock_response.json = lambda: {"status": "success", "pg_id": 77}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.save_decision(
            title="Use asyncpg over psycopg2",
            decided_by="Xenofon",
            project="shared-memory",
            rationale="asyncpg does not block the event loop",
            source="qwen3-30b",
            assisted_by="claude-sonnet-4-6",
            confidence="high",
            entities="asyncpg,PostgreSQL",
        )

    assert "pg_id=77" in result
    assert "Use asyncpg over psycopg2" in result
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    assert payload["metadata"]["type"] == "decision"
    assert payload["metadata"]["decision"]["decided_by"] == "Xenofon"
    assert payload["metadata"]["decision"]["confidence"] == "high"
    assert "asyncpg" in payload["metadata"]["entities"]


@pytest.mark.asyncio
async def test_mcp_save_decision_coordinator_down():
    """save_decision returns a readable error when the coordinator is unreachable."""
    with patch("httpx.AsyncClient.post", side_effect=Exception("connection refused")):
        result = await vector_skill.save_decision(
            title="T", decided_by="X", project="P",
            rationale="R", source="test-model",
        )
    assert "Error" in result
    assert "hive_mind_proxy.py" in result


@pytest.mark.asyncio
async def test_mcp_save_decision_coordinator_returns_400():
    """save_decision surfaces coordinator error messages (e.g. missing required fields)."""
    mock_response = MagicMock()
    mock_response.json = lambda: {
        "status": "error",
        "message": "decision save missing required fields: ['rationale']",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.save_decision(
            title="T", decided_by="X", project="P",
            rationale="", source="test-model",
        )
    assert "Error" in result
    assert "rationale" in result


@pytest.mark.asyncio
async def test_mcp_check_memory_health():
    with patch("vector_skill.get_pg_conn") as mock_pg_conn, \
         patch("vector_skill.release_pg_conn") as mock_pg_release, \
         patch("httpx.AsyncClient.post") as mock_http_post:
        
        # Postgres OK
        mock_cur = mock_pg_conn.return_value.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = [100]
        
        # APIs OK
        mock_http_post.return_value = MagicMock(status_code=200)
        
        result_str = await vector_skill.check_memory_health()
        result = json.loads(result_str)
        
        assert result["status"] == "healthy"
        assert result["components"]["postgres"]["docs"] == 100
        assert result["components"]["retriever"]["status"] == "OK"
        mock_pg_release.assert_called()


# ── supersede / review_hold MCP tools (fact supersession, decision 381/384) ──

@pytest.mark.asyncio
async def test_mcp_supersede_bare_retract():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "superseded": 7, "superseded_by": None,
        "purged_outbox": 1, "message": "Fact 7 superseded (retracted, no replacement).",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.supersede(7)
    assert "superseded" in result.lower()
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/supersede")
    assert call.kwargs["json"] == {"pg_id": 7}          # no `by` when omitted


@pytest.mark.asyncio
async def test_mcp_supersede_with_successor():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "superseded": 7, "superseded_by": 9,
        "purged_outbox": 0, "message": "Fact 7 superseded by 9.",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.supersede(7, by=9)
    assert "9" in result
    assert mock_post.call_args.kwargs["json"] == {"pg_id": 7, "by": 9}


@pytest.mark.asyncio
async def test_mcp_supersede_surfaces_coordinator_error():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "error", "message": "fact 7 is already superseded"}
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.supersede(7)
    assert "Error" in result and "already superseded" in result


@pytest.mark.asyncio
async def test_mcp_review_hold():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "summary_id": 3, "reviewed": {"old": 5, "by": 6},
        "message": "Summary 3: supersession of 5 marked reviewed-and-held.",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.review_hold(3, 5)
    assert "reviewed-and-held" in result
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/review_hold")
    assert call.kwargs["json"] == {"summary_id": 3, "pg_id": 5}
