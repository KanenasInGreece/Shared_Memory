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
async def test_get_embedding_success():
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"embedding": MOCK_EMBEDDING}]}
        )
        embedding = await memory_bridge.get_embedding("hello")
        assert embedding == MOCK_EMBEDDING

@pytest.mark.asyncio
async def test_get_embedding_failure():
    with patch("httpx.AsyncClient.post", side_effect=Exception("Down")):
        embedding = await memory_bridge.get_embedding("hello")
        assert embedding is None

@pytest.mark.asyncio
async def test_save_artifact_hard_mandate_fail():
    # Test that saving is blocked when embedding service is down
    with patch("memory_bridge.get_embedding", return_value=None):
        result = await memory_bridge.save_artifact(MOCK_CONTENT)
        assert result["status"] == "error"
        assert "CRITICAL" in result["message"]

@pytest.mark.asyncio
async def test_save_artifact_success():
    # Mock embedding, pg connection, and neo4j driver
    with patch("memory_bridge.get_embedding", return_value=MOCK_EMBEDDING), \
         patch("psycopg2.connect") as mock_pg_conn, \
         patch("neo4j.GraphDatabase.driver") as mock_neo4j:
        
        # Setup Postgres mock
        mock_cur = mock_pg_conn.return_value.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = [MOCK_PG_ID]
        
        # Setup Neo4j mock
        mock_session = mock_neo4j.return_value.session.return_value.__enter__.return_value
        
        result = await memory_bridge.save_artifact(MOCK_CONTENT)
        
        assert result["status"] == "success"
        assert f"ID {MOCK_PG_ID}" in result["message"]
        assert "Linked to Neo4j" in result["message"]
        
        # Verify pg insert was called
        mock_cur.execute.assert_called()
        # Verify neo4j merge was called
        mock_session.run.assert_called()

@pytest.mark.asyncio
async def test_search_and_rerank_full_success():
    # Test search with reranking and graph expansion
    with patch("memory_bridge.get_embedding", return_value=MOCK_EMBEDDING), \
         patch("psycopg2.connect") as mock_pg_conn, \
         patch("httpx.AsyncClient.post") as mock_rerank_post, \
         patch("neo4j.GraphDatabase.driver") as mock_neo4j:
        
        # Mock Postgres candidates
        mock_cur = mock_pg_conn.return_value.cursor.return_value.__enter__.return_value
        mock_cur.fetchall.return_value = [(MOCK_PG_ID, MOCK_CONTENT, {"source": "test"})]
        
        # Mock Reranker response
        mock_rerank_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"results": [{"index": 0, "relevance_score": 0.95}]}
        )
        
        # Mock Neo4j expansion
        mock_session = mock_neo4j.return_value.session.return_value.__enter__.return_value
        mock_session.run.return_value = [
            {"labels": ["Entity"], "name": "RelatedNode", "rel_type": "KNOWS"}
        ]
        
        results = await memory_bridge.search_and_rerank(MOCK_QUERY)
        
        assert len(results) == 1
        assert results[0]["content"] == MOCK_CONTENT
        assert results[0]["score"] == 0.95
        assert "KNOWS -> RelatedNode" in results[0]["graph_context"]

@pytest.mark.asyncio
async def test_search_and_rerank_keyword_fallback():
    # Test fallback to keyword search when retriever is down
    with patch("memory_bridge.get_embedding", return_value=None), \
         patch("psycopg2.connect") as mock_pg_conn:
        
        mock_cur = mock_pg_conn.return_value.cursor.return_value.__enter__.return_value
        # Mock 3 columns: id, content, metadata
        mock_cur.fetchall.return_value = [(MOCK_PG_ID, MOCK_CONTENT, {"source": "fallback"})]
        
        results = await memory_bridge.search_and_rerank(MOCK_QUERY)
        
        assert len(results) == 1
        assert "Keyword search fallback" in results[0]["note"]
