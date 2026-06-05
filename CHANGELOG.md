# Changelog

All notable changes to the Shared Memory Framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Docs

- **Documentation fact-check pass** (`/doc-audit`): reviewed every README chapter and standalone doc against current v0.4.0 code. Corrections:
  - README `--version` example output `0.3.6` → `0.4.0` (§10a, §11) to match `memory_bridge.py` `VERSION`.
  - README §11 coordinator API table `/health` response now lists `rem_daemon` (the gateway has emitted it since v0.4.0).
  - README §13 "The Sleep Cycle" rewritten for the v0.4.0 two-phase **REM/NREM** architecture (it still described the pre-0.4.0 single-stage daemon): REM enrichment (`rem_loop.py` — 120 s poll, batch 5, oldest-first, `applied`-gated, `rem_processed` set last), NREM `rem_processed` gate, and CommunitySummary supersession. Heading + TOC anchor updated.
  - README §14 "Audit Logging" — documented the v0.4.0 `AUDIT_LOG_PATH` REM outbox audit log (was only mentioned in the §19 roadmap): added it to the config table, a dedicated subsection with the JSON-lines format and field table, and clarified it is a separate, REM-daemon-side log from the `MEMORY_LOG_LEVEL` per-save logs. Fixed the dangling `rem_loop.py` docstring pointer to name §14.
  - Auth-introduction version aligned to **v0.3.5** (Phase 2C landed in 0.3.5, hardened through 0.3.6) across README §18/§19 and `SKILL.md`, matching `CHANGELOG`, `SECURITY.md`, and `system-prompt.md`.
  - README §19 schema-migrations row extended to cover migrations 004–006.
  - `SECURITY.md` audit-cadence pointer advanced past the completed v0.4.0 review.
- **New `/doc-audit` workflow** (`.claude/commands/doc-audit.md`): repeatable chapter-by-chapter doc fact-check wired to this repo's source-of-truth files and release guardrails.

---

## [0.4.0] — 2026-06-04

### Added

- **REM/NREM two-phase sleep cycle** (`shared-memory/scripts/rem_loop.py` new, `consolidation_loop.py` modified, `hive_mind_proxy.py` modified): Replaces single-stage consolidation with a two-pass sleep architecture modelled on biological memory consolidation.

  **REM daemon (`rem_loop.py`)** — new background process, auto-started by `hive_mind_proxy.py` alongside the consolidation daemon. On each idle scan (120 s poll, batch of 5):
  - Fetches oldest non-REM Fact nodes from Neo4j (pg_id ASC — clears the historical backlog first).
  - Gates on outbox `status='applied'` — only enriches facts confirmed written to Neo4j.
  - Batch-fetches full Postgres content in one query (a single AUTOCOMMIT connection per cycle, replacing per-operation connection churn).
  - Builds a closed typed-node registry from the graph (`Human`, `AIAgent`, `Project`, `Decision`, `Entity` nodes) — once a name is typed, its label never changes across the batch.
  - Single LLM round-trip per fact: three-part prompt (full text + closed entity set + ontology vocabulary) → produces a paragraph summary (≤5 sentences) and typed entity→relationship assignments.
  - Writes to Neo4j in one session: entity MERGE edges first, Decision extras second, then `SET f.content = summary, f.rem_processed = true` last — so a partial failure never marks a fact processed.
  - For Decision facts: additionally extracts `CONSIDERED`, `REJECTED`, `UNDER_CONDITIONS`, `PRODUCES_INSIGHT` edges on the Decision node.
  - Verifies Fact node consistency (full-string, not prefix) before touching the outbox.
  - Optionally writes outbox row to `AUDIT_LOG_PATH` (JSON-lines) before marking `rem_reviewed`.
  - Sends `pg_notify('new_artifact', pg_id)` to wake NREM after each enriched fact.

  **NREM (modified `consolidation_loop.py`)** — cluster query now requires `AND coalesce(neighbor.rem_processed, false) = true`. Raw (non-REM-enriched) facts are never consolidated directly. Threshold unchanged (5+ rem_processed unconsolidated facts per entity hub).

  **CommunitySummary supersession** — after every NREM consolidation, any existing active `community_summary` row whose `source_pg_ids` is a strict subset of the new summary's `source_pg_ids` is marked `superseded = true` in Postgres and linked with a `(new)-[:SUPERSEDES]->(old)` edge in Neo4j. Tier 3 search in `coordinator.py` filters `WHERE NOT superseded` — stale summaries are never surfaced.

  **`hive_mind_proxy.py`** — adds `_start_rem_daemon()`, `_watchdog_rem_daemon()` (same circuit-breaker logic as the consolidation watchdog). `/health` now reports `rem_daemon` status. Drain sequence stops both daemons cleanly.

