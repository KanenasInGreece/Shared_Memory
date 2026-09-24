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
import socket
import sys
import ipaddress
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

# Load .env before importing coordinator (it reads config at module level).
from secure_env import (  # noqa: E402
    load_split_env, get_secret, is_secret_key, require_llm_backends_json_parses,
)
from log_hygiene import append_secure, secure_path, scrub_url_credentials, FILE_MODE  # noqa: E402
from framework_defaults import FRAMEWORK_DEFAULTS  # noqa: E402
# Use effective_role, not a bare _AGENT_ROLES lookup: that misses read_only_agents() and would call a confined identity write-capable.
from agent_roles import effective_role  # noqa: E402
# Import the health contract from a leaf module. handle_health drops a key once this release reaches its removed_in, and the import must not cycle back here.
from telemetry_contract import HEALTH as HEALTH_CONTRACT, strip_dropped  # noqa: E402
# Private log_hygiene helper with no public equivalent: secure_path opens for append, which the capacity-log replace path does not want.
# Import at startup so a rename fails here, not on the first capacity-log write.
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
    normalize_encoder_base,
    backup_quiesce_active,
    resolve_identity,
    _AGENT_TOKENS,
    _AGENT_ROLES,
    AUTH_CONFIGURED_AT_STARTUP,
    AUTH_SCHEME,
    FRAMEWORK_VERSION,
    API_VERSION,
    require_no_plaintext_agent_tokens,
    require_unprotected_paths_are_plain_routes,
    record_daemon_token_issued,
    record_credentialed_route_denied,
    record_llm_gateway_fault,
    record_llm_upstream_fault,
    record_llm_client_disconnect,
    _parse_upstream_error_type,
    _decompress_prefix_for_parse,
    _decompress_full_for_usage,
    _ERROR_BODY_PARSE_CAP,
    SUPPORTED_CONTENT_ENCODINGS,
    _short,
    # Same limits /health compares against and /memory/telemetry reports. Two spellings of one limit make the payload contradict itself.
    OUTBOX_AGE_WARN_S,
    ENCODER_LATENCY_WARN_MS,
    TOKEN_VERIFY_WARN_PER_MIN,
    NREM_FOLD_ATTEMPT_WARN,
    telemetry_gateway_counters,
    telemetry_credential_counters,
    telemetry_token_verify_ring,
    _llm_faults_snapshot,
)

# Gateway HTTP process: encoder routes, LLM pool, /health, and daemon watchdogs.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
# coordinator imports httpx and runs in this process. WARNING hides per-request chatter; a real client failure still logs. The aiohttp access log stays the per-request record.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("hive-proxy")

# Watchdog configuration
_DAEMON_MAX_RESTARTS    = 5    # circuit-breaker trip count
_DAEMON_RESTART_WINDOW  = 600  # rolling window (seconds) for the trip counter
_DAEMON_MIN_STABLE_SEC  = 30   # uptime needed to reset backoff — avoids boot-loop penalty
_DAEMON_MAX_BACKOFF_SEC = 60   # exponential backoff ceiling (seconds)

# Shared state written by the watchdogs, read by the health handler.
_daemon_proc:    "asyncio.subprocess.Process | None" = None
_daemon_healthy: bool = False  # True while the consolidation subprocess is alive
_rem_proc:       "asyncio.subprocess.Process | None" = None
_rem_healthy:    bool = False  # True while the REM subprocess is alive

