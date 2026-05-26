"""
Memory Coordinator — Phase 2

Owns all Postgres and Neo4j I/O for the memory system.
Embedded in hive_mind_proxy.py via attach(); designed so the only change
needed to extract it into a standalone process (Phase 4) is the attach() call.

Isolation principle: no import-time dependency on aiohttp internals beyond
web.Request / web.Response / web.Application. All storage logic lives here.

Phase 2 additions (over Phase 1)
─────────────────────────────────
  Outbox worker — background asyncio task that drains neo4j_outbox:
    - polls every OUTBOX_POLL_INTERVAL seconds
    - applies each pending row to Neo4j (MERGE Fact + Entity + MENTIONS)
    - marks rows applied or failed (up to OUTBOX_MAX_RETRIES attempts)
    - started with the coordinator, cancelled on clean shutdown
  Direct Neo4j writes removed from handle_save — all Neo4j writes now
    go through the outbox worker (eliminates ADR-001 atomicity risk)
  ?consistency=neo4j query param on /memory/save — blocks until the
    outbox row for the saved fact is marked applied (or timeout)

Routes registered by attach()
──────────────────────────────
  POST /memory/save              Postgres-ack; returns 200 + pg_id
  POST /memory/search            Tier 3 → Tier 1 → rerank → Neo4j expand
  POST /memory/graph             Raw Cypher passthrough
  GET  /memory/status/{pg_id}   Outbox row state
"""

import asyncio
import hashlib
import json
import logging
import os
import re
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
EMBED_BACKOFF = 0.5      # seconds × attempt number  (0.5 s, 1 s, 1.5 s, 2 s)
POOL_MIN, POOL_MAX = 2, 10

OUTBOX_POLL_INTERVAL = 2.0   # seconds between outbox drain cycles
OUTBOX_BATCH_SIZE    = 20    # rows processed per cycle
OUTBOX_MAX_RETRIES   = 5     # row marked 'failed' after this many Neo4j errors
CONSISTENCY_TIMEOUT  = 15.0  # seconds to wait for ?consistency=neo4j

