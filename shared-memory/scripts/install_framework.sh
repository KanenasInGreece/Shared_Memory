#!/usr/bin/env bash
# install_framework.sh — first-time framework setup.
#
# Prompts for host data paths + DB passwords, writes the gitignored framework
# env (shared-memory/.env) from the committed template, and creates the data
# dirs docker-compose mounts. Idempotent-ish: refuses to clobber an existing
# .env without confirmation. The CLIENT token is configured separately in each
# agent's skill .env (shared-memory-skill/shared-memory/.env.example).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK_DIR="$(dirname "$SCRIPT_DIR")"          # …/shared-memory
REPO_DIR="$(dirname "$FRAMEWORK_DIR")"            # repo root
EXAMPLE="$FRAMEWORK_DIR/.env.example"
ENV_FILE="$FRAMEWORK_DIR/.env"

[ -f "$EXAMPLE" ] || { echo "ERROR: missing $EXAMPLE" >&2; exit 1; }

echo "── Shared Memory — framework first-install ──"
if [ -f "$ENV_FILE" ]; then
  read -r -p "shared-memory/.env already exists. Overwrite? [y/N] " yn
  [[ "${yn:-}" =~ ^[Yy]$ ]] || { echo "Aborted — existing .env kept."; exit 0; }
fi

ask() {  # prompt default  → echoes answer (default if blank)
  local v; read -r -p "$1 [$2]: " v; printf '%s' "${v:-$2}"
}
ask_secret() {  # prompt → echoes answer (input hidden)
  local v; read -r -s -p "$1: " v; echo >&2; printf '%s' "$v"
}

NEO4J_HOST_DIR="$(ask 'Neo4j host data dir'        "$HOME/databases/neo4j")"
PG_DATA_DIR="$(ask 'Postgres data dir'             "$HOME/databases/postgres")"
LLM_MODELS_DIR="$(ask 'GGUF models dir (blank if using LM Studio)' '')"
NEO4J_PASSWORD="$(ask_secret 'Neo4j password')"
PG_PASSWORD="$(ask_secret 'Postgres password')"
# CPU thread budget for the two encoder containers, DERIVED from this host
# rather than assumed: about half its threads plus one, so reranking cannot
# starve Postgres, Neo4j, the gateway and the desktop. Portable across the
# three ways a machine reports its CPU count; falls back to the compose default.
# `--threads` counts THREADS, so this counts threads — matching the unit of the
# flag it feeds. (Deriving it from physical cores and passing it to a thread
# flag mixes two units and silently halves the budget.) Half the machine plus
# one leaves room for Postgres, Neo4j, the gateway and the desktop.
#
# ⚠ This is a PER-CONTAINER default and there are two encoders. They can run at
# once (a search reranks while a save embeds), so on a machine where that
# overlap is sustained, halve it again or pin each to its own cores — the
# framework cannot know which, so it ships the simple derivation and leaves the
# tuning to the operator.
_ncpu="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null \
         || sysctl -n hw.ncpu 2>/dev/null || echo 8)"
LLAMA_CPU_THREADS="$(( _ncpu / 2 + 1 ))"
[ "$LLAMA_CPU_THREADS" -lt 1 ] && LLAMA_CPU_THREADS=1
# S-07: NEO4J_HOST_DIR/PG_DATA_DIR/LLM_MODELS_DIR/LLAMA_CPU_THREADS are plain
# config — safe to export at the top level, and install_service.sh /
# install_llm_backends.sh (spawned below, neither of which needs a DB
# password) inheriting them costs nothing. NEO4J_PASSWORD/PG_PASSWORD are
# exported ONLY inside this subshell, scoped to the one awk invocation that
# needs them via ENVIRON — they never reach the outer script's environment,
# so a later `bash "$FRAMEWORK_DIR/ops/install_service.sh"` or
# `install_llm_backends.sh` cannot inherit a DB password neither one needs.
export NEO4J_HOST_DIR PG_DATA_DIR LLM_MODELS_DIR LLAMA_CPU_THREADS

# S-07: umask 077 so $ENV_FILE is created 600 from the FIRST byte — never
# create-then-chmod, which leaves a window at the process umask (often 0644,
# world-readable) between the file's creation and the chmod below. The
# trailing chmod stays as a belt-and-suspenders no-op for an inherited umask
# that already happened to be tighter.
(
  export NEO4J_PASSWORD PG_PASSWORD
  umask 077
  # Render: copy the template, replacing only the value lines (ENVIRON avoids
  # any escaping pitfalls with slashes/special chars in paths or passwords).
  awk '
    function put(k) { print k "=" ENVIRON[k]; }
    /^NEO4J_HOST_DIR=/ { put("NEO4J_HOST_DIR"); next }
    /^PG_DATA_DIR=/    { put("PG_DATA_DIR");    next }
    /^LLM_MODELS_DIR=/ { put("LLM_MODELS_DIR"); next }
    /^NEO4J_PASSWORD=/ { put("NEO4J_PASSWORD"); next }
    /^PG_PASSWORD=/    { put("PG_PASSWORD");    next }
    /^LLAMA_CPU_THREADS=/ { put("LLAMA_CPU_THREADS"); next }
    { print }
  ' "$EXAMPLE" > "$ENV_FILE"
)
chmod 600 "$ENV_FILE"

mkdir -p "$NEO4J_HOST_DIR"/{data,logs,import,plugins} "$PG_DATA_DIR"

echo
echo "✓ Wrote $ENV_FILE (chmod 600) and created data dirs."
echo "  Encoder CPU budget:         LLAMA_CPU_THREADS=$LLAMA_CPU_THREADS (of $_ncpu host threads)"
echo "  Confirm it is gitignored:   git -C \"$REPO_DIR\" check-ignore shared-memory/.env"
echo "  Bring up the stack:         docker compose -f \"$REPO_DIR/shared-memory/ops/shared-memory/ops/postgres_neo4j_limits.yaml\" --env-file \"$ENV_FILE\" up -d"
echo "  Then mint client tokens:    uv run python shared-memory/scripts/generate_tokens.py"

echo
if command -v systemctl >/dev/null 2>&1; then
  read -r -p "Install the gateway as a systemd --user service now (auto-start on boot, clean shutdown, no manual restart step)? [Y/n] " svc_yn
  if [[ ! "${svc_yn:-Y}" =~ ^[Nn]$ ]]; then
    bash "$FRAMEWORK_DIR/ops/install_service.sh"
  else
    echo "  Skipped. Install later:      bash shared-memory/ops/install_service.sh"
  fi
else
  echo "  systemd not found — skipping the service-install prompt. The gateway still"
  echo "  runs fine started by hand; it just won't survive logout/reboot without one."
fi

echo
read -r -p "Configure reasoning-LLM backend(s) now (local, remote, or a paid cloud API)? [y/N] " llm_yn
if [[ "${llm_yn:-N}" =~ ^[Yy]$ ]]; then
  bash "$FRAMEWORK_DIR/ops/install_llm_backends.sh"
else
  echo "  Skipped. A single default backend at LLM_DEFAULT_TARGET (http://localhost:5000)"
  echo "  is used until you configure one:  bash shared-memory/ops/install_llm_backends.sh"
fi