# Encoder bases from EMBEDDER_URL/RERANKER_URL (`or` empty=default, same as coordinator); normalize_encoder_base strips a pasted /v1.
EMBEDDER_URL = normalize_encoder_base((os.environ.get("EMBEDDER_URL") or FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"]).strip())
RERANKER_URL = normalize_encoder_base((os.environ.get("RERANKER_URL") or FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"]).strip())
ROUTING_MAP = {
    "/v1/embeddings": EMBEDDER_URL,
    "/v1/reranking":  RERANKER_URL,
}


def _encoder_near_miss(path: str) -> "str | None":
    """Return the matched encoder path if decoded `path` is a near-miss prefix or
    traversal, preventing fallthrough to the LLM pool (fact:1535 — an MCP tool's GET
    with query params reached the LLM pool instead of the framework route it named,
    so a mistyped or wrong-method framework path must 404 or 405 and never fall
    through to the catch-all)."""
    for encoder_path in ROUTING_MAP:
        if path != encoder_path and path.startswith(encoder_path):
            return encoder_path
    return None

# S-04: catch-all forwards rel_url verbatim to a credentialed backend, so only exact (method, raw_path) framework POSTs may carry a provider key.
CREDENTIALED_BACKEND_ALLOWED_ROUTES = frozenset({
    ("POST", "/v1/chat/completions"),
    # Encoder POST paths stay on the S-04 allowlist even though handle_encoder never takes the pool path today (a future credentialed embedder would need them).
    ("POST", "/v1/embeddings"),
    ("POST", "/v1/reranking"),
})
# Mistyped/wrong-method framework paths must 404/405, never fall through to the LLM catch-all (fact:1535). Known routes come from the router (decision:1032).
RESERVED_ROUTE_PREFIXES = ("/memory/", "/admin/")
# handle_encoder counts bytes and returns 413 above this; it never streams the encoder body (decision:2651). LLM traffic uses client_max_size instead.
EMBED_RERANK_BUFFER_CAP = int(os.environ.get("EMBED_RERANK_BUFFER_CAP", str(1024 * 1024)))
# Used only when LLM_BACKENDS is unset. Keep the port overridable; LLM_BACKENDS is the real knob.
DEFAULT_TARGET = os.environ.get("LLM_DEFAULT_TARGET", FRAMEWORK_DEFAULTS["LLM_DEFAULT_TARGET"]["default"])

# LLM_BACKENDS_JSON (preferred) or LLM_BACKENDS CSV; token_env is a process env NAME, never a literal in the URL.
def _backend_url_credential_error(url: str) -> "str | None":
    """None if the URL has no userinfo credential; otherwise a scrubbed refusal. Unparseable URLs refuse. Query strings are allowed."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return (
            f"backend URL is unparseable — refusing (this framework never "
            "guesses whether an unparseable URL string carries a credential; "
            "fix the URL or use LLM_BACKENDS_JSON with a token_env instead)."
        )
    if parsed.username is None and parsed.password is None:
        return None
    return (
        f"{scrub_url_credentials(url)}: URL embeds a credential in its userinfo "
        "(user:pass@host or user@host) — this framework never accepts a "
        "credential inside the URL string itself. Use token_env instead: the "
        "backend gets its Authorization header from a NAMED env var already "
        "exported in the gateway's own process environment (see "
        "shared-memory/ops/README.md, 'Reasoning-LLM backends')."
    )


# Weight tail must be a finite numeric token. Bare float() accepts nan/inf, which must fall through as "the whole entry is the URL".
_BACKEND_WEIGHT_TOKEN_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")


def _parse_backend(entry: str) -> tuple[str, float]:
    """Split url@weight on the last @ only when the tail is numeric and is not URL userinfo."""
    entry = entry.strip()
    head, sep, tail = entry.rpartition("@")
    if sep and _BACKEND_WEIGHT_TOKEN_RE.match(tail):
        try:
            head_parts = urllib.parse.urlsplit(head)
        except Exception:
            head_parts = None
        head_is_bare_token = head_parts is not None and not head_parts.scheme
        head_has_port = head_parts is not None and head_parts.port is not None
        head_has_username = head_parts is not None and head_parts.username is not None
        try:
            entry_username = urllib.parse.urlsplit(entry).username
        except Exception:
            entry_username = "<unparseable>"   # never treat as "no userinfo"
        entry_has_no_userinfo_of_its_own = entry_username is None
        if not head_has_username and (
            head_is_bare_token or head_has_port or entry_has_no_userinfo_of_its_own
        ):
            url, w = head, tail
        else:
            url, w = entry, ""
    else:
        url, w = entry, ""
    try:
        weight = float(w) if w else 1.0
    except ValueError:
        weight = 1.0
    return url.rstrip("/"), max(weight, 0.1)


# Backend descriptor: roles (extract/judge; absent=serves-all), n_ctx, private_ok default-deny (decision:1824), max_inflight, optional price metadata (never used to route).
ROUTING_ROLE_NAMES = frozenset({"extract", "judge"})
RESERVED_ROLE_NAMES = frozenset({"summarize"})


_PRIVATE_NAME_SUFFIXES = (".local", ".lan", ".internal", ".home", ".home.arpa", ".ts.net")


def _bearer_transport_ok(backend_url: str, plaintext_ok: bool = False) -> bool:
    """Check whether bearer auth may be sent to `backend_url`.

    ⛔ A KEY OVER PLAINTEXT TO A PUBLIC HOST IS A KEY PUBLISHED TO THE PATH.
    Allows https, non-global IP/local host destinations (.local, .lan, .internal,
    .home, .home.arpa, .ts.net), or plaintext http with "plaintext_ok": true."""
    try:
        parts = urllib.parse.urlsplit(backend_url)
    except Exception:
        return False
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme == "https":
        return True
    if scheme != "http" or not host:
        return False
    if plaintext_ok:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_loopback or not ip.is_global)
    except ValueError:
        pass
    if host in ("localhost",) or "." not in host:
        # Reject numeric IPv4 spellings ipaddress.ip_address() misses but the resolver accepts (S3).
        try:
            socket.inet_aton(host)
            return False
        except OSError:
            pass
        return True
    return host.endswith(_PRIVATE_NAME_SUFFIXES)


# Operator plaintext_ok for a private http path (VPN or tunnel). The loader refuses a public plaintext backend; the health probe reads this map.
LLM_BACKEND_PLAINTEXT_OK: dict[str, bool] = {}
# Set when LLM_BACKENDS_JSON yielded no usable backend and the legacy fallback took over.
LLM_POOL_FALLBACK_REASON: "str | None" = None
# decision:1832: DEFAULT_TARGET stands in because nothing was declared. LLM_POOL_FALLBACK_REASON is the other case, a declared fleet with every entry excluded.
# Do not infer this from backend_status; that only shows what was probed.
LLM_POOL_CONFIG_EMPTY: bool = False


def _load_llm_backends() -> tuple[
        list[str], dict[str, float], dict[str, "str | None"], dict[str, "str | None"],
        dict[str, "dict | None"], dict[str, "frozenset[str] | None"], dict[str, "int | None"],
        dict[str, bool], dict[str, bool], dict[str, "int | None"],
        dict[str, "float | None"], dict[str, "float | None"], list[str], list[str]]:
    """Parse configured LLM backends from LLM_BACKENDS_JSON or legacy LLM_BACKENDS.

    Returns parsed backend attributes and config errors; errors are collected
    rather than raised so module imports stay clean, with startup enforcement
    deferred to require_valid_llm_routing_config() and
    require_no_backend_url_credentials()."""
    role_config_errors: list[str] = []
    url_credential_errors: list[str] = []
    # Set LLM_POOL_CONFIG_EMPTY on every return, both branches. Do not leave it to the module default.
    global LLM_POOL_CONFIG_EMPTY

    def _parse_roles(url: str, raw) -> "frozenset[str] | None":
        if raw is None:
            return None
        if not isinstance(raw, list):
            role_config_errors.append(
                f"{scrub_url_credentials(url)}: roles must be a JSON array, got {type(raw).__name__}")
            return frozenset()
        if not raw:
            # decision:1357: an empty roles list is eligible for nothing, and it must not pass the is-None serves-all check. Refuse it at startup.
            role_config_errors.append(
                f"{scrub_url_credentials(url)}: roles is an EMPTY list — this backend would be "
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
                    f"{scrub_url_credentials(url)}: roles names {sorted(reserved_hit)} — RESERVED, not "
                    f"accepted (NREM narrative folds are zero-inference; the only "
                    f"NREM LLM path is judge). Allowed: {sorted(ROUTING_ROLE_NAMES)}")
            unknown = bad - RESERVED_ROLE_NAMES
            if unknown:
                role_config_errors.append(
                    f"{scrub_url_credentials(url)}: unknown role name(s) {sorted(unknown)} — allowed: "
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
            if not isinstance(entry, dict):
                log.error(
                    "LLM_BACKENDS_JSON entry is %s, not an object — excluding it. "
                    "Each entry needs a url field (not OpenAI SDK's base_url).",
                    type(entry).__name__)
                continue
            url = str(entry.get("url", "")).rstrip("/")
            if not url:
                keys = sorted(str(k) for k in entry)
                alias = "base_url" if "base_url" in entry else (
                    "baseURL" if "baseURL" in entry else None)
                if alias:
                    log.error(
                        "LLM_BACKENDS_JSON entry has %s but the field name is url, "
                        "not OpenAI SDK's base_url — excluding this backend. "
                        "Rename the key to url. Keys present: %s",
                        alias, keys)
                else:
                    log.error(
                        "LLM_BACKENDS_JSON entry has no url — excluding this backend. "
                        "The required field is url (not base_url). Keys present: %s",
                        keys)
                continue
            # A userinfo credential is excluded before any other check, and this function must not raise. Startup exits in require_no_backend_url_credentials(); query strings are allowed.
            _cred_err = _backend_url_credential_error(url)
            if _cred_err:
                url_credential_errors.append(_cred_err)
                continue
            # The schema reads token_env, a name. A literal token or api_key would sit in plaintext and still leave the backend with no credential.
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
                    scrub_url_credentials(url), _raw_secret_fields)
                continue
            token_env = entry.get("token_env")
            token = None
            if token_env:
                # token_env names are secrets, so read them with get_secret. It still falls back to a value supplied in the process environment.
                token = get_secret(token_env)
                if not token:
                    # Falsy token_env is unset or a refused `_FILE` secret; do not say only "not set".
                    log.warning(
                        "LLM backend %s configured with token_env=%s but that "
                        "variable did not resolve to a usable secret (unset, or "
                        "refused by secure_env — see the [secure_env] WARNING "
                        "above) — excluding this backend from the pool.",
                        scrub_url_credentials(url), token_env)
                    continue
            if token and not _bearer_transport_ok(url, bool(entry.get("plaintext_ok"))):
                # A bearer on plaintext http to a public host would publish the key. Refuse at configuration, not on the first request.
                log.error(
                    "LLM backend %s is credentialed (token_env=%s) but its URL "
                    "is plaintext http to a PUBLIC host — this gateway never "
                    "sends a provider key in the clear across the internet. "
                    "Use https; a LAN, loopback or tailnet address is accepted "
                    "as-is; if the path really is private (VPN, tunnel) set "
                    "\"plaintext_ok\": true on the entry. Excluding this "
                    "backend from the pool.",
                    scrub_url_credentials(url), token_env)
                continue
            # extra_body merges provider switches (e.g. DeepSeek thinking disabled); a malformed value excludes the backend.
            extra_body = entry.get("extra_body")
            if extra_body is not None and not isinstance(extra_body, dict):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has a non-object extra_body "
                    "(%r) — excluding this backend from the pool.",
                    scrub_url_credentials(url), type(extra_body).__name__)
                continue
            # decision:1357: n_ctx and max_inflight must be a real int >= 1. bool is an int subclass; 0 disables the fit check or wedges the cap.
            n_ctx_raw = entry.get("n_ctx")
            if n_ctx_raw is not None and (not isinstance(n_ctx_raw, int)
                                          or isinstance(n_ctx_raw, bool)
                                          or n_ctx_raw < 1):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has an invalid n_ctx "
                    "(%r — must be an integer >= 1) — excluding this backend "
                    "from the pool.", scrub_url_credentials(url), n_ctx_raw)
                continue
            max_inflight_raw = entry.get("max_inflight")
            if max_inflight_raw is not None and (not isinstance(max_inflight_raw, int)
                                                 or isinstance(max_inflight_raw, bool)
                                                 or max_inflight_raw < 1):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has an invalid max_inflight "
                    "(%r — must be an integer >= 1) — excluding this backend "
                    "from the pool.", scrub_url_credentials(url), max_inflight_raw)
                continue
            private_ok_raw = entry.get("private_ok")
            if private_ok_raw is not None and not isinstance(private_ok_raw, bool):
                log.error(
                    "LLM_BACKENDS_JSON entry for %s has a non-boolean private_ok "
                    "(%r) — excluding this backend from the pool.",
                    scrub_url_credentials(url), private_ok_raw)
                continue
            urls.append(url)
            weights[url] = max(float(entry.get("weight", 1.0) or 1.0), 0.1)
            tokens[url] = token
            LLM_BACKEND_PLAINTEXT_OK[url] = bool(entry.get("plaintext_ok"))
            models[url] = entry.get("model") or None
            extras[url] = extra_body or None
            roles[url] = _parse_roles(url, entry.get("roles"))
            n_ctxs[url] = n_ctx_raw
            max_inflights[url] = max_inflight_raw
            # decision:1824: omitted private_ok is false, so an undeclared backend serves nothing. An explicit bool always wins.
            private_ok_explicit[url] = private_ok_raw is not None
            private_oks[url] = private_ok_raw if private_ok_raw is not None else False
            price_ins[url] = entry.get("price_per_mtok_in")
            price_outs[url] = entry.get("price_per_mtok_out")
        if urls:
            # A usable JSON fleet is not the absence case. Set the flag here instead of leaving the module default.
            LLM_POOL_CONFIG_EMPTY = False
            return (urls, weights, tokens, models, extras, roles, n_ctxs, private_oks,
                    private_ok_explicit, max_inflights, price_ins, price_outs, role_config_errors,
                    url_credential_errors)
        log.error("LLM_BACKENDS_JSON produced no usable backend — falling back to LLM_BACKENDS/LLM_DEFAULT_TARGET")
        # Remember why every JSON entry was excluded, or /health would show a healthy pool at the fallback address.
        global LLM_POOL_FALLBACK_REASON
        LLM_POOL_FALLBACK_REASON = "LLM_BACKENDS_JSON produced no usable backend (every entry excluded) — serving the LLM_BACKENDS/LLM_DEFAULT_TARGET fallback"

    # The legacy CSV gets the same userinfo check as JSON before a URL enters the pool.
    _raw_backends: list[tuple[str, float]] = []
    for _e in os.environ.get("LLM_BACKENDS", FRAMEWORK_DEFAULTS["LLM_BACKENDS"]["default"]).split(","):
        if not _e.strip():
            continue
        _u, _w = _parse_backend(_e)
        _cred_err = _backend_url_credential_error(_u)
        if _cred_err:
            # Ambiguous url@weight (no port/path) is a config-shape error, not a generic credential message.
            _stripped_e = _e.strip()
            _tail_candidate = _stripped_e.rpartition("@")[2]
            if _u == _stripped_e and _BACKEND_WEIGHT_TOKEN_RE.match(_tail_candidate):
                _cred_err = (
                    f"{scrub_url_credentials(_u)}: ambiguous LLM_BACKENDS entry — "
                    "this could be a credentialed URL, or a bare 'url@weight' "
                    "shorthand for a URL with no port. Refusing rather than "
                    "silently guessing. Fix by either: (1) adding an explicit "
                    "port to the URL, so 'host:port@weight' parses "
                    "unambiguously, or (2) declaring this backend via "
                    "LLM_BACKENDS_JSON instead of the LLM_BACKENDS CSV form."
                )
            url_credential_errors.append(_cred_err)
            continue
        _raw_backends.append((_u, _w))
    if not _raw_backends:
        # This fallback is absence only when it is not already the JSON-exclusion case. Set the flag on both branches so a later call cannot inherit a stale value.
        LLM_POOL_CONFIG_EMPTY = not LLM_POOL_FALLBACK_REASON
        _raw_backends = [(DEFAULT_TARGET, 1.0)]
    else:
        LLM_POOL_CONFIG_EMPTY = False
    urls = [u for u, _ in _raw_backends]
    weights = {u: w for u, w in _raw_backends}
    # decision:1824: CSV and DEFAULT_TARGET never set private_ok, so those backends are eligible for nothing until declared.
    return (urls, weights, {u: None for u in urls}, {u: None for u in urls}, {u: None for u in urls},
            {u: None for u in urls}, {u: None for u in urls}, {u: False for u in urls},
            {u: False for u in urls}, {u: None for u in urls}, {u: None for u in urls},
            {u: None for u in urls}, [], url_credential_errors)


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
 _LLM_BACKEND_ROLE_CONFIG_ERRORS, _LLM_BACKEND_URL_CREDENTIAL_ERRORS) = _load_llm_backends()

# W4 default-deny is computed once at import from env presence, never inside the loader (decision:1824).
LLM_POOL_LEGACY_KEY_PRESENT: bool = (
    "LLM_BACKENDS" in os.environ or "LLM_DEFAULT_TARGET" in os.environ)

# Runtime remedy for an empty role-less pool is check_config.py + declaring LLM_BACKENDS_JSON; migrate_env.py no-ops on current-generation loader.
_LLM_POOL_LEGACY_REMEDY = (
    "run check_config.py and declare LLM_BACKENDS_JSON explicitly "
    "(private_ok/roles) — see GET /health")


def _apply_backend_body_overrides(body: bytes, model: "str | None",
                                  extra: "dict | None",
                                  _body_obj: "dict | None" = None) -> bytes:
    """Apply backend `extra` and `model` overrides to request JSON bytes.

    Merges `extra` and sets `model` if originally present; `_body_obj` may
    provide the pre-parsed payload to avoid re-parsing, returning unparseable
    bodies unchanged."""
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

# Per-backend fail/cooldown changes future routing; the only retry is one same-target replay of a buffered body on a stale socket (S12).
LLM_FAIL_THRESHOLD = int(os.environ.get("LLM_FAIL_THRESHOLD", "2"))
# Transport failures and HTTP failures get separate thresholds because they mean different things: a refused connection is one backend event, while a hosted provider emits 429s and transient 5xx as part of normal operation, so the transport threshold of 2 would cool a healthy provider for LLM_COOLDOWN on a routine blip.
# fact:1338 (a proposed parameter value is a measurement claim in disguise): 5 is UNMEASURED — no rate has been observed against a real provider here, so tune it rather than trusting it.
LLM_HTTP_FAIL_THRESHOLD = max(1, int(os.environ.get("LLM_HTTP_FAIL_THRESHOLD", "5")))
LLM_FAIL_WINDOW = float(os.environ.get("LLM_FAIL_WINDOW", "60"))
LLM_COOLDOWN = float(os.environ.get("LLM_COOLDOWN", "300"))

# Fit uses chars/CHARS_PER_TOKEN_RATIO (measured floor 1.2) plus FIT_MARGIN; never under-count prompt tokens.
CHARS_PER_TOKEN_RATIO = float(os.environ.get("LLM_CHARS_PER_TOKEN_RATIO", "1.2"))
# Eligible iff est_prompt_tokens + effective_max_tokens <= n_ctx * (1 - FIT_MARGIN).
FIT_MARGIN = float(os.environ.get("FIT_MARGIN", "0.10"))
# fact:1338: unmeasured output budget when the caller sends no max_tokens, not a live measurement.
# decision:1330: REM_MAX_TOKENS_* is the measured ceiling for dream traffic; this is only the estimator fallback.
FIT_DEFAULT_OUTPUT_TOKENS = int(os.environ.get("FIT_DEFAULT_OUTPUT_TOKENS", "2048"))
# At max_inflight the backend counts as busy. Wait for an eligible one instead of widening eligibility or picking over cap, and bound the wait so a saturated backend still fails.
LLM_MAX_INFLIGHT_WAIT_S = float(os.environ.get("LLM_MAX_INFLIGHT_WAIT_S", "120"))
# decision:1357: floor of 0.05s. A configured 0 would busy-spin the event loop for the whole wait.
LLM_MAX_INFLIGHT_POLL_S = max(0.05, float(os.environ.get("LLM_MAX_INFLIGHT_POLL_S", "0.5")))
# Cap concurrent capacity-waiters (floor 1); beyond that, 503 backend_at_capacity (decision:1357, fact:1338).
LLM_MAX_CAPACITY_WAITERS = max(1, int(os.environ.get("LLM_MAX_CAPACITY_WAITERS", "8")))

# Working set the router iterates; eligibility is `_role_eligible` (roles / private_ok), not "every role shares the pool".
LLM_POOL: list[str] = list(LLM_BACKENDS)

_llm_inflight: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
# Inflight count cannot tell busy from stuck. Record start times so status can flag suspect_wedged after a lazy health probe.
_llm_inflight_started: dict[str, list[float]] = {b: [] for b in LLM_BACKENDS}
LLM_WEDGE_SUSPECT_AGE = float(os.environ.get("LLM_WEDGE_SUSPECT_AGE", "900"))


# ⚠ The two sides of the cap deliberately disagree for an UNDECLARED backend, and only this reporting side defaults to 1.
# Routing's own `_at_cap` treats an absent max_inflight as no limit at all, which is long-standing behaviour no request should have changed under it; here an absent value keeps meaning one slot, which is exactly what this function reported before the declared cap was read at all.
def _reported_max_inflight(backend: str) -> int:
    """The concurrency /pool/status measures `available` against: the backend's declared max_inflight, or 1 when it declared none (which reproduces the old inflight == 0 test exactly)."""
    return LLM_BACKEND_MAX_INFLIGHT.get(backend) or 1


def _oldest_inflight_age(backend: str, now: float) -> float | None:
    starts = _llm_inflight_started.get(backend) or []
    return round(now - min(starts), 1) if starts else None


def _v1_models_probe_url(backend_base: str) -> str:
    """Return the /v1/models probe URL for `backend_base` without doubling /v1 if
    the configured base already includes it."""
    base = backend_base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _upstream_url(target_base: str, rel_url) -> str:
    """Join target base to request path without doubling /v1.

    Prevents silent /v1/v1/chat/completions 404s (which are unbilled and leave
    /health reporting ok while REM retries indefinitely)."""
    base = str(target_base).rstrip("/")
    rel = str(rel_url)
    if base.endswith("/v1") and rel.startswith("/v1/"):
        rel = rel[len("/v1"):]
    return f"{base}{rel}"


def _probe_headers(backend: str) -> dict:
    """Return authentication headers for a liveness/health probe of `backend`.

    Carries the backend's bearer token so probes distinguish rejected keys from
    unauthenticated requests, preventing false degraded states (fact:1794 — a false
    llm_pool degraded because the health probe carried no backend credential, and
    DeepSeek answers 401 to any unauthenticated request on any path)."""
    token = LLM_BACKEND_TOKENS.get(backend)
    if not token:
        return {}
    # Probe uses the same bearer-transport rule as the loader; GET /v1/models is the one deliberate S-04 GET carve-out (URL from config, not the request).
    if _bearer_transport_ok(backend, LLM_BACKEND_PLAINTEXT_OK.get(backend, False)):
        return {"Authorization": f"Bearer {token}"}
    return {}


async def _probe_backend_alive(session, backend: str) -> bool:
    """2s liveness probe of the backend's own health surface. llama.cpp serves
    /health; OpenAI-compatible fallback is /v1/models. True = answered."""
    for path in ("/health", None):
        try:
            url = f"{backend}{path}" if path else _v1_models_probe_url(backend)
            async with session.get(url, timeout=ClientTimeout(total=2.0),
                                   headers=_probe_headers(backend),
                                   allow_redirects=False) as r:
                # 404 means this path is not served here, not that the backend is down. Accepting it let a backend that 404s every real call look ok while REM retried it.
                if r.status == 404:
                    continue
                if r.status < 500:
                    return True
        except Exception:
            continue
    return False
_llm_unhealthy_until: dict[str, float] = {b: 0.0 for b in LLM_BACKENDS}
_llm_fail_times: dict[str, list] = {b: [] for b in LLM_BACKENDS}
# Cumulative routed and fail counts, so the realised split can be checked against the weights.
_llm_routed: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_fail_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
# fact:1314: flat routing counters, each paired with its own last-event timestamp. Added without changing an existing key's meaning.
_llm_routed_by_role: dict[str, int] = {r: 0 for r in ROUTING_ROLE_NAMES}
_llm_routed_by_role_last_ts: dict[str, "str | None"] = {r: None for r in ROUTING_ROLE_NAMES}
_routing_no_eligible_backend_count = 0
_routing_no_eligible_backend_last_ts: "str | None" = None
_routing_fit_rejected_count = 0
_routing_fit_rejected_last_ts: "str | None" = None
# decision:1357: the 503 backend_at_capacity refusal has its own counter and timestamp. The 422 counters never fire on this path.
_routing_backend_at_capacity_count = 0
_routing_backend_at_capacity_last_ts: "str | None" = None
# decision:1357: how many requests currently hold a capacity-wait slot, bounded by LLM_MAX_CAPACITY_WAITERS.
_capacity_waiters = 0
# decision:1357: warn once per unknown X-SM-LLM-Role from a steer-permitted caller. A typo otherwise degrades to role-less eligibility with no signal.
_warned_unknown_role_values: set[str] = set()
# In-process token totals from usage on a non-streaming response. A parse failure skips the counter and must not break the proxy.
_llm_tokens_prompt_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_tokens_completion_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_tokens_last_ts: dict[str, "str | None"] = {b: None for b in LLM_BACKENDS}
# Per-backend LLM latency is the full proxied exchange (success and failure, in-process, reset on restart).
_llm_requests_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_requests_failed_total: dict[str, int] = {b: 0 for b in LLM_BACKENDS}
_llm_latency_sum_s: dict[str, float] = {b: 0.0 for b in LLM_BACKENDS}
_llm_latency_max_s: dict[str, float] = {b: 0.0 for b in LLM_BACKENDS}
_llm_latency_last_ts: dict[str, "str | None"] = {b: None for b in LLM_BACKENDS}
# LLM_USAGE_CAPTURE_CAP_BYTES bounds compressed bytes accumulated for a trailing usage parse; over the cap, capture is abandoned.
LLM_USAGE_CAPTURE_CAP_BYTES = int(os.environ.get("LLM_USAGE_CAPTURE_CAP_BYTES", str(2 * 1024 * 1024)))
# In-process reservation set, not env or user config. Selection holds these backends out of the ordinary pool while another eligible one exists.
_llm_reserved: set[str] = set()

# Cache-affinity: sticky routing for a repeated prompt prefix (llama.cpp KV cache).
AFFINITY_PREFIX_CHARS = int(os.environ.get("LLM_AFFINITY_PREFIX_CHARS", "6144"))
AFFINITY_TTL          = float(os.environ.get("LLM_AFFINITY_TTL", "600"))
AFFINITY_MAX_INFLIGHT = int(os.environ.get("LLM_AFFINITY_MAX_INFLIGHT", "4"))
AFFINITY_PROTECT_HITS = 2
_llm_affinity: dict[str, list] = {}   # key -> [backend, last_ts, hits]
_llm_affinity_hits = 0
_llm_affinity_misses = 0


def _parse_json_body(body: bytes) -> "dict | None":
    """Parse request JSON bytes once into a dict, returning None on failure."""
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
    """Return positive max_tokens from `body_obj`, or FIT_DEFAULT_OUTPUT_TOKENS.
    Used as read-only headroom for fit checks and never written back."""
    if isinstance(body_obj, dict):
        mt = body_obj.get("max_tokens")
        if isinstance(mt, (int, float)) and mt > 0:
            return float(mt)
    return float(FIT_DEFAULT_OUTPUT_TOKENS)


def _serves_all(url: str) -> bool:
    """Return True if backend has no explicit roles and has private_ok enabled."""
    return LLM_BACKEND_ROLES.get(url) is None and LLM_BACKEND_PRIVATE_OK.get(url, False)


def _role_eligible(url: str, role: str) -> bool:
    """Role-less traffic needs explicit private_ok; a roles list serves only those roles (decision:1824)."""
    roles = LLM_BACKEND_ROLES.get(url)
    if not role:
        return LLM_BACKEND_PRIVATE_OK.get(url, False)
    if roles is None:
        return LLM_BACKEND_PRIVATE_OK.get(url, False)
    return role in roles


def _fits(url: str, est_prompt_tokens: float, effective_max_tokens: float) -> bool:
    """Return True if total tokens fit within backend n_ctx with FIT_MARGIN headroom.
    Undeclared n_ctx always fits."""
    n_ctx = LLM_BACKEND_NCTX.get(url)
    if not n_ctx:
        return True
    return (est_prompt_tokens + effective_max_tokens) <= n_ctx * (1 - FIT_MARGIN)


def _eligible_backends(role: str, est_prompt_tokens: float = 0.0,
                       effective_max_tokens: float = 0.0) -> list[str]:
    """Return backends eligible by role, privacy, and fit before dynamic routing.
    An empty return triggers a 422 no_eligible_backend refusal."""
    role = (role or "").strip().lower()
    return [b for b in LLM_POOL if _role_eligible(b, role)
            and _fits(b, est_prompt_tokens, effective_max_tokens)]


def _all_roles_ineligible() -> "str | None":
    """Return a reason string if no backend can serve role-less traffic or any
    ROUTING_ROLE_NAMES role (decision:1832 — the nothing-eligible verdict lives in the
    rem and nrem dependency reasons rather than in a new /health surface).
    Returns None if at least one role can be served."""
    if any(_eligible_backends(r) for r in ("",) + tuple(sorted(ROUTING_ROLE_NAMES))):
        return None
    reason = "configured, but no backend is eligible for any traffic"
    if LLM_POOL_LEGACY_KEY_PRESENT:
        # A live CSV or bare LLM_DEFAULT_TARGET used to serve role-less traffic and now serves nothing. Only this reason string carries the remedy for that shape.
        reason = f"{reason}; {_LLM_POOL_LEGACY_REMEDY}"
    return reason


def _declaration_gap() -> "str | None":
    """None if any LLM_POOL entry set private_ok; else 'none' (nothing declared) or 'no_role_less_opt_in' (roles without a privacy opt-in) (decision:1824)."""
    if any(LLM_BACKEND_PRIVATE_OK_EXPLICIT.get(b, False) for b in LLM_POOL):
        return None
    if all(LLM_BACKEND_PRIVATE_OK_EXPLICIT.get(b, False) is False
           and LLM_BACKEND_ROLES.get(b) is None for b in LLM_POOL):
        return "none"
    return "no_role_less_opt_in"


_DECLARATION_GAP_REMEDY = {
    "none": "declare LLM_BACKENDS_JSON explicitly (private_ok/roles); see check_config.py / migrate_env.py",
    "no_role_less_opt_in": "add \"private_ok\": true to a role-less-eligible entry, or opt back in explicitly; see check_config.py",
}


def _classify_no_eligible_constraint(role: str, est_prompt_tokens: float,
                                     effective_max_tokens: float) -> str:
    """Classify why routing eligibility was empty ("role", "privacy", or "fit").
    Evaluates role and privacy before fit to determine the bounding constraint."""
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
    # Bound the dedupe set and truncate the logged value so garbage role strings cannot grow memory or flood the journal.
    if len(_warned_unknown_role_values) >= 64:
        return
    _warned_unknown_role_values.add(role)
    log.warning(
        "X-SM-LLM-Role %r is not a known routing role %s — treating the "
        "request as ROLE-LESS (private_ok backends only). If this is a "
        "daemon-side typo, its traffic is silently degraded until fixed.",
        role[:80], sorted(ROUTING_ROLE_NAMES))


def _counts_free_slot(url: str) -> bool:
    """Check if backend counts toward /pool/status free_slots.

    Counts backends that can take any dream job (serves-all with private_ok or
    explicitly covering all dream roles), preventing silently zeroed dream cycles
    (decision:1357 — the routing ruling: counting only serves-all backends toward
    free_slots silently zeroed the dream cycle, plus six required fixes)."""
    roles = LLM_BACKEND_ROLES.get(url)
    if roles is None:
        return LLM_BACKEND_PRIVATE_OK.get(url, False)
    return ROUTING_ROLE_NAMES <= roles


def _record_backend_token_usage(backend: str, usage: dict) -> None:
    """Record cumulative prompt and completion tokens from response `usage` object."""
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
        log.warning("token usage accounting failed for %s: %s", scrub_url_credentials(backend), exc)


def _record_llm_latency(backend: str, elapsed_s: float, failed: bool) -> None:
    """Record per-backend LLM request latency and failure counts.
    Called from handle_proxy's finally block without raising."""
    try:
        _llm_requests_total[backend] = _llm_requests_total.get(backend, 0) + 1
        if failed:
            _llm_requests_failed_total[backend] = _llm_requests_failed_total.get(backend, 0) + 1
        _llm_latency_sum_s[backend] = _llm_latency_sum_s.get(backend, 0.0) + elapsed_s
        _llm_latency_max_s[backend] = max(_llm_latency_max_s.get(backend, 0.0), elapsed_s)
        _llm_latency_last_ts[backend] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        log.warning("latency accounting failed for %s: %s", scrub_url_credentials(backend), exc)


def _llm_mark_fail(backend: str, threshold: "int | None" = None) -> None:
    """Record a backend failure and trip the cooldown once it fails too often, counting against `threshold` (LLM_FAIL_THRESHOLD for a transport failure, LLM_HTTP_FAIL_THRESHOLD for an upstream 429 or 5xx)."""
    now = time.monotonic()
    limit = LLM_FAIL_THRESHOLD if threshold is None else max(1, threshold)
    _llm_fail_total[backend] = _llm_fail_total.get(backend, 0) + 1
    fails = [t for t in _llm_fail_times.get(backend, []) if now - t < LLM_FAIL_WINDOW]
    fails.append(now)
    _llm_fail_times[backend] = fails
    if len(fails) >= limit:
        _llm_unhealthy_until[backend] = now + LLM_COOLDOWN
        _llm_fail_times[backend] = []
        log.warning("LLM backend %s in cooldown for %.0fs (%d fails)", scrub_url_credentials(backend), LLM_COOLDOWN, limit)


# 429 and 5xx fault the backend; client 4xx errors reflect request defects and
# must not trip backend cooldown.
def _http_status_faults_backend(status: int) -> bool:
    """True iff this upstream status should count toward the backend's fail streak."""
    return status == 429 or status >= 500


def _llm_mark_ok(backend: str) -> None:
    _llm_fail_times[backend] = []


# _ordered_llm_backends was removed; it had no callers.

def _select_llm_backend(role: str = "", affinity_key: str | None = None,
                        est_prompt_tokens: float = 0.0,
                        effective_max_tokens: float = 0.0) -> "str | None":
    """Select an eligible LLM backend via cache affinity or least-in-flight.

    Operates strictly within role, privacy, and fit eligibility, returning None
    if no candidate is eligible or if all candidates are at capacity."""
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

    # Affinity hit only when that card is eligible, usable, and under both the affinity cap and max_inflight.
    ent = _llm_affinity.get(affinity_key) if affinity_key else None
    if (ent and ent[0] in eligible_set and _usable(ent[0]) and not _at_cap(ent[0])
            and _llm_inflight.get(ent[0], 0) < AFFINITY_MAX_INFLIGHT):
        ent[1] = now
        ent[2] += 1
        _llm_affinity_hits += 1
        return ent[0]

    # Miss: least-in-flight inside the eligible set, protecting a reused hot prefix. Never fall back to the full pool.
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
    """Poll _select_llm_backend up to LLM_MAX_INFLIGHT_WAIT_S for capacity slots.
    Returns None if all eligible backends remain saturated through the deadline."""
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
    """Wait for capacity while bounding concurrency by LLM_MAX_CAPACITY_WAITERS
    (decision:1357 — the routing ruling: counting only serves-all backends toward
    free_slots silently zeroed the dream cycle, plus six required fixes).
    Returns None immediately when the waiter limit is reached."""
    global _capacity_waiters
    if _capacity_waiters >= LLM_MAX_CAPACITY_WAITERS:
        return None
    _capacity_waiters += 1
    try:
        return await _select_backend_waiting_on_capacity(
            role, affinity_key, est_prompt_tokens, effective_max_tokens)
    finally:
        _capacity_waiters -= 1

# Hop-by-hop headers (RFC 7230 §6.1) plus Content-Length are never forwarded; Accept-Encoding stays (auto_decompress=False).
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
})

# S13 denylist: caller-describing headers (Cookie, X-Forwarded-*, X-Real-IP) never reach a backend; Referer/User-Agent stay (pinned).
CLIENT_ORIGIN_HEADERS = frozenset({
    "cookie", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
    "x-real-ip",
})

# S16f: drop Set-Cookie from upstream responses so a backend cannot plant a cookie on our caller.
UPSTREAM_ONLY_RESPONSE_HEADERS = frozenset({"set-cookie"})

GATEWAY_SERVER_HEADER = "shared-memory-gateway"


async def _set_server_header(request, response) -> None:
    """Stamp the gateway's Server header on every gateway and proxied response,
    overriding aiohttp 3.14.3 defaults to prevent server software disclosure."""
    response.headers["Server"] = GATEWAY_SERVER_HEADER


def _scrub_url_credentials(text: str) -> str:
    """Scrub credentials from URLs in text for log hygiene."""
    return scrub_url_credentials(text)


def _safe_request_id(request) -> "str | None":
    """Return request_id stashed by auth_middleware for audit correlation, or None."""
    try:
        return request.get("request_id")
    except AttributeError:
        return None


def _safe_agent_name(request) -> "str | None":
    """Return authenticated_agent stashed by auth_middleware, or None."""
    try:
        return request.get("authenticated_agent")
    except AttributeError:
        return None


# Audit marker substituted for a query string. The value stays out of the log; a provider key in the query is what must not be written.
QUERY_REDACTED_MARKER = "?<query-redacted>"


def _audited_route_spelling(rel_url) -> str:
    """Return raw path with redacted query marker for credential auditing.

    ⛔ The query VALUE is never returned. Preserves raw path encoding to record
    exact refusal reasons without leaking query contents."""
    spelling = rel_url.raw_path
    if rel_url.query_string:
        spelling += QUERY_REDACTED_MARKER
    return spelling


def _safe_resolve_identity(request) -> "str | None":
    """Safely resolve request identity, returning None if request is absent or malformed."""
    try:
        return resolve_identity(request)
    except AttributeError:
        return None


# Only the two framework daemons this module mints for may steer LLM routing, besides admins.
_CONSOLIDATION_AGENT_NAME = "consolidation"
_REM_DAEMON_AGENT_NAME    = "rem_daemon"
# Framework tools that send X-SM-LLM-Role but run outside the gateway are the explicit steering allowlist (decision:1357).
LLM_STEER_EXTRA_AGENT_NAMES = frozenset(
    n.strip() for n in os.environ.get("LLM_STEER_EXTRA_AGENT_NAMES", "").split(",")
    if n.strip())
DAEMON_AGENT_NAMES = frozenset(
    {_CONSOLIDATION_AGENT_NAME, _REM_DAEMON_AGENT_NAME}) | LLM_STEER_EXTRA_AGENT_NAMES


def _may_steer_llm(request) -> bool:
    """Return True if identity may send X-SM-LLM-* headers (daemons, admins,
    or when authentication is disabled at startup)."""
    if not AUTH_CONFIGURED_AT_STARTUP:
        return True
    agent_name = _safe_agent_name(request)
    if agent_name in DAEMON_AGENT_NAMES:
        return True
    return _AGENT_ROLES.get(agent_name, "full") == "admin"


def _strip_llm_steering_headers(headers) -> "CIMultiDict[str]":
    """Return a CIMultiDict copy of headers with X-SM-LLM-* steering headers removed."""
    result: CIMultiDict = CIMultiDict()
    for k, v in headers.items():
        if not k.lower().startswith("x-sm-llm-"):
            result.add(k, v)
    return result

# Upstream reset while reading iter_any(): an aiohttp client error, not a downstream disconnect. A clean close just ends the iterator.
UPSTREAM_DISCONNECT = (ServerDisconnectedError,)


class _EncoderBodyTooLarge(Exception):
    """Counted encoder body exceeded EMBED_RERANK_BUFFER_CAP."""

    def __init__(self, nbytes: int):
        self.nbytes = nbytes
        super().__init__(nbytes)


async def _read_encoder_body_capped(stream, cap: int = EMBED_RERANK_BUFFER_CAP) -> bytes:
    """Read an encoder request body, aborting at cap without a further pull.

    Each pull is remaining_to_cap+1 (aiohttp StreamReader.read(n)). Never
    read() / read(-1). handle_encoder is the only caller (decision:2651).
    """
    buf = bytearray()
    while True:
        remaining_to_cap = cap - len(buf)
        n = remaining_to_cap + 1
        chunk = await stream.read(n)
        if not chunk:
            break
        buf += chunk
        if len(buf) > cap:
            raise _EncoderBodyTooLarge(len(buf))
    return bytes(buf)


def _encoder_payload_too_large_response(nbytes: int) -> web.Response:
    """413 the encoder caller; unread remainder is left for protocol close."""
    resp = web.json_response(
        {"error": f"Payload length {nbytes} exceeds buffer cap of {EMBED_RERANK_BUFFER_CAP} bytes"},
        status=413,
        headers={"X-SM-Fault-Origin": "gateway"},
    )
    resp.force_close()
    return resp


# Proxy
class AsyncHiveMindProxy:
    def __init__(self):
        self.session: ClientSession | None = None
        # Filled once at startup from the router. A dynamic route matches on its pattern, not by string equality on the formatter key.
        self._known_routes: dict = {}

    def set_known_routes(self, router: "web.UrlDispatcher") -> None:
        """Snapshot registered routes (method and path pattern) from UrlDispatcher,
        excluding wildcard catch-all routes (decision:1032 — duplicate what the graph walk
        consumes, dereference what the reader renders; never write what a reader can derive).
        Enables handle_proxy route guarding against mistyped framework calls."""
        known: dict = {}
        for route in router.routes():
            if route.method == "*":
                # Do not snapshot the catch-all * route (that would 405 every LLM passthrough).
                continue
            resource = route.resource
            if resource is None:
                continue
            info = resource.get_info()
            # PlainResource exposes path; DynamicResource exposes formatter and pattern. Measured against aiohttp 3.14.
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
        # connect=5s, total=None (never cut a generation), auto_decompress=False (keep Content-Encoding with the bytes).
        timeout = ClientTimeout(total=None, connect=5.0)
        self.session = ClientSession(
            connector=connector,
            timeout=timeout,
            auto_decompress=False,
            # already aiohttp's default; stated so .netrc credentials are never injected and the posture is visible in code
            trust_env=False,
        )
        log.info("Connection pool ready (limit=200, limit_per_host=80)")

    async def cleanup(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            log.info("Upstream client session closed.")

    def _filter_headers(self, headers, *, strip_gateway_namespace: bool = False) -> dict:
        """Filter headers across request and response directions.

        Strips hop-by-hop, Host, and client Authorization headers.
        ⚠ When strip_gateway_namespace is True (response direction), removes x-sm-*,
        www-authenticate, and UPSTREAM_ONLY_RESPONSE_HEADERS to prevent upstream
        spoofing; otherwise (request direction), removes CLIENT_ORIGIN_HEADERS."""
        result = {
            k: v for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in ("host", "authorization")
        }
        if strip_gateway_namespace:
            result = {
                k: v for k, v in result.items()
                if not k.lower().startswith("x-sm-")
                and k.lower() != "www-authenticate"
                and k.lower() not in UPSTREAM_ONLY_RESPONSE_HEADERS
            }
        else:
            result = {
                k: v for k, v in result.items()
                if k.lower() not in CLIENT_ORIGIN_HEADERS
            }
        return result

    def _route_guard(self, request: web.Request) -> "web.Response | None":
        """Catch-all: known path + any method → 405; reserved prefix unknown → 404; trailing-slash near-miss of /health or /pool/status → 404; else None (LLM passthrough) (fact:1535, A1)."""
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

        # 405 for any decoded path that matches a known key (do not add a method test): GET /pool%2fstatus must not reach LLM dispatch. Pinned by test_encoded_slash_spelling_of_an_owned_route_is_never_proxied.
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

        # Reserved-prefix 404 runs before the near-miss branch so /memory/*/ and /admin/*/ keep their original body (A1).
        if path.startswith(RESERVED_ROUTE_PREFIXES):
            return web.json_response(
                {"error": f"No such framework route: {path}. This path is "
                          f"not registered under the framework's reserved "
                          f"prefix. The request was NOT forwarded to any LLM "
                          f"backend — correct the path and retry."},
                status=404,
                headers={"X-SM-Fault-Origin": "gateway"},
            )

        # Trailing-slash near-miss of a static gateway-owned route (/health/, /pool/status/) is 404, not LLM passthrough — grant stays exact (A1).
        normalised = path.rstrip("/") or "/"
        if normalised != path:
            for key, entry in self._known_routes.items():
                if entry["pattern"] is None and key == normalised:
                    return web.json_response(
                        {"error": f"No such framework route: {path}. The "
                                  f"framework registers {key} exactly — this "
                                  f"is a near-miss spelling of a gateway route, "
                                  f"not a passthrough path. The request was NOT "
                                  f"forwarded to any LLM backend — correct the "
                                  f"path and retry."},
                        status=404,
                        headers={"X-SM-Fault-Origin": "gateway"},
                    )

        return None

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        # fact:1535: run the route guard before header stripping. A mistyped or wrong-method framework request must fail here, not reach dispatch.
        guard_response = self._route_guard(request)
        if guard_response is not None:
            return guard_response

        # Encoder near-miss spellings (suffix/extra segment) 404 in the guard's voice; they must not fall through to the LLM pool (fact:1535).
        encoder_near_miss = _encoder_near_miss(request.path)
        if encoder_near_miss is not None:
            return web.json_response(
                {"error": f"No such framework route: {request.path}. The "
                          f"framework registers {encoder_near_miss} exactly — this "
                          f"is a near-miss spelling of a gateway route, "
                          f"not a passthrough path. The request was NOT "
                          f"forwarded to any LLM backend — correct the "
                          f"path and retry."},
                status=404,
                headers={"X-SM-Fault-Origin": "gateway"},
            )

        # Catch-all is pool-routed LLM; strip X-SM-LLM-* from non-steering callers before role or routing is read (S-14).
        steer_headers = (request.headers if _may_steer_llm(request)
                          else _strip_llm_steering_headers(request.headers))
        # Encoder paths are registered on handle_encoder, so this handler only selects an LLM backend. Do not prefix-match the decoded path; that forwarded near-miss spellings to the encoder.
        llm_backend: str | None = None
        llm_body: bytes | None = None
        # Buffer the body so the affinity key is taken from the prompt before a backend rewrite.
        llm_body = await request.read() if request.can_read_body else b""
        # Parse the body once. Affinity, fit, and the backend rewrite all read this object; do not json.loads the same bytes again.
        body_obj = _parse_json_body(llm_body)
        role = steer_headers.get("X-SM-LLM-Role", "").strip().lower()
        affinity_key = _affinity_key_from_obj(body_obj)
        est_prompt_tokens = (len(llm_body) / CHARS_PER_TOKEN_RATIO
                             if CHARS_PER_TOKEN_RATIO > 0 else 0.0)
        effective_max_tokens = _extract_effective_max_tokens(body_obj)

        # Eligibility is a hard pre-filter, before affinity, health, cooldown, reservation, or cap. Empty is a 422 with no inflight slot taken, and it must not widen.
        if role and role not in ROUTING_ROLE_NAMES:
            _warn_unknown_role_once(role)
        eligible_pre = _eligible_backends(role, est_prompt_tokens, effective_max_tokens)
        if not eligible_pre:
            constraint = _classify_no_eligible_constraint(
                role, est_prompt_tokens, effective_max_tokens)
            _record_no_eligible_backend(constraint)
            refusal_body = {"error": "no_eligible_backend",
                            "constraint": constraint, "role": role or None}
            # Additive declaration key only. Do not change the constraint vocabulary; both daemons read only error, constraint, and role.
            declaration = _declaration_gap()
            if declaration is not None:
                refusal_body["declaration"] = declaration
                log.warning(
                    "no_eligible_backend refusal carries declaration=%s "
                    "(role=%s, constraint=%s) — remedy: %s",
                    declaration, role or "(role-less)", constraint,
                    _DECLARATION_GAP_REMEDY.get(declaration, "declare explicitly"))
            if constraint == "fit":
                # decision:1357: a fit rejection is not charged as a record defect. Name the estimate so the operator can retune the ratio, margin, or n_ctx.
                refusal_body["est_prompt_tokens"] = int(est_prompt_tokens)
                refusal_body["effective_max_tokens"] = int(effective_max_tokens)
                # Daemons read only error, constraint, and role, so the estimate fields never reach an operator. This journal line is the retune signal.
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

        # Credentialed-only eligible set: refuse before the capacity wait unless (method, rel_url.raw_path) is on the S-04 allowlist with no query string (decision:1357).
        _route = (request.method, request.rel_url.raw_path)
        if ((_route not in CREDENTIALED_BACKEND_ALLOWED_ROUTES
             or request.rel_url.query_string)
                and all(LLM_BACKEND_TOKENS.get(b) is not None for b in eligible_pre)):
            record_credentialed_route_denied(
                eligible_pre[0], request.method,
                _audited_route_spelling(request.rel_url),  # QA-1: RAW path + query marker
                agent_name=_safe_agent_name(request),
                request_id=_safe_request_id(request),
            )
            return web.json_response(
                {"error": "credentialed backends accept only framework endpoints"},
                status=403, headers={"X-SM-Fault-Origin": "gateway"},
            )

        # If every eligible backend is at its cap, wait on that set. The cap must not widen eligibility, and no inflight slot is taken yet.
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

        # A request that will carry a provider key may only POST a framework-owned path. Check that before the model-mismatch refusal so a denied route is 403, not 400.
        if LLM_BACKEND_TOKENS.get(llm_backend) is not None:
            route = (request.method, request.rel_url.raw_path)
            if (route not in CREDENTIALED_BACKEND_ALLOWED_ROUTES
                    or request.rel_url.query_string):
                record_credentialed_route_denied(
                    llm_backend, request.method,
                    _audited_route_spelling(request.rel_url),  # QA-1: RAW path + query marker
                    agent_name=_safe_agent_name(request),
                    request_id=_safe_request_id(request),
                )
                return web.json_response(
                    {"error": "credentialed backends accept only framework endpoints"},
                    status=403, headers={"X-SM-Fault-Origin": "gateway"},
                )

        # S5: a credentialed backend serves only its declared model (refuse, not rewrite) before a capacity slot is taken.
        backend_model = LLM_BACKEND_MODELS.get(llm_backend)
        if LLM_BACKEND_TOKENS.get(llm_backend) is not None and backend_model and isinstance(body_obj, dict):
            caller_model = body_obj.get("model")
            if caller_model and caller_model not in (backend_model, "local-model"):
                return web.json_response(
                    {"error": "model_mismatch",
                     "detail": f"credentialed backend requires model '{backend_model}', but request specified '{caller_model}'"},
                    status=400,
                    headers={"X-SM-Fault-Origin": "gateway"},
                )

        # Rewrite model and extra_body per backend. Callers send local-model and do not know the provider's switches.
        llm_body = _apply_backend_body_overrides(
            llm_body, LLM_BACKEND_MODELS.get(llm_backend),
            LLM_BACKEND_EXTRAS.get(llm_backend), _body_obj=body_obj)

        return await self._forward_upstream(
            request,
            target_base=target_base,
            llm_backend=llm_backend,
            llm_body=llm_body,
            body_obj=body_obj,
            role=role,
            steer_headers=steer_headers,
        )

    async def handle_encoder(self, request: web.Request) -> web.StreamResponse:
        """Handle exact POST /v1/embeddings and /v1/reranking routes.

        Enforces exact raw wire path equality, applies payload caps, and forwards
        upstream without credentials.

        ⛔ NEVER bind these paths to handle_proxy and never wrap this handler in
        _route_guard: the guard answers 405 for ANY known key, including an
        allowed method, so a registered encoder route routed through it would
        refuse every legitimate POST. Near spellings are meant to fall to the
        catch-all, where _route_guard and _encoder_near_miss refuse them
        instead (fact:1535 — a mistyped framework path must 404 or 405, never
        fall through to the LLM pool)."""
        if request.rel_url.raw_path != request.rel_url.path_safe:
            return web.json_response(
                {"error": f"No such framework route: {request.rel_url.raw_path}. The framework "
                          f"registers {request.rel_url.path_safe} exactly — this is an encoded "
                          f"spelling of a gateway route, not a passthrough path. The request "
                          f"was NOT forwarded to any backend — correct the path and retry."},
                status=404, headers={"X-SM-Fault-Origin": "gateway"})

        # This handler is only reached through its own registration. aiohttp matches the plain route on rel_url.path_safe, the same string ROUTING_MAP uses.
        target_base = ROUTING_MAP[request.rel_url.path_safe]
        llm_body: bytes | None = None
        if request.can_read_body:
            content_length = request.content_length
            if content_length is not None and content_length > EMBED_RERANK_BUFFER_CAP:
                return _encoder_payload_too_large_response(content_length)
            try:
                raw_body = await _read_encoder_body_capped(request.content)
            except _EncoderBodyTooLarge as exc:
                return _encoder_payload_too_large_response(exc.nbytes)
            if not raw_body:
                llm_body = b""
            elif request.rel_url.path_safe == "/v1/reranking":
                try:
                    data = json.loads(raw_body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    data = None
                if isinstance(data, dict):
                    from dream_telemetry import prefix_rerank_doc, prefix_rerank_query
                    modified = False
                    q = data.get("query")
                    pref_q = prefix_rerank_query(q)
                    data["query"] = pref_q
                    if pref_q != q:
                        modified = True
                    docs = data.get("documents")
                    if isinstance(docs, list):
                        new_docs = []
                        for d in docs:
                            if isinstance(d, str):
                                pref_d = prefix_rerank_doc(data.get("query"), d)
                                if pref_d != d:
                                    modified = True
                                new_docs.append(pref_d)
                            else:
                                new_docs.append(d)
                        if modified:
                            data["documents"] = new_docs
                    if modified:
                        llm_body = json.dumps(data).encode("utf-8")
                    else:
                        llm_body = raw_body
                else:
                    llm_body = raw_body
            else:
                from encoder_window import clamp_encoder_payload
                llm_body = clamp_encoder_payload(raw_body)
        return await self._forward_upstream(
            request,
            target_base=target_base,
            llm_body=llm_body,
            steer_headers=request.headers,
        )


    async def _forward_upstream(
        self,
        request: web.Request,
        *,
        target_base: str,
        llm_backend: "str | None" = None,
        llm_body: "bytes | None" = None,
        body_obj: "dict | None" = None,
        role: str = "",
        steer_headers=None,
    ) -> web.StreamResponse:
        """Forward request upstream, relaying streamed or buffered responses.
        Handles header filtering, optional backend credentials, retries, and
        telemetry accounting."""
        if steer_headers is None:
            steer_headers = request.headers
        target_url = _upstream_url(target_base, request.rel_url)
        scrubbed_target_url = scrub_url_credentials(target_url)
        log.debug("→ %s %s", request.method, scrubbed_target_url)

        # Strip X-SM-LLM-* before the upstream forward for every caller (provider never sees routing metadata).
        upstream_headers = self._filter_headers(_strip_llm_steering_headers(steer_headers))
        # Authorization was stripped above. Put it back only for a backend that has its own configured credential.
        backend_token = None
        if llm_backend is not None:
            backend_token = LLM_BACKEND_TOKENS.get(llm_backend)
            if backend_token:
                # S-04: a request about to carry a provider key may only POST a framework-owned path (raw_path, no query) before Authorization is attached.
                route = (request.method, request.rel_url.raw_path)
                if (route not in CREDENTIALED_BACKEND_ALLOWED_ROUTES
                        or request.rel_url.query_string):
                    record_credentialed_route_denied(
                        llm_backend, request.method,
                        _audited_route_spelling(request.rel_url),  # QA-1: RAW path + query marker
                        agent_name=_safe_agent_name(request),
                        request_id=_safe_request_id(request),
                    )
                    return web.json_response(
                        {"error": "credentialed backends accept only framework endpoints"},
                        status=403, headers={"X-SM-Fault-Origin": "gateway"},
                    )
                upstream_headers["Authorization"] = f"Bearer {backend_token}"
            # Stash backend and key_attached for the audit line auth_middleware reads after return. The request may be a test double with no mapping interface.
            try:
                request["backend"] = llm_backend
                if backend_token:
                    request["key_attached"] = True
            except (TypeError, AttributeError):
                pass

        # Stream the body unless it is under EMBED_RERANK_BUFFER_CAP (buffering is what makes the stale-socket retry possible).
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
        # Retry only a buffered body. request.content is consumed on first use and cannot be resent.
        have_buffered_body = llm_body is not None or embed_body is not None
        max_attempts = 2 if have_buffered_body else 1

        try:
            # Reserve the inflight slot inside the try (one slot covers retries) so a pre-try cancel cannot leak it forever.
            if llm_backend is not None:
                _llm_inflight[llm_backend] = _llm_inflight.get(llm_backend, 0) + 1
                _llm_inflight_started.setdefault(llm_backend, []).append(time.monotonic())
                _llm_routed[llm_backend] = _llm_routed.get(llm_backend, 0) + 1
                # decision:1357: count the routed role here, at dispatch, beside inflight. Selection can still refuse the request before any dispatch.
                if role:
                    _record_role_routed(role)

            # LLM latency timer spans retries as one request; _llm_req_failed flips False only on the success return.
            _llm_req_start_mono = time.monotonic() if llm_backend is not None else None
            _llm_req_failed = True
            # Client abort is the caller's event: no backend ok/fail mark, skip latency record.
            _client_aborted = False

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
                        # Stamp serving backend via scrub_url_credentials (query-less keys round-trip; query-bearing attribution degrades).
                        if llm_backend is not None:
                            proxy_resp.headers["X-SM-LLM-Backend"] = scrub_url_credentials(llm_backend)
                        # Any upstream status >= 400 is an upstream-origin fault. Replace the provider body with a typed refusal; the fault-origin header is not set on success.
                        if upstream.status >= 400:
                            # Read only a prefix of a >=400 body. Classification does not need the rest, and an oversized body must not be buffered.
                            content_encoding = upstream.headers.get("Content-Encoding")
                            try:
                                body_bytes = await upstream.content.read(_ERROR_BODY_PARSE_CAP)
                            except Exception:
                                body_bytes = b""
                            try:
                                error_type = _parse_upstream_error_type(
                                    _decompress_prefix_for_parse(body_bytes, content_encoding))
                            except Exception:
                                error_type = "transient"

                            if llm_backend is not None:
                                try:
                                    record_llm_upstream_fault(
                                        llm_backend, upstream.status, error_type,
                                        credentialed=bool(backend_token),
                                        request_id=_safe_request_id(request),
                                    )
                                except Exception as exc:
                                    log.warning(
                                        "credential-fault classification failed for %s: %s",
                                        scrubbed_target_url, type(exc).__name__)

                            # A 429 or 5xx is the only failure signal a hosted provider gives: it reports its faults as HTTP status, never as a dropped connection, so without this the fail streak stays clean forever and the backend is never taken out of rotation.
                            # Worse than merely staying in: a fast error returns its inflight slot at once, so least-in-flight selection then PREFERS the failing backend over a healthy one mid-generation.
                            if llm_backend is not None and _http_status_faults_backend(upstream.status):
                                _llm_mark_fail(llm_backend, threshold=LLM_HTTP_FAIL_THRESHOLD)

                            headers = {"X-SM-Fault-Origin": "upstream"}
                            if llm_backend is not None:
                                headers["X-SM-LLM-Backend"] = scrub_url_credentials(llm_backend)

                            payload = {
                                "error": "upstream_fault",
                                "status": upstream.status,
                                "type": error_type,
                            }
                            # decision:2583: embed 400/413 becomes a structured overflow for get_embedding, never the provider body. Not used for the LLM pool, rerank, or 5xx.
                            path_safe = getattr(request.rel_url, "path_safe", "") or ""
                            if (
                                llm_backend is None
                                and path_safe == "/v1/embeddings"
                                and upstream.status in (400, 413)
                            ):
                                from encoder_window import (
                                    classify_overflow,
                                    overflow_fields,
                                )
                                prefix = _decompress_prefix_for_parse(
                                    body_bytes, content_encoding)
                                payload["overflow"] = overflow_fields(
                                    classify_overflow(upstream.status, prefix))

                            return web.json_response(
                                payload,
                                status=upstream.status,
                                headers=headers,
                            )
                        # prepare() ConnectionResetError is a downstream client abort, not the upstream reuse race (do not mark the backend failed; do not swallow CancelledError).
                        try:
                            await proxy_resp.prepare(request)
                        except (ConnectionResetError, IOError) as e:
                            log.warning(
                                "Client disconnected before response headers "
                                "could be sent: %s — %s", scrubbed_target_url, e)
                            record_llm_client_disconnect()
                            _client_aborted = True
                            return proxy_resp

                        # auto_decompress is false, so a compressed body arrives as framing bytes. Read Content-Encoding once so usage capture can decompress it.
                        content_encoding = upstream.headers.get("Content-Encoding")

                        # Best-effort usage parse after the loop (gzip/deflate/br, cap-bounded; skip SSE) (decision:1357).
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
                                await proxy_resp.write(chunk)
                            await proxy_resp.write_eof()

                        except asyncio.CancelledError:
                            # CancelledError is task cancellation, not a disconnect. Re-raise it or graceful shutdown stalls.
                            log.warning("Handler task cancelled during stream: %s", scrubbed_target_url)
                            raise

                        except UPSTREAM_DISCONNECT as e:
                            # Upstream dropped the connection after headers were sent. Log and return the partial response; a new reply is no longer possible.
                            log.warning("Upstream dropped connection mid-stream: %s — %s", scrubbed_target_url, e)

                        except (ConnectionResetError, IOError) as e:
                            # Downstream client reset: log and return; nothing more can be sent.
                            log.warning("Client disconnected mid-stream: %s — %s", scrubbed_target_url, e)
                            record_llm_client_disconnect()
                            _client_aborted = True

                        if usage_chunks:
                            try:
                                usage_body = b"".join(usage_chunks)
                                if content_encoding:
                                    # Whole-body decompress for trailing usage (gzip/deflate/br); failure abandons capture.
                                    usage_body = _decompress_full_for_usage(usage_body, content_encoding)
                                resp_payload = json.loads(usage_body)
                                usage = (resp_payload.get("usage")
                                        if isinstance(resp_payload, dict) else None)
                                if isinstance(usage, dict):
                                    _record_backend_token_usage(llm_backend, usage)
                            except Exception:
                                pass   # not a single-object JSON body (e.g. SSE) — skip, never breaks the proxy path

                        # A client abort mid-stream is the caller's event, not a verdict on this backend. Do not mark it ok or failed.
                        if llm_backend is not None and not _client_aborted:
                            _llm_mark_ok(llm_backend)   # connected + served — clear fail streak
                        # This return is the <400 stream path; >=400 already returned a typed S-08 JSON above.
                        _llm_req_failed = upstream.status >= 400
                        return proxy_resp

                except (ClientConnectionResetError, ServerDisconnectedError) as e:
                    # First-attempt retry on ClientConnectionResetError / ServerDisconnectedError (stale pooled socket); second failure is real.
                    if attempt < max_attempts - 1 and proxy_resp is None:
                        log.warning(
                            "Stale connection to %s (%s) — retrying once on a fresh "
                            "connection before treating this as a backend failure.",
                            scrubbed_target_url, e)
                        continue
                    raise

        except asyncio.CancelledError:
            # CancelledError is a BaseException, so except Exception will not see it. Do not absorb cancellation here either.
            raise

        except ClientError as ce:
            # Upstream is down or refused the connection: 503, the proxy itself is fine. Log scrubbed text only; a ClientError string can contain the URL and a credential. The client sees the exception class, not that text.
            log.error("Upstream unreachable %s: %s", scrubbed_target_url,
                      _short(_scrub_url_credentials(str(ce))))
            if llm_backend is not None:
                _llm_mark_fail(llm_backend)
                # This connect failure is a gateway-origin fault, not something the upstream said. Count it in the gateway group, and only when the call was credentialed.
                record_llm_gateway_fault(llm_backend, type(ce).__name__,
                                          credentialed=bool(backend_token),
                                          request_id=_safe_request_id(request))
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": f"Backend unreachable: {type(ce).__name__}"}, status=503,
                                      headers={"X-SM-Fault-Origin": "gateway"})

        except asyncio.TimeoutError:
            # Connect timeout to upstream — correct status is 504, not 500.
            log.warning("Upstream connect timeout: %s", scrubbed_target_url)
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
            # Log ClientError with a substitute exception carrying scrubbed text (traceback str() would re-embed the raw message).
            scrubbed_msg = _short(_scrub_url_credentials(str(e)))
            try:
                scrubbed_exc = type(e)(scrubbed_msg)
            except Exception:
                scrubbed_exc = RuntimeError(scrubbed_msg)  # exotic __init__ signature — fall back
            log.error("Unexpected proxy error for %s: %s", scrubbed_target_url, scrubbed_msg,
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
            # Release the inflight slot on every outcome so least-busy selection stays honest.
            # A client timeout can leave the backend generating; oldest-inflight age and suspect_wedged exist for that gap.
            if llm_backend is not None:
                _llm_inflight[llm_backend] = max(0, _llm_inflight.get(llm_backend, 0) - 1)
                starts = _llm_inflight_started.get(llm_backend)
                if starts:
                    starts.remove(min(starts))
                # Record pool-routed LLM latency on every exit except a client abort (abort duration is not a service time).
                if _llm_req_start_mono is not None and not _client_aborted:
                    _record_llm_latency(
                        llm_backend, time.monotonic() - _llm_req_start_mono, _llm_req_failed)


# Daemon token helpers

def _daemon_env(agent_name: str) -> dict:
    """Return non-secret environment variables for daemon subprocesses,
    preventing credential leakage via /proc/<pid>/environ."""
    return {k: v for k, v in os.environ.items() if not is_secret_key(k)}


# Digest of the ephemeral daemon token in _AGENT_TOKENS, not a persisted registry entry. A restart revokes the previous token instead of leaving both valid.
_ephemeral_daemon_token_digests: dict[str, str] = {}


def _mint_daemon_token(agent_name: str) -> str:
    """Mint an in-memory ephemeral bearer token for agent_name, registering its
    digest in coordinator._AGENT_TOKENS and revoking any previous token."""
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
    """Deregister ephemeral daemon token from _AGENT_TOKENS if spawn fails."""
    digest = _ephemeral_daemon_token_digests.pop(agent_name, None)
    if digest is not None:
        _AGENT_TOKENS.pop(digest, None)


def _daemon_env_and_token_fd(agent_name: str) -> "tuple[dict, int | None]":
    """Return child daemon environment and an inherited pipe read fd containing
    an ephemeral AGENT_TOKEN, or (env, None) if auth is disabled."""
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
        # If the write itself raised, close read_fd here. Nothing else on this error path does, and the caller never receives it.
        os.close(read_fd)
        _revoke_daemon_token(agent_name)
        raise
    env["AGENT_TOKEN_FD"] = str(read_fd)
    return env, read_fd


# Consolidation daemon lifecycle
def _find_uv() -> "str | None":
    """Resolve uv binary from PATH or ~/.local/bin and ~/.cargo/bin fallbacks."""
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


def _find_gateway_lock() -> str:
    """Resolve the pinned requirements-gateway.lock for daemon spawns (SEC-1)."""
    candidate = Path(__file__).resolve().parents[2] / "requirements-gateway.lock"
    if candidate.is_file():
        return str(candidate)
    return "requirements-gateway.lock"


async def _start_daemon() -> "asyncio.subprocess.Process | None":
    """Spawn the consolidation daemon, publishing _daemon_proc immediately to
    prevent orphaning processes if cancelled before caller assignment."""
    global _daemon_proc
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
            "--no-project",
            "--with-requirements", _find_gateway_lock(),
            "--with", "psycopg2-binary==2.9.12",
            "python", str(daemon_path),
            env=env,
            pass_fds=(read_fd,) if read_fd is not None else (),
        )
        _daemon_proc = proc
    except Exception:
        # The token is minted before spawn. If spawn fails, revoke it; nothing else holds it.
        if read_fd is not None:
            _revoke_daemon_token(_CONSOLIDATION_AGENT_NAME)
        raise
    finally:
        if read_fd is not None:
            os.close(read_fd)
    log.info("Consolidation daemon started (pid %d)", proc.pid)
    return proc


async def _start_rem_daemon() -> "asyncio.subprocess.Process | None":
    """Spawn the REM daemon, publishing _rem_proc immediately to prevent
    orphaning processes if cancelled before caller assignment."""
    global _rem_proc
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
            "--no-project",
            "--with-requirements", _find_gateway_lock(),
            "--with", "psycopg2-binary==2.9.12",
            "python", str(rem_path),
            env=env,
            pass_fds=(read_fd,) if read_fd is not None else (),
        )
        _rem_proc = proc
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
    """Run and supervise the REM daemon with exponential backoff and circuit-breaking.
    Revokes the ephemeral daemon token on every exit path in finally."""
    global _rem_proc, _rem_healthy

    try:
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
    finally:
        _revoke_daemon_token(_REM_DAEMON_AGENT_NAME)


async def _watchdog_daemon(stop_event: asyncio.Event) -> None:
    """Run and supervise the consolidation daemon with backoff and circuit-breaking.
    Revokes the ephemeral daemon token on every exit path in finally."""
    global _daemon_proc, _daemon_healthy

    try:
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

            # Reset backoff after a stable stretch, so a brief Postgres or LLM blip does not become a boot-loop penalty.
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
    finally:
        _revoke_daemon_token(_CONSOLIDATION_AGENT_NAME)


# Pool status: in-memory LLM capacity for the dreaming daemons
async def handle_pool_status(request: web.Request) -> web.Response:
    """GET /pool/status — report in-memory LLM pool availability for daemons.
    Returns backend availability and free slots, redacting details for anonymous callers
    when auth is configured."""
    if AUTH_CONFIGURED_AT_STARTUP and not bool(_safe_resolve_identity(request)):
        return web.json_response({})

    now = time.monotonic()
    # The session is only for the rare suspect-wedged probe. Tests call this handler with no real app.
    _app = getattr(request, "app", None)
    _session = _app["proxy"].session if _app is not None and "proxy" in _app else None
    backends, free = {}, 0
    for b in LLM_POOL:
        # Available means "has spare capacity", not "is idle": a hosted backend that declares max_inflight 32 has 31 slots free while one request is in flight, and reading it as busy stalled every dream cycle for the length of every request.
        avail = (_llm_inflight.get(b, 0) < _reported_max_inflight(b)
                 and _llm_unhealthy_until.get(b, 0.0) <= now
                 and b not in _llm_reserved)
        age = _oldest_inflight_age(b, now)
        # serves_all is visibility only: roles absent and private_ok. The free_slots count is the next flag, not this one.
        serves_all = _serves_all(b)
        # decision:1357: a backend with the full dream-role list counts as free, same as serves-all. Counting only serves-all zeroed free_slots and halted every dream daemon.
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
        # Probe a backend only when a request has been in flight past the suspect age. A busy generator still answers /health; a hung driver cannot.
        if age is not None and age > LLM_WEDGE_SUSPECT_AGE and _session is not None:
            entry["suspect_wedged"] = not await _probe_backend_alive(_session, b)
        backends[b] = entry
        free += 1 if (avail and counts_free) else 0
    # Scrub backend URL keys before this response leaves. A clean query-less URL is unchanged; collapsing two distinct keys must not merge them.
    backends = _scrub_backend_keyed_dict(backends, context="/pool/status")
    return web.json_response({"free_slots": free, "backends": backends})


# Capability probing: whether a backend can serve, not merely whether it answers.
# The probe costs inference on the backends that serve traffic, so the interval is long.
CAPABILITY_PROBE_INTERVAL_S = float(
    os.environ.get("CAPABILITY_PROBE_INTERVAL_S", "600"))
# fact:1609: retry a failing probe soon. Waiting the full interval turned a short encoder cold-start into a long degraded window.
# A too_slow backend is still serving, so it keeps the full interval.
CAPABILITY_PROBE_RETRY_S = float(
    os.environ.get("CAPABILITY_PROBE_RETRY_S", "15"))
# Large enough to represent a real payload. A one-token ping would not measure the cost that matters.
CAPABILITY_PROBE_DOCS = int(os.environ.get("CAPABILITY_PROBE_DOCS", "4"))
CAPABILITY_PROBE_DOC_CHARS = int(
    os.environ.get("CAPABILITY_PROBE_DOC_CHARS", "1000"))

# Probe result for /health. Stays unknown until the first probe; decision 928 says not-yet-probed must not read as verified clean.
_capability: dict = {"status": "unknown", "probed_at": None}


def _projection_age_s(last_ok_at, now=None) -> float | None:
    """Return seconds since numbers were measured, or None if unknown or in the future."""
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
    """Return published capability probe view with read-time projection ages,
    copying per-backend blocks to prevent caller mutation."""
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
    """Measure backend throughput on a representative payload and project latency
    against worst-case timeouts to verify full payload service."""
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
            allow_redirects=False,
        ) as r:
            await r.read()
            ok = 200 <= r.status < 300
        dt = max(time.monotonic() - t0, 1e-6)
        entry["latency_s"] = round(dt, 2)
        entry["throughput_chars_s"] = round(probe_chars / dt)
        if ok and entry["throughput_chars_s"] > 0:
            # Worst case this framework can send: a full candidate set of fully clamped documents.
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
            allow_redirects=False,
        ) as r:
            await r.read()
            ok = 200 <= r.status < 300
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


