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

### Before Phase 0 — Obtain the source

Every phase below runs commands inside this repository, the interview included — Q2 asks you to
check paths in the compose file. So the checkout comes first. You are almost certainly reading this
file from inside one already: if `shared-memory/scripts/install_framework.sh` resolves from your
working directory, you are done here — say so and move on.

```bash
git clone https://github.com/KanenasInGreece/Shared_Memory.git && cd Shared_Memory
```

The alternative matters for one reason: a release tarball needs no root, and `git` may. Where git is
not installed, cloning means finding an admin; unpacking a tag archive does not. Take the current tag
from the [releases page](https://github.com/KanenasInGreece/Shared_Memory/releases/latest) — and note
where it lands, because GitHub names the directory for the tag minus the leading `v`:

```bash
curl -L https://github.com/KanenasInGreece/Shared_Memory/archive/refs/tags/vX.Y.Z.tar.gz | tar xz
cd Shared_Memory-X.Y.Z
```

That buys a user without sudo the run up to Phase 2, where they can read exactly which steps still
need one, instead of being stopped before Phase 1. It does not make the install rootless — Phase 3's
Docker packages need root either way.

It costs something later, though: the documented upgrade path is `git pull`, so a tarball install
starts fine and cannot be maintained the documented way. Settle that with the user now — install git,
or agree that upgrades mean fetching a fresh tarball by hand. Say which; Phase 2 needs the answer,
because preflight reports the missing `git` as a failure and that is the one ✗ it may pass.

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
  waits), Q3b defaults to Q2's pair-wise choice if left unanswered (accepting "cpu" for both writes
  nothing extra — exactly today's behavior, never a blocker), Q5 agents can be added later (see the
  *Add an agent later* runbook), Q7 is optional from day one. When the user defers, say exactly what
  will work in the meantime and what won't.
- **"Can we pick this up after?" — yes, always.** Every phase is idempotent and ends with a check,
  so setup resumes cleanly from wherever it stopped: re-run the phase checks top to bottom and
  continue from the first failure. Offer to write a one-line note of where you stopped so the next
  session (yours or another agent's) resumes without re-interviewing.

| # | Ask the user | Fills |
|---|---|---|
| 1 | Where should database data live on disk? [`~/databases/neo4j`, `~/databases/postgres`] | `NEO4J_HOST_DIR`, `PG_DATA_DIR` |
| 2 | Use the **bundled embedder + reranker containers** (recommended; started by compose, CPU-only by default), or an existing endpoint? If bundled: which folder holds your GGUF model files? **Does this host have a spare GPU you want the encoder pair to use instead?** (Phase 4 has the trade-off — a GPU is otherwise easy to leave silently unused, or double-booked with the reasoning LLM. Q3b below covers moving only ONE of the two.) **If "existing endpoint":** Phase 1 (`install_framework.sh`) writes `EMBEDDER_URL`/`RERANKER_URL` into `.env` as explicit lines carrying the bundled compose's own defaults regardless — edit those two lines to the real endpoint after Phase 1 runs, rather than leaving them pointing at a container you never start. | `LLM_MODELS_DIR`; if GPU: `GPU_ENCODER_REPLICAS`/`CPU_ENCODER_REPLICAS`, `GPU_RENDER_GID` |
| 3 | Where is your **reasoning LLM** served? Any OpenAI-compatible endpoint works (LM Studio, llama.cpp server, etc.) [`http://localhost:5000`]. More than one backend, local or remote? List them all. **Does any of them need an API credential** (a paid cloud endpoint, e.g. DeepSeek/xAI/OpenRouter)? If so, ask only for the **name** of the env var they'll export it under — never the key itself. | default `:5000` route, `LLM_BACKENDS`, or `LLM_BACKENDS_JSON` |
| 3b | Only if Q2 put the encoders on the bundled compose stack: **which encoder(s), if any, go on this host's GPU** — embedder, reranker, both, or neither (stay on CPU)? This is a SPLIT of Q2's pair-wise choice, not a repeat of it — Q2 already asked bundled-vs-existing-endpoint. Measured guidance to give: on a small (~4 GB) card the **embedder** fits comfortably (671 MB VRAM measured) but the **reranker at the 8192-token context window overflows device memory** — the safe default recommendation on a small card is embedder-on-GPU, reranker-on-CPU, not the pair moving together. A larger card can take both (Q2's pair-wise switch already covers that case). | `EMBEDDER_CPU_REPLICAS`/`EMBEDDER_GPU_REPLICAS`/`RERANKER_CPU_REPLICAS`/`RERANKER_GPU_REPLICAS` (all four, always — never only the moved encoder's); `GPU_RENDER_GID` too, auto-derived, when either answer is gpu |
| 4 | Which model family is it? Gemma → `DREAM_TEMPERATURE=0.6`; Mistral-3 Instruct / Qwen → `0.1`; Mistral-3 Reasoning → `1.0`; DeepSeek (online) → `0.6`, **with thinking disabled** via the backend entry's `extra_body` — in thinking mode DeepSeek silently ignores temperature. **Does this model reason/think** (emit a `reasoning_content`/thinking block before its answer)? If the provider implements a suppression switch (DeepSeek's `{"thinking":{"type":"disabled"}}`, or the equivalent `enable_thinking:false` shape), set it on that backend's `extra_body` — `check_config.py` renders whatever suppression state is declared, per backend. If left on: `postflight.sh`'s A8 check still passes on a reasoning-only response (a real completion crossed the join even with `content` empty), but the daemons' own JSON extraction never asked for the reasoning tokens, so a metered backend burns output tokens on them for nothing. This is advice, not a mechanism the framework enforces — the operator's call, stated once here. | `DREAM_TEMPERATURE` |
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
The script also refuses any password of 8 characters or fewer (including empty) and re-prompts —
`openssl rand -hex 20` is always well over that, so this never bites the flow below — and if stdin
runs out before a valid password is given (nothing left to feed it, not just a short answer) it fails
loudly with a nonzero exit instead of writing a blank one.
**Skip this phase entirely if `shared-memory/.env` already exists** (resuming a stopped setup) —
re-running the script would hit its own overwrite prompt instead of the directory prompts, which is
not what a resume wants.

