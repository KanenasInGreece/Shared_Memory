# `mcp/` — the MCP connector

This folder is the **MCP front door** to the shared-memory gateway — the second of the two
client surfaces (the first is the CLI skill in `shared-memory-skill/`, used by Claude Code,
Grok, Codex CLI and Antigravity). Everything an MCP host needs to mount the memory lives here,
together, so the connector is distinguishable from the framework's server surface: nothing in
this folder runs daemons, touches a database, or holds server credentials.

| File | Role | Delivered to an install? |
|---|---|---|
| `vector-skill.py` | The MCP server (FastMCP, **stdio**) — a thin HTTP client to the gateway on `:8888` | ✅ yes |
| `CONSTITUTION_SNIPPET_MCP.md` | The standing rules as a marker-delimited, versioned block, for an **agent** host's own constitution file | ✅ yes |
| `system-prompt.md` | The same rules wrapped for an **LLM server** whose model is configured by a system prompt (LM Studio), plus the operational detail | ✅ yes |
| `mcp.json` | Host config **template** — adapt the `rag-orchestrator` block into your MCP host's config and fill the `YOUR_*` placeholders | ⛔ **never** — see below |

⛔ **`mcp.json` is never copied into an install.** It ships `YOUR_*` placeholders and a
`/path/to/your/...` repo path: in a live install it looks like configuration and is not, and a host
that read it would try to authenticate with the literal string `YOUR_LM_STUDIO_AGENT_TOKEN`. Adapt
it into the host's own config file instead.

⚠ **The two rule files are not interchangeable, and no rule lives in only one of them.**
`CONSTITUTION_SNIPPET_MCP.md` is for an agent that has a constitution file; `system-prompt.md` is
for a model that has a system-prompt field. A CLI agent running the thin-client skill takes
neither — it takes `shared-memory/CONSTITUTION_SNIPPET.md`.

## What it is (and is not)

`vector-skill.py` is a **thin client**: every operation is an authenticated HTTP call to the
Hive-Mind Gateway. It opens no ports (stdio transport — the MCP host spawns it and pipes to it),
holds no database drivers, and enforces nothing the gateway does not enforce — beyond refusing to SEND what the gateway would certainly reject (a save without its required provenance fields fails fast client-side, before any credential is spent) — auth, read
authorization, locking, idempotency and consolidation all live server-side. That is deliberate:
a direct database MCP registered alongside it would bypass all of those, which is why
`system-prompt.md` forbids one and the shipped template contains none.

**It is client-deployable, exactly like the CLI skill** — and on the gateway machine that
deployment is a **walled install directory**, registered in `AGENT_INSTALLS` with kind `mcp` and
kept current by `sync_skills.sh`, not a host config pointed at this checkout. The connector needs
only the file, a token, and a route to the gateway (directly, or over a tunnel/VPN), so a host on
another machine takes a hand-placed copy of the same three files instead. Any MCP host that can
spawn a stdio server and inject environment variables can mount the memory this way — both an
**agent** host (opencode) and an **LLM server** (LM Studio, README §21), which differ only in
where the standing rules go.

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
host). Three ways to supply it, in the order to prefer them:

1. **`VECTOR_SKILL_ENV`** (recommended, and what the walled install below uses) — an explicit path
   to the walled directory's own `.env`, which the mint wrote through at mode 600. No token in any
   config file, and the same file a `--remint` later rewrites in place.
2. **A `.env` beside the script** — same shape, discovered rather than named. Holds only
   `AGENT_TOKEN` and optionally `COORDINATOR_URL` / `AGENT_ID`.
3. **The MCP host's `env` block** — `AGENT_TOKEN` injected at spawn, which is what the `mcp.json`
   template still shows. Simplest, but it puts a live token into a config file, so it travels
   wherever that file is backed up or synced. Prefer 1.

MCP servers read their environment once, at spawn — restart the host completely after a token
change, and restart the **gateway** too, since auth is startup-frozen.

⚠ **The client env is never the framework env.** `shared-memory/.env` on the gateway host
carries `PG_PASSWORD`, `NEO4J_PASSWORD` and the entire `AGENT_TOKENS` registry — a client that
loaded it would inherit every agent's credentials. `vector-skill.py` recognises a server env by
its keys and **refuses to load it**, telling you why; give the connector its own token in one of
the three forms above.

## The WALLED INSTALL — the primary path for an MCP host on the gateway machine

Do **not** point a host's config at this folder inside the repo checkout. Give the connector its
own **walled directory**, mode 700, holding its own token and nothing else — the same shape a CLI
agent's skill directory has, and for the same reasons: the install survives the checkout being
moved, renamed or switched to another branch, and the token sits in a directory nothing else
writes to.