# Capability measured keys travel together; serves_full_payload is a present-tense verdict and is not carried here (null while the reading ages).
_PROJECTION_CARRY_KEYS = ("projected_full_payload_s", "ceiling_s",
                          "throughput_chars_s", "latency_s")


def _merge_capability_projection(previous: dict | None, fresh: dict) -> dict:
    """Retain last measured projection metrics across probe failures (fact:1560 —
    the client's search timeout collapsed to its 30 s floor exactly when the
    reranker was busy, because a failing probe wrote only status and the
    measured projection vanished).
    Marks projections stale when carried over and sets serves_full_payload to None.
    A backend that has NEVER measured keeps today's shape with projection_stale
    None: "never measured" is a third state, and no number is invented to fill it."""
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
            # Nothing was measured, so carry nothing and invent nothing. Null the verdict too, the same shape as an ageing block.
            block["serves_full_payload"] = None
            block["projection_stale"] = None
            continue
        for key in _PROJECTION_CARRY_KEYS:
            if key in prev_block:
                block[key] = prev_block[key]
            else:
                block.pop(key, None)
        # The verdict does not travel with a carried-forward reading. Null, not absent, so a missing key is not read as false and a failed probe cannot stay green.
        block["serves_full_payload"] = None
        block["projection_stale"] = True
        # The previous block may itself have been carried forward. The age is the age of the numbers, not of the cycle that last copied them.
        block["last_ok_at"] = (prev_block.get("last_ok_at")
                                or (previous or {}).get("probed_at"))
    return fresh


