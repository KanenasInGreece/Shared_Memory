# Hive-Mind Proxy: Implementation History and Decision Log

**Final artefact:** `proxy_v6.py`  
**Purpose:** Async reverse proxy routing LLM and embedding traffic to three local backends within the Cloe/OpenClaw shared-memory architecture.

| Port | Service | Route prefix |
|------|---------|--------------|
| 8070 | BGE-M3 (embeddings) | `/v1/embeddings` |
| 8071 | BGE-Reranker-v2-m3 | `/v1/reranking` |
| 5000 | LM Studio / llama-server (LLM generation, GraphRAG) | everything else |

This document records every change made across all versions — what was wrong, what was changed, why that specific change was the correct one, and when an alternative fix was proposed but rejected. Every decision present in v6 has a traceable entry here.

---

## Baseline: `proxy_threaded.py`

The working starting point. Pure stdlib: `http.server.ThreadingHTTPServer` + `urllib.request`. One thread per connection, handles only `POST`, no streaming — the response is fully buffered in `urllib` before being written back.

### What it does correctly
- Only strips `Host` from forwarded headers (basic proxy hygiene)
- Returns a JSON error body on exception
- `ThreadingHTTPServer` provides real concurrency for multiple simultaneous requests

### Structural limitations (not bugs — design ceiling)
- **One thread per connection.** Under concurrent embedding batches + simultaneous LLM generation, threads contend for the GIL and for OS scheduler time. This was the stated motivation for moving to async.
- **`urllib` buffers the entire response before writing.** A 4,000-token streamed generation is held in RAM until complete, then sent as one write. Clients see no tokens until the model finishes.
- **Only handles `POST`.** GET requests (health checks, model listing) are silently dropped.
- **Substring route matching.** `if "/embeddings" in self.path` — a path like `/v1/embeddings_bulk` or `/internal/v1/embeddings` would route to port 8070 incorrectly.
- **No structured logging.** Debug prints are commented out; errors are silently discarded in production.

---

## `proxy_v2.py` — First Async Attempt

Rewrote the proxy using `asyncio` + `aiohttp`. The intent was correct; the execution introduced eight new bugs, most of which would cause silent failures or crashes in production.

### Bug 1 — Hard crash on startup: `NameError: aiohttp`

**Code:**
```python
from aiohttp import web, ClientSession, ClientTimeout
# ...
connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
```

**Problem:** `aiohttp` was imported by pulling three names from it with `from aiohttp import ...`. The module itself was never bound to the name `aiohttp`. The first call to `start_session()` raised `NameError: name 'aiohttp' is not defined` and the proxy crashed before handling a single request.

**Fix:** Add `TCPConnector` to the `from aiohttp import` line.

---

### Bug 2 — Query string silently dropped

**Code:**
```python
path = request.path
target_url = f"{target_base}{path}"
```

**Problem:** In aiohttp, `request.path` returns only the path component with no query string. Any call containing `?model=qwen3`, `?stream=true`, or similar parameters was forwarded to the upstream with those parameters stripped. LM Studio uses query parameters for model selection; this silently broke model routing.

**Fix:** Use `request.rel_url`, which returns path + query string together (e.g. `/v1/chat/completions?stream=true`).

---

### Bug 3 — Substring route matching

**Code:**
```python
if route in path:
```

**Problem:** `"/v1/embeddings" in "/v1/embeddings_bulk"` evaluates to `True`. Any path containing the route string as a substring would be misrouted, regardless of whether the match was at a path boundary.

**Fix:** `request.path.startswith(prefix)` — matches only if the path begins with the route prefix.

---

### Bug 4 — Hop-by-hop headers forwarded verbatim; client hangs or truncates

**Code:**
```python
response_headers = dict(upstream_response.headers)
proxy_response = web.StreamResponse(status=..., headers=response_headers)
```

