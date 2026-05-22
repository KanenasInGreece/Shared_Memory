# Architectural Decision Records (ADR) - Shared Memory Framework

This document tracks the evolution of the Oratotis Shared Memory Framework. Each entry represents a grounded decision made to ensure technical integrity and agentic clarity.

---

## [2026-05-21] ADR-006: Hive-Mind Proxy Auto-Starts Consolidation Daemon

### Status
Accepted

### Context
ADR-005 added a liveness check so save operations warn when the consolidation daemon is not running. But the daemon still required manual startup before any agent session — two separate commands, one easy to forget. Since `hive_mind_proxy.py` is already the mandatory prerequisite for any embedding or save operation, it is the correct place to manage the daemon lifecycle.

### Decision
`hive_mind_proxy.py` launches `consolidation_loop.py` as a managed subprocess immediately after the proxy begins accepting requests. The daemon path is resolved relative to `__file__`, so the proxy works correctly whether run from the project directory or from the installed Gemini skill. Key properties:

- **`asyncio.create_subprocess_exec`** with `uv run --with httpx --with psycopg2-binary --with neo4j python <path>` — same invocation that was previously manual.
- **`_monitor_daemon` task:** awaits the process in a background asyncio task and logs an unexpected exit. `-SIGTERM` (clean shutdown) and `0` are treated as normal; any other code is a warning.
- **Drain-sequence shutdown:** after `runner.cleanup()` (in-flight proxy requests drained), `daemon_proc.terminate()` is called. A 5-second `asyncio.wait_for` gives the daemon time to flush its Postgres listener connection cleanly; SIGKILL is sent only if it stalls.
- **Graceful fallbacks:** if `consolidation_loop.py` is not found next to the proxy script, or `uv` is not in PATH, a `WARNING` is logged and the proxy starts normally without the daemon.

### Consequences
- Single command (`uv run --with aiohttp python hive_mind_proxy.py 8888`) now starts the full memory stack.
- The proxy and daemon share a process lifetime — stopping the proxy stops the daemon.
- The liveness check from ADR-005 remains as a belt-and-suspenders signal for the brief window between proxy start and the daemon registering its Postgres listener connection.
- `shutil` and `pathlib.Path` added as imports (stdlib only, no new dependencies).

### Rejected alternatives
- **systemd unit for the daemon:** decouples lifetimes but adds OS-level configuration, making the setup less portable.
- **Restart the daemon on unexpected exit:** adds complexity; deferred until there is evidence that daemon crashes are operationally significant.

---

## [2026-05-21] ADR-005: Consolidation Daemon — Liveness Check on Save

### Status
Accepted

### Context
The consolidation daemon (`consolidation_loop.py`) must be started manually before any agent session. `pg_notify('new_artifact', ...)` is fire-and-forget: if no backend is listening when a fact is saved, the notification is permanently lost and no Tier 3 consolidation is ever triggered for that fact. This failure was silent — agents received a successful save response with no indication that consolidation would not run.

Discovered during architecture review on 2026-05-21.

### Decision
1. **Daemon registers itself:** The listener connection in `consolidation_loop.py` now connects with `application_name="consolidation_daemon"`.
2. **Save paths check before notifying:** Both `memory_bridge.py` (CLI, used by Claude Code and Gemini CLI) and `vector-skill.py` (MCP, used by LM Studio) query `pg_stat_activity` within the same cursor transaction as the INSERT, before issuing `pg_notify`:
   ```sql
   SELECT count(*) FROM pg_stat_activity WHERE application_name = 'consolidation_daemon'
   ```
3. **Warning surfaced in response:** If the count is zero, a `WARNING:` string is appended to the save response. The save itself always completes — the check informs, it does not block.

### Consequences
- Any agent that saves a fact while the daemon is down receives an explicit warning instead of a silent success.
- No schema changes required; `pg_stat_activity` is always available.
- The check adds one fast catalog query per save (negligible overhead).
- The daemon must still be started manually — this change does not auto-start it.
- Edge case: if multiple daemon instances are running, `count(*) > 0` is still correct (over-counting is safe).
- Edge case: the check fires after the INSERT RETURNING and before pg_notify — all within one autocommit cursor, so the pg_id is valid when the check runs.

### Rejected alternatives
- **Auto-start daemon from save path:** would couple a long-lived background process lifetime to a single save call; not appropriate.
- **Queue unsent notifications in a table:** adds schema complexity and a separate drain mechanism; deferred to future work if silent drops prove operationally significant.

---

## [2026-05-21] ADR-004: Hive-Mind Gateway — Async aiohttp Rewrite (supersedes ADR-003)

### Status
Accepted

