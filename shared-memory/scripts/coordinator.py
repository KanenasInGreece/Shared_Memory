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
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import pwd
import random
import re
import socket
import struct
import time
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
    GROUNDING_ROLES, GROUNDING_RELATIONS, default_grounding_role, RETRO_RATINGS,
    record_label_for_type,
)
from project_axis import (
    PROJECT_SQL, PROJECT_EXISTS_SQL, PROJECT_ID_SQL, PROJECT_PROPOSALS_SQL,
    PROPOSAL_SIMILARITY, PROPOSAL_LIMIT, SENTINEL,
    CONFUSABLE_SQL, CONFUSABLE_SIMILARITY, PROJECT_NAMES_SQL,
    same_spelling, spelling_variant_of, unconfirmed_confusables,
    fold_eligible, resolve_project, project_for_graph, project_merge_cypher,
)
from domain_axis import (
    DOMAIN_EXISTS_SQL, DOMAIN_PROPOSALS_SQL, DOMAIN_PROPOSAL_SIMILARITY,
    DOMAIN_PROPOSAL_LIMIT, DOMAIN_CONFUSABLE_SQL, DOMAIN_CONFUSABLE_SIMILARITY,
    DOMAIN_ALIAS_RESOLVE_SQL, DOMAIN_REGISTER_SQL, DOMAIN_KEYS,
    DOMAIN_NAMES_SQL,
    domain_merge_cypher, names_a_domain, resolve_domains,
)
from insight_gate import walk_group_reached_set, passes_insight_gate
from project_promotion import (
    promote_record, sole_project, METHOD_GROUNDING,
)
from project_alias import ALIAS_RESOLVE_SQL
from secure_env import get_secret

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


def _short(value: Any, cap: int = 200) -> str:
    """repr(value), truncated so a caller-supplied string can never blow up a
    400 response body. Every 400 error message that echoes a request value
    (a project/domain name, an entities_provenance key, a `since` filter, …)
    must route through this rather than interpolating the raw value — an
    unbounded echo turns a validation error into an amplification vector and,
    against a large-enough payload, a proto-DoS.

    Pure and total: any input that `repr()` accepts is safe here, including
    non-strings (ints, None, dicts) passed through the same validators.
    """
    text = repr(value)
    if len(text) <= cap:
        return text
    return text[:cap] + "…[truncated]"


# ── Version contract ────────────────────────────────────────────────────────────
# FRAMEWORK_VERSION is the informational build/semver — it changes every release.
# API_VERSION is the wire contract between memory_bridge.py (the thin client that
# ships with the skill) and this coordinator. Bump it ONLY when the request or
# response shape, auth scheme, or routes change in a way that breaks older clients.
# Client and server build-versions are allowed to drift; their API_VERSION must agree.
FRAMEWORK_VERSION = "0.9.17"
# v2 (retro-as-record): /memory/retrospective now creates a full record (own
# pg_id, embedding, Retrospective node) and accepts rating enum + grounding —
# the response shape changed (returns the retro's own pg_id).
# v3 (relation calibration): new operator-facing routes /memory/relations/review
# and /memory/relations/label (the review-edges / label-edges client commands
# require them) — the operator is the calibration oracle for machine-minted
# relation edges (decisions 726/727).
# v4 (project registry): a fact save without a REGISTERED metadata.project is
# rejected 400 carrying error=project_required|project_unknown plus near-match
# proposals. BREAKING for any client that saved untagged facts. The second
# submission is accepted in three forms: a proposal, new_project=true, or the
# reserved sentinel general_discussion.
API_VERSION = 4
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
    agent = _lookup_agent_by_token(token[1]) if len(token) == 2 else None
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


# Plaintext AGENT_TOKENS entries are REFUSED outright, as of v0.9.3 (RULED,
# Xenofon, 2026-08-14, superseding this PR's own original accept+warn draft —
# there is no deprecation window). A plaintext entry is a standing downgrade
# vector: a stale on-disk registry that still verifies exactly as strong as
# a live one is not a convenience worth keeping, even temporarily. Parsing
# still ACCEPTS the shape below (so the refusal can name exactly which
# agents need converting) — the refusal itself is a startup-time check,
# `require_no_plaintext_agent_tokens()`, called ONLY from
# hive_mind_proxy.main() (the real gateway entrypoint), never at bare
# import time — merely importing coordinator.py (every test in this repo
# does) must not crash on a plaintext-configured checkout.
_PLAINTEXT_AGENT_TOKENS_SEEN: list[str] = []

# Digest-form AGENT_TOKENS entries: name:sha256:<64-hex-char digest>.
_DIGEST_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _token_digest(token: str) -> str:
    """SHA-256 hex digest of a bearer token — the only form ever stored in
    _AGENT_TOKENS or compared against a presented credential (SEC-07)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_agent_tokens() -> dict[str, str]:
    """Parse AGENT_TOKENS env var into a digest(sha256 hex) → agent_name mapping.

    Two accepted entry shapes, comma-separated:
      - digest form (required from v0.9.3): name:sha256:<64-hex-digest>
      - legacy plaintext:                   name:token  (token MAY itself
        contain one or more colons — everything after the first colon is
        the token, unless it takes the digest shape above)

    A legacy plaintext entry is hashed once, here, at load time and stored
    exactly like a digest entry — from this point on in the process, the raw
    token value is retained nowhere. Parsing still ACCEPTS a plaintext entry
    (so the gateway can name it precisely when refusing to start — see
    require_no_plaintext_agent_tokens() below); every plaintext agent name
    seen is recorded in _PLAINTEXT_AGENT_TOKENS_SEEN. `generate_tokens.py
    --convert-digests` rewrites an existing gateway .env's plaintext entries
    to digest form in place, in one command.

    Returns empty dict if AGENT_TOKENS is not set (auth disabled, backward
    compat) — unchanged by the format change.

    Read via secure_env.get_secret(), never os.environ directly (SEC-05/
    SEC-09, PR A1) — AGENT_TOKENS is a secret key, so hive_mind_proxy's split
    loader never exports it to os.environ; get_secret() still falls back to
    os.environ for a value set through the process's own exec-time
    environment (a test's monkeypatch.setenv, an operator's `export`).
    """
    raw = get_secret("AGENT_TOKENS", "").strip()
    _PLAINTEXT_AGENT_TOKENS_SEEN.clear()
    if not raw:
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(":", 2)
        if len(parts) == 3 and parts[1].strip().lower() == "sha256":
            name   = parts[0].strip()
            digest = parts[2].strip().lower()
            if not name or not _DIGEST_HEX_RE.match(digest):
                log.warning(
                    "AGENT_TOKENS: malformed digest entry %r (expected "
                    "name:sha256:<64-hex-digest>)", pair,
                )
                continue
        elif len(parts) == 3:
            # Finding 6 (A2 security review): a 3-part split whose middle
            # segment ISN'T "sha256" is not malformed -- split(":", 2) only
            # ever produces 3 parts when the raw value has 2+ colons, which
            # a PLAINTEXT token containing a colon of its own triggers just
            # as easily as a genuine digest entry. Falling through to
            # "malformed, drop it" here used to mean this entry never
            # reached _PLAINTEXT_AGENT_TOKENS_SEEN, so
            # require_no_plaintext_agent_tokens() never named it and a
            # gateway with this entry started up "clean" while the agent's
            # real token silently verified nothing -- an unexplained 401
            # instead of the one-line startup refusal. Reassemble the
            # colon(s) the split consumed: the token is everything after
            # the FIRST colon, verbatim.
            name  = parts[0].strip()
            token = f"{parts[1]}:{parts[2]}".strip()
            if not name or not token:
                log.warning("AGENT_TOKENS: malformed entry %r (expected name:token)", pair)
                continue
            digest = _token_digest(token)
            _PLAINTEXT_AGENT_TOKENS_SEEN.append(name)
        elif len(parts) == 2:
            name, token = parts[0].strip(), parts[1].strip()
            if not name or not token:
                log.warning("AGENT_TOKENS: malformed entry %r (expected name:token)", pair)
                continue
            digest = _token_digest(token)
            _PLAINTEXT_AGENT_TOKENS_SEEN.append(name)
        else:
            log.warning(
                "AGENT_TOKENS: malformed entry %r (expected name:token or "
                "name:sha256:<hex>)", pair,
            )
            continue
        if digest in result:
            log.warning(
                "AGENT_TOKENS: digest for %r collides with an existing entry "
                "already assigned to %r — ignoring duplicate; fix .env to "
                "prevent misattribution", name, result[digest],
            )
            continue
        result[digest] = name
    return result


_AGENT_TOKENS: dict[str, str] = _load_agent_tokens()

# Captured ONCE, immediately after the line above, before any daemon token
# has ever been minted (CRITICAL fix, A2 security review, finding 1). The
# auth middleware's backward-compat bypass and /health's `auth_required`
# MUST gate on this flag, never on `bool(_AGENT_TOKENS)` at request time:
# hive_mind_proxy._mint_daemon_token() mutates this same dict in place, a
# few seconds after boot, when the REM/NREM watchdogs first spawn their
# daemons (SEC-10, PR A2) -- registering an ephemeral entry even on an
# install that never configured AGENT_TOKENS at all. A bypass keyed on live
# emptiness would therefore flip an auth-unset install to "authenticating"
# moments after startup, 401-ing every unauthenticated client -- exactly
# the seamlessness invariant ("clients: zero action required") this
# workstream is not allowed to break. This flag never changes for the life
# of the process; hive_mind_proxy skips minting entirely when it is False
# (see _daemon_env_and_token_fd()), so _AGENT_TOKENS in fact stays empty
# too in that case -- but the gate reads this flag explicitly rather than
# relying on that as an invariant to keep proving forever.
AUTH_CONFIGURED_AT_STARTUP: bool = bool(_AGENT_TOKENS)


def require_no_plaintext_agent_tokens() -> None:
    """FATAL, one line, naming the fix (RULED, Xenofon, 2026-08-14): from
    v0.9.3 the gateway refuses to start when AGENT_TOKENS carries even one
    legacy plaintext entry. Call this from hive_mind_proxy.main() ONLY —
    the actual gateway entrypoint — never at bare import time, matching
    secure_env.require_db_credentials()'s established pattern: every test
    in this repo imports coordinator.py, and many do so against a
    plaintext-configured AGENT_TOKENS on purpose (test_auth.py's whole
    suite), so an unconditional check here would kill test collection
    itself, not just a genuinely misconfigured gateway.
    """
    if _PLAINTEXT_AGENT_TOKENS_SEEN:
        names = ", ".join(sorted(_PLAINTEXT_AGENT_TOKENS_SEEN))
        raise SystemExit(
            f"FATAL: AGENT_TOKENS has plaintext entries for: {names} — plaintext "
            "tokens are refused as of v0.9.3. Convert with: uv run python "
            "shared-memory/scripts/generate_tokens.py --convert-digests"
        )


def _lookup_agent_by_token(token: str) -> "str | None":
    """Resolve a presented bearer token to its registered agent name.

    Hashes the presented token FIRST (SEC-07): the only thing ever compared
    against a stored value is the token's own SHA-256 digest, never the
    token itself — an attacker who can only observe response timing cannot
    steer a byte-by-byte comparison against a secret, because no code path
    here performs one. `hmac.compare_digest` is used for the digest
    comparison itself too (belt-and-braces: every place a value derived from
    the presented token is compared against a stored one uses the
    constant-time primitive, not `==`, even though the digest is not itself
    secret).

    Includes ephemeral, in-memory-only daemon tokens (SEC-10, PR A2) — they
    are registered into this same dict by hive_mind_proxy._mint_daemon_token()
    and look, to this function, exactly like any other registry entry.
    """
    digest = _token_digest(token)
    for stored_digest, name in _AGENT_TOKENS.items():
        if hmac.compare_digest(digest, stored_digest):
            return name
    return None


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


def _extract_bearer_token(request: web.Request) -> str | None:
    """Return the raw ``Authorization: Bearer <token>`` value, or None if no
    such header/scheme is present — regardless of whether the token verifies.
    Shared by identity resolution and, on a verify failure, by the RFC 6750
    WWW-Authenticate choice + the credential-audit digest (PR A3): a token
    that was PRESENTED but rejected gets ``error="invalid_token"`` and a
    digest_prefix; no token at all gets the bare challenge and no digest."""
    parts = request.headers.get("Authorization", "").split(maxsplit=1)
    if len(parts) != 2 or parts[0] != "Bearer":
        return None
    return parts[1]


def _resolve_bearer(request: web.Request) -> str | None:
    """Map ``Authorization: Bearer <token>`` to a verified agent name, or None."""
    token = _extract_bearer_token(request)
    return _lookup_agent_by_token(token) if token is not None else None


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


def _normalise_decided_by(metadata: dict[str, Any]) -> bool:
    """Canonicalise a decision's person axis onto the KERNEL-ATTESTED principal.

    `decided_by` is free text an agent types, so it drifts: the same operator has
    arrived as a case variant, and as a compound naming the AI that helped
    ("<operator> + <agent>") or naming only the agent. Each spelling mints its own
    :Human node and splits that person across Tier-3 provenance, so a summary
    sourced from one operator's decisions can report several.

    The OS account behind the socket is already the normal form — server-owned,
    unforgeable, and identical for every write that operator makes. So when a
    principal exists it BECOMES `decided_by`, and the original wording is kept as
    `decided_by_claimed` whenever it differed, losing nothing: the attested axis is
    what provenance joins on, the claim is what the operator said.

    This deliberately does NOT parse compounds. Splitting "<operator> + <agent>"
    would need heuristics about which component is a person, and the principal
    already answers that without guessing — the AI's contribution belongs in
    `assisted_by`, which the caller sets directly.

    With no principal (TCP transport carries no kernel credential) the claim is
    left exactly as given — honestly unknown, never guessed, matching
    _apply_principal. Returns True when the stored value changed.
    """
    if not isinstance(metadata, dict) or metadata.get("type") != "decision":
        return False
    principal = metadata.get("principal")
    decision  = metadata.get("decision")
    if not principal or not isinstance(principal, str) or not isinstance(decision, dict):
        return False
    claimed = decision.get("decided_by")
    claimed = claimed.strip() if isinstance(claimed, str) else ""
    if claimed == principal:
        return False
    if claimed:
        decision["decided_by_claimed"] = claimed
    decision["decided_by"] = principal
    return True


def _supersession_target_error(pg_id: int, record_type: object) -> str | None:
    """Reject supersession of a JUDGEMENT record. Returns an error message, or
    None when the target may be superseded.

    Supersession is the FACT lifecycle: a fact is a claim about the world, and
    when the world changes the claim is retracted and replaced. A judgement is
    not a claim about the world — it is a dated act by a person, and the record
    that it turned out wrong is a RETROSPECTIVE, not a retraction. Overturning a
    decision therefore goes through `rating='reversed'`, which marks the decision
    superseded as a CONSEQUENCE of a verdict that stays in the graph, leaving the
    lineage a successor can ground on. Retracting it directly would delete the
    reasoning instead of recording that it was overturned.

    A retrospective is refused for the mirror-image reason: it is an observation
    dated to when it was made, so a changed outcome is a NEW retrospective, not
    an edit of the old one. Entity inheritance already prefers the latest live
    verdict, so nothing needs retracting for the newer judgement to take effect.

    Both refusals also close a real corruption: the supersede mirror MERGEs its
    target as a :Fact by pg_id, so superseding a decision or retrospective minted
    a phantom :Fact node carrying that id while the real node stayed unmarked.
    """
    kind = (record_type or "fact").strip().lower() if isinstance(record_type, str) else "fact"
    if kind == "decision":
        return (
            f"record {pg_id} is a decision and cannot be superseded directly — "
            "save a retrospective with rating='reversed' against it instead. That "
            "records WHY it was overturned, marks the decision superseded, and "
            "leaves a verdict a later decision can ground on."
        )
    if kind == "retrospective":
        return (
            f"record {pg_id} is a retrospective and cannot be superseded — a "
            "retrospective is dated to when it was made. Save a NEW retrospective "
            "against the same decision; the newer verdict is the one that counts."
        )
    return None


# entities_provenance (fact:1215): who named each entity — "operator" (an
# explicit, human-chosen concept) or "agent" (proposed without that
# confirmation). Closed enum; a value outside it is a shape error, not a new
# spelling to accommodate.
ENTITIES_PROVENANCE_VALUES = ("operator", "agent")


# ── Entity vocabulary ingress (fact:1375, migration 033) ──────────────────────
#
# The two SQL primitives the save-time entity gate needs (Coordinator methods
# `_entity_vocab_resolve`/`_entity_vocab_mint`, near `_project_ingress_error`).
# Both call the migration's own `entity_normalize()` rather than reimplementing
# it in Python — the ONE normalization definition every reader/writer of this
# vocabulary must share (033's own comment on the function).
#
# Resolve: canonical match first, alias match second, COALESCEd into one
# column — NULL when neither table knows the name. LIMIT 1 caps the (harmless,
# same-canonical) duplicate rows Option B's alias table can produce when one
# entity has two verbatim aliases that happen to normalize to the same key.
ENTITY_VOCAB_RESOLVE_SQL = """
    SELECT COALESCE(canon.name, alias_canon.name) AS canonical_name
      FROM (SELECT entity_normalize($1::text) AS norm) n
      LEFT JOIN entity_vocabulary canon
        ON canon.normalized_key = n.norm
      LEFT JOIN entity_vocab_aliases a
        ON a.normalized_alias = n.norm
      LEFT JOIN entity_vocabulary alias_canon
        ON alias_canon.id = a.entity_id
     LIMIT 1
"""

# Mint: the ONLY INSERT this gate ever issues, and only into entity_vocabulary
# — never entity_vocab_aliases (rule 5; alias curation stays a manual,
# operator-only act, decision:1380). ON CONFLICT (normalized_key) DO NOTHING
# lets Postgres's own unique index arbitrate a same-table race between two
# concurrent mints (exactly what the migration's seed relies on for its own
# idempotency — "ON CONFLICT arbitrates"); RETURNING comes back empty exactly
# when that happened, which is the caller's signal to re-resolve rather than
# assume its own mint won.
ENTITY_VOCAB_MINT_SQL = """
    INSERT INTO entity_vocabulary (name, registered_by)
    VALUES ($1, $2)
    ON CONFLICT (normalized_key) DO NOTHING
    RETURNING id, name
"""


def save_response_warning(record_type: object, entities, grounded_in) -> str:
    """The save response's advisory suffix — WHICH omission leaves this record
    unreachable by synthesis, stated per record type.

    "Unreachable" means something different for each type, so one message
    cannot serve both. A DECISION mints nothing by design — since v0.8.26 it
    inherits its topics by walking to the facts it rests on — so warning it
    about `entities` fires on every decision saved exactly as instructed and
    teaches the operator the opposite of the shipped rule. The client-side
    twin of this message was already made type-aware; this is the server half
    of the same edit.

    A decision that rests on no fact is NOT an error. The greenfield case is
    real and supported: a project with no facts yet, where the operator decides
    on experience — which is also why a decision may ground on another decision.
    But it is UNUSUAL, and the only thing that makes it legible later is the
    retrospective that eventually measures it, whose facts the decision then
    inherits across HAD_OUTCOME. So the note says exactly that, and does not
    pretend the record is broken.

    ⛔ A FACT with no entities is NOT what this used to say. Before fact:1215,
    consolidation gated on the (entity, project) cluster key, so an empty
    `entities` really did mean "never reaches Tier 3" — that was true when this
    message was written. It no longer is: the fold now walks the
    DOMAIN_OF→PROJECT_OF spine (project+domain), not an entity level, so an
    entity-less fact is fully consolidatable. `entities` still matters — it is
    the only way a new concept enters the graph, and it feeds graph navigation
    and REM/entity-relation linking — but Tier 3 eligibility is not one of the
    things it buys. Saying otherwise trains the operator to add entities for a
    reason that no longer holds, which is a worse outcome than an honest note.

    Retrospectives never reach this function: grounding is REQUIRED of them at
    ingress (an ungrounded verdict measures nothing), so the omission is a 400,
    not a warning. Returns "" when nothing is missing.
    """
    kind = record_type.strip().lower() if isinstance(record_type, str) else "fact"
    if kind == "decision":
        if grounded_in:
            return ""
        return (
            " NOTE: this decision rests on no fact — unusual, and valid only when"
            " meant (a call made on experience before the project has evidence)."
            " It inherits its topics from the facts of the retrospective that"
            " later measures it, so that retrospective is what makes it legible."
        )
    if kind == "retrospective":
        return ""
    if entities:
        return ""
    return (
        " NOTE: no entities — fine for Tier 3 consolidation (the fold keys on"
        " project+domain, not entities); entities feed graph navigation and"
        " REM/entity-relation linking only."
    )


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
# Checked FIRST in auth_middleware, ahead of every auth exemption (S-11).
# SEC-A5-05a (PR A5 fix round): incremented/decremented uniformly around
# EVERY admitted request — auth-disabled bypass, unprotected-path exemption,
# and the authenticated dispatch alike — so an anonymous /health/pool-status
# flood, or (on an auth-off install) any traffic at all, actually counts
# toward the cap it can also be shed by. A request the valve sheds was never
# admitted and never reaches the increment.
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
           principal: dict[str, Any] | None = None,
           backend: str | None = None, key_attached: bool = False) -> None:
    """Append one JSON line recording a completed request. Best-effort and OFF the
    DB hot path: it never touches Postgres (so audit volume can't steal the pool's
    connection budget) and a logging failure never surfaces into the request.
    No-op unless GATEWAY_AUDIT_LOG_PATH is set. The identity is the verified agent
    name — when PoP lands the same rows become non-repudiable with no schema change.

    The write goes through _audit_writer (an AsyncLineWriter): the line is enqueued
    O(1) and a background task does the disk append in an executor, so the write
    never blocks the event loop. Rotation/gzip is handled by logrotate(8).

    `backend`/`key_attached` (PR A3, additive — existing fields unchanged) are set
    only when the request was proxied to an LLM backend / a provider key was
    attached to it (hive_mind_proxy.handle_proxy stashes them on the request for
    this hook to read back — see request["backend"]/request["key_attached"]).
    Per-request USE auditing stops here; the credential-events log (a separate
    stream, see _write_credential_audit_line) carries only high-signal faults.
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
        if backend:
            record["backend"] = backend
        if key_attached:
            record["key_attached"] = True
        line = json.dumps(record, separators=(",", ":"))
        _audit_writer.write(line)
    except Exception as exc:  # never break a request because auditing failed
        log.warning("audit write failed: %s", exc)


# ── Credential-use audit trail (ISO 27001 A.5.17 property 4, PR A3) ─────────────
#
# Governing split: telemetry (below, served on GET /memory/telemetry) surfaces
# SIGNAL — operator-attention counters plus the last event's context. The
# credential-audit log (_credential_audit_writer, a SEPARATE stream from the
# per-request _audit_writer above) carries the DETAIL, and only for HIGH-SIGNAL
# events — never a mirror of request volume. Per-request use-auditing (every
# proxied call) stays in the existing gateway audit line via _audit()'s
# additive backend/key_attached fields; this log is for faults and credential
# lifecycle events only.
#
# Origin-ownership invariant: `gateway` = what the gateway itself decided or
# observed (routing, shed, connect/timeout, retries, own-door auth, key
# attach). `llm` = what the upstream SAID (status class, typed error body).
# A retried request counts per-attempt in `llm`, once in `gateway`. Nothing is
# ever counted in both groups for the same cause.
_llm_fault_counters: dict[str, dict] = {}
_credential_counters: dict[str, int] = {
    "token_verify_failed": 0,
    "daemon_tokens_issued": 0,
    # S-04 (PR A5): a request that would have carried a provider key but
    # whose method+path is not one of the framework's own endpoints —
    # see hive_mind_proxy.record_credentialed_route_denied.
    "credentialed_route_denied": 0,
}
# When each credential counter last moved. Counters answer "how many"; a
# consumer asking "is this still happening?" needs "when", and for these
# events there is nowhere else to get it: the no-token 401 is deliberately
# never logged (C-1), the rate-limited path emits its suppression summary
# only lazily, and both counters reset with the process — so a poll-delta
# INVERTS across a restart (the count drops to 0 and reads as "never
# failed" while a real failure was minutes ago). Stamped at the increment,
# beside the counter, so the pair can never disagree.
_credential_last_ts: dict[str, str | None] = {
    "token_verify_failed": None,
    "daemon_tokens_issued": None,
    "credentialed_route_denied": None,
}


def _fault_entry(backend: str) -> dict:
    """Lazily create (and return) the per-backend fault counter shape."""
    return _llm_fault_counters.setdefault(backend, {
        "gateway": {"count": 0, "last": None},
        "llm": {
            "credential": {"count": 0, "last": None},
            "transient":  {"count": 0, "last": None},
        },
    })


def _classify_llm_fault(status: int, error_type: str | None) -> str:
    """credential = 401/403 always, or 429 whose upstream body names OpenAI's
    error.code == 'insufficient_quota' (never-retry, fix-the-key class).
    Everything else — including an unparseable/foreign 429, and 5xx/529 — is
    transient (retry-with-backoff class): a false quiet beats a false alarm
    when the body can't be read."""
    if status in (401, 403):
        return "credential"
    if status == 429 and error_type == "insufficient_quota":
        return "credential"
    return "transient"


# Security review (2026-08-15, R-2/R-4): the parse boundary is where both
# bugs are fixed at once — refuse a chunk this large before touching
# json.loads (removes a synchronous parse of an attacker-sized buffer from
# the streaming hot path), and coerce+bound whatever the body claims its
# error code/type is before it can reach telemetry or a log line.
_ERROR_BODY_PARSE_CAP = 65536       # bytes — R-2
_ERROR_TYPE_LABEL_CAP  = 120         # chars — R-2/R-4, matches _short()'s spirit


