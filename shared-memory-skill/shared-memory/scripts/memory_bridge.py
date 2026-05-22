import sys
import json
import os
import psycopg2
import httpx
import asyncio
import logging
import hashlib
from neo4j import GraphDatabase

# Configuration — set via environment variables or .env file
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "")
_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_CONN = os.environ.get(
    "PG_CONN",
    f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
RETRIEVER_URL = "http://localhost:8888/v1/embeddings"
RERANKER_URL = "http://localhost:8888/v1/reranking"

def query_graph(cypher):
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        with driver.session() as session:
            result = session.run(cypher)
            return [record.data() for record in result]
    except Exception as e:
        return {"error": f"Neo4j Error: {str(e)}"}

async def get_embedding(text):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(RETRIEVER_URL, json={"input": text, "model": "bge-m3"})
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception:
        return None

async def search_and_rerank(query, limit=5):
    try:
        # 1. Attempt Vector Search
        query_vector = await get_embedding(query)

        conn = psycopg2.connect(PG_CONN)
        ids = []
        candidates = []
        meta = []

        if query_vector:
            # High-signal Vector Search
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, content, metadata FROM technical_docs
                    ORDER BY embedding <=> %s::vector LIMIT 20
                """, (query_vector,))
                rows = cur.fetchall()
                ids = [row[0] for row in rows]
                candidates = [row[1] for row in rows]
                meta = [row[2] for row in rows]
        else:
            # Fallback 1: Keyword Search (Model is down)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, content, metadata FROM technical_docs
                    WHERE content ILIKE %s OR metadata::text ILIKE %s
                    LIMIT %s
                """, (f"%{query}%", f"%{query}%", limit))
                rows = cur.fetchall()
                ids = [row[0] for row in rows]
                candidates = [row[1] for row in rows]
                meta = [row[2] for row in rows]
                conn.close()
                return [{"content": c, "score": 0.0, "metadata": m, "note": "Keyword search fallback"} for c, m in zip(candidates, meta)]

        conn.close()

        if not candidates:
            return {"results": [], "message": "No candidates found."}

        # 2. Attempt Rerank
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(RERANKER_URL, json={
                    "query": query,
                    "documents": candidates,
                    "top_k": limit
                })
                resp.raise_for_status()
                rerank_results = resp.json()["results"]
        except Exception:
            # Fallback: Top vector hits
            rerank_results = [{"index": i, "relevance_score": 1.0} for i in range(min(limit, len(candidates)))]

        # 3. Relational Expansion (Neo4j)
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        final_results = []
        for res in rerank_results:
            idx = res["index"]
            pg_id = ids[idx]

            # Query Neo4j for related context
            relational_context = []
            try:
                with driver.session() as session:
                    graph_result = session.run("""
                        MATCH (f:Fact {pg_id: $pg_id})
                        OPTIONAL MATCH (f)-[r]-(related)
                        RETURN labels(related) as labels, related.name as name, type(r) as rel_type
                        LIMIT 5
                    """, pg_id=pg_id)
                    for record in graph_result:
                        if record["name"]:
                            relational_context.append(f"{record['rel_type']} -> {record['name']} ({record['labels'][0]})")
            except:
                pass

            final_results.append({
                "content": candidates[idx],
                "score": res["relevance_score"],
                "metadata": meta[idx],
                "graph_context": " | ".join(relational_context) if relational_context else None
            })
        driver.close()
        return final_results

    except Exception as e:
        return {"error": str(e)}

async def save_artifact(content, metadata_json="{}"):
    try:
        # 1. HARD MANDATE: No saving without vectors
        embedding = await get_embedding(content)
        if not embedding:
            return {
                "status": "error",
                "message": "CRITICAL: Hive-Mind Gateway (port 8888) is DOWN. Save aborted to prevent memory degradation. Please start hive_mind_proxy.py first."
            }

        # 2. Parse Metadata
        try:
            m_data = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
        except:
            m_data = {"raw_metadata": metadata_json}

        # 3. Insert into Postgres (Semantic Anchor - Idempotent)
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        daemon_up = True
        conn = psycopg2.connect(PG_CONN)
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
        conn.close()

        # 4. Sync to Neo4j (Relational Anchor)
        try:
            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
            with driver.session() as session:
                session.run("""
                    MERGE (f:Fact {pg_id: $pg_id})
                    SET f.content = $content,
                        f.embedding = $embedding,
                        f.created_at = datetime(),
                        f.source = $source
                """, pg_id=pg_id, content=content[:200], embedding=embedding, source=m_data.get("source", "manual_sync"))

                entities = m_data.get("entities", [])
                for entity_name in entities:
                    session.run("""
                        MATCH (f:Fact {pg_id: $pg_id})
                        MERGE (e:Entity {name: $name})
                        MERGE (f)-[:MENTIONS]->(e)
                    """, pg_id=pg_id, name=entity_name)

            driver.close()
            sync_status = f"Linked to Neo4j Fact{f' with {len(entities)} entities' if entities else ''}."
        except Exception as e:
            sync_status = f"Postgres saved, but Neo4j sync failed: {str(e)}"

        daemon_warning = "" if daemon_up else " WARNING: Consolidation daemon not running — NOTIFY dropped. Start consolidation_loop.py."
        return {
            "status": "success",
            "message": f"Artifact stored with ID {pg_id}. {sync_status}{daemon_warning}"
        }
    except Exception as e:
        return {"error": str(e)}

async def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: python memory_bridge.py [graph|search|save] <query/content> [metadata/limit]"}))
        sys.exit(1)

    action = sys.argv[1]

    if action == "graph":
        print(json.dumps(query_graph(sys.argv[2]), indent=2))
    elif action == "search":
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        print(json.dumps(await search_and_rerank(sys.argv[2], limit), indent=2))
    elif action == "save":
        metadata = sys.argv[3] if len(sys.argv) > 3 else "{}"
        print(json.dumps(await save_artifact(sys.argv[2], metadata), indent=2))
    else:
        print(json.dumps({"error": f"Unknown action: {action}"}))

if __name__ == "__main__":
    asyncio.run(main())
