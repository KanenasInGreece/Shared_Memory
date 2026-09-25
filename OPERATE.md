# OPERATE.md

Install, update, and uninstall. Coding this repository is [`AGENTS.md`](AGENTS.md). Architecture is [`README.md`](README.md). This file is not auto-loaded. Drive the scripts below. Do not reimplement them.

## Tripwires

- Never call `:8070` or `:8071`. Embed and rerank traffic goes through the gateway on `:8888`.
- Ask before `bootstrap_tokens.sh --force`, `shared-memory/ops/restore.sh`, or deleting data dirs.
- `--reveal` prints a live token. It is operator-only, in the operator's terminal, never through an agent.
- Do not `cat`, `grep`, or `.` a `.env`, and do not print a token. Phase 9 may `sed` `AGENT_TOKEN` into the environment for `postflight.sh`. That is the allowed read.
- Drive `bash shared-memory/scripts/install_framework.sh`. Do not mirror it. Never generate the two database passwords (fact:1499). On a first install the two password answers are empty so the script generates them internally. If `shared-memory/.env` already exists, do not pipe those empty answers: the overwrite path re-prompts, the empty line shifts every later answer, and both stores can lock.
- Do not run `merge_release.sh` unless the operator asked for a release.

## Install

Run the checks in order. Do not continue past a failing check. The helper scripts are idempotent.

### Obtain the source

If `shared-memory/scripts/install_framework.sh` is already here, stay. Otherwise clone:

```bash
git clone https://github.com/KanenasInGreece/Shared_Memory.git && cd Shared_Memory
```

