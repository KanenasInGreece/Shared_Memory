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
import math
import os
import re
from datetime import datetime
from typing import Any

import asyncpg
import httpx
from aiohttp import web
from neo4j import AsyncGraphDatabase

from ontology import ONT

log = logging.getLogger("coordinator")

# ── Version contract ────────────────────────────────────────────────────────────
# FRAMEWORK_VERSION is the informational build/semver — it changes every release.
# API_VERSION is the wire contract between memory_bridge.py (the thin client that
# ships with the skill) and this coordinator. Bump it ONLY when the request or
# response shape, auth scheme, or routes change in a way that breaks older clients.
# Client and server build-versions are allowed to drift; their API_VERSION must agree.
FRAMEWORK_VERSION = "0.4.4"
API_VERSION = 1
CLIENT_VERSION_HEADER = "X-SM-Api-Version"

# Throttle: remember (agent, version) pairs already logged so a misversioned
# client does not flood the gateway log on every request.
_seen_version_skews: set[tuple[str, int]] = set()


def _check_client_version(request: web.Request) -> None:
    """Log a one-time warning when a client's API_VERSION differs from ours.

    Best-effort and never raises — a missing/garbled header is simply ignored,
    so old clients that don't send the header are unaffected.
    """
    raw = request.headers.get(CLIENT_VERSION_HEADER)
    if raw is None:
        return
    try:
        client_api = int(raw)
    except (TypeError, ValueError):
        return
    if client_api == API_VERSION:
        return
    # Attribute the skew to an agent when the bearer token resolves one.
    token = request.headers.get("Authorization", "").split(maxsplit=1)
    agent = _AGENT_TOKENS.get(token[1]) if len(token) == 2 else None
    agent = agent or "unknown"
    key = (agent, client_api)
    if key in _seen_version_skews:
        return
    _seen_version_skews.add(key)
    upgrade = "client (re-sync the skill)" if client_api < API_VERSION else "gateway (git pull + restart)"
    log.warning(
        "API version skew: agent %r speaks v%d, gateway speaks v%d — upgrade the %s.",
        agent, client_api, API_VERSION, upgrade,
    )


# ── Agent authentication ───────────────────────────────────────────────────────

_UNPROTECTED_PATHS = {"/health"}


def _load_agent_tokens() -> dict[str, str]:
    """Parse AGENT_TOKENS env var into a token→agent_name mapping.

    Format: AGENT_TOKENS=claude:tok_abc,gemini:tok_xyz,...
    Returns empty dict if AGENT_TOKENS is not set (auth disabled, backward compat).
    """
    raw = os.environ.get("AGENT_TOKENS", "").strip()
    if not raw:
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            log.warning("AGENT_TOKENS: malformed entry %r (expected name:token)", pair)
            continue
        name, token = pair.split(":", 1)
        name  = name.strip()
        token = token.strip()
        if token in result:
            log.warning(
                "AGENT_TOKENS: token for %r is already assigned to %r — "
                "ignoring duplicate; fix .env to prevent misattribution",
                name, result[token],
            )
            continue
        result[token] = name
    return result


_AGENT_TOKENS: dict[str, str] = _load_agent_tokens()


# ── Read-only roles (e.g. the telemetry monitor) ────────────────────────────────
#
# Routes a "read" role may reach. Everything else — saves, retrospectives,
# search, and the LLM/embeddings proxy passthrough — returns 403 for a read
# token. /health is unauthenticated for everyone (see _UNPROTECTED_PATHS).
# /memory/graph is included because handle_graph already enforces a read-only
# Cypher guard, so a read token cannot mutate Neo4j through it.
_READ_ROLE_ROUTES: set[tuple[str, str]] = {
    ("GET",  "/memory/telemetry"),
    ("POST", "/memory/graph"),
}


