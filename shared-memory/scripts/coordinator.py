"""
Memory Coordinator — Phase 1

Owns all Postgres and Neo4j I/O for the memory system.
Embedded in hive_mind_proxy.py via attach(); designed so the only change
needed to extract it into a standalone process (Phase 4) is the attach() call.

Isolation principle: no import-time dependency on aiohttp internals beyond
web.Request / web.Response / web.Application. All storage logic lives here.

Phase 1 scope
─────────────
  asyncpg pool (replaces per-call psycopg2)
  Per-entity asyncio.Lock for write serialization
  Embedding with exponential-backoff retry
  Outbox row written atomically with each Postgres fact (Phase 2 worker drains it)
  Direct Neo4j writes (replaced by outbox worker in Phase 2)

Routes registered by attach()
──────────────────────────────
  POST /memory/save              Postgres-ack; returns 200 + pg_id
  POST /memory/search            Tier 3 → Tier 1 → rerank → Neo4j expand
  POST /memory/graph             Raw Cypher passthrough
  GET  /memory/status/{pg_id}   Outbox row state for ?consistency=neo4j callers
"""

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

import asyncpg
import httpx
from aiohttp import web
from neo4j import AsyncGraphDatabase

from ontology import ONT

log = logging.getLogger("coordinator")

# ── Config ────────────────────────────────────────────────────────────────────

