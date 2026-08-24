#!/usr/bin/env bash
#
# preflight.sh — verify a host is ready to run the Shared Memory gateway stack.
#
# Checks the hard prerequisites (docker, docker compose v2, uv, a populated
# .env) and warns on soft ones (RAM, disk). Read-only — changes nothing.
# Exit 0 when every hard check passes; exit 1 otherwise.
#
#   bash shared-memory/scripts/preflight.sh
#
# Run before `docker compose up` on a fresh gateway host (Quick Start step 1).

set -uo pipefail   # not -e: we run every check and summarise, never abort early

# ⛔ RULING 4: every operator-facing script accepts -h/--help (prints its own
# header, exits 0, does nothing else) and refuses any argument it does not
# recognise — this script previously had no argument parsing at all, so any
# flag (including --help) was silently ignored and the checks ran anyway.
for _arg in "$@"; do
    case "$_arg" in
        -h|--help)
            awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            printf '\033[31m%s\033[0m\n' "✗ unknown argument: $_arg (this script takes none — see --help)" >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Framework env lives at shared-memory/.env; the repo-root path is the pre-0.6
# fallback — same resolution order as the gateway (hive_mind_proxy.py).
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()   { printf '\033[33m%s\033[0m\n' "$*"; }

fail=0
ok()   { grn "  ✓ $*"; }
warn() { ylw "  ! $*"; }
bad()  { red "  ✗ $*"; fail=1; }
# OPERATOR-ACTIONABLE REMEDIATION. The agent running preflight is often not
# permitted to install anything on the host — prerequisites are the operator's
# to place. So every hard failure that a human must fix by installing something
# also records the exact command or source here, reprinted as ONE block at the
# end. A ✗ that only says what is missing leaves the operator to go and find
# out how; this hands it to them.
REMEDIES=()
need() { REMEDIES+=("$*"); }

echo "Shared Memory — preflight checks"
echo

# ── Hard requirements ─────────────────────────────────────────────────────────
echo "Required:"

if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
        ok "docker ($(docker --version | awk '{print $3}' | tr -d ,)) — daemon reachable"
    else
        bad "docker is installed but the daemon is not reachable (start Docker / check permissions)"
    fi
else
    # THE TESTED PATH IS DOCKER'S OWN REPOSITORY, for every distro — that is
    # what our installs run and therefore the only packaging this project can
    # speak for. Distro packages are named only as a fallback FACT, with their
    # provenance, never as the recommendation: Fedora's own repos carry
    # moby-engine + docker-compose (measured on a Fedora 43 install, fact:1399)
    # and Debian ships compose v2 under the legacy name `docker-compose`
    # (measured on Debian 13). Neither is what we test against.
    if command -v dnf >/dev/null 2>&1; then
        bad "docker not found — install Docker Engine + Compose v2 from Docker's own repository (the tested path): https://docs.docker.com/engine/install/fedora/ — then sudo systemctl enable --now docker and add your user to the docker group. Fedora's own moby-engine + docker-compose also provide 'docker compose' v2, but that is not the packaging we test."
        need "Docker Engine + Compose v2, from Docker's repo: https://docs.docker.com/engine/install/fedora/ then: sudo systemctl enable --now docker && sudo usermod -aG docker \$USER"
    elif command -v apt-get >/dev/null 2>&1; then
        bad "docker not found — install Docker Engine + Compose v2 from Docker's own repository (the tested path): https://docs.docker.com/engine/install/debian/ (or .../ubuntu/) — then sudo systemctl enable --now docker and add your user to the docker group. If docker.io was EVER installed here, purge docker-buildx too: it owns /usr/libexec/docker/cli-plugins/docker-buildx and blocks Docker's docker-buildx-plugin with a dpkg overwrite conflict that leaves the daemon disabled while docker --version still answers."
        need "Docker Engine + Compose v2, from Docker's repo: https://docs.docker.com/engine/install/debian/ then: sudo systemctl enable --now docker && sudo usermod -aG docker \$USER  (if docker.io was ever installed: sudo apt purge docker-buildx first)"
    else
        bad "docker not found — install Docker Engine + Compose v2 from Docker's own repository (the tested path): https://docs.docker.com/engine/install/"
        need "Docker Engine + Compose v2, from Docker's repo: https://docs.docker.com/engine/install/"
    fi
