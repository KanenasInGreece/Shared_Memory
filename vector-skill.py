import httpx
import psycopg2
from psycopg2 import pool
import json
import os
import logging
import asyncio
import hashlib
from datetime import datetime
from fastmcp import FastMCP
from neo4j import GraphDatabase

# Configure logging to stderr for MCP visibility
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RAG_Orchestrator")

mcp = FastMCP("Local_RAG_Orchestrator")

# Standardized endpoints via Hive-Mind Gateway (8888)
RETRIEVER_URL = "http://localhost:8888/v1/embeddings"
RERANKER_URL = "http://localhost:8888/v1/reranking"
_pg_pass = os.environ.get("PG_PASSWORD", "")
DB_CONN = os.environ.get(
    "PG_CONN",
    f"dbname=agent_data user=postgres password={_pg_pass} host=localhost"
)
NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", os.environ.get("NEO4J_PASSWORD", ""))

# Global timeout configuration - balanced for local inference
# Increased to 20s to handle heavy BGE-M3 processing on AMD/Intel stack
TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# Global Drivers
_neo4j_driver = None
_pg_pool = None

def get_neo4j():
    global _neo4j_driver
    if _neo4j_driver is None:
        _neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    return _neo4j_driver

def get_pg_conn():
    global _pg_pool
    if _pg_pool is None:
        # ThreadedConnectionPool is mandatory for multi-agent MCP servers
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, DB_CONN)
    return _pg_pool.getconn()

def release_pg_conn(conn):
    _pg_pool.putconn(conn)

async def get_embedding(text: str):
    """Internal helper to get embeddings with retry logic."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            headers = {"Authorization": "Bearer none"}
            resp = await client.post(
                RETRIEVER_URL,
                json={"input": text, "model": "bge-m3"},
                headers=headers
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"Embedding service unreachable or slow: {str(e)}")
        return None

@mcp.tool()
async def hybrid_search_and_rerank(query: str, limit: int = 5) -> str:
    """
    Performs semantic search with Global Context (Summaries) and Relational Expansion (Neo4j).
    Links Postgres technical docs with Neo4j entity relationships and hierarchical summaries.
    """
    logger.info(f"Starting expanded search for: {query[:50]}...")
    start_time = datetime.now()

    try:
        # 1. Generate Embedding
        query_vector = await get_embedding(query)
        if not query_vector:
            return "Error: Embedding service down. Cannot perform high-precision search."

        # 2. Global Context Search (Postgres community_summaries)
        global_context = ""
        conn = get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT content FROM community_summaries
                    ORDER BY embedding <=> %s::vector LIMIT 1
                """, (query_vector,))
                g_row = cur.fetchone()
                if g_row:
                    global_context = f"### Global Context Summary\n{g_row[0]}\n\n---\n\n"
        except Exception as e:
            logger.warning(f"Global context retrieval failed: {str(e)}")
        finally:
            release_pg_conn(conn)

        # 3. Vector Search (Postgres technical_docs)
        conn = get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, content, metadata FROM technical_docs
                    ORDER BY embedding <=> %s::vector LIMIT 20
                """, (query_vector,))
                rows = cur.fetchall()
                ids = [row[0] for row in rows]
                candidates = [row[1] for row in rows]
                meta = [row[2] for row in rows]
        finally:
            release_pg_conn(conn)

        if not candidates:
            return "Result: No relevant documentation found."

        # 4. Rerank
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(RERANKER_URL, json={
                "query": query,
                "documents": candidates,
                "top_k": limit
            })
            resp.raise_for_status()
            rerank_results = resp.json()["results"]

        # 5. Relational Expansion (Neo4j)
        driver = get_neo4j()
        output_docs = []
        for res in rerank_results:
            idx = res["index"]
            score = res["relevance_score"]
            pg_id = ids[idx]
            content = candidates[idx]
            m = meta[idx]

            relational_context = ""
            try:
                with driver.session() as session:
                    graph_result = session.run("""
                        MATCH (f:Fact {pg_id: $pg_id})
                        OPTIONAL MATCH (f)-[r]-(related)
                        RETURN labels(related) as labels, related.name as name, type(r) as rel_type
                        LIMIT 5
                    """, pg_id=pg_id)

                    rels = []
                    for record in graph_result:
                        if record["name"]:
                            rels.append(f"{record['rel_type']} -> {record['name']} ({record['labels'][0]})")

                    if rels:
                        relational_context = "\n[Graph Context]: " + " | ".join(rels)
            except Exception as ge:
                logger.warning(f"Neo4j expansion failed for ID {pg_id}: {str(ge)}")

            source = m.get("source", "unknown") if isinstance(m, dict) else "unknown"
            output_docs.append(f"[Score: {score:.2f} | Source: {source}]{relational_context}\n{content}")

        duration = (datetime.now() - start_time).total_seconds()
        header = f"### Unified Memory Results ({len(output_docs)} items found in {duration:.2f}s)\n\n"
        return global_context + header + "\n\n---\n\n".join(output_docs)

    except Exception as e:
        logger.error(f"Search failed: {str(e)}")
        return f"Error: {str(e)}"

@mcp.tool()
async def save_artifact(content: str, metadata_json: str = "{}") -> str:
    """
    Stores artifact in both Postgres and Neo4j.
    Hard Mandate: Aborts if embedding service is down.
    Idempotent: Reuses existing ID if content is identical.
    """
    try:
        embedding = await get_embedding(content)
        if not embedding:
            return "Error: Hive-Mind Gateway (8888) is DOWN. Save aborted to protect memory integrity. Start hive_mind_proxy.py first."

        try:
            m_data = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
        except:
            m_data = {"raw_metadata": metadata_json}

        m_data["timestamp"] = datetime.now().isoformat()

        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # 1. Postgres Save (Idempotent Upsert)
        daemon_up = True
        conn = get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO technical_docs (content, metadata, embedding, content_hash)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (content_hash)
                    DO UPDATE SET metadata = EXCLUDED.metadata
                    RETURNING id
                """, (content, json.dumps(m_data), embedding, content_hash))
                pg_id = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'consolidation_daemon'")
                daemon_up = cur.fetchone()[0] > 0
                cur.execute("SELECT pg_notify('new_artifact', %s)", (json.dumps({"pg_id": pg_id}),))
            conn.commit()
        finally:
            release_pg_conn(conn)

        # 2. Neo4j Sync (agent-memory schema)
        try:
            driver = get_neo4j()
            with driver.session() as session:
                session.run("""
                    MERGE (f:Fact {pg_id: $pg_id})
                    SET f.content = $content,
                        f.embedding = $embedding,
                        f.created_at = datetime(),
                        f.source = $source
                """, pg_id=pg_id, content=content[:200], embedding=embedding, source=m_data.get("source", "mcp_sync"))

                entities = m_data.get("entities", [])
                for entity_name in entities:
                    session.run("""
                        MATCH (f:Fact {pg_id: $pg_id})
                        MERGE (e:Entity {name: $name})
                        MERGE (f)-[:MENTIONS]->(e)
                    """, pg_id=pg_id, name=entity_name)

            sync_msg = f"Successfully linked to Graph (Neo4j){f' with {len(entities)} entities' if entities else ''}."
        except Exception as ne:
            sync_msg = f"Postgres saved (ID: {pg_id}), but Graph sync failed: {str(ne)}"

        daemon_warning = "" if daemon_up else "\nWARNING: Consolidation daemon not running — NOTIFY dropped. Start consolidation_loop.py."
        return f"Success: {sync_msg}{daemon_warning}"
    except Exception as e:
        logger.error(f"Save failed: {str(e)}")
        return f"Error saving artifact: {str(e)}"

