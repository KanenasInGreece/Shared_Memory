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
from log_hygiene import append_secure, secure_path, scrub_url_credentials, FILE_MODE  # noqa: E402
# A-4: the ROLE RULE, from the module that owns it — never a bare _AGENT_ROLES
# lookup, which cannot see read_only_agents() and would report a confined
# identity as write-capable.
from agent_roles import effective_role  # noqa: E402
# M9 (fix round): _chmod_created_ancestors is a private log_hygiene member --
# the only thing this module needs from it that has no public equivalent
# (secure_path's own dir-hardening is entangled with its append-mode open,
# which _write_capacity_records_sync's atomic-replace path doesn't want).
# Imported here (module top, not per-call as it was) so a future log_hygiene
# refactor that drops or renames it fails LOUDLY at gateway startup instead
# of silently at the first capacity-log write.
try:
    from log_hygiene import _chmod_created_ancestors  # noqa: E402
except ImportError:  # pragma: no cover -- defensive against a log_hygiene refactor
    logging.getLogger("hive-proxy").critical(
        "log_hygiene._chmod_created_ancestors is no longer importable -- "
        "capacity-log parent directories will NOT be hardened to 0700 on "
        "first creation; update hive_mind_proxy.py's import to match "
        "log_hygiene's current internals")

    def _chmod_created_ancestors(dir_path):  # type: ignore[no-redef]
        pass

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
    _decompress_full_for_usage,
    SUPPORTED_CONTENT_ENCODINGS,
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
# `or`, not a get() default: an EMPTY value (EMBEDDER_URL= in .env) means "the
# default", the same reading the coordinator gives the same variable — the two
# consumers must never disagree on where the encoder is.
EMBEDDER_URL = (os.environ.get("EMBEDDER_URL") or "http://localhost:8070").strip().rstrip("/")
RERANKER_URL = (os.environ.get("RERANKER_URL") or "http://localhost:8071").strip().rstrip("/")
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
# fact:1535 (route-guard): a mistyped or wrong-method framework request must
# FAIL AND SAY WHY, not fall through the catch-all into a reasoning-LLM
# dispatch (mcp/vector-skill.py's review_edges did exactly this — GET where
# the gateway registers POST-only — and the request was silently forwarded
# to the LLM pool as if it were a chat completion). RESERVED_ROUTE_PREFIXES
# is the ONLY hand-written table this guard uses: which path namespaces are
# framework-owned, so an unrecognised path under one of them is a mistyped
# framework call (404) rather than a legitimate LLM-passthrough path (which
# is everything NOT under these prefixes, and keeps today's behaviour). The
# actual known ROUTES — and their allowed methods — are never hand-written;
# see AsyncHiveMindProxy.set_known_routes(), which derives them from the
# app router itself (decision:1032 class — never write what can be derived).
RESERVED_ROUTE_PREFIXES = ("/memory/", "/admin/")
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


# Model-attributes routing (Model_Attributes_Routing_Plan_2026-08-18, REVISED
# DESIGN). Descriptor schema, additive to the existing url/weight/token_env/
# model/extra_body fields:
#   roles          list from {extract, verify, judge} ("summarize" is
#                  RESERVED — NREM narrative folds are zero-inference by
#                  construction, the only NREM LLM path is insight folds =
#                  "judge"). Absent = serves all (homogeneous-fleet degenerate
#                  case, every existing install unchanged).
#   n_ctx          int, the model's usable context in tokens. Absent = no fit
#                  information (this backend always "fits").
#   private_ok     bool. Default = (no token_env present) — an uncredentialed
#                  (local) backend defaults True, a provider-credentialed one
#                  defaults False. An EXPLICIT value always wins either way.
#   max_inflight   int, per-backend concurrency ceiling. Absent = unbounded
#                  (today's behavior).
#   price_per_mtok_in / price_per_mtok_out — optional operator metadata,
#                  stored + surfaced on /health for the MONITOR to multiply.
#                  NEVER used in any routing decision here — the gateway
#                  stays price-agnostic (M-4 honesty note).
ROUTING_ROLE_NAMES = frozenset({"extract", "verify", "judge"})
RESERVED_ROLE_NAMES = frozenset({"summarize"})


def _load_llm_backends() -> tuple[
        list[str], dict[str, float], dict[str, "str | None"], dict[str, "str | None"],
        dict[str, "dict | None"], dict[str, "frozenset[str] | None"], dict[str, "int | None"],
        dict[str, bool], dict[str, bool], dict[str, "int | None"],
        dict[str, "float | None"], dict[str, "float | None"], list[str]]:
    """Returns (urls, weights, tokens, models, extras, roles, n_ctx, private_ok,
    private_ok_explicit, max_inflight, price_in, price_out, role_config_errors).

    `role_config_errors` collects a human-readable message per backend whose
    `roles` list names something outside ROUTING_ROLE_NAMES — collected here
    (module import time, so every test that imports this module freely still
    collects cleanly) rather than raised here. The actual SystemExit lives in
    require_valid_llm_routing_config(), called from main() ONLY — same
    placement reasoning as require_auth_when_provider_keys_configured()."""
    role_config_errors: list[str] = []

    def _parse_roles(url: str, raw) -> "frozenset[str] | None":
        if raw is None:
            return None
        if not isinstance(raw, list):
            role_config_errors.append(
                f"{url}: roles must be a JSON array, got {type(raw).__name__}")
            return frozenset()
        if not raw:
            # R-3 (decision:1357): an EMPTY roles list makes the backend
            # eligible for nothing — every request 422s — and it must not
            # sidestep the M-5 explicit-choice refusal (which tests `is
            # None`). A backend that serves nothing is a config mistake, not
            # a scope; refuse loudly at startup.
            role_config_errors.append(
                f"{url}: roles is an EMPTY list — this backend would be "
                f"eligible for NOTHING (every request refused). Either omit "
                f"`roles` (serves all) or list at least one of "
                f"{sorted(ROUTING_ROLE_NAMES)}.")
            return frozenset()
        names = {str(r).strip().lower() for r in raw}
        bad = names - ROUTING_ROLE_NAMES
        if bad:
            reserved_hit = bad & RESERVED_ROLE_NAMES
            if reserved_hit:
                role_config_errors.append(
                    f"{url}: roles names {sorted(reserved_hit)} — RESERVED, not "
                    f"accepted (NREM narrative folds are zero-inference; the only "
                    f"NREM LLM path is judge). Allowed: {sorted(ROUTING_ROLE_NAMES)}")
            unknown = bad - RESERVED_ROLE_NAMES
            if unknown:
                role_config_errors.append(
                    f"{url}: unknown role name(s) {sorted(unknown)} — allowed: "
                    f"{sorted(ROUTING_ROLE_NAMES)}")
        return frozenset(names & ROUTING_ROLE_NAMES)

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
        roles: dict[str, "frozenset[str] | None"] = {}
        n_ctxs: dict[str, "int | None"] = {}
        private_oks: dict[str, bool] = {}
        private_ok_explicit: dict[str, bool] = {}
        max_inflights: dict[str, "int | None"] = {}
        price_ins: dict[str, "float | None"] = {}
        price_outs: dict[str, "float | None"] = {}
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
                    # Wording covers BOTH causes of a falsy value, because
                    # v0.9.63 gave this branch a second one: unset (nothing
                    # anywhere resolves the name) and REFUSED (a `_FILE` /
                    # $CREDENTIALS_DIRECTORY secret was found but rejected —
                    # over the size cap, empty, or holding a control
                    # character). Saying only "not set" sends an operator
                    # whose key file IS present looking for a missing export
                    # instead of at the [secure_env] line that names the file.
                    log.warning(
                        "LLM backend %s configured with token_env=%s but that "
                        "variable did not resolve to a usable secret (unset, or "
                        "refused by secure_env — see the [secure_env] WARNING "
                        "above) — excluding this backend from the pool.",
                        url, token_env)
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
            # R-2 + Optional (decision:1357): both int fields must be a REAL
            # int >= 1 — bool is an int subclass in Python (True passes a
            # bare isinstance check), and 0/negative values are traps:
            # max_inflight=0 makes the backend permanently at-cap (every
            # request waits the full window then 503s), n_ctx=0 silently
            # disables the fit check via `if not n_ctx`.
            n_ctx_raw = entry.get("n_ctx")
            if n_ctx_raw is not None and (not isinstance(n_ctx_raw, int)
                                          or isinstance(n_ctx_raw, bool)
                                          or n_ctx_raw < 1):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has an invalid n_ctx "
                    "(%r — must be an integer >= 1) — excluding this backend "
                    "from the pool.", url, n_ctx_raw)
                continue
            max_inflight_raw = entry.get("max_inflight")
            if max_inflight_raw is not None and (not isinstance(max_inflight_raw, int)
                                                 or isinstance(max_inflight_raw, bool)
                                                 or max_inflight_raw < 1):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has an invalid max_inflight "
                    "(%r — must be an integer >= 1) — excluding this backend "
                    "from the pool.", url, max_inflight_raw)
                continue
            private_ok_raw = entry.get("private_ok")
            if private_ok_raw is not None and not isinstance(private_ok_raw, bool):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has a non-boolean private_ok "
                    "(%r) — excluding this backend from the pool.",
                    url, private_ok_raw)
                continue
            urls.append(url)
            weights[url] = max(float(entry.get("weight", 1.0) or 1.0), 0.1)
            tokens[url] = token
            models[url] = entry.get("model") or None
            extras[url] = extra_body or None
            roles[url] = _parse_roles(url, entry.get("roles"))
            n_ctxs[url] = n_ctx_raw
            max_inflights[url] = max_inflight_raw
            # Default = (no token_env present): uncredentialed/local backend
            # defaults True, provider-credentialed defaults False. Explicit
            # value (private_ok_raw is not None) always wins either way.
            private_ok_explicit[url] = private_ok_raw is not None
            private_oks[url] = private_ok_raw if private_ok_raw is not None else (token is None)
            price_ins[url] = entry.get("price_per_mtok_in")
            price_outs[url] = entry.get("price_per_mtok_out")
        if urls:
            return (urls, weights, tokens, models, extras, roles, n_ctxs, private_oks,
                    private_ok_explicit, max_inflights, price_ins, price_outs, role_config_errors)
        log.error("LLM_BACKENDS_JSON produced no usable backend — falling back to LLM_BACKENDS/LLM_DEFAULT_TARGET")

    _raw_backends = [_parse_backend(e) for e in os.environ.get("LLM_BACKENDS", "").split(",") if e.strip()]
    if not _raw_backends:
        _raw_backends = [(DEFAULT_TARGET, 1.0)]
    urls = [u for u, _ in _raw_backends]
    weights = {u: w for u, w in _raw_backends}
    # Legacy comma form (and the DEFAULT_TARGET fallback) never carries a
    # credential, so every backend it produces defaults private_ok=True,
    # roles absent (serves-all) — I-5a: byte-identical to v0.9.12 selection.
    return (urls, weights, {u: None for u in urls}, {u: None for u in urls}, {u: None for u in urls},
            {u: None for u in urls}, {u: None for u in urls}, {u: True for u in urls},
            {u: False for u in urls}, {u: None for u in urls}, {u: None for u in urls},
            {u: None for u in urls}, [])


LLM_BACKENDS: list[str]
LLM_WEIGHTS: dict[str, float]
LLM_BACKEND_TOKENS: dict[str, "str | None"]
LLM_BACKEND_MODELS: dict[str, "str | None"]
LLM_BACKEND_ROLES: dict[str, "frozenset[str] | None"]
LLM_BACKEND_NCTX: dict[str, "int | None"]
LLM_BACKEND_PRIVATE_OK: dict[str, bool]
LLM_BACKEND_PRIVATE_OK_EXPLICIT: dict[str, bool]
LLM_BACKEND_MAX_INFLIGHT: dict[str, "int | None"]
LLM_BACKEND_PRICE_IN: dict[str, "float | None"]
LLM_BACKEND_PRICE_OUT: dict[str, "float | None"]
(LLM_BACKENDS, LLM_WEIGHTS, LLM_BACKEND_TOKENS, LLM_BACKEND_MODELS, LLM_BACKEND_EXTRAS,
 LLM_BACKEND_ROLES, LLM_BACKEND_NCTX, LLM_BACKEND_PRIVATE_OK, LLM_BACKEND_PRIVATE_OK_EXPLICIT,
 LLM_BACKEND_MAX_INFLIGHT, LLM_BACKEND_PRICE_IN, LLM_BACKEND_PRICE_OUT,
 _LLM_BACKEND_ROLE_CONFIG_ERRORS) = _load_llm_backends()


def _apply_backend_body_overrides(body: bytes, model: "str | None",
                                  extra: "dict | None",
                                  _body_obj: "dict | None" = None) -> bytes:
    """The request body as this backend must receive it.

    Pure so it can be tested (and mutation-checked) without the proxy plumbing.
    `extra` (the backend's extra_body config) is merged first and overrides the
    caller — it is the operator's per-backend truth, and the callers are our own
    daemons sending one homogeneous request shape. The `model` override is
    applied last and only when the caller sent a model field, preserving the
    long-standing rewrite contract — so an explicit per-backend model always
    beats an extra_body["model"] left there by mistake. Best-effort by design:
    an unparseable or non-object body is forwarded unchanged rather than
    dropped.

    `_body_obj` (A-3, "parse the body once"): handle_proxy already parses the
    body once for affinity/fit — pass that SAME dict through here to skip a
    second json.loads of identical bytes. A shallow copy is taken before any
    mutation so the caller's own parsed struct (read earlier, for affinity/
    fit) is never touched by this step. None (the default, and every direct
    unit-test call in this repo) parses `body` itself, unchanged from before."""
    if not model and not extra:
        return body
    if _body_obj is not None:
        payload = dict(_body_obj)
    else:
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

# ── Fit check (A-1/A-2/N-1/N-3, Model_Attributes_Routing_Plan_2026-08-18) ────
# est_prompt_tokens = body_chars / CHARS_PER_TOKEN_RATIO, computed GATEWAY-side
# from the already-buffered request body char count (no per-message chat-
# template simulation — deliberately simple, see N-1). MEASURED (builder,
# v0.9.13, HANDOFF.md): 20 live /tokenize comparisons against BOTH local
# backends (localhost:5000, localhost:4000 — same Qwen3-14B model, identical
# results) across prose/JSON/code/SQL/Greek-text samples gave chars/token
# ranging 1.205 (Greek, the densest) to 6.905 (English prose, the sparsest).
# 1.2 is that measured floor, rounded down slightly — the MOST CONSERVATIVE
# (highest tokens-per-char) observed ratio, per N-3, so est_prompt_tokens
# never UNDER-counts for any sampled content type. A live chat-completion
# check (4 real message-array bodies, max_tokens=1) confirmed this floor
# already overestimates true chat-template prompt_n by 83%-525% in every
# sample — i.e. RATIO's own conservatism, not FIT_MARGIN, is what protects
# against under-counting; FIT_MARGIN is the residual buffer below.
CHARS_PER_TOKEN_RATIO = float(os.environ.get("LLM_CHARS_PER_TOKEN_RATIO", "1.2"))
# Fraction of n_ctx held back as headroom: eligible iff est_prompt_tokens +
# effective_max_tokens <= n_ctx * (1 - FIT_MARGIN). MEASURED (builder,
# v0.9.13): since CHARS_PER_TOKEN_RATIO's own conservatism already
# overestimates real prompt tokens by 83%-525% against the live samples
# above, there is no measured within-sample under-count for this margin to
# cover — its job is the residual, UNMEASURED risk of content denser than
# anything sampled (heavy CJK/emoji, deeply repeated JSON keys) plus general
# n_ctx bookkeeping slop (special/BOS-EOS tokens, KV overhead) this estimator
# does not model. 10% is a modest, deliberately non-zero buffer for that
# unmeasured residual — flagged, not derived from a further live measurement
# (see HANDOFF.md; N-3 names per-family ratios as the escalation if this
# proves insufficient in practice).
FIT_MARGIN = float(os.environ.get("FIT_MARGIN", "0.10"))
# Reserved output budget when the caller's body has no max_tokens at all.
# UNMEASURED (flagged in .env.example per fact:1338) — conservative round
# number, not derived from a live measurement; the daemons' own task-owned
# budgets (REM_MAX_TOKENS_*, decision:1330) are the actually-measured ceiling
# for real dream traffic, this is only the estimator's fallback when a caller
# sends none at all.
FIT_DEFAULT_OUTPUT_TOKENS = int(os.environ.get("FIT_DEFAULT_OUTPUT_TOKENS", "2048"))
# A backend AT its max_inflight cap counts as busy in selection (I-8); when
# every ELIGIBLE backend is at its cap, the request WAITS on it rather than
# widening eligibility or picking an over-cap backend (the cap never widens
# eligibility) — bounded so a permanently-saturated single-backend
# configuration still fails eventually instead of hanging a request forever.
LLM_MAX_INFLIGHT_WAIT_S = float(os.environ.get("LLM_MAX_INFLIGHT_WAIT_S", "120"))
# Floor of 0.05s: a configured 0 would busy-spin the event loop for the whole
# wait window (Optional finding, decision:1357).
LLM_MAX_INFLIGHT_POLL_S = max(0.05, float(os.environ.get("LLM_MAX_INFLIGHT_POLL_S", "0.5")))
# R-4 (decision:1357): how many requests may HOLD a capacity-wait slot at
# once. Every waiter occupies an admitted request for up to the full wait
# window (and, with GATEWAY_INFLIGHT_MAX set, counts toward the gateway-wide
# S-11 shed) — unbounded waiters let one capped backend starve the whole
# gateway. Beyond this many concurrent waiters a request gets an immediate
# 503 backend_at_capacity instead of waiting. ⚠ UNMEASURED default
# (fact:1338 — flagged in .env.example): a small bound chosen for its shape
# (strictly less than any plausible S-11 admission budget), not derived from
# a live measurement.
# Floor of 1 (FR-4, delta re-review): 0/negative would mean nothing ever
# waits — the same invalid-int class the descriptor fields now reject.
LLM_MAX_CAPACITY_WAITERS = max(1, int(os.environ.get("LLM_MAX_CAPACITY_WAITERS", "8")))

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


