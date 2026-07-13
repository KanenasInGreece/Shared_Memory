"""
Memory Bridge — thin CLI client for the Memory Coordinator.

Delegates all storage I/O to the coordinator running inside hive_mind_proxy
on port 8888. Direct Postgres and Neo4j access has been removed; the
coordinator owns those connections.

CLI usage:
    python memory_bridge.py --version
    python memory_bridge.py save   "<content>" '<metadata_json>'
    python memory_bridge.py search "<query>" [limit]
    python memory_bridge.py graph  "<cypher>"
    python memory_bridge.py save_decision --title "..." --decided-by "..." \
        --project "..." --rationale "..." [--source "..."] \
        [--assisted-by "a,b"] [--alternatives "x,y"] \
        [--confidence "high"] [--entities "E1,E2"]
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime

import httpx

VERSION = "0.6.3"
# Wire contract this client was built against. Must match the gateway's
# api_version (reported by GET /health). Bump only on breaking protocol changes.
API_VERSION = 1
CLIENT_VERSION_HEADER = "X-SM-Api-Version"

# Two-source dotenv search — both sources tried; first definition wins.
# Always invoke memory_bridge.py by absolute path so __file__ resolves
# to the skill directory (e.g. ~/.gemini/skills/shared-memory/scripts/).
#   1. find_dotenv() — searches parent dirs from the script's location
#   2. script-adjacent .env — covers ~/.{agent}/skills/shared-memory/.env
try:
    from dotenv import find_dotenv, load_dotenv
    for _env in (
        find_dotenv(usecwd=False),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ):
        if _env and os.path.exists(_env):
            load_dotenv(_env, override=False)
except ImportError:
    # python-dotenv not installed — manually parse skill-adjacent .env files
    # so auth tokens are found when running bare `python` or `uv run --with httpx`
    def _read_env_file(path: str) -> None:
        try:
            with open(path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    if _k and _k not in os.environ:   # first definition wins
                        os.environ[_k] = _v.strip()
        except OSError:
            pass
    # covers ~/.{agent}/skills/shared-memory/scripts/.env and the parent dir
    _read_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    _read_env_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

COORDINATOR_BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
AGENT_ID         = os.environ.get("AGENT_ID", "memory_bridge")


def _uds_path() -> str | None:
    """The gateway Unix socket to connect over, so the gateway can read this client's
    OS account via SO_PEERCRED (the person axis). Explicit COORDINATOR_UDS wins;
    otherwise auto-detect the per-user default if it exists. Empty string disables it
    (force TCP). Connecting over the UDS is what lets the gateway stamp the principal;
    over TCP there is no kernel credential and the save is recorded with no principal."""
    p = os.environ.get("COORDINATOR_UDS")
    if p is None:
        base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        cand = os.path.join(base, "shared-memory-gw.sock")
        p = cand if os.path.exists(cand) else ""
    return p or None


def _async_client(timeout: float) -> "httpx.AsyncClient":
    uds = _uds_path()
    if uds:
        return httpx.AsyncClient(timeout=timeout, transport=httpx.AsyncHTTPTransport(uds=uds))
    return httpx.AsyncClient(timeout=timeout)


def _sync_client(timeout: float) -> "httpx.Client":
    uds = _uds_path()
    if uds:
        return httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(uds=uds))
    return httpx.Client(timeout=timeout)


def _request_headers() -> dict:
    """Headers attached to every coordinator request.

    Always advertises this client's API_VERSION so the gateway can log skew
    (see coordinator._check_client_version). Adds the Bearer token when
    AGENT_TOKEN is set.
    """
    headers = {CLIENT_VERSION_HEADER: str(API_VERSION)}
    token = os.environ.get("AGENT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

# ── Audit logging ─────────────────────────────────────────────────────────────

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
                entry["content_size_warn"] = (
                    f"content is {len(content.encode())} bytes"
                    " — reduce log level to avoid large logs"
                )
        # Create 0600 if absent and tighten an existing world-readable file —
        # logs may carry agent activity; keep them owner-only. (merge_logs rotates
        # these per-tool logs daily; the gateway-side logs use log_hygiene.)
        log_path = os.path.join(log_dir, f"{tool}.log")
        if not os.path.exists(log_path):
            os.close(os.open(log_path, os.O_CREAT | os.O_WRONLY, 0o600))
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"[WARN] shared-memory: audit log unavailable ({e})", file=sys.stderr)
    except Exception:
        pass  # logging must never break the save path


# ── Coordinator HTTP helpers ──────────────────────────────────────────────────

def _coordinator_unavailable(exc: Exception) -> dict:
    return {
        "status": "error",
        "message": (
            f"Memory coordinator unreachable at {COORDINATOR_BASE} — "
            f"is hive_mind_proxy.py running? ({exc})"
        ),
    }


async def check_gateway_compat() -> dict:
    """GET /health and compare the wire contract. Pure diagnostic; never raises.

    Returns a dict with a ``compat`` field of "ok" | "incompatible" | "unknown",
    plus a human-readable ``warning`` when the client and gateway disagree on
    API_VERSION. Used by the ``doctor`` command and to enrich error messages.
    """
    try:
        async with _async_client(3.0) as client:
            h = (await client.get(f"{COORDINATOR_BASE}/health")).json()
    except Exception as exc:
        return {"reachable": False, "error": str(exc), "compat": "unknown"}

    srv = h.get("api_version")
    diag = {
        "reachable": True,
        "gateway_status":     h.get("status"),
        "gateway_version":    h.get("version"),
        "client_version":     VERSION,
        "server_api_version": srv,
        "client_api_version": API_VERSION,
    }
    if srv is None:
        diag["compat"]  = "unknown"
        diag["warning"] = (
            "Gateway does not report api_version — it predates the version "
            "contract. Upgrade the gateway (git pull + restart)."
        )
    elif srv != API_VERSION:
        lag = "client (re-sync the skill)" if srv < API_VERSION else "gateway (git pull + restart)"
        diag["compat"]  = "incompatible"
        diag["warning"] = (
            f"API contract skew: client speaks v{API_VERSION}, gateway speaks v{srv}. "
            f"Upgrade the {lag}."
        )
    else:
        diag["compat"] = "ok"
    return diag


async def _warn_on_skew(result: dict) -> dict:
    """When a request failed, probe /health and append a version-skew hint.

    Only runs on the failure path, so the happy path pays no extra round trip.
    """
    if not isinstance(result, dict) or result.get("status") != "error":
        return result
    diag = await check_gateway_compat()
    if diag.get("compat") in ("incompatible", "unknown") and diag.get("warning"):
        print(f"[WARN] shared-memory: {diag['warning']}", file=sys.stderr)
        result["version_warning"] = diag["warning"]
    return result


async def save_artifact(content: str, metadata_json: str = "{}") -> dict:
    if isinstance(metadata_json, str):
        try:
            metadata = json.loads(metadata_json)
        except (json.JSONDecodeError, ValueError) as e:
            _append_log("memory_bridge", 2, "bad_metadata", {"error": str(e), "content_preview": content[:100]}, content)
            return {"status": "error", "message": f"Invalid metadata JSON: {e}"}
    else:
        metadata = metadata_json

    if not isinstance(metadata, dict):
        _append_log("memory_bridge", 2, "bad_metadata_type", {"got": type(metadata).__name__, "content_preview": content[:100]}, content)
        return {"status": "error", "message": f"Metadata must be a JSON object, got {type(metadata).__name__}"}

    try:
        async with _async_client(60.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/save",
                json={"content": content, "metadata": metadata, "agent_id": AGENT_ID},
                headers=_request_headers(),
            )
            if r.status_code == 401:
                _append_log("memory_bridge", 2, "auth_failed",
                            {"hint": "Check AGENT_TOKEN in .env matches an entry in gateway AGENT_TOKENS"})
                return {"status": "error",
                        "message": "Coordinator rejected token. Set AGENT_TOKEN in this agent's .env."}
            result = r.json()
    except Exception as exc:
        _append_log("memory_bridge", 2, "coordinator_down", {"content_preview": content[:100]}, content)
        return await _warn_on_skew(_coordinator_unavailable(exc))

    if result.get("status") == "success":
        pg_id    = result.get("pg_id")
        entities = metadata.get("entities", [])
        _append_log("memory_bridge", 3, "save_success",
                    {"pg_id": pg_id, "source": metadata.get("source"), "entity_count": len(entities)},
                    content)
        if not entities:
            _append_log("memory_bridge", 1, "no_entities", {"pg_id": pg_id, "source": metadata.get("source")}, content)
    else:
        _append_log("memory_bridge", 2, "save_failed", {"response": result, "content_preview": content[:100]}, content)

    return result


async def supersede_fact(pg_id: int, by: int | None = None) -> dict:
    """Retract an existing fact without saving a replacement (decision 381/384).
    With `by`, point it at an existing successor fact."""
    payload: dict = {"pg_id": pg_id}
    if by is not None:
        payload["by"] = by
    try:
        async with _async_client(30.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/supersede",
                json=payload,
                headers=_request_headers(),
            )
            if r.status_code == 401:
                return {"status": "error",
                        "message": "Coordinator rejected token. Set AGENT_TOKEN in this agent's .env."}
            result = r.json()
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc))
    return result


async def review_hold(summary_id: int, pg_id: int) -> dict:
    """Mark a summary's flagged stale source as reviewed-and-held (decision 384, 8e):
    stop surfacing the supersession of `pg_id` for summary `summary_id`."""
    try:
        async with _async_client(30.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/review_hold",
                json={"summary_id": summary_id, "pg_id": pg_id},
                headers=_request_headers(),
            )
            if r.status_code == 401:
                return {"status": "error",
                        "message": "Coordinator rejected token. Set AGENT_TOKEN in this agent's .env."}
            result = r.json()
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc))
    return result


async def search_and_rerank(query: str, limit: int = 5) -> list | dict:
    try:
        async with _async_client(30.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/search",
                json={"query": query, "limit": limit, "agent_id": AGENT_ID},
                headers=_request_headers(),
            )
            if r.status_code == 401:
                _append_log("memory_bridge", 2, "auth_failed",
                            {"hint": "Check AGENT_TOKEN in .env matches an entry in gateway AGENT_TOKENS"})
                return {"status": "error",
                        "message": "Coordinator rejected token. Set AGENT_TOKEN in this agent's .env."}
            result = r.json()
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc))

    return result.get("results", result)


def query_graph(cypher: str, params: dict = None) -> list | dict:
    try:
        with _sync_client(30.0) as client:
            r = client.post(
                f"{COORDINATOR_BASE}/memory/graph",
                json={"cypher": cypher, "params": params or {}},
                headers=_request_headers(),
            )
        if r.status_code == 401:
            _append_log("memory_bridge", 2, "auth_failed",
                        {"hint": "Check AGENT_TOKEN in .env matches an entry in gateway AGENT_TOKENS"})
            return {"status": "error",
                    "message": "Coordinator rejected token. Set AGENT_TOKEN in this agent's .env."}
        result = r.json()
    except Exception as exc:
        return _coordinator_unavailable(exc)

    return result.get("records", result)


def get_telemetry() -> dict:
    """Fetch the gateway's operational telemetry snapshot (GET /memory/telemetry)."""
    try:
        with _sync_client(15.0) as client:
            r = client.get(
                f"{COORDINATOR_BASE}/memory/telemetry",
                headers=_request_headers(),
            )
        if r.status_code == 401:
            return {"status": "error",
                    "message": "Coordinator rejected token. Set AGENT_TOKEN in this agent's .env."}
        return r.json()
    except Exception as exc:
        return _coordinator_unavailable(exc)


