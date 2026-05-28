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

**Indexes:** `technical_docs_embedding_idx` — `ivfflat (embedding vector_cosine_ops)`; btree indexes on `agent_id`, `scope`, `visibility`

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
| `agent_id` | `TEXT NOT NULL DEFAULT 'legacy'` | Agent that triggered consolidation; `'legacy'` for pre-coordinator rows |
| `scope` | `TEXT NOT NULL DEFAULT 'global'` | Inherited from the source `Fact` cluster's scope |
| `visibility` | `TEXT NOT NULL DEFAULT 'global'` | Read policy, same semantics as `technical_docs.visibility` |

**`metadata` structure (written by `consolidation_loop.py`):**
```json
{
  "type": "community_summary",
  "entity": "<Entity.name that anchors the cluster>",
  "source_pg_ids": [<list of technical_docs.id values that were consolidated>],
  "timestamp": "<ISO-8601 datetime of this consolidation run>"
}
```

> **Note:** `source_pg_ids` is stored both as the dedicated column above and inside `metadata` JSONB. The column is the authoritative query path; the JSONB key is retained for backwards compatibility with tooling that reads raw metadata.

**Indexes:** `community_summaries_embedding_idx` — `ivfflat (embedding vector_cosine_ops)`; btree indexes on `agent_id`, `scope`, `visibility`

**Retrieval role:** queried first on every search — top-1 cosine match is prepended to results as "Global Context Summary" to orient the response before the Tier 1 vector search runs.

**Growth behaviour:** each consolidation cycle appends a **new row**. Superseded summaries are never deleted or marked inactive — they accumulate alongside newer ones. Query `ORDER BY id DESC LIMIT 1` to get the latest summary for a given entity, or use the retrieval path which surfaces the embedding-closest match regardless of age.

---

### `neo4j_outbox` — Coordinator outbox (cross-DB atomicity)

Written by the coordinator on every save, in the same Postgres transaction as `technical_docs`. Applied asynchronously to Neo4j by the outbox worker. Survives crashes and Neo4j outages — pending rows are replayed on restart.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | Monotonic outbox entry ID |
| `pg_id` | `BIGINT NOT NULL` | References the `technical_docs.id` row this write belongs to |
| `cypher_params` | `JSONB NOT NULL` | Parameters passed to the Neo4j Cypher write (content snippet, source, entities, etc.) |
| `status` | `TEXT NOT NULL DEFAULT 'pending'` | `pending` → `applied` or `failed` |
| `retries` | `INT NOT NULL DEFAULT 0` | Incremented on each failed application attempt |
| `created_at` | `TIMESTAMPTZ DEFAULT now()` | When the outbox row was written |
| `applied_at` | `TIMESTAMPTZ` | Set when status transitions to `applied` |

**Index:** partial btree on `status WHERE status = 'pending'` — keeps the worker scan O(backlog), not O(table).

**Consistency note:** after a save returns success (Postgres committed), the corresponding Neo4j `Fact` node may not yet exist — the outbox worker applies it asynchronously. `graph` queries immediately after a save use `?consistency=neo4j` to block until the outbox row is applied.

---

## Neo4j (Relational Memory)

> Configurable via `ontology.yaml`. Label and relationship keys map directly to the `labels:` and `relationships:` sections.

### Core labels (Tier 1 / Tier 3 nodes)

| Label | Written by | Purpose |
|---|---|---|
| `Fact` | Outbox worker (on every save) | Primary node — one per `technical_docs` row, keyed by `pg_id` |
| `Entity` | Outbox worker | Named entity extracted from `metadata["entities"]`; anchors consolidation clusters |
| `CommunitySummary` | Consolidation daemon | Synthesised thematic narrative — one per Entity hub |
| `ReasoningTrace` | Agent (via `archive_reasoning_trace`) | Root of a reasoning session |
| `ReasoningStep` | Agent (via `archive_reasoning_trace`) | Individual step within a trace |

### Provenance labels (Phase A — PROV-O inspired)

Written by the outbox worker when `metadata["type"] == "decision"`.

| Label | Purpose |
|---|---|
| `Decision` | An architectural or design decision — keyed by `pg_id`, links to all PROV-O edges |
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
| `SUMMARIZED_BY` | `(:Fact)-[:SUMMARIZED_BY]->(:CommunitySummary)` | Consolidation daemon after synthesis |
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