_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_DSN   = os.environ.get(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", os.environ.get("NEO4J_PASSWORD", ""))

# Embedding routes through the gateway.
# Reranker is called directly (port 8071) to avoid a circular path —
# the coordinator runs inside the proxy, so calling :8888/v1/reranking
# would be the proxy calling itself.
EMBED_URL  = "http://localhost:8888/v1/embeddings"
RERANK_URL = "http://localhost:8071/v1/reranking"

EMBED_RETRIES = 4
EMBED_BACKOFF = 0.5   # seconds × attempt number  (0.5 s, 1 s, 1.5 s, 2 s)
POOL_MIN, POOL_MAX = 2, 10


# ── Coordinator ───────────────────────────────────────────────────────────────

class MemoryCoordinator:
    """
    Single-process coordinator for all memory writes and reads.

    Instantiate once, await start() during app startup, await stop() on shutdown.
    Routes are registered via attach() — the only coupling point with aiohttp.
    """

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._neo4j: Any = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_mu = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(PG_DSN, min_size=POOL_MIN, max_size=POOL_MAX)
        self._neo4j = AsyncGraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        log.info("coordinator ready (pool %d–%d)", POOL_MIN, POOL_MAX)

    async def stop(self) -> None:
        if self._pool:
            await self._pool.close()
        if self._neo4j:
            await self._neo4j.close()
        log.info("coordinator stopped")

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _lock_for(self, entity: str) -> asyncio.Lock:
        async with self._locks_mu:
            if entity not in self._locks:
                self._locks[entity] = asyncio.Lock()
            return self._locks[entity]

    async def _embed(self, text: str, client: httpx.AsyncClient) -> list[float]:
        """Embed text via the gateway with exponential-backoff retry."""
        for attempt in range(1, EMBED_RETRIES + 1):
            try:
                r = await client.post(EMBED_URL, json={"input": text, "model": "bge-m3"})
                r.raise_for_status()
                return r.json()["data"][0]["embedding"]
            except Exception as exc:
                if attempt == EMBED_RETRIES:
                    raise RuntimeError(
                        f"Embedding failed after {EMBED_RETRIES} attempts — "
                        f"is hive_mind_proxy running? ({exc})"
                    ) from exc
                wait = EMBED_BACKOFF * attempt
                log.warning(
                    "embed attempt %d/%d failed (%s) — retry in %.1f s",
                    attempt, EMBED_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)

    # ── POST /memory/save ─────────────────────────────────────────────────────

    async def handle_save(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )

        content    = body.get("content", "")
        metadata   = body.get("metadata", {})
        agent_id   = body.get("agent_id", "unknown")
        scope      = body.get("scope", "global")
        visibility = body.get("visibility", "global")

        if not content:
            return web.json_response(
                {"status": "error", "message": "content is required"}, status=400
            )
        if not isinstance(metadata, dict):
            return web.json_response(
                {"status": "error", "message": "metadata must be a JSON object"}, status=400
            )

        entities     = metadata.get("entities", [])
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Embedding — hard mandate; no save without a vector
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                embedding = await self._embed(content, client)
        except RuntimeError as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=503)

        # Acquire per-entity write locks (sorted to prevent deadlocks across concurrent saves)
        entity_locks = [await self._lock_for(e) for e in sorted(set(entities))]
        for lk in entity_locks:
            await lk.acquire()
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        INSERT INTO technical_docs
                            (content, metadata, embedding, content_hash,
                             agent_id, scope, visibility)
                        VALUES ($1, $2::jsonb, $3::vector, $4, $5, $6, $7)
                        ON CONFLICT (content_hash) DO UPDATE
                            SET metadata = EXCLUDED.metadata,
                                agent_id = EXCLUDED.agent_id
                        RETURNING id
                        """,
                        content, json.dumps(metadata), str(embedding),
                        content_hash, agent_id, scope, visibility,
                    )
                    pg_id = row["id"]

                    # Outbox row written atomically with the fact.
                    # The Phase 2 outbox worker drains this table.
                    await conn.execute(
                        """
                        INSERT INTO neo4j_outbox (pg_id, cypher_params)
                        VALUES ($1, $2::jsonb)
                        """,
                        pg_id,
                        json.dumps({
                            "content_snippet": content[:200],
                            "source": metadata.get("source", "coordinator"),
                            "entities": entities,
                            "agent_id": agent_id,
                        }),
                    )

                    # Wake the consolidation daemon
                    await conn.execute(
                        "SELECT pg_notify('new_artifact', $1)",
                        json.dumps({"pg_id": pg_id}),
                    )
        finally:
            for lk in entity_locks:
                lk.release()

        # Neo4j direct write — Phase 1.
        # In Phase 2 the outbox worker takes over; this block is removed.
        neo4j_status = "synced"
        try:
            async with self._neo4j.session() as session:
                await session.run(
                    f"MERGE (f:{ONT.fact} {{pg_id: $pg_id}})"
                    " SET f.content = $content, f.source = $source",
                    pg_id=pg_id,
                    content=content[:200],
                    source=metadata.get("source", "coordinator"),
                )
                for name in entities:
                    await session.run(
                        f"MATCH (f:{ONT.fact} {{pg_id: $pg_id}})"
                        f" MERGE (e:{ONT.entity} {{name: $name}})"
                        f" MERGE (f)-[:{ONT.entity_link}]->(e)",
                        pg_id=pg_id, name=name,
                    )
        except Exception as exc:
            log.warning("neo4j sync failed for pg_id=%d: %s", pg_id, exc)
            neo4j_status = "pending"  # outbox worker retries in Phase 2

        warn = (
            ""
            if entities
            else " WARNING: no 'entities' in metadata — fact ineligible for Tier 3 consolidation."
        )
        return web.json_response({
            "status": "success",
            "pg_id": pg_id,
            "neo4j": neo4j_status,
            "message": f"Artifact stored with ID {pg_id}.{warn}",
        })

    # ── POST /memory/search ───────────────────────────────────────────────────

    async def handle_search(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )

        query = body.get("query", "")
        limit = int(body.get("limit", 5))
        scope = body.get("scope")  # None = no scope filter

        if not query:
            return web.json_response(
                {"status": "error", "message": "query is required"}, status=400
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                q_vec = await self._embed(query, client)
            except RuntimeError:
                q_vec = None

            if q_vec is None:
                # Keyword fallback when the embedding service is unavailable
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT id, content, metadata FROM technical_docs
                        WHERE content ILIKE $1 OR metadata::text ILIKE $1
                        LIMIT $2
                        """,
                        f"%{query}%", limit,
                    )
                return web.json_response({
                    "status": "success",
                    "fallback": "keyword",
                    "results": [
                        {"content": r["content"], "score": 0.0, "metadata": r["metadata"]}
                        for r in rows
                    ],
                })

            async with self._pool.acquire() as conn:
                # Tier 3 — top community summary for context orientation
                summary = await conn.fetchrow(
                    "SELECT content FROM community_summaries"
                    " ORDER BY embedding <=> $1::vector LIMIT 1",
                    str(q_vec),
                )

                # Tier 1 — vector search, 20 candidates for reranker
                scope_sql = "AND scope = $3" if scope else ""
                args: list = [str(q_vec), 20]
                if scope:
                    args.append(scope)
                candidates = await conn.fetch(
                    f"""
                    SELECT id, content, metadata FROM technical_docs
                    WHERE 1=1 {scope_sql}
                    ORDER BY embedding <=> $1::vector LIMIT $2
                    """,
                    *args,
                )

            if not candidates:
                return web.json_response({"status": "success", "results": []})

            ids      = [r["id"]       for r in candidates]
            contents = [r["content"]  for r in candidates]
            metas    = [r["metadata"] for r in candidates]

            # Rerank — direct to port 8071 to avoid circular proxy call
            try:
                rr = await client.post(
                    RERANK_URL,
                    json={"query": query, "documents": contents, "top_k": limit},
                    timeout=5.0,
                )
                rr.raise_for_status()
                ranked = rr.json()["results"]
            except Exception:
                ranked = [
                    {"index": i, "relevance_score": 1.0}
                    for i in range(min(limit, len(candidates)))
                ]

        # Neo4j relational expansion
        final: list[dict] = []
        if summary:
            final.append({
                "type": "community_summary",
                "content": summary["content"],
                "score": None,
                "metadata": None,
                "graph_context": None,
            })

        async with self._neo4j.session() as session:
            for hit in ranked:
                idx   = hit["index"]
                pg_id = ids[idx]
                ctx: list[str] = []
                try:
                    result = await session.run(
                        f"MATCH (f:{ONT.fact} {{pg_id: $pg_id}})"
                        " OPTIONAL MATCH (f)-[r]-(related)"
                        " RETURN labels(related) as labels, related.name as name,"
                        "        type(r) as rel_type LIMIT 5",
                        pg_id=pg_id,
                    )
                    async for rec in result:
                        if rec["name"]:
                            ctx.append(
                                f"{rec['rel_type']} -> {rec['name']} ({rec['labels'][0]})"
                            )
                except Exception:
                    pass
                final.append({
                    "content": contents[idx],
                    "score": hit["relevance_score"],
                    "metadata": metas[idx],
                    "graph_context": " | ".join(ctx) or None,
                })

        return web.json_response({"status": "success", "results": final})

    # ── POST /memory/graph ────────────────────────────────────────────────────

    async def handle_graph(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )

        cypher = body.get("cypher", "")
        params = body.get("params", {})

        if not cypher:
            return web.json_response(
                {"status": "error", "message": "cypher is required"}, status=400
            )

        try:
            async with self._neo4j.session() as session:
                result  = await session.run(cypher, **params)
                records = await result.data()
        except Exception as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

        return web.json_response({"status": "success", "records": records})

    # ── GET /memory/status/{pg_id} ────────────────────────────────────────────

    async def handle_status(self, request: web.Request) -> web.Response:
        try:
            pg_id = int(request.match_info["pg_id"])
        except ValueError:
            return web.json_response(
                {"status": "error", "message": "pg_id must be an integer"}, status=400
            )

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT status, retries, applied_at FROM neo4j_outbox
                WHERE pg_id = $1 ORDER BY id DESC LIMIT 1
                """,
                pg_id,
            )

        if not row:
            return web.json_response({"pg_id": pg_id, "neo4j": "unknown"})

        return web.json_response({
            "pg_id": pg_id,
            "neo4j": row["status"],
            "retries": row["retries"],
            "applied_at": row["applied_at"].isoformat() if row["applied_at"] else None,
        })


# ── Registration ──────────────────────────────────────────────────────────────

def attach(app: web.Application, coordinator: MemoryCoordinator) -> None:
    """Register /memory/* routes on an aiohttp Application.

    Must be called before the proxy catch-all route so these exact-path routes
    take precedence. To extract the coordinator into a standalone process
    (Phase 4), only this call site changes.
    """
    app.router.add_post("/memory/save",           coordinator.handle_save)
    app.router.add_post("/memory/search",         coordinator.handle_search)
    app.router.add_post("/memory/graph",          coordinator.handle_graph)
    app.router.add_get( "/memory/status/{pg_id}", coordinator.handle_status)