```bash
install -d -m 700 ~/.config/<host>/shared-memory-mcp
bash shared-memory/scripts/bootstrap_tokens.sh --add <agent> --mcp \
     --install-path ~/.config/<host>/shared-memory-mcp/.env
bash shared-memory/scripts/sync_skills.sh
```

**File manifest after a sync** — exactly four things live there, three delivered and one minted:

| Path | Mode | Written by |
|---|---|---|
| `<walled-dir>/` | `700` | you (`install -d -m 700`), re-enforced by every sync |
| `vector-skill.py` | `600` | `sync_skills.sh` |
| `CONSTITUTION_SNIPPET_MCP.md` | `600` | `sync_skills.sh` |
| `system-prompt.md` | `600` | `sync_skills.sh` |
| `.env` (holds only `AGENT_TOKEN`) | `600` | the **mint**, write-through — never sync, which only mode-checks it |

`--mcp` is what makes this work: it records the registry entry as `name:mcp:path`, and that kind is
what `sync_skills.sh` reads to deliver the **connector** package here rather than the CLI skill
package. Without it, `SKILL.md` and `memory_bridge.py` land in the walled directory — a skill no
MCP host can run, beside a live token. An entry with no kind (`name:path`) is a CLI skill install
permanently; nothing rewrites an existing line.

⚠ **`--install-path` names the `.env` FILE, not the directory.** The mint splits it into
dirname + basename and writes the leaf.

⚠ **Already-registered name?** `--add` refuses it, by design — there is no silent single-agent
rotation. Re-home it with `--remint <agent> --mcp --install-path <walled-dir>/.env`, which
write-throughs the new token and touches nobody else. ⛔ Do not reach for `--reveal`: it prints a
live token, so it is operator-only, run in the operator's own terminal, never through an agent.

Then register it with the host — the **walled copy's** path, never the repo's:

```jsonc
"shared-memory": {
  "type": "local",
  "command": ["/home/you/.local/bin/uv", "run", "--with", "fastmcp", "--with", "httpx",
              "--with", "python-dotenv", "python",
              "/home/you/.config/<host>/shared-memory-mcp/vector-skill.py"],
  "environment": {
    "COORDINATOR_URL": "http://localhost:8888",
    "VECTOR_SKILL_ENV": "/home/you/.config/<host>/shared-memory-mcp/.env"
  }
}
```

⚠ **Name `uv` by ABSOLUTE path.** An MCP host spawns its stdio server from a non-interactive,
non-login shell, and the installer this project recommends puts `uv` under `$HOME/.local/bin`,
exposed only by a profile that shell never reads. A bare `"uv"` here simply never starts, and the
host reports a dead MCP server rather than a PATH problem. `which uv` on the gateway host gives the
path to use. `sync_skills.sh` warns whenever this host's `uv` is profile-only.

⚠ **No `AGENT_TOKEN` in the host config.** `VECTOR_SKILL_ENV` points at the walled `.env` the mint
already wrote; a token in a config file is a token in whatever backs that file up.

Finally, **apply the rules and restart both processes**:

- **Agent host** (has its own constitution file): propose splicing the marker-delimited block from
  `CONSTITUTION_SNIPPET_MCP.md` — ask first (`AGENTS.md` Phase 8b), never write it silently.
- **LLM server** (has a system-prompt field): paste `system-prompt.md` into the model's system
  prompt.
- **Restart the MCP host fully** — an MCP server reads its environment once, at spawn.
- ⚠ **Restart the GATEWAY too, and this is the one that gets forgotten.** The mint's new digest
  loads only on gateway **restart**: auth is startup-frozen, so the running process keeps the old
  digest while the new one sits in the `.env`. An install reported "done" without
  `systemctl --user restart hive-mind-gateway.service` is a **401 on the next session**.
- Then call `check_memory_health` — it verifies the gateway, both models, and the token in one
  shot. *(That is an MCP tool call, so it needs the host running. `sync_skills.sh` runs the
  non-authenticating half of it for you at delivery time: it byte-compiles the connector copy and
  compares the gateway's `/health` `api_version` against the connector's.)*

## Setup on ANOTHER machine

A host that is not on the gateway machine is genuinely remote: nothing here can write a token
through to it. Copy the three delivered files onto that machine by hand, have an **operator** mint
and carry its token (`--remint <agent> --reveal <agent>`, run in their own terminal), and use the
same config shape above with a `COORDINATOR_URL` that reaches the gateway.
