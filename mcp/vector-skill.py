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
#
# This process is a CLIENT. The only secrets it may hold are its OWN AGENT_TOKEN
# and the gateway URL — never the framework/server env, which carries
# PG_PASSWORD, NEO4J_PASSWORD and the entire AGENT_TOKENS registry. A client that
# loaded that file would inherit every other agent's credentials, and the point
# of per-agent tokens is that each origin is separately identifiable and
# separately revocable.
#
# The two used to collide by default: this script lived at the repo root, where
# a pre-0.6 install keeps the server env. It now lives in mcp/, so "the .env
# beside me" is mcp/.env — a client-only location — and loading the file beside
# this script is what makes a per-install copy work, the same shape as each CLI
# agent owning its own skill .env. The guard stays: a server env copied here is
# still recognisably the server's, gets REFUSED, and the refusal says why. Any
# MCP host can install its own copy in its own directory with its own token;
# nothing here assumes LM Studio.
#
# VECTOR_SKILL_ENV overrides the path outright, for an install that keeps its
# client env somewhere else. An MCP host that injects AGENT_TOKEN through its own
# config block (mcp.json's `env`) needs no file at all — that path is unaffected
# BY CONSTRUCTION: the host writes it into this process's os.environ before this
# module is ever imported, which is outside this loader's reach either way.
_SERVER_ONLY_KEYS = frozenset({"AGENT_TOKENS", "PG_PASSWORD", "NEO4J_PASSWORD"})

# D.4 (SEC round, ADV1-15): key = the text before the first "=", with an
# optional leading "export " stripped — so `export AGENT_TOKENS=...` (a
# legitimate shell-sourceable form some deployers use) is recognised by its
# KEY, not lost the way a naive re-parse could. The previous implementation
# matched "AGENT_TOKENS=" etc. as a bare SUBSTRING of the whole line, which
# happened to also catch the `export` form (the substring is still present
# after the word "export ") but for the wrong reason — it would just as
# readily match the same text appearing inside an unrelated VALUE (a comment
# quoting the line, a value containing "AGENT_TOKENS=" as literal text).
# Matching the parsed KEY instead is the correct check either way; handling
# `export` explicitly is what keeps that one legitimate form recognised
# under the corrected approach.
#
# Fix round F5 (SEC1 HIGH-3 + MED-5): re.IGNORECASE added. The "export "
# in the pattern above was case-SENSITIVE, so "EXPORT AGENT_TOKENS=..."
# matched NEITHER the export branch NOR the bare-key branch (the whole
# "EXPORT" token, followed by a space then more non-"=" text, satisfies
# neither `(?:export\s+)?` case-sensitively nor `[^=\s]+\s*=` at position
# 0) — probed: this regex returned no match at all for an "EXPORT "-
# prefixed server-only key, so _looks_like_server_env() never detected the
# framework .env and this client loaded it.
_ENV_KEY_RE = re.compile(r"^(?:export\s+)?([^=\s]+)\s*=", re.IGNORECASE)


_EXPORT_PREFIX_RE = re.compile(r"^export\s+", re.IGNORECASE)


def _strip_export_prefix(key: str) -> str:
    """Fix round F5: strip an optional leading shell `export ` keyword
    (case-insensitive) from a raw .env line's key text, at parse time, in
    the MANUAL fallback parser (_load_env_manually) only — the dotenv_
    values() path already strips a lowercase "export " natively and
    silently drops an uppercase "EXPORT " line entirely (neither reaches
    this function). Mirrors secure_env._strip_export_prefix() exactly."""
    s = key
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    s = _EXPORT_PREFIX_RE.sub("", s, count=1)
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    return s


def _looks_like_server_env(path: str) -> bool:
    """True when this .env is the FRAMEWORK's, not a client's. Best-effort: an
    unreadable file is not treated as a server env, since the only cost of trying
    to load it is dotenv's own failure."""
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


# AGENT_TOKEN is read into a private variable and NEVER exported into this
# process's own os.environ (ported from memory_bridge.py:86-97, S-18 /
# MCPW-R2-C5 — origin fact:1816): this door used to load its whole .env —
# AGENT_TOKEN included, and after the MCP-W mint a write-capable one — into
# os.environ, the same "secret sitting in a long-lived process's own
# environment" class the CLI door already closed (visible via this process's
# own /proc/<pid>/environ, same UID, and to anything that later snapshots the
# environment). An operator's own real `export AGENT_TOKEN=...` still wins —
# checked FIRST on every call, before any file value is considered (see
# _auth_headers below). _AGENT_TOKEN_FROM_FILE is populated once below, from
# the file only, and is the seam tests use to neutralise a real on-disk .env
# during isolated runs
# (monkeypatch.setattr(vs, "_AGENT_TOKEN_FROM_FILE", "")).
_AGENT_TOKEN_FROM_FILE = ""

