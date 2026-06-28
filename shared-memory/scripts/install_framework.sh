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
export NEO4J_HOST_DIR PG_DATA_DIR LLM_MODELS_DIR NEO4J_PASSWORD PG_PASSWORD

# Render: copy the template, replacing only the value lines (ENVIRON avoids any
# escaping pitfalls with slashes/special chars in paths or passwords).
awk '
  function put(k) { print k "=" ENVIRON[k]; }
  /^NEO4J_HOST_DIR=/ { put("NEO4J_HOST_DIR"); next }
  /^PG_DATA_DIR=/    { put("PG_DATA_DIR");    next }
  /^LLM_MODELS_DIR=/ { put("LLM_MODELS_DIR"); next }
  /^NEO4J_PASSWORD=/ { put("NEO4J_PASSWORD"); next }
  /^PG_PASSWORD=/    { put("PG_PASSWORD");    next }
  { print }
' "$EXAMPLE" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

mkdir -p "$NEO4J_HOST_DIR"/{data,logs,import,plugins} "$PG_DATA_DIR"

echo
echo "✓ Wrote $ENV_FILE (chmod 600) and created data dirs."
echo "  Confirm it is gitignored:   git -C \"$REPO_DIR\" check-ignore shared-memory/.env"
echo "  Bring up the stack:         docker compose -f \"$REPO_DIR/postgres_neo4j_limits.yaml\" --env-file \"$ENV_FILE\" up -d"
echo "  Then mint client tokens:    uv run python shared-memory/scripts/generate_tokens.py"
