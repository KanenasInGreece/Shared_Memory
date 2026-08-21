# AGENTS.md

**The canonical agent file for this repository.** Codex CLI reads it automatically before each session; Claude Code, Grok, Antigravity CLI and others are pointed here by `AGENT.md`.

It has **one mission: operate the framework** — get this repo *installed, configured, started, stopped, upgraded, or backed up* on the user's machine. You will interview the user, write their `.env`, bring up the stack, mint tokens, and prove the result with the postflight verification (Phase 9) — an install is not finished until postflight passes. Quick start, maintenance, and updates: nothing else.

`README.md` is the authoritative deep reference for everything else — architecture, internals, and working on the framework's own code; every step below links the section with the full detail. Setup-affecting changes must keep README Quick Start, this file, and `CHANGELOG.md` in sync.

---

# Operating the framework

Everything here runs on the **gateway host** — the one machine that owns the databases and daemons. Installing the *skill* into an agent is a separate, much smaller step (Phase 8).

## Ground rules for the operating agent

1. **Secrets never enter the conversation or git.** Generate passwords yourself — **hex only** (`openssl rand -hex 20` or Python `secrets.token_hex(20)`), never base64: base64 output contains `/`, which the compose file's `NEO4J_AUTH=neo4j/<password>` cannot carry — the container restart-loops on "… is invalid" (measured on a fresh install). Generate rather than asking the user to type them into chat. Write them only to the gitignored `shared-memory/.env` (`chmod 600`). Confirm with `git check-ignore shared-memory/.env` before moving on. Never commit `.env`, tokens, or anything under a user's home config.
   **This is stricter still for a reasoning-LLM backend's own API credential** (`LLM_BACKENDS_JSON`'s `token_env`, Q3/Phase 1 below) — that value is never written to *any* file at all, gitignored or not, including `shared-memory/.env` itself. Ask the user only for the **name** of an env var they'll export from their own encrypted secret store (`pass`, GPG-backed, or equivalent); never ask them to paste the literal key into chat or a file. If a `.env`/`LLM_BACKENDS_JSON` you're editing ever contains something that looks like a real key in a `token`/`api_key`/`secret` field rather than a `token_env` name, stop and fix it — the gateway itself refuses to load that backend (`hive_mind_proxy.py`, `_load_llm_backends`) and logs exactly why, but don't rely on that as the first line of defense.
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
| 3 | Where is your **reasoning LLM** served? Any OpenAI-compatible endpoint works (LM Studio, llama.cpp server, etc.) [`http://localhost:5000`]. More than one backend, local or remote? List them all. **Does any of them need an API credential** (a paid cloud endpoint, e.g. DeepSeek/xAI/OpenRouter)? If so, ask only for the **name** of the env var they'll export it under — never the key itself. | default `:5000` route, `LLM_BACKENDS`, or `LLM_BACKENDS_JSON` |
| 4 | Which model family is it? Gemma → `DREAM_TEMPERATURE=0.6`; Mistral-3 Instruct / Qwen → `0.1`; Mistral-3 Reasoning → `1.0`; DeepSeek (online) → `0.6`, **with thinking disabled** via the backend entry's `extra_body` — in thinking mode DeepSeek silently ignores temperature | `DREAM_TEMPERATURE` |
| 5 | Which **agents** will use the memory? (Claude Code / Codex CLI / Grok / Antigravity CLI / LM Studio / a read-only monitor) | token minting + Phase 8 targets |
| 6 | DB passwords: shall I generate strong random ones? (recommended) | `NEO4J_PASSWORD`, `PG_PASSWORD` |
| 7 | *(optional)* Tavily API key for LM Studio web search? Backups from day one? | `TAVILY_API_KEY`, §Backup runbook |