def _load_agent_roles() -> dict[str, str]:
    """Parse AGENT_ROLES into an agent_name→role mapping.

    Format: AGENT_ROLES=monitor:read,dashboard:read
    Roles only ever NARROW access — they never grant it (a token must still be a
    valid AGENT_TOKENS entry). The value "read" restricts an agent to
    _READ_ROLE_ROUTES; "full" (or absence from the map) keeps full read/write.
    Unset AGENT_ROLES → every token is full-access (backward compatible).
    """
    raw = os.environ.get("AGENT_ROLES", "").strip()
    if not raw:
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            log.warning("AGENT_ROLES: malformed entry %r (expected name:role)", pair)
            continue
        name, role = (p.strip() for p in pair.split(":", 1))
        if role not in ("read", "full"):
            log.warning(
                "AGENT_ROLES: unknown role %r for %r — expected 'read' or 'full'; "
                "ignoring (agent keeps full access)", role, name,
            )
            continue
        result[name] = role
    return result


_AGENT_ROLES: dict[str, str] = _load_agent_roles()


def _read_role_permits(request: web.Request) -> bool:
    """True if a read-only role may reach this route (exact method+path allowlist)."""
    path = request.path.rstrip("/") or "/"
    return (request.method, path) in _READ_ROLE_ROUTES


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """DEFAULT DENY — every route requires a valid Bearer token unless explicitly allowlisted."""
    _check_client_version(request)  # logs API skew to the gateway log; never raises
    if not _AGENT_TOKENS:
        return await handler(request)
    if request.path.rstrip("/") in _UNPROTECTED_PATHS or request.path in _UNPROTECTED_PATHS:
        return await handler(request)
    raw_header = request.headers.get("Authorization", "")
    parts = raw_header.split(maxsplit=1)
    if len(parts) != 2 or parts[0] != "Bearer":
        raise web.HTTPUnauthorized(reason="Authorization: Bearer <token> required")
    agent_name = _AGENT_TOKENS.get(parts[1])
    if not agent_name:
        raise web.HTTPUnauthorized(reason="Unrecognised token")
    request["authenticated_agent"] = agent_name
    # Read-only roles are confined to the telemetry/graph allowlist. A leaked
    # monitor token therefore cannot save, supersede, or proxy LLM calls.
    if _AGENT_ROLES.get(agent_name) == "read" and not _read_role_permits(request):
        raise web.HTTPForbidden(
            reason="Read-only token: this route requires a write-capable agent token",
        )
    return await handler(request)

# ── Config ────────────────────────────────────────────────────────────────────

