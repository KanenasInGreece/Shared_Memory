# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp==4.0.3", "httpx==0.28.1"]
# ///
#
# PEP 723 inline metadata: the connector declares its own dependencies, so the
# spawn line is `uv run --no-project <this file>`. The versions above are the
# ones `requirements-mcp.lock` pins, checked by tests/test_mcp_spawn_lines_pinned.py.
#
# `--with-requirements requirements-mcp.lock` resolves the lock relative to the
# spawning process's working directory, and an MCP host spawns stdio servers
# from a directory nobody documents. Inline metadata travels with the script, so
# it cannot be aimed at another file.
"""
Vector Skill — MCP server exposing the shared memory to an MCP host (LM Studio).

THIN CLIENT (ADR-014). This process owns no database connections. Every
operation is an HTTP call to the Hive-Mind Gateway on :8888, which is the single
component that talks to Postgres and Neo4j.

A client that queried the stores directly would apply no read-visibility
predicate, would drift from the one retrieval chain, and would need server-only
modules it is not shipped. So search, graph queries, lineage and saves all go
through the gateway, and this file holds rendering plus the MCP tool surface.

MCP tools: hybrid_search_and_rerank, save_artifact, save_decision,
save_retrospective, supersede, review_hold, check_memory_health,
memory_telemetry, record_lineage, graph_query.
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import re
import sys
from datetime import datetime

import httpx
from fastmcp import FastMCP

# ── Client-scoped credentials ────────────────────────────────────────────────
# This client may hold only its own AGENT_TOKEN and the gateway URL, so a .env
# carrying any of these server keys is refused rather than loaded, because it
# would hand this process every other agent's credentials.
# VECTOR_SKILL_ENV names another file, and a host that injects AGENT_TOKEN
# through its own config block needs no file at all.
_SERVER_ONLY_KEYS = frozenset({"AGENT_TOKENS", "PG_PASSWORD", "NEO4J_PASSWORD"})

# The key is the text before the first "=", with an optional `export` of any
# casing stripped, so a shell-sourceable line is classified by its key rather
# than by a substring that could also appear inside an unrelated value.
_ENV_KEY_RE = re.compile(r"^(?:export\s+)?([^=\s]+)\s*=", re.IGNORECASE)


_EXPORT_PREFIX_RE = re.compile(r"^export\s+", re.IGNORECASE)


def _strip_export_prefix(key: str) -> str:
    """Drop a leading shell ``export`` from a .env key so ``export AGENT_TOKENS`` is still classified as a secret."""
    s = key
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    s = _EXPORT_PREFIX_RE.sub("", s, count=1)
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    return s


def _strip_balanced_quotes(value: str) -> str:
    """Strip one matching pair of surrounding quotes from a .env value, and leave every other quote untouched."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _looks_like_server_env(path: str) -> bool:
    """True when this .env is the framework's rather than a client's; an unreadable file is not treated as one, since the loader will fail to read it anyway."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.lstrip().lstrip("﻿")
                if not stripped or stripped.startswith("#"):
                    continue
                m = _ENV_KEY_RE.match(stripped)
                if not m:
                    continue
                key = m.group(1).strip().upper()
                if key in _SERVER_ONLY_KEYS:
                    return True
    except OSError:
        return False
    return False


# AGENT_TOKEN from the file stays in this variable, never in os.environ where
# /proc and child processes would see it, and an operator export still wins
# (fact:1816: this door used to export its whole .env, token included).
_AGENT_TOKEN_FROM_FILE = ""

# Same secret names as the server, pinned by test_client_secret_mirror_parity.py, because this client ships alone and a missed name would land in os.environ.
_CLIENT_KNOWN_SECRET_NAMES = {
    "PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY", "AGENT_TOKENS",
    "BACKUP_ADMIN_TOKEN", "PG_CONN",
}
_CLIENT_SECRET_SUFFIXES = (
    "_PASSWORD", "_TOKEN", "_API_KEY", "_SECRET", "_KEY",
    "_CREDENTIAL", "_CREDENTIALS",
)


def _client_key_norm(name: str) -> str:
    """Strip a leading or trailing BOM and whitespace in either order, then upper-case, matching the server key normaliser."""
    s = name
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    while s and (s[-1].isspace() or s[-1] == "﻿"):
        s = s[:-1]
    return s.upper()


def _is_client_secret_key(name: str) -> bool:
    """True when this name must stay out of os.environ; AGENT_TOKEN is excluded because it has its own private variable, and the check normalises the name first."""
    key_norm = _client_key_norm(name)
    if key_norm == "AGENT_TOKEN":
        return False
    if key_norm in _CLIENT_KNOWN_SECRET_NAMES:
        return True
    return key_norm.endswith(_CLIENT_SECRET_SUFFIXES)


_ENV_PATH = os.environ.get("VECTOR_SKILL_ENV", "").strip() or os.path.join(
    os.path.dirname(os.path.realpath(__file__)), ".env")


def _load_env_manually(path: str) -> None:
    """Parse this client's `.env` with the gateway's rules: a value runs to the end of its line, an inline hash stays in the value, and an unbalanced quote does not swallow the next line.

    The parser is written out here rather than imported so a missing
    dependency can never make the loader silently no-op.
    """
    global _AGENT_TOKEN_FROM_FILE
    try:
        # utf-8-sig, matching memory_bridge.py: a BOM would otherwise stick to
        # the first key, and the per-key strip below catches one left mid-file.
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                # Strip `export` before classifying, or the key of an
                # `export AGENT_TOKENS=` line matches neither the token
                # diversion nor the secret list.
                key = _strip_export_prefix(key).strip()
                val = _strip_balanced_quotes(val.strip())
                if not key:
                    continue
                # One normalised form classifies the key, and the stored name
                # keeps the caller's casing. This catches every case variant of
                # the token, which memory_bridge.py diverts case-sensitively.
                key_norm = _client_key_norm(key)
                if key_norm == "AGENT_TOKEN":
                    if not _AGENT_TOKEN_FROM_FILE:
                        _AGENT_TOKEN_FROM_FILE = val
                    continue
                if _is_client_secret_key(key_norm):
                    continue
                if key not in os.environ:   # first definition wins
                    os.environ[key] = val
    except OSError:
        pass  # absent file = rely on externally-set env vars


if os.path.isfile(_ENV_PATH) and _looks_like_server_env(_ENV_PATH):
    sys.stderr.write(
        f"[rag-orchestrator] refusing to load {_ENV_PATH}: it holds "
        "server-only keys (AGENT_TOKENS / PG_PASSWORD / NEO4J_PASSWORD), so "
        "it is the framework env, not this client's. Give this MCP client its "
        "own directory with its own .env containing only AGENT_TOKEN (and "
        "optionally COORDINATOR_URL / AGENT_ID), set VECTOR_SKILL_ENV to that "
        "file, or inject AGENT_TOKEN via the MCP host's own env block.\n")
else:
    _load_env_manually(_ENV_PATH)

# Configure logging to stderr for MCP visibility
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RAG_Orchestrator")

mcp = FastMCP("Local_RAG_Orchestrator")

# The one endpoint this process talks to. Env-overridable like every other
# endpoint in the framework — never assume the bundled port layout.
COORDINATOR_BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
AGENT_ID = os.environ.get("AGENT_ID", "vector_skill")

# The wire contract, kept in step with coordinator.py and memory_bridge.py; on
# v4 a fact save without a registered metadata.project is rejected 400 carrying
# project_required or project_unknown plus near-match proposals.
API_VERSION = 4
VERSION = "1.0.1"
CLIENT_VERSION_HEADER = "X-SM-Api-Version"
# Framework build, separate from api_version, so two clients on the same wire contract can still be counted apart in clients.versions_seen.
CLIENT_BUILD_HEADER = "X-Shared-Memory-Client"

# Outcome states, not valence: 'reversed' drives the supersession cascade; nuance goes in notes.
RETRO_RATINGS = ("validated", "mixed", "refined", "pending", "reversed")
# A record id is unique only within its table, so a reference is qualified (decision 822: a bare integer off a summary resolved against the facts table and returned an unrelated record).
RECORD_TYPES = ("fact", "decision", "retrospective", "summary", "insight")

CALL_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# The search wait comes from /health projections, not a fixed 60s (fact:1112: the shipped client used a fixed wait and reported a live gateway as down).
HEALTH_PROBE_TIMEOUT_S    = float(os.environ.get("HEALTH_PROBE_TIMEOUT_S", "3"))
SEARCH_TIMEOUT_S          = float(os.environ.get("SEARCH_TIMEOUT_S", "0") or 0)
SEARCH_TIMEOUT_FLOOR_S    = float(os.environ.get("SEARCH_TIMEOUT_FLOOR_S", "30"))
SEARCH_TIMEOUT_MAX_S      = float(os.environ.get("SEARCH_TIMEOUT_MAX_S", "300"))
SEARCH_TIMEOUT_FALLBACK_S = float(os.environ.get("SEARCH_TIMEOUT_FALLBACK_S", "120"))
SEARCH_SAFETY_FACTOR      = float(os.environ.get("SEARCH_SAFETY_FACTOR", "1.5"))
SEARCH_OVERHEAD_S         = float(os.environ.get("SEARCH_OVERHEAD_S", "15"))


def search_ceiling(capability: dict | None, capacity: dict | None = None) -> float:
    """Seconds to wait for a search, matching memory_bridge.search_ceiling; a mixed or unknown encoder pair floors at SEARCH_TIMEOUT_FALLBACK_S (fact:1560: an unknown backend cost is not a zero cost)."""
    if SEARCH_TIMEOUT_S > 0:
        return SEARCH_TIMEOUT_S

    projected, probed, unknown = 0.0, False, False
    for backend in ("reranker", "embedder"):
        block = (capability or {}).get(backend)
        if not isinstance(block, dict) or not block:
            unknown = True
            continue
        try:
            value = float(block.get("projected_full_payload_s") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            projected += value
            probed = True
        elif block.get("status") == "failing" or block.get("projection_stale"):
            unknown = True   # this backend's real cost is unknown, not zero

    if not probed:
        derived = SEARCH_TIMEOUT_FALLBACK_S
    else:
        # Postgres vector search, the graph traversal and response assembly sit
        # outside both probes, so they are ADDED rather than scaled.
        floor = SEARCH_TIMEOUT_FALLBACK_S if unknown else SEARCH_TIMEOUT_FLOOR_S
        derived = max(floor, projected * SEARCH_SAFETY_FACTOR + SEARCH_OVERHEAD_S)

    capacity_derived = (capacity or {}).get("derived")
    if isinstance(capacity_derived, dict):
        client_ceiling_s = capacity_derived.get("client_ceiling_s")
        if isinstance(client_ceiling_s, (int, float)) and client_ceiling_s > 0:
            derived = max(derived, client_ceiling_s)
        s_max_measured_s = capacity_derived.get("s_max_measured_s")
        if isinstance(s_max_measured_s, (int, float)) and s_max_measured_s > 0:
            derived = max(derived, s_max_measured_s * SEARCH_SAFETY_FACTOR + SEARCH_OVERHEAD_S)
        s_mean_s = capacity_derived.get("s_mean_s")
        if isinstance(s_mean_s, (int, float)) and s_mean_s > 0:
            derived = max(derived, s_mean_s * SEARCH_SAFETY_FACTOR + SEARCH_OVERHEAD_S)

    return min(derived, SEARCH_TIMEOUT_MAX_S)


_CAPABILITY_CACHE: dict | None = None
_CAPACITY_CACHE: dict | None = None
# Stops two searches that start in the same instant from both firing a /health request.
_HEALTH_FETCH_LOCK = asyncio.Lock()


async def _fetch_health_blocks() -> None:
    """GET /health once per process and cache both ``backend_capability`` and ``capacity`` from that one request, never raising, because sizing the search must not be what fails it.

    The call carries this client's auth headers: both blocks sit behind auth,
    so an anonymous call would silently fall back to the constant ceiling.
    """
    global _CAPABILITY_CACHE, _CAPACITY_CACHE
    if _CAPABILITY_CACHE is not None:
        return   # already attempted this process — do not retry
    async with _HEALTH_FETCH_LOCK:
        if _CAPABILITY_CACHE is not None:
            return   # a concurrent waiter already filled it while we queued
        try:
            async with httpx.AsyncClient(timeout=HEALTH_PROBE_TIMEOUT_S, trust_env=False) as client:
                health = _reply_json(await client.get(f"{COORDINATOR_BASE}/health",
                                                      headers=_auth_headers()),
                                     "_fetch_health_blocks")
            block = health.get("backend_capability")
            _CAPABILITY_CACHE = block if isinstance(block, dict) else {}
            capacity = health.get("capacity")
            _CAPACITY_CACHE = capacity if isinstance(capacity, dict) else None
        except Exception:
            _CAPABILITY_CACHE = {}      # tried and got nothing; do not retry
            _CAPACITY_CACHE = None


async def _gateway_capability() -> dict | None:
    """The cached ``backend_capability`` block — see ``_fetch_health_blocks``."""
    await _fetch_health_blocks()
    return _CAPABILITY_CACHE or None


async def _gateway_capacity() -> dict | None:
    """The cached ``capacity`` block — see ``_fetch_health_blocks``. None on an
    older/unreachable gateway, or one with no derivation yet."""
    await _fetch_health_blocks()
    return _CAPACITY_CACHE


def _auth_headers() -> dict:
    """Headers for every coordinator request: the API version so the gateway can log skew, and a Bearer token read fresh on each call so an operator export wins over the file value."""
    headers = {CLIENT_VERSION_HEADER: str(API_VERSION),
               CLIENT_BUILD_HEADER: VERSION}
    # Deliberate truthiness, not a presence check: an exported empty token
    # falls back to the file value rather than suppressing the header, which is
    # the CLI door's rule too. Pinned by
    # test_exported_empty_agent_token_falls_back_to_file_token.
    token = os.environ.get("AGENT_TOKEN", "").strip() or _AGENT_TOKEN_FROM_FILE
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


_CONTENT_SIZE_WARN_BYTES = 10 * 1024


_LOG_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="sm-audit-log")


def _append_line(path: str, line: str) -> None:
    try:
        with open(path, "a") as f:
            f.write(line)
    except OSError as e:
        print(f"[WARN] shared-memory: audit log unavailable ({e})", file=sys.stderr)


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
        # One worker thread, because a sync append on the event loop would
        # stall every concurrent tool call and a second thread would reorder.
        _LOG_EXECUTOR.submit(_append_line,
                             os.path.join(log_dir, f"{tool}.log"),
                             json.dumps(entry) + "\n")
    except OSError as e:
        print(f"[WARN] shared-memory: audit log unavailable ({e})", file=sys.stderr)
    except Exception:
        pass  # logging must never break the save path


def _unavailable(exc: Exception, ceiling: float | None = None) -> str:
    """The one message for a gateway this client could not reach.

    A timeout is a different fault and gets its own wording (fact:1112: an
    httpx ReadTimeout stringifies to nothing, so the reader was told to start a
    service that was already running). A GatewayReplyError means the gateway
    answered and so can never be reported as unreachable, even from a call site
    that forgot its own except clause (fact:1503).
    """
    if isinstance(exc, GatewayReplyError):
        return exc.message
    if isinstance(exc, httpx.TimeoutException):
        waited = f"{ceiling:.0f}s" if ceiling else "the client timeout"
        return (f"Error: gateway did not answer within {waited} — it is most likely "
                f"UP and SLOW, not down. A search costs what the reranker costs. "
                f"Read `backend_capability` on {COORDINATOR_BASE}/health, and raise "
                f"SEARCH_TIMEOUT_S if its projection exceeds that ceiling.")
    return (f"Error: memory gateway unreachable at {COORDINATOR_BASE} ({exc}). "
            "Start it with: systemctl --user start hive-mind-gateway.service")


def _auth_rejected(tool: str) -> str:
    """The one 401 response, logged under the calling tool's name so the audit trail says which call was rejected.

    It branches on whether a credential was sent, because a 401 with no
    Authorization header is a missing token rather than a rejected one.
    """
    presented = "Authorization" in _auth_headers()
    where = ("this client's own .env (beside this script, or wherever "
             "VECTOR_SKILL_ENV points), or in the MCP host's env block")
    if presented:
        _append_log(tool, 2, "auth_failed",
                    {"hint": "Check AGENT_TOKEN matches a gateway AGENT_TOKENS entry"})
        return (f"Error: the gateway rejected this client's token. Set AGENT_TOKEN in "
                f"{where}. It must match an entry in the gateway's AGENT_TOKENS.")
    _append_log(tool, 2, "auth_failed",
                {"hint": "No AGENT_TOKEN was sent; the gateway requires auth"})
    return (f"Error: no AGENT_TOKEN was sent and the gateway requires "
            f"authentication. Set AGENT_TOKEN in {where}.")


class GatewayReplyError(Exception):
    """The gateway answered, and its answer was not a 2xx JSON payload.

    ``logged_event`` names the audit event the raise site already wrote, or is
    None when it wrote nothing, so a catch block does not record one refused
    call twice. It is an attribute rather than a phrase read back out of the
    message, which would tie the audit trail to wording meant to improve.
    """

    def __init__(self, message: str, *, logged_event: str | None = None):
        super().__init__(message)
        self.message = message
        self.logged_event = logged_event


def _body_snippet(r, limit: int = 200) -> str:
    """A short, whitespace-collapsed piece of the response body, or "". Never
    raises: this runs on the error path, where a second failure would replace a
    diagnosis with a traceback."""
    try:
        # Gateway-controlled text bound for a terminal, so control characters
        # are stripped before the collapse and the cap.
        return " ".join(_clean_gateway_text(r.text or "").split())[:limit]
    except Exception:
        return ""


# COORDINATOR_BASE is env-overridable, so the gateway's own words are capped
# and stripped before they reach a log or a terminal. The longest message any
# deployed refusal emits measured 378 characters, so this cap truncates only a
# body no deployed path produces.
_GATEWAY_MESSAGE_MAX = 600


def _clean_gateway_text(msg: str) -> str:
    """Strip ASCII control characters, keeping newline and tab, then cap the length, because an ANSI escape printed to a terminal can rewrite the line the operator is reading."""
    cleaned = "".join(
        ch for ch in msg
        if ch in ("\n", "\t") or (ord(ch) >= 32 and ord(ch) != 127)
    )
    return cleaned.strip()[:_GATEWAY_MESSAGE_MAX]


def _gateway_message(r) -> str | None:
    """The gateway's own ``message`` when the body is JSON and carries one, capped and control-stripped, with a decode failure returned as a result rather than raised into the transport handler."""
    try:
        body = r.json()
    except Exception:
        return None
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error")
        if isinstance(msg, str) and msg.strip():
            return _clean_gateway_text(msg) or None
    return None