# Duplicated (not imported) from memory_bridge.py:129-148 — both clients ship
# alone, so neither may depend on the other or on a server-only module (the
# same reason memory_bridge doesn't import secure_env). A contract test
# (test_client_secret_mirror_parity.py) pins both copies against secure_env.
# py's own KNOWN_SECRET_NAMES / _SECRET_SUFFIXES so a future drift fails
# loudly instead of needing a fresh probe to find (the R6 lesson, reopened
# here if this mirror is ever edited alone).
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


_ENV_PATH = os.environ.get("VECTOR_SKILL_ENV", "").strip() or os.path.join(
    os.path.dirname(os.path.realpath(__file__)), ".env")


def _load_env_manually(path: str) -> None:
    """Fallback parser for when python-dotenv is absent. An env loader must
    NEVER silently no-op because its parser dependency is missing — that class
    once made two verifiers report a CREDENTIALS error for a missing
    DEPENDENCY. Same structure as memory_bridge.py's manual fallback: strip,
    skip comments/no-`=` lines, divert AGENT_TOKEN to _AGENT_TOKEN_FROM_FILE
    (never exported), skip any secret-shaped key (never exported either), and
    first-definition-wins for everything else — real env vars still win
    because they were already set before this ever runs."""
    global _AGENT_TOKEN_FROM_FILE
    try:
        # utf-8-sig: a file saved as UTF-8-with-BOM has its first three bytes
        # decoded to a leading U+FEFF on the FIRST key otherwise — SEC round
        # HIGH-2 (2026-09-01, gemini). Belt and braces with the per-key
        # lstrip below, which also catches a BOM character that ended up
        # embedded mid-file some other way (e.g. a bad concatenation).
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                # F5 (SEC1 HIGH-3/MED-5): strip an optional leading
                # "export "/"EXPORT " prefix before classification —
                # without this, the stored key for an "export
                # AGENT_TOKENS=..." line was the literal "export
                # AGENT_TOKENS", matching neither the AGENT_TOKEN diversion
                # nor _is_client_secret_key's exact-name list, exporting
                # the registry straight into os.environ.
                key = _strip_export_prefix(key).strip()
                val = val.strip()
                if not key:
                    continue
                # SEC round HIGH-1 + HIGH-2 (2026-09-01, gemini), H-1
                # follow-up fix (2026-09-01 — regression in the first
                # pass): normalize for FILTERING/DIVERSION ONLY, at this
                # call site — never inside _is_client_secret_key, which
                # stays a BYTE-IDENTICAL mirror of memory_bridge.py's (the
                # parity test pins it). str.strip() does NOT remove U+FEFF
                # (it is not whitespace), so a stray BOM or a lowercase
                # spelling used to sail past both the AGENT_TOKEN divert
                # and the secret-suffix/name check below and land straight
                # in os.environ. `key` itself — never `key_norm` — is
                # still what gets exported when the key isn't filtered:
                # this only changes what counts as secret/AGENT_TOKEN,
                # never the exported name's casing.
                #
                # H-1: key_norm is upper-cased HERE, once, and BOTH the
                # AGENT_TOKEN comparison and the predicate call use this
                # single already-uppercased value. The first pass instead
                # compared the token key case-SENSITIVELY
                # (`key_norm == "AGENT_TOKEN"` with key_norm only
                # lstripped+stripped) while calling the predicate with
                # `key_norm.upper()` — so a lowercase `agent_token=` line
                # folded onto "AGENT_TOKEN" for the predicate's OWN
                # deliberate early-return exemption (`_is_client_secret_
                # key`: `if name == "AGENT_TOKEN": return False`, which
                # exists BECAUSE the token has its own diversion path)
                # while never matching the still-case-sensitive diversion
                # check above it — caught by NEITHER, exported by the
                # setdefault below. This deliberately now catches every
                # case-variant spelling of the token — a SAFER-direction
                # divergence from memory_bridge.py's exact-case diversion,
                # recorded there as a SEC-round twins item, not fixed here
                # (memory_bridge.py, the CLI door, is not this file's to
                # touch).
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
        pass  # absent file = rely on externally-set env vars, same as dotenv


if os.path.isfile(_ENV_PATH) and _looks_like_server_env(_ENV_PATH):
    sys.stderr.write(
        f"[rag-orchestrator] refusing to load {_ENV_PATH}: it holds "
        "server-only keys (AGENT_TOKENS / PG_PASSWORD / NEO4J_PASSWORD), so "
        "it is the framework env, not this client's. Give this MCP client its "
        "own directory with its own .env containing only AGENT_TOKEN (and "
        "optionally COORDINATOR_URL / AGENT_ID), set VECTOR_SKILL_ENV to that "
        "file, or inject AGENT_TOKEN via the MCP host's own env block.\n")
