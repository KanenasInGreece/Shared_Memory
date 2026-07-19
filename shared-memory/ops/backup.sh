#!/usr/bin/env bash
#
# backup.sh — consistent, quiesced backup of the Shared Memory stores.
#
# Captures BOTH stores (Postgres source-of-truth + Neo4j, which holds non-derivable
# HAD_OUTCOME retrospective edges) as one set:
#   - Postgres : pg_dump -Fc (online, MVCC-consistent)
#   - Neo4j    : APOC apoc.export.cypher.all to /import (online; needs the
#                NEO4J_apoc_export_file_enabled=true compose flag)
#
# Consistency: before dumping it asks the gateway to QUIESCE — client writes shed
# (503 + Retry-After) and the REM/NREM daemons are fenced by a Postgres advisory
# lock — then drains the outbox so the two stores are caught up. A trap resumes the
# gateway on ANY exit, and the gateway's own TTL auto-resumes if this script dies.
#
# This is an OPERATIONS-surface script: it runs on the single gateway host, never
# from a skill dir. Policy (schedule/retention/destination/encryption) is the
# admin's — set it in the private .env; the cron/systemd cadence is the admin's too.
#
#   bash shared-memory/ops/backup.sh                 # full quiesced backup
#   bash shared-memory/ops/backup.sh --dry-run       # sizes/space/retention, no writes
#   bash shared-memory/ops/backup.sh --verify        # integrity-check the latest set
#   bash shared-memory/ops/backup.sh --verify NAME   # integrity-check a named set
#   bash shared-memory/ops/backup.sh --env /path/.env
#
# Exit 0 on success; non-zero on failure (a partial dump is never promoted).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── CLI ──────────────────────────────────────────────────────────────────────
MODE="backup"
VERIFY_TARGET=""
# Framework env lives at shared-memory/.env (v0.6+); the repo-root path is the
# pre-0.6 fallback — the same resolution init_db.sh and the maintenance scripts
# use. backup.sh was the lone outlier: with only the repo-root path it silently
# found NO env file, so BACKUP_DIR fell back to $HOME/.shared-memory (local disk,
# not the configured destination) and BACKUP_ADMIN_TOKEN was empty (no quiesce).
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -dr|--dry-run) MODE="dry-run" ;;
    --verify)      MODE="verify"; [[ $# -gt 1 && "$2" != -* ]] && { VERIFY_TARGET="$2"; shift; } ;;
    --env)         ENV_FILE="${2:?--env needs a path}"; shift ;;
    -h|--help)     sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

