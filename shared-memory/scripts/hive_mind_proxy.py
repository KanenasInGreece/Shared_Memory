import asyncio
import time
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector
from aiohttp.client_exceptions import (
    ClientConnectionResetError,
    ClientError,
    ServerDisconnectedError,
)
from multidict import CIMultiDict

# Load .env BEFORE importing coordinator — coordinator reads config vars at
# module level, so config must be in os.environ by the time that import runs
# (and secrets must be in secure_env's in-process store — coordinator reads
# those via get_secret(), never os.environ).
#
# SEC-05/S-03 (Credential_Custody_Plan_2026-08-14, PR A1): this used to be a
# private _load_env() that dumped the whole .env into os.environ, including
# every secret it held. It is now the shared split loader also used by
# rem_loop.py and consolidation_loop.py — see secure_env.py.
from secure_env import load_split_env, get_secret, is_secret_key  # noqa: E402

load_split_env()

from coordinator import (
    MemoryCoordinator,
    attach as attach_coordinator,
    auth_middleware,
    backup_quiesce_active,
    resolve_identity,
    _AGENT_TOKENS,
    _AGENT_ROLES,
    AUTH_CONFIGURED_AT_STARTUP,
    AUTH_SCHEME,
    FRAMEWORK_VERSION,
    API_VERSION,
    require_no_plaintext_agent_tokens,
    record_daemon_token_issued,
    record_credentialed_route_denied,
    record_llm_gateway_fault,
    record_llm_upstream_fault,
    _parse_upstream_error_type,
    _decompress_prefix_for_parse,
    _short,
)

# Unified Hive-Mind Async Proxy v7
# Routes /v1/embeddings -> 8070 (BGE-M3)
# Routes /v1/reranking  -> 8071 (BGE-Reranker-v2-m3)
# Routes everything else -> 5000 (LM Studio / local LLM)
# Usage: python proxy_v6.py [PORT]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("hive-proxy")

# --------------------------------------------------------------------------- #
# Watchdog configuration
# --------------------------------------------------------------------------- #
_DAEMON_MAX_RESTARTS    = 5    # circuit-breaker trip count
_DAEMON_RESTART_WINDOW  = 600  # rolling window (seconds) for the trip counter
_DAEMON_MIN_STABLE_SEC  = 30   # uptime needed to reset backoff — avoids boot-loop penalty
_DAEMON_MAX_BACKOFF_SEC = 60   # exponential backoff ceiling (seconds)

# Shared state written by the watchdogs, read by the health handler.
_daemon_proc:    "asyncio.subprocess.Process | None" = None
_daemon_healthy: bool = False  # True while the consolidation subprocess is alive
_rem_proc:       "asyncio.subprocess.Process | None" = None
_rem_healthy:    bool = False  # True while the REM subprocess is alive

# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
# Embedding / reranking backends. Overridable because deployments differ — these
# may run on other ports, in another container network, or on a REMOTE host — so
# the ports this stack happens to use are a default, never an assumption. Clients
# still only ever call the gateway (the 1024-dim mandate is unchanged); this is
# where the gateway itself forwards to.
EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://localhost:8070").rstrip("/")
RERANKER_URL = os.environ.get("RERANKER_URL", "http://localhost:8071").rstrip("/")
ROUTING_MAP = {
    "/v1/embeddings": EMBEDDER_URL,
    "/v1/reranking":  RERANKER_URL,
}
# S-04 (Critical, Credential_Custody_Plan PR A5): the catch-all route forwards
# request.rel_url VERBATIM to whatever backend gets selected, and a backend
# configured with token_env has the provider key attached — so before this
# gate, any caller with a gateway agent token could make the gateway sign an
# arbitrary GET/PUT/DELETE to an arbitrary path on the provider. Binds ONLY
# the credentialed branch (see handle_proxy's `backend_token` check) — a
# local, uncredentialed backend keeps today's full pass-through, because
# there is no key to misuse. Framework-internal (not env-overridable): this
# is the exact, closed set of endpoints the framework itself ever calls.
CREDENTIALED_BACKEND_ALLOWED_ROUTES = frozenset({
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/embeddings"),
    ("POST", "/v1/reranking"),
})
# Embeddings/reranking bodies at or under this size get buffered (not streamed),
# which is what makes the stale-connection retry possible for them. Every real
# caller (coordinator._embed) sends one text field capped at EMBED_MAX_CHARS
# (24000 chars, ~24KB as JSON) — 1MB is a generous margin above that observed
# traffic while still refusing to buffer something genuinely large.
EMBED_RERANK_BUFFER_CAP = int(os.environ.get("EMBED_RERANK_BUFFER_CAP", str(1024 * 1024)))
# Fallback used ONLY when LLM_BACKENDS is unset. Deployments differ — LM Studio
# defaults to :1234, llama.cpp servers commonly :8080 — so keep this overridable
# instead of baking one port into the code. LLM_BACKENDS is the real knob.
DEFAULT_TARGET = os.environ.get("LLM_DEFAULT_TARGET", "http://localhost:5000")

# Reasoning-LLM backend POOL. The gateway owns LLM routing + parallelisation:
# clients have ONE way in (/v1/chat/completions) and never know how many models
# back it, where they live (local or REMOTE host), or which GPU. Configure the
# pool ONLY here, in the framework env — never exposed to clients.
#   LLM_BACKENDS="http://localhost:5000@3,http://localhost:4000@1,http://remote:1234@2"
# Each entry is "url" or "url@weight" (capacity weight, default 1 — give a faster/
# larger card a higher weight). Unset → single backend (DEFAULT_TARGET); identical
# to before. Algorithm (advisor-agent-validated for this exact stack): WEIGHTED
# LEAST-IN-FLIGHT (score = inflight/weight) → fan out concurrent long jobs to the
# free-est capable card. NOT least-response-time (long completions make latency
# meaningless), NOT a gateway queue (forward-and-absorb into llama.cpp's own slot
# queue keeps the gateway stateless). A backend that fails twice in a window is put
# in cooldown; requests retry on the next-best backend — one card always serves.
#
# LLM_BACKENDS_JSON is the preferred form when any backend needs its own
# credential (a paid cloud API, e.g. DeepSeek/xAI) or its own model id — the
# plain comma form above has no way to carry either. It takes priority over
# LLM_BACKENDS when set:
#   LLM_BACKENDS_JSON=[{"url":"http://localhost:5000"},
#                       {"url":"https://api.deepseek.com/v1",
#                        "token_env":"DEEPSEEK_API_KEY","model":"deepseek-chat"}]
# `token_env` names an env var the gateway PROCESS must already have — never a
# literal secret. The gateway resolves it once at startup and, for every request
# routed to that backend, sets Authorization from it — the client's own
# Authorization (its gateway auth token) is never forwarded to any backend, see
# _filter_headers. A configured token_env that isn't actually set excludes that
# backend from the pool (loud, at startup) rather than sending a doomed request.
# `model` overrides the global LLM_MODEL for that backend only — see the model
# rewrite in handle_proxy. See shared-memory/ops/README.md for how to get the
# named var into the gateway's process env (systemd EnvironmentFile, etc.).
def _parse_backend(entry: str) -> tuple[str, float]:
    url, _, w = entry.strip().partition("@")
    try:
        weight = float(w) if w else 1.0
    except ValueError:
        weight = 1.0
    return url.rstrip("/"), max(weight, 0.1)


def _load_llm_backends() -> tuple[list[str], dict[str, float], dict[str, "str | None"], dict[str, "str | None"], dict[str, "dict | None"]]:
    raw_json = os.environ.get("LLM_BACKENDS_JSON", "").strip()
    if raw_json:
        try:
            entries = json.loads(raw_json)
            if not isinstance(entries, list):
                raise ValueError("LLM_BACKENDS_JSON must be a JSON array")
        except (json.JSONDecodeError, ValueError) as e:
            log.error("LLM_BACKENDS_JSON invalid (%s) — falling back to LLM_BACKENDS/LLM_DEFAULT_TARGET", e)
            entries = []
        urls: list[str] = []
        weights: dict[str, float] = {}
        tokens: dict[str, "str | None"] = {}
        models: dict[str, "str | None"] = {}
        extras: dict[str, "dict | None"] = {}
        for entry in entries:
            url = str(entry.get("url", "")).rstrip("/")
            if not url:
                continue
            # Refuse a literal secret in config, loudly — the schema only ever
            # reads token_env (a NAME). Silently ignoring a stray "token"/"api_key"
            # field would be worse than rejecting it: the real key would already
            # be sitting in plaintext in whatever file holds LLM_BACKENDS_JSON
            # (.env or an EnvironmentFile), AND the backend would silently get no
            # credential at all. Exclude the backend and say exactly why.
            _raw_secret_fields = [f for f in ("token", "api_key", "apikey", "secret", "key")
                                   if entry.get(f)]
            if _raw_secret_fields:
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has a literal %s field — this "
                    "framework never accepts a raw secret in config. Use token_env "
                    "instead: the NAME of an env var already exported in the "
                    "gateway's own process environment (see "
                    "shared-memory/ops/README.md, 'Reasoning-LLM backends'). "
                    "Excluding this backend from the pool.",
                    url, _raw_secret_fields)
                continue
            token_env = entry.get("token_env")
            token = None
            if token_env:
                # secure_env classifies every token_env name as a secret
                # (SEC-09) — read it via the accessor, not os.environ; the
                # accessor still falls back to os.environ for a value the
                # deployer supplied through the process's own exec-time
                # environment rather than the framework .env.
                token = get_secret(token_env)
                if not token:
                    log.warning(
                        "LLM backend %s configured with token_env=%s but that "
                        "variable is not set in the gateway's own environment — "
                        "excluding this backend from the pool.", url, token_env)
                    continue
            # Per-backend request-body overrides ("extra_body", the OpenAI-SDK
            # name for the same thing): keys merged into every chat payload
            # routed to this backend. This is what carries provider-specific
            # switches a caller does not know it needs — e.g. DeepSeek's
            # {"thinking": {"type": "disabled"}}, without which a hybrid
            # reasoning model burns metered output tokens on a think block and
            # returns reasoning_content the daemons' JSON extraction never
            # asked for. A malformed value excludes the backend rather than
            # routing to it unconfigured: for a metered backend, "reached
            # without its overrides" is exactly the misconfiguration the
            # field exists to prevent.
            extra_body = entry.get("extra_body")
            if extra_body is not None and not isinstance(extra_body, dict):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has a non-object extra_body "
                    "(%r) — excluding this backend from the pool.",
                    url, type(extra_body).__name__)
                continue
            urls.append(url)
            weights[url] = max(float(entry.get("weight", 1.0) or 1.0), 0.1)
            tokens[url] = token
            models[url] = entry.get("model") or None
            extras[url] = extra_body or None
        if urls:
            return urls, weights, tokens, models, extras
        log.error("LLM_BACKENDS_JSON produced no usable backend — falling back to LLM_BACKENDS/LLM_DEFAULT_TARGET")

    _raw_backends = [_parse_backend(e) for e in os.environ.get("LLM_BACKENDS", "").split(",") if e.strip()]
    if not _raw_backends:
        _raw_backends = [(DEFAULT_TARGET, 1.0)]
    urls = [u for u, _ in _raw_backends]
    weights = {u: w for u, w in _raw_backends}
    return urls, weights, {u: None for u in urls}, {u: None for u in urls}, {u: None for u in urls}


