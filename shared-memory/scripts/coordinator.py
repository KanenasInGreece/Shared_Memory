"""Memory coordinator: Postgres ingress, neo4j_outbox origin, and dual-store reads.

Saves ack Postgres and enqueue neo4j_outbox in the same SQL transaction; a
background worker applies the graph. attach() registers save, search, graph,
status, telemetry, supersede, review_hold, retrospective, and admin backup/outbox.
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
import urllib.parse
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx
from aiohttp import web
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ClientError

from log_hygiene import AsyncLineWriter, scrub_url_credentials
from agent_roles import effective_role, read_only_agents
from ontology import (
    ONT, sanitize_entity_names, sanitize_entity_name,
    reserved_entity_name_reason,
    KNOWN_LABELS, KNOWN_RELATIONSHIPS, fact_kind_from_source_ref,
    GROUNDING_ROLES, GROUNDING_RELATIONS, default_grounding_role, RETRO_RATINGS,
    record_label_for_type, derived_belonging_cypher,
)
from project_axis import (
    PROJECT_SQL, PROJECT_EXISTS_SQL, PROJECT_ID_SQL, PROJECT_PROPOSALS_SQL,
    PROPOSAL_SIMILARITY, PROPOSAL_LIMIT, SENTINEL,
    CONFUSABLE_SQL, CONFUSABLE_SIMILARITY, PROJECT_NAMES_SQL,
    PROJECT_NAME_OR_KEY_SQL,
    same_spelling, spelling_variant_of, unconfirmed_confusables,
    fold_eligible, resolve_project, project_for_graph, project_merge_cypher,
    axis_key, resolve_axis_value, expand_axis_spellings,
    VIA_EXACT, VIA_ALIAS, VIA_NORMALISED,
)
from domain_axis import (
    DOMAIN_EXISTS_SQL, DOMAIN_PROPOSALS_SQL, DOMAIN_PROPOSAL_SIMILARITY,
    DOMAIN_PROPOSAL_LIMIT, DOMAIN_CONFUSABLE_SQL, DOMAIN_CONFUSABLE_SIMILARITY,
    DOMAIN_ALIAS_RESOLVE_SQL, DOMAIN_REGISTER_SQL, DOMAIN_KEYS,
    DOMAIN_NAMES_SQL, DOMAIN_ALIASES_SQL, DOMAIN_NAME_OR_KEY_SQL,
    domain_merge_cypher, names_a_domain, resolve_domains,
)
from insight_gate import walk_group_reached_set, passes_insight_gate
from project_promotion import (
    promote_record, sole_project, METHOD_GROUNDING,
)
from project_alias import ALIAS_RESOLVE_SQL, ACTIVE_ALIASES_SQL
from secure_env import get_secret
from framework_defaults import FRAMEWORK_DEFAULTS
from telemetry_instruments import LatencyRing, Counter, safe
# A key whose removed_in this release has reached is stripped in handle_telemetry. Leaf module: its only import is annotations, so it cannot cycle back here.
from telemetry_contract import TELEMETRY as TELEMETRY_CONTRACT, strip_dropped

log = logging.getLogger("coordinator")

try:
    from gpu_load import inference_busy_state, probe_status
except Exception as _gpu_exc:  # pragma: no cover - import-time safety only
    # Observability only: a missing gpu_load must not stop the gateway, and "unknown" is not a false idle.
    log.warning("gpu_load.inference_busy_state unavailable (%s) — "
                "inference_busy will report 'unknown'", _gpu_exc)

    async def inference_busy_state() -> str:  # type: ignore[misc]
        return "unknown"

    def probe_status() -> dict:  # type: ignore[misc]
        return {"state": "unavailable", "consecutive_hangs": 0, "leaked_children": 0}


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


# FRAMEWORK_VERSION is the build string and may drift. API_VERSION is the wire contract with memory_bridge.py; bump it only when shape, auth, or routes break older clients.
FRAMEWORK_VERSION = "1.0.0"
# API v2: retrospective is a full record. v4: unregistered project is 400 (proposal / new_project / sentinel).
API_VERSION = 4
CLIENT_VERSION_HEADER = "X-SM-Api-Version"
#: Client build, not API_VERSION. The same wire version can hide an older build, and a client that omits the header is not counted.
CLIENT_BUILD_HEADER = "X-Shared-Memory-Client"

# A record id is unique only inside its table; qualify as fact:N / summary:N (bare ints still mean technical_docs) (decision:882).
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


# Auth exemption uses the router's own path_safe string, exact match, plain routes only (A1).
_UNPROTECTED_PATHS = {"/health", "/pool/status"}


def _router_match_path(request) -> "str | None":
    """aiohttp's path_safe string, or None (deny) if it is missing or not a str."""
    rel_url   = getattr(request, "rel_url", None)
    path_safe = getattr(rel_url, "path_safe", None)
    return path_safe if isinstance(path_safe, str) else None


def require_unprotected_paths_are_plain_routes(router) -> None:
    """Boot-fail unless every unprotected path is a registered PlainResource canonical (A1)."""
    from yarl import URL as _URL  # aiohttp's own hard dependency; always present
    if not isinstance(getattr(_URL("/health"), "path_safe", None), str):
        raise RuntimeError(
            "the installed yarl does not expose URL.path_safe, which is the "
            "attribute aiohttp's router matches on and the attribute the "
            "_UNPROTECTED_PATHS exemption compares. Without it every request "
            "would be denied the exemption and /health would require a token. "
            "Install the pinned dependency set (requirements-gateway.lock)."
        )
    plain: set = set()
    dynamic: dict = {}
    for resource in router.resources():
        canonical = getattr(resource, "canonical", None)
        if not isinstance(canonical, str):
            continue
        if isinstance(resource, web.PlainResource):
            plain.add(canonical)
        else:
            dynamic[canonical] = type(resource).__name__
    for entry in sorted(_UNPROTECTED_PATHS):
        if entry in plain:
            continue
        if entry in dynamic:
            raise RuntimeError(
                f"_UNPROTECTED_PATHS entry {entry!r} resolves to a "
                f"{dynamic[entry]}, not a PlainResource. A dynamic canonical "
                f"is many-to-one, so exempting it would make every path "
                f"behind that pattern anonymous. Register the unauthenticated "
                f"endpoint as a static route, or drop it from the set."
            )
        raise RuntimeError(
            f"_UNPROTECTED_PATHS entry {entry!r} is not the canonical of any "
            f"registered route. An exemption for a path the router does not "
            f"own cannot be honoured by the router — it can only be honoured "
            f"by the catch-all, which forwards to an LLM backend."
        )


# Parse accepts plaintext so boot can name the agents; require_no_plaintext_agent_tokens() then refuses.
_PLAINTEXT_AGENT_TOKENS_SEEN: list[str] = []

# Digest-form AGENT_TOKENS entries: name:sha256:<64-hex-char digest>.
_DIGEST_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _token_digest(token: str) -> str:
    """SHA-256 hex digest of a bearer token — the only form ever stored in
    _AGENT_TOKENS or compared against a presented credential (SEC-07)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_agent_tokens() -> dict[str, str]:
    """Parse AGENT_TOKENS via get_secret into digest→name; plaintext entries are hashed then refused at boot."""
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
            # A 3-part split whose middle is not sha256 is still a plaintext token (rejoin after the first colon) so require_no_plaintext_agent_tokens() can refuse boot.
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

# AUTH_CONFIGURED_AT_STARTUP is captured once at boot, before daemon mint mutates _AGENT_TOKENS; request-time emptiness would flip an auth-off install to authenticating.
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


# A read role may reach only this set. Saves, graph, and the LLM proxy are 403; /memory/graph stays off so a read token cannot traverse it (S1).
_READ_ROLE_ROUTES: set[tuple[str, str]] = {
    ("GET",  "/memory/telemetry"),
    # Search is a read: a monitor can query knowledge without /memory/graph, which stays full or admin (S1).
    ("POST", "/memory/search"),
}

# Client WRITE routes — shed (503 + Retry-After) while a backup quiesce is active.
# Reads (search/telemetry/status) and /health always flow (/memory/graph requires full or admin role).
_WRITE_ROUTES: set[tuple[str, str]] = {
    ("POST", "/memory/save"),
    ("POST", "/memory/retrospective"),
    ("POST", "/memory/supersede"),
    ("POST", "/memory/review_hold"),
}

# An admin token reaches only these routes, so a leaked backup token cannot save. fact:2022: polling /memory/telemetry was 403, so this census is the read the drain gate can finish.
_ADMIN_ROUTES: set[tuple[str, str]] = {
    ("POST", "/admin/backup"),
    ("GET", "/admin/outbox"),
}

# When set, writes require an AF_UNIX SO_PEERCRED principal. Off by default so TCP writers still work until every writer is on the socket.
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


# One path segment, the same shape aiohttp's DynamicResource grants, so this matches only /memory/status/{pg_id}.
_MEMORY_STATUS_RE = re.compile(r"/memory/status/[^{}/]+")


def _read_role_permits(request: web.Request) -> bool:
    """True if a read-only role may reach this route (method+path allowlist)."""
    path = request.path.rstrip("/") or "/"
    if (request.method, path) in _READ_ROLE_ROUTES:
        return True
    # GET /memory/status/{pg_id} is read-role; match by fullmatch, not prefix (extra segments used to grant then fall through to the LLM catch-all).
    return request.method == "GET" and bool(_MEMORY_STATUS_RE.fullmatch(path))


# resolve_identity() returns the first verified agent name; downstream only sees the name (PoP plugs in here).
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


def _extract_bearer_token_ci(request: web.Request) -> str | None:
    """Like _extract_bearer_token, but the scheme match is CASE-INSENSITIVE
    (F, S4, ADV2-15 — RFC 7235 SS2.1: an auth-scheme token is compared
    case-insensitively). Used ONLY by the unprotected-path token-oracle
    audit in auth_middleware below, which must see -- and count as a
    verify-failure attempt -- a presented `bearer`/`BEARER` scheme just as
    readily as `Bearer`. _extract_bearer_token itself, the PROTECTED-path
    helper, keeps its existing case-sensitive match unchanged: widening it
    would be a distinct, unscoped behaviour change to identity resolution,
    not this item's job."""
    parts = request.headers.get("Authorization", "").split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
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


# Principal is the kernel SO_PEERCRED login on AF_UNIX (never a client claim); TCP has no peercred so principal is None (decision:347).
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
    """When a UDS principal exists, store it as decided_by and keep the typed wording as decided_by_claimed."""
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
    """Error string if this record_type cannot be superseded (judgements use a retrospective); None if a fact may be."""
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


# fact:1215: entities_provenance is who named each entity, operator or agent. Anything else is a shape error, not a new spelling.
ENTITIES_PROVENANCE_VALUES = ("operator", "agent")

# Only facts carry entities; judgements inherit topics from grounded facts. Ask via is_judgement_type(), never a raw-string tuple (decision:1664, fact:970).
JUDGEMENT_LABELS = (ONT.decision, ONT.retrospective)


def is_judgement_type(record_type: object) -> bool:
    """Is this record type a JUDGEMENT? Pure.

    ⛔ IT DELEGATES TO `record_label_for_type` AND HOLDS NO COPY OF THE RULE.
    The E3 gate first spelled this as an EXACT `in ("decision", "retrospective")`
    match — while `record_label_for_type` (which decides the record's
    graph LABEL, and therefore what it actually IS everywhere downstream)
    lowercases and strips first. So `{"type": "Decision"}` was a Decision to the
    graph and a fact to the gate: it carried entities straight past the refusal
    and minted them. A second normalisation is a second rule; there is now one,
    in one place, and this asks it the question rather than re-deciding it.
    """
    return record_label_for_type(record_type) in JUDGEMENT_LABELS


class ProjectIdentityUnavailable(RuntimeError):
    """Registry lookup failed for a name that should have an id: outbox retries, save is 503, readers degrade."""


class DomainIdentityUnavailable(RuntimeError):
    """Registry lookup failed for a section that should have an id: outbox retries; a missing row stays None."""


_OUTBOX_ROW_TYPES = frozenset({
    "fact", "decision", "retrospective", "supersede", "project_of", "domain_of",
})
_OUTBOX_GRAPH_PRESENT = frozenset({"applied", "rem_reviewed", "consolidated"})
_DREAM_CYCLE_OUTBOX_TYPE = (
    "(cypher_params->>'type' IS NULL"
    " OR cypher_params->>'type' IN ('fact','decision','retrospective'))"
)


def _outbox_row_type(raw) -> str:
    """Known outbox type, or fact for missing/blank. Unknown non-empty stays unknown so `_require_outbox_type` raises."""
    if not isinstance(raw, str) or not raw.strip():
        return "fact"
    t = raw.strip().lower()
    return t if t in _OUTBOX_ROW_TYPES else t


def _require_outbox_type(params: dict) -> None:
    """Refuse an unknown cypher_params type before INSERT (CHECK is the other half)."""
    t = params.get("type") if isinstance(params, dict) else None
    if t is None or t == "":
        return
    if isinstance(t, str) and t.strip().lower() in _OUTBOX_ROW_TYPES:
        return
    raise ValueError(f"unknown neo4j_outbox type {t!r}")


def _record_kind_label(metadata, incoming_type=None) -> str:
    """Graph kind for the frozen kind axis: record_label_for_type, plus a decision blob means Decision."""
    meta = _coerce_jsonb_obj(metadata) if metadata is not None else {}
    if not isinstance(meta, dict):
        meta = {}
    t = incoming_type if incoming_type is not None else meta.get("type")
    label = record_label_for_type(t)
    blob = meta.get("decision")
    if label == ONT.fact and isinstance(blob, dict) and blob:
        return ONT.decision
    return label


# Resolve one name via entity_normalize: canonical first, then alias (fact:1375).
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

# Only INSERT into entity_vocabulary; aliases are operator-curated (decision:1380).
ENTITY_VOCAB_MINT_SQL = """
    INSERT INTO entity_vocabulary (name, registered_by)
    VALUES ($1, $2)
    ON CONFLICT (normalized_key) DO NOTHING
    RETURNING id, name
"""

# One round-trip resolve for a list of names (fact:1412).
ENTITY_VOCAB_RESOLVE_MANY_SQL = """
    SELECT i.raw_name,
           COALESCE(canon.name, alias_canon.name) AS canonical_name
      FROM unnest($1::text[]) AS i(raw_name)
      LEFT JOIN entity_vocabulary canon
        ON canon.normalized_key = entity_normalize(i.raw_name)
      LEFT JOIN entity_vocab_aliases a
        ON a.normalized_alias = entity_normalize(i.raw_name)
      LEFT JOIN entity_vocabulary alias_canon
        ON alias_canon.id = a.entity_id
     GROUP BY i.raw_name, COALESCE(canon.name, alias_canon.name)
"""

# Near-match names/aliases at mint time; key-identical spellings already resolve and never reach this (fact:1734).
ENTITY_CONFUSABLE_SQL = """
    SELECT name, similarity(name, $1) AS score
      FROM (
            SELECT name FROM entity_vocabulary
            UNION ALL
            SELECT alias AS name FROM entity_vocab_aliases
           ) v
     WHERE similarity(name, $1) >= $2 AND name <> $1
     ORDER BY similarity(name, $1) DESC, name
     LIMIT $3
"""

# Floor inherited from projects; not re-measured on this vocabulary (fact:1338).
ENTITY_CONFUSABLE_SIMILARITY = float(
    os.environ.get("ENTITY_CONFUSABLE_SIMILARITY", "0.6")
)
ENTITY_PROPOSAL_LIMIT = _env_int("ENTITY_PROPOSAL_LIMIT", 5)


# A registered (or retired-alias) project name is an axis, not an entity; refuse minting it (fact:1215, fact:1734).
ENTITY_RESERVED_PROJECT_SQL = (
    "SELECT p.name AS name, p.normalized_key AS matched_key"
    "  FROM projects p"
    " WHERE p.normalized_key = ANY($1::text[])"
    " UNION ALL"
    " SELECT p.name AS name, axis_normalize(a.name) AS matched_key"
    "  FROM project_aliases pa"
    "  JOIN aliases a ON a.id = pa.alias_id"
    "  JOIN projects p ON p.id = pa.project_id"
    " WHERE pa.active AND axis_normalize(a.name) = ANY($1::text[])"
)

# general_discussion is a project-axis sentinel the projects CHECK excludes, so the registry query cannot reserve that name.
RESERVED_ENTITY_AXIS_KEYS: dict[str, str] = {
    axis_key(SENTINEL): SENTINEL,
}

# Entity name/list caps are DoS bounds measured above this corpus (fact:1338), env-overridable.
ENTITY_NAME_MAX_LEN = _env_int("ENTITY_NAME_MAX_LEN", 200)
ENTITY_LIST_MAX_LEN = _env_int("ENTITY_LIST_MAX_LEN", 50)


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
    and search matching — but Tier 3 eligibility is not one of the
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
        " search matching only."
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


# S-11 load-shed is first in auth_middleware and counts every admitted request, including anonymous /health.
_inflight = 0

# In-memory gateway latency/status counters; recorders swallow so they cannot add a failure mode the audit line does not already have.
GATEWAY_LATENCY_WINDOW = int(os.environ.get("GATEWAY_LATENCY_WINDOW", "500"))
_gateway_latency = LatencyRing(GATEWAY_LATENCY_WINDOW)
_gateway_requests_total = 0
_gateway_shed_503_total = 0
# LLM client-abort counter lives here (shallow telemetry merge would clobber a hive-side copy of the gateway section).
_gateway_client_disconnects_total = 0
_gateway_by_status: dict[str, int] = {
    "2xx": 0, "4xx": 0, "5xx": 0, "401": 0, "403": 0, "409": 0, "503": 0,
}
#: Requests per client build. A missing header is not a version; do not invent an "unknown" bucket for old clients.
_client_versions_seen: dict[str, int] = {}
#: Cap on distinct client builds. The header is caller-supplied, so the map is unbounded without this.
CLIENT_VERSIONS_MAX = int(os.environ.get("CLIENT_VERSIONS_MAX", "64"))


def _record_gateway_request(status: int, latency_ms: "float | None") -> None:
    """Fold one served request into the gateway instrument. Never raises.

    D2 (OBS round): ``latency_ms`` is now optional. ``requests_total`` and
    ``by_status.*`` count EVERY exit `auth_middleware` can take — the shed
    valve, the auth-off bypass, the unprotected-path exemption, every
    HTTPException it raises itself, and the original authenticated
    handler-reached path. The LATENCY RING stays on the OLD boundary only
    (ruling R-C): a caller passes ``None`` from every one of the nine
    early-exit sites, and only the authenticated, handler-reached call
    site (where ``started`` is taken) ever passes a real float. Gateway
    counting therefore widened; the meaning of ``gateway.latency_p50/p95_ms``
    did not move, so there is no MEANING_CHANGES entry for it."""
    global _gateway_requests_total
    try:
        _gateway_requests_total += 1
        if latency_ms is not None:
            _gateway_latency.record(latency_ms)
        cls = f"{status // 100}xx"
        if cls in _gateway_by_status:
            _gateway_by_status[cls] += 1
        key = str(status)
        if key in _gateway_by_status:
            _gateway_by_status[key] += 1
    except Exception:
        pass


def telemetry_gateway_counters() -> dict:
    """The gateway counters, read across the module boundary.

    An accessor rather than an import of the names themselves: these are
    REBOUND integers, so `from coordinator import _gateway_shed_503_total` would
    capture the value at import time and never move again — a counter frozen at
    zero that looks exactly like a counter that never fired.
    """
    return {
        "requests_total": _gateway_requests_total,
        "shed_503_total": _gateway_shed_503_total,
    }


def telemetry_credential_counters() -> dict:
    """Ditto for the credential counters (a dict, so this is only for symmetry
    and to keep every cross-module telemetry read in one place)."""
    return dict(_credential_counters)


def telemetry_token_verify_ring() -> list:
    """D1: a synchronous snapshot of the token_verify_failed rate ring — the
    monotonic timestamp of every event, bounded to the most recent 256 (see
    `_token_verify_failure_ring`'s declaration for why 256 is fixed).

    A plain list copy, never the deque itself: the proxy's
    `_token_verify_failure_rate` walks this with an injectable `now` (tests
    inject the clock; production passes None and reads the live one) and
    counts entries within a true 60 s window. No await anywhere in this
    path — the ring is appended synchronously at both bump sites in this
    module, and read synchronously here."""
    return list(_token_verify_failure_ring)


class _TimedAcquire:
    """Times a pool acquire without changing anything about it.

    A pure delegation wrapper: ``__aenter__`` and ``__aexit__`` forward to the
    asyncpg acquire context, so the POOL_ACQUIRE_TIMEOUT, the
    ``asyncio.TimeoutError`` that auth_middleware maps to a 503, and the
    connection's release are all exactly as before. The clock reads and the ring
    write are the only additions, and the ring write cannot raise.
    """

    __slots__ = ("_ctx", "_ring", "_t0")

    def __init__(self, ctx, ring):
        self._ctx = ctx
        self._ring = ring
        self._t0 = 0.0

    async def __aenter__(self):
        self._t0 = time.monotonic()
        try:
            conn = await self._ctx.__aenter__()
        except asyncio.CancelledError:
            # Cancellation is not a pool failure: shutdown or disconnect says nothing about the pool, and counting it would look like a database outage.
            raise
        except BaseException:
            self._ring.record_error()
            raise
        self._ring.record((time.monotonic() - self._t0) * 1000.0)
        return conn

    async def __aexit__(self, exc_type, exc, tb):
        return await self._ctx.__aexit__(exc_type, exc, tb)


def _record_client_version(request: web.Request) -> None:
    """Count the caller's client VERSION (not api_version). Never raises."""
    try:
        raw = request.headers.get(CLIENT_BUILD_HEADER)
        if not raw:
            return
        # Caller-supplied and copied into telemetry: keep only a short version-shaped string.
        v = raw.strip()[:32]
        if not v or not all(c.isalnum() or c in "._-+" for c in v):
            return
        if v not in _client_versions_seen and len(_client_versions_seen) >= CLIENT_VERSIONS_MAX:
            return
        _client_versions_seen[v] = _client_versions_seen.get(v, 0) + 1
    except Exception:
        pass

# Backup quiesce sheds client writes (503 Retry-After); reads flow. Daemons take the advisory lock SHARED and skip if the gateway holds it EXCLUSIVE.
_backup_quiesce: bool = False

# Shared with REM and NREM; the key must match both daemons. A disconnect drops the session lock, so a crash cannot wedge the others.
BACKUP_ADVISORY_LOCK_KEY    = _env_int("BACKUP_ADVISORY_LOCK_KEY", 8765309)
# Mirror of rem_loop.REM_MAX_ATTEMPTS for the give-up count. The gateway does not enforce it, so the default must match the daemon.
REM_MAX_ATTEMPTS            = _env_int("REM_MAX_ATTEMPTS", 5)
# Mirror of rem_loop.REM_STARVED_THRESHOLD (decision 890). The gateway only reports how many pending rows are at the promotion point, so the default must match the daemon.
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
            "role": effective_role(agent, _AGENT_ROLES.get(agent)),
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": round(latency_ms, 1),
            "request_id": request_id,
        }
        # Kernel SO_PEERCRED account and fingerprint. Absent on TCP, and never taken from the client.
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


# Credential-audit log is faults/lifecycle only (ISO 27001 A.5.17); per-request use stays on the gateway audit line. gateway=our decision, llm=upstream said.
_llm_fault_counters: dict[str, dict] = {}
_credential_counters: dict[str, int] = {
    "token_verify_failed": 0,
    "daemon_tokens_issued": 0,
    # S-04: a provider key was offered on a path that is not a framework endpoint.
    "credentialed_route_denied": 0,
}
# Credential counters stamp last-moved at increment (no-token 401 is unlogged; a restart would invert a poll-delta).
_credential_last_ts: dict[str, str | None] = {
    "token_verify_failed": None,
    "daemon_tokens_issued": None,
    "credentialed_route_denied": None,
}

# Bounded monotonic ring of token_verify_failed timestamps (maxlen=256, not derived from the warn threshold) so /health rate is not poll-cadence.
_token_verify_failure_ring: "deque[float]" = deque(maxlen=256)


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


# Refuse a body this large before json.loads, and bound the claimed error code before it reaches telemetry or a log (R-2/R-4).
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

# Encodings both decompress helpers accept. hive_mind_proxy's usage gate imports this so the two paths cannot drift.
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


# Decompressed-output cap for usage capture. Measured gzip is about 2.9:1, so 32× the 2 MiB compressed cap stays above real bodies and cuts the measured 1028:1 worst case.
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
        # 16+MAX_WBITS is the gzip header. max_length stops an over-cap body past cap+1.
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
# On unless the env is empty, unlike GATEWAY_AUDIT_LOG_PATH. The filename matches the shipped *-audit.jsonl logrotate glob.
_credential_audit_writer = (
    AsyncLineWriter(os.path.expanduser(CREDENTIAL_AUDIT_LOG_PATH))
    if CREDENTIAL_AUDIT_LOG_PATH.strip() else None
)

# Flood-controlled credential-audit events drop-NEWEST so they cannot evict genuine lifecycle evidence.
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


# Throttle token_verify_failed log lines only (C-1). The caller controls the volume, so the counter stays complete and the log is a separate bucket refilled over the window.
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
    _token_verify_failure_ring.append(time.monotonic())
    # Stamp last-moved before the C-1 early return so an unlogged no-token 401 still has a time, without a disk write.
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


# Separate bucket for verify failures on /health and /pool/status, so a flood there cannot exhaust the protected-path log budget.
_tvf_unprotected_bucket_tokens: float = float(TOKEN_VERIFY_FAILED_LOG_RATE)
_tvf_unprotected_bucket_last_refill: float = time.monotonic()
_tvf_unprotected_suppressed_count: int = 0
_tvf_unprotected_suppressed_since: float | None = None


def _tvf_unprotected_rate_limit_allow() -> bool:
    """Own bucket, identical refill logic to _tvf_rate_limit_allow() — see
    that function's docstring. Kept SEPARATE so unprotected-path noise
    cannot suppress a protected-path line (ADV1-16)."""
    global _tvf_unprotected_bucket_tokens, _tvf_unprotected_bucket_last_refill
    now = time.monotonic()
    elapsed = max(0.0, now - _tvf_unprotected_bucket_last_refill)
    _tvf_unprotected_bucket_last_refill = now
    refill_rate = (TOKEN_VERIFY_FAILED_LOG_RATE / TOKEN_VERIFY_FAILED_LOG_WINDOW
                   if TOKEN_VERIFY_FAILED_LOG_WINDOW > 0 else 0.0)
    _tvf_unprotected_bucket_tokens = min(
        float(TOKEN_VERIFY_FAILED_LOG_RATE),
        _tvf_unprotected_bucket_tokens + elapsed * refill_rate,
    )
    if _tvf_unprotected_bucket_tokens >= 1.0:
        _tvf_unprotected_bucket_tokens -= 1.0
        return True
    return False