async def _capability_probe_daemon(proxy, stop_event, coordinator=None) -> None:
    """Periodically probe backend capability without raising uncaught exceptions.

    ⛔ Fresh readings are merged onto previous snapshots rather than replacing
    them wholesale so measured projections do not vanish when a backend is busy."""
    global _capability
    while not stop_event.is_set():
        try:
            from encoder_window import probe_encoder_window
            await probe_encoder_window(proxy.session, EMBEDDER_URL, RERANKER_URL)
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
    """Return retry_s if any backend is failing or unprobed, else interval_s."""
    interval_s = CAPABILITY_PROBE_INTERVAL_S if interval_s is None else interval_s
    retry_s = CAPABILITY_PROBE_RETRY_S if retry_s is None else retry_s
    cap = capability or {}
    blocks = [cap.get("reranker") or {}, cap.get("embedder") or {}]
    if not any(b.get("status") for b in blocks):
        return retry_s                      # never probed successfully
    if any(b.get("status") == "failing" for b in blocks):
        return retry_s
    return interval_s


# Capacity derivation is report-only (decision:1424): no request is limited, queued, or rejected here.
_MEM_SIZE_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([KMGT]?I?B?)$", re.IGNORECASE)
# Accept k8s binary suffixes (Ki, Mi, Gi, Ti) as well as KiB and the compose 8G form.
_MEM_SIZE_MULTIPLIERS = {
    "": 1, "B": 1,
    "K": 1024, "KB": 1024, "KI": 1024, "KIB": 1024,
    "M": 1024**2, "MB": 1024**2, "MI": 1024**2, "MIB": 1024**2,
    "G": 1024**3, "GB": 1024**3, "GI": 1024**3, "GIB": 1024**3,
    "T": 1024**4, "TB": 1024**4, "TI": 1024**4, "TIB": 1024**4,
}