**Problem:** All upstream response headers were forwarded including `Transfer-Encoding`, `Connection`, `Keep-Alive`, and critically `Content-Length`. When a response is streamed in chunks, the `Content-Length` value describes the total uncompressed byte count — a number the client receives before any bytes arrive. The client then waits for exactly that many bytes via the chunked stream, which delivers them in a different framing. Result: clients hang indefinitely waiting for a byte count that will never match, or truncate early when they think they have received the declared length.

The same problem existed on the request side: only `Host` was stripped; all other hop-by-hop headers were forwarded to the upstream.

**RFC reference:** RFC 7230 §6.1 defines hop-by-hop headers as connection-specific and explicitly prohibits proxies from forwarding them.

**Fix:** Define a `HOP_BY_HOP` constant (`frozenset` for immutability and faster membership test) containing: `connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailers`, `transfer-encoding`, `upgrade`, `content-length`. Apply this filter to both request headers (outgoing to upstream) and response headers (returning to client) via a single `_filter_headers()` method.

---

### Bug 5 — `prepare()` + error handler produces a second response on an already-committed stream

**Code:**
```python
await proxy_response.prepare(request)
async for chunk in upstream_response.content.iter_any():
    await proxy_response.write(chunk)
await proxy_response.write_eof()
return proxy_response

except Exception as e:
    return web.json_response({"error": ...}, status=500)
```

**Problem:** Once `prepare()` is called, response headers are on the wire. If the client disconnects mid-stream, `write(chunk)` raises `ConnectionResetError`, which falls into the `except Exception` handler. That handler then attempts to return a new `web.json_response` — a second response object — on a connection that already has headers sent. This produces an aiohttp internal error or a silent hang depending on version.

**Fix:** Track whether `prepare()` has been called. In exception handlers, check this state before attempting to send a JSON error: if headers are already on the wire, return the existing stream response object; if not, return the JSON error. This was initially implemented as a `prepared = True` boolean flag, later refined to `proxy_resp = None` initialization with `proxy_resp.prepared` attribute check (see v4→v5).

---

### Bug 6 — `KeyboardInterrupt` never fires inside `asyncio.run()`

**Code:**
```python
try:
    await site.start()
    while True:
        await asyncio.sleep(3600)
except KeyboardInterrupt:
    pass
finally:
    await runner.cleanup()
    await proxy.cleanup()
```

**Problem:** `asyncio.run()` wraps the entire coroutine. When `Ctrl+C` is pressed, the OS delivers `SIGINT`. Inside a running event loop, `asyncio.run()` converts this to `asyncio.CancelledError` on the current task — not `KeyboardInterrupt`. The `except KeyboardInterrupt` block inside the coroutine never executes. The `finally` clause's execution was therefore not guaranteed across Python versions.

**Fix:** Use `asyncio.Event` as the shutdown gate and register OS signal handlers on the event loop directly with `loop.add_signal_handler(signal.SIGINT, ...)` and `loop.add_signal_handler(signal.SIGTERM, ...)`. The coroutine blocks on `await stop_event.wait()`. When a signal arrives, the handler sets the event and the coroutine proceeds to the drain sequence deterministically.

---

### Bug 7 — Session closed before server finishes draining in-flight requests

**Code:**
```python
finally:
    await runner.cleanup()   # tears down HTTP server
    await proxy.cleanup()    # closes upstream session
```

**Problem (order):** `runner.cleanup()` tears down the aiohttp web server, which may cancel active request handler tasks that are still mid-flight and still holding references to `self.session`. At that moment the session is still open — the order here is actually correct. The separate problem is the cleanup guard.

**Problem (guard):** `if self.session:` only checks that the session object exists, not whether it is already closed. A closed `ClientSession` object still evaluates as truthy. Calling `await session.close()` on an already-closed session is a no-op in practice but should be guarded explicitly.

**Fix:** Guard with `if self.session and not self.session.closed`.

---

### Bug 8 — `client_max_size=1024**3` is a 1 GiB memory bomb

**Code:**
```python
app = web.Application(client_max_size=1024**3)
```

**Problem:** aiohttp buffers the entire request body in RAM before the handler is called (when using `request.read()`). Setting the ceiling at 1 GiB means a concurrent burst of embedding batches + a GraphRAG ingestion call could allocate several GiB before the event loop has a chance to reject any of them. On a 15 GB RAM VM this is a real OOM risk.

