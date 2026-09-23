#!/usr/bin/env bash
#
# init_db.sh — initialise both stores for a fresh Shared Memory install.
#
# Postgres applies schema_init.sql, then fills schema_migrations only when this run created that schema. A database that already had the schema, such as a restored backup, is not adopted.
# Neo4j applies neo4j_init.cypher. Both clients run inside the compose containers, so the host needs neither psql nor cypher-shell.
#
# The ledger step runs on the host via uv and warns, rather than failing, when uv is missing. The schema files are idempotent, so a re-run is safe.
#
#   bash shared-memory/scripts/init_db.sh
#
# The compose stack must already be up. This script waits until the databases accept connections.

set -euo pipefail

# --help prints this header and exits. Any other argument is refused, because this script used to ignore flags and run the init anyway.
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
# shared-memory/.env first, then the pre-0.6 repo-root file, matching the gateway.
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

# Fail on a missing docker binary before a later "container not found" sends the reader to compose.
command -v docker >/dev/null 2>&1 || { red "✗ docker not found on PATH — install Docker first (preflight.sh checks this)."; exit 1; }
# Read keys with the shared parser instead of sourcing .env. PG_PASSWORD is for the authenticated check at the end, not the peer-trust schema steps.
read_env() { python3 "$SCRIPT_DIR/read_env_key.py" "$ENV_FILE" "$1"; }
NEO4J_PASSWORD="$(read_env NEO4J_PASSWORD)"
[[ -n "$NEO4J_PASSWORD" ]] || { red "✗ NEO4J_PASSWORD not set in .env"; exit 1; }
PG_PASSWORD="$(read_env PG_PASSWORD)"
[[ -n "$PG_PASSWORD" ]] || { red "✗ PG_PASSWORD not set in .env"; exit 1; }
# Export so `docker exec -e NEO4J_PASSWORD` inherits it. The password must not appear on argv.
export NEO4J_PASSWORD

docker inspect "$PG_CONTAINER"    >/dev/null 2>&1 || { red "✗ container '$PG_CONTAINER' not found — is the compose stack up?"; exit 1; }
docker inspect "$NEO4J_CONTAINER" >/dev/null 2>&1 || { red "✗ container '$NEO4J_CONTAINER' not found — is the compose stack up?"; exit 1; }

echo "Waiting for Postgres ($PG_CONTAINER) ..."
for ((i = 0; i < WAIT_TIMEOUT; i++)); do
    if docker exec "$PG_CONTAINER" pg_isready -U postgres -d "$PG_DB" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U postgres -d "$PG_DB" >/dev/null 2>&1 \
    || { red "✗ Postgres did not become ready within ${WAIT_TIMEOUT}s"; exit 1; }

# Ask whether technical_docs already exists before schema_init.sql runs. Adopting a restored database would mark migrations that were never applied.
# Only an explicit "f" counts as fresh. Anything else skips auto-adopt, which is recoverable by hand; a docker or psql failure still aborts.
# >>> SCHEMA_PREEXISTENCE
schema_preexisted() {
    local out
    out="$(docker exec -i "$PG_CONTAINER" psql -q -t -A -U postgres -d "$PG_DB" \
        -c "SELECT to_regclass('technical_docs') IS NOT NULL")"
    [[ "$out" == "f" ]] && echo 0 || echo 1
}

# Two queries, because SQL cannot skip FROM schema_migrations when that table may not exist. Anything but a confirmed "t" counts as unpopulated, and the caller only uses that to decide whether to print advice.
ledger_populated() {
    local exists_out
    exists_out="$(docker exec -i "$PG_CONTAINER" psql -q -t -A -U postgres -d "$PG_DB" \
        -c "SELECT to_regclass('schema_migrations') IS NOT NULL")"
    if [[ "$exists_out" != "t" ]]; then
        echo 0
        return 0
    fi
    local rows_out
    rows_out="$(docker exec -i "$PG_CONTAINER" psql -q -t -A -U postgres -d "$PG_DB" \
        -c "SELECT EXISTS(SELECT 1 FROM schema_migrations)")"
    [[ "$rows_out" == "t" ]] && echo 1 || echo 0
}
# <<< SCHEMA_PREEXISTENCE

SCHEMA_PREEXISTED="$(schema_preexisted)"