For question 2, the compose file expects this layout under `LLM_MODELS_DIR` (edit the two `command:` paths in `shared-memory/ops/postgres_neo4j_limits.yaml` if the user's files differ):

```
$LLM_MODELS_DIR/gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf
$LLM_MODELS_DIR/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf
```

If the user does not have the files yet, download them (~600 MB each, from the
`gpustack` GGUF repackagings on Hugging Face — the same paths the compose defaults name):

```bash
mkdir -p "$LLM_MODELS_DIR"/gpustack/{bge-m3-GGUF,bge-reranker-v2-m3-GGUF}
curl -L -o "$LLM_MODELS_DIR/gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf" \
  https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf
curl -L -o "$LLM_MODELS_DIR/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf" \
  https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf
```

Verify both files exist before Phase 4 (preflight checks this too). If the user substitutes a different embedding model, the vector dimension must match the schema — see the embedding-consistency note in README Quick Start step 4.

### Phase 1 — Write the framework `.env`

`bash shared-memory/scripts/install_framework.sh` does this interactively for a human. As an agent, write the file directly instead — **under `umask 077` before the copy**, the same fix item 3/S-07 applied to the script itself, so the file holding two DB passwords is never even briefly world/group-readable (create-then-chmod leaves exactly that window): copy `shared-memory/.env.example` → `shared-memory/.env`, replace the values for `NEO4J_HOST_DIR`, `PG_DATA_DIR`, `LLM_MODELS_DIR`, `NEO4J_PASSWORD`, `PG_PASSWORD` (leave every other line as shipped — the commented defaults are correct), then:

```bash
umask 077
cp shared-memory/.env.example shared-memory/.env
# … edit shared-memory/.env in place: NEO4J_HOST_DIR, PG_DATA_DIR, LLM_MODELS_DIR, NEO4J_PASSWORD, PG_PASSWORD …
chmod 600 shared-memory/.env          # belt-and-suspenders — umask above already made it 600
git check-ignore shared-memory/.env          # MUST print the path
mkdir -p "$NEO4J_HOST_DIR"/{data,logs,import,plugins} "$PG_DATA_DIR"
```

Also set `LLAMA_CPU_THREADS` the way the script derives it — host threads / 2 + 1 (`$(( $(nproc) / 2 + 1 ))`) — the compose fallback is 4, which oversubscribes a small CPU and starves the databases. On a host under ~8 GB RAM, set the small-host Neo4j memory preset too (`NEO4J_HEAP_INITIAL`/`NEO4J_HEAP_MAX`/`NEO4J_PAGECACHE` — values and the why in `.env.example`): the shipped defaults refuse to start when heap max + pagecache exceed physical RAM.

Uncomment/set `DREAM_TEMPERATURE` (Q4) and, for multiple LLM backends, `LLM_BACKENDS` (Q3). If the user's reasoning server **validates model names** (a named-model server, a routing proxy, a hosted OpenAI-compatible endpoint, or a desktop app with several models loaded), also set `LLM_MODEL` to the real id — the shipped default only suits servers that ignore the field. A single backend on a non-default port is `LLM_DEFAULT_TARGET`. All framework and helper tooling reads `shared-memory/.env` first, with a repo-root `.env` honoured as a pre-0.6 fallback.

**If Q3 turned up a backend needing a credential, use `LLM_BACKENDS_JSON` instead of `LLM_BACKENDS`.** The complete numbered walkthrough (encrypted store → `LoadCredential=` or a `<VAR_NAME>_FILE` runtime pointer → JSON entry with `token_env` plus the mandatory `private_ok`/`roles` choice → restart → verify on `/health`) and the full per-entry parameter table both live in `shared-memory/ops/README.md`, "Reasoning-LLM backends" — **follow them verbatim rather than improvising**; `.env.example` carries the short form beside `LLM_BACKENDS_JSON`. Three rules they encode: the literal key never goes in any file this framework writes — only the env-var **name**; the key at rest belongs in an encrypted store (`pass`/GPG/`systemd-creds`), with **`LoadCredential=` or a runtime `<VAR_NAME>_FILE`** (SEC-06, PR A4) preferred over `systemctl --user import-environment`, which is deprecated (readable by any same-uid process via `show-environment`, and inherited by every user unit); and a credentialed entry with neither `roles` nor an explicit `private_ok` refuses gateway startup by design — ask the operator which they want; never pick for them.

### Phase 2 — Preflight

```bash
bash shared-memory/scripts/preflight.sh
```

Verifies Docker + compose v2, `uv`, and a populated `.env`; warns on low RAM/disk (16 GB RAM and ~30 GB disk are the common floor; a GPU is optional — the three measured example configurations are README §3). Resolve every ✗ before continuing.

### Phase 3 — OS limits (Linux)

Raise inotify limits per README §4 (needs sudo — give the user the commands to run if you cannot). On Fedora/RHEL with SELinux, keep the `:z` suffixes on the compose volume mounts.

**Sudo for an agent-driven install:** several steps here need root (package install, inotify, dir ownership). Either hand the user each command to run in their own terminal, or ask them to grant the session temporary passwordless sudo — from a real terminal (an agent session has no TTY for the password prompt):

```bash
ssh -t <host> "echo '<user> ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/99-<user>-temp"
```

and **delete `/etc/sudoers.d/99-<user>-temp` at the end of the session** — offer that cleanup unprompted. On Fedora/RHEL, docker itself is the first thing needing it: the distro ships podman, and the helper scripts call the docker CLI (`sudo dnf install moby-engine docker-compose` provides `docker compose` v2; the `podman-docker` shim is the untested alternative).

**Driving the install over ssh: run every phase through a login shell** (`bash -lc "…"`). A bare
ssh command runs a non-interactive shell whose PATH omits user-level installs — uv's documented
installer lands in `~/.local/bin`, so preflight reports "uv not found" on a machine where uv is
correctly installed (measured on a clean Ubuntu Server install).

### Phase 4 — Databases + inference containers

```bash
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env ps   # all four healthy
```

The one yaml carries **both** encoder pairs — CPU (`llama-retriever`/`llama-reranker`, the
default) and Vulkan GPU (`llama-retriever-gpu`/`llama-reranker-gpu`, off by default). The
choice is two lines in `shared-memory/.env`: `GPU_ENCODER_REPLICAS=1` + `CPU_ENCODER_REPLICAS=0`
(exactly one pair nonzero — they share ports). **Put the choice to the operator, with the
compromise stated plainly:** a GPU with enough VRAM for their reasoning model is usually better
spent on the model backend; a small card (~4 GB) is best spent on the encoders (~2 GB for the
pair, repaid in search latency — measured numbers in README §17). Always the operator's call —
never pick silently. `ps` accordingly shows four containers — the inference pair `healthy`
(they carry healthchecks; which pair depends on the choice) and the two stores `Up` (they
carry none; Phase 5's init is what proves them) — or two when **both** pairs are 0 because the
encoders are hosted outside the stack entirely — then `EMBEDDER_URL`/`RERANKER_URL` must say
where, or saves are refused.

Postgres (`:5432`), Neo4j (`:7474/:7687`), embedder (`:8070`), reranker (`:8071`). An `unhealthy` inference container is almost always a wrong model path (Phase 0 Q2).

### Phase 5 — Initialise both schemas

```bash
bash shared-memory/scripts/init_db.sh
```

Applies `schema_init.sql` (Postgres) and `neo4j_init.cypher` (Neo4j constraints) *inside* the containers — no host `psql`/`cypher-shell` needed. Idempotent.

Confirm both actually landed rather than trusting the exit status — each store has a verifier that builds or reads the real thing, and both are read-only unless told otherwise:

```bash
uv run --with psycopg2-binary python shared-memory/migrations/verify_schema_init.py
uv run --with neo4j python shared-memory/migrations/verify_neo4j_init.py
```

### Phase 6 — Mint agent tokens

```bash
bash shared-memory/scripts/bootstrap_tokens.sh
```

Appends `AGENT_TOKENS` (digest form) and a read-only `AGENT_ROLES` line for `monitor` to the framework `.env`. generate_tokens.py's write-through mint flow (PR A2) writes each LOCAL agent's token straight into that agent's own skill `.env` (mode 600) — nothing is printed here to save. A REMOTE agent's token needs `--reveal <name>` passed to `bootstrap_tokens.sh` itself on this SAME invocation (a later, separate reveal mints a fresh token for every agent — a full rotation, not a free peek). ⚠ **The reveal invocation is the one command the HUMAN runs in their own terminal, never you** — the script prints the raw token, and an agent transcript turns "shown once" into "stored forever" (the script's own warning; verified the hard way — a token revealed through an agent session had to be rotated). Hand the user the exact command line and step back. One distinct token per agent, never shared. The script refuses to overwrite an existing registry; `--force` rotates **all** tokens (destructive — rule 2).

### Phase 7 — Start the gateway and verify

Smoke-test in the foreground first:

```bash
uv run --no-project --with-requirements requirements-gateway.lock \
  python shared-memory/scripts/hive_mind_proxy.py 8888
curl -s http://localhost:8888/health                       # anonymous: liveness only
curl -s -H "Authorization: Bearer <a-phase-6-token>" http://localhost:8888/health   # full payload
```

**Dependency pinning is the default — say so when you start the gateway.** The lock pins the
gateway's dependencies to the exact tested versions (the shipped systemd unit below runs from the
same lock, and `git pull` advances it with the code). Tell the user that is what you did; if they
prefer latest-at-invocation resolution instead, the equivalent unpinned form is
`uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair python …` —
their call, not yours. `requirements-gateway.lock` is deliberately narrower than
`requirements.txt` (the gateway process must not carry `psycopg2`) — never substitute the full
`requirements.lock` here.

**`/health` has two shapes once Phase 6 minted tokens (S-10):** an anonymous caller gets exactly
`{"status","version","api_version"}` — enough for liveness, nothing more — and every richer field
needs a bearer token. So: from the bare curl expect just `"status":"ok"`; from the authenticated
one expect `"auth_required":true`, `"embedder":"ok"`, `"daemon":"running"`, `"rem_daemon":"running"`.
(`"llm":"down"` only blocks dreaming, not saves/search — check the reasoning LLM from Q3.) An
install that skipped tokens (auth off) serves the full payload to everyone, unchanged.

Then make it survive logout/reboot with the shipped `systemd --user` unit — a terminal-launched gateway dies with its session:

```bash
bash shared-memory/ops/install_service.sh   # substitutes the repo path into the unit,
                                            # enables linger, enables + starts the service;
                                            # degrades with a clear message on non-systemd hosts
curl -s http://localhost:8888/health
```

### Phase 8 — Install the skill into each agent

For every agent from Q5, follow README §10 (§10a for remote/laptop clients): create the agent's skill dir (Antigravity CLI uses the legacy `~/.gemini/skills/` path), install the skill package, and write that agent's own `AGENT_TOKEN` into the skill `.env` (template: `shared-memory-skill/shared-memory/.env.example`). An MCP host (LM Studio is the exercised example) instead registers `mcp/vector-skill.py` through its config (template: `mcp/mcp.json` — fill the `YOUR_*` placeholders) and needs a full restart after token changes; the connector is client-deployable, so on a machine without the repo install a copy of the `mcp/` folder and point the host's config at it (see `mcp/README.md`).

**What "the skill package" is — `shared-memory-skill/shared-memory/MANIFEST.txt` is the authority, not a list in this file.** It currently ships `SKILL.md`, `CONSTITUTION_SNIPPET.md`, `.env.example`, `scripts/memory_bridge.py`, `scripts/update_skill.sh` and `Documentation/schema.md`. Install all of it: two later phases depend on files an "just SKILL.md and the script" install would leave out — Phase 8b copies its block from `CONSTITUTION_SNIPPET.md` *in the skill directory*, and Phase 8c and every future update run `scripts/update_skill.sh` *from there*. The reliable way to get it right is to let the tooling do it: create the directory with `memory_bridge.py` in place, then run `update_skill.sh` (or `sync_skills.sh` on the gateway host), which reads the manifest so a file added to the package later needs no change here.

⛔ **COPY EVERY FILE — NEVER SYMLINK ONE INTO A SOURCE CHECKOUT.** A link is auto-current, and that convenience is not worth what it costs: it binds every agent on the machine to one checkout's path, so moving, renaming or archiving that directory breaks all of them at once, silently, and the first symptom is an agent failing mid-task. Staleness is the lesser risk precisely because it is **detectable** — every file is content-compared on each sync and `doctor` reports version skew. `sync_skills.sh` and `update_skill.sh` both **replace** any symlink they find with a real copy, and `sync_skills.sh` refuses outright to write into an install directory that is itself a link, because that would make the source its own destination.

Final end-to-end check, as an agent (uses the skill path, exercises auth + embedding + storage).
**Run it from inside a project directory** — the repo checkout itself is fine — because the
client derives the record's project from the working directory; issued from elsewhere (the
skill dir, `$HOME`) the save is refused with `project_required`:

```bash
cd <repo-or-any-project-root>
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py doctor
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py save "install smoke test" '{"source":"<agent>","entities":["SetupTest"],"new_entities":["SetupTest"],"new_project":true}'
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py search "install smoke test" 3
```

Two flags are needed exactly once, for the same reason: a fresh corpus has no registered
projects and an empty entity vocabulary, so the very first save is refused (`project_unknown`
/ `entity_unknown`) until `new_project: true` registers the project and `new_entities` mints
the entity's canonical spelling — the gateway asks rather than guessing. Every later save into
a registered project with known entities omits both. (Each refusal's message carries its own
recovery instructions; follow them rather than overriding.)

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
copy the block verbatim from **`CONSTITUTION_SNIPPET.md`** (shipped alongside `SKILL.md` in this
same skill directory — the file, not this paragraph, is the canonical source of truth) into the
constitution file, adapting only the surrounding tone if needed and never overwriting existing
content. Do not regenerate or paraphrase the block — copying it verbatim keeps it marker-delimited
and versioned (`<!-- shared-memory:constitution-snippet vN -->`), which is what lets a later
update (Phase 8c below) detect drift and re-propose instead of duplicating it.

Respect a "no" without re-asking; the skill remains fully usable without it.

### Phase 8c — Keep an installed constitution line current (per agent, on update)

`CONSTITUTION_SNIPPET.md`'s version marker can advance between framework releases (a wording fix,
for instance). After running `update_skill.sh` for an agent that already has the block installed
(Phase 8b), compare the version marker in the operator's constitution file against the one in the
freshly-updated `CONSTITUTION_SNIPPET.md`. If they differ, **propose** replacing the old
marker-delimited block with the new one (show what changed and why) — never overwrite it silently.
If the agent's constitution file has no marker-delimited block at all, treat it as never having
been offered and fall back to Phase 8b.