LLM_BACKENDS: list[str]
LLM_WEIGHTS: dict[str, float]
LLM_BACKEND_TOKENS: dict[str, "str | None"]
LLM_BACKEND_MODELS: dict[str, "str | None"]
LLM_BACKENDS, LLM_WEIGHTS, LLM_BACKEND_TOKENS, LLM_BACKEND_MODELS, LLM_BACKEND_EXTRAS = _load_llm_backends()


def _apply_backend_body_overrides(body: bytes, model: "str | None",
                                  extra: "dict | None") -> bytes:
    """The request body as this backend must receive it.

    Pure so it can be tested (and mutation-checked) without the proxy plumbing.
    `extra` (the backend's extra_body config) is merged first and overrides the
    caller — it is the operator's per-backend truth, and the callers are our own
    daemons sending one homogeneous request shape. The `model` override is
    applied last and only when the caller sent a model field, preserving the
    long-standing rewrite contract — so an explicit per-backend model always
    beats an extra_body["model"] left there by mistake. Best-effort by design:
    an unparseable or non-object body is forwarded unchanged rather than
    dropped."""
    if not model and not extra:
        return body
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("Could not parse request body for backend overrides — forwarding unchanged")
        return body
    if not isinstance(payload, dict):
        return body
    if extra:
        payload.update(extra)
    if model and "model" in payload:
        payload["model"] = model
    return json.dumps(payload).encode("utf-8")

# Failure/cooldown (advisor spec): N fails within a window → cooldown; re-probed
# (a normal request) after it elapses. A success clears the fail streak.
LLM_FAIL_THRESHOLD = int(os.environ.get("LLM_FAIL_THRESHOLD", "2"))
LLM_FAIL_WINDOW = float(os.environ.get("LLM_FAIL_WINDOW", "60"))
LLM_COOLDOWN = float(os.environ.get("LLM_COOLDOWN", "300"))
# Per-request retries across backends on a connect failure (transparent to client).
LLM_MAX_TRIES = int(os.environ.get("LLM_MAX_TRIES", "2")) + 1

# The whole pool parallelises. Judge/quality routing (v0.6.1) is GATEWAY-controlled
# at runtime — NOT a user/env setting. The framework's judge will signal its role
# (X-SM-LLM-Role: judge) and the gateway decides allocation; for now every role
# shares the weighted pool, so the judge simply gets the free-est backend. Any
# future dedicate-a-backend policy lives here, in the gateway, never in env.
LLM_POOL: list[str] = list(LLM_BACKENDS)

_llm_inflight: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
# Wedge visibility (2026-07-17 backend-hang lesson): a connection count cannot
# distinguish BUSY from STUCK — a GPU-driver hang held generations for hours
# while every surface said "ok" (reachability) or just "inflight>0". Track when
# each in-flight request STARTED so the pool can report the oldest age; past
# LLM_WEDGE_SUSPECT_AGE the status endpoints add a lazy 2s probe of the
# backend's own /health and flag `suspect_wedged` when it cannot answer.
_llm_inflight_started: dict[str, list[float]] = {b: [] for b in LLM_BACKENDS}
LLM_WEDGE_SUSPECT_AGE = float(os.environ.get("LLM_WEDGE_SUSPECT_AGE", "900"))


def _oldest_inflight_age(backend: str, now: float) -> float | None:
    starts = _llm_inflight_started.get(backend) or []
    return round(now - min(starts), 1) if starts else None


async def _probe_backend_alive(session, backend: str) -> bool:
    """2s liveness probe of the backend's own health surface. llama.cpp serves
    /health; OpenAI-compatible fallback is /v1/models. True = answered."""
    for path in ("/health", "/v1/models"):
        try:
            async with session.get(f"{backend}{path}",
                                   timeout=ClientTimeout(total=2.0)) as r:
                if r.status < 500:
                    return True
        except Exception:
            continue
    return False
_llm_unhealthy_until: dict[str, float] = {b: 0.0 for b in LLM_BACKENDS}
_llm_fail_times: dict[str, list] = {b: [] for b in LLM_BACKENDS}
# Parallelisation telemetry (instrument-first): cumulative requests routed +
# fails per backend, so the realised distribution can be checked against the
# weights (is A770@3 / B580@1 actually happening?) and cooldowns observed.
_llm_routed: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_fail_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
# Runtime reservation (gateway-controlled, NEVER env/user). A backend in this set
# is held OUT of the general parallelise pool so a quality task can use it
# exclusively, then released — e.g. the periodical golden-set recheck (v0.6.1)
# reserves a card, runs its eval on it, releases it: no restart, no degradation,
# the rest of the pool keeps serving REM/NREM. Control endpoint + consumer land
# with the v0.6.1 quality work; the state + pool-exclusion seam is here now.
_llm_reserved: set[str] = set()

# Cache-affinity dispatch (advisor-reviewed). llama.cpp's KV prefix-cache makes a
# repeated large prompt prefix ~8x cheaper (measured 22251->5 prompt tokens,
# 274s->35s) IF the request lands on the SAME backend that already holds that
# prefix. REM's grounding prefix is byte-stable (ORDER BY name), so all REM calls
# share one affinity key and pin to one warm card; NREM's varied prompts flow
# elsewhere. Allocation-free: the card is chosen by least-in-flight on first sight,
# then remembered by prefix hash. A prefix reused >=PROTECT_HITS times (i.e. REM's
# grounding, not a one-shot NREM cluster) marks its card protected, so a non-affine
# request won't evict that warm cache (--parallel 1 has a single KV slot).
AFFINITY_PREFIX_CHARS = int(os.environ.get("LLM_AFFINITY_PREFIX_CHARS", "6144"))
AFFINITY_TTL          = float(os.environ.get("LLM_AFFINITY_TTL", "600"))
AFFINITY_MAX_INFLIGHT = int(os.environ.get("LLM_AFFINITY_MAX_INFLIGHT", "4"))
AFFINITY_PROTECT_HITS = 2
_llm_affinity: dict[str, list] = {}   # key -> [backend, last_ts, hits]
_llm_affinity_hits = 0
_llm_affinity_misses = 0


def _affinity_key(body: bytes) -> str | None:
    """sha1 of the leading AFFINITY_PREFIX_CHARS of the concatenated message
    content — identifies requests sharing a large prompt prefix (the KV-cache
    unit). None if the body is not a parseable chat payload."""
    try:
        msgs = (json.loads(body) or {}).get("messages") or []
        text = "".join(str(m.get("content", "")) for m in msgs)
        if not text:
            return None
        return hashlib.sha1(text[:AFFINITY_PREFIX_CHARS].encode("utf-8", "ignore")).hexdigest()
    except Exception:
        return None


def _llm_mark_fail(backend: str) -> None:
    """Record a backend failure; trip the cooldown if it fails too often."""
    now = time.monotonic()
    _llm_fail_total[backend] = _llm_fail_total.get(backend, 0) + 1
    fails = [t for t in _llm_fail_times.get(backend, []) if now - t < LLM_FAIL_WINDOW]
    fails.append(now)
    _llm_fail_times[backend] = fails
    if len(fails) >= LLM_FAIL_THRESHOLD:
        _llm_unhealthy_until[backend] = now + LLM_COOLDOWN
        _llm_fail_times[backend] = []
        log.warning("LLM backend %s in cooldown for %.0fs (%d fails)", backend, LLM_COOLDOWN, LLM_FAIL_THRESHOLD)


def _llm_mark_ok(backend: str) -> None:
    _llm_fail_times[backend] = []


def _ordered_llm_backends(role: str = "") -> list[str]:
    """Backends to try, best-first, by WEIGHTED least-in-flight (inflight/weight).
    The general pool excludes any backend the gateway has reserved at runtime;
    healthy (out-of-cooldown) backends come first, cooldown ones last so service
    never stops. `role` is a gateway-internal signal (e.g. a future quality/judge
    task addressing its reserved backend) — clients never set or see routing."""
    now = time.monotonic()
    pool = [b for b in LLM_POOL if b not in _llm_reserved] or LLM_POOL
    healthy = [b for b in pool if _llm_unhealthy_until.get(b, 0.0) <= now]
    cooling = [b for b in pool if b not in healthy]
    key = lambda b: _llm_inflight.get(b, 0) / LLM_WEIGHTS.get(b, 1.0)
    return sorted(healthy, key=key) + sorted(cooling, key=key)


def _select_llm_backend(role: str = "", affinity_key: str | None = None) -> str:
    """Pick a backend: cache-affinity first (keep a warm KV prefix on its card),
    else least-in-flight while PROTECTING cards that hold a frequently-reused
    prefix from eviction. Allocation-free (no precomputed weights). Clients never
    choose. Records/refreshes the affinity map as a side effect."""
    global _llm_affinity_hits, _llm_affinity_misses
    now = time.monotonic()
    for k in [k for k, v in _llm_affinity.items() if now - v[1] > AFFINITY_TTL]:
        _llm_affinity.pop(k, None)

    def _usable(b: str) -> bool:
        return (b in LLM_POOL and b not in _llm_reserved
                and _llm_unhealthy_until.get(b, 0.0) <= now)

    # 1) affinity hit — same prefix already warm on a usable, non-saturated card
    ent = _llm_affinity.get(affinity_key) if affinity_key else None
    if ent and _usable(ent[0]) and _llm_inflight.get(ent[0], 0) < AFFINITY_MAX_INFLIGHT:
        ent[1] = now
        ent[2] += 1
        _llm_affinity_hits += 1
        return ent[0]

    # 2) miss — least-in-flight, protecting cards holding a reused (hits>=N) hot prefix
    protected = {v[0] for v in _llm_affinity.values()
                 if now - v[1] <= AFFINITY_TTL and v[2] >= AFFINITY_PROTECT_HITS}
    usable = [b for b in LLM_POOL if _usable(b)]
    cold = ([b for b in usable if b not in protected] or usable
            or [b for b in LLM_POOL if b not in _llm_reserved] or list(LLM_POOL))
    chosen = min(cold, key=lambda b: _llm_inflight.get(b, 0))
    if affinity_key:
        _llm_affinity[affinity_key] = [chosen, now, (ent[2] + 1 if ent else 1)]
        _llm_affinity_misses += 1
    return chosen

# --------------------------------------------------------------------------- #
# RFC 7230 §6.1 — hop-by-hop headers must never be forwarded by a proxy.
# Content-Length is included because we always stream (chunked TE); forwarding
# a stale byte-count causes clients to truncate or hang indefinitely.
# Accept-Encoding is NOT included here — it is an end-to-end request header
# by RFC definition and must be forwarded. Compression is handled transparently
# via auto_decompress=False on the ClientSession (see start_session).
# --------------------------------------------------------------------------- #
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
})


def _scrub_url_credentials(text: str) -> str:
    """Security review O-6: strip userinfo (user:pass@) and the query string
    from any http(s) URL found in `text` before it reaches a client-visible
    body or the gateway log. A ClientError's own __str__ can render the full
    request URL (aiohttp's InvalidURL does), and a real provider pattern
    puts a credential in a URL — a `?key=...` query parameter, or userinfo —
    so echoing that text verbatim is a provider-key leakage path. Only the
    scheme/host/port/path survive; never raises (a malformed "URL" that
    urlsplit chokes on is replaced outright rather than echoed unscrubbed)."""
    def _scrub(m: "re.Match") -> str:
        try:
            parsed = urllib.parse.urlsplit(m.group(0))
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except Exception:
            return "<url-redacted>"
    return re.sub(r"https?://\S+", _scrub, text)


def _safe_request_id(request) -> "str | None":
    """Best-effort read of the request_id auth_middleware stashes on the
    request (PR A3), used to correlate a credential-audit line with the same
    request's gateway audit line. None for a request never routed through
    the middleware (auth disabled, or a lightweight stand-in without a
    mapping interface — every direct handle_proxy() caller in this repo's
    own tests)."""
    try:
        return request.get("request_id")
    except AttributeError:
        return None


