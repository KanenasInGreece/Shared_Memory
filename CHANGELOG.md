# Changelog

All notable changes to the Shared Memory Framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

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

[0.1.0]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.1.0