else:
    try:
        from dotenv import dotenv_values  # parses without touching os.environ
        if os.path.isfile(_ENV_PATH):
            for _k, _v in dotenv_values(_ENV_PATH).items():
                if _v is None or not _k:
                    continue
                # SEC round HIGH-1 + HIGH-2 (2026-09-01, gemini), H-1
                # follow-up fix — same rationale AND same shape as
                # _load_env_manually above: _k_norm is uppercased ONCE and
                # used for BOTH the AGENT_TOKEN comparison and the
                # predicate call, so a lowercase `agent_token=` cannot slip
                # through the case-sensitive-vs-normalized mismatch that
                # made the first pass's two separate `.upper()` calls a
                # regression (see the detailed note above). `_k` — never
                # `_k_norm` — is still what gets exported.
                _k_norm = _client_key_norm(_k)
                if _k_norm == "AGENT_TOKEN":
                    if not _AGENT_TOKEN_FROM_FILE:
                        _AGENT_TOKEN_FROM_FILE = _v.strip()
                    continue
                if _is_client_secret_key(_k_norm):
                    continue
                os.environ.setdefault(_k, _v)
    except ImportError:
        # python-dotenv not installed — manually parse the client .env so
        # config/token are found when running bare `python` or
        # `uv run --with httpx`.
        _load_env_manually(_ENV_PATH)

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
# v4 (project registry): a fact save without a REGISTERED metadata.project is
# rejected 400 carrying error=project_required|project_unknown plus near-match
# proposals. BREAKING for any client that saved untagged facts. The second
# submission is accepted in three forms: a proposal, new_project=true, or the
# reserved sentinel general_discussion.
API_VERSION = 4
VERSION = "0.9.86"
CLIENT_VERSION_HEADER = "X-SM-Api-Version"
# This client's own FRAMEWORK VERSION, distinct from the wire API_VERSION: two
# clients can speak api_version 4 while one of them is forty releases behind on
# behaviour. The gateway counts it as `clients.versions_seen` (0.9.74). Group 1
# parity — memory_bridge.py sends the same header under the same name.
CLIENT_BUILD_HEADER = "X-Shared-Memory-Client"

# Constants that MUST mirror the gateway's (a thin client never imports server
# modules, so they are restated here and kept in step by review).
# ontology.RETRO_RATINGS — outcome STATES, not valence:
RETRO_RATINGS = ("validated", "mixed", "refined", "pending", "reversed")
# Record types that may qualify a reference. A record id is unique only WITHIN
# its table — technical_docs and community_summaries run independent sequences —
# so a bare integer lifted off a summary result resolves against the wrong table
# and returns a confident, unrelated record (decision 822).
RECORD_TYPES = ("fact", "decision", "retrospective", "summary", "insight")

CALL_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# ── Search timeout sizing ────────────────────────────────────────────────────
# Mirrors memory_bridge.search_ceiling() exactly — a thin client never imports
# server modules, and the two front doors never import each other, so the rule is
# restated here and held in step by a parity test. A search costs what the
# RERANKER costs, which tracks the candidate payload rather than `limit`. This
# door shipped a constant 60 s against a gateway that projected 127 s for a full
# payload and permits far more; the CLI door shipped 30 s and was already failing
# intermittently, blaming a gateway that was up (fact:1112).
HEALTH_PROBE_TIMEOUT_S    = float(os.environ.get("HEALTH_PROBE_TIMEOUT_S", "3"))
SEARCH_TIMEOUT_S          = float(os.environ.get("SEARCH_TIMEOUT_S", "0") or 0)
SEARCH_TIMEOUT_FLOOR_S    = float(os.environ.get("SEARCH_TIMEOUT_FLOOR_S", "30"))
SEARCH_TIMEOUT_MAX_S      = float(os.environ.get("SEARCH_TIMEOUT_MAX_S", "300"))
SEARCH_TIMEOUT_FALLBACK_S = float(os.environ.get("SEARCH_TIMEOUT_FALLBACK_S", "120"))
SEARCH_SAFETY_FACTOR      = float(os.environ.get("SEARCH_SAFETY_FACTOR", "1.5"))
SEARCH_OVERHEAD_S         = float(os.environ.get("SEARCH_OVERHEAD_S", "15"))