**Fix:** `50 * 1024 * 1024` (50 MB). This is generous for any realistic payload: the largest GraphRAG document batches are a few MB of JSON; embedding request batches are in the tens to hundreds of KB.

**Note (added in v6):** The 50 MB ceiling is enforced when using `request.read()`. When the streaming request body path was introduced in v6 (see §v5→v6, Case 3), this limit is bypassed for the request body. The comment is preserved in v6 to document this explicitly.

---

## `proxy_v3.py` — All Eight Bugs Fixed

v3 resolved all bugs listed above. Key structural decisions made here that survived to v6:

- `from aiohttp import web, ClientSession, ClientTimeout, TCPConnector` — explicit imports
- `HOP_BY_HOP = frozenset({...})` — immutable, fast membership test
- `_filter_headers()` applied to both request and response headers
- `request.rel_url` for upstream URL construction
- `request.path.startswith(prefix)` for routing
- `asyncio.Event` + `loop.add_signal_handler()` for shutdown
- `if self.session and not self.session.closed` guard in cleanup
- `50 * 1024 * 1024` request size ceiling
- `logging.basicConfig(stream=sys.stderr)` — proxy diagnostics on stderr, not stdout, so they do not pollute any stdout pipe in a tmux session alongside llama-server

**TCPConnector parameters introduced in v3:**
```python
TCPConnector(
    limit=200,
    limit_per_host=80,
    ttl_dns_cache=300,
    enable_cleanup_closed=True,
)
```
- `limit=200`: total connection pool ceiling across all upstreams. Provides headroom for concurrent burst (embeddings + reranking + LLM generation simultaneously).
- `limit_per_host=80`: without this, a burst of embedding requests to port 8070 can consume the entire pool and starve port 5000 (LLM backend), causing GraphRAG ingestion to queue behind embedding batches. With `limit_per_host`, each upstream has an independent ceiling.
- `enable_cleanup_closed=True`: evicts half-open sockets from the pool immediately rather than waiting for TTL. Prevents pool exhaustion from stale connections after a backend restart.

**`ClientTimeout(total=None, connect=5.0)`:**
- `connect=5.0`: fail fast if a backend is not running; a 5-second connect timeout surfaces misconfiguration immediately rather than silently queuing.
- `total=None`: never impose a total timeout on a request. A 4,000-token generation at 20 tok/s takes 200 seconds; a hard total timeout would kill it mid-stream.

---

## `proxy_v3_g.py` — Gemini's Parallel Implementation

Gemini's independent implementation of the same fixes. Points where it improved on v3:

**Adopted into v4:**

1. **`ClientError` → HTTP 503 (not 500).** When the upstream backend is unreachable (`ClientError`), the proxy itself is functioning correctly — the backend is not. HTTP 503 (Service Unavailable) is the semantically correct response. 500 (Internal Server Error) implies the fault is in the proxy. This distinction matters for any client that implements retry logic on 503.

2. **Explicit `await site.stop()` before `runner.cleanup()`.** `runner.cleanup()` eventually stops the site internally, but calling `site.stop()` explicitly first closes the listening socket before the drain phase begins. This prevents new connections from arriving while in-flight requests are still draining — a meaningful operational guarantee.

3. **Named shutdown handler function.** A named `_on_shutdown_signal()` function is more traceable in stack dumps and log output than an anonymous lambda.

4. **Outer `try/except KeyboardInterrupt` around `asyncio.run()`.** Belt-and-suspenders for the edge case where `SIGINT` arrives before the event loop is fully running and the signal handler has been registered.

5. **Single `_filter_headers()` method for both directions.** The filtering logic for request headers (→ upstream) and response headers (→ client) is identical. Two separate functions were redundant. One method applied in both directions is DRY.

**Not adopted (v3 was correct):**

