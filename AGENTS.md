# AGENTS.md

**The canonical agent file for this repository.** Codex CLI reads it automatically before each session; Claude Code, Grok, Antigravity CLI and others are pointed here by `AGENT.md`.

It has **one mission: operate the framework** — get this repo *installed, configured, started, stopped, upgraded, or backed up* on the user's machine. You will interview the user, write their `.env`, bring up the stack, mint tokens, and verify health. Quick start, maintenance, and updates: nothing else.

`README.md` is the authoritative deep reference for everything else — architecture, internals, and working on the framework's own code; every step below links the section with the full detail. Setup-affecting changes must keep README Quick Start, this file, and `CHANGELOG.md` in sync.

---

# Operating the framework

Everything here runs on the **gateway host** — the one machine that owns the databases and daemons. Installing the *skill* into an agent is a separate, much smaller step (Phase 8).

## Ground rules for the operating agent

1. **Secrets never enter the conversation or git.** Generate passwords yourself (`openssl rand -base64 24` or Python `secrets`) instead of asking the user to type them into chat. Write them only to the gitignored `shared-memory/.env` (`chmod 600`). Confirm with `git check-ignore shared-memory/.env` before moving on. Never commit `.env`, tokens, or anything under a user's home config.
2. **Ask before destructive actions.** Token rotation (`bootstrap_tokens.sh --force`) invalidates every existing agent token; `ops/restore.sh` overwrites both databases; removing data dirs loses memory permanently. Get explicit confirmation each time.
3. **Verify each phase before the next.** Every phase ends with a check command. Do not continue past a failing check — the Quick Start troubleshooting table (README) maps the first failures you'll hit.
4. **The helper scripts are idempotent** — `preflight.sh`, `init_db.sh`, `bootstrap_tokens.sh`, `migrations/apply.py` are all safe to re-run. Prefer them over hand-rolled equivalents.
5. **Never call the embedder (`:8070`) or reranker (`:8071`) directly** — all traffic goes through the gateway on `:8888`. Never copy daemon files into a skill directory.

## First-time setup

### Phase 0 — Interview the user

Collect these answers before touching anything. Defaults in brackets are safe to offer.

**Interview conduct — these are commitments, not suggestions.** For every question you ask:
- **Explain why it is needed** when the user asks (or looks unsure): what the answer configures, and
  what happens if they take the default. Never push past "why do I need this?" with "it's required".
- **"Under what conditions?" gets a real answer** — e.g. Q3's reasoning LLM matters only for the
  background dreaming passes; saves and search work without it. Q7's Tavily key only affects
  LM Studio web search. Say what each answer does and does not gate.
- **Any question may be deferred.** Q1/Q2/Q6 block Phase 4 (the stores need paths and passwords);
  everything else can be decided later: Q3/Q4 can be filled in after first start (dreaming simply
  waits), Q5 agents can be added later (see the *Add an agent later* runbook), Q7 is optional from
  day one. When the user defers, say exactly what will work in the meantime and what won't.
