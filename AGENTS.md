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
| 2 | Use the **bundled embedder + reranker containers** (recommended; started by compose, CPU-only by default), or an existing endpoint? If bundled: which folder holds your GGUF model files? **Does this host have a spare GPU you want the encoder pair to use instead?** (Phase 4 has the trade-off — a GPU is otherwise easy to leave silently unused, or double-booked with the reasoning LLM.) | `LLM_MODELS_DIR`; if GPU: `GPU_ENCODER_REPLICAS`/`CPU_ENCODER_REPLICAS`, `GPU_RENDER_GID` |
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

`bash shared-memory/scripts/install_framework.sh` does this interactively for a human — and it does
more than write a file: it also derives `LLAMA_CPU_THREADS` from this host's core count and chowns
Neo4j's `import`/`plugins` dirs to the container's uid (7474), which a freshly `mkdir -p`'d,
user-owned dir is NOT (the container crash-loops on "/import is not accessible" without it —
measured on a fresh install). An earlier version of this phase hand-reimplemented the file write and
`mkdir` for agent use and never picked up the chown step at all, so every agent-driven install
silently skipped it — a **second instance of the same class**: an agent path that hand-mirrors a
helper script's side effects only stays correct until the script grows a new one. **The fix is to
stop mirroring and DRIVE THE SCRIPT ITSELF**, so this phase cannot drift from what
`install_framework.sh` actually does again — when the script changes, this phase's behavior changes
with it, automatically. Any other phase that reimplements a helper script's logic by hand rather
than invoking it carries the identical risk and deserves the same treatment.

