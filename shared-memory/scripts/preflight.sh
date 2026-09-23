#!/usr/bin/env bash
#
# preflight.sh — verify a host is ready to run the Shared Memory gateway stack.
#
# Checks hard prerequisites (docker, docker compose v2, uv, a populated .env) and warns on soft ones (RAM, disk). Read-only. Exit 0 when every hard check passes; exit 1 otherwise.
#
#   bash shared-memory/scripts/preflight.sh
#
# Run before `docker compose up` on a fresh gateway host.

set -uo pipefail   # not -e: we run every check and summarise, never abort early

# --help prints this header and exits. Any other argument is refused, because this script used to ignore flags and run the checks anyway.
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
# shared-memory/.env first, then the pre-0.6 repo-root file, matching the gateway.
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()   { printf '\033[33m%s\033[0m\n' "$*"; }

fail=0
ok()   { grn "  ✓ $*"; }
warn() { ylw "  ! $*"; }
bad()  { red "  ✗ $*"; fail=1; }
# Record the install command for each hard failure. The agent running preflight often cannot install, and a bare "missing" leaves the operator to find the fix.
REMEDIES=()
need() { REMEDIES+=("$*"); }

echo "Shared Memory — preflight checks"
echo

echo "Required:"

if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
        ok "docker ($(docker --version | awk '{print $3}' | tr -d ,)) — daemon reachable"
    else
        bad "docker is installed but the daemon is not reachable (start Docker / check permissions)"
    fi
else
    # Recommend Docker's own repository, the only packaging this project tests. Distro packages are a fallback only: Fedora's repos carry moby-engine + docker-compose (fact:1399), and Debian ships compose v2 as docker-compose.
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

    # The check above is the operator's login shell. Agents spawn uv with no profile, and the documented installer only puts ~/.local/bin on PATH via that profile, so a correct install is still invisible to them.
    # env -i plus getconf PATH asks only whether uv resolves with no profile at all. It cannot see one agent's own PATH, so the warning reports that measurement and nothing more.
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

# curl, python3, and timeout sit on postflight.sh and it guards none of them, so a missing one means the install cannot be proved. jq stays under Recommended because only the optional LLM installer uses it.
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

# Read one key without sourcing .env. The shared parser does not do bash quote-matching.
read_env() { python3 "$SCRIPT_DIR/read_env_key.py" "$ENV_FILE" "$1"; }

if [[ -f "$ENV_FILE" ]]; then
    ok ".env present ($ENV_FILE)"
    [[ -n "$(read_env PG_PASSWORD)"    ]] && ok "PG_PASSWORD set"    || bad "PG_PASSWORD empty in .env"
    [[ -n "$(read_env NEO4J_PASSWORD)" ]] && ok "NEO4J_PASSWORD set" || bad "NEO4J_PASSWORD empty in .env"
else
    bad ".env not found — run: bash shared-memory/scripts/install_framework.sh  (or copy shared-memory/.env.example → shared-memory/.env and fill it in)"
fi