- **Ontology additions** (`ontology.yaml`, `ontology.py`): Four new REM-enrichment relationship types used for decision capture in the knowledge graph:
  - `PRODUCES_INSIGHT`: `(Fact/Decision)-[:PRODUCES_INSIGHT]->(Entity)` — insight or knowledge this node generates
  - `UNDER_CONDITIONS`: `(Decision)-[:UNDER_CONDITIONS]->(Entity)` — constraints or conditions that bound the decision
  - `CONSIDERED`: `(Decision)-[:CONSIDERED]->(Entity)` — alternatives evaluated
  - `REJECTED`: `(Decision)-[:REJECTED]->(Entity)` — alternatives explicitly ruled out

- **Migration 006** (`shared-memory/migrations/006_rem_supersession.sql`):
  - `superseded BOOLEAN NOT NULL DEFAULT false` added to `community_summaries`; partial index `WHERE NOT superseded` keeps retrieval scans fast as superseded history accumulates.
  - Source normalisation backfill — historical pre-auth source variants normalised to canonical agent names: `claude_code`, `claude-code`, `claude_session`, `claude_code_fix`, `claude_code_session`, `claude_code_verification`, `claude-sonnet-4-6`, `design_session`, `architectural_hardening`, `architectural_fix` → `"claude"`; `workstation-assistant` + null-source rows → `"lm_studio"`; `design_session_cloe` left unchanged.

- **17 new tests** (`tests/test_rem_loop.py`): `_safe_label`, `_build_entity_registry`, `_resolve_rel` pure helpers; LLM mock output shape (plain fact vs decision); oldest-first ordering assertion; `rem_processed=true` SET last invariant; full-string consistency check; NREM `rem_processed` guard assertion; Tier 3 supersession filter assertion. **Total: 130 tests.**

### Changed

- **NREM cluster query** (`consolidation_loop.py`): Added `AND coalesce(neighbor.rem_processed, false) = true` to the density query — only REM-enriched facts participate in NREM synthesis.
- **Tier 3 search** (`coordinator.py`): `community_summaries` query now filters `WHERE NOT superseded`.
- **Outbox status lifecycle**: New terminal status `rem_reviewed` — REM writes this after verifying Fact consistency. Rows at `rem_reviewed` are safe to prune (handled by future `pruning_loop.py`).
- **`AUDIT_LOG_PATH` env var**: Set to a writable path to enable JSON-lines outbox audit log before `rem_reviewed` marking. Default: disabled.

### Fixed (post-release review — same day)

- **A2 — Search hard-crash without migration 006** (`coordinator.py`): `WHERE NOT superseded` referenced the new column unconditionally. Operators who restarted the gateway after a `git pull` without running `apply.py` got HTTP 500 on every vector-backed search. Fixed by wrapping the Tier 3 query in try/except and falling back to the unsupervised query with a migration warning.

- **A3 — CommunitySummary / ReasoningTrace in REM `_KNOWN_LABELS`** (`rem_loop.py`): Those node types are keyed by `pg_id`, not `name`. Including them in the set used to build `MERGE (e:{label} {name: n})` patterns could create structurally incompatible phantom nodes. Removed `ONT.community_summary`, `ONT.reasoning_trace`, `ONT.reasoning_step` from `_KNOWN_LABELS`. Practical risk was low because the closed-set query does not fetch those labels; removed as defence-in-depth.

- **A1 — NREM silence after upgrade invisible to operators** (`consolidation_loop.py`): After upgrading from v0.3.x, all existing facts have `rem_processed=NULL` — NREM correctly waits for REM enrichment but logged nothing. Added an INFO log explaining the expected silence window and pointing operators to `/health` → `rem_daemon`.

