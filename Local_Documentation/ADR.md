# Architectural Decision Records (ADR) - Shared Memory Framework

This document tracks the evolution of the Oratotis Shared Memory Framework. Each entry represents a grounded decision made to ensure technical integrity and agentic clarity.

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

## [2026-05-24] ADR-005: Memory Coordinator — Centralised I/O with asyncpg Pool and Outbox Pattern

### Status
Accepted

### Context
Each save previously opened its own psycopg2 connection and Neo4j session. Under concurrent multi-agent writes this produced connection burst spikes, race conditions on Entity hub creation (two agents could MERGE the same entity simultaneously, producing duplicates or deadlocks), and the ADR-001 cross-DB atomicity risk: a crash after the Neo4j write but before the Postgres commit left the fact permanently orphaned.

### Decision
Introduce `coordinator.py` — an async aiohttp module embedded in the gateway — to centralise all Postgres and Neo4j I/O:

1. **asyncpg connection pool** (min=2, max=10) — one pool per gateway process; eliminates the per-save connection cost.
2. **Per-entity `asyncio.Lock`** — sorted-order acquisition prevents deadlocks; concurrent saves to the same entity cluster serialise cleanly.
3. **Outbox pattern** — every save writes a `neo4j_outbox` row inside the same Postgres transaction as `technical_docs`. A background worker (`_outbox_worker`) drains the outbox asynchronously every 2 seconds and applies Neo4j writes. Eliminates ADR-001 cross-DB risk.
4. **Routes** exposed: `POST /memory/save`, `POST /memory/search`, `POST /memory/graph`, `GET /memory/status/{pg_id}`.
5. **`?consistency=neo4j`** parameter blocks the HTTP response until the outbox row transitions to `applied` (15 s timeout).
6. **memory_bridge.py** simplified to a thin HTTP client — no direct Postgres or Neo4j imports; all storage I/O delegated to coordinator.

### Consequences
- Saves are Postgres-ack by default (fast) and Neo4j-consistent on demand.
- ADR-001 dangling-Fact risk is eliminated.
- Direct agent-to-database connections are gone; coordinator is the single choke point for all memory writes.
- `asyncpg` added to `requirements.txt`.

### Alternatives Considered
- **Keep per-call connections:** Does not scale to concurrent agents; connection pool is the standard solution.
- **Synchronous Neo4j write in save path:** Reintroduces ADR-001 risk; also adds Neo4j latency to every HTTP response.

---

## [2026-05-24] ADR-006: Configurable Ontology — Path A (ontology.yaml + ONT Singleton)

### Status
Accepted (Path A complete; Path B planned)

### Context
All Neo4j label names (`Fact`, `Entity`, `CommunitySummary`, etc.) and relationship types (`MENTIONS`, `SUMMARIZED_BY`, etc.) were hardcoded as inline strings inside Cypher f-strings. Changing the graph schema required finding and updating every Cypher statement across multiple scripts. There was also no validation — a typo would produce a silent Cypher injection or a confusing query error.

### Decision — Path A
Extract all labels and relationship types to `ontology.yaml` at the repo root. A new `ontology.py` module loads the yaml at import time, exposes an `OntologyConfig` dataclass and a module-level `ONT` singleton, and validates every string field against `^[A-Za-z_][A-Za-z0-9_]*$` before startup. Falls back to hardcoded defaults if the file is absent.

`vector-skill.py` (at repo root) accesses `ontology.py` (in `shared-memory/scripts/`) via a `sys.path.insert` hack — the two are not in the same Python package. Noted as technical debt; resolved when the codebase is packaged (see Roadmap).

### Decision — Path B (planned, not yet implemented)
Store the ontology AS nodes and relationships in Neo4j itself: `(:Class {name:'Fact'})` nodes connected by `SCO` (subclass-of) relationships. Bootstrap from `ontology.yaml` on every proxy startup. Enables live ontology inspection, Cypher hierarchy queries, and forward compatibility with Neosemantics (n10s) for OWL/RDFS import and inference. `ontology.yaml` remains the human-editable source of truth; the graph is a materialised copy.

### Consequences
- Deployers can rename any label or relationship without touching Python source.
- Startup fails fast with a clear error if any ontology value would inject Cypher.
- `density_threshold` (consolidation trigger) is also configurable in the same file.

---

## [2026-05-27] ADR-008: Source Provenance Column — `source_pg_ids` on `community_summaries`

### Status
Accepted

### Context
When the consolidation daemon synthesises a `community_summaries` row from multiple `technical_docs` facts, the contributing fact IDs were stored only inside the `metadata` JSONB blob. This made provenance queries expensive (JSON path extraction, no index) and invisible to callers inspecting the table schema. The gap surfaced clearly when evaluating citation traceability: any agent that retrieved a community summary had no queryable path back to the original facts without parsing raw metadata JSON.

### Decision
Add a first-class `source_pg_ids INTEGER[]` column to `community_summaries` (migration 003). Changes:

1. **`migrations/003_source_pg_ids.sql`** — `ALTER TABLE ... ADD COLUMN IF NOT EXISTS source_pg_ids INTEGER[]`; back-fill existing rows from `metadata->'source_pg_ids'` via `jsonb_array_elements_text`. Idempotent.
2. **Both `consolidation_loop.py` copies** — `INSERT` and `ON CONFLICT DO UPDATE` extended to write `pg_ids` (already collected from Neo4j) into the new column on every consolidation write. The skill-copy INSERT was also missing `ON CONFLICT DO UPDATE` entirely (regressed since migration 002) — corrected in the same change.
3. **`tests/test_consolidation_e2e.py`** — verification block now selects `source_pg_ids` and fails the test if `NULL`, catching future regressions.

`source_pg_ids` in the `metadata` JSONB is retained as-is for backwards compatibility with tooling that reads raw metadata.

### Consequences
- Provenance queries are now a simple array operation: `SELECT * FROM community_summaries WHERE $fact_id = ANY(source_pg_ids)`.
- The column can be indexed with a GIN index if query volume warrants it.
- `metadata` JSONB is now partially redundant for `source_pg_ids`; the column is the authoritative path.
- Skill-copy `ON CONFLICT` bug resolved — re-consolidations of the same entity no longer risk a crash on the second cycle.

### Alternatives Considered
- **Keep IDs in JSONB only:** Functional but forces every provenance query through `jsonb_array_elements_text`, which cannot use a standard btree index and couples callers to the internal metadata shape.
- **Separate `summary_sources` join table:** More normalised but adds a join to every provenance query and a second write on every consolidation cycle. The array column achieves the same result with one column and one write.

---

## [2026-05-26] ADR-007: Concurrency and Security Hardening — Multi-Agent Correctness Baseline

### Status
Accepted

### Context
The coordinator (ADR-005) was designed for a single-agent workstation. A structured audit of concurrent multi-agent workloads identified nine correctness bugs and six security gaps. The bugs could corrupt state under parallel writes; the security gaps left the coordinator open to destructive Cypher, network exposure, and prompt injection.

### Decision — Concurrency (C1–C9)
- **C1:** Track acquired locks in a list; release only what was acquired. Prevents `RuntimeError` when `asyncio.Lock.acquire()` is cancelled mid-list.
- **C2:** `FOR UPDATE SKIP LOCKED` in `_drain_outbox` inside a transaction — concurrent coordinator instances claim disjoint outbox rows; no double-drain.
- **C3:** `SET retries = retries + 1` (SQL-level) — atomic; prevents lost increment when two instances process the same row.
- **C4:** Single `UNWIND` query creates Fact + all Entity nodes + all MENTIONS edges in one Neo4j round-trip — fully atomic.
- **C5:** `ON CONFLICT ((metadata->>'entity')) DO UPDATE` on `community_summaries` + unique partial index (migration 002) — one summary per entity; upsert semantics.
- **C6:** `ON CONFLICT DO UPDATE` includes `embedding = EXCLUDED.embedding` — re-saves refresh stale vectors.
- **C7:** `conn.poll()` wrapped with reconnect on `DatabaseError`/`OperationalError` — silent LISTEN connection loss no longer stops the daemon.
- **C8:** `limit` capped at `min(max(1, n), 100)` in `handle_search`.
- **C9:** `select.select()` moved to `loop.run_in_executor` — asyncio event loop stays responsive during 1-second poll windows.