def _safe_agent_name(request) -> "str | None":
    """Best-effort read of the authenticated_agent auth_middleware stashes on
    the request — same fallback shape as _safe_request_id, for a lightweight
    test stand-in without a mapping interface, or a request that never went
    through the middleware (auth disabled)."""
    try:
        return request.get("authenticated_agent")
    except AttributeError:
        return None


def _safe_resolve_identity(request) -> "str | None":
    """resolve_identity(), tolerant of a lightweight/missing request double
    — handle_health/handle_pool_status are unit-tested by calling the
    handler directly (some callers pass None: "the session is needed only
    for the rare suspect-wedged probe; unit tests call this handler without
    a real request/app", per handle_pool_status's own docstring). Same
    fallback shape as _safe_request_id/_safe_agent_name above; a genuinely
    absent identity (no headers at all, or no request at all) is
    indistinguishable from an anonymous caller here, which is the correct
    reading either way."""
    try:
        return resolve_identity(request)
    except AttributeError:
        return None


# S-14 (Credential_Custody_Plan PR A5): the two framework daemons — the only
# identities _mint_daemon_token() ever mints for (see the two call sites
# below) — are the sole non-admin identities allowed to steer LLM routing.
_CONSOLIDATION_AGENT_NAME = "consolidation"
_REM_DAEMON_AGENT_NAME    = "rem_daemon"
DAEMON_AGENT_NAMES = frozenset({_CONSOLIDATION_AGENT_NAME, _REM_DAEMON_AGENT_NAME})


def _may_steer_llm(request) -> bool:
    """True if the resolved identity for this request may set X-SM-LLM-*
    backend-steering headers (S-14): a framework daemon, or an admin-role
    token (today unreachable in practice — auth_middleware confines admin
    tokens to /admin/*, so one can never reach handle_proxy at all; checked
    anyway so this stays correct if that routing ever changes). Auth-off
    installs have no identity to check — steering stays available to
    everyone, same backward-compat shape as every other identity-gated
    check in this file when AUTH_CONFIGURED_AT_STARTUP is False."""
    if not AUTH_CONFIGURED_AT_STARTUP:
        return True
    agent_name = _safe_agent_name(request)
    if agent_name in DAEMON_AGENT_NAMES:
        return True
    return _AGENT_ROLES.get(agent_name, "full") == "admin"


def _strip_llm_steering_headers(headers) -> "CIMultiDict[str]":
    """Drop every X-SM-LLM-* header (backend/affinity steering signals) from
    a client-originated request whose identity may not set them (S-14).
    Returns a CIMultiDict — case-insensitive .get()/.items(), matching
    aiohttp's own request.headers semantics — so every downstream reader
    (the role lookup in handle_proxy, then _filter_headers) sees the same
    interface whether or not stripping happened."""
    result: CIMultiDict = CIMultiDict()
    for k, v in headers.items():
        if not k.lower().startswith("x-sm-llm-"):
            result.add(k, v)
    return result

# Upstream mid-stream disconnect — abrupt reset from the upstream server
# (llama-server, BGE-M3) while we are reading via iter_any(). This is a
# ClientError subclass raised by aiohttp's HTTP *client* — NOT a downstream
# client disconnect signal. Caught in the inner streaming block to log at
# WARNING level rather than surfacing as ERROR via the outer ClientError handler.
# Note: ClientDisconnectedError was removed in aiohttp 3.9+; ServerDisconnectedError
# covers abrupt resets. A clean upstream close simply ends iter_any() with no exception.
UPSTREAM_DISCONNECT = (ServerDisconnectedError,)