def format_status(payload: dict) -> str:
    """Render the telemetry snapshot as a compact human-readable report."""
    if payload.get("status") != "success":
        return json.dumps(payload, indent=2)
    t  = payload["telemetry"]
    pg = t.get("postgres", {})
    nj = t.get("neo4j", {})
    lines = [f"Shared-memory status  @ {t.get('timestamp','?')}"]
    if "error" in pg:
        lines.append(f"  postgres: ERROR {pg['error']}")
    else:
        cs = pg.get("community_summaries", {})
        _sup = pg.get('technical_docs_superseded', 0)
        lines.append(f"  technical_docs:      {pg.get('technical_docs','?')}"
                     + (f" (superseded {_sup})" if _sup else ""))
        lines.append(f"  outbox:              {pg.get('outbox', {})}")
        lines.append(f"  community_summaries: {cs.get('total','?')} "
                     f"(superseded {cs.get('superseded',0)}, insight {cs.get('insight',0)})")
    if "error" in nj:
        lines.append(f"  neo4j: ERROR {nj['error']}")
    else:
        lines.append(f"  facts:     {nj.get('facts_total','?')} total | "
                     f"REM pending {nj.get('facts_rem_pending','?')} | "
                     f"unconsolidated {nj.get('facts_unconsolidated','?')}")
        lines.append(f"  decisions: {nj.get('decisions_total','?')} total | "
                     f"REM pending {nj.get('decisions_rem_pending','?')}")
    # Entity-graph shape (ADR-017). singletons = mentioned by one fact only
    # (fragmentation proxy); aliases climb from 0 once the alias layer ships.
    eg = t.get("entity_graph", {})
    if eg and "error" not in eg:
        _tot = eg.get("entities_total", 0) or 0
        _cov = eg.get("alias_covered_entities", 0) or 0
        _pct = f" ({_cov * 100 // _tot}% covered)" if _tot and _cov else ""
        lines.append(f"  entities:  {_tot} total | singletons {eg.get('singleton_entities',0)} "
                     f"| orphans {eg.get('orphan_entities',0)} | aliases {eg.get('alias_edges',0)}{_pct}")
    elif "error" in eg:
        lines.append(f"  entities: ERROR {eg['error']}")
    nr = t.get("nrem", {})
    if nr and "error" not in nr:
        lines.append(f"  NREM cycles: {nr.get('total_cycles','?')} pending "
                     f"(facts {nr.get('fact_cycles',0)}, decisions {nr.get('decision_cycles',0)})")
    elif "error" in nr:
        lines.append(f"  nrem: ERROR {nr['error']}")
    # Inference/GPU-busy signal (tri-state). "unknown" = nvtop absent / SLOT_AWARE
    # off — shown verbatim so the LLM is never reported falsely idle.
    ib = t.get("inference_busy")
    if ib is not None:
        lines.append(f"  inference (LLM/GPU): {ib}")
    # Consolidation liveness/coverage signal (ADR-018). stalled = eligible backlog
    # but no fold succeeded within the threshold and nothing in-flight.
    cn = t.get("consolidation", {})
    if cn and "error" not in cn:
        age = cn.get("last_success_age_seconds")
        age_s = f"{age}s ago" if age is not None else "—"
        flag = "STALLED ⚠" if cn.get("stalled") else "ok"
        lines.append(f"  consolidation: {flag} | last {cn.get('last_outcome') or '—'} "
                     f"| last success {age_s}")
        for ct in ("insight", "fact_consolidation"):
            c = cn.get(ct)
            if not isinstance(c, dict):
                continue
            parts = [c.get("last_outcome") or "—"]
            if c.get("last_outcome") == "deferred" and c.get("last_deferred_reason"):
                parts[0] = f"deferred ({c['last_deferred_reason']})"
            if c.get("stalled"):
                parts.append("STALLED")
            if c.get("consecutive_failures"):
                parts.append(f"{c['consecutive_failures']} fails")
            if c.get("last_error"):
                parts.append(f"err {c['last_error'].get('class','?')}")
            if c.get("eligible_clusters") is not None:
                cov = f"eligible {c['eligible_clusters']}"
                if c.get("eligible_oldest_age_seconds") is not None:
                    cov += f" (oldest {c['eligible_oldest_age_seconds']}s)"
                parts.append(cov)
            lines.append(f"    {ct}: " + ", ".join(parts))
    elif "error" in cn:
        lines.append(f"  consolidation: ERROR {cn['error']}")
    return "\n".join(lines)


