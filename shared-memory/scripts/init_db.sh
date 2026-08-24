#!/usr/bin/env bash
#
# init_db.sh — initialise both stores for a fresh Shared Memory install.
#
#   Postgres : applies schema_init.sql (tables, indexes, vector extension),
#              then populates the migration ledger (schema_migrations) so
#              the FIRST-ever `apply.py` upgrade run does not refuse this
#              vouchable, just-created database (see the ADOPT_LEDGER block
#              below).
#   Neo4j    : applies neo4j_init.cypher (uniqueness constraints)
#
# Runs the clients INSIDE the compose containers (postgres-vector / neo4j-memory)
# via `docker exec`, so the host needs neither psql nor cypher-shell. Both files
# are idempotent (IF NOT EXISTS), so this is safe to re-run. The ledger step is
# the one exception: it runs on the HOST via `uv` (matching how apply.py is
# invoked everywhere else), and degrades to a warning — never a hard failure —
# when `uv` is not on PATH, so a host without it still finishes initialisation.
#
#   bash shared-memory/scripts/init_db.sh
#
# Prerequisite: the compose stack is up (docker compose ... up -d) and the
# databases are accepting connections. The script waits for readiness.

set -euo pipefail

# ⛔ RULING 4: every operator-facing script accepts -h/--help (prints its own
# header, exits 0, does nothing else) and refuses any argument it does not
# recognise — this script previously had no argument parsing at all, so any
# flag (including --help) was silently ignored and the init ran anyway.
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
MIGRATIONS_DIR="$SCRIPT_DIR/../migrations"
# Framework env lives at shared-memory/.env; the repo-root path is the pre-0.6
# fallback — same resolution order as the gateway (hive_mind_proxy.py).
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

PG_CONTAINER="${PG_CONTAINER:-postgres-vector}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j-memory}"
PG_DB="${PG_DB:-agent_data}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-60}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

[[ -f "$ENV_FILE" ]] || { red "✗ .env not found at $ENV_FILE — run preflight.sh first."; exit 1; }

# Presence check before first use — without it a missing docker would surface
# below as "container not found", sending the reader to compose instead of to
# the actual cause (the sister project's install review found exactly this
# misdiagnosis class: a script that dies for a reason it misreports).
command -v docker >/dev/null 2>&1 || { red "✗ docker not found on PATH — install Docker first (preflight.sh checks this)."; exit 1; }
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

# ── Populate the migration ledger — the ONE moment adoption is automatic ─────
#
# schema_init.sql just created the framework schema, but deliberately does NOT
# create schema_migrations itself (see its own header) — that leaves a bare
# fresh install with the framework tables present and no ledger, which
# apply.py cannot tell apart from a pre-v0.8.35 backup it must refuse to touch
# until an operator vouches for it (exit 2, "predates migration tracking").
# For a database schema_init.sql just created, THIS run IS the vouching:
# schema_init.sql is generated FROM the migration files
# (generate_schema_init.py), so recording every migration file as already
# applied restates what just happened here, seconds ago — not a guess about
# an unknown database's history. apply.py's stance toward a database it did
# NOT just create is unchanged: --adopt is never called automatically for one.
#
# Runs on the HOST, not via `docker exec` — apply.py connects out via
# psycopg2 (matching how update_framework.sh invokes it), and Postgres's port
# is published to the host by ops/postgres_neo4j_limits.yaml. --adopt is
# idempotent (ON CONFLICT DO NOTHING for every file), so re-running this
# script is always safe, including against an already-adopted ledger.
# >>> ADOPT_LEDGER
adopt_ledger() {
    if ! command -v uv >/dev/null 2>&1; then
        ylw "⚠ uv not found on PATH — could not populate the migration ledger automatically."
        ylw "  Run this once, from the host, before the first upgrade:"
        ylw "      uv run --with psycopg2-binary python $MIGRATIONS_DIR/apply.py --adopt"
        return 0
    fi
    if uv run --with psycopg2-binary python "$MIGRATIONS_DIR/apply.py" --adopt >/dev/null; then
        grn "✓ Migration ledger populated (schema_migrations, adopted from schema_init.sql)"
    else
        ylw "⚠ Could not populate the migration ledger automatically (apply.py --adopt failed)."
        ylw "  Postgres schema is still fine; run this by hand before the first upgrade:"
        ylw "      uv run --with psycopg2-binary python $MIGRATIONS_DIR/apply.py --adopt"
    fi
}
# <<< ADOPT_LEDGER
adopt_ledger

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