fi

if docker compose version >/dev/null 2>&1; then
    ok "docker compose ($(docker compose version --short 2>/dev/null))"
else
    bad "docker compose v2 not found (the 'docker compose' subcommand) — the standalone docker-compose binary is NOT a substitute; the scripts call the subcommand"
    need "Compose v2 plugin: install docker-compose-plugin from Docker's repo (https://docs.docker.com/engine/install/) — verify with: docker compose version"
fi

if command -v uv >/dev/null 2>&1; then
    ok "uv ($(uv --version | awk '{print $2}'))"

    # ── Is uv reachable WITHOUT the operator's shell profile? ─────────────────
    #
    # The check just above answers "can the OPERATOR run uv" — it runs in
    # whatever shell invoked preflight.sh, almost always an interactive login
    # shell that has already sourced ~/.bashrc / ~/.profile. Every agent that
    # spawns uv instead runs it through a NON-interactive, NON-login shell (a
    # CLI harness execs a command; it does not open a terminal), and that kind
    # of shell reads none of those files — it starts with whatever PATH its own
    # parent process handed it, nothing more.
    #
    # The recommended install two lines above — curl -LsSf
    # https://astral.sh/uv/install.sh | sh, the upstream installer and the ONLY
    # path this project tests against; a distro package is not something this
    # project can speak for — places uv under $HOME/.local/bin and relies on
    # the shell profile to put that directory on PATH. So on a fresh host that
    # followed this exact recommendation correctly, uv ends up on the
    # operator's PATH and invisible to everything else. That is the EXPECTED
    # RESULT of the documented install, not a misconfiguration — and it fails
    # completely silently: an agent that cannot run uv does not report a
    # broken memory system, it answers some other way (or saves nothing) and
    # nobody sees why.
    #
    # What is actually knowable here, and no more: whether uv resolves with NO
    # profile in effect at all. `env -i` clears the entire environment (not
    # just PATH) so no inherited variable can smuggle a profile's PATH edit
    # back in; the reference path is `getconf PATH`, the platform's own
    # compiled-in default search path — the closest thing to "what a shell has
    # before anything user-specific runs" that any POSIX host can answer, and
    # asking it costs nothing (getconf ships with the C library; it is never
    # uv or python — see the note above `need()` for why that matters here).
    # This cannot know any particular AGENT's own PATH — a framework may set
    # one of its own — so it is worded as what was actually measured, never as
    # a verdict on a specific agent.
    sys_path="$(getconf PATH 2>/dev/null)"
    if [[ -z "$sys_path" ]]; then
        : # getconf unavailable — nothing measured, so nothing claimed either way
    elif env -i PATH="$sys_path" sh -c 'command -v uv' >/dev/null 2>&1; then
        ok "uv also resolves on the system default PATH ($sys_path) — reachable from a profile-free shell"
    else
        warn "uv resolves ONLY when your shell profile is loaded — it is NOT on the system default PATH ($sys_path). This is the normal outcome of the install recommended above, not a misconfiguration: the upstream installer puts uv under \$HOME/.local/bin and counts on your profile to expose it. Any AGENT that spawns a non-interactive, non-login shell to run this skill will be UNABLE to find uv — and the failure is SILENT, the agent answers some other way (or saves nothing) instead of reporting a broken memory system. Fix EITHER (keeps the upstream installer either way): symlink uv onto a directory already on the system default PATH, e.g. sudo ln -s \"\$(command -v uv)\" /usr/local/bin/uv — or set PATH inside that agent's OWN configuration to include uv's directory."
        need "uv is reachable only via your shell profile, not the system default PATH — a non-login agent shell cannot find it. Either: sudo ln -s \"\$(command -v uv)\" /usr/local/bin/uv   (exposes the existing upstream install system-wide, no reinstall)   or add uv's directory to PATH in the affected agent's own configuration."
    fi