def _v1_models_probe_url(backend_base: str) -> str:
    """H-3 (Model_Attributes_Routing_Plan_2026-08-18): the /v1/models
    liveness-probe URL for a backend base, without doubling /v1 when the
    configured base ALREADY includes it. LLM_BACKENDS_JSON's own documented
    cloud-base shape is "https://api.deepseek.com/v1" — naively concatenating
    "/v1/models" onto that probes ".../v1/v1/models", a malformed URL.

    Verified live (builder, v0.9.13): DeepSeek's edge auth governor returns
    401 uniformly for ANY path under its domain (confirmed against a bogus
    path too — same 401), so the doubled and correct forms were
    indistinguishable through DeepSeek specifically; this fix is not
    validated as observably behavior-changing for that one provider, but the
    doubled URL is a real construction defect regardless of what any single
    provider's auth gate happens to do with it — a path-sensitive gate on a
    different provider would answer these two URLs differently."""
    base = backend_base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _upstream_url(target_base: str, rel_url) -> str:
    """Join a backend base to the incoming request path WITHOUT doubling /v1.

    Every OpenAI-compatible cloud provider publishes a base that already ends
    in /v1 (our own .env.example ships exactly that for DeepSeek), while the
    daemons address this gateway at /v1/chat/completions. A naive concat
    therefore produced /v1/v1/chat/completions, which providers answer with
    404 -- measured live: 404 on the doubled path, 200 on the correct one with
    the same key. It failed SILENTLY: a 404 is never billed, so neither the
    token counters nor the provider dashboard showed anything, while /health
    reported the backend ok and REM retried every 30 s forever.

    _v1_models_probe_url already de-duplicated this for the PROBE path; the
    judgement that it was "not observably behavior-changing" was formed there
    and does not carry to the work path, where the path is decisive. This is
    the join every proxied route goes through, so embedder/reranker bases
    ending in /v1 are covered too, not just chat.

    Pure and total so a mutation check can bite it."""
    base = str(target_base).rstrip("/")
    rel = str(rel_url)
    if base.endswith("/v1") and rel.startswith("/v1/"):
        rel = rel[len("/v1"):]
    return f"{base}{rel}"


async def _probe_backend_alive(session, backend: str) -> bool:
    """2s liveness probe of the backend's own health surface. llama.cpp serves
    /health; OpenAI-compatible fallback is /v1/models. True = answered."""
    for path in ("/health", None):
        try:
            url = f"{backend}{path}" if path else _v1_models_probe_url(backend)
            async with session.get(url, timeout=ClientTimeout(total=2.0)) as r:
                # 404 means "not served HERE", never "this backend is down" --
                # so fall through to the next candidate rather than accepting
                # it. Accepting 404 is what let a backend whose every real
                # call 404s report itself ok on /health while REM looped on it
                # for 45 minutes (measured, fresh Debian 13 install).
                if r.status == 404:
                    continue
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
# Routing telemetry (Group 3, fact:1314 shape — flat counters, each paired
# with its own last-event ts; additive, no existing key changes meaning).
_llm_routed_by_role: dict[str, int] = {r: 0 for r in ROUTING_ROLE_NAMES}
_llm_routed_by_role_last_ts: dict[str, "str | None"] = {r: None for r in ROUTING_ROLE_NAMES}
_routing_no_eligible_backend_count = 0
_routing_no_eligible_backend_last_ts: "str | None" = None
_routing_fit_rejected_count = 0
_routing_fit_rejected_last_ts: "str | None" = None
# R-1 (decision:1357): the 503 backend_at_capacity refusal gets its OWN
# counter + last-ts pair — without it a saturated capped backend stalls
# every record while /health shows zero refusals (the 422 counters never
# fire on this path).
_routing_backend_at_capacity_count = 0
_routing_backend_at_capacity_last_ts: "str | None" = None
# R-4 (decision:1357): live count of requests currently holding a
# capacity-wait slot — bounded by LLM_MAX_CAPACITY_WAITERS above.
_capacity_waiters = 0
# Optional (decision:1357): unknown X-SM-LLM-Role values seen from
# steer-permitted callers, warned ONCE per distinct value — a typo'd role
# silently degrades to role-less eligibility and is otherwise invisible.
_warned_unknown_role_values: set[str] = set()
# Per-backend token accounting (post-review addition A). IN-PROCESS only —
# reset on restart, deliberate, same semantics as every other gateway
# counter; the paired last-ts is what makes a restart-aware delta
# computable. Parsed from `usage` on a non-streaming proxied LLM response;
# a parse failure skips the counter and never breaks the proxy path.
_llm_tokens_prompt_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_tokens_completion_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_tokens_last_ts: dict[str, "str | None"] = {b: None for b in LLM_BACKENDS}
# Per-backend LLM request latency (new instrument, single-cloud-fleet
# debugging cycle): nothing recorded per-request latency, so a local-vs-
# online backend comparison had no data. Measured with time.monotonic()
# around the FULL proxied upstream exchange — request sent through response
# body fully drained (write_eof or the terminal disconnect/cancellation
# branch) — for pool-routed LLM requests only (llm_backend is not None;
# embeddings/reranking never touch these dicts). Recorded for success AND
# failure, with failures (status >= 400 or an exception) counted separately
# so the average isn't diluted by them. IN-PROCESS only — reset on restart,
# same lifecycle as the token counters above; the paired last-ts is what
# makes a restart-aware delta computable.
_llm_requests_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_requests_failed_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_latency_sum_s: dict[str, float] = {b: 0.0 for b in LLM_BACKENDS}
_llm_latency_max_s: dict[str, float] = {b: 0.0 for b in LLM_BACKENDS}
_llm_latency_last_ts: dict[str, "str | None"] = {b: None for b in LLM_BACKENDS}
# Bounds how much response body the usage-parsing peek accumulates for a
# single LLM call — a streamed multi-megabyte completion must never be held
# fully in memory just to look for a trailing `usage` object. Capture is
# abandoned (not attempted) once this is exceeded. Applies to the COMPRESSED
# bytes accumulated for a gzip/deflate/br response too (see
# _decompress_full_for_usage in coordinator.py) — the memory bound is the
# point, and decompression happens once, after the loop, on the
# already-cap-bounded accumulated body.
LLM_USAGE_CAPTURE_CAP_BYTES = int(os.environ.get("LLM_USAGE_CAPTURE_CAP_BYTES", str(2 * 1024 * 1024)))
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


def _parse_json_body(body: bytes) -> "dict | None":
    """The request body parsed ONCE (Model_Attributes_Routing_Plan_2026-08-18
    A-3: "affinity, overrides, and fit all read that struct; never a second
    json.loads of the same body"). Returns a dict, or None for anything that
    doesn't parse to a JSON object — every caller below treats None exactly
    like "nothing usable here", never raises."""
    try:
        obj = json.loads(body)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _affinity_key_from_obj(body_obj: "dict | None") -> str | None:
    """sha1 of the leading AFFINITY_PREFIX_CHARS of the concatenated message
    content — identifies requests sharing a large prompt prefix (the KV-cache
    unit). None if there is no parsed body or no message content."""
    try:
        msgs = (body_obj or {}).get("messages") or []
        text = "".join(str(m.get("content", "")) for m in msgs)
        if not text:
            return None
        return hashlib.sha1(text[:AFFINITY_PREFIX_CHARS].encode("utf-8", "ignore")).hexdigest()
    except Exception:
        return None


def _affinity_key(body: bytes) -> str | None:
    """Thin bytes-in wrapper over _affinity_key_from_obj, kept for direct
    unit-testability (tests/test_llm_affinity.py calls this with raw bytes)
    and any caller that has not already parsed the body itself. handle_proxy
    calls _affinity_key_from_obj directly against its own single parse."""
    return _affinity_key_from_obj(_parse_json_body(body))


def _extract_effective_max_tokens(body_obj: "dict | None") -> float:
    """The OUTPUT budget the fit check must reserve headroom for: the
    caller's own `max_tokens` when present and a positive number, else
    FIT_DEFAULT_OUTPUT_TOKENS (I-3: this is read-only sizing information for
    the fit check — it is never written back into the request body)."""
    if isinstance(body_obj, dict):
        mt = body_obj.get("max_tokens")
        if isinstance(mt, (int, float)) and mt > 0:
            return float(mt)
    return float(FIT_DEFAULT_OUTPUT_TOKENS)


def _serves_all(url: str) -> bool:
    """True iff `url` is in the "roles absent" serves-all degenerate class —
    gated by private_ok (the ONLY place private_ok gates a serves-all
    candidate; an explicit roles list is its own opt-in and is never gated
    by private_ok, see _role_eligible)."""
    return LLM_BACKEND_ROLES.get(url) is None and LLM_BACKEND_PRIVATE_OK.get(url, True)


def _role_eligible(url: str, role: str) -> bool:
    """Role+privacy eligibility (P-1/P-3, I-1a) — the HARD PRE-FILTER's role
    axis, independent of health/cooldown/reserved/cap. Role-carrying traffic:
    eligible iff (roles absent AND private_ok) OR role in roles — an explicit
    roles list is itself the per-function privacy opt-in (I-1), so a
    private_ok=false backend CAN be eligible for a role it explicitly lists.
    Role-less traffic: eligible on any private_ok backend; the roles list is
    IGNORED (a local card pinned to "extract" must not refuse an ad-hoc
    authenticated chat, R-3) — and a private_ok=false backend is NEVER
    eligible for role-less traffic, regardless of its roles list."""
    roles = LLM_BACKEND_ROLES.get(url)
    if not role:
        return LLM_BACKEND_PRIVATE_OK.get(url, True)
    if roles is None:
        return LLM_BACKEND_PRIVATE_OK.get(url, True)
    return role in roles


def _fits(url: str, est_prompt_tokens: float, effective_max_tokens: float) -> bool:
    """Fit check (A-1): backends without a declared n_ctx always fit
    (backward compat — no fit information available). May only EXCLUDE a
    backend (I-3); never modifies the request in any way."""
    n_ctx = LLM_BACKEND_NCTX.get(url)
    if not n_ctx:
        return True
    return (est_prompt_tokens + effective_max_tokens) <= n_ctx * (1 - FIT_MARGIN)


def _eligible_backends(role: str, est_prompt_tokens: float = 0.0,
                       effective_max_tokens: float = 0.0) -> list[str]:
    """The HARD PRE-FILTER (P-1 Critical): role+privacy+fit eligibility,
    computed BEFORE any affinity/health/cooldown/reserved/cap logic runs.
    Everything else in _select_llm_backend operates strictly inside this
    set — an empty return here is the 422 no_eligible_backend signal."""
    role = (role or "").strip().lower()
    return [b for b in LLM_POOL if _role_eligible(b, role)
            and _fits(b, est_prompt_tokens, effective_max_tokens)]


def _classify_no_eligible_constraint(role: str, est_prompt_tokens: float,
                                     effective_max_tokens: float) -> str:
    """Which axis emptied the eligible set, for the 422 body's `constraint`
    field ("role"|"privacy"|"fit") — computed by re-checking role+privacy
    ALONE (ignoring fit): if that alone is already empty, the size of the
    request was never the issue. Within that: "privacy" if a serves-all
    (roles-absent) candidate EXISTS but is blocked by private_ok=false —
    the fleet does have a home for this traffic class, if only it were
    private; "role" if no backend is configured to handle this function at
    all (privacy is moot — nothing to opt in). Role-less traffic that fails
    role+privacy is always "privacy" (roles never enter into it)."""
    role = (role or "").strip().lower()
    role_privacy_eligible = [b for b in LLM_POOL if _role_eligible(b, role)]
    if role_privacy_eligible:
        return "fit"
    if not role:
        return "privacy"
    any_serves_all_candidate = any(LLM_BACKEND_ROLES.get(b) is None for b in LLM_POOL)
    return "privacy" if any_serves_all_candidate else "role"


def _record_role_routed(role: str) -> None:
    role = (role or "").strip().lower()
    if role not in _llm_routed_by_role:
        return
    _llm_routed_by_role[role] += 1
    _llm_routed_by_role_last_ts[role] = datetime.now(timezone.utc).isoformat()


def _record_no_eligible_backend(constraint: str) -> None:
    global _routing_no_eligible_backend_count, _routing_no_eligible_backend_last_ts
    global _routing_fit_rejected_count, _routing_fit_rejected_last_ts
    now_iso = datetime.now(timezone.utc).isoformat()
    _routing_no_eligible_backend_count += 1
    _routing_no_eligible_backend_last_ts = now_iso
    if constraint == "fit":
        _routing_fit_rejected_count += 1
        _routing_fit_rejected_last_ts = now_iso


def _record_backend_at_capacity() -> None:
    """R-1 (decision:1357): count a 503 backend_at_capacity refusal, paired
    last-ts — same fact:1314 shape as the 422 counters."""
    global _routing_backend_at_capacity_count, _routing_backend_at_capacity_last_ts
    _routing_backend_at_capacity_count += 1
    _routing_backend_at_capacity_last_ts = datetime.now(timezone.utc).isoformat()


def _warn_unknown_role_once(role: str) -> None:
    """Optional (decision:1357): a role value outside ROUTING_ROLE_NAMES from
    a steer-permitted caller degrades silently to role-less eligibility —
    warn ONCE per distinct value so a daemon-side typo is visible."""
    if role in _warned_unknown_role_values:
        return
    # FR-1 (delta re-review): bound the dedupe set and truncate the logged
    # value — distinct garbage values from a steer-permitted (or auth-off)
    # caller must not grow memory or flood the journal with full-length
    # strings.
    if len(_warned_unknown_role_values) >= 64:
        return
    _warned_unknown_role_values.add(role)
    log.warning(
        "X-SM-LLM-Role %r is not a known routing role %s — treating the "
        "request as ROLE-LESS (private_ok backends only). If this is a "
        "daemon-side typo, its traffic is silently degraded until fixed.",
        role[:80], sorted(ROUTING_ROLE_NAMES))


def _counts_free_slot(url: str) -> bool:
    """C-1 Critical fix (decision:1357, amending the plan's F-3 clause
    minimally): a backend counts toward /pool/status free_slots iff it can
    take ANY dream job — either serves-all (roles absent AND private_ok) or
    an EXPLICIT roles list covering every dream role. Dream traffic always
    carries a role, so a full explicit list is exactly as capable as
    serves-all for the daemons' gating purposes; counting only the former
    (the original F-3 reading) silently zeroed free_slots for a fleet whose
    every backend declares roles — the very configuration M-5 steers
    credentialed operators toward — and halted REM/NREM/relation_sweep with
    no warning anywhere. Partial-role fleets still count 0 (per-role slot
    accounting stays deferred) — require_valid_llm_routing_config() warns
    LOUDLY at startup for that case instead of leaving it silent."""
    roles = LLM_BACKEND_ROLES.get(url)
    if roles is None:
        return LLM_BACKEND_PRIVATE_OK.get(url, True)
    return ROUTING_ROLE_NAMES <= roles


def _record_backend_token_usage(backend: str, usage: dict) -> None:
    """Post-review addition A: per-backend cumulative prompt/completion token
    counters from a proxied response's `usage` object. Best-effort — never
    raises, never breaks the proxy path it is called from."""
    try:
        p = usage.get("prompt_tokens")
        c = usage.get("completion_tokens")
        touched = False
        if isinstance(p, (int, float)):
            _llm_tokens_prompt_total[backend] = _llm_tokens_prompt_total.get(backend, 0) + int(p)
            touched = True
        if isinstance(c, (int, float)):
            _llm_tokens_completion_total[backend] = _llm_tokens_completion_total.get(backend, 0) + int(c)
            touched = True
        if touched:
            _llm_tokens_last_ts[backend] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        log.warning("token usage accounting failed for %s: %s", backend, exc)


def _record_llm_latency(backend: str, elapsed_s: float, failed: bool) -> None:
    """New instrument: per-backend LLM request latency. Called exactly once
    per pool-routed request from handle_proxy's finally block (O-2 precedent
    at the credential-fault recorder above) — wrapped so an exception here
    can never break the proxy path it is called from."""
    try:
        _llm_requests_total[backend] = _llm_requests_total.get(backend, 0) + 1
        if failed:
            _llm_requests_failed_total[backend] = _llm_requests_failed_total.get(backend, 0) + 1
        _llm_latency_sum_s[backend] = _llm_latency_sum_s.get(backend, 0.0) + elapsed_s
        _llm_latency_max_s[backend] = max(_llm_latency_max_s.get(backend, 0.0), elapsed_s)
        _llm_latency_last_ts[backend] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        log.warning("latency accounting failed for %s: %s", backend, exc)


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


# NOTE: _ordered_llm_backends() was removed (M-3, Model_Attributes_Routing_
# Plan_2026-08-18 pre-build review) — zero callers anywhere in this repo.