### Phase 9 — Verify the install (postflight)

Prove the installed stack works end to end — liveness and payload shape, version contract, schema
truth, the full write path (canary save → 1024-dim vector → outbox applied → `:Fact` node), and an
honestly-graded read path — and emit a performance baseline for this hardware. The contract is
`shared-memory/Documentation/postflight.md`; the script implements it and exits 0 iff assertions
A1–A5 pass. The canary lands under the reserved project `install-verification` and stays in the
corpus — the install's birth certificate.

```bash
export AGENT_TOKEN=...   # auth-on installs: any minted agent token, from that agent's skill .env
bash shared-memory/scripts/postflight.sh
```

## Runbooks

### Add an agent later (no token rotation)

`bootstrap_tokens.sh` refuses to touch an existing registry and `--force` rotates **everyone** —
neither is what you want for one new agent. Instead: choose one token yourself
(`openssl rand -hex 32`), get its **digest** entry with `generate_tokens.py --digest` (reads the
raw token from STDIN, never argv — argv is visible via `ps` and shell history), append the printed
`<agent>:sha256:<hex>` to the existing `AGENT_TOKENS=` line in `shared-memory/.env` (and to
`AGENT_ROLES=` only if it needs a restricted role), write the SAME raw token into that agent's own
skill `.env` yourself (mode 600 — `chmod 600` it), restart the gateway
(`systemctl --user restart hive-mind-gateway.service`), then run Phase 8 (+ 8b) for that agent
alone. Verify with the agent's own `doctor`. **A plaintext `<agent>:<token>` entry is refused
outright as of v0.9.3** — the gateway will not start with one, so the digest step above is not
optional.

