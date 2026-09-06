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
# THE CONTRACT DECIDES WHAT /memory/telemetry SERVES: a key whose `removed_in`
# this release has reached comes off the response in handle_telemetry. A leaf
# module — its only import is `__future__.annotations`.
from telemetry_contract import TELEMETRY as TELEMETRY_CONTRACT, strip_dropped

log = logging.getLogger("coordinator")

try:
    from gpu_load import inference_busy_state, probe_status
except Exception as _gpu_exc:  # pragma: no cover - import-time safety only
    # The busy signal is observability, never load-bearing: if gpu_load can't be
    # imported the gateway must still serve. Fall back to "unknown" so the monitor
    # never renders a false "idle" (it cannot tell, and says so).
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


# ── Version contract ────────────────────────────────────────────────────────────
# FRAMEWORK_VERSION is the informational build/semver — it changes every release.
# API_VERSION is the wire contract between memory_bridge.py (the thin client that
# ships with the skill) and this coordinator. Bump it ONLY when the request or
# response shape, auth scheme, or routes change in a way that breaks older clients.
# Client and server build-versions are allowed to drift; their API_VERSION must agree.
FRAMEWORK_VERSION = "0.9.91"
# v2 (retro-as-record): /memory/retrospective now creates a full record (own
# pg_id, embedding, Retrospective node) and accepts rating enum + grounding —
# the response shape changed (returns the retro's own pg_id).
# v4 (project registry): a fact save without a REGISTERED metadata.project is
# rejected 400 carrying error=project_required|project_unknown plus near-match
# proposals. BREAKING for any client that saved untagged facts. The second
# submission is accepted in three forms: a proposal, new_project=true, or the
# reserved sentinel general_discussion.
API_VERSION = 4
CLIENT_VERSION_HEADER = "X-SM-Api-Version"
#: The client's own FRAMEWORK VERSION (e.g. "0.9.74"), distinct from the wire
#: API_VERSION above: two clients can speak api_version 4 while one of them is
#: forty releases behind on behaviour, and only this header can tell them apart.
#: Advertised by memory_bridge.py (both copies) and mcp/vector-skill.py from
#: 0.9.74; a pre-0.9.74 client sends nothing and is simply not counted.
CLIENT_BUILD_HEADER = "X-Shared-Memory-Client"

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
#
# ⛔ INVARIANT (v0.9.76, security fix A1): every member of this set must be the
# canonical of a registered PLAIN (static) aiohttp resource, and membership is
# tested against `request.rel_url.path_safe` — the string the ROUTER itself
# compares — by EXACT equality. Never against a normalised or decoded spelling.
#
# Until v0.9.76 the exemption tested `request.path.rstrip("/")`, which exempted
# `/health/`, `/health//` and `/health%2f` while the router sent all three to
# the catch-all LLM proxy: the auth middleware and the router disagreed about
# what "/health" meant, and the gap between them was an unauthenticated,
# unaudited path to the LLM backend.
#
# ⛔ The FIRST round of that fix replaced `rstrip` with `request.path` and left
# the same class open, because `request.path` is fully percent-DECODED:
# `/pool%2fstatus` decodes to `/pool/status` and was exempted, while the router
# — which matches on `path_safe`, where `%2F` stays encoded — resolved it to
# the catch-all. Exempt from auth AND proxied: A1's exact shape, inside A1's
# own fix. `_router_match_path` below closes it by comparing what the router
# compares; `require_unprotected_paths_are_plain_routes` enforces at startup
# the route-ownership half that makes that comparison meaningful.
_UNPROTECTED_PATHS = {"/health", "/pool/status"}


def _router_match_path(request) -> "str | None":
    """The exact path string aiohttp's router compared this request against,
    or None when there is no usable one.

    Security fix A1. The auth exemption must test the SAME string the router
    tests. Any second opinion — a normalisation, a decoding, a rstrip — is a
    place where the middleware and the router can disagree about what
    "/health" means, and every A1-class hole lives in that disagreement.

    aiohttp 3.14.3, `web_urldispatcher.Resource.resolve`::

        if (match_dict := self._match(request.rel_url.path_safe)) is None:

    and `PlainResource._match` is `self._path == path`, where `_path` is the
    resource's `canonical`. So for a PLAIN (static) resource — which
    `require_unprotected_paths_are_plain_routes` guarantees every exempt entry
    is — `rel_url.path_safe in _UNPROTECTED_PATHS` is byte-for-byte the
    router's own admission test, run on the router's own input. The middleware
    grants exactly when the router will route to the exempt handler.

    ⛔ NOT `request.path`: that is the fully percent-DECODED path, so
    `/pool%2fstatus` reads as `/pool/status` there while the router sees
    `/pool%2Fstatus` and matches nothing. `path_safe` keeps `%2F` and `%25`
    encoded, which is precisely the difference the defect lived in.

    ⚠ Read defensively, and require a `str`. Anything that is not a `str` — a
    None link in the chain, a test double's auto-attribute — yields None and
    therefore DENIES: the exemption is never granted on a value whose type we
    could not establish. A `None` return is not in `_UNPROTECTED_PATHS`, so the
    caller needs no separate check.
    """
    rel_url   = getattr(request, "rel_url", None)
    path_safe = getattr(rel_url, "path_safe", None)
    return path_safe if isinstance(path_safe, str) else None


