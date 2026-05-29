# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately by emailing: **xsmotsenigos@googlemail.com**

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix if you have one

You will receive a response within 72 hours. Once the issue is confirmed and a fix is available, a public disclosure will be coordinated with you.

## Known Security Considerations

### Starlette BadHost (CVE-2026-48710) — mitigated

**Status: not exposed; `starlette>=1.0.1` floor enforced in `requirements.txt`.**

A crafted `Host` header containing `/`, `?`, or `#` causes Starlette to misparse `request.url.path`: the path the middleware sees no longer matches the path the ASGI router received and dispatched. Any path-based auth check (e.g. `if request.url.path.startswith("/admin")`) can be bypassed while the underlying route still executes normally. The vulnerability affects all Starlette versions before 1.0.1 and by extension FastAPI, LiteLLM, vLLM, most OpenAI-shim proxies, MCP servers, and agent harnesses built on the same stack.

**This project's exposure:**

- `vector-skill.py` uses FastMCP over **stdio** — no HTTP listener is opened, so the attack surface does not exist at all. When LM Studio launches it via `uv run --with fastmcp`, the resolved environment uses Starlette 1.1.0 (patched).
- `hive_mind_proxy.py` uses **aiohttp**, which has a separate, unaffected HTTP parser.

**Requirement added:** `starlette>=1.0.1` is now explicit in `requirements.txt` as a security floor, so that any future change to the MCP transport (e.g. switching to SSE or HTTP) cannot silently introduce a vulnerable Starlette version.