def _reply_json(r, tool: str) -> dict:
    """Decode JSON only after the status class is known (fact:1503)."""
    # _auth_rejected has already logged `auth_failed`, which is what
    # `logged_event` tells the catch block.
    if r.status_code == 401:
        raise GatewayReplyError(_auth_rejected(tool), logged_event="auth_failed")

    # The gateway's own words come before this client's framing.
    if r.status_code == 403:
        detail = _gateway_message(r) or _body_snippet(r)
        head = (f"Error: the gateway refused this request (HTTP 403): {detail}"
                if detail else "Error: the gateway refused this request (HTTP 403).")
        raise GatewayReplyError(
            f"{head} — the gateway ANSWERED and the credential was ACCEPTED, so this is "
            f"an authorization refusal, not an authentication failure and not a "
            f"transport fault.")

    if r.status_code >= 400:
        detail = _gateway_message(r) or _body_snippet(r) or "(empty body)"
        raise GatewayReplyError(
            f"Error: the gateway answered HTTP {r.status_code}: {detail} — it is UP at "
            f"{COORDINATOR_BASE} and refused or failed this request.")

    try:
        return r.json()
    except Exception as exc:
        raise GatewayReplyError(
            f"Error: the gateway answered HTTP {r.status_code} at {COORDINATOR_BASE} "
            f"with a body this client could not parse as JSON ({exc}). The gateway is "
            f"LIVE and ANSWERED — this is a malformed reply, not a transport fault. "
            f"Body began: "
            f"{_body_snippet(r, 120) or '(empty)'}") from exc


