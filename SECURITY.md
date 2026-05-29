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

`hive_mind_proxy.py` binds to **`127.0.0.1:8888` by default** — localhost only.

To opt into all-interfaces binding (e.g. inside a Docker or VM network), set `PROXY_BIND=0.0.0.0` in your `.env` file. **Only safe over an encrypted overlay network (Tailscale, WireGuard) or behind TLS termination.** Bearer tokens are transmitted in plaintext over HTTP and are interceptable on an unencrypted network.

### Agent authentication — implemented (v0.3.5)

`Authorization: Bearer <token>` middleware is now enforced on all coordinator routes. Unregistered callers receive HTTP 401. The verified agent identity is stamped server-side onto every saved artifact — `agent_id` from the request body is no longer trusted.

**Setup:**
1. Run `uv run python shared-memory/scripts/generate_tokens.py` to generate tokens
2. Add `AGENT_TOKENS=claude:tok_...,gemini:tok_...,...` to the gateway `.env`
3. Add `AGENT_TOKEN=<your-token>` to each agent's skill `.env` (or `~/.config/shared-memory/client.env`)
4. Restart the gateway; LM Studio requires a full application restart

**Backward compatible:** `AGENT_TOKENS` unset → auth disabled (no-op for existing installs).

**Token rotation** requires: edit gateway `.env`, restart gateway, update agent `.env` files (CLI agents take effect on next invocation; LM Studio requires full restart).

### Network transport — tokens require an encrypted channel

Bearer tokens are sent in plaintext over HTTP. `PROXY_BIND=0.0.0.0` is only safe when the network between gateway and agents is encrypted end-to-end:

- **Safe:** Tailscale overlay, WireGuard tunnel, or TLS termination at a reverse proxy
- **Unsafe:** Raw LAN, unencrypted Docker bridge network exposed to other hosts

Never expose port 8888 to an untrusted network, even with authentication enabled.

### Raw Cypher execution — mitigated

**Status: two-layer defence in place (v0.3.4). Caller authentication implemented in v0.3.5.**

`POST /memory/graph` now has two independent controls:

1. **Keyword regex guard** (`_WRITE_CYPHER`): rejects queries containing `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `REMOVE`, `MERGE`, `CALL`, `LOAD CSV`, or `DROP` before they reach Neo4j — fast-fail, no round-trip.
2. **Driver-level read-only session** (`default_access_mode="READ"`): the Neo4j session is opened in read-only mode. Even if the regex is bypassed by a novel query pattern, the Neo4j driver rejects any write at the protocol level.

The remaining gap is caller identity: any process that can reach port 8888 can issue read queries. **v0.3.5** adds `Authorization: Bearer <token>` authentication so only registered agents can reach the endpoint at all.

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

- The previous summary is preserved in `summary_history JSONB` (migration 004, v0.3.1) — an append-only array capped at 20 entries, written before each overwrite. Full drift history is auditable, but rollback requires manual `DELETE` + re-consolidation.
- A successfully injected summary, once written, replaces the legitimate one and persists until the next consolidation cycle overwrites it with a corrected narrative. The `summary_history` column records the injected version but does not auto-remediate it.

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

**Audit cadence:** Security reviews run at every **x.y.5 release** and on demand via `/security-review`. The review covers four vectors: Concurrency & State, Database & Persistence Integrity, Dependency & Supply Chain, and Edge-Case Resilience. Next scheduled: v0.4.0 (or v0.3.10 if reached first).

---

### S1 — Unrestricted Cypher Execution via `/memory/graph` ✅ Fixed

**Severity:** HIGH | **File:** `coordinator.py` `handle_graph`

The `/memory/graph` endpoint's only write guard was a keyword regex. Any local process could bypass it with novel Cypher patterns and read the full graph schema.

**Fix:** Neo4j session opened with `default_access_mode="READ"`. Driver-level enforcement rejects write operations even if the regex is bypassed. **v0.3.5 added `Authorization: Bearer <token>` caller authentication** as the third layer — only registered agents can reach the endpoint at all.

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

## Post-release Fixes — v0.3.5 (2026-05-29)

Four security-relevant bugs discovered on first live deployment of the v0.3.5 auth system. All fixed same day.

---

### P1 — Auth middleware blocked coordinator's own embedding calls ✅ Fixed

**Severity:** HIGH (breaks all saves) | **File:** `coordinator.py`

`EMBED_URL` pointed to `http://localhost:8888/v1/embeddings` — the proxy the coordinator runs inside. The DEFAULT DENY auth middleware rejected these internal calls with 401 because the coordinator has no `AGENT_TOKEN`. Every `POST /memory/save` failed at the embedding step.

