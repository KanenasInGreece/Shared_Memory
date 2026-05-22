# Documentation — Oratotis Shared Memory Framework

This folder contains the complete written record of the Shared Memory Framework: its design rationale, operational history, architectural decisions, and published articles. Everything here is retrospective — the live operational interface is `SKILL.md` one level up.

---

## Files

### Design & Architecture

**[dreaming-cycle-v6.md](dreaming-cycle-v6.md)**
The primary design article: *"Why Your AI Workstation Needs to Dream."* Covers the interference problem (BGE-M3 effective dimensionality, power-law retrieval degradation), the three-tier memory model (episodic/structural/semantic), why centroid averaging fails geometrically, and how the consolidation loop implements the hippocampus/neocortex split. Read this first before changing anything in `consolidation_loop.py`.

**[ADR.md](ADR.md)**
Architecture Decision Records — a sequential log of every structural decision made to the framework since its first deployment. Each entry states the context that forced the decision, what was decided, what was rejected and why, and what consequences followed. Entries are numbered and dated; the most recent is always at the top.

**[schema.md](schema.md)**
The database schema: PostgreSQL tables (`technical_docs`, `community_summaries`), Neo4j labels, relationships, and the entity ingestion protocol. The entity ingestion section is operationally critical — facts saved without `entities` are retrievable but never eligible for Tier 3 consolidation.

---

### Operational History

**[After_Dreaming.md](After_Dreaming.md)**
A continuation of the design article. Documents three silent failures that prevented consolidation from ever running in production despite correct design: `pg_notify` was never sent, `Entity` nodes were never created, and the consolidation daemon called the embedding service directly instead of through the gateway. Documents the fixes, the ingestion contract they introduce, and the entity resolution gap that remains open. Updated as the architecture evolves.

**[proxy_implementation.md](proxy_implementation.md)**
The complete development history of `hive_mind_proxy.py` across all versions (v2 → v6). Documents every bug found in the initial async rewrite, every change made by Gemini's parallel implementations, and the four-case architectural audit that produced the final v6 design. Includes a rejected-decisions table. Read before modifying any proxy behavior.

---

### Published Articles

**[SHARED_MEMORY_ARTICLE.md](SHARED_MEMORY_ARTICLE.md)**
Markdown source for the first LinkedIn article in the series: *"Shared Memory Framework for Your Smart Tools."* Describes the original two-tier architecture (pgvector + Neo4j), the agent access split, and the motivation for a shared embedding space.

**[shared-memory-framework.html](shared-memory-framework.html)**
HTML render of the first article, formatted for publication.

**[industry-semantic-contextual-memory-report.html](industry-semantic-contextual-memory-report.html)**
A broader industry research report on semantic and contextual memory architectures. Background reading; not part of the operational framework documentation.

---

## Reading Order

For someone new to the codebase, the intended reading sequence is:

1. `SHARED_MEMORY_ARTICLE.md` — what the framework is and why it exists
2. `dreaming-cycle-v6.md` — why consolidation is necessary and how it works
3. `schema.md` — the concrete data structures that implement the above
4. `After_Dreaming.md` — what was wrong in the initial implementation and how it was fixed
5. `ADR.md` — the sequence of structural decisions, most recent first
6. `proxy_implementation.md` — the gateway's full development history (proxy-specific only)