# --------------------------------------------------------------------------- #
# Proxy
# --------------------------------------------------------------------------- #
class AsyncHiveMindProxy:
    def __init__(self):
        self.session: ClientSession | None = None

    async def start_session(self) -> None:
        connector = TCPConnector(
            limit=200,                  # total concurrent connections across all upstreams
            limit_per_host=80,          # prevents embedding bursts from starving LLM backend
            ttl_dns_cache=300,
            enable_cleanup_closed=True, # evicts half-open sockets immediately; prevents pool leaks
        )
        # connect=5.0: fail fast if an upstream is down.
        # total=None:  never cut off a long-running LLM generation mid-stream.
        #
        # auto_decompress=False: aiohttp decompresses by default but still forwards
        # the upstream's Content-Encoding header. A client receiving decompressed bytes
        # labelled Content-Encoding: gzip will try to decompress again — corruption.
        # With auto_decompress=False the proxy is fully transparent: compressed bytes
        # and their headers travel together and the client handles them correctly.
        timeout = ClientTimeout(total=None, connect=5.0)
        self.session = ClientSession(
            connector=connector,
            timeout=timeout,
            auto_decompress=False,
        )
        log.info("Connection pool ready (limit=200, limit_per_host=80)")

    async def cleanup(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            log.info("Upstream client session closed.")

    def _filter_headers(self, headers, *, strip_gateway_namespace: bool = False) -> dict:
        """Strip hop-by-hop, Host, and Authorization headers.
        Applied identically to both request (→ upstream) and response (→ client).
        Authorization is never forwarded: what a client sends here is its OWN
        gateway auth token (see coordinator.auth_middleware) — a credential
        scoped to talking to the gateway, not to whatever sits behind it. A
        backend that needs its own credential (a paid cloud API) gets one
        added back explicitly in handle_proxy, from LLM_BACKENDS_JSON's
        token_env — never from what the client happened to send.

        `strip_gateway_namespace` additionally strips `x-sm-*` and
        `www-authenticate` — used ONLY on the RESPONSE direction (upstream →
        client, security review O-1). Without it, a hostile/misconfigured
        upstream can set `X-SM-Fault-Origin: gateway` (or `X-SM-LLM-Backend`)
        on an otherwise-successful response and have it pass straight
        through — the gateway's own header assignment only happens on a
        FAULT status, so on success nothing overwrites an upstream-supplied
        value. Treating the X-SM- namespace (and the RFC 6750 challenge
        header, so a client can't be confused about which side of the
        credential boundary wants a token) as gateway-owned means any such
        header on the wire is always one the gateway put there."""
        result = {
            k: v for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in ("host", "authorization")
        }
        if strip_gateway_namespace:
            result = {
                k: v for k, v in result.items()
                if not k.lower().startswith("x-sm-") and k.lower() != "www-authenticate"
            }
        return result

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        # Route on path: embeddings/reranking have fixed targets; everything else
        # is a reasoning-LLM request, dispatched through the backend POOL so the
        # gateway owns parallelisation. The optional X-SM-LLM-Role header is set
        # ONLY by framework components (e.g. the v0.6.1 judge) — never by clients.
        # S-14: backend steering is a daemon/admin capability — every X-SM-LLM-*
        # header a non-steering caller sent is dropped from the view used below
        # AND from what gets forwarded upstream (see upstream_headers further
        # down), before either the role signal or the routing decision is read.
        steer_headers = (request.headers if _may_steer_llm(request)
                          else _strip_llm_steering_headers(request.headers))
        llm_backend: str | None = None
        target_base: str | None = None
        llm_body: bytes | None = None
        for prefix, target in ROUTING_MAP.items():
            if request.path.startswith(prefix):
                target_base = target
                break
        if target_base is None:
            # Reasoning-LLM request → buffer the body so we can compute the
            # cache-affinity key (bounded chat payload; buffering cost negligible),
            # then dispatch cache-affinity-first. The key is computed as late as
            # possible, just before selection, so nothing mutates the prompt after.
            llm_body = await request.read() if request.can_read_body else b""
            role = steer_headers.get("X-SM-LLM-Role", "").strip().lower()
            llm_backend = _select_llm_backend(role, _affinity_key(llm_body))
            target_base = llm_backend

            # Per-backend body rewrites (LLM_BACKENDS_JSON "model" +
            # "extra_body") — a cloud endpoint needs its real model id, not the
            # local "local-model" every caller sends by default, and its
            # provider-specific switches (e.g. thinking disabled) that no
            # caller knows to send. See _apply_backend_body_overrides.
            llm_body = _apply_backend_body_overrides(
                llm_body, LLM_BACKEND_MODELS.get(llm_backend),
                LLM_BACKEND_EXTRAS.get(llm_backend))

        target_url = f"{target_base}{request.rel_url}"
        log.debug("→ %s %s", request.method, target_url)

        upstream_headers = self._filter_headers(steer_headers)
        # Authorization was just stripped above (see _filter_headers) — add it
        # back ONLY for a backend that has its own configured credential. Every
        # other backend (local, or one with no token_env) gets none, same as today.
        backend_token = None
        if llm_backend is not None:
            backend_token = LLM_BACKEND_TOKENS.get(llm_backend)
            if backend_token:
                # S-04 (Critical, PR A5): a request about to carry a provider
                # key may only be POST to a framework-owned endpoint — never
                # an arbitrary method/path forwarded verbatim to a
                # credentialed backend. Checked before Authorization is
                # attached (below) and before any upstream call, so a
                # rejected request never gets near the key.
                route = (request.method, request.path.rstrip("/") or "/")
                if route not in CREDENTIALED_BACKEND_ALLOWED_ROUTES:
                    record_credentialed_route_denied(
                        llm_backend, request.method, request.path,
                        agent_name=_safe_agent_name(request),
                        request_id=_safe_request_id(request),
                    )
                    return web.json_response(
                        {"error": "credentialed backends accept only framework endpoints"},
                        status=403, headers={"X-SM-Fault-Origin": "gateway"},
                    )
                upstream_headers["Authorization"] = f"Bearer {backend_token}"
            # Surface for the gateway's own per-request audit line (PR A3,
            # coordinator._audit's additive backend/key_attached fields) —
            # auth_middleware reads these back off the request after this
            # handler returns. Best-effort: request may be a lightweight
            # stand-in (tests, direct handle_proxy callers) without a
            # mapping interface.
            try:
                request["backend"] = llm_backend
                if backend_token:
                    request["key_attached"] = True
            except (TypeError, AttributeError):
                pass

        # Stream the request body directly to the upstream without buffering it
        # into a single byte array first, UNLESS it's small enough that buffering
        # (and the stale-connection retry that requires a buffered body — see
        # ServerDisconnectedError handling below) is worth it. Every real caller
        # (coordinator._embed) sends one text field capped at EMBED_MAX_CHARS
        # (24000 chars), so this covers all observed traffic; anything with no
        # Content-Length or over the cap keeps streaming as before (memory-flat,
        # unprotected by the retry — the original behaviour for oversized/chunked
        # bodies, e.g. large GraphRAG ingestion payloads if any client ever sends
        # one through this path).
        # NOTE: this bypasses the client_max_size check that request.read() would
        # enforce. Acceptable for this trusted localhost deployment; revisit if the
        # proxy is ever exposed to untrusted clients.
        embed_body: bytes | None = None
        if llm_body is None and request.can_read_body:
            content_length = request.content_length
            if content_length is not None and content_length <= EMBED_RERANK_BUFFER_CAP:
                embed_body = await request.read()

        upstream_data = (
            llm_body if llm_body is not None else
            embed_body if embed_body is not None else
            (request.content if request.can_read_body else None)
        )

        # Initialized to None so exception handlers can check object state directly
        # (.prepared attribute) rather than relying on a parallel boolean flag.
        proxy_resp: web.StreamResponse | None = None
        # A retry is only safe when the body was buffered (llm_body, always; or
        # embed_body, when small enough — see above) rather than streamed via
        # request.content, which is consumed on first use and can never be
        # resent. Anything still streaming keeps the pre-fix single-attempt
        # behaviour.
        have_buffered_body = llm_body is not None or embed_body is not None
        max_attempts = 2 if have_buffered_body else 1

        try:
            # Reserve the in-flight slot INSIDE the try, so the finally below is
            # GUARANTEED to release it. Reserving before the try left a window —
            # target_url/_filter_headers construction, or a CancelledError from an
            # early client disconnect — in which a slot leaked permanently. A leaked
            # slot makes the pool read busy forever, which starves the idle-gated
            # dream daemons (NREM defers on a never-idle pool) with no way back
            # short of a gateway restart. One reservation covers both attempts —
            # a retry is the same logical request, not a second one.
            if llm_backend is not None:
                _llm_inflight[llm_backend] = _llm_inflight.get(llm_backend, 0) + 1
                _llm_inflight_started.setdefault(llm_backend, []).append(time.monotonic())
                _llm_routed[llm_backend] = _llm_routed.get(llm_backend, 0) + 1

            for attempt in range(max_attempts):
                try:
                    async with self.session.request(
                        method=request.method,
                        url=target_url,
                        headers=upstream_headers,
                        data=upstream_data,
                        allow_redirects=False,  # proxy must pass redirects through, never chase them
                    ) as upstream:

                        proxy_resp = web.StreamResponse(
                            status=upstream.status,
                            headers=self._filter_headers(upstream.headers, strip_gateway_namespace=True),
                        )
                        # Stamp the serving backend so daemons can attribute per-backend
                        # telemetry (obs tok/s) without learning routing — observability only.
                        if llm_backend is not None:
                            proxy_resp.headers["X-SM-LLM-Backend"] = llm_backend
                        # Client-facing standard messaging (PR A3): a fault status
                        # from ANY upstream (LLM, embedder, reranker) is an upstream-
                        # origin error — the body still passes through verbatim below,
                        # unchanged. This header is additive and never set on success.
                        if upstream.status >= 400:
                            proxy_resp.headers["X-SM-Fault-Origin"] = "upstream"
                        await proxy_resp.prepare(request)

                        # Best-effort credential-fault classification (PR A3): only for
                        # the LLM pool (embeddings/reranking never carry a provider key)
                        # and only on a fault status. Peeks the FIRST streamed chunk —
                        # error bodies are small JSON that arrive in one chunk in
                        # practice; classification never buffers or reorders anything,
                        # so the passthrough below is untouched even when the peek
                        # can't parse a split/foreign body (falls through to
                        # "transient" — see _classify_llm_fault).
                        fault_classified = llm_backend is None or upstream.status < 400
                        # R-3: auto_decompress=False means a compressed error body
                        # arrives as framing bytes, not JSON — read Content-Encoding
                        # once so the peek below can decompress a bounded prefix
                        # before parsing. The passthrough chunk itself is untouched.
                        content_encoding = upstream.headers.get("Content-Encoding")

                        # write_eof() lives inside the same try as the chunk loop so that
                        # an EOF-time disconnect is handled by the same except clauses.
                        try:
                            async for chunk in upstream.content.iter_any():
                                if not fault_classified:
                                    fault_classified = True
                                    try:
                                        # O-2: the recorder call is wrapped — an
                                        # exception here (e.g. a future edit to the
                                        # recorder) must never truncate the
                                        # passthrough that follows on the next line.
                                        error_type = _parse_upstream_error_type(
                                            _decompress_prefix_for_parse(chunk, content_encoding))
                                        record_llm_upstream_fault(
                                            llm_backend, upstream.status, error_type,
                                            credentialed=bool(backend_token),
                                            request_id=_safe_request_id(request),
                                        )
                                    except Exception as exc:
                                        log.warning(
                                            "credential-fault classification failed for %s: %s",
                                            target_url, type(exc).__name__)
                                await proxy_resp.write(chunk)
                            await proxy_resp.write_eof()

                        except asyncio.CancelledError:
                            # CancelledError is the event loop signalling task cancellation
                            # (shutdown, timeout, framework teardown). It is NOT a disconnect
                            # signal. Must always be re-raised so the event loop can complete
                            # its cancellation sequence; swallowing it stalls graceful shutdown.
                            log.warning("Handler task cancelled during stream: %s", target_url)
                            raise

                        except UPSTREAM_DISCONNECT as e:
                            # Upstream server dropped the connection mid-stream (clean close or
                            # abrupt reset). Response headers are already on the wire; log and
                            # return the partial response rather than attempting a new reply.
                            log.warning("Upstream dropped connection mid-stream: %s — %s", target_url, e)

                        except (ConnectionResetError, IOError) as e:
                            # OS-level socket reset from the downstream client.
                            # Nothing more can be sent; log and return.
                            log.warning("Client disconnected mid-stream: %s — %s", target_url, e)

                        # Fallback classification (PR A3): the loop above never ran its
                        # body — an empty-bodied fault response, or a disconnect before
                        # the first chunk arrived. Still worth recording: the status
                        # alone is enough to classify 401/403, and an unparseable/absent
                        # body classifies as transient either way.
                        if not fault_classified:
                            fault_classified = True
                            try:
                                record_llm_upstream_fault(
                                    llm_backend, upstream.status, None,
                                    credentialed=bool(backend_token),
                                    request_id=_safe_request_id(request),
                                )
                            except Exception as exc:
                                log.warning(
                                    "credential-fault classification failed for %s: %s",
                                    target_url, type(exc).__name__)

                        if llm_backend is not None:
                            _llm_mark_ok(llm_backend)   # connected + served — clear fail streak
                        return proxy_resp

                except (ClientConnectionResetError, ServerDisconnectedError) as e:
                    # A pooled connection reused just as the backend started closing
                    # it — a connection-reuse race, not evidence the backend is down
                    # (enable_cleanup_closed already evicts the stale socket).
                    # "Cannot write to closing transport" (the write-phase reset,
                    # verified live as the actual exception raised — aiohttp 3.14
                    # ClientConnectionResetError, NOT ServerDisconnectedError, which
                    # an earlier version of this fix caught instead and which never
                    # once matched this error in production) is caught here alongside
                    # ServerDisconnectedError (the read-phase counterpart) since both
                    # represent the same underlying race, just observed at a
                    # different point in the request lifecycle. proxy_resp is still
                    # None here: the failure happens writing the request, before any
                    # response is read, so retrying on a fresh connection is safe.
                    # First-attempt-only: a second failure in a row is treated as a
                    # real problem, same as before this retry existed.
                    if attempt < max_attempts - 1 and proxy_resp is None:
                        log.warning(
                            "Stale connection to %s (%s) — retrying once on a fresh "
                            "connection before treating this as a backend failure.",
                            target_url, e)
                        continue
                    raise

        except asyncio.CancelledError:
            # CancelledError is BaseException (Python 3.8+) and won't be caught by
            # `except Exception` below, but this explicit clause documents that we
            # never absorb cancellation at any level.
            raise

        except ClientError as ce:
            # Upstream is down, unreachable, or refused the connection.
            # 503: the proxy is fine; the backend is not.
            # O-6: log the SCRUBBED, BOUNDED message text — a ClientError's own
            # __str__ can render the full request URL (aiohttp's InvalidURL
            # does), and a real provider pattern puts a credential in a URL
            # (userinfo, or a `?key=...` query parameter). The client-visible
            # body below uses the exception's CLASS NAME only, never its text.
            log.error("Upstream unreachable %s: %s", target_url,
                      _short(_scrub_url_credentials(str(ce))))
            if llm_backend is not None:
                _llm_mark_fail(llm_backend)
                # Gateway-origin fault (PR A3) — the gateway itself observed this
                # (a connect/refuse failure), never what the upstream said, so it
                # counts in the `gateway` group only, and is logged only when the
                # call was credentialed (see record_llm_gateway_fault docstring).
                record_llm_gateway_fault(llm_backend, type(ce).__name__,
                                          credentialed=bool(backend_token),
                                          request_id=_safe_request_id(request))
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": f"Backend unreachable: {type(ce).__name__}"}, status=503,
                                      headers={"X-SM-Fault-Origin": "gateway"})

        except asyncio.TimeoutError:
            # Connect timeout to upstream — correct status is 504, not 500.
            log.warning("Upstream connect timeout: %s", target_url)
            if llm_backend is not None:
                _llm_mark_fail(llm_backend)
                record_llm_gateway_fault(llm_backend, "TimeoutError",
                                          credentialed=bool(backend_token),
                                          request_id=_safe_request_id(request))
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": "Upstream connect timeout"}, status=504,
                                      headers={"X-SM-Fault-Origin": "gateway"})

        except Exception as e:
            # O-6: same treatment as the ClientError branch above — scrubbed/
            # bounded text in the log, class name only in the client-visible
            # body. `exc_info=True` alone is NOT enough here: the traceback
            # formatter calls str() on the ORIGINAL exception object again
            # when rendering its final line, which would re-embed the raw
            # (unscrubbed) text regardless of what was passed as the log
            # message — so a substitute exception carrying the SCRUBBED text
            # is passed via an explicit exc_info tuple instead, keeping the
            # real traceback frames (file/line — the actual debugging value)
            # while the rendered exception message stays scrubbed.
            scrubbed_msg = _short(_scrub_url_credentials(str(e)))
            try:
                scrubbed_exc = type(e)(scrubbed_msg)
            except Exception:
                scrubbed_exc = RuntimeError(scrubbed_msg)  # exotic __init__ signature — fall back
            log.error("Unexpected proxy error for %s: %s", target_url, scrubbed_msg,
                      exc_info=(type(scrubbed_exc), scrubbed_exc, e.__traceback__))
            if llm_backend is not None:
                record_llm_gateway_fault(llm_backend, type(e).__name__,
                                          credentialed=bool(backend_token),
                                          request_id=_safe_request_id(request))
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": f"Proxy error: {type(e).__name__}"}, status=500,
                                      headers={"X-SM-Fault-Origin": "gateway"})

        finally:
            # Release the in-flight slot so least-busy selection stays accurate,
            # whatever the outcome (success, disconnect, error, cancellation).
            # NOTE the honesty gap this creates: on a client timeout the SERVER
            # may keep generating (zombie) — inflight drops to 0 while the
            # backend is still busy. The oldest-inflight age + suspect_wedged
            # probe on the status surfaces exist to catch the sustained form.
            if llm_backend is not None:
                _llm_inflight[llm_backend] = max(0, _llm_inflight.get(llm_backend, 0) - 1)
                starts = _llm_inflight_started.get(llm_backend)
                if starts:
                    starts.remove(min(starts))


# --------------------------------------------------------------------------- #
# Daemon token helpers
# --------------------------------------------------------------------------- #

def _daemon_env(agent_name: str) -> dict:
    """Build a subprocess environment for the named daemon: non-secret config
    the proxy must pin, and NOTHING ELSE.

    SEC-05 (Credential_Custody_Plan_2026-08-14, PR A1): this used to be
    `os.environ.copy()`, which handed every secret the gateway process held
    (PG_PASSWORD, NEO4J_PASSWORD, AGENT_TOKENS, provider keys) to the child's
    exec-time environment — visible for the child's whole lifetime via
    `/proc/<pid>/environ`. Daemons self-load their own DB credentials through
    secure_env.load_split_env() (each has its own copy of the framework .env
    to read); they never receive one via this env dict.

    PR A1 still had one deliberate exception here: the daemon's own
    AGENT_TOKEN crossed via this dict. PR A2 (SEC-10) closes it — see
    `_daemon_env_and_token_fd()` below, which delivers a freshly-minted,
    per-boot token through an inherited pipe fd instead. `agent_name` is
    kept as a parameter for call-site symmetry with that function and
    because every caller already has it at hand, even though this function
    itself no longer branches on it.
    """
    return {k: v for k, v in os.environ.items() if not is_secret_key(k)}


