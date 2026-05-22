# Breaking the LLM Silo: Building a Shared Memory Framework for Agentic Orchestration

### **Introduction: The Context Paradox**
As developers, we are increasingly living in a multi-agent world. We use **LM Studio** for local brainstorming, **Gemini CLI** for surgical codebase edits, and **Claude Code** for complex refactoring. Yet, each of these tools suffers from a fundamental flaw: **they are technically isolated.** 

When you solve a complex architectural bottleneck in a chat window, that insight usually dies with the session. The next agent you spin up has no "memory" of that decision, leading to fragmented workflows and the "context tax"—constantly re-explaining your stack to your own tools.

At **Oratotis ai-labs**, we’ve solved this by moving from transient chat sessions to a **Permanent Technical Hive-Mind.** Here is how we built a Shared Memory Framework that bridges the gap between local inference and CLI agents.

---

### **The Architecture: Dual-Layer Cognition**
A robust memory model requires more than just a list of past messages. It requires the ability to handle both **meaning** and **structure**. Our framework utilizes a two-tier database approach:

#### **1. The Semantic Layer (pgvector + PostgreSQL)**
Using **PostgreSQL 17** with the **pgvector** extension, we store the "essence" of technical artifacts. 
- **Model:** BGE-M3 (1024-dimensional embeddings).
- **Function:** Handles conceptual recall. When an agent asks "How do we handle L/C trade finance logic?", the vector store retrieves semantically relevant snippets from past sessions, even if the exact keywords differ.

#### **2. The Relational Layer (Neo4j)**
Context isn't just a list of facts; it’s a web of relationships. 
- **Function:** Models dependencies. If an agent modifies a service, the graph knows exactly which containers, MCP tools, and documentation files are impacted. We move from "what happened" to "how it’s connected."

---

### **The Infrastructure: The "Mobile Command Center"**
To ensure privacy and high performance, the entire stack runs locally on a distributed homelab (Tailscale mesh):
- **Embedding/Reranking:** Hosted via `llama.cpp` (Ports 8070/8071) for low-latency local inference.
- **Hive-Mind Gateway:** A custom proxy (`hive_mind_proxy.py`) on Port 8888 that unifies fragmented model ports and enforces 1024-dim consistency across all agents.
- **Orchestration:** A custom **FastMCP** server (`vector-skill.py`) that allows any agent to "call" the memory via standard tool use.

---

### **Engineering for Stability: Concurrency & Performance**
As our agentic usage scaled, we encountered the "recursive deadlock" problem and potential race conditions. To solve this, we've hardened the infrastructure with:
- **Multithreaded Gateway:** The Hive-Mind Proxy (`http.server.ThreadingHTTPServer`) spawns a thread per request, enabling simultaneous streams for reasoning, embeddings, and reranking across all agents. A slow LLM call to port 5000 does not block fast embedding or reranking calls for other agents.
- **Idempotent Vector Saves:** We implemented **SHA-256 content hashing** in Postgres. Simultaneous saves of identical content now result in a single, unified artifact via `ON CONFLICT` upserts.
- **Engine-Level Write Locks:** By enforcing **UNIQUE constraints** in Neo4j, we leverage the database's native locking mechanisms to serialize concurrent graph merges safely.
- **Connection Pooling:** We utilize `ThreadedConnectionPool` to ensure thread safety across high-concurrency MCP server requests.
- **Tuned Timeouts:** Global timeouts are set to 20s to respect the high-compute nature of local BGE-M3 generation.
- **Gateway Port Mandate:** All embedding and reranking calls route through the Hive-Mind Gateway (port 8888). Never call ports 8070 or 8071 directly — this includes the consolidation daemon itself, which previously bypassed the gateway.

---

### **Achieved Goals: Cross-Agent Parity**
The primary success of this framework is the **elimination of context drift.** 

In the demonstration below, we see a real-world application:
1. An **LM Studio** session is used to finalize a Docker configuration.
2. The agent "absorbs" this decision into the Shared Memory.
3. Minutes later, the **Gemini CLI** is invoked. Without any manual input, it "remembers" the Docker changes because it automatically queries the shared vector store during its research phase.

---

> **[INSERT IMAGE 1: LM STUDIO CHAT SESSION]**
> *Caption: Finalizing the Unified Memory Stack architecture in LM Studio. Note the agent establishes the specific port mappings and resource limits.*

---

> **[INSERT IMAGE 2: GEMINI CLI RETRIEVING MEMORY]**
> *Caption: The Gemini CLI, operating in a completely separate session, retrieves the LM Studio artifacts via the `shared-memory` skill to inform its codebase edits. The "context tax" has been eliminated.*

---

