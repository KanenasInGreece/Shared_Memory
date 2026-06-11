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

**▶ [Quick Start — set it up from scratch](#quick-start)** — new here? Start here.

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
    - [10a. Remote Clients: SSH Tunnel Access](#10a-remote-clients-ssh-tunnel-access)
11. [Agent Access: CLI and MCP](#11-agent-access-cli-and-mcp)
    - [11a. Complete Cycle: End-to-End Workflow with Cross-Agent Examples](#11a-complete-cycle-end-to-end-workflow-with-cross-agent-examples)
12. [The Save Path — From Artifact to Memory](#12-the-save-path--from-artifact-to-memory)
13. [The Sleep Cycle — REM and NREM Consolidation](#13-the-sleep-cycle--rem-and-nrem-consolidation)
14. [Audit Logging](#14-audit-logging)
15. [Retrieval: Three-Tier Lookup](#15-retrieval-three-tier-lookup)
16. [LM Studio MCP Configuration](#16-lm-studio-mcp-configuration)
17. [Testing](#17-testing)
18. [Open Problems](#18-open-problems)
19. [Development Roadmap — Multi-Agent Safe Workstation](#19-development-roadmap--multi-agent-safe-workstation)
20. [References](#20-references)

---

## Quick Start

The rest of this README explains *why* each piece exists. This chapter is the *order to do things in* — a complete first-time setup that points to the chapter with the detail for each step, so nothing is repeated. Do the steps in sequence.

**What you are standing up:** a local GraphRAG memory shared by every AI tool on your machine — CLI agents and LM Studio alike. They talk only to one gateway on `127.0.0.1:8888`; the gateway owns Postgres (vectors + facts) and Neo4j (the graph), and runs the REM/NREM sleep cycle that turns saved facts into shared knowledge.

### Two surfaces: usage vs. operations

The framework has two distinct surfaces with separate lifecycles. Conflating them is the most common setup mistake.

| | **Usage** (skill / client) | **Operations** (gateway / daemons) |
|---|---|---|
| What it is | `memory_bridge.py` + `SKILL.md` | gateway, coordinator, REM/NREM daemons, `migrations/` |
| Runs on | **every** agent, every host (incl. remote laptops) | the **one** gateway host |
| Talks to DB/GPU? | No — HTTP to `:8888` only | Yes — owns Postgres, Neo4j, GPU |
| Distributed by | `sync_skills.sh` (thin client only) | this repo, via `git` |
| Upgraded by | re-sync the skill | `git pull` → `migrations/apply.py` → restart gateway |

**Installing the skill is not installing the framework.** The skill is a thin HTTP client; the daemons never run from a skill directory, and a remote agent (no DB, no GPU) cannot run or upgrade them. Daemon and **schema** changes reach a hive through `git` on the gateway host — never through a skill download, so updating a skill never triggers a migration. The operations runbook lives in [`shared-memory/Documentation/server-setup.md`](shared-memory/Documentation/server-setup.md). Steps 1–6 below are operations (gateway host); steps 7–8 are usage (any agent).

**Version contract:** client and gateway are decoupled and may drift, so compatibility is enforced by an `api_version` exchanged on `GET /health` — not by copying daemon code into skills. Run `memory_bridge.py doctor` to check it; on skew it names which side to upgrade.

### Resources & prerequisites

**Hardware — lean minimum:** **16 GB RAM · ~8 GB VRAM · ~30 GB free disk.** Postgres + Neo4j take ~6 GB RAM between them; BGE-M3 and the reranker are small; your **reasoning LLM dominates VRAM**.

**Software:** Docker + Docker Compose · [`uv`](https://docs.astral.sh/uv/) (recommended — every command here uses it; or Python 3.11+ with `pip`) · a server for your reasoning LLM on `:5000` (LM Studio, or any OpenAI-compatible endpoint) — the embedder and reranker run as Docker containers from the compose file, so they need no separate install · at least one consumer that talks to the memory: a **CLI agent** (Claude Code, Antigravity CLI, Grok, or Codex CLI) and/or **LM Studio** — which serves a model and provides a chat interface, reaching the memory through MCP rather than as an agent.

**Reasoning LLM (your choice, on `:5000`):** any OpenAI-compatible local endpoint works. We run **Qwen3-27B**; on the 8 GB lean tier a 7–8B model (e.g. Qwen3-8B) is the practical pick. That choice of model may affect the quality of your graph -- my experience with this is reflected in [**GraphRAG's Hidden Cost**](https://www.linkedin.com/pulse/graphrags-hidden-cost-youre-always-paying-question-when-motsenigos-w81pc/).

**Optional — [`nvtop`](https://github.com/Syllo/nvtop) for GPU-aware dreaming:** if installed, REM/NREM yield while the GPU running your LLM is busy, so consolidation never competes with active inference. It works across Nvidia/AMD/Intel via one `nvtop --snapshot` call. Without it the daemons still run, falling back to the time-based `WRITE_QUIESCE_SEC` guard ([§13](#13-the-sleep-cycle--rem-and-nrem-consolidation)).

### Steps

1. **Get the code, set DB passwords, raise OS limits.**
   Clone the repo and `cp .env.example .env` ([§10](#10-agent-integration-first-time-setup)); fill `NEO4J_PASSWORD` and `PG_PASSWORD` (needed before the databases start). Raise inotify limits and — on Fedora/RHEL — keep the SELinux `:z` mounts ([§4](#4-os-prerequisites--fedora--linux), [§5](#5-infrastructure-setup-docker-compose)).

2. **Start the stack (databases + inference).** First put your BGE-M3 and reranker GGUF files in the folder the compose mounts ([§5](#5-infrastructure-setup-docker-compose)). Then `docker compose -f postgres_neo4j_limits.yaml up -d` brings up Postgres, Neo4j, the embedder (`:8070`) and the reranker (`:8071`); `docker compose … ps` should show all four `healthy`.

3. **Create the schema — one command.** `uv run --with psycopg2-binary python shared-memory/migrations/apply.py` brings an empty DB to the latest schema; then run the one-time Neo4j constraints ([§6](#6-database-schema)). *(Upgrading an older install? Same command — see §6.)*

4. **Start the reasoning LLM.** The embedder and reranker already came up with the compose stack (step 2); you only need your reasoning LLM on `:5000` — LM Studio or any OpenAI-compatible server ([§7](#7-inference-backends-llamacpp)).

5. **Generate tokens and finish your `.env`.** `uv run python shared-memory/scripts/generate_tokens.py` ([§10 token setup](#10-agent-integration-first-time-setup)). Put `AGENT_TOKENS=…` in the **gateway `.env` at the repo root** (alongside the passwords from step 1); put each agent's own `AGENT_TOKEN=…` in that agent's skill `.env`. One distinct token per agent — never shared. `.env.example` is the annotated template.

6. **Start the gateway.** `uv run --with aiohttp --with asyncpg --with neo4j --with httpx python shared-memory/scripts/hive_mind_proxy.py 8888` — this also launches the REM and NREM daemons ([§9](#9-starting-the-full-stack)). Verify: `curl http://localhost:8888/health` should report `"status":"ok"` and `"auth_required":true`.

7. **Install the skill into your agent.** The skill is a **thin client** — only `memory_bridge.py` ships with it (the daemons stay on the gateway host from step 6). Symlink/copy `SKILL.md` + `memory_bridge.py` into the agent's skills directory ([§10](#10-agent-integration-first-time-setup); remote/laptop clients → [§10a](#10a-remote-clients-ssh-tunnel-access)). Shortcut: just tell your agent — *"clone this repo and install the shared-memory skill per README §10."*

8. **Use it.** Activate the skill in your agent — `/shared-memory` (Claude Code, Grok), `$shared-memory` (Codex), `/activate shared-memory` (Antigravity) — and tell the agent to **use the shared-memory skill to recall context before a task and store decisions after** ([§11](#11-agent-access-cli-and-mcp), [§11a](#11a-complete-cycle-end-to-end-workflow-with-cross-agent-examples)). Quick shell smoke test: `memory_bridge.py search "test" 3` ([§10 smoke-test](#10-agent-integration-first-time-setup)).

### Troubleshooting — the first four you'll hit

| Symptom | Likely cause | Fix |
|---|---|---|
| **401 Unauthorized** | `AGENT_TOKEN` missing, or it doesn't match an entry in the gateway's `AGENT_TOKENS` | Re-check both `.env`s (§10). Restart the gateway after editing `AGENT_TOKENS`; restart LM Studio **fully** after changing its token. |
| **503 on save/search** | Embedder/reranker (`:8070`/`:8071`) down or `unhealthy` | Run `docker compose ps` **first** — the inference services have healthchecks; an `unhealthy` one (usually a wrong model path, §5) is the cause. Then `curl :8888/health`. |
| **Search returns HTTP 500** | Migrations not applied — the coordinator hits a missing column | Run `apply.py` (§6). Safe to re-run; it's idempotent. |
| **Silent DB failures / file-watcher errors (Fedora)** | inotify limits too low, or a volume mount missing `:z` so Neo4j/Postgres can't read the host dir | Raise inotify limits (§4); add `:z` to the mounts (§5). |
| *Bonus:* **agent "doesn't know" earlier facts** | the skill was never invoked for that turn | Activate the skill and explicitly ask the agent to **search shared memory first** (step 8). |

> **Maintainers:** this chapter is the single source of setup truth. Any change that affects setup — a new env var, a different model, a schema/migration, a port, a new daemon — **must update Quick Start** in the same change.

---

## 1. The Vision: One Brain, Many Agents

An AI workstation today may be running several tools in parallel — a coding agent, a desktop chat model, a local assistant (agent). Each of them reasons through a problem, discovers something useful, but then the session ends, and all of that is gone. It is not the artifact produced, it is the knowledge gained, the decision process, which we are capturing with this shared memory framework -- The important lessons learned should inform the work you do with other tools, it should be captured as part of the value created and shared so that your whole ecosystem may benefit.

The shared memory framework is built around this idea: your tools should capture and share the knowledge gained from each project they work on. When Gemini CLI figures out why the proxy was failing, any other agent should already know the next time it is asked about the proxy. When LM Studio runs a consolidation on a set of architectural facts, those summaries should be there for any agent that searches next.

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

> **Grade: Passes (v0.3.6).** Search results carry `tier` (fact | community_summary), `score_normalized` (sigmoid of raw reranker logit → [0, 1]), `matched_entities` (intersection of the query string against the saved entity list), and `graph_context` as a structured list of `{rel_type, name, label}` triples. Agent source attribution is server-verified via token authentication — `"source": "gemini"` is a cryptographic guarantee, not a client claim. An agent can reason: *"I returned a Tier-3 community synthesis — normalized score 0.91, matching entity OutboxPattern — alongside two Tier-1 precision hits, both saved by the verified Gemini CLI identity."* **Gaps remaining:** retrieval events are not audited (no record of who searched, when, from which agent); cross-encoder span attribution is not exposed (reranker scores full documents, not spans).

**The Consolidation Test:** *When the agent learns something new, does the system update a coherent knowledge base, or does it just accumulate versions? After six months, do you have one "truth" or three conflicting ones?*

> **Grade: Passes (v0.4.0).** Two-phase REM/NREM sleep cycle now in place. **REM** (idle daemon): enriches each Fact with a full LLM summary and typed entity relationships before it can enter NREM — first-pass entity extraction is agent-supplied only; REM is the first LLM-based entity extraction pass. **NREM** (consolidation daemon): synthesises community summaries only from `rem_processed=true` facts (density ≥ 5 per entity hub). `summary_history JSONB` (migration 004) preserves prior versions. **CommunitySummary supersession:** if A.source_pg_ids ⊆ B.source_pg_ids, A is marked superseded — retrieval always surfaces the most comprehensive non-superseded summary. **Gaps remaining:** cross-entity reconciliation (two summaries for overlapping entities may still diverge until supersession collapses them); during the REM backlog window, Tier 1 raw facts are available but NREM synthesis has not yet run.

**The Lineage Test:** *Can I trace a decision back to the original source — the raw image, the specific video frame, or the precise document page — or just the text summary extracted from it?*

> **Grade: Strong Partial (v0.3.6).** Decisions trace fully to human (`WAS_ATTRIBUTED_TO`), AI agent (`WAS_ASSISTED_BY`), and project (`PROJECT_OF`). `HAD_OUTCOME` dated edges close the forward trace: decision → outcome → rating + notes (the Why-To loop). Community summaries link back to source facts via `source_pg_ids`. The optional `source_ref` key (e.g. `"design-doc.pdf#p12"`, `"meeting.mp4@00:04:32"`) propagates to `Fact.source_ref` in Neo4j. Agent source attribution is server-verified (v0.3.6). **Gaps remaining:** `source_ref` is not enforced — agents supply it when they can; no back-edge from a raw `Fact` to the `Decision` it influenced; read events are not audited.

### What we are building toward

Beyond storing facts, the framework is evolving to answer questions about how your work happened — who decided what, with which tool, in which context, and whether it held up:

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
   BGE-base-768: evaluated — acceptable, weaker multilingual coverage.
   BGE-M3-1024: selected — strong multilingual retrieval at 1024 dimensions.

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
│                              AGENT LAYER                                    │
│                                                                            │
│  Claude Code · Grok · Codex CLI · Antigravity CLI    LM Studio    Any      │
│  (skills — memory_bridge.py)                         (MCP —       HTTP     │
│                                                vector-skill.py)   client    │
└────────────────────────────────┬───────────────────────────────────────────┘
                                 │ HTTP — every memory op (agents never touch the DBs)
                       ┌─────────▼───────────────────────────────────┐
                       │  Hive-Mind Gateway + Coordinator            │
                       │  hive_mind_proxy.py — binds 127.0.0.1:8888  │
                       │  Bearer-token auth · DEFAULT DENY           │
                       │                                             │
                       │  /memory/save|search|graph → coordinator.py │
                       │  /v1/embeddings → :8070 (BGE-M3, 1024-dim)  │
                       │  /v1/reranking  → :8071 (BGE-Reranker)      │
                       │  default        → :5000 (reasoning LLM)     │
                       └─────┬───────────────────────────────┬───────┘
                  spawns +   │ watchdogs        coordinator   │ owns all DB I/O
                       ┌─────▼──────────────────┐             │
                       │  Sleep-cycle daemons   │             │
                       │  REM  — rem_loop.py     │             │
                       │  NREM — consolidation_  │             │
                       │         loop.py         │             │
                       │  (LISTEN new_artifact)  │             │
                       └─────┬──────────────────┘             │
                             │ writes                          │ reads / writes
              ┌──────────────▼──────────────────────────────────▼─┐
              │                  MEMORY LAYER                      │
              │  ┌──────────────────────┐   ┌───────────────┐     │
              │  │  PostgreSQL+pgvector │   │     Neo4j     │     │
              │  │ technical_docs       │   │ Fact nodes    │     │
              │  │  (Tier 1 — Episodic) │   │ Entity hubs   │     │
              │  │ community_summaries  │   │ MENTIONS      │     │
              │  │  (Tier 3 — Semantic) │   │ CommunitySumm │     │
              │  │ neo4j_outbox (WAL)   │   │ SUMMARIZED_BY │     │
              │  └──────────────────────┘   └───────────────┘     │
              └───────────────────────────────────────────────────┘
```

| Tier | Store | Role | Biological Analogy |
|---|---|---|---|
| **1 — Episodic** | `technical_docs` (Postgres + pgvector) | Original facts, full content, surgical precision via cosine similarity | Hippocampus — fast, specific, pattern-separated |
| **2 — Structural** | Neo4j `Fact` nodes (keyed by `pg_id`) | Relationships, provenance, `consolidated` flag, Entity hubs | Hippocampus — relational context cosine similarity cannot express |
| **3 — Semantic** | `community_summaries` (Postgres + pgvector) | Consolidated thematic narratives; queried first on retrieval | Neocortex — slow, abstract, statistical regularities across episodes |

**Retrieval always queries Tier 3 first** (thematic orientation), then Tier 1 (surgical precision), then expands through Neo4j (relational context). Artifacts saved by one agent become retrievable by all others once the sleep cycle runs (§13).

### Topology enforcement

The topology above is not a convention agents are trusted to follow — each property is enforced in code. These are the steps that keep one shared brain coherent under concurrent multi-agent load:

| Property | How it is enforced | Where |
|---|---|---|
| **One shared embedding space (1024-dim)** | Agents call only `:8888`; the coordinator alone calls `:8070`/`:8071`. No agent can introduce a foreign vector space. | gateway routing + `EMBED_URL`/`RERANK_URL` in `coordinator.py` |
| **Localhost-only by default** | Gateway binds `127.0.0.1`; `PROXY_BIND=0.0.0.0` is opt-in and documented as overlay-network-only. | `hive_mind_proxy.py` (§9) |
| **Caller authentication** | Every route except `/health` requires a registered `Authorization: Bearer` token (DEFAULT DENY); the server overwrites `source` with the verified agent name, so identity cannot be spoofed. | `auth_middleware` in `coordinator.py` (§10) |
| **No direct DB access** | Agents hold no database credentials; all Postgres/Neo4j I/O flows through the coordinator's `asyncpg` pool and per-entity `asyncio.Lock`s. | `coordinator.py` (§11–§12) |
| **Read-only graph queries** | `/memory/graph` rejects `CREATE`/`DELETE`/`SET`/`MERGE`/`CALL`/`DROP`/`LOAD CSV`; the Neo4j session is opened read-only as a second layer. | `coordinator.py` (§11) |
| **Cross-DB atomicity** | Every save writes a `neo4j_outbox` row in the same Postgres transaction; the worker applies Neo4j asynchronously — no orphaned `Fact` nodes. | outbox worker, `coordinator.py` (§12) |
| **Hard embedding mandate** | A save returns `503` after 4 failed embed retries rather than storing a vectorless, unsearchable row. | `coordinator.py` (§12) |
| **Authenticated daemons** | REM and NREM run as registered agents (`rem_daemon`, `consolidation`); the gateway injects their tokens so their own embedding/LLM calls authenticate like any agent. | `hive_mind_proxy.py` (§9) |

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

`postgres_neo4j_limits.yaml` defines the whole local stack as **four** services — the two persistent stores (**neo4j**, **postgres**) and the two inference backends (**retriever-api**, the BGE-M3 embedder on `:8070`, and **reranker-api**, BGE-Reranker-v2-m3 on `:8071`, both llama.cpp `server` containers). One `docker compose up -d` brings up all four. The key structure:

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

  # --- Inference layer (llama.cpp server containers) ---
  retriever-api:                 # BGE-M3 embedder
    image: ghcr.io/ggml-org/llama.cpp:server
    ports: ["8070:8070"]
    volumes:
      - /path/to/your/LLM_Models:/models:ro,z   # host models folder → /models in-container
    command: >
      -m /models/gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf
      --embedding --host 0.0.0.0 --port 8070 -c 8192 -b 8192 -ub 8192
    healthcheck:                 # surfaces as healthy/unhealthy in `docker compose ps`
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8070/health"]
      interval: 30s
      retries: 3
      start_period: 60s
    restart: always

  reranker-api:                  # BGE-Reranker-v2-m3
    image: ghcr.io/ggml-org/llama.cpp:server
    ports: ["8071:8071"]
    volumes:
      - /path/to/your/LLM_Models:/models:ro,z
    command: >
      -m /models/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf
      --rerank --host 0.0.0.0 --port 8071 -c 8192 -b 8192 -ub 8192
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8071/health"]
      interval: 30s
      retries: 3
      start_period: 60s
    restart: always
```

```bash
# Start both services
docker compose -f postgres_neo4j_limits.yaml up -d

# Verify
docker compose -f postgres_neo4j_limits.yaml ps
```

Credentials are read from environment variables — copy `.env.example` to `.env` and fill in `NEO4J_PASSWORD` and `PG_PASSWORD` before starting.

> **Place the models where the compose expects them.** The two inference containers mount a host models directory read-only (`/path/to/your/LLM_Models:/models:ro,z`) and load each GGUF by path (`-m /models/gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf`, and the reranker equivalent). Put your GGUF files under that host folder at those sub-paths — or edit the mount and `-m` paths to match where you keep them. A wrong path lets the container start but then fail its healthcheck.

> **Healthchecks are the first troubleshooting step.** Both inference services declare a Docker `healthcheck` against `/health`. Run `docker compose -f postgres_neo4j_limits.yaml ps` and read the **STATUS** column — `healthy`, `starting`, or `unhealthy`. An `unhealthy` embedder or reranker (almost always a wrong model path or too little RAM) is why saves and searches return 503 — check this before debugging the gateway.

---

## 6. Database Schema

The Postgres schema is managed by versioned migrations in `shared-memory/migrations/`, applied by `apply.py`. The runner executes every migration in order, and every migration is idempotent (`IF NOT EXISTS` throughout) — so it is always safe to re-run.

### New install — one command

With the databases up (§5) and `PG_PASSWORD` in `.env`, run:

```bash
uv run --with psycopg2-binary python shared-memory/migrations/apply.py
```

That is the only schema command a new user runs. It takes an empty `agent_data` database all the way to the latest schema in one step:

- `000` creates the `vector` extension and the two base tables (`technical_docs`, `community_summaries`).
- `001` adds the multi-agent columns (`agent_id`, `scope`, `visibility`) and the `neo4j_outbox` table.
- `002`–`006` add concurrency indexes, `source_pg_ids`, `summary_history`, and the `superseded` flag, plus one-time data back-fills.

The resulting tables — `technical_docs` (Tier 1), `community_summaries` (Tier 3), and `neo4j_outbox` (coordinator WAL) — are documented column-by-column, with all Neo4j labels and relationship types, in [`shared-memory/Documentation/schema.md`](shared-memory/Documentation/schema.md).

### Neo4j constraints — one-time

Neo4j constraints are **not** created automatically. Run these once in Neo4j Browser or cypher-shell (also idempotent):

```cypher
CREATE CONSTRAINT fact_pg_id    IF NOT EXISTS FOR (f:Fact)             REQUIRE f.pg_id IS UNIQUE;
CREATE CONSTRAINT entity_name   IF NOT EXISTS FOR (e:Entity)           REQUIRE e.name  IS UNIQUE;
CREATE CONSTRAINT summary_pg_id IF NOT EXISTS FOR (s:CommunitySummary) REQUIRE s.pg_id IS UNIQUE;
```

### Upgrading from an earlier schema

If you started on an older schema — before the coordinator, before REM/NREM, or before supersession — run the **same one command**:

```bash
uv run --with psycopg2-binary python shared-memory/migrations/apply.py
```

Because every migration is additive and `IF NOT EXISTS`, and `apply.py` re-runs the full set each time, this brings any existing database up to the latest schema **without dropping data**: missing columns and tables are added, and the one-time data migrations run (003/005 back-fill `source_pg_ids`; 006 normalises historical `source` values). Existing rows are preserved — pre-coordinator rows default to `agent_id='legacy'`, `scope`/`visibility='global'`. Re-running later is a no-op. Migration `000` is a no-op on a database that already has the tables, so new and upgrading users run the identical command.

> **Key schema rule:** Every fact saved must include `"entities": ["Name1", "Name2"]` in its metadata. The saver creates `Entity` nodes and `MENTIONS` edges for each name. Without them the fact is stored and retrievable by vector search, but consolidation will never cluster it into Tier 3. The graph layer is the prerequisite for the semantic layer.

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

Two models serve the embedding and reranking paths. In the default setup they run as the **`retriever-api`** (BGE-M3, `:8070`) and **`reranker-api`** (BGE-Reranker-v2-m3, `:8071`) services in the compose file — so `docker compose up -d` (§5) already started them; see §5 for where to place the GGUF files. A third port (`5000`) hosts the reasoning LLM (LM Studio or any OpenAI-compatible endpoint) — this one is **not** in the compose file, so start it yourself.

To run the embedder and reranker outside Docker instead, launch `llama-server` directly (point `-m` at your GGUF files):

```bash
# BGE-M3 — embedding model, port 8070
llama-server -m /path/to/bge-m3-Q8_0.gguf --port 8070 --embedding -c 8192 -b 8192 -ub 8192

# BGE-Reranker-v2-m3 — reranking model, port 8071
llama-server -m /path/to/bge-reranker-v2-m3-Q8_0.gguf --port 8071 --rerank -c 8192 -b 8192 -ub 8192
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

**1. Start databases + inference** — one compose brings up Postgres, Neo4j, the BGE-M3 embedder (`:8070`) and the BGE-Reranker (`:8071`):
```bash
docker compose -f postgres_neo4j_limits.yaml up -d
docker compose -f postgres_neo4j_limits.yaml ps    # all four should reach 'healthy'
```
First run? Make sure the GGUF models are in the mounted folder before this — see §5.

**2. Start the reasoning LLM** (LM Studio or any OpenAI-compatible server on port 5000 — this one is *not* in the compose file)

**3. Start the Hive-Mind Gateway** — this also starts the REM and NREM daemons automatically
```bash
uv run --with aiohttp --with asyncpg --with neo4j --with httpx \
  python shared-memory/scripts/hive_mind_proxy.py 8888
```

You will see log lines confirming all three are up:
```
INFO  ### Hive-Mind Proxy on :8888 [aiohttp]
INFO  Consolidation daemon started (pid XXXXX)
INFO  REM daemon started (pid XXXXX)
INFO  Listening for 'new_artifact' notifications...
INFO:REMDaemon:REM daemon started (poll=120s, batch=5)
```

**4. LM Studio** — start the application; it will pick up the MCP servers from `mcp.json` automatically.

The gateway is the only repo script you start by hand — databases and both inference backends come up with `docker compose up -d`. The proxy starts both daemons; all processes shut down cleanly when the proxy receives SIGINT or SIGTERM.

**Verify the full stack is healthy:**
```bash
curl http://localhost:8888/health
# {"status":"ok","embedder":"ok","reranker":"ok","llm":"ok","daemon":"running","rem_daemon":"running","auth_required":true}
```

HTTP 200 means the save/search path (embedder + reranker) is operational. HTTP 503 means at least one critical backend is down — do not attempt saves until resolved (run `docker compose ps`; an `unhealthy` embedder or reranker is the usual cause). `daemon` and `rem_daemon` are informational; their degradation affects consolidation/enrichment only. `auth_required: true` confirms token authentication is enforced.

**Daemon watchdogs:** the gateway automatically restarts both the NREM consolidation daemon and the REM daemon if either crashes, with independent exponential backoff and circuit breakers (5 crashes / 10 min each). If a circuit breaker trips, restart the gateway.

**Daemon tokens (architecture note):** The REM and consolidation daemons are registered agents in `AGENT_TOKENS` (`rem_daemon` and `consolidation`). The gateway injects their `AGENT_TOKEN` into the subprocess environment so their outbound calls (embeddings, LLM) are authenticated through the proxy like any other agent. The daemon token is used **only for authentication** — it identifies the caller as a trusted internal process. It does **not** affect source attribution: `Fact.source` always reflects the original saving agent (e.g. `"claude"`, `"gemini"`). The daemon enriches facts on behalf of their authors; it does not claim ownership. Add tokens to `.env` → `AGENT_TOKENS` before starting the gateway; the proxy reads them at startup and passes the correct one to each subprocess.

> **Network exposure:** The gateway binds to `127.0.0.1:8888` by default — localhost only. Set `PROXY_BIND=0.0.0.0` in `.env` to opt into all-interfaces binding, but only over an encrypted overlay network (Tailscale, WireGuard, or TLS). Bearer tokens are plaintext over HTTP. See [SECURITY.md](SECURITY.md) for details.

---

## 10. Agent Integration: First-Time Setup

This section covers where to place files and how to register each agent. For runtime usage (commands and examples) see [§11: Agent Access: CLI and MCP](#11-agent-access-cli-and-mcp).

> **The skill is a thin client.** The only script it needs is `memory_bridge.py` (an HTTP client to the gateway on `:8888`). The daemons run on the gateway host from this repo (§9) — **never install a daemon into a skill dir**. Each per-agent block below symlinks `memory_bridge.py` alone; standing up the gateway/daemons is a separate, gateway-host task ([server-setup.md](shared-memory/Documentation/server-setup.md)). After installing, run `memory_bridge.py doctor` to confirm the client and gateway agree on `api_version`.

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

### Token setup — one-time, all agents

The coordinator requires `Authorization: Bearer <token>` on all memory routes. Generate tokens once, then configure each agent's install.

```bash
# Step 1 — generate tokens (run from repo root)
uv run python shared-memory/scripts/generate_tokens.py
```

This prints something like:

```
=== Gateway .env — add this line ===
AGENT_TOKENS=claude:tok_abc123...,gemini:tok_def456...,lm_studio:tok_ghi789...,...

=== Per-agent .env — copy the matching AGENT_TOKEN line ===
  claude           AGENT_TOKEN=tok_abc123...
  gemini           AGENT_TOKEN=tok_def456...
  lm_studio        AGENT_TOKEN=tok_ghi789...
  ...
```

```bash
# Step 2 — add AGENT_TOKENS to the gateway .env (already open from above)
echo 'AGENT_TOKENS=claude:tok_abc123...,gemini:tok_def456...,lm_studio:tok_ghi789...' >> .env

# Step 3a — Claude Code: add AGENT_TOKEN to the skill .env
echo 'AGENT_TOKEN=tok_abc123...' >> ~/.claude/skills/shared-memory/.env

# Step 3b — Antigravity CLI (or legacy Gemini CLI): add to the skill .env
echo 'AGENT_TOKEN=tok_def456...' >> ~/.gemini/skills/shared-memory/.env

# Step 3c — LM Studio: add to mcp.json env block (see §16)
# "env": { "AGENT_TOKEN": "tok_ghi789..." }
# Then restart LM Studio completely.

# Step 4 — restart the gateway to load AGENT_TOKENS
```

**After the gateway restarts**, its startup log confirms auth is active:
```
INFO  coordinator auth enabled — 6 agent(s): antigravity, claude, codex, gemini, grok, lm_studio
```

**Token search order** — `memory_bridge.py` loads `AGENT_TOKEN` from up to two `.env` locations (first definition of each variable wins):
1. the nearest `.env` found by walking up from the `scripts/` directory (`python-dotenv`'s `find_dotenv()`) — normally the skill-root `~/.{agent}/skills/shared-memory/.env`, which is exactly where Step 3 places the token
2. a `scripts/`-adjacent `.env` (`~/.{agent}/skills/shared-memory/scripts/.env`), if you placed one there

Both require `memory_bridge.py` to be invoked by absolute path so `__file__` resolves to the skill directory. See the skill's path note for the correct invocation form.

If `python-dotenv` is not installed (e.g. bare `uv run --with httpx python ...`), a built-in plain-Python parser reads the skill-root and `scripts/`-adjacent `.env` files directly — auth works regardless.

Each token maps to a verified agent identity. Never share tokens across agents on a multi-agent machine.

**Backward compatible:** If `AGENT_TOKENS` is unset, the coordinator accepts all requests (existing installs are unaffected until you add the variable).

**Read-only roles (`AGENT_ROLES`).** A registered token can be confined to read-only access by adding it to `AGENT_ROLES` in the gateway `.env`:

```bash
# Gateway .env — comma-separated name:role pairs; role is "read" or "full".
AGENT_ROLES=monitor:read
```

A `read` token may reach only `GET /health`, `GET /memory/telemetry`, and `POST /memory/graph` (which already enforces a read-only Cypher guard). Every other route — `save`, `retrospective`, `search`, and the embeddings/LLM proxy passthrough — returns **403**. Roles only ever *narrow* access; a token must still be a valid `AGENT_TOKENS` entry. `AGENT_ROLES` unset (or `name:full`) preserves full read/write — the backward-compatible default. `generate_tokens.py` mints a `monitor` identity and emits the matching `AGENT_ROLES=monitor:read` line.

This exists so read-only ops clients — e.g. the companion **Shared Memory Monitor** dashboard — hold their own dedicated, non-write-capable identity instead of borrowing an agent's full-access token. A leaked monitor token cannot save or poison memory.

### Smoke-test the bridge

After the full stack is running and tokens are configured, verify the bridge works from any shell:

```bash
uv run --with httpx --with python-dotenv \
  python /path/to/Shared_Memory/shared-memory/scripts/memory_bridge.py search "test" 3
```

### Claude Code

Claude Code loads skills from `~/.claude/skills/`. Create the skill directory with a symlink so scripts always stay in sync with the repo:

```bash
mkdir -p ~/.claude/skills/shared-memory/scripts

# Symlink the CLIENT SCRIPT ONLY — the skill is a thin client. The daemons are
# server-side and run from the repo on the gateway host (see server-setup.md).
ln -s /path/to/Shared_Memory/shared-memory/scripts/memory_bridge.py ~/.claude/skills/shared-memory/scripts/memory_bridge.py

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
mkdir -p ~/.grok/skills/shared-memory/scripts

# Symlink the client script only (thin client — daemons stay on the gateway host)
ln -s /path/to/Shared_Memory/shared-memory/scripts/memory_bridge.py ~/.grok/skills/shared-memory/scripts/memory_bridge.py

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
mkdir -p ~/.codex/skills/shared-memory/scripts

# Symlink the client script only (thin client — daemons stay on the gateway host)
ln -s /path/to/Shared_Memory/shared-memory/scripts/memory_bridge.py ~/.codex/skills/shared-memory/scripts/memory_bridge.py

# Copy SKILL.md
cp shared-memory/SKILL.md ~/.codex/skills/shared-memory/SKILL.md
```

Invoke explicitly in any Codex CLI session:

```
$shared-memory
```

Codex CLI also supports **implicit invocation**: if the description in SKILL.md's frontmatter matches the task, the skill is loaded automatically without an explicit `$` call.

> **AGENTS.md:** Codex CLI reads `AGENTS.md` at the project root before each session (their equivalent of `CLAUDE.md`). This repo provides `AGENTS.md` alongside `AGENT.md` — both contain the same architectural guidance.

### Antigravity CLI (and Gemini CLI)

**Antigravity CLI** (`agy`) is the current CLI for this skill, replacing Gemini CLI. Both tools load skills from `~/.gemini/skills/` — the same install directory works for both.

```bash
mkdir -p ~/.gemini/skills

# Copy (standalone — updates require a re-copy)
cp -r shared-memory-skill/shared-memory ~/.gemini/skills/shared-memory

# Or symlink (always in sync with the repo)
ln -s /path/to/Shared_Memory/shared-memory-skill/shared-memory ~/.gemini/skills/shared-memory
```

Activate in any Antigravity CLI session:

```
/activate shared-memory
```

> **New clients:** install the skill to `~/.gemini/skills/shared-memory/`, place your `AGENT_TOKEN` in `~/.gemini/skills/shared-memory/.env`, and invoke `memory_bridge.py` by its absolute path: `~/.gemini/skills/shared-memory/scripts/memory_bridge.py`. See the [Path Setup note](#path-setup) in SKILL.md for the substitution table.

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

Edit `mcp.json` from this repo: replace all `YOUR_*` placeholders with real values, update the absolute path to `vector-skill.py`, and add `AGENT_TOKEN` to the `rag-orchestrator` env block:

```json
"rag-orchestrator": {
  "command": "uv",
  "args": ["run", "--with", "fastmcp", "--with", "httpx",
           "--with", "psycopg2-binary", "--with", "neo4j",
           "--with", "python-dotenv", "python", "/path/to/vector-skill.py"],
  "env": {
    "NEO4J_PASSWORD":  "your-neo4j-password",
    "PG_PASSWORD":     "your-postgres-password",
    "AGENT_TOKEN":     "tok_your_lm_studio_token"
  }
}
```

Save it to LM Studio's MCP config location (`~/.lmstudio/mcp.json` on Linux and macOS). **Restart LM Studio completely after any `AGENT_TOKEN` change** — the MCP server process is cached and does not hot-reload env vars.

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

## 10a. Remote Clients: SSH Tunnel Access

A remote client is any machine that runs an AI CLI tool (Antigravity CLI, Claude Code, Grok, etc.) but **cannot run the infrastructure** — no Docker, no Postgres, no Neo4j, no BGE models. Only `memory_bridge.py` runs on the remote machine; all storage and compute stay on the host.

**Requirements on the remote machine:**
- `uv` — install with `curl -LsSf https://astral.sh/uv/install.sh | sh` (handles all Python dependencies automatically). Alternatively: `python3` + `pip install httpx python-dotenv` and replace `uv run --with httpx --with python-dotenv python` with `python3` in all commands.
- SSH access to the machine running the gateway
- A distinct token registered in the gateway's `AGENT_TOKENS`

> **Not required remotely:** Docker, the databases, the BGE models, and `nvtop`. GPU-aware dreaming ([§13](#13-the-sleep-cycle--rem-and-nrem-consolidation)) runs entirely on the infrastructure host — `nvtop` belongs there, not on the remote client. Inference you trigger remotely still executes on the host GPU, so it is detected correctly.

### Step 1 — Register a token for this remote agent

On the **host machine**, add a new named entry to `AGENT_TOKENS` in the gateway `.env`. Use a descriptive name that identifies both the tool and the machine (e.g. `laptop-agy`, `chromebook-agy`):

```bash
# Generate a token for the remote agent
python3 -c "import secrets; print('tok_' + secrets.token_urlsafe(24))"

# Add to the gateway .env — append to the existing AGENT_TOKENS line:
# AGENT_TOKENS=claude:tok_...,gemini:tok_...,laptop-agy:tok_<generated>
```

**Restart the gateway** and confirm the new agent appears in the startup log:
```
INFO  coordinator auth enabled — N agent(s): ..., laptop-agy, ...
```

> **Identity matters:** the token name is the agent's identity. The coordinator stamps it as `source` on every saved artifact — it is how the knowledge graph distinguishes which machine contributed which fact. Never share tokens across agents or machines.

### Step 2 — Open an SSH tunnel to the gateway

```bash
# Keep this running in a terminal while using shared memory:
ssh -N -L 8888:localhost:8888 user@your-gateway-host
```

This maps `localhost:8888` on the remote machine to port `8888` on the gateway host. `memory_bridge.py` defaults to `http://localhost:8888` and reaches the gateway transparently through the tunnel.

**Persistent tunnel via `~/.ssh/config`** (recommended — survives shell restarts):

```
Host gateway-tunnel
    HostName your-gateway-host
    User your-user
    LocalForward 8888 localhost:8888
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

Then `ssh -N gateway-tunnel` or set `ExitOnForwardFailure yes` and add it to your session startup.

### Step 3 — Install the skill

```bash
mkdir -p ~/.gemini/skills/shared-memory/scripts

curl -fsSL https://raw.githubusercontent.com/KanenasInGreece/Shared_Memory/main/shared-memory/scripts/memory_bridge.py \
  -o ~/.gemini/skills/shared-memory/scripts/memory_bridge.py

curl -fsSL https://raw.githubusercontent.com/KanenasInGreece/Shared_Memory/main/shared-memory-skill/shared-memory/SKILL.md \
  -o ~/.gemini/skills/shared-memory/SKILL.md
```

### Step 4 — Configure the remote `.env`

```bash
printf 'AGENT_TOKEN=tok_<your-token>\nCOORDINATOR_URL=http://localhost:8888\n' \
  > ~/.gemini/skills/shared-memory/.env
```

`COORDINATOR_URL` defaults to `http://localhost:8888` — correct for SSH-tunnel clients since the tunnel maps the remote gateway port to local. Set it explicitly only if your tunnel uses a different local port.

### Step 5 — Verify

```bash
uv run --with httpx \
  python ~/.gemini/skills/shared-memory/scripts/memory_bridge.py --version
# → {"version": "0.4.6", "api_version": 1, "tool": "shared-memory-framework"}

uv run --with httpx \
  python ~/.gemini/skills/shared-memory/scripts/memory_bridge.py search "test" 3
```

A valid response (even empty results) confirms the tunnel, token, and `.env` are all working correctly.

### Updating the skill

When a new version is released, re-run the two `curl` commands from Step 3 to pull the latest `memory_bridge.py` and `SKILL.md`. No other files are needed on the remote machine. Then run `memory_bridge.py doctor` — if it reports `compat: incompatible`, the gateway and this client disagree on `api_version`; upgrade whichever side it names (the gateway upgrades via `git pull` + restart on its host, not from here).

---

## 11. Agent Access: CLI and MCP

| Consumer | Interface | Entry point | Consolidation trigger |
|---|---|---|---|
| **Claude Code** | CLI (skill `/shared-memory`) | `~/.claude/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **Codex CLI** | CLI (skill `$shared-memory`) | `~/.codex/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **Grok** | CLI (skill `/shared-memory`) | `~/.grok/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **Antigravity CLI** (`agy`) | CLI (skill `/activate shared-memory`) | `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **Gemini CLI** *(legacy)* | CLI (skill `/activate shared-memory`) | `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` | via coordinator → `pg_notify` |
| **LM Studio** | MCP (FastMCP) | `vector-skill.py` → `rag-orchestrator` in `mcp.json` | via coordinator → `pg_notify` |
| **Any HTTP client** | REST | `POST http://localhost:8888/memory/save\|search\|graph` | via coordinator → `pg_notify` |

All three paths route through the coordinator on port 8888. The coordinator owns all Postgres and Neo4j connections — agents no longer connect to the databases directly. The Hive-Mind Gateway must be running before any save or search.

### CLI usage

```bash
# Check the framework version
python shared-memory/scripts/memory_bridge.py --version
# → {"version": "0.4.6", "api_version": 1, "tool": "shared-memory-framework"}

# Operational telemetry — outbox health + REM/NREM backlog snapshot (add --json for raw)
python shared-memory/scripts/memory_bridge.py status

# Search — semantic + rerank + Neo4j expansion
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py search "bgem3 interference problem" 5

# Save — always include source and entities
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py save \
  "The proxy routes all embeddings through :8888 to enforce 1024-dim consistency." \
  '{"source":"claude_code","entities":["hive_mind_proxy","BGE-M3","SharedMemory"]}'

# Save a decision — structured flags, no JSON blob required
uv run --with httpx --with python-dotenv \
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
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query who-decided --project shared-memory

# Named query shortcuts (no raw Cypher required)
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query why-to-check --title "gateway"
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query agent-decisions --assisted-by claude
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query retrospectives --rating good

# Raw Cypher — entity hub sizes (top referenced concepts)
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py graph \
  "MATCH (e:Entity)<-[:MENTIONS]-(f:Fact) RETURN e.name, count(f) AS refs ORDER BY refs DESC LIMIT 10"
```

### Coordinator HTTP API

The coordinator exposes six memory endpoints on port 8888. All routes (except `/health`) require `Authorization: Bearer <token>`.

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/memory/save` | `{content, metadata, agent_id?, scope?, visibility?}` | `{status, pg_id, neo4j, message}` |
| `POST` | `/memory/search` | `{query, limit?, scope?, agent_id?}` | `{status, results[]}` |
| `POST` | `/memory/graph` | `{cypher, params?}` | `{status, records[]}` |
| `POST` | `/memory/retrospective` | `{pg_id, rating, notes, date?, agent_id?}` | `{status, target_pg_id}` |
| `GET` | `/memory/status/{pg_id}` | — | `{pg_id, neo4j, retries, applied_at}` |
| `GET` | `/memory/telemetry` | — | `{status, telemetry: {postgres, neo4j, nrem, breakdown}}` — outbox + dream-cycle backlog rollup, NREM consolidation-cycle counts, and metadata distributions. The coordinator owns both backends and does the joins, so a read-only client can render a full dashboard from this one call with no direct DB access. (v0.4.3; `nrem`/`breakdown` added v0.4.4) |
| `GET` | `/health` | — | `{status, embedder, reranker, llm, daemon, rem_daemon, auth_required}` |

> **`/memory/graph` is read-only enforced.** Queries containing `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `MERGE`, `CALL`, `LOAD CSV`, or `DROP` are rejected with HTTP 400 before reaching Neo4j.

**Write acknowledgment:** saves return `200 OK` once the fact is committed to Postgres. Use `GET /memory/status/{pg_id}` to confirm Neo4j application, or pass `?consistency=neo4j` to block until the outbox row is applied.

### Skill activation

```
/shared-memory          # Claude Code and Grok
$shared-memory          # Codex CLI (explicit); also auto-matched via SKILL.md description
/activate shared-memory # Gemini CLI
```

### Optional: Shared Memory Monitor (companion dashboard)

**Telemetry is built into the framework.** The coordinator emits a full operational snapshot at `GET /memory/telemetry` — outbox health, the REM/NREM dream-cycle backlog, NREM consolidation-cycle counts (`nrem`), and metadata distributions (`breakdown`) — and any agent can read it directly via `memory_bridge.py status` (see [§11 CLI usage](#cli-usage)). This works with or without any dashboard installed; the telemetry is part of the gateway, not an add-on.

[**Shared Memory Monitor**](https://github.com/KanenasInGreece/Shared_Memory_Monitor) is a separate, optional dashboard that simply gives that **existing** telemetry a visual: REM/NREM backlog charts, outbox/gateway/daemon status, metadata breakdown, and live log tailing at `http://127.0.0.1:8765/`. It adds **no instrumentation of its own** and ships nothing server-side — it is purely a viewer over `GET /memory/telemetry`. Install it only if you want the visualisation.

It is a pure **read-only client**: it authenticates with its own dedicated `monitor` token (`AGENT_ROLES=monitor:read` — see [§10](#read-only-roles-agent_roles)) and renders the entire dashboard from `GET /memory/telemetry` (`nrem` + `breakdown`) and read-only `POST /memory/graph`. With that token it **cannot** save, search, or reach the inference proxy, and needs **no Postgres or Neo4j credentials** — the coordinator does the joins. To enable it, mint a `monitor` token (`generate_tokens.py`), add `monitor:tok_…` to `AGENT_TOKENS` and `AGENT_ROLES=monitor:read` to the gateway `.env`, restart the gateway, and point the monitor's own `.env` at that token. See the monitor repo's README for setup.

---

## 11a. Complete Cycle: End-to-End Workflow with Cross-Agent Examples

This section shows the full memory lifecycle: one agent saves knowledge, the two-phase sleep cycle (REM enrichment → NREM consolidation, §13) synthesises it, a different agent retrieves it later, then a retrospective closes the loop.

### Step 1 — Claude Code saves a decision

Claude Code works through an architectural choice and saves it with full provenance:

```bash
# Claude Code session — invoked via /shared-memory or by the model directly
uv run --with httpx --with python-dotenv \
  python ~/.claude/skills/shared-memory/scripts/memory_bridge.py save_decision \
  --title "Use outbox-as-WAL for Neo4j writes" \
  --decided-by "Xenofon" \
  --project "shared-memory" \
  --rationale "Postgres transaction commits atomically with the outbox row. If the process crashes, the row survives and is replayed on restart — Neo4j can never be ahead of Postgres." \
  --assisted-by "claude-sonnet-4-6" \
  --alternatives "synchronous writes,no Neo4j,event sourcing" \
  --confidence "high" \
  --entities "OutboxPattern,Neo4j,Postgres,SharedMemory"
```

Response:
```json
{
  "status": "success",
  "pg_id": 42,
  "neo4j": "pending",
  "message": "Artifact stored with ID 42."
}
```

`pg_id=42` is the Postgres row ID. Note it — you'll use it to attach a retrospective later. `neo4j: "pending"` means the outbox worker will apply the Neo4j write asynchronously (typically within seconds).

### Step 2 — Plain fact saved by Antigravity CLI

Later the same day, Antigravity CLI (`agy`) is debugging the proxy restart sequence and discovers something worth preserving:

```bash
# Antigravity CLI (agy) session — invoked via /activate shared-memory
uv run --with httpx --with python-dotenv \
  python ~/.gemini/skills/shared-memory/scripts/memory_bridge.py save \
  "On gateway restart, any neo4j_outbox rows with status='in_progress' are reset to 'pending' by coordinator.start(). This prevents double-processing when a crash left rows claimed but not applied." \
  '{"source":"antigravity","entities":["OutboxPattern","coordinator","SharedMemory"],"source_ref":"coordinator.py#start()"}'
```

Response:
```json
{
  "status": "success",
  "pg_id": 47,
  "neo4j": "pending",
  "message": "Artifact stored with ID 47."
}
```

Note `"source_ref":"coordinator.py#start()"` — this propagates to the Neo4j `Fact` node, preserving the back-link to the exact code location.

### Step 3 — The sleep cycle runs (automatic, no action needed)

This is where the two-phase sleep cycle (§13) turns the raw saves into shared, consolidated knowledge — with no action from any agent.

**REM enrichment — within ~2 minutes.** The REM daemon polls every 120 s, takes the new `OutboxPattern` facts oldest-first (only those whose Neo4j write is `applied`), and makes one LLM call per fact to rewrite it into a concise summary and attach typed entity relationships. It sets `rem_processed = true` on each fact and re-notifies NREM.

```
INFO:REMDaemon:REM cycle: 2 fact(s) to process (pg_ids=[42, 47])
INFO:REMDaemon:REM: pg_id=42 done (decision=True, rels=4, outbox_marked=True)
INFO:REMDaemon:REM: pg_id=47 done (decision=False, rels=3, outbox_marked=True)
```

**NREM consolidation — after the idle window.** The NREM daemon waits for 15 minutes of quiet on the `new_artifact` channel (45-minute hard backstop), then counts unconsolidated Fact neighbours on the `OutboxPattern` hub — **only the `rem_processed` ones**. Once that reaches the density threshold (5) it integrates the new facts into the existing summary cumulatively, re-embeds the narrative via BGE-M3, and writes it to `community_summaries`. An older summary whose source facts are now subsumed is marked `superseded`.

```
INFO:ConsolidationDaemon:Idle threshold reached. Starting consolidation.
INFO:ConsolidationDaemon:Generated summary for 'OutboxPattern'. Vectorizing...
INFO:ConsolidationDaemon:Successfully consolidated 6 facts for 'OutboxPattern'.
```

> **REM gates NREM.** A fact REM has not enriched yet is fully searchable as a Tier-1 hit but does not count toward consolidation — so a freshly-saved fact appears in search immediately, while the thematic Tier-3 summary follows once REM enriches it and the cluster crosses the threshold.

### Step 4 — A different agent searches and finds both pieces

The next morning, Grok (or any other agent) starts a session with no prior context about the outbox:

```bash
# Grok session — invoked via /shared-memory
uv run --with httpx --with python-dotenv \
  python ~/.grok/skills/shared-memory/scripts/memory_bridge.py search \
  "how does the coordinator handle Neo4j writes safely" 5
```

Condensed response:
```json
{
  "status": "success",
  "results": [
    {
      "tier": "community_summary",
      "content": "The SharedMemory coordinator uses an outbox pattern (neo4j_outbox table) to guarantee that Postgres and Neo4j remain consistent. Every save commits the fact and an outbox row atomically. An asynchronous worker applies the Neo4j write and marks it 'applied'. On restart, any rows stuck in 'in_progress' are reset to 'pending', preventing double-processing. This design means Neo4j can never be ahead of Postgres, and a crash never creates orphaned Fact nodes.",
      "score": null,
      "score_normalized": null,
      "matched_entities": [],
      "graph_context": []
    },
    {
      "tier": "fact",
      "content": "Use outbox-as-WAL for Neo4j writes\n\nPostgres transaction commits atomically...",
      "score": 3.21,
      "score_normalized": 0.96,
      "matched_entities": ["OutboxPattern", "Neo4j", "Postgres"],
      "graph_context": [
        {"rel_type": "WAS_ATTRIBUTED_TO", "name": "Xenofon",         "label": "Human"},
        {"rel_type": "WAS_ASSISTED_BY",   "name": "claude-sonnet-4-6","label": "AIAgent"},
        {"rel_type": "PROJECT_OF",        "name": "shared-memory",    "label": "Project"}
      ]
    },
    {
      "tier": "fact",
      "content": "On gateway restart, any neo4j_outbox rows with status='in_progress' are reset...",
      "score": 2.88,
      "score_normalized": 0.95,
      "matched_entities": ["OutboxPattern"],
      "graph_context": [
        {"rel_type": "MENTIONS", "name": "OutboxPattern", "label": "Entity"}
      ]
    }
  ]
}
```

Grok gets:
- **Tier-3** — the consolidated narrative synthesised from everything Claude Code and Antigravity CLI saved (the first result always orients the agent)
- **Tier-1 fact** — the original decision Claude Code saved, with full provenance (who, which AI, which project)
- **Tier-1 fact** — Antigravity CLI's finding about restart recovery, with a `source_ref` link back to the code

Neither piece of knowledge was created by Grok. Neither existed in Grok's context window before this search. The shared brain made both available.

### Step 5 — Query the provenance graph directly

Any agent can query the knowledge graph to understand the decision chain:

```bash
# Who decided, and which AI assisted — named shortcut
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query who-decided \
  --title "outbox" --project "shared-memory"

# → [{d.title: "Use outbox-as-WAL for Neo4j writes",
#     decided_by: "Xenofon",
#     assisted_by: "claude-sonnet-4-6",
#     d.date: "2026-05-29",
#     project: "shared-memory"}]

# What decisions has Claude Code assisted with?
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query agent-decisions \
  --assisted-by "claude-sonnet-4-6"

# Check retrospectives before starting work in this area (the Why-To protocol)
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query why-to-check \
  --title "outbox"
# → No retrospective yet — the decision is recent. Record one after 30 days.
```

### Step 6 — Record a retrospective (closing the loop)

Four weeks later, the outbox has been running in production. Any agent can record the outcome:

```bash
# LM Studio records the retrospective via MCP tool:
# save_retrospective(pg_id=42, rating="high",
#   notes="Held up under multi-agent concurrent load. Outbox replay on crash worked correctly. Neo4j lag < 200 ms typical. No orphaned Fact nodes after 4 weeks.",
#   source="qwen3-27b")

# Or from the CLI (any agent):
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py save_retrospective \
  --pg-id 42 \
  --rating "high" \
  --notes "Held up under multi-agent concurrent load. Outbox replay on crash worked correctly. Neo4j lag < 200 ms typical. No orphaned Fact nodes after 4 weeks." \
  --source "antigravity"
```

Response:
```json
{"status": "success", "target_pg_id": 42}
```

Now the Why-To check returns something useful for any future agent:

```bash
uv run --with httpx --with python-dotenv \
  python shared-memory/scripts/memory_bridge.py query why-to-check \
  --title "outbox"

# → [{d.title: "Use outbox-as-WAL for Neo4j writes",
#     o.rating: "high",
#     o.notes:  "Held up under multi-agent concurrent load...",
#     o.date:   "2026-06-26",
#     decided_by: "Xenofon"}]
```

Before touching outbox logic, any agent can check whether past decisions held up. This is the Why-To loop — decision → outcome → inform the next decision.

### Step 7 — LM Studio (MCP path)

LM Studio uses the MCP tools from `rag-orchestrator`. The model calls them automatically when the system prompt's search-first directive is active.

**Save a fact:**
```
Tool: save_artifact
Args: {
  "content": "The consolidation daemon uses AsyncGraphDatabase (neo4j async driver) so the event loop is never blocked during Neo4j I/O. psycopg2 calls use run_in_executor for the same reason.",
  "metadata": "{\"source\":\"qwen3-27b\",\"entities\":[\"consolidation_loop\",\"AsyncGraphDatabase\",\"SharedMemory\"]}"
}
```

**Search:**
```
Tool: hybrid_search_and_rerank
Args: {"query": "why does the consolidation daemon use async neo4j driver", "limit": 5}
```

The model receives Tier-3 orientation + Tier-1 precision hits + Neo4j graph context — the same result shape as the CLI, but surfaced inline in the chat.

**Save a decision:**
```
Tool: save_decision
Args: {
  "title": "Use AsyncGraphDatabase in consolidation daemon",
  "decided_by": "Xenofon",
  "project": "shared-memory",
  "rationale": "Sync GraphDatabase inside async def blocked the event loop for every Neo4j round-trip, causing LISTEN/NOTIFY drops under write bursts.",
  "source": "qwen3-27b",
  "assisted_by": "qwen3-27b",
  "entities": "AsyncGraphDatabase,consolidation_loop,SharedMemory"
}
```

**Save a retrospective (close the Why-To loop from LM Studio):**
```
Tool: save_retrospective
Args: {
  "pg_id": 42,
  "rating": "high",
  "notes": "No NOTIFY drops observed after migration to async. Event loop latency stable under 6-agent concurrent write test.",
  "source": "qwen3-27b"
}
```

---

## 12. The Save Path — From Artifact to Memory

The save path runs inside the coordinator (`coordinator.py`) on every `POST /memory/save`:

```
caller: POST /memory/save {content, metadata, agent_id, scope, visibility}
  Authorization: Bearer <token>  ← verified against AGENT_TOKENS registry
       ↓ 401 if token missing or unrecognised
  metadata["source"] ← overwritten with verified agent name (server-side)
       ↓
embed(content) → embedder :8070 directly (coordinator runs inside the gateway) — retry ×4, linear backoff
       ↓ 503 if all retries fail — hard mandate: no save without a vector
acquire per-entity asyncio.Lock for each name in metadata["entities"]
       ↓ serializes concurrent writes to the same entity cluster
BEGIN TRANSACTION
  INSERT INTO technical_docs ... ON CONFLICT (content_hash) DO UPDATE
       ↓ idempotent: SHA-256 hash prevents duplicates; verified source stored
  INSERT INTO neo4j_outbox (pg_id, cypher_params)
       ↓ outbox row committed atomically with the fact
  SELECT pg_notify('new_artifact', {"pg_id": id})
COMMIT  ← 200 OK returned to caller here (Postgres-ack)
       ↓ async outbox worker applies Neo4j write (MERGE Fact + Entity + MENTIONS)
NOTIFY 'new_artifact' → NREM resets its idle timer; REM enriches the fact first,
                        then it becomes eligible for consolidation (rem_processed, §13)
```

> **Hard Mandate — Embedding Integrity:** Saves return 503 if the embedding service is unreachable after all retries. An artifact without a vector is invisible to semantic search — this failure must surface, never be swallowed.

> **Why `:8070` here, not `:8888`?** Agents must always embed through the gateway (`:8888`) so every vector shares one space (§7). The coordinator is the process *behind* `:8888` — if it called `:8888` it would hit its own auth middleware (401) and loop on itself. It therefore calls the embedder (`:8070`) directly. The single-embedding-space guarantee is intact: `:8070` is the very BGE-M3 the gateway routes embedding traffic to.

> **Per-entity write serialization:** Concurrent saves targeting the same entity are serialized via `asyncio.Lock[entity_name]`, acquired in sorted order to avoid deadlock between concurrent saves. This prevents duplicate `Entity` hub creation under agent-swarm concurrency and ensures the sleep cycle sees a consistent cluster. (A future multi-process deployment would replace this in-process lock with Postgres advisory locks.)

> **Cross-DB atomicity:** The outbox row is written in the same Postgres transaction as the fact. If the process crashes after commit, the outbox row survives and the outbox worker replays the Neo4j write on restart — the ADR-001 dangling-`Fact` window is eliminated.

> **Audit logging:** Every event in the save path — coordinator unreachable, malformed metadata, missing entities, Neo4j sync failures, and successful saves — is optionally logged based on `MEMORY_LOG_LEVEL`. See [§14: Audit Logging](#14-audit-logging).

---

## 13. The Sleep Cycle — REM and NREM Consolidation

Since v0.4.0 the sleep cycle is **two-phase**, modelled on the biological division between REM and slow-wave (NREM) sleep. Two daemons run, both auto-started by the gateway:

- **REM** (`rem_loop.py`) — *enrichment*. Operates on individual `Fact` **and `Decision`** nodes: rewrites each `Fact` into an LLM summary and attaches typed entity relationships; for a `Decision` it extracts the reasoning layer — `CONSIDERED`/`REJECTED` alternatives and `PRODUCES_INSIGHT`/`UNDER_CONDITIONS` from the rationale — onto the node (v0.4.3). This is the first LLM-based entity-extraction pass; on save, entities are agent-supplied only.
- **NREM** (`consolidation_loop.py`) — *consolidation*. Operates on Entity hub *communities*: synthesises high-density clusters of REM-enriched facts into thematic `community_summaries`. This is the neocortical layer.

A fact must pass through REM before NREM will ever consolidate it. The phases are gated, not parallel.

### Phase 1 — REM (enrichment), `rem_loop.py`

Unlike NREM, REM **polls** — every 120 seconds, a batch of 5 facts (the per-fact LLM call is the latency bottleneck). Backlog enrichment has to drain steadily regardless of `NOTIFY` traffic, so it cannot be purely event-driven.

Per cycle:

1. Fetch the oldest non-REM `Fact` **or `Decision`** nodes (`pg_id ASC` — clears the historical backlog first).
2. **Gate on outbox `status='applied'`** — only enrich facts whose Neo4j write is confirmed.
3. Batch-fetch full content from Postgres in one query, over a single AUTOCOMMIT connection per cycle (replacing per-operation connection churn).
4. Build a closed typed-node registry from the graph (`Human`, `AIAgent`, `Project`, `Decision`, `Entity`) so a name's label never changes mid-batch.
5. One LLM round-trip per fact: a ≤5-sentence summary **plus** typed entity→relationship assignments. For `Decision` facts it additionally extracts `CONSIDERED`, `REJECTED`, `UNDER_CONDITIONS`, and `PRODUCES_INSIGHT` edges.
6. Write to Neo4j in one session — entity `MERGE` edges on the node first (a `Fact`, or the `Decision`), Decision extras second, then the processed-mark **last**: a `Fact` gets `SET f.content = summary, f.rem_processed = true`; a `Decision` gets `SET d.rem_summary = summary, d.rem_processed = true` (its rationale is never overwritten). Marking last means a partial failure never leaves a node processed.
7. Notify NREM (`pg_notify('new_artifact', pg_id)`) so the entity cluster is re-evaluated, then mark the outbox row `rem_reviewed`.

### Phase 2 — NREM (consolidation), `consolidation_loop.py`

NREM **does not poll for work** — polling would compete with inference workloads that need full GPU headroom. It waits for a Postgres `NOTIFY`, then applies a dual gate: an idle timer and a graph density check. `new_artifact` notifications arrive from **two senders**: the coordinator at save time, and REM after it enriches a fact (Phase 1, step 7). The save-time notification is usually premature for the fact that triggered it — REM has not run yet, so the fact is not `rem_processed` and the density gate excludes it. It is REM's post-enrichment notification that makes the fact countable and re-opens its entity cluster for evaluation.

- Each `pg_notify` adds the artifact's `pg_id` to `pending_pg_ids` and resets a 15-minute idle timer. Consolidation runs after 15 minutes of quiet; a 45-minute hard backstop prevents indefinite deferral during continuous ingestion.
- The queued `pg_id`s are entry points into Neo4j, not the consolidation targets. From each, NREM traverses to Entity hubs and counts unconsolidated Fact neighbours — **but only those with `rem_processed = true`**. Raw, un-enriched facts are never consolidated directly. Communities with fewer than 5 qualifying facts wait.
- **Domain-scoped (migration 007):** within each Entity hub, facts are partitioned by `domain = COALESCE(metadata->>'project', metadata->>'domain', scope, 'general')`, and the density threshold is re-applied **per (entity, domain)**. Facts that share an entity but belong to unrelated domains are never fused into one narrative. Summaries are keyed on `(entity, domain)`; untagged facts collapse to `general`, reproducing the prior single-summary-per-entity behaviour until agents tag their saves.
- **Outbox dream-cycle ledger:** `NOTIFY` is fire-and-forget — a notification sent while the daemon is down (restart, crash, host reboot) is lost. The durable record is the `neo4j_outbox` table: a fact's row now lives through `pending → applied → rem_reviewed → consolidated → deleted`. `consolidated` is set **in the same transaction** as the community-summary INSERT the fact was folded into; the row is deleted only after the Neo4j marking succeeds — a row's presence always means "this artifact has not finished dreaming", and its absence is the conclusive record that **both stores are synced**. The set of `rem_reviewed` fact rows is therefore the NREM backlog, restart-proof.
- **Ledger sweep:** on each `NREM_SWEEP_INTERVAL_SEC` tick (default 3600 s; idle-gated, yields to GPU inference), NREM backfills already-covered rows, **reconciles** any rows stuck at `consolidated` (a crash between the Postgres commit and the Neo4j marking — the sweep re-applies the idempotent marking and closes the rows), and, when the `rem_reviewed` backlog reaches the density threshold, feeds those `pg_id`s to the same anchored cluster query the event path uses. Once per process start, an **unanchored global graph sweep** also runs — the only pass that reaches pre-coordinator facts that have no outbox rows.
- **Fail-safe write order:** the Postgres transaction (summary + supersession + ledger flag) commits **before** the graph marking. A crash between the stores leaves facts unmarked in Neo4j and ledger rows at `consolidated` — repairable state the next sweep heals — instead of the old failure mode (graph-marked facts with no committed summary, stranded invisibly).

A community-summary write **closes the loop**: it is a direct `INSERT` into `community_summaries` plus an inline Neo4j sync — it produces no *new* `neo4j_outbox` row and no `new_artifact` NOTIFY (it only advances, then deletes, the ledger rows of the facts it consumed), so consolidation can never re-trigger itself.

### Phase 2b — Insight consolidation (decision clusters → `kind='insight'`)

NREM has a second cluster path for **decisions**. A solitary decision is never round-tripped back to Postgres — it is already Tier-1-searchable via its own embedding. Value exists only in synthesis across **linked decisions**, so the unit of work is a decision *cluster* (`insight_threshold: 2` in `ontology.yaml`), and the output is an elevated `community_summaries` row with `metadata.kind = "insight"`.

**The eligibility gate is pure graph state — no LLM, no rating taxonomy:**

1. ≥ 2 unconsolidated, REM-enriched, non-reversed `:Decision` nodes converge on a shared `:Entity` that also carries at least one `:Fact` (insights stay grounded in verified knowledge);
2. the shared entity is **not a mega-hub** (total degree ≤ `INSIGHT_HUB_DEGREE_CAP`, default 50 — clustering through a hub like the project itself links everything to everything);
3. the decisions span **≥ 2 distinct projects** — cross-project recurrence is the signal that a principle generalises;
4. at least one decision in the cluster has a `HAD_OUTCOME` edge — the **existence** of a retrospective, never its rating, is the trust gate. Two untested decisions agreeing is consensus, not verification.

The fold prompt receives each decision's full Postgres content plus **every retrospective verbatim** (`HAD_OUTCOME` edge properties are the permanent outcome archive). Valence lives in the synthesised narrative — a positive outcome strengthens the principle, a negative or reversed one bounds it ("held when…, failed when…").

**Insights are always-INSERT; supersession is the dedup.** Insight rows are exempt from the `(entity, domain)` upsert key (partial unique index, migration 009): a later retrospective on any source decision **re-folds the same `source_pg_ids`**, and the equal source set rides the covered-subset supersession rule — the new insight (now carrying the outcome wording) supersedes the old, and retrieval only ever surfaces the active one.

**The trigger is the durable ledger, not NOTIFY.** Decisions have no `:Fact` node, so the event path is structurally deaf to them. Decision and retrospective outbox rows now complete the same lifecycle as facts: an open retrospective row means "this outcome has not been folded yet" and re-triggers the fold; the consumed rows flip to `consolidated` transactionally with the insight INSERT and are deleted (logged) after the graph marking. A retrospective row whose decision belongs to no insight and no qualifying cluster **stays open deliberately** — durable backlog, not a stuck outbox.

**Reversal is the one structural rating:** `save_retrospective` with `rating="reversed"` marks the decision `superseded` in both stores — Tier-1 search excludes it and it never seeds a *fresh* cluster. Existing insights are never invalidated without replacement: the re-fold supersedes them with a narrative that records the reversal as boundary evidence.

Retrieval elevation: `handle_search` returns the nearest active insight **above** the nearest thematic summary, tagged `tier: "insight_summary"` with decision ids in `source_pg_ids` (mirrored in the LM Studio MCP path).

For each community that meets the threshold:

1. Fetch the most recent `CommunitySummary` for that `(Entity, domain)` pair from Postgres (if any).
2. Call the LLM via `:8888 → :5000` to integrate the new facts into the existing narrative — **cumulative**, not a new isolated snapshot. This prevents content drift from parallel summary fragments about the same entity.
3. Re-embed the new narrative via BGE-M3 through `:8888`.
4. Write to `community_summaries`; create/update the `CommunitySummary` node in Neo4j; link source Facts via `SUMMARIZED_BY`; set `Fact.consolidated = true`.

> **Why centroid averaging is not used:** The obvious compression approach — averaging related embeddings into a centroid — collapses the angular distinctions that cosine similarity depends on (Vangara & Gopinath, 2026, *"The Geometry of Consolidation"*). The LLM instead generates new language representing the theme of the cluster, which is then re-embedded from scratch. This produces a new semantic point that did not exist before — not a mathematical blend. Retrievable volume grows O(log n) with LLM-based consolidation versus O(n) without it.

### Yielding to active inference (GPU-aware)

Both daemons drive the LLM at `:8888 → :5000`, the same endpoint a user's chat uses. Beyond the time-based `WRITE_QUIESCE_SEC` guard (REM yields if a fact was saved in the last 30 s), each phase also checks whether the **GPU running the LLM is busy** before starting a cycle, and defers if so — logged at **WARNING**.

Detection is cross-architecture via `nvtop --snapshot` (one JSON path for Nvidia/AMD/Intel — no per-vendor `nvidia-smi`/`rocm-smi`/sysfs parsing, which breaks unevenly; Intel Arc exposes no `gpu_busy_percent`). The gate is **platform-agnostic**: it makes no assumption about which server runs on `:5000` — the only contract is an OpenAI-compatible completions endpoint. Any GPU at/above the threshold counts as busy, so dreaming yields equally to the local LLM, a **direct chat that bypasses the gateway**, or any **unrelated GPU app** — none of which a process-name or request-count heuristic would catch. By default every GPU is gated; set `GPU_INDICES` to scope the gate to specific cards on a multi-GPU host. **nvtop is a prerequisite but the probe fails open:** if it is absent or errors, the check is skipped (logged once) and only the time-guard applies. NREM's 45-minute hard backstop always fires regardless of GPU state, so consolidation is never starved. Tunables: `SLOT_AWARE` (default on), `GPU_BUSY_PERCENT` (default 50), `GPU_INDICES`, `NVTOP_BIN`, `NVTOP_TIMEOUT_SEC`.

**Remote clients:** REM and NREM run on the **infrastructure host** (the gateway spawns them), so `nvtop --snapshot` always samples the GPU that actually serves inference — including generation that [remote clients](#10a-remote-clients-ssh-tunnel-access) trigger through the gateway, since that load lands on the host's `:5000`/GPU. **Install nvtop on the infrastructure host only**; remote client machines need nothing for this. (The probe assumes the LLM is co-located with the daemons, as in the standard topology; if your `:5000` is on a different machine, set `SLOT_AWARE=0`.)

### Supersession

After each NREM pass, any active summary whose `source_pg_ids` is a strict subset of the new summary's is marked `superseded = true` in Postgres and linked `(new)-[:SUPERSEDES]->(old)` in Neo4j. Tier 3 retrieval filters `WHERE NOT superseded`, so a stale, narrower summary is never surfaced once a more comprehensive one absorbs its source facts. Supersession is cross-entity — an "Outbox" summary can supersede a "Neo4j" summary if it absorbed all the same source facts.

### Re-consolidation

The `consolidated` flag is not permanent. If future ingestion introduces unflagged Facts with sufficient neighbourhood density that pull previously-consolidated Facts back into a candidate community, the entire cluster becomes eligible again.

---

## 14. Audit Logging

There are two independent, opt-in logs:

1. **Per-save logging** — the save path in both `memory_bridge.py` and `vector-skill.py` writes structured JSON entries to per-tool files, gated by `MEMORY_LOG_LEVEL`. Covered first below.
2. **REM outbox audit log** — the REM daemon (`rem_loop.py`, §13 Phase 1) appends each applied outbox row to a single JSON-lines file, gated by `AUDIT_LOG_PATH`. Covered at the end of this section.

Both are **off by default**.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORY_LOG_LEVEL` | `0` (off) | Per-save logging: controls which events are logged |
| `MEMORY_LOG_PATH` | `~/.shared-memory/logs` | Per-save logging: directory where log files are written |
| `AUDIT_LOG_PATH` | unset (off) | REM outbox audit log: file path; empty/unset disables it |

### Log levels

| Level | Events logged |
|---|---|
| `0` | Nothing (default) |
| `1` | **Warnings** — save succeeded but `entities` missing; fact is stored but ineligible for consolidation |
| `2` | Warnings + **errors** — backend unreachable (`coordinator_down` from the CLI, `gateway_down` from the MCP), token rejected (`auth_failed`), malformed metadata JSON (`bad_metadata`), non-dict metadata (`bad_metadata_type`), missing `source` (`missing_source`), coordinator returned an error (`save_failed`), Neo4j sync failure (`neo4j_sync_failed`) |
| `3` | All above + **successful saves** (`save_success`) — records `pg_id`, `source`, and entity count on every completed save |
| `4` | All above + **full content copy** — adds the complete `content` field to each entry; adds a `content_size_warn` note if content exceeds 10 KB |

### Per-tool log files

Each entry point writes to its own file. Concurrent writes from CLI agents (both using `memory_bridge.py`) are safe — `O_APPEND` mode writes are atomic on Linux for writes smaller than `PIPE_BUF` (4096 bytes); individual log lines are well within that limit. Rotation is excluded from the writing tools to eliminate any write/rotate race condition.

| Tool | Log file |
|---|---|
| CLI agents (Claude · Grok · Codex · Antigravity CLI) — all via `memory_bridge.py` | `{MEMORY_LOG_PATH}/memory_bridge.log` |
| LM Studio MCP (`vector-skill.py`) | `{MEMORY_LOG_PATH}/vector_skill.log` |

### Log format

Each line is a self-contained JSON object:

```json
{"ts": "2026-05-24T14:32:01.123456", "tool": "memory_bridge", "event": "no_entities", "pg_id": 42, "source": "gemini_cli"}
{"ts": "2026-05-24T14:35:17.891234", "tool": "memory_bridge", "event": "save_success", "pg_id": 43, "source": "gemini_cli", "entity_count": 2}
{"ts": "2026-05-24T14:41:03.552109", "tool": "vector_skill",   "event": "gateway_down", "content_preview": "Architectural dec..."}
```

The two tools emit slightly different `event` sets — notably they name the unreachable-backend case differently (`coordinator_down` from the CLI, `gateway_down` from the MCP):

- **`memory_bridge`** (CLI agents): `no_entities`, `bad_metadata`, `bad_metadata_type`, `auth_failed`, `coordinator_down`, `save_failed`, `save_success`
- **`vector_skill`** (LM Studio MCP): `no_entities`, `bad_metadata`, `bad_metadata_type`, `missing_source`, `gateway_down`, `neo4j_sync_failed`, `save_success`

### Daily merge of the per-tool logs (NREM consolidation daemon)

The **NREM consolidation daemon** (`consolidation_loop.py`) runs `merge_logs()` once per calendar day — on the first tick of its 1-second `LISTEN` poll after the date rolls over. Only the two per-tool *save* logs are merged; the REM outbox audit log (`AUDIT_LOG_PATH`, below) is separate and is never rotated or merged. The merge uses the logrotate pattern:

1. Rename `memory_bridge.log` → `memory_bridge.log.rotating` and `vector_skill.log` → `vector_skill.log.rotating` (an empty per-tool log is just deleted). Writing tools create fresh files on next open.
2. Parse all entries from both rotating files, group by calendar date, and sort each day's entries by timestamp.
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

### REM outbox audit log (`AUDIT_LOG_PATH`)

Distinct from the per-save logs above. The per-save logs record what each *agent* did on the save path and are gated by `MEMORY_LOG_LEVEL`. The REM outbox audit log records what the **REM daemon** did on the *write* path — a forensic trail of exactly which Neo4j writes were applied and when.

Set `AUDIT_LOG_PATH` to a writable file path to enable it (unset or empty disables it — the default). During REM enrichment (§13 Phase 1), immediately before each processed outbox row is marked `rem_reviewed`, the daemon appends that row to the file as one JSON line. Unlike the per-save logs, this file is **never rotated or merged** — it is a plain append-only ledger, so point it at a path you rotate externally if it must stay bounded.

Each line carries the applied outbox row plus an ingest timestamp:

```json
{"ts": "2026-06-04T09:14:22.481Z", "outbox_id": 311, "pg_id": 205, "cypher_params": {"pg_id": 205, "entities": ["OutboxPattern"], "source": "claude"}, "created_at": "2026-06-04T09:02:10Z", "applied_at": "2026-06-04T09:02:11Z"}
```

| Field | Meaning |
|---|---|
| `ts` | When REM wrote this audit entry (ISO-8601, UTC) |
| `outbox_id` | `neo4j_outbox` row ID |
| `pg_id` | `technical_docs` row the write corresponds to |
| `cypher_params` | The parameters applied to Neo4j for this fact |
| `created_at` | When the outbox row was first written (save time) |
| `applied_at` | When the outbox worker applied it to Neo4j |

---

## 15. Retrieval: Three-Tier Lookup

Both the MCP tool (`hybrid_search_and_rerank` in `vector-skill.py`) and the CLI (`memory_bridge.py search`) implement the same retrieval chain:

1. **Embed the query** via BGE-M3 through `:8888`.
2. **Global context scan:** query `community_summaries` — the nearest active **insight** (`kind='insight'`, cross-project principle — surfaced first as `tier: "insight_summary"`) and the nearest thematic match. Insights outrank thematic narratives: a principle validated by at least one retrospective carries more weight than a single-domain synthesis.
3. **Semantic hit:** query `technical_docs` — top-20 candidates by cosine similarity, excluding `superseded` rows (reversed decisions, migration 009).
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

**Implemented:** both LLM-processing stages are hardened. The REM enrichment pass (`rem_loop.py`) and the NREM consolidation pass (`consolidation_loop.py`) each wrap fact content in `[BEGIN/END … CONTENT]` delimiters behind a "treat this as DATA, not instructions" preamble, so an injected `"Ignore previous…"` string is processed as data rather than obeyed. This protects the Tier 3 synthesis path and the typed-relationship extraction that feeds it. **Still unprotected:** Tier 1 retrieval — raw facts surfaced in an agent's context window during search are returned verbatim, with no sanitisation.

**Planned:** ingestion boundary sanitisation; counterfactual simulation pass (both in §19). Full details in [SECURITY.md](SECURITY.md).

**Do not ingest external or web-retrieved content at volume before implementing the remaining defences.**

### Agent Authentication — RESOLVED (v0.3.5)

Formerly the top open problem: any localhost process could read or write shared memory and claim any agent identity. **Closed in v0.3.5** — all coordinator routes now require `Authorization: Bearer <token>` (DEFAULT DENY), and the gateway stamps the verified agent identity onto every saved artifact, so `agent_id` from the request body is no longer trusted. Setup and token rotation live in [§10 (Token setup)](#10-agent-integration-first-time-setup) and [SECURITY.md](SECURITY.md); the milestone is tracked under Completed in §19. Retained here as the record of a closed problem.

### Entity Resolution

The consolidation daemon clusters facts by entity names supplied by callers. Two callers using different names for the same concept (`"hive_mind_proxy"` vs `"Hive-Mind Gateway"`) produce separate clusters and separate community summaries. As the agent population grows, entity resolution — merging synonymous nodes — becomes a real structural problem. Not implemented.

### Consolidation Quality

The daemon trusts the LLM to synthesise accurately. There is no quantitative signal for whether a generated narrative is a sharp thematic abstraction or a lossy blur. Without a quality measure, tuning the density threshold or summarisation prompt is guesswork.

### Density Threshold Calibration

`density_threshold` in `ontology.yaml` (default 5) is architecturally necessary but empirically uncalibrated. Configurable without code changes; the right value for a given corpus requires empirical tuning.

### Observability

Per-save audit logging (§14) records gateway failures, missing entities, and Neo4j sync errors, and `GET /memory/telemetry` — via `memory_bridge.py status` (v0.4.3) — gives a system-level operational snapshot (outbox health and the REM/NREM backlog). What is still missing is a **quality** signal: whether consolidation is improving retrieval over time.

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
| **Agent integration** | Claude Code, Grok, Codex CLI, Antigravity CLI (`agy` — current, replacing Gemini CLI, which stays supported as legacy), LM Studio (MCP) — all live; SKILL.md carries YAML frontmatter for implicit Codex invocation; `AGENTS.md` project context file added | ✅ Done |
| **Schema migrations** | Migration runner; 000 (base schema: `vector` extension + `technical_docs`/`community_summaries` base tables — makes `apply.py` a one-command install); 001 (multi-agent schema: agent_id, scope, visibility, neo4j_outbox); 002 (concurrency hardening: unique index on community_summaries, covering index on outbox); 003 (source provenance: `source_pg_ids integer[]` on community_summaries, back-fill from metadata); 004 (`summary_history JSONB`); 005 (corrected `source_pg_ids` back-fill + applies 004); 006 (REM supersession: `superseded` column + partial index, source normalisation back-fill) | ✅ Done |
| **Provenance layer — Phase A** | PROV-O-inspired ontology: 6 new node labels (`Decision`, `Human`, `AIAgent`, `Project`, `Activity`, `Milestone`) and 8 provenance relationships (`WAS_ATTRIBUTED_TO`, `WAS_ASSISTED_BY`, `WAS_GENERATED_BY`, `PROJECT_OF`, `ACTED_ON_BEHALF_OF`, `SUPERSEDES`, `INFORMED_BY`, `HAD_OUTCOME`). Coordinator ingress validates `type:decision` saves (rejects missing `decided_by` / `project` / `rationale` before the row touches the outbox WAL). Outbox dispatches decision rows to a dedicated `_apply_decision_outbox_row` that materialises the full PROV-O subgraph in a single atomic Neo4j session. Plain `Fact` saves unchanged. | ✅ Done |
| **Provenance layer — Phase B** | `save_decision` subcommand in `memory_bridge.py` (named flags — `--title`, `--decided-by`, `--project`, `--rationale` required; `--assisted-by`, `--alternatives`, `--confidence`, `--entities` optional) and `save_decision` MCP tool in `vector-skill.py`. `build_decision_metadata()` pure helper. `--version` flag added to `memory_bridge.py`. | ✅ Done |
| **Three-test fixes (v0.3.1)** | Retrieval visibility: search results carry `tier`, `score_normalized` (sigmoid), `matched_entities`, structured `graph_context` list. Consolidation history: `summary_history JSONB` column on `community_summaries` (migration 004) — prior summary appended before each `DO UPDATE`, capped at 20. Lineage: `source_ref` optional metadata key flows from coordinator to Neo4j `Fact.source_ref` property. 14 new tests added. `schema.md` "appends new rows" inaccuracy corrected. | ✅ Done |
| **REM/NREM + supersession (v0.4.0)** | `rem_loop.py` new daemon — LLM summary + typed entity relationships on oldest-first Fact nodes; single AUTOCOMMIT Postgres connection per cycle; `rem_processed=true` SET last (partial failure leaves fact retryable). NREM gated on `rem_processed`. CommunitySummary supersession: `superseded` column (migration 006) + `SUPERSEDES` Neo4j edges. 4 new ontology relationships (PRODUCES_INSIGHT, UNDER_CONDITIONS, CONSIDERED, REJECTED). Source normalisation backfill (migration 006). `AUDIT_LOG_PATH` opt-in JSON-lines outbox audit log. 17 new tests; 130 total. | ✅ Done |
| **Provenance layer — Phase C** | Retrospective layer — closes the Why-To loop. `POST /memory/retrospective` (`coordinator.py`) writes a dated `HAD_OUTCOME` edge (an edge property, not a node — lineage without node explosion); `save_retrospective` in `memory_bridge.py` + MCP tool in `vector-skill.py`; multiple retrospectives per decision allowed; retrospectives never create a `technical_docs` row, so they do not pollute semantic search. | ✅ Done (v0.3.3) |
| **Provenance layer — Phase D** | Four named query shortcuts in `memory_bridge.py query <template>`: `who-decided`, `agent-decisions`, `retrospectives`, `why-to-check`. Filter values sanitised before Cypher interpolation; raw `graph` subcommand preserved for custom traversals; SKILL.md Task 3 documents both paths. 7 new tests. | ✅ Done (v0.3.3) |
| **Agent authentication (Phase 2C)** | `AGENT_TOKENS` registry; `Authorization: Bearer <token>` DEFAULT DENY middleware; server-side `source` overwrite (identity cannot be spoofed); duplicate-token guard; trailing-slash normalisation; 22 new tests. | ✅ Done (v0.3.5, hardened through v0.3.6) |
| **GPU-aware dreaming (v0.4.1)** | REM/NREM yield when the GPU is busy — platform-agnostic `nvtop --snapshot` probe (no per-vendor parsing), fail-open, `SLOT_AWARE`/`GPU_BUSY_PERCENT`/`GPU_INDICES` tunables; NREM 45-min hard backstop preserved. `/health` probes the LLM via `/v1/models` (the route OpenAI-compatible servers actually serve). | ✅ Done |
| **JSONB integrity + thin-client split (v0.4.2)** | Fixed JSONB double-encoding — `metadata`/`cypher_params` were `json.dumps()`'d against an asyncpg codec that dumps again, storing string scalars so `metadata->>` returned NULL (migration 008 repairs them). Strict thin-client/operations split: the skill ships only `memory_bridge.py`; daemons + schema deploy on the gateway host. Client↔gateway `api_version` contract + `doctor`. MCP `save_artifact` routed through the gateway (outbox atomicity, `metadata.model` preserves the loaded model name). | ✅ Done |
| **Decision enrichment + telemetry (v0.4.3)** | REM now enriches `:Decision` nodes — activates the previously-orphaned reasoning layer (`CONSIDERED`/`REJECTED`/`PRODUCES_INSIGHT`/`UNDER_CONDITIONS`), marking decisions with a non-destructive `rem_summary`. Operational telemetry: `GET /memory/telemetry` + `memory_bridge.py status` (outbox + REM/NREM backlog rollup). Structured logging enabled by default in deployment. | ✅ Done |

### In Progress / Planned

| Phase | Milestone | Notes |
|---|---|---|
| **Insight consolidation (Phase 3a)** | NREM folds *clusters* of ≥2 decisions converging on a shared grounded `Fact` (non-mega-hub entity, `INSIGHT_THRESHOLD=2`) into elevated `community_summaries` tagged `metadata.kind="insight"` (`source_pg_ids`=decisions, aggregate retrospective `outcome_rating`); adds a `consolidated` flag on decisions and elevates insights in `handle_search`. Reuses the existing summary write + supersession. | **Design complete** (decision 245); next build. A *decision-cluster* trigger, not fact-density — a lone decision is never round-tripped. |
| **Provenance layer — Phase E** | Separate `pruning_loop.py` on a slow cron; enforces the information foraging heuristic (save if retrieval utility + decision impact > storage cost); `type:decision` and `decision_impact`-flagged rows are unconditionally shielded; plain facts compete on retrieval frequency × age | Queued. Decoupled from the consolidation daemon — different cadence. |
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