def require_unprotected_paths_are_plain_routes(router) -> None:
    """Startup assertion for the invariant the A1 exemption rests on: every
    `_UNPROTECTED_PATHS` entry must be the canonical of a registered
    PlainResource (adversarial review A-08).

    WHY THIS STILL MATTERS AFTER THE FIX ROUND. The per-request test is
    `_router_match_path(request) in _UNPROTECTED_PATHS`, i.e. the router's own
    `PlainResource._match` comparison. That equivalence holds *only* for a
    plain resource: `PlainResource._match` is string equality, but every other
    resource class matches by pattern or prefix, and `canonical` is deliberately
    MANY-TO-ONE for them — `/memory/status/1` and `/memory/status/2` both report
    `/memory/status/{pg_id}`. So the moment this set gains a value that is a
    dynamic canonical, the middleware's exact compare stops modelling the
    router's match, and the whole family behind that pattern is at risk of being
    treated as exempt. The offending string looks perfectly innocent in a diff.
    Fail at startup instead, where an operator sees it.

    It also guarantees the weaker but load-bearing property that an exempt path
    is a path some registered route OWNS — an exemption for a path the router
    does not own can only ever be honoured by the catch-all, which forwards to
    an LLM backend. That IS the A1 defect, stated as a route-table property.

    Raises RuntimeError naming the offending entry. Called from
    hive_mind_proxy.main() next to set_known_routes(), i.e. after every real
    route is registered and before the catch-all — the same window that makes
    the route snapshot meaningful.
    """
    # The exemption reads `rel_url.path_safe` because that is the attribute
    # `Resource.resolve` passes to `_match`. If the installed yarl does not
    # expose it, `_router_match_path` returns None for EVERY request and
    # /health 401s on every auth-on install — a hard availability failure
    # wearing the shape of a safe deny, and one no unit test that builds its
    # own request double would ever see. Refuse to boot instead.
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
    # Search is a READ — this file's own quiesce classification already says so
    # ("Reads (search/graph/telemetry/status) and /health always flow"), and the
    # allowed read-only Cypher on /memory/graph can reach every record search
    # can, so admitting search widens no exposure. Measured 2026-08-24: the
    # first read-only MCP client on the fleet was 403'd on the most read-like
    # operation there is, while graph_query would have answered.
    ("POST", "/memory/search"),
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
# backups and read the outbox census). Backup quiesce/resume is the first such
# route. The second is the outbox census the backup drain gate needs (v0.9.92,
# fact:2022): the admin token is confined to /admin/*, so a drain gate that
# polled /memory/telemetry was 403'd on every poll and could only ever time
# out — this route is read-only and lets that gate actually read live.
_ADMIN_ROUTES: set[tuple[str, str]] = {
    ("POST", "/admin/backup"),
    ("GET", "/admin/outbox"),
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
    an edit of the old one. The latest live retrospective is simply read as the
    current verdict, so nothing needs retracting for the newer judgement to take
    effect.

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

# The two JUDGEMENT record types. Only FACTS carry entities (decision:1664,
# ruled R1 at v0.9.69): a judgement reaches its topics by walking to the facts
# it rests on, so an entity named on one is never written to the graph and only
# adds an unvetted name to the vocabulary (`fact:970`).
# ⛔ THERE IS NO `JUDGEMENT_TYPES` TUPLE OF RAW STRINGS, deliberately. One
# existed for exactly one release and every use of it was a bug waiting to be
# written: an exact `metadata["type"] in (...)` match against a CLIENT-SUPPLIED
# string, beside a `record_label_for_type` that normalises. Removed so the only
# way to ask the question is the predicate below.
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
    """The project registry could not produce an identity for a name that has
    one (item 6, v0.9.69; ruled R3).

    Its own class rather than a bare RuntimeError because three surfaces answer
    it differently and each must be able to say which one it is handling: an
    outbox row RETRIES (then goes `failed`, visibly); an ingress turns it into
    a 503 `registry_unavailable`; a READER degrades to "no identity" and
    reports the degrade, never a 500.
    """


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

# Batched resolve (S-5, security review fact:1412): the array/ANY form of
# ENTITY_VOCAB_RESOLVE_SQL, one round trip for the whole candidate list
# instead of one `self._acquire()` per name. `unnest` drives the row set so
# every input name gets exactly one output row even when it matches nothing;
# GROUP BY collapses the (harmless, same-canonical) duplicate rows Option B's
# alias table can produce when one entity has two verbatim aliases that
# normalize to the same key — the same case ENTITY_VOCAB_RESOLVE_SQL's
# LIMIT 1 caps for a single name. Verified against the live database
# read-only; see EG_LEG1_HANDOFF.md's FIX ROUND section for the numbers.
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

# ── Minting a NEW entity: the CONFUSABLE check (item 1, v0.9.69) ──────────────
#
# The mint path had no equivalent of `_new_project_refusal` / `_new_domain_refusal`:
# `Games Workshops` minted straight beside `Games Workshop` with no warning
# (`fact:1734` A(2)). This is the same rule those two enforce, on the same
# override — the caller names the neighbour it means to differ from, because a
# name cannot be produced without having read it, while a boolean can be flipped
# without reading anything.
#
# ⚠ THERE IS NO SPELLING-VARIANT HALF HERE, and its absence is deliberate rather
# than an omission. On the project axis a separator/case variant is refused
# outright, uncconfirmable — but for entities that case cannot reach the mint at
# all: `_entity_ingress_validate` resolves every candidate through
# `ENTITY_VOCAB_RESOLVE_MANY_SQL`, which joins on `entity_normalize()` (the SQL
# twin of `axis_key`), so a key-identical name is already RESOLVED to its
# canonical and never appears in `unknown`. A spelling-variant guard here would
# be code no input can reach — and a test for it would be unkillable.
#
# Names and aliases both, because a caller confusing its new name with a
# spelling the operator has already curated is the same mistake as confusing it
# with a canonical.
#
# ⚠ IT IS A SEQUENTIAL SCAN, AND NO INDEX WOULD CHANGE THAT. A trigram GIN
# index answers `name % $1` and `name ILIKE '%…%'`; it cannot answer
# `similarity(name, $1) >= $2`, which is an ordinary function call in the WHERE
# clause, so the planner reads every row whatever indexes exist. A migration
# adding `gin (name gin_trgm_ops)` for this query was written, reviewed and
# DROPPED for exactly that reason — an index that is never used is not free:
# it costs every INSERT, and its presence argues that the cost was measured.
#
# ⚠ THE COST IS UNMEASURED (`fact:1338`). What is known is the SIZE: 157 rows
# in `entity_vocabulary` on this deployment today, plus the alias table, on a
# path that only runs when a save actually MINTS. No timing has been taken. If
# this ever needs to be fast, the change is to the PREDICATE — `name % $1`,
# which a trigram index does serve, with `similarity()` kept only for ordering
# — and it should be driven by a measurement, not by adding an index to the
# query as it stands.
#
# ✅ SAME SHAPE, SAME NON-USE, in `CONFUSABLE_SQL` (projects, migration 022) and
# `DOMAIN_CONFUSABLE_SQL` (domains, 028) — and those two indexes are now GONE
# (migration 039, v0.9.72). Measured before dropping them:
# `pg_stat_user_indexes.idx_scan` was 0 for both against a
# `pg_stat_database.stats_reset` of NULL, so neither had ever been used, and
# every one of the five similarity() lookups planned as a sequential scan in
# well under a millisecond. The `pg_trgm` EXTENSION stays — `similarity()`
# comes from it.
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

# ⚠ UNMEASURED ON THIS VOCABULARY, and said plainly (`fact:1338`). The default
# is CARRIED OVER from `PROJECT_CONFUSABLE_SIMILARITY`, whose 0.6 was derived
# from a live registry — every pair of 37 registered projects, closest
# legitimately distinct pair 0.500, realistic typos 0.78-1.00. No equivalent
# pairwise sweep has been run over `entity_vocabulary`, whose names are longer
# and more varied than project names, so this floor is a starting point that
# INHERITS a measurement rather than one that has its own.
#
# The measurement that would settle it: score every pair of registered canonical
# + alias spellings, and score a set of realistic typos of them, then place the
# floor in the gap between the two populations — the same procedure the project
# floor came from. Until that runs, the env override is the answer for an
# install whose vocabulary this floor fits badly.
ENTITY_CONFUSABLE_SIMILARITY = float(
    os.environ.get("ENTITY_CONFUSABLE_SIMILARITY", "0.6")
)
ENTITY_PROPOSAL_LIMIT = _env_int("ENTITY_PROPOSAL_LIMIT", 5)


# A project name is an AXIS, never an entity (`fact:1215`) — and the graph's own
# gate does not catch it: `sanitize_entity_name` rejects `Project` (the schema
# label) but passes `shared-memory-GitHub` cleanly, which is exactly why the rule
# kept being broken and why the live graph carries `:Entity` nodes named after
# registered projects (`fact:1734` A/C).
#
# ⚠ IT READS THE STORED KEY, for the reason `PROJECT_NAME_OR_KEY_SQL` documents:
# migration 035 maintains `projects.normalized_key` with a trigger and a UNIQUE
# constraint, so the key is the database's own materialised value on an indexed
# column, and the Python side stays the single definition (`axis_key`).
#
# ⚠ THE COMPARISON SET IS `projects` ALONE. A retired spelling is not a separate
# population to check — a rename writes the old spelling into `project_aliases`
# AND keeps its row in `projects`, so the alias table adds nothing here. The one
# name that is NOT in `projects` is the sentinel (a CHECK constraint keeps it
# out), so it is named explicitly in `RESERVED_ENTITY_AXIS_KEYS` below.
#
# ANY() rather than one query per name: one round trip for the whole candidate
# list, the same choice `ENTITY_VOCAB_RESOLVE_MANY_SQL` makes.
# ⚠ TWO POPULATIONS, AND THE SECOND ONE IS THE LIKELIER MISTAKE. A live project
# keeps a `projects` row with a trigger-maintained `normalized_key`. A RETIRED
# spelling does not: `normalize_projects.py` deletes the `projects` row and
# leaves the old string in `aliases`, joined through `project_aliases`. So a
# registry-only check answers "not a project" for exactly the spelling a machine
# still carrying the old folder name will send — which is the same reasoning
# `_new_project_refusal` records for its own alias sweep.
#
# ⚠ THE ALIAS HALF COMPUTES ITS KEY, because `aliases` has no `normalized_key`
# column (035 added one to `projects` and `project_domains` only). It calls the
# database's own `axis_normalize()` — the SQL twin of `axis_key`, and the same
# function 035's trigger uses — never a second normalisation expression.
#
# ⚠ BOTH HALVES RETURN THE CANONICAL PROJECT NAME, never the alias: an alias is
# not somewhere a record may be saved, so pointing a refusal at one would name a
# spelling the caller must not use either.
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

# Axis keys reserved against entity names that no `projects` row can carry.
# `general_discussion` is the parked-record sentinel: it is a legitimate value on
# the PROJECT axis and is excluded from the registry by a CHECK constraint, so a
# registry query can never answer for it.
RESERVED_ENTITY_AXIS_KEYS: dict[str, str] = {
    axis_key(SENTINEL): SENTINEL,
}

# Env-overridable caps (S-5): a name/list-length bound is a correctness/DoS
# property, not a performance tuning parameter (fact:1338 governs the
# UNCACHED lookup choice, not this). Measured against the live corpus before
# choosing defaults (2026-08-20, `agent_data`, 1316 technical_docs rows):
# max canonical/alias name length 22 chars (avg 9/11), max individual entity
# string ever saved 22 chars; max `entities` list length ever saved 6 (p99
# 5.0, avg 1.28), max in the last 30 days 5. Defaults set comfortably above
# both (≈9x the measured name-length max, ≈8x the measured list-length max)
# — a bound, not a claim about what "should" be typical. Env-overridable so
# an install with a genuinely different usage pattern is not locked to this
# one's measurement.
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


# ── Governance: outer in-flight load-shed valve ─────────────────────────────────
# Checked FIRST in auth_middleware, ahead of every auth exemption (S-11).
# SEC-A5-05a (PR A5 fix round): incremented/decremented uniformly around
# EVERY admitted request — auth-disabled bypass, unprotected-path exemption,
# and the authenticated dispatch alike — so an anonymous /health/pool-status
# flood, or (on an auth-off install) any traffic at all, actually counts
# toward the cap it can also be shed by. A request the valve sheds was never
# admitted and never reaches the increment.
_inflight = 0

# ── Gateway request instrument (the telemetry contract, v0.9.74) ───────────────
# The per-request latency the audit line already carries (see auth_middleware's
# `finally`) was written to the JSONL and aggregated NOWHERE, so nothing could
# answer "what is this gateway's p95" without parsing a log. These are the same
# numbers, aggregated in memory.
#
# ⛔ RECORDING MUST NOT CHANGE THIS PATH'S FAILURE MODES. Every write below goes
# through telemetry_instruments, whose recorders swallow everything, and the one
# assembling function is wrapped by the caller. It never awaits, never touches a
# connection, and runs in the SAME `finally` that already writes the audit line —
# so it cannot add a failure mode the audit line does not already have.
GATEWAY_LATENCY_WINDOW = int(os.environ.get("GATEWAY_LATENCY_WINDOW", "500"))
_gateway_latency = LatencyRing(GATEWAY_LATENCY_WINDOW)
_gateway_requests_total = 0
_gateway_shed_503_total = 0
# D9 (OBS round) — the LLM proxy's own client-abort counter. Storage lives
# here rather than in hive_mind_proxy.py (which owns the increment SITES,
# in its prepare()/streaming windows) for the same reason `_llm_faults_snapshot`
# and its `record_llm_*_fault` writers do: `telemetry_extras_provider`'s merge
# into the telemetry payload (see `_build_telemetry` below) is a SHALLOW
# `dict.update`, which would silently clobber this whole `gateway` section
# were a second copy of it assembled on the hive_mind_proxy side instead.
# ⚠ SCOPE NOTE: this is a deviation from this build step's stated coordinator.py
# ownership (`/memory/graph handler ONLY`) — recorded in HANDOFF.md, not
# decided silently. See `record_llm_client_disconnect` below and its one new
# line in `_gateway_telemetry`.
_gateway_client_disconnects_total = 0
_gateway_by_status: dict[str, int] = {
    "2xx": 0, "4xx": 0, "5xx": 0, "401": 0, "403": 0, "409": 0, "503": 0,
}
#: {client VERSION string: requests seen}. Fed by the X-Shared-Memory-Client
#: header both front doors send from 0.9.74; a pre-0.9.74 client sends none and
#: is simply not counted — an absent client is not a version, and inventing
#: "unknown" as a bucket would put every old client into one made-up release.
_client_versions_seen: dict[str, int] = {}
#: How many DISTINCT client versions may be tracked. A header is caller-supplied
#: text, so the map is a caller-controlled allocation without a bound.
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
            # ⛔ CANCELLATION IS NOT A POOL FAILURE. A task cancelled while
            # waiting — shutdown, a client disconnect, an outer timeout — says
            # nothing about whether the pool could have served it. Counting it
            # would make an orderly gateway restart look like a burst of
            # database errors, which is exactly the false alarm this counter
            # exists to avoid raising.
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
        # Bounded and sanitised: this is caller-supplied text on an
        # unauthenticated-reachable path, and it ends up in a JSON payload an
        # operator reads. A version string is short and boring; anything else is
        # not a version.
        v = raw.strip()[:32]
        if not v or not all(c.isalnum() or c in "._-+" for c in v):
            return
        if v not in _client_versions_seen and len(_client_versions_seen) >= CLIENT_VERSIONS_MAX:
            return
        _client_versions_seen[v] = _client_versions_seen.get(v, 0) + 1
    except Exception:
        pass

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
            "role": effective_role(agent, _AGENT_ROLES.get(agent)),
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

# D1 (OBS round): a FIXED, bounded ring of the monotonic timestamp of every
# token_verify_failed event — replaces the old health-build-gap extrapolation
# (`hive_mind_proxy._delta_per_min`), whose reading was poll-cadence-dependent
# (one event read ~24/min at the 3 s HEALTH_CACHE_TTL_S, 0.1/min under 600 s
# polling — the poll cadence WAS the reading, not the event rate).
#
# ⛔ `maxlen=256` is a FIXED bound, deliberately NOT derived from
# TOKEN_VERIFY_WARN_PER_MIN (an env-overridable float defined ~800 lines
# below the two bump sites this ring is appended from) — deriving the ring's
# capacity from that threshold invites a NameError at import order, a
# maxlen=0 crash if the operator ever sets the threshold to 0, or an
# unbounded ring if they set it to something that reads as infinite. A flood
# inside one 60 s window therefore saturates at exactly 256 counted events,
# not the true rate — the warning still fires; the unbounded lifetime total
# stays visible at credentials.token_verify_failed. See
# telemetry_token_verify_ring()'s docstring for the read side.
#
# Stamped with time.monotonic(), NEVER wall-clock: a wall-clock step
# backward would make every past event look freshly-arrived and stick the
# gateway `degraded` until the ring drains 256 entries later.
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
    _token_verify_failure_ring.append(time.monotonic())
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


# F (S4, ADV1-16): a SEPARATE token bucket for verify failures observed on
# an UNPROTECTED path (/health, /pool/status). Same shape and rate as
# _tvf_bucket_* above, but kept as independent state — a caller flooding
# /health with a bad bearer must not be able to exhaust the budget the
# protected-path forensic lines above depend on (bucket isolation).
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
    """F (S4): a bearer PRESENTED on an UNPROTECTED path (/health,
    /pool/status) that fails to verify — a token-oracle probe. Never gates
    the response: the caller still gets exactly the same anonymous/slim
    payload it always did (auth_middleware's own unprotected-path exemption
    is unchanged; this is a pure side-effect audit call). This is what makes
    such an attempt visible for forensics, the same way a bad bearer on a
    protected path already is.

    Shares _record_token_verify_failed's COUNTER
    (credentials.token_verify_failed) and event name — decision:1785 rules
    out a new /health-only telemetry key, so the existing
    /memory/telemetry consumer sees this signal too, unchanged in shape.
    Only the LOG-LINE rate limit is separate (its own bucket, above), so a
    caller flooding /health with a bad bearer cannot exhaust the budget a
    protected-path failure depends on to be seen."""
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

    # S-11 (PR A5): the load-shed valve is the FIRST gate — ahead of both the
    # auth-disabled bypass below and the per-path _UNPROTECTED_PATHS
    # exemption further down. It used to sit after both, so an anonymous
    # /health or /pool/status hit (and, on an auth-unset install, EVERY
    # request) could never be shed no matter how saturated the gateway
    # already was. The valve exists to protect the PROCESS from an in-flight
    # pile-up, not to gate access, so it must apply uniformly regardless of
    # what auth decides afterward.
    if GATEWAY_INFLIGHT_MAX and _inflight >= GATEWAY_INFLIGHT_MAX:
        # Counted HERE, not in the `finally` below: a shed request is never
        # admitted, so it never reaches the audit line and would otherwise be
        # the one 503 the gateway serves that nothing can see. `gateway.shed_
        # 503_total` is the number; /health raises the warning off it.
        try:
            _gateway_shed_503_total += 1
        except Exception:
            pass
        # D2 (OBS round): a shed request is never admitted, so it never
        # reaches the deep `finally` below either — this is the ONLY place
        # it can be counted into gateway.requests_total/by_status.503 at
        # all. No latency ring entry (R-C): this exit never takes
        # `started`, and `by_status.503` from here is now only
        # `>= shed_503_total`, not equal-by-construction on the shed class
        # alone (see MEANING_CHANGES).
        _record_gateway_request(503, None)
        raise web.HTTPServiceUnavailable(
            reason="gateway at capacity", headers={"Retry-After": "1"},
            **_error_body("Gateway at capacity — too many requests in flight; "
                          "retry after the Retry-After interval."),
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
            # D2 (OBS round): count this request too — auth-off traffic used
            # to be entirely invisible to gateway.requests_total/by_status.
            # `_audit` is UNTOUCHED and still never fires on this path (its
            # variable dependencies — agent_name/started/request_id — are
            # never established here; ADV1's traced UnboundLocalError trap
            # only exists if `_audit` itself moves into this block, which it
            # does not). No latency ring entry (R-C): this exit never takes
            # `started` either. `_status` defaults to 500 exactly like the
            # deep block below's own fallback, for any exception this
            # bypass does not special-case (it never converted
            # asyncio.TimeoutError before, and still does not).
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
        # Security fix A1 (v0.9.76). ONE comparison, and it is the ROUTER's own.
        #
        # `rel_url.path_safe` is the exact string aiohttp hands to
        # `PlainResource._match` (`self._path == path`), so membership in
        # _UNPROTECTED_PATHS *is* the router's admission test for the two static
        # routes this set names. The middleware and the router cannot disagree
        # about what "/health" means, because they compare the same bytes. See
        # _router_match_path's docstring for the aiohttp source this rests on,
        # and require_unprotected_paths_are_plain_routes for the startup
        # assertion that keeps the "plain resource" precondition true.
        #
        # This is also why `OPTIONS /health` and `POST /health` keep the
        # anonymous 405 + `Allow: GET, HEAD` that fact:1535 requires. Their
        # path_safe IS "/health", so the exemption grants; aiohttp then routes
        # them to the catch-all (no GET/HEAD route accepts the method) whose
        # _route_guard answers 405 before any LLM dispatch. "Get a token" is
        # not why those requests failed.
        #
        # ⛔ TWO spellings have been removed from here, and BOTH were the defect:
        #   `request.path.rstrip("/")` — exempted `/health/`, `/health//`,
        #        `/health%2f`, `/pool/status/`; the router sends none of them to
        #        handle_health, so each fell through the catch-all into an
        #        anonymous, unaudited LLM proxy call.
        #   `request.path` — the fix round's own first attempt, and the same
        #        class: `request.path` is fully percent-DECODED, so
        #        `/pool%2fstatus` read as `/pool/status` and was EXEMPTED while
        #        the router (matching `/pool%2Fstatus`) sent it to the catch-all.
        # Do not reintroduce either. Normalisation and decoding are legitimate
        # for a REFUSAL decision (see AsyncHiveMindProxy._route_guard, which
        # refuses a near-miss spelling of an owned path), never for a GRANT.
        if _router_match_path(request) in _UNPROTECTED_PATHS:
            # F (S4, ADV1-16/ADV2-1/ADV2-15): a bearer PRESENTED on this
            # unprotected path that fails to verify is a token-oracle probe
            # — audit the attempt, but the RESPONSE must stay byte-identical
            # either way (ADV2-1: falling through to the 401 branch below
            # would break the anonymous contract every daemon and doctor
            # relies on). A VALID bearer still reaches the full
            # authenticated payload — handle_health resolves identity itself
            # via _safe_resolve_identity, entirely independent of this
            # audit-only side effect. Scheme match is case-insensitive here
            # (unlike the protected-path helper) so a lower-case `bearer`
            # scheme is audited too, not silently ignored.
            _unprotected_presented = _extract_bearer_token_ci(request)
            if _unprotected_presented is not None and not _lookup_agent_by_token(_unprotected_presented):
                _record_unprotected_path_token_verify_failed(request, _unprotected_presented)
            # D2 (OBS round): count this request too — /health and
            # /pool/status traffic used to be entirely invisible to
            # gateway.requests_total/by_status. A stale-bearer probe here
            # still serves the SAME anonymous 200 it always did (the
            # response is untouched above), so it lands in `by_status.2xx`,
            # never `by_status.401` — that 401-shaped signal already went
            # to `credentials.token_verify_failed` and the D1 ring above.
            # No latency ring entry (R-C): this exit never takes `started`.
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
            # RFC 6750 §3: a token was PRESENTED but rejected gets
            # error="invalid_token"; no token at all gets the bare challenge —
            # and only the former has a digest worth logging (PR A3). This is
            # the gateway's OWN 401 (its own door), never an upstream one.
            presented = _extract_bearer_token(request)
            _record_token_verify_failed(request, presented)
            www_authenticate = 'Bearer error="invalid_token"' if presented else "Bearer"
            # D2 (OBS round): the gateway's own 401 used to be invisible to
            # gateway.requests_total/by_status — only the D1 ring and
            # credentials.token_verify_failed saw it. No latency ring entry
            # (R-C): this exit never takes `started`.
            _record_gateway_request(401, None)
            raise web.HTTPUnauthorized(
                reason="Authorization: a valid Bearer token is required",
                # X-SM-Fault-Origin alongside the RFC 6750 challenge (security
                # review O-5): the header is otherwise set only on the three
                # LLM-path gateway errors, so a client distinguished a gateway-
                # origin 401 from an upstream one only by the header's ABSENCE —
                # and absence is exactly what a stripping intermediary produces.
                headers={"WWW-Authenticate": www_authenticate, "X-SM-Fault-Origin": "gateway"},
                **_error_body("Authorization: a valid Bearer token is required."),
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
        role  = effective_role(agent_name, _AGENT_ROLES.get(agent_name))
        route = (request.method, request.path.rstrip("/") or "/")
        # D2 (OBS round): every HTTPException `auth_middleware` raises itself
        # (this one and the four below) used to be invisible to
        # gateway.requests_total/by_status — only the deep `finally` at the
        # bottom of this function ever recorded anything. No latency ring
        # entry on any of them (R-C): none of these exits take `started`.
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
                # Also feeds gateway.by_status.503 alongside the shed valve
                # and pool-saturated — see MEANING_CHANGES.
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
            # The same numbers the audit line just wrote, aggregated in memory
            # so a consumer does not have to parse the JSONL to get a p95.
            # Deliberately AFTER _audit: the durable record is written first,
            # and this call cannot raise (see _record_gateway_request).
            _record_gateway_request(status, latency_ms)
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
#
# The backend BASE is the same env the gateway's routing map reads
# (EMBEDDER_URL / RERANKER_URL, hive_mind_proxy.py) — one setting moves BOTH the
# passthrough and the coordinator's own save/search calls. Before this the two
# were literals here, so pointing EMBEDDER_URL at a remote host redirected only
# the raw /v1/embeddings passthrough while every real embedding still went to
# localhost (measured on a LAN embedder: passthrough answered from the remote,
# saves kept using the local container). The port is a default, never an assumption.
def _encoder_url(env_name: str, default_base: str, path: str) -> str:
    """Full endpoint for an encoder backend: env-overridable BASE + fixed PATH.

    Validated at the same time it is derived (module import/reload) so a bad
    value is caught before the process ever accepts traffic, rather than
    surfacing as an opaque connection error on the first save/search:
      - the resolved BASE must be an http(s) URL — anything else (a bare
        host, a typo'd scheme, a leftover placeholder) fails LOUDLY, naming
        env_name, rather than producing a confusing httpx/aiohttp exception
        deep inside _embed()/_rerank() on the first real request.
      - a base carrying ANY path segment only WARNS (never fails): the path
        this function appends always starts with "/v1/...", so ANY existing
        path on the base — not only a base ending in exactly "/v1" — gets a
        second path appended after it. L4 (PR #308 review): the original
        check only matched a base ending in "/v1" literally, so a plausible
        copy-paste like "http://h:8070/v1/embeddings" (the full endpoint,
        pasted as if it were the base) silently doubled the whole path with
        no warning at all.
    """
    base = (os.environ.get(env_name) or default_base).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"{env_name} must be an http(s) URL, got {base!r} "
            f"(scheme {parsed.scheme!r}) — check {env_name} in shared-memory/.env"
        )
    if parsed.path:
        log.warning(
            "%s (%s) already carries a path (%r) — the resolved endpoint "
            "will be %s%s, appending onto whatever is already there. "
            "%s should normally be just scheme://host[:port], with no path.",
            env_name, scrub_url_credentials(base), parsed.path,
            scrub_url_credentials(base), path, env_name,
        )
    return f"{base}{path}"

EMBED_URL  = _encoder_url("EMBEDDER_URL", FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"], "/v1/embeddings")
RERANK_URL = _encoder_url("RERANKER_URL", FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"], "/v1/reranking")

# M1 (PR #308 review): this USED to log right here, at module import — but
# hive_mind_proxy.py imports coordinator (line 55) BEFORE its own
# logging.basicConfig() call (line ~85), and a named logger's .info() before
# basicConfig is dropped (root defaults to WARNING; Python's "lastResort"
# handler is WARNING-and-above only). Verified in isolation: the /v1 WARNING
# above survives that gap (it reaches lastResort), the INFO line did not —
# it never appeared in the running gateway's own journal, which is exactly
# the debugging surface review finding F6 (PR #307) added it for. Moved to
# a plain function, called once from MemoryCoordinator.start() — which only
# ever runs from hive_mind_proxy's real startup path, strictly after
# basicConfig has configured the root logger — instead of firing at import.
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

# ⛔ RELATION_ASSERTED_INHERITED IS GONE (`decision:1736`). It stamped a COPY of
# an edge some other record already had, so an inherited naming could be told
# apart from a first-write one. Nothing writes such a copy any more — belonging
# is derived on READ (`derived_belonging_cypher`) — so the stamp has no writer
# and, with the inherit-mode outbox branch retired, no reader either. The
# `'inherited'` edges already in the live graph are LEGACY DATA, retired by a
# one-time ledgered operation, not by framework code.

# Pool sizing is a SYSTEM budget, not just a coordinator knob: Postgres
# max_connections must cover this pool + REM (1 conn) + NREM (per-op) + the
# LISTEN connection + headroom. POOL_ACQUIRE_TIMEOUT bounds how long a request
# waits for a free connection — on expiry the request sheds (503 + Retry-After)
# via auth_middleware instead of hanging the gateway under concurrent load.
POOL_MIN = _env_int("POOL_MIN", 2)
POOL_MAX = _env_int("POOL_MAX", 20)
POOL_ACQUIRE_TIMEOUT = _env_float("POOL_ACQUIRE_TIMEOUT", 5.0)

# Bounded startup wait for Postgres (fact:1609): at boot the gateway can start
# before Postgres is accepting connections yet, and the unguarded pool create
# used to crash on the FIRST attempt -- Restart=on-failure then just replays
# the same race every time. PG_STARTUP_WAIT_S bounds how long start() retries
# a "not ready yet" connection failure before giving up and re-raising, so
# systemd's Restart= stays the real backstop instead of masking a boot-order
# problem as a crash loop.
PG_STARTUP_WAIT_S  = _env_float("PG_STARTUP_WAIT_S", 60.0)
# Clamped to a 0.1s floor: an operator-set 0 or negative value must not spin
# the retry loop with no pacing at all (a busy-loop against a down DB) — see
# _connect_with_startup_wait, which sleeps min(PG_STARTUP_RETRY_S, remaining).
PG_STARTUP_RETRY_S = max(0.1, _env_float("PG_STARTUP_RETRY_S", 2.0))

# The pgvector floor for `hnsw.iterative_scan` (decision:1584, fact:1583).
# Below it a selective axis filter (--project/--domain) can return ZERO rows
# past ~75k-300k records: HNSW returns its ef_search candidates and the SQL
# WHERE post-filter then removes them, and a selective filter can empty that
# set entirely. Migration 036's expression index fixes the Seq-Scan regression
# that shows up from ~15k rows; this session setting is the other half —
# see start()'s version probe and _init_connection below.
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

# ── The telemetry contract's tunables (v0.9.74, decision:1785) ─────────────────
# ⚠ EVERY DEFAULT BELOW IS UNMEASURED unless its comment says otherwise
# (fact:1338 — a proposed limit is a measurement claim in disguise, so it says
# plainly which it is). The same statement is repeated in .env.example, where an
# operator actually reads it.
#
#: Whole-payload cache for /memory/telemetry. UNMEASURED: chosen so the
#: monitor's 30 s browser re-fetch cannot stack two builds, not from a measured
#: build cost. The measured build cost on this corpus at 0.9.73 was 733 ms
#: median (7 samples) for the WHOLE payload.
TELEMETRY_CACHE_S = float(os.environ.get("TELEMETRY_CACHE_S", "15"))
#: Observation window for the encoder latency rings. UNMEASURED.
ENCODER_LATENCY_WINDOW = int(os.environ.get("ENCODER_LATENCY_WINDOW", "200"))
#: F9 — the Postgres-pool-wait and Neo4j rings get their OWN windows, defaulting
#: to the encoder one. They were sharing ENCODER_LATENCY_WINDOW, which meant a
#: name that said "encoder" silently sized three unrelated instruments: an
#: operator widening the encoder window to chase a slow reranker would have
#: moved the Neo4j percentiles underneath themselves at the same time, and
#: nothing in the name would have warned them. ⚠ Both UNMEASURED.
POOL_WAIT_WINDOW = int(os.environ.get("POOL_WAIT_WINDOW", str(ENCODER_LATENCY_WINDOW)))
NEO4J_LATENCY_WINDOW = int(os.environ.get("NEO4J_LATENCY_WINDOW", str(ENCODER_LATENCY_WINDOW)))
#: Encoder p95 above this raises a /health warning. Default None → DERIVED
#: per-encoder from backend_capability.<encoder>.ceiling_s, which IS measured
#: (the capability probe times a fixed representative payload). Set the env only
#: to pin a flat ceiling instead.
_ENCODER_WARN_RAW = os.environ.get("ENCODER_LATENCY_WARN_MS", "").strip()
ENCODER_LATENCY_WARN_MS = float(_ENCODER_WARN_RAW) if _ENCODER_WARN_RAW else None
#: An outbox row pending longer than this raises a /health warning and marks the
#: outbox dependency degraded. UNMEASURED — one hour is a round number, not an
#: observation about this pipeline's normal drain time.
OUTBOX_AGE_WARN_S = int(os.environ.get("OUTBOX_AGE_WARN_S", "3600"))
#: token_verify_failed climbing faster than this raises a /health warning.
#: UNMEASURED.
TOKEN_VERIFY_WARN_PER_MIN = float(os.environ.get("TOKEN_VERIFY_WARN_PER_MIN", "10"))
#: NREM is DEGRADED when it attempted at least this many folds in 24 h and
#: succeeded at none. UNMEASURED — the shape of the condition (attempted ≫
#: succeeded) is the ruling; the number is a floor to keep one unlucky fold from
#: raising an alarm.
NREM_FOLD_ATTEMPT_WARN = int(os.environ.get("NREM_FOLD_ATTEMPT_WARN", "5"))
#: Guards the `(rem_timing->>'ts')::double precision` cast in `_rem_telemetry`.
#: ⛔ NOT DECORATION. `rem_timing` is JSONB on a table with rows older than the
#: writer that fills `ts`, and ONE unparseable value aborts the whole query —
#: taking the REM section down with it, not just that row. Named rather than
#: inlined so the pattern is unit-testable: every row on the development corpus
#: is a clean number today (measured 2026-08-28, 188/188), so the guard is a
#: no-op HERE and would go untested exactly where it matters — a corpus that
#: has one bad row.
REM_TS_NUMERIC_RE = r"^[0-9]+(\.[0-9]+)?$"
#: Top-N for the two REGISTRY-BACKED breakdowns (projects, domains). ⚠
#: UNMEASURED as a value; what IS measured is that the previous hard-coded 12
#: truncated both on this corpus (38 projects, 15 domain names in use,
#: 2026-08-28). 50 is headroom above the registry, not a tuned number.
#: `agents`/`sources` keep their own top-12 — those are unbounded populations
#: where a top-N is the answer rather than a truncation.
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
# Defence-in-depth (second layer: the session opens with default_access_mode
# ="READ"). Every keyword is matched on WORD BOUNDARIES, never on a following
# whitespace character: `SET\s` let `SET  n:Label` (two spaces) through the
# guard entirely, because the `\b` closing the alternation then had to hold
# between two spaces. Live-reproduced bypass, fact:1734 (item 7 of the
# v0.9.69 post-first-write hardening plan).
#
# `\bSET\b` does NOT match a property name that merely CONTAINS "set"
# (`n.settings`, `n.asset`) — those are the cases the old comment feared and
# they still pass. It DOES over-block a bare `n.set`, an `AS set` alias, and
# any write keyword appearing inside a string literal; those are known,
# accepted over-blocks (a read-only guard erring towards refusal), pinned as
# such in tests/test_graph_route_guard.py.
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


def _axis_filter_predicate(start: int, project: "str | list[str] | None",
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

    `project` matches the top-level `metadata->>'project'` string against a SET
    of spellings (`= ANY`), and `domains` matches the top-level
    `metadata->'domains'` JSON array against the union of every filter entry's
    spellings. BOTH ARE ALREADY EXPANDED BY THE CALLER — `handle_search`
    resolves what the searcher typed to a canonical and hands in every stored
    spelling that means it (`expand_axis_spellings`). A str is accepted for
    `project` and treated as a one-element set, which is what an unresolvable
    value degrades to: the literal string, matching whatever carries it —
    exactly the behaviour before the expansion existed.

    ⚠ THE EXPANSION IS THE CALLER'S, AND MUST STAY THERE. It costs two registry
    reads; doing it here would put them inside a pure function called once per
    candidate query (five of them) and turn one lookup into five.

    `domains` matches with OR semantics (`?|` — true when ANY named domain is
    present) — the
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

    ⛔ THE CAP IS ON WHAT THE CALLER SUPPLIED, NEVER ON THE EXPANDED SET, and
    that distinction is load-bearing rather than pedantic. The cap exists to
    bound what an UNTRUSTED caller can make the database scan; the expansion is
    the SERVER's own answer, derived from its own registry, and is bounded by
    how many spellings that registry holds. Applying the cap after expansion
    would let a deployment that has recorded a few renames silently lose filter
    entries — a partial filter whose empty result reads as authoritative, which
    is the precise failure the cap was written to prevent.

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


# Module attribute (not a bare `time.monotonic` call inline) so a test can
# monkeypatch `coordinator._monotonic` directly instead of faking elapsed
# time by counting mocked sleeps.
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
        # pgvector extension version, and whether hnsw.iterative_scan applies
        # (decision:1584/fact:1583) — probed once in start(), BEFORE the pool
        # exists (see there for why). None/False until that probe runs.
        self.pgvector_version: str | None = None
        self.hnsw_iterative_scan: bool = False

        # ── The telemetry contract's in-process instruments (v0.9.74) ────────
        # ⛔ EVERY ONE OF THESE IS WRITTEN FROM A WORK PATH, so every write goes
        # through telemetry_instruments (which swallows) and never awaits. See
        # that module's docstring for the rule and why it is centralised there.
        #
        # The encoders had NO per-call latency at all before this: the only
        # number was the 600 s synthetic capability probe, which is a
        # projection, not an observation of what real callers experienced.
        self._embed_ring = LatencyRing(ENCODER_LATENCY_WINDOW)
        self._rerank_ring = LatencyRing(ENCODER_LATENCY_WINDOW)
        # Postgres pool wait — how long `_acquire` blocked before handing over a
        # connection. Saturation was only ever visible as the 503 it eventually
        # produced; this is the number that climbs BEFORE that.
        self._pool_wait_ring = LatencyRing(POOL_WAIT_WINDOW)
        # Neo4j query latency + failure counters. `cypher_rejected` is the
        # CALLER's fault (a query the database refused) and `tx_failures` is
        # ours; counting them together would make a user typo read as an outage.
        self._neo4j_ring = LatencyRing(NEO4J_LATENCY_WINDOW)
        self._cypher_rejected_total = 0
        self._neo4j_tx_failures_total = 0
        # ⛔ NO OUTBOX RING HERE, DELIBERATELY. Apply latency and drain rate are
        # DERIVABLE — `neo4j_outbox` already stores `created_at` and
        # `applied_at`, and `_apply_outbox_row` already stamps the second one.
        # Adding an in-memory ring would write a value a reader can reach by
        # query (decision:1032), and would be the WORSE copy: it resets on
        # restart, where the columns do not. Both numbers are SQL percentiles
        # over those two columns — see `_outbox_telemetry`.
        #
        # Registry census health (F1). ⛔ THE CENSUS QUERY WAS DEAD FROM THE DAY
        # IT SHIPPED — it selected FROM a `domains` table that does not exist —
        # and the refresher's bare `except Exception: registry = None` made that
        # indistinguishable from "not probed yet". Three pieces of state fix the
        # class, not just the query: a COUNTER the registry dependency reads, so
        # /health degrades when its own census cannot be read; the LAST GOOD
        # value plus when it was taken, so a transient failure does not blank a
        # number an operator was watching; and an ok/failed flag so the log line
        # fires ONCE PER TRANSITION rather than once per 60-second tick.
        self._registry_census_failures = 0
        self._registry_census_last_error: str | None = None
        self._registry_census_last_good: dict | None = None
        self._registry_census_as_of: str | None = None
        self._registry_census_ok: bool | None = None
        # Ingress refusal counters (0.9.69 shipped every one of these gates
        # UNINSTRUMENTED — a refusal was visible to the one caller who got it
        # and to nobody else). The seven keys are the contract's; several
        # aggregate a family of refusal codes, and telemetry_contract.py's own
        # note for each says exactly which.
        self._registry_refusals = Counter((
            "entity_reserved", "entity_confusable", "entity_unknown",
            "axis_conflict", "entities_not_allowed_on_judgement",
            "new_project_refused", "new_domain_refused",
        ))
        # Rerank outcome counters. The reranker is a separate process on the
        # search path with a FALLBACK, so its total failure is silent by
        # construction — it degrades to vector order and still answers. These
        # make that visible: a rising failure count against a flat success count
        # is a reranker that is up (it answers /health) but cannot serve.
        self._rerank_successes = 0
        self._rerank_failures = 0
        # When the fallback last fired. Stamped beside _rerank_failures at the
        # same increment (never derived from the log) so the pair can never
        # disagree — same contract as _credential_last_ts. ISO-8601 UTC, None
        # until the first fallback in this process.
        self._rerank_fallback_last_ts: str | None = None
        # Axis registry reads that FAILED (PR-C). A failed read is not a quiet
        # degrade: by-key resolution stops answering and a search filter matches
        # only the literal string, so the answer CHANGES while looking exactly
        # like the ordinary "that name is not registered" case. This is the only
        # signal that separates the two from outside one request — the paired
        # `filters_resolved.error` says it inside one. Same flat-additive shape
        # and reset-on-restart contract as the rerank pair above (fact:1314).
        self._axis_registry_read_failures = 0
        self._axis_registry_read_failure_last_ts: str | None = None
        # Payload-size instrument (fact:1441) — cumulative chars/docs actually
        # handed to the reranker across every search this process has served,
        # regardless of outcome (a fallback still counts what it WOULD have
        # sent). No paired "measured" counter: that count is already
        # _rerank_successes + _rerank_failures (see handle_search), and
        # writing it twice would duplicate a derivable value.
        self._rerank_payload_chars_total = 0
        self._rerank_payload_docs_total = 0
        # Observed-maximum tracker (operator ruling, 2026-08-23, on top of
        # fact:1441): a capacity SIGNAL needs the worst case a real search
        # actually produced, not an average -- a sum+count only ever gives a
        # mean. Updated at the SAME increment site as the pair above so it
        # stays aligned with the identical population (both outcome paths,
        # via `ranked`). Monotonic non-decreasing for this process's
        # lifetime BY DESIGN: it can only rise, so one outlier search pins it
        # until the next restart -- for a capacity signal that is the safe
        # direction (it never becomes less conservative), never a defect.
        self._rerank_payload_chars_max = 0
        # Backup quiesce: dedicated connection holding the EXCLUSIVE advisory lock
        # (None = not held), plus the TTL auto-resume task.
        self._quiesce_conn: Any = None
        self._quiesce_timer: asyncio.Task | None = None
        # ADR-018 consolidation health: cached snapshot refreshed by a background
        # task so /health stays DB-free. Defaults read as "unknown" until the
        # first refresh lands (stalled is never asserted on no data).
        # Whole-payload cache for /memory/telemetry (v0.9.74) + its single-flight
        # lock. See _telemetry_cached for why the lock is not optional.
        self._telemetry_cache: dict = {"snap": None, "ts": 0.0}
        self._telemetry_lock = asyncio.Lock()
        # Set by hive_mind_proxy at startup: a zero-argument callable returning
        # the blocks that live in the PROXY's module state (the llm_* family,
        # the capability/capacity snapshots, the resolved config). A callback
        # rather than an import because hive_mind_proxy imports THIS module —
        # importing back would be a cycle. None on a coordinator running without
        # a proxy (every unit test), and the sections simply do not appear.
        self.telemetry_extras_provider = None

        # ── The DB-free dependency snapshot /health reads (v0.9.74) ──────────
        # ⛔ EVERY STATE STARTS "unknown", NEVER "ok". A never-probed dependency
        # that reads healthy is the exact failure decision:374/fact:375 named,
        # and the one this block exists to avoid repeating for Postgres, Neo4j,
        # the outbox and the registry — four dependencies that, before 0.9.74,
        # had no representation on /health at all.
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
                                             # None (not 0) until the first refresh:
                                             # "not yet probed" must never read as
                                             # "verified clean" (decision 928).
                                             "graph_invalid_nodes": None,
                                             # Same rule for the identity gauge:
                                             # "not yet probed" must never read
                                             # as "upgrade complete".
                                             "project_identity": None,
                                             "domain_identity": None,
                                             "inference_busy": "unknown",
                                             # Same "not yet probed" rule as the
                                             # gauges above: None until the first
                                             # refresh, never a fabricated "ok".
                                             "gpu_probe": None, "fresh": False}
        self._consolidation_health_task: asyncio.Task | None = None
        self._alt_vector_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # M1 (PR #308 review): logged HERE, not at module import — this only
        # ever runs from the real gateway startup path, strictly after
        # hive_mind_proxy.py's logging.basicConfig() has configured the root
        # logger, so the INFO line is actually visible in the journal. See
        # log_encoder_endpoints()'s docstring for the import-time defect this
        # replaces (a caplog-forced test hid it: the fixture installs its own
        # handler, so it never observed the real, unconfigured logger).
        log_encoder_endpoints()
        # pgvector version probe — on a STANDALONE connection, BEFORE the pool
        # is created, so self.hnsw_iterative_scan is already correct by the
        # time _init_connection runs for the pool's own warm-up connections.
        # Probing AFTER create_pool() (e.g. via the first acquired connection,
        # the way the outbox recovery below does) would leave the first
        # POOL_MIN connections permanently without the SET below — they are
        # created and initialised during create_pool() itself, before any
        # query against them could have told us whether to apply it.
        #
        # ONE shared startup-wait deadline for BOTH the probe below and
        # create_pool further down (C3/C4, merger fix round) — see
        # _connect_with_startup_wait's docstring for why a separate budget
        # per call site would leave hnsw_iterative_scan silently disabled
        # forever instead of ever crashing start() when Postgres never comes up.
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
            # Log the roles ACTUALLY APPLIED, not the ones declared. A roster
            # identity is confined even with no AGENT_ROLES entry, and a startup
            # line showing only the file's contents would tell the operator a
            # read-only agent is unconfined at the exact moment they check.
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
        # Per-request timeout sized on the clamped input, overriding the shared
        # client's default. Embedding cost is superlinear in length, so a
        # constant that suits a short fact under-provisions a long decision or a
        # maximally-sized summary — the client default of 30s did not even cover
        # this function's own clamp.
        ceiling = embed_ceiling(len(text))
        for attempt in range(1, EMBED_RETRIES + 1):
            # ⛔ ONE ATTEMPT, ONE OBSERVATION. Timing the whole retry loop would
            # fold the backoff sleeps into "how long the embedder takes", which
            # is a statement about our retry policy, not about the encoder.
            _t0 = time.monotonic()
            try:
                r = await client.post(EMBED_URL, json={"input": text, "model": "bge-m3"},
                                      timeout=ceiling)
                r.raise_for_status()
                vec = r.json()["data"][0]["embedding"]
                # `safe` (not a bare call) because the recorder must tolerate a
                # partially-constructed or stubbed owner too: an instrument that
                # can AttributeError is an instrument that can break the embed
                # path, which is the one thing it may never do.
                safe(lambda: self._embed_ring.record(
                    (time.monotonic() - _t0) * 1000.0, payload_chars=len(text)))
                return vec
            except Exception as exc:
                safe(lambda: self._embed_ring.record_error())
                if attempt == EMBED_RETRIES:
                    # The encoder URL is operator-supplied and may carry
                    # userinfo; an httpx error renders the full URL, and this
                    # message is the client-visible 503 body — scrub it.
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
        for attempt in range(1, EMBED_RETRIES + 1):
            _t0 = time.monotonic()
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
                # A batch is ONE call and is recorded as one — its payload is
                # the whole batch, which is what the ceiling was sized on.
                safe(lambda: self._embed_ring.record(
                    (time.monotonic() - _t0) * 1000.0, payload_chars=total_chars))
                return [d["embedding"] for d in ordered]
            except Exception as exc:
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
        # F6/F7: the outbox apply is the OTHER Neo4j caller, and B2 asked for
        # both. Timing only the graph route would have made `neo4j.query_p95_ms`
        # describe read-only ad-hoc Cypher while the write path — the one that
        # actually blocks the pipeline — stayed invisible; counting only the
        # graph route's failures would have made `tx_failures_total` read as
        # "Neo4j is fine" through a Neo4j outage that was failing every apply.
        # One clock read on either side, and the recorders cannot raise.
        _t0 = time.monotonic()
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
            safe(lambda: self._neo4j_ring.record(
                (time.monotonic() - _t0) * 1000.0))
            log.debug("outbox: applied pg_id=%d (outbox_id=%d)", pg_id, outbox_id)
        except Exception as exc:
            # OURS, not the caller's — the same discriminator the graph route
            # uses: `cypher_rejected_total` is a query the DATABASE refused
            # because the CALLER wrote it wrong, and there is no caller here.
            safe(lambda: setattr(self, "_neo4j_tx_failures_total",
                                 self._neo4j_tx_failures_total + 1))
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
            # ⛔ NO INHERITED SECTIONS. A decision's DOMAIN_OF edges are exactly
            # the ones the operator asserted on THIS record, written by the
            # projection above; a decision that named none carries none
            # (`decision:1736`). Its belonging is still ANSWERABLE — read side,
            # by traversal (`derived_belonging_cypher`) — it is simply not
            # materialised, because a value a reader can reach by walking is a
            # value nothing should write twice (`decision:1032`).
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
                # ⛔ A RETROSPECTIVE'S SAVE WRITES NO DOMAIN_OF EDGE — neither
                # onto the decision it judges nor onto itself (`decision:1736`).
                # It used to do both: it re-ran the decision's inheritance
                # (because a retrospective is the moment an ungrounded decision
                # first reaches facts) and then took the decision's sections for
                # itself. Both were POST-FIRST-WRITE MUTATIONS of somebody's
                # belonging axis, inferred rather than asserted — the class
                # `fact:1671` forbids. The verdict is reached from its decision
                # and its facts on READ, and `derived_belonging_cypher` is where
                # that answer now lives.
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
        `backfill_domain_of.py` enqueues.

        `domains: [names]`  the record's OWN, ASSERTED sections, resolved through
                            the registry the same way first write resolves them.
                            The only mode there is.

        ⛔ THE `inherit: true` MODE IS GONE (`decision:1736`). It re-derived a
        judgement's sections from what it grounds in and wrote them as edges
        stamped `inherited` — a materialised copy of a value the reader can
        reach by walking, and a mutation of a record's belonging axis after its
        first write. Nothing derives belonging into an edge any more; the read
        side answers it (`derived_belonging_cypher`).

        ⚠ A LEGACY `inherit` ROW IS DROPPED, NOT FALLEN THROUGH. Rows enqueued
        before this shipped may still be pending, and they carry no `domains`
        key — so letting one reach the explicit branch below would DELETE every
        DOMAIN_OF edge the record has and write nothing back. Recognising the
        retired mode and dropping the row is the difference between a no-op and
        silent data loss.

        Narrow, like `project_of` and for the same reason: replaying an ordinary
        fact row would re-run its `MENTIONS` merges and resurrect enrichment
        edges a later sweep deliberately deleted.

        ⚠ IT REPLACES THE SET IT MANAGES: it deletes EVERY DOMAIN_OF edge, then
        writes what Postgres says. The record's own assertion is the whole
        answer — that is the P19 lesson on a MULTI-valued axis, "the graph
        mirrors the current answer rather than keeping every answer".

        One-shot, DELETED on success: it carries no dream lifecycle and must
        never be counted as working-set backlog.
        """
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
            "outbox: backfilled %s pg_id=%s edges=%d (outbox_id=%d, row deleted)",
            ONT.domain_of, pg_id, written, outbox_id,
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

    # ── Deferred axis registration (P4′, v0.9.72) ────────────────────────────
    #
    # ⛔ NO REGISTRY ROW IS WRITTEN BY A REFUSAL THAT COULD HAVE FIRED EARLIER.
    # The project and domain gates ACCEPT a declared-new name where they always
    # did — the position of that acceptance is the rule (P9) and has not moved
    # — but they now record an INTENT instead of inserting.
    # `_commit_axis_registrations` performs the inserts in `handle_save`,
    # immediately before `_entity_commit_mints`: after every validation that
    # can still 400, before the embed. It is the same ordering rule S-4 gave
    # the entity mint (`decision:1413`) and for the same reason — a write that
    # survives the refusal of the save that requested it is a lie in the
    # registry.
    #
    # ⚠ P4′ IS "COULD HAVE FIRED EARLIER", NOT "NEVER". Stating it as an
    # absolute was wrong (review R1) and hid a real leak for one release
    # candidate. Three exits remain downstream of the commit BY CONSTRUCTION —
    # the hard-mandate embed's 503, the in-transaction `axis_conflict` 409, and
    # the commit's own 503 — each enumerated in
    # `_commit_axis_registrations`'s docstring with why it cannot be hoisted.
    # Anything that is NOT in that list and still fires after the commit is a
    # defect, and the list is how you tell.
    #
    # The intents live in the axis REPORT dict, the out parameter both gates
    # already carry, keyed under `_PENDING_KEY`. `_commit_axis_registrations`
    # POPS it, so it can never reach the save response.
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
            # ⛔ A CODING ERROR, AND IT RAISES (v0.9.72, R4). The first version
            # logged a warning and returned, which meant a caller that forgot
            # the report got a 200 for a save whose project was never
            # registered — the graph would then carry a project the registry
            # does not have, which is the exact divergence migration 027
            # exists to remove, reintroduced by an omission nobody would see.
            # There is one production caller and it always passes a report, so
            # this can only fire in new code, which is when it is cheap to fix.
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

        # P9 — the second submission is ACCEPTED, in any of its three forms: pick
        # a proposal (now a registry hit), declare a new project, or park it on
        # the sentinel. There is deliberately NO round counter on the server: the
        # bound comes from those three forms all succeeding, not from per-caller
        # state a gateway would have to keep and expire. What the gateway never
        # does, however many times it is asked, is accept an unregistered name.
        #
        # ⛔ IT IS ANSWERED HERE — AFTER THE REGISTRY, BEFORE EVERY OTHER STEP —
        # AND THE POSITION IS THE RULE. A caller that DECLARES a new project is
        # asserting that no such project exists. When the name is a retired
        # spelling, or a separator/case variant of a live or retired one, that
        # assertion is FALSE and the caller must be told, loudly, with the
        # spelling to use. Resolving it quietly would store the record correctly
        # and destroy the only signal that an agent believes it is creating
        # projects that already exist — which is how every retired spelling in
        # this registry arrived. A save that makes no such claim gets the
        # opposite treatment below: its spelling is simply resolved, because it
        # never claimed anything about the registry in the first place.
        #
        # ⚠ It sits AFTER the exact-registry check above, and only there: a
        # `new_project` flag on a name that is already registered verbatim is a
        # redundant flag, not a false claim, and has always been accepted.
        if metadata.get("new_project") is True:
            # ⛔ A DECLARATION IS NOT A DEFENCE. The agent that sets this flag is
            # the same agent that makes the spelling error, so accepting the
            # claim on its own guards nothing: the operator says "go ahead with
            # this idea", meaning THIS project, and a plausible variant becomes a
            # second one. So the claim faces the checks below before it registers.
            refusal = await self._new_project_refusal(supplied, metadata)
            if refusal is not None:
                return refusal
            # ⛔ ACCEPTED HERE, WRITTEN LATER (P4′, v0.9.72). This used to INSERT
            # the registry row on the spot, which made the acceptance and the
            # write the same event — and every 400 still ahead of it (an
            # axis_conflict on the content hash, a mint Postgres refuses) and the
            # 503 `registry_unavailable` then left a project row behind for a
            # record that was never stored, under a refusal whose own text says
            # "Nothing was written". The acceptance is unchanged; only the write
            # moved, to `_commit_axis_registrations`.
            self._defer_project_registration(report, supplied, agent_id, metadata)
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
            self._rewrite_project(metadata, supplied, canonical)
            if report is not None:
                report["project_resolved"] = {
                    "supplied": supplied, "canonical": canonical,
                    "via": VIA_ALIAS,
                }
            return None

        # Steps 3 and 4 — THE SAME NAME, SPELLED DIFFERENTLY (decision:1015,
        # fact:1047, fact:1490). A registry that answers only exact strings makes
        # `Shared_Memory` and `shared-memory` two unrelated events: one is a
        # project and the other is a stranger, and the caller is asked to pick a
        # proposal that is character-for-character what it already meant. The key
        # is what fact:1047's spelling guard has always compared on — this simply
        # stops the guard being the only thing that knows it.
        #
        # It runs LAST because exact answers must never be reachable through a
        # key: a value already on file is answered by itself, and only a value
        # that is on file NOWHERE gets normalised.
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
        # ⛔ FIRST, AND BEFORE ANY QUERY. It needs no registry: a name with no
        # key is not a near-match of anything, cannot be confirmed distinct from
        # anything, and has nothing to propose.
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
            # ⚠ THE SPELLING CHECK RUNS OVER THE WHOLE REGISTRY, NOT OVER `near`.
            # It used to read the trigram neighbours, which silently made an
            # EXACT rule conditional on a FUZZY one: a separator/case variant was
            # refused only when it also scored above the similarity floor.
            # Measured live — `testing` vs `Test_Ing` scores 0.545 against a
            # floor of 0.6 — so a pure spelling variant registered as new,
            # which is precisely the event this guard exists to prevent.
            all_names = [r["name"] for r in await conn.fetch(PROJECT_NAMES_SQL)]
            # ⚠ AND OVER THE RETIRED SPELLINGS TOO. A name that was aliased away
            # is a name this deployment has ALREADY adjudicated, so a variant of
            # it is the same mistake as a variant of a live name — and it is the
            # likelier one, because the retired spelling is what the machine that
            # still carries the old folder name will send. The refusal points at
            # the project the alias resolves to, never at the alias, because the
            # alias is not somewhere a record may be saved.
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

        # ⛔ NEW-PROJECT MODE (v0.9.72). The project gate above now DEFERS the
        # registry row for a declared-new project, so by here that project has
        # no row — and `_project_identity` RAISES on a missing row, which would
        # turn the ordinary "new project plus its first section" save into a
        # false 503 `registry_unavailable`. So the pending intent is read first
        # and answers the identity question instead: a project being registered
        # by THIS save has no sections yet, which is a fact about the registry,
        # not a failure to read it.
        new_project = self._pending_project(report) == project

        # ⛔ STRICT SINCE v0.9.69 (item 6, ruled R3). This used to treat a None
        # identity as "accept the record with its domain unvalidated and
        # unlinked" — which turned an unreadable registry into a SILENTLY
        # half-filed record: stored, searchable by text, and reachable from no
        # axis. `_project_identity` now RAISES instead, and handle_save turns
        # that into a 503 `registry_unavailable` — the same answer the hard
        # embedding mandate gives when the other half of a save cannot be
        # completed. A save that cannot be filed correctly is not saved.
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

        # The `new_domain` claim is answered HERE, after the registry and before
        # every other step — the project axis' ordering rule, for the same
        # reason. Declaring a section NEW while naming a retired spelling of one,
        # or a separator/case variant of a live or retired one, is a false claim
        # about the registry, and the caller is told rather than quietly
        # corrected.
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

        # Steps 3 and 4, scoped to this project's sections — the project axis'
        # by-key resolution, on the axis where it matters MORE. A section name is
        # an ordinary word typed by different people at different times, so
        # `graph-quality` and `graph quality` are the same section far more often
        # than `Alpha-Service` and `alpha service` are the same project. Same
        # ordering rule: exact answers first, and a `new_domain` declaration is
        # answered above so a false claim is told rather than silently resolved.
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
            # The project axis' rule, for the same reason: a section that was
            # aliased away has already been adjudicated on this project, so a
            # variant of the retired spelling is the same mistake as a variant
            # of a live one.
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

    # ── Entity vocabulary ingress (fact:1375, migration 033) ──────────────────
    #
    # The whole gate lives in `_entity_ingress_error`, called from both writers
    # of caller-supplied entity names — handle_save (facts and decisions share
    # this generic path) and handle_retrospective (its own endpoint, its own
    # `entities` field). See that method's docstring for the full rule; the
    # two methods below are its DB-facing primitives, kept separate so a test
    # can stub either one without reimplementing the gate's control flow.

    #: Which refusal CODE lands in which contract counter. Several counters
    #: aggregate a family: the contract documents one key per KIND of refusal an
    #: operator acts on, not one per error string — "a project name was refused"
    #: is the actionable fact, and which of the three naming rules refused it is
    #: in the refusal the caller already received. Any code not listed here is
    #: deliberately uncounted rather than silently folded into a neighbour.
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

        existing_project = resolve_project(stored)
        if axis_key(existing_project) != axis_key(project):
            # The keys differ — which is what a RENAME looks like from here.
            # Resolve the stored spelling once (one hop, never a walk, A3)
            # before calling it a conflict.
            resolved_stored = (await self._resolve_project_alias(existing_project)
                               if existing_project else None)
            if resolved_stored is None or \
                    axis_key(resolved_stored) != axis_key(project):
                return _refusal("project", existing_project, project)

        existing_domains = resolve_domains(stored)
        # ⚠ DOMAINS ARE COMPARED BY KEY BUT NOT ALIAS-RESOLVED. A domain alias
        # resolves only INSIDE a project identity, and looking one up here would
        # put `_project_identity` — which now RAISES on a registry blip — on the
        # re-save path, turning a transient database problem into a refused
        # save. So a section RENAME (a genuinely different name, not a
        # respelling) still conflicts on re-save until that is designed; it is
        # recorded as a known gap rather than closed with a call that can fail.
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
        """400 when a DECISION or RETROSPECTIVE carries entities — or None
        (item 3, v0.9.69; ruled R1, grounded on `decision:1664`).

        ONLY FACTS CARRY ENTITIES. A judgement reaches its topics by walking to
        the facts it rests on, which is why its `entities` never became a
        MENTIONS edge in the first place — but the value was still validated,
        still MINTABLE through `new_entities`, and still returned by search,
        which made the judgement path a second, unvetted faucet into the
        vocabulary (`fact:970`, `fact:1734` A(3)).

        An EMPTY list is accepted PERMANENTLY, not for one release: the shipped
        client's `build_decision_metadata` always emits `entities: []`, and a
        field that is present-and-empty asserts nothing.

        Refuses BEFORE any write — nothing reaches Postgres, nothing mints.
        """
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
                f"a {kind} may not carry {offending} (decision:1664). Only "
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
        # ⚠ RETURNED ONLY BESIDE A REFUSAL, where the caller never reads it. A
        # SUCCESS path must build its own `canonical` — see the no-candidates
        # return below for what happens when it does not.
        empty_plan: dict = {"resolved": {}, "to_mint": [], "canonical": []}

        raw_entities = metadata.get("entities") or []
        if len(raw_entities) > ENTITY_LIST_MAX_LEN:
            return (self._entities_list_too_long_rejection(
                "entities", len(raw_entities)), empty_plan)
        for e in raw_entities:
            if isinstance(e, str) and len(e) > ENTITY_NAME_MAX_LEN:
                return self._entity_name_too_long_rejection(e), empty_plan

        # RESERVED VOCABULARY (item 2a, v0.9.69) — a schema word or an axis
        # declaration, on the RAW names, because `sanitize_entity_name` rejects
        # exactly these and they would otherwise never become candidates: the
        # name would reach Postgres verbatim and be dropped at the graph, which
        # is the silence this refusal replaces. `new_entities` is swept too, so
        # a reserved name is refused whether or not it is also a mint request.
        # ⚠ `new_entities` has not had its SHAPE validated yet (that check needs
        # `candidates`, below) — so only sweep it when it is already a list.
        # A malformed one still gets its own `new_entities_invalid` refusal.
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
            # ⛔ THE PLAN STILL HAS TO SAY WHAT WILL BE STORED. This returned
            # `empty_plan`, whose `canonical` is hard-coded `[]` — but a record
            # whose entities are ALL shape-noise (`["254"]`, I3's gate-exempt
            # class) stores those names VERBATIM. The re-save axis check then
            # compared a stored `["254"]` against an incoming `[]` and refused
            # every re-save of that record with a 409, permanently, over an
            # axis that never moved. `_canonical_entity_list(metadata, {})` is
            # the same answer `_rewrite_entities` produces for an empty
            # `resolved` — it leaves the list exactly as it is — so the two
            # cannot disagree.
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

            # ⛔ THE UNNAMEABLE CHECK, HERE AND NOT AT THE MINT (v0.9.72, R1).
            # Cheapest of the mint validations and the only one needing no
            # query at all — the project and domain axes put their twin in the
            # same position, first, for the same reason. Checked over
            # `mint_requested` rather than over `to_mint`: the two coincide
            # (a name normalizing to nothing can never be IN the vocabulary,
            # because the same trigger refused it there too), and this way the
            # refusal fires before the reserved-project query and the batched
            # resolution as well as before every write.
            for sanitized in sorted(mint_requested):
                unnameable = self._new_entity_unnameable_refusal(sanitized)
                if unnameable is not None:
                    return unnameable, empty_plan

        # A PROJECT NAME IS AN AXIS, NEVER AN ENTITY (item 2b, v0.9.69;
        # `fact:1215`). Compared on `axis_key`, so `Shared_Memory`,
        # `shared-memory` and `SHARED MEMORY` are one answer — the same key the
        # registry itself is unique on. It runs BEFORE resolution deliberately:
        # a name that is a project must be refused whether or not the
        # vocabulary already knows it, because the legacy vocabulary DOES carry
        # such names and resolving one would launder it back in.
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

            # E1 — a name about to be minted must not be a typo of one the
            # vocabulary already holds. Last of the validations, because it is
            # the only one that needs to know WHICH names will be minted.
            confusable = await self._entity_confusable_error(to_mint, metadata)
            if confusable is not None:
                return confusable, empty_plan

        plan = {
            "resolved": resolved,
            "to_mint": to_mint,
            # A name about to be minted canonicalizes to ITSELF — the one
            # exception is the mint race `_entity_vocab_mint` arbitrates, which
            # can only substitute an equivalent spelling of the same key.
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
                # ⚠ NO LONGER THE FIRST LINE OF DEFENCE, and kept anyway. The
                # normalizes-to-nothing case is refused by
                # `_entity_ingress_validate` now (v0.9.72, R1) — before the
                # axis registrations commit — so this fires only if the
                # database refuses a mint the gateway-side twin accepted:
                # a `[:alnum:]` locale difference, or a future trigger rule
                # Python does not know about. The database is the authority on
                # its own writes; this is what turns its RAISE into a 400
                # rather than a 500.
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
        # What the two axis gates REWROTE, collected for the response. A save
        # that is rewritten and not told about it is a save whose caller keeps
        # sending the same spelling forever and never learns where its record
        # actually landed — the same reasoning that put `entities_rewritten` on
        # this response.
        axis_report: dict = {}

        # ⛔ ENTITY VALIDATION RUNS FIRST — BEFORE THE PROJECT AXIS, and the
        # position is the rule (item 8, v0.9.69; `fact:1734` A(4)).
        # `_project_ingress_error` used to REGISTER a declared-new project as
        # its acceptance, and every entity refusal below could still 400 the
        # save afterwards — so a save refused for an unknown entity left a
        # project registered that no record ever named. ⚠ v0.9.72 closed the
        # same hole from the other side (P4′: the registry write is deferred to
        # `_commit_axis_registrations`), which makes this ordering no longer
        # the ONLY thing standing between an entity refusal and a stray
        # project row — it is kept because it is still the right order: a save
        # is refused on the cheapest, most local check first. Validation writes
        # NOTHING; the mint it plans is committed further down, still last
        # (S-4, decision:1413).
        entities_field = metadata.get("entities", [])
        if not isinstance(entities_field, list):
            return web.json_response(
                {"status": "error", "message": "metadata.entities must be a list"},
                status=400,
            )
        # ⛔ ONLY FACTS CARRY ENTITIES (item 3, v0.9.69; ruled R1 on
        # decision:1664). A judgement naming any is refused here — before the
        # gate, before the axes, before any write — and never reaches the
        # vocabulary at all. An empty list is accepted permanently.
        if is_judgement_type(metadata.get("type")):
            judgement_error = self._judgement_entities_error(metadata)
            if judgement_error is not None:
                self._count_refusal(judgement_error)
                return web.json_response(judgement_error, status=400)
            # ⛔ NOTHING IS ADDED TO `metadata` HERE. An earlier spelling called
            # `setdefault("entities", [])`, which PERSISTED a key the caller
            # never sent — the storage half of the very rule this gate enforces
            # ("a judgement carries no entities in any store"). Every reader
            # below already defaults it: `metadata.get("entities", [])` feeds
            # the locks loop, and the outbox row omits the key outright.
            entity_plan: dict = {"resolved": {}, "to_mint": [], "canonical": []}
        else:
            entity_refusal, entity_plan = await self._entity_ingress_validate(metadata)
            if entity_refusal is not None:
                self._count_refusal(entity_refusal)
                return web.json_response(entity_refusal, status=400)

        # ⛔ A JUDGEMENT'S PROJECT IS ONE VALUE, AND IT IS `decision.project`
        # (v0.9.72, `fact:1757`). The client's `build_decision_metadata` never
        # set the TOP-LEVEL key, so `save_artifact` filled it from the cwd walk
        # — which yields `.claude` under `~/.claude/...` and nothing under `~`.
        # The gateway stored what it was given, and 12 live decisions ended up
        # with a top-level project their own decision blob disagreed with. The
        # client now sets both; this makes the SERVER independent of that, for
        # every client that has not been updated and every one that never will.
        #
        # ⚠ ONE DIRECTION ONLY. The blob is what the operator asserted through
        # `--project`; the top level is what a walk DERIVED. The assertion
        # wins, always — never the reverse.
        #
        # It runs BEFORE `_project_ingress_error` so the registry gate answers
        # about the value that will actually be stored. The pre-overwrite value
        # is kept here because the ingress derives `project_resolved` by
        # comparing what it was HANDED against the canonical: rewriting first
        # and letting it report would hide the rewrite entirely.
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

        # Disclose the rewrite the caller did not ask for. When the ingress
        # ALSO moved the name (an alias, a key variant), its canonical is the
        # value actually stored, so it wins for `to` — the caller is told one
        # destination, not two.
        if decision_project_rewrite is not None:
            _ingress = axis_report.get("project_resolved") or {}
            if _ingress.get("canonical"):
                decision_project_rewrite["to"] = _ingress["canonical"]
            axis_report["project_resolved"] = {
                **_ingress, **decision_project_rewrite}

        # The domain axis (028), AFTER the project — a section cannot be resolved
        # before the project that contains it, and by here the project name is
        # canonical, so an aliased project reaches the right registry. A record
        # naming no domain passes straight through: most do, and that is correct
        # rather than untagged.
        #
        # ⛔ 503, NOT 500 AND NOT A SILENT ACCEPT (item 6, ruled R3). The domain
        # axis resolves through the project's registry IDENTITY, so an
        # unreadable registry means this record cannot be FILED — and a record
        # that saves without its axes is invisible to every reader who navigates
        # by them. Same answer as the hard embedding mandate, and the same
        # reason: half a save is not a save.
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

        # Shape already validated (and the ENTITY GATE'S VALIDATION HALF
        # already run) above, before the project axis.
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

        # ── P1: a re-save never moves a record's axes (item 4, v0.9.69) ──────
        #
        # The hash moved UP to here, ahead of the mint and the embed. `content`
        # is fixed by this point (nothing below rewrites it), so computing it
        # earlier changes no value — it just lets the conflict be found before
        # anything is written. What it PROTECTS is exactly what used to be
        # spent on a save that was going to be refused anyway: a vocabulary
        # mint, and a GPU embedding.
        #
        # ⚠ THIS CHECK IS ADVISORY, and deliberately so: the AUTHORITATIVE one
        # is the `FOR UPDATE` re-read inside the transaction below, which is
        # the only place a concurrent save of the same content cannot slip
        # between the read and the INSERT. This one exists for the cost, not
        # for the correctness — the same "cheap indexed pre-check before the
        # GPU" shape `handle_retrospective` uses for its target pg_id.
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
                entity_plan.get("canonical") or [], is_judgement)
            if conflict is not None:
                self._count_refusal(conflict)
                return web.json_response(conflict, status=409)

        # ⛔ THE AXIS REGISTRY WRITES LAND HERE (P4′, v0.9.72) — after every
        # 400-capable validation above and after the 409 axis-conflict check,
        # immediately before the entity mint and the embed. The project and
        # domain gates ACCEPTED these names where they always did; this is
        # where the acceptance becomes a row, so a save refused anywhere above
        # leaves `projects` and `project_domains` exactly as it found them.
        # Same ordering rule as the mint below, same reason (decision:1413).
        try:
            await self._commit_axis_registrations(axis_report, agent_id)
        except ProjectIdentityUnavailable as exc:
            # The project row was just written and its id could not be read
            # back, so its sections cannot be filed. Same answer the domain
            # gate gives: a record that cannot be filed correctly is not saved.
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

        # Entity vocabulary ingress gate (fact:1375, migration 033) — the
        # COMMIT half. Every refusal the gate can produce already fired above,
        # before the project axis (item 8, v0.9.69); what is left here is the
        # mint itself plus the in-place rewrite of `metadata['entities']` to
        # the CANONICAL spelling, before anything downstream — locks, PG
        # metadata, the outbox row, entity_registry, the graph — sees this
        # list, so `entities` is re-read below.
        #
        # ⛔ S-4 (security review fact:1412, ruled decision:1413): the MINT is
        # still LAST among the writes — after entities_provenance above, after
        # the axis gates, after the axis registrations just committed, and
        # immediately before the hard-mandate embedding call. A mint is a real
        # write (to entity_vocabulary, no shared transaction with the record
        # insert), so anything that can still 400 the save must run BEFORE this
        # call, or a mint survives the very refusal that requested it. Only the
        # hard-mandate embedding 503 (unavoidably later — it needs the final
        # content) can still race a mint; that residual is accepted by ruling,
        # not fixed here — see the method's own docstring.
        entities_before = list(entities)
        entity_error = await self._entity_commit_mints(
            metadata, agent_id, entity_plan)
        if entity_error is not None:
            self._count_refusal(entity_error)
            return web.json_response(entity_error, status=400)
        entities = metadata.get("entities", [])
        # S-6 (security review fact:1412): a caller must be able to see what
        # was actually stored when the gate changed anything — an alias/
        # case-variant rewrite, or a mint substituting a race's winning
        # spelling (S-6/S-12) — never silently. `None` when nothing moved,
        # so the ordinary case (no entities, or every name already
        # canonical) adds no noise to the response.
        entities_rewritten = entities if entities != entities_before else None

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
                    # P1, AUTHORITATIVELY (item 4, v0.9.69). The pre-check
                    # above saved the mint and the embed; THIS one is the
                    # guard, because only a row lock stops a concurrent save
                    # of the same content from landing between a read and the
                    # INSERT. `FOR UPDATE` on a hash that matches nothing
                    # locks nothing and costs an index probe.
                    prior = await conn.fetchval(
                        "SELECT metadata FROM technical_docs"
                        " WHERE content_hash = $1 FOR UPDATE",
                        content_hash,
                    )
                    if prior is not None:
                        conflict = await self._axis_conflict_error(
                            prior, incoming_project, incoming_domains,
                            entities, is_judgement)
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
                            # ⛔ FACTS ONLY (item 3, v0.9.69). A judgement
                            # carries no entities at all now, so the key is
                            # OMITTED rather than sent empty — the projection
                            # already defaults it, and a key that is always
                            # empty is a promise the row should stop making.
                            **({} if is_judgement_type(metadata.get("type"))
                               else {"entities": entities}),
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
            # S-6 (security review fact:1412): non-null only when the entity
            # vocabulary gate changed something the caller sent — an alias/
            # case-variant rewritten to its canonical, or a name substituted
            # by a mint race's winner (S-6/S-12) — so a caller can always see
            # what was actually stored, never infer it.
            "entities_rewritten": entities_rewritten,
            # The axis twins of `entities_rewritten` (PR-C). Non-null only when
            # the gateway stored the record under a DIFFERENT spelling from the
            # one supplied — a retired name resolved through an alias, or a
            # separator/case variant resolved on the axis key. A caller sending
            # the canonical value already sees null and has nothing to reconcile.
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

        # ⛔ THE ENTITY GATE NO LONGER RUNS HERE (item 3, v0.9.69; ruled R1 on
        # decision:1664). A retrospective is a JUDGEMENT: it reaches its topics
        # by walking to the facts it is grounded in, so an entity named on one
        # was never written to the graph — it was merely validated, mintable
        # through `new_entities`, and returned by search, which made this
        # endpoint a second unvetted faucet into the vocabulary (`fact:970`).
        # Naming any is now a refusal, checked BEFORE any write; an empty list
        # is accepted permanently, so an unchanged client still saves.
        #
        # `metadata['entities']` stays initialised (to the empty list it now
        # always is) because the locks loop and the response below index it.
        judgement_error = self._judgement_entities_error(metadata)
        if judgement_error is not None:
            self._count_refusal(judgement_error)
            return web.json_response(judgement_error, status=400)
        metadata.pop("new_entities", None)
        entities_rewritten = None

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
                            # ⛔ No `entities` key: a retrospective carries none
                            # (item 3, v0.9.69). It never produced a MENTIONS
                            # edge; the empty list was a promise this row should
                            # stop making.
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

    # Anchors whose belonging has to be DERIVED rather than read off their own
    # edges. A fact's sections are its own bare edges and need nothing;
    # a CommunitySummary has no belonging edges at all.
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
                # means "something asserted this", and INHERITANCE was a
                # something: it wrote MENTIONS with asserted_by='inherited'. Those
                # edges are LEGACY now (no writer since `decision:1736`) but they
                # are still in the graph, so the ordering rule stands on the data
                # that provoked it. A decision with 31 such edges buried its own
                # HAD_OUTCOME (stamp null)
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
        # A judgement hit also carries WHERE IT BELONGS, derived (`decision:1736`).
        # Facts are untouched: their belonging is their own bare edges, already
        # above. Skipped entirely when no judgement label is anchored on, so a
        # summary expansion pays for nothing.
        if any(lbl in self._DERIVED_BELONGING_LABELS for lbl in anchor_labels):
            belonging = await self._derived_belonging(session, [pg_id])
            if pg_id in belonging:
                ctx.append(self._belonging_entry(belonging[pg_id]))
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
                # Lifecycle edges rank by TYPE — see the note on the
                # single-anchor query above; inheritance stamped MENTIONS and
                # those legacy edges remain, so keying the first sort on
                # asserted_by buried HAD_OUTCOME.
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
        # Derived belonging for the judgement anchors, in ONE more round trip
        # for the whole batch — see `_derived_belonging` on why it is not per
        # hit. Anchors that are facts simply come back with no row.
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
        # ⛔ A FAILED REGISTRY READ IS DISCLOSED, NOT SWALLOWED. When it fails the
        # filter degrades to the literal string, which is indistinguishable from
        # a genuinely unregistered name — so the reason is carried into the
        # response beside the resolution it silently changed. One key for both
        # axes: what a reader needs to know is that this answer is not
        # authoritative, and which registry could not be read is in the value.
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
            # A READER DEGRADES, never 500s (item 6, ruled R3). Without an
            # identity the domain half of the filter resolves to nothing —
            # which looks exactly like "nobody registered that section", so
            # the degrade is REPORTED (counter + `filters_resolved.error`)
            # rather than left to be read as an empty answer.
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
                # ONE flat array for the `?|` operator, which is OR over the
                # whole set — so every spelling of every requested section goes
                # in together and the filter's OR semantics are unchanged.
                for spelling in matched:
                    if spelling not in values:
                        values.append(spelling)
            domain_values = values
            resolved["domains"] = entries

        if errors:
            # A STRING, not a boolean, and not a nested object: it says which
            # registry could not be read, it is absent on the ordinary path, and
            # it never restructures the keys beside it (fact:1314). The counted
            # twin is telemetry `axis_registry_read_failures_total`.
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

        # Resolve what the searcher TYPED to what the corpus HOLDS — once, here,
        # for every candidate query below. The cap above is deliberately applied
        # to the SUPPLIED list, before this line: what is bounded is what an
        # untrusted caller can ask for, not what the server's own registry adds.
        project_values, domain_values, filters_resolved = \
            await self._resolve_search_filters(project, domains_filter)

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
                    3 + len(vis_params), project_values, domain_values, since_dt)
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
                return web.json_response(_with_filters_resolved({
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
                }, filters_resolved))

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
                return web.json_response(_with_filters_resolved(
                    {"status": "success", "results": []}, filters_resolved))

            ids      = [r["id"]       for r in candidates]
            contents = [r["content"]  for r in candidates]
            metas    = [_coerce_jsonb_obj(r["metadata"]) for r in candidates]
            # .get: tolerant of pre-migration schemas (and test stubs) where the
            # created_at column is absent — recency simply degrades to off.
            createds = [r.get("created_at") for r in candidates]

            # Rerank — direct to RERANK_URL (env-derived) to avoid a circular proxy call.
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
            # Payload-size instrument (fact:1441). fact:1441's cross-host
            # capacity sweep on a CPU-only test host produced an
            # UNDER-DETERMINED finding: the harness never recorded how many
            # characters were actually sent to the reranker per search, so
            # per-request FIXED OVERHEAD (embedding, two DB round trips,
            # candidate batching, HTTP) could not be separated from
            # DOCUMENT-LENGTH cost — and the capacity model's `chars / mu`
            # term, with no fixed-overhead component, turns from conservative
            # to OPTIMISTIC below roughly 2000 chars. This closes exactly that
            # gap: total chars and document count, so a mean chars/doc can be
            # derived per search.
            #
            # Measured HERE, after `rerank_docs` is fully built — every entry
            # has already been through `clamp_rerank_doc` above — because a
            # PRE-clamp count would reintroduce the very ambiguity this exists
            # to remove (a document longer than RERANK_MAX_DOC_CHARS is
            # truncated before the reranker ever sees the rest of it, so only
            # the truncated length was actually "sent"). Pure arithmetic over
            # a list already in hand: no extra query, no extra I/O, and
            # nothing here can raise — adding a metric must not add a new
            # failure mode to the search path.
            #
            # Computed BEFORE the try/except below so BOTH outcomes carry the
            # measurement: a reranked search records what it sent, and a
            # fallback records what it WOULD have sent — the two are
            # distinguished by `ranked`, never by one of them going missing.
            rerank_payload_chars = sum(len(d) for d in rerank_docs)
            rerank_payload_docs = len(rerank_docs)
            # Cumulative, same reset-on-restart contract as
            # _rerank_successes/_rerank_failures below. Deliberately NOT
            # paired with a third "searches measured" counter: that count
            # already exists as _rerank_successes + _rerank_failures (every
            # search that reaches this point ends up in exactly one of those
            # two buckets), and writing it again would be exactly the
            # derived-value duplication this repo's storage rule forbids.
            # Read it off the pair instead: successes+failures == 0 means
            # "not measured yet" (the payload totals below are vacuous, not a
            # real zero); once it's > 0 the totals are real measurements, and
            # a zero among them is a genuine all-empty-content search, not an
            # absence.
            self._rerank_payload_chars_total += rerank_payload_chars
            self._rerank_payload_docs_total += rerank_payload_docs
            # Same increment site as the pair above -- see this counter's
            # __init__ comment for why a max (not just the sum/count mean)
            # matters for a capacity signal.
            if rerank_payload_chars > self._rerank_payload_chars_max:
                self._rerank_payload_chars_max = rerank_payload_chars
            reranked = False
            _rr_t0 = time.monotonic()
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
                safe(lambda: self._rerank_ring.record(
                    (time.monotonic() - _rr_t0) * 1000.0,
                    payload_chars=rerank_payload_chars))
            except Exception as exc:
                safe(lambda: self._rerank_ring.record_error())
                # FAILURE != IDLE. The fallback serves VECTOR order, which is a
                # different answer from a ranked one — so it is logged, counted
                # and declared in the response rather than dressed up as a
                # confident uniform score.
                #
                # Probable-cause extension (operator ruling, W2′): a dropped or
                # reset connection mid-request is the httpx shape a reranker
                # container makes when the kernel OOM-kills it — a timeout is
                # not that shape (the process is merely slow/busy, not gone),
                # so only the transport-drop family gets the extra sentence.
                # Matched by TYPE, never a blanket except-Exception guess.
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
                        # Per-search payload instrument (fact:1441), repeated
                        # on every row exactly the way `ranked` is — see the
                        # fact-tier branch below for the full rationale.
                        "rerank_payload_chars": rerank_payload_chars,
                        "rerank_payload_docs": rerank_payload_docs,
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
                    # Chars/docs actually sent to the reranker for THIS
                    # search — same value on every row of the response, one
                    # search-level measurement, not a per-row one (fact:1441).
                    # Computed once, before the rerank try/except, so a
                    # fallback row carries the payload it WOULD have sent
                    # rather than a null: `ranked` is what tells the reader
                    # whether it was scored, this is what tells them what was
                    # measured — the two are never conflated, and a zero here
                    # is always a real (if degenerate) all-empty-content
                    # search, never "not measured" (see the counters near
                    # __init__ for how absence is told apart from zero at the
                    # cumulative-telemetry level).
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

        _t0 = time.monotonic()
        try:
            async with self._neo4j.session(default_access_mode="READ") as session:
                result  = await session.run(cypher, **params)
                records = await result.data()
            safe(lambda: self._neo4j_ring.record((time.monotonic() - _t0) * 1000.0))
        except ClientError as exc:
            # A REJECTION IS THE CALLER'S, NOT OURS — counted separately from
            # tx_failures for exactly that reason. Rolling a syntax error into
            # a failure count makes a user's typo read as a database outage.
            safe(lambda: setattr(self, "_cypher_rejected_total",
                                 self._cypher_rejected_total + 1))
            # ⛔ THE CALLER'S ERROR, NOT THE SERVER'S (v0.9.72). Every exception
            # used to become a 500 "query failed" — including a Cypher the
            # DATABASE refused: a syntax error, an unknown function, a type
            # error (live-reproduced with `coalesce(x.name, x.pg_id)` over
            # mixed-typed properties). A 500 tells the caller the gateway is
            # broken and to retry, when the one thing that will never help is
            # sending the same query again; it also buries a real outage in the
            # same bucket as a typo. `ClientError` is the driver's own word for
            # "you asked for something invalid", so it is the discriminator —
            # not a string match on the message.
            #
            # The driver's message is passed through, capped: it names the
            # offending clause and column, which is the entire value of the
            # reply, and nothing in it comes from the database's contents —
            # only from the query the caller just sent.
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

        # Serialization is a SEPARATE question from "did the query succeed" —
        # a Neo4j `DateTime`/`Date`/`Time` (or anything else `json.dumps`
        # chokes on) in a returned property is a bug in OUR coercion, not
        # evidence the database failed. Deliberately a NARROW try, split from
        # the query try/except above: only (TypeError, ValueError) — the
        # exceptions `json.dumps` itself raises — are caught here, and
        # `_neo4j_tx_failures_total` is NOT touched, so a serialization defect
        # never reads as a database outage on `/memory/telemetry`.
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
        # THE DROP, at the response boundary. ⛔ Never on `snap` itself — it is
        # the TTL cache, shared by every caller inside the window; strip_dropped
        # returns a fresh object and leaves it whole.
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
        # `generated_at` is stamped HERE and `timestamp` at SERVE time, so the
        # two differ by exactly the cache age. The age itself is deliberately
        # NOT a third key: a reader who wants it subtracts two timestamps it
        # already has, and writing it would be the derived-value duplication
        # decision:1032 forbids.
        snap: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}

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
                # ⚠ MOVED (removed in 0.9.75) — see the `outbox` section below.
                # This census OMITS a status with zero rows, so `outbox.failed`
                # went missing exactly when it read zero.
                "outbox": {r["status"]: r["n"] for r in outbox},
                "outbox_failed_oldest_age_seconds": failed_age,
                "community_summaries": {
                    "total": summ["total"],
                    "superseded": summ["superseded"],
                    "insight": summ["insight"],
                },
                # Pool gauges (0.9.74) — saturation was previously visible only
                # as the 503 it eventually produced; these climb before that.
                # asyncpg exposes both, so nothing here is derived twice.
                **self._pool_gauges(),
                # pgvector, moved off /health: it is a Postgres FACT, and it was
                # only ever on /health because that is where the startup probe's
                # result happened to be surfaced.
                "pgvector": {
                    "version": self.pgvector_version,
                    "iterative_scan": bool(self.hnsw_iterative_scan),
                },
            }
        except Exception as exc:
            snap["postgres"] = {"error": str(exc)}

        # Outbox (0.9.74) — its own section, with EVERY status always present
        # and the two latency numbers DERIVED from the columns the writer
        # already stamps (created_at, applied_at) rather than duplicated into an
        # in-memory ring that would reset on restart (decision:1032).
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
                # Neo4j's own service numbers (0.9.74). Before this the section
                # named after Neo4j contained only REM/NREM backlog counts —
                # nothing about the DATABASE. `cypher_rejected_total` is the
                # CALLER's fault and `tx_failures_total` is ours; see the
                # increment sites for why they are never summed.
                # ONE snapshot, three reads (F12). Three separate snapshot()
                # calls sorted the ring three times and — worse — could observe
                # three different windows, since a concurrent request can record
                # between them: p50 and p95 would then describe populations that
                # never coexisted, and `window` would name neither.
                **{f"query_{k}": v for k, v in _nj.items()
                   if k in ("p50_ms", "p95_ms", "window")},
                "cypher_rejected_total": self._cypher_rejected_total,
                "tx_failures_total": self._neo4j_tx_failures_total,
            }
        except Exception as exc:
            snap["neo4j"] = {"error": str(exc)}

        # REM (0.9.74) — the four backlog numbers that were living under
        # `neo4j.*` because that is where the query ran, plus the throughput the
        # durable rem_timing clock can answer and nothing was asking.
        try:
            snap["rem"] = await self._rem_telemetry()
        except Exception as exc:
            snap["rem"] = {"error": str(exc)}

        # Registry (0.9.74) — row counts and the ingress refusal counters. Every
        # one of these gates shipped in 0.9.69 UNINSTRUMENTED: a refusal was
        # visible to the one caller who received it and to nobody else.
        try:
            snap["registry"] = self._registry_telemetry()
        except Exception as exc:
            snap["registry"] = {"error": str(exc)}

        # NREM dream-cycle backlog — pending consolidation CYCLES, not raw facts.
        # One cycle per (entity, domain) cluster meeting the density threshold.
        # Needs both backends: Neo4j supplies the rem_processed/unconsolidated
        # clusters; Postgres supplies the authoritative domain per pg_id (the
        # Fact node has no domain). This is the join a read-only client cannot
        # do itself — hence it lives here.
        #
        # ⭐ SERVED FROM THE 60 s REFRESHER, NOT COMPUTED HERE (v0.9.74, B4).
        # MEASURED on this corpus 2026-08-28 through the gateway's own read-only
        # graph route: the insight half is 149 SEQUENTIAL Neo4j round-trips —
        # 8 gating (project, domain) groups at density>=3, each walked over 9-26
        # BFS layers — and the walk is unbounded by construction (insight_gate,
        # I3: no hop cap, no edge cap, termination by fixpoint). A per-request
        # cap would not have been honest either: the number of layers is a
        # property of the corpus, not a budget. `as_of` states when it was
        # computed so a reader is never guessing how old it is.
        dep = self._dependency_health
        nrem = dep.get("nrem")
        if isinstance(nrem, dict):
            snap["nrem"] = {**nrem, "as_of": dep.get("as_of")}
        else:
            snap["nrem"] = {"error": "not yet computed", "as_of": dep.get("as_of")}

        # Metadata breakdown — drill-down distributions a dashboard renders
        # (record types, agents, sources, domains, summary kinds). Cheap GROUP
        # BYs over technical_docs + community_summaries; surfaced here so the
        # monitor needs no direct Postgres connection for its breakdown panels.
        try:
            snap["breakdown"] = await self._metadata_breakdown()
        except Exception as exc:
            snap["breakdown"] = {"error": str(exc)}

        # Entity-graph shape (ADR-017) — the live, cheap counterpart to the
        # offline ER calibration harness this framework once shipped (retired).
        # Surfaces fragmentation and alias coverage. The O(n²) cosine over-merge
        # analysis stays OUT of the hot path; only aggregates here.
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

        # Rerank outcome counters (operator ruling, W2′) — in-process, no I/O,
        # same reset-on-restart contract as the LLM-fault/credential counters
        # this pairing mirrors. The reranker degrades to vector order on ANY
        # failure (a dead process, a timeout, or the kernel OOM-killing it on
        # a memory-constrained host) and still answers 200 — so without this
        # the whole class of failure is invisible from outside the log.
        # Flat additive keys + a paired last-event timestamp, never a nested
        # restructure (see inference_busy — the existing flat top-level
        # exemplar; ruled flat at W2' merge, fact:1314 shape).
        snap["rerank_successes_total"] = self._rerank_successes
        snap["rerank_fallbacks_total"] = self._rerank_failures
        snap["rerank_fallbacks_last_ts"] = self._rerank_fallback_last_ts

        # Axis registry read failures (PR-C) — the same shape, for the same
        # reason. When the projects/domains registry cannot be read, the gateway
        # still answers 200: by-key resolution silently becomes a no-op and a
        # filtered search matches only the literal string it was given. From
        # outside, that is identical to a name nobody registered. NEW KEYS, never
        # a rename of an existing one; `..._total` is cumulative since process
        # start and `..._last_ts` is stamped at the same increment so the pair
        # cannot disagree.
        snap["axis_registry_read_failures_total"] = self._axis_registry_read_failures
        snap["axis_registry_read_failures_last_ts"] = \
            self._axis_registry_read_failure_last_ts

        # Payload-size instrument (fact:1441) — same flat-additive style,
        # ADDED alongside the pair above rather than restructuring them.
        # Cumulative chars/docs actually handed to the reranker across every
        # search this process has served (both outcomes count — see
        # handle_search). Deliberately no third "searches measured" counter:
        # rerank_successes_total + rerank_fallbacks_total already IS that
        # count, and a reader who wants to know whether the totals below are
        # a real zero or simply "no searches yet" reads it off that existing
        # pair rather than a duplicate written here. Divide chars_total by
        # docs_total for the mean-chars-per-doc that separates document-length
        # cost from the fixed per-request overhead fact:1441 could not.
        snap["rerank_payload_chars_total"] = self._rerank_payload_chars_total
        snap["rerank_payload_docs_total"] = self._rerank_payload_docs_total
        # NEW (operator ruling, 2026-08-23): observed maximum, not just the
        # sum -- a capacity signal needs the worst real payload seen, which
        # a sum+count mean cannot give. Monotonic non-decreasing for this
        # process's lifetime (see the counter's own __init__ comment) —
        # this is the safe direction for a capacity signal, never a defect.
        snap["rerank_payload_chars_max"] = self._rerank_payload_chars_max

        # Credential-use audit trail signal (PR A3) — in-process counters, no
        # I/O, so no try/except: same reset-on-restart contract as the
        # existing _llm_routed counters. Detail lives in the separate
        # credential-events log; this is the operator-attention SIGNAL only.
        # ⚠ MOVED to `llm.faults` (removed in 0.9.75) — dual-emitted here so the
        # monitor migrates without a flag day.
        snap["llm_faults"] = _llm_faults_snapshot()
        snap["credentials"] = {
            **_credentials_snapshot(),
            # The limit next to the number it bounds — the contract's own rule.
            "token_verify_warn_per_min": TOKEN_VERIFY_WARN_PER_MIN,
        }

        # ── In-memory sections (0.9.74) — no I/O, so no try/except needed on
        # the calls themselves; the assembly is guarded anyway because a
        # telemetry section must never be the reason the endpoint 500s.
        snap["encoders"] = safe(self._encoders_telemetry, default={"error": "unavailable"})
        snap["gateway"] = safe(self._gateway_telemetry, default={"error": "unavailable"})
        snap["clients"] = {"versions_seen": dict(_client_versions_seen)}

        # ── The blocks that live in hive_mind_proxy's module state ───────────
        # The whole llm_* family, the capability/capacity snapshots and the
        # resolved config were only ever on /health, which is the 30-second
        # endpoint every client hits on every call — ~130 of its 193 keys were
        # ANALYTICS a monitor drawer reads, not liveness. They belong here.
        # Delivered through a provider callback (see telemetry_extras_provider)
        # because hive_mind_proxy imports this module, so importing back would
        # be a cycle.
        if self.telemetry_extras_provider is not None:
            extras = safe(self.telemetry_extras_provider, default=None)
            if isinstance(extras, dict):
                snap.update(extras)

        # gpu_probe + the two identity probes, moved off /health. Read from the
        # coordinator's OWN cached snapshot — the same value /health's
        # consolidation block carries, not a second probe.
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

        # ⛔ THE `alias` BLOCK IS GONE (v0.9.72). It counted
        # `alias_adjudications`, the ADR-017 per-pair verdict ledger migration
        # 014 built: the alias-writer sweep wrote a row per adjudicated pair,
        # and this rollup was its ONLY reader. Nothing has written to it since
        # v0.8.60, so the block reported a frozen census of a retired layer as
        # though it were current state — the worst shape a metric can take. The
        # table is dropped by migration 040, so the key leaves with it rather
        # than becoming an `{"error": ...}` on every call.
        #
        # ⚠ `aliases` (the AXIS alias table `project_aliases.alias_id` and
        # `domain_aliases.alias_id` point at) is a different table and STAYS —
        # it is live identity, not the retired adjudication layer.
        #
        # MONITOR CONTRACT: `/memory/telemetry` no longer carries `alias`.

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
                last_error_at = r["last_error_at"]
                # C1 fix (merger ruling): compare against last_completed_at,
                # NEVER last_success. last_success is FILTERed on
                # `folds_succeeded > 0`, which a run that itself CRASHED after
                # folding at least one cluster also satisfies (consolidation_
                # loop writes a crashed row with rec.succeeded already > 0) —
                # so last_success can be exactly this crash's own finished_at,
                # and comparing against it would call a seconds-old crash
                # "superseded" by itself. last_completed_at is FILTERed on
                # `outcome = 'completed'` — a run that actually finished
                # clean — the only apples-to-apples comparison for
                # "did a real success land after this crash".
                last_completed_at = r["last_completed_at"]
                # superseded: a real success has landed AFTER this crash — the
                # crash is history, not a current condition. NOT the same test
                # as `stalled` above (that gates on age vs threshold + backlog;
                # this gates on ORDER relative to the crash alone), and not the
                # same population as consec_fail (crashes SINCE last success,
                # already 0 in exactly this case) — this is about how the ONE
                # most-recent crash, however old, should ever be *displayed*.
                superseded = bool(
                    last_completed_at is not None and last_error_at is not None
                    and last_completed_at > last_error_at)
                # O9: age computed in SQL (EXTRACT(EPOCH ...) against the same
                # DB clock last_success_age already uses), not a second,
                # Python-side `now() - last_error_at` subtraction.
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
                try:
                    # probe_status() is pure module state (fact:1645) -- this
                    # can't actually raise today, but it shares the module with
                    # inference_busy_state() above, so it gets the same
                    # tolerance as its three siblings rather than a bare call.
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
                    # Which type the headline age belongs to, and who is
                    # actually stalled — without these two the compact /health
                    # snapshot cannot be read correctly when the types disagree.
                    # Read tolerantly: a missing roll-up key must degrade this
                    # one field, never abort the refresh and blank the whole
                    # snapshot (which would report the system as unknown).
                    "last_success_cycle_type": full.get("last_success_cycle_type"),
                    "stalled_types": full.get("stalled_types", []),
                    "inference_busy": inference_busy,
                    "gpu_probe": gpu_probe,
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

            # ── The dependency snapshot (v0.9.74) ────────────────────────────
            # Its own try/except, NOT folded into the block above: a failing
            # Postgres probe must not blank the consolidation snapshot, and a
            # failing consolidation rollup must not blank the liveness enums.
            # Each sub-probe is guarded on its own for the same reason — this is
            # the block that answers "is anything up", and it is worth nothing
            # if one dead component can take the whole answer down with it.
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
                    # ⭐ MOVED OUT OF THE REQUEST PATH (B4, measured 2026-08-28
                    # on this corpus): the insight walk is 149 SEQUENTIAL Neo4j
                    # round-trips — 8 gating groups at density>=3, each walked
                    # over 9-26 BFS layers — and the walk is unbounded by
                    # construction (I3: no hop cap, no edge cap). It grows with
                    # the corpus, so no per-request cap would be honest either.
                    # Computed here, served from cache with `as_of`.
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
            # ⛔ NULL WHEN THERE IS NO BASIS, 0.0 WHEN THERE IS. `n` is how many
            # rows were applied in the 24 h window; if none were, this process
            # has measured nothing and a rate of 0.0 would assert an
            # observation nobody made. With a non-empty window a 0.0 IS a real
            # measurement — nothing drained this minute — and nulling THAT
            # would be the same absence-is-not-zero rule pointed backwards.
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
                # The regex guard makes the cast SAFE: `ts` is written by one
                # code path as a float, but this column is JSONB on a table with
                # rows older than that writer, and one unparseable value would
                # abort the whole query rather than skip a row.
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
            # ⛔ NEVER NULL ONCE A CENSUS HAS SUCCEEDED. On a failed poll the
            # LAST GOOD value is served and `error`/`as_of` say what happened
            # and how old it is — a null would make "the query failed" look
            # exactly like "this deployment has no projects", which is the same
            # absence-is-not-zero confusion the outbox census had. Before the
            # first successful poll they are 0 with `as_of: null`, which reads
            # as "nothing counted yet" rather than "nothing exists".
            "projects": (census or {}).get("projects", 0),
            "domains": (census or {}).get("domains", 0),
            "aliases": (census or {}).get("aliases", 0),
            "as_of": self._registry_census_as_of,
            # The SEARCH-path counter: a filter that could not be resolved.
            "read_failures_total": self._axis_registry_read_failures,
            # The CENSUS counter, deliberately separate. A failed census means
            # this telemetry is stale; a failed axis read means a SEARCH
            # silently answered from the literal string. Same subsystem, two
            # different consequences, and summing them would let an operator
            # read a stale gauge as a broken retrieval path.
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
        return {
            "embed": self._embed_ring.snapshot(),
            "rerank": self._rerank_ring.snapshot(),
            "limit_ms": ENCODER_LATENCY_WARN_MS,
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
            # D9 (OBS round): incremented from hive_mind_proxy.py via
            # record_llm_client_disconnect() — see that function's docstring
            # for why the counter is stored here rather than assembled on
            # the proxy side.
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
            # ⚠ THE AXIS BREAKDOWNS GET THEIR OWN, LARGER LIMIT. `agents` and
            # `sources` are unbounded populations where a top-12 IS the answer.
            # Projects and domains are REGISTRY-BACKED: truncating them below
            # the number of registered entries silently hides whole sections,
            # and a reader has no way to tell a section with no records from a
            # section that fell off the end of a LIMIT. Live 2026-08-28: 38
            # projects and 15 distinct domain names in use against a top-12.
            projects = await conn.fetch(
                f"SELECT COALESCE({PROJECT_SQL}, '(none)') AS key,"
                " count(*)::int AS count FROM technical_docs"
                " GROUP BY 1 ORDER BY count DESC LIMIT $1", BREAKDOWN_AXIS_TOP_N
            )
            # ⚠ THE REAL DOMAIN DISTRIBUTION (0.9.74). `domains` is a JSON ARRAY
            # on the record — one record belongs to several sections — so the
            # counts here SUM TO MORE than the record count, unlike every other
            # breakdown in this payload. That is the axis, not a defect.
            domains = await conn.fetch(
                "SELECT d AS key, count(*)::int AS count"
                "  FROM technical_docs,"
                "       LATERAL jsonb_array_elements_text("
                "         CASE WHEN jsonb_typeof(metadata->'domains') = 'array'"
                "              THEN metadata->'domains' ELSE '[]'::jsonb END) AS d"
                " GROUP BY 1 ORDER BY count DESC LIMIT $1", BREAKDOWN_AXIS_TOP_N
            )
            # ⛔ THE DENOMINATOR SHIPS WITH THE DISTRIBUTION. Domain counts are
            # over an ARRAY column, so they are not comparable with any other
            # breakdown in this payload and cannot be read against a record
            # total the reader has to guess at. Live 2026-08-28: 629 of 1691
            # records carry a non-empty `domains` — 62.8% carry NONE — so the
            # counts describe a 37% subset. Without these two numbers a reader
            # sums the distribution, gets less than the corpus, and concludes
            # records are missing rather than unlabelled.
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
            # ⚠ `domains` CHANGED MEANING IN 0.9.74 (enumerated in
            # telemetry_contract.MEANING_CHANGES, per fact:1626). It used to
            # carry the PROJECT distribution — it was built from PROJECT_SQL,
            # under a name that said domain. The project distribution now has
            # its own correct name; `domains` finally means domains.
            "projects": kv(projects),
            "domains": kv(domains),
            "records_with_domains": coverage["records_with_domains"],
            "records_total": coverage["records_total"],
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
    app.router.add_get( "/admin/outbox",           coordinator.handle_admin_outbox, allow_head=False)
