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
import pwd
import random
import re
import socket
import struct
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx
from aiohttp import web
from neo4j import AsyncGraphDatabase

from log_hygiene import AsyncLineWriter
from ontology import ONT

log = logging.getLogger("coordinator")

try:
    from gpu_load import inference_busy_state
except Exception as _gpu_exc:  # pragma: no cover - import-time safety only
    # The busy signal is observability, never load-bearing: if gpu_load can't be
    # imported the gateway must still serve. Fall back to "unknown" so the monitor
    # never renders a false "idle" (it cannot tell, and says so).
    log.warning("gpu_load.inference_busy_state unavailable (%s) — "
                "inference_busy will report 'unknown'", _gpu_exc)

    async def inference_busy_state() -> str:  # type: ignore[misc]
        return "unknown"


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to default on unset/invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s: invalid int %r — using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to default on unset/invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s: invalid float %r — using default %s", name, raw, default)
        return default


# ── Version contract ────────────────────────────────────────────────────────────
# FRAMEWORK_VERSION is the informational build/semver — it changes every release.
# API_VERSION is the wire contract between memory_bridge.py (the thin client that
# ships with the skill) and this coordinator. Bump it ONLY when the request or
# response shape, auth scheme, or routes change in a way that breaks older clients.
# Client and server build-versions are allowed to drift; their API_VERSION must agree.
FRAMEWORK_VERSION = "0.6.0"
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

# Client WRITE routes — shed (503 + Retry-After) while a backup quiesce is active.
# Reads (search/graph/telemetry/status) and /health always flow.
_WRITE_ROUTES: set[tuple[str, str]] = {
    ("POST", "/memory/save"),
    ("POST", "/memory/retrospective"),
    ("POST", "/memory/supersede"),
    ("POST", "/memory/review_hold"),
}

# Admin-only routes — reachable ONLY by an "admin"-role token, which in turn can
# reach nothing else (least privilege: a leaked backup token can only pause/resume
# backups). Backup quiesce/resume is the first such route.
_ADMIN_ROUTES: set[tuple[str, str]] = {
    ("POST", "/admin/backup"),
}

# When set, write routes require a kernel-attested principal — i.e. the client must
# connect over the AF_UNIX listener (SO_PEERCRED), not TCP. OFF by default so the TCP
# path keeps working during rollout; turn ON once every writer is on the UDS to
# guarantee every stored fact carries a non-repudiable person identity.
GATEWAY_REQUIRE_PRINCIPAL = os.environ.get(
    "GATEWAY_REQUIRE_PRINCIPAL", ""
).strip().lower() in ("1", "true", "yes", "on")


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
        if role not in ("read", "full", "admin"):
            log.warning(
                "AGENT_ROLES: unknown role %r for %r — expected 'read', 'full', or "
                "'admin'; ignoring (agent keeps full access)", role, name,
            )
            continue
        result[name] = role
    return result


_AGENT_ROLES: dict[str, str] = _load_agent_roles()


def _read_role_permits(request: web.Request) -> bool:
    """True if a read-only role may reach this route (exact method+path allowlist)."""
    path = request.path.rstrip("/") or "/"
    return (request.method, path) in _READ_ROLE_ROUTES


# ── Pluggable identity resolution ───────────────────────────────────────────────
#
# resolve_identity() walks an ordered list of resolvers and returns the first
# verified agent name. Today only Bearer-token resolution is wired; the planned
# PoP (asymmetric-key + proof-of-possession) overhaul appends a second resolver
# here and bumps API_VERSION. Everything downstream — the role check, the audit
# hook, and every handler — only ever sees the resolved *name*, so none of it
# changes when the scheme does. (This is the seam PoP plugs into.)
AUTH_SCHEME = "bearer"


def _resolve_bearer(request: web.Request) -> str | None:
    """Map ``Authorization: Bearer <token>`` to a verified agent name, or None."""
    parts = request.headers.get("Authorization", "").split(maxsplit=1)
    if len(parts) != 2 or parts[0] != "Bearer":
        return None
    return _AGENT_TOKENS.get(parts[1])


_IDENTITY_RESOLVERS = [_resolve_bearer]


def resolve_identity(request: web.Request) -> str | None:
    """First resolver to recognise the request wins; None if none authenticate."""
    for resolver in _IDENTITY_RESOLVERS:
        name = resolver(request)
        if name:
            return name
    return None


# ── Person axis: the principal (OS account), kernel-attested ─────────────────────
#
# Identity has two orthogonal axes: the AGENT (which tool — resolved above) and the
# PRINCIPAL (which human is accountable). The principal is NEVER carried in the
# request and is NEVER inferred from the agent: it is the OS login account behind
# the connection, read from the kernel via SO_PEERCRED on the AF_UNIX listener.
# Local users are their logged-in account; remote users reach the gateway over SSH
# (which already authenticated them by public key), and the agent process inherits
# that login UID. The peer cannot lie about it — it is the kernel's word, not a
# client claim, so the connecting party can neither forge nor repudiate it.
#
# Alongside the username we capture the *connection fingerprint* (pid, the immutable
# audit loginuid, and the audit session id). loginuid is set once by PAM at login
# and cannot be changed thereafter (kernel audit subsystem), so it survives fork and
# setuid — a non-repudiable handle on the login session. The session id resolves the
# remote host on demand via `loginctl show-session <id>` (RemoteHost) — i.e. final
# resolution to the person is deliberately left to the OS records, not duplicated
# here. On a TCP transport there is no peer credential, so the principal is honestly
# None (unknown); the gateway never guesses. (Pre-PoP person-identity foundation —
# decision pg_id 347.)
_LOGINUID_UNSET = 0xFFFFFFFF  # /proc/<pid>/loginuid when no login session is attached


def _proc_login_context(pid: int) -> dict[str, Any]:
    """Best-effort, world-readable login fingerprint for a pid: the immutable audit
    loginuid (+ its username) and the audit session id. Empty dict if unreadable
    (e.g. hidepid, or the process already exited). Never raises."""
    ctx: dict[str, Any] = {}
    try:
        with open(f"/proc/{pid}/loginuid") as fh:
            luid = int(fh.read().strip())
        if 0 <= luid < _LOGINUID_UNSET:
            ctx["login_uid"] = luid
            try:
                ctx["login_user"] = pwd.getpwuid(luid).pw_name
            except KeyError:
                pass
    except (OSError, ValueError):
        pass
    try:
        with open(f"/proc/{pid}/sessionid") as fh:
            sid = fh.read().strip()
        if sid and sid != str(_LOGINUID_UNSET):
            ctx["session"] = sid
    except OSError:
        pass
    return ctx


def _peer_identity(request: web.Request) -> dict[str, Any] | None:
    """Kernel-attested identity of the connecting peer via SO_PEERCRED, or None on a
    non-UDS transport. Server-derived; the client cannot assert or override any field.

    Returns {user, uid, gid, pid, [login_uid, login_user, session]} — the username is
    the queryable principal; the rest is the connection fingerprint that lets the
    audit resolve back to the human against the OS's own records."""
    transport = request.transport
    if transport is None:
        return None
    sock = transport.get_extra_info("socket")
    if sock is None or sock.family != socket.AF_UNIX:
        return None  # TCP/loopback: no kernel peer credential — principal is unknown
    try:
        # struct ucred = { pid_t pid; uid_t uid; gid_t gid; } — three native ints.
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error):
        return None
    ident: dict[str, Any] = {"uid": uid, "gid": gid, "pid": pid}
    try:
        ident["user"] = pwd.getpwuid(uid).pw_name
    except KeyError:
        ident["user"] = str(uid)  # uid with no passwd entry — record the number
    ident.update(_proc_login_context(pid))
    return ident


# Fields the server owns on the person axis. The client may never set these: they are
# stripped from any client payload and re-stamped from the kernel-attested principal.
_PRINCIPAL_KEYS = ("uid", "gid", "pid", "login_uid", "login_user", "session")


def _apply_principal(target: dict[str, Any], principal: dict[str, Any] | None) -> dict[str, Any]:
    """Stamp the operator identity DETERMINISTICALLY onto a payload dict.

    Whatever the client put in `principal` / `connected_from` is STRIPPED first, then
    the kernel-attested values (from auth_middleware via SO_PEERCRED) are written. An
    agent told to "save as someone else" therefore cannot move these — at most it can
    write a separate narrative claim (e.g. decision.decided_by). When `principal` is
    None (TCP transport, no kernel credential) the fields are simply absent — honestly
    unknown, never guessed. The same enforcement applies to every write path."""
    if not isinstance(target, dict):
        return target
    target.pop("principal", None)
    target.pop("connected_from", None)
    if principal:
        target["principal"]      = principal.get("user")
        target["connected_from"] = {k: principal[k] for k in _PRINCIPAL_KEYS if k in principal}
    return target


# ── Governance: outer in-flight load-shed valve ─────────────────────────────────
_inflight = 0

