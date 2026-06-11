#!/usr/bin/env bash
#
# init_db.sh — initialise both stores for a fresh Shared Memory install.
#
#   Postgres : applies schema_init.sql (tables, indexes, vector extension)
#   Neo4j    : applies neo4j_init.cypher (uniqueness constraints)
#
# Runs the clients INSIDE the compose containers (postgres-vector / neo4j-memory)
# via `docker exec`, so the host needs neither psql nor cypher-shell. Both files
# are idempotent (IF NOT EXISTS), so this is safe to re-run.
#
#   bash shared-memory/scripts/init_db.sh
#
# Prerequisite: the compose stack is up (docker compose ... up -d) and the
# databases are accepting connections. The script waits for readiness.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MIGRATIONS_DIR="$SCRIPT_DIR/../migrations"
ENV_FILE="$REPO_ROOT/.env"

PG_CONTAINER="${PG_CONTAINER:-postgres-vector}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j-memory}"
PG_DB="${PG_DB:-agent_data}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-60}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

[[ -f "$ENV_FILE" ]] || { red "✗ .env not found at $ENV_FILE — run preflight.sh first."; exit 1; }
# Read keys without sourcing — .env values may contain spaces (e.g.
# PROJECT_ALIASES) that bash `source` would mis-parse. Postgres needs no
# password here: docker exec runs as the in-container postgres superuser
# (local trust auth). Only Neo4j's cypher-shell needs the password.
read_env() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2-; }
NEO4J_PASSWORD="$(read_env NEO4J_PASSWORD)"
[[ -n "$NEO4J_PASSWORD" ]] || { red "✗ NEO4J_PASSWORD not set in .env"; exit 1; }
# Export so `docker exec -e NEO4J_PASSWORD` (no value) passes it through from
# this process's environment — the password never appears on any argv (a
# world-readable /proc/<pid>/cmdline), unlike `cypher-shell -p <password>`.
export NEO4J_PASSWORD

docker inspect "$PG_CONTAINER"    >/dev/null 2>&1 || { red "✗ container '$PG_CONTAINER' not found — is the compose stack up?"; exit 1; }
docker inspect "$NEO4J_CONTAINER" >/dev/null 2>&1 || { red "✗ container '$NEO4J_CONTAINER' not found — is the compose stack up?"; exit 1; }

# ── Wait for Postgres ─────────────────────────────────────────────────────────
echo "Waiting for Postgres ($PG_CONTAINER) ..."
for ((i = 0; i < WAIT_TIMEOUT; i++)); do
    if docker exec "$PG_CONTAINER" pg_isready -U postgres -d "$PG_DB" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U postgres -d "$PG_DB" >/dev/null 2>&1 \
    || { red "✗ Postgres did not become ready within ${WAIT_TIMEOUT}s"; exit 1; }

# ── Apply Postgres schema ─────────────────────────────────────────────────────
# client_min_messages=warning silences the "already exists, skipping" NOTICE
# spam that IF NOT EXISTS emits on a re-run.
echo "Applying schema_init.sql → Postgres/$PG_DB ..."
docker exec -e PGOPTIONS='-c client_min_messages=warning' -i "$PG_CONTAINER" \
    psql -q -v ON_ERROR_STOP=1 -U postgres -d "$PG_DB" \
    < "$MIGRATIONS_DIR/schema_init.sql" >/dev/null
grn "✓ Postgres schema applied"

# ── Wait for Neo4j ────────────────────────────────────────────────────────────
echo "Waiting for Neo4j ($NEO4J_CONTAINER) ..."
for ((i = 0; i < WAIT_TIMEOUT; i++)); do
    if docker exec -e NEO4J_PASSWORD "$NEO4J_CONTAINER" cypher-shell -u neo4j \
         "RETURN 1" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec -e NEO4J_PASSWORD "$NEO4J_CONTAINER" cypher-shell -u neo4j "RETURN 1" >/dev/null 2>&1 \
    || { red "✗ Neo4j did not become ready within ${WAIT_TIMEOUT}s"; exit 1; }

# ── Apply Neo4j constraints ───────────────────────────────────────────────────
# --fail-at-end: attempt every statement, so one conflict doesn't block the
# rest. On a fresh single-purpose instance all constraints apply cleanly. On a
# Neo4j shared with another system, a label like :Entity may already carry a
# differently-keyed index/constraint — those statements are reported, the rest
# still apply.
echo "Applying neo4j_init.cypher → Neo4j ..."
# set -e is active; guard with `if` so we can give a useful diagnostic on failure.
if docker exec -e NEO4J_PASSWORD -i "$NEO4J_CONTAINER" cypher-shell -u neo4j \
       --fail-at-end < "$MIGRATIONS_DIR/neo4j_init.cypher"; then
    grn "✓ Neo4j constraints applied"
    echo
    grn "Both stores initialised. Next: bootstrap_tokens.sh, then start the gateway."
else
    echo
    red "✗ One or more Neo4j constraints could not be applied."
    ylw "  On a FRESH, standalone Neo4j all 7 constraints create cleanly. This error"
    ylw "  usually means the :Entity (or another) label already carries a conflicting"
    ylw "  index/constraint — e.g. a Neo4j shared with another memory system that"
    ylw "  keys Entity by id with a non-unique name index. Inspect with:"
    ylw "    docker exec -it $NEO4J_CONTAINER cypher-shell -u neo4j -p <password> 'SHOW CONSTRAINTS'"
    ylw "  Postgres was initialised successfully; resolve the Neo4j conflict by hand"
    ylw "  if this instance is meant to be a single-purpose framework store."
    exit 1
fi
