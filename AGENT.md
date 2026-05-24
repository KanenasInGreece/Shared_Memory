# AGENT.md

Guidance for AI coding agents (Gemini CLI, and others) working in this repository.

## What This Repository Is

The **Shared Memory Framework** — a three-tier semantic memory system that lets Gemini CLI, LM Studio, and other AI tools share a single persistent memory backend (Postgres + pgvector + Neo4j). All credentials are read from environment variables; no secrets are hardcoded.

## Commands

```bash
# Run all tests (from repo root — test files use relative dynamic imports)
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx --with neo4j pytest tests/ -v

# Run a single test file or single test case
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx --with neo4j pytest tests/test_vector_skill.py
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx --with neo4j pytest tests/test_vector_skill.py::test_mcp_save_artifact_success

# Start Hive-Mind Gateway (also auto-starts the consolidation daemon)
uv run --with aiohttp python shared-memory/scripts/hive_mind_proxy.py 8888

# CLI memory bridge (requires gateway to be running for embed/save)
uv run --with httpx --with psycopg2-binary --with neo4j \
  python shared-memory/scripts/memory_bridge.py search "<query>" 5
uv run --with httpx --with psycopg2-binary --with neo4j \
  python shared-memory/scripts/memory_bridge.py save "<content>" '{"source":"agent","entities":["Entity1"]}'
uv run --with httpx --with psycopg2-binary --with neo4j \
  python shared-memory/scripts/memory_bridge.py graph "MATCH (n:Entity) RETURN n LIMIT 10"
```

Set `MOCK_LLM=1` to bypass LLM calls in consolidation tests. Tests are fully mocked — no live infrastructure needed.

## Architecture

### Agent Access Split

| Consumer | Interface | Entry point |
|---|---|---|
| Gemini CLI, other CLI agents | CLI only | `shared-memory/scripts/memory_bridge.py` |
| LM Studio | MCP (FastMCP) | `vector-skill.py` |

`vector-skill.py` is registered in `mcp.json` for LM Studio — it is **not** used by CLI-based agents.

### Three-Tier Storage

| Tier | Store | Role |
|---|---|---|
| 1 — Episodic | Postgres `technical_docs` + pgvector | Original facts, full content, surgical precision |
| 2 — Structural | Neo4j `Fact` nodes (`pg_id`, `consolidated`) | Relationships, provenance |
| 3 — Semantic | Postgres `community_summaries` | LLM-synthesised thematic narratives |

Retrieval queries **Tier 3 first** (thematic orientation), then Tier 1 (precision), then expands through Neo4j. Artifacts saved by one agent become retrievable by all others once consolidation runs.

### Hive-Mind Gateway (`hive_mind_proxy.py`)

Async aiohttp reverse proxy on port 8888:
- `POST /v1/embeddings` → BGE-M3 on port 8070
- `POST /v1/reranking` → BGE-Reranker-v2-m3 on port 8071
- everything else → reasoning LLM on port 5000

On startup it spawns `consolidation_loop.py` as a subprocess. Stopping the proxy (Ctrl+C) also stops the daemon.

### Consolidation Daemon (`consolidation_loop.py`)

Triggered by Postgres `LISTEN/NOTIFY` on the `new_artifact` channel. After a 15-minute idle window (or a 45-min hard backstop), it inspects Neo4j for Entity hub clusters where unconsolidated Fact nodes exceed `DENSITY_THRESHOLD = 5`. For each qualifying community it calls the LLM to synthesise a cumulative narrative, re-embeds it with BGE-M3, and writes the result to `community_summaries`.

**Facts saved without `"entities"` in metadata are never eligible for consolidation.**

## Key Invariants

- **1024-dim via :8888 only** — always route embedding calls through the gateway. Never call 8070 or 8071 directly.
- **Hard embedding mandate** — saves abort if the gateway is unreachable. An orphaned row without a vector is invisible to semantic search.
- **SHA-256 idempotency** — `ON CONFLICT (content_hash) DO UPDATE`. Safe to re-save identical content.
- **Cross-DB atomicity is an accepted risk** — a Neo4j write followed by a Postgres commit failure creates a dangling Fact node. See `shared-memory/Documentation/ADR.md`.

## Configuration

Copy `.env.example` to `.env` and fill in `NEO4J_PASSWORD` and `PG_PASSWORD`. For `mcp.json`, replace all `YOUR_*` placeholders and update the absolute path to `vector-skill.py`.

## Documentation

`README.md` is the primary reference — architecture, setup, save path, consolidation cycle, retrieval chain, open problems, and full agent integration instructions.

`shared-memory/Documentation/schema.md` — full Postgres + Neo4j schema with all labels and relationship types.

## Licensing

This repository is licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) for the full text.

When contributing or building on this work:
- Retain the copyright notice and licence header in any derived files.
- If you distribute a modified version, state that changes were made.
- Attribution to the original author is appreciated: **Xenofon S. Motsenigos**.

Do not introduce dependencies with licences incompatible with Apache 2.0 (e.g. GPL) without explicit discussion.
