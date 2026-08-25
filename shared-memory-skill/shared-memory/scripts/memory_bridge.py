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
    python memory_bridge.py review-edges [entity_relation|evidential] [N]
    python memory_bridge.py label-edges "12=correct,13=incorrect" [--promote 12]

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

VERSION = "0.9.52"
# Wire contract this client was built against. Must match the gateway's
# api_version (reported by GET /health). Bump only on breaking protocol changes.
# v3: review-edges / label-edges require the gateway's /memory/relations/* routes.
# v4 (project registry): a fact save without a REGISTERED metadata.project is
# rejected 400 carrying error=project_required|project_unknown plus near-match
# proposals. BREAKING for any client that saved untagged facts. The second
# submission is accepted in three forms: a proposal, new_project=true, or the
# reserved sentinel general_discussion.
API_VERSION = 4

# Relation-adjudication calibration families — MUST mirror
# relation_confidence.FAMILIES on the gateway (the thin client never imports
# server modules). Each family calibrates on its own operator-label curve.
RELATION_FAMILIES = ("entity_relation", "evidential")

# Retrospective outcome-state ratings — MUST mirror ontology.RETRO_RATINGS on
# the gateway (the thin client never imports server modules). Outcome STATES,
# not valence: 'reversed' drives the supersession cascade; nuance goes in notes.
RETRO_RATINGS = ("validated", "mixed", "refined", "pending", "reversed")
CLIENT_VERSION_HEADER = "X-SM-Api-Version"

# Skill-directory-scoped dotenv search (S-18, Credential_Custody_Plan
# PR A2) — exactly two candidates, in order, first definition wins:
#   1. script-adjacent .env — scripts/.env, co-located with this file
#   2. skill root .env — ../.env from here, e.g.
#      ~/.gemini/skills/shared-memory/.env (the documented install location)
# NEVER a parent-directory walk. python-dotenv's find_dotenv(usecwd=False)
# used to walk from this file up toward $HOME looking for the first ".env"
# it found anywhere on the way, so a stray $HOME/.env (some other tool's,
# or a leftover from a different agent's install) could silently supply
# AGENT_TOKEN/COORDINATOR_URL before this skill's own .env was ever
# consulted. Always invoke memory_bridge.py by absolute path so __file__
# resolves correctly (e.g. ~/.gemini/skills/shared-memory/scripts/).
_ENV_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
]
# SECURE_ENV_FILE — same contract as the server loader (secure_env.
# _select_env_file): a path names the EXACT env file this process loads, the
# empty string loads none at all (the test suite's hermeticity pin — in admin
# mode candidate 2 IS the live gateway .env, and importing this module from a
# test would otherwise setdefault its config, LLM_BACKENDS_JSON included,
# into the whole process's os.environ), and unset keeps the candidate walk.
_secure_env_file = os.environ.get("SECURE_ENV_FILE")
if _secure_env_file is not None:
    _ENV_CANDIDATES = [_secure_env_file.strip()] if _secure_env_file.strip() else []

# AGENT_TOKEN is read into a private variable and NEVER exported into this
# process's own os.environ (A1-deferred, S-18 follow-up): the client used to
# load its whole .env — AGENT_TOKEN included — into os.environ, the same
# "secret sitting in a long-lived process's own environment" class PR A1
# closed server-side (visible via this process's own /proc/<pid>/environ
# and to any child it might exec). An operator's own real
# `export AGENT_TOKEN=...` still wins — checked FIRST, before any file is
# parsed, mirroring secure_env.get_secret()'s precedence on the gateway.
# _AGENT_TOKEN_FROM_FILE is populated once below, from the file only, and
# is the seam tests use to neutralise a real on-disk .env during isolated
# runs (monkeypatch.setattr(memory_bridge, "_AGENT_TOKEN_FROM_FILE", "")).
_AGENT_TOKEN_FROM_FILE = ""

# Required fix (A2 security review, finding 7): a small, standalone mirror
# of secure_env.is_secret_key() (the GATEWAY's classification) -- duplicated
# rather than imported, because this client ships alone and must never
# depend on a server-only module (Group 1: the client/server surface
# split). Candidate 2 (_ENV_CANDIDATES[1], the skill root .env) IS the
# gateway .env when this same file is invoked in admin mode from the repo
# root, so without this predicate PG_PASSWORD/NEO4J_PASSWORD/AGENT_TOKENS/
# every provider key would land in setdefault() below and leak into this
# client process's own os.environ -- the identical class of leak S-18
# already closed for AGENT_TOKEN specifically, one level broader. AGENT_TOKEN
# itself is exempt here: it keeps its own dedicated _AGENT_TOKEN_FROM_FILE
# path above, never routed through os.environ either way. Everything else
# this predicate catches is simply skipped -- the client has no use for any
# of these values, so unlike the server there is no config-name allowlist
# carve-out and no dynamic token_env discovery to widen it.
#
# R6 (fix round 1, Opus review, probe-confirmed): this "mirror" had drifted
# from what it mirrors. secure_env.KNOWN_SECRET_NAMES gained PG_CONN in
# secure_env's OWN review round (a full DSN embeds the Postgres password
# verbatim -- postgresql://postgres:<pw>@host/db) and _SECRET_SUFFIXES was
# widened past _PASSWORD/_TOKEN/_API_KEY to also catch _SECRET/_KEY/
# _CREDENTIAL(S) -- neither change was ever brought over here. Probe-
# confirmed live: PG_CONN, a DEEPSEEK_SECRET-suffixed name, and an
# OPENROUTER_CREDENTIAL-suffixed name all landed in THIS client's own
# os.environ from a scratch shared-memory/.env with the environment
# pre-cleared -- exactly the class S-18/A2 finding 7 closed, reopened by
# drift. Both sets below are now IN SYNC with secure_env.py's own
# KNOWN_SECRET_NAMES / _SECRET_SUFFIXES; keep them that way (a contract test
# -- test_client_secret_mirror_parity.py -- pins the two lists against each
# other so the next drift fails loudly instead of needing a probe to find).
_CLIENT_KNOWN_SECRET_NAMES = {
    "PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY", "AGENT_TOKENS",
    "BACKUP_ADMIN_TOKEN", "PG_CONN",
}
_CLIENT_SECRET_SUFFIXES = (
    "_PASSWORD", "_TOKEN", "_API_KEY", "_SECRET", "_KEY",
    "_CREDENTIAL", "_CREDENTIALS",
)


