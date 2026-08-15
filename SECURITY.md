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

**Setup (`bootstrap_tokens.sh` / `generate_tokens.py`, PR A2 — digest registry + write-through mint):**
1. Run `bash shared-memory/scripts/bootstrap_tokens.sh` (or `uv run python shared-memory/scripts/generate_tokens.py` directly) — it mints a fresh token for every agent, appends `AGENT_TOKENS=claude:sha256:<hex>,gemini:sha256:<hex>,...` (**digest form** — no token value is ever printed) to the gateway `.env`, and writes each LOCAL agent's own token straight into that agent's skill `.env` (mode 600) — nothing to copy by hand.
2. A REMOTE agent's token needs an explicit, human-run reveal on the SAME mint invocation: `--reveal <name>` (never through an agent — a later, separate run mints a fresh token for every agent, rotating the whole registry).
3. Restart the gateway; LM Studio requires a full application restart

**As of v0.9.3 a plaintext `name:token` entry in `AGENT_TOKENS` makes the gateway refuse to start** — convert an older registry in place with `generate_tokens.py --convert-digests`. **Backward compatible on the auth-off axis:** `AGENT_TOKENS` unset → auth disabled (no-op for existing installs).

**Read-only roles (`AGENT_ROLES`).** A registered token can be confined to read-only access with `AGENT_ROLES=name:read` in the gateway `.env`. A `read` token may reach only `GET /health`, `GET /memory/telemetry`, and `POST /memory/graph` (read-only-Cypher-guarded); `save`, `retrospective`, `search`, and the proxy passthrough return **403**. Roles only narrow access — the token must still be registered in `AGENT_TOKENS`; unset/`name:full` keeps full read/write. This lets read-only ops clients (e.g. the companion Shared Memory Monitor) hold a dedicated, non-write-capable identity rather than borrowing a full-access agent token — so a leaked monitor token cannot write to memory, and agent-token rotation cannot break telemetry.

**Token rotation** requires: edit gateway `.env`, restart gateway, update agent `.env` files (CLI agents take effect on next invocation; LM Studio requires full restart).

### Credential-use audit trail (PR A3, v0.9.4)

A separate, on-by-default log (`~/.shared-memory/logs/credential-audit.jsonl`, `CREDENTIAL_AUDIT_LOG_PATH` to relocate or disable — see `.env.example`) records credential-USE events distinct from the per-request `GATEWAY_AUDIT_LOG_PATH` above: an upstream LLM backend rejecting our own provider key, a gateway-side connect/timeout/proxy failure on a credentialed call, a client's bearer token failing to verify, and every daemon-token mint. Never the token/key value itself — only an 8-hex-char digest prefix of a presented (and rejected) token, where applicable.

**Hardened against an anonymous caller before release.** A same-day internal security review found the first cut let any unauthenticated client drive unbounded disk writes (a loop of `POST /memory/save` with no `Authorization` header). Fixed: a no-token request writes no line at all (only the `credentials.token_verify_failed` counter — the complete, unthrottled signal — moves); a presented-but-invalid token is rate-limited (a token bucket, default ~60 lines/minute, both knobs env-overridable) with one summary line recording how many were suppressed; and the event's own lines evict themselves under a full write queue rather than displacing genuine security events queued ahead of them.