# client_min_messages=warning hides the IF NOT EXISTS "already exists" notices on a re-run.
echo "Applying schema_init.sql → Postgres/$PG_DB ..."
docker exec -e PGOPTIONS='-c client_min_messages=warning' -i "$PG_CONTAINER" \
    psql -q -v ON_ERROR_STOP=1 -U postgres -d "$PG_DB" \
    < "$MIGRATIONS_DIR/schema_init.sql" >/dev/null
grn "✓ Postgres schema applied"

# Fill schema_migrations only when SCHEMA_PREEXISTED is 0. schema_init.sql does not create the ledger, and adopting a restored database would record migrations that were never applied.
# The adopt runs on the host via uv, matching update_framework.sh, and is idempotent.
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

# >>> LEDGER_GATE_DECISION
if [[ "$SCHEMA_PREEXISTED" == "0" ]]; then
    adopt_ledger
elif [[ "$(ledger_populated)" == "0" ]]; then
    # The schema predates this run and the ledger is empty. Whether --adopt is safe is apply.py's call, not this script's.
    ylw "⚠ This database's framework schema already existed before this run —"
    ylw "  its migration ledger is empty. Whether it is safe to adopt is not"
    ylw "  this script's call (see apply.py's own guidance, which covers both"
    ylw "  origins this can be):"
    ylw "      uv run --with psycopg2-binary python $MIGRATIONS_DIR/apply.py --status"
    ylw "      uv run --with psycopg2-binary python $MIGRATIONS_DIR/apply.py --adopt"
fi
# <<< LEDGER_GATE_DECISION

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

# --fail-at-end applies every statement so one conflict does not hide the rest. A shared Neo4j may already index :Entity differently.
echo "Applying neo4j_init.cypher → Neo4j ..."
# set -e is active; `if` lets the shared-instance diagnostic below run instead of aborting at the exec.
if docker exec -e NEO4J_PASSWORD -i "$NEO4J_CONTAINER" cypher-shell -u neo4j \
       --fail-at-end < "$MIGRATIONS_DIR/neo4j_init.cypher"; then
    grn "✓ Neo4j constraints applied"
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

# Peer-trust checks above never use PG_PASSWORD, so a stale data directory still looks green (fact:1515). This check uses TCP with that password, the path the gateway actually authenticates on.
# Neo4j's cypher-shell already uses NEO4J_PASSWORD. The second check is so both stores are verified the same way, not only as a side effect.
# >>> AUTHENTICATED_CONNECTIVITY_CHECK
pg_authenticated_check() {
    PGPASSWORD="$PG_PASSWORD" docker exec -e PGPASSWORD -i "$PG_CONTAINER" \
        psql -q -t -A -h 127.0.0.1 -U postgres -d "$PG_DB" -c "SELECT 1" >/dev/null 2>&1
}

neo4j_authenticated_check() {
    docker exec -e NEO4J_PASSWORD "$NEO4J_CONTAINER" cypher-shell -u neo4j \
        "RETURN 1" >/dev/null 2>&1
}

authenticated_connectivity_check() {
    local store="$1" fn="$2"
    if "$fn"; then
        grn "✓ $store authenticated with this .env's credentials (host-facing path, same as the gateway)"
        return 0
    fi
    red "✗ $store REFUSED this .env's credentials over the password-checked"
    red "  connection the gateway will actually use to reach it."
    ylw "  Likely cause: this data directory pre-existed this install — a previous"
    ylw "  cluster's credentials are still in force. Reusing an old data directory"
    ylw "  silently keeps its old password; this .env's password is simply not it."
    ylw "  This is a data-directory problem, not a credentials problem — do not edit"
    ylw "  the .env to make this pass. Point the data directory at a genuinely fresh"
    ylw "  location, restore the matching backup set (shared-memory/ops/restore.sh),"
    ylw "  or clear the stale data first:"
    ylw "      bash shared-memory/scripts/uninstall_framework.sh --level data"
    return 1
}
# <<< AUTHENTICATED_CONNECTIVITY_CHECK

echo "Verifying host-facing authentication (the same credential path the gateway uses) ..."
_auth_failures=0
authenticated_connectivity_check "Postgres" pg_authenticated_check || _auth_failures=$((_auth_failures + 1))
authenticated_connectivity_check "Neo4j"    neo4j_authenticated_check || _auth_failures=$((_auth_failures + 1))
if [[ "$_auth_failures" -gt 0 ]]; then
    exit 1
fi

echo
grn "Both stores initialised. Next: bootstrap_tokens.sh, then start the gateway."