```bash
NEO4J_DIR=<from Q1>                      # e.g. $HOME/databases/neo4j
PG_DIR=<from Q1>                         # e.g. $HOME/databases/postgres
MODELS_DIR=<from Q2, blank if using LM Studio>
EMB_DEV=<from Q3b, "cpu" or "gpu">       # blank/Enter also means "cpu"
RER_DEV=<from Q3b, "cpu" or "gpu">       # blank/Enter also means "cpu"
RENDER_GID=<from Q3b, only if either above is "gpu"> # blank/Enter accepts the
                                          # auto-detected value when the host
                                          # has one (stat -c '%g' /dev/dri/renderD128)
NEO4J_PW="$(openssl rand -hex 20)"
PG_PW="$(openssl rand -hex 20)"
# 8 answers for the dirs/encoder-devices/render-gid/passwords, then "n" to
# each of the two TRAILING prompts (systemd service install, LLM-backend
# helper) — Phase 7 and the LLM_BACKENDS / LLM_BACKENDS_JSON edit below
# handle those explicitly, with more context than the script's own generic
# prompt gives. All three encoder prompts are ALWAYS asked (unconditionally,
# same fixed-length sequence every time) — answering "cpu"/"cpu" (or leaving
# both blank) reproduces today's pair-wise-only behaviour exactly and writes
# nothing extra to .env; RENDER_GID is asked either way too but only written
# when a GPU was actually chosen for at least one encoder. printf is a shell
# builtin, so none of this — including the passwords — ever appears on a
# process's own argv.
printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\nn\nn\n' "$NEO4J_DIR" "$PG_DIR" "$MODELS_DIR" "$EMB_DEV" "$RER_DEV" "$RENDER_GID" "$NEO4J_PW" "$PG_PW" \
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
or Q3b turned up a GPU for the encoders**, uncomment/set `GPU_ENCODER_REPLICAS=1` +
`CPU_ENCODER_REPLICAS=0` for Q2's pair-wise choice (Phase 4 has the trade-off) — Q3b's per-service
split, if driven via the printf above, already wrote its own `*_REPLICAS` vars and `GPU_RENDER_GID`
automatically, so this is a confirm-and-backstop step for that path, not a first-time need. Either
way, `GPU_RENDER_GID` should hold the actual value — read it with
`stat -c '%g' /dev/dri/renderD128` rather than trusting the packaged default: on Debian the render
node's group is `render` (gid 992 there, measured on a fresh Debian 13 install), not `video`. If the
user's reasoning server **validates model names** (a named-model server, a routing proxy, a hosted
OpenAI-compatible endpoint, or a desktop app with several models loaded), also set `LLM_MODEL` to the
real id — the shipped default only suits servers that ignore the field. A single backend on a
non-default port is `LLM_DEFAULT_TARGET`. All framework and helper tooling reads `shared-memory/.env`
first, with a repo-root `.env` honoured as a pre-0.6 fallback.

**If Q3 turned up a backend needing a credential, use `LLM_BACKENDS_JSON` instead of `LLM_BACKENDS`.** This applies to a LOCAL backend behind a token (a llama-server on the LAN or tailnet) exactly as to a cloud API — same `token_env`, same key file under `~/.shared-memory/creds/<name>` (mode 600); tell the operator where to put the file and never ask for its contents. Plaintext `http` to a private address is accepted; to a public one the entry is excluded unless the operator sets `"plaintext_ok": true` (ops/README, "Reasoning-LLM backends"). The complete numbered walkthrough (encrypted store → `LoadCredential=` or a `<VAR_NAME>_FILE` runtime pointer → JSON entry with `token_env` plus the mandatory `private_ok`/`roles` choice → restart → verify on `/health`) and the full per-entry parameter table both live in `shared-memory/ops/README.md`, "Reasoning-LLM backends" — **follow them verbatim rather than improvising**; `.env.example` carries the short form beside `LLM_BACKENDS_JSON`. Three rules they encode: the literal key never goes in any file this framework writes — only the env-var **name**; the key at rest belongs in an encrypted store (`pass`/GPG/`systemd-creds`), with **`LoadCredential=` or a runtime `<VAR_NAME>_FILE`** (SEC-06, PR A4) preferred over `systemctl --user import-environment`, which is deprecated (readable by any same-uid process via `show-environment`, and inherited by every user unit); and a credentialed entry with neither `roles` nor an explicit `private_ok` is never selected under default-deny (safe by construction, but loudly warned about at startup and by `check_config.py`) — ask the operator which they want; never pick for them.

### Phase 2 — Preflight

```bash
bash shared-memory/scripts/preflight.sh
```

Verifies Docker + compose v2, `uv`, `git` and a populated `.env`; warns on low RAM/disk (16 GB RAM and ~30 GB disk are the common floor; a GPU is optional — the three measured example configurations are README §3). Resolve every ✗ before continuing.

**One ✗ may be accepted, and only one.** A host that took the tarball route *because* it has no `git` will not have it, and
preflight reports that as a failure — correctly, because `git pull` is the upgrade path. It does not
block the install. If the user has agreed to upgrade by fetching a fresh tarball, note the agreement
and continue past that single ✗; otherwise install git first. Nothing else on the Required list is
negotiable, and do not extend this to any other ✗.

It also checks the smaller tools the shipped scripts run. `curl`, `python3` and `timeout` are
failures: all three sit on postflight's verification path and postflight guards none of them, so
without one an install cannot be proven.

Under *Recommended* it reports the four that only backup and restore need — `gzip`, `gunzip`,
`sha256sum`, `flock` — and `node`, which no framework script runs at all. Node is there because the
agents that consume the skill are node programs and `mcp/mcp.json` launches two servers with `npx`;
ignore that line on a gateway-only host.

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

**Sudo for an agent-driven install:** several steps here need root (package install, inotify, dir
ownership, one Docker-group membership). The default, and the safer path, is to hand the user each
command below and let them type it themselves in their own terminal — nothing needs a standing
grant when the operator is present to answer a password prompt a handful of times. Reach for a
temporary sudo grant only when the install is genuinely unattended and the agent must drive Phase 3
itself; even then, scope the grant to the commands actually needed rather than to the account.

**Which steps actually need root**, enumerated so a scoped grant can cover exactly these instead of
discovering them piecemeal mid-install:
- Before Phase 0 — installing `git`, only if you clone and the host has none (`apt install git` /
  `dnf install git`, per `preflight.sh`'s own remedy); the tarball route avoids it.
- Phase 1, only as a fallback — `install_framework.sh` chowns Neo4j's `import`/`plugins` dirs to
  uid 7474 itself, falling back to a rootless `docker run … chown` if plain `chown` fails, and only
  printing `sudo chown -R 7474:7474 "$NEO4J_HOST_DIR"/{import,plugins}` for the human to run if
  neither works.
- Phase 3 — Docker's own repository bootstrap and the package install itself (the exact commands
  are in the grant script below, since they differ by distro).
- Phase 3 — `systemctl enable --now docker`, `usermod -aG docker $USER` (group membership needs a
  fresh login/shell to take effect — do not expect it to apply to the CURRENT shell). Treat that
  membership as one step from full root: anyone who can reach the docker socket can mount the host
  filesystem into a container and read or write it as root. This project does not currently support
  a rootless-Docker alternative, so there is no narrower option to recommend here.
- Phase 3 — raising inotify limits (`sysctl`, per README §4).
- Phase 3, only if `docker.io` was ever installed on this host — `apt purge docker-buildx`,
  `dpkg --configure -a` (the recovery two paragraphs up).
- Phase 7, only as a fallback — `install_service.sh` tries `loginctl enable-linger` for the current
  user first, which works without root on most systemd-logind setups, and reaches for `sudo` only if
  that is refused.

Everything else in this file — `docker compose`, `docker exec`, the Python/bash helper scripts, the
skill itself — runs as the ordinary user; do not `sudo` anything not on this list.

**A scoped grant, derived on the target host rather than guessed.** Identify the distro from
`/etc/os-release` (`ID`, falling back to `ID_LIKE`) — this repo tests Debian, Ubuntu and Fedora, so
anything else stops rather than guessing a package manager or a repository URL. Resolve every
binary the grant needs, by name, before building anything, so a missing tool fails as itself rather
than as a downstream `visudo` parse error — and resolve each one under a PATH with the sbin
directories prepended, not the invoking user's bare `PATH`: every one of these runs as root once
`sudo` invokes it, and root's PATH includes sbin, so looking them up any other way asks the wrong
question (Debian keeps sbin off a normal user's `PATH` entirely; `usermod`/`sysctl`/`visudo` are
invisible to plain `command -v` there even though they're on disk). Save this as a script and run
it on the target host, from a real terminal (an agent session has no TTY for the sudo password
prompt), from the repo root:

```bash
#!/usr/bin/env bash
set -euo pipefail

[[ -r /etc/os-release ]] || { echo "cannot identify this host: /etc/os-release is missing" >&2; exit 1; }
. /etc/os-release
DISTRO=""
case "$ID" in
  ubuntu) DISTRO=ubuntu ;; debian) DISTRO=debian ;; fedora) DISTRO=fedora ;;
  *) case " ${ID_LIKE:-} " in
       *" ubuntu "*) DISTRO=ubuntu ;; *" debian "*) DISTRO=debian ;; *" fedora "*) DISTRO=fedora ;;
     esac ;;
esac
[[ -n "$DISTRO" ]] || { echo "unrecognized distro (ID=${ID:-unset}) — hand the user each root command instead" >&2; exit 1; }

COMMON_BINS="systemctl usermod tee sysctl chown loginctl visudo"
case "$DISTRO" in
  debian|ubuntu) DISTRO_BINS="apt install curl chmod dpkg" ;;
  fedora)        DISTRO_BINS="dnf" ;;
esac
for b in $COMMON_BINS $DISTRO_BINS; do
  path="$(PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH" command -v "$b")" || \
    { echo "required tool not found: $b" >&2; exit 1; }
  printf -v "BIN_${b^^}" '%s' "$path"
done