def _valid_ref(ref: str) -> bool:
    """A bare id, or a qualified `type:id` reference (decision 822)."""
    head, _, tail = str(ref).partition(":")
    if tail:
        return tail.lstrip("-").isdigit() and head.lower() in RECORD_TYPES
    return str(ref).lstrip("-").isdigit()


# ── Rendering ────────────────────────────────────────────────────────────────

def _render_results(results: list, elapsed: float) -> str:
    """Render the gateway's search response for an MCP host, surfacing each row's own ``ref`` and ``record_type`` verbatim, because a bare integer is what makes a follow-up lookup resolve against the wrong table."""
    if not results:
        return "Result: No relevant documentation found."

    # Render in the order the gateway returned. It ranks summaries against
    # facts on one scale, so re-grouping here would put a summary on top
    # carrying a score that says it belongs sixth.
    body = []
    for r in results:
        rtype = r.get("record_type")
        score = r.get("score")
        if rtype in ("summary", "insight"):
            kind = ("Insight (cross-project principle)" if rtype == "insight"
                    else "Global Context Summary")
            bits = [f"Ref: {r.get('ref', r.get('pg_id'))}"]
            # Showing the score makes a Tier-3 row's position inspectable.
            if score is not None:
                bits.insert(0, f"Score: {score:.2f}")
            src = r.get("source_pg_ids") or []
            if src:
                bits.append(f"synthesised from {len(src)} record(s)")
            line = f"### {kind}  [{' | '.join(bits)}]"
        else:
            meta = r.get("metadata") or {}
            source = meta.get("source", "unknown") if isinstance(meta, dict) else "unknown"
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

    # Counts every row, Tier-3 included, so the header matches what is printed.
    header = f"### Unified Memory Results ({len(results)} item(s) found in {elapsed:.2f}s)\n\n"
    parts = [header + "\n\n---\n\n".join(body)] if body else []
    return "\n\n---\n\n".join(parts)