def search_ceiling(capability: dict | None, capacity: dict | None = None) -> float:
    """Client-side search timeout in seconds, derived from the gateway's own
    published backend sizing (``backend_capability`` on GET /health), and —
    when the gateway has one — its own measured worst case (``capacity`` on
    GET /health).

    Pure → unit-testable with no gateway present. MUST stay behaviourally
    identical to ``memory_bridge.search_ceiling``; a parity test compares the two
    across the shipped defaults, the fallback, and both clamps.

    fact:1560 (grounded on decision:1114): a MIXED capability block — one
    backend probes fine while the other reports ``status: "failing"`` (or
    ``projection_stale: true``) with no positive projection of its own — floors
    the derivation at ``SEARCH_TIMEOUT_FALLBACK_S``, never
    ``SEARCH_TIMEOUT_FLOOR_S``. The known backend's number is only a LOWER
    bound on the true cost; a failing backend's true cost is unknown, not zero.

    R2-N3 (PR-A delta review): a backend block that is ABSENT entirely, an
    empty ``{}``, or not a dict at all (malformed) is the SAME ignorance as an
    explicit ``status: "failing"`` — the server mirror gets the identical rule.

    This is narrower than "every backend must report a positive projection": a
    block that IS a well-formed, non-empty dict — carrying a plain
    ``status: "error"``, or ``"ok"`` with no projection at all — does NOT trip
    the fallback floor by itself; only the three states above do (T-05/R2-N3,
    PR #310 review). Our own gateway's probe never actually produces that
    narrower gap today, but this function also has to make sense of an older,
    third-party or future gateway's /health, so that shape is exercised and
    pinned as documented behaviour rather than assumed unreachable.

    When ``capacity`` carries the gateway's own measured numbers
    (``capacity["derived"]``), three of its fields are folded in too — never
    smaller, this only ever RAISES the ceiling:

      * ``client_ceiling_s`` — the server's own already-derived ceiling;
        compared as-is.
      * ``s_mean_s`` — the theoretical full-payload projection the GATEWAY
        itself computed, always present once the gateway has probed at all
        (T-02, PR #310 review: the field that would have sized fact:1560's own
        measured case correctly).
      * ``s_max_measured_s`` — a PROJECTION too, the same kind of number as
        ``projected_full_payload_s``, but over the coordinator's own observed
        MAXIMUM rerank payload; ``None`` until real search traffic has been
        served this process's lifetime, unlike ``s_mean_s``.

    ``s_mean_s``/``s_max_measured_s`` are not yet safety-scaled for THIS
    client, so each gets the same ``SEARCH_SAFETY_FACTOR``/
    ``SEARCH_OVERHEAD_S`` treatment as the theoretical projection before
    comparison; ``client_ceiling_s`` is already derived and is compared as-is.
    """
    if SEARCH_TIMEOUT_S > 0:
        return SEARCH_TIMEOUT_S

    projected, probed, unknown = 0.0, False, False
    for backend in ("reranker", "embedder"):
        block = (capability or {}).get(backend)
        if not isinstance(block, dict) or not block:
            # R2-N3 (PR-A delta review): an ABSENT, empty {} or non-dict
            # block is the SAME ignorance as an explicit `status: "failing"`
            # — this backend's real cost is unknown, not zero, exactly as if
            # it had said so. (The server mirror gets the identical rule.)
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
        # T-02 (fact:1560): the gateway's own full-payload projection, always
        # present once ANY probe has run.
        s_mean_s = capacity_derived.get("s_mean_s")
        if isinstance(s_mean_s, (int, float)) and s_mean_s > 0:
            derived = max(derived, s_mean_s * SEARCH_SAFETY_FACTOR + SEARCH_OVERHEAD_S)

    return min(derived, SEARCH_TIMEOUT_MAX_S)


_CAPABILITY_CACHE: dict | None = None
_CAPACITY_CACHE: dict | None = None
# CQ-03 (PR #310 review): guards against two searches starting in the same
# instant both seeing an empty cache and both firing a /health request.
_HEALTH_FETCH_LOCK = asyncio.Lock()


async def _fetch_health_blocks() -> None:
    """GET /health once per process and cache both ``backend_capability`` and
    ``capacity`` from it — ONE request feeds both caches, never two. Never
    raises: sizing the search must never be the thing that fails it.

    Sends this client's own auth headers (S-10, PR A5): ``backend_capability``
    moved behind auth along with the rest of /health's operational detail, so
    an unauthenticated call here would always land on the anonymous-slim
    shape and silently fall back to the constant ceiling on every
    authenticated install."""
    global _CAPABILITY_CACHE, _CAPACITY_CACHE
    if _CAPABILITY_CACHE is not None:
        return   # already attempted this process — do not retry
    async with _HEALTH_FETCH_LOCK:
        if _CAPABILITY_CACHE is not None:
            return   # a concurrent waiter already filled it while we queued
        try:
            async with httpx.AsyncClient(timeout=HEALTH_PROBE_TIMEOUT_S) as client:
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
    """Headers for every coordinator request.

    Advertises this server's API_VERSION so the gateway can log version skew.
    Adds the Bearer token when AGENT_TOKEN is set — checked fresh on every
    call so an operator export or a test's monkeypatch.setenv/setattr always
    wins, falling back to the value this module parsed out of its own .env at
    import time (never itself exported to os.environ — see
    _AGENT_TOKEN_FROM_FILE above). An MCP host that injects AGENT_TOKEN
    through its own env block (mcp.json's `env`) is unaffected by any of
    this: the host writes it into os.environ before this module is even
    imported, outside this module's reach either way.
    """
    headers = {CLIENT_VERSION_HEADER: str(API_VERSION),
               CLIENT_BUILD_HEADER: VERSION}
    # ⚠ Deliberate TRUTHINESS, not an emptiness/None check (MCPW_R2C5_Builder_
    # Brief.md §2.4, pinned by test_exported_empty_agent_token_falls_back_to_
    # file_token): an exported AGENT_TOKEN="" used to suppress the header even
    # when the file held a real token. Full parity with the CLI door
    # (memory_bridge.py:494, byte-identical expression) is chosen over
    # preserving that edge — a blank export reads as a mistake-shaped input,
    # not a documented kill switch, and one precedence rule across both doors
    # beats a silent divergence. Do not "fix" this back to a presence check.
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
        # Offloaded to a single worker thread: these handlers are async and a
        # sync append on the event loop stalls every concurrent tool call for
        # the duration of a disk write. One thread preserves entry ordering.
        _LOG_EXECUTOR.submit(_append_line,
                             os.path.join(log_dir, f"{tool}.log"),
                             json.dumps(entry) + "\n")
    except OSError as e:
        print(f"[WARN] shared-memory: audit log unavailable ({e})", file=sys.stderr)
    except Exception:
        pass  # logging must never break the save path


