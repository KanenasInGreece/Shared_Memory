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
from ontology import (
    ONT, sanitize_entity_names, sanitize_entity_name,
    KNOWN_LABELS, KNOWN_RELATIONSHIPS, fact_kind_from_source_ref,
    GROUNDING_ROLES, default_grounding_role, RETRO_RATINGS,
    record_label_for_type,
)

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
FRAMEWORK_VERSION = "0.8.16"
# v2 (retro-as-record): /memory/retrospective now creates a full record (own
# pg_id, embedding, Retrospective node) and accepts rating enum + grounding —
# the response shape changed (returns the retro's own pg_id).
# v3 (relation calibration): new operator-facing routes /memory/relations/review
# and /memory/relations/label (the review-edges / label-edges client commands
# require them) — the operator is the calibration oracle for machine-minted
# relation edges (decisions 726/727).
API_VERSION = 3
CLIENT_VERSION_HEADER = "X-SM-Api-Version"

# ── Record references: a record id is only unique WITHIN ITS TABLE ────────────
# `technical_docs` and `community_summaries` run INDEPENDENT id sequences, so the
# same integer names two unrelated real records. Inside this process that has
# always been safe by accident — every content path happens to be label-scoped or
# table-scoped — but a BARE integer crossing the API boundary is genuinely
# ambiguous: search returns the id under the same field name for both namespaces,
# so an id lifted off a summary result and handed back to a lookup used to resolve
# against technical_docs and return a confident, unrelated record.
#
# The fix is to make the record TYPE explicit on every reference (`fact:816`,
# `summary:87`) rather than to renumber both tables onto one global sequence — an
# irreversible migration to close something this closes additively. A bare integer
# is still accepted, and still means technical_docs, for compatibility.
#
# Shared with consolidation_loop.py (decision 882 reuses this exact scheme to key
# the NREM fold dead-letter ledger) — see record_ref.py, the single source of truth.
from record_ref import (             # noqa: E402
    REF_TYPES_DOCS, REF_TYPES_SUMMARIES, REF_SEPARATOR,
    make_ref, parse_ref, summary_record_type, doc_record_type,
)

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

# /pool/status is read-only, DB-free in-memory LLM capacity — the REM/NREM daemons
# poll it (tokenless) to gate dreaming, same trust level as /health.
_UNPROTECTED_PATHS = {"/health", "/pool/status"}


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
    # Operator labeling/promotion mutates the adjudication ledger + Neo4j edges,
    # so it sheds during a backup quiesce like every other client write.
    # /memory/relations/review is a pure read and deliberately absent here —
    # but neither route is in _READ_ROLE_ROUTES, so a read-only token (monitor)
    # gets 403 on both: labeling is operator-grade.
    ("POST", "/memory/relations/label"),
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


# Mirrors aiohttp's own DynamicResource pattern for a `{name}` path segment
# (`web_urldispatcher.DynamicResource.GOOD = "[^{}/]+"`) so this check only ever
# grants what the registered `/memory/status/{pg_id}` route can actually match.
_MEMORY_STATUS_RE = re.compile(r"/memory/status/[^{}/]+")


def _read_role_permits(request: web.Request) -> bool:
    """True if a read-only role may reach this route (method+path allowlist)."""
    path = request.path.rstrip("/") or "/"
    if (request.method, path) in _READ_ROLE_ROUTES:
        return True
    # Per-record lineage — GET /memory/status/{pg_id} — is read-only (no mutation),
    # so a read-role client (e.g. the Monitor drilling into a record) may reach it.
    # It carries a path param, so it is matched by a fullmatch regex, NOT a prefix
    # check — a bare `startswith("/memory/status/")` let a crafted extra-segment
    # path (e.g. "/memory/status/1/x") pass this gate while aiohttp's own single-
    # segment {pg_id} pattern does NOT match it, so the request actually falls
    # through to the catch-all proxy passthrough underneath a "granted" verdict —
    # a read-role token reaching the LLM/embeddings proxy it's meant to be denied.
    return request.method == "GET" and bool(_MEMORY_STATUS_RE.fullmatch(path))


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
# Read-only mirror of rem_loop.REM_MAX_ATTEMPTS — the gateway never enforces the
# cap, it only needs the threshold to report how many records REM has given up
# on. MUST match the daemon's default.
REM_MAX_ATTEMPTS            = _env_int("REM_MAX_ATTEMPTS", 5)
# Read-only mirror of rem_loop.REM_STARVED_THRESHOLD (decision 890, STEP 3) —
# the gateway never runs the starved-drain itself, it only needs the threshold
# to report how many pending records are AT the promotion point. MUST match
# the daemon's default.
REM_STARVED_THRESHOLD        = _env_int("REM_STARVED_THRESHOLD", 3)
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
# BGE-M3 runs with -c 8192 (context tokens). An input over that fails to embed —
# which would abort a save or drop a community summary from search. Truncate the
# EMBEDDING input to a conservative char budget (~<=8000 tokens at ~3 chars/token,
# safely under 8192); the FULL text is always kept in Tier 1 (technical_docs), so
# search still returns it — only the vector is computed from the prefix. Prompted
# by an advisor hitting a smaller BGE-M3 limit; guards us as summaries grow with
# the larger-context work. Env-tunable if the embedder's -c changes.
EMBED_MAX_CHARS = int(os.environ.get("EMBED_MAX_CHARS", "24000"))

# Read-contract graph expansion cap: how many edges surface per anchored record
# in search results. Env-tunable. Ordering (in the expansion Cypher) puts
# provenance-bearing edges (r.asserted_by set) and typed relations ahead of bare
# MENTIONS, so the highest-signal context survives the cap — context without
# relation properties is noise disguised as fact.
GRAPH_EXPANSION_LIMIT = _env_int("GRAPH_EXPANSION_LIMIT", 15)

# ── Relation-adjudication review/label surface (migration 020; decisions 726/727)
# These constants MUST mirror relation_confidence.py (the psycopg2 foundation the
# sweep/REM daemons use). The coordinator reimplements the ledger queries
# asyncpg-natively instead of importing that module: the gateway venv ships
# WITHOUT psycopg2 (uv run --with asyncpg …), so a module-level import of
# relation_confidence would break gateway startup. Same env knobs → same values.
RELATION_FAMILIES: tuple[str, ...] = ("entity_relation", "evidential")
# asserted_by values a machine minted — the ONLY values the guarded edge delete
# may remove; an operator-asserted edge is never deleted by a label.
RELATION_MACHINE_ASSERTED: tuple[str, ...] = ("rem", "rem_sweep")
RELCONF_CONSUME_THRESHOLD: dict[str, float] = {
    "entity_relation": _env_float("RELCONF_CONSUME_ENTITY", 0.60),
    "evidential":      _env_float("RELCONF_CONSUME_EVIDENTIAL", 0.70),
}
RELCONF_MIN_LABELS = _env_int("RELCONF_MIN_LABELS", 20)
RELATION_REVIEW_LIMIT_DEFAULT = 20
RELATION_REVIEW_LIMIT_CAP    = 100
RELATION_SNIPPET_CHARS       = 160   # evidential rows: LEFT(content, N) per endpoint

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


# Every consolidation cycle type, in report order. One tuple so the per-type
# roll-up and the per-type report can never drift apart.
CONSOLIDATION_CYCLE_TYPES = ("insight", "fact_consolidation")


def _consolidation_rollup(by_type: dict, any_stalled: bool, started_at: dict,
                          cycle_types=CONSOLIDATION_CYCLE_TYPES) -> dict:
    """Top-level consolidation keys derived from EVERY cycle type.

    These keys used to be mirrored from the insight cycle alone. That made a
    healthy cycle unreportable: fact consolidation folded 17 clusters in 24h
    while the headline read "stalled, last success 5.3 days ago" — which was
    insight's age, for a cycle type the reader was not asking about. A headline
    that names one type while claiming to describe consolidation is not a
    summary, it is a wrong answer.

    So: `last_success_age_seconds` is now the MOST RECENT success across types
    (the honest answer to "when did consolidation last succeed"), tagged with
    the type that achieved it, and `last_outcome`/`last_deferred_reason` come
    from whichever type ran most recently rather than a hardcoded one.
    `stalled` stays an OR — a stalled sibling must still raise the flag — but
    `stalled_types` now names who, so the flag is actionable. Pure → testable.
    """
    ages = [(by_type[ct]["last_success_age_seconds"], ct)
            for ct in cycle_types
            if isinstance(by_type.get(ct), dict)
            and by_type[ct]["last_success_age_seconds"] is not None]
    freshest = min(ages) if ages else (None, None)

    # "Most recent activity" orders on the RAW started_at datetimes, never on
    # their ISO strings: string ordering is only correct while every value
    # carries the same UTC offset, which is true of one timestamptz column
    # today and silently wrong the day it is not. A type that never ran has no
    # timestamp and must not win by sorting as empty.
    started = [(started_at[ct], ct)
               for ct in cycle_types
               if started_at.get(ct) is not None]
    latest_ct = max(started)[1] if started else None
    latest = by_type.get(latest_ct) if latest_ct else None

    return {
        "stalled": any_stalled,
        "stalled_types": [ct for ct in cycle_types
                          if isinstance(by_type.get(ct), dict)
                          and by_type[ct]["stalled"]],
        "last_success_age_seconds": freshest[0],
        "last_success_cycle_type": freshest[1],
        "last_outcome": latest["last_outcome"] if latest else None,
        "last_deferred_reason": latest["last_deferred_reason"] if latest else None,
        "last_active_cycle_type": latest_ct,
    }

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


def _rerank_doc_text(content: str, metadata: dict | None, created_at) -> str:
    """The text the reranker scores. For decisions and retrospectives the
    recording date is prepended so recency is VISIBLE to relevance scoring —
    a decision's latest retrospective is its current verdict, and the reranker
    cannot weigh what it cannot see. Facts are passed through untouched (their
    truth is not time-ordered the way outcome records are). Pure."""
    t = (metadata or {}).get("type") if isinstance(metadata, dict) else None
    if t in ("decision", "retrospective") and created_at is not None:
        try:
            day = created_at.date().isoformat()
        except AttributeError:
            day = str(created_at)[:10]
        return f"[{t} recorded {day}] {content}"
    return content


def _order_retros_latest_first(results: list[dict]) -> list[dict]:
    """Within one result set, when SEVERAL retrospectives of the SAME decision
    surface, present them newest-first in the positions they already occupy —
    the newest retro is the decision's current verdict; everything else keeps
    the reranker's order. Deterministic, no scoring change. Pure."""
    groups: dict[int, list[int]] = {}
    for i, r in enumerate(results):
        meta = r.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("type") == "retrospective":
            tgt = meta.get("target_pg_id")
            if isinstance(tgt, int):
                groups.setdefault(tgt, []).append(i)
    out = list(results)
    for positions in groups.values():
        if len(positions) < 2:
            continue
        entries = sorted((out[i] for i in positions),
                         key=lambda e: str(e.get("created_at") or ""), reverse=True)
        for pos, entry in zip(positions, entries):
            out[pos] = entry
    return out


def _visibility_filter(viewer: str | None, viewer_scope: str | None,
                       start: int) -> tuple[str, list]:
    """Build the read-authorization predicate for the `visibility` column.

    A row is visible when its ``visibility`` is:
      - ``'global'``  → to everyone;
      - ``'private'`` → only to the owning ``agent_id`` (the viewer);
      - ``'scope'``   → only when the viewer asserts the matching ``scope``.

    The viewer is the server-verified ``authenticated_agent`` (spoof-proof); an
    anonymous caller (no verified identity) sees only ``'global'`` — fail closed.
    A caller that asserts no scope cannot match ``'scope'`` rows. Returns the SQL
    fragment and its parameters; ``start`` is the next free asyncpg positional
    index (``$N``). Every read in ``handle_search`` composes this, so a private
    fact is filtered from Tier-1 AND its Tier-3 synthesis never leaks (the
    community summary inherits the source cluster's scope/visibility).
    """
    if not viewer:
        return "visibility = 'global'", []
    clauses = ["visibility = 'global'",
               f"(visibility = 'private' AND agent_id = ${start})"]
    params: list = [viewer]
    if viewer_scope:
        clauses.append(f"(visibility = 'scope' AND scope = ${start + 1})")
        params.append(viewer_scope)
    return "(" + " OR ".join(clauses) + ")", params


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