def _select_llm_backend(role: str = "", affinity_key: str | None = None,
                        est_prompt_tokens: float = 0.0,
                        effective_max_tokens: float = 0.0) -> "str | None":
    """Pick a backend, or None. Eligibility (role+privacy+fit) is now a HARD
    PRE-FILTER (P-1 Critical): every fallback tier below — affinity hit, cold
    selection, the protected-prefix logic, the cooldown-ignoring last resort
    — operates STRICTLY inside the eligible set; none of them may widen past
    it (P-1/P-2). Precedence: eligibility > _llm_reserved > cooldown (P-4).

    Two None cases, deliberately not distinguished by this function's return
    value alone (the caller, handle_proxy, tells them apart by calling
    _eligible_backends() itself FIRST):
      * the eligible set is empty (role/privacy/fit) — the 422 case.
      * the eligible set is non-empty but every member is AT its
        max_inflight cap right now — the WAIT case (I-8: the cap never
        widens eligibility, so this function will never pick an over-cap
        backend to avoid returning None).

    Cache-affinity first (keep a warm KV prefix on its card, P-2: an
    affinity-cached backend outside the eligible set is a MISS), else
    least-in-flight while PROTECTING cards holding a frequently-reused
    prefix from eviction. Allocation-free. Clients never choose. Records/
    refreshes the affinity map as a side effect."""
    global _llm_affinity_hits, _llm_affinity_misses
    now = time.monotonic()
    for k in [k for k, v in _llm_affinity.items() if now - v[1] > AFFINITY_TTL]:
        _llm_affinity.pop(k, None)

    eligible = _eligible_backends(role, est_prompt_tokens, effective_max_tokens)
    if not eligible:
        return None
    eligible_set = set(eligible)

    def _at_cap(b: str) -> bool:
        cap = LLM_BACKEND_MAX_INFLIGHT.get(b)
        return cap is not None and _llm_inflight.get(b, 0) >= cap

    def _usable(b: str) -> bool:
        return (b in eligible_set and b not in _llm_reserved
                and _llm_unhealthy_until.get(b, 0.0) <= now)

    # 1) affinity hit — same prefix already warm on an ELIGIBLE, usable,
    #    non-saturated, non-capped card
    ent = _llm_affinity.get(affinity_key) if affinity_key else None
    if (ent and ent[0] in eligible_set and _usable(ent[0]) and not _at_cap(ent[0])
            and _llm_inflight.get(ent[0], 0) < AFFINITY_MAX_INFLIGHT):
        ent[1] = now
        ent[2] += 1
        _llm_affinity_hits += 1
        return ent[0]

    # 2) miss — least-in-flight, protecting cards holding a reused (hits>=N)
    #    hot prefix, with every fallback tier bottoming out at `eligible`
    #    (P-1/P-2), never the full pool.
    protected = {v[0] for v in _llm_affinity.values()
                 if now - v[1] <= AFFINITY_TTL and v[2] >= AFFINITY_PROTECT_HITS}
    usable = [b for b in eligible if _usable(b)]
    cold = ([b for b in usable if b not in protected] or usable
            or [b for b in eligible if b not in _llm_reserved] or eligible)
    not_capped = [b for b in cold if not _at_cap(b)]
    if not not_capped:
        return None   # every eligible candidate is at its concurrency cap
    chosen = min(not_capped, key=lambda b: _llm_inflight.get(b, 0))
    if affinity_key:
        _llm_affinity[affinity_key] = [chosen, now, (ent[2] + 1 if ent else 1)]
        _llm_affinity_misses += 1
    return chosen


async def _select_backend_waiting_on_capacity(
        role: str, affinity_key: "str | None",
        est_prompt_tokens: float, effective_max_tokens: float) -> "str | None":
    """Bounded poll loop around _select_llm_backend for the max_inflight WAIT
    case (operator ruling 2026-08-18: "daemons already wait synchronously" —
    this mirrors that, gateway-side, via the existing S-11-style poll shape
    rather than a new queue). Called ONLY after the caller has already
    confirmed the eligible set (role+privacy+fit) is non-empty — a None
    return here means every eligible backend stayed at its cap for the whole
    wait window, not that nothing was eligible."""
    deadline = time.monotonic() + LLM_MAX_INFLIGHT_WAIT_S
    while True:
        backend = _select_llm_backend(role, affinity_key, est_prompt_tokens, effective_max_tokens)
        if backend is not None:
            return backend
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(LLM_MAX_INFLIGHT_POLL_S)


async def _wait_for_capacity_slot(role: str, affinity_key: "str | None",
                                  est_prompt_tokens: float,
                                  effective_max_tokens: float) -> "str | None":
    """R-4 (decision:1357): the bounded-WAITER wrapper around the bounded
    wait. Every waiter holds an admitted request for up to the full wait
    window (and counts toward any S-11 gateway-wide admission budget), so
    the number of simultaneous waiters is itself capped — beyond
    LLM_MAX_CAPACITY_WAITERS the caller gets an immediate None (→ 503)
    instead of joining the queue. The counter has the same
    increment-at-entry / release-on-every-exit lifetime discipline as the
    I-8b inflight score."""
    global _capacity_waiters
    if _capacity_waiters >= LLM_MAX_CAPACITY_WAITERS:
        return None
    _capacity_waiters += 1
    try:
        return await _select_backend_waiting_on_capacity(
            role, affinity_key, est_prompt_tokens, effective_max_tokens)
    finally:
        _capacity_waiters -= 1

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
    """Security review O-6 — see log_hygiene.scrub_url_credentials (shared with
    the coordinator since v0.9.50, when its encoder URLs became operator-supplied)."""
    return scrub_url_credentials(text)


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
# R-6 (decision:1357, closing the plan's R-2 item): standalone framework
# tools that legitimately steer — relation_sweep sends X-SM-LLM-Role: judge
# but runs outside the gateway process, so it can never hold a minted daemon
# token — get in via an OPERATOR-DECLARED name allowlist, unioned here. The
# operator mints an ordinary agent token under one of these names and points
# the tool at it. Deliberately a NAME list, never a role-based widening and
# never admin (auth_middleware confines admin tokens to /admin/*): each
# entry is one explicit identity the operator chose to trust with steering.
LLM_STEER_EXTRA_AGENT_NAMES = frozenset(
    n.strip() for n in os.environ.get("LLM_STEER_EXTRA_AGENT_NAMES", "").split(",")
    if n.strip())