NEO4J_HOST_DIR="$(grep '^NEO4J_HOST_DIR=' shared-memory/.env | cut -d= -f2- || true)"
[[ -n "$NEO4J_HOST_DIR" ]] || { echo "NEO4J_HOST_DIR not in shared-memory/.env — run Phase 1 first" >&2; exit 1; }
GRANT_USER="$(id -un)"
STAGE="$(mktemp -d)"; chmod 700 "$STAGE"           # private dir — no predictable /tmp path to race
SUDOERS_FILE="$STAGE/99-shared-memory-temp"

{
  echo "# Shared Memory Framework: temporary install grant, created $(date -Is) by $GRANT_USER"
  echo "# for an agent-driven install of this repo ($DISTRO). Remove once Phase 3 (and the"
  echo "# Phase 7 linger fallback, if it fired) are done:"
  echo "#   sudo rm /etc/sudoers.d/99-shared-memory-temp"
  echo

  if [[ "$DISTRO" == fedora ]]; then
    echo "Cmnd_Alias SM_INSTALL_PKGS = $BIN_DNF config-manager addrepo --from-repofile https\\://download.docker.com/linux/fedora/docker-ce.repo, \\"
    echo "    $BIN_DNF install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin, $BIN_DNF install git"
  else
    echo "Cmnd_Alias SM_INSTALL_PKGS = $BIN_APT update, $BIN_APT install ca-certificates curl, \\"
    echo "    $BIN_INSTALL -m 0755 -d /etc/apt/keyrings, \\"
    echo "    $BIN_CURL -fsSL https\\://download.docker.com/linux/$DISTRO/gpg -o /etc/apt/keyrings/docker.asc, \\"
    echo "    $BIN_CHMOD a+r /etc/apt/keyrings/docker.asc, \\"
    echo "    $BIN_TEE /etc/apt/sources.list.d/docker.sources, \\"
    echo "    $BIN_APT install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin, \\"
    echo "    $BIN_APT install git, $BIN_APT purge docker-buildx, $BIN_DPKG --configure -a"
  fi

  echo "Cmnd_Alias SM_DOCKER_SETUP = $BIN_SYSTEMCTL enable --now docker, $BIN_USERMOD -aG docker $GRANT_USER"
  echo "Cmnd_Alias SM_INOTIFY = $BIN_TEE /etc/sysctl.d/90-inotify.conf, \\"
  echo "    $BIN_TEE -a /etc/sysctl.d/90-inotify.conf, $BIN_SYSCTL -p /etc/sysctl.d/90-inotify.conf"
  echo "Cmnd_Alias SM_NEO4J_CHOWN = $BIN_CHOWN -R 7474\\:7474 $NEO4J_HOST_DIR/import $NEO4J_HOST_DIR/plugins"
  echo "Cmnd_Alias SM_LINGER = $BIN_LOGINCTL enable-linger $GRANT_USER"
  echo "$GRANT_USER ALL=(root) NOPASSWD: SM_INSTALL_PKGS, SM_DOCKER_SETUP, SM_INOTIFY, SM_NEO4J_CHOWN, SM_LINGER"
} > "$SUDOERS_FILE"

"$BIN_VISUDO" -cf "$SUDOERS_FILE" && \
  sudo install -m 0440 -o root -g root "$SUDOERS_FILE" /etc/sudoers.d/99-shared-memory-temp
rm -rf "$STAGE"
```

A literal `:` in a sudoers command argument (a URL's `https://`, `chown`'s `uid:gid`) must be
backslash-escaped or `visudo` rejects the whole file with a syntax error — both are escaped above,
confirmed by feeding the generated output to `visudo -cf` on each of the three `DISTRO` branches.
`visudo -cf` only checks grammar, not that a referenced binary exists, so the per-name `command -v`
loop above is the actual defense against a wrong or missing path, not the validation step — verified
by removing each required binary from `PATH` in turn and confirming the script names that binary
specifically, rather than surfacing a `visudo` column-offset error days later.

The Debian, Ubuntu and Fedora command sequences above are Docker's currently-documented ones per
distro (<https://docs.docker.com/engine/install/>, re-fetched 2026-08-23) — Ubuntu and Debian share
every command except the GPG key's path segment (`.../linux/ubuntu/gpg` vs `.../linux/debian/gpg`,
handled by `$DISTRO` above), and Fedora's own form has already changed once (from a plain
`--add-repo` flag to `config-manager addrepo --from-repofile`), so re-diff against that URL if a
fresh install stalls on a step here that no longer matches. Both `apt install` and `dnf install` are
run exactly as documented, with no `-y`; that means they prompt for confirmation, which an
unattended agent must supply itself (e.g. pipe `yes`) — the grant makes the command reachable
without a password, not non-interactive.

Don't oversell what this buys: `SM_INSTALL_PKGS` is a root shell in disguise regardless of which
package names it's scoped to — installing anything runs that package's own maintainer scripts as
root. `SM_DOCKER_SETUP` and `SM_INOTIFY` are one step from root, not narrow — docker-group
membership is root via the socket, and the `tee`/`sysctl -p` pair can set *any* kernel parameter
through that one file, not just the two inotify limits. `SM_NEO4J_CHOWN` and `SM_LINGER` are the
narrow tier, though `SM_NEO4J_CHOWN` is an arbitrary-ownership-change primitive bounded to uid/gid
7474, not a guarantee it only ever touches those two directories — `chown -R` follows symlinks
placed under them and retargets whatever those point to.

Remove the grant once Phase 3 (and the Phase 7 linger fallback, if it was used) are done — the
header comment repeats this command so the file is self-identifying even without this doc:

```bash
sudo rm /etc/sudoers.d/99-shared-memory-temp
```

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
where, or saves are refused. ⚠ **If the operator is serving the encoders with vLLM, the reranker
needs `shared-memory/scripts/rerank_shim.py` in front of it** and `RERANKER_URL` points at the
shim, not at vLLM — vLLM answers on `/v1/rerank` while the gateway posts `/v1/reranking`. The
embedder needs no shim. Runbook: *Serve the encoders with vLLM* below.

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

### Phase 6 — Mint remote and registry agent tokens

```bash
bash shared-memory/scripts/bootstrap_tokens.sh
```

Appends `AGENT_TOKENS` (digest form) and a read-only `AGENT_ROLES` line (which pre-declares
`monitor:read`, so the dashboard is confined the moment it is ever registered) to the framework
`.env`. **This phase mints only remote and registry identities — `lm_studio` (takes its
token from `mcp.json`'s own env block, never a skill `.env`) and `antigravity` (ambiguous between
`~/.gemini/skills/` and a Claude-family path, deliberately left unguessed).** Each local CLI agent
(`claude`, `codex`, `gemini`, `grok`) is REFUSED here, loudly, by name — its skill directory does
not exist yet, because Phase 8 (which installs it) has not run — and that refusal is **expected,
not an error**: nothing needs fixing at this phase for those agents; Phase 8 mints each of them
right after installing its package (`decision:1473`, grounded on `fact:1472`). An earlier version
of this file minted here for every agent, before Phase 8 had created a single skill directory —
on a genuinely fresh host that refused all four local agents and left `AGENT_TOKENS=` empty, which
the gateway parses identically to auth being deliberately off (`.env.example`'s S-05 note); an
operator following the documented order ended up with an unauthenticated gateway without ever
being told so.

A REMOTE agent's token still needs `--reveal <name>` passed to `bootstrap_tokens.sh` itself on
this SAME invocation (a later, separate reveal mints a fresh token for every agent — a full
rotation, not a free peek). ⚠ **The reveal invocation is the one command the HUMAN runs in their
own terminal, never you** — the script prints the raw token, and an agent transcript turns "shown
once" into "stored forever" (the script's own warning; verified the hard way — a token revealed
through an agent session had to be rotated). Hand the user the exact command line and step back.
One distinct token per agent, never shared. The script refuses to overwrite an existing registry;
`--force` rotates **all** tokens (destructive — rule 2).