**Fix:** `EMBED_URL` changed to `http://localhost:8070/v1/embeddings` (direct to BGE-M3), consistent with `RERANK_URL` which already used `:8071` direct for the same reason. External agents still route embeddings through `:8888` and must authenticate; the coordinator is trusted and bypasses auth by calling the backend directly.

---

### P2 — `_auth_headers()` missing from three call sites in `vector-skill.py` ✅ Fixed

**Severity:** HIGH (LM Studio search/health broken) | **File:** `vector-skill.py`

`_auth_headers()` was added to `save_decision()` and `save_retrospective()` coordinator calls but missed three HTTP call sites that go through port 8888: the reranker call in `hybrid_search_and_rerank()` and both health-check probes in `check_memory_health()`. Every LM Studio search returned `401` and the health check reported the retriever and reranker as `FAIL`.

**Fix:** All six `client.post()` call sites in `vector-skill.py` now pass `headers=_auth_headers()`.

---

### P3 — Dotenv `or`-chain loaded only the first file found ✅ Fixed

**Severity:** MEDIUM (auth token silently not loaded) | **File:** `memory_bridge.py`

The three-tier dotenv fallback used Python `or` chaining: `find_dotenv() or path2 or path3`. Once `find_dotenv()` returned the gateway `.env` (which has `AGENT_TOKENS` but not `AGENT_TOKEN`), the chain short-circuited and the agent's own token file was never read. `_auth_headers()` returned `{}`, every request got 401, and there was no warning — the failure was silent.

**Fix:** Changed to a `for` loop with `load_dotenv(..., override=False)` so all three sources are loaded and the first definition of each variable wins.

---

### P4 — Auth token not loaded when `python-dotenv` absent ✅ Fixed

**Severity:** MEDIUM (auth bypass via dependency gap) | **File:** `memory_bridge.py`

Agents running `uv run --with httpx python scripts/memory_bridge.py` without `--with python-dotenv` hit `except ImportError: pass` silently — no `.env` was parsed, `AGENT_TOKEN` stayed unset, and every call got 401. Grok's standard invocation pattern triggered this: it ran without `python-dotenv` and fell back to reading local filesystem files to answer queries, bypassing shared memory entirely.

**Fix:** Added a plain-Python fallback in the `except ImportError` block that manually parses the skill-root `.env` (one directory above `scripts/`, covering `~/.grok/skills/shared-memory/.env` etc.) and `~/.config/shared-memory/client.env`. Auth tokens load with no external dependencies.

---

## Security Audit — v0.3.5 post-release (2026-05-29)

Run after the post-release fixes above were applied. Zero confirmed findings above the 8/10 confidence threshold.

| Candidate | Verdict |
|---|---|
| Cypher injection via `_safe()` regex in `memory_bridge.py` | **False positive** — allowed characters (alphanumerics, space, `_`, `.`, `-`) cannot break out of a single-quoted Cypher string. Single quotes and backslashes are both stripped. CLI tool, not a network endpoint. |
| Write-Cypher guard bypass via tabs/comments | **False positive** — `\s` already matches tabs and newlines. The Neo4j session `default_access_mode="READ"` is a second independent layer at the driver level; both layers would have to fail simultaneously. |
| Cross-DB atomicity in `consolidation_loop.py` | **Not a security issue** — data consistency concern with no exploitation path (no new access, no code execution, no privilege escalation). Documented accepted trade-off in ADR-001. |

---

## Supported Versions

This project is in active development. Security fixes are applied to the latest commit on `main` only.