### **The Functional Rationale: Why Share?**
Sharing artifacts between tools like Gemini and Claude isn't just about convenience; it’s about **consistency.** 
- **Consistency of Intent:** Your architectural philosophy is preserved across tools.
- **Operational Speed:** Agents skip the "researching from scratch" phase.
- **Technical Integrity:** Decisions made in one layer of the stack are visible to agents working on another.

### **Verification: The Rigor of a Production Stack**
A shared memory is only as good as its integrity. To ensure that our "Technical Hive-Mind" never degrades, we’ve implemented a **Rigorous Verification Suite**:
- **The Hard Mandate:** We’ve hardened our orchestration layer to abort "save" operations if the embedding models are offline. We refuse to save degraded, non-vectorized data.
- **Unit Testing at Scale:** A comprehensive suite of 12 unit tests validates every move, from "Sync-on-Save" triggers to "Relational Expansion" search logic, ensuring that a decision saved by one agent is always perfectly retrievable by another.

### **Architectural Decision: Event-Driven Consolidation (The Sleep Cycle)**
A critical challenge in shared memory is **Consolidation**: the process of distilling fragmented facts into cohesive high-level narratives. Initially, we considered immediate or polled consolidation, but settled on an **Event-Driven Sleep Cycle** architecture.

#### **The Logic: Postgres NOTIFY + Neo4j Density**
For a detailed technical breakdown of the trade-offs and alternatives considered, see our formal record in [ADR.md](ADR.md).

1. **Pulse:** Every new artifact saved to Postgres fires `pg_notify('new_artifact', {"pg_id": id})` inside the same transaction, before commit. The daemon receives this signal atomically — there is no window where a committed fact goes unnoticed.
2. **Entity Wiring:** The caller supplies `"entities": ["Name1", "Name2"]` in the metadata JSON. The saver creates `Entity` nodes in Neo4j and `MENTIONS` relationships from each `Fact`. This is the structural prerequisite for consolidation — the daemon clusters via Entity hubs, and facts saved without entities are stored and retrievable but never synthesized into Tier 3.
3. **Idle Timer:** The daemon listens for these pulses and resets a **15-minute idle timer**. A **45-minute hard backstop** fires during continuous ingestion regardless of activity, preventing indefinite deferral.
4. **Targeted Density:** When the timer expires, the daemon uses the queued `pg_id`s as surgical entry points into Neo4j. It evaluates the count of *unconsolidated* (`consolidated = false`) facts around connected Entity hubs. If a neighborhood exceeds the threshold (currently 5 facts), it triggers LLM-based distillation.
5. **Resilience:** If the LLM or vector services are offline, the facts remain unflagged in Neo4j and their IDs are re-queued, ensuring they are picked up in the next successful cycle.

#### **Why this approach?**
We rejected **polling** because it’s inefficient and lacks reactivity. We rejected **immediate consolidation** because it competes for GPU resources during active work. Our "Sleep Cycle" mimics human cognitive patterns, resolidifying knowledge only during periods of rest.

### **Beyond the Snapshot: Cumulative Consolidation**
A common failure mode in shared memory systems is **Staleness.** If an agent saves three facts about a database migration, a traditional summarizer creates three snapshots. As the project evolves, the vector store becomes cluttered with redundant, slightly-different summaries—a phenomenon we call **Semantic Accumulation.**

To solve this, we’ve moved from episodic snapshots to **Cumulative Narrative Synthesis:**
- **Content Integrity:** Every consolidation cycle now proactively fetches the *last known summary* for an entity. The LLM then "folds in" the new facts, producing a single, evolving narrative. 
- **Graph Traceability:** We’ve linked these summaries directly into the Neo4j graph. Each episodic `Fact` now carries a `SUMMARIZED_BY` relationship to its corresponding `CommunitySummary` node, allowing agents to traverse from a high-level overview down to the specific technical pulse that created it.

### **Current Limitations & The Road Ahead**
While we’ve eliminated **Content Drift**, we are still refining the **Structural Accumulation** problem. Currently, each update still inserts a new row in the vector store; "retiring" or pruning superseded summaries is our next major architectural milestone. 

Furthermore, we continue to iterate on:
- **Observability:** Developing a visual dashboard for the "Sleep Cycle" daemon.
- **Dynamic Density:** Moving from hardcoded thresholds to entropy-based triggers.
- **Sanitization:** Implementing a counterfactual pass to ensure consolidated summaries don't hallucinate "consensus" across conflicting facts.

### **Conclusion: Design with Intent. Build with Clarity.**
The future of software engineering isn't just "Better LLMs"; it's **Better Context Management.** By building a Shared Memory Framework, we’ve transformed our AI tools from isolated assistants into a cohesive, collaborative team that grows smarter with every line of code written.

#AI #AgenticAI #Neo4j #VectorDatabase #GeminiCLI #LMStudio #Oratotis #Industry5.0 #LLMOps
