import argparse
import sys
import json
import os
import psycopg2
import httpx
import asyncio
import logging
import hashlib
from datetime import datetime
from neo4j import GraphDatabase

VERSION = "0.3.2"

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
    except Exception:
        pass  # logging must never break the save path

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
            _append_log("memory_bridge", 2, "gateway_down", {"content_preview": content[:100]}, content)
            return {
                "status": "error",
                "message": "CRITICAL: Hive-Mind Gateway (port 8888) is DOWN. Save aborted to prevent memory degradation. Please start hive_mind_proxy.py first."
            }

        # 2. Parse Metadata
        if isinstance(metadata_json, str):
            try:
                m_data = json.loads(metadata_json)
            except (json.JSONDecodeError, ValueError) as e:
                _append_log("memory_bridge", 2, "bad_metadata", {"error": str(e), "content_preview": content[:100]}, content)
                return {"status": "error", "message": f"Invalid metadata JSON: {e}"}
        else:
            m_data = metadata_json

        if not isinstance(m_data, dict):
            _append_log("memory_bridge", 2, "bad_metadata_type", {"got": type(m_data).__name__, "content_preview": content[:100]}, content)
            return {"status": "error", "message": f"Metadata must be a JSON object, got {type(m_data).__name__}"}

        entities = m_data.get("entities", [])

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
            _append_log("memory_bridge", 2, "neo4j_sync_failed", {"pg_id": pg_id, "error": str(e)}, content)

        if not entities:
            _append_log("memory_bridge", 1, "no_entities", {"pg_id": pg_id, "source": m_data.get("source")}, content)
        _append_log("memory_bridge", 3, "save_success", {"pg_id": pg_id, "source": m_data.get("source"), "entity_count": len(entities)}, content)

        entities_warning = "" if entities else " WARNING: No 'entities' in metadata — fact stored but ineligible for Tier 3 consolidation."
        daemon_warning = "" if daemon_up else " WARNING: Consolidation daemon not running — NOTIFY dropped. Start consolidation_loop.py."
        return {
            "status": "success",
            "message": f"Artifact stored with ID {pg_id}. {sync_status}{entities_warning}{daemon_warning}"
        }
    except Exception as e:
        return {"error": str(e)}

COORDINATOR_BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
AGENT_ID = os.environ.get("AGENT_ID", "memory_bridge")


def build_decision_metadata(
    title: str,
    decided_by: str,
    project: str,
    rationale: str,
    source: str = None,
    assisted_by: str = "",
    alternatives: str = "",
    confidence: str = "",
    entities: str = "",
) -> tuple:
    """Build (content, metadata) for a decision save. Pure function — no I/O."""
    content = f"{title}\n\n{rationale}"
    decision = {
        "title": title,
        "decided_by": decided_by,
        "project": project,
        "rationale": rationale,
        "date": datetime.now().date().isoformat(),
    }
    if assisted_by:
        decision["assisted_by"] = [a.strip() for a in assisted_by.split(",") if a.strip()]
    if alternatives:
        decision["alternatives"] = [a.strip() for a in alternatives.split(",") if a.strip()]
    if confidence:
        decision["confidence"] = confidence

    metadata = {
        "type": "decision",
        "source": source or AGENT_ID,
        "entities": [e.strip() for e in entities.split(",") if e.strip()],
        "decision": decision,
    }
    return content, metadata


async def save_decision_via_coordinator(content: str, metadata: dict) -> dict:
    """Route a decision save through the coordinator (handles Decision outbox path)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/save",
                json={"content": content, "metadata": metadata, "agent_id": AGENT_ID},
            )
            return r.json()
    except Exception as exc:
        return {
            "status": "error",
            "message": (
                f"Memory coordinator unreachable at {COORDINATOR_BASE} — "
                f"is hive_mind_proxy.py running? ({exc})"
            ),
        }


def build_retrospective_payload(
    pg_id: int,
    rating: str,
    notes: str,
    date: str = "",
    source: str = None,
) -> dict:
    """Build the JSON payload for POST /memory/retrospective. Pure function — no I/O."""
    return {
        "pg_id": pg_id,
        "rating": rating,
        "notes": notes,
        "date": date or datetime.now().date().isoformat(),
        "agent_id": source or AGENT_ID,
    }


async def save_retrospective_via_coordinator(
    pg_id: int,
    rating: str,
    notes: str,
    date: str = "",
    source: str = None,
) -> dict:
    payload = build_retrospective_payload(pg_id, rating, notes, date, source)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{COORDINATOR_BASE}/memory/retrospective", json=payload)
            return r.json()
    except Exception as exc:
        return {
            "status": "error",
            "message": (
                f"Memory coordinator unreachable at {COORDINATOR_BASE} — "
                f"is hive_mind_proxy.py running? ({exc})"
            ),
        }


async def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python memory_bridge.py [--version|graph|search|save|save_decision|save_retrospective] ..."}))
        sys.exit(1)

    action = sys.argv[1]

    if action in ("--version", "version", "-v"):
        print(json.dumps({"version": VERSION, "tool": "shared-memory-framework"}))
        return
    elif action == "graph":
        print(json.dumps(query_graph(sys.argv[2]), indent=2))
    elif action == "search":
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        print(json.dumps(await search_and_rerank(sys.argv[2], limit), indent=2))
    elif action == "save":
        metadata = sys.argv[3] if len(sys.argv) > 3 else "{}"
        print(json.dumps(await save_artifact(sys.argv[2], metadata), indent=2))
    elif action == "save_decision":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py save_decision",
            description="Save an architectural or design decision with PROV-O provenance.",
        )
        p.add_argument("--title",       required=True)
        p.add_argument("--decided-by",  required=True)
        p.add_argument("--project",     required=True)
        p.add_argument("--rationale",   required=True)
        p.add_argument("--source",      default=AGENT_ID)
        p.add_argument("--assisted-by", default="")
        p.add_argument("--alternatives", default="")
        p.add_argument("--confidence",  default="")
        p.add_argument("--entities",    default="")
        args = p.parse_args(sys.argv[2:])
        content, metadata = build_decision_metadata(
            title=args.title,
            decided_by=args.decided_by,
            project=args.project,
            rationale=args.rationale,
            source=args.source,
            assisted_by=args.assisted_by,
            alternatives=args.alternatives,
            confidence=args.confidence,
            entities=args.entities,
        )
        print(json.dumps(await save_decision_via_coordinator(content, metadata), indent=2))
    elif action == "save_retrospective":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py save_retrospective",
            description="Record an outcome for a past decision (HAD_OUTCOME edge).",
        )
        p.add_argument("--pg-id",  required=True, type=int,
                       help="pg_id of the target Decision")
        p.add_argument("--rating", required=True,
                       help="Outcome rating (e.g. high, medium, low)")
        p.add_argument("--notes",  required=True,
                       help="What actually happened / lessons learned")
        p.add_argument("--date",   default="",
                       help="ISO date of outcome (default: today)")
        p.add_argument("--source", default=AGENT_ID,
                       help="Agent/model recording the outcome (default: $AGENT_ID)")
        args = p.parse_args(sys.argv[2:])
        print(json.dumps(
            await save_retrospective_via_coordinator(
                pg_id=args.pg_id,
                rating=args.rating,
                notes=args.notes,
                date=args.date,
                source=args.source,
            ),
            indent=2,
        ))
    else:
        print(json.dumps({"error": f"Unknown action: {action}. Use graph|search|save|save_decision|save_retrospective"}))

if __name__ == "__main__":
    asyncio.run(main())