`install_framework.sh` is interactive (`read -r -p` prompts), but that does not require a live
terminal — feeding its answers as newline-delimited **stdin**, in the order it asks them, drives it
exactly as a human would (`read`/`read -s` consume the next line from a pipe the same as from a tty;
`-s` just can't visually hide it, which does not matter here since nothing sensitive is echoed back).
Generate the two passwords yourself first (ground rule 1 — hex only, never base64: a `/` breaks the
compose file's `NEO4J_AUTH=neo4j/<password>` parsing and the container restart-loops on "… is
invalid", though the script itself also rejects one and re-prompts, so this is belt-and-suspenders).
**Skip this phase entirely if `shared-memory/.env` already exists** (resuming a stopped setup) —
re-running the script would hit its own overwrite prompt instead of the directory prompts, which is
not what a resume wants.

```bash
NEO4J_DIR=<from Q1>                      # e.g. $HOME/databases/neo4j
PG_DIR=<from Q1>                         # e.g. $HOME/databases/postgres
MODELS_DIR=<from Q2, blank if using LM Studio>
NEO4J_PW="$(openssl rand -hex 20)"
PG_PW="$(openssl rand -hex 20)"
# 5 answers for the dirs/passwords, then "n" to each of the two TRAILING prompts
# (systemd service install, LLM-backend helper) — Phase 7 and the LLM_BACKENDS /
# LLM_BACKENDS_JSON edit below handle those explicitly, with more context than
# the script's own generic prompt gives. printf is a shell builtin, so none of
# this — including the passwords — ever appears on a process's own argv.
printf '%s\n%s\n%s\n%s\n%s\nn\nn\n' "$NEO4J_DIR" "$PG_DIR" "$MODELS_DIR" "$NEO4J_PW" "$PG_PW" \
  | bash shared-memory/scripts/install_framework.sh
git check-ignore shared-memory/.env          # MUST print the path
```

The script already writes the file `umask 077` (S-07 — never briefly world/group-readable), derives
`LLAMA_CPU_THREADS` from this host rather than the compose fallback of 4 (which oversubscribes a
small CPU and starves the databases), creates every data dir, and chowns Neo4j's `import`/`plugins`
for you. On a host under ~8 GB RAM, still set the small-host Neo4j memory preset by hand afterward
(`NEO4J_HEAP_INITIAL`/`NEO4J_HEAP_MAX`/`NEO4J_PAGECACHE` — values and the why in `.env.example`): the
script does not ask about RAM, and the shipped defaults refuse to start when heap max + pagecache
exceed physical RAM.

Uncomment/set `DREAM_TEMPERATURE` (Q4) and, for multiple LLM backends, `LLM_BACKENDS` (Q3). **If Q2
turned up a GPU for the encoders**, uncomment/set `GPU_ENCODER_REPLICAS=1` + `CPU_ENCODER_REPLICAS=0`
(Phase 4 has the trade-off) and `GPU_RENDER_GID` — read the actual value with
`stat -c '%g' /dev/dri/renderD128` rather than trusting the packaged default: on Debian the render
node's group is `render` (gid 992 there, measured on a fresh Debian 13 install), not `video`. If the
user's reasoning server **validates model names** (a named-model server, a routing proxy, a hosted
OpenAI-compatible endpoint, or a desktop app with several models loaded), also set `LLM_MODEL` to the
real id — the shipped default only suits servers that ignore the field. A single backend on a
non-default port is `LLM_DEFAULT_TARGET`. All framework and helper tooling reads `shared-memory/.env`
first, with a repo-root `.env` honoured as a pre-0.6 fallback.

**If Q3 turned up a backend needing a credential, use `LLM_BACKENDS_JSON` instead of `LLM_BACKENDS`.** The complete numbered walkthrough (encrypted store → `LoadCredential=` or a `<VAR_NAME>_FILE` runtime pointer → JSON entry with `token_env` plus the mandatory `private_ok`/`roles` choice → restart → verify on `/health`) and the full per-entry parameter table both live in `shared-memory/ops/README.md`, "Reasoning-LLM backends" — **follow them verbatim rather than improvising**; `.env.example` carries the short form beside `LLM_BACKENDS_JSON`. Three rules they encode: the literal key never goes in any file this framework writes — only the env-var **name**; the key at rest belongs in an encrypted store (`pass`/GPG/`systemd-creds`), with **`LoadCredential=` or a runtime `<VAR_NAME>_FILE`** (SEC-06, PR A4) preferred over `systemctl --user import-environment`, which is deprecated (readable by any same-uid process via `show-environment`, and inherited by every user unit); and a credentialed entry with neither `roles` nor an explicit `private_ok` refuses gateway startup by design — ask the operator which they want; never pick for them.

### Phase 2 — Preflight

```bash
bash shared-memory/scripts/preflight.sh
```

Verifies Docker + compose v2, `uv`, and a populated `.env`; warns on low RAM/disk (16 GB RAM and ~30 GB disk are the common floor; a GPU is optional — the three measured example configurations are README §3). Resolve every ✗ before continuing.

**The disk warning is about docker's data-root, not the checkout — a different concern from Q1's
`NEO4J_HOST_DIR`/`PG_DATA_DIR`.** `preflight.sh` measures free space on
`docker info --format '{{.DockerRootDir}}'` (falling back to `/var/lib/docker`): images and container
layers land there, NOT the databases (those follow wherever Q1 pointed them — a separate, already-made
decision). On many distros' default partitioning this is a smaller, DIFFERENT filesystem than the
checkout (Debian's default LVM layout commonly gives `/var` a thin slice and `/home` the rest —
measured on a fresh Debian 13 install). Resolve a low-disk warning here, **before Phase 4 pulls any
images** — moving docker's data-root after several GB have already landed there just repeats the
copy. To move it: stop docker, set `"data-root": "/new/path"` in `/etc/docker/daemon.json`,
`rsync -a` the old tree across, restart docker, and confirm with the same `docker info` command.

### Phase 3 — OS limits (Linux)

Raise inotify limits per README §4 (needs sudo — give the user the commands to run if you cannot). On Fedora/RHEL with SELinux, keep the `:z` suffixes on the compose volume mounts.

**Sudo for an agent-driven install:** several steps here need root (package install, inotify, dir ownership). Either hand the user each command to run in their own terminal, or ask them to grant the session temporary passwordless sudo — from a real terminal (an agent session has no TTY for the password prompt):

```bash
ssh -t <host> "echo '<user> ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/99-<user>-temp"
```

and **delete `/etc/sudoers.d/99-<user>-temp` at the end of the session** — offer that cleanup unprompted. On Fedora/RHEL, docker itself is the first thing needing it: the distro ships podman, and the helper scripts call the docker CLI.

**Which steps actually need root**, enumerated so a temporary-sudo grant can cover all of them in
one window instead of being discovered piecemeal mid-install:
- Phase 3 — installing the Docker Engine + Compose v2 packages themselves (`apt`/`dnf install …`).
- Phase 3 — `systemctl enable --now docker`, `usermod -aG docker $USER` (group membership needs a
  fresh login/shell to take effect — do not expect it to apply to the CURRENT shell).
- Phase 3 — raising inotify limits (`sysctl`, per README §4).
- Phase 3, only if `docker.io` was ever installed on this host — `apt purge docker-buildx`,
  `dpkg --configure -a` (the recovery two paragraphs up).
- Phase 7, only as a fallback — `install_service.sh` tries `loginctl enable-linger` for the current
  user first, which works without root on most systemd-logind setups, and reaches for `sudo` only if
  that is refused.

Everything else in this file — `docker compose`, `docker exec`, the Python/bash helper scripts, the
skill itself — runs as the ordinary user; do not `sudo` anything not on this list.

⛔ **Install Docker Engine + Compose v2 from Docker's own repository — <https://docs.docker.com/engine/install/> — on every distro.** That is the packaging our installs run and the only one this project tests against; a distro's own docker packages may work but are not the tested path. (Recorded fallbacks, with provenance: Fedora's repos carry `moby-engine` + `docker-compose`, measured on Fedora 43; Debian 13 ships compose v2 under the legacy package name `docker-compose`, measured on Debian 13. The `podman-docker` shim remains untested.)

⚠ **Switching to Docker's repo on a host where `docker.io` was ever installed:** purging `docker.io`, `docker-compose`, `containerd` and `runc` is **not enough**. Debian's `docker-buildx` arrives as a dependency of `docker.io`, survives that purge, and owns `/usr/libexec/docker/cli-plugins/docker-buildx` — so Docker's `docker-buildx-plugin` fails to unpack on a file-overwrite conflict and the install half-lands: binaries answer `--version`, several packages sit unpacked-but-unconfigured, and `docker.service` is left **disabled and dead** while nothing obviously looks wrong. Recovery: `sudo apt purge docker-buildx` → `sudo dpkg --configure -a` → reinstall the five Docker packages → `sudo systemctl enable --now docker`.

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

⚠ **`uv` must be reachable WITHOUT this agent's shell profile, or every invocation of the skill fails silently.** Every command in this phase (and in `SKILL.md`) runs `memory_bridge.py` **through `uv run`** — and the agent almost never spawns the interactive login shell that installed uv. It spawns a non-interactive, non-login shell to exec the command, which reads none of `~/.bashrc` / `~/.profile` and starts with whatever PATH its own parent process handed it. The upstream installer this project recommends (`curl -LsSf https://astral.sh/uv/install.sh | sh`) puts `uv` under `$HOME/.local/bin` and relies on exactly the profile that shell never reads — so a host that followed the recommended install correctly can still leave every agent unable to run the skill, and the agent does not report a broken memory system when this happens: it answers some other way, or saves nothing, and nobody sees why. `preflight.sh` now checks this directly (a warning distinct from its existing "is uv installed at all" check) and `sync_skills.sh` repeats it once per run whenever an agent install actually exists on disk — **re-run `preflight.sh` after installing an agent, not only before**, since this failure mode is per-agent-shell, not per-host. If either warns, either symlink `uv` onto a directory already on the system default PATH (e.g. `sudo ln -s "$(command -v uv)" /usr/local/bin/uv` — keeps the recommended install, just exposes it further) or set `PATH` inside this agent's own configuration to include `uv`'s directory.

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
standing behavior lives in each agent's own constitution file. **The `AGENT_INSTALLS` registry
(Phase 6, `generate_tokens.py`) is the actual roster of installed agents as of v0.9.27 — an agent
added later via `--add` needs no framework release to be supported by THAT mechanism.** This
section stays prose rather than deriving its list from the registry (a documentation phase has no
programmatic view of it), so treat the paths below as illustrative of the ones this framework
ships a thin-client skill to, not exhaustive — nothing in this repo tracks a constitution-file path
per agent centrally, only the skill-install path. Known constitution files: `~/.claude/CLAUDE.md`
(Claude Code), `~/.grok/AGENTS.md` (Grok), `~/.codex/AGENTS.md` (Codex CLI), Antigravity's
`~/.gemini/` equivalent, `~/.config/opencode/AGENTS.md` (opencode's own native global instruction
file). *(This list drifted before: opencode was the first agent ever registered through the new
`AGENT_INSTALLS` mechanism and was never added here — the roster mechanism stopped needing a
release, the prose enumerating it did not follow.)* For an agent not listed here, ask the operator
where that agent's own constitution/instructions file lives rather than guessing a path.

For every agent installed in Phase 8, **ask the user**: *"Would you like a short section in this
agent's constitution describing the shared memory as its preferred depository of knowledge?"* If
yes, copy the block verbatim from **`CONSTITUTION_SNIPPET.md` AT THAT AGENT'S OWN INSTALLED SKILL
DIRECTORY** — the exact file Phase 8 just placed there, e.g.
`~/.claude/skills/shared-memory/CONSTITUTION_SNIPPET.md` — into the constitution file, adapting
only the surrounding tone if needed and never overwriting existing content.

⚠ **Read the INSTALLED skill's copy, never a repository clone that happens to sit on the same
machine.** A host that also has this repo checked out carries a SECOND `CONSTITUTION_SNIPPET.md`
under the checkout's own `shared-memory-skill/shared-memory/` tree, and the two can diverge — the
checkout may sit on a different tag than the last `sync_skills.sh` run, or be mid-edit on a branch.
Copying from the checkout risks installing a snippet version the installed skill does not actually
ship, while Phase 8c's drift check compares the constitution file against the *installed skill's*
copy and would report it current regardless — a wrong install that silently reads as verified. The
installed skill directory is the only source of truth for this step; a repository checkout is a
development tree, never an install.

Do not regenerate or paraphrase the block — copying it verbatim keeps it marker-delimited
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
**A1–A5 and A8** pass (A8 SKIPs rather than gates when no reasoning backend is currently reported
healthy — a SKIP there is not a failure). The canary lands under the reserved project `install-verification` and stays in the
corpus — the install's birth certificate. **This first run always mints it** (the corpus has no
live Tier-3 summaries yet); a **later re-run** (e.g. after a hardware change) switches
automatically to re-baseline mode once the corpus holds real summaries — no new canary, the read
path is proven against that real content instead (see `postflight.md`).

```bash
export AGENT_TOKEN=...   # auth-on installs: any minted agent token, from that agent's skill .env
bash shared-memory/scripts/postflight.sh
```

## Runbooks

### Add an agent later (no token rotation)

`bootstrap_tokens.sh` (bare) refuses to touch an existing registry and `--force` rotates
**everyone** — neither is what you want for one new agent. `--add` (v0.9.27) is the purpose-built
additive mint: it registers exactly one new agent's `AGENT_INSTALLS` entry, mints its token,
write-throughs it into that agent's skill `.env` (mode 600), and updates `AGENT_TOKENS` +
`AGENT_INSTALLS` in `shared-memory/.env` **in place** — every other agent's digest byte-identical,
untouched.

⚠ **The required order — two individually-correct guards are jointly circular otherwise:**
`--add` REFUSES a target directory that does not exist yet (deliberate — D19: never mint a token
into a digest registry that nobody actually received, which is worse than not minting at all), while
`sync_skills.sh` only creates a directory for an agent the registry **already** names. Neither script
can bootstrap the other, so the directory has to come from somewhere outside both:

1. **`mkdir -p <skill-dir>`** — the ONE manual step, e.g. `mkdir -p ~/.codex/skills/shared-memory`.
2. **`bootstrap_tokens.sh --add <agent> --install-path <skill-dir>/.env`** — mints, writes the token
   through, and updates the gateway `.env` registry.
3. **`sync_skills.sh`** — the directory and the registry entry both exist now, so this populates the
   rest of the skill package (`SKILL.md`, `memory_bridge.py`, …) into it — no `--install` flag
   needed (that flag is for the *other* edge case: a registered agent whose directory does not exist
   yet, not this one).

Then restart the gateway (`systemctl --user restart hive-mind-gateway.service`) so it loads the
updated `AGENT_TOKENS`, add the agent to `AGENT_ROLES=` by hand only if it needs a restricted role
(`--add` does not touch `AGENT_ROLES`), and run Phase 8b for that agent alone (the constitution
offer — the skill itself is already installed by step 3). Verify with the agent's own `doctor`.

```bash
mkdir -p <skill-dir>
bash shared-memory/scripts/bootstrap_tokens.sh --add <agent> --install-path <skill-dir>/.env
bash shared-memory/scripts/sync_skills.sh
systemctl --user restart hive-mind-gateway.service
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