def _is_client_secret_key(name: str) -> bool:
    """True if `name` must never be exported into this client's own
    os.environ (mirrors secure_env.is_secret_key(), narrowed to what this
    client can ever encounter). AGENT_TOKEN is excluded -- it has its own
    private-variable path and is never routed through this predicate."""
    if name == "AGENT_TOKEN":
        return False
    if name in _CLIENT_KNOWN_SECRET_NAMES:
        return True
    return name.upper().endswith(_CLIENT_SECRET_SUFFIXES)


try:
    from dotenv import dotenv_values  # parses without touching os.environ
    for _env in _ENV_CANDIDATES:
        if not (_env and os.path.exists(_env)):
            continue
        for _k, _v in dotenv_values(_env).items():
            if _v is None or not _k:
                continue
            if _k == "AGENT_TOKEN":
                if not _AGENT_TOKEN_FROM_FILE:
                    _AGENT_TOKEN_FROM_FILE = _v.strip()
                continue
            if _is_client_secret_key(_k):
                continue
            os.environ.setdefault(_k, _v)
except ImportError:
    # python-dotenv not installed — manually parse skill-adjacent .env files
    # so config/token are found when running bare `python` or `uv run --with httpx`.
    def _read_env_file(path: str) -> None:
        global _AGENT_TOKEN_FROM_FILE
        try:
            with open(path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    _v = _v.strip()
                    if not _k:
                        continue
                    if _k == "AGENT_TOKEN":
                        if not _AGENT_TOKEN_FROM_FILE:
                            _AGENT_TOKEN_FROM_FILE = _v
                        continue
                    if _is_client_secret_key(_k):
                        continue
                    if _k not in os.environ:   # first definition wins
                        os.environ[_k] = _v
        except OSError:
            pass
    for _env in _ENV_CANDIDATES:
        _read_env_file(_env)

COORDINATOR_BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
AGENT_ID         = os.environ.get("AGENT_ID", "memory_bridge")

# ── Search timeout sizing ────────────────────────────────────────────────────
# The SAME lesson as the server's rerank_ceiling(), on the side it was never
# applied to. A search costs what the RERANKER costs, and that tracks the total
# candidate payload — not the caller's `limit`, which barely moves it. Both
# clients shipped a CONSTANT ceiling (CLI 30 s, MCP 60 s) while the gateway sized
# its own rerank call from measured throughput and published the result on
# /health. The constants straddled the real cost, so searches failed
# intermittently and blamed a gateway that had answered /health 3 ms earlier.
# Measured 2026-08-06 at v0.8.56: real searches 19-35 s against a client ceiling
# of 30 s, while /health projected 127 s for a full payload (fact:1112).
#
# So the ceiling is DERIVED from the gateway's own published sizing instead of
# guessed at. The client then needs no re-tuning when the server's text window,
# hardware or model changes: the number it uses is the server's own number.
HEALTH_PROBE_TIMEOUT_S    = float(os.environ.get("HEALTH_PROBE_TIMEOUT_S", "3"))
SEARCH_TIMEOUT_S          = float(os.environ.get("SEARCH_TIMEOUT_S", "0") or 0)
SEARCH_TIMEOUT_FLOOR_S    = float(os.environ.get("SEARCH_TIMEOUT_FLOOR_S", "30"))
SEARCH_TIMEOUT_MAX_S      = float(os.environ.get("SEARCH_TIMEOUT_MAX_S", "300"))
SEARCH_TIMEOUT_FALLBACK_S = float(os.environ.get("SEARCH_TIMEOUT_FALLBACK_S", "120"))
SEARCH_SAFETY_FACTOR      = float(os.environ.get("SEARCH_SAFETY_FACTOR", "1.5"))
SEARCH_OVERHEAD_S         = float(os.environ.get("SEARCH_OVERHEAD_S", "15"))

# Markers that identify a project root, in priority order. `.git` first because a
# repository root is the least ambiguous boundary; the agent-instruction files are
# the fallback for project directories that are not repositories.
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
        # $HOME itself is a boundary, not a project: it commonly holds a CLAUDE.md
        # and would otherwise tag every stray save with the account name.
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
        return httpx.AsyncClient(timeout=timeout, transport=httpx.AsyncHTTPTransport(uds=uds))
    return httpx.AsyncClient(timeout=timeout)


def _sync_client(timeout: float) -> "httpx.Client":
    uds = _uds_path()
    if uds:
        return httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(uds=uds))
    return httpx.Client(timeout=timeout)


