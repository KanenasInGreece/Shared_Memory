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
| `visibility` | `TEXT NOT NULL DEFAULT 'global'` | Read policy: `'global'` \| `'scope'` \| `'private'` |
| `superseded` | `BOOLEAN NOT NULL DEFAULT false` | Decision-level reversal flag (decision pg_id 276). Set when a retrospective with `rating="reversed"` lands on the row; mirrored as `superseded = true` on the graph `:Decision` node. Tier-1 search excludes superseded rows; reversed decisions never seed a fresh insight cluster (re-folds keep them as boundary evidence). Added by migration 009. |

**Indexes:** `technical_docs_embedding_idx` — `ivfflat (embedding vector_cosine_ops)`; btree indexes on `agent_id`, `scope`, `visibility`

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

Written exclusively by the consolidation daemon. Each row is an LLM-synthesised narrative that distils a dense cluster of facts grouped around a shared `Entity` hub in Neo4j. Never written directly by agents.

| Column | Type | Notes |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | Referenced as `pg_id` on the matching `CommunitySummary` node in Neo4j |
| `content` | `TEXT NOT NULL` | LLM-generated cumulative narrative for the entity cluster |
| `metadata` | `JSONB` | Written by the daemon — see structure below |
| `embedding` | `vector(1024)` | BGE-M3 embedding of the synthesised `content`; used for top-1 retrieval |
| `source_pg_ids` | `INTEGER[]` | IDs of `technical_docs` rows that contributed to this summary. Added by migration 003; back-filled from `metadata` for existing rows. Enables `WHERE $fact_id = ANY(source_pg_ids)` provenance queries without JSON parsing. |
| `summary_history` | `JSONB NOT NULL DEFAULT '[]'` | Append-only array of previous summaries, capped at 20. Written by the daemon on every `DO UPDATE` — the outgoing content, `source_pg_ids`, and `timestamp` are pushed to the array before the row is overwritten. Enables drift auditing without a temporal schema. Added by migration 004. |
| `superseded` | `BOOLEAN NOT NULL DEFAULT false` | Set to `true` when another summary's `source_pg_ids` is a strict superset of this row's `source_pg_ids` — the newer summary subsumes this one. Retrieval (`coordinator.py`) always filters `WHERE NOT superseded`. Partial index `community_summaries_active_idx` keeps this scan fast. Added by migration 006. |
| `agent_id` | `TEXT NOT NULL DEFAULT 'legacy'` | Agent that triggered consolidation; `'legacy'` for pre-coordinator rows |
| `scope` | `TEXT NOT NULL DEFAULT 'global'` | Inherited from the source `Fact` cluster's scope |
| `visibility` | `TEXT NOT NULL DEFAULT 'global'` | Read policy, same semantics as `technical_docs.visibility` |

**`metadata` structure (written by `consolidation_loop.py`):**
```json
{
  "type": "community_summary",
  "kind": "thematic",
  "entity": "<Entity.name that anchors the cluster>",
  "domain": "<COALESCE(project, domain, scope, 'general')>",
  "source_pg_ids": [<list of technical_docs.id values that were consolidated>],
  "timestamp": "<ISO-8601 datetime of this consolidation run>"
}
```

**Insight rows (`kind: "insight"`, decision pg_id 276):** the second consolidation path folds cross-project *decision* clusters. Same table, distinguished by metadata — `kind: "insight"`, `domain: "insight"`, a `projects` array, and `source_pg_ids` containing **decision** ids (disjoint from fact ids, so the two kinds can never supersede each other). Insight rows are **always-INSERT**: they are exempt from the `(entity, domain)` unique upsert (partial index, migration 009) and rely on supersession for dedup — a re-fold on the same source set writes a fresh row that supersedes the old one.

> **Note:** `source_pg_ids` is stored both as the dedicated column above and inside `metadata` JSONB. The column is the authoritative query path; the JSONB key is retained for backwards compatibility with tooling that reads raw metadata.

**Indexes:** `community_summaries_embedding_idx` — `ivfflat (embedding vector_cosine_ops)`; btree indexes on `agent_id`, `scope`, `visibility`

