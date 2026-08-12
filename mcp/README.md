# `mcp/` — the MCP connector

This folder is the **MCP front door** to the shared-memory gateway — the second of the two
client surfaces (the first is the CLI skill in `shared-memory-skill/`, used by Claude Code,
Grok, Codex CLI and Antigravity). Everything an MCP host needs to mount the memory lives here,
together, so the connector is distinguishable from the framework's server surface: nothing in
this folder runs daemons, touches a database, or holds server credentials.

| File | Role |
|---|---|
| `vector-skill.py` | The MCP server (FastMCP, **stdio**) — a thin HTTP client to the gateway on `:8888` |
| `system-prompt.md` | System prompt for the MCP host's model — the memory protocol, search-first discipline, and the prohibition on database MCPs |
| `mcp.json` | Host config **template** — copy the `rag-orchestrator` block into your MCP host's config and fill the `YOUR_*` placeholders |

## What it is (and is not)

`vector-skill.py` is a **thin client**: every operation is an authenticated HTTP call to the
Hive-Mind Gateway. It opens no ports (stdio transport — the MCP host spawns it and pipes to it),
holds no database drivers, and enforces nothing the gateway does not enforce — auth, read
authorization, locking, idempotency and consolidation all live server-side. That is deliberate:
a direct database MCP registered alongside it would bypass all of those, which is why
`system-prompt.md` forbids one and the shipped template contains none.

LM Studio is the host exercised end to end (README §21), but nothing here assumes it — any MCP
host that can spawn a stdio server and inject environment variables can mount the memory.

## Tools (13, at parity with the CLI skill)

Retrieval and diagnostics: `hybrid_search_and_rerank`, `graph_query` (read-only Cypher),
`record_lineage`, `memory_telemetry`, `check_memory_health`.
Capture: `save_artifact`, `save_decision`, `save_retrospective`, `archive_reasoning_trace`.
Record lifecycle: `supersede`, `review_hold`.
Edge calibration: `review_edges`, `label_edges`.

The full client contract — every field, refusal and the reasoning behind each — is
[`shared-memory/SKILL.md`](../shared-memory/SKILL.md); the MCP tools mirror it.

## How it authenticates — where the token lives

The connector authenticates as **one agent** with its own `AGENT_TOKEN` (minted on the gateway
host, e.g. `lm_studio`). Three ways to supply it, in the order to prefer them:

1. **The MCP host's `env` block** (recommended — what the `mcp.json` template shows):
   `AGENT_TOKEN` and `COORDINATOR_URL` injected at spawn. No file involved. MCP servers read
   their environment once, at spawn — restart the host completely after a token change.
2. **`VECTOR_SKILL_ENV`** — an explicit path to a client `.env`, for installs that keep it
   elsewhere.
3. **A `.env` beside the script** (`mcp/.env`, gitignored) — holding only `AGENT_TOKEN` and
   optionally `COORDINATOR_URL` / `AGENT_ID`.

⚠ **The client env is never the framework env.** `shared-memory/.env` on the gateway host
carries `PG_PASSWORD`, `NEO4J_PASSWORD` and the entire `AGENT_TOKENS` registry — a client that
loaded it would inherit every agent's credentials. `vector-skill.py` recognises a server env by
its keys and **refuses to load it**, telling you why; give the connector its own token in one of
the three forms above.

## Setup in one minute

```bash
# 1. Mint a token on the gateway host (see Documentation/server-setup.md), then
# 2. register the server in your MCP host's config — adapted from mcp/mcp.json:
"rag-orchestrator": {
  "command": "uv",
  "args": ["run", "--with", "fastmcp", "--with", "httpx", "--with", "python-dotenv",
           "python", "/path/to/shared_mem/mcp/vector-skill.py"],
  "env": { "COORDINATOR_URL": "http://localhost:8888", "AGENT_TOKEN": "YOUR_TOKEN" }
}
# 3. give the host's model mcp/system-prompt.md (or fold it into your existing prompt)
# 4. restart the MCP host fully, then call check_memory_health — it verifies the
#    gateway, both models, and the token in one shot.
```