```bash
tok=$(openssl rand -hex 32)
printf '%s' "$tok" | uv run python shared-memory/scripts/generate_tokens.py --digest <agent>
# → prints <agent>:sha256:<hex> — append that to AGENT_TOKENS= in shared-memory/.env
echo "AGENT_TOKEN=$tok" >> <skill-dir>/.env && chmod 600 <skill-dir>/.env
```

### Start (e.g. after reboot)

The compose services carry `restart: always` and the systemd unit auto-starts under linger, so a healthy install largely self-starts. Verify, and repair only what's down:

```bash
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d   # no-op if running
systemctl --user start hive-mind-gateway.service                                   # or restart
curl -s http://localhost:8888/health                                               # status: ok
```

The reasoning LLM (Q3) is managed by the user's own server (LM Studio etc.) — confirm it's serving before expecting dreaming to run. If a model lives on a partition that isn't mounted at boot, mount it **before** starting containers that bind-mount it (a missing bind source gets created as an empty root-owned dir, which can divert the mount).

### Stop (reclaim resources / maintenance)

```bash
systemctl --user stop hive-mind-gateway.service        # gateway + REM/NREM daemons
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env stop   # DBs + inference
```

Stopping only the inference containers (`docker stop llama-retriever llama-reranker`) degrades saves/search (embedding mandate → 503) — stop the gateway too, or don't stop the embedder. Facts already saved are never lost by a stop; dreaming resumes where it left off.