# ── Retrieval ────────────────────────────────────────────────────────────────

def _unranked_warning(results) -> str | None:
    """The sentence both doors share for an unranked result set, held byte-identical to ``memory_bridge._unranked_warning`` by a parity test and decorated per door."""
    if not isinstance(results, list):
        return None
    unranked = sum(1 for row in results if isinstance(row, dict) and row.get("ranked") is False)
    if not unranked:
        return None
    return (f"{unranked} of {len(results)} results are UNRANKED — the reranker "
            f"timed out, this is vector order (see backend_capability on /health)")


def _fallback_warning(payload: object) -> str | None:
    """Warn that the gateway served a keyword fallback instead of failing the search, firing on an empty result list too, since that is the shape a natural-language query takes against a substring match (fact:1609)."""
    if not isinstance(payload, dict) or payload.get("fallback") != "keyword":
        return None
    results = payload.get("results")
    n = len(results) if isinstance(results, list) else 0
    return (f"EMBEDDING UNAVAILABLE — keyword (substring) fallback served "
            f"{n} result(s), unranked; the embedder is down or still "
            f"starting (see embedder on /health)")


def _stale_projection_note(capability: dict | None) -> str | None:
    """Name each backend whose cost projection has gone stale, so the ceiling above reads as a lower bound rather than a measurement."""
    if not isinstance(capability, dict):
        return None
    notes = []
    for backend in ("reranker", "embedder"):
        block = capability.get(backend)
        if not isinstance(block, dict) or not block.get("projection_stale"):
            continue
        age = block.get("projection_age_s")
        if isinstance(age, (int, float)) and age > 0:
            notes.append(f"{backend} projection stale for {age:.0f}s")
        else:
            notes.append(f"{backend} projection stale")
    return "; ".join(notes) if notes else None