else
    bad "uv not found — install from https://docs.astral.sh/uv/ (user-local, no root needed)"
    need "uv (user-local, no root): curl -LsSf https://astral.sh/uv/install.sh | sh — then ensure \$HOME/.local/bin is on PATH, including for systemd units"
fi

if command -v git >/dev/null 2>&1; then
    ok "git ($(git --version | awk '{print $3}'))"
else
    bad "git not found — needed to obtain and update this checkout"
    need "git: your distro's package is fine (apt install git / dnf install git)"
fi

# Tools the shipped scripts actually execute, beyond docker/uv/git above.
# curl, python3 and timeout all sit on postflight.sh's verification path and it
# guards none of them, so without any one an install cannot be proven. (Their
# roles differ — curl makes the gateway calls, python3 parses the responses,
# timeout bounds the bridge probes — so do not collapse them into one claim.) The feature-scoped ones are
# checked further down, under Recommended, where an optional finding belongs.
# (jq is not here because its only consumer, ops/install_llm_backends.sh, is an
# optional install helper — not because that script self-checks. Several do:
# backup.sh and restore.sh self-check python3 and sha256sum too, and both are
# checked anyway. The discriminator is whether the CONSUMER is on the path to a
# working install, never whether the script guards itself.)
for _tool in curl python3 timeout; do
    if command -v "$_tool" >/dev/null 2>&1; then
        ok "$_tool"
    else
        case "$_tool" in
          curl)    bad "curl not found — postflight.sh verifies the install through it and ops/backup.sh drives the gateway with it (update_skill.sh too); nothing here can be verified without it" ;;
          python3) bad "python3 not found — postflight.sh, ops/backup.sh and ops/restore.sh call it directly, so uv's own interpreter does not satisfy this" ;;
          timeout) bad "timeout not found — postflight.sh wraps its memory_bridge.py probes in it and guards it nowhere, so each probe returns 127 with empty output and postflight reports a parse failure that reads like a slow gateway" ;;
        esac
        need "$_tool: your distro's package (apt install $_tool / dnf install $_tool)"
    fi
done