@mcp.tool()
async def archive_reasoning_trace(session_id: str, task: str, steps: list) -> str:
    """
    Archives the agent's reasoning path as a linked graph chain in Neo4j.
    Steps should be a list of dicts: [{'thought': '...', 'tool': '...', 'result': '...'}]
    """
    try:
        task_embedding = await get_embedding(task)

        driver = get_neo4j()
        with driver.session() as session:
            session.run("""
                MERGE (t:ReasoningTrace {id: $session_id})
                SET t.task = $task,
                    t.task_embedding = $embedding,
                    t.timestamp = datetime()
            """, session_id=session_id, task=task, embedding=task_embedding)

            prev_id = session_id
            for i, step in enumerate(steps):
                step_id = f"{session_id}_step_{i}"
                content = f"Thought: {step.get('thought', '')}\nTool: {step.get('tool', '')}"

                step_embedding = await get_embedding(content)

                session.run("""
                    MATCH (prev) WHERE prev.id = $prev_id
                    CREATE (s:ReasoningStep {id: $step_id})
                    SET s.content = $content,
                        s.result = $result,
                        s.embedding = $embedding,
                        s.index = $i
                    CREATE (prev)-[:NEXT_STEP]->(s)
                """, prev_id=prev_id, step_id=step_id, content=content,
                   result=str(step.get('result', '')), embedding=step_embedding, i=i)

                prev_id = step_id

        logger.info(f"Trace archived for session {session_id}")
        return f"Success: Archived reasoning trace with {len(steps)} steps."
    except Exception as e:
        logger.error(f"Trace archive failed: {str(e)}")
        return f"Error: {str(e)}"

@mcp.tool()
async def check_memory_health() -> str:
    """Full-stack diagnostic for the Shared Memory infrastructure."""
    stats = {"status": "healthy", "components": {}}

    # Check Postgres
    try:
        conn = get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM technical_docs")
                count = cur.fetchone()[0]
                stats["components"]["postgres"] = {"status": "OK", "docs": count}
        finally:
            release_pg_conn(conn)
    except Exception as e:
        stats["status"] = "degraded"
        stats["components"]["postgres"] = {"status": "FAIL", "error": str(e)}

    # Check Retriever (via Gateway)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(RETRIEVER_URL, json={"input": "healthcheck", "model": "bge-m3"})
            stats["components"]["retriever"] = {"status": "OK" if resp.status_code == 200 else "FAIL"}
    except Exception as e:
        stats["status"] = "degraded"
        stats["components"]["retriever"] = {"status": "FAIL", "error": str(e)}

    # Check Reranker (via Gateway)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(RERANKER_URL, json={"query": "health", "documents": ["check"]})
            stats["components"]["reranker"] = {"status": "OK" if resp.status_code == 200 else "FAIL"}
    except Exception as e:
        stats["status"] = "degraded"
        stats["components"]["reranker"] = {"status": "FAIL", "error": str(e)}

    return json.dumps(stats, indent=2)

if __name__ == "__main__":
    mcp.run()
