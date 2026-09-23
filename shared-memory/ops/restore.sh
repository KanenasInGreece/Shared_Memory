#!/usr/bin/env bash
#
# restore.sh — restore both stores from a backup.sh set. Stop the gateway first. Postgres (source of truth) is restored before Neo4j, and a non-empty store is refused unless --force.
# The set's sha256 is checked before anything is touched.
#
#   bash shared-memory/ops/restore.sh [NAME]
#   bash shared-memory/ops/restore.sh --force
#   bash shared-memory/ops/restore.sh --env /path/.env

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TARGET=""
FORCE=0
# shared-memory/.env first. The repo-root path alone found nothing on a normal install and restored with empty passwords.
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)   FORCE=1 ;;
    --env)     ENV_FILE="${2:?--env needs a path}"; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
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

# Passwords stay off the host argv. `docker exec -e` is visible in ps; --env-file is not. Create the 0700 directory as a bare statement, or the subshell loses the path and the trap cannot clean it up.
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

# cypher-shell has no -p. It reads NEO4J_PASSWORD from the container env that --env-file sets.
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

# --force must replace Neo4j the way pg_restore --clean replaces Postgres. A bare CREATE CONSTRAINT aborts after Postgres is already overwritten, and a replay that does not clear first merges the two graphs.
# Clear before any overwrite. A failed clear used to leave Postgres restored and Neo4j half-emptied.
if [[ "$FORCE" -eq 1 && "$neo_nodes" != "0" ]]; then
  echo "Clearing Neo4j before replay (--force) ..."
  # One transaction of the whole corpus exhausts the heap. Leave constraints alone; the export recreates the ones it owns.
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
# Made idempotent here, not in backup.sh. A fix at export time would leave every set already on disk unrestorable.
gunzip -c "$BASE.cypher.gz" \
  | sed -E '/^CREATE (CONSTRAINT|([A-Z]+ )?INDEX)/ { /IF NOT EXISTS/! s/ FOR / IF NOT EXISTS FOR /; }
s/^DROP CONSTRAINT ([^ ;]+);/DROP CONSTRAINT \1 IF EXISTS;/' \
  | $DOCKER exec -i --env-file "$_NEO4J_ENV_FILE" "$NEO4J_CONTAINER" \
  cypher-shell -u "$NEO4J_USER" \
  && grn "  ✓ neo4j restored" || die "cypher-shell replay failed"

# Logs are this host's history, not the corpus. Unpacking another machine's audit trail into the live files would record events that never happened here. They land in a sidecar.
logs_sha="$(json_get logs_sha256 < "$MANIFEST")"
if [[ -n "$logs_sha" && -f "$BASE.logs.tar.gz" ]]; then
  if [[ "$(sha256sum "$BASE.logs.tar.gz" | awk '{print $1}')" != "$logs_sha" ]]; then
    die "logs archive sha256 mismatch — refusing to unpack a corrupt artifact"
  fi
  # Not under the live log directory. The monitor and logrotate scan that tree, so a nested sidecar would still be read as local.
  _live_logs="${SHARED_MEMORY_LOG_DIR:-$HOME/.shared-memory/logs}"
  _logs_dest="$(dirname "$_live_logs")/restored-logs/$(basename "${MANIFEST%.manifest.json}")"
  mkdir -p "$_logs_dest"
  if tar xzf "$BASE.logs.tar.gz" -C "$_logs_dest" 2>/dev/null; then
    # Prefix the file itself. A directory name stops labelling it the moment it is copied out.
    while IFS= read -r -d '' f; do
      _b="$(basename "$f")"
      case "$_b" in restored-*) continue ;; esac
      mv "$f" "$(dirname "$f")/restored-$_b" 2>/dev/null || true
    done < <(find "$_logs_dest" -type f -print0 2>/dev/null)
    # The monitor only scans the live directory, so the sidecar would be invisible without a stable path: restored-logs/latest and RESTORED.json. Readers must present that as another host's history.
    python3 - "$_logs_dest/RESTORED.json" "$(basename "${MANIFEST%.manifest.json}")" \
             "$MANIFEST" <<'PYJSON' 2>/dev/null || true
import json, sys, datetime
dest, setname, manifest = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    src = json.load(open(manifest))
except Exception:
    src = {}
json.dump({
    "set": setname,
    "restored_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source_created": src.get("created"),
    "source_pg_db": src.get("pg_db"),
    "source_neo4j_nodes": src.get("neo4j_nodes"),
    "is_local_history": False,
    "note": ("Logs from ANOTHER deployment, restored beside the live ones. "
             "Present as restored history; never as events that happened here."),
}, open(dest, "w"), indent=2)
PYJSON
    ln -sfn "$_logs_dest" "$(dirname "$_logs_dest")/latest" 2>/dev/null || true
    grn "  ✓ logs restored to $_logs_dest"
    echo "    every file is prefixed 'restored-'; the LIVE logs are untouched, and"
    echo "    this directory is outside the one the monitor and logrotate read."
    echo "    Discoverable at: $(dirname "$_logs_dest")/latest  (see RESTORED.json)"
  else
    ylw "  ! could not unpack the logs archive — continuing; the stores are restored"
  fi
else
  echo "  i no logs in this set — the source host's operational history is not"
  echo "    included. A monitor here will show the corpus but no prior warnings."
fi

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

# The dump carries its own schema level. "Restore complete" used to be the last line, so a newer gateway ran an older schema with no error.
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