# ── Backup quiesce: client-write shed + daemon advisory-lock gate ───────────────
# While a backup runs, client WRITE routes shed (503 + Retry-After) so the dump
# sees a quiet database; reads always flow. Set/cleared via POST /admin/backup by
# an admin-role token. _backup_quiesce mirrors the state for the auth chokepoint
# and is surfaced on /health as backup_in_progress. The REM/NREM daemons are fenced
# separately through a Postgres advisory lock (MemoryCoordinator._begin_quiesce):
# the gateway holds it EXCLUSIVE for the dump, each daemon takes it SHARED per cycle
# and skips when it can't — so a dump never races a daemon write.
_backup_quiesce: bool = False

# Single well-known advisory-lock key shared by the gateway (exclusive) and the
# REM/NREM daemons (shared). MUST match BACKUP_ADVISORY_LOCK_KEY in rem_loop.py and
# consolidation_loop.py. Postgres drops session advisory locks on disconnect, so a
# crashed gateway or daemon never wedges the others.
BACKUP_ADVISORY_LOCK_KEY    = _env_int("BACKUP_ADVISORY_LOCK_KEY", 8765309)
# Seconds the gateway waits for in-flight daemon cycles to release their shared lock
# before reporting drain_timeout. Bounds the quiesce handshake.
BACKUP_DAEMON_DRAIN_TIMEOUT = _env_float("BACKUP_DAEMON_DRAIN_TIMEOUT", 45.0)
# TTL safety net: auto-resume if a backup script dies without calling resume, so a
# crashed backup can never wedge writes. The script passes its own max_seconds.
BACKUP_QUIESCE_MAX_SECONDS  = _env_float("BACKUP_QUIESCE_MAX_SECONDS", 900.0)
# Retry-After (seconds) handed to a write shed while quiesced.
BACKUP_RETRY_AFTER          = _env_int("BACKUP_RETRY_AFTER", 30)


def backup_quiesce_active() -> bool:
    """True while a backup quiesce is in effect (read by /health and the chokepoint)."""
    return _backup_quiesce


# ── Thin per-request audit hook ─────────────────────────────────────────────────
def _audit(agent: str, method: str, path: str, status: int,
           latency_ms: float, request_id: str,
           principal: dict[str, Any] | None = None) -> None:
    """Append one JSON line recording a completed request. Best-effort and OFF the
    DB hot path: it never touches Postgres (so audit volume can't steal the pool's
    connection budget) and a logging failure never surfaces into the request.
    No-op unless GATEWAY_AUDIT_LOG_PATH is set. The identity is the verified agent
    name — when PoP lands the same rows become non-repudiable with no schema change.

    The write goes through _audit_writer (an AsyncLineWriter): the line is enqueued
    O(1) and a background task does the disk append in an executor, so the write
    never blocks the event loop. Rotation/gzip is handled by logrotate(8).
    """
    if _audit_writer is None:
        return
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "role": _AGENT_ROLES.get(agent, "full"),
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": round(latency_ms, 1),
            "request_id": request_id,
        }
        # Person axis: the kernel-attested OS account + connection fingerprint. None
        # on the TCP transport. Server-derived (SO_PEERCRED) — never a client claim,
        # so the operator can neither forge nor repudiate it.
        if principal:
            record["principal"]      = principal.get("user")
            record["connected_from"] = {
                k: principal[k] for k in
                ("uid", "gid", "pid", "login_uid", "login_user", "session")
                if k in principal
            }
        line = json.dumps(record, separators=(",", ":"))
        _audit_writer.write(line)
    except Exception as exc:  # never break a request because auditing failed
        log.warning("audit write failed: %s", exc)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """DEFAULT DENY, and the single identity → govern → audit choke point.

    Order: resolve a verified identity (pluggable — bearer today, PoP later) →
    enforce read-only role → shed if over the in-flight cap → dispatch → audit
    the outcome. A DB pool that stays saturated past POOL_ACQUIRE_TIMEOUT surfaces
    as asyncio.TimeoutError from a handler's _acquire(); it is mapped here to
    503 + Retry-After so the gateway sheds load instead of hanging a caller.
    """
    global _inflight
    _check_client_version(request)  # logs API skew to the gateway log; never raises
    if not _AGENT_TOKENS:
        return await handler(request)
    if request.path.rstrip("/") in _UNPROTECTED_PATHS or request.path in _UNPROTECTED_PATHS:
        return await handler(request)

    agent_name = resolve_identity(request)
    if not agent_name:
        raise web.HTTPUnauthorized(reason="Authorization: a valid Bearer token is required")
    request["authenticated_agent"] = agent_name
    # Person axis: stamp the kernel-attested principal (OS account + connection
    # fingerprint) from SO_PEERCRED. None on the TCP transport — never inferred from
    # the agent. Every handler and the audit hook read it from here, spoof-proof.
    principal = _peer_identity(request)
    request["principal"] = principal
    # Role + governance gate. Read-only roles are confined to the telemetry/graph
    # allowlist; admin-role tokens are confined to /admin/* (and no other role may
    # reach an admin route); and while a backup quiesce is active the write routes
    # shed 503 + Retry-After so the dump sees a quiet DB. Reads always flow — so a
    # leaked monitor token cannot save/supersede/proxy, and a leaked backup token
    # can only pause/resume backups.
    role  = _AGENT_ROLES.get(agent_name, "full")
    route = (request.method, request.path.rstrip("/") or "/")
    if role == "read" and not _read_role_permits(request):
        raise web.HTTPForbidden(
            reason="Read-only token: this route requires a write-capable agent token",
        )
    if route in _ADMIN_ROUTES:
        if role != "admin":
            raise web.HTTPForbidden(reason="This route requires an admin-role token")
    else:
        if role == "admin":
            raise web.HTTPForbidden(reason="Admin token is confined to /admin/* routes")
        if _backup_quiesce and route in _WRITE_ROUTES:
            raise web.HTTPServiceUnavailable(
                reason="backup in progress — writes are briefly paused",
                headers={"Retry-After": str(BACKUP_RETRY_AFTER)},
            )
        if GATEWAY_REQUIRE_PRINCIPAL and route in _WRITE_ROUTES and principal is None:
            raise web.HTTPForbidden(
                reason="writes require a kernel-attested principal — connect over the "
                       "gateway Unix socket (GATEWAY_UDS_PATH), not TCP",
            )

    # Outer load-shed valve (disabled when GATEWAY_INFLIGHT_MAX == 0). Caps total
    # concurrent requests — including ones parked on a slow embedding/LLM call
    # that hold no DB connection — which the pool timeout alone cannot bound.
    if GATEWAY_INFLIGHT_MAX and _inflight >= GATEWAY_INFLIGHT_MAX:
        raise web.HTTPServiceUnavailable(
            reason="gateway at capacity", headers={"Retry-After": "1"},
        )

    started    = asyncio.get_running_loop().time()
    request_id = uuid.uuid4().hex[:12]
    status     = 500
    _inflight += 1
    try:
        resp = await handler(request)
        status = resp.status
        return resp
    except asyncio.TimeoutError:
        # DB pool stayed saturated past POOL_ACQUIRE_TIMEOUT — shed, don't hang.
        status = 503
        raise web.HTTPServiceUnavailable(
            reason="database pool saturated", headers={"Retry-After": "1"},
        )
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        _inflight -= 1
        latency_ms = (asyncio.get_running_loop().time() - started) * 1000
        _audit(agent_name, request.method, request.path, status, latency_ms,
               request_id, request.get("principal"))

# ── Config ────────────────────────────────────────────────────────────────────