def _parse_mem_size(raw: str | None) -> int | None:
    """Parse memory size strings into bytes, returning None without raising on error."""
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


# Capacity RAM subtrahends are declared allowances (env-overridable); Neo4j prefers configured heap+pagecache over the compose 8G cap.
CAPACITY_NEO4J_FALLBACK_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_NEO4J_FALLBACK_BYTES", "8G"))
# Postgres has no single heap figure (shared_buffers is a floor). The allowance is the container memory cap.
CAPACITY_PG_MEM_ALLOWANCE_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_PG_MEM_ALLOWANCE_BYTES", "4G"))
# Not measured: a steady-state allowance for the embedder on this host. Zero it when the embedder runs elsewhere.
CAPACITY_EMBEDDER_MEM_ALLOWANCE_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_EMBEDDER_MEM_ALLOWANCE_BYTES", "2G"))
# Not measured: this process's own steady-state footprint.
CAPACITY_GATEWAY_MEM_ALLOWANCE_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_GATEWAY_MEM_ALLOWANCE_BYTES", "512M"))
# Not measured: kernel, page cache, and the desktop session on a shared box.
CAPACITY_OS_MEM_MARGIN_BYTES = _parse_mem_size(
    os.environ.get("CAPACITY_OS_MEM_MARGIN_BYTES", "1G"))

# A bad CAPACITY_* number must not crash import. Log the name and the fallback, then use the default.
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


# Server-side search_ceiling mirrors the client formula (same defaults); CAPACITY_TOLERABLE_WAIT_S is what queue_bound actually uses.
CAPACITY_SEARCH_TIMEOUT_S          = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_S", 0.0, float)
CAPACITY_SEARCH_TIMEOUT_FLOOR_S    = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_FLOOR_S", 30.0, float)
CAPACITY_SEARCH_TIMEOUT_MAX_S      = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_MAX_S", 300.0, float)
CAPACITY_SEARCH_TIMEOUT_FALLBACK_S = _capacity_env_number("CAPACITY_SEARCH_TIMEOUT_FALLBACK_S", 120.0, float)
CAPACITY_SEARCH_SAFETY_FACTOR      = _capacity_env_number("CAPACITY_SEARCH_SAFETY_FACTOR", 1.5, float)
CAPACITY_SEARCH_OVERHEAD_S         = _capacity_env_number("CAPACITY_SEARCH_OVERHEAD_S", 15.0, float)

# queue_bound is measured against CAPACITY_TOLERABLE_WAIT_S, not client_ceiling_s (that one is informative only).
CAPACITY_TOLERABLE_WAIT_S = _capacity_env_number("CAPACITY_TOLERABLE_WAIT_S", 30.0, float)

# Capacity projects the observed-max rerank payload once CAPACITY_PAYLOAD_MIN_SAMPLES is met, else the theoretical 20×RERANK_MAX_DOC_CHARS worst case; mean is reported, never the bound (fact:1441).
CAPACITY_PAYLOAD_MIN_SAMPLES = _capacity_env_number("CAPACITY_PAYLOAD_MIN_SAMPLES", 5, int)

# probe_drift fires outside a ×N band of last derived chars/s; factor <= 1.0 disables the trigger.
CAPACITY_DRIFT_BAND_FACTOR = _capacity_env_number("CAPACITY_DRIFT_BAND_FACTOR", 2.0, float)

# JSON-lines capacity log, env-overridable, mode 0600. Keep the last N records; a non-positive cap keeps one.
CAPACITY_LOG_PATH = os.environ.get(
    "CAPACITY_LOG_PATH", "~/.shared-memory/capacity/derivations.jsonl")
CAPACITY_LOG_MAX_RECORDS = _capacity_env_number("CAPACITY_LOG_MAX_RECORDS", 20, int)

# Latest record for /health, so a hit does not read disk. None until one exists; do not treat absence as a measurement (decision 928).
_capacity_latest: dict | None = None
_capacity_latest_loaded_from_disk = False   # memoizes the one lazy disk read
# True after this process's first capability probe (startup fingerprint vs later config_change/probe_drift).
_capacity_first_probe_done = False

# payload_threshold_crossed compares live observed max to the durable stored max (no process-local latch) so a restart cannot freeze an under-reported worst case.


def _capacity_neo4j_allowance_bytes() -> int | None:
    """Return configured Neo4j heap+pagecache bytes if both parse, otherwise fallback default.
    Warns if either env var is set but fails parsing."""
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
    """Return hardware fingerprint (nproc, MemTotal, GPU presence), failing open without raising."""
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
        # Fingerprint only whether nvtop is installed. A runtime self-disable is not a hardware change and must not move a persisted fingerprint.
        from gpu_load import gpu_probe_installed
        out["gpu_present"] = bool(gpu_probe_installed())
    except Exception:
        pass   # never let GPU detection block a fingerprint
    return out


def _encoder_config_fingerprint() -> dict:
    """Return configuration subset affecting encoder probe measurements and capacity derivation."""
    from dream_telemetry import RERANK_MAX_DOC_CHARS
    from coordinator import SEARCH_CANDIDATE_FLOOR
    return {
        "rerank_max_doc_chars": RERANK_MAX_DOC_CHARS,
        "search_candidate_floor": SEARCH_CANDIDATE_FLOOR,
        # Strip userinfo before this URL is written to the on-disk JSONL log.
        "embedder_url": _scrub_url_credentials(EMBEDDER_URL or ""),
        "reranker_url": _scrub_url_credentials(RERANKER_URL or ""),
        "cpu_encoder_replicas": os.environ.get("CPU_ENCODER_REPLICAS", "1"),
        "gpu_encoder_replicas": os.environ.get("GPU_ENCODER_REPLICAS", "0"),
    }


def _capacity_fingerprint() -> dict:
    return {"hardware": _hardware_fingerprint(),
            "encoder_config": _encoder_config_fingerprint()}


def _capacity_client_ceiling_s(capability: dict | None) -> float:
    """Server-side mirror of memory_bridge.search_ceiling() for capacity derivation.

    ⚠ Falls back to CAPACITY_SEARCH_TIMEOUT_FALLBACK_S when any backend projection is
    unknown or failing (fact:1560 — the client's search timeout collapsed to its 30 s
    floor exactly when the reranker was busy, because a failing probe wrote only
    status and the measured projection vanished)."""
    if CAPACITY_SEARCH_TIMEOUT_S > 0:
        return CAPACITY_SEARCH_TIMEOUT_S

    projected, probed, unknown = 0.0, False, False
    for backend in ("reranker", "embedder"):
        block = (capability or {}).get(backend)
        if not isinstance(block, dict) or not block:
            # A missing or empty block is an unknown cost, not zero. Treating absence as zero is the under-count this ceiling exists to avoid.
            unknown = True
            continue
        try:
            value = float(block.get("projected_full_payload_s") or 0)
        except (TypeError, ValueError):
            # A malformed projection is an unknown cost, not zero. Fall through to the flag instead of skipping the block.
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
    """Return max search requests fitting within tolerable_wait_s, or None if unmeasured."""
    if not s_mean or s_mean <= 0:
        return None
    return max(0, int(tolerable_wait_s // s_mean))


def _capacity_recommended_mem_limit_bytes(mem_total_bytes: int | None) -> int | None:
    """Return MemTotal minus declared allowances, returning None if any allowance is invalid."""
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
    """Return True if current/basis ratio strictly exceeds band_factor or 1/band_factor."""
    if band_factor is None:
        band_factor = CAPACITY_DRIFT_BAND_FACTOR
    if not current or not basis or current <= 0 or basis <= 0 or band_factor <= 1:
        return False
    ratio = current / basis
    return ratio > band_factor or ratio < (1.0 / band_factor)


def _capacity_payload_stats(coordinator) -> dict:
    """Read cumulative rerank payload counters from coordinator (fact:1441 —
    the measured payload-wall cost curve, which exposed two derivation defects;
    the mean is reported and never the bound), failing open to zeros if absent."""
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
    """Return backend measurement timestamp from last_ok_at or capability probed_at."""
    stamp = block.get("last_ok_at") if isinstance(block, dict) else None
    return stamp or (capability or {}).get("probed_at")


def _build_capacity_record(capability: dict | None, fingerprint: dict,
                            trigger: str, coordinator=None) -> dict:
    """Assemble a capacity derivation record from capability probe data and coordinator stats.
    Uses observed maximum payload for queue_bound once sample thresholds are met,
    reporting payload_basis and sample counts explicitly."""
    reranker = (capability or {}).get("reranker") or {}
    embedder = (capability or {}).get("embedder") or {}
    # Fixed theoretical full-payload projection, copied from the probe. The basis switch below does not change this value.
    s_mean_theoretical = reranker.get("projected_full_payload_s")
    reranker_chars_per_s = reranker.get("throughput_chars_s")

    payload_stats = _capacity_payload_stats(coordinator)
    have_enough_samples = payload_stats["samples"] >= CAPACITY_PAYLOAD_MIN_SAMPLES
    have_throughput = bool(reranker_chars_per_s and reranker_chars_per_s > 0)

    # Mean projection is context only. It must not feed queue_bound; an average under-states a search at the observed max.
    s_mean_measured = None
    try:
        if (have_enough_samples and have_throughput
                and payload_stats["mean_chars_per_search"] is not None):
            s_mean_measured = round(
                payload_stats["mean_chars_per_search"] / reranker_chars_per_s, 1)
    except (TypeError, ZeroDivisionError):
        s_mean_measured = None

    # Worst-case basis. Once enough samples exist, this observed max feeds queue_bound, not the mean above.
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

    # queue_bound / single_search_exceeds_wait use effective_s_mean (observed max when sampled, else theoretical); s_mean_s itself stays the theoretical figure.
    client_ceiling = _capacity_client_ceiling_s(capability)
    queue_bound = _capacity_queue_bound(effective_s_mean, CAPACITY_TOLERABLE_WAIT_S)
    # queue_bound 0 means one search already exceeds the wait. None means not measured; do not collapse those.
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
            # Keep the probe status with the reading. Drift must not trust a throughput that was never ok.
            "reranker_status": reranker.get("status"),
            "embedder_chars_per_s": embedder.get("throughput_chars_s"),
            "probed_at": (capability or {}).get("probed_at"),
            # Carried throughput travels with the timestamp it was measured at; probe_stale says the block mixes cycles.
            "reranker_measured_at": _probe_measured_at(capability, reranker),
            "embedder_measured_at": _probe_measured_at(capability, embedder),
            "probe_stale": bool(reranker.get("projection_stale")
                                or embedder.get("projection_stale")),
        },
        "derived": {
            # Always the theoretical full-payload projection, whatever basis fed queue_bound.
            "s_mean_s": s_mean_theoretical,
            # s_max_measured_s is the observed-max projection that feeds queue_bound once CAPACITY_PAYLOAD_MIN_SAMPLES is met.
            "s_max_measured_s": s_max_measured,
            # Mean projection, context only. It must not feed queue_bound; an average under-states a search at the observed max.
            "s_mean_measured_s": s_mean_measured,
            # Which basis fed queue_bound on this record. The name stays honest only because this field says so.
            "payload_basis": payload_basis,
            # Real searches this process has served, even when the basis is still theoretical. Do not force the count to 0 below the sample threshold.
            "payload_basis_sample_count": payload_stats["samples"],
            # Observed mean payload chars. None when nothing has been seen; 0 would mean a measured empty payload.
            "payload_mean_chars_measured": mean_chars_measured,
            # Observed maximum payload chars, the number behind the measured projection. None on zero samples, and it does not decrease for this process.
            "payload_max_chars_measured": max_chars_measured,
            "client_ceiling_s": client_ceiling,
            "queue_bound": queue_bound,
            # Store the wait the queue_bound was measured against. A later change to the default must not make an old record unreadable.
            "tolerable_wait_s": CAPACITY_TOLERABLE_WAIT_S,
            "single_search_exceeds_wait": single_search_exceeds_wait,
            # No staleness flag on the derived capacity record; projection liveness lives on backend_capability.*.projection_stale.
            "recommended_reranker_mem_limit_bytes": recommended_mem_limit,
        },
    }