def _json_safe(value):
    """Coerce a Neo4j-driver value into JSON-serialisable primitives.

    Edge/node property maps can carry temporal values (neo4j DateTime/Date/Time,
    or plain datetime) — json.dumps raises TypeError on those. Primitives pass
    through; lists/dicts recurse; temporals become ISO strings (neo4j exposes
    ``iso_format()``, stdlib ``isoformat()``); anything else degrades to str().
    Pure and defensive — surfacing edge properties must never fail a search.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    for attr in ("iso_format", "isoformat"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                break
    return str(value)


def _neighbor_adr_props(rec) -> dict:
    """Collect the ADR node properties a graph-expansion row projects for a
    pg_id-keyed neighbor (Decision: confidence/alternatives; Fact: fact_kind/
    source_ref), returning only the keys that are set.

    These already sit on the one-hop neighbor node — projecting them lets a
    summary hit carry a folded decision's confidence/alternatives and a folded
    fact's evidence weight WITHOUT a second query (decision 909). A neighbor
    that carries none returns ``{}`` (no ``adr_props`` key is added). Missing
    projection columns (older single-anchor callers, test stubs) are tolerated
    via ``.get``. Pure — never raises, so it can never fail a search.
    """
    def _g(key):
        try:
            return rec[key]
        except (KeyError, TypeError, IndexError):
            return None
    adr: dict = {}
    if _g("adr_confidence"):
        adr["confidence"] = _g("adr_confidence")
    alts = _g("adr_alternatives")
    if alts:
        adr["alternatives"] = _json_safe(list(alts))
    if _g("adr_fact_kind"):
        adr["fact_kind"] = _g("adr_fact_kind")
    if _g("adr_source_ref"):
        adr["source_ref"] = _g("adr_source_ref")
    return adr


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
                                             "last_success_cycle_type": None,
                                             "stalled_types": [],
                                             # None (not 0) until the first refresh:
                                             # "not yet probed" must never read as
                                             # "verified clean" (decision 928).
                                             "graph_invalid_nodes": None,
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
        if len(text) > EMBED_MAX_CHARS:
            log.warning("embed input %d chars > %d — truncating to fit BGE-M3 8192-ctx "
                        "(full text kept in Tier 1)", len(text), EMBED_MAX_CHARS)
            text = text[:EMBED_MAX_CHARS]
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

    @staticmethod
    def _gate_graph_entities(pg_id: int, raw: object) -> list[str]:
        """Outbox→graph entity-name gate (Phase 1 inbound hygiene).

        Sanitises names at the point they are projected from the Postgres outbox
        into Neo4j — keeping Tier-1 (the stored fact) pristine while ensuring only
        meaningful names become graph hubs. Rejected names are logged as a quality
        signal (leaked pg-ids, booleans, placeholders, schema vocabulary).
        """
        clean = sanitize_entity_names(raw)
        if isinstance(raw, (list, tuple)):
            dropped = [r for r in raw
                       if isinstance(r, str) and sanitize_entity_name(r) is None]
            if dropped:
                log.info("outbox->graph gate rejected %d name(s) for pg_id=%s: %s",
                         len(dropped), pg_id, dropped)
        return clean

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
            fact_kind = params.get("fact_kind") or "observation"
            async with self._neo4j.session() as session:
                await session.run(
                    f"MERGE (f:{ONT.fact} {{pg_id: $pg_id}})"
                    f" SET f.content = $content, f.source = $source,"
                    f"     f.fact_kind = $fact_kind"
                    + (" SET f.source_ref = $source_ref" if source_ref else "")
                    + f" WITH f"
                    # Fact-custody edges (decision 915): the agent GENERATED this
                    # record (WAS_ATTRIBUTED_TO), acting ON BEHALF OF the operator
                    # (ACTED_ON_BEHALF_OF — delegation, NOT authorship: this is why we
                    # do not point WAS_ATTRIBUTED_TO at the human as decisions do),
                    # scoped to a project. All three are DERIVED (agent = token source,
                    # person = kernel principal, project = folder) and written only when
                    # present. The 'coordinator' fallback source is the system itself,
                    # not a real agent, so it mints no AIAgent node.
                    f" FOREACH (_ IN CASE WHEN $source <> '' AND $source <> 'coordinator'"
                    f"                    THEN [1] ELSE [] END |"
                    f"   MERGE (a:{ONT.ai_agent} {{name: $source}})"
                    f"   MERGE (f)-[:{ONT.was_attributed_to}]->(a))"
                    f" FOREACH (_ IN CASE WHEN $source <> '' AND $source <> 'coordinator'"
                    f"                    AND $person <> '' THEN [1] ELSE [] END |"
                    f"   MERGE (a:{ONT.ai_agent} {{name: $source}})"
                    f"   MERGE (h:{ONT.human} {{name: $person}})"
                    f"   MERGE (a)-[:{ONT.acted_on_behalf_of}]->(h))"
                    f" FOREACH (_ IN CASE WHEN $project <> '' THEN [1] ELSE [] END |"
                    f"   MERGE (p:{ONT.project} {{name: $project}})"
                    f"   MERGE (f)-[:{ONT.project_of}]->(p))"
                    + f" WITH f"
                    f" UNWIND $entities AS ename"
                    f" MERGE (e:{ONT.entity} {{name: ename}})"
                    f" MERGE (f)-[:{ONT.entity_link}]->(e)",
                    pg_id=pg_id,
                    content=params.get("content_snippet", "")[:200],
                    source=params.get("source", "coordinator"),
                    person=params.get("person") or "",
                    project=params.get("project") or "",
                    fact_kind=fact_kind,
                    entities=self._gate_graph_entities(pg_id, params.get("entities", [])),
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

    async def _resolve_typed_grounding(
        self, conn, grounded_ids: list, grounded_roles: dict
    ) -> list:
        """Resolve grounded pg_ids to typed edges (decision 582, OPTION A). For each
        target, look up its node label (Fact / Decision / Retrospective) and
        fact_kind from technical_docs, then choose the ROLE: an explicit operator
        role (asserted_by=operator) or the fact_kind default
        (asserted_by=system_default).
        Advisory — no silent rewrite; an operator role always wins. Returns
        [{pg_id, rel, asserted_by, label}] for the cross-type apoc writer.

        The label comes from `record_label_for_type` — exhaustive over the spine
        record types on purpose. A binary Decision-else-Fact conditional here is
        what made a Retrospective target mint a hollow :Fact stub while the real
        :Retrospective stayed unlinked (bug 578's shape, repeated); grounding a
        successor decision on the retrospective that drove it is a first-class
        lineage, so its target label must resolve correctly."""
        if not grounded_ids:
            return []
        rows = await conn.fetch(
            "SELECT id, metadata->>'type' AS type, metadata->>'source_ref' AS source_ref"
            " FROM technical_docs WHERE id = ANY($1)",
            grounded_ids,
        )
        meta = {r["id"]: r for r in rows}
        out: list[dict] = []
        for pid in grounded_ids:
            r = meta.get(pid)
            label = record_label_for_type(r["type"] if r else None)
            requested = (grounded_roles.get(str(pid)) or "").strip().lower()
            if requested in GROUNDING_ROLES:
                rel, asserted_by = GROUNDING_ROLES[requested], "operator"
            else:
                fk = fact_kind_from_source_ref(r["source_ref"] if r else None)
                rel, asserted_by = default_grounding_role(fk), "system_default"
            out.append({"pg_id": pid, "rel": rel,
                        "asserted_by": asserted_by, "label": label})
        return out

    @staticmethod
    async def _write_typed_grounding(
        session, anchor_label: str, pg_id: int, grounded: list
    ) -> None:
        """Write the typed grounding ROLE edges (decision 582) from an anchor
        record (Decision or Retrospective) to its REAL targets across labels
        (Fact OR Decision — no shadow-Fact stub, bug 578). apoc supplies the
        dynamic label + relation type; every edge records asserted_by
        (operator | system_default). Shared by the decision and retrospective
        projections so the two writers can never drift."""
        if not grounded:
            return
        await session.run(
            f"MATCH (a:{anchor_label} {{pg_id: $pg_id}})"
            f" UNWIND $grounded AS g"
            f" CALL apoc.merge.node([g.label], {{pg_id: g.pg_id}}) YIELD node AS gf"
            f" CALL apoc.merge.relationship(a, g.rel, {{}},"
            f"      {{asserted_by: g.asserted_by}}, gf, {{asserted_by: g.asserted_by}}) YIELD rel"
            f" RETURN count(*) AS n",
            pg_id=pg_id, grounded=grounded,
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
        grounded = params.get("grounded") or []
        grounded_in_flat = params.get("grounded_in", [])
        async with self._neo4j.session() as session:
            await session.run(
                f"MERGE (d:{ONT.decision} {{pg_id: $pg_id}})"
                f"  SET d.title       = $title,"
                f"      d.rationale   = $rationale,"
                f"      d.date        = $date,"
                f"      d.source      = $source,"
                # confidence + alternatives are SPINE ADR fields, materialised
                # deterministically as PROPERTIES (not entity nodes): the alias-
                # pressure measurement (fact 551) showed alternatives are 65% free
                # phrases, so minting them as :Entity would flood the graph — REM
                # still extracts clean CONSIDERED entities from the text.
                f"      d.confidence  = $confidence,"
                f"      d.alternatives = $alternatives"
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
                confidence=decision.get("confidence") or "",
                alternatives=[a for a in (decision.get("alternatives") or []) if isinstance(a, str)],
                decided_by=decision.get("decided_by", "unknown"),
                project=decision.get("project", "unknown"),
                assisted_by=decision.get("assisted_by", []),
                entities=self._gate_graph_entities(pg_id, params.get("entities", [])),
            )
            # Typed decision→fact grounding (decision 582): shared writer — see
            # _write_typed_grounding. Legacy flat GROUNDED_IN is the fallback for
            # outbox rows queued before this shipped (no 'grounded').
            if grounded:
                await self._write_typed_grounding(session, ONT.decision, pg_id, grounded)
            elif grounded_in_flat:
                # Legacy path — RESOLVE the cited id's real label before linking.
                # This used to MERGE the target as a :Fact unconditionally, which
                # manufactured a contentless phantom whenever the cited record was
                # a decision or a retrospective: the real node kept its own label,
                # so the stub was never filled, never enriched, and sat in the REM
                # queue forever (820). A pg_id does not imply a Fact — check what
                # the id actually refers to and attach to THAT node, creating the
                # placeholder only when no record node exists yet.
                await session.run(
                    f"MATCH (d:{ONT.decision} {{pg_id: $pg_id}})"
                    f" UNWIND $grounded_in AS fid"
                    f" OPTIONAL MATCH (g)"
                    f"   WHERE (g:{ONT.fact} OR g:{ONT.decision} OR g:{ONT.retrospective})"
                    f"     AND g.pg_id = fid"
                    f" WITH d, fid, collect(g)[0] AS existing"
                    f" FOREACH (_ IN CASE WHEN existing IS NULL THEN [1] ELSE [] END |"
                    f"   MERGE (gf:{ONT.fact} {{pg_id: fid}})"
                    f"   MERGE (d)-[:{ONT.grounded_in}]->(gf) )"
                    f" FOREACH (_ IN CASE WHEN existing IS NULL THEN [] ELSE [1] END |"
                    f"   MERGE (d)-[:{ONT.grounded_in}]->(existing) )",
                    pg_id=pg_id, grounded_in=grounded_in_flat,
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
        """Materialise a retrospective in Neo4j — two shapes during the
        retro-as-record transition:

        v2 (params['v'] == 2, retro-as-record): pg_id is the RETRO'S OWN id.
        MERGE a :Retrospective node (rating/date/content-snippet/source/
        fact_kind), link the target Decision via the HAD_OUTCOME trigger edge,
        MENTIONS edges for elicited entities (gated), and the typed grounding
        ROLE edges (shared writer). The target Decision is matched in its own
        statement so a missing decision leaves the record intact (edge no-op).

        Legacy (no 'v'): pg_id is the TARGET DECISION's id — a HAD_OUTCOME
        self-loop carrying rating/date/notes as edge properties. Kept until the
        pre-conversion outbox rows drain. MATCH only (no MERGE) so a missing
        Decision surfaces as a no-op rather than a phantom node.
        """
        retro = params.get("retrospective", {})
        # Reversal (decision 276): the cascade is decision-level only — the
        # graph node mirrors technical_docs.superseded so the insight gate's
        # fresh-cluster query can exclude reversed decisions cheaply. Insights
        # are never invalidated here; the re-fold supersedes them instead.
        reversal = bool(retro.get("superseded"))
        async with self._neo4j.session() as session:
            if params.get("v") == 2:
                target_pg_id = params.get("target_pg_id")
                source_ref = params.get("source_ref") or None
                await session.run(
                    f"MERGE (r:{ONT.retrospective} {{pg_id: $pg_id}})"
                    f" SET r.rating = $rating, r.date = $date,"
                    f"     r.content = $content, r.source = $source,"
                    f"     r.fact_kind = $fact_kind"
                    + (" SET r.source_ref = $source_ref" if source_ref else "")
                    + f" WITH r"
                    f" UNWIND $entities AS ename"
                    f" MERGE (e:{ONT.entity} {{name: ename}})"
                    f" MERGE (r)-[:{ONT.entity_link}]->(e)",
                    pg_id=pg_id,
                    rating=retro.get("rating", ""),
                    date=retro.get("date", ""),
                    content=params.get("content_snippet", "")[:200],
                    source=params.get("source", "coordinator"),
                    fact_kind=params.get("fact_kind") or "observation",
                    entities=self._gate_graph_entities(pg_id, params.get("entities", [])),
                    **({"source_ref": source_ref} if source_ref else {}),
                )
                # HAD_OUTCOME trigger edge from the target Decision (+ reversal
                # mirror) — separate statement: a missing decision is a no-op
                # for the edge but never loses the Retrospective record.
                await session.run(
                    f"MATCH (d:{ONT.decision} {{pg_id: $target}})"
                    f" MATCH (r:{ONT.retrospective} {{pg_id: $pg_id}})"
                    f" MERGE (d)-[:{ONT.had_outcome} {{date: $date}}]->(r)"
                    + (" SET d.superseded = true" if reversal else ""),
                    target=target_pg_id, pg_id=pg_id, date=retro.get("date", ""),
                )
                # Typed grounding ROLE edges (decision 582) — a retrospective
                # grounds in the evidence that measured the outcome
                # (test-grounded retrospectives, decision 542, now structural).
                await self._write_typed_grounding(
                    session, ONT.retrospective, pg_id, params.get("grounded") or []
                )
            else:
                superseded_clause = " SET d.superseded = true" if reversal else ""
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

        entities = metadata.get("entities", [])
        if not isinstance(entities, list):
            return web.json_response(
                {"status": "error", "message": "metadata.entities must be a list"},
                status=400,
            )
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

                    # Typed decision→fact grounding (decision 582): resolve each
                    # grounded pg_id's node label + fact_kind from the durable store
                    # and pick the ROLE relation (an operator role → asserted_by=
                    # operator; else the fact_kind default → system_default). Advisory,
                    # no silent rewrite. Drives the cross-type writer; the flat
                    # grounded_in list is kept for telemetry/back-compat.
                    grounded_ids = [
                        g for g in (metadata.get("grounded_in") or [])
                        if isinstance(g, int) and not isinstance(g, bool)
                    ]
                    grounded_typed = await self._resolve_typed_grounding(
                        conn, grounded_ids, metadata.get("grounded_roles") or {}
                    )

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
                            # Fact-provenance axes (decision 912) — materialised as
                            # traversable edges on the :Fact node so provenance is a
                            # type-bounded subgraph, not just Postgres metadata. All
                            # three are DERIVED, never elicited: person = kernel-attested
                            # principal (SO_PEERCRED user, "user from system", None on
                            # TCP), agent = the token-verified source ("agent from token"),
                            # project = the normalised folder name ("project from folder").
                            # Each edge is written only when its value is present.
                            "person": metadata.get("principal"),
                            "project": metadata.get("project"),
                            "type": metadata.get("type", "fact"),
                            "decision": metadata.get("decision", {}),
                            "source_ref": metadata.get("source_ref") or None,
                            # fact_kind: soft epistemic tag, DERIVED from source_ref
                            # (decision 553), stamped as a :Fact property at first
                            # write. observation | discussion | tested | measured |
                            # researched. Deterministic, never elicited separately.
                            "fact_kind": fact_kind_from_source_ref(
                                metadata.get("source_ref")
                            ),
                            # Fact-grounding (decision 550): pg_ids of the Fact(s) this
                            # record rests on. Deterministic 1-1 → GROUNDED_IN edges at
                            # first write (id match, no alias pressure). Facts-only
                            # source_ref stays the fact's own origin; grounded_in points
                            # a Decision/record at its evidence facts.
                            "grounded_in": grounded_ids,
                            # Typed roles + asserted_by for the cross-type writer
                            # (decision 582): [{pg_id, rel, asserted_by, label}].
                            "grounded": grounded_typed,
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
                succ = await conn.fetchrow(
                    "SELECT superseded FROM technical_docs WHERE id = $1", by
                )
                if succ is None:
                    return web.json_response(
                        {"status": "error", "message": f"successor {by} not found"},
                        status=400,
                    )
                # A stale multi-hop chain (A -> B -> C where B is itself already
                # superseded) leaves a consumer told "A is stale, see B" with no
                # signal that B is ALSO stale — mirrors the existing check on
                # pg_id above and the parallel check in handle_save's `supersedes`.
                if succ["superseded"]:
                    return web.json_response(
                        {"status": "error",
                         "message": f"successor {by} is itself already superseded"},
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
        """Retro-as-record (API v2): a retrospective is a FULL record — own
        pg_id + technical_docs row + embedding (searchable), materialised in
        Neo4j as a :Retrospective node behind the target Decision's HAD_OUTCOME
        trigger edge. The one machine-readable outcome field is the rating,
        validated against the outcome-state enum (RETRO_RATINGS); the notes
        carry the nuance. Optional grounding (grounded_in + roles) records the
        evidence that measured the outcome — the test-grounded-retrospectives
        rule (decision 542), now structural."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )

        pg_id    = body.get("pg_id")            # TARGET decision's id
        rating   = (body.get("rating") or "").strip().lower()
        notes    = body.get("notes", "")
        date     = body.get("date") or datetime.now().date().isoformat()
        # Verified token identity over the client's script-name default ("memory_bridge").
        agent_id = request.get("authenticated_agent") or body.get("agent_id", "unknown")

        if isinstance(pg_id, bool) or not isinstance(pg_id, int) or not rating or not notes:
            return web.json_response(
                {"status": "error", "message": "pg_id (int), rating, and notes are required"},
                status=400,
            )
        if rating not in RETRO_RATINGS:
            return web.json_response(
                {"status": "error",
                 "message": (f"rating must be one of {sorted(RETRO_RATINGS)} "
                             "(outcome states — nuance belongs in the notes)")},
                status=400,
            )

        # Reversal (decision 276): rating 'reversed' is the structural rating —
        # it marks the DECISION superseded in both stores (Tier-1 filter +
        # fresh-cluster exclusion).
        is_reversal = rating == "reversed"
        retro_payload = {"rating": rating, "date": date}

        # The retro's own record metadata. Person-axis enforcement as in
        # handle_save: principal is stamped from the kernel-attested identity.
        source_ref = body.get("source_ref") or None
        raw_entities = body.get("entities")
        if raw_entities is not None and not isinstance(raw_entities, list):
            return web.json_response(
                {"status": "error", "message": "entities must be a list"}, status=400
            )
        metadata = {
            "type": "retrospective",
            "source": agent_id,
            "target_pg_id": pg_id,
            "rating": rating,
            "date": date,
            "entities": [e for e in (raw_entities or [])
                         if isinstance(e, str) and e.strip()],
        }
        if source_ref:
            metadata["source_ref"] = source_ref
        if body.get("elicited"):
            metadata["elicited"] = True
        grounded_ids = [
            g for g in (body.get("grounded_in") or [])
            if isinstance(g, int) and not isinstance(g, bool)
        ]
        if grounded_ids:
            metadata["grounded_in"] = grounded_ids
            roles = body.get("grounded_roles") or {}
            if isinstance(roles, dict) and roles:
                metadata["grounded_roles"] = roles
        _apply_principal(metadata, request.get("principal"))

        # Cheap indexed existence pre-check BEFORE the GPU embedding — a typoed
        # pg_id must not occupy the embedder just to 404. The FOR SHARE re-check
        # inside the transaction below remains authoritative.
        async with self._acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM technical_docs WHERE id=$1", pg_id
            )
        if not exists:
            return web.json_response(
                {"status": "error", "message": f"No record found with pg_id={pg_id}"},
                status=404,
            )

        # Embedding — hard mandate, same as every record; no save without a vector.
        # Identity: a retrospective is (target decision, notes) — the target is part
        # of the hash, so identical boilerplate notes on two DIFFERENT decisions stay
        # two records (and can never hash-collide with a plain fact whose content
        # equals the notes), while re-saving the same retro still dedupes.
        content_hash = hashlib.sha256(
            f"retrospective:{pg_id}:{notes}".encode()
        ).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                embedding = await self._embed(notes, client)
        except RuntimeError as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=503)

        # Per-entity write locks — same discipline as handle_save: entity MERGEs
        # must be serialized (the locks are the only uniqueness guarantee for
        # Entity nodes), and a retro's elicited entities reach the same projection.
        acquired: list[asyncio.Lock] = []
        try:
            for e in sorted(set(metadata["entities"])):
                lk = await self._lock_for(e)
                await lk.acquire()
                acquired.append(lk)
            async with self._acquire() as conn:
                async with conn.transaction():
                    target = await conn.fetchrow(
                        "SELECT id, metadata->>'type' AS type,"
                        "       COALESCE(metadata->'decision'->>'project',"
                        "                metadata->>'project') AS project"
                        " FROM technical_docs WHERE id=$1 FOR SHARE",
                        pg_id,
                    )
                    if not target:
                        return web.json_response(
                            {"status": "error", "message": f"No record found with pg_id={pg_id}"},
                            status=404,
                        )
                    # Inherit the target's project so domain-scoped reads see the
                    # retro beside its decision.
                    if target["project"]:
                        metadata["project"] = target["project"]

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
                        notes, metadata, str(embedding), content_hash,
                        agent_id, body.get("scope", "global"),
                        body.get("visibility", "global"),
                    )
                    retro_pg_id = row["id"]

                    grounded_typed = await self._resolve_typed_grounding(
                        conn, grounded_ids, metadata.get("grounded_roles") or {}
                    )

                    # Outbox row under the RETRO'S OWN pg_id — ordinary record
                    # lifecycle (applied → rem_reviewed → consolidated → deleted
                    # after the insight fold). 'v': 2 selects the node projection;
                    # target_pg_id keys the insight triggers.
                    await conn.execute(
                        "INSERT INTO neo4j_outbox (pg_id, cypher_params) VALUES ($1, $2::jsonb)",
                        retro_pg_id,
                        {
                            "v": 2,
                            "type": "retrospective",
                            "target_pg_id": pg_id,
                            "retrospective": retro_payload
                                              | ({"superseded": True} if is_reversal else {}),
                            "content_snippet": notes[:200],
                            "source": agent_id,
                            "agent_id": agent_id,
                            "entities": metadata["entities"],
                            "source_ref": source_ref,
                            "fact_kind": fact_kind_from_source_ref(source_ref),
                            "grounded_in": grounded_ids,
                            "grounded": grounded_typed,
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
                    # Wake the consolidation daemon — the retro is a record now.
                    await conn.execute(
                        "SELECT pg_notify('new_artifact', $1)",
                        json.dumps({"pg_id": retro_pg_id}),
                    )
        finally:
            for lk in acquired:
                lk.release()

        return web.json_response({
            "status": "success",
            "pg_id": retro_pg_id,
            "target_pg_id": pg_id,
            "message": f"Retrospective stored with ID {retro_pg_id} "
                       f"(rating={rating}, target decision {pg_id}).",
        })

    # ── POST /memory/relations/review + /memory/relations/label ──────────────
    #
    # The operator-facing rung of the evidential ladder (decisions 726/727): REM
    # and the evidence sweep land every machine relation verdict in the
    # relation_adjudications ledger; the operator labels a stratified sample here
    # (the calibration oracle — per-family reliability is computed FROM these
    # labels, and an uncalibrated family's machine edges are invisible to
    # synthesis), and may PROMOTE a correct edge to asserted_by='operator',
    # which bypasses confidence thresholds permanently.

    async def _relation_calibration(self, conn, family: str) -> dict:
        """Per-family calibration state from operator labels — asyncpg port of
        relation_confidence.calibration_state (identical semantics: accept-verdict
        rows only; precision per 0.1 confidence band)."""
        rows = await conn.fetch(
            """
            SELECT width_bucket(COALESCE(confidence, 0.5), 0, 1, 10) AS band,
                   count(*) FILTER (WHERE operator_label IS NOT NULL) AS labeled,
                   count(*) FILTER (WHERE operator_label = 'correct') AS correct
            FROM relation_adjudications
            WHERE family = $1 AND verdict = 'accept'
            GROUP BY band ORDER BY band
            """,
            family,
        )
        bands, total_labeled = [], 0
        for r in rows:
            total_labeled += r["labeled"]
            bands.append({
                "band": f"{(r['band'] - 1) / 10:.1f}-{r['band'] / 10:.1f}",
                "labeled": r["labeled"],
                "precision": round(r["correct"] / r["labeled"], 3) if r["labeled"] else None,
            })
        return {
            "family": family,
            "labels": total_labeled,
            "calibrated": total_labeled >= RELCONF_MIN_LABELS,
            "min_labels": RELCONF_MIN_LABELS,
            "threshold": RELCONF_CONSUME_THRESHOLD[family],
            "bands": bands,
        }

    async def handle_relations_review(self, request: web.Request) -> web.Response:
        """Stratified unlabeled ledger sample for operator labeling — asyncpg port
        of relation_confidence.fetch_review_sample (rows bucketed into confidence
        deciles, drawn round-robin so labels cover the whole curve). Evidential
        rows are enriched with content snippets of both endpoint records; the
        response envelope carries the family's calibration state."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )
        family = body.get("family") or "entity_relation"
        if family not in RELATION_FAMILIES:
            return web.json_response(
                {"status": "error",
                 "message": f"family must be one of {list(RELATION_FAMILIES)}"},
                status=400,
            )
        limit = body.get("limit", RELATION_REVIEW_LIMIT_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return web.json_response(
                {"status": "error", "message": "limit must be a positive integer"},
                status=400,
            )
        limit = min(limit, RELATION_REVIEW_LIMIT_CAP)

        async with self._acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, family, src_name, tgt_name, src_pg_id, tgt_pg_id, rel_type,
                       verdict, method, confidence, support, signals, rationale, created_at
                FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY width_bucket(COALESCE(confidence, 0.5), 0, 1, 10)
                        ORDER BY created_at DESC) AS rn
                    FROM relation_adjudications
                    WHERE family = $1 AND operator_label IS NULL
                ) t
                ORDER BY rn, confidence DESC NULLS LAST
                LIMIT $2
                """,
                family, limit,
            )
            rows = [dict(r) for r in rows]
            if family == "evidential" and rows:
                pg_ids = sorted({p for r in rows
                                 for p in (r["src_pg_id"], r["tgt_pg_id"])
                                 if p is not None})
                snips = await conn.fetch(
                    "SELECT id, LEFT(content, $2) AS snippet"
                    " FROM technical_docs WHERE id = ANY($1)",
                    pg_ids, RELATION_SNIPPET_CHARS,
                )
                snip_map = {s["id"]: s["snippet"] for s in snips}
                for r in rows:
                    r["src_snippet"] = snip_map.get(r["src_pg_id"])
                    r["tgt_snippet"] = snip_map.get(r["tgt_pg_id"])
            calibration = await self._relation_calibration(conn, family)

        return web.json_response({
            "status": "success",
            "family": family,
            "rows": _json_safe(rows),
            "calibration": calibration,
        })

    def _relation_edge_match(self, row: dict) -> tuple[str, dict] | None:
        """Cypher MATCH fragment + params addressing a ledger row's LIVE edge.
        The rel_type is interpolated into Cypher, so membership in the schema's
        KNOWN_RELATIONSHIPS is a hard precondition (injection guard) — None when
        the rel is not schema vocabulary (e.g. the reject sentinel 'NONE'), which
        callers treat as "no edge to touch". Entity family matches name-keyed
        Entity endpoints; evidential family matches pg_id-keyed RECORD endpoints
        across the record labels (Fact / Decision / Retrospective)."""
        rel = row["rel_type"]
        if rel not in KNOWN_RELATIONSHIPS:
            return None
        if row["family"] == "entity_relation":
            return (
                f"MATCH (a:{ONT.entity} {{name: $src}})-[r:{rel}]->"
                f"(b:{ONT.entity} {{name: $tgt}})",
                {"src": row["src_name"], "tgt": row["tgt_name"]},
            )
        rec_a = " OR ".join(f"a:{lbl}" for lbl in
                            (ONT.fact, ONT.decision, ONT.retrospective))
        rec_b = " OR ".join(f"b:{lbl}" for lbl in
                            (ONT.fact, ONT.decision, ONT.retrospective))
        return (
            f"MATCH (a {{pg_id: $src}})-[r:{rel}]->(b {{pg_id: $tgt}})"
            f" WHERE ({rec_a}) AND ({rec_b})",
            {"src": row["src_pg_id"], "tgt": row["tgt_pg_id"]},
        )

    async def _promote_relation_edge(self, row: dict) -> int:
        """Flip the live edge to asserted_by='operator' (operator promotion —
        bypasses consumption thresholds permanently). Returns edges updated."""
        m = self._relation_edge_match(row)
        if m is None:
            return 0
        match, params = m
        cypher = (match
                  + " SET r.asserted_by = 'operator',"
                  + "     r.promoted_at = coalesce(r.promoted_at, datetime())"
                  + " RETURN count(r) AS n")
        async with self._neo4j.session() as session:
            result = await session.run(cypher, **params)
            rec = await result.single()
        return rec["n"] if rec else 0

    async def _delete_machine_relation_edge(self, row: dict) -> int:
        """Delete the live edge an operator labeled 'incorrect' — ONLY when the
        edge is machine-asserted (asserted_by IN rem/rem_sweep). An operator-
        asserted edge is never deleted here: the Cypher guard makes the delete a
        no-op on it. The ledger row always stays (audit + don't-re-ask).
        Returns edges deleted."""
        m = self._relation_edge_match(row)
        if m is None:
            return 0
        match, params = m
        glue = " AND " if " WHERE " in match else " WHERE "
        cypher = (match + glue + "r.asserted_by IN $machine"
                  + " WITH r DELETE r RETURN count(*) AS n")
        async with self._neo4j.session() as session:
            result = await session.run(
                cypher, machine=list(RELATION_MACHINE_ASSERTED), **params)
            rec = await result.single()
        return rec["n"] if rec else 0

    async def handle_relations_label(self, request: web.Request) -> web.Response:
        """Apply operator labels {row_id: 'correct'|'incorrect'} and optional
        promotions. Labels land in relation_adjudications.operator_label (the
        calibration substrate). Side effects on the LIVE graph:
          - 'incorrect' on an accept-verdict row → guarded delete of the machine
            edge (never an operator-asserted one); ledger row stays.
          - promote (row must be labeled 'correct', now or already) → ledger
            promoted_at=now() + live edge asserted_by='operator'.
        Per-row outcomes are reported; a Neo4j failure degrades to an edge_error
        on that row rather than failing the whole batch (the label is already
        durable in the ledger)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )
        raw_labels  = body.get("labels") or {}
        raw_promote = body.get("promote") or []
        if not isinstance(raw_labels, dict) or not isinstance(raw_promote, list):
            return web.json_response(
                {"status": "error",
                 "message": "labels must be an object and promote a list"},
                status=400,
            )
        labels: dict[int, str] = {}
        try:
            for rid, lab in raw_labels.items():
                if lab not in ("correct", "incorrect"):
                    return web.json_response(
                        {"status": "error",
                         "message": f"invalid operator label {lab!r} — "
                                    "must be 'correct' or 'incorrect'"},
                        status=400,
                    )
                labels[int(rid)] = lab
            promote = [int(p) for p in raw_promote]
        except (TypeError, ValueError):
            return web.json_response(
                {"status": "error", "message": "row ids must be integers"}, status=400
            )
        if not labels and not promote:
            return web.json_response(
                {"status": "error", "message": "labels and/or promote is required"},
                status=400,
            )

        _ROW_SQL = ("SELECT id, family, src_name, tgt_name, src_pg_id, tgt_pg_id,"
                    "       rel_type, verdict, operator_label, promoted_at"
                    " FROM relation_adjudications WHERE id = ANY($1)")
        outcomes: dict[str, dict] = {}
        async with self._acquire() as conn:
            # One batched fetch for every row either loop needs (an id in both
            # `labels` and `promote` is fetched once, not twice) instead of a
            # fetchrow per row — same N+1 pattern as _expand_graph_context_batch
            # fixed above, flagged by this repo's own code-review process.
            all_rids = sorted(set(labels.keys()) | set(promote))
            rows_by_id = {row["id"]: row for row in await conn.fetch(_ROW_SQL, all_rids)}

            for rid, lab in labels.items():
                row = rows_by_id.get(rid)
                if row is None:
                    outcomes[str(rid)] = {"error": "ledger row not found"}
                    continue
                await conn.execute(
                    "UPDATE relation_adjudications"
                    " SET operator_label=$2, operator_labeled_at=now(), updated_at=now()"
                    " WHERE id=$1",
                    rid, lab,
                )
                out: dict[str, Any] = {"labeled": lab}
                if lab == "incorrect" and row["verdict"] == "accept":
                    # An accepted edge the operator refutes leaves the graph —
                    # machine-asserted edges only; the ledger row stays.
                    try:
                        out["edge_deleted"] = await self._delete_machine_relation_edge(
                            dict(row))
                    except Exception as exc:
                        log.error("relation label: edge delete failed for row %d: %s",
                                  rid, exc, exc_info=True)
                        out["edge_error"] = "edge delete failed — label recorded"
                outcomes[str(rid)] = out

            for rid in promote:
                row = rows_by_id.get(rid)
                out = outcomes.setdefault(str(rid), {})
                if row is None:
                    out["error"] = "ledger row not found"
                    continue
                effective = labels.get(rid) or row["operator_label"]
                if effective != "correct":
                    out["promoted"] = False
                    out["error"] = ("promotion requires an operator label of "
                                    "'correct' (in this call or already recorded)")
                    continue
                await conn.execute(
                    "UPDATE relation_adjudications"
                    " SET promoted_at=now(), updated_at=now() WHERE id=$1",
                    rid,
                )
                out["promoted"] = True
                try:
                    out["edges_updated"] = await self._promote_relation_edge(dict(row))
                except Exception as exc:
                    log.error("relation promote: edge update failed for row %d: %s",
                              rid, exc, exc_info=True)
                    out["edge_error"] = "edge update failed — promotion recorded in ledger"
        return web.json_response({"status": "success", "outcomes": outcomes})

    # ── POST /memory/search ───────────────────────────────────────────────────

    async def _expand_graph_context(self, session, pg_id: int,
                                    anchor_labels: tuple[str, ...]) -> list[dict]:
        """Read-contract graph expansion for one anchored record.

        Anchors on any of ``anchor_labels`` (by ``pg_id``) and returns one entry
        per edge, capped at GRAPH_EXPANSION_LIMIT with provenance-bearing edges
        (``r.asserted_by`` set) and typed relations ordered ahead of bare
        MENTIONS. Every edge surfaces its type, direction, and FULL property map
        (asserted_by / confidence / role / method / support / created_at, …) —
        a bare relation name cannot be weighed by the consumer.

        Neighbor identity is never silently dropped: name-keyed nodes (Entity /
        Human / AIAgent / Project) keep the legacy ``{rel_type, name, label,
        aliases?}`` shape plus the new keys; pg_id-keyed nodes (Decision /
        Retrospective / Fact / CommunitySummary) return ``{rel_type, direction,
        properties, label, pg_id, snippet}`` where snippet is the first ~120
        chars of the node's text-bearing property (content / title+rationale /
        notes / rem_summary — null when the node carries none). Entity ALIASES
        siblings are still folded in (ADR-017). Failures degrade to [] — graph
        context enriches a search, it never fails one.
        """
        ctx: list[dict] = []
        anchor_where = " OR ".join(f"n:{lbl}" for lbl in anchor_labels)
        try:
            result = await session.run(
                f"MATCH (n {{pg_id: $pg_id}}) WHERE {anchor_where}"
                " OPTIONAL MATCH (n)-[r]-(related)"
                # ADR-017: also pull each related Entity's alias siblings so
                # search surfaces every surface form of a concept. One query,
                # no-op-safe (empty when no ALIASES edges exist). Gated on
                # ontology.py's GENUINELY_REFERENCED_ENTITY_RULE (decision 890)
                # so a Decision-provenance node's stray legacy ALIASES edge
                # (pre-718) never surfaces a wrongly-merged real entity name
                # as if it were a "surface form" of free-text condition/
                # alternative content — same criterion as fetch_entities().
                f" OPTIONAL MATCH (related)-[:{ONT.aliases}]-(al:{ONT.entity})"
                f"   WHERE EXISTS {{"
                f"     MATCH (related)<-[:{ONT.entity_link}]-(m)"
                f"     WHERE m.pg_id IS NOT NULL AND coalesce(m.superseded,false) = false"
                f"   }}"
                " WITH n, r, related, labels(related) AS labels,"
                "      collect(DISTINCT al.name) AS aliases"
                # Highest-signal edges survive the cap: provenance-bearing
                # first, then any typed relation, bare MENTIONS last.
                " ORDER BY CASE WHEN r.asserted_by IS NOT NULL THEN 0 ELSE 1 END,"
                f"          CASE WHEN type(r) = '{ONT.entity_link}' THEN 1 ELSE 0 END"
                " LIMIT $cap"
                " RETURN labels, related.name AS name, related.pg_id AS pg_id,"
                "        type(r) AS rel_type,"
                "        CASE WHEN r IS NULL THEN null"
                "             WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction,"
                "        properties(r) AS rel_props,"
                "        left(coalesce(related.content, related.title,"
                "                      related.rationale, related.notes,"
                "                      related.rem_summary), 120) AS snippet,"
                # ADR node properties already sitting on this one-hop neighbor —
                # projected so a summary hit carries a folded decision's
                # confidence/alternatives and a folded fact's fact_kind/source_ref
                # WITHOUT a second query (decision 909). Null on neighbors that
                # do not carry them; folded into `adr_props` below only when set.
                "        related.confidence AS adr_confidence,"
                "        related.alternatives AS adr_alternatives,"
                "        related.fact_kind AS adr_fact_kind,"
                "        related.source_ref AS adr_source_ref,"
                "        aliases",
                pg_id=pg_id, cap=GRAPH_EXPANSION_LIMIT,
            )
            async for rec in result:
                if not rec["rel_type"]:
                    continue  # OPTIONAL MATCH row for an anchor with no edges
                entry = {
                    "rel_type": rec["rel_type"],
                    "direction": rec["direction"],
                    "properties": _json_safe(dict(rec["rel_props"] or {})),
                    "label": rec["labels"][0] if rec["labels"] else None,
                }
                if rec["name"]:
                    # Name-keyed neighbor — legacy shape preserved, new keys additive.
                    entry["name"] = rec["name"]
                    if rec["aliases"]:
                        entry["aliases"] = rec["aliases"]
                else:
                    # pg_id-keyed neighbor (Decision/Retrospective/Fact/
                    # CommunitySummary) — previously dropped by the name filter.
                    entry["pg_id"] = rec["pg_id"]
                    entry["snippet"] = rec["snippet"]
                    adr = _neighbor_adr_props(rec)
                    if adr:
                        entry["adr_props"] = adr
                ctx.append(entry)
        except Exception:
            return []
        return ctx

    async def _expand_graph_context_batch(
        self, session, pg_ids: list[int], anchor_labels: tuple[str, ...],
    ) -> dict[int, list[dict]]:
        """Batched form of `_expand_graph_context`: one Neo4j round-trip for
        every anchor in `pg_ids` instead of one round-trip per anchor.

        `handle_search`'s two `_expand_graph_context` loops were N+1 — up to
        ~102 sequential queries per call (this repo's own code-review process
        flagged it). Same query body as `_expand_graph_context`, wrapped in a
        `CALL (pg_id) {{ ... LIMIT $cap }}` correlated subquery per `UNWIND`ed
        anchor so the per-anchor cap is preserved exactly (a single flat `LIMIT` across
        all anchors combined would silently change behaviour, not just
        performance). Returns `{pg_id: [entries]}`; any `pg_id` with no anchor
        node or no edges maps to `[]`, matching `_expand_graph_context`'s
        single-anchor return. Same degrade-to-empty contract on failure —
        graph context enriches a search, it never fails one, so a query error
        here returns `{}` (every caller treats a missing key as `[]` via `.get`).
        """
        if not pg_ids:
            return {}
        out: dict[int, list[dict]] = {pid: [] for pid in pg_ids}
        anchor_where = " OR ".join(f"n:{lbl}" for lbl in anchor_labels)
        try:
            result = await session.run(
                "UNWIND $pg_ids AS pg_id"
                " CALL (pg_id) {"
                f"   MATCH (n {{pg_id: pg_id}}) WHERE {anchor_where}"
                "   OPTIONAL MATCH (n)-[r]-(related)"
                f"   OPTIONAL MATCH (related)-[:{ONT.aliases}]-(al:{ONT.entity})"
                f"     WHERE EXISTS {{"
                f"       MATCH (related)<-[:{ONT.entity_link}]-(m)"
                f"       WHERE m.pg_id IS NOT NULL AND coalesce(m.superseded,false) = false"
                f"     }}"
                "   WITH n, r, related, labels(related) AS labels,"
                "        collect(DISTINCT al.name) AS aliases"
                "   ORDER BY CASE WHEN r.asserted_by IS NOT NULL THEN 0 ELSE 1 END,"
                f"            CASE WHEN type(r) = '{ONT.entity_link}' THEN 1 ELSE 0 END"
                "   LIMIT $cap"
                "   RETURN labels, related.name AS name, related.pg_id AS rel_pg_id,"
                "          type(r) AS rel_type,"
                "          CASE WHEN r IS NULL THEN null"
                "               WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction,"
                "          properties(r) AS rel_props,"
                "          left(coalesce(related.content, related.title,"
                "                        related.rationale, related.notes,"
                "                        related.rem_summary), 120) AS snippet,"
                # ADR node props on the one-hop neighbor — see the single-anchor
                # form above (decision 909). Same projection, batched.
                "          related.confidence AS adr_confidence,"
                "          related.alternatives AS adr_alternatives,"
                "          related.fact_kind AS adr_fact_kind,"
                "          related.source_ref AS adr_source_ref,"
                "          aliases"
                " }"
                " RETURN pg_id AS anchor_pg_id, labels, name, rel_pg_id, rel_type,"
                "        direction, rel_props, snippet,"
                "        adr_confidence, adr_alternatives, adr_fact_kind, adr_source_ref,"
                "        aliases",
                pg_ids=list(pg_ids), cap=GRAPH_EXPANSION_LIMIT,
            )
            async for rec in result:
                if not rec["rel_type"]:
                    continue  # OPTIONAL MATCH row for an anchor with no edges
                entry = {
                    "rel_type": rec["rel_type"],
                    "direction": rec["direction"],
                    "properties": _json_safe(dict(rec["rel_props"] or {})),
                    "label": rec["labels"][0] if rec["labels"] else None,
                }
                if rec["name"]:
                    entry["name"] = rec["name"]
                    if rec["aliases"]:
                        entry["aliases"] = rec["aliases"]
                else:
                    entry["pg_id"] = rec["rel_pg_id"]
                    entry["snippet"] = rec["snippet"]
                    adr = _neighbor_adr_props(rec)
                    if adr:
                        entry["adr_props"] = adr
                out[rec["anchor_pg_id"]].append(entry)
        except Exception:
            return {pid: [] for pid in pg_ids}
        return out

    async def handle_search(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )

        query = body.get("query", "")
        limit = body.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int):
            return web.json_response(
                {"status": "error", "message": "limit must be an integer"}, status=400
            )
        limit = min(max(1, limit), 100)
        scope = body.get("scope")  # None = no scope filter
        # Read authorization — the server-verified identity gates which rows this
        # caller may see (visibility column). None = anonymous → 'global' only.
        viewer = request.get("authenticated_agent")

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
                vis_sql, vis_params = _visibility_filter(viewer, scope, 3)
                async with self._acquire() as conn:
                    rows = await conn.fetch(
                        f"""
                        SELECT id, content, metadata FROM technical_docs
                        WHERE (content ILIKE $1 OR metadata::text ILIKE $1)
                          AND {vis_sql}
                        LIMIT $2
                        """,
                        f"%{query}%", limit, *vis_params,
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
                # Same read-authorization predicate gates Tier-3: a private/scoped
                # fact's synthesized narrative must not leak where the fact itself
                # is filtered. Params start at $2 ($1 is the query vector).
                vis_t3, vis_t3_params = _visibility_filter(viewer, scope, 2)
                insight = None
                try:
                    insight = await conn.fetchrow(
                        "SELECT id, content, metadata, source_pg_ids FROM community_summaries"
                        " WHERE NOT superseded"
                        "   AND metadata->>'kind' = 'insight'"
                        f"   AND {vis_t3}"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec), *vis_t3_params,
                    )
                except Exception:
                    insight = None  # pre-006 schema — thematic guard below warns

                # Thematic summary. Guard: if migration 006 has not been
                # applied, fall back to the unsupervised query so search
                # continues to work (with a warning).
                try:
                    summary = await conn.fetchrow(
                        "SELECT id, content, metadata, source_pg_ids FROM community_summaries"
                        " WHERE NOT superseded"
                        "   AND COALESCE(metadata->>'kind', 'thematic') <> 'insight'"
                        f"   AND {vis_t3}"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec), *vis_t3_params,
                    )
                except Exception:
                    log.warning(
                        "community_summaries.superseded column missing — "
                        "run migrations: uv run --with psycopg2-binary "
                        "python shared-memory/migrations/apply.py"
                    )
                    summary = await conn.fetchrow(
                        "SELECT id, content, metadata, source_pg_ids FROM community_summaries"
                        f" WHERE {vis_t3}"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec), *vis_t3_params,
                    )

                # Tier 1 — vector search, 20 candidates for reranker.
                # Reversed decisions (superseded=true, migration 009) are
                # excluded; the fallback keeps pre-migration schemas working.
                args: list = [str(q_vec), 20]
                scope_sql = ""
                if scope:
                    args.append(scope)
                    scope_sql = f"AND scope = ${len(args)}"
                vis_sql, vis_params = _visibility_filter(viewer, scope, len(args) + 1)
                args.extend(vis_params)
                try:
                    candidates = await conn.fetch(
                        f"""
                        SELECT id, content, metadata, created_at FROM technical_docs
                        WHERE NOT superseded AND {vis_sql} {scope_sql}
                        ORDER BY embedding <=> $1::vector LIMIT $2
                        """,
                        *args,
                    )
                except Exception:
                    candidates = await conn.fetch(
                        f"""
                        SELECT id, content, metadata, created_at FROM technical_docs
                        WHERE {vis_sql} {scope_sql}
                        ORDER BY embedding <=> $1::vector LIMIT $2
                        """,
                        *args,
                    )

            if not candidates:
                return web.json_response({"status": "success", "results": []})

            ids      = [r["id"]       for r in candidates]
            contents = [r["content"]  for r in candidates]
            metas    = [_coerce_jsonb_obj(r["metadata"]) for r in candidates]
            # .get: tolerant of pre-migration schemas (and test stubs) where the
            # created_at column is absent — recency simply degrades to off.
            createds = [r.get("created_at") for r in candidates]

            # Rerank — direct to port 8071 to avoid circular proxy call.
            # Decisions/retrospectives are scored WITH their recording date
            # prepended (recency-aware: the newest retro is the current verdict).
            rerank_docs = [
                _rerank_doc_text(c, m, t)
                for c, m, t in zip(contents, metas, createds)
            ]
            try:
                rr = await client.post(
                    RERANK_URL,
                    json={"query": query, "documents": rerank_docs, "top_k": limit},
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
                # A DIFFERENT id namespace from the fact tier below — same field
                # name, independent sequence. record_type/ref disambiguate it.
                "record_type": summary_record_type(insight["metadata"]),
                "ref": make_ref(summary_record_type(insight["metadata"]),
                                insight.get("id")),
                # .get: tolerant of pre-change callers/stubs without the id
                # column — pg_id (community_summaries.id = the CommunitySummary
                # node key) enables the summary→sources graph walk below.
                "pg_id": insight.get("id"),
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
                # Same-name, different-namespace id — see the insight tier above.
                "record_type": summary_record_type(summary["metadata"]),
                "ref": make_ref(summary_record_type(summary["metadata"]),
                                summary.get("id")),
                "pg_id": summary.get("id"),
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
            # Summary→sources walk: a Tier-3 narrative's graph context (its
            # SUMMARIZED_BY source records + any typed edges) surfaces the same
            # way a record's does. Degrades to [] on any Neo4j failure. Batched
            # (one round-trip for every summary/insight anchor, not one per) —
            # was an N+1 here, up to ~102 sequential queries per search call.
            summary_pg_ids = [res["pg_id"] for res in final if res.get("pg_id") is not None]
            summary_ctx = await self._expand_graph_context_batch(
                session, summary_pg_ids, (ONT.community_summary,)
            )
            for res in final:
                if res.get("pg_id") is not None:
                    res["graph_context"] = summary_ctx.get(res["pg_id"], [])

            # Same batching for the Tier-1 hits' graph context. Anchor on ALL
            # record labels — Decision and Retrospective rows get graph context
            # too, not just Facts (read contract).
            fact_pg_ids = [ids[hit["index"]] for hit in ranked]
            fact_ctx = await self._expand_graph_context_batch(
                session, fact_pg_ids, (ONT.fact, ONT.decision, ONT.retrospective)
            )
            for hit in ranked:
                idx   = hit["index"]
                pg_id = ids[idx]
                raw_score = hit["relevance_score"]
                ctx = fact_ctx.get(pg_id, [])
                # `tier` says WHERE the hit came from; `record_type` says WHAT it
                # is. They are not the same: the Tier-1 "fact" tier carries
                # decisions and retrospectives too, and the id namespace is keyed
                # on the record type, not the tier.
                rtype = doc_record_type(metas[idx])
                final.append({
                    "tier": "fact",
                    "pg_id": pg_id,
                    "record_type": rtype,
                    "ref": make_ref(rtype, pg_id),
                    "content": contents[idx],
                    "score": raw_score,
                    "score_normalized": _sigmoid(raw_score),
                    "matched_entities": _matched_entities(query, metas[idx]),
                    "metadata": metas[idx],
                    "created_at": (createds[idx].isoformat()
                                   if createds[idx] is not None else None),
                    "graph_context": ctx,
                })

        # Latest-retro-as-verdict: same-decision retrospectives newest-first.
        final = _order_retros_latest_first(final)
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
        """Full record lineage — "what happened to pg_id N?". The coordinator owns both
        backends and does the joins (ADR-014); the thin client only calls the gateway.
        Returns record state (type / created_at / superseded / grounded_in), the
        in-flight dream-cycle stamps, and what it consolidated INTO — which summary or
        insight (the FORM, via the source_pg_ids reverse lookup) and the coarse
        fact→summary latency when both timestamps exist. Backwards-compatible: the old
        `neo4j`/`retries`/`applied_at` fields are retained."""
        # Accepts a qualified reference (`fact:816`, `summary:87`) or a bare
        # integer. The bare form still means technical_docs — the compatibility
        # concession, and the one place the namespace ambiguity survives.
        try:
            record_type, pg_id = parse_ref(request.match_info["pg_id"])
        except ValueError as exc:
            return web.json_response(
                {"status": "error",
                 "message": f"reference must be an integer or <type>:<id> ({exc})"},
                status=400,
            )

        # A summary id resolved against technical_docs is the exact wrong answer
        # this qualification exists to prevent: the sequences are independent, so
        # the lookup would succeed and return an unrelated record. Route it to
        # the table it actually names.
        if record_type in REF_TYPES_SUMMARIES:
            return await self._status_of_summary(pg_id, record_type)

        async with self._acquire() as conn:
            rec = await conn.fetchrow(
                "SELECT metadata->>'type' AS type, created_at, superseded, superseded_by,"
                "       metadata->'grounded_in' AS grounded_in"
                " FROM technical_docs WHERE id = $1", pg_id,
            )
            ob = await conn.fetchrow(
                "SELECT status, retries, applied_at, rem_reviewed_at, consolidated_at"
                " FROM neo4j_outbox WHERE pg_id = $1 ORDER BY id DESC LIMIT 1", pg_id,
            )
            summ = await conn.fetch(
                "SELECT cs.id, COALESCE(cs.metadata->>'kind','thematic') AS kind,"
                "       cs.metadata->>'entity' AS entity, cs.created_at, cs.run_id,"
                "       cr.started_at AS cycle_started, cr.finished_at AS cycle_finished"
                " FROM community_summaries cs"
                " LEFT JOIN consolidation_runs cr ON cr.id = cs.run_id"
                " WHERE $1 = ANY(cs.source_pg_ids) AND NOT cs.superseded ORDER BY cs.id", pg_id,
            )

        if rec is None and ob is None:
            return web.json_response({"pg_id": pg_id, "exists": False, "neo4j": "unknown"})

        # A qualified ref that names the wrong type is a caller error worth
        # surfacing: the id resolved, but not to the record the caller meant.
        # Silently returning the row is how a mismatched reference becomes a
        # confident wrong answer.
        actual_type = doc_record_type({"type": rec["type"]} if rec else None)
        if record_type and record_type != actual_type:
            return web.json_response(
                {"status": "error", "pg_id": pg_id,
                 "message": (f"{make_ref(record_type, pg_id)} does not exist — id {pg_id} "
                             f"in technical_docs is a {actual_type} "
                             f"({make_ref(actual_type, pg_id)})")},
                status=404,
            )

        def _iso(t):
            return t.isoformat() if t else None

        consolidated_into = []
        for s in summ:
            latency = None
            if rec and rec["created_at"] and s["created_at"]:
                latency = round((s["created_at"] - rec["created_at"]).total_seconds(), 3)
            cycle_dur = None
            if s["cycle_started"] and s["cycle_finished"]:
                cycle_dur = round((s["cycle_finished"] - s["cycle_started"]).total_seconds(), 3)
            consolidated_into.append({
                "summary_pg_id": s["id"],
                "form": "insight" if s["kind"] == "insight" else "thematic_summary",
                "entity": s["entity"],
                "summary_created_at": _iso(s["created_at"]),
                "fact_to_summary_seconds": latency,
                # which consolidation cycle produced/last-refreshed this summary + how
                # long that cycle ran (fact → summary → cycle join, Stage 2b)
                "run_id": s["run_id"],
                "cycle_duration_seconds": cycle_dur,
            })

        gi = rec["grounded_in"] if rec else None
        if isinstance(gi, str):
            try:
                gi = json.loads(gi)
            except Exception:
                gi = None

        return web.json_response({
            "pg_id": pg_id,
            # The unambiguous form of the thing just returned — quote THIS back,
            # not the bare id, and the reference can never resolve elsewhere.
            "record_type": actual_type,
            "ref": make_ref(actual_type, pg_id),
            "exists": rec is not None,
            "type": rec["type"] if rec else None,
            "created_at": _iso(rec["created_at"]) if rec else None,
            "superseded": rec["superseded"] if rec else None,
            "superseded_by": rec["superseded_by"] if rec else None,
            "grounded_in": gi if isinstance(gi, list) else None,
            # in-flight dream-cycle stamps — None once the outbox row is deleted
            "neo4j": ob["status"] if ob else "unknown",
            "retries": ob["retries"] if ob else None,
            "applied_at": _iso(ob["applied_at"]) if ob else None,
            "rem_reviewed_at": _iso(ob["rem_reviewed_at"]) if ob else None,
            "consolidated_at": _iso(ob["consolidated_at"]) if ob else None,
            # what it became (durable — from the source_pg_ids reverse lookup)
            "consolidated_into": consolidated_into,
        })

    async def _status_of_summary(self, pg_id: int, record_type: str) -> web.Response:
        """Status of a `community_summaries` row — the other id namespace.

        Reached only from a QUALIFIED reference (`summary:87` / `insight:87`),
        because a bare integer cannot say which table it means and must keep
        resolving against technical_docs for compatibility. Returns the
        narrative's own identity plus the Tier-1 records it was synthesised
        from, so a summary can be traced to its sources with one call — those
        `source_pg_ids` ARE technical_docs ids, and are handed back already
        qualified so they cannot be mistaken for ids in this namespace."""
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, metadata, source_pg_ids, created_at, superseded, run_id"
                "  FROM community_summaries WHERE id = $1", pg_id,
            )
            if row is None:
                return web.json_response(
                    {"pg_id": pg_id, "record_type": record_type,
                     "ref": make_ref(record_type, pg_id), "exists": False},
                    status=404,
                )
            meta   = _coerce_jsonb_obj(row["metadata"])
            actual = summary_record_type(meta)
            if record_type != actual:
                return web.json_response(
                    {"status": "error", "pg_id": pg_id,
                     "message": (f"{make_ref(record_type, pg_id)} does not exist — id "
                                 f"{pg_id} in community_summaries is a {actual} "
                                 f"({make_ref(actual, pg_id)})")},
                    status=404,
                )
            src_types = {}
            if row["source_pg_ids"]:
                for r in await conn.fetch(
                    "SELECT id, metadata->>'type' AS type FROM technical_docs"
                    "  WHERE id = ANY($1::bigint[])", list(row["source_pg_ids"]),
                ):
                    src_types[r["id"]] = doc_record_type({"type": r["type"]})
        return web.json_response({
            "pg_id": pg_id,
            "record_type": actual,
            "ref": make_ref(actual, pg_id),
            "exists": True,
            "entity": meta.get("entity"),
            "domain": meta.get("domain"),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "superseded": row["superseded"],
            "run_id": row["run_id"],
            "source_pg_ids": list(row["source_pg_ids"] or []),
            "sources": [
                {"pg_id": sid,
                 "record_type": src_types.get(sid),
                 "ref": (make_ref(src_types[sid], sid) if sid in src_types else None)}
                for sid in (row["source_pg_ids"] or [])
            ],
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
                    f"        coalesce(f.consolidated,false) AS con,"
                    f"        coalesce(f.superseded,false) AS superseded, count(*) AS n"
                )
                facts = await fres.data()
                dres = await session.run(
                    f"MATCH (d:{ONT.decision})"
                    f" RETURN coalesce(d.rem_processed,false) AS rem,"
                    f"        coalesce(d.superseded,false) AS superseded, count(*) AS n"
                )
                decisions = await dres.data()
                # REM attempt/dead-letter gauge: a record at the attempt cap is
                # excluded from REM's queue but still counts as rem_pending, so
                # without this the backlog just sits there with no way to tell
                # "waiting its turn" from "given up on and never retried again".
                ares = await session.run(
                    f"MATCH (n) WHERE (n:{ONT.fact} OR n:{ONT.decision}"
                    f"                 OR n:{ONT.retrospective})"
                    f"   AND coalesce(n.rem_processed,false) = false"
                    f"   AND coalesce(n.superseded,false) = false"
                    f"   AND n.pg_id IS NOT NULL"
                    f" RETURN coalesce(n.rem_attempts,0) AS a,"
                    f"        coalesce(n.rem_passed_over,0) AS p, count(*) AS n"
                )
                attempts = await ares.data()
            _cap = REM_MAX_ATTEMPTS
            snap["neo4j"] = {
                "facts_total":          sum(r["n"] for r in facts),
                # Superseded records are permanently excluded from REM's own
                # candidacy query (rem_loop.py:_fetch_non_rem_batch) — counting
                # them here inflates "pending" with a backlog REM will never
                # touch and no operator action can ever clear.
                "facts_rem_pending":    sum(r["n"] for r in facts if not r["rem"] and not r["superseded"]),
                "facts_unconsolidated": sum(r["n"] for r in facts if r["rem"] and not r["con"]),
                "decisions_total":      sum(r["n"] for r in decisions),
                "decisions_rem_pending": sum(r["n"] for r in decisions if not r["rem"] and not r["superseded"]),
                # Records REM has given up on: excluded from its queue until an
                # operator resets n.rem_attempts. Non-zero means enrichment is
                # silently losing records — investigate before it grows.
                "rem_dead_lettered":    sum(r["n"] for r in attempts if r["a"] >= _cap),
                # Pending records carrying at least one failed attempt.
                "rem_failing":          sum(r["n"] for r in attempts if 0 < r["a"] < _cap),
                "rem_max_attempts":     _cap,
                # STEP 3 (decision 890) fairness gauge — ships dormant (reads 0
                # until the solo backlog is large enough to re-exercise the
                # batch-vs-solo yield path; baseline: 8/15 yields at 0 solo
                # records handled, 2026-07-20 12:28-22:41, before this fix).
                "rem_passed_over_total": sum(r["n"] * r["p"] for r in attempts),
                "rem_starved_pending":  sum(r["n"] for r in attempts
                                            if r["p"] >= REM_STARVED_THRESHOLD),
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

        # Schema compliance — node labels / relationship types in the live graph
        # outside the ontology vocabulary (legacy or foreign drift the inbound
        # gates now prevent but cannot retroactively remove). Predicate
        # distribution is the supporting census.
        try:
            snap["compliance"] = await self._graph_compliance()
        except Exception as exc:
            snap["compliance"] = {"error": str(exc)}

        # Graph integrity (decision 928) — REM's label_mismatch verdict, which it
        # has always computed and nothing ever read. A write-path defect signal,
        # not a backlog: non-zero means a writer is producing nodes under the
        # wrong label and someone must fix it.
        try:
            snap["graph_integrity"] = await self._graph_integrity()
        except Exception as exc:
            snap["graph_integrity"] = {"error": str(exc)}

        # Consolidation signal (ADR-018) — the dream-cycle liveness rollup from
        # the daemon's consolidation_runs ledger: per-cycle-type last outcome,
        # success age, in-flight, last error, plus the derived stall verdict.
        # Computed fresh here (telemetry is auth-scoped and already heavier); the
        # cheaper /health subset reads the cached snapshot instead.
        try:
            snap["consolidation"] = await self._consolidation_telemetry()
        except Exception as exc:
            snap["consolidation"] = {"error": str(exc)}

        # Spine coverage (decision 559) — required-field completeness + the elicited
        # rate + emergent (captured-but-unprojected) fields + alias-adjudication
        # volume. The data behind the first-write-quality push; the monitor samples
        # this over time for the trend.
        try:
            snap["spine"] = await self._spine_telemetry()
        except Exception as exc:
            snap["spine"] = {"error": str(exc)}

        # Latency (decisions 568/570/571) — REM service/contention per model (durable,
        # ungated, model/hardware) + the NREM whole-cycle compute window alongside it.
        # Never fact→summary (gate-dominated, survivorship-biased — fact 567).
        try:
            snap["latency"] = await self._latency_telemetry()
        except Exception as exc:
            snap["latency"] = {"error": str(exc)}

        # Inference/GPU-busy signal (tri-state: "busy"|"idle"|"unknown"). Read the
        # cached value the consolidation refresher already probed so telemetry never
        # shells out to nvtop itself. "unknown" (nvtop absent) is surfaced verbatim
        # so the monitor never shows a false "idle".
        snap["inference_busy"] = self._consolidation_health.get("inference_busy", "unknown")

        return web.json_response({"status": "success", "telemetry": snap})

    # ── Spine coverage (decision 559) ─────────────────────────────────────────

    async def _spine_telemetry(self) -> dict:
        """Spine-coverage telemetry — the data behind the first-write-quality push.
        Three families as cheap Postgres aggregates (the monitor samples over time
        for the trend): (A) required-field completeness + the elicited rate — an
        elicited null is a deliberate choice, so completeness is read *among
        elicited saves*; (B) emergent = metadata keys captured but NOT first-write
        projected (promotion candidates); (C) alias-adjudication volume (does the
        deterministic projection keep the graph clean). No hot-path counters."""
        PROJECTED = {"source", "type", "entities", "decision", "source_ref",
                     "supersedes", "grounded_in", "fact_kind", "elicited"}

        def pct(a: int, b: int) -> float:
            return round(100.0 * a / b, 1) if b else 0.0

        async with self._acquire() as conn:
            drow = await conn.fetchrow(
                "SELECT count(*) AS n,"
                " count(*) FILTER (WHERE metadata ? 'grounded_in') AS grounded,"
                " count(*) FILTER (WHERE metadata->'decision' ? 'alternatives') AS alts,"
                " count(*) FILTER (WHERE metadata->'decision' ? 'confidence') AS conf,"
                " count(*) FILTER (WHERE (metadata->>'elicited')='true') AS elicited"
                " FROM technical_docs"
                " WHERE metadata->>'type'='decision' AND NOT superseded"
            )
            frow = await conn.fetchrow(
                "SELECT count(*) AS n,"
                " count(*) FILTER (WHERE metadata ? 'source_ref') AS sref,"
                " count(*) FILTER (WHERE (metadata->>'elicited')='true') AS elicited"
                " FROM technical_docs"
                " WHERE (metadata->>'type' IS NULL OR metadata->>'type' <> 'decision')"
                "   AND NOT superseded"
            )
            keys = await conn.fetch(
                "SELECT k, count(*) AS n FROM technical_docs, jsonb_object_keys(metadata) k"
                " WHERE NOT superseded GROUP BY k ORDER BY n DESC"
            )
            try:
                alias_total = await conn.fetchval("SELECT count(*) FROM alias_adjudications")
                asplit = await conn.fetch(
                    "SELECT verdict, count(*) AS n FROM alias_adjudications GROUP BY verdict"
                )
                alias = {"adjudications": alias_total,
                         "by_verdict": {r["verdict"]: r["n"] for r in asplit}}
            except Exception as exc:
                alias = {"error": str(exc)}

        dn, fn = drow["n"], frow["n"]
        emergent = [{"key": r["k"], "n": r["n"]}
                    for r in keys if r["k"] not in PROJECTED][:12]
        return {
            "decisions": {
                "total": dn,
                "grounded_in_pct": pct(drow["grounded"], dn),
                "alternatives_pct": pct(drow["alts"], dn),
                "confidence_pct": pct(drow["conf"], dn),
                "elicited_pct": pct(drow["elicited"], dn),
            },
            "facts": {
                "total": fn,
                "source_ref_pct": pct(frow["sref"], fn),
                "elicited_pct": pct(frow["elicited"], fn),
            },
            "emergent_unprojected_fields": emergent,
            "alias": alias,
        }

    # ── Latency rollup (decisions 568/570/571) ────────────────────────────────

    async def _latency_telemetry(self) -> dict:
        """Latency rollup for the monitor, from the DURABLE technical_docs.rem_timing
        (survives outbox deletion — migration 019). REM is the anchor because it is
        UNGATED: every saved fact passes through it (fact 567), so this is unbiased and
        reflects model + hardware. Two REM percentile pairs, grouped by model so the
        series is a model-evolution axis (decision 571):
          service_ms   = pure inference = MODEL + HARDWARE, load-invariant.
          contention_ms= queue behind a busy backend = CAPACITY (→ 0 as the pool grows).
        The NREM whole-cycle COMPUTE window (consolidation_runs started_at→finished_at)
        is kept ALONGSIDE (decision 568), never fact→summary — that end-to-end is
        density-gate-dominated and survivorship-biased, an erroneous latency (fact 567).
        p50/p95 via percentile_cont; each block independent so one failure spares the rest."""
        def _r(v):
            return round(float(v), 1) if v is not None else None

        out: dict = {}
        async with self._acquire() as conn:
            # REM: per-model service/contention percentiles over the durable rows.
            rem_rows = await conn.fetch(
                "SELECT rem_timing->>'model' AS model, count(*) AS n,"
                "  percentile_cont(0.5)  WITHIN GROUP (ORDER BY (rem_timing->>'service_ms')::float)    AS svc_p50,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY (rem_timing->>'service_ms')::float)    AS svc_p95,"
                "  percentile_cont(0.5)  WITHIN GROUP (ORDER BY (rem_timing->>'contention_ms')::float) AS con_p50,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY (rem_timing->>'contention_ms')::float) AS con_p95,"
                "  max((rem_timing->>'batch_size')::int) AS max_batch"
                " FROM technical_docs"
                " WHERE rem_timing IS NOT NULL AND (rem_timing->>'service_ms') IS NOT NULL"
                " GROUP BY rem_timing->>'model' ORDER BY n DESC"
            )
            # NREM whole-cycle compute window (kept alongside REM, decision 568).
            # ONLY cycles that actually synthesised (folds_succeeded > 0): deferred and
            # no-op sweeps open+close a row instantly (~0s) and otherwise swamp p50/p95
            # to zero — the same meaningless-denominator trap that fact 567 warns about.
            # Same gate as last_success in _compute_consolidation_health.
            cyc = await conn.fetchrow(
                "SELECT count(*) AS n,"
                "  percentile_cont(0.5)  WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at-started_at))) AS p50,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at-started_at))) AS p95"
                " FROM consolidation_runs"
                " WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
                "   AND folds_succeeded > 0"
                "   AND finished_at >= now() - interval '7 days'"
            )
        out["rem_ms"] = {
            "note": "service_ms = model/hardware (anchor); contention_ms = capacity",
            "by_model": [
                {"model": r["model"], "n": r["n"], "max_batch_size": r["max_batch"],
                 "service_ms":    {"p50": _r(r["svc_p50"]), "p95": _r(r["svc_p95"])},
                 "contention_ms": {"p50": _r(r["con_p50"]), "p95": _r(r["con_p95"])}}
                for r in rem_rows
            ],
        }
        out["nrem_cycle_seconds"] = (
            {"window_days": 7, "n": cyc["n"], "p50": _r(cyc["p50"]), "p95": _r(cyc["p95"]),
             "note": "synthesis cycles only (folds_succeeded>0); excludes deferred/no-op sweeps"}
            if cyc else {}
        )
        return out

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
                     -- Projected into the CTE because the outer aggregate reads
                     -- them; a column that exists on consolidation_runs but is
                     -- not listed here is invisible downstream.
                     folds_succeeded, folds_attempted,
                     max(finished_at) FILTER (WHERE folds_succeeded > 0)
                         OVER (PARTITION BY cycle_type) AS last_success
              FROM consolidation_runs
            )
            SELECT cycle_type,
              max(last_success) AS last_success,
              (array_agg(outcome ORDER BY started_at DESC))[1] AS last_outcome,
              EXTRACT(EPOCH FROM now() - max(last_success))::int AS last_success_age,
              -- Per-type timing + throughput (decision: price each cycle type
              -- separately). The whole-cycle timer is skewed by slot contention
              -- and cannot honestly price either daemon's slot cost, so average
              -- only COMPLETED runs and bound the window to 24h — an all-history
              -- mean would be dominated by long-dead configurations.
              max(started_at) AS last_started,
              avg(EXTRACT(EPOCH FROM finished_at - started_at))
                  FILTER (WHERE outcome = 'completed' AND finished_at IS NOT NULL
                          AND started_at > now() - interval '24 hours') AS cycle_seconds_avg,
              -- runs_24h counts runs of the cycle BODY. A 'deferred' row (the
              -- cycle was due and skipped) and an 'idle' row (the gate ran and
              -- found nothing eligible) are zero-duration records of a
              -- NON-run: counting them here would inflate the rate that sits
              -- beside cycle_seconds_avg and misprice the cycle. They are
              -- reported on their own keys instead.
              count(*) FILTER (WHERE started_at > now() - interval '24 hours'
                  AND outcome IS DISTINCT FROM 'deferred'
                  AND outcome IS DISTINCT FROM 'idle') AS runs_24h,
              count(*) FILTER (WHERE started_at > now() - interval '24 hours'
                  AND outcome = 'deferred') AS deferred_24h,
              count(*) FILTER (WHERE started_at > now() - interval '24 hours'
                  AND outcome = 'idle') AS idle_24h,
              sum(folds_succeeded) FILTER (WHERE started_at > now() - interval '24 hours')
                  AS folds_succeeded_24h,
              sum(folds_attempted) FILTER (WHERE started_at > now() - interval '24 hours')
                  AS folds_attempted_24h,
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
        started_at: dict = {}
        for ct in CONSOLIDATION_CYCLE_TYPES:
            r = by_type.get(ct)
            age = r["last_success_age"] if r else None
            in_flight = bool(r["inflight"]) if r else False
            elig = r["eligible_clusters"] if r else None
            # Backlog must match the gate the cycle ACTUALLY folds on (see
            # _consolidation_backlog): the recorded eligible_clusters, not nrem.
            started_at[ct] = r["last_started"] if r else None
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
                # Per-type cost + throughput. cycle_seconds_avg is the mean of
                # COMPLETED runs in the last 24h — the per-type price a slot
                # allocator needs; the whole-cycle timer cannot supply it.
                "cycle_seconds_avg": (
                    round(float(r["cycle_seconds_avg"]), 1)
                    if r and r["cycle_seconds_avg"] is not None else None),
                "runs_24h": int(r["runs_24h"]) if r and r["runs_24h"] is not None else 0,
                # Non-runs, reported separately so runs_24h stays a price the
                # slot allocator can divide by: the cycle was due and skipped
                # (deferred), or its gate ran and found nothing (idle).
                "deferred_24h": (
                    int(r["deferred_24h"]) if r and r["deferred_24h"] is not None else 0),
                "idle_24h": int(r["idle_24h"]) if r and r["idle_24h"] is not None else 0,
                "folds_succeeded_24h": (
                    int(r["folds_succeeded_24h"])
                    if r and r["folds_succeeded_24h"] is not None else 0),
                "folds_attempted_24h": (
                    int(r["folds_attempted_24h"])
                    if r and r["folds_attempted_24h"] is not None else 0),
                "last_started": (
                    r["last_started"].isoformat()
                    if r and r["last_started"] is not None else None),
            }
        out.update(_consolidation_rollup(out, any_stalled, started_at))
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
                # Graph integrity rides the same background refresh (decision
                # 928) so /health stays a cached read and never queries Neo4j
                # per request. Only the COUNT is carried here — the by_reason /
                # by_label breakdown stays on /memory/telemetry, which is
                # auth-scoped and already heavier.
                try:
                    integrity = await self._graph_integrity()
                    invalid_nodes = integrity["invalid_nodes"]
                except Exception:
                    # Never let an integrity probe blank the whole snapshot and
                    # report the system as unknown — the same tolerance the
                    # roll-up keys get below.
                    invalid_nodes = None
                self._consolidation_health = {
                    "stalled": full["stalled"],
                    "graph_invalid_nodes": invalid_nodes,
                    "last_outcome": full["last_outcome"],
                    "last_success_age_seconds": full["last_success_age_seconds"],
                    # Which type the headline age belongs to, and who is
                    # actually stalled — without these two the compact /health
                    # snapshot cannot be read correctly when the types disagree.
                    # Read tolerantly: a missing roll-up key must degrade this
                    # one field, never abort the refresh and blank the whole
                    # snapshot (which would report the system as unknown).
                    "last_success_cycle_type": full.get("last_success_cycle_type"),
                    "stalled_types": full.get("stalled_types", []),
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
                    # `scope` is excluded on purpose — it is an access-control axis,
                    # not a topical one; see consolidation_loop's domain-map note.
                    # This must mirror that chain exactly or the eligibility count
                    # reported here diverges from what the daemon actually folds.
                    "SELECT id, COALESCE(metadata->>'project', metadata->>'domain',"
                    " $2) AS domain FROM technical_docs WHERE id = ANY($1)",
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
                "SELECT COALESCE(metadata->>'project', metadata->>'domain', 'general') AS key,"
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
          orphan_entities  — TRULY dangling: no relationship of ANY kind (degree 0).
              NOT "no live-fact MENTIONS" — an entity reached only by typed edges
              (UNDER_CONDITIONS / PRODUCES_INSIGHT / CONSIDERED / REJECTED / ALIASES)
              from REM enrichment is legitimately connected, not an orphan. Counting
              only MENTIONS overstated this ~500x on our graph (see below).
          unmentioned_entities — has edges but no non-superseded fact/decision MENTIONS
              (mostly REM-typed-edge targets). A coverage/fragmentation proxy, NOT
              dead refs.
          singleton_entities — mentioned by exactly one live fact (fragmentation proxy)
          genuinely_referenced_entities — entities meeting ontology.py's
              GENUINELY_REFERENCED_ENTITY_RULE (>=1 non-superseded MENTIONS edge,
              decision 890): the population alias/duplicate-resolution work should
              be measured against, NOT entities_total — entities_total also
              includes Decision provenance-text nodes (CONSIDERED/REJECTED/
              UNDER_CONDITIONS/PRODUCES_INSIGHT targets), which alias coverage %
              was previously silently diluted by (~54% of entities_total on this
              graph, live-measured 2026-07-22). Kept as a separate field rather
              than redefining entities_total, which other consumers may depend on.
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
                f"WITH e, count(n) AS mentions "
                f"RETURN count(e) AS total, "
                f"  sum(CASE WHEN NOT (e)--() THEN 1 ELSE 0 END) AS orphans, "
                f"  sum(CASE WHEN mentions = 0 AND (e)--() THEN 1 ELSE 0 END) AS unmentioned, "
                f"  sum(CASE WHEN mentions = 1 THEN 1 ELSE 0 END) AS singletons, "
                f"  sum(CASE WHEN mentions >= 1 THEN 1 ELSE 0 END) AS genuinely_referenced"
            )).single()
            aliases = await (await session.run(
                f"MATCH ()-[r:{self._ALIAS_REL}]-() RETURN count(DISTINCT r) AS edges"
            )).single()
            covered = await (await session.run(
                f"MATCH (e:{ONT.entity})-[:{self._ALIAS_REL}]-() RETURN count(DISTINCT e) AS c"
            )).single()
            # Alias-component distribution (gds.wcc stamps Entity.alias_component;
            # singletons get their own id, so a group is a component of size > 1).
            comp = await (await session.run(
                f"MATCH (e:{ONT.entity}) WHERE e.alias_component IS NOT NULL "
                f"WITH e.alias_component AS c, count(*) AS sz WHERE sz > 1 "
                f"RETURN count(*) AS groups, coalesce(max(sz), 0) AS largest"
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
            "unmentioned_entities": deg["unmentioned"] or 0,
            "singleton_entities": deg["singletons"] or 0,
            "genuinely_referenced_entities": deg["genuinely_referenced"] or 0,
            "alias_edges": aliases["edges"] or 0,
            "alias_covered_entities": covered["c"] or 0,
            "alias_components": comp["groups"] or 0,
            "largest_alias_component": comp["largest"] or 0,
            "top_hubs": [{"name": h["name"], "degree": h["degree"]} for h in hubs],
        }

    @staticmethod
    def _compliance_split(counts: dict[str, int], known: frozenset[str]) -> tuple[str, list[dict]]:
        """Partition a {name: count} distribution against the ontology vocabulary.

        Returns (status, invalid) where status is "ok" when every name is known
        and "non-compliant" otherwise; invalid lists the offending names with
        counts, highest first. Pure — unit-testable without a graph.
        """
        invalid = [{"name": n, "count": c} for n, c in counts.items() if n not in known]
        invalid.sort(key=lambda d: (-d["count"], d["name"]))
        return ("ok" if not invalid else "non-compliant"), invalid

    async def _graph_compliance(self) -> dict:
        """Schema-compliance telemetry: which node labels and relationship types
        in the live graph fall outside the ontology vocabulary. Two cheap Neo4j
        aggregates; the valid/invalid split is computed in-process so the rule
        (KNOWN_LABELS / KNOWN_RELATIONSHIPS) can never drift from what the daemons
        write. Surfaces legacy/foreign drift (e.g. a DockerContainer node or a
        REQUIRES edge from a pre-gate experiment) that entity-shape metrics miss.
        """
        async with self._neo4j.session() as session:
            preds = await (await session.run(
                "MATCH ()-[r]->() RETURN type(r) AS name, count(*) AS c"
            )).data()
            labels = await (await session.run(
                "MATCH (n) UNWIND labels(n) AS l RETURN l AS name, count(*) AS c"
            )).data()
        pred_dist = {r["name"]: r["c"] for r in preds}
        label_dist = {r["name"]: r["c"] for r in labels}
        rel_status, invalid_rels = self._compliance_split(pred_dist, KNOWN_RELATIONSHIPS)
        lbl_status, invalid_lbls = self._compliance_split(label_dist, KNOWN_LABELS)
        return {
            "predicate_distribution": dict(
                sorted(pred_dist.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "label_compliance": lbl_status,
            "invalid_labels": invalid_lbls,
            "relationship_compliance": rel_status,
            "invalid_relationships": invalid_rels,
        }

    async def _graph_integrity(self) -> dict:
        """Graph-integrity telemetry (decision 928): nodes REM retired because
        their LABEL contradicted the record their pg_id names.

        REM already detects this precisely — it retires the node, records
        `rem_invalid_reason` on it, and logs a WARNING — but nothing consumed
        that verdict, so the only trace was a log line scrolling past and a
        property nobody queried. Three separate write-path defects were each
        diagnosed correctly here and still found only by a hand-written Cypher
        hunt weeks later. This makes the existing signal visible; it adds no
        detection of its own.

        Read it as a WRITE-PATH defect, never as a retryable record failure: a
        mismatch means some writer produced a node under the wrong label, so the
        fix is to correct that writer and repair the node. `invalid_nodes` is
        expected to be 0 — any non-zero value is a standing defect, not a queue
        depth that drains on its own.
        """
        async with self._neo4j.session() as session:
            rows = await (await session.run(
                "MATCH (n) WHERE coalesce(n.rem_invalid, false) = true "
                "RETURN head(labels(n)) AS label, "
                "       n.rem_invalid_reason AS reason, "
                "       count(*) AS c"
            )).data()
        total = sum(r["c"] for r in rows)

        def _rollup(key: str, fallback: str) -> dict:
            # SUM per key — the query groups by (label, reason), so one label
            # spans several reasons and vice versa. Building the dict directly
            # from the rows would let a later row overwrite an earlier one and
            # silently UNDER-report the defect, which is the one direction an
            # integrity metric must never fail in.
            acc: dict[str, int] = {}
            for r in rows:
                acc[r[key] or fallback] = acc.get(r[key] or fallback, 0) + r["c"]
            return dict(sorted(acc.items(), key=lambda kv: (-kv[1], kv[0])))

        return {
            "invalid_nodes": total,
            # Grouped so the shape of the defect is legible without a follow-up
            # query: which label got written, and what it should have been.
            "by_reason": _rollup("reason", "unspecified"),
            "by_label": _rollup("label", "unlabelled"),
            "clean": total == 0,
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
    app.router.add_post("/memory/relations/review", coordinator.handle_relations_review)
    app.router.add_post("/memory/relations/label",  coordinator.handle_relations_label)
    app.router.add_get( "/memory/status/{pg_id}", coordinator.handle_status)
    app.router.add_get( "/memory/telemetry",       coordinator.handle_telemetry)
    app.router.add_post("/admin/backup",           coordinator.handle_backup)
