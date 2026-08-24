#!/usr/bin/env bash
#
# init_db.sh — initialise both stores for a fresh Shared Memory install.
#
#   Postgres : applies schema_init.sql (tables, indexes, vector extension),
#              then — ONLY when this run created that schema from nothing,
#              never against a database that already had it (e.g. a restored
#              pre-v0.8.35 backup) — populates the migration ledger
#              (schema_migrations) so the FIRST-ever `apply.py` upgrade run
#              does not refuse this vouchable, just-created database (see the
#              SCHEMA_PREEXISTENCE and ADOPT_LEDGER blocks below).
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
# PROJECT_ALIASES) that bash `source` would mis-parse. Schema/constraint work
# below still runs as the in-container postgres superuser over the local
# socket (peer trust, no password) for Postgres, and with the password for
# Neo4j's cypher-shell — PG_PASSWORD itself is read here only for the
# AUTHENTICATED_CONNECTIVITY_CHECK near the end, which deliberately does NOT
# use peer trust (see that block for why).
read_env() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2-; }
NEO4J_PASSWORD="$(read_env NEO4J_PASSWORD)"
[[ -n "$NEO4J_PASSWORD" ]] || { red "✗ NEO4J_PASSWORD not set in .env"; exit 1; }
PG_PASSWORD="$(read_env PG_PASSWORD)"
[[ -n "$PG_PASSWORD" ]] || { red "✗ PG_PASSWORD not set in .env"; exit 1; }
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

# ── Did the framework schema already exist, BEFORE this run touches it? ──────
#
# CRITICAL (Ops & Release Integrity review, verified by the merger). This
# script is documented idempotent and schema_init.sql is `IF NOT EXISTS`, so
# running init_db.sh against a RESTORED pre-v0.8.35 backup silently succeeds
# without altering the old tables at all — indistinguishable, from the
# ADOPT_LEDGER block's own point of view further down, from a database this
# run just created. An unconditional adopt_ledger() call there would then
# record EVERY current migration as already applied on a database that is
# genuinely missing every post-v0.8.35 alteration — bypassing apply.py's own
# needs_adoption() safety stance and corrupting migration state permanently.
#
# The fix is to ask the question BEFORE schema_init.sql runs, while it is
# still answerable: does `technical_docs` (apply.py's own `_FRAMEWORK_TABLE`
# discriminator) already exist? A restored backup of any vintage has it; a
# truly fresh, empty database does not. `to_regclass()` never errors on an
# absent relation, so this is safe to run against a database with no
# framework objects at all — same in-container psql the rest of this script
# already uses, no new dependency.
#
# Defaults CONSERVATIVELY: only an explicit "f" (Postgres confirming the
# table is absent) is read as fresh. Anything else — "t", or output this
# script did not expect — is treated as "the schema pre-existed", which
# only ever costs a skipped auto-adopt (still recoverable by hand via
# apply.py --adopt), never a wrongly-adopted ledger. A `docker exec`/`psql`
# failure here is not swallowed — it propagates via `set -e`, exactly like
# every other docker/psql call in this script.
# >>> SCHEMA_PREEXISTENCE
schema_preexisted() {
    local out
    out="$(docker exec -i "$PG_CONTAINER" psql -q -t -A -U postgres -d "$PG_DB" \
        -c "SELECT to_regclass('technical_docs') IS NOT NULL")"
    [[ "$out" == "f" ]] && echo 0 || echo 1
}

# Whether schema_migrations exists AND already has at least one row. Two
# queries because SQL cannot lazily skip a FROM clause on a table that may
# not exist — referencing schema_migrations at all when to_regclass() says
# it is absent fails at parse time, not merely at runtime, even inside an
# AND. Defaults toward "0" (unpopulated) on anything but a confirmed "t" —
# the caller's only use of this is deciding whether to print an advisory
# pointer, so erring toward printing it costs nothing but a few extra lines.
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

# ── Apply Postgres schema ─────────────────────────────────────────────────────
# client_min_messages=warning silences the "already exists, skipping" NOTICE
# spam that IF NOT EXISTS emits on a re-run.
echo "Applying schema_init.sql → Postgres/$PG_DB ..."
docker exec -e PGOPTIONS='-c client_min_messages=warning' -i "$PG_CONTAINER" \
    psql -q -v ON_ERROR_STOP=1 -U postgres -d "$PG_DB" \
    < "$MIGRATIONS_DIR/schema_init.sql" >/dev/null
