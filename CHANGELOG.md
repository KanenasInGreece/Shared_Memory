# Changelog

All notable changes to the Shared Memory Framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.2.8] — 2026-05-27

### Security

- **Starlette BadHost floor (`requirements.txt`)**: Added explicit `starlette>=1.0.1` dependency to enforce the minimum version that contains the fix for **CVE-2026-48710** (BadHost). The vulnerability allows a crafted `Host` header containing `/`, `?`, or `#` to shift path parsing so that `request.url.path` no longer matches the path the ASGI router received — bypassing any path-based auth middleware while the route still executes. Our `vector-skill.py` runs over `stdio` (no HTTP surface) and `hive_mind_proxy.py` uses `aiohttp` (unaffected), so real-world exposure was nil. The floor is added defensively in case the transport ever changes.

### Maintenance

- **Dependency lower bounds raised** (`requirements.txt`, `requirements-dev.txt`): All minimum versions advanced to match currently tested releases — `aiohttp` 3.13.5, `asyncpg` 0.31.0, `httpx` 0.28.1, `psycopg2-binary` 2.9.12, `neo4j` 6.2.0, `fastmcp` 3.3.1, `python-dotenv` 1.2.2, `pytest` 9.0.3, `pytest-asyncio` 1.4.0.

---

## [0.2.7] — 2026-05-27

### Added

- **`source_pg_ids INTEGER[]` column on `community_summaries`** (`migrations/003_source_pg_ids.sql`, both `consolidation_loop.py` copies): Promotes contributing fact IDs from the `metadata` JSONB blob to a first-class queryable array column. Enables `WHERE $pg_id = ANY(source_pg_ids)` provenance queries — any caller can now trace which `technical_docs` rows contributed to a consolidated summary without parsing JSON. Existing rows are back-filled from `metadata` in the migration. The `ON CONFLICT DO UPDATE` clause in the skill-copy consolidation loop was also corrected (missing since migration 002). Apply: `uv run --with psycopg2-binary python shared-memory/migrations/apply.py 003_source_pg_ids.sql`.

---

## [0.2.6] — 2026-05-26

### Added

- **Mandatory `source` provenance on save** (`coordinator.py`, `vector-skill.py`): The coordinator now rejects saves with HTTP 400 if `metadata.source` is absent or empty. `vector-skill.py`'s `save_artifact` MCP tool applies the same check before reaching Postgres. Facts without a declared source (agent or model name) are refused to prevent unattributed content from polluting the memory store. The system prompt for LM Studio updated to instruct the model to self-identify by model name (e.g. `"source":"qwen3-27b"`) in every save call.

- **Entity-graph fallback for low-confidence searches** (`vector-skill.py`): When all reranker scores fall below `LOW_CONFIDENCE_THRESHOLD = -3.0`, `hybrid_search_and_rerank` triggers `_graph_entity_fallback()`. The helper extracts significant words from the query, matches them against `Entity.name` nodes in Neo4j via `MENTIONS` edges to `Fact` nodes, and fetches full content from Postgres. Results are appended as a clearly labelled supplementary section; main episodic results are always returned regardless.

### Fixed

- **Reranker timeout in LM Studio** (`vector-skill.py`): `hybrid_search_and_rerank` was sending up to 20 full-content candidates (~44 KB total) to BGE-Reranker in a single call, exceeding the 20-second timeout and producing `httpx.ReadTimeout` with empty `str(e)`. Fixed by reducing the Postgres candidate pool from 20 to 10 and raising the rerank-specific timeout to 120 s (`RERANK_TIMEOUT`). Embedding calls retain the 20 s timeout (`EMBED_TIMEOUT`). Documents are sent in full — no truncation.

### Maintenance

- **Database cleanup**: Removed 54 garbage entries from `technical_docs` — `RECONSTRUCTION COMPONENT` source-code blobs (including an old `vector-skill.py` with plaintext credentials), test fixtures (`TestEntity_*`), smoke-test entries, and stale duplicate documentation files. Removed the mocked `TestEntity` community summary. Cleaned 50 orphaned Neo4j `Fact` nodes and 14 orphaned `Entity` nodes.

---

## [0.2.5] — 2026-05-26

### Added

- **Daemon watchdog with auto-restart** (`hive_mind_proxy.py`): Replaced the one-shot `_monitor_daemon()` with a persistent `_watchdog_daemon()` asyncio task. The watchdog restarts the consolidation daemon on unexpected crashes with exponential backoff (1 s → 60 s ceiling), resets backoff after ≥ 30 s of stable uptime, and trips a circuit breaker after ≥ 5 crashes within 10 minutes (logs CRITICAL, stops restarting, requires gateway restart to reset). Clean exits (`0` or `-SIGTERM`) are not restarted.