async def _search_payload(query: str, limit: int = 5, project: str = "",
                           domains: list[str] | str = "",
                           since: str = "") -> dict | str:
    """Make the search call and return either the decoded payload or an already-phrased error string, so one round trip feeds both the unranked and the keyword-fallback warnings.

    A dict is any 2xx answer, including a gateway-reported ``status: error``
    body; a string is a refusal or a transport failure, so a caller branches on
    ``isinstance(payload, str)``.
    """
    # Sized from the gateway's published cost, never a constant.
    ceiling = search_ceiling(await _gateway_capability(), await _gateway_capacity())
    body = {"query": query, "limit": limit, "agent_id": AGENT_ID}
    if project:
        body["project"] = project
    _domains = ([d.strip() for d in domains if isinstance(d, str) and d.strip()]
                if isinstance(domains, list)
                else [d.strip() for d in (domains or "").split(",") if d.strip()])
    if _domains:
        body["domains"] = _domains
    if since:
        body["since"] = since
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(ceiling, connect=5.0),
            trust_env=False,
        ) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/search",
                json=body,
                headers=_auth_headers(),
            )
            return _reply_json(r, "hybrid_search_and_rerank")
    except GatewayReplyError as exc:
        logger.error(f"Search refused: {exc.message}")
        return exc.message
    except Exception as exc:
        logger.error(f"Search failed: {exc}")
        return _unavailable(exc, ceiling)


@mcp.tool()
async def hybrid_search_and_rerank(query: str, limit: int = 5, project: str = "",
                                   domains: list[str] | str = "",
                                   since: str = "") -> str:
    """
    Search the shared memory: Tier-3 thematic/insight narratives for orientation,
    Tier-1 facts for precision, expanded through the entity graph.

    Delegates the whole retrieval chain — embedding, vector search, reranking,
    graph expansion, and READ AUTHORIZATION — to the gateway, so this host sees
    exactly what every other agent sees, and only what it is permitted to see.

    `project`/`domains`/`since` are optional AXIS FILTERS applied to the
    candidate set BEFORE reranking — a named place (project/domain) or time
    (since) is a FILTER, not query text: folding a project name into the query
    text instead ranks records that merely MENTION it above records that
    BELONG to it. `domains` is a list, or one name ("domains" is also accepted
    as a comma-free single string — pass a list for several; OR semantics, any
    match qualifies). `since` is an ISO date/datetime
    (e.g. "2026-08-01T00:00:00") — records created at/after it. An unknown
    project/domain name is not refused, it simply matches nothing (the read
    path never blocks on registry state).

    The wait for this call is sized from the gateway's own published backend
    capability, not a constant — set SEARCH_TIMEOUT_S (env) to pin an explicit
    override instead.
    """
    logger.info(f"Search: {query[:50]}...")
    start = datetime.now()
    payload = await _search_payload(query, limit, project=project,
                                     domains=domains, since=since)
    if isinstance(payload, str):
        return payload

    results = payload.get("results", payload)
    if isinstance(results, dict) and results.get("status") == "error":
        return f"Error: {results.get('message', 'search failed')}"
    results_list = results if isinstance(results, list) else []
    rendered = _render_results(results_list, (datetime.now() - start).total_seconds())
    # This tool returns text, so the warnings are prepended lines rather than
    # `note` fields. They go on in reverse, leaving the reader the same
    # top-to-bottom order the CLI door prints: unranked, then fallback.
    fallback_warning = _fallback_warning(payload)
    if fallback_warning:
        rendered = f"NOTE: {fallback_warning}\n\n" + rendered
    warning = _unranked_warning(results_list)
    if warning:
        rendered = f"NOTE: {warning}\n\n" + rendered
    stale_note = _stale_projection_note(await _gateway_capability())
    if stale_note:
        rendered = (f"NOTE: {stale_note} — the ceiling above still used its "
                    f"last number as a lower bound (see backend_capability on "
                    f"/health)\n\n" + rendered)
    return rendered


@mcp.tool()
async def save_artifact(content: str, metadata_json: str = "{}") -> str:
    """Store an artifact in shared memory through the Hive-Mind Gateway.

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.

    Routes through the Memory Coordinator (POST /memory/save) — no direct DB
    writes here — so the save gets the full server-side path: BGE-M3 embedding
    (hard mandate; the coordinator returns 503 if the embedder is down), SHA-256
    idempotent upsert into Postgres, and a neo4j_outbox row written in the SAME
    transaction and applied asynchronously by the outbox worker. That closes the
    ADR-001 dangling-Fact gap the old direct Postgres+Neo4j write left open: the
    two stores can no longer diverge on a crash between them.

    Idempotent: identical content reuses the existing row.

    metadata_json MUST carry "project" on a fact — the canonical value is the
    project folder name, checked against a registry. A save with none, or with an
    unregistered value, returns 400 carrying an "error" of project_required or
    project_unknown plus near-match "proposals". Ask the operator which project
    applies rather than inferring one; re-send with "new_project": true to
    register a genuinely new project, or use "general_discussion" for a record
    that belongs to no project (it saves and searches normally but is never
    folded into a project's narrative).

    metadata_json MAY carry "domain" — a registered SECTION of that project, as
    a string or a list ("domains" is accepted for the list form). Sections are
    project-local: the same name under two projects is two sections. The same
    protocol as project applies — an unregistered value returns 400 with
    "error": "domain_unknown" plus proposals (matched on a section's DESCRIPTION
    as well as its name), and "new_domain": true registers it after the operator
    confirms. Ask only when the project already HAS registered sections; a record
    with no domain is filed under its project, which is always correct.

    ⛔ A RETROSPECTIVE MUST NOT CARRY ONE (400
    "domain_not_allowed_on_judgement"). Facts and decisions assert their own
    project and domain. Omit on a decision stores no section. It does not
    take the grounding facts' sections. `belonging` is read-side only. A
    retrospective does not store sections.

    Supersede-on-save: include "supersedes": <old_pg_id> in metadata_json to save
    this as a CORRECTION that retires an older fact in one call (the old fact is
    kept but flagged + hidden from search). To retract a fact WITHOUT a
    replacement, use the `supersede` tool instead.
    """
    # Validated here so the model gets a clear error before any network call;
    # the coordinator is the authority and re-checks.
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

    # Project is required on a fact here as on the CLI door, and there is no
    # cwd to derive it from, because the host launches this server anywhere.
    if m_data.get("type") not in ("decision", "retrospective") and not m_data.get("project"):
        _append_log("vector_skill", 2, "missing_project", {"content_preview": content[:100]}, content)
        return (
            "Error: metadata.project is required — the canonical value is the "
            "PROJECT FOLDER NAME. Ask the operator which project this belongs to "
            "rather than inferring one; a plausible wrong project is worse than "
            "none. If it belongs to no project, use 'general_discussion', which "
            "saves and searches normally but is never folded into a project's "
            "narrative. If the project is new, also set metadata.new_project=true."
        )

    m_data["timestamp"] = datetime.now().isoformat()
    # An auth-enabled gateway overwrites metadata.source with the verified
    # agent identity, so the loaded model name is kept here too.
    m_data.setdefault("model", m_data["source"])
    entities = m_data.get("entities", [])

    coordinator_url = COORDINATOR_BASE
    agent_id = AGENT_ID

    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            r = await client.post(
                f"{coordinator_url}/memory/save",
                json={"content": content, "metadata": m_data, "agent_id": agent_id},
                headers=_auth_headers(),
            )
            result = _reply_json(r, "save_artifact")
    except GatewayReplyError as exc:
        # One refused save is one audit line: a 401 was already logged by
        # _auth_rejected, and every other class logs `save_rejected` here
        # rather than the `gateway_down` these replies used to claim.
        if exc.logged_event is None:
            _append_log("vector_skill", 2, "save_rejected", {"message": exc.message}, content)
        return exc.message
    except Exception as exc:
        _append_log("vector_skill", 2, "gateway_down", {"content_preview": content[:100]}, content)
        return (f"{_unavailable(exc)} Save aborted to protect memory "
                f"integrity — an unembedded record is invisible to search.")

    if result.get("status") != "success":
        # The coordinator refused the save, so its message is surfaced verbatim.
        _append_log("vector_skill", 2, "save_rejected", {"message": result.get("message", result)}, content)
        return f"Error: {result.get('message', result)}"

    pg_id = result.get("pg_id")
    if not entities:
        _append_log("vector_skill", 1, "no_entities", {"pg_id": pg_id, "source": m_data.get("source")}, content)
    _append_log("vector_skill", 3, "save_success", {"pg_id": pg_id, "source": m_data.get("source"), "entity_count": len(entities)}, content)

    # The coordinator's message carries the no-entities Tier-3 warning.
    neo4j_status = result.get("neo4j", "pending")
    return f"Success (pg_id={pg_id}, neo4j={neo4j_status}): {result.get('message', '')}".rstrip()

