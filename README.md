# Shared Memory Framework

**A local, private, shared brain for every AI agent on your workstation.**
Every insight one agent gains is available to every other — across sessions, across tools, across models. Knowledge stays yours.

A unified semantic and relational memory layer built from first principles to survive the interference problem and scale safely to concurrent multi-agent workloads.

![Claude Code](https://img.shields.io/badge/Claude_Code-Skill-blue)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-Skill-blue)
![Grok](https://img.shields.io/badge/Grok-Skill-blue)
![Gemini CLI](https://img.shields.io/badge/Gemini_CLI-Skill-blue)
![LM Studio](https://img.shields.io/badge/LM_Studio-MCP-blue)
![Neo4j](https://img.shields.io/badge/Neo4j-Graph-green)
![Postgres](https://img.shields.io/badge/Postgres%2Bpgvector-Vector-green)
![BGE-M3](https://img.shields.io/badge/BGE--M3-1024--dim-purple)
![Local & Private](https://img.shields.io/badge/Local_%26_Private-yes-success)

---

## Table of Contents

1. [The Vision: One Brain, Many Agents](#1-the-vision-one-brain-many-agents)
2. [The Problem: Why RAG Systems Forget](#2-the-problem-why-rag-systems-forget)
3. [Architecture Overview: Three Tiers](#3-architecture-overview-three-tiers)
4. [OS Prerequisites — Fedora / Linux](#4-os-prerequisites--fedora--linux)
5. [Infrastructure Setup: Docker Compose](#5-infrastructure-setup-docker-compose)
6. [Database Schema](#6-database-schema)
7. [Inference Backends (llama.cpp)](#7-inference-backends-llamacpp)
8. [The Hive-Mind Gateway: Why It Exists](#8-the-hive-mind-gateway-why-it-exists)
9. [Starting the Full Stack](#9-starting-the-full-stack)
10. [Agent Integration: First-Time Setup](#10-agent-integration-first-time-setup)
11. [Agent Access: CLI and MCP](#11-agent-access-cli-and-mcp)
12. [The Save Path — From Artifact to Memory](#12-the-save-path--from-artifact-to-memory)
13. [The Sleep Cycle — Consolidation](#13-the-sleep-cycle--consolidation)
14. [Audit Logging](#14-audit-logging)
15. [Retrieval: Three-Tier Lookup](#15-retrieval-three-tier-lookup)
16. [LM Studio MCP Configuration](#16-lm-studio-mcp-configuration)
17. [Testing](#17-testing)
18. [Open Problems](#18-open-problems)
19. [Development Roadmap — Multi-Agent Safe Workstation](#19-development-roadmap--multi-agent-safe-workstation)
20. [References](#20-references)

---

## 1. The Vision: One Brain, Many Agents

Every AI workstation today runs several tools in parallel — a terminal agent, a desktop chat model, a coding assistant. Each of them works hard in a session, reasons through a problem, discovers something useful. Then the session ends, and all of that is gone. The next tool starts cold, the next session starts from zero. They do not talk to each other. They cannot.

This framework is built around one idea: those tools should share a brain. When Gemini CLI figures out why the proxy was failing, any other agent should already know the next time it is asked about the proxy. When LM Studio runs a consolidation on a set of architectural facts, those summaries should be there for any agent that searches next.

**The consumers, and how they connect:**

- **Claude Code** — uses `memory_bridge.py` packaged as a Claude skill (`/shared-memory`). Install the skill directory under `~/.claude/skills/`.

- **Codex CLI** — uses `memory_bridge.py` packaged as a Codex skill (`$shared-memory`). Install the skill directory under `~/.codex/skills/`. SKILL.md frontmatter enables implicit invocation when the task description matches.

- **Grok** — uses `memory_bridge.py` packaged as a Grok skill (`/shared-memory`). Install the skill directory under `~/.grok/skills/`.

- **Gemini CLI** — uses `memory_bridge.py` packaged as a Gemini skill (`/activate shared-memory`). Install the skill directory under `~/.gemini/skills/`.

- **LM Studio** — uses an MCP server (`vector-skill.py`), registered in `mcp.json`. The model calls `save_artifact` and `hybrid_search_and_rerank` as tools against the same backend.

The infrastructure underneath all agents is identical: one coordinator managing all Postgres and Neo4j connections, one embedding space enforced by BGE-M3, one consolidation daemon synthesising shared narratives. The agents differ; the memory layer does not.

The design is intentionally agent-agnostic: any tool that can make HTTP calls can reach the coordinator directly on port 8888. Adding a new agent type is a matter of packaging — not changing the backend.

### Three diagnostic tests

Vishakha Gupta's *AI Memory & Cognition: The Architect's Playbook* (ApertureData, May 2026) proposes three questions that any serious AI memory system must be able to answer. They are reproduced here with the current state of this framework's answers — updated with every release.

**The Retrieval Test:** *Can the agent explain why it retrieved a specific memory? Not just what was retrieved, but which specific context, session, and principal metadata informed the decision.*

> As of v0.3.4: search results carry `tier` (fact | community_summary), `score_normalized` (sigmoid of raw reranker logit → [0, 1]), `matched_entities` (intersection of the query string against the saved entity list), and `graph_context` as a structured list of `{rel_type, name, label}` triples. An agent can reason: *"I returned a Tier-3 community synthesis — normalized score 0.91, matching entity OutboxPattern — alongside two Tier-1 precision hits."* **Gap remaining:** retrieval events are not yet audited (no record of who searched, when, from which agent); cross-encoder span attribution is not yet exposed.

**The Consolidation Test:** *When the agent learns something new, does the system update a coherent knowledge base, or does it just accumulate versions? After six months, do you have one "truth" or three conflicting ones?*

> As of v0.3.4: one row per entity, not three. The `community_summaries` table uses `ON CONFLICT (metadata->>'entity') DO UPDATE` — each consolidation cycle replaces the row with a cumulatively synthesised narrative (the LLM receives the prior summary as context). `summary_history JSONB` (migration 004) records the previous N versions before each overwrite, enabling drift auditing. The consolidation daemon now uses `AsyncGraphDatabase` + `loop.run_in_executor()` — `LISTEN/NOTIFY` signals are no longer dropped under write bursts. **Gap remaining:** consolidation is per-entity; two summaries for overlapping entities may diverge slightly if the LLM synthesises them in separate calls. No cross-entity reconciliation step yet.

**The Lineage Test:** *Can I trace a decision back to the original source — the raw image, the specific video frame, or the precise document page — or just the text summary extracted from it?*

> As of v0.3.4: decisions trace fully to human (`WAS_ATTRIBUTED_TO`), AI agent (`WAS_ASSISTED_BY`), and project (`PROJECT_OF`). Community summaries link back to their source facts via `source_pg_ids`. The optional `source_ref` metadata key (e.g. `"design-doc.pdf#p12"`, `"meeting.mp4@00:04:32"`) propagates through the coordinator to the `Fact` Neo4j node. `HAD_OUTCOME` self-loop edges on Decision nodes close the forward trace: decision → outcome → rating + notes. `/memory/graph` enforces read-only access at the driver level (`default_access_mode="READ"`); outbox rows cannot be double-processed during restart (atomic `in_progress` claim + startup recovery). **Gap remaining:** `source_ref` is not enforced — agents supply it when they can. No back-edge yet from a raw `Fact` to the `Decision` it influenced (planned for a later phase).

### What we are building toward

Beyond storing facts, the framework is evolving to answer questions that no other tool on your workstation can answer today:

> *"Who decided on a consolidator on project shared\_memory, when, and was that a good decision?"*

Target answer shape: *"Xenofon, using Claude Code, decided that project shared\_memory should have a consolidator — to simulate dreaming — on 2026-05-20. The related document is ADR-001. He was using Postgres with pgvector as an outbox to achieve non-blocking Neo4j writes, giving optional consistency guarantees on Neo4j and hard guarantees on Postgres. Retrospective as of 2026-05-28: good — held up under multi-agent load."*

This requires not just storing knowledge, but storing **who decided what, with which tool, in which context, and whether it held up**. It requires a provenance layer with first-class nodes for people, AI agents, projects, and decisions — not just facts.

### The signal we are saving

The governing rule: **save what GitHub cannot tell you.** Code is on GitHub. Git blame gives you what changed and when. What is permanently lost without explicit capture:

| Save — signal | Skip — noise |
|---|---|
| Why a decision was made + alternatives rejected | The code that resulted from it |
| What was known / unknown at decision time | Raw web search results |
| Who participated and with which AI tool | Debug output, stack traces |
| Milestones + the context that made them significant | Test results (unless they caused a decision) |
| Retrospectives: was the decision right after N weeks? | Health checks, routine saves |
| Abandoned approaches and why they were dropped | Intermediate build artifacts |

Every memory save should answer at least one of: **Who? Why? What was rejected? Was it right?**

### Saving everything vs. saving what matters

This distinction is not cosmetic — it directly determines what you can query later.

If you adopt a "save everything" policy (logs, test output, status checks, raw search results), the shared memory fills with low-signal noise. Consolidation groups semantically similar content into community summaries, so noise consolidates into more noise: you end up with thematic summaries of debug sessions rather than thematic summaries of decisions. Retrieval accuracy degrades because high-density noisy clusters crowd out the sparse, high-signal facts.

**What you can query with disciplined saves:**

```
# Who decided, when, under what conditions, and with which tool?
"Who decided to use an outbox for Neo4j writes on the shared_memory project?"
→ Xenofon, using Claude Code, on 2026-05-20.
   Condition at the time: Neo4j had no native async write path compatible with asyncpg.
   Rationale: non-blocking — Postgres guarantees hard consistency, Neo4j is eventual.
   Alternatives rejected: synchronous writes (too slow), no Neo4j (lost graph queries).

# Provenance chain — who + what AI assisted
"What decisions did Claude Code assist with on project shared_memory?"
→ Decision: Add outbox-as-WAL for Neo4j writes (2026-05-20)
   Decision: Use FOREACH over UNWIND for empty-list safety in Cypher (2026-05-28)
   Decision: Add consolidation daemon as a dreaming analogue (2026-05-20)

# Reasoning behind a specific approach
"Why does the coordinator use FOREACH instead of UNWIND?"
→ UNWIND produces zero rows for an empty list — the write silently drops.
   FOREACH handles empty lists safely. Saved 2026-05-28 by Claude Code.

# What was abandoned and why
"What embedding models were considered before BGE-M3?"
→ MiniLM-384: rejected — too few dimensions for cross-agent coherence.
   BGE-base-768: evaluated — acceptable, not best-in-class for multilingual.
   BGE-M3-1024: selected — highest multilingual retrieval quality in class.

# Was a past decision successful? (Phase C — retrospectives)
"Was the outbox-as-WAL approach a good decision for the shared_memory project?"
→ Retrospective 2026-06-15 (rating: 8/10): held up under multi-agent load.
   Note: outbox replay on crash worked correctly; Neo4j lag < 200 ms typical.
   Suggested follow-up: add TTL pruning for applied rows > 30 days.

# Phase A (done): who decided + which AI + which project + why
# Phase C (done): outcomes, retrospectives, was it right after N weeks?
```

**What you cannot query if you save noise:**

```
# Only works if the reasoning was explicitly saved
"Why was the consolidation threshold set to 5 facts?"
→ No result — this tuning choice was never recorded with rationale.
   Fix: save a decision with rationale when the threshold is next changed.

# Transient runtime state is never here
"What did the health check return yesterday at 14:30?"
→ Not in memory. Routine health checks are not saved — check Prometheus or logs.

# Current code state lives in Git, not memory
"What is the current value of DENSITY_THRESHOLD in consolidation_loop.py?"
→ Read the file. Memory holds decisions about code, not code itself.

# Retrospectives require save_retrospective (Phase C — now available)
"Was the BGE-M3 selection the right call?"
→ No retrospective saved yet. Use save_retrospective --pg-id <id> --rating high --notes "..."
```

The governing heuristic: **if you can get the answer in 3 seconds from `git log`, `grep`, or `cat`, don't save it here.** Memory is for context that evaporates without capture — the why behind a decision, the options that were weighed, the outcome after the fact.

### Local mounts — your work stays yours

Both databases are deployed via Docker Compose with host-mounted volumes. The data lives on your filesystem, not inside a container — you can back it up with any standard tool, and a container restart or upgrade does not lose what you have accumulated.

```yaml
# Postgres data on the host filesystem — survives container rebuilds
volumes:
  - /your/databases/postgres/data:/var/lib/postgresql/data:z

# Neo4j data on the host filesystem — same guarantee
volumes:
  - /your/databases/neo4j/data:/data:z
```

> **Note for Fedora/RHEL users:** The `:z` suffix is required — it sets the SELinux label so the container process can read and write the host directory. Without it, Neo4j and Postgres fail silently.

### The binding element: 1024-dimensional BGE-M3

What makes the three tools a unified memory system rather than three separate stores is the embedding model. Every vector in the system — saved by Gemini CLI, saved by LM Studio, saved by any CLI agent, re-embedded by the consolidation daemon — was generated by the same BGE-M3 instance through the same gateway. The coordinate system is shared. Cosine similarity between a vector one agent saved last Tuesday and a query another agent is making right now is a genuine semantic comparison.

---

## 2. The Problem: Why RAG Systems Forget

The common assumption in RAG architectures is that you can save everything and the vector database will sort it out. This assumption has been formally disproved.

Barman et al. (2026) in *"The Geometry of Forgetting"* expose what they call the **Dimensionality Illusion**: BGE-M3 is nominally 1024-dimensional but concentrates its variance in approximately 16 effective dimensions — a figure that holds across MiniLM at 384 dimensions and BGE-base at 768 as well, regardless of what the model card claims.

An agent navigating that space is not moving through a vast semantic landscape. It is moving through a narrow corridor, and every new memory saved into the same neighborhood is another body crowding that corridor. Retrieval accuracy does not dip gradually — it degrades as a power law with database size, driven by the mechanism the paper names: **semantic interference**. You are most vulnerable where you would expect to gain the most value from your memory.

This is the problem the Shared Memory Framework is designed to address. The solution has three parts: a dual-store architecture that separates episodic from structural memory, a consolidation loop that synthesises high-density clusters into a thematic semantic tier before interference pressure accumulates, and a single shared embedding space enforced across all agents.

> **The biological parallel:** The **Complementary Learning Systems** hypothesis (McClelland, McNaughton & O'Reilly, 1995) proposes that the hippocampus holds fast, episodic, pattern-separated traces while the neocortex extracts slow statistical patterns across episodes — abstract, generalizable, thematic. This transfer happens primarily during offline states, including sleep. The architecture here implements the same division: Neo4j as the hippocampus, `community_summaries` as the neocortex, and the consolidation daemon as the sleep cycle.

---

## 3. Architecture Overview: Three Tiers

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              AGENT LAYER                                 │
│                                                                          │
│  Claude Code   Grok    Gemini CLI   LM Studio (MCP)    Any HTTP          │
│  (skill)       (skill) (skill)      vector-skill.py    client            │
│  memory_bridge.py ←→ memory_bridge.py ←→ memory_bridge.py               │
└─────────────┬──────────────────────┬─────────────────────┬──────────────┘
              │                      │                     │
              └──────────────────────▼─────────────────────┘
                                     │ HTTP (all memory ops)
                         ┌───────────▼────────────────────────┐
                         │  Hive-Mind Gateway + Coordinator   │
                         │  hive_mind_proxy.py  :8888         │
                         │                                    │
                         │  /memory/save   → coordinator.py  │
                         │  /memory/search → coordinator.py  │
                         │  /memory/graph  → coordinator.py  │
                         │  /v1/embeddings → :8070 (BGE-M3)  │
                         │  /v1/reranking  → :8071            │
                         │  default        → :5000 (LLM)     │
                         └────────┬──────────────────────────┘
                                  │ spawns
                         ┌────────▼───────────────┐
                         │  Consolidation Daemon  │
                         │  consolidation_loop.py │
                         │  LISTEN new_artifact   │
                         └────────┬───────────────┘
                                  │ writes
              ┌───────────────────▼───────────────────────────┐
              │                MEMORY LAYER                    │
              │                                                │
              │  ┌──────────────────────┐  ┌───────────────┐  │
              │  │  PostgreSQL+pgvector │  │    Neo4j      │  │
              │  │                      │  │               │  │
              │  │ technical_docs       │  │ Fact nodes    │  │
              │  │  (Tier 1 — Episodic) │  │ Entity hubs   │  │
              │  │                      │  │ MENTIONS edges│  │
              │  │ community_summaries  │  │ CommunitySumm │  │
              │  │  (Tier 3 — Semantic) │  │ SUMMARIZED_BY │  │
              │  │                      │  └───────────────┘  │
              │  │ neo4j_outbox         │                      │
              │  │  (coordinator WAL)   │                      │
              │  └──────────────────────┘                      │
              └────────────────────────────────────────────────┘
```

| Tier | Store | Role | Biological Analogy |
|---|---|---|---|
| **1 — Episodic** | `technical_docs` (Postgres + pgvector) | Original facts, full content, surgical precision via cosine similarity | Hippocampus — fast, specific, pattern-separated |
| **2 — Structural** | Neo4j `Fact` nodes (keyed by `pg_id`) | Relationships, provenance, `consolidated` flag, Entity hubs | Hippocampus — relational context cosine similarity cannot express |
| **3 — Semantic** | `community_summaries` (Postgres + pgvector) | Consolidated thematic narratives; queried first on retrieval | Neocortex — slow, abstract, statistical regularities across episodes |

**Retrieval always queries Tier 3 first** (thematic orientation), then Tier 1 (surgical precision), then expands through Neo4j (relational context). Artifacts saved by one agent become retrievable by all others once the consolidation daemon runs.

---

## 4. OS Prerequisites — Fedora / Linux

An agentic workstation running Neo4j, Postgres, LM Studio, and multiple MCP servers creates many more filesystem watchers than a standard desktop. Fedora's default kernel limits will cause failures under this load.

### Raise inotify limits

```bash
# Create a persistent sysctl override
echo "fs.inotify.max_user_instances=1024" | sudo tee /etc/sysctl.d/90-inotify.conf
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.d/90-inotify.conf

# Apply immediately (no reboot required)
sudo sysctl -p /etc/sysctl.d/90-inotify.conf

# Verify
sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches
```

A stock Fedora workstation defaults to 128 instances and 65536 watches — adequate for a desktop, not for a workstation running five database services, two MCP runtimes, and a file watcher per active LLM tool.

---

## 5. Infrastructure Setup: Docker Compose

Neo4j and Postgres are the two persistent stores. Both run in Docker. See `postgres_neo4j_limits.yaml` for the full compose file; the key structure is:

```yaml
services:
  neo4j:
    image: neo4j:5-community
    ports:
      - "7474:7474"   # Browser UI
      - "7687:7687"   # Bolt protocol
    volumes:
      - /your/databases/neo4j/data:/data:z
    environment:
      - NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}
      - NEO4J_PLUGINS=["apoc"]
      # Neo4j 5 uses double underscores (__) for nested config keys
      - NEO4J_server_memory_heap_max__size=2G
      - NEO4J_server_memory_pagecache_size=2G
    restart: always

  postgres:
    image: pgvector/pgvector:pg17
    ports:
      - "5432:5432"
    volumes:
      - /your/databases/postgres/data:/var/lib/postgresql/data:z
    command: postgres -c shared_buffers=1GB -c work_mem=64MB
    environment:
      - POSTGRES_PASSWORD=${PG_PASSWORD}
      - POSTGRES_DB=agent_data
    restart: always
```

```bash
# Start both services
docker compose -f postgres_neo4j_limits.yaml up -d

# Verify
docker compose -f postgres_neo4j_limits.yaml ps
```

Credentials are read from environment variables — copy `.env.example` to `.env` and fill in `NEO4J_PASSWORD` and `PG_PASSWORD` before starting.

---

## 6. Database Schema

Run these once against the Postgres instance to create the vector extension and both tables.

```sql
-- Connect: psql postgresql://postgres:${PG_PASSWORD}@localhost:5432/agent_data

CREATE EXTENSION IF NOT EXISTS vector;

-- Tier 1: episodic facts from all agents
CREATE TABLE IF NOT EXISTS technical_docs (
    id            SERIAL PRIMARY KEY,
    content       TEXT NOT NULL,
    metadata      JSONB,
    embedding     vector(1024),
    content_hash  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS technical_docs_embedding_idx
    ON technical_docs USING ivfflat (embedding vector_cosine_ops);

-- Tier 3: consolidated thematic narratives
CREATE TABLE IF NOT EXISTS community_summaries (
    id             SERIAL PRIMARY KEY,
    content        TEXT NOT NULL,
    metadata       JSONB,
    embedding      vector(1024),
    source_pg_ids  integer[]       -- IDs of technical_docs rows that contributed to this summary
);
CREATE INDEX IF NOT EXISTS community_summaries_embedding_idx
    ON community_summaries USING ivfflat (embedding vector_cosine_ops);
```

### Neo4j constraints

```cypher
// Run in Neo4j Browser or cypher-shell
CREATE CONSTRAINT fact_pg_id IF NOT EXISTS FOR (f:Fact) REQUIRE f.pg_id IS UNIQUE;
CREATE CONSTRAINT entity_name IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE;
CREATE CONSTRAINT summary_pg_id IF NOT EXISTS FOR (s:CommunitySummary) REQUIRE s.pg_id IS UNIQUE;
```

> **Key schema rule:** Every fact saved must include `"entities": ["Name1", "Name2"]` in its metadata. The saver creates `Entity` nodes and `MENTIONS` edges for each name. Without them the fact is stored and retrievable by vector search, but the consolidation daemon will never cluster it into Tier 3. The graph layer is the prerequisite for the semantic layer.

Full schema with all Neo4j labels and relationship types: [`shared-memory/Documentation/schema.md`](shared-memory/Documentation/schema.md)

### Ontology configuration

All Neo4j label names and relationship types are defined in `ontology.yaml` at the repo root. The defaults match the schema above. Override any value to adapt the graph to your naming conventions without touching Python source — then restart the scripts.

```yaml
# ontology.yaml — excerpt showing defaults
labels:
  fact: Fact
  entity: Entity
  community_summary: CommunitySummary
  # Provenance layer (Phase A)
  decision: Decision       # architectural / design decision
  human: Human             # person who owns a decision
  ai_agent: AIAgent        # AI tool that assisted
  project: Project         # project scope
  activity: Activity       # work session context
  milestone: Milestone     # significant achievement marker

relationships:
  entity_link: MENTIONS          # Fact → Entity, written on save
  entity_link_alias: REPORTS_ON  # legacy alias accepted by consolidation
  summarized_by: SUMMARIZED_BY
  # Provenance relationships (Phase A)
  was_attributed_to: WAS_ATTRIBUTED_TO  # Decision → Human
  was_assisted_by: WAS_ASSISTED_BY      # Decision → AIAgent
  project_of: PROJECT_OF                # Decision → Project
  supersedes: SUPERSEDES                # Decision → Decision
  informed_by: INFORMED_BY              # Decision → Decision
  had_outcome: HAD_OUTCOME              # Decision → (self or Milestone)

consolidation:
  density_threshold: 5        # unconsolidated Facts per Entity to trigger synthesis
```

Set `SMEM_ONTOLOGY_PATH=/path/to/your/ontology.yaml` to load from a non-default location. If the file is absent the stack starts with the built-in defaults — no configuration required for a standard deployment.

---

## 7. Inference Backends (llama.cpp)

Two models serve the embedding and reranking paths. Both are hosted via `llama-server` on separate ports. A third port (5000) hosts the reasoning LLM — typically LM Studio's inference server, but any OpenAI-compatible endpoint works.

```bash
# BGE-M3 — embedding model, port 8070
llama-server --model /path/to/bge-m3-Q8_0.gguf --port 8070 --embedding --pooling mean

# BGE-Reranker-v2-m3 — reranking model, port 8071
llama-server --model /path/to/bge-reranker-v2-m3.gguf --port 8071 --reranking
```

> **Never call ports 8070 or 8071 directly.** All agents must go through the Hive-Mind Gateway on port 8888. The gateway is what enforces the shared embedding space — if any agent bypasses it, the 1024-dim consistency guarantee is broken in operational practice.

---

## 8. The Hive-Mind Gateway: Why It Exists

### The hardcoded embedder problem

Many tools in this stack are built around the OpenAI API. LM Studio's internal agent tooling and other OpenAI-compatible clients accept an API base URL and call `/v1/embeddings` against it. Without a gateway, the choices are:

- Point every tool individually at port 8070 — fragile, breaks reranking which lives on 8071
- Accept that each tool calls whatever model it prefers — produces different vector spaces, destroying cross-agent retrieval
- Let credentials leak to the real OpenAI API if a tool ignores the local override

The gateway solves all three. Every tool points at `http://localhost:8888/v1`. The gateway routes internally:

| Path | Backend |
|---|---|
| `/v1/embeddings` | Port 8070 (BGE-M3, 1024-dim) |
| `/v1/reranking` | Port 8071 (BGE-Reranker-v2-m3) |
| All other requests | Port 5000 (reasoning LLM) |

One endpoint. All agents. Same vector space.

### From ThreadingHTTPServer to async aiohttp — why streaming required a rewrite

The first versions of the gateway used Python's stdlib `http.server.ThreadingHTTPServer` with `urllib` for upstream calls. This worked for embedding and reranking (which return quickly), but it broke fundamentally for LLM streaming: `urllib` buffers the entire upstream response before returning. A 4,000-token generation at 20 tokens/second takes 200 seconds, delivered as a single write — that is not streaming.

The v6 async rewrite replaced the entire implementation with `aiohttp.web` + `aiohttp.ClientSession`. Key properties:

- **True streaming:** `iter_any()` pipes upstream chunks to the client as they arrive. The first token reaches the client in milliseconds.
- **RFC 7230 hop-by-hop filtering:** `Transfer-Encoding`, `Content-Length`, `Connection`, and other hop-by-hop headers are stripped from both request and response. Forwarding a stale `Content-Length` alongside a chunked stream causes clients to truncate or hang.
- **`auto_decompress=False`:** aiohttp decompresses upstream responses by default but still forwards `Content-Encoding: gzip`. A client receiving decompressed bytes labelled as compressed double-decompresses — corruption. Disabled so compressed bytes and headers travel together.
- **`CancelledError` always re-raised:** swallowing it leaves tasks as zombies; graceful shutdown stalls indefinitely.
- **Self-defusing signal handler:** after the first SIGINT/SIGTERM, both handlers are removed. A second Ctrl+C falls back to Python's default `KeyboardInterrupt` — emergency hard-abort if the drain stalls on a hung backend.
- **HTTP 503 for unreachable backends, 504 for connect timeout:** correct semantics for client retry logic.

---

## 9. Starting the Full Stack

The startup sequence is order-dependent. The gateway must be up before any embedding or save operation. Starting the gateway also starts the consolidation daemon — you do not need to manage them separately.

**1. Start databases**
```bash
docker compose -f postgres_neo4j_limits.yaml up -d
```

**2. Start BGE-M3 and BGE-Reranker-v2-m3** (llama-server, ports 8070 and 8071)

**3. Start the reasoning LLM** (LM Studio or any OpenAI-compatible server on port 5000)

**4. Start the Hive-Mind Gateway** — this also starts the consolidation daemon automatically
```bash
uv run --with aiohttp python shared-memory/scripts/hive_mind_proxy.py 8888
```

You will see two log lines confirming both are up:
```
INFO  ### Hive-Mind Proxy on :8888 [aiohttp]
INFO  Consolidation daemon started (pid XXXXX)
INFO  Listening for 'new_artifact' notifications...
```

**5. LM Studio** — start the application; it will pick up the MCP servers from `mcp.json` automatically.

Step 4 is the only manual step required after databases and models are running. The proxy starts the daemon; the daemon registers its Postgres listener; both shut down cleanly when the proxy receives SIGINT or SIGTERM.

**Verify the full stack is healthy:**
```bash
curl http://localhost:8888/health
# {"status":"ok","embedder":"ok","reranker":"ok","llm":"ok","daemon":"running"}
```

HTTP 200 means the save/search path (embedder + reranker) is operational. HTTP 503 means at least one critical backend is down — do not attempt saves until resolved. The `llm` and `daemon` fields are informational; their degradation affects consolidation only.

**Daemon watchdog:** the gateway automatically restarts the consolidation daemon if it crashes, with exponential backoff and a circuit breaker (5 crashes / 10 min). If the circuit breaker trips, restart the gateway.

> **Network exposure:** The gateway binds to `127.0.0.1:8888` by default — localhost only. Set `PROXY_BIND=0.0.0.0` in `.env` to opt into all-interfaces binding (e.g. inside an isolated Docker or VM network). The coordinator API is unauthenticated — do not expose port 8888 on an untrusted network. See [SECURITY.md](SECURITY.md) for details.

---

## 10. Agent Integration: First-Time Setup

This section covers where to place files and how to register each agent. For runtime usage (commands and examples) see [§11: Agent Access: CLI and MCP](#11-agent-access-cli-and-mcp).

### Clone the repository and set up the environment

```bash
git clone https://github.com/KanenasInGreece/Shared_Memory.git
cd Shared_Memory
cp .env.example .env
# Edit .env — fill in NEO4J_PASSWORD and PG_PASSWORD
```

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate

# Runtime only
pip install -r requirements.txt

# Runtime + test dependencies
pip install -r requirements-dev.txt
```

Activate the venv in every new shell session before running any script:

```bash
source .venv/bin/activate
```

> **uv users:** all commands in this README use `uv run --with ...` which handles dependencies automatically without a venv. Both approaches work — use whichever fits your workflow.

### Smoke-test the bridge

After the full stack is running, verify the bridge works from any shell:

```bash
uv run --with httpx --with python-dotenv \
  python /path/to/Shared_Memory/shared-memory/scripts/memory_bridge.py search "test" 3
```

### Claude Code

Claude Code loads skills from `~/.claude/skills/`. Create the skill directory with a symlink so scripts always stay in sync with the repo:

```bash
mkdir -p ~/.claude/skills/shared-memory

# Symlink scripts — always in sync with the repo
ln -s /path/to/Shared_Memory/shared-memory/scripts ~/.claude/skills/shared-memory/scripts

# Copy SKILL.md (or symlink it too)
cp shared-memory-skill/shared-memory/SKILL.md ~/.claude/skills/shared-memory/SKILL.md
```

Invoke in any Claude Code session:

```
/shared-memory
```

### Grok

Grok loads skills from `~/.grok/skills/`. Same symlink pattern:

```bash
mkdir -p ~/.grok/skills/shared-memory

# Symlink scripts — always in sync with the repo
ln -s /path/to/Shared_Memory/shared-memory/scripts ~/.grok/skills/shared-memory/scripts

# Copy SKILL.md
cp shared-memory-skill/shared-memory/SKILL.md ~/.grok/skills/shared-memory/SKILL.md
```

Invoke in any Grok session:

```
/shared-memory
```

### Codex CLI

Codex CLI loads skills from `~/.codex/skills/` (global) or `.agents/skills/` (project-level). Install globally so the skill is available in every project:

```bash
mkdir -p ~/.codex/skills/shared-memory

# Symlink scripts — always in sync with the repo
ln -s /path/to/Shared_Memory/shared-memory/scripts ~/.codex/skills/shared-memory/scripts

# Copy SKILL.md
cp shared-memory/SKILL.md ~/.codex/skills/shared-memory/SKILL.md
```

Invoke explicitly in any Codex CLI session:

```
$shared-memory
```

Codex CLI also supports **implicit invocation**: if the description in SKILL.md's frontmatter matches the task, the skill is loaded automatically without an explicit `$` call.

> **AGENTS.md:** Codex CLI reads `AGENTS.md` at the project root before each session (their equivalent of `CLAUDE.md`). This repo provides `AGENTS.md` alongside `AGENT.md` — both contain the same architectural guidance.

### Gemini CLI

Gemini CLI loads skills from `~/.gemini/skills/`. Drop the `shared-memory` skill directory there:

```bash
mkdir -p ~/.gemini/skills

# Copy (standalone — updates require a re-copy)
cp -r shared-memory-skill/shared-memory ~/.gemini/skills/shared-memory

# Or symlink (always in sync with the repo)
ln -s /path/to/Shared_Memory/shared-memory-skill/shared-memory ~/.gemini/skills/shared-memory
```

Activate in any Gemini CLI session:

```
/activate shared-memory
```

### LM Studio

LM Studio integrates through two files: an MCP config (`mcp.json`) and the MCP server script (`vector-skill.py`).

**Step 1 — Place `vector-skill.py`**

Put it anywhere that stays accessible, for example:

```bash
mkdir -p ~/ai/shared-memory
cp vector-skill.py ~/ai/shared-memory/vector-skill.py
```

LM Studio does not manage this path — you reference it by absolute path in `mcp.json`.

**Step 2 — Configure and place `mcp.json`**

Edit `mcp.json` from this repo: replace all `YOUR_*` placeholders with real values and update the absolute path to `vector-skill.py` in the `rag-orchestrator` entry. Then save it to LM Studio's MCP config location (`~/.lmstudio/mcp.json` on Linux and macOS).

**Step 3 — Configure and load the system prompt**

`system-prompt.md` is the operational contract for the LM Studio model. It defines:

- **Search-first directive** — the model must call `rag-orchestrator` → `hybrid_search_and_rerank` as the first tool on every query. `rag-orchestrator` already includes Neo4j graph expansion internally; no separate graph MCP is needed.
- **Gateway mandate** — the architectural context explicitly states that all embedding and reranking calls route through port 8888; the model must never reference 8070 or 8071 directly.
- **Consolidation awareness** — the model knows that every save triggers a Postgres `pg_notify` and that the consolidation daemon (auto-started with the gateway) synthesises Tier 3 summaries. It also knows to warn you if the daemon is not running.
- **Memory cycle** — when to absorb (end of task, new decision) and that `"entities"` in save metadata is required for Tier 3 eligibility.

Before importing, fill in the `[YOUR ...]` placeholder fields at the top (name, location, hardware, OS). Then import in LM Studio: **Settings → System Prompt → Import**.

**Step 4 — Verify**

Start LM Studio. The `rag-orchestrator` MCP server should appear in the tool panel. If it shows an error, confirm the full stack is running (gateway on :8888, databases up) and that there are no remaining `YOUR_*` placeholders in `mcp.json`.

---

## 11. Agent Access: CLI and MCP

| Consumer | Interface | Entry point | Consolidation trigger |
|---|---|---|---|
| **Claude Code** | CLI (skill `/shared-memory`) | `~/.claude/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **Codex CLI** | CLI (skill `$shared-memory`) | `~/.codex/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **Grok** | CLI (skill `/shared-memory`) | `~/.grok/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **Gemini CLI** | CLI (skill `/activate shared-memory`) | `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **LM Studio** | MCP (FastMCP) | `vector-skill.py` → `rag-orchestrator` in `mcp.json` | via coordinator → `pg_notify` |
| **Any HTTP client** | REST | `POST http://localhost:8888/memory/save\|search\|graph` | via coordinator → `pg_notify` |

All three paths route through the coordinator on port 8888. The coordinator owns all Postgres and Neo4j connections — agents no longer connect to the databases directly. The Hive-Mind Gateway must be running before any save or search.

### CLI usage

```bash
# Check the framework version
python shared-memory/scripts/memory_bridge.py --version
# → {"version": "0.3.0", "tool": "shared-memory-framework"}

# Search — semantic + rerank + Neo4j expansion
uv run --with httpx \
  python shared-memory/scripts/memory_bridge.py search "bgem3 interference problem" 5

# Save — always include source and entities
uv run --with httpx \
  python shared-memory/scripts/memory_bridge.py save \
  "The proxy routes all embeddings through :8888 to enforce 1024-dim consistency." \
  '{"source":"claude-code","entities":["hive_mind_proxy","BGE-M3","SharedMemory"]}'

# Save a decision — structured flags, no JSON blob required
uv run --with httpx \
  python shared-memory/scripts/memory_bridge.py save_decision \
  --title "Route all embeddings through the gateway" \
  --decided-by "Xenofon" \
  --project "shared-memory" \
  --rationale "Enforces 1024-dim consistency across all agents; prevents dimension mismatch on retrieval" \
  --assisted-by "claude-sonnet-4-6" \
  --alternatives "direct port 8070 calls, per-agent embedding models" \
  --confidence "high" \
  --entities "BGE-M3,hive_mind_proxy,SharedMemory"

# Query decisions — who decided what, with which AI, on which project
uv run --with httpx \
  python shared-memory/scripts/memory_bridge.py graph \
  "MATCH (h:Human)-[:WAS_ATTRIBUTED_TO]-(d:Decision)-[:PROJECT_OF]->(p:Project)
   OPTIONAL MATCH (d)-[:WAS_ASSISTED_BY]->(ai:AIAgent)
   RETURN h.name, d.title, d.rationale, d.date, p.name, ai.name
   ORDER BY d.date DESC LIMIT 5"

# Graph query — entity hub sizes (top referenced concepts)
uv run --with httpx \
  python shared-memory/scripts/memory_bridge.py graph \
  "MATCH (e:Entity)<-[:MENTIONS]-(f:Fact) RETURN e.name, count(f) AS refs ORDER BY refs DESC LIMIT 10"
```

### Coordinator HTTP API

The coordinator exposes four endpoints on port 8888. These can be called directly by any HTTP client — agents, scripts, or future tools.

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/memory/save` | `{content, metadata, agent_id?, scope?, visibility?}` | `{status, pg_id, neo4j, message}` |
| `POST` | `/memory/search` | `{query, limit?, scope?, agent_id?}` | `{status, results[]}` |
| `POST` | `/memory/graph` | `{cypher, params?}` | `{status, records[]}` |
| `GET` | `/memory/status/{pg_id}` | — | `{pg_id, neo4j, retries, applied_at}` |

> **`/memory/graph` is read-only enforced.** Queries containing `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `MERGE`, `CALL`, `LOAD CSV`, or `DROP` are rejected with HTTP 400 before reaching Neo4j. Use it for `MATCH`/`RETURN`/`WITH`/`WHERE` exploration only.

**Write acknowledgment:** saves return `200 OK` once the fact is committed to Postgres. The outbox row for Neo4j is written in the same transaction; Neo4j application is asynchronous. Use `GET /memory/status/{pg_id}` to confirm Neo4j application, or pass `?consistency=neo4j` (Phase 2) to block until the outbox row is applied.

### Skill activation

```
/shared-memory          # Claude Code and Grok
$shared-memory          # Codex CLI (explicit); also auto-matched via SKILL.md description
/activate shared-memory # Gemini CLI
```

---

## 12. The Save Path — From Artifact to Memory

The save path runs inside the coordinator (`coordinator.py`) on every `POST /memory/save`:

```
caller: POST /memory/save {content, metadata, agent_id, scope, visibility}
       ↓
embed(content) via :8888 — retry with exponential backoff (4 attempts)
       ↓ 503 if all retries fail — hard mandate: no save without a vector
acquire per-entity asyncio.Lock for each name in metadata["entities"]
       ↓ serializes concurrent writes to the same entity cluster
BEGIN TRANSACTION
  INSERT INTO technical_docs ... ON CONFLICT (content_hash) DO UPDATE
       ↓ idempotent: SHA-256 hash prevents duplicates; agent_id/scope/visibility stored
  INSERT INTO neo4j_outbox (pg_id, cypher_params)
       ↓ outbox row committed atomically — Phase 2 worker drains this
  SELECT pg_notify('new_artifact', {"pg_id": id})
COMMIT  ← 200 OK returned to caller here (Postgres-ack)
       ↓
MERGE (f:Fact {pg_id}) in Neo4j  [Phase 1 — direct write; replaced by outbox worker in Phase 2]
for each entity name in metadata["entities"]:
    MERGE (e:Entity {name})
    MERGE (f)-[:MENTIONS]->(e)
       ↓
daemon receives NOTIFY → adds pg_id to pending_pg_ids → idle timer starts
```

> **Hard Mandate — Embedding Integrity:** Saves return 503 if the embedding service is unreachable after all retries. An artifact without a vector is invisible to semantic search — this failure must surface, never be swallowed.

> **Per-entity write serialization:** Concurrent saves targeting the same entity are serialized via `asyncio.Lock[entity_name]`. This prevents duplicate `Entity` hub creation under agent-swarm concurrency and ensures the consolidation daemon sees a consistent cluster. (Phase 4 replaces this with Postgres advisory locks for multi-process deployment.)

> **Cross-DB atomicity:** The outbox row is written in the same Postgres transaction as the fact. If the process crashes after commit, the outbox row survives and the Phase 2 worker replays the Neo4j write on restart. The ADR-001 dangling-Fact window is eliminated in Phase 2.

> **Audit logging:** Every event in the save path — coordinator unreachable, malformed metadata, missing entities, Neo4j sync failures, and successful saves — is optionally logged based on `MEMORY_LOG_LEVEL`. See [§14: Audit Logging](#14-audit-logging).

---

## 13. The Sleep Cycle — Consolidation

The consolidation daemon is the neocortical layer of the architecture. It does not poll — polling would compete with inference workloads that need full GPU headroom. It waits for a Postgres `NOTIFY`, then applies a dual gate before acting: an idle timer and a graph density check.

### Trigger logic

- Each `pg_notify` adds the artifact's `pg_id` to `pending_pg_ids` and resets a 15-minute idle timer.
- After 15 minutes with no new notifications (idle threshold), consolidation runs.
- A 45-minute hard backstop fires during continuous ingestion even if notifications never stop — preventing indefinite deferral.

### Per-community consolidation

The daemon uses the queued `pg_id`s as entry points into Neo4j, not as the consolidation targets themselves. From each entry point it traverses to Entity hubs and counts unconsolidated Fact neighbors. Communities with fewer than 5 unconsolidated Facts wait — sparse neighborhoods are not ready for synthesis.

For each community that meets the threshold:

1. Fetch the most recent `CommunitySummary` for that Entity from Postgres (if any).
2. Call the LLM via `:8888 → :5000` to integrate new facts into the existing narrative — **cumulative**, not a new isolated snapshot. This prevents content drift from parallel summary fragments about the same entity.
3. Re-embed the new narrative via BGE-M3 through `:8888`.
4. Write to `community_summaries`; create/update `CommunitySummary` node in Neo4j; link source Facts via `SUMMARIZED_BY`; set `Fact.consolidated = true`.

> **Why centroid averaging is not used:** The obvious compression approach — averaging related embeddings into a centroid — collapses the angular distinctions that cosine similarity depends on (Vangara & Gopinath, 2026, *"The Geometry of Consolidation"*). The LLM instead generates new language representing the theme of the cluster, which is then re-embedded from scratch. This produces a new semantic point that did not exist before — not a mathematical blend. Retrievable volume grows O(log n) with LLM-based consolidation versus O(n) without it.

### Re-consolidation

The `consolidated` flag is not permanent. If future ingestion introduces unflagged Facts with sufficient neighborhood density that pull previously-consolidated Facts back into a candidate community, the entire cluster becomes eligible again.

---

## 14. Audit Logging

The save path in both `memory_bridge.py` and `vector-skill.py` writes structured JSON log entries to per-tool files. Logging is **off by default** — enable it by setting `MEMORY_LOG_LEVEL` in `.env`.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORY_LOG_LEVEL` | `0` (off) | Controls which events are logged |
| `MEMORY_LOG_PATH` | `~/.shared-memory/logs` | Directory where log files are written |

### Log levels

| Level | Events logged |
|---|---|
| `0` | Nothing (default) |
| `1` | **Warnings** — save succeeded but `entities` missing; fact is stored but ineligible for consolidation |
| `2` | Warnings + **errors** — gateway down (save aborted), malformed metadata JSON, non-dict metadata, Neo4j sync failure |
| `3` | All above + **successful saves** — records `pg_id`, `source`, and entity count on every completed save |
| `4` | All above + **full content copy** — includes the complete `content` field in each entry; warns if content exceeds 10 KB |

### Per-tool log files

Each entry point writes to its own file. Concurrent writes from CLI agents (both using `memory_bridge.py`) are safe — `O_APPEND` mode writes are atomic on Linux for writes smaller than `PIPE_BUF` (4096 bytes); individual log lines are well within that limit. Rotation is excluded from the writing tools to eliminate any write/rotate race condition.

| Tool | Log file |
|---|---|
| CLI tools / Gemini CLI | `{MEMORY_LOG_PATH}/memory_bridge.log` |
| LM Studio MCP | `{MEMORY_LOG_PATH}/vector_skill.log` |

### Log format

Each line is a self-contained JSON object:

```json
{"ts": "2026-05-24T14:32:01.123456", "tool": "memory_bridge", "event": "no_entities", "pg_id": 42, "source": "gemini_cli"}
{"ts": "2026-05-24T14:35:17.891234", "tool": "memory_bridge", "event": "save_success", "pg_id": 43, "source": "gemini_cli", "entity_count": 2}
{"ts": "2026-05-24T14:41:03.552109", "tool": "vector_skill",   "event": "gateway_down", "content_preview": "Architectural dec..."}
```

`event` is one of: `gateway_down`, `bad_metadata`, `bad_metadata_type`, `neo4j_sync_failed`, `no_entities`, `save_success`.

### Daily merge by the consolidation daemon

The consolidation daemon runs `merge_logs()` once per calendar day on the first 1-second poll of a new day. It uses the logrotate pattern:

1. Rename `memory_bridge.log` → `memory_bridge.log.rotating` and `vector_skill.log` → `vector_skill.log.rotating`. Writing tools create fresh files on next open.
2. Parse all entries from both rotating files, sort by timestamp, group by calendar date.
3. For each date, merge with any existing archive and write `shared_memory_YYYY-MM-DD.log.gz` (atomic `os.replace`).
4. Delete the `.rotating` files.

The `shared_memory_` prefix distinguishes merged archives from agent memory files in the same directory.

```
~/.shared-memory/logs/
  memory_bridge.log               ← active, append-only
  vector_skill.log                ← active, append-only
  shared_memory_2026-05-23.log.gz ← yesterday, merged
  shared_memory_2026-05-22.log.gz ← two days ago, merged
```

If the daemon is not running, per-tool logs accumulate; entries from multiple days are correctly split into separate dated archives on the next merge run.

---

## 15. Retrieval: Three-Tier Lookup

Both the MCP tool (`hybrid_search_and_rerank` in `vector-skill.py`) and the CLI (`memory_bridge.py search`) implement the same retrieval chain:

1. **Embed the query** via BGE-M3 through `:8888`.
2. **Global context scan:** query `community_summaries` — top-1 thematic match. This orients the result set toward the most relevant synthesised narrative.
3. **Semantic hit:** query `technical_docs` — top-20 candidates by cosine similarity.
4. **Rerank:** BGE-Reranker-v2-m3 via `:8888` scores all 20 candidates against the original query and returns the top-N by cross-encoder relevance.
5. **Relational expansion:** for each top-N hit, query Neo4j for related entities and facts — surfaces structural context that vector similarity cannot express.

Vector retrieval and graph traversal fail differently. Cosine similarity degrades with semantic crowding. Graph traversal executes structural logic — path length, relationship type, graph density — and does not degrade with interference. As `technical_docs` accumulates interference pressure, facts that become harder to surface through vector retrieval remain fully reachable through graph traversal. The two layers compensate for each other's weaknesses.

---

## 16. LM Studio MCP Configuration

Edit `mcp.json` — replace all `YOUR_*` placeholders with real values and update the absolute path to `vector-skill.py`. Save it to `~/.lmstudio/mcp.json` (or wherever LM Studio reads MCP config on your system).

The `rag-orchestrator` entry runs the custom MCP server for this framework. It is the only memory MCP server needed — it covers semantic retrieval (Tier 1 + Tier 3) and Neo4j graph expansion in a single call, and routes all writes through the coordinator's atomicity and locking guarantees.

> **Why no separate graph MCP?** A direct-bolt Neo4j MCP server (e.g. `neo4j-agent-memory`) bypasses the coordinator entirely: no per-entity locks, no outbox atomicity, no SHA-256 deduplication, and no read-only Cypher guard. Any write it makes produces orphaned Neo4j nodes with no corresponding Postgres record — invisible to semantic search and outside the consolidation pipeline. `rag-orchestrator` already includes Neo4j graph expansion; a separate graph MCP adds ambiguity and write-safety risk without adding capability.

```json
{
  "mcpServers": {
    "rag-orchestrator": {
      "command": "uv",
      "args": [
        "run", "--with", "fastmcp",
        "--with", "httpx",
        "--with", "psycopg2-binary",
        "--with", "neo4j",
        "--with", "python-dotenv",
        "python", "/path/to/your/vector-skill.py"
      ]
    },
    "tavily-mcp": {
      "command": "npx",
      "args": ["-y", "tavily-mcp@latest"],
      "env": {
        "TAVILY_API_KEY": "YOUR_TAVILY_API_KEY"
      }
    }
  }
}
```

### Web search — choose your provider

The framework treats web search as a pluggable MCP slot. The `mcp.json` above uses Tavily; Brave Search is a fully local-key alternative with no per-query metering. Use whichever fits your setup — the rest of the stack does not care which one is registered, as long as the tool name you reference in your system prompt matches the MCP server key.

**Tavily** (default — advanced search, image results, 15-result depth):
```json
"tavily-mcp": {
  "command": "npx",
  "args": ["-y", "tavily-mcp@latest"],
  "env": {
    "TAVILY_API_KEY": "YOUR_TAVILY_API_KEY",
    "DEFAULT_PARAMETERS": "{\"include_images\": true, \"max_results\": 15, \"search_depth\": \"advanced\"}"
  }
}
```
Get a key at [tavily.com](https://tavily.com).

**Brave Search** (alternative — privacy-focused, independent index, no per-query cost on paid plans):
```json
"brave-search": {
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-brave-search"],
  "env": {
    "BRAVE_API_KEY": "YOUR_BRAVE_API_KEY"
  }
}
```
Get a key at [brave.com/search/api](https://brave.com/search/api).

> **Adjust your system prompt to match.** The `COGNITIVE HIERARCHY` section in `system-prompt.md` references the search tool by its MCP server key. If you switch from `tavily-mcp` to `brave-search`, update that reference so the model knows which tool to call for web lookups.

---

## 17. Testing

All tests are fully mocked — no live database or gateway required. Run from the project root.

```bash
# Full suite
uv run --with pytest --with pytest-asyncio --with fastmcp \
       --with psycopg2-binary --with httpx --with neo4j \
       pytest tests/ -v

# Single file
uv run --with pytest --with pytest-asyncio --with fastmcp \
       --with psycopg2-binary --with httpx --with neo4j \
       pytest tests/test_vector_skill.py

# Single test case
uv run --with pytest --with pytest-asyncio --with fastmcp \
       --with psycopg2-binary --with httpx --with neo4j \
       pytest tests/test_vector_skill.py::test_mcp_save_artifact_success

# Skip LLM calls in consolidation tests
MOCK_LLM=1 uv run --with pytest --with pytest-asyncio --with fastmcp \
           --with psycopg2-binary --with httpx --with neo4j \
           pytest tests/test_consolidation_e2e.py
```

| Test file | Coverage |
|---|---|
| `test_memory_bridge.py` | Embedding hard mandate, save idempotency, search + rerank + fallback, Neo4j expansion |
| `test_vector_skill.py` | MCP tool contracts (save, search, health check, reasoning trace) |
| `test_consolidation_e2e.py` | Consolidation cycle with mock LLM, density threshold, community summary write, `source_pg_ids` populated |
| `test_logging.py` | `_append_log` level filtering, per-tool file routing, content size warnings; `save_artifact` logging at each event type; `merge_logs` sort order, multi-tool merge, malformed line handling, daily archive merge, logrotate cleanup |

---

## 18. Open Problems

### Stored Prompt Injection (partially mitigated)

Web-retrieved content enters the same ingestion pipeline as internally authored facts. A crafted document can embed near a legitimate fact cluster and — after consolidation — contaminate `community_summaries` as trusted context for all agents.

**Implemented:** `[BEGIN/END RETRIEVED FACTS]` delimiters and a "treat as DATA" preamble in consolidation prompts harden the Tier 3 synthesis path. Tier 1 retrieval (raw facts in agent context windows) remains unprotected.

**Planned:** ingestion boundary sanitisation; counterfactual simulation pass. Full details in [SECURITY.md](SECURITY.md).

**Do not ingest external or web-retrieved content at volume before implementing the remaining defences.**

### Agent Authentication — implemented (v0.3.5)

All coordinator routes require `Authorization: Bearer <token>`. The gateway verifies the token against the `AGENT_TOKENS` registry and stamps the verified agent identity onto every saved artifact — `agent_id` from the request body is no longer trusted.

Setup: run `uv run python shared-memory/scripts/generate_tokens.py`, add the `AGENT_TOKENS` line to the gateway `.env`, and add `AGENT_TOKEN=<your-token>` to each agent's skill `.env` (or `~/.config/shared-memory/client.env` as a universal fallback). LM Studio requires a full restart after any `AGENT_TOKEN` change. See `SECURITY.md` for the full rollout procedure and token rotation instructions.

### Entity Resolution

The consolidation daemon clusters facts by entity names supplied by callers. Two callers using different names for the same concept (`"hive_mind_proxy"` vs `"Hive-Mind Gateway"`) produce separate clusters and separate community summaries. As the agent population grows, entity resolution — merging synonymous nodes — becomes a real structural problem. Not implemented.

### Consolidation Quality

The daemon trusts the LLM to synthesise accurately. There is no quantitative signal for whether a generated narrative is a sharp thematic abstraction or a lossy blur. Without a quality measure, tuning the density threshold or summarisation prompt is guesswork.

### Density Threshold Calibration

`density_threshold` in `ontology.yaml` (default 5) is architecturally necessary but empirically uncalibrated. Configurable without code changes; the right value for a given corpus requires empirical tuning.

### Observability

Per-save audit logging (§14) records gateway failures, missing entities, and Neo4j sync errors. What it does not provide is a system-level signal for whether consolidation is improving retrieval quality over time.

---

## 19. Development Roadmap — Multi-Agent Safe Workstation

This framework is actively evolving toward a workstation where any number of AI agents can read and write shared memory concurrently without corrupting each other's state, impersonating each other, or poisoning shared narratives. The table below tracks where that transition stands.

### Completed

| Phase | Milestone | Status |
|---|---|---|
| **Foundation** | Three-tier storage (Postgres + Neo4j), BGE-M3 gateway, consolidation daemon, save/search/graph CLI | ✅ Done |
| **Consolidation pipeline** | LISTEN/NOTIFY trigger, explicit entity contract, gateway routing for re-embedding, cumulative narrative synthesis | ✅ Done |
| **Coordinator** | asyncpg connection pool, per-entity `asyncio.Lock`, outbox pattern — all Postgres and Neo4j I/O centralised, ADR-001 cross-DB atomicity risk eliminated | ✅ Done |
| **Concurrency hardening** | FOR UPDATE SKIP LOCKED, atomic retry increment, single UNWIND batch query, acquired-lock tracking, ON CONFLICT upsert for community_summaries, embedding refresh on re-save, LISTEN reconnect, event-loop non-blocking poll | ✅ Done |
| **Security baseline** | Read-only Cypher guard, localhost-only bind (PROXY_BIND opt-in), opaque error responses, bounded limit, ONT label validation at startup, prompt injection delimiters | ✅ Done |
| **Configurable ontology — Path A** | All Neo4j labels and relationship types in `ontology.yaml`; ONT singleton with validation; falls back to hardcoded defaults; density threshold configurable | ✅ Done |
| **Agent integration** | Claude Code, Grok, Gemini CLI, LM Studio (MCP), Codex CLI — all 5 agents live, SKILL.md carries YAML frontmatter for implicit Codex invocation, `AGENTS.md` project context file added | ✅ Done |
| **Schema migrations** | Migration runner; 001 (multi-agent schema: agent_id, scope, visibility, neo4j_outbox); 002 (concurrency hardening: unique index on community_summaries, covering index on outbox); 003 (source provenance: `source_pg_ids integer[]` on community_summaries, back-fill from metadata) | ✅ Done |
| **Provenance layer — Phase A** | PROV-O-inspired ontology: 6 new node labels (`Decision`, `Human`, `AIAgent`, `Project`, `Activity`, `Milestone`) and 8 provenance relationships (`WAS_ATTRIBUTED_TO`, `WAS_ASSISTED_BY`, `WAS_GENERATED_BY`, `PROJECT_OF`, `ACTED_ON_BEHALF_OF`, `SUPERSEDES`, `INFORMED_BY`, `HAD_OUTCOME`). Coordinator ingress validates `type:decision` saves (rejects missing `decided_by` / `project` / `rationale` before the row touches the outbox WAL). Outbox dispatches decision rows to a dedicated `_apply_decision_outbox_row` that materialises the full PROV-O subgraph in a single atomic Neo4j session. Plain `Fact` saves unchanged. | ✅ Done |
| **Provenance layer — Phase B** | `save_decision` subcommand in `memory_bridge.py` (named flags — `--title`, `--decided-by`, `--project`, `--rationale` required; `--assisted-by`, `--alternatives`, `--confidence`, `--entities` optional) and `save_decision` MCP tool in `vector-skill.py`. `build_decision_metadata()` pure helper. `--version` flag added to `memory_bridge.py`. | ✅ Done |
| **Three-test fixes (v0.3.1)** | Retrieval visibility: search results carry `tier`, `score_normalized` (sigmoid), `matched_entities`, structured `graph_context` list. Consolidation history: `summary_history JSONB` column on `community_summaries` (migration 004) — prior summary appended before each `DO UPDATE`, capped at 20. Lineage: `source_ref` optional metadata key flows from coordinator to Neo4j `Fact.source_ref` property. 14 new tests added. `schema.md` "appends new rows" inaccuracy corrected. | ✅ Done |

### In Progress / Planned

| Phase | Milestone | Notes |
|---|---|---|
| **Provenance layer — Phase C** | Retrospective layer: `HAD_OUTCOME` edge written as a dated edge property (not a node) so lineage is preserved without node explosion; Why-To loop — agents query past retrospectives before executing new work in the same area | Phase B is the prerequisite. |
| **Provenance layer — Phase D** ✅ | Four named query shortcuts in `memory_bridge.py query <template>`: `who-decided`, `agent-decisions`, `retrospectives`, `why-to-check`. Filter values sanitised before Cypher interpolation. Raw `graph` subcommand preserved for custom traversals. SKILL.md Task 3 restructured to document both paths. 7 new tests — 91 total. | v0.3.3. |
| **Provenance layer — Phase E** | Separate `pruning_loop.py` on a slow cron; enforces the information foraging heuristic (save if retrieval utility + decision impact > storage cost); `type:decision` and `decision_impact`-flagged rows are unconditionally shielded; plain facts compete on retrieval frequency × age | Decoupled from the consolidation daemon — different cadence. |
| **Agent authentication (Phase 2C)** ✅ | `AGENT_TOKENS` env var; `Authorization: Bearer <token>` DEFAULT DENY middleware; server-side source overwrite; duplicate-token guard; trailing-slash normalisation; 22 new tests | v0.3.5. |
| **Ontology as graph (Path B)** | Bootstrap `(:Class)` nodes + `SCO` relationships from `ontology.yaml` into Neo4j on startup; replace `ONT.*` string constants with startup-cached dict read from graph; enables live ontology inspection and Neosemantics (n10s) forward compatibility | Path A is the prerequisite ✅. Does not replace `ontology.yaml` — yaml stays the human-editable source; graph is a materialised copy. |
| **Entity type enrichment** | Apply Neo4j multi-label to distinguish entity kinds — `:Entity:Person`, `:Entity:System`, `:Entity:Tool`, `:Entity:Decision` etc. — without breaking existing queries | Path A + Path B are the prerequisites. Enables richer graph traversal and type-aware consolidation clustering. |
| **Entity resolution** | Detect and merge synonymous Entity nodes (`"hive_mind_proxy"` ≡ `"Hive-Mind Gateway"`); maintain a canonical name + alias set; re-link Fact nodes on merge | The entity contract (explicit caller-supplied names) makes this tractable. Implementation is a background reconciliation job, not a save-path change. |
| **Horizontal agent expansion** | Packaging guides and integration templates for additional agent types (VS Code extensions, Claude Desktop, any MCP-capable tool, REST-only agents) | The coordinator's HTTP API is already agent-agnostic. New agents require packaging only — no backend changes. |
| **Ingestion boundary sanitisation** | Trust-tier tagging for web-retrieved content; strip instructional patterns; quarantine external facts before Tier 3 promotion | Security prerequisite for ingesting external content at volume. |
| **Counterfactual simulation pass** | Before committing a consolidated narrative, verify every claim traces to a source Fact node; reject narratives that introduce unsourced claims | Completes the stored-injection defence. |
| **Python packaging** | Rename `shared-memory/` → `shared_memory/`, add `__init__.py` files and `pyproject.toml`; replace `sys.path` hack in `vector-skill.py` with `from shared_memory.scripts.ontology import ONT` | Low urgency; enables clean imports when the codebase grows. |

---

## 20. References

- **AI Memory & Cognition: The Architect's Playbook** (Vishakha Gupta, ApertureData, May 2026) — Proposes the KMC Blueprint (Knowledge · Memory · Context) and the three diagnostic tests used in the [§1 Vision](#1-the-vision-one-brain-many-agents) section: Retrieval, Consolidation, and Lineage. [aperturedata.io/resources/ai-memory-cognition-the-architects-playbook](https://www.aperturedata.io/resources/ai-memory-cognition-the-architects-playbook)
- **The Geometry of Forgetting** (Barman et al., 2026) — *Exposing the Dimensionality Illusion*. arXiv:2604.06222
- **The Geometry of Consolidation** (Vangara & Gopinath, 2026) — NeurIPS 2026 submission. Proves centroid averaging collapses retrieval identity.
- **Active Dreaming Memory (ADM)** (Dudekula Kasim Vali, 2025) — Biologically-Inspired Episodic Consolidation. engrXiv preprint, DOI: 10.31224/5919
- **Complementary Learning Systems** (McClelland, McNaughton & O'Reilly, 1995) — *Psychological Review* 102(3):419–457

---

*Neo4j · PostgreSQL/pgvector · BGE-M3 · aiohttp · FastMCP · Docker*

---

## Connect

If this framework is useful to you, or you are building something in the same space — local AI memory, multi-agent architectures, or knowledge graph systems — I would be glad to connect.

I write about these projects and the ideas behind them on LinkedIn and X. Follow for articles, updates, and the reasoning behind architectural decisions that do not fit in a README.

- **LinkedIn:** [linkedin.com/in/xsmotsenigos](https://www.linkedin.com/in/xsmotsenigos/)
- **X:** [x.com/xsmotsenigos](https://x.com/xsmotsenigos/)

---

Copyright 2026 Xenofon S. Motsenigos. Licensed under the [Apache License, Version 2.0](LICENSE).
If you reuse or build on this work, attribution to the original author is appreciated.