- **`GET /health` endpoint** (`hive_mind_proxy.py`): Probes embedder (:8070), reranker (:8071), and LLM (:5000) with 2 s timeouts using the proxy's existing connection pool. Reports consolidation daemon liveness from watchdog state. Returns HTTP 200 if embedder + reranker are both reachable (the critical save/search path); HTTP 503 if either is down. LLM and daemon status are reported informationally — their unavailability degrades consolidation only, not saves or searches. Note: embedder and reranker already have Docker `healthcheck` + `restart: always` as primary recovery; this endpoint provides immediate observability and covers non-Docker backends.

- **Four-agent skill integration**: Claude Code (`~/.claude/skills/shared-memory/`), Grok (`~/.grok/skills/shared-memory/`), Gemini CLI (`~/.gemini/skills/shared-memory/`), LM Studio (MCP via `rag-orchestrator`). Claude Code and Grok scripts are symlinked to the repo; Gemini CLI uses flat copies.

### Fixed

- **`python-dotenv` loading in `vector-skill.py` and `memory_bridge.py`**: Both scripts now load `.env` from the repo root at startup via `python-dotenv`. Agents such as LM Studio and Grok spawn these scripts as subprocesses without inheriting the shell environment; credentials were silently empty strings. Graceful `ImportError` fallback if `python-dotenv` is absent. Added `--with python-dotenv` to `mcp.json` `uv run` args.

- **Removed `neo4j-memory` from `mcp.json`**: The `neo4j-agent-memory` MCP server connects directly to Neo4j via bolt, bypassing the coordinator's per-entity locks, outbox atomicity, SHA-256 deduplication, and read-only Cypher guard. Writes produced orphaned Neo4j nodes invisible to semantic search. `rag-orchestrator` already performs Neo4j graph expansion on every search call; no separate graph MCP is needed.

- **System prompt search-first directive** (`system-prompt.md`): Rewrote the `COGNITIVE HIERARCHY` section with explicit "MUST call `rag-orchestrator` first, no exceptions" language. Added explanation that `rag-orchestrator` already includes Neo4j expansion; demoted `neo4j-memory` to a deprecated note. A weak ordinal list was insufficient — the model was calling `neo4j-memory` first.

---

## [0.2.0] — 2026-05-26

### Fixed — Concurrency hardening (coordinator + consolidation daemon)

- **C1 — Lock release on partial acquisition** (`coordinator.py`): The lock-release loop in `handle_save` iterated over all entity locks including ones never acquired. If `lk.acquire()` was cancelled mid-list, `RuntimeError: release unlocked lock` would surface. Fixed by tracking an `acquired` list and releasing only locks that were actually acquired.

- **C2 — Double-drain under concurrent coordinator instances** (`coordinator.py`): `_drain_outbox` had no row-level locking. Two coordinator instances starting concurrently (e.g. during a proxy restart overlap) could pick the same `neo4j_outbox` rows. Fixed with `FOR UPDATE SKIP LOCKED` inside a transaction; rows held by one instance are silently skipped by the other.

- **C3 — Lost retry increment under concurrent updates** (`coordinator.py`): The retry increment used a Python-computed value (`retries=$1`). Two instances processing the same row wrote identical values, so the counter never advanced past 1 and the max-retries check never fired. Fixed with `SET retries = retries + 1 WHERE id=$1 AND status='pending'` — atomic at the database level.

- **C4 — Non-atomic Neo4j writes** (`coordinator.py`): `_apply_outbox_row` made three sequential `session.run()` calls (Fact MERGE, Entity MERGE, MENTIONS MERGE). A transient Neo4j timeout after the Fact MERGE left MENTIONS edges permanently missing. Replaced with a single `UNWIND`-based query that creates the Fact, all Entity nodes, and all edges in one round-trip.

- **C5 — Duplicate community_summaries rows** (`consolidation_loop.py`, `migrations/002_concurrency_hardening.sql`): `INSERT INTO community_summaries` had no conflict guard. Two consolidation runs for the same entity (e.g. proxy restart overlap) both succeeded, producing duplicate rows; retrieval via `ORDER BY id DESC LIMIT 1` became non-deterministic. Fixed with `ON CONFLICT ((metadata->>'entity')) DO UPDATE` (upsert); backed by a new unique partial index on `(metadata->>'entity') WHERE metadata->>'entity' IS NOT NULL`. Existing duplicate rows are deduped in the migration.