# Agent name -> the digest currently registered in coordinator._AGENT_TOKENS
# for that agent's EPHEMERAL daemon token (as opposed to a persisted
# AGENT_TOKENS registry entry). Lets a re-mint on daemon restart revoke the
# PREVIOUS ephemeral token rather than leaving it valid forever alongside
# the new one — a crash-restart rotates and invalidates in the same step.
_ephemeral_daemon_token_digests: dict[str, str] = {}


def _mint_daemon_token(agent_name: str) -> str:
    """Mint a fresh, random, per-boot bearer token for one of the two
    framework daemons (SEC-10, Credential_Custody_Plan_2026-08-14 PR A2) and
    register it in-memory in coordinator._AGENT_TOKENS, keyed by its digest
    exactly like every other registry entry — `_lookup_agent_by_token()`
    does not need to know an entry is ephemeral.

    Never written to any file, never logged, and never persisted anywhere:
    a gateway restart mints a fresh token for both daemons, which is the
    whole migration (the plan's 'Daemons: transparent on restart'
    seamlessness criterion). Any PREVIOUS ephemeral token this function
    registered for `agent_name` is revoked first, so a daemon respawn does
    not accumulate stale-but-still-valid tokens in the registry.
    """
    old_digest = _ephemeral_daemon_token_digests.pop(agent_name, None)
    if old_digest is not None:
        _AGENT_TOKENS.pop(old_digest, None)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    _AGENT_TOKENS[digest] = agent_name
    _ephemeral_daemon_token_digests[agent_name] = digest
    record_daemon_token_issued(agent_name)  # PR A3: counter + credential-audit line
    return token


def _revoke_daemon_token(agent_name: str) -> None:
    """Deregister `agent_name`'s ephemeral daemon token (nit fix, A2 security
    review, finding 10). Used when the daemon process failed to spawn AFTER
    its token was already minted and registered — no process holds it, so it
    must not linger in coordinator._AGENT_TOKENS as a still-valid credential
    with nothing behind it."""
    digest = _ephemeral_daemon_token_digests.pop(agent_name, None)
    if digest is not None:
        _AGENT_TOKENS.pop(digest, None)


def _daemon_env_and_token_fd(agent_name: str) -> "tuple[dict, int | None]":
    """Build a daemon's child environment plus a pipe fd carrying its
    freshly-minted AGENT_TOKEN (SEC-10) — never via the child environment,
    argv, or any file.

    The read end's fd NUMBER is named by the AGENT_TOKEN_FD env var (a
    number is meaningless off this process tree, so it is not itself
    secret); the token VALUE crosses only through the pipe's kernel buffer.
    The token is written and the write end closed HERE, before the caller
    spawns the child: token_urlsafe(32) is far under PIPE_BUF, so the write
    never blocks, and the bytes are already sitting in the pipe's kernel
    buffer by the time exec() runs.

    Returns (env, read_fd) — read_fd is None when auth is disabled for this
    install (coordinator.AUTH_CONFIGURED_AT_STARTUP is False, fix for
    finding 1): no token is minted, AGENT_TOKEN_FD is not set, and the
    caller must not pass_fds. Minting a token for a disabled-auth install
    would flip coordinator._AGENT_TOKENS from empty to non-empty — exactly
    the state AUTH_CONFIGURED_AT_STARTUP exists to stop the auth middleware
    from misreading as "auth is now configured". Skipping the mint keeps
    pre-A2 behaviour byte for byte: daemons' unauthenticated calls pass
    exactly as before.
    """
    env = _daemon_env(agent_name)
    if not AUTH_CONFIGURED_AT_STARTUP:
        return env, None
    token = _mint_daemon_token(agent_name)
    read_fd, write_fd = os.pipe()
    try:
        try:
            os.write(write_fd, token.encode("utf-8"))
        finally:
            os.close(write_fd)
    except Exception:
        # Nit fix (finding 10): if the write itself raised, read_fd would
        # otherwise leak -- nothing else in this function's error path
        # closes it, and the caller never receives it to close either.
        os.close(read_fd)
        _revoke_daemon_token(agent_name)
        raise
    env["AGENT_TOKEN_FD"] = str(read_fd)
    return env, read_fd


# --------------------------------------------------------------------------- #
# Consolidation daemon lifecycle
# --------------------------------------------------------------------------- #
async def _start_daemon() -> "asyncio.subprocess.Process | None":
    daemon_path = Path(__file__).parent / "consolidation_loop.py"
    if not daemon_path.exists():
        log.warning("Daemon script not found at %s — consolidation will not run", daemon_path)
        return None
    uv = shutil.which("uv")
    if not uv:
        log.warning("uv not in PATH — cannot start consolidation daemon")
        return None
    env, read_fd = _daemon_env_and_token_fd(_CONSOLIDATION_AGENT_NAME)
    try:
        proc = await asyncio.create_subprocess_exec(
            uv, "run",
            "--with", "httpx",
            "--with", "psycopg2-binary",
            "--with", "neo4j",
            "python", str(daemon_path),
            env=env,
            pass_fds=(read_fd,) if read_fd is not None else (),
        )
    except Exception:
        # Nit fix (finding 10): the token was already minted and registered
        # before spawn was attempted -- if spawn itself failed, nothing
        # holds that token, so revoke it rather than leaving it valid with
        # no daemon behind it.
        if read_fd is not None:
            _revoke_daemon_token(_CONSOLIDATION_AGENT_NAME)
        raise
    finally:
        if read_fd is not None:
            os.close(read_fd)
    log.info("Consolidation daemon started (pid %d)", proc.pid)
    return proc


async def _start_rem_daemon() -> "asyncio.subprocess.Process | None":
    rem_path = Path(__file__).parent / "rem_loop.py"
    if not rem_path.exists():
        log.warning("REM script not found at %s — REM enrichment will not run", rem_path)
        return None
    uv = shutil.which("uv")
    if not uv:
        log.warning("uv not in PATH — cannot start REM daemon")
        return None
    env, read_fd = _daemon_env_and_token_fd(_REM_DAEMON_AGENT_NAME)
    try:
        proc = await asyncio.create_subprocess_exec(
            uv, "run",
            "--with", "httpx",
            "--with", "psycopg2-binary",
            "--with", "neo4j",
            "python", str(rem_path),
            env=env,
            pass_fds=(read_fd,) if read_fd is not None else (),
        )
    except Exception:
        if read_fd is not None:
            _revoke_daemon_token(_REM_DAEMON_AGENT_NAME)
        raise
    finally:
        if read_fd is not None:
            os.close(read_fd)
    log.info("REM daemon started (pid %d)", proc.pid)
    return proc


async def _watchdog_rem_daemon(stop_event: asyncio.Event) -> None:
    """Start the REM daemon and restart it on unexpected crashes.

    Uses identical watchdog logic to the consolidation daemon:
    exponential backoff, stable-uptime reset, and circuit-breaker trip.
    """
    global _rem_proc, _rem_healthy

    restart_times: list[float] = []
    backoff = 1.0

    while not stop_event.is_set():
        proc = await _start_rem_daemon()
        if proc is None:
            _rem_healthy = False
            return

        _rem_proc    = proc
        _rem_healthy = True
        t_start = asyncio.get_event_loop().time()

        await proc.wait()
        _rem_healthy = False

        if stop_event.is_set():
            break

        uptime   = asyncio.get_event_loop().time() - t_start
        exitcode = proc.returncode

        if exitcode in (0, -signal.SIGTERM):
            log.info("REM daemon exited cleanly (code %d).", exitcode)
            break

        log.warning(
            "REM daemon crashed (code %d, uptime %.1fs) — evaluating restart.",
            exitcode, uptime,
        )

        if uptime >= _DAEMON_MIN_STABLE_SEC:
            backoff = 1.0

        now = asyncio.get_event_loop().time()
        restart_times = [t for t in restart_times if now - t < _DAEMON_RESTART_WINDOW]
        if len(restart_times) >= _DAEMON_MAX_RESTARTS:
            log.critical(
                "REM daemon crashed %d times in %ds — circuit breaker open.",
                _DAEMON_MAX_RESTARTS, _DAEMON_RESTART_WINDOW,
            )
            break

        restart_times.append(now)
        log.info(
            "Restarting REM daemon in %.1fs (crash %d/%d this window)...",
            backoff, len(restart_times), _DAEMON_MAX_RESTARTS,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            break
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, _DAEMON_MAX_BACKOFF_SEC)

    log.info("REM daemon watchdog exiting.")


async def _watchdog_daemon(stop_event: asyncio.Event) -> None:
    """Start the consolidation daemon and restart it on unexpected crashes.

    False-positive protection:
    - Only restarts on unexpected exits (not returncode 0 or -SIGTERM).
    - Exponential backoff (1 s → … → 60 s) so a boot-loop fails slowly.
    - Backoff resets when the daemon ran stably for ≥ _DAEMON_MIN_STABLE_SEC.
    - Circuit breaker: ≥ _DAEMON_MAX_RESTARTS crashes inside _DAEMON_RESTART_WINDOW
      seconds → log CRITICAL and stop restarting (requires gateway restart to reset).
    """
    global _daemon_proc, _daemon_healthy

    restart_times: list[float] = []
    backoff = 1.0

    while not stop_event.is_set():
        proc = await _start_daemon()
        if proc is None:
            _daemon_healthy = False
            return

        _daemon_proc    = proc
        _daemon_healthy = True
        t_start = asyncio.get_event_loop().time()

        await proc.wait()
        _daemon_healthy = False

        if stop_event.is_set():
            # Clean shutdown — gateway is going down; don't restart.
            break

        uptime   = asyncio.get_event_loop().time() - t_start
        exitcode = proc.returncode

        if exitcode in (0, -signal.SIGTERM):
            log.info("Consolidation daemon exited cleanly (code %d).", exitcode)
            break

        log.warning(
            "Consolidation daemon crashed (code %d, uptime %.1fs) — evaluating restart.",
            exitcode, uptime,
        )

        # Reset backoff if the daemon was stable long enough — avoids penalising
        # recoverable transient failures (brief Postgres blip, LLM timeout).
        if uptime >= _DAEMON_MIN_STABLE_SEC:
            backoff = 1.0

        # Circuit breaker: count crashes inside the rolling window.
        now = asyncio.get_event_loop().time()
        restart_times = [t for t in restart_times if now - t < _DAEMON_RESTART_WINDOW]
        if len(restart_times) >= _DAEMON_MAX_RESTARTS:
            log.critical(
                "Consolidation daemon crashed %d times in %ds — "
                "circuit breaker open. Restart the gateway to reset.",
                _DAEMON_MAX_RESTARTS, _DAEMON_RESTART_WINDOW,
            )
            break

        restart_times.append(now)
        log.info(
            "Restarting consolidation daemon in %.1fs (crash %d/%d this window)...",
            backoff, len(restart_times), _DAEMON_MAX_RESTARTS,
        )
        try:
            # Sleep with backoff — but wake immediately if shutdown fires.
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            break  # stop_event fired during backoff — clean exit
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, _DAEMON_MAX_BACKOFF_SEC)

    log.info("Daemon watchdog exiting.")