DAEMON_AGENT_NAMES = frozenset(
    {_CONSOLIDATION_AGENT_NAME, _REM_DAEMON_AGENT_NAME}) | LLM_STEER_EXTRA_AGENT_NAMES


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
        # Populated by set_known_routes() at startup, once, from the app
        # router — see that method's docstring and the route-guard fields
        # this backs: {key: {"pattern": re.Pattern | None, "methods": set[str]}}.
        # A key is a PlainResource's exact path, or a DynamicResource's
        # formatter string (e.g. "/memory/status/{pg_id}") — either way it's
        # only ever used as a dict key, never matched by string equality for
        # the dynamic case (that's what "pattern" is for).
        self._known_routes: dict = {}

    def set_known_routes(self, router: "web.UrlDispatcher") -> None:
        """Snapshot the framework's registered routes (method + path pattern)
        for the wrong-method/unknown-path guard in handle_proxy.

        Derived from the router itself, never a hand-written table
        (decision:1032 class — never write what can be derived): every
        /memory/* and /admin/* route attach() registers, plus /health and
        /pool/status, is picked up automatically, so a future route added
        anywhere in the app is covered without touching this method.

        Called after attach_coordinator() and the /health, /pool/status
        registrations. The catch-all route ("*", "/{tail:.*}") is excluded
        by its wildcard METHOD below, not by registration order — so the
        snapshot is correct whether it is taken before or after the
        catch-all is added. main() still calls this before adding the
        catch-all purely as a readable convention.
        """
        known: dict = {}
        for route in router.routes():
            if route.method == "*":
                # A wildcard-method registration — the catch-all proxy route
                # ("*", "/{tail:.*}") — is by definition not a specific
                # framework route: snapshotting it would mark every path
                # "known" and blanket-405 the LLM passthrough. Filtering by
                # method makes the snapshot correct regardless of WHEN it is
                # taken relative to the catch-all's registration, so the
                # startup ordering is a convention, not a correctness
                # requirement.
                continue
            resource = route.resource
            if resource is None:
                continue
            info = resource.get_info()
            # PlainResource → {"path": "/memory/save"}; DynamicResource →
            # {"formatter": "/memory/status/{pg_id}", "pattern": re.Pattern}.
            # (Measured against aiohttp 3.14 — see HANDOFF.md.)
            key = info.get("path", info.get("formatter"))
            if key is None:
                continue
            entry = known.setdefault(key, {"pattern": info.get("pattern"), "methods": set()})
            entry["methods"].add(route.method)
        self._known_routes = known

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

    def _route_guard(self, request: web.Request) -> "web.Response | None":
        """fact:1535 — run BEFORE any ROUTING_MAP/LLM dispatch decision.

        handle_proxy is the catch-all handler: by construction, aiohttp only
        ever reaches it for a (method, path) pair no specific resource
        accepted — either because the path matches a real framework
        resource but the METHOD doesn't (aiohttp's own router can't surface
        that as 405 here, because the catch-all's method="*" absorbs every
        method once a specific resource declines to match), or because the
        path is genuinely unregistered. This tells the two apart using the
        route view set_known_routes() derived from the router itself:

        - Path matches a known resource, method doesn't → 405, Allow header
          naming the accepted method(s), body says the request was NOT
          forwarded to any LLM backend (same voice as the 401/403 replies
          coordinator.auth_middleware raises — fact:1503 class: informative,
          says explicitly what did NOT happen, so a retry can be safe).
        - Path doesn't match any known resource, but starts with a reserved
          framework prefix (/memory/, /admin/) → 404, same voice.
        - Anything else → None (today's ROUTING_MAP/LLM behaviour, unchanged
          — the LM Studio passthrough for /v1/chat/completions and any
          non-framework path is a supported contract, not a mistyped call).
        """
        path = request.path
        methods = None
        for key, entry in self._known_routes.items():
            pattern = entry["pattern"]
            if pattern is not None:
                if pattern.fullmatch(path):
                    methods = entry["methods"]
                    break
            elif key == path:
                methods = entry["methods"]
                break

        if methods is not None:
            allow = ", ".join(sorted(methods))
            return web.json_response(
                {"error": f"Method {request.method} not allowed on {path}. "
                          f"This framework route accepts: {allow}. The "
                          f"request was NOT forwarded to any LLM backend — "
                          f"correct the method and retry."},
                status=405,
                headers={"Allow": allow, "X-SM-Fault-Origin": "gateway"},
            )
        if path.startswith(RESERVED_ROUTE_PREFIXES):
            return web.json_response(
                {"error": f"No such framework route: {path}. This path is "
                          f"not registered under the framework's reserved "
                          f"prefix. The request was NOT forwarded to any LLM "
                          f"backend — correct the path and retry."},
                status=404,
                headers={"X-SM-Fault-Origin": "gateway"},
            )
        return None

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        # fact:1535 route-guard — runs before ANYTHING else in this method,
        # including the S-14 header stripping below: a mistyped/wrong-method
        # framework request must fail and say why, never reach dispatch.
        guard_response = self._route_guard(request)
        if guard_response is not None:
            return guard_response

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
            # A-3: parse the body ONCE — affinity, fit (est_prompt_tokens is a
            # pure char count, no parse needed; effective_max_tokens reads the
            # parsed struct), and the post-selection override rewrite all read
            # this SAME struct; never a second json.loads of the same body.
            body_obj = _parse_json_body(llm_body)
            role = steer_headers.get("X-SM-LLM-Role", "").strip().lower()
            affinity_key = _affinity_key_from_obj(body_obj)
            est_prompt_tokens = (len(llm_body) / CHARS_PER_TOKEN_RATIO
                                 if CHARS_PER_TOKEN_RATIO > 0 else 0.0)
            effective_max_tokens = _extract_effective_max_tokens(body_obj)

            # Eligibility is a HARD PRE-FILTER (P-1 Critical): computed BEFORE
            # any affinity/health/cooldown/reserved/cap logic runs. Empty →
            # 422, PRE-DISPATCH (I-8b: no inflight accounting has happened
            # yet, this return is well before the try: block that reserves a
            # slot) — never silently widens, never falls back to an
            # ineligible backend, never queues.
            if role and role not in ROUTING_ROLE_NAMES:
                _warn_unknown_role_once(role)
            eligible_pre = _eligible_backends(role, est_prompt_tokens, effective_max_tokens)
            if not eligible_pre:
                constraint = _classify_no_eligible_constraint(
                    role, est_prompt_tokens, effective_max_tokens)
                _record_no_eligible_backend(constraint)
                refusal_body = {"error": "no_eligible_backend",
                                "constraint": constraint, "role": role or None}
                if constraint == "fit":
                    # R-5 disposition (decision:1357): retry-without-charge
                    # STANDS as ruled (F-1: a config gap is not a record
                    # defect) — observability is the mitigation. The refusal
                    # names the estimate that failed so an operator can
                    # retune LLM_CHARS_PER_TOKEN_RATIO / FIT_MARGIN / n_ctx
                    # against real traffic.
                    refusal_body["est_prompt_tokens"] = int(est_prompt_tokens)
                    refusal_body["effective_max_tokens"] = int(effective_max_tokens)
                    # FR-2 (delta re-review): the daemons' refusal handling
                    # reads only {error, constraint, role}, so the estimate
                    # fields alone reach no operator — this journal line is
                    # where the retune signal actually lands.
                    log.warning(
                        "fit-rejected: est_prompt_tokens=%d + max_tokens=%d "
                        "fits no declared n_ctx (role=%s) — if this request "
                        "is legitimately sized, retune LLM_CHARS_PER_TOKEN_"
                        "RATIO / FIT_MARGIN or raise the backend's n_ctx.",
                        int(est_prompt_tokens), int(effective_max_tokens),
                        role or "none")
                return web.json_response(
                    refusal_body,
                    status=422, headers={"X-SM-Fault-Origin": "gateway"},
                )

            # R-4 (decision:1357): a request that can ONLY land on a
            # credentialed backend but is not on the S-04 allowlist is DOOMED
            # — deny it here, BEFORE it can hold a capacity-wait slot for the
            # full window. A mixed eligible set (any uncredentialed member)
            # falls through: selection may legitimately pick the
            # uncredentialed one, and the post-selection S-04 check below
            # still guards the credentialed choice.
            _route = (request.method, request.path.rstrip("/") or "/")
            if (_route not in CREDENTIALED_BACKEND_ALLOWED_ROUTES
                    and all(LLM_BACKEND_TOKENS.get(b) is not None for b in eligible_pre)):
                record_credentialed_route_denied(
                    eligible_pre[0], request.method, request.path,
                    agent_name=_safe_agent_name(request),
                    request_id=_safe_request_id(request),
                )
                return web.json_response(
                    {"error": "credentialed backends accept only framework endpoints"},
                    status=403, headers={"X-SM-Fault-Origin": "gateway"},
                )

            # At least one backend IS eligible — fast path first; only if
            # every eligible backend is AT its max_inflight cap right now do
            # we join the (waiter-capped, R-4) bounded wait on it (I-8: the
            # cap never widens eligibility, so selection never picks an
            # over-cap backend to avoid waiting) rather than refusing or
            # overriding the cap. All pre-dispatch: no inflight accounting yet.
            llm_backend = _select_llm_backend(
                role, affinity_key, est_prompt_tokens, effective_max_tokens)
            if llm_backend is None:
                llm_backend = await _wait_for_capacity_slot(
                    role, affinity_key, est_prompt_tokens, effective_max_tokens)
            if llm_backend is None:
                _record_backend_at_capacity()
                return web.json_response(
                    {"error": "backend_at_capacity"}, status=503,
                    headers={"X-SM-Fault-Origin": "gateway"},
                )
            target_base = llm_backend

            # Per-backend body rewrites (LLM_BACKENDS_JSON "model" +
            # "extra_body") — a cloud endpoint needs its real model id, not the
            # local "local-model" every caller sends by default, and its
            # provider-specific switches (e.g. thinking disabled) that no
            # caller knows to send. See _apply_backend_body_overrides.
            llm_body = _apply_backend_body_overrides(
                llm_body, LLM_BACKEND_MODELS.get(llm_backend),
                LLM_BACKEND_EXTRAS.get(llm_backend), _body_obj=body_obj)

        target_url = _upstream_url(target_base, request.rel_url)
        log.debug("→ %s %s", request.method, target_url)

        # P-6 (Model_Attributes_Routing_Plan_2026-08-18): X-SM-LLM-* headers
        # are stripped before the upstream forward for EVERY caller, daemons
        # and admins included — the role/affinity signal above was already
        # read off `steer_headers` (whose S-14 gate decides who may SET it
        # for the gateway's OWN routing decision); the provider itself must
        # never see routing metadata on the wire, aligning the request
        # direction with the response direction's existing X-SM- stripping
        # (_filter_headers' strip_gateway_namespace). Deliberately changes
        # the pinned expectation in tests/test_llm_steering_headers.py:98.
        upstream_headers = self._filter_headers(_strip_llm_steering_headers(steer_headers))
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
                # Optional (decision:1357): the per-role ROUTED counter
                # increments HERE, at dispatch, beside the inflight/_llm_routed
                # accounting it mirrors — not back at selection, where a
                # request could still be refused (S-04) before any dispatch.
                if role:
                    _record_role_routed(role)

            # New instrument: latency timer starts here (request dispatch),
            # spans every retry attempt below as ONE logical request (same
            # framing as the inflight/_llm_routed accounting just above —
            # a retry is the same call, not a second one), and is read back
            # in the finally at the bottom of this method regardless of how
            # the method exits. _llm_req_failed defaults to True (covers
            # every exception/error-status exit path below) and is flipped
            # to False only at the single success return.
            _llm_req_start_mono = time.monotonic() if llm_backend is not None else None
            _llm_req_failed = True

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

                        # Post-review addition A: accumulate the response body
                        # for a best-effort `usage` parse AFTER the loop, so a
                        # per-backend token count can be kept without ever
                        # delaying or altering the passthrough chunk written
                        # below. Attempted for a successful LLM response that
                        # is either uncompressed OR compressed with a
                        # SUPPORTED_CONTENT_ENCODINGS value (gzip/deflate/br —
                        # see _decompress_full_for_usage). Live debugging on a
                        # cloud backend (api.deepseek.com) showed the original
                        # `not content_encoding` gate reading 0 tokens on
                        # every gzipped response while the billable call
                        # succeeded — the cost meter was blind exactly where
                        # cost matters. The accumulation itself is bounded by
                        # LLM_USAGE_CAPTURE_CAP_BYTES on the COMPRESSED bytes
                        # (the memory bound is the point — decompression
                        # happens once, after the loop, on the accumulated
                        # cap-bounded body) so a large streamed completion is
                        # never held fully in memory just to look for a
                        # trailing `usage` object. An unsupported/unknown
                        # encoding (e.g. a future provider using `zstd`)
                        # leaves capture_usage False, same as the original
                        # gate for any compression it didn't understand.
                        # stream:true responses are SSE — the accumulated
                        # bytes can never parse as a single JSON object, so
                        # skip capture instead of buffering up to the cap for
                        # a parse that always fails (Optional, decision:1357).
                        _capture_encoding_ok = (
                            not content_encoding
                            or content_encoding.strip().lower() in SUPPORTED_CONTENT_ENCODINGS
                        )
                        capture_usage = (llm_backend is not None and upstream.status < 400
                                        and _capture_encoding_ok
                                        and not (body_obj or {}).get("stream"))
                        usage_chunks: "list[bytes] | None" = [] if capture_usage else None
                        usage_bytes = 0

                        # write_eof() lives inside the same try as the chunk loop so that
                        # an EOF-time disconnect is handled by the same except clauses.
                        try:
                            async for chunk in upstream.content.iter_any():
                                if usage_chunks is not None:
                                    usage_bytes += len(chunk)
                                    if usage_bytes > LLM_USAGE_CAPTURE_CAP_BYTES:
                                        usage_chunks = None   # abandon — too big to hold
                                    else:
                                        usage_chunks.append(chunk)
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

                        if usage_chunks:
                            try:
                                usage_body = b"".join(usage_chunks)
                                if content_encoding:
                                    # Whole-body decompress — mirrors
                                    # _decompress_prefix_for_parse's encoding
                                    # support exactly (gzip/deflate/br) but
                                    # without its bounded-prefix truncation,
                                    # since the `usage` object trails the
                                    # response and a prefix would miss it.
                                    # Raises on failure (unlike the prefix
                                    # helper), caught by this same except —
                                    # a decompression failure abandons
                                    # capture silently, exactly like today's
                                    # parse failure.
                                    usage_body = _decompress_full_for_usage(usage_body, content_encoding)
                                resp_payload = json.loads(usage_body)
                                usage = (resp_payload.get("usage")
                                        if isinstance(resp_payload, dict) else None)
                                if isinstance(usage, dict):
                                    _record_backend_token_usage(llm_backend, usage)
                            except Exception:
                                pass   # not a single-object JSON body (e.g. SSE) — skip, never breaks the proxy path

                        if llm_backend is not None:
                            _llm_mark_ok(llm_backend)   # connected + served — clear fail streak
                        # The exchange completed and the body was fully
                        # written above (write_eof / the disconnect branches
                        # both fall through to here) — but a fault STATUS
                        # from upstream (>= 400) still reaches this same
                        # return (the body passes through verbatim either
                        # way, per the X-SM-Fault-Origin comment above), so
                        # the latency instrument's failed flag reads the
                        # status here rather than assuming success.
                        _llm_req_failed = upstream.status >= 400
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
                # New instrument: record latency for every exit path from
                # this method (success, upstream fault status, gateway
                # error, timeout, cancellation) — _llm_req_start_mono is set
                # iff llm_backend is not None, i.e. exactly the pool-routed
                # requests this instrument covers; embeddings/reranking
                # never reach here with a backend set. _llm_req_failed
                # defaults True and is flipped only at the single success
                # return above, reading the upstream status there.
                if _llm_req_start_mono is not None:
                    _record_llm_latency(
                        llm_backend, time.monotonic() - _llm_req_start_mono, _llm_req_failed)


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
def _find_uv() -> "str | None":
    """Resolve the uv binary for daemon spawns.

    PATH first; then the documented user-level install locations, because a
    systemd unit's default PATH omits ~/.local/bin — without the fallback the
    gateway serves normally while both daemons silently stay stopped.
    """
    uv = shutil.which("uv")
    if uv:
        return uv
    for candidate in (
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


async def _start_daemon() -> "asyncio.subprocess.Process | None":
    daemon_path = Path(__file__).parent / "consolidation_loop.py"
    if not daemon_path.exists():
        log.warning("Daemon script not found at %s — consolidation will not run", daemon_path)
        return None
    uv = _find_uv()
    if not uv:
        log.warning("uv not found (PATH, ~/.local/bin, ~/.cargo/bin) — cannot start consolidation daemon")
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
    uv = _find_uv()
    if not uv:
        log.warning("uv not found (PATH, ~/.local/bin, ~/.cargo/bin) — cannot start REM daemon")
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
        # F-3 (Model_Attributes_Routing_Plan_2026-08-18): free_slots counts
        # ONLY serves-all-eligible backends (roles absent AND private_ok) —
        # dream gating stays conservative rather than assuming a role-scoped
        # or private_ok=false backend is fair game for whatever the caller
        # happens to be. Full per-role slot accounting is deferred (F-3,
        # named follow-up, not built this cycle) — `serves_all` is additive
        # visibility, not a promise of finer-grained accounting.
        serves_all = _serves_all(b)
        # C-1 (decision:1357): the free count uses _counts_free_slot — a
        # backend with an explicit FULL dream-roles list is as capable as a
        # serves-all one for the daemons' gating, and counting only
        # serves-all silently zeroed free_slots (halting every dream daemon)
        # for all-declared fleets. `counts_free_slot` is surfaced additively
        # so a monitor can see WHY the count is what it is.
        counts_free = _counts_free_slot(b)
        entry = {
            "inflight": _llm_inflight.get(b, 0),
            "oldest_inflight_age_s": age,
            "cooldown": round(max(0.0, _llm_unhealthy_until.get(b, 0.0) - now), 1),
            "reserved": b in _llm_reserved,
            "available": avail,
            "serves_all": serves_all,
            "counts_free_slot": counts_free,
        }
        # Lazy wedge check (the one exception to "no upstream probes"): only
        # when a request has been in flight suspiciously long — busy-generating
        # backends answer their own /health instantly; a driver-hung one can't.
        if age is not None and age > LLM_WEDGE_SUSPECT_AGE and _session is not None:
            entry["suspect_wedged"] = not await _probe_backend_alive(_session, b)
        backends[b] = entry
        free += 1 if (avail and counts_free) else 0
    return web.json_response({"free_slots": free, "backends": backends})


# --------------------------------------------------------------------------- #
# Backend CAPABILITY probing — "can it serve", not "is it up"
# --------------------------------------------------------------------------- #
# How often the capability probe re-measures. It costs real inference time on
# the same backends that serve traffic, so it is deliberately infrequent.
CAPABILITY_PROBE_INTERVAL_S = float(
    os.environ.get("CAPABILITY_PROBE_INTERVAL_S", "600"))
# How soon the probe re-tries after a FAILING probe (backend unreachable,
# non-2xx, or an exception): a backend that is not serving costs nothing to
# ping, and waiting the full interval turned a 60 s encoder cold-start into a
# ten-minute window of `degraded` and empty searches (fact:1609). A `too_slow`
# backend IS serving, so it keeps the full interval — a fast re-probe there
# would add load to exactly the encoder that is struggling. Unmeasured
# default; bounds the post-recovery blind window to one retry interval.
CAPABILITY_PROBE_RETRY_S = float(
    os.environ.get("CAPABILITY_PROBE_RETRY_S", "15"))
# The probe payload. Small enough to be cheap, large enough to be representative
# — a one-token ping would measure nothing about the cost that actually matters.
CAPABILITY_PROBE_DOCS = int(os.environ.get("CAPABILITY_PROBE_DOCS", "4"))
CAPABILITY_PROBE_DOC_CHARS = int(
    os.environ.get("CAPABILITY_PROBE_DOC_CHARS", "1000"))

# Populated by the background probe; read by /health. "unknown" until the first
# probe lands — NEVER asserted as healthy on no data (decision 928's rule: "not
# yet probed" must not read as "verified clean").
_capability: dict = {"status": "unknown", "probed_at": None}


def _projection_age_s(last_ok_at, now=None) -> float | None:
    """Seconds since the surviving numbers were actually measured.

    None — never 0.0 — for every case where the age is UNKNOWN: never
    measured, no stamp, an unparseable stamp, and (R2-N7) a stamp in the
    future. 0.0 means "just measured", and a clock stepped backwards is the
    one case where publishing that would be actively misleading: the oldest
    possible reading would read as the freshest. One shape for "we do not
    know how old this is"."""
    if not last_ok_at:
        return None
    try:
        measured = datetime.fromisoformat(last_ok_at)
    except (TypeError, ValueError):
        return None
    if measured.tzinfo is None:
        measured = measured.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = (now - measured).total_seconds()
    return round(age, 1) if age >= 0 else None


def capability_snapshot() -> dict:
    """The published view of the capability probe — what /health serialises
    as `backend_capability`.

    A-2 (ADV-2): each backend block also carries `projection_age_s`, computed
    HERE rather than at merge time, so the age is the age at the moment of
    READING. There is deliberately NO age cap: an old projection is still the
    only measurement anyone has, and capping it back to absence would restore
    the very defect this feature removes. Computing it at read time also means
    a probe daemon that has stopped running shows a monotonically growing age
    instead of a frozen one — a stalled instrument becomes visible rather than
    looking like a quiet system.

    The per-backend blocks are COPIED before the computed key is added: the
    carried projection is now the only copy of a number meant to outlive its
    cycle, so a caller must not be able to reach in and edit it."""
    snap = dict(_capability)
    now = datetime.now(timezone.utc)
    for backend in ("reranker", "embedder"):
        block = snap.get(backend)
        if not isinstance(block, dict):
            continue
        block = dict(block)
        block["projection_age_s"] = _projection_age_s(block.get("last_ok_at"), now)
        snap[backend] = block
    return snap


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


# The MEASURED set a successful probe cycle produces for one backend. These
# keys travel TOGETHER: throughput and the latency it was computed from are
# one coherent reading, and the projection and its ceiling are derived from
# that same reading — carrying half of them forward would publish a mix of
# two different cycles under one block.
#
# ⛔ `serves_full_payload` is deliberately NOT here (operator ruling A-1 on
# ADV-1). It is not a reading, it is a VERDICT in the present tense — "this
# backend can serve a full payload inside the caller's timeout" — and
# publishing it as `true` beside `status: "failing"` prints an affirmative
# green next to a backend that answered nothing, demoted only by a sibling
# key no renderer is obliged to show. Only measured NUMBERS are carried; the
# verdict goes to null while the reading ages (see _merge_capability_
# projection).
_PROJECTION_CARRY_KEYS = ("projected_full_payload_s", "ceiling_s",
                          "throughput_chars_s", "latency_s")


def _merge_capability_projection(previous: dict | None, fresh: dict) -> dict:
    """A projection, once measured, never DISAPPEARS from /health WITHIN A
    PROCESS LIFETIME; it only ages and says so. (The carry is in-memory: a
    gateway restart legitimately starts again from "unknown" rather than
    trusting a number measured before whatever caused the restart.)

    The defect this exists for (fact:1560): the probe daemon replaced the
    module-level snapshot WHOLESALE every cycle, and a failing probe writes
    only `status`/`error` — so `projected_full_payload_s` vanished from a
    backend's block at exactly the moment the backend was busy. Clients size
    their search timeout from that block (memory_bridge.search_ceiling), so
    they fell back to a fixed default (CAPACITY_SEARCH_TIMEOUT_FALLBACK_S,
    120 s — NOT the 30 s floor, which only clamps a derived value) while the
    gateway kept working the same request for minutes. An absent number read
    as "nothing to worry about" when the truth was "the last thing we
    measured was alarming".

    So: a cycle that MEASURED a projection publishes it fresh and stamps
    `projection_stale: False`; a cycle that failed keeps the last measured
    NUMBERS of that block, nulls the `serves_full_payload` verdict, stamps
    `projection_stale: True` and `last_ok_at` (when the surviving numbers
    were actually taken), and leaves this cycle's own `status`/`error` in
    place. A backend that has NEVER measured keeps today's shape and gets
    `projection_stale: None` — "never measured" is a third state, and no
    number is invented to fill it.

    "Measured" means the block carries `projected_full_payload_s`, which
    includes a `too_slow` verdict: that reading succeeded, it was just slow,
    and a slow-but-real projection is precisely the value a client must not
    lose. Mutates and returns `fresh` (the dict _probe_capability just
    built); `previous` is only read."""
    if not isinstance(fresh, dict):
        return fresh
    for backend in ("reranker", "embedder"):
        block = fresh.get(backend)
        if not isinstance(block, dict):
            continue
        if block.get("projected_full_payload_s") is not None:
            block["projection_stale"] = False
            block["last_ok_at"] = fresh.get("probed_at")
            continue
        prev_block = (previous or {}).get(backend)
        if (not isinstance(prev_block, dict)
                or prev_block.get("projected_full_payload_s") is None):
            # Never measured — nothing to carry, and nothing to invent.
            # R2-N6: the verdict is nulled here too, so "we make no claim"
            # has ONE shape across both never-measured and ageing blocks
            # rather than being absent in one and null in the other.
            block["serves_full_payload"] = None
            block["projection_stale"] = None
            continue
        for key in _PROJECTION_CARRY_KEYS:
            if key in prev_block:
                block[key] = prev_block[key]
            else:
                block.pop(key, None)
        # A-1 (ADV-1): the VERDICT does not travel with the reading. It is
        # explicitly null — not absent — so a renderer sees "we no longer
        # claim this" rather than reading a missing key as false, and never
        # sees a green verdict on a backend that just failed to answer.
        block["serves_full_payload"] = None
        block["projection_stale"] = True
        # `previous` may itself already be a carried-forward block, so the
        # stamp is chained: the age reported is the age of the NUMBERS, not
        # of the cycle that last carried them.
        block["last_ok_at"] = (prev_block.get("last_ok_at")
                               or (previous or {}).get("probed_at"))
    return fresh


async def _capability_probe_daemon(proxy, stop_event, coordinator=None) -> None:
    """Refresh the capability snapshot on a slow cadence, forever.

    Wrapped so a probe failure can never propagate: this is an OBSERVABILITY
    path, and an unguarded exception here would take down the thing it exists
    to report on (the trap named in CLAUDE.md's Group 3).

    `coordinator` (optional — the running MemoryCoordinator instance, when
    the caller has one) is threaded through to _maybe_derive_capacity so the
    measured-payload basis can read its cumulative rerank counters.

    ⛔ The fresh reading is MERGED onto the previous snapshot, never assigned
    over it — see _merge_capability_projection for why a wholesale
    replacement made a measured projection disappear on the cycle it
    mattered most."""
    global _capability
    while not stop_event.is_set():
        try:
            _capability = _merge_capability_projection(
                _capability, await _probe_capability(proxy.session))
            await _maybe_derive_capacity(_capability, coordinator)
        except Exception as exc:
            log.warning("capability probe failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=_probe_sleep_s(_capability))
        except asyncio.TimeoutError:
            pass


def _probe_sleep_s(capability: dict | None,
                   interval_s: float | None = None,
                   retry_s: float | None = None) -> float:
    """INVARIANT: the probe interval is a function of the last probe's
    outcome. A backend block whose status is `failing` (or a snapshot that
    has never landed: `unknown` / absent) → the short retry interval;
    otherwise (`ok`, `too_slow`) → the full interval. Pure, so the branch is
    testable and mutation-checkable without a gateway."""
    interval_s = CAPABILITY_PROBE_INTERVAL_S if interval_s is None else interval_s
    retry_s = CAPABILITY_PROBE_RETRY_S if retry_s is None else retry_s
    cap = capability or {}
    blocks = [cap.get("reranker") or {}, cap.get("embedder") or {}]
    if not any(b.get("status") for b in blocks):
        return retry_s                      # never probed successfully
    if any(b.get("status") == "failing" for b in blocks):
        return retry_s
    return interval_s


# --------------------------------------------------------------------------- #
# Capacity derivation — R0-I (decision:1424). REPORT ONLY: this section never
# limits, queues, rejects or resizes a single request. It reads the capability
# probe's own numbers (never a second measurement) and derives what an
# operator or a future wall would need — a projected mean rerank service
# time, a sustainable queue depth against the CLIENT's own timeout ceiling,
# and a memory allowance the reranker container could be given without
# starving Neo4j/Postgres/the embedder/the gateway/the OS. Nothing here
# writes a compose file or applies a limit.
# --------------------------------------------------------------------------- #
_MEM_SIZE_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([KMGT]?I?B?)$", re.IGNORECASE)
# M5 (fix round): KI/MI/GI/TI added alongside the existing KIB/MIB/GIB/TIB
# so k8s-style binary notation ("8Gi", "512Mi", "4Ki" -- no trailing "B")
# parses too, not just the docker-compose "8G"/"512M" and hand-typed
# "2GiB" forms already handled.
_MEM_SIZE_MULTIPLIERS = {
    "": 1, "B": 1,
    "K": 1024, "KB": 1024, "KI": 1024, "KIB": 1024,
    "M": 1024**2, "MB": 1024**2, "MI": 1024**2, "MIB": 1024**2,
    "G": 1024**3, "GB": 1024**3, "GI": 1024**3, "GIB": 1024**3,
    "T": 1024**4, "TB": 1024**4, "TI": 1024**4, "TIB": 1024**4,
}


def _parse_mem_size(raw: str | None) -> int | None:
    """Parse a docker-compose-style memory size ('8G', '512M', a bare byte
    count, or an operator's hand-edited 'GiB'/'MiB'/'KiB' notation) into
    bytes. None on anything unparsable — fail-open, never raises, so a
    malformed env value degrades the derivation rather than the gateway."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _MEM_SIZE_RE.match(s)
    if not m:
        return None
    mult = _MEM_SIZE_MULTIPLIERS.get(m.group(2).upper())
    if mult is None:
        return None
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return None


# Each subtrahend below is a DECLARED ALLOWANCE, not a measurement — the
# comment on each says what it stands in for. Every one is env-overridable so
# a deployment whose compose limits or steady-state usage differ can correct
# the recommendation without a code change.
#
# Neo4j: prefer the OPERATOR'S OWN configured heap+pagecache (the same
# NEO4J_HEAP_MAX / NEO4J_PAGECACHE env vars ops/postgres_neo4j_limits.yaml
# already reads) over the compose `deploy.resources.limits.memory: 8G` cap —
# the configured heap+pagecache is what Neo4j will actually try to hold;
# the 8G cap is only what docker permits it, and is usually never reached.
CAPACITY_NEO4J_FALLBACK_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_NEO4J_FALLBACK_BYTES", "8G"))
# Postgres: the compose `deploy.resources.limits.memory: 4G` cap — Postgres
# has no single configured-heap equivalent to read (shared_buffers=1GB in the
# compose command is a floor, not what the backend processes actually use
# under load), so the allowance is the container's own docker ceiling.
CAPACITY_PG_MEM_ALLOWANCE_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_PG_MEM_ALLOWANCE_BYTES", "4G"))
# Embedder: NOT measured here — a steady-state allowance for the llama.cpp
# embedding container (fits in ~2 GB per ops/postgres_neo4j_limits.yaml's own
# comment on the encoder pair). Only relevant when the embedder runs on THIS
# host (CPU_ENCODER_REPLICAS=1); an operator running it elsewhere can zero
# this.
CAPACITY_EMBEDDER_MEM_ALLOWANCE_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_EMBEDDER_MEM_ALLOWANCE_BYTES", "2G"))
# Gateway: NOT measured — this process's own steady-state footprint
# (aiohttp + asyncpg pool + in-process telemetry dicts).
CAPACITY_GATEWAY_MEM_ALLOWANCE_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_GATEWAY_MEM_ALLOWANCE_BYTES", "512M"))
# OS margin: NOT measured — kernel, page cache pressure, the operator's own
# desktop session on a shared box.
CAPACITY_OS_MEM_MARGIN_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_OS_MEM_MARGIN_BYTES", "1G"))

# Fail-open parse for a CAPACITY_* numeric env setting (H2, fix round): a
# malformed value used to crash the gateway on import via a bare float()/
# int() call -- these are observability/tuning knobs, not something a typo
# in .env should be able to take the whole process down over. Logs ONE
# warning naming the variable and the fallback it's using instead, then
# returns the default -- never raises.
def _capacity_env_number(name: str, default, cast):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log.warning(
            "capacity: %s=%r is not a valid number -- using default %r",
            name, raw, default)
        return default


# Mirrors memory_bridge.py's search_ceiling() — same FORMULA, same shipped
# DEFAULTS — so client_ceiling_s (reported alongside, but see
# CAPACITY_TOLERABLE_WAIT_S below for what queue_bound is actually measured
# against) reflects the same timeout the client will actually apply by
# default. Duplicated rather than imported: hive_mind_proxy is the server
# (ADR-014); importing the thin client back into the server would invert
# that split. A parity unit test (given the same capability input, both
# functions must agree) is the guard against the two copies drifting apart.
#
# Deliberately CAPACITY_-prefixed, NOT the client's own SEARCH_TIMEOUT_* names
# — this repo's own .env.example already documents that those names, set in
# the GATEWAY's env, "have no effect unless a client happens to run with this
# file loaded" (the client reads them from its own, separate .env). Reusing
# the identical names here would make that documented boundary quietly false
# for this one purpose while looking unchanged everywhere else — exactly the
# kind of stale-comment defect CLAUDE.md calls out. An operator who wants
# this derivation's ceiling to track a non-default client ceiling sets the
# CAPACITY_ variant explicitly; the defaults agree today by construction
# (the parity test pins the numbers, not just the env var names).
CAPACITY_SEARCH_TIMEOUT_S          = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_S", 0.0, float)
CAPACITY_SEARCH_TIMEOUT_FLOOR_S    = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_FLOOR_S", 30.0, float)
CAPACITY_SEARCH_TIMEOUT_MAX_S      = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_MAX_S", 300.0, float)
CAPACITY_SEARCH_TIMEOUT_FALLBACK_S = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_FALLBACK_S", 120.0, float)
CAPACITY_SEARCH_SAFETY_FACTOR      = _capacity_env_number("CAPACITY_SEARCH_SAFETY_FACTOR", 1.5, float)
CAPACITY_SEARCH_OVERHEAD_S         = _capacity_env_number("CAPACITY_SEARCH_OVERHEAD_S", 15.0, float)

# H1 (fix round): what queue_bound is actually measured against -- an
# operator's own stated tolerance for how long a search may sit in queue.
# Deliberately NOT client_ceiling_s: client_ceiling_s is itself partly
# DERIVED from s_mean (see _capacity_client_ceiling_s), so the original
# queue_bound = floor(client_ceiling / s_mean) - 1 folded the same
# measurement into itself -- circular, and degenerate (pinned to 0 or 1)
# outside a narrow clamp region. client_ceiling_s is still computed and
# reported as its own field (informative: what the client will itself time
# out at) but no longer feeds this calculation.
#
# Default 30.0s is MEASURED, not a guess: it is the developer reference
# machine's own validated tolerance -- see .env.example's
# CAPACITY_TOLERABLE_WAIT_S comment for the full provenance. Change it to
# your own validated tolerance.
CAPACITY_TOLERABLE_WAIT_S = _capacity_env_number("CAPACITY_TOLERABLE_WAIT_S", 30.0, float)

# Measured-payload basis (operator rulings, 2026-08-23): s_mean_s above
# projects onto a fixed THEORETICAL worst case -- 20 x RERANK_MAX_DOC_CHARS
# (encoder_config's own search_candidate_floor x rerank_max_doc_chars) --
# 491,520 chars, a payload no real query on the reference workstation
# produces. Measured there: 8 real searches against a 1377-record corpus
# had a MEAN of 71,139 rerank_payload_chars and topped out (MAX) at
# 101,240 -- the max is 0.21x the theoretical basis and 1.42x the mean
# (NOT "~4.9x the mean": 4.9x is theoretical-over-observed-max, the
# opposite ratio; an earlier draft of this comment had this backwards).
# The resulting s_mean_s of 36.4s tripped single_search_exceeds_wait
# against real search times measured at 2.78-10.84s wall.
# CAPACITY_TOLERABLE_WAIT_S itself is not the problem -- see its own
# comment above; this is what gets compared against it.
#
# RULING 1: the basis that replaces the theoretical one is the OBSERVED
# MAXIMUM, not the mean -- this is a CAPACITY signal, and an average-case
# basis would under-project a search at the observed max by the same 1.42x
# just measured, exactly the direction a safety bound must never err in.
# Using the max still clears the false alarm with room to spare (it
# projects to roughly 7.5s here, comfortably under the 30s
# CAPACITY_TOLERABLE_WAIT_S default) while staying conservative. The mean
# is still computed and reported alongside (s_mean_measured_s) as cheap,
# useful context -- it is NEVER what feeds queue_bound or
# single_search_exceeds_wait; only the max-based s_max_measured_s does
# (see payload_basis in the derived record for which one actually drove a
# given reading).
#
# When the coordinator (fact:1441's payload counters, reused verbatim --
# see _capacity_payload_stats) has served at least this many real searches
# this process's lifetime, the derivation trusts the observed max over the
# fixed theoretical one. Below this count -- including every fresh
# install, which has zero -- it falls back to the theoretical basis
# unchanged. UNMEASURED: no data here justifies 5 over 3 or 10; it exists
# only so a single one-off search right after startup is never treated as
# representative. Fails open like every CAPACITY_* setting.
#
# The observed max is MONOTONIC non-decreasing for this process's
# lifetime -- it can only rise, so one outlier search pins
# s_max_measured_s until the next gateway restart. For a capacity signal
# that is the safe direction (it never becomes less conservative over
# time), never a defect -- but it is a real property a reader should know
# rather than discover.
#
# STALENESS ACROSS A CONFIG CHANGE: _encoder_config_fingerprint includes
# rerank_max_doc_chars/search_candidate_floor, so raising either fires a
# fresh derivation (config_change trigger) while the coordinator's payload
# counters still hold pre-change traffic -- a record can read
# payload_basis "measured" against a max observed under the OLD config.
# No reset hook exists for this (the counters are process-lifetime, not
# config-lifetime); a reader comparing records across a config change
# should treat payload_basis "measured" readings from before/after the
# change as not directly comparable.
CAPACITY_PAYLOAD_MIN_SAMPLES = _capacity_env_number("CAPACITY_PAYLOAD_MIN_SAMPLES", 5, int)

# "Outside a x2 band" for the probe_drift trigger: fires when the current
# reranker chars/s is more than this factor above OR below the basis
# reading stored in the last derivation record. Exactly at the factor is
# still INSIDE the band (see _capacity_drift_outside_band's docstring).
# A factor <= 1.0 DISABLES this trigger entirely (see
# _capacity_drift_outside_band's own guard) -- documented in .env.example
# (L12, fix round): "fire on any change" is not a meaningful band, so a
# non-positive-band value is treated as "do not use this trigger" rather
# than as "fire on the smallest possible difference".
CAPACITY_DRIFT_BAND_FACTOR = _capacity_env_number("CAPACITY_DRIFT_BAND_FACTOR", 2.0, float)

# Where derivation records persist (MEMORY_LOG_PATH's convention: env-
# overridable, ~/.shared-memory/... default, secured 0600/0700 via
# log_hygiene). JSON-lines, oldest-first; capped at the last N (values <= 0
# behave as 1 -- see _append_capacity_record's M4 clamp).
CAPACITY_LOG_PATH = os.environ.get(
    "CAPACITY_LOG_PATH", "~/.shared-memory/capacity/derivations.jsonl")
CAPACITY_LOG_MAX_RECORDS = _capacity_env_number("CAPACITY_LOG_MAX_RECORDS", 20, int)

# In-memory mirror of the latest record, surfaced on /health without a disk
# read on every hit. None until the first derivation of this process's
# lifetime lands OR a prior record is lazily loaded from disk (see
# _capacity_latest_snapshot). Never asserted as present on no data — the same
# discipline decision 928 established for _capability.
_capacity_latest: dict | None = None
_capacity_latest_loaded_from_disk = False   # memoizes the one lazy disk read
# True once this PROCESS has run its first capability probe — distinguishes
# the startup check (trigger gateway_start_fingerprint_mismatch, which
# compares against whatever the LOG says, not against this process's own
# prior state) from every later cycle (trigger config_change / probe_drift).
_capacity_first_probe_done = False

# B-1 fix (reviewer HIGH, operator 2026-08-23): the payload_threshold_
# crossed trigger (see _maybe_derive_capacity) used to be gated by a
# PROCESS-LOCAL one-shot latch, exactly like _capacity_first_probe_done
# above. That was wrong for this specific trigger: a process-local latch
# resets on every restart, but the comparison it gated (whether the STORED
# record still says "theoretical") is read from the durable, cross-process
# LOG -- so after any restart with unchanged hardware/config (no
# fingerprint mismatch, hence no other trigger fires either), the stored
# record already said "measured" from the PREVIOUS process's life, the
# latch's "last basis is still theoretical" half of the guard was already
# false, and the trigger could never fire again for the rest of this
# install's life -- even as the new process's own observed max grew past
# what the dead process ever saw. That is staleness in the UNSAFE
# direction for a capacity signal: it under-reports the worst payload,
# exactly what the max basis exists to prevent (reviewer's live
# reproduction: process 1 measured/max 100,000 -> restart -> 12 real
# searches, observed max 150,000 -> zero new records).
#
# Fix: no process-local latch at all. The trigger now re-arms itself by
# comparing LIVE state against the DURABLE stored record's own
# payload_max_chars_measured (see _maybe_derive_capacity) -- it fires
# again exactly when the live observed max exceeds whatever is already on
# disk, regardless of which process wrote that disk record or how long ago.
# This is restart-safe by construction (a fresh process's live max is
# compared against the SAME durable value a long-running process would be
# compared against) and storm-safe without a latch: immediately after a
# successful derivation the stored max equals the live max, so the same
# reading never re-fires -- only a NEW, larger reading does, which is
# inherently sparse. It is also retry-safe (B-2): a failed append leaves
# the stored record unchanged, so the very next cycle re-evaluates the
# identical comparison and tries again, rather than a spent latch
# permanently forfeiting the one attempt it had.


def _capacity_neo4j_allowance_bytes() -> int | None:
    """Operator-configured heap+pagecache when BOTH parse; otherwise the
    compose cap default. Partial config (one of the two set) still falls
    back to the default rather than guessing the missing half. None
    (never silently 0) when the fallback itself fails to parse -- M5, fix
    round: the caller decides what an unparsable allowance means for the
    overall recommendation.

    N3 (fix round 2): a variable that was left UNSET falls back silently --
    that is the normal, expected "not configured" case, nothing failed.
    A variable that WAS set but rejected by _parse_mem_size is different:
    the operator tried to configure this and got silently overridden by the
    default, which used to leave no trace anywhere. One warning names the
    rejected variable (never its value -- these are memory sizes, not
    secrets, but there is no reason to echo a malformed operator string back
    into the log either); the fallback behavior itself is unchanged."""
    heap_raw = os.environ.get("NEO4J_HEAP_MAX")
    pagecache_raw = os.environ.get("NEO4J_PAGECACHE")
    heap = _parse_mem_size(heap_raw)
    pagecache = _parse_mem_size(pagecache_raw)
    if heap_raw is not None and heap is None:
        log.warning(
            "capacity: NEO4J_HEAP_MAX is set but did not parse as a memory "
            "size -- using the CAPACITY_NEO4J_FALLBACK_BYTES default for "
            "the neo4j allowance")
    if pagecache_raw is not None and pagecache is None:
        log.warning(
            "capacity: NEO4J_PAGECACHE is set but did not parse as a "
            "memory size -- using the CAPACITY_NEO4J_FALLBACK_BYTES "
            "default for the neo4j allowance")
    if heap is not None and pagecache is not None:
        return heap + pagecache
    return CAPACITY_NEO4J_FALLBACK_BYTES


def _hardware_fingerprint() -> dict:
    """nproc + MemTotal + GPU presence. Every field fails open to None/False
    on a platform that cannot answer — this must never raise, called from a
    background daemon loop with no request to fail."""
    out: dict = {"nproc": None, "mem_total_bytes": None, "gpu_present": False}
    try:
        out["nproc"] = os.cpu_count()
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    out["mem_total_bytes"] = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError, IndexError):
        pass   # non-Linux, unreadable, or unexpected format — stays None
    try:
        from gpu_load import gpu_probe_available
        out["gpu_present"] = bool(gpu_probe_available())
    except Exception:
        pass   # never let GPU detection block a fingerprint
    return out


def _encoder_config_fingerprint() -> dict:
    """The config subset that changes what the probe measures or what the
    derivation assumes. RERANK_MAX_DOC_CHARS/SEARCH_CANDIDATE_FLOOR come
    from the encoder-sizing modules (imported lazily, matching
    _probe_capability's own pattern, to avoid a load-order dependency).
    Replica counts are the compose knobs that move the encoders onto a GPU
    (ops/postgres_neo4j_limits.yaml CPU_ENCODER_REPLICAS / GPU_ENCODER_
    REPLICAS) — not read anywhere else in this module, so read directly."""
    from dream_telemetry import RERANK_MAX_DOC_CHARS
    from coordinator import SEARCH_CANDIDATE_FLOOR
    return {
        "rerank_max_doc_chars": RERANK_MAX_DOC_CHARS,
        "search_candidate_floor": SEARCH_CANDIDATE_FLOOR,
        # L17 (fix round): strip userinfo (user:pass@) before this persists
        # to the on-disk JSONL log -- same scrub already applied to
        # client-visible error text elsewhere in this module.
        "embedder_url": _scrub_url_credentials(EMBEDDER_URL or ""),
        "reranker_url": _scrub_url_credentials(RERANKER_URL or ""),
        "cpu_encoder_replicas": os.environ.get("CPU_ENCODER_REPLICAS", "1"),
        "gpu_encoder_replicas": os.environ.get("GPU_ENCODER_REPLICAS", "0"),
    }


def _capacity_fingerprint() -> dict:
    return {"hardware": _hardware_fingerprint(),
            "encoder_config": _encoder_config_fingerprint()}


def _capacity_client_ceiling_s(capability: dict | None) -> float:
    """Server-side mirror of memory_bridge.search_ceiling() — see that
    function's docstring for the reasoning; this must stay semantically
    identical (a parity test asserts it) so queue_bound is computed against
    the timeout the client will genuinely apply, not a server guess.

    A-5 (T-01): that includes the PARTIAL-ignorance rule the clients apply.
    When one backend reports a positive projection and the other reports
    `status: "failing"` (or a `projection_stale` block with no projection of
    its own), the known backend's number is only a LOWER bound on the true
    cost — the failing backend's cost is unknown, not zero. So the floor
    under the derivation becomes CAPACITY_SEARCH_TIMEOUT_FALLBACK_S rather
    than CAPACITY_SEARCH_TIMEOUT_FLOOR_S: ignorance of PART of the cost must
    not resolve to a number already known to be too small, exactly as
    ignorance of ALL of it does not. Before this, the fact:1560 shape (an
    embedder that probed in 1.8 s beside a reranker that answered nothing)
    produced 30 s here while the client produced 120 s — the mirror's own
    parity test never saw the case the mechanism exists for.

    R2-N3 extends that to ignorance expressed as ABSENCE: a backend block
    that is missing, empty or not a dict is unknown, not free. ⚠ This makes
    the mirror STRICTER than the clients on those three shapes (the clients
    key on `status`/`projection_stale` only, so they read absence as zero
    cost and can floor at 30 s where this returns 120 s). The divergence is
    in the SAFE direction — the server's queue_bound is computed against a
    more generous ceiling than the client will apply — but it is a real
    difference, and closing it belongs in the clients.

    The clients additionally fold in the gateway's published `capacity`
    block; this mirror does not, and must not — it IS the function that
    produces `capacity.derived.client_ceiling_s`, so reading it back here
    would be circular. Parity is over the capability input."""
    if CAPACITY_SEARCH_TIMEOUT_S > 0:
        return CAPACITY_SEARCH_TIMEOUT_S

    projected, probed, unknown = 0.0, False, False
    for backend in ("reranker", "embedder"):
        block = (capability or {}).get(backend)
        if not isinstance(block, dict) or not block:
            # R2-N3: ignorance expressed as ABSENCE is still ignorance. A
            # block that is missing, empty, or not a dict says nothing about
            # what that backend costs — and treating "nothing" as "zero" is
            # the same mistake this whole mechanism exists to remove. Not
            # reachable from our own probe (it always writes both blocks);
            # very reachable for a client reading a partial or older
            # gateway's /health over the wire.
            unknown = True
            continue
        try:
            value = float(block.get("projected_full_payload_s") or 0)
        except (TypeError, ValueError):
            # A malformed projection is an UNKNOWN cost, not a zero one —
            # fall through to the flag check rather than skipping the block.
            value = 0.0
        if value > 0:
            projected += value
            probed = True
        elif block.get("status") == "failing" or block.get("projection_stale"):
            unknown = True   # this backend's real cost is unknown, not zero

    if not probed:
        derived = CAPACITY_SEARCH_TIMEOUT_FALLBACK_S
    else:
        floor = (CAPACITY_SEARCH_TIMEOUT_FALLBACK_S if unknown
                 else CAPACITY_SEARCH_TIMEOUT_FLOOR_S)
        derived = max(floor, projected * CAPACITY_SEARCH_SAFETY_FACTOR
                      + CAPACITY_SEARCH_OVERHEAD_S)
    return min(derived, CAPACITY_SEARCH_TIMEOUT_MAX_S)


def _capacity_queue_bound(s_mean: float | None, tolerable_wait_s: float) -> int | None:
    """floor(tolerable_wait_s / s_mean), floored at 0 (division of two
    positives already never goes negative; the floor is defensive
    documentation, not a correction). None when s_mean is unknown/non-
    positive (no probe reading yet) — a queue bound of 0 would otherwise
    read as "no room", which is a different claim from "not yet measured".

    H1 (fix round): measured against CAPACITY_TOLERABLE_WAIT_S, an
    operator-stated tolerance, NOT client_ceiling_s — see that constant's
    module-level comment for why the old client_ceiling-based formula was
    circular. 0 here genuinely means "a single search already exceeds the
    tolerable wait" (see single_search_exceeds_wait in the derived record,
    M10) rather than a second, ambiguous meaning of "no room"."""
    if not s_mean or s_mean <= 0:
        return None
    return max(0, int(tolerable_wait_s // s_mean))


def _capacity_recommended_mem_limit_bytes(mem_total_bytes: int | None) -> int | None:
    """MemTotal minus every declared allowance above. None when MemTotal
    itself is unknown, OR (M5, fix round) when ANY declared allowance failed
    to parse -- an operator-set CAPACITY_*_BYTES value _parse_mem_size
    rejected used to silently coerce to 0 via `x or 0`, which SUBTRACTS
    LESS than intended and so INFLATES the recommendation in the dangerous
    direction (recommending more memory for the reranker than the host
    actually has spare). Unknown beats wrong-in-the-dangerous-direction: one
    warning names the offending variable and the whole recommendation comes
    back None rather than a falsely generous number. Floored at 0 rather
    than negative when every value DOES parse — a negative number is not a
    smaller recommendation, it is "no room", which 0 says plainly."""
    if mem_total_bytes is None:
        return None
    named = [
        ("neo4j allowance (NEO4J_HEAP_MAX/NEO4J_PAGECACHE or "
         "CAPACITY_NEO4J_FALLBACK_BYTES)", _capacity_neo4j_allowance_bytes()),
        ("CAPACITY_PG_MEM_ALLOWANCE_BYTES", CAPACITY_PG_MEM_ALLOWANCE_BYTES),
        ("CAPACITY_EMBEDDER_MEM_ALLOWANCE_BYTES", CAPACITY_EMBEDDER_MEM_ALLOWANCE_BYTES),
        ("CAPACITY_GATEWAY_MEM_ALLOWANCE_BYTES", CAPACITY_GATEWAY_MEM_ALLOWANCE_BYTES),
        ("CAPACITY_OS_MEM_MARGIN_BYTES", CAPACITY_OS_MEM_MARGIN_BYTES),
    ]
    subtrahends = []
    for label, value in named:
        if value is None:
            log.warning(
                "capacity: %s did not parse -- memory-limit recommendation "
                "withheld (unknown beats a silently-zeroed, inflated "
                "recommendation)", label)
            return None
        subtrahends.append(value)
    return max(0, mem_total_bytes - sum(subtrahends))


def _capacity_drift_outside_band(current: float | None, basis: float | None,
                                  band_factor: float | None = None) -> bool:
    """True iff `current` sits outside a [1/band, band] ratio of `basis`.
    Exactly AT the factor (ratio == band or == 1/band) is still INSIDE the
    band — "outside a x2 band" means strictly outside, not at-or-beyond, so
    a probe that happens to land on exactly double doesn't flap the trigger
    on rounding. None/non-positive inputs never fire (nothing to compare)."""
    if band_factor is None:
        band_factor = CAPACITY_DRIFT_BAND_FACTOR
    if not current or not basis or current <= 0 or basis <= 0 or band_factor <= 1:
        return False
    ratio = current / basis
    return ratio > band_factor or ratio < (1.0 / band_factor)


def _capacity_payload_stats(coordinator) -> dict:
    """Read-only snapshot of the coordinator's own cumulative rerank
    payload counters (fact:1441 — coordinator._rerank_payload_chars_total /
    _rerank_payload_docs_total / _rerank_payload_chars_max, plus
    _rerank_successes / _rerank_failures for the sample count, all already
    accumulated for GET /memory/telemetry — reused verbatim here, not a
    second collection mechanism).

    `coordinator` may be None (a caller that never wired it through, or
    every existing test in this suite, none of which pass one) — that is
    the same as "zero samples", never an error. Every attribute read is
    guarded: a mocked/partial coordinator missing one, or a non-numeric
    value on any of them, degrades to zero rather than raising -- this is
    an observability derivation and must fail open like the rest of this
    module (Group 3)."""
    samples = chars_total = docs_total = chars_max = 0
    if coordinator is not None:
        try:
            successes = int(getattr(coordinator, "_rerank_successes", 0) or 0)
            failures = int(getattr(coordinator, "_rerank_failures", 0) or 0)
            samples = successes + failures
            chars_total = int(getattr(coordinator, "_rerank_payload_chars_total", 0) or 0)
            docs_total = int(getattr(coordinator, "_rerank_payload_docs_total", 0) or 0)
            chars_max = int(getattr(coordinator, "_rerank_payload_chars_max", 0) or 0)
        except (TypeError, ValueError):
            samples = chars_total = docs_total = chars_max = 0
    mean_chars_per_search = (chars_total / samples) if samples > 0 else None
    max_chars_per_search = chars_max if samples > 0 and chars_max > 0 else None
    return {
        "samples": samples,
        "chars_total": chars_total,
        "docs_total": docs_total,
        "mean_chars_per_search": mean_chars_per_search,
        "max_chars_per_search": max_chars_per_search,
    }


def _probe_measured_at(capability: dict | None, block: dict) -> str | None:
    """When the throughput reported for one backend was actually measured.

    `last_ok_at` is the authority whenever the block has one (the merge
    stamps it on every probed block, fresh or carried). Falling back to the
    snapshot's own `probed_at` keeps this honest for a capability dict that
    never went through the merge — a caller predating it, or a record
    rebuilt from an older log."""
    stamp = block.get("last_ok_at") if isinstance(block, dict) else None
    return stamp or (capability or {}).get("probed_at")


def _build_capacity_record(capability: dict | None, fingerprint: dict,
                            trigger: str, coordinator=None) -> dict:
    """Assemble one capacity derivation record. `capability` is the SAME
    dict _probe_capability() produced this cycle (capability_snapshot()'s
    shape) — s_mean_s reuses its reranker.projected_full_payload_s verbatim
    rather than recomputing a second model; this field's meaning is
    UNCHANGED and always equals that theoretical projection, exactly as
    before this change, regardless of `coordinator` — every caller that
    predates this parameter (including every existing test) gets identical
    output.

    NOTE on the probe's own model vs the real candidate pool: the probe
    projects onto 20 x RERANK_MAX_DOC_CHARS (see _probe_capability), a fixed
    worst-case count that measurement on the reference workstation showed
    is ~4.9x the largest real payload observed and ~6.9x the mean (operator
    rulings, 2026-08-23 — see CAPACITY_PAYLOAD_MIN_SAMPLES's module-level
    comment for the numbers and the corrected ratios). The REAL per-search
    candidate pool is max(SEARCH_CANDIDATE_FLOOR, limit) + 2
    (coordinator.py's Tier-1 fetch: the vector-search LIMIT plus the Tier-3
    summary and the deep-dive lookup that ride along).

    `coordinator` (optional — None on a fresh install, a process that
    hasn't wired it through, or any caller that predates this parameter)
    supplies the OBSERVED payload stats via _capacity_payload_stats. When
    at least CAPACITY_PAYLOAD_MIN_SAMPLES real searches have been served,
    the derived record's queue_bound and single_search_exceeds_wait are
    computed from the observed MAXIMUM payload (s_max_measured_s) instead
    of the fixed theoretical one — a capacity signal must stay
    worst-case, so the average (s_mean_measured_s, still reported as cheap
    informational context) never feeds these two fields. s_mean_s ITSELF
    is never touched, so a reader who only ever looked at s_mean_s keeps
    seeing exactly what it always meant.

    Why it is still safe to leave queue_bound/single_search_exceeds_wait
    under their existing names even though the basis feeding them can now
    change: `payload_basis` ships in the SAME record and is mandatory
    (never omitted), so a reader can always tell which basis actually
    drove a given value — a name is only a problem to reuse when its
    meaning changes SILENTLY; here it cannot, because the record is
    self-describing. `payload_basis_sample_count` always reports the true
    sample count regardless of which basis was used (NOT 0 on
    "theoretical" — an earlier draft of this comment said otherwise; the
    code was already right, only the comment was wrong)."""
    reranker = (capability or {}).get("reranker") or {}
    embedder = (capability or {}).get("embedder") or {}
    # UNCHANGED meaning: the fixed theoretical full-payload projection,
    # verbatim from the probe, exactly as before this change.
    s_mean_theoretical = reranker.get("projected_full_payload_s")
    reranker_chars_per_s = reranker.get("throughput_chars_s")

    payload_stats = _capacity_payload_stats(coordinator)
    have_enough_samples = payload_stats["samples"] >= CAPACITY_PAYLOAD_MIN_SAMPLES
    have_throughput = bool(reranker_chars_per_s and reranker_chars_per_s > 0)

    # Informational only (ruling 1) -- computed whenever there is enough
    # data to trust it, but NEVER feeds queue_bound/single_search_exceeds_
    # wait. Reported purely as useful context alongside the max.
    s_mean_measured = None
    try:
        if (have_enough_samples and have_throughput
                and payload_stats["mean_chars_per_search"] is not None):
            s_mean_measured = round(
                payload_stats["mean_chars_per_search"] / reranker_chars_per_s, 1)
    except (TypeError, ZeroDivisionError):
        s_mean_measured = None

    # RULING 1: the basis. A capacity signal must stay worst-case, so this
    # -- not the mean above -- is what feeds queue_bound/single_search_
    # exceeds_wait once trusted.
    s_max_measured = None
    try:
        if (have_enough_samples and have_throughput
                and payload_stats["max_chars_per_search"] is not None):
            s_max_measured = round(
                payload_stats["max_chars_per_search"] / reranker_chars_per_s, 1)
    except (TypeError, ZeroDivisionError):
        s_max_measured = None

    if s_max_measured is not None:
        payload_basis = "measured"
        effective_s_mean = s_max_measured
    else:
        payload_basis = "theoretical"
        effective_s_mean = s_mean_theoretical

    # H1: queue_bound is measured against the operator's own tolerable-wait
    # setting, NOT client_ceiling_s -- client_ceiling_s is still computed
    # and reported below (informative: what the client will itself time out
    # at) but no longer feeds this calculation. See CAPACITY_TOLERABLE_WAIT_S's
    # module-level comment for why the old formula was circular.
    #
    # Operator ruling (2026-08-23): queue_bound and single_search_exceeds_
    # wait are now derived from `effective_s_mean` -- the observed-MAX
    # basis when CAPACITY_PAYLOAD_MIN_SAMPLES is met, else the same
    # theoretical basis these two fields always used. Their CONTRACT ("is
    # a single search's projected rerank time within the operator's
    # tolerable wait") is unchanged; what changes is which population the
    # basis describes (worst-case theoretical vs worst-case OBSERVED,
    # never the average -- see ruling 1 above). s_mean_s (below) is never
    # altered, precisely so a fixed meaning stays available even while
    # these two derived fields adopt the better input -- see
    # _build_capacity_record's own docstring for why reusing these names is
    # still honest (payload_basis is mandatory and always present).
    client_ceiling = _capacity_client_ceiling_s(capability)
    queue_bound = _capacity_queue_bound(effective_s_mean, CAPACITY_TOLERABLE_WAIT_S)
    # M10: makes explicit what queue_bound == 0 means, since "not yet
    # measured" (None) and "one search already exceeds the tolerable wait"
    # (0) would otherwise both read as "no usable number".
    single_search_exceeds_wait = (
        None if not effective_s_mean or effective_s_mean <= 0
        else effective_s_mean > CAPACITY_TOLERABLE_WAIT_S
    )
    mem_total = fingerprint.get("hardware", {}).get("mem_total_bytes")
    recommended_mem_limit = _capacity_recommended_mem_limit_bytes(mem_total)
    mean_chars_measured = payload_stats["mean_chars_per_search"]
    if mean_chars_measured is not None:
        mean_chars_measured = round(mean_chars_measured, 1)
    max_chars_measured = payload_stats["max_chars_per_search"]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "fingerprint": fingerprint,
        "probe": {
            "reranker_chars_per_s": reranker.get("throughput_chars_s"),
            # H3: the probe's own status rides along with the reading it
            # describes, so a future comparison can refuse to trust a
            # reading that was never actually "ok" (see _maybe_derive_
            # capacity's probe_drift guard).
            "reranker_status": reranker.get("status"),
            "embedder_chars_per_s": embedder.get("throughput_chars_s"),
            "probed_at": (capability or {}).get("probed_at"),
            # A-3 (ADV-5, Group 3): once a reading can be CARRIED from an
            # earlier cycle, `<backend>_chars_per_s` stamped with this
            # cycle's `probed_at` alone would silently change meaning under
            # an unchanged name -- in a record that is appended to the
            # durable capacity.jsonl log. So each throughput now travels
            # with the timestamp it was actually MEASURED at (equal to
            # `probed_at` on a fresh cycle, earlier on a carried one), and
            # `probe_stale` says outright that this block mixes cycles.
            "reranker_measured_at": _probe_measured_at(capability, reranker),
            "embedder_measured_at": _probe_measured_at(capability, embedder),
            "probe_stale": bool(reranker.get("projection_stale")
                                or embedder.get("projection_stale")),
        },
        "derived": {
            # UNCHANGED meaning -- see this function's docstring. Always the
            # theoretical full-payload projection, regardless of basis.
            "s_mean_s": s_mean_theoretical,
            # NEW (additive, ruling 1): the projection computed over the
            # coordinator's OBSERVED MAXIMUM rerank payload instead of the
            # theoretical worst case. This -- NOT s_mean_measured_s below
            # -- is what feeds queue_bound/single_search_exceeds_wait when
            # payload_basis is "measured". None until CAPACITY_PAYLOAD_
            # MIN_SAMPLES real searches have been served this process's
            # lifetime (always None with no coordinator wired through --
            # e.g. a fresh install, or any pre-existing caller of this
            # function). MONOTONIC non-decreasing for this process's
            # lifetime -- see CAPACITY_PAYLOAD_MIN_SAMPLES's module-level
            # comment for why that is the safe direction, not a defect.
            "s_max_measured_s": s_max_measured,
            # NEW (additive): the same projection over the OBSERVED MEAN
            # instead -- cheap, useful CONTEXT only. Never feeds
            # queue_bound/single_search_exceeds_wait (ruling 1: an
            # average-case basis would under-project a search at the
            # observed max). Same None-until-enough-samples gating as
            # s_max_measured_s above.
            "s_mean_measured_s": s_mean_measured,
            # NEW (additive): which basis actually fed queue_bound /
            # single_search_exceeds_wait THIS record. A reader must never
            # have to guess -- this field is why reusing the existing
            # names for those two fields is still honest (see docstring).
            "payload_basis": payload_basis,
            # NEW (additive): real searches (rerank_successes_total +
            # rerank_fallbacks_total at the coordinator) THIS PROCESS has
            # served, reported UNCONDITIONALLY -- true regardless of
            # whether payload_basis is "measured" or "theoretical" (a
            # "theoretical" reading can still carry a nonzero count below
            # CAPACITY_PAYLOAD_MIN_SAMPLES; it is never forced to 0).
            "payload_basis_sample_count": payload_stats["samples"],
            # NEW (additive): the observed mean rerank_payload_chars per
            # real search this process has served. None on zero samples --
            # never a false 0, which would read as "measured a zero-byte
            # payload" rather than "nothing observed yet".
            "payload_mean_chars_measured": mean_chars_measured,
            # NEW (additive, ruling 1): the observed MAXIMUM
            # rerank_payload_chars per real search -- the raw number behind
            # s_max_measured_s. None on zero samples, same discipline as
            # the mean above. MONOTONIC non-decreasing for this process's
            # lifetime (see CAPACITY_PAYLOAD_MIN_SAMPLES's module-level
            # comment).
            "payload_max_chars_measured": max_chars_measured,
            "client_ceiling_s": client_ceiling,
            "queue_bound": queue_bound,
            # N2 (fix round 2): the tolerance queue_bound was actually
            # measured against travels WITH the record it produced -- a
            # reader (postflight, /health, a future dashboard) must not have
            # to know today's CAPACITY_TOLERABLE_WAIT_S default separately
            # to make sense of a stored queue_bound; the record is
            # self-describing even if the operator's setting changes later.
            "tolerable_wait_s": CAPACITY_TOLERABLE_WAIT_S,
            "single_search_exceeds_wait": single_search_exceeds_wait,
            # A-4 (ADV-6): there is deliberately NO staleness flag in this
            # block. `capacity` on /health is the last DERIVED record, and
            # derivation fires on rare triggers -- so during an outage this
            # block is frozen at its last healthy derivation and a flag here
            # would read "fresh" during exactly the outage it exists to
            # expose. Liveness of the projection is reported ONLY where it is
            # actually re-evaluated every cycle: `backend_capability.<backend>
            # .projection_stale` / `.projection_age_s`. Within THIS record,
            # `probe.probe_stale` above describes the reading it was derived
            # from, alongside `timestamp` (when the record was derived).
            "recommended_reranker_mem_limit_bytes": recommended_mem_limit,
        },
    }


def _read_capacity_records_sync(path: str) -> list[dict]:
    """Tolerant JSON-lines reader: a malformed line is skipped, never fatal
    — the log is an observability trail, not a transaction log. Missing
    file returns []."""
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return []
    out: list[dict] = []
    try:
        with open(expanded, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _write_capacity_records_sync(path: str, records: list[dict]) -> None:
    """Atomic replace via a same-directory temp file opened 0600 directly
    (log_hygiene's FILE_MODE) — os.replace preserves the source inode's
    permission bits, so the final file is never briefly world-readable
    under the process umask the way `open(tmp, 'w')` then chmod would be.

    M9 (fix round): the temp-file open now carries O_NOFOLLOW too, mirroring
    log_hygiene.secure_path's own reasoning -- a symlink pre-planted at the
    `.tmp` path by a different-uid actor in a relocated (CAPACITY_LOG_PATH
    under /tmp or another shared dir) log directory must not get this
    process to write through it."""
    expanded = os.path.expanduser(path)
    _chmod_created_ancestors(Path(expanded).parent)
    tmp = f"{expanded}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, expanded)
    secure_path(expanded)   # tightens perms if the target pre-existed at 0644


async def _append_capacity_record(record: dict) -> None:
    """Append + prune to the last CAPACITY_LOG_MAX_RECORDS, off the event
    loop (log_hygiene.AsyncLineWriter's reasoning — disk I/O never runs
    inline — but this needs read-modify-write for pruning, which
    AsyncLineWriter's append-only writer does not do)."""
    def _do() -> None:
        records = _read_capacity_records_sync(CAPACITY_LOG_PATH)
        records.append(record)
        # M4 (fix round): records[-0:] is the WHOLE list, not zero records --
        # a CAPACITY_LOG_MAX_RECORDS of 0 (or a negative value) used to keep
        # every record ever written instead of pruning. Clamp to 1: always
        # keep at least the latest.
        max_records = CAPACITY_LOG_MAX_RECORDS if CAPACITY_LOG_MAX_RECORDS > 0 else 1
        records = records[-max_records:]
        _write_capacity_records_sync(CAPACITY_LOG_PATH, records)
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do)
    except RuntimeError:
        _do()   # no running loop (sync caller/test) — inline, matches
                # AsyncLineWriter.write's own fallback


def _last_capacity_record() -> dict | None:
    records = _read_capacity_records_sync(CAPACITY_LOG_PATH)
    return records[-1] if records else None


def capacity_snapshot() -> dict | None:
    """The latest derivation record, or None before any exists (this
    process's own derivation OR a prior process's, lazily loaded from disk
    once). Read by /health — never asserted as present on no data."""
    global _capacity_latest, _capacity_latest_loaded_from_disk
    if _capacity_latest is None and not _capacity_latest_loaded_from_disk:
        _capacity_latest_loaded_from_disk = True
        try:
            _capacity_latest = _last_capacity_record()
        except Exception:
            pass   # fail-open — a corrupt/unreadable log must not break /health
    return dict(_capacity_latest) if _capacity_latest is not None else None


def _log_capacity_change(trigger: str, last: dict | None, record: dict) -> None:
    """One line per re-derivation.

    M7 (fix round): the very first record this log has EVER held is not an
    alarm -- there is nothing prior to compare against, so it logs at INFO
    as "capacity baseline established" and carries no re-run-postflight
    tail. Every other trigger means something ACTUALLY changed (a hardware/
    config mismatch, or measured drift) and keeps the louder WARNING path
    with that tail (fact:1425 A2: every hardware-era change should produce
    a fresh postflight verification, and this line is where the operator
    learns that -- log only, the gateway never runs postflight itself).

    N1(b) (fix round 2): basis_recovery is likewise informational, not an
    alarm -- it means the instrument just HEALED itself from an unusable
    basis, which is good news the operator did nothing to cause and need do
    nothing about. No re-run-postflight tail either."""
    def _mib(b):
        return f"{b / (1024 ** 2):.0f}MiB" if isinstance(b, (int, float)) else "?"

    new_d = record["derived"]

    if trigger == "first_derivation":
        log.info(
            "capacity baseline established: s_mean %s s, queue_bound %s, "
            "reranker_mem_limit_bytes %s",
            new_d.get("s_mean_s"), new_d.get("queue_bound"),
            new_d.get("recommended_reranker_mem_limit_bytes"),
        )
        return

    if trigger == "basis_recovery":
        log.info(
            "capacity basis recovered: deriving from the first healthy "
            "probe (s_mean %s s, queue_bound %s, reranker_mem_limit_bytes "
            "%s)",
            new_d.get("s_mean_s"), new_d.get("queue_bound"),
            new_d.get("recommended_reranker_mem_limit_bytes"),
        )
        return

    old_hw = (last or {}).get("fingerprint", {}).get("hardware", {}) or {}
    new_hw = record["fingerprint"]["hardware"]
    old_d = (last or {}).get("derived", {}) or {}
    old_probe = (last or {}).get("probe", {}) or {}
    new_probe = record["probe"]

    if trigger == "probe_drift":
        # M7: the number that actually MOVED for this trigger is reranker
        # chars/s -- MemTotal is unchanged in a drift event, so leading with
        # it read as "nothing happened" to an operator scanning the log.
        headline = (f"capacity basis changed ({trigger}): reranker "
                    f"{old_probe.get('reranker_chars_per_s')}->"
                    f"{new_probe.get('reranker_chars_per_s')} chars/s")
    else:
        headline = (f"capacity basis changed ({trigger}): MemTotal "
                    f"{_mib(old_hw.get('mem_total_bytes'))}->"
                    f"{_mib(new_hw.get('mem_total_bytes'))}")

    log.warning(
        "%s -- re-derived: s_mean %s->%s s, queue_bound %s->%s, "
        "reranker_mem_limit_bytes %s->%s -- re-run postflight to verify and "
        "re-baseline on this hardware: bash shared-memory/scripts/postflight.sh",
        headline,
        old_d.get("s_mean_s"), new_d.get("s_mean_s"),
        old_d.get("queue_bound"), new_d.get("queue_bound"),
        old_d.get("recommended_reranker_mem_limit_bytes"),
        new_d.get("recommended_reranker_mem_limit_bytes"),
    )


async def _maybe_derive_capacity(capability: dict, coordinator=None) -> None:
    """Called every capability-probe cycle. Decides which of the passive
    triggers (if any) fires, derives + stores + logs on a hit, and NEVER
    raises — this rides the same observability path _probe_capability does,
    so a bug here must not take down the probe daemon (Group 3).

    `coordinator` (optional) is passed straight through to
    _build_capacity_record so the measured-payload basis can read its
    cumulative rerank counters — see that function's docstring."""
    global _capacity_first_probe_done
    try:
        fingerprint = _capacity_fingerprint()
        last = _last_capacity_record()
        is_first = not _capacity_first_probe_done
        _capacity_first_probe_done = True

        current_reranker = (capability or {}).get("reranker") or {}
        current_status = current_reranker.get("status")

        trigger = None
        if is_first:
            if last is None:
                # N1(a) (fix round 2): no prior record ANYWHERE -- this is a
                # first-ever baseline, not a mismatch (there is nothing to
                # have mismatched against). But a not-ok probe (warming
                # compose stack, connection refused, fast HTTP error) must
                # NOT be allowed to establish that baseline: a not-ok basis
                # blocks every later trigger from ever firing again --
                # probe_drift requires an "ok" stored basis, config_change
                # has nothing changed to compare, and a later restart's
                # fingerprint still matches -- so a single bad first probe
                # used to freeze the instrument permanently with no operator
                # remedy. Defer silently (one INFO log) instead; the next
                # healthy probe cycle derives the baseline normally.
                if current_status != "ok":
                    log.info(
                        "capacity baseline deferred -- reranker probe not "
                        "ok yet; will derive on the first healthy probe")
                    return
                trigger = "first_derivation"
            elif last.get("fingerprint") != fingerprint:
                trigger = "gateway_start_fingerprint_mismatch"
        else:
            # config_change: encoder_config is module-level state fixed for
            # this process's whole lifetime (RERANK_MAX_DOC_CHARS et al. are
            # read once at import), so within ONE process this can only ever
            # equal what trigger 1 already checked. It still earns its own
            # cheap check on every cycle because the LOG is shared state: a
            # differently-configured process (a rolling restart mid-flight,
            # or CAPACITY_LOG_PATH pointed at a shared location) can have
            # written the last record, and that mismatch should surface on
            # the very next cycle rather than wait for this process's own
            # next restart.
            if last is None:
                # Log file cleared/rotated out from under a running process,
                # OR this process's own first cycle never found one either.
                # M7: nothing prior exists to have mismatched against, so
                # this is a fresh baseline too, not an alarm. N1(a): the
                # same not-ok guard as the is_first branch above applies
                # here too -- a not-ok probe must not become the first
                # stored basis via this path either.
                if current_status != "ok":
                    log.info(
                        "capacity baseline deferred -- reranker probe not "
                        "ok yet; will derive on the first healthy probe")
                    return
                trigger = "first_derivation"
            elif last.get("fingerprint", {}).get(
                    "encoder_config") != fingerprint.get("encoder_config"):
                trigger = "config_change"
            else:
                probe_block = (last.get("probe") or {})
                basis = probe_block.get("reranker_chars_per_s")
                basis_status = probe_block.get("reranker_status")
                current = current_reranker.get("throughput_chars_s")
                # H3: a probe that did not answer "ok" produces fantasy
                # throughput (a fast HTTP error can read as near-zero
                # latency), so it must never fire drift and must never
                # become the new basis. Both sides are guarded: the stored
                # basis must have been recorded under an "ok" reading too,
                # or a genuinely-recovered probe would compare against a
                # poisoned number forever.
                if (basis_status == "ok" and current_status == "ok"
                        and _capacity_drift_outside_band(current, basis)):
                    trigger = "probe_drift"

        if trigger is None and last is not None:
            # N1(b) (fix round 2): recovers an already-stored not-ok basis
            # (or a status-less legacy record predating this field -- absent
            # reads the same as not-ok here) the moment a healthy probe shows
            # up. Mirror image of (a): the only remedy for a basis that was
            # ALREADY poisoned before this fix landed, since (a) alone only
            # stops NEW poisoning. No operator action needed -- the next
            # healthy probe cycle heals it on its own. Only reached once
            # every trigger above has had its say: a fingerprint/config
            # change already produces a fresh (healthy) record on its own,
            # so this exists specifically for the "nothing else moved" case
            # that used to go permanently, silently stuck.
            basis_status = (last.get("probe") or {}).get("reranker_status")
            if basis_status != "ok" and current_status == "ok":
                trigger = "basis_recovery"

        # Ruling 2 (operator, 2026-08-23), fixed for B-1/B-2/B-3 (reviewer,
        # 2026-08-23): without this, the feature added by rulings elsewhere
        # in this module is INERT in normal operation -- verified live: a
        # fresh theoretical/samples-0 baseline plus six real searches left
        # the stored record reading theoretical/samples 0, because a
        # capacity record is only ever (re)computed on one of the triggers
        # above, and at each of those moments the payload counters are at
        # or near zero. "measured" was reachable only incidentally, if a
        # probe_drift/config_change happened to fire after traffic had
        # already accumulated.
        #
        # Checked LAST, only when nothing else already decided to fire this
        # cycle -- it never fights or reorders first_derivation /
        # gateway_start_fingerprint_mismatch / config_change / probe_drift /
        # basis_recovery above; it only ever fills a cycle those would
        # otherwise leave silent.
        #
        # B-1 (HIGH, fixed): the original version of this trigger gated on
        # a PROCESS-LOCAL one-shot latch plus "the stored basis is still
        # theoretical". After any restart with unchanged hardware/config
        # (no fingerprint mismatch -> no other trigger fires either), the
        # stored record already said "measured" from the PREVIOUS process's
        # life -- so guard (b) was permanently false for the rest of this
        # install's life, even as the NEW process's own observed max grew
        # past what the dead process ever saw. That is staleness in the
        # UNSAFE direction for a capacity signal (under-reporting the worst
        # payload) -- exactly what the max basis exists to prevent.
        #
        # Fixed by dropping the process-local latch AND the "theoretical
        # only" restriction entirely. The trigger now compares LIVE state
        # against the DURABLE stored record's own payload_max_chars_
        # measured, regardless of which process wrote that stored record or
        # how long ago: it fires whenever the live observed max EXCEEDS the
        # max already on disk (or the disk has none yet, i.e. still
        # theoretical). This is:
        #   - restart-safe by construction: a freshly restarted process
        #     with fresh (zero) counters is compared against the exact same
        #     durable value a long-running process would be compared
        #     against, so "a restarted process with a larger observed max"
        #     re-derives correctly -- there is no process-local memory to
        #     go stale.
        #   - storm-safe without any latch: immediately after a successful
        #     derivation the stored max equals the live max, so the
        #     identical reading can never re-fire on the next cycle -- only
        #     a NEW, larger reading does, which is inherently sparse (the
        #     max is monotonic per process, so within one process this
        #     fires at most once per new high-water mark; across a restart
        #     it fires again only if the new process's traffic genuinely
        #     exceeds the old one's worst case).
        #   - a smaller live max than the stored one deliberately does NOT
        #     re-fire and does NOT regress the stored value -- the stored
        #     max stays the more conservative (larger, safer) figure until
        #     real traffic actually exceeds it. This is not a residual bug:
        #     the capacity signal must never UNDER-report, and a real
        #     historical worst-case number is not "wrong" merely because
        #     the process that observed it has since restarted.
        #   - B-2 (MEDIUM, fixed as a consequence): retry-safe with no
        #     special-casing needed. A failed _append_capacity_record leaves
        #     the durable max unchanged, so the very next cycle re-evaluates
        #     the identical "live > stored" comparison and tries again --
        #     there is no one-shot flag to have been spent prematurely.
        #   - B-3 (LOW, fixed): requiring live_payload["max_chars_per_
        #     search"] is not None closes the all-empty-payload edge case
        #     (chars_max stays 0 despite samples > 0) that used to satisfy
        #     the old guards and still yield "theoretical".
        # Still gated on current_status == "ok", a positive current
        # throughput reading, and enough samples -- the same preconditions
        # _build_capacity_record itself needs to actually produce a
        # "measured" basis, checked here too so a doomed-to-fail attempt is
        # never even tried (e.g. a reranker probe that is momentarily down).
        if trigger is None and last is not None:
            live_payload = _capacity_payload_stats(coordinator)
            current_throughput = current_reranker.get("throughput_chars_s")
            if (live_payload["samples"] >= CAPACITY_PAYLOAD_MIN_SAMPLES
                    and current_status == "ok"
                    and current_throughput and current_throughput > 0
                    and live_payload["max_chars_per_search"] is not None):
                last_max = (last.get("derived") or {}).get(
                    "payload_max_chars_measured")
                if last_max is None or live_payload["max_chars_per_search"] > last_max:
                    trigger = "payload_threshold_crossed"

        if trigger is None:
            return
        record = _build_capacity_record(capability, fingerprint, trigger, coordinator)
        await _append_capacity_record(record)
        global _capacity_latest
        _capacity_latest = record
        _log_capacity_change(trigger, last, record)
    except Exception as exc:
        log.warning("capacity derivation failed: %s", exc)


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
    # R0-I (decision:1424): the latest CAPACITY derivation record, if any —
    # a top-level ADDITIVE key, never nested under backend_capability (that
    # dict is the raw probe reading; this is what was DERIVED from it plus
    # the hardware/config fingerprint). None until the first derivation of
    # this deployment's lifetime lands. REPORT ONLY — see the section this
    # snapshot function lives in for the "never limits a request" invariant.
    checks["capacity"] = capacity_snapshot()

    # Reasoning-LLM backend pool — probe each; "llm" is ok if ANY is up (the pool
    # tolerates a down backend). Per-backend statuses are reported for observability;
    # a reserved judge backend is flagged. A single-backend deployment just shows one.
    backend_status: dict[str, str] = {}
    for b in LLM_BACKENDS:
        try:
            async with proxy.session.get(_v1_models_probe_url(b), timeout=ClientTimeout(total=2.0)) as r:
                if r.status < 400:
                    backend_status[b] = "ok"
                elif LLM_BACKEND_TOKENS.get(b) is not None and r.status in (401, 403):
                    # H-1/H-2: this is a BARE probe — no Authorization header
                    # is attached (has_credential is deliberately never used
                    # to authenticate a liveness poll, see this section's own
                    # header comment: no per-poll provider-key probing). A
                    # 401/403 from a CREDENTIALED backend therefore means the
                    # server ANSWERED — this file's own liveness definition
                    # ("answered <500 = alive") — its rejection of an
                    # unauthenticated probe is correct auth behavior, not
                    # downness. H-1: the unauthenticated probe never carried
                    # key-validity information anyway; llm_faults.credential
                    # on a REAL call is that signal. Genuinely down (connect
                    # error / 5xx) is unaffected by this branch.
                    backend_status[b] = "ok"
                else:
                    backend_status[b] = f"http_{r.status}"
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
    # Gate on ANY configured backend, not "more than one" — the original
    # `> 1` read single-backend as "legacy default, nothing interesting to
    # show", but that meaning inverted: a cloud-only fleet (the
    # VRAM-constrained configuration our own docs recommend) IS a
    # single-backend fleet, and the old gate made it vanish from the
    # monitor entirely. This is a PRESENCE change only — additive, no
    # existing key's meaning changes; sections below now also appear for a
    # one-backend fleet.
    if LLM_BACKENDS:
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

    # Routing telemetry (Group 3, fact:1314 shape — flat, additive, each
    # counter paired with its own last-event ts; no existing key changes
    # meaning). Surfaced regardless of pool size — meaningful even for a
    # single role-scoped backend.
    checks["llm_routing"] = {
        "routed_role_extract": _llm_routed_by_role.get("extract", 0),
        "routed_role_extract_last_ts": _llm_routed_by_role_last_ts.get("extract"),
        "routed_role_verify": _llm_routed_by_role.get("verify", 0),
        "routed_role_verify_last_ts": _llm_routed_by_role_last_ts.get("verify"),
        "routed_role_judge": _llm_routed_by_role.get("judge", 0),
        "routed_role_judge_last_ts": _llm_routed_by_role_last_ts.get("judge"),
        "routing_no_eligible_backend": _routing_no_eligible_backend_count,
        "routing_no_eligible_backend_last_ts": _routing_no_eligible_backend_last_ts,
        "routing_fit_rejected": _routing_fit_rejected_count,
        "routing_fit_rejected_last_ts": _routing_fit_rejected_last_ts,
        "routing_backend_at_capacity": _routing_backend_at_capacity_count,
        "routing_backend_at_capacity_last_ts": _routing_backend_at_capacity_last_ts,
    }
    # Post-review addition A: per-backend cumulative token counters, IN-
    # PROCESS ONLY (reset on restart — deliberate, the ts pairing is what
    # makes a restart-aware delta computable; see the README proposal's B2
    # note for the operator-facing framing of this).
    if LLM_BACKENDS:
        checks["llm_token_usage"] = {
            b: {
                "tokens_prompt_total": _llm_tokens_prompt_total.get(b, 0),
                "tokens_completion_total": _llm_tokens_completion_total.get(b, 0),
                "tokens_last_ts": _llm_tokens_last_ts.get(b),
            }
            for b in LLM_BACKENDS
        }
    # New instrument: per-backend LLM request latency (local-vs-online
    # comparison). Same lifecycle as llm_token_usage above — IN-PROCESS
    # ONLY, reset on restart, the ts pairing is what makes a restart-aware
    # delta computable. latency_sum_s + requests_total makes the average
    # derivable on the read side without the gateway ever caring what
    # "average" means; requests_failed_total is counted separately so a
    # string of failures (fast, low-latency) doesn't dilute the success
    # average.
    if LLM_BACKENDS:
        checks["llm_latency"] = {
            b: {
                "requests_total": _llm_requests_total.get(b, 0),
                "requests_failed_total": _llm_requests_failed_total.get(b, 0),
                "latency_sum_s": round(_llm_latency_sum_s.get(b, 0.0), 6),
                "latency_max_s": round(_llm_latency_max_s.get(b, 0.0), 6),
                "latency_last_ts": _llm_latency_last_ts.get(b),
            }
            for b in LLM_BACKENDS
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
             "model": LLM_BACKEND_MODELS.get(b),
             # Model-attributes routing descriptor fields (additive) — never
             # used by the monitor for routing math, only display; the
             # gateway itself stays price-agnostic (M-4).
             "roles": sorted(LLM_BACKEND_ROLES[b]) if LLM_BACKEND_ROLES.get(b) else None,
             "n_ctx": LLM_BACKEND_NCTX.get(b),
             "private_ok": LLM_BACKEND_PRIVATE_OK.get(b, True),
             "max_inflight": LLM_BACKEND_MAX_INFLIGHT.get(b),
             "price_per_mtok_in": LLM_BACKEND_PRICE_IN.get(b),
             "price_per_mtok_out": LLM_BACKEND_PRICE_OUT.get(b)}
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

        # pgvector version + hnsw.iterative_scan (decision:1584/fact:1583) —
        # flat additive key, read straight off the coordinator's own startup
        # probe (coordinator.py start()); never re-probed here, so /health
        # stays DB-free like the rest of this block. "version": null means the
        # probe failed or the extension was unreadable — that is itself the
        # signal (iterative_scan is then always False, the safe default).
        checks["pgvector"] = {
            "version": getattr(coordinator, "pgvector_version", None),
            "iterative_scan": bool(getattr(coordinator, "hnsw_iterative_scan", False)),
        }

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


def _health_role_for(agent_name: str) -> str:
    """`read` or `write` for an authenticated /health caller (A-4).

    Two-valued by ruling, over a three-valued underlying vocabulary: it
    answers the question a client actually has — *may I write?* — rather than
    exposing the roster's internal spelling.

    It goes through `effective_role`, never a bare `_AGENT_ROLES` lookup, and
    that is the whole reason this is a function. `read_only_agents()` confines
    an identity REGARDLESS of what AGENT_ROLES declares, so a raw read of the
    map would report `write` for an identity the gateway 403s on every write
    route — the exact false reassurance this key exists to remove.
    """
    return ("read"
            if effective_role(agent_name, _AGENT_ROLES.get(agent_name)) == "read"
            else "write")


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

    A-4 — WHO AM I, AND WHAT MAY I DO? Two additive keys, `agent` and
    `role`, on the AUTHENTICATED payload only. A client holding a token
    could not previously learn either without attempting a write and reading
    the refusal, which is a poor way to find out: a read-only token gets a
    403 that looks like a permissions bug to anyone who did not already know
    the token was confined. `doctor` can now say it plainly.

    ⛔ THE OTHER TWO SHAPES ARE UNCHANGED, and that is the whole constraint.
    An ANONYMOUS caller on an auth-configured install still gets the
    three-key slim payload — adding an identity to a response served to
    someone who proved no identity would be absurd, and the slimming
    contract test asserts that shape exactly. An AUTH-OFF install still gets
    today's full payload with NEITHER key: there is no token registry there,
    so every caller is the same unnamed everyone, and emitting `agent: null`
    / `role: "write"` would dress an absence up as an answer. Absent means
    "this install has no identities", which is true and useful.

    ⚠ `role` is two-valued by ruling — `read` for an identity confined to
    the read allowlist, `write` for everyone else. An `admin` token reaches
    here (`/health` is in `_UNPROTECTED_PATHS`, so the role gate never runs
    on it) and reports `write`, which OVERSTATES it: an admin token is
    confined to `/admin/*` and cannot save either. Raised as a finding
    rather than answered here — the vocabulary is the operator's.

    HTTP 200: embedder + reranker both reachable (save/search path healthy).
    HTTP 503: at least one critical backend is down — computed identically
    for every caller; an anonymous caller learns the VERDICT, not why.
    """
    proxy: AsyncHiveMindProxy = request.app["proxy"]
    checks = await _health_probe_cached(proxy, request.app.get("coordinator"))
    critical_ok = checks["status"] == "ok"
    status_code = 200 if critical_ok else 503

    # Resolved HERE rather than read off `request["authenticated_agent"]`,
    # because /health is in `_UNPROTECTED_PATHS`: auth_middleware returns
    # early on it and never stashes a name, so the only way to know who is
    # asking is to resolve it. On an auth-off install it is not even
    # attempted — `resolve_identity()` cannot match anything against an empty
    # registry, and asking would only produce a None meaning "no identities
    # exist here", not "you are anonymous".
    identity = _safe_resolve_identity(request) if AUTH_CONFIGURED_AT_STARTUP else None
    if AUTH_CONFIGURED_AT_STARTUP and not identity:
        return web.json_response(
            {"status": checks["status"], "version": checks["version"],
             "api_version": checks["api_version"]},
            status=status_code,
        )
    if identity:
        # ⛔ A COPY, NEVER A MUTATION. `checks` is the TTL cache, SHARED by
        # every caller inside the window (see _health_probe_cached), and its
        # docstring states the contract this obeys: the per-caller projection
        # is applied fresh on every call and is never cached itself.
        #
        # ⚠ SAID HONESTLY, because the first draft of this comment claimed a
        # leak it could not produce: an in-place write is NOT observable from
        # outside today. There is one consumer, the anonymous branch above
        # rebuilds its own three keys and is immune, and two authenticated
        # callers each overwrite with their own values. What makes the copy
        # right is that writing per-caller identity into shared state is only
        # safe by accident — a second consumer, or a response serialised after
        # an await, turns it into a cross-identity disclosure with no other
        # change. The test pins the CACHE CONTENTS rather than a response,
        # because a response cannot see the difference.
        checks = {**checks, "agent": identity,
                  "role": _health_role_for(identity)}
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


def require_valid_llm_routing_config() -> None:
    """Model-attributes routing startup refusals (Model_Attributes_Routing_
    Plan_2026-08-18 REVISED DESIGN). Three loud, named refusals, all
    deferred to main() ONLY — same placement reasoning as require_auth_
    when_provider_keys_configured() above: every test in this repo imports
    this module freely with all manner of deliberately-invalid combinations,
    so an unconditional check at import/parse time would kill test
    collection itself, not just a genuinely misconfigured gateway.

    1. Unknown `roles` entry — collected by _load_llm_backends() into
       _LLM_BACKEND_ROLE_CONFIG_ERRORS at parse time; raised here.
    2. M-5 (Critical): a credentialed (token_env resolved) backend with
       NEITHER `roles` NOR an EXPLICIT `private_ok` would silently go dark
       under the plain default — a cloud-only install bricked on upgrade.
       Demands the operator pick one: private_ok: true (today's
       serve-everything) or roles: [...] (per-function opt-in).
    3. P-5: auth OFF (no AGENT_TOKENS configured) + ANY private_ok=false
       backend configured → refuse (without identities, I-1/I-6 privacy and
       steering invariants cannot hold — a backend deliberately scoped away
       from serving arbitrary/private traffic is meaningless if every
       caller is anonymous and indistinguishable). Governed by the SAME
       override env S-05 uses (ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1) —
       reusing S-05's precedent rather than inventing a second knob for the
       same "I have decided the risk is acceptable" declaration.
    """
    if _LLM_BACKEND_ROLE_CONFIG_ERRORS:
        raise SystemExit(
            "FATAL: LLM_BACKENDS_JSON has invalid `roles` entries:\n  "
            + "\n  ".join(_LLM_BACKEND_ROLE_CONFIG_ERRORS)
            + f"\nAllowed role names: {sorted(ROUTING_ROLE_NAMES)} "
              "(\"summarize\" is RESERVED, not accepted)."
        )

    needs_explicit_choice = sorted(
        b for b in LLM_BACKENDS
        if LLM_BACKEND_TOKENS.get(b) is not None
        and LLM_BACKEND_ROLES.get(b) is None
        and not LLM_BACKEND_PRIVATE_OK_EXPLICIT.get(b, False)
    )
    if needs_explicit_choice:
        raise SystemExit(
            "FATAL: credentialed LLM backend(s) configured with neither "
            f"`roles` nor an explicit `private_ok`: {', '.join(needs_explicit_choice)}. "
            "Pick one in LLM_BACKENDS_JSON: private_ok: true (keep today's "
            "serve-everything behavior) or roles: [\"extract\", \"verify\", "
            "\"judge\"] (per-function opt-in, this backend never receives "
            "role-less/other-function traffic). See shared-memory/.env.example."
        )

    if AUTH_CONFIGURED_AT_STARTUP:
        return
    private_false = sorted(b for b in LLM_BACKENDS if not LLM_BACKEND_PRIVATE_OK.get(b, True))
    if not private_false:
        return
    if os.environ.get("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", "").strip().lower() in ("1", "true", "yes", "on"):
        log.warning(
            "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS is set — starting UNAUTHENTICATED "
            "with private_ok=false backend(s) configured: %s. Without agent identities "
            "the gateway cannot tell one caller from another, so a backend scoped away "
            "from role-less/private traffic has no enforceable meaning. This is the "
            "deliberate override documented in shared-memory/.env.example, not a default.",
            ", ".join(private_false),
        )
        return
    raise SystemExit(
        "FATAL: AGENT_TOKENS is unset (auth off) but private_ok=false backend(s) "
        f"are configured ({', '.join(private_false)}) — the privacy/steering "
        "invariants (I-1/I-6) require a caller identity to enforce, which an "
        "auth-off install cannot provide. Configure AGENT_TOKENS, or set "
        "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1 to run anyway (see "
        "shared-memory/.env.example)."
    )


def warn_if_dream_slots_impossible() -> None:
    """C-1 (decision:1357): if NO backend counts toward /pool/status
    free_slots, every dream daemon (REM, NREM, relation_sweep) gates itself
    off a permanent 0 and simply never runs — with no LLM call ever made, no
    refusal counter ever fires, so without this warning the condition is
    invisible everywhere. A partial-role fleet (e.g. every backend scoped to
    a single function) is the remaining way to reach it; per-role slot
    accounting stays deferred, so the honest answer today is a LOUD startup
    line naming the fix. A warning, not a refusal — a gateway serving only
    ad-hoc client traffic is legitimate."""
    if any(_counts_free_slot(b) for b in LLM_BACKENDS):
        return
    log.warning(
        "NO configured backend counts toward /pool/status free_slots (each "
        "either declares a partial `roles` list or is private_ok=false with "
        "no full roles list). The dream daemons (REM/NREM/relation_sweep) "
        "gate on free_slots and will NEVER run against this fleet. Fix: give "
        "at least one backend no `roles` field (with private_ok), or an "
        "explicit roles list covering all of %s.", sorted(ROUTING_ROLE_NAMES))


# A2 (post-review addition, operator 2026-08-18): lifecycle token-count sum
# lines. Read independently here rather than importing coordinator's
# _audit_writer — this write is DELIBERATELY never routed through
# AsyncLineWriter (its pre-existing shutdown-only flush-hang, fact:1335 open
# item, must never risk stalling the gateway's own drain sequence), so there
# is nothing here to share with that writer beyond the file path.
GATEWAY_AUDIT_LOG_PATH = os.environ.get("GATEWAY_AUDIT_LOG_PATH", "").strip()
# UNMEASURED convenience knob (flagged per fact:1338) — 0 disables periodic
# emission entirely (the shutdown emission still always fires). A deployer
# who wants bounded loss on a hard kill (no graceful shutdown) sets this.
TOKEN_LIFECYCLE_SUM_INTERVAL_S = float(os.environ.get("TOKEN_LIFECYCLE_SUM_INTERVAL_S", "0") or "0")


def _emit_token_lifecycle_sums(reason: str) -> None:
    """One structured line per backend with the LIFECYCLE token totals so
    far, to the journal AND the gateway audit JSONL (A2). DIRECT SYNCHRONOUS
    write — never AsyncLineWriter, whose shutdown-only flush-hang (fact:1335
    open item) this call must not risk triggering during the gateway's own
    drain sequence. Best-effort: a write failure here must never break
    shutdown or the periodic caller's loop."""
    ts = datetime.now(timezone.utc).isoformat()
    for b in LLM_BACKENDS:
        p = _llm_tokens_prompt_total.get(b, 0)
        c = _llm_tokens_completion_total.get(b, 0)
        if p == 0 and c == 0:
            continue
        log.info(
            "llm-token-lifecycle-sum backend=%s reason=%s tokens_prompt_total=%d "
            "tokens_completion_total=%d", b, reason, p, c)
        if GATEWAY_AUDIT_LOG_PATH:
            try:
                append_secure(GATEWAY_AUDIT_LOG_PATH, json.dumps({
                    "ts": ts, "kind": "llm_token_lifecycle_sum", "reason": reason,
                    "backend": b, "tokens_prompt_total": p, "tokens_completion_total": c,
                }))
            except Exception as exc:
                log.warning("token lifecycle sum audit write failed: %s", exc)


async def _token_lifecycle_sum_daemon(stop_event: asyncio.Event) -> None:
    """Periodic A2 emission on TOKEN_LIFECYCLE_SUM_INTERVAL_S (no-op, exits
    immediately, when unset/0 — the shutdown emission in main()'s drain
    sequence is unconditional and covers every install either way)."""
    if TOKEN_LIFECYCLE_SUM_INTERVAL_S <= 0:
        return
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TOKEN_LIFECYCLE_SUM_INTERVAL_S)
            break   # stop_event fired — the drain sequence's own shutdown emission covers this
        except asyncio.TimeoutError:
            _emit_token_lifecycle_sums("periodic")


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
    # Model-attributes routing (M-5/P-5/unknown-role refusals) — see
    # require_valid_llm_routing_config()'s docstring for why this call
    # lives here too.
    require_valid_llm_routing_config()
    warn_if_dream_slots_impossible()

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
    # fact:1535 route-guard: snapshot the known framework routes from the
    # router itself, AFTER every real route above is registered and BEFORE
    # the catch-all below — see set_known_routes()'s docstring for why the
    # ordering is what excludes the catch-all from the snapshot.
    proxy.set_known_routes(app.router)
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
    log.info("### /v1/embeddings->%s | /v1/reranking->%s | default->LLM pool",
             EMBEDDER_URL, RERANKER_URL)

    stop_event = asyncio.Event()
    watchdog_task     = asyncio.create_task(_watchdog_daemon(stop_event))
    rem_watchdog_task = asyncio.create_task(_watchdog_rem_daemon(stop_event))
    # Backend capability probe — measures whether the critical backends can
    # actually SERVE, not merely whether they answer /health.
    capability_task   = asyncio.create_task(
        _capability_probe_daemon(proxy, stop_event, coordinator))
    # A2: periodic lifecycle token-count sum lines (no-op unless
    # TOKEN_LIFECYCLE_SUM_INTERVAL_S is set) — the unconditional shutdown
    # emission happens later in the drain sequence below regardless.
    token_lifecycle_task = asyncio.create_task(
        _token_lifecycle_sum_daemon(stop_event))
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
    token_lifecycle_task.cancel()
    for task in (watchdog_task, rem_watchdog_task, capability_task, token_lifecycle_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    # A2: unconditional lifecycle sum on graceful shutdown — DIRECT
    # SYNCHRONOUS write (see _emit_token_lifecycle_sums' docstring), so it
    # runs here rather than through proxy.cleanup()/coordinator.stop().
    _emit_token_lifecycle_sums("shutdown")
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
