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
    # Fedora/RHEL ship podman, not docker — and the helper scripts
    # (init_db.sh, ops/backup.sh) call the docker CLI, so name the packages
    # that actually provide it there (verified: Fedora's own repos carry
    # moby-engine + docker-compose, and the latter provides `docker compose` v2).
    if command -v dnf >/dev/null 2>&1; then
        bad "docker not found — this distro ships podman; install real docker: sudo dnf install moby-engine docker-compose && sudo systemctl enable --now docker (then add your user to the docker group)"
    else
        bad "docker not found — install Docker Engine + Compose"
    fi
fi

if docker compose version >/dev/null 2>&1; then
    ok "docker compose ($(docker compose version --short 2>/dev/null))"
else
    bad "docker compose v2 not found (the 'docker compose' subcommand)"
fi

if command -v uv >/dev/null 2>&1; then
    ok "uv ($(uv --version | awk '{print $2}'))"
else
    bad "uv not found — install from https://docs.astral.sh/uv/"
fi

# Read one key from .env without sourcing it — values may contain spaces or
# other characters bash `source` would mis-parse (e.g. PROJECT_ALIASES).
read_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-; }

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
if [[ -f "$ENV_FILE" ]]; then
    cpu_reps="$(read_env CPU_ENCODER_REPLICAS)"; cpu_reps="${cpu_reps:-1}"
    gpu_reps="$(read_env GPU_ENCODER_REPLICAS)"; gpu_reps="${gpu_reps:-0}"
    if [[ "$cpu_reps" != "0" || "$gpu_reps" != "0" ]]; then
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
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
neo4j_heap_override="$(read_env NEO4J_HEAP_MAX)"
if [[ "$mem_gb" -ge 16 ]]; then
    ok "RAM ${mem_gb} GB (>= 16 GB)"
elif [[ "$mem_gb" -ge 8 ]]; then
    warn "RAM ${mem_gb} GB — 16 GB recommended (measured example configurations: README §3)"
elif [[ "$mem_gb" -gt 0 && -n "$neo4j_heap_override" ]]; then
    warn "RAM ${mem_gb} GB with small-host Neo4j override (NEO4J_HEAP_MAX=$neo4j_heap_override) — expect reduced capacity; 8 GB is the no-override floor, 16 GB recommended"
elif [[ "$mem_gb" -gt 4 ]]; then
    warn "RAM ${mem_gb} GB — below the ~8 GB no-override floor; the full stack will not fit at the shipped Neo4j defaults. Set the small-host preset (NEO4J_HEAP_INITIAL/NEO4J_HEAP_MAX/NEO4J_PAGECACHE in shared-memory/.env — see .env.example)"
elif [[ "$mem_gb" -gt 0 ]]; then
    bad "RAM ${mem_gb} GB — the shipped Neo4j memory defaults (heap 2G + pagecache 2G) exceed physical RAM and Neo4j will refuse to start. Set the small-host preset in shared-memory/.env (see .env.example) and re-run"
fi

disk_gb=$(df -BG --output=avail "$REPO_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
if [[ "$disk_gb" -ge 30 ]]; then
    ok "Disk ${disk_gb} GB free (>= 30 GB)"
elif [[ "$disk_gb" -gt 0 ]]; then
    warn "Disk ${disk_gb} GB free — ~30 GB recommended (images + model GGUFs + data)"
fi

command -v nvtop >/dev/null 2>&1 \
    && ok "nvtop present (GPU-aware dreaming enabled)" \
    || warn "nvtop not found — REM/NREM fall back to the time-based quiesce guard (optional)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo
if [[ "$fail" -eq 0 ]]; then
    grn "Preflight passed. Next: docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d"
else
    red "Preflight failed — resolve the ✗ items above, then re-run."
fi
exit "$fail"