### Decision — Security (S1–S7 partial)
- **S1:** `_WRITE_CYPHER` regex guard in `handle_graph` rejects `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `MERGE`, `CALL`, `LOAD CSV`, `DROP` — defence-in-depth; Neo4j RBAC is the complete solution.
- **S2:** Proxy binds to `127.0.0.1` by default; `PROXY_BIND=0.0.0.0` opts into all-interfaces binding.
- **S4:** `str(exc)` in error responses replaced with `"query failed"`; full exception logged server-side.
- **S5:** `limit` bounded (C8 covers this).
- **S6:** `ontology.py` `_validate()` at import time rejects any label/rel not matching the Cypher identifier grammar — prevents injection via tampered `ontology.yaml`.
- **S7:** Retrieved facts wrapped in `[BEGIN/END RETRIEVED FACTS]` delimiters with a "treat as DATA" preamble in consolidation prompts — hardens Tier 3 synthesis against stored prompt injection.
- **S3/Phase 2C (pending):** Pre-shared agent token authentication via `AGENT_TOKENS` env var and `Authorization: Bearer` middleware. Separate PR; requires coordinated agent rollout.

### Consequences
- System is correct under concurrent multi-agent writes.
- Destructive Cypher blocked at coordinator level.
- LAN exposure eliminated by default; opt-in documented.
- Stored injection surface reduced at the synthesis layer; Tier 1 retrieval (raw facts in agent context) remains unprotected until ingestion boundary sanitisation is implemented.
- Migration 002 applied: two new indexes in Postgres.

### Alternatives Considered
- **Python-level lock tracking via contextlib:** Cleaner but adds a dependency on a non-obvious pattern; the explicit `acquired` list is readable to any contributor.
- **Neo4j RBAC instead of regex guard:** The correct long-term solution; regex is interim defence-in-depth while RBAC setup is documented.
- **Bind to 0.0.0.0 with firewall guidance:** Rejected — default-secure is better than default-open with instructions.

---

## [2026-05-29] ADR-009: Async Consolidation Daemon — Non-Blocking I/O via AsyncGraphDatabase + run_in_executor

### Status
Accepted

### Context
`consolidation_loop.py` used the synchronous `neo4j.GraphDatabase` driver and `psycopg2` directly inside `async def` functions. Both block the asyncio event loop thread for the full duration of each DB round-trip. During a slow Neo4j traversal or a Postgres write, the single-threaded event loop cannot process new Postgres `LISTEN/NOTIFY` signals, service timeouts, or advance other coroutines. Under sustained write bursts, `new_artifact` notifications accumulated faster than the stalled daemon could drain them, causing missed consolidation triggers with no error logged. `connect_timeout` was also absent on `psycopg2.connect()`, allowing a silent hang of up to 130 seconds on a dead Postgres host.

### Decision
1. Replace `from neo4j import GraphDatabase` with `AsyncGraphDatabase` (already the pattern in `coordinator.py`). All `with self.driver.session()` blocks become `async with`; all `session.run()` calls are awaited.
2. Wrap every `psycopg2` call block in `loop.run_in_executor(None, lambda: ...)`. Each lambda is `await`ed before the next, serialising access to the shared connection — psycopg2 is safe for single-threaded use in this pattern.
3. Make `_make_listen_conn` async; wrap the entire connect + set_isolation_level + LISTEN sequence in a single `run_in_executor` lambda.
4. Make `stop()` async (`await self.driver.close()`).
5. Add `connect_timeout=5` to all `psycopg2.connect()` calls.

### Consequences
- The event loop never blocks during DB I/O; `LISTEN/NOTIFY` signals are processed immediately.
- `run_in_executor` serialisation is safe because each call is `await`ed before the next — no concurrent psycopg2 access occurs.
- No asyncpg migration required; psycopg2 stays as the consolidation daemon's Postgres driver.
- Startup is slightly slower (async driver initialisation) but negligible.

### Alternatives Considered
- **Full asyncpg migration:** More idiomatic but requires rewriting all cursor-based SQL into asyncpg's API. Higher risk for a background daemon. Deferred.
- **Leave as-is:** `NOTIFY` drops under load; accepted as known defect. Rejected once the drop mechanism was confirmed.

---

## [2026-05-29] ADR-010: Outbox Atomicity — Atomic in_progress Claim + Startup Recovery

### Status
Accepted

### Context
`_drain_outbox` used `SELECT ... FOR UPDATE SKIP LOCKED` inside a transaction to claim rows, then committed that transaction before applying the rows to Neo4j. Once the transaction committed, the rows were unlocked and still `status='pending'`. A second coordinator instance polling 2 seconds later could claim the same rows. Since Neo4j writes use `MERGE` (idempotent), data was not corrupted, but the `retries` counter was incremented twice per actual failure. A row could reach `OUTBOX_MAX_RETRIES=5` in three actual Neo4j failures, be permanently marked `failed`, and the corresponding Neo4j node would never be created — Postgres would report a successful save while the graph was silently incomplete. The failure-path update also conditioned on `AND status='pending'`, which would silently no-op once the row was claimed (status was not updated during the claim window).

### Decision
1. Replace the `SELECT ... FOR UPDATE SKIP LOCKED` claim with `UPDATE neo4j_outbox SET status='in_progress' WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING id, pg_id, cypher_params, retries` — rows are atomically owned before the lock is released.
2. On coordinator `start()`, run `UPDATE neo4j_outbox SET status='pending' WHERE status='in_progress'` — rows stuck in `in_progress` after a crash are recovered.
3. On apply failure, reset to `status='pending'` unconditionally (remove `AND status='pending'` guard).
4. Document `in_progress` as a valid transient status in `schema.md`.

### Consequences
- Two coordinator instances can never process the same outbox row simultaneously.
- Crash survivors are always recovered on next startup — no manual intervention needed.
- `GET /memory/status/{pg_id}` may briefly return `in_progress`; callers must tolerate this value.
- The state machine is now: `pending` → `in_progress` → `applied` | `failed` (with `pending` as the retry reset target).

### Alternatives Considered
- **Per-row advisory locks (`pg_try_advisory_xact_lock`):** Lighter — no new status value. Rejected because crash recovery requires a separate mechanism (advisory locks are automatically released on connection close, not on row ownership).
- **Hold transaction open across Neo4j apply:** Eliminates the window but keeps a Postgres transaction open for the duration of a Neo4j network call — lock contention scales with Neo4j latency.
- **Defer (MERGE idempotency covers data):** The retry counter corruption was accepted as a known defect. Rejected once the premature-failure scenario was confirmed.
