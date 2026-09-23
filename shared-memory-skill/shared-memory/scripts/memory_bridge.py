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
        --project "..." --rationale "..." --grounded-in "601:based_on,602" \
        [--source "..."] [--assisted-by "a,b"] [--confidence "high"] \
        [--alternatives "one option" --alternatives "another, with a comma"]

Environment overrides (not CLI flags — set in the shell or the client .env):
    SEARCH_TIMEOUT_S       explicit override of the derived search wait; pins a
                           constant client-side search timeout instead of sizing
                           it from the gateway's own published backend capability
                           (see `search`).
    SHARED_MEMORY_PROJECT  overrides project derivation when saving from outside
                           a project root (see `save`, `save_decision`).
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

VERSION = "0.9.114"
# Must match GET /health api_version; v4 refuses unregistered project on fact save.
API_VERSION = 4

# Must mirror ontology.RETRO_RATINGS on the gateway; this client never imports server modules.
# Outcome states, not valence: 'reversed' drives the supersession cascade; nuance goes in notes.
RETRO_RATINGS = ("validated", "mixed", "refined", "pending", "reversed")
CLIENT_VERSION_HEADER = "X-SM-Api-Version"
# Framework build, distinct from the wire API_VERSION. Two clients can share api_version 4 and
# still be releases apart.
# The gateway counts this header as clients.versions_seen so that skew is visible.
CLIENT_BUILD_HEADER = "X-Shared-Memory-Client"

# Only this skill's scripts/.env then ../.env; never walk toward $HOME.
_ENV_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
]
# SECURE_ENV_FILE matches secure_env._select_env_file: a path is the exact file, empty loads
# none, unset keeps the walk.
# Empty is the test pin. In admin mode the second candidate is the live gateway .env, and
# importing it would setdefault secrets into os.environ.
_secure_env_file = os.environ.get("SECURE_ENV_FILE")
if _secure_env_file is not None:
    _ENV_CANDIDATES = [_secure_env_file.strip()] if _secure_env_file.strip() else []

# AGENT_TOKEN stays in this private variable and is never copied into os.environ, where /proc
# and child processes would see it.
# An operator export still wins, checked before any file. Tests clear _AGENT_TOKEN_FROM_FILE to
# ignore a real on-disk .env.
_AGENT_TOKEN_FROM_FILE = ""

# Duplicated from secure_env.is_secret_key because this client ships alone. Admin mode's second
# candidate is the gateway .env, so a missed name leaks into os.environ; AGENT_TOKEN stays on
# its own path.
# test_client_secret_mirror_parity.py pins these lists to the server copies, including PG_CONN
# and the _SECRET/_KEY/_CREDENTIAL suffixes.
_CLIENT_KNOWN_SECRET_NAMES = {
    "PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY", "AGENT_TOKENS",
    "BACKUP_ADMIN_TOKEN", "PG_CONN",
}
_CLIENT_SECRET_SUFFIXES = (
    "_PASSWORD", "_TOKEN", "_API_KEY", "_SECRET", "_KEY",
    "_CREDENTIAL", "_CREDENTIALS",
)


def _client_key_norm(name: str) -> str:
    """Fix round F11 (SEC1 MED-7 + LOW-8): shared client-side key
    normaliser — mirrors secure_env._normalize_key() exactly (duplicated,
    not imported: this client ships alone and may not depend on a
    server-only module). BOM (U+FEFF) + whitespace stripped from both
    ends, in EITHER order and any interleaving, then upper-cased. A single
    fixed-order strip (e.g. .strip().lstrip(BOM)) only handles ONE of the
    two orderings a raw line can carry — probed by SEC1: "﻿ AGENT_TOKENS"
    (BOM then space) and " ﻿AGENT_TOKENS" (space then BOM) each defeat
    exactly one fixed order."""
    s = name
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    while s and (s[-1].isspace() or s[-1] == "﻿"):
        s = s[:-1]
    return s.upper()


_EXPORT_PREFIX_RE = re.compile(r"^export\s+", re.IGNORECASE)


def _strip_export_prefix(key: str) -> str:
    """Fix round F5 (SEC1 HIGH-3 + MED-5): strip an optional leading shell
    `export ` keyword (case-insensitive) from a raw .env line's key text,
    at parse time. Mirrors secure_env._strip_export_prefix() exactly."""
    s = key
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    s = _EXPORT_PREFIX_RE.sub("", s, count=1)
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    return s