### Context
ADR-003 replaced the single-threaded `TCPServer` with `ThreadingHTTPServer` — one thread per connection, stdlib only. This eliminated head-of-line blocking but retained the structural ceiling of the threaded model: full response buffering in RAM before the first byte reaches the client (no streaming), only `POST` handled (GET health checks and model listings silently dropped), substring route matching (`"/embeddings" in path` misroutes `/v1/embeddings_bulk`), no structured logging, and no graceful shutdown — SIGINT inside `asyncio.run()` does not surface as `KeyboardInterrupt` in a running event loop.

A parallel iterative development process (Claude + Gemini across six versions, v2–v6) produced a complete async rewrite and a full decision log (`proxy_implementation.md`). Eight bugs were found and fixed in the initial async attempt; two subsequent Gemini iterations contributed critical fixes (503 vs 500 semantics, `CancelledError` re-raise, `proxy_resp.prepared` guard); a four-case architectural audit produced the final v6 design.

### Decision
Replace `http.server.ThreadingHTTPServer` + `urllib` with `aiohttp.web` + `aiohttp.ClientSession`. The rewrite is complete and the implementation is `hive_mind_proxy.py` (formerly `proxy_v6_c.py`). Key properties of the v6 design:

- **True streaming:** `iter_any()` pipes upstream chunks to the client as they arrive; no buffering. A 4,000-token generation begins reaching the client after the first chunk, not after the model finishes.
- **RFC 7230 hop-by-hop filtering:** `HOP_BY_HOP` frozenset applied symmetrically to both request and response headers, including `Content-Length` — forwarding a stale byte count with chunked framing causes clients to truncate or hang.
- **`auto_decompress=False`:** aiohttp decompresses by default but still forwards `Content-Encoding: gzip`. A client receiving decompressed bytes labelled as compressed double-decompresses — corruption. Disabled so compressed bytes and their headers travel together.
- **`CancelledError` re-raised at every level:** swallowing it leaves cancelled tasks zombie; `runner.cleanup()` stalls indefinitely during graceful shutdown.
- **Self-defusing signal handler:** after the first SIGINT/SIGTERM, both handlers are removed. A second Ctrl+C falls back to Python's default `SIGINT` disposition — `KeyboardInterrupt` — providing an emergency hard-abort if the drain stalls on a hung backend.
- **`allow_redirects=False`:** a proxy must forward redirects to the client, never chase them silently.
- **`limit_per_host=80`:** without a per-host ceiling, an embedding burst can exhaust the 200-connection pool and starve the LLM backend.
- **`ClientTimeout(total=None, connect=5.0)`:** fast failure on dead backends; no ceiling on live generations.
- **HTTP 503 for upstream unreachable** (not 500 — the proxy is fine; the backend is not); **504 for connect timeout** (not 500).

Run command: `uv run --with aiohttp python shared-memory/scripts/hive_mind_proxy.py 8888`

