# Shared Memory Schema

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

**Index:** `technical_docs_embedding_idx` — `ivfflat (embedding vector_cosine_ops)`

---

### `community_summaries` — Tier 3 (Semantic)

Written exclusively by the consolidation daemon. Each row is an LLM-synthesised narrative that distils a dense cluster of facts grouped around a shared `Entity` hub in Neo4j. Never written directly by agents.

| Column | Type | Notes |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | Referenced as `pg_id` on the matching `CommunitySummary` node in Neo4j |
| `content` | `TEXT NOT NULL` | LLM-generated cumulative narrative for the entity cluster |
| `metadata` | `JSONB` | Written by the daemon — see structure below |
| `embedding` | `vector(1024)` | BGE-M3 embedding of the synthesised `content`; used for top-1 retrieval |

**`metadata` structure (written by `consolidation_loop.py`):**
```json
{
  "type": "community_summary",
  "entity": "<Entity.name that anchors the cluster>",
  "source_pg_ids": [<list of technical_docs.id values that were consolidated>],
  "timestamp": "<ISO-8601 datetime of this consolidation run>"
}
```

**Index:** `community_summaries_embedding_idx` — `ivfflat (embedding vector_cosine_ops)`

**Retrieval role:** queried first on every search — top-1 cosine match is prepended to results as "Global Context Summary" to orient the response before the Tier 1 vector search runs.

**Growth behaviour:** each consolidation cycle appends a **new row**. Superseded summaries are never deleted or marked inactive — they accumulate alongside newer ones. Query `ORDER BY id DESC LIMIT 1` to get the latest summary for a given entity, or use the retrieval path which surfaces the embedding-closest match regardless of age.

## Neo4j (Relational Memory)
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
