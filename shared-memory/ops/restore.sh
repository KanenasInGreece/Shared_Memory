#!/usr/bin/env bash
#
# restore.sh — ground-up restore of the Shared Memory stores from a backup set.
#
# Restores BOTH stores from a set produced by backup.sh:
#   - Postgres : pg_restore (custom format) into $PG_DB
#   - Neo4j    : replay the APOC cypher export via cypher-shell
#
# Use after rebuilding a host: bring the Postgres + Neo4j containers up EMPTY
# (docker compose up -d), STOP the gateway so nothing writes, then run this.
#
# SAFETY: refuses to restore over a non-empty store unless --force, verifies the
# set's sha256 + integrity before touching anything, and restores Postgres (the
# source of truth) before Neo4j.
#
#   bash shared-memory/ops/restore.sh                 # restore the LATEST set
#   bash shared-memory/ops/restore.sh NAME            # restore a named set
#   bash shared-memory/ops/restore.sh --force         # overwrite a non-empty store
#   bash shared-memory/ops/restore.sh --env /path/.env
#
# Exit 0 on success; non-zero on any failure (verification or restore).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TARGET=""
FORCE=0
# Item 9(c), fix round 1: shared-memory/.env FIRST — the standing env-loader
# candidate order (CLAUDE.md Group 4 / apply.py / backup.sh's own fix for the
# same gap). This file previously tried the repo-root path ONLY, so on a
# correctly-installed machine (credentials live at shared-memory/.env, the
# v0.6+ location) it silently found no env at all and ran with empty
# passwords — backup.sh fixed this exact gap in an earlier release; restore.sh
# was the last holdout.
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)   FORCE=1 ;;
    --env)     ENV_FILE="${2:?--env needs a path}"; shift ;;
    -h|--help) sed -n '2,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*)        echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
    *)         TARGET="$1" ;;
  esac
  shift
done

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