- **A4 — Deferred REM batch logged at DEBUG only** (`rem_loop.py`): When the outbox gate deferred all batch candidates, the reason was invisible at the default log level. Changed to INFO so operators see "N fact(s) deferred (outbox not yet applied)".

### Added (post-release)

- **Write quiesce for remote agents** (`rem_loop.py`): REM now skips its enrichment cycle if any fact was saved within `WRITE_QUIESCE_SEC` seconds (default 30, configurable via env var). Prevents REM's `pg_notify` calls from resetting NREM's idle timer during active write sessions from remote agents (e.g. chromebook-antigravity). See README §REM daemon and `.env.example`.

- **Skill sync script** (`shared-memory/scripts/sync_skills.sh`): executable shell script that copies canonical sources to all agent install paths. Run after every code change: `bash shared-memory/scripts/sync_skills.sh`.

### Upgrade path (from v0.3.x)

**Expected NREM silence after upgrade:** all existing facts have `rem_processed=NULL`.
NREM waits for REM to enrich each cluster before synthesising. At batch_size=5 and poll=120s,
a graph with ~80 facts clears the backlog in ~30 minutes. Monitor with:
```bash
# Check how many facts still need REM processing
curl http://localhost:8888/health  # rem_daemon: running
```

Users upgrading from any v0.3.x release must apply migration 006 before restarting the gateway:

```bash
# From the repo root
PG_PASSWORD=<your_password> uv run --with psycopg2-binary python shared-memory/migrations/apply.py
```

Migration 006 adds the `superseded` column — the coordinator will fail to serve searches correctly without it. The source normalisation in the same migration is idempotent (safe to re-run). After migrating, restart the gateway; `rem_loop.py` will start automatically.

---

## [0.3.6] — 2026-06-01

### Fixed

- **Relative path in SKILL.md broke remote installs (Bug 1 — script not found):** All 20 CLI commands used bare `scripts/memory_bridge.py`. Skill runners execute from the user's project directory, not the skill directory — the script was silently not found on any non-local install. All commands now use the canonical absolute path `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` with an AI-instruction block providing the per-agent prefix substitution table.
- **Relative path broke token loading (Bug 2 — same root cause):** `memory_bridge.py` resolves `.env` files via `os.path.abspath(__file__)`. With a relative invocation path, `__file__` resolved against CWD, pointing `.env` lookups at the wrong directory and silently dropping the agent token. Fixed by the same absolute-path change.

### Changed

- **Removed `client.env` universal token fallback:** `~/.config/shared-memory/client.env` removed from `memory_bridge.py` (both branches), `generate_tokens.py`, SKILL.md, `.env.example`, and README. Agent tokens are identity — the coordinator stamps verified `source` on every saved artifact. A shared fallback token collapses all agent attribution in the knowledge graph. Per-agent skill `.env` is now the only supported method.
- **Antigravity CLI (`agy`) added as primary replacement for Gemini CLI.** Both tools share `~/.gemini/skills/`. Gemini CLI marked legacy in SKILL.md and README. `chromebook-antigravity` token documented for remote instances of `agy`.

---

## [0.3.5-post] — 2026-05-29

### Fixed

- **Auth self-loop in coordinator embedding calls** (`coordinator.py`): `EMBED_URL` was pointing to `:8888` (the proxy the coordinator lives inside), causing internal embedding calls to hit the auth middleware — which has no token and returns 401, aborting every save. Changed to `:8070` direct, consistent with `RERANK_URL` which already used `:8071` direct for the same reason. External agents still route embeddings through `:8888` and must authenticate.

- **Dotenv `or`-chain only loaded one file** (`memory_bridge.py`, skill copy): The three-tier fallback used Python `or` chaining — once `find_dotenv()` returned the project `.env` (which has `AGENT_TOKENS` for the gateway but not `AGENT_TOKEN` for the agent), the skill `.env` and `~/.config/shared-memory/client.env` were never reached. Changed to a `for` loop with `load_dotenv(..., override=False)` so all three sources contribute and the first definition of each variable wins.