### Consequences
- LLM token streaming is now live end-to-end. Clients see tokens as they are generated.
- Graceful shutdown drains in-flight requests before closing the connection pool.
- All methods handled (GET, POST, PUT, DELETE) — health check endpoints now work.
- Route matching is prefix-based (`startswith`), not substring — misrouting eliminated.
- `aiohttp` is a new runtime dependency; must be specified as `--with aiohttp` in `uv run` invocations (not in the project's default venv).
- The full decision rationale for every property of the v6 design is in `proxy_implementation.md`.

### Rejected alternatives
- **Keep ThreadingHTTPServer + add streaming:** stdlib `urllib` buffers the entire upstream response before returning — streaming requires replacing the HTTP client, not just the server. The change scope was equivalent to a full rewrite.
- **Strip `Accept-Encoding` from hop-by-hop to prevent compression:** `Accept-Encoding` is an end-to-end header by RFC 7230 — stripping it is semantically incorrect even if it achieves the practical goal for localhost backends. `auto_decompress=False` solves the actual problem correctly.

---

## [2026-05-20] ADR-003: Hive-Mind Gateway — Thread-Per-Request Concurrency

### Status
Superseded by ADR-004

### Context
`hive_mind_proxy.py` used `socketserver.TCPServer`, which is single-threaded. All requests were serialized in the OS accept queue. A slow upstream call (large embedding payload or LLM inference to port 5000, which can take 10–60 seconds) blocked every other agent for its full duration. GEMINI.md previously documented the gateway as "multithreaded" — that was inaccurate.

Measured empirically: with one slow request in flight, two fast requests each waited 5.4 seconds before completing.

### Decision
Replace `socketserver.TCPServer` with `http.server.ThreadingHTTPServer` (one-word change, stdlib only, no new dependencies). This mixin spawns a new thread per accepted connection, allowing concurrent upstream I/O across all agents.

### Consequences
- Fast requests complete in ~27ms while a slow request runs concurrently.
- Five simultaneous 100ms requests complete in 0.105s wall time (previously ~0.5s serial).
- Thread count is unbounded but capped naturally by the number of concurrent agents (3 local agents maximum in this setup).
- GEMINI.md "multithreaded" claim now accurately reflects the implementation.

---

## [2026-05-20] ADR-002: Hive-Mind Gateway — Reranking Route Fix

### Status
Accepted

### Context
The Hive-Mind Gateway (`hive_mind_proxy.py`) was deployed to enforce a single entry point for all embed/rerank traffic across agents. However, only the `/v1/embeddings` path was explicitly handled. `/v1/reranking` had no dedicated branch and fell through to port 5000 (LM Studio), which does not implement the reranking API. All reranking calls were silently failing with a 404-equivalent error, meaning `memory_bridge.py` and `vector-skill.py` were running without reranking even when BGE-Reranker-v2-m3 was healthy.

A second bug was also present: `json` was referenced in the error handler but never imported, causing a `NameError` crash on any proxy-level error — making failures invisible.

Discovered and fixed via Claude Code on 2026-05-20 during a full stack audit.

### Decision
1. Added explicit `elif "/reranking" in self.path` branch routing `/v1/reranking` → `localhost:8071`.
2. Added `import json` to the module imports.
3. Updated startup log to print the correct three-way routing table.
4. Synced fix to the Gemini skill package (`shared-memory-skill/`) and all SKILL.md documentation.
5. Updated all agent scripts to reference port 8888 in error messages (previously said "port 8070 is down").

### Consequences
- Reranking now works correctly end-to-end through the gateway for all agents.
- Full routing table: `/v1/embeddings` → 8070 | `/v1/reranking` → 8071 | default → 5000.
- All 12 unit tests pass. Live gateway verified: embeddings return 1024-dim vectors, reranking returns ranked scores.

---

## [2026-05-20] ADR-001: Event-Driven "Sleep Cycle" Consolidation (v2)

### Status
Accepted (Amended with Cumulative Synthesis and Optimized Session Lifecycle)

### Context
Initial consolidation produced isolated snapshots. As a concept evolved, the vector store would become cluttered with multiple summaries of the same entity, leading to context drift. Furthermore, holding Neo4j sessions open during long LLM inference calls (300s+) was identified as a stability risk.

### Decision
We have enhanced the Consolidation Daemon (`consolidation_loop.py`) with the following architectural patterns:

1.  **Cumulative Narrative Synthesis:** Instead of creating isolated summaries, the generator now fetches the *most recent existing summary* for an entity and folds in new facts to produce a single, cohesive, updated narrative.
2.  **Short-Lived Database Sessions:** We decoupled Neo4j sessions from long-running async LLM calls. Sessions are now opened only for the initial discovery query and the final atomic write, eliminating server-side timeout risks.
3.  **Graph Back-pointers:** Summaries are now mirrored in Neo4j as `CommunitySummary` nodes. Each source `Fact` is linked via a `SUMMARIZED_BY` relationship, providing bidirectional traceability between episodic and semantic memory.
4.  **Optimized Batch Writing:** We refactored the Cypher logic to use `UNWIND/collect` patterns, separating bulk property updates from single-node creation to reduce redundant MERGE operations.
5.  **Hard Deferral Backstop:** To prevent "starvation" during continuous ingestion streams, we implemented a `MAX_DEFERRAL_SEC` backstop ($3 \times$ idle threshold).

### Consequences & Outcomes
- **Resolved: Content Drift.** The cumulative synthesis ensures a single "Source of Truth" for an entity's narrative state.
- **Open: Structural Accumulation.** While content is consolidated, the system still inserts new rows in `community_summaries` for every cycle. Pruning or "retiring" superseded vectors remains an open optimization task.
- **Accepted Risk: Cross-DB Atomicity.** We acknowledge that a failure in the Postgres commit *after* a successful Neo4j write can result in 'lost' facts (flagged as consolidated but missing from the vector summary). This is an accepted trade-off to avoid the complexity of a 2PC coordinator in this local high-reliability environment.
- **Improved Observability:** Every `Fact` is now explicitly traceable to the `CommunitySummary` that represents it.

### Alternatives Considered
- **Saga Pattern / 2PC:** (Rejected) Overkill for a local single-user system.
- **Summary Node Full-Text Storage:** (Rejected) We only store metadata in Neo4j; full text lives exclusively in Postgres to maintain a single source of truth and keep the graph lean.


---