def _record_unprotected_path_token_verify_failed(request: web.Request, presented_token: str) -> None:
    """Audit a bad bearer on /health or /pool/status without changing the anonymous response (decision:1785)."""
    global _tvf_unprotected_suppressed_count, _tvf_unprotected_suppressed_since
    _credential_counters["token_verify_failed"] += 1
    _token_verify_failure_ring.append(time.monotonic())
    _credential_last_ts["token_verify_failed"] = datetime.now(timezone.utc).isoformat()
    if not _tvf_unprotected_rate_limit_allow():
        if _tvf_unprotected_suppressed_count == 0:
            _tvf_unprotected_suppressed_since = time.monotonic()
        _tvf_unprotected_suppressed_count += 1
        return
    if _tvf_unprotected_suppressed_count:
        window_s = (round(time.monotonic() - _tvf_unprotected_suppressed_since, 1)
                    if _tvf_unprotected_suppressed_since is not None else None)
        _write_credential_audit_line(
            "token_verify_failed_suppressed", origin="gateway",
            count=_tvf_unprotected_suppressed_count, window_s=window_s,
        )
        _tvf_unprotected_suppressed_count = 0
        _tvf_unprotected_suppressed_since = None

    fields: dict[str, Any] = {
        "claimed_agent": None,
        "digest_prefix": _token_digest(presented_token)[:8],
        "path": request.path,
        "transport": _transport_kind(request),
        "unprotected_path": True,
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


def record_llm_client_disconnect() -> None:
    """D9 (OBS round): a CLIENT (the caller of our gateway) aborted an
    LLM-proxy request — either before we could write response headers, or
    partway through the streamed body. Deliberately NOT a per-backend fault:
    the backend saw nothing wrong, so this counts only under `gateway.*`,
    the same namespace `shed_503_total` already uses for a gateway-side
    event that is not about any one backend. Never raises."""
    global _gateway_client_disconnects_total
    try:
        _gateway_client_disconnects_total += 1
    except Exception:
        pass


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
    # Read the writer once. A swap between two reads can pair a non-zero drop count with a null stamp (I1).
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


def _error_body(message: str) -> dict:
    """Keyword arguments that give an aiohttp HTTPException the SAME JSON error
    body every handler in this file already returns: {"status", "message"}.

    Why this exists (fact:1503). aiohttp renders an unadorned HTTPException as
    a plain-text page — ``"403: Read-only token: this route requires a
    write-capable agent token"``. A client that decodes before branching on the
    status class hands that page to ``json.loads`` and gets
    ``JSONDecodeError: Extra data: line 1 column 4 (char 3)``, which reads as a
    transport fault, not an authorization refusal — a live gateway reported as
    a dead one. The status line (``reason``) is unchanged; only the BODY gains
    the shape the rest of the gateway already speaks, so a client can read the
    refusal instead of guessing at it.

    Additive on the error path only: no 2xx payload changes shape, so this is
    not an API_VERSION event.
    """
    return {"text": json.dumps({"status": "error", "message": message}),
            "content_type": "application/json"}


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
    global _inflight, _gateway_shed_503_total
    _check_client_version(request)  # logs API skew to the gateway log; never raises
    _record_client_version(request)  # counts the caller's build; never raises

    # Load-shed is the first gate (ahead of auth-off and unprotected exemptions) so anonymous /health floods still count.
    if GATEWAY_INFLIGHT_MAX and _inflight >= GATEWAY_INFLIGHT_MAX:
        # Counted here: a shed request never reaches the audit line, so this is the only 503 /health can see.
        try:
            _gateway_shed_503_total += 1
        except Exception:
            pass
        # Only place a shed 503 enters requests_total. Pass None: this exit never takes started, so it stays out of the latency ring (R-C).
        _record_gateway_request(503, None)
        raise web.HTTPServiceUnavailable(
            reason="gateway at capacity", headers={"Retry-After": "1"},
            **_error_body("Gateway at capacity — too many requests in flight; "
                          "retry after the Retry-After interval."),
        )

    _inflight += 1
    try:
        # Use the boot snapshot. A later daemon mint fills _AGENT_TOKENS and would flip an auth-off install.
        if not AUTH_CONFIGURED_AT_STARTUP:
            _status = 500
            try:
                resp = await handler(request)
                _status = resp.status
                return resp
            except web.HTTPException as exc:
                _status = exc.status
                raise
            finally:
                _record_gateway_request(_status, None)
        # Exempt /health and /pool/status by the router's path_safe string (A1).
        if _router_match_path(request) in _UNPROTECTED_PATHS:
            _unprotected_presented = _extract_bearer_token_ci(request)
            if _unprotected_presented is not None and not _lookup_agent_by_token(_unprotected_presented):
                _record_unprotected_path_token_verify_failed(request, _unprotected_presented)
            _status = 500
            try:
                resp = await handler(request)
                _status = resp.status
                return resp
            except web.HTTPException as exc:
                _status = exc.status
                raise
            finally:
                _record_gateway_request(_status, None)

        agent_name = resolve_identity(request)
        if not agent_name:
            # RFC 6750: a presented-but-rejected token gets error=invalid_token and a digest; no token gets the bare challenge. This 401 is the gateway's, not upstream.
            presented = _extract_bearer_token(request)
            _record_token_verify_failed(request, presented)
            www_authenticate = 'Bearer error="invalid_token"' if presented else "Bearer"
            # Count the gateway's own 401. Pass None: this exit never takes started (R-C).
            _record_gateway_request(401, None)
            raise web.HTTPUnauthorized(
                reason="Authorization: a valid Bearer token is required",
                # Presence, not absence, marks a gateway 401. A stripping proxy would otherwise make it look upstream (O-5).
                headers={"WWW-Authenticate": www_authenticate, "X-SM-Fault-Origin": "gateway"},
                **_error_body("Authorization: a valid Bearer token is required."),
            )
        request["authenticated_agent"] = agent_name
        # Handlers read the kernel principal from here. None on TCP, and never inferred from the agent name.
        principal = _peer_identity(request)
        request["principal"] = principal
        # Read stays off /memory/graph; admin stays on /admin/*; quiesce sheds writes so the dump sees a quiet database. Reads still flow.
        role  = effective_role(agent_name, _AGENT_ROLES.get(agent_name))
        route = (request.method, request.path.rstrip("/") or "/")
        # Each refusal below is counted here. Pass None: these exits never take started, so they stay out of the latency ring (R-C).
        if role == "read" and not _read_role_permits(request):
            _record_gateway_request(403, None)
            raise web.HTTPForbidden(
                reason="Read-only token: this route requires a write-capable agent token",
                **_error_body("Read-only token: this route requires a write-capable "
                              "agent token. The credential is VALID — it is confined to "
                              "the read allowlist, so this is a role refusal, not an "
                              "authentication failure."),
            )
        if route in _ADMIN_ROUTES:
            if role != "admin":
                _record_gateway_request(403, None)
                raise web.HTTPForbidden(
                    reason="This route requires an admin-role token",
                    **_error_body("This route requires an admin-role token. The "
                                  "credential is VALID but does not carry the admin role."),
                )
        else:
            if role == "admin":
                _record_gateway_request(403, None)
                raise web.HTTPForbidden(
                    reason="Admin token is confined to /admin/* routes",
                    **_error_body("Admin token is confined to /admin/* routes. The "
                                  "credential is VALID — use a write-capable agent token "
                                  "for this route."),
                )
            if _backup_quiesce and route in _WRITE_ROUTES:
                # This 503 is quiesce, not the shed valve. by_status.503 counts both (MEANING_CHANGES).
                _record_gateway_request(503, None)
                raise web.HTTPServiceUnavailable(
                    reason="backup in progress — writes are briefly paused",
                    headers={"Retry-After": str(BACKUP_RETRY_AFTER)},
                    **_error_body("Backup in progress — writes are briefly paused so the "
                                  "dump sees a quiet database. Reads are unaffected; "
                                  "retry after the Retry-After interval."),
                )
            if GATEWAY_REQUIRE_PRINCIPAL and route in _WRITE_ROUTES and principal is None:
                _record_gateway_request(403, None)
                raise web.HTTPForbidden(
                    reason="writes require a kernel-attested principal — connect over the "
                           "gateway Unix socket (GATEWAY_UDS_PATH), not TCP",
                    **_error_body("Writes require a kernel-attested principal — connect "
                                  "over the gateway Unix socket (GATEWAY_UDS_PATH), not "
                                  "TCP. The credential is VALID; the TRANSPORT is what "
                                  "this route refuses."),
                )

        started    = asyncio.get_running_loop().time()
        request_id = uuid.uuid4().hex[:12]
        status     = 500
        # The proxy correlates credential-audit lines to this request (PR A3).
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
                **_error_body("Database pool saturated past POOL_ACQUIRE_TIMEOUT — the "
                              "gateway is UP and shedding rather than hanging; retry "
                              "after the Retry-After interval."),
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
            # After the durable audit line. This call cannot raise, so it cannot skip the write.
            _record_gateway_request(status, latency_ms)
    finally:
        _inflight -= 1

# PG_PASSWORD/NEO4J_PASSWORD/PG_CONN via secure_env.get_secret() (a DSN embeds the password); hive loads the store before importing this module.

_pg_pass = get_secret("PG_PASSWORD", "")
PG_DSN   = get_secret(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", get_secret("NEO4J_PASSWORD", ""))

# Cap the shared driver so a search burst fails the acquire instead of queueing forever.
NEO4J_MAX_POOL        = _env_int("NEO4J_MAX_POOL", 50)
NEO4J_ACQUIRE_TIMEOUT = _env_float("NEO4J_ACQUIRE_TIMEOUT", 30.0)

# Coordinator calls EMBEDDER_URL/RERANKER_URL directly (same env as the gateway map); strip pasted /v1 suffixes longest-first.
_ENCODER_BASE_SUFFIXES = ("/v1/embeddings", "/v1/reranking", "/v1")


def normalize_encoder_base(base: str) -> str:
    """Strip a pasted encoder-path suffix from an encoder BASE URL.
    Parses with urllib.parse so scheme, netloc and userinfo are preserved; only
    the PATH component is touched. Terminal slashes are removed, then the
    longest of ('/v1/embeddings', '/v1/reranking', '/v1') that the path ends
    with is stripped (longest first so '/v1' cannot eat '/v1/embeddings').
    A path that is not one of those (e.g. a proxy prefix '/api') is retained.

    A query string or fragment is REJECTED (ValueError): the framework appends
    /v1/... itself, and only the coordinator can join onto a query — the
    gateway's own string joins cannot, so a query-bearing base would work on
    save/search but break the passthrough, capability probes and /health
    fan-out. Credentials belong in userinfo (preserved in netloc) or a header,
    not the query.

    The scheme is lowercased by urlsplit (RFC 3986 schemes are case-
    insensitive), which is the correct normalization. Returns the normalized
    base with no trailing slash.
    """
    parsed = urllib.parse.urlsplit(base)
    if parsed.query or parsed.fragment:
        raise ValueError(
            "encoder base must not carry a query string or fragment (the "
            "framework appends /v1/... itself; use userinfo or a header for "
            f"credentials): {scrub_url_credentials(base)}"
        )
    path = parsed.path.rstrip("/")
    for suffix in _ENCODER_BASE_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, "", "")
    )


def _encoder_url(env_name: str, default_base: str, path: str) -> str:
    """Full endpoint for an encoder backend: env-overridable BASE + fixed PATH.

    The BASE is normalized first (normalize_encoder_base): a pasted encoder
    path — '/v1', '/v1/embeddings', '/v1/reranking' — is stripped so a base
    copied from an LM Studio-style URL (http://host:1234/v1) does not resolve
    to the doubled http://host:1234/v1/v1/embeddings that LM Studio answers
    with 200, masking the defect. A non-encoder prefix (e.g. '/api') is kept.

    Validated at the same time it is derived (module import/reload) so a bad
    value is caught before the process ever accepts traffic, rather than
    surfacing as an opaque connection error on the first save/search:
      - the resolved BASE must be an http(s) URL — anything else (a bare
        host, a typo'd scheme, a leftover placeholder) fails LOUDLY, naming
        env_name, rather than producing a confusing httpx/aiohttp exception
        deep inside _embed()/_rerank() on the first real request.
      - a known encoder suffix that was stripped is logged at INFO (the
        suffix and the scrubbed URL named).
      - a base still carrying a NON-encoder path segment only WARNS (never
        fails): the endpoint appends onto that prefix, which is legitimate for
        a proxy but worth naming.
      - a query string or fragment on the base fails LOUDLY (the gateway's own
        joins cannot carry one — see normalize_encoder_base).
    """
    raw_base = (os.environ.get(env_name) or default_base).strip()
    base = normalize_encoder_base(raw_base)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"{env_name} must be an http(s) URL, got {base!r} "
            f"(scheme {parsed.scheme!r}) — check {env_name} in shared-memory/.env"
        )
    # Set the path on the parsed base. String-joining would glue a proxy prefix such as /api onto the wrong place.
    endpoint = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path + path, "", "")
    )
    raw_path = urllib.parse.urlsplit(raw_base).path.rstrip("/")
    stripped = raw_path[len(parsed.path):]
    if stripped:
        log.info(
            "%s (%s) carried the encoder path %r — stripped it and using %s "
            "as the base (one %s is appended).",
            env_name, scrub_url_credentials(raw_base), stripped,
            scrub_url_credentials(base), stripped,
        )
    if parsed.path:
        log.warning(
            "%s (%s) already carries a path (%r) — the resolved endpoint "
            "will be %s, appending onto whatever is already there. "
            "%s should normally be just scheme://host[:port], with no path.",
            env_name, scrub_url_credentials(raw_base), parsed.path,
            scrub_url_credentials(endpoint), env_name,
        )
    return endpoint

EMBED_URL  = _encoder_url("EMBEDDER_URL", FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"], "/v1/embeddings")
RERANK_URL = _encoder_url("RERANKER_URL", FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"], "/v1/reranking")

# Encoder-base INFO log runs from MemoryCoordinator.start() (after hive basicConfig); an import-time INFO was dropped.
_encoder_endpoints_logged = False

def log_encoder_endpoints() -> None:
    """Log the resolved encoder endpoints once per process, scrubbed the same
    way the failure-path messages already are (see _embed()'s
    scrub_url_credentials use) so a credential embedded in the URL never
    lands in a log file even on the success path. Idempotent — a second
    call is a no-op, so a caller does not need to track whether it already
    fired."""
    global _encoder_endpoints_logged
    if _encoder_endpoints_logged:
        return
    _encoder_endpoints_logged = True
    log.info(
        "encoder endpoints resolved: EMBED_URL=%s RERANK_URL=%s",
        scrub_url_credentials(EMBED_URL), scrub_url_credentials(RERANK_URL),
    )

EMBED_RETRIES = 4
EMBED_BACKOFF = 0.5      # seconds × attempt number  (0.5 s, 1 s, 1.5 s, 2 s)
# Truncate embedding input to the dream_telemetry char budget from the 8192-token context; full text stays in Tier 1.
from dream_telemetry import (EMBED_CHARS_PER_TOKEN, EMBED_MAX_CHARS,  # noqa: E402
                             EMBED_MAX_CONTEXT_TOKENS, EMBED_SPECIAL_TOKEN_RESERVE,
                             EMBED_TIMEOUT_FLOOR_S, RERANK_MAX_DOC_CHARS,
                             clamp_rerank_doc, prefix_rerank_doc, prefix_rerank_query,
                             embed_ceiling, rerank_ceiling)


# Edges shown per search hit. The Cypher orders asserted and typed relations ahead of bare MENTIONS so the cap keeps the signal.
GRAPH_EXPANSION_LIMIT = _env_int("GRAPH_EXPANSION_LIMIT", 15)

# Row cap on read-only Cypher queries submitted to /memory/graph. Rejects
# oversized result sets with HTTP 400 before serialization.
GRAPH_QUERY_ROW_CAP = _env_int("GRAPH_QUERY_ROW_CAP", 10000)

# Floor, not a cap: the pool is max(this, the caller's limit). A request for 100 must not collapse to this number, or the reranker has nothing extra to reorder.
SEARCH_CANDIDATE_FLOOR = _env_int("SEARCH_CANDIDATE_FLOOR", 20)

# Cap the domains filter. A silent truncate would return 200 for a partial filter and an empty result would look authoritative (PR 235).
SEARCH_DOMAINS_FILTER_CAP = _env_int("SEARCH_DOMAINS_FILTER_CAP", 16)

# decision:1736: RELATION_ASSERTED_INHERITED is gone. Belonging is derived on read, so nothing writes that copied edge; leftover inherited edges are legacy data, not framework code.

# Postgres max_connections must cover this pool plus REM, NREM, LISTEN, and headroom. POOL_ACQUIRE_TIMEOUT sheds 503 instead of hanging.
POOL_MIN = _env_int("POOL_MIN", 2)
POOL_MAX = _env_int("POOL_MAX", 20)
POOL_ACQUIRE_TIMEOUT = _env_float("POOL_ACQUIRE_TIMEOUT", 5.0)

# fact:1609: at boot Postgres may not be accepting yet. An immediate pool create crashed, and Restart=on-failure replayed that race, so start() retries only until this wait.
PG_STARTUP_WAIT_S  = _env_float("PG_STARTUP_WAIT_S", 60.0)
# Floor at 0.1s so a 0 or negative operator value cannot busy-loop a down database.
PG_STARTUP_RETRY_S = max(0.1, _env_float("PG_STARTUP_RETRY_S", 2.0))

# decision:1584, fact:1583: below this pgvector version a selective project or domain filter can empty the HNSW candidates and return zero rows. The session setting is the other half of migration 036.
PGVECTOR_ITERATIVE_SCAN_MIN = (0, 8)


def _parse_pgvector_version(raw: "str | None") -> "tuple[int, int] | None":
    """"0.8.2" -> (0, 8); "1.0.0" -> (1, 0); None or unparseable -> None.

    Only major.minor matter — iterative_scan is a 0.8 feature, not a patch-
    level one. Never raises: an unrecognised string degrades to None, which
    reads as "iterative scan unavailable", the safe direction (it never
    enables a session setting a genuinely-older server would reject).
    """
    if not raw:
        return None
    m = re.match(r"(\d+)\.(\d+)", raw)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))

OUTBOX_POLL_INTERVAL = 2.0   # seconds between outbox drain cycles
OUTBOX_BATCH_SIZE    = 20    # rows processed per cycle
OUTBOX_MAX_RETRIES   = 5     # row marked 'failed' after this many Neo4j errors
CONSISTENCY_TIMEOUT  = 15.0  # seconds to wait for ?consistency=neo4j

# Push a failed outbox row's next_attempt_at out by base·2^retries. Without it a down Neo4j retries the whole batch every poll.
OUTBOX_BACKOFF_BASE = _env_float("OUTBOX_BACKOFF_BASE", 2.0)   # seconds
OUTBOX_BACKOFF_MAX  = _env_float("OUTBOX_BACKOFF_MAX", 300.0)  # seconds (cap)

# Alternative embeddings fill after save (embedding IS NULL is the queue); the sweep is latency, not correctness.
ALT_VECTOR_POLL_INTERVAL = _env_float("ALT_VECTOR_POLL_INTERVAL", 10.0)
ALT_VECTOR_BATCH_SIZE    = _env_int("ALT_VECTOR_BATCH_SIZE", 32)

# OUTBOX_MAX_RETRIES gives up on a permanently unapplicable Cypher; a still-pending row is retried, never abandoned.
ALT_VECTOR_FAILING_AFTER = _env_int("ALT_VECTOR_FAILING_AFTER", 5)

# Evict idle per-entity locks once the registry passes this, so unique names cannot grow it without bound.
LOCKS_MAX_SIZE = _env_int("LOCKS_MAX_SIZE", 4096)

# Cap in-flight requests at the auth seam (0 disables). The pool timeout only covers requests that hold a database connection.
GATEWAY_INFLIGHT_MAX = _env_int("GATEWAY_INFLIGHT_MAX", 100)

# JSON-lines request audit, off the database path. Unset disables it. The identity is the verified agent name.
GATEWAY_AUDIT_LOG_PATH = os.environ.get("GATEWAY_AUDIT_LOG_PATH", "").strip()

# Off-event-loop writer for the audit log (None = auditing disabled). Created at
# import; its drain task starts lazily on the first write within a running loop.
_audit_writer = AsyncLineWriter(GATEWAY_AUDIT_LOG_PATH) if GATEWAY_AUDIT_LOG_PATH else None

# NREM dream-cycle backlog gauge: pending (project, domain) cycles from graph edges, using the same density/insight thresholds as the fold; untagged facts are skipped.

# /health must stay database-free, so a background task caches the consolidation_runs rollup. The stall threshold is 2.5× the sweep so one failed sweep does not trip it.
_NREM_SWEEP_INTERVAL_SEC = int(os.environ.get("NREM_SWEEP_INTERVAL_SEC", "3600"))
CONSOLIDATION_STALL_THRESHOLD_SEC = int(os.environ.get(
    "CONSOLIDATION_STALL_THRESHOLD_SEC", str(int(2.5 * _NREM_SWEEP_INTERVAL_SEC))))
CONSOLIDATION_HEALTH_REFRESH_SEC = int(os.environ.get("CONSOLIDATION_HEALTH_REFRESH_SEC", "60"))
# An in-flight row older than this is a dead fold, so a crashed daemon cannot leave in_flight true forever.
CONSOLIDATION_ORPHAN_TIMEOUT_SEC = int(os.environ.get("CONSOLIDATION_ORPHAN_TIMEOUT_SEC", "1800"))

# Telemetry tunables: every default is unmeasured unless its comment says otherwise (decision:1785, fact:1338).
TELEMETRY_CACHE_S = float(os.environ.get("TELEMETRY_CACHE_S", "15"))
#: Observation window for the encoder latency rings. UNMEASURED.
ENCODER_LATENCY_WINDOW = int(os.environ.get("ENCODER_LATENCY_WINDOW", "200"))
#: Own window for the pool-wait and Neo4j rings. Sharing the encoder window let one rename move three instruments. Both unmeasured.
POOL_WAIT_WINDOW = int(os.environ.get("POOL_WAIT_WINDOW", str(ENCODER_LATENCY_WINDOW)))
NEO4J_LATENCY_WINDOW = int(os.environ.get("NEO4J_LATENCY_WINDOW", str(ENCODER_LATENCY_WINDOW)))
#: Encoder p95 warning. None means derive it from the measured capability ceiling; set the env only to pin a flat one.
_ENCODER_WARN_RAW = os.environ.get("ENCODER_LATENCY_WARN_MS", "").strip()
ENCODER_LATENCY_WARN_MS = float(_ENCODER_WARN_RAW) if _ENCODER_WARN_RAW else None
#: Pending-outbox age that marks the dependency degraded. Unmeasured: one hour is a round number, not an observed drain time.
OUTBOX_AGE_WARN_S = int(os.environ.get("OUTBOX_AGE_WARN_S", "3600"))
#: token_verify_failed climbing faster than this raises a /health warning.
#: UNMEASURED.
TOKEN_VERIFY_WARN_PER_MIN = float(os.environ.get("TOKEN_VERIFY_WARN_PER_MIN", "10"))
#: NREM is degraded after this many folds in 24h with none succeeding. Unmeasured floor so one unlucky fold does not alarm.
NREM_FOLD_ATTEMPT_WARN = int(os.environ.get("NREM_FOLD_ATTEMPT_WARN", "5"))
#: Guard the rem_timing ts cast: one unparseable JSONB value aborts the whole REM telemetry query.
REM_TS_NUMERIC_RE = r"^[0-9]+(\.[0-9]+)?$"
#: Top-N for registry-backed project and domain breakdowns. A hard-coded 12 hid entries on this corpus; agents and sources stay at 12 because those populations are unbounded.
BREAKDOWN_AXIS_TOP_N = int(os.environ.get("BREAKDOWN_AXIS_TOP_N", "50"))


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

    # Order on the raw started_at values. ISO strings sort only while every offset matches, and a type that never ran must not sort as empty.
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


