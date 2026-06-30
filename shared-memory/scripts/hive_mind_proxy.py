import asyncio
import time
import logging
import os
import shutil
import signal
import sys
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector
from aiohttp.client_exceptions import (
    ClientError,
    ServerDisconnectedError,
)

# Load .env BEFORE importing coordinator — coordinator reads env vars at module
# level, so credentials must be in os.environ by the time that import runs.
def _load_env() -> None:
    # Framework env now lives in the framework folder (shared-memory/.env);
    # the repo-root path is kept as a fallback so pre-0.6 installs don't break.
    # __file__ = shared-memory/scripts/hive_mind_proxy.py → parent.parent = shared-memory/
    here = Path(__file__).resolve()
    candidates = [here.parent.parent / ".env", here.parent.parent.parent / ".env"]
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_env()

from coordinator import (
    MemoryCoordinator,
    attach as attach_coordinator,
    auth_middleware,
    backup_quiesce_active,
    _AGENT_TOKENS,
    AUTH_SCHEME,
    FRAMEWORK_VERSION,
    API_VERSION,
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
ROUTING_MAP = {
    "/v1/embeddings": "http://localhost:8070",
    "/v1/reranking":  "http://localhost:8071",
}
DEFAULT_TARGET = "http://localhost:5000"   # primary reasoning LLM (main)

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
def _parse_backend(entry: str) -> tuple[str, float]:
    url, _, w = entry.strip().partition("@")
    try:
        weight = float(w) if w else 1.0
    except ValueError:
        weight = 1.0
    return url.rstrip("/"), max(weight, 0.1)


_raw_backends = [_parse_backend(e) for e in os.environ.get("LLM_BACKENDS", "").split(",") if e.strip()]
if not _raw_backends:
    _raw_backends = [(DEFAULT_TARGET, 1.0)]
LLM_BACKENDS: list[str] = [u for u, _ in _raw_backends]
LLM_WEIGHTS: dict[str, float] = {u: w for u, w in _raw_backends}

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
_llm_unhealthy_until: dict[str, float] = {b: 0.0 for b in LLM_BACKENDS}
_llm_fail_times: dict[str, list] = {b: [] for b in LLM_BACKENDS}
# Runtime reservation (gateway-controlled, NEVER env/user). A backend in this set
# is held OUT of the general parallelise pool so a quality task can use it
# exclusively, then released — e.g. the periodical golden-set recheck (v0.6.1)
# reserves a card, runs its eval on it, releases it: no restart, no degradation,
# the rest of the pool keeps serving REM/NREM. Control endpoint + consumer land
# with the v0.6.1 quality work; the state + pool-exclusion seam is here now.
_llm_reserved: set[str] = set()


def _llm_mark_fail(backend: str) -> None:
    """Record a backend failure; trip the cooldown if it fails too often."""
    now = time.monotonic()
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


def _select_llm_backend(role: str = "") -> str:
    """The single best backend (weighted least-in-flight). Clients never choose."""
    return _ordered_llm_backends(role)[0]

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

    def _filter_headers(self, headers) -> dict:
        """Strip hop-by-hop and Host headers.
        Applied identically to both request (→ upstream) and response (→ client)."""
        return {
            k: v for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        # Route on path: embeddings/reranking have fixed targets; everything else
        # is a reasoning-LLM request, dispatched through the backend POOL so the
        # gateway owns parallelisation. The optional X-SM-LLM-Role header is set
        # ONLY by framework components (e.g. the v0.6.1 judge) — never by clients.
        llm_backend: str | None = None
        target_base: str | None = None
        for prefix, target in ROUTING_MAP.items():
            if request.path.startswith(prefix):
                target_base = target
                break
        if target_base is None:
            role = request.headers.get("X-SM-LLM-Role", "").strip().lower()
            llm_backend = _select_llm_backend(role)
            _llm_inflight[llm_backend] = _llm_inflight.get(llm_backend, 0) + 1
            target_base = llm_backend

        target_url = f"{target_base}{request.rel_url}"
        log.debug("→ %s %s", request.method, target_url)

        upstream_headers = self._filter_headers(request.headers)

        # Stream the request body directly to the upstream without buffering it
        # into a single byte array first. This keeps memory footprint flat even
        # for large GraphRAG ingestion payloads.
        # NOTE: this bypasses the client_max_size check that request.read() would
        # enforce. Acceptable for this trusted localhost deployment; revisit if the
        # proxy is ever exposed to untrusted clients.
        upstream_data = request.content if request.can_read_body else None

        # Initialized to None so exception handlers can check object state directly
        # (.prepared attribute) rather than relying on a parallel boolean flag.
        proxy_resp: web.StreamResponse | None = None

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
                    headers=self._filter_headers(upstream.headers),
                )
                await proxy_resp.prepare(request)

                # write_eof() lives inside the same try as the chunk loop so that
                # an EOF-time disconnect is handled by the same except clauses.
                try:
                    async for chunk in upstream.content.iter_any():
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

                if llm_backend is not None:
                    _llm_mark_ok(llm_backend)   # connected + served — clear fail streak
                return proxy_resp

        except asyncio.CancelledError:
            # CancelledError is BaseException (Python 3.8+) and won't be caught by
            # `except Exception` below, but this explicit clause documents that we
            # never absorb cancellation at any level.
            raise

        except ClientError as ce:
            # Upstream is down, unreachable, or refused the connection.
            # 503: the proxy is fine; the backend is not.
            log.error("Upstream unreachable %s: %s", target_url, ce)
            if llm_backend is not None:
                _llm_mark_fail(llm_backend)
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": f"Backend unreachable: {ce}"}, status=503)

        except asyncio.TimeoutError:
            # Connect timeout to upstream — correct status is 504, not 500.
            log.warning("Upstream connect timeout: %s", target_url)
            if llm_backend is not None:
                _llm_mark_fail(llm_backend)
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": "Upstream connect timeout"}, status=504)

        except Exception as e:
            log.error("Unexpected proxy error for %s: %s", target_url, e, exc_info=True)
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": f"Proxy error: {e}"}, status=500)

        finally:
            # Release the in-flight slot so least-busy selection stays accurate,
            # whatever the outcome (success, disconnect, error, cancellation).
            if llm_backend is not None:
                _llm_inflight[llm_backend] = max(0, _llm_inflight.get(llm_backend, 0) - 1)


# --------------------------------------------------------------------------- #
# Daemon token helpers
# --------------------------------------------------------------------------- #

def _daemon_env(agent_name: str) -> dict:
    """Build a subprocess environment that includes AGENT_TOKEN for the named daemon.

    The daemon uses this token to authenticate its outbound calls through the
    proxy (embeddings, LLM).  The token identifies the daemon as a trusted
    internal caller — it does NOT change the source attribution of the facts
    it enriches.  Fact.source always reflects the original saving agent.

    If no token is registered for the daemon, AGENT_TOKEN is omitted and the
    daemon's calls will be rejected 401 when auth is active — this surfaces
    a misconfiguration rather than silently bypassing auth.
    """
    env = os.environ.copy()
    for pair in env.get("AGENT_TOKENS", "").split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        name, token = pair.split(":", 1)
        if name.strip() == agent_name:
            env["AGENT_TOKEN"] = token.strip()
            break
    else:
        log.warning(
            "No token registered for daemon %r — add %s:<token> to AGENT_TOKENS",
            agent_name, agent_name,
        )
    return env


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
    proc = await asyncio.create_subprocess_exec(
        uv, "run",
        "--with", "httpx",
        "--with", "psycopg2-binary",
        "--with", "neo4j",
        "python", str(daemon_path),
        env=_daemon_env("consolidation"),
    )
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
    proc = await asyncio.create_subprocess_exec(
        uv, "run",
        "--with", "httpx",
        "--with", "psycopg2-binary",
        "--with", "neo4j",
        "python", str(rem_path),
        env=_daemon_env("rem_daemon"),
    )
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
# Health endpoint
# --------------------------------------------------------------------------- #
async def handle_health(request: web.Request) -> web.Response:
    """GET /health — probe all upstream backends and report daemon liveness.

    HTTP 200: embedder + reranker both reachable (save/search path is healthy).
    HTTP 503: at least one critical backend is down.

    LLM (:5000) is non-critical for saves and searches — its status is reported
    but does not affect the overall HTTP status code (it only affects consolidation).
    """
    proxy: AsyncHiveMindProxy = request.app["proxy"]
    checks: dict[str, str] = {}

    # The embedder and reranker are llama.cpp containers that expose /health.
    # The reasoning LLM (:5000) is "LM Studio or any OpenAI-compatible endpoint";
    # those do NOT standardise /health — LM Studio logs an error for the unknown
    # route on every probe. Use /v1/models, which every OpenAI-compatible server
    # (LM Studio included) serves, as the LLM liveness check instead.
    for name, url in [
        ("embedder", "http://localhost:8070/health"),
        ("reranker",  "http://localhost:8071/health"),
    ]:
        try:
            timeout = ClientTimeout(total=2.0)
            async with proxy.session.get(url, timeout=timeout) as r:
                checks[name] = "ok" if r.status < 400 else f"http_{r.status}"
        except asyncio.TimeoutError:
            checks[name] = "timeout"
        except Exception:
            checks[name] = "down"

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
    if len(LLM_BACKENDS) > 1:
        checks["llm_backends"] = backend_status
        if _llm_reserved:
            checks["llm_reserved"] = sorted(_llm_reserved)

    checks["daemon"]     = "running" if _daemon_healthy else "stopped"
    checks["rem_daemon"] = "running" if _rem_healthy    else "stopped"

    # Version contract — clients compare api_version against their own to detect
    # skew. Cheap string fields; no backend probe. version is informational only.
    checks["version"]     = FRAMEWORK_VERSION
    checks["api_version"] = API_VERSION

    # Embedder and reranker are the critical path — every save and search
    # depends on them.  LLM and daemon degradation is reported but does not
    # fail the health check so agents can still read/write memory.
    critical_ok = checks["embedder"] == "ok" and checks["reranker"] == "ok"
    checks["status"] = "ok" if critical_ok else "degraded"
    checks["auth_required"] = bool(_AGENT_TOKENS)
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
    coordinator = request.app.get("coordinator")
    if coordinator is not None:
        try:
            consolidation = coordinator.consolidation_health()
            checks["consolidation"] = consolidation
            # Top-level inference/GPU-busy signal for the monitor's LLM tile.
            # Tri-state ("busy"|"idle"|"unknown") from the cached snapshot the
            # coordinator probes in the background — /health never shells out to
            # nvtop. "unknown" (nvtop absent / SLOT_AWARE off) is reported verbatim
            # so the monitor shows "unknown", never a false "idle". Distinct from
            # checks["llm"], which stays a pure reachability probe of :5000.
            checks["inference_busy"] = consolidation.get("inference_busy", "unknown")
        except Exception:
            checks["consolidation"] = {"fresh": False}
            checks["inference_busy"] = "unknown"

    return web.json_response(checks, status=200 if critical_ok else 503)


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #
def _default_uds_path() -> str:
    """Per-user runtime socket by default (0700 dir → only this user reaches it,
    which is exactly right for a single-user box). For a multi-user gateway set
    GATEWAY_UDS_PATH to a shared location and widen GATEWAY_UDS_MODE."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, "shared-memory-gw.sock")


async def main() -> None:
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
    for task in (watchdog_task, rem_watchdog_task):
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
