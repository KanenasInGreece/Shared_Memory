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

**Core Labels:**
- `Project`: Root node for a project.
- `File`: A file within a project.
- `Concept`: An abstract idea or technical component.
- `Entity`: Generic POLE (Person, Object, Location, Event).
- `Fact`: A specific technical truth or decision.

**Common Relationships:**
- `(:File)-[:BELONGS_TO]->(:Project)`
- `(:Concept)-[:IMPLEMENTED_IN]->(:File)`
- `(:Fact)-[:VALID_FOR]->(:Project)`
- `(:Entity)-[:PARTICIPATES_IN]->(:Event)`
- `(:Fact)-[:MENTIONS]->(:Entity)` — created at save time from `metadata["entities"]`; required for consolidation clustering
- `(:Fact)-[:REPORTS_ON]->(:Entity)` — alias accepted by consolidation query; use MENTIONS for new saves
- `(:Fact)-[:SUMMARIZED_BY]->(:CommunitySummary)` — written by consolidation daemon after synthesis

**Entity Ingestion Protocol:**
Supply `"entities": ["Name1", "Name2"]` in the metadata JSON when saving. The saver creates `Entity` nodes and `MENTIONS` relationships for each name. Facts saved without entities are stored and retrievable but are **never eligible for consolidation** — the daemon clusters only via `Entity` hubs.

**Vector Indexes (1024 Dimensions):**
- `entity_embedding_idx`
- `fact_embedding_idx`
- `message_embedding_idx`
- `preference_embedding_idx`
- `step_embedding_idx`
- `task_embedding_idx`
