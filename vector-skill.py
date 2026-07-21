"""
Vector Skill — MCP server exposing the shared memory to an MCP host (LM Studio).

THIN CLIENT (ADR-014). This process owns no database connections. Every
operation is an HTTP call to the Hive-Mind Gateway on :8888, which is the single
component that talks to Postgres and Neo4j.

That was not always true, and the reason it matters is not tidiness. This server
used to run its own copy of the retrieval chain — its own vector query, its own
Tier-3 lookup, its own graph expansion — straight against the databases. Three
consequences, all of them real:

  * READ AUTHORIZATION WAS BYPASSED. The gateway applies a visibility predicate
    to every read (`global`, own `private`, matching `scope`). A direct
    `SELECT ... FROM technical_docs WHERE NOT superseded` applies none, so this
    host could retrieve other agents' private records and scope-restricted rows.
    A second implementation of a read path is a second implementation of its
    access control, and this one simply did not have any.
  * IT DRIFTED. Every retrieval improvement had to be made twice, and in
    practice was made once — so this host silently served months-old ranking
    behaviour while every other agent got the current chain.
  * IT IMPORTED SERVER MODULES. The Cypher it built needed `ontology`, pulled in
    off `shared-memory/scripts`, which is the operations surface and is not
    shipped to clients.

So: search, graph queries, lineage and saves all go through the gateway, and
this file holds rendering plus the MCP tool surface. Nothing else.

MCP tools: hybrid_search_and_rerank, save_artifact, archive_reasoning_trace,
save_decision, save_retrospective, supersede, review_hold, check_memory_health,
memory_telemetry, record_lineage, graph_query, review_edges, label_edges.
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

import httpx
from fastmcp import FastMCP

# Load .env from the same directory as this script so credentials are available
# when LM Studio (or any MCP host) spawns this process without inheriting the shell env.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # python-dotenv not installed; rely on env vars being set externally

# Configure logging to stderr for MCP visibility
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RAG_Orchestrator")

mcp = FastMCP("Local_RAG_Orchestrator")

# The one endpoint this process talks to. Env-overridable like every other
# endpoint in the framework — never assume the bundled port layout.
COORDINATOR_BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
AGENT_ID = os.environ.get("AGENT_ID", "vector_skill")

# Wire contract this MCP server speaks on its /memory/* gateway calls. Keep in
# step with API_VERSION in coordinator.py / memory_bridge.py — the gateway logs
# a warning (coordinator._check_client_version) if they disagree.
# v3: review_edges / label_edges require the gateway's /memory/relations/* routes.
API_VERSION = 3
VERSION = "0.8.2"
CLIENT_VERSION_HEADER = "X-SM-Api-Version"

# Constants that MUST mirror the gateway's (a thin client never imports server
# modules, so they are restated here and kept in step by review).
# relation_confidence.FAMILIES:
RELATION_FAMILIES = ("entity_relation", "evidential")
# ontology.RETRO_RATINGS — outcome STATES, not valence:
RETRO_RATINGS = ("validated", "mixed", "refined", "pending", "reversed")
# Record types that may qualify a reference. A record id is unique only WITHIN
# its table — technical_docs and community_summaries run independent sequences —
# so a bare integer lifted off a summary result resolves against the wrong table
# and returns a confident, unrelated record (decision 822).
RECORD_TYPES = ("fact", "decision", "retrospective", "summary", "insight")

SEARCH_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
CALL_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


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
        print(f"[WARN] shared-memory: audit log unavailable ({e})", file=sys.stderr)
    except Exception:
        pass  # logging must never break the save path


def _unavailable(exc: Exception) -> str:
    """Uniform message when the gateway cannot be reached. The gateway is the
    only path to memory now, so this is a hard failure rather than a degraded
    mode — saying so plainly beats silently returning nothing."""
    return (f"Error: memory gateway unreachable at {COORDINATOR_BASE} ({exc}). "
            "Start it with: systemctl --user start hive-mind-gateway.service")


def _auth_rejected(tool: str) -> str:
    _append_log(tool, 2, "auth_failed",
                {"hint": "Check AGENT_TOKEN in this skill's .env matches a gateway AGENT_TOKENS entry"})
    return ("Error: the gateway rejected this client's token. Set AGENT_TOKEN in "
            "the .env beside vector-skill.py (or in the mcp.json env block).")


def _valid_ref(ref: str) -> bool:
    """A bare id, or a qualified `type:id` reference (decision 822)."""
    head, _, tail = str(ref).partition(":")
    if tail:
        return tail.lstrip("-").isdigit() and head.lower() in RECORD_TYPES
    return str(ref).lstrip("-").isdigit()


# ── Rendering ────────────────────────────────────────────────────────────────

def _render_results(results: list, elapsed: float) -> str:
    """Render the gateway's search response for an MCP host.

    Every result carries the gateway's own `ref` (`fact:816`, `summary:87`) and
    `record_type`. Those are surfaced verbatim rather than reduced to a bare
    integer, because the bare integer is exactly what makes a follow-up lookup
    resolve against the wrong table.
    """
    if not results:
        return "Result: No relevant documentation found."

    # Tier-3 narratives (thematic summaries, cross-project insights) lead, as
    # the gateway ordered them; the precision facts follow with their scores.
    tier3 = [r for r in results if r.get("record_type") in ("summary", "insight")]
    tier1 = [r for r in results if r not in tier3]

    out = []
    for r in tier3:
        kind = "Insight (cross-project principle)" if r.get("record_type") == "insight" \
               else "Global Context Summary"
        head = f"### {kind}  [{r.get('ref', r.get('pg_id'))}]"
        src = r.get("source_pg_ids") or []
        if src:
            head += f"\n_synthesised from {len(src)} record(s)_"
        out.append(f"{head}\n{r.get('content', '')}")

    body = []
    for r in tier1:
        meta = r.get("metadata") or {}
        source = meta.get("source", "unknown") if isinstance(meta, dict) else "unknown"
        score = r.get("score")
        bits = [f"Ref: {r.get('ref', r.get('pg_id'))}", f"Source: {source}"]
        if score is not None:
            bits.insert(0, f"Score: {score:.2f}")
        line = f"[{' | '.join(bits)}]"
        gc = r.get("graph_context")
        if gc:
            line += f"\n[Graph Context]: {gc if isinstance(gc, str) else json.dumps(gc)}"
        ents = r.get("matched_entities")
        if ents:
            line += f"\n[Matched entities]: {', '.join(map(str, ents))}"
        body.append(f"{line}\n{r.get('content', '')}")

    header = f"### Unified Memory Results ({len(tier1)} item(s) found in {elapsed:.2f}s)\n\n"
    parts = out + [header + "\n\n---\n\n".join(body)] if body else out
    return "\n\n---\n\n".join(parts)


# ── Retrieval ────────────────────────────────────────────────────────────────

@mcp.tool()
async def hybrid_search_and_rerank(query: str, limit: int = 5) -> str:
    """
    Search the shared memory: Tier-3 thematic/insight narratives for orientation,
    Tier-1 facts for precision, expanded through the entity graph.

    Delegates the whole retrieval chain — embedding, vector search, reranking,
    graph expansion, and READ AUTHORIZATION — to the gateway, so this host sees
    exactly what every other agent sees, and only what it is permitted to see.
    """
    logger.info(f"Search: {query[:50]}...")
    start = datetime.now()
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/search",
                json={"query": query, "limit": limit, "agent_id": AGENT_ID},
                headers=_auth_headers(),
            )
            if r.status_code == 401:
                return _auth_rejected("vector_skill")
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:
        logger.error(f"Search failed: {exc}")
        return _unavailable(exc)

    results = payload.get("results", payload)
    if isinstance(results, dict) and results.get("status") == "error":
        return f"Error: {results.get('message', 'search failed')}"
    return _render_results(results if isinstance(results, list) else [],
                           (datetime.now() - start).total_seconds())


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

    Supersede-on-save: include "supersedes": <old_pg_id> in metadata_json to save
    this as a CORRECTION that retires an older fact in one call (the old fact is
    kept but flagged + hidden from search). To retract a fact WITHOUT a
    replacement, use the `supersede` tool instead.
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

    coordinator_url = COORDINATOR_BASE
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
    Archive the agent's reasoning path as a memory record.

    `steps` is a list of dicts: [{'thought': ..., 'tool': ..., 'result': ...}].

    This used to CREATE ReasoningTrace/ReasoningStep nodes straight in Neo4j.
    A client writing its own subgraph bypasses the outbox — which is what makes
    a save atomic across Postgres and Neo4j — and bypasses read authorization,
    so the trace was durable in one store only and visible to everyone. It is
    now saved through the normal save path: one record, embedded, access-
    controlled, searchable, and eligible for consolidation like any other.
    """
    if not steps:
        return "Error: no steps to archive."
    lines = [f"Reasoning trace for task: {task}", ""]
    for i, step in enumerate(steps):
        lines.append(f"{i + 1}. Thought: {step.get('thought', '')}")
        if step.get("tool"):
            lines.append(f"   Tool: {step['tool']}")
        if step.get("result") is not None:
            lines.append(f"   Result: {step['result']}")
    content = "\n".join(lines)
    metadata = {
        "source": os.environ.get("AGENT_ID", "vector_skill"),
        "type": "reasoning_trace",
        "session_id": session_id,
        "task": task,
        "step_count": len(steps),
    }
    return await save_artifact(content, json.dumps(metadata))


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

    coordinator_url = COORDINATOR_BASE
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
    Record an outcome for an existing Decision as a full retrospective record
    (own searchable record + Retrospective node behind the decision's
    HAD_OUTCOME trigger edge).

    Use this after a decision has been acted on to close the Why-To loop.
    Multiple retrospectives per decision are allowed — the newest is the
    decision's current verdict.

    Required: pg_id (returned by save_decision), rating, notes, source.
    rating is a closed outcome-state enum: validated | mixed | refined |
    pending | reversed ('reversed' supersedes the decision; nuance goes in notes).
    Optional: date (ISO string, default: today).
    """
    coordinator_url = COORDINATOR_BASE
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
        own = result.get("pg_id")
        own_note = f" (record pg_id={own})" if own else ""
        return f"Retrospective recorded on Decision pg_id={result['target_pg_id']}{own_note}."

    return f"Error: {result.get('message', result)}"


@mcp.tool()
async def supersede(pg_id: int, by: int = 0) -> str:
    """
    Retract / supersede an existing fact (decision 381/384). Soft — the old fact
    is KEPT (provenance) but flagged, hidden from search, and excluded from
    consolidation. Supersession is EXPLICIT; never infer it from similarity.

    Use when a stored fact is wrong or outdated and you are NOT saving a
    replacement in the same call. To save a correction that supersedes an old
    fact in one step, instead call save_artifact with "supersedes": <old_pg_id>
    in its metadata_json.

    Required: pg_id (the fact to retract).
    Optional: by (pg_id of an existing successor fact to point at; omit / 0 = none).
    """
    coordinator_url = COORDINATOR_BASE
    payload = {"pg_id": pg_id}
    if by and by > 0:
        payload["by"] = by
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/supersede",
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
        return result.get("message", f"Fact {pg_id} superseded.")
    return f"Error: {result.get('message', result)}"


@mcp.tool()
async def review_hold(summary_id: int, pg_id: int) -> str:
    """
    Mark a summary's flagged stale source as reviewed-and-held (decision 384).

    When a search result carries a stale_sources warning (a summary/insight was
    synthesised from a since-superseded fact) and you judge the change immaterial,
    call this so the warning stops re-surfacing for that summary. A later
    supersession of a DIFFERENT source still surfaces.

    Required: summary_id (the community_summaries id), pg_id (the superseded
    source fact to acknowledge).
    """
    coordinator_url = COORDINATOR_BASE
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/review_hold",
                json={"summary_id": summary_id, "pg_id": pg_id},
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
        return result.get("message", f"Summary {summary_id}: supersession of {pg_id} held.")
    return f"Error: {result.get('message', result)}"


@mcp.tool()
async def check_memory_health() -> str:
    """
    Full-stack diagnostic for the shared-memory infrastructure.

    Reports what the gateway reports — embedder, reranker, reasoning backends,
    both dream daemons, and consolidation liveness — rather than opening its own
    database connection to count rows. The gateway is the component that knows
    whether the stack is healthy; asking it is also the only check that
    exercises the path this client actually uses.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{COORDINATOR_BASE}/health",
                                    headers=_auth_headers())
        if resp.status_code == 401:
            return _auth_rejected("vector_skill")
        payload = resp.json()
    except Exception as exc:
        return json.dumps({"status": "unreachable",
                           "gateway": COORDINATOR_BASE,
                           "error": str(exc),
                           "hint": "systemctl --user start hive-mind-gateway.service"},
                          indent=2)
    payload["client"] = {"tool": "vector-skill", "version": VERSION,
                         "api_version": API_VERSION}
    gw_api = payload.get("api_version")
    if gw_api is not None and gw_api != API_VERSION:
        payload["client"]["version_skew"] = (
            f"this client speaks v{API_VERSION}, gateway speaks v{gw_api} — "
            "upgrade whichever is older")
    return json.dumps(payload, indent=2, default=str)