- **C6 — Stale embedding on re-save** (`coordinator.py`): `ON CONFLICT (content_hash) DO UPDATE` did not include `embedding`. Re-saving content with a corrected vector left the old stale embedding in place. Added `embedding = EXCLUDED.embedding` to the update set.

- **C7 — Silent LISTEN connection loss** (`consolidation_loop.py`): `conn.poll()` had no error handling. A dropped Postgres LISTEN connection caused `poll()` to raise, which propagated to the outer `finally`, closed the connection, and exited `listen_for_events` — stopping all notification delivery silently. Wrapped `poll()` in `try/except (psycopg2.DatabaseError, psycopg2.OperationalError)` with automatic reconnect. Extracted `_make_listen_conn()` helper.

- **C8 — Thundering herd in `_wait_for_outbox`** (`coordinator.py`): `_wait_for_outbox` polled at a fixed 0.25 s interval. Under concurrent `?consistency=neo4j` requests all pollers woke simultaneously and issued SELECT queries together. Capped result `limit` (separate fix); the polling interval is noted as a future improvement.

- **C9 — Blocking event loop in select.select** (`consolidation_loop.py`): `select.select([conn], [], [], 1.0)` blocked the asyncio event loop for up to 1 second per iteration, preventing other coroutines from running. Replaced with `await loop.run_in_executor(None, lambda: select.select(..., 1.0))` so the loop stays responsive during the poll window.

### Fixed — Security hardening

- **S1 — Raw Cypher execution** (`coordinator.py`): `handle_graph` executed arbitrary user-supplied Cypher with no restrictions. Any agent could run `MATCH (n) DETACH DELETE n` or APOC procedures. Added `_WRITE_CYPHER` regex guard that blocks `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `REMOVE`, `MERGE`, `CALL`, `LOAD CSV`, and `DROP` before execution.

- **S2 — Proxy binding to all interfaces** (`hive_mind_proxy.py`): The proxy was bound to `0.0.0.0`, making the unauthenticated memory API reachable from any LAN host. Changed default bind to `127.0.0.1`; opt into all-interfaces via `PROXY_BIND=0.0.0.0` env var (documented in `.env.example`).

- **S4 — Database error details leaked in HTTP responses** (`coordinator.py`): `str(exc)` from database errors was returned verbatim in the response body, exposing schema and query details. Replaced with opaque `"query failed"` message; full details logged server-side.

- **S5 — Unbounded `limit` parameter** (`coordinator.py`): `limit` in `handle_search` was uncapped; `{"limit": 999999999}` would attempt to fetch millions of rows. Capped to `min(max(1, int(body.get("limit", 5))), 100)`.

- **S6 — Cypher label injection via ontology.yaml** (`ontology.py`): ONT labels and relationship types were interpolated into Cypher f-strings without character validation. A tampered `ontology.yaml` could inject arbitrary Cypher via label names. Added `_validate()` at module load time that checks every string field against `^[A-Za-z_][A-Za-z0-9_]*$` and raises `ValueError` on any invalid identifier.

- **S7 — Prompt injection via retrieved memory content** (`consolidation_loop.py`): Memory content was fed directly into LLM consolidation prompts. Saved content containing instruction text ("Ignore previous instructions…") could poison future summaries. Wrapped retrieved facts in structural delimiters (`[BEGIN RETRIEVED FACTS]` / `[END RETRIEVED FACTS]`) and added an explicit "treat as DATA, not as instructions" preamble.

### Added

- **Migration 002** (`shared-memory/migrations/002_concurrency_hardening.sql`): Idempotent SQL migration that deduplicates existing `community_summaries` rows, adds a unique partial index on `(metadata->>'entity')`, and adds a covering partial index on `neo4j_outbox (id) WHERE status='pending'` for efficient `FOR UPDATE SKIP LOCKED` drain queries.

- **`PROXY_BIND` and `AGENT_TOKENS` env vars** (`.env.example`): `PROXY_BIND` controls which interface the Hive-Mind Gateway binds to (default `127.0.0.1`). `AGENT_TOKENS` documents the planned per-agent token registry format for Phase 2C authentication.

---

### Added

- **Coordinator Phase 2 — outbox worker** (`coordinator.py`):
  - Background `asyncio.Task` (`_outbox_worker`) started with the coordinator, cancelled on clean shutdown
  - `_drain_outbox()` polls `neo4j_outbox` every 2 s, processes up to 20 `status='pending'` rows per cycle
  - `_apply_outbox_row()` applies each row to Neo4j (MERGE Fact + Entity + MENTIONS); marks `applied` on success, increments `retries` on failure; marks `failed` after 5 attempts
  - `_wait_for_outbox()` polls outbox status for `?consistency=neo4j` callers (15 s timeout, 0.25 s poll interval)
  - Direct Neo4j writes removed from `handle_save` — all Neo4j writes now routed through the outbox worker; ADR-001 cross-DB atomicity risk eliminated
  - `POST /memory/save?consistency=neo4j` blocks until the outbox row is applied before returning

### Added

- **Memory Coordinator — Phase 1** (`shared-memory/scripts/coordinator.py`) — all Postgres and Neo4j I/O centralised in a single module embedded in the Hive-Mind Gateway:
  - `asyncpg` connection pool (min 2, max 10) replaces per-call `psycopg2` connections; eliminates the connection-per-save burst problem under concurrent agent writes
  - Per-entity `asyncio.Lock` — concurrent saves to the same entity cluster are serialized; prevents duplicate hub creation under agent-swarm concurrency
  - Embedding with exponential-backoff retry (4 attempts, 0.5 s × attempt) — replaces hard abort; gateway downtime is retried rather than propagated as an error
  - Outbox row written atomically with each `technical_docs` row in a single Postgres transaction — Phase 2 worker drains `neo4j_outbox` asynchronously; ADR-001 cross-DB atomicity risk eliminated from Phase 2 onward
  - Routes: `POST /memory/save` (Postgres-ack, 200 + pg_id), `POST /memory/search` (Tier 3 → Tier 1 → rerank → Neo4j expand), `POST /memory/graph` (raw Cypher), `GET /memory/status/{pg_id}` (outbox state for `?consistency=neo4j` callers)
  - Reranker called directly on port 8071 — avoids circular path through the proxy

- **`memory_bridge.py` — thin HTTP client** — direct `psycopg2` and `neo4j` imports removed; all storage I/O delegated to the coordinator via `httpx`. CLI interface (`save`, `search`, `graph`) is unchanged. `COORDINATOR_URL` env var overrides the default `http://localhost:8888`. `AGENT_ID` env var stamps writes with a caller identity.