⛔ **`monitor` is NOT minted here, deliberately.** It used to be on the default roster, which meant
every fresh install registered a `monitor` digest whose plaintext was discarded at birth — the
dashboard lives in a sibling repo, so it has no install path to write through to, and this bare
invocation carries no `--reveal`. That is exactly what D19 forbids ("never mint a token into a
digest registry that nobody actually received, which is worse than not minting at all"), and
`--add` then refused it as already registered. Mint it **only if the operator wants the dashboard**,
on demand, with the reveal on the same invocation — again, a command the **human** runs:

```bash
bash shared-memory/scripts/bootstrap_tokens.sh --add monitor --reveal monitor
```

Its `monitor:read` confinement is applied on that path too (`READ_ONLY_AGENTS` is authoritative on
every mint path — see the runbook below).

### Phase 7 — Start the gateway and verify

Smoke-test in the foreground first:

```bash
uv run --no-project --with-requirements requirements-gateway.lock \
  python shared-memory/scripts/hive_mind_proxy.py 8888
curl -s http://localhost:8888/health
```

**Dependency pinning is the default — say so when you start the gateway.** The lock pins the
gateway's dependencies to the exact tested versions (the shipped systemd unit below runs from the
same lock, and `git pull` advances it with the code). Tell the user that is what you did; if they
prefer latest-at-invocation resolution instead, the equivalent unpinned form is
`uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair python …` —
their call, not yours. `requirements-gateway.lock` is deliberately narrower than
`requirements.txt` (the gateway process must not carry `psycopg2`) — never substitute the full
`requirements.lock` here.

**Read the JSON SHAPE of that one anonymous call — never the HTTP status, and do not wait until a
local agent has a token to run this check.** `/health` answers HTTP 200 in BOTH states below, and
even a REJECTED bearer token still gets back the anonymous shape at 200 — the gateway serves the
anonymous payload rather than refusing, so **status codes never distinguish "auth configured" from
"auth off"; only the payload shape does.** Two, and only two, shapes are possible from the bare
curl above:

- **Exactly three keys** — `{"status","version","api_version"}`, no `auth_required` field present
  at all — means **auth IS configured**: something (Phase 6's remote/registry mint, or an earlier
  `--add`) already minted at least one token, and every richer field is now gated behind a bearer.
  This is the state to want once any agent is registered, and it no longer depends on Phase 8
  having run — Phase 6 alone gets you here on any install that mints `lm_studio`/`antigravity`.
  Once a specific credential exists (a Phase 6 `--reveal`, or after Phase 8 mints a
  local agent), confirm THAT credential actually authenticates:
  `curl -s -H "Authorization: Bearer <token>" http://localhost:8888/health` must come back with
  `"auth_required":true` plus the full payload (`"embedder":"ok"`, `"daemon":"running"`,
  `"rem_daemon":"running"`; `"llm":"down"` only blocks dreaming, not saves/search — check the
  reasoning LLM from Q3). If nothing has been minted yet at this point in the sequence, note that
  and come back to this authenticated check after Phase 8. ⚠ **If Q3's backends have not been
  configured yet at this point in the sequence, expect `dependencies.llm_pool.state:"degraded"`**
  (W2, decision:1832) — a declared-nothing install now reads `degraded`, not `ok`, because nothing
  was declared and the implicit fallback (`LLM_DEFAULT_TARGET`) is what answered instead; this is
  expected and clears once backends are configured (`bash shared-memory/ops/install_llm_backends.sh`),
  not a fault in this Phase.
- **The full payload already, from the bare curl, unauthenticated** — dozens of keys, INCLUDING
  `"auth_required":false` spelled out in the JSON itself — means **auth is OFF**: no token has
  ever been minted (or `bootstrap_tokens.sh` was never run), and every caller who can reach `:8888`
  gets the complete operational payload, forever, with no token able to restore the slimming later
  (`resolve_identity()` has nothing to match against an empty registry). This is a legitimate,
  deliberate choice for a single-operator box with no other network path to `:8888` — but it is a
  **choice**, and the operator must say so explicitly before setup is treated as complete. ⛔ Never
  read "no auth configured" as "auth verified" just because the check came back 200 — if this
  wasn't the intended outcome, run Phase 6 (or `bootstrap_tokens.sh` directly) and restart the
  gateway before moving on.

Then make it survive logout/reboot with the shipped `systemd --user` unit — a terminal-launched gateway dies with its session:

```bash
bash shared-memory/ops/install_service.sh   # substitutes the repo path into the unit,
                                            # enables linger, enables + starts the service;
                                            # degrades with a clear message on non-systemd hosts
curl -s http://localhost:8888/health
```

### Phase 8 — Install the skill into each agent

For every **local** agent from Q5 (`claude`, `codex`, `gemini`, `grok`, or `antigravity` on its
legacy `~/.gemini/skills/` path), three steps, in this order (README §10, §10a for remote/laptop
clients) — **mint follows the directory, never the reverse**, because `--add` refuses a directory
that does not exist yet and `sync_skills.sh` only populates a directory the registry already
names, so neither step can bootstrap the other:

```bash
mkdir -p <skill-dir>                                            # e.g. ~/.codex/skills/shared-memory
bash shared-memory/scripts/bootstrap_tokens.sh --add <agent> --install-path <skill-dir>/.env
bash shared-memory/scripts/sync_skills.sh                       # or update_skill.sh from a remote/laptop client
```

**This is where a local agent's token is actually minted** (`decision:1473` — Phase 6 above mints
only remote/registry identities). `--add` mints exactly this one agent's token, write-throughs it
into `<skill-dir>/.env` (mode 600, S-01) as the file's only content so far, and merges its digest
into the gateway `AGENT_TOKENS` line **in place** — every OTHER agent's digest is reproduced
byte-identical, so working through Q5's agents one at a time this way is additive, never a
rotation. `sync_skills.sh` (or `update_skill.sh`, run from inside the skill directory on a
remote/laptop client) then installs the rest of the skill package into the now-existing,
now-registered directory — it MERGES `.env.example`'s defaults into the live `.env` rather than
overwriting it, so the token `--add` just wrote survives. `--add` REFUSES outright if `<agent>` is
already registered — there is no single-agent rotation, only `bootstrap_tokens.sh --force` for
everyone — so if this phase is ever re-run for an agent already fully installed, that refusal is
expected and means nothing to fix; move on to the next agent. **Restart the gateway after
minting** (`systemctl --user restart hive-mind-gateway.service`, or re-run Phase 7's
`install_service.sh`) so it loads the updated `AGENT_TOKENS` before this phase's own end-to-end
check below.

#### Phase 8, MCP variant — an MCP host with a WALLED install directory

An MCP host used to be treated as remote by definition ("no fixed local install path", token
delivered by `--reveal`). That is only true of a host on another machine. A host on THIS machine —
opencode is the exercised example, LM Studio is the other one — gets a **walled install
directory** and is registered exactly like any local agent, with one extra flag. Same three steps,
same order, same reason (mint follows the directory, never the reverse):

```bash
install -d -m 700 <walled-dir>        # e.g. ~/.config/opencode/shared-memory-mcp
bash shared-memory/scripts/bootstrap_tokens.sh --add <agent> --mcp --install-path <walled-dir>/.env
bash shared-memory/scripts/sync_skills.sh
```

⚠ **`--install-path` is a FILE, not a directory** — `<walled-dir>/.env`. The mint splits it into
dirname + basename and writes the leaf; handed the directory itself it has nothing to write.

**What `--mcp` buys.** It records the registry entry as `name:mcp:path` instead of `name:path`, and
that kind is what `sync_skills.sh` reads to decide what to DELIVER. Without it the entry is a CLI
skill install, and sync dumps `SKILL.md` + `memory_bridge.py` into the walled directory — a skill
no MCP host can run, sitting beside a live token. (An entry with no kind stays a CLI skill install
permanently; nothing rewrites an existing line, and no migration is needed.)

