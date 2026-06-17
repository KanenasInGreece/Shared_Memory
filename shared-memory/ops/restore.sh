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
ENV_FILE="$REPO_ROOT/.env"
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

neo4j_q() {
  $DOCKER exec -e NEO4J_PASSWORD="$NEO4J_PASSWORD" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" --format plain "$1" 2>/dev/null | tail -n1
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
pg_rows="$($DOCKER exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
  psql -U "$PG_USER" -d "$PG_DB" -tAc 'SELECT count(*) FROM technical_docs' 2>/dev/null || echo 0)"
pg_rows="${pg_rows:-0}"
neo_nodes="$(neo4j_q 'MATCH (n) RETURN count(n)')"; neo_nodes="${neo_nodes:-0}"
if [[ "$FORCE" -ne 1 && ( "$pg_rows" != "0" || "$neo_nodes" != "0" ) ]]; then
  die "target not empty (technical_docs=$pg_rows, neo4j nodes=$neo_nodes) — pass --force to overwrite"
fi
[[ "$FORCE" -eq 1 ]] && ylw "  ! --force: overwriting existing data (technical_docs=$pg_rows, neo4j nodes=$neo_nodes)"

# ── Restore Postgres (source of truth) first, then Neo4j ─────────────────────
echo "Restoring Postgres → $PG_DB ..."
$DOCKER exec -i -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
  pg_restore -U "$PG_USER" -d "$PG_DB" --clean --if-exists --no-owner < "$BASE.pgdump" \
  && grn "  ✓ postgres restored" || die "pg_restore failed"

echo "Restoring Neo4j (replaying cypher export) ..."
gunzip -c "$BASE.cypher.gz" | $DOCKER exec -i -e NEO4J_PASSWORD="$NEO4J_PASSWORD" "$NEO4J_CONTAINER" \
  cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
  && grn "  ✓ neo4j restored" || die "cypher-shell replay failed"

# ── Report post-restore counts vs the manifest ───────────────────────────────
post_rows="$($DOCKER exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
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