# Safe .env loader — parses KEY=VALUE lines only (no shell execution, so a value
# with spaces can't run as a command), skips comments/blank/malformed lines,
# strips matched surrounding quotes, and lets a pre-set environment var win.
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
    [[ -n "${!key+x}" ]] && continue                       # already in env → keep it
    [[ "$val" =~ ^\".*\"$ || "$val" =~ ^\'.*\'$ ]] && val="${val:1:${#val}-2}"
    export "$key=$val"
  done < "$f"
}

# ── Config (private .env first, then env, then defaults) ─────────────────────
load_env "$ENV_FILE"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8888}"
DOCKER="${DOCKER:-docker}"
PG_CONTAINER="${PG_CONTAINER:-postgres-vector}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j-memory}"
PG_DB="${PG_DB:-agent_data}"
PG_USER="${PG_USER:-postgres}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
# Where APOC writes the export = Neo4j's server.directories.import. Auto-detected
# at run time (SHOW SETTINGS) so it works on any deployment; this is only the
# fallback if that query fails. Set it explicitly to skip auto-detection.
NEO4J_IMPORT_DIR="${NEO4J_IMPORT_DIR:-/var/lib/neo4j/import}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.shared-memory/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_LOCKFILE="${BACKUP_LOCKFILE:-$HOME/.shared-memory/backup.lock}"
# Both the drain wait AND the quiesce TTL: /admin/backup blocks up to this long
# waiting for the dream daemons to drain, then returns 202 and holds the fence for
# the same span. Keep it well above the dump duration but low enough that a nightly
# run cannot stall — and the client MUST out-wait it (see qcurl below).
BACKUP_QUIESCE_MAX_SECONDS="${BACKUP_QUIESCE_MAX_SECONDS:-120}"
BACKUP_DRAIN_MAX_SECONDS="${BACKUP_DRAIN_MAX_SECONDS:-120}"
BACKUP_DRAIN_POLL_SECONDS="${BACKUP_DRAIN_POLL_SECONDS:-2}"
BACKUP_QUIESCE_REQUIRED="${BACKUP_QUIESCE_REQUIRED:-0}"
BACKUP_ADMIN_TOKEN="${BACKUP_ADMIN_TOKEN:-}"
PG_PASSWORD="${PG_PASSWORD:-}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
PREFIX="sm-backup"

command -v "$DOCKER"   >/dev/null || die "'$DOCKER' not found"
command -v python3     >/dev/null || die "python3 not found (needed for JSON parsing)"
command -v sha256sum   >/dev/null || die "sha256sum not found"

# Pull one scalar out of a JSON object on stdin (dotted path). Empty on any
# miss/parse error — robust to an empty or non-JSON response (no stack trace).
json_get() { python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        d=d.get(k,{}) if isinstance(d,dict) else {}
    print(d if isinstance(d,(int,float,str)) else "")
except Exception:
    print("")' "$1" 2>/dev/null; }

dcurl() { curl -s --max-time 15 "$@"; }
# The quiesce handshake BLOCKS for up to BACKUP_QUIESCE_MAX_SECONDS while the
# gateway fences the dream daemons, so it needs its own, longer budget. With the
# 15s dcurl the client always timed out first, so every backup silently ran
# UNQUIESCED whenever a REM/NREM generation was in flight (which, with the ~22-30K
# token grounding prompt, is most of the time).
qcurl() { curl -s --max-time "$(( BACKUP_QUIESCE_MAX_SECONDS + 30 ))" "$@"; }

# ── Quiesce handshake ────────────────────────────────────────────────────────
QUIESCED=0
quiesce() {
  [[ -z "$BACKUP_ADMIN_TOKEN" ]] && return 1
  local resp code body
  resp="$(qcurl -w $'\n%{http_code}' -X POST "$GATEWAY_URL/admin/backup" \
    -H "Authorization: Bearer $BACKUP_ADMIN_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"state\":\"quiesce\",\"max_seconds\":$BACKUP_QUIESCE_MAX_SECONDS}")" || return 1
  code="$(tail -n1 <<<"$resp")"; body="$(sed '$d' <<<"$resp")"
  case "$code" in
    200) QUIESCED=1; grn "  ✓ quiesced — client writes shed, daemons drained" ;;
    202) QUIESCED=1; ylw "  ! quiesced — daemon drain TIMED OUT; a daemon may write during the dump ($(json_get daemons <<<"$body"))" ;;
    *)   ylw "  ! quiesce request returned HTTP $code"; return 1 ;;
  esac
  return 0
}
resume() {
  [[ "$QUIESCED" -eq 1 ]] || return 0
  dcurl -X POST "$GATEWAY_URL/admin/backup" \
    -H "Authorization: Bearer $BACKUP_ADMIN_TOKEN" -H 'Content-Type: application/json' \
    -d '{"state":"resume"}' >/dev/null 2>&1
  QUIESCED=0
}

# Wait until the outbox is empty (pending==0 && in_progress==0) so Neo4j is caught
# up with Postgres before the snapshot. Best-effort: proceeds on timeout.
drain_outbox() {
  local waited=0 pend prog
  while (( waited < BACKUP_DRAIN_MAX_SECONDS )); do
    local tel; tel="$(dcurl "$GATEWAY_URL/memory/telemetry" \
      -H "Authorization: Bearer $BACKUP_ADMIN_TOKEN")" || break
    pend="$(json_get telemetry.postgres.outbox.pending     <<<"$tel")"; pend="${pend:-0}"
    prog="$(json_get telemetry.postgres.outbox.in_progress <<<"$tel")"; prog="${prog:-0}"
    [[ "$pend" == "0" && "$prog" == "0" ]] && { grn "  ✓ outbox drained"; return 0; }
    sleep "$BACKUP_DRAIN_POLL_SECONDS"; waited=$(( waited + BACKUP_DRAIN_POLL_SECONDS ))
  done
  ylw "  ! outbox not fully drained after ${BACKUP_DRAIN_MAX_SECONDS}s — proceeding (restore self-heals on replay)"
}

# ── Dump primitives ──────────────────────────────────────────────────────────
neo4j_count() {  # $1 = cypher count query → integer (or empty)
  $DOCKER exec -e NEO4J_PASSWORD="$NEO4J_PASSWORD" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" --format plain "$1" 2>/dev/null | tail -n1
}

dump_postgres() {  # $1 = dest path
  $DOCKER exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
    pg_dump -U "$PG_USER" -Fc "$PG_DB" > "$1.tmp" 2>/dev/null \
    && mv "$1.tmp" "$1" || { rm -f "$1.tmp"; return 1; }
}