grn "✓ Postgres schema applied"

# ── Populate the migration ledger — but ONLY when THIS run created the ──────
# ── schema from nothing (SCHEMA_PREEXISTED, computed above) ─────────────────
#
# schema_init.sql deliberately does NOT create schema_migrations itself (see
# its own header) — that leaves a bare fresh install with the framework
# tables present and no ledger, which apply.py cannot tell apart from a
# pre-v0.8.35 backup it must refuse to touch until an operator vouches for it
# (exit 2, "predates migration tracking"). For a database schema_init.sql
# just created, THIS run IS the vouching: schema_init.sql is generated FROM
# the migration files (generate_schema_init.py), so recording every
# migration file as already applied restates what just happened here,
# seconds ago — not a guess about an unknown database's history.
#
# ⛔ That vouching is ONLY valid when SCHEMA_PREEXISTED == 0. A restored
# pre-v0.8.35 backup already has the framework schema, so schema_init.sql's
# `IF NOT EXISTS` silently no-ops against it — calling adopt_ledger() there
# would record every current migration as applied on a database genuinely
# missing every post-v0.8.35 alteration, which is exactly the corruption
# apply.py's own needs_adoption() refusal exists to prevent. apply.py's
# stance toward a database this run did NOT just create is therefore
# unchanged: --adopt is never called automatically for one — the operator
# is pointed at it instead, and only when it would actually help (the ledger
# is genuinely empty; a database that already has one stays quiet on re-run).
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

# >>> LEDGER_GATE_DECISION
if [[ "$SCHEMA_PREEXISTED" == "0" ]]; then
    adopt_ledger
elif [[ "$(ledger_populated)" == "0" ]]; then
    # The schema was already here before this run, AND it has no ledger —
    # apply.py's own guidance (Fix B) already explains both origins this
    # state can have and when --adopt is safe; point at it rather than
    # duplicating that prose here.
    ylw "⚠ This database's framework schema already existed before this run —"
    ylw "  its migration ledger is empty. Whether it is safe to adopt is not"
    ylw "  this script's call (see apply.py's own guidance, which covers both"
    ylw "  origins this can be):"
    ylw "      uv run --with psycopg2-binary python $MIGRATIONS_DIR/apply.py --status"
    ylw "      uv run --with psycopg2-binary python $MIGRATIONS_DIR/apply.py --adopt"
fi
# <<< LEDGER_GATE_DECISION

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

# ── Authenticated connectivity — the SAME credential path the gateway uses ──
#
# Everything above that touched Postgres ran as the in-container superuser
# over the local socket — pg_hba.conf's `local ... trust` line, which checks
# no password at all. That is exactly how a STALE data directory (measured,
# fact:1515 F7/F8) stayed invisible: a directory that predates this install
# and was never re-initialised still carries a PREVIOUS cluster's password
# ("Skipping initialization" in the container log confirms it), so every
# check above this line passes while the credential the GATEWAY actually
# authenticates with — TCP, from the host, with this .env's PG_PASSWORD — is
# wrong. init_db.sh reported green; the gateway then crash-looped on
# asyncpg.InvalidPasswordError, with the mismatch discovered only there.
#
# The fix: authenticate the SAME way the gateway will, once, now that the
# schema/constraint work above is done — giving F7 its early detection,
# inside this script rather than in the gateway's crash loop later.
# pg_hba.conf routes a Unix-socket connection through `trust` and a TCP
# connection through the password method the image was started with —
# passing `-h 127.0.0.1` from inside this docker exec forces that same
# password-checked path, with no psql, psycopg2 or `uv` needed on the HOST:
# the mechanic this file already uses throughout (docker exec into the
# container that already has the client).
#
# Neo4j never had this gap — cypher-shell above already connects over bolt
# WITH NEO4J_PASSWORD, so a stale Neo4j data directory already fails loudly
# during "Applying neo4j_init.cypher" — the check below re-confirms it
# explicitly anyway, so both stores are verified the same way on purpose,
# rather than one being verified only as a side effect of unrelated work.
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