**What sync delivers to an `mcp` install — three files, and deliberately not a fourth:**
`vector-skill.py` (the connector), `CONSTITUTION_SNIPPET_MCP.md` (the standing rules as a
marker-delimited block, for Phase 8b) and `system-prompt.md` (the same rules wrapped for an LLM
server's system-prompt field). It then byte-compiles the connector copy, removes the
`__pycache__` that leaves behind, enforces directory 700 / files 600, and probes the gateway's
`/health` for an `api_version` match. ⛔ **Never `mcp.json`** — it is a template full of `YOUR_*`
placeholders and a `/path/to/your/...` repo path; adapt it into the host's own config instead.
The token `.env` is never copied or overwritten, only mode-checked.

**Sync delivers; it never configures the host.** Its output names what is still owed, and who
applies it:

| Host kind | The deliverable that applies | Who applies it |
|---|---|---|
| **Agent host** (its own constitution file: `~/.config/opencode/AGENTS.md`, …) | `CONSTITUTION_SNIPPET_MCP.md` — splice the marker-delimited block | Phase 8b, **ask first**, never silently |
| **LLM server** (a system-prompt field — LM Studio) | `system-prompt.md` — paste into the model's system prompt | the operator, in that host's UI |
| Both | the host's MCP config → the WALLED COPY's `vector-skill.py`, plus `VECTOR_SKILL_ENV` pointing at the walled `.env` | the operator or an installing agent |

⚠ **Name `uv` by ABSOLUTE path in the host's MCP config.** An MCP host spawns its stdio server from
a non-interactive, non-login shell, and the recommended installer puts `uv` under
`$HOME/.local/bin` — a bare `"uv"` in the config simply never starts, reported as a dead MCP
server rather than a PATH problem. `sync_skills.sh` warns when this host's `uv` is profile-only.

⚠ **Restart TWO things, and the gateway is the one that gets forgotten.** The MCP host reads its
environment once, at spawn. And auth is **startup-frozen**: the mint writes the new digest into the
gateway `.env` while the running gateway keeps the old one, so an install reported "done" without
`systemctl --user restart hive-mind-gateway.service` is a 401 on the next session.
*(A full host restart is not always required for the MCP-server side — see the re-mint runbook
below for what "the client re-reads its token" actually means and how narrowly it can be satisfied.)*

⚠ **Already registered?** `--add` refuses a name already in `AGENT_TOKENS` — deliberately, since
there is no silent single-agent rotation. Re-homing an existing identity onto a walled directory is
`--remint <name> --mcp --install-path <walled-dir>/.env`, which write-throughs the new token and
touches nobody else. ⛔ Do NOT reach for `--reveal`: it prints a live token, so it is
operator-only, in the operator's own terminal, never through an agent.

A host on ANOTHER machine is still genuinely remote: it has no local directory here, so it stays
one of Phase 6's registry identities, its token delivered by an operator-run `--reveal`, and a copy
of the `mcp/` folder is placed on that machine by hand (see `mcp/README.md`).

**What "the skill package" is — `shared-memory-skill/shared-memory/MANIFEST.txt` is the authority, not a list in this file.** It currently ships `SKILL.md`, `CONSTITUTION_SNIPPET.md`, `.env.example`, `scripts/memory_bridge.py`, `scripts/update_skill.sh` and `Documentation/schema.md`. Install all of it: two later phases depend on files an "just SKILL.md and the script" install would leave out — Phase 8b copies its block from `CONSTITUTION_SNIPPET.md` *in the skill directory*, and Phase 8c and every future update run `scripts/update_skill.sh` *from there*. The reliable way to get it right is to let the tooling do it: create the directory with `memory_bridge.py` in place, then run `update_skill.sh` (or `sync_skills.sh` on the gateway host), which reads the manifest so a file added to the package later needs no change here.

⛔ **COPY EVERY FILE — NEVER SYMLINK ONE INTO A SOURCE CHECKOUT.** A link is auto-current, and that convenience is not worth what it costs: it binds every agent on the machine to one checkout's path, so moving, renaming or archiving that directory breaks all of them at once, silently, and the first symptom is an agent failing mid-task. Staleness is the lesser risk precisely because it is **detectable** — every file is content-compared on each sync and `doctor` reports version skew. `sync_skills.sh` and `update_skill.sh` both **replace** any symlink they find with a real copy, and `sync_skills.sh` refuses outright to write into an install directory that is itself a link, because that would make the source its own destination.

⚠ **`uv` must be reachable WITHOUT this agent's shell profile, or every invocation of the skill fails silently.** Every command in this phase (and in `SKILL.md`) runs `memory_bridge.py` **through `uv run`** — and the agent almost never spawns the interactive login shell that installed uv. It spawns a non-interactive, non-login shell to exec the command, which reads none of `~/.bashrc` / `~/.profile` and starts with whatever PATH its own parent process handed it. The upstream installer this project recommends (`curl -LsSf https://astral.sh/uv/install.sh | sh`) puts `uv` under `$HOME/.local/bin` and relies on exactly the profile that shell never reads — so a host that followed the recommended install correctly can still leave every agent unable to run the skill, and the agent does not report a broken memory system when this happens: it answers some other way, or saves nothing, and nobody sees why. `preflight.sh` now checks this directly (a warning distinct from its existing "is uv installed at all" check) and `sync_skills.sh` repeats it once per run whenever an agent install actually exists on disk — **re-run `preflight.sh` after installing an agent, not only before**, since this failure mode is per-agent-shell, not per-host. If either warns, either symlink `uv` onto a directory already on the system default PATH (e.g. `sudo ln -s "$(command -v uv)" /usr/local/bin/uv` — keeps the recommended install, just exposes it further) or set `PATH` inside this agent's own configuration to include `uv`'s directory.

Final end-to-end check, as an agent (uses the skill path, exercises auth + embedding + storage).
**Run it from inside a project directory** — the repo checkout itself is fine — because the
client derives the record's project from the working directory; issued from elsewhere (the
skill dir, `$HOME`) the save is refused with `project_required`:

```bash
cd <repo-or-any-project-root>
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py doctor
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py save "install smoke test" '{"source":"<agent>","entities":["SetupTest"],"new_entities":["SetupTest"],"new_project":true}'
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py search "install smoke test" 3
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
(`generate_tokens.py` — Phase 6 seeds the remote/registry identities, Phase 8's `--add` registers
each local agent as its package installs) is the actual roster of installed agents as of v0.9.27 —
an agent added later via `--add` needs no framework release to be supported by THAT mechanism.** This
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

⚠ **An agent wired through MCP takes the OTHER snippet.** For an install registered `--mcp`, the
file to copy from is `CONSTITUTION_SNIPPET_MCP.md` in that install's own walled directory, with
its own marker (`<!-- shared-memory:mcp-constitution-snippet vN -->`). It says the same standing
rules in the vocabulary that agent actually has — MCP tool names, and a role that decides which
writes succeed — because the CLI block's "use the shared memory skill" names something an MCP host
does not have and cannot run. The two markers are distinct, so an agent may only ever hold one.
*(An MCP host that is an LLM SERVER rather than an agent has no constitution file at all: it takes
`system-prompt.md` in the model's system-prompt field instead, and this phase does not apply.)*

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
been offered and fall back to Phase 8b. For an `mcp`-kind install the same check runs against
`CONSTITUTION_SNIPPET_MCP.md` and its own `mcp-constitution-snippet` marker, refreshed by
`sync_skills.sh` rather than `update_skill.sh`.

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
# auth-on installs only: read AGENT_TOKEN from the skill .env — NEVER `. file` (that EXECUTES it) and
# never cat/grep it (fact:1499 — a raw credential must never pass through an agent's own transcript).
# AGENT_ENV below is Claude's path; the same shape works for any agent install — swap in
# ~/.grok/skills/shared-memory/.env, ~/.codex/skills/shared-memory/.env, or
# ~/.gemini/skills/shared-memory/.env for that agent's own skill directory.
AGENT_ENV=${AGENT_ENV:-$HOME/.claude/skills/shared-memory/.env}
AGENT_TOKEN=$(sed -n 's/^AGENT_TOKEN=//p' "$AGENT_ENV" | head -1); export AGENT_TOKEN
bash shared-memory/scripts/postflight.sh
```

## Runbooks

### Add an agent later (no token rotation)

⛔ **A BULK MINT CAN REGISTER A TOKEN NOBODY EVER RECEIVES.** The default roster still mints for
`lm_studio` and `antigravity`, neither of which has a seeded install path — their tokens are
minted, their digests are registered, and nothing is written anywhere. They are **REMOTE**: the only
delivery is `--reveal <name>` **on that same invocation**. Miss it and the `.env` shows an agent that
cannot authenticate, which is worse than an agent that is plainly absent. The mint says so per agent,
in the word **UNDELIVERABLE**, and again in a closing block that names `--remint <name> --reveal
<name>` for each one — the recovery that does not rotate the fleet. *(Until v0.9.36 the report told
you to run `generate_tokens.py --reveal <name>` afterwards. That command is a **full rotation of
every agent**, so the advice printed beside a fresh credential destroyed all the others. And until
this release `monitor` was on that roster too — a dashboard in a sibling repo, undeliverable by
construction, registered on **every** fresh install; it is now minted on demand instead, see
Phase 6.)*

