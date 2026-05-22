# Shared Memory Schema

## PostgreSQL (Semantic Memory)
**Table:** `technical_docs`
- `id`: Primary Key (Serial)
- `content`: Text content of the artifact or document.
- `metadata`: JSONB field for additional context (source, type, timestamp).
- `embedding`: Vector(1024) - BGE-M3 semantic embedding.

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