**Retrieval role:** queried first on every search — top-1 cosine match is prepended to results as "Global Context Summary" to orient the response before the Tier 1 vector search runs.

**Growth behaviour (thematic rows):** one row per `(entity, domain)`, keyed by `metadata->>'entity'` + `metadata->>'domain'` (partial unique index `community_summaries_entity_domain_unique`, migrations 007 + 009 — insight rows exempt). Each consolidation cycle replaces the existing row via `ON CONFLICT DO UPDATE` — the new LLM synthesis overwrites `content` and `embedding`, while the previous `content` is appended to `summary_history` (capped at 20 entries). The row ID (`id`) is stable across updates. Insight rows instead accumulate as inserts and retire via supersession. Retrieval surfaces the embedding-closest `WHERE NOT superseded` match per kind — insight first, then thematic.

**Supersession rule:** if summary A's `source_pg_ids` is **covered by** (subset of, or equal to) summary B's `source_pg_ids`, A is superseded by B. The equal-set case is how an insight re-fold replaces its predecessor. The consolidation daemon sets `A.superseded = true` in Postgres and writes `(B)-[:SUPERSEDES]->(A)` in Neo4j. Cross-entity supersession is supported — an "Outbox" summary can supersede a "Neo4j" summary if it absorbed all the same source facts; insight and thematic rows never collide because their source id spaces (decisions vs facts) are disjoint.

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

### Provenance labels (Phase A — PROV-O inspired)

Written by the outbox worker when `metadata["type"] == "decision"`.

| Label | Purpose |
|---|---|
| `Decision` | An architectural or design decision — keyed by `pg_id`, links to all PROV-O edges. Lifecycle flags: `rem_processed` (REM enrichment done), `consolidated` (folded into an insight), `superseded` (reversed via `rating="reversed"`). |
| `Human` | A person who owns or makes a decision (`decided_by` field) |
| `AIAgent` | An AI tool that assisted in the decision (`assisted_by` list) |
| `Project` | Project scope — root node for decisions and milestones |
| `Activity` | A work session or task context (reserved; not yet written automatically) |
| `Milestone` | A significant achievement marker (reserved; not yet written automatically) |

### Core relationships

| Relationship | Pattern | Written by |
|---|---|---|
| `MENTIONS` | `(:Fact)-[:MENTIONS]->(:Entity)` | Outbox worker — from `metadata["entities"]`; required for consolidation clustering |
| `REPORTS_ON` | `(:Fact)-[:REPORTS_ON]->(:Entity)` | Legacy alias; accepted by consolidation query. Use `MENTIONS` for new saves. |
| `SUMMARIZED_BY` | `(:Fact\|:Decision)-[:SUMMARIZED_BY]->(:CommunitySummary)` | Consolidation daemon after synthesis (Decision source = insight fold) |
| `NEXT_STEP` | `(:ReasoningStep)-[:NEXT_STEP]->(:ReasoningStep)` | Agent — links consecutive steps in a trace |

### Provenance relationships (Phase A — PROV-O inspired)

Written by the outbox worker for `type:decision` saves.

| Relationship | Pattern | Meaning |
|---|---|---|
| `WAS_ATTRIBUTED_TO` | `(:Decision)-[:WAS_ATTRIBUTED_TO]->(:Human)` | Who owns the decision |
| `WAS_ASSISTED_BY` | `(:Decision)-[:WAS_ASSISTED_BY]->(:AIAgent)` | Which AI tool(s) assisted |
| `PROJECT_OF` | `(:Decision)-[:PROJECT_OF]->(:Project)` | Which project the decision belongs to |
| `WAS_GENERATED_BY` | `(:Decision)-[:WAS_GENERATED_BY]->(:Activity)` | Which session produced it (reserved) |
| `ACTED_ON_BEHALF_OF` | `(:AIAgent)-[:ACTED_ON_BEHALF_OF]->(:Human)` | Delegation chain (reserved) |
| `SUPERSEDES` | `(:Decision)-[:SUPERSEDES]->(:Decision)` | Replaces a prior decision |
| `INFORMED_BY` | `(:Decision)-[:INFORMED_BY]->(:Decision)` | Prior decision used as input |
| `HAD_OUTCOME` | `(:Decision)-[:HAD_OUTCOME {rating,date,notes}]->()` | Retrospective — dated edge property, not a node |
| `SUPERSEDES` | `(:CommunitySummary)-[:SUPERSEDES]->(:CommunitySummary)` | Also written between CommunitySummary nodes when supersession rule fires (v0.4.0) |

