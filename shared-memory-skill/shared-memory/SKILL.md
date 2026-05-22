# Shared Memory (Hive-Mind)

## Overview
This skill provides a bridge to the Shared Memory Framework. It enables persistence of technical decisions, entity relationships, and document embeddings across sessions and different agents.

## Core Tasks

### 1. High-Precision Retrieval (Search & Rerank)
Search the shared memory with semantic similarity and secondary reranking for maximum relevance.
- **Trigger:** When looking for past technical solutions, documentation, or specific facts.
- **Primary Method (MCP):** Use the `hybrid_search_and_rerank` tool from the `rag-orchestrator` MCP server.
- **Secondary Method (CLI):**
  1. Execute: `uv run --with httpx --with psycopg2-binary --with neo4j python scripts/memory_bridge.py search "<query>" 5`

### 2. Relational Querying (Neo4j)
Run Cypher queries against the knowledge graph to understand dependencies.
- **Trigger:** When needing to understand project structure, file dependencies, or entity relationships.
- **Workflow:**
  1. Formulate a Cypher query (refer to [schema.md](Documentation/schema.md) for labels).
  2. Execute: `uv run --with neo4j python scripts/memory_bridge.py graph "<cypher_query>"`

### 3. Artifact Persistence (Save)
Commit new findings or technical summaries to the vector store.
- **Trigger:** At the conclusion of a task or when a new "Fact" is established.
- **Workflow:**
  1. Prepare the content string and metadata JSON. **Always include `"entities"`** — a list of 1–4 named concepts the fact is about (components, systems, decisions). Without entities, the fact is stored but never eligible for Tier 3 consolidation.
  2. Execute: `uv run --with httpx --with psycopg2-binary --with neo4j python scripts/memory_bridge.py save "<content>" '{"source":"<name>","entities":["EntityA","EntityB"]}'`

- **What happens on save:**
  1. Embeds content via BGE-M3 (through gateway :8888)
  2. Upserts into Postgres `technical_docs` (idempotent by SHA-256 hash)
  3. Checks `pg_stat_activity` for a live `consolidation_daemon` connection — warns if not found
  4. Fires `pg_notify('new_artifact', ...)` — wakes the consolidation daemon
  5. Creates/merges `Fact` node in Neo4j, then `Entity` nodes + `MENTIONS` edges for each entity name

> **Note:** If the save response contains `WARNING: Consolidation daemon not running`, the artifact is stored but Tier 3 consolidation will not run. Start the Hive-Mind Gateway (see below) — it auto-starts the daemon.

## Infrastructure & Engine (Admin)

### 🛰️ Hive-Mind Gateway + Consolidation Daemon (single command)
Starts both the async reverse proxy and the consolidation daemon together. Must be running before any embed/rerank/save operation.
- **Command:** `uv run --with aiohttp python scripts/hive_mind_proxy.py 8888`

The proxy spawns `consolidation_loop.py` automatically on startup. You will see both confirmation lines in the log:
```
INFO  ### Hive-Mind Proxy on :8888 [aiohttp]
INFO  Consolidation daemon started (pid XXXXX)
INFO  Listening for 'new_artifact' notifications...
```
Stopping the proxy (Ctrl+C) also stops the daemon. No separate daemon management needed.

### 🚀 MCP Orchestrator
If the MCP server is not responding in LM Studio/Claude, restart it from here:
- **Command:** `uv run --with mcp --with httpx --with psycopg2-binary python /path/to/your/vector-skill.py`

## Reference
- **Full documentation:** See the project [README.md](../../README.md) — architecture, save path, consolidation cycle, retrieval chain, and open problems.
- **Database Schema:** Neo4j labels, relationship types, and Postgres tables: [schema.md](Documentation/schema.md).
- **Standardization:** All embedding and reranking requests route through the **Hive-Mind Gateway (Port 8888)**. Never call port 8070 (BGE-M3) or 8071 (BGE-Reranker-v2-m3) directly — the gateway enforces 1024-dim consistency and unified routing for all agents.