### Status / health

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" http://localhost:8888/health \
                                              # gateway, daemons, backends, consolidation liveness
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env ps
systemctl --user status hive-mind-gateway.service
journalctl --user -u hive-mind-gateway.service -n 50   # daemon logs
uv run --with httpx --with python-dotenv python <skill-dir>/scripts/memory_bridge.py status   # telemetry
```

⚠ **Every `/health` read below this point means the AUTHENTICATED payload** (any minted agent
token; S-10 slims the anonymous shape to `status`/`version`/`api_version`). A triage that reads
`consolidation`, `stalled_types`, backend names, `project_identity` or `domain_identity` from a
bare curl on an auth-configured install sees none of them — that is the missing token, not a
broken gateway. `memory_bridge.py status` sends its own token and is unaffected.

`status: degraded` shows anonymously; *which* backend is down is in the authenticated payload.

**Reading `consolidation` — the two halves live on DIFFERENT endpoints.** There is more than one consolidation cycle type, with very different costs and cadences, so the per-type block is what you act on. But `/health` carries only the summary; the per-type census is on `/memory/telemetry`, which is what `memory_bridge.py status` reads. Asking `/health` for a per-type field returns nothing and is the easiest way to get stuck mid-triage.

On **`/health`** → `consolidation`, the summary:

- `stalled: true` is an **OR across cycle types** — it means *at least one* is stalled. **`stalled_types` names which**, and is the only actionable field here; a healthy cycle can sit beside a stalled sibling.
- `last_success_age_seconds` is tagged with `last_success_cycle_type` — the type that achieved it, which may not be the type you are asking about.

On **`GET /memory/telemetry`** → `consolidation.<cycle_type>` (e.g. `consolidation.fact_consolidation`, `consolidation.insight`), the per-type detail:

- `eligible_clusters` is that cycle's own gate census. **`0` means "it looked and there was nothing to do" — that is idle, not broken**, and it is the normal state when the enrichment pass hasn't yet produced a dense enough cluster. Only a *non-zero* backlog with no successful fold is a real stall.
- `runs_24h` counts runs of the cycle body; `deferred_24h` (due but skipped, usually the inference slot was busy) and `idle_24h` (gate ran, nothing eligible) are reported separately. A cycle with high `deferred_24h` is losing the slot, not failing.
- `last_deferred_reason` says *why* it yielded; `folds_attempted_24h` / `folds_succeeded_24h` separate "tried and failed" from "never tried".

So the triage order is: `/health` → `stalled_types` → then switch to `status` / `/memory/telemetry` for that type's `eligible_clusters` → its `last_deferred_reason` → only then the reasoning LLM.

**Two staleness traps in the same payload.** `backend_capability` (the embedder/reranker speed
projection) re-probes on its own schedule, not on backend changes — after swapping encoders
(CPU pair ↔ GPU pair) it can keep reporting the OLD backend's numbers for a while; check
`probed_at` before acting on it. And a **Neo4j outage longer than the outbox retry window**
(5 attempts with backoff) leaves `neo4j_outbox` rows in a terminal `failed` status that nothing
retries — Tier 1 keeps the record, but it stays absent from the graph. `/health` surfaces it as
a non-null `failed_age`; recovery is one statement, then the worker drains it within seconds:

```bash
docker exec postgres-vector psql -U postgres -d agent_data \
  -c "UPDATE neo4j_outbox SET status='pending', retries=0, next_attempt_at=now() WHERE status='failed';"
