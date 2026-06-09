# AGENT.md

Guidance for AI coding agents (Claude Code, Codex CLI, Grok, Antigravity/Gemini CLI, LM Studio, and others) working in this repository.

## What This Repository Is

The **Shared Memory Framework** — a three-tier semantic memory shared by every local AI tool through one gateway (Postgres + pgvector + Neo4j). All credentials are read from `.env`; no secrets are hardcoded. Current version **v0.4.2**: two-phase REM/NREM sleep cycle, per-agent token auth, summary supersession, domain-scoped consolidation, Tier-3 trace-back pointers, and GPU-aware dreaming.

## Commands

```bash
# Run all tests from repo root (fully mocked — no live infra; MOCK_LLM=1 bypasses LLM)
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary \
  --with httpx --with neo4j --with asyncpg --with aiohttp pytest tests/ -v

# Start the gateway — also launches coordinator + REM + NREM daemons; loads .env automatically
uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with psycopg2-binary \
  python shared-memory/scripts/hive_mind_proxy.py 8888

# Apply schema migrations (idempotent; run after clone or when migrations/ gains files)
uv run --with psycopg2-binary python shared-memory/migrations/apply.py

# CLI memory bridge (thin HTTP client; gateway must be running)
uv run --with httpx --with python-dotenv python shared-memory/scripts/memory_bridge.py search "<query>" 5
uv run --with httpx --with python-dotenv python shared-memory/scripts/memory_bridge.py save "<content>" '{"source":"agent","entities":["Entity1"],"project":"<domain>"}'
uv run --with httpx --with python-dotenv python shared-memory/scripts/memory_bridge.py save_decision --title "..." --decided-by "..." --project "..." --rationale "..."
uv run --with httpx --with python-dotenv python shared-memory/scripts/memory_bridge.py save_retrospective --pg-id N --rating high --notes "..."
uv run --with httpx --with python-dotenv python shared-memory/scripts/memory_bridge.py graph "MATCH (n:Entity) RETURN n LIMIT 10"
uv run --with httpx --with python-dotenv python shared-memory/scripts/memory_bridge.py doctor   # check client↔gateway api_version compatibility
```

The skill is a **thin client** — only `memory_bridge.py` ships with it. After a client or SKILL.md change, run `bash shared-memory/scripts/sync_skills.sh` (add `--prune` to clear daemons older installs left in skill dirs). The daemons are **server-side**: changes to them or to `migrations/` deploy on the gateway host via `git pull` + `migrations/apply.py` + restart — never via a skill sync. See `shared-memory/Documentation/server-setup.md`.

## Architecture

### Agent Access Split

| Consumer | Interface | Entry point |
|---|---|---|
| Claude Code | CLI skill (`/shared-memory`) | `~/.claude/skills/shared-memory/scripts/memory_bridge.py` |
| Codex CLI | CLI skill (`$shared-memory`) | `~/.codex/skills/shared-memory/scripts/memory_bridge.py` |
| Grok | CLI skill (`/shared-memory`) | `~/.grok/skills/shared-memory/scripts/memory_bridge.py` |
| Antigravity / Gemini CLI | CLI skill (`/activate shared-memory`) | `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` |
| LM Studio | MCP (FastMCP) | `vector-skill.py` → `rag-orchestrator` in `mcp.json` |

`vector-skill.py` (MCP) is for LM Studio only; CLI agents use `memory_bridge.py`, a thin HTTP client that delegates all storage to the coordinator (needs only `httpx` + `python-dotenv`).