- **`python-dotenv` not available in bare `uv run --with httpx`** (`memory_bridge.py`, skill copy): When agents run `uv run --with httpx python scripts/memory_bridge.py` without `--with python-dotenv`, the `try: from dotenv import ...` block hit `ImportError` silently and no `.env` was ever loaded, causing `_auth_headers()` to return `{}` and every call to get 401. Added a plain-Python fallback in the `except ImportError` block that manually parses the skill-root `.env` (one directory above `scripts/`) and `~/.config/shared-memory/client.env` — auth tokens are loaded with no dependencies.

- **Missing `_auth_headers()` on reranker and health-check calls** (`vector-skill.py`): `_auth_headers()` was added to `save_decision()` and `save_retrospective()` in v0.3.5 but missed three HTTP call sites that go through port 8888: the reranker call in `hybrid_search_and_rerank()` and both health-check probes in `check_memory_health()`. All six `client.post()` call sites in `vector-skill.py` now pass `_auth_headers()`.

- **Gemini CLI skill was at v0.3.3** (pre-coordinator): The Gemini skill directory at `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` had never been synced after the coordinator refactor. It still had direct `import psycopg2` and `import neo4j` at the top, no auth support, and crashed on every call with `ModuleNotFoundError`. Synced to v0.3.5 — all five skill install locations now verified identical to canonical source after every change.

### Documentation

- **README `uv run` commands**: Added `--with python-dotenv` to every `memory_bridge.py` invocation. Added full dependency list (`--with asyncpg --with neo4j --with httpx`) to gateway startup command. Added "Token search order" section explaining the three-tier dotenv fallback. Fixed smoke-test command (had literal `\n` instead of a real line break).

- **SKILL.md** (all copies): Added `--with python-dotenv` to all `uv run` commands. Updated Authentication Setup section with dotenv search order, `curl /health` verify step, 401 error hint.

- **Complete Cycle section** (README §11a): End-to-end walkthrough — Claude Code saves a decision, Gemini CLI saves a plain fact, consolidation synthesises, Grok retrieves both (annotated response showing Tier-3 + Tier-1 + `graph_context`), named query shortcuts, retrospective closes the loop, LM Studio MCP equivalents.

- **Security review** (v0.3.5 post-release): All three candidate findings filtered as false positives — Cypher injection (regex strips single quotes and backslashes, no breakout possible), write-Cypher guard bypass (Neo4j read-only session is a second independent layer), cross-DB atomicity in consolidation (data integrity issue, no security exploitation path).

---

## [0.3.5] — 2026-05-29

### Security

- **Phase 2C — Agent token authentication** (`coordinator.py`, `hive_mind_proxy.py`, `memory_bridge.py`, `vector-skill.py`): The Memory Coordinator now enforces `Authorization: Bearer <token>` authentication on all routes. Any process that cannot present a registered token is rejected with HTTP 401. This closes the last open security finding — previously any localhost process could read/write shared memory and claim any agent identity.

  - **`coordinator.py` — `_load_agent_tokens()` + `auth_middleware`**: Parses `AGENT_TOKENS` env var (`name:token,...`) into a token→agent mapping. Middleware is DEFAULT DENY — every route except `_UNPROTECTED_PATHS = {"/health"}` requires a valid token. Trailing-slash normalisation (`/health/` passes). Duplicate-token guard: if two agents share a token the second mapping is discarded and a WARNING is logged. `source` in saved metadata is forcefully overwritten with the server-verified agent name — clients cannot spoof identity.

  - **`hive_mind_proxy.py`**: `auth_middleware` registered globally on the aiohttp app — applies to all routes including the catch-all proxy (which would otherwise be an unauthenticated SSRF relay if `PROXY_BIND=0.0.0.0` is used). `/health` reports `auth_required` flag.

  - **`memory_bridge.py` + skill copy**: `_auth_headers()` reads `AGENT_TOKEN` and injects `Authorization: Bearer` on all coordinator calls. Three-tier dotenv fallback: `find_dotenv()` → script-adjacent `.env` → `~/.config/shared-memory/client.env`. Explicit 401 handling with clear error message and hint.

  - **`vector-skill.py`**: Same `_auth_headers()` pattern. Replaces `"Bearer none"` placeholder. Auth headers added to `save_decision()` and `save_retrospective()` coordinator calls.

  - **`scripts/generate_tokens.py`** (new): Bootstrap utility — generates cryptographically random `tok_` prefixed tokens for all 6 agents and prints ready-to-paste `.env` lines.

  - **Backward compatible**: `AGENT_TOKENS` unset → auth disabled, all requests pass through (no behaviour change for existing installs).

  - **22 new tests** — token loading, middleware DEFAULT DENY, allowlist, valid token, rejection cases, source overwrite, `_auth_headers()`, 401 response handling. **Total: 113 tests.**