**Re-issuing ONE agent's token — `--remint`.** For an agent that is already registered but whose token
was never delivered (or was lost), this mints a replacement for **that name only**, leaving every other
digest byte-identical:

```bash
bash shared-memory/scripts/bootstrap_tokens.sh --remint lm_studio --reveal lm_studio
# or, when the agent has a local skill directory:
bash shared-memory/scripts/bootstrap_tokens.sh --remint codex --install-path ~/.codex/skills/shared-memory/.env
```

⚠ **`--install-path` is the agent's `.env` FILE, never its directory** — a directory-shaped path
(a trailing slash, or a path naming an existing directory) is refused outright, before anything is
minted or written.

⚠ It **invalidates that agent's current token**, so pair it with `--reveal` or `--install-path` — the
agent must receive the new one. ⚠ **Restart the gateway afterwards: auth is startup-frozen.**
⛔ Run `--reveal` yourself, never through an agent — a transcript turns "shown once" into "stored
forever".

⚠ **A running CLIENT must ALSO re-read its token, separately from the gateway restart above.**
`vector-skill.py`'s MCP server and `memory_bridge.py`'s CLI client each cache their token in memory
once, at import — a re-mint rotates the registered digest immediately, so a process that was
already running keeps presenting the *previous* token until it re-reads the file, and every
request (reads included) 401s until then. What "re-reads" requires differs by shape, measured
against a live MCP conversion: a full host restart is **not** always necessary.
- **MCP agent** — respawn the memory MCP server: a full host restart works, or, if the host can
  reload / disable-then-re-enable one server on its own, that alone is enough.
- **CLI/skill agent** — `memory_bridge.py` reads its token fresh on every process start, so a
  one-shot invocation already picks up the rotation automatically; only a long-running CLI
  session held open across the rotation needs restarting.

Verify with a successful authenticated call (or the gateway audit log) after the respawn/restart;
re-mint again only if the agent still shows the old token afterward.

`bootstrap_tokens.sh` (bare) refuses to touch an existing registry and `--force` rotates
**everyone** — neither is what you want for one new agent. `--add` (v0.9.27) is the purpose-built
additive mint: it registers exactly one new agent's `AGENT_INSTALLS` entry, mints its token,
write-throughs it into that agent's skill `.env` (mode 600), and updates `AGENT_TOKENS` +
`AGENT_INSTALLS` — and `AGENT_ROLES` when the agent needs one — in `shared-memory/.env` **in
place**, every other agent's digest byte-identical, untouched.

⛔ **A READ-ONLY IDENTITY IS ALWAYS MINTED READ-ONLY.** `generate_tokens.py`'s `READ_ONLY_AGENTS`
list is authoritative on **every** mint path: `--add monitor` writes `monitor:read` into
`AGENT_ROLES`, confining that token to `GET /health`, `GET /memory/telemetry` and read-only Cypher
on `POST /memory/graph` — every other route answers 403. Pass `--role read|full|admin` to confine an
agent that is *not* on that list; **widening one that is, is refused before anything is minted.**
Absence from `AGENT_ROLES` means full read/write, so a missing entry is not a neutral default — it
is the widest one. *(Until v0.9.35 only the BULK mint emitted this line, so `--add monitor` produced
a write-capable token for a dashboard that must never have one.)*

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

### Serve the encoders with vLLM (any accelerator vLLM supports)

Optional, and **only when the operator asks for it** — the bundled `llama-server` pair is the
default and stays supported. Do not propose this silently; it changes which engine writes the
vectors (see the warning at the end).

1. **Weights.** vLLM needs Hugging Face format, not GGUF:
   `hf download BAAI/bge-m3 --local-dir <dir>/bge-m3` and
   `hf download BAAI/bge-reranker-v2-m3 --local-dir <dir>/bge-reranker-v2-m3`.
2. **Two containers — a vLLM process serves ONE model.** Start each with `--runner pooling`
   (these are encoders, not generative models), its own port, and `--served-model-name` set.
   On Intel XPU the image is `intel/vllm:0.21.0-xpu`, and it needs **both** `--device /dev/dri`
   **and** `-v /dev/dri/by-path:/dev/dri/by-path` plus `-e CCL_ZE_IPC_EXCHANGE=sockets` — without
   the `by-path` mount oneCCL cannot enumerate the GPU and the engine never starts. On a shared
   card set `--gpu-memory-utilization` low; for a pooling model it is a ceiling, not a
   reservation, so it will not be claimed.
3. **Free the ports the framework expects, or point it elsewhere.** If the bundled pair is
   running, stop it (`CPU_ENCODER_REPLICAS=0` + `GPU_ENCODER_REPLICAS=0`, or stop the units).
4. **Put the shim in front of the reranker** — the embedder does not need one:
   ```bash
   SHIM_VLLM_URL=http://127.0.0.1:<vllm-rerank-port> SHIM_PORT=8092 \
     python3 shared-memory/scripts/rerank_shim.py
   ```
   It rewrites `/v1/reranking` → `/v1/rerank` and nothing else, refuses any other path, and binds
   loopback on purpose (no encoder here carries authentication). For a permanent install give it a
   service unit ordered **before** the gateway, so the gateway's first capability probe succeeds.
5. **Point the framework at both** in `shared-memory/.env` — `EMBEDDER_URL` straight at vLLM,
   `RERANKER_URL` at the **shim** — then restart the gateway (both are read at import).
6. **Verify:** `curl -s localhost:8888/health` → `status ok`; then a real search and confirm every
   row comes back `ranked: true`. A dead reranker degrades silently to vector order; a dead
   embedder makes search return `[]` rather than an error, so check the rows, not just `/health`.

⚠ **Tell the operator before switching the EMBEDDER.** vLLM's vectors are not bit-identical to
llama.cpp's (measured: cosine 0.9982 at full-length inputs, one position swap in 760 pairs), so
records written afterwards sit slightly apart from records written before, and only a full re-embed
puts that back. Switching **only the reranker** carries no such cost — a reranker stores nothing.

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

### Uninstall (tiered; the irreversible levels are gated on a backup)

```bash
bash shared-memory/scripts/uninstall_framework.sh --level service --dry-run   # preview; shows a refusal too
bash shared-memory/scripts/uninstall_framework.sh --level service             # stop gateway, remove skill dirs — reversible; shared-memory/.env is left in place, every declared LLM backend intact
bash shared-memory/scripts/uninstall_framework.sh --level data                # + containers, volumes, data dirs, .env — NOT reversible
bash shared-memory/scripts/uninstall_framework.sh --level all                 # + model weights
```

`--level` is required — there is no default. `data` and `all` refuse to start unless a backup set
exists in `BACKUP_DIR` (`bash shared-memory/ops/backup.sh` first); `--no-backup` is the explicit
opt-out for a disposable host. Ask the operator which level they mean and show the dry run before
the real run. `~/.shared-memory` (backups, audit trail, capacity history) and the checkout are never
removed by the script; it prints the checkout's `rm -rf` for the operator to run themselves.

### Status / health

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" http://localhost:8888/health \
                                              # gateway, daemons, backends, consolidation liveness
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env ps
systemctl --user status hive-mind-gateway.service
journalctl --user -u hive-mind-gateway.service -n 50   # daemon logs
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py status   # telemetry
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

**Auditing the effective config WITHOUT a running gateway** — `check_config.py` renders every
env-overridable default (declared / present-but-empty / inherited), a boolean `has_credential` per
secret (never the value), and — when its daemon dependencies are available — the resolved LLM
backend roster plus whether the gateway's own startup guards would refuse to boot. A renderer, not
an enforcer: it calls the gateway's own guard functions rather than re-implementing their logic.

```bash
# Phase A only (env half) — plain python3, no third-party packages needed:
python3 shared-memory/scripts/check_config.py --phase-a-only

# Both phases (needs the daemon deps):
uv run --with aiohttp --with asyncpg --with httpx --with neo4j \
    python3 shared-memory/scripts/check_config.py
```