**Client ↔ gateway version contract.** `memory_bridge.py` (and `vector-skill.py`'s `/memory/*` calls) send the `X-SM-Api-Version` header; the gateway reports `api_version` on `GET /health` and logs any skew. `API_VERSION` lives in both `coordinator.py` and `memory_bridge.py` — bump them together on a breaking protocol change. Never copy daemons into a skill dir to "match versions"; the contract, not file parity, governs compatibility. (ADR-014)

**No separate graph MCP (e.g. neo4j-agent-memory).** Direct-bolt Neo4j servers bypass the coordinator's per-entity locks, outbox atomicity, and SHA-256 dedup — producing orphaned nodes invisible to search. `rag-orchestrator` already expands Neo4j on every search.

### Three-Tier Storage

| Tier | Store | Role |
|---|---|---|
| 1 — Episodic | Postgres `technical_docs` + pgvector | Original facts, full content |
| 2 — Structural | Neo4j `Fact` (`pg_id`, `consolidated`, `rem_processed`) | Relationships, provenance |
| 3 — Semantic | Postgres `community_summaries` (keyed on **entity + domain**) | LLM-synthesised narratives |

Retrieval queries **Tier 3 first**, then Tier 1 (vector + rerank), then Neo4j expansion. Tier-3 results carry `source_pg_ids` so a narrative traces back to its source facts. Saves by one agent become retrievable by all once consolidation runs.

### Hive-Mind Gateway + Coordinator (`hive_mind_proxy.py` + `coordinator.py`)

Async aiohttp on `:8888`; **all routes require `Authorization: Bearer <token>` when `AGENT_TOKENS` is set**.
- `/memory/save|search|graph`, `/memory/decision`, `/memory/retrospective`, `GET /memory/status/{pg_id}` → `coordinator.py`
- `/v1/embeddings` → BGE-M3 `:8070` · `/v1/reranking` → reranker `:8071` · everything else → reasoning LLM `:5000`

Startup launches the coordinator (asyncpg pool, per-entity locks, outbox worker) plus the REM and NREM daemons; the gateway auto-restarts both on crash (circuit breaker after 5 crashes / 10 min — restart the gateway to reset). `GET /health` reports backend + daemon liveness, `auth_required`, and the gateway `version` / `api_version`.

### Sleep cycle — REM (`rem_loop.py`) + NREM (`consolidation_loop.py`)

- **REM** polls every 120 s, enriches the oldest un-enriched `Fact`s (LLM summary + typed entity relationships), sets `rem_processed=true`, and notifies NREM. A fact must pass REM before NREM can consolidate it.
- **NREM** waits on `LISTEN/NOTIFY` with a 15-min idle timer (45-min hard backstop). It consolidates Entity-hub clusters of ≥5 `rem_processed` facts, **partitioned per `(entity, domain)`** where `domain = COALESCE(metadata->>'project', metadata->>'domain', scope, 'general')`. It writes cumulative `community_summaries` and supersedes any summary whose `source_pg_ids` is a strict subset.
- Both phases yield to active writes (`WRITE_QUIESCE_SEC`, default 30 s) **and** to a busy inference GPU (via `nvtop --snapshot`, cross-vendor; nvtop is a prerequisite but fails open). Deferrals log at **WARNING**; NREM's hard backstop is never blocked.

**Facts saved without `metadata.entities` are stored and searchable but never consolidated.** Tag `project`/`domain` so consolidation stays domain-coherent.

## Key Invariants

- **1024-dim via :8888 only** — never call 8070/8071 directly.
- **Per-agent auth** — each agent's `.env` holds its own `AGENT_TOKEN` matching one entry in the gateway's `AGENT_TOKENS`; tokens are never shared.
- **Hard embedding mandate** — saves abort (503) if the gateway/embedder is unreachable; an unvectored row is invisible to search.
- **SHA-256 idempotency** — `ON CONFLICT (content_hash) DO UPDATE`. Safe to re-save identical content.
- **Outbox atomicity** — every save writes a `neo4j_outbox` row in the same Postgres transaction; the worker applies Neo4j asynchronously (eliminates ADR-001 dangling-Fact risk).
- **`entities` required for consolidation; `project`/`domain` scopes it.**
- **Health check before saves** — `GET :8888/health` → `status: ok` when embedder and reranker are up; 503 means saves will fail.

## Configuration

Copy `.env.example` → `.env`; fill `NEO4J_PASSWORD`, `PG_PASSWORD`, and `AGENT_TOKENS` (plus each agent's own `AGENT_TOKEN`). For `mcp.json`, replace all `YOUR_*` placeholders and the absolute path to `vector-skill.py`. Optional: install `nvtop` on the **infrastructure host** (where REM/NREM run) for GPU-aware dreaming — not on remote clients. Both `memory_bridge.py` and `vector-skill.py` load `.env` via `python-dotenv`.

## Documentation

`README.md` — primary reference (architecture, Quick Start, save path, sleep cycle, retrieval chain). `CHANGELOG.md` — version history. `shared-memory/Documentation/schema.md` — full Postgres + Neo4j schema.

## Licensing

This repository is licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) for the full text.

When contributing or building on this work:
- Retain the copyright notice and licence header in any derived files.
- If you distribute a modified version, state that changes were made.
- Attribution to the original author is appreciated: **Xenofon S. Motsenigos**.

Do not introduce dependencies with licences incompatible with Apache 2.0 (e.g. GPL) without explicit discussion.