def _bounded_error_label(value: Any) -> str | None:
    """Coerce an extracted error code/type to a bounded, plain string, or
    None. Deliberately NOT `_short()` (repr()-wrapping would quote a string
    value and break the exact `error_type == "insufficient_quota"` match
    `_classify_llm_fault` depends on) — this truncates the value's OWN text,
    never its repr. Only str/int/float are accepted (R-4): a dict/list-valued
    `code` — a hostile or malformed upstream body — becomes None rather than
    an object landing in telemetry or the audit log."""
    if not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    return text if len(text) <= _ERROR_TYPE_LABEL_CAP else text[:_ERROR_TYPE_LABEL_CAP] + "…[truncated]"


def _parse_upstream_error_type(body: bytes) -> str | None:
    """Best-effort extraction of the upstream error's own type/code label
    (OpenAI-compatible {"error": {"code"|"type": ...}} shape) — used for
    classification and for the telemetry/audit `error_type` field. Never
    raises: a foreign shape, a truncated peek of a chunked body, an
    oversized body (R-2), or a non-str/int/float value (R-4) all just yield
    None, which classifies as transient rather than guessing at a credential
    fault it can't actually name."""
    if len(body) > _ERROR_BODY_PARSE_CAP:
        return None
    try:
        payload = json.loads(body)
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return _bounded_error_label(err.get("code") or err.get("type"))
    except Exception:
        pass
    return None


_DECOMPRESS_PREFIX_CAP = 8192  # bytes of DECOMPRESSED output — bounds a hostile expansion ratio too

# Single source of truth for which Content-Encoding values either decompression
# helper below understands. Referenced by hive_mind_proxy.py's usage-capture
# gate too, so the two paths (fault-body peek, usage-body decompress) can never
# drift apart on what "supported" means.
SUPPORTED_CONTENT_ENCODINGS = {"gzip", "deflate", "br"}


def _decompress_prefix_for_parse(body: bytes, content_encoding: str | None) -> bytes:
    """Security review R-3: `auto_decompress=False` on the shared proxy
    session means a gzip/deflate/br-compressed upstream error body reaches
    `_parse_upstream_error_type` as framing bytes, `json.loads` fails, and
    the 429→insufficient_quota→credential rule silently degrades to
    transient for exactly the paid-provider case it exists for. Decompresses
    a BOUNDED prefix so the parser sees JSON instead — the ORIGINAL bytes
    handed to the client (the passthrough chunk itself) are never touched;
    this only changes what gets fed to the parser. Never raises: an
    unsupported/unknown encoding, a genuinely truncated prefix, or `br`
    without the optional `brotli` package installed all just return the
    body unchanged, which the parser already treats as unparseable → None →
    transient (no regression versus today)."""
    if not content_encoding:
        return body
    enc = content_encoding.strip().lower()
    raw = body[:_DECOMPRESS_PREFIX_CAP]
    try:
        if enc == "gzip":
            import gzip
            import io
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read(_DECOMPRESS_PREFIX_CAP)
        if enc == "deflate":
            import zlib
            try:
                return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw, _DECOMPRESS_PREFIX_CAP)
            except zlib.error:
                return zlib.decompressobj().decompress(raw, _DECOMPRESS_PREFIX_CAP)
        if enc == "br":
            import brotli  # optional dependency — not declared elsewhere in this repo
            return brotli.Decompressor().decompress(raw)
    except Exception:
        pass
    return body


# Ceiling on the DECOMPRESSED output of `_decompress_full_for_usage` below —
# the R-3 rationale on `_DECOMPRESS_PREFIX_CAP` above ("bounds a hostile
# expansion ratio too") applies to this path's peer identically. Default is
# 32× the compressed-side cap (2 MiB × 32 = 64 MiB): measured on real
# LLM-shaped JSON, gzip compresses ~2.9:1, so a max-cap body decompresses to
# ~6 MiB — this leaves >10× headroom over that while cutting the worst-case
# amplification from the measured 1028:1 down to 32:1.
LLM_USAGE_DECOMPRESS_CAP_BYTES = int(os.environ.get(
    "LLM_USAGE_DECOMPRESS_CAP_BYTES", str(64 * 1024 * 1024)))


def _decompress_full_for_usage(body: bytes, content_encoding: str) -> bytes:
    """Whole-body decompression for hive_mind_proxy.py's usage-capture path,
    which needs the COMPLETE trailing `usage` object rather than the bounded
    fault-body prefix `_decompress_prefix_for_parse` above peeks at. Supports
    exactly the same encodings as that function (see
    SUPPORTED_CONTENT_ENCODINGS — gzip/deflate/br, no new encodings added).
    LLM_USAGE_CAPTURE_CAP_BYTES bounds the COMPRESSED bytes the caller
    accumulates; LLM_USAGE_DECOMPRESS_CAP_BYTES bounds what this function
    will inflate them to (gzip/deflate never allocate past it; br is checked
    after the fact, matching the prefix helper's own unbounded br call).

    Unlike `_decompress_prefix_for_parse`, this RAISES on any failure
    (unsupported encoding, corrupt/truncated body, over-cap decompressed
    output, `brotli` not installed) instead of returning the input
    unchanged — the caller's usage capture is best-effort and abandons on
    any exception, so a silent pass-through here would hand compressed
    bytes to `json.loads` instead of just failing cleanly at the point the
    trouble actually occurred.
    """
    enc = content_encoding.strip().lower()
    cap = LLM_USAGE_DECOMPRESS_CAP_BYTES
    if enc == "gzip":
        import io
        import zlib
        # wbits=16+MAX_WBITS reads the gzip framing; decompressobj honours
        # max_length so an over-cap body never allocates past cap+1.
        out = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, cap + 1)
    elif enc == "deflate":
        import zlib
        try:
            out = zlib.decompressobj(-zlib.MAX_WBITS).decompress(body, cap + 1)
        except zlib.error:
            out = zlib.decompressobj().decompress(body, cap + 1)
    elif enc == "br":
        import brotli  # optional dependency — not declared elsewhere in this repo
        out = brotli.Decompressor().decompress(body)
    else:
        raise ValueError(f"unsupported content-encoding for usage capture: {content_encoding!r}")
    if len(out) > cap:
        raise ValueError(
            f"decompressed usage body exceeds LLM_USAGE_DECOMPRESS_CAP_BYTES ({cap})")
    return out


CREDENTIAL_AUDIT_LOG_PATH = os.environ.get(
    "CREDENTIAL_AUDIT_LOG_PATH", "~/.shared-memory/logs/credential-audit.jsonl",
)
# ON by default (unlike GATEWAY_AUDIT_LOG_PATH — see the "Config" section
# further below in this file) — credential-use auditing is a baseline
# control, not an opt-in diagnostic. Set the env var to an empty string to
# disable it explicitly. Rotation is already handled — the shipped
# logrotate config globs *-audit.jsonl, which this filename matches (see
# shared-memory/ops/README.md and shared-memory/.env.example).
_credential_audit_writer = (
    AsyncLineWriter(os.path.expanduser(CREDENTIAL_AUDIT_LOG_PATH))
    if CREDENTIAL_AUDIT_LOG_PATH.strip() else None
)

# Events an attacker fully controls the volume of — either fully
# unauthenticated (security review R-5) or, for credentialed_route_denied
# (SEC-A5-04, PR A5 fix round), any holder of a valid-but-leaked agent
# token in a loop. Their log lines use drop-NEWEST eviction (a flood can
# only evict itself) instead of the writer's default drop-oldest, which
# would otherwise let the flood evict the genuine lifecycle/compromise
# evidence (token_verify_failed, key_attached) queued before it started —
# exactly the audit trail this log exists to preserve.
_ATTACKER_TRIGGERABLE_EVENTS = frozenset({
    "token_verify_failed", "token_verify_failed_suppressed",
    "credentialed_route_denied",
})


def _write_credential_audit_line(event: str, *, origin: str, **fields) -> None:
    """Append one high-signal line to the credential-events log. Best-effort,
    off the DB hot path, and never lets a logging failure surface into the
    request — same contract as _audit() above. No-op when the writer is
    disabled (CREDENTIAL_AUDIT_LOG_PATH set to an empty string).

    `ts`/`event`/`origin` are applied AFTER `**fields` in the dict literal
    (security review N-3) so a caller-supplied field can never shadow a
    reserved key — Python dict-literal construction lets a later key win,
    which is what makes this safe rather than merely conventional."""
    if _credential_audit_writer is None:
        return
    try:
        record = {**fields, "ts": datetime.now(timezone.utc).isoformat(),
                  "event": event, "origin": origin}
        _credential_audit_writer.write(
            json.dumps(record, separators=(",", ":")),
            drop_newest_when_full=event in _ATTACKER_TRIGGERABLE_EVENTS,
        )
    except Exception as exc:  # never break a request because auditing failed
        log.warning("credential-audit write failed: %s", exc)


# Token-bucket rate limit for token_verify_failed LOG LINES (security review
# C-1): an unauthenticated caller controls this event's volume entirely — a
# no-token 401 in a loop, or a fresh random token every attempt — so the
# counter (credentials.token_verify_failed, unthrottled, the complete signal)
# and the LOG LINE (throttled, the detail) are deliberately decoupled.
# Continuous refill: capacity TOKEN_VERIFY_FAILED_LOG_RATE tokens,
# replenished evenly over TOKEN_VERIFY_FAILED_LOG_WINDOW seconds.
TOKEN_VERIFY_FAILED_LOG_RATE   = _env_int("TOKEN_VERIFY_FAILED_LOG_RATE", 60)     # burst / lines per window
TOKEN_VERIFY_FAILED_LOG_WINDOW = _env_float("TOKEN_VERIFY_FAILED_LOG_WINDOW", 60.0)  # seconds

_tvf_bucket_tokens: float = float(TOKEN_VERIFY_FAILED_LOG_RATE)
_tvf_bucket_last_refill: float = time.monotonic()
_tvf_suppressed_count: int = 0
_tvf_suppressed_since: float | None = None


def _tvf_rate_limit_allow() -> bool:
    """True if a token_verify_failed LOG LINE may be written right now,
    consuming one token from the bucket. False means suppress (the caller
    still bumps the counter — see _record_token_verify_failed)."""
    global _tvf_bucket_tokens, _tvf_bucket_last_refill
    now = time.monotonic()
    elapsed = max(0.0, now - _tvf_bucket_last_refill)
    _tvf_bucket_last_refill = now
    refill_rate = TOKEN_VERIFY_FAILED_LOG_RATE / TOKEN_VERIFY_FAILED_LOG_WINDOW if TOKEN_VERIFY_FAILED_LOG_WINDOW > 0 else 0.0
    _tvf_bucket_tokens = min(float(TOKEN_VERIFY_FAILED_LOG_RATE), _tvf_bucket_tokens + elapsed * refill_rate)
    if _tvf_bucket_tokens >= 1.0:
        _tvf_bucket_tokens -= 1.0
        return True
    return False


def _transport_kind(request: web.Request) -> str:
    """"uds" | "tcp" — the connection's OWN socket family, independent of
    whether SO_PEERCRED could actually be read (_peer_identity can fail
    closed on a UDS connection too). Used for O-3 attribution only; never
    gates behaviour."""
    transport = request.transport
    if transport is None:
        return "tcp"
    sock = transport.get_extra_info("socket")
    if sock is not None and getattr(sock, "family", None) == socket.AF_UNIX:
        return "uds"
    return "tcp"


def _record_token_verify_failed(request: web.Request, presented_token: str | None) -> None:
    """A client's bearer token failed to verify. The COUNTER
    (credentials.token_verify_failed) always increments — it is the
    complete, unthrottled signal. The LOG LINE is more selective:

    - No token presented at all (security review C-1): never logged. The
      record would be byte-identical every time
      (`{"event":"token_verify_failed","digest_prefix":null,...}`) and
      carries no information the counter doesn't already have — logging it
      is a zero-cost-to-the-attacker, zero-forensic-gain disk write, exactly
      the amplification the finding describes.
    - A token WAS presented: rate-limited (see _tvf_rate_limit_allow). When
      the bucket is empty the line is suppressed and counted internally;
      the next line that IS allowed is preceded by one
      `token_verify_failed_suppressed` summary line so the gap is visible
      without paying per-attempt disk cost.

    Surviving lines carry attribution (security review O-3): the kernel-
    attested peer identity when available (UDS only — None on TCP, same as
    `_audit`/`_peer_identity` everywhere else in this file), the request
    path, and the transport kind. `claimed_agent` is always None today: the
    bearer scheme carries no separate name claim to disagree with (unlike
    the planned PoP resolver, whose handshake will) — kept as an explicit
    field so that lands with no shape change. The presented token's own
    value NEVER appears (SEC-08); only the first 8 hex chars of its SHA-256
    digest, enough to correlate a repeat offender without recovering the
    secret."""
    global _tvf_suppressed_count, _tvf_suppressed_since
    _credential_counters["token_verify_failed"] += 1
    # Stamped before the C-1 early return, so the no-token class — the one
    # that never produces a log line — still carries a "when". This is the
    # single piece of information the byte-identical line would have added,
    # and it costs no disk write, so it does not re-open C-1's amplification
    # argument.
    _credential_last_ts["token_verify_failed"] = datetime.now(timezone.utc).isoformat()
    if presented_token is None:
        return
    if not _tvf_rate_limit_allow():
        if _tvf_suppressed_count == 0:
            _tvf_suppressed_since = time.monotonic()
        _tvf_suppressed_count += 1
        return
    if _tvf_suppressed_count:
        window_s = (round(time.monotonic() - _tvf_suppressed_since, 1)
                    if _tvf_suppressed_since is not None else None)
        _write_credential_audit_line(
            "token_verify_failed_suppressed", origin="gateway",
            count=_tvf_suppressed_count, window_s=window_s,
        )
        _tvf_suppressed_count = 0
        _tvf_suppressed_since = None

    fields: dict[str, Any] = {
        "claimed_agent": None,
        "digest_prefix": _token_digest(presented_token)[:8],
        "path": request.path,
        "transport": _transport_kind(request),
    }
    principal = _peer_identity(request)
    if principal:
        fields["principal"] = principal.get("user")
        fields["connected_from"] = {
            k: principal[k] for k in
            ("uid", "gid", "pid", "login_uid", "login_user", "session")
            if k in principal
        }
    _write_credential_audit_line("token_verify_failed", origin="gateway", **fields)


def record_daemon_token_issued(agent_name: str) -> None:
    """Bump the daemon-tokens-issued counter and log the mint — daemon name
    and timestamp only, never token material. Called by
    hive_mind_proxy._mint_daemon_token() (SEC-10, PR A2) on every mint,
    including re-mints on daemon respawn."""
    _credential_counters["daemon_tokens_issued"] += 1
    _credential_last_ts["daemon_tokens_issued"] = datetime.now(timezone.utc).isoformat()
    _write_credential_audit_line("daemon_token_issued", origin="gateway", daemon=agent_name)


def record_llm_gateway_fault(backend: str, error_class: str, *,
                              credentialed: bool = False,
                              request_id: str | None = None) -> None:
    """Record a gateway-origin failure on an LLM-pool call (connect/timeout/
    shed/proxy error — `error_class` is the exception's class name, a string,
    never the exception's own text). Telemetry counts every backend; the
    credential-audit log line is written only when the call was credentialed
    (a provider key was attached) — this log is about credential USE, and an
    uncredentialed local backend's connection hiccup isn't that."""
    entry = _fault_entry(backend)["gateway"]
    entry["count"] += 1
    entry["last"] = {"ts": datetime.now(timezone.utc).isoformat(), "class": error_class}
    if credentialed:
        extra = {"request_id": request_id} if request_id else {}
        _write_credential_audit_line("gateway_fault", origin="gateway",
                                      backend=backend, error_class=error_class, **extra)


def record_credentialed_route_denied(backend: str, method: str, path: str, *,
                                      agent_name: str | None = None,
                                      request_id: str | None = None) -> None:
    """S-04 (Credential_Custody_Plan, PR A5): a request bound for a
    credentialed backend (a provider key was about to be attached) whose
    method+path is not one of the framework's own endpoints —
    hive_mind_proxy.CREDENTIALED_BACKEND_ALLOWED_ROUTES. Counter + a
    credential-audit line carrying method/path/agent — never the key
    itself, which this rejection never even reaches (it fires before
    Authorization is attached)."""
    _credential_counters["credentialed_route_denied"] += 1
    _credential_last_ts["credentialed_route_denied"] = datetime.now(timezone.utc).isoformat()
    extra = {"request_id": request_id} if request_id else {}
    _write_credential_audit_line(
        "credentialed_route_denied", origin="gateway",
        backend=backend, method=method, path=path, agent=agent_name, **extra,
    )


def record_llm_upstream_fault(backend: str, status: int, error_type: str | None, *,
                               credentialed: bool = False,
                               request_id: str | None = None) -> str:
    """Record an upstream-origin fault (the backend itself returned a fault
    status) on an LLM-pool call; returns the classification ("credential" |
    "transient") so the caller can log alongside it if it wants to. Telemetry
    counts every backend regardless of credential status; the
    upstream_credential_fault audit line is written only for the credential
    class AND only on a credentialed call — it exists to answer "did a
    request using OUR provider key get rejected", not to mirror every 5xx."""
    cls = _classify_llm_fault(status, error_type)
    entry = _fault_entry(backend)["llm"][cls]
    entry["count"] += 1
    entry["last"] = {"ts": datetime.now(timezone.utc).isoformat(),
                      "status": status, "error_type": error_type}
    if cls == "credential" and credentialed:
        extra = {"request_id": request_id} if request_id else {}
        _write_credential_audit_line("upstream_credential_fault", origin="llm",
                                      backend=backend, status=status,
                                      error_type=error_type, **extra)
    return cls


def _llm_faults_snapshot() -> dict:
    """Read-only render of the in-process per-backend fault counters for
    GET /memory/telemetry. In-process only (reset on restart) — same
    contract as the existing _llm_routed counters this section mirrors.

    A full `copy.deepcopy` (security review N-5): a shallow copy shares the
    nested `last` dict by reference, which makes the docstring's "read-only"
    claim false — a caller mutating the returned structure would corrupt
    live counter state. Harmless today (the caller only ever serialises it
    immediately) but the claim should be true regardless of what a future
    caller does with it."""
    return copy.deepcopy(_llm_fault_counters)


def _credentials_snapshot() -> dict:
    """Read-only render of the credential counters for GET /memory/telemetry.
    audit_log_dropped surfaces the credential log's own AsyncLineWriter.dropped
    (0 when auditing is disabled or nothing was ever dropped).

    Each counter is paired with a `<name>_last_ts` on the SAME snapshot —
    ISO-8601 UTC, the format `llm_faults[...]["last"]["ts"]` already uses, and
    None until the counter first moves. Flat sibling keys rather than the
    nested `{count, last: {...}}` shape `llm_faults` carries: this section
    shipped as bare ints at v0.9.4 and consumers read them as ints, so the
    additive form preserves the existing contract where a restructure would
    break it.

    INVARIANT: a non-zero counter always carries a non-null partner. Absence
    of a timestamp means the event has not happened in this process, never
    that it happened at an unknown time — which is what makes the pair usable
    as an age (`now - last_ts`) instead of a poll-delta that inverts on
    restart."""
    # Bound ONCE, not read twice off the global: the count and its timestamp
    # must come from the same writer. Reading the global per entry lets a swap
    # or a disable land between them and produce a non-zero count beside a null
    # stamp — the exact pair-disagreement this section exists to rule out.
    # (Code-quality review I1.)
    writer = _credential_audit_writer
    return {
        "token_verify_failed": _credential_counters["token_verify_failed"],
        "token_verify_failed_last_ts": _credential_last_ts["token_verify_failed"],
        "daemon_tokens_issued": _credential_counters["daemon_tokens_issued"],
        "daemon_tokens_issued_last_ts": _credential_last_ts["daemon_tokens_issued"],
        "credentialed_route_denied": _credential_counters["credentialed_route_denied"],
        "credentialed_route_denied_last_ts": _credential_last_ts["credentialed_route_denied"],
        "audit_log_dropped": writer.dropped if writer else 0,
        "audit_log_dropped_last_ts": writer.last_dropped_ts if writer else None,
    }


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """DEFAULT DENY, and the single identity → govern → audit choke point.

    Order: shed if over the in-flight cap (S-11 — ahead of every exemption,
    unprotected paths included) → auth-disabled bypass → unprotected-path
    exemption → resolve a verified identity (pluggable — bearer today, PoP
    later) → enforce read-only role → dispatch → audit the outcome. A DB pool
    that stays saturated past POOL_ACQUIRE_TIMEOUT surfaces as
    asyncio.TimeoutError from a handler's _acquire(); it is mapped here to
    503 + Retry-After so the gateway sheds load instead of hanging a caller.
    """
    global _inflight
    _check_client_version(request)  # logs API skew to the gateway log; never raises

    # S-11 (PR A5): the load-shed valve is the FIRST gate — ahead of both the
    # auth-disabled bypass below and the per-path _UNPROTECTED_PATHS
    # exemption further down. It used to sit after both, so an anonymous
    # /health or /pool/status hit (and, on an auth-unset install, EVERY
    # request) could never be shed no matter how saturated the gateway
    # already was. The valve exists to protect the PROCESS from an in-flight
    # pile-up, not to gate access, so it must apply uniformly regardless of
    # what auth decides afterward.
    if GATEWAY_INFLIGHT_MAX and _inflight >= GATEWAY_INFLIGHT_MAX:
        raise web.HTTPServiceUnavailable(
            reason="gateway at capacity", headers={"Retry-After": "1"},
        )

    # SEC-A5-05a (PR A5 fix round): a request that reaches this point WAS
    # ADMITTED past the valve above — count it uniformly here, regardless of
    # which branch below actually serves it, so the cap the valve enforces
    # reflects TOTAL admitted load (anonymous /health/pool-status traffic
    # and, on an auth-off install, every request) and not just the
    # authenticated dispatch. The earlier version of this comment claimed
    # this protection while `_inflight` was only ever incremented far below,
    # inside the authenticated-only branch — that was false; this `try/
    # finally` is what makes it true. A request that sheds at the check
    # above was never admitted and correctly never reaches this increment.
    _inflight += 1
    try:
        # Gate on the STARTUP truth (finding 1), not on whether _AGENT_TOKENS
        # happens to be non-empty right now -- see AUTH_CONFIGURED_AT_STARTUP's
        # docstring above for why the two diverge after a daemon token is minted.
        if not AUTH_CONFIGURED_AT_STARTUP:
            return await handler(request)
        if request.path.rstrip("/") in _UNPROTECTED_PATHS or request.path in _UNPROTECTED_PATHS:
            return await handler(request)

        agent_name = resolve_identity(request)
        if not agent_name:
            # RFC 6750 §3: a token was PRESENTED but rejected gets
            # error="invalid_token"; no token at all gets the bare challenge —
            # and only the former has a digest worth logging (PR A3). This is
            # the gateway's OWN 401 (its own door), never an upstream one.
            presented = _extract_bearer_token(request)
            _record_token_verify_failed(request, presented)
            www_authenticate = 'Bearer error="invalid_token"' if presented else "Bearer"
            raise web.HTTPUnauthorized(
                reason="Authorization: a valid Bearer token is required",
                # X-SM-Fault-Origin alongside the RFC 6750 challenge (security
                # review O-5): the header is otherwise set only on the three
                # LLM-path gateway errors, so a client distinguished a gateway-
                # origin 401 from an upstream one only by the header's ABSENCE —
                # and absence is exactly what a stripping intermediary produces.
                headers={"WWW-Authenticate": www_authenticate, "X-SM-Fault-Origin": "gateway"},
            )
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

        started    = asyncio.get_running_loop().time()
        request_id = uuid.uuid4().hex[:12]
        status     = 500
        # Stashed so a downstream handler (hive_mind_proxy.handle_proxy) can
        # correlate its own gateway_fault/upstream_credential_fault credential-
        # audit lines with this same request (PR A3) — mirrors request["principal"]
        # above.
        request["request_id"] = request_id
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
            latency_ms = (asyncio.get_running_loop().time() - started) * 1000
            _audit(agent_name, request.method, request.path, status, latency_ms,
                   request_id, request.get("principal"),
                   backend=request.get("backend"),
                   key_attached=bool(request.get("key_attached")))
    finally:
        _inflight -= 1

# ── Config ────────────────────────────────────────────────────────────────────
# PG_PASSWORD/NEO4J_PASSWORD/PG_CONN are secrets (SEC-05/SEC-09, PR A1; PG_CONN
# added in the review fix round — a DSN embeds the password verbatim) — read
# via secure_env.get_secret(), never os.environ directly. hive_mind_proxy calls
# secure_env.load_split_env() before importing this module, so by the time
# these run the framework .env's secret keys are already in secure_env's
# in-process store; get_secret() checks os.environ first regardless (an
# operator-exported value always wins), so a direct module load (tests, a
# standalone run) still works off an exported var.