```

**A rerank-fallback burst on a small host** — `GET /memory/telemetry` now carries
`rerank_fallbacks_total` / `rerank_fallbacks_last_ts` / `rerank_successes_total` (the search path
already degrades to vector order on any rerank failure and still answers 200, so this was
previously invisible from outside the log). A rising `rerank_fallbacks_total` against a flat
`rerank_successes_total` on a memory-constrained install is most often the kernel OOM-killing the
reranker container — the fallback's WARNING log line names this explicitly when the failure is a
dropped/reset connection (not a timeout): check
`docker inspect <your reranker container: llama-reranker or llama-reranker-gpu> --format '{{.State.OOMKilled}} {{.RestartCount}}'` (a bare `llama-server` reranker has no container — go straight to `dmesg`), `dmesg`, and the
`capacity` record on authenticated `/health` for this host's derived limits. The mem_limit itself
is reported-only (decision:1424) — nothing here caps or restarts the container.

### Upgrade (gateway host)

```bash
git pull
uv run --with psycopg2-binary python shared-memory/migrations/apply.py   # BEFORE restart
uv run --with psycopg2-binary --with neo4j python \
    shared-memory/scripts/reconcile_project_identity.py --apply          # graph half of a migration
uv run --with neo4j python shared-memory/migrations/verify_neo4j_init.py # Neo4j has NO ledger
systemctl --user restart hive-mind-gateway.service
curl -s http://localhost:8888/health                                     # api_version, status ok
uv run --with psycopg2-binary python \
    shared-memory/scripts/backfill_domain_of.py                          # AFTER restart, dry-run first — see below