# ── Decision shortcut ─────────────────────────────────────────────────────────

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
    grounded_in: str = "",
    elicited: bool = False,
) -> tuple:
    """Build (content, metadata) for a decision save.

    Returns a (content_str, metadata_dict) tuple ready for save_artifact().
    Pure function — no I/O, no side effects.
    """
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
    # grounded_in: pg_ids of the facts this decision rests on — materialised at
    # first write as (:Decision)-[:GROUNDED_IN]->(:Fact). Always include at least
    # the conversation fact (grounding floor, decision 552).
    gi = [int(x) for x in grounded_in.split(",") if x.strip().isdigit()]
    if gi:
        metadata["grounded_in"] = gi
    # elicited: the spine fields were asked of the operator (decision 559). An
    # elicited null is a deliberate choice; coverage telemetry counts the ask.
    if elicited:
        metadata["elicited"] = True
    return content, metadata


# ── Retrospective shortcut ────────────────────────────────────────────────────

def build_retrospective_payload(
    pg_id: int,
    rating: str,
    notes: str,
    date: str = "",
    source: str = None,
) -> dict:
    """Build the JSON payload for POST /memory/retrospective.

    Pure function — no I/O, no side effects.
    """
    return {
        "pg_id": pg_id,
        "rating": rating,
        "notes": notes,
        "date": date or datetime.now().date().isoformat(),
        "agent_id": source or AGENT_ID,
    }