def search_ceiling(capability: dict | None, capacity: dict | None = None) -> float:
    """Client-side search timeout in seconds, derived from the gateway's own
    published backend sizing (``backend_capability`` on GET /health), and — when
    the gateway has one — its own measured worst case (``capacity`` on GET
    /health).

    Pure → unit-testable with no gateway present. Both probed backends contribute:
    the reranker dominates, but the query is embedded on the same path, and the
    two run on hardware that may or may not be shared.

    A missing, malformed or unprobed capability block yields
    ``SEARCH_TIMEOUT_FALLBACK_S``, which is deliberately well ABOVE the constant it
    replaces — the failure being fixed is a ceiling *below* the real cost, so an
    unknown cost must not fall back to the number already known to be too small.

    fact:1560 (grounded on decision:1114): that same rule also covers the MIXED
    case — one backend probes fine while the other reports ``status: "failing"``
    (or ``projection_stale: true``) with no positive projection of its own. The
    known backend's number is still only a LOWER bound on the true cost; a
    failing backend's true cost is unknown, not zero. So when any backend block
    carries one of those two EXPLICIT "I don't know" signals, the floor under
    the derivation is ``SEARCH_TIMEOUT_FALLBACK_S``, never
    ``SEARCH_TIMEOUT_FLOOR_S`` — ignorance of PART of the cost must not resolve
    to the number already known to be too small, exactly as ignorance of ALL of
    it does.

    This is narrower than "every backend must report a positive projection": a
    block that is simply ABSENT, malformed, carries a plain ``status: "error"``,
    or is ``"ok"`` with no projection at all does NOT trip the fallback floor by
    itself — only the two explicit signals above do (T-05, PR #310 review). Our
    own gateway's probe (``hive_mind_proxy._probe_capability``) never actually
    produces that narrower gap today — it always writes both blocks, and only
    ever as ``ok``/``too_slow``/``failing``, never ``ok`` with no projection —
    but this function also has to make sense of an older, third-party or future
    gateway's /health, so those shapes are exercised and pinned as documented
    behaviour below rather than assumed unreachable.

    When ``capacity`` carries the gateway's own measured numbers
    (``capacity["derived"]``), three of its fields are folded in too — the
    server's measured/derived worst case wins over the client's own theoretical
    projection whenever it is larger (never smaller: this only ever RAISES the
    ceiling):

      * ``client_ceiling_s`` — the server's own already-derived ceiling;
        compared as-is.
      * ``s_mean_s`` — the theoretical full-payload projection the GATEWAY
        itself computed, always present once the gateway has probed at all
        (T-02, PR #310 review: this is the one field that would have sized
        fact:1560's own measured 96-260s case correctly on the host that
        measured it — folding it in is what closes that gap rather than
        merely improving on it).
      * ``s_max_measured_s`` — a PROJECTION too, the same kind of number as
        ``projected_full_payload_s``, but computed over the coordinator's own
        observed MAXIMUM rerank payload instead of the theoretical full-payload
        one; ``None`` until real search traffic has been served this process's
        lifetime, unlike ``s_mean_s`` above.

    ``s_mean_s`` and ``s_max_measured_s`` are not yet safety-scaled for THIS
    client, so each gets the same ``SEARCH_SAFETY_FACTOR``/
    ``SEARCH_OVERHEAD_S`` treatment as the theoretical projection before being
    compared.

    ``SEARCH_TIMEOUT_S`` wins outright when set: the operator's escape hatch, and
    the only way to get a constant back.
    """
    if SEARCH_TIMEOUT_S > 0:
        return SEARCH_TIMEOUT_S

    projected, probed, unknown = 0.0, False, False
    for backend in ("reranker", "embedder"):
        block = (capability or {}).get(backend)
        if not isinstance(block, dict):
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
        # outside both probes, so they are ADDED rather than scaled — they do
        # not grow with encoder throughput.
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
        # T-02 (fact:1560): the gateway's own full-payload projection, always
        # present once ANY probe has run — unlike s_max_measured_s above,
        # which needs real search traffic first. This is the field that would
        # have sized fact:1560's measured case correctly; see the docstring.
        s_mean_s = capacity_derived.get("s_mean_s")
        if isinstance(s_mean_s, (int, float)) and s_mean_s > 0:
            derived = max(derived, s_mean_s * SEARCH_SAFETY_FACTOR + SEARCH_OVERHEAD_S)

    return min(derived, SEARCH_TIMEOUT_MAX_S)


_CAPABILITY_CACHE: dict | None = None
_CAPACITY_CACHE: dict | None = None
# CQ-03 (PR #310 review): guards the fetch-and-fill below against the race
# where two searches start in the same instant, both see an empty cache, and
# both fire a /health request. Module-level construction is safe without a
# running loop on the Python versions this project targets — the gateway's
# own equivalent (hive_mind_proxy._health_probe_lock) does the same.
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
    headers = {CLIENT_VERSION_HEADER: str(API_VERSION)}
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
        # Same hazard as _gateway_message: this is gateway-controlled text on
        # its way to a terminal or log — strip control characters before the
        # whitespace collapse and the cap (the non-JSON error page is exactly
        # the attacker-shaped body this path exists for).
        return " ".join(_clean_gateway_text(r.text or "").split())[:limit]
    except Exception:
        return ""