def _unavailable(exc: Exception, ceiling: float | None = None) -> str:
    """Uniform message when the gateway cannot be reached. The gateway is the
    only path to memory now, so this is a hard failure rather than a degraded
    mode — saying so plainly beats silently returning nothing.

    A read timeout is a DIFFERENT fault and gets its own message: httpx's
    ReadTimeout stringifies to nothing, so folding it in here told the reader to
    start a service that was already running (fact:1112).

    Structural guard, not a courtesy: a GatewayReplyError means the gateway
    ANSWERED, so it can never be reported as unreachable — even from a call
    site that forgot its own `except GatewayReplyError` clause. This is the
    last place fact:1503's defect could re-enter.
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
    """The ONE 401 response. Six tools used to inline their own copy of this
    message and so never logged the failure — and the write tools were the six,
    which is the worse half to lose from the audit trail. `tool` is the calling
    tool's own name, so the log says which call was rejected.

    Branches on whether a credential was actually SENT: a 401 with no
    Authorization header is a MISSING token, not a rejected one, and telling
    the operator it was "rejected" points them at comparing a value against
    the gateway registry when nothing was ever configured to compare.
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
    """The gateway ANSWERED, and its answer was not a 2xx JSON payload.

    Carries the ready-to-return tool string. Mirrors memory_bridge's class of
    the same name — Group 1: two front doors, one gateway, one error contract.

    ``logged_event`` names the audit event the RAISE SITE already wrote, or is
    None when it wrote nothing. Centralising the decode routed 401 through this
    exception instead of an early return, and a catch block that logs
    unconditionally would then record ONE refused call TWICE — ``auth_failed``
    from _auth_rejected and ``save_rejected`` behind it, where before the 401
    logged ``auth_failed`` alone. Deliberately an ATTRIBUTE and not a phrase
    read back out of the message: keying the audit trail on message text would
    tie it to wording that exists to be improved.
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
        # Same hazard as _gateway_message: this is gateway-controlled text on
        # its way to a terminal or log — strip control characters before the
        # whitespace collapse and the cap (the non-JSON error page is exactly
        # the attacker-shaped body this path exists for).
        return " ".join(_clean_gateway_text(r.text or "").split())[:limit]
    except Exception:
        return ""


# The gateway's own words are reflected into this client's audit log and into
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
    reader will actually see. Mirrors memory_bridge's helper of the same name —
    Group 1: two front doors, one error contract.
    """
    cleaned = "".join(
        ch for ch in msg
        if ch in ("\n", "\t") or (ord(ch) >= 32 and ord(ch) != 127)
    )
    return cleaned.strip()[:_GATEWAY_MESSAGE_MAX]