async def save_retrospective_artifact(
    pg_id: int,
    rating: str,
    notes: str,
    date: str = "",
    source: str = None,
) -> dict:
    payload = build_retrospective_payload(pg_id, rating, notes, date, source)
    try:
        async with _async_client(60.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/retrospective",
                json=payload,
                headers=_request_headers(),
            )
            if r.status_code == 401:
                _append_log("memory_bridge", 2, "auth_failed",
                            {"hint": "Check AGENT_TOKEN in .env matches an entry in gateway AGENT_TOKENS"})
                return {"status": "error",
                        "message": "Coordinator rejected token. Set AGENT_TOKEN in this agent's .env."}
            return r.json()
    except httpx.ConnectError as exc:
        return _coordinator_unavailable(exc)


# ── Named query templates ─────────────────────────────────────────────────────

def _build_query(template: str, args) -> str:
    """Return a read-only Cypher string for the named provenance template.

    Filter values are scrubbed to [A-Za-z0-9 _.-] before interpolation —
    prevents quote-escape injection and avoids false-positive hits against
    the coordinator's write-keyword guard on strings like 'delete'.
    Pure function — no I/O, no side effects.
    """
    def _safe(v: str) -> str:
        return re.sub(r"[^A-Za-z0-9 _.\-]", "", v or "")

    if template == "who-decided":
        title   = _safe(getattr(args, "title",   ""))
        project = _safe(getattr(args, "project", ""))
        lines = ["MATCH (d:Decision)-[:WAS_ATTRIBUTED_TO]->(h:Human)"]
        if title:
            lines.append(f"WHERE d.title CONTAINS '{title}'")
        lines += [
            "OPTIONAL MATCH (d)-[:WAS_ASSISTED_BY]->(a:AIAgent)",
            "OPTIONAL MATCH (d)-[:PROJECT_OF]->(p:Project)",
        ]
        if project:
            lines.append("WITH d, h, a, p")
            lines.append(f"WHERE p.name CONTAINS '{project}'")
        lines.append(
            "RETURN d.title, d.pg_id, h.name AS decided_by, "
            "a.name AS assisted_by, d.date, p.name AS project ORDER BY d.date DESC"
        )
        return "\n".join(lines)

    elif template == "agent-decisions":
        assisted_by = _safe(getattr(args, "assisted_by", ""))
        project     = _safe(getattr(args, "project",     ""))
        lines = ["MATCH (d:Decision)-[:WAS_ASSISTED_BY]->(a:AIAgent)"]
        if assisted_by:
            lines.append(f"WHERE a.name CONTAINS '{assisted_by}'")
        lines.append("OPTIONAL MATCH (d)-[:PROJECT_OF]->(p:Project)")
        if project:
            lines.append("WITH d, a, p")
            lines.append(f"WHERE p.name CONTAINS '{project}'")
        lines.append(
            "RETURN d.title, d.pg_id, a.name AS assisted_by, "
            "d.date, p.name AS project ORDER BY d.date DESC"
        )
        return "\n".join(lines)

    elif template == "retrospectives":
        rating = _safe(getattr(args, "rating", ""))
        lines = ["MATCH (d:Decision)-[o:HAD_OUTCOME]->()"]
        if rating:
            lines.append(f"WHERE o.rating CONTAINS '{rating}'")
        lines.append(
            "RETURN d.title, d.pg_id, o.rating, o.notes, o.date ORDER BY o.date DESC"
        )
        return "\n".join(lines)

    elif template == "why-to-check":
        title   = _safe(getattr(args, "title",   ""))
        project = _safe(getattr(args, "project", ""))
        lines = ["MATCH (d:Decision)-[o:HAD_OUTCOME]->()"]
        if title:
            lines.append(f"WHERE d.title CONTAINS '{title}'")
        lines += [
            "OPTIONAL MATCH (d)-[:WAS_ATTRIBUTED_TO]->(h:Human)",
            "OPTIONAL MATCH (d)-[:PROJECT_OF]->(p:Project)",
        ]
        if project:
            lines.append("WITH d, o, h, p")
            lines.append(f"WHERE p.name CONTAINS '{project}'")
        lines.append(
            "RETURN d.title, d.pg_id, o.rating, o.notes, "
            "o.date, h.name AS decided_by ORDER BY o.date DESC"
        )
        return "\n".join(lines)

    else:
        print(json.dumps({
            "error": f"Unknown template '{template}'.",
            "available": ["who-decided", "agent-decisions", "retrospectives", "why-to-check"],
        }))
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({
            "error": "Usage: python memory_bridge.py [--version|doctor|status|graph|query|search|save|save_decision|save_retrospective] ..."
        }))
        sys.exit(1)

    action = sys.argv[1]

    if action in ("--version", "version", "-v"):
        print(json.dumps({
            "version": VERSION,
            "api_version": API_VERSION,
            "tool": "shared-memory-framework",
        }))
        return
    elif action == "status":
        payload = get_telemetry()
        # --json for machine-readable; default is the compact human report.
        if "--json" in sys.argv:
            print(json.dumps(payload, indent=2))
        else:
            print(format_status(payload))
        return
    elif action == "lineage":
        # "What happened to pg_id N?" — record state + in-flight dream-cycle stamps +
        # what it consolidated into (which summary/insight, the form, fact→summary
        # latency). All joins done gateway-side (ADR-014); this only calls the endpoint.
        if len(sys.argv) < 3 or not sys.argv[2].lstrip("-").isdigit():
            print(json.dumps({"error": "Usage: memory_bridge.py lineage <pg_id>"}))
            sys.exit(1)
        pid = int(sys.argv[2])
        try:
            async with _async_client(30.0) as client:
                r = await client.get(
                    f"{COORDINATOR_BASE}/memory/status/{pid}",
                    headers=_request_headers(),
                )
                print(json.dumps(r.json(), indent=2))
        except httpx.ConnectError as exc:
            print(json.dumps(_coordinator_unavailable(exc)))
        return
    elif action in ("doctor", "health"):
        diag = await check_gateway_compat()
        print(json.dumps(diag, indent=2))
        # Non-zero exit on an actionable problem so scripts can gate on it.
        sys.exit(0 if diag.get("compat") == "ok" else 1)
    elif action == "graph":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: memory_bridge.py graph <cypher>"}))
            sys.exit(1)
        print(json.dumps(query_graph(sys.argv[2]), indent=2))
    elif action == "search":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: memory_bridge.py search <query> [limit]"}))
            sys.exit(1)
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        print(json.dumps(await search_and_rerank(sys.argv[2], limit), indent=2))
    elif action == "save":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py save",
            description="Save a fact, optionally superseding an existing one.",
        )
        p.add_argument("content", help="Fact content")
        p.add_argument("metadata", nargs="?", default="{}", help="Metadata JSON (optional)")
        p.add_argument("--supersedes", type=int, default=None,
                       help="pg_id of an existing fact this save supersedes "
                            "(soft-retire: old fact kept, flagged, hidden from search)")
        sargs = p.parse_args(sys.argv[2:])
        metadata = sargs.metadata
        if sargs.supersedes is not None:
            try:
                mobj = json.loads(metadata) if isinstance(metadata, str) else metadata
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"status": "error", "message": f"Invalid metadata JSON: {e}"}))
                sys.exit(1)
            if not isinstance(mobj, dict):
                mobj = {}
            mobj["supersedes"] = sargs.supersedes
            metadata = json.dumps(mobj)
        print(json.dumps(await save_artifact(sargs.content, metadata), indent=2))
    elif action == "supersede":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py supersede",
            description="Retract an existing fact (no replacement). Soft: kept, "
                        "flagged, hidden from search.",
        )
        p.add_argument("--pg-id", type=int, required=True,
                       help="pg_id of the fact to retract")
        p.add_argument("--by", type=int, default=None,
                       help="pg_id of an existing successor fact (optional)")
        sargs = p.parse_args(sys.argv[2:])
        print(json.dumps(await supersede_fact(sargs.pg_id, sargs.by), indent=2))
    elif action == "review-hold":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py review-hold",
            description="Mark a summary's flagged stale source as reviewed-and-held "
                        "(stops re-surfacing it).",
        )
        p.add_argument("--summary-id", type=int, required=True,
                       help="community_summaries.id of the flagged summary")
        p.add_argument("--pg-id", type=int, required=True,
                       help="pg_id of the superseded source fact to acknowledge")
        sargs = p.parse_args(sys.argv[2:])
        print(json.dumps(await review_hold(sargs.summary_id, sargs.pg_id), indent=2))
    elif action == "query":
        if len(sys.argv) < 3:
            print(json.dumps({
                "error": "Usage: memory_bridge.py query <template> [filters]",
                "available": ["who-decided", "agent-decisions", "retrospectives", "why-to-check"],
            }))
            sys.exit(1)
        template = sys.argv[2]
        p = argparse.ArgumentParser(prog=f"memory_bridge.py query {template}")
        if template == "who-decided":
            p.add_argument("--title",   default="", help="Filter by decision title (substring)")
            p.add_argument("--project", default="", help="Filter by project name (substring)")
        elif template == "agent-decisions":
            p.add_argument("--assisted-by", default="", help="Filter by AI agent name (substring)")
            p.add_argument("--project",     default="", help="Filter by project name (substring)")
        elif template == "retrospectives":
            p.add_argument("--rating", default="", help="Filter by outcome rating (substring)")
        elif template == "why-to-check":
            p.add_argument("--title",   required=True, help="Decision title to look up (required)")
            p.add_argument("--project", default="",    help="Filter by project name (substring)")
        else:
            print(json.dumps({
                "error": f"Unknown template '{template}'.",
                "available": ["who-decided", "agent-decisions", "retrospectives", "why-to-check"],
            }))
            sys.exit(1)
        args = p.parse_args(sys.argv[3:])
        cypher = _build_query(template, args)
        print(json.dumps(query_graph(cypher), indent=2))
    elif action == "save_decision":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py save_decision",
            description="Save an architectural or design decision with PROV-O provenance.",
        )
        p.add_argument("--title",       required=True,  help="Short decision title")
        p.add_argument("--decided-by",  required=True,  help="Human who made the decision")
        p.add_argument("--project",     required=True,  help="Project context")
        p.add_argument("--rationale",   required=True,  help="Why this decision was made")
        p.add_argument("--source",      default=AGENT_ID,
                       help="Agent/model saving this record (default: $AGENT_ID)")
        p.add_argument("--assisted-by", default="",
                       help="Comma-separated AI agents that assisted")
        p.add_argument("--alternatives", default="",
                       help="Comma-separated alternatives that were considered")
        p.add_argument("--confidence",  default="",
                       help="Confidence level (e.g. high, medium, low)")
        p.add_argument("--entities",    default="",
                       help="Comma-separated Neo4j entities to link")
        p.add_argument("--grounded-in", default="",
                       help="Comma-separated pg_ids of the facts this decision rests on "
                            "(include at least the conversation fact — grounding floor)")
        p.add_argument("--elicited",    action="store_true",
                       help="The spine fields were elicited from the operator (an elicited "
                            "null is deliberate; coverage telemetry counts the ask)")
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
            grounded_in=args.grounded_in,
            elicited=args.elicited,
        )
        print(json.dumps(await save_artifact(content, metadata), indent=2))
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
            await save_retrospective_artifact(
                pg_id=args.pg_id,
                rating=args.rating,
                notes=args.notes,
                date=args.date,
                source=args.source,
            ),
            indent=2,
        ))
    else:
        print(json.dumps({"error": f"Unknown action: {action}. Use graph|query|search|save|save_decision|save_retrospective"}))


if __name__ == "__main__":
    asyncio.run(main())