- **`hive_mind_proxy.py`** — coordinator started on proxy startup, stopped on clean shutdown. `/memory/*` routes registered before the catch-all proxy route. Two-line change: `attach_coordinator(app, coordinator)` + lifecycle hooks.

- **`asyncpg>=0.29.0`** added to `requirements.txt`; `psycopg2-binary` comment updated to reflect remaining uses.

- **README §3, §11, §12** updated — architecture diagram shows coordinator layer; §11 documents the coordinator HTTP API table; §12 shows the updated save path with per-entity locking, outbox, and Postgres-ack semantics.

### Added

- **Multi-agent schema migration** (`shared-memory/migrations/001_multiagent_schema.sql`) — additive schema changes preparing the storage layer for coordinator-based multi-agent support:
  - `technical_docs` and `community_summaries` gain `agent_id TEXT DEFAULT 'legacy'`, `scope TEXT DEFAULT 'global'`, and `visibility TEXT DEFAULT 'global'` columns with btree indexes. Existing rows are unaffected — defaults preserve current single-agent behaviour.
  - New `neo4j_outbox` table for the coordinator outbox pattern: each pending Neo4j write is committed atomically alongside its `technical_docs` row, then applied asynchronously by the outbox worker. Eliminates the ADR-001 cross-DB atomicity window and makes the system resilient to Neo4j downtime and workstation crashes.
  - Migration is idempotent (`IF NOT EXISTS` throughout) — safe to run multiple times.

- **Migration runner** (`shared-memory/migrations/apply.py`) — thin CLI wrapper: `uv run --with psycopg2-binary python shared-memory/migrations/apply.py [filename.sql]`. Runs all `*.sql` files in order if no filename is given. Reads `PG_CONN` / `PG_PASSWORD` from environment or `.env`.

---

## [0.1.0] — 2026-05-24

### Added

- **Audit logging** — `_append_log()` helper in `memory_bridge.py` (both copies) and `vector-skill.py`. Controlled by two new env vars:
  - `MEMORY_LOG_LEVEL` — `0` (off, default) through `4` (full content copy with size warning)
  - `MEMORY_LOG_PATH` — log directory, defaults to `~/.shared-memory/logs`

- **Per-tool log files with write/rotate separation** — `memory_bridge.log` (CLI tools) and `vector_skill.log` (LM Studio MCP). The architectural decision is to make writing tools **append-only, never rotate**. CLI tools invoke `memory_bridge.py` as separate short-lived OS processes; concurrent appends via `O_APPEND` are atomic on Linux for writes under `PIPE_BUF` (4096 bytes), so individual log lines are safe. Rotation across concurrent processes is not safe — a rename or truncate mid-write from one process corrupts the other's output. Separating the write responsibility (tools) from the rotate/merge responsibility (daemon) eliminates this class of race condition entirely.