### REM-enrichment relationships (v0.4.0 — written by `rem_loop.py`)

Written by the REM daemon during idle-time fact enrichment. These relationships are **never** written by the save path — they require full Postgres content and the closed typed-node set.

| Relationship | Pattern | Meaning |
|---|---|---|
| `PRODUCES_INSIGHT` | `(:Fact\|:Decision)-[:PRODUCES_INSIGHT]->(:Entity)` | Insight or knowledge this fact/decision generates |
| `UNDER_CONDITIONS` | `(:Decision)-[:UNDER_CONDITIONS]->(:Entity)` | Constraints or conditions that bound the decision |
| `CONSIDERED` | `(:Decision)-[:CONSIDERED]->(:Entity)` | Alternatives evaluated for the decision |
| `REJECTED` | `(:Decision)-[:REJECTED]->(:Entity)` | Alternatives explicitly ruled out |

**`rem_processed` Fact property:** after REM enriches a Fact node, it sets `rem_processed = true`. NREM (`consolidation_loop.py`) requires this flag before including a Fact in a consolidation cluster — `WHERE coalesce(neighbor.rem_processed, false) = true`. A Fact whose Neo4j write is still pending in the outbox is never marked `rem_processed`.

**Entity type registry:** REM builds a closed registry from all existing typed nodes (`Human`, `AIAgent`, `Project`, `Decision`, `Entity`) before each batch. Once a name is registered (e.g. "Xenofon → Human"), every occurrence in the batch uses the same label and a compatible relationship type. The LLM cannot reclassify existing nodes.

### Retrospective write protocol

To record an outcome on an existing Decision, `POST /memory/retrospective` with:

```json
{"pg_id": 42, "rating": "high", "notes": "Held up in prod.", "agent_id": "claude_code"}
```

The coordinator verifies the `pg_id` exists in `technical_docs`, then writes a `neo4j_outbox` row with `type=retrospective`. The outbox worker issues:

```cypher
MATCH (d:Decision {pg_id: $pg_id})
CREATE (d)-[:HAD_OUTCOME {rating: $rating, date: $date, notes: $notes}]->(d)
```

Self-loop pattern — each call creates a new dated edge; multiple retrospectives per Decision are allowed. `date` defaults to today (ISO) if omitted.

**Two roles, one record (decision pg_id 276):** the `HAD_OUTCOME` edge is the **permanent outcome archive** — insight folds read every edge's wording verbatim, including on cumulative re-folds. The outbox row is only the **trigger**: while it is open (`applied`/`rem_reviewed`), the retrospective has not been folded into an insight yet; the fold consumes it (flips to `consolidated`, then deletes after the graph marking). A retro row on a decision in no insight and no qualifying cluster stays open deliberately.

**`rating` semantics:** free text, with one structural value — `"reversed"` marks the target decision `superseded` in `technical_docs` and on the `:Decision` node (excluded from Tier-1 search and from fresh insight clusters). Every other rating carries no enum meaning; its wording reaches Tier 3 through the insight narrative.

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
Supply `"entities": ["Name1", "Name2"]` in the metadata JSON when saving. The saver creates `Entity` nodes and `MENTIONS` relationships for each name. Facts saved without entities are stored and retrievable but are **never eligible for consolidation** — the daemon clusters only via `Entity` hubs. Decision nodes also receive `MENTIONS` edges to their entities.

**Vector Indexes (1024 Dimensions):**
- `entity_embedding_idx`
- `fact_embedding_idx`
- `message_embedding_idx`
- `preference_embedding_idx`
- `step_embedding_idx`
- `task_embedding_idx`
