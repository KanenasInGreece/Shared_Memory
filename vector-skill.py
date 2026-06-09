import httpx
import psycopg2
from psycopg2 import pool
import json
import os
import sys
import logging
import asyncio
from datetime import datetime
from fastmcp import FastMCP
from neo4j import GraphDatabase

# Load .env from the same directory as this script so credentials are available
# when LM Studio (or any MCP host) spawns this process without inheriting the shell env.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # python-dotenv not installed; rely on env vars being set externally

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "shared-memory", "scripts"))
from ontology import ONT

# Configure logging to stderr for MCP visibility
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RAG_Orchestrator")

mcp = FastMCP("Local_RAG_Orchestrator")

# Standardized endpoints via Hive-Mind Gateway (8888)
RETRIEVER_URL = "http://localhost:8888/v1/embeddings"
RERANKER_URL = "http://localhost:8888/v1/reranking"

# Wire contract this MCP server speaks on its /memory/* gateway calls. Keep in
# step with API_VERSION in coordinator.py / memory_bridge.py — the gateway logs
# a warning (coordinator._check_client_version) if they disagree.
API_VERSION = 1
CLIENT_VERSION_HEADER = "X-SM-Api-Version"


def _auth_headers() -> dict:
    """Headers for every coordinator request.

    Advertises this server's API_VERSION so the gateway can log version skew,
    and adds the Bearer token when AGENT_TOKEN is set.
    """
    headers = {CLIENT_VERSION_HEADER: str(API_VERSION)}
    token = os.environ.get("AGENT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

_CONTENT_SIZE_WARN_BYTES = 10 * 1024

def _append_log(tool: str, min_level: int, event: str, data: dict, content: str = None) -> None:
    log_level = int(os.environ.get("MEMORY_LOG_LEVEL", "0"))
    if log_level < min_level:
        return
    log_dir = os.path.expanduser(os.environ.get("MEMORY_LOG_PATH", "~/.shared-memory/logs"))
    try:
        os.makedirs(log_dir, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(), "tool": tool, "event": event, **data}
        if log_level >= 4 and content is not None:
            entry["content"] = content
            if len(content.encode()) > _CONTENT_SIZE_WARN_BYTES:
                entry["content_size_warn"] = f"content is {len(content.encode())} bytes — reduce log level to avoid large logs"
        with open(os.path.join(log_dir, f"{tool}.log"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        import sys
        print(f"[WARN] shared-memory: audit log unavailable ({e})", file=sys.stderr)
    except Exception:
        pass  # logging must never break the save path
_pg_pass = os.environ.get("PG_PASSWORD", "")
DB_CONN = os.environ.get(
    "PG_CONN",
    f"dbname=agent_data user=postgres password={_pg_pass} host=localhost"
)
NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", os.environ.get("NEO4J_PASSWORD", ""))

# Embedding timeout: BGE-M3 is fast even for long inputs.
EMBED_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
# Reranking timeout: BGE-Reranker processes each (query, doc) pair sequentially;
# 10 full-content candidates can exceed 20s on CPU-only inference stacks.
RERANK_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
# Legacy alias kept for callers not yet updated.
TIMEOUT = EMBED_TIMEOUT
# If the top reranker score is below this value, episodic results are not
# confidently relevant and the entity-graph fallback is triggered.
LOW_CONFIDENCE_THRESHOLD = -3.0

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
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as client:
            resp = await client.post(
                RETRIEVER_URL,
                json={"input": text, "model": "bge-m3"},
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"Embedding service unreachable or slow: {str(e)}")
        return None


def _graph_entity_fallback(query: str, limit: int = 5) -> str:
    """Entity-graph search used when reranker confidence is low.
    Extracts significant words from the query, matches against Entity.name nodes,
    follows MENTIONS edges to Fact nodes, fetches full content from Postgres."""
    terms = [w.lower() for w in query.split() if len(w) > 3]
    if not terms:
        return ""
    try:
        driver = get_neo4j()
        with driver.session() as session:
            result = session.run(
                f"MATCH (e:{ONT.entity})"
                f" WHERE any(term IN $terms WHERE toLower(e.name) CONTAINS term)"
                f" MATCH (f:{ONT.fact})-[:{ONT.entity_link}]->(e)"
                f" RETURN DISTINCT f.pg_id LIMIT $cap",
                terms=terms, cap=limit * 2
            )
            pg_ids = [r["f.pg_id"] for r in result if r["f.pg_id"] is not None]
    except Exception as e:
        logger.warning(f"Graph entity fallback Neo4j query failed: {e}")
        return ""
    if not pg_ids:
        return ""
    try:
        conn = get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content, metadata FROM technical_docs"
                    " WHERE id = ANY(%s) LIMIT %s",
                    (pg_ids, limit)
                )
                rows = cur.fetchall()
        finally:
            release_pg_conn(conn)
    except Exception as e:
        logger.warning(f"Graph entity fallback Postgres fetch failed: {e}")
        return ""
    docs = []
    for content, meta in rows:
        source = meta.get("source", "unknown") if isinstance(meta, dict) else "unknown"
        docs.append(f"[Graph Fallback | Source: {source}]\n{content}")
    return "\n\n---\n\n".join(docs)


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
                    ORDER BY embedding <=> %s::vector LIMIT 10
                """, (query_vector,))
                rows = cur.fetchall()
                ids = [row[0] for row in rows]
                candidates = [row[1] for row in rows]
                meta = [row[2] for row in rows]
        finally:
            release_pg_conn(conn)

        if not candidates:
            return "Result: No relevant documentation found."

        # 4. Rerank — full document content, no truncation.
        async with httpx.AsyncClient(timeout=RERANK_TIMEOUT) as client:
            resp = await client.post(RERANKER_URL, json={
                "query": query,
                "documents": candidates,
                "top_k": limit
            }, headers=_auth_headers())
            resp.raise_for_status()
            rerank_results = resp.json()["results"]

        # Low-confidence check — trigger entity-graph fallback if no result is
        # confidently relevant (best score below threshold).
        best_score = max((r["relevance_score"] for r in rerank_results), default=-999.0)
        graph_supplement = ""
        if best_score < LOW_CONFIDENCE_THRESHOLD:
            logger.warning(
                "Reranker low confidence (best=%.2f < %.1f) — querying entity graph",
                best_score, LOW_CONFIDENCE_THRESHOLD,
            )
            graph_supplement = _graph_entity_fallback(query, limit)

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
                    graph_result = session.run(
                        f"MATCH (f:{ONT.fact} {{pg_id: $pg_id}})"
                        " OPTIONAL MATCH (f)-[r]-(related)"
                        " RETURN labels(related) as labels, related.name as name, type(r) as rel_type"
                        " LIMIT 5",
                        pg_id=pg_id)

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
        result_text = global_context + header + "\n\n---\n\n".join(output_docs)
        if graph_supplement:
            result_text += (
                "\n\n---\n\n### Entity Graph Results (low confidence — supplementary)\n\n"
                + graph_supplement
            )
        return result_text

    except Exception as e:
        logger.error(f"Search failed: {str(e)}")
        return f"Error: {str(e)}"

@mcp.tool()
async def save_artifact(content: str, metadata_json: str = "{}") -> str:
    """
    Stores an artifact in shared memory via the Hive-Mind Gateway.

    Routes through the Memory Coordinator (POST /memory/save) — no direct DB
    writes here — so the save gets the full server-side path: BGE-M3 embedding
    (hard mandate; the coordinator returns 503 if the embedder is down), SHA-256
    idempotent upsert into Postgres, and a neo4j_outbox row written in the SAME
    transaction and applied asynchronously by the outbox worker. That closes the
    ADR-001 dangling-Fact gap the old direct Postgres+Neo4j write left open: the
    two stores can no longer diverge on a crash between them.

    Idempotent: identical content reuses the existing row.
    """
    # Validate metadata client-side first so the model gets a clear MCP error
    # before any network call. The coordinator is the authority and re-checks.
    if isinstance(metadata_json, str):
        try:
            m_data = json.loads(metadata_json)
        except (json.JSONDecodeError, ValueError) as e:
            _append_log("vector_skill", 2, "bad_metadata", {"error": str(e), "content_preview": content[:100]}, content)
            return f"Error: Invalid metadata JSON: {e}"
    else:
        m_data = metadata_json

    if not isinstance(m_data, dict):
        _append_log("vector_skill", 2, "bad_metadata_type", {"got": type(m_data).__name__, "content_preview": content[:100]}, content)
        return f"Error: Metadata must be a JSON object, got {type(m_data).__name__}"

    if not m_data.get("source"):
        _append_log("vector_skill", 2, "missing_source", {"content_preview": content[:100]}, content)
        return (
            "Error: metadata.source is required — set it to the loaded model name "
            "(e.g. 'qwen3-27b', 'llama3-70b'). "
            "Facts without provenance are rejected to protect memory integrity."
        )

    m_data["timestamp"] = datetime.now().isoformat()
    # Auth-enabled gateways overwrite metadata.source with the verified agent
    # identity (e.g. "lm_studio"). Preserve the loaded model name so the
    # specific model behind the save is not lost when several share one token.
    m_data.setdefault("model", m_data["source"])
    entities = m_data.get("entities", [])

    coordinator_url = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
    agent_id = os.environ.get("AGENT_ID", "lm_studio")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/save",
                json={"content": content, "metadata": m_data, "agent_id": agent_id},
                headers=_auth_headers(),
            )
            if r.status_code == 401:
                return "Error: Coordinator rejected token. Set AGENT_TOKEN in mcp.json env block."
            result = r.json()
    except Exception as exc:
        _append_log("vector_skill", 2, "gateway_down", {"content_preview": content[:100]}, content)
        return (
            f"Error: Hive-Mind Gateway unreachable at {coordinator_url} — is "
            f"hive_mind_proxy.py running? Save aborted to protect memory integrity. ({exc})"
        )

    if result.get("status") != "success":
        # Coordinator rejected the save — e.g. missing source (400) or the
        # embedder unreachable after retries (503). Surface its message verbatim.
        _append_log("vector_skill", 2, "save_rejected", {"message": result.get("message", result)}, content)
        return f"Error: {result.get('message', result)}"

    pg_id = result.get("pg_id")
    if not entities:
        _append_log("vector_skill", 1, "no_entities", {"pg_id": pg_id, "source": m_data.get("source")}, content)
    _append_log("vector_skill", 3, "save_success", {"pg_id": pg_id, "source": m_data.get("source"), "entity_count": len(entities)}, content)

    # The coordinator's message already carries the no-entities Tier-3 warning.
    neo4j_status = result.get("neo4j", "pending")
    return f"Success (pg_id={pg_id}, neo4j={neo4j_status}): {result.get('message', '')}".rstrip()

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
            session.run(
                f"MERGE (t:{ONT.reasoning_trace} {{id: $session_id}})"
                " SET t.task = $task,"
                "     t.timestamp = datetime()",
                session_id=session_id, task=task)

            prev_id = session_id
            for i, step in enumerate(steps):
                step_id = f"{session_id}_step_{i}"
                content = f"Thought: {step.get('thought', '')}\nTool: {step.get('tool', '')}"

                step_embedding = await get_embedding(content)

                session.run(
                    " MATCH (prev) WHERE prev.id = $prev_id"
                    f" CREATE (s:{ONT.reasoning_step} {{id: $step_id}})"
                    " SET s.content = $content,"
                    "     s.result = $result,"
                    "     s.index = $i"
                    f" CREATE (prev)-[:{ONT.reasoning_next}]->(s)",
                    prev_id=prev_id, step_id=step_id, content=content,
                    result=str(step.get('result', '')), i=i)

                prev_id = step_id

        logger.info(f"Trace archived for session {session_id}")
        return f"Success: Archived reasoning trace with {len(steps)} steps."
    except Exception as e:
        logger.error(f"Trace archive failed: {str(e)}")
        return f"Error: {str(e)}"

@mcp.tool()
async def save_decision(
    title: str,
    decided_by: str,
    project: str,
    rationale: str,
    source: str,
    assisted_by: str = "",
    alternatives: str = "",
    confidence: str = "",
    entities: str = "",
) -> str:
    """
    Save an architectural or design decision with full PROV-O provenance.

    Routes through the Memory Coordinator so the Decision→Human→Project→AIAgent
    subgraph is written by the outbox worker — no direct Neo4j writes here.

    Required: title, decided_by, project, rationale, source (loaded model name).
    Optional: assisted_by, alternatives, confidence, entities — all comma-separated.
    """
    decision_data: dict = {
        "title": title,
        "decided_by": decided_by,
        "project": project,
        "rationale": rationale,
        "date": datetime.now().date().isoformat(),
    }
    if assisted_by:
        decision_data["assisted_by"] = [a.strip() for a in assisted_by.split(",") if a.strip()]
    if alternatives:
        decision_data["alternatives"] = [a.strip() for a in alternatives.split(",") if a.strip()]
    if confidence:
        decision_data["confidence"] = confidence

    metadata = {
        "type": "decision",
        "source": source,
        "entities": [e.strip() for e in entities.split(",") if e.strip()],
        "decision": decision_data,
    }
    content = f"{title}\n\n{rationale}"

    coordinator_url = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
    agent_id = os.environ.get("AGENT_ID", "lm_studio")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/save",
                json={"content": content, "metadata": metadata, "agent_id": agent_id},
                headers=_auth_headers(),
            )
            if r.status_code == 401:
                return "Error: Coordinator rejected token. Set AGENT_TOKEN in mcp.json env block."
            result = r.json()
    except Exception as exc:
        return (
            f"Error: Memory coordinator unreachable at {coordinator_url} — "
            f"is hive_mind_proxy.py running? ({exc})"
        )

    if result.get("status") == "success":
        pg_id = result.get("pg_id")
        ent_count = len(metadata["entities"])
        ent_note = f" with {ent_count} entities" if ent_count else ""
        return f"Decision saved (pg_id={pg_id}){ent_note}: {title}"

    return f"Error: {result.get('message', result)}"


@mcp.tool()
async def save_retrospective(
    pg_id: int,
    rating: str,
    notes: str,
    source: str,
    date: str = "",
) -> str:
    """
    Record an outcome on an existing Decision node (HAD_OUTCOME edge).

    Use this after a decision has been acted on to close the Why-To loop.
    Each call appends a new dated edge — multiple retrospectives per decision are allowed.

    Required: pg_id (returned by save_decision), rating, notes, source.
    Optional: date (ISO string, default: today).
    """
    coordinator_url = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
    agent_id = os.environ.get("AGENT_ID", "lm_studio")

    payload = {
        "pg_id": pg_id,
        "rating": rating,
        "notes": notes,
        "date": date or datetime.now().date().isoformat(),
        "agent_id": source or agent_id,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/retrospective",
                json=payload,
                headers=_auth_headers(),
            )
            if r.status_code == 401:
                return "Error: Coordinator rejected token. Set AGENT_TOKEN in mcp.json env block."
            result = r.json()
    except Exception as exc:
        return (
            f"Error: Memory coordinator unreachable at {coordinator_url} — "
            f"is hive_mind_proxy.py running? ({exc})"
        )

    if result.get("status") == "success":
        return f"Retrospective recorded on Decision pg_id={result['target_pg_id']}."

    return f"Error: {result.get('message', result)}"


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
            resp = await client.post(RETRIEVER_URL, json={"input": "healthcheck", "model": "bge-m3"},
                                     headers=_auth_headers())
            stats["components"]["retriever"] = {"status": "OK" if resp.status_code == 200 else "FAIL"}
    except Exception as e:
        stats["status"] = "degraded"
        stats["components"]["retriever"] = {"status": "FAIL", "error": str(e)}

    # Check Reranker (via Gateway)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(RERANKER_URL, json={"query": "health", "documents": ["check"]},
                                     headers=_auth_headers())
            stats["components"]["reranker"] = {"status": "OK" if resp.status_code == 200 else "FAIL"}
    except Exception as e:
        stats["status"] = "degraded"
        stats["components"]["reranker"] = {"status": "FAIL", "error": str(e)}

    return json.dumps(stats, indent=2)

if __name__ == "__main__":
    mcp.run()