def _read_capacity_records_sync(path: str) -> list[dict]:
    """Read capacity records from JSONL file, skipping malformed lines and returning [] if missing."""
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
    """Atomically write capacity records to temporary file with O_NOFOLLOW and replace path."""
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
    """Append record and prune CAPACITY_LOG_PATH off the event loop."""
    def _do() -> None:
        records = _read_capacity_records_sync(CAPACITY_LOG_PATH)
        records.append(record)
        # records[-0:] is the whole list. A non-positive cap used to keep every record; clamp to 1 so at least the latest remains.
        max_records = CAPACITY_LOG_MAX_RECORDS if CAPACITY_LOG_MAX_RECORDS > 0 else 1
        records = records[-max_records:]
        _write_capacity_records_sync(CAPACITY_LOG_PATH, records)
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do)
    except RuntimeError:
        _do()   # no running loop (sync caller or test); inline, same fallback as AsyncLineWriter.write


def _last_capacity_record() -> dict | None:
    records = _read_capacity_records_sync(CAPACITY_LOG_PATH)
    return records[-1] if records else None


def capacity_snapshot() -> dict | None:
    """Return latest derivation record or None, lazily loaded from disk if uninitialized."""
    global _capacity_latest, _capacity_latest_loaded_from_disk
    if _capacity_latest is None and not _capacity_latest_loaded_from_disk:
        _capacity_latest_loaded_from_disk = True
        try:
            _capacity_latest = _last_capacity_record()
        except Exception:
            pass   # fail-open — a corrupt/unreadable log must not break /health
    return dict(_capacity_latest) if _capacity_latest is not None else None


def _log_capacity_change(trigger: str, last: dict | None, record: dict) -> None:
    """Log capacity re-derivations, emitting INFO for baselines/recoveries and WARNING
    with a verification tail for hardware changes (fact:1425 — operator mid-build
    rulings; A2 is that every hardware-era change should produce a fingerprint tail)."""
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
        # The number that moved is reranker chars/s. Leading with MemTotal, which did not change, reads as if nothing happened.
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
    """Evaluate capacity triggers each probe cycle, deriving and persisting records on change.

    ⛔ NEVER raises: this rides the same observability path as
    _probe_capability, so a bug here must not take down the probe daemon it
    reports through (the Group 3 trap — a metric added to a work path changes
    that path's failure modes)."""
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
                # First-ever capacity baseline (no prior record), not a mismatch.
                if current_status != "ok":
                    log.info(
                        "capacity baseline deferred -- reranker probe not "
                        "ok yet; will derive on the first healthy probe")
                    return
                trigger = "first_derivation"
            elif last.get("fingerprint") != fingerprint:
                trigger = "gateway_start_fingerprint_mismatch"
        else:
            # config_change still runs every cycle: the log is shared, so a differently-configured process can have written the last record.
            if last is None:
                # No prior record on a later cycle is a fresh baseline, not a mismatch. A not-ok probe must not become that first stored basis.
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
                # A probe that was not ok can report a fantasy throughput. Do not fire drift, and do not replace the stored basis, unless both sides were ok.
                if (basis_status == "ok" and current_status == "ok"
                        and _capacity_drift_outside_band(current, basis)):
                    trigger = "probe_drift"

        if trigger is None and last is not None:
            # Recover a stored not-ok/legacy basis on the next healthy probe when nothing else triggered a re-derive.
            basis_status = (last.get("probe") or {}).get("reranker_status")
            if basis_status != "ok" and current_status == "ok":
                trigger = "basis_recovery"

        # Last trigger: re-derive when live observed max exceeds the durable stored max (restart/retry-safe; never regresses a larger stored worst case).
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


# /health TTL cache (S-11): in-process, a few seconds, so a poll burst does not re-fan-out every upstream.
HEALTH_CACHE_TTL_S = float(os.environ.get("HEALTH_CACHE_TTL_S", "3"))
_health_cache: dict = {"checks": None, "ts": 0.0}
# One in-flight health probe per cache miss. A Lock may be created before a loop is running; it binds on first use.
_health_probe_lock = asyncio.Lock()


# /health HTTP 503 is only embedder or reranker down; every other degraded dependency is 200 with status in the body (decision:1785).

_STATE_OK, _STATE_DEGRADED, _STATE_DOWN = "ok", "degraded", "down"
_STATE_UNKNOWN = "unknown"

# Last state per dependency, and which warnings are up, so a transition logs and a steady state does not.
# Process-wide: every caller shares the probe, so these are not per client.
_dependency_state: dict[str, str] = {}
_warning_state: set = set()


def _dep(state: str, reason: str | None = None) -> dict:
    return {"state": state, "reason": reason}


def _encoder_dependency(probe: str, capability: object, window: dict | None = None,
                        kind: str = "embedder") -> dict:
    """Return encoder dependency state.

    ⛔ Liveness is not capability: slow capability degrades rather than marks down.
    Enforces window contracts (decision:2540 — the encoder window contract: a conservative
    token-reserve clamp, a runtime probe and a postflight check, because encoders advertising
    8192 tokens were sent payloads tokenising to 8193), treating reranker overflow as prefix
    scoring (decision:2557 — the reranker's 8192 is a ranking prefix it scores, not a rejection
    boundary; only the embedder's 8192 rejects)."""
    if probe != "ok":
        return _dep(_STATE_DOWN, f"probe:{probe}")
    if isinstance(window, dict):
        adv = window.get("advertised_tokens")
        from dream_telemetry import EMBED_MAX_CONTEXT_TOKENS
        required = EMBED_MAX_CONTEXT_TOKENS
        if isinstance(adv, int) and adv < required:
            return _dep(_STATE_DEGRADED, f"window_short:{adv}<{required}")
        if kind != "reranker" and window.get("full_payload_ok") is False:
            return _dep(_STATE_DEGRADED, "window_overrun")
    if isinstance(capability, dict):
        status = capability.get("status")
        if status in ("too_slow", "failing", "degraded"):
            return _dep(_STATE_DEGRADED, f"capability:{status}")
    return _dep(_STATE_OK)


def _llm_pool_dependency(backend_status: dict) -> dict:
    """Return LLM pool dependency state, prioritizing liveness before configuration.

    Evaluates down/degraded states composing reasons (decision:1832 — the nothing-eligible
    verdict lives in the rem and nrem dependency reasons rather than in a new /health surface),
    warning if fleet configuration is empty or legacy fallbacks serve (⚠)."""
    if not backend_status:
        return _dep(_STATE_UNKNOWN, "no backend configured")
    # decision:374 and fact:375: unknown means not probed yet, not down. A pool nobody has probed stays unknown and must not be raised to down.
    probed = {b: s for b, s in backend_status.items() if s != "unknown"}
    if not probed:
        return _dep(_STATE_UNKNOWN, "not yet probed")
    bad = sorted(b for b, s in probed.items() if s != "ok")
    if len(bad) == len(probed):
        down_reasons: list = []
        if LLM_POOL_FALLBACK_REASON:
            # decision:1832: the declared fleet was entirely excluded and the fallback is also unreachable. Keep that reason; do not drop it.
            down_reasons.append(LLM_POOL_FALLBACK_REASON)
        if LLM_POOL_CONFIG_EMPTY:
            down_reasons.append(
                f"nothing serves the built-in fallback "
                f"({scrub_url_credentials(DEFAULT_TARGET)}, no backend declared)")
        down_reasons.append(f"all {len(bad)} backend(s) down")
        return _dep(_STATE_DOWN, "; ".join(down_reasons))
    reasons: list = []
    if LLM_POOL_FALLBACK_REASON:
        # The configured fleet was entirely excluded and the legacy fallback is what is serving.
        reasons.append(LLM_POOL_FALLBACK_REASON)
    if LLM_POOL_CONFIG_EMPTY:
        # decision:1832: nothing was declared and the built-in fallback is up. Show that, instead of reading ok.
        config_empty_reason = (
            f"no backend declared — serving the built-in "
            f"{scrub_url_credentials(DEFAULT_TARGET)} fallback")
        if LLM_POOL_LEGACY_KEY_PRESENT:
            # A bare LLM_DEFAULT_TARGET override is not a declaration. Attach the same remedy this reason carries for a live CSV.
            config_empty_reason = f"{config_empty_reason}; {_LLM_POOL_LEGACY_REMEDY}"
        reasons.append(config_empty_reason)
    if bad:
        reasons.append(f"{len(bad)}/{len(probed)} backend(s) down")
    ineligible_reason = _all_roles_ineligible()
    if ineligible_reason:
        reasons.append(ineligible_reason)
    if reasons:
        return _dep(_STATE_DEGRADED, "; ".join(reasons))
    return _dep(_STATE_OK)


def _outbox_dependency(census: object, age_limit_s: int) -> dict:
    """Return outbox dependency state, reporting degraded if failed rows exist
    or pending rows exceed age limits."""
    if not isinstance(census, dict):
        return _dep(_STATE_UNKNOWN, "not yet probed")
    failed = census.get("failed") or 0
    if failed:
        return _dep(_STATE_DEGRADED, f"failed:{failed}")
    age = census.get("oldest_pending_age_s")
    if isinstance(age, (int, float)) and age > age_limit_s:
        return _dep(_STATE_DEGRADED, f"oldest_pending_age_s:{int(age)}")
    return _dep(_STATE_OK)


def _dream_slots_impossible_reason() -> "str | None":
    """Return explanation if no backend provides dream slots (decision:1832 —
    the nothing-eligible verdict lives in the rem and nrem dependency reasons rather
    than in a new /health surface), else None."""
    if any(_counts_free_slot(b) for b in LLM_BACKENDS):
        return None
    return ("no backend counts toward dream slots — REM and NREM will never "
            "run against this fleet")


def _rem_dependency(process_running: bool, dead_lettered: object) -> dict:
    """Return REM daemon dependency state checking process liveness, dead letters, and
    dream slot configuration (decision:1832 — the nothing-eligible verdict lives in the
    rem and nrem dependency reasons rather than in a new /health surface)."""
    if not process_running:
        return _dep(_STATE_DOWN, "process not running")
    dead_letter_reason = (f"dead_letters:{dead_lettered}"
                          if isinstance(dead_lettered, int) and dead_lettered > 0
                          else None)
    slots_reason = _dream_slots_impossible_reason()
    if dead_letter_reason and slots_reason:
        return _dep(_STATE_DEGRADED, f"{dead_letter_reason}; {slots_reason}")
    if dead_letter_reason:
        return _dep(_STATE_DEGRADED, dead_letter_reason)
    if slots_reason:
        return _dep(_STATE_DEGRADED, slots_reason)
    return _dep(_STATE_OK)


def _nrem_dependency(process_running: bool, consolidation: object,
                     attempt_floor: int) -> dict:
    """Return NREM daemon dependency state evaluating liveness, fold successes, and
    dream slot configuration (decision:1832 — the nothing-eligible verdict lives in the
    rem and nrem dependency reasons rather than in a new /health surface)."""
    if not process_running:
        return _dep(_STATE_DOWN, "process not running")
    slots_reason = _dream_slots_impossible_reason()
    if not isinstance(consolidation, dict):
        if slots_reason:
            return _dep(_STATE_DEGRADED, slots_reason)
        return _dep(_STATE_UNKNOWN, "not yet probed")
    if consolidation.get("stalled"):
        types = consolidation.get("stalled_types") or []
        reason = f"stalled:{','.join(types)}" if types else "stalled"
        if slots_reason:
            reason = f"{reason}; {slots_reason}"
        return _dep(_STATE_DEGRADED, reason)
    attempted = 0
    succeeded = 0
    for key, block in consolidation.items():
        if isinstance(block, dict) and "folds_attempted_24h" in block:
            attempted += block.get("folds_attempted_24h") or 0
            succeeded += block.get("folds_succeeded_24h") or 0
    if attempted >= attempt_floor and succeeded == 0:
        reason = f"folds_attempted_24h:{attempted} succeeded:0"
        if slots_reason:
            reason = f"{reason}; {slots_reason}"
        return _dep(_STATE_DEGRADED, reason)
    if slots_reason:
        return _dep(_STATE_DEGRADED, slots_reason)
    return _dep(_STATE_OK)


def _registry_dependency(read_failures: object,
                         census_failures: object = 0) -> dict:
    """Return registry dependency state from read search failures and internal probe census failures.

    ⛔ Both search-path read failures and health-probe census failures must reach
    the verdict so failures in the health instrument itself do not read as clean."""
    if not isinstance(read_failures, int):
        return _dep(_STATE_UNKNOWN, "not yet probed")
    reasons = []
    if read_failures > 0:
        reasons.append(f"read_failures:{read_failures}")
    if isinstance(census_failures, int) and census_failures > 0:
        reasons.append(f"census_failures:{census_failures}")
    if reasons:
        return _dep(_STATE_DEGRADED, " ".join(reasons))
    return _dep(_STATE_OK)


def _warning(key: str, limit, observed, unit: str) -> dict:
    return {"key": key, "limit": limit, "observed": observed, "unit": unit}


# Shed warning is a health-build delta, not a cumulative counter, so it can clear. token_verify_failed uses the timestamp ring, not this helper.
_rate_marks: dict[str, tuple[int, float]] = {}


def _delta_per_min(key: str, total: int) -> float | None:
    """Return events per minute since last call for key, or None on first call or counter resets."""
    now = time.monotonic()
    prev = _rate_marks.get(key)
    _rate_marks[key] = (total, now)
    if prev is None:
        return None
    prev_total, prev_at = prev
    elapsed = now - prev_at
    if elapsed <= 0:
        return None
    # A counter that went down means the process restarted. There is no rate across a restart, only a new baseline.
    if total < prev_total:
        return None
    return round((total - prev_total) * 60.0 / elapsed, 2)


def _gateway_shed_rate() -> int:
    """Load-shed 503s since the previous health build. 0 clears the warning."""
    try:
        total = telemetry_gateway_counters().get("shed_503_total", 0)
        now = time.monotonic()
        prev = _rate_marks.get("shed")
        _rate_marks["shed"] = (total, now)
        if prev is None or total < prev[0]:
            return 0
        return total - prev[0]
    except Exception:
        return 0


def _token_verify_failure_rate(now: float | None = None) -> float | None:
    """Return count of token_verify_failed events within a 60 s sliding window,
    reading from coordinator telemetry ring and saturating at its 256-entry capacity."""
    try:
        if now is None:
            now = time.monotonic()
        return float(sum(1 for ts in telemetry_token_verify_ring()
                          if now - ts <= 60.0))
    except Exception:
        return None


def overall_status(dependencies: dict, warnings: list) -> str:
    """Compute overall status (down, degraded, ok).

    ⚠ unknown never elevates: unprobed dependencies do not report degraded."""
    states = [d.get("state") for d in dependencies.values()]
    if _STATE_DOWN in states:
        return _STATE_DOWN
    if _STATE_DEGRADED in states or warnings:
        return _STATE_DEGRADED
    return _STATE_OK


def _log_health_transitions(dependencies: dict, warnings: list) -> None:
    """Log dependency state changes and warning transitions on state shifts,
    avoiding steady-state poll noise."""
    try:
        for name, dep in dependencies.items():
            new = dep.get("state")
            old = _dependency_state.get(name)
            if old == new:
                continue
            _dependency_state[name] = new
            if new in (_STATE_DOWN, _STATE_DEGRADED):
                log.warning("health.%s: %s -> %s (%s)", name, old or "unknown",
                            new, dep.get("reason"))
            else:
                log.info("health.%s: %s -> %s", name, old or "unknown", new)
        keys = {w["key"] for w in warnings}
        for w in warnings:
            if w["key"] not in _warning_state:
                log.warning("health.warning.%s RAISED: observed=%s limit=%s %s",
                            w["key"], w["observed"], w["limit"], w["unit"])
        for cleared in sorted(_warning_state - keys):
            log.info("health.warning.%s cleared", cleared)
        _warning_state.clear()
        _warning_state.update(keys)
    except Exception:
        # A logging failure must never take the health payload with it.
        pass


def _scrub_backend_keyed_dict(d: dict, *, context: str) -> dict:
    """Scrub credentials from dict keys, appending positional disambiguation suffixes
    on key collisions to preserve entries without exposing credentials."""
    scrubbed = {scrub_url_credentials(k): v for k, v in d.items()}
    if len(scrubbed) != len(d):
        log.error(
            "%s: scrubbing backend URL keys collapsed %d distinct key(s) into "
            "%d — rendering scrubbed, positionally de-duplicated keys instead "
            "of the raw (credential-bearing) originals.", context, len(d), len(scrubbed))
        return {
            f"{scrub_url_credentials(k)}#{i}": v
            for i, (k, v) in enumerate(d.items())
        }
    return scrubbed