def _gateway_message(r) -> str | None:
    """The gateway's own ``message`` when the body is JSON and carries one.
    Guarded end to end: a decode failure is a RESULT here, never an exception
    that escapes into the transport handler.

    The message is capped and control-stripped on the way out — see
    ``_clean_gateway_text``."""
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
    """Decode a gateway response ONLY after branching on its status class.

    THE RULE (fact:1503). A non-2xx aiohttp page is plain text — ``"403:
    Read-only token: this route requires a write-capable agent token"`` — and
    ``json.loads`` of ANY such page raises ``JSONDecodeError``. Decoding before
    the status class is branched on therefore turns every unenumerated status
    into a decode exception, which the transport handler then reports as an
    unreachable gateway: a live gateway refusing on authorization read as a
    dead one. This surface carried the identical idiom at twelve sites; the fix
    is one rule applied at all of them, not another per-site guard.

    Raises GatewayReplyError (already-phrased tool string) on every non-2xx and
    on an unparseable 2xx; returns the decoded payload otherwise.
    """
    # _auth_rejected writes `auth_failed` in BOTH of its sub-branches, so the
    # 401 is already in the audit log by the time this is raised — which is what
    # `logged_event` tells the catch block.
    if r.status_code == 401:
        raise GatewayReplyError(_auth_rejected(tool), logged_event="auth_failed")

    # The gateway's OWN words come FIRST, before this client's framing — see the
    # matching comment in memory_bridge._reply_json.
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
    """Render the gateway's search response for an MCP host.

    Every result carries the gateway's own `ref` (`fact:816`, `summary:87`) and
    `record_type`. Those are surfaced verbatim rather than reduced to a bare
    integer, because the bare integer is exactly what makes a follow-up lookup
    resolve against the wrong table.
    """
    if not results:
        return "Result: No relevant documentation found."

    # ⛔ RENDER IN THE ORDER THE GATEWAY RETURNED. This used to partition the
    # results and print every Tier-3 narrative above every fact — which was
    # harmless only while the gateway pinned them there too. The gateway now
    # RANKS summaries against facts on one scale and returns them interleaved,
    # so re-grouping here would reinstate a guarantee the server deliberately
    # removed, and would do it invisibly: the summary would sit on top carrying
    # a score that says it belongs sixth.
    #
    # A client must not re-impose an ordering the server took a position on.
    body = []
    for r in results:
        rtype = r.get("record_type")
        score = r.get("score")
        if rtype in ("summary", "insight"):
            kind = ("Insight (cross-project principle)" if rtype == "insight"
                    else "Global Context Summary")
            bits = [f"Ref: {r.get('ref', r.get('pg_id'))}"]
            # Tier-3 rows carry a real score now; showing it is what makes the
            # position it was given inspectable rather than a matter of trust.
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

    # Counts every row, Tier-3 included — the old header counted only the facts,
    # so a result set was reported as smaller than what was printed.
    header = f"### Unified Memory Results ({len(results)} item(s) found in {elapsed:.2f}s)\n\n"
    parts = [header + "\n\n---\n\n".join(body)] if body else []
    return "\n\n---\n\n".join(parts)


# ── Retrieval ────────────────────────────────────────────────────────────────

def _unranked_warning(results) -> str | None:
    """T-07 (PR #310 review): the SHARED core sentence — must return the
    identical string as ``memory_bridge._unranked_warning`` for the identical
    input; a parity test holds the two in step. Each door decorates it in its
    own idiom (a bare stderr line there, a ``NOTE: …`` prefix here)."""
    if not isinstance(results, list):
        return None
    unranked = sum(1 for row in results if isinstance(row, dict) and row.get("ranked") is False)
    if not unranked:
        return None
    return (f"{unranked} of {len(results)} results are UNRANKED — the reranker "
            f"timed out, this is vector order (see backend_capability on /health)")


def _fallback_warning(payload: object) -> str | None:
    """Mirrors ``memory_bridge._fallback_warning`` exactly (v0.9.62,
    fact:1609) — a parity test holds the two in step. See there for the
    rationale: the gateway serves a KEYWORD (substring) fallback rather than
    failing the search when the embedder is unavailable, and this MUST fire
    on an empty ``results: []`` too — the common shape a natural-language
    query takes against a substring match. Input is the raw gateway payload
    dict, not the unwrapped results list."""
    if not isinstance(payload, dict) or payload.get("fallback") != "keyword":
        return None
    results = payload.get("results")
    n = len(results) if isinstance(results, list) else 0
    return (f"EMBEDDING UNAVAILABLE — keyword (substring) fallback served "
            f"{n} result(s), unranked; the embedder is down or still "
            f"starting (see embedder on /health)")