- `'proxy_response' in locals()` — rejected. `locals()` is a CPython implementation detail that returns a snapshot of the current scope. The check does not prevent `NameError` if the variable was never bound; it just obscures the intent. A boolean `prepared` flag (later refined to `proxy_resp = None` with `.prepared` attribute check) is explicit and portable.
- `limit=100, limit_per_host=20` — rejected. See v3 reasoning above. `limit_per_host=20` with three upstreams leaves 60 connections max in use (20×3) out of 100 total. This is sufficient in steady state but creates a bottleneck during embedding bursts where 20 concurrent requests to BGE-M3 is a real ceiling.
- Missing `asyncio.CancelledError` in disconnect catch — v3_g only caught `(ConnectionResetError, IOError)`. See below.
- Missing `allow_redirects=False` — a proxy must not silently follow upstream redirects. If llama-server returns a 302, that redirect belongs to the client to decide on, not the proxy.
- Missing `enable_cleanup_closed=True` — see v3 reasoning.
- f-strings in logger calls — `logger.error(f"... {var}")` formats the string unconditionally even when the log level would suppress the line. `logger.error("... %s", var)` defers formatting. This matters under high-frequency embedding log lines.

---

## `proxy_v4.py` — Merge of v3 and v3_g

v4 combined the correct elements from both v3 and v3_g. No new bugs were introduced. See v3→v3_g above for the full adopted/rejected table.

---

## `proxy_v4_g.py` — Gemini's Second Iteration

One critical correctness fix, plus two refinements.

**Adopted into v5:**

### Critical fix: `CancelledError` must be re-raised, never swallowed

**v4 code (wrong):**
```python
except (ConnectionResetError, IOError, asyncio.CancelledError):
    log.warning("Client disconnected mid-stream: %s", target_url)
```

**Problem:** v4 treated `CancelledError` as equivalent to a client disconnect and returned `proxy_resp` silently. This is wrong. `CancelledError` is the event loop signalling task cancellation — it fires during graceful shutdown when `runner.cleanup()` cancels all active handler tasks, or during a framework timeout. Swallowing it means the task appears to have completed normally from the event loop's perspective, but the cancellation was never acknowledged. The event loop waits indefinitely for the task to honour its cancellation. Graceful shutdown stalls.

**Concrete scenario:** SIGTERM received → `stop_event.set()` → `site.stop()` → `runner.cleanup()`. `runner.cleanup()` cancels active handler tasks. A handler mid-stream in `write(chunk)` receives `CancelledError`. v4 catches it alongside `ConnectionResetError`, logs "client disconnected", and returns. The event loop is now waiting for a task that returned without re-raising — `runner.cleanup()` hangs until its internal timeout.

**Fix:** Separate `CancelledError` into its own `except` clause and `raise` unconditionally. Add an outer `except asyncio.CancelledError: raise` as a second layer. In Python 3.8+, `CancelledError` is `BaseException` and will not be caught by `except Exception` — the outer clause is technically redundant but is explicit documentation that cancellation is never absorbed at any level.

**Additional refinement: `proxy_resp = None` replaces `prepared = True` flag**

Both track whether `prepare()` has been called. `proxy_resp = None` with `proxy_resp and proxy_resp.prepared` uses the object's own `.prepared` attribute rather than a parallel state variable. It also handles the edge case where `prepare()` itself raises (connection already closed before streaming begins) — `proxy_resp` exists but `.prepared` is `False`, so the JSON error path is taken correctly.

**Additional refinement: `write_eof()` moved inside the streaming try block**

v4 wrapped `write_eof()` in a separate nested try after the chunk loop. v4_g puts it inside the same try as the chunk loop. The effect is identical — `write_eof()` failures are caught by the same disconnect handlers — but the structure is flatter and easier to read.

**Not adopted:**

- `limit=100, limit_per_host=20` — same reasoning as before; too tight for embedding bursts.
- `asyncio.TimeoutError` missing as named case → falls to `except Exception` → HTTP 500. The correct status for an upstream connect timeout is 504 Gateway Timeout, not 500 Internal Server Error.

---

## `proxy_v5.py` — Merge of v4 and v4_g