def _strip_balanced_quotes(value: str) -> str:
    """S16g (HYG round, R-G'): a `.env` VALUE wrapped in ONE balanced pair of
    surrounding quotes — `"v"` or `'v'` — has that pair stripped; everything
    else (an unbalanced leading quote with no matching trailing one, a bare
    quote embedded in the value, mismatched quote characters, or no quotes
    at all) is kept VERBATIM. Same rule, independently duplicated in
    secure_env.py and mcp/vector-skill.py — none of the three may import from
    another (Group 1: the client/server surface split)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _is_client_secret_key(name: str) -> bool:
    """True if `name` must never be exported into this client's own
    os.environ (mirrors secure_env.is_secret_key(), narrowed to what this
    client can ever encounter). AGENT_TOKEN is excluded -- it has its own
    private-variable path and is never routed through this predicate.

    Fix round F11 (SEC1 MED-7): normalises internally via
    _client_key_norm(), so ANY caller — pre-normalised or raw — classifies
    correctly. Before this fix, `_is_client_secret_key("agent_tokens")` was
    False (exact-match-only against the upper-cased name list, no internal
    normalisation) — defused today only because every call site happens to
    pass an already-`.upper()`d key; a future caller passing a raw key
    would silently re-open a live `agent_tokens=` export."""
    key_norm = _client_key_norm(name)
    if key_norm == "AGENT_TOKEN":
        return False
    if key_norm in _CLIENT_KNOWN_SECRET_NAMES:
        return True
    return key_norm.endswith(_CLIENT_SECRET_SUFFIXES)


def _read_env_file(path: str) -> None:
    """Parse one skill-scoped `.env` into this client's config.

    The ONE parser this client has — the same rules the gateway applies to its
    own env (secure_env.py), duplicated rather than imported because this
    client ships alone (Group 1: the client/server surface split). A VALUE is
    read verbatim to the end of its line: an inline `# comment` after a value
    is part of the value, and a line with an unbalanced quote is kept as
    written and never swallows the next line. Multi-line values, `${VAR}`
    interpolation and `\\n` escapes are not a form any shipped or minted `.env`
    uses and are not supported."""
    global _AGENT_TOKEN_FROM_FILE
    try:
        # utf-8-sig, matching mcp/vector-skill.py: a BOM would stick to the first key, and later
        # normalisation must not be the only catch.
        with open(path, encoding="utf-8-sig") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                # Strip a leading export before classification, or "export AGENT_TOKENS" matches
                # neither secret check and lands in os.environ.
                _k = _strip_export_prefix(_k).strip()
                _v = _strip_balanced_quotes(_v.strip())
                if not _k:
                    continue
                # One normalisation for both the AGENT_TOKEN check and the secret check,
                # matching mcp/vector-skill.py.
                # Export _k, not _k_norm: this changes what counts as secret, never the stored
                # name's casing.
                _k_norm = _client_key_norm(_k)
                if _k_norm == "AGENT_TOKEN":
                    if not _AGENT_TOKEN_FROM_FILE:
                        _AGENT_TOKEN_FROM_FILE = _v
                    continue
                if _is_client_secret_key(_k_norm):
                    continue
                if _k not in os.environ:   # first definition wins
                    os.environ[_k] = _v
    except OSError:
        pass


for _env in _ENV_CANDIDATES:
    _read_env_file(_env)

COORDINATOR_BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
AGENT_ID         = os.environ.get("AGENT_ID", "memory_bridge")

# Search wait is derived from /health backend_capability (and capacity when present), not a
# fixed 30s (fact:1112).
HEALTH_PROBE_TIMEOUT_S    = float(os.environ.get("HEALTH_PROBE_TIMEOUT_S", "3"))
SEARCH_TIMEOUT_S          = float(os.environ.get("SEARCH_TIMEOUT_S", "0") or 0)
SEARCH_TIMEOUT_FLOOR_S    = float(os.environ.get("SEARCH_TIMEOUT_FLOOR_S", "30"))
SEARCH_TIMEOUT_MAX_S      = float(os.environ.get("SEARCH_TIMEOUT_MAX_S", "300"))
SEARCH_TIMEOUT_FALLBACK_S = float(os.environ.get("SEARCH_TIMEOUT_FALLBACK_S", "120"))
SEARCH_SAFETY_FACTOR      = float(os.environ.get("SEARCH_SAFETY_FACTOR", "1.5"))
SEARCH_OVERHEAD_S         = float(os.environ.get("SEARCH_OVERHEAD_S", "15"))

# Names that mark a project root. Any match stops the walk; `.git` is the least ambiguous, and
# the instruction files cover directories that are not repositories.
PROJECT_ROOT_MARKERS = tuple(
    m for m in os.environ.get(
        "PROJECT_ROOT_MARKERS", ".git,CLAUDE.md,AGENTS.md,GEMINI.md"
    ).split(",") if m.strip()
)


def derive_project(start: str | None = None) -> str:
    """Derive the canonical project tag from the working directory.

    The canonical project is the PROJECT FOLDER NAME, so that every session on a
    project produces the same tag no matter which agent wrote the record. The
    gateway cannot do this — it is a server and never sees a client's working
    directory — but skill runners execute from the user's project directory, so
    the client can, identically for every agent.

    Walks up from `start` to the first directory holding a project-root marker and
    returns its basename. Walking (rather than taking the bare basename of the cwd)
    is the whole point: a save issued from `<project>/tests` must tag `<project>`,
    not `tests`. Stops at the filesystem root and never ascends past $HOME, so a
    save issued from a home directory or an unmarked scratch dir derives nothing
    and returns "" — an empty tag is strictly better than a confidently wrong one.

    `SHARED_MEMORY_PROJECT` overrides the walk entirely, for callers whose working
    directory is not a meaningful project boundary (daemons, CI, cron).
    """
    override = os.environ.get("SHARED_MEMORY_PROJECT", "").strip()
    if override:
        return override

    try:
        cur = os.path.abspath(start or os.getcwd())
    except OSError:          # cwd deleted out from under us
        return ""
    home = os.path.abspath(os.path.expanduser("~"))

    while True:
        # $HOME is a boundary, not a project: it often holds a CLAUDE.md and would tag every
        # stray save with the account name.
        if cur == home:
            return ""
        if any(os.path.exists(os.path.join(cur, m)) for m in PROJECT_ROOT_MARKERS):
            return os.path.basename(cur)
        parent = os.path.dirname(cur)
        if parent == cur:    # filesystem root
            return ""
        cur = parent


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
        return httpx.AsyncClient(timeout=timeout, transport=httpx.AsyncHTTPTransport(uds=uds, trust_env=False), trust_env=False)
    return httpx.AsyncClient(timeout=timeout, trust_env=False)


def _sync_client(timeout: float) -> "httpx.Client":
    uds = _uds_path()
    if uds:
        return httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(uds=uds, trust_env=False), trust_env=False)
    return httpx.Client(timeout=timeout, trust_env=False)


def search_ceiling(capability: dict | None, capacity: dict | None = None) -> float:
    """Seconds to wait for search: max of /health projections, with SEARCH_TIMEOUT_FALLBACK_S if any encoder is unknown or failing (fact:1560).

    SEARCH_TIMEOUT_S overrides. Absent/empty/non-dict blocks count as unknown. Capacity derived fields only raise the ceiling.
    """
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
        # Vector search, the graph walk, and response assembly sit outside both probes, so that
        # overhead is added rather than scaled with encoder throughput.
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
# Two searches can both see an unfilled cache and both call /health; the lock makes one fill
# win.
# Constructed at import: asyncio.Lock() does not need a running loop on the Python versions this
# project targets.
_HEALTH_FETCH_LOCK = asyncio.Lock()


async def _fetch_health_blocks() -> None:
    """GET /health once per process and cache both ``backend_capability`` and
    ``capacity`` from it — ONE request feeds both caches, never two.

    Never raises. An unreachable or slow gateway leaves both caches at their
    "tried and got nothing" state and callers fall back to a constant ceiling —
    sizing the search must never be the thing that fails the search. The
    gateway caches its own probe, so this costs a few ms.

    Sends this client's own auth headers (S-10, PR A5): ``backend_capability``
    moved behind auth along with the rest of /health's operational detail, so
    an unauthenticated call here would always land on the anonymous-slim shape
    (no ``backend_capability``/``capacity`` keys at all) and silently fall back
    to the constant ceiling on every authenticated install — the exact "unknown
    cost" case ``search_ceiling`` already degrades safely for, just permanently
    rather than only when the gateway is genuinely old/unreachable/unprobed.
    """
    global _CAPABILITY_CACHE, _CAPACITY_CACHE
    if _CAPABILITY_CACHE is not None:
        return   # already attempted this process — do not retry
    async with _HEALTH_FETCH_LOCK:
        if _CAPABILITY_CACHE is not None:
            return   # a concurrent waiter already filled it while we queued
        try:
            async with _async_client(HEALTH_PROBE_TIMEOUT_S) as client:
                health = _reply_json(await client.get(f"{COORDINATOR_BASE}/health",
                                                      headers=_request_headers()))
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


def _request_headers() -> dict:
    """Headers attached to every coordinator request.

    Always advertises this client's API_VERSION so the gateway can log skew
    (see coordinator._check_client_version). Adds the Bearer token when
    AGENT_TOKEN is set — checked fresh on every call so an operator export
    or a test's monkeypatch.setenv always wins, falling back to the value
    this module parsed out of its own .env at import time (never itself
    exported to os.environ — see _AGENT_TOKEN_FROM_FILE above).
    """
    headers = {CLIENT_VERSION_HEADER: str(API_VERSION),
               CLIENT_BUILD_HEADER: VERSION}
    token = os.environ.get("AGENT_TOKEN", "").strip() or _AGENT_TOKEN_FROM_FILE
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _token_presented() -> bool:
    """Whether this client actually sent a credential on a request just now.

    Derived from _request_headers() rather than re-reading AGENT_TOKEN, so the
    two can never disagree about what was on the wire — the equality is asserted
    against the real header, not against a second copy of the lookup (fact:1309).
    """
    return "Authorization" in _request_headers()


def _auth_error() -> dict:
    """The ONE 401 reply, phrased for the failure that actually happened.

    A 401 with NO Authorization header sent is a MISSING credential, not a
    rejected one: this client never presented anything for the gateway to
    reject. Saying "rejected" in that case sends the operator off to compare a
    token value against the gateway's AGENT_TOKENS registry, when the real
    answer is that no token was configured at all — a different fix, in a
    different file. Both branches still name AGENT_TOKEN and this agent's own
    .env, because that is the remedy either way.
    """
    if _token_presented():
        return {"status": "error",
                "message": ("Coordinator rejected this agent's token. Check that AGENT_TOKEN "
                            "in this agent's .env matches an entry in the gateway's "
                            "AGENT_TOKENS.")}
    return {"status": "error",
            "message": ("No AGENT_TOKEN was sent and this gateway requires authentication. "
                        "Set AGENT_TOKEN in this agent's .env.")}


def _auth_log_hint() -> dict:
    """Log payload for the 401 sites that record one. Which sites log is
    deliberately UNCHANGED here; only the wording follows the branch above."""
    if _token_presented():
        return {"hint": "Check AGENT_TOKEN in .env matches an entry in gateway AGENT_TOKENS"}
    return {"hint": "No AGENT_TOKEN was sent; this gateway requires auth — set it in this agent's .env"}

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
        # Logs may carry agent activity, so create them 0600 and tighten a world-readable file.
        # merge_logs rotates these; gateway logs use log_hygiene.
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

class GatewayReplyError(Exception):
    """The gateway ANSWERED, and its answer was not a 2xx JSON payload.

    Carries the client-facing error dict so every call site returns one shape.
    It exists so that a reply which is not a success payload can never be
    mistaken for a transport failure: it is raised INSIDE the request's
    ``try``, and every site catches it BEFORE the generic handler that reports
    an unreachable gateway.

    ``logged_event`` names the audit event the RAISE SITE already wrote, or is
    None when it wrote nothing. Centralising the decode moved some logging
    inside ``_reply_json``, and a catch block that logs unconditionally would
    then record ONE refused call TWICE — a 401 as both ``auth_failed`` and
    ``save_failed``, where before it was a single ``auth_failed`` line. The
    catch block therefore asks the exception what has already been recorded.
    Deliberately an ATTRIBUTE and not a phrase read back out of the message:
    keying the audit trail on message text would tie it to wording that exists
    to be improved.
    """

    def __init__(self, payload: dict, *, logged_event: str | None = None):
        super().__init__(payload.get("message", ""))
        self.payload = payload
        self.logged_event = logged_event


def _body_snippet(r, limit: int = 200) -> str:
    """A short, whitespace-collapsed piece of the response body, or "".

    Never raises: this runs on the error path, where a second failure would
    replace a diagnosis with a traceback.
    """
    try:
        # Gateway-controlled text, including a non-JSON error page, is heading for a terminal or
        # log, so strip controls before collapsing and capping it.
        return " ".join(_clean_gateway_text(r.text or "").split())[:limit]
    except Exception:
        return ""


# Gateway text reaches the audit log and the terminal, and COORDINATOR_BASE is env-overridable,
# so it is not trusted.
# 600 keeps the longest deployed refusal (378 characters) whole and cuts only a body no deployed
# path emits.
_GATEWAY_MESSAGE_MAX = 600


def _clean_gateway_text(msg: str) -> str:
    """Strip ASCII control characters (newline and tab kept) and cap the length.

    A message printed to a terminal is not inert: an ANSI escape can clear the
    screen or rewrite the line the operator is reading, and a BEL is not a
    diagnosis. Stripping runs BEFORE the cap so the cap counts characters the
    reader will actually see rather than characters that were about to be
    removed.
    """
    cleaned = "".join(
        ch for ch in msg
        if ch in ("\n", "\t") or (ord(ch) >= 32 and ord(ch) != 127)
    )
    return cleaned.strip()[:_GATEWAY_MESSAGE_MAX]


def _gateway_message(r) -> str | None:
    """The gateway's own ``message`` when the body is JSON and carries one.

    Guarded end to end: the whole point of this module's error contract is
    that a decode failure is a RESULT here, never an exception that escapes
    into the transport handler.

    The message is capped and control-stripped on the way out — see
    ``_clean_gateway_text``.
    """
    try:
        body = r.json()
    except Exception:
        return None
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error")
        if isinstance(msg, str) and msg.strip():
            return _clean_gateway_text(msg) or None
    return None


def _reply_json(r, *, log_auth: bool = False,
                accept_status: tuple = ()) -> dict:
    """Decode JSON only after the status class is known; pass accept_status to keep a non-2xx body (fact:1503)."""
    # The catch block is told the event name this branch just wrote, so a second literal cannot
    # drift away from the audit line.
    if r.status_code == 401:
        logged = "auth_failed" if log_auth else None
        if logged:
            _append_log("memory_bridge", 2, logged, _auth_log_hint())
        raise GatewayReplyError(_auth_error(), logged_event=logged)

    # The gateway's own words come first. Downstream readers truncate (postflight slices a
    # search error to 200 characters), and a long preamble hides the refusal.
    if r.status_code == 403:
        detail = _gateway_message(r) or _body_snippet(r)
        head = f"Gateway refused this request (HTTP 403): {detail}" if detail else \
               "Gateway refused this request (HTTP 403)."
        message = (f"{head} — the gateway ANSWERED and the credential was ACCEPTED, so "
                   f"this is an authorization refusal, not an authentication failure "
                   f"and not a transport fault.")
        raise GatewayReplyError({"status": "error", "message": message})

    if r.status_code >= 400 and r.status_code not in accept_status:
        detail = _gateway_message(r) or _body_snippet(r) or "(empty body)"
        raise GatewayReplyError({"status": "error", "message": (
            f"Gateway answered HTTP {r.status_code}: {detail} — it is UP at "
            f"{COORDINATOR_BASE} and refused or failed this request."
        )})

    try:
        return r.json()
    except Exception as exc:
        raise GatewayReplyError({"status": "error", "message": (
            f"Gateway answered HTTP {r.status_code} at {COORDINATOR_BASE} with a body this "
            f"client could not parse as JSON ({exc}). The gateway is LIVE and ANSWERED "
            f"— this is a malformed reply, not a transport fault. Body began: "
            f"{_body_snippet(r, 120) or '(empty)'}"
        )}) from exc


def _coordinator_unavailable(exc: Exception, ceiling: float | None = None) -> dict:
    """Timeout vs refused vs unreachable are different messages; a GatewayReplyError is never 'unreachable' (fact:1112, fact:1503)."""
    if isinstance(exc, GatewayReplyError):
        return exc.payload
    if isinstance(exc, httpx.TimeoutException):
        waited = f"{ceiling:.0f}s" if ceiling else "the client timeout"
        return {
            "status": "error",
            "message": (
                f"Gateway did not answer within {waited} — it is most likely UP and "
                f"SLOW, not down. A search costs what the reranker costs. Read "
                f"`backend_capability` on {COORDINATOR_BASE}/health, and raise "
                f"SEARCH_TIMEOUT_S if its projection exceeds that ceiling."
            ),
        }
    return {
        "status": "error",
        "message": (
            f"Memory coordinator unreachable at {COORDINATOR_BASE} — "
            f"is hive_mind_proxy.py running? ({exc})"
        ),
    }


ROLE_REPORTING_MIN_VERSION = "0.9.54"  # authenticated /health reports role from 0.9.54; 0.9.52 does not.


def _gateway_predates(version: str | None, minimum: str = ROLE_REPORTING_MIN_VERSION) -> bool | None:
    """Whether ``version`` names a gateway release strictly before ``minimum``.

    ``None`` when ``version`` cannot be parsed as dotted integers — an old,
    pre-version-contract gateway or a malformed string. The caller treats that
    the same as "predates": a gateway too old to even report a parseable
    version is certainly too old to report `role` (T-04, PR #310 review).
    """
    try:
        parsed = tuple(int(p) for p in str(version).split("."))
        floor = tuple(int(p) for p in minimum.split("."))
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed < floor


def _role_diagnosis(h: dict) -> str:
    """T-04 (PR #310 review): THREE distinguishable reasons `role` can be
    missing from a /health payload, not one generic "unknown" — the old single
    fallback text asserted a version floor even when `gateway_version` in the
    SAME payload said the gateway was current, which is a false diagnosis
    exactly when it matters most (an operator running `doctor` to find out
    why their own token isn't working).

      1. `role` present → surfaced verbatim.
      2. `role` absent AND the gateway's own reported version predates
         ROLE_REPORTING_MIN_VERSION (or reports no parseable version at all)
         → the gateway genuinely never sends this field.
      3. `role` absent AND the gateway version is current → this caller's
         token was not accepted, so the gateway served the anonymous-slim
         /health shape, which has no `role` key regardless of gateway age.
    """
    if "role" in h:
        return h.get("role")
    predates = _gateway_predates(h.get("version"))
    if predates is False:
        return "not reported (token not accepted — anonymous payload)"
    gw = h.get("version")
    if gw is not None:
        return f"not reported (gateway {gw} predates {ROLE_REPORTING_MIN_VERSION})"
    return f"not reported (gateway version unknown, predates {ROLE_REPORTING_MIN_VERSION} assumed)"


async def check_gateway_compat() -> dict:
    """GET /health and compare the wire contract. Pure diagnostic; never raises.

    Returns a dict with a ``compat`` field of "ok" | "incompatible" | "unknown",
    plus a human-readable ``warning`` when the client and gateway disagree on
    API_VERSION. Used by the ``doctor`` command and to enrich error messages.
    """
    try:
        async with _async_client(3.0) as client:
            h = _reply_json(await client.get(f"{COORDINATOR_BASE}/health",
                                             headers=_request_headers()))
    except GatewayReplyError as exc:
        # The gateway answered. Marking this unreachable would send doctor to restart a service
        # that is only refusing.
        return {"reachable": True, "error": exc.payload.get("message", str(exc)),
                "compat": "unknown"}
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
    # agent and role are present only on authenticated /health; an anonymous or older gateway
    # omits them. A missing role is read by _role_diagnosis.
    if "agent" in h:
        diag["agent"] = h.get("agent")
    diag["role"] = _role_diagnosis(h)
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

    # Fill project only when the caller left it empty. An untagged fact uses the consolidation
    # fallback, so it fragments away from its project's cluster and never reaches a summary.
    if not metadata.get("project"):
        derived = derive_project()
        if not derived:
            # If the cwd is not a project, walk an absolute source_ref the same way. A relative
            # ref names no location, and a guessed project is worse than none.
            ref = metadata.get("source_ref")
            if isinstance(ref, str) and os.path.isabs(ref):
                derived = derive_project(os.path.dirname(ref))
        if derived:
            metadata["project"] = derived
            _append_log("memory_bridge", 3, "project_derived", {"project": derived})

    try:
        async with _async_client(60.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/save",
                json={"content": content, "metadata": metadata, "agent_id": AGENT_ID},
                headers=_request_headers(),
            )
            result = _reply_json(r, log_auth=True)
    except GatewayReplyError as exc:
        # A 401 was already logged as auth_failed inside _reply_json; a save_failed here would
        # double-count it.
        # Every other answered refusal logs save_failed. Those used to be recorded as
        # coordinator_down, which was a gateway that had answered.
        if exc.logged_event is None:
            _append_log("memory_bridge", 2, "save_failed",
                        {"response": exc.payload, "content_preview": content[:100]}, content)
        return exc.payload
    except Exception as exc:
        _append_log("memory_bridge", 2, "coordinator_down", {"content_preview": content[:100]}, content)
        return await _warn_on_skew(_coordinator_unavailable(exc))

    if result.get("status") == "success":
        pg_id    = result.get("pg_id")
        entities = metadata.get("entities", [])
        _append_log("memory_bridge", 3, "save_success",
                    {"pg_id": pg_id, "source": metadata.get("source"), "entity_count": len(entities)},
                    content)
        # A fact with no entities never reaches synthesis. A decision or retrospective mints
        # none and inherits topics from its grounding, so empty grounded_in is the defect there,
        # not no_entities.
        if metadata.get("type") in ("decision", "retrospective"):
            if not metadata.get("grounded_in"):
                _append_log("memory_bridge", 1, "no_grounding",
                            {"pg_id": pg_id, "source": metadata.get("source"),
                             "type": metadata.get("type")}, content)
        elif not entities:
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
            result = _reply_json(r)
    except GatewayReplyError as exc:
        return exc.payload
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
            result = _reply_json(r)
    except GatewayReplyError as exc:
        return exc.payload
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc))
    return result


async def _search_payload(query: str, limit: int = 5, project: str = None,
                           domains: list = None, since: str = None) -> dict:
    """The raw gateway payload for one search call — the HTTP call and error
    handling `search_and_rerank()` used to inline, pulled out so a caller
    that needs more than the bare results list (v0.9.62: the keyword-fallback
    headline, `_fallback_warning`) can derive it from the SAME call instead
    of a second HTTP round trip. Always a dict: the decoded success payload,
    or this client's own error dict (`exc.payload` / `_coordinator_unavailable`
    already return one) — never the bare results list `search_and_rerank`
    unwraps it into."""
    # The reranker dominates this call and its cost tracks the payload, so the wait comes from
    # the gateway's published ceiling, not a constant.
    ceiling = search_ceiling(await _gateway_capability(), await _gateway_capacity())
    body = {"query": query, "limit": limit, "agent_id": AGENT_ID}
    # Extra fields are filters, not query text. An unfiltered call must send the same body it
    # always sent.
    if project:
        body["project"] = project
    if domains:
        body["domains"] = domains
    if since:
        body["since"] = since
    try:
        async with _async_client(ceiling) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/search",
                json=body,
                headers=_request_headers(),
            )
            return _reply_json(r, log_auth=True)
    except GatewayReplyError as exc:
        return exc.payload
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc, ceiling))


async def search_and_rerank(query: str, limit: int = 5, project: str = None,
                             domains: list = None, since: str = None) -> list | dict:
    result = await _search_payload(query, limit, project=project,
                                    domains=domains, since=since)
    return result.get("results", result)


def _unranked_warning(results) -> str | None:
    """One line for stderr when some rows in a search result are vector-order,
    not reranked — the gateway marks each row ``ranked: false`` when the
    reranker timed out and it served candidate/vector order instead. A
    positional result printed silently in that state reads as ranked when it
    is not; the JSON to stdout carries the per-row truth already, this is
    just the operator-facing headline. None when ``results`` is not a list of
    rows (an error payload, an empty result) or nothing is unranked.

    T-07 (PR #310 review): the returned SENTENCE (no leading/trailing
    decoration) is the shared core both front doors present — MCP's
    equivalent ``vector_skill._unranked_warning`` must return the identical
    string for the identical input; a parity test holds the two in step
    exactly like ``search_ceiling``'s S5. Each door decorates it in its own
    idiom (a bare stderr line here, a ``NOTE: …`` prefix there)."""
    if not isinstance(results, list):
        return None
    unranked = sum(1 for row in results if isinstance(row, dict) and row.get("ranked") is False)
    if not unranked:
        return None
    return (f"{unranked} of {len(results)} results are UNRANKED — the reranker "
            f"timed out, this is vector order (see backend_capability on /health)")


def _fallback_warning(payload: object) -> str | None:
    """One line for stderr when the gateway served a KEYWORD (substring)
    fallback because the embedder was unavailable — `coordinator.py` answers
    that case honestly with ``{"status":"success","fallback":"keyword",
    "results":[...]}`` rather than failing the search, but until this both
    clients silently stripped the envelope and only the results list reached
    the operator. A natural-language query almost never ILIKE-matches, so the
    common shape of that silence was an EMPTY list reading as "nothing
    known" — this MUST fire on ``results: []`` too, which is the one case
    `_unranked_warning` (rows marked ``ranked: false``) can never catch (fact:1609).

    Input is the RAW gateway payload (a dict, e.g. from `_search_payload`),
    not the unwrapped results list `_unranked_warning` takes — the
    ``fallback`` marker lives one level up from ``results``. None unless
    `payload` is a dict with ``fallback == "keyword"``.

    Mirrors `_unranked_warning`'s parity discipline (T-07, PR #310 review):
    the returned SENTENCE is the shared core both front doors present —
    `vector_skill._fallback_warning` must return the identical string for
    the identical input; a parity test holds the two in step."""
    if not isinstance(payload, dict) or payload.get("fallback") != "keyword":
        return None
    results = payload.get("results")
    n = len(results) if isinstance(results, list) else 0
    return (f"EMBEDDING UNAVAILABLE — keyword (substring) fallback served "
            f"{n} result(s), unranked; the embedder is down or still "
            f"starting (see embedder on /health)")


def _stale_projection_note(capability: dict | None) -> str | None:
    """B1/T-02 (PR #310 review): a backend whose block carries
    ``projection_stale: true`` still has its number USED by ``search_ceiling``
    when it has one (only a `status: "failing"`/stale block with NO number is
    treated as unknown-cost) — but nothing said so out loud. This names which
    backend and, when the gateway reports ``projection_age_s`` (PR-A), for how
    long. None when nothing is stale — the common case pays nothing."""
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


def _search_argparser() -> "argparse.ArgumentParser":
    """T-08 (PR #310 review): pulled out of ``main()``'s inline dispatch so a
    test can call ``.format_help()``/read ``.description`` directly, rather
    than the B4 documentation unit (SEARCH_TIMEOUT_S mention below) having
    zero test coverage because nothing could reach the parser without also
    running the search action."""
    p = argparse.ArgumentParser(
        prog="memory_bridge.py search", add_help=False,
        description="Search shared memory. The wait is sized from the "
                    "gateway's own published backend capability, not a "
                    "constant — set SEARCH_TIMEOUT_S (env) to pin an "
                    "explicit override instead.",
    )
    p.add_argument("limit", nargs="?", type=int, default=5)
    p.add_argument("--project", default=None, metavar="NAME",
                   help="restrict to records BELONGING to this project — a "
                        "named place is a FILTER, not query text. An "
                        "unregistered name is not refused, it simply "
                        "matches nothing.")
    # Repeatable and never comma-split, same as save's --domain. The gateway ORs the names.
    p.add_argument("--domain", action="append", default=None, metavar="NAME",
                   dest="domains",
                   help="restrict to records in this SECTION of the "
                        "project. REPEAT for several (OR semantics — any "
                        "match qualifies). Same 'filter, not query text' "
                        "rule as --project.")
    p.add_argument("--since", default=None, metavar="ISO_DATE",
                   help="restrict to records created at/after this ISO "
                        "date or datetime, e.g. 2026-08-01 or "
                        "2026-08-01T00:00:00. A named time is a FILTER, "
                        "not query text.")
    return p


def _save_argparser() -> "argparse.ArgumentParser":
    """T-08 (PR #310 review): see ``_search_argparser`` — same reason."""
    p = argparse.ArgumentParser(
        prog="memory_bridge.py save",
        description="Save a fact, optionally superseding an existing one. "
                    "project is derived from the working directory (walking "
                    "up to the nearest .git/CLAUDE.md/AGENTS.md) unless "
                    "SHARED_MEMORY_PROJECT (env) overrides it — for callers "
                    "saving from outside the project root.",
    )
    p.add_argument("content", help="Fact content")
    p.add_argument("metadata", nargs="?", default="{}", help="Metadata JSON (optional)")
    p.add_argument("--supersedes", type=int, default=None,
                   help="pg_id of an existing fact this save supersedes "
                        "(soft-retire: old fact kept, flagged, hidden from search)")
    # Repeatable and never comma-split. A separator that can occur inside a section name is not
    # a delimiter — the same trap --alternatives taught.
    p.add_argument("--domain", action="append", default=None, metavar="NAME",
                   help="a registered SECTION of this project, e.g. --domain "
                        "operations. REPEAT the flag for several; the value is "
                        "stored verbatim and never split. Sections are "
                        "project-local, so the same word under another project "
                        "is a different section. Optional — a record with none "
                        "is filed under its project, which is always correct. "
                        "An unregistered name returns 400 domain_unknown with "
                        "near matches; add \"new_domain\": true to the metadata "
                        "to register it, after asking the operator.")
    return p


def query_graph(cypher: str, params: dict = None) -> list | dict:
    try:
        with _sync_client(30.0) as client:
            r = client.post(
                f"{COORDINATOR_BASE}/memory/graph",
                json={"cypher": cypher, "params": params or {}},
                headers=_request_headers(),
            )
        result = _reply_json(r, log_auth=True)
    except GatewayReplyError as exc:
        return exc.payload
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
        return _reply_json(r)
    except GatewayReplyError as exc:
        return exc.payload
    except Exception as exc:
        return _coordinator_unavailable(exc)


def get_health_payload() -> dict | None:
    """The gateway's /health payload, or None if it could not be fetched.

    Separate from ``check_gateway_compat`` (which reads only the three keys an
    anonymous caller receives) because this one wants the AUTHENTICATED shape:
    ``dependencies`` and ``warnings`` are operational detail about the
    deployment and are not served anonymously.

    Never raises: `status` is a diagnostic command, and a report that refuses to
    print because one of its two sources was unreachable is worse than a report
    that prints the half it has.
    """
    try:
        with _sync_client(HEALTH_PROBE_TIMEOUT_S) as client:
            r = client.get(f"{COORDINATOR_BASE}/health", headers=_request_headers())
        # /health answers 503 when an encoder is down, and that body is the verdict this
        # renders, so 503 is kept rather than discarded (fact:1503).
        return _reply_json(r, accept_status=(503,))
    except Exception:
        return None


def _age_phrase(ts: str | None) -> str:
    """Render an ISO-8601 telemetry timestamp as an age, or '—' when absent.

    Kept a pure function so a mutation check can bite it. Absence is rendered
    honestly rather than as "0s ago": a null last_ts means the event has not
    happened in the gateway's current process, which is a different statement
    from "it happened just now"."""
    if not ts:
        return "—"
    try:
        when = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        # TypeError as well as ValueError: a non-string timestamp must not take down the status
        # report. Render the field and continue.
        return str(ts)
    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    delta = int((now - when).total_seconds())
    if delta < 0:
        # A negative age is clock skew or a backward jump, not a framework bug, so name the
        # cause instead of printing a negative.
        return f"{ts} (clock skew: stamp is {abs(delta)}s in the future)"
    return f"{delta}s ago"


def format_health_verdict(health: dict | None) -> list[str]:
    """The /health verdict lines: one enum per dependency, then any warnings.

    ⛔ THE VERDICT COMES FROM THE GATEWAY, NOT FROM HERE. Before v0.9.74 every
    consumer derived its own health from telemetry numbers — the monitor had one
    opinion about when the outbox was unwell, this client had none, and a third
    consumer would have invented a third. The threshold now lives server-side
    and this function only renders what it was told.

    A pure function so a mutation check can bite it, and tolerant of an OLDER
    gateway: no `dependencies` key means a pre-0.9.74 server, which is not an
    error — it is a server that cannot answer the question yet, and saying
    nothing is better than inventing a verdict on its behalf.
    """
    if not isinstance(health, dict):
        return []
    deps = health.get("dependencies")
    warnings = health.get("warnings") or []
    if not isinstance(deps, dict):
        return []
    lines = [f"  gateway: {health.get('status', '?')}"]
    unwell = [(name, d) for name, d in sorted(deps.items())
              if isinstance(d, dict) and d.get("state") not in ("ok", None)]
    if unwell:
        for name, d in unwell:
            reason = d.get("reason")
            lines.append(f"    {name}: {d['state']}"
                         + (f" ({reason})" if reason else ""))
    else:
        # Name the all-ok count. An empty section would read as nothing checked.
        lines.append(f"    all {len(deps)} dependencies ok")
    for w in warnings:
        if isinstance(w, dict):
            lines.append(f"    ⚠ {w.get('key')}: {w.get('observed')} "
                         f"> {w.get('limit')} {w.get('unit', '')}".rstrip())
    return lines


def format_status(payload: dict, health: dict | None = None) -> str:
    """Render the telemetry snapshot as a compact human-readable report.

    `health` is the /health payload, rendered FIRST when supplied: the numbers
    below are the detail, and the question an operator opens this with is
    whether the system is usable at all.
    """
    if payload.get("status") != "success":
        return json.dumps(payload, indent=2)
    t  = payload["telemetry"]
    pg = t.get("postgres", {})
    nj = t.get("neo4j", {})
    lines = [f"Shared-memory status  @ {t.get('timestamp','?')}"]
    lines.extend(format_health_verdict(health))
    if "error" in pg:
        lines.append(f"  postgres: ERROR {pg['error']}")
    else:
        cs = pg.get("community_summaries", {})
        _sup = pg.get('technical_docs_superseded', 0)
        lines.append(f"  technical_docs:      {pg.get('technical_docs','?')}"
                     + (f" (superseded {_sup})" if _sup else ""))
        # Only the four census counts fit a status line. A count the gateway did not report is
        # omitted, not printed as 0.
        _ob = t.get("outbox")
        _ob = _ob if isinstance(_ob, dict) else {}
        _census = {k: _ob[k] for k in ("pending", "applied", "rem_reviewed", "failed")
                   if _ob.get(k) is not None}
        lines.append(f"  outbox:              {_census}")
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
    # REM is computed apart from the graph census, so a Neo4j error and an enrichment error stay
    # on their own lines.
    rem = t.get("rem", {})
    if "error" in rem:
        lines.append(f"  rem: ERROR {rem['error']}")
    else:
        # A record at the attempt cap still counts as REM pending but has left the queue, so a
        # dead-lettered backlog needs its own line.
        _dead = rem.get("dead_lettered", 0) or 0
        _failing = rem.get("failing", 0) or 0
        if _dead or _failing:
            _warn = "  ⚠ operator reset needed" if _dead else ""
            lines.append(f"  REM enrichment: {_failing} retrying | "
                         f"{_dead} DEAD-LETTERED at {rem.get('max_attempts','?')} "
                         f"attempts{_warn}")
        # Printed only once passed-over or starved climbs above zero; both stay 0 until the solo
        # backlog is large enough to matter (decision 890).
        _passed_over = rem.get("passed_over", 0) or 0
        _starved = rem.get("starved_pending", 0) or 0
        if _passed_over or _starved:
            lines.append(f"  REM fairness: {_passed_over} passed-over event(s) | "
                         f"{_starved} record(s) at/above starvation threshold")
    # Entity-graph shape (ADR-017). Singletons are entities mentioned by one fact, a
    # fragmentation proxy.
    eg = t.get("entity_graph", {})
    if eg and "error" not in eg:
        _tot = eg.get("entities_total", 0) or 0
        lines.append(f"  entities:  {_tot} total | singletons {eg.get('singleton_entities',0)} "
                     f"| orphans {eg.get('orphan_entities',0)} "
                     f"| referenced {eg.get('genuinely_referenced_entities',0)}")
    elif "error" in eg:
        lines.append(f"  entities: ERROR {eg['error']}")
    # Nodes REM retired because their label contradicted the record their id names. They do not
    # drain, so a count above zero names a writer to fix.
    gi = t.get("graph_integrity", {})
    if gi and "error" not in gi:
        _bad = gi.get("invalid_nodes", 0) or 0
        if _bad:
            _why = ", ".join(f"{k} x{v}" for k, v in (gi.get("by_reason") or {}).items())
            lines.append(f"  graph integrity: {_bad} INVALID node(s) ⚠ — {_why}")
            lines.append("    (a writer produced nodes under the wrong label — "
                         "fix the writer, then repair the nodes)")
        else:
            lines.append("  graph integrity: ok (0 invalid nodes)")
    elif "error" in gi:
        lines.append(f"  graph integrity: ERROR {gi['error']}")
    nr = t.get("nrem", {})
    if nr and "error" not in nr:
        lines.append(f"  NREM cycles: {nr.get('total_cycles','?')} pending "
                     f"(facts {nr.get('fact_cycles',0)}, decisions {nr.get('decision_cycles',0)})")
    elif "error" in nr:
        lines.append(f"  nrem: ERROR {nr['error']}")
    # Tri-state GPU signal. "unknown" (nvtop absent or SLOT_AWARE off) is shown verbatim so the
    # LLM is never reported falsely idle.
    ib = t.get("inference_busy")
    if ib is not None:
        lines.append(f"  inference (LLM/GPU): {ib}")
    # Consolidation liveness (ADR-018): stalled means an eligible backlog, no fold within the
    # threshold, and nothing in flight.
    cn = t.get("consolidation", {})
    if cn and "error" not in cn:
        age = cn.get("last_success_age_seconds")
        age_s = f"{age}s ago" if age is not None else "—"
        # Name the cycle type on the age. A bare stalled age reads as the whole system being
        # dead when only one type is old.
        if cn.get("last_success_cycle_type"):
            age_s += f" ({cn['last_success_cycle_type']})"
        stalled_types = cn.get("stalled_types") or []
        flag = ("STALLED ⚠ [" + ", ".join(stalled_types) + "]") if stalled_types \
            else ("STALLED ⚠" if cn.get("stalled") else "ok")
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
                _err = c["last_error"]
                # A crash superseded by a later success is history, not a current error. Older
                # gateways omit "superseded", so absence stays the bare err line (fact:1609).
                if _err.get("superseded"):
                    _err_age = _err.get("age_seconds")
                    _err_age_s = f"{_err_age}s ago" if _err_age is not None else "—"
                    parts.append(f"last err {_err.get('class','?')} {_err_age_s}")
                else:
                    parts.append(f"err {_err.get('class','?')}")
            if c.get("eligible_clusters") is not None:
                cov = f"eligible {c['eligible_clusters']}"
                if c.get("eligible_oldest_age_seconds") is not None:
                    cov += f" (oldest {c['eligible_oldest_age_seconds']}s)"
                parts.append(cov)
            # Cost and folds are shown only after this type has run in the window. A zero from
            # no runs would read as failure rather than absence.
            if c.get("runs_24h"):
                thru = f"{c['runs_24h']} runs/24h"
                if c.get("cycle_seconds_avg") is not None:
                    thru += f" avg {c['cycle_seconds_avg']}s"
                thru += f", folds {c.get('folds_succeeded_24h', 0)}/{c.get('folds_attempted_24h', 0)}"
                parts.append(thru)
            # Deferred means the slot was busy; idle means the gate found nothing. Both zero is
            # a healthy cycle, so that line is omitted.
            if c.get("deferred_24h") or c.get("idle_24h"):
                parts.append(
                    f"non-runs {c.get('deferred_24h', 0)} deferred"
                    f"/{c.get('idle_24h', 0)} idle")
            lines.append(f"    {ct}: " + ", ".join(parts))
    elif "error" in cn:
        lines.append(f"  consolidation: ERROR {cn['error']}")
    # Credential failures are printed only when non-zero, same as the enrichment and fairness
    # lines. A healthy run would otherwise be noise.
    # Each count carries the age of its own last event. The counters reset on gateway restart,
    # so a diff of polls would read that as no failures.
    cr = t.get("credentials", {})
    if cr and "error" not in cr:
        _tvf = cr.get("token_verify_failed", 0) or 0
        _crd = cr.get("credentialed_route_denied", 0) or 0
        _drop = cr.get("audit_log_dropped", 0) or 0
        if _tvf:
            lines.append(f"  credentials: {_tvf} token verification failure(s) "
                         f"| last {_age_phrase(cr.get('token_verify_failed_last_ts'))} "
                         f"(since gateway start)")
        if _crd:
            # Allowlist denials are otherwise invisible on status, so a non-zero count is
            # printed here.
            lines.append(f"  credentials: {_crd} credentialed-route denial(s) "
                         f"| last {_age_phrase(cr.get('credentialed_route_denied_last_ts'))} "
                         f"(since gateway start)")
        if _drop:
            lines.append(f"  credential audit: {_drop} LINE(S) DROPPED ⚠ "
                         f"| last {_age_phrase(cr.get('audit_log_dropped_last_ts'))} "
                         f"— the audit trail is incomplete")
    elif "error" in cr:
        lines.append(f"  credentials: ERROR {cr['error']}")
    # Per-backend LLM faults. credential (401/403/quota) is the fix-the-key signal; transient
    # retries on its own and is reported without a flag.
    _llm_section = t.get("llm")
    lf = _llm_section.get("faults", {}) if isinstance(_llm_section, dict) else {}
    if isinstance(lf, dict):
        for backend, f in sorted(lf.items()):
            if not isinstance(f, dict):
                continue
            parts = []
            # `or {}` lets a truthy non-dict through, and a string from a drifted gateway would
            # then raise and take down the report. Type-check instead.
            _llm = f.get("llm")
            _llm = _llm if isinstance(_llm, dict) else {}
            for label, sub in (("credential", _llm.get("credential")),
                               ("transient", _llm.get("transient")),
                               ("gateway", f.get("gateway"))):
                if isinstance(sub, dict) and (sub.get("count") or 0):
                    seg = f"{label} {sub['count']}"
                    last = sub.get("last")
                    if isinstance(last, dict) and last.get("ts"):
                        seg += f" (last {_age_phrase(last['ts'])})"
                    parts.append(seg)
            if parts:
                _cred = _llm.get("credential")
                _flag = " ⚠ fix the key" if (
                    isinstance(_cred, dict) and _cred.get("count")) else ""
                lines.append(f"  llm faults [{backend}]: " + ", ".join(parts) + _flag)
    return "\n".join(lines)


# ── Decision shortcut ─────────────────────────────────────────────────────────

def alternatives_list(alternatives) -> list[str]:
    """One value in, ONE alternative out — verbatim, and never split.

    This used to be ``alternatives.split(",")``. A well-written alternative
    contains commas — *"use explicit Neo4j transactions for atomicity (APOC not
    available, auto-commit is the existing pattern)"* — so it was stored as two
    fragments that do not stand alone, in Postgres AND in the graph's ADR
    properties, with no warning. Measured across the corpus: 21% of the
    decisions carrying alternatives held at least one fragment, and nothing in
    the record said which pieces had once been a single entry.

    A capture surface must not accept a value it cannot faithfully represent,
    so the separator is gone rather than replaced. Repeat the flag once per
    alternative; a value that arrives as one string is one alternative, which is
    at worst under-split and never invents an option nobody wrote.

    Accepts a list (the CLI's repeated flag, or a JSON array over the wire) or a
    lone string. Blank entries are dropped — an empty value is an absence.
    """
    if alternatives is None:
        return []
    if isinstance(alternatives, str):
        alternatives = [alternatives]
    return [str(a).strip() for a in alternatives if str(a).strip()]


def build_decision_metadata(
    title: str,
    decided_by: str,
    project: str,
    rationale: str,
    source: str = None,
    assisted_by: str = "",
    alternatives=None,
    confidence: str = "",
    entities: str = "",
    grounded_in: str = "",
    elicited: bool = False,
    new_project: bool = False,
    distinct_from: str = "",
    domains=None,
    new_domain: bool = False,
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
    alts = alternatives_list(alternatives)
    if alts:
        decision["alternatives"] = alts
    if confidence:
        decision["confidence"] = confidence

    metadata = {
        "type": "decision",
        "source": source or AGENT_ID,
        # Top-level project is the operator-asserted value, the same one as in the blob. Readers
        # of Postgres trust this key.
        # Left unset, save_artifact fills it from the cwd (`.claude` under ~/.claude, nothing
        # from ~) and the two fields disagree (fact:1757).
        "project": project,
        "entities": [e.strip() for e in entities.split(",") if e.strip()],
        "decision": decision,
    }
    # grounded_in is the pg ids this rests on, written as ROLE edges; include at least the
    # conversation fact (decision 552).
    # A role after the colon is optional; a bare id lets fact_kind pick the default
    # (decision 582). Roles: based_on, considered, rejected, under_conditions, informed_by.
    gi: list[int] = []
    grounded_roles: dict[str, str] = {}
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
    # elicited means the spine fields were asked of the operator. An elicited null is
    # deliberate, and coverage telemetry counts the ask (decision 559).
    if elicited:
        metadata["elicited"] = True
    # new_project registers a name the operator confirmed. It is not inferred: an agent setting
    # it to clear a rejection turns a typo into a permanent project.
    if new_project:
        metadata["new_project"] = True
    # Domains are asserted, not inherited from evidence, and they go in the decision blob beside
    # project. That is the half the gateway reads a judgement's axes from.
    # --domain was parsed and dropped for one release, so the record silently inherited its
    # evidence's sections and looked correct.
    if domains:
        decision["domains"] = list(domains)
    if new_domain:
        metadata["new_domain"] = True
    # Registered projects this new name is deliberately not. The gateway refuses a confusable
    # name until they are named, which an agent cannot do without looking.
    df = [d.strip() for d in (distinct_from or "").split(",") if d.strip()]
    if df:
        metadata["confirm_distinct_from"] = df
    return content, metadata


# ── Retrospective shortcut ────────────────────────────────────────────────────

def build_retrospective_payload(
    pg_id: int,
    rating: str,
    notes: str,
    date: str = "",
    source: str = None,
    grounded_in: str = "",
    source_ref: str = "",
    elicited: bool = False,
) -> dict:
    """Build the JSON payload for POST /memory/retrospective (API v2 —
    retro-as-record: the gateway mints a full searchable record and returns
    its own pg_id).

    grounded_in uses the same "pgid[:role],pgid" grammar as save_decision —
    the facts that MEASURED this outcome (test-grounded retrospectives,
    decision 542). A retrospective carries no entities — only facts do
    (decision:1664) — so this never sends the field; the gateway inherits
    its topics from the grounding facts. Pure function — no I/O, no side
    effects.
    """
    payload = {
        "pg_id": pg_id,
        "rating": rating.strip().lower(),
        "notes": notes,
        "date": date or datetime.now().date().isoformat(),
        "agent_id": source or AGENT_ID,
    }
    gi: list[int] = []
    grounded_roles: dict[str, str] = {}
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
    if source_ref:
        payload["source_ref"] = source_ref
    if elicited:
        payload["elicited"] = True
    return payload


async def save_retrospective_artifact(
    pg_id: int,
    rating: str,
    notes: str,
    date: str = "",
    source: str = None,
    grounded_in: str = "",
    source_ref: str = "",
    elicited: bool = False,
) -> dict:
    # Reject an unknown rating here, naming the outcome states, rather than waiting for the
    # gateway's 400.
    if rating.strip().lower() not in RETRO_RATINGS:
        return {"status": "error",
                "message": (f"rating must be one of {list(RETRO_RATINGS)} — outcome "
                            "states, not valence; put the nuance in --notes")}
    payload = build_retrospective_payload(pg_id, rating, notes, date, source,
                                          grounded_in, source_ref, elicited)
    try:
        async with _async_client(60.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/retrospective",
                json=payload,
                headers=_request_headers(),
            )
            return _reply_json(r, log_auth=True)
    except GatewayReplyError as exc:
        return exc.payload
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

    # The payload lives on the record node now; older installs still keep it on the edge. Read
    # node fields for a Retrospective, edge fields otherwise.
    _RETRO_FIELDS = (
        "WITH d, o, t,"
        " CASE WHEN t:Retrospective THEN t.rating ELSE o.rating END AS rating,"
        " CASE WHEN t:Retrospective THEN coalesce(t.rem_summary, t.content)"
        "      ELSE o.notes END AS notes,"
        " CASE WHEN t:Retrospective THEN t.date ELSE o.date END AS date"
    )

    if template == "retrospectives":
        rating = _safe(getattr(args, "rating", ""))
        lines = ["MATCH (d:Decision)-[o:HAD_OUTCOME]->(t)", _RETRO_FIELDS]
        if rating:
            lines.append(f"WHERE rating CONTAINS '{rating}'")
        lines.append(
            "RETURN d.title, d.pg_id, rating, notes, date ORDER BY date DESC"
        )
        return "\n".join(lines)

    elif template == "why-to-check":
        title   = _safe(getattr(args, "title",   ""))
        project = _safe(getattr(args, "project", ""))
        lines = ["MATCH (d:Decision)-[o:HAD_OUTCOME]->(t)"]
        if title:
            lines.append(f"WHERE d.title CONTAINS '{title}'")
        lines += [
            "OPTIONAL MATCH (d)-[:WAS_ATTRIBUTED_TO]->(h:Human)",
            "OPTIONAL MATCH (d)-[:PROJECT_OF]->(p:Project)",
            _RETRO_FIELDS.replace("WITH d, o, t,", "WITH d, o, t, h, p,"),
        ]
        if project:
            lines.append(f"WHERE p.name CONTAINS '{project}'")
        lines.append(
            "RETURN d.title, d.pg_id, rating, notes, "
            "date, h.name AS decided_by ORDER BY date DESC"
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
            "error": "Usage: python memory_bridge.py [--version|doctor|status|graph|query|search|save|save_decision|save_retrospective|lineage|supersede|review-hold] ..."
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
        # status also reads /health: dependency enums and warnings say whether it is usable. A
        # missing /health adds no lines rather than failing the report.
        health = get_health_payload()
        if "--json" in sys.argv:
            print(json.dumps({**payload, "health": health}, indent=2))
        else:
            print(format_status(payload, health))
        return
    elif action == "lineage":
        # Record state, dream-cycle stamps, and what it consolidated into. Joins stay
        # gateway-side (ADR-014); this only calls the endpoint.
        # A bare id or a qualified reference (fact:816, summary:87). An id is unique only within
        # its table, so a bare summary id resolves against the wrong table.
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: memory_bridge.py lineage <pg_id|type:id>"}))
            sys.exit(1)
        ref = sys.argv[2].strip()
        head, _, tail = ref.partition(":")
        valid = (tail.lstrip("-").isdigit()
                 and head.lower() in ("fact", "decision", "retrospective",
                                      "summary", "insight")) if tail else \
                ref.lstrip("-").isdigit()
        if not valid:
            print(json.dumps({"error": (
                "Usage: memory_bridge.py lineage <pg_id|type:id> — type is one of "
                "fact, decision, retrospective, summary, insight")}))
            sys.exit(1)
        pid = ref
        try:
            async with _async_client(30.0) as client:
                r = await client.get(
                    f"{COORDINATOR_BASE}/memory/status/{pid}",
                    headers=_request_headers(),
                )
                print(json.dumps(_reply_json(r), indent=2))
        except GatewayReplyError as exc:
            print(json.dumps(exc.payload, indent=2))
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
            print(json.dumps({
                "error": "Usage: memory_bridge.py search <query> [limit] "
                         "[--project NAME] [--domain NAME ...] [--since ISO_DATE]"
            }))
            sys.exit(1)
        query = sys.argv[2]
        p = _search_argparser()
        sargs = p.parse_args(sys.argv[3:])
        payload = await _search_payload(query, sargs.limit, project=sargs.project,
                                         domains=sargs.domains, since=sargs.since)
        results = payload.get("results", payload)
        warning = _unranked_warning(results)
        if warning:
            print(warning, file=sys.stderr)
        fallback_warning = _fallback_warning(payload)
        if fallback_warning:
            print(fallback_warning, file=sys.stderr)
        stale_note = _stale_projection_note(await _gateway_capability())
        if stale_note:
            print(f"NOTE: {stale_note} — the ceiling above still used its last "
                  f"number as a lower bound (see backend_capability on /health)",
                  file=sys.stderr)
        print(json.dumps(results, indent=2))
    elif action == "save":
        p = _save_argparser()
        sargs = p.parse_args(sys.argv[2:])
        metadata = sargs.metadata
        if sargs.supersedes is not None or sargs.domain:
            try:
                mobj = json.loads(metadata) if isinstance(metadata, str) else metadata
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"status": "error", "message": f"Invalid metadata JSON: {e}"}))
                sys.exit(1)
            if not isinstance(mobj, dict):
                mobj = {}
            if sargs.supersedes is not None:
                mobj["supersedes"] = sargs.supersedes
            if sargs.domain:
                mobj["domains"] = sargs.domain
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
        # Optional here: derive_project fills the folder name. The gateway still requires a
        # project, so a save outside any root fails instead of storing none.
        p.add_argument("--project",     default="",     help="Project context (default: project folder name)")
        p.add_argument("--domain", action="append", default=None, metavar="NAME",
                       help="a registered SECTION of this project; REPEAT for "
                            "several. Omit stores no section. It does not take "
                            "the grounding facts' sections. `belonging` is "
                            "read-side only.")
        p.add_argument("--new-domain", action="store_true",
                       help="THE OPERATOR HAS CONFIRMED these sections are new and "
                            "registers them. Ask first — the registry exists so a "
                            "misspelling and a new section stop being one event.")
        p.add_argument("--rationale",   required=True,  help="Why this decision was made")
        p.add_argument("--source",      default=AGENT_ID,
                       help="Agent/model saving this record (default: $AGENT_ID)")
        p.add_argument("--assisted-by", default="",
                       help="Comma-separated AI agents that assisted")
        p.add_argument("--alternatives", action="append", default=None,
                       metavar="ALTERNATIVE",
                       help="ONE alternative that was considered. Repeat the flag "
                            "for each one — the value is stored VERBATIM and is "
                            "never split, so an alternative may contain commas, "
                            "brackets and any other punctuation.")
        p.add_argument("--confidence",  default="",
                       help="Confidence level (e.g. high, medium, low)")
        p.add_argument("--grounded-in", default="",
                       help="pg_ids of the records this decision rests on, comma "
                            "separated, each optionally carrying the ROLE it plays: "
                            "'601:based_on,602,603:rejected'. Roles: based_on, "
                            "considered, rejected, under_conditions, informed_by; a "
                            "bare id takes a default from that fact's evidential "
                            "kind. THIS is what gives the decision its topics — it "
                            "mints none of its own, so an ungrounded decision reaches "
                            "no cluster and never enters synthesis. Include at least "
                            "the conversation fact. Legitimately empty only when the "
                            "call really was made on experience; the gateway flags "
                            "that rather than refusing it.")
        p.add_argument("--elicited",    action="store_true",
                       help="The spine fields were elicited from the operator (an elicited "
                            "null is deliberate; coverage telemetry counts the ask)")
        p.add_argument("--distinct-from", default="",
                       help="Comma-separated REGISTERED projects this new project "
                            "is deliberately not. Required only when the gateway "
                            "refuses the name as confusable with one of them — and "
                            "it names which. Confirm with the operator first.")
        p.add_argument("--new-project", action="store_true",
                       help="THE OPERATOR HAS CONFIRMED this project is new and "
                            "registers it. Only ever pass this after asking: a "
                            "decision can now introduce a project, and the whole "
                            "point of the registry is that a misspelling and a new "
                            "project stop being the same event. Without it, an "
                            "unregistered name is refused and answered with near "
                            "matches from the registry.")
        args = p.parse_args(sys.argv[2:])
        content, metadata = build_decision_metadata(
            title=args.title,
            decided_by=args.decided_by,
            project=args.project or derive_project(),
            rationale=args.rationale,
            source=args.source,
            assisted_by=args.assisted_by,
            alternatives=args.alternatives,
            confidence=args.confidence,
            grounded_in=args.grounded_in,
            elicited=args.elicited,
            new_project=args.new_project,
            distinct_from=args.distinct_from,
            domains=args.domain,
            new_domain=args.new_domain,
        )
        print(json.dumps(await save_artifact(content, metadata), indent=2))
    elif action == "save_retrospective":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py save_retrospective",
            description="Record an outcome for a past decision as a full searchable "
                        "record (Retrospective node behind the decision's HAD_OUTCOME "
                        "trigger edge).",
        )
        p.add_argument("--pg-id",  required=True, type=int,
                       help="pg_id of the target Decision")
        p.add_argument("--rating", required=True,
                       help=f"The outcome STATE, not a sentiment: {list(RETRO_RATINGS)}. "
                            "'refined' means the decision evolved; 'pending' that it is "
                            "not yet judged. 'reversed' is STRUCTURAL — it marks the "
                            "decision superseded — so never reach for it merely to "
                            "retire a record, which writes a false statement into the "
                            "corpus. Nuance belongs in --notes.")
        p.add_argument("--notes",  required=True,
                       help="What actually happened / lessons learned (becomes the "
                            "record's searchable content)")
        p.add_argument("--date",   default="",
                       help="ISO date of outcome (default: today)")
        p.add_argument("--source", default=AGENT_ID,
                       help="Agent/model recording the outcome (default: $AGENT_ID)")
        p.add_argument("--grounded-in", default="",
                       required=True,
                       help="REQUIRED — pg_ids of the facts that MEASURED this "
                            "outcome, optionally with a role: '601,602:considered'. "
                            "Required here and optional on a decision because a "
                            "retrospective exists to report what measuring showed: "
                            "with nothing measured it asserts a verdict from nowhere, "
                            "and it also strands the decision it judges, which reaches "
                            "its own topics through this record. Refused with 400 "
                            "when absent.")
        p.add_argument("--source-ref",  default="",
                       help="THE INSTRUMENT THAT MEASURED THIS OUTCOME — the test "
                            "re-run, the live reading, the URL. A different question "
                            "from a fact's source_ref, which says where the KNOWLEDGE "
                            "came from, and one the grounding facts cannot answer for "
                            "this record: they may belong to another project and cite "
                            "a different file tree. A test-grounded decision earns a "
                            "retrospective that re-references the same tests.")
        p.add_argument("--elicited",    action="store_true",
                       help="The fields were elicited from the operator")
        args = p.parse_args(sys.argv[2:])
        print(json.dumps(
            await save_retrospective_artifact(
                pg_id=args.pg_id,
                rating=args.rating,
                notes=args.notes,
                date=args.date,
                source=args.source,
                grounded_in=args.grounded_in,
                source_ref=args.source_ref,
                elicited=args.elicited,
            ),
            indent=2,
        ))
    else:
        print(json.dumps({"error": f"Unknown action: {action}. Use --version|doctor|status|graph|query|search|save|save_decision|save_retrospective|lineage|supersede|review-hold"}))


if __name__ == "__main__":
    asyncio.run(main())