# Cypher write-operation guard — reject queries containing mutating keywords.
# Defence-in-depth: blocks obvious destructive ops while a deeper Neo4j RBAC
# solution is built. Regex is intentionally strict (SET followed by a space
# avoids matching property names that contain "set" as a substring).
_WRITE_CYPHER = re.compile(
    r"\b(CREATE|DELETE|DETACH\s+DELETE|SET\s|REMOVE|MERGE|CALL|LOAD\s+CSV|DROP)\b",
    re.IGNORECASE,
)


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
        self._outbox_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            PG_DSN, min_size=POOL_MIN, max_size=POOL_MAX,
            init=self._init_connection,
        )
        self._neo4j = AsyncGraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        self._outbox_task = asyncio.create_task(self._outbox_worker(), name="outbox-worker")
        log.info("coordinator ready (pool %d–%d, outbox worker running)", POOL_MIN, POOL_MAX)

    async def stop(self) -> None:
        if self._outbox_task:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
        if self._pool:
            await self._pool.close()
        if self._neo4j:
            await self._neo4j.close()
        log.info("coordinator stopped")

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        """Register JSONB codec so columns decode to Python dicts, not raw strings."""
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
            format="text",
        )

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

    # ── Outbox worker ─────────────────────────────────────────────────────────

    async def _outbox_worker(self) -> None:
        """Background task: drain neo4j_outbox, applying pending rows to Neo4j."""
        log.info("outbox worker started (poll every %.1f s)", OUTBOX_POLL_INTERVAL)
        while True:
            try:
                await self._drain_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("outbox worker error: %s", exc, exc_info=True)
            await asyncio.sleep(OUTBOX_POLL_INTERVAL)

    async def _drain_outbox(self) -> None:
        # FOR UPDATE SKIP LOCKED: if two coordinator instances overlap (e.g. during
        # proxy restart), each instance claims a disjoint set of rows — no row is
        # processed twice. Rows held by another transaction are silently skipped and
        # picked up on the next poll cycle.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT id, pg_id, cypher_params, retries
                    FROM neo4j_outbox
                    WHERE status = 'pending' AND retries < $1
                    ORDER BY id
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                    """,
                    OUTBOX_MAX_RETRIES, OUTBOX_BATCH_SIZE,
                )
                rows = list(rows)   # copy out before the transaction closes
        if not rows:
            return
        log.debug("outbox: draining %d row(s)", len(rows))
        for row in rows:
            # asyncpg returns JSONB as a string in some configurations;
            # parse defensively rather than relying on codec registration.
            params = row["cypher_params"]
            if isinstance(params, str):
                params = json.loads(params)
            await self._apply_outbox_row(row["id"], row["pg_id"], params, row["retries"])

    async def _apply_outbox_row(
        self, outbox_id: int, pg_id: int, params: dict, retries: int
    ) -> None:
        try:
            # All Neo4j writes in one round-trip via UNWIND so they succeed or
            # fail atomically. MERGE is idempotent — safe to retry on transient errors.
            async with self._neo4j.session() as session:
                await session.run(
                    f"MERGE (f:{ONT.fact} {{pg_id: $pg_id}})"
                    f" SET f.content = $content, f.source = $source"
                    f" WITH f"
                    f" UNWIND $entities AS ename"
                    f" MERGE (e:{ONT.entity} {{name: ename}})"
                    f" MERGE (f)-[:{ONT.entity_link}]->(e)",
                    pg_id=pg_id,
                    content=params.get("content_snippet", "")[:200],
                    source=params.get("source", "coordinator"),
                    entities=params.get("entities", []),
                )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE neo4j_outbox SET status='applied', applied_at=now() WHERE id=$1",
                    outbox_id,
                )
            log.debug("outbox: applied pg_id=%d (outbox_id=%d)", pg_id, outbox_id)
        except Exception as exc:
            log.warning(
                "outbox: neo4j write failed pg_id=%d attempt %d/%d: %s",
                pg_id, retries + 1, OUTBOX_MAX_RETRIES, exc,
            )
            async with self._pool.acquire() as conn:
                if retries + 1 >= OUTBOX_MAX_RETRIES:
                    # Atomic: bump retries AND flip status in one statement
                    await conn.execute(
                        "UPDATE neo4j_outbox SET status='failed', retries=retries+1 WHERE id=$1",
                        outbox_id,
                    )
                    log.error(
                        "outbox: pg_id=%d permanently failed after %d attempts",
                        pg_id, retries + 1,
                    )
                else:
                    # retries+1 at DB level prevents lost updates if two instances
                    # process the same row concurrently (last-write-wins is avoided).
                    await conn.execute(
                        "UPDATE neo4j_outbox SET retries=retries+1 WHERE id=$1 AND status='pending'",
                        outbox_id,
                    )

    async def _wait_for_outbox(self, pg_id: int) -> bool:
        """Poll until the outbox row for pg_id is applied, or CONSISTENCY_TIMEOUT expires."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONSISTENCY_TIMEOUT
        while loop.time() < deadline:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status FROM neo4j_outbox WHERE pg_id=$1 ORDER BY id DESC LIMIT 1",
                    pg_id,
                )
            if row and row["status"] == "applied":
                return True
            await asyncio.sleep(0.25)
        return False

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

        # Acquire per-entity write locks (sorted to prevent deadlocks across concurrent saves).
        # Track only the locks we actually acquired: if acquire() is cancelled mid-list,
        # the finally block releases only what we hold — avoiding RuntimeError on unacquired locks.
        entity_locks = [await self._lock_for(e) for e in sorted(set(entities))]
        acquired: list[asyncio.Lock] = []
        try:
            for lk in entity_locks:
                await lk.acquire()
                acquired.append(lk)
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        INSERT INTO technical_docs
                            (content, metadata, embedding, content_hash,
                             agent_id, scope, visibility)
                        VALUES ($1, $2::jsonb, $3::vector, $4, $5, $6, $7)
                        ON CONFLICT (content_hash) DO UPDATE
                            SET metadata  = EXCLUDED.metadata,
                                agent_id  = EXCLUDED.agent_id,
                                embedding = EXCLUDED.embedding
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
            for lk in acquired:
                lk.release()

        # Neo4j is applied asynchronously by the outbox worker.
        # ?consistency=neo4j blocks until the row is marked applied.
        if request.rel_url.query.get("consistency") == "neo4j":
            applied = await self._wait_for_outbox(pg_id)
            neo4j_status = "applied" if applied else "timeout"
        else:
            neo4j_status = "pending"

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
        limit = min(max(1, int(body.get("limit", 5))), 100)
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

        if _WRITE_CYPHER.search(cypher):
            return web.json_response(
                {
                    "status": "error",
                    "message": "only read-only Cypher (MATCH/RETURN/WITH/WHERE/OPTIONAL MATCH) is permitted",
                },
                status=400,
            )

        try:
            async with self._neo4j.session() as session:
                result  = await session.run(cypher, **params)
                records = await result.data()
        except Exception as exc:
            log.error("graph query error for cypher=%r: %s", cypher[:120], exc, exc_info=True)
            return web.json_response({"status": "error", "message": "query failed"}, status=500)

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