uv run --with psycopg2-binary python \
    shared-memory/scripts/backfill_domain_of.py --apply                  # then APPLY — dry-run alone enqueues nothing
bash shared-memory/scripts/sync_skills.sh                                # refresh installed skills
bash shared-memory/scripts/postflight.sh                                 # verify end to end (Phase 9)
```

⚠ **`backfill_domain_of.py` runs AFTER the restart, and that ordering is a guard rather than a
preference.** It enqueues a narrow repair row that only a gateway from v0.8.47 understands; an older
worker does not recognise the row type, falls through to its ordinary fact branch, and blanks the
content of every record it touches. The script refuses to enqueue against a gateway that is too old —
including one it cannot reach, because an unknown version is not permission to write — so running it
early is safe but pointless. It is **dry-run by default**; nothing is enqueued without `--apply` —
the upgrade snippet above runs it once without the flag to show what WOULD be enqueued, then again
with `--apply` to actually enqueue it, because the upgrade flow needs the row applied, not previewed.
It is also only needed on a deployment whose records already carry a `domain` in their metadata: a
new install has none, and every save from here on writes its own edge.

⚠ **`reconcile_project_identity.py` is the graph half of a Postgres migration, and no migration can
run it for you.** A project's identity is a registry row id; the `:Project` node carries that id so the
cross-project fold gate can count *identities* rather than a renameable name. `apply.py` creates the ids
and cannot reach Neo4j, so this stamps the nodes. It is idempotent and read-only without `--apply` — run
it on every upgrade, exactly like the Neo4j constraint check, and for the same reason. **Skipping it
does not break writes**: records still save, still search, still enrich. What stops is *cross-project
synthesis* — the gate fails closed on a node with no identity — which presents as a system with nothing
to fold rather than as an error. Authenticated `GET /health` → `project_identity` is where that
state is visible (`complete: false` with an `unidentified` count).

⚠ **The domain axis has the same shape and one extra number.** Authenticated `GET /health` → `domain_identity`
reports `unregistered` / `mismatched` between the registry and the graph, plus **`unattached`** — a
`:Domain` node with no `PROJECT_OF` edge, i.e. a section belonging to no project. That last one is
reported for a walk that does not exist yet: cross-project and cross-domain synthesis will traverse
`(:Domain)-[:PROJECT_OF]->(:Project)`, and a section missing that edge would silently drop out of it,
which looks like a quiet corpus rather than an error. A **fresh install reads `nodes: 0,
registry_rows: 0, complete: true`** and that is correct — domains are optional, and an empty registry
is the right starting state. Sections are registered through ingress the same way projects are.

⚠ **Run the Neo4j check on every upgrade, and do not assume it is redundant.** `apply.py` covers Postgres only, and Postgres has a migration ledger that records what has been applied. **Neo4j has none** — `neo4j_init.cypher` is a one-time manual step, so a long-lived instance enforces whatever constraint set was true the day someone last ran it, and a constraint added to the file in a later release reaches new installs and nobody else. A missing uniqueness constraint is silent: `MERGE` keeps working and the only symptom is a duplicate node appearing under a race. Add `--apply` to create what is missing; it exits 1 when a declared constraint is not in force, so it is safe to gate on. *(This is not hypothetical — the deployment this framework was built on was enforcing one of the seven declared constraints, and a plain index on `Entity.name` was blocking a second. `--apply` handles that case; re-running `neo4j_init.cypher` does not.)*

Clients and gateway may drift; `memory_bridge.py doctor` names which side to upgrade on `api_version` skew.

### Backup / restore

Day-2 duty: schedule `shared-memory/ops/backup.sh` (quiesced; captures Postgres **and** Neo4j together) via the shipped systemd timer. It needs an admin-role token (`AGENT_ROLES=…,backup:admin` — confined to `/admin/*`, cannot read or write memory). Rebuild = Phases 4–5 to bring the stores up empty, then `ops/restore.sh`. Full detail: README §20 and `shared-memory/ops/README.md`.

---

## Configuration

**Framework env (gateway host):** `shared-memory/.env`, written in Phase 1 (or by `install_framework.sh`); the repo-root `.env` is honoured as a pre-0.6 fallback by the gateway **and** by `preflight.sh` / `init_db.sh` / `bootstrap_tokens.sh` / `migrations/apply.py`. **Client env:** each agent stores only `AGENT_TOKEN` in `~/<agent>/skills/shared-memory/.env` (see `shared-memory-skill/shared-memory/.env.example`). For `mcp.json` (template: `mcp/mcp.json`), replace all `YOUR_*` placeholders and the absolute path to `mcp/vector-skill.py`. Optional: install `nvtop` on the **infrastructure host** (where REM/NREM run) for GPU-aware dreaming — not on remote clients. **Neo4j GDS plugin** (free Community tier, `graph-data-science`) is required for alias-component grouping.

## Documentation

`README.md` — primary reference (architecture, Quick Start, save path, sleep cycle, retrieval chain). `CHANGELOG.md` — version history. `shared-memory/Documentation/schema.md` — full Postgres + Neo4j schema. `shared-memory/Documentation/server-setup.md` — operations runbook detail.

## Licensing

This repository is licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) for the full text.

When contributing or building on this work:
- Retain the copyright notice and licence header in any derived files.
- If you distribute a modified version, state that changes were made.
- Attribution to the original author is appreciated: **Xenofon S. Motsenigos**.

Do not introduce dependencies with licences incompatible with Apache 2.0 (e.g. GPL) without explicit discussion.