---

## [0.3.4] — 2026-05-29

### Security

- **S1 (HIGH) — `/memory/graph` read-only enforcement** (`coordinator.py`): Neo4j session for `handle_graph` now opens with `default_access_mode="READ"`. Driver-level write enforcement is layered on top of the existing `_WRITE_CYPHER` keyword regex — a regex-bypassing query can no longer execute writes at the Neo4j protocol level.

- **S2 (HIGH) — Async consolidation daemon** (`consolidation_loop.py`): Migrated from synchronous `GraphDatabase` driver and `psycopg2` calls inside `async def` to `AsyncGraphDatabase` + `loop.run_in_executor()`. The event loop no longer blocks during Neo4j or Postgres I/O, preventing `LISTEN/NOTIFY` signal drops under write bursts. `connect_timeout=5` added to all `psycopg2.connect()` calls.

- **S3 (HIGH) — TOCTOU fix in `handle_retrospective`** (`coordinator.py`): The `SELECT` existence check and `INSERT INTO neo4j_outbox` are now wrapped in a single `conn.transaction()` with `SELECT ... FOR SHARE`. A concurrent delete of the target row between check and insert can no longer produce a dangling outbox entry and a silent missing `HAD_OUTCOME` edge.

- **S4 (MEDIUM) — `--project` filter in named query templates** (`memory_bridge.py`): The `WHERE p.name CONTAINS '...'` clause appended directly after `OPTIONAL MATCH ... (p:Project)` was parsed as an inline WHERE (filtering what value `p` gets, not which rows return). Added `WITH d, [vars], p` before the project WHERE in `who-decided`, `agent-decisions`, and `why-to-check` — the filter now correctly excludes decisions not linked to the specified project.

- **S5 (MEDIUM) — Dead embedding property on Neo4j nodes** (`vector-skill.py`): `f.embedding`, `t.task_embedding`, and `s.embedding` removed from all three Neo4j write calls in the LM Studio MCP path. The property was never used in any Cypher query (all similarity search goes through `pgvector`) and consumed ~8 KB of Neo4j heap per node.

- **S6 (MEDIUM) — Audit log OSError surfaced to stderr** (`memory_bridge.py`, `vector-skill.py`): `_append_log` now catches `OSError` (disk full, permission denied) and prints a warning to `stderr` before the bare `except Exception: pass` fallback. Disk/permission failures are no longer silent.

- **S7 (MEDIUM) — Outbox double-processing on concurrent restart** (`coordinator.py`): `_drain_outbox` now atomically claims rows with `UPDATE ... SET status='in_progress' WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING ...` before releasing the lock. A second coordinator instance SKIP LOCKs `in_progress` rows. `start()` resets any `in_progress` rows (crash survivors) back to `pending` on startup. The failure-path update no longer conditions on `AND status='pending'`, correctly resetting claimed-but-failed rows.

---

## [0.3.3] — 2026-05-29

### Added