@mcp.tool()
async def memory_telemetry() -> str:
    """Operational telemetry snapshot from the gateway (GET /memory/telemetry).

    Pull-based rollup the coordinator computes over both backends: outbox health,
    REM/NREM dream-cycle backlog, NREM consolidation-cycle counts (`nrem`), and
    metadata distributions (`breakdown`). Use this to see whether the dream cycle
    has work pending or is caught up — it is the same snapshot the CLI agents get
    via `memory_bridge.py status`. Read-only; no direct database access needed.
    """
    coordinator_url = COORDINATOR_BASE
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{coordinator_url}/memory/telemetry", headers=_auth_headers()
            )
        if resp.status_code == 401:
            return "Error: coordinator rejected token (check AGENT_TOKEN in mcp.json)."
        if resp.status_code >= 400:
            return f"Error: coordinator returned HTTP {resp.status_code}."
        return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return f"Error: gateway unreachable — {e}"


# ── Reads that the CLI skill already had and this surface did not ────────────

@mcp.tool()
async def record_lineage(ref: str) -> str:
    """
    "What happened to this record?" — its state, its dream-cycle stamps
    (applied → rem_reviewed → consolidated), and which summary it was folded
    into, with the fact→summary latency.

    `ref` takes a bare id or a QUALIFIED reference: "fact:816", "decision:840",
    "summary:87". Prefer the qualified form and take it verbatim from a search
    result. A record id is unique only within its table, and facts and summaries
    run independent sequences — so a bare integer lifted off a summary result
    resolves against the facts table and returns a confident, unrelated record.
    """
    ref = str(ref).strip()
    if not _valid_ref(ref):
        return ("Error: ref must be a bare id or type:id, where type is one of "
                + ", ".join(RECORD_TYPES))
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
            r = await client.get(f"{COORDINATOR_BASE}/memory/status/{ref}",
                                 headers=_auth_headers())
            if r.status_code == 401:
                return _auth_rejected("vector_skill")
            return json.dumps(r.json(), indent=2, default=str)
    except Exception as exc:
        return _unavailable(exc)