def _stale_projection_note(capability: dict | None) -> str | None:
    """Mirrors ``memory_bridge._stale_projection_note`` exactly (B1/T-02,
    PR #310 review) — see there for the rationale."""
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
    """The HTTP call + error handling ``hybrid_search_and_rerank()`` used to
    inline, pulled out so it can derive BOTH the unranked warning and
    (v0.9.62, fact:1609) the keyword-fallback warning from the SAME call
    instead of a second HTTP round trip.

    Returns the decoded gateway payload (a dict) on a 2xx reply — success OR
    a gateway-reported ``status: error`` body, both are legitimate JSON
    answers. Returns an already-phrased error STRING — exactly what
    ``hybrid_search_and_rerank`` returned directly before this split — when
    the gateway could not be reached or refused the request outright
    (``GatewayReplyError``/transport failure): those are a different fault
    than a gateway-SERVED fallback and never carry a ``fallback`` marker, so
    a caller need only branch on ``isinstance(payload, str)``."""
    # Sized from the gateway's own published cost, never from a constant.
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
            timeout=httpx.Timeout(ceiling, connect=5.0)
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
    # This tool returns rendered text, not a dict/list — so the unranked and
    # fallback warnings are lines prepended to that text rather than `note`
    # fields. Prepended in the OPPOSITE order the CLI door prints them (the
    # fallback one first, THEN unranked) so the final top-to-bottom order —
    # unranked, then fallback — matches the CLI's stderr order on the two
    # front doors (nit d, delta review).
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
    """

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.
    Stores an artifact in shared memory via the Hive-Mind Gateway.

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
    project and domain; a retrospective inherits BOTH from the decision it
    judges. A decision that names no domain inherits its grounding facts'
    sections as a default — never a ceiling, because a decision routinely
    reaches further than the fact that prompted it.

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

    # Project is required on a fact, exactly as on the CLI front door — the two
    # are doors to one gateway, and a rule enforced on only one of them is a rule
    # with a way around it. There is no cwd to derive from here (the MCP server
    # runs wherever the host launched it), so the model must supply it: ask the
    # operator which project this belongs to rather than inferring one.
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
    # Auth-enabled gateways overwrite metadata.source with the verified agent
    # identity (e.g. "lm_studio"). Preserve the loaded model name so the
    # specific model behind the save is not lost when several share one token.
    m_data.setdefault("model", m_data["source"])
    entities = m_data.get("entities", [])

    coordinator_url = COORDINATOR_BASE
    agent_id = AGENT_ID

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/save",
                json={"content": content, "metadata": m_data, "agent_id": agent_id},
                headers=_auth_headers(),
            )
            result = _reply_json(r, "save_artifact")
    except GatewayReplyError as exc:
        # ONE refused save is ONE audit line. Before the decode was centralised
        # a 401 returned here early, logging `auth_failed` alone and no
        # `save_rejected`; _auth_rejected still writes that line, so this path
        # must not add a second. Every OTHER class — 403, other 4xx, 5xx, a
        # malformed 2xx — logs `save_rejected` here, and that IS new signal:
        # those replies used to be logged as `gateway_down`, which was a lie
        # about a gateway that had answered.
        if exc.logged_event is None:
            _append_log("vector_skill", 2, "save_rejected", {"message": exc.message}, content)
        return exc.message
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
async def archive_reasoning_trace(session_id: str, task: str, steps: list,
                                  project: str = "") -> str:
    """

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.
    Archive the agent's reasoning path as a memory record.

    `steps` is a list of dicts: [{'thought': ..., 'tool': ..., 'result': ...}].

    `project` is REQUIRED, exactly as for any other record — a trace belongs to
    the work that produced it. It is deliberately NOT exempt and NOT defaulted to
    the sentinel: exempting it would quietly rebuild the untagged population the
    project axis exists to remove, and defaulting it would park records without
    anyone deciding to. Ask the operator, or pass 'general_discussion' knowingly.

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
        "source": AGENT_ID,
        "type": "reasoning_trace",
        "session_id": session_id,
        "task": task,
        "step_count": len(steps),
    }
    if project:
        metadata["project"] = project
    return await save_artifact(content, json.dumps(metadata))