- **Named query templates — Phase D** (`shared-memory/scripts/memory_bridge.py`, `shared-memory-skill/shared-memory/scripts/memory_bridge.py`, `tests/test_memory_bridge_query.py`): Converts the provenance graph from a queryable archive into a usable pre-task protocol — four named shortcuts so agents never need to write raw Cypher for standard provenance questions.

  - **`memory_bridge.py query <template> [filters]` subcommand**: Routes through the existing `query_graph()` → `/memory/graph` coordinator path. No new endpoint, no coordinator changes.

  - **Four templates**:
    - `who-decided [--title TEXT] [--project TEXT]` — returns Decision + Human + AIAgent + Project attribution chain.
    - `agent-decisions [--assisted-by TEXT] [--project TEXT]` — all decisions an AI agent assisted with.
    - `retrospectives [--rating TEXT]` — all HAD_OUTCOME records, optionally filtered by rating.
    - `why-to-check --title TEXT [--project TEXT]` — HAD_OUTCOME records for a given decision topic; `--title` is required (without a topic the result is unscoped). **Intended as the standard pre-task check**: run before starting work in any area with prior decisions.

  - **`_build_query(template, args) -> str`** pure function: builds the Cypher string from template name and parsed args. Filter values are scrubbed with `re.sub(r"[^A-Za-z0-9 _.-]", "", ...)` before interpolation — prevents quote-escape injection. OPTIONAL MATCH + WHERE is used for joined filters (project, assisted_by) so rows without the optional edge are still returned (p/a is NULL, not excluded).

  - **Raw Cypher path preserved**: the existing `graph` subcommand is unchanged and explicitly documented in SKILL.md alongside the named shortcuts. Custom traversals, multi-hop paths, and cross-entity queries still use `memory_bridge.py graph "<cypher>"`.

  - **`shared-memory/SKILL.md` and `shared-memory-skill/shared-memory/SKILL.md`** — Task 3 restructured: named shortcuts section (with `why-to-check` trigger) + raw Cypher section (with read-only enforcement note). No new task section added; every added line carries unique information not present elsewhere.

  - **7 new tests** (`tests/test_memory_bridge_query.py`): pure-function shape tests for all four templates, sanitisation check (`;` and `'` stripped), unknown-template exit, and CLI integration (mock `query_graph`, assert called with non-empty Cypher). Total: **91 tests passing**.

---

## [0.3.2] — 2026-05-29

### Added

- **Retrospective layer — Phase C** (`shared-memory/scripts/coordinator.py`, `shared-memory/scripts/memory_bridge.py`, `vector-skill.py`, `shared-memory-skill/shared-memory/scripts/memory_bridge.py`): Closes the Why-To loop — agents can now record whether a past decision held up, enabling retrospective queries before executing new tasks in the same area.

  - **`POST /memory/retrospective` endpoint** (`coordinator.py`): Accepts `{pg_id, rating, notes, date?, agent_id?}`. Verifies the target `pg_id` exists in `technical_docs`, then writes to `neo4j_outbox` with `type=retrospective`. No new `technical_docs` row — retrospectives do not pollute semantic search.

  - **`_apply_retrospective_outbox_row()`** (`coordinator.py`): Outbox worker method. Issues `MATCH (d:Decision {pg_id}) CREATE (d)-[:HAD_OUTCOME {rating, date, notes}]->(d)` — a self-loop per call. Multiple retrospectives per Decision are allowed; the Why-To query uses `ORDER BY o.date DESC` to surface the most recent.

  - **`build_retrospective_payload()` + `save_retrospective_artifact()`** (`memory_bridge.py`): Pure helper and async HTTP client for `POST /memory/retrospective`. Pattern mirrors `build_decision_metadata()` / `save_artifact()` from Phase B.

  - **`memory_bridge.py save_retrospective` subcommand**: Flags: `--pg-id` (required, int), `--rating` (required), `--notes` (required), `--date` (optional ISO, default today), `--source` (optional, default `$AGENT_ID`).

  - **`vector-skill.py save_retrospective` MCP tool**: LM Studio can record retrospectives through the same coordinator path.

  - **`shared-memory-skill` Gemini copy updated**: `build_retrospective_payload()`, `save_retrospective_via_coordinator()`, and `save_retrospective` subcommand added.

  - **`datetime` import added** to `coordinator.py` (was missing; required by `handle_retrospective`).

  - **10 new tests**: 7 in `tests/test_memory_bridge_retrospective.py` (new file — pure helper shape, date default, explicit date, source default, source override, CLI forwarding, CLI missing-flag exit) and 3 additions to `tests/test_coordinator.py` (outbox dispatch, HAD_OUTCOME Cypher, 400 on missing fields). Total: **84 tests passing**.

  - **`shared-memory/SKILL.md`** (both copies): Task 5 — Save a Retrospective added with CLI, MCP, and Why-To query examples.

  - **`shared-memory/Documentation/schema.md`**: Retrospective write protocol section added under `HAD_OUTCOME` relationship row.

---

## [0.3.1] — 2026-05-28

### Added