# Safe .env loader — parses KEY=VALUE lines only (no shell execution), skips
# comments/blank/malformed lines, strips matched quotes, env var wins over file.
load_env() {
  local f="$1" line key val
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!key+x}" ]] && continue
    [[ "$val" =~ ^\".*\"$ || "$val" =~ ^\'.*\'$ ]] && val="${val:1:${#val}-2}"
    export "$key=$val"
  done < "$f"
}

load_env "$ENV_FILE"

DOCKER="${DOCKER:-docker}"
PG_CONTAINER="${PG_CONTAINER:-postgres-vector}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j-memory}"
PG_DB="${PG_DB:-agent_data}"
PG_USER="${PG_USER:-postgres}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.shared-memory/backups}"
PG_PASSWORD="${PG_PASSWORD:-}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
PREFIX="sm-backup"

command -v "$DOCKER" >/dev/null || die "'$DOCKER' not found"
command -v python3   >/dev/null || die "python3 not found"
command -v sha256sum >/dev/null || die "sha256sum not found"

json_get() { python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        d=d.get(k,{}) if isinstance(d,dict) else {}
    print(d if isinstance(d,(int,float,str)) else "")
except Exception:
    print("")' "$1" 2>/dev/null; }

# C1, fix round 1 (this file had NO tmpdir/trap handling at all before this
# fix — backup.sh's own C1 fix is the model, copied here in full): NEITHER
# database password may sit on the docker CLIENT's own argv on this host.
# `docker exec -e KEY=VALUE` — what this file originally used, and what
# S-08's own comment incorrectly called "not argv" — is an argument to the
# `docker` process running HERE, on the gateway/restore host, visible to
# `ps aux` for the whole `docker exec` lifetime. `docker exec --env-file
# <file>` reads KEY=VALUE from a file instead. ONE private 0700 mktemp -d
# directory holds both database env-files; created EAGERLY as a bare
# statement (never inside `$(...)`, which runs in a subshell and would lose
# the assignment — see backup.sh's QF-1 comment for the failure mode this
# avoids) right after config loads, with a trap that removes it on any exit.
_SECRETS_DIR=""
_NEO4J_ENV_FILE=""
_PG_ENV_FILE=""
init_secrets_dir() {
  [[ -n "$_SECRETS_DIR" ]] && return 0
  _SECRETS_DIR="$(mktemp -d)"
  chmod 700 "$_SECRETS_DIR"
  _NEO4J_ENV_FILE="$_SECRETS_DIR/neo4j.env"
  _PG_ENV_FILE="$_SECRETS_DIR/pg.env"
  printf 'NEO4J_PASSWORD=%s\n' "$NEO4J_PASSWORD" > "$_NEO4J_ENV_FILE"
  printf 'PGPASSWORD=%s\n' "$PG_PASSWORD" > "$_PG_ENV_FILE"
  chmod 600 "$_NEO4J_ENV_FILE" "$_PG_ENV_FILE"
}
cleanup_secrets_dir() {
  [[ -n "$_SECRETS_DIR" ]] && rm -rf "$_SECRETS_DIR"
  _SECRETS_DIR=""; _NEO4J_ENV_FILE=""; _PG_ENV_FILE=""
}
trap cleanup_secrets_dir EXIT INT TERM
init_secrets_dir

# cypher-shell itself takes no `-p` — it reads NEO4J_PASSWORD from the
# CONTAINER's own environment, which --env-file populates exactly like -e
# did (verified live: `docker exec … cypher-shell --help` → "-p PASSWORD …
# Can also be specified using the environment variable NEO4J_PASSWORD").
neo4j_q() {
  $DOCKER exec --env-file "$_NEO4J_ENV_FILE" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" --format plain "$1" 2>/dev/null | tail -n1
}

# ── Locate the set ───────────────────────────────────────────────────────────
if [[ -n "$TARGET" ]]; then
  MANIFEST="$BACKUP_DIR/${TARGET%.manifest.json}.manifest.json"
else
  MANIFEST="$(find "$BACKUP_DIR" -name "${PREFIX}-*.manifest.json" 2>/dev/null | sort | tail -n1)"
fi
[[ -f "$MANIFEST" ]] || die "no backup manifest found (looked in $BACKUP_DIR)"
BASE="${MANIFEST%.manifest.json}"
NAME="$(basename "$BASE")"
echo "Restore source: $NAME"

# ── Verify the set BEFORE touching the live stores ───────────────────────────
pg_sha="$(json_get pg_sha256 < "$MANIFEST")"
neo_sha="$(json_get neo4j_sha256 < "$MANIFEST")"
[[ "$(sha256sum "$BASE.pgdump"    | awk '{print $1}')" == "$pg_sha"  ]] || die "pgdump sha256 mismatch — refusing to restore a corrupt set"
[[ "$(sha256sum "$BASE.cypher.gz" | awk '{print $1}')" == "$neo_sha" ]] || die "cypher.gz sha256 mismatch — refusing to restore a corrupt set"
gzip -t "$BASE.cypher.gz" 2>/dev/null || die "cypher.gz corrupt"
grn "  ✓ set verified (sha256 + gzip integrity)"

# ── Refuse to clobber a non-empty store (unless --force) ─────────────────────
pg_rows="$($DOCKER exec --env-file "$_PG_ENV_FILE" "$PG_CONTAINER" \
  psql -U "$PG_USER" -d "$PG_DB" -tAc 'SELECT count(*) FROM technical_docs' 2>/dev/null || echo 0)"
pg_rows="${pg_rows:-0}"
neo_nodes="$(neo4j_q 'MATCH (n) RETURN count(n)')"; neo_nodes="${neo_nodes:-0}"
if [[ "$FORCE" -ne 1 && ( "$pg_rows" != "0" || "$neo_nodes" != "0" ) ]]; then
  die "target not empty (technical_docs=$pg_rows, neo4j nodes=$neo_nodes) — pass --force to overwrite"
fi
[[ "$FORCE" -eq 1 ]] && ylw "  ! --force: overwriting existing data (technical_docs=$pg_rows, neo4j nodes=$neo_nodes)"

# ── Neo4j: --force must REPLACE, exactly as it already does for Postgres ─────
#
# pg_restore runs with --clean --if-exists, so the Postgres half genuinely
# replaces what is there. The Neo4j half did not, and that asymmetry produced
# two failures that only appear on a target that is not empty — i.e. precisely
# the target --force exists for:
#
#   1. The APOC export emits bare `CREATE CONSTRAINT <name> FOR …` and
#      `CREATE [RANGE|POINT|…] INDEX FOR …` with no IF NOT EXISTS. Replaying
#      onto a store that already has them aborts the open transaction with
#      "An equivalent constraint already exists" — and it aborts AFTER Postgres
#      has been overwritten, leaving the two stores divergent. That is the exact
#      state quiescing a backup exists to prevent, manufactured during a restore.
#   2. Even with the schema statements fixed, the replay never CLEARED the graph,
#      so a forced restore MERGED the incoming graph into the existing one
#      instead of replacing it. Node counts would then exceed the manifest and
#      the closing comparison below would report a mismatch it could not explain.
#
# Measured on a real set (2615 nodes / 9560 rels, 38 schema statements): the
# rewrite guards 38 of 38, changes no data line, and is idempotent.
#
# ⛔ ORDERING: THE DESTRUCTIVE PREPARATION HAPPENS BEFORE ANYTHING IS OVERWRITTEN.
# The clear used to sit next to the Neo4j replay, i.e. AFTER pg_restore had
# already replaced Postgres. A clear that then failed — a timeout, a dropped
# connection, heap pressure part-way through the batches — left Postgres holding
# the restored corpus and Neo4j holding a partly-emptied old one, and the script
# died there. That is the split-brain a quiesced backup exists to prevent,
# manufactured by the restore itself.
# Doing it here means the only failure this can produce is "nothing was
# overwritten yet", which is recoverable by re-running.
if [[ "$FORCE" -eq 1 && "$neo_nodes" != "0" ]]; then
  echo "Clearing Neo4j before replay (--force) ..."
  # Batched: one transaction holding a whole corpus is how a restore runs the
  # heap out on a large graph. Constraints and indexes are deliberately LEFT
  # ALONE — the export recreates the ones it owns, and dropping the rest would
  # discard schema this set never knew about.
  $DOCKER exec -i --env-file "$_NEO4J_ENV_FILE" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" \
    'CALL { MATCH (n) DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS' \
    >/dev/null || die "could not clear Neo4j before a forced restore — refusing to
  replay on top of existing data, which would merge two graphs into one"
  grn "  ✓ neo4j cleared ($neo_nodes node(s) removed)"
fi

# ── Restore Postgres (source of truth) first, then Neo4j ─────────────────────
echo "Restoring Postgres → $PG_DB ..."
$DOCKER exec -i --env-file "$_PG_ENV_FILE" "$PG_CONTAINER" \
  pg_restore -U "$PG_USER" -d "$PG_DB" --clean --if-exists --no-owner < "$BASE.pgdump" \
  && grn "  ✓ postgres restored" || die "pg_restore failed"


echo "Restoring Neo4j (replaying cypher export) ..."
# The schema statements are made idempotent IN STREAM rather than at export
# time, deliberately: a fix in backup.sh would only help sets taken after it,
# and every set already on disk would stay unrestorable onto a live host.
gunzip -c "$BASE.cypher.gz" \
  | sed -E '/^CREATE (CONSTRAINT|([A-Z]+ )?INDEX)/ { /IF NOT EXISTS/! s/ FOR / IF NOT EXISTS FOR /; }
s/^DROP CONSTRAINT ([^ ;]+);/DROP CONSTRAINT \1 IF EXISTS;/' \
  | $DOCKER exec -i --env-file "$_NEO4J_ENV_FILE" "$NEO4J_CONTAINER" \
  cypher-shell -u "$NEO4J_USER" \
  && grn "  ✓ neo4j restored" || die "cypher-shell replay failed"

# ── Report post-restore counts vs the manifest ───────────────────────────────
post_rows="$($DOCKER exec --env-file "$_PG_ENV_FILE" "$PG_CONTAINER" \
  psql -U "$PG_USER" -d "$PG_DB" -tAc 'SELECT count(*) FROM technical_docs' 2>/dev/null || echo '?')"
post_nodes="$(neo4j_q 'MATCH (n) RETURN count(n)')"
post_rels="$(neo4j_q 'MATCH ()-[r]->() RETURN count(r)')"
man_nodes="$(json_get neo4j_nodes < "$MANIFEST")"
man_rels="$(json_get neo4j_rels   < "$MANIFEST")"
echo
echo "Post-restore counts:"
printf '  technical_docs : %s\n' "$post_rows"
printf '  neo4j nodes    : %s (manifest: %s)\n' "${post_nodes:-?}" "${man_nodes:-?}"
printf '  neo4j rels     : %s (manifest: %s)\n' "${post_rels:-?}" "${man_rels:-?}"
echo
if [[ -n "$man_nodes" && "$post_nodes" == "$man_nodes" ]]; then
  grn "Restore complete — node count matches the manifest."
else
  ylw "Restore complete — verify node counts above (a mismatch can be normal if the backup ran without full quiesce)."
fi

# ── The restore is HALF the operation ────────────────────────────────────────
#
# The data is back, but it is back at whatever schema level the dump was taken
# at — and that is very unlikely to be the level the gateway about to read it
# expects. `schema_migrations` travels INSIDE the dump, so the database now
# states its own level correctly; nothing has yet moved it forward to the code.
#
# Saying nothing here is how a restored deployment ends up running an older
# schema under a newer gateway with no error anywhere: this script's last line
# used to read "Restore complete", which an operator reasonably takes as done.
echo
ylw "⚠ The data is restored, but NOT yet migrated to the running code's level."
ylw "  A dump carries its own schema_migrations ledger, so this database is at the"
ylw "  level it was backed up at — not necessarily the level this gateway expects."
echo
ylw "  Finish the operation:"
ylw "    bash shared-memory/scripts/update_framework.sh --from-restore"
echo
ylw "  That runs the forward-only migration path (Postgres ledger, Neo4j constraints,"
ylw "  project identity, restart, domain backfill) and proves the result with"
ylw "  postflight. It REFUSES if this dump came from a NEWER deployment than this"
ylw "  checkout — migrations are forward-only and the schema cannot be moved back."