- **Daily log merge in the consolidation daemon** — `merge_logs()` function in `consolidation_loop.py`. Triggered once per calendar day on the first 1-second poll of a new day. Uses the logrotate rename pattern: renames source files (writing tools create fresh files on next open), merges entries by timestamp, writes `shared_memory_YYYY-MM-DD.log.gz`. Appends to an existing archive for the same date if present. Handles entries spanning multiple days when the daemon was offline.

  **Why the consolidation daemon, not the proxy:** The Hive-Mind Gateway proxy was the initial candidate for triggering log merge, since it is the other long-running process in the stack. It was rejected because the proxy may be replaced by a lightweight LLM router — it is an infrastructure convenience, not an architectural constant. The consolidation daemon is the right owner: it is the one stable background process whose role is defined at the system level (not tied to any particular agent interface or transport), it already manages the sleep-cycle cleanup loop, and short-lived CLI invocations cannot perform time-based rotation reliably since they exit immediately after each save.

- **`test_logging.py`** — 30 new tests covering `_append_log` level filtering, per-tool routing, content size warnings, `save_artifact` logging integration at each event type, and all `merge_logs` paths.

### Changed

- **Metadata parsing in `save_artifact` — fail-fast replacing silent corruption** — the original code caught all exceptions with a bare `except:` (including `SystemExit` and `KeyboardInterrupt`) and fell back to `{"raw_metadata": metadata_json}`. This silent fallback was an architectural failure: the save would proceed and return `status: success`, but with no `entities` key in the stored metadata, the fact was permanently ineligible for Tier 3 consolidation — with no indication to the caller. The fix replaces the bare `except:` with `except (json.JSONDecodeError, ValueError)` and returns an explicit error instead of falling back. A second check validates that the parsed result is a `dict`: valid JSON that is not an object (e.g. `[1,2,3]`) would previously parse successfully but crash downstream on `m_data.get()`. Both error paths now log at level 2 and return `status: error` immediately.

- **`entities` extraction moved earlier** — `entities = m_data.get("entities", [])` now runs immediately after metadata parsing, before the Postgres insert. Previously it was defined inside the Neo4j `try` block; a Neo4j connection failure before that line left `entities` undefined, causing a `NameError` in the entities warning and success log calls. Moving it earlier ensures it is always in scope.

- **Entities warning in success response** — saves that succeed but include no `entities` now append `WARNING: No 'entities' in metadata — fact stored but ineligible for Tier 3 consolidation.` to the response message (both `memory_bridge.py` and `vector-skill.py`).

- **`.env.example`** — added `MEMORY_LOG_LEVEL` and `MEMORY_LOG_PATH` entries with inline documentation.

- **README** — added §14 Audit Logging; renumbered §14–18 → §15–19; added logging note to §12 (Save Path); updated §17 Testing table; updated §18 Observability open problem.

- **Configurable ontology (`ontology.yaml`)** — all Neo4j label names (`Fact`, `Entity`, `CommunitySummary`, `ReasoningTrace`, `ReasoningStep`) and relationship types (`MENTIONS`, `REPORTS_ON`, `SUMMARIZED_BY`, `NEXT_STEP`) were previously hardcoded as inline strings inside Cypher queries. They are now defined in `ontology.yaml` at the repo root and loaded at startup via `shared-memory/scripts/ontology.py`. Override any value to customise the graph schema for your deployment without touching Python source. Changing `SMEM_ONTOLOGY_PATH` points the loader at a non-default location. The consolidation density threshold (`density_threshold: 5`) is also configurable in the same file.

- **`shared-memory/scripts/ontology.py`** — new shared module. Loads `ontology.yaml`, exposes an `OntologyConfig` dataclass and a module-level `ONT` singleton consumed by `memory_bridge.py` and `consolidation_loop.py`. Falls back to hardcoded defaults if the config file is absent, so existing deployments without `ontology.yaml` are unaffected.

- **`shared-memory/Documentation/schema.md`** — added full `community_summaries` table documentation (was missing): all columns, the exact `metadata` JSONB shape written by the consolidation daemon, the retrieval role (top-1 cosine match prepended as global context), and the append-only growth behaviour. Added configurable-ontology note to both PostgreSQL and Neo4j sections.

---

[0.2.8]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.8
[0.2.7]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.7
[0.2.0]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.0
[0.1.0]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.1.0