- **Retrieval visibility** (`coordinator.py`): search results now include `tier` ("fact" | "community_summary"), `score_normalized` (sigmoid of raw reranker logit → [0, 1]), `matched_entities` (intersection of query string against `metadata["entities"]`), and `graph_context` as a structured list of `{rel_type, name, label}` objects instead of an opaque pipe-separated string. Keyword-fallback results carry the same shape.

- **Consolidation history** (`consolidation_loop.py`, migration `004_summary_history.sql`): `community_summaries` gains a `summary_history JSONB NOT NULL DEFAULT '[]'` column. On every `ON CONFLICT DO UPDATE`, the outgoing `content`, `source_pg_ids`, and `timestamp` are appended (capped at 20 entries) before the row is overwritten. Enables drift auditing without a temporal schema.

- **`source_ref` lineage convention** (`coordinator.py` outbox, `schema.md`, both `SKILL.md` copies): agents may include `"source_ref"` in metadata to record the sub-document origin of a fact (e.g. `"design-doc.pdf#p12"`, `"meeting-2026-05-15.mp4@00:04:32"`). Propagated through coordinator to `cypher_params`; outbox worker stores it as `Fact.source_ref` property in Neo4j.

- **14 new tests** (`tests/test_coordinator.py`): `_sigmoid()` (4 tests), `_matched_entities()` (6 tests), `source_ref` outbox propagation (2 tests), search response shape — `tier` / `score_normalized` / `matched_entities` / `graph_context` list (2 tests). Total: 74 tests passing.

- **ApertureData reference + three diagnostic tests** (`README.md §1`): Vishakha Gupta's *AI Memory & Cognition: The Architect's Playbook* (May 2026) attributed in §20 References. Three diagnostic questions (Retrieval · Consolidation · Lineage) asked and answered with current implementation state at the end of the Vision section. Updated with every release.

### Fixed

- **`schema.md` inaccuracy**: `community_summaries` "Growth behaviour" section incorrectly stated "appends a new row" per cycle. The code applies `ON CONFLICT DO UPDATE` — one row per entity, replaced. Documentation now matches the code.

---

## [0.3.0] — 2026-05-28

### Added

- **Decision shortcut — Phase B** (`shared-memory/scripts/memory_bridge.py`, `vector-skill.py`, `tests/test_memory_bridge_decision.py`, `tests/test_vector_skill.py`): Low-friction `save_decision` command and MCP tool so agents don't need to hand-craft the full `type=decision` JSON payload.

  - **`memory_bridge.py save_decision` subcommand** — accepts named flags (`--title`, `--decided-by`, `--project`, `--rationale`, `--source`, `--assisted-by`, `--alternatives`, `--confidence`, `--entities`). Comma-separated strings for list fields. Builds the correct `type=decision` metadata shape via `build_decision_metadata()` and forwards to the coordinator. Required flags: `--title`, `--decided-by`, `--project`, `--rationale`; missing flags print usage and exit non-zero.

  - **`vector-skill.py save_decision` MCP tool** — individual typed parameters instead of raw JSON. Routes through the coordinator (HTTP to port 8888) so the Decision outbox path and PROV-O subgraph write are handled consistently. Required: `title`, `decided_by`, `project`, `rationale`, `source`. Optional: `assisted_by`, `alternatives`, `confidence`, `entities` (all comma-separated).

  - **`build_decision_metadata()` pure helper** (`memory_bridge.py`) — separates metadata construction from I/O for clean unit testing. Returns `(content_str, metadata_dict)`.

  - **`tests/test_memory_bridge_decision.py`** — 7 new tests covering shape, optional fields, ISO date, empty commas, source default, CLI forwarding, and missing-flag exit code.

  - **`tests/test_vector_skill.py`** — 3 new tests: success (correct payload + pg_id), coordinator unreachable (error references `hive_mind_proxy.py`), 400 error surfaced to caller.

  - **SKILL.md** (both locations): Task 4 updated — `save_decision` shortcut is now the recommended path; MCP tool noted; raw-JSON path marked as legacy.

  - **`shared-memory-skill/shared-memory/scripts/memory_bridge.py`** (Gemini copy): `save_decision` action added, routes through coordinator.