v5 incorporated the `CancelledError` re-raise fix from v4_g, the `proxy_resp = None` pattern, and the flattened `write_eof()` placement. `limit=200/limit_per_host=80`, `asyncio.TimeoutError → 504`, and `stream=sys.stderr` were retained from v4.

---

## `proxy_v6.py` — Final Version

v6 was produced in response to a four-case architectural audit. Two cases were accepted (with one requiring a corrected fix mechanism). Two cases were rejected as described. All four are documented here.

---

### Case 1: Upstream mid-stream disconnects — `ClientDisconnectedError` + `ServerDisconnectedError`

**Claim (audit):** "aiohttp wraps client write errors during active streaming inside `ClientDisconnectedError`. If a client disconnects cleanly without resetting the socket at OS level, it triggers this internal framework exception, which completely bypasses `(ConnectionResetError, IOError)`."

**Verdict: Fix accepted. Stated direction is wrong.**

`ClientDisconnectedError` and `ServerDisconnectedError` are defined in `aiohttp.client_exceptions`. They are raised by aiohttp's **HTTP client** (our `self.session`) when the **upstream server** drops the connection — not when the downstream client (OpenClaw) disconnects. The audit had the direction backwards.

The actual path for downstream client disconnect:
- aiohttp cancels the handler task → `CancelledError` (already handled by `raise`)
- OS-level socket reset from client → `ConnectionResetError` or `IOError` (already handled)

The actual path for **upstream** dropping mid-stream:
- `iter_any()` raises `ClientDisconnectedError` (clean close) or `ServerDisconnectedError` (abrupt reset)
- Both are `ClientError` subclasses
- In v5, they bubble to the outer `except ClientError` handler — but since `proxy_resp.prepared` is True at that point, they return `proxy_resp` silently anyway
- The improvement: catching them in the inner streaming block logs them at `WARNING` level instead of `ERROR`, and uses a message that correctly identifies the event as an upstream drop rather than a backend unreachable error

**v6 implementation:**
```python
from aiohttp.client_exceptions import (
    ClientError,
    ClientDisconnectedError,
    ServerDisconnectedError,
)

UPSTREAM_DISCONNECT = (ClientDisconnectedError, ServerDisconnectedError)
```

Kept as a named constant with a comment explaining the direction (upstream, not downstream), because this is the most likely source of future confusion. Separated from `(ConnectionResetError, IOError)` with a distinct log message:

```python
except UPSTREAM_DISCONNECT as e:
    log.warning("Upstream dropped connection mid-stream: %s — %s", target_url, e)

except (ConnectionResetError, IOError) as e:
    log.warning("Client disconnected mid-stream: %s — %s", target_url, e)
```

---

### Case 2: Compression — `auto_decompress=False` replaces audit's `Accept-Encoding` stripping

**Claim (audit):** "Add `accept-encoding` to `HOP_BY_HOP`. This forces upstream backends to answer in raw JSON strings, eliminating situations where downstream parsers choke on chunked gzip frames."

**Verdict: Underlying problem is real. Proposed fix is wrong. Correct fix substituted.**

`Accept-Encoding` is an end-to-end request header by RFC 7230 definition — it describes the client's decompression capabilities and must be forwarded to the upstream. It is not a hop-by-hop header. Adding it to `HOP_BY_HOP` is semantically incorrect even if it achieves the practical goal of preventing compressed responses from localhost backends.

**The actual bug:** aiohttp's `ClientSession` has `auto_decompress=True` by default. When an upstream returns a gzip-compressed response:
1. aiohttp decompresses the body transparently
2. The upstream's `Content-Encoding: gzip` response header is still forwarded to the client
3. The client receives decompressed bytes labelled `Content-Encoding: gzip`
4. The client attempts to decompress already-decompressed data — corruption

**The correct fix:** `auto_decompress=False` on `ClientSession`. This makes the proxy fully transparent: compressed bytes and their `Content-Encoding` header travel together. The client receives what the upstream sent, with accurate headers, and handles decompression itself.