- **"Can we pick this up after?" — yes, always.** Every phase is idempotent and ends with a check,
  so setup resumes cleanly from wherever it stopped: re-run the phase checks top to bottom and
  continue from the first failure. Offer to write a one-line note of where you stopped so the next
  session (yours or another agent's) resumes without re-interviewing.

| # | Ask the user | Fills |
|---|---|---|
| 1 | Where should database data live on disk? [`~/databases/neo4j`, `~/databases/postgres`] | `NEO4J_HOST_DIR`, `PG_DATA_DIR` |
| 2 | Use the **bundled embedder + reranker containers** (recommended; CPU-only, started by compose), or an existing endpoint? If bundled: which folder holds your GGUF model files? | `LLM_MODELS_DIR` |
| 3 | Where is your **reasoning LLM** served? Any OpenAI-compatible endpoint works (LM Studio, llama.cpp server, etc.) [`http://localhost:5000`]. More than one backend? List them all. | default `:5000` route, or `LLM_BACKENDS` |
| 4 | Which model family is it? Gemma → `DREAM_TEMPERATURE=0.6`; Mistral-3 Instruct / Qwen → `0.1`; Mistral-3 Reasoning → `1.0` | `DREAM_TEMPERATURE` |
| 5 | Which **agents** will use the memory? (Claude Code / Codex CLI / Grok / Antigravity CLI / LM Studio / a read-only monitor) | token minting + Phase 8 targets |
| 6 | DB passwords: shall I generate strong random ones? (recommended) | `NEO4J_PASSWORD`, `PG_PASSWORD` |
| 7 | *(optional)* Tavily API key for LM Studio web search? Backups from day one? | `TAVILY_API_KEY`, §Backup runbook |

For question 2, the compose file expects this layout under `LLM_MODELS_DIR` (edit the two `command:` paths in `postgres_neo4j_limits.yaml` if the user's files differ):

```
$LLM_MODELS_DIR/gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf
$LLM_MODELS_DIR/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf
```

Verify both files exist before Phase 4. If the user substitutes a different embedding model, the vector dimension must match the schema — see the embedding-consistency note in README Quick Start step 4.

### Phase 1 — Write the framework `.env`

`bash shared-memory/scripts/install_framework.sh` does this interactively for a human. As an agent, write the file directly instead: copy `shared-memory/.env.example` → `shared-memory/.env`, replace the values for `NEO4J_HOST_DIR`, `PG_DATA_DIR`, `LLM_MODELS_DIR`, `NEO4J_PASSWORD`, `PG_PASSWORD` (leave every other line as shipped — the commented defaults are correct), then:

```bash
chmod 600 shared-memory/.env
git check-ignore shared-memory/.env          # MUST print the path
mkdir -p "$NEO4J_HOST_DIR"/{data,logs,import,plugins} "$PG_DATA_DIR"
```

Uncomment/set `DREAM_TEMPERATURE` (Q4) and, for multiple LLM backends, `LLM_BACKENDS` (Q3). If the user's reasoning server **validates model names** (a named-model server, a routing proxy, a hosted OpenAI-compatible endpoint, or a desktop app with several models loaded), also set `LLM_MODEL` to the real id — the shipped default only suits servers that ignore the field. A single backend on a non-default port is `LLM_DEFAULT_TARGET`. All framework and helper tooling reads `shared-memory/.env` first, with a repo-root `.env` honoured as a pre-0.6 fallback.

### Phase 2 — Preflight

```bash
bash shared-memory/scripts/preflight.sh
```

Verifies Docker + compose v2, `uv`, and a populated `.env`; warns on low RAM/disk (lean minimum: 16 GB RAM, ~8 GB VRAM, ~30 GB disk). Resolve every ✗ before continuing.

### Phase 3 — OS limits (Linux)

Raise inotify limits per README §4 (needs sudo — give the user the commands to run if you cannot). On Fedora/RHEL with SELinux, keep the `:z` suffixes on the compose volume mounts.

### Phase 4 — Databases + inference containers

```bash
docker compose -f postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d
docker compose -f postgres_neo4j_limits.yaml --env-file shared-memory/.env ps   # all four healthy
```

Postgres (`:5432`), Neo4j (`:7474/:7687`), embedder (`:8070`), reranker (`:8071`). An `unhealthy` inference container is almost always a wrong model path (Phase 0 Q2).

### Phase 5 — Initialise both schemas

```bash
bash shared-memory/scripts/init_db.sh
```

Applies `schema_init.sql` (Postgres) and `neo4j_init.cypher` (Neo4j constraints) *inside* the containers — no host `psql`/`cypher-shell` needed. Idempotent.

### Phase 6 — Mint agent tokens

```bash
bash shared-memory/scripts/bootstrap_tokens.sh
```

Appends `AGENT_TOKENS` (and a read-only `AGENT_ROLES` line for `monitor`) to the framework `.env`, and prints one `AGENT_TOKEN` per agent. Save the per-agent lines — Phase 8 distributes them. One distinct token per agent, never shared. The script refuses to overwrite an existing registry; `--force` rotates **all** tokens (destructive — rule 2).

### Phase 7 — Start the gateway and verify

Smoke-test in the foreground first:

```bash
uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair \
  python shared-memory/scripts/hive_mind_proxy.py 8888
curl -s http://localhost:8888/health
```

Expect `"status":"ok"`, `"auth_required":true`, `"embedder":"ok"`, `"daemon":"running"`, `"rem_daemon":"running"`. (`"llm":"down"` only blocks dreaming, not saves/search — check the reasoning LLM from Q3.)

Then make it survive logout/reboot with the shipped `systemd --user` unit — a terminal-launched gateway dies with its session:

```bash
cp shared-memory/ops/hive-mind-gateway.service ~/.config/systemd/user/
# edit WorkingDirectory in the unit to this repo's absolute path
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now hive-mind-gateway.service
curl -s http://localhost:8888/health
```

### Phase 8 — Install the skill into each agent

For every agent from Q5, follow README §10 (§10a for remote/laptop clients): create the agent's skill dir (Antigravity CLI uses the legacy `~/.gemini/skills/` path), symlink/copy `memory_bridge.py` + `SKILL.md`, and write that agent's own `AGENT_TOKEN` into the skill `.env` (template: `shared-memory-skill/shared-memory/.env.example`). LM Studio instead registers `vector-skill.py` through `mcp.json` (fill the `YOUR_*` placeholders) and needs a full restart after token changes.

Final end-to-end check, as an agent (uses the skill path, exercises auth + embedding + storage):

```bash
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py doctor
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py save "install smoke test" '{"source":"<agent>","entities":["SetupTest"]}'
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py search "install smoke test" 3
```

**Hand each installed agent its expectations** (tell the user to relay this, or write it where that
agent will read it — Phase 8b): the memory is built around **facts** (durable results of work, with
named entities), **decisions** grounded in those facts (what was chosen, why, what was rejected),
and **retrospectives** recording whether a decision held up. Some fields are **elicited** — when the
skill asks for a rating, a grounding role, or entities and the right value is unclear, the agent
should ask its user rather than invent. The full field schemas and CLI wording live in the skill's
`SKILL.md` — that file is the contract; this paragraph is only the shape.

### Phase 8b — Offer a constitution line (per agent, optional)

A skill tells an agent *how* to call the memory; it cannot make the agent *reach for it*. That
standing behavior lives in each agent's own constitution file (`~/.claude/CLAUDE.md`,
`~/.grok/AGENTS.md`, `~/.codex/AGENTS.md`, Antigravity's `~/.gemini/` equivalent, …). For every
agent installed in Phase 8, **ask the user**: *"Would you like a short section in this agent's
constitution describing the shared memory as its preferred depository of knowledge?"* If yes,
append (adapting the file's tone; never overwrite existing content):

> **Shared memory (gateway `:8888`) is the preferred depository of knowledge on this machine.**
> Search it before starting any non-trivial task — prior facts, decisions, and retrospectives are
> the context a seasoned collaborator would want on day one. Capture as you work: durable results
> as facts; choices as decisions grounded in those facts (ask the user before recording a
> decision); outcomes as retrospectives once evidence shows whether a decision held. When the
> skill expects an elicited field you cannot infer, ask the user rather than invent. Field schemas:
> the `shared-memory` skill's `SKILL.md`.

Respect a "no" without re-asking; the skill remains fully usable without it.

## Runbooks

### Add an agent later (no token rotation)

`bootstrap_tokens.sh` refuses to touch an existing registry and `--force` rotates **everyone** —
neither is what you want for one new agent. Instead: generate one token (`openssl rand -hex 32`),
append `,<agent>:<token>` to the existing `AGENT_TOKENS=` line in `shared-memory/.env` (and to
`AGENT_ROLES=` only if it needs a restricted role), restart the gateway
(`systemctl --user restart hive-mind-gateway.service`), then run Phase 8 (+ 8b) for that agent
alone. Verify with the agent's own `doctor`.

### Start (e.g. after reboot)

The compose services carry `restart: always` and the systemd unit auto-starts under linger, so a healthy install largely self-starts. Verify, and repair only what's down:

```bash
docker compose -f postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d   # no-op if running
systemctl --user start hive-mind-gateway.service                                   # or restart
curl -s http://localhost:8888/health                                               # status: ok
```

The reasoning LLM (Q3) is managed by the user's own server (LM Studio etc.) — confirm it's serving before expecting dreaming to run. If a model lives on a partition that isn't mounted at boot, mount it **before** starting containers that bind-mount it (a missing bind source gets created as an empty root-owned dir, which can divert the mount).

### Stop (reclaim resources / maintenance)

```bash
systemctl --user stop hive-mind-gateway.service        # gateway + REM/NREM daemons
docker compose -f postgres_neo4j_limits.yaml --env-file shared-memory/.env stop   # DBs + inference
```

Stopping only the inference containers (`docker stop llama-retriever llama-reranker`) degrades saves/search (embedding mandate → 503) — stop the gateway too, or don't stop the embedder. Facts already saved are never lost by a stop; dreaming resumes where it left off.

### Status / health

```bash
curl -s http://localhost:8888/health          # gateway, daemons, backends, consolidation liveness
docker compose -f postgres_neo4j_limits.yaml --env-file shared-memory/.env ps
systemctl --user status hive-mind-gateway.service
journalctl --user -u hive-mind-gateway.service -n 50   # daemon logs
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py status   # telemetry
```

`status: degraded` on `/health` names the down backend.

**Reading `consolidation`.** There is more than one consolidation cycle type, and they have very different costs and cadences, so read the per-type block rather than the headline:

- `stalled: true` is an **OR across cycle types** — it means *at least one* is stalled. **`stalled_types` names which**, and is the only actionable field; a healthy cycle can sit beside a stalled sibling.
- Per type, `eligible_clusters` is that cycle's own gate census. **`0` means "it looked and there was nothing to do" — that is idle, not broken**, and it is the normal state when the enrichment pass hasn't yet produced a dense enough cluster. Only a *non-zero* backlog with no successful fold is a real stall.
- `runs_24h` counts runs of the cycle body; `deferred_24h` (due but skipped, usually the inference slot was busy) and `idle_24h` (gate ran, nothing eligible) are reported separately. A cycle with high `deferred_24h` is losing the slot, not failing.
- `last_success_age_seconds` is tagged with `last_success_cycle_type` — the type that achieved it, which may not be the type you are asking about.

So the triage order is: `stalled_types` → that type's `eligible_clusters` → its `last_deferred_reason` → only then the reasoning LLM.

### Upgrade (gateway host)

```bash
git pull
uv run --with psycopg2-binary python shared-memory/migrations/apply.py   # BEFORE restart
systemctl --user restart hive-mind-gateway.service
curl -s http://localhost:8888/health                                     # api_version, status ok
bash shared-memory/scripts/sync_skills.sh                                # refresh installed skills
```

Clients and gateway may drift; `memory_bridge.py doctor` names which side to upgrade on `api_version` skew.

### Backup / restore

Day-2 duty: schedule `shared-memory/ops/backup.sh` (quiesced; captures Postgres **and** Neo4j together) via the shipped systemd timer. It needs an admin-role token (`AGENT_ROLES=…,backup:admin` — confined to `/admin/*`, cannot read or write memory). Rebuild = Phases 4–5 to bring the stores up empty, then `ops/restore.sh`. Full detail: README §20 and `shared-memory/ops/README.md`.

---

## Configuration

**Framework env (gateway host):** `shared-memory/.env`, written in Phase 1 (or by `install_framework.sh`); the repo-root `.env` is honoured as a pre-0.6 fallback by the gateway **and** by `preflight.sh` / `init_db.sh` / `bootstrap_tokens.sh` / `migrations/apply.py`. **Client env:** each agent stores only `AGENT_TOKEN` in `~/<agent>/skills/shared-memory/.env` (see `shared-memory-skill/shared-memory/.env.example`). For `mcp.json`, replace all `YOUR_*` placeholders and the absolute path to `vector-skill.py`. Optional: install `nvtop` on the **infrastructure host** (where REM/NREM run) for GPU-aware dreaming — not on remote clients. **Neo4j GDS plugin** (free Community tier, `graph-data-science`) is required for alias-component grouping.

## Documentation

`README.md` — primary reference (architecture, Quick Start, save path, sleep cycle, retrieval chain). `CHANGELOG.md` — version history. `shared-memory/Documentation/schema.md` — full Postgres + Neo4j schema. `shared-memory/Documentation/server-setup.md` — operations runbook detail.

## Licensing

This repository is licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) for the full text.

When contributing or building on this work:
- Retain the copyright notice and licence header in any derived files.
- If you distribute a modified version, state that changes were made.
- Attribution to the original author is appreciated: **Xenofon S. Motsenigos**.

Do not introduce dependencies with licences incompatible with Apache 2.0 (e.g. GPL) without explicit discussion.