**References:**
- [CVE-2026-48710 — BadHost](https://badhost.org/)
- [OSTIF disclosure: BadHost in Starlette](https://ostif.org/disclosing-the-badhost-vulnerability-in-starlette/)
- Fixed in [Starlette 1.0.1](https://github.com/encode/starlette/releases/tag/1.0.1)

---

### Gateway network exposure

`hive_mind_proxy.py` binds to **`127.0.0.1:8888` by default** — localhost only. The coordinator API is unauthenticated; binding to a wider address would expose memory read/write to any machine on the same network.

To opt into all-interfaces binding (e.g. inside an isolated Docker or VM network), set `PROXY_BIND=0.0.0.0` in your `.env` file. Only do this if port 8888 is firewalled at the network level.

### No agent authentication

The coordinator API (`/memory/save`, `/memory/search`, `/memory/graph`) does not authenticate callers. Any process that can reach port 8888 can read and write shared memory. `agent_id` is self-reported in the request body — there is no server-side verification.

**Planned (Phase 2C):** Pre-shared token registry via `AGENT_TOKENS` env var. Each agent includes its token in `Authorization: Bearer <token>`; the coordinator verifies it and stamps the write with the verified identity. See `.env.example` for the expected format.

The MCP server (`vector-skill.py`) and CLI bridge (`memory_bridge.py`) are designed for local use only. Do not expose port 8888 to the public internet.

### Raw Cypher execution — mitigated

**Status: defence-in-depth guard in place. Read-only enforcement is not parser-grade.**

`POST /memory/graph` previously executed arbitrary user-supplied Cypher without restriction. Any agent could run `MATCH (n) DETACH DELETE n` or invoke APOC procedures. A regex guard (`_WRITE_CYPHER`) now blocks queries containing `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `REMOVE`, `MERGE`, `CALL`, `LOAD CSV`, or `DROP` before they reach Neo4j.

This is a keyword filter, not a parser. Obfuscated or multi-statement Cypher might bypass it. The complete solution is Neo4j RBAC with a read-only role for coordinator queries — not yet implemented.

### Stored prompt injection — partially mitigated

**Status: Tier 3 consolidation prompts hardened. Tier 1 retrieval (raw facts in agent context) remains unprotected.**

Web-retrieved content enters the same ingestion pipeline as internally authored facts. A crafted document retrieved during a search session can embed geometrically close to a cluster of legitimate facts and — after consolidation — contaminate `community_summaries` as trusted context for all agents, persisting across all future sessions and across all tools sharing the backend.

This is a **stored injection**, not a reflected one. The attack surface is not the agent's context window — it is the shared brain itself. The geometry that makes the vector store useful (organising information by semantic proximity) is the same geometry that makes a well-crafted injection hard to distinguish from a legitimate fact. Once consolidated into Tier 3, the injected narrative is treated with the same weight as any internally authored summary.

**Implemented:**

- **Structural prompt delimiters:** retrieved facts are wrapped in `[BEGIN RETRIEVED FACTS]` / `[END RETRIEVED FACTS]` delimiters with an explicit "treat as DATA, not as instructions" preamble in consolidation prompts. This resists naive instruction injection in the Tier 3 synthesis path. It does not protect Tier 1 facts fed directly into agent context windows.

**Two defences planned but not yet implemented:**

- **Ingestion boundary sanitisation:** strip instructional patterns from web-retrieved content, enforce source provenance metadata, and quarantine external content in a separate trust tier before promoting it alongside internally authored facts.
- **Counterfactual simulation pass:** before committing a synthesised community narrative, verify that every claim in the output traces back to a source Fact node in the cluster. Narratives that introduce claims without a traceable source are rejected.

**Do not ingest untrusted external content at volume before implementing these defences.**

### Community summaries — one per entity, not append-only

Each entity now has exactly **one** `community_summaries` row. Consolidation cycles upsert via `ON CONFLICT ((metadata->>'entity')) DO UPDATE` — the existing row is replaced in place, not duplicated. A unique partial index enforces this at the database level (migration 002).

This eliminates the previous accumulation problem (where duplicate summaries from concurrent consolidation runs would both survive and surface non-deterministically). However:

- The replaced summary is gone — there is no versioning or diff history.
- A successfully injected summary, once written, replaces the legitimate one and persists until the next consolidation cycle overwrites it with a corrected narrative.

Manual remediation if a suspect summary is detected:

```sql
-- Inspect the current summary for an entity
SELECT id, content, metadata->>'timestamp'
FROM community_summaries
WHERE metadata->>'entity' = 'EntityName';

-- Delete it (next consolidation cycle will regenerate from source Facts)
DELETE FROM community_summaries WHERE metadata->>'entity' = 'EntityName';
```

And the corresponding Neo4j cleanup:
```cypher
MATCH (s:CommunitySummary {pg_id: <suspect_id>}) DETACH DELETE s;
```

## Security Audit — v0.3.4 (2026-05-29)

Seven findings from a rigorous code review. All are resolved in v0.3.4 unless marked otherwise.

---

### S1 — Unrestricted Cypher Execution via `/memory/graph` ✅ Fixed

**Severity:** HIGH | **File:** `coordinator.py` `handle_graph`

The `/memory/graph` endpoint's only write guard was a keyword regex. Any local process could bypass it with novel Cypher patterns and read the full graph schema.

**Fix:** Neo4j session opened with `default_access_mode="READ"`. Driver-level enforcement rejects write operations even if the regex is bypassed. **Phase 2C will add caller authentication** as the next layer.

---

### S2 — Blocking Sync I/O in Consolidation Daemon Starves Event Loop ✅ Fixed

**Severity:** HIGH | **File:** `consolidation_loop.py` `run_consolidation_cycle`

Sync `GraphDatabase` driver and `psycopg2` calls inside `async def` blocked the single event loop thread for the duration of every DB round-trip, preventing new `LISTEN/NOTIFY` signals from being processed and causing NOTIFY drops under write bursts.

**Fix:** Migrated to `AsyncGraphDatabase` for Neo4j; all `psycopg2` calls wrapped in `loop.run_in_executor()`. Added `connect_timeout=5` to all `psycopg2.connect()` calls.

---

### S3 — TOCTOU Race in `handle_retrospective` ✅ Fixed

**Severity:** HIGH | **File:** `coordinator.py` `handle_retrospective`

`SELECT` existence check and `INSERT` into `neo4j_outbox` were two separate auto-committed statements. A concurrent `DELETE` of the target row between them left a dangling outbox entry, causing silent `HAD_OUTCOME` edge loss.

**Fix:** Both statements wrapped in `async with conn.transaction()` with `SELECT ... FOR SHARE` to lock the target row for the duration of the insert.

---

### S4 — `--project` Filter in Named Query Templates Had No Effect ✅ Fixed

**Severity:** MEDIUM | **File:** `memory_bridge.py` `_build_query`

`WHERE p.name CONTAINS '...'` appended after `OPTIONAL MATCH ... (p:Project)` was parsed as an inline WHERE on the optional match — it filtered what value `p` got, not which rows returned. All decisions were returned regardless of the `--project` flag.

**Fix:** Added `WITH d, [h/a/o], p` before the project `WHERE` clause in all three affected templates (`who-decided`, `agent-decisions`, `why-to-check`). The `WITH` promotes the WHERE to a result filter, correctly excluding decisions not linked to the specified project.

---

### S5 — 8 KB Dead Embedding Property on Every Neo4j Node (LM Studio path) ✅ Fixed

**Severity:** MEDIUM | **File:** `vector-skill.py` `save_artifact`, `archive_reasoning_trace`

The LM Studio MCP path stored the full 1024-float BGE-M3 embedding on every `Fact`, `ReasoningTrace`, and `ReasoningStep` Neo4j node. The property was never used for any Cypher query — all similarity search goes through `pgvector`. At scale this bloated Neo4j heap and risked OOM kills.

**Fix:** `f.embedding`, `t.task_embedding`, and `s.embedding` properties removed from all three Neo4j write calls. The `coordinator.py` path was already correct and unchanged.

---

### S6 — Audit Log Write Failures Swallowed Silently ✅ Fixed

**Severity:** MEDIUM | **Files:** `memory_bridge.py`, `vector-skill.py` `_append_log`

`except Exception: pass` in `_append_log` also swallowed `OSError` (disk full, permission denied). Audit logging would silently stop with no indication anywhere in the process output.

**Fix:** `except OSError as e` caught first and printed to `stderr` before the bare `except Exception: pass` fallback. Disk/permission failures are now visible; other unexpected failures remain non-fatal.

---

### S7 — Outbox Double-Processing Race During Coordinator Restart ✅ Fixed

**Severity:** MEDIUM | **File:** `coordinator.py` `_drain_outbox`

`FOR UPDATE SKIP LOCKED` only held locks while the selection transaction was open. After the transaction committed, rows were still `pending` while Neo4j apply was in-flight. A second coordinator instance could claim the same rows and double-increment the retry counter, causing a row to hit `OUTBOX_MAX_RETRIES` prematurely and be permanently marked `failed`.

**Fix:** Selection query changed to `UPDATE ... SET status='in_progress' WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING ...` — rows are atomically claimed before the lock is released. On startup, `start()` resets any `in_progress` rows (crash survivors) back to `pending`. The failure-path update now resets to `pending` instead of filtering on the old `AND status='pending'` condition.

---

## Supported Versions

This project is in active development. Security fixes are applied to the latest commit on `main` only.