_pg_pass = get_secret("PG_PASSWORD", "")
PG_DSN   = get_secret(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", get_secret("NEO4J_PASSWORD", ""))

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
# EMBEDDING input to a conservative char budget derived from that context; the
# FULL text is always kept in Tier 1 (technical_docs), so search still returns
# it — only the vector is computed from the prefix. Prompted by an advisor
# hitting a smaller BGE-M3 limit; guards us as summaries grow with the
# larger-context work.
#
# The clamp AND the request timeout both come from dream_telemetry so the save
# path and the NREM fold path cannot drift apart — they call one embedder with
# one context limit, so they get one derivation. (dream_telemetry imports only
# stdlib + log_hygiene, which this module already depends on, so this does not
# pull psycopg2 into the gateway venv.)
from dream_telemetry import (EMBED_MAX_CHARS, EMBED_TIMEOUT_FLOOR_S,  # noqa: E402
                             RERANK_MAX_DOC_CHARS, clamp_rerank_doc,
                             embed_ceiling, rerank_ceiling)

# Read-contract graph expansion cap: how many edges surface per anchored record
# in search results. Env-tunable. Ordering (in the expansion Cypher) puts
# provenance-bearing edges (r.asserted_by set) and typed relations ahead of bare
# MENTIONS, so the highest-signal context survives the cap — context without
# relation properties is noise disguised as fact.
GRAPH_EXPANSION_LIMIT = _env_int("GRAPH_EXPANSION_LIMIT", 15)

# Tier-1 candidates fetched for the reranker when the caller asks for few. A
# FLOOR, not a cap: the effective pool is max(this, the caller's limit), so a
# request for 100 results retrieves 100 candidates rather than silently
# collapsing to this number. Retrieve-then-rerank depends on the pool being
# wider than the result set — the reranker can only reorder what it is handed.
SEARCH_CANDIDATE_FLOOR = _env_int("SEARCH_CANDIDATE_FLOOR", 20)

# Security review, PR 235: the `domains` search filter binds straight into a
# jsonb `?|` operator with a caller-controlled array — an authenticated caller
# sending thousands of entries per request is a DoS vector (unbounded work per
# row scanned). Capped, not silently truncated: a search that silently dropped
# entries past the cap would still return 200 with an incomplete filter, and an
# empty result would then read as authoritative when it was really partial.
SEARCH_DOMAINS_FILTER_CAP = _env_int("SEARCH_DOMAINS_FILTER_CAP", 16)

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
# Stamp carried by a judgement's COPY of a topic its evidence already had — see
# _inherit_entities_from_facts (989). Mirrors relation_confidence.ASSERTED_INHERITED.
RELATION_ASSERTED_INHERITED = "inherited"
RELCONF_CONSUME_THRESHOLD: dict[str, float] = {
    # 0.68 mirrors the WRITE floor exactly (989): what is trusted enough to
    # write is trusted enough to fold. Keep in step with relation_confidence.
    "entity_relation": _env_float("RELCONF_CONSUME_ENTITY", 0.68),
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

# Per-alternative vectors (migration 026). A decision's alternatives are written
# to `decision_alternatives` inside the save transaction with a NULL embedding,
# and filled here afterwards — the save path stays one embedding call regardless
# of how many options a decision weighed.
#
# THE TABLE IS THE QUEUE, which is what makes the async choice safe: pending
# work is `embedding IS NULL` in a committed row, so a restart between the write
# and the embed cannot strand anything. The sweep interval is therefore a
# latency knob, not a correctness one.
ALT_VECTOR_POLL_INTERVAL = _env_float("ALT_VECTOR_POLL_INTERVAL", 10.0)
ALT_VECTOR_BATCH_SIZE    = _env_int("ALT_VECTOR_BATCH_SIZE", 32)

# A PENDING ROW IS NEVER ABANDONED, and this threshold does not abandon it.
#
# The outbox gives up after OUTBOX_MAX_RETRIES because a Cypher statement can be
# permanently unapplyable. An alternative cannot: the text is non-blank, clamped
# to the embedder's context, and already stored — so essentially the only way to
# fail is that the embedder is unavailable, which is a condition of the SYSTEM
# and says nothing about the row. Charging a batch-wide outage to each row until
# it is written off is the v0.7.2 defect exactly (a batch 503 charged to every
# record, which stopped the cycle for days).
#
# So `attempts` counts CONSECUTIVE failures, resets on success, and drives the
# backoff. Past this threshold the row is reported as `failing` in telemetry
# with its last error, and keeps retrying at the capped interval — visible, and
# still recoverable the moment the embedder returns.
ALT_VECTOR_FAILING_AFTER = _env_int("ALT_VECTOR_FAILING_AFTER", 5)

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
# (project, domain) group that meets the v2 FACT GATE NREM actually fires on
# (Dreaming Cycle Plan to v2, §2.1; C1/C1b) — NOT a raw unconsolidated record
# count, and NOT a per-entity or project-only count (neither level exists any
# more). Fact groups reuse ONT.density_threshold, the SAME value
# consolidation_loop.py gates on, over facts resolved straight off the graph's
# GROUNDED_IN/DOMAIN_OF/PROJECT_OF edges — no project_axis.PROJECT_SQL
# involved here, since a DOMAIN_OF/PROJECT_OF edge only exists for an already-
# registered (project, domain) pair. Decision cycles run the insight gate
# itself, count-only, from insight_gate.py — its own threshold travels with
# it, so a deployment that tunes insight_threshold sees the tuned number here
# rather than a hardcoded twin.
#
# There is deliberately no default-project constant here any more: the gauge
# never invents a key. consolidation_loop keeps DEFAULT_DOMAIN for a summary's
# OWN stored key, which is a different thing from a fact's fold key.

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


def _consolidation_backlog(eligible_clusters) -> int:
    """Backlog for the stall verdict = the cycle's OWN recorded gate census
    (``eligible_clusters``) and NOTHING else.

    I7 contract (Dreaming_Cycle_Plan_to_v2.md §2.6, `decision:1121`):
    consolidation is SELECTIVE BY DESIGN — a cycle that folds nothing because
    nothing GATED is a correct outcome, not a stall. "Stall" means GATED BUT
    NOT FOLDING; a candidate that never gated is not backlog. So when no cycle
    has yet recorded its own census (``eligible_clusters is None`` — e.g. a
    fresh deploy, or every run so far crashed before reaching the gate), that
    is an ABSENCE OF EVIDENCE, not evidence of backlog: report 0, not a looser
    substitute count.

    Previously this fell back to the NREM density count
    (``_nrem_cycle_counts``) when no census had been recorded, which answers
    "does raw candidate material exist" rather than "did it gate" — the two
    are exactly the distinction I7 draws, and conflating them let a cycle that
    had never run report a stall the strict gate would never have agreed to.
    The fallback is removed; it must not be reintroduced. Pure → testable."""
    return eligible_clusters if eligible_clusters is not None else 0


def _consolidation_stall_verdict(last_success_age, in_flight, has_backlog, threshold) -> bool:
    """Pure stall rule (ADR-018): a cycle is stalled when an eligible backlog
    exists, no successful fold landed within the threshold (or none ever), and
    nothing is currently in-flight. Extracted so the verdict is unit-testable
    without a database.

    I7 (`decision:1121`): this function was already correct — ``has_backlog``
    is trusted as given, so the guarantee that it means GATING backlog (not
    raw density) lives entirely in what the caller passes as ``has_backlog``,
    i.e. in ``_consolidation_backlog`` above. Not changed by this fix; cited
    here so the two functions' contracts are read together."""
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


def _axis_filter_predicate(start: int, project: str | None,
                            domains: list[str] | None,
                            since: datetime | None) -> tuple[str, list]:
    """Build the optional project/domains/since AND-predicate `handle_search`
    adds to a candidate query — Tier-1 `technical_docs` and Tier-3
    `community_summaries` alike. Applied to the CANDIDATE SET the reranker
    scores, never as a post-hoc filter on already-ranked results: a named
    place/time is a FILTER, not query text (the motivating measured failure —
    folding a project name into query text ranked records that merely MENTION
    the project above records that BELONG to it, and the genuinely relevant
    facts landed below the limit cut on their weakest signal).

    All three are optional and additive. No filter requested returns `("", [])`
    — an unfiltered search's query text and arg list are byte-for-byte
    unchanged from before this predicate existed.

    `project` matches the canonical top-level `metadata->>'project'` string.
    `domains` matches the canonical top-level `metadata->'domains'` JSON array
    with OR semantics (`?|` — true when ANY named domain is present) — the
    CANONICAL KEY ONLY (decision:1214), never the older singular `domain`
    string or the `decision` blob. A pre-1214 thematic community_summaries row
    (still written with singular `domain`) legitimately does not match a
    domains filter rather than being silently reached through a second key —
    the canonical key is the contract now. `since` matches `created_at >=` a
    parsed, tz-aware datetime.

    Read path never blocks on registry state: an unknown project/domain name is
    not refused here, it simply matches nothing (a searcher may probe).

    ⚠ `domains` is CALLER-BOUND before it ever reaches here: `handle_search`
    rejects more than `SEARCH_DOMAINS_FILTER_CAP` (16) entries with a 400
    `filters_invalid` at ingress, never truncates silently — an unbounded list
    binds straight into the `?|` scan (a DoS vector), and a silent drop would
    let a partial filter's empty result read as authoritative. This function
    does not re-check the cap; it trusts its caller.

    `start` is the next free asyncpg positional index (`$N`).
    """
    clauses: list[str] = []
    params: list = []
    idx = start
    if project:
        clauses.append(f"metadata->>'project' = ${idx}")
        params.append(project)
        idx += 1
    if domains:
        clauses.append(f"metadata->'domains' ?| ${idx}::text[]")
        params.append(list(domains))
        idx += 1
    if since is not None:
        clauses.append(f"created_at >= ${idx}::timestamptz")
        params.append(since)
        idx += 1
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


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
    pg_id-keyed neighbor, returning only the keys that are set.

    Fact/Retrospective evidence weight (``fact_kind``/``source_ref``) only.
    A DECISION's ``confidence``/``alternatives`` are NOT read here any more —
    they are payload and are dereferenced from Postgres by pg_id (see
    ``_decision_payload_props``). ``fact_kind`` stays on the node because it is
    DERIVED at write from ``source_ref`` rather than copied from a Postgres
    column, which makes it a different question from a payload copy.

    A neighbor that carries none returns ``{}`` (no ``adr_props`` key is
    added). Missing projection columns (older single-anchor callers, test
    stubs) are tolerated via ``.get``. Pure — never raises, so it can never
    fail a search.
    """
    def _g(key):
        try:
            return rec[key]
        except (KeyError, TypeError, IndexError):
            return None
    adr: dict = {}
    if _g("adr_fact_kind"):
        adr["fact_kind"] = _g("adr_fact_kind")
    if _g("adr_source_ref"):
        adr["source_ref"] = _g("adr_source_ref")
    return adr


def _decision_payload_props(alternatives, confidence) -> dict:
    """Build the ``adr_props`` payload for ONE decision from its Postgres
    ``metadata->'decision'`` values, returning only the keys that are set.

    This is the read half of *duplicate what the walk consumes, dereference
    what the reader renders*: no Cypher filters or orders on these two, so the
    graph carries neither and the record they belong to supplies them, reached
    by the ``pg_id`` the subgraph already carries.

    ⚠ NEVER bare-``list()`` the alternatives. Postgres holds a JSON array for
    every decision that has the key today, where ``list()`` is a passthrough —
    but this value has been stored as a JSON *string* before, and ``list()`` on
    a string explodes it into single characters, turning three alternatives
    into several hundred one-character ones. A string is ONE entry, not a
    sequence of them. The trap moved stores; the guard moves with it.

    Pure — never raises, so it can never fail a search.
    """
    props: dict = {}
    if alternatives:
        props["alternatives"] = _json_safe(
            list(alternatives) if isinstance(alternatives, (list, tuple))
            else [alternatives])
    if confidence:
        props["confidence"] = _json_safe(confidence)
    return props


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
        # Rerank outcome counters. The reranker is a separate process on the
        # search path with a FALLBACK, so its total failure is silent by
        # construction — it degrades to vector order and still answers. These
        # make that visible: a rising failure count against a flat success count
        # is a reranker that is up (it answers /health) but cannot serve.
        self._rerank_successes = 0
        self._rerank_failures = 0
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
                                             # Same rule for the identity gauge:
                                             # "not yet probed" must never read
                                             # as "upgrade complete".
                                             "project_identity": None,
                                             "domain_identity": None,
                                             "inference_busy": "unknown", "fresh": False}
        self._consolidation_health_task: asyncio.Task | None = None
        self._alt_vector_task: asyncio.Task | None = None

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
        # No startup recovery step, unlike the outbox above: pending work here
        # is `embedding IS NULL`, a state the row was committed in rather than
        # one a running process moved it to. There is nothing for a restart to
        # put back.
        self._alt_vector_task = asyncio.create_task(
            self._alternative_vector_worker(), name="alternative-vector-worker")
        log.info("coordinator ready (pool %d–%d, outbox + alternative-vector workers running)",
                 POOL_MIN, POOL_MAX)
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
        for task in (self._outbox_task, self._consolidation_health_task,
                     self._alt_vector_task):
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
        if _credential_audit_writer is not None:
            try:
                await _credential_audit_writer.aclose()
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
        # Per-request timeout sized on the clamped input, overriding the shared
        # client's default. Embedding cost is superlinear in length, so a
        # constant that suits a short fact under-provisions a long decision or a
        # maximally-sized summary — the client default of 30s did not even cover
        # this function's own clamp.
        ceiling = embed_ceiling(len(text))
        for attempt in range(1, EMBED_RETRIES + 1):
            try:
                r = await client.post(EMBED_URL, json={"input": text, "model": "bge-m3"},
                                      timeout=ceiling)
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

    async def _embed_many(
        self, texts: list[str], client: httpx.AsyncClient
    ) -> list[list[float]]:
        """Embed several texts in ONE request, preserving input order.

        The embedder accepts a list on `input` and returns one object per item
        carrying its own `index`. The results are re-ordered by that index
        rather than trusted to arrive in order — a response that came back
        shuffled would otherwise attach every vector to the wrong alternative,
        which is invisible in the data and fatal to the similarity it exists for.

        Same clamp as `_embed`, applied per item, and the timeout is sized on
        the TOTAL payload because the whole batch travels as one request.
        """
        if not texts:
            return []
        clamped = [t[:EMBED_MAX_CHARS] for t in texts]
        ceiling = embed_ceiling(sum(len(t) for t in clamped))
        for attempt in range(1, EMBED_RETRIES + 1):
            try:
                r = await client.post(
                    EMBED_URL, json={"input": clamped, "model": "bge-m3"},
                    timeout=ceiling,
                )
                r.raise_for_status()
                data = r.json()["data"]
                if len(data) != len(clamped):
                    raise RuntimeError(
                        f"embedder returned {len(data)} vectors for "
                        f"{len(clamped)} inputs"
                    )
                ordered = sorted(data, key=lambda d: d.get("index", 0))
                return [d["embedding"] for d in ordered]
            except Exception as exc:
                if attempt == EMBED_RETRIES:
                    raise RuntimeError(
                        f"Batch embedding failed after {EMBED_RETRIES} attempts "
                        f"({len(clamped)} inputs): {exc}"
                    ) from exc
                wait = EMBED_BACKOFF * attempt
                log.warning(
                    "batch embed attempt %d/%d failed (%s) — retry in %.1f s",
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
        if len(clean) > 50:
            clean = clean[:50]
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

            if params.get("type") == "project_of":
                await self._apply_project_of_outbox_row(outbox_id, pg_id, params)
                return

            if params.get("type") == "domain_of":
                await self._apply_domain_of_outbox_row(outbox_id, pg_id, params)
                return

            # Standard Fact + Entity MERGE — all writes in one round-trip so they
            # succeed or fail atomically. MERGE is idempotent — safe to retry.
            source_ref = params.get("source_ref") or None
            fact_kind = params.get("fact_kind") or "observation"
            project_id = await self._project_identity(params.get("project"))
            domain_ids = await self._domain_identities(
                pg_id, project_id, params.get("domains"))
            clean_entities = self._gate_graph_entities(pg_id, params.get("entities", []))
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
                    f"   {project_merge_cypher(project_id)}"
                    f"   MERGE (f)-[:{ONT.project_of}]->(p))"
                    # The domain chain, in the SAME round-trip (028):
                    # (:Fact)-[:DOMAIN_OF]->(:Domain)-[:PROJECT_OF]->(:Project).
                    # One FOREACH per named section, keyed on the registry id —
                    # there is no name-keyed form, so an unresolved section
                    # simply contributes no row to $domains and no edge.
                    # The Domain→Project edge reuses PROJECT_OF rather than
                    # inventing a second belonging relation: a section belongs
                    # to its project in exactly the sense a record does.
                    # ⚠ The project node is MERGED AGAIN here rather than reusing
                    # `p`. A variable bound inside a FOREACH does not survive it,
                    # so `p` is simply not in scope in this block — and Cypher
                    # would reject the query outright rather than silently
                    # writing the wrong thing. Re-merging on the same key is
                    # idempotent and reaches the same node; the name is SET by
                    # the project block above, so it is not repeated here.
                    + (
                        f" FOREACH (row IN $domains |"
                        f"   MERGE (dp:{ONT.project} {{project_id: $project_id}})"
                        f"   {domain_merge_cypher(id_param='row.id')}"
                        f"   SET d.name = row.name"
                        f"   MERGE (f)-[:{ONT.domain_of}]->(d)"
                        f"   MERGE (d)-[:{ONT.project_of}]->(dp))"
                        if domain_ids and project_id is not None else ""
                    )
                    + f" WITH f"
                    f" UNWIND $entities AS ename"
                    f" MERGE (e:{ONT.entity} {{name: ename}})"
                    f" MERGE (f)-[:{ONT.entity_link}]->(e)",
                    pg_id=pg_id,
                    content=params.get("content_snippet", "")[:200],
                    source=params.get("source", "coordinator"),
                    person=params.get("person") or "",
                    project=params.get("project") or "",
                    project_id=project_id,
                    domains=domain_ids,
                    fact_kind=fact_kind,
                    entities=clean_entities,
                    **( {"source_ref": source_ref} if source_ref else {} ),
                )
                if clean_entities:
                    async with self._acquire() as conn:
                        await conn.executemany(
                            "INSERT INTO entity_registry (name, registered_by) VALUES ($1, 'fact_ingress') ON CONFLICT (name) DO NOTHING",
                            [(e,) for e in clean_entities],
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

    # ── Per-alternative vectors ───────────────────────────────────────────────

    @staticmethod
    def _desired_alternatives(metadata: dict) -> list[tuple[int, str]]:
        """The (ordinal, text) pairs a record's metadata calls for.

        Pure, so the convergence rule is testable without a database. The
        ordinal is the position in the decision's OWN array, and blank entries
        are dropped without renumbering what follows — an alternative's ordinal
        has to keep pointing at the same entry of `metadata.decision.alternatives`
        or the two stores stop agreeing about which option is which.

        A record that is not a decision, or carries no alternatives, wants NO
        rows — which is what makes this converge rather than accumulate: the
        same code path that adds a new alternative removes a retracted one.
        """
        if metadata.get("type") != "decision":
            return []
        alts = (metadata.get("decision") or {}).get("alternatives")
        if not isinstance(alts, list):
            return []
        return [(i, t) for i, t in enumerate(alts)
                if isinstance(t, str) and t.strip()]

    async def _reconcile_decision_alternatives(
        self, conn, pg_id: int, metadata: dict
    ) -> dict:
        """Converge `decision_alternatives` on what this save actually says.

        RECONCILE, NEVER APPEND. A save can rewrite an existing record in place
        — `ON CONFLICT (content_hash) DO UPDATE` — and alternatives do get
        rewritten: the repair that rejoined 46 shredded decisions changed the
        text of rows that already existed. Appending would leave the fragments
        behind as vectors that cluster on nothing, which is the failure the
        repair was ordered before the vectors to avoid.

        UNCHANGED TEXT IS NOT TOUCHED, and that is enforced by the statement
        rather than by care: the `DO UPDATE` carries a `WHERE text IS DISTINCT
        FROM` guard, so an idempotent re-save of a decision with five
        alternatives writes nothing and re-embeds nothing. Only an entry whose
        text actually differs is reset to pending.

        Runs INSIDE the save transaction, so the rows and the record they belong
        to commit together — there is no window where a decision exists with a
        stale alternative set.
        """
        desired = self._desired_alternatives(metadata)
        ordinals = [o for o, _ in desired]
        texts = [t for _, t in desired]

        # Anything not in the desired set goes, including every row when the
        # set is empty: `NOT (ordinal = ANY('{}'))` is true for all rows.
        removed = await conn.execute(
            "DELETE FROM decision_alternatives"
            " WHERE decision_pg_id = $1 AND NOT (ordinal = ANY($2::int[]))",
            pg_id, ordinals,
        )
        written = []
        if desired:
            written = await conn.fetch(
                "INSERT INTO decision_alternatives (decision_pg_id, ordinal, text)"
                " SELECT $1, o, t FROM unnest($2::int[], $3::text[]) AS x(o, t)"
                " ON CONFLICT (decision_pg_id, ordinal) DO UPDATE"
                "    SET text = EXCLUDED.text,"
                # A changed alternative is a DIFFERENT alternative: its old
                # vector describes text nobody wrote any more, so the row goes
                # back to pending rather than keeping a stale embedding.
                "        embedding = NULL, embedded_at = NULL,"
                "        attempts = 0, last_error = NULL, next_attempt_at = NULL"
                "  WHERE decision_alternatives.text IS DISTINCT FROM EXCLUDED.text"
                " RETURNING id",
                pg_id, ordinals, texts,
            )
        return {"desired": len(desired),
                "written": len(written),
                "removed": int(removed.split()[-1]) if removed else 0}

    async def _alternative_vector_worker(self) -> None:
        """Background task: fill alternatives whose embedding is still NULL."""
        log.info("alternative-vector worker started (poll every %.1f s)",
                 ALT_VECTOR_POLL_INTERVAL)
        while True:
            try:
                await self._fill_pending_alternative_vectors()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # FAILURE ≠ IDLE. A sweep that dies quietly leaves a table full
                # of pending rows and a worker that looks like it has nothing to
                # do; the loop survives, and the error is on the record.
                log.error("alternative-vector worker error: %s", exc, exc_info=True)
            await asyncio.sleep(ALT_VECTOR_POLL_INTERVAL)

    async def _fill_pending_alternative_vectors(self) -> int:
        """One sweep: embed a batch of pending alternatives. Returns rows filled.

        The pending set is a QUERY (`embedding IS NULL`), not a queue held in
        this process, which is the whole reason the write path can be async: a
        restart between the save and the embed leaves committed rows that the
        next sweep picks up. Nothing needs to remember what was in flight.

        No GPU gate. Consolidation defers on inference load because a fold is a
        long LLM call; an embedding is small and the save path already issues one
        unconditionally, so deferring here would add latency to the backlog
        without relieving anything the folds compete for.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, text FROM decision_alternatives"
                " WHERE embedding IS NULL"
                "   AND (next_attempt_at IS NULL OR next_attempt_at <= now())"
                " ORDER BY attempts, id"
                " LIMIT $1",
                ALT_VECTOR_BATCH_SIZE,
            )
        if not rows:
            return 0

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                vectors = await self._embed_many([r["text"] for r in rows], client)
        except Exception as exc:
            await self._defer_pending_alternatives([r["id"] for r in rows], exc)
            return 0

        async with self._acquire() as conn:
            async with conn.transaction():
                for row, vec in zip(rows, vectors):
                    await conn.execute(
                        "UPDATE decision_alternatives"
                        "   SET embedding = $2::vector, embedded_at = now(),"
                        "       attempts = 0, last_error = NULL,"
                        "       next_attempt_at = NULL"
                        # The row may have been reset to a NEW text while this
                        # batch was in flight (a re-save mid-sweep). Writing the
                        # old vector onto it would attach an embedding to text
                        # it does not describe, so the update is conditioned on
                        # the row still being the one that was read.
                        " WHERE id = $1 AND text = $3 AND embedding IS NULL",
                        row["id"], str(vec), row["text"],
                    )
        log.info("alternative vectors: embedded %d row(s)", len(rows))
        return len(rows)

    async def _defer_pending_alternatives(self, ids: list[int], exc: Exception) -> None:
        """Back off a batch that could not be embedded — WITHOUT writing it off.

        `attempts` is a consecutive-failure counter here, not a budget: it grows
        the backoff and raises the `failing` flag in telemetry, and no value of
        it stops the row being retried. See ALT_VECTOR_FAILING_AFTER for why an
        alternative that cannot be embedded is nearly always a statement about
        the embedder rather than about the row.
        """
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE decision_alternatives"
                "   SET attempts = attempts + 1,"
                "       last_error = $2,"
                "       next_attempt_at = now() + make_interval("
                "           secs => least($3, $4 * power(2, attempts)))"
                " WHERE id = ANY($1::bigint[])",
                ids, str(exc)[:500],
                OUTBOX_BACKOFF_MAX, OUTBOX_BACKOFF_BASE,
            )
        log.warning("alternative vectors: %d row(s) deferred — %s", len(ids), exc)

    async def _promote_grounded_parked_facts(
        self, conn, grounded_typed: list, agent_id: str, judgement_pg_id: int
    ) -> list:
        """Caller 1 — a judgement that grounds a PARKED fact establishes its
        project, when the judgements grounding that fact agree on exactly one.

        The evidence is the fact's own grounding neighbourhood, not just the
        judgement being written: the query below reads EVERY judgement citing
        the fact, including the one this transaction just inserted. Two
        judgements naming two projects leave the fact parked — `sole_project`
        is the ambiguity guard, and abstentions (judgements with no project of
        their own) are ignored rather than counted as dissent.

        ⚠ EACH PROMOTION RUNS IN ITS OWN SAVEPOINT, and that is not tidiness.
        This runs INSIDE the save's transaction so the promotion is atomic with
        the record that justified it — but a failing statement poisons a
        Postgres transaction, so without a savepoint one bad promotion would
        roll back the SAVE it rode in on. That would turn an opportunistic
        enrichment into a new way for `/memory/save` to fail, which is the same
        shape as the telemetry query that would have made a REM blip read as a
        quiet system. The save is the work; this is a passenger.
        """
        promoted: list = []
        for g in grounded_typed or []:
            if g.get("label") != ONT.fact:
                continue
            target_id = g.get("pg_id")
            if not isinstance(target_id, int) or isinstance(target_id, bool):
                continue
            try:
                async with conn.transaction():
                    rows = await conn.fetch(
                        f"SELECT DISTINCT {PROJECT_SQL} AS project"
                        f" FROM technical_docs"
                        f" WHERE metadata->>'type' IN ('decision', 'retrospective')"
                        f"   AND metadata->'grounded_in' @> to_jsonb($1::bigint)",
                        target_id,
                    )
                    agreed = sole_project([r["project"] for r in rows])
                    if agreed is None:
                        continue
                    result = await promote_record(
                        conn, target_id, agreed,
                        method=METHOD_GROUNDING,
                        actor=agent_id or "coordinator",
                        note=f"grounded by judgement pg_id={judgement_pg_id}",
                    )
                    if result["promoted"]:
                        promoted.append(target_id)
            except Exception as exc:
                log.warning(
                    "grounding promotion failed for pg_id=%s (save unaffected): %s",
                    target_id, exc,
                )
        return promoted

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

    async def _inherit_domains(self, session, pg_id: int,
                               anchor_label: str = None) -> int:
        """A judgement's DEFAULT sections, when it asserted none of its own.
        Returns the number of `DOMAIN_OF` edges written.

        Where they come from differs by record, and each route mirrors how that
        record already gets its PROJECT:

          decision       the union of the sections of the FACTS it grounds in.
          retrospective  the sections of the DECISION it judges — the same source
                         its project comes from, so a verdict is always filed
                         with what it judges.

        ⛔ A GOOD DEFAULT IS NOT AN ENFORCED ONE, and for a decision that
        distinction is the whole rule. A decision REACHES FURTHER THAN ITS
        EVIDENCE: a fact observes that agents write to the graph directly — an
        infrastructure observation — while the decision it provokes may govern
        which agents are AUTHORISED to write, which is about tokens and access
        and sits above the infrastructure that prompted it. Inheriting would cap
        the decision at its evidence's sections, so the section that most needs
        to surface it never would. So a decision that names its own sections
        keeps them, and one that names none takes its evidence's — which is right
        far more often than it is wrong, and is never a ceiling.

        ⚠ THE GUARD IS THE `asserted_by` STAMP, which is why this is safe to
        re-run from anywhere. A self-asserted edge is written bare at first
        write; an inherited one is stamped. So "did this record name its own?"
        is answerable from the graph, and inheritance simply declines when the
        answer is yes. Without that test, re-running after a retrospective landed
        would ADD the evidence's sections to a decision that deliberately chose
        different ones — silently converting an assertion into a superset.

        ⚠ A RETROSPECTIVE DELIBERATELY DOES NOT READ ITS OWN GROUNDING FACTS,
        which would have been the obvious symmetry with the entity inheritance
        beside it. Those facts are the LATER measurement and routinely sit in a
        different section from the decision. Entities are ABOUTNESS and
        legitimately come from the measurement; domain is BELONGING and comes
        from what is being judged.

        No timing defect at first write — a decision's grounding and a
        retrospective's target both exist before the row that reads them. A
        section added LATER does not propagate on its own;
        `backfill_domain_of.py`'s inherit mode re-runs exactly this query, which
        is why the rule lives here rather than in that tool.
        """
        anchor = anchor_label or ONT.retrospective
        # "Names no section of its own" — a bare DOMAIN_OF is an assertion, a
        # stamped one is a copy. Same convention the entity inheritance uses.
        self_asserted = (
            f" WHERE NOT EXISTS {{ MATCH (a)-[sm:{ONT.domain_of}]->()"
            f" WHERE sm.asserted_by IS NULL }}"
        )
        if anchor == ONT.decision:
            rels = "|".join(GROUNDING_RELATIONS)
            cypher = (
                f"MATCH (a:{ONT.decision} {{pg_id: $pg_id}})"
                + self_asserted +
                f" MATCH (a)-[:{rels}]->(t)"
                f" MATCH (t)-[:{rels}*0..1]->(f:{ONT.fact})"
                f" WHERE coalesce(f.superseded, false) = false"
                f" MATCH (f)-[:{ONT.domain_of}]->(d:{ONT.domain})"
                f" WITH a, collect(DISTINCT d) AS ds"
                f" UNWIND ds AS d"
                f" MERGE (a)-[m:{ONT.domain_of}]->(d)"
                f"   ON CREATE SET m.asserted_by = '{RELATION_ASSERTED_INHERITED}'"
                f" WITH count(d) AS n RETURN n"
            )
        else:
            cypher = (
                f"MATCH (a:{anchor} {{pg_id: $pg_id}})"
                + self_asserted +
                f" MATCH (a)<-[:{ONT.had_outcome}]-(o:{ONT.decision})"
                f" MATCH (o)-[:{ONT.domain_of}]->(d:{ONT.domain})"
                f" WITH a, collect(DISTINCT d) AS ds"
                f" UNWIND ds AS d"
                f" MERGE (a)-[m:{ONT.domain_of}]->(d)"
                f"   ON CREATE SET m.asserted_by = '{RELATION_ASSERTED_INHERITED}'"
                f" WITH count(d) AS n RETURN n"
            )
        try:
            rec = await (await session.run(cypher, pg_id=pg_id)).single()
        except Exception as exc:
            # Never let the belonging axis take down the record write. The
            # judgement, its project and its grounding are all already correct;
            # a missing inherited section is repairable by the backfill tool,
            # while a failed outbox row would retry the whole projection.
            log.warning("%s pg_id=%d: domain inheritance failed, record is intact "
                        "and the edge is repairable: %s", anchor, pg_id, exc)
            return 0
        n = (rec["n"] if rec else 0) or 0
        if n:
            log.debug("%s pg_id=%d inherited %d domain(s)", anchor, pg_id, n)
        return n

    async def _apply_decision_outbox_row(
        self, outbox_id: int, pg_id: int, params: dict
    ) -> None:
        """
        Materialise a Decision node and its PROV-O edges in Neo4j.

        Creates: Decision, Human (decided_by), Project, AIAgent(s) (assisted_by).
        FOREACH handles empty lists so the query is safe regardless of whether
        assisted_by is set. All writes in one session — atomic on transient
        failures (MERGE is idempotent).

        A decision MINTS NO ENTITIES. It INHERITS them by traversing its
        grounding path to facts — see _inherit_entities_from_facts. The
        caller-supplied `entities` metadata stays in Postgres (Tier 1 pristine)
        but is no longer projected into the graph: a decision's topics are
        whatever its evidence is about, never a second free-text vocabulary
        minted alongside it.
        """
        decision = params.get("decision", {})
        grounded = params.get("grounded") or []
        grounded_in_flat = params.get("grounded_in", [])
        project_id = await self._project_identity(decision.get("project"))
        # A decision SELF-ASSERTS its sections, exactly as it self-asserts its
        # project — it is an axis-asserting record, not an inheriting one. What
        # it inherits is what it is ABOUT (entities, below), never where it
        # belongs. Ingress has already registry-checked every name here.
        domain_ids = await self._domain_identities(
            pg_id, project_id, params.get("domains"))
        async with self._neo4j.session() as session:
            await session.run(
                f"MERGE (d:{ONT.decision} {{pg_id: $pg_id}})"
                f"  SET d.title       = $title,"
                f"      d.rationale   = $rationale,"
                f"      d.date        = $date,"
                f"      d.source      = $source"
                # ⛔ confidence + alternatives are deliberately NOT written here.
                # They stay SPINE ADR fields and are still never minted as
                # :Entity (fact 551: alternatives are 65% free phrases, which
                # would flood the graph — REM still extracts clean CONSIDERED
                # entities from the text). What changed is that they are not
                # COPIED either: nothing walks on them, so the node carries the
                # pg_id and the record carries the payload
                # (`_attach_decision_payload`).
                f" WITH d"
                f" MERGE (h:{ONT.human} {{name: $decided_by}})"
                f" MERGE (d)-[:{ONT.was_attributed_to}]->(h)"
                f" WITH d"
                f" {project_merge_cypher(project_id)}"
                f" MERGE (d)-[:{ONT.project_of}]->(p)"
                # The decision's own sections, in the same round-trip. `d` is the
                # Decision here, so the Domain node is bound as `dm` — and the
                # project node is re-merged inside the FOREACH because a variable
                # bound outside one is not usable as a MERGE target within it.
                + (
                    f" WITH d"
                    f" FOREACH (row IN $domains |"
                    f"   MERGE (dp:{ONT.project} {{project_id: $project_id}})"
                    f"   MERGE (dm:{ONT.domain} {{domain_id: row.id}})"
                    f"   SET dm.name = row.name"
                    f"   MERGE (d)-[:{ONT.domain_of}]->(dm)"
                    f"   MERGE (dm)-[:{ONT.project_of}]->(dp))"
                    if domain_ids and project_id is not None else ""
                )
                + f" WITH d"
                f" FOREACH (ai_name IN $assisted_by |"
                f"   MERGE (a:{ONT.ai_agent} {{name: ai_name}})"
                f"   MERGE (d)-[:{ONT.was_assisted_by}]->(a)"
                f" )",
                pg_id=pg_id,
                title=decision.get("title", params.get("content_snippet", "")[:100]),
                rationale=decision.get("rationale", ""),
                date=decision.get("date", ""),
                source=params.get("source", "coordinator"),
                decided_by=decision.get("decided_by", "unknown"),
                project=decision.get("project", "unknown"),
                project_id=project_id,
                domains=domain_ids,
                assisted_by=decision.get("assisted_by", []),
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
            # Sections, AFTER grounding exists — and unconditionally, because the
            # call guards itself: a decision that asserted its own sections above
            # already carries a bare DOMAIN_OF edge and this declines. One that
            # named none takes its evidence's sections as the default.
            await self._inherit_domains(session, pg_id, ONT.decision)
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
        then the typed grounding ROLE edges (shared writer). The target Decision
        is matched in its own statement so a missing decision leaves the record
        intact (edge no-op). MENTIONS edges are NOT written here: a retrospective
        mints no entity, it inherits the topics of the facts it grounds in — see
        _inherit_entities_from_facts, which runs last for exactly that reason.

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
                    + (" SET r.source_ref = $source_ref" if source_ref else ""),
                    pg_id=pg_id,
                    rating=retro.get("rating", ""),
                    date=retro.get("date", ""),
                    content=params.get("content_snippet", "")[:200],
                    source=params.get("source", "coordinator"),
                    fact_kind=params.get("fact_kind") or "observation",
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
                if target_pg_id is not None:
                    # The decision's own default may also have become reachable:
                    # a decision that asserted no section takes its evidence's,
                    # and this is the moment that evidence can first exist (an
                    # ungrounded decision reaches facts through its retrospective).
                    # Declines on a decision that named its own.
                    await self._inherit_domains(
                        session, target_pg_id, ONT.decision
                    )
                # ⚠ THE RETROSPECTIVE'S SECTIONS COME LAST, and the order is the
                # rule rather than tidiness: it reads the DECISION's edges, so
                # running it before the line above would read them as they were
                # before this same transaction completed them — and a verdict
                # would inherit nothing while the decision it judges ends up filed.
                await self._inherit_domains(session, pg_id, ONT.retrospective)
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

    # Resolve a pg_id to the node that ALREADY carries it under any spine label,
    # creating a :Fact placeholder only when no such node exists. A plain
    # `MERGE (n:Fact {pg_id: $id})` matches on label+property together, so a
    # pg_id belonging to a :Decision or :Retrospective node does not match and a
    # SECOND, phantom :Fact node is minted beside the real record — carrying the
    # supersession while the real node stays unmarked. Ingress now refuses to
    # supersede a judgement at all (_supersession_target_error), but outbox rows
    # queued before that guard existed still replay through here, and the
    # successor side was never guarded at ingress at all. The placeholder branch
    # is kept because the target's own outbox row may not have applied yet — the
    # original reason this was a MERGE.
    _SPINE = f"{ONT.fact}|{ONT.decision}|{ONT.retrospective}"

    async def _ensure_spine_node(self, session, pg_id: int) -> None:
        """Make sure SOME spine node carries this pg_id, minting a :Fact
        placeholder only when none does. The placeholder branch is why this was
        a MERGE originally: the record's own outbox row may not have applied
        yet, and the supersession must still be recorded."""
        await session.run(
            f"OPTIONAL MATCH (n:{self._SPINE}) WHERE n.pg_id = $pg_id"
            f" WITH collect(n) AS ns"
            f" FOREACH (_ IN CASE WHEN size(ns) = 0 THEN [1] ELSE [] END |"
            f"   MERGE (p:{ONT.fact} {{pg_id: $pg_id}}) )",
            pg_id=pg_id,
        )

    async def _apply_supersede_outbox_row(self, outbox_id: int, params: dict) -> None:
        """Standalone supersession mirror for the /memory/supersede route (bare
        retract, or point an existing fact at an existing successor — no new fact
        to piggyback on). One-shot: the row is DELETED on success — it carries no
        dream lifecycle and must never count as working-set backlog.

        Marks the REAL node carrying the pg_id under ANY spine label. A plain
        `MERGE (old:Fact {pg_id: $id})` matches on label+property together, so a
        pg_id belonging to a :Decision or :Retrospective node does not match and
        a SECOND, phantom :Fact is minted beside the real record — carrying the
        supersession while the real node stays unmarked. Ingress now refuses to
        supersede a judgement at all (_supersession_target_error), but rows
        queued before that guard replay through here, and the SUCCESSOR side is
        not guarded at ingress at all."""
        old_id = params.get("old_pg_id")
        new_id = params.get("new_pg_id")
        async with self._neo4j.session() as session:
            await self._ensure_spine_node(session, old_id)
            await session.run(
                f"MATCH (o:{self._SPINE}) WHERE o.pg_id = $old_id"
                f" SET o.superseded = true",
                old_id=old_id,
            )
            if new_id is not None:
                await self._ensure_spine_node(session, new_id)
                await session.run(
                    f"MATCH (o:{self._SPINE}) WHERE o.pg_id = $old_id"
                    f" MATCH (nw:{self._SPINE}) WHERE nw.pg_id = $new_id"
                    f" MERGE (nw)-[:{ONT.supersedes}]->(o)",
                    old_id=old_id, new_id=new_id,
                )
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM neo4j_outbox WHERE id=$1", outbox_id)
        log.debug(
            "outbox: applied supersede old=%s new=%s (outbox_id=%d, row deleted)",
            old_id, new_id, outbox_id,
        )

    async def _apply_project_of_outbox_row(
        self, outbox_id: int, pg_id: int, params: dict
    ) -> None:
        """Point an EXISTING spine record at its :Project — the narrow repair row
        that backfill_project_of.py and the promotion writer enqueue, so both the
        historical gap and the parked → real transition close through the outbox
        instead of by writing Neo4j directly.

        ⚠ It exists because re-enqueuing an ordinary fact row would be actively
        DESTRUCTIVE. That row's Cypher also re-runs `UNWIND $entities MERGE
        MENTIONS`, so replaying it would resurrect every enrichment edge a later
        sweep deliberately deleted — the below-floor cleanup would silently undo
        itself. A repair must touch only what it repairs.

        MATCH on the record, never MERGE: a repair mints no records. If the node
        is gone the row is dropped rather than conjuring a phantom whose only
        property is a pg_id. The match is over the SPINE, not :Fact alone —
        a promotion cascades to retrospectives (P20), and matching one label
        would silently drop those rows.

        ⚠ IT REPLACES, IT DOES NOT ACCUMULATE (P19). This used to be a bare
        MERGE, and a bare MERGE is only correct while every target has no edge —
        which is true of the backfill's population by construction and false of
        the promotion writer's. Measured before this changed: 35 parked facts
        already carried an edge Postgres could not justify, and 4 spine nodes
        carried TWO project edges. A record belongs to one project, and the
        Postgres resolution is that answer (P1), so the graph mirrors it rather
        than keeping every value ever written. Deleting first is unconditional
        on purpose — a flag that can be omitted is how the second-writer defect
        arrives, and on a node with no edge the delete is simply a no-op.

        One-shot, DELETED on success, following the supersede row: it carries no
        dream lifecycle and must never be counted as working-set backlog.
        """
        project = (params.get("project") or "").strip()
        if project:
            project_id = await self._project_identity(project)
            async with self._neo4j.session() as session:
                await session.run(
                    f"MATCH (n:{self._SPINE}) WHERE n.pg_id = $pg_id"
                    f" OPTIONAL MATCH (n)-[stale:{ONT.project_of}]->()"
                    f" DELETE stale"
                    f" WITH DISTINCT n"
                    f" {project_merge_cypher(project_id)}"
                    f" MERGE (n)-[:{ONT.project_of}]->(p)",
                    pg_id=pg_id, project=project, project_id=project_id,
                )
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM neo4j_outbox WHERE id=$1", outbox_id)
        log.info(
            "outbox: backfilled PROJECT_OF pg_id=%s project=%r (outbox_id=%d, row deleted)",
            pg_id, project or "(none — skipped)", outbox_id,
        )

    async def _apply_domain_of_outbox_row(
        self, outbox_id: int, pg_id: int, params: dict
    ) -> None:
        """Point an EXISTING record at its :Domain(s) — the narrow repair row
        `backfill_domain_of.py` enqueues, in two modes.

        `domains: [names]`  a FACT's sections, resolved through the registry the
                            same way first write resolves them.
        `inherit: true`     a JUDGEMENT's sections, re-derived by running the
                            SAME `_inherit_domains` rule the write path runs.

        ⚠ THE INHERIT MODE EXISTS SO THE RULE HAS ONE IMPLEMENTATION. The obvious
        alternative was to let the backfill tool compute a judgement's domains
        from Postgres — decision → grounding facts → sections — which is a second
        expression of P17 that can drift from this one. A repair that re-derives
        a rule is a repair that can disagree with the thing it repairs.

        Narrow, like `project_of` and for the same reason: replaying an ordinary
        fact row would re-run its `MENTIONS` merges and resurrect enrichment
        edges a later sweep deliberately deleted.

        ⚠ IT REPLACES THE SET IT MANAGES, AND ONLY THAT SET — which is why the
        two modes delete different things, and getting that wrong is not
        cosmetic:

          explicit  deletes EVERY DOMAIN_OF edge, then writes what Postgres
                    says. The record's own assertion is the whole answer.
          inherit   deletes only edges STAMPED `inherited` — never a bare one.

        A bare edge is a SELF-ASSERTION. If inherit mode cleared those too it
        would delete a decision's own sections and then re-derive its evidence's,
        silently converting a deliberate choice into the default the operator
        chose to override — and a decision reaches further than its evidence
        precisely so that it CAN differ. Measured: one retrospective in this
        corpus was enqueued in both modes, and the inherit row applied second
        replaced its edge; that came out right only because a retrospective has
        nothing to assert. On a decision the same sequence loses data.

        That is the P19 lesson applied to a MULTI-valued axis: the rule is not
        "exactly one edge", it is "the graph mirrors the current answer rather
        than keeping every answer" — where the current answer is the assertion
        when there is one, and the inheritance when there is not.

        One-shot, DELETED on success: it carries no dream lifecycle and must
        never be counted as working-set backlog.
        """
        inherit = bool(params.get("inherit"))
        anchor = params.get("anchor") or ONT.decision
        written = 0
        async with self._neo4j.session() as session:
            # MATCH, never MERGE — a repair mints no records. A record whose node
            # is gone leaves the row dropped rather than conjuring a phantom.
            exists = await (await session.run(
                f"MATCH (n:{self._SPINE}) WHERE n.pg_id = $pg_id RETURN count(n) AS n",
                pg_id=pg_id,
            )).single()
            if not exists or not exists["n"]:
                log.info("outbox: domain_of pg_id=%s has no spine node — row dropped",
                         pg_id)
            elif inherit:
                # Only what inheritance owns. A bare edge is the record's own
                # assertion and outranks any default — see the docstring.
                await session.run(
                    f"MATCH (n:{self._SPINE}) WHERE n.pg_id = $pg_id"
                    f" MATCH (n)-[stale:{ONT.domain_of}]->()"
                    f" WHERE stale.asserted_by = $stamp"
                    f" DELETE stale",
                    pg_id=pg_id, stamp=RELATION_ASSERTED_INHERITED,
                )
                written = await self._inherit_domains(session, pg_id, anchor)
            else:
                project_id = await self._project_identity(params.get("project"))
                domain_ids = await self._domain_identities(
                    pg_id, project_id, params.get("domains"))
                await session.run(
                    f"MATCH (n:{self._SPINE}) WHERE n.pg_id = $pg_id"
                    f" MATCH (n)-[stale:{ONT.domain_of}]->()"
                    f" DELETE stale",
                    pg_id=pg_id,
                )
                if domain_ids and project_id is not None:
                    await session.run(
                        f"MATCH (n:{self._SPINE}) WHERE n.pg_id = $pg_id"
                        f" FOREACH (row IN $domains |"
                        f"   MERGE (dp:{ONT.project} {{project_id: $project_id}})"
                        f"   {domain_merge_cypher(id_param='row.id')}"
                        f"   SET d.name = row.name"
                        f"   MERGE (n)-[:{ONT.domain_of}]->(d)"
                        f"   MERGE (d)-[:{ONT.project_of}]->(dp))",
                        pg_id=pg_id, domains=domain_ids, project_id=project_id,
                    )
                    written = len(domain_ids)
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM neo4j_outbox WHERE id=$1", outbox_id)
        log.info(
            "outbox: backfilled %s pg_id=%s edges=%d mode=%s (outbox_id=%d, row deleted)",
            ONT.domain_of, pg_id, written, "inherit" if inherit else "explicit",
            outbox_id,
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

    async def _project_ingress_error(self, metadata: dict, agent_id: str) -> dict | None:
        """The whole project-ingress rule (P4, P9). Returns the 400 body, or None
        when the save may proceed. Registers the project as a side effect when the
        caller declares it new — that IS the acceptance.
        """
        # Scope: RETROSPECTIVES only. They arrive on their own endpoint and
        # inherit the project of the decision they judge, which passed this
        # check itself — so re-checking here would demand a value the caller
        # never supplies. DECISIONS were excluded alongside them until v0.8.44
        # and should not have been: presence was mistaken for validity, and an
        # unregistered name reached the graph as a project node. See the call
        # site in handle_save.
        if metadata.get("type") == "retrospective":
            return None

        # ⚠ The check is on `project`, never on a chain. A record carrying only a
        # `domain` is NOT accepted as tagged: a domain is a SECTION of a project,
        # so accepting it would let a part vouch for the whole.
        supplied = resolve_project(metadata)
        if not supplied:
            return await self._project_rejection("project_required", None)

        # The sentinel is a legitimate answer, not a bypass: it saves, searches
        # and enriches, and is simply never folded as a subject (P5).
        if supplied == SENTINEL:
            return None

        if await self._project_registered(supplied):
            return None

        # A retired spelling resolves to the name that replaced it, and the
        # record is stored under the CANONICAL name. This is what makes a rename
        # durable: a folder on another machine still carries the old name, and
        # without this the next save from it recreates the variant the merge
        # just removed. Rewriting here rather than resolving in every reader is
        # the same choice PROJECT_SQL made — one resolution, at ingress.
        canonical = await self._resolve_project_alias(supplied)
        if canonical is not None:
            log.info("project alias: %r → %r (record stored as the canonical name)",
                     supplied, canonical)
            # EVERY carrier holding the retired spelling moves — not just the
            # field the alias was read from. Rewriting one and not the other
            # leaves a record whose Postgres metadata and graph axis disagree
            # about which project it belongs to. Only fields equal to the
            # resolved spelling are touched, so a carrier naming a different
            # project is never clobbered.
            if metadata.get("project") == supplied:
                metadata["project"] = canonical
            if isinstance(metadata.get("decision"), dict) and \
                    metadata["decision"].get("project") == supplied:
                metadata["decision"]["project"] = canonical
            return None

        # P9 — the second submission is ACCEPTED, in any of its three forms: pick
        # a proposal (now a registry hit), declare a new project, or park it on
        # the sentinel. There is deliberately NO round counter on the server: the
        # bound comes from those three forms all succeeding, not from per-caller
        # state a gateway would have to keep and expire. What the gateway never
        # does, however many times it is asked, is accept an unregistered name.
        if metadata.get("new_project") is True:
            # ⛔ A DECLARATION IS NOT A DEFENCE. The agent that sets this flag is
            # the same agent that makes the spelling error, so accepting the
            # claim on its own guards nothing: the operator says "go ahead with
            # this idea", meaning THIS project, and a plausible variant becomes a
            # second one. Every retired spelling in this registry arrived that
            # way. So the claim faces the two checks below before it registers.
            refusal = await self._new_project_refusal(supplied, metadata)
            if refusal is not None:
                return refusal
            await self._register_project(supplied, agent_id)
            log.info("project registry: %r registered by %s (new_project, "
                     "record type %s)", supplied, agent_id,
                     metadata.get("type") or "fact")
            return None

        return await self._project_rejection("project_unknown", supplied)

    async def _new_project_refusal(self, supplied: str, metadata: dict) -> dict | None:
        """Why this NEW-project declaration must not register — or None (P23).

        Two checks, and only the second can be overridden, because they are
        different claims about the world:

        **A spelling of a registered project is never a new project.** Names
        differing only in separators and case reduce to one spelling key, and no
        confirmation can make them distinct — the caller is told the registered
        spelling to use. This is not a judgement call: every rename this registry
        has recorded was exactly this shape.

        **A CONFUSABLE name is refused once and can be confirmed.** Above the
        similarity floor the caller must name the registered project it means to
        differ from. Naming it, rather than setting a second boolean, is the
        point: a flag can be flipped without reading anything, while the name of
        the neighbour cannot be produced without having seen it — which is what
        puts the decision in front of the operator instead of inside the agent.

        ⚠ Neither check refuses a genuinely new project outright, and that is
        deliberate. Work legitimately starts with an idea, a fact, and a decision
        to act on it, before any project exists. What must not happen is that a
        project starts because a name was mistyped.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(CONFUSABLE_SQL, supplied,
                                    CONFUSABLE_SIMILARITY, PROPOSAL_LIMIT)
            # ⚠ THE SPELLING CHECK RUNS OVER THE WHOLE REGISTRY, NOT OVER `near`.
            # It used to read the trigram neighbours, which silently made an
            # EXACT rule conditional on a FUZZY one: a separator/case variant was
            # refused only when it also scored above the similarity floor.
            # Measured live — `testing` vs `Test_Ing` scores 0.545 against a
            # floor of 0.6 — so a pure spelling variant registered as new,
            # which is precisely the event this guard exists to prevent.
            all_names = [r["name"] for r in await conn.fetch(PROJECT_NAMES_SQL)]
        near = [r["name"] for r in rows]

        variant = spelling_variant_of(supplied, all_names)
        if variant is not None:
            log.info("project registry: refused %r — a spelling of registered %r",
                     supplied, variant)
            return {
                "status": "error",
                "error": "project_spelling_variant",
                "message": (
                    f"project {_short(supplied)} differs from the registered project "
                    f"{_short(variant)} only in separators or capitalisation, so it is a "
                    f"SPELLING of it and not a new project. Save under {_short(variant)}. "
                    "If the project genuinely needs to be renamed, that is a "
                    "deliberate operation with its own tool and ledger, never a "
                    "side effect of a save."
                ),
                "proposals": near,
            }

        unconfirmed = unconfirmed_confusables(
            near, metadata.get("confirm_distinct_from"))
        if unconfirmed:
            log.info("project registry: %r held for confirmation against %s",
                     supplied, unconfirmed)
            return {
                "status": "error",
                "error": "project_confusable",
                "message": (
                    f"project {_short(supplied)} is close enough to an existing project to "
                    f"be a typo for it: {unconfirmed}. ASK THE OPERATOR whether this "
                    "is genuinely a separate project. If it is, re-send with "
                    "metadata.confirm_distinct_from listing the projects above; if it "
                    "is not, save under the existing name. Registering a variant is "
                    "how one project quietly becomes two."
                ),
                "proposals": near,
            }
        return None

    # ── Domain ingress (P17, migration 028) ──────────────────────────────────

    async def _domain_ingress_error(
        self, metadata: dict, agent_id: str
    ) -> dict | None:
        """The whole domain-ingress rule. Returns the 400 body, or None when the
        save may proceed. Registers a domain the caller declares new, exactly as
        the project protocol does — that IS the acceptance.

        ⚠ IT RUNS AFTER THE PROJECT CHECK, and the order is a dependency rather
        than a preference: a domain is a section of a project, so there is
        nothing to resolve it against until the project has been established.
        By the time this runs, `metadata` carries the CANONICAL project name (an
        alias has already been rewritten), which is what makes the registry
        lookup below reach the right sections.

        ⛔ WHICH RECORD CONTROLS WHICH AXIS — the rule this enforces, and the
        reason a retrospective is refused while a decision is not:

          FACT           project OWN · domain OWN · mints its own entities
          DECISION       project OWN · domain OWN · entities INHERITED from the
                         facts it grounds in
          RETROSPECTIVE  project and domain BOTH from the DECISION it judges ·
                         entities inherited from its grounding facts

        A decision is an axis-asserting record. It already self-asserts its
        project and is registry-checked on it, and a section is the same kind of
        claim about the same thing — so it takes the same path a fact does,
        including registering a new section under the naming guards. What a
        decision does NOT control is what it is ABOUT: its topics come from its
        evidence.

        ⛔ AND THIS IS WHY IT CANNOT INHERIT, which was the obvious design and is
        wrong: A DECISION REACHES FURTHER THAN THE FACT THAT PROMPTED IT. A fact
        observes that agents write to the graph directly — an `infrastructure`
        observation. The decision it provokes may govern which agents are
        AUTHORISED to write, which is about tokens and access and sits above the
        infrastructure it was prompted by. Inheriting would file that decision
        only where its evidence was, so the section that most needs to surface it
        never would. The scope of a judgement is not the scope of its evidence,
        and only the person making it knows the difference.

        A retrospective controls neither axis. Its project has always come from
        the decision it judges, and its domain now follows the same route, so a
        verdict is always filed with what it judges rather than with the later
        evidence that measured it. A retrospective supplying a domain is
        therefore refused — silently stripping it was the alternative and it is
        worse: the save succeeds, the caller sees no complaint, and the agent
        goes on sending a field that has never once had an effect.
        """
        record_type = doc_record_type(metadata)
        if record_type == "retrospective":
            if names_a_domain(metadata):
                return self._domain_on_judgement_rejection(record_type)
            return None

        supplied = resolve_domains(metadata)
        if not supplied:
            return None

        # The sentinel parks a record OUTSIDE any project, so it has no sections
        # to be a member of. Refusing here rather than looking up a registry that
        # cannot answer keeps the message about the real mistake.
        project = resolve_project(metadata)
        if project == SENTINEL:
            return {
                "status": "error",
                "error": "domain_without_project",
                "message": (
                    f"a domain is a SECTION of a project, and this record is parked "
                    f"on {SENTINEL!r} — which is not a project and has no sections. "
                    "Either save it under the project whose section this is, or drop "
                    "the domain."
                ),
            }

        project_id = await self._project_identity(project)
        if project_id is None:
            # The project passed its own check, so this is not an unregistered
            # name — it is a lookup that failed. Refusing the save would turn a
            # transient database problem into a rejected record; accepting it
            # keeps the value in Postgres, where the backfill can reach it.
            log.warning("domain ingress: no identity for project %r — accepting the "
                        "record with its domain unvalidated and unlinked", project)
            return None

        for name in supplied:
            error = await self._domain_value_error(
                name, project, project_id, metadata, agent_id)
            if error is not None:
                return error
        return None

    async def _domain_value_error(
        self, name: str, project: str, project_id: int,
        metadata: dict, agent_id: str,
    ) -> dict | None:
        """One domain value, through the same protocol a project name faces.

        Registered → accepted. A retired spelling → rewritten to the canonical
        section and accepted. Otherwise the caller is told, with proposals, and a
        second submission declaring `new_domain` registers it — subject to the
        same two naming guards a new project faces (decision 1048), because the
        agent that sets the flag is the agent that makes the spelling error.
        """
        if await self._domain_registered(project_id, name):
            return None

        canonical = await self._resolve_domain_alias(project_id, name)
        if canonical is not None:
            log.info("domain alias: %r → %r in project %r (record stored as the "
                     "canonical name)", name, canonical, project)
            self._rewrite_domain(metadata, name, canonical)
            return None

        if metadata.get("new_domain") is True:
            refusal = await self._new_domain_refusal(name, project, project_id, metadata)
            if refusal is not None:
                return refusal
            await self._register_domain(project_id, name, agent_id)
            log.info("domain registry: %r registered under project %r by %s "
                     "(new_domain)", name, project, agent_id)
            return None

        return await self._domain_rejection(name, project, project_id)

    async def _new_domain_refusal(
        self, name: str, project: str, project_id: int, metadata: dict,
    ) -> dict | None:
        """Why this NEW-domain declaration must not register — or None.

        The project axis' two checks (decision 1048), scoped to one project's
        sections. A separator/case variant of a section this project already has
        is a SPELLING of it and no confirmation can make it distinct; a merely
        confusable name is held once and can be confirmed by naming the section
        it means to differ from.
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(DOMAIN_CONFUSABLE_SQL, project_id, name,
                                    DOMAIN_CONFUSABLE_SIMILARITY,
                                    DOMAIN_PROPOSAL_LIMIT)
            # Every section of THIS project, not just the trigram neighbours —
            # see the identical note on the project axis above.
            all_names = [r["name"]
                         for r in await conn.fetch(DOMAIN_NAMES_SQL, project_id)]
        near = [r["name"] for r in rows]

        variant = spelling_variant_of(name, all_names)
        if variant is not None:
            log.info("domain registry: refused %r — a spelling of %r in project %r",
                     name, variant, project)
            return {
                "status": "error",
                "error": "domain_spelling_variant",
                "message": (
                    f"domain {_short(name)} differs from {_short(variant)}, already a section of "
                    f"{_short(project)}, only in separators or capitalisation — so it is a "
                    f"SPELLING of it and not a new section. Save under {_short(variant)}."
                ),
                "proposals": near,
            }

        unconfirmed = unconfirmed_confusables(
            near, metadata.get("confirm_distinct_from"))
        if unconfirmed:
            log.info("domain registry: %r held for confirmation against %s in "
                     "project %r", name, unconfirmed, project)
            return {
                "status": "error",
                "error": "domain_confusable",
                "message": (
                    f"domain {_short(name)} is close enough to a section {_short(project)} "
                    f"already has to be a typo for it: {unconfirmed}. ASK THE "
                    "OPERATOR whether this is genuinely a separate section. If it "
                    "is, re-send with metadata.confirm_distinct_from listing the "
                    "sections above; if it is not, save under the existing name."
                ),
                "proposals": near,
            }
        return None

    @staticmethod
    def _rewrite_domain(metadata: dict, old: str, new: str) -> None:
        """Replace one domain value in place, under whichever key carried it.

        The record is stored under the CANONICAL section name for the same
        reason a project alias is rewritten at ingress: resolving on every read
        instead would leave the retired spelling in the data forever, and the
        next save from the same source would recreate it.

        ⚠ It rewrites the DECISION BLOB as well as the top level, because that is
        where a decision carries its axis values — the same two places
        `resolve_domains` reads. A rewriter that reached fewer places than the
        resolver would leave the old spelling in the half nobody rewrote, which
        is exactly the shadowed-field defect `PROJECT_MATCH_SQL` exists to warn
        about.
        """
        for blob in (metadata, metadata.get("decision")):
            if not isinstance(blob, dict):
                continue
            for key in DOMAIN_KEYS:
                value = blob.get(key)
                if isinstance(value, str) and value.strip() == old:
                    blob[key] = new
                elif isinstance(value, (list, tuple)):
                    blob[key] = [
                        new if isinstance(v, str) and v.strip() == old else v
                        for v in value
                    ]

    async def _domain_identities(
        self, pg_id: int, project_id, raw_domains,
    ) -> list[dict]:
        """Resolve a record's domain NAMES to [{id, name}] for the graph write.

        Returns only the sections the registry can identify, in the order they
        were named. A name that resolves to nothing is LOGGED and dropped — the
        no-name-keyed-fallback invariant — and the value stays verbatim in the
        record's Postgres metadata, so `backfill_domain_of.py` can write the edge
        once the section is registered.

        ⚠ AN UNRESOLVED NAME IS NOT NORMAL HERE. Ingress refuses an unregistered
        domain, so by the time a row is applied the registry has already
        answered. Reaching this path means something changed underneath the row
        — a section deleted between enqueue and apply, or a row enqueued by a
        tool rather than by ingress — which is exactly why it gets a warning
        rather than a debug line.
        """
        names = [n for n in (raw_domains or []) if isinstance(n, str) and n.strip()]
        if not names or project_id is None:
            if names:
                log.warning("outbox: pg_id=%s names %d domain(s) but its project has "
                            "no registry identity — no %s edge written",
                            pg_id, len(names), ONT.domain_of)
            return []
        out: list[dict] = []
        for name in names:
            domain_id = await self._domain_identity(project_id, name)
            if domain_id is None:
                log.warning("outbox: domain %r is not a registered section of "
                            "project id %s (pg_id=%s) — no %s edge written; the "
                            "value is kept in the record's metadata",
                            name, project_id, pg_id, ONT.domain_of)
                continue
            out.append({"id": domain_id, "name": name.strip()})
        return out

    async def _domain_registered(self, project_id: int, name: str) -> bool:
        """Is this an established section of that project? (migration 028.)"""
        async with self._acquire() as conn:
            return await conn.fetchval(DOMAIN_EXISTS_SQL, project_id, name) is not None

    async def _domain_identity(self, project_id, name) -> int | None:
        """The registry id behind (project, section name), or None.

        Uncached for the same reason ``_project_identity`` is: one indexed lookup
        on a path already writing to two stores, and a cache would hold a stale
        answer across exactly the operation an identity exists to survive.

        None takes the write down the no-edge path — there is deliberately no
        name-keyed rescue on this axis. See ``domain_merge_cypher``.
        """
        if project_id is None or not isinstance(name, str) or not name.strip():
            return None
        try:
            async with self._acquire() as conn:
                return await conn.fetchval(DOMAIN_EXISTS_SQL, project_id, name.strip())
        except Exception as exc:
            log.warning("domain identity lookup failed for %r in project id %s, "
                        "the record keeps its domain and gets no edge: %s",
                        name, project_id, exc)
            return None

    async def _resolve_domain_alias(self, project_id: int, name: str) -> str | None:
        """The canonical section a retired spelling resolves to, or None.

        ONE lookup, never a walk, and scoped to the project — the same shape as
        the project alias resolver, for the same two reasons: chains are
        collapsed when a rename is written, and a walk on the ingress path can
        cycle. Failure is treated as "not an alias" so an error here produces the
        ordinary rejection rather than a 500.
        """
        try:
            async with self._acquire() as conn:
                return await conn.fetchval(DOMAIN_ALIAS_RESOLVE_SQL, project_id, name)
        except Exception as exc:
            log.warning("domain alias lookup failed for %r, treating as unknown: %s",
                        name, exc)
            return None

    async def _register_domain(self, project_id: int, name: str, agent_id: str) -> None:
        """Register a section the caller declared new.

        No description, for the same reason a new project gets none: it is owed
        from the operator, and a placeholder would claim one was supplied. On
        this axis that costs more than on the project axis — descriptions are
        half of how domain proposals work — so an undescribed section is a real,
        visible gap rather than a cosmetic one.
        """
        async with self._acquire() as conn:
            await conn.execute(DOMAIN_REGISTER_SQL, project_id, name,
                               agent_id or "unknown")

    async def _domain_proposals(self, project_id: int, name: str) -> list[str]:
        """Sections of THIS project near a value that missed — by name or by
        description. The description half is what lets an operator reach a
        section whose name they could not have guessed."""
        async with self._acquire() as conn:
            rows = await conn.fetch(
                DOMAIN_PROPOSALS_SQL, project_id, name,
                DOMAIN_PROPOSAL_SIMILARITY, DOMAIN_PROPOSAL_LIMIT,
            )
        return [r["name"] for r in rows]

    def _domain_on_judgement_rejection(self, record_type: str) -> dict:
        """The 400 a retrospective gets for naming a domain.

        Only a retrospective reaches this. A decision self-asserts both axes and
        goes down the ordinary registry path — see `_domain_ingress_error` for
        which record controls what.
        """
        return {
            "status": "error",
            "error": "domain_not_allowed_on_judgement",
            "message": (
                f"a {record_type} does not name its own domain, for the same reason "
                "it does not name its own project: both come from the DECISION it "
                "judges, so a verdict is always filed with what it judges. Remove "
                "the field and save again. If the section is wrong, it is wrong on "
                "the decision — fix it there and this record follows."
            ),
        }

    async def _domain_rejection(
        self, name: str, project: str, project_id: int
    ) -> dict:
        """The 400 body for an unregistered section. One status code, so a client
        branches on `error`; the message tells the model to ASK rather than
        infer, because a plausible wrong section is a record filed under a name
        nobody will think to look in."""
        proposals = await self._domain_proposals(project_id, name)
        body = {
            "status": "error",
            "error": "domain_unknown",
            "message": (
                f"domain {_short(name)} is not a registered section of project {_short(project)}. "
                "Either it is a typo for one of the proposals, or it is a new "
                "section, in which case re-send with metadata.new_domain = true to "
                "register it. ASK THE OPERATOR which, rather than picking for them. "
                "A record needs no domain at all — leaving it off files the record "
                "under its project, which is always correct."
            ),
        }
        if proposals:
            body["proposals"] = proposals
        return body

    # ── Entity vocabulary ingress (fact:1375, migration 033) ──────────────────
    #
    # The whole gate lives in `_entity_ingress_error`, called from both writers
    # of caller-supplied entity names — handle_save (facts and decisions share
    # this generic path) and handle_retrospective (its own endpoint, its own
    # `entities` field). See that method's docstring for the full rule; the
    # two methods below are its DB-facing primitives, kept separate so a test
    # can stub either one without reimplementing the gate's control flow.

    async def _entity_vocab_resolve(self, name: str) -> str | None:
        """The canonical spelling `name` resolves to via `entity_vocabulary` +
        `entity_vocab_aliases` (migration 033's `entity_normalize` match), or
        None if unregistered. Deliberately UNCACHED, one lookup per name — the
        same choice `_project_identity`/`_domain_identity` make, for the same
        reason: this sits on a path already writing to two stores, and no
        measurement justifies a cache here (fact:1338 — an unmeasured cache
        size/TTL is a measurement claim in disguise).
        """
        async with self._acquire() as conn:
            return await conn.fetchval(ENTITY_VOCAB_RESOLVE_SQL, name)

    async def _entity_vocab_mint(self, name: str, agent_id: str) -> str:
        """Mint NAME as a new canonical — the ONLY path that ever inserts into
        `entity_vocabulary` (rule 2, lookup-never-create everywhere else).
        Creates the canonical alone, no alias (rule 5 — alias curation stays a
        manual, operator-only act, decision:1380). Attribution is the save's
        own agent identity, in `registered_by` — the same column the 033 seed
        carried over from `entity_registry.registered_by`.

        Returns the canonical NAME actually on record: `name` itself on a
        clean mint, or whatever canonical already claims this normalized key
        if two mints race — `ON CONFLICT (normalized_key) DO NOTHING` lets
        Postgres's own unique index arbitrate that, same-table, exactly as the
        migration's seed relies on for its own idempotency. No
        application-level lock is added here: this path NEVER inserts into
        `entity_vocab_aliases`, so it cannot produce the CROSS-table race the
        migration's trigger comment warns about — that race needs a
        concurrent alias insert, which stays a separate, manual, operator-only
        act per decision:1380 and is not something this gate (or any current
        writer) performs. See the handoff's N-3/trigger-race disposition.
        """
        async with self._acquire() as conn:
            row = await conn.fetchrow(ENTITY_VOCAB_MINT_SQL, name, agent_id or "unknown")
        if row is not None:
            return row["name"]
        resolved = await self._entity_vocab_resolve(name)
        return resolved if resolved is not None else name

    @staticmethod
    def _entity_unknown_rejection(unknown: list[str]) -> dict:
        """The 400 body for one or more entity names the vocabulary does not
        know. A refusal is a QUESTION for the operator, never a silent drop or
        auto-registration (rule 4) — the message says exactly how to answer
        it, mirroring the project/domain rejections' "ASK THE OPERATOR" shape.
        """
        plural = len(unknown) != 1
        return {
            "status": "error",
            "error": "entity_unknown",
            "message": (
                f"entit{'ies' if plural else 'y'} "
                f"{', '.join(_short(n) for n in unknown)} "
                f"{'are' if plural else 'is'} not in the entity vocabulary. "
                "ASK THE OPERATOR whether each is a genuinely new concept or a "
                "spelling of an existing one. If it is new, re-send with "
                "metadata.new_entities listing exactly these names (each must "
                "also appear in metadata.entities) to mint it as a canonical "
                "spelling; if it is a spelling of something that already "
                "exists, save under the registered canonical name instead."
            ),
            "unknown_entities": unknown,
        }

    @staticmethod
    def _rewrite_entities(metadata: dict, resolved: dict[str, str]) -> None:
        """Replace every entity name the gate canonicalized, IN PLACE,
        everywhere it is carried: `entities` itself and, if present, the KEYS
        of `entities_provenance` — which must keep naming exactly the values
        in `entities`, or its own "not in this save's entities list" check
        would spuriously fire on a name this gate just rewrote. A name
        mapping to itself (already canonical) is a harmless no-op replace.

        Names `sanitize_entity_name` rejected never entered `resolved` (the
        gate only resolves its `candidates` — sanitize's survivors — see
        `_entity_ingress_error`) and so are left untouched here: verbatim,
        exactly as Tier 1 has always stored them (`sanitize_entity_name`'s own
        contract — "governs what reaches the GRAPH, never what is stored").
        """
        if not resolved:
            return
        entities = metadata.get("entities")
        if isinstance(entities, list):
            metadata["entities"] = [
                resolved.get(e, e) if isinstance(e, str) else e for e in entities
            ]
        provenance = metadata.get("entities_provenance")
        if isinstance(provenance, dict):
            metadata["entities_provenance"] = {
                resolved.get(k, k): v for k, v in provenance.items()
            }

    async def _entity_ingress_error(self, metadata: dict, agent_id: str) -> dict | None:
        """The whole save-time entity ingress gate (fact:1375). Returns the
        400 body, or None when the save may proceed — having already
        rewritten `metadata['entities']` (+ `entities_provenance` keys) to the
        CANONICAL spelling in place, the same "resolve once at ingress, store
        the canonical" choice `_project_ingress_error`/`_domain_value_error`
        make for their own axes (rule 3).

        Runs on BOTH writers of caller-supplied entity names: handle_save
        (facts and decisions share this generic path — a decision's `entities`
        stays Tier-1-only and is never minted into the graph, but it DOES
        reach Postgres metadata, so it is in scope for canonicalization) and
        handle_retrospective (its own endpoint, its own `entities` field).
        Retrospectives also never mint into the graph (`_inherit_domains`/the
        v2 outbox row writes no MENTIONS edge for them either), but the same
        Tier-1-reaches-metadata reasoning applies.

        ⛔ ENTITIES STAY OPTIONAL (fact:1215) — an empty/absent list returns
        None immediately, before any lookup or `new_entities` validation. The
        gate must never affect consolidation eligibility, which keys on
        project+domain, never on entities.

        Scope is deliberately narrower than "every string in entities": only
        names `sanitize_entity_name` (ontology.py) would treat as a genuine
        entity candidate are checked against the vocabulary — rule 7's
        "additive AFTER it, not a replacement" taken literally. A name
        sanitize would reject as noise (a leaked pg_id, a bare number, an
        axis declaration, ...) is not a candidate for canonicalization
        either: it is left exactly where it already lived, verbatim, in
        Postgres metadata, and the outbox→graph gate (`_gate_graph_entities`)
        still filters it out before the graph, unchanged. Asking "is `12345`
        a registered entity" makes no sense, and refusing an entire save over
        it would be a regression from today's silent-drop behaviour, which
        this gate must not cause. A side effect of this scoping: every name
        this gate ever mints has ALREADY passed `sanitize_entity_name` (mint
        only sees `candidates`, sanitize's survivors), so a canonical can
        never enter the vocabulary in a shape the graph gate would later
        reject — see EG_LEG1_HANDOFF.md's invariant list.
        """
        raw_entities = metadata.get("entities") or []
        candidates = sanitize_entity_names(raw_entities)
        if not candidates:
            return None

        new_entities_raw = metadata.get("new_entities")
        if new_entities_raw is not None:
            if not isinstance(new_entities_raw, list) or not all(
                isinstance(n, str) for n in new_entities_raw
            ):
                return {
                    "status": "error",
                    "error": "new_entities_invalid",
                    "message": "metadata.new_entities must be a list of strings.",
                }
        mint_requested = set(new_entities_raw or [])

        resolved: dict[str, str] = {}
        unknown: list[str] = []
        for name in candidates:
            canonical = await self._entity_vocab_resolve(name)
            if canonical is not None:
                resolved[name] = canonical
            else:
                unknown.append(name)

        if unknown:
            to_mint = [n for n in unknown if n in mint_requested]
            still_unknown = [n for n in unknown if n not in mint_requested]
            if still_unknown:
                return self._entity_unknown_rejection(still_unknown)
            for name in to_mint:
                canonical = await self._entity_vocab_mint(name, agent_id)
                resolved[name] = canonical
                log.info("entity vocabulary: %r minted as canonical %r by %s "
                         "(new_entities)", name, canonical, agent_id)

        self._rewrite_entities(metadata, resolved)
        return None

    async def _project_registered(self, name: str) -> bool:
        """Is this an established project? (P4, migration 022's registry.)"""
        async with self._acquire() as conn:
            return await conn.fetchval(PROJECT_EXISTS_SQL, name) is not None

    async def _project_identity(self, project) -> int | None:
        """The registry id behind a project name, or None (migration 027).

        Deliberately UNCACHED. The registry is tens of rows and this is one
        indexed lookup on a path that is already writing to two stores; a cache
        would buy nothing measurable and would hold a stale answer across
        exactly the operation the identity exists to survive — a rename.

        None means "no identity to key on", from any cause: an unregistered
        name, or a lookup that failed. Both take the write down the same
        name-keyed fallback rather than losing the edge — see
        ``project_merge_cypher``. A failure here must never turn a save into a
        500, because the record and its project are both already valid.
        """
        if not isinstance(project, str) or not project.strip():
            return None
        try:
            async with self._acquire() as conn:
                return await conn.fetchval(PROJECT_ID_SQL, project.strip())
        except Exception as exc:
            log.warning("project identity lookup failed for %r, writing the "
                        "project node keyed on its name: %s", project, exc)
            return None

    async def _resolve_project_alias(self, name: str) -> str | None:
        """The canonical project a retired spelling resolves to, or None.

        ONE lookup, never a walk (A3). Chains exist in this corpus — one project
        has been spelled three ways across two machines — but they are collapsed
        when the rename is WRITTEN, so every active alias points directly at a
        canonical name. Following links here would put a graph walk on the
        ingress path, and a walk can cycle.

        Failure is treated as "not an alias" rather than propagated: this sits
        between the registry check and the 400, so an error here must produce
        the ordinary rejection an unknown project already gets, not a 500 on a
        save that was merely using an unregistered name.
        """
        try:
            async with self._acquire() as conn:
                return await conn.fetchval(
                    ALIAS_RESOLVE_SQL.format(p="$1"), name
                )
        except Exception as exc:
            log.warning("alias lookup failed for %r, treating as unknown: %s",
                        name, exc)
            return None

    async def _register_project(self, name: str, agent_id: str) -> None:
        """Register a project the caller declared new (P9's second form).

        No description: it is owed from the operator, and a placeholder would
        claim one was supplied. The sentinel can never arrive here — it short
        circuits above — and the schema's CHECK constraint keeps that true even
        if a future caller reaches this by another path.
        """
        async with self._acquire() as conn:
            await conn.execute(
                "INSERT INTO projects (name, created_by) VALUES ($1, $2)"
                " ON CONFLICT (name) DO NOTHING",
                name, agent_id or "unknown",
            )

    async def _project_proposals(self, name: str | None) -> list[str]:
        """Registry neighbours of a value that missed.

        ⚠ A DELIBERATE REVERSAL, recorded rather than slipped in: the earlier
        design returned no project names at all, reasoning that a list discloses
        the shape of other agents' work. Proposals disclose a
        relevance-filtered SLICE of that shape — far narrower than a full list —
        and without them a rejection is a dead end the caller cannot act on
        except by guessing. Accepted knowingly.
        """
        if not name:
            return []
        async with self._acquire() as conn:
            rows = await conn.fetch(
                PROJECT_PROPOSALS_SQL, name, PROPOSAL_SIMILARITY, PROPOSAL_LIMIT
            )
        return [r["name"] for r in rows]

    async def _project_rejection(self, error: str, supplied: str | None) -> dict:
        """The 400 body. One status code, so a client branches on `error` rather
        than on HTTP semantics — nothing here CONFLICTS, so 409 would be wrong.

        The message tells the model to ASK THE OPERATOR rather than infer. An
        agent that guesses a project produces a record filed under a plausible
        wrong name, which is worse than one that is parked: parked is visible and
        repairable, wrong is neither.
        """
        proposals = await self._project_proposals(supplied)
        if error == "project_required":
            message = (
                "metadata.project is required. The canonical value is the PROJECT "
                "FOLDER NAME, and the client derives it from the working directory "
                "— an empty value means the save was issued from outside any project "
                "root. ASK THE OPERATOR which project this belongs to rather than "
                "inferring one; a plausible wrong project is worse than none. If it "
                f"genuinely belongs to no project, send {SENTINEL!r}, which saves and "
                "searches normally but is never folded into a project's narrative."
            )
        else:
            message = (
                f"project {_short(supplied)} is not registered. Either it is a typo for an "
                "existing project — the proposals list near matches — or it is a new "
                "project, in which case re-send with metadata.new_project = true to "
                "register it. ASK THE OPERATOR which, rather than picking for them. "
                f"If it belongs to no project, send {SENTINEL!r}."
            )
        body = {"status": "error", "error": error, "message": message}
        if proposals:
            body["proposals"] = proposals
        return body

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
            decision_data = metadata.get("decision")
            decision_data = decision_data if isinstance(decision_data, dict) else {}
            # Required AND textual. A non-string that happens to be truthy (a JSON
            # client sending decided_by as a list) used to pass this check and then
            # be silently discarded by _normalise_decided_by, which can only keep a
            # string claim. Rejecting it here means the claim is never destroyed:
            # the caller is told the shape is wrong while it still has the value.
            missing = [
                f for f in ("decided_by", "project", "rationale")
                if not isinstance(decision_data.get(f), str)
                or not decision_data[f].strip()
            ]
            if missing:
                return web.json_response(
                    {
                        "status": "error",
                        "message": (
                            f"decision save missing or non-text required fields: {missing}. "
                            "Include a 'decision' object in metadata with "
                            "'decided_by', 'project', and 'rationale', each a "
                            "non-empty string."
                        ),
                    },
                    status=400,
                )
            # Canonicalise the person axis onto the attested principal — AFTER the
            # required-field check, so omitting decided_by still fails loudly
            # rather than being silently filled in from the socket.
            if _normalise_decided_by(metadata):
                log.info("decision ingress: decided_by normalised to principal %r "
                         "(claim %r preserved)", metadata["decision"]["decided_by"],
                         metadata["decision"].get("decided_by_claimed"))
                body["metadata"] = metadata

        # Project is REQUIRED and checked against the registry (P4) — on FACTS
        # and on DECISIONS alike.
        #
        # Unconditional — no env gate. A nullifiable invariant is not an invariant,
        # and the failure it guards against is silent: an untagged record saves
        # cleanly, searches cleanly, and simply never reaches synthesis.
        #
        # ⚠ The check is on `project`, never on a chain. A record carrying only a
        # `domain` is NOT accepted as tagged: a domain is a SECTION of a project,
        # so accepting it here would mean a part vouching for the whole.
        #
        # ⛔ DECISIONS WERE EXCLUDED FROM THIS UNTIL v0.8.44, and the reasoning
        # that excluded them conflated PRESENCE with VALIDITY: "decisions already
        # fail without decision.project". They do — but a present name that no
        # registry knows was accepted, and the outbox then minted a `:Project`
        # node for it. That is the one way the graph can end up holding a project
        # the registry does not have, and unlike the ingress→outbox window (which
        # leaves the graph BEHIND the registry, always safe) it does not resolve
        # itself. Registration is what makes a project an identity, so a record
        # that can create a node without one puts an unidentifiable project into
        # the axis that gates consolidation.
        #
        # ⚠ IT RUNS AFTER the decision-shape check, deliberately: a decision with
        # no project at all should still be told it is missing decided_by,
        # project and rationale together, rather than being rejected for the
        # project alone and coming back to discover the rest one at a time.
        #
        # RETROSPECTIVES stay out, and that one IS a scope statement rather than
        # an oversight: they arrive on their own endpoint and inherit the project
        # of the decision they judge — a decision that passed this very check.
        project_error = await self._project_ingress_error(metadata, agent_id)
        if project_error is not None:
            return web.json_response(project_error, status=400)

        # The domain axis (028), AFTER the project — a section cannot be resolved
        # before the project that contains it, and by here the project name is
        # canonical, so an aliased project reaches the right registry. A record
        # naming no domain passes straight through: most do, and that is correct
        # rather than untagged.
        domain_error = await self._domain_ingress_error(metadata, agent_id)
        if domain_error is not None:
            return web.json_response(domain_error, status=400)

        # Canonical top-level axis key (decision:1214): every OPERATOR-ASSERTED
        # axis lives at metadata TOP LEVEL on every record type — that is
        # already how a fact carries its `project`/`domain`, and it is the key
        # every reader that inspects Postgres directly (rather than resolving
        # through `resolve_domains`) should be able to trust. A decision's
        # asserted domains have only ever lived in the `decision` blob (the
        # client shape memory_bridge.py's build_decision_metadata has always
        # used), so materialise the SAME list to the top level here — after
        # ingress validation, so any alias rewrite has already landed on the
        # blob, and before the row is persisted.
        #
        # ⚠ ADDITIVE, NOT A REWRITE: the blob is left exactly as sent (payload
        # fidelity for existing clients — nothing threads a new field through
        # the CLI/MCP surface for this). Only decisions gain the top-level key;
        # a retrospective is refused before this point if it names a domain at
        # all (P17) and never populates a `decision` blob of its own, so it can
        # never reach this branch with a value.
        #
        # ⛔ WHY NO GATE WAS NEEDED FOR THE OUTBOX: `resolve_domains` (used to
        # build the outbox row's cypher_params["domains"] a few lines below)
        # reads the `decision` blob FIRST and only falls back to the top level
        # when the blob is empty (domain_axis.py). Since the blob already
        # carries this exact list, adding it at the top level changes nothing
        # `resolve_domains` returns for a decision — the outbox row is
        # unaffected, not double-written, and a judgement still never reaches
        # the graph write with a top-level-ONLY value. See
        # test_decision_domain_materialisation_does_not_change_the_outbox_row.
        if metadata.get("type") == "decision":
            decision_domains = resolve_domains(metadata)
            if decision_domains:
                metadata["domains"] = decision_domains

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
                    "SELECT superseded, metadata->>'type' AS type"
                    " FROM technical_docs WHERE id = $1", supersedes
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
            bad = _supersession_target_error(supersedes, target["type"])
            if bad:
                return web.json_response({"status": "error", "message": bad}, status=400)

        entities = metadata.get("entities", [])
        if not isinstance(entities, list):
            return web.json_response(
                {"status": "error", "message": "metadata.entities must be a list"},
                status=400,
            )

        # Entity vocabulary ingress gate (fact:1375, migration 033) — additive
        # AFTER sanitize_entity_name (rule 7), lookup-never-create (rule 2),
        # refuses an unknown name unless metadata.new_entities explicitly
        # mints it (rules 4/5). Rewrites `metadata['entities']` to the
        # CANONICAL spelling in place before anything downstream — locks, PG
        # metadata, the outbox row, entity_registry, the graph — sees this
        # list, so `entities` is re-read below.
        entity_error = await self._entity_ingress_error(metadata, agent_id)
        if entity_error is not None:
            return web.json_response(entity_error, status=400)
        entities = metadata.get("entities", [])

        # Per-entity provenance stamping (fact:1215) — additive, no api_version
        # bump. `entities_provenance` is an optional {name: "operator"|"agent"}
        # mapping saying, for each named entity, WHO named it: the operator
        # (an explicit, human-chosen concept) or the agent (proposed without
        # that confirmation). Validated at ingress so a malformed mapping fails
        # loudly rather than being stored verbatim and silently ignored; the
        # shape check is the whole job here — the values themselves are never
        # second-guessed against the record's content.
        entities_provenance = metadata.get("entities_provenance")
        if entities_provenance is not None:
            if not isinstance(entities_provenance, dict):
                return web.json_response(
                    {
                        "status": "error",
                        "error": "entities_provenance_invalid",
                        "message": (
                            "metadata.entities_provenance must be an object mapping "
                            "each named entity to 'operator' or 'agent'."
                        ),
                    },
                    status=400,
                )
            entity_set = set(entities)
            for name, value in entities_provenance.items():
                if name not in entity_set:
                    return web.json_response(
                        {
                            "status": "error",
                            "error": "entities_provenance_invalid",
                            "message": (
                                f"entities_provenance names {_short(name)}, which is not "
                                "in this save's entities list."
                            ),
                        },
                        status=400,
                    )
                if value not in ENTITIES_PROVENANCE_VALUES:
                    return web.json_response(
                        {
                            "status": "error",
                            "error": "entities_provenance_invalid",
                            "message": (
                                f"entities_provenance[{_short(name)}] = {_short(value)} — must be "
                                f"one of {sorted(ENTITIES_PROVENANCE_VALUES)}."
                            ),
                        },
                        status=400,
                    )
        # An entity named with no stated provenance is not an error — it is an
        # honest gap the response surfaces (see `entities_provenance_note`
        # below) so it is seen at capture time rather than only on inspection.
        entities_provenance_missing = bool(entities) and entities_provenance is None

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
        alt_stats: dict | None = None
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

                    # Caller 1 of the promotion writer: a parked fact cited as
                    # evidence inherits the project its citing judgements agree
                    # on. Fires here because this is where grounded pg_ids are
                    # already resolved to real records with real labels.
                    await self._promote_grounded_parked_facts(
                        conn, grounded_typed, agent_id, pg_id
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
                            # P3 + P8 — the resolved PROJECT, never a section and
                            # never a chain, and never the SENTINEL: a :Project node
                            # must be a project. A parked record saves, searches and
                            # enriches normally but puts no placeholder into the
                            # project set, where the insight gate's ">= 2 distinct
                            # projects" rule would count it as a subject.
                            "project": project_for_graph(metadata),
                            # The SECTIONS of that project this record sits in
                            # (028) — a list, because a record may sit in
                            # several. Carried as NAMES and resolved to registry
                            # ids by the worker, exactly as `project` is: the
                            # outbox row must stay replayable, and an id
                            # captured here would be a snapshot of the registry
                            # at enqueue time rather than at apply time.
                            # Judgements never reach this with a value — ingress
                            # refuses one, and their domains are inherited.
                            "domains": resolve_domains(metadata),
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

                    # Per-alternative rows, written with the record they belong
                    # to (migration 026). Text only — the embedding is filled by
                    # the background worker, so a decision that weighed eight
                    # options costs the same one embedding call on the save path
                    # as one that weighed none.
                    alt_stats = await self._reconcile_decision_alternatives(
                        conn, pg_id, metadata
                    )

                    # Wake the consolidation daemon
                    await conn.execute(
                        "SELECT pg_notify('new_artifact', $1)",
                        json.dumps({"pg_id": pg_id}),
                    )
        finally:
            for lk in acquired:
                lk.release()

        # Every durable row leaves a log line: a table that gained or lost rows
        # silently is one nobody can explain later. Logged after commit, so the
        # line means the rows are actually there.
        if alt_stats and (alt_stats["written"] or alt_stats["removed"]):
            log.info(
                "alternatives: pg_id=%d wants %d row(s) — %d written pending, %d removed",
                pg_id, alt_stats["desired"], alt_stats["written"], alt_stats["removed"],
            )

        # Neo4j is applied asynchronously by the outbox worker.
        # ?consistency=neo4j blocks until the row is marked applied.
        if request.rel_url.query.get("consistency") == "neo4j":
            applied = await self._wait_for_outbox(pg_id)
            neo4j_status = "applied" if applied else "timeout"
        else:
            neo4j_status = "pending"

        warn = save_response_warning(
            metadata.get("type"), entities, metadata.get("grounded_in")
        )
        superseded_pg_id = (
            supersedes if (supersedes is not None and supersedes != pg_id) else None
        )
        sup_msg = (
            f" Superseded fact {superseded_pg_id}." if superseded_pg_id is not None else ""
        )
        # entities_provenance_note (fact:1215): a separate advisory field, not
        # folded into `warn`/`message` — it answers a different question ("who
        # named these entities?") from the consolidation-eligibility note above,
        # and conflating the two would put a Tier-3 question and a provenance
        # question behind the same string. Present (non-null) only when the
        # save named entities but stated no provenance for any of them.
        entities_provenance_note = (
            "no entities_provenance stated — each named entity's origin"
            " (operator-named vs agent-added) is unknown."
            if entities_provenance_missing else None
        )
        return web.json_response({
            "status": "success",
            "pg_id": pg_id,
            "neo4j": neo4j_status,
            "superseded": superseded_pg_id,
            "message": f"Artifact stored with ID {pg_id}.{sup_msg}{warn}",
            "entities_provenance_note": entities_provenance_note,
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
                "SELECT superseded, metadata->>'type' AS type"
                " FROM technical_docs WHERE id = $1", pg_id
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
            bad = _supersession_target_error(pg_id, target["type"])
            if bad:
                return web.json_response({"status": "error", "message": bad}, status=400)
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

        # P17 on this endpoint too. It reads differently here and that is exactly
        # why it is needed: this handler BUILDS its own metadata from named body
        # fields, so a client-supplied domain never reaches storage anyway — it
        # is dropped on the floor with no complaint. Silence is what the rule is
        # against. A retrospective inherits the domains of the facts it grounds
        # in; being told so once is what stops an agent sending the field forever.
        if names_a_domain(body):
            return web.json_response(
                self._domain_on_judgement_rejection("retrospective"), status=400)

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
        new_entities_body = body.get("new_entities")
        if new_entities_body is not None:
            metadata["new_entities"] = new_entities_body
        # Entity vocabulary ingress gate (fact:1375) — the second of the two
        # writers `_entity_ingress_error` covers; see its docstring. Runs
        # before the embedding call so a refusal fails fast, and rewrites
        # `metadata['entities']` to the canonical spelling in place before the
        # locks loop below reads it.
        entity_error = await self._entity_ingress_error(metadata, agent_id)
        if entity_error is not None:
            return web.json_response(entity_error, status=400)
        if source_ref:
            metadata["source_ref"] = source_ref
        if body.get("elicited"):
            metadata["elicited"] = True
        grounded_ids = [
            g for g in (body.get("grounded_in") or [])
            if isinstance(g, int) and not isinstance(g, bool)
        ]
        # Grounding is REQUIRED of a retrospective, and this is the asymmetry
        # between the two judgement types. A decision may legitimately rest on
        # experience alone before its project has any evidence. A retrospective
        # cannot: it exists to report what MEASURING the outcome showed, so with
        # nothing measured it asserts a verdict from nowhere — and it is also the
        # route by which an ungrounded decision finally reaches topics, across
        # HAD_OUTCOME. An ungrounded retrospective therefore breaks two records,
        # not one, which is why this is a refusal and not a warning.
        if not grounded_ids:
            return web.json_response(
                {"status": "error",
                 "message": ("grounded_in is required on a retrospective — name the "
                             "pg_id(s) of the fact(s) that measured this outcome. A "
                             "verdict resting on nothing measures nothing, and it is "
                             "also what gives the decision it judges its topics. Save "
                             "the measurement as a fact first, then cite it here.")},
                status=400,
            )
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
                        f"SELECT id, metadata->>'type' AS type,"
                        f"       {PROJECT_SQL} AS project"
                        f" FROM technical_docs WHERE id=$1 FOR SHARE",
                        pg_id,
                    )
                    if not target:
                        return web.json_response(
                            {"status": "error", "message": f"No record found with pg_id={pg_id}"},
                            status=404,
                        )
                    # Inherit the target's project so domain-scoped reads see the
                    # retro beside its decision.
                    #
                    # ⚠ OPEN, deliberately not settled here: whether a
                    # retrospective's project MUST equal its decision's — i.e.
                    # whether it is derived rather than self-asserted.
                    #
                    # The case FOR it: a retrospective never needs to span
                    # projects, because a decision in one project affecting
                    # another is expressed as an INTERMEDIATE DECISION in the
                    # second project grounded in the first — and that decision
                    # carries its own same-project retrospective. Cross-project
                    # linkage then lives decision→decision, where it is explicit,
                    # rather than inside a retrospective, where it would be
                    # implicit. Both halves check out against the live corpus
                    # (2026-08-04): 0 of 162 retrospectives differ from their
                    # target, and the intermediate-decision pattern is already in
                    # use — 28 decision→judgement grounding links, one of them
                    # genuinely cross-project (a decision in one project grounded
                    # in a decision in another).
                    #
                    # If it is ratified, this line becomes unconditional — a
                    # PARKED target would then have to CLEAR the retro's own
                    # value rather than leave it standing — and the promotion
                    # writer gains a cascade, because it would then be a second
                    # writer of a derived value. Until that is decided, behaviour
                    # is left exactly as it was.
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

                    # Caller 1, retrospective side — a retrospective is a
                    # judgement too, and the plan is explicit that both kinds
                    # supply a project to the facts they cite.
                    await self._promote_grounded_parked_facts(
                        conn, grounded_typed, agent_id, retro_pg_id
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
                         "message": f"invalid operator label {_short(lab)} — "
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

    async def _attach_decision_payload(self, entries: list[dict]) -> None:
        """Fill every Decision neighbor's ``adr_props`` from POSTGRES, in ONE
        query for the whole batch. Mutates ``entries`` in place.

        WHY THIS IS NOT A GRAPH PROJECTION (the successor to decision 909).
        909 widened the expansion projection so a hit carried a folded
        decision's confidence/alternatives at zero extra query, accepting that
        deeper provenance stayed behind. Both values are payload: no Cypher
        anywhere filters, orders or matches on them — they are only ever
        rendered. A second copy of a value nobody walks on buys nothing the
        neighbor's ``pg_id`` does not already give, and guarantees a divergence
        class instead: the graph copy of ``alternatives`` silently missed 64%
        of decisions until a one-time sync repaired it, and ``confidence`` was
        measured in exactly that state (Postgres 236 vs graph 85, a clean
        cutover) at the time this shipped. Dereferencing removes the class
        rather than repairing an instance of it.

        The cost 909 was protecting is bounded and paid once per search, not
        per hit: the neighbors are already collected, so this is a single
        ``id = ANY(...)`` primary-key lookup, not an N+1.

        ⚠ FAIL-OPEN, exactly like the expansion it completes: graph context
        enriches a search and must never fail one. A payload error leaves the
        entries without ``adr_props`` and logs — it never propagates.
        """
        wanted = sorted({
            e["pg_id"] for e in entries
            if e.get("pg_id") is not None and e.get("label") == ONT.decision
        })
        if not wanted:
            return
        try:
            async with self._acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id,"
                    "       metadata->'decision'->'alternatives' AS alternatives,"
                    "       metadata->'decision'->>'confidence'  AS confidence"
                    " FROM technical_docs WHERE id = ANY($1::bigint[])",
                    wanted,
                )
            # Row handling lives INSIDE the guard on purpose: a helper that
            # fetches cannot be fail-open only for the fetch. Anything raised
            # while reading a row would otherwise propagate out of the
            # expansion — which is the whole failure mode being avoided.
            payload = {
                row["id"]: _decision_payload_props(
                    row["alternatives"], row["confidence"])
                for row in rows
            }
            for entry in entries:
                props = payload.get(entry.get("pg_id"))
                if props:
                    entry.setdefault("adr_props", {}).update(props)
        except Exception as exc:
            log.warning(
                "graph context: decision payload dereference failed for %d "
                "neighbor(s) — hits keep their graph context without it: %s",
                len(wanted), exc,
            )

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
                # LIFECYCLE EDGES RANK BY TYPE, NOT BY THE PROVENANCE STAMP.
                # The stamp was never a proxy for "structurally important" — it
                # means "something asserted this", and INHERITANCE is a something:
                # it writes MENTIONS with asserted_by='inherited'. A decision with
                # 31 such edges therefore buried its own HAD_OUTCOME (stamp null)
                # below the cap, so a reader could not see the decision had been
                # judged at all, and a retrospective did not surface the decision
                # it judges. Measured: 131 decisions carry a verdict, 6 had it
                # certainly hidden and 8 more at risk — growing as inheritance
                # stamps more edges.
                f" ORDER BY CASE WHEN type(r) IN ['{ONT.had_outcome}','{ONT.supersedes}',"
                f"                                '{ONT.grounded_in}','{ONT.informed_by}'] THEN 0"
                "               WHEN r.asserted_by IS NOT NULL THEN 1 ELSE 2 END,"
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
                # Evidence weight already sitting on this one-hop neighbor, so a
                # summary hit carries a folded fact's fact_kind/source_ref with
                # no second query (decision 909). A DECISION's confidence and
                # alternatives are NOT projected here — they are payload, and
                # `_attach_decision_payload` dereferences them from Postgres by
                # the pg_id this row already carries. Null on neighbors that do
                # not carry them; folded into `adr_props` below only when set.
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
        await self._attach_decision_payload(ctx)
        return ctx

    # Ratings that QUALIFY a decision — the reader needs the verdict's reasoning,
    # not just its name. `validated`/`pending` say the decision stands as written,
    # so the rating alone is enough.
    _QUALIFYING_RATINGS = ("refined", "mixed", "reversed")

    async def _resolve_decision_lifecycle(self, session, pg_ids: list[int]) -> dict:
        """Current lifecycle state of each decision, resolved in the GRAPH.

        ⛔ ORDERED BY pg_id, NEVER BY date. A Retrospective node carries `rating`
        and `pg_id` but NO `created_at`; its only temporal property is `date`,
        which is the OPERATOR-SUPPLIED outcome date, not a write time. Measured:
        19 of 27 multi-verdict decisions have DUPLICATE dates among their
        retrospectives — one holds a `mixed` and a `validated` on the same day —
        so `ORDER BY r.date` is non-deterministic. `pg_id` is a monotonic
        sequence already on the node, needs no join and no schema change, and
        reproduces the Postgres census exactly.

        ⚠ AND ONLY THE LATEST COUNTS, because lifecycle is NOT monotonic:
        measured sequences run `validated → refined → validated` and
        `refined → validated → validated`. A rule of "has a refined
        retrospective" would retire decisions that were later re-validated.

        Degrades to {} on any failure — this enriches a search, it never fails
        one."""
        if not pg_ids:
            return {}
        try:
            result = await session.run(
                "UNWIND $pg_ids AS pid"
                " CALL (pid) {"
                f"   MATCH (d:{ONT.decision} {{pg_id: pid}})"
                f"        -[:{ONT.had_outcome}]->(r:{ONT.retrospective})"
                "   RETURN r ORDER BY r.pg_id DESC LIMIT 1"
                " }"
                " RETURN pid AS pg_id, r.rating AS rating, r.pg_id AS retro_pg_id",
                pg_ids=pg_ids,
            )
            out: dict[int, dict] = {}
            async for rec in result:
                if rec["rating"] is None:
                    continue
                out[rec["pg_id"]] = {"rating": rec["rating"],
                                     "retrospective_pg_id": rec["retro_pg_id"]}
            return out
        except Exception:
            return {}

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
                # Lifecycle edges rank by TYPE — see the note on the
                # single-anchor query above; inheritance stamps MENTIONS, so
                # keying the first sort on asserted_by buried HAD_OUTCOME.
                f"   ORDER BY CASE WHEN type(r) IN ['{ONT.had_outcome}','{ONT.supersedes}',"
                f"                                  '{ONT.grounded_in}','{ONT.informed_by}'] THEN 0"
                "                 WHEN r.asserted_by IS NOT NULL THEN 1 ELSE 2 END,"
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
                # Evidence weight on the one-hop neighbor — see the single-anchor
                # form above (decision 909). Same projection, batched; a
                # decision's payload is dereferenced from Postgres, not here.
                "          related.fact_kind AS adr_fact_kind,"
                "          related.source_ref AS adr_source_ref,"
                "          aliases"
                " }"
                " RETURN pg_id AS anchor_pg_id, labels, name, rel_pg_id, rel_type,"
                "        direction, rel_props, snippet,"
                "        adr_fact_kind, adr_source_ref,"
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
        # ONE dereference for every anchor's neighbors together — the batching
        # this function exists for would be undone by a query per anchor.
        await self._attach_decision_payload(
            [entry for entries in out.values() for entry in entries])
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

        # Axis filters (v0.8.74) — a named place/time is a FILTER, not query
        # text. All three optional and additive; validated lightly (shape
        # only) — an unknown project/domain name is NOT refused here (the
        # read path never blocks on registry state), it simply matches
        # nothing via `_axis_filter_predicate` below.
        project = body.get("project")
        if project is not None and not isinstance(project, str):
            return web.json_response(
                {"status": "error", "message": "project must be a string"},
                status=400,
            )
        project = project.strip() if project else None

        domains_filter = body.get("domains")
        if domains_filter is not None:
            if not isinstance(domains_filter, list) or not all(
                    isinstance(d, str) for d in domains_filter):
                return web.json_response(
                    {"status": "error",
                     "message": "domains must be a list of strings"},
                    status=400,
                )
            # Security (PR 235): capped at ingress, not silently truncated — a
            # caller sending thousands of entries binds straight into the `?|`
            # operator (DoS vector), and a silent drop would let a partial
            # filter's empty result read as authoritative.
            if len(domains_filter) > SEARCH_DOMAINS_FILTER_CAP:
                return web.json_response(
                    {"status": "error", "error": "filters_invalid",
                     "message": (
                         f"domains carries {len(domains_filter)} entries, over "
                         f"the {SEARCH_DOMAINS_FILTER_CAP}-entry cap")},
                    status=400,
                )
            domains_filter = [d.strip() for d in domains_filter if d and d.strip()]
            if not domains_filter:
                domains_filter = None

        since_raw = body.get("since")
        since_dt = None
        if since_raw is not None:
            if not isinstance(since_raw, str):
                return web.json_response(
                    {"status": "error",
                     "message": "since must be an ISO date/datetime string"},
                    status=400,
                )
            try:
                since_dt = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
            except ValueError:
                return web.json_response(
                    {"status": "error",
                     "message": f"since is not a valid ISO date/datetime: {_short(since_raw)}"},
                    status=400,
                )

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                q_vec = await self._embed(query, client)
            except RuntimeError:
                q_vec = None

            if q_vec is None:
                # Keyword fallback when the embedding service is unavailable.
                # Axis filters apply here too — a filtered search must not
                # silently drop its filter just because the embedder is down.
                vis_sql, vis_params = _visibility_filter(viewer, scope, 3)
                axis_sql, axis_params = _axis_filter_predicate(
                    3 + len(vis_params), project, domains_filter, since_dt)
                async with self._acquire() as conn:
                    rows = await conn.fetch(
                        f"""
                        SELECT id, content, metadata FROM technical_docs
                        WHERE NOT superseded
                          AND (content ILIKE $1 OR metadata::text ILIKE $1)
                          AND {vis_sql} {axis_sql}
                        LIMIT $2
                        """,
                        f"%{query}%", limit, *vis_params, *axis_params,
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
                # Same axis predicate as Tier-1, computed once and reused by
                # every Tier-3 variant below (insight, thematic, and the
                # pre-006 fallback) — each is an independent query starting
                # its own $1, so the same fragment/params apply to all three.
                t3_axis_sql, t3_axis_params = _axis_filter_predicate(
                    2 + len(vis_t3_params), project, domains_filter, since_dt)
                insight = None
                try:
                    insight = await conn.fetchrow(
                        "SELECT id, content, metadata, source_pg_ids FROM community_summaries"
                        " WHERE NOT superseded"
                        "   AND metadata->>'kind' = 'insight'"
                        f"   AND {vis_t3} {t3_axis_sql}"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec), *vis_t3_params, *t3_axis_params,
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
                        f"   AND {vis_t3} {t3_axis_sql}"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec), *vis_t3_params, *t3_axis_params,
                    )
                except Exception:
                    log.warning(
                        "community_summaries.superseded column missing — "
                        "run migrations: uv run --with psycopg2-binary "
                        "python shared-memory/migrations/apply.py"
                    )
                    # Same rule as the Tier-1 fallback: the `kind` column may
                    # predate migration 006, but supersession is never dropped.
                    # PROVEN before this change — querying with a superseded
                    # summary's own text returned that summary from this branch
                    # while the guarded query correctly returned a live one.
                    summary = await conn.fetchrow(
                        "SELECT id, content, metadata, source_pg_ids FROM community_summaries"
                        f" WHERE NOT superseded AND {vis_t3} {t3_axis_sql}"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec), *vis_t3_params, *t3_axis_params,
                    )

                # Tier 1 — vector search. The pool handed to the reranker is a
                # DEFAULT FLOOR, never a ceiling on what the caller may ask for:
                # it was a hardcoded 20, so a caller requesting more than 20
                # silently received 20 while the endpoint advertised up to 100.
                # A default the caller can exceed is configuration; a limit the
                # caller cannot see is a defect.
                #
                # Retrieve-then-rerank also needs the pool to be at least as
                # large as the result set — reranking can only reorder what it
                # was given, so a pool equal to the limit makes the stage
                # pointless. The floor keeps small searches reranking from a
                # genuinely wider pool.
                pool = max(SEARCH_CANDIDATE_FLOOR, limit)
                # Reversed decisions (superseded=true, migration 009) are
                # excluded; the fallback keeps pre-migration schemas working.
                args: list = [str(q_vec), pool]
                scope_sql = ""
                if scope:
                    args.append(scope)
                    scope_sql = f"AND scope = ${len(args)}"
                vis_sql, vis_params = _visibility_filter(viewer, scope, len(args) + 1)
                args.extend(vis_params)
                # Axis filters applied to the CANDIDATE SET before reranking —
                # the reranker never sees a candidate that failed the filter.
                axis_sql, axis_params = _axis_filter_predicate(
                    len(args) + 1, project, domains_filter, since_dt)
                args.extend(axis_params)
                try:
                    candidates = await conn.fetch(
                        f"""
                        SELECT id, content, metadata, created_at FROM technical_docs
                        WHERE NOT superseded AND {vis_sql} {scope_sql} {axis_sql}
                        ORDER BY embedding <=> $1::vector LIMIT $2
                        """,
                        *args,
                    )
                except asyncpg.UndefinedColumnError:
                    # PRE-MIGRATION SCHEMA ONLY, and it FAILS CLOSED on the
                    # supersession guard: `created_at` may be absent on an old
                    # install, but `superseded` is not optional — a search that
                    # returns nothing is visibly broken, while one that quietly
                    # serves retired records is invisibly wrong. The previous
                    # bare `except` caught every error and dropped the guard
                    # with it, so any transient fault served superseded rows.
                    candidates = await conn.fetch(
                        f"""
                        SELECT id, content, metadata FROM technical_docs
                        WHERE NOT superseded AND {vis_sql} {scope_sql} {axis_sql}
                        ORDER BY embedding <=> $1::vector LIMIT $2
                        """,
                        *args,
                    )
                    # ⚠ Known gap, out of scope: a `since` filter needs
                    # `created_at` too, so it raises the same
                    # UndefinedColumnError a second time here, uncaught, on a
                    # schema this old. `since` is a brand-new v0.8.74 filter
                    # and migration 015 predates the whole axis system it
                    # filters on.

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
            # ⛔ TIER-3 NARRATIVES ARE CANDIDATES, NOT GUARANTEED POSITIONS.
            # They used to be fetched nearest-neighbour with LIMIT 1 and NO
            # distance floor, then placed ABOVE every fact without ever being
            # scored — so the two most prominent slots of every answer were held
            # by records that had never been required to be relevant, and a
            # summary was not ranked badly, it was not ranked at all.
            #
            # Measured before this change (10 queries, summary scored against the
            # same 20 facts): median rank 6 of 21, a NEGATIVE relevance score on
            # 6 of 10, and genuinely first on 2. So the guarantee was wrong on 8
            # of 10 — but the TIER is not noise, which is why summaries are
            # demoted into the contest rather than dropped from it. They keep the
            # top slot exactly when they earn it.
            t3_rows = [r for r in (insight, summary) if r is not None]
            n_t3 = len(t3_rows)

            # ONE candidate list: summaries first BY INDEX ONLY (so a hit can be
            # dispatched back to its kind), never by rank. Each document is
            # clamped to the relevance window before it is sent — the full text
            # is still what search RETURNS, only what the reranker SCORES is
            # bounded, the same relationship EMBED_MAX_CHARS has with
            # embed_ceiling.
            rerank_docs = [
                clamp_rerank_doc(r["content"] or "") for r in t3_rows
            ] + [
                clamp_rerank_doc(_rerank_doc_text(c, m, t))
                for c, m, t in zip(contents, metas, createds)
            ]
            reranked = False
            try:
                rr = await client.post(
                    RERANK_URL,
                    # `top_n` is what llama.cpp's /v1/reranking honours; `top_k`
                    # was silently IGNORED, so the server returned all 20
                    # candidates and the caller's limit was enforced only by the
                    # failure path. Both are sent because other reranking servers
                    # spell it differently — but neither is TRUSTED: the slice
                    # below is what actually enforces the contract.
                    json={"query": query, "documents": rerank_docs,
                          "top_n": limit, "top_k": limit},
                    # Derived from the payload, never constant — a constant
                    # under-provisions exactly the large sets that need it most.
                    timeout=rerank_ceiling(rerank_docs),
                )
                rr.raise_for_status()
                ranked = rr.json()["results"]
                reranked = True
            except Exception as exc:
                # FAILURE != IDLE. The fallback serves VECTOR order, which is a
                # different answer from a ranked one — so it is logged, counted
                # and declared in the response rather than dressed up as a
                # confident uniform score.
                log.warning("rerank failed (%s: %s) — serving vector order "
                            "for %d candidates", type(exc).__name__, exc,
                            len(rerank_docs))
                self._rerank_failures += 1
                # ⛔ THE FALLBACK DROPS TIER-3 ENTIRELY, and that is deliberate.
                # Vector order is meaningful only WITHIN one table: the fact
                # distances and the summary distances come from separate queries
                # and are never comparable. Emitting the combined list in index
                # order would put summaries first again — silently restoring the
                # guarantee this release removed, at the exact moment there is no
                # evidence to justify any position for them. When ranking is
                # unavailable the honest answer is the facts in their own order,
                # not a guess about where a narrative belongs.
                ranked = [
                    {"index": i + n_t3, "relevance_score": None}
                    for i in range(len(candidates))
                ]
            else:
                self._rerank_successes += 1
            # The caller's limit is enforced HERE, never delegated to the
            # reranking server. A server that ignores its truncation parameter
            # must not be able to inflate the result set.
            ranked = ranked[:limit]

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
            except Exception as e:
                # FAILURE != IDLE — same reasoning as `stale_summaries` below:
                # a transient DB fault must not read as "no superseded
                # sources" with zero trace. Search still degrades (the
                # annotation is advisory, never load-bearing for the result
                # itself), but the degrade is now visible in the log — CQ-02
                # (v0.8.75) closed the asymmetry the parity note used to flag.
                log.warning(
                    "stale_sources annotation degraded (%s: %s) — "
                    "%d source ids unchecked",
                    type(e).__name__, e, len(prov_ids),
                )
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

        # Lazy thematic→insight LINEAGE annotation (decision:1207). §2.5/§5.2
        # AMENDED: a superseded thematic summary no longer eagerly supersedes
        # an insight resting on it (consolidation_loop.py's lineage leg 3 is
        # disabled). Same philosophy as `stale_sources` above — annotate at
        # READ time, let the consumer judge materiality — but keyed on the
        # insight's OWN `summary_ids` (C4's field: the thematic summaries the
        # insight rests on), never on `source_pg_ids`.
        # ⚠ `summary_ids` holds `community_summaries` ids — a DIFFERENT,
        # OVERLAPPING sequence from `technical_docs` (§3.2's documented trap).
        # This MUST query `community_summaries`, never `technical_docs`, or a
        # `technical_docs` row sharing the same integer id would silently
        # produce a wrong-provenance false positive.
        insight_summary_ids: set[int] = set()
        if insight:
            insight_meta = insight.get("metadata")
            insight_meta = (_coerce_jsonb_obj(insight_meta)
                             if not isinstance(insight_meta, dict) else insight_meta)
            insight_summary_ids.update((insight_meta or {}).get("summary_ids") or [])
        stale_summary_map: dict[int, str | None] = {}
        if insight_summary_ids:
            try:
                async with self._acquire() as conn:
                    ssrows = await conn.fetch(
                        "SELECT id, superseded_reason FROM community_summaries"
                        " WHERE id = ANY($1) AND superseded",
                        list(insight_summary_ids),
                    )
                stale_summary_map = {r["id"]: r["superseded_reason"] for r in ssrows}
            except Exception as e:
                # FAILURE != IDLE — a transient DB fault must not read as "no
                # superseded summaries" with zero trace. Search still degrades
                # (the annotation is advisory, never load-bearing for the
                # result itself), but the degrade is now visible in the log.
                log.warning(
                    "stale_summaries annotation degraded (%s: %s) — "
                    "%d summary_ids unchecked",
                    type(e).__name__, e, len(insight_summary_ids),
                )
                stale_summary_map = {}  # degrade to no annotation

        def _stale_summaries(meta) -> list[dict]:
            m = _coerce_jsonb_obj(meta) if not isinstance(meta, dict) else meta
            sids = (m or {}).get("summary_ids") or []
            return [
                {"summary_id": sid, "superseded_reason": stale_summary_map.get(sid)}
                for sid in sids
                if sid in stale_summary_map
            ]

        # Neo4j relational expansion. `final` is built in the RERANKER'S ORDER,
        # summaries and facts interleaved — no tier is inserted ahead of the
        # ranking and none is appended after it.
        final: list[dict] = []

        async with self._neo4j.session() as session:
            # Summary→sources walk: a Tier-3 narrative's graph context (its
            # SUMMARIZED_BY source records + any typed edges) surfaces the same
            # way a record's does. Degrades to [] on any Neo4j failure. Batched
            # (one round-trip for every summary/insight anchor, not one per) —
            # was an N+1 here, up to ~102 sequential queries per search call.
            #
            # ⚠ The two id namespaces are INDEPENDENT sequences that collide
            # freely — community_summaries.id 42 and technical_docs.id 42 are
            # different records — so the two context maps are kept apart and each
            # hit is resolved against the one for ITS kind, never merged.
            surviving_t3 = [t3_rows[h["index"]] for h in ranked
                            if h["index"] < n_t3]
            summary_ctx = await self._expand_graph_context_batch(
                session,
                [r.get("id") for r in surviving_t3 if r.get("id") is not None],
                (ONT.community_summary,),
            )

            # Same batching for the Tier-1 hits' graph context. Anchor on ALL
            # record labels — Decision and Retrospective rows get graph context
            # too, not just Facts (read contract).
            fact_pg_ids = [ids[h["index"] - n_t3] for h in ranked
                           if h["index"] >= n_t3]
            fact_ctx = await self._expand_graph_context_batch(
                session, fact_pg_ids, (ONT.fact, ONT.decision, ONT.retrospective)
            )

            for hit in ranked:
                raw_score = hit["relevance_score"]

                if hit["index"] < n_t3:
                    # A Tier-3 narrative that EARNED this position. It now
                    # carries a score like anything else — the null score that
                    # used to mark it was a consequence of never being ranked,
                    # not a property of the tier.
                    row  = t3_rows[hit["index"]]
                    meta = row["metadata"]
                    rtype = summary_record_type(meta)
                    res = {
                        # `insight_summary` still names the cross-project kind,
                        # but it no longer implies a position: an insight and a
                        # thematic summary are now ranked against each other and
                        # against the facts, on the same scale.
                        "tier": ("insight_summary" if rtype == "insight"
                                 else "community_summary"),
                        # A DIFFERENT id namespace from the fact tier — same
                        # field name, independent sequence. record_type/ref
                        # disambiguate it.
                        "record_type": rtype,
                        "ref": make_ref(rtype, row.get("id")),
                        # .get: tolerant of pre-change callers/stubs without the
                        # id column — pg_id (community_summaries.id = the
                        # CommunitySummary node key) enables the walk above.
                        "pg_id": row.get("id"),
                        "content": row["content"],
                        "ranked": reranked,
                        "score": raw_score,
                        "score_normalized": (_sigmoid(raw_score)
                                             if raw_score is not None else None),
                        "matched_entities": [],
                        "metadata": meta,
                        # Surface the summary's provenance so an agent can trace
                        # a Tier-3 narrative back to the exact Tier-1 facts it
                        # was synthesised from — drill down via /memory/graph or
                        # status/{pg_id}.
                        "source_pg_ids": row["source_pg_ids"],
                        "graph_context": (summary_ctx.get(row.get("id"), [])
                                          if row.get("id") is not None else []),
                    }
                    stale = _stale_sources(row["source_pg_ids"], meta)
                    if stale:
                        res["stale_sources"] = stale
                    if rtype == "insight":
                        stale_sum = _stale_summaries(meta)
                        if stale_sum:
                            res["stale_summaries"] = stale_sum
                    final.append(res)
                    continue

                idx   = hit["index"] - n_t3
                pg_id = ids[idx]
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
                    # `ranked` says whether these positions mean anything. When
                    # the reranker is unreachable the rows are in VECTOR order
                    # and carry NO score — a fabricated 1.0 made a dead reranker
                    # indistinguishable from a confident one.
                    "ranked": reranked,
                    "score": raw_score,
                    "score_normalized": (_sigmoid(raw_score)
                                         if raw_score is not None else None),
                    "matched_entities": _matched_entities(query, metas[idx]),
                    "metadata": metas[idx],
                    "created_at": (createds[idx].isoformat()
                                   if createds[idx] is not None else None),
                    "graph_context": ctx,
                })

        # ── LIFECYCLE RESOLUTION (decision 1109) ────────────────────────────
        # The reranked set is an ENTRY POINT into the graph, not the answer. Each
        # returned decision is walked to its current verdict so the reader is
        # never handed an ADR that has since been judged without being told.
        #
        # It ATTACHES rather than adding rows: the caller's limit is a contract
        # (v0.8.51), so a companion record must not silently inflate the result
        # set. The verdict's reasoning travels with the decision it qualifies.
        decision_ids = [r["pg_id"] for r in final
                        if r.get("record_type") == "decision"
                        and r.get("pg_id") is not None]
        if decision_ids:
            try:
                async with self._neo4j.session() as session:
                    lifecycle = await self._resolve_decision_lifecycle(
                        session, decision_ids)
            except Exception:
                lifecycle = {}
            # Pull the retrospective's OWN TEXT for the ratings that qualify the
            # decision — `refined`/`mixed`/`reversed` mean the reader must weigh
            # the verdict, and a rating word alone does not carry the reasoning.
            wanted = [v["retrospective_pg_id"] for v in lifecycle.values()
                      if v["rating"] in self._QUALIFYING_RATINGS
                      and v.get("retrospective_pg_id") is not None]
            notes: dict[int, str] = {}
            if wanted:
                try:
                    async with self._acquire() as conn:
                        for row in await conn.fetch(
                            "SELECT id, content FROM technical_docs"
                            " WHERE id = ANY($1::bigint[])", wanted,
                        ):
                            notes[row["id"]] = row["content"]
                except Exception:
                    notes = {}
            for r in final:
                state = lifecycle.get(r.get("pg_id")) if \
                    r.get("record_type") == "decision" else None
                if not state:
                    continue
                entry = {
                    "rating": state["rating"],
                    # A record reference the caller can fetch directly — the same
                    # qualified form used everywhere else, because a bare integer
                    # resolves against the wrong table (decision 822).
                    "ref": make_ref("retrospective", state["retrospective_pg_id"]),
                    "retrospective_pg_id": state["retrospective_pg_id"],
                }
                text = notes.get(state["retrospective_pg_id"])
                if text is not None:
                    entry["retrospective_content"] = text
                r["lifecycle"] = entry

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
                 "message": f"reference must be an integer or <type>:<id> ({_short(exc)})"},
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
            # A thematic summary carries a single `domain`; an insight (C4,
            # §3.2) carries MULTI-VALUED `domains` instead (the walk can
            # legitimately cross domains). Expose both keys always — a
            # thematic row's `domains` degrades to a one-element list built
            # from its own `domain` so a caller can read either field
            # uniformly regardless of record kind, and an insight row's
            # `domain` stays present (its first `domains` entry, or None for
            # an insight with none) rather than silently disappearing for a
            # client that has not been updated to read the new field yet.
            "domain": meta.get("domain") or (
                (meta.get("domains") or [None])[0] if meta.get("domains") else None),
            "domains": meta.get("domains") or (
                [meta["domain"]] if meta.get("domain") else []),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "superseded": row["superseded"],
            "run_id": row["run_id"],
            "source_pg_ids": list(row["source_pg_ids"] or []),
            "summary_ids": meta.get("summary_ids") or [],
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

        # refold_ledger breakdown (O1, fact:1189) — a lone backlog number
        # misleads: dropped/below_density and dropped/out_of_scan (I7,
        # decision:1121) must be distinguishable from a genuinely open row.
        # Also the insight-kind reconciliation read (O2, I17, decision:1181)
        # — the durable record's only visibility, since nothing else reads
        # insight-kind rows.
        try:
            snap["refold_ledger"] = await self._refold_ledger_telemetry()
        except Exception as exc:
            snap["refold_ledger"] = {"error": str(exc)}

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

        # Credential-use audit trail signal (PR A3) — in-process counters, no
        # I/O, so no try/except: same reset-on-restart contract as the
        # existing _llm_routed counters. Detail lives in the separate
        # credential-events log; this is the operator-attention SIGNAL only.
        snap["llm_faults"] = _llm_faults_snapshot()
        snap["credentials"] = _credentials_snapshot()

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
        # Keys that ARE first-write projected, so they must NOT be reported as
        # promotion candidates in family (B). `rating` and `target_pg_id` joined
        # this set when the retrospectives block below started projecting them —
        # leaving them in the emergent list would advertise, as an unmet
        # opportunity, the very measurement that now exists.
        PROJECTED = {"source", "type", "entities", "decision", "source_ref",
                     "supersedes", "grounded_in", "fact_kind", "elicited",
                     "rating", "target_pg_id"}

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
            # `facts` means FACTS. It used to mean "everything that is not a
            # decision", which silently absorbed every retrospective into the
            # total — so retrospective first-write quality was unmeasurable and
            # the facts figure was diluted by records held to different required
            # fields. Retrospectives now have their own block below, and the two
            # totals no longer double-count the same records.
            frow = await conn.fetchrow(
                "SELECT count(*) AS n,"
                " count(*) FILTER (WHERE metadata ? 'source_ref') AS sref,"
                " count(*) FILTER (WHERE (metadata->>'elicited')='true') AS elicited"
                " FROM technical_docs"
                " WHERE (metadata->>'type' IS NULL"
                "        OR metadata->>'type' NOT IN ('decision', 'retrospective'))"
                "   AND NOT superseded"
            )
            # Retrospectives carry their spine fields at the TOP level of metadata
            # (`rating`, `target_pg_id`), not nested under a per-type object the way
            # a decision's alternatives/confidence are — so these are direct `?`
            # checks, matching how the write path actually stores them.
            rrow = await conn.fetchrow(
                "SELECT count(*) AS n,"
                " count(*) FILTER (WHERE metadata ? 'rating') AS rating,"
                " count(*) FILTER (WHERE metadata ? 'target_pg_id') AS target,"
                " count(*) FILTER (WHERE metadata ? 'grounded_in') AS grounded,"
                " count(*) FILTER (WHERE (metadata->>'elicited')='true') AS elicited"
                " FROM technical_docs"
                " WHERE metadata->>'type'='retrospective' AND NOT superseded"
            )
            keys = await conn.fetch(
                "SELECT k, count(*) AS n FROM technical_docs, jsonb_object_keys(metadata) k"
                " WHERE NOT superseded GROUP BY k ORDER BY n DESC"
            )
            # Per-alternative vectors (migration 026). `alternatives_pct` above
            # says how many decisions RECORDED alternatives; this says how many
            # of those entries are actually retrievable by similarity. The two
            # answer different questions and both are needed — a full
            # `alternatives_pct` beside a stalled `pending` is a populator that
            # has stopped, which no coverage figure would show.
            #
            # `failing` is the working/failing split Group 3 asks for: rows that
            # keep coming back are counted separately from rows that simply have
            # not been reached yet, and `oldest_pending_age_s` distinguishes a
            # backlog that is draining from one that is stuck.
            try:
                arow = await conn.fetchrow(
                    "SELECT count(*) AS entries,"
                    " count(*) FILTER (WHERE embedding IS NOT NULL) AS embedded,"
                    " count(*) FILTER (WHERE embedding IS NULL) AS pending,"
                    " count(*) FILTER (WHERE embedding IS NULL AND attempts >= $1)"
                    "     AS failing,"
                    " count(DISTINCT decision_pg_id) AS decisions,"
                    # FILTER belongs to the AGGREGATE, not to the expression
                    # wrapping it: `extract(...) FILTER (...)` parses as a
                    # filter on a non-aggregate and is a syntax error. Caught
                    # only by running it — the suite stubs every query.
                    " extract(epoch FROM now() -"
                    "     min(created_at) FILTER (WHERE embedding IS NULL))"
                    "     AS oldest_pending_age_s"
                    " FROM decision_alternatives",
                    ALT_VECTOR_FAILING_AFTER,
                )
                alt_vectors = {
                    "entries": arow["entries"],
                    "decisions": arow["decisions"],
                    "embedded": arow["embedded"],
                    "pending": arow["pending"],
                    "failing": arow["failing"],
                    "embedded_pct": pct(arow["embedded"], arow["entries"]),
                    "oldest_pending_age_s": (
                        round(float(arow["oldest_pending_age_s"]), 1)
                        if arow["oldest_pending_age_s"] is not None else None
                    ),
                }
            except Exception as exc:
                # Reported, never raised: this block is a measurement inside a
                # read endpoint, and a telemetry query that propagates would take
                # the whole rollup down with it.
                alt_vectors = {"error": str(exc)}

            try:
                alias_total = await conn.fetchval("SELECT count(*) FROM alias_adjudications")
                asplit = await conn.fetch(
                    "SELECT verdict, count(*) AS n FROM alias_adjudications GROUP BY verdict"
                )
                alias = {"adjudications": alias_total,
                         "by_verdict": {r["verdict"]: r["n"] for r in asplit}}
            except Exception as exc:
                alias = {"error": str(exc)}

        dn, fn, rn = drow["n"], frow["n"], rrow["n"]
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
            "alternative_vectors": alt_vectors,
            "facts": {
                "total": fn,
                "source_ref_pct": pct(frow["sref"], fn),
                "elicited_pct": pct(frow["elicited"], fn),
            },
            # A retrospective's required fields are its own: the outcome state it
            # reports (`rating`), the decision it judges (`target_pg_id`), and the
            # records that MEASURED that outcome (`grounded_in`). The first two are
            # set by every write path, so they read as a regression alarm rather
            # than a trend; grounded_in is the one that carries signal — a
            # retrospective without it asserts an outcome nothing backs.
            "retrospectives": {
                "total": rn,
                "rating_pct": pct(rrow["rating"], rn),
                "target_pg_id_pct": pct(rrow["target"], rn),
                "grounded_in_pct": pct(rrow["grounded"], rn),
                "elicited_pct": pct(rrow["elicited"], rn),
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
        verdict. One windowed query (last-success per partition); backlog is
        the cycle's OWN recorded ``eligible_clusters`` census, nothing else
        (I7, `decision:1121` — see `_consolidation_backlog`). stalled =
        backlog present AND no successful fold within STALL_THRESHOLD AND
        nothing in-flight.

        ⚠ No longer calls ``_nrem_cycle_counts`` here: that density count used
        to be the no-census fallback and is not any more (I7). It remains a
        SEPARATE, purely informational gauge elsewhere (``snap["nrem"]`` in
        the telemetry snapshot) — "raw candidate material exists" is still
        worth reporting, it just must never stand in for "the gate fired"."""
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
              -- AR-01 (v0.8.75): truncation_failures/slot_failures are written
              -- into consolidation_runs.extra by _CycleRec.extra() (same shape
              -- as dead_lettered_clusters below) but were never rolled up here
              -- — so the FIRST scaffold-fold protocol failure (slot_failed) was
              -- invisible to any monitor. Additive keys only; mirrors the
              -- dead_lettered_clusters extraction pattern for the latest value,
              -- and folds_succeeded_24h's sum-FILTER shape for the 24h total.
              sum((extra->>'truncation_failures')::int)
                  FILTER (WHERE started_at > now() - interval '24 hours'
                          AND extra ? 'truncation_failures') AS truncation_failures_24h,
              sum((extra->>'slot_failures')::int)
                  FILTER (WHERE started_at > now() - interval '24 hours'
                          AND extra ? 'slot_failures') AS slot_failures_24h,
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
              -- R1 fix: paired to the SAME row as eligible_clusters above —
              -- FILTER on eligible_clusters IS NOT NULL, not on this column's
              -- own nullness. A row whose census recorded eligible_clusters=0
              -- also writes eligible_oldest_age_seconds=NULL (no oldest
              -- cluster exists); filtering on this column separately let the
              -- age pick up an OLDER row's non-null value while the count
              -- came from the newest row, producing an impossible pair like
              -- "eligible 0 (oldest 263684s)". Filtering both arrays on the
              -- same predicate keeps them on one row, so a NULL age here
              -- means the latest census itself recorded no oldest age.
              (array_agg(eligible_oldest_age_seconds ORDER BY started_at DESC)
                  FILTER (WHERE eligible_clusters IS NOT NULL))[1] AS eligible_oldest_age,
              -- Reason of the most-recent deferral (e.g. 'gpu_busy' | 'backup_drain'),
              -- written to consolidation_runs.extra by the daemon. Lets the monitor
              -- show "deferred — inference GPU busy" instead of a bare "deferred".
              (array_agg(extra->>'reason' ORDER BY started_at DESC)
                  FILTER (WHERE outcome = 'deferred' AND extra ? 'reason'))[1] AS last_deferred_reason,
              -- D1 (fact:1189, decision:1121/I7) — the latest count of
              -- clusters this cycle EXCLUDED from eligible_clusters because
              -- NREM_FOLD_FAIL_CAP dead-lettered them. Written to
              -- consolidation_runs.extra by the daemon (_CycleRec.extra()).
              -- A NEW key — never an alias for eligible_clusters.
              (array_agg((extra->>'dead_lettered_clusters')::int ORDER BY started_at DESC)
                  FILTER (WHERE extra ? 'dead_lettered_clusters'))[1] AS dead_lettered_clusters,
              -- AR-01: latest recorded value of each, same shape as
              -- dead_lettered_clusters above — None means no cycle has yet
              -- written this key (pre-fix rows), not zero.
              (array_agg((extra->>'truncation_failures')::int ORDER BY started_at DESC)
                  FILTER (WHERE extra ? 'truncation_failures'))[1] AS truncation_failures,
              (array_agg((extra->>'slot_failures')::int ORDER BY started_at DESC)
                  FILTER (WHERE extra ? 'slot_failures'))[1] AS slot_failures,
              -- Output-identity skips (operator ruling 2026-08-11) — latest
              -- count of clusters whose re-fold would have been byte-identical
              -- and was skipped without embedding or write. Same shape as
              -- dead_lettered_clusters: a NEW key, never an alias for
              -- eligible_clusters (those clusters are deliberately NOT
              -- eligible backlog, so the stall verdict cannot read a
              -- fully-current corpus as stalled). None = no cycle has written
              -- the key yet (pre-fix rows), not zero.
              (array_agg((extra->>'unchanged_clusters')::int ORDER BY started_at DESC)
                  FILTER (WHERE extra ? 'unchanged_clusters'))[1] AS unchanged_clusters,
              -- Singleton-component deferrals (operator ruling 2026-08-16,
              -- third application of the I7/decision:1121 class) — latest
              -- count of clusters excluded from `eligible_clusters` because
              -- their judgement reach was exactly 1 (no second judgement to
              -- fold with yet), never attempted. Same shape/contract as
              -- dead_lettered_clusters/unchanged_clusters above: a NEW key,
              -- never an alias for eligible_clusters. None = no cycle has
              -- written this key yet (pre-fix rows), not zero.
              (array_agg((extra->>'singleton_clusters')::int ORDER BY started_at DESC)
                  FILTER (WHERE extra ? 'singleton_clusters'))[1] AS singleton_clusters
            FROM ranked GROUP BY cycle_type
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(query, CONSOLIDATION_ORPHAN_TIMEOUT_SEC)
        by_type = {r["cycle_type"]: r for r in rows}

        out: dict = {"stall_threshold_seconds": CONSOLIDATION_STALL_THRESHOLD_SEC}
        any_stalled = False
        started_at: dict = {}
        for ct in CONSOLIDATION_CYCLE_TYPES:
            r = by_type.get(ct)
            age = r["last_success_age"] if r else None
            in_flight = bool(r["inflight"]) if r else False
            elig = r["eligible_clusters"] if r else None
            # I7 (decision:1121): backlog must match the gate the cycle
            # ACTUALLY folds on — the recorded eligible_clusters, and NOTHING
            # else. No fallback to a looser density count when no census has
            # been recorded; see _consolidation_backlog's docstring.
            started_at[ct] = r["last_started"] if r else None
            backlog_count = _consolidation_backlog(elig)
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
                # R1 fix (review finding): eligible_oldest_age is pulled from
                # the SAME row as eligible_clusters (both filtered on
                # `eligible_clusters IS NOT NULL` in the query above), so a
                # census that reports eligible_clusters=0 reports its own
                # eligible_oldest_age_seconds honestly — NULL, not a stale
                # non-null value carried over from an earlier row.
                "eligible_clusters": elig,
                "eligible_oldest_age_seconds": (r["eligible_oldest_age"] if r else None),
                # D1 (fact:1189, decision:1121/I7) — clusters this cycle
                # excluded from `eligible_clusters` because they were
                # dead-lettered (NREM_FOLD_FAIL_CAP). NEW key; None means no
                # census has recorded this yet (pre-D1 rows), NOT zero.
                "dead_lettered_clusters": (
                    int(r["dead_lettered_clusters"])
                    if r and r["dead_lettered_clusters"] is not None else None),
                # Output-identity skips (operator ruling 2026-08-11) — latest
                # count of clusters whose re-fold would have rewritten the
                # active summary byte-identically and was skipped without
                # embedding or write. Same None-means-not-yet-recorded
                # contract as dead_lettered_clusters above.
                "unchanged_clusters": (
                    int(r["unchanged_clusters"])
                    if r and r["unchanged_clusters"] is not None else None),
                # Singleton-component deferrals (operator ruling 2026-08-16) —
                # latest count of clusters excluded from `eligible_clusters`
                # because their judgement reach was exactly 1 (one-judgement
                # reach cannot fold an insight) and never attempted. Same
                # None-means-not-yet-recorded contract as dead_lettered_clusters
                # above.
                "singleton_clusters": (
                    int(r["singleton_clusters"])
                    if r and r["singleton_clusters"] is not None else None),
                # AR-01 (v0.8.75): latest recorded truncation_failures/
                # slot_failures, same None-means-not-yet-recorded contract as
                # dead_lettered_clusters above — a protocol failure (slot_failed)
                # is now as visible as a capacity one (truncation_failed) always
                # should have been.
                "truncation_failures": (
                    int(r["truncation_failures"])
                    if r and r["truncation_failures"] is not None else None),
                "slot_failures": (
                    int(r["slot_failures"])
                    if r and r["slot_failures"] is not None else None),
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
                # AR-01: 24h sums, same zero-means-none-occurred contract as the
                # folds_* pair above (unlike the latest-value keys, absence over
                # a real window is a true zero, not missing evidence).
                "truncation_failures_24h": (
                    int(r["truncation_failures_24h"])
                    if r and r["truncation_failures_24h"] is not None else 0),
                "slot_failures_24h": (
                    int(r["slot_failures_24h"])
                    if r and r["slot_failures_24h"] is not None else 0),
                "last_started": (
                    r["last_started"].isoformat()
                    if r and r["last_started"] is not None else None),
            }
        out.update(_consolidation_rollup(out, any_stalled, started_at))
        return out

    async def _project_identity_health(self) -> dict:
        """Is the project-identity upgrade complete on THIS deployment? (027)

        Migration 027 gives every registry row an id; only
        ``reconcile_project_identity.py`` can put that id on the graph nodes,
        and until it has, the insight gate FAILS CLOSED on the nodes it has not
        reached — they do not count toward its two-project rule. Without this
        gauge that state is invisible: folds simply do not happen, which looks
        exactly like a quiet corpus.

        So the metric is named for the question an operator actually has —
        ``complete`` — and counts the two distinguishable ways it can be false:

          ``unidentified``  registered project nodes with no id: run reconcile
          ``mismatched``    an id that disagrees with the registry: also
                            reconcile, but read it first — it means the node was
                            stamped against a registry row that has since moved
          ``unregistered``  a node whose name has no registry row at all. NOT
                            counted in ``complete``, because no tool can fix it
                            without deciding what the project IS — an operator's
                            call. It still cannot take part in a fold, which is
                            why it is reported rather than left implicit.

        ⚠ THE TWO DIRECTIONS OF DISAGREEMENT ARE NOT SYMMETRIC, and only one of
        them is a defect. A project is registered at INGRESS, while its node is
        written LATER by the outbox worker, so the stores legitimately disagree
        for the length of that window — and a registered project that no record
        has ever named has no node at all. **Fewer nodes than registry rows is
        the normal resting state** (registry rows are a superset by
        construction) and is deliberately not counted here at all. The reverse —
        a node the registry does not know — cannot arise from that window in
        that direction, so it is reported as its own number.

        ⚠ THE READ ORDER IS PART OF THAT, not incidental: the graph is read
        FIRST and the registry SECOND, so the registry snapshot is never older
        than the node snapshot. A project registered concurrently therefore
        cannot produce a phantom ``unregistered`` — its row is already visible by
        the time the nodes are checked. Reversing these two reads would make the
        normal ingress path emit false alarms.
        """
        async with self._neo4j.session() as session:
            rows = await (await session.run(
                f"MATCH (p:{ONT.project})"
                f" RETURN p.name AS name, p.project_id AS project_id"
            )).data()
        async with self._acquire() as conn:
            registry = {
                r["name"]: r["id"]
                for r in await conn.fetch("SELECT name, id FROM projects")
            }
        unidentified = mismatched = unregistered = 0
        for row in rows:
            expected = registry.get(row["name"])
            if expected is None:
                unregistered += 1
            elif row["project_id"] is None:
                unidentified += 1
            elif row["project_id"] != expected:
                mismatched += 1
        return {
            "nodes": len(rows),
            "unidentified": unidentified,
            "mismatched": mismatched,
            "unregistered": unregistered,
            "complete": unidentified == 0 and mismatched == 0,
        }

    async def _domain_identity_health(self) -> dict:
        """Is the domain registry consistent with the graph? (P13, migration 028.)

        The same question `project_identity` answers, asked of an axis that is
        keyed on its identity from the first day — so the shapes it can go wrong
        in are narrower and mean different things:

          ``unregistered``  a `:Domain` node whose id is in no registry row. On
                            this axis that is the ONLY real defect, and unlike
                            its project twin it cannot be produced by the
                            ordinary ingress→outbox window: a domain is
                            registered BEFORE it can be written, so a node
                            without a row means a row was deleted underneath it.
          ``mismatched``    a node whose name disagrees with its registry row's
                            — a rename that has not reached the graph. Harmless
                            to belonging (the id is what edges hang off) and
                            visible because a stale label is what a human reads.
          ``unattached``    a `:Domain` with no `PROJECT_OF` edge — a section
                            belonging to no project.

        ⚠ `unattached` IS HERE FOR A TRAVERSAL THAT DOES NOT EXIST YET, and that
        is deliberate rather than premature. Cross-project and cross-domain
        synthesis will walk `(:Domain)<-[:DOMAIN_OF]-(record)-[:GROUNDED_IN]->
        (:Fact)` and reach a project through `(:Domain)-[:PROJECT_OF]->`, then
        count DISTINCT `domain_id` the same way the insight gate counts DISTINCT
        `project_id` today. Both halves of that walk are properties of how the
        node is WRITTEN, so they can be broken now and only discovered when the
        gate is built — at which point a section silently missing from a walk
        looks like a quiet corpus, which is the failure mode this whole axis
        exists to remove. A number that is zero today is what makes it provable
        that it stayed zero.

        ⚠ FEWER NODES THAN REGISTRY ROWS IS THE NORMAL RESTING STATE and is not
        counted, exactly as on the project axis: a section nobody has filed a
        record under has no node. The registry is a superset by construction.

        ⚠ THE READ ORDER IS PART OF THE RULE: graph FIRST, registry SECOND, so
        the registry snapshot is never older than the node snapshot and a section
        registered concurrently cannot present as `unregistered`.
        """
        async with self._neo4j.session() as session:
            rows = await (await session.run(
                f"MATCH (d:{ONT.domain})"
                f" RETURN d.domain_id AS domain_id, d.name AS name,"
                f"        EXISTS {{ (d)-[:{ONT.project_of}]->(:{ONT.project}) }}"
                f"          AS attached"
            )).data()
        async with self._acquire() as conn:
            registry = {
                r["id"]: r["name"]
                for r in await conn.fetch("SELECT id, name FROM project_domains")
            }
        unregistered = mismatched = unattached = 0
        for row in rows:
            expected = registry.get(row["domain_id"])
            if expected is None:
                unregistered += 1
            elif row["name"] != expected:
                mismatched += 1
            if not row["attached"]:
                unattached += 1
        return {
            "nodes": len(rows),
            "registry_rows": len(registry),
            "unregistered": unregistered,
            "mismatched": mismatched,
            "unattached": unattached,
            "complete": unregistered == 0 and mismatched == 0 and unattached == 0,
        }

    async def _consolidation_telemetry(self) -> dict:
        """Full consolidation section for /memory/telemetry (computed fresh)."""
        return await self._compute_consolidation_health()

    async def _refold_ledger_telemetry(self) -> dict:
        """O1/O2 (fact:1189) — refold_ledger visibility. A lone backlog
        number misleads: `dropped/below_density` and `dropped/out_of_scan`
        (I7, `decision:1121` — a candidate the cycle scanned and correctly
        did not gate THIS pass) must be distinguishable from a genuinely
        open row, which is what a real stall looks like. Two breakdowns
        over the ledger's own columns:

          ``by_status_reason`` -- (status, closed_reason) counts. An open
              row has ``closed_reason IS NULL``.
          ``by_trigger_kind``  -- counts by ``trigger_kind``:
              'technical_docs' = a superseded fact or reversed decision
              triggered this row directly; 'community_summaries' = a
              retired summary's own retirement cascaded to this row (C3's
              lineage mechanism, one summary's retirement raising another).

        O2's reconciliation read (I17, `decision:1181`): an insight-kind
        row is an ATTRIBUTION row, never a clock entry (nothing reads
        insight-kind rows for due-ness — the insight re-fold trigger is the
        graph's own ``consolidated`` clear, not this ledger). So a
        best-effort graph write that silently failed
        (`run_lineage_invalidation_pass`'s Neo4j half, after the Postgres
        commit) has NO OTHER visibility. ``insight_reconciliation_stuck``
        counts OPEN insight-kind LEDGER ROWS whose judgement's Decision/
        Retrospective node STILL reads ``consolidated = true`` in Neo4j —
        meaning the graph never got the clear that would let G3
        (`insight_gate.py`) re-gate it. Read-only PG + Neo4j join, gateway-
        side (a read-scoped client has no direct access to either store)."""
        async with self._acquire() as conn:
            by_status_reason = await conn.fetch(
                "SELECT status, closed_reason, count(*) AS n"
                "  FROM refold_ledger GROUP BY status, closed_reason"
            )
            by_trigger = await conn.fetch(
                "SELECT trigger_kind, count(*) AS n"
                "  FROM refold_ledger GROUP BY trigger_kind"
            )
            open_insight_pg_ids = await conn.fetch(
                "SELECT DISTINCT pg_id FROM refold_ledger"
                " WHERE status = 'open' AND summary_kind = 'insight'"
            )
        pg_ids = [r["pg_id"] for r in open_insight_pg_ids]

        stuck_ids: set = set()
        if pg_ids:
            async with self._neo4j.session() as session:
                res = await session.run(
                    f"UNWIND $ids AS pid"
                    f" MATCH (d) WHERE (d:{ONT.decision} OR d:{ONT.retrospective})"
                    f"                  AND d.pg_id = pid AND d.consolidated = true"
                    f" RETURN collect(DISTINCT pid) AS stuck_ids",
                    ids=pg_ids,
                )
                rec = await res.single()
                stuck_ids = set((rec["stuck_ids"] if rec else None) or [])

        # O2 counts LEDGER ROWS (decision:1181: "open insight-kind ledger
        # rows whose judgement nodes are still consolidated=true"), not
        # distinct pg_ids — a pg_id can carry more than one open row.
        stuck_row_count = 0
        if stuck_ids:
            async with self._acquire() as conn:
                stuck_row_count = await conn.fetchval(
                    "SELECT count(*) FROM refold_ledger"
                    " WHERE status = 'open' AND summary_kind = 'insight'"
                    "   AND pg_id = ANY($1::bigint[])",
                    list(stuck_ids),
                )

        return {
            "by_status_reason": [
                {"status": r["status"], "closed_reason": r["closed_reason"],
                 "count": r["n"]}
                for r in by_status_reason
            ],
            "by_trigger_kind": {r["trigger_kind"]: r["n"] for r in by_trigger},
            "insight_reconciliation_stuck": stuck_row_count,
        }

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
                try:
                    domain_identity = await self._domain_identity_health()
                except Exception:
                    # Same tolerance, same reason as the project probe below: a
                    # metric about registry drift must never present as a
                    # stalled system.
                    domain_identity = None
                try:
                    project_identity = await self._project_identity_health()
                except Exception:
                    # Same tolerance, and the same reason it must be guarded at
                    # all: a probe that raises inside the refresher would take
                    # the whole cached snapshot down with it, so a metric about
                    # an incomplete upgrade would present as a stalled system.
                    project_identity = None
                self._consolidation_health = {
                    "stalled": full["stalled"],
                    "graph_invalid_nodes": invalid_nodes,
                    "project_identity": project_identity,
                    "domain_identity": domain_identity,
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

        v2 (C1b/C2): reproduces consolidation_loop's v2 FACT GATE (Dreaming
        Cycle Plan to v2, §2.1) exactly — there is no more entity-hub gate and
        no more project-only gate to sum against it. Facts are the grounded,
        non-superseded Facts of each registered (project, domain) group,
        counted where a group meets ONT.density_threshold; `decision_cycles`
        (C2) now counts GATING (project, domain) groups — G1 (the fact gate)
        passed AND `insight_gate.passes_insight_gate` (G2 >=1 Retrospective,
        G3 >=1 fresh judgement) true on the group's full walked reach — the
        SAME predicate `consolidation_loop._find_fresh_insight_clusters`
        folds on, via the SAME `insight_gate.walk_group_reached_set`/
        `passes_insight_gate` this module imports at the top (never
        `consolidation_loop` itself — that module imports psycopg2 at its own
        top level and the shipped gateway service does not carry psycopg2;
        see the v0.8.65 nrem-telemetry-gauge fix this pattern follows).
        """
        # Neo4j: the SAME graph-native walk consolidation_loop's
        # _find_grounded_fact_groups folds on — GROUNDED_IN -> DOMAIN_OF ->
        # PROJECT_OF, never MENTIONS/Entity. project/domain now come straight
        # off these graph edges, so no separate Postgres round-trip is needed
        # to resolve them (a DOMAIN_OF/PROJECT_OF edge only exists for a
        # REGISTERED section — coordinator._domain_identities never writes one
        # otherwise — so edge presence already proves registration).
        async with self._neo4j.session() as session:
            fres = await session.run(
                f"MATCH (j) WHERE j:{ONT.decision} OR j:{ONT.retrospective}"
                f" MATCH (j)-[:{ONT.grounded_in}]->(f:{ONT.fact})"
                f" WHERE coalesce(f.superseded, false) = false"
                f" MATCH (f)-[:{ONT.domain_of}]->(dom:{ONT.domain})"
                f"           -[:{ONT.project_of}]->(proj:{ONT.project})"
                f" WITH DISTINCT f, proj.name AS project, dom.name AS domain"
                f" RETURN f.pg_id AS pg_id, project, domain",
            )
            fact_rows = await fres.data()

        # SAME pure partitioner the fold uses (eligible_domain_level_clusters,
        # here used for its GROUPS, not just its count-only twin) — sourced
        # from nrem_gate, a module that imports no DB driver. NEVER reach back
        # into the daemon module that owns the fold itself: that module
        # imports psycopg2 at its own top level and the shipped gateway
        # service does not carry psycopg2, so an import of this function that
        # reached into the daemon module used to raise ModuleNotFoundError on
        # every call — silently killing this gauge in production while every
        # unit test (fully stubbed) stayed green. nrem_gate.py holds only
        # these two functions and imports no DB driver — see its docstring.
        # Never invent a second rule, and never route this import back
        # through the daemon module again.
        from nrem_gate import count_domain_level_cycles
        from nrem_gate import eligible_domain_level_clusters
        project_map: dict[int, str] = {}
        domains_map: dict[int, list] = {}
        registered_sections: set = set()
        for r in fact_rows:
            pid = r["pg_id"]
            project_map[pid] = r["project"]
            doms = domains_map.setdefault(pid, [])
            if r["domain"] not in doms:
                doms.append(r["domain"])
            registered_sections.add((r["project"], r["domain"]))
        pg_ids_all = list(project_map)

        # `fact_cycles` IS the fact gate now — there is no second level to sum
        # it against (v2, C1/C1b). A separate, always-equal
        # "domain_level_cycles" field would be a duplicate a future reader
        # could wrongly assume differs; one number, named for what it
        # measures.
        fact_cycles = count_domain_level_cycles(
            pg_ids_all, project_map, domains_map,
            ONT.density_threshold, registered_sections,
        ) if pg_ids_all else 0

        # decision_cycles (v2, C2): count GATING groups, not decisions. G1's
        # groups (density >= ONT.density_threshold) are the same call
        # fact_cycles makes; each is then walked (I3: undirected, unbounded,
        # over the closed relation set) and G2+G3 checked on its full reach —
        # one small Neo4j round-trip per candidate group (order 1-5 groups on
        # this corpus), acceptable for a periodic health-refresh gauge.
        groups = eligible_domain_level_clusters(
            [""] * len(pg_ids_all), pg_ids_all, project_map, domains_map,
            ONT.density_threshold, registered_sections,
        ) if pg_ids_all else []
        decision_cycles = 0
        for _key, _contents, fact_ids in groups:
            labels, consolidated, _components = await walk_group_reached_set(
                self._neo4j, fact_ids)
            if passes_insight_gate(labels, consolidated):
                decision_cycles += 1

        # v2 (C2): `decision_threshold` is REMOVED, not repurposed — same
        # precedent v0.8.64 set for `domain_threshold` (removed outright with
        # NREM_DOMAIN_THRESHOLD, never repointed at a different number).
        # There is no decision COUNT any more to report a threshold for: G2
        # and G3 are each "at least one" conditions (>=1 Retrospective
        # reached, >=1 fresh judgement reached), not a tunable volume. A bare
        # number under the old name would read as "the threshold was lowered
        # to 1", which is false — nothing was lowered, the concept a decision
        # THRESHOLD named no longer exists for this gate. No replacement
        # field: G2/G3 are not "a threshold under a new name", they are a
        # different kind of condition, and inventing a field that still reads
        # as a number would recreate the exact trap. Consumers of this
        # endpoint (including the monitor dashboard) must be updated — see
        # HANDOFF.md's monitor-effect list, carried into the release notes.
        return {
            "fact_cycles": fact_cycles,
            "decision_cycles": decision_cycles,
            "total_cycles": fact_cycles + decision_cycles,
            "fact_threshold": ONT.density_threshold,
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
                f"SELECT COALESCE({PROJECT_SQL}, '(none)') AS key,"
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
