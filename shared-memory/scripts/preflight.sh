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
ENV_FILE="$REPO_ROOT/.env"

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
    bad "docker not found — install Docker Engine + Compose"
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
    bad ".env not found — run: cp .env.example .env  (then set PG_PASSWORD + NEO4J_PASSWORD)"
fi

# ── Soft requirements (warnings only) ─────────────────────────────────────────
echo
echo "Recommended:"

mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
if [[ "$mem_gb" -ge 16 ]]; then
    ok "RAM ${mem_gb} GB (>= 16 GB)"
elif [[ "$mem_gb" -gt 0 ]]; then
    warn "RAM ${mem_gb} GB — 16 GB recommended (Postgres + Neo4j ~6 GB; your LLM dominates VRAM)"
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
    grn "Preflight passed. Next: docker compose -f postgres_neo4j_limits.yaml up -d"
else
    red "Preflight failed — resolve the ✗ items above, then re-run."
fi
exit "$fail"