def _alternatives_list(alternatives) -> list[str]:
    """One value in, one alternative out, verbatim and never split, because a well-written alternative contains commas and splitting on one stored fragments that do not stand alone.

    A list is one entry per option and a lone string is exactly one option,
    since under-splitting never invents an option nobody wrote.
    """
    if alternatives is None:
        return []
    if isinstance(alternatives, str):
        alternatives = [alternatives]
    return [str(a).strip() for a in alternatives if str(a).strip()]


@mcp.tool()
async def save_decision(
    title: str,
    decided_by: str,
    project: str,
    rationale: str,
    source: str,
    assisted_by: str = "",
    alternatives: list[str] | str = "",
    confidence: str = "",
    grounded_in: str = "",
    elicited: bool = False,
    new_project: bool = False,
    confirm_distinct_from: str = "",
    domain: list[str] | str = "",
    new_domain: bool = False,
) -> str:
    """Save an architectural or design decision with full PROV-O provenance.

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.

    Routes through the Memory Coordinator so the Decision→Human→Project→AIAgent
    subgraph is written by the outbox worker — no direct Neo4j writes here.

    `domain` names the SECTION(S) of the project this decision belongs to —
    a list, or one name. A decision asserts its own sections just as it asserts
    its own project. Naming none stores no section. It does not take the
    grounding facts' sections. `belonging` is read-side only.
    Each must already be registered, or pass new_domain=True after the operator
    confirms. A RETROSPECTIVE may never carry one.

    Required: title, decided_by, project, rationale, source (loaded model name).
    Optional: assisted_by (comma-separated), confidence, and `alternatives` —
    a LIST, one entry per alternative. `alternatives` is never split on any
    separator, so an entry may contain commas and brackets; passing a lone
    string records exactly one alternative. This is deliberate: splitting on
    commas shredded 21% of the decisions that carried alternatives, because a
    well-written option contains them.

    grounded_in is the important one: a decision NAMES NO ENTITIES of its own,
    it inherits its topics from the facts it rests on, so an ungrounded decision
    reaches no cluster and never enters cross-project synthesis. Format
    "pgid[:role],pgid" — role one of based_on/considered/rejected/
    under_conditions/informed_by (bare id picks the fact's kind-derived default).
    A decision or retrospective that named entities of its own is refused by
    the gateway (decision:1664) — this tool accepts none.

    The rationale carries the two things no other field holds: the CONDITIONS
    the decision is expected to hold under, and WHY each alternative was
    rejected — `alternatives` records only what was passed over, never why.
    Write both into the rationale. State "conditions: none" explicitly when
    there are none, so a deliberate absence cannot be mistaken for an unasked
    question. Synthesis is told to state each principle's limits and what it
    chose against; supplying neither invites the model to invent both.

    decided_by names the PERSON only. Do not fold the assisting model into it
    ("<operator> + <agent>") — that is what assisted_by is for. Each such
    spelling mints its own :Human node and splits one operator across provenance.
    Over a UNIX socket the gateway canonicalises this onto the kernel-attested
    OS account and keeps the wording as decided_by_claimed; this MCP client
    connects over TCP, which carries no kernel credential, so nothing is
    attested and whatever is typed here is stored verbatim. The discipline is
    the caller's on this path.

    elicited: set True when the operator was asked for these fields (drives
    spine-coverage telemetry, decision 559).

    project is checked against the registry exactly as a fact's is: an
    unregistered value is refused with near-match proposals rather than
    silently creating a project. new_project=true declares the value genuinely
    new and registers it — pass it ONLY after the operator has confirmed the
    spelling, because it is what separates a new project from a typo, and a
    registry row is permanent. Work that introduces a project declares it ONCE,
    on the first record that names it; everything saved afterwards simply uses
    the now-registered name. If the name is not settled yet, save on
    "general_discussion" and promote later — never invent a placeholder.
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
    alts = _alternatives_list(alternatives)
    if alts:
        decision_data["alternatives"] = alts
    if confidence:
        decision_data["confidence"] = confidence

    metadata = {
        "type": "decision",
        "source": source,
        # A decision has one project, written both here and in the blob,
        # because the top-level key is what a reader inspecting Postgres reads.
        "project": project,
        # A decision mints no entity of its own, so the gateway accepts this
        # empty list and refuses a non-empty one (decision:1664).
        "entities": [],
        "decision": decision_data,
    }
    # A decision asserts its own sections as it asserts its own project, so
    # naming none stores none and does not take the grounding facts'.
    _domains = ([d.strip() for d in domain if isinstance(d, str) and d.strip()]
                if isinstance(domain, list)
                else [d.strip() for d in (domain or "").split(",") if d.strip()])
    if _domains:
        decision_data["domains"] = _domains
    if new_domain:
        metadata["new_domain"] = True
    # The operator has confirmed the name is new, so the save registers it
    # instead of being refused; the registry check only means something while
    # declaring a new project stays a deliberate act.
    if new_project:
        metadata["new_project"] = True
    # The registered projects this new one is deliberately not, needed only
    # after the gateway refuses the name as confusable and names which.
    _distinct = [d.strip() for d in (confirm_distinct_from or "").split(",") if d.strip()]
    if _distinct:
        metadata["confirm_distinct_from"] = _distinct
    # The "pgid[:role],pgid" grammar the CLI door uses, materialised by the
    # outbox worker as typed edges out of the decision.
    gi: list = []
    grounded_roles: dict = {}
    for tok in grounded_in.split(","):
        tok = tok.strip()
        if not tok:
            continue
        pid_str, _, role = tok.partition(":")
        pid_str = pid_str.strip()
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        gi.append(pid)
        if role.strip():
            grounded_roles[str(pid)] = role.strip().lower()
    if gi:
        metadata["grounded_in"] = gi
    if grounded_roles:
        metadata["grounded_roles"] = grounded_roles
    if elicited:
        metadata["elicited"] = True
    content = f"{title}\n\n{rationale}"

    coordinator_url = COORDINATOR_BASE
    agent_id = AGENT_ID

    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            r = await client.post(
                f"{coordinator_url}/memory/save",
                json={"content": content, "metadata": metadata, "agent_id": agent_id},
                headers=_auth_headers(),
            )
            result = _reply_json(r, "save_decision")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)

    if result.get("status") == "success":
        pg_id = result.get("pg_id")
        return f"Decision saved (pg_id={pg_id}): {title}"

    return f"Error: {result.get('message', result)}"


@mcp.tool()
async def save_retrospective(
    pg_id: int,
    rating: str,
    notes: str,
    source: str,
    date: str = "",
    grounded_in: str = "",
    elicited: bool = False,
) -> str:
    """Record an outcome for an existing Decision as a full retrospective
    record: its own searchable record plus a Retrospective node behind the
    decision's HAD_OUTCOME trigger edge.

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.

    Use this after a decision has been acted on to close the Why-To loop.
    Multiple retrospectives per decision are allowed — the newest is the
    decision's current verdict.

    Required: pg_id (returned by save_decision), rating, notes, source.
    rating is a closed outcome-state enum: validated | mixed | refined |
    pending | reversed ('reversed' supersedes the decision; nuance goes in notes).
    Optional: date (ISO string, default: today). grounded_in: pg_ids of the facts
    that MEASURED this outcome, same "pgid[:role],pgid" grammar as save_decision
    (test-grounded retrospectives, decision 542). A retrospective names no
    entities either — it inherits its topics from those facts, falling back to
    the decision it judges — so grounded_in is what carries the outcome into
    synthesis. elicited: set True when the operator was asked for these fields.
    """
    coordinator_url = COORDINATOR_BASE
    agent_id = AGENT_ID

    payload = {
        "pg_id": pg_id,
        "rating": rating,
        "notes": notes,
        "date": date or datetime.now().date().isoformat(),
        "agent_id": source or agent_id,
    }
    gi: list = []
    grounded_roles: dict = {}
    for tok in grounded_in.split(","):
        tok = tok.strip()
        if not tok:
            continue
        pid_str, _, role = tok.partition(":")
        pid_str = pid_str.strip()
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        gi.append(pid)
        if role.strip():
            grounded_roles[str(pid)] = role.strip().lower()
    if gi:
        payload["grounded_in"] = gi
    if grounded_roles:
        payload["grounded_roles"] = grounded_roles
    if elicited:
        payload["elicited"] = True

    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            r = await client.post(
                f"{coordinator_url}/memory/retrospective",
                json=payload,
                headers=_auth_headers(),
            )
            result = _reply_json(r, "save_retrospective")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)

    if result.get("status") == "success":
        own = result.get("pg_id")
        own_note = f" (record pg_id={own})" if own else ""
        return f"Retrospective recorded on Decision pg_id={result['target_pg_id']}{own_note}."

    return f"Error: {result.get('message', result)}"


@mcp.tool()
async def supersede(pg_id: int, by: int = 0) -> str:
    """Retract or supersede an existing FACT (decision 381/384).

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.

    Soft: the old fact is KEPT for provenance but flagged, hidden from search,
    and excluded from consolidation. Supersession is EXPLICIT; never infer it
    from similarity.

    Decisions and retrospectives are refused with HTTP 400: supersession is the
    fact lifecycle. To overturn a decision call save_retrospective against it
    with rating='reversed' — that marks it superseded as the consequence of a
    verdict that stays in the graph for a successor to ground on. To revise a
    retrospective, save a NEW one against the same decision; the latest live
    verdict is the one that counts.

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
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            r = await client.post(
                f"{coordinator_url}/memory/supersede",
                json=payload,
                headers=_auth_headers(),
            )
            result = _reply_json(r, "supersede")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)
    if result.get("status") == "success":
        return result.get("message", f"Fact {pg_id} superseded.")
    return f"Error: {result.get('message', result)}"


