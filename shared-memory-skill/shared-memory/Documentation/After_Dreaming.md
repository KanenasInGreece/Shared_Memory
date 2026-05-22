# After Dreaming: Closing the Gaps in the Consolidation Pipeline

*A continuation of: [Why Your AI Workstation Needs to Dream](dreaming-cycle-v6.md)*

---

The previous article described a consolidation daemon that wakes on an idle signal, evaluates graph density around Entity anchors, synthesizes narrative summaries through an LLM, and writes them back into a Tier 3 semantic layer. The architecture was correct in design. What the article did not say — because it was not yet true — is that it was not working.

This article is a record of what was wrong, how it was diagnosed, and what decisions were made to close each gap. It will be updated as the architecture continues to evolve.

---

### Three silent failures

A well-designed system can fail silently. The consolidation pipeline had three places where it failed without producing an error, without surfacing a symptom, and — most dangerously — without revealing that anything was wrong. The daemon ran. The gateway ran. Saves succeeded. But no consolidation ever occurred.

**Failure one: the signal was never sent.**

The daemon's trigger mechanism is a Postgres `LISTEN/NOTIFY` subscription. Every new artifact saved to `technical_docs` is meant to fire `pg_notify('new_artifact', {"pg_id": id})`, waking the daemon and adding the artifact's ID to the queue. The daemon was correctly written to listen for this. Neither `memory_bridge.py` (used by Claude Code and Gemini CLI) nor `vector-skill.py` (the MCP server used by LM Studio) ever sent it. The daemon's `pending_pg_ids` set stayed empty. The idle timer never started. Consolidation never fired — not once, across every session that had run since the daemon was written.

This is the hardest class of failure to catch: a contract between two components where one side implements the interface correctly and the other never calls it. No error is produced. The daemon simply waits indefinitely, in perfect health, for a signal that never comes.

The fix is one line, in the right place. Inside the same cursor context as the `INSERT`, before `conn.commit()`:

```python
cur.execute("SELECT pg_notify('new_artifact', %s)", (json.dumps({"pg_id": pg_id}),))
```

The placement matters. The NOTIFY fires inside the transaction and only delivers to listeners after the commit succeeds. There is no window where a committed fact goes unannounced, and no risk of announcing a fact that was subsequently rolled back.

**Failure two: the graph had no structure.**

Even if the daemon had been woken correctly, the consolidation query would have returned nothing. The query traverses:

```cypher
MATCH (f:Fact)-[:REPORTS_ON|MENTIONS]->(e:Entity)
```

But `save_artifact` in both entry points created only a bare `Fact` node. No `Entity` nodes. No `MENTIONS` relationships. The hub-and-spoke graph that consolidation depends on to form clusters did not exist. Every fact was structurally isolated — stored, embedded, retrievable by vector search, but invisible to the consolidation cycle that was supposed to synthesize it into Tier 3.

The decision here required some thought, because there are multiple ways to produce entity relationships at save time.

The most automatic option is NLP-based extraction: run a named entity recognizer over the content and derive entity names from the text. This is appealing because it requires nothing from the caller. It is wrong for this architecture for two reasons. First, it adds a latency-sensitive processing step to every save, on a path that is already waiting for an embedding call to complete. Second, and more importantly, it produces entities that the caller did not choose. The graph structure would reflect what an NLP model inferred rather than what the agent actually understood the artifact to be about. The article this series is based on is explicit: the failure modes of the graph layer are structural — sparse ontologies, poorly typed relationships — and those are problems that disciplined ingestion controls directly. Discipline belongs to the caller, not to an automated pass that the caller cannot see.

The second option is LLM-based extraction: call the reasoning model on each save to extract semantically meaningful entities. This is slower still, introduces a second failure mode (saves now abort or degrade if the LLM is also down), and does not obviously produce better structure than a caller who knows what they are saving.

The chosen approach is explicit caller-supplied entities. The caller passes `"entities": ["Name1", "Name2"]` in the metadata JSON. The saver creates `Entity` nodes and `MENTIONS` relationships for each name. The burden is on the caller to declare what a fact is about. This is a real burden — facts saved without entities are stored and retrievable, but they never reach Tier 3. That consequence is intentional: if you cannot say what a fact is about, it should not be synthesized into a shared narrative that every other agent reads first.