# Resolve Neo4j's configured import dir (where APOC writes) so we read the export
# from the right place regardless of the deployment's server.directories.import.
resolve_import_dir() {
  local d
  d="$($DOCKER exec -e NEO4J_PASSWORD="$NEO4J_PASSWORD" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" --format plain \
    "SHOW SETTINGS YIELD name,value WHERE name='server.directories.import' RETURN value" \
    2>/dev/null | tail -n +2 | tr -d '"' | tail -1)"
  if   [[ -z "$d"   ]]; then echo "$NEO4J_IMPORT_DIR"          # query failed → fallback
  elif [[ "$d" == /* ]]; then echo "$d"                       # absolute path
  else echo "/var/lib/neo4j/$d"; fi                           # relative to neo4j home
}

dump_neo4j() {  # $1 = dest path (.cypher.gz)
  local fname="${PREFIX}-export.cypher" idir
  idir="$(resolve_import_dir)"
  # APOC writes into the container's import dir; stream it out and gzip, then clean up.
  $DOCKER exec -e NEO4J_PASSWORD="$NEO4J_PASSWORD" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" --format plain \
    "CALL apoc.export.cypher.all('$fname', {format:'cypher-shell'}) YIELD file RETURN file" \
    >/dev/null 2>&1 || return 1
  $DOCKER exec "$NEO4J_CONTAINER" cat "$idir/$fname" 2>/dev/null | gzip > "$1.tmp" || { rm -f "$1.tmp"; return 1; }
  $DOCKER exec "$NEO4J_CONTAINER" rm -f "$idir/$fname" >/dev/null 2>&1
  [[ -s "$1.tmp" ]] || { rm -f "$1.tmp"; return 1; }
  mv "$1.tmp" "$1"
}

# ── Modes ────────────────────────────────────────────────────────────────────
do_dry_run() {
  echo "Shared Memory — backup DRY RUN (no writes, no quiesce)"; echo
  local pgsize nodes rels
  pgsize="$($DOCKER exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
    psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT pg_size_pretty(pg_database_size('$PG_DB'))" 2>/dev/null)"
  nodes="$(neo4j_count 'MATCH (n) RETURN count(n)')"
  rels="$(neo4j_count 'MATCH ()-[r]->() RETURN count(r)')"
  printf '  Postgres DB size : %s\n' "${pgsize:-unknown}"
  printf '  Neo4j nodes/rels : %s / %s\n' "${nodes:-?}" "${rels:-?}"
  echo
  mkdir -p "$BACKUP_DIR"
  local free count total
  free="$(df -h "$BACKUP_DIR" | awk 'NR==2{print $4}')"
  count="$(find "$BACKUP_DIR" -name "${PREFIX}-*.manifest.json" 2>/dev/null | wc -l | tr -d ' ')"
  total="$(du -sh "$BACKUP_DIR" 2>/dev/null | awk '{print $1}')"
  printf '  Backup dir       : %s\n' "$BACKUP_DIR"
  printf '  Free space       : %s\n' "${free:-?}"
  printf '  Existing sets    : %s (using %s)\n' "$count" "${total:-0}"
  printf '  Retention        : %s days\n' "$BACKUP_RETENTION_DAYS"
  echo; grn "Dry run complete — no changes made."
}

do_verify() {
  local manifest
  if [[ -n "$VERIFY_TARGET" ]]; then
    manifest="$BACKUP_DIR/${VERIFY_TARGET%.manifest.json}.manifest.json"
  else
    manifest="$(find "$BACKUP_DIR" -name "${PREFIX}-*.manifest.json" 2>/dev/null | sort | tail -n1)"
  fi
  [[ -f "$manifest" ]] || die "no manifest found to verify (looked in $BACKUP_DIR)"
  echo "Verifying: $(basename "$manifest")"
  local base; base="${manifest%.manifest.json}"
  local fail=0

  # 1. sha256 of each artifact matches the manifest
  local pg_sha neo_sha
  pg_sha="$(json_get pg_sha256    < "$manifest")"
  neo_sha="$(json_get neo4j_sha256 < "$manifest")"
  if [[ "$(sha256sum "$base.pgdump"     | awk '{print $1}')" == "$pg_sha"  ]]; then grn "  ✓ pgdump sha256 OK"; else red "  ✗ pgdump sha256 MISMATCH"; fail=1; fi
  if [[ "$(sha256sum "$base.cypher.gz"  | awk '{print $1}')" == "$neo_sha" ]]; then grn "  ✓ cypher.gz sha256 OK"; else red "  ✗ cypher.gz sha256 MISMATCH"; fail=1; fi

  # 2. structural integrity
  if gzip -t "$base.cypher.gz" 2>/dev/null; then grn "  ✓ cypher.gz gzip integrity OK"; else red "  ✗ cypher.gz corrupt"; fail=1; fi
  if command -v pg_restore >/dev/null; then
    if pg_restore --list "$base.pgdump" >/dev/null 2>&1; then grn "  ✓ pgdump archive readable (pg_restore --list)"; else red "  ✗ pgdump archive unreadable"; fail=1; fi
  else
    ylw "  ! pg_restore not on host — skipped archive TOC check (sha256 still validated)"
  fi

  echo
  [[ "$fail" -eq 0 ]] && { grn "Backup set VERIFIED."; return 0; } || die "Backup set FAILED verification."
}

do_backup() {
  mkdir -p "$BACKUP_DIR"
  local ts name base
  ts="$(date +%Y%m%d-%H%M%S)"; name="${PREFIX}-${ts}"; base="$BACKUP_DIR/$name"
  echo "Shared Memory — backup → $base.*"

  # Quiesce (best-effort unless required). Trap guarantees resume on any exit.
  trap resume EXIT INT TERM
  if quiesce; then drain_outbox
  else
    if [[ "$BACKUP_QUIESCE_REQUIRED" == "1" ]]; then die "quiesce required but unavailable (set BACKUP_ADMIN_TOKEN / check gateway)"; fi
    ylw "  ! proceeding WITHOUT quiesce (gateway unreachable or no admin token) — online dumps, restore self-heals"
  fi

  # Counts captured under quiesce for the manifest.
  local nodes rels; nodes="$(neo4j_count 'MATCH (n) RETURN count(n)')"; rels="$(neo4j_count 'MATCH ()-[r]->() RETURN count(r)')"

  # Dump Postgres FIRST (source of truth + outbox), then Neo4j.
  dump_postgres "$base.pgdump"   || die "pg_dump failed"
  grn "  ✓ postgres dumped"
  dump_neo4j    "$base.cypher.gz" || die "neo4j APOC export failed (is NEO4J_apoc_export_file_enabled=true?)"
  grn "  ✓ neo4j dumped"

  resume   # release the quiesce as early as possible — dumps are done

  # Manifest LAST — its presence marks the set complete (verify keys off it).
  local pg_sha neo_sha pg_toc
  pg_sha="$(sha256sum "$base.pgdump"    | awk '{print $1}')"
  neo_sha="$(sha256sum "$base.cypher.gz" | awk '{print $1}')"
  pg_toc="$(command -v pg_restore >/dev/null && pg_restore --list "$base.pgdump" 2>/dev/null | grep -cvE '^;|^$' || echo 0)"
  python3 - "$base.manifest.json" <<PY
import json, sys
json.dump({
  "name": "$name", "created": "$(date -Iseconds)",
  "pg_db": "$PG_DB",
  "pg_file": "$name.pgdump", "pg_sha256": "$pg_sha", "pg_toc_entries": int("$pg_toc" or 0),
  "neo4j_file": "$name.cypher.gz", "neo4j_sha256": "$neo_sha",
  "neo4j_nodes": "${nodes:-}", "neo4j_rels": "${rels:-}",
}, open(sys.argv[1], "w"), indent=2)
PY
  grn "  ✓ manifest written"

  # Retention — only ever touches our own prefix, older than N days.
  local pruned=0
  while IFS= read -r f; do rm -f "$f"; pruned=$((pruned+1)); done < <(
    find "$BACKUP_DIR" -maxdepth 1 -name "${PREFIX}-*" -type f -mtime +"$BACKUP_RETENTION_DAYS" 2>/dev/null)
  (( pruned > 0 )) && ylw "  ! pruned $pruned file(s) older than ${BACKUP_RETENTION_DAYS}d"

  echo; grn "Backup complete: $name (nodes=${nodes:-?} rels=${rels:-?})"
}

# ── Single-instance lock + dispatch ──────────────────────────────────────────
mkdir -p "$(dirname "$BACKUP_LOCKFILE")"
exec 9>"$BACKUP_LOCKFILE" || die "cannot open lockfile $BACKUP_LOCKFILE"
if ! flock -n 9; then die "another backup is already running (lock: $BACKUP_LOCKFILE)"; fi

case "$MODE" in
  dry-run) do_dry_run ;;
  verify)  do_verify ;;
  backup)  do_backup ;;
esac
