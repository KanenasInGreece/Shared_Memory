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
    with patch("vector_skill.get_embedding", return_value=MOCK_EMBEDDING), \
         patch("vector_skill.get_pg_conn") as mock_pg_conn, \
         patch("vector_skill.release_pg_conn") as mock_pg_release, \
         patch("vector_skill.get_neo4j") as mock_neo4j:
        
        # Postgres mock
        mock_cur = mock_pg_conn.return_value.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = [MOCK_PG_ID]
        
        # Neo4j mock
        mock_session = mock_neo4j.return_value.session.return_value.__enter__.return_value
        
        result = await vector_skill.save_artifact(MOCK_CONTENT)
        
        assert "Success" in result
        assert "linked to Graph" in result
        mock_cur.execute.assert_called()
        # Verify idempotency clause present in one of the execute calls
        all_sql = [call.args[0] for call in mock_cur.execute.call_args_list]
        assert any("ON CONFLICT (content_hash)" in sql for sql in all_sql)
        assert any("pg_notify" in sql for sql in all_sql)
        mock_session.run.assert_called()
        mock_pg_release.assert_called()

@pytest.mark.asyncio
async def test_mcp_save_artifact_abort_on_no_vector():
    with patch("vector_skill.get_embedding", return_value=None):
        result = await vector_skill.save_artifact(MOCK_CONTENT)
        assert "Error" in result
        assert "DOWN" in result

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