Exit 0 = readable and the gateway would boot; exit 1 = readable but the gateway will refuse to
start; exit 2 = could not read/render at all (an unreadable-but-present `.env`, or a daemon-side
import crash — an absent `.env` is NOT exit 2, that is a legitimate headless install). ⛔ Not wired
into `preflight.sh` — its 0/1/2 contract differs from preflight's 0/1, and it has nothing useful to
say before Phase 1 has written `shared-memory/.env`. See `shared-memory/ops/README.md` for the full
reference.

### Upgrade (gateway host)

**If this host took the tarball route** (no `git` — see *Before Phase 0*), `git pull` will fail here
because there is no repository. Fetch the new tag's tarball, unpack it beside the old tree and carry
`shared-memory/.env` across, then run everything below from the new directory. Record which upgrade
route this host uses as a comment at the top of `shared-memory/.env`, so the next agent reading only
this section is not left running `git pull` in a directory that was never a checkout.

**If this host is on a DETACHED HEAD**, step 0 refuses rather than trying to `git pull` with no branch
to update — most often reached by running `git checkout <tag>` right after cloning, a defensible
reading of "install the release" that this repo does not actually want: the release branch is `main`
(tags mark a point on it; they are not meant to be checked out and left). Recover with
`git checkout main`, then re-run. Deliberately want a specific pinned tag instead of the moving branch?
Check that tag out instead, or use the tarball route above.

**Use the script. It is the procedure.**

```bash
# Same non-executing read as Phase 9 above — never `. file`, never cat/grep the .env (fact:1499).
# AGENT_ENV is Claude's path; swap in ~/.grok/, ~/.codex/, or ~/.gemini/skills/shared-memory/.env
# for that agent's own install.
AGENT_ENV=${AGENT_ENV:-$HOME/.claude/skills/shared-memory/.env}
AGENT_TOKEN=$(sed -n 's/^AGENT_TOKEN=//p' "$AGENT_ENV" | head -1); export AGENT_TOKEN  # postflight needs it, or A1/A5/A8 skip and it exits 1
bash shared-memory/scripts/update_framework.sh --dry-run   # see every step, run nothing
bash shared-memory/scripts/update_framework.sh             # do it, and prove it
bash shared-memory/scripts/update_framework.sh --domain-backfill  # also run step 8 (opt-in; skipped by default)
bash shared-memory/scripts/update_framework.sh --skip-env-migration  # skip steps 0 & 3 below; see there
```

**After the script finishes, treat a stack-drift verdict as a step, not an aside.**
`update_framework.sh` never touches the containers by design — it only runs
`reconcile_stack.sh --dry-run` (read-only) and reports the verdict on its own closing banner
(the words "stack reconcile REQUIRED" appear there when drift is present). An operating agent
should:

1. **Read the drift report** the update just printed, or run
   `bash shared-memory/scripts/reconcile_stack.sh --dry-run` yourself — read-only, changes nothing.
2. **If any row reads `DRIFT`, STOP and ASK the operator** — show them the drift table and the exact
   command (`bash shared-memory/scripts/reconcile_stack.sh`) rather than running it yourself. It
   recreates the database containers; that is the operator's call, never an agent's to make alone.