No git: take the current tag from the [releases page](https://github.com/KanenasInGreece/Shared_Memory/releases/latest). The directory name drops the leading `v`. Agree now that upgrades are a fresh tarball, or install git. `preflight.sh` reports missing git as a failure. That is the one ✗ you may pass, and only after that agreement.

```bash
curl -L https://github.com/KanenasInGreece/Shared_Memory/archive/refs/tags/vX.Y.Z.tar.gz | tar xz
cd Shared_Memory-X.Y.Z
```

### Phase 0 — Interview

Data directories and the first-install passwords block the stores. Everything else can wait; say what waits.

| Fills | Ask |
|---|---|
| `NEO4J_DIR`, `PG_DIR` | Database directories. Default `~/databases/neo4j` and `~/databases/postgres`. |
| `MODELS_DIR` | GGUF folder for the bundled encoders, or blank if an endpoint already answers. |
| `EMB_DEV`, `RER_DEV` | `cpu` or `gpu` for each encoder. Blank means `cpu`. On a small card: embedder on GPU, reranker on CPU. |
| `RENDER_GID` | Render-node gid if either encoder is `gpu`. Blank accepts the detected value. |
| passwords | Do not choose them. The script generates both on a first install. |
| agents | Local CLIs and MCP hosts. A remote identity stays registry-only until the operator reveals a token. |
| bind | Loopback unless other machines must connect. `PROXY_BIND=0.0.0.0` only on a trusted LAN or an encrypted overlay. Tokens travel in plaintext HTTP. |

Bundled weights, when the operator does not already have them:

```
$LLM_MODELS_DIR/gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf
$LLM_MODELS_DIR/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf
```

### Phase 1 — Write `.env`

Skip this phase when `shared-memory/.env` already exists.

First install only. The two empty lines are the password answers. Do not replace them. The trailing `n` answers skip the script's systemd prompt and its LLM-backend prompt; Phase 7 and `bash shared-memory/ops/install_llm_backends.sh` cover those.

```bash
NEO4J_DIR=<from the interview>
PG_DIR=<from the interview>
MODELS_DIR=<GGUF folder, or blank>
EMB_DEV=<cpu or gpu>
RER_DEV=<cpu or gpu>
RENDER_GID=<blank unless a GPU encoder was chosen>
printf '%s\n%s\n%s\n%s\n%s\n%s\n\n\nn\nn\n' "$NEO4J_DIR" "$PG_DIR" "$MODELS_DIR" "$EMB_DEV" "$RER_DEV" "$RENDER_GID" \
  | bash shared-memory/scripts/install_framework.sh
git check-ignore shared-memory/.env
```

`git check-ignore` must print the path. Under ~8 GB RAM, set `NEO4J_HEAP_INITIAL`, `NEO4J_HEAP_MAX`, and `NEO4J_PAGECACHE` from `shared-memory/.env.example` after the script. A credentialed reasoning backend: drive `bash shared-memory/ops/install_llm_backends.sh`. The JSON key is `url`, never `base_url`. Ask for the env-var name, never the key. Then `python3 shared-memory/scripts/check_config.py --phase-a-only`.

### Phase 2 — Preflight

```bash
bash shared-memory/scripts/preflight.sh
```

Resolve every ✗ except the agreed missing-git case. `curl`, `python3`, and `timeout` are required. The disk warning is docker's data-root, not the database directories.

### Phase 3 — OS packages

Docker Engine and Compose v2 from Docker's repository, then the inotify limits in README. Hand the operator each root command.

### Phase 4 — Databases and encoders

```bash
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env ps
```

Never call `:8070` or `:8071`. An external encoder pair must already be answering on `EMBEDDER_URL` and `RERANKER_URL`.

### Phase 5 — Schemas

```bash
bash shared-memory/scripts/init_db.sh
```

### Phase 6 — Remote and registry tokens

Phase 6 mints only identities with no local skill directory: `lm_studio` and `antigravity`. It does not mint `claude`, `gemini`, `grok`, or `codex`. Those four are refused until their directories exist. Their tokens are minted in Phase 8.

```bash
bash shared-memory/scripts/bootstrap_tokens.sh
```

A bare run refuses to overwrite an existing registry. `--force` rotates every token. Ask first.

`--reveal` is operator-only, in the operator's terminal, never through an agent. Do not run the next block yourself.

```bash
# Operator only. Never through an agent. Prints a live token.
bash shared-memory/scripts/bootstrap_tokens.sh --remint lm_studio --reveal lm_studio
bash shared-memory/scripts/bootstrap_tokens.sh --add monitor --reveal monitor
```

`monitor` is not on the bare run. Mint it only when the operator wants the dashboard, with `--reveal` on that same command, in their terminal.

A READ-ONLY IDENTITY IS ALWAYS MINTED READ-ONLY. A `read` token may reach `GET /memory/telemetry`, `POST /memory/search`, and `GET /memory/status/{pg_id}`; `POST /memory/graph` is 403; `/health` is anonymous (not a read-role grant).

⚠ Ask before `bash shared-memory/scripts/bootstrap_tokens.sh --force`, `bash shared-memory/ops/restore.sh`, or deleting data dirs.

One new local agent, in this order: `mkdir` the skill directory, then `--add`, then `bash shared-memory/scripts/sync_skills.sh`. `--add` refuses a directory that does not exist yet.

### Phase 7 — Gateway

Say that you pinned dependencies with the lock. The unpinned `uv run --with` form is the operator's choice, not yours.

The smoke test stays in the foreground. Stop it before the next command. It dies with the session, and nothing after it in the same paste will run.

```bash
uv run --no-project --with-requirements requirements-gateway.lock \
  python shared-memory/scripts/hive_mind_proxy.py 8888
```

```bash
bash shared-memory/ops/install_service.sh
curl -s http://localhost:8888/health
```

HTTP status codes never distinguish auth configured from auth off: both answer 200, and a rejected bearer still gets the anonymous body. Auth on, with no bearer, is exactly `status`, `version`, and `api_version` — no `auth_required` key. Auth off is the full payload and spells `auth_required:false`. Do not treat 200 as proof that auth is on. After a real token exists, an authenticated curl shows `"auth_required":true` plus the daemon fields. `"llm":"down"` blocks dreaming, not saves or search.

Auth is read at startup. A mint after this start is not visible until the restart in Phase 8.

### Phase 8 — Install the skill

For each local agent (`claude`, `codex`, `gemini`, `grok`), the directory exists before the mint.

```bash
mkdir -p <skill-dir>
bash shared-memory/scripts/bootstrap_tokens.sh --add <agent> --install-path <skill-dir>/.env
bash shared-memory/scripts/sync_skills.sh
```

`--add` refuses a name already registered. That refusal is expected on a re-run. Move on. `--remint` invalidates that agent's current token. Ask before it. Use `--reveal` only when there is no local file, and only the operator runs it.

An MCP host on this machine uses a walled directory and `--mcp`, in the same order:

```bash
install -d -m 700 <walled-dir>
bash shared-memory/scripts/bootstrap_tokens.sh --add <agent> --mcp --install-path <walled-dir>/.env
bash shared-memory/scripts/sync_skills.sh
```

`--install-path` is the `.env` file, not the directory. `MANIFEST.txt` is the list of what a skill install ships: `SKILL.md`, `USAGE.md`, `CONSTITUTION_SNIPPET.md`, `.env.example`, `memory_bridge.py`, `update_skill.sh`, and `schema.md`. An `mcp` install receives `vector-skill.py`, `CONSTITUTION_SNIPPET_MCP.md`, and `system-prompt.md` instead, never `mcp/mcp.json`. Spawn that connector with `uv run --no-project` and an absolute `uv`. The host config shape is `mcp/README.md`.

Restart after the mint, before `doctor` or `postflight.sh`. The running gateway still has the previous registry.

```bash
systemctl --user restart hive-mind-gateway.service
```

Smoke from a project directory, not the skill directory. The first save in an empty corpus needs `new_project: true` and `new_entities`.

```bash
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py doctor
```

### Phase 8b — Constitution line

Ask first. Copy the block from the installed skill's `CONSTITUTION_SNIPPET.md`, or `CONSTITUTION_SNIPPET_MCP.md` when the install was `--mcp`. Never from this checkout. Never splice into this checkout's `AGENTS.md` or `OPERATE.md`. For OpenCode, do not guess `~/.config/opencode/AGENTS.md`. A first install from `$HOME` is often `~/AGENTS.md`. Ask which file the session loaded.

### Phase 9 — Prove the install

`bash shared-memory/scripts/postflight.sh` exits 0 iff assertions **A1–A5, A8 and A9** all pass. A8 skips, rather than fails, when no reasoning backend is healthy. Do not `cat`, `grep`, or source the skill `.env`. This `sed` is the allowed read. Do not print the token.

```bash
AGENT_ENV=${AGENT_ENV:-$HOME/.claude/skills/shared-memory/.env}
AGENT_TOKEN=$(sed -n 's/^AGENT_TOKEN=//p' "$AGENT_ENV" | head -1); export AGENT_TOKEN
bash shared-memory/scripts/postflight.sh
```

Point `AGENT_ENV` at the agent just installed when it is not Claude.

### Serve the encoders with vLLM

The default image stays llama.cpp (`postgres_neo4j_limits.yaml`). Do not switch the embedder silently on any other card.

An Intel Arc B580 (`8086:e20b`, 12 GB) may use the packaged file `shared-memory/ops/intel-arc-b580.yaml`. That is Main workstation configuration 2: both encoders on `intel/vllm:0.21.0-xpu`, and `rerank_shim.py` in front of the reranker. Leave the llama.cpp encoder replicas at 0. Set `EMBEDDER_URL=http://127.0.0.1:8091` and `RERANKER_URL=http://127.0.0.1:8092`. Each server already passes `--max-model-len 8192`. The embedder needs no shim. Do not use that file for a different Arc card.

## Update

Do not hand-roll the migration order. Do not run `merge_release.sh` unless the operator asked for a release.

### Upgrade (gateway host)

A checkout on a DETACHED HEAD makes `update_framework.sh` refuse. Recover with `git checkout main`, then re-run. A deliberate pinned tag is checked out as that tag, or installed from a tarball. Do not pull from a detached HEAD.

A tarball host has no repository to pull. Unpack the new tag, copy `shared-memory/.env` across, and continue in the new directory.

```bash
AGENT_ENV=${AGENT_ENV:-$HOME/.claude/skills/shared-memory/.env}
AGENT_TOKEN=$(sed -n 's/^AGENT_TOKEN=//p' "$AGENT_ENV" | head -1); export AGENT_TOKEN
bash shared-memory/scripts/update_framework.sh --dry-run
bash shared-memory/scripts/update_framework.sh
```

`--from-restore` is a different entry. Run it only after `shared-memory/ops/restore.sh` has loaded a dump, and only after the operator has agreed. The overwrite is `restore.sh`. This flag skips `git pull` and skips `backup.sh`, then migrates. Do not paste it into an upgrade.

```bash
AGENT_ENV=${AGENT_ENV:-$HOME/.claude/skills/shared-memory/.env}
AGENT_TOKEN=$(sed -n 's/^AGENT_TOKEN=//p' "$AGENT_ENV" | head -1); export AGENT_TOKEN
bash shared-memory/scripts/update_framework.sh --from-restore
```

| # | Step | Note |
|---|---|---|
| 2 | `shared-memory/ops/backup.sh` | The script runs this before migrating. `--skip-backup` is not for a host holding the only copy of the data. |

Do not run `shared-memory/scripts/backfill_domain_of.py` before the restarted gateway is new enough. An older worker can blank record content. A plain `update_framework.sh` skips that step. Pass `--domain-backfill` only when existing records already carry a domain.

Stack reconcile is separate and recreates database containers. Ask first. Show `bash shared-memory/scripts/reconcile_stack.sh --dry-run`. Run `bash shared-memory/scripts/reconcile_stack.sh` only on the operator's word.

If a constitution snippet's version marker moved after sync, propose the new block. Do not overwrite it silently.

## Start, stop, backup

Start brings the stores and the gateway up. It does not install or mint.

```bash
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d
systemctl --user start hive-mind-gateway.service
```

Stop releases the gateway and the containers. It does not remove skill directories or data.

```bash
systemctl --user stop hive-mind-gateway.service
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env stop
```

Backup captures Postgres and Neo4j together. It needs a `backup:admin` token already available to the script. Do not paste that token.

```bash
bash shared-memory/ops/backup.sh
```

## Running these over ssh

A non-interactive ssh shell reads no profile, so a user-local `uv` in `~/.local/bin` is not on PATH. That is the correct outcome rather than a defect (`fact:1460`: install tooling comes from upstream instructions, and a user-local install being invisible to a profile-free shell is what that buys). Every script here that shells out to `uv` fails having done nothing and says so, so export it first:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

`uninstall_framework.sh` also asks for the level to be typed and has no terminal over ssh, so it refuses with `not confirmed — nothing was removed`. Its documented `--yes` is the non-interactive path, after the `--dry-run`.

⛔ `--reveal` is the opposite case: it refuses when stdout is not a terminal, and that is deliberate. Do not reach for the override to get it working over ssh — run it in your own terminal.

## Uninstall

**Capture these first when the level removes `shared-memory/.env`** — `data` and `all` both do, and it holds every credential this install has. Restoring a credentialed reasoning backend needs TWO lines and only the first is obvious (measured on a d9400 rebuild).

```bash
grep '^LLM_BACKENDS_JSON' shared-memory/.env      # the backend entry
grep '_FILE=' shared-memory/.env                  # the pointers to key files
```

⛔ The `<KEY>_FILE` pointer is a separate line. Without it the backend loads `has_credential=False` and the pool serves nothing while `LLM_BACKENDS_JSON` looks correctly restored; after a reinstall confirm with `python3 shared-memory/scripts/check_config.py --phase-a-only`, where that env-var name must read `has_credential=True`. The key FILE survives when it lives outside the framework's own state, and the uninstall lists any such credential it deliberately did not remove.

A reinstall MUST mint fresh tokens: the `.env` held digests only, every raw token lived in the skill directories the uninstall deletes, so an old `.env` configures auth nobody can satisfy.

`--level` is required. There is no default. Show `--dry-run` first. Ask which level.

```bash
bash shared-memory/scripts/uninstall_framework.sh --level service --dry-run
bash shared-memory/scripts/uninstall_framework.sh --level service
```

`service` stops the gateway and removes skill directories. It is reversible. `data` and `all` are not. They refuse unless a backup set exists, unless the operator passes `--no-backup`. Each level is its own command. The script never removes `~/.shared-memory` or the checkout. It prints the checkout removal for the operator to run.

```bash
bash shared-memory/scripts/uninstall_framework.sh --level data --dry-run
```

```bash
bash shared-memory/scripts/uninstall_framework.sh --level data
```

```bash
bash shared-memory/scripts/uninstall_framework.sh --level all --dry-run
```

```bash
bash shared-memory/scripts/uninstall_framework.sh --level all
```