# Word-boundary mutating-keyword regex is the write control (Community READ mode is not a server boundary); over-blocks of n.set / string literals are accepted (fact:1734).
_WRITE_CYPHER = re.compile(
    r"\b(CREATE|DELETE|DETACH\s+DELETE|SET|REMOVE|MERGE|CALL|LOAD\s+CSV|DROP)\b",
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


def _ilike_contains(query: str) -> str:
    """A contains-pattern in which % and _ are literal. The escape character is backslash."""
    escaped = (
        query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _keyword_hit(row, query: str) -> dict:
    """One keyword-fallback hit. ranked is false so a client cannot read the 0.5 as a rerank score."""
    meta = row["metadata"] if isinstance(row["metadata"], dict) else {}
    rtype = doc_record_type(meta)
    pg_id = row["id"]
    return {
        "tier": "fact",
        "pg_id": pg_id,
        "record_type": rtype,
        "ref": make_ref(rtype, pg_id),
        "content": row["content"],
        "ranked": False,
        "score": 0.0,
        "score_normalized": 0.5,
        "matched_entities": _matched_entities(query, meta),
        "metadata": meta,
        "graph_context": [],
    }


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


def _validate_visibility_and_scope(body: dict) -> tuple[str, str] | web.Response:
    """Validate visibility and scope on save and retrospective ingress (S4 / ADV-4 / ADV-5).

    Refuses visibility not in ('global', 'scope', 'private').
    When visibility == 'scope', scope must be a non-empty string (stripped).
    """
    if "visibility" in body:
        visibility = body["visibility"]
        if visibility not in ("global", "scope", "private"):
            return web.json_response(
                {
                    "status": "error",
                    "message": "visibility must be one of 'global', 'scope', 'private'",
                },
                status=400,
            )
    else:
        visibility = "global"

    if visibility == "scope":
        raw_scope = body.get("scope")
        if not isinstance(raw_scope, str) or not raw_scope.strip():
            return web.json_response(
                {
                    "status": "error",
                    "message": "scope is required and must be a non-empty string when visibility is 'scope'",
                },
                status=400,
            )
        scope = raw_scope.strip()
    else:
        scope = body.get("scope", "global")

    return visibility, scope


def _axis_filter_predicate(start: int, project: "str | list[str] | None",
                            domains: list[str] | None,
                            since: datetime | None) -> tuple[str, list]:
    """AND-predicate for search candidates: project ANY, domains ?| on metadata.domains, since created_at (decision:1214). Caller already expanded spellings and capped the domain list.

    `start` is the next free asyncpg positional index (`$N`).
    """
    clauses: list[str] = []
    params: list = []
    idx = start
    projects = [project] if isinstance(project, str) else list(project or [])
    projects = [p for p in projects if isinstance(p, str) and p]
    if projects:
        clauses.append(f"metadata->>'project' = ANY(${idx}::text[])")
        params.append(projects)
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


def _with_filters_resolved(body: dict, filters_resolved) -> dict:
    """Attach the axis-filter account to a search response — or leave it alone.

    ONE function for all three of `handle_search`'s exits (reranked, empty, and
    the keyword fallback), because the account has to be on all of them or it is
    worse than absent: an empty result is exactly the answer whose reader most
    needs to know which spellings were searched, and the keyword fallback is
    exactly the path a reader is least likely to have tested.

    ⚠ ADDITIVE, AND ABSENT RATHER THAN NULL WHEN NO FILTER WAS SUPPLIED. An
    unfiltered search's body is byte-for-byte what it was before this key
    existed, so `api_version` does not move: a client that knows nothing about
    the key sees nothing new, and one that looks for it can tell "no filter" from
    "a filter that resolved to nothing" without a second field.
    """
    if filters_resolved:
        body["filters_resolved"] = filters_resolved
    return body


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


def render_rem_by_model(rows) -> list[dict]:
    """Pure row→dict rendering for the REM latency-by-model rollup.

    ``rows`` is a sequence of mapping-like objects (asyncpg Record or dict)
    carrying: model, n, n_service, max_batch, svc_p50, svc_p95, con_p50,
    con_p95, wall_p50, wall_p95, backend.

    Keeps the legacy keys (model, n, max_batch_size, service_ms, contention_ms)
    exactly as before — server-timed rows are unchanged — and adds flat keys
    so a wall-only row (no llama.cpp ``timings`` block, e.g. an OpenAI-compatible
    external backend) still renders instead of vanishing from ``by_model``:
      wall_ms       = caller-observed p50/p95, present for every backend.
      n_service     = how many of this model's rows carried server timings
                      (out of the legacy ``n`` = count(*), unchanged for the
                      monitor contract — n now counts wall rows, not service
                      samples, so it can exceed n_service).
      backend       = the modal backend string for this model, or None.
      timing_source = "server" when n_service == n (every row server-timed),
                      "mixed" when 0 < n_service < n (some rows external),
                      "wall" when n_service == 0 (no row server-timed).
    Never invents a number: a None percentile stays None.
    """
    def _r(v):
        return round(float(v), 1) if v is not None else None

    out = []
    for r in rows:
        n = r["n"]
        n_service = int(r["n_service"] or 0)
        if n_service == 0:
            timing_source = "wall"
        elif n_service == n:
            timing_source = "server"
        else:
            timing_source = "mixed"
        out.append({
            "model": r["model"],
            "n": n,
            "max_batch_size": r["max_batch"],
            "service_ms":    {"p50": _r(r["svc_p50"]), "p95": _r(r["svc_p95"])},
            "contention_ms": {"p50": _r(r["con_p50"]), "p95": _r(r["con_p95"])},
            "wall_ms":       {"p50": _r(r["wall_p50"]), "p95": _r(r["wall_p95"])},
            "n_service": n_service,
            "backend": r["backend"],
            "timing_source": timing_source,
        })
    return out


# A module attribute so a test can patch coordinator._monotonic instead of counting mocked sleeps.
_monotonic = time.monotonic


async def _connect_with_startup_wait(factory, deadline: float):
    """Call `factory()` (a zero-arg async callable), retrying while Postgres
    is still starting up (fact:1609), bounded by a WALL-CLOCK `deadline` —
    an absolute `_monotonic()` reading, not an accumulated-sleep count.

    `deadline` is a parameter, not read from a module constant here, so that
    `start()` can compute it ONCE and pass the SAME value to both call sites
    (C3/C4, merger fix round): a Postgres that never comes up must not let
    the pgvector probe spend the whole `PG_STARTUP_WAIT_S` budget and then
    hand `create_pool` a fresh, separate budget of its own — that would leave
    `hnsw_iterative_scan` silently, permanently disabled (the probe "gave up"
    into its own except-Exception fallback) while the pool goes on to retry
    for another full window and succeed. With one shared deadline, if the
    probe alone exhausts it, `create_pool`'s first attempt already sees an
    expired deadline and — if Postgres is genuinely still down — raises
    immediately with no further retry, propagating out of `start()` instead
    of leaving a permanently-degraded process running.

    Retries ONLY on `OSError` (covers `ConnectionRefusedError`, and — since
    `TimeoutError`/`asyncio.TimeoutError` has been an `OSError` subclass
    since Python 3.11 — a connect that times out, which IS "not ready", and
    is now safely bounded by wall clock rather than an open-ended retry) or
    `asyncpg.exceptions.CannotConnectNowError` ("the database system is
    starting up"). Any other exception — including
    `asyncpg.exceptions.TooManyConnectionsError`, which means the server IS
    up but has no room right now, not a startup race — is not retried and
    propagates on the first attempt.

    C2 (Optional, adopted, merger fix round): each attempt is itself wrapped
    in `asyncio.wait_for(..., timeout=max(0.0, deadline - _monotonic()))` so
    the deadline is LITERALLY true even for a single hanging attempt (a TCP
    connect that never completes, not merely a fast fail-then-retry) — a
    slow attempt can no longer run past `deadline` on its own. The resulting
    `asyncio.TimeoutError` lands in the same `except` clause below (still an
    `OSError` subclass), so the give-up check fires exactly as it would for
    any other retryable failure.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncio.wait_for(
                factory(), timeout=max(0.0, deadline - _monotonic()))
        except (OSError, asyncpg.exceptions.CannotConnectNowError) as exc:
            now = _monotonic()
            if now >= deadline:
                log.warning(
                    "Postgres still not accepting connections after %d "
                    "attempt(s) — giving up: %s", attempt, exc)
                raise
            sleep_s = min(PG_STARTUP_RETRY_S, deadline - now)
            log.warning(
                "Postgres not ready yet (attempt %d) — retrying in %.1fs: %s",
                attempt, sleep_s, exc)
            await asyncio.sleep(sleep_s)


def _outbox_public_view(census: dict) -> dict:
    """The six census-derived keys of `_outbox_telemetry`'s payload, in the
    SAME order and by the SAME fold — `pending` is census pending + in_progress
    (v0.9.92). One owner of this shape: `_outbox_telemetry` splices it back
    in with the four latency-derived keys, and `handle_admin_outbox` serves it
    directly — dereference the rest rather than writing a second copy
    (decision:1032)."""
    return {
        "pending": census["pending"] + census["in_progress"],
        "applied": census["applied"],
        "failed": census["failed"],
        "rem_reviewed": census["rem_reviewed"],
        "oldest_failed_age_s": census["oldest_failed_age_s"],
        "oldest_pending_age_s": census["oldest_pending_age_s"],
    }


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
        # decision:1584/fact:1583: whether iterative_scan applies. Probed in start() before the pool exists; None until that probe runs.
        self.pgvector_version: str | None = None
        self.hnsw_iterative_scan: bool = False

        # Work-path instruments go through telemetry_instruments (never await). Encoder per-call latency is observed here, not the 600s probe.
        self._embed_ring = LatencyRing(ENCODER_LATENCY_WINDOW)
        self._rerank_ring = LatencyRing(ENCODER_LATENCY_WINDOW)
        # How long _acquire blocked. This climbs before saturation becomes a 503.
        self._pool_wait_ring = LatencyRing(POOL_WAIT_WINDOW)
        # cypher_rejected is a query the caller wrote wrong; tx_failures is ours. Together, a typo would read as an outage.
        self._neo4j_ring = LatencyRing(NEO4J_LATENCY_WINDOW)
        self._cypher_rejected_total = 0
        self._neo4j_tx_failures_total = 0
        self._embed_window_overruns_total = 0
        self._embed_window_overruns_last_ts: str | None = None
        # Outbox apply latency/drain rate are SQL over created_at/applied_at, not an in-memory ring (decision:1032). Registry census keeps last-good + fail counter so a dead query is not silent.
        self._registry_census_failures = 0
        self._registry_census_last_error: str | None = None
        self._registry_census_last_good: dict | None = None
        self._registry_census_as_of: str | None = None
        self._registry_census_ok: bool | None = None
        # These gates shipped uncounted, so a refusal was visible only to the caller who got it. telemetry_contract.py says which codes each key aggregates.
        self._registry_refusals = Counter((
            "entity_reserved", "entity_confusable", "entity_unknown",
            "axis_conflict", "entities_not_allowed_on_judgement",
            "new_project_refused", "new_domain_refused",
        ))
        # The reranker falls back to vector order and still answers, so a failure is silent unless failures rise against a flat success count.
        self._rerank_successes = 0
        self._rerank_failures = 0
        # Stamped with _rerank_failures, never taken from the log, so the pair cannot disagree. None until the first fallback.
        self._rerank_fallback_last_ts: str | None = None
        # fact:1314: a failed axis read changes the answer while looking like an unregistered name. This counter is the only signal of that outside one request, and it resets on restart like the rerank pair.
        self._axis_registry_read_failures = 0
        self._axis_registry_read_failure_last_ts: str | None = None
        # fact:1441: cumulative chars and docs handed to the reranker, including what a fallback would have sent. Successes plus failures already count the calls.
        self._rerank_payload_chars_total = 0
        self._rerank_payload_docs_total = 0
        # Observed-max rerank payload (monotonic this process) for the capacity signal, updated with the cumulative pair (fact:1441).
        self._rerank_payload_chars_max = 0
        # Backup quiesce: dedicated connection holding the EXCLUSIVE advisory lock
        # (None = not held), plus the TTL auto-resume task.
        self._quiesce_conn: Any = None
        self._quiesce_timer: asyncio.Task | None = None
        # Cached consolidation snapshot so /health stays database-free; stalled is not asserted on no data. The telemetry lock is single-flight; see _telemetry_cached.
        self._telemetry_cache: dict = {"snap": None, "ts": 0.0}
        self._telemetry_lock = asyncio.Lock()
        # Proxy-owned telemetry blocks. Importing hive_mind_proxy back would cycle; None means those sections are absent.
        self.telemetry_extras_provider = None

        # decision:374/fact:375: a never-probed dependency that reads healthy is the failure this avoids. Every state starts unknown, not ok.
        self._dependency_health: dict = {
            "postgres": {"state": "unknown", "reason": "not yet probed"},
            "neo4j": {"state": "unknown", "reason": "not yet probed"},
            "outbox": None,
            "rem": None,
            "nrem": None,
            "as_of": None,
            "fresh": False,
        }
        self._consolidation_health: dict = {"stalled": False, "last_outcome": None,
                                             "last_success_age_seconds": None,
                                             "last_success_cycle_type": None,
                                             "stalled_types": [],
                                             # decision 928: None until the first refresh. Not yet probed must not read as verified clean.
                                             "graph_invalid_nodes": None,
                                             # Not yet probed must not read as upgrade complete.
                                             "project_identity": None,
                                             "domain_identity": None,
                                             "inference_busy": "unknown",
                                             # None until the first refresh, never a fabricated ok.
                                             "gpu_probe": None, "fresh": False}
        self._consolidation_health_task: asyncio.Task | None = None
        self._alt_vector_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Logged here, after hive_mind_proxy's basicConfig, so the INFO line reaches the journal. An import-time log was dropped.
        log_encoder_endpoints()
        # Probe pgvector on a standalone connection before create_pool so pool warm-up connections already have hnsw_iterative_scan set.
        _pg_startup_deadline = _monotonic() + PG_STARTUP_WAIT_S
        try:
            _probe = await _connect_with_startup_wait(
                lambda: asyncpg.connect(PG_DSN), _pg_startup_deadline)
            try:
                _raw_version = await _probe.fetchval(
                    "SELECT extversion FROM pg_extension WHERE extname='vector'"
                )
            finally:
                await _probe.close()
        except Exception:
            log.warning("pgvector version probe failed — treating as unknown "
                        "(hnsw.iterative_scan stays disabled)", exc_info=True)
            _raw_version = None
        self.pgvector_version = _raw_version
        _parsed = _parse_pgvector_version(_raw_version)
        self.hnsw_iterative_scan = (
            _parsed is not None and _parsed >= PGVECTOR_ITERATIVE_SCAN_MIN)
        log.info("pgvector extension version %s — hnsw.iterative_scan %s",
                 _raw_version or "unknown",
                 "enabled" if self.hnsw_iterative_scan else "disabled")
        if not self.hnsw_iterative_scan:
            log.warning(
                "pgvector %s is below the 0.8 floor for hnsw.iterative_scan — "
                "a selective axis filter (--project/--domain) can return EMPTY "
                "results at scale once HNSW hands over its candidate set before "
                "the post-filter narrows it (decision:1584); upgrade to "
                "pgvector >= 0.8 to fix", _raw_version or "unknown")

        self._pool = await _connect_with_startup_wait(lambda: asyncpg.create_pool(
            PG_DSN, min_size=POOL_MIN, max_size=POOL_MAX,
            init=self._init_connection,
        ), _pg_startup_deadline)
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
        # No startup recovery: pending work is embedding IS NULL, the state the row was committed in, so a restart has nothing to put back.
        self._alt_vector_task = asyncio.create_task(
            self._alternative_vector_worker(), name="alternative-vector-worker")
        log.info("coordinator ready (pool %d–%d, outbox + alternative-vector workers running)",
                 POOL_MIN, POOL_MAX)
        if _AGENT_TOKENS:
            log.info(
                "coordinator auth enabled — %d agent(s): %s",
                len(_AGENT_TOKENS), ", ".join(sorted(_AGENT_TOKENS.values())),
            )
            # Log the role actually applied. A roster identity is confined even with no AGENT_ROLES entry.
            _applied = {n: effective_role(n, _AGENT_ROLES.get(n))
                        for n in set(_AGENT_TOKENS.values()) | set(_AGENT_ROLES)}
            _confined = {n: r for n, r in _applied.items() if r != "full"}
            if _confined:
                log.info(
                    "coordinator applied roles: %s",
                    ", ".join(f"{n}={r}" for n, r in sorted(_confined.items())),
                )
            _unwritten = [n for n in read_only_agents()
                          if n in set(_AGENT_TOKENS.values())
                          and _AGENT_ROLES.get(n) != "read"]
            if _unwritten:
                # Enforced anyway — but the .env disagrees with reality, and an
                # operator reading that file would draw the wrong conclusion.
                log.warning(
                    "coordinator: %s registered but NOT declared read-only in "
                    "AGENT_ROLES — confined by the roster regardless; re-run "
                    "bootstrap_tokens.sh --add to make the .env state the truth",
                    ", ".join(sorted(_unwritten)),
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

    async def _init_connection(self, conn: asyncpg.Connection) -> None:
        """Register JSONB codec so columns decode to Python dicts, not raw
        strings — then, when this coordinator's pgvector probe (start())
        found the extension at >= 0.8, set `hnsw.iterative_scan = relaxed_
        order` for the session so a selective axis filter's HNSW candidate
        handoff keeps searching instead of handing over an empty post-filter
        result (decision:1584/fact:1583).

        A bound method (not @staticmethod, as this used to be) precisely so
        it can read `self.hnsw_iterative_scan` — asyncpg calls this once per
        pooled connection, on creation, and self.hnsw_iterative_scan is fixed
        before the pool exists (see start()), so every connection this ever
        runs for — warm-up or later growth — sees the same answer.

        The SET is wrapped separately from the codec registration: a session
        GUC that fails to apply (an unexpected server error, a build without
        the setting) is logged once and must not fail the whole connection —
        the codec above is required for correct decoding everywhere, this is
        a performance/correctness improvement for one query shape.
        """
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
            format="text",
        )
        if self.hnsw_iterative_scan:
            try:
                await conn.execute("SET hnsw.iterative_scan = relaxed_order")
            except Exception:
                log.warning("failed to SET hnsw.iterative_scan on a pooled "
                            "connection — that connection keeps the default "
                            "(strict) HNSW scan for its lifetime", exc_info=True)

    def _acquire(self):
        """Acquire a pooled connection, bounded by POOL_ACQUIRE_TIMEOUT.

        asyncpg raises ``asyncio.TimeoutError`` when the pool stays saturated
        past the timeout. Request handlers let it propagate to auth_middleware,
        which maps it to 503 + Retry-After — the gateway sheds load instead of
        blocking a caller on the pool forever. Background tasks (outbox worker)
        catch it in their own loop and retry on the next cycle.

        v0.9.74: the wait is TIMED (``postgres.pool_wait_p95_ms``). The wrapper
        below delegates ``__aenter__``/``__aexit__`` straight through to
        asyncpg's own acquire context, so the timeout, the exception type, and
        the release semantics are byte-for-byte what they were — the ONLY thing
        added is a monotonic clock read on either side of the enter, and the
        recording itself cannot raise. A failed acquire is counted as an error,
        never timed into the window: it lands on POOL_ACQUIRE_TIMEOUT by
        definition and would tell you about the ceiling, not the pool.
        """
        return _TimedAcquire(self._pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT),
                             self._pool_wait_ring)

    async def _lock_for(self, entity: str) -> asyncio.Lock:
        return await self._locks.get(entity)

    async def _embed(self, text: str, client: httpx.AsyncClient) -> list[float]:
        """Embed text directly at EMBED_URL (env-derived) with exponential-backoff retry."""
        if len(text) > EMBED_MAX_CHARS:
            log.warning("embed input %d chars > %d — truncating to fit BGE-M3 8192-ctx "
                        "(full text kept in Tier 1)", len(text), EMBED_MAX_CHARS)
            text = text[:EMBED_MAX_CHARS]
        # Timeout follows the clamped length. A constant sized for a short fact under-provisions a long decision, and the 30s client default missed this clamp.
        ceiling = embed_ceiling(len(text))
        reserved_snap = int((EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * EMBED_CHARS_PER_TOKEN)
        for attempt in range(1, EMBED_RETRIES + 1):
            # Time one attempt. The retry loop's sleeps are the retry policy, not the encoder.
            _t0 = time.monotonic()
            try:
                r = await client.post(EMBED_URL, json={"input": text, "model": "bge-m3"},
                                      timeout=ceiling)
                if getattr(r, "status_code", None) == 400:
                    from encoder_window import (
                        classify_overflow,
                        record_embed_window_overrun,
                        OVERFLOW_TOKEN_SLACK,
                    )
                    resp_text = getattr(r, "text", "")
                    classification = classify_overflow(400, resp_text, EMBED_MAX_CONTEXT_TOKENS)
                    if classification.kind == "mismatch":
                        raise RuntimeError(
                            f"Embedding context mismatch: server advertised {classification.advertised} tokens, "
                            f"but framework requires EMBED_MAX_CONTEXT_TOKENS={EMBED_MAX_CONTEXT_TOKENS}. "
                            f"Start the encoder with --max-model-len {EMBED_MAX_CONTEXT_TOKENS} or "
                            f"-c {EMBED_MAX_CONTEXT_TOKENS} (or unset/lower oversize EMBED_MAX_CHARS)."
                        )
                    elif classification.kind == "overrun":
                        record_embed_window_overrun()
                        safe(lambda: setattr(self, "_embed_window_overruns_total", getattr(self, "_embed_window_overruns_total", 0) + 1))
                        safe(lambda: setattr(self, "_embed_window_overruns_last_ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                        log.error(
                            "embed input %d chars caused %d-token overflow on %d-token model — "
                            "snapping to reserved clamp (reserved=%d, EMBED_MAX_CHARS=%d)",
                            len(text), classification.requested, classification.advertised, reserved_snap, EMBED_MAX_CHARS,
                        )
                        new_len = min(reserved_snap, len(text) - 1)
                        if attempt == EMBED_RETRIES or new_len < 1:
                            raise RuntimeError(
                                f"Embedding context overrun after {attempt} attempts: server reported {classification.requested} "
                                f"tokens on {classification.advertised}-token window (last input {len(text)} chars). "
                                f"Check for leftover or oversize EMBED_MAX_CHARS (currently {EMBED_MAX_CHARS}) in .env; "
                                f"unset or lower to {reserved_snap}."
                            )
                        text = text[:new_len]
                        ceiling = embed_ceiling(len(text))
                        continue
                    elif (
                        classification.advertised is not None
                        and classification.requested is not None
                        and classification.advertised >= EMBED_MAX_CONTEXT_TOKENS
                    ):
                        raise RuntimeError(
                            f"Embedding input requested {classification.requested} tokens on "
                            f"{classification.advertised}-token window (exceeds slack {OVERFLOW_TOKEN_SLACK}). "
                            f"Lower EMBED_CHARS_PER_TOKEN (currently {EMBED_CHARS_PER_TOKEN}) or "
                            f"EMBED_MAX_CHARS (currently {EMBED_MAX_CHARS}) in .env."
                        )
                r.raise_for_status()
                vec = r.json()["data"][0]["embedding"]
                # safe() so a stubbed owner cannot AttributeError the embed path.
                safe(lambda: self._embed_ring.record(
                    (time.monotonic() - _t0) * 1000.0, payload_chars=len(text)))
                return vec
            except Exception as exc:
                if isinstance(exc, RuntimeError) and (
                    "Embedding context mismatch:" in str(exc)
                    or "exceeds slack" in str(exc)
                    or "Embedding context overrun" in str(exc)
                ):
                    raise
                safe(lambda: self._embed_ring.record_error())
                if attempt == EMBED_RETRIES:
                    # The encoder URL may carry userinfo, and this text is the client-visible 503, so scrub it.
                    raise RuntimeError(
                        f"Embedding failed after {EMBED_RETRIES} attempts at the "
                        f"embedder {scrub_url_credentials(EMBED_URL)} — is it "
                        f"running and is EMBEDDER_URL right? "
                        f"({scrub_url_credentials(str(exc))})"
                    ) from exc
                wait = EMBED_BACKOFF * attempt
                log.warning(
                    "embed attempt %d/%d failed (%s) — retry in %.1f s",
                    attempt, EMBED_RETRIES, scrub_url_credentials(str(exc)), wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"Embedding context overrun: failed after {EMBED_RETRIES} attempts. "
            f"Check for leftover or oversize EMBED_MAX_CHARS (currently {EMBED_MAX_CHARS}) in .env; "
            f"unset or lower to {reserved_snap}."
        )


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
        total_chars = sum(len(t) for t in clamped)
        ceiling = embed_ceiling(total_chars)
        reserved_snap = int((EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * EMBED_CHARS_PER_TOKEN)
        for attempt in range(1, EMBED_RETRIES + 1):
            _t0 = time.monotonic()
            try:
                r = await client.post(
                    EMBED_URL, json={"input": clamped, "model": "bge-m3"},
                    timeout=ceiling,
                )
                if getattr(r, "status_code", None) == 400:
                    from encoder_window import (
                        classify_overflow,
                        record_embed_window_overrun,
                        OVERFLOW_TOKEN_SLACK,
                    )
                    resp_text = getattr(r, "text", "")
                    classification = classify_overflow(400, resp_text, EMBED_MAX_CONTEXT_TOKENS)
                    if classification.kind == "mismatch":
                        raise RuntimeError(
                            f"Embedding context mismatch: server advertised {classification.advertised} tokens, "
                            f"but framework requires EMBED_MAX_CONTEXT_TOKENS={EMBED_MAX_CONTEXT_TOKENS}. "
                            f"Start the encoder with --max-model-len {EMBED_MAX_CONTEXT_TOKENS} or "
                            f"-c {EMBED_MAX_CONTEXT_TOKENS} (or unset/lower oversize EMBED_MAX_CHARS)."
                        )
                    elif classification.kind == "overrun":
                        record_embed_window_overrun()
                        safe(lambda: setattr(self, "_embed_window_overruns_total", getattr(self, "_embed_window_overruns_total", 0) + 1))
                        safe(lambda: setattr(self, "_embed_window_overruns_last_ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                        log.error(
                            "batch embed caused %d-token overflow on %d-token model — "
                            "snapping items to reserved clamp (reserved=%d, EMBED_MAX_CHARS=%d)",
                            classification.requested, classification.advertised, reserved_snap, EMBED_MAX_CHARS,
                        )
                        max_len = max(len(t) for t in clamped)
                        if attempt == EMBED_RETRIES or max_len < 1:
                            raise RuntimeError(
                                f"Batch embedding context overrun after {attempt} attempts: server reported {classification.requested} "
                                f"tokens on {classification.advertised}-token window. "
                                f"Check for leftover or oversize EMBED_MAX_CHARS (currently {EMBED_MAX_CHARS}) in .env; "
                                f"unset or lower to {reserved_snap}."
                            )
                        if max_len >= reserved_snap:
                            clamped = [
                                t[:min(reserved_snap, len(t) - 1)] if len(t) >= reserved_snap else t
                                for t in clamped
                            ]
                        else:
                            clamped = [
                                t[:len(t) - 1] if len(t) == max_len else t
                                for t in clamped
                            ]
                        total_chars = sum(len(t) for t in clamped)
                        ceiling = embed_ceiling(total_chars)
                        continue
                    elif (
                        classification.advertised is not None
                        and classification.requested is not None
                        and classification.advertised >= EMBED_MAX_CONTEXT_TOKENS
                    ):
                        raise RuntimeError(
                            f"Batch embedding requested {classification.requested} tokens on "
                            f"{classification.advertised}-token window (exceeds slack {OVERFLOW_TOKEN_SLACK}). "
                            f"Lower EMBED_CHARS_PER_TOKEN (currently {EMBED_CHARS_PER_TOKEN}) or "
                            f"EMBED_MAX_CHARS (currently {EMBED_MAX_CHARS}) in .env."
                        )
                r.raise_for_status()
                data = r.json()["data"]
                if len(data) != len(clamped):
                    raise RuntimeError(
                        f"embedder returned {len(data)} vectors for "
                        f"{len(clamped)} inputs"
                    )
                ordered = sorted(data, key=lambda d: d.get("index", 0))
                # A batch is ONE call and is recorded as one — its payload is
                # the whole batch, which is what the ceiling was sized on.
                safe(lambda: self._embed_ring.record(
                    (time.monotonic() - _t0) * 1000.0, payload_chars=total_chars))
                return [d["embedding"] for d in ordered]
            except Exception as exc:
                if isinstance(exc, RuntimeError) and (
                    "Embedding context mismatch:" in str(exc)
                    or "exceeds slack" in str(exc)
                    or "Batch embedding context overrun" in str(exc)
                ):
                    raise
                safe(lambda: self._embed_ring.record_error())
                if attempt == EMBED_RETRIES:
                    raise RuntimeError(
                        f"Batch embedding failed after {EMBED_RETRIES} attempts "
                        f"({len(clamped)} inputs) at the embedder "
                        f"{scrub_url_credentials(EMBED_URL)}: "
                        f"{scrub_url_credentials(str(exc))}"
                    ) from exc
                wait = EMBED_BACKOFF * attempt
                log.warning(
                    "batch embed attempt %d/%d failed (%s) — retry in %.1f s",
                    attempt, EMBED_RETRIES, scrub_url_credentials(str(exc)), wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"Batch embedding context overrun: failed after {EMBED_RETRIES} attempts. "
            f"Check for leftover or oversize EMBED_MAX_CHARS (currently {EMBED_MAX_CHARS}) in .env; "
            f"unset or lower to {reserved_snap}."
        )


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
        # Claim rows as in_progress before releasing the lock so a peer SKIP LOCKs them. start() resets rows left in_progress by a crash.
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
        async with self._acquire() as conn:
            for row in rows:
                # asyncpg returns JSONB as a string in some configurations;
                # parse defensively rather than relying on codec registration.
                params = row["cypher_params"]
                if isinstance(params, str):
                    params = json.loads(params)
                await self._apply_outbox_row(row["id"], row["pg_id"], params, row["retries"], conn=conn)

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
        self, outbox_id: int, pg_id: int, params: dict, retries: int, conn=None
    ) -> None:
        # Time this apply too. The graph route alone would hide the write path that blocks the pipeline, and its failures would hide an apply outage.
        _t0 = time.monotonic()
        try:
            row_type = params.get("type")
            if row_type not in (None, "", "fact"):
                if row_type == "decision":
                    await self._apply_decision_outbox_row(outbox_id, pg_id, params)
                    return
                if row_type == "retrospective":
                    await self._apply_retrospective_outbox_row(outbox_id, pg_id, params)
                    return
                if row_type == "supersede":
                    await self._apply_supersede_outbox_row(outbox_id, params)
                    return
                if row_type == "project_of":
                    await self._apply_project_of_outbox_row(outbox_id, pg_id, params)
                    return
                if row_type == "domain_of":
                    await self._apply_domain_of_outbox_row(outbox_id, pg_id, params)
                    return
                log.warning(
                    "outbox: unknown type %r pg_id=%d (outbox_id=%d) — marking failed; "
                    "a type error will never succeed",
                    row_type, pg_id, outbox_id,
                )
                await self._fail_outbox_immediately(outbox_id, conn)
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
                merge_result = await session.run(
                    f"OPTIONAL MATCH (existing:{self._SPINE}) WHERE existing.pg_id = $pg_id"
                    f" WITH existing WHERE existing IS NULL OR existing:{ONT.fact}"
                    f" MERGE (f:{ONT.fact} {{pg_id: $pg_id}})"
                    f" SET f.content = $content, f.source = $source,"
                    f"     f.fact_kind = $fact_kind"
                    + (" SET f.source_ref = $source_ref" if source_ref else "")
                    + f" WITH f"
                    # Fact WAS_ATTRIBUTED_TO agent, ACTED_ON_BEHALF_OF principal, PROJECT_OF folder — derived, written only when present (decision:915).
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
                    # Same-round-trip DOMAIN_OF then Domain-[:PROJECT_OF]->Project (re-MERGE the project; FOREACH does not keep `p`).
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
                if not await self._outbox_write_landed(merge_result):
                    log.warning(
                        "outbox: pg_id=%d already exists under a different spine label — "
                        "marking failed (will never succeed)",
                        pg_id,
                    )
                    await self._fail_outbox_immediately(outbox_id, conn)
                    return
                if clean_entities:
                    async with self._acquire() as c:
                        await c.executemany(
                            "INSERT INTO entity_registry (name, registered_by) VALUES ($1, 'fact_ingress') ON CONFLICT (name) DO NOTHING",
                            [(e,) for e in clean_entities],
                        )
                # decision 381/384: flag the old Fact and link SUPERSEDES on this row. MATCH-only so a missing pre-coordinator node is a no-op, not a phantom.
                supersedes = params.get("supersedes")
                if supersedes is not None:
                    # MERGE the old node: this row can apply before the old fact's own row. The later apply sets content and does not clear superseded.
                    await session.run(
                        f"MERGE (old:{ONT.fact} {{pg_id: $old_id}})"
                        f" SET old.superseded = true"
                        f" WITH old"
                        f" MATCH (new:{ONT.fact} {{pg_id: $new_id}})"
                        f" MERGE (new)-[:{ONT.supersedes}]->(old)",
                        old_id=supersedes, new_id=pg_id,
                    )
            if conn is None:
                async with self._acquire() as c:
                    await c.execute(
                        "UPDATE neo4j_outbox SET status='applied', applied_at=now() WHERE id=$1",
                        outbox_id,
                    )
            else:
                await conn.execute(
                    "UPDATE neo4j_outbox SET status='applied', applied_at=now() WHERE id=$1",
                    outbox_id,
                )
            safe(lambda: self._neo4j_ring.record(
                (time.monotonic() - _t0) * 1000.0))
            log.debug("outbox: applied pg_id=%d (outbox_id=%d)", pg_id, outbox_id)
        except Exception as exc:
            if isinstance(exc, (asyncpg.PostgresError, asyncpg.InterfaceError, asyncio.TimeoutError, ProjectIdentityUnavailable, DomainIdentityUnavailable)):
                log.warning(
                    "outbox: postgres error pg_id=%d attempt %d/%d: %s",
                    pg_id, retries + 1, OUTBOX_MAX_RETRIES, exc,
                )
            else:
                # Counted as ours. cypher_rejected is a query the caller wrote wrong, and there is no caller on this path.
                safe(lambda: setattr(self, "_neo4j_tx_failures_total",
                                     self._neo4j_tx_failures_total + 1))
                log.warning(
                    "outbox: neo4j write failed pg_id=%d attempt %d/%d: %s",
                    pg_id, retries + 1, OUTBOX_MAX_RETRIES, exc,
                )
            try:
                async def _record_retry(c):
                    if retries + 1 >= OUTBOX_MAX_RETRIES:
                        # Atomic: bump retries AND flip status in one statement
                        await c.execute(
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
                        await c.execute(
                            "UPDATE neo4j_outbox"
                            " SET retries=retries+1, status='pending',"
                            "     next_attempt_at = now() + make_interval(secs => $2)"
                            " WHERE id=$1",
                            outbox_id, delay,
                        )

                if conn is not None:
                    await _record_retry(conn)
                else:
                    async with self._acquire() as c:
                        await _record_retry(c)
            except Exception as update_exc:
                log.error(
                    "outbox: failed to update retry status for outbox_id=%d (pg_id=%d): %s",
                    outbox_id, pg_id, update_exc,
                )

    async def _fail_outbox_immediately(self, outbox_id: int, conn=None) -> None:
        """Mark failed without bumping retries — a type error will never succeed."""
        if conn is None:
            async with self._acquire() as c:
                await c.execute(
                    "UPDATE neo4j_outbox SET status='failed' WHERE id=$1",
                    outbox_id,
                )
        else:
            await conn.execute(
                "UPDATE neo4j_outbox SET status='failed' WHERE id=$1",
                outbox_id,
            )

    @staticmethod
    async def _outbox_write_landed(result) -> bool:
        """False when the spine-kind WHERE dropped the MERGE (0 nodes, 0 properties)."""
        consume = getattr(result, "consume", None)
        if consume is None:
            return True
        summary = consume()
        if hasattr(summary, "__await__"):
            summary = await summary
        counters = getattr(summary, "counters", None)
        if counters is None:
            return True
        created = getattr(counters, "nodes_created", None)
        props = getattr(counters, "properties_set", None)
        return not (created == 0 and props == 0)


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
                # A changed alternative is different text, so the old vector goes back to pending.
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
                # A dead sweep is not an idle one: pending rows remain, and the error stays on the record.
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
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
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
                        # A re-save can change the text mid-batch. The update matches the text that was read so the old vector cannot land on new text.
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
        """Write grounding role edges (decision 582) from a Decision or Retrospective onto the target's real label (Fact, Decision, or Retrospective).
        apoc supplies that label and the relation; every edge records asserted_by. The decision and retrospective projections share this writer."""
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

        Creates: Decision, Human (decided_by), Project, AIAgent(s) (assisted_by).
        FOREACH handles empty lists so the query is safe regardless of whether
        assisted_by is set. All writes in one session — atomic on transient
        failures (MERGE is idempotent).

        A decision CARRIES NO ENTITIES — only facts do (`decision:1664`). The
        caller-supplied `entities` metadata stays in Postgres (Tier 1 pristine)
        and is never projected into the graph: a decision's topics are whatever
        its evidence is about, reached by walking its grounding path to the
        facts, never a second free-text vocabulary minted alongside it.
        """
        decision = params.get("decision", {})
        grounded = params.get("grounded") or []
        grounded_in_flat = params.get("grounded_in", [])
        project_id = await self._project_identity(decision.get("project"))
        # A decision asserts its own sections, the same way it asserts its project. It inherits what it is about, not where it belongs.
        domain_ids = await self._domain_identities(
            pg_id, project_id, params.get("domains"))
        async with self._neo4j.session() as session:
            merge_result = await session.run(
                f"OPTIONAL MATCH (existing:{self._SPINE}) WHERE existing.pg_id = $pg_id"
                f" WITH existing WHERE existing IS NULL OR existing:{ONT.decision}"
                f" MERGE (d:{ONT.decision} {{pg_id: $pg_id}})"
                f"  SET d.title       = $title,"
                f"      d.rationale   = $rationale,"
                f"      d.date        = $date,"
                f"      d.source      = $source"
                # confidence/alternatives stay on the record payload, never minted as :Entity (fact:551).
                f" WITH d"
                f" MERGE (h:{ONT.human} {{name: $decided_by}})"
                f" MERGE (d)-[:{ONT.was_attributed_to}]->(h)"
                f" WITH d"
                f" {project_merge_cypher(project_id)}"
                f" MERGE (d)-[:{ONT.project_of}]->(p)"
                # Bind the Domain as dm because d is the Decision. Re-merge the project inside FOREACH: a variable bound outside it cannot be a MERGE target.
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
            if not await self._outbox_write_landed(merge_result):
                log.warning(
                    "outbox: pg_id=%d already exists under a different spine label — "
                    "marking failed (will never succeed)",
                    pg_id,
                )
                await self._fail_outbox_immediately(outbox_id)
                return
            # decision 582: typed grounding via _write_typed_grounding. Rows queued before that, with no grounded list, keep flat GROUNDED_IN.
            if grounded:
                await self._write_typed_grounding(session, ONT.decision, pg_id, grounded)
            elif grounded_in_flat:
                # Resolve the cited pg_id's real label before linking; MERGE-as-Fact used to mint a phantom beside a Decision/Retrospective.
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
            # decision:1736: a decision's DOMAIN_OF edges are only the names it asserted. decision:1032: belonging is derived on read, so a walkable value is not written twice.
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
        intact (edge no-op). MENTIONS edges are NOT written here: a
        retrospective carries no entities — only facts do (`decision:1664`); its
        topics are reached by walking to the facts its decision grounds in.

        Legacy (no 'v'): pg_id is the TARGET DECISION's id — a HAD_OUTCOME
        self-loop carrying rating/date/notes as edge properties. Kept until the
        pre-conversion outbox rows drain. MATCH only (no MERGE) so a missing
        Decision surfaces as a no-op rather than a phantom node.
        """
        retro = params.get("retrospective", {})
        # decision 276: reversal marks the decision node so the insight gate can skip it. Insights are superseded by the re-fold, not invalidated here.
        reversal = bool(retro.get("superseded"))
        async with self._neo4j.session() as session:
            if params.get("v") == 2:
                target_pg_id = params.get("target_pg_id")
                source_ref = params.get("source_ref") or None
                merge_result = await session.run(
                    f"OPTIONAL MATCH (existing:{self._SPINE}) WHERE existing.pg_id = $pg_id"
                    f" WITH existing WHERE existing IS NULL OR existing:{ONT.retrospective}"
                    f" MERGE (r:{ONT.retrospective} {{pg_id: $pg_id}})"
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
                if not await self._outbox_write_landed(merge_result):
                    log.warning(
                        "outbox: pg_id=%d already exists under a different spine label — "
                        "marking failed (will never succeed)",
                        pg_id,
                    )
                    await self._fail_outbox_immediately(outbox_id)
                    return
                # Separate statement so a missing decision drops the edge and still keeps the Retrospective.
                await session.run(
                    f"MATCH (d:{ONT.decision} {{pg_id: $target}})"
                    f" MATCH (r:{ONT.retrospective} {{pg_id: $pg_id}})"
                    f" MERGE (d)-[:{ONT.had_outcome} {{date: $date}}]->(r)"
                    + (" SET d.superseded = true" if reversal else ""),
                    target=target_pg_id, pg_id=pg_id, date=retro.get("date", ""),
                )
                # decision 582: role edges to the evidence that measured the outcome. decision 542 made test-grounded retrospectives structural.
                await self._write_typed_grounding(
                    session, ONT.retrospective, pg_id, params.get("grounded") or []
                )
                # A retrospective writes no DOMAIN_OF; belonging is derived on read (decision:1736, fact:1671).
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

    # MERGE the existing spine node by pg_id (any label); a :Fact placeholder only if none exists yet, so a judgement is not cloned as Fact.
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
        """Replace PROJECT_OF on an existing spine node from the repair row; MATCH not MERGE, then delete the outbox row."""
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
        """Replace DOMAIN_OF edges with the asserted names; drop a legacy inherit=true row without writing (decision:1736)."""
        retired_inherit = bool(params.get("inherit"))
        written = 0
        if retired_inherit:
            # Retired mode — see the docstring. Drop the row without touching
            # the graph; it asks for a write this system no longer performs.
            async with self._acquire() as conn:
                await conn.execute("DELETE FROM neo4j_outbox WHERE id=$1", outbox_id)
            log.info(
                "outbox: domain_of pg_id=%s carries the retired 'inherit' mode — "
                "no graph write, row dropped (outbox_id=%d)", pg_id, outbox_id,
            )
            return
        project_id = await self._project_identity(params.get("project"))
        names = [n.strip() for n in (params.get("domains") or [])
                 if isinstance(n, str) and n.strip()]
        domain_ids = await self._domain_identities(
            pg_id, project_id, names)
        resolved = {d["name"] for d in domain_ids}
        # An unresolved name used to clear every DOMAIN_OF edge and delete the row. Fail the row instead; the edges stay until a later repair can write them all.
        if project_id is None or not names or any(n not in resolved for n in names):
            log.warning(
                "outbox: domain_of pg_id=%s project_id=%s names=%s resolved=%s "
                "— not applied; existing edges kept",
                pg_id, project_id, names, sorted(resolved),
            )
            await self._fail_outbox_immediately(outbox_id)
            return
        async with self._neo4j.session() as session:
            # One transaction: clear, then MERGE every requested node and edge. domains is non-empty here, so the UNWIND cannot commit a clear and then write nothing.
            result = await session.run(
                f"MATCH (n:{self._SPINE}) WHERE n.pg_id = $pg_id"
                f" OPTIONAL MATCH (n)-[stale:{ONT.domain_of}]->()"
                f" DELETE stale"
                f" WITH DISTINCT n"
                f" UNWIND $domains AS row"
                f" {domain_merge_cypher(id_param='row.id')}"
                f" SET d.name = row.name"
                f" WITH n, d"
                f" {project_merge_cypher(project_id)}"
                f" MERGE (d)-[:{ONT.project_of}]->(p)"
                f" MERGE (n)-[:{ONT.domain_of}]->(d)"
                f" RETURN n.pg_id AS matched",
                pg_id=pg_id, domains=domain_ids, project_id=project_id,
                project=(params.get("project") or "").strip(),
            )
            matched = await result.single()
            if matched is None:
                # No spine node: the statement wrote nothing. Leave the row so the next attempt can retry.
                raise RuntimeError(f"domain_of: no spine node for pg_id={pg_id}")
            written = len(domain_ids)
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM neo4j_outbox WHERE id=$1", outbox_id)
        log.info(
            "outbox: backfilled %s pg_id=%s edges=%d (outbox_id=%d, row deleted)",
            ONT.domain_of, pg_id, written, outbox_id,
        )

    async def _wait_for_outbox(self, pg_id: int) -> str:
        """Poll the dream-cycle outbox row. Returns applied, failed, or timeout.

        applied covers rem_reviewed, consolidated, and a row that is already gone.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONSISTENCY_TIMEOUT
        while loop.time() < deadline:
            async with self._acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status FROM neo4j_outbox WHERE pg_id=$1"
                    f" AND {_DREAM_CYCLE_OUTBOX_TYPE}"
                    " ORDER BY id DESC LIMIT 1",
                    pg_id,
                )
            if row is None:
                # Fast worker: applied (and NREM-deleted) before the first poll,
                # or vanished after we had seen the dream-cycle row.
                return "applied"
            status = row["status"]
            if status in _OUTBOX_GRAPH_PRESENT:
                return "applied"
            if status == "failed":
                return "failed"
            await asyncio.sleep(0.25)
        return "timeout"

    # ── POST /memory/save ─────────────────────────────────────────────────────

    @staticmethod
    def _rewrite_project(metadata: dict, supplied: str, canonical: str) -> None:
        """Move a record onto the CANONICAL project name, in every carrier.

        EVERY carrier holding the supplied spelling moves — not just the field
        the resolution was read from. Rewriting one and not the other leaves a
        record whose Postgres metadata and graph axis disagree about which
        project it belongs to. Only fields equal to the resolved spelling are
        touched, so a carrier naming a different project is never clobbered.
        """
        if metadata.get("project") == supplied:
            metadata["project"] = canonical
        blob = metadata.get("decision")
        if isinstance(blob, dict) and blob.get("project") == supplied:
            blob["project"] = canonical

    # Axis gates record an intent; `_commit_axis_registrations` inserts after every 400 and before embed so a refused save cannot leave a registry row (decision:1413).
    _PENDING_KEY = "pending_registrations"

    def _pending_registrations(self, report: dict | None) -> dict | None:
        """The intent ledger for this save, created on first use, or None when
        the caller passed no report to keep it in."""
        if report is None:
            return None
        return report.setdefault(self._PENDING_KEY,
                                 {"project": None, "domains": []})

    def _pending_project(self, report: dict | None) -> str | None:
        """The project this save is REGISTERING but has not registered yet.

        Read-only — it must never create the ledger, because the domain gate
        asks this question on every save and a `setdefault` here would put an
        empty intent record into the report of every save that has none.
        """
        return ((report or {}).get(self._PENDING_KEY) or {}).get("project")

    def _defer_project_registration(self, report: dict | None, name: str,
                                    agent_id: str, metadata: dict) -> None:
        pending = self._pending_registrations(report)
        if pending is None:
            # Missing axis-report argument raises (a 200 without a registry row would diverge graph from registry).
            raise RuntimeError(
                f"_defer_project_registration({name!r}) was called with no "
                "axis report — the caller must pass one, because that is "
                "where the registration intent lives")
        pending["project"] = name
        log.info("project registry: %r accepted as new by %s (new_project, "
                 "record type %s) — registration deferred until every gate "
                 "has passed", name, agent_id, metadata.get("type") or "fact")

    def _defer_domain_registration(self, report: dict | None, project: str,
                                   name: str, project_id: int | None,
                                   agent_id: str) -> None:
        """Record a section to register once the save is certain.

        ⚠ `project_id` IS None WHEN THE PROJECT IS ITSELF PENDING, and that is
        the whole reason this is an intent and not an id: the project row does
        not exist yet, so the id is resolved at COMMIT time, after the project
        insert, never here.
        """
        pending = self._pending_registrations(report)
        if pending is None:
            # A coding error, and it raises — see `_defer_project_registration`.
            raise RuntimeError(
                f"_defer_domain_registration({name!r}) was called with no axis "
                "report — the caller must pass one, because that is where the "
                "registration intent lives")
        pending["domains"].append(
            {"project": project, "project_id": project_id, "name": name})
        log.info("domain registry: %r accepted as a new section of project %r "
                 "by %s (new_domain) — registration deferred until every gate "
                 "has passed", name, project, agent_id)

    async def _commit_axis_registrations(self, report: dict | None,
                                         agent_id: str) -> None:
        """Write the registry rows this save's gates accepted (P4′).

        Called from `handle_save` ONLY, immediately before
        `_entity_commit_mints` — after every 400-capable validation and the
        409 axis-conflict check, before the hard-mandate embed.

        ⛔ PROJECT FIRST, THEN ITS SECTIONS, and the order is a dependency:
        `project_domains` is keyed on the project's registry id, so a section
        declared on a brand-new project can only be written once the project
        row exists. An intent that carries no `project_id` resolves one here.

        ⚠ WHAT STILL EXITS AFTER THIS POINT — stated in full, because the
        first version of this docstring claimed "no registry row is written by
        a save refused with 4xx or 503" and that was OVERSTATED (review R1).
        Three exits remain downstream of this commit, and they are here by
        CONSTRUCTION rather than by oversight:

          * the **hard-mandate embedding 503** — it needs the final content,
            so it cannot run earlier. The standing `decision:1413` residual,
            the same exposure the entity mint has carried since S-4.
          * the **in-transaction 409 `axis_conflict`** — the authoritative one,
            re-read under `FOR UPDATE`. Only a row lock stops a concurrent save
            of the same content landing between the read and the INSERT, so it
            cannot be hoisted; the cheap pre-check above it already fires
            before this commit for every non-racing case.
          * this method's **own 503**, when the project row is written and its
            id cannot be read back for the sections that follow.

        Closing any of them needs one transaction spanning the registry writes
        and the record insert, which no axis has today. What WAS fixed is the
        exit that was not by construction at all: a `new_entities` name that
        normalizes to nothing used to be refused by `_entity_commit_mints`,
        below this line, so `--new-project --new-domain` with
        `new_entities: ["!!"]` committed both rows and then 400'd. That check
        now runs in `_entity_ingress_validate`, before every write.
        """
        pending = (report or {}).pop(self._PENDING_KEY, None)
        if not pending:
            return
        project = pending.get("project")
        if project:
            await self._register_project(project, agent_id)
            log.info("project registry: %r REGISTERED by %s — every gate passed",
                     project, agent_id)
        for intent in pending.get("domains") or []:
            project_id = intent.get("project_id")
            if project_id is None:
                project_id = await self._project_identity(intent["project"])
            await self._register_domain(project_id, intent["name"], agent_id)
            log.info("domain registry: %r REGISTERED under project %r by %s — "
                     "every gate passed", intent["name"], intent["project"],
                     agent_id)

    async def _project_ingress_error(self, metadata: dict, agent_id: str,
                                     report: dict | None = None) -> dict | None:
        """The whole project-ingress rule (P4, P9). Returns the 400 body, or None
        when the save may proceed. ACCEPTS a project the caller declares new and
        records the INTENT to register it; the row itself is written by
        `_commit_axis_registrations` once every gate has passed (P4′).

        ⛔ `report` IS WHERE THAT INTENT LIVES, so a caller that ACCEPTS a
        declared-new project must pass one — calling this with `report=None`
        on that path RAISES. It is a coding error, not a degraded mode: a
        silent drop would answer 200 to a save whose project was never
        registered, and the outbox would then mint a `:Project` node the
        registry does not have. Every OTHER path (a registered name, an alias,
        a refusal) still takes `report=None` happily, which is why the
        parameter stays optional.

        `report`, when given, is filled with `project_resolved` — `{supplied,
        canonical, via}` — whenever the value stored differs from the value
        sent. It is an OUT PARAMETER rather than a second return value on
        purpose: this method's contract is "the 400 body, or None", every
        caller and every test reads it that way, and widening the return type
        to carry an advisory would make forty call sites unpack a tuple to learn
        nothing they asked for.
        """
        # Retrospectives inherit the project of the decision they judge, so this check would demand a value the caller never sends. Decisions stay in the check: an unregistered name used to become a project node.
        if metadata.get("type") == "retrospective":
            return None

        # Check project, not a domain chain. A section must not vouch for the project it belongs to.
        supplied = resolve_project(metadata)
        if not supplied:
            return await self._project_rejection("project_required", None)

        # The sentinel is a legitimate answer, not a bypass: it saves, searches
        # and enriches, and is simply never folded as a subject (P5).
        if supplied == SENTINEL:
            return None

        if await self._project_registered(supplied):
            return None

        # new_project is accepted only as a true claim of absence (after exact-registry); a retired or variant spelling is refused, never quietly resolved.
        if metadata.get("new_project") is True:
            # new_project is not proof. The same agent can misspell it, so the claim still faces the checks below.
            refusal = await self._new_project_refusal(supplied, metadata)
            if refusal is not None:
                return refusal
            # new_project is accepted here and written later in `_commit_axis_registrations` so a later 400/503 cannot leave a registry row.
            self._defer_project_registration(report, supplied, agent_id, metadata)
            return None

        # Store the canonical name. A folder that still carries the retired spelling would otherwise recreate the variant the merge removed.
        canonical = await self._resolve_project_alias(supplied)
        if canonical is not None:
            log.info("project alias: %r → %r (record stored as the canonical name)",
                     supplied, canonical)
            self._rewrite_project(metadata, supplied, canonical)
            if report is not None:
                report["project_resolved"] = {
                    "supplied": supplied, "canonical": canonical,
                    "via": VIA_ALIAS,
                }
            return None

        # Key-identical spellings (Shared_Memory vs shared-memory) are the same project; exact hits stay exact (decision:1015, fact:1047, fact:1490).
        registered, aliases, _err = await self._project_spellings(supplied)
        canonical, via = resolve_axis_value(supplied, registered, aliases)
        if canonical is not None:
            log.info("project key: %r → %r (via %s; record stored as the "
                     "canonical name)", supplied, canonical, via)
            self._rewrite_project(metadata, supplied, canonical)
            if report is not None and canonical != supplied:
                report["project_resolved"] = {
                    "supplied": supplied, "canonical": canonical, "via": via,
                }
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

        THREE checks now. The first is newer and cheaper than both, and it is the
        gateway-side twin of a rule the DATABASE started enforcing in migration
        035: a name every character of which is punctuation keys to the empty
        string, so it can be told apart from nothing, and the registry's
        BEFORE-write trigger RAISEs on it. Without this the refusal still
        happened — as a raw Postgres error surfacing to the caller as a 5xx,
        which is not a refusal an agent can act on. A gate the database enforces
        and the ingress does not is a 500 waiting to be reported as an outage.
        """
        # Before any query: a name with no axis key cannot be a near-match and has nothing to propose.
        if not axis_key(supplied):
            log.info("project registry: refused %r — normalizes to nothing",
                     supplied)
            return {
                "status": "error",
                "error": "project_unnameable",
                "message": (
                    f"project {_short(supplied)} normalizes to nothing — every "
                    "character is punctuation, whitespace or similar, so there is "
                    "no spelling left to register and it could never be told "
                    "apart from any other such name. Name the project with at "
                    "least one letter or digit."
                ),
            }
        async with self._acquire() as conn:
            rows = await conn.fetch(CONFUSABLE_SQL, supplied,
                                    CONFUSABLE_SIMILARITY, PROPOSAL_LIMIT)
            # Check the whole registry, not the trigram neighbours. A separator variant can score under the floor (testing vs Test_Ing was 0.545) and still be the same project.
            all_names = [r["name"] for r in await conn.fetch(PROJECT_NAMES_SQL)]
            # Include retired spellings. A machine still using the old folder name will send that variant, and the refusal names the canonical project, not the alias.
            alias_map = {r["alias"]: r["canonical"]
                         for r in await conn.fetch(ACTIVE_ALIASES_SQL)}
        near = [r["name"] for r in rows]

        variant = spelling_variant_of(supplied, all_names)
        aliased = None
        if variant is None:
            aliased = spelling_variant_of(supplied, list(alias_map))
            if aliased is not None:
                variant = alias_map[aliased]
        if variant is not None:
            log.info("project registry: refused %r — a spelling of %s %r",
                     supplied, "retired" if aliased else "registered", variant)
            retired = (
                f" {_short(aliased)} is a RETIRED spelling of it and is already "
                "resolved on save, so no new registration is needed."
                if aliased else ""
            )
            return {
                "status": "error",
                "error": "project_spelling_variant",
                "message": (
                    f"project {_short(supplied)} differs from the registered project "
                    f"{_short(variant)} only in separators or capitalisation, so it is a "
                    f"SPELLING of it and not a new project. Save under {_short(variant)}."
                    f"{retired} "
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
        self, metadata: dict, agent_id: str, report: dict | None = None,
    ) -> dict | None:
        """The whole domain-ingress rule. Returns the 400 body, or None when the
        save may proceed. Registers a domain the caller declares new, exactly as
        the project protocol does — that IS the acceptance.

        `report`, when given, collects `domains_resolved` — one `{supplied,
        canonical, via}` entry per value the gateway REWROTE, and nothing for
        the ones it accepted as sent. An out parameter, for the reason
        `_project_ingress_error` documents.

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

        # The sentinel is outside every project, so it has no sections. The registry cannot answer for it.
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

        # Pending new-project intent answers identity here (no registry row yet); _project_identity would otherwise 503 the first section save.
        new_project = self._pending_project(report) == project

        # Unreadable project identity is 503 registry_unavailable, not a silently unlinked save.
        project_id = None if new_project else await self._project_identity(project)

        for name in supplied:
            error = await self._domain_value_error(
                name, project, project_id, metadata, agent_id, report,
                new_project=new_project)
            if error is not None:
                return error
        return None

    async def _domain_value_error(
        self, name: str, project: str, project_id: int,
        metadata: dict, agent_id: str, report: dict | None = None,
        new_project: bool = False,
    ) -> dict | None:
        """One domain value, through the same protocol a project name faces.

        Registered → accepted. A retired spelling → rewritten to the canonical
        section and accepted. Otherwise the caller is told, with proposals, and a
        second submission declaring `new_domain` registers it — subject to the
        same two naming guards a new project faces (decision 1048), because the
        agent that sets the flag is the agent that makes the spelling error.

        `new_project` says the project itself is a PENDING registration of this
        save, so `project_id` is None and every registry step below has nothing
        to read. That case takes its own short path — see
        `_new_project_domain_error`.
        """
        if new_project:
            return await self._new_project_domain_error(
                name, project, metadata, agent_id, report)
        if await self._domain_registered(project_id, name):
            return None

        # Answer new_domain before the other steps. A retired or separator variant claimed as new is a false claim, and the caller is told rather than quietly corrected.
        if metadata.get("new_domain") is True:
            refusal = await self._new_domain_refusal(name, project, project_id, metadata)
            if refusal is not None:
                return refusal
            # Accepted here, WRITTEN by `_commit_axis_registrations` (P4′) —
            # the project axis' rule, for the same reason.
            self._defer_domain_registration(
                report, project, name, project_id, agent_id)
            return None

        canonical = await self._resolve_domain_alias(project_id, name)
        if canonical is not None:
            log.info("domain alias: %r → %r in project %r (record stored as the "
                     "canonical name)", name, canonical, project)
            self._rewrite_domain(metadata, name, canonical)
            self._note_domain_resolved(report, name, canonical, VIA_ALIAS)
            return None

        # Resolve by key inside this project. A section name is an ordinary word, so graph-quality and graph quality are the same section more often than those spellings are the same project.
        registered, aliases, _err = await self._domain_spellings(project_id, name)
        resolved, via = resolve_axis_value(name, registered, aliases)
        if resolved is not None:
            log.info("domain key: %r → %r in project %r (via %s; record stored "
                     "as the canonical name)", name, resolved, project, via)
            self._rewrite_domain(metadata, name, resolved)
            if resolved != name:
                self._note_domain_resolved(report, name, resolved, via)
            return None

        return await self._domain_rejection(name, project, project_id)

    async def _new_project_domain_error(
        self, name: str, project: str, metadata: dict, agent_id: str,
        report: dict | None,
    ) -> dict | None:
        """One domain value on a project this same save is registering (P4′).

        The project has NO SECTIONS — not "none we could read", none at all —
        so every registry step the ordinary path takes has nothing to answer
        with: no exact hit, no alias, no spelling variant, no confusable
        neighbour and no proposal to offer. What survives is the ONE guard that
        needs no registry, the all-punctuation check migration 035's trigger
        also enforces, and the ordinary unknown-domain refusal for a caller
        that did not declare the section new.

        ⛔ AND `new_domain` IS STILL REQUIRED. A brand-new project is exactly
        where an agent is most likely to invent a section name in passing, and
        accepting one silently here would make "declare a new section
        deliberately" a rule that stops applying precisely when the project is
        new. The refusal carries an empty proposal list because there is
        genuinely nothing to propose.
        """
        if metadata.get("new_domain") is not True:
            return await self._domain_rejection(name, project, None)
        unnameable = self._domain_unnameable_refusal(name, project)
        if unnameable is not None:
            return unnameable
        self._defer_domain_registration(report, project, name, None, agent_id)
        return None

    @staticmethod
    def _note_domain_resolved(report, supplied: str, canonical: str,
                              via: str) -> None:
        """Append one rewrite to the save response's `domains_resolved`.

        A LIST rather than a map, because a record may name several sections and
        the caller needs to know which of the values IT sent moved — a map keyed
        on the canonical would lose that when two supplied spellings resolve to
        one section.
        """
        if report is None:
            return
        report.setdefault("domains_resolved", []).append(
            {"supplied": supplied, "canonical": canonical, "via": via}
        )

    async def _new_domain_refusal(
        self, name: str, project: str, project_id: int, metadata: dict,
    ) -> dict | None:
        """Why this NEW-domain declaration must not register — or None.

        The project axis' two checks (decision 1048), scoped to one project's
        sections. A separator/case variant of a section this project already has
        is a SPELLING of it and no confirmation can make it distinct; a merely
        confusable name is held once and can be confirmed by naming the section
        it means to differ from.

        Plus the same unnameable check, for the same reason: migration 035's
        BEFORE-write trigger covers `project_domains` as well, so without a
        gateway-side twin an all-punctuation section name reaches the database
        and comes back as a 5xx instead of a refusal the caller can answer.
        """
        unnameable = self._domain_unnameable_refusal(name, project)
        if unnameable is not None:
            return unnameable
        async with self._acquire() as conn:
            rows = await conn.fetch(DOMAIN_CONFUSABLE_SQL, project_id, name,
                                    DOMAIN_CONFUSABLE_SIMILARITY,
                                    DOMAIN_PROPOSAL_LIMIT)
            # Every section of THIS project, not just the trigram neighbours —
            # see the identical note on the project axis above.
            all_names = [r["name"]
                         for r in await conn.fetch(DOMAIN_NAMES_SQL, project_id)]
            # A retired section spelling was already adjudicated, so a variant of it is the same mistake as a variant of a live name.
            alias_map = {r["alias"]: r["canonical"]
                         for r in await conn.fetch(DOMAIN_ALIASES_SQL, project_id)}
        near = [r["name"] for r in rows]

        variant = spelling_variant_of(name, all_names)
        if variant is None:
            aliased = spelling_variant_of(name, list(alias_map))
            if aliased is not None:
                variant = alias_map[aliased]
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
    def _domain_unnameable_refusal(name: str, project: str) -> dict | None:
        """The 400 for a section name that normalizes to nothing, or None.

        Extracted so the two callers cannot drift: the ordinary `new_domain`
        path and the new-project path, which skips every OTHER guard precisely
        because they all need a registry this project does not have yet. This
        one needs none — a name with no key is not a near-match of anything —
        and migration 035's BEFORE-write trigger RAISEs on it either way, so
        without a gateway-side twin the refusal arrives as a 5xx.
        """
        if axis_key(name):
            return None
        log.info("domain registry: refused %r in project %r — normalizes to "
                 "nothing", name, project)
        return {
            "status": "error",
            "error": "domain_unnameable",
            "message": (
                f"domain {_short(name)} normalizes to nothing — every "
                "character is punctuation, whitespace or similar, so there is "
                "no spelling left to register. Name the section with at least "
                "one letter or digit."
            ),
        }

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
        except DomainIdentityUnavailable:
            raise
        except Exception as exc:
            log.error("domain identity lookup FAILED for %r in project id %s: %s",
                      name, project_id, exc)
            raise DomainIdentityUnavailable(
                f"the domain registry could not be read for {name!r} in project id {project_id}"
            ) from exc

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

    async def _domain_proposals(self, project_id: int | None, name: str) -> list[str]:
        """Sections of THIS project near a value that missed — by name or by
        description. The description half is what lets an operator reach a
        section whose name they could not have guessed.

        No project id means the project is a PENDING registration of the save
        being answered (P4′), so it has no sections and there is nothing to
        propose. Returning [] is the honest answer; querying on a NULL id would
        propose the sections of no project at all.
        """
        if project_id is None:
            return []
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
        self, name: str, project: str, project_id: int | None
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

    # Entity vocabulary is minted only on facts (fact:1375, migration 033); judgements are refused by `_judgement_entities_error` before this gate.

    #: One counter per kind of refusal an operator acts on, not per error string. A code missing here is uncounted, not folded into a neighbour.
    _REFUSAL_COUNTER: dict[str, str] = {
        "entity_reserved": "entity_reserved",
        "entity_confusable": "entity_confusable",
        "entity_unknown": "entity_unknown",
        "axis_conflict": "axis_conflict",
        "entities_not_allowed_on_judgement": "entities_not_allowed_on_judgement",
        "project_unnameable": "new_project_refused",
        "project_spelling_variant": "new_project_refused",
        "project_confusable": "new_project_refused",
        "domain_unnameable": "new_domain_refused",
        "domain_spelling_variant": "new_domain_refused",
        "domain_confusable": "new_domain_refused",
        "domain_unknown": "new_domain_refused",
        "domain_without_project": "new_domain_refused",
        "domain_not_allowed_on_judgement": "new_domain_refused",
    }

    def _count_refusal(self, payload: object) -> None:
        """Count one ingress refusal on its way out. Never raises.

        Called where the refusal BECOMES A RESPONSE rather than where the dict
        is built: a builder can be called speculatively and its result
        discarded, and counting there would report refusals nobody ever
        received. Reads the code off the payload the caller is about to send, so
        the counter and the client's `error` field can never disagree.
        """
        try:
            if not isinstance(payload, dict):
                return
            key = self._REFUSAL_COUNTER.get(payload.get("error"))
            if key:
                self._registry_refusals.bump(
                    key, ts=datetime.now(timezone.utc).isoformat())
        except Exception:
            pass

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

    async def _entity_vocab_resolve_many(self, names: list[str]) -> dict[str, str]:
        """Resolve MANY names in ONE round trip on ONE connection (S-5,
        security review fact:1412) — the batched twin of
        `_entity_vocab_resolve`, used by `_entity_ingress_error`'s candidate
        loop so a save naming several entities issues one query instead of
        one `self._acquire()` per name. Returns `{name: canonical}` for every
        name the vocabulary recognises; a name ABSENT from the result is
        unregistered — exactly what `_entity_vocab_resolve` returning `None`
        means for one name. `_entity_vocab_resolve` itself is unchanged and
        still used for the single-name mint-conflict re-resolve, where
        batching buys nothing (it is already the rare, race-only path).
        """
        if not names:
            return {}
        async with self._acquire() as conn:
            rows = await conn.fetch(ENTITY_VOCAB_RESOLVE_MANY_SQL, list(names))
        return {
            r["raw_name"]: r["canonical_name"]
            for r in rows if r["canonical_name"] is not None
        }

    async def _entity_vocab_mint(self, name: str, agent_id: str) -> str | None:
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

        Returns `None` — never lets the exception escape — when Postgres
        itself REFUSES the insert outright (`asyncpg.RaiseError`, migration
        033's `entity_vocabulary_before_write` trigger). The only such RAISE
        reachable from this gate fires when NAME normalizes to the empty
        string: `MIN_ENTITY_NAME_LEN` is 2, so a two-character
        punctuation/emoji name (`'!!'`, `'🔥🔥'`) survives
        `sanitize_entity_name` intact and can reach here. Before this fix that
        exception propagated uncaught past `auth_middleware` (which maps only
        `asyncio.TimeoutError`/`web.HTTPException`) as a 500 with a non-JSON
        body — which `memory_bridge.py` cannot `.json()`-parse, so the
        operator saw "coordinator is down" for what was a malformed entity
        name (S-2, security review fact:1412). The caller
        (`_entity_ingress_error`) turns `None` into a structured 400
        `new_entities_invalid` instead.
        """
        try:
            async with self._acquire() as conn:
                row = await conn.fetchrow(
                    ENTITY_VOCAB_MINT_SQL, name, agent_id or "unknown")
        except asyncpg.RaiseError as exc:
            log.info("entity vocabulary: mint of %r refused by the database "
                      "(%s) — refusing the save as a 400, not a 500", name, exc)
            return None
        if row is not None:
            return row["name"]
        resolved = await self._entity_vocab_resolve(name)
        return resolved if resolved is not None else name

    @staticmethod
    def _new_entity_unnameable_refusal(name: str, forced: bool = False):
        """The 400 for a `new_entities` name that normalizes to nothing — or
        None when the name is nameable (`forced=True` returns the body
        unconditionally, for the caller that already has the database's answer).

        Extracted for the reason `_domain_unnameable_refusal` was: two callers
        must not drift. `axis_key` is the Python twin of migration 033's
        `entity_normalize()`, whose BEFORE-write trigger RAISEs on an empty
        key — so a two-character punctuation name (`'!!'`, `'🔥🔥'`) survives
        `sanitize_entity_name` (MIN_ENTITY_NAME_LEN is 2), reaches the mint,
        and is refused by Postgres.

        ⛔ IT HAD TO MOVE EARLIER (v0.9.72, R1). The refusal used to fire
        inside `_entity_commit_mints`, which runs AFTER
        `_commit_axis_registrations` — so `--new-project --new-domain` with
        `new_entities: ["!!"]` committed both registry rows and THEN 400'd,
        which is exactly the leak P4′ exists to close. A gate the database
        enforces and the ingress does not is a refusal that arrives too late
        to be useful.
        """
        if not forced and axis_key(name):
            return None
        log.info("entity ingress: refused mint of %r — normalizes to nothing",
                 name)
        return {
            "status": "error",
            "error": "new_entities_invalid",
            "message": (
                f"new_entities name {_short(name)} cannot be minted "
                "as a canonical entity — it normalizes to nothing "
                "(every character is punctuation, whitespace, or "
                "similar), so there is no spelling left to "
                "register. Name it with at least one letter or "
                "digit, or drop it from new_entities."
            ),
        }

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
    def _entity_reserved_rejection(name: str, reason: str, use: str = "") -> dict:
        """The 400 body for a name that is RESERVED — a schema word, an axis
        declaration, or a registered project name (item 2, v0.9.69;
        `fact:1215`, `decision:1678` (4)).

        ⛔ IT IS A REFUSAL, NOT A DROP, and the difference is the whole point.
        Both halves of this rule were already enforced somewhere DOWNSTREAM —
        the outbox→graph gate filters a schema word out, and a project name
        simply never becomes a useful entity — so the name reached Postgres
        verbatim and vanished on the way to the graph, silently, leaving a
        record whose stored entities do not match its graph edges and an agent
        that goes on sending the same name forever. The gate stays where it is
        (belt); this is the brace, at the point where the caller can still be
        told.
        """
        return {
            "status": "error",
            "error": "entity_reserved",
            "message": (
                f"entity {_short(name)} is {reason} and cannot be an entity. "
                f"{use}"
                "ASK THE OPERATOR which CONCEPT the record is actually about "
                "and name that instead, or drop the name — an entity is a "
                "topic the content is about, never a label from the schema and "
                "never the axis the record is filed on."
            ),
            "reserved_entities": [name],
        }

    @staticmethod
    def _entities_list_too_long_rejection(field: str, length: int) -> dict:
        """The 400 body for an oversized `entities`/`new_entities` list
        (S-5, security review fact:1412) — a correctness/DoS bound on the
        REQUEST, not a performance tuning parameter; see
        `ENTITY_LIST_MAX_LEN`'s module-level comment for the live-corpus
        measurement behind the default.
        """
        return {
            "status": "error",
            "error": "entities_list_too_long",
            "message": (
                f"metadata.{field} names {length} entities; the maximum is "
                f"{ENTITY_LIST_MAX_LEN} (env ENTITY_LIST_MAX_LEN). Split the "
                "save into smaller records, or raise the cap on this install "
                "if that list length is genuinely expected here."
            ),
        }

    @staticmethod
    def _entity_name_too_long_rejection(name: str) -> dict:
        """The 400 body for an over-length entity name (S-5, security review
        fact:1412) — bounds what `entity_vocabulary.name` (unbounded TEXT)
        can ever be asked to hold permanently. Never echoes `name` itself
        (only its length) — an oversized name is exactly the input this
        check exists to keep out of a response body too.
        """
        return {
            "status": "error",
            "error": "entity_name_too_long",
            "message": (
                f"an entity name is {len(name)} characters; the maximum is "
                f"{ENTITY_NAME_MAX_LEN} (env ENTITY_NAME_MAX_LEN). Name each "
                "entity as a concept, not a sentence — a name this long is "
                "almost certainly a phrase that belongs in the record "
                "content, not the entities list."
            ),
        }

    @staticmethod
    def _rewrite_entities(metadata: dict, resolved: dict[str, str]) -> None:
        """Replace every entity name the gate canonicalized, IN PLACE,
        everywhere it is carried: `entities` itself and, if present, the KEYS
        of `entities_provenance` — which must keep naming exactly the values
        in `entities`, or its own "not in this save's entities list" check
        would spuriously fire on a name this gate just rewrote. A name
        mapping to itself (already canonical) is a harmless no-op replace.

        ⛔ S-1 FIX (security review fact:1412): `resolved` is keyed on the
        SANITIZED candidate name — `_entity_ingress_error` resolves
        `candidates` (sanitize_entity_names' output), never the raw strings
        — while `entities`/`entities_provenance` carry the RAW strings the
        caller sent. `sanitize_entity_name` TRANSFORMS its input (collapses
        internal whitespace, strips leading/trailing whitespace), so a raw
        name that differs from its own sanitized form — a trailing space, a
        doubled internal space — used to look ITSELF up directly in
        `resolved` and miss, leaving the UNCANONICAL raw spelling in Tier-1
        metadata (and, downstream, in `entity_registry` and the graph)
        despite having already passed the gate: the primary invariant this
        whole gate exists to enforce, defeated by whitespace. Every raw name
        is now RE-SANITIZED here and looked up by its sanitized form —
        exactly the pairing `_entity_ingress_error` used to build `resolved`
        in the first place, so the two keyspaces can no longer diverge.

        Names `sanitize_entity_name` rejects (noise — never a candidate)
        resolve to themselves here, unchanged: verbatim, exactly as Tier 1
        has always stored them (`sanitize_entity_name`'s own contract —
        "governs what reaches the GRAPH, never what is stored").
        """
        if not resolved:
            return

        _canonical_of = MemoryCoordinator._canonical_entity_name

        entities = metadata.get("entities")
        if isinstance(entities, list):
            metadata["entities"] = [_canonical_of(e, resolved) for e in entities]
        provenance = metadata.get("entities_provenance")
        if isinstance(provenance, dict):
            metadata["entities_provenance"] = {
                _canonical_of(k, resolved): v for k, v in provenance.items()
            }

    @staticmethod
    def _canonical_entity_name(raw: object, resolved: dict[str, str]) -> object:
        """One raw entity name → the spelling that will be STORED. Pure.

        Extracted so the re-save axis-conflict check (item 4 of the v0.9.69
        plan) can ask "what will this save's entities be?" BEFORE the mints
        run, without a second, drifting copy of the mapping rule.
        """
        if not isinstance(raw, str):
            return raw
        sanitized = sanitize_entity_name(raw)
        if sanitized is None:
            return raw
        return resolved.get(sanitized, sanitized)

    @staticmethod
    def _canonical_entity_list(metadata: dict, resolved: dict[str, str]) -> list:
        """`metadata['entities']` as it will be stored, without storing it. Pure."""
        entities = metadata.get("entities")
        if not isinstance(entities, list):
            return []
        if not resolved:
            return list(entities)
        return [MemoryCoordinator._canonical_entity_name(e, resolved)
                for e in entities]

    async def _entity_ingress_error(self, metadata: dict, agent_id: str) -> dict | None:
        """The two halves of the gate, composed — the shape every caller used
        before v0.9.69 and the shape the gate's own unit tests still drive.

        ⚠ handle_save no longer calls THIS. It calls
        `_entity_ingress_validate` early (before the project axis, so no
        registry row is written by a save the entity rules will refuse) and
        `_entity_commit_mints` late (where the gate has always been, so S-4's
        "a mint is the last write before the embed" still holds). This wrapper
        keeps the two halves' composition in ONE place, so a test that drives
        the whole gate is testing what the endpoint does, not a second
        arrangement of it.
        """
        refusal, plan = await self._entity_ingress_validate(metadata)
        if refusal is not None:
            return refusal
        return await self._entity_commit_mints(metadata, agent_id, plan)

    async def _axis_conflict_error(
        self, stored: object, project, domains, entities, is_judgement: bool,
        incoming_type=None, incoming_metadata=None,
    ) -> dict | None:
        """409 when re-saving identical CONTENT under DIFFERENT axes — or None
        (P1, item 4 of the v0.9.69 plan). Pure.

        ⛔ AFTER A RECORD'S FIRST WRITE, NOTHING IN THE SAVE PATH MOVES ITS
        AXES. `ON CONFLICT (content_hash) DO UPDATE` replaces the metadata blob
        WHOLESALE, so re-saving the same words under a different project or
        domain silently relabelled the record — while the graph kept the edges
        from the first write and gained the new ones, so the two stores stopped
        agreeing (`fact:1734` C(a)). The explicit paths — supersede, and a
        ledgered operator backfill (`fact:1255`) — remain the only ways an axis
        moves.

        ⚠ COMPARED THROUGH THE RESOLVERS, NEVER ON THE LITERAL KEYS.
        `resolve_project`/`resolve_domains` read the `decision` blob first and
        the top level second, and they accept a bare string where a list is
        expected — so 152 legacy facts carrying a singular `domain` resolve to
        exactly what a modern `domains` list resolves to, instead of
        false-conflicting on the key name.

        ⚠ A JUDGEMENT COMPARES PROJECT + DOMAINS ONLY. 194 legacy decisions
        carry entities in Postgres; item 3 refuses new ones, so an unchanged
        re-save of one of those must stay idempotent rather than becoming
        permanently unsaveable over a field the record may no longer even send.

        ⚠ COMPARED ON `axis_key`, NEVER ON THE LITERAL SPELLING. The stored
        blob was written when the record was first saved and keeps whatever
        spelling was canonical THEN; the incoming value has just been rewritten
        to whatever is canonical NOW. Comparing the strings made every identical
        re-save of every pre-rename record a 409 — a rename would have
        retrospectively frozen the whole corpus that predates it. `Old_Name`
        and `old-name` are one axis value here for the same reason they are one
        project in the registry.

        ⚠ AND A KEY DIFFERENCE IS NOT YET A CONFLICT. A rename to a genuinely
        DIFFERENT name (`Old-Name` → `new-name`) changes the key, so the stored
        spelling is resolved through the project alias table once — the same
        one-hop resolution ingress does — before anything is refused. The lookup
        is on the rare path only: identical keys never reach it.

        Identity — same content AND same axes — is untouched: it still takes
        the `DO UPDATE` path, which is what repairs a missing embedding.
        """
        stored = _coerce_jsonb_obj(stored)
        if not isinstance(stored, dict):
            return None

        def _refusal(axis: str, existing, incoming) -> dict:
            return {
                "status": "error",
                "error": "axis_conflict",
                "message": (
                    f"this content is already saved under {axis} "
                    f"{_short(existing)}; this save names {_short(incoming)}. "
                    "A record's axes are fixed at its FIRST write — a re-save "
                    "never moves them, because the graph edges written the "
                    "first time do not move with it. If the record genuinely "
                    "belongs elsewhere, SUPERSEDE it with a new record that "
                    "says so; if the axes were wrong, that is a deliberate "
                    "operator backfill with its own ledger, never a side "
                    "effect of a save. If you meant to save something new, "
                    "the content has to differ."
                ),
                "axis": axis,
                "existing": existing,
                "incoming": incoming,
            }

        incoming_meta = incoming_metadata if isinstance(incoming_metadata, dict) else {}
        if incoming_type is not None or incoming_meta:
            incoming_label = _record_kind_label(incoming_meta, incoming_type=incoming_type)
        else:
            incoming_label = ONT.decision if is_judgement else ONT.fact
        stored_label = _record_kind_label(stored)
        if stored_label != incoming_label:
            return _refusal("kind", stored_label, incoming_label)

        existing_project = resolve_project(stored)
        if axis_key(existing_project) != axis_key(project):
            # Different keys look like a rename. Resolve the stored spelling one hop before calling it a conflict.
            resolved_stored = (await self._resolve_project_alias(existing_project)
                               if existing_project else None)
            if resolved_stored is None or \
                    axis_key(resolved_stored) != axis_key(project):
                return _refusal("project", existing_project, project)

        existing_domains = resolve_domains(stored)
        # Compare domains by key, not by alias. An alias lookup calls _project_identity, which raises on a registry blip and would turn that into a refused re-save. A real section rename still conflicts; that gap stays open.
        if {axis_key(d) for d in existing_domains} != \
                {axis_key(d) for d in (domains or [])}:
            return _refusal("domains", existing_domains, list(domains or []))

        if is_judgement:
            return None

        existing_entities = stored.get("entities")
        existing_entities = (existing_entities
                             if isinstance(existing_entities, list) else [])
        incoming = [e for e in (entities or []) if isinstance(e, str)]
        if {e for e in existing_entities if isinstance(e, str)} != set(incoming):
            return _refusal("entities", existing_entities, incoming)
        return None

    @staticmethod
    def _judgement_entities_error(metadata: dict) -> dict | None:
        """400 unless this judgement omits entities / new_entities (or sends an empty list); facts alone mint vocabulary (decision:1664)."""
        entities = metadata.get("entities")
        new_entities = metadata.get("new_entities")
        offending = "entities" if entities else None
        if offending is None and new_entities:
            offending = "new_entities"
        if offending is None:
            return None
        kind = record_label_for_type(metadata.get("type")).lower()
        return {
            "status": "error",
            "error": "entities_not_allowed_on_judgement",
            "message": (
                f"a {kind} may not carry {offending}. Only "
                "FACTS name entities: a judgement reaches its topics by "
                "walking to the facts it is grounded in, so an entity named "
                "here is never written to the graph and only adds an unvetted "
                "name to the vocabulary. Save the concept on the FACT that "
                "evidences it, and cite that fact in grounded_in. An empty "
                "entities list is accepted and means the same thing as "
                "omitting it."
            ),
        }

    async def _entity_confusable_error(
        self, to_mint: list[str], metadata: dict,
    ) -> dict | None:
        """400 when a name about to be MINTED is confusable with one the
        vocabulary already holds and the caller has not confirmed it is
        distinct — or None (E1, item 1 of the v0.9.69 plan).

        Mirrors `_new_project_refusal`'s confusable half exactly, including the
        override: `metadata.confirm_distinct_from` names the existing spellings
        this new name is deliberately different from, compared on the spelling
        key so confirming `Games Workshop` confirms `games-workshop`.

        ONE query per name to mint. That is the same shape
        `_new_project_refusal` uses, and `new_entities` is a short list by
        construction (every name in it must also appear in `entities`, which is
        capped at `ENTITY_LIST_MAX_LEN`) — a mint is already the rare path.
        """
        for name in to_mint:
            async with self._acquire() as conn:
                rows = await conn.fetch(
                    ENTITY_CONFUSABLE_SQL, name,
                    ENTITY_CONFUSABLE_SIMILARITY, ENTITY_PROPOSAL_LIMIT)
            near = [r["name"] for r in rows]
            if not near:
                continue
            unconfirmed = unconfirmed_confusables(
                near, metadata.get("confirm_distinct_from"))
            if not unconfirmed:
                continue
            log.info("entity vocabulary: %r held for confirmation against %s",
                     name, unconfirmed)
            return {
                "status": "error",
                "error": "entity_confusable",
                "message": (
                    f"entity {_short(name)} is close enough to a name the "
                    f"vocabulary already holds to be a typo for it: "
                    f"{unconfirmed}. ASK THE OPERATOR whether this is genuinely "
                    "a separate concept. If it is, re-send with "
                    "metadata.confirm_distinct_from listing the names above; if "
                    "it is not, save under the existing name. Minting a variant "
                    "is how one concept quietly becomes two."
                ),
                "proposals": near,
            }
        return None

    async def _entity_reserved_project_error(
        self, candidates: list[str], metadata: dict | None = None,
    ) -> dict | None:
        """400 when one of these entity names IS a project — or None.

        ONE round trip for the whole list. Three populations, in ascending cost:

          1. the parked-project SENTINEL — no query at all, because a CHECK
             constraint keeps it out of `projects` so no query could answer
          2. THIS SAVE'S OWN project — also no query, and it is the one case a
             registry lookup CANNOT answer. This check runs before
             `_project_ingress_error`, which is what ACCEPTS a declared-new
             project (the row itself lands later still, in
             `_commit_axis_registrations`); so `--project Foo --new-project`
             with `entities: ["Foo"]`
             asks the registry about a name that is not in it yet, gets "not a
             project", and files the record's own axis as its own topic — the
             exact `fact:1215` violation, on the one save where it is most
             likely, because the operator has that name in mind twice.
             Resolved through `resolve_project` + `axis_key`, so it costs
             nothing and needs no ordering change.
          3. every registered project and every RETIRED spelling of one — the
             one query (see `ENTITY_RESERVED_PROJECT_SQL`)
        """
        keys = {axis_key(n): n for n in candidates if axis_key(n)}
        if not keys:
            return None
        for key, registered_as in RESERVED_ENTITY_AXIS_KEYS.items():
            if key in keys:
                log.info("entity ingress: refused %r — the parked-project "
                         "sentinel %r", keys[key], registered_as)
                return self._entity_reserved_rejection(
                    keys[key],
                    f"the parked-project sentinel {_short(registered_as)}",
                    "It is a value on the PROJECT axis, carried by the "
                    "record's own project field. ",
                )
        own = resolve_project(metadata) if metadata is not None else None
        own_key = axis_key(own)
        if own_key and own_key in keys:
            log.info("entity ingress: refused %r — it is THIS record's own "
                     "project %r", keys[own_key], own)
            return self._entity_reserved_rejection(
                keys[own_key],
                f"this record's own project {_short(own)}",
                "The record is already filed under it; naming it as an entity "
                "too files the axis as its own topic. ",
            )
        async with self._acquire() as conn:
            rows = await conn.fetch(ENTITY_RESERVED_PROJECT_SQL, sorted(keys))
        for row in rows:
            name = keys.get(row["matched_key"])
            if name is None:
                continue
            log.info("entity ingress: refused %r — a spelling of the "
                     "registered project %r", name, row["name"])
            return self._entity_reserved_rejection(
                name,
                f"the registered project {_short(row['name'])}",
                "A project is an AXIS a record is filed on, carried by its "
                "own project field and by the PROJECT_OF edge; naming it as "
                "an entity as well makes the axis a hub that records cluster "
                "on. ",
            )
        return None

    async def _entity_ingress_validate(
        self, metadata: dict,
    ) -> tuple[dict | None, dict]:
        """The VALIDATION half of the save-time entity ingress gate — every
        refusal it can produce, and NOT ONE WRITE (item 8 of the v0.9.69
        post-first-write hardening plan; `fact:1734` A(4)).

        Returns `(refusal_body_or_None, plan)`. `plan` is what
        `_entity_commit_mints` needs to finish the job:

          ``resolved``   {sanitized candidate: canonical} for every name the
                         vocabulary already knows
          ``to_mint``    the sanitized candidates `new_entities` asked to mint,
                         in candidate order — nothing is minted yet
          ``canonical``  the entity list AS IT WILL BE STORED, computed without
                         writing anything (a to-mint name canonicalizes to
                         itself except in the mint race `_entity_vocab_mint`
                         arbitrates). The re-save axis-conflict check reads
                         this, because it must run BEFORE the mints it is
                         protecting.

        ⛔ WHY IT MOVED IN FRONT OF THE PROJECT AXIS. `_project_ingress_error`
        REGISTERS a project as its acceptance, and the entity rules could still
        400 the save afterwards — so a refused save left a registry row behind
        with no record that named it. Validating here means every entity
        refusal fires before the first registry write; the mint itself stays
        last (see `_entity_commit_mints`).

        Returns `(None, plan)` with an empty plan for the ordinary case — no
        entities at all (`fact:1215`: entities stay optional and never gate
        anything).

        Runs on BOTH writers of caller-supplied entity names: handle_save
        (facts and decisions share this generic path — a decision's `entities`
        stays Tier-1-only and is never minted into the graph, but it DOES
        reach Postgres metadata, so it is in scope for canonicalization) and
        handle_retrospective (its own endpoint, its own `entities` field).
        Retrospectives also never mint into the graph (their v2 outbox row
        writes no MENTIONS edge and no DOMAIN_OF edge), but the same
        Tier-1-reaches-metadata reasoning applies.

        ⛔ ENTITIES STAY OPTIONAL (fact:1215) — an empty/absent list returns
        None immediately, before any lookup, length cap, or `new_entities`
        validation. The gate must never affect consolidation eligibility,
        which keys on project+domain, never on entities.

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

        S-5 BOUNDS (security review fact:1412): `entities` and `new_entities`
        are each capped at `ENTITY_LIST_MAX_LEN` items, and every individual
        name at `ENTITY_NAME_MAX_LEN` characters — both env-overridable,
        checked on the RAW strings before sanitize (a bound on the request,
        not on what survives filtering), both measured against the live
        corpus before choosing a default (module-level comment beside the
        constants; EG_LEG1_HANDOFF.md's FIX ROUND section has the numbers).
        Candidate resolution is ONE batched round trip
        (`_entity_vocab_resolve_many`), not one query per name.

        S-10 (security review fact:1412): every name in `new_entities` must
        also appear in `entities` — ENFORCED here, not merely claimed in the
        refusal message (which is what it was before this fix: a name that
        did not appear in `entities` was silently ignored rather than
        rejected). Matched in the same sanitized-candidate space the S-1
        rewrite fix uses, so a whitespace variant in `new_entities` matches
        its counterpart in `entities` correctly rather than silently failing
        to match.
        """
        # Returned only beside a refusal, which the caller does not read. A success path must build its own canonical list.
        empty_plan: dict = {"resolved": {}, "to_mint": [], "canonical": []}

        raw_entities = metadata.get("entities") or []
        if len(raw_entities) > ENTITY_LIST_MAX_LEN:
            return (self._entities_list_too_long_rejection(
                "entities", len(raw_entities)), empty_plan)
        for e in raw_entities:
            if isinstance(e, str) and len(e) > ENTITY_NAME_MAX_LEN:
                return self._entity_name_too_long_rejection(e), empty_plan

        # Reserved schema/axis names are refused on the raw list (and new_entities when it is already a list).
        declared_new = metadata.get("new_entities")
        swept = list(raw_entities) + (
            list(declared_new) if isinstance(declared_new, list) else [])
        for e in swept:
            reason = reserved_entity_name_reason(e)
            if reason is not None:
                log.info("entity ingress: refused %r — %s", e, reason)
                return self._entity_reserved_rejection(e, reason), empty_plan

        candidates = sanitize_entity_names(raw_entities)
        if not candidates:
            # Shape-noise-only entities stay verbatim (not empty_plan []) so a re-save does not 409 against a list that never moved.
            return None, {"resolved": {}, "to_mint": [],
                          "canonical": self._canonical_entity_list(metadata, {})}
        candidates_set = set(candidates)

        new_entities_raw = metadata.get("new_entities")
        mint_requested: set[str] = set()
        if new_entities_raw is not None:
            if not isinstance(new_entities_raw, list) or not all(
                isinstance(n, str) for n in new_entities_raw
            ):
                return {
                    "status": "error",
                    "error": "new_entities_invalid",
                    "message": "metadata.new_entities must be a list of strings.",
                }, empty_plan
            if len(new_entities_raw) > ENTITY_LIST_MAX_LEN:
                return (self._entities_list_too_long_rejection(
                    "new_entities", len(new_entities_raw)), empty_plan)
            for n in new_entities_raw:
                if len(n) > ENTITY_NAME_MAX_LEN:
                    return self._entity_name_too_long_rejection(n), empty_plan

            # S-10: enforce the subset claim the refusal message makes,
            # matched in sanitized-candidate space (see docstring).
            for raw_name in new_entities_raw:
                sanitized = sanitize_entity_name(raw_name)
                if sanitized is None or sanitized not in candidates_set:
                    return {
                        "status": "error",
                        "error": "new_entities_invalid",
                        "message": (
                            f"new_entities names {_short(raw_name)}, which does "
                            "not appear in metadata.entities. Every name in "
                            "new_entities must also be named in entities — add "
                            "it there, or remove it from new_entities."
                        ),
                    }, empty_plan
                mint_requested.add(sanitized)

            # Unnameable (normalizes to nothing) is refused here before any mint query.
            for sanitized in sorted(mint_requested):
                unnameable = self._new_entity_unnameable_refusal(sanitized)
                if unnameable is not None:
                    return unnameable, empty_plan

        # fact:1215: a project name is an axis, never an entity. Compared on axis_key, and before resolution, because the legacy vocabulary already holds those names and resolving one would let it back in.
        reserved = await self._entity_reserved_project_error(candidates, metadata)
        if reserved is not None:
            return reserved, empty_plan

        resolved = await self._entity_vocab_resolve_many(candidates)
        unknown = [n for n in candidates if n not in resolved]
        to_mint: list[str] = []

        if unknown:
            to_mint = [n for n in unknown if n in mint_requested]
            still_unknown = [n for n in unknown if n not in mint_requested]
            if still_unknown:
                return self._entity_unknown_rejection(still_unknown), empty_plan

            # Last check: it needs the names that will actually be minted, and a near-match of one already held is refused.
            confusable = await self._entity_confusable_error(to_mint, metadata)
            if confusable is not None:
                return confusable, empty_plan

        plan = {
            "resolved": resolved,
            "to_mint": to_mint,
            # A name about to be minted canonicalizes to itself. The mint race can only substitute another spelling of the same key.
            "canonical": self._canonical_entity_list(
                metadata, resolved | {n: n for n in to_mint}),
        }
        return None, plan

    async def _entity_commit_mints(
        self, metadata: dict, agent_id: str, plan: dict,
    ) -> dict | None:
        """The WRITING half of the entity gate: mint what `new_entities` asked
        for, then rewrite `metadata['entities']` (+ `entities_provenance` keys)
        to the CANONICAL spelling in place — the same "resolve once at ingress,
        store the canonical" choice `_project_ingress_error`/
        `_domain_value_error` make for their own axes (rule 3) — and pop
        `metadata['new_entities']` (S-8, security review fact:1412: a transient
        mint REQUEST must not persist as durable record content, visible to
        every future reader and to REM's prompts).

        Returns the 400 body, or None when the save may proceed. The only
        refusal left on this side is the one that needs the database's own
        answer: a name Postgres itself refuses to mint.

        ⛔ S-4 CALL-SITE CONTRACT (security review fact:1412, ruled by
        decision:1413): the caller MUST invoke this method LAST — after every
        other 400-capable metadata validation on that endpoint
        (entities_provenance shape/membership, supersedes/grounded_in/
        existence checks, the re-save axis-conflict check, ...) — immediately
        before the hard-mandate embedding call. A mint is a real write to
        `entity_vocabulary`, on its own connection, sharing no transaction with
        the record insert; any refusal that can still fire AFTER this method
        runs would leave a minted canonical permanently attached to a record
        that never existed. Calling it last eliminates every SUCH refusal from
        racing a mint.

        The ONE residual this does not close — a mint surviving the
        hard-mandate embedding call's own 503, which necessarily runs AFTER
        this method returns None — is ACCEPTED BY RULING (decision:1413),
        not fixed transactionally: it mirrors the exposure
        `_commit_axis_registrations` (which now runs immediately before this
        method) still carries — a project or a section can likewise be
        registered by a save that goes on to fail on embedding — so it is not
        a new class of risk this leg introduces, and closing it would need a
        shared transaction
        between the vocabulary write and the record insert that neither axis
        has today.
        """
        resolved = dict(plan.get("resolved") or {})
        for name in (plan.get("to_mint") or []):
            canonical = await self._entity_vocab_mint(name, agent_id)
            if canonical is None:
                # DB RAISE on mint becomes 400 (gateway already refused unnameable; this is locale/trigger drift).
                return self._new_entity_unnameable_refusal(name, forced=True)
            resolved[name] = canonical
            log.info("entity vocabulary: %r minted as canonical %r by %s "
                     "(new_entities)", name, canonical, agent_id)

        self._rewrite_entities(metadata, resolved)
        metadata.pop("new_entities", None)
        return None

    async def _project_registered(self, name: str) -> bool:
        """Is this an established project? (P4, migration 022's registry.)"""
        async with self._acquire() as conn:
            return await conn.fetchval(PROJECT_EXISTS_SQL, name) is not None

    async def _project_identity(self, project) -> int | None:
        """The registry id behind a project name (migration 027). RAISES when
        it cannot produce one for a name that HAS one to produce.

        Deliberately UNCACHED. The registry is tens of rows and this is one
        indexed lookup on a path that is already writing to two stores; a cache
        would buy nothing measurable and would hold a stale answer across
        exactly the operation the identity exists to survive — a rename.

        ⛔ STRICT SINCE v0.9.69 (item 6, ruled R3). It used to return None from
        ANY cause — an unregistered name or a failed lookup alike — and every
        caller then wrote a node keyed on the NAME instead
        (``project_merge_cypher(None)``). That rule made sense while an
        unregistered project name could still reach a save. It cannot any more:
        the ingress gate registers every project it accepts, so a missing row
        is no longer "a name nobody registered" — it is a DATA-INTEGRITY DEFECT,
        and the name-keyed fallback silently mints a SECOND node for a project
        that already has one, which is precisely the divergence migration 027
        exists to remove.

        So both failures now raise :class:`ProjectIdentityUnavailable`:

          * the lookup itself failed (the registry is unreadable)
          * the lookup succeeded and there is NO ROW for a non-blank name

        and each caller answers for its own surface: an outbox row RETRIES and
        then goes `failed`, where it is visible; ingress turns it into a 503
        `registry_unavailable`, consistent with the hard embedding mandate; a
        READER degrades to "no identity" and says so, never a 500.

        ``None`` survives for exactly one input — a blank or absent name. That
        is the parked-record sentinel path (``project_for_graph`` returns None
        for the sentinel), and it is the only remaining caller of
        ``project_merge_cypher``'s name-keyed branch.

        ⛔ This SUPERSEDES the rule stated in ``project_merge_cypher``'s own
        docstring ("the WRITE must never be lost"); that docstring has been
        rewritten rather than edited around.
        """
        if not isinstance(project, str) or not project.strip():
            return None
        name = project.strip()
        try:
            async with self._acquire() as conn:
                project_id = await conn.fetchval(PROJECT_ID_SQL, name)
        except Exception as exc:
            log.error("project identity lookup FAILED for %r: %s", name, exc)
            raise ProjectIdentityUnavailable(
                f"the project registry could not be read for {name!r}") from exc
        if project_id is None:
            log.error("project identity: no registry row for %r — every project "
                      "a save accepts is registered, so this is a data-integrity "
                      "defect, not an unknown name", name)
            raise ProjectIdentityUnavailable(
                f"no registry identity for project {name!r}")
        return project_id

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

    def _note_registry_read_failure(self, axis: str, exc: Exception) -> str:
        """Record a registry read that failed, and return the reason to disclose.

        ⛔ A DEGRADE THAT CHANGES THE ANSWER MUST BE VISIBLE. When the registry
        cannot be read, by-key resolution becomes a no-op and a search filter
        resolves to nothing — which is indistinguishable, in the response, from
        the legitimate case of a name nobody registered. A journal warning is not
        enough: nobody reads the gateway's journal while looking at an empty
        search result, and Group 3's question is "can this be seen FAILING?".

        So it COUNTS (telemetry `axis_registry_read_failures`, additive, with a
        last-event timestamp per fact:1314's shape) and it RETURNS a short reason
        the caller puts in `filters_resolved.error`. Two audiences, deliberately:
        the counter is for the monitor, the string is for whoever is looking at
        this one answer and needs to know it is not authoritative.
        """
        self._axis_registry_read_failures += 1
        self._axis_registry_read_failure_last_ts = \
            datetime.now(timezone.utc).isoformat()
        log.warning("%s registry read failed — by-key resolution is a no-op for "
                    "this call, and a filter on it matches only the literal "
                    "string: %s", axis, exc)
        return f"{axis}_registry_unavailable"

    async def _project_spellings(self, supplied: str) -> tuple[list, dict, str | None]:
        """`(matching project names, {alias: canonical}, error)`. Never raises.

        Everything that could answer "what does THIS spelling mean?", and nothing
        else: the registry rows whose `name` or whose stored `normalized_key`
        matches — at most two, both indexed — plus every active alias.

        ⚠ THE KEY IS READ, NOT COMPUTED, IN SQL. Migration 035 maintains
        `projects.normalized_key` by trigger and puts a UNIQUE constraint on it,
        so this is an indexed equality on a value the database owns rather than a
        scan over a normalising expression. The alias half is still a full read,
        because `aliases` deliberately carries no key column: 024 permits one
        spelling to alias on both axes, so a key-unique constraint there would
        forbid what the design allows, and the ambiguity rule is enforced by
        trigger instead.

        ⛔ UNCACHED, deliberately, for the third time in this file (see
        `_project_identity`, `_domain_identity`, `_entity_vocab_resolve`): a
        cache here would hold a stale answer across precisely the operation the
        registry exists to survive — a rename — and no measurement justifies a
        size or a TTL (fact:1338: an unmeasured cache parameter is a measurement
        claim in disguise).

        A failure degrades to `([], {}, reason)`, which makes every by-key step a
        no-op and leaves the exact-match behaviour that shipped before it — but
        SAYS SO, which the first version of this did not. A read path must not
        start blocking on registry state because a query failed, and a save must
        not turn a transient database fault into a rejected record.
        """
        try:
            async with self._acquire() as conn:
                names = [r["name"] for r in await conn.fetch(
                    PROJECT_NAME_OR_KEY_SQL, supplied, axis_key(supplied))]
                aliases = {r["alias"]: r["canonical"]
                           for r in await conn.fetch(ACTIVE_ALIASES_SQL)}
            return names, aliases, None
        except Exception as exc:
            return [], {}, self._note_registry_read_failure("project", exc)

    async def _domain_spellings(
        self, project_id, supplied,
    ) -> tuple[list, dict, str | None]:
        """The domain twin, scoped to ONE project.

        It takes a `project_id` for the reason every statement in `domain_axis`
        does: a section is identified WITHIN its project, and a by-name-alone
        lookup on this axis is the one way it reproduces the defect the project
        registry was built to remove. No project id means there is no scope to
        resolve in — that is not a failure and reports no error.

        `supplied` is one name or MANY: a record and a search filter both name
        several sections at once, and answering only the first would leave the
        rest silently unresolved.
        """
        if project_id is None:
            return [], {}, None
        wanted = [supplied] if isinstance(supplied, str) else list(supplied or [])
        wanted = [n for n in wanted if isinstance(n, str) and n.strip()]
        if not wanted:
            return [], {}, None
        try:
            async with self._acquire() as conn:
                names = [r["name"] for r in await conn.fetch(
                    DOMAIN_NAME_OR_KEY_SQL, project_id, wanted,
                    [axis_key(n) for n in wanted])]
                aliases = {r["alias"]: r["canonical"]
                           for r in await conn.fetch(DOMAIN_ALIASES_SQL, project_id)}
            return names, aliases, None
        except Exception as exc:
            return [], {}, self._note_registry_read_failure("domain", exc)

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
        vis_res = _validate_visibility_and_scope(body)
        if isinstance(vis_res, web.Response):
            return vis_res
        visibility, scope = vis_res


        # The verified agent overrides the client claim, and the metadata dict is reattached because a missing key would drop the mutation. agent_id uses that same name: the body defaults it to memory_bridge.
        if request.get("authenticated_agent"):
            metadata["source"] = request["authenticated_agent"]
            agent_id = request["authenticated_agent"]
            body["metadata"] = metadata

        # decision 347: stamp the kernel principal and strip the client claim. The operator cannot supply this.
        if isinstance(metadata, dict):
            _apply_principal(metadata, request.get("principal"))
            body["metadata"] = metadata

        if not content:
            return web.json_response(
                {"status": "error", "message": "content is required"}, status=400
            )
        try:
            _require_outbox_type({
                "type": _outbox_row_type(
                    metadata.get("type") if isinstance(metadata, dict) else None
                ),
            })
        except ValueError:
            raw_type = metadata.get("type") if isinstance(metadata, dict) else None
            return web.json_response(
                {
                    "status": "error",
                    "error": "unknown_type",
                    "message": (
                        f"metadata.type {_short(raw_type)} is not a known record type. "
                        "A fact omits type (or sends 'fact'); a decision sends 'decision'."
                    ),
                },
                status=400,
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

        # Reject a bad decision shape here. A corrupt outbox row would replay it on every restart.
        if metadata.get("type") == "decision":
            decision_data = metadata.get("decision")
            decision_data = decision_data if isinstance(decision_data, dict) else {}
            # decided_by must be text. A truthy list used to pass and then be discarded, so the caller lost the value.
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
            # After the required-field check, so a missing decided_by still fails instead of being filled from the socket.
            if _normalise_decided_by(metadata):
                log.info("decision ingress: decided_by normalised to principal %r "
                         "(claim %r preserved)", metadata["decision"]["decided_by"],
                         metadata["decision"].get("decided_by_claimed"))
                body["metadata"] = metadata

        # Project is required on facts and decisions (not retrospectives, which inherit); registry check runs after decision-shape so missing fields surface together.
        axis_report: dict = {}

        # Entity validation runs before the project axis so a 400 cannot leave a stray registry row; mint still commits last (fact:1734, decision:1413).
        entities_field = metadata.get("entities", [])
        if not isinstance(entities_field, list):
            return web.json_response(
                {"status": "error", "message": "metadata.entities must be a list"},
                status=400,
            )
        # decision:1664: only facts carry entities. A judgement that names any is refused before any write; an empty list stays allowed.
        if is_judgement_type(metadata.get("type")):
            judgement_error = self._judgement_entities_error(metadata)
            if judgement_error is not None:
                self._count_refusal(judgement_error)
                return web.json_response(judgement_error, status=400)
            # Do not setdefault entities. That persisted a key the caller never sent, and a judgement must carry none in any store.
            entity_plan: dict = {"resolved": {}, "to_mint": [], "canonical": []}
        else:
            entity_refusal, entity_plan = await self._entity_ingress_validate(metadata)
            if entity_refusal is not None:
                self._count_refusal(entity_refusal)
                return web.json_response(entity_refusal, status=400)

        # A decision's project is decision.project; copy it to the top-level key (blob wins over a cwd walk) before the registry gate (fact:1757).
        decision_project_rewrite = None
        _blob = metadata.get("decision")
        if (metadata.get("type") in ("decision", "retrospective")
                and isinstance(_blob, dict)):
            _asserted = _blob.get("project")
            if isinstance(_asserted, str) and _asserted.strip():
                _asserted = _asserted.strip()
                _sent = metadata.get("project")
                if _sent != _asserted:
                    log.info(
                        "decision project: top-level %r replaced by "
                        "decision.project %r — the blob is authoritative",
                        _sent, _asserted)
                    decision_project_rewrite = {
                        "from": _sent if isinstance(_sent, str) and _sent.strip()
                                else None,
                        "to": _asserted,
                        "reason": "decision.project is authoritative",
                    }
                metadata["project"] = _asserted

        project_error = await self._project_ingress_error(
            metadata, agent_id, axis_report)
        if project_error is not None:
            self._count_refusal(project_error)
            return web.json_response(project_error, status=400)

        # Tell the caller about a rewrite they did not ask for. If ingress also moved the name, that canonical is the one destination.
        if decision_project_rewrite is not None:
            _ingress = axis_report.get("project_resolved") or {}
            if _ingress.get("canonical"):
                decision_project_rewrite["to"] = _ingress["canonical"]
            axis_report["project_resolved"] = {
                **_ingress, **decision_project_rewrite}

        # Domain resolves after project (canonical name in hand); an unreadable registry is 503, not a silent accept.
        try:
            domain_error = await self._domain_ingress_error(
                metadata, agent_id, axis_report)
        except ProjectIdentityUnavailable as exc:
            log.error("save refused: %s", exc)
            return web.json_response(
                {
                    "status": "error",
                    "error": "registry_unavailable",
                    "message": (
                        "the project registry could not be read, so this "
                        "record's axes cannot be resolved and it would be "
                        "saved unfiled. Nothing was written. Retry; if it "
                        "persists, the gateway's database is the thing to "
                        "look at, not this save."
                    ),
                },
                status=503,
            )
        if domain_error is not None:
            self._count_refusal(domain_error)
            return web.json_response(domain_error, status=400)

        # Materialise a decision's asserted domains to metadata top-level after ingress rewrite (additive; blob unchanged) (decision:1214).
        if metadata.get("type") == "decision":
            decision_domains = resolve_domains(metadata)
            if decision_domains:
                metadata["domains"] = decision_domains

        # decision 381, refined by 384: supersedes is checked before embed, and dependents are flagged at retrieval rather than rewritten here.
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

        # Shape already validated (and the ENTITY GATE'S VALIDATION HALF
        # already run) above, before the project axis.
        entities = metadata.get("entities", [])

        # entities_provenance is an optional operator|agent map, shape-checked at ingress (fact:1215).
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
        # Missing provenance is not an error. The response says so at capture time instead of leaving it for later inspection.
        entities_provenance_missing = bool(entities) and entities_provenance is None

        # Cheap hash pre-check before mint/embed; the authoritative axis_conflict is the FOR UPDATE re-read inside the transaction.
        is_judgement = is_judgement_type(metadata.get("type"))
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        incoming_project = resolve_project(metadata)
        incoming_domains = resolve_domains(metadata)
        async with self._acquire() as conn:
            prior = await conn.fetchval(
                "SELECT metadata FROM technical_docs WHERE content_hash = $1",
                content_hash,
            )
        if prior is not None:
            conflict = await self._axis_conflict_error(
                prior, incoming_project, incoming_domains,
                entity_plan.get("canonical") or [], is_judgement,
                incoming_type=metadata.get("type"), incoming_metadata=metadata)
            if conflict is not None:
                self._count_refusal(conflict)
                return web.json_response(conflict, status=409)

        # decision:1413: registry rows are written after every 400 and the 409, and before the mint. A refused save must not leave a project or domain row.
        try:
            await self._commit_axis_registrations(axis_report, agent_id)
        except ProjectIdentityUnavailable as exc:
            # The project row's id could not be read back, so its sections cannot be filed. A record that cannot be filed is not saved.
            log.error("save refused while committing axis registrations: %s", exc)
            return web.json_response(
                {
                    "status": "error",
                    "error": "registry_unavailable",
                    "message": (
                        "the project registry could not be read back while "
                        "registering this record's axes, so it would be saved "
                        "unfiled. No record was written. Retry; if it "
                        "persists, the gateway's database is the thing to "
                        "look at, not this save."
                    ),
                },
                status=503,
            )

        # Entity mint is last among writes (after axis commit, before embed) so a 400 cannot leave a vocabulary row (fact:1375, decision:1413).
        entities_before = list(entities)
        entity_error = await self._entity_commit_mints(
            metadata, agent_id, entity_plan)
        if entity_error is not None:
            self._count_refusal(entity_error)
            return web.json_response(entity_error, status=400)
        entities = metadata.get("entities", [])
        # fact:1412: report a rewrite when the gate changed a name. None when nothing moved, so an already-canonical list adds no field noise.
        entities_rewritten = entities if entities != entities_before else None

        # Embedding — hard mandate; no save without a vector
        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                embedding = await self._embed(content, client)
        except RuntimeError as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=503)

        # Sort, then get and acquire together, so the bounded registry cannot evict a lock this save is about to hold. Release only what was acquired if the list is cancelled.
        acquired: list[asyncio.Lock] = []
        alt_stats: dict | None = None
        try:
            for e in sorted(set(entities)):
                lk = await self._lock_for(e)
                await lk.acquire()
                acquired.append(lk)
            async with self._acquire() as conn:
                async with conn.transaction():
                    # The pre-check is not the guard. Only FOR UPDATE stops a concurrent save of the same content between the read and the insert.
                    prior = await conn.fetchval(
                        "SELECT metadata FROM technical_docs"
                        " WHERE content_hash = $1 FOR UPDATE",
                        content_hash,
                    )
                    if prior is not None:
                        conflict = await self._axis_conflict_error(
                            prior, incoming_project, incoming_domains,
                            entities, is_judgement,
                            incoming_type=metadata.get("type"),
                            incoming_metadata=metadata)
                        if conflict is not None:
                            self._count_refusal(conflict)
                            return web.json_response(conflict, status=409)

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

                    # decision 582: pick the role from the stored label and fact_kind. Advisory only; the flat grounded_in list stays for telemetry.
                    grounded_ids = [
                        g for g in (metadata.get("grounded_in") or [])
                        if isinstance(g, int) and not isinstance(g, bool)
                    ]
                    grounded_typed = await self._resolve_typed_grounding(
                        conn, grounded_ids, metadata.get("grounded_roles") or {}
                    )

                    # A parked fact cited as evidence inherits the project its citing judgements agree on. The pg_ids are already resolved to real records here.
                    await self._promote_grounded_parked_facts(
                        conn, grounded_typed, agent_id, pg_id
                    )

                    # Outbox row for this fact, in the same transaction. The outbox worker drains it.
                    outbox_params = {
                            "content_snippet": content[:200],
                            "source": metadata.get("source", "coordinator"),
                            # A judgement carries no entities, so the key is omitted rather than sent empty.
                            **({} if is_judgement_type(metadata.get("type"))
                               else {"entities": entities}),
                            "agent_id": agent_id,
                            # Fact provenance edges (person/agent/project) are derived and written only when present (decision:912).
                            "person": metadata.get("principal"),
                            # The resolved project, never a section and never the sentinel. A parked record must not count as a project in the insight gate.
                            "project": project_for_graph(metadata),
                            # Outbox carries domain NAMES (resolved at apply); judgements never reach here with a value.
                            "domains": resolve_domains(metadata),
                            "type": _outbox_row_type(metadata.get("type")),
                            "decision": metadata.get("decision", {}),
                            "source_ref": metadata.get("source_ref") or None,
                            # decision 553: fact_kind is derived from source_ref at first write, not asked separately.
                            "fact_kind": fact_kind_from_source_ref(
                                metadata.get("source_ref")
                            ),
                            # decision 550: grounded_in is the evidence facts, written 1-1 at first write. source_ref stays the fact's own origin.
                            "grounded_in": grounded_ids,
                            # Typed roles + asserted_by for the cross-type writer
                            # (decision 582): [{pg_id, rel, asserted_by, label}].
                            "grounded": grounded_typed,
                            # Mirror supersession on this row so the census does not gain a second outbox type.
                            "supersedes": (
                                supersedes if (supersedes is not None
                                               and supersedes != pg_id) else None
                            ),
                    }
                    _require_outbox_type(outbox_params)
                    await conn.execute(
                        """
                        INSERT INTO neo4j_outbox (pg_id, cypher_params)
                        VALUES ($1, $2::jsonb)
                        """,
                        pg_id,
                        outbox_params,
                    )

                    # decision 384: retire the predecessor in the same transaction. The id guard skips a hash collision onto this row itself.
                    if supersedes is not None and supersedes != pg_id:
                        await conn.execute(
                            "UPDATE technical_docs"
                            " SET superseded = true, superseded_by = $2"
                            " WHERE id = $1 AND id != $2",
                            supersedes, pg_id,
                        )

                    # Store alternative text with the record. The background worker embeds them, so the save path stays one embedding call.
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

        # Log after commit so a line means the alternative rows are actually there.
        if alt_stats and (alt_stats["written"] or alt_stats["removed"]):
            log.info(
                "alternatives: pg_id=%d wants %d row(s) — %d written pending, %d removed",
                pg_id, alt_stats["desired"], alt_stats["written"], alt_stats["removed"],
            )

        # Neo4j is applied asynchronously by the outbox worker.
        if request.rel_url.query.get("consistency") == "neo4j":
            neo4j_status = await self._wait_for_outbox(pg_id)
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
        # fact:1215: who named these entities is a different question from Tier-3 eligibility, so it is its own field. Set only when entities were named and none have provenance.
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
            # fact:1412: non-null only when the vocabulary gate rewrote a name the caller sent.
            "entities_rewritten": entities_rewritten,
            # Non-null only when the stored project or domain spelling differs from the one supplied.
            "project_resolved": axis_report.get("project_resolved"),
            "domains_resolved": axis_report.get("domains_resolved") or None,
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
                # A successor that is itself superseded must say so. Otherwise the caller is sent from A to a stale B.
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
                _supersede_params = {"type": "supersede", "old_pg_id": pg_id, "new_pg_id": by}
                _require_outbox_type(_supersede_params)
                await conn.execute(
                    "INSERT INTO neo4j_outbox (pg_id, cypher_params) VALUES ($1, $2::jsonb)",
                    pg_id,
                    _supersede_params,
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
        vis_res = _validate_visibility_and_scope(body)
        if isinstance(vis_res, web.Response):
            return vis_res
        visibility, scope = vis_res
        if rating not in RETRO_RATINGS:
            return web.json_response(
                {"status": "error",
                 "message": (f"rating must be one of {sorted(RETRO_RATINGS)} "
                             "(outcome states — nuance belongs in the notes)")},
                status=400,
            )

        # This handler builds metadata from named fields, so a client domain would be dropped with no complaint. Say so: a retrospective inherits domains from the facts it grounds.
        if names_a_domain(body):
            return web.json_response(
                self._domain_on_judgement_rejection("retrospective"), status=400)

        # decision 276: rating reversed marks the decision superseded in both stores.
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
        if source_ref:
            metadata["source_ref"] = source_ref
        if body.get("elicited"):
            metadata["elicited"] = True
        grounded_ids = [
            g for g in (body.get("grounded_in") or [])
            if isinstance(g, int) and not isinstance(g, bool)
        ]
        # A retrospective without grounding is refused (a decision may be ungrounded); it is also how an ungrounded decision reaches topics.
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

        # Check the target exists before the embedding. A typo must not occupy the embedder just to 404; the later FOR SHARE is the real lock.
        async with self._acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM technical_docs WHERE id=$1", pg_id
            )
        if not exists:
            return web.json_response(
                {"status": "error", "message": f"No record found with pg_id={pg_id}"},
                status=404,
            )

        # Retrospectives mint no entities; a non-empty list is 400 (decision:1664).
        judgement_error = self._judgement_entities_error(metadata)
        if judgement_error is not None:
            self._count_refusal(judgement_error)
            return web.json_response(judgement_error, status=400)
        metadata.pop("new_entities", None)
        entities_rewritten = None

        # No save without a vector. The hash includes the target, so the same notes on two decisions stay two records.
        content_hash = hashlib.sha256(
            f"retrospective:{pg_id}:{date}:{rating}:{notes}".encode()
        ).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                embedding = await self._embed(notes, client)
        except RuntimeError as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=503)

        # Same per-entity locks as handle_save. They are the only uniqueness guarantee for Entity merges.
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
                    # Copy the decision's project onto the retrospective when the decision has one.
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
                        agent_id, scope, visibility,
                    )
                    retro_pg_id = row["id"]

                    grounded_typed = await self._resolve_typed_grounding(
                        conn, grounded_ids, metadata.get("grounded_roles") or {}
                    )

                    # A retrospective is a judgement too, so the facts it cites can inherit its project.
                    await self._promote_grounded_parked_facts(
                        conn, grounded_typed, agent_id, retro_pg_id
                    )

                    # Outbox row under the retrospective's own pg_id. v 2 selects the node projection; target_pg_id keys the insight triggers.
                    _retro_outbox = {
                            "v": 2,
                            "type": "retrospective",
                            "target_pg_id": pg_id,
                            "retrospective": retro_payload
                                              | ({"superseded": True} if is_reversal else {}),
                            "content_snippet": notes[:200],
                            "source": agent_id,
                            "agent_id": agent_id,
                            # Omit entities. A retrospective carries none and never wrote MENTIONS.
                            "source_ref": source_ref,
                            "fact_kind": fact_kind_from_source_ref(source_ref),
                            "grounded_in": grounded_ids,
                            "grounded": grounded_typed,
                    }
                    _require_outbox_type(_retro_outbox)
                    await conn.execute(
                        "INSERT INTO neo4j_outbox (pg_id, cypher_params) VALUES ($1, $2::jsonb)",
                        retro_pg_id,
                        _retro_outbox,
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
            # S-6 (security review fact:1412) — see handle_save's identical field.
            "entities_rewritten": entities_rewritten,
        })

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
            # Keep the row read inside the guard. A raise while reading would escape the expansion this guard exists to contain.
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

    # Judgement labels whose belonging is derived. A fact already has its own edges; a summary has none.
    _DERIVED_BELONGING_LABELS = (ONT.decision, ONT.retrospective)

    async def _derived_belonging(self, session, pg_ids: list) -> dict:
        """`{pg_id: {"project": name, "domains": [...]}}` for the JUDGEMENTS in
        `pg_ids` — computed on read, never stored.

        `decision:1736` stopped materialising a judgement's belonging: a
        decision and a retrospective carry only the sections their operator
        asserted on them, and nothing writes an inherited edge any more. That
        answer did not stop existing — it moved to the read side, and this is
        where a search hit picks it up. The Cypher and every rule it enforces
        live in `derived_belonging_cypher`.

        ⚠ ONE ROUND TRIP FOR THE WHOLE BATCH, not one per hit. The obvious
        shape — a bounded query per judgement — is the N+1 that
        `_expand_graph_context_batch` was written to remove; re-introducing it
        beside the fix would have undone it for exactly the hits (decisions and
        their verdicts) a lifecycle-aware search returns most of.

        Rows come back only for anchors that ARE judgements and DO resolve to a
        project, so a fact's pg_id in the list simply produces nothing. Degrades
        to `{}` on any failure, exactly like the expansion it enriches: graph
        context enriches a search, it never fails one.
        """
        if not pg_ids:
            return {}
        out: dict = {}
        try:
            result = await session.run(
                derived_belonging_cypher(), pg_ids=list(pg_ids),
            )
            async for rec in result:
                out[rec["anchor_pg_id"]] = {
                    "project": rec["project"],
                    "domains": list(rec["domains"] or []),
                }
        except Exception as exc:
            log.warning(
                "graph context: derived belonging failed for %d anchor(s) — "
                "hits keep their graph context without it: %s", len(pg_ids), exc,
            )
            return {}
        return out

    @staticmethod
    def _belonging_entry(belonging: dict) -> dict:
        """The additive expansion entry a judgement hit carries.

        ⚠ IT IS NOT AN EDGE, and it deliberately does not pretend to be one: no
        `rel_type`, no `direction`, no neighbour. A consumer that walks the
        expansion looking for relations skips it; one that wants to know where
        the record belongs reads it by name. Appended AFTER the capped edge
        list, so no edge is displaced by it — the cap governs edges, and this is
        not one.
        """
        return {"belonging": belonging}

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

        ⭐ ONE ENTRY IS NOT AN EDGE. When the anchor is a Decision or a
        Retrospective the list ends with ``{belonging: {project, domains}}`` —
        where that record belongs, DERIVED on read rather than read off its own
        edges, because nothing writes a judgement's inherited sections any more
        (`decision:1736`). A fact never carries it: its belonging IS its own
        bare edges, and they are already in the list above.
        """
        ctx: list[dict] = []
        anchor_where = " OR ".join(f"n:{lbl}" for lbl in anchor_labels)
        try:
            result = await session.run(
                f"MATCH (n {{pg_id: $pg_id}}) WHERE {anchor_where}"
                " OPTIONAL MATCH (n)-[r]-(related)"
                # Pull genuinely-referenced entity alias siblings (decision:890); stray Decision ALIASES edges do not surface.
                f" OPTIONAL MATCH (related)-[:{ONT.aliases}]-(al:{ONT.entity})"
                f"   WHERE EXISTS {{"
                f"     MATCH (related)<-[:{ONT.entity_link}]-(m)"
                f"     WHERE m.pg_id IS NOT NULL AND coalesce(m.superseded,false) = false"
                f"   }}"
                " WITH n, r, related, labels(related) AS labels,"
                "      collect(DISTINCT al.name) AS aliases"
                # Cap keeps provenance then typed relations then MENTIONS; lifecycle edges rank by type, not asserted_by (legacy inherited MENTIONS still in the graph) (decision:1736).
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
                # decision 909: the neighbor already carries fact_kind and source_ref, so no second query. A decision's confidence stays in Postgres and is attached by pg_id.
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
        # decision:1736: a judgement hit also carries where it belongs, derived. Facts already have their own edges, and a summary-only expansion skips this.
        if any(lbl in self._DERIVED_BELONGING_LABELS for lbl in anchor_labels):
            belonging = await self._derived_belonging(session, [pg_id])
            if pg_id in belonging:
                ctx.append(self._belonging_entry(belonging[pg_id]))
        return ctx

    # Ratings whose verdict the reader must weigh. validated and pending stand as written, so the word alone is enough.
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

        Carries the same `{belonging: ...}` entry as the single-anchor form for
        every judgement anchor — see there — in one further round trip for the
        whole batch, never one per hit.
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
                # Rank lifecycle edges by type. Legacy inherited MENTIONS still exist, so sorting on asserted_by first buried HAD_OUTCOME.
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
                # decision 909: same neighbor projection as the single-anchor query, batched. Decision payload stays in Postgres.
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
        # One more round trip for the whole batch, not one per hit. Fact anchors come back with no row.
        if any(lbl in self._DERIVED_BELONGING_LABELS for lbl in anchor_labels):
            for pid, belonging in (
                    await self._derived_belonging(session, list(pg_ids))).items():
                if pid in out:
                    out[pid].append(self._belonging_entry(belonging))
        return out

    async def _resolve_search_filters(
        self, project: str | None, domains: list | None,
    ) -> tuple:
        """`(project spellings, domain spellings, filters_resolved)` for one search.

        THE READ SIDE OF THE SAME RESOLUTION INGRESS DOES, and it exists because
        the two sides had drifted into asking different questions. A save that
        names a project by a retired or differently-punctuated spelling is stored
        under the canonical one; a SEARCH naming it the same way matched the
        literal string and therefore matched nothing — so the corpus answered
        "there is nothing here" to a filter that was merely spelled the way the
        asker's folder is spelled. One resolution, both directions.

        What comes back:

        * the spellings to bind into the predicate — the canonical, every active
          alias of it, and every registered variant sharing its key;
        * `filters_resolved`, the response's account of what the server did with
          what it was given. Additive, and present only when a filter was
          supplied, so an unfiltered search's body is unchanged.

        ⛔ AN UNRESOLVABLE VALUE IS NOT AN ERROR AND IS NOT WIDENED. It degrades
        to the literal string — exactly what the filter did before this existed
        — with `canonical: null` saying so. The read path never blocks on
        registry state: a searcher is allowed to probe for a name that is not
        registered, and telling them "unknown project" would make search a
        second gate on a registry only the write path is supposed to enforce.

        ⚠ BUT A REGISTRY THAT COULD NOT BE READ IS A DIFFERENT EVENT, and it used
        to look identical: both produced `canonical: null` and an answer computed
        from the literal string. One of those is the truth about the corpus and
        the other is the gateway saying it could not check — so a read failure
        now sets `filters_resolved.error` and increments a telemetry counter. The
        result is still served, because a degraded answer beats no answer; what
        it must not do is pass for an authoritative one.

        ⚠ DOMAINS RESOLVE ONLY INSIDE A RESOLVED PROJECT. A section is
        identified by (project, name) and by nothing else, so with no project
        filter — or one that resolves to nothing — there is no scope to look a
        section up in, and every supplied domain stays the literal string. That
        is the same absence `domain_axis` calls load-bearing: the one way this
        axis reproduces the project axis' original defect is by letting a name
        answer on its own.
        """
        if not project and not domains:
            return None, None, None

        resolved: dict = {}
        project_values = None
        canonical = None
        # A failed registry read degrades to the literal string, which looks like an unregistered name. The response says which registry failed.
        errors: list = []
        if project:
            registered, aliases, err = await self._project_spellings(project)
            if err:
                errors.append(err)
            canonical, _via = resolve_axis_value(project, registered, aliases)
            project_values = (expand_axis_spellings(canonical, registered, aliases)
                              if canonical is not None else [project])
            resolved["project"] = {
                "supplied": project,
                "canonical": canonical,
                "matched": project_values,
            }

        domain_values = None
        if domains:
            # A reader degrades instead of 500. An unresolved domain looks like an unregistered section, so the miss is counted and reported.
            try:
                project_id = (await self._project_identity(canonical)
                              if canonical is not None else None)
            except ProjectIdentityUnavailable as exc:
                errors.append(self._note_registry_read_failure("project", exc))
                project_id = None
            d_registered, d_aliases, d_err = await self._domain_spellings(
                project_id, domains)
            if d_err:
                errors.append(d_err)
            entries: list = []
            values: list = []
            for name in domains:
                d_canonical, _v = resolve_axis_value(name, d_registered, d_aliases)
                matched = (expand_axis_spellings(d_canonical, d_registered, d_aliases)
                           if d_canonical is not None else [name])
                entries.append({"supplied": name, "canonical": d_canonical,
                                "matched": matched})
                # One flat array: ?| is OR over the whole set, so every spelling of every requested section goes in together.
                for spelling in matched:
                    if spelling not in values:
                        values.append(spelling)
            domain_values = values
            resolved["domains"] = entries

        if errors:
            # fact:1314: a string naming the registry that failed, absent on the ordinary path, so it does not restructure the keys beside it.
            resolved["error"] = "; ".join(dict.fromkeys(errors))
        return project_values, domain_values, resolved

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

        # A named project or domain is a filter, not query text. An unknown name is not refused on the read path; it matches nothing.
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
            # Cap at ingress. A silent drop would let a partial filter's empty result look authoritative (PR 235).
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

        # Resolve typed names once, for every query below. The cap applies to the supplied list, before the registry adds spellings.
        project_values, domain_values, filters_resolved = \
            await self._resolve_search_filters(project, domains_filter)

        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            try:
                q_vec = await self._embed(query, client)
            except RuntimeError:
                q_vec = None

            if q_vec is None:
                # Keyword fallback when the embedder is down. The axis filter still applies.
                pattern = _ilike_contains(query)
                args: list = [pattern, limit]
                vis_sql, vis_params = _visibility_filter(viewer, scope, len(args) + 1)
                args.extend(vis_params)
                scope_sql = ""
                if scope:
                    args.append(scope)
                    scope_sql = f"AND scope = ${len(args)}"
                axis_sql, axis_params = _axis_filter_predicate(
                    len(args) + 1, project_values, domain_values, since_dt)
                args.extend(axis_params)
                async with self._acquire() as conn:
                    rows = await conn.fetch(
                        f"""
                        SELECT id, content, metadata FROM technical_docs
                        WHERE NOT superseded
                          AND (content ILIKE $1 ESCAPE '\\' OR metadata::text ILIKE $1 ESCAPE '\\')
                          AND {vis_sql} {scope_sql} {axis_sql}
                        LIMIT $2
                        """,
                        *args,
                    )
                return web.json_response(_with_filters_resolved({
                    "status": "success",
                    "fallback": "keyword",
                    "results": [
                        _keyword_hit(r, query)
                        for r in rows
                    ],
                }, filters_resolved))

            async with self._acquire() as conn:
                # decision 276: the nearest active insight is a separate query from the thematic summary. The same visibility predicate applies, or a private fact's narrative would leak.
                vis_t3, vis_t3_params = _visibility_filter(viewer, scope, 2)
                # Computed once and reused. Each Tier-3 query starts its own $1, so the same fragment applies to all three.
                t3_axis_sql, t3_axis_params = _axis_filter_predicate(
                    2 + len(vis_t3_params), project_values, domain_values, since_dt)
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

                # If migration 006 is missing, fall back to the unsupervised query so search still works.
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
                    # kind may predate migration 006; superseded must not be dropped. This branch used to return the retired summary the guarded query refused.
                    summary = await conn.fetchrow(
                        "SELECT id, content, metadata, source_pg_ids FROM community_summaries"
                        f" WHERE NOT superseded AND {vis_t3} {t3_axis_sql}"
                        " ORDER BY embedding <=> $1::vector LIMIT 1",
                        str(q_vec), *vis_t3_params, *t3_axis_params,
                    )

                # Retrieve pool is a floor (max(limit, SEARCH_CANDIDATE_FLOOR)), never a silent 20-cap the caller cannot see.
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
                    len(args) + 1, project_values, domain_values, since_dt)
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
                    # Fail closed on superseded. created_at may be absent on an old install; dropping the guard on any error served retired rows.
                    candidates = await conn.fetch(
                        f"""
                        SELECT id, content, metadata FROM technical_docs
                        WHERE NOT superseded AND {vis_sql} {scope_sql} {axis_sql}
                        ORDER BY embedding <=> $1::vector LIMIT $2
                        """,
                        *args,
                    )
                    # Known gap: since also needs created_at, so on this old schema it raises again, uncaught.

            if not candidates:
                return web.json_response(_with_filters_resolved(
                    {"status": "success", "results": []}, filters_resolved))

            ids      = [r["id"]       for r in candidates]
            contents = [r["content"]  for r in candidates]
            metas    = [_coerce_jsonb_obj(r["metadata"]) for r in candidates]
            # .get: tolerant of pre-migration schemas (and test stubs) where the
            # created_at column is absent — recency simply degrades to off.
            createds = [r.get("created_at") for r in candidates]

            # Rerank goes to RERANK_URL; Tier-3 narratives are scored candidates, not reserved top slots.
            t3_rows = [r for r in (insight, summary) if r is not None]
            n_t3 = len(t3_rows)

            # Summaries come first by index so a hit can be mapped back, not by rank. Clamp what the reranker scores; search still returns the full text.
            rerank_docs = [
                prefix_rerank_doc(query, r["content"] or "") for r in t3_rows
            ] + [
                prefix_rerank_doc(query, _rerank_doc_text(c, m, t))
                for c, m, t in zip(contents, metas, createds)
            ]
            # Measure chars/docs actually sent (post-clamp, both ranked and fallback) so capacity can separate document length from fixed overhead (fact:1441).
            rerank_payload_chars = sum(len(d) for d in rerank_docs)
            rerank_payload_docs = len(rerank_docs)
            # Cumulative rerank payload totals; searches-measured is successes+failures, not a third counter.
            self._rerank_payload_chars_total += rerank_payload_chars
            self._rerank_payload_docs_total += rerank_payload_docs
            # Track the max beside the sum. A mean cannot show the worst payload a capacity signal needs.
            if rerank_payload_chars > self._rerank_payload_chars_max:
                self._rerank_payload_chars_max = rerank_payload_chars
            reranked = False
            _rr_t0 = time.monotonic()
            try:
                rr = await client.post(
                    RERANK_URL,
                    # llama.cpp honours top_n and ignored top_k. Both are sent; neither is trusted. The slice below enforces the limit.
                    json={"query": prefix_rerank_query(query), "documents": rerank_docs,
                          "top_n": limit, "top_k": limit},
                    # Derived from the payload, never constant — a constant
                    # under-provisions exactly the large sets that need it most.
                    timeout=rerank_ceiling(rerank_docs),
                )
                rr.raise_for_status()
                ranked = rr.json()["results"]
                reranked = True
                safe(lambda: self._rerank_ring.record(
                    (time.monotonic() - _rr_t0) * 1000.0,
                    payload_chars=rerank_payload_chars))
            except Exception as exc:
                safe(lambda: self._rerank_ring.record_error())
                # Rerank fallback is vector order (logged/counted); a dropped connection adds the OOM-kill sentence, a timeout does not.
                is_dropped_connection = isinstance(
                    exc,
                    (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError),
                )
                msg = ("rerank failed (%s: %s) — serving vector order "
                       "for %d candidates")
                if is_dropped_connection:
                    msg += (
                        " — a dropped connection mid-rerank on a "
                        "memory-constrained host is most often the kernel "
                        "OOM-killing the reranker: check your reranker "
                        "container (llama-reranker or llama-reranker-gpu) — "
                        "`docker inspect <name> --format '{{.State.OOMKilled}} "
                        "{{.RestartCount}}'` — and the kernel log (dmesg), and "
                        "see the capacity record on authenticated /health "
                        "for this host's derived limits"
                    )
                log.warning(msg, type(exc).__name__, scrub_url_credentials(str(exc)), len(rerank_docs))
                self._rerank_failures += 1
                self._rerank_fallback_last_ts = datetime.now(timezone.utc).isoformat()
                # Unranked fallback omits Tier-3: vector distances are not comparable across tables, so narratives get no guessed slot.
                ranked = [
                    {"index": i + n_t3, "relevance_score": None}
                    for i in range(len(candidates))
                ]
            else:
                self._rerank_successes += 1
            # Enforce the caller's limit here. A server that ignores its truncation parameter must not inflate the result.
            ranked = ranked[:limit]

        # decision 384: flag a returned narrative whose sources were superseded or reversed. The join is the check; nothing is rewritten here.
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
                # A database fault must not read as no superseded sources. The annotation is advisory, and the miss is logged.
                log.warning(
                    "stale_sources annotation degraded (%s: %s) — "
                    "%d source ids unchecked",
                    type(e).__name__, e, len(prov_ids),
                )
                stale_map = {}  # column missing (pre-013) — degrade to no annotation

        def _stale_sources(source_pg_ids, meta) -> list[dict]:
            # Hide a pair the operator already held. A later supersession of a different source is a new pair and still surfaces.
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

        # Annotate insight stale_summaries at read time from community_summaries via summary_ids, never technical_docs (decision:1207).
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
                # A database fault must not read as no superseded summaries. The annotation is advisory, and the miss is logged.
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

        # Build final in the reranker's order. No tier is inserted ahead of that ranking or appended after it.
        final: list[dict] = []

        async with self._neo4j.session() as session:
            # Batched summary→sources graph context; keep summary vs fact id maps apart (independent sequences).
            surviving_t3 = [t3_rows[h["index"]] for h in ranked
                            if h["index"] < n_t3]
            summary_ctx = await self._expand_graph_context_batch(
                session,
                [r.get("id") for r in surviving_t3 if r.get("id") is not None],
                (ONT.community_summary,),
            )

            # Anchor every record label. Decisions and retrospectives get graph context too, not only facts.
            fact_pg_ids = [ids[h["index"] - n_t3] for h in ranked
                           if h["index"] >= n_t3]
            fact_ctx = await self._expand_graph_context_batch(
                session, fact_pg_ids, (ONT.fact, ONT.decision, ONT.retrospective)
            )

            for hit in ranked:
                raw_score = hit["relevance_score"]

                if hit["index"] < n_t3:
                    # This narrative earned its rank, so it carries a score. A null score used to mean it was never ranked.
                    row  = t3_rows[hit["index"]]
                    meta = row["metadata"]
                    rtype = summary_record_type(meta)
                    res = {
                        # insight_summary names the kind, not a reserved position. It is ranked on the same scale as the other hits.
                        "tier": ("insight_summary" if rtype == "insight"
                                 else "community_summary"),
                        # Same field name, a different id sequence from the fact tier. record_type and ref say which.
                        "record_type": rtype,
                        "ref": make_ref(rtype, row.get("id")),
                        # .get so a stub without the id column still works. The id is the CommunitySummary node key the walk uses.
                        "pg_id": row.get("id"),
                        "content": row["content"],
                        "ranked": reranked,
                        # fact:1441: the same per-search payload numbers on every row, matching the fact-tier branch.
                        "rerank_payload_chars": rerank_payload_chars,
                        "rerank_payload_docs": rerank_payload_docs,
                        "score": raw_score,
                        "score_normalized": (_sigmoid(raw_score)
                                             if raw_score is not None else None),
                        "matched_entities": [],
                        "metadata": meta,
                        # The facts this narrative was synthesised from, so the caller can open them.
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
                # tier is where the hit came from; record_type is what it is. The fact tier also carries decisions and retrospectives.
                rtype = doc_record_type(metas[idx])
                final.append({
                    "tier": "fact",
                    "pg_id": pg_id,
                    "record_type": rtype,
                    "ref": make_ref(rtype, pg_id),
                    "content": contents[idx],
                    # False means vector order and no score. A fabricated 1.0 made a dead reranker look confident.
                    "ranked": reranked,
                    # Search-level rerank chars/docs (same on every row; fallback still carries what would have been sent) (fact:1441).
                    "rerank_payload_chars": rerank_payload_chars,
                    "rerank_payload_docs": rerank_payload_docs,
                    "score": raw_score,
                    "score_normalized": (_sigmoid(raw_score)
                                         if raw_score is not None else None),
                    "matched_entities": _matched_entities(query, metas[idx]),
                    "metadata": metas[idx],
                    "created_at": (createds[idx].isoformat()
                                   if createds[idx] is not None else None),
                    "graph_context": ctx,
                })

        # Attach each returned decision's current verdict in place (does not inflate limit) (decision:1109).
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
            # Fetch the retrospective text for refined, mixed, and reversed. The rating word does not carry the reasoning.
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
                    # decision 822: a qualified ref, because a bare integer resolves against the wrong table.
                    "ref": make_ref("retrospective", state["retrospective_pg_id"]),
                    "retrospective_pg_id": state["retrospective_pg_id"],
                }
                text = notes.get(state["retrospective_pg_id"])
                if text is not None:
                    entry["retrospective_content"] = text
                r["lifecycle"] = entry

        # Latest-retro-as-verdict: same-decision retrospectives newest-first.
        final = _order_retros_latest_first(final)
        return web.json_response(_with_filters_resolved(
            {"status": "success", "results": final}, filters_resolved))

    # ── POST /memory/graph ────────────────────────────────────────────────────

    async def handle_graph(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "request body must be JSON"}, status=400
            )

        cypher = body.get("cypher", "")
        if "params" in body:
            params = body["params"]
            if not isinstance(params, dict):
                return web.json_response(
                    {"status": "error", "message": "params must be an object/dict"},
                    status=400,
                )
        else:
            params = {}

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

        _t0 = time.monotonic()
        try:
            async def _read_tx(tx):
                result = await tx.run(cypher, **params)
                return await result.fetch(GRAPH_QUERY_ROW_CAP + 1)

            async with self._neo4j.session(default_access_mode="READ") as session:
                records = await session.execute_read(_read_tx)
            safe(lambda: self._neo4j_ring.record((time.monotonic() - _t0) * 1000.0))
        except ClientError as exc:
            # A rejected query is the caller's. Counting it with tx_failures would make a typo look like an outage.
            safe(lambda: setattr(self, "_cypher_rejected_total",
                                 self._cypher_rejected_total + 1))
            # neo4j.exceptions.ClientError is 400 cypher_rejected (caller's invalid Cypher), not 500 query failed.
            log.info("graph query REJECTED by neo4j for cypher=%r: %s",
                     cypher[:120], exc)
            return web.json_response(
                {
                    "status": "error",
                    "error": "cypher_rejected",
                    "message": str(exc)[:300],
                },
                status=400,
            )
        except Exception as exc:
            safe(lambda: setattr(self, "_neo4j_tx_failures_total",
                                 self._neo4j_tx_failures_total + 1))
            log.error("graph query error for cypher=%r: %s", cypher[:120], exc, exc_info=True)
            return web.json_response({"status": "error", "message": "query failed"}, status=500)

        if len(records) > GRAPH_QUERY_ROW_CAP:
            return web.json_response(
                {
                    "status": "error",
                    "error": "graph_row_cap_exceeded",
                    "message": f"query returned more than {GRAPH_QUERY_ROW_CAP} rows",
                },
                status=400,
            )

        records = [
            r.data() if callable(getattr(r, "data", None)) and not isinstance(r, dict) else dict(r)
            for r in records
        ]

        # json.dumps TypeError/ValueError is our coercion bug, not a Neo4j tx failure.
        try:
            body = json.dumps({"status": "success", "records": _json_safe(records)})
        except (TypeError, ValueError) as exc:
            log.error("graph query result failed to serialize for cypher=%r: %s",
                      cypher[:120], exc, exc_info=True)
            return web.json_response({"status": "error", "message": "query failed"}, status=500)

        return web.json_response(text=body)

    # ── GET /memory/status/{pg_id} ────────────────────────────────────────────

    async def handle_status(self, request: web.Request) -> web.Response:
        """Full record lineage — "what happened to pg_id N?". The coordinator owns both
        backends and does the joins (ADR-014); the thin client only calls the gateway.
        Returns record state (type / created_at / superseded / grounded_in), the
        in-flight dream-cycle stamps, and what it consolidated INTO — which summary or
        insight (the FORM, via the source_pg_ids reverse lookup) and the coarse
        fact→summary latency when both timestamps exist. Backwards-compatible: the old
        `neo4j`/`retries`/`applied_at` fields are retained."""
        # A qualified ref such as fact:816 or summary:87, or a bare integer. The bare form still means technical_docs.
        try:
            record_type, pg_id = parse_ref(request.match_info["pg_id"])
        except ValueError as exc:
            return web.json_response(
                {"status": "error",
                 "message": f"reference must be an integer or <type>:<id> ({_short(exc)})"},
                status=400,
            )

        # The sequences are independent, so a summary id looked up in technical_docs would succeed and return the wrong record.
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
                f" FROM neo4j_outbox WHERE pg_id = $1 AND {_DREAM_CYCLE_OUTBOX_TYPE}"
                " ORDER BY id DESC LIMIT 1", pg_id,
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

        # The id resolved, but not to the type the caller named. Returning the row would be a confident wrong answer.
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
            # Thematic domain plus insight domains both exposed (thematic domains degrades to a one-element list).
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
        """Operational telemetry snapshot — THE NUMBERS (the telemetry contract,
        decision:1785): counters, gauges, percentiles and censuses, each with
        the limit stated next to it. Every section is computed independently so
        a partial backend failure still returns whatever the others can.

        This endpoint is the single read-only source of truth for the pipeline:
        a read-scoped client (e.g. the Shared Memory Monitor) can render the
        whole live dashboard from here without any direct Postgres or Neo4j
        credentials — the coordinator owns both backends and does the joins.

        v0.9.74: the whole payload is CACHED for TELEMETRY_CACHE_S and served
        stale inside that window. The monitor re-fetches live on a 30 s browser
        timer while also polling on its own 600 s loop, so without a cache two
        builds could overlap; `generated_at` states when the served payload was
        actually built, and `timestamp` when it was served.
        """
        snap = await self._telemetry_cached()
        # Strip dropped keys on the response, never on snap. The cache is shared, and strip_dropped returns a fresh object.
        return web.json_response(
            {"status": "success",
             "telemetry": strip_dropped(snap, TELEMETRY_CONTRACT)})

    async def _telemetry_cached(self) -> dict:
        """TTL cache + single-flight around ``_build_telemetry``.

        Same shape as the /health probe cache, for the same reason: the TTL
        alone bounds SEQUENTIAL cost only, so N concurrent misses arriving
        together would each run a full build. The second-and-later caller
        re-checks the cache after taking the lock and finds it fresh — that
        re-check IS the coalescing, not a redundant guard.
        """
        now = time.monotonic()
        cached = self._telemetry_cache["snap"]
        if cached is not None and now - self._telemetry_cache["ts"] < TELEMETRY_CACHE_S:
            return {**cached, "timestamp": datetime.now(timezone.utc).isoformat()}
        async with self._telemetry_lock:
            now = time.monotonic()
            cached = self._telemetry_cache["snap"]
            if cached is not None and now - self._telemetry_cache["ts"] < TELEMETRY_CACHE_S:
                return {**cached, "timestamp": datetime.now(timezone.utc).isoformat()}
            snap = await self._build_telemetry()
            self._telemetry_cache = {"snap": snap, "ts": now}
            return {**snap, "timestamp": datetime.now(timezone.utc).isoformat()}

    async def _build_telemetry(self) -> dict:
        """Build the telemetry payload from scratch. See ``handle_telemetry``."""
        # decision:1032: generated_at is stamped here and timestamp at serve time. The age is their difference, so it is not stored again.
        snap: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}

        # Postgres — outbox status, doc + summary counts
        try:
            async with self._acquire() as conn:
                outbox = await conn.fetch(
                    "SELECT status, count(*) AS n FROM neo4j_outbox GROUP BY status"
                )
                # Age of the oldest permanently failed row. A growing value means Neo4j writes are being abandoned.
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
                # Moved in 0.9.75. This census omits a zero status, so outbox.failed disappeared exactly when it was zero.
                "outbox": {r["status"]: r["n"] for r in outbox},
                "outbox_failed_oldest_age_seconds": failed_age,
                "community_summaries": {
                    "total": summ["total"],
                    "superseded": summ["superseded"],
                    "insight": summ["insight"],
                },
                # These climb before saturation becomes a 503. asyncpg exposes both, so neither is derived twice.
                **self._pool_gauges(),
                # A Postgres fact. It was on /health only because that is where the startup probe's result was surfaced.
                "pgvector": {
                    "version": self.pgvector_version,
                    "iterative_scan": bool(self.hnsw_iterative_scan),
                },
            }
        except Exception as exc:
            snap["postgres"] = {"error": str(exc)}

        # decision:1032: every outbox status is present, and latency comes from created_at and applied_at rather than a ring that resets on restart.
        try:
            snap["outbox"] = await self._outbox_telemetry()
        except Exception as exc:
            snap["outbox"] = {"error": str(exc)}

        # Neo4j — REM/NREM backlog for facts and decisions
        _nj = self._neo4j_ring.snapshot()
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
                # A record at the attempt cap is still rem_pending. Without this gauge that looks like waiting, not given up.
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
                # Superseded rows are excluded from REM's candidacy query, so counting them here inflates a backlog nothing can clear.
                "facts_rem_pending":    sum(r["n"] for r in facts if not r["rem"] and not r["superseded"]),
                "facts_unconsolidated": sum(r["n"] for r in facts if r["rem"] and not r["con"]),
                "decisions_total":      sum(r["n"] for r in decisions),
                "decisions_rem_pending": sum(r["n"] for r in decisions if not r["rem"] and not r["superseded"]),
                # Excluded from REM's queue until n.rem_attempts is reset. Non-zero means enrichment is dropping records.
                "rem_dead_lettered":    sum(r["n"] for r in attempts if r["a"] >= _cap),
                # Pending records carrying at least one failed attempt.
                "rem_failing":          sum(r["n"] for r in attempts if 0 < r["a"] < _cap),
                "rem_max_attempts":     _cap,
                # decision 890: fairness gauge for the batch-versus-solo yield. It reads 0 until a solo backlog re-exercises that path.
                "rem_passed_over_total": sum(r["n"] * r["p"] for r in attempts),
                "rem_starved_pending":  sum(r["n"] for r in attempts
                                            if r["p"] >= REM_STARVED_THRESHOLD),
                # Neo4j telemetry is one snapshot (p50/p95 share a window); cypher_rejected is the caller's fault, tx_failures is ours.
                **{f"query_{k}": v for k, v in _nj.items()
                   if k in ("p50_ms", "p95_ms", "window")},
                "cypher_rejected_total": self._cypher_rejected_total,
                "tx_failures_total": self._neo4j_tx_failures_total,
            }
        except Exception as exc:
            snap["neo4j"] = {"error": str(exc)}

        # The backlog numbers that used to sit under neo4j because that is where the query ran, plus what rem_timing can already answer.
        try:
            snap["rem"] = await self._rem_telemetry()
        except Exception as exc:
            snap["rem"] = {"error": str(exc)}

        # Row counts and ingress refusals. Those gates shipped uncounted, so a refusal was visible only to the caller who got it.
        try:
            snap["registry"] = self._registry_telemetry()
        except Exception as exc:
            snap["registry"] = {"error": str(exc)}

        # NREM backlog is pending (project, domain) cycles from graph edges, served from the 60s refresher (as_of stamped).
        dep = self._dependency_health
        nrem = dep.get("nrem")
        if isinstance(nrem, dict):
            snap["nrem"] = {**nrem, "as_of": dep.get("as_of")}
        else:
            snap["nrem"] = {"error": "not yet computed", "as_of": dep.get("as_of")}

        # Dashboard distributions. Surfaced here so the monitor does not need its own Postgres connection.
        try:
            snap["breakdown"] = await self._metadata_breakdown()
        except Exception as exc:
            snap["breakdown"] = {"error": str(exc)}

        # Fragmentation and alias coverage. The pairwise cosine over-merge stays off this path; only the aggregates are here.
        try:
            snap["entity_graph"] = await self._entity_graph()
        except Exception as exc:
            snap["entity_graph"] = {"error": str(exc)}

        # Labels and relationship types outside the ontology. Ingress now refuses them and cannot remove the ones already stored.
        try:
            snap["compliance"] = await self._graph_compliance()
        except Exception as exc:
            snap["compliance"] = {"error": str(exc)}

        # decision 928: REM's label_mismatch verdict, which nothing else read. Non-zero means a writer is using the wrong label.
        try:
            snap["graph_integrity"] = await self._graph_integrity()
        except Exception as exc:
            snap["graph_integrity"] = {"error": str(exc)}

        # Fresh rollup of consolidation_runs. /health reads the cached subset so a probe stays off the database.
        try:
            snap["consolidation"] = await self._consolidation_telemetry()
        except Exception as exc:
            snap["consolidation"] = {"error": str(exc)}

        # fact:1189: a lone backlog number hides why a row was dropped. decision:1121: below_density and out_of_scan must not look like an open row. decision:1181: nothing else reads insight-kind ledger rows.
        try:
            snap["refold_ledger"] = await self._refold_ledger_telemetry()
        except Exception as exc:
            snap["refold_ledger"] = {"error": str(exc)}

        # decision 559: which required fields were present at first write, and which captured fields were never projected.
        try:
            snap["spine"] = await self._spine_telemetry()
        except Exception as exc:
            snap["spine"] = {"error": str(exc)}

        # decisions 568/570/571: REM service and contention per model, plus the NREM cycle window. Not fact-to-summary: that interval is gate-dominated (fact 567).
        try:
            snap["latency"] = await self._latency_telemetry()
        except Exception as exc:
            snap["latency"] = {"error": str(exc)}

        # Read the cached probe so telemetry does not shell out to nvtop. unknown stays unknown, not a false idle.
        snap["inference_busy"] = self._consolidation_health.get("inference_busy", "unknown")

        # Rerank success/fallback counters (in-process); fallback still answers 200 so this is the only outside-the-log signal (fact:1314).
        snap["rerank_successes_total"] = self._rerank_successes
        snap["rerank_fallbacks_total"] = self._rerank_failures
        snap["rerank_fallbacks_last_ts"] = self._rerank_fallback_last_ts

        # Registry-read-failure counters: a failed lookup still 200s and looks like an unknown name.
        snap["axis_registry_read_failures_total"] = self._axis_registry_read_failures
        snap["axis_registry_read_failures_last_ts"] = \
            self._axis_registry_read_failure_last_ts

        # Cumulative rerank chars/docs this process; divide for mean chars/doc (fact:1441).
        snap["rerank_payload_chars_total"] = self._rerank_payload_chars_total
        snap["rerank_payload_docs_total"] = self._rerank_payload_docs_total
        # The worst payload this process has seen. A sum-and-count mean cannot show it, and the max only moves up.
        snap["rerank_payload_chars_max"] = self._rerank_payload_chars_max

        # In-process counters, so no try. Detail stays in the credential log. Also emitted as llm.faults until 0.9.75 removes this copy.
        snap["llm_faults"] = _llm_faults_snapshot()
        snap["credentials"] = {
            **_credentials_snapshot(),
            # The limit next to the number it bounds — the contract's own rule.
            "token_verify_warn_per_min": TOKEN_VERIFY_WARN_PER_MIN,
        }

        # No I/O, so the calls need no try. The assembly is still guarded: a telemetry section must not 500 the endpoint.
        snap["encoders"] = safe(self._encoders_telemetry, default={"error": "unavailable"})
        snap["gateway"] = safe(self._gateway_telemetry, default={"error": "unavailable"})
        snap["clients"] = {"versions_seen": dict(_client_versions_seen)}

        # llm/capability/capacity/config come from hive via telemetry_extras_provider (import cycle otherwise).
        if self.telemetry_extras_provider is not None:
            extras = safe(self.telemetry_extras_provider, default=None)
            if isinstance(extras, dict):
                snap.update(extras)

        # Moved off /health. Read the cached snapshot /health already carries, not a second probe.
        snap["gpu_probe"] = self._consolidation_health.get("gpu_probe")
        snap["axes"] = {
            "project_identity": self._consolidation_health.get("project_identity"),
            "domain_identity": self._consolidation_health.get("domain_identity"),
        }

        return snap

    # ── Spine coverage (decision 559) ─────────────────────────────────────────

    async def _spine_telemetry(self) -> dict:
        """Spine-coverage telemetry — the data behind the first-write-quality push.
        Three families as cheap Postgres aggregates (the monitor samples over time
        for the trend): (A) required-field completeness + the elicited rate — an
        elicited null is a deliberate choice, so completeness is read *among
        elicited saves*; (B) emergent = metadata keys captured but NOT first-write
        projected (promotion candidates); (C) alias-adjudication volume (does the
        deterministic projection keep the graph clean). No hot-path counters."""
        # Already projected at first write, so they are not promotion candidates. rating and target_pg_id joined this set when retrospectives started projecting them.
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
            # Facts only. Counting every non-decision row absorbed retrospectives, which have different required fields and their own block below.
            frow = await conn.fetchrow(
                "SELECT count(*) AS n,"
                " count(*) FILTER (WHERE metadata ? 'source_ref') AS sref,"
                " count(*) FILTER (WHERE (metadata->>'elicited')='true') AS elicited"
                " FROM technical_docs"
                " WHERE (metadata->>'type' IS NULL"
                "        OR metadata->>'type' NOT IN ('decision', 'retrospective'))"
                "   AND NOT superseded"
            )
            # rating and target_pg_id sit at the top of metadata, not under a nested object, so the checks are direct.
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
            # Alternative-vector pending vs failing vs oldest_pending_age_s (coverage vs a stuck populator).
            try:
                arow = await conn.fetchrow(
                    "SELECT count(*) AS entries,"
                    " count(*) FILTER (WHERE embedding IS NOT NULL) AS embedded,"
                    " count(*) FILTER (WHERE embedding IS NULL) AS pending,"
                    " count(*) FILTER (WHERE embedding IS NULL AND attempts >= $1)"
                    "     AS failing,"
                    " count(DISTINCT decision_pg_id) AS decisions,"
                    # FILTER attaches to the aggregate. extract(...) FILTER is a syntax error, and the suite stubs this query so it would not catch it.
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
                # Report the error. A raised telemetry query would blank the whole rollup.
                alt_vectors = {"error": str(exc)}

        # Telemetry no longer carries `alias` (retired adjudication ledger); axis alias tables stay.

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
            # rating and target_pg_id are set by every write, so a miss is a regression. grounded_in is the signal: an outcome nothing backs.
            "retrospectives": {
                "total": rn,
                "rating_pct": pct(rrow["rating"], rn),
                "target_pg_id_pct": pct(rrow["target"], rn),
                "grounded_in_pct": pct(rrow["grounded"], rn),
                "elicited_pct": pct(rrow["elicited"], rn),
            },
            "emergent_unprojected_fields": emergent,
        }

    # ── Latency rollup (decisions 568/570/571) ────────────────────────────────

    async def _latency_telemetry(self) -> dict:
        """Latency rollup for the monitor, from the DURABLE technical_docs.rem_timing
        (survives outbox deletion — migration 019).

        ⚠ THIS IS A GATED SUBSET SINCE v0.9.66, and this docstring claimed the
        opposite for eight releases. REM was chosen as the anchor because it was
        UNGATED — every saved fact passed through it (fact 567) — but 0.9.66
        made short records SKIP THE MODEL entirely, so a row only carries
        rem_timing if its record was long enough to be sent. The percentiles
        below therefore describe the records REM actually ran on, which is a
        LONGER population than the corpus average, and they are not comparable
        with a pre-0.9.66 series. They still reflect model + hardware for that
        population, which is what the by_model axis is for.

        Two REM percentile pairs, grouped by model so the series is a
        model-evolution axis (decision 571):
          service_ms   = pure inference = MODEL + HARDWARE, load-invariant.
          contention_ms= queue behind a busy backend = CAPACITY (→ 0 as the pool grows).
        A row is included whenever it has a wall_ms, not only when it carries the
        llama.cpp-proprietary ``timings`` block: an OpenAI-compatible external backend
        (fact:1621) returns no such block, so service_ms/contention_ms are null for it
        while wall_ms/backend are populated — filtering on service_ms silently dropped
        every external model from by_model. Rendering is done by the pure
        ``render_rem_by_model`` so external backends now surface with wall-only or
        mixed timing (timing_source="wall"/"mixed"/"server") instead of vanishing.
        The NREM whole-cycle COMPUTE window (consolidation_runs started_at→finished_at)
        is kept ALONGSIDE (decision 568), never fact→summary — that end-to-end is
        density-gate-dominated and survivorship-biased, an erroneous latency (fact 567).
        p50/p95 via percentile_cont; each block independent so one failure spares the rest."""
        def _r(v):
            return round(float(v), 1) if v is not None else None

        out: dict = {}
        async with self._acquire() as conn:
            # REM: per-model service/contention/wall percentiles over the durable rows.
            rem_rows = await conn.fetch(
                "SELECT rem_timing->>'model' AS model, count(*) AS n,"
                "  count((rem_timing->>'service_ms')) AS n_service,"
                "  percentile_cont(0.5)  WITHIN GROUP (ORDER BY (rem_timing->>'service_ms')::float)    AS svc_p50,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY (rem_timing->>'service_ms')::float)    AS svc_p95,"
                "  percentile_cont(0.5)  WITHIN GROUP (ORDER BY (rem_timing->>'contention_ms')::float) AS con_p50,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY (rem_timing->>'contention_ms')::float) AS con_p95,"
                "  percentile_cont(0.5)  WITHIN GROUP (ORDER BY (rem_timing->>'wall_ms')::float)       AS wall_p50,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY (rem_timing->>'wall_ms')::float)       AS wall_p95,"
                "  max((rem_timing->>'batch_size')::int) AS max_batch,"
                "  mode() WITHIN GROUP (ORDER BY rem_timing->>'backend') AS backend"
                " FROM technical_docs"
                " WHERE rem_timing IS NOT NULL AND (rem_timing->>'wall_ms') IS NOT NULL"
                " GROUP BY rem_timing->>'model' ORDER BY n DESC"
            )
            # decision 568: only cycles that synthesised. A deferred sweep closes in about 0s and would swamp the percentiles, the trap fact 567 names.
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
            "note": "service_ms/contention_ms percentiles are over n_service rows "
                    "(server timings only, model/hardware anchor + capacity); "
                    "wall_ms is over n rows, caller-observed, present for every "
                    "backend incl. external",
            "by_model": render_rem_by_model(rem_rows),
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
              -- C1 fix (merger ruling, fix round on fact:1609/1621): NOT the
              -- same thing as last_success above. last_success is FILTERed on
              -- `folds_succeeded > 0`, which a CRASHED run can also satisfy —
              -- consolidation_loop writes a crashed row with rec.succeeded
              -- already > 0 when the daemon folded at least one cluster before
              -- dying. So `last_success` can be NEWER than a crash that is
              -- itself the very row inflating it, and comparing last_error_at
              -- against last_success would then call that crash "superseded"
              -- seconds after it happened. last_completed_at is FILTERed on
              -- `outcome = 'completed'` instead — a run that actually finished
              -- clean — and is the only thing `superseded` below may compare
              -- against.
              max(finished_at) FILTER (WHERE outcome = 'completed') AS last_completed_at,
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
              -- fact:1609/1621 companion — a crash from WEEKS ago, superseded
              -- by hundreds of later successes, used to read identically to a
              -- CURRENT one (both surfaced as bare "err <class>"). Paired to
              -- the SAME row as last_error_class/last_error_msg above (same
              -- FILTER predicate). `superseded` below compares this against
              -- last_completed_at, NEVER last_success — last_success is not
              -- apples-to-apples here (see the C1 comment on last_completed_at
              -- above: a crashed run can itself be what last_success reads as
              -- newest, which would make a crash read as its own supersession).
              (array_agg(started_at ORDER BY started_at DESC)
                  FILTER (WHERE outcome = 'crashed'))[1] AS last_error_at,
              -- O9 — age computed in SQL, the same way last_success_age is
              -- above, rather than a Python `now() - last_error_at` subtraction
              -- (which duplicated a clock read the DB had already taken and
              -- risked client/server clock drift). Necessarily repeats the
              -- array_agg/FILTER expression above rather than referencing
              -- last_error_at by name — the SELECT list cannot reference its
              -- own other output columns.
              EXTRACT(EPOCH FROM now() - (array_agg(started_at ORDER BY started_at DESC)
                  FILTER (WHERE outcome = 'crashed'))[1])::int AS last_error_age,
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
                  FILTER (WHERE extra ? 'singleton_clusters'))[1] AS singleton_clusters,
              -- Groups the insight gate skipped. Same NULL-until-recorded
              -- contract. Not backlog, so it does not move the stall verdict.
              (array_agg((extra->>'insight_gate_skips')::int ORDER BY started_at DESC)
                  FILTER (WHERE extra ? 'insight_gate_skips'))[1] AS insight_gate_skips
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
            # decision:1121: the backlog is the recorded eligible_clusters, not a looser density count when no census exists.
            started_at[ct] = r["last_started"] if r else None
            backlog_count = _consolidation_backlog(elig)
            has_backlog = backlog_count > 0
            stalled = _consolidation_stall_verdict(
                age, in_flight, has_backlog, CONSOLIDATION_STALL_THRESHOLD_SEC)
            any_stalled = any_stalled or stalled
            err = None
            if r and r["last_error_class"]:
                last_error_at = r["last_error_at"]
                # Crash-superseded uses last_completed_at (outcome=completed), never last_success (a crash after a fold can stamp that).
                last_completed_at = r["last_completed_at"]
                # A success after this crash means the crash is history. That is order, not the stall test and not consec_fail.
                superseded = bool(
                    last_completed_at is not None and last_error_at is not None
                    and last_completed_at > last_error_at)
                # Age in SQL on the same clock as last_success_age, not a second Python now().
                age_seconds = (
                    int(r["last_error_age"]) if r["last_error_age"] is not None else None)
                err = {"class": r["last_error_class"], "msg": r["last_error_msg"],
                       "age_seconds": age_seconds, "superseded": superseded}
            out[ct] = {
                "last_outcome": r["last_outcome"] if r else None,
                "last_success_age_seconds": age,
                "in_flight": in_flight,
                "consecutive_failures": int(r["consec_fail"]) if r else 0,
                "backlog": backlog_count,
                "stalled": stalled,
                "last_error": err,
                # eligible_oldest_age comes from the same row as eligible_clusters. A zero census must report its own age as null, not a stale value from an earlier row.
                "eligible_clusters": elig,
                "eligible_oldest_age_seconds": (r["eligible_oldest_age"] if r else None),
                # fact:1189, decision:1121: clusters left out of eligible_clusters because they were dead-lettered. None means no census has recorded it yet, not zero.
                "dead_lettered_clusters": (
                    int(r["dead_lettered_clusters"])
                    if r and r["dead_lettered_clusters"] is not None else None),
                # Clusters skipped because the re-fold matched the active summary byte for byte. None means not recorded yet, not zero.
                "unchanged_clusters": (
                    int(r["unchanged_clusters"])
                    if r and r["unchanged_clusters"] is not None else None),
                # Left out of eligible_clusters because judgement reach was exactly 1, so no insight can fold. None means not recorded yet, not zero.
                "singleton_clusters": (
                    int(r["singleton_clusters"])
                    if r and r["singleton_clusters"] is not None else None),
                # Insight groups that failed G2 or G3. None means not recorded yet. Not backlog.
                "insight_gate_skips": (
                    int(r["insight_gate_skips"])
                    if r and r["insight_gate_skips"] is not None else None),
                # Latest truncation and slot failures. None means not recorded yet. A slot failure is a protocol miss, not only a capacity one.
                "truncation_failures": (
                    int(r["truncation_failures"])
                    if r and r["truncation_failures"] is not None else None),
                "slot_failures": (
                    int(r["slot_failures"])
                    if r and r["slot_failures"] is not None else None),
                # Why the most-recent deferral happened (None if never deferred);
                # only meaningful when last_outcome == "deferred".
                "last_deferred_reason": (r["last_deferred_reason"] if r else None),
                # Mean of completed runs in 24h. The whole-cycle timer cannot price one cycle type.
                "cycle_seconds_avg": (
                    round(float(r["cycle_seconds_avg"]), 1)
                    if r and r["cycle_seconds_avg"] is not None else None),
                "runs_24h": int(r["runs_24h"]) if r and r["runs_24h"] is not None else 0,
                # Kept out of runs_24h so that count stays divisible. Deferred means skipped; idle means the gate found nothing.
                "deferred_24h": (
                    int(r["deferred_24h"]) if r and r["deferred_24h"] is not None else 0),
                "idle_24h": int(r["idle_24h"]) if r and r["idle_24h"] is not None else 0,
                "folds_succeeded_24h": (
                    int(r["folds_succeeded_24h"])
                    if r and r["folds_succeeded_24h"] is not None else 0),
                "folds_attempted_24h": (
                    int(r["folds_attempted_24h"])
                    if r and r["folds_attempted_24h"] is not None else 0),
                # Sums over 24h. Unlike the latest-value keys, absence in a real window is zero, not missing evidence.
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

        # decision:1181: count open insight-kind ledger rows whose judgements are still consolidated, not distinct pg_ids. One pg_id can hold more than one open row.
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

    # ── The dependency snapshot behind /health (v0.9.74, decision:1785) ───────

    async def _probe_postgres(self) -> dict:
        """``SELECT 1``. Postgres liveness had NO representation on /health: a
        dead database read `ok` right up until the first save 500'd, because the
        only Postgres signal was `pgvector`, probed once at startup and never
        again. Runs in the 60 s refresher, never at request time."""
        try:
            async with self._acquire() as conn:
                await conn.fetchval("SELECT 1")
            return {"state": "ok", "reason": None}
        except Exception as exc:
            return {"state": "down", "reason": type(exc).__name__}

    async def _probe_neo4j(self) -> dict:
        """``RETURN 1``. Neo4j had no liveness key at all — not even a stale
        one."""
        try:
            async with self._neo4j.session(default_access_mode="READ") as session:
                await (await session.run("RETURN 1 AS ok")).single()
            return {"state": "ok", "reason": None}
        except Exception as exc:
            return {"state": "down", "reason": type(exc).__name__}

    async def _outbox_census(self) -> dict:
        """Outbox counts + ages, in ONE query, with EVERY status present.

        ⛔ ZERO IS A NUMBER; ABSENCE IS NOT. The pre-0.9.74 census was a
        `GROUP BY status`, so a status with no rows vanished from the payload —
        `outbox.failed` was missing exactly when it was zero, which is the one
        state a consumer most needs to be able to READ rather than infer. The
        counts below are `FILTER` aggregates over one scan, so every key is
        always there.
        """
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                "SELECT"
                "  count(*) FILTER (WHERE status='pending')::int      AS pending,"
                "  count(*) FILTER (WHERE status='in_progress')::int  AS in_progress,"
                "  count(*) FILTER (WHERE status='applied')::int      AS applied,"
                "  count(*) FILTER (WHERE status='failed')::int       AS failed,"
                "  count(*) FILTER (WHERE status='rem_reviewed')::int AS rem_reviewed,"
                "  EXTRACT(EPOCH FROM now() - min(created_at)"
                "    FILTER (WHERE status='failed'))::int             AS oldest_failed_age_s,"
                "  EXTRACT(EPOCH FROM now() - min(created_at)"
                "    FILTER (WHERE status IN ('pending','in_progress')))::int"
                "                                                     AS oldest_pending_age_s"
                " FROM neo4j_outbox"
            )
        return dict(row)

    async def _registry_census(self) -> dict:
        """Row counts for the three registries the axes resolve against. Nothing
        reported these: `complete: true` on the identity probes says the graph
        and the registry AGREE, which is equally true of two empty stores.

        ⛔ THE FIRST VERSION OF THIS QUERY SELECTED FROM A TABLE THAT DOES NOT
        EXIST. `SELECT count(*) FROM domains` raises UndefinedTableError on
        every install — there is no `domains` table and there never was. The
        refresher's `except Exception: registry = None` then swallowed it, so
        `registry.projects/domains/aliases` were null FOREVER and nothing said
        why. That is the whole reason the swallow now logs and counts: a probe
        that cannot run must not be indistinguishable from a probe that has not
        run yet.

        WHAT THE THREE NUMBERS ACTUALLY COUNT, because two of them are not
        obvious from their names:

        * ``projects``  — rows in `projects`, one per registered project.
        * ``domains``   — rows in `project_domains`. A domain is identified by
          (project_id, name), so the same NAME registered under two projects is
          two rows and must be: they are different sections.
        * ``aliases``   — ACTIVE alias BINDINGS, `project_aliases` +
          `domain_aliases`, not rows in `aliases`. `aliases` is the shared
          NAME POOL; a name in it that no active binding points at resolves
          nothing, so counting the pool would report alias coverage this
          deployment does not have. Inactive (superseded) bindings are excluded
          for the same reason.

        Measured live 2026-08-28: projects 38, domains 20, aliases 18 in 1.9 ms.
        """
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                "SELECT (SELECT count(*) FROM projects)::int        AS projects,"
                "       (SELECT count(*) FROM project_domains)::int AS domains,"
                "       (SELECT (SELECT count(*) FROM project_aliases WHERE active)"
                "             + (SELECT count(*) FROM domain_aliases  WHERE active))::int"
                "                                                    AS aliases"
            )
        return dict(row)

    async def _refresh_registry_census(self) -> None:
        """Take one registry census and fold the OUTCOME into health state.

        Extracted from the refresher (F1) so the refresher and the tests run the
        SAME code — the defect this fixes was a census nothing ever executed in
        a test, and re-testing it through a different path would have reproduced
        exactly that.

        ⛔ A FAILURE IS COUNTED, LOGGED ONCE PER TRANSITION, AND SERVES THE LAST
        GOOD VALUE. The bare `except Exception: registry = None` this replaces
        is why `SELECT count(*) FROM domains` — a table that does not exist —
        ran unnoticed on every install: /health showed nulls, nothing said why,
        and the `registry` dependency stayed `ok` because it was reading a
        different counter entirely.
        """
        try:
            census = await self._registry_census()
        except Exception as exc:
            self._registry_census_failures += 1
            self._registry_census_last_error = f"{type(exc).__name__}: {exc}"
            # Once per TRANSITION: this runs every CONSOLIDATION_HEALTH_REFRESH_
            # SEC, and a line per tick is a log nobody reads.
            if self._registry_census_ok is not False:
                log.warning(
                    "health.registry: census FAILED (%s: %s) — registry.* is "
                    "serving its last good value; the registry dependency is "
                    "degraded", type(exc).__name__, exc)
            self._registry_census_ok = False
            return
        self._registry_census_last_good = census
        self._registry_census_as_of = datetime.now(timezone.utc).isoformat()
        self._registry_census_last_error = None
        if self._registry_census_ok is False:
            log.info("health.registry: census recovered")
        self._registry_census_ok = True

    async def _rem_dead_letter_count(self) -> dict:
        """How many records REM has GIVEN UP on. One cheap Neo4j aggregate.

        Deliberately narrower than `_rem_telemetry`: /health needs a VERDICT
        ("is REM losing records"), and the rule of thumb puts the numbers on
        telemetry. Computing the whole REM section here to answer one boolean
        would drag a Postgres query into the health refresher for nothing.
        """
        async with self._neo4j.session(default_access_mode="READ") as session:
            rec = await (await session.run(
                f"MATCH (n) WHERE (n:{ONT.fact} OR n:{ONT.decision}"
                f"                 OR n:{ONT.retrospective})"
                f"   AND coalesce(n.rem_processed,false) = false"
                f"   AND coalesce(n.superseded,false) = false"
                f"   AND n.pg_id IS NOT NULL"
                f"   AND coalesce(n.rem_attempts,0) >= $cap"
                f" RETURN count(*) AS n", cap=REM_MAX_ATTEMPTS
            )).single()
        return {"dead_lettered": (rec["n"] if rec else 0) or 0}

    def dependency_snapshot(self) -> dict:
        """The cached, DB-FREE inputs /health derives its dependency enums from.

        Mirrors ``consolidation_health()``'s contract exactly: refreshed in the
        background every CONSOLIDATION_HEALTH_REFRESH_SEC, read synchronously,
        and ``fresh: false`` when the last pass failed — which is a statement
        about the SNAPSHOT, never a verdict about the system.
        """
        return dict(self._dependency_health)

    def consolidation_health(self) -> dict:
        """Cached compact snapshot for /health (DB-free, refreshed in background).
        Returns {stalled, last_outcome, last_success_age_seconds, inference_busy,
        gpu_probe, fresh}. inference_busy is tri-state ("busy"|"idle"|"unknown").
        gpu_probe is gpu_load.probe_status() or None if not yet probed."""
        return dict(self._consolidation_health)

    async def _consolidation_health_refresher(self) -> None:
        """Background loop: recompute the cached /health snapshot every
        CONSOLIDATION_HEALTH_REFRESH_SEC so /health never touches the DB."""
        while True:
            try:
                full = await self._compute_consolidation_health()
                # Probe here so /health reads a cache and never shells out per request. unknown stays unknown, not a false idle.
                inference_busy = await inference_busy_state()
                # decision 928: refresh with the rest of the cache so /health never queries Neo4j per request. Only the count is carried; the breakdown stays on telemetry.
                try:
                    integrity = await self._graph_integrity()
                    invalid_nodes = integrity["invalid_nodes"]
                except Exception:
                    # A failed integrity probe must not blank the snapshot and report the system as unknown.
                    invalid_nodes = None
                try:
                    domain_identity = await self._domain_identity_health()
                except Exception:
                    # Registry drift must not present as a stalled system.
                    domain_identity = None
                try:
                    project_identity = await self._project_identity_health()
                except Exception:
                    # An incomplete upgrade must not present as a stalled system if this probe raises.
                    project_identity = None
                try:
                    # fact:1645: probe_status is pure module state and does not raise today. It still shares this guard so one probe cannot blank the snapshot.
                    gpu_probe = probe_status()
                except Exception:
                    gpu_probe = None
                self._consolidation_health = {
                    "stalled": full["stalled"],
                    "graph_invalid_nodes": invalid_nodes,
                    "project_identity": project_identity,
                    "domain_identity": domain_identity,
                    "last_outcome": full["last_outcome"],
                    "last_success_age_seconds": full["last_success_age_seconds"],
                    # Which cycle the headline age belongs to, and which type is stalled. A missing key degrades this field and must not blank the snapshot.
                    "last_success_cycle_type": full.get("last_success_cycle_type"),
                    "stalled_types": full.get("stalled_types", []),
                    "inference_busy": inference_busy,
                    "gpu_probe": gpu_probe,
                    "fresh": True,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Mark the snapshot stale instead of asserting a stall. Keep the previous inference_busy rather than inventing idle.
                log.warning("consolidation health refresh failed: %s", exc)
                self._consolidation_health = {**self._consolidation_health, "fresh": False}

            # Its own try. A dead Postgres probe must not blank consolidation, and a dead rollup must not blank the liveness enums.
            try:
                postgres = await self._probe_postgres()
                neo4j = await self._probe_neo4j()
                try:
                    outbox = await self._outbox_census()
                except Exception:
                    outbox = None
                await self._refresh_registry_census()
                try:
                    rem = await self._rem_dead_letter_count()
                except Exception:
                    rem = None
                try:
                    # Off the request path. The insight walk is unbounded sequential Neo4j hops and grows with the corpus, so it is cached with as_of.
                    nrem = await self._nrem_cycle_counts()
                except Exception as exc:
                    nrem = {"error": str(exc)}
                self._dependency_health = {
                    "postgres": postgres,
                    "neo4j": neo4j,
                    "outbox": outbox,
                    "rem": rem,
                    "nrem": nrem,
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "fresh": True,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dependency health refresh failed: %s", exc)
                self._dependency_health = {**self._dependency_health, "fresh": False}

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
        # The same walk the fold uses: GROUNDED_IN, then DOMAIN_OF, then PROJECT_OF, never MENTIONS. An edge exists only for a registered section, so presence already proves registration.
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

        # NREM gauge uses nrem_gate (no DB driver); never import the daemon module (psycopg2 not in the gateway venv).
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

        # This is the fact gate. A second always-equal domain_level_cycles field would look like a different number.
        fact_cycles = count_domain_level_cycles(
            pg_ids_all, project_map, domains_map,
            ONT.density_threshold, registered_sections,
        ) if pg_ids_all else 0

        # Count gating groups, not decisions. Each candidate is one small walk on the refresh, not on the request.
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

        # No decision_threshold: G2/G3 are at-least-one conditions, not a lowered count.
        return {
            "fact_cycles": fact_cycles,
            "decision_cycles": decision_cycles,
            "total_cycles": fact_cycles + decision_cycles,
            "fact_threshold": ONT.density_threshold,
        }

    async def _outbox_telemetry(self) -> dict:
        """The outbox's own numbers (0.9.74).

        ⛔ EVERY STATUS IS ALWAYS PRESENT, 0 WHEN ZERO — see `_outbox_census`,
        which this shares its shape with, for why absence was the defect.

        Apply latency and drain rate are DERIVED from `created_at`/`applied_at`,
        which `_apply_outbox_row` already stamps: adding an in-memory ring would
        write a value a reader can reach by query (decision:1032), and it would
        be the worse copy — a ring resets on restart, the columns do not. The
        percentile window is 24 h because applied rows are DELETED on NREM
        consolidation, so an unbounded percentile silently measures only
        whatever the last sweep happened to leave behind.
        """
        census = await self._outbox_census()
        async with self._acquire() as conn:
            lat = await conn.fetchrow(
                "SELECT count(*)::int AS n,"
                "  percentile_cont(0.5)  WITHIN GROUP ("
                "    ORDER BY EXTRACT(EPOCH FROM (applied_at - created_at))) AS p50,"
                "  percentile_cont(0.95) WITHIN GROUP ("
                "    ORDER BY EXTRACT(EPOCH FROM (applied_at - created_at))) AS p95,"
                "  count(*) FILTER (WHERE applied_at >= now() - interval '1 minute')::int"
                "    AS applied_last_min"
                " FROM neo4j_outbox"
                " WHERE applied_at IS NOT NULL AND created_at IS NOT NULL"
                "   AND applied_at >= now() - interval '24 hours'"
            )

        def _r(v):
            return round(float(v), 3) if v is not None else None

        return {
            **_outbox_public_view(census),
            "apply_latency_p50_s": _r(lat["p50"]),
            "apply_latency_p95_s": _r(lat["p95"]),
            "apply_latency_window": lat["n"],
            # Drain rate is null with an empty window (no observation) and 0.0 when rows applied but none this minute.
            "drain_rate_per_min": (float(lat["applied_last_min"])
                                   if lat["n"] else None),
            "age_limit_s": OUTBOX_AGE_WARN_S,
        }

    async def _rem_telemetry(self) -> dict:
        """REM's own section (0.9.74).

        The four backlog numbers moved here from `neo4j.*`, where they lived
        only because that is the query that produced them. `throughput_per_hour`
        is new and needs no new writer: `technical_docs.rem_timing` already
        carries a `ts` (unix epoch seconds, see dream_telemetry.call_timing_
        summary), so the rate is a count over that column.

        ⚠ `degeneration_firings` IS NULL AND WILL BE UNTIL SOMETHING DURABLE
        RECORDS IT. REM runs in a SEPARATE PROCESS (rem_loop.py, spawned by the
        gateway), and its anti-degeneration detector writes only a log line — no
        column, no counter this process can see. Reporting 0 would say "it never
        fired", which is a claim this gateway cannot make. Null says what is
        true: not observable from here.
        """
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                # The regex makes the cast safe. One older unparseable JSONB value would abort the query instead of skipping the row.
                "SELECT count(*)::int AS n FROM technical_docs"
                " WHERE rem_timing IS NOT NULL"
                f"   AND rem_timing->>'ts' ~ '{REM_TS_NUMERIC_RE}'"
                "   AND (rem_timing->>'ts')::double precision"
                "       >= EXTRACT(EPOCH FROM now()) - 3600"
            )
            attempts = None
        async with self._neo4j.session(default_access_mode="READ") as session:
            ares = await (await session.run(
                f"MATCH (n) WHERE (n:{ONT.fact} OR n:{ONT.decision}"
                f"                 OR n:{ONT.retrospective})"
                f"   AND coalesce(n.rem_processed,false) = false"
                f"   AND coalesce(n.superseded,false) = false"
                f"   AND n.pg_id IS NOT NULL"
                f" RETURN coalesce(n.rem_attempts,0) AS a,"
                f"        coalesce(n.rem_passed_over,0) AS p, count(*) AS n"
            )).data()
            attempts = ares
        return {
            "dead_lettered": sum(r["n"] for r in attempts if r["a"] >= REM_MAX_ATTEMPTS),
            "failing": sum(r["n"] for r in attempts if 0 < r["a"] < REM_MAX_ATTEMPTS),
            "passed_over": sum(r["n"] * r["p"] for r in attempts),
            "starved_pending": sum(r["n"] for r in attempts
                                   if r["p"] >= REM_STARVED_THRESHOLD),
            "max_attempts": REM_MAX_ATTEMPTS,
            "throughput_per_hour": float(row["n"]),
            "degeneration_firings": None,
        }

    def _registry_telemetry(self) -> dict:
        """Registry row counts + ingress refusal counters (0.9.74).

        The counts come off the SAME cached census /health's registry
        dependency reads — one query per refresher pass, not one per telemetry
        request. The refusal counters are in-process and reset on restart, the
        same contract every other counter in this payload carries.
        """
        census = self._registry_census_last_good
        out = {
            # After a successful census, serve last-good on a failed poll (error/as_of say so); null would look like an empty registry.
            "projects": (census or {}).get("projects", 0),
            "domains": (census or {}).get("domains", 0),
            "aliases": (census or {}).get("aliases", 0),
            "as_of": self._registry_census_as_of,
            # The SEARCH-path counter: a filter that could not be resolved.
            "read_failures_total": self._axis_registry_read_failures,
            # Separate from axis-read failures. A stale census and a search that answered from the literal string are not the same fault.
            "census_failures_total": self._registry_census_failures,
            "refusals": self._registry_refusals.snapshot(),
        }
        if self._registry_census_last_error is not None:
            out["error"] = self._registry_census_last_error
        return out

    def _pool_gauges(self) -> dict:
        """asyncpg pool size / free / in-use, or None for each.

        ⛔ THE TYPE IS CHECKED, not just the call. `_pool` is a mock in every
        unit test, and a bare `safe(self._pool.get_size)` returns the MOCK —
        which is not an exception, so nothing catches it, and it reaches
        `json.dumps` as a TypeError at serialise time: the section's own
        try/except is long past by then and the WHOLE endpoint 500s. A gauge
        that cannot be read is None; None is a documented value.
        """
        def _int(fn):
            v = safe(fn, default=None)
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        size = _int(getattr(self._pool, "get_size", None))
        free = _int(getattr(self._pool, "get_idle_size", None))
        wait = self._pool_wait_ring.snapshot()
        return {
            "pool_size": size,
            "pool_free": free,
            # Derived from the two above ONLY when both are real numbers — a
            # subtraction over a None is not a zero.
            "pool_in_use": (size - free) if (size is not None and free is not None) else None,
            "pool_wait_p50_ms": wait["p50_ms"],
            "pool_wait_p95_ms": wait["p95_ms"],
            "pool_wait_window": wait["window"],
        }

    def _encoders_telemetry(self) -> dict:
        """Per-CALL embed/rerank latency (0.9.74).

        Before this the only encoder timing anywhere was the 600 s capability
        probe — a PROJECTION from one synthetic payload, not an observation of
        what real callers experienced. These are the real calls.
        """
        from encoder_window import get_embed_window_overruns
        overruns_total, overruns_last_ts = get_embed_window_overruns()
        return {
            "embed": self._embed_ring.snapshot(),
            "rerank": self._rerank_ring.snapshot(),
            "limit_ms": ENCODER_LATENCY_WARN_MS,
            "embed_window_overruns_total": overruns_total,
            "embed_window_overruns_last_ts": overruns_last_ts,
        }

    @staticmethod
    def _gateway_telemetry() -> dict:
        """Request rate, status split, latency percentiles, in-flight (0.9.74).

        The per-request latency this aggregates has existed since the audit line
        did; it went to the JSONL and was aggregated nowhere, so "what is this
        gateway's p95" could only be answered by parsing a log file.
        """
        ring = _gateway_latency.snapshot()
        return {
            "requests_total": _gateway_requests_total,
            "by_status": dict(_gateway_by_status),
            "latency_p50_ms": ring["p50_ms"],
            "latency_p95_ms": ring["p95_ms"],
            "latency_window": ring["window"],
            "inflight": _inflight,
            "inflight_max": GATEWAY_INFLIGHT_MAX,
            "shed_503_total": _gateway_shed_503_total,
            # Incremented from record_llm_client_disconnect. A client abort is not a backend fault, so it is counted under gateway.
            "client_disconnects_total": _gateway_client_disconnects_total,
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
            # Projects and domains are registry-backed, so a top-12 hid whole sections. Agents and sources stay at 12 because those populations are unbounded.
            projects = await conn.fetch(
                f"SELECT COALESCE({PROJECT_SQL}, '(none)') AS key,"
                " count(*)::int AS count FROM technical_docs"
                " GROUP BY 1 ORDER BY count DESC LIMIT $1", BREAKDOWN_AXIS_TOP_N
            )
            # domains is an array, so one record belongs to several sections and these counts sum past the record count.
            domains = await conn.fetch(
                "SELECT d AS key, count(*)::int AS count"
                "  FROM technical_docs,"
                "       LATERAL jsonb_array_elements_text("
                "         CASE WHEN jsonb_typeof(metadata->'domains') = 'array'"
                "              THEN metadata->'domains' ELSE '[]'::jsonb END) AS d"
                " GROUP BY 1 ORDER BY count DESC LIMIT $1", BREAKDOWN_AXIS_TOP_N
            )
            # Domain breakdown ships its own denominator (array column; most records have none).
            coverage = await conn.fetchrow(
                "SELECT count(*)::int AS records_total,"
                "       count(*) FILTER ("
                "         WHERE jsonb_typeof(metadata->'domains') = 'array'"
                "           AND jsonb_array_length(metadata->'domains') > 0"
                "       )::int AS records_with_domains"
                "  FROM technical_docs"
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
            # fact:1626: domains used to be built from PROJECT_SQL. The project distribution now has its own name, and domains means domains.
            "projects": kv(projects),
            "domains": kv(domains),
            "records_with_domains": coverage["records_with_domains"],
            "records_total": coverage["records_total"],
            "summaries": [
                {"kind": r["kind"], "superseded": r["superseded"], "active": r["active"]}
                for r in summaries
            ],
        }

    # A literal until ADR-017 names it in the ontology. Matching a type that does not exist yet returns 0, not an error.
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
          top_hubs         — highest-degree entities, the consolidation backbone

        The over-merge RISK behind these (which singletons are really the same
        concept) is the offline harness's job; this just sizes the problem live.

        ⛔ REMOVED IN 0.9.74 — `alias_edges`, `alias_covered_entities`,
        `alias_components`, `largest_alias_component`. Not moved: REMOVED. The
        first two counted an `ALIASES` relationship NO CODE PATH HAS EVER
        WRITTEN, and the second two read `Entity.alias_component`, whose only
        writer (a gds.wcc caller) was retired. All four had therefore read 0
        since they shipped, and a metric that can only ever read 0 does not
        report an empty graph — it reports nothing, while looking like a
        measurement. The name also collided with the LIVE alias tables
        (`aliases`, `project_aliases`, `domain_aliases`), which is worse than
        useless: a reader seeing `alias_edges: 0` beside a registry with real
        aliases in it concludes the alias layer is broken.
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
            # Sum per key. The query groups by label and reason, so a direct dict would let a later row overwrite an earlier one and under-report.
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

    # ── GET /admin/outbox (the backup drain gate's read) ───────────────────────

    async def handle_admin_outbox(self, request: web.Request) -> web.Response:
        """GET /admin/outbox — the outbox census for the backup drain gate (admin-role only).
        Same six keys and values as telemetry.outbox, through _outbox_public_view, served behind
        the prefix an admin token may reach. Read LIVE on purpose — no TELEMETRY_CACHE_S, no
        strip_dropped — because a drain gate must not declare drained from a stale snapshot.

        O12: no handler-side `_record_gateway_request` call on the 503 path below —
        this handler RETURNS the error response rather than raising, so
        `auth_middleware`'s own `finally` (status = resp.status) already records
        it once; a second call here would double-count."""
        try:
            census = await self._outbox_census()
        except Exception as exc:
            return web.json_response(
                {"status": "error", "message": f"outbox census unavailable: {type(exc).__name__}"},
                status=503)
        return web.json_response({"status": "success", "outbox": _outbox_public_view(census)})

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
    """Register the coordinator's exact-path routes before the proxy catch-all."""
    app.router.add_post("/memory/save",           coordinator.handle_save)
    app.router.add_post("/memory/retrospective",  coordinator.handle_retrospective)
    app.router.add_post("/memory/supersede",      coordinator.handle_supersede)
    app.router.add_post("/memory/review_hold",     coordinator.handle_review_hold)
    app.router.add_post("/memory/search",         coordinator.handle_search)
    app.router.add_post("/memory/graph",          coordinator.handle_graph)
    app.router.add_get( "/memory/status/{pg_id}", coordinator.handle_status)
    app.router.add_get( "/memory/telemetry",       coordinator.handle_telemetry)
    app.router.add_post("/admin/backup",           coordinator.handle_backup)
    app.router.add_get( "/admin/outbox",           coordinator.handle_admin_outbox, allow_head=False)
