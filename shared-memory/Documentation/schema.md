# Shared Memory Schema

> Label and relationship names are configurable via `ontology.yaml` at the repo root (override `SMEM_ONTOLOGY_PATH` to point elsewhere). All names shown here are the defaults.

## PostgreSQL (Semantic Memory)

### `technical_docs` — Tier 1 (Episodic)

Holds every artifact saved by any agent. This is the authoritative fact store.

| Column | Type | Notes |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | Auto-incrementing row ID; used as `pg_id` in Neo4j `Fact` nodes |
| `content` | `TEXT NOT NULL` | Full text of the saved artifact |
| `metadata` | `JSONB` | Caller-supplied context: `source`, `type`, `timestamp`, `entities`, etc. |
| `embedding` | `vector(1024)` | BGE-M3 embedding routed through the gateway on port 8888 |
| `content_hash` | `TEXT UNIQUE` | SHA-256 of `content`; `ON CONFLICT DO UPDATE` makes saves idempotent |
| `agent_id` | `TEXT NOT NULL DEFAULT 'legacy'` | Identity of the writing agent; `'legacy'` for pre-coordinator rows |
| `scope` | `TEXT NOT NULL DEFAULT 'global'` | Namespace for access control; `'global'` = visible to all agents |
| `visibility` | `TEXT NOT NULL DEFAULT 'global'` | Read policy, **enforced on every `/memory/search` read** (v0.6.2): `'global'` = all callers; `'private'` = only the owning `agent_id`; `'scope'` = only a caller asserting the matching `scope`. Anonymous callers (no verified identity) see `'global'` only — fail closed. Gates Tier-1 and Tier-3 alike, so a private fact's community summary never leaks. |
| `superseded` | `BOOLEAN NOT NULL DEFAULT false` | Soft-supersede flag. Set when (a) a retrospective with `rating="reversed"` lands on a decision row (pg_id 276), or (b) a **fact** is superseded by a correction (`save --supersedes`) or retracted (`POST /memory/supersede`) — decision 381. Mirrored as `superseded = true` on the graph `:Fact`/`:Decision` node. Tier-1 search, REM/NREM selection, and the working-set census all exclude superseded rows; the row is kept (provenance, compare/contrast). Added by migration 009. |
| `superseded_by` | `INTEGER REFERENCES technical_docs(id) ON DELETE SET NULL` | The successor fact when this row was superseded by a correction; `NULL` for a live row, a bare retract, or a reversed decision. Powers the retrieval-time `stale_sources: [{old, superseded_by}]` annotation (decision 384) as a cheap join — no Neo4j hop. Added by migration 013. |
| `created_at` | `TIMESTAMPTZ DEFAULT now()` | Server-stamped creation time (migration 015) — temporal provenance for **reranker recency** (`id` is creation *order*, not time-magnitude). Backfilled from `neo4j_outbox.created_at` where recoverable; `NULL` = unknown (consolidated/legacy rows whose outbox row was already deleted), which the reranker treats as "old / no recency boost". Index `technical_docs_created_at_idx`. |
| `rem_timing` | `JSONB` | Durable per-call REM latency summary (migration 019, decisions 570/571; comment re-issued by migration 037, fact:1621), built by `dream_telemetry.py`'s `call_timing_summary` and persisted by `rem_loop.py`'s `_write_rem_timing`: `{service_ms,wall_ms,contention_ms,poll_ms,model,backend,batch_size,prompt_chars,completion_tokens,tok_s_wall,ts}`. `service_ms` (prompt_ms + predicted_ms from llama.cpp's own `timings` block) is pure inference = MODEL + HARDWARE, load-invariant; `wall_ms` is the client-observed call time; `contention_ms = max(0, wall_ms - service_ms)` is the CAPACITY signal (queueing behind a busy backend); both NULL for a backend whose response carries no `timings` block. `model`/`backend`/`batch_size`/`prompt_chars`/`ts` are the call's own envelope, never llama.cpp `timings` output: `model` is the response's own top-level `model` field, `backend`/`batch_size`/`prompt_chars` are supplied by the CALLER (gateway pool route, fold batch, grounding-prompt size), and `ts` is stamped by the function itself (`time.time()`) — none of the five come from inside `timings`. `completion_tokens`/`tok_s_wall` are the OpenAI-compatible fallback, read from the response's `usage.completion_tokens` whenever it is present (`tok_s_wall = completion_tokens / wall_s` is an EFFECTIVE rate including TTFT and network time, unlike `service_ms`'s pure-inference rate) — this is the fallback rate for a backend that returns no `timings` block, but it is filled unconditionally whenever `usage.completion_tokens` is present, so it MAY also appear beside a non-null `service_ms`/`contention_ms` on a response that carries both. `poll_ms` is `created_at` → REM pickup (daemon cadence). Survives `neo4j_outbox` row deletion on NREM consolidation, so it is the only durable record of per-call REM cost once a fact has finished dreaming. |

**Indexes:** `technical_docs_embedding_idx` — `hnsw (embedding vector_cosine_ops)` (migration 010); btree indexes on `agent_id`, `scope`, `visibility`; partial `technical_docs_superseded_by_idx` on `superseded_by WHERE superseded_by IS NOT NULL`; `technical_docs_project_expr_idx` — btree on the expression `(metadata->>'project')`, the column the `--project` search filter actually compares against (migration 036); `technical_docs_domains_gin_idx` — GIN on the expression `(metadata->'domains')`, for the `--domain` filter's `?|` containment check (migration 036, added by analogy to the project index — unmeasured)

**`source_ref` convention (optional metadata key):** agents may include `"source_ref"` in metadata to record the sub-document origin of a fact. The coordinator passes it through unchanged; the outbox worker stores it as a property on the `Fact` Neo4j node. No schema enforcement — supply it when the source is a specific document location.

| Example value | Meaning |
|---|---|
| `"design-doc.pdf#p12"` | PDF page 12 |
| `"meeting-2026-05-15.mp4@00:04:32"` | Video timestamp |
| `"screenshot-2026-05-20.png"` | Image file |
| `"CLAUDE.md#L45-50"` | File line range |

Query example: `MATCH (f:Fact) WHERE f.source_ref IS NOT NULL RETURN f.pg_id, f.source_ref LIMIT 20`

---

### `community_summaries` — Tier 3 (Semantic)

Written exclusively by the consolidation daemon. Never written directly by agents.

⚠ **Two rows are NOT the same shape any more (Dreaming Cycle Plan to v2 §3, C4).**
A **thematic** row's `content` is a **Zettelkasten index** — a deterministic,
zero/low-inference concatenation of each constituent fact's own tight text
(`fold_record_line` over `coalesce(rem_summary, content)`); it is **never**
LLM-synthesised. An **insight** row's `content` **is** LLM-synthesised — the
causal chain over its ordered judgements, extracting strictly each
judgement's own Title and Rationale (its `content` verbatim), excluding
`CONSIDERED`/`REJECTED`/`UNDER_CONDITIONS` and everything else (confidence,
alternatives, grounding-edge detail) from the embedded text — all of that is
deferred to the graph walk via `metadata.cypher_query` below.

| Column | Type | Notes |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | Referenced as `pg_id` on the matching `CommunitySummary` node in Neo4j |
| `content` | `TEXT NOT NULL` | Thematic: a deterministic Zettelkasten index (§3.1, zero/low inference — never LLM output). Insight: an LLM-synthesised causal chain (§3.2) over the ordered judgement component. |
| `metadata` | `JSONB` | Written by the daemon — see structure below |
| `embedding` | `vector(1024)` | BGE-M3 embedding of the synthesised `content`; used for top-1 retrieval |
| `source_pg_ids` | `INTEGER[]` | IDs of `technical_docs` rows that contributed to this summary. Added by migration 003; back-filled from `metadata` for existing rows. Enables `WHERE $fact_id = ANY(source_pg_ids)` provenance queries without JSON parsing. |
| `summary_history` | `JSONB NOT NULL DEFAULT '[]'` | Append-only array of previous summaries, capped at 20. Written by the daemon on every `DO UPDATE` — the outgoing content, `source_pg_ids`, and `timestamp` are pushed to the array before the row is overwritten. Enables drift auditing without a temporal schema. Added by migration 004. |
| `superseded` | `BOOLEAN NOT NULL DEFAULT false` | Set to `true` when another summary's `source_pg_ids` is a strict superset of this row's `source_pg_ids` — the newer summary subsumes this one. Retrieval (`coordinator.py`) always filters `WHERE NOT superseded`. Partial index `community_summaries_active_idx` keeps this scan fast. Added by migration 006. |
| `superseded_at` | `TIMESTAMPTZ` | When this row was retired. `NULL` for every row superseded before migration 031 — honest: its reason/timestamp was never recorded, not backfilled. |
| `superseded_reason` | `TEXT` | `'coverage'` — Mechanism A, `supersede_covered_summaries` (subset/equal `source_pg_ids`). `'lineage'` — Mechanism B, `retire_invalidated_summaries` (Dreaming Cycle Plan to v2 §5): a source fact was superseded, a source decision was reversed, or (insight only) a thematic summary this insight rested on was itself lineage-retired. `NULL` for rows superseded before migration 031. |
| `agent_id` | `TEXT NOT NULL DEFAULT 'legacy'` | Agent that triggered consolidation; `'legacy'` for pre-coordinator rows |
| `scope` | `TEXT NOT NULL DEFAULT 'global'` | Inherited from the source `Fact` cluster's scope |
| `visibility` | `TEXT NOT NULL DEFAULT 'global'` | Read policy, same semantics as `technical_docs.visibility` — enforced on Tier-3 reads (v0.6.2) so a scoped/private source fact's synthesis inherits the same gate. |

**`metadata` structure — THEMATIC rows (§3.1, written by `_consolidate_clusters`):**
```json
{
  "type": "community_summary",
  "kind": "thematic",
  "entity": "",
  "project": "<the gating project axis>",
  "domain": "<the gating domain axis>",
  "level": "domain",
  "aliases": [],
  "source_pg_ids": [<technical_docs.id of every constituent FACT>],
  "entities": ["<human-asserted entity names of the constituent facts — payload, never a gate key>"],
  "cypher_query": "<self-contained Cypher rebuilding this group's provenance neighbourhood at READ time>",
  "timestamp": "<ISO-8601 datetime of this consolidation run>"
}
```

**`metadata` structure — INSIGHT rows (§3.2, `kind: "insight"`, written by `_fold_insight`):** the second consolidation path folds a walked component of judgements (decisions **and** retrospectives — criterion C / the C4 payload rewrite; the pre-C4 fold was decision-only, a seam that silently never marked a Retrospective node `consolidated`).
```json
{
  "type": "community_summary",
  "kind": "insight",
  "entity": "<always \"\" — I1, no entity anchor>",
  "project": "<the seeding group's project — singular, unlike domains>",
  "domains": ["<every domain the walk actually touched — MULTI-VALUED: the walk can legitimately cross domains>"],
  "entities": ["<union of the constituent judgements' own human-asserted entities>"],
  "source_pg_ids": [<technical_docs.id of every JUDGEMENT the fold covers — decisions AND retrospectives, never a fact id — I9>],
  "summary_ids": [<community_summaries.id of the thematic summar(ies) this insight rests on — a SEPARATE field from source_pg_ids; the two id sequences overlap, so mixing them resolves a summary id against a technical_docs row silently>],
  "cypher_query": "<self-contained Cypher re-deriving the §2.3 walk from these judgement ids>",
  "timestamp": "<ISO-8601 datetime of this consolidation run>"
}
```
⛔ **`source_pg_ids` on an insight row is a WIRE CONTRACT** — `coordinator.py`'s `_status_of_summary` (`GET /memory/status/insight:N`) joins it straight to `technical_docs`, and readers must be able to assume every element resolves there. `summary_ids` exists precisely so a thematic summary id never has to be smuggled into that array.

⚠ **Pre-C4 rows still carry the OLD shape** — `domain: "insight"` (a fixed placeholder, not a real domain) and a `projects` array (plural). No backfill: a reader distinguishes the two shapes by checking `metadata ? 'domains'` (C4+) vs `metadata ? 'projects'` (pre-C4). `GET /memory/status/{ref}` (`coordinator.py`) exposes **both** `domain` and `domains` keys on every response so an older client reading the singular key still gets a sensible value (the first `domains` entry) rather than a silent `null`.

Insight rows are **always-INSERT**: they are exempt from the `(entity, domain)` unique upsert (partial index, migration 009) and rely on supersession for dedup — a re-fold on the same source set writes a fresh row that supersedes the old one. ⚠ Facts, decisions and retrospectives all share the **single** `technical_docs` id sequence — they are **not** disjoint (a stale claim `supersede_covered_summaries`' docstring carried before C3 fixed it (U5)); kind isolation is enforced explicitly (an unconditional `kind` check), never assumed from the id space.

**§2.5 identity — an insight's identity is the SET of judgement pg_ids it covers.** A freshly-walked component whose judgement set exactly matches an ACTIVE insight's `source_pg_ids` does **not** fold again: `append_insight_references` (`consolidation_loop.py`) instead appends the triggering thematic summary id to that insight's `summary_ids` and the triggering domain to its `domains`, in place, deduplicated. A strict-subset match ('covered' — not in the plan's LOCKED table, `insight_gate.classify_identity`'s own defensive extra case) is skipped with no write at all.

> **Note:** `source_pg_ids` is stored both as the dedicated column above and inside `metadata` JSONB. The column is the authoritative query path; the JSONB key is retained for backwards compatibility with tooling that reads raw metadata.

> **`metadata.reviewed_supersessions`** (optional, decision 384 §8e): `[{old, by}, …]` — supersessions of this summary's source facts that a consumer reviewed and judged immaterial via `POST /memory/review_hold`. Retrieval suppresses `stale_sources` entries whose `old` appears here, so a held summary stops re-flagging until a *different* source is superseded.

**Indexes:** `community_summaries_embedding_idx` — `ivfflat (embedding vector_cosine_ops)`; btree indexes on `agent_id`, `scope`, `visibility`

**Retrieval role:** queried first on every search — top-1 cosine match is prepended to results as "Global Context Summary" to orient the response before the Tier 1 vector search runs.

**Growth behaviour (thematic rows):** one row per `(entity, project, domain, level)`, keyed by the partial unique index `community_summaries_axis_level_unique` (migration 029, `COALESCE`d on all four axis expressions — `WHERE COALESCE(metadata->>'kind','thematic') <> 'insight' AND NOT superseded` as of **migration 032**). Each consolidation cycle upserts via `ON CONFLICT (...) WHERE ... DO UPDATE` — the freshly-recomputed Zettelkasten index (§3.1 — deterministic, not LLM output) overwrites `content` and `embedding`, while the previous `content` is appended to `summary_history` (capped at 20 entries). The row ID (`id`) is stable across updates **of an ACTIVE row on that axis key**. ⚠ **Migration 032 (Dreaming Cycle Plan to v2 §6 C3.1, defect F0):** before it, the index and the `ON CONFLICT` arbiter did not exclude `superseded` rows, so re-folding a lineage-retired `(project, domain)` group UPDATED the retired row IN PLACE and it stayed superseded forever — permanently invisible to retrieval, and unclosable by `close_refold_ledger_rows`'s `NOT cs.superseded` check. With `AND NOT superseded` on both the index and the arbiter, a retired row no longer conflicts; the next fold on that axis key INSERTs a fresh ACTIVE row instead, and the retired row remains, unmodified, as history (per §4.2 Path A step 4 — a new summary supersedes the old, never resurrected in place). Insight rows instead accumulate as inserts and retire via supersession, exempt from this index entirely. Retrieval surfaces the embedding-closest `WHERE NOT superseded` match per kind — insight first, then thematic.

**Supersession rule — TWO mechanisms, both needed (Dreaming Cycle Plan to v2 §5.1):**

* **Mechanism A — subset coverage (identity-driven).** If summary A's `source_pg_ids` is **covered by** (subset of, or equal to) summary B's `source_pg_ids`, A is superseded by B (`superseded_reason='coverage'`). The equal-set case is how an insight re-fold replaces its predecessor. `supersede_covered_summaries` — kind-isolated **unconditionally** (an explicit `kind` check, never inferred from the id space: facts, decisions and retrospectives share one `technical_docs` sequence, so a thematic and an insight summary's `source_pg_ids` CAN coincidentally overlap) and level-isolated when a level is given (P12).
* **Mechanism B — lineage (invalidation-driven, migration 031).** A summary is retired because a member it was built from is no longer valid — found by **reverse lookup** on `source_pg_ids`/`metadata->'summary_ids'`, never by set comparison (a reversal makes the covered set *smaller*, which Mechanism A structurally cannot express: `retire_invalidated_summaries`, `superseded_reason='lineage'`; the ledger clock is `refold_ledger`, above).

The consolidation daemon sets `A.superseded = true` in Postgres and writes `(B)-[:SUPERSEDES]->(A)` in Neo4j for Mechanism A refolds; Mechanism B retirements do not write a `SUPERSEDES` edge (there is no successor summary at retirement time — only a future re-fold, which supersedes normally when it lands). Cross-entity supersession is supported — an "Outbox" summary can supersede a "Neo4j" summary if it absorbed all the same source facts.

---

### `neo4j_outbox` — Coordinator outbox + dream-cycle ledger

Written by the coordinator on every save, in the same Postgres transaction as `technical_docs`. Applied asynchronously to Neo4j by the outbox worker. Survives crashes and Neo4j outages — pending rows are replayed on restart. For **fact rows** the table doubles as the dream-cycle ledger: a row's presence means "this artifact has not finished dreaming", and its deletion is the conclusive record that both stores are synced.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | Monotonic outbox entry ID |
| `pg_id` | `BIGINT NOT NULL` | References the `technical_docs.id` row this write belongs to |
| `cypher_params` | `JSONB NOT NULL` | Parameters passed to the Neo4j Cypher write (content snippet, source, entities, etc.) |
| `status` | `TEXT NOT NULL DEFAULT 'pending'` | Fact lifecycle: `pending` → `in_progress` → `applied` → `rem_reviewed` → `consolidated` → **row deleted**; `failed` is the dead-letter state. `in_progress` means a coordinator instance has claimed the row for Neo4j apply. `applied` means the Neo4j write succeeded. `rem_reviewed` means REM has enriched the fact and verified Neo4j consistency — the durable NREM backlog. `consolidated` is set by NREM **in the same transaction** as the `community_summaries` INSERT the fact was folded into; the row is **deleted** only after the Neo4j consolidation marking succeeds. Rows stuck in `in_progress` after a crash are reset to `pending` on coordinator startup; rows stuck at `consolidated` (crash between the stores) are reconciled by the next ledger sweep, which re-applies the idempotent graph marking and closes them. **Decision and retrospective rows** follow the same lifecycle through the *insight* path (decision pg_id 276): a fold flips the cluster's decision rows plus the consumed retrospective rows to `consolidated` transactionally with the `kind='insight'` INSERT and deletes them (by row id) after the graph marking. Identify retrospective rows by `cypher_params->>'type'`, never by status. |
| `retries` | `INT NOT NULL DEFAULT 0` | Incremented on each failed application attempt |
| `created_at` | `TIMESTAMPTZ DEFAULT now()` | When the outbox row was written |
| `applied_at` | `TIMESTAMPTZ` | Set when status transitions to `applied` |
| `next_attempt_at` | `TIMESTAMPTZ` | (migration 011) Earliest time a failed row may be retried — set to `now() + exponential·jitter` on each failure so a Neo4j outage backs off instead of re-hammering every drain cycle. `NULL` = eligible immediately (never failed). |

**Index:** partial btree on `status WHERE status = 'pending'` — keeps the worker scan O(backlog), not O(table). The drain claim also gates on `next_attempt_at IS NULL OR next_attempt_at <= now()`, a cheap filter on the small pending set.

**Decision and retrospective rows are exempt from the ledger tail** (`consolidated`/deletion): their lifecycle ends at `applied`/`rem_reviewed` until the decision-NREM design (insight consolidation, reversal cascade) is ratified. Note that a retrospective row shares its **target decision's** `pg_id`; because REM's outbox mark targets the latest `applied` row for a `pg_id`, retro rows can sit at `rem_reviewed` — they are identified by `cypher_params->>'type'`, never by status.

**Consistency note:** after a save returns success (Postgres committed), the corresponding Neo4j `Fact` node may not yet exist — the outbox worker applies it asynchronously. `graph` queries immediately after a save use `?consistency=neo4j` to block until the outbox row is applied.

### `refold_ledger` — lineage-invalidation clock (migration 031, Dreaming Cycle Plan to v2 §5)

The durable attribution trail for **Mechanism B** (cascading/lineage supersession, as distinct from Mechanism A's ordinary subset-coverage retirement above): when a source fact is superseded or a source decision is reversed, every ACTIVE `community_summaries` row holding it is retired (`superseded_reason = 'lineage'`), and its still-eligible constituents need to rejoin a future fold. **The ledger is only the clock** — re-gating itself is always re-derived from the graph (`_find_grounded_fact_groups` / `_find_fresh_insight_clusters`), never from this table's contents; its one job is to widen the durable backlog count so a cycle actually fires. Follows the `project_promotions` model: rows are **closed, never deleted** — the row itself is the record that an invalidation happened and what it raised.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `pg_id` | `BIGINT NOT NULL` | The `technical_docs.id` now eligible to rejoin a fold — resolved through `superseded_by` to the record that STANDS (`resolve_standing_ids`), never the invalidating record itself. |
| `summary_id` | `BIGINT NOT NULL` | The retired `community_summaries.id` this row's eligibility traces back to. |
| `summary_kind` | `TEXT NOT NULL` | `'thematic'` \| `'insight'` — which fold path `pg_id` re-enters. |
| `trigger_kind` | `TEXT NOT NULL` | `'technical_docs'` (a superseded fact, or a reversed decision) \| `'community_summaries'` (a retired thematic summary whose retirement cascaded to an insight resting on it). Two typed shapes, deliberately never collapsed into one untyped id column — `technical_docs` and `community_summaries` are separate id sequences that can overlap numerically. |
| `trigger_id` | `BIGINT NOT NULL` | The id in the sequence `trigger_kind` names. |
| `status` | `TEXT NOT NULL DEFAULT 'open'` | `open` → `refolded` \| `dropped`. `refolded`: `pg_id` now appears in an ACTIVE summary of the matching kind **whose `COALESCE(updated_at, created_at)` is no older than the ledger row itself** (C3.1 F2 — without the recency bound, `pg_id` sitting in some OTHER active summary that merely *predates* the invalidation closed the row `constituent_folded` with nothing having actually folded). `dropped`: `closed_reason='below_density'` (I7 — the row's group was scanned this cycle and did not meet `density_threshold`; not backlog, not a stall), `'out_of_scan'` (C3.1 F1 — the row's `pg_id` was not even a member of this cycle's scan at all, e.g. ungrounded/domainless; distinct from `'below_density'` so the two zombie classes stay tellable apart), or `'constituent_superseded'` (defensive — the record became superseded again after the row opened). |
| `closed_at` / `closed_reason` | `TIMESTAMPTZ` / `TEXT` | Set together, on the terminal transition. `NULL`/`NULL` while `status='open'`. |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

**Indexes:** partial btree `refold_ledger_open_pgid_idx` on `pg_id WHERE status = 'open'` — the due-ness read (`fetch_refold_backlog`: `SELECT DISTINCT pg_id ... WHERE status='open'`); btree `refold_ledger_summary_idx` on `summary_id` — attribution lookups ("what did summary X's retirement raise?").

**No uniqueness constraint.** Duplicate rows for the same `pg_id` are legitimate — two different summaries invalidated at different times can both raise the same constituent, and a single retirement can carry more than one trigger. Due-ness always counts `DISTINCT pg_id`, never a row count.

**Read by:** `fetch_combined_fact_backlog` (`fetch_ledger_backlog` UNION `fetch_refold_backlog`, deduped) — this is the WIDENED input to `consolidation_due` / `run_ledger_sweep`'s density gate; the gate's own predicate (`len(backlog) >= DENSITY_THRESHOLD`) is unchanged, only what feeds it grew a second source.

**Written by:** `retire_invalidated_summaries` (opens rows, atomically with the `community_summaries` retirement — one Postgres transaction) and `close_refold_ledger_rows` / `drop_below_density_refold_rows` / `drop_out_of_scan_refold_rows` (close them; the last two are C3.1's F1 fix — see `status` above). `run_lineage_invalidation_pass` (`consolidation_loop.py`) is the async driver: Postgres retirement first, then — **insight retirements only** — `d.consolidated = false` is cleared on the retired insight's `Decision`/`Retrospective` graph nodes (gate-critical, `insight_gate.py`'s G3 freshness check). A retired **thematic** summary never touches the graph: a `Fact` node's own `consolidated` property has no reader (`_find_grounded_fact_groups`'s full-scan discovery never reads it).

### `consolidation_runs` — dream-cycle liveness/coverage ledger (ADR-018, migration 012)

One row per consolidation/insight **cycle** so a fold outcome is queryable state, not just a journal line. The daemon (`consolidation_loop.py`) is the sole writer; the coordinator rolls it up into the `/memory/telemetry` `consolidation` section and a cached `/health.consolidation` snapshot.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `cycle_type` | `TEXT NOT NULL` | `insight` \| `fact_consolidation` |
| `started_at` / `finished_at` | `TIMESTAMPTZ` | `finished_at IS NULL` ⇒ in-flight (not stalled). A prior process's in-flight rows are marked `crashed` on daemon startup (orphan reap, mirrors ADR-010). |
| `outcome` | `TEXT` | `completed` \| `crashed` \| `deferred` (deferred = a due cycle skipped for GPU-busy / backup quiesce, reason in `extra`, throttled to one per cycle_type per minute). |
| `folds_attempted` / `folds_succeeded` / `folds_failed` | `INTEGER NOT NULL DEFAULT 0` | A row with `folds_succeeded > 0` is the success that resets `last_success_age` in the stall rule. |
| `eligible_clusters` | `INTEGER` | Coverage census captured **at gate-time, before folding** (PR-2) — uncovered insight opportunities. |
| `eligible_oldest_age_seconds` | `INTEGER` | Age of the most-neglected actionable cluster — the **K-th-oldest (K=`INSIGHT_THRESHOLD`) member's `neo4j_outbox.created_at`** (eligibility onset). NULL-safe for facts predating the outbox. |
| `error_class` / `error_msg` | `TEXT` | Populated on `crashed` (msg truncated to 500 chars). |
| `extra` | `JSONB` | Defer reason; reserved for family C (per-fold quality: `max_cosine`, fold shape). |

**Indexes:** `consolidation_runs_type_started_idx` on `(cycle_type, started_at DESC)` (latest / latest-success per type); partial `consolidation_runs_inflight_idx` on `(started_at DESC) WHERE finished_at IS NULL` (in-flight probe).

**Self-pruning:** the daemon deletes rows older than `CONSOLIDATION_RUNS_RETENTION_DAYS` (default 30) at startup. Writes are ~hourly (one per sweep), so volume is trivial.

**Stall rule (coordinator):** `stalled = eligible backlog present AND no successful fold within CONSOLIDATION_STALL_THRESHOLD_SEC (default 2.5× the NREM sweep interval) AND nothing in-flight`. A slow LLM fold reads as in-flight, not stalled.

---

### `entity_embeddings` — alias candidate-generation store (ADR-017, migration 014)

Entity **names** embedded once (BGE-M3, 1024-dim, via the gateway) so the alias writer finds cosine-near
candidates with an indexed ANN query instead of re-embedding the whole entity set each sweep. Vectors stay in
Postgres (Neo4j remains the structure tier); reuses the `technical_docs` HNSW pattern.

| Column | Type | Notes |
|---|---|---|
| `name` | `TEXT PRIMARY KEY` | Entity identity (the coordinator MERGEs entities by name) |
| `embedding` | `vector(1024)` | BGE-M3 name embedding (the gateway 1024-dim contract) |
| `updated_at` | `TIMESTAMPTZ` | Last upsert |

**Indexes:** `entity_embeddings_embedding_idx` — `hnsw (embedding vector_cosine_ops)`.

### `alias_adjudications` — alias verdict ledger (ADR-017, migration 014)

Per-pair `alias` / `distinct` verdicts from the writer — both the audit trail (method / confidence / signals /
rationale, revocable) and the idempotency cache: a sweep skips pairs already judged, so the LLM is never re-asked.

| Column | Type | Notes |
|---|---|---|
| `name_a`, `name_b` | `TEXT` | Canonical order `name_a < name_b`; `UNIQUE(name_a, name_b)` |
| `verdict` | `TEXT` | `alias` \| `distinct` |
| `method` | `TEXT` | `normalized_exact` (auto-accept) \| `llm` |
| `confidence` | `REAL` | 0..1 (llm) or 1.0 (normalized-exact) |
| `cosine`, `lexical_jaccard`, `shared_facts`, `domain_disjoint` | `REAL`/`INT`/`BOOLEAN` | Signals recorded at adjudication |
| `rationale` | `TEXT` | Short LLM justification (audit) |

**Indexes:** `alias_adjudications_verdict_idx` on `(verdict)`.

### `relation_adjudications` — machine-relation verdict + calibration ledger (migration 020)

One ledger for BOTH machine-minted relation families: `entity_relation` (typed
Entity→Entity edges from the evidence sweep / REM, name-keyed endpoints) and
`evidential` (record→record proposals such as `Decision INFORMED_BY Fact`,
pg_id-keyed endpoints; `GROUNDED_IN` is never machine-minted so it never appears
here). Every machine verdict lands here with its quantitative signals; operator
labels recorded on these rows are the ONLY calibration input — per-family
reliability curves are computed from them, and a family's confidence thresholds
act only once it is calibrated (~20 labels). Also the audit trail and the
don't-re-ask idempotency cache. One CURRENT row per directed edge per family:
a re-score updates the row in place and preserves the prior rung inside
`signals.prior_rungs` (the evidential ladder: `rem_k3` proposal → `llm_sweep`
re-score → operator label/promotion).

⛔ **The `evidential` family is DORMANT since the Dreaming Cycle v2 plan (§1.1,
task B1).** REM's judgement-relation decommissioning stopped REM proposing
record→record `INFORMED_BY`/evidential edges — the only writer of `method='rem_k3'`
rows — so this table currently holds zero `evidential` rows and `relation_sweep.py
--evidential` (rung 2) finds nothing to re-score. `relation_confidence.
FAMILY_EVIDENTIAL` and the rung-2 re-scoring pipeline are kept intact
deliberately, as the operator-invoked surface a future non-spine ontology would
turn back on — not wired to anything, not deleted.

| Column | Type | Notes |
|---|---|---|
| `family` | `TEXT` | `entity_relation` \| `evidential` (CHECK-enforced endpoint encoding per family) |
| `src_name`, `tgt_name` | `TEXT` | Entity endpoints (entity_relation family; directed) |
| `src_pg_id`, `tgt_pg_id` | `BIGINT` | Record endpoints (evidential family; directed) |
| `rel_type` | `TEXT` | The typed relation; rejects use the sentinel `NONE` (never interpolated into Cypher) |
| `verdict` | `TEXT` | `accept` \| `reject` |
| `method` | `TEXT` | `llm_sweep` \| `rem_k3` \| `operator` |
| `confidence` | `REAL` | 0..1; evidential `rem_k3` rows are capped BELOW the consumption threshold (born-below rule) |
| `support` | `TEXT` | `graph_evidence` (≥2 corroborating facts) \| `text_only` |
| `signals` | `JSONB` | co-occurrence count, sub-labels, vote share, `prior_rungs` history, … |
| `operator_label` | `TEXT` | `correct` \| `incorrect` — the calibration oracle (review-edges flow) |
| `promoted_at` | `TIMESTAMPTZ` | Operator promotion → live edge `asserted_by='operator'` |
| `model`, `run_id`, `rationale` | `TEXT` | Audit: adjudicating model, sweep/cycle correlation id, short justification |

**Indexes:** partial UNIQUE per family on the directed edge; `(family, operator_label, created_at)` for review/calibration reads.

---

### `decision_alternatives` — one row and one vector per option considered (migration 026)

A decision stores the options it weighed in `metadata.decision.alternatives`, and
that stays the source of truth. This table exists for the question the array
cannot answer: **which decisions considered the same thing?** A decision has a
single embedding, dominated by its own text, so two records that both weighed
"one vector per record versus one per fragment" look unrelated unless their
headlines happen to agree. Giving each alternative its own vector — and keeping
`decision_pg_id` beside it — resolves an alternative-level similarity back to a
pair of **decisions**, which is the answer wanted.

Postgres only. There is no node, no entity and no graph half: a node per
alternative would be a mostly-singleton node named with free prose.

| Column | Type | Notes |
|---|---|---|
| `decision_pg_id` | `BIGINT` | FK → `technical_docs(id)` **ON DELETE CASCADE** — an alternative has no meaning without its decision |
| `ordinal` | `INTEGER` | Position in the decision's OWN array, 0-based. Blank entries are skipped **without renumbering**, so the ordinal keeps pointing at the same entry |
| `text` | `TEXT` | The alternative verbatim. Punctuation inside an entry is prose, never a delimiter |
| `embedding` | `vector(1024)` | BGE-M3. **NULL means PENDING, never "has no vector"** |
| `embedded_at` | `TIMESTAMPTZ` | CHECK-tied to `embedding`: both set or both null |
| `attempts`, `last_error`, `next_attempt_at` | | Consecutive-failure count driving exponential backoff. **Not a budget** — no value of `attempts` removes a row from the pending set |
| `created_at` | `TIMESTAMPTZ` | Also the attribution trail: rows created long after their decision were seeded by an operation, not by the save path |

**Write path.** Rows are reconciled inside the save's own transaction — the
record and its alternatives commit together — and carry no embedding. The
coordinator's alternative-vector worker fills them afterwards, in batches,
through the same embedding endpoint every other vector uses. So a decision that
weighed eight options costs the same **one** embedding call on the request path
as one that weighed none.

**Reconcile, never append.** A save can rewrite a record in place
(`ON CONFLICT (content_hash) DO UPDATE`), and alternatives do get rewritten. The
reconciler converges on the decision's array: entries whose text is unchanged are
not touched and keep their vectors, changed entries return to pending, retracted
ones are deleted. An idempotent re-save therefore embeds nothing.

**Pending is a query, not a queue** (`WHERE embedding IS NULL`), which is what
makes filling them asynchronously safe: the pending state lives in a committed
row, so a crash or restart between the write and the embed leaves work the next
sweep finds. Nothing needs to remember what was in flight.

**Indexes:** UNIQUE `(decision_pg_id, ordinal)`; HNSW `vector_cosine_ops` on
`embedding`; a partial index on `next_attempt_at WHERE embedding IS NULL` so the
worker's scan grows with the backlog rather than with the corpus.

**Coverage** is reported at `GET /memory/telemetry` → `spine.alternative_vectors`
(`entries / embedded / pending / failing / oldest_pending_age_s`). A full
`decisions.alternatives_pct` beside a stalled `pending` means the populator has
stopped — the two measure different things and both are needed.

**Grouping decisions by what they considered** — the query this table exists for:

```sql
-- Decisions whose alternatives are closest to those of decision $1,
-- scored by the best-matching pair of alternatives.
SELECT b.decision_pg_id,
       max(1 - (a.embedding <=> b.embedding)) AS best_similarity
  FROM decision_alternatives a
  JOIN decision_alternatives b
    ON b.decision_pg_id <> a.decision_pg_id
 WHERE a.decision_pg_id = $1
   AND a.embedding IS NOT NULL
   AND b.embedding IS NOT NULL
 GROUP BY b.decision_pg_id
HAVING max(1 - (a.embedding <=> b.embedding)) >= $2   -- the floor; see below
 ORDER BY best_similarity DESC
 LIMIT 10;
```

⚠ **The floor is not optional, and it is per-deployment.** `ORDER BY … LIMIT 10`
without one always returns ten rows, so the query cannot say *"nothing here
considered the same thing"* — it just ranks the noise. Short technical prose
embeds into a narrow band, and the top of that band is nowhere near a real match.

**It is a BLOCKING KEY, not a verdict.** The floor selects candidates that a
reader or a downstream gate then judges — the same rule this framework already
applies to alias candidates, where cosine generates candidates and the LLM
decides. Read a sample of the pairs a candidate floor admits: near the boundary a
substantial share are not the same consideration at all, sharing only the *shape*
of a rejection ("X (rejected: …)" resembles every other rejection). Choose the
floor for the recall you want in that candidate set, not for precision it cannot
deliver on its own.

**Derive it from the corpus's own tail, not from a guessed constant.** Compute the
distribution over all cross-decision pairs — on a few hundred alternatives that is
a hundred thousand pairs and costs a single query — and take the floor from just
above a high percentile (p99.9 is a reasonable anchor).

⚠ **Do not look for "the maximum of unrelated pairs" to sit above.** There isn't
one: genuine matches live in the same population as the noise, so the maximum is
whatever the strongest true match scores. Sampling a small subset appears to give
such a maximum only because it missed the true pairs — a landmark that moves with
sample size is not a landmark. Re-derive whenever the embedding model changes.

An alternative's own text is reached from the graph by dereferencing the
`:Decision` node's `pg_id` into Postgres — the graph is not a second home for
this data.

### The project registry — `projects`, `project_aliases`, `project_promotions`

Every record belongs to exactly one project, established at first write and
validated against this registry at ingress. The registry is what makes an
unrecognised value **loud** instead of merely new: without it a typo and a new
project are the same event, and both enter the corpus silently.

**`projects` — the identity (migrations 022, 027).**

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT` | **PRIMARY KEY, `GENERATED BY DEFAULT AS IDENTITY`.** The identity: what other tables and the graph node point at. `BY DEFAULT` rather than `ALWAYS` so a restore can carry ids across machines instead of having them reissued under the rows that reference them |
| `name` | `TEXT` | UNIQUE, NOT NULL. A **label** — what a client asserts, what an operator types, what the client-side graph templates filter on. Renameable, which is exactly why it is not the key |
| `description` | `TEXT` | Owed from the operator; never auto-filled, because a placeholder would claim one was supplied |
| `created_by` | `TEXT` | e.g. `workspace_scan`, or the agent that declared the project new |

⚠ **A record's `metadata` keeps the project NAME, not the id** — it records what
the client asserted at write time, and a creation-time assertion is history. The
record reaches its identity through this registry.

**`project_aliases` — a retired spelling, resolved in one hop (migrations 024, 027).**
Keyed on `alias_id → aliases(name)` and **`project_id`**, never on a project
name: under an identity an alias stops being a mapping between two labels and
becomes an *alternate label on one identity*, so an inactive row stays true
across a rename with no maintenance. Resolution is one indexed lookup at ingress
and never a walk — chains are collapsed when the rename is written, so no alias
ever points at another alias and resolution can never cycle. A trigger keeps the
two namespaces disjoint: a string is never both a registered project and an
active alias for a different one.

**`project_promotions` — the one-way ledger (migrations 023, 027).**
Carries **both** `to_project` (TEXT, the name the promotion actually targeted, on
the day it targeted it) and `to_project_id` (the durable pointer). They answer
different questions and neither replaces the other: a later rename re-points the
id and preserves the original name in the row's `note`, because the ledger's
whole purpose is to answer *"what was this before"*. It deliberately has **no**
foreign key on the name and never uses `ON UPDATE CASCADE` — a cascade would
rewrite the evidence with no trace at all.

### The domain registry — `project_domains`, `domain_aliases` (migration 028)

A **domain is a SECTION of one project** — `operations`, `infrastructure`. It is
optional, a record may sit in several, and it is validated at ingress exactly as
a project is. Sections are **project-local**: `operations` under one project and
`operations` under another are different sections that share a word, so nothing
here resolves a domain by name alone.

**`project_domains` — the identity.**

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT` | **PRIMARY KEY, `GENERATED BY DEFAULT AS IDENTITY`.** The identity the `:Domain` node is keyed on. Built on the surrogate key from the start rather than on a name pair, so a rename never becomes a distributed rewrite |
| `project_id` | `BIGINT` | **NOT NULL → `projects(id)`** — the project this is a section of, referenced by IDENTITY and never by name |
| `name` | `TEXT` | NOT NULL, **UNIQUE within the project**. A label. Blank and the parked sentinel are refused by CHECK |
| `description` | `TEXT` | Owed from the operator. It carries more weight here than on a project: proposals match a section's DESCRIPTION as well as its name, which is what lets an operator reach a section named nothing like the word they typed |

**`domain_aliases` — a retired spelling, resolved in one hop.** The same junction
shape as `project_aliases` (migration 024 fixed it for both axes), pointing at
`domain_id`. It also carries `project_id`, which looks like the duplication this
schema spends its time removing and is not: a **composite foreign key** on
`(domain_id, project_id) → project_domains(id, project_id)` makes the pair valid
only if it matches a real registry row, so the column cannot become a second,
disagreeing answer. It is there because the uniqueness rule is project-local —
at most one ACTIVE mapping per alias **per project** — and an index cannot follow
a foreign key to discover that. The alternative, an expression index over a
function reading `project_domains`, would require declaring a table-reading
function `IMMUTABLE`, which is false; an index built on a false immutability
claim is silently wrong rather than loudly broken. A trigger keeps the two
namespaces disjoint **within a project**, not globally — the same spelling may
legitimately be canonical in one project and an alias in another.

**Who controls the axis.** A **fact** and a **decision** each assert their own
project and domain. A **retrospective** asserts neither: both come from the
decision it judges, and one that names a domain is refused (`400
domain_not_allowed_on_judgement`). A decision that names no domain inherits its
grounding facts' sections as a **default, never a ceiling** — a decision
routinely reaches further than the fact that prompted it, so the operator can
always name more.

**The graph half is not automatic here either.** `:Domain` nodes and their edges
are written by the outbox worker at first write; the historical population is
enqueued by `scripts/backfill_domain_of.py --apply`. `GET /health` →
`domain_identity` reports registry-vs-graph drift plus `unattached` — a section
with no `PROJECT_OF` edge — because the cross-project and cross-domain walk this
axis exists to enable traverses exactly that edge.

⛔ **There is NO name-keyed `:Domain` fallback**, deliberately unlike the project
axis. Losing a `PROJECT_OF` edge violates an axis that already gates folding, so
that write falls back to keying on the name; nothing gates on domain yet and the
value stays verbatim in Postgres either way, so the honest answer to "no
identity" is no edge and a log line. A name-keyed section node would re-ship, on
a new axis, the identity defect migration 027 removed.

**The graph half is not automatic.** A Postgres migration cannot reach Neo4j, so
`:Project` nodes are stamped with `project_id` by
`scripts/reconcile_project_identity.py --apply`, which is part of the documented
upgrade path and is idempotent. Until it has run, the fold gate declines to count
an unidentified project toward its "≥ 2 distinct projects" rule — it fails
closed — and `GET /health` → `project_identity` reports the outstanding count so
an incomplete upgrade is visible rather than presenting as a quiet corpus.

### One normalised key per axis — `normalized_key` (migration 035)

Both registries answered on exact strings until 035. `axis_normalize(text)` — lower-case, every
non-alphanumeric character removed, the same key the gateway's `axis_key()` computes — is now a
SQL function, and each registry carries a **trigger-maintained `normalized_key` column**:
`projects (normalized_key)` is UNIQUE and `project_domains (project_id, normalized_key)` is
UNIQUE, so two registered names can never share a key. Migration 035 backfills the column,
pre-checks existing rows and names the colliding pair (and an empty-key legacy name) before any
constraint is added, and widens the alias-namespace disjointness triggers from 024/028 to the
key, excluding an alias's own target so a rename alias keys like the project it retires. The
gateway resolves a supplied project or domain by exact name, active alias, key, then alias key,
rewrites to the canonical and reports it (`project_resolved`, `domains_resolved`); search filters
match the canonical, its active aliases and any key-equivalent spelling. No functional index is
used anywhere — the column is the invariant carrier, as in 033.

### The entity vocabulary — `entity_vocabulary`, `entity_vocab_aliases` (migration 033)

`entity_registry` (migration 030) is a flat set of exact strings — a lookup
gate with no notion that two spellings name the same thing, so "Kubernetes"
and "kubernetes" register as unrelated names and a genuinely different
spelling like "K8s" has nowhere to point AT at all. This vocabulary gives
entities the same canonical + alias shape the project and domain axes already
have: a spelling resolves to ONE identity instead of fragmenting across
near-duplicate rows.

⛔ **This migration creates the tables and seeds them; it wires up NOTHING.**
No ingress gate, coordinator path, or REM prompt consults these tables yet —
that is a later unit. `entity_registry` is untouched and remains the ingress
log; what becomes of it once this vocabulary exists is a separate question.

**Aliasing is SEMANTIC (`decision:1380`, Option B), not normalization-derived.**
An alias is any spelling the operator asserts for a canonical — "K8s" is a
legitimate alias of "Kubernetes" despite normalizing to a completely
different key. What the schema still guarantees is **lookup-unambiguity**: a
normalized value must never resolve to two different identities. See the
triggers below.

**`entity_normalize(text)` — THE one normalization definition**, `IMMUTABLE`:
lowercase, then strip every character that is not alphanumeric per the POSIX
`[:alnum:]` class. ⚠ `[:alnum:]` is **locale-dependent** — under a non-`C`
UTF8 locale, accented and non-Latin letters survive normalization instead of
being stripped as punctuation; under `C` it behaves like `[a-zA-Z0-9]`. This
install's actual behavior is a live-database question, not something this
doc can answer. Every reader or writer that resolves a spelling against this
vocabulary must call this exact function rather than re-implementing the
rule, or the two could disagree about whether two spellings resolve to the
same identity.

**`entity_vocabulary` — the canonical identity.**

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT` | **PRIMARY KEY, `GENERATED BY DEFAULT AS IDENTITY`.** What a future ingress gate and the graph point at |
| `name` | `TEXT` | NOT NULL. The verbatim canonical spelling — TEXT, not `VARCHAR(n)`: the registry this seeds from is itself unbounded, and a length cap would make a legal row abort the migration |
| `normalized_key` | `TEXT` | NOT NULL, UNIQUE, `CHECK (<> '')`. `entity_normalize(name)`, maintained by a `BEFORE INSERT OR UPDATE` trigger rather than a generated/stored column — see the note below |
| `created_at` | `TIMESTAMPTZ` | default `now()` |
| `registered_by` | `TEXT` | default `'system'`; the seed carries the value straight from `entity_registry.registered_by` |

**`entity_vocab_aliases` — every spelling the operator asserts.** Mirrors
`project_aliases`' junction shape: the alias STRING is kept verbatim and is
what must be **unique** (`UNIQUE (alias)`) — a verbatim spelling maps to
exactly one identity. `normalized_alias` carries a **plain** index, not a
unique one: under Option B, two distinct verbatim aliases of the *same*
entity can legitimately normalize to the same key, so the schema no longer
forces every alias of an entity to share one normalized form. The FK
(`entity_id`, `ON DELETE CASCADE`) points at the canonical's IDENTITY, never
at its name.

**Two triggers enforce lookup-unambiguity, because no single CHECK can see
across these two tables:**
- Inserting/updating a **canonical** is refused if its `normalized_key` is
  already claimed by an existing alias whose own parent resolves to a
  *different* `normalized_key`. Same-key parent → allowed through, so
  `ON CONFLICT (normalized_key) DO NOTHING` arbitrates — what makes a re-run
  a true no-op.
- Inserting/updating an **alias** is refused if its normalized value is
  already a *different* canonical's identity, or already another alias
  row's identity pointing at a *different* entity. Equal to its own parent's
  key is fine — the ordinary case/punctuation variant, not a collision.
- Either write is refused outright, with the offending name in the message,
  if it normalizes to the **empty string** (every character stripped) — a
  value that ungateable has nothing to look up by.

⚠ **What SQL cannot enforce:** both are `BEFORE ROW` triggers reading a
sibling table under ordinary READ COMMITTED isolation, not one atomic
constraint — two concurrent transactions can each pass the check before
either commits. Serializing writes to this vocabulary is owed from the
ingress gate that becomes its write path in the later unit — a standing
condition of `decision:1380`, not resolved here.

⚠ **Names appear verbatim in these triggers' exception messages, by design**
(security review, ruled ACCEPTED under `decision:1380`) — entity names here
are operator-curated data inside the trust boundary, the same boundary
`assert_alias_namespaces_disjoint` (migration 024) already puts project/alias
names across, never untrusted tenant input.

**SEEDING is pure SQL, deriving the vocabulary from THIS install's own**
`entity_registry` **+** `technical_docs` **— no entity name is ever written
into the migration file**, so it stays portable across every deployment. On a
fresh install `entity_registry` is empty and the seed inserts nothing; there
is no assumption that any curation of the registry has already happened.
Per normalized-key group (spellings that normalize the SAME way — a semantic
alias like "K8s" is an operator judgement the seed cannot make from
`entity_registry` alone), the canonical pick is: highest count of
`technical_docs` FACT records whose `metadata->'entities'` array contains
that exact spelling; tie → earliest `entity_registry.created_at`; tie →
shortest `name`; tie → lexicographic `name` (`entity_registry.name` is a
PRIMARY KEY, so this always resolves — the pick is fully deterministic).
Every other spelling in the group becomes an alias of the pick, attributed to
`entity_registry`'s own `registered_by`/`created_by` (that column is `NOT
NULL` there, so there is no fallback literal to reach for). A registry name
whose normalized key is the empty string is **filtered out of the seed**
(`WHERE entity_normalize(name) <> ''`) rather than reaching the triggers'
`RAISE` — it stays untouched in `entity_registry`; the seed skips an
ungateable name, it never aborts and never deletes.

⚠ **Why `normalized_key`/`normalized_alias` are trigger-maintained, not
`GENERATED ALWAYS AS (...) STORED` columns.** `generate_schema_init.py`
introspects columns via `information_schema.columns`, which does not report a
generation expression — a stored generated column would silently render as a
bare, unpopulated column in a regenerated `schema_init.sql`. That is the same
class of silent drop this schema's CHECK/FK/IDENTITY history warns about,
just not tripped yet; the trigger form uses the function-and-trigger path the
generator does faithfully introspect.

---

## Neo4j (Relational Memory)

> Configurable via `ontology.yaml`. Label and relationship keys map directly to the `labels:` and `relationships:` sections.

### Core labels (Tier 1 / Tier 3 nodes)

| Label | Written by | Purpose |
|---|---|---|
| `Fact` | Outbox worker (on every save) | Primary node — one per `technical_docs` row, keyed by `pg_id` |
| `Entity` | Outbox worker | Named entity extracted from `metadata["entities"]`; anchors consolidation clusters |
| `CommunitySummary` | Consolidation daemon | Synthesised narrative, keyed by `pg_id`. Thematic: one per `(entity, domain)` hub. Insight (`kind: "insight"` property, decision pg_id 276): cross-project decision synthesis, accumulates + supersedes. |
| `ReasoningTrace` | Agent (via `archive_reasoning_trace`) | Root of a reasoning session |
| `ReasoningStep` | Agent (via `archive_reasoning_trace`) | Individual step within a trace |

### Entity type sub-labels (Path A multi-label)

Type specialisations applied **on top of** `:Entity` (e.g. `:Entity:Component`), so all existing
`:Entity` queries keep working. **Staged rollout:** defined in `ontology.yaml` now; REM assigns them during
enrichment (Stage 1.3) and a one-time backfill types the existing corpus (Stage 1.4). Person / Agent /
Process reuse the provenance labels `Human` / `AIAgent` / `Activity` rather than minting parallel types.

| Sub-label | Captures |
|---|---|
| `Component` | software unit we build (module / class / script / daemon) |
| `System` | service / datastore / framework / infrastructure we run |
| `Model` | AI/ML model |
| `Concept` | pattern / technique / principle / signal |
| `Document` | spec / ADR / doc / research artifact |

### Provenance labels (Phase A — PROV-O inspired)

Written by the outbox worker when `metadata["type"] == "decision"`.

| Label | Purpose |
|---|---|
| `Decision` | An architectural or design decision — keyed by `pg_id`, links to all PROV-O edges. Lifecycle flags: `rem_processed` (REM enrichment done), `consolidated` (folded into an insight), `superseded` (reversed via `rating="reversed"`). ⚠ **The options weighed and the confidence held are NOT node properties** — no query filters, orders or matches on them, so they are payload and live once, in Postgres (`metadata->'decision'`), reached by this node's `pg_id`. Graph expansion dereferences them into a search hit's `adr_props`. A record property the graph must *walk* on (a project identity, a lifecycle flag) is duplicated deliberately; one only ever rendered is not. |
| `Retrospective` | A recorded outcome for a decision (retro-as-record, v2) — keyed by `pg_id` like Fact/Decision. Carries `rating` (outcome-state enum: `validated`\|`mixed`\|`refined`\|`pending`\|`reversed`), `date`, `content` (notes snippet; full notes in Postgres), `source`, `fact_kind`, and `rem_processed`. Reached from its decision via the `HAD_OUTCOME` trigger edge; grounds in evidence facts via typed ROLE edges. Framework-defined — never configurable via `ontology.yaml`. |
| `Human` | A person who owns or makes a decision (`decided_by` field) |
| `AIAgent` | An AI tool that assisted in the decision (`assisted_by` list) |
| `Project` | Project scope — the node every record's belonging edge points at. **Keyed on `project_id`, the registry identity (migration 027); `name` is a display label carried on the node, unique but renameable.** The fold gate counts distinct `project_id`, so a node with none is not counted — see `projects` below. |
| `Domain` | A SECTION of one project (migration 028). **Keyed on `domain_id`, the registry identity**; `name` is a display label, unique only WITHIN its project — which is why there is deliberately no name constraint on this label. Never an `:Entity`, never the target of `MENTIONS`, never in REM's label table: it is a belonging axis, not a topic. |
| `Activity` | A work session or task context (reserved; not yet written automatically) |
| `Milestone` | A significant achievement marker (reserved; not yet written automatically) |

### Core relationships

| Relationship | Pattern | Written by |
|---|---|---|
| `MENTIONS` | `(:Fact)-[:MENTIONS]->(:Entity)` | Outbox worker — from `metadata["entities"]`; drives graph navigation and REM/entity-relation linking (Tier 3 consolidation itself keys on project+domain, not entities — fact 1215) |
| `REPORTS_ON` | `(:Fact)-[:REPORTS_ON]->(:Entity)` | Legacy alias; accepted by consolidation query. Use `MENTIONS` for new saves. |
| `ALIASES` | `(:Entity)-[:ALIASES]-(:Entity)` | Soft synonym link between entity surface forms (`coordinator` ↔ `Coordinator`), v0.6.0. **Never merges nodes** — reversible. Consolidation + search traverse alias *components*: Neo4j GDS `gds.wcc` stamps `Entity.alias_component`, and clusters group on `coalesce(alias_component, elementId(e))`. Edges carry `method`/`score`/`confidence`. Written by the REM alias-writer (v0.6.1); until then created via the offline `entity_resolution_eval.py` harness. |
| `SUMMARIZED_BY` | `(:Fact\|:Decision)-[:SUMMARIZED_BY]->(:CommunitySummary)` | Consolidation daemon after synthesis (Decision source = insight fold) |
| `NEXT_STEP` | `(:ReasoningStep)-[:NEXT_STEP]->(:ReasoningStep)` | Agent — links consecutive steps in a trace |

### Provenance relationships (Phase A — PROV-O inspired)

Written by the outbox worker for `type:decision` saves.

| Relationship | Pattern | Meaning |
|---|---|---|
| `WAS_ATTRIBUTED_TO` | `(:Decision)-[:WAS_ATTRIBUTED_TO]->(:Human)` | Who owns the decision |
| `WAS_ASSISTED_BY` | `(:Decision)-[:WAS_ASSISTED_BY]->(:AIAgent)` | Which AI tool(s) assisted |
| `PROJECT_OF` | `(:Fact\|:Decision)-[:PROJECT_OF]->(:Project)` | Which project the record belongs to. Written at first write from the record's resolved project — a `:Project` node is only ever minted from a **project**, never from a section of one. A record whose project does not resolve gets no edge and no node; it is left for the repair path rather than attached to an invented default. |
| `DOMAIN_OF` | `(:Fact\|:Decision\|:Retrospective)-[:DOMAIN_OF]->(:Domain)-[:PROJECT_OF]->(:Project)` | Which SECTION(s) of its project the record sits in — multi-valued, unlike `PROJECT_OF`. Written at first write from a registry identity, never from a name. A **bare** edge is the record's own assertion; one stamped `asserted_by='inherited'` is a default derived from what the record judges, so a repair may replace an inherited edge and must never touch an asserted one. |
| `WAS_GENERATED_BY` | `(:Decision)-[:WAS_GENERATED_BY]->(:Activity)` | Which session produced it (reserved) |
| `ACTED_ON_BEHALF_OF` | `(:AIAgent)-[:ACTED_ON_BEHALF_OF]->(:Human)` | Delegation chain (reserved) |
| `SUPERSEDES` | `(:Decision)-[:SUPERSEDES]->(:Decision)` | Replaces a prior decision |
| `INFORMED_BY` | `(:Decision)-[:INFORMED_BY]->(:Decision)` | Prior decision used as input. Populated by reference resolution (Stage 1.2b) from textual cross-references in content, plus any explicit links. |
| `REFERENCES` | `(:Fact\|:Decision)-[:REFERENCES]->(:Fact\|:Decision)` | A record cross-reference resolved from content (Stage 1.2b): a context-gated pg-id mention (e.g. "refines decision 381") that resolves to a real record. `Decision→Decision` is promoted to `INFORMED_BY`; everything else is `REFERENCES`. Carries `resolved_from='content'` + `cue`. Never auto-`SUPERSEDES` (explicit-only). |
| `HAD_OUTCOME` | `(:Decision)-[:HAD_OUTCOME {date}]->(:Retrospective)` | Retro-as-record (v2): the trigger edge to the retrospective RECORD (rating/date/content live on the node). Legacy pre-conversion shape: a self-loop `(:Decision)-[:HAD_OUTCOME {rating,date,notes}]->(:Decision)` with the payload as edge props — readers accept both during the transition |
| `SUPERSEDES` | `(:CommunitySummary)-[:SUPERSEDES]->(:CommunitySummary)` | Also written between CommunitySummary nodes when supersession rule fires (v0.4.0) |
| `SUPERSEDES` | `(:Fact)-[:SUPERSEDES]->(:Fact)` | A correction supersedes an older fact (decision 381); the old `:Fact` also gets `superseded = true` so REM/NREM skip it |

### Judgement relations targeting `:Entity` — RETIRED, never minted by REM (v0.4.0 → retired E5/B1)

⛔ **These four relation types are judgement relations and are NEVER minted by
`rem_loop.py`, or by anything else, as of E5 (v0.8.60) and confirmed permanent
by the Dreaming Cycle v2 plan (§1, task B1).** They are first-write-only
properties on the `Decision` record (`metadata.decision.considered` /
`.rejected` / `.under_conditions` / `.produces_insight`, free text) and are
**never** materialized as a graph edge to an `:Entity` node — a candidate name
REM's decision-extras task proposes is unconditionally logged to
`extras_dropped` and discarded, registry-known or not. Kept here as a record of
a retired mechanism, not a current capability:

| Relationship | Pattern | Meaning (historical — never written since E5/B1) |
|---|---|---|
| `PRODUCES_INSIGHT` | `(:Fact\|:Decision)-[:PRODUCES_INSIGHT]->(:Entity)` | Insight or knowledge this fact/decision generates |
| `UNDER_CONDITIONS` | `(:Decision)-[:UNDER_CONDITIONS]->(:Entity)` | Constraints or conditions that bound the decision |
| `CONSIDERED` | `(:Decision)-[:CONSIDERED]->(:Entity)` | Alternatives evaluated for the decision |
| `REJECTED` | `(:Decision)-[:REJECTED]->(:Entity)` | Alternatives explicitly ruled out |

For the role-typed grounding edges these relation NAMES also appear on
(`(:Decision)-[:CONSIDERED\|REJECTED\|UNDER_CONDITIONS]->(:Fact\|:Decision)`,
written at first write, never by REM) — see "Typed decision grounding" below.

**Typed decision grounding (v0.6.4):** the grounding edges that link a `Decision` to the *records it rests on* are role-typed — `GROUNDED_IN` (basis), `CONSIDERED`, `REJECTED`, `UNDER_CONDITIONS`, or `INFORMED_BY`, targeting `(:Fact\|:Decision)`. First write picks the relation from the operator-supplied role (`--grounded-in "42:considered"`) or, when omitted, from the grounded fact's `fact_kind` — a `discussion` defaults to `INFORMED_BY`, other kinds to `GROUNDED_IN` (advisory, never enforced). Each edge carries an **`asserted_by`** property (`operator` \| `system_default`). The target is matched by `pg_id` **across labels**, so grounding a decision in another decision links the real node rather than an empty placeholder.

**Typed Entity→Entity relationships (REM rebuild):** the domain-layer relations
(`DEPENDS_ON`, `PART_OF`, `IMPLEMENTS`, `PRODUCES`, `CONSUMES`, `RUNS_ON`,
`CONFIGURES`, `DESCRIBES`, `VALIDATES`) are minted by the periodic **evidence
sweep** (`relation_sweep.py`), never by the per-record save or enrichment path:
candidate pairs come from co-occurrence across facts aggregated per alias
component, are legality-gated by the ontology `DOMAIN_RANGE` map in both
directions, LLM-adjudicated in batches against shared-fact evidence, and every
verdict lands in `relation_adjudications`. `MENTIONS` remains the explicit
neutral-weight fallback.

**Universal machine-edge provenance (two-axis: who asserted × how evidenced):**
every machine-minted edge carries `asserted_by` (`rem` = per-record enrichment,
`rem_sweep` = evidence sweep), `confidence` (k-vote self-consistency for REM,
adjudication score for the sweep), `model`, `run_id`, `created_at` — stamped
`ON CREATE` only, so an existing edge (in particular an operator-asserted one)
is never re-stamped; operator promotion via the review flow flips
`asserted_by` to `operator`. Pre-rebuild edges carry no `asserted_by` and are
consumed at a fixed neutral prior (era-gated legacy class — no LLM backfill).
Consolidation consumes a machine edge only when its family is CALIBRATED and
its confidence clears the family threshold; operator and legacy edges are
always consumable.

**`rem_processed` Fact property:** after REM enriches a Fact node, it sets `rem_processed = true`. NREM (`consolidation_loop.py`) requires this flag before including a Fact in a consolidation cluster — `WHERE coalesce(neighbor.rem_processed, false) = true`. A Fact whose Neo4j write is still pending in the outbox is never marked `rem_processed`.

**Entity type registry:** REM builds a closed registry from all existing typed nodes (`Human`, `AIAgent`, `Project`, `Decision`, `Entity`) before each batch. Once a name is registered (e.g. "Xenofon → Human"), every occurrence in the batch uses the same label and a compatible relationship type. The LLM cannot reclassify existing nodes.

### Retrospective write protocol (v2 — retro-as-record)

A retrospective is a **full record**: its own `technical_docs` row (content = the notes, embedded and searchable), plus a `:Retrospective` node in the graph. `POST /memory/retrospective` with:

```json
{"pg_id": 42, "rating": "validated", "notes": "Held up in prod.",
 "grounded_in": [601], "grounded_roles": {"601": "based_on"},
 "source_ref": "tests/test_outbox_ledger.py", "elicited": true,
 "agent_id": "claude_code"}
```

The coordinator validates the rating against the outcome-state enum, verifies the target `pg_id` exists, embeds the notes (hard mandate), inserts the retro's own row (inheriting the target's `project`), and writes a `neo4j_outbox` row **under the retro's own pg_id** (`type=retrospective`, `v: 2`, `target_pg_id` = the decision). The outbox worker materialises:

```cypher
MERGE (r:Retrospective {pg_id: $pg_id})            // rating, date, content snippet,
SET r.rating = ..., r.fact_kind = ...              // source, fact_kind on the node
MATCH (d:Decision {pg_id: $target})
MERGE (d)-[:HAD_OUTCOME {date: $date}]->(r)        // the TRIGGER edge
// + MENTIONS edges for elicited entities, + typed grounding ROLE edges
// (GROUNDED_IN/CONSIDERED/... with asserted_by) to the facts that MEASURED
// the outcome — a test-grounded decision gets a test-grounded retrospective.
```

Multiple retrospectives per Decision are allowed; consumers treat the **newest as the decision's current verdict**. `date` defaults to today (ISO). Legacy pre-conversion rows (no `v`) still produce the old self-loop; the one-time migration converts existing self-loops to records.

**Lifecycle:** the retro's outbox row lives the ordinary record lifecycle (`applied` → `rem_reviewed` after REM enriches the node → `consolidated` when an insight fold consumes it → deleted after the graph marking). Insight triggers key retro rows on `COALESCE(cypher_params->>'target_pg_id', pg_id)` so legacy rows (decision's pg_id) and v2 rows (retro's own pg_id) behave identically. A retro row on a decision in no insight and no qualifying cluster stays open deliberately.

**`rating` semantics:** a closed outcome-state enum — `validated` \| `mixed` \| `refined` \| `pending` \| `reversed`. `"reversed"` is structural: it marks the target decision `superseded` in `technical_docs` and on the `:Decision` node (excluded from Tier-1 search and from fresh insight clusters). States, not grades — the nuance belongs in the notes, which insight synthesis quotes. Legacy free-text ratings are preserved as `metadata.original_rating` by the migration.

### Provenance query examples

```cypher
// Who decided X on project Y?
MATCH (h:Human)-[:WAS_ATTRIBUTED_TO]-(d:Decision)-[:PROJECT_OF]->(p:Project)
      OPTIONAL MATCH (d)-[:WAS_ASSISTED_BY]->(ai:AIAgent)
WHERE toLower(d.title) CONTAINS 'consolidat'
RETURN h.name, ai.name, d.title, d.rationale, d.date, p.name

// What decisions did claude-code assist with?
MATCH (ai:AIAgent {name:'claude-code'})<-[:WAS_ASSISTED_BY]-(d:Decision)-[:PROJECT_OF]->(p:Project)
RETURN d.title, p.name, d.date ORDER BY d.date DESC

// Why-To loop: retrospectives before acting in an area
MATCH (d:Decision)-[o:HAD_OUTCOME]->()
WHERE toLower(d.title) CONTAINS 'write safety'
RETURN d.title, o.rating, o.notes, o.date ORDER BY o.date DESC LIMIT 1
```

### Decision save protocol

To create a Decision node, save with `metadata["type"] == "decision"` and a nested `decision` object. The coordinator validates the required fields at ingress (before the outbox WAL) and returns HTTP 400 if any are missing.

```json
{
  "content": "We decided to add a consolidation daemon to simulate dreaming.",
  "metadata": {
    "source": "claude-code",
    "type": "decision",
    "entities": ["Consolidator", "SharedMemory"],
    "decision": {
      "title": "Add consolidation daemon",
      "decided_by": "Xenofon",
      "project": "shared_memory",
      "rationale": "simulate dreaming; reduce hot-path latency via outbox",
      "assisted_by": ["claude-code"],
      "date": "2026-05-20",
      "alternatives_considered": ["synchronous writes", "no consolidation"],
      "confidence_at_time": 0.8
    }
  }
}
```

Required fields: `decided_by`, `project`, `rationale`. All others optional.

**Entity Ingestion Protocol:**
Supply `"entities": ["Name1", "Name2"]` in the metadata JSON when saving. The saver creates `Entity` nodes and `MENTIONS` relationships for each name. **Tier 3 consolidation no longer keys on entities** (fact 1215) — the NREM fold walks the `DOMAIN_OF`→`PROJECT_OF` spine (project+domain), so a fact saved without entities is stored, retrievable, and fully consolidatable. Entities still matter: they are the only way a new concept enters the graph, and they drive graph navigation and REM/entity-relation linking. Decision nodes also receive `MENTIONS` edges to their (inherited) entities. Optionally, `entities_provenance` (`{"<name>": "operator"|"agent"}`) stamps who named each one.

**Vector Indexes (1024 Dimensions):**
- `entity_embedding_idx`
- `fact_embedding_idx`
- `message_embedding_idx`
- `preference_embedding_idx`
- `step_embedding_idx`
- `task_embedding_idx`
