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
async def test_thin_client_owns_no_database_handles():
    """The whole point of the re-cut. This MCP server used to run its own copy of
    the retrieval chain against Postgres and Neo4j — which meant a second
    implementation of the read path, and therefore a second implementation of its
    ACCESS CONTROL that simply did not have any: the gateway filters every read on
    `visibility` (global / own private / matching scope), a direct
    `SELECT ... WHERE NOT superseded` filters on none. It also drifted, and it
    imported server-side modules into a client.

    If any of these names come back, that whole class of defect comes back with
    them."""
    for name in ("get_pg_conn", "release_pg_conn", "get_neo4j",
                 "_graph_entity_fallback", "get_embedding",
                 "DB_CONN", "NEO4J_URI", "NEO4J_AUTH"):
        assert not hasattr(vector_skill, name), (
            f"{name} is back — vector-skill must reach memory only through the gateway")
    # Scan CODE only — the module docstring narrates the removed design on
    # purpose, and a prose mention of it is the opposite of a regression.
    src = open(os.path.join(os.path.dirname(__file__), "..", "vector-skill.py")).read()
    code = "\n".join(l for l in src.splitlines()
                     if l.strip() and not l.startswith(("#", " ", "\t")) or
                     l.lstrip().startswith(("import ", "from ", "sys.path")))
    for banned in ("import psycopg2", "from neo4j import",
                   "from ontology import", "sys.path.insert"):
        assert banned not in code, f"{banned!r} must not appear in a thin client"


@pytest.mark.asyncio
async def test_client_speaks_the_gateway_wire_version():
    """A stale API_VERSION makes the gateway log skew on every single request."""
    bridge = open(os.path.join(os.path.dirname(__file__), "..",
                               "shared-memory", "scripts", "memory_bridge.py")).read()
    expected = int(next(l for l in bridge.splitlines()
                        if l.startswith("API_VERSION")).split("=")[1])
    assert vector_skill.API_VERSION == expected


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
async def test_mcp_hybrid_search_goes_through_the_gateway():
    """Search delegates the entire chain — embedding, vector search, reranking,
    graph expansion AND read authorization — to POST /memory/search, and renders
    what comes back. The qualified `ref` is surfaced verbatim rather than reduced
    to a bare integer, because a bare integer taken off a summary result resolves
    against the facts table (decision 822)."""
    payload = {"results": [
        {"pg_id": 87, "ref": "summary:87", "record_type": "summary", "tier": 3,
         "content": "Global summary text", "source_pg_ids": [1, 2], "score": None},
        {"pg_id": 92, "ref": "insight:92", "record_type": "insight", "tier": 3,
         "content": "Cross-project principle", "source_pg_ids": [3], "score": None},
        {"pg_id": MOCK_PG_ID, "ref": f"fact:{MOCK_PG_ID}", "record_type": "fact",
         "tier": 1, "content": MOCK_CONTENT, "metadata": {"source": "mcp"},
         "score": 0.88, "graph_context": "BELONGS_TO -> SharedMem"},
    ]}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)

    call = mock_post.call_args
    assert call.args[0].endswith("/memory/search")
    assert call.kwargs["json"]["query"] == MOCK_QUERY
    # rendered
    assert "Global Context Summary" in result and "Global summary text" in result
    assert "Insight (cross-project principle)" in result
    assert "Unified Memory Results" in result
    assert "Score: 0.88" in result
    assert "BELONGS_TO -> SharedMem" in result
    # qualified refs surfaced, never a bare id for a summary
    assert "summary:87" in result and f"fact:{MOCK_PG_ID}" in result


@pytest.mark.asyncio
async def test_mcp_hybrid_search_reports_a_down_gateway_plainly():
    """The gateway is now the only path to memory, so an outage is a hard failure.
    Saying so beats returning an empty result set that reads as 'nothing found'."""
    with patch("httpx.AsyncClient.post", side_effect=RuntimeError("connection refused")):
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)
    assert "unreachable" in result.lower()
    assert "hive-mind-gateway" in result


@pytest.mark.asyncio
async def test_mcp_archive_reasoning_trace_saves_a_record():
    """It used to CREATE ReasoningTrace/ReasoningStep nodes straight in Neo4j,
    which bypasses the outbox (the thing that makes a save atomic across both
    stores) and bypasses read authorization — durable in one store, visible to
    everyone. Now it is an ordinary record on the ordinary save path."""
    mock_response = MagicMock(status_code=200, json=lambda: {
        "status": "success", "pg_id": MOCK_PG_ID, "neo4j": "pending", "message": "ok"})
    steps = [{"thought": "research", "tool": "grep", "result": "found"}]
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.archive_reasoning_trace("sess_1", "test task", steps)

    assert "Success" in result
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/save")
    meta = call.kwargs["json"]["metadata"]
    assert meta["type"] == "reasoning_trace"
    assert meta["session_id"] == "sess_1"
    assert meta["step_count"] == 1
    assert "research" in call.kwargs["json"]["content"]


@pytest.mark.asyncio
async def test_mcp_archive_reasoning_trace_rejects_empty():
    result = await vector_skill.archive_reasoning_trace("sess_1", "t", [])
    assert "Error" in result


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
async def test_mcp_check_memory_health_asks_the_gateway():
    """Health is what the gateway reports — daemons, backends, consolidation
    liveness — not a row count from a database handle this client should not
    hold. It is also the only check that exercises the path the client uses."""
    gw = {"status": "ok", "version": "0.7.7", "api_version": 3,
          "daemon": "running", "rem_daemon": "running", "embedder": "ok"}
    mock_response = MagicMock(status_code=200, json=lambda: gw)
    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        result = json.loads(await vector_skill.check_memory_health())

    assert mock_get.call_args.args[0].endswith("/health")
    assert result["status"] == "ok"
    assert result["client"]["version"] == vector_skill.VERSION
    assert "version_skew" not in result["client"]


@pytest.mark.asyncio
async def test_mcp_check_memory_health_names_version_skew():
    gw = {"status": "ok", "api_version": 99}
    mock_response = MagicMock(status_code=200, json=lambda: gw)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = json.loads(await vector_skill.check_memory_health())
    assert "version_skew" in result["client"]


@pytest.mark.asyncio
async def test_mcp_check_memory_health_unreachable_gateway():
    with patch("httpx.AsyncClient.get", side_effect=RuntimeError("refused")):
        result = json.loads(await vector_skill.check_memory_health())
    assert result["status"] == "unreachable"


@pytest.mark.asyncio
async def test_mcp_record_lineage_requires_a_valid_ref():
    """A bare id is accepted for compatibility; a malformed or wrongly-typed
    qualified ref is refused before it can resolve against the wrong table."""
    bad = await vector_skill.record_lineage("summary:notanumber")
    assert "Error" in bad
    bad2 = await vector_skill.record_lineage("widget:12")
    assert "Error" in bad2

    mock_response = MagicMock(status_code=200, json=lambda: {"pg_id": 87, "exists": True})
    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        ok = await vector_skill.record_lineage("summary:87")
    assert mock_get.call_args.args[0].endswith("/memory/status/summary:87")
    assert json.loads(ok)["exists"] is True


@pytest.mark.asyncio
async def test_mcp_review_edges_validates_family():
    bad = await vector_skill.review_edges("not_a_family")
    assert "Error" in bad
    assert vector_skill.RELATION_FAMILIES == ("entity_relation", "evidential")


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