# The gateway's own words are reflected into this agent's audit log and into
# the operator's terminal — and COORDINATOR_BASE is an env-overridable default,
# so the endpoint that produced them is not axiomatically trusted. Two limits
# apply before the string is used anywhere.
#
# MEASURED, not guessed: the longest message any deployed middleware refusal
# emits is 378 characters, so a 600-character cap preserves every legitimate
# message whole and truncates only a body no deployed path can produce.
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


# Route-specific tail for the relation-adjudication endpoints. It is APPENDED to
# the gateway's own refusal rather than replacing it: which token was refused is
# the gateway's to say, what the route is FOR is the client's. Before this, the
# two 403 sites that carried it discarded the gateway's reason entirely.
_EDGE_REVIEW_FORBIDDEN_HINT = ("This token may not review/label relation edges "
                               "(operator-grade route). Use a write-capable agent token.")


def _reply_json(r, *, log_auth: bool = False, forbidden_hint: str | None = None) -> dict:
    """Decode a gateway response ONLY after branching on its status class.

    THE RULE (fact:1503). A non-2xx aiohttp page is plain text — ``"403:
    Read-only token: this route requires a write-capable agent token"`` — and
    ``json.loads`` of ANY such page raises ``JSONDecodeError: Extra data: line
    1 column 4 (char 3)``. Decoding before the status class is branched on
    therefore turns EVERY unenumerated status into a decode exception, which
    the transport handler then reports as "coordinator unreachable — is
    hive_mind_proxy.py running?". A live gateway refusing on authorization was
    read as a dead gateway; three wrong diagnoses followed. The defect is a
    CLASS, not a 403 special case, so the fix is a single rule applied at
    every response site rather than another per-site guard (v0.9.33 patched
    one site; the class shipped again).

    401 keeps ``_auth_error()``'s two sub-branches verbatim (sent vs not
    sent). 403 surfaces the gateway's OWN message, so a read-only role refusal
    says exactly that instead of sending the operator to inspect auth setup.
    Any other >= 400 names the status and quotes the body. A 2xx whose body
    will not parse says the gateway is LIVE and its reply malformed — which is
    a different fault with a different fix from an unreachable one.

    Raises GatewayReplyError on every non-2xx and on an unparseable 2xx;
    returns the decoded payload otherwise.
    """
    # ONE name for the line written and the line reported: the event the catch
    # block is told about IS the event this branch wrote, never a second literal
    # that could drift away from it.
    if r.status_code == 401:
        logged = "auth_failed" if log_auth else None
        if logged:
            _append_log("memory_bridge", 2, logged, _auth_log_hint())
        raise GatewayReplyError(_auth_error(), logged_event=logged)

    # The gateway's OWN words come FIRST, before this client's framing. Readers
    # downstream truncate (postflight A5 slices a search error to 200 chars), and
    # a preamble long enough to push the actual refusal past the cut restores the
    # defect one level up — the operator still cannot see WHY.
    if r.status_code == 403:
        detail = _gateway_message(r) or _body_snippet(r)
        head = f"Gateway refused this request (HTTP 403): {detail}" if detail else \
               "Gateway refused this request (HTTP 403)."
        message = (f"{head} — the gateway ANSWERED and the credential was ACCEPTED, so "
                   f"this is an authorization refusal, not an authentication failure "
                   f"and not a transport fault.")
        if forbidden_hint:
            message += f" {forbidden_hint}"
        raise GatewayReplyError({"status": "error", "message": message})

    if r.status_code >= 400:
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
    """Map a transport failure to a message that names the RIGHT cause.

    A read timeout and a dead gateway are different faults with different fixes,
    and httpx's ReadTimeout stringifies to the empty string — so reporting both as
    "unreachable — is hive_mind_proxy.py running? ()" sent readers to inspect a
    daemon that had answered /health 3 ms earlier (fact:1112). The same shape as
    the v0.8.45 verifiers reporting a credentials error for a missing dependency.

    Structural guard, not a courtesy: a GatewayReplyError means the gateway
    ANSWERED, so it can never be reported as unreachable — even from a call
    site that forgot its own `except GatewayReplyError` clause. This function
    is the last place the defect of fact:1503 could re-enter, so the rule is
    enforced here too rather than relying on eleven call sites staying correct.
    """
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