```python
self.session = ClientSession(
    connector=connector,
    timeout=timeout,
    auto_decompress=False,
)
```

For this specific deployment (localhost backends: llama-server, Python embedding servers), compression is not used in practice. The fix is therefore low-stakes but architecturally correct and forward-safe if backends ever change.

---

### Case 3: Streaming request body — `request.content` replaces `request.read()`

**Claim (audit):** "Loading a 30 MB GraphRAG document ingestion payload entirely into a single contiguous byte array creates significant GC spikes. Replace with `data=request.content` for a zero-copy pipeline."

**Verdict: Accepted. One hidden cost documented.**

**v5 code:**
```python
body = await request.read()
```

`request.read()` buffers the entire request body into a single `bytes` object in RAM before the upstream request is made. For a large GraphRAG ingestion payload this creates a spike: allocate the buffer, hold it for the duration of the upstream request, then release it for GC.

**v6 code:**
```python
upstream_data = request.content if request.can_read_body else None
```

`request.content` is an `asyncio.StreamReader`. Passing it as `data` to `self.session.request()` causes aiohttp to stream the request body directly to the upstream as it arrives — no intermediate buffer, flat memory footprint throughout the request.

`request.can_read_body` guards against passing a body for requests that have none (GET, HEAD, DELETE without body). Passing an empty stream is harmless on most servers but avoids sending `Transfer-Encoding: chunked` with zero bytes unnecessarily.

**Hidden cost explicitly documented in v6:**

The `client_max_size=50 * 1024 * 1024` ceiling is enforced inside `request.read()`. Bypassing `read()` means the 50 MB limit is not enforced for the request body. For this deployment (trusted localhost pipeline clients), this is acceptable. The comment appears in two places in v6 so future readers cannot miss it:

```python
# NOTE: this bypasses the client_max_size check that request.read() would
# enforce. Acceptable for this trusted localhost deployment; revisit if the
# proxy is ever exposed to untrusted clients.
```

and on the `web.Application` line:

```python
# 50 MB ceiling applies to requests buffered via request.read().
# The streaming path (request.content) bypasses this — see handle_proxy.
```

---

### Case 4: Self-defusing signal handler for emergency abort

**Claim (audit):** "If the user presses Ctrl+C a second time while in-flight requests are draining, the signal handler fires again, sets an already-set event, and does nothing. The terminal appears frozen until the drain timeout finishes."

**Verdict: Accepted as-is.**

**v5 code:**
```python
def _on_shutdown_signal():
    log.info("Termination signal received — initiating drain sequence...")
    stop_event.set()
```

Once `stop_event` is set, a second `SIGINT` calls the same handler, sets an already-set event, and returns. If the drain is blocked by a hung upstream connection, the operator has no recourse short of `kill -9`.

**v6 code:**
```python
def _on_shutdown_signal():
    log.info("Termination signal received — initiating drain sequence...")
    stop_event.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.remove_signal_handler(s)
```

After the first signal, both handlers are removed. Python falls back to its default `SIGINT` disposition on the second `Ctrl+C`: `KeyboardInterrupt` is raised. `asyncio.run()` catches this, cancels the main task, and exits. The outer `except KeyboardInterrupt` logs it and the process terminates immediately.

The outer handler was changed from `pass` to a log line:
```python
except KeyboardInterrupt:
    log.info("Emergency halt via KeyboardInterrupt.")
```

Emergency aborts should be visible in the terminal output, not silent.

---

## Complete Change Summary