3. **Run `bash shared-memory/scripts/reconcile_stack.sh` only on the operator's explicit word** —
   never silently as part of "update the framework"; it is a separate, standalone step (see the
   *Reconcile the stack to the shipped pins* runbook below). A `floating` row (a pinned tag with no
   version in it, e.g. today's llama.cpp images) is never something to reconcile — there is no pin
   to reconcile it to, and it must never be read as drift.

**After a restore, the same script finishes the job** — `ops/restore.sh` brings the data back at
whatever schema level the dump was taken at, and nothing has yet moved it forward to this code:

```bash
bash shared-memory/scripts/update_framework.sh --from-restore
```

⭐ **Upgrade and restore are ONE procedure with two entry points.** An upgrade is new *code* arriving
at existing *data*; a restore is existing *data* arriving at running *code*. Everything between is
identical, which is why `--from-restore` is a flag and not a second script: it skips fetching code
(restore has just supplied the data instead) and skips the pre-migration backup (the dump you just
restored **is** the safeguard set). Every guard below applies to both.

⭐ **THE DATABASE STATES ITS OWN LEVEL — never read a version out of a backup manifest.**
`schema_migrations` is a table *inside* the database, and `pg_dump -Fc` carries it, so a restored
database announces exactly how far it got. A version stamped into the manifest would be a **derived**
value and a second source of truth that can drift from the schema it claims to describe.

⛔ **Forward-only, and now enforced.** `apply.py` **refuses with exit 3** when the database's ledger
names migrations this checkout does not contain — the restore-onto-older-code case. Before this it
reported `Up to date at <a filename this code has never seen>` and exited 0, because selection is by
position and a database twelve releases ahead produces an empty pending list, indistinguishable from
one that is finished. `apply.py --status` reports the same state without refusing. **The fix is
always to update the CHECKOUT; the schema cannot be moved backwards.**

**What the script runs, in this order, each step gated on the last succeeding:**

| # | Step | Why it is here |
|---|---|---|
| 0 | capture pre-upgrade effective config (`migrate_env.py --capture-preimage`) | **upgrade path only**, immediately before the pull — the OLD checkout's own copy, so the pre/post equality below is a construction, not an assumption. Skippable with `--skip-env-migration` (skips this AND step 3) |
| 1 | `git pull --ff-only` | skipped with `--from-restore`. Refuses a **detached HEAD** and refuses a tarball tree, rather than failing in a way that reads as broken tooling |
| 2 | `ops/backup.sh` | migration is the one step nothing can undo. Uses the shipped script so it is the same artifact `restore.sh` can read |
| 3 | migrate `.env` to explicit configuration (`migrate_env.py --apply`) | runs on **both** the upgrade and restore paths; uses step 0's pre-image when it ran, else self-captures with the current loader. See *Migrate `.env` to explicit LLM routing config* below |
| 4 | `apply.py` | Postgres, ledger-driven, forward-only (exit 3 = database ahead) |
| 5 | `verify_neo4j_init.py --apply` | **Neo4j has no ledger** — this is the graph's entire forward-migration |
| 6 | `reconcile_project_identity.py --apply` | graph half of migration 027; no migration can run it |
| 7 | restart + wait for `/health` | the running gateway must BE the migrated code |
| 8 | `backfill_domain_of.py --apply` | **after** the restart — see the guard below; **opt-in** via `--domain-backfill` (skipped by default; `--no-domain-backfill` is a one-release no-op) |
| 9 | `sync_skills.sh` | after the restart, so it cannot print a false incompatibility warning |
| 10 | `postflight.sh` | an update is not complete until this passes |

⚠ **Under `--from-restore`, steps 0 and 1 do not run at all** (no placeholder — the whole
Step-0 block is skipped, exactly as before W3), so every step number above shifts down by 2 on
that path (step 3 becomes the first thing that runs; step 10 (`postflight.sh`) becomes step 8).

⚠ **Step 9 is after step 7 deliberately.** Run before the restart, `update_skill.sh` compares the new
client against the *old* gateway and prints `⚠ Updated to X but still incompatible. The GATEWAY
itself …` — alarming, self-resolving one step later, and observed on two hosts. The ordering removes
the false alarm rather than rewording it.

⚠ **`backfill_domain_of.py` runs AFTER the restart, and that ordering is a guard rather than a
preference.** It enqueues a narrow repair row that only a gateway from v0.8.47 understands; an older
worker does not recognise the row type, falls through to its ordinary fact branch, and blanks the
content of every record it touches. The script refuses to enqueue against a gateway that is too old —
including one it cannot reach, because an unknown version is not permission to write — so running it
early is safe but pointless. It is **dry-run by default**; nothing is enqueued without `--apply`. Since
v0.9.35 the preview belongs to plain `backfill_domain_of.py` (no flag) alone — under it the report
prints and the function returns before anything is enqueued — and a real (`--apply`) run enqueues
**once, with no separate preview step**: the same counts the preview would have shown are printed by
the applied run itself, before it enqueues. The upgrade flow (`update_framework.sh`, step 8 above)
therefore invokes it exactly once, with `--apply`; there is no second invocation to read a preview
from. It is also only needed on a deployment whose records already carry a `domain` in their metadata:
a new install has none, and every save from here on writes its own edge.

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

Clients and gateway may drift; `memory_bridge.py doctor` names which side to upgrade on `api_version` skew. `doctor` also names the token's own `agent`/`role` once a gateway reports them (0.9.54+) — a gateway that genuinely predates that is named `role: not reported (gateway <version> predates 0.9.54)`; a current gateway with no `role` in the reply means THIS token was not accepted, named `role: not reported (token not accepted — anonymous payload)`.

### Migrate `.env` to explicit LLM routing config (W3, `migrate_env.py`)

**Steps 0 and 3 above are this tool.** `Backend_Declaration_Spec_2026-08-30.md` §4 rules that an
upgrade must never change a running install's behaviour — mechanically, not by operator memory —
so before any release stops defaulting an undeclared pool, every existing install's implicit
routing config (a bare `LLM_BACKENDS` CSV line, or nothing declared at all) needs to become the
explicit `LLM_BACKENDS_JSON` form **without changing what actually serves today**. `migrate_env.py`
is that one-time-per-install migration, standalone (`shared-memory/scripts/migrate_env.py`, no
other module imports it):

```bash
migrate_env.py --capture-preimage <out.json>     # capture only (used by update_framework.sh's step 0)
migrate_env.py [--preimage <in.json>] [--apply]  # evaluate / apply — default is a DRY-RUN PREVIEW
```

**What it does, per install, first match wins — GATED on an actual loader-semantics boundary
(Ruling A(a)/V2, W4):** every write this tool can make (`private_ok: true` added to an
already-usable `LLM_BACKENDS_JSON`'s bare entries; a live `LLM_BACKENDS` CSV converted to the JSON
form; the bare `LLM_DEFAULT_TARGET` fallback materialised) exists **only to preserve a pre-W4
install's effective behaviour across the default-deny flip.** It therefore plans a write **only
when the pre-image it is comparing against was captured by OLD (pre-W4) loader code** — i.e. the
boundary between "undeclared defaults to on" and "undeclared defaults to off" is actually crossed.
When the pre-image is already current-generation (which a **self-capture run always is** — pre and
post are both computed by the loader you are currently running), there is no prior effective
behaviour to preserve, so the tool reports "already reflects the CURRENT loader generation... would
be an OPINION, not a preserved behaviour" and **writes nothing** — even for cases that would
otherwise be eligible (an already-usable JSON with bare entries, a live CSV, an undeclared
fallback). Nothing declared at all, WHEN the boundary is genuinely open, is still
**advisory-probed and asks the operator to confirm** — interactively only, with a 60s deadline
(anything but `y` = no write, and it asks again at the next upgrade); non-interactively, with the
boundary open and a materialisation genuinely needed, it now **refuses (`EXIT_STOP`)** rather than
silently leaving the gateway serving nothing (Ruling D(a) V1) — re-run interactively, declare
`LLM_BACKENDS_JSON` yourself, or pass `--skip-env-migration`. Every other population (roles-carrying
entries, a credentialed backend with neither key, any key the systemd unit itself owns, an empty
`LLM_BACKENDS_JSON=`) is **left untouched and reported by name** regardless of boundary state — this
tool refuses to write in every case where a human decision is actually owed. It proves any
boundary-crossing migration changed nothing BEHAVIOURAL by capturing the effective config before
and after and comparing them (urls, weights, credentials-present, roles, fit numbers, effective
`private_ok` under Layer-2 planned-direction rules, both startup-guard verdicts) — any divergence
restores the pre-migration backup and refuses, naming it. **Predicted no-op on an install that is
already fully explicit, or already on the current loader generation** — any write there is a
finding, not the expected case.

**`--skip-env-migration`** skips both steps 0 and 3 for one run — a capture/migration bug must not
block a security update. Coverage for that upgrade is deferred to a manual standalone run (below).

**Standalone / headless run** (no `update_framework.sh` in the loop — e.g. a host where
`shared-memory/.env` does not exist yet, which is the one case `update_framework.sh` itself refuses
before any step at all): run it directly, same dependencies the gateway itself needs —

```bash
uv run --no-project --with-requirements requirements-gateway.lock \
    python shared-memory/scripts/migrate_env.py               # preview (no --preimage: self-captures)
uv run --no-project --with-requirements requirements-gateway.lock \
    python shared-memory/scripts/migrate_env.py --apply       # apply, same self-capture
```

This standalone `--apply` run is safe to run at any time and is idempotent — but on a post-W4
checkout **it self-captures with the CURRENT (post-flip) loader, so pre and post are always the
same generation and the boundary never opens.** It proves the tool is a true no-op on an
already-current install (the §6.4 H9 property test is exactly this, mutation-checked); it does
**not** materialise anything for a host that skipped the flip.

⚠ **The pre-W3 → post-W4 version-jump case (H8, §6.5 — remedy honesty).**
`update_framework.sh` never re-execs after its own `git pull` (git replaces the file, the running
process keeps its old inode), so **the migration step only fires on an upgrade FROM a checkout
that already has it** — a host upgrading directly from before W3 straight to W4 or later runs a
pre-W3 updater with no capture/migrate step at all, and lands on the release that starts refusing
to default an undeclared pool having never migrated. **`migrate_env.py` cannot repair this after
the fact by self-capturing** — a self-capture on the now-running post-W4 checkout is
same-generation by construction (see above), so it plans nothing for exactly the population that
needs it. W4's degraded `/health` reasons therefore do **not** name `migrate_env.py` for this
runtime case — they name `check_config.py` and declaring `LLM_BACKENDS_JSON` explicitly, because a
remedy that will correctly no-op is a false remedy. `migrate_env.py` remains the right tool only
where it can still act: **during the update itself**, when `update_framework.sh` step 0 captures
the pre-image with the OLD checkout BEFORE `git pull` — that pre-image genuinely predates the flip,
so `--apply --preimage <that JSON>` after the pull crosses the boundary correctly. If you know a
host jumped straight from before W3, its update never took that capture — the recovery is to
**declare `LLM_BACKENDS_JSON` explicitly yourself** (see `check_config.py`'s per-backend census and
`shared-memory/ops/README.md`, "Reasoning-LLM backends"), not to invoke `migrate_env.py`
standalone after the fact. **Nothing is lost:** declare the SAME URL(s) the old `LLM_BACKENDS` CSV
(or bare `LLM_DEFAULT_TARGET`) named, adding `"private_ok": true` to each one you want serving
role-less traffic exactly as before — a one-line edit, not a data-losing operation; the corpus, the
credentials, and every other config are unaffected either way.

### Reconcile the stack to the shipped pins

`update_framework.sh` moves code, schema and skills forward but never the containers — a released
image pin (pgvector, neo4j) moving does not, by ruling, recreate anything on its own. That stays a
standalone script the operator runs when they choose, never a step the update path takes for you:
a host may have other legacy problems to work through first, and recreating a database container
is not something to do silently.

```bash
bash shared-memory/scripts/reconcile_stack.sh --dry-run   # table only, changes nothing, exit 2 if drift
bash shared-memory/scripts/reconcile_stack.sh             # shows the table, then asks before reconciling
bash shared-memory/scripts/reconcile_stack.sh --yes       # skips the confirmation prompt
```

**Dry-run first, show the table to the operator, run only on their word.** The table compares the
pinned image in `shared-memory/ops/postgres_neo4j_limits.yaml` against what each running container
was actually created from (`in sync` / `DRIFT` / `floating` — a pinned tag with no version in it,
e.g. today's llama.cpp images, can never be reconciled to a pin and never counts as drift), plus the
Postgres pgvector extension's own SQL-reported version against what the running image carries. On
confirmation it pulls the pinned images, runs `docker compose ... up -d`, waits for Postgres, and
runs `ALTER EXTENSION vector UPDATE` (idempotent). It never edits `.env`, never runs a migration, and
never restarts the gateway — the gateway reconnects to Postgres/Neo4j on its own; check with
`curl -s $GATEWAY_URL/health` (default `http://localhost:8888`).

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