ROLE_REPORTING_MIN_VERSION = "0.9.52"


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
        # The gateway ANSWERED — `reachable` says so, or `doctor` would send the
        # operator to restart a service that is running and merely refusing.
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
    # `agent`/`role` ride on the AUTHENTICATED /health payload (a server change
    # this PR does not build) — sent here so doctor can surface them once a
    # token is configured; an anonymous/older gateway simply omits both.
    # `role`'s three-way diagnosis is `_role_diagnosis` — see its docstring
    # (T-04, PR #310 review).
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

    # Derive the project tag when the caller supplied none. An explicit value always
    # wins — this fills the gap, it does not override intent. Untagged facts are not
    # merely untidy: the consolidation key falls back for them, so they fragment
    # away from their own project's cluster and never reach a summary.
    if not metadata.get("project"):
        derived = derive_project()
        if not derived:
            # Second chance, and only a deterministic one: when the working
            # directory is not inside any project root but the record cites an
            # ABSOLUTE path, that path is itself evidence of where the record
            # belongs — walk up from it exactly as the cwd walk does.
            #
            # Its honest value is forward-looking: on this corpus it rescues 0 of
            # 127 untagged records, because none of their refs are absolute. It is
            # correct for records written from now on, and that is the whole claim.
            #
            # NO relative-prefix inference. A ref like "scripts/foo.py" names no
            # filesystem location, so guessing a project from its first segment is
            # the entity vote wearing a different hat — a plausible wrong project,
            # which is worse than none.
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
        # ONE refused save is ONE audit line. A 401 was already written as
        # `auth_failed` inside _reply_json, which is exactly what this path
        # logged before the decode was centralised; adding `save_failed` behind
        # it would double-count every rejected credential in the audit trail.
        # Every OTHER class — 403, other 4xx, 5xx, a malformed 2xx — logs
        # `save_failed` here, and that IS new signal: those replies used to be
        # logged as `coordinator_down`, which was a lie about a gateway that
        # had answered.
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
        # What "unreachable by synthesis" MEANS depends on the record type, so the
        # warning has to follow it. A fact mints its own topics, so an empty
        # `entities` is the defect. A judgement mints none by design — it inherits
        # from the facts it cites — so the equivalent defect is empty grounding,
        # and warning `no_entities` there would fire on every decision saved
        # exactly as instructed, training the operator to ignore the log.
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


async def fetch_review_edges(family: str = "entity_relation", limit: int = 20) -> dict:
    """Fetch the stratified unlabeled relation-adjudication sample for operator
    review (POST /memory/relations/review) — the calibration elicitation flow.
    Everything goes through the gateway; the client never touches the ledger."""
    try:
        async with _async_client(30.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/relations/review",
                json={"family": family, "limit": limit},
                headers=_request_headers(),
            )
            result = _reply_json(r, forbidden_hint=_EDGE_REVIEW_FORBIDDEN_HINT)
    except GatewayReplyError as exc:
        return exc.payload
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc))
    return result


async def apply_edge_labels(labels: dict, promote: list | None = None) -> dict:
    """Apply operator labels {row_id: 'correct'|'incorrect'} (+ optional
    promotions to operator-asserted) via POST /memory/relations/label."""
    payload: dict = {"labels": labels}
    if promote:
        payload["promote"] = promote
    try:
        async with _async_client(60.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/relations/label",
                json=payload,
                headers=_request_headers(),
            )
            result = _reply_json(r, forbidden_hint=_EDGE_REVIEW_FORBIDDEN_HINT)
    except GatewayReplyError as exc:
        return exc.payload
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc))
    return result


def _parse_edge_labels(spec: str) -> dict:
    """Parse the 'id=correct,id=incorrect' label grammar into {id_str: label}.
    Pure — validation (int ids, label vocabulary) happens before the request."""
    labels: dict = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        rid, _, lab = part.partition("=")
        labels[rid.strip()] = lab.strip().lower()
    return labels


def format_calibration_line(cal: dict) -> str:
    """One line telling the operator what their labels have (not yet) unlocked."""
    fam = cal.get("family", "?")
    if cal.get("calibrated"):
        return (f"family {fam}: {cal.get('labels')} labels — calibrated, "
                f"threshold {cal.get('threshold')}")
    return (f"family {fam}: {cal.get('labels', 0)}/{cal.get('min_labels', 20)} labels "
            f"— UNCALIBRATED, machine edges not consumed by synthesis")


def format_review_edges(payload: dict) -> str:
    """Render the review sample human-readably: one block per ledger row (id,
    verdict, confidence, src -rel-> tgt, method/support, rationale, and content
    snippets for evidential rows) + the family calibration line."""
    if payload.get("status") != "success":
        return json.dumps(payload, indent=2)
    rows = payload.get("rows") or []
    lines = []
    if not rows:
        lines.append(f"No unlabeled {payload.get('family')} adjudications — nothing to review.")
    else:
        lines.append(f"Unlabeled {payload.get('family')} adjudications "
                     '(label with: label-edges "id=correct,id=incorrect" [--promote id,id]):')
        for r in rows:
            conf = (f"{r['confidence']:.2f}"
                    if isinstance(r.get("confidence"), (int, float)) else "  — ")
            if r.get("src_name") is not None:
                src, tgt = repr(r.get("src_name")), repr(r.get("tgt_name"))
            else:
                src, tgt = f"record {r.get('src_pg_id')}", f"record {r.get('tgt_pg_id')}"
            lines.append(f"  id={r.get('id'):<5} [{r.get('verdict', '?'):<6}] conf={conf} "
                         f"{src} -{r.get('rel_type')}-> {tgt}  "
                         f"({r.get('method')}, support={r.get('support')})")
            if r.get("rationale"):
                lines.append(f"           rationale: {str(r['rationale'])[:160]}")
            if r.get("src_snippet"):
                lines.append(f"           src: {r['src_snippet']}")
            if r.get("tgt_snippet"):
                lines.append(f"           tgt: {r['tgt_snippet']}")
    cal = payload.get("calibration") or {}
    if cal:
        lines.append(format_calibration_line(cal))
    return "\n".join(lines)