- **Codex CLI integration** (`AGENTS.md`, `shared-memory/SKILL.md`, `shared-memory-skill/shared-memory/SKILL.md`, `AGENT.md`, `README.md`): OpenAI Codex CLI documented and supported as a fifth skill-based agent.

  - **`AGENTS.md` (new)** — Codex CLI project context file (their `CLAUDE.md` equivalent); read automatically before each Codex session. Contains architecture, commands, key invariants, and a `$shared-memory` invocation note.

  - **SKILL.md YAML frontmatter** — both `shared-memory/SKILL.md` and `shared-memory-skill/shared-memory/SKILL.md` now carry `name` and `description` frontmatter required by Codex CLI for implicit skill matching.

  - **Agent table in SKILL.md Overview** — "agents currently integrated" section upgraded from a prose list to a summary table covering all five agents (Claude Code, Grok, Codex CLI, Gemini CLI, LM Studio) with invocation syntax and install path.

  - **`AGENT.md`** — Codex CLI row added to agent access split table.

  - **`README.md`** — Codex CLI badge; §1 agent overview; full §10 setup section (install path, explicit `$shared-memory` invocation, implicit invocation via frontmatter description matching, `AGENTS.md` note); §11 agent access table row.

---

## [0.2.9] — 2026-05-28

### Added

- **Decision provenance layer — Phase A** (`ontology.yaml`, `shared-memory/scripts/ontology.py`, `shared-memory/scripts/coordinator.py`, `tests/test_coordinator.py`): PROV-O-inspired provenance nodes and relationships for recording architectural and design decisions with full attribution context.

  - **`ontology.yaml`** — 6 new provenance labels (`Decision`, `Human`, `AIAgent`, `Project`, `Activity`, `Milestone`) and 8 provenance relationships (`WAS_ATTRIBUTED_TO`, `WAS_ASSISTED_BY`, `PROJECT_OF`, `WAS_GENERATED_BY`, `ACTED_ON_BEHALF_OF`, `SUPERSEDES`, `INFORMED_BY`, `HAD_OUTCOME`). All configurable via the existing `ontology.yaml` override mechanism.

  - **`coordinator.py` ingress validation** — saves with `metadata["type"] == "decision"` are validated at ingress before any DB write. Missing required fields (`decided_by`, `project`, `rationale`) return HTTP 400 with a descriptive error listing the missing fields. Plain fact saves are unaffected.

  - **`coordinator.py` outbox dispatch** — `_apply_outbox_row` routes `type=decision` saves to the new `_apply_decision_outbox_row` method. Writes a `Decision→Human→Project→AIAgent` subgraph in a single Neo4j session with `WAS_ATTRIBUTED_TO`, `PROJECT_OF`, `WAS_ASSISTED_BY`, and `MENTIONS` edges. `FOREACH` (not `UNWIND`) used for `assisted_by` and `entities` lists — handles empty lists safely without dropping the write.

  - **`tests/test_coordinator.py`** — 8 new tests: ingress validation (missing all required fields → 400; single missing field named in error); plain fact regression; valid decision passes validation; outbox dispatch routing to decision path and not to it for plain facts; Neo4j write shape (correct labels + relationship types + kwargs); empty `assisted_by` does not crash.

- **Schema documentation** (`shared-memory/Documentation/schema.md`): Full Neo4j section replaced with provenance labels table (Phase A), provenance relationships table with PROV-O patterns and meanings, Cypher query examples (who-decided, agent contributions, Why-To loop), and the decision save protocol JSON example.

- **All 5 SKILL.md locations**: Task 4 — Decision Provenance — added to every agent skill file with CLI save example, required fields list, three-step write flow, and Cypher query template for retrieving saved decisions.

- **README** (`README.md`): "What we are building toward" vision subsection with target question and answer shape; "Saving everything vs. saving what matters" with concrete queryable examples (who/when/why/conditions/outcome) and counter-examples (what stays in Git); roadmap updated — Phase A marked done, Phases B–E (CLI/MCP tools, retrospectives, named query templates, pruning) listed as planned.

- **CLAUDE.md** — test command updated to include `--with asyncpg --with aiohttp` (required since `coordinator.py` imports both at module level).

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

[0.2.9]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.9
[0.2.8]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.8
[0.2.7]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.7
[0.2.0]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.0
[0.1.0]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.1.0