def _llm_runtime_snapshot(backend_status: dict | None = None) -> dict:
    """Build in-memory snapshot of LLM routing, token usage, latency, and pool state.
    Uses cached backend_status without performing network I/O, scrubbing backend keys."""
    now = time.monotonic()
    total_routed = sum(_llm_routed.values()) or 1
    aff_total = _llm_affinity_hits + _llm_affinity_misses
    pool = {
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
    token_usage = {
        b: {
            "tokens_prompt_total": _llm_tokens_prompt_total.get(b, 0),
            "tokens_completion_total": _llm_tokens_completion_total.get(b, 0),
            "tokens_last_ts": _llm_tokens_last_ts.get(b),
        }
        for b in LLM_BACKENDS
    }
    latency = {
        b: {
            "requests_total": _llm_requests_total.get(b, 0),
            "requests_failed_total": _llm_requests_failed_total.get(b, 0),
            "latency_sum_s": round(_llm_latency_sum_s.get(b, 0.0), 6),
            "latency_max_s": round(_llm_latency_max_s.get(b, 0.0), 6),
            "latency_last_ts": _llm_latency_last_ts.get(b),
        }
        for b in LLM_BACKENDS
    }
    return {
        "backends": _scrub_backend_keyed_dict(dict(backend_status or {}),
                                              context="_llm_runtime_snapshot.backends"),
        "reserved": sorted(scrub_url_credentials(b) for b in _llm_reserved),
        # Per-backend weight, inflight, routed, fails, and cooldown, so the realised split can be checked against the weights.
        "pool": _scrub_backend_keyed_dict(pool, context="_llm_runtime_snapshot.pool"),
        # Affinity hit rate and which backend holds each hot prefix, so a KV-cache win is visible.
        "affinity": {
            "hits": _llm_affinity_hits,
            "misses": _llm_affinity_misses,
            "hit_rate": round(_llm_affinity_hits / aff_total, 3) if aff_total else None,
            "hot_prefixes": {k[:8]: {"backend": scrub_url_credentials(v[0]), "hits": v[2]}
                             for k, v in _llm_affinity.items()
                             if now - v[1] <= AFFINITY_TTL},
        },
        # fact:1314: flat routing counters, each with its own last-event timestamp. Kept even for a single role-scoped backend.
        "routing": {
            "routed_role_extract": _llm_routed_by_role.get("extract", 0),
            "routed_role_extract_last_ts": _llm_routed_by_role_last_ts.get("extract"),
            "routed_role_judge": _llm_routed_by_role.get("judge", 0),
            "routed_role_judge_last_ts": _llm_routed_by_role_last_ts.get("judge"),
            "routing_no_eligible_backend": _routing_no_eligible_backend_count,
            "routing_no_eligible_backend_last_ts": _routing_no_eligible_backend_last_ts,
            "routing_fit_rejected": _routing_fit_rejected_count,
            "routing_fit_rejected_last_ts": _routing_fit_rejected_last_ts,
            "routing_backend_at_capacity": _routing_backend_at_capacity_count,
            "routing_backend_at_capacity_last_ts": _routing_backend_at_capacity_last_ts,
        },
        # In-process token totals; they reset on restart. The paired timestamp is what makes a restart-aware delta possible.
        "token_usage": _scrub_backend_keyed_dict(token_usage, context="_llm_runtime_snapshot.token_usage"),
        # Latency sum and request count, so the reader computes the average. Failures are separate so fast errors do not dilute successes.
        "latency": _scrub_backend_keyed_dict(latency, context="_llm_runtime_snapshot.latency"),
    }


def _config_snapshot() -> dict:
    """Return effective non-secret configuration resolved from environment.

    ⛔ Secrets are never exposed: credentials appear only as booleans (fact:898 —
    /health's config.llm_backends block reports url and weight and never exposes
    a backend token)."""
    import dream_telemetry
    cfg = {
        # url is scrubbed text inside a list of dicts, so two backends cannot collapse into one key. A clean query-less URL is unchanged.
        "llm_backends": [
            {"url": scrub_url_credentials(b), "weight": LLM_WEIGHTS.get(b, 1.0),
             "has_credential": LLM_BACKEND_TOKENS.get(b) is not None,
             "model": LLM_BACKEND_MODELS.get(b),
             # Descriptor fields for display. Price is not an input to routing.
             "roles": sorted(LLM_BACKEND_ROLES[b]) if LLM_BACKEND_ROLES.get(b) else None,
             "n_ctx": LLM_BACKEND_NCTX.get(b),
             "private_ok": LLM_BACKEND_PRIVATE_OK.get(b, False),
             "max_inflight": LLM_BACKEND_MAX_INFLIGHT.get(b),
             "price_per_mtok_in": LLM_BACKEND_PRICE_IN.get(b),
             "price_per_mtok_out": LLM_BACKEND_PRICE_OUT.get(b)}
            for b in LLM_BACKENDS
        ],
        "llm_pool_tuning": {
            "fail_threshold": LLM_FAIL_THRESHOLD,
            # The transport threshold above and this one are separate on purpose, and an operator reading only one of them would misjudge how tolerant the pool is of a hosted provider (see .env.example).
            "http_fail_threshold": LLM_HTTP_FAIL_THRESHOLD,
            "fail_window_s": LLM_FAIL_WINDOW,
            "cooldown_s": LLM_COOLDOWN,
        },
        "llm_affinity": {
            "prefix_chars": AFFINITY_PREFIX_CHARS,
            "ttl_s": AFFINITY_TTL,
            "max_inflight": AFFINITY_MAX_INFLIGHT,
        },
        "embed_max_chars": dream_telemetry.EMBED_MAX_CHARS,
    }
    # Present only while auth is off and a live provider key is exposed. Omitted otherwise, so an older monitor still renders.
    if _unauthenticated_provider_keys_override_active():
        cfg["allow_unauthenticated_provider_keys"] = True
    return cfg


def telemetry_extras() -> dict:
    """Return telemetry blocks served by proxy for /memory/telemetry.

    ⛔ No network probes run here: reads cached health probe enums synchronously
    to avoid per-request upstream network fan-out."""
    cached = _health_cache.get("checks") or {}
    rt = _llm_runtime_snapshot(cached.get("llm_backends") or {})
    llm: dict = {
        "status": cached.get("llm"),
        "backends": rt["backends"],
        "reserved": rt["reserved"],
        "oldest_inflight_age_s": cached.get("llm_oldest_inflight_age_s"),
        "suspect_wedged": cached.get("llm_suspect_wedged") or [],
        "pool": rt["pool"],
        "affinity": rt["affinity"],
        "routing": rt["routing"],
        "token_usage": rt["token_usage"],
        "latency": rt["latency"],
        # The fault snapshot is keyed by the raw backend URL. Scrub it here, and do not let two distinct URLs collapse to one key.
        "faults": _scrub_backend_keyed_dict(_llm_faults_snapshot(), context="telemetry_extras.faults"),
    }
    return {
        "llm": llm,
        "config": _config_snapshot(),
        "capacity": capacity_snapshot(),
    }


def _coordinator_health_keys(coordinator) -> dict:
    """Derive consolidation health keys for /health from coordinator's cached snapshot
    (decision:362 — ADR-018 locked a cached consolidation snapshot so /health makes no database call).
    Fails open to unknown defaults without raising."""
    try:
        consolidation = coordinator.consolidation_health()
        return {
            "consolidation": consolidation,
            # GPU busy/idle/unknown from the cached snapshot. /health must not shell out to nvtop, and unknown must not be reported as idle.
            "inference_busy": consolidation.get("inference_busy", "unknown"),
            # Mis-labelled graph nodes, not a dream-cycle metric, so this stays top-level. None means not probed yet, not verified clean (decision 928).
            "graph_invalid_nodes": consolidation.get("graph_invalid_nodes"),
            # Upgrade completeness for project identity, not a dream-cycle metric. None means not probed yet, not complete.
            "project_identity": consolidation.get("project_identity"),
            # Same upgrade signal for the domain axis: registry versus graph, and whether every section is attached to its project. None means not probed yet.
            "domain_identity": consolidation.get("domain_identity"),
            # fact:1645: nvtop self-health stays top-level so a monitor need not open the consolidation tile. None means not probed yet, not ok.
            "gpu_probe": consolidation.get("gpu_probe"),
        }
    except Exception:
        return {
            "consolidation": {"fresh": False},
            "inference_busy": "unknown",
            "graph_invalid_nodes": None,
            "project_identity": None,
            "domain_identity": None,
            "gpu_probe": None,
        }



_llm_status_cache: dict[str, str] = {}

# Probe cadence, env-overridable because a metered endpoint and a loopback one cost nothing alike (the portability rule: our layout is one valid configuration, never the only one).
LLM_PROBE_INTERVAL_S = max(0.5, float(os.environ.get("LLM_PROBE_INTERVAL_S", "3.0")))
# A credentialed backend is probed far less often: at the 3 s loop cadence a hosted provider takes about 28,800 authenticated GET /v1/models per day, which can exhaust a request quota on liveness alone and invites the rate limiting that used to read as an outage.
# fact:1338: 30 s is UNMEASURED — no provider quota was sampled to derive it.
LLM_PROBE_INTERVAL_CREDENTIALED_S = max(
    LLM_PROBE_INTERVAL_S,
    float(os.environ.get("LLM_PROBE_INTERVAL_CREDENTIALED_S", "30.0")))
# Last probe time per backend, so a credentialed backend can be skipped on a loop pass without slowing the loop for everyone else.
_llm_last_probe_at: dict[str, float] = {}


def _fresh_probe_map() -> dict:
    """The probe map for one pass, keyed off LLM_BACKENDS and never off the previous cache, so no entry can outlive the pool it describes; a backend still inside its own interval carries its last verdict forward rather than being dropped, which would read as never probed."""
    return {b: _llm_status_cache.get(b, "unknown") for b in LLM_BACKENDS}


def _probe_interval_for(backend: str) -> float:
    """How long to leave `backend` alone between probes: the credentialed interval when a provider key is attached, else the ordinary one."""
    if LLM_BACKEND_TOKENS.get(backend) is not None:
        return LLM_PROBE_INTERVAL_CREDENTIALED_S
    return LLM_PROBE_INTERVAL_S


def _classify_probe_status(status: int) -> str:
    """Classify backend probe HTTP status into liveness verdicts.

    Treats 429 as ok (live backend rate-limiting).
    ⛔ Every other 4xx stays not-ok, especially 404 (mistyped URLs would otherwise
    read healthy and never cool down) and 401/403 rejected keys (fact:1794 — a false
    llm_pool degraded because the health probe carried no backend credential, and
    DeepSeek answers 401 to any unauthenticated request on any path)."""
    if status < 400 or status == 429:
        return "ok"
    return f"http_{status}"


async def _llm_probe_daemon(proxy, stop_event) -> None:
    """Background loop to probe LLM backends (S7), off the request path."""
    global _llm_status_cache
    while not stop_event.is_set():
        new_status = _fresh_probe_map()
        now = time.monotonic()
        for b in LLM_BACKENDS:
            last = _llm_last_probe_at.get(b)
            if last is not None and now - last < _probe_interval_for(b):
                continue
            _llm_last_probe_at[b] = now
            try:
                async with proxy.session.get(_v1_models_probe_url(b), timeout=ClientTimeout(total=2.0),
                                             headers=_probe_headers(b), allow_redirects=False) as r:
                    new_status[b] = _classify_probe_status(r.status)
            except asyncio.TimeoutError:
                new_status[b] = "timeout"
            except Exception:
                new_status[b] = "down"
        _llm_status_cache = new_status
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LLM_PROBE_INTERVAL_S)
        except asyncio.TimeoutError:
            pass

async def _build_health_checks(proxy: "AsyncHiveMindProxy", coordinator) -> dict:
    """Full /health probe shape (S-10 disclosure is handle_health's job, applied after cache)."""
    checks: dict[str, str] = {}

    # Embedder and reranker are llama.cpp and expose /health. Do not probe a reasoning backend here; those servers do not standardize /health.
    for name, url in [
        ("embedder", f"{EMBEDDER_URL}/health"),
        ("reranker",  f"{RERANKER_URL}/health"),
    ]:
        try:
            timeout = ClientTimeout(total=2.0)
            async with proxy.session.get(url, timeout=timeout, allow_redirects=False) as r:
                checks[name] = "ok" if 200 <= r.status < 300 else f"http_{r.status}"
        except asyncio.TimeoutError:
            checks[name] = "timeout"
        except Exception:
            checks[name] = "down"

    # Encoder /health liveness is not capability; window/throughput come from the capability probe, not these GETs.
    checks["backend_capability"] = capability_snapshot()
    # decision:1424: latest capacity record, top-level and additive, not nested under the raw probe. None until the first derivation. Report only; it limits no request.
    checks["capacity"] = capacity_snapshot()
    from encoder_window import get_encoder_window_snapshot
    checks["encoder_window"] = get_encoder_window_snapshot()

    # Read the reasoning pool from the background probe cache. llm is ok if any backend is up; per-backend status is still reported.
    backend_status: dict[str, str] = dict(_llm_status_cache)
    if not backend_status:
        backend_status = {b: "unknown" for b in LLM_BACKENDS}

    # Scrub backend URL keys before they reach /health or /memory/telemetry. A clean query-less URL is unchanged; two distinct URLs must not collapse to one key.
    backend_status = _scrub_backend_keyed_dict(backend_status, context="/health backend_status")
    # unknown means the background probe has not landed, not that the backend is down.
    if any(s == "ok" for s in backend_status.values()):
        checks["llm"] = "ok"
    elif backend_status and all(s == "unknown" for s in backend_status.values()):
        checks["llm"] = "unknown"
    else:
        checks["llm"] = "down"
    # Reachability stays ok through a driver hang. Surface the oldest inflight age and flag the backend when its own health no longer answers.
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
                        # Scrub these URLs too. The list is rendered on checks["llm_suspect_wedged"].
                        _wedged.append(scrub_url_credentials(b))
            if _wedged:
                checks["llm_suspect_wedged"] = _wedged
    # llm_* runtime is built once by _llm_runtime_snapshot for any configured backend (including a one-backend fleet).
    _llm_rt = _llm_runtime_snapshot(backend_status)
    if LLM_BACKENDS:
        checks["llm_backends"] = backend_status
        if _llm_rt["reserved"]:
            checks["llm_reserved"] = _llm_rt["reserved"]
        checks["llm_pool"] = _llm_rt["pool"]
        checks["llm_affinity"] = _llm_rt["affinity"]
    checks["llm_routing"] = _llm_rt["routing"]
    if LLM_BACKENDS:
        checks["llm_token_usage"] = _llm_rt["token_usage"]
        checks["llm_latency"] = _llm_rt["latency"]

    # PID liveness only; HEALTH uses dependencies.rem_daemon / nrem_daemon. Bare `daemon`/`rem_daemon` are still filled then stripped by the contract.
    checks["nrem_daemon_process"] = "running" if _daemon_healthy else "stopped"
    checks["rem_daemon_process"]  = "running" if _rem_healthy    else "stopped"
    checks["daemon"]     = checks["nrem_daemon_process"]
    checks["rem_daemon"] = checks["rem_daemon_process"]

    # Clients compare api_version to detect skew. version is informational and needs no probe.
    checks["version"]     = FRAMEWORK_VERSION
    checks["api_version"] = API_VERSION

    # Non-secret resolved config on authenticated /health (no .env read); credentialed backends show has_credential, never the value.
    checks["config"] = _config_snapshot()

    # Startup flag, not live token emptiness. A daemon token minted after boot must not mark an install that never configured auth as authenticated.
    checks["auth_required"] = AUTH_CONFIGURED_AT_STARTUP
    # Advertise the auth scheme so a client can tell bearer tokens from a later proof-of-possession scheme.
    checks["auth_scheme"] = AUTH_SCHEME
    # True during backup quiesce, so a client can expect write 503s and the monitor can show a backup in progress.
    checks["backup_in_progress"] = backup_quiesce_active()

    # Dream-cycle and other cached coordinator signals. The lift is _coordinator_health_keys so it can be tested without this handler.
    if coordinator is not None:
        checks.update(_coordinator_health_keys(coordinator))

        # decision:1584 and fact:1583: pgvector version and hnsw.iterative_scan come from the coordinator startup probe, not a query here.
        # A null version means that probe failed; iterative_scan then stays false.
        checks["pgvector"] = {
            "version": getattr(coordinator, "pgvector_version", None),
            "iterative_scan": bool(getattr(coordinator, "hnsw_iterative_scan", False)),
        }

    # Dependencies, warnings, and the derived status. Everything below is already in hand.
    # decision:362: no database call here. /health runs on every client call and on a polling dashboard.
    dep_snap = {}
    if coordinator is not None:
        try:
            dep_snap = coordinator.dependency_snapshot()
        except Exception:
            dep_snap = {}
    consolidation = checks.get("consolidation") if isinstance(
        checks.get("consolidation"), dict) else {}
    outbox_census = dep_snap.get("outbox")
    capability = checks.get("backend_capability") or {}

    dependencies = {
        "postgres": dep_snap.get("postgres") or _dep(_STATE_UNKNOWN, "not yet probed"),
        "neo4j": dep_snap.get("neo4j") or _dep(_STATE_UNKNOWN, "not yet probed"),
        "embedder": _encoder_dependency(checks["embedder"],
                                        capability.get("embedder"),
                                        checks.get("encoder_window", {}).get("embedder"),
                                        kind="embedder"),
        "reranker": _encoder_dependency(checks["reranker"],
                                        capability.get("reranker"),
                                        checks.get("encoder_window", {}).get("reranker"),
                                        kind="reranker"),
        "llm_pool": _llm_pool_dependency(backend_status),
        "rem_daemon": _rem_dependency(
            _rem_healthy,
            (dep_snap.get("rem") or {}).get("dead_lettered")
            if isinstance(dep_snap.get("rem"), dict) else None),
        "nrem_daemon": _nrem_dependency(_daemon_healthy, consolidation,
                                        NREM_FOLD_ATTEMPT_WARN),
        "outbox": _outbox_dependency(outbox_census, OUTBOX_AGE_WARN_S),
        "registry": _registry_dependency(
            getattr(coordinator, "_axis_registry_read_failures", None)
            if coordinator is not None else None,
            getattr(coordinator, "_registry_census_failures", 0)
            if coordinator is not None else 0),
    }

    warnings: list = []
    # fact:1338: the encoder p95 limit comes from the capability probe unless an operator pinned one. A flat default would be a number nobody measured.
    for name in ("embedder", "reranker"):
        block = capability.get(name)
        if not isinstance(block, dict):
            continue
        limit_ms = ENCODER_LATENCY_WARN_MS
        if limit_ms is None:
            ceiling = block.get("ceiling_s")
            limit_ms = ceiling * 1000.0 if isinstance(ceiling, (int, float)) else None
        observed = block.get("projected_full_payload_s")
        if (limit_ms is not None and isinstance(observed, (int, float))
                and observed * 1000.0 > limit_ms):
            warnings.append(_warning(f"encoder_{name}_projected_ms",
                                     round(limit_ms, 1),
                                     round(observed * 1000.0, 1), "ms"))
    if isinstance(outbox_census, dict):
        age = outbox_census.get("oldest_pending_age_s")
        if isinstance(age, (int, float)) and age > OUTBOX_AGE_WARN_S:
            warnings.append(_warning("outbox_oldest_pending_age_s",
                                     OUTBOX_AGE_WARN_S, int(age), "s"))
    rem_block = dep_snap.get("rem")
    if isinstance(rem_block, dict) and (rem_block.get("dead_lettered") or 0) > 0:
        warnings.append(_warning("rem_dead_lettered", 0,
                                 rem_block["dead_lettered"], "records"))
    # Call _gateway_shed_rate once. It moves its own watermark, so a second call measures the gap between the calls and reports observed 0 (fact:1309).
    shed = _gateway_shed_rate()
    if shed > 0:
        warnings.append(_warning("gateway_shed_503_total", 0, shed, "requests"))
    tv_rate = _token_verify_failure_rate()
    if tv_rate is not None and tv_rate > TOKEN_VERIFY_WARN_PER_MIN:
        warnings.append(_warning("token_verify_failed_per_min",
                                 TOKEN_VERIFY_WARN_PER_MIN, tv_rate, "per_min"))

    checks["dependencies"] = dependencies
    checks["warnings"] = warnings
    checks["status"] = overall_status(dependencies, warnings)
    _log_health_transitions(dependencies, warnings)

    return checks