def format_label_outcomes(payload: dict) -> str:
    """Per-row outcome lines for label-edges (labeled / edge_deleted / promoted)."""
    if payload.get("status") != "success":
        return json.dumps(payload, indent=2)
    lines = []
    for rid, out in sorted((payload.get("outcomes") or {}).items(),
                           key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        parts = []
        if out.get("labeled"):
            parts.append(f"labeled {out['labeled']}")
        if "edge_deleted" in out:
            parts.append(f"machine edge(s) deleted: {out['edge_deleted']}")
        if out.get("promoted"):
            parts.append(f"PROMOTED to operator-asserted "
                         f"(edges updated: {out.get('edges_updated', '?')})")
        if out.get("edge_error"):
            parts.append(f"WARNING: {out['edge_error']}")
        if out.get("error"):
            parts.append(f"ERROR: {out['error']}")
        lines.append(f"  id {rid}: " + ", ".join(parts))
    return "\n".join(lines) if lines else "(no outcomes)"


async def search_and_rerank(query: str, limit: int = 5, project: str = None,
                             domains: list = None, since: str = None) -> list | dict:
    # Sized from the gateway's own published cost, never from a constant — the
    # reranker dominates this call and its cost tracks the candidate payload.
    ceiling = search_ceiling(await _gateway_capability(), await _gateway_capacity())
    body = {"query": query, "limit": limit, "agent_id": AGENT_ID}
    # Additive only — an unfiltered call sends exactly what it always sent.
    # A named place/time is a FILTER, not query text.
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
            result = _reply_json(r, log_auth=True)
    except GatewayReplyError as exc:
        return exc.payload
    except Exception as exc:
        return await _warn_on_skew(_coordinator_unavailable(exc, ceiling))

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
    # Same flag pattern as save's --domain: repeatable, never comma-split,
    # OR semantics at the gateway.
    p.add_argument("--project", default=None, metavar="NAME",
                   help="restrict to records BELONGING to this project — a "
                        "named place is a FILTER, not query text. An "
                        "unregistered name is not refused, it simply "
                        "matches nothing.")
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
    # Repeatable, never comma-split. A separator that can occur inside a
    # value is not a delimiter — the lesson --alternatives taught, applied
    # before this surface can repeat it.
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
        # TypeError, not just ValueError: a gateway that ever sends a non-string
        # here (an epoch number, a nested object) must not take the whole status
        # report down with it. This renderer is the operator's health dashboard —
        # degrading one field beats denying the page. (Security review REV-04.)
        return str(ts)
    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    delta = int((now - when).total_seconds())
    if delta < 0:
        # Clock skew between gateway and client, or a backward time jump on the
        # gateway after stamping. "-15s ago" reads as a bug in the framework;
        # naming the cause is more useful than a negative number.
        return f"{ts} (clock skew: stamp is {abs(delta)}s in the future)"
    return f"{delta}s ago"


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
        # Enrichment health: a record at the attempt cap is still counted as
        # "REM pending" but has been dropped from REM's queue — without this
        # line a dead-lettered backlog is indistinguishable from a waiting one.
        _dead = nj.get("rem_dead_lettered", 0) or 0
        _failing = nj.get("rem_failing", 0) or 0
        if _dead or _failing:
            _warn = "  ⚠ operator reset needed" if _dead else ""
            lines.append(f"  REM enrichment: {_failing} retrying | "
                         f"{_dead} DEAD-LETTERED at {nj.get('rem_max_attempts','?')} "
                         f"attempts{_warn}")
        # Fairness gauge (decision 890, STEP 3) — ships dormant (both read 0
        # until the solo backlog is large enough to re-exercise the
        # batch-vs-solo yield path); only printed once either climbs above 0,
        # matching the enrichment-health line's pattern above.
        _passed_over = nj.get("rem_passed_over_total", 0) or 0
        _starved = nj.get("rem_starved_pending", 0) or 0
        if _passed_over or _starved:
            lines.append(f"  REM fairness: {_passed_over} passed-over event(s) | "
                         f"{_starved} record(s) at/above starvation threshold")
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
    # Graph integrity — nodes REM retired because their label contradicted the
    # record their id names. A WRITE-PATH defect, not a backlog: it does not
    # drain on its own, so anything above 0 names a writer that needs fixing.
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
        # Name the cycle type behind the headline age and behind the stall.
        # A bare "STALLED, last success 456107s ago" reads as "consolidation is
        # dead" even when a sibling type folded minutes ago — it was one type's
        # number wearing the whole system's label.
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
                parts.append(f"err {c['last_error'].get('class','?')}")
            if c.get("eligible_clusters") is not None:
                cov = f"eligible {c['eligible_clusters']}"
                if c.get("eligible_oldest_age_seconds") is not None:
                    cov += f" (oldest {c['eligible_oldest_age_seconds']}s)"
                parts.append(cov)
            # Per-type cost + throughput: what this cycle type actually costs a
            # slot, and what it returned for it. Only shown once the type has
            # run in the window — a bare "0 folds" from no runs would read as
            # failure rather than absence.
            if c.get("runs_24h"):
                thru = f"{c['runs_24h']} runs/24h"
                if c.get("cycle_seconds_avg") is not None:
                    thru += f" avg {c['cycle_seconds_avg']}s"
                thru += f", folds {c.get('folds_succeeded_24h', 0)}/{c.get('folds_attempted_24h', 0)}"
                parts.append(thru)
            # Non-runs, shown beside the run count so the rate cannot be read as
            # the whole story: deferred = due but the slot was busy, idle = the
            # gate ran and found nothing. Omitted when both are zero rather than
            # printing noise on a healthy cycle.
            if c.get("deferred_24h") or c.get("idle_24h"):
                parts.append(
                    f"non-runs {c.get('deferred_24h', 0)} deferred"
                    f"/{c.get('idle_24h', 0)} idle")
            lines.append(f"    {ct}: " + ", ".join(parts))
    elif "error" in cn:
        lines.append(f"  consolidation: ERROR {cn['error']}")
    # Credential custody (PR A3). The gateway has attached this section since
    # v0.9.4 and nothing rendered it — telemetry is the only non-monitor
    # surface a client sees, so an operator running `status` could not see a
    # credential fault or a lost audit line without reading raw --json.
    # Shown only when an attention signal is non-zero, matching the enrichment
    # and fairness lines above: daemon token mints are routine (one per daemon
    # per boot) and would be noise on every healthy run. Each count is printed
    # with the age of its OWN last event — the counters reset with the gateway
    # process, so a reader diffing polls instead would read a restart as "no
    # failures ever".
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
            # Q-1 (PR A5 fix round): S-04's allowlist gate was otherwise
            # invisible from the client surface — an operator watching
            # `status` had no way to see it firing (Group-3 obligation:
            # every refusal visible working AND failing).
            lines.append(f"  credentials: {_crd} credentialed-route denial(s) "
                         f"| last {_age_phrase(cr.get('credentialed_route_denied_last_ts'))} "
                         f"(since gateway start)")
        if _drop:
            lines.append(f"  credential audit: {_drop} LINE(S) DROPPED ⚠ "
                         f"| last {_age_phrase(cr.get('audit_log_dropped_last_ts'))} "
                         f"— the audit trail is incomplete")
    elif "error" in cr:
        lines.append(f"  credentials: ERROR {cr['error']}")
    # Per-backend credential/transient faults (PR A3). `credential` is the
    # fix-the-key signal (401/403/quota); `transient` retries on its own, so it
    # is reported but not flagged.
    lf = t.get("llm_faults", {})
    if isinstance(lf, dict):
        for backend, f in sorted(lf.items()):
            if not isinstance(f, dict):
                continue
            parts = []
            # `or {}` is not enough to make .get() safe — a non-dict truthy value
            # (a bare string from a drifted or hostile gateway) passes straight
            # through it and then raises AttributeError, taking the whole status
            # report down. Type-check instead. (Code-quality review C1.)
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
        "entities": [e.strip() for e in entities.split(",") if e.strip()],
        "decision": decision,
    }
    # grounded_in: pg_ids of the facts this decision rests on — materialised at
    # first write as typed (:Decision)-[:ROLE]->(:Fact|:Decision) edges. Always
    # include at least the conversation fact (grounding floor, decision 552).
    # Per-fact ROLE is optional: "534:considered,573,575:rejected" — a bare pg_id
    # lets fact_kind pick the default role (decision 582). Roles: based_on,
    # considered, rejected, under_conditions, informed_by.
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
    # elicited: the spine fields were asked of the operator (decision 559). An
    # elicited null is a deliberate choice; coverage telemetry counts the ask.
    if elicited:
        metadata["elicited"] = True
    # new_project: the OPERATOR has confirmed this project is new, so the save
    # registers it instead of being refused. It is deliberately not a default
    # and deliberately not inferred: from v0.8.44 a decision's project is
    # checked against the registry, which only means anything if declaring a new
    # one is a deliberate act. An agent that sets this to clear its own
    # rejection has converted a typo into a permanent project.
    if new_project:
        metadata["new_project"] = True
    # confirm_distinct_from: the registered projects this new one is deliberately
    # NOT. The gateway refuses a new name that is confusable with an existing one
    # until they are named, because naming the neighbour is something an agent
    # cannot do without having looked at it — which is what puts the choice in
    # front of the operator instead of inside the agent.
    # domains: the SECTIONS of the project this decision belongs to. A decision
    # ASSERTS these, exactly as it asserts its project — it does not inherit them
    # from its evidence, because a decision reaches further than the fact that
    # prompted it. They go inside the decision blob, beside `project`, because
    # that is the half the gateway resolves a judgement's axes from; putting them
    # at the top level would leave a decision's project and its domain coming
    # from different halves of one record.
    #
    # ⚠ Threaded through EXPLICITLY. The flag existed for one release while this
    # line did not, so `--domain` parsed cleanly, was dropped on the floor, and
    # the decision silently fell back to inheriting its evidence's sections. It
    # read as correct because the inherited answer happened to match what was
    # asked for — a field the CLI accepts and the record never carries is the
    # capture defect that hides longest.
    if domains:
        decision["domains"] = list(domains)
    if new_domain:
        metadata["new_domain"] = True
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
    entities: str = "",
    source_ref: str = "",
    elicited: bool = False,
) -> dict:
    """Build the JSON payload for POST /memory/retrospective (API v2 —
    retro-as-record: the gateway mints a full searchable record and returns
    its own pg_id).

    grounded_in uses the same "pgid[:role],pgid" grammar as save_decision —
    the facts that MEASURED this outcome (test-grounded retrospectives,
    decision 542). Pure function — no I/O, no side effects.
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
    if entities:
        payload["entities"] = [e.strip() for e in entities.split(",") if e.strip()]
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
    entities: str = "",
    source_ref: str = "",
    elicited: bool = False,
) -> dict:
    # Client-side enum check — a friendlier error than the gateway's 400, and
    # the doc surface for what a rating IS.
    if rating.strip().lower() not in RETRO_RATINGS:
        return {"status": "error",
                "message": (f"rating must be one of {list(RETRO_RATINGS)} — outcome "
                            "states, not valence; put the nuance in --notes")}
    payload = build_retrospective_payload(pg_id, rating, notes, date, source,
                                          grounded_in, entities, source_ref, elicited)
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

    # Retrospective payload lives on the RECORD node since the retro-as-record
    # change; pre-conversion installs still carry it as edge properties. Both
    # templates therefore read BOTH shapes: node fields when the target is a
    # Retrospective, edge fields otherwise (same tolerance the consolidation
    # daemon uses).
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
            "error": "Usage: python memory_bridge.py [--version|doctor|status|graph|query|search|save|save_decision|save_retrospective|review-edges|label-edges] ..."
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
        # Accepts a bare id or a QUALIFIED reference (`fact:816`, `summary:87`).
        # A record id is unique only within its table — technical_docs and
        # community_summaries run independent sequences — so a bare id lifted
        # off a summary search result would resolve against the wrong table and
        # return a confident, unrelated record. Qualify it and it cannot.
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
        results = await search_and_rerank(query, sargs.limit, project=sargs.project,
                                           domains=sargs.domains, since=sargs.since)
        warning = _unranked_warning(results)
        if warning:
            print(warning, file=sys.stderr)
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
        # Not required: defaults to the project folder name (see derive_project).
        # Still mandatory at the gateway, so a save from outside any project root
        # fails loudly rather than recording a decision with no project.
        p.add_argument("--project",     default="",     help="Project context (default: project folder name)")
        p.add_argument("--domain", action="append", default=None, metavar="NAME",
                       help="a registered SECTION of this project; REPEAT for "
                            "several. A decision asserts its OWN sections rather "
                            "than inheriting them, because a decision reaches "
                            "further than the fact that prompted it — a fact about "
                            "how agents write to the graph is infrastructure, while "
                            "the decision on who may write is about access. Omit it "
                            "and the decision takes its grounding facts' sections as "
                            "a default, which any explicit value replaces.")
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
        p.add_argument("--entities",    default="",
                       help="DEPRECATED — kept for older callers and IGNORED by the "
                            "graph. A decision mints no entity; it inherits the topics "
                            "of the facts in --grounded-in. Use that instead.")
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
            entities=args.entities,
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
        p.add_argument("--entities",    default="",
                       help="DEPRECATED — kept for older callers and IGNORED by the "
                            "graph. A retrospective inherits the topics of the facts "
                            "in --grounded-in, which is required.")
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
                entities=args.entities,
                source_ref=args.source_ref,
                elicited=args.elicited,
            ),
            indent=2,
        ))
    elif action == "review-edges":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py review-edges",
            description="Fetch a stratified sample of unlabeled machine relation "
                        "verdicts for operator labeling (calibration oracle). "
                        "Label the FIRST evidence-sweep batch immediately — before "
                        "any confidence threshold acts.",
        )
        p.add_argument("family", nargs="?", default="entity_relation",
                       choices=list(RELATION_FAMILIES),
                       help="calibration family (default: entity_relation)")
        p.add_argument("limit", nargs="?", type=int, default=20,
                       help="rows to review (default 20, gateway caps at 100)")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        sargs = p.parse_args(sys.argv[2:])
        payload = await fetch_review_edges(sargs.family, sargs.limit)
        if sargs.json:
            print(json.dumps(payload, indent=2))
        else:
            print(format_review_edges(payload))
    elif action == "label-edges":
        p = argparse.ArgumentParser(
            prog="memory_bridge.py label-edges",
            description="Apply operator labels to relation-adjudication rows. "
                        "'correct' = the relation AS TYPED AND DIRECTED is true; "
                        "'incorrect' on an accepted row deletes the machine edge "
                        "(operator-asserted edges are never deleted; the ledger row "
                        "stays). --promote upgrades a correct edge to "
                        "asserted_by=operator — it bypasses thresholds permanently.",
        )
        p.add_argument("labels", help='label spec: "12=correct,13=incorrect"')
        p.add_argument("--promote", default="",
                       help="comma-separated row ids to promote to operator-asserted "
                            "(each must be labeled correct, now or already)")
        sargs = p.parse_args(sys.argv[2:])
        labels = _parse_edge_labels(sargs.labels)
        bad = {rid: lab for rid, lab in labels.items()
               if lab not in ("correct", "incorrect") or not rid.isdigit()}
        if not labels or bad:
            print(json.dumps({"status": "error",
                              "message": f"invalid label spec near {bad or sargs.labels!r} — "
                                         'use "id=correct,id=incorrect" (integer ids)'}))
            sys.exit(1)
        promote = [int(x) for x in sargs.promote.split(",") if x.strip().isdigit()]
        result = await apply_edge_labels(labels, promote)
        print(format_label_outcomes(result))
    else:
        print(json.dumps({"error": f"Unknown action: {action}. Use graph|query|search|save|save_decision|save_retrospective|review-edges|label-edges"}))


if __name__ == "__main__":
    asyncio.run(main())