@mcp.tool()
async def review_hold(summary_id: int, pg_id: int) -> str:
    """Mark a summary's flagged stale source as reviewed-and-held (decision 384).

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.

    When a search result carries a stale_sources warning (a summary/insight was
    synthesised from a since-superseded fact) and you judge the change immaterial,
    call this so the warning stops re-surfacing for that summary. A later
    supersession of a DIFFERENT source still surfaces.

    Required: summary_id (the community_summaries id), pg_id (the superseded
    source fact to acknowledge).
    """
    coordinator_url = COORDINATOR_BASE
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            r = await client.post(
                f"{coordinator_url}/memory/review_hold",
                json={"summary_id": summary_id, "pg_id": pg_id},
                headers=_auth_headers(),
            )
            result = _reply_json(r, "review_hold")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)
    if result.get("status") == "success":
        return result.get("message", f"Summary {summary_id}: supersession of {pg_id} held.")
    return f"Error: {result.get('message', result)}"


# The gateway only reports agent and role on authenticated /health from here on.
ROLE_REPORTING_MIN_VERSION = "0.9.54"


def _gateway_predates(version: str | None, minimum: str = ROLE_REPORTING_MIN_VERSION) -> bool | None:
    """Whether ``version`` names a gateway release strictly before ``minimum``, returning None when it cannot be parsed, which the caller treats as predating."""
    try:
        parsed = tuple(int(p) for p in str(version).split("."))
        floor = tuple(int(p) for p in minimum.split("."))
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed < floor