_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_DSN   = os.environ.get(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", os.environ.get("NEO4J_PASSWORD", ""))

# Bound the Neo4j driver pool so a burst of concurrent searches (or daemon
# traffic sharing this driver) cannot queue indefinitely. acquisition_timeout
# fails fast instead of blocking forever when the pool is saturated.
NEO4J_MAX_POOL        = _env_int("NEO4J_MAX_POOL", 50)
NEO4J_ACQUIRE_TIMEOUT = _env_float("NEO4J_ACQUIRE_TIMEOUT", 30.0)

# Both inference backends are called directly so the coordinator does not
# route through its own auth middleware (which would require a valid token
# for an internal call).  External agents still go through :8888 and must
# authenticate; the coordinator is trusted and bypasses that layer.
EMBED_URL  = "http://localhost:8070/v1/embeddings"
RERANK_URL = "http://localhost:8071/v1/reranking"

EMBED_RETRIES = 4
EMBED_BACKOFF = 0.5      # seconds × attempt number  (0.5 s, 1 s, 1.5 s, 2 s)

# Pool sizing is a SYSTEM budget, not just a coordinator knob: Postgres
# max_connections must cover this pool + REM (1 conn) + NREM (per-op) + the
# LISTEN connection + headroom. POOL_ACQUIRE_TIMEOUT bounds how long a request
# waits for a free connection — on expiry the request sheds (503 + Retry-After)
# via auth_middleware instead of hanging the gateway under concurrent load.
POOL_MIN = _env_int("POOL_MIN", 2)
POOL_MAX = _env_int("POOL_MAX", 20)
POOL_ACQUIRE_TIMEOUT = _env_float("POOL_ACQUIRE_TIMEOUT", 5.0)

OUTBOX_POLL_INTERVAL = 2.0   # seconds between outbox drain cycles
OUTBOX_BATCH_SIZE    = 20    # rows processed per cycle
OUTBOX_MAX_RETRIES   = 5     # row marked 'failed' after this many Neo4j errors
CONSISTENCY_TIMEOUT  = 15.0  # seconds to wait for ?consistency=neo4j

# Per-row exponential backoff for failed outbox rows. Without it, a down Neo4j
# turns the 2 s drain cycle into a retry storm (BATCH_SIZE rows × every poll).
# A failed row's next_attempt_at is pushed out by base·2^retries (capped),
# jittered, so a Neo4j outage backs off instead of hammering.
OUTBOX_BACKOFF_BASE = _env_float("OUTBOX_BACKOFF_BASE", 2.0)   # seconds
OUTBOX_BACKOFF_MAX  = _env_float("OUTBOX_BACKOFF_MAX", 300.0)  # seconds (cap)

# Per-entity write-lock registry size. Locks are kept only for keys in active
# use; idle locks are evicted LRU once the registry exceeds this bound, so the
# map cannot grow unbounded with unique entity names over months of operation.
LOCKS_MAX_SIZE = _env_int("LOCKS_MAX_SIZE", 4096)

# Outer load-shed valve: cap concurrent in-flight requests at the auth seam.
# 0 = disabled (default). Complements POOL_ACQUIRE_TIMEOUT — the semaphore caps
# total requests (incl. those parked on embeddings/LLM that hold no DB conn);
# the pool timeout protects the DB connection budget specifically.
GATEWAY_INFLIGHT_MAX = _env_int("GATEWAY_INFLIGHT_MAX", 0)

# Thin per-request observability audit log (JSON-lines, append-only, OFF the DB
# hot path). Records {ts, agent, role, method, path, status, latency_ms,
# request_id}. Unset = disabled. Identity is the verified agent name — when PoP
# auth lands, the same rows become non-repudiable with no schema change.
GATEWAY_AUDIT_LOG_PATH = os.environ.get("GATEWAY_AUDIT_LOG_PATH", "").strip()

# Off-event-loop writer for the audit log (None = auditing disabled). Created at
# import; its drain task starts lazily on the first write within a running loop.
_audit_writer = AsyncLineWriter(GATEWAY_AUDIT_LOG_PATH) if GATEWAY_AUDIT_LOG_PATH else None

# NREM dream-cycle backlog gauge (GET /memory/telemetry). A "cycle" is one
# (entity, domain) cluster that meets the consolidation density threshold —
# the unit NREM actually fires on, NOT the raw unconsolidated fact count. Fact
# clusters reuse ONT.density_threshold (the same value consolidation_loop.py
# gates on). Decision clusters track the Phase 3a insight-consolidation design
# (≥2 rem_processed, unconsolidated decisions per (entity, domain)).
DEFAULT_DOMAIN = "general"
NREM_DECISION_THRESHOLD = 2

# ── Consolidation health signal (ADR-018) ───────────────────────────────────
# The coordinator rolls up the daemon's consolidation_runs ledger into a cached
# snapshot that /health and /memory/telemetry read. /health is polled frequently
# and must stay DB-free, so a background task refreshes the snapshot rather than
# querying per probe. STALL threshold defaults to 2.5× the NREM sweep interval
# (the insight cycle rides every sweep) so a single deferred/failed sweep never
# trips it; two consecutive failures do.
_NREM_SWEEP_INTERVAL_SEC = int(os.environ.get("NREM_SWEEP_INTERVAL_SEC", "3600"))
CONSOLIDATION_STALL_THRESHOLD_SEC = int(os.environ.get(
    "CONSOLIDATION_STALL_THRESHOLD_SEC", str(int(2.5 * _NREM_SWEEP_INTERVAL_SEC))))
CONSOLIDATION_HEALTH_REFRESH_SEC = int(os.environ.get("CONSOLIDATION_HEALTH_REFRESH_SEC", "60"))
# An in-flight run row older than this is treated as a dead-mid-fold orphan, not
# a live fold — so a crashed daemon cannot peg in_flight=true forever (the daemon
# also reaps these on restart; this is the read-side backstop).
CONSOLIDATION_ORPHAN_TIMEOUT_SEC = int(os.environ.get("CONSOLIDATION_ORPHAN_TIMEOUT_SEC", "1800"))


def _consolidation_backlog(eligible_clusters, nrem_count) -> int:
    """Backlog for the stall verdict = the cycle's OWN gate census
    (eligible_clusters, the strict insight gate) when the daemon has recorded
    one; else the looser nrem density count as a fresh-deploy fallback. Using
    nrem alone falsely flags a stall when nrem sees a dense cluster the insight
    gate rejects (≥2 projects / HAD_OUTCOME / non-mega-hub). Pure → testable."""
    return eligible_clusters if eligible_clusters is not None else nrem_count


def _consolidation_stall_verdict(last_success_age, in_flight, has_backlog, threshold) -> bool:
    """Pure stall rule (ADR-018): a cycle is stalled when an eligible backlog
    exists, no successful fold landed within the threshold (or none ever), and
    nothing is currently in-flight. Extracted so the verdict is unit-testable
    without a database."""
    if not has_backlog or in_flight:
        return False
    return last_success_age is None or last_success_age > threshold

# Canonical project names (decision pg_id 276): the project folder name is
# canonical, and free-text drift ("shared_memory" vs "shared-memory") breaks
# the insight gate's ≥2-distinct-projects rule. PROJECT_ALIASES maps legacy
# spellings to the canonical name at ingress, e.g.
#   PROJECT_ALIASES="shared_memory=shared-memory-GitHub,shared-memory=shared-memory-GitHub"
# Empty (default) = no rewriting. One-time backfill of existing rows/nodes:
# scripts/normalize_projects.py.
def _parse_project_aliases(raw: str) -> dict[str, str]:
    aliases = {}
    for pair in raw.split(","):
        old, sep, new = pair.partition("=")
        if sep and old.strip() and new.strip():
            aliases[old.strip()] = new.strip()
    return aliases


PROJECT_ALIASES = _parse_project_aliases(os.environ.get("PROJECT_ALIASES", ""))


def _normalize_project(name):
    return PROJECT_ALIASES.get(name, name) if isinstance(name, str) else name


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


def _outbox_backoff_delay(retries: int) -> float:
    """Seconds to defer the next attempt of a failed outbox row.

    Exponential in the retry count, capped at OUTBOX_BACKOFF_MAX, then jittered
    ±50% so concurrent failures don't re-converge on the same instant. Pure (no
    I/O) so the schedule is unit-testable.
    """
    base = min(OUTBOX_BACKOFF_MAX, OUTBOX_BACKOFF_BASE * (2 ** retries))
    return base * (0.5 + random.random())


class BoundedKeyedLocks:
    """Per-key ``asyncio.Lock`` registry with a bounded number of entries.

    The unbounded ``dict[str, Lock]`` it replaces grew one permanent lock per
    unique entity name, leaking for the life of the process. Here, idle locks
    are evicted LRU once the registry passes ``max_size``.

    Safety: a lock is only evicted when it is provably unreferenced — not held
    (``locked()`` is False) and with no waiters. A just-requested key is moved to
    MRU before any eviction runs, so a caller that does ``lk = await get(k)``
    immediately followed by ``await lk.acquire()`` cannot have its lock evicted
    out from under it (it is the most-recently-used entry, never the LRU victim).
    Eviction is best-effort: if every over-budget candidate is still in use the
    map may briefly exceed the bound rather than ever drop a live lock — memory
    over correctness. This is the same pattern a future PoP nonce/replay cache
    reuses with a TTL victim test instead of the lock-state test.
    """

    def __init__(self, max_size: int) -> None:
        self._max = max(1, max_size)
        self._locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
        self._mu = asyncio.Lock()

    async def get(self, key: str) -> asyncio.Lock:
        async with self._mu:
            lk = self._locks.get(key)
            if lk is None:
                lk = asyncio.Lock()
            self._locks[key] = lk
            self._locks.move_to_end(key)          # mark MRU — never this call's victim
            if len(self._locks) > self._max:
                self._evict_idle()
            return lk

    def _evict_idle(self) -> None:
        # Oldest-first; drop only locks nobody is holding or waiting on.
        for k in list(self._locks.keys()):
            if len(self._locks) <= self._max:
                break
            lk = self._locks[k]
            waiters = getattr(lk, "_waiters", None)
            if not lk.locked() and not waiters:
                del self._locks[k]

    def __len__(self) -> int:                      # for tests / introspection
        return len(self._locks)


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
        self._locks = BoundedKeyedLocks(LOCKS_MAX_SIZE)
        self._outbox_task: asyncio.Task | None = None
        # Backup quiesce: dedicated connection holding the EXCLUSIVE advisory lock
        # (None = not held), plus the TTL auto-resume task.
        self._quiesce_conn: Any = None
        self._quiesce_timer: asyncio.Task | None = None
        # ADR-018 consolidation health: cached snapshot refreshed by a background
        # task so /health stays DB-free. Defaults read as "unknown" until the
        # first refresh lands (stalled is never asserted on no data).
        self._consolidation_health: dict = {"stalled": False, "last_outcome": None,
                                             "last_success_age_seconds": None,
                                             "inference_busy": "unknown", "fresh": False}
        self._consolidation_health_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            PG_DSN, min_size=POOL_MIN, max_size=POOL_MAX,
            init=self._init_connection,
        )
        async with self._acquire() as conn:
            result = await conn.execute(
                "UPDATE neo4j_outbox SET status='pending' WHERE status='in_progress'"
            )
            recovered = int(result.split()[-1])
            if recovered:
                log.warning("outbox startup: recovered %d in_progress row(s) → pending", recovered)
        self._neo4j = AsyncGraphDatabase.driver(
            NEO4J_URI, auth=NEO4J_AUTH,
            max_connection_pool_size=NEO4J_MAX_POOL,
            connection_acquisition_timeout=NEO4J_ACQUIRE_TIMEOUT,
        )
        self._outbox_task = asyncio.create_task(self._outbox_worker(), name="outbox-worker")
        self._consolidation_health_task = asyncio.create_task(
            self._consolidation_health_refresher(), name="consolidation-health")
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
        for task in (self._outbox_task, self._consolidation_health_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._end_quiesce()   # release the backup advisory lock if held
        if _audit_writer is not None:
            try:
                await _audit_writer.aclose()   # flush queued audit lines, stop drain
            except Exception:
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

    def _acquire(self):
        """Acquire a pooled connection, bounded by POOL_ACQUIRE_TIMEOUT.

        asyncpg raises ``asyncio.TimeoutError`` when the pool stays saturated
        past the timeout. Request handlers let it propagate to auth_middleware,
        which maps it to 503 + Retry-After — the gateway sheds load instead of
        blocking a caller on the pool forever. Background tasks (outbox worker)
        catch it in their own loop and retry on the next cycle.
        """
        return self._pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT)

    async def _lock_for(self, entity: str) -> asyncio.Lock:
        return await self._locks.get(entity)

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
        async with self._acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE neo4j_outbox SET status = 'in_progress'
                    WHERE id IN (
                        SELECT id FROM neo4j_outbox
                        WHERE status = 'pending' AND retries < $1
                          AND (next_attempt_at IS NULL OR next_attempt_at <= now())
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

            if params.get("type") == "supersede":
                await self._apply_supersede_outbox_row(outbox_id, params)
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
                # Fact supersession mirror (decision 381/384), piggybacked on this
                # fact's row: flag the old Fact node so REM/NREM exclude it, and
                # link (new)-[:SUPERSEDES]->(old) — same relationship + direction
                # community-summary supersession uses. MATCH-only on old so a
                # missing node (pre-coordinator fact) is a no-op, never a phantom.
                supersedes = params.get("supersedes")
                if supersedes is not None:
                    # MERGE (not MATCH) on old: the outbox worker may apply the new
                    # fact's row before the old fact's own row, so the old node may
                    # not exist yet. MERGE marks it (stub if needed); the old fact's
                    # later row-apply only SETs content, never clearing superseded.
                    await session.run(
                        f"MERGE (old:{ONT.fact} {{pg_id: $old_id}})"
                        f" SET old.superseded = true"
                        f" WITH old"
                        f" MATCH (new:{ONT.fact} {{pg_id: $new_id}})"
                        f" MERGE (new)-[:{ONT.supersedes}]->(old)",
                        old_id=supersedes, new_id=pg_id,
                    )
            async with self._acquire() as conn:
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
            async with self._acquire() as conn:
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
                    # Exponential backoff with jitter so a Neo4j outage backs off
                    # rather than re-hammering BATCH_SIZE rows every poll cycle.
                    delay = _outbox_backoff_delay(retries)
                    await conn.execute(
                        "UPDATE neo4j_outbox"
                        " SET retries=retries+1, status='pending',"
                        "     next_attempt_at = now() + make_interval(secs => $2)"
                        " WHERE id=$1",
                        outbox_id, delay,
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
        async with self._acquire() as conn:
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
        # Reversal (decision 276): the cascade is decision-level only — the
        # graph node mirrors technical_docs.superseded so the insight gate's
        # fresh-cluster query can exclude reversed decisions cheaply. Insights
        # are never invalidated here; the re-fold supersedes them instead.
        superseded_clause = " SET d.superseded = true" if retro.get("superseded") else ""
        async with self._neo4j.session() as session:
            await session.run(
                f"MATCH (d:{ONT.decision} {{pg_id: $pg_id}})"
                f" CREATE (d)-[:{ONT.had_outcome} {{rating: $rating, date: $date, notes: $notes}}]->(d)"
                f"{superseded_clause}",
                pg_id=pg_id,
                rating=retro.get("rating", ""),
                date=retro.get("date", ""),
                notes=retro.get("notes", ""),
            )
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE neo4j_outbox SET status='applied', applied_at=now() WHERE id=$1",
                outbox_id,
            )
        log.debug("outbox: applied retrospective pg_id=%d (outbox_id=%d)", pg_id, outbox_id)

    async def _apply_supersede_outbox_row(self, outbox_id: int, params: dict) -> None:
        """Standalone supersession mirror for the /memory/supersede route (bare
        retract, or point an existing fact at an existing successor — no new fact
        to piggyback on). MERGE old so it is marked even if its own row has not
        applied; optional (new)-[:SUPERSEDES]->(old) edge to an existing successor.
        One-shot: the row is DELETED on success — it carries no dream lifecycle and
        must never count as working-set backlog."""
        old_id = params.get("old_pg_id")
        new_id = params.get("new_pg_id")
        async with self._neo4j.session() as session:
            if new_id is not None:
                await session.run(
                    f"MERGE (old:{ONT.fact} {{pg_id: $old_id}})"
                    f" SET old.superseded = true"
                    f" WITH old"
                    f" MERGE (new:{ONT.fact} {{pg_id: $new_id}})"
                    f" MERGE (new)-[:{ONT.supersedes}]->(old)",
                    old_id=old_id, new_id=new_id,
                )
            else:
                await session.run(
                    f"MERGE (old:{ONT.fact} {{pg_id: $old_id}}) SET old.superseded = true",
                    old_id=old_id,
                )
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM neo4j_outbox WHERE id=$1", outbox_id)
        log.debug(
            "outbox: applied supersede old=%s new=%s (outbox_id=%d, row deleted)",
            old_id, new_id, outbox_id,
        )

    async def _wait_for_outbox(self, pg_id: int) -> bool:
        """Poll until the outbox row for pg_id is applied, or CONSISTENCY_TIMEOUT expires."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONSISTENCY_TIMEOUT
        while loop.time() < deadline:
            async with self._acquire() as conn:
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
        # The agent_id COLUMN is stamped from the same verified identity — the client
        # defaults it to the script name ("memory_bridge"), so trusting the body left
        # every authenticated save recorded under that placeholder instead of the real
        # token identity. Source and agent_id now agree and are both spoof-proof.
        if request.get("authenticated_agent"):
            metadata["source"] = request["authenticated_agent"]
            agent_id = request["authenticated_agent"]
            body["metadata"] = metadata

        # Person-axis enforcement — DETERMINISTIC, never agent-supplied. The operator
        # identity + connection fingerprint are stamped from the kernel-attested
        # principal (auth_middleware → SO_PEERCRED); any client claim is stripped. See
        # _apply_principal / decision 347.
        if isinstance(metadata, dict):
            _apply_principal(metadata, request.get("principal"))
            body["metadata"] = metadata

        # Project-name normalisation (decision 276): canonical = folder name.
        # Applied before the row and its outbox params are written so the
        # graph Project node and the Postgres metadata never drift again.
        if isinstance(metadata, dict):
            if metadata.get("project"):
                metadata["project"] = _normalize_project(metadata["project"])
            decision_blob = metadata.get("decision")
            if isinstance(decision_blob, dict) and decision_blob.get("project"):
                decision_blob["project"] = _normalize_project(decision_blob["project"])
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

        # Fact supersession (decision 381, refined by 384): an optional
        # `supersedes` pointer marks an existing fact superseded by THIS save.
        # Validated at ingress — target must exist and not already be superseded —
        # before the embed/WAL work. Propagation to dependent summaries/decisions
        # is LAZY (resolved at retrieval, decision 384), so nothing else fires here
        # beyond flagging the old row; the Neo4j mirror + REM/NREM exclusion follow.
        supersedes = metadata.get("supersedes")
        if supersedes is not None:
            if isinstance(supersedes, bool) or not isinstance(supersedes, int):
                return web.json_response(
                    {"status": "error",
                     "message": "metadata.supersedes must be an integer pg_id"},
                    status=400,
                )
            async with self._acquire() as conn:
                target = await conn.fetchrow(
                    "SELECT superseded FROM technical_docs WHERE id = $1", supersedes
                )
            if target is None:
                return web.json_response(
                    {"status": "error",
                     "message": f"supersedes target {supersedes} not found"},
                    status=400,
                )
            if target["superseded"]:
                return web.json_response(
                    {"status": "error",
                     "message": f"supersedes target {supersedes} is already superseded"},
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
        # Get-and-acquire each in the sorted loop: merging the fetch with the acquire keeps
        # the lock at MRU and taken immediately, so the bounded registry can never evict a
        # lock this save is about to hold. Track only locks actually acquired: if acquire()
        # is cancelled mid-list, the finally releases only what we hold.
        acquired: list[asyncio.Lock] = []
        try:
            for e in sorted(set(entities)):
                lk = await self._lock_for(e)
                await lk.acquire()
                acquired.append(lk)
            async with self._acquire() as conn:
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
                            # Piggyback the supersession mirror onto THIS fact's row
                            # (no separate outbox row to pollute the census): when it
                            # applies, the worker also marks the old Fact node
                            # superseded and writes (new)-[:SUPERSEDES]->(old).
                            "supersedes": (
                                supersedes if (supersedes is not None
                                               and supersedes != pg_id) else None
                            ),
                        },
                    )

                    # Flag the superseded predecessor in the SAME transaction as
                    # its replacement (atomic: the correction and the retirement
                    # commit together). superseded_by powers the read-time
                    # stale_sources annotation (decision 384) as a pure Postgres
                    # join. The id != pg_id guard covers the degenerate case where
                    # identical content hash-collides onto the target row itself.
                    if supersedes is not None and supersedes != pg_id:
                        await conn.execute(
                            "UPDATE technical_docs"
                            " SET superseded = true, superseded_by = $2"
                            " WHERE id = $1 AND id != $2",
                            supersedes, pg_id,
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
        superseded_pg_id = (
            supersedes if (supersedes is not None and supersedes != pg_id) else None
        )
        sup_msg = (
            f" Superseded fact {superseded_pg_id}." if superseded_pg_id is not None else ""
        )
        return web.json_response({
            "status": "success",
            "pg_id": pg_id,
            "neo4j": neo4j_status,
            "superseded": superseded_pg_id,
            "message": f"Artifact stored with ID {pg_id}.{sup_msg}{warn}",
        })

    # ── POST /memory/supersede ────────────────────────────────────────────────

    async def handle_supersede(self, request: web.Request) -> web.Response:
        """Retract an existing fact WITHOUT saving a replacement (decision 381/384):
        `supersede {pg_id, by?}`. With `by`, point the retracted fact at an existing
        successor. Soft: the row is kept + flagged (search excludes it; provenance
        intact). The Neo4j mirror runs via a one-shot 'supersede' outbox row.

        GC (decision 389): a superseded fact rides along with its successor and is
        purged when that successor consolidates. A bare retract (no `by`) — or a
        `by` whose successor has no live outbox row to ride with — has no future
        purger, so its outbox row is purged here and logged."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )
        pg_id = body.get("pg_id")
        by    = body.get("by")
        if isinstance(pg_id, bool) or not isinstance(pg_id, int):
            return web.json_response(
                {"status": "error", "message": "pg_id (int) is required"}, status=400
            )
        if by is not None and (isinstance(by, bool) or not isinstance(by, int)):
            return web.json_response(
                {"status": "error", "message": "by must be an integer pg_id"}, status=400
            )
        if by is not None and by == pg_id:
            return web.json_response(
                {"status": "error", "message": "a fact cannot supersede itself"}, status=400
            )

        async with self._acquire() as conn:
            target = await conn.fetchrow(
                "SELECT superseded FROM technical_docs WHERE id = $1", pg_id
            )
            if target is None:
                return web.json_response(
                    {"status": "error", "message": f"fact {pg_id} not found"}, status=400
                )
            if target["superseded"]:
                return web.json_response(
                    {"status": "error", "message": f"fact {pg_id} is already superseded"},
                    status=400,
                )
            if by is not None:
                succ = await conn.fetchval(
                    "SELECT 1 FROM technical_docs WHERE id = $1", by
                )
                if succ is None:
                    return web.json_response(
                        {"status": "error", "message": f"successor {by} not found"},
                        status=400,
                    )

            purged = 0
            async with conn.transaction():
                await conn.execute(
                    "UPDATE technical_docs SET superseded = true, superseded_by = $2"
                    " WHERE id = $1",
                    pg_id, by,
                )
                await conn.execute(
                    "INSERT INTO neo4j_outbox (pg_id, cypher_params) VALUES ($1, $2::jsonb)",
                    pg_id,
                    {"type": "supersede", "old_pg_id": pg_id, "new_pg_id": by},
                )
                # Ride-along only if a live successor fact row exists to purge us
                # later; otherwise purge this fact's own dream-cycle row now.
                ride = False
                if by is not None:
                    ride = await conn.fetchval(
                        "SELECT 1 FROM neo4j_outbox WHERE pg_id = $1"
                        " AND COALESCE(cypher_params->>'type','fact') = 'fact' LIMIT 1",
                        by,
                    ) is not None
                if not ride:
                    rows = await conn.fetch(
                        "DELETE FROM neo4j_outbox WHERE pg_id = $1"
                        " AND COALESCE(cypher_params->>'type','fact') = 'fact'"
                        " RETURNING id",
                        pg_id,
                    )
                    purged = len(rows)
            if purged:
                log.info(
                    "Supersede: purged %d outbox row(s) for retracted fact %d "
                    "(no live successor to ride with).", purged, pg_id,
                )

        return web.json_response({
            "status": "success",
            "superseded": pg_id,
            "superseded_by": by,
            "purged_outbox": purged,
            "message": (
                f"Fact {pg_id} superseded"
                + (f" by {by}." if by is not None else " (retracted, no replacement).")
            ),
        })

    # ── POST /memory/review_hold ──────────────────────────────────────────────

    async def handle_review_hold(self, request: web.Request) -> web.Response:
        """Mark a summary's supersession as reviewed-and-held (decision 384, 8e):
        the consumer judged a flagged stale source immaterial, so stop surfacing it.
        Records {old, by} in community_summaries.metadata.reviewed_supersessions
        (dedup by old). A later supersession of a DIFFERENT source still surfaces;
        a re-fold (8c) makes a new summary with fresh metadata, so acks never leak."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )
        summary_id = body.get("summary_id")
        pg_id      = body.get("pg_id")
        if isinstance(summary_id, bool) or not isinstance(summary_id, int) \
           or isinstance(pg_id, bool) or not isinstance(pg_id, int):
            return web.json_response(
                {"status": "error",
                 "message": "summary_id (int) and pg_id (int) are required"},
                status=400,
            )
        async with self._acquire() as conn:
            srow = await conn.fetchrow(
                "SELECT source_pg_ids, metadata FROM community_summaries WHERE id = $1",
                summary_id,
            )
            if srow is None:
                return web.json_response(
                    {"status": "error", "message": f"summary {summary_id} not found"},
                    status=400,
                )
            if pg_id not in list(srow["source_pg_ids"] or []):
                return web.json_response(
                    {"status": "error",
                     "message": f"fact {pg_id} is not a source of summary {summary_id}"},
                    status=400,
                )
            by = await conn.fetchval(
                "SELECT superseded_by FROM technical_docs WHERE id = $1 AND superseded",
                pg_id,
            )
            meta = _coerce_jsonb_obj(srow["metadata"]) or {}
            acks = meta.get("reviewed_supersessions")
            if not isinstance(acks, list):
                acks = []
            if not any(isinstance(e, dict) and e.get("old") == pg_id for e in acks):
                acks.append({"old": pg_id, "by": by})
            # jsonb_set touches ONLY the reviewed_supersessions key in-place, so a
            # concurrent NREM re-fold rewriting other metadata keys isn't clobbered.
            await conn.execute(
                "UPDATE community_summaries"
                " SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb),"
                "                          '{reviewed_supersessions}', $2::jsonb)"
                " WHERE id = $1",
                summary_id, acks,
            )
        return web.json_response({
            "status": "success",
            "summary_id": summary_id,
            "reviewed": {"old": pg_id, "by": by},
            "message": f"Summary {summary_id}: supersession of {pg_id} marked reviewed-and-held.",
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
        # Verified token identity over the client's script-name default ("memory_bridge").
        agent_id = request.get("authenticated_agent") or body.get("agent_id", "unknown")

        if not isinstance(pg_id, int) or not rating or not notes:
            return web.json_response(
                {"status": "error", "message": "pg_id (int), rating, and notes are required"},
                status=400,
            )

        # Reversal vocabulary (decision 276): rating 'reversed' is the one
        # structural rating — it marks the DECISION superseded in both stores
        # (Tier-1 filter + fresh-cluster exclusion). All other ratings carry
        # no enum semantics; their wording reaches insights via the re-fold.
        is_reversal = rating.strip().lower() == "reversed"
        retro_payload = {"rating": rating, "date": date, "notes": notes}
        if is_reversal:
            retro_payload["superseded"] = True
        # Person-axis enforcement (see handle_save): the operator who recorded this
        # outcome is stamped from the kernel-attested principal, never from the body.
        _apply_principal(retro_payload, request.get("principal"))

        async with self._acquire() as conn:
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
                        "retrospective": retro_payload,
                        "source": agent_id,
                    },
                )
                if is_reversal:
                    try:
                        # Savepoint: a pre-migration-009 schema must not
                        # poison the retrospective save itself.
                        async with conn.transaction():
                            await conn.execute(
                                "UPDATE technical_docs SET superseded = true WHERE id = $1",
                                pg_id,
                            )
                    except Exception:
                        log.warning(
                            "technical_docs.superseded column missing — run migration "
                            "009; reversal of pg_id=%d recorded on the graph only.",
                            pg_id,
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
                async with self._acquire() as conn:
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

            async with self._acquire() as conn:
                # Tier 3 — nearest active insight (cross-project principle,
                # decision 276) surfaces ABOVE the nearest thematic summary.
                # Disjoint queries, both filtered to non-superseded rows.
                insight = None
                try:
                    insight = await conn.fetchrow(
                        "SELECT content, metadata, source_pg_ids FROM community_summaries"
                        " WHERE NOT superseded"
                        "   AND metadata->>'kind' = 'insight'"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec),
                    )
                except Exception:
                    insight = None  # pre-006 schema — thematic guard below warns

                # Thematic summary. Guard: if migration 006 has not been
                # applied, fall back to the unsupervised query so search
                # continues to work (with a warning).
                try:
                    summary = await conn.fetchrow(
                        "SELECT content, metadata, source_pg_ids FROM community_summaries"
                        " WHERE NOT superseded"
                        "   AND COALESCE(metadata->>'kind', 'thematic') <> 'insight'"
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

                # Tier 1 — vector search, 20 candidates for reranker.
                # Reversed decisions (superseded=true, migration 009) are
                # excluded; the fallback keeps pre-migration schemas working.
                scope_sql = "AND scope = $3" if scope else ""
                args: list = [str(q_vec), 20]
                if scope:
                    args.append(scope)
                try:
                    candidates = await conn.fetch(
                        f"""
                        SELECT id, content, metadata FROM technical_docs
                        WHERE NOT superseded {scope_sql}
                        ORDER BY embedding <=> $1::vector LIMIT $2
                        """,
                        *args,
                    )
                except Exception:
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

        # Retrieval-time supersession check (decision 384) — the PRIMARY mechanism.
        # A summary/insight outlives its sources, so flag any returned narrative
        # whose provenance touches a superseded fact (or a reversed decision —
        # decisions set `superseded` too, with a NULL successor). Cheap PG join via
        # the superseded_by pointer (migration 013); no LLM, no Neo4j hop. The
        # consumer judges materiality on the spot (8b) and may trigger an on-demand
        # re-fold / retrospective (8c) — propagation is never eager.
        prov_ids: set[int] = set()
        if insight:
            prov_ids.update(insight.get("source_pg_ids") or [])
        if summary:
            prov_ids.update(summary.get("source_pg_ids") or [])
        stale_map: dict[int, int | None] = {}
        if prov_ids:
            try:
                async with self._acquire() as conn:
                    srows = await conn.fetch(
                        "SELECT id, superseded_by FROM technical_docs"
                        " WHERE id = ANY($1) AND superseded",
                        list(prov_ids),
                    )
                stale_map = {r["id"]: r["superseded_by"] for r in srows}
            except Exception:
                stale_map = {}  # column missing (pre-013) — degrade to no annotation

        def _stale_sources(source_pg_ids, meta) -> list[dict]:
            # 8e: suppress supersessions already reviewed-and-held for this summary
            # (metadata.reviewed_supersessions = [{old, by}, ...]). A later, distinct
            # supersession of a different source is a new pair, so still surfaces.
            m = _coerce_jsonb_obj(meta) if not isinstance(meta, dict) else meta
            acked = {
                e["old"] for e in (m or {}).get("reviewed_supersessions", [])
                if isinstance(e, dict) and "old" in e
            }
            return [
                {"old": pid, "superseded_by": stale_map[pid]}
                for pid in (source_pg_ids or [])
                if pid in stale_map and pid not in acked
            ]

        # Neo4j relational expansion
        final: list[dict] = []
        if insight:
            # Insights rank above thematic summaries: a cross-project
            # principle validated by at least one retrospective outranks a
            # single-domain narrative. source_pg_ids are DECISION ids here.
            ins_result = {
                "tier": "insight_summary",
                "content": insight["content"],
                "score": None,
                "score_normalized": None,
                "matched_entities": [],
                "metadata": insight["metadata"],
                "source_pg_ids": insight["source_pg_ids"],
                "graph_context": [],
            }
            stale = _stale_sources(insight["source_pg_ids"], insight["metadata"])
            if stale:
                ins_result["stale_sources"] = stale
            final.append(ins_result)
        if summary:
            # Surface the summary's provenance so an agent can trace a Tier-3
            # narrative back to the exact Tier-1 facts it was synthesised from
            # (source_pg_ids) — drill down via /memory/graph or status/{pg_id}.
            sum_result = {
                "tier": "community_summary",
                "content": summary["content"],
                "score": None,
                "score_normalized": None,
                "matched_entities": [],
                "metadata": summary["metadata"],
                "source_pg_ids": summary["source_pg_ids"],
                "graph_context": [],
            }
            stale = _stale_sources(summary["source_pg_ids"], summary["metadata"])
            if stale:
                sum_result["stale_sources"] = stale
            final.append(sum_result)

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
                        # ADR-017: also pull each related Entity's alias siblings so
                        # search surfaces every surface form of a concept. One query,
                        # no-op-safe (empty when no ALIASES edges exist).
                        f" OPTIONAL MATCH (related)-[:{ONT.aliases}]-(al:{ONT.entity})"
                        " WITH r, related, labels(related) AS labels,"
                        "      collect(DISTINCT al.name) AS aliases LIMIT 5"
                        " RETURN labels, related.name as name,"
                        "        type(r) as rel_type, aliases",
                        pg_id=pg_id,
                    )
                    async for rec in result:
                        if rec["name"]:
                            entry = {
                                "rel_type": rec["rel_type"],
                                "name": rec["name"],
                                "label": rec["labels"][0] if rec["labels"] else None,
                            }
                            if rec["aliases"]:
                                entry["aliases"] = rec["aliases"]
                            ctx.append(entry)
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

        async with self._acquire() as conn:
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
            async with self._acquire() as conn:
                outbox = await conn.fetch(
                    "SELECT status, count(*) AS n FROM neo4j_outbox GROUP BY status"
                )
                # Dead-letter age: how long the oldest permanently-failed row has
                # been stuck. A non-null, growing value means Neo4j writes are
                # being abandoned — the signal a dashboard alerts on.
                failed_age = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM now() - min(created_at))::int"
                    " FROM neo4j_outbox WHERE status='failed'"
                )
                docrow = await conn.fetchrow(
                    "SELECT count(*) AS total,"
                    " count(*) FILTER (WHERE superseded) AS superseded"
                    " FROM technical_docs"
                )
                docs = docrow["total"]
                summ = await conn.fetchrow(
                    "SELECT count(*) AS total,"
                    " count(*) FILTER (WHERE superseded) AS superseded,"
                    " count(*) FILTER (WHERE metadata->>'kind'='insight') AS insight"
                    " FROM community_summaries"
                )
            snap["postgres"] = {
                "technical_docs": docs,
                "technical_docs_superseded": docrow["superseded"],
                "outbox": {r["status"]: r["n"] for r in outbox},
                "outbox_failed_oldest_age_seconds": failed_age,
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

        # Entity-graph shape (ADR-017) — the live, cheap counterpart to the
        # offline ER calibration harness (entity_resolution_eval.py). Surfaces
        # fragmentation and, once the alias layer ships, alias coverage. The O(n²)
        # cosine over-merge analysis stays OUT of the hot path; only aggregates here.
        try:
            snap["entity_graph"] = await self._entity_graph()
        except Exception as exc:
            snap["entity_graph"] = {"error": str(exc)}

        # Consolidation signal (ADR-018) — the dream-cycle liveness rollup from
        # the daemon's consolidation_runs ledger: per-cycle-type last outcome,
        # success age, in-flight, last error, plus the derived stall verdict.
        # Computed fresh here (telemetry is auth-scoped and already heavier); the
        # cheaper /health subset reads the cached snapshot instead.
        try:
            snap["consolidation"] = await self._consolidation_telemetry()
        except Exception as exc:
            snap["consolidation"] = {"error": str(exc)}

        # Inference/GPU-busy signal (tri-state: "busy"|"idle"|"unknown"). Read the
        # cached value the consolidation refresher already probed so telemetry never
        # shells out to nvtop itself. "unknown" (nvtop absent) is surfaced verbatim
        # so the monitor never shows a false "idle".
        snap["inference_busy"] = self._consolidation_health.get("inference_busy", "unknown")

        return web.json_response({"status": "success", "telemetry": snap})

    # ── Consolidation health (ADR-018) ────────────────────────────────────────

    async def _compute_consolidation_health(self) -> dict:
        """Roll up consolidation_runs into per-cycle-type liveness + the stall
        verdict. One windowed query (last-success per partition) plus the live
        backlog from _nrem_cycle_counts. stalled = backlog present AND no
        successful fold within STALL_THRESHOLD AND nothing in-flight."""
        query = """
            WITH ranked AS (
              SELECT cycle_type, started_at, finished_at, outcome, error_class, error_msg,
                     eligible_clusters, eligible_oldest_age_seconds, extra,
                     max(finished_at) FILTER (WHERE folds_succeeded > 0)
                         OVER (PARTITION BY cycle_type) AS last_success
              FROM consolidation_runs
            )
            SELECT cycle_type,
              max(last_success) AS last_success,
              (array_agg(outcome ORDER BY started_at DESC))[1] AS last_outcome,
              EXTRACT(EPOCH FROM now() - max(last_success))::int AS last_success_age,
              count(*) FILTER (WHERE finished_at IS NULL
                  AND started_at > now() - make_interval(secs => $1)) AS inflight,
              count(*) FILTER (WHERE outcome = 'crashed'
                  AND (last_success IS NULL OR started_at > last_success)) AS consec_fail,
              (array_agg(error_class ORDER BY started_at DESC)
                  FILTER (WHERE outcome = 'crashed'))[1] AS last_error_class,
              (array_agg(error_msg ORDER BY started_at DESC)
                  FILTER (WHERE outcome = 'crashed'))[1] AS last_error_msg,
              (array_agg(eligible_clusters ORDER BY started_at DESC)
                  FILTER (WHERE eligible_clusters IS NOT NULL))[1] AS eligible_clusters,
              (array_agg(eligible_oldest_age_seconds ORDER BY started_at DESC)
                  FILTER (WHERE eligible_oldest_age_seconds IS NOT NULL))[1] AS eligible_oldest_age,
              -- Reason of the most-recent deferral (e.g. 'gpu_busy' | 'backup_drain'),
              -- written to consolidation_runs.extra by the daemon. Lets the monitor
              -- show "deferred — inference GPU busy" instead of a bare "deferred".
              (array_agg(extra->>'reason' ORDER BY started_at DESC)
                  FILTER (WHERE outcome = 'deferred' AND extra ? 'reason'))[1] AS last_deferred_reason
            FROM ranked GROUP BY cycle_type
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(query, CONSOLIDATION_ORPHAN_TIMEOUT_SEC)
        by_type = {r["cycle_type"]: r for r in rows}
        try:
            nrem = await self._nrem_cycle_counts()
        except Exception:
            nrem = {}
        backlog = {"insight": nrem.get("decision_cycles", 0) or 0,
                   "fact_consolidation": nrem.get("fact_cycles", 0) or 0}

        out: dict = {"stall_threshold_seconds": CONSOLIDATION_STALL_THRESHOLD_SEC}
        any_stalled = False
        for ct in ("insight", "fact_consolidation"):
            r = by_type.get(ct)
            age = r["last_success_age"] if r else None
            in_flight = bool(r["inflight"]) if r else False
            elig = r["eligible_clusters"] if r else None
            # Backlog must match the gate the cycle ACTUALLY folds on (see
            # _consolidation_backlog): the recorded eligible_clusters, not nrem.
            backlog_count = _consolidation_backlog(elig, backlog.get(ct, 0))
            has_backlog = backlog_count > 0
            stalled = _consolidation_stall_verdict(
                age, in_flight, has_backlog, CONSOLIDATION_STALL_THRESHOLD_SEC)
            any_stalled = any_stalled or stalled
            err = None
            if r and r["last_error_class"]:
                err = {"class": r["last_error_class"], "msg": r["last_error_msg"]}
            out[ct] = {
                "last_outcome": r["last_outcome"] if r else None,
                "last_success_age_seconds": age,
                "in_flight": in_flight,
                "consecutive_failures": int(r["consec_fail"]) if r else 0,
                "backlog": backlog_count,
                "stalled": stalled,
                "last_error": err,
                # Coverage census (PR-2): latest gate snapshot the daemon recorded.
                "eligible_clusters": elig,
                "eligible_oldest_age_seconds": (r["eligible_oldest_age"] if r else None),
                # Why the most-recent deferral happened (None if never deferred);
                # only meaningful when last_outcome == "deferred".
                "last_deferred_reason": (r["last_deferred_reason"] if r else None),
            }
        # Top-level signal keys mirror the insight cycle (the fragile one the
        # signal exists for); stalled is OR across cycle types.
        ins = out["insight"]
        out["stalled"] = any_stalled
        out["last_outcome"] = ins["last_outcome"]
        out["last_success_age_seconds"] = ins["last_success_age_seconds"]
        out["last_deferred_reason"] = ins["last_deferred_reason"]
        return out

    async def _consolidation_telemetry(self) -> dict:
        """Full consolidation section for /memory/telemetry (computed fresh)."""
        return await self._compute_consolidation_health()

    def consolidation_health(self) -> dict:
        """Cached compact snapshot for /health (DB-free, refreshed in background).
        Returns {stalled, last_outcome, last_success_age_seconds, inference_busy,
        fresh}. inference_busy is tri-state ("busy"|"idle"|"unknown")."""
        return dict(self._consolidation_health)

    async def _consolidation_health_refresher(self) -> None:
        """Background loop: recompute the cached /health snapshot every
        CONSOLIDATION_HEALTH_REFRESH_SEC so /health never touches the DB."""
        while True:
            try:
                full = await self._compute_consolidation_health()
                # Probe the GPU here (background, ~CONSOLIDATION_HEALTH_REFRESH_SEC)
                # so /health reads a cached value and never shells out to nvtop per
                # request. Tri-state: "unknown" when nvtop is absent — surfaced as-is
                # so the monitor never shows a false "idle".
                inference_busy = await inference_busy_state()
                self._consolidation_health = {
                    "stalled": full["stalled"],
                    "last_outcome": full["last_outcome"],
                    "last_success_age_seconds": full["last_success_age_seconds"],
                    "inference_busy": inference_busy,
                    "fresh": True,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never let the snapshot assert a stall on a compute failure —
                # mark it stale so a reader can tell it is not current. Keep the
                # prior inference_busy rather than inventing "idle" on failure.
                log.warning("consolidation health refresh failed: %s", exc)
                self._consolidation_health = {**self._consolidation_health, "fresh": False}
            try:
                await asyncio.sleep(CONSOLIDATION_HEALTH_REFRESH_SEC)
            except asyncio.CancelledError:
                raise

    async def _nrem_cycle_counts(self) -> dict:
        """Pending NREM consolidation cycles for facts and decisions.

        Reproduces consolidation_loop's gating: entity clusters of
        rem_processed, unconsolidated nodes, re-partitioned per (entity, domain)
        and counted only where a bucket meets its density threshold.
        """
        # Neo4j: clusters of eligible facts (global, not just pending ids).
        # ADR-017: grouped by ALIAS COMPONENT, identical to the gate
        # consolidation_loop._find_anchored_clusters actually folds on — so this
        # census (which drives the ADR-018 stall/coverage signal) never disagrees
        # with what NREM does. No-op-safe: null alias_component → per-entity, as before.
        rels = f"{ONT.entity_link_alias}|{ONT.entity_link}"
        async with self._neo4j.session() as session:
            fres = await session.run(
                f"MATCH (f:{ONT.fact}) WHERE f.pg_id IS NOT NULL"
                f" MATCH (f)-[:{rels}]->(e0:{ONT.entity})"
                f" WITH DISTINCT e0"
                f" CALL (e0) {{"
                f"   OPTIONAL MATCH (sib:{ONT.entity})"
                f"     WHERE e0.alias_component IS NOT NULL"
                f"       AND sib.alias_component = e0.alias_component"
                f"   WITH e0, collect(sib) AS sibs"
                f"   RETURN CASE WHEN e0.alias_component IS NULL"
                f"               THEN [e0] ELSE sibs END AS members"
                f" }}"
                f" WITH coalesce(e0.alias_component, elementId(e0)) AS comp, members"
                f" WITH comp, head(collect(members)) AS members"
                f" UNWIND members AS m"
                f" MATCH (m)<-[:{rels}]-(n:{ONT.fact})"
                f" WHERE coalesce(n.consolidated,false) = false"
                f"   AND coalesce(n.rem_processed,false) = true"
                f"   AND coalesce(n.superseded,false) = false"
                f" WITH comp, members, collect(DISTINCT n.pg_id) AS pg_ids"
                f" WHERE size(pg_ids) >= $threshold"
                f" RETURN reduce(c = null, nm IN [x IN members | x.name] |"
                f"          CASE WHEN c IS NULL OR nm < c THEN nm ELSE c END) AS entity,"
                f"        pg_ids",
                threshold=ONT.density_threshold,
            )
            fact_clusters = await fres.data()
            dres = await session.run(
                f"MATCH (d:{ONT.decision})"
                f" WHERE coalesce(d.rem_processed,false) = true"
                f"   AND coalesce(d.consolidated,false) = false"
                f"   AND coalesce(d.superseded,false) = false"
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
            async with self._acquire() as conn:
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
        async with self._acquire() as conn:
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

    # Anticipated ADR-017 alias edge type. Kept as a literal (a valid Cypher
    # identifier) until ADR-017 lands and formalises it in ontology.yaml; MATCHing
    # a relationship type that doesn't exist yet returns 0, not an error — so this
    # honestly reads 0 alias edges today rather than failing.
    _ALIAS_REL = "ALIASES"

    async def _entity_graph(self) -> dict:
        """Entity-graph shape metrics for the ADR-017 alias work — all cheap Neo4j
        aggregates, no embeddings, no pairwise scan:

          entities_total   — distinct Entity nodes
          orphan_entities  — mentioned by no non-superseded fact/decision (dead refs)
          singleton_entities — mentioned by exactly one (fragmentation/noise proxy)
          alias_edges / alias_covered_entities — populate once ADR-017 ships alias
              edges; 0 until then (an honest gap, the metric to watch climb)
          top_hubs         — highest-degree entities, the consolidation backbone

        The over-merge RISK behind these (which singletons are really the same
        concept) is the offline harness's job; this just sizes the problem live.
        """
        async with self._neo4j.session() as session:
            deg = await (await session.run(
                f"MATCH (e:{ONT.entity}) "
                f"OPTIONAL MATCH (n)-[:{ONT.entity_link}]->(e) "
                f"  WHERE n.pg_id IS NOT NULL AND coalesce(n.superseded,false) = false "
                f"WITH e, count(n) AS deg "
                f"RETURN count(e) AS total, "
                f"  sum(CASE WHEN deg = 0 THEN 1 ELSE 0 END) AS orphans, "
                f"  sum(CASE WHEN deg = 1 THEN 1 ELSE 0 END) AS singletons"
            )).single()
            aliases = await (await session.run(
                f"MATCH ()-[r:{self._ALIAS_REL}]-() RETURN count(DISTINCT r) AS edges"
            )).single()
            covered = await (await session.run(
                f"MATCH (e:{ONT.entity})-[:{self._ALIAS_REL}]-() RETURN count(DISTINCT e) AS c"
            )).single()
            hubs = await (await session.run(
                f"MATCH (e:{ONT.entity})<-[:{ONT.entity_link}]-(n) "
                f"  WHERE n.pg_id IS NOT NULL AND coalesce(n.superseded,false) = false "
                f"RETURN e.name AS name, count(n) AS degree "
                f"ORDER BY degree DESC LIMIT 8"
            )).data()
        return {
            "entities_total": deg["total"] or 0,
            "orphan_entities": deg["orphans"] or 0,
            "singleton_entities": deg["singletons"] or 0,
            "alias_edges": aliases["edges"] or 0,
            "alias_covered_entities": covered["c"] or 0,
            "top_hubs": [{"name": h["name"], "degree": h["degree"]} for h in hubs],
        }


    # ── POST /admin/backup (quiesce / resume) ─────────────────────────────────

    async def handle_backup(self, request: web.Request) -> web.Response:
        """Admin control for the backup quiesce. Body: {"state","max_seconds"}.

        state=quiesce → shed client writes immediately and acquire the EXCLUSIVE
        backup advisory lock, which blocks only until in-flight daemon cycles
        release their shared lock — so a 200 means "all writers drained, safe to
        dump". A daemon cycle that outlasts BACKUP_DAEMON_DRAIN_TIMEOUT yields 202
        (drain_timeout): client writes are still shed, but a daemon may write
        during the dump, so the caller decides whether to proceed.
        state=resume → release the lock, clear the flag, cancel the TTL.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        state = (body.get("state") or "").strip().lower()
        if state == "quiesce":
            try:
                max_s = float(body.get("max_seconds") or BACKUP_QUIESCE_MAX_SECONDS)
            except (TypeError, ValueError):
                max_s = BACKUP_QUIESCE_MAX_SECONDS
            drained = await self._begin_quiesce(max_s)
            return web.json_response(
                {
                    "status": "success",
                    "quiesced": True,
                    "daemons": "drained" if drained else "drain_timeout",
                    "ttl_seconds": max_s,
                },
                status=200 if drained else 202,
            )
        if state == "resume":
            await self._end_quiesce()
            return web.json_response({"status": "success", "quiesced": False})
        return web.json_response(
            {"status": "error", "message": "state must be 'quiesce' or 'resume'"},
            status=400,
        )

    async def _begin_quiesce(self, max_seconds: float) -> bool:
        """Shed client writes now; acquire the exclusive backup advisory lock to
        fence the REM/NREM daemons. Returns True once the lock is held (daemons
        drained), False if an in-flight cycle outlasts the drain timeout. Idempotent:
        a second call while quiesced only refreshes the TTL.
        """
        global _backup_quiesce
        _backup_quiesce = True
        # (Re)arm the TTL auto-resume so a dead backup script can't wedge writes.
        if self._quiesce_timer and not self._quiesce_timer.done():
            self._quiesce_timer.cancel()
        self._quiesce_timer = asyncio.create_task(self._quiesce_ttl(max_seconds))
        # Already holding the exclusive lock → nothing more to do (TTL refreshed).
        if self._quiesce_conn is not None:
            return True
        # Dedicated connection (outside the pool) so the lock auto-releases if this
        # process dies — Postgres drops session advisory locks on disconnect.
        try:
            conn = await asyncpg.connect(PG_DSN)
        except Exception as exc:
            log.warning("backup quiesce: could not open advisory-lock connection: %s", exc)
            return False
        try:
            await conn.execute(
                "SET lock_timeout = '%dms'" % int(BACKUP_DAEMON_DRAIN_TIMEOUT * 1000)
            )
            await conn.execute("SELECT pg_advisory_lock($1)", BACKUP_ADVISORY_LOCK_KEY)
        except Exception as exc:
            # Daemons did not drain in time (lock_timeout). Leave client writes shed
            # but report drain_timeout; drop the connection so no half-held lock lingers.
            log.warning(
                "backup quiesce: daemons did not drain within %.0fs (%s)",
                BACKUP_DAEMON_DRAIN_TIMEOUT, exc,
            )
            try:
                await conn.close()
            except Exception:
                pass
            return False
        self._quiesce_conn = conn
        log.info(
            "backup quiesce: client writes shed + daemons fenced "
            "(exclusive advisory lock %d held)", BACKUP_ADVISORY_LOCK_KEY,
        )
        return True

    async def _end_quiesce(self) -> None:
        """Release the advisory lock, clear the flag, cancel the TTL. Safe to call
        when not quiesced (idempotent).
        """
        global _backup_quiesce
        _backup_quiesce = False
        if self._quiesce_timer and not self._quiesce_timer.done():
            self._quiesce_timer.cancel()
        self._quiesce_timer = None
        if self._quiesce_conn is not None:
            try:
                await self._quiesce_conn.execute(
                    "SELECT pg_advisory_unlock($1)", BACKUP_ADVISORY_LOCK_KEY
                )
            except Exception:
                pass
            try:
                await self._quiesce_conn.close()
            except Exception:
                pass
            self._quiesce_conn = None
            log.info("backup quiesce: released — writes and daemons resumed")

    async def _quiesce_ttl(self, max_seconds: float) -> None:
        """Auto-resume backstop: if no resume arrives within max_seconds, release."""
        try:
            await asyncio.sleep(max_seconds)
        except asyncio.CancelledError:
            return
        log.warning(
            "backup quiesce: TTL of %.0fs expired without resume — auto-resuming",
            max_seconds,
        )
        # Detach self first so _end_quiesce won't cancel this running task mid-cleanup.
        self._quiesce_timer = None
        await self._end_quiesce()


# ── Registration ──────────────────────────────────────────────────────────────

def attach(app: web.Application, coordinator: MemoryCoordinator) -> None:
    """Register /memory/* routes on an aiohttp Application.

    Must be called before the proxy catch-all route so these exact-path routes
    take precedence. To extract the coordinator into a standalone process
    (Phase 4), only this call site changes.
    """
    app.router.add_post("/memory/save",           coordinator.handle_save)
    app.router.add_post("/memory/retrospective",  coordinator.handle_retrospective)
    app.router.add_post("/memory/supersede",      coordinator.handle_supersede)
    app.router.add_post("/memory/review_hold",     coordinator.handle_review_hold)
    app.router.add_post("/memory/search",         coordinator.handle_search)
    app.router.add_post("/memory/graph",          coordinator.handle_graph)
    app.router.add_get( "/memory/status/{pg_id}", coordinator.handle_status)
    app.router.add_get( "/memory/telemetry",       coordinator.handle_telemetry)
    app.router.add_post("/admin/backup",           coordinator.handle_backup)