# --------------------------------------------------------------------------- #
# Pool status — cheap, in-memory LLM capacity signal for the dreaming daemons
# --------------------------------------------------------------------------- #
async def handle_pool_status(request: web.Request) -> web.Response:
    """GET /pool/status — in-memory LLM pool availability, no upstream probes.
    A backend is available iff zero in-flight, not in cooldown, not reserved.
    Because ALL LLM traffic (dream cycles AND user chats) flows through the
    gateway, in-flight IS the LLM-usage signal — so REM/NREM gate on this instead
    of a global nvtop check (which self-defers to our own dream work and ignores a
    free card). Desktop/display GPU use is intentionally not considered.

    SEC-A5-01 (PR A5 fix round): the roster/pool-state detail below (backend
    URLs keyed to inflight/cooldown/reserved/available — an idle/busy oracle
    for whatever provider is configured) is gated exactly like /health
    (SEC-A5-03: ONLY when AUTH_CONFIGURED_AT_STARTUP is true — an auth-off
    install keeps today's full payload). Every REAL internal caller sends
    its own token: pool_status.pool_has_free_slot() (rem_loop.py,
    consolidation_loop.py) and relation_sweep.py's direct probe both attach
    their daemon/agent Authorization header — see those modules'
    _auth_headers(). An anonymous caller on an auth-configured install gets
    an empty object; pool_status.py's `.get("free_slots", 1)` default then
    fail-opens exactly as it already does on any other unreachable/erroring
    gateway, rather than silently and permanently losing slot-awareness."""
    if AUTH_CONFIGURED_AT_STARTUP and not bool(_safe_resolve_identity(request)):
        return web.json_response({})

    now = time.monotonic()
    # Lazy/defensive: the session is needed only for the rare suspect-wedged
    # probe; unit tests call this handler without a real request/app.
    _app = getattr(request, "app", None)
    _session = _app["proxy"].session if _app is not None and "proxy" in _app else None
    backends, free = {}, 0
    for b in LLM_POOL:
        avail = (_llm_inflight.get(b, 0) == 0
                 and _llm_unhealthy_until.get(b, 0.0) <= now
                 and b not in _llm_reserved)
        age = _oldest_inflight_age(b, now)
        entry = {
            "inflight": _llm_inflight.get(b, 0),
            "oldest_inflight_age_s": age,
            "cooldown": round(max(0.0, _llm_unhealthy_until.get(b, 0.0) - now), 1),
            "reserved": b in _llm_reserved,
            "available": avail,
        }
        # Lazy wedge check (the one exception to "no upstream probes"): only
        # when a request has been in flight suspiciously long — busy-generating
        # backends answer their own /health instantly; a driver-hung one can't.
        if age is not None and age > LLM_WEDGE_SUSPECT_AGE and _session is not None:
            entry["suspect_wedged"] = not await _probe_backend_alive(_session, b)
        backends[b] = entry
        free += 1 if avail else 0
    return web.json_response({"free_slots": free, "backends": backends})


# --------------------------------------------------------------------------- #
# Backend CAPABILITY probing — "can it serve", not "is it up"
# --------------------------------------------------------------------------- #
# How often the capability probe re-measures. It costs real inference time on
# the same backends that serve traffic, so it is deliberately infrequent.
CAPABILITY_PROBE_INTERVAL_S = float(
    os.environ.get("CAPABILITY_PROBE_INTERVAL_S", "600"))
# The probe payload. Small enough to be cheap, large enough to be representative
# — a one-token ping would measure nothing about the cost that actually matters.
CAPABILITY_PROBE_DOCS = int(os.environ.get("CAPABILITY_PROBE_DOCS", "4"))
CAPABILITY_PROBE_DOC_CHARS = int(
    os.environ.get("CAPABILITY_PROBE_DOC_CHARS", "1000"))

# Populated by the background probe; read by /health. "unknown" until the first
# probe lands — NEVER asserted as healthy on no data (decision 928's rule: "not
# yet probed" must not read as "verified clean").
_capability: dict = {"status": "unknown", "probed_at": None}


def capability_snapshot() -> dict:
    return dict(_capability)