**Failure three: the daemon called the embedding service directly.**

The consolidation loop re-embeds every synthesized summary using BGE-M3. It was calling port 8070 directly — the llama.cpp embedding service — bypassing the Hive-Mind Gateway at port 8888. This is a hard mandate violation. All agents must route through 8888, because the gateway enforces 1024-dimensional consistency and unified access. An agent bypassing the gateway is not sharing the same embedding space in the operational sense, even if the underlying model is the same. The fix is a single URL change.

---

### What is now true

As of commit `2f15321` (2026-05-20):

- Every save in both `memory_bridge.py` and `vector-skill.py` fires `pg_notify('new_artifact', ...)` inside the Postgres transaction before commit. The consolidation daemon wakes automatically. No manual trigger is needed.
- Every save creates `Entity` nodes and `MENTIONS` edges in Neo4j for each name supplied in `metadata["entities"]`. The consolidation density query now finds real hub-and-spoke clusters.
- The consolidation daemon routes all embedding calls through the Hive-Mind Gateway at port 8888.

The complete save path, from invocation to consolidation eligibility, now looks like this:

```
caller supplies: content + metadata["entities"]
       ↓
get_embedding(content) via :8888 → abort if down
       ↓
INSERT INTO technical_docs ... ON CONFLICT (content_hash) DO UPDATE → get pg_id
pg_notify('new_artifact', {"pg_id": id})   ← atomic, inside transaction
conn.commit()
       ↓
MERGE (f:Fact {pg_id}) in Neo4j
for each entity name:
    MERGE (e:Entity {name})
    MERGE (f)-[:MENTIONS]->(e)
       ↓
daemon receives NOTIFY → adds pg_id to pending set → idle timer starts
after 15-minute idle (or 45-minute backstop):
    find Entity hubs with ≥ 5 unconsolidated Fact neighbors
    LLM synthesizes cumulative narrative
    re-embed via :8888
    write to community_summaries
    set Fact.consolidated = true
```

---

### The ingestion contract

The entity ingestion decision introduces a contract that every caller must understand.

A fact saved without `"entities"` is a fact the system cannot cluster. It lives in `technical_docs`, it is retrievable by vector search and relational expansion, but it will never appear in `community_summaries`. It will never be part of the thematic orientation layer that every retrieval query hits first. Over time, as the database fills with unconsolidated facts, those facts contribute to the interference problem the architecture was designed to solve — and they contribute nothing to the solution.

The obligation is not heavy. One to four entity names per save is sufficient. The names do not need to match a controlled vocabulary; they need to be stable enough that multiple facts about the same subject share the same name. `"BGE-M3"` and `"bge_m3"` will create two separate Entity nodes and two separate clusters. That is a disciplined ingestion problem, and it is one the caller controls entirely.

The schema reference at [`schema.md`](schema.md) now documents the `MENTIONS` relationship and the entity ingestion protocol. Both the Claude Code global skill (`~/.claude/skills/shared-memory.md`) and the packaged Gemini CLI skill (`shared-memory-skill/shared-memory/SKILL.md`) have been updated to surface this requirement at the point of use.

---

### What remains open

The open problems from the previous article are unchanged: community staleness (superseded summaries accumulate in `community_summaries` and are never retired), consolidation quality (no quantitative signal on synthesis accuracy), observability (no system-level retrieval trajectory measurement), and threshold calibration (density of 5 is hardcoded and empirically uncalibrated).

One new structural gap has become visible through this work. The consolidation daemon clusters facts by the Entity names the caller supplies at save time. If two callers use different names for the same concept — one saves with `"entities": ["hive_mind_proxy"]`, another with `"entities": ["Hive-Mind Gateway"]` — those facts land in different clusters and produce separate community summaries. Neither summary is wrong, but the graph fails to recognize that they are about the same thing. Entity resolution — merging synonymous nodes — is not implemented. It may not need to be implemented urgently, but it will become a real problem as the number of agents writing to shared memory grows.