| Change | Introduced | Reason |
|--------|-----------|--------|
| `from aiohttp import TCPConnector` | v3 | v2 `NameError` crash on startup |
| `request.rel_url` for upstream URL | v3 | v2 silently dropped query string (`?model=...`) |
| `request.path.startswith(prefix)` | v3 | v2 substring match misrouted paths containing route as substring |
| `HOP_BY_HOP` filter on both header directions | v3 | v2 forwarded `Content-Length` with chunked stream → client hang/truncation |
| `proxy_resp = None` / `.prepared` guard | v3→v5 | v2 attempted second response on already-committed stream; v3 used `prepared` flag, v5 refined to object attribute check |
| `asyncio.Event` + `loop.add_signal_handler()` | v3 | v2 `except KeyboardInterrupt` never fired inside `asyncio.run()` |
| `not self.session.closed` guard | v3 | v2 `if self.session` truthy even for closed session |
| `client_max_size=50MB` | v3 | v2 1 GiB ceiling was a memory bomb under concurrent load |
| `logging` to `stderr` | v3 | Proxy diagnostics must not pollute stdout in tmux pane with llama-server |
| `enable_cleanup_closed=True` | v3 | Prevents half-open socket pool leaks after backend restart |
| `limit_per_host=80` | v3 | Without this, embedding bursts exhaust the pool and starve LLM backend |
| `limit=200` | v3 | Provides headroom for concurrent burst across three upstreams |
| `ClientTimeout(total=None, connect=5.0)` | v3 | `connect` fails fast on dead backend; `total=None` never kills a live generation |
| `allow_redirects=False` | v3 | Proxy must forward redirects to client, never chase them |
| `ClientError` → HTTP 503 | v4 (from v3_g) | 503 = backend unavailable; 500 = proxy fault. Semantically distinct |
| Explicit `await site.stop()` | v4 (from v3_g) | Closes listen socket before drain; prevents new arrivals during drain phase |
| Single `_filter_headers()` method | v4 (from v3_g) | DRY; filter logic is identical for request and response headers |
| Named `_on_shutdown_signal()` | v4 (from v3_g) | More traceable in logs/stack than lambda |
| Outer `try/except KeyboardInterrupt` | v4 (from v3_g) | Belt-and-suspenders for pre-event-loop signal edge case |
| `asyncio.TimeoutError` → HTTP 504 | v4 | Connect timeout is 504 Gateway Timeout, not 500 Internal Server Error |
| `% style` logger calls | v4 | Defers string formatting; matters under high-frequency embedding log lines |
| `CancelledError` re-raised, never swallowed | v5 (from v4_g) | Swallowing it leaves cancelled tasks zombie; graceful shutdown stalls |
| Outer `except asyncio.CancelledError: raise` | v5 (from v4_g) | Documents intent: cancellation is never absorbed at any level |
| `write_eof()` inside streaming try | v5 (from v4_g) | Flatter structure; EOF-time disconnects handled by same except clauses |
| `UPSTREAM_DISCONNECT` tuple + `ServerDisconnectedError` | v6 | Upstream mid-stream drop logged at WARNING, not ERROR; separated from downstream disconnect |
| `auto_decompress=False` on ClientSession | v6 | aiohttp decompresses by default but forwards `Content-Encoding: gzip` → client double-decompresses |
| `data=request.content` streaming | v6 | Flat memory footprint for large GraphRAG payloads; `request.read()` buffered entire body |
| Self-defusing signal handler | v6 | Second Ctrl+C restores default SIGINT → emergency abort if drain stalls |
| Emergency halt log line | v6 | Hard aborts must be visible in terminal output |

---

## Decisions Explicitly Rejected

These were proposed at various points and turned down with reasoning.

| Rejected change | Proposed at | Reason rejected |
|----------------|-------------|-----------------|
| `'proxy_response' in locals()` as prepared check | v3_g | `locals()` is a CPython implementation detail; does not prevent `NameError` if variable was never bound |
| `limit=100, limit_per_host=20` | v3_g, v4_g | Too tight for embedding burst parallelism; see `limit_per_host` reasoning in v3 section |
| f-strings in logger calls | v3_g | Formats unconditionally regardless of log level |
| `accept-encoding` in `HOP_BY_HOP` | v6 audit | `Accept-Encoding` is an end-to-end header by RFC 7230, not hop-by-hop; stripping it is semantically wrong even if pragmatically effective for localhost |
| `ClientDisconnectedError` as downstream disconnect signal | v6 audit framing | `ClientDisconnectedError` is raised by the aiohttp *client* when the *upstream* drops, not when the downstream client disconnects. The fix was applied with corrected direction |