async def _probe_capability(session) -> dict:
    """Time both critical backends on a fixed, representative payload.

    Reports the OBSERVED throughput and — the part that matters — projects it
    onto the largest payload the framework can actually send, then compares that
    against the timeout the caller would apply. `serves_full_payload: false` is
    the machine-readable form of the defect that hid here: a backend that is up,
    answers /health, and still cannot finish a real request in time."""
    from dream_telemetry import (EMBED_MAX_CHARS, RERANK_MAX_DOC_CHARS,
                                 embed_ceiling, rerank_ceiling)

    out: dict = {"probed_at": datetime.now(timezone.utc).isoformat()}
    try:
        out["gateway_host_load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass   # not available on every platform — never fail the probe for it

    # ── reranker ────────────────────────────────────────────────────────────
    docs = ["lorem ipsum dolor sit amet " * 40] * CAPABILITY_PROBE_DOCS
    docs = [d[:CAPABILITY_PROBE_DOC_CHARS] for d in docs]
    probe_chars = sum(len(d) for d in docs)
    entry: dict = {"probe_chars": probe_chars}
    try:
        t0 = time.monotonic()
        async with session.post(
            f"{RERANKER_URL}/v1/reranking",
            json={"query": "capability probe", "documents": docs,
                  "top_n": len(docs)},
            timeout=ClientTimeout(total=max(30.0, rerank_ceiling(docs))),
        ) as r:
            await r.read()
            ok = r.status < 400
        dt = max(time.monotonic() - t0, 1e-6)
        entry["latency_s"] = round(dt, 2)
        entry["throughput_chars_s"] = round(probe_chars / dt)
        if ok and entry["throughput_chars_s"] > 0:
            # The worst case this framework can actually send: a full candidate
            # set of fully-clamped documents.
            full_chars = 20 * RERANK_MAX_DOC_CHARS
            projected = full_chars / entry["throughput_chars_s"]
            allowed = rerank_ceiling(["x" * RERANK_MAX_DOC_CHARS] * 20)
            entry["projected_full_payload_s"] = round(projected, 1)
            entry["ceiling_s"] = round(allowed, 1)
            entry["serves_full_payload"] = projected <= allowed
            entry["status"] = "ok" if projected <= allowed else "too_slow"
        else:
            entry["status"] = "failing"
    except Exception as exc:
        entry["status"] = "failing"
        entry["error"] = type(exc).__name__
    out["reranker"] = entry

    # ── embedder ────────────────────────────────────────────────────────────
    text = ("lorem ipsum dolor sit amet " * 40)[:CAPABILITY_PROBE_DOC_CHARS]
    entry = {"probe_chars": len(text)}
    try:
        t0 = time.monotonic()
        async with session.post(
            f"{EMBEDDER_URL}/v1/embeddings",
            json={"input": text, "model": "bge-m3"},
            timeout=ClientTimeout(total=max(30.0, embed_ceiling(len(text)))),
        ) as r:
            await r.read()
            ok = r.status < 400
        dt = max(time.monotonic() - t0, 1e-6)
        entry["latency_s"] = round(dt, 2)
        entry["throughput_chars_s"] = round(len(text) / dt)
        if ok and entry["throughput_chars_s"] > 0:
            projected = EMBED_MAX_CHARS / entry["throughput_chars_s"]
            allowed = embed_ceiling(EMBED_MAX_CHARS)
            entry["projected_full_payload_s"] = round(projected, 1)
            entry["ceiling_s"] = round(allowed, 1)
            entry["serves_full_payload"] = projected <= allowed
            entry["status"] = "ok" if projected <= allowed else "too_slow"
        else:
            entry["status"] = "failing"
    except Exception as exc:
        entry["status"] = "failing"
        entry["error"] = type(exc).__name__
    out["embedder"] = entry

    out["status"] = ("ok" if all(out[k].get("status") == "ok"
                                 for k in ("reranker", "embedder"))
                     else "degraded")
    return out


async def _capability_probe_daemon(proxy, stop_event) -> None:
    """Refresh the capability snapshot on a slow cadence, forever.

    Wrapped so a probe failure can never propagate: this is an OBSERVABILITY
    path, and an unguarded exception here would take down the thing it exists
    to report on (the trap named in CLAUDE.md's Group 3)."""
    global _capability
    while not stop_event.is_set():
        try:
            _capability = await _probe_capability(proxy.session)
        except Exception as exc:
            log.warning("capability probe failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=CAPABILITY_PROBE_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------- #
# Health endpoint
# --------------------------------------------------------------------------- #
# S-11 (PR A5): short TTL cache around handle_health's own expensive fan-out
# (2+N upstream probes per hit: embedder, reranker, every LLM backend, plus
# the wedge probe when suspect). /health is polled frequently (the monitor,
# `doctor`, every LLM-wedge caller) and none of that traffic needs a fresh
# probe every single hit. Env-overridable; a few seconds by default — long
# enough to absorb a burst, short enough that a real outage still shows up
# fast. In-process only: a restart clears `_health_cache` along with every
# other module global, so a cached response can never outlive the process it
# was computed in — trivially true, not merely assumed, because the cache
# and FRAMEWORK_VERSION/API_VERSION live in the same process memory and are
# rebuilt together on the next probe after a restart.
HEALTH_CACHE_TTL_S = float(os.environ.get("HEALTH_CACHE_TTL_S", "3"))
_health_cache: dict = {"checks": None, "ts": 0.0}
# SEC-A5-05b (PR A5 fix round): single-flight coalescing for a cache miss —
# see _health_probe_cached's docstring. Constructed at module import time
# with no running loop; safe under Python's current asyncio (a Lock no
# longer binds to a specific loop at construction, only on first use inside
# one), which is what lets this be a plain module global like _health_cache.
_health_probe_lock = asyncio.Lock()


async def _build_health_checks(proxy: "AsyncHiveMindProxy", coordinator) -> dict:
    """The full probe: every upstream backend, daemon liveness, config, and
    the dream-cycle snapshot. Returns the SAME shape regardless of caller —
    disclosure is handle_health's job, applied AFTER this is computed or
    read from cache, never baked into the probe itself (S-10). has_credential/
    model are unconditionally included here for that reason: only an
    authenticated caller ever sees this dict at all now, so the per-field
    gate this function used to apply is redundant — handle_health's own
    caller_authenticated branch is the one gate that matters.
    """
    checks: dict[str, str] = {}

    # The embedder and reranker are llama.cpp containers that expose /health.
    # A reasoning backend is "LM Studio or any OpenAI-compatible endpoint";
    # those do NOT standardise /health — LM Studio logs an error for the unknown
    # route on every probe. Use /v1/models, which every OpenAI-compatible server
    # (LM Studio included) serves, as the LLM liveness check instead.
    for name, url in [
        ("embedder", f"{EMBEDDER_URL}/health"),
        ("reranker",  f"{RERANKER_URL}/health"),
    ]:
        try:
            timeout = ClientTimeout(total=2.0)
            async with proxy.session.get(url, timeout=timeout) as r:
                checks[name] = "ok" if r.status < 400 else f"http_{r.status}"
        except asyncio.TimeoutError:
            checks[name] = "timeout"
        except Exception:
            checks[name] = "down"

    # ⛔ LIVENESS IS NOT CAPABILITY. The two probes above ask "is the process
    # up", which both backends answered "ok" throughout a period in which the
    # reranker could not serve a single real request inside the caller's
    # timeout: a full candidate set cost ~64 s against a 5 s ceiling, so every
    # search silently fell back to unranked vector order while /health read
    # green. A backend that answers /health and cannot do its job is the exact
    # failure this section exists to make visible.
    #
    # So alongside liveness we report CAPABILITY, measured by sending a fixed
    # representative payload to the real scoring endpoint and timing it. The
    # result is CACHED and refreshed on a slow cadence by a background task —
    # the probe costs real seconds, and /health is polled by the monitor, so it
    # must never run inline. Same pattern as the consolidation snapshot below.
    checks["backend_capability"] = capability_snapshot()

    # Reasoning-LLM backend pool — probe each; "llm" is ok if ANY is up (the pool
    # tolerates a down backend). Per-backend statuses are reported for observability;
    # a reserved judge backend is flagged. A single-backend deployment just shows one.
    backend_status: dict[str, str] = {}
    for b in LLM_BACKENDS:
        try:
            async with proxy.session.get(f"{b}/v1/models", timeout=ClientTimeout(total=2.0)) as r:
                backend_status[b] = "ok" if r.status < 400 else f"http_{r.status}"
        except asyncio.TimeoutError:
            backend_status[b] = "timeout"
        except Exception:
            backend_status[b] = "down"
    checks["llm"] = "ok" if any(s == "ok" for s in backend_status.values()) else "down"
    # Wedge visibility: reachability alone reported "ok" through a GPU-driver
    # hang (the accept thread answers while the generation engine is dead).
    # Surface the oldest in-flight age; past the suspect threshold, verify the
    # backend can still answer its own health and flag it when it cannot.
    _now_mono = time.monotonic()
    _ages = {b: _oldest_inflight_age(b, _now_mono) for b in LLM_BACKENDS}
    _max_age = max((a for a in _ages.values() if a is not None), default=None)
    if _max_age is not None:
        checks["llm_oldest_inflight_age_s"] = _max_age
        if _max_age > LLM_WEDGE_SUSPECT_AGE:
            _wedged = []
            for b, a in _ages.items():
                if a is not None and a > LLM_WEDGE_SUSPECT_AGE:
                    if not await _probe_backend_alive(proxy.session, b):
                        _wedged.append(b)
            if _wedged:
                checks["llm_suspect_wedged"] = _wedged
    if len(LLM_BACKENDS) > 1:
        checks["llm_backends"] = backend_status
        if _llm_reserved:
            checks["llm_reserved"] = sorted(_llm_reserved)
        # Parallelisation telemetry: per-backend weight, current in-flight, cumulative
        # routed (check the realised split against weights), total fails, cooldown.
        now = time.monotonic()
        total_routed = sum(_llm_routed.values()) or 1
        checks["llm_pool"] = {
            b: {
                "weight": LLM_WEIGHTS.get(b, 1.0),
                "inflight": _llm_inflight.get(b, 0),
                "routed": _llm_routed.get(b, 0),
                "routed_pct": round(100 * _llm_routed.get(b, 0) / total_routed, 1),
                "fails": _llm_fail_total.get(b, 0),
                "cooldown": round(max(0.0, _llm_unhealthy_until.get(b, 0.0) - now), 1),
                "reserved": b in _llm_reserved,
            }
            for b in LLM_BACKENDS
        }
        # Cache-affinity telemetry: hit rate + which backend holds each hot prefix
        # (so the KV-cache win is observable). hits/(hits+misses) should climb as
        # REM's stable grounding prefix keeps landing on its warm card.
        _aff_total = _llm_affinity_hits + _llm_affinity_misses
        checks["llm_affinity"] = {
            "hits": _llm_affinity_hits,
            "misses": _llm_affinity_misses,
            "hit_rate": round(_llm_affinity_hits / _aff_total, 3) if _aff_total else None,
            "hot_prefixes": {k[:8]: {"backend": v[0], "hits": v[2]}
                             for k, v in _llm_affinity.items()
                             if now - v[1] <= AFFINITY_TTL},
        }

    checks["daemon"]     = "running" if _daemon_healthy else "stopped"
    checks["rem_daemon"] = "running" if _rem_healthy    else "stopped"

    # Version contract — clients compare api_version against their own to detect
    # skew. Cheap string fields; no backend probe. version is informational only.
    checks["version"]     = FRAMEWORK_VERSION
    checks["api_version"] = API_VERSION

    # Effective NON-SECRET configuration the running gateway resolved from the
    # environment — so the live LLM/tuning setup is inspectable via /health
    # without reading .env on the host (and works for a single backend too, where
    # llm_pool above is omitted). Secrets (AGENT_TOKENS, PG/NEO4J passwords) are
    # NEVER echoed here — has_credential is a bool, never the token itself.
    # Tracked regardless of whether any backend actually uses it today, so the
    # capability (external/paid backends, LLM_BACKENDS_JSON) is monitor-visible
    # from the moment it's configured, not only once someone goes looking (fact 898).
    #
    # has_credential/model: confirming "this specific backend has a live paid
    # key loaded right now" is materially more sensitive than a bare URL — a
    # URL alone doesn't confirm a credential is actually attached, and every
    # backend that reaches LLM_BACKENDS at all already has one iff it needed
    # one (an unresolved token_env excludes it from the pool entirely, see
    # _load_llm_backends). Unconditional here (S-10): this whole function's
    # output is now only ever handed to an authenticated caller — see this
    # function's own docstring.
    checks["config"] = {
        "llm_backends": [
            {"url": b, "weight": LLM_WEIGHTS.get(b, 1.0),
             "has_credential": LLM_BACKEND_TOKENS.get(b) is not None,
             "model": LLM_BACKEND_MODELS.get(b)}
            for b in LLM_BACKENDS
        ],
        "llm_pool_tuning": {
            "fail_threshold": LLM_FAIL_THRESHOLD,
            "fail_window_s": LLM_FAIL_WINDOW,
            "cooldown_s": LLM_COOLDOWN,
            "max_tries": LLM_MAX_TRIES,
        },
        "llm_affinity": {
            "prefix_chars": AFFINITY_PREFIX_CHARS,
            "ttl_s": AFFINITY_TTL,
            "max_inflight": AFFINITY_MAX_INFLIGHT,
        },
        "embed_max_chars": int(os.environ.get("EMBED_MAX_CHARS", "24000")),
    }
    # SEC-A5-02 (PR A5 fix round): present ONLY while the S-05 override is
    # actually exposing a live provider key unauthenticated — additive, so
    # a monitor that doesn't know the key renders exactly as before on
    # every other install. See _unauthenticated_provider_keys_override_
    # active's docstring for why this mirrors the startup log.warning.
    if _unauthenticated_provider_keys_override_active():
        checks["config"]["allow_unauthenticated_provider_keys"] = True

    # Embedder and reranker are the critical path — every save and search
    # depends on them.  LLM and daemon degradation is reported but does not
    # fail the health check so agents can still read/write memory.
    critical_ok = checks["embedder"] == "ok" and checks["reranker"] == "ok"
    checks["status"] = "ok" if critical_ok else "degraded"
    # The STARTUP truth (finding 1), not live _AGENT_TOKENS emptiness -- a
    # daemon token minted after boot must not flip this to True for an
    # install that never configured auth. See coordinator.
    # AUTH_CONFIGURED_AT_STARTUP's docstring.
    checks["auth_required"] = AUTH_CONFIGURED_AT_STARTUP
    # Advertise the active auth scheme so clients can detect when the gateway
    # moves from bearer tokens to PoP (asymmetric-key proof-of-possession).
    checks["auth_scheme"] = AUTH_SCHEME
    # True while a backup quiesce is active — the monitor surfaces this as
    # "backup ongoing", and a client seeing it knows write 503s are expected.
    checks["backup_in_progress"] = backup_quiesce_active()

    # Dream-cycle liveness (ADR-018) — cached snapshot from the coordinator
    # (refreshed ~60 s in the background) so /health stays DB-free. stalled=true
    # means an eligible backlog exists but nothing has folded within the stall
    # window and no fold is in-flight — an actionable alert, not a probe miss.
    if coordinator is not None:
        try:
            consolidation = coordinator.consolidation_health()
            checks["consolidation"] = consolidation
            # Top-level inference/GPU-busy signal for the monitor's LLM tile.
            # Tri-state ("busy"|"idle"|"unknown") from the cached snapshot the
            # coordinator probes in the background — /health never shells out to
            # nvtop. "unknown" (nvtop absent / SLOT_AWARE off) is reported verbatim
            # so the monitor shows "unknown", never a false "idle". Distinct from
            # checks["llm"], which is a reachability probe of the configured pool.
            checks["inference_busy"] = consolidation.get("inference_busy", "unknown")
            # Graph integrity is NOT a dream-cycle metric — it counts nodes a
            # write path stored under the wrong label. It rides the same cached
            # snapshot for cheapness, but it is surfaced TOP-LEVEL so a monitor
            # never renders it inside the consolidation tile. None = not yet
            # probed, which must never be read as "verified clean" (decision 928).
            checks["graph_invalid_nodes"] = consolidation.get("graph_invalid_nodes")
            # Project identity (migration 027) — top-level for the same reason:
            # it is an UPGRADE-completeness signal, not a dream-cycle metric. It
            # answers "may this deployment be trusted to fold across projects
            # yet", because the insight gate declines to count a project node
            # that has no registry identity. None = not yet probed, never
            # "complete". An ADDITIVE field: a monitor that does not know it
            # renders exactly as before.
            checks["project_identity"] = consolidation.get("project_identity")
            # Domain identity (migration 028) — the same kind of signal for the
            # sibling axis: registry vs graph, plus whether every section is
            # attached to its project, which is what the cross-domain walk will
            # depend on. Additive; None = not yet probed, never "complete".
            checks["domain_identity"] = consolidation.get("domain_identity")
        except Exception:
            checks["consolidation"] = {"fresh": False}
            checks["inference_busy"] = "unknown"
            checks["graph_invalid_nodes"] = None
            checks["project_identity"] = None
            checks["domain_identity"] = None

    return checks


async def _health_probe_cached(proxy: "AsyncHiveMindProxy", coordinator) -> dict:
    """TTL-cached wrapper around _build_health_checks (S-11), with
    single-flight coalescing on a miss (SEC-A5-05b, PR A5 fix round): the
    TTL alone bounds SEQUENTIAL cost only — N concurrent misses arriving
    together (e.g. a burst of /health hits right as the TTL expires) would
    each observe a stale timestamp and each run the full 2+N-probe fan-out
    before any of them writes back. The lock below makes every concurrent
    miss AWAIT the one probe already in flight: the second-and-later caller
    re-checks the cache immediately after acquiring the lock and finds it
    fresh (the first caller already populated it) — that re-check IS the
    coalescing, not a redundant guard — so only ONE _build_health_checks()
    call ever runs per TTL window regardless of how many callers arrive
    concurrently.

    Anonymous and authenticated callers SHARE this cache — the probe itself
    (embedder/reranker/LLM reachability, daemon liveness, dream-cycle
    snapshot) costs the same regardless of who's asking, and a second probe
    within the TTL window buys nothing; only the RESPONSE SHAPE handle_
    health serves differs per caller, and that projection is applied fresh
    on every call, never cached itself (see _build_health_checks's
    docstring)."""
    now = time.monotonic()
    cached = _health_cache["checks"]
    if cached is not None and now - _health_cache["ts"] < HEALTH_CACHE_TTL_S:
        return cached
    async with _health_probe_lock:
        now = time.monotonic()
        cached = _health_cache["checks"]
        if cached is not None and now - _health_cache["ts"] < HEALTH_CACHE_TTL_S:
            return cached
        checks = await _build_health_checks(proxy, coordinator)
        _health_cache["checks"] = checks
        _health_cache["ts"] = now
        return checks


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — liveness for everyone; the full operational payload
    (backend roster, per-backend pool state, capability probes, daemon/
    dream-cycle detail) only for a caller presenting a valid agent bearer
    token ON AN AUTH-CONFIGURED INSTALL (S-10, Credential_Custody_Plan
    PR A5). That detail is operational information about this deployment's
    infrastructure, not something every unauthenticated network peer should
    learn just by asking.

    SEC-A5-03 (PR A5 fix round): slimming applies ONLY when
    AUTH_CONFIGURED_AT_STARTUP is true. An auth-off install has no token
    registry at all — `resolve_identity()` can never match ANY presented
    token against an empty `_AGENT_TOKENS`, so gating on bare
    `resolve_identity()` made such an install "always anonymous, with no
    reachable alternative": its monitor, `doctor`, and every `/health`
    triage command would have silently and PERMANENTLY lost the full
    payload with no token able to restore it. There is also nothing on such
    an install for the slimming to protect — S-05 guarantees an auth-off
    install has no LIVE provider key unless it took the explicit override
    (and that override is itself now surfaced in the full payload — see
    `_unauthenticated_provider_keys_override_active`), so an auth-off
    install keeps today's full payload unconditionally, exactly as before
    this branch.

    Anonymous shape on an auth-configured install: {"status", "version",
    "api_version"} — exactly what memory_bridge.py's `doctor` parses
    (check_gateway_compat() reads only these three keys), so `doctor` and
    any liveness-only poller keep working unchanged. Authenticated shape
    (or ANY caller on an auth-off install): today's full payload,
    byte-compatible.

    HTTP 200: embedder + reranker both reachable (save/search path healthy).
    HTTP 503: at least one critical backend is down — computed identically
    for every caller; an anonymous caller learns the VERDICT, not why.
    """
    proxy: AsyncHiveMindProxy = request.app["proxy"]
    checks = await _health_probe_cached(proxy, request.app.get("coordinator"))
    critical_ok = checks["status"] == "ok"
    status_code = 200 if critical_ok else 503

    if AUTH_CONFIGURED_AT_STARTUP and not bool(_safe_resolve_identity(request)):
        return web.json_response(
            {"status": checks["status"], "version": checks["version"],
             "api_version": checks["api_version"]},
            status=status_code,
        )
    return web.json_response(checks, status=status_code)


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #
def _unauthenticated_provider_keys_override_active() -> bool:
    """True iff this process is running with the S-05 override ACTUALLY in
    effect — auth off, a provider key attached to a configured backend, and
    the operator set ALLOW_UNAUTHENTICATED_PROVIDER_KEYS (SEC-A5-02, PR A5
    fix round). Read fresh every call (never cached) so it always reflects
    the live env/config rather than a value captured once at import time.
    Shared by require_auth_when_provider_keys_configured() below (decides
    warn-vs-raise) and _build_health_checks() (surfaces the condition on
    the authenticated /health payload so it stays MONITORABLE for the
    gateway's whole lifetime, not just greppable in a boot log that
    rotates)."""
    if AUTH_CONFIGURED_AT_STARTUP:
        return False
    if not any(LLM_BACKEND_TOKENS.get(b) for b in LLM_BACKENDS):
        return False
    return os.environ.get("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", "").strip().lower() in ("1", "true", "yes", "on")


def require_auth_when_provider_keys_configured() -> None:
    """S-05 (Required, RULED — decision:1303, PR A5): AGENT_TOKENS unset
    disables auth AND the in-flight cap AND the audit path (see coordinator.
    auth_middleware's AUTH_CONFIGURED_AT_STARTUP early-return) while any
    provider key stays attached to whatever backend it's configured for — so
    an auth-unset install with a credentialed backend lets ANY network peer
    that can reach the gateway sign a request with that key. Refuses to
    start rather than run that way.

    Two ways out, both named in the error: configure AGENT_TOKENS, or set
    ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1 — an explicit, deliberate override
    for a deployment that has decided the risk is acceptable (documented in
    .env.example with a warning).

    REFINED invariant, not a narrowed one: an auth-unset install with NO
    provider-credentialed backend configured — the original backward-compat
    population, e.g. a bare local llama-server — is completely unaffected;
    this only gates the NEW combination of auth-off *and* a live provider
    key. Call from main() ONLY (the real entrypoint), same placement
    reasoning as require_no_plaintext_agent_tokens(): every test in this
    repo imports this module freely, many with AUTH_CONFIGURED_AT_STARTUP
    False on purpose, so an unconditional check here would kill test
    collection itself, not just a genuinely misconfigured gateway.
    """
    if AUTH_CONFIGURED_AT_STARTUP:
        return
    credentialed = sorted(b for b in LLM_BACKENDS if LLM_BACKEND_TOKENS.get(b))
    if not credentialed:
        return
    if os.environ.get("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", "").strip().lower() in ("1", "true", "yes", "on"):
        # SEC-A5-02 (PR A5 fix round): this branch used to be a bare
        # `return` — no log line, no telemetry, no /health field. Six
        # months later there was no artefact anywhere that would let anyone
        # discover the gateway was running as an unauthenticated proxy
        # signing requests with a live provider key short of re-reading
        # .env. Loud now: this log.warning at startup, PLUS a flat additive
        # field on the authenticated /health config block (see
        # _unauthenticated_provider_keys_override_active, used by
        # _build_health_checks) so the condition stays visible for the
        # gateway's whole lifetime, not just the moment it boots.
        log.warning(
            "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS is set — starting UNAUTHENTICATED "
            "with a live provider key attached to %d backend(s): %s. Any caller "
            "that can reach this gateway can sign a request with that key. This is "
            "the deliberate override documented in shared-memory/.env.example, not "
            "a default — also visible on GET /health as "
            "config.allow_unauthenticated_provider_keys once the gateway is up.",
            len(credentialed), ", ".join(credentialed),
        )
        return
    raise SystemExit(
        "FATAL: AGENT_TOKENS is unset but a provider-credentialed backend is "
        f"configured ({', '.join(credentialed)}) — starting would let any "
        "caller sign a request with that key. Configure AGENT_TOKENS, or set "
        "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1 to run anyway (see "
        "shared-memory/.env.example)."
    )


def _default_uds_path() -> str:
    """Per-user runtime socket by default (0700 dir → only this user reaches it,
    which is exactly right for a single-user box). For a multi-user gateway set
    GATEWAY_UDS_PATH to a shared location and widen GATEWAY_UDS_MODE."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, "shared-memory-gw.sock")


async def main() -> None:
    # RULED (Xenofon, 2026-08-14): a plaintext AGENT_TOKENS entry refuses
    # gateway startup outright, from v0.9.3 — before anything else stands
    # up. See coordinator.require_no_plaintext_agent_tokens()'s docstring
    # for why this call lives here (the real entrypoint) and nowhere else.
    require_no_plaintext_agent_tokens()
    # S-05 (RULED — decision:1303): auth-off + a live provider key also
    # refuses to start — see require_auth_when_provider_keys_configured()'s
    # docstring for why this call lives here too.
    require_auth_when_provider_keys_configured()

    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8888

    proxy = AsyncHiveMindProxy()
    await proxy.start_session()

    coordinator = MemoryCoordinator()
    await coordinator.start()

    # 50 MB ceiling applies to requests buffered via request.read().
    # The streaming path (request.content) bypasses this — see handle_proxy.
    app = web.Application(client_max_size=50 * 1024 * 1024, middlewares=[auth_middleware])
    app["proxy"] = proxy  # shared with health handler
    app["coordinator"] = coordinator  # health reads the cached consolidation snapshot

    # Coordinator routes and health endpoint before the catch-all proxy route.
    attach_coordinator(app, coordinator)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/pool/status", handle_pool_status)
    app.router.add_route("*", "/{tail:.*}", proxy.handle_proxy)

    runner = web.AppRunner(app)
    await runner.setup()
    # Bind to localhost by default. Set PROXY_BIND=0.0.0.0 to opt into
    # all-interfaces binding — only safe over an encrypted overlay network
    # (Tailscale, WireGuard) or behind TLS. Bearer tokens are plaintext over HTTP.
    bind_host = os.environ.get("PROXY_BIND", "127.0.0.1")
    site = web.TCPSite(runner, bind_host, PORT)
    await site.start()

    # AF_UNIX listener for kernel-attested person identity (SO_PEERCRED). Local
    # agents and SSH-forwarded Unix sockets connect here so the gateway reads the
    # operator's OS account straight from the kernel — see coordinator._peer_identity.
    # The TCP listener stays up for back-compat (no principal on that path). Disable
    # by setting GATEWAY_UDS_PATH="".
    uds_site = None
    uds_path = os.environ.get("GATEWAY_UDS_PATH")
    if uds_path is None:
        uds_path = _default_uds_path()
    if uds_path:
        try:
            if os.path.exists(uds_path):
                os.unlink(uds_path)          # clear a stale socket from a prior run
            uds_site = web.UnixSite(runner, uds_path)
            await uds_site.start()
            os.chmod(uds_path, int(os.environ.get("GATEWAY_UDS_MODE", "0600"), 8))
            log.info("### Hive-Mind Proxy on unix:%s [SO_PEERCRED principal]", uds_path)
        except (OSError, ValueError) as exc:
            log.warning("UDS listener disabled (%s): %s", uds_path, exc)
            uds_site = None

    log.info("### Hive-Mind Proxy on :%d [aiohttp]", PORT)
    log.info("### /v1/embeddings->8070 | /v1/reranking->8071 | default->5000")

    stop_event = asyncio.Event()
    watchdog_task     = asyncio.create_task(_watchdog_daemon(stop_event))
    rem_watchdog_task = asyncio.create_task(_watchdog_rem_daemon(stop_event))
    # Backend capability probe — measures whether the critical backends can
    # actually SERVE, not merely whether they answer /health.
    capability_task   = asyncio.create_task(
        _capability_probe_daemon(proxy, stop_event))
    loop = asyncio.get_running_loop()

    def _on_shutdown_signal():
        log.info("Termination signal received — initiating drain sequence...")
        stop_event.set()
        # Remove handlers immediately so a second Ctrl+C falls back to the default
        # Python KeyboardInterrupt handler, giving the operator an emergency hard-abort
        # if the drain stalls on a hung backend connection.
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(s)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_shutdown_signal)

    await stop_event.wait()

    # Drain sequence — order is load-bearing:
    # 1. site.stop()      — close the listen socket; no new connections accepted
    # 2. runner.cleanup() — wait for in-flight requests to finish
    # 3. terminate daemon — unblocks watchdog's proc.wait()
    # 4. watchdog_task    — wait for watchdog to confirm it has exited
    # 5. coordinator/proxy cleanup last
    log.info("Stopping listener...")
    await site.stop()
    if uds_site is not None:
        await uds_site.stop()
    log.info("Draining in-flight requests...")
    await runner.cleanup()
    if _daemon_proc and _daemon_proc.returncode is None:
        log.info("Stopping consolidation daemon (pid %d)...", _daemon_proc.pid)
        _daemon_proc.terminate()
        try:
            await asyncio.wait_for(_daemon_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("Consolidation daemon did not exit in 5 s — sending SIGKILL")
            _daemon_proc.kill()
    if _rem_proc and _rem_proc.returncode is None:
        log.info("Stopping REM daemon (pid %d)...", _rem_proc.pid)
        _rem_proc.terminate()
        try:
            await asyncio.wait_for(_rem_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("REM daemon did not exit in 5 s — sending SIGKILL")
            _rem_proc.kill()
    watchdog_task.cancel()
    rem_watchdog_task.cancel()
    capability_task.cancel()
    for task in (watchdog_task, rem_watchdog_task, capability_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await coordinator.stop()
    await proxy.cleanup()
    log.info("Clean shutdown complete.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Emergency halt via KeyboardInterrupt.")