---

---

### The gateway the articles did not show

The first article described a Hive-Mind Gateway at port 8888 as a routing layer. What neither article described is what the gateway actually was: a `ThreadingHTTPServer` built on Python's `http.server` stdlib, routing requests by substring match (`"/embeddings" in path`), buffering the entire upstream response into RAM before writing the first byte to the client, handling only POST, and with no structured logging. ADR-003 documents the upgrade from single-threaded to `ThreadingHTTPServer`. ADR-004 documents why that was not sufficient.

The problem that forced a full rewrite was streaming. An LLM generating 4,000 tokens at 20 tokens per second takes 200 seconds. The threaded gateway held all 4,000 tokens in a buffer until the model finished, then sent them as a single write. From the client's perspective, the response arrived 200 seconds after the request — with no intermediate output, no visible progress, and no way to begin processing partial results. This is not streaming; it is late delivery of a complete document.

A parallel development process across six proxy versions — written iteratively by Claude and Gemini, each identifying bugs and corrections in the other's implementation — produced the v6 async rewrite. The full decision log is in [`proxy_implementation.md`](proxy_implementation.md). The summary of what changed:

- **Streaming:** `aiohttp` + `iter_any()` pipes chunks to the client as they arrive from the upstream model. The first token reaches the client in milliseconds, not 200 seconds.
- **RFC 7230 compliance:** hop-by-hop headers (`Transfer-Encoding`, `Content-Length`, `Connection`, and others) are stripped from both request and response in both directions. Forwarding `Content-Length` alongside a chunked stream causes clients to truncate or hang — a bug that is invisible until it manifests as a stalled terminal.
- **`auto_decompress=False`:** aiohttp decompresses upstream responses by default but still forwards `Content-Encoding: gzip`. A client receiving decompressed bytes labelled as compressed double-decompresses — corruption. Disabling auto-decompress makes the proxy fully transparent.
- **Graceful shutdown:** SIGINT/SIGTERM closes the listen socket, drains in-flight requests, then closes the connection pool — in that order. A second Ctrl+C removes both signal handlers, restoring Python's default `SIGINT` disposition and providing an emergency hard-abort if a hung backend connection stalls the drain.
- **`CancelledError` never swallowed:** swallowing a `CancelledError` leaves the task in a zombie state from the event loop's perspective; graceful shutdown stalls waiting for a task that returned without acknowledging its cancellation.

The gateway is now a production-grade async reverse proxy. The eight bugs found in the initial async rewrite, the fixes contributed by Gemini's parallel implementations, and the four-case architectural audit are all documented in [`proxy_implementation.md`](proxy_implementation.md) with the reasoning behind every accepted and rejected change.

One structural change accompanied the proxy upgrade: the project directory was reorganised. The top level now holds only files required by external tools — `mcp.json`, `postgres_neo4j_limits.yaml`, `vector-skill.py`, `system-prompt.md`. All skill scripts and documentation live inside `shared-memory/`. This document is in `shared-memory/Documentation/`, alongside the schema reference, the ADR log, the design articles, and the proxy decision history.

---

*Next: the ingestion boundary and the simulation pass — building the loop a way to doubt its sources and itself.*

---

### Changelog

| Date | Commit | Change |
|---|---|---|
| 2026-05-21 | `377c9d1` | Proxy rewritten to async aiohttp v6 (streaming, RFC 7230 hop-by-hop filtering, graceful shutdown, `auto_decompress=False`); directory restructured — all skill files moved into `shared-memory/`; `proxy_implementation.md` added |
| 2026-05-20 | `2f15321` | Wired `pg_notify` in `memory_bridge.py` and `vector-skill.py`; entity ingestion via `metadata["entities"]`; `consolidation_loop.py` port 8070 → 8888; tests updated (12/12) |
| 2026-05-20 | `daa7c1c` | Updated `SHARED_MEMORY_ARTICLE.md` and `shared-memory-framework.html` to reflect NOTIFY wiring, entity protocol, and three-tier model terminology |