_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_DSN   = os.environ.get(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", os.environ.get("NEO4J_PASSWORD", ""))

# Both inference backends are called directly so the coordinator does not
# route through its own auth middleware (which would require a valid token
# for an internal call).  External agents still go through :8888 and must
# authenticate; the coordinator is trusted and bypasses that layer.
EMBED_URL  = "http://localhost:8070/v1/embeddings"
RERANK_URL = "http://localhost:8071/v1/reranking"

EMBED_RETRIES = 4
EMBED_BACKOFF = 0.5      # seconds × attempt number  (0.5 s, 1 s, 1.5 s, 2 s)
POOL_MIN, POOL_MAX = 2, 10

OUTBOX_POLL_INTERVAL = 2.0   # seconds between outbox drain cycles
OUTBOX_BATCH_SIZE    = 20    # rows processed per cycle
OUTBOX_MAX_RETRIES   = 5     # row marked 'failed' after this many Neo4j errors
CONSISTENCY_TIMEOUT  = 15.0  # seconds to wait for ?consistency=neo4j

# NREM dream-cycle backlog gauge (GET /memory/telemetry). A "cycle" is one
# (entity, domain) cluster that meets the consolidation density threshold —
# the unit NREM actually fires on, NOT the raw unconsolidated fact count. Fact
# clusters reuse ONT.density_threshold (the same value consolidation_loop.py
# gates on). Decision clusters track the Phase 3a insight-consolidation design
# (≥2 rem_processed, unconsolidated decisions per (entity, domain)).
DEFAULT_DOMAIN = "general"
NREM_DECISION_THRESHOLD = 2


def _count_domain_cycles(pg_ids: list[int], domain_map: dict[int, str], threshold: int) -> int:
    """Partition pg_ids by domain and count buckets meeting the density threshold.

    Pure function (no I/O) so the per-(entity, domain) gating rule is unit-testable.
    Mirrors consolidation_loop.eligible_domain_clusters' partitioning, but returns
    a count rather than the work items — telemetry needs the gauge, not the payload.
    """
    by_domain: dict[str, int] = {}
    for pid in pg_ids:
        dom = domain_map.get(pid) or DEFAULT_DOMAIN
        by_domain[dom] = by_domain.get(dom, 0) + 1
    return sum(1 for n in by_domain.values() if n >= threshold)

# Cypher write-operation guard — reject queries containing mutating keywords.
# Defence-in-depth: blocks obvious destructive ops while a deeper Neo4j RBAC
# solution is built. Regex is intentionally strict (SET followed by a space
# avoids matching property names that contain "set" as a substring).
_WRITE_CYPHER = re.compile(
    r"\b(CREATE|DELETE|DETACH\s+DELETE|SET\s|REMOVE|MERGE|CALL|LOAD\s+CSV|DROP)\b",
    re.IGNORECASE,
)


def _sigmoid(x: float) -> float:
    """Sigmoid normalization for raw reranker logits → [0, 1]."""
    return 1.0 / (1.0 + math.exp(-x))


def _matched_entities(query: str, metadata: dict | None) -> list[str]:
    """Return entities from metadata whose names appear in the query string."""
    if not metadata or not isinstance(metadata, dict):
        return []
    q = query.lower()
    return [e for e in metadata.get("entities", []) if isinstance(e, str) and e.lower() in q]


def _coerce_jsonb_obj(value):
    """Return a value ready to bind to a JSONB parameter — a Python object, never
    a pre-serialised JSON string.

    The asyncpg pool registers a jsonb codec with ``encoder=json.dumps`` (see
    ``_init_connection``), so every jsonb parameter is serialised exactly once at
    the driver layer. Passing an already-stringified value double-encodes it: the
    row stores a JSON *string scalar* (``jsonb_typeof = 'string'``) instead of an
    object, so ``metadata->>'key'`` silently returns NULL and SQL audits of the
    column find nothing (migration 008 repairs rows written before this guard).
    Some clients also send ``metadata`` as a JSON string; parse it back so it
    stores as a queryable object. Non-JSON strings and non-strings pass through.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


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
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE neo4j_outbox SET status='pending' WHERE status='in_progress'"
            )
            recovered = int(result.split()[-1])
            if recovered:
                log.warning("outbox startup: recovered %d in_progress row(s) → pending", recovered)
        self._neo4j = AsyncGraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        self._outbox_task = asyncio.create_task(self._outbox_worker(), name="outbox-worker")
        log.info("coordinator ready (pool %d–%d, outbox worker running)", POOL_MIN, POOL_MAX)
        if _AGENT_TOKENS:
            log.info(
                "coordinator auth enabled — %d agent(s): %s",
                len(_AGENT_TOKENS), ", ".join(sorted(_AGENT_TOKENS.values())),
            )
            if _AGENT_ROLES:
                log.info(
                    "coordinator read-only roles: %s",
                    ", ".join(f"{n}={r}" for n, r in sorted(_AGENT_ROLES.items())),
                )
            log.info("NOTE: MCP clients (LM Studio) must be fully restarted after .env changes")
        else:
            log.warning("AGENT_TOKENS not set — coordinator running unauthenticated")
            log.warning("Run: uv run python shared-memory/scripts/generate_tokens.py to bootstrap")

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
        # Atomically claim rows by flipping status to 'in_progress' before releasing
        # the lock. A concurrent coordinator instance SKIP LOCKs these rows and moves on.
        # Any rows stuck in 'in_progress' after a crash are reset to 'pending' by start().
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE neo4j_outbox SET status = 'in_progress'
                    WHERE id IN (
                        SELECT id FROM neo4j_outbox
                        WHERE status = 'pending' AND retries < $1
                        ORDER BY id
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, pg_id, cypher_params, retries
                    """,
                    OUTBOX_MAX_RETRIES, OUTBOX_BATCH_SIZE,
                )
                rows = list(rows)
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
            if params.get("type") == "decision":
                await self._apply_decision_outbox_row(outbox_id, pg_id, params)
                return

            if params.get("type") == "retrospective":
                await self._apply_retrospective_outbox_row(outbox_id, pg_id, params)
                return

            # Standard Fact + Entity MERGE — all writes in one round-trip so they
            # succeed or fail atomically. MERGE is idempotent — safe to retry.
            source_ref = params.get("source_ref") or None
            async with self._neo4j.session() as session:
                await session.run(
                    f"MERGE (f:{ONT.fact} {{pg_id: $pg_id}})"
                    f" SET f.content = $content, f.source = $source"
                    + (" SET f.source_ref = $source_ref" if source_ref else "")
                    + f" WITH f"
                    f" UNWIND $entities AS ename"
                    f" MERGE (e:{ONT.entity} {{name: ename}})"
                    f" MERGE (f)-[:{ONT.entity_link}]->(e)",
                    pg_id=pg_id,
                    content=params.get("content_snippet", "")[:200],
                    source=params.get("source", "coordinator"),
                    entities=params.get("entities", []),
                    **( {"source_ref": source_ref} if source_ref else {} ),
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
                    await conn.execute(
                        "UPDATE neo4j_outbox SET retries=retries+1, status='pending' WHERE id=$1",
                        outbox_id,
                    )

    async def _apply_decision_outbox_row(
        self, outbox_id: int, pg_id: int, params: dict
    ) -> None:
        """
        Materialise a Decision node and its PROV-O edges in Neo4j.

        Creates: Decision, Human (decided_by), Project, AIAgent(s) (assisted_by),
        and Entity nodes for each name in entities.  FOREACH handles empty lists
        so the query is safe regardless of whether assisted_by or entities are set.
        All writes in one session — atomic on transient failures (MERGE is idempotent).
        """
        decision = params.get("decision", {})
        async with self._neo4j.session() as session:
            await session.run(
                f"MERGE (d:{ONT.decision} {{pg_id: $pg_id}})"
                f"  SET d.title     = $title,"
                f"      d.rationale = $rationale,"
                f"      d.date      = $date,"
                f"      d.source    = $source"
                f" WITH d"
                f" MERGE (h:{ONT.human} {{name: $decided_by}})"
                f" MERGE (d)-[:{ONT.was_attributed_to}]->(h)"
                f" WITH d"
                f" MERGE (p:{ONT.project} {{name: $project}})"
                f" MERGE (d)-[:{ONT.project_of}]->(p)"
                f" WITH d"
                f" FOREACH (ai_name IN $assisted_by |"
                f"   MERGE (a:{ONT.ai_agent} {{name: ai_name}})"
                f"   MERGE (d)-[:{ONT.was_assisted_by}]->(a)"
                f" )"
                f" WITH d"
                f" FOREACH (ename IN $entities |"
                f"   MERGE (e:{ONT.entity} {{name: ename}})"
                f"   MERGE (d)-[:{ONT.entity_link}]->(e)"
                f" )",
                pg_id=pg_id,
                title=decision.get("title", params.get("content_snippet", "")[:100]),
                rationale=decision.get("rationale", ""),
                date=decision.get("date", ""),
                source=params.get("source", "coordinator"),
                decided_by=decision.get("decided_by", "unknown"),
                project=decision.get("project", "unknown"),
                assisted_by=decision.get("assisted_by", []),
                entities=params.get("entities", []),
            )
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE neo4j_outbox SET status='applied', applied_at=now() WHERE id=$1",
                outbox_id,
            )
        log.debug("outbox: applied decision pg_id=%d (outbox_id=%d)", pg_id, outbox_id)

    async def _apply_retrospective_outbox_row(
        self, outbox_id: int, pg_id: int, params: dict
    ) -> None:
        """Materialise a HAD_OUTCOME self-loop on an existing Decision node in Neo4j.

        Each call creates a new dated edge — multiple retrospectives per decision are allowed.
        MATCH only (no MERGE) so a missing Decision surfaces as a no-op rather than a phantom node.
        """
        retro = params.get("retrospective", {})
        async with self._neo4j.session() as session:
            await session.run(
                f"MATCH (d:{ONT.decision} {{pg_id: $pg_id}})"
                f" CREATE (d)-[:{ONT.had_outcome} {{rating: $rating, date: $date, notes: $notes}}]->(d)",
                pg_id=pg_id,
                rating=retro.get("rating", ""),
                date=retro.get("date", ""),
                notes=retro.get("notes", ""),
            )
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE neo4j_outbox SET status='applied', applied_at=now() WHERE id=$1",
                outbox_id,
            )
        log.debug("outbox: applied retrospective pg_id=%d (outbox_id=%d)", pg_id, outbox_id)

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
        metadata   = _coerce_jsonb_obj(body.get("metadata", {}))
        agent_id   = body.get("agent_id", "unknown")
        scope      = body.get("scope", "global")
        visibility = body.get("visibility", "global")

        # Server-side identity enforcement — verified agent name overrides client claim.
        # body["metadata"] is explicitly reattached; dict.get() returns an independent
        # dict when the key is absent, so the mutation would otherwise be discarded.
        if request.get("authenticated_agent"):
            metadata["source"] = request["authenticated_agent"]
            body["metadata"] = metadata

        if not content:
            return web.json_response(
                {"status": "error", "message": "content is required"}, status=400
            )
        if not isinstance(metadata, dict):
            return web.json_response(
                {"status": "error", "message": "metadata must be a JSON object"}, status=400
            )
        if not metadata.get("source"):
            return web.json_response(
                {
                    "status": "error",
                    "message": (
                        "metadata.source is required — use the agent or model name "
                        "(e.g. 'claude_code', 'grok', 'qwen3-27b'). "
                        "Facts without provenance are rejected to protect memory integrity."
                    ),
                },
                status=400,
            )

        # Decision saves require structured provenance fields — validated at ingress
        # before the row touches the outbox WAL.  Bad data from an LLM is rejected
        # here rather than replayed on every restart from a corrupt outbox entry.
        if metadata.get("type") == "decision":
            decision_data = metadata.get("decision", {})
            missing = [
                f for f in ("decided_by", "project", "rationale")
                if not decision_data.get(f)
            ]
            if missing:
                return web.json_response(
                    {
                        "status": "error",
                        "message": (
                            f"decision save missing required fields: {missing}. "
                            "Include a 'decision' object in metadata with "
                            "'decided_by', 'project', and 'rationale'."
                        ),
                    },
                    status=400,
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
                        content, metadata, str(embedding),
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
                        {
                            "content_snippet": content[:200],
                            "source": metadata.get("source", "coordinator"),
                            "entities": entities,
                            "agent_id": agent_id,
                            "type": metadata.get("type", "fact"),
                            "decision": metadata.get("decision", {}),
                            "source_ref": metadata.get("source_ref") or None,
                        },
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

    # ── POST /memory/retrospective ────────────────────────────────────────────

    async def handle_retrospective(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )

        pg_id    = body.get("pg_id")
        rating   = body.get("rating", "")
        notes    = body.get("notes", "")
        date     = body.get("date") or datetime.now().date().isoformat()
        agent_id = body.get("agent_id", "unknown")

        if not isinstance(pg_id, int) or not rating or not notes:
            return web.json_response(
                {"status": "error", "message": "pg_id (int), rating, and notes are required"},
                status=400,
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id FROM technical_docs WHERE id=$1 FOR SHARE",
                    pg_id,
                )
                if not row:
                    return web.json_response(
                        {"status": "error", "message": f"No record found with pg_id={pg_id}"},
                        status=404,
                    )
                await conn.execute(
                    "INSERT INTO neo4j_outbox (pg_id, cypher_params) VALUES ($1, $2::jsonb)",
                    pg_id,
                    {
                        "type": "retrospective",
                        "target_pg_id": pg_id,
                        "retrospective": {"rating": rating, "date": date, "notes": notes},
                        "source": agent_id,
                    },
                )

        return web.json_response({"status": "success", "target_pg_id": pg_id})

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
                        {
                            "tier": "fact",
                            "content": r["content"],
                            "score": 0.0,
                            "score_normalized": 0.5,
                            "matched_entities": _matched_entities(query, r["metadata"]),
                            "metadata": r["metadata"],
                            "graph_context": [],
                        }
                        for r in rows
                    ],
                })

            async with self._pool.acquire() as conn:
                # Tier 3 — top active (non-superseded) community summary.
                # Guard: if migration 006 has not been applied, fall back to the
                # unsupervised query so search continues to work (with a warning).
                try:
                    summary = await conn.fetchrow(
                        "SELECT content, metadata, source_pg_ids FROM community_summaries"
                        " WHERE NOT superseded"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec),
                    )
                except Exception:
                    log.warning(
                        "community_summaries.superseded column missing — "
                        "run migrations: uv run --with psycopg2-binary "
                        "python shared-memory/migrations/apply.py"
                    )
                    summary = await conn.fetchrow(
                        "SELECT content, metadata, source_pg_ids FROM community_summaries"
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
            # Surface the summary's provenance so an agent can trace a Tier-3
            # narrative back to the exact Tier-1 facts it was synthesised from
            # (source_pg_ids) — drill down via /memory/graph or status/{pg_id}.
            final.append({
                "tier": "community_summary",
                "content": summary["content"],
                "score": None,
                "score_normalized": None,
                "matched_entities": [],
                "metadata": summary["metadata"],
                "source_pg_ids": summary["source_pg_ids"],
                "graph_context": [],
            })

        async with self._neo4j.session() as session:
            for hit in ranked:
                idx   = hit["index"]
                pg_id = ids[idx]
                raw_score = hit["relevance_score"]
                ctx: list[dict] = []
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
                            ctx.append({
                                "rel_type": rec["rel_type"],
                                "name": rec["name"],
                                "label": rec["labels"][0] if rec["labels"] else None,
                            })
                except Exception:
                    pass
                final.append({
                    "tier": "fact",
                    "content": contents[idx],
                    "score": raw_score,
                    "score_normalized": _sigmoid(raw_score),
                    "matched_entities": _matched_entities(query, metas[idx]),
                    "metadata": metas[idx],
                    "graph_context": ctx,
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
            async with self._neo4j.session(default_access_mode="READ") as session:
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

    # ── GET /memory/telemetry ─────────────────────────────────────────────────

    async def handle_telemetry(self, request: web.Request) -> web.Response:
        """Operational telemetry snapshot — pull-based rollup of the state that
        matters day to day: outbox health, the REM/NREM dream-cycle backlog,
        consolidation-cycle counts, and a metadata breakdown. Each section is
        computed independently so a partial backend failure still returns
        whatever the others can.

        This endpoint is the single read-only source of truth for the pipeline:
        a read-scoped client (e.g. the Shared Memory Monitor) can render the
        whole live dashboard from here without any direct Postgres or Neo4j
        credentials — the coordinator owns both backends and does the joins.
        """
        from datetime import datetime, timezone
        snap: dict = {"timestamp": datetime.now(timezone.utc).isoformat()}

        # Postgres — outbox status, doc + summary counts
        try:
            async with self._pool.acquire() as conn:
                outbox = await conn.fetch(
                    "SELECT status, count(*) AS n FROM neo4j_outbox GROUP BY status"
                )
                docs = await conn.fetchval("SELECT count(*) FROM technical_docs")
                summ = await conn.fetchrow(
                    "SELECT count(*) AS total,"
                    " count(*) FILTER (WHERE superseded) AS superseded,"
                    " count(*) FILTER (WHERE metadata->>'kind'='insight') AS insight"
                    " FROM community_summaries"
                )
            snap["postgres"] = {
                "technical_docs": docs,
                "outbox": {r["status"]: r["n"] for r in outbox},
                "community_summaries": {
                    "total": summ["total"],
                    "superseded": summ["superseded"],
                    "insight": summ["insight"],
                },
            }
        except Exception as exc:
            snap["postgres"] = {"error": str(exc)}

        # Neo4j — REM/NREM backlog for facts and decisions
        try:
            async with self._neo4j.session() as session:
                fres = await session.run(
                    f"MATCH (f:{ONT.fact}) WHERE f.pg_id IS NOT NULL"
                    f" RETURN coalesce(f.rem_processed,false) AS rem,"
                    f"        coalesce(f.consolidated,false) AS con, count(*) AS n"
                )
                facts = await fres.data()
                dres = await session.run(
                    f"MATCH (d:{ONT.decision})"
                    f" RETURN coalesce(d.rem_processed,false) AS rem, count(*) AS n"
                )
                decisions = await dres.data()
            snap["neo4j"] = {
                "facts_total":          sum(r["n"] for r in facts),
                "facts_rem_pending":    sum(r["n"] for r in facts if not r["rem"]),
                "facts_unconsolidated": sum(r["n"] for r in facts if r["rem"] and not r["con"]),
                "decisions_total":      sum(r["n"] for r in decisions),
                "decisions_rem_pending": sum(r["n"] for r in decisions if not r["rem"]),
            }
        except Exception as exc:
            snap["neo4j"] = {"error": str(exc)}

        # NREM dream-cycle backlog — pending consolidation CYCLES, not raw facts.
        # One cycle per (entity, domain) cluster meeting the density threshold.
        # Needs both backends: Neo4j supplies the rem_processed/unconsolidated
        # clusters; Postgres supplies the authoritative domain per pg_id (the
        # Fact node has no domain). This is the join a read-only client cannot
        # do itself — hence it lives here.
        try:
            snap["nrem"] = await self._nrem_cycle_counts()
        except Exception as exc:
            snap["nrem"] = {"error": str(exc)}

        # Metadata breakdown — drill-down distributions a dashboard renders
        # (record types, agents, sources, domains, summary kinds). Cheap GROUP
        # BYs over technical_docs + community_summaries; surfaced here so the
        # monitor needs no direct Postgres connection for its breakdown panels.
        try:
            snap["breakdown"] = await self._metadata_breakdown()
        except Exception as exc:
            snap["breakdown"] = {"error": str(exc)}

        return web.json_response({"status": "success", "telemetry": snap})

    async def _nrem_cycle_counts(self) -> dict:
        """Pending NREM consolidation cycles for facts and decisions.

        Reproduces consolidation_loop's gating: entity clusters of
        rem_processed, unconsolidated nodes, re-partitioned per (entity, domain)
        and counted only where a bucket meets its density threshold.
        """
        # Neo4j: entity clusters of eligible facts (global, not just pending ids).
        async with self._neo4j.session() as session:
            fres = await session.run(
                f"MATCH (f:{ONT.fact}) WHERE f.pg_id IS NOT NULL"
                f" MATCH (f)-[:{ONT.entity_link_alias}|{ONT.entity_link}]->(e:{ONT.entity})"
                f" WITH DISTINCT e"
                f" MATCH (e)<-[:{ONT.entity_link_alias}|{ONT.entity_link}]-(n:{ONT.fact})"
                f" WHERE coalesce(n.consolidated,false) = false"
                f"   AND coalesce(n.rem_processed,false) = true"
                f" WITH e, collect(n.pg_id) AS pg_ids"
                f" WHERE size(pg_ids) >= $threshold"
                f" RETURN e.name AS entity, pg_ids",
                threshold=ONT.density_threshold,
            )
            fact_clusters = await fres.data()
            dres = await session.run(
                f"MATCH (d:{ONT.decision})"
                f" WHERE coalesce(d.rem_processed,false) = true"
                f"   AND coalesce(d.consolidated,false) = false"
                f" RETURN collect(d.pg_id) AS pg_ids"
            )
            drows = await dres.data()
        decision_ids = [int(x) for x in (drows[0]["pg_ids"] if drows else []) if x is not None]

        # Postgres: authoritative domain per pg_id across all eligible nodes.
        all_ids = sorted(
            {int(pid) for c in fact_clusters for pid in (c["pg_ids"] or []) if pid is not None}
            | set(decision_ids)
        )
        domain_map: dict[int, str] = {}
        if all_ids:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, COALESCE(metadata->>'project', metadata->>'domain',"
                    " scope, $2) AS domain FROM technical_docs WHERE id = ANY($1)",
                    all_ids, DEFAULT_DOMAIN,
                )
            domain_map = {r["id"]: r["domain"] for r in rows}

        fact_cycles = sum(
            _count_domain_cycles(
                [int(pid) for pid in (c["pg_ids"] or []) if pid is not None],
                domain_map, ONT.density_threshold,
            )
            for c in fact_clusters
        )
        decision_cycles = _count_domain_cycles(
            decision_ids, domain_map, NREM_DECISION_THRESHOLD
        )
        return {
            "fact_cycles": fact_cycles,
            "decision_cycles": decision_cycles,
            "total_cycles": fact_cycles + decision_cycles,
            "fact_threshold": ONT.density_threshold,
            "decision_threshold": NREM_DECISION_THRESHOLD,
        }

    async def _metadata_breakdown(self) -> dict:
        """Distribution counts a dashboard renders, sourced server-side so a
        read-only client needs no direct Postgres access.
        """
        async with self._pool.acquire() as conn:
            record_types = await conn.fetch(
                "SELECT COALESCE(metadata->>'type','(untagged)') AS key,"
                " count(*)::int AS count FROM technical_docs GROUP BY 1 ORDER BY count DESC"
            )
            agents = await conn.fetch(
                "SELECT agent_id AS key, count(*)::int AS count"
                " FROM technical_docs GROUP BY 1 ORDER BY count DESC LIMIT 12"
            )
            sources = await conn.fetch(
                "SELECT COALESCE(metadata->>'source','(none)') AS key,"
                " count(*)::int AS count FROM technical_docs GROUP BY 1 ORDER BY count DESC LIMIT 12"
            )
            domains = await conn.fetch(
                "SELECT COALESCE(metadata->>'project', metadata->>'domain', scope, 'general') AS key,"
                " count(*)::int AS count FROM technical_docs GROUP BY 1 ORDER BY count DESC LIMIT 12"
            )
            summaries = await conn.fetch(
                "SELECT COALESCE(metadata->>'kind','community_summary') AS kind,"
                " count(*) FILTER (WHERE superseded)::int AS superseded,"
                " count(*) FILTER (WHERE NOT superseded)::int AS active"
                " FROM community_summaries GROUP BY 1 ORDER BY active DESC"
            )
        kv = lambda rows: [{"key": r["key"], "count": r["count"]} for r in rows]
        return {
            "record_types": kv(record_types),
            "agents": kv(agents),
            "sources": kv(sources),
            "domains": kv(domains),
            "summaries": [
                {"kind": r["kind"], "superseded": r["superseded"], "active": r["active"]}
                for r in summaries
            ],
        }


# ── Registration ──────────────────────────────────────────────────────────────

def attach(app: web.Application, coordinator: MemoryCoordinator) -> None:
    """Register /memory/* routes on an aiohttp Application.

    Must be called before the proxy catch-all route so these exact-path routes
    take precedence. To extract the coordinator into a standalone process
    (Phase 4), only this call site changes.
    """
    app.router.add_post("/memory/save",           coordinator.handle_save)
    app.router.add_post("/memory/retrospective",  coordinator.handle_retrospective)
    app.router.add_post("/memory/search",         coordinator.handle_search)
    app.router.add_post("/memory/graph",          coordinator.handle_graph)
    app.router.add_get( "/memory/status/{pg_id}", coordinator.handle_status)
    app.router.add_get( "/memory/telemetry",       coordinator.handle_telemetry)