def _alternatives_list(alternatives) -> list[str]:
    """One value in, ONE alternative out — verbatim, and never split.

    Deliberately duplicated from memory_bridge.alternatives_list rather than
    imported: a thin client never imports server modules, and these two files
    are the framework's two independent front doors. They carried the SAME
    `alternatives.split(",")` and shredded identically, which is why the fix
    belongs in both — a capability corrected on one client and not the other is
    the Group 1 parity defect this framework keeps paying for.

    A well-written alternative contains commas, so splitting on one stored
    fragments that do not stand alone, in Postgres AND in the graph, with no
    warning. Accepts a list (one entry per option) or a lone string (exactly one
    option — under-splitting never invents an option nobody wrote).
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
    """

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.
    Save an architectural or design decision with full PROV-O provenance.

    Routes through the Memory Coordinator so the Decision→Human→Project→AIAgent
    subgraph is written by the outbox worker — no direct Neo4j writes here.

    `domain` names the SECTION(S) of the project this decision belongs to —
    a list, or one name. A decision asserts its own sections just as it asserts
    its own project; it does NOT inherit them from its evidence, because a
    decision reaches further than the fact that prompted it. Naming none means
    "take my grounding facts' sections", which is a default, never a ceiling.
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
        # ⛔ THE SAME VALUE, IN BOTH PLACES, AND ONLY EVER THIS ONE. A decision
        # has ONE project — the one the operator asserted — and it belongs at
        # the top level as well as in the blob, because that is the key every
        # reader inspecting Postgres directly trusts. Parity with
        # memory_bridge.py's `build_decision_metadata` (`fact:1757`).
        "project": project,
        # A decision mints no entity of its own (decision:1664) — the gateway
        # accepts an empty list permanently; a non-empty one is refused.
        "entities": [],
        "decision": decision_data,
    }
    # The operator has confirmed this project is new, so the save registers it
    # instead of being refused. Never inferred and never a default: from v0.8.44
    # a decision's project is checked against the registry, and that check only
    # means something if declaring a new project is a deliberate act.
    # The SECTIONS of the project this decision belongs to — a list, or one
    # name. A decision ASSERTS these exactly as it asserts its project: it does
    # not inherit them from its evidence, because a decision reaches further
    # than the fact that prompted it. Naming none is fine and means "take my
    # evidence's sections", which is a default and never a ceiling.
    _domains = ([d.strip() for d in domain if isinstance(d, str) and d.strip()]
                if isinstance(domain, list)
                else [d.strip() for d in (domain or "").split(",") if d.strip()])
    if _domains:
        decision_data["domains"] = _domains
    if new_domain:
        metadata["new_domain"] = True
    if new_project:
        metadata["new_project"] = True
    # The registered projects this new one is deliberately NOT. Needed only when
    # the gateway refuses the name as confusable — and it names which.
    _distinct = [d.strip() for d in (confirm_distinct_from or "").split(",") if d.strip()]
    if _distinct:
        metadata["confirm_distinct_from"] = _distinct
    # grounded_in: same "pgid[:role],pgid" grammar as memory_bridge.py's
    # build_decision_metadata — materialised as typed (:Decision)-[:ROLE]->
    # (:Fact|:Decision) edges by the outbox worker.
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
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/save",
                json={"content": content, "metadata": metadata, "agent_id": agent_id},
                headers=_auth_headers(),
            )
            result = _reply_json(r, "save_decision")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return (
            f"Error: Memory coordinator unreachable at {coordinator_url} — "
            f"is hive_mind_proxy.py running? ({exc})"
        )

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
    """

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.
    Record an outcome for an existing Decision as a full retrospective record
    (own searchable record + Retrospective node behind the decision's
    HAD_OUTCOME trigger edge).

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
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{coordinator_url}/memory/retrospective",
                json=payload,
                headers=_auth_headers(),
            )
            result = _reply_json(r, "save_retrospective")
    except GatewayReplyError as exc:
        return exc.message
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

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.
    Retract / supersede an existing FACT (decision 381/384). Decisions and
    retrospectives are refused with HTTP 400: supersession is the fact
    lifecycle. To overturn a decision call save_retrospective against it with
    rating='reversed' — that marks it superseded as the consequence of a verdict
    that stays in the graph for a successor to ground on. To revise a
    retrospective, save a NEW one against the same decision; the latest live
    verdict is the one that counts.

    Soft — the old fact
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
            result = _reply_json(r, "supersede")
    except GatewayReplyError as exc:
        return exc.message
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

    Requires a write-capable agent token: a read-only token receives an
    honest HTTP 403 role refusal from the gateway — expected, do not retry.
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
            result = _reply_json(r, "review_hold")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return (
            f"Error: Memory coordinator unreachable at {coordinator_url} — "
            f"is hive_mind_proxy.py running? ({exc})"
        )
    if result.get("status") == "success":
        return result.get("message", f"Summary {summary_id}: supersession of {pg_id} held.")
    return f"Error: {result.get('message', result)}"


ROLE_REPORTING_MIN_VERSION = "0.9.54"  # R2-01: the server half (agent/role
# on authenticated /health) does not exist on 0.9.52 -- it ships in PR #311.


def _gateway_predates(version: str | None, minimum: str = ROLE_REPORTING_MIN_VERSION) -> bool | None:
    """Whether ``version`` names a gateway release strictly before ``minimum``.
    None when unparseable — treated the same as "predates" by the caller.
    Mirrors ``memory_bridge._gateway_predates`` exactly (T-04, PR #310 review)."""
    try:
        parsed = tuple(int(p) for p in str(version).split("."))
        floor = tuple(int(p) for p in minimum.split("."))
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed < floor


def _role_diagnosis(payload: dict) -> str:
    """T-04 (PR #310 review): three distinguishable reasons `role` can be
    missing — see ``memory_bridge._role_diagnosis`` for the full rationale.
    1) present → verbatim. 2) absent + gateway predates ROLE_REPORTING_MIN_VERSION
    (or unparseable version) → the gateway never sends it. 3) absent + gateway
    current → this caller's own token was not accepted (anonymous-slim reply)."""
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
        async with httpx.AsyncClient(timeout=10.0) as client:
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
    # `agent`/`role` ride on the AUTHENTICATED /health payload (a server change
    # this PR does not build). `agent` is surfaced only when present — it
    # passes through verbatim below; `role` always gets a line via the
    # three-way diagnosis in `_role_diagnosis` (T-04, PR #310 review).
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
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{coordinator_url}/memory/telemetry", headers=_auth_headers()
            )
        return json.dumps(_reply_json(resp, "memory_telemetry"), indent=2)
    except GatewayReplyError as exc:
        return exc.message
    except Exception as e:
        return f"Error: gateway unreachable — {e}"


# ── Reads that the CLI skill already had and this surface did not ────────────

@mcp.tool()
async def record_lineage(ref: str) -> str:
    """

    This is a READ — GET /memory/status/{ref} makes no mutation, so it is on
    the gateway's read-role allowlist and a read-only agent token reaches it
    fine, no 403.
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
            return json.dumps(_reply_json(r, "record_lineage"), indent=2, default=str)
    except GatewayReplyError as exc:
        return exc.message
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
            payload = _reply_json(r, "graph_query")
    except GatewayReplyError as exc:
        return exc.message
    except Exception as exc:
        return _unavailable(exc)
    return json.dumps(payload.get("records", payload), indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