# Read one key from .env without sourcing it — values may contain spaces or
# other characters bash `source` would mis-parse (e.g. PROJECT_ALIASES).
#
# Normalises the raw grep/cut output the same way an operator's editor (or a
# CRLF-saving one) or a trailing inline comment would leave it, so the value
# this script COMPARES matches the value docker compose actually resolves —
# not a stricter, unquoted, no-comment ideal of it. Three real spellings,
# each of which renders CORRECTLY through compose but previously failed
# preflight's double-start guard with a raw string compare (H1, PR #308
# review, reproduced against the real compose file):
#   EMBEDDER_CPU_REPLICAS="0"          (matched double quotes)
#   EMBEDDER_CPU_REPLICAS=0 # keep off (trailing inline comment)
#   EMBEDDER_CPU_REPLICAS=0\r          (CRLF-saved .env)
# Order matters: strip the CR first (it would otherwise hide inside the
# trailing-comment/quote match), then the inline comment, then one layer of
# MATCHED surrounding quotes — mismatched or partial quoting is left as-is
# rather than guessed at.
read_env() {
    local raw
    raw="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)"
    raw="${raw%$'\r'}"
    raw="$(printf '%s' "$raw" | sed -E 's/[[:space:]]+#.*$//')"
    if [[ ${#raw} -ge 2 && "${raw:0:1}" == '"' && "${raw: -1}" == '"' ]]; then
        raw="${raw:1:${#raw}-2}"
    elif [[ ${#raw} -ge 2 && "${raw:0:1}" == "'" && "${raw: -1}" == "'" ]]; then
        raw="${raw:1:${#raw}-2}"
    fi
    printf '%s' "$raw"
}

if [[ -f "$ENV_FILE" ]]; then
    ok ".env present ($ENV_FILE)"
    [[ -n "$(read_env PG_PASSWORD)"    ]] && ok "PG_PASSWORD set"    || bad "PG_PASSWORD empty in .env"
    [[ -n "$(read_env NEO4J_PASSWORD)" ]] && ok "NEO4J_PASSWORD set" || bad "NEO4J_PASSWORD empty in .env"
else
    bad ".env not found — run: bash shared-memory/scripts/install_framework.sh  (or copy shared-memory/.env.example → shared-memory/.env and fill it in)"
fi

# Encoder model files — an inference container with a wrong/missing model path
# is the most common `unhealthy` in Phase 4 (AGENTS.md), and it is checkable
# now: the .env names the dir and the compose defaults name the subpaths.
#
# EFFECTIVE replicas — mirrors postgres_neo4j_limits.yaml's own nested default
# (${EMBEDDER_GPU_REPLICAS:-${GPU_ENCODER_REPLICAS:-0}}) in bash, so this
# script's picture of "what will actually start" matches what `docker compose
# up` will actually do, per-service override included, not just the pair-wise
# knobs a per-service install may have moved past.
if [[ -f "$ENV_FILE" ]]; then
    cpu_reps="$(read_env CPU_ENCODER_REPLICAS)"; cpu_reps="${cpu_reps:-1}"
    gpu_reps="$(read_env GPU_ENCODER_REPLICAS)"; gpu_reps="${gpu_reps:-0}"
    emb_cpu="$(read_env EMBEDDER_CPU_REPLICAS)";   emb_cpu="${emb_cpu:-$cpu_reps}"
    emb_gpu="$(read_env EMBEDDER_GPU_REPLICAS)";   emb_gpu="${emb_gpu:-$gpu_reps}"
    rer_cpu="$(read_env RERANKER_CPU_REPLICAS)";   rer_cpu="${rer_cpu:-$cpu_reps}"
    rer_gpu="$(read_env RERANKER_GPU_REPLICAS)";   rer_gpu="${rer_gpu:-$gpu_reps}"

    # Double-start guard: the CPU and GPU variant of the SAME encoder bind the
    # SAME port (8070 for both retrievers, 8071 for both rerankers) — compose
    # itself fails loudly on the second bind when this happens, but that
    # failure surfaces mid-`up`, after Postgres/Neo4j are already starting.
    # Catching it here, before anything starts, is louder and earlier.
    #
    # A value that is not a plain non-negative integer AFTER normalising
    # cannot be safely compared — warn and SKIP this encoder's verdict rather
    # than guess: compose itself already fails loudly on a bad `replicas:`
    # value at `up` time (verified: a non-integer/duplicate-name value is
    # rejected at `config` time), so silence here is not a missed guard.
    _is_int() { [[ "$1" =~ ^[0-9]+$ ]]; }
    emb_numeric=1
    if ! _is_int "$emb_cpu"; then warn "EMBEDDER_CPU_REPLICAS resolved to '$emb_cpu', not a plain integer — skipping the embedder double-start check (compose will fail loudly on this value)"; emb_numeric=0; fi
    if ! _is_int "$emb_gpu"; then warn "EMBEDDER_GPU_REPLICAS resolved to '$emb_gpu', not a plain integer — skipping the embedder double-start check (compose will fail loudly on this value)"; emb_numeric=0; fi
    if [[ "$emb_numeric" == "1" && "$emb_cpu" != "0" && "$emb_gpu" != "0" ]]; then
        bad "embedder would double-start: EMBEDDER_CPU_REPLICAS=$emb_cpu AND EMBEDDER_GPU_REPLICAS=$emb_gpu both resolve non-zero — both bind :8070; set exactly one to 0"
    fi
    rer_numeric=1
    if ! _is_int "$rer_cpu"; then warn "RERANKER_CPU_REPLICAS resolved to '$rer_cpu', not a plain integer — skipping the reranker double-start check (compose will fail loudly on this value)"; rer_numeric=0; fi
    if ! _is_int "$rer_gpu"; then warn "RERANKER_GPU_REPLICAS resolved to '$rer_gpu', not a plain integer — skipping the reranker double-start check (compose will fail loudly on this value)"; rer_numeric=0; fi
    if [[ "$rer_numeric" == "1" && "$rer_cpu" != "0" && "$rer_gpu" != "0" ]]; then
        bad "reranker would double-start: RERANKER_CPU_REPLICAS=$rer_cpu AND RERANKER_GPU_REPLICAS=$rer_gpu both resolve non-zero — both bind :8071; set exactly one to 0"
    fi

    # M4 ruling (PR #308 review, operator-adjudicated): there is no
    # EMBEDDER_DEVICE/RERANKER_DEVICE var to cross-check against the
    # replicas — it was a persisted derived value (decision:1032) whose
    # only purpose was surviving this exact drift check, and install_
    # framework.sh no longer writes it. The double-start guard above is the
    # only encoder-config verdict this section reaches.

    if [[ "$emb_cpu" != "0" || "$emb_gpu" != "0" || "$rer_cpu" != "0" || "$rer_gpu" != "0" ]]; then
        models_dir="$(read_env LLM_MODELS_DIR)"
        embed_sub="$(read_env EMBED_MODEL_SUBPATH)"
        embed_sub="${embed_sub:-gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf}"
        rerank_sub="$(read_env RERANK_MODEL_SUBPATH)"
        rerank_sub="${rerank_sub:-gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf}"
        if [[ -f "$models_dir/$embed_sub" && -f "$models_dir/$rerank_sub" ]]; then
            ok "encoder GGUFs present under LLM_MODELS_DIR"
        else
            bad "encoder GGUF(s) missing under LLM_MODELS_DIR ($models_dir) — download commands are in shared-memory/.env.example (or set both *_ENCODER_REPLICAS=0 and point EMBEDDER_URL/RERANKER_URL elsewhere)"
        fi
    fi

    # Neo4j data-dir writability for the container user. The neo4j image drops
    # to uid 7474 and demands WRITE access to its mounted dirs; its entrypoint
    # (running as root) chowns /data and /logs for you but NOT /import and
    # /plugins, so dirs created by an ordinary `mkdir -p` (user-owned, 0755)
    # crash-loop the container on "/import is not accessible" — measured on a
    # fresh Fedora install. Host ancestor permissions do NOT matter: the
    # daemon mounts as root (verified: uid 7474 reads a bind mount through a
    # 0700 home). What matters is the mounted dirs' own ownership/mode.
    neo4j_dir="$(read_env NEO4J_HOST_DIR)"
    if [[ -n "$neo4j_dir" && -d "$neo4j_dir" ]]; then
        unwritable=""
        for sub in import plugins; do
            d="$neo4j_dir/$sub"
            [[ -d "$d" ]] || continue
            read -r owner perm < <(stat -c '%u %a' "$d" 2>/dev/null) || continue
            owner_w=$(( (10#${perm:0:1} & 2) != 0 ))
            world_w=$(( (10#${perm: -1} & 2) != 0 ))
            if ! { [[ "$owner" == "7474" && "$owner_w" == "1" ]] || [[ "$world_w" == "1" ]]; }; then
                unwritable="$unwritable $d"
            fi
        done
        if [[ -n "$unwritable" ]]; then
            bad "Neo4j dirs not writable by the container user (uid 7474):$unwritable — run: sudo chown -R 7474:7474 $neo4j_dir/{data,logs,import,plugins}   (data/logs the image fixes itself; import/plugins it does not)"
        else
            ok "Neo4j data dirs writable by the container user"
        fi
    fi
fi

# ── Soft requirements (warnings only) ─────────────────────────────────────────
echo
echo "Recommended:"

# Neo4j checks configured heap max + pagecache (shipped defaults: 2G + 2G)
# against physical RAM at startup and refuses to boot when they exceed it —
# so a very small host is a HARD failure unless the .env overrides are set,
# not a soft "you may be slow" warning. Measured on a 3.2 GB host: shipped
# defaults refuse; the .env.example small-host preset runs.
# MemTotal is what the kernel was LEFT, not what is fitted: firmware and
# integrated graphics reserve some first, so a nominally-16 GB host reports 15.
# Thresholds below are therefore set one GB under each nominal figure — a
# machine that meets the recommendation must be able to PASS the check for it
# (measured: 16 GB host, MemTotal 15 GB, previously warned forever).
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
neo4j_heap_override="$(read_env NEO4J_HEAP_MAX)"
if [[ "$mem_gb" -ge 15 ]]; then
    ok "RAM ${mem_gb} GB (meets the 16 GB recommendation)"
elif [[ "$mem_gb" -ge 7 ]]; then
    warn "RAM ${mem_gb} GB — 16 GB recommended (measured example configurations: README §3)"
elif [[ "$mem_gb" -gt 0 && -n "$neo4j_heap_override" ]]; then
    warn "RAM ${mem_gb} GB with small-host Neo4j override (NEO4J_HEAP_MAX=$neo4j_heap_override) — expect reduced capacity; 8 GB is the no-override floor, 16 GB recommended"
elif [[ "$mem_gb" -gt 4 ]]; then
    warn "RAM ${mem_gb} GB — below the ~8 GB no-override floor; the full stack will not fit at the shipped Neo4j defaults. Set the small-host preset (NEO4J_HEAP_INITIAL/NEO4J_HEAP_MAX/NEO4J_PAGECACHE in shared-memory/.env — see .env.example)"
elif [[ "$mem_gb" -gt 0 ]]; then
    bad "RAM ${mem_gb} GB — the shipped Neo4j memory defaults (heap 2G + pagecache 2G) exceed physical RAM and Neo4j will refuse to start. Set the small-host preset in shared-memory/.env (see .env.example) and re-run"
fi

# THE FILESYSTEM THAT FILLS IS DOCKER'S, NOT THE REPO'S. Images, volumes and
# both databases live under the docker data-root; the checkout holds source.
# They are frequently different mounts — Debian's default LVM layout gives /var
# ~11 GB while /home gets the rest, so measuring the repo reported hundreds of
# free GB while the filesystem about to fill had eleven (measured, Debian 13).
avail_gb() { df -BG --output=avail "$1" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0; }
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
[[ -d "$docker_root" ]] || docker_root=/var/lib/docker
[[ -d "$docker_root" ]] || docker_root="$REPO_ROOT"

disk_gb=$(avail_gb "$docker_root")
[[ -n "$disk_gb" ]] || disk_gb=0
if [[ "$disk_gb" -ge 30 ]]; then
    ok "Disk ${disk_gb} GB free on $docker_root (>= 30 GB)"
elif [[ "$disk_gb" -gt 0 ]]; then
    warn "Disk ${disk_gb} GB free on $docker_root — ~30 GB recommended there (images + volumes + both databases land on THIS filesystem, not the checkout's). Move docker's data-root to a larger filesystem, or grow this one."
fi

# The checkout's own filesystem matters too (GGUFs commonly sit near it), but
# only report it when it is a DIFFERENT mount — otherwise it is the same number.
repo_fs=$(df --output=target "$REPO_ROOT" 2>/dev/null | tail -1)
docker_fs=$(df --output=target "$docker_root" 2>/dev/null | tail -1)
if [[ -n "$repo_fs" && "$repo_fs" != "$docker_fs" ]]; then
    repo_gb=$(avail_gb "$REPO_ROOT")
    [[ "$repo_gb" -ge 10 ]] \
        && ok "Disk ${repo_gb} GB free on $repo_fs (checkout + GGUFs)" \
        || warn "Disk ${repo_gb} GB free on $repo_fs — the checkout and model GGUFs live here"
fi

# PROBE IT, DO NOT ASSERT IT. `command -v nvtop` says a binary exists; it says
# nothing about whether that binary can see a GPU. Measured on Debian 13: the
# packaged nvtop links no libdrm backends and dlopens them at runtime, so
# without libdrm-amdgpu1 it answers "No GPU to monitor" — as root too, so it
# does not even look like a permission problem — while preflight cheerfully
# reported GPU-aware dreaming as enabled.
if ! command -v "${NVTOP_BIN:-nvtop}" >/dev/null 2>&1; then
    warn "nvtop not found — REM/NREM fall back to the time-based quiesce guard (optional)"
elif nvtop_out=$("${NVTOP_BIN:-nvtop}" -s 2>/dev/null) && [[ "$nvtop_out" == *device_name* ]]; then
    if [[ "$nvtop_out" == *mem_total* ]]; then
        ok "nvtop sees a GPU and reports memory (GPU-aware dreaming enabled)"
    else
        warn "nvtop sees a GPU but reports NO memory fields — this build is too old for VRAM-aware checks (measured: 3.2.0 has no mem_total, 3.3.2 does). GPU-aware dreaming still works."
    fi
else
    warn "nvtop is installed but sees NO GPU — GPU-aware dreaming is inert. On AMD this is usually a missing libdrm-amdgpu1 (nvtop dlopens it); it normally arrives with Mesa, which a container-encoder host does not otherwise need. Verify with: ${NVTOP_BIN:-nvtop} -s"
    need "libdrm for your GPU vendor, so nvtop can see it (AMD: libdrm-amdgpu1) — then confirm '${NVTOP_BIN:-nvtop} -s' lists a device"
fi

# Backup and restore stand on four small tools, and the gateway needs none of
# them. Each line names what stops working so "proceed anyway" is a choice
# rather than a surprise on the day a restore is actually needed. All four are
# hard dependencies of ops/backup.sh and ops/restore.sh: those scripts die on a
# missing sha256sum by their own check, and simply fail mid-run on the others.
for _tool in gzip gunzip sha256sum flock; do
    if command -v "$_tool" >/dev/null 2>&1; then
        ok "$_tool"
    else
        case "$_tool" in
          gzip)      warn "gzip not found — ops/backup.sh writes the Neo4j dump through it and ops/restore.sh verifies it with 'gzip -t'; backup and restore will not run. Everything else works." ;;
          gunzip)    warn "gunzip not found — ops/restore.sh decompresses the Neo4j dump with it; a backup can still be TAKEN, but not restored on this host." ;;
          sha256sum) warn "sha256sum not found — ops/backup.sh and ops/restore.sh both die on its absence by their own check; it is what proves a dump was not corrupted or swapped." ;;
          flock)     warn "flock not found — ops/backup.sh takes its 'another backup is already running' lock with it; backups lose their concurrency guard." ;;
        esac
        need "$_tool (optional): needed by ops/backup.sh and ops/restore.sh only"
    fi
done

# No framework or helper script runs node — the things that do are the agents
# and the MCP host, and mcp/mcp.json's two example servers launch through npx.
# So a gateway-only host needs none of it, while a host that will also run an
# agent or that MCP config needs all of it, and the operator should learn which
# they have here rather than at Phase 8. Same failure shape as uv above: the
# upstream installer at https://nodejs.org/en/download lands user-local, and an
# agent spawns profile-free shells that never read the profile exposing it.
if command -v node >/dev/null 2>&1; then
    _sys_path_n="$(getconf PATH 2>/dev/null || echo /usr/bin:/bin)"
    if env -i PATH="$_sys_path_n" sh -c 'command -v node' >/dev/null 2>&1; then
        ok "node ($(node --version 2>/dev/null)) — also on the system default PATH"
    else
        warn "node ($(node --version 2>/dev/null)) resolves only via your shell profile, not the system default PATH ($_sys_path_n). Agents that spawn profile-free shells will not find it — the same shape as the uv warning above. Harmless if this host only runs the gateway."
    fi
elif command -v npm >/dev/null 2>&1; then
    warn "npm present but node is not on PATH — an agent host needs both"
else
    warn "node not found — no framework script needs it, but the agents that consume this skill do, and mcp/mcp.json launches two servers with npx. Install per https://nodejs.org/en/download if this host will run an agent or that MCP config; ignore this line on a gateway-only host."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
if [[ "$fail" -eq 0 ]]; then
    grn "Preflight passed. Next: docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d"
else
    red "Preflight failed — resolve the ✗ items above, then re-run."
    if [[ ${#REMEDIES[@]} -gt 0 ]]; then
        echo
        ylw "Hand this to whoever administers the host — preflight never installs anything:"
        for r in "${REMEDIES[@]}"; do printf '  • %s\n' "$r"; done
    fi
fi
exit "$fail"