@mcp.tool()
async def graph_query(cypher: str) -> str:
    """
    Run a READ-ONLY Cypher query against the knowledge graph.

    The gateway enforces read-only: CREATE, DELETE, DETACH DELETE, SET, MERGE,
    CALL, LOAD CSV and DROP are rejected there, not here — a client-side check
    would be advisory only.
    """
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
            r = await client.post(f"{COORDINATOR_BASE}/memory/graph",
                                  json={"cypher": cypher, "params": {}},
                                  headers=_auth_headers())
            if r.status_code == 401:
                return _auth_rejected("vector_skill")
            payload = r.json()
    except Exception as exc:
        return _unavailable(exc)
    return json.dumps(payload.get("records", payload), indent=2, default=str)


# ── Relation adjudication (API v3) ───────────────────────────────────────────

@mcp.tool()
async def review_edges(family: str = "entity_relation", limit: int = 20) -> str:
    """
    Fetch machine-proposed graph edges awaiting operator adjudication.

    `family` is one of: entity_relation, evidential. Each family calibrates on
    its own operator-label curve, so they are reviewed separately.
    """
    if family not in RELATION_FAMILIES:
        return f"Error: family must be one of {', '.join(RELATION_FAMILIES)}"
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
            r = await client.get(
                f"{COORDINATOR_BASE}/memory/relations/review",
                params={"family": family, "limit": limit},
                headers=_auth_headers())
            if r.status_code == 401:
                return _auth_rejected("vector_skill")
            return json.dumps(r.json(), indent=2, default=str)
    except Exception as exc:
        return _unavailable(exc)


@mcp.tool()
async def label_edges(labels_json: str, promote: list = None) -> str:
    """
    Record operator labels on proposed edges — the calibration signal.

    `labels_json` maps adjudication id to verdict, e.g.
    '{"12": "correct", "13": "incorrect"}'. `promote` optionally lists ids to
    promote to operator-asserted edges.
    """
    try:
        labels = json.loads(labels_json)
        if not isinstance(labels, dict):
            raise ValueError("labels_json must be a JSON object")
    except Exception as exc:
        return f"Error: could not parse labels_json ({exc})"
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/relations/label",
                json={"labels": labels, "promote": promote or []},
                headers=_auth_headers())
            if r.status_code == 401:
                return _auth_rejected("vector_skill")
            return json.dumps(r.json(), indent=2, default=str)
    except Exception as exc:
        return _unavailable(exc)


if __name__ == "__main__":
    mcp.run()