async def _health_probe_cached(proxy: "AsyncHiveMindProxy", coordinator) -> dict:
    """Return TTL-cached health check results with single-flight lock coalescing
    to prevent concurrent probe fan-out on cache expiry."""
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
    """read / write / admin via effective_role (admin is not write; read_only_agents() still confines regardless of AGENT_ROLES)."""
    role = effective_role(agent_name, _AGENT_ROLES.get(agent_name))
    if role == "read":
        return "read"
    if role == "admin":
        return "admin"
    return "write"


async def handle_health(request: web.Request) -> web.Response:
    """GET /health: anonymous auth-on installs get {status, version, api_version}; a valid bearer (or auth-off) gets the full payload. HTTP 503 only if embedder or reranker is down."""
    proxy: AsyncHiveMindProxy = request.app["proxy"]
    checks = await _health_probe_cached(proxy, request.app.get("coordinator"))
    # HTTP 503 only if embedder or reranker state is down; other degraded deps stay 200.
    _deps = checks.get("dependencies") or {}
    critical_down = any(
        (_deps.get(name) or {}).get("state") == _STATE_DOWN
        for name in ("embedder", "reranker")
    )
    status_code = 503 if critical_down else 200

    # /health is unprotected so auth_middleware never stashes a name; resolve here (skip on auth-off).
    identity = _safe_resolve_identity(request) if AUTH_CONFIGURED_AT_STARTUP else None
    if AUTH_CONFIGURED_AT_STARTUP and not identity:
        return web.json_response(
            {"status": checks["status"], "version": checks["version"],
             "api_version": checks["api_version"]},
            status=status_code,
        )
    if identity:
        # Copy before stamping agent/role — checks is the shared TTL cache.
        checks = {**checks, "agent": identity,
                  "role": _health_role_for(identity)}
    # Strip dropped keys on a fresh object at the response boundary, never off the cache telemetry_extras still reads.
    return web.json_response(strip_dropped(checks, HEALTH_CONTRACT),
                             status=status_code)


# Startup and shutdown
def require_no_backend_url_credentials() -> None:
    """Refuse startup if any backend URL embeds userinfo credentials (user:pass@host).
    Checked at startup rather than import time to allow test imports without killing
    collection."""
    if not _LLM_BACKEND_URL_CREDENTIAL_ERRORS:
        return
    raise SystemExit(
        "FATAL: backend URL(s) embed a credential in userinfo "
        "(user:pass@host) — this framework never accepts a credential "
        "inside the URL string itself:\n  "
        + "\n  ".join(_LLM_BACKEND_URL_CREDENTIAL_ERRORS)
        + "\nUse token_env instead (LLM_BACKENDS_JSON form): the backend "
          "gets its Authorization header from a NAMED env var already "
          "exported in the gateway's own process environment, never from "
          "the URL. A bare query string (e.g. \"?api-version=...\") is NOT "
          "refused — only userinfo is."
    )


def _unauthenticated_provider_keys_override_active() -> bool:
    """Return True if running unauthenticated with configured provider keys and
    ALLOW_UNAUTHENTICATED_PROVIDER_KEYS is set. Evaluated dynamically on each
    call for startup validation and /health monitoring."""
    if AUTH_CONFIGURED_AT_STARTUP:
        return False
    if not any(LLM_BACKEND_TOKENS.get(b) for b in LLM_BACKENDS):
        return False
    return os.environ.get("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", "").strip().lower() in ("1", "true", "yes", "on")


def require_auth_when_provider_keys_configured() -> None:
    """Refuse startup if provider keys are configured without AGENT_TOKENS or explicit
    ALLOW_UNAUTHENTICATED_PROVIDER_KEYS (decision:1303 — auth off with a live provider
    key attached to a backend refuses startup). Called from main() to avoid failing test
    collection."""
    if AUTH_CONFIGURED_AT_STARTUP:
        return
    credentialed = sorted(b for b in LLM_BACKENDS if LLM_BACKEND_TOKENS.get(b))
    if not credentialed:
        return
    if os.environ.get("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", "").strip().lower() in ("1", "true", "yes", "on"):
        # Auth-off + live provider key: warn at startup and keep a /health config flag for the process lifetime (SEC-A5-02).
        log.warning(
            "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS is set — starting UNAUTHENTICATED "
            "with a live provider key attached to %d backend(s): %s. Any caller "
            "that can reach this gateway can sign a request with that key. This is "
            "the deliberate override documented in shared-memory/.env.example, not "
            "a default — also visible on GET /health as "
            "config.allow_unauthenticated_provider_keys once the gateway is up.",
            len(credentialed), ", ".join(scrub_url_credentials(b) for b in credentialed),
        )
        return
    raise SystemExit(
        "FATAL: AGENT_TOKENS is unset but a provider-credentialed backend is "
        f"configured ({', '.join(scrub_url_credentials(b) for b in credentialed)}) — starting would let any "
        "caller sign a request with that key. Configure AGENT_TOKENS, or set "
        "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1 to run anyway (see "
        "shared-memory/.env.example)."
    )


def require_valid_llm_routing_config() -> None:
    """Validate model routing configuration at startup. Raises on invalid roles
    and warns when credentialed backends lack roles or explicit private_ok
    (decision:1824 — backend configuration must be declared or it does not fire:
    private_ok is default-deny, so an undeclared backend serves nothing;
    decision:1785 — the telemetry contract: /health answers "can I use it and what
    to expect", /memory/telemetry carries the numbers, logs carry the events), or
    when unauthenticated backends set private_ok to false."""
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
        # Say here that the credential is still probed. An operator who never runs check_config.py only sees the startup log.
        log.warning(
            "credentialed LLM backend(s) configured with neither `roles` nor "
            "an explicit `private_ok` — configured, but will never be "
            "selected under default-deny (declare `roles` or `private_ok` "
            "explicitly): %s. Its credential is still sent on every /health "
            "probe cycle even though it can serve nothing — remove the "
            "entry if you did not mean to attach the key. See "
            "shared-memory/.env.example / check_config.py.",
            ", ".join(scrub_url_credentials(b) for b in needs_explicit_choice),
        )

    if AUTH_CONFIGURED_AT_STARTUP:
        return
    private_false_explicit = sorted(
        b for b in LLM_BACKENDS
        if LLM_BACKEND_PRIVATE_OK_EXPLICIT.get(b, False)
        and not LLM_BACKEND_PRIVATE_OK.get(b, False)
    )
    if not private_false_explicit:
        return
    # Only the roles-absent subset is safe by construction: no roles and private_ok false serves nothing.
    # A roles list does not consult private_ok, so that subset must not use the same wording.
    roles_absent = sorted(b for b in private_false_explicit if LLM_BACKEND_ROLES.get(b) is None)
    roles_carrying = sorted(b for b in private_false_explicit if LLM_BACKEND_ROLES.get(b) is not None)
    if roles_absent:
        log.warning(
            "AGENT_TOKENS is unset (auth off) but private_ok=false backend(s) "
            "with NO `roles` are EXPLICITLY configured (%s) — without caller "
            "identities the privacy/steering invariants (I-1/I-6) have "
            "nothing to enforce against, but this subset really is safe by "
            "construction: no roles plus private_ok=false serves nothing at "
            "all. Configure AGENT_TOKENS if that scoping was meant to matter.",
            ", ".join(scrub_url_credentials(b) for b in roles_absent),
        )
    if roles_carrying:
        log.warning(
            "AGENT_TOKENS is unset (auth off) and role-scoped backend(s) also "
            "carry an EXPLICITLY configured private_ok=false (%s) — this is "
            "NOT safe by construction: those roles still serve EVERY caller, "
            "and with auth off the gateway cannot tell callers apart. Set "
            "AGENT_TOKENS if that scoping was meant to be enforceable.",
            ", ".join(scrub_url_credentials(b) for b in roles_carrying),
        )


def warn_if_dream_slots_impossible() -> None:
    """Warn at startup if no backend contributes to /pool/status free_slots, which
    would prevent REM and NREM dream daemons from ever running (decision:1357 — the
    routing ruling: counting only serves-all backends toward free_slots silently
    zeroed the dream cycle, plus six required fixes)."""
    if any(_counts_free_slot(b) for b in LLM_BACKENDS):
        return
    log.warning(
        "NO configured backend counts toward /pool/status free_slots (each "
        "either declares a partial `roles` list or is private_ok=false with "
        "no full roles list). The dream daemons (REM/NREM) "
        "gate on free_slots and will NEVER run against this fleet. Fix: give "
        "at least one backend no `roles` field (with private_ok), or an "
        "explicit roles list covering all of %s.", sorted(ROUTING_ROLE_NAMES))


# fact:1335: do not route this write through AsyncLineWriter. Its shutdown flush can hang and must not stall the gateway drain.
# Read the path here; there is nothing else to share with that writer.
GATEWAY_AUDIT_LOG_PATH = os.environ.get("GATEWAY_AUDIT_LOG_PATH", "").strip()
# fact:1338: unmeasured interval. 0 disables the periodic emission; the shutdown emission still runs.
TOKEN_LIFECYCLE_SUM_INTERVAL_S = float(os.environ.get("TOKEN_LIFECYCLE_SUM_INTERVAL_S", "0") or "0")


def _emit_token_lifecycle_sums(reason: str) -> None:
    """Write lifecycle token totals per backend directly to the journal and audit
    JSONL, bypassing AsyncLineWriter (fact:1335 — v0.9.9 shipped the method-and-path
    allowlist on the credentialed proxy path; its open item is that AsyncLineWriter's
    shutdown flush can hang). Best-effort write that never breaks shutdown or loops."""
    ts = datetime.now(timezone.utc).isoformat()
    for b in LLM_BACKENDS:
        p = _llm_tokens_prompt_total.get(b, 0)
        c = _llm_tokens_completion_total.get(b, 0)
        if p == 0 and c == 0:
            continue
        # Scrub the URL for both the journal line and the audit JSONL. A clean query-less URL is unchanged.
        _b_scrubbed = scrub_url_credentials(b)
        log.info(
            "llm-token-lifecycle-sum backend=%s reason=%s tokens_prompt_total=%d "
            "tokens_completion_total=%d", _b_scrubbed, reason, p, c)
        if GATEWAY_AUDIT_LOG_PATH:
            try:
                append_secure(GATEWAY_AUDIT_LOG_PATH, json.dumps({
                    "ts": ts, "kind": "llm_token_lifecycle_sum", "reason": reason,
                    "backend": _b_scrubbed, "tokens_prompt_total": p, "tokens_completion_total": c,
                }))
            except Exception as exc:
                log.warning("token lifecycle sum audit write failed: %s", exc)


async def _token_lifecycle_sum_daemon(stop_event: asyncio.Event) -> None:
    """Periodically emit token lifecycle sums on TOKEN_LIFECYCLE_SUM_INTERVAL_S;
    exits immediately if interval is <= 0."""
    if TOKEN_LIFECYCLE_SUM_INTERVAL_S <= 0:
        return
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TOKEN_LIFECYCLE_SUM_INTERVAL_S)
            break   # stop_event fired — the drain sequence's own shutdown emission covers this
        except asyncio.TimeoutError:
            _emit_token_lifecycle_sums("periodic")


def _default_uds_path() -> str:
    """Return default per-user runtime socket path under XDG_RUNTIME_DIR or /tmp
    (overridden by GATEWAY_UDS_PATH / GATEWAY_UDS_MODE)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, "shared-memory-gw.sock")


def _encoder_routing_log_line() -> str:
    """Format the startup encoder routing announcement with scrubbed EMBEDDER_URL
    and RERANKER_URL endpoints for unit test assertion."""
    return (
        f"### /v1/embeddings->{scrub_url_credentials(EMBEDDER_URL)} | "
        f"/v1/reranking->{scrub_url_credentials(RERANKER_URL)} | default->LLM pool"
    )


def _resolve_proxy_bind_host() -> str:
    """Resolve the bind interface from PROXY_BIND, defaulting unset or empty values
    to 127.0.0.1 (loopback); all-interfaces requires explicit PROXY_BIND=0.0.0.0."""
    raw = os.environ.get("PROXY_BIND", "")
    resolved = raw.strip() or "127.0.0.1"
    if "PROXY_BIND" in os.environ and not raw.strip():
        log.warning(
            "PROXY_BIND is set but empty — falling back to 127.0.0.1 "
            "(loopback), never all-interfaces. Set PROXY_BIND=0.0.0.0 "
            "explicitly to opt into all-interfaces binding.")
    return resolved


async def _drain_watchdogs_and_daemons(
        watchdog_task: "asyncio.Task", rem_watchdog_task: "asyncio.Task",
        other_tasks: "tuple[asyncio.Task, ...]" = ()) -> None:
    """Terminate both daemon processes, then cancel watchdogs and other_tasks, then revoke tokens as an idempotent backstop (order is load-bearing)."""
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
    for t in other_tasks:
        t.cancel()
    for task in (watchdog_task, rem_watchdog_task, *other_tasks):
        try:
            await task
        except asyncio.CancelledError:
            pass
    _revoke_daemon_token(_CONSOLIDATION_AGENT_NAME)
    _revoke_daemon_token(_REM_DAEMON_AGENT_NAME)


async def main() -> None:
    # A plaintext AGENT_TOKENS entry refuses startup, before anything else stands up. This is the real entrypoint, so the check lives here.
    require_no_plaintext_agent_tokens()
    # A malformed LLM_BACKENDS_JSON refuses startup here, not at import. check_config.py deliberately does not make this call.
    require_llm_backends_json_parses("hive_mind_proxy")
    # A backend URL with a credential in userinfo refuses startup. The check lives here, with the other startup refusals.
    require_no_backend_url_credentials()
    # decision:1303: auth off plus a live provider key refuses startup. The check lives here, with the other startup refusals.
    require_auth_when_provider_keys_configured()
    # decision:1824: an unknown role still refuses startup. A credentialed backend with neither roles nor an explicit private_ok, and auth-off with private_ok explicitly false, are warnings, not refusals.
    require_valid_llm_routing_config()
    warn_if_dream_slots_impossible()

    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8888

    proxy = AsyncHiveMindProxy()
    await proxy.start_session()

    coordinator = MemoryCoordinator()
    await coordinator.start()

    # The 50 MB ceiling applies to bodies read via request.read(). The streaming path uses request.content and bypasses it.
    app = web.Application(client_max_size=50 * 1024 * 1024, middlewares=[auth_middleware])
    app["proxy"] = proxy  # shared with health handler
    app["coordinator"] = coordinator  # health reads the cached consolidation snapshot
    # Telemetry for this module's state is a callback, not an import. coordinator cannot import this module. Set it before any request is served.
    coordinator.telemetry_extras_provider = telemetry_extras
    # One Server identity on every response, including proxied ones. Registered on the app so a new handler cannot forget it.
    app.on_response_prepare.append(_set_server_header)

    # Coordinator routes and health endpoint before the catch-all proxy route.
    attach_coordinator(app, coordinator)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/pool/status", handle_pool_status)
    # Register encoder routes before set_known_routes; never bind them to handle_proxy/_route_guard (A1 405s every known key).
    app.router.add_post("/v1/embeddings", proxy.handle_encoder)
    app.router.add_post("/v1/reranking", proxy.handle_encoder)
    # fact:1535: snapshot known routes after the real routes and before the catch-all. The catch-all must not become a known route.
    proxy.set_known_routes(app.router)
    # Every unprotected path must be the canonical of a static route. A dynamic canonical would exempt a whole pattern family. Assert that in this same window, before the catch-all.
    require_unprotected_paths_are_plain_routes(app.router)
    app.router.add_route("*", "/{tail:.*}", proxy.handle_proxy)

    runner = web.AppRunner(app)
    await runner.setup()
    # Localhost unless PROXY_BIND says otherwise. All-interfaces is only safe on an encrypted overlay or behind TLS; bearer tokens are plaintext on HTTP.
    bind_host = _resolve_proxy_bind_host()
    site = web.TCPSite(runner, bind_host, PORT)
    await site.start()

    # Unix socket so local and SSH-forwarded clients present the operator's OS account via SO_PEERCRED. TCP stays up and carries no principal. GATEWAY_UDS_PATH empty disables this.
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
    log.info(_encoder_routing_log_line())

    stop_event = asyncio.Event()
    watchdog_task     = asyncio.create_task(_watchdog_daemon(stop_event))
    rem_watchdog_task = asyncio.create_task(_watchdog_rem_daemon(stop_event))
    # Capability probe: whether the backends can serve, not merely whether they answer /health.
    llm_probe_task    = asyncio.create_task(_llm_probe_daemon(proxy, stop_event))
    capability_task   = asyncio.create_task(
        _capability_probe_daemon(proxy, stop_event, coordinator))
    # Periodic token-count sums. A no-op unless the interval is set; shutdown still emits once in the drain below.
    token_lifecycle_task = asyncio.create_task(
        _token_lifecycle_sum_daemon(stop_event))
    loop = asyncio.get_running_loop()

    def _on_shutdown_signal():
        log.info("Termination signal received — initiating drain sequence...")
        stop_event.set()
        # Drop the handlers immediately so a second Ctrl+C is a hard abort if the drain is stuck on a hung backend.
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(s)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_shutdown_signal)

    await stop_event.wait()

    # Drain: stop listener, finish in-flight, terminate daemons, then cancel watchdogs (revoke after terminate so the token cannot race a live daemon).
    log.info("Stopping listener...")
    await site.stop()
    if uds_site is not None:
        await uds_site.stop()
    log.info("Draining in-flight requests...")
    await runner.cleanup()
    # S7 probe uses the proxy session, so cancel it before session close.
    await _drain_watchdogs_and_daemons(
        watchdog_task, rem_watchdog_task,
        (capability_task, token_lifecycle_task, llm_probe_task))
    # Shutdown sum is a direct synchronous write, so it runs here rather than inside proxy.cleanup or coordinator.stop.
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
