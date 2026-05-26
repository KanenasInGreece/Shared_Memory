import asyncio
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
    env_path = Path(__file__).parent.parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_env()

from coordinator import MemoryCoordinator, attach as attach_coordinator

# Unified Hive-Mind Async Proxy v6
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
# Routing
# --------------------------------------------------------------------------- #
ROUTING_MAP = {
    "/v1/embeddings": "http://localhost:8070",
    "/v1/reranking":  "http://localhost:8071",
}
DEFAULT_TARGET = "http://localhost:5000"

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
        # Route on path only (exact prefix); forward rel_url to preserve query string.
        target_base = DEFAULT_TARGET
        for prefix, target in ROUTING_MAP.items():
            if request.path.startswith(prefix):
                target_base = target
                break

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
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": f"Backend unreachable: {ce}"}, status=503)

        except asyncio.TimeoutError:
            # Connect timeout to upstream — correct status is 504, not 500.
            log.warning("Upstream connect timeout: %s", target_url)
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": "Upstream connect timeout"}, status=504)

        except Exception as e:
            log.error("Unexpected proxy error for %s: %s", target_url, e, exc_info=True)
            if proxy_resp and proxy_resp.prepared:
                return proxy_resp
            return web.json_response({"error": f"Proxy error: {e}"}, status=500)


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
    )
    log.info("Consolidation daemon started (pid %d)", proc.pid)
    return proc


async def _monitor_daemon(proc: "asyncio.subprocess.Process") -> None:
    await proc.wait()
    # -SIGTERM means we terminated it cleanly during shutdown
    if proc.returncode not in (0, -signal.SIGTERM):
        log.warning("Consolidation daemon exited unexpectedly (code %d)", proc.returncode)
    else:
        log.info("Consolidation daemon stopped (code %d).", proc.returncode)


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #
async def main() -> None:
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8888

    proxy = AsyncHiveMindProxy()
    await proxy.start_session()

    coordinator = MemoryCoordinator()
    await coordinator.start()

    # 50 MB ceiling applies to requests buffered via request.read().
    # The streaming path (request.content) bypasses this — see handle_proxy.
    app = web.Application(client_max_size=50 * 1024 * 1024)

    # Coordinator routes must be registered before the catch-all proxy route.
    attach_coordinator(app, coordinator)

    app.router.add_route("*", "/{tail:.*}", proxy.handle_proxy)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info("### Hive-Mind Proxy on :%d [aiohttp]", PORT)
    log.info("### /v1/embeddings->8070 | /v1/reranking->8071 | default->5000")

    daemon_proc = await _start_daemon()
    monitor_task = asyncio.create_task(_monitor_daemon(daemon_proc)) if daemon_proc else None

    stop_event = asyncio.Event()
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
    # 3. proxy.cleanup()  — close the upstream connection pool last
    log.info("Stopping listener...")
    await site.stop()
    log.info("Draining in-flight requests...")
    await runner.cleanup()
    if daemon_proc:
        log.info("Stopping consolidation daemon (pid %d)...", daemon_proc.pid)
        daemon_proc.terminate()
        try:
            await asyncio.wait_for(daemon_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("Daemon did not exit in 5 s — sending SIGKILL")
            daemon_proc.kill()
    if monitor_task:
        monitor_task.cancel()
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