# A missing GGUF is the usual Phase 4 unhealthy, and the .env plus compose defaults name the path now. Replica defaults follow the compose nested chain, including a per-service override, so this matches what `docker compose up` will start.
if [[ -f "$ENV_FILE" ]]; then
    cpu_reps="$(read_env CPU_ENCODER_REPLICAS)"; cpu_reps="${cpu_reps:-1}"
    gpu_reps="$(read_env GPU_ENCODER_REPLICAS)"; gpu_reps="${gpu_reps:-0}"
    emb_cpu="$(read_env EMBEDDER_CPU_REPLICAS)";   emb_cpu="${emb_cpu:-$cpu_reps}"
    emb_gpu="$(read_env EMBEDDER_GPU_REPLICAS)";   emb_gpu="${emb_gpu:-$gpu_reps}"
    rer_cpu="$(read_env RERANKER_CPU_REPLICAS)";   rer_cpu="${rer_cpu:-$cpu_reps}"
    rer_gpu="$(read_env RERANKER_GPU_REPLICAS)";   rer_gpu="${rer_gpu:-$gpu_reps}"

    # CPU and GPU variants of one encoder bind the same port, and compose only fails that after Postgres and Neo4j have started. A non-integer count is skipped rather than guessed, because compose already rejects it.
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

    # There is no EMBEDDER_DEVICE or RERANKER_DEVICE to cross-check. It was a persisted derived value (decision:1032), and install_framework.sh no longer writes it.

    # Require a GGUF only for an encoder that will actually start. A host that runs the other encoder remotely must not fail on the unused file.
    need_embed=0
    need_rerank=0
    if [[ "$emb_cpu" != "0" || "$emb_gpu" != "0" ]]; then need_embed=1; fi
    if [[ "$rer_cpu" != "0" || "$rer_gpu" != "0" ]]; then need_rerank=1; fi
    if [[ "$need_embed" == "1" || "$need_rerank" == "1" ]]; then
        models_dir="$(read_env LLM_MODELS_DIR)"
        embed_sub="$(read_env EMBED_MODEL_SUBPATH)"
        embed_sub="${embed_sub:-gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf}"
        rerank_sub="$(read_env RERANK_MODEL_SUBPATH)"
        rerank_sub="${rerank_sub:-gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf}"
        gguf_missing=0
        if [[ "$need_embed" == "1" && ! -f "$models_dir/$embed_sub" ]]; then
            bad "embedder GGUF missing under LLM_MODELS_DIR ($models_dir/$embed_sub) — download commands are in shared-memory/.env.example (or set EMBEDDER_*_REPLICAS=0 and point EMBEDDER_URL elsewhere)"
            gguf_missing=1
        fi
        if [[ "$need_rerank" == "1" && ! -f "$models_dir/$rerank_sub" ]]; then
            bad "reranker GGUF missing under LLM_MODELS_DIR ($models_dir/$rerank_sub) — download commands are in shared-memory/.env.example (or set RERANKER_*_REPLICAS=0 and point RERANKER_URL elsewhere)"
            gguf_missing=1
        fi
        if [[ "$gguf_missing" == "0" ]]; then
            ok "encoder GGUFs present under LLM_MODELS_DIR"
        fi
    else
        ctx_tokens="$(read_env EMBED_MAX_CONTEXT_TOKENS)"
        ctx_tokens="${ctx_tokens:-8192}"
        warn "all encoder replicas are 0 (remote encoders): remote must serve context window ($ctx_tokens tokens, EMBED_MAX_CONTEXT_TOKENS) via --max-model-len or -c; postflight A9 will gate"
    fi

    # The neo4j image runs as uid 7474 and chowns data and logs, but not import or plugins. A user-owned 0755 mkdir crash-loops those two, and only the mounted dirs' own mode matters.
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

# Warnings only. A miss here does not fail preflight.
echo
echo "Recommended:"

# Neo4j refuses to boot when heap plus pagecache exceed RAM, so a small host is a hard failure unless the .env override is set. MemTotal is what the kernel was left, so the 16 GB recommendation passes at 15.
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
neo4j_heap_override=""
[[ -f "$ENV_FILE" ]] && neo4j_heap_override="$(read_env NEO4J_HEAP_MAX)"
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

# Images, volumes, and both databases land on Docker's data-root, which is often not the checkout's filesystem. Measuring the repo hides a small /var.
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

# Report the checkout's free space only when it is a different mount from Docker's. Otherwise it is the same number.
repo_fs=$(df --output=target "$REPO_ROOT" 2>/dev/null | tail -1)
docker_fs=$(df --output=target "$docker_root" 2>/dev/null | tail -1)
if [[ -n "$repo_fs" && "$repo_fs" != "$docker_fs" ]]; then
    repo_gb=$(avail_gb "$REPO_ROOT")
    [[ "$repo_gb" -ge 10 ]] \
        && ok "Disk ${repo_gb} GB free on $repo_fs (checkout + GGUFs)" \
        || warn "Disk ${repo_gb} GB free on $repo_fs — the checkout and model GGUFs live here"
fi

# `command -v nvtop` only proves a binary exists. Packaged nvtop dlopens libdrm at runtime, so a missing backend reports no GPU even as root.
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

# The gateway needs none of these four. Each warning names what backup or restore loses, because those scripts die on a missing sha256sum and fail mid-run on the others.
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

# No framework or helper script runs node. Agents and mcp/mcp.json's npx servers do, and a user-local install is invisible to a profile-free agent shell, same as uv.
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