**Qualification (F-8, matches the raw-facts-return-verbatim / bearer-tokens-until-PoP posture items below): this is a detective control against *external* credential misuse, not a tamper-evident one against a local same-uid actor.** The log is a plain, append-only JSONL file with 0600 permissions — no hash chain, no append-only filesystem attribute, no off-host shipping. A process running as the same OS user as the gateway (already inside this framework's trust boundary — see the *localhost-trusted-deployment* stance elsewhere in this document) can rewrite or truncate it at will. Treat it as evidence of what a remote/unprivileged caller attempted, not as a record that survives a compromise of the gateway's own account.

### Credential custody — secrets out of the environment, file-based delivery (v0.9.2 → v0.9.5)

The gateway is the framework's credential custodian twice over: it verifies client bearer
tokens at its own door, and it holds provider API keys that it attaches to upstream LLM
calls on the daemons' behalf. Four releases in August 2026 rebuilt how those secrets are
stored, delivered, and observed, working from a threat model stated plainly: the TCP
surface must be treated as the internet, and on a single-user machine **other processes
running as the same OS user cannot be excluded by any storage scheme** — so the goal of
this work is exfiltration resistance, least exposure, and detection, and this document
says so rather than implying a boundary that does not exist.

**v0.9.2 — secrets never touch the process environment.** Historically every secret in
`shared-memory/.env` was loaded into `os.environ`, which meant it appeared in
`/proc/<pid>/environ` and was inherited by every child process any daemon ever spawned. A
split loader (`secure_env.py`) now sends config keys to the environment and secret keys —
classified by an explicit name list *plus* a suffix pattern (`*_PASSWORD`, `*_TOKEN`,
`*_API_KEY`, …), so a newly added secret is caught even if nobody extends the list — to an
in-process store read through one accessor. Daemons are spawned with a filtered
environment. This is a standing invariant with a mutation-checked test: *no framework
process ever exports a secret into `os.environ` or a child environment.*

**v0.9.3 — the token registry stops holding tokens.** `AGENT_TOKENS` now stores SHA-256
digests only; verification is timing-safe; a plaintext entry refuses gateway startup
outright (convert with `generate_tokens.py --convert-digests`). Unsalted SHA-256 is the
NIST SP 800-63B-prescribed treatment for look-up secrets at or above 112 bits of entropy —
these tokens carry 192. Minting is write-through: token values land directly in each local
agent's own 600-mode skill `.env` and are never printed to a transcript; a remote agent's
value requires an explicit human-run `--reveal` on the same invocation. The daemons' own
tokens became per-boot ephemeral, delivered over an inherited pipe file descriptor —
present in no file and no environment at all.

**v0.9.4 — the audit trail** (previous section) made credential *use* observable: every
provider-key rejection, gateway-side fault on a credentialed call, failed client token,
and daemon-token mint leaves a log line that never contains token material.

**v0.9.5 — file-based delivery and the operational surface.** Every secret-classified key
can now be delivered without ever writing its value into `shared-memory/.env`:

- **systemd `LoadCredential=`** — the gateway reads `$CREDENTIALS_DIRECTORY/<key,
  lowercased>`; the shipped unit carries a working commented example for user units.
- **`<KEY>_FILE=/path/to/secret`** — the Docker official-images convention, compatible
  with mounted container secrets, `pass`-style stores, and vault-agent templates.

Setting a `_FILE` pointer or placing a file in the credentials directory is itself what
makes a key a candidate — there is no fixed list to extend. Both paths feed the in-process
store directly, extending the v0.9.2 invariant. The file reads are deliberately paranoid:
the file descriptor is opened non-blocking and `fstat`-checked so a FIFO or device node is
refused instead of hanging startup, size is capped before a byte is read
(`SECURE_ENV_SECRET_FILE_MAX_BYTES`), and a candidate key name must parse as an ordinary
identifier before it becomes part of a filesystem path — closing a traversal class
reachable through attacker-influenced backend configuration. Symlinks are followed on
purpose: Kubernetes delivers mounted secrets through a `..data` symlink chain, and
refusing them would break the convention this path exists to serve. A startup advisory
(key name only, never the value) fires when a known-secret key is found already sitting in
the process's inherited environment — the `EnvironmentFile=` / exported-shell-var pattern,
which this document now names as an anti-pattern for secrets since it re-opens the exact
`/proc/<pid>/environ` exposure v0.9.2 closed.

The same release cleaned the operational surface: `ops/backup.sh` and `restore.sh` no
longer place any credential on host-visible process argv (`docker exec --env-file` and
`curl -H @file`, staged in a single 0700 temp directory that is created once per run and
removed by the exit trap); the installers create and rewrite secret-bearing files at mode
600 from the first byte, with no window at the default umask, and fail closed if they
cannot; thirteen standalone maintenance scripts that each hand-rolled an `.env` loader —
dumping every key, tokens included, into their environment — now delegate to the split
loader; `systemctl --user import-environment` for provider keys is deprecated in the docs
(readable by any same-uid process via `show-environment`, inherited by every later user
unit); and the shipped units gained `UMask=0077` and `NoNewPrivileges=yes` (measured
effect: `systemd-analyze security` exposure 9.4 → 9.2 — an honest small step; the
remaining score is capability-bounding and syscall-filter work that needs case-by-case
evaluation against a networked Python service).

**What each tier honestly buys.** The default plain-`.env` install (600-mode file) is
*hygiene plus detection*: the key exists in exactly two places — the file and the
gateway's process memory — and every use is audit-logged, but any same-uid process can
still read the file. The file-based tiers remove the key from the `.env` entirely; under a
**system** unit with a root-owned credential store, same-uid processes cannot read the key
at rest at all. Under a `systemd --user` unit the credential remains readable by the
owning account — `man systemd.exec` is explicit that credentials are accessible to the
user associated with the unit — so for a user-unit deployment these tiers reduce exposure
and accident surface but are **not** isolation from the account itself. Deployments using
paid provider keys should prefer the hardened shape.

**Verified live, not only in the suite.** Each of these releases was verified on the
running system after deploy: `/proc/<pid>/environ` scans of every framework process (zero
secret-classified keys); a real backup run under continuous `ps` argv sampling (zero
occurrences of any secret value on host argv for the entire run, exactly one secrets
temp directory created and none surviving exit); the documented `LoadCredential=` form
proven to start — and the wrong form proven to fail — in disposable transient units; and
hostile-input probes (FIFOs, oversized files, traversal-shaped names) against the deployed
loader.

**Named open items, tracked deliberately rather than implied closed:**

- The ops shell scripts (`backup.sh`, `restore.sh`) still source the `.env` into their own
  shell environment, which their child processes inherit — the argv fix does not close
  this, and the environment-invariant does not yet hold for the shell surface.
- The secret-file size cap is read at import time, so it cannot itself be set from the
  `.env` file.
- Chokepoint governance is the planned next step: a method-and-path allowlist on the
  credentialed proxy branch (today the catch-all route forwards any path with the provider
  key attached), a startup refusal when auth is disabled while provider keys are
  configured, and a slimmer anonymous `/health`.
- Transport security (TLS, proof-of-possession) remains a separate, later workstream —
  see the section below.

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
- **Insight synthesis path (v0.4.5):** the new cross-project insight fold (`generate_insight`) folds decision content **and agent-supplied retrospective notes** into elevated `kind='insight'` Tier-3 summaries. It applies the same delimiter + "treat as DATA, not instructions" preamble as the thematic path. Note this widens the Tier-3 attack surface: a crafted retrospective `notes` field becomes synthesis input. The same mitigations and limits as thematic consolidation apply — review retrospectives recorded from untrusted automation before they accumulate. Insight rows are never invalidated in place; a corrected re-fold supersedes a suspect one (and a reversed source decision drops it from the fresh-cluster gate).

**Two defences planned but not yet implemented:**

- **Ingestion boundary sanitisation:** strip instructional patterns from web-retrieved content, enforce source provenance metadata, and quarantine external content in a separate trust tier before promoting it alongside internally authored facts.
- **Counterfactual simulation pass:** before committing a synthesised community narrative, verify that every claim in the output traces back to a source Fact node in the cluster. Narratives that introduce claims without a traceable source are rejected.

**Do not ingest untrusted external content at volume before implementing these defences.**

### Community summaries — one per entity, not append-only

Each entity now has exactly **one** `community_summaries` row. Consolidation cycles upsert via `ON CONFLICT ((metadata->>'entity')) DO UPDATE` — the existing row is replaced in place, not duplicated. A unique partial index enforces this at the database level (migration 002).

This eliminates the previous accumulation problem (where duplicate summaries from concurrent consolidation runs would both survive and surface non-deterministically). **Exception (v0.4.5):** `kind='insight'` rows are exempt from this unique key — the index is now partial (`WHERE COALESCE(metadata->>'kind','thematic') <> 'insight'`, migration 009) — and are written always-INSERT. Insight dedup is handled by supersession (a re-fold on the same source decisions supersedes the prior insight), not by in-place upsert; this deliberately avoids resurrecting a superseded row. However:

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

**Audit cadence:** Security reviews run at every **x.y.5 release** and on demand via `/security-review`. The review covers four vectors: Concurrency & State, Database & Persistence Integrity, Dependency & Supply Chain, and Edge-Case Resilience. The v0.4.0 and v0.4.5 audits are complete (see below); next scheduled: v0.5.0 or on demand.

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

**Fix:** Added a plain-Python fallback in the `except ImportError` block that manually parses the skill-root `.env` (one directory above `scripts/`, covering `~/.grok/skills/shared-memory/.env` etc.) and the `scripts/`-adjacent `.env`. Auth tokens load with no external dependencies.

> **Later change:** the original `~/.config/shared-memory/client.env` shared-fallback tier (present in the v0.3.5 fix) was **removed**. A single shared token file could attach the wrong agent identity to a save — defeating the server-side source verification — so each agent now reads only its own skill `.env` and the `scripts/`-adjacent `.env`.

---

## Security Audit — v0.3.5 post-release (2026-05-29)

Run after the post-release fixes above were applied. Zero confirmed findings above the 8/10 confidence threshold.

| Candidate | Verdict |
|---|---|
| Cypher injection via `_safe()` regex in `memory_bridge.py` | **False positive** — allowed characters (alphanumerics, space, `_`, `.`, `-`) cannot break out of a single-quoted Cypher string. Single quotes and backslashes are both stripped. CLI tool, not a network endpoint. |
| Write-Cypher guard bypass via tabs/comments | **False positive** — `\s` already matches tabs and newlines. The Neo4j session `default_access_mode="READ"` is a second independent layer at the driver level; both layers would have to fail simultaneously. |
| Cross-DB atomicity in `consolidation_loop.py` | **Not a security issue** — data consistency concern with no exploitation path (no new access, no code execution, no privilege escalation). Documented accepted trade-off in ADR-001. |

---

---

## Security Audit — v0.4.0 (2026-06-04)

Run during the REM/NREM release. Covered: `rem_loop.py` (new), modified `coordinator.py`,
`consolidation_loop.py`, `hive_mind_proxy.py`, `ontology.py`, migration 006.

### Confirmed findings (all fixed before release)

**A2 — Coordinator search crashes with 500 if migration 006 not applied**

`coordinator.py` `handle_search` added `WHERE NOT superseded` to the Tier 3 query.
If an operator restarted the gateway after a `git pull` without running `apply.py`, the
column was absent and every vector-backed search returned HTTP 500.

**Fix:** Wrapped in try/except with a fallback to the unsupervised query and a warning log.

**A3 — Structurally incompatible labels in REM's `_KNOWN_LABELS`**

`_KNOWN_LABELS` included `CommunitySummary`, `ReasoningTrace`, `ReasoningStep`.
The REM writer uses this set to build `MERGE (e:{label} {name: n})` patterns. Those
three node types are keyed by `pg_id`, not `name` — a MERGE on `name` would create
a phantom node structurally incompatible with the rest of the graph.

The immediate risk was low because `_fetch_closed_entity_set` does not query those labels,
so the registry would not normally contain them. Removed as a defence-in-depth fix.

**Fix:** Removed `ONT.community_summary`, `ONT.reasoning_trace`, `ONT.reasoning_step`
from `_KNOWN_LABELS`. The set now contains only labels whose identity key is `name`.

### Candidates reviewed and excluded

| Candidate | Verdict |
|---|---|
| Cypher injection via `label`/`rel_type` f-string interpolation in `rem_loop.py` | **Refuted** — labels and rel types pass through `ontology.py` `_validate()` regex at module load; only `^[A-Za-z_][A-Za-z0-9_]*$` identifiers reach any Cypher. |
| Prompt injection via entity node names in LLM prompt | **Excluded by policy** — "Including user-controlled content in AI system prompts is not a vulnerability" (security review guidelines). |
| `_daemon_env` full env passthrough exposes new secrets | **Not a new regression** — consolidation daemon already inherited the full env before v0.4.0. |
| SQL injection in `_check_supersession` `UPDATE WHERE id = %s` | **Refuted** — `old_id` is a database-sourced integer, not user input; parameterised `%s` binding. |
| `AUDIT_LOG_PATH` path traversal | **Below threshold** — requires write access to the `.env` file, implying full local system access. |

**Audit cadence note:** Next scheduled review at v0.4.5 or on demand.

---

## Security Audit — v0.4.5 (2026-06-11)

Run during the Phase 3a insight-consolidation release. Covered: `consolidation_loop.py` (new insight path — gate, fold, ledger, supersession), modified `coordinator.py` (`handle_search` insight elevation, `handle_retrospective` reversal hook, `PROJECT_ALIASES` ingress normalisation), `rem_loop.py`, `vector-skill.py`, `normalize_projects.py` (new), and migration 009.

### Result: zero findings above the 8/10 confidence threshold.

All new database access is parameterised (psycopg2 `%s` / asyncpg `$n`); the `_FACT_ROW`/`_DREAM_ROW`/`_RETRO_ROW` SQL fragments are static literals with no user input. All new Cypher interpolates only `ONT.*` label/relationship names (already validated by `ontology.py::_validate`) and binds every value (`$entity`, `$decision_ids`, `$old/$new`, …). No new routes or auth surface — the `rating="reversed"` reversal path runs inside the already-authenticated `POST /memory/retrospective` (unreachable by read-only roles) and marks a decision superseded within the write capability an authenticated agent already holds. No deserialization (`json` only), no secret logging, no user-controlled file paths (`normalize_projects.py::_load_env` uses a fixed repo-root path).

### Candidates reviewed and excluded

| Candidate | Verdict |
|---|---|
| Cypher injection via `entity` name in `_mark_insight_in_graph` / `_fetch_outcome_edges` | **Refuted** — `entity` reaches Neo4j only as a bound `$entity` parameter; labels/rels are `ONT.*` validated identifiers. |
| SQL injection via `PROJECT_ALIASES` / `--map` values in `normalize_projects.py` | **Refuted** — values bind as `%s`; env/CLI inputs are trusted per the framework threat model regardless. |
| Privilege escalation via `rating="reversed"` suppressing arbitrary decisions | **Refuted** — within an authenticated agent's existing memory-write capability; read-only roles cannot reach the endpoint; trusted-agent model. |
| Stored prompt injection via retrospective `notes` into insight synthesis | **Documented, not a new code vuln** — same Tier-3 surface and delimiters as thematic consolidation; per policy, content-in-LLM-prompt is not itself a vulnerability. |

---

## Security & Quality Reviews — v0.4.12 → v0.9.5 (2026-06 → 2026-08)

From v0.4.12 onward, security work moved from standalone audits to a standing practice: reviews
run at every `x.y.5` release and on demand, and since v0.8.73 they run as one role inside a
**multi-role review framework** — six independent reviewer roles (Architecture & Release, Code
Quality, Security, Test & Verification, Ops & Release Integrity, Adversarial), each producing
findings with explicit severity. Reviewers find; the operator rules — a finding becomes a change
only by an operator decision, and shipped fixes are documented in the
[CHANGELOG](CHANGELOG.md) release that carries them. Per-version record:

- **v0.4.12 (2026-06-12) — observability before stronger auth.** Opt-in per-request gateway
  audit log (`GATEWAY_AUDIT_LOG_PATH`): every authenticated search/graph/status access recorded
  with agent identity and timestamp, off the event-loop hot path. Concurrent-load hardening
  (bounded connection pools, acquire-timeout load shedding, keyed locks) and a pluggable
  auth/audit seam (`auth_scheme` surfaced on `/health`). Deliberate sequence: harden → thin
  audit → proof-of-possession (upcoming) → full audit — so auditability landed ahead of, not
  gated on, the larger auth work.
- **v0.8.50 (2026-08) — trust-boundary pass.** Four boundaries evaluated: HTTP API
  authentication, header-stripping proxy guards, parameterised SQL/Cypher with the read-only
  graph guard, and the REM/NREM dreaming pipeline's handling of stored content.
- **v0.8.60 (2026-08) — ingress boundary.** SQL/Cypher injection safety re-verified; the
  human-asserted entity ingress boundary (gates E1–E5: agent-proposed entity and judgement
  labels are dropped, never written) reviewed together with the live graph hygiene sweep.
- **v0.8.74 audit → fixed in v0.8.75 (2026-08-11) — full six-role pre-milestone audit.**
  Security verdict: four boundaries clean. Ruled **Required** and shipped: **SEC-03** — every
  400-path error message that echoes caller-supplied input is now bounded (200-char cap via a
  single helper), closing a response-amplification vector; mutation-checked against 5000-char
  probes. The Adversarial role's **AR-01 (Critical)**: two failure-mode counters written to the
  consolidation ledger were never rolled up into `/memory/telemetry`, so a first live failure
  would have been invisible — both now served, verified live. Queued, tracked openly: **SEC-04**
  (neutralising protocol-shaped markers in REM prompt input, the same class the insight builder
  already neutralises).
- **v0.9.2 → v0.9.5 (2026-08-14/15) — the credential-custody series.** The plan itself was
  security-reviewed twice before any code (a plan review and an independent current-code
  review, eighteen findings ruled on by the operator), and every release then passed a
  dedicated pre-merge security review of the actual diff, with findings probe-confirmed
  against running code rather than argued from reading. That process earned its keep each
  time. At v0.9.2 it caught an inverted precedence in the new secrets accessor that made 36
  tests fail on any checkout with a real `.env` — invisible in the clean build environment.
  At v0.9.3 it caught a critical where minting tokens silently flipped an auth-disabled
  install to auth-enabled, locking out every client. At v0.9.4 it confirmed the audit log
  could be driven by an anonymous writer and the fix landed before release. At v0.9.5 review
  found — and live probes confirmed — that the first cut of the argv fix had moved passwords
  off a *container* process's argv while leaving them on the *host* docker client's argv,
  and that the auth-header temp directory was created inside command substitutions, so its
  cleanup trap removed nothing and token-bearing files outlived the script. Both were fixed
  and re-probed before merge, and the post-deploy verification exercised the whole surface
  end-to-end on the live system, including a real backup under continuous argv sampling.
  The recurring lesson written into this series: the *mechanism* being right is not enough —
  every claim a comment or document makes about the mechanism gets checked against what the
  platform actually does, because a confident wrong statement is what stops the next reader
  from looking.

The standing items from these reviews that are posture, not defects — raw facts return verbatim
from search, ingestion-boundary sanitisation is planned, bearer tokens until proof-of-possession
lands — are kept current in the *Known Security Considerations* section above and in README
§25 (*Honest state*).

---

## Supported Versions

This project is in active development. Security fixes are applied to the latest commit on `main` only.
