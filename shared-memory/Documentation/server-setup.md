# Server Setup & Operations

This is the runbook for the **operations surface** of the Shared Memory Framework —
the gateway and its daemons. It is written for whoever (human or agent) stands up
and maintains the **one** gateway host per hive.

If you only want an agent to *use* memory, you do not need this document — install
the thin-client skill and point it at a running gateway. See [`../SKILL.md`](../SKILL.md).

---

## Two surfaces — know which one you are touching

| | **Usage** (skill / client) | **Operations** (this document) |
|---|---|---|
| What it is | `memory_bridge.py` + `SKILL.md` | gateway, daemons, `migrations/` |
| Runs on | every agent, every host (incl. remote) | the **one** gateway host |
| Talks to DB/GPU? | No — HTTP to `:8888` only | Yes — owns Postgres, Neo4j, GPU |
| Shipped by | `sync_skills.sh` | this repo, via `git` |
| Upgraded by | re-sync the skill | `git pull` → `migrations/apply.py` → restart gateway |

**Installing the skill is not installing the framework.** The skill is a thin
HTTP client; the daemons never run from a skill directory. A remote agent has no
database and cannot run or upgrade them.

---

## Prerequisites (gateway host only)

- Postgres with `pgvector`, Neo4j, and the BGE‑M3 embedder (`:8070`) + reranker (`:8071`).
  `docker compose -f postgres_neo4j_limits.yaml up -d` starts the database and
  inference layer.
- An LLM endpoint on `:5000` (LM Studio or equivalent) for consolidation.
- `nvtop` for GPU‑aware dreaming (optional — the daemons fall back to the time
  guard without it).
- `uv` for dependency-pinned runs.

Remote agent hosts need **none** of the above — only `python` + `httpx` and a token.

---

## First-time install

```bash
# 1. Clone the framework repo onto the gateway host.
git clone <repo-url> shared-memory-GitHub
cd shared-memory-GitHub

# 2. Configure credentials.
cp .env.example .env
#    Fill in: NEO4J_PASSWORD, PG_PASSWORD, TAVILY_API_KEY
#    Optional: MEMORY_LOG_LEVEL, AUDIT_LOG_PATH, PROXY_BIND, WRITE_QUIESCE_SEC

# 3. Start the database + inference layer.
docker compose -f postgres_neo4j_limits.yaml up -d

# 4. Apply all schema migrations (idempotent — safe to re-run).
uv run --with psycopg2-binary python shared-memory/migrations/apply.py

# 5. Mint agent tokens (one-time auth setup).
uv run python shared-memory/scripts/generate_tokens.py
#    → add the AGENT_TOKENS=... line to this host's .env
#    → give each agent its own AGENT_TOKEN in its skill .env (never share tokens)

# 6. Start the gateway (also spawns the REM + NREM daemons).
uv run --with aiohttp --with asyncpg --with neo4j --with httpx \
  python shared-memory/scripts/hive_mind_proxy.py 8888

# 7. Verify.
curl http://localhost:8888/health
#    → {"status":"ok","api_version":1,"version":"0.4.4","daemon":"running",...}
```

The proxy binds to `127.0.0.1:8888` by default. Set `PROXY_BIND=0.0.0.0` only over
an encrypted overlay network (Tailscale/WireGuard) or behind TLS — bearer tokens
are plaintext over HTTP.

---

## The daemon roster (operations scripts)

All live in `shared-memory/scripts/` and run on the gateway host only.

| Script | Role |
|---|---|
| `hive_mind_proxy.py` | The gateway. aiohttp server on `:8888`; routes memory ops to the coordinator and embeds/reranks to `:8070`/`:8071`. Spawns and watchdogs the daemons. |
| `coordinator.py` | Owns all Postgres + Neo4j I/O — per-entity locks, outbox worker, auth middleware. Embedded in the gateway. |
| `rem_loop.py` | REM daemon — idle enrichment: full LLM summary + typed relationships per Fact. |
| `consolidation_loop.py` | NREM daemon — synthesises Tier‑3 community summaries once 5+ enriched facts share an entity hub. |
| `gpu_load.py` | GPU‑busy probe (`nvtop --snapshot`) so dreaming yields to active inference. |
| `ontology.py` | Loads `ontology.yaml`; supplies Neo4j labels/relationship types. |
| `generate_tokens.py` | One-time token minting helper. |

None of these ship with the skill. See [`sync_skills.sh`](../scripts/sync_skills.sh).

---

## Upgrading the gateway

Daemon and schema changes reach a hive through **git**, not through a skill download:

```bash
cd shared-memory-GitHub
git pull
uv run --with psycopg2-binary python shared-memory/migrations/apply.py   # apply any new migrations (idempotent)
# restart the gateway (Ctrl+C the running process, then re-run step 6 above)
```

Migrations are idempotent and run "all pending" when invoked with no argument, so
re-running after a pull is always safe. Updating an agent's **skill** never runs a
migration — the client does not own the schema.

---

## Version contract (client ↔ gateway)

The thin client and the gateway are decoupled, so they can drift. Compatibility is
enforced by an **API version**, not by file-copy parity:

- The gateway reports `api_version` (and informational `version`) on `GET /health`.
- The client sends its API version on every request via the `X-SM-Api-Version` header.
- `API_VERSION` is defined in `coordinator.py` (server) and `memory_bridge.py` (client).
  It bumps **only** on a breaking change to request/response shape, auth, or routes —
  not on every release.

Two ways skew surfaces:

1. **Caller-facing** — any agent can run the doctor command:
   ```bash
   python ~/.claude/skills/shared-memory/scripts/memory_bridge.py doctor
   ```
   It prints `compat: ok | incompatible | unknown` and, on skew, names which side
   to upgrade. The same warning is appended to error output when a request fails.

2. **Gateway-log** — when a client sends a mismatched `X-SM-Api-Version`, the
   coordinator logs a one-time warning naming the agent and the version gap.

When you bump `API_VERSION`, bump it in **both** `coordinator.py` and
`memory_bridge.py`, then `git pull` + restart the gateway and re-sync the skills.

---

## Health and observability

```bash
curl http://localhost:8888/health
```

| Field | Meaning |
|---|---|
| `status` | `ok` (embedder + reranker reachable) or `degraded` (HTTP 503) |
| `embedder` / `reranker` / `llm` | upstream backend reachability |
| `daemon` / `rem_daemon` | NREM / REM liveness |
| `auth_required` | whether `AGENT_TOKENS` is set |
| `version` / `api_version` | build version / wire contract |

The gateway auto-restarts a crashed daemon with exponential backoff; a circuit
breaker stops after 5 crashes in 10 minutes (restart the gateway to reset). Set
`AUDIT_LOG_PATH` to capture a JSON-lines log of outbox rows.