def _role_diagnosis(payload: dict) -> str:
    """Tell the three reasons `role` can be missing apart: present, absent because the gateway is too old to send it, or absent because this caller's token was not accepted."""
    if "role" in payload:
        return payload.get("role")
    predates = _gateway_predates(payload.get("version"))
    if predates is False:
        return "not reported (token not accepted — anonymous payload)"
    gw = payload.get("version")
    if gw is not None:
        return f"not reported (gateway {gw} predates {ROLE_REPORTING_MIN_VERSION})"
    return f"not reported (gateway version unknown, predates {ROLE_REPORTING_MIN_VERSION} assumed)"


@mcp.tool()
async def check_memory_health() -> str:
    """
    Full-stack diagnostic for the shared-memory infrastructure.

    Reports what the gateway reports rather than opening its own database
    connection to count rows. The gateway is the component that knows whether
    the stack is healthy; asking it is also the only check that exercises the
    path this client actually uses.

    Read `status` (ok | degraded | down) and `dependencies` first: one enum per
    dependency — postgres, neo4j, embedder, reranker, llm_pool, rem_daemon,
    nrem_daemon, outbox, registry — each with a `reason` when it is not ok.
    `warnings` lists every limit that has been crossed, as
    {key, limit, observed, unit}; the NUMBER behind each one is on
    memory_telemetry.

    ⛔ HTTP 503 MEANS ONE THING: the embedder or the reranker is down, so a save
    cannot produce a vector. Every other verdict — a dead Postgres, a failing
    outbox, a stalled daemon — is served 200 with the enum in the body.

    That full detail requires AGENT_TOKEN to be set (S-10, PR A5): a
    credential-less caller gets liveness only (status/version/api_version) —
    the backend roster and per-backend pool state are operational
    information about this deployment, not something every unauthenticated
    caller should learn.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(f"{COORDINATOR_BASE}/health",
                                    headers=_auth_headers())
        payload = _reply_json(resp, "check_memory_health")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return json.dumps({"status": "unreachable",
                           "gateway": COORDINATOR_BASE,
                           "error": str(exc),
                           "hint": "systemctl --user start hive-mind-gateway.service"},
                          indent=2)
    # Both ride on the authenticated payload: `agent` passes through when
    # present, and `role` always gets a line from the three-way diagnosis.
    payload["role"] = _role_diagnosis(payload)
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

    THE NUMBERS, with the limit stated next to each one: counters, gauges,
    percentiles and censuses over both backends. `encoders` (per-call embed and
    rerank latency), `gateway` (request rate, status split, latency, in-flight,
    load-shed), `outbox` (apply latency, drain rate, and `failed` ALWAYS present
    even at zero), `postgres`/`neo4j` (pool and query latency), `rem`, `nrem`,
    `registry` (row counts and ingress refusals), `llm`, `clients`, plus the
    consolidation, spine, breakdown and compliance rollups.

    Use this for "how bad is it" and check_memory_health for "is it usable".
    `generated_at` is when the payload was BUILT (it is cached briefly) and
    `timestamp` when it was served. Read-only; no direct database access needed.
    The full key-by-key contract is Documentation/telemetry-contract.md.
    """
    coordinator_url = COORDINATOR_BASE
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(
                f"{coordinator_url}/memory/telemetry", headers=_auth_headers()
            )
        return json.dumps(_reply_json(resp, "memory_telemetry"), indent=2)
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)


# ── Reads ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def record_lineage(ref: str) -> str:
    """Answer "what happened to this record?" — its state, its dream-cycle
    stamps (applied → rem_reviewed → consolidated), and which summary it was
    folded into, with the fact→summary latency.

    This is a READ — GET /memory/status/{ref} makes no mutation, so it is on
    the gateway's read-role allowlist and a read-only agent token reaches it
    fine, no 403.

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
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=False) as client:
            r = await client.get(f"{COORDINATOR_BASE}/memory/status/{ref}",
                                 headers=_auth_headers())
            return json.dumps(_reply_json(r, "record_lineage"), indent=2, default=str)
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)


@mcp.tool()
async def graph_query(cypher: str) -> str:
    """
    Run a READ-ONLY Cypher query against the knowledge graph.

    Requires a token with full or admin role (read-only tokens receive 403).
    The gateway enforces read-only: CREATE, DELETE, DETACH DELETE, SET, MERGE,
    CALL, LOAD CSV and DROP are rejected there, not here — a client-side check
    would be advisory only. Write-Cypher is blocked for everyone.
    """
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=False) as client:
            r = await client.post(f"{COORDINATOR_BASE}/memory/graph",
                                  json={"cypher": cypher, "params": {}},
                                  headers=_auth_headers())
            payload = _reply_json(r, "graph_query")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)
    return json.dumps(payload.get("records", payload), indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
